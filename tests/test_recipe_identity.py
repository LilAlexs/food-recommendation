import json
import os
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch
from rag_modules.recipe_catalog import (RecipeCatalog, CatalogUnavailable, get_catalog,
                                       requested_name, resolve_request, public_result)
import test_v3_features as v3tests

class Document:
    def __init__(self,page_content,metadata):
        self.page_content=page_content;self.metadata=metadata

class CatalogTests(unittest.TestCase):
    def test_real_exact_alias_and_ambiguity(self):
        c=get_catalog()
        self.assertEqual(c.resolve('宫保鸡丁')['status'],'exact')
        self.assertEqual(c.resolve('番茄炒蛋')['recipe'].name,'西红柿炒鸡蛋')
        self.assertEqual(c.resolve('清蒸鱼')['status'],'needs_confirmation')
        self.assertEqual(c.resolve('红烧肉')['status'],'needs_confirmation')

    def test_unknown_never_becomes_nearest(self):
        c=get_catalog()
        for name in ['火星麻婆豆腐','宫爆鸡丁','宇宙独角兽炖菜']:
            result=c.resolve(name)
            self.assertEqual(result['status'],'not_found');self.assertIsNone(result['recipe'])

    def test_document_is_exact_target_content(self):
        docs=types.ModuleType('langchain_core.documents');docs.Document=Document
        c=get_catalog()
        with patch.dict('sys.modules',{'langchain_core.documents':docs}):
            doc=c.document(c.resolve('宫保鸡丁')['recipe'])
        self.assertIn('熟花生',doc.page_content)
        self.assertNotIn('土豆块复炸',doc.page_content)
        self.assertEqual(doc.metadata['recipe_name'],'宫保鸡丁')
        self.assertTrue(doc.metadata['recipe_id'].startswith('local:'))

    def test_missing_directory_is_unavailable(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(CatalogUnavailable):RecipeCatalog(Path(tmp)/'missing')
            with self.assertRaises(CatalogUnavailable):RecipeCatalog(tmp)

    def test_title_mismatch_is_not_treated_as_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            (root/'目标菜.md').write_text('# 另一道菜的做法\n## 操作\n错误正文')
            (root/'正常菜.md').write_text('# 正常菜的做法\n## 操作\n正常正文')
            c=RecipeCatalog(root)
            with self.assertRaises(CatalogUnavailable):c.resolve('目标菜')
            self.assertEqual(c.resolve('正常菜')['status'],'exact')

    def test_changed_source_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'正常菜.md';p.write_text('# 正常菜的做法\n## 操作\n原文')
            c=RecipeCatalog(tmp);recipe=c.resolve('正常菜')['recipe']
            p.write_text('# 正常菜的做法\n## 操作\n正文已改')
            with self.assertRaises(CatalogUnavailable):c.document(recipe)

    def test_duplicate_name_needs_confirmation(self):
        with tempfile.TemporaryDirectory() as tmp:
            for folder in ['a','b']:
                p=Path(tmp)/folder;p.mkdir()
                (p/'正常菜.md').write_text('# 正常菜的做法\n## 操作\n'+folder)
            c=RecipeCatalog(tmp)
            self.assertEqual(c.resolve('正常菜')['status'],'needs_confirmation')

    def test_selection_is_resolved_again_not_trusted(self):
        self.assertEqual(requested_name('查看菜谱：麻婆豆腐'),'麻婆豆腐')
        self.assertEqual(resolve_request('查看菜谱：麻婆豆腐')['status'],'exact')
        self.assertEqual(resolve_request('查看菜谱：宇宙假菜')['status'],'not_found')
        self.assertIsNone(resolve_request('今天晚餐吃什么？'))
        self.assertEqual(resolve_request('宫保鸡丁')['status'],'exact')

    def test_suggestions_are_explicit_choices(self):
        payload=public_result(get_catalog().resolve('清蒸鱼'))
        self.assertTrue(payload['options'])
        self.assertTrue(all(o['message'].startswith('查看菜谱：') for o in payload['options']))
        self.assertEqual(payload['answer_source'],'catalog_notice')

    def test_constraint_form_retains_identity_without_multiple_dishes(self):
        self.assertEqual(resolve_request('不放花生的宫保鸡丁怎么做？')['recipe'].name,'宫保鸡丁')
        self.assertIsNone(resolve_request('宫保鸡丁和麻婆豆腐怎么做？')['recipe'])
        self.assertIsNone(resolve_request('它怎么做？')['recipe'])


class GuardWebTests(v3tests.WebSafetyTests):
    def setUp(self):
        super().setUp()
        self.v4env=patch.dict(os.environ,{'RAG_RECIPE_GUARD':'1'});self.v4env.start()
        docs=types.ModuleType('langchain_core.documents');docs.Document=Document
        self.docs_patch=patch.dict('sys.modules',{'langchain_core.documents':docs});self.docs_patch.start()
        self.flask.request.get_json=lambda **kw:dict(message='宫保鸡丁怎么做？',session_id='s')
        self.system.cache_manager.session_contexts={}

    def tearDown(self):
        self.docs_patch.stop();self.v4env.stop();super().tearDown()

    # Do not inherit v3 fixture assertions tied to the old query; v3 tests still run separately.
    test_normal_failure_is_502_and_not_cached=None
    test_normal_success_is_saved=None
    test_stream_partial_failure_is_not_saved=None
    test_stream_disconnect_is_not_saved=None
    test_empty_stream_is_not_saved=None
    test_successful_stream_saved_and_first_content_recorded=None

    def test_exact_normal_bypasses_retriever_and_old_cache(self):
        self.system.generation_module.generate_adaptive_answer.return_value='正确回答'
        response=self.handler._handle_chat_request()
        self.assertEqual(response.data['recipe_status'],'exact')
        self.system.query_router.route_query.assert_not_called()
        self.system.cache_manager.check_semantic_cache.assert_not_called()
        docs=self.system.generation_module.generate_adaptive_answer.call_args.args[1]
        self.assertEqual(len(docs),1);self.assertIn('熟花生',docs[0].page_content)

    def test_unknown_normal_does_not_call_model_or_write(self):
        self.flask.request.get_json=lambda **kw:dict(message='宇宙独角兽炖菜怎么做？',session_id='s')
        response=self.handler._handle_chat_request()
        self.assertEqual(response.data['recipe_status'],'not_found')
        self.system.generation_module.generate_adaptive_answer.assert_not_called()
        self.system.query_router.route_query.assert_not_called()
        self.system.cache_manager.add_to_context.assert_not_called()

    def test_unknown_stream_has_notice_and_done(self):
        self.flask.request.get_json=lambda **kw:dict(message='清蒸鱼怎么做？',session_id='s')
        raw=''.join(self.handler._handle_stream_request().iterable)
        self.assertIn('needs_confirmation',raw);self.assertIn('[DONE]',raw)
        self.system.generation_module.generate_adaptive_answer_stream.assert_not_called()
        self.system.cache_manager.add_to_context.assert_not_called()

    def test_exact_stream_uses_single_target(self):
        self.system.generation_module.generate_adaptive_answer_stream.return_value=iter(['回答'])
        raw=''.join(self.handler._handle_stream_request().iterable)
        self.assertIn('recipe_library',raw)
        self.system.query_router.route_query.assert_not_called()
        docs=self.system.generation_module.generate_adaptive_answer_stream.call_args.args[1]
        self.assertEqual([d.metadata['recipe_name'] for d in docs],['宫保鸡丁'])

    def test_catalog_unavailable_is_503(self):
        with patch('rag_modules.web_service_handler.resolve_request',side_effect=CatalogUnavailable()):
            response,code=self.handler._handle_chat_request()
        self.assertEqual(code,503);self.assertEqual(response.data['error_code'],'catalog_unavailable')
        self.system.generation_module.generate_adaptive_answer.assert_not_called()

    def test_no_old_assistant_recipe_in_generation_question(self):
        self.system.cache_manager.get_context_for_query.return_value='AI答: 凉拌豆腐的操作步骤\n当前问题: 宫保鸡丁怎么做？'
        self.system.cache_manager.session_contexts={'s':[dict(query='我花生过敏',response='错误旧做法')]}
        self.system.generation_module.generate_adaptive_answer.return_value='资料含花生，请不要按原方制作'
        self.handler._handle_chat_request()
        question=self.system.generation_module.generate_adaptive_answer.call_args.args[0]
        self.assertNotIn('凉拌豆腐',question);self.assertIn('花生过敏',question)

if __name__=='__main__':unittest.main()
