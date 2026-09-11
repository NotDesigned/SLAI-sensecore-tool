# 创建请求字段

`cci.json`、`acp.json` 提取自 2026-09-11 官方页面嵌入的 OpenAPI 请求 schema。`scripts/templates.py` 用其递归移除只读字段并拒绝未识别的非空字段，避免复制时静默丢失配置。

- [CCI 创建接口](https://console.sensecore.cn/micro/help/docs/API/cci/app-service-create-app/)
- [ACP 创建接口](https://console.sensecore.cn/micro/help/docs/API/acp/training-job-service-create-training-job/)

ACP 创建不能同时提交 total_replicas 和 GET 返回的每规格 replicas；此约束由实际写入验证并在复制投影后处理。服务端将来增加可写字段时，应更新 schema 后再允许复制。

ACP 查询响应还可能带有 `metadata`，但官方创建和更新接口均未声明此字段。复制时明确提示并排除该字段，不将其加入创建请求；其他未知的非空字段仍会阻止复制。
