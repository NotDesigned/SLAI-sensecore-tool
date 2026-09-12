#!/usr/bin/env python3
"""Application entry point and the repository's single account configuration."""
import os
import getpass
import warnings
from pathlib import Path
import sys
import tempfile
import tomllib
import tomlkit

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / 'config.toml'


def print(*args, **kwargs):
    from scripts.ui import output
    return output(*args, **kwargs)


class ConfigError(Exception):
    def __init__(self, message, *, title=None):
        super().__init__(message)
        self.title = title


def configure_stdio():
    # Windows console streams are usually UTF-8, redirected streams may be cp1252.
    if sys.platform == 'win32':
        for stream in (sys.stdout, sys.stderr):
            if hasattr(stream, 'reconfigure') and getattr(stream, 'encoding', '').lower().replace('-', '') != 'utf8':
                stream.reconfigure(encoding='utf-8')


configure_stdio()


def config_text(path):
    try:
        return path.read_text(encoding='utf-8-sig')
    except UnicodeError:
        raise ConfigError('config.toml 必须保存为 UTF-8 编码；请在编辑器中转换编码后重试。', title='配置文件编码错误') from None


def load_config(for_setup=False):
    try:
        config = tomllib.loads(config_text(CONFIG))
    except FileNotFoundError:
        if not for_setup:
            raise ConfigError('缺少 config.toml，请先选择“配置账户”，或复制 config.example.toml。') from None
        config = tomllib.loads((ROOT / 'config.example.toml').read_text(encoding='utf-8'))
    except tomllib.TOMLDecodeError:
        raise ConfigError('config.toml 语法错误，请检查 TOML 格式。') from None
    if for_setup:
        config.setdefault('account', {})
    if not isinstance(config.get('account'), dict):
        raise ConfigError('config.toml 缺少 [account]，请按 config.example.toml 更新配置。')
    for section in ('cci', 'docker', 'acp', 'workspace', 'network'):
        if section in config and not isinstance(config[section], dict):
            raise ConfigError(f'[{section}] 必须是配置表。')
    return config


def string_value(table, key, required=False):
    value = table.get(key, '')
    if not isinstance(value, str):
        raise ConfigError(f'配置项 {key} 必须是字符串。')
    if required and not value.strip():
        raise ConfigError(f'请在 config.toml 中填写 {key}。')
    return value


def save_config_updates(section, updates, original):
    try:
        text = config_text(CONFIG) if CONFIG.exists() else (ROOT / 'config.example.toml').read_text(encoding='utf-8')
        document = tomlkit.parse(text)
    except tomlkit.exceptions.ParseError:
        raise ConfigError('config.toml 语法错误，未保存输入。') from None
    if section not in document:
        document[section] = tomlkit.table()
    if not isinstance(document[section], dict):
        raise ConfigError(f'[{section}] 必须是配置表，未保存输入。')
    for key, value in updates.items():
        current = document[section].get(key, '')
        if current != original.get(key, ''):
            raise ConfigError('输入期间相关配置已被修改，请重新选择操作。')
        document[section][key] = value
    serialized = tomlkit.dumps(document)
    updated = tomllib.loads(serialized)
    # Replace atomically after collecting inputs; mode 0600 restricts POSIX permissions.
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=CONFIG.parent,
                                         prefix='.config-', suffix='.tmp', delete=False) as stream:
            temporary = Path(stream.name)
            os.chmod(temporary, 0o600)
            stream.write(serialized)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, CONFIG)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return updated


def configure_account(config):
    from scripts import ui, rest
    settings = config['account']
    for key in ('access_key_id','access_key_secret'):
        string_value(settings,key)
    complete = all(settings.get(k,'').strip() for k in ('access_key_id','access_key_secret'))
    replace = complete and ui.choose('账户设置',['验证当前账户','切换账户'],default='验证当前账户')=='切换账户'
    updates = dict(settings)
    if replace:
        updates = {}
    if not updates.get('access_key_id','').strip():
        updates['access_key_id'] = ui.ask('AccessKey ID')
    if not updates.get('access_key_secret','').strip():
        try:
            with warnings.catch_warnings():
                warnings.simplefilter('error',getpass.GetPassWarning)
                updates['access_key_secret'] = ui.secret('AccessKey Secret').strip()
        except getpass.GetPassWarning:
            raise ConfigError('当前终端无法隐藏密钥输入，请在交互终端重试或填写 config.toml。') from None
    if not updates['access_key_secret']:
        raise ConfigError('AccessKey Secret 不能为空。')
    try:
        identity = rest.get_json({**config,'account':updates},'https://iam.sensecoreapi.cn/iam/idp/v1/me',timeout=20)
        rest.identity_id(identity)
    except (ConfigError, OSError) as error:
        reason = str(error) if isinstance(error, ConfigError) else '请检查网络连接。'
        raise ConfigError('账户验证未通过，配置未保存。' + reason, title='账户验证失败') from None
    try:
        save_config_updates('account',updates,settings)
    except (ConfigError, OSError) as error:
        reason = str(error) if isinstance(error, ConfigError) else '请检查项目目录的写入权限，以及文件是否被占用。'
        raise ConfigError('账户验证通过，但配置未保存。' + reason, title='账户配置保存失败') from None
    print('账户已验证并保存：' + str(identity.get('username') or identity['id']))


def configure_workspace(config):
    from scripts.workspace import configure
    configure(config)


SERVICES = {'ccr': 'ccr', 'cci': 'cci_service', 'dnat': 'dnat', 'acp': 'acp'}
MENU_ITEMS = (('ccr', 'CCR · 镜像管理'), ('cci', 'CCI · 交互调试'), ('dnat', 'DNAT · 连接入口'),
              ('acp', 'ACP · 长任务'))


def guarded(operation):
    from scripts.ui import Cancelled
    try:
        return operation() or 0
    except Cancelled:
        from scripts import ui
        if ui.active():
            raise
        print('已取消操作。')
        return 0
    except (ConfigError, OSError) as error:
        from scripts import ui
        if ui.active():
            raise
        print(str(error) if isinstance(error, ConfigError) else
              '无法读取配置或执行命令，请检查路径、权限和依赖。', file=sys.stderr)
        return 1


def run_service(action, args):
    from importlib import import_module
    return guarded(lambda: import_module('scripts.' + SERVICES[action]).main(args))


def execute(action):
    if action in SERVICES:
        from scripts import onboarding
        if onboarding.state()[0] == 'account':
            return guarded(onboarding.start)
        return run_service(action, [])

    def operation():
        from scripts import onboarding
        if action in ('configure', 'workspace') and onboarding.state()[0] == 'account':
            return onboarding.start()
        config = load_config(for_setup=action == 'configure')
        {'configure': configure_account,
         'workspace': configure_workspace}[action](config)
    return guarded(operation)


def menu_title(identity_cache):
    """Show configured account identity without blocking navigation on network failure."""
    import hashlib
    import time
    from scripts.rest import get_json

    def clean(value):
        return ''.join(c for c in value if c.isprintable()).strip()

    try:
        config = load_config()
        workspace = clean(string_value(config.get('workspace', {}), 'name')) or '未选择'
        settings = config['account']
        ak = string_value(settings, 'access_key_id')
        sk = string_value(settings, 'access_key_secret')
        if not ak or not sk:
            username = '未配置'
        else:
            key = hashlib.sha256((ak + '\0' + sk).encode()).hexdigest()
            now = time.monotonic()
            if identity_cache.get('key') != key or now >= identity_cache.get('expires', 0):
                username = '暂时无法确认'
                ttl = 30
                try:
                    identity = get_json(config, 'https://iam.sensecoreapi.cn/iam/idp/v1/me', timeout=3)
                    value = identity.get('username') if isinstance(identity, dict) else None
                    if isinstance(value, str) and clean(value):
                        username = clean(value)
                        ttl = 300
                except (ConfigError, OSError):
                    pass
                identity_cache.update(key=key, username=username, expires=time.monotonic() + ttl)
            username = identity_cache['username']
    except (ConfigError, OSError):
        username, workspace = ('未配置' if not CONFIG.exists() else '配置需要检查'), '未选择'
    return f'SLAI-tool · 用户：{username} · 工作空间：{workspace}'


def menu():
    identity_cache = {}
    proxy_status = None
    while True:
        print('\n' + menu_title(identity_cache))
        from scripts import onboarding
        print(onboarding.state()[1])
        print('a. 配置账户  w. 选择工作空间  p. 配置 SOCKS5  h. 使用指南')
        from scripts import proxy_settings
        if proxy_status is None:
            print('正在检测 SOCKS5 代理（最多约 8 秒）…')
            proxy_status = proxy_settings.status()
        print(proxy_status)
        for index, (_, title) in enumerate(MENU_ITEMS, 1):
            print(f'{index}. {title}')
        print('r. 检测代理\n0. 退出')
        choice = input(f'请选择 [0-{len(MENU_ITEMS)}]：').strip().lower()
        if choice in ('a', 'w'):
            execute('configure' if choice == 'a' else 'workspace')
            continue
        if choice == 'p':
            guarded(proxy_settings.configure)
            proxy_status = None
            continue
        if choice == 'r':
            proxy_status = None
            continue
        if choice == 'h':
            onboarding.guide()
            continue
        if choice in ('0', 'q'):
            return 0
        if not choice.isascii() or not choice.isdecimal() or not 1 <= int(choice) <= len(MENU_ITEMS):
            print(f'请输入 0 至 {len(MENU_ITEMS)}。')
            continue
        execute(MENU_ITEMS[int(choice) - 1][0])


def service_parser(service, actions, description, examples=''):
    import argparse
    parser = argparse.ArgumentParser(prog=f'uv run main.py {service}',
        description=description, formatter_class=argparse.RawDescriptionHelpFormatter, add_help=False,
        epilog='操作：\n' + '\n'.join(f'  {key:<14} {value}' for key,value in actions.items()) +
               '\n\n使用 --text 强制文本交互；创建/复制会打开可编辑配置表，仍需交互输入。\n' + examples)
    parser.add_argument('-h', '--help', action='help', help='显示此帮助并退出')
    parser.add_argument('action', nargs='?', choices=list(actions), default='list' if 'list' in actions else next(iter(actions)),
                        help='要执行的操作，默认 %(default)s')
    return parser


def show_help():
    print('''用法：uv run main.py [--text] [服务或设置] [操作] [参数]

不带参数：在交互终端打开 Textual；--text 强制使用文本菜单。
服务：
  cci         创建、列表、详情、连接、启动、停止、复制、删除、保存镜像和查看保存记录
  acp         创建、列表、详情、停止、复制、删除
  dnat        创建、列表、详情、绑定 CCI、解绑、删除
  ccr         可访问命名空间/镜像列表、上传本地镜像
设置：
  configure   配置并验证账户
  workspace   选择并保存默认工作空间
  proxy       配置 SOCKS5；proxy status 检测当前代理
  guide       显示使用指南

任一服务/设置后加 --help 查看参数。0 返回；文本输入 q 取消。
创建和复制仍需交互选择；--yes 仅跳过指定操作的确认，不补全缺失参数。
普通列表可用 --plain 输出；ACP 支持 --page、--page-size、--state。
密钥和代理密码保存在本地 config.toml，不通过命令参数传递。

示例：
  uv run main.py --text cci copy --name my-cci
  uv run main.py --text cci connect --name my-cci
  uv run main.py acp list --plain --state RUNNING --page 1
  uv run main.py ccr list --plain --namespace my-namespace
  uv run main.py proxy status''')


def setting(name, args):
    import argparse
    from scripts import proxy_settings, onboarding
    descriptions = {'configure':'配置并验证 AccessKey，首次设置后选择工作空间。',
                    'workspace':'从可访问工作空间中选择并保存默认项。',
                    'proxy':'配置 SOCKS5 或检测当前代理握手、认证。',
                    'guide':'显示研究任务使用指南。'}
    parser = argparse.ArgumentParser(prog='uv run main.py ' + name, description=descriptions[name], add_help=False)
    parser.add_argument('-h', '--help', action='help', help='显示此帮助并退出')
    if name == 'proxy':
        parser.add_argument('action', nargs='?', choices=['configure','status'], default='configure',
                            help='configure 交互配置；status 只读检测，默认 configure')
    options = parser.parse_args(args)
    if name in ('configure','workspace'):
        return execute(name)
    if name == 'guide':
        return guarded(onboarding.guide)
    return guarded(lambda: print(proxy_settings.status()) if options.action == 'status' else proxy_settings.configure())


def main():
    if sys.platform not in ('linux', 'darwin', 'win32'):
        print('目前支持 Windows、Linux 和 macOS。', file=sys.stderr)
        return 1
    try:
        text_mode = '--text' in sys.argv[1:]
        if text_mode:
            sys.argv.remove('--text')
        if len(sys.argv) == 1:
            if not text_mode and sys.stdin.isatty() and sys.stdout.isatty():
                from scripts.tui import run as run_tui
                return run_tui()
            return menu()
        if sys.argv[1] in ('-h', '--help'):
            show_help()
            return 0
        if sys.argv[1] in SERVICES:
            if not text_mode and sys.stdin.isatty() and sys.stdout.isatty() and not any(x in sys.argv for x in ('--plain', '--help', '-h', '--yes')):
                from scripts.tui import run as run_tui
                return run_tui(sys.argv[1] if len(sys.argv) == 2 else lambda: run_service(sys.argv[1], sys.argv[2:]))
            return run_service(sys.argv[1], sys.argv[2:])
        if sys.argv[1] in ('configure', 'workspace', 'proxy', 'guide'):
            if not text_mode and sys.stdin.isatty() and sys.stdout.isatty() and not any(x in sys.argv for x in ('--help','-h','status')) and sys.argv[1] != 'guide':
                from scripts.tui import run as run_tui
                return run_tui(lambda: setting(sys.argv[1], sys.argv[2:]))
            return setting(sys.argv[1], sys.argv[2:])
        print('未知命令或参数；使用 uv run main.py --help 查看用法。', file=sys.stderr)
        return 2
    except (EOFError, KeyboardInterrupt):
        print('\n已退出。')
        return 130

if __name__ == '__main__':
    sys.exit(main())
