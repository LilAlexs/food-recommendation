"""Regression for uploaded 0.88 current-reference trace and state loss."""
import unittest
from unittest.mock import patch
import test_context_origin_fix as context_tests
client_result = context_tests.client_result


class CurrentQuantityTests(unittest.TestCase):
    setUp = context_tests.SemanticWebTests.setUp
    tearDown = context_tests.SemanticWebTests.tearDown

    def send(self, question, decision=None):
        self.q=question
        if decision is not None:
            self.system.generation_module.client=client_result(decision)
        return self.handler._handle_chat_request()

    def test_reported_oil_then_scallion_at_088(self):
        self.send('红烧鱼怎么做？')
        decision=dict(kind='recipe',target=None,reference='current',confidence=.88)
        for question in ['油需要多少','葱需要多少']:
            response=self.send(question,decision)
            self.assertEqual(response.data['recipe_name'],'红烧鱼')
            prompt,docs=self.system.generation_module.generate_adaptive_answer.call_args.args
            self.assertIn(question,prompt)
            self.assertIn('30-50ml',docs[0].page_content)
            self.assertIn('葱 1-2 根',docs[0].page_content)
        self.assertEqual(self.system.generation_module.generate_adaptive_answer.call_count,3)

    def test_ambiguous_operation_keeps_current_for_next_quantity(self):
        self.send('红烧鱼怎么做？')
        response=self.send('它能放多久',dict(kind='clarify',target=None,reference='current',confidence=.35))
        self.assertEqual(response.data['recipe_status'],'needs_confirmation')
        self.assertEqual(self.system.cache_manager.confirmed_recipes['s']['name'],'红烧鱼')
        response=self.send('葱需要多少',dict(kind='recipe',target=None,reference='current',confidence=.88))
        self.assertEqual(response.data['recipe_name'],'红烧鱼')

    def test_missing_identity_does_not_accept_current(self):
        response=self.send('油需要多少',dict(kind='recipe',target=None,reference='current',confidence=.88))
        self.assertEqual(response.data['recipe_status'],'needs_confirmation')
        self.system.generation_module.generate_adaptive_answer.assert_not_called()

    def test_new_explicit_dish_cannot_be_overridden_by_current(self):
        self.send('红烧鱼怎么做？')
        response=self.send('另外详细介绍地三鲜的材料用量',dict(kind='recipe',target=None,reference='current',confidence=.88))
        self.assertEqual(response.data['recipe_status'],'needs_confirmation')
        self.assertEqual(self.system.generation_module.generate_adaptive_answer.call_count,1)

    def test_uncollected_new_dish_still_clears_identity(self):
        self.send('红烧鱼怎么做？')
        response=self.send('宇宙独角兽炖菜怎么做？')
        self.assertEqual(response.data['recipe_status'],'not_found')
        self.assertIsNone(self.system.cache_manager.confirmed_recipes['s'])

    def test_stream_quantity_uses_actual_target_document(self):
        self.send('红烧鱼怎么做？')
        self.q='葱需要多少'
        self.system.generation_module.client=client_result(dict(kind='recipe',target=None,reference='current',confidence=.88))
        self.system.generation_module.generate_adaptive_answer_stream.return_value=iter(['葱1至2根'])
        response=self.handler._handle_stream_request();list(response.iterable)
        prompt,docs=self.system.generation_module.generate_adaptive_answer_stream.call_args.args
        self.assertEqual(docs[0].metadata['recipe_name'],'红烧鱼')
        self.assertIn('葱 1-2 根',docs[0].page_content)
