"""Dependency-free monitoring tests; fake providers are NOT a performance baseline."""
import concurrent.futures
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest

ROOT = Path(__file__).resolve().parents[1]
# Load only the inspected modules: production package __init__ eagerly imports GPU/DB dependencies.
pkg = types.ModuleType('rag_modules');pkg.__path__=[str(ROOT/'rag_modules')]
sys.modules['rag_modules']=pkg
from rag_modules import performance_monitor as m


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.path=Path(self.tmp.name)/'traces.jsonl'
        self.old=os.environ.get('RAG_TRACE_PATH')
        os.environ['RAG_TRACE_PATH']=str(self.path)

    def tearDown(self):
        if self.old is None: os.environ.pop('RAG_TRACE_PATH',None)
        else: os.environ['RAG_TRACE_PATH']=self.old
        self.tmp.cleanup()

    def read(self):
        return [json.loads(s) for s in self.path.read_text().splitlines()]

    def test_parallel_context_and_request_isolation(self):
        def worker():
            with m.request_trace('test'):
                with concurrent.futures.ThreadPoolExecutor(2) as pool:
                    futures=[m.submit_context(pool,m.llm_create,lambda **kw: types.SimpleNamespace(usage=None),model='fake') for _ in range(4)]
                    for f in futures:f.result()
        with concurrent.futures.ThreadPoolExecutor(3) as pool:
            list(pool.map(lambda _: worker(),range(3)))
        rows=self.read()
        self.assertEqual(len({r['trace_id'] for r in rows}),3)
        self.assertEqual([len(r['events']) for r in rows],[4]*3)
        self.assertIsNone(m._current.get())

    def test_tokens_and_timeout(self):
        usage=types.SimpleNamespace(prompt_tokens=3,completion_tokens=4,total_tokens=7)
        def fail(**kw): raise TimeoutError('secret query must not be logged')
        with m.request_trace('test'):
            m.llm_create(lambda **kw: types.SimpleNamespace(usage=usage),model='fake')
            with self.assertRaises(TimeoutError):m.llm_create(fail,model='fake')
        calls=self.read()[0]['events']
        self.assertEqual(calls[0]['total_tokens'],7)
        self.assertTrue(calls[1]['timeout'])
        self.assertNotIn('secret query',self.path.read_text())

    def test_stream_usage_only_chunk_and_close(self):
        class Stream:
            closed=False
            def __iter__(self):
                yield types.SimpleNamespace(usage=None,choices=['x'])
                yield types.SimpleNamespace(usage=types.SimpleNamespace(total_tokens=9),choices=[])
            def close(self):self.closed=True
        stream=Stream()
        with m.request_trace('stream'):
            self.assertEqual(len(list(m.llm_create(lambda **kw:stream,stream=True))),2)
        self.assertTrue(stream.closed)
        self.assertEqual(self.read()[0]['events'][0]['total_tokens'],9)

    def test_stream_iteration_failure(self):
        def stream():
            yield 1
            raise TimeoutError()
        with m.request_trace('stream'):
            with self.assertRaises(TimeoutError):list(m.llm_create(lambda **kw:stream(),stream=True))
        self.assertTrue(self.read()[0]['events'][0]['timeout'])

    def test_generator_trace_covers_consumption_and_disconnect(self):
        @m.traced('sse')
        def stream():
            yield 1
            yield 2
        gen=stream();self.assertFalse(self.path.exists())
        self.assertEqual(next(gen),1);gen.close()
        self.assertTrue(self.read()[0]['failed'])
        self.assertIsNone(m._current.get())

    def test_disabled_monitoring_preserves_result(self):
        os.environ.pop('RAG_TRACE_PATH')
        @m.traced('test')
        @m.timed('stage')
        def f():return 42
        self.assertEqual(f(),42)
        self.assertFalse(self.path.exists())

    def test_swallowed_http_error(self):
        @m.traced('chat')
        def f(): return types.SimpleNamespace(headers={}),500
        f();self.assertTrue(self.read()[0]['failed'])

    def test_real_router_all_three_branches(self):
        documents=types.ModuleType('langchain_core.documents')
        documents.Document=object
        core=types.ModuleType('langchain_core');core.documents=documents
        sys.modules['langchain_core']=core;sys.modules['langchain_core.documents']=documents
        from rag_modules.intelligent_query_router import IntelligentQueryRouter
        for strategy in ['hybrid_traditional','graph_rag','combined']:
            def create(**kw):
                return types.SimpleNamespace(choices=[types.SimpleNamespace(message=types.SimpleNamespace(content=json.dumps({'recommended_strategy':strategy})))],usage=None)
            client=types.SimpleNamespace(chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=create)))
            class Retrieval:
                @m.timed('fake.retrieval')
                def hybrid_search(self,*args):
                    m.llm_create(lambda **kw: types.SimpleNamespace(usage=None),model='fake')
                    return [types.SimpleNamespace(page_content='one',metadata={'recipe_id':'r1'})]
                graph_rag_search=hybrid_search
            router=IntelligentQueryRouter(Retrieval(),Retrieval(),client,types.SimpleNamespace(llm_model='fake'))
            with m.request_trace(strategy):
                docs,analysis=router.route_query('test',5)
                self.assertTrue(docs)
            trace=self.read()[-1]
            self.assertEqual(sum(e['kind']=='llm' for e in trace['events']),3 if strategy=='combined' else 2)
            self.assertTrue(any(e['kind']=='retrieval' for e in trace['events']))

    def test_dataset_structure(self):
        rows=[json.loads(s) for s in (ROOT/'benchmarks/queries.jsonl').read_text().splitlines()]
        self.assertEqual(len(rows),50);self.assertEqual(len({r['id'] for r in rows}),50)
        self.assertEqual({c:sum(r['category']==c for r in rows) for c in {r['category'] for r in rows}},dict.fromkeys(['independent','followup','relationship','constraints','switch'],10))
        self.assertTrue(all(r['relevant_recipe_ids'] is None for r in rows))

if __name__=='__main__':unittest.main()
