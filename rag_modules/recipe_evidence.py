"""Identity is strict; derived prose is not required to be a literal copy.
Only source projections become generation evidence. No fuzzy score grants factual trust.
"""
import re
import unicodedata


def factual_text(value):
    value=unicodedata.normalize('NFKC',str(value or '')).lower()
    # Preserve decimal points, ranges, division and comparison operators.
    return re.sub(r'[\s#*`_，,。;；:：!！?？()（）\[\]【】、]+','',value)


class SourceEvidence:
    def __init__(self,text):
        self.lines=text.splitlines()
        self.index=[(i+1,line,factual_text(line.lstrip('-* ')))
                    for i,line in enumerate(self.lines) if line.strip()]

    def project(self,value):
        compact=factual_text(value)
        if len(compact)<4:return []
        for line_no,line,source in self.index:
            if compact in source:
                # Return the WHOLE source line, not the candidate fragment: preserves
                # 'optional', negation, serving formula and range around a phrase.
                return [dict(line=line_no,text=line)]
        return []

    def document_text(self,projections):
        by_line={row['line']:row['text'] for row in projections}
        return '\n'.join(by_line[n] for n in sorted(by_line))


def parse_chunk(text):
    """Recognise known legacy step envelopes; never blindly delete all labels."""
    lines=str(text or '').splitlines()
    descriptions=[];unsupported=[];has_description=False
    for line in lines:
        match=re.match(r'^\s*描述\s*[:：]\s*(.*)$',line)
        if match:
            has_description=True
            if match.group(1).strip():descriptions.append(match.group(1).strip())
            continue
        if not line.strip() or re.match(r'^\s*#+',line):continue
        if re.match(r'^\s*(?:步骤|方法|工具|时间)\s*[:：]',line):
            continue # Derived auxiliary fields never become factual evidence.
        unsupported.append(line)
    if has_description:
        return dict(kind='step_envelope',payloads=descriptions,unparsed=unsupported)
    # No schema for amounts/formulas/times: retain authoritative original, not these fields.
    if re.search(r'^\s*##?\s*(?:所需食材|食材|原料|菜品描述)',str(text),re.M):
        return dict(kind='derived_fields',payloads=[],unparsed=lines)
    if re.search(r'^\s*#\s+',str(text),re.M) and re.search(r'菜系[:：]|难度[:：]|时间信息[:：]|份量[:：]',str(text)):
        return dict(kind='derived_fields',payloads=[],unparsed=lines)
    if any(re.match(r'^\s*#+',line) for line in lines):
        # Tags are allowed only by projecting their exact supported source lines.
        if re.search(r'^\s*##\s*标签\s*$',str(text),re.M):
            return dict(kind='tags',payloads=[x for x in lines if x.strip() and not x.lstrip().startswith('#')],unparsed=[])
        return dict(kind='unknown_envelope',payloads=[],unparsed=lines)
    return dict(kind='plain',payloads=[str(text)],unparsed=[])


def evaluate_chunk(hit,graph_id,recipe_name,source):
    reasons=[]
    if hit.get('node_type')!='Recipe':reasons.append('node_type_mismatch')
    if str(hit.get('node_id'))!=str(graph_id):reasons.append('node_id_mismatch')
    if str(hit.get('parent_id','')) not in {'',str(graph_id)}:reasons.append('parent_id_mismatch')
    if hit.get('recipe_name')!=recipe_name:reasons.append('recipe_name_mismatch')
    if reasons:
        return dict(state='identity_rejected',reasons=reasons,projections=[],payloads=[])
    parsed=parse_chunk(hit.get('text') or '')
    projections=[];checks=[]
    for payload in parsed['payloads']:
        rows=source.project(payload)
        projections.extend(rows)
        checks.append(dict(state='source_projected' if rows else 'unverified_summary',
                           source_lines=[r['line'] for r in rows]))
    state='source_projected' if projections else 'unverified_summary' if parsed['payloads'] else 'derived_fields_withheld'
    return dict(state=state,reasons=[],projections=projections,payloads=checks,
                kind=parsed['kind'],unparsed_lines=len(parsed['unparsed']),
                unverified_payloads=sum(c['state']=='unverified_summary' for c in checks))
