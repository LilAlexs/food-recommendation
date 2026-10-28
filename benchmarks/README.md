# 第一阶段：监控与性能基线

当前状态：监控代码和评测工具已实现并离线验证；真实服务性能、检索质量基线仍未完成。旧监控补丁未在本次附件、工作目录或两轮文件搜索中找到，本次是基于上传源码的新补丁。

## 运行

在原有可正常运行的 Python 环境中，将补丁包中的 `rag_modules` 文件覆盖到项目同名目录。保留自己的 `.env`，补丁包不含任何密钥文件。

启动后端前设置路径（单进程运行；Docker 内需挂载可写目录）：

```powershell
$env:RAG_TRACE_PATH = "$PWD\benchmark_results\trace.jsonl"
python main.py
```

沿用项目现有方式构建/加载知识库并进入 Web 服务模式（8000 端口）。在另一终端运行：

```powershell
python benchmarks/run_baseline.py --base-url http://127.0.0.1:8000 --model YOUR_ACTUAL_MODEL --top-k 5 --cache-state uncontrolled --repeat 3 --trace benchmark_results/trace.jsonl --output benchmark_results/baseline_run1
```

`--model`、`--top-k` 和缓存状态是实验者声明，脚本不修改服务配置。务必与实际配置一致。每个场景建立独立 session；追问与话题切换先执行种子轮，种子轮不计入目标请求的耗时，仍记录在 trace 中。

输出：`responses.jsonl` 保存目标轮的回答供人工审核；`baseline.csv` 保存每请求 HTTP 耗时、调用数、已知 Token、错误及阶段耗时；`manifest.json` 保存参数、数据集校验和、分组 P50/P95、失败率和 trace 覆盖率。健康检查失败时只输出 `status=blocked`，不会生成虚假的性能数字。原 `/health` 仅为浅层检查，不能保证数据库可用。

同机单进程测试时使用 `--trace` 指向服务端日志；若服务运行在容器或另一台机器，需要让日志可在评测端读取。未提供 trace 时 LLM/Token 指标留空，HTTP 200 也可能包含应用降级文本，必须人工审核；不宜作为最终成功率。日志包含种子轮，可按响应 `X-Trace-ID` 关联目标轮。

## 缓存与实验控制

- 首轮建议填 `uncontrolled`。`cold`/`warm` 只记录声明，不会自动清缓存或预热。
- 需要冷缓存时，在专用测试实例上调用现有 `cache_manager.clear_all_caches()` 或重启实例（并确认缓存初始化状态）；后续查询仍可能命中语义缓存，应在 CSV 中核对 `from_cache`。
- 不要在追问前清会话历史。50 个场景、一次重复包含 50 个目标轮和 20 个种子轮，三次重复共 210 次 HTTP 调用；缓存可能减少实际 LLM 调用。
- 固定数据快照、模型/供应商、温度、max_tokens、top_k、硬件、并发及缓存策略，重复运行。脚本为串行延迟评测，不是并发压测。
- P50/P95 使用最近秩定义，只聚合判定成功的目标请求，包含缓存命中。应同时查看失败率、缓存命中和各类别结果；10 条一组的一轮 P95 样本量较小。

## 监控含义与限制

`RAG_TRACE_PATH` 未设置时不写 trace。线程池通过 `copy_context` 传播同一请求 ID，每个提交复制独立上下文。阶段耗时采用 `perf_counter`；并行阶段与嵌套阶段有重叠，不能相加作为端到端耗时。

LLM 调用数指一次 SDK `create` 调用；SDK 内部自动重试不单独计数。流式调用从创建流计时至迭代结束，包含消费时间；供应商没返回 usage 时 Token 为未知值，不记为 0。本次不强行要求流式 `include_usage`，以免破坏供应商兼容性。服务自带重试会多次计数。超时以异常类名含 timeout 判断，分母可用 CSV 中 LLM 调用总数；不代表统一端到端超时控制。

监控记录模型名、阶段、异常类型及候选标识，不记录提示词、回答和异常正文。原项目自己的业务日志不在此保证范围。trace 为追加文件，应自行轮转。当前只验证单进程；多进程请各用独立日志文件再汇总。

`graph.total` 包含图查询理解，`hybrid.total` 包含关键词理解及检索，`vector.search` 是实际向量搜索，`generation` 是生成总耗时。图路径转 Document 时可能没有 recipe_id，trace 同时保留 recipe_name/node_id；本次不冒充已完成统一菜谱 ID 改造。

HTTP 失败、种子轮失败及可观察到的 LLM 异常都会使目标请求不计为成功；旧检索器吞掉数据库异常后返回空列表的情况仍可能只表现为低质量回答，需结合候选数和人工审核。HTTP 超时后服务可能继续运行，缺失的 trace 不能按零调用处理。

当前 runner 评测 `/api/chat`。SSE 监控覆盖迭代生命周期和中断，但尚无真实 SSE 端到端/首 Token 基线。非法流式请求返回 JSON 时不生成 SSE trace。

## 质量评测待办

50 条查询是起始场景集，不是已标注金标准。`relevant_recipe_ids=null` 表示尚未审核，不可解释为空相关集。需要结合实际图谱、菜谱 ID、数据快照补充相关菜谱/分级相关性、硬约束检查及多轮上下文判定。之后才能报告 Recall@5、MRR@10、NDCG@10、约束满足率和答案正确率。top_k=5 无法计算完整的 @10 指标，必须另开 top_k>=10 的固定配置实验。

## 本次代码检查结论

调用链：

`WebServiceHandler → cache.lookup / query.context（并行）→ route.analysis（LLM）→ hybrid.total 或 graph.total 或两者并行 → generation（LLM）→ 写入缓存/上下文`

正常、无缓存、无重试路径的静态预期：传统分支通常 3 次 LLM（路由、关键词、生成）；图分支通常 3 次（路由、图理解、生成）；组合分支通常 4 次。图数据库未连接、提前失败、回退、缓存命中、流式重试会改变次数，必须以 trace 实测为准。规则指代模块当前虽被导入，但 Web 路径未调用它。

现有 `wait(timeout=...)` 后线程池退出仍会等待运行中的任务，不能把这些值当成真实请求超时。线程内部异常可能被 future 或检索器吞掉。流式重试可能在已输出部分文本后重新生成；本阶段记录问题，未改变检索/重试业务语义。

额外修复两处直接阻碍监控的流式问题：Web 模块缺少 `jsonify` 导入；生成器不能处理 choices 为空的 usage-only chunk。

离线检查：

```bash
python -m unittest discover -s tests -v
python -m compileall -q rag_modules benchmarks tests
```

已通过 10 项测试，其中 HTTP fixture 完整验证 50 目标轮 + 20 种子轮、CSV/汇总和未知 Token 处理。fixture 与假 LLM 的耗时不属于 GraphRAG 性能基线。
