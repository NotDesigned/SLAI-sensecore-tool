# SLAI-tool

面向 SLAI 账户的 SenseCore 管理工具。通过 REST 管理云资源，终端界面使用 Textual。

| 服务 | 支持的操作 |
| --- | --- |
| CCI | 创建、按照上次配置创建、浏览、连接、停止、复制、删除、附加 DNAT |
| ACP | 创建、按照上次配置创建、浏览、详情、复制、停止、删除 |
| DNAT | 创建、浏览、详情、绑定 CCI、解绑、删除 |
| CCR | 上传镜像、浏览和搜索可访问镜像 |

## 开始使用

安装 Python 3.11+ 和 [uv](https://docs.astral.sh/uv/getting-started/installation/)，在项目目录运行：

```sh
uv sync
uv run main.py
```

首次打开会看到“欢迎使用 SLAI-tool · 首次使用 1/2”。点击 **开始设置**（文本菜单输入 a），按顺序完成：

1. 在 SenseCore 控制台右上角头像菜单打开“AccessKey 访问密钥”，创建并保存 ID 和 Secret；这是 API 密钥，不是网页登录密码。[官方说明](https://www.sensecore.cn/help/docs/ApiDoc/synopsis)
2. 在工具里输入这两项，Secret 输入隐藏。验证成功后自动生成根目录 `config.toml`，不用手动复制文件。
3. 选择默认工作空间。若没有选项，请联系 SLAI 管理员授权；取消此步后，下次启动会提示继续第二步。

设置后，交互调试选 **CCI**，长任务选 **ACP**。进入服务列表后点击“创建”。首页的 **使用指南** 随时解释服务用途和所需工具。高级用户也可复制 [config.example.toml](config.example.toml) 后填写。配置和本地计划均不进入 Git。

选择 顶部的 **工作空间** 按钮后，CCI 和 ACP 默认使用它。标题显示当前用户名和工作空间；操作时仍会校验资源是否可访问。

顶部是 **配置账户 / 工作空间 / 配置 SOCKS5 / 使用指南** 按钮；首次使用时另显示“开始设置”。代理状态栏显示本次握手和认证的结果及检测时间，可点击“检测代理”重测。

主列表只显示服务：

```text
1. CCR · 镜像管理
2. CCI · 交互调试
3. DNAT · 连接入口
4. ACP · 长任务
0. 退出
```

文本菜单用 a / w / p 进入账户、工作空间、代理配置，r 检测代理，h 查看指南。

支持 Windows、macOS 和 Linux。完整安装说明见 [运行环境](docs/INSTALLATION.md)。

## 界面操作

方向键移动，Enter 打开或编辑，0 / Esc 返回。文字输入中 0 是普通字符，Esc 取消；多行命令用 Ctrl+S 保存。

进入服务直接打开列表，创建、上传和“按照上次配置”是列表页上的独立按钮。CCR 先显示可访问命名空间，选中后浏览镜像。

列表每页 20 条，←→ 翻页，`/` 搜索，Enter 查询，`r` 刷新。选中资源后进入操作。查询在后台进行，等待时可以返回。详情的“复制全部”写入系统剪贴板；无法使用剪贴板时可“保存文本”后打开复制。

不使用全屏界面时运行 `uv run main.py --text`；列表加 `--plain` 可直接打印结果。

## CCI 创建与 SSH

**CCI → 创建**会打开可编辑表单，集中显示资源池、规格、镜像、SSH 公钥、存储和 DNAT。默认主按钮为“提交创建”，校验通过后直接提交；旁边的“仅保存配置”只保存草稿，不创建资源。

- 优先级固定 NORMAL，配额默认预留资源。
- CPU、内存和加速卡资源键从实际规格读取。
- AFS 默认挂载 `/<当前 IAM 用户名>` 到 `/data`；唯一存储自动选中。
- SSH 默认开启，使用本机公钥登录 root，容器端口为 22。只上传公钥，私钥留在本机。
- 镜像字段可以搜索 CCR 命名空间中的镜像名或标签，选中自动填入完整地址；默认镜像也可直接使用。
- DNAT 可以新建、复用已有规则或不附加。新端口随机避开所有可见占用；迁移已有绑定会单独确认。

保存过创建配置后，再次进入服务会显示“按照上次配置”。

**CCI → 按照上次配置创建**会回填上次通过检查并保存的选项。资源会重新校验，名称自动更新；若上次附加 DNAT，会在同一 EIP 上创建新规则和新端口，不迁移旧实例的入口。无需修改时直接点击“提交创建”。上次配置按账号和工作空间隔离，保存在 `[cci.last]`。

默认镜像为预装 sshd 的 `ccr-zhicheng-02/slai-cci-pytorch-ssh:25.06-20260911`，已验证 CCI 登录；需要该命名空间的拉取权限。默认 SSH 配置包含服务就绪检查。自定义镜像若缺少 sshd，启动脚本仍会尝试通过 apt 安装；预装镜像的构建说明见 [CCI 镜像](images/cci/README.md)。复制现有 CCI 保留其原模板和存储，不自动迁移 DNAT。

CCI 列表中选择运行中的实例，会检查同可用区、同 VPC 的 DNAT；存在绑定的 TCP 单端口入口时显示“连接”。多个入口可按 IP:端口选择，默认优先容器端口 22。点击后重新核对绑定、检测 SSH 响应，再显示命令。自定义端口需要容器实际提供 SSH 服务。

工具输出一条 SSH 命令：终端直接执行；VS Code 中先用 **Remote-SSH: Add New SSH Host…** 粘贴保存，再通过 **Connect to Host…** 选择主机。整条命令不能填进只接受 `user@host` 的输入框。非默认私钥可另加 `-i 私钥路径`。

### SLAI 内网代理

点击首页 **配置 SOCKS5**，填写 IP、端口和认证信息，也可关闭代理。密码隐藏输入；同一代理可保留已保存的密码，切换地址或用户名需重新输入。SSH 使用保存的代理，ACP 保持原来的独立开关；关闭代理时也关闭 ACP 代理。

启动和保存配置后自动检测，手动“检测代理”可重测。状态只验证代理的 SOCKS5 握手与认证，不代表某个 CCI 已可达；连接 CCI 时另行检查 SSH 响应。该检测不依赖 Ncat。

也可以在根 `config.toml` 中填写：

```toml
[network.socks5]
server = ""
port = 1080
username = ""
password = ""
```

留空则 SSH 直连；填写后，Python 代理脚本读取配置并调用 Ncat 转发。命令中不包含代理密码。未找到 Ncat 时会自动尝试系统安装命令；缺少权限或包管理器时显示可复制的手动命令：

| 系统 | 命令 |
| --- | --- |
| macOS | `brew install nmap` |
| Ubuntu / Debian | `sudo apt install ncat` |
| Windows | `winget install --id Insecure.Nmap -e` |

Homebrew 包名是 **nmap**。Windows 的生成命令使用 PowerShell 7.3+。SSH 命令引用本机 Python 和项目绝对路径，移动项目后需重新生成；VS Code 也需要能找到 Ncat。

输出命令前会检查 SSH 协议响应，最多约 8 秒。直连超时时，会提示在 `config.toml` 中添加 SOCKS5 代理并给出字段模板；已配置代理时，会提示检查现有代理。全屏界面也会单独显示检测失败提示。失败时可能不在 SLAI 内网，也可能是实例、sshd 或 DNAT 尚未就绪；检查不进行认证登录。代理类型是 SOCKS5。`network.acp_proxy = true` 可让 ACP REST 请求使用同一代理；其他管理接口使用系统网络。

## ACP 长任务

ACP 使用同样的可编辑表单，支持搜索镜像和选择资源。任务命令必须明确填写：使用镜像入口时，填写入口程序及其参数，例如 `/opt/nvidia/nvidia_entrypoint.sh python /data/train.py`。省略命令不能保留 Docker Entrypoint，默认 NGC 入口也不会自行开始训练。

默认单 Worker、闲时资源、NORMAL、重试次数 0。列表仅显示当前用户任务，支持服务端名称前缀、状态筛选及按需分页。选择任务后可查看详情、复制、停止或删除。复制是新任务，checkpoint 恢复参数由训练命令负责；当前不提供原地重启。

ACP 也提供“按照上次配置”按钮：恢复镜像、启动方式、命令、资源及挂载，核对后可以直接提交。入口程序和参数就是唯一的任务命令，不需要再填写第二份启动命令。

## 本地镜像与 CCR、DNAT

CCI / ACP 的镜像字段可选择本地 Docker 镜像，包括没有远端地址的标签。选择命名空间后显示源镜像 → Registry 目标；提交时自动按原镜像名称和标签同步，任务使用同步后的地址。检查、保存或取消不会上传；上传失败不会创建任务。当前资源池要求 linux/amd64 镜像，ARM 本地镜像需先按该架构构建。

上传镜像需要正在运行的 Docker。先选择可访问且 ACTIVE 的命名空间，再从本地镜像列表选择源镜像（可搜索名称或标签，也可手动填写名称 / ID），最后填写目标名称和标签。已有可用 Docker 凭据时不重复询问密码；推送权限由 Registry 校验。

镜像列表表示“当前账号可访问的命名空间”，不等于本人创建。CCR 首次查询可能返回整个仓库目录。列表与 CCI/ACP 镜像选择共享 5 分钟本地缓存，重新进入或重启程序可复用，页面显示缓存状态和更新时间；搜索、翻页不请求云端。点击“更新云端”（或 r）强制读取最新数据，仍可能等待较久；失败时保留已显示的结果。上传成功后自动清除对应命名空间的缓存，取消上传则复用缓存。缓存按账户和完整命名空间范围隔离，保存在忽略提交的 `.cache/ccr/`，不包含账户密钥。

DNAT 列表直接汇总本人创建的规则，无需先选择 EIP。创建规则仍需选择承载 EIP。删除已绑定规则会先解绑；删除 CCI 不会连带删除独立 DNAT。

```sh
uv run main.py cci create-last
uv run main.py acp create-last
uv run main.py cci list --plain
uv run main.py acp list --name task-prefix
uv run main.py dnat list --plain
uv run main.py ccr list --namespace your-namespace
```

[配置参考](docs/CONFIGURATION.md) · [服务行为](docs/SERVICES.md) · [代码结构](docs/ARCHITECTURE.md) · [真实验证](docs/LIVE-VALIDATION.md)
