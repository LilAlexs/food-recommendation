import unittest
from unittest.mock import MagicMock, patch
import types
import test_recipe_identity as identities
import test_context_origin_fix as base
from rag_modules.quantity_followup import source_quantity_target
from rag_modules.recipe_catalog import get_catalog
from rag_modules.session_constraints import state_for


class QuantityTargetTests(unittest.TestCase):
    def setUp(self):
        docs=types.ModuleType('langchain_core.documents');docs.Document=identities.Document
        self.docs_patch=patch.dict('sys.modules',{'langchain_core.documents':docs});self.docs_patch.start()
        self.addCleanup(self.docs_patch.stop)
        self.catalog=get_catalog()
        recipe=self.catalog.resolve('水煮肉片')['recipe']
        self.current={'name':recipe.name,'recipe_id':recipe.recipe_id}

    def test_different_ingredients_are_not_hardcoded(self):
        for q in ['油需要多少？','小葱放多少克','芹菜要几根','土豆淀粉需要多少']:
            result=source_quantity_target(q,self.current,self.catalog)
            self.assertEqual(result['recipe'].name,'水煮肉片')

    def test_no_identity_or_unknown_ingredient_uses_no_shortcut(self):
        self.assertIsNone(source_quantity_target('油需要多少',None,self.catalog))
        self.assertIsNone(source_quantity_target('鱼露需要多少',self.current,self.catalog))
        self.assertIsNone(source_quantity_target('油需要多少',dict(name='水煮肉片',recipe_id='wrong'),self.catalog))

    def test_switch_comparison_and_time_are_not_quantity_shortcuts(self):
        for q in ['换个话题，油需要多少','油和盐需要多少','红烧鱼需要多少油','它能放多久','不放葱需要多少油']:
            self.assertIsNone(source_quantity_target(q,self.current,self.catalog))


class ResilienceWebTests(unittest.TestCase):
    setUp=base.SemanticWebTests.setUp
    tearDown=base.SemanticWebTests.tearDown

    def send(self,q):
        self.q=q
        return self.handler._handle_chat_request()

    def test_reported_chain_never_calls_analysis_for_oil(self):
        self.send('水煮肉片怎么做？');self.send('这道菜不放辣椒')
        client=MagicMock();client.with_options.side_effect=TimeoutError()
        self.system.generation_module.client=client
        response=self.send('油需要多少？')
        self.assertEqual(response.data['recipe_name'],'水煮肉片')
        client.with_options.assert_not_called()
        q,docs=self.system.generation_module.generate_adaptive_answer.call_args.args
        self.assertIn('不使用/不食用 辣椒',q)
        self.assertIn('植物油 280g',docs[0].page_content)
        self.assertIn('菜籽油 200g',docs[0].page_content)

    def test_analysis_timeout_preserves_identity_and_requirements(self):
        self.send('水煮肉片怎么做？');self.send('这道菜不放辣椒')
        client=MagicMock();client.with_options.side_effect=TimeoutError()
        self.system.generation_module.client=client
        r=self.send('帮我解释一下刚才的处理细节')
        self.assertEqual(r.data['recipe_status'],'analysis_unavailable')
        self.assertFalse(r.data['success']);self.assertTrue(r.data['retryable'])
        current=self.system.cache_manager.confirmed_recipes['s']
        self.assertEqual(current['name'],'水煮肉片')
        self.assertEqual(state_for(self.system.cache_manager,'s').active(current['recipe_id'])[0]['value'],'辣椒')
        self.assertNotIn('补充菜名',r.data['response'])
        r=self.send('葱需要多少');self.assertEqual(r.data['recipe_name'],'水煮肉片')
        self.assertEqual(self.system.generation_module.generate_adaptive_answer.call_count,2)

    def test_invalid_json_is_service_failure_not_topic_reset(self):
        self.send('水煮肉片怎么做？')
        client=base.client_result({})
        self.system.generation_module.client=client
        r=self.send('刚才的操作详细解释一下')
        self.assertEqual(r.data['answer_source'],'analysis_error')
        self.assertEqual(self.system.cache_manager.confirmed_recipes['s']['name'],'水煮肉片')

    def test_stream_quantity_uses_source_without_analysis(self):
        self.send('水煮肉片怎么做？');self.send('这道菜不放辣椒')
        self.system.generation_module.client.with_options.side_effect=TimeoutError()
        self.q='油应该放多少才合适'
        self.system.generation_module.generate_adaptive_answer_stream.return_value=iter(['模拟答案'])
        response=self.handler._handle_stream_request();list(response.iterable)
        self.system.generation_module.client.with_options.assert_not_called()
        q,docs=self.system.generation_module.generate_adaptive_answer_stream.call_args.args
        self.assertIn('不使用/不食用 辣椒',q);self.assertEqual(docs[0].metadata['recipe_name'],'水煮肉片')

    def test_transient_analysis_error_recovers_and_uses_preserved_requirements(self):
        self.send('水煮肉片怎么做？');self.send('这道菜不放辣椒')
        client=base.client_result(dict(kind='recipe',reference='current',target=None,confidence=.95))
        good=client.with_options.return_value.chat.completions.create.return_value
        client.with_options.return_value.chat.completions.create.side_effect=[TimeoutError(),good]
        self.system.generation_module.client=client
        with patch('rag_modules.analysis_retry.time.sleep'):
            r=self.send('帮我解释一下刚才的处理细节')
        self.assertEqual(r.data['recipe_name'],'水煮肉片')
        self.assertEqual(client.with_options.return_value.chat.completions.create.call_count,2)
        q,docs=self.system.generation_module.generate_adaptive_answer.call_args.args
        self.assertIn('不使用/不食用 辣椒',q)

    def test_auth_failure_is_not_retryable_and_keeps_state(self):
        self.send('水煮肉片怎么做？')
        client=base.client_result({})
        error=type('AuthenticationError',(Exception,),{'status_code':401})()
        client.with_options.return_value.chat.completions.create.side_effect=error
        self.system.generation_module.client=client
        r=self.send('帮我解释一下刚才的处理细节')
        self.assertFalse(r.data['retryable']);self.assertFalse(r.data['success'])
        self.assertEqual(client.with_options.return_value.chat.completions.create.call_count,1)
        self.assertEqual(self.system.cache_manager.confirmed_recipes['s']['name'],'水煮肉片')
