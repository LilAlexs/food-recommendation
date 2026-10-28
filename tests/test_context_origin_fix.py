import json
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import MagicMock, patch
import test_v3_features as v3
import test_recipe_identity as identities
from rag_modules.context_evidence import content_key, evidence_context
from rag_modules.query_understanding import semantic_resolution


def client_result(data):
    client=MagicMock()
    client.with_options.return_value.chat.completions.create.return_value=types.SimpleNamespace(
        choices=[types.SimpleNamespace(message=types.SimpleNamespace(content=json.dumps(data)))])
    return client


class SemanticTests(unittest.TestCase):
    def resolve(self, question, data, current=None, history=None):
        client=client_result(data)
        return semantic_resolution(question,current,history or [],identities.get_catalog(),client,'fixture'),client

    def test_unlisted_wrapper_extracts_actual_alias(self):
        result,client=self.resolve('先不说这个了，教我做番茄炒蛋',dict(kind='recipe',target='番茄炒蛋',reference='explicit',confidence=.98))
        self.assertEqual(result['recipe'].name,'西红柿炒鸡蛋')
        client.with_options.assert_called_once_with(timeout=15,max_retries=0)

    def test_novel_followup_uses_committed_identity(self):
        result,_=self.resolve('鸡肉得提前腌多长时间才入味',dict(kind='recipe',target='干煸仔鸡',reference='current',confidence=.96),{'name':'宫保鸡丁'})
        self.assertEqual(result['recipe'].name,'宫保鸡丁')

    def test_no_current_identity_requires_confirmation(self):
        result,_=self.resolve('提前腌多久',dict(kind='recipe',target='宫保鸡丁',reference='current',confidence=.99))
        self.assertIsNone(result['recipe'])

    def test_unknown_target_never_maps_to_similar_recipe(self):
        result,_=self.resolve('教教我火星麻婆豆腐',dict(kind='recipe',target='火星麻婆豆腐',reference='explicit',confidence=.99))
        self.assertEqual(result['status'],'not_found')

    def test_nonliteral_model_inferred_name_requires_confirmation(self):
        result,_=self.resolve('教我做那个豆腐',dict(kind='recipe',target='麻婆豆腐',reference='explicit',confidence=.99))
        self.assertIsNone(result['recipe']);self.assertEqual(result['suggestions'],['麻婆豆腐'])

    def test_ambiguous_operation_does_not_guess(self):
        result,_=self.resolve('鸡肉能放多久',dict(kind='clarify',target=None,reference='current',confidence=.95),{'name':'宫保鸡丁'})
        self.assertIsNone(result['recipe'])

    def test_general_query_preserves_general_route(self):
        result,_=self.resolve('鸡肉适合搭配什么菜',dict(kind='general',target=None,reference='none',confidence=.99))
        self.assertIsNone(result)

    def test_analysis_never_receives_old_assistant_fact(self):
        result,client=self.resolve('腌好之后怎么办',dict(kind='recipe',target=None,reference='current',confidence=.99),{'name':'宫保鸡丁'},[{'query':'宫保鸡丁怎么做','response':'错误旧答案：腌30分钟'}])
        messages=client.with_options.return_value.chat.completions.create.call_args.kwargs['messages']
        self.assertNotIn('错误旧答案',str(messages));self.assertNotIn('腌30分钟',str(messages))

    def test_invalid_schema_and_timeout_are_not_not_found(self):
        for data in [{},dict(kind='recipe',target='麻婆豆腐',reference='explicit',confidence=True)]:
            result,_=self.resolve('教我做麻婆豆腐',data)
            self.assertEqual(result['status'],'analysis_unavailable')
        client=MagicMock();client.with_options.side_effect=TimeoutError()
        result=semantic_resolution('随便问',None,[],identities.get_catalog(),client,'fixture')
        self.assertEqual(result['status'],'analysis_unavailable')


class FusionReplayTests(unittest.TestCase):
    def test_actual_uploaded_recall_keeps_correct_step_chunk(self):
        bm=types.ModuleType('langchain_community.retrievers');bm.BM25Retriever=object
        neo=types.ModuleType('neo4j');neo.GraphDatabase=object
        docs_module=types.ModuleType('langchain_core.documents');docs_module.Document=identities.Document
        with patch.dict(sys.modules,{'langchain_core':types.ModuleType('langchain_core'),'langchain_core.documents':docs_module,'langchain_community':types.ModuleType('langchain_community'),'langchain_community.retrievers':bm,'neo4j':neo}):
            from rag_modules.hybrid_retrieval import HybridRetrievalModule
        rows=json.loads(Path(__file__).with_name('fixtures').joinpath('context_fusion_snapshot.json').read_text())
        def docs(case,stage):
            return [identities.Document(x['text'],dict(x['metadata'])) for r in rows if r['case']==case and r['stage']==stage for x in r['result']]
        obj=object.__new__(HybridRetrievalModule)
        obj.dual_level_retrieval=lambda q,k:docs('chain-1','dual_level_retrieval.output')
        obj.vector_search_enhanced=lambda q,k:docs('chain-1','vector_search_enhanced.output')
        fixed=obj.hybrid_search('宫保鸡丁怎么做',5)
        self.assertTrue(any(d.metadata.get('chunk_id')=='201002454_chunk_501' for d in fixed))
        old=docs('chain-1','hybrid_search.output')
        self.assertFalse(any(d.metadata.get('chunk_id')=='201002454_chunk_501' for d in old))
        # Fusion repair alone does not isolate a target: confirmed retrieval must still do that.

    def test_same_recipe_different_content_survives_identical_content_collapses(self):
        a=identities.Document('基本信息',{'node_id':'1'})
        b=identities.Document('制作步骤',{'node_id':'1','chunk_id':'1_steps'})
        self.assertNotEqual(content_key(a),content_key(b))
        self.assertEqual(content_key(b),content_key(identities.Document('制作步骤',{'node_id':'1'})))

    def test_shared_prefix_does_not_hide_different_tail(self):
        a=identities.Document('同一前缀'*40+'步骤甲',{'node_id':'1'})
        b=identities.Document('同一前缀'*40+'步骤乙',{'node_id':'1'})
        self.assertNotEqual(content_key(a),content_key(b))

    def test_prompt_labels_wrong_recipe_step_fragment(self):
        doc=identities.Document('制作步骤：腌制30分钟',{'node_id':'201002575','recipe_name':'干煸仔鸡','node_type':'Recipe'})
        text=evidence_context([doc])
        self.assertIn('干煸仔鸡',text);self.assertIn('201002575',text)


class SemanticWebTests(unittest.TestCase):
    def setUp(self):
        v3.WebSafetyTests.setUp(self)
        self.env=patch.dict('os.environ',{'RAG_RECIPE_GUARD':'1','RAG_BOUND_RETRIEVAL':'0','RAG_SEMANTIC_QUERY':'1'});self.env.start()
        docs=types.ModuleType('langchain_core.documents');docs.Document=identities.Document
        self.docpatch=patch.dict(sys.modules,{'langchain_core.documents':docs});self.docpatch.start()
        self.system.cache_manager=v3.CacheSafetyTests().manager()
        self.system.generation_module.generate_adaptive_answer.return_value='成功回答'
        self.q=''
        self.flask.request.get_json=lambda **kw:{'message':self.q,'session_id':'s'}

    def tearDown(self):
        self.docpatch.stop();self.env.stop();v3.WebSafetyTests.tearDown(self)

    def test_novel_topic_switch_and_followup_normal(self):
        self.q='先不说这个了，教我做番茄炒蛋'
        self.system.generation_module.client=client_result(dict(kind='recipe',target='番茄炒蛋',reference='explicit',confidence=.98))
        self.handler._handle_chat_request()
        self.assertEqual(self.system.generation_module.generate_adaptive_answer.call_args.args[1][0].metadata['recipe_name'],'西红柿炒鸡蛋')
        self.q='盐应该放多少克才合适'
        self.system.generation_module.client=client_result(dict(kind='recipe',target=None,reference='current',confidence=.98))
        response=self.handler._handle_chat_request()
        self.assertEqual(response.data['recipe_name'],'西红柿炒鸡蛋')
        self.assertEqual(self.system.generation_module.generate_adaptive_answer.call_count,2)
        self.assertEqual(self.system.generation_module.generate_adaptive_answer.call_args.args[1][0].metadata['recipe_name'],'西红柿炒鸡蛋')
        self.system.query_router.route_query.assert_not_called()

    def test_stream_semantic_target_keeps_original_constraints(self):
        self.q='帮我详细讲讲不放花生的宫保鸡丁该咋弄'
        self.system.generation_module.client=client_result(dict(kind='recipe',target='宫保鸡丁',reference='explicit',confidence=.98))
        self.system.generation_module.generate_adaptive_answer_stream.return_value=iter(['回答'])
        response=self.handler._handle_stream_request();list(response.iterable)
        question,docs=self.system.generation_module.generate_adaptive_answer_stream.call_args.args
        self.assertIn('不放花生',question);self.assertEqual(docs[0].metadata['recipe_name'],'宫保鸡丁')

    def test_general_switch_does_not_embed_old_assistant_answer(self):
        self.system.cache_manager.add_to_context('s','宫保鸡丁怎么做','土豆复炸，腌30分钟')
        self.q='先不聊这个，推荐几个蔬菜菜谱'
        self.system.generation_module.client=client_result(dict(kind='general',target=None,reference='none',confidence=.98))
        self.handler._handle_chat_request()
        actual=self.system.query_router.route_query.call_args.kwargs['query']
        self.assertNotIn('土豆',actual);self.assertNotIn('宫保鸡丁',actual)
