# 安装与维护

[返回 README](../README.md) · [配置参考](CONFIGURATION.md)

## 安装并配置 SCO

调用 SenseCore 官方安装器，安装到配置的路径，并执行 `sco version` 检查结果；随后配置 Bash/Zsh 环境。随后自动进入初始化配置：沿用已有账户配置，交互补齐缺失项，执行 `sco init`，成功后统一安装 EIP、CCR。已有 Profile 也遵循这一顺序，每次流程仅在初始化成功后尝试安装一次各组件。安装失败时不继续初始化，初始化失败时不继续安装组件。组件使用 `[sco].region` 指定地区，留空时使用 `cnsh01`；首次下载组件需要联网。

需要 Bash、curl、tar 和 awk。使用随仓库分发的 [官方安装器](https://sco.sensecore.cn/registry/install.sh) 及安装包，缓存完整时无需重新下载。

### 内置安装包

仓库直接通过 Git 分发 `vendor/sco/` 中的官方安装器、元数据和 **SCO v2.0.2（Registry 20260830）** 安装包。克隆仓库即可获得以下四个平台的 launcher、v1 和 v2，共 12 个安装包：

| 系统 | 架构 |
| --- | --- |
| Linux | AMD64 / x86_64 |
| Linux | ARM64 / aarch64 |
| macOS | AMD64 / Intel |
| macOS | ARM64 / Apple Silicon |

安装资源清单见 [vendor/sco/README.md](../vendor/sco/README.md)。每个文件均有来源 URL、大小和 SHA-256 记录，安装包还记录官方清单提供的校验值。命中有效缓存时直接复制本地文件，官方安装器继续负责校验和安装。

选择 **1. 安装并配置 SCO** 即可使用本机对应的缓存。缓存完整时 SCO 主程序可以离线安装；所需的 EIP、CCR 组件在 Profile 初始化后安装，首次下载需要联网，失败时会按组件名称报告安装失败，已安装的 SCO 主程序仍会保留。卸载 SCO 不会清理仓库缓存。安装结束的 `sco version` 可能联网检查更新；离线时更新状态不可用，不影响已完成的安装。缓存损坏时会重新下载；失败或中断的下载不会作为完整缓存使用。

默认缓存目录已纳入 Git。若自行更改 `install.cache_dir`，需要将随仓库分发的缓存复制到新目录，否则会重新下载。更新捆绑版本时，应同步替换安装器、元数据、各平台安装包及校验记录；原生 `sco update` 使用官方更新机制，不更新本仓库的缓存。

### 初始化配置

读取 `config.toml` 的账户信息，执行非交互 `sco init`，完成 AccessKey 校验、profile 保存和官方自带的诊断。“登录”通过这一步完成，无需单独的登录命令。

缺失或留空的 AccessKey ID、AccessKey Secret 会逐项询问，必填项输入为空时会重新询问。Region 缺失时显示编号列表：`1. cn-sh-01 (cnsh01)`、`2. cn-sh-02 (cnsh02)`、`3. cn-yc-01 (cnyc01)`；输入编号后将对应 code 保存到 `sco.region`。选项来自 `config.toml` 的 `[regions]`，初始值依据仓库中的官方地区清单，实际访问权限由后续 `sco init` 校验。密钥输入不回显。zone、profile、language 缺失时也会询问，可回车采用 `cn-sh-01`、`default`、`zh-CN`。已有非空值直接沿用。

所有输入收集完成后，程序将其一次性写回 `config.toml`，保留注释和其他配置，并将文件权限设为仅当前用户可读写；中途取消不保存。随后调用 SCO 执行初始化，如果凭据校验失败，已填写的信息仍会保留供下次使用。重复执行会重新初始化指定 profile。程序不打印凭据或完整命令；官方接口通过命令参数接收密钥，运行期间系统进程列表可能包含这些参数。

参数依据：[SCO 初始化文档](https://console.sensecore.cn/micro/help/docs/CLI/SCO_Refrence/init)。

## Windows x64

使用[官方 PowerShell 安装方式](https://www.sensecore.cn/help/docs/CLI/Introduction)。项目将安装器缓存到 `install.cache_dir`，通过 PowerShell 调用，保留官方解包、校验和回滚逻辑。Windows 安装器需使用 `curl.exe` 和 `tar.exe`；首次下载各 Windows runtime 与组件需要联网。仓库内原有离线包仅覆盖 Linux/macOS，不能当作 Windows 离线包。

安装后执行 `sco.exe version`，然后将 SCO_HOME、SCO_DATA_HOME、SCO_CONFIG 及 SCO bin 路径写入当前用户的 Windows Environment 注册表项。保留用户原 PATH 与变量引用，不修改系统 PATH，不写 Bash/Zsh 配置。后续初始化成功后再安装 EIP、CCR。

新开终端生效；VS Code 内终端可能需要重启 VS Code。当前项目菜单始终直接使用配置里的 sco.exe 路径。

菜单可使用 Windows PowerShell；复制执行复杂 SSH 命令需要 PowerShell 7.3+。OpenSSH Client 可通过 Windows“可选功能”安装，Ncat 可通过 Nmap Windows 安装包或 `winget install --id Insecure.Nmap -e` 安装。项目先查 PATH，再查 Program Files 下常见 Nmap 安装目录。

未修改 PowerShell 执行策略；若组织策略禁止脚本执行，应使用组织允许的安装方式。当前官方脚本选择 Windows AMD64 包；项目拒绝在 ARM64 上直接按 AMD64 安装。

## Shell 环境

Linux/macOS 安装会向 `~/.bashrc`、Bash 当前有效的登录配置（依次选已有 `.bash_profile`、`.bash_login`、`.profile`，否则创建 `.bash_profile`）及 Zsh 的 `.zshrc`、`.zprofile` 写入带标记的 SCO 环境块；Zsh 遵循 `ZDOTDIR`。自动导出 `SCO_HOME`、`SCO_DATA_HOME`、`SCO_CONFIG` 并将 `home/bin` 加入 PATH，重复安装更新同一配置块，保留其他内容。打开新终端生效；当前 Bash 可执行 `source ~/.bashrc`，Zsh 可执行 `source "${ZDOTDIR:-$HOME}/.zshrc"`。安装进程无法直接修改父终端环境。

## 卸载

菜单“卸载 SCO”先运行 `sco uninstall --dry-run`，展示待删除的本地配置和数据。输入 `yes` 后执行卸载，其他输入取消。仓库安装缓存不会被删除。

## 开发与验证

功能代码位于 `scripts/`，测试位于 `tests/`。新增配置需同步更新根目录模板和文档。

```bash
uv run python -m unittest discover -s tests -v
uv run python -m compileall -q scripts tests
git diff --check
```

组件安装集中维护于 `scripts/cli.py` 的 `REQUIRED_COMPONENTS`，当前为 `eip`、`ccr`。仅将确实需要单独安装的组件加入列表；ACP 在当前 SCO 中已可直接调用，不应盲目加入。初始化成功后安装组件，单个失败仍尝试其余组件，最后汇总错误。

[历史检查记录](AUDIT.md)记录的是检查当时的结果，测试数量和验证范围不会随代码自动更新。

## 跨平台 CI

`.github/workflows/tests.yml` 在 Linux/macOS 运行完整测试，在 Windows 运行专项测试（包含原生 PowerShell 参数往返、OpenSSH 命令解析）及入口检查。CI 不安装或修改实际云资源，也不写用户注册表。测试配置已加入，未将尚未运行的远端 CI 声称为通过。

PowerShell 参数要求依据 [Microsoft 原生命令参数说明](https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.core/about/about_parsing?view=powershell-7.6)；如果用户自行将 `$PSNativeCommandArgumentPassing` 设为 Legacy，需要恢复为 Windows/Standard 模式后执行生成命令。
