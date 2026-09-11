# 配置参考

[返回 README](../README.md) · [完整模板](../config.example.toml) · [服务操作参考](SERVICES.md)

项目读取仓库根目录的 `config.toml`。可以复制模板后编辑，也可以在“安装并配置 SCO”时交互创建。菜单操作会重新读取配置；生成的代理命令在执行时读取配置，因此修改代理凭据后无需重建 CCI。

## 账户与路径

| 配置项 | 说明 |
| --- | --- |
| `paths.home` | SCO 安装目录，默认 `~/.sco`，对应 `SCO_HOME` |
| `paths.data_home` | SCO 产品与组件数据，默认 `~/.local/share/sco`，对应 `SCO_DATA_HOME` |
| `paths.config` | SCO Profile 与凭据目录，默认 `~/.config/sco`，对应 `SCO_CONFIG` |
| `sco.access_key_id` / `sco.access_key_secret` | SenseCore AccessKey，初始化时必填 |
| `sco.region` | Region code，缺失时编号选择；可选项由 `[regions]` 提供 |
| `sco.zone` | 默认 `cn-sh-01`；与 Region code 分开填写 |
| `sco.profile` | 默认 `default` |
| `sco.language` | `zh-CN` 或 `en-US` |
| `install.cache_dir` | 安装缓存目录，默认 `vendor/sco` |

路径支持 `~`；相对路径以项目根目录为基准。项目为 SCO 子进程设置这些环境变量，并将安装目录下的 `bin` 加入 PATH。

Region code 与显示名称的默认映射为 `cnsh01 → cn-sh-01`、`cnsh02 → cn-sh-02`、`cnyc01 → cn-yc-01`。可访问范围由账户权限决定。

程序保存配置时保留注释和其他配置项；Linux/macOS 将文件权限设为 `0600`。Windows 的 chmod 不能代替用户 ACL，文件访问权限遵循所在目录的 Windows ACL。Linux/macOS 手动复制配置后，也可执行：

```bash
chmod 600 config.toml
```

配置文件及 `config.toml.*` 备份均由 Git 忽略。不要将真实凭据写进示例文件。

## CCI 默认值

```toml
[cci]
image = "registry.cn-sh-01.sensecore.cn/lepton-trainingjob/ngc-pytorch:25.06-cu12.9-py3.12-ubuntu24.04"
ssh_enabled = true
ssh_public_key = ""
command = ""
```

- `image`：默认镜像，可换成自己的完整远端地址；留空或省略使用上面的默认值。
- `ssh_enabled`：默认 `true`，自动生成 sshd 启动脚本，开放 22 端口；不再询问普通启动命令和端口。
- `ssh_public_key`：OpenSSH 公钥文件路径，不能填写私钥。留空时查找 `~/.ssh/*.pub`；只有一项则自动使用，多项时编号选择，没有则询问路径。相对路径以项目根目录为基准。
- `command`：SSH 模式下留空时以前台 sshd 保持容器运行；非空命令则在启动 sshd 后执行。

`ssh_enabled = false` 时恢复普通容器的启动命令、端口交互，默认命令为 `sleep infinity`。自定义命令通过 `/bin/sh -c` 执行。

工作空间默认使用主菜单 7 保存的选择，资源池、规格通过实时云端列表选择。优先级固定 NORMAL，配额默认 RESERVED，可选择 SPOT。AFS 默认本人 IAM 用户名目录挂载到 `/data`；仅一个同可用区存储时自动选中，多项时优先匹配 `afs-share-<可用区末段>`。这些不是可任意扩展的 TOML 参数，当前模板未提供额外配置键。

## SSH 与代理

```toml
[network.socks5]
server = ""
port = 1080
username = ""
password = ""
```

`server` 为 SOCKS5 服务器 IP，留空则直连。认证代理需同时填写 username/password；不需要认证时两项都留空。端口须在 1–65535 范围内。

代理模式的执行链为：系统 SSH → 项目 Python 脚本 → Ncat → SOCKS5 服务器 → DNAT 入口。Python 负责读取配置和构造参数，Ncat 负责认证及字节转发，SSH 负责主机指纹检查和公钥认证。

输出命令前自动做约 8 秒的 SSH 协议响应检查，按当前配置直连或经 SOCKS5 代理；不进行认证登录，不写入 `~/.ssh/config`。检查失败仍输出一条可复制的 SSH 命令，并提示可能未处于 SLAI 内网及检查代理、CCI/sshd/DNAT。当前代理类型为 SOCKS5，不是 HTTP/HTTPS CONNECT。同一命令可粘贴到 VS Code 的 `Remote-SSH: Add New SSH Host…`。非默认私钥需在命令中添加 `-i 私钥路径`，或在用户自己的 SSH 配置中指定 IdentityFile。

命令引用本机 Python 解释器和项目脚本的绝对路径，可从任意工作目录执行。换电脑或移动项目后需重新生成。SSH/VS Code 运行环境也须能找到 ncat。

| 系统 | 安装命令 |
| --- | --- |
| Windows | `winget install --id Insecure.Nmap -e`，或 Nmap 官方 Windows 安装包 |
| macOS | `brew install nmap` |
| Ubuntu / Debian | `sudo apt update && sudo apt install -y ncat` |
| Fedora / Rocky / AlmaLinux | `sudo dnf install -y nmap-ncat` |
| Arch Linux | `sudo pacman -S nmap` |

输出命令不含代理凭据；Ncat 运行时的进程参数仍含凭据，不能视为对本机进程检查隐藏。参考：[Ncat 代理文档](https://nmap.org/ncat/guide/ncat-proxy.html)、[Ubuntu 包](https://packages.ubuntu.com/noble/ncat)、[Fedora 包](https://packages.fedoraproject.org/pkgs/nmap/nmap-ncat/)。

## Docker / CCR 上传

```toml
[docker]
registry = "registry.cn-sh-01.sensecore.cn"
username = ""
namespace = ""
source_image = ""
image_name = ""
tag = "latest"
```

`registry` 只填写主机名及可选端口，不带 `https://` 或仓库路径。每次上传询问 namespace、source_image、image_name 和 tag；配置中的值作为默认值，修改后保存。最终目标为 `registry/namespace/image_name:tag`。

`username` 用于 Docker 登录提示，密码由 Docker 的凭据存储管理，不保存到此配置。请使用 CCR 客户端登录密码，而非 AccessKey Secret。详情见[镜像操作参考](SERVICES.md#ccr-镜像管理)。

Windows TOML 路径推荐使用正斜杠，例如 `C:/Users/name/.sco`，或使用 TOML 单引号保留反斜杠。默认 `~/.sco` 等路径在 Windows 下也可用；项目自动选择 `sco.exe`。SSH 代理命令输出采用 PowerShell 7.3+ 引号规则，路径包含百分号时需移动项目后重新生成。


## 默认工作空间

通过主菜单 7 或 `uv run main.py workspace` 选择，自动保存 `[workspace]` 的名称、资源 ID、Region、订阅、资源组和可用区。CCI、ACP 默认使用该资源范围；`--workspace` 可临时覆盖本次操作。工作空间失效时会重新询问，不静默切换。

## ACP 默认值

```toml
[acp]
image = ""
command = ""

```

`image` 留空使用与 CCI 相同的默认 NGC 镜像；`command` 为任务命令输入框默认值。创建页确认启动方式、框架、Worker 数量和配额；优先级固定 NORMAL，重试次数 0。不会注入 CCI 的 SSH 启动脚本。

`[network].acp_proxy = true` 时，仅 ACP 任务请求复用 `[network.socks5]`，不需要 ncat；资源目录及身份查询保持系统网络。默认 `false` 使用系统网络。此项与 SSH 连接的代理开关互不替代。


```toml
[network]
acp_proxy = false
```

账户信息仍集中在 `[sco]`，已选工作空间在 `[workspace]`，服务默认值在 `[cci]` / `[acp]`，网络与服务配置分开。旧的 `cci.ssh_proxy` 和 `acp.use_ssh_proxy` 不再读取；使用新模板配置。
