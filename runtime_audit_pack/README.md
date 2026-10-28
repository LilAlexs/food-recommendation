# 运行库只读核验与候选采集

本包应在用户已运行的 backend 容器执行。数据库只读，不调用LLM，不创建/重建/加载向量集合，不改服务主流程。向量候选采集会使用现有Embedding模型，占用CPU；若缓存没有模型，可能尝试下载。请在知识库不发生更新时执行。

## Windows PowerShell

解压本包为项目根目录下的 runtime_audit_pack 文件夹。在项目根目录执行：

```powershell
docker cp .\runtime_audit_pack what-to-eat-backend:/tmp/runtime_audit_pack
docker exec -w /app what-to-eat-backend python /tmp/runtime_audit_pack/collect_runtime.py --output /tmp/runtime_audit_result --collect-vector --candidate-k 30
docker cp what-to-eat-backend:/tmp/runtime_audit_result .\runtime_audit_result
Compress-Archive -Path .\runtime_audit_result -DestinationPath .\runtime_audit_result.zip
```

把 runtime_audit_result.zip 发回即可。不需要上传 .env、API key、数据库密码、数据库端口或整个数据库备份。

请注意：如果第二条命令返回非零，仍执行 docker cp，将部分结果带回。不要以相同输出目录重跑；重试时使用新的 /tmp/runtime_audit_result2 和本地目录，避免覆盖。

## 如果Embedding模型加载失败

先只核验运行库，不运行Embedding：

```powershell
docker exec -w /app what-to-eat-backend python /tmp/runtime_audit_pack/collect_runtime.py --output /tmp/runtime_audit_only
docker cp what-to-eat-backend:/tmp/runtime_audit_only .\runtime_audit_only
Compress-Archive -Path .\runtime_audit_only -DestinationPath .\runtime_audit_only.zip
```

这份结果也有用。脚本不读取或输出API密钥；导出包含知识库文档、菜谱节点和关联属性，请自行检查是否有私有内容再上传。向量字段不导出。

## 输出用途

- runtime_manifest.json：各阶段成功/失败、题集指纹、实际集合、模型声明与候选数量。
- source_verification.jsonl：容器里的源文档是否与审核快照一致，附实际内容。
- runtime_aliases.json、negative_source_audit.json：运行目录别名和未收录名称检查。
- graph_recipes.jsonl、graph_recipe_edges.jsonl：运行图谱的菜谱与出边；后续用于身份、关系及否定名称核查。
- milvus_schema.json、milvus_documents.jsonl：实际集合字段与完整非向量文档导出；后续用于ID映射、BM25语料和相关性标注。
- vector_candidates.jsonl（可选）：60题各最多30个原问题向量候选、排名分数和耗时。COSINE越大越相似。

向量结果是独立原问题候选，不是完整系统F结果，也不是B-E方案结果。固定历史不会注入原问题向量检索。这一轮用于运行核验与候选池起步；后续需要补入BM25、重写候选等再进行最终pool判定，不能现在就宣布全库Recall标注齐全。

Milvus使用query_iterator导出全量，不使用可能截断的固定limit。若当前pymilvus版本缺少该接口，会明确标记失败，不会把部分数据当完整导出。仅做离线模拟验证，尚未在用户运行环境执行。
