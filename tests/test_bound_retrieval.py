import json
import sys
import types
import unittest
from unittest.mock import patch, MagicMock
from pathlib import Path
# Avoid the production package's eager GPU/database imports in isolated tests.
if 'rag_modules' not in sys.modules:
    package=types.ModuleType('rag_modules')
    package.__path__=[str(Path(__file__).resolve().parents[1]/'rag_modules')]
    sys.modules['rag_modules']=package
from rag_modules.identity_bound_retrieval import IdentityBoundRetriever, milvus_filter

class Document:
    def __init__(self,page_content,metadata):self.page_content=page_content;self.metadata=metadata

class Session:
    def __init__(self,data):self.data=data;self.calls=[]
    def __enter__(self):return self
    def __exit__(self,*args):pass
    def run(self,query,**params):
        self.calls.append((query,params))
        for label in ['identity_lookup','identity_steps','identity_ingredients']:
            if label in query:return self.data[label]
        raise AssertionError(query)

class BoundTests(unittest.TestCase):
    def setUp(self):
        neo=types.ModuleType('neo4j');neo.Query=lambda text,timeout:text
        docs=types.ModuleType('langchain_core.documents');docs.Document=Document
        self.p=patch.dict(sys.modules,{'neo4j':neo,'langchain_core.documents':docs});self.p.start()
        self.original=Document('# 宫保鸡丁\n食材：鸡肉、熟花生\n先将鸡肉切成均匀的小块并腌制半小时。\n随后下锅翻炒鸡肉并加入熟花生收汁。',dict(recipe_id='local:test',recipe_name='宫保鸡丁',source='source.md'))
        self.data=dict(identity_lookup=[dict(recipe_id='201',recipe_name='宫保鸡丁')],
            identity_steps=[dict(step_id='s1',description='先将鸡肉切成均匀的小块并腌制半小时',step_order=1,owner_ids=['201']),
                            dict(step_id='s2',description='随后下锅翻炒鸡肉并加入熟花生收汁',step_order=2,owner_ids=['201'])],
            identity_ingredients=[dict(ingredient_id='i1',name='鸡肉'),dict(ingredient_id='i2',name='熟花生')])
        self.session=Session(self.data);self.driver=MagicMock();self.driver.session.return_value=self.session
        self.vector=MagicMock();self.vector.fetch_recipe_chunks.return_value=[]

    def tearDown(self):self.p.stop()
    def run_retrieval(self):return IdentityBoundRetriever(self.driver,self.vector).retrieve(self.original)

    def test_valid_binding_uses_parameters_and_read_session(self):
        result=self.run_retrieval()
        self.assertEqual(result.status,'graph_verified');self.assertEqual(result.graph_id,'201')
        self.driver.session.assert_called_with(database='neo4j',default_access_mode='READ')
        self.assertEqual(self.session.calls[0][1],{'recipe_name':'宫保鸡丁'})
        self.assertTrue(all(c[1]=={'recipe_id':'201'} for c in self.session.calls[1:]))
        self.assertTrue(all(d.metadata['recipe_id']=='local:test' for d in result.documents))
        self.vector.fetch_recipe_chunks.assert_called_once_with('201',limit=20)

    def test_missing_binding_never_searches_similar_names(self):
        self.data['identity_lookup']=[]
        result=self.run_retrieval()
        self.assertEqual(result.reason,'graph_identity_missing');self.assertEqual(result.documents,[self.original])
        self.vector.fetch_recipe_chunks.assert_not_called()

    def test_duplicate_binding_is_rejected(self):
        self.data['identity_lookup']*=2
        self.assertEqual(self.run_retrieval().reason,'graph_identity_ambiguous')

    def test_shared_steps_are_rejected(self):
        self.data['identity_steps'][0]['owner_ids']=['201','other']
        self.assertEqual(self.run_retrieval().reason,'step_has_other_or_missing_owner')

    def test_same_name_wrong_body_rejected(self):
        self.data['identity_steps'][0]['description']='土豆块复炸以后加入青椒继续炒制'
        result=self.run_retrieval()
        self.assertEqual(result.status,'graph_scoped_source')
        self.assertEqual(result.unverified_steps,1)
        self.assertTrue(all('土豆' not in d.page_content for d in result.documents))

    def test_source_order_cannot_be_reversed(self):
        a,b=self.data['identity_steps'];a['description'],b['description']=b['description'],a['description']
        result=self.run_retrieval()
        projected=result.documents[1].page_content
        self.assertLess(projected.index('先将鸡肉'),projected.index('随后下锅'))

    def test_missing_order_rejected(self):
        self.data['identity_steps'][0]['step_order']=None
        self.assertEqual(self.run_retrieval().reason,'step_order_missing')

    def test_unknown_ingredient_rejected(self):
        self.data['identity_ingredients'].append(dict(name='土豆'))
        result=self.run_retrieval()
        self.assertTrue(all('土豆' not in d.page_content for d in result.documents))

    def chunk(self,**changes):
        row=dict(node_id='201',parent_id='201',node_type='Recipe',recipe_name='宫保鸡丁',
                 text='随后下锅翻炒鸡肉并加入熟花生收汁',chunk_id='c1')
        row.update(changes);return row

    def test_vector_chunk_needs_identity_and_content(self):
        self.vector.fetch_recipe_chunks.return_value=[self.chunk(),self.chunk(node_id='202'),
            self.chunk(parent_id='202'),self.chunk(recipe_name='干煸仔鸡'),
            self.chunk(text='另一道菜的土豆复炸以后加入青椒'),self.chunk(node_type='Ingredient')]
        result=self.run_retrieval()
        self.assertEqual(result.accepted_chunks,1);self.assertEqual(result.rejected_chunks,4)
        self.assertEqual(result.withheld_chunks,1)
        self.assertEqual(len(result.documents),3)
        self.assertTrue(all('复炸' not in d.page_content for d in result.documents))

    def test_vector_failure_keeps_verified_graph_and_original(self):
        self.vector.fetch_recipe_chunks.side_effect=TimeoutError()
        result=self.run_retrieval()
        self.assertEqual(result.status,'graph_verified');self.assertIn('vector_error',result.vector_status)
        self.assertEqual(len(result.documents),2)

    def test_graph_failure_is_explicit_local_fallback(self):
        self.driver.session.side_effect=TimeoutError()
        result=self.run_retrieval()
        self.assertEqual(result.status,'local_fallback');self.assertEqual(result.reason,'graph_error:TimeoutError')

    def test_milvus_filter_escapes_id_and_requires_both_id_fields(self):
        rid='x" or node_id != "x'
        expr=milvus_filter(rid)
        self.assertIn('node_id == '+json.dumps(rid),expr)
        self.assertIn('parent_id == '+json.dumps(rid),expr)
        self.assertIn('node_type == "Recipe"',expr)

    def test_binding_never_overwrites_original_id(self):
        result=self.run_retrieval()
        self.assertEqual(result.documents[1].metadata['recipe_id'],'local:test')
        self.assertEqual(result.documents[1].metadata['graph_recipe_id'],'201')


    def test_actual_milvus_method_uses_filter_and_propagates_errors(self):
        # Compile the actual method in isolation; the module otherwise imports GPU libraries.
        import ast
        from rag_modules.performance_monitor import timed
        source=Path(__file__).resolve().parents[1]/'rag_modules/milvus_index_construction.py'
        tree=ast.parse(source.read_text())
        method=next(n for n in ast.walk(tree) if isinstance(n,ast.FunctionDef) and n.name=='fetch_recipe_chunks')
        module=ast.Module(body=[method],type_ignores=[]);ast.fix_missing_locations(module)
        namespace={'timed':timed};exec(compile(module,str(source),'exec'),namespace)
        obj=types.SimpleNamespace(client=MagicMock(),collection_name='fixture')
        obj.client.query.return_value=[]
        self.assertEqual(namespace['fetch_recipe_chunks'](obj,'201'),[])
        self.assertEqual(obj.client.query.call_args.kwargs['filter'],milvus_filter('201'))
        self.assertEqual(obj.client.query.call_args.kwargs['timeout'],5)
        obj.client.query.side_effect=TimeoutError()
        with self.assertRaises(TimeoutError):namespace['fetch_recipe_chunks'](obj,'201')

if __name__=='__main__':unittest.main()
