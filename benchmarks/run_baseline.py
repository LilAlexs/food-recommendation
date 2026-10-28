"""Serial HTTP evaluation with partial checkpoints and fail-fast transport handling."""
import argparse
import csv
import hashlib
import json
import math
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path


def atomic_json(path, value):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)


def percentile(values, q):
    values = sorted(values)
    return values[max(0, math.ceil(len(values)*q)-1)] if values else None


def post(base, message, session, timeout):
    request = urllib.request.Request(base+'/api/chat', data=json.dumps(
        {'message': message, 'session_id': session}).encode(),
        headers={'Content-Type': 'application/json'})
    start = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.load(response)
            return dict(duration_ms=(time.perf_counter()-start)*1000,
                        http_status=response.status, trace_id=response.headers.get('X-Trace-ID'),
                        from_cache=data.get('from_cache', False),
                        response=data.get('response'), error_type=None,
                        recipe_status=data.get('recipe_status'), answer_source=data.get('answer_source'),
                        recipe_id=data.get('recipe_id'), options=data.get('options'),
                        retrieval_backend=data.get('retrieval_backend'), graph_recipe_id=data.get('graph_recipe_id'),
                        vector_status=data.get('vector_status'))
    except urllib.error.HTTPError as exc:
        return dict(duration_ms=(time.perf_counter()-start)*1000,
                    http_status=exc.code, trace_id=exc.headers.get('X-Trace-ID'),
                    from_cache=False, response=None, error_type='HTTPError', recipe_status=None, answer_source=None, recipe_id=None, options=None, retrieval_backend=None, graph_recipe_id=None, vector_status=None)
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
        return dict(duration_ms=(time.perf_counter()-start)*1000,
                    http_status=getattr(exc, 'code', None), trace_id=None,
                    from_cache=None, response=None, error_type=type(exc).__name__, recipe_status=None, answer_source=None, recipe_id=None, options=None, retrieval_backend=None, graph_recipe_id=None, vector_status=None)


def checkpoint(args, rows, manifest):
    rows = [dict(row) for row in rows]
    traces = {}
    if args.trace and args.trace.exists():
        for line in args.trace.read_text(encoding='utf-8').splitlines():
            try:
                trace = json.loads(line); traces[trace['trace_id']] = trace
            except (ValueError, KeyError):
                continue # an actively appended last line may be incomplete
    for row in rows:
        trace = traces.get(row['trace_id'])
        for seed_id in row['seed_trace_ids']:
            seed_trace = traces.get(seed_id)
            if seed_trace and (seed_trace['failed'] or any(
                    e['kind']=='llm' and e['error_type'] for e in seed_trace['events'])):
                row['seed_failed'] = True
        row['trace_available'] = trace is not None
        row['application_failed'] = trace['failed'] if trace else None
        row['failure_layer'] = ('application' if trace and trace['failed'] else
                                'llm' if trace and any(e['kind']=='llm' and e['error_type'] for e in trace['events']) else
                                'client_or_http' if row['error_type'] else None)
        row['adaptive_context'] = trace.get('features', {}).get('RAG_ADAPTIVE_CONTEXT') if trace else None
        row['fast_simple_query'] = trace.get('features', {}).get('RAG_FAST_SIMPLE_QUERY') if trace else None
        calls = [e for e in trace['events'] if e['kind']=='llm'] if trace else []
        row['llm_calls'] = len(calls) if trace else None
        row['llm_errors'] = sum(bool(e['error_type']) for e in calls) if trace else None
        row['llm_timeouts'] = sum(bool(e['timeout']) for e in calls) if trace else None
        row['tokens_known'] = bool(trace) and all(e['total_tokens'] is not None for e in calls)
        row['total_tokens'] = sum(e['total_tokens'] for e in calls) if row['tokens_known'] else None
        row['success'] = (row['http_status']==200 and bool(row['response'])
                          and not row['seed_failed'] and not row['application_failed']
                          and not row['llm_errors'])
        # Exceptions may be swallowed by legacy modules; traces expose degraded requests.
        for name in ['route.analysis','graph.total','vector.search','hybrid.total','generation']:
            durations = [e['duration_ms'] for e in trace['events'] if e['kind']=='span' and e['name']==name] if trace else []
            row[name+'_ms'] = sum(durations) if durations else None
    fields = [k for k in (rows[0] if rows else dict(id=None, category=None, duration_ms=None, http_status=None, error_type=None)) if k != 'response']
    with (args.output/'baseline.csv').open('w', encoding='utf-8-sig', newline='') as out:
        writer = csv.DictWriter(out, fieldnames=fields);writer.writeheader()
        writer.writerows({k:r[k] for k in fields} for r in rows)
    summary = {}
    for category in (['all'] if rows else [])+sorted({r['category'] for r in rows}):
        group = [r for r in rows if category=='all' or r['category']==category]
        valid = [r['duration_ms'] for r in group if r['success']]
        summary[category] = dict(requests=len(group),successes=len(valid),
            failure_rate=1-len(valid)/len(group),p50_success_ms=percentile(valid,.5),
            p95_success_ms=percentile(valid,.95),
            trace_coverage=sum(r['trace_available'] for r in group)/len(group),
            llm_calls=sum(r['llm_calls'] or 0 for r in group) if all(r['trace_available'] for r in group) else None,
            llm_errors=sum(r['llm_errors'] or 0 for r in group) if all(r['trace_available'] for r in group) else None,
            llm_timeouts=sum(r['llm_timeouts'] or 0 for r in group) if all(r['trace_available'] for r in group) else None)
    manifest['last_scenario_success'] = rows[-1]['success'] if rows else None
    manifest.update(updated_at_unix=time.time(),
                    quality_status='pending labels and review; no retrieval/constraint scores claimed',
                    latency_scope='HTTP client wall clock; seed turns excluded; includes cache hits', summary=summary)
    atomic_json(args.output/'manifest.json', manifest)
    return summary

def main():
    p = argparse.ArgumentParser()
    p.add_argument('--base-url', default='http://127.0.0.1:8000')
    p.add_argument('--queries', type=Path, default=Path(__file__).with_name('queries.jsonl'))
    p.add_argument('--output', type=Path, default=Path('benchmark_results'))
    p.add_argument('--trace', type=Path)
    p.add_argument('--repeat', type=int, default=1)
    p.add_argument('--limit', type=int, default=0, help='0 = all scenarios')
    p.add_argument('--timeout', type=float, default=240, help='Socket I/O timeout; not server cancellation')
    p.add_argument('--max-consecutive-failures', type=int, default=1)
    p.add_argument('--summarize-only', action='store_true', help='Rebuild CSV/manifest from output/responses.jsonl without requests')
    p.add_argument('--cache-state', required=True, choices=['cold','warm','uncontrolled'])
    p.add_argument('--model', required=True)
    p.add_argument('--top-k', type=int, required=True)
    args = p.parse_args()
    if args.repeat < 1 or args.limit < 0 or args.timeout <= 0 or args.max_consecutive_failures < 1:
        p.error('repeat, timeout, max-consecutive-failures must be positive; limit must be >=0')
    args.output.mkdir(parents=True, exist_ok=True)
    raw_path = args.output/'responses.jsonl'
    manifest = dict(base_url=args.base_url, model=args.model, top_k=args.top_k,
        cache_state=args.cache_state, repeat=args.repeat, limit=args.limit, concurrency=1,
        timeout_seconds=args.timeout, max_consecutive_failures=args.max_consecutive_failures,
        started_at_unix=time.time(), status='running',
        note='Model/top_k/cache state are operator declarations. HTTP timeout does not cancel backend work.')
    if args.summarize_only:
        rows=[]
        for line in raw_path.read_text(encoding='utf-8').splitlines():
            try: rows.append(json.loads(line))
            except ValueError: manifest['incomplete_lines_skipped']=manifest.get('incomplete_lines_skipped',0)+1
        manifest.update(status='recovered_partial', completed_scenarios=len(rows))
        checkpoint(args, rows, manifest)
        print('Recovered partial results. No backend requests sent.')
        return 0
    if raw_path.exists() and raw_path.stat().st_size:
        p.error('Output contains responses.jsonl; choose a NEW output directory or use --summarize-only.')
    cases=[json.loads(line) for line in args.queries.read_text(encoding='utf-8').splitlines() if line.strip()]
    if args.limit: cases=cases[:args.limit]
    if not cases: p.error('Dataset is empty')
    manifest.update(dataset_sha256=hashlib.sha256(args.queries.read_bytes()).hexdigest(),
                    planned_scenarios=len(cases)*args.repeat, completed_scenarios=0)
    rows=[]
    checkpoint(args, rows, manifest)
    try:
        with urllib.request.urlopen(args.base_url+'/health',timeout=5) as response:
            if response.status != 200: raise ValueError('health status')
    except Exception as exc:
        manifest.update(status='blocked', error_type=type(exc).__name__)
        checkpoint(args, rows, manifest)
        print('BLOCKED: backend unavailable. No performance baseline produced.')
        return 2
    consecutive=0
    stop=False
    try:
        with raw_path.open('w',encoding='utf-8') as out:
            for repeat in range(args.repeat):
                for case in cases:
                    session=uuid.uuid4().hex
                    seed_ids=[]
                    failed_seed=None
                    for seed in case['history']:
                        result=post(args.base_url,seed,session,args.timeout)
                        seed_ids.append(result['trace_id'])
                        if result['error_type'] or not result['response']:
                            failed_seed=result
                            break # dependent target must not run after failed setup
                    result=failed_seed or post(args.base_url,case['message'],session,args.timeout)
                    row=dict(id=case['id'],category=case['category'],repeat=repeat,
                        seed_failed=failed_seed is not None,seed_trace_ids=seed_ids,
                        request_scope='seed_failure' if failed_seed else 'target', **result)
                    rows.append(row)
                    out.write(json.dumps(row,ensure_ascii=False)+'\n');out.flush()
                    manifest['completed_scenarios']=len(rows)
                    checkpoint(args,rows,manifest)
                    failed=bool(result['error_type'] or not result['response'] or manifest['last_scenario_success'] is False)
                    consecutive=consecutive+1 if failed else 0
                    print(f"[{len(rows)}/{manifest['planned_scenarios']}] {case['id']}: "
                          f"{result['duration_ms']/1000:.1f}s {result['error_type'] or result['http_status']}",flush=True)
                    if consecutive >= args.max_consecutive_failures:
                        manifest.update(status='stopped_after_failures',
                            stop_reason='Application, LLM or client failure; inspect failure_layer. Client timeout does not cancel backend work.')
                        stop=True
                        break
                if stop: break
        if not stop: manifest['status']='finished'
    except KeyboardInterrupt:
        manifest.update(status='interrupted',stop_reason='User interrupt; in-flight request may continue on backend.')
        print('Interrupted. Completed scenarios preserved; current in-flight scenario is not counted.')
    except Exception as exc:
        manifest.update(status='error',error_type=type(exc).__name__)
        raise
    finally:
        manifest['finished_at_unix']=time.time()
        checkpoint(args,rows,manifest)
    print(f"Saved {len(rows)} scenarios; status={manifest['status']}; output={args.output}")
    return 0 if manifest['status']=='finished' else 3

if __name__=='__main__':
    raise SystemExit(main())
