# v9.1 当前菜谱用量追问修复

前提：已安装v9。本包是v9的小增量，不是从原版/v7直接升级的完整累积包。

## 实测根因

用户红烧鱼之后问“油需要多少”，模型返回recipe/current/0.88，v9要求0.9导致误拦截；提示后清除当前菜谱身份，下一句“葱需要多少”失去对象，返回clarify/none/0.35。
红烧鱼原文写明油30-50ml、葱1-2根，不是资料缺失。

## 修复

- 模型判断recipe/current，且后端已保存成功确认的菜谱时，以该身份回查资料，不再用0.9门槛否决有对象的追问。不是把所有分支阈值统一降低。
- 如原话提及另一道已知菜或明确切换话题，与current判断冲突时仍澄清，不沿用旧菜。
- 同一当前菜谱内的操作含义待确认（clarify/current）保留菜谱身份，澄清不写入历史/答案缓存。
- 未收录的新菜、无已确认对象、多菜/换话题冲突仍不自动继承旧菜。超时/非法输出策略未放宽。
- 新增query_resolution_decision和recipe_state事件，定位接收/清状态原因；修正Trace开关缺省值，并记录RAG_SEMANTIC_QUERY。
- 不增加额外模型调用，不改数据库、索引、前端、模型和Nginx。

## 安装

备份v9的rag_modules目录后，把包内rag_modules与tests合并进：
E:\work\What-to-eat-today-main
覆盖同名文件，保留其他文件。不要安装到food-recommendation-main原始备份。

已运行v9且Python目录挂载时：

```powershell
cd E:\work\What-to-eat-today-main
docker-compose -f docker-compose.yml -f compose.query-debug.yml restart backend
```

无需build、不重建容器/数据库。restart保留当前容器记录开关，但会清空内存会话。/tmp日志可能包含先前记录，按trace时间区分。

等待健康：

```powershell
curl.exe --noproxy "*" --max-time 10 http://127.0.0.1:8000/health
```

未重建容器通常无需重启Nginx；若页面API报502再检查Nginx日志。

## 验收

网页新会话连续问：
1. 红烧鱼怎么做？
2. 油需要多少？
3. 葱需要多少？

预期第2题30-50ml，第3题1-2根，或忠实保留原文语义。两题均应取红烧鱼资料，不能再次要求确认菜名。用同义的新问法也要继续观察，不宣称自然语言已全覆盖。

```powershell
docker cp what-to-eat-backend:/tmp/query_debug.jsonl .\query_debug_v9_1.jsonl
```

如仍失败，请提供回答及新trace，其中会多出实际接受理由、是否保留当前菜谱状态。

## 验证范围

126项离线测试通过：原120项+新增6项，包含0.88实测分支连续油/葱追问、同菜澄清后继续追问、无对象不猜、显式新菜与current冲突、未收录新菜清身份及SSE实际取材。
测试使用模拟模型检查传入资料（含30-50ml、葱1-2根），未连接用户在线模型。真实回答仍待复测，原先模型识别不确定性并未完全消失。
