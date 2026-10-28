# v3：失败处理、按需上下文、简单查询规则优先

基于已安装 v2 的增量补丁。先备份本地改动，再将本包同名目录合并到原项目，替换同名文件。不要只复制 .patch，也不要删除原项目其他文件。不提供或修改 .env、数据库、前端、主 docker-compose.yml。

## 本次改动与边界

1. 失败处理始终启用：生成失败/空回答抛出 GenerationError；普通 HTTP 返回 502，不再把错误字符串当成功答案。SSE 已开始时无法改状态码，保留 chunk 格式并发出 error/success=false；Trace 标记失败。未完成、断流、空内容不写缓存/历史。存储入口也拦截已知旧错误提示；不是按任意“抱歉”字符串判断失败。仅修复后续写入，不能删除浏览器已有错误消息。重建后端会清空原内存缓存/历史。
2. RAG_ADAPTIVE_CONTEXT=1：独立做法/用料查询跳过无关历史；单一明确前菜的简单“它炖多久”用本地规则补全；省略/模糊追问保留最近三轮用户文本及每轮前100字回答。无额外 LLM 重写调用。没有实现完整 SessionState 或复杂指代模型；模糊问题宁可保留上下文，不强行猜菜。
3. RAG_FAST_SIMPLE_QUERY=1：完整匹配本地 data/dishes 中菜名及“怎么做/的做法/需要哪些材料”等模板时，路由固定为传统混合检索，关键词为明确菜名，不再猜测额外食材/主题。路由、关键词共用 SimpleQuery 解析函数，无跨线程可变共享分析状态。带限制、比较/关系、多菜、未知模板及包含拼接历史的查询继续原 LLM 流程。检索融合方式未改。预期简单无缓存成功路径从3次调用变为1次，真实速度和检索效果需实测，不承诺保持完全相同候选。

两个开关默认0，便于在同一版代码做消融。失败修复在所有组都启用，不提供“重新缓存错误”的开关。

保守约束策略：最近保存的用户请求中出现过敏、忌口、不放、不含、时间、人份等标记时，会保留最近最多3条约束原文，不直接丢弃。显式取消限制后，不继承更早限制。当前不自动判断临时条件/长期过敏的精确作用范围，可能过度携带原文；这是保守回退，不是已完成的结构化约束管理。历史仍最多10轮，长期偏好持久化未实现。

为避免旧语义缓存把不同菜谱/不同追问当同一问题，启用上下文优化时：独立问题只做原文完全一致的缓存命中；依赖上下文的问题不查/不写原始query键的缓存。这个变化计入B组，所以应核对 from_cache，不能把B组差异全部归功于输入缩短。对比脚本标记缓存 uncontrolled，不伪装冷缓存实验。

## 先应用并运行 C 组（完整优化）

PowerShell 项目根目录执行：

```powershell
$env:RAG_ADAPTIVE_CONTEXT="1"
$env:RAG_FAST_SIMPLE_QUERY="1"
docker-compose -f docker-compose.yml -f compose.llm-diagnostics.yml -f compose.optimizations.yml up -d --no-deps --force-recreate backend
```

原项目已有 rag_modules 挂载，无新增依赖，本次无需 pull/build。保留你当前已验证的 ModelScope 模型、令牌和超时配置。重建后端终止旧请求并清空内存会话，不删除数据库卷。后续重建时继续使用这三个 -f 参数，或将同样变量手动合入原 Compose。

确认：

```powershell
docker-compose exec -T backend python -c "import os; print({k:os.getenv(k) for k in ['LLM_MODEL','RAG_ADAPTIVE_CONTEXT','RAG_FAST_SIMPLE_QUERY']})"
```

待后端就绪后先跑之前相同的5条：

```powershell
python benchmarks/run_baseline.py --base-url http://localhost:8000 --model Qwen/Qwen3.8-Flash-Next --top-k 5 --cache-state uncontrolled --limit 5 --repeat 1 --timeout 240 --trace benchmark_results/trace.jsonl --output benchmark_results/v3_C_pilot1
```

已有同名输出时改用新目录。不要同时网页聊天。发送 baseline.csv、manifest.json、responses.jsonl 和 trace.jsonl 分析。若 simple_query 匹配生效，Trace 会出现 route/keywords source=rule；无匹配不会强制走捷径。

## A/B/C 分步对比

| 组 | RAG_ADAPTIVE_CONTEXT | RAG_FAST_SIMPLE_QUERY | 用途 |
|---|---:|---:|---|
| A | 0 | 0 | 失败修复后的原分析与上下文流程 |
| B | 1 | 0 | 按需上下文及配套缓存策略 |
| C | 1 | 1 | 再加入单菜规则分析 |

每组在PowerShell先设置两个变量，然后执行上面的三文件Compose重建命令。每组使用相同模型、数据、Top-K、温度、max_tokens、超时/重试、完整数据集。后端重建重置内存缓存，但每个组内仍可能有语义缓存命中，应核对输出。不要 down -v。每组输出新目录 A_run1/B_run1/C_run1。

三个组统一使用：

```powershell
python benchmarks/run_baseline.py --base-url http://localhost:8000 --model Qwen/Qwen3.8-Flash-Next --top-k 5 --cache-state uncontrolled --queries benchmarks/comparison_queries.jsonl --repeat 1 --timeout 240 --trace benchmark_results/trace.jsonl --output benchmark_results/A_run1
```

B/C组把输出目录相应改名。数据集15个目标场景，种子轮另执行，包括独立查询、指代、省略、追加条件、话题切换、约束保留/取消、多菜关系。可以先各跑一次检查，再按需要重复同样实验，不预设必须长时间全量压测。模拟生成失败属于离线测试，不向线上供应商故意制造故障。

合并已有结果（不发送请求）：

```powershell
python benchmarks/compare_runs.py benchmark_results/A_run1 benchmark_results/B_run1 benchmark_results/C_run1 --output benchmark_results/comparison.csv
```

CSV记录数据集校验和、组开关、耗时、调用次数和失败率。必须核对组间场景数、数据集hash、配置、缓存命中及失败是否一致，不能直接比较一组完整/一组中途停止的总指标。质量列仍是 pending_manual_review：对照responses人工检查目标菜是否正确、追问指代、约束保留、话题切换；不能拿调用成功率当答案正确率。失败样本Token未知值不补0。

已有5条 ModelScope pilot保留为原始参考：4条未观察到LLM异常，客户端耗时29.7/32.8/39.6/55.4秒；第5条生成连接失败。仅5条新会话，不能用于多轮准确率或正式P95结论。旧pilot与v3存在失败处理差异，正式消融应使用v3的A/B/C。

## 监控修正

Trace增加功能开关、上下文处理原因/字符长度、分析决策来源。SSE记录后端首次非空正文可yield的 first_content 时间，包含前置路由检索；不等同浏览器首字时间，不记录思考Token或网络代理缓冲。非流式依旧测完整回答。HTTP错误保留X-Trace-ID，便于502与Trace关联。CSV区分application/llm/client_or_http失败层，停止原因不再误称全部为客户端超时。

## 修改文件

新增：generation_errors.py、query_preparation.py、compose.optimizations.yml、comparison_queries.jsonl、compare_runs.py、test_v3_features.py。
修改：generation_integration.py、session_cache_manager.py、web_service_handler.py、intelligent_query_router.py、hybrid_retrieval.py、performance_monitor.py、run_baseline.py、test_client_reliability.py。

## 验证与限制

35项离线测试通过，含真实路由/关键词方法开关对照（两次分析调用→0）、普通生成失败502且不入缓存、成功写入、SSE部分失败/断流/空流不保存、首次内容计时、上下文/约束回退、缓存安全、评测中断/恢复。Web测试使用Flask接口替身调用实际处理器；没有真实Flask客户端、数据库、模型或前端集成验证。后端共享对象的同会话并发顺序控制、结构化长期偏好、菜谱候选混用、复杂追问质量仍待后续工作，本包不声称已经解决。
