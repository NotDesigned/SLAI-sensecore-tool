"""First-run guidance inferred from the one root configuration; no extra state file."""
from scripts import cli, ui

GUIDE = '''先完成账户与工作空间设置，再选择要做的事：

CCI · 交互调试：创建调试容器，通过 SSH 或 VS Code 连接。
ACP · 长任务：提交训练等任务，退出本工具后任务继续运行。
CCR · 镜像：浏览可访问的镜像，或上传自己的 Docker 镜像。
DNAT · 连接入口：把 IP:端口绑定到 CCI，供 SSH 等服务访问。

仅管理云资源不需要 Docker；上传本地镜像时才需要安装并启动 Docker。
CCI 的 SSH 登录需要本机 OpenSSH 和公钥（.pub 文件）。没有密钥时，可在终端运行 ssh-keygen -t ed25519，按提示生成并保留私钥。
默认 SSH 镜像位于共享命名空间，需要拉取权限；没有权限时在镜像字段搜索可访问镜像，或上传自己的镜像。
在 SLAI 内网外连接 CCI，可能需要在 config.toml 的 network.socks5 中填写管理员提供的代理；详见 README 的“SLAI 内网代理”。

方向键移动，Enter 选择，0 / Esc 返回。列表页的“创建”打开表单；默认主按钮是“提交创建”；点击后校验并提交。“仅保存配置”只保存，不创建云资源。'''


def state():
    try:
        config = cli.load_config(for_setup=True)
        account = config['account']
        if not all(cli.string_value(account, k).strip() for k in ('access_key_id', 'access_key_secret')):
            return 'account', '欢迎使用 SLAI-tool · 首次使用 1/2\n先连接你的 SenseCore 账户，再选择工作空间。无需手动创建配置文件。'
        from scripts.workspace import FIELDS
        if not all(cli.string_value(config.get('workspace', {}), k).strip() for k in FIELDS):
            return 'workspace', '首次使用 2/2 · 选择默认工作空间\n账户信息已填写；选择你有权限使用的工作空间，之后 CCI / ACP 自动使用它。'
        return 'ready', '选择服务 · ↑↓ 移动 · Enter 打开 · 不知道选哪个可查看“使用指南”'
    except (cli.ConfigError, OSError):
        return 'error', '配置需要检查。请查看使用指南或修复根目录 config.toml。'


def guide():
    ui.show_text('使用指南', GUIDE)


def start():
    kind, _ = state()
    if kind == 'error':
        cli.load_config(for_setup=True)  # Report the concrete error; never overwrite it.
        return
    if kind == 'account':
        ui.show_text('首次使用 1/2 · 连接账户',
            '需要你自己的 SenseCore AccessKey ID 和 Secret，不是网页登录密码。\n'
            '在控制台右上角头像菜单中打开“AccessKey 访问密钥”，创建并保存这两项。\n'
            '下一步输入后先验证账户；成功才生成本机 config.toml。Secret 输入会隐藏。\n'
            '官方说明：https://www.sensecore.cn/help/docs/ApiDoc/synopsis',
            hint='看完后按 0 / Esc 继续；后续输入中按 Esc 可取消。')
        cli.configure_account(cli.load_config(for_setup=True))
    kind, _ = state()
    if kind == 'workspace':
        ui.output('首次使用 2/2 · 正在读取可访问工作空间；没有选项时请联系 SLAI 管理员授权。')
        try:
            cli.configure_workspace(cli.load_config())
        except ui.Cancelled:
            raise cli.ConfigError('账户已验证并保存。可稍后通过顶部“工作空间”按钮继续配置，无需重新输入密钥。',
                                  title='账户已保存，工作空间未配置') from None
        except (cli.ConfigError, OSError) as error:
            reason = str(error) if isinstance(error, cli.ConfigError) else '请检查网络连接。'
            raise cli.ConfigError('账户已验证并保存，工作空间配置未完成。' + reason + '\n可通过顶部“工作空间”按钮重试。',
                                  title='账户已保存，工作空间未完成') from None
    ui.output('设置完成。交互调试选 CCI，长任务选 ACP；进入列表后点击“创建”。')
