# v6：只读诊断补充（基于v5，不改变在线回答）

目的：解释宫保鸡丁/西红柿炒鸡蛋的 step_source_not_verified，以及麻婆豆腐4个向量片段的具体拒绝原因。当前证据不足以判定数据库错误，所以本次不放宽阈值、不重建索引、不改数据。

## 应用

先备份原审计脚本，再将以下两个文件合并到已安装v5的项目中：

- 新增 rag_modules/binding_diagnostics.py
- 替换 benchmarks/audit_bound_retrieval.py

附带 tests/test_binding_diagnostics.py 供离线回归。本包不覆盖在线检索模块、Web处理器、生成模块、.env、Compose或数据库。原项目已把rag_modules挂载进容器，审计脚本通过stdin执行，所以不需要pull/build/restart。要求后端容器已在运行，且之前v5模块仍保留。

## 执行（PowerShell项目根目录）

```powershell
New-Item -ItemType Directory -Force benchmark_results | Out-Null
cmd /c "docker-compose exec -T backend python - < benchmarks\audit_bound_retrieval.py > benchmark_results\binding_details.jsonl"
```

将生成的 benchmark_results/binding_details.jsonl 上传分析即可。输出包含菜谱内容片段，可能比较长，不必逐行复制到聊天。固定文件名会覆盖上一次同名审计报告；需要保留多次结果时改用 binding_details2.jsonl 等新文件名。

不调用LLM或Embedding，只执行原v5的读查询和校验。图节点可唯一确认但步骤不通过时，诊断脚本会额外按这个候选ID只读查询向量片段，帮助定位关联问题；这些audit-only片段绝不会传给生成模型。无法确认唯一身份时，不会继续查询向量。生产流程仍维持v5的原拒绝/回退规则。

## 报告怎么看

每行是一道菜的JSON，共默认3道菜：宫保鸡丁、麻婆豆腐、西红柿炒鸡蛋。

- production：原v5实际判定、回退原因、接收/拒绝chunk数，保持原语义。
- candidate_graph_recipe_id：图名称唯一匹配时保留的候选ID。步骤校验失败也能显示；不意味着该绑定已经通过。
- identity：唯一名称检查及返回的图节点。
- steps.items：步骤ID、序号、所有归属Recipe ID、正文预览、具体失败项和最接近的原文行。
- steps.order_unique_sorted / source_sequence_ok：序号及原文位置顺序检查。
- ingredients：所属食材与名称在原文是否出现。
- chunks.items：chunk ID、node_id、parent_id、名称/类型、正文预览及全部失败项。
- chunks.production_eligible：这些片段是否具备进入原生产校验路径的前提。图失败时为false，即使某片段的局部检查通过，也不表示被生产流程接受。
- vector_audit_after_graph_rejection：是否进行了上述额外的只读排查。

拒绝原因分项：

| 位置 | 原因 | 含义 |
|---|---|---|
| 步骤 | step_has_other_or_missing_owner | 入边归属不是唯一目标菜谱 |
| 步骤 | description_empty / description_too_short | 正文为空/短于原规则要求 |
| 步骤 | description_not_in_target_source | 整段规范化正文不在目标原文中 |
| 步骤 | step_order_missing | 无合法数值序号 |
| 向量 | node_type_mismatch | 不是Recipe类型 |
| 向量 | node_id_mismatch | 不是目标图ID |
| 向量 | parent_id_mismatch | 分块所属父ID不一致 |
| 向量 | recipe_name_mismatch | 规范名称不一致 |
| 向量 | chunk_too_short | 正文过短 |
| 向量 | text_not_verified | 无法作为完整规范化片段匹配允许资料 |

同一项可能有多个原因，不只显示第一个。duplicate只表示通过局部校验后发现与前面重复，不会重复计入生产接受数量。向量最多读取20条；达到20条时may_be_truncated=true，不能当成全量索引审计。

## 关键限制

closest_source_line 的 lexical_ratio 是词面字符相似度，仅用于定位人工对照，不是语义置信度或放行阈值。它比较的是最多300个规范化输入字符与最多400个规范化原文行字符，不用于完整长文等价证明。

format_hint 会尝试忽略标题、编号和“描述”等字段标签，检查较长内容行能否在原文中找到。all_long_payload_lines_supported=true 只表示有格式差异的可能；短字段可能未检查，标题本身也可能有重要含义，不能据此自动判断合理或修改数据库。

步骤正文预览最多2000字符，向量最多4000字符，原文对照行最多600字符；characters/truncated明确标记截断。原v5生产校验仍使用完整文本，不使用预览。需要更长内容时应进一步按具体ID读取，而不是先降低约束。

异常只记录错误类型，不记录异常正文、凭证、连接串。客户端连接配置沿用v5脚本；如自定义认证/端口/库名，请保持与真实部署一致。

## 接下来如何决策

1. 若显示跨菜谱步骤归属、明显不同菜名/ID：定位对应边/分块，准备具体修复清单，再备份后修复数据。
2. 若身份正确、仅标题/编号/字段标签导致拒绝：根据实测格式设计确定性的解析，不直接改成语义分数放行。
3. 若确为合理改写：先明确允许的改写范围和验证样本，不仅凭高相似度判断。
4. 不能确定时保持原文回退，不增加新的模型调用来猜测。

当前只完成诊断代码，尚无本次真实数据库的详细报告，不预先给出数据错误结论。

## 验证

79项离线测试通过（原65项+新增14项），覆盖原判定保持、候选ID保留、跨菜归属细节、向量独立拒绝原因、格式线索不改变准入、向量异常不重复调用、错误正文不泄漏、重复片段、序号和原文顺序、只读参数化查询及预览截断。使用数据库客户端替身，仍需用户部署运行以上只读审计。
