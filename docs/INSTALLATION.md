# 运行环境

需要 Python 3.11+ 和 uv。


```sh
uv sync --locked
uv run main.py
```

首次点击“开始设置”：按页面说明取得 AccessKey，验证成功后自动生成根 `config.toml`，然后从列表选择工作空间。可以中途取消，下次启动会继续提示未完成的步骤。账户验证失败、配置保存失败会分别提示；如果账户已保存而工作空间读取失败，可以从顶部“工作空间”重试，无需重新填写密钥。

手动编辑 `config.toml` 时请保存为 UTF-8（允许 BOM）。UTF-16 等编码会显示转换编码的提示。

| 功能 | 额外依赖 |
| --- | --- |
| 云资源管理、列表和表单 | 无 |
| SSH 公钥校验与连接 | OpenSSH：ssh-keygen、ssh |
| SOCKS5 SSH 转发 | Ncat，通常随 Nmap 提供 |
| Docker 镜像上传、构建 | Docker CLI 与运行中的 Docker Engine / Docker Desktop |
| Linux 桌面复制 | wl-clipboard、xclip 或 xsel 之一 |

macOS 安装 Ncat 用 `brew install nmap`；Ubuntu/Debian 用 `sudo apt install ncat`；Windows 可用 `winget install --id Insecure.Nmap -e`。其他发行版通过包管理器安装提供 `ncat` 命令的软件包。

Windows 推荐 Windows Terminal。生成的 SSH 命令需要 PowerShell 7.3+；OpenSSH Client 可从 Windows 可选功能安装。Docker 镜像构建使用 Linux 容器，CCI 默认镜像面向 linux/amd64。WSL 按 Linux 使用。

终端不适合全屏界面时运行 `uv run main.py --text`。Windows 云端功能复用同一 REST 代码。GitHub Actions 在 Windows、macOS 和 Linux 上运行完整测试；Windows 另安装 Ncat，执行原生 PowerShell / OpenSSH → 项目代理 → SOCKS5 → 本地 SSH 服务的登录与命令回传测试，覆盖含空格和中文的项目路径。该测试不连接 SenseCore，真实云端登录及 VS Code 界面仍需在使用环境验证。

启用代理而本机没有 Ncat 时，程序尝试 Homebrew、apt-get、dnf、pacman 或 winget。Linux 使用 root 或非交互 sudo；没有权限时给出手动安装命令，避免界面等待密码。安装失败可手动执行后重试。

安装包来源：[Homebrew nmap](https://formulae.brew.sh/formula/nmap)、[Ncat 官方说明](https://nmap.org/ncat/)。
