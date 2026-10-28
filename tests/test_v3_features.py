import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch, MagicMock

ROOT=Path(__file__).resolve().parents[1]
from rag_modules.query_preparation import prepare, simple_query
from rag_modules.generation_errors import GenerationError
from rag_modules import performance_monitor as monitor


class QueryTests(unittest.TestCase):
    def test_independent_drops_old_topic(self):
        p=prepare('麻婆豆腐怎么做',[dict(query='今天晚餐吃什么？',response='推荐炒饭')])
        self.assertEqual(p.text,'麻婆豆腐怎么做');self.assertFalse(p.dependent)

    def test_pronoun_and_ellipsis(self):
        h=[dict(query='红烧肉怎么做？',response='做法')]
        self.assertEqual(prepare('它需要炖多久？',h).text,'红烧肉需要炖多久？')
        self.assertTrue(prepare('不要放辣椒',h).dependent)
        self.assertIn('红烧肉',prepare('两个人呢？',h).text)

    def test_ambiguous_recommendation_keeps_context(self):
        p=prepare('它怎么做？',[dict(query='推荐晚餐',response='红烧肉、番茄炒蛋')])
        self.assertTrue(p.dependent);self.assertIn('番茄炒蛋',p.text)

    def test_constraints_not_silently_removed(self):
        h=[dict(query='我花生过敏，不吃辣',response='知道了')]
        self.assertIn('花生过敏',prepare('宫保鸡丁怎么做？',h).text)
        self.assertIsNone(simple_query(prepare('宫保鸡丁怎么做？',h).text))
        h.append(dict(query='取消之前忌口限制',response='好'))
        self.assertEqual(prepare('清蒸鱼怎么做？',h).text,'清蒸鱼怎么做？')

    def test_simple_query_scope(self):
        self.assertEqual(simple_query('宫保鸡丁怎么做？').dish,'宫保鸡丁')
        for q in ['鸡肉配什么菜？','宫保鸡丁和红烧肉有什么区别？','不放花生的宫保鸡丁怎么做？','未知菜名怎么做？','今天吃什么？']:
            self.assertIsNone(simple_query(q))

    def test_fast_route_does_not_call_llm(self):
        from rag_modules.intelligent_query_router import IntelligentQueryRouter
        router=IntelligentQueryRouter(None,None,MagicMock(),types.SimpleNamespace(llm_model='fixture'))
        with patch.dict(os.environ,{'RAG_FAST_SIMPLE_QUERY':'1'}):
            self.assertEqual(router.analyze_query('宫保鸡丁怎么做？').recommended_strategy.value,'hybrid_traditional')
        router.llm_client.chat.completions.create.assert_not_called()

    def test_fast_keywords_real_method(self):
        bm=types.ModuleType('langchain_community.retrievers');bm.BM25Retriever=object
        neo=types.ModuleType('neo4j');neo.GraphDatabase=object
        with patch.dict(sys.modules,{'langchain_community':types.ModuleType('langchain_community'), 'langchain_community.retrievers':bm,'neo4j':neo}):
            from rag_modules.hybrid_retrieval import HybridRetrievalModule
            obj=object.__new__(HybridRetrievalModule);obj.llm_client=MagicMock()
            with patch.dict(os.environ,{'RAG_FAST_SIMPLE_QUERY':'1'}):
                self.assertEqual(obj.extract_query_keywords('宫保鸡丁怎么做？'),(['宫保鸡丁'],[]))
            obj.llm_client.chat.completions.create.assert_not_called()


    def test_simple_route_and_keyword_ablation_counts(self):
        # Actual router + actual keyword method; retrieval data is a fixture.
        from rag_modules.intelligent_query_router import IntelligentQueryRouter
        bm=types.ModuleType('langchain_community.retrievers');bm.BM25Retriever=object
        neo=types.ModuleType('neo4j');neo.GraphDatabase=object
        with patch.dict(sys.modules,{'langchain_community':types.ModuleType('langchain_community'), 'langchain_community.retrievers':bm,'neo4j':neo}):
            from rag_modules.hybrid_retrieval import HybridRetrievalModule
        for enabled_value, expected in [('0',2), ('1',0)]:
            response=types.SimpleNamespace(usage=None,choices=[types.SimpleNamespace(
                message=types.SimpleNamespace(content=json.dumps(dict(
                    recommended_strategy='hybrid_traditional', entity_keywords=['宫保鸡丁'], topic_keywords=[]))))])
            client=MagicMock();client.chat.completions.create.return_value=response
            retrieval=object.__new__(HybridRetrievalModule)
            retrieval.llm_client=client;retrieval.config=types.SimpleNamespace(llm_model='fixture')
            def search(query,top_k):
                retrieval.extract_query_keywords(query)
                return []
            retrieval.hybrid_search=search
            router=IntelligentQueryRouter(retrieval,None,client,retrieval.config)
            with patch.dict(os.environ,{'RAG_FAST_SIMPLE_QUERY':enabled_value}):
                router.route_query('宫保鸡丁怎么做？')
            self.assertEqual(client.chat.completions.create.call_count,expected)


class WebSafetyTests(unittest.TestCase):
    def setUp(self):
        self.guard_env = patch.dict(os.environ, {'RAG_RECIPE_GUARD':'0'})
        self.guard_env.start()
        self.flask=types.ModuleType('flask')
        self.flask.request=types.SimpleNamespace(get_json=lambda **kw:dict(message='红烧肉怎么做？',session_id='s'))
        self.flask.jsonify=lambda obj:types.SimpleNamespace(data=obj,headers={})
        self.flask.Response=lambda iterable,**kw:types.SimpleNamespace(iterable=iterable,headers={})
        self.patch=patch.dict(sys.modules,{'flask':self.flask});self.patch.start()
        from rag_modules.web_service_handler import WebServiceHandler
        self.system=types.SimpleNamespace(cache_manager=MagicMock(),query_router=MagicMock(),generation_module=MagicMock(),config=types.SimpleNamespace(top_k=5))
        self.system.cache_manager.get_context_for_query.return_value='红烧肉怎么做？'
        self.system.cache_manager.check_semantic_cache.return_value=None
        self.system.query_router.route_query.return_value=([],None)
        self.handler=WebServiceHandler(self.system)

    def tearDown(self):
        self.patch.stop()
        self.guard_env.stop()

    def test_normal_failure_is_502_and_not_cached(self):
        self.system.generation_module.generate_adaptive_answer.side_effect=GenerationError('upstream')
        response,status=self.handler._handle_chat_request()
        self.assertEqual(status,502);self.assertFalse(response.data['success'])
        self.system.cache_manager.add_to_semantic_cache.assert_not_called()
        self.system.cache_manager.add_to_context.assert_not_called()

    def test_normal_success_is_saved(self):
        self.system.generation_module.generate_adaptive_answer.return_value='答案'
        response=self.handler._handle_chat_request()
        self.assertTrue(response.data['success'])
        self.system.cache_manager.add_to_context.assert_called_once_with('s','红烧肉怎么做？','答案')

    def test_stream_partial_failure_is_not_saved(self):
        def chunks():
            yield '部分文本'
            raise GenerationError('failed')
        self.system.generation_module.generate_adaptive_answer_stream.side_effect=lambda *args:chunks()
        with tempfile.TemporaryDirectory() as tmp,patch.dict(os.environ,{'RAG_TRACE_PATH':str(Path(tmp)/'trace.jsonl')}):
            result=''.join(self.handler._handle_stream_request().iterable)
            t=json.loads((Path(tmp)/'trace.jsonl').read_text())
        self.assertTrue(t['failed']);self.assertIn('generation_failed',result)
        self.system.cache_manager.add_to_context.assert_not_called()
        self.system.cache_manager.add_to_semantic_cache.assert_not_called()

    def test_stream_disconnect_is_not_saved(self):
        self.system.generation_module.generate_adaptive_answer_stream.return_value=iter(['one','two'])
        gen=self.handler._handle_stream_request().iterable
        next(gen);gen.close()
        self.system.cache_manager.add_to_context.assert_not_called()

    def test_empty_stream_is_not_saved(self):
        self.system.generation_module.generate_adaptive_answer_stream.return_value=iter([])
        result=''.join(self.handler._handle_stream_request().iterable)
        self.assertIn('generation_failed',result)
        self.system.cache_manager.add_to_semantic_cache.assert_not_called()

    def test_successful_stream_saved_and_first_content_recorded(self):
        self.system.generation_module.generate_adaptive_answer_stream.return_value=iter(['one','two'])
        with tempfile.TemporaryDirectory() as tmp,patch.dict(os.environ,{'RAG_TRACE_PATH':str(Path(tmp)/'trace.jsonl')}):
            result=''.join(self.handler._handle_stream_request().iterable)
            t=json.loads((Path(tmp)/'trace.jsonl').read_text())
        self.assertFalse(t['failed']);self.assertIn('[DONE]',result)
        self.assertEqual(sum(e['kind']=='first_content' for e in t['events']),1)
        self.system.cache_manager.add_to_context.assert_called_once_with('s','红烧肉怎么做？','onetwo')


class CacheSafetyTests(unittest.TestCase):
    def manager(self):
        # Minimal numpy shim: tests only exercise exact cache and text guards.
        np=types.ModuleType('numpy');np.ndarray=object
        with patch.dict(sys.modules,{'numpy':np}):
            from rag_modules.session_cache_manager import SessionCacheManager
            return SessionCacheManager(types.SimpleNamespace(embed_documents=lambda qs:[[1,0] for q in qs]))

    def test_error_text_rejected_at_storage_boundary(self):
        m=self.manager()
        for answer in ['', '抱歉，生成回答时出现错误：Connection error.']:
            m.add_to_context('s','q',answer);m.add_to_semantic_cache('q',answer,'s')
        self.assertEqual(m.session_contexts,{});self.assertEqual(m.session_caches,{})

    def test_adaptive_cache_bypasses_followup_and_exact_only(self):
        m=self.manager()
        with patch.dict(os.environ,{'RAG_ADAPTIVE_CONTEXT':'1'}):
            m.add_to_semantic_cache('红烧肉怎么做？','正常回答','s')
            m.add_to_context('s','红烧肉怎么做？','正常回答')
            self.assertEqual(m.check_semantic_cache('红烧肉怎么做？','s'),'正常回答')
            self.assertIsNone(m.check_semantic_cache('麻婆豆腐怎么做？','s'))
            self.assertIsNone(m.check_semantic_cache('它炖多久？','s'))
            m.add_to_semantic_cache('它炖多久？','半小时','s')
            self.assertNotIn('它炖多久？',m.session_caches['s'])

if __name__=='__main__':unittest.main()
