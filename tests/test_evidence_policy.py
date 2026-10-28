import json
from pathlib import Path
import types
import unittest
from unittest.mock import MagicMock
import test_bound_retrieval as fixtures
from rag_modules.recipe_evidence import SourceEvidence, evaluate_chunk, factual_text
from rag_modules.identity_bound_retrieval import IdentityBoundRetriever
from rag_modules.recipe_catalog import get_catalog

class EvidencePolicyTests(unittest.TestCase):
    def hit(self,text,**changes):
        value=dict(node_id='201',parent_id='201',node_type='Recipe',recipe_name='目标菜',text=text)
        value.update(changes);return value

    def test_decimal_and_range_operators_are_not_erased(self):
        self.assertNotEqual(factual_text('1.5g'),factual_text('15g'))
        self.assertNotEqual(factual_text('1-5g'),factual_text('15g'))
        self.assertNotEqual(factual_text('1/5'),factual_text('15'))

    def test_wrong_quantity_does_not_project(self):
        s=SourceEvidence('- 放入1.5g盐，搅拌均匀。')
        c=evaluate_chunk(self.hit('放入15g盐，搅拌均匀'),'201','目标菜',s)
        self.assertEqual(c['state'],'unverified_summary');self.assertEqual(c['projections'],[])

    def test_optional_condition_is_retained(self):
        s=SourceEvidence('- 可选：加入花生，过敏者不要添加。')
        c=evaluate_chunk(self.hit('加入花生'),'201','目标菜',s)
        self.assertIn('可选',c['projections'][0]['text'])
        self.assertIn('过敏者不要添加',c['projections'][0]['text'])

    def test_negation_is_retained_in_source_not_fragment(self):
        s=SourceEvidence('- 不要加入花生，先确认是否过敏。')
        c=evaluate_chunk(self.hit('加入花生'),'201','目标菜',s)
        self.assertTrue(c['projections'][0]['text'].startswith('- 不要'))

    def test_serving_formula_and_rounding_are_retained(self):
        s=SourceEvidence('* 鸡蛋 = 1.5 个 * 份数，向上取整')
        c=evaluate_chunk(self.hit('鸡蛋 = 1.5 个'),'201','目标菜',s)
        self.assertIn('份数',c['projections'][0]['text']);self.assertIn('向上取整',c['projections'][0]['text'])

    def test_envelope_auxiliary_fields_do_not_become_evidence(self):
        text='## 制作步骤\n### 第1步\n描述: 大蒜和生姜切碎，备用\n工具: 激光刀\n时间: 999分钟\n额外加入其他菜谱的材料'
        c=evaluate_chunk(self.hit(text),'201','目标菜',SourceEvidence('- 大蒜和生姜切碎，备用'))
        self.assertEqual(c['state'],'source_projected')
        self.assertNotIn('999',str(c['projections']));self.assertNotIn('激光',str(c['projections']))
        self.assertEqual(c['unparsed_lines'],1)

    def test_derived_amounts_not_silently_accepted(self):
        c=evaluate_chunk(self.hit('## 所需食材\n1. 鸡蛋(1.5个)'),'201','目标菜',SourceEvidence('* 鸡蛋 = 1.5 个 * 份数，向上取整'))
        self.assertEqual(c['state'],'derived_fields_withheld')

    def test_other_recipe_rejected_even_if_text_is_shared(self):
        c=evaluate_chunk(self.hit('大蒜和生姜切碎，备用',node_id='202'),'201','目标菜',SourceEvidence('- 大蒜和生姜切碎，备用'))
        self.assertEqual(c['state'],'identity_rejected');self.assertEqual(c['projections'],[])

    def test_partial_envelope_only_projects_supported_payloads(self):
        c=evaluate_chunk(self.hit('## 制作步骤\n描述: 大蒜和生姜切碎，备用\n描述: 土豆块复炸以后加入青椒'),'201','目标菜',SourceEvidence('- 大蒜和生姜切碎，备用'))
        self.assertEqual(c['unverified_payloads'],1)
        self.assertTrue(all('复炸' not in p['text'] for p in c['projections']))


class ReplayTests(unittest.TestCase):
    def setUp(self):fixtures.BoundTests.setUp(self)
    def tearDown(self):fixtures.BoundTests.tearDown(self)
    def replay(self,name):
        path=Path(__file__).with_name('fixtures')/'user_binding_snapshot.json'
        snapshot=json.loads(path.read_text(encoding='utf-8'))
        r=next(r for r in snapshot['recipes'] if r['recipe']==name)
        catalog=get_catalog();original=catalog.document(catalog.resolve(name)['recipe'])
        self.assertEqual(original.metadata['content_sha256'],r['content_sha256'])
        data=dict(identity_lookup=[dict(recipe_id=r['graph_id'],recipe_name=name)],identity_steps=r['steps'],identity_ingredients=r['ingredients'])
        session=fixtures.Session(data);driver=MagicMock();driver.session.return_value=session
        vector=MagicMock();vector.fetch_recipe_chunks.return_value=r['chunks']
        result=IdentityBoundRetriever(driver,vector).retrieve(original)
        # Derived amounts and raw external recipe prose never enter the projected documents.
        for document in result.documents[1:]:
            lines=document.metadata.get('projected_source_lines',[])
            for number in lines:self.assertIn(original.page_content.splitlines()[number-1],document.page_content)
        return result

    def test_replay_gongbao_paraphrases_keep_recipe_available(self):
        r=self.replay('宫保鸡丁')
        self.assertEqual(r.status,'graph_scoped_source');self.assertEqual(r.unverified_steps,9)
        self.assertEqual(r.rejected_chunks,0)
        self.assertTrue(all('土豆块复炸' not in d.page_content for d in r.documents))

    def test_replay_mapo_formatted_steps_project_successfully(self):
        r=self.replay('麻婆豆腐')
        self.assertEqual(r.status,'graph_verified');self.assertEqual(r.unverified_steps,0)
        self.assertGreaterEqual(r.accepted_chunks,1);self.assertEqual(r.rejected_chunks,0)
        self.assertIn('参考麻婆豆腐创作',r.documents[0].page_content)

    def test_replay_tomato_keeps_original_formulas(self):
        r=self.replay('西红柿炒鸡蛋')
        self.assertEqual(r.status,'graph_scoped_source');self.assertEqual(r.unverified_steps,6)
        self.assertIn('向上取整',r.documents[0].page_content)
        self.assertEqual(r.rejected_chunks,0)

if __name__=='__main__':unittest.main()
