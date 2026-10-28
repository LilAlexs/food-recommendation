# 第一阶段 v2：评测恢复与 ModelScope 试跑

这是基于上一版监控补丁的增量更新。先备份本地修改，再复制本包中的同名文件到项目根目录；不会提供或覆盖 `.env`、主 docker-compose.yml、数据库或前端。

修改的核心源码仅两份：
- rag_modules/generation_integration.py：共享客户端默认 socket 超时 60 秒、SDK 自动重试 0 次、流式尝试 1 次；已输出部分文本后失败不再从头重试，避免混合/重复答案。
- rag_modules/performance_monitor.py：异常增加 cause_type 和 status_code，不记录异常正文。

新增/更新 benchmarks/run_baseline.py、benchmarks/probe_llm.py、测试及 compose.llm-diagnostics.yml。默认重试策略与上一版不同，这是可靠性配置改进，比较结果时必须注明。没有重构路由规则、融合算法和提示词，也未启用供应商特有的思考模式参数。

## 已能确认的问题

上传日志中，“红烧肉”从 03:02:21 到 03:07:43 约 322 秒；“番茄炒蛋”约 238 秒。客户端 180 秒超时后服务端仍在执行，下一题已开始，形成重叠。LLM 多次 Connection error 和重试；向量检索约 0.3–0.5 秒。长模型调用叠加串行路由/检索理解/生成，远超客户端等待时间。

这些证据不能进一步区分 DNS、TLS、代理、上游排队/断连；健康检查 200 也不能证明模型可用。更换供应商和模型是有价值的排查对照，但不能把所有提速归因于模型。此前单条流式 trace 出现 GeneratorExit，能说明生成器关闭，不能单凭它判断是谁主动断开。

## 1. 修改 .env

先在本地备份原供应商三项配置，然后替换为你在另一个项目已用通的配置。ModelScope 常用入口示例如下；若你原项目使用专属入口，以已验证的入口为准：

```dotenv
OPENAI_API_KEY=你的ModelScope令牌
OPENAI_BASE_URL=https://api-inference.modelscope.cn/v1
LLM_MODEL=Qwen/Qwen3.8-Flash-Next
LLM_TIMEOUT_SECONDS=60
LLM_MAX_RETRIES=0
LLM_STREAM_ATTEMPTS=1
```

原变量名 OPENAI_API_KEY 是兼容接口凭证名称，不要求使用 OpenAI 的密钥。不要发送令牌给助手。本包没有实际令牌，没有执行真实供应商调用。

## 2. 重建容器配置

先停止评测（Ctrl+C），不要同时在网页发送问题。复制文件后，在原项目根目录、Windows CMD 执行：

```bat
docker-compose -f docker-compose.yml -f compose.llm-diagnostics.yml up -d --no-deps --force-recreate backend
docker-compose logs --tail=60 backend
```

原 Compose 已把 rag_modules 挂载到容器，因此本次不增加依赖、也无需 build。上述命令会重新创建后端以注入新环境变量，并终止旧请求；内存缓存和上下文会重置，数据库卷保留。后续若希望保留诊断环境配置，启动后端时继续带上这两个 -f 参数。不要使用 down -v。

override 只补充监控挂载和诊断变量，模型名、API 地址和令牌仍由你原 docker-compose.yml 中的 .env 插值传递。如果你后来改过主 Compose，请确认它仍引用这些变量，而非写死旧配置。不要把 `docker-compose config` 完整输出发给别人，里面可能含凭证。

## 3. 先在同一个后端容器做三项小测试

等服务启动后，在 Windows CMD 中执行（PowerShell 不支持同样的 `<` 输入重定向，见下一条）：

```bat
docker-compose exec -T backend python - < benchmarks\probe_llm.py
```

PowerShell：

```powershell
Get-Content -Raw -Encoding UTF8 benchmarks/probe_llm.py | docker-compose exec -T backend python -
```

脚本从容器已有环境读取凭证，执行路由 JSON、关键词 JSON、依据资料回答三次非流式测试，显示耗时、Token、finish_reason 和少量测试回答。JSON 检查仅确认能直接解析为对象，不是完整业务 Schema 或答案质量判定。遇到连接/鉴权等异常就停止后续调用。内容为空、JSON 解析失败或 length 截断时先检查结果，不要直接跑全量。固定 256 输出 Token 是小测试预算，不能当成生产生成长度。

如果你之前的项目很快、容器小测试仍慢，需要继续对比宿主机与容器的网络/代理条件。脚本异常只打印错误类、直接原因类和 HTTP 状态，不打印异常正文或令牌；凭这些信息也不一定能一次定位底层网络问题。

## 4. 先跑 5 个场景

在宿主机终端执行（本机需 Python，不需要安装额外依赖）：

```bat
python benchmarks/run_baseline.py --base-url http://localhost:8000 --model Qwen/Qwen3.8-Flash-Next --top-k 5 --cache-state uncontrolled --limit 5 --repeat 1 --timeout 240 --trace benchmark_results/trace.jsonl --output benchmark_results/modelscope_pilot1
```

输出目录必须是新目录，已有 responses.jsonl 时拒绝覆盖。--limit 5 取查询文件前 5 条，当前都是独立查询，只用于连通性试跑，不能代表五类整体质量。每个完成的场景都会更新 responses.jsonl、baseline.csv 和 manifest.json，Ctrl+C 也会保存已完成场景；正在等待的请求不算已完成。强制杀进程/断电无法保证最终写入，CSV 检查点也非事务持久化。

默认一次客户端失败即停止（--max-consecutive-failures 1）；可观察到 trace 中的 LLM 错误也会触发停止。健康检查失败会输出空 CSV 和 blocked 汇总。HTTP 返回异常后服务端可能继续执行；停止发题减少继续叠加，但不取消已有服务端任务。

LLM_TIMEOUT_SECONDS 是 SDK 网络 I/O 等待限制，不是完整请求硬截止；多阶段仍可能累积超过它。API 重试为 0 也不保证服务响应必定在 60 秒以内。若再次失败，先查看状态和日志，不要循环重复启动测试。

## 5. 恢复旧评测的 CSV 和汇总（可选）

```bat
python benchmarks/run_baseline.py --model Qwen/Qwen3-8B --top-k 5 --cache-state uncontrolled --summarize-only --trace benchmark_results/trace.jsonl --output benchmark_results/baseline_run1
```

不会调用后端，也不会继续发题。会从旧 responses.jsonl 重建 CSV 和汇总，标为 recovered_partial；不会把客户端超时变成成功。应使用旧模型名，不能把旧结果归到新模型。没有 X-Trace-ID 的超时请求无法自动关联后来完成的 trace，其调用/Token 保持未知。

## 如何决定保留模型

Qwen/Qwen3.8-Flash-Next 是可核实的官方模型 ID，ModelScope 提供 OpenAI 兼容入口。适合作为本项目中文菜谱问答、关键词提取和路由解析的候选，是否正式保留由你实际账号的 API 可用性、短 JSON 输出、延迟和检索约束质量决定。另一个项目速度快是试跑依据，不是本项目效果保证。

先通过容器小测试，再跑 5 条，最后补齐 50 条场景及人工审核，固定检索数据/Top-K/缓存/温度/max_tokens。对照应使用同一版 v2 代码和重试设置，分别切换供应商配置。原来两条客户端超时只是故障样本，不能计算可信的成功请求 P50/P95，也不能声称已实现某个优化百分比。

核实来源：
- https://modelscope.cn/models/Qwen/Qwen3.8-Flash-Next/summary
- https://github.com/QwenLM/Qwen3.8-Flash-Next/blob/main/README.md
- https://help.aliyun.com/en/mobi/integrating-openai-api-with-llm

## 验证

19 项离线测试通过：包括完整 50+20 次模拟 HTTP 请求、失败停止、Ctrl+C 部分保存、空结果保存、失败种子轮不继续发题、旧结果恢复、避免覆盖、客户端超时配置、流式部分输出后禁止重试等。真实 ModelScope 调用仍需在你的环境验证。
