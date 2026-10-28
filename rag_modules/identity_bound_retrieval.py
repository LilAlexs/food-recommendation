"""Read-only joins from confirmed local recipe to legacy Neo4j/Milvus identity.
Names establish a candidate binding only. Ownership and source content must also pass.
"""
import json
import re
import unicodedata
from dataclasses import dataclass, field
from rag_modules.recipe_evidence import SourceEvidence, evaluate_chunk
from rag_modules.performance_monitor import event, timed


class BindingRejected(RuntimeError):
    pass


def normalized(text):
    return re.sub(r'[^\w\u4e00-\u9fff]', '', unicodedata.normalize('NFKC',str(text))).lower()


def milvus_filter(recipe_id):
    # Fixed field names and JSON-encoded string literals; no user expression interpolation.
    value=json.dumps(str(recipe_id),ensure_ascii=False)
    return f'node_type == "Recipe" and node_id == {value} and (parent_id == {value} or parent_id == "")'


@dataclass
class BoundResult:
    documents: list
    status: str
    graph_id: str = None
    reason: str = None
    vector_status: str = 'not_attempted'
    accepted_chunks: int = 0
    rejected_chunks: int = 0
    withheld_chunks: int = 0
    unverified_steps: int = 0
    chunk_checks: list = field(default_factory=list)


class IdentityBoundRetriever:
    LOOKUP = '''/* identity_lookup */
        MATCH (r:Recipe) WHERE r.name = $recipe_name
        RETURN r.nodeId AS recipe_id, r.name AS recipe_name
        ORDER BY r.nodeId LIMIT 2'''
    STEPS = '''/* identity_steps */
        MATCH (r:Recipe {nodeId: $recipe_id})-[rel:CONTAINS_STEP]->(s:CookingStep)
        OPTIONAL MATCH (owner:Recipe)-[:CONTAINS_STEP]->(s)
        RETURN s.nodeId AS step_id, s.description AS description, s.name AS name,
               coalesce(rel.stepOrder, s.stepNumber) AS step_order,
               collect(DISTINCT owner.nodeId) AS owner_ids
        ORDER BY step_order, step_id LIMIT 101'''
    INGREDIENTS = '''/* identity_ingredients */
        MATCH (r:Recipe {nodeId: $recipe_id})-[:REQUIRES]->(i:Ingredient)
        RETURN DISTINCT i.nodeId AS ingredient_id, i.name AS name
        ORDER BY ingredient_id LIMIT 101'''

    def __init__(self, driver, vector_module=None, database='neo4j'):
        self.driver=driver
        self.vector_module=vector_module
        self.database=database

    def _graph(self, recipe_name, original):
        if self.driver is None:
            raise BindingRejected('graph_unavailable')
        # Query timeout bounds each transaction, not the entire HTTP request.
        from neo4j import Query
        with self.driver.session(database=self.database, default_access_mode="READ") as session:
            matches=list(session.run(Query(self.LOOKUP,timeout=5),recipe_name=recipe_name))
            if len(matches)!=1:
                raise BindingRejected('graph_identity_missing' if not matches else 'graph_identity_ambiguous')
            record=matches[0]
            if record['recipe_name']!=recipe_name or record['recipe_id'] is None:
                raise BindingRejected('graph_identity_mismatch')
            # Preserve the original type for Cypher equality (legacy schema uses strings).
            graph_id=record['recipe_id']
            steps=list(session.run(Query(self.STEPS,timeout=5),recipe_id=graph_id))
            ingredients=list(session.run(Query(self.INGREDIENTS,timeout=5),recipe_id=graph_id))
        if not steps or len(steps)>100 or len(ingredients)>100:
            raise BindingRejected('graph_steps_missing_or_over_limit')
        source=SourceEvidence(original.page_content)
        orders=[];projections=[];checks=[]
        for step in steps:
            if set(map(str,step['owner_ids']))!={str(graph_id)}:
                raise BindingRejected('step_has_other_or_missing_owner')
            order=step.get('step_order')
            if not isinstance(order,(int,float)) or isinstance(order,bool):
                raise BindingRejected('step_order_missing')
            orders.append(order)
            rows=source.project(step.get('description') or '')
            projections.extend(rows)
            checks.append(dict(step_id=step.get('step_id'),
                state='source_projected' if rows else 'unverified_summary',
                source_lines=[r['line'] for r in rows]))
        if len(set(orders))!=len(orders) or orders!=sorted(orders):
            raise BindingRejected('step_order_invalid')
        # Do not use graph sequence to rewrite original sequence. The source controls order.
        # Unverified paraphrases do not reject the recipe and are never copied as facts.
        for ingredient in ingredients:
            projections.extend(source.project(ingredient.get('name') or ''))
        from langchain_core.documents import Document
        body='# '+recipe_name+'\n\n目标原文摘录（保留原条件、范围与可选项）：\n'+source.document_text(projections)
        doc=Document(page_content=body,metadata={**original.metadata,
            'graph_recipe_id':str(graph_id),'source_type':'neo4j_source_projection',
            'identity_binding':'name_unique+step_owner+original_source',
            'evidence_policy':'source_projection_v7','step_checks':checks,
            'projected_source_lines':sorted({r['line'] for r in projections})})
        return graph_id,doc

    @timed('recipe.bound_retrieval')
    def retrieve(self, original):
        # Local full document always provides scope/intro, quantities and completeness.
        # Graph evidence cannot replace missing local context or introduce another dish.
        try:
            graph_id,graph_doc=self._graph(original.metadata['recipe_name'],original)
        except BindingRejected as exc:
            result=BoundResult([original],'local_fallback',reason=str(exc))
            self._record(result,original);return result
        except Exception as exc:
            result=BoundResult([original],'local_fallback',reason='graph_error:'+type(exc).__name__)
            self._record(result,original);return result
        unverified=sum(c['state']=='unverified_summary' for c in graph_doc.metadata['step_checks'])
        status='graph_scoped_source' if unverified else 'graph_verified'
        result=BoundResult([original,graph_doc],status,str(graph_id),unverified_steps=unverified)
        if self.vector_module is None or not callable(getattr(self.vector_module,'fetch_recipe_chunks',None)):
            result.vector_status='vector_unavailable'
            self._record(result,original);return result
        try:
            hits=self.vector_module.fetch_recipe_chunks(str(graph_id),limit=20)
            result.vector_status='empty' if not hits else 'checked'
            from langchain_core.documents import Document
            source=SourceEvidence(original.page_content)
            seen=set()
            for hit in hits:
                check=evaluate_chunk(hit,graph_id,original.metadata['recipe_name'],source)
                rows=check.pop('projections')
                check['chunk_id']=hit.get('chunk_id')
                result.chunk_checks.append(check)
                if check['state']=='identity_rejected':
                    result.rejected_chunks+=1
                    continue
                if not rows:
                    result.withheld_chunks+=1
                    continue
                text=source.document_text(rows)
                if text in seen:
                    check['duplicate']=True
                    continue
                seen.add(text)
                result.accepted_chunks+=1
                if result.accepted_chunks<=3:
                    result.documents.append(Document(page_content=text,metadata={**original.metadata,
                        'graph_recipe_id':str(graph_id),'source_type':'milvus_source_projection',
                        'chunk_id':hit.get('chunk_id'),'evidence_policy':'source_projection_v7',
                        'projected_source_lines':sorted({r['line'] for r in rows})}))
            if result.rejected_chunks:result.vector_status='contains_identity_rejections'
            elif result.accepted_chunks:result.vector_status='source_projected'
            elif hits:result.vector_status='derived_content_withheld'
        except Exception as exc:
            result.vector_status='vector_error:'+type(exc).__name__
        self._record(result,original)
        return result

    def _record(self,result,original):
        event(kind='recipe_binding',local_recipe_id=original.metadata['recipe_id'],
              graph_recipe_id=result.graph_id,status=result.status,reason=result.reason,
              vector_status=result.vector_status,accepted_chunks=result.accepted_chunks,
              rejected_chunks=result.rejected_chunks,withheld_chunks=result.withheld_chunks,
              unverified_steps=result.unverified_steps,evidence_policy='source_projection_v7')
