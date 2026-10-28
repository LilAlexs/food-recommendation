import contextlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch, MagicMock

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('baseline_v2',ROOT/'benchmarks/run_baseline.py')
b=importlib.util.module_from_spec(spec);spec.loader.exec_module(b)

class ResilienceTests(unittest.TestCase):
    def args(self,directory,*extra):
        return ['runner','--output',str(directory),'--model','fixture','--top-k','5',
                '--cache-state','uncontrolled','--limit','2',*extra]

    def result(self,error=None):
        return dict(duration_ms=10,http_status=None if error else 200,trace_id=None,
                    from_cache=False,response=None if error else 'answer',error_type=error)

    def run_main(self,directory,results,*extra):
        health=MagicMock();health.__enter__.return_value.status=200
        with patch.object(sys,'argv',self.args(directory,*extra)), patch.object(b.urllib.request,'urlopen',return_value=health),patch.object(b,'post',side_effect=results) as calls,contextlib.redirect_stdout(io.StringIO()):
            code=b.main()
        return code,calls.call_count,json.loads((directory/'manifest.json').read_text())

    def test_fail_fast_preserves_first_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            d=Path(tmp);code,count,m=self.run_main(d,[self.result('TimeoutError')])
            self.assertEqual(code,3);self.assertEqual(count,1)
            self.assertEqual(m['status'],'stopped_after_failures')
            self.assertTrue((d/'baseline.csv').exists())
            self.assertEqual(m['summary']['all']['successes'],0)

    def test_interrupt_after_completed_row(self):
        with tempfile.TemporaryDirectory() as tmp:
            d=Path(tmp);code,count,m=self.run_main(d,[self.result(),KeyboardInterrupt()])
            self.assertEqual(m['status'],'interrupted');self.assertEqual(count,2)
            self.assertEqual(m['completed_scenarios'],1)
            self.assertEqual(len((d/'responses.jsonl').read_text().splitlines()),1)
            self.assertTrue((d/'baseline.csv').exists())

    def test_interrupt_first_request_writes_empty_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            d=Path(tmp);code,_,m=self.run_main(d,[KeyboardInterrupt()])
            self.assertEqual(m['status'],'interrupted')
            self.assertEqual(m['summary'],{})
            self.assertTrue((d/'baseline.csv').exists())

    def test_recover_old_output_without_network(self):
        with tempfile.TemporaryDirectory() as tmp:
            d=Path(tmp);row=dict(id='old',category='independent',repeat=0,seed_failed=False,seed_trace_ids=[],**self.result('TimeoutError'))
            (d/'responses.jsonl').write_text(json.dumps(row)+'\n{"incomplete":')
            with patch.object(sys,'argv',self.args(d,'--summarize-only')),patch.object(b,'post') as post,contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(b.main(),0);post.assert_not_called()
            m=json.loads((d/'manifest.json').read_text())
            self.assertEqual(m['status'],'recovered_partial')
            self.assertEqual(m['incomplete_lines_skipped'],1)

    def test_seed_failure_does_not_send_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            d=Path(tmp);queries=d/'cases.jsonl'
            queries.write_text(json.dumps(dict(id='follow',category='followup',history=['seed'],message='target')))
            code,count,m=self.run_main(d,[self.result('TimeoutError')],'--queries',str(queries))
            self.assertEqual(count,1)
            self.assertEqual(m['status'],'stopped_after_failures')
            self.assertEqual(json.loads((d/'responses.jsonl').read_text())['request_scope'],'seed_failure')

    def test_output_reuse_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            d=Path(tmp);(d/'responses.jsonl').write_text('preserve me')
            with patch.object(sys,'argv',self.args(d)),contextlib.redirect_stderr(io.StringIO()),self.assertRaises(SystemExit):b.main()
            self.assertEqual((d/'responses.jsonl').read_text(),'preserve me')

if __name__=='__main__':unittest.main()
