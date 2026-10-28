"""Resolve simple quantity questions only against a committed recipe's source.
This selects a target, never invents quantities or changes the original question.
"""
import re
from rag_modules.performance_monitor import event

AMOUNT = re.compile(r'(?:请问[，,\s]*)?(?:这道菜的?)?'
                   r'([\u4e00-\u9fff]{1,12}?)(?:的)?'
                   r'(?:需要|要|应该(?:放|加|用)?|该(?:放|加|用)?|放|加|用)?'
                   r'(?:多少(?:克|毫升|ml|g|根|个|勺)?|几(?:克|毫升|根|个|勺))'
                   r'(?:才合适|合适|呢)?[？?。！!]?')


def source_quantity_target(query, current, catalog):
    if not isinstance(current,dict) or not current.get('name') or not current.get('recipe_id'):
        return None
    match=AMOUNT.fullmatch(query.strip())
    if not match:return None
    term=match[1]
    # No comparisons, exclusions, multi-item requests or hidden topic switches.
    if re.search(r'和|与|及|不|换|别|其他|推荐|保存|时间|多久',term):return None
    result=catalog.resolve(current['name'])
    if not result.get('recipe') or result['recipe'].recipe_id!=current['recipe_id']:
        return None
    document=catalog.document(result['recipe'])
    in_ingredients=False
    names=[]
    for line in document.page_content.splitlines():
        if line.startswith('## '):
            in_ingredients=bool(re.search(r'原料|食材|材料|配料|计算|用料',line))
        if in_ingredients:
            item=re.match(r'^\s*[-*]\s+([\u4e00-\u9fff]+)',line)
            if item:names.append(item[1])
    # Accept an exact ingredient name or a suffix (葱 -> 小葱, 油 -> 植物油).
    # Multiple matching oils remain separate evidence for the generator, not summed.
    if not any(name==term or name.endswith(term) for name in names):return None
    event(kind='query_resolution_decision', reason='quantity_in_confirmed_source')
    return result
