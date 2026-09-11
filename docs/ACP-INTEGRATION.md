> 2026-09-11：已加入主菜单 ACP 服务，提供创建、本人列表、详情、停止、复制、删除。下文为调研设计记录；当前使用方法以 README 的 ACP 长任务章节为准。

# ACP 调研与接入准备

> 2026-09-11 更新：已进行最小任务实测，发现空 startup_script 不被接受、当前工作空间禁止重启已停止任务。实际结果与修订方案见 [长任务实测记录](ACP-LIVE-VALIDATION.md)；下文保留前次调研背景，冲突处以实测记录为准。

调研日期：2026-09-10。结论：可以开始做 ACP 服务，但应将它作为训练任务服务单独接入，复用 CCI 的资源选择能力，而不是复制 CCI 的容器模板和 SSH 启动脚本。本轮没有提交、启动、停止或删除任何 ACP 任务；完成了官方文档核对、本机 CLI help 检查、当前账号的任务列表及详情只读查询。

## 1. 产品边界与推荐入口

ACP 面向训练作业；CCI 面向长期运行的容器应用。ACP 的资源层次是工作空间 → 已关联的 AEC2 资源池 → Job → Worker。镜像和 AFS 可以复用当前项目的默认选择，但 Job/Worker 的模板与 CCI App/Service 不同。[官方创建指南](https://www.sensecore.cn/help/docs/cloud-foundation/compute/acp/acpUserGuide/acpCreateJob)

建议主菜单新增 `ACP 服务`，子菜单仍保持“创建 / 列出”；在列表选择任务后提供详情、Worker、日志、终端、停止、启动、复制、删除。第一版仅操作当前用户的任务，不默认开启批量操作。这与平台已有的任务管理范围相符。[任务列表](https://www.sensecore.cn/help/docs/cloud-foundation/compute/acp/acpUserGuide/acpJobList)

**本轮只完成接入准备，尚未新增可执行的 ACP 菜单。**

## 2. 本机 CLI 已核实的能力

下表来自当前本机 `sco acp jobs --help` 及各子命令 help；不是推测出来的接口名。

| 操作 | 原生命令 | 接入注意 |
| --- | --- | --- |
| 创建 | `sco acp jobs create` | 创建参数见下一节；不是 CCI YAML |
| 列表 | `sco acp jobs list` | 支持 `--user-id`、分页和 `-o json` |
| 详情 | `sco acp jobs describe JOB_NAME -o json` | 修改前读取并核对归属、UID |
| 复制 | `sco acp jobs copy --copy-job-name SOURCE` | 会直接创建新 Job，必须先预览确认 |
| 停止 | `sco acp jobs stop JOB_NAME` | 提交后轮询 SUSPENDED |
| 启动 | `sco acp jobs start JOB_NAME` | help 描述为启动已停止任务，不当作恢复模型 checkpoint |
| 删除 | `sco acp jobs delete JOB_NAME` | help 标明立即尝试删除；需要确认与结果复查 |
| Worker | `sco acp jobs get-workers JOB_NAME` | 当前 help 的文字提到格式输出，但 flags 未列出 `-o`，需单独验证 |
| 终端 | `sco acp jobs exec JOB_NAME --worker-name WORKER` | 单独的交互子进程，不能用捕获输出的 `Client.read` |
| 日志 | `sco acp jobs stream-logs JOB_NAME` | 可指定 Worker/容器，可 `--follow`，`-o` 是日志文件路径 |

所有命令都需指定 `--workspace-name`，并沿用 profile、region、subscription、resource-group。`clone`、`logs` 不是本机实际子命令名称，应使用 `copy`、`stream-logs`。注意某些未知子命令加 `--help` 只回显父菜单，不能只看退出码判断命令存在。

本机 `sco components list` 的可安装列表中没有 `acp`，但 `sco acp` 已可调用；本机二进制含 ACP 客户端。因此当前版本接入时**不要盲目给 REQUIRED_COMPONENTS 加 acp**。目前仍仅安装 `eip`、`ccr`；ACP 使用启动时能力检查，并随 SCO 版本重新核对。

## 3. 创建参数与项目默认值

此表主要由本机 `jobs create --help` 核对；官方也列有对应参数。[CLI 创建说明](https://console.sensecore.cn/micro/help/docs/CLI/SCO_Refrence/ACP/instances/create/)

| 参数 | 原生语义 | 第一版建议 |
| --- | --- | --- |
| `--workspace-name` | 工作空间 | 实时编号选择 |
| `--aec2-name` | 关联集群；公共集群用 public | 优先已关联专属池，公共池作为明确分支 |
| `--job-name` | 显示名称 | 自动默认值，可编辑 |
| `--name` | 资源名称；不传由后端生成 | 提前生成唯一名称，便于请求超时后查重 |
| `--container-image-url` | 完整镜像地址 | 复用当前 ngc-pytorch 默认镜像 |
| `--training-framework` | pytorch/pt、tensorflow/tf、senseparrots、mpi | 默认 pytorch |
| `--worker-spec` | 规格名称，支持逗号分隔多个 | 第一版只选一种已验证规格 |
| `--worker-nodes` | Worker 数，默认 1 | 默认 1，可输入 |
| `--priority` | NORMAL/HIGH/HIGHEST | 固定 NORMAL，不弹选项 |
| `--quota-type` | CLI 使用小写 reserved/spot | 默认 reserved，用户可选 spot |
| `--command` | 训练启动命令，CLI 默认 sleep inf | 训练模式要求实际命令；sleep 仅作为明确调试模式 |
| `--storage-mount` | 创建 help 为 VOLUME_ID/SUB_DIR:MOUNT_PATH | 默认同区域 AFS、本人目录、/data；仅一项自动选择 |
| `--env` | KEY:VALUE，多项逗号分隔 | 字典输入后验证，含逗号等歧义时改用 REST 模板 |
| `--wait` | 缺配额时是否排队，默认 false | 单独展示，避免无意长时间排队 |
| `--enable-fault-tolerance` | 容错开关 | 第一版默认关闭 |
| `--enable-anomaly-detection` | 依赖容错开关 | 第一版默认关闭 |
| `--retry-times` | 重试次数 | 默认 0；高级策略后续接入 |
| `--vpc-id` / `--az` | public 池必填 | 同可用区 VPC 列表选择 |

拟议配置，不应理解为当前 main.py 已支持：

```toml
[acp]
image = "registry.cn-sh-01.sensecore.cn/lepton-trainingjob/ngc-pytorch:25.06-cu12.9-py3.12-ubuntu24.04"
framework = "pytorch"
worker_nodes = 1
quota_type = "reserved"
command = ""
wait_for_quota = false
retry_times = 0
```

创建前保存权限 0600 的计划文件，展示资源池、单 Worker 规格、Worker 数、镜像、命令、挂载及配额；确认后仅提交一次，按预生成的 name 查回 UID。请求超时不能直接重复创建。

## 4. 已有账号的只读验证

- 对可访问工作空间查询 `jobs list --user-id <当前 IAM ID> --page-size 2 --page-token 1/2 -o json`。
- `share-space-01e` 的两页各返回两条任务；抽样结果的 `ownership.user_id` 均匹配当前用户。详情查询成功。这里只验证样本，不代表已遍历全部任务。
- 另一个工作空间在没有匹配任务时返回提示文字，不是 JSON。因此未来解析器应仅对白名单内的“无任务”响应认定为空；其他非 JSON 输出必须报错。
- CLI JSON 列表是数组，未携带 REST 页码元数据；不能误用 CCI 或 CCR 的返回结构。
- 详情实测有 `ownership`、`framework`、`roles`、`mount`、`scheduling`、`resource_pool`、`ssh`、`state` 及时间字段。
- 实测 role 名为 Worker，包含 `total_replicas`、`image_path`、`startup_script` 和 `resource_spec[]`；规格中含 name、replicas、requests、limits。
- ACP 的 `resource_pool.zone` 与 CCI 的 `resource_pool.available_zone` 不同，不能直接拷贝字段名。
- ownership 是身份判断依据；不能用显示名称、用户名的前缀过滤代替 user_id 精确匹配。

## 5. REST API 的取舍

API 中心已有 TrainingJobService，发布了创建、列表、详情、Worker 列表、批量删除和更新文档。[API 总览](https://console.sensecore.cn/micro/help/docs/API/acp/trainingjobservice-api/)

| REST 能力 | 已核实的契约 | 接入建议 |
| --- | --- | --- |
| 列表 | creator_id 过滤；返回 training_jobs、next_page_token、total_size | 保留分页元数据，适合统一只读 adapter |
| Worker 列表 | 返回 workers、phase、IP、容器资源、分页信息 | 不把 Worker phase 当作 Job state |
| 创建 | roles 结构和高级字段比 CLI 更完整 | 简单单规格可先 CLI，高级模板再 REST |
| 更新 | update_mask 仅支持 display_name、scheduling.priority | 不提供“原地修改镜像/命令”入口 |
| 批量删除 | training_job_names，最多 200 个 | 第一版仍逐项确认、逐项验证 |

[列表 API](https://console.sensecore.cn/micro/help/docs/API/acp/training-job-service-list-training-jobs/)、[Worker API](https://console.sensecore.cn/micro/help/docs/API/acp/training-job-service-list-workers/)、[更新 API](https://console.sensecore.cn/micro/help/docs/API/acp/training-job-service-update-training-job/)、[批量删除 API](https://console.sensecore.cn/micro/help/docs/API/acp/training-job-service-batch-delete-training-jobs/)

REST 路径里的 zone 是工作空间逻辑可用区，例如 cn-sh-01z；不能用 Worker 所在的 cn-sh-01e 替换。列表 total_size 是用于评估后续页数的受限值，不能无条件当作准确全量总数。优先跟随 next_page_token，检测重复及不完整分页。

创建 API 还包含 fault_tolerance、ssh、barrier 等配置，ownership 为输出字段。复制模板需剔除 UID、ownership、state 和时间戳，并通过明确白名单保留可写字段。[创建 API](https://console.sensecore.cn/micro/help/docs/API/acp/training-job-service-create-training-job/)

认证也需单独验证：API 页面标记 Bearer；通用签名文档列出 date、host、@request-target 的 HMAC。当前项目旧 REST helper 使用 x-date 单字段签名，虽在已有服务上可用，不能未经 ACP 实测就宣布通用。不得为了接入 ACP 直接改坏现有 CCI/DNAT/CCR 的签名逻辑。[签名文档](https://console.sensecore.cn/micro/help/docs/ApiDoc/synopsis/)

本轮 CLI 的读查询已验证；完整 REST 访问地址与认证请求尚需单独核实，不能凭产品名拼出一个 sensecoreapi.cn 域名就用于写入。第一阶段推荐 CLI adapter，REST adapter 作为独立后续验证项。停止/启动、终端和日志优先使用已存在的 CLI。

## 6. 文档差异，接入前必须处理

1. 环境变量上限：控制台创建指南写 20，创建 API 写 10。第一版最多 10；更多参数先验证具体接口版本，不硬编码一个未经验证的宽松上限。
2. 重试次数：控制台指南为 -1～99，API backoff_limit 的类型约束更宽，CLI 又提供独立 retry-times。第一版默认 0，高级重试不混为一个已验证开关。
3. `copy --storage-mount` help 与 `create` help 的格式说明不同。第一版复制沿用原挂载；修改挂载前做特定命令验证，不能共享未经核实的字符串编码。
4. API 存在 PYTORCH 与 PYTORCH_DDP 等枚举，但本机 CLI 支持 pytorch/pt 一组输入；第一版按 CLI 支持的值传入，不能将 API 枚举直接放到 CLI。
5. Worker help 提及 JSON 格式，但参数列表未列出输出选项；解析 Worker 列表的方案要单独实测。
6. 内部 SSH 配置挂载路径在用户指南与 API 文档中不同。尊重返回的 ssh.config_mount_path，不覆盖平台维护的文件。

## 7. 多机训练与 SSH 的边界

Worker 数是节点/Pod 数，不是总 GPU 数。第一版总卡数显示为 Worker 数乘以单 Worker 卡数；训练进程数由用户的 torchrun/mpirun 启动命令决定。

ACP 会注入 MASTER_ADDR、MASTER_PORT、SENSECORE_PYTORCH_NNODES、SENSECORE_PYTORCH_NODE_RANK、SENSECORE_ACCELERATE_DEVICE_COUNT 等分布式变量。旧 WORLD_SIZE 在平台语义中可能表示节点数，不能直接等同于 PyTorch 的全局进程数。训练网络参数优先沿用平台注入值，不统一硬编码某组 NCCL 环境变量。[环境变量说明](https://www.sensecore.cn/help/docs/cloud-foundation/compute/acp/acpUserGuide/acpEnvironmentVariable)

ACP 的 ssh 配置是 Worker 之间的互信，不能当作用户本机公网 SSH 入口。调试优先使用 `jobs exec` 或 Worker 终端；不要把 CCI 的 sshd 前台常驻脚本直接替换训练命令，也不要默认给 ACP 绑定 CCI_DEPLOYMENT_SERVICE 类型的 DNAT。[Worker 指南](https://www.sensecore.cn/help/docs/cloud-foundation/compute/acp/acpUserGuide/acpWorkerList)

## 8. 代码拆分与验收计划

拟新增 `scripts/acp.py`（创建、列表菜单）、`scripts/acp_client.py`（命令/响应适配）、`tests/test_acp.py`，不把 ACP 的 roles 模板塞入 cci.py。复用 Client.runtime/scope、workspace/cluster/spec 查询、镜像默认值、AFS 选择和配置写入；AFS 与规格选择可以逐步从 cci.py 抽为共享模块。

第一批验收：

- 只读：本人列表、第一页/第二页/无任务、重复页/异常 JSON、跨工作空间归属、详情和 Worker。
- 模板：单规格、1 Worker、NORMAL、reserved、默认镜像、本人 AFS 子目录，训练命令完整保留引号、分号、逗号和环境变量引用。
- 生命周期：提交前 name 唯一、后端返回 UID；停止确认 SUSPENDED；启动区分等待/排队/运行；删除确认不存在；复制不修改源任务。
- 交互：q/取消无写操作；日志跟随或 Worker 终端使用交互 subprocess；退出不误认为任务停止。
- 经明确选择的最小真实作业：先单 Worker 短命令成功，再验证两 Worker 通信及日志；未完成前不宣称分布式训练已打通。

本轮不把默认镜像 CUDA/驱动兼容、公共池权限、配额容量、Worker 终端连通性或训练成功作为已验证事实。
