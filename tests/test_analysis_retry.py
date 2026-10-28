"""Failure recovery with fake providers/clocks, plus a real local-wait deadline."""
import json
import os
from pathlib import Path
import sys
import threading
import time
import types
import unittest
from unittest.mock import patch

if 'rag_modules' not in sys.modules:
    pkg=types.ModuleType('rag_modules');pkg.__path__=[str(Path(__file__).resolve().parents[1]/'rag_modules')]
    sys.modules['rag_modules']=pkg
from rag_modules.analysis_retry import (request_analysis, parse_analysis, AnalysisFailure,
                                       AnalysisTimeout, wait_for_call)

GOOD=dict(kind='recipe',target=None,reference='current',confidence=.88)
MESSAGES=[{'role':'system','content':'仅分析'},{'role':'user','content':'问题'}]


def response(data=GOOD):
    text=json.dumps(data) if not isinstance(data,str) else data
    return types.SimpleNamespace(choices=[types.SimpleNamespace(message=types.SimpleNamespace(content=text))])


class HTTPError(Exception):
    def __init__(self,status,headers=None,code=None):
        self.status_code=status;self.code=code
        self.response=types.SimpleNamespace(headers=headers or {})


class Client:
    def __init__(self,results):
        self.results=list(results);self.options=[];self.requests=[]
        self.chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=self.create))

    def with_options(self,**kw):self.options.append(kw);return self

    def create(self,**kw):
        self.requests.append(kw)
        value=self.results.pop(0)
        if isinstance(value,Exception):raise value
        return value


class Clock:
    def __init__(self):self.value=0
    def __call__(self):return self.value
    def sleep(self,seconds):self.value+=seconds


class RetryTests(unittest.TestCase):
    def setUp(self):
        self.env=patch.dict(os.environ,{'QUERY_ANALYSIS_BUDGET_SECONDS':'25','QUERY_ANALYSIS_ATTEMPT_SECONDS':'15'})
        self.env.start();self.addCleanup(self.env.stop)
        self.clock=Clock()

    def request(self,client,wait=None):
        return request_analysis(client,'fixture',MESSAGES,clock=self.clock,sleep=self.clock.sleep,
                                wait=wait or (lambda create,timeout,kw:create(**kw)))

    def test_success_one_call_sdk_retry_disabled(self):
        c=Client([response()]);self.assertEqual(self.request(c),GOOD)
        self.assertEqual(c.options,[dict(timeout=15,max_retries=0)])
        self.assertEqual(len(c.requests),1)

    def test_transient_connection_retries_once(self):
        c=Client([ConnectionError(),response()]);self.assertEqual(self.request(c),GOOD)
        self.assertEqual(len(c.requests),2);self.assertEqual(self.clock.value,.2)

    def test_server_status_retries_once(self):
        for status in [408,409,429,500,502,503,504]:
            c=Client([HTTPError(status),response()]);self.assertEqual(self.request(c),GOOD)
            self.assertEqual(len(c.requests),2)

    def test_auth_and_parameters_no_retry(self):
        for status in [400,401,403,404,422]:
            c=Client([HTTPError(status),response()])
            with self.assertRaises(AnalysisFailure) as ctx:self.request(c)
            self.assertEqual(ctx.exception.reason,'permanent');self.assertFalse(ctx.exception.retryable)
            self.assertEqual(len(c.requests),1)

    def test_quota_error_no_retry(self):
        c=Client([HTTPError(429,code='insufficient_quota'),response()])
        with self.assertRaises(AnalysisFailure) as ctx:self.request(c)
        self.assertEqual(ctx.exception.reason,'quota');self.assertEqual(len(c.requests),1)

    def test_format_fence_recovered_locally(self):
        c=Client([response('```json\n'+json.dumps(GOOD)+'\n```')])
        self.assertEqual(self.request(c),GOOD);self.assertEqual(len(c.requests),1)

    def test_bad_json_gets_one_correction(self):
        c=Client([response('{bad'),response()]);self.assertEqual(self.request(c),GOOD)
        self.assertEqual(len(c.requests),2)
        self.assertIn('上一条输出不是合规JSON',c.requests[1]['messages'][-1]['content'])
        self.assertEqual(MESSAGES,[{'role':'system','content':'仅分析'},{'role':'user','content':'问题'}])

    def test_schema_error_also_gets_one_correction(self):
        c=Client([response(dict(kind='recipe',reference='current',confidence=True)),response()])
        self.assertEqual(self.request(c),GOOD);self.assertEqual(len(c.requests),2)

    def test_two_bad_outputs_stop(self):
        c=Client([response('{}'),response('{}'),response()])
        with self.assertRaises(AnalysisFailure) as ctx:self.request(c)
        self.assertEqual(ctx.exception.reason,'format');self.assertEqual(ctx.exception.attempts,2)
        self.assertEqual(len(c.requests),2)

    def test_mixed_failure_types_share_two_call_cap(self):
        for values in [[TimeoutError(),response('{}'),response()], [response('{}'),HTTPError(503),response()]]:
            c=Client(values)
            with self.assertRaises(AnalysisFailure):self.request(c)
            self.assertEqual(len(c.requests),2)

    def test_clarify_and_low_confidence_are_normal_outputs_not_retried(self):
        for data in [dict(kind='clarify',target=None,reference='none',confidence=.35),GOOD]:
            c=Client([response(data)]);self.assertEqual(self.request(c),data)
            self.assertEqual(len(c.requests),1)

    def test_second_attempt_gets_only_remaining_budget(self):
        c=Client([TimeoutError(),response()]);timeouts=[]
        def wait(create,timeout,kw):
            timeouts.append(timeout)
            if len(timeouts)==1:self.clock.value+=15
            else:self.clock.value+=1
            return create(**kw)
        self.assertEqual(self.request(c,wait),GOOD)
        self.assertEqual(timeouts[0],15);self.assertAlmostEqual(timeouts[1],9.8)
        self.assertLess(self.clock.value,25)

    def test_exhausted_budget_does_not_start_second_call(self):
        c=Client([response(),response()])
        def wait(create,timeout,kw):
            self.clock.value+=25
            return create(**kw)
        with self.assertRaises(AnalysisFailure) as ctx:self.request(c,wait)
        self.assertEqual(ctx.exception.reason,'budget');self.assertEqual(len(c.requests),1)

    def test_retry_after_honored_within_budget(self):
        c=Client([HTTPError(429,{'retry-after':'2'}),response()]);self.assertEqual(self.request(c),GOOD)
        self.assertEqual(self.clock.value,2)

    def test_retry_after_larger_than_budget_does_not_retry_early(self):
        c=Client([HTTPError(429,{'retry-after':'60'}),response()])
        with self.assertRaises(AnalysisFailure) as ctx:self.request(c)
        self.assertEqual(ctx.exception.reason,'budget');self.assertEqual(len(c.requests),1)

    def test_unexpected_error_no_retry(self):
        c=Client([RuntimeError('private details'),response()])
        with self.assertRaises(AnalysisFailure) as ctx:self.request(c)
        self.assertEqual(ctx.exception.reason,'unexpected');self.assertNotIn('private',str(ctx.exception))
        self.assertEqual(len(c.requests),1)

    def test_invalid_configuration_makes_no_calls(self):
        c=Client([response()])
        with patch.dict(os.environ,{'QUERY_ANALYSIS_BUDGET_SECONDS':'-1'}):
            with self.assertRaises(AnalysisFailure) as ctx:self.request(c)
        self.assertEqual(ctx.exception.reason,'configuration');self.assertEqual(c.requests,[])

    def test_local_wait_ends_even_if_sdk_callback_is_still_blocked(self):
        release=threading.Event()
        def blocked(**kwargs):release.wait(2);return response()
        started=time.monotonic()
        try:
            with self.assertRaises(AnalysisTimeout):wait_for_call(blocked,.03,{})
            self.assertLess(time.monotonic()-started,.5)
        finally:release.set()
