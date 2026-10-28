import importlib.util
import os
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]

class ClientTests(unittest.TestCase):
    def load(self,create):
        class Client:
            def __init__(self,**kw):
                self.kwargs=kw
                self.chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=create))
        provider=types.ModuleType('openai');provider.OpenAI=Client
        docs=types.ModuleType('langchain_core.documents');docs.Document=object
        spec=importlib.util.spec_from_file_location('generation_fixture',ROOT/'rag_modules/generation_integration.py')
        module=importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules,{'openai':provider,'langchain_core.documents':docs}):spec.loader.exec_module(module)
        return module

    def test_shared_client_timeout_retry_settings(self):
        module=self.load(lambda **kw:None)
        with patch.dict(os.environ,{'OPENAI_API_KEY':'fixture','LLM_TIMEOUT_SECONDS':'15','LLM_MAX_RETRIES':'0','LLM_STREAM_ATTEMPTS':'1'}):
            client=module.GenerationIntegrationModule()
        self.assertEqual(client.client.kwargs['timeout'],15)
        self.assertEqual(client.client.kwargs['max_retries'],0)

    def test_partial_stream_never_restarts(self):
        calls=[]
        def stream():
            yield types.SimpleNamespace(usage=None,choices=[types.SimpleNamespace(delta=types.SimpleNamespace(content='first'))])
            raise TimeoutError()
        def create(**kw):calls.append(kw);return stream()
        module=self.load(create)
        with patch.dict(os.environ,{'OPENAI_API_KEY':'fixture','LLM_STREAM_ATTEMPTS':'3'}):client=module.GenerationIntegrationModule()
        gen=client.generate_adaptive_answer_stream('question',[])
        self.assertEqual(next(gen),'first')
        with self.assertRaises(TimeoutError):next(gen)
        self.assertEqual(len(calls),1)

    def test_default_stream_failure_has_no_extra_fallback(self):
        calls=[]
        def create(**kw):calls.append(kw);raise TimeoutError()
        module=self.load(create)
        with patch.dict(os.environ,{'OPENAI_API_KEY':'fixture','LLM_STREAM_ATTEMPTS':'1'}):client=module.GenerationIntegrationModule()
        from rag_modules.generation_errors import GenerationError
        with self.assertRaises(GenerationError):list(client.generate_adaptive_answer_stream('question',[]))
        self.assertEqual(len(calls),1)

if __name__=='__main__':unittest.main()
