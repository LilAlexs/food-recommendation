"""Two SDK calls maximum, classified recovery, shared wall-clock wait budget.
SDK retries are disabled. Late network results cannot mutate conversation state.
"""
import json
import os
import queue
import re
import threading
import time
from rag_modules.performance_monitor import event, llm_create, span


class AnalysisFormatError(ValueError):
    pass


class AnalysisTimeout(TimeoutError):
    pass


class AnalysisFailure(RuntimeError):
    def __init__(self, reason, retryable, attempts):
        super().__init__(reason)
        self.reason=reason
        self.retryable=retryable
        self.attempts=attempts


def parse_analysis(text):
    if not isinstance(text,str) or len(text)>16000:
        raise AnalysisFormatError('Invalid output type or size')
    value=text.strip()
    if value.startswith('```'):
        match=re.fullmatch(r'```(?:json)?\s*([\s\S]*?)\s*```',value,re.I)
        if not match:raise AnalysisFormatError('Unclosed JSON fence')
        value=match[1]
    try:
        data=json.loads(value)
    except (ValueError,TypeError) as exc:
        raise AnalysisFormatError('Invalid JSON') from exc
    if not isinstance(data,dict):raise AnalysisFormatError('Expected object')
    if data.get('kind') not in {'recipe','general','clarify'} or data.get('reference') not in {'explicit','current','none'}:
        raise AnalysisFormatError('Invalid enum')
    confidence=data.get('confidence')
    if isinstance(confidence,bool) or not isinstance(confidence,(int,float)) or not 0<=confidence<=1:
        raise AnalysisFormatError('Invalid confidence')
    target=data.get('target')
    if target is not None and (not isinstance(target,str) or not target.strip() or len(target)>80):
        raise AnalysisFormatError('Invalid target')
    return data


def classify(exc):
    if isinstance(exc,AnalysisFormatError):return 'format', True
    status=getattr(exc,'status_code',None)
    status=status if isinstance(status,int) else None
    code=getattr(exc,'code',None)
    if status==429 and isinstance(code,str) and code in {'insufficient_quota','billing_hard_limit_reached'}:
        return 'quota',False
    if status in {408,409,429} or status is not None and 500<=status<600:
        return 'transient',True
    if status is not None and 400<=status<500:return 'permanent',False
    names={c.__name__ for c in type(exc).__mro__}
    if isinstance(exc,(TimeoutError,ConnectionError)) or names & {
        'APITimeoutError','APIConnectionError','ReadTimeout','ConnectTimeout',
        'PoolTimeout','RemoteProtocolError','ConnectError','ReadError','WriteError'}:
        return 'transient',True
    return 'unexpected',False


def retry_delay(exc):
    response=getattr(exc,'response',None)
    headers=getattr(response,'headers',{})
    try:
        value=headers.get('retry-after')
        if isinstance(value,str):
            # Never retry earlier than a numeric Retry-After. An HTTP date is
            # handled conservatively: do not automatically retry it in this budget.
            try:return max(.2,float(value))
            except ValueError:return float('inf')
    except (TypeError,AttributeError):pass
    return .2


def wait_for_call(create, timeout, kwargs):
    """Bound the local wait even when an SDK read timeout resets on traffic.
    Network work can outlive the wait; it only puts a value in this private queue.
    It cannot record late trace events or apply state changes.
    """
    result=queue.Queue(maxsize=1)
    def worker():
        try:result.put((True,create(**kwargs)))
        except Exception as exc:result.put((False,exc))
    threading.Thread(target=worker,daemon=True,name='query-analysis-call').start()
    try:ok,value=result.get(timeout=timeout)
    except queue.Empty as exc:raise AnalysisTimeout('Analysis local wait expired') from exc
    if not ok:raise value
    return value


def request_analysis(client, model, messages, *, clock=None, sleep=None, wait=None):
    clock=clock or time.monotonic
    sleep=sleep or time.sleep
    wait=wait or wait_for_call
    try:
        budget=float(os.getenv('QUERY_ANALYSIS_BUDGET_SECONDS','25'))
        per_attempt=float(os.getenv('QUERY_ANALYSIS_ATTEMPT_SECONDS','15'))
        if not 0<budget<=120 or not 0<per_attempt<=120:raise ValueError()
    except (ValueError,TypeError) as exc:
        raise AnalysisFailure('configuration',False,0) from exc
    deadline=clock()+budget
    messages=[dict(x) for x in messages]
    for attempt in (1,2):
        remaining=deadline-clock()
        if remaining<=0:raise AnalysisFailure('budget',True,attempt-1)
        timeout=min(per_attempt,remaining)
        text=None
        event(kind='query_analysis_attempt',attempt=attempt,remaining_ms=remaining*1000,
              timeout_ms=timeout*1000)
        try:
            bounded=client.with_options(timeout=timeout,max_retries=0)
            kwargs=dict(model=model,messages=messages,temperature=0,max_tokens=256,
                        response_format={'type':'json_object'})
            with span('query.analysis'):
                # Instrument the bounded wait on the request thread, not late SDK workers.
                response=llm_create(lambda **kw:wait(bounded.chat.completions.create,timeout,kw),**kwargs)
            if clock()>=deadline:raise AnalysisTimeout('Shared analysis budget exhausted')
            try:text=response.choices[0].message.content
            except (AttributeError,IndexError,TypeError) as exc:
                raise AnalysisFormatError('Missing response content') from exc
            result=parse_analysis(text)
            event(kind='query_analysis_result',attempt=attempt,outcome='accepted')
            return result
        except Exception as exc:
            category,retryable=classify(exc)
            event(kind='query_analysis_result',attempt=attempt,outcome='failed',
                  category=category,error_type=type(exc).__name__)
            if not retryable or attempt==2:
                raise AnalysisFailure(category,retryable,attempt) from exc
            delay=0 if category=='format' else retry_delay(exc)
            remaining=deadline-clock()
            # Leave a minimum useful window; never shorten server Retry-After.
            if remaining<=delay+.05:
                raise AnalysisFailure('budget',True,attempt) from exc
            if category=='format':
                messages += [{'role':'assistant','content':text[:2000] if isinstance(text,str) else ''},
                    {'role':'user','content':'上一条输出不是合规JSON。请重新分析同一问题，严格返回kind、target、reference、confidence四个字段；不要解释，不要回答菜谱。对象不明确时返回clarify，不要猜测。'}]
            event(kind='query_analysis_retry',next_attempt=attempt+1,reason=category,delay_ms=delay*1000)
            if delay:sleep(delay)
    raise AssertionError('unreachable')
