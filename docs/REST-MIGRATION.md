# REST 请求契约

云资源直接从根配置读取 AccessKey 并生成 HMAC 鉴权。资源路径使用所选工作空间或资源的完整范围。

| 操作 | 请求 |
| --- | --- |
| CCI 创建、查询、删除 | POST apps、GET apps/{name}、DELETE apps/{name} |
| CCI 停止 | POST apps/{name}:stop |
| 容器端口 | 独立 POST services，selector.app 使用 CCI 名称 |
| ACP 创建、查询 | POST trainingJobs、GET trainingJobs/{name} |
| ACP 停止、删除 | POST trainingJobs:batchStop / :batchDelete，每次仅提交选中的名称 |
| DNAT | GET / POST dnatRules，以及已验证的 bind / unbind / DELETE |
| 复制 | GET 原资源，投影可写字段，以新名称创建 |

CCI 应用位于 `cci.<region>.sensecore.cn/compute/cci/data/v2`，Service 位于同一主机的 `compute/service/data/v2`。ACP 位于 `aec2.<region>.sensecoreapi.cn/compute/acp/data/v2`。

CCI 和 Service 分别保存 request_id，创建前检查名称不存在，提交后核对 UID、归属及端口。结果未知时提示刷新检查，不自动重发写入。删除应用后只清理仍属于该应用的 Service；无端口时返回的虚拟空 Service 无需删除。

ACP 的 Worker 使用 total_replicas；复制时去掉 GET 返回的每规格分配副本数，避免与 total_replicas 冲突。停止接口使用 batchStop，等待 SUSPENDED；删除等待 404，期间持续复查 UID。

请求字段投影依据 [本地 schema](../schemas/README.md)。真实写入验证及尚未完成的环节见 [验证记录](LIVE-VALIDATION.md)。
