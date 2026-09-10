# SLAI-tool

面向 **SLAI 账户用户**的 SenseCore 辅助脚本合集。所有运行配置统一使用根目录的 **`config.toml`**，功能代码放在 [`scripts/`](scripts/) 下。

## 启动

安装 uv 后，在仓库根目录执行：

```bash
uv run main.py
```

支持 Linux 和 macOS 的 AMD64/ARM64 架构。uv 会准备 Python 3.11+ 和`tomlkit`、`PyYAML` 依赖，无需手动安装。首次准备运行环境需要联网，SCO 安装包本身随仓库分发。

进入菜单后输入数字：

```text
SLAI-tool
1. 安装并配置 SCO
2. 卸载 SCO
3. CCR 服务
4. CCI 服务
5. DNAT 服务
0. 退出
请选择 [0-5]：
```

操作结束后返回菜单；输入 `0` 退出。

## 统一配置

首次使用可将 [`config.example.toml`](config.example.toml) 复制为 `config.toml`，账户字段可以先留空。选择 **1. 安装并配置 SCO** 时会交互补齐；如果配置文件不存在，该操作会基于模板创建文件。已有配置直接沿用，每次选择操作都会重新读取，无需重启程序。

```toml
[paths]
home = "~/.sco"
data_home = "~/.local/share/sco"
config = "~/.config/sco"

[sco]
access_key_id = ""
access_key_secret = ""
region = ""
zone = "cn-sh-01"
profile = "default"
language = "zh-CN"

[install]
cache_dir = "vendor/sco"

[regions]
cnsh01 = "cn-sh-01"
cnsh02 = "cn-sh-02"
cnyc01 = "cn-yc-01"
```

| 配置项 | 说明 |
| --- | --- |
| `paths.home` | SCO 命令及运行目录，对应 `SCO_HOME` |
| `paths.data_home` | 产品、Region 及组件数据目录，对应 `SCO_DATA_HOME` |
| `paths.config` | SCO 自身保存凭据和 profile 的目录，对应 `SCO_CONFIG` |
| `sco.access_key_id` / `sco.access_key_secret` | SLAI 账户对应的 AccessKey，配置 SCO 时必填 |
| `sco.region` | 有权限访问的 Region code，配置 SCO 时必填；与 zone 分开填写 |
| `sco.zone` | 默认 `cn-sh-01`；留空或省略也使用该默认值 |
| `sco.profile` | 要写入的 profile，默认 `default`；留空时交互补齐 |
| `sco.language` | `zh-CN` 或 `en-US` |
| `install.cache_dir` | 仓库内置安装资源目录，默认 `vendor/sco` |

路径支持 `~`；相对路径以仓库根目录为基准。程序以配置文件中的路径覆盖子进程的同名 SCO 环境变量，确保各操作使用一致的目录。

`config.toml` 已加入 Git 忽略，仓库仅维护不含凭据的模板。请勿强制提交真实凭据；Linux/macOS 可用 `chmod 600 config.toml` 限制读取权限。

## 菜单功能

### 1. 安装并配置 SCO

调用 SenseCore 官方安装器，安装到配置的路径，并执行 `sco version` 检查结果；随后配置 Bash/Zsh 环境。随后自动进入初始化配置：沿用已有账户配置，交互补齐缺失项，执行 `sco init`，成功后统一安装 EIP、CCR。已有 Profile 也遵循这一顺序，每次流程仅在初始化成功后尝试安装一次各组件。安装失败时不继续初始化，初始化失败时不继续安装组件。组件使用 `[sco].region` 指定地区，留空时使用 `cnsh01`；首次下载组件需要联网。

需要 Bash、curl、tar 和 awk。使用随仓库分发的 [官方安装器](https://sco.sensecore.cn/registry/install.sh) 及安装包，缓存完整时无需重新下载。

### 仓库内置安装包

仓库直接通过 Git 分发 `vendor/sco/` 中的官方安装器、元数据和 **SCO v2.0.2（Registry 20260830）** 安装包。克隆仓库即可获得以下四个平台的 launcher、v1 和 v2，共 12 个安装包：

| 系统 | 架构 |
| --- | --- |
| Linux | AMD64 / x86_64 |
| Linux | ARM64 / aarch64 |
| macOS | AMD64 / Intel |
| macOS | ARM64 / Apple Silicon |

安装资源清单见 [vendor/sco/README.md](vendor/sco/README.md)。每个文件均有来源 URL、大小和 SHA-256 记录，安装包还记录官方清单提供的校验值。命中有效缓存时直接复制本地文件，官方安装器继续负责校验和安装。

选择 **1. 安装并配置 SCO** 即可使用本机对应的缓存。缓存完整时 SCO 主程序可以离线安装；所需的 EIP、CCR 组件在 Profile 初始化后安装，首次下载需要联网，失败时会按组件名称报告安装失败，已安装的 SCO 主程序仍会保留。卸载 SCO 不会清理仓库缓存。安装结束的 `sco version` 可能联网检查更新；离线时更新状态不可用，不影响已完成的安装。缓存损坏时会重新下载；失败或中断的下载不会作为完整缓存使用。

默认缓存目录已纳入 Git。若自行更改 `install.cache_dir`，需要将随仓库分发的缓存复制到新目录，否则会重新下载。更新捆绑版本时，应同步替换安装器、元数据、各平台安装包及校验记录；原生 `sco update` 使用官方更新机制，不更新本仓库的缓存。

#### 初始化配置

读取 `config.toml` 的账户信息，执行非交互 `sco init`，完成 AccessKey 校验、profile 保存和官方自带的诊断。“登录”通过这一步完成，无需单独的登录命令。

缺失或留空的 AccessKey ID、AccessKey Secret 会逐项询问，必填项输入为空时会重新询问。Region 缺失时显示编号列表：`1. cn-sh-01 (cnsh01)`、`2. cn-sh-02 (cnsh02)`、`3. cn-yc-01 (cnyc01)`；输入编号后将对应 code 保存到 `sco.region`。选项来自 `config.toml` 的 `[regions]`，初始值依据仓库中的官方地区清单，实际访问权限由后续 `sco init` 校验。密钥输入不回显。zone、profile、language 缺失时也会询问，可回车采用 `cn-sh-01`、`default`、`zh-CN`。已有非空值直接沿用。

所有输入收集完成后，程序将其一次性写回 `config.toml`，保留注释和其他配置，并将文件权限设为仅当前用户可读写；中途取消不保存。随后调用 SCO 执行初始化，如果凭据校验失败，已填写的信息仍会保留供下次使用；兼容入口 `init` 在 SCO 未安装时也会先保存输入。重复执行会重新初始化指定 profile。程序不打印凭据或完整命令；官方接口通过命令参数接收密钥，运行期间系统进程列表可能包含这些参数。

参数依据：[SCO 初始化文档](https://console.sensecore.cn/micro/help/docs/CLI/SCO_Refrence/init)。

### 2. 卸载 SCO

先执行 `sco uninstall --dry-run` 展示将删除的路径，再由用户输入 `yes` 执行 `sco uninstall --yes`，移除 SCO CLI 及其本地配置和数据。其他输入取消卸载。

### 3. CCR 服务

包含“上传镜像”和“列出可访问镜像”，操作结束返回 CCR 子菜单。也可运行 `uv run main.py ccr`。列表范围为当前账号可访问的指定命名空间，**不代表镜像由当前用户创建**；CCR REST 数据未提供创建者字段，不能按 DNAT 的方式筛选创建者。列表使用 REST 查询 SRM 命名空间和 CCR 仓库，不依赖 `sco ccr images list`，展示接口返回的完整镜像地址及标签。

```bash
uv run main.py ccr list --namespace ccr-zhicheng-06
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

### 4. CCI 服务

子菜单提供“创建”和“列出”。进入列表后选择 CCI，再选择停止、复制或删除；操作结束会刷新列表，也可输入 `r` 刷新、`0` 返回。列表只显示当前用户的 CCI，按 IAM 用户 ID 与 `ownership.user_id` 匹配。停止、删除默认取消，执行前再次检查归属和所选实例 UID，执行后分别核实 `SUSPENDED` 状态或目标从列表消失。删除 CCI 不自动删除独立 EIP/DNAT 资源。

复制会读取源实例配置，以新名称提交相同资源池、镜像、启动命令、环境变量、挂载、副本数和调度设置，保留 TCP 等值服务端口；不复制运行状态、资源标识或原 DNAT 绑定。源服务若含当前 CLI 无法完整重建的端口映射，会停止复制并提示。提交前展示模板，默认仅保存权限为 `0600` 的 YAML；副本仍使用相同存储目录。

```bash
uv run main.py cci
uv run main.py cci create
uv run main.py cci list --workspace share-space-01e
uv run main.py cci list --workspace share-space-01e --plain  # 仅打印列表
```

#### 创建 CCI

选择菜单 **4. CCI 服务 → 1. 创建** 或执行 `uv run main.py cci-create`。需要先配置 SCO，流程如下：

1. 从实时云端列表选择工作空间，再选择已关联该工作空间的 ACTIVE 资源池。
2. 调用 `sco aec2 clusters list-workerspec` 列出当前工作空间和资源池支持的规格，编号选择 CPU、内存和加速卡数量组合。
3. 自动使用该规格的可用区及资源池的 VPC；资源池未提供 VPC 时，从同可用区 VPC 列表选择。
4. 从本地带 Registry 地址的镜像标签和 `[cci].image` 默认值中选择，或手填远端镜像。`image` 留空或省略时，默认使用 `registry.cn-sh-01.sensecore.cn/lepton-trainingjob/ngc-pytorch:25.06-cu12.9-py3.12-ubuntu24.04`，镜像选择处回车即可使用。候选不代表镜像已推送或 CCI 有拉取权限。
5. 输入任务名称和副本数，SSH 模式自动生成启动命令并选择本机公钥；默认选择 AI 文件存储，当前可用区只有一个时自动选中，不再显示存储选择列表；有多个时优先使用同可用区的 `afs-share-<可用区末段>`（如 `afs-share-01e`），子目录默认为 `/<当前 IAM 用户名>`，容器挂载路径默认为 `/data`。用户名通过认证接口实时获取，不使用本机用户名。可回车使用默认值、修改路径或选择不挂载；不存在匹配的默认存储时需从列表选择。
6. 调度优先级固定为 `NORMAL`，不再询问；编号选择配额类型；SSH 模式自动开放 22，无需输入端口。配额类型为 `RESERVED` 和 `SPOT`，默认 `RESERVED`，回车即可使用。
7. 选择“附加公网 DNAT”（SSH 模式默认创建新 DNAT，也可选择已有规则或不附加）：选择“创建新 DNAT”或“选择已有 DNAT”。新建时选择同可用区、同 VPC 的 EIP；选择已有时自动汇总所有匹配 EIP 下当前用户的可用规则，直接展示统一规则列表，不再先选择 EIP。每项以 `公网 IP:端口` 开头，随后显示绑定目标和规则名称。新建时从 `20000–65535` 随机选择公网端口作为默认值，避开该 EIP 全部可见规则的端口和端口段（包括其他用户及 TCP/UDP 规则）；可回车使用或手动修改。提交新规则前再次检查端口冲突，避免覆盖已有规则。这里的可用指尚未被规则占用，不代表已验证公网连通性，也不提前预留端口。SSH 模式的容器端口自动设为 `22`，不再询问；已有规则只列出当前用户的 TCP 单端口规则（CREATED/ACTIVE）。未绑定规则直接复用，已绑定规则会显示原目标并单独确认迁移，原目标将失去该公网入口。自动读取当前用户归属信息、检查端口冲突，并将容器端口并入 CCI 应用服务端口。当前支持一条 TCP 单端口映射。
8. 展示完整配置，选择“仅保存配置”或“提交创建”。默认仅保存；配置写入被 Git 忽略的 `.cache/cci/`，权限为 `0600`。如附加 DNAT，同时保存 `.dnat.json` 计划文件；仅保存不会创建任何云资源。

提交时创建 CCI 和应用服务；新规则先创建并确认 CREATED，再绑定实际 CCI UID。已有规则不会重复创建，绑定前重新核对归属、规则 UID 和原目标；如已有绑定，先解绑并确认 CREATED，再绑定新 CCI。最后重新查询检查目标类型、目标标识及端口。迁移绑定失败会明确提示原入口可能已断开，不宣称迁移成功。绑定未验证通过会报告部分完成，不宣称公网可连。历史测试出现过绑定类型为 `UNSPECIFIED`，后续已观察到 `ACTIVE` 与 `CCI_DEPLOYMENT_SERVICE` 正确持久化；这仍不等于 SSH 登录已验证。默认 SSH 模式自动配置 sshd 和公钥；此项启动配置仍需在目标镜像上实际登录验证。绑定失败时已创建的 CCI/规则保留供检查。

列表必须输入有效编号；输入 `q` 或按 Ctrl-C 可取消。列表查询失败、为空或格式异常时停止，不会用猜测的资源继续创建。分页读取 SRM 列表，避免只展示第一页。

CPU、内存、加速卡数量和资源键均自动获取。SCO 的规格表省略了资源键，程序会在内存中解析同一次规格查询的原始响应，按规格名称、可用区和资源数量核对 `device.resource_key`；普通 GPU 和 MIG 使用平台实际返回的值。原始调试输出可能含认证信息，不打印、不落盘。缺失或冲突时停止，不猜测、不要求手填；旧配置中的 `accelerator_key` 不再使用。此适配依赖 SCO v2.0.2 当前的响应日志格式，格式变化会明确报错。

`[cci].ssh_enabled` 默认 `true`。默认镜像按当前 Registry 补全为 `registry.cn-sh-01.sensecore.cn/lepton-trainingjob/ngc-pytorch:25.06-cu12.9-py3.12-ubuntu24.04`。SSH 模式不再询问启动命令和开放端口：自动使用 22，读取 `ssh_public_key` 指定的公钥；留空时从 `~/.ssh/*.pub` 选择，只有一项则自动使用，没有则询问公钥路径。仅上传公钥，私钥留在本机。

启动脚本要求容器以 root 运行；缺少 `/usr/sbin/sshd` 时通过 apt 安装 openssh-server（需要容器能访问软件源），生成实例主机密钥，配置 root 公钥登录并禁用密码认证。使用独立 sshd 配置，不依赖镜像原有 SSH 设置。配置依据 [OpenSSH sshd_config](https://man.openbsd.org/sshd_config)。`command` 留空或为 `sleep infinity` / `sleep inf` 时，以前台 sshd 保持容器运行；自定义命令则在启动 sshd 后执行。`ssh_enabled = false` 恢复普通容器的启动命令和端口交互。DNAT 绑定校验通过后按代理配置输出 SSH 命令，非默认私钥需另加 `-i 私钥路径`。是否可连接仍以实际 SSH 登录为准。

Region/Profile 沿用根配置，选中工作空间后显式传递其订阅和资源组。提交成功仅表示创建请求成功，不代表容器已就绪。

#### SSH 与 SOCKS5 代理

当前实现使用 **Python 读取配置 + Ncat 转发**，没有切换成 Python 自行转发，也不依赖 mihomo。本机需要系统 SSH 和 Ncat；云端容器需要 sshd。

```toml
[cci.ssh_proxy]
server = ""  # 填写 SOCKS5 服务器 IP；留空则直连
port = 1080
username = ""
password = ""
```

SSH 连接提示支持 `[cci.ssh_proxy]` 的 `server`、`port`、`username`、`password`。配置后生成通过项目 `scripts/ncat_proxy.py` 调用 ncat 的 SSH 命令，不修改 `~/.ssh/config`。代理脚本在连接时读取本地 `config.toml`，输出命令不含账号密码；凭据传给 ncat 的进程参数，未宣称对本机进程检查隐藏。macOS 缺少 Ncat 时可执行 `brew install nmap`。参数见 [Ncat 官方代理文档](https://nmap.org/ncat/guide/ncat-proxy.html)。

CCI 创建并验证 DNAT 绑定后，只打印一条可直接执行的 SSH 命令；同一命令也可粘贴到 VS Code 的 `Remote-SSH: Add New SSH Host…`，不再重复输出配置片段。工具仅输出，不启动 SSH、不写入 SSH config，也不自动安装系统软件。缺少 ncat 时按系统提示安装：macOS 使用 `brew install nmap`；Ubuntu/Debian 使用 `sudo apt update && sudo apt install -y ncat`；Fedora/Rocky/AlmaLinux 使用 `sudo dnf install -y nmap-ncat`；Arch 使用 `sudo pacman -S nmap`。Homebrew 的包名是 nmap，不是 ncat。包名参见 [Ubuntu](https://packages.ubuntu.com/noble/ncat)、[Fedora](https://packages.fedoraproject.org/pkgs/nmap/nmap-ncat/)。



代理配置使用 `[cci.ssh_proxy]`；示例文件保留空服务器及凭据，填写后启用代理。生成命令引用本机 Python 和项目脚本的绝对路径，因此可从任意目录执行，也可在本机 VS Code Remote SSH 中使用；移动项目或换电脑后需重新生成命令。

## 直接运行单个功能

也可以跳过菜单，仍使用同一份 `config.toml`：

```bash
uv run main.py install  # 安装并配置 SCO
uv run main.py init     # 兼容入口：仅重新配置并安装组件
uv run main.py uninstall
uv run main.py docker-push
uv run main.py cci-create
```

卸载仍会预览并要求确认。安装会向 `~/.bashrc`、Bash 当前有效的登录配置（依次选已有 `.bash_profile`、`.bash_login`、`.profile`，否则创建 `.bash_profile`）及 Zsh 的 `.zshrc`、`.zprofile` 写入带标记的 SCO 环境块；Zsh 遵循 `ZDOTDIR`。自动导出 `SCO_HOME`、`SCO_DATA_HOME`、`SCO_CONFIG` 并将 `home/bin` 加入 PATH，重复安装更新同一配置块，保留其他内容。打开新终端生效；当前 Bash 可执行 `source ~/.bashrc`，Zsh 可执行 `source "${ZDOTDIR:-$HOME}/.zshrc"`。安装进程无法直接修改父终端环境。

## EIP 命令

### DNAT 规则管理（项目入口）

运行 `uv run main.py dnat` 或菜单 **5. DNAT 服务**，子菜单仅提供“创建”和“列出”。列表自动汇总当前配置下所有可访问 EIP 中由当前用户创建的规则，无需选择 EIP；每项以 `公网 IP:端口` 开头，显示协议、绑定目标、状态和规则名。选中规则后可删除或绑定已有 CCI，默认取消，完成后刷新列表；输入 `r` 刷新、`0` 返回。创建时仍需选择承载新规则的 EIP。也可直接运行：

```bash
uv run main.py dnat list
uv run main.py dnat list --plain  # 仅打印全部自己的规则
uv run main.py dnat create --eip eip-zhicheng-b7763cff --file dnat.json
uv run main.py dnat delete --eip eip-zhicheng-b7763cff --name my-dnat-rule
```

列出规则和删除选择仅显示当前用户创建的规则：通过 IAM `/v1/me` 实时获取当前 AccessKey 对应的用户 ID，再按 `creator_id` 精确匹配，不按所有者或名称猜测。无法确认身份时停止，不回退展示全部规则；创建者为空的规则不会显示。创建端口冲突检查仍覆盖该 EIP 下所有可见规则。

创建模板见 [dnat.example.json](dnat.example.json)。填写新的规则名、真实的创建者/所有者/租户 UUID、外部端口和内部端口；程序从所选 EIP 自动补齐网关 ID、EIP ID、资源路径、可用区，并生成新的规则 UID。已有规则能够提供唯一公网 IP 时自动沿用，否则须填写 `external_ip`。归属 UUID 必须来自当前账户的实际信息，不能复制别人的 ID；缺失归属字段的规则可能创建成功却无法由本人删除，因此程序强制校验。

当前创建功能用于**未绑定目标的 DNAT 规则**，不自动绑定 CCI，也不宣称公网可连。即使未绑定，创建请求的内部端口也必须为有效端口（如 `22`），不能填 `0`。不接受手动设置规则生命周期状态；后端管理 `CREATING → CREATED`。

创建前检查同名和同协议端口/端口段重叠，确认后提交，并重新查询确认 `CREATED` 及归属信息；创建最多轮询 30 次、删除最多 10 次，间隔 2 秒（另加请求耗时），超时提示继续查询，不自动重复提交。删除前展示目标并要求确认，仍绑定资源的规则拒绝删除，必须先解绑。`--yes` 可跳过创建/删除确认；HTTP 错误返回失败，不把旧 EIP 组件的退出码 0 当作成功。列表支持分页；任一 EIP 查询失败会报错，不把部分结果当作完整列表。删除前重新核对创建者、规则 UID 和内容，删除后核实规则确实从列表消失。

该管理入口直接使用官方 EIP HTTP 接口和 HMAC 认证，无需旧 EIP 组件，凭据仅用于请求头，不打印或写入文件。

DNAT 列表新增“绑定 CCI”：汇总同订阅、同可用区、同 VPC 的当前用户实例，选择其已有 TCP 服务端口。没有服务或没有 TCP 端口时停止，不擅自修改 CCI 模板。规则已有绑定时明确提示迁移，确认后先解绑并核实，再绑定；再次检查源规则和目标 CCI 身份，最后核实 ACTIVE、目标类型、UID 和端口。绑定到 22 后输出 SSH 命令，仍需实际登录验证。

### 原生 EIP 组件兼容入口

本机 SCO v2 主程序会拒绝旧 EIP 组件的 `--zone` 参数。项目提供兼容入口，将指定可用区写入权限为 `0600` 的临时 Profile，命令结束后自动清理，原 SCO 配置保持不变。安装组件使用 `sco components install eip`。

在仓库根目录定义以下 shell 函数，订阅和 EIP 按自己的资源调整：

```bash
eip_name=eip-zhicheng-b7763cff
subscription=0197ee17-b6eb-7846-b2b4-a77c5f509b92
rule_name=your-rule-name
eipctl() {
  uv run main.py eip --zone cn-sh-01e "$@" \
    --subscription "$subscription" --resource-group default
}

# 查询 EIP、查询规则列表
eipctl describe "$eip_name" -o json
eipctl dnat list "$eip_name" -o json

# 查询一条规则
eipctl dnat describe "$eip_name" "$rule_name"

# 从 JSON 文件创建规则或绑定目标
eipctl dnat create "$eip_name" "$rule_name" -d "$(cat dnat.json)"
eipctl dnat bind "$eip_name" "$rule_name" -d "$(cat dnat.json)"

# 解绑及删除指定规则
eipctl dnat unbind "$eip_name" "$rule_name"
eipctl dnat delete "$eip_name" "$rule_name"
```

`dnat.json` 是平台的 DNAT 规则对象，不是 CCI YAML。主要映射字段位于 `properties`：`external_ip`、`external_port`、`protocol`、`internal_port`、`internal_instance_name`，以及 EIP/网关标识；目标实例标识须使用平台实际返回的值，不能把显示名称当作 UID。JSON 结构参考[官方创建命令](https://www.sensecore.cn/help/docs/CLI/SCO_Refrence/EIP/instances/create-dnat)。

SSH 对应 `公网IP:外部端口 → CCI:22`，还需要 CCI 应用开放 22 端口、容器运行 sshd 并配置登录公钥。`--ports 22` 不会自动创建 DNAT。当前 EIP 组件提供 EIP 查询和 DNAT 管理，不提供购买/删除 EIP 本身的命令。以上修改命令仅为用法说明，请按目标资源执行。

## 开发与测试

新增功能放入 `scripts/` 并注册到统一菜单；运行参数统一加入 `config.toml`，同步更新模板与文档。

```bash
uv run python -m unittest discover -s tests -v
```

组件安装由 `scripts/cli.py` 的 `REQUIRED_COMPONENTS` 统一维护，当前为 `eip`、`ccr`；之后新增组件加入此列表。初始化成功后统一安装，某个组件失败仍尝试其余组件，最后汇总失败项。


检查范围与结果见 [代码与文档检查记录](docs/AUDIT.md)。
