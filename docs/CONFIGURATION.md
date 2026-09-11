# 配置参考

所有脚本读取仓库根目录的 `config.toml`，模板见 [config.example.toml](../config.example.toml)。菜单保存配置时保留注释和其他字段，检查并发修改后原子替换。POSIX 文件权限为 0600；Windows 遵循用户目录 ACL。

| 配置表 | 用途 |
| --- | --- |
| `[account]` | AccessKey ID、AccessKey Secret；直接用于 REST HMAC 鉴权 |
| `[workspace]` | 默认工作空间及完整资源范围，由顶部“工作空间”按钮 保存 |
| `[cci]` | 默认镜像、是否启用 SSH、公钥路径、附加命令 |
| `[cci.last]` | 上次保存的创建选项，由程序维护 |
| `[acp]` | 默认镜像和任务命令 |
| `[acp.last]` | 上次保存的 ACP 创建选项 |
| `[docker]` | Registry 与上次上传选择；密码由 Docker 管理 |
| `[network]` | ACP 是否使用代理 |
| `[network.socks5]` | SSH 与可选 ACP 代理地址、端口及认证信息 |

账户配置只有两项：

```toml
[account]
access_key_id = ""
access_key_secret = ""
```

“配置账户”先校验 IAM 身份，再保存。已有完整配置可验证当前账户或切换账户。工作空间、区域和资源组根据云端资源选择，自动填入相应范围。


## CCI

`ssh_enabled` 默认 true。`ssh_public_key` 是公钥文件路径；留空时选择 `~/.ssh/*.pub`。相对路径以项目目录为基准，不能填写私钥。`command` 留空以前台 sshd 保持容器运行，非空则启动 sshd 后执行该命令。关闭 SSH 时恢复普通容器命令和端口选择。

普通创建检查完成后保存 `[cci.last]`。复用时重新核对账号、工作空间、资源池身份、规格及存储访问范围；过期选项会提示重新选择。配置不会记住旧 CCI 名称或直接复用旧 DNAT 绑定。新实例使用新名称；附加入口时新建规则并分配空闲端口。该记录可以删除以清除历史。

## 代理与 SSH

```toml
[network]
acp_proxy = false

[network.socks5]
server = ""
port = 1080
username = ""
password = ""
```

顶部“配置 SOCKS5”按钮可编辑或关闭代理，不必手动改文件；密码隐藏输入，同一代理可选择保留已有密码。关闭代理时同步关闭 ACP 代理开关。

主界面在启动和配置完成后检测，也支持手动重测，显示检测时间。检测只验证 SOCKS5 握手及用户名密码认证，不验证任意目标可达；具体 CCI 由 SSH 入口检查判断。协议依据 [RFC 1928](https://www.rfc-editor.org/rfc/rfc1928.html) 和 [RFC 1929](https://www.rfc-editor.org/rfc/rfc1929.html)。

server 留空时 SSH 直连；填写时使用 SOCKS5，用户名和密码须同时填写或同时留空。SSH 通过 Ncat 转发；输出命令没有代理凭据，辅助程序运行时读取本机配置。Ncat 进程参数仍包含代理凭据。

`acp_proxy=true` 时，ACP REST 使用同一 SOCKS5 和远端 DNS，不需要 Ncat；TLS 校验开启，没有自动直连回退。资源目录、CCI、DNAT、CCR 和 IAM 仍使用系统网络。

生成的 SSH 命令引用本机 Python 与项目绝对路径。换电脑或移动项目后重新生成；VS Code 用 Add New SSH Host 添加，再从 Connect to Host 选择。用户非默认私钥可额外配置 `IdentityFile` 或 `-i`。

## ACP 与 Docker

ACP 默认闲时资源，CCI 默认预留资源。所选资源池还需允许对应配额；例如调试池可能只允许预留资源，服务端拒绝时界面会提示切换资源池或配额。`[acp.last]` 保存镜像、启动方式、命令、资源和挂载，复用时重新校验。

ACP 的 `image`、`command` 是表单初始值；命令必须非空。入口模式显式填写入口程序及任务参数，不自动推断训练脚本。

Docker 每次上传都选择命名空间、源镜像、目标名称及标签。账号密码由 Docker 的 auths、credential helper 或 credsStore 管理，不写入本文件。没有凭据或推送明确认证失败时才提示登录。CCR 客户端密码与 AccessKey Secret 不同。
创建表单选择本地镜像时，历史记录还包含源标签、镜像 ID 和同步目标。复用会重新读取本机标签；只有提交时才上传。
