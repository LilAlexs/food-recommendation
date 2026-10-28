"""Run via stdin inside backend container. Read-only; no LLM/embedding/index rebuild."""
import json
import os
import sys
from neo4j import GraphDatabase
from pymilvus import MilvusClient
from rag_modules.recipe_catalog import get_catalog
from rag_modules.identity_bound_retrieval import milvus_filter
from rag_modules.binding_diagnostics import BindingDiagnosticAuditor

class VectorStore:
    def __init__(self,client,collection):self.client=client;self.collection=collection
    def fetch_recipe_chunks(self,recipe_id,limit=20):
        return self.client.query(collection_name=self.collection,filter=milvus_filter(recipe_id),limit=limit,
            output_fields=['text','node_id','recipe_name','node_type','chunk_id','parent_id','doc_type'],timeout=5)

def main():
    if hasattr(sys.stdout,"reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    driver=None;client=None
    try:
        driver=GraphDatabase.driver(os.environ.get('NEO4J_URI','bolt://neo4j:7687'),
            auth=(os.environ.get('NEO4J_USER','neo4j'),os.environ['NEO4J_PASSWORD']),
            connection_timeout=5)
        # Match the collection configured by this project's GraphRAGConfig.
        from config import DEFAULT_CONFIG
        client=MilvusClient(uri='http://'+os.environ.get('MILVUS_HOST','milvus-standalone')+':'+os.environ.get('MILVUS_PORT','19530'),timeout=5)
        vector=VectorStore(client,DEFAULT_CONFIG.milvus_collection_name)
        auditor=BindingDiagnosticAuditor(driver,vector,os.environ.get('NEO4J_DATABASE','neo4j'))
        catalog=get_catalog(os.getenv('RAG_RECIPE_DIR') or None)
        for name in ['宫保鸡丁','麻婆豆腐','西红柿炒鸡蛋']:
            match=catalog.resolve(name)
            if match['recipe'] is None:
                print(json.dumps(dict(recipe=name,status=match['status']),ensure_ascii=False),flush=True)
                continue
            try:
                report=auditor.audit(catalog.document(match['recipe']))
                print(json.dumps(report,ensure_ascii=False),flush=True)
            except Exception as exc:
                print(json.dumps(dict(recipe=name,status='diagnostic_error',error_type=type(exc).__name__),ensure_ascii=False),flush=True)
        return 0
    except Exception as exc:
        print(json.dumps(dict(status='audit_unavailable',error_type=type(exc).__name__),ensure_ascii=False))
        return 2
    finally:
        for resource in (client,driver):
            if resource is not None:
                try:resource.close()
                except Exception:pass

if __name__=='__main__':raise SystemExit(main())
