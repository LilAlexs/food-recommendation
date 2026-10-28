import unittest
import types
from unittest.mock import patch
import test_context_origin_fix as base
from rag_modules.session_constraints import ConstraintState, parse_updates, state_for, constraint_text


class ScopeTests(unittest.TestCase):
    def test_scope_markers_and_ingredient_not_whitelisted(self):
        for question,scope in [('这道菜不放芝麻','dish'),('我一直不吃芝麻','session'),('以后所有菜都不放芹菜','session')]:
            parsed=parse_updates(question,'红烧鱼')
            self.assertTrue(parsed.only_update)
            self.assertEqual(parsed.changes[0].scope,scope)

    def test_missing_scope_with_no_current_is_ambiguous(self):
        p=parse_updates('不放辣椒')
        self.assertTrue(p.only_update);self.assertTrue(p.ambiguous)

    def test_dish_constraint_expires_when_switching_and_does_not_resurrect(self):
        s=ConstraintState();s.apply(parse_updates('这道菜不放辣椒','红烧鱼').changes,'fish')
        self.assertEqual(len(s.active('fish')),1)
        s.bind('egg');self.assertEqual(s.active('egg'),[])
        s.bind('fish');self.assertEqual(s.active('fish'),[])

    def test_session_constraint_survives_switch(self):
        s=ConstraintState();s.apply(parse_updates('我不吃辣').changes)
        s.bind('fish');s.bind('egg');self.assertEqual(s.active('egg')[0]['value'],'辣')

    def test_allergy_is_session_scoped(self):
        for q in ['我花生过敏','我对花生过敏','对花生过敏']:
            p=parse_updates(q)
            self.assertEqual(p.changes[0].kind,'allergy');self.assertEqual(p.changes[0].scope,'session')

    def test_scoped_clear_does_not_clear_other_scope(self):
        s=ConstraintState();s.apply(parse_updates('我不吃辣').changes)
        s.apply(parse_updates('这道菜不放葱','鱼').changes,'fish')
        s.apply(parse_updates('取消这道菜的限制','鱼').changes,'fish')
        self.assertEqual(len(s.active('fish')),1);self.assertEqual(s.active('fish')[0]['scope'],'session')

    def test_remove_specific_requirement_not_allergy(self):
        s=ConstraintState();s.apply(parse_updates('我花生过敏').changes)
        s.apply(parse_updates('这道菜不放辣椒','鱼').changes,'fish')
        s.apply(parse_updates('这道菜取消不放辣椒要求','鱼').changes,'fish')
        self.assertEqual([x['kind'] for x in s.active('fish')],['allergy'])

    def test_generic_clear_requires_scope(self):
        p=parse_updates('取消所有限制','鱼');self.assertTrue(p.ambiguous);self.assertTrue(p.only_update)

    def test_numeric_requirements_override_within_scope(self):
        s=ConstraintState();s.apply(parse_updates('所有菜最多20分钟').changes)
        s.apply(parse_updates('这道菜最多10分钟','鱼').changes,'fish')
        self.assertEqual([x['value'] for x in s.active('fish')],['10'])
        s.bind('egg');self.assertEqual([x['value'] for x in s.active('egg')],['20'])

    def test_servings_overwrite_not_accumulate(self):
        s=ConstraintState()
        for q in ['这道菜做两份','这道菜改成三份']:
            s.apply(parse_updates(q,'鱼').changes,'fish')
        self.assertEqual(len(s.active('fish')),1);self.assertEqual(s.active('fish')[0]['value'],'三')

    def test_combined_question_retains_question(self):
        p=parse_updates('不放花生的宫保鸡丁怎么做','宫保鸡丁')
        self.assertFalse(p.only_update);self.assertEqual(p.changes[0].value,'花生')

    def test_no_update_in_ordinary_quantity_question(self):
        for q in ['油需要多少','葱需要多少','蚝油需要多少','它需要腌制多久']:
            p=parse_updates(q,'红烧鱼');self.assertFalse(p.only_update);self.assertEqual(p.changes,[])


class ScopeWebTests(unittest.TestCase):
    setUp=base.SemanticWebTests.setUp
    tearDown=base.SemanticWebTests.tearDown

    def send(self,q):
        self.q=q
        return self.handler._handle_chat_request()

    def question(self):
        return self.system.generation_module.generate_adaptive_answer.call_args.args[0]

    def test_dish_exclusion_followup_then_switch(self):
        self.send('红烧鱼怎么做？');r=self.send('这道菜不放辣椒')
        self.assertEqual(r.data['answer_source'],'constraint_notice')
        self.assertEqual(self.system.cache_manager.confirmed_recipes['s']['name'],'红烧鱼')
        self.system.generation_module.client=base.client_result(dict(kind='recipe',reference='current',target=None,confidence=.88))
        self.send('油需要多少');self.assertIn('不使用/不食用 辣椒',self.question())
        self.send('番茄炒蛋怎么做？');self.assertNotIn('不使用/不食用 辣椒',self.question())

    def test_session_exclusion_survives_new_dish(self):
        self.send('我一直不吃辣');self.send('红烧鱼怎么做？')
        self.assertIn('会话要求：不使用/不食用 辣',self.question())
        self.send('番茄炒蛋怎么做？');self.assertIn('会话要求：不使用/不食用 辣',self.question())

    def test_explicit_clear_then_followup_does_not_replay_old_requirement(self):
        self.send('红烧鱼怎么做？');self.send('这道菜不放葱');self.send('取消这道菜的限制')
        self.system.generation_module.client=base.client_result(dict(kind='recipe',reference='current',target=None,confidence=.88))
        self.send('葱需要多少');self.assertNotIn('不使用/不食用 葱',self.question())

    def test_allergy_survives_clearing_dish_requirements(self):
        self.send('我花生过敏');self.send('红烧鱼怎么做？');self.send('这道菜不放葱');self.send('取消这道菜的限制')
        self.send('宫保鸡丁怎么做？')
        self.assertIn('花生过敏',self.question());self.assertIn('若与原方冲突',self.question())
        self.assertNotIn('不使用/不食用 葱',self.question())

    def test_global_clear_does_not_get_reimported_from_old_history(self):
        self.system.cache_manager.add_to_context('s','我花生过敏','知道了')
        self.send('红烧鱼怎么做？');self.assertIn('花生过敏',self.question())
        self.send('清除本次会话的所有限制');self.send('宫保鸡丁怎么做？')
        self.assertNotIn('花生过敏',self.question())

    def test_constraint_update_invalidates_and_bypasses_answer_cache(self):
        m=self.system.cache_manager
        m.add_to_semantic_cache('番茄炒蛋怎么做？','未限制的旧回答','s')
        self.send('我不吃辣')
        self.assertNotIn('s',m.session_caches)
        m.add_to_semantic_cache('q','不应缓存','s');self.assertNotIn('s',m.session_caches)
        self.assertIsNone(m.check_semantic_cache('q','s'))

    def test_session_and_clear_all_remove_constraints(self):
        m=self.system.cache_manager;self.send('我不吃辣')
        m.clear_session_cache('s');self.assertNotIn('s',m.constraint_states)
        self.send('我不吃辣');m.clear_all_caches();self.assertEqual(m.constraint_states,{})

    def test_sessions_do_not_share_requirements(self):
        self.send('我不吃辣')
        self.flask.request.get_json=lambda **kw:dict(message='番茄炒蛋怎么做？',session_id='other')
        self.handler._handle_chat_request();self.assertNotIn('不使用/不食用 辣',self.question())

    def test_ambiguous_update_does_not_mutate_or_call_model(self):
        r=self.send('不放辣椒')
        self.assertEqual(r.data['answer_source'],'constraint_notice')
        self.assertEqual(state_for(self.system.cache_manager,'s').active(),[])
        self.system.generation_module.generate_adaptive_answer.assert_not_called()

    def test_stream_uses_same_scoped_requirements(self):
        self.send('我一直不吃辣')
        self.q='番茄炒蛋怎么做？'
        self.system.generation_module.generate_adaptive_answer_stream.return_value=iter(['模拟答案'])
        response=self.handler._handle_stream_request();list(response.iterable)
        q,docs=self.system.generation_module.generate_adaptive_answer_stream.call_args.args
        self.assertIn('会话要求：不使用/不食用 辣',q)

    def test_more_than_ten_turns_does_not_lose_session_requirement(self):
        self.send('我一直不吃辣')
        for _ in range(12):self.send('番茄炒蛋怎么做？')
        self.assertIn('会话要求：不使用/不食用 辣',self.question())

    def test_general_recommendation_receives_only_session_requirements(self):
        self.send('我一直不吃辣');self.send('红烧鱼怎么做？');self.send('这道菜不放葱')
        self.system.generation_module.client=base.client_result(dict(kind='general',reference='none',target=None,confidence=.99))
        self.send('推荐几道家常菜')
        actual=self.system.query_router.route_query.call_args.kwargs['query']
        self.assertIn('不使用/不食用 辣',actual)
        self.assertNotIn('不使用/不食用 葱',actual)

    def test_combined_constraint_is_preserved_for_followup(self):
        self.send('不放花生的宫保鸡丁怎么做？')
        self.assertIn('不使用/不食用 花生',self.question())
        self.system.generation_module.client=base.client_result(dict(kind='recipe',reference='current',target=None,confidence=.88))
        self.send('腌多久')
        self.assertIn('不使用/不食用 花生',self.question())

    def test_servings_update_keeps_recipe_and_is_used_in_generation(self):
        self.send('番茄炒蛋怎么做？');self.send('这道菜做两份')
        self.system.generation_module.client=base.client_result(dict(kind='recipe',reference='current',target=None,confidence=.88))
        self.send('盐应该放多少')
        self.assertIn('当前菜要求：份数 两',self.question())
        self.assertEqual(self.system.cache_manager.confirmed_recipes['s']['name'],'西红柿炒鸡蛋')
