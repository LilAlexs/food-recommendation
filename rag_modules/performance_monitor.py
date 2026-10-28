"""Opt-in JSONL tracing. No queries, answers, credentials, or exception messages."""
import contextvars
import functools
import inspect
import json
import logging
import os
import threading
import time
import uuid
from contextlib import contextmanager

_current = contextvars.ContextVar("rag_trace", default=None)
_stage = contextvars.ContextVar("rag_stage", default="unknown")
_lock = threading.Lock()


def submit_context(executor, fn, *args, **kwargs):
    # A fresh context per submission; Context cannot be entered concurrently.
    return executor.submit(contextvars.copy_context().run, fn, *args, **kwargs)


def event(**fields):
    trace = _current.get()
    if trace is not None:
        with trace["lock"]:
            trace["events"].append(fields)


def mark_failure(kind):
    event(kind="failure", error_type=kind)


@contextmanager
def span(name):
    start = time.perf_counter()
    token = _stage.set(name)
    error = None
    try:
        yield
    except BaseException as exc:
        error = type(exc).__name__
        raise
    finally:
        event(kind="span", name=name, duration_ms=(time.perf_counter()-start)*1000,
              error_type=error)
        _stage.reset(token)


def timed(name):
    def decorate(fn):
        if inspect.isgeneratorfunction(fn):
            @functools.wraps(fn)
            def stream(*args, **kwargs):
                with span(name):
                    yield from fn(*args, **kwargs)
            return stream
        @functools.wraps(fn)
        def wrapped(*args, **kwargs):
            with span(name):
                result = fn(*args, **kwargs)
                if name == "cache.lookup":
                    event(kind="cache", hit=bool(result))
                if name == "route.total":
                    docs, analysis = result
                    event(kind="retrieval", strategy=analysis.recommended_strategy.value,
                          recipe_ids=[str(d.metadata["recipe_id"]) for d in docs
                                      if d.metadata.get("recipe_id") is not None],
                          document_count=len(docs),
                          candidates=[{k: d.metadata.get(k) for k in
                                       ("recipe_id", "recipe_name", "node_id")} for d in docs])
                return result
        return wrapped
    return decorate


def _usage(response):
    usage = getattr(response, "usage", None)
    return {k: getattr(usage, k, None) for k in
            ("prompt_tokens", "completion_tokens", "total_tokens")}


def llm_create(create, **kwargs):
    """One SDK invocation; SDK internal retries are included in its latency.
    Streaming usage is unknown unless the provider emits it. Never estimate zero.
    """
    start = time.perf_counter()
    name = _stage.get()
    def record(response=None, error=None):
        event(kind="llm", name=name, model=kwargs.get("model"),
              duration_ms=(time.perf_counter()-start)*1000,
              error_type=type(error).__name__ if error else None,
              cause_type=type(error.__cause__).__name__ if error and error.__cause__ else None,
              status_code=getattr(error, "status_code", None),
              timeout=bool(error and "timeout" in type(error).__name__.lower()),
              **_usage(response))
    try:
        response = create(**kwargs)
    except BaseException as exc:
        record(error=exc)
        raise
    if not kwargs.get("stream"):
        record(response)
        return response
    def chunks():
        last_usage = None
        error = None
        try:
            for chunk in response:
                if getattr(chunk, "usage", None) is not None:
                    last_usage = chunk
                yield chunk
        except BaseException as exc:
            error = exc
            raise
        finally:
            record(last_usage, error)
            close = getattr(response, "close", None)
            if close:
                close()
    return chunks()


@contextmanager
def request_trace(endpoint):
    if not os.getenv("RAG_TRACE_PATH") or _current.get() is not None:
        yield _current.get()
        return
    trace = {"trace_id": uuid.uuid4().hex, "endpoint": endpoint,
             "started_at_unix": time.time(),
             "features": {k: os.getenv(k, default) for k, default in
                          (("RAG_ADAPTIVE_CONTEXT","0"), ("RAG_FAST_SIMPLE_QUERY","0"),
                           ("RAG_RECIPE_GUARD","1"), ("RAG_BOUND_RETRIEVAL","1"),
                           ("RAG_SEMANTIC_QUERY","1"))},
             "events": [], "lock": threading.Lock()}
    token = _current.set(trace)
    start = time.perf_counter()
    try:
        yield trace
    except BaseException as exc:
        mark_failure(type(exc).__name__)
        raise
    finally:
        trace["duration_ms"] = (time.perf_counter()-start)*1000
        trace.pop("lock")
        trace["failed"] = any(e.get("kind") == "failure" for e in trace["events"])
        try:
            path = os.environ["RAG_TRACE_PATH"]
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
            with _lock, open(path, "a", encoding="utf-8") as out:
                out.write(json.dumps(trace, ensure_ascii=False)+"\n")
        except Exception:
            logging.getLogger(__name__).exception("Unable to write performance trace")
        finally:
            _current.reset(token)


def traced(endpoint):
    def decorate(fn):
        if inspect.isgeneratorfunction(fn):
            @functools.wraps(fn)
            def stream(*args, **kwargs):
                with request_trace(endpoint):
                    yield from fn(*args, **kwargs)
            return stream
        @functools.wraps(fn)
        def wrapped(*args, **kwargs):
            with request_trace(endpoint) as trace:
                result = fn(*args, **kwargs)
                response = result[0] if isinstance(result, tuple) else result
                status = result[1] if isinstance(result, tuple) else getattr(response, "status_code", 200)
                if status >= 400:
                    mark_failure("HTTP"+str(status))
                if trace is not None and hasattr(response, "headers"):
                    response.headers["X-Trace-ID"] = trace["trace_id"]
                return result
        return wrapped
    return decorate
