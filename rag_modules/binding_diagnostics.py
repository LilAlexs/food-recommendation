"""Read-only explanation of active evidence policy plus legacy lexical diagnostics.
Legacy lexical checks below are diagnostic aids, not current admission policy.
Raw text/excerpts are returned ONLY by an explicitly run audit, not production traces.
"""
import difflib
import re
from rag_modules.identity_bound_retrieval import IdentityBoundRetriever, normalized


class RecordingSession:
    def __init__(self, context, capture):
        self.context=context
        self.capture=capture
    def __enter__(self):
        self.session=self.context.__enter__()
        return self
    def __exit__(self,*args):return self.context.__exit__(*args)
    def run(self, query, **params):
        rows=list(self.session.run(query,**params))
        text=getattr(query,'text',str(query))
        for label in ('identity_lookup','identity_steps','identity_ingredients'):
            if label in text:
                self.capture[label]=[dict(row) for row in rows]
                break
        return rows


class RecordingDriver:
    def __init__(self, driver):self.driver=driver;self.capture={}
    def session(self,**kwargs):
        return RecordingSession(self.driver.session(**kwargs),self.capture)


class RecordingVector:
    def __init__(self, vector):self.vector=vector;self.hits=None;self.error_type=None
    def fetch_recipe_chunks(self, recipe_id, limit=20):
        try:
            self.hits=list(self.vector.fetch_recipe_chunks(recipe_id,limit=limit))
            return self.hits
        except Exception as exc:
            self.error_type=type(exc).__name__
            raise


def preview(value,limit=2000):
    text=str(value or '')
    return dict(text=text[:limit],characters=len(text),truncated=len(text)>limit)


def closest_source(text, original):
    """Lexical aid for human review, not semantic confidence or an acceptance rule."""
    needle=normalized(text)[:300]
    if not needle:return None
    best=None
    for line_no,line in enumerate(original.splitlines(),1):
        value=normalized(line)[:400]
        if len(value)<8:continue
        ratio=difflib.SequenceMatcher(None,needle,value,autojunk=False).ratio()
        if best is None or ratio>best['lexical_ratio']:
            best=dict(line=line_no,lexical_ratio=round(ratio,4),**preview(line,600))
    return best


def layout_hint(text, original):
    # This deliberately does NOT affect production decisions. Headings may carry meaning.
    supported=[];unsupported=[]
    source=normalized(original)
    for line in str(text or '').splitlines():
        if re.match(r'^\s*#',line):continue
        value=re.sub(r'^\s*(?:[-*]|\d+[.)、])\s*','',line)
        value=re.sub(r'^\s*(?:步骤|描述|方法|工具|时间)\s*[:：]\s*','',value)
        canonical=normalized(value)
        if len(canonical)<8:continue
        (supported if canonical in source else unsupported).append(value)
    return dict(long_payload_lines=len(supported)+len(unsupported),
        supported_lines=len(supported),unsupported_lines=len(unsupported),
        all_long_payload_lines_supported=bool(supported) and not unsupported,
        note='仅作格式差异排查线索，不代表语义正确，不自动放行')


def explain_steps(steps, graph_id, original):
    source=normalized(original.page_content)
    details=[];positions=[];orders=[]
    for row in steps:
        text=row.get('description') or ''
        canonical=normalized(text)
        owners=row.get('owner_ids')
        owner_ok=isinstance(owners,(list,tuple)) and set(map(str,owners))=={str(graph_id)}
        order=row.get('step_order')
        order_ok=isinstance(order,(int,float)) and not isinstance(order,bool)
        reasons=[]
        if not owner_ok:reasons.append('step_has_other_or_missing_owner')
        if len(canonical)<8:reasons.append('description_too_short')
        if canonical and canonical not in source:reasons.append('description_not_in_target_source')
        if not canonical:reasons.append('description_empty')
        if not order_ok:reasons.append('step_order_missing')
        positions.append(source.find(canonical) if len(canonical)>=8 and canonical in source else None)
        orders.append(order if order_ok else None)
        details.append(dict(step_id=row.get('step_id'),name=row.get('name'),step_order=order,
            owner_ids=owners,normalized_characters=len(canonical),
            reasons=reasons,description=preview(text),
            closest_source_line=closest_source(text,original.page_content),
            format_hint=layout_hint(text,original.page_content)))
    all_ordered=bool(orders) and all(n is not None for n in orders)
    order_unique_sorted=all_ordered and len(set(orders))==len(orders) and orders==sorted(orders)
    all_in_source=bool(positions) and all(n is not None for n in positions)
    source_sequence_ok=all_in_source and positions==sorted(set(positions))
    return dict(count=len(steps),over_limit=len(steps)>100,order_unique_sorted=order_unique_sorted,
                source_sequence_ok=source_sequence_ok,items=details)


def explain_chunks(hits, graph_id, original, allowed_documents):
    allowed=[normalized(d.page_content) for d in allowed_documents]
    seen=set();items=[]
    for hit in hits:
        text=hit.get('text') or '';value=normalized(text);reasons=[]
        if hit.get('node_type')!='Recipe':reasons.append('node_type_mismatch')
        if str(hit.get('node_id'))!=str(graph_id):reasons.append('node_id_mismatch')
        if str(hit.get('parent_id','')) not in {'',str(graph_id)}:reasons.append('parent_id_mismatch')
        if hit.get('recipe_name')!=original.metadata['recipe_name']:reasons.append('recipe_name_mismatch')
        if len(value)<8:reasons.append('chunk_too_short')
        if not value or not any(value in source for source in allowed):reasons.append('text_not_verified')
        duplicate=not reasons and value in seen
        if not reasons:seen.add(value)
        items.append(dict(chunk_id=hit.get('chunk_id'),node_id=hit.get('node_id'),
            parent_id=hit.get('parent_id'),node_type=hit.get('node_type'),
            recipe_name=hit.get('recipe_name'),doc_type=hit.get('doc_type'),reasons=reasons,
            passes_legacy_text_checks=not reasons,duplicate=duplicate,text=preview(text,4000),
            closest_source_line=closest_source(text,original.page_content),
            format_hint=layout_hint(text,original.page_content)))
    return dict(count=len(items),limit=20,may_be_truncated=len(items)>=20,items=items)


class BindingDiagnosticAuditor:
    def __init__(self,driver,vector=None,database='neo4j'):
        self.driver=driver;self.vector=vector;self.database=database

    def audit(self,original):
        recording=RecordingDriver(self.driver) if self.driver is not None else None
        vector=RecordingVector(self.vector) if self.vector is not None else None
        retriever=IdentityBoundRetriever(recording,vector,self.database)
        production=retriever.retrieve(original)
        data=recording.capture if recording else {}
        matches=data.get('identity_lookup',[])
        unique=(len(matches)==1 and matches[0].get('recipe_name')==original.metadata['recipe_name']
                and matches[0].get('recipe_id') is not None)
        candidate=matches[0]['recipe_id'] if unique else None
        report=dict(schema_version=1,recipe=original.metadata['recipe_name'],
            local_recipe_id=original.metadata['recipe_id'],source=original.metadata.get('source'),
            content_sha256=original.metadata.get('content_sha256'),
            production=dict(status=production.status,reason=production.reason,
                graph_recipe_id=production.graph_id,vector_status=production.vector_status,
                accepted_chunks=production.accepted_chunks,rejected_chunks=production.rejected_chunks,withheld_chunks=production.withheld_chunks,
                unverified_steps=production.unverified_steps,evidence_policy='source_projection_v7'),
            candidate_graph_recipe_id=str(candidate) if candidate is not None else None,
            identity=dict(unique_exact_name=unique,matches=matches),
            note='诊断不改变v5准入判定；最近原文行和格式线索需要人工复核')
        if not unique:
            report['diagnostic_vector_status']='not_attempted_no_unique_identity'
            return report
        steps=data.get('identity_steps',[])
        report['steps']=explain_steps(steps,candidate,original)
        source=normalized(original.page_content)
        report['ingredients']=[dict(ingredient_id=i.get('ingredient_id'),name=i.get('name'),
            source_name_present=bool(normalized(i.get('name') or '')) and normalized(i.get('name') or '') in source)
            for i in data.get('identity_ingredients',[])]
        # Audit-only access: a rejected graph binding must never supply vector text to the LLM.
        audit_after_rejection=vector is not None and vector.hits is None and vector.error_type is None
        if audit_after_rejection:
            try:vector.fetch_recipe_chunks(str(candidate),limit=20)
            except Exception:pass
        report['vector_audit_after_graph_rejection']=audit_after_rejection
        report['diagnostic_vector_status']=('unavailable' if vector is None else
            'error:'+vector.error_type if vector.error_type else 'read')
        allowed=[original]
        if production.status in {'graph_verified','graph_scoped_source'}:
            allowed += [d for d in production.documents if d.metadata.get('source_type')=='neo4j_source_projection']
        report['chunks']=explain_chunks(vector.hits or [],candidate,original,allowed) if vector else dict(count=0,items=[])
        report['legacy_text_checks_note']='steps/chunks中的旧整段匹配reasons仅用于对照；当前准入见active_evidence_policy'
        report['active_evidence_policy']={
            'steps':[c for d in production.documents for c in d.metadata.get('step_checks',[])],
            'chunks':production.chunk_checks}
        report['chunks']['production_eligible']=production.status in {'graph_verified','graph_scoped_source'}
        return report
