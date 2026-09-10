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
    for section in ('install', 'cci', 'docker'):
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
            from scripts.cci import Cancelled
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


def docker_push(config):
    from scripts.docker_registry import push_image
    push_image(config)


def cci_create(config):
    from scripts.cci import create
    create(config)


def ccr_service(config):
    from scripts.ccr import menu as ccr_menu
    ccr_menu(config)


def cci_service(config):
    from scripts.cci_service import main as cci_main
    cci_main([])


def execute(action):
    from scripts.cci import Cancelled
    try:
        config = load_config(for_init=action in ('install', 'init', 'docker-push', 'ccr'))
        {'install': install_and_configure, 'init': initialize, 'uninstall': uninstall,
         'docker-push': docker_push, 'cci-create': cci_create, 'ccr': ccr_service,
         'cci': cci_service}[action](config)
    except Cancelled:
        print('已返回。')
        return 0
    except (ConfigError, OSError) as error:
        # OSError text may include user-controlled values; keep it generic.
        print(str(error) if isinstance(error, ConfigError) else '无法读取配置或执行命令，请检查路径、权限和依赖。', file=sys.stderr)
        return 1
    return 0


def menu():
    while True:
        print('\nSLAI-tool\n1. 安装并配置 SCO\n2. 卸载 SCO\n3. CCR 服务\n4. CCI 服务\n5. DNAT 服务\n0. 退出')
        choice = input('请选择 [0-5]：').strip()
        if choice == '0':
            return 0
        if choice == '5':
            try:
                from scripts.dnat import main as dnat_main
                dnat_main([])
            except (ConfigError, OSError) as error:
                print(str(error) if isinstance(error, ConfigError) else '无法读取规则文件或配置。', file=sys.stderr)
            continue
        action = {'1': 'install', '2': 'uninstall', '3': 'ccr', '4': 'cci'}.get(choice)
        if action is None:
            print('请输入 0 至 5。')
            continue
        # Reload for every action so edits to config.toml take effect immediately.
        execute(action)


def main():
    if sys.platform not in ('linux', 'darwin', 'win32'):
        print('目前支持 Windows x64、Linux 和 macOS。', file=sys.stderr)
        return 1
    try:
        if len(sys.argv) == 1:
            return menu()
        if sys.argv[1] == 'cci':
            from scripts.cci_service import main as cci_main
            from scripts.cci import Cancelled
            try:
                return cci_main(sys.argv[2:])
            except Cancelled:
                print('已取消 CCI 操作。')
                return 0
            except (ConfigError, OSError) as error:
                print(str(error) if isinstance(error, ConfigError) else '无法读取 CCI 配置或执行命令。', file=sys.stderr)
                return 1
        if sys.argv[1] == 'ccr':
            from scripts.ccr import main as ccr_main
            try:
                return ccr_main(sys.argv[2:])
            except (ConfigError, OSError) as error:
                print(str(error) if isinstance(error, ConfigError) else '无法读取 CCR 配置或执行命令。', file=sys.stderr)
                return 1
        if sys.argv[1] == 'dnat':
            from scripts.dnat import main as dnat_main
            try:
                return dnat_main(sys.argv[2:])
            except (ConfigError, OSError) as error:
                print(str(error) if isinstance(error, ConfigError) else '无法读取规则文件或配置。', file=sys.stderr)
                return 1
        if sys.argv[1] == 'eip':
            from scripts.eip import main as eip_main
            try:
                return eip_main(sys.argv[2:])
            except (ConfigError, OSError) as error:
                print(str(error) if isinstance(error, ConfigError) else '无法执行 EIP 命令，请检查 SCO 配置及组件安装。', file=sys.stderr)
                return 1
        if len(sys.argv) == 2 and sys.argv[1] in ('install', 'init', 'uninstall', 'docker-push', 'cci-create', 'ccr'):
            return execute(sys.argv[1])
        print('用法：uv run main.py [install|init|uninstall|ccr|cci|dnat|docker-push|cci-create]；EIP：uv run main.py eip --zone <可用区> <命令>（无参数打开菜单）', file=sys.stderr)
        return 2
    except (EOFError, KeyboardInterrupt):
        print('\n已退出。')
        return 130


if __name__ == '__main__':
    sys.exit(main())
