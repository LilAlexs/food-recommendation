import json
import unittest
import test_bound_retrieval as fixtures
from rag_modules.binding_diagnostics import BindingDiagnosticAuditor, layout_hint, closest_source, preview

class DiagnosticTests(unittest.TestCase):
    def setUp(self):fixtures.BoundTests.setUp(self)
    def tearDown(self):fixtures.BoundTests.tearDown(self)
    def audit(self):return BindingDiagnosticAuditor(self.driver,self.vector).audit(self.original)

    def test_valid_production_result_is_preserved(self):
        report=self.audit()
        self.assertEqual(report['production']['status'],'graph_verified')
        self.assertEqual(report['candidate_graph_recipe_id'],'201')
        self.assertFalse(report['vector_audit_after_graph_rejection'])
        self.assertTrue(report['steps']['source_sequence_ok'])
        self.assertEqual(self.vector.fetch_recipe_chunks.call_count,1)

    def test_rejected_graph_retains_candidate_id_and_explains_step(self):
        self.data['identity_steps'][0]['description']='加入土豆复炸后再和青椒一起翻炒'
        self.vector.fetch_recipe_chunks.return_value=[dict(node_id='201',parent_id='201',node_type='Recipe',recipe_name='宫保鸡丁',chunk_id='c',text='随后下锅翻炒鸡肉并加入熟花生收汁')]
        report=self.audit()
        self.assertEqual(report['production']['status'],'graph_scoped_source')
        self.assertEqual(report['production']['unverified_steps'],1)
        self.assertEqual(report['production']['graph_recipe_id'],'201')
        self.assertEqual(report['candidate_graph_recipe_id'],'201')
        self.assertEqual(report['steps']['items'][0]['step_id'],'s1')
        self.assertIn('description_not_in_target_source',report['steps']['items'][0]['reasons'])
        self.assertIn('text',report['steps']['items'][0]['closest_source_line'])
        self.assertFalse(report['vector_audit_after_graph_rejection'])
        self.assertTrue(report['chunks']['production_eligible'])
        self.assertEqual(report['production']['accepted_chunks'],1)

    def test_shared_owner_is_explicit(self):
        self.data['identity_steps'][0]['owner_ids']=['201','202']
        report=self.audit()
        self.assertIn('step_has_other_or_missing_owner',report['steps']['items'][0]['reasons'])
        self.assertEqual(report['steps']['items'][0]['owner_ids'],['201','202'])

    def test_format_hint_never_changes_admission(self):
        self.vector.fetch_recipe_chunks.return_value=[dict(node_id='201',parent_id='201',node_type='Recipe',recipe_name='宫保鸡丁',chunk_id='formatted',text='# 宫保鸡丁\n## 制作步骤\n### 第2步\n描述: 随后下锅翻炒鸡肉并加入熟花生收汁')]
        report=self.audit();chunk=report['chunks']['items'][0]
        self.assertEqual(chunk['reasons'],['text_not_verified'])
        self.assertTrue(chunk['format_hint']['all_long_payload_lines_supported'])
        self.assertFalse(chunk['passes_legacy_text_checks'])
        self.assertEqual(report['production']['rejected_chunks'],0)
        self.assertEqual(report['production']['accepted_chunks'],1)
        self.assertEqual(report['active_evidence_policy']['chunks'][0]['state'],'source_projected')

    def test_independent_vector_rejection_reasons(self):
        self.vector.fetch_recipe_chunks.return_value=[dict(node_id='202',parent_id='999',node_type='Ingredient',recipe_name='其他菜',chunk_id='bad',text='土豆复炸后和青椒一起继续翻炒')]
        chunk=self.audit()['chunks']['items'][0]
        self.assertEqual(set(chunk['reasons']),{'node_type_mismatch','node_id_mismatch','parent_id_mismatch','recipe_name_mismatch','text_not_verified'})

    def test_ambiguous_identity_does_not_query_vectors(self):
        self.data['identity_lookup']*=2
        report=self.audit()
        self.assertEqual(report['diagnostic_vector_status'],'not_attempted_no_unique_identity')
        self.vector.fetch_recipe_chunks.assert_not_called()

    def test_vector_error_is_not_retried_and_no_error_message_leaks(self):
        self.vector.fetch_recipe_chunks.side_effect=RuntimeError('SECRET_TOKEN_MUST_NOT_APPEAR')
        report=self.audit()
        self.assertEqual(self.vector.fetch_recipe_chunks.call_count,1)
        self.assertEqual(report['diagnostic_vector_status'],'error:RuntimeError')
        self.assertNotIn('SECRET_TOKEN',json.dumps(report))

    def test_duplicate_is_reported_but_not_counted_twice(self):
        row=dict(node_id='201',parent_id='201',node_type='Recipe',recipe_name='宫保鸡丁',chunk_id='dup',text='随后下锅翻炒鸡肉并加入熟花生收汁')
        self.vector.fetch_recipe_chunks.return_value=[row,row]
        report=self.audit()
        self.assertEqual(report['production']['accepted_chunks'],1)
        self.assertTrue(report['chunks']['items'][1]['duplicate'])

    def test_order_error_explained(self):
        self.data['identity_steps'][0]['step_order']=None
        report=self.audit()
        self.assertFalse(report['steps']['order_unique_sorted'])
        self.assertIn('step_order_missing',report['steps']['items'][0]['reasons'])

    def test_source_order_error_explained(self):
        a,b=self.data['identity_steps'];a['description'],b['description']=b['description'],a['description']
        report=self.audit()
        self.assertFalse(report['steps']['source_sequence_ok'])
        self.assertEqual(report['production']['status'],'graph_verified')
        self.assertEqual(report['production']['evidence_policy'],'source_projection_v7')

    def test_original_driver_receives_parameters_and_read_mode(self):
        self.audit()
        self.assertEqual(self.session.calls[0][1],{'recipe_name':'宫保鸡丁'})
        self.driver.session.assert_called_with(database='neo4j',default_access_mode='READ')
        self.assertEqual(len(self.session.calls),3)

    def test_preview_is_bounded_and_labels_truncation(self):
        result=preview('x'*5000,4000)
        self.assertEqual(len(result['text']),4000)
        self.assertTrue(result['truncated']);self.assertEqual(result['characters'],5000)

    def test_closest_line_is_only_a_lexical_aid(self):
        result=closest_source('先将鸡肉切成均匀的小块并腌制半小时',self.original.page_content)
        self.assertGreater(result['lexical_ratio'],.8)
        self.assertEqual(result['line'],3)

    def test_missing_driver_has_explicit_fallback(self):
        report=BindingDiagnosticAuditor(None,self.vector).audit(self.original)
        self.assertEqual(report['production']['reason'],'graph_unavailable')
        self.assertFalse(report['identity']['unique_exact_name'])
        self.vector.fetch_recipe_chunks.assert_not_called()

if __name__=='__main__':unittest.main()
