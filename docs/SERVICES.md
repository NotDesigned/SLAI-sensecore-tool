# 服务操作参考

[返回 README](../README.md) · [配置参考](CONFIGURATION.md)

## CCI 创建与绑定

选择菜单 **4. CCI 服务 → 1. 创建** 或执行 `uv run main.py cci create`。需要先配置 SCO，流程如下：

1. 使用已保存的默认工作空间（未设置时从云端列表选择），再选择已关联的 ACTIVE 资源池。
2. 调用 `sco aec2 clusters list-workerspec` 列出当前工作空间和资源池支持的规格，编号选择 CPU、内存和加速卡数量组合。
3. 自动使用该规格的可用区及资源池的 VPC；资源池未提供 VPC 时，从同可用区 VPC 列表选择。
4. 从本地带 Registry 地址的镜像标签和 `[cci].image` 默认值中选择，或手填远端镜像。`image` 留空或省略时，默认使用 `registry.cn-sh-01.sensecore.cn/lepton-trainingjob/ngc-pytorch:25.06-cu12.9-py3.12-ubuntu24.04`，镜像选择处回车即可使用。候选不代表镜像已推送或 CCI 有拉取权限。
5. 输入任务名称和副本数，SSH 模式自动生成启动命令并选择本机公钥；默认选择 AI 文件存储，当前可用区只有一个时自动选中，不再显示存储选择列表；有多个时优先使用同可用区的 `afs-share-<可用区末段>`（如 `afs-share-01e`），子目录默认为 `/<当前 IAM 用户名>`，容器挂载路径默认为 `/data`。用户名通过认证接口实时获取，不使用本机用户名。可回车使用默认值、修改路径或选择不挂载；不存在匹配的默认存储时需从列表选择。
6. 调度优先级固定为 `NORMAL`，不再询问；编号选择配额类型；SSH 模式自动开放 22，无需输入端口。配额类型为 `RESERVED` 和 `SPOT`，默认 `RESERVED`，回车即可使用。
7. 选择“附加公网 DNAT”（SSH 模式默认创建新 DNAT，也可选择已有规则或不附加）：选择“创建新 DNAT”或“选择已有 DNAT”。新建时选择同可用区、同 VPC 的 EIP；选择已有时自动汇总所有匹配 EIP 下当前用户的可用规则，直接展示统一规则列表，不再先选择 EIP。每项以 `公网 IP:端口` 开头，随后显示绑定目标和规则名称。新建时从 `20000–65535` 随机选择公网端口作为默认值，避开该 EIP 全部可见规则的端口和端口段（包括其他用户及 TCP/UDP 规则）；可回车使用或手动修改。提交新规则前再次检查端口冲突，避免覆盖已有规则。这里的可用指尚未被规则占用，不代表已验证公网连通性，也不提前预留端口。SSH 模式的容器端口自动设为 `22`，不再询问；已有规则只列出当前用户的 TCP 单端口规则（CREATED/ACTIVE）。未绑定规则直接复用，已绑定规则会显示原目标并单独确认迁移，原目标将失去该公网入口。自动读取当前用户归属信息、检查端口冲突，并将容器端口并入 CCI 应用服务端口。当前支持一条 TCP 单端口映射。
8. 展示关键配置摘要，完整启动命令和参数保存在配置文件中；选择“仅保存配置”或“提交创建”。默认仅保存；配置写入被 Git 忽略的 `.cache/cci/`，权限为 `0600`。如附加 DNAT，同时保存 `.dnat.json` 计划文件；仅保存不会创建任何云资源。

提交时创建 CCI 和应用服务；新规则先创建并确认 CREATED，再绑定实际 CCI UID。已有规则不会重复创建，绑定前重新核对归属、规则 UID 和原目标；如已有绑定，先解绑并确认 CREATED，再绑定新 CCI。最后重新查询检查目标类型、目标标识及端口。迁移绑定失败会明确提示原入口可能已断开，不宣称迁移成功。绑定未验证通过会报告部分完成，不宣称公网可连。历史测试出现过绑定类型为 `UNSPECIFIED`，后续已观察到 `ACTIVE` 与 `CCI_DEPLOYMENT_SERVICE` 正确持久化；这仍不等于 SSH 登录已验证。默认 SSH 模式自动配置 sshd 和公钥；此项启动配置仍需在目标镜像上实际登录验证。绑定失败时已创建的 CCI/规则保留供检查。

编号列表输入 `0` 返回；文字输入可用 `q` 取消，Ctrl-C 退出当前流程。列表查询失败、为空或格式异常时停止，不会用猜测的资源继续创建。分页读取 SRM 列表，避免只展示第一页。

CPU、内存、加速卡数量和资源键均自动获取。SCO 的规格表省略了资源键，程序会在内存中解析同一次规格查询的原始响应，按规格名称、可用区和资源数量核对 `device.resource_key`；普通 GPU 和 MIG 使用平台实际返回的值。原始调试输出可能含认证信息，不打印、不落盘。缺失或冲突时停止，不猜测、不要求手填；旧配置中的 `accelerator_key` 不再使用。此适配依赖 SCO v2.0.2 当前的响应日志格式，格式变化会明确报错。

`[cci].ssh_enabled` 默认 `true`。默认镜像按当前 Registry 补全为 `registry.cn-sh-01.sensecore.cn/lepton-trainingjob/ngc-pytorch:25.06-cu12.9-py3.12-ubuntu24.04`。SSH 模式不再询问启动命令和开放端口：自动使用 22，读取 `ssh_public_key` 指定的公钥；留空时从 `~/.ssh/*.pub` 选择，只有一项则自动使用，没有则询问公钥路径。仅上传公钥，私钥留在本机。

启动脚本要求容器以 root 运行；缺少 `/usr/sbin/sshd` 时通过 apt 安装 openssh-server（需要容器能访问软件源），生成实例主机密钥，配置 root 公钥登录并禁用密码认证。使用独立 sshd 配置，不依赖镜像原有 SSH 设置。配置依据 [OpenSSH sshd_config](https://man.openbsd.org/sshd_config)。`command` 留空时，以前台 sshd 保持容器运行；自定义命令则在启动 sshd 后执行。`ssh_enabled = false` 恢复普通容器的启动命令和端口交互。DNAT 绑定校验通过后按代理配置输出 SSH 命令，非默认私钥需另加 `-i 私钥路径`。是否可连接仍以实际 SSH 登录为准。

Region/Profile 沿用根配置，选中工作空间后显式传递其订阅和资源组。提交成功仅表示创建请求成功，不代表容器已就绪。

## CCR 镜像管理

包含“上传镜像”和“列出可访问镜像”，操作结束返回 CCR 子菜单。也可运行 `uv run main.py ccr`。列表范围为当前账号可访问的指定命名空间，**不代表镜像由当前用户创建**；CCR REST 数据未提供创建者字段，不能按 DNAT 的方式筛选创建者。列表使用 REST 查询 SRM 命名空间和 CCR 仓库，不依赖 `sco ccr images list`，展示接口返回的完整镜像地址及标签。

```bash
uv run main.py ccr list --namespace your-namespace
uv run main.py ccr upload
```

CCR 仓库列表分页使用 `pageSize/pageToken` 和 `totalSize/nextPageToken`。实测服务端可能忽略请求的页大小，一次返回全部仓库；较大命名空间查询较慢，因此读取超时为 120 秒。错误不会当作空列表，返回不完整或分页重复会报错。上传继续使用下面的 Docker 流程。

通过 Docker 将本地镜像推送到 SenseCore 容器镜像服务（CCR）。需要已安装并启动 Docker、本地已有源镜像，且账户具备目标命名空间的 `ccr.namespace.operate` 权限。

在 `config.toml` 中配置：

```toml
[docker]
registry = "registry.cn-sh-01.sensecore.cn"
username = ""
namespace = ""
source_image = "my-app:local"
image_name = "my-app"
tag = "latest"
```

每次上传都会询问命名空间、源镜像、目标镜像名和标签，配置中的上次使用值作为回车默认值，修改后保存。Registry 地址沿用配置。

程序读取 Docker 的 `auths`、`credHelpers` 或 `credsStore`，检查该 Registry 是否有已保存凭据。有凭据时跳过账号密码询问；没有时才询问用户名和隐藏输入的密码。若推送明确返回认证失败，会重新询问并重试一次；其他推送错误直接停止。

使用 CCR 页面的**客户端登录密码**，不是 SCO AccessKey Secret。密码由 Docker 管理，不再写入 `config.toml`；旧配置中的 `password` 字段不再使用。登录检测遵循 [Docker 凭据存储机制](https://docs.docker.com/reference/cli/docker/login/#credential-stores)。

例如命名空间填写 `my-project`，上面的配置会推送到 `registry.cn-sh-01.sensecore.cn/my-project/my-app:latest`。程序检查本地镜像，仅在需要时执行 `docker login --password-stdin`，然后执行 `docker tag` 和 `docker push`。密码通过标准输入传给 Docker，不放入命令参数；登录凭据由 Docker 按本机凭据存储配置管理。

后续上传其他镜像时，重新选择菜单项并输入本次镜像信息即可。目标标签已有镜像时，推送可能更新该标签指向。

## DNAT 规则管理

运行 `uv run main.py dnat` 或菜单 **5. DNAT 服务**，子菜单仅提供“创建”和“列出”。列表自动汇总当前配置下所有可访问 EIP 中由当前用户创建的规则，无需选择 EIP；每项以 `公网 IP:端口` 开头，显示协议、绑定目标、状态和规则名。选中规则后可查看详情、绑定已有 CCI、解绑或删除，修改操作默认取消，完成后刷新列表；输入 `r` 刷新、`0` 返回。创建时仍需选择承载新规则的 EIP。也可直接运行：

```bash
uv run main.py dnat list
uv run main.py dnat list --plain  # 仅打印全部自己的规则
uv run main.py dnat create --eip your-eip
uv run main.py dnat delete --eip your-eip --name my-dnat-rule
```

列出规则和删除选择仅显示当前用户创建的规则：通过 IAM `/v1/me` 实时获取当前 AccessKey 对应的用户 ID，再按 `creator_id` 精确匹配，不按所有者或名称猜测。无法确认身份时停止，不回退展示全部规则；创建者为空的规则不会显示。创建端口冲突检查仍覆盖该 EIP 下所有可见规则。

创建时交互填写规则名、公网端口、内部端口和协议；公网端口默认随机避开当前 EIP 的全部可见规则。创建者、所有者与租户从当前身份读取，网关、EIP 标识和可用区从所选资源补齐。没有唯一可推导公网 IP 时询问 IP。完整计划保存到 `.cache/dnat/`，确认后才提交。

当前创建功能用于**未绑定目标的 DNAT 规则**，不自动绑定 CCI，也不宣称公网可连。即使未绑定，创建请求的内部端口也必须为有效端口（如 `22`），不能填 `0`。不接受手动设置规则生命周期状态；后端管理 `CREATING → CREATED`。

创建前检查同名和同协议端口/端口段重叠，确认后提交，并重新查询确认 `CREATED` 及归属信息；创建最多轮询 30 次、删除最多 10 次，间隔 2 秒（另加请求耗时），超时提示继续查询，不自动重复提交。删除前展示目标并要求确认：已绑定规则会明确提示“解绑并删除”，先核实解绑完成再删除；解绑未确认时停止，不继续删除。也可单独选择“解绑”，保留规则及端口。`--yes` 可跳过创建/删除确认，删除已绑定规则同样执行先解绑后删除；HTTP 错误返回失败，不把旧 EIP 组件的退出码 0 当作成功。列表支持分页；任一 EIP 查询失败会报错，不把部分结果当作完整列表。删除前重新核对创建者、规则 UID 和内容，删除后核实规则确实从列表消失。

官方 EIP CLI 当前提供 create/list/describe/bind/unbind/delete，没有独立的 update 命令；本项目没有据此猜测端口修改接口。详情和解绑能力见[官方 EIP 命令总览](https://www.sensecore.cn/help/docs/CLI/SCO_Refrence/EIP/overview)。

该管理入口直接使用官方 EIP HTTP 接口和 HMAC 认证，无需旧 EIP 组件，凭据仅用于请求头，不打印或写入文件。

DNAT 列表新增“绑定 CCI”：汇总同订阅、同可用区、同 VPC 的当前用户实例，选择其已有 TCP 服务端口。没有服务或没有 TCP 端口时停止，不擅自修改 CCI 模板。规则已有绑定时明确提示迁移，确认后先解绑并核实，再绑定；再次检查源规则和目标 CCI 身份，最后核实 ACTIVE、目标类型、UID 和端口。绑定到 22 后输出 SSH 命令，仍需实际登录验证。

## ACP 长任务

参见 [ACP 创建与管理](../README.md#acp-长任务)。菜单包含创建与列表，实例操作集中在列表中。可直接运行 `uv run main.py acp create --workspace <名称>`，或 `uv run main.py acp list --name <任务名前缀>`。

CCI 与 ACP 的创建确认页只显示关键配置摘要，完整配置保存于 `.cache/cci/` 或 `.cache/acp/`；默认仅保存，选择提交后才创建云资源。复制计划保留源配置以便复核。ACP 列表的“详情”可查看完整云端返回。
