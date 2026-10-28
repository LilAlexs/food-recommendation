"""Compare saved manifests. No provider calls; no fabricated quality metrics."""
import argparse
import csv
import json
from pathlib import Path

p=argparse.ArgumentParser()
p.add_argument('runs',nargs='+',type=Path)
p.add_argument('--output',type=Path,default=Path('benchmark_results/comparison.csv'))
args=p.parse_args()
rows=[]
for directory in args.runs:
    manifest=json.loads((directory/'manifest.json').read_text(encoding='utf-8'))
    with (directory/'baseline.csv').open(encoding='utf-8-sig') as f:
        cases=list(csv.DictReader(f))
    all_metrics=manifest.get('summary',{}).get('all',{})
    calls=[int(c['llm_calls']) for c in cases if c.get('llm_calls')!='' and c.get('llm_calls') is not None]
    rows.append(dict(run=directory.name,model=manifest['model'],status=manifest['status'],
        dataset_sha256=manifest.get('dataset_sha256'),top_k=manifest['top_k'],cache_state=manifest['cache_state'],
        adaptive_context=';'.join(sorted({c['adaptive_context'] for c in cases if c.get('adaptive_context')})),
        fast_simple_query=';'.join(sorted({c['fast_simple_query'] for c in cases if c.get('fast_simple_query')})),
        scenarios=all_metrics.get('requests'),successes=all_metrics.get('successes'),
        failure_rate=all_metrics.get('failure_rate'),p50_success_ms=all_metrics.get('p50_success_ms'),
        p95_success_ms=all_metrics.get('p95_success_ms'),
        mean_llm_calls=sum(calls)/len(calls) if calls else None,
        quality_status='pending_manual_review'))
args.output.parent.mkdir(parents=True,exist_ok=True)
with args.output.open('w',encoding='utf-8-sig',newline='') as f:
    w=csv.DictWriter(f,fieldnames=list(rows[0]));w.writeheader();w.writerows(rows)
print(args.output)
