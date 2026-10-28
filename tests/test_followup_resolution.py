from test_context_origin_fix import client_result
import json
import os
import types
import unittest
from unittest.mock import patch
import test_v3_features as v3tests
import test_recipe_identity as identity_tests
from rag_modules.query_normalization import clean_query
from rag_modules.query_preparation import prepare
from rag_modules.recipe_catalog import requested_name, resolve_request
from rag_modules.generation_errors import GenerationError

class ParsingTests(unittest.TestCase):
    def test_marinating_time_forms(self):
        h=[dict(query='宫保鸡丁怎么做？',response='做法')]
        for q in ['它需要腌多久？','它需要腌制多久？','它要腌多长时间？']:
            enhanced=prepare(q,h).text
            self.assertIn('宫保鸡丁',enhanced)
            result=resolve_request(q,enhanced)
            self.assertEqual(result['recipe'].name,'宫保鸡丁')

    def test_topic_prefix_removed_before_alias_resolution(self):
        for q in ['换个话题，番茄炒蛋需要哪些材料？','换一个话题,番茄炒蛋需要哪些材料？','请问，换个话题，番茄炒蛋需要哪些材料？']:
            self.assertEqual(requested_name(q),'番茄炒蛋')
            self.assertEqual(resolve_request(q)['recipe'].name,'西红柿炒鸡蛋')

    def test_negation_and_constraints_are_not_stripped(self):
        self.assertEqual(clean_query('换个话题，不放花生的宫保鸡丁怎么做？'),'不放花生的宫保鸡丁怎么做？')
        h=[dict(query='我花生过敏',response='知道了')]
        self.assertIn('花生过敏',prepare('换个话题，宫保鸡丁怎么做？',h).text)

    def test_pronoun_after_explicit_switch_requires_new_target(self):
        r=resolve_request('换个话题，它需要腌多久？','宫保鸡丁需要腌多久？',confirmed_recipe={'name':'宫保鸡丁'},has_identity_state=True)
        self.assertEqual(r['status'],'needs_confirmation')
        self.assertIsNone(r['recipe'])

    def test_cleared_identity_is_not_replaced_by_old_history(self):
        r=resolve_request('它需要腌多久？','宫保鸡丁需要腌多久？',confirmed_recipe=None,has_identity_state=True)
        self.assertEqual(r['status'],'needs_confirmation')


class ConversationTests(unittest.TestCase):
    def setUp(self):
        v3tests.WebSafetyTests.setUp(self)
        self.extra_env=patch.dict(os.environ,{'RAG_RECIPE_GUARD':'1','RAG_ADAPTIVE_CONTEXT':'1','RAG_BOUND_RETRIEVAL':'0'})
        self.extra_env.start()
        docs=types.ModuleType('langchain_core.documents');docs.Document=identity_tests.Document
        self.docs_patch=patch.dict('sys.modules',{'langchain_core.documents':docs});self.docs_patch.start()
        self.system.cache_manager=v3tests.CacheSafetyTests().manager()
        self.current=''
        self.flask.request.get_json=lambda **kw:dict(message=self.current,session_id='s')
        self.system.generation_module.generate_adaptive_answer.return_value='模拟成功回答'

    def tearDown(self):
        self.docs_patch.stop();self.extra_env.stop();v3tests.WebSafetyTests.tearDown(self)

    def send(self,q):
        self.current=q
        return self.handler._handle_chat_request()

    def test_exact_reported_three_turn_chain(self):
        self.send('宫保鸡丁怎么做？')
        self.send('它需要腌多久？')
        q,docs=self.system.generation_module.generate_adaptive_answer.call_args.args
        self.assertIn('宫保鸡丁',q)
        self.assertEqual(docs[0].metadata['recipe_name'],'宫保鸡丁')
        self.assertIn('腌制 1 小时',docs[0].page_content)
        result=self.send('换个话题，番茄炒蛋需要哪些材料？')
        self.assertEqual(result.data['recipe_status'],'alias')
        q,docs=self.system.generation_module.generate_adaptive_answer.call_args.args
        self.assertEqual(docs[0].metadata['recipe_name'],'西红柿炒鸡蛋')
        self.assertNotIn('宫保鸡丁',q)
        self.system.query_router.route_query.assert_not_called()

    def test_repeated_followup_keeps_confirmed_target(self):
        self.send('宫保鸡丁怎么做？')
        self.send('它需要腌多久？')
        self.send('这道菜要腌多长时间？')
        self.assertEqual(self.system.generation_module.generate_adaptive_answer.call_args.args[1][0].metadata['recipe_name'],'宫保鸡丁')

    def test_alias_and_selection_also_set_canonical_identity(self):
        self.send('查看菜谱：清蒸鲈鱼')
        self.send('它需要蒸多久？')
        self.assertEqual(self.system.generation_module.generate_adaptive_answer.call_args.args[1][0].metadata['recipe_name'],'清蒸鲈鱼')

    def test_unresolved_new_target_clears_old_identity(self):
        self.send('宫保鸡丁怎么做？')
        self.send('宇宙独角兽炖菜怎么做？')
        self.system.generation_module.generate_adaptive_answer.reset_mock()
        self.system.generation_module.client=client_result(dict(kind='clarify',reference='none',target=None,confidence=.35))
        r=self.send('它需要腌多久？')
        self.assertEqual(r.data['recipe_status'],'needs_confirmation')
        self.system.generation_module.generate_adaptive_answer.assert_not_called()

    def test_generic_recommendation_does_not_keep_old_single_dish(self):
        self.send('宫保鸡丁怎么做？')
        self.system.generation_module.client=client_result(dict(kind='general',reference='none',target=None,confidence=.99))
        self.send('今天晚餐推荐两道菜')
        self.assertIsNone(self.system.cache_manager.confirmed_recipes['s'])
        self.system.generation_module.client=client_result(dict(kind='clarify',reference='none',target=None,confidence=.35))
        r=self.send('它需要腌多久？')
        self.assertEqual(r.data['recipe_status'],'needs_confirmation')

    def test_failed_generation_does_not_commit_new_identity(self):
        self.send('宫保鸡丁怎么做？')
        self.system.generation_module.generate_adaptive_answer.side_effect=GenerationError('failed')
        _,status=self.send('麻婆豆腐怎么做？')
        self.assertEqual(status,502)
        self.assertIsNone(self.system.cache_manager.confirmed_recipes['s'])

    def test_stream_chain_uses_same_identity_after_full_consumption(self):
        self.system.generation_module.generate_adaptive_answer_stream.return_value=iter(['模拟答案'])
        self.current='宫保鸡丁怎么做？'
        ''.join(self.handler._handle_stream_request().iterable)
        self.system.generation_module.generate_adaptive_answer_stream.return_value=iter(['模拟时间'])
        self.current='它需要腌多久？'
        ''.join(self.handler._handle_stream_request().iterable)
        docs=self.system.generation_module.generate_adaptive_answer_stream.call_args.args[1]
        self.assertEqual(docs[0].metadata['recipe_name'],'宫保鸡丁')
        self.assertIn('腌制 1 小时',docs[0].page_content)

    def test_clear_cache_also_clears_identity(self):
        self.send('宫保鸡丁怎么做？')
        self.system.cache_manager.clear_session_cache('s')
        self.assertNotIn('s',self.system.cache_manager.confirmed_recipes)
        self.send('宫保鸡丁怎么做？')
        self.system.cache_manager.clear_all_caches()
        self.assertEqual(self.system.cache_manager.confirmed_recipes,{})

if __name__=='__main__':unittest.main()
