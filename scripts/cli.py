#!/usr/bin/env python3
"""SCO operations configured by the repository's single config.toml."""
import os
import getpass
import warnings

import tomlkit
from pathlib import Path
import shutil
import shlex
import subprocess
import sys
import tempfile

from scripts.download_cache import fetch

try:
    import tomllib
except ModuleNotFoundError:
    sys.exit('需要 Python 3.11+；请使用该版本运行 main.py。')

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / 'config.toml'


class ConfigError(Exception):
    pass


def load_config(for_init=False):
    try:
        with CONFIG.open('rb') as stream:
            config = tomllib.load(stream)
    except FileNotFoundError:
        if not for_init:
            raise ConfigError('缺少 config.toml，请复制 config.example.toml 为 config.toml 并填写配置。') from None
        config = tomllib.loads((ROOT / 'config.example.toml').read_text(encoding='utf-8'))
    except tomllib.TOMLDecodeError:
        # Parser errors can contain excerpts of credentials.
        raise ConfigError('config.toml 语法错误，请检查 TOML 格式。') from None
    if for_init:
        config.setdefault('sco', {})
    for name in ('paths', 'sco'):
        if not isinstance(config.get(name), dict):
            raise ConfigError(f'config.toml 缺少 [{name}] 配置表。')
    for section in ('install', 'cci', 'docker', 'acp', 'workspace', 'network'):
        if section in config and not isinstance(config[section], dict):
            raise ConfigError(f'[{section}] 必须是配置表。')
    if 'ssh_proxy' in config.get('cci', {}) or 'use_ssh_proxy' in config.get('acp', {}):
        raise ConfigError('配置结构已更新：请将代理移至 [network.socks5]，ACP 开关改为 network.acp_proxy。')
    return config


def string_value(table, key, required=False):
    value = table.get(key, '')
    if not isinstance(value, str):
        raise ConfigError(f'配置项 {key} 必须是字符串。')
    if required and not value.strip():
        raise ConfigError(f'请在 config.toml 中填写 {key}。')
    return value


def runtime(config):
    env = os.environ.copy()
    for key, variable in (('home', 'SCO_HOME'), ('data_home', 'SCO_DATA_HOME'), ('config', 'SCO_CONFIG')):
        value = string_value(config['paths'], key, required=True)
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = ROOT / path
        env[variable] = str(path.resolve())
    bin_dir = Path(env['SCO_HOME']) / 'bin'
    env['PATH'] = str(bin_dir) + os.pathsep + env.get('PATH', '')
    executable = bin_dir / ('sco.exe' if sys.platform == 'win32' else 'sco')
    return env, executable


def run(command, env):
    # Pass an argv list, never a shell string; never print credential-bearing argv.
    result = subprocess.run(command, env=env, check=False)
    if result.returncode:
        raise ConfigError(f'SCO 操作失败（退出码 {result.returncode}）。')


REQUIRED_COMPONENTS = ('eip', 'ccr')


def install_components(config, env, executable):
    profile = string_value(config['sco'], 'profile').strip() or 'default'
    region = string_value(config['sco'], 'region').strip() or 'cnsh01'
    failures = []
    for component in REQUIRED_COMPONENTS:
        print(f'正在安装 {component.upper()} 组件（Profile：{profile}，Region：{region}）……')
        try:
            run([str(executable), '--profile', profile, '--region', region,
                 'components', 'install', component], env)
        except (ConfigError, OSError):
            failures.append(component)
    if failures:
        raise ConfigError('SCO 已安装/配置，但以下组件安装失败：' + '、'.join(failures)
                          + '。请修复后重新选择“安装并配置 SCO”以重试。')


def install(config):
    if sys.platform not in ('linux', 'darwin', 'win32'):
        raise ConfigError('目前支持 Windows x64、Linux 和 macOS。')
    if sys.platform == 'win32':
        from scripts.windows import install as windows_install
        return windows_install(config)
    env, executable = runtime(config)
    if not shutil.which('curl') or not shutil.which('bash'):
        raise ConfigError('请先安装 curl 和 Bash。')
    with tempfile.TemporaryDirectory(prefix='slai-sco-') as directory:
        cache_value = string_value(config.get('install', {}), 'cache_dir') or 'vendor/sco'
        cache = Path(cache_value).expanduser()
        if not cache.is_absolute():
            cache = ROOT / cache
        cache = cache.resolve()
        curl = shutil.which('curl')
        try:
            installer = fetch('https://sco.sensecore.cn/registry/install.sh', cache, curl, env)
        except RuntimeError as error:
            raise ConfigError(str(error)) from None
        # Preserve the official installation, rollback and checksum logic.
        shim = Path(directory) / 'curl'
        helper = Path(__file__).resolve().with_name('download_cache.py')
        shim.write_text('#!/bin/sh\nexec ' + shlex.quote(sys.executable) + ' '
                        + shlex.quote(str(helper)) + ' "$@"\n', encoding='utf-8')
        shim.chmod(0o700)
        env['SLAI_DOWNLOAD_CACHE'] = str(cache)
        env['SLAI_REAL_CURL'] = curl
        env['PATH'] = str(shim.parent) + os.pathsep + env['PATH']
        run(['bash', str(installer)], env)
    # Discard the temporary curl shim before running installed components.
    env, executable = runtime(config)
    run([str(executable), 'version'], env)
    from scripts.shell_env import configure
    try:
        shell_files = configure(env)
    except ValueError as error:
        raise ConfigError(str(error)) from None
    print('已配置 Bash/Zsh 环境：' + '、'.join(map(str, shell_files)))
    print('请打开新终端生效；当前终端可执行 source ~/.bashrc（Bash）或 source "${ZDOTDIR:-$HOME}/.zshrc"（Zsh）。')
    print(f'SCO CLI 安装完成。命令目录：{executable.parent}')


def install_and_configure(config):
    install(config)
    print('正在配置 SCO……')
    initialize(config)
    print('SCO 安装、配置及所需组件安装完成。')


def save_config_updates(section, updates, original):
    try:
        text = CONFIG.read_text(encoding='utf-8') if CONFIG.exists() else (ROOT / 'config.example.toml').read_text(encoding='utf-8')
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
    # Replace atomically only after all inputs are collected, using owner-only permissions.
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


def save_sco_updates(updates, original):
    return save_config_updates('sco', updates, original)


def choose_region(config):
    regions = config.get('regions')
    if not isinstance(regions, dict) or not regions or any(
        not isinstance(code, str) or not code.strip() or not isinstance(name, str) or not name.strip()
        for code, name in regions.items()
    ):
        raise ConfigError('请在 config.toml 的 [regions] 中配置 Region code 与名称，可参考模板。')
    choices = list(regions.items())
    print('请选择 Region：')
    for number, (code, name) in enumerate(choices, 1):
        print(f'{number}. {name} ({code})')
    print('0. 返回')
    while True:
        value = input(f'输入编号 [0-{len(choices)}]：').strip()
        if value in ('0', 'q'):
            from scripts.ui import Cancelled
            raise Cancelled
        if value.isascii() and value.isdecimal() and 1 <= int(value) <= len(choices):
            return choices[int(value) - 1][0]
        print('请输入列表中的有效编号。')


def complete_sco_config(config):
    settings = config['sco']
    fields = (
        ('access_key_id', 'AccessKey ID', None),
        ('access_key_secret', 'AccessKey Secret', None),
        ('region', 'Region code', None),
        ('zone', '可用区', 'cn-sh-01'),
        ('profile', 'Profile', 'default'),
        ('language', '语言（zh-CN / en-US）', 'zh-CN'),
    )
    # Validate existing values before asking for or saving credentials.
    for key, _, _ in fields:
        string_value(settings, key)
    if settings.get('language', '').strip() and settings['language'] not in ('zh-CN', 'en-US'):
        raise ConfigError('language 只能为 zh-CN 或 en-US。')
    updates = {}
    for key, label, default in fields:
        if settings.get(key, '').strip():
            continue
        if key == 'region':
            updates[key] = choose_region(config)
            continue
        prompt = f'{label}' + (f' [{default}]' if default else '') + '：'
        while True:
            if key == 'access_key_secret':
                try:
                    with warnings.catch_warnings():
                        warnings.simplefilter('error', getpass.GetPassWarning)
                        value = getpass.getpass(prompt)
                except getpass.GetPassWarning:
                    raise ConfigError('当前终端不支持隐藏密钥输入，请在交互终端重试或填写 config.toml。') from None
            else:
                value = input(prompt).strip()
            if not value.strip():
                if default:
                    value = default
                else:
                    print(f'{label} 不能为空，请重新输入。')
                    continue
            if key == 'language' and value not in ('zh-CN', 'en-US'):
                print('语言请选择 zh-CN 或 en-US。')
                continue
            updates[key] = value
            break
    if updates:
        updated = save_sco_updates(updates, settings)
        config.clear()
        config.update(updated)
        print('缺失配置已保存到 config.toml。')


def initialize(config):
    complete_sco_config(config)
    settings = config['sco']
    command_args = []
    for key in ('access_key_id', 'access_key_secret', 'region'):
        command_args += ['--' + key.replace('_', '-'), string_value(settings, key, required=True)]
    language = string_value(settings, 'language', required=True)
    if language not in ('zh-CN', 'en-US'):
        raise ConfigError('language 只能为 zh-CN 或 en-US。')
    command_args += ['--language', language]
    command_args += ['--zone', string_value(settings, 'zone') or 'cn-sh-01']
    profile = string_value(settings, 'profile')
    if profile:
        command_args += ['--profile', profile]
    env, executable = runtime(config)
    if not executable.is_file():
        raise ConfigError('配置的安装目录中找不到 SCO CLI，请先运行安装脚本或修改 paths.home。')
    run([str(executable), 'init', *command_args], env)
    print('SCO 初始化完成；凭据校验和保存后的诊断由 sco init 执行。')
    install_components(config, env, executable)


def uninstall(config):
    env, executable = runtime(config)
    if not executable.is_file():
        raise ConfigError('配置的安装目录中找不到 SCO CLI，无需卸载。')
    print('即将预览卸载 SCO CLI 时删除的本地配置和数据：')
    run([str(executable), 'uninstall', '--dry-run'], env)
    if input('确认卸载以上内容？输入 yes 确认，其他输入取消：').strip().lower() != 'yes':
        print('已取消卸载。')
        return
    run([str(executable), 'uninstall', '--yes'], env)
    print('SCO CLI 已卸载。')


def configure_workspace(config):
    from scripts.workspace import configure
    configure(config)


SERVICES = {'ccr': 'ccr', 'cci': 'cci_service', 'dnat': 'dnat', 'acp': 'acp'}
MENU_ITEMS = (('install', '安装并配置 SCO'), ('uninstall', '卸载 SCO'),
              ('ccr', 'CCR 服务'), ('cci', 'CCI 服务'), ('dnat', 'DNAT 服务'),
              ('acp', 'ACP 服务'), ('workspace', '选择默认工作空间'))


def guarded(operation):
    from scripts.ui import Cancelled
    try:
        return operation() or 0
    except Cancelled:
        print('已取消操作。')
        return 0
    except (ConfigError, OSError) as error:
        print(str(error) if isinstance(error, ConfigError) else
              '无法读取配置或执行命令，请检查路径、权限和依赖。', file=sys.stderr)
        return 1


def run_service(action, args):
    from importlib import import_module
    return guarded(lambda: import_module('scripts.' + SERVICES[action]).main(args))


def execute(action):
    if action in SERVICES:
        return run_service(action, [])

    def operation():
        config = load_config(for_init=action == 'install')
        {'install': install_and_configure, 'uninstall': uninstall,
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
        settings = config['sco']
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
        username, workspace = '未配置或配置无效', '未选择'
    return f'SLAI-tool · 用户：{username} · 工作空间：{workspace}'


def menu():
    identity_cache = {}
    while True:
        print('\n' + menu_title(identity_cache))
        for index, (_, title) in enumerate(MENU_ITEMS, 1):
            print(f'{index}. {title}')
        print('0. 退出')
        choice = input(f'请选择 [0-{len(MENU_ITEMS)}]：').strip().lower()
        if choice in ('0', 'q'):
            return 0
        if not choice.isascii() or not choice.isdecimal() or not 1 <= int(choice) <= len(MENU_ITEMS):
            print(f'请输入 0 至 {len(MENU_ITEMS)}。')
            continue
        execute(MENU_ITEMS[int(choice) - 1][0])


def show_help():
    print('用法：uv run main.py  （打开交互菜单）')
    print('服务：ccr、cci、dnat、acp；在服务名后加 --help 查看操作参数。')
    print('设置：install（安装并配置）、workspace（选择默认工作空间）、uninstall（卸载）')
    print('示例：uv run main.py cci list\n      uv run main.py acp create --workspace 工作空间名称')


def main():
    if sys.platform not in ('linux', 'darwin', 'win32'):
        print('目前支持 Windows x64、Linux 和 macOS。', file=sys.stderr)
        return 1
    try:
        if len(sys.argv) == 1:
            return menu()
        if sys.argv[1] in ('-h', '--help'):
            show_help()
            return 0
        if sys.argv[1] in SERVICES:
            return run_service(sys.argv[1], sys.argv[2:])
        if len(sys.argv) == 2 and sys.argv[1] in ('install', 'uninstall', 'workspace'):
            return execute(sys.argv[1])
        print('未知命令或参数；使用 uv run main.py --help 查看用法。', file=sys.stderr)
        return 2
    except (EOFError, KeyboardInterrupt):
        print('\n已退出。')
        return 130


if __name__ == '__main__':
    sys.exit(main())
