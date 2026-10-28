"""Check catalog outcomes only; not an answer correctness score."""
import argparse
import json
from pathlib import Path

p=argparse.ArgumentParser()
p.add_argument('--responses',type=Path,required=True)
p.add_argument('--queries',type=Path,default=Path(__file__).with_name('identity_queries.jsonl'))
a=p.parse_args()
expected={r['id']:r for r in map(json.loads,a.queries.read_text(encoding='utf-8').splitlines())}
rows=[json.loads(s) for s in a.responses.read_text(encoding='utf-8').splitlines() if s.strip()]
seen=set();correct=0
for row in rows:
    if row['id'] not in expected:continue
    seen.add(row['id']);want=expected[row['id']]['expected_recipe_status']
    ok=row.get('recipe_status')==want;correct+=ok
    print(json.dumps(dict(id=row['id'],expected=want,actual=row.get('recipe_status'),match=ok),ensure_ascii=False))
print(json.dumps(dict(completed_records=len(rows),matched_records=correct,
    missing_scenarios=sorted(set(expected)-seen),answer_quality='requires_manual_review'),ensure_ascii=False))
