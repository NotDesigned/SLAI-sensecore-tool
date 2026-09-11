# ACP 长任务接入：实测与设计结论

> 本文件保留接入阶段的实验和设计记录；重构后的当前验收见[真实使用验收](LIVE-VALIDATION.md)，当前用法见 README。

日期：2026-09-11。基于用户授权进行了独立小任务测试；没有修改用户原有训练任务，也没有挂载用户数据目录。测试使用现有工作空间与 debug-mig 资源池，配额类型 RESERVED，优先级 NORMAL，单 Worker、重试次数 0。

## 结论

ACP 的显式启动命令、创建、复制、正常结束、停止路径已经获得实际证据；日志读取与通用重启流程不能按原先假设实现。最重要的是：**不能通过省略或清空 ACP 启动脚本来保留 Docker Entrypoint。** 原生 CLI 会使用默认 `sleep inf`，显式空脚本又会被 CLI 和后端拒绝。

建议保留用户提出的两种启动方式，但把问题明确为：

```text
镜像是否已内置完整任务启动逻辑（Entrypoint / CMD）？
1. 使用镜像启动逻辑
2. 输入任务命令
0. 返回
```

这里不预选“使用镜像启动逻辑”，不能因为镜像包含一个 Entrypoint 就认定它包含训练程序。默认 NGC 镜像就是反例。

## 1. 实际执行的实验

| 实验 | 实际行为 | 结论与边界 |
| --- | --- | --- |
| CPU 显式命令 | 创建单 Worker、2 CPU、4 GiB 规格任务；脚本打印标记、运行 Python、短暂等待 | 创建成功，Worker 可列出；停止前 Job 为 STARTING、Worker 为 Running，未据此声称脚本已执行完毕 |
| 停止 | 对本轮 CPU 任务执行 stop | 观察到 SUSPENDING → SUSPENDED，停止已验证 |
| 重新启动 | 对该 SUSPENDED 任务执行 start | 退出码 70、HTTP 400，错误原因 wsImmutableJobEnabled；当前工作空间不允许直接重启已停止任务 |
| CLI 空命令 | copy 时传入 `--command ''` | 退出码 65，提示 startup script is mandatory；未创建新任务 |
| REST 空脚本 | 对官方创建路径提交 roles[].startup_script 为空的最小任务 | HTTP 400，错误原因 tjRequiredStartupScript；证明限制也存在于后端 |
| MIG 显式命令 | 复制本轮任务，换为 1 个 MIG 规格、2 CPU / 32 GiB；命令包含 nvidia-smi、Python 标记、sleep 30 | 复制成功，Job 进入 RUNNING，随后 SUCCEEDED；验证到任务完成，但未取得日志内容，不能声称核对过每一行输出 |
| 显式执行 Entrypoint | 复制本轮 MIG 任务，启动脚本为 `exec /opt/nvidia/nvidia_entrypoint.sh` | 任务很快结束为 SUCCEEDED，没有长任务；说明此镜像默认入口不能替代训练命令 |
| 日志 | stream-logs 直连、通过 SOCKS5、不跟随、显式 follow 均做了尝试 | 直连超时；经代理非 follow 仅出现调度成功信息；follow 未取得日志。日志功能尚未验收 |
| 清理 | 仅对本轮记录的 3 个已确认 UID、同一创建者的任务执行批量删除 | 清理结果见本文件末尾；未删除任何原有任务 |

这些是生命周期和最小命令实验，不是模型训练、checkpoint 恢复、多机通信或吞吐测试。尚未验证交互 Worker 终端。

## 2. Docker Entrypoint 的具体结论

读取默认镜像配置得到：

```text
Entrypoint: ["/opt/nvidia/nvidia_entrypoint.sh"]
Cmd: null
WorkingDir: /workspace
```

对应镜像：`registry.cn-sh-01.sensecore.cn/lepton-trainingjob/ngc-pytorch:25.06-cu12.9-py3.12-ubuntu24.04`。

其 Entrypoint 是 NVIDIA 初始化入口；显式执行该入口、不给任务参数的 ACP 测试会直接结束。因此创建长任务时，应判断的是“镜像是否内置要执行的程序”，而非只检查 Entrypoint 字段是否非空。

拟议实现：

1. 用户选择“使用镜像启动逻辑”后，读取镜像 Entrypoint 和 Cmd 数组，展示最终启动程序。
2. 按 Docker 参数顺序合并 Entrypoint + Cmd，生成非空 ACP 启动脚本，例如 `exec python /app/train.py --config /app/train.yaml`。参数逐项做 shell 引号处理，不将数组粗暴拼成可二次展开的字符串。
3. 镜像无 Entrypoint 但有 Cmd，也可以使用 Cmd；两者都为空则必须输入任务命令。
4. 如果只能读到初始化入口而看不到明确任务参数，不自动补 `sleep infinity`，要求用户确认该入口本身是否会运行完整任务，或改为输入命令。
5. 获取镜像配置失败时给出明确错误或允许手工指定完整启动程序，不猜测 `/docker-entrypoint.sh` 之类的路径。
6. 该方案是**显式重建启动 argv**，并非已经证明 ACP 原生保留 Docker Entrypoint 的全部语义；WorkingDir、环境变量及框架包装还需要后续对照测试。

本轮 Docker 远端配置查询遇到对象存储 HTTP 重定向返回 403；单独通过 HTTPS 读取相同配置对象成功。不要把本机 Docker metadata 查询失败直接解释成云端无法拉取镜像；也不应把临时签名 URL 保存到公开文档或日志。

## 3. 长任务的生命周期策略

当前工作空间启用了不可变任务策略。拟议菜单应把“启动”作为受工作空间能力约束的操作，而不是默认承诺可重启所有 SUSPENDED 任务。

- 长任务停止后，默认路径为“复制为新任务”，并在命令中显式使用 checkpoint 恢复参数。
- 复制仅复用模板，不等于恢复优化器状态、随机数状态或数据进度；这些由训练代码和持久化 checkpoint 决定。
- 为每次提交预生成唯一 name，提交后保存 UID、镜像引用、规格、命令、AFS 路径和时间。发生超时先按 name 查回，不重复 POST。
- 使用 AFS 保存代码、数据、日志和 checkpoint；本轮未测试 AFS 写入，不声称其权限与恢复流程已验收。
- 普通长任务启动命令应以前台进程为主，优先 `exec` 训练程序，不使用 SSH 守护进程替代主任务，也不默认后台运行训练后 `sleep`。
- 重试策略默认 0，再单独开放有限次数重试；是否能够安全重试取决于训练程序的 checkpoint 与幂等性，不能默认无限重启。

## 4. 控制 API 与日志通路

本轮从官方 API 页的结构化数据核实了访问地址，并成功进行一次 HMAC 列表查询：

```text
https://aec2.cn-sh-01.sensecoreapi.cn
/compute/acp/data/v2/subscriptions/{subscription_name}/resourceGroups/{resource_group_name}/zones/{workspace_zone}/workspaces/{workspace_name}/trainingJobs
```

创建使用 POST，列表使用 GET。路径中的 zone 是工作空间逻辑可用区，不是 Worker 所在可用区。后续直接 REST 查询也出现过超时；设置本轮 SOCKS5 代理后，CLI 列表查询可取得最终状态。应将网络路由纳入客户端配置，而不是在遇到超时时自动改写任务状态。

当前项目 x-date HMAC helper 在这条列表请求和空脚本创建验证中可用；这只是本环境的实际结果，不代表所有未来 API 都接受同一种签名。

日志不能用“CLI 返回 0”当作验收标准。本轮经代理的非 follow 调用返回 0，但内容只有任务已调度，没有应用日志。第一版应分别处理：历史日志读取、实时跟随、连接超时、任务终态，以及退出跟随不停止任务。

官方参考：[创建接口](https://console.sensecore.cn/micro/help/docs/API/acp/training-job-service-create-training-job/)、[CLI 创建](https://console.sensecore.cn/micro/help/docs/CLI/SCO_Refrence/ACP/instances/create/)、[API 列表](https://console.sensecore.cn/micro/help/docs/API/acp/training-job-service-list-training-jobs/)。

## 5. 接入顺序

第一阶段：创建与本人列表、详情、停止、复制、删除；创建支持“镜像启动逻辑 / 显式命令”两种模式，严格记录实际非空 startup_script。沿用 NORMAL、RESERVED、单 Worker 默认，规格与挂载实时选择。

第二阶段：独立解决日志读取和跟随的通路，完成真实日志标记验证，再开放 Worker 终端。控制面成功不替代这些验收。

第三阶段：选择用户指定的真实训练脚本，用短训练验证 AFS checkpoint、复制恢复、失败退出码，再扩展多 Worker 和容错配置。

本轮没有把 ACP 菜单提前加入产品代码。接入不能直接沿用旧方案中“已停止就能 start”“空命令保留入口”这两个假设。

## 清理结果

2026-09-11 已对本轮 3 个测试任务执行删除，CLI 返回成功。随后按本轮唯一任务名前缀重新查询，返回 `No jobs found`。两次空脚本请求均被拒绝，未留下对应新任务。测试范围内没有保留运行资源。


## 2026-09-11 菜单接入实测

新增 `uv run main.py acp` 和主菜单第 6 项。通过新建流程生成并提交 `slai-acp-menu-0911-a`，使用 `n6lv.n.i10.1.2c32g`（单 MIG、2 CPU、32 GiB），不挂载用户存储，默认 NGC 镜像。

启动命令为 `set -eu; python -c 'import torch; assert torch.cuda.is_available(); print("ACP_MENU_OK")'; sleep 30`。已观察到 RUNNING → SUCCEEDED；列表与 describe 的资源规格、命令一致。成功状态证明这次断言未使任务失败，但未另行取得日志文本，也不是完整训练性能验证。

网络实测存在波动：CLI `--user-id` 和 REST `creator_id` 均能返回本人任务，但部分请求超过 45 秒。读取超时调整到 90 秒；创建查重仅按新任务名前缀查询。ACP 默认使用系统网络，`use_ssh_proxy=true` 可选复用 SOCKS5，不强制改变资源目录查询网络。

同时核对 CCI 官方 ListAppsOwn 接口，将列表从 `/apps` 改为 `/appsOwn`。旧接口扫描共享工作空间全部 2261 条记录，即使传 `owner_id` 仍返回全量；新接口一次返回本人 10 条。旧报错本次未稳定复现：其触发条件是记录缺少名称或扫描中出现重复名称。已缩小查询范围并拆分错误提示，仍保留本地身份核验。

来源：[ListAppsOwn](https://console.sensecore.cn/micro/help/docs/API/cci/app-service-list-apps-own/)、[ListApps](https://console.sensecore.cn/micro/help/docs/API/cci/app-service-list-apps/)。


分页修复补充：ACP 全量查询期间创建副本时触发过重复检测，待列表稳定后扫描 5 页、452 条未发现重复名称。这与按页码分页时新增记录导致边界移动相符。CCI 与 ACP 均改为按 UID 去重，允许页间部分重叠；完全重复的页仍拒绝，不能保证动态分页具备数据库快照一致性，需要刷新获取最新状态。CCI 原始报错的具体重复项未捕获，不能据此断言当次后端一定返回了重复。


菜单生命周期验收：`slai-acp-menu-0911-copy` 从列表复制后达到 SUCCEEDED；原任务与副本均经列表删除并确认消失。另以相同最小规格提交 `slai-acp-menu-0911-stop`，命令尾部改为 `sleep 300` 留出停止窗口，通过同一操作函数确认 RUNNING → SUSPENDING → SUSPENDED。未修改任何用户原有任务。修复后的全量本人列表实测返回 452 条（当时包含测试记录）。

最终清理：3 个 `slai-acp-menu-0911-*` 测试任务均已删除，菜单按此前缀查询返回 0 条；无测试算力遗留。自动测试共 158 项，通过 156 项，跳过 2 项原生 Windows 测试。AFS 挂载和日志流未在本次菜单实测中验证。
