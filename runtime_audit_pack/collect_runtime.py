"""Read-only runtime export and optional vector candidate collection. No LLM."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import time

ROOT=Path('/app') if Path('/app/config.py').exists() else Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))

def digest(text):return hashlib.sha256(text.encode('utf-8')).hexdigest()
def write(path,obj):path.write_text(json.dumps(obj,ensure_ascii=False,indent=2,default=str),encoding='utf-8')
def jsonl(path,rows):
 with path.open('w',encoding='utf-8') as f:
  for r in rows:f.write(json.dumps(r,ensure_ascii=False,default=str)+'\n')

def main():
 p=argparse.ArgumentParser(description=__doc__)
 p.add_argument('--queries',type=Path,default=Path(__file__).with_name('evaluation_cases_60_audited.jsonl'))
 p.add_argument('--output',type=Path,required=True)
 p.add_argument('--collect-vector',action='store_true')
 p.add_argument('--candidate-k',type=int,default=30)
 args=p.parse_args()
 if args.candidate_k<1:p.error('candidate-k must be positive')
 if args.output.exists() and any(args.output.iterdir()):p.error('Use a new/empty output directory')
 args.output.mkdir(parents=True,exist_ok=True)
 cases=[json.loads(x) for x in args.queries.read_text(encoding='utf-8-sig').splitlines() if x.strip()]
 manifest={'status':'running','dataset_sha256':hashlib.sha256(args.queries.read_bytes()).hexdigest(),'case_count':len(cases),'candidate_k':args.candidate_k,'stages':{},'notes':['No LLM calls, no writes to databases, no index rebuild','Vector candidates are raw-query only; not BM25/RRF/rerank/rewrite results','Exports contain recipe and database document content; review before sharing'],'vector_requested':args.collect_vector}
 clients=[]
 def checkpoint():write(args.output/'runtime_manifest.json',manifest)
 checkpoint()
 # Source snapshot from the actual container. No model or database imports.
 try:
  source_root=Path(os.getenv('RAG_RECIPE_DIR',str(ROOT/'data/dishes')))
  refs={r['path']:r for c in cases for r in c.get('reference_sources',[])}
  reports=[]
  for rel,ref in sorted(refs.items()):
   path=source_root/rel.removeprefix('data/dishes/')
   text=path.read_text(encoding='utf-8-sig') if path.exists() else None
   reports.append({'path':rel,'exists':text is not None,'sha256':digest(text) if text is not None else None,'matches_uploaded':digest(text)==ref['sha256'] if text is not None else False,'text':text})
  jsonl(args.output/'source_verification.jsonl',reports)
  negative=[]
  for name in ['佛跳墙','北京烤鸭','毛血旺','东坡肉','汽锅鸡','文思豆腐']:
   negative.append({'name':name,'source_matches':[str(f.relative_to(source_root)) for f in source_root.rglob('*.md') if name in f.read_text(encoding='utf-8-sig')]})
  write(args.output/'negative_source_audit.json',negative)
  alias=ROOT/'rag_modules/recipe_aliases.json'
  if alias.exists():write(args.output/'runtime_aliases.json',json.loads(alias.read_text(encoding='utf-8-sig')))
  manifest['stages']['sources']={'status':'ok','mismatches':sum(not r['matches_uploaded'] for r in reports)}
 except Exception as e:manifest['stages']['sources']={'status':'failed','error_type':type(e).__name__}
 checkpoint()
 try:
  from neo4j import GraphDatabase
  driver=GraphDatabase.driver(os.getenv('NEO4J_URI','bolt://neo4j:7687'),auth=(os.getenv('NEO4J_USER','neo4j'),os.environ['NEO4J_PASSWORD']),connection_timeout=10);clients.append(driver)
  with driver.session(database=os.getenv('NEO4J_DATABASE','neo4j')) as session:
   recipe_rows=[dict(x) for x in session.run('MATCH (r:Recipe) RETURN properties(r) AS properties ORDER BY r.nodeId')]
   jsonl(args.output/'graph_recipes.jsonl',recipe_rows)
   # Preserve native edge type and properties; do not infer graph facts from names.
   edges=[dict(x) for x in session.run('MATCH (r:Recipe)-[e]->(n) RETURN r.nodeId AS recipe_id, type(e) AS relation_type, properties(e) AS relation_properties, labels(n) AS target_labels, properties(n) AS target_properties')]
   jsonl(args.output/'graph_recipe_edges.jsonl',edges)
  manifest['stages']['neo4j']={'status':'ok','recipes':len(recipe_rows),'edges':len(edges)}
 except Exception as e:manifest['stages']['neo4j']={'status':'failed','error_type':type(e).__name__}
 checkpoint()
 try:
  from pymilvus import MilvusClient
  from config import DEFAULT_CONFIG
  cfg=DEFAULT_CONFIG
  client=MilvusClient(uri='http://'+os.getenv('MILVUS_HOST','milvus-standalone')+':'+os.getenv('MILVUS_PORT','19530'),timeout=30);clients.append(client)
  collection=cfg.milvus_collection_name
  schema=client.describe_collection(collection_name=collection)
  write(args.output/'milvus_schema.json',schema)
  fields=[f['name'] for f in schema['fields'] if f['name']!='vector']
  primary=next(f['name'] for f in schema['fields'] if f.get('is_primary'))
  # Iterator avoids offset limits and exports all batches. No truncated "complete" report.
  iterator=client.query_iterator(collection_name=collection,filter='',output_fields=fields,batch_size=500)
  count=0; corpus=[]
  try:
   with (args.output/'milvus_documents.jsonl').open('w',encoding='utf-8') as out:
    while True:
     batch=iterator.next()
     if not batch:break
     for row in batch:
      out.write(json.dumps(row,ensure_ascii=False,default=str)+'\n');corpus.append(row);count+=1
  finally:iterator.close()
  manifest['collection']=collection
  manifest['embedding_model']=cfg.embedding_model
  manifest['stages']['milvus']={'status':'ok','documents':count,'primary_field':primary,'full_iterator_completed':True,'snapshot_consistency':'No concurrent KB updates should occur during export; not an atomic cross-DB snapshot'}
  if args.collect_vector:
   from langchain_huggingface import HuggingFaceEmbeddings
   embeddings=HuggingFaceEmbeddings(model_name=cfg.embedding_model, model_kwargs={'device':'cpu'}, encode_kwargs={'normalize_embeddings':True})
   # A collection must already be loaded by the service. Never load/rebuild here.
   successes=0
   with (args.output/'vector_candidates.jsonl').open('w',encoding='utf-8') as out:
    for case in cases:
     start=time.perf_counter()
     try:
      vector=embeddings.embed_query(case['message'])
      result=client.search(collection_name=collection,data=[vector],anns_field='vector',limit=args.candidate_k,output_fields=fields,search_params={'metric_type':'COSINE','params':{'ef':max(64,args.candidate_k)}},timeout=60)
      row={'id':case['id'],'query':case['message'],'history_used_for_retrieval':False,'status':'ok','candidates':result[0] if result else [],'duration_ms':(time.perf_counter()-start)*1000};successes+=1
     except Exception as e:row={'id':case['id'],'status':'failed','error_type':type(e).__name__,'duration_ms':(time.perf_counter()-start)*1000}
     out.write(json.dumps(row,ensure_ascii=False,default=str)+'\n');out.flush();print(case['id'],row['status'],flush=True)
   manifest['stages']['vector']={'status':'ok' if successes==len(cases) else 'partial','successes':successes,'total':len(cases),'metric':'COSINE (larger is more similar)','embedding_settings':'matches uploaded project: cpu and normalize_embeddings=True; actual runtime revision still needs confirmation'}
 except Exception as e:
  # Preserve already-completed Milvus stage if model setup fails.
  stage='vector' if manifest['stages'].get('milvus',{}).get('status')=='ok' and args.collect_vector else 'milvus'
  manifest['stages'][stage]={'status':'failed','error_type':type(e).__name__}
 finally:
  for c in reversed(clients):
   try:c.close()
   except Exception:pass
  manifest['status']='completed' if all(s.get('status')=='ok' for s in manifest['stages'].values()) else 'partial_or_blocked'
  checkpoint()
 print(json.dumps(manifest,ensure_ascii=False,indent=2))
 return 0 if manifest['status']=='completed' else 2
if __name__=='__main__':raise SystemExit(main())
