# v10.1：简单用量追问与分析失败状态保留

前提：已经安装v10。本包为v10的小增量，不是从原版/v7直接升级的完整包。

## 本次真实根因

query_debug_v10最后一轮：语义分析约16.4秒后APITimeoutError，底层ReadTimeout。
不是0.88门槛、不是缺少油用量、不是限制更新时直接丢失身份。
分析异常后返回通用澄清，Web据此清除当前菜谱，形成二次状态损失。

## 修复

1. 新增quantity_followup.py：有已确认菜谱名称和ID时，匹配有限的用量问句语法，从该菜谱的原料/食材/计算等章节核对食材名。食材从原文读取，不硬编码“油、葱、蚝油”。
2. 核对成功只确定目标，不在规则里计算数量；仍按现有流程取完整原文与生效要求交给生成模型。省去一次语义分析，不省去最终回答调用。
3. 无身份、来源ID不符、未在原文食材章节核对到、多菜比较、换话题及复杂问法不走这条捷径，保留原有处理。
4. 同一菜有多种油时仍保留原文各项，不能把“油”直接转换成一个合计量。原始用量冲突仍需答案说明，当前未实现数值一致性自动审核。
5. 分析超时/非法JSON/字段错误改为analysis_unavailable、可重试服务错误；保留已有菜谱和要求，不生成未经确认的答案。不再让用户补充已经确认过的菜名。
6. 此错误会标记Trace failed；普通JSON success=false，SSE携带错误状态和提示，仍使用既有结束协议。没有重新增加SDK自动重试，不修改模型或延长所有超时。

## 安装

先备份v10的rag_modules目录。将包内rag_modules与tests合并到：
E:\work\What-to-eat-today-main
覆盖同名文件、保留其他文件，不修改food-recommendation-main原版备份。

```powershell
cd E:\work\What-to-eat-today-main
docker-compose -f docker-compose.yml -f compose.query-debug.yml restart backend
```

Python目录已挂载，不用build、不重建数据库/索引。restart清空内存会话。

```powershell
curl.exe --noproxy "*" --max-time 10 http://127.0.0.1:8000/health
```

健康后新建网页会话，依次问：
1. 水煮肉片怎么做？
2. 这道菜不放辣椒。
3. 油需要多少？
4. 小葱需要多少？

第3/4题应该直接取该菜资料且保留不放辣椒要求，Trace应出现query_resolution_decision/quantity_in_confirmed_source；该轮不应再有额外query_understanding模型调用。
最终生成仍可能因模型服务故障失败，此次不声称所有模型调用超时已消失。

水煮肉片原文计算段列植物油280g、菜籽油200g；操作段还有分步用油及滑锅留底油，不应凭空合并/修正成单一总油量。小葱2根。此处是核对原文，不是重新审核菜谱配方的合理性。

```powershell
docker cp what-to-eat-backend:/tmp/query_debug.jsonl .\query_debug_v10_1.jsonl
```

## 验证

159项离线测试通过（原152项+新增7项）；含真实超时分支的模拟、超时后身份/要求保留、JSON错误状态、简单用量无分析调用、普通/SSE原文取材、未知食材/新话题不误走捷径。
旧测试更新为明确模拟“正常澄清/正常推荐”和“服务异常”，不再把Mock的非法响应当作模型已成功解析。
真实在线效果待用户复测，不承诺任意表达都走本地捷径，也不宣称已实现推荐硬过滤或最终答案事实核验。

回滚：恢复备份的rag_modules并重启backend。
