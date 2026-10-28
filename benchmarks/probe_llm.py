"""Run inside the backend container. No dotenv read and no credential output.
Three short non-stream calls validate connection and JSON/content compatibility.
Uses the same environment as the backend; this is not a RAG quality baseline.
"""
import json
import os
import time
from openai import OpenAI


def main():
    client=OpenAI(api_key=os.environ['OPENAI_API_KEY'],
                  base_url=os.environ['OPENAI_BASE_URL'],timeout=60,max_retries=0)
    model=os.environ['LLM_MODEL']
    cases=[
        ('route_json','仅返回JSON，不要解释：{"recommended_strategy":"hybrid_traditional","confidence":0.9}。'),
        ('keyword_json','提取“番茄炒蛋需要哪些材料”的关键词。仅返回JSON，使用entity_keywords和topic_keywords两个字符串数组字段。'),
        ('grounded_answer','仅基于资料回答。资料：番茄炒蛋的食材为番茄、鸡蛋、盐、食用油。问题：需要哪些材料？不要添加资料中没有的食材。')]
    failed=False
    for name,prompt in cases:
        start=time.perf_counter()
        try:
            response=client.chat.completions.create(model=model,
                messages=[{'role':'user','content':prompt}],temperature=0.1,max_tokens=256)
            content=response.choices[0].message.content or ''
            valid=None
            if name.endswith('_json'):
                try:
                    value=json.loads(content)
                    valid=isinstance(value,dict)
                except ValueError:valid=False
            failed=failed or not content.strip() or valid is False
            usage=response.usage
            print(json.dumps(dict(test=name,model=model,seconds=round(time.perf_counter()-start,3),
                success=bool(content.strip()) and valid is not False,json_valid=valid,
                finish_reason=response.choices[0].finish_reason,
                prompt_tokens=getattr(usage,'prompt_tokens',None),
                completion_tokens=getattr(usage,'completion_tokens',None),
                content_preview=content[:400]),ensure_ascii=False),flush=True)
        except Exception as exc:
            failed=True
            print(json.dumps(dict(test=name,seconds=round(time.perf_counter()-start,3),
                success=False,error_type=type(exc).__name__,
                cause_type=type(exc.__cause__).__name__ if exc.__cause__ else None,
                status_code=getattr(exc,'status_code',None)),ensure_ascii=False),flush=True)
            break # do not spend more calls after a connection/auth/rate-limit failure
    return int(failed)

if __name__=='__main__':
    raise SystemExit(main())
