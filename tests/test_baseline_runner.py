import csv
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
import uuid

ROOT=Path(__file__).resolve().parents[1]

class RunnerTests(unittest.TestCase):
    def test_http_runner_full_50_case_fixture(self):
        # HTTP fixture validates runner I/O only; its timing must not enter real baselines.
        with tempfile.TemporaryDirectory() as directory:
            directory=Path(directory);trace=directory/'trace.jsonl'
            class Handler(BaseHTTPRequestHandler):
                def log_message(self,*args):pass
                def do_GET(self):
                    self.send_response(200);self.end_headers();self.wfile.write(b'{}')
                def do_POST(self):
                    data=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                    tid=uuid.uuid4().hex
                    with trace.open('a') as out:
                        out.write(json.dumps(dict(trace_id=tid,failed=False,events=[dict(kind='llm',name='generation',error_type=None,timeout=False,total_tokens=None)]))+'\n')
                    self.send_response(200);self.send_header('X-Trace-ID',tid);self.end_headers()
                    self.wfile.write(json.dumps({'response':'fixture','from_cache':False}).encode())
            server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            try:
                result=subprocess.run([sys.executable,str(ROOT/'benchmarks/run_baseline.py'),
                    '--base-url',f'http://127.0.0.1:{server.server_port}',
                    '--model','FAKE_FIXTURE','--top-k','5','--cache-state','uncontrolled',
                    '--trace',str(trace),'--output',str(directory/'output')],capture_output=True,text=True)
                self.assertEqual(result.returncode,0,result.stderr)
                summary=json.loads((directory/'output/manifest.json').read_text())['summary']['all']
                self.assertEqual(summary['requests'],50)
                self.assertEqual(summary['successes'],50)
                self.assertEqual(summary['trace_coverage'],1)
                self.assertEqual(summary['llm_calls'],50)
                with (directory/'output/baseline.csv').open() as f:rows=list(csv.DictReader(f))
                self.assertEqual(len(rows),50)
                self.assertTrue(all(r['total_tokens']=='' for r in rows))
                self.assertEqual(len(trace.read_text().splitlines()),70) # 20 seed turns
            finally:
                server.shutdown();server.server_close();thread.join()

if __name__=='__main__':unittest.main()
