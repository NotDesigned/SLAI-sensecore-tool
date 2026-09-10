# SLAI-tool

面向 SLAI 账户的 SenseCore 命令行工具，用交互菜单管理镜像、云容器和 DNAT 规则。支持 Windows x64、Linux、macOS，所有配置保存在项目根目录的 `config.toml`。

| 功能 | 支持的操作 |
| --- | --- |
| SCO | 安装并配置、卸载 |
| CCR | 上传镜像、列出可访问命名空间内的镜像 |
| CCI | 创建、列出、停止、复制、删除 |
| DNAT | 创建、列出、详情、绑定 CCI、解绑、删除 |
| ACP | [调研与接入准备](docs/ACP-INTEGRATION.md)，尚未加入菜单 |

## 快速开始

准备好 [uv](https://docs.astral.sh/uv/getting-started/installation/) 和 SenseCore AccessKey，然后执行：

```bash
git clone git@github.com:NotDesigned/SLAI-sensecore-tool.git
cd SLAI-sensecore-tool
uv run main.py
```

uv 会准备 Python 3.11+ 及项目依赖。首次运行需联网；SCO 安装包随仓库分发，仓库因此较大。Linux/macOS 安装 SCO 需要 Bash、curl、tar、awk；Windows 安装使用 PowerShell、curl.exe、tar.exe，首次需联网下载 Windows 包。

```text
1. 安装并配置 SCO
2. 卸载 SCO
3. CCR 服务
4. CCI 服务
5. DNAT 服务
0. 退出
```

首次选择 **1. 安装并配置 SCO**：安装主程序 → 配置本机环境变量和 PATH → 补齐账户信息并初始化 → 安装 EIP、CCR 组件。缺少 `config.toml` 时会基于[配置模板](config.example.toml)创建，不必提前填写全部字段。

编号菜单统一用 **`0` 返回**，确认页的 `0` 为取消，主菜单的 `0` 为退出。文字输入可用 `q` 取消，列表页可用 `r` 刷新。

### Windows 使用说明

支持 Windows 10/11 x64；官方 SCO 安装器当前仅提供 AMD64，Windows ARM64 不在此原生支持范围。项目安装流程不需要 Bash，也不修改系统级 PATH；SCO 目录写入当前用户环境变量。

菜单可从 Windows PowerShell 或 PowerShell 7 启动；**输出的 SSH 命令请在 PowerShell 7.3+ 执行**，避免旧版本对嵌套引号的处理差异。需要 Windows OpenSSH Client；上传镜像需要 Docker Desktop 处于 Linux 容器模式。WSL 请按 Linux 流程使用。

详见 [Windows 安装说明](docs/INSTALLATION.md#windows-x64)。当前在 macOS 完成兼容分支测试，原生 Windows 安装及云端连接尚待 Windows 环境验证。

## 创建 CCI 并连接

进入 **CCI 服务 → 创建**，选择工作空间、资源池和规格，确认镜像、存储及 DNAT 入口，最后检查生成的配置并选择“提交创建”。默认先保存配置，不会直接创建云资源。

| 项目 | 默认行为 |
| --- | --- |
| 镜像 | `lepton-trainingjob/ngc-pytorch:25.06-cu12.9-py3.12-ubuntu24.04`，完整地址见配置模板 |
| 资源 | 从云端规格自动获取 CPU、内存及加速卡资源键 |
| 调度 | 优先级固定 `NORMAL`，配额默认 `RESERVED` |
| AFS | 默认选择 AI 文件存储；同可用区仅一项时自动选中 |
| 挂载 | `/<当前 IAM 用户名>` → `/data` |
| SSH | 默认开启，使用本机公钥登录 `root`，容器端口 `22` |
| DNAT | 默认新建，也可选择已有规则；公网端口随机避开已占用端口 |

默认 SSH 启动脚本要求容器以 root 运行；镜像没有 sshd 时会尝试通过 apt 安装，需要容器能访问软件源。公钥通过 `cci.ssh_public_key` 指定，留空时从本机 `~/.ssh/*.pub` 选择。

创建并核实 DNAT 绑定后，工具输出**一条 SSH 命令**：终端直接执行，也可粘贴到 VS Code 的 **Remote-SSH: Add New SSH Host…**。工具不会自动连接或修改 SSH config；使用非默认私钥时，在命令中加 `-i 私钥路径`。

绑定状态正确不等于 SSH 已能登录；仍需目标服务正常运行、网络可达及公钥认证成功。

### 需要 SOCKS5 代理时

在本地 `config.toml` 中填写：

```toml
[cci.ssh_proxy]
server = ""  # SOCKS5 服务器 IP；留空则直连
port = 1080
username = ""
password = ""
```

代理模式使用 Python 读取配置，再调用 **ncat** 转发。先按本机系统安装：

| 系统 | 安装命令 |
| --- | --- |
| Windows | `winget install --id Insecure.Nmap -e`，或安装 Nmap 官方 Windows 安装包 |
| macOS | `brew install nmap` |
| Ubuntu / Debian | `sudo apt update && sudo apt install -y ncat` |
| Fedora / Rocky / AlmaLinux | `sudo dnf install -y nmap-ncat` |
| Arch Linux | `sudo pacman -S nmap` |

Homebrew 包名是 **nmap**，不是 ncat。生成的 SSH 命令不含代理账号密码，执行时从配置读取；ncat 的进程参数仍包含凭据。命令引用本机 Python 和项目的绝对路径，移动项目或换电脑后需重新生成。

更多配置见[SSH 与代理配置](docs/CONFIGURATION.md#ssh-与代理)。

## 管理已有资源

### CCI

**CCI 服务 → 列出 → 选择实例**，可停止、复制或删除。列表仅显示当前用户的实例。

复制以新名称提交相同模板，沿用原存储目录，不自动迁移 DNAT。删除 CCI 也不会删除独立 DNAT 规则。

### DNAT

**DNAT 服务 → 列出 → 选择规则**，可查看详情、绑定已有 CCI、解绑或删除。列表汇总各 EIP 下本人创建的规则，以 **IP:端口** 开头，无需先选择 EIP。

绑定时选择同可用区、同 VPC 的本人 CCI 及其已有 TCP 服务端口。迁移旧绑定会先确认；删除已绑定规则会先解绑，核实完成后再删除。单独解绑则保留规则及端口。

### CCR

**CCR 服务 → 上传镜像 / 列出可访问镜像**。上传需要本机 Docker 服务和已有镜像；登录使用 CCR 的客户端密码，不是 SCO AccessKey Secret。

镜像列表范围是当前账号**可访问命名空间内的镜像**，不代表镜像均由本人创建。

## 常用命令

在项目根目录运行；服务命令不带子命令时打开菜单：

```bash
uv run main.py install
uv run main.py cci
uv run main.py dnat
uv run main.py ccr

# 仅打印列表，不进入操作菜单
uv run main.py cci list --workspace your-workspace --plain
uv run main.py dnat list --plain
uv run main.py ccr list --namespace your-namespace
```

各服务支持 `--help`。完整参数与原生 EIP 兼容入口见[服务操作参考](docs/SERVICES.md)。

## 常见问题

- **安装后终端找不到 sco？** 打开新终端，或按安装输出加载 Shell 配置；项目菜单使用配置中的安装路径。
- **找不到 ncat？** 需要 Nmap 的 Ncat，不是任意版本的 `nc`；macOS 用 `brew install nmap`。
- **SSH 一直停在 Connecting？** 先检查是否需要配置 SOCKS5、DNAT 目标及 CCI 状态；TCP 连接尚未建立时，还没有进入公钥认证。
- **列表没有资源？** 核对账户权限、工作空间和区域；CCI/DNAT 只显示本人资源，CCR 显示可访问范围。
- **操作超时？** 先刷新列表核对云端状态，再决定是否重试，避免重复创建。

## 详细文档

- [配置参考](docs/CONFIGURATION.md)：账户、路径、镜像、SSH、代理和 Docker。
- [服务操作参考](docs/SERVICES.md)：创建、绑定、复制、原生 CLI 兼容及当前限制。
- [安装与维护](docs/INSTALLATION.md)：缓存、Shell 环境、卸载及测试。
- [安装包清单](vendor/sco/README.md)、[历史检查记录](docs/AUDIT.md)。
- [ACP 调研与接入方案](docs/ACP-INTEGRATION.md)。

`config.toml` 及其备份、虚拟环境和运行缓存均由 Git 忽略。真实账户配置只保留在本机。
