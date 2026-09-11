# 运行环境

需要 Python 3.11+ 和 uv。


```sh
uv sync --locked
uv run main.py
```

首次点击“开始设置”：按页面说明取得 AccessKey，验证成功后自动生成根 `config.toml`，然后从列表选择工作空间。可以中途取消，下次启动会继续提示未完成的步骤。

| 功能 | 额外依赖 |
| --- | --- |
| 云资源管理、列表和表单 | 无 |
| SSH 公钥校验与连接 | OpenSSH：ssh-keygen、ssh |
| SOCKS5 SSH 转发 | Ncat，通常随 Nmap 提供 |
| Docker 镜像上传、构建 | Docker CLI 与运行中的 Docker Engine / Docker Desktop |
| Linux 桌面复制 | wl-clipboard、xclip 或 xsel 之一 |

macOS 安装 Ncat 用 `brew install nmap`；Ubuntu/Debian 用 `sudo apt install ncat`；Windows 可用 `winget install --id Insecure.Nmap -e`。其他发行版通过包管理器安装提供 `ncat` 命令的软件包。

Windows 推荐 Windows Terminal。生成的 SSH 命令需要 PowerShell 7.3+；OpenSSH Client 可从 Windows 可选功能安装。Docker 镜像构建使用 Linux 容器，CCI 默认镜像面向 linux/amd64。WSL 按 Linux 使用。

终端不适合全屏界面时运行 `uv run main.py --text`。Windows 云端功能复用同一 REST 代码；原生 Windows 的安装环境和 SSH 登录仍需在对应机器验证。

启用代理而本机没有 Ncat 时，程序尝试 Homebrew、apt-get、dnf、pacman 或 winget。Linux 使用 root 或非交互 sudo；没有权限时给出手动安装命令，避免界面等待密码。安装失败可手动执行后重试。

安装包来源：[Homebrew nmap](https://formulae.brew.sh/formula/nmap)、[Ncat 官方说明](https://nmap.org/ncat/)。
