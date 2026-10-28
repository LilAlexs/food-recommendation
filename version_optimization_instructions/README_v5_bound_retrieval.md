# v5：确认菜谱身份后，按Neo4j ID限定图/向量取材

基于已安装v4的增量补丁。先备份本地修改，再合并同名文件，保留数据、.env及主Compose。所有数据库操作均为读取，不导入数据、不建索引、不修改Schema，也不清空已有向量集合。

## 实现路径

1. 沿用v4的规范名/别名/多版本确认，先取得可信本地原文及local菜谱ID。
2. Neo4j按规范菜名精确查找Recipe，要求唯一；通过后取得现有nodeId作为graph_recipe_id。无匹配/重名不挑第一条，不做模糊绑定。
3. 仅按该nodeId读取REQUIRES食材、CONTAINS_STEP步骤。步骤的所有Recipe入边必须只属于当前ID，拒绝跨菜谱共享或错误关联；步骤序号完整、无重复，顺序需与原文对应。
4. 每条步骤description去标点/空白归一化后，必须作为完整片段出现在目标原文；食材名称也要出现在目标原文。未核验的图谱用量、工具、时长等属性不送入生成模型。图文不一致即整路回退本地原文。
5. 验证图绑定后，Milvus按现有node_id和parent_id限定目标Recipe读取文本，不再为明确菜名做跨菜谱Top-K搜索，也不重新计算Embedding。固定字段、JSON转义ID，避免构造任意过滤表达式。
6. 对返回的每个chunk再次验证菜谱名、node_type、node_id、parent_id及正文归属；不通过的chunk丢弃并记录数量。最多追加3个去重、核验过的证据片段。

本地完整文档始终保留，提供简介/适用范围、用量和完整步骤，避免图步骤缺失造成资料不完整。图证据与向量证据是同一目标的补充，不是放弃已核验原文。此设计可能增加重复内容及Token，不是新一轮提速优化；当前优先验证关联正确性。

local:路径哈希ID与Neo4j nodeId是不同标识，分别记录recipe_id和graph_recipe_id，不混用。Milvus现有Schema没有统一recipe_id字段，本次使用其实际node_id/parent_id，不伪造Schema已迁移。

## 结果分类

- graph_verified：唯一图身份、步骤所有权和原文校验通过，可附加该图证据。
- local_fallback：图缺失、重名、错误步骤关系、内容不一致、序号异常或服务不可用。保留v4原文，不自动选择其他菜。
- vector_status=empty/vector_unavailable/vector_error：保留已验证图证据与原文，不代表“库中没有这道菜”。
- vector_status=contains_rejected_chunks：至少一个返回chunk被再次校验拦截，需后续检查索引数据。

图校验通过只说明以上检查通过，不等于烹饪内容经过专业审核。完整原文中的改编说明仍可能需要前端/模型更清楚地标示，本包没有治理菜谱权威性。

## 应用与只读审计

复制代码后，在PowerShell项目根目录执行：

```powershell
$env:RAG_ADAPTIVE_CONTEXT="1"
$env:RAG_FAST_SIMPLE_QUERY="1"
$env:RAG_RECIPE_GUARD="1"
$env:RAG_BOUND_RETRIEVAL="1"
docker-compose -f docker-compose.yml -f compose.llm-diagnostics.yml -f compose.optimizations.yml -f compose.recipe-identity.yml -f compose.bound-retrieval.yml up -d --no-deps --force-recreate backend
```

无新增依赖，无需pull/build。后端会重置内存会话，但数据库卷保留。等待/health可访问后，先运行只读审计，**无需调用大模型，也不需要重新跑50条评测**：

```powershell
cmd /c "docker-compose exec -T backend python - < benchmarks\audit_bound_retrieval.py"
```

默认审计宫保鸡丁、麻婆豆腐、西红柿炒鸡蛋。输出名称、local/graph ID、status、reason、vector_status、接收/拒绝chunk数量；不打印令牌、数据库密码或异常正文。脚本使用当前容器的连接配置和DEFAULT_CONFIG集合名；如使用了额外Milvus认证或自定义连接，应先把脚本连接参数与你部署配置对齐，不能据连接失败认定索引坏了。

把输出发回分析实际数据。大量local_fallback也可能只是旧图谱description是LLM改写而无法通过逐段核对，不足以认定这些数据一定错误。保守校验有意偏向拒绝不确定证据。现有数据库不在当前运行环境里，所以未声称已验证你的真实图/向量关联。

图查询为READ会话，所有Cypher值参数化，每次查询timeout=5秒；Milvus查询timeout=5秒。不是整个HTTP请求的5秒截止，也不保证底层连接错误立即结束。

## 网页与日志

审计后新建会话问“宫保鸡丁怎么做？”或“麻婆豆腐怎么做？”。确认资料仍只属于目标菜；Trace新增recipe_binding，记录绑定结果与回退原因。普通JSON返回retrieval_backend、graph_recipe_id、vector_status；SSE通过前置元数据发送，现有前端仍显示正文。

需要v4/v5对照时，仅改RAG_BOUND_RETRIEVAL（0为v4原文分支，1为本次按ID核验），重新创建后端；其他开关、模型、数据、问法保持一致。不要把服务回退或业务澄清当成全量数据库读取成功。

可继续使用v4身份场景评测：

```powershell
python benchmarks/run_baseline.py --base-url http://localhost:8000 --model Qwen/Qwen3.8-Flash-Next --top-k 5 --cache-state uncontrolled --queries benchmarks/identity_queries.jsonl --repeat 1 --timeout 240 --trace benchmark_results/trace.jsonl --output benchmark_results/v5_identity1
```

输出中新记录retrieval_backend、graph_recipe_id、vector_status；已有目录换新名称。v4的not_found/needs_confirmation照旧不调用图/向量或LLM。

## 修改文件与验证

新增：identity_bound_retrieval.py、compose.bound-retrieval.yml、audit_bound_retrieval.py、test_bound_retrieval.py。
修改：web_service_handler.py、milvus_index_construction.py、performance_monitor.py、run_baseline.py。

65项离线测试通过。新增覆盖唯一身份、参数化读取、跨菜步骤拒绝、同名错误正文拒绝、原文步骤顺序、未知食材、chunk身份与正文二次校验、服务异常回退、Milvus过滤转义，以及实际Milvus方法的参数/异常契约。数据库客户端和Web仍为替身，没有真实Neo4j/Milvus集成测试、模型质量测试或上线验证。

未完成：全量图索引数据审计/迁移、统一跨库永久recipe_id、结构化长期偏好、硬约束过滤、所有复杂自然语言问题的对象确认。复杂推荐/关系查询仍走旧检索，不把单菜确认路径的验证结果推广到所有GraphRAG场景。
