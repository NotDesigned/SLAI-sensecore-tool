"""Push a local Docker image to SenseCore CCR using config.toml."""
import getpass
import json
from pathlib import Path
from urllib.parse import urlsplit
import os
import re
import shutil
import subprocess
import warnings

from scripts.cli import ConfigError, save_config_updates, string_value

COMPONENT = r'[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*'


def validate(settings):
    for key in ('registry', 'namespace', 'source_image', 'image_name', 'tag'):
        string_value(settings, key, required=True)
    if not re.fullmatch(r'[a-zA-Z0-9](?:[a-zA-Z0-9.-]*[a-zA-Z0-9])?(?::[0-9]{1,5})?', settings['registry']):
        raise ConfigError('docker.registry 应填写主机名及可选端口，不含 https:// 或路径。')
    if not re.fullmatch(COMPONENT, settings['namespace']):
        raise ConfigError('docker.namespace 应为小写命名空间名称。')
    if not re.fullmatch(COMPONENT + r'(?:/' + COMPONENT + ')*', settings['image_name']):
        raise ConfigError('docker.image_name 应为小写镜像名称，可包含仓库子路径。')
    if not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}', settings['tag']):
        raise ConfigError('docker.tag 格式无效。')
    if settings['source_image'].startswith('-') or any(c.isspace() for c in settings['source_image']):
        raise ConfigError('docker.source_image 应为本地镜像名称:标签或镜像 ID。')


def ask(label, default=''):
    while True:
        value = input(label + (f' [{default}]' if default else '') + '：').strip()
        if value or default:
            return value or default
        print(f'{label} 不能为空。')


def complete_config(config):
    original = config.get('docker', {})
    if not isinstance(original, dict):
        raise ConfigError('[docker] 必须是配置表。')
    settings = dict(original)
    registry = string_value(settings, 'registry')
    if not registry.strip():
        settings['registry'] = ask('Registry 地址', 'registry.cn-sh-01.sensecore.cn')
    # Last-used values are defaults, never a reason to skip per-upload questions.
    for key, label in (('namespace', '目标命名空间'), ('source_image', '本地镜像（名称:标签或 ID）'),
                       ('image_name', '目标镜像名称'), ('tag', '目标镜像标签')):
        default = string_value(settings, key) or ('latest' if key == 'tag' else '')
        settings[key] = ask(label, default)
    validate(settings)
    updates = {key: settings[key] for key in ('registry', 'namespace', 'source_image', 'image_name', 'tag')
               if settings[key] != original.get(key, '')}
    if updates:
        updated = save_config_updates('docker', updates, original)
        config.clear()
        config.update(updated)
        settings = config['docker']
        validate(settings)
    return settings


def has_credentials(registry, env):
    directory = Path(env.get('DOCKER_CONFIG') or Path.home() / '.docker')
    try:
        data = json.loads((directory / 'config.json').read_text(encoding='utf-8'))
    except FileNotFoundError:
        return False
    except (OSError, ValueError):
        raise ConfigError('无法读取 Docker 登录配置，请检查 DOCKER_CONFIG 或 ~/.docker/config.json。') from None
    if not isinstance(data, dict):
        raise ConfigError('Docker 登录配置格式无效。')
    helpers = data.get('credHelpers', {})
    auths = data.get('auths', {})
    if not isinstance(helpers, dict) or not isinstance(auths, dict):
        raise ConfigError('Docker 登录配置格式无效。')
    helper = helpers.get(registry) or data.get('credsStore')
    if helper:
        if not isinstance(helper, str):
            raise ConfigError('Docker credential helper 配置格式无效。')
        executable = shutil.which('docker-credential-' + helper)
        if not executable:
            raise ConfigError('找不到 Docker 配置指定的 credential helper，请修复 Docker 安装。')
        try:
            result = subprocess.run([executable, 'get'], input=registry, text=True,
                                    capture_output=True, env=env, timeout=15, encoding='utf-8')
        except subprocess.TimeoutExpired:
            raise ConfigError('读取 Docker 凭据超时，请检查系统钥匙串是否解锁。') from None
        if result.returncode:
            return False
        try:
            entry = json.loads(result.stdout)
            return isinstance(entry, dict) and bool(entry.get('Username') and entry.get('Secret'))
        except ValueError:
            raise ConfigError('Docker credential helper 返回格式无效。') from None
    for address, entry in auths.items():
        host = urlsplit(address if '://' in address else 'https://' + address).netloc
        if host == registry and isinstance(entry, dict):
            return bool(entry.get('auth') or entry.get('identitytoken'))
    return False


def login(docker, settings, env):
    username = ask('Registry 用户名', string_value(settings, 'username'))
    while True:
        try:
            with warnings.catch_warnings():
                warnings.simplefilter('error', getpass.GetPassWarning)
                password = getpass.getpass('Registry 客户端登录密码：')
        except getpass.GetPassWarning:
            raise ConfigError('当前终端不能隐藏密码，请在交互终端重试。') from None
        if password.strip():
            break
        print('密码不能为空。')
    result = subprocess.run([docker, 'login', settings['registry'], '--username', username, '--password-stdin'],
                            env=env, input=password + '\n', text=True, encoding='utf-8')
    if result.returncode:
        raise ConfigError('Docker 登录失败，上传流程已停止。')
    # Docker manages persistence via its credential store, not config.toml.


def run_push(docker, target, env):
    tail = []
    with subprocess.Popen([docker, 'push', target], env=env, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, text=True, encoding='utf-8') as process:
        for line in process.stdout:
            print(line, end='', flush=True)
            tail.append(line.lower())
            tail = tail[-30:]
        code = process.wait()
    message = ''.join(tail)
    auth_failed = any(marker in message for marker in
                      ('unauthorized', 'authentication required', 'no basic auth credentials'))
    return code, auth_failed


def push_image(config):
    docker = shutil.which('docker')
    if not docker:
        raise ConfigError('找不到 docker，请先安装 Docker 并启动 Docker 服务。')
    settings = complete_config(config)
    source = settings['source_image']
    target = f"{settings['registry']}/{settings['namespace']}/{settings['image_name']}:{settings['tag']}"
    env = os.environ.copy()
    result = subprocess.run([docker, 'image', 'inspect', source], env=env, stdout=subprocess.DEVNULL)
    if result.returncode:
        raise ConfigError('本地镜像检查失败，请检查镜像名称和 Docker 服务。')
    saved_login = has_credentials(settings['registry'], env)
    if not saved_login:
        print('Docker 未保存此 Registry 的可用凭据，请登录。')
        login(docker, settings, env)
    print(f'上传镜像：{source} → {target}', flush=True)
    result = subprocess.run([docker, 'tag', source, target], env=env)
    if result.returncode:
        raise ConfigError('Docker tag 失败，上传流程已停止。')
    code, auth_failed = run_push(docker, target, env)
    if code and auth_failed and saved_login:
        print('已有凭据未通过认证，请重新登录。')
        login(docker, settings, env)
        code, _ = run_push(docker, target, env)
    if code:
        raise ConfigError(f'Docker push 失败（退出码 {code}）。')
    print(f'镜像上传完成：{target}')
