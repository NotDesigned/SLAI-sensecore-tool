"""Push a local Docker image to SenseCore CCR using config.toml."""
from scripts.ui import output as print
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
    from scripts import ui
    if ui.active():
        return ui.ask(label, default)
    while True:
        value = input(label + (f' [{default}]' if default else '') + '：').strip()
        if value or default:
            return value or default
        print(f'{label} 不能为空。')


def select_local_image(default=''):
    from scripts import cloud, ui
    print('正在读取本地 Docker 镜像…')
    images = cloud.local_images()
    manual = '手动填写镜像名称或 ID'
    if not images:
        print('没有可列出的本地标签。请确认 Docker 已启动且已构建或拉取镜像，也可手动填写镜像 ID。')
        return ui.ask('本地镜像（名称:标签或 ID）', default)
    preferred = default if default in images else images[0]
    selected = ui.choose('选择本地 Docker 镜像（可搜索名称或标签）', [*images, manual], default=preferred)
    return ui.ask('本地镜像（名称:标签或 ID）', default) if selected == manual else selected


def upload_defaults(source):
    """Replace the source registry/namespace, retaining repository and tag."""
    if re.fullmatch(r'(?:sha256:)?[a-f0-9]{12,64}', source) or '@' in source:
        return '', 'latest'
    repository, separator, tag = source.rpartition(':')
    if not separator or '/' in tag:
        repository, tag = source, 'latest'
    first, separator, rest = repository.partition('/')
    if separator and ('.' in first or ':' in first or first == 'localhost'):
        repository = rest
    if '/' in repository:
        repository = repository.split('/', 1)[1]
    return repository, tag


def complete_config(config):
    original = config.get('docker', {})
    if not isinstance(original, dict):
        raise ConfigError('[docker] 必须是配置表。')
    settings = dict(original)
    registry = string_value(settings, 'registry')
    if not registry.strip():
        settings['registry'] = ask('Registry 地址', 'registry.cn-sh-01.sensecore.cn')
    from scripts.ccr import select_upload_namespace
    settings['namespace'] = select_upload_namespace(config, settings['registry'], string_value(settings, 'namespace'))
    # Last-used values are defaults, never a reason to skip per-upload choices.
    settings['source_image'] = select_local_image(string_value(settings, 'source_image'))
    defaults = dict(zip(('image_name', 'tag'), upload_defaults(settings['source_image'])))
    for key, label in (('image_name', '目标镜像名称'), ('tag', '目标镜像标签')):
        default = defaults[key]
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
        from scripts import ui
        if ui.active():
            password = ui.secret('Registry 客户端登录密码')
            break
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
                            env=env, input=password + '\n', text=True, encoding='utf-8', capture_output=ui.active())
    if result.returncode:
        raise ConfigError('Docker 登录失败，上传流程已停止。')
    # Docker manages persistence via its credential store, not config.toml.


def run_push(docker, target, env):
    tail = []
    with subprocess.Popen([docker, 'push', target], env=env, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, text=True, encoding='utf-8') as process:
        for line in process.stdout:
            # Registry storage errors can contain short-lived signed URLs.
            print(re.sub(r'(https?://[^\s?]+)\?[^\s]+', r'\1?[redacted]', line), end='', flush=True)
            tail.append(line.lower())
            tail = tail[-30:]
        code = process.wait()
    message = ''.join(tail)
    auth_failed = any(marker in message for marker in
                      ('unauthorized', 'authentication required', 'no basic auth credentials'))
    return code, auth_failed


def inspect_local(source):
    docker = shutil.which('docker')
    if not docker:
        raise ConfigError('请先安装并启动 Docker。')
    try:
        result = subprocess.run([docker, 'image', 'inspect', '--format',
            '{{json .Id}} {{json .Os}} {{json .Architecture}}', source],
            capture_output=True, text=True, encoding='utf-8', timeout=20)
        if result.returncode:
            raise ValueError
        image_id, system, arch = [json.loads(x) for x in result.stdout.strip().split()]
        if not re.fullmatch(r'sha256:[a-f0-9]{64}', image_id):
            raise ValueError
    except (ValueError, OSError, subprocess.TimeoutExpired):
        raise ConfigError('本地镜像不可用，请检查 Docker 服务与镜像标签。') from None
    if (system, arch) != ('linux', 'amd64'):
        raise ConfigError('所选镜像不是 linux/amd64，不能用于当前资源池；请用 docker buildx build --platform linux/amd64 --load 重新构建。')
    return image_id


def plan_sync(config, source):
    """Resolve an explicit target without tagging, uploading, or logging secrets."""
    image_id = inspect_local(source)
    repository, tag = upload_defaults(source)
    settings = dict(config.get('docker', {}))
    registry = string_value(settings, 'registry') or 'registry.cn-sh-01.sensecore.cn'
    from scripts.ccr import select_upload_namespace
    namespace = select_upload_namespace(config, registry, string_value(settings, 'namespace'))
    settings.update(registry=registry, namespace=namespace, source_image=source,
                    image_name=repository, tag=tag)
    validate(settings)
    target = f'{registry}/{namespace}/{repository}:{tag}'
    print(f'提交时将自动同步：{source} → {target}；保留镜像名称和标签，任务使用同步后的地址。')
    return {key:settings[key] for key in ('registry','namespace','source_image','image_name','tag')} | dict(source_id=image_id, target=target)


def sync_image(config, plan):
    """Upload only the exact local image the user reviewed; fail before creation."""
    validate(plan)
    target = f"{plan['registry']}/{plan['namespace']}/{plan['image_name']}:{plan['tag']}"
    if target != plan['target'] or inspect_local(plan['source_image']) != plan['source_id']:
        raise ConfigError('本地镜像或同步目标已变化，请重新选择镜像后提交。')
    settings = dict(plan, username=string_value(config.get('docker', {}), 'username'))
    upload(settings, plan['source_id'], config)
    return target


def upload(settings, source, config):
    """Shared authentication, exact-source tagging, upload and cache invalidation."""
    from scripts import ui
    docker, env = shutil.which('docker'), os.environ.copy()
    target = f"{settings['registry']}/{settings['namespace']}/{settings['image_name']}:{settings['tag']}"
    saved_login = has_credentials(settings['registry'], env)
    if not saved_login:
        login(docker, settings, env)
    ui.mark_changed()
    print(f'上传镜像：{source} → {target}', flush=True)
    result = subprocess.run([docker, 'tag', source, target], capture_output=True, env=env)
    if result.returncode:
        raise ConfigError('Docker tag 失败，上传流程已停止。')
    code, auth_failed = run_push(docker, target, env)
    if code and auth_failed and saved_login:
        login(docker, settings, env)
        code, _ = run_push(docker, target, env)
    if code:
        raise ConfigError(f'镜像上传失败（退出码 {code}），未创建任务。')
    from scripts.ccr_cache import invalidate
    invalidate(config, settings['registry'], settings['namespace'])
    print('镜像上传完成：' + target)


def push_image(config):
    docker = shutil.which('docker')
    if not docker:
        raise ConfigError('找不到 docker，请先安装 Docker 并启动 Docker 服务。')
    settings = complete_config(config)
    source = settings['source_image']
    target = f"{settings['registry']}/{settings['namespace']}/{settings['image_name']}:{settings['tag']}"
    env = os.environ.copy()
    result = subprocess.run([docker, 'image', 'inspect', source], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if result.returncode:
        raise ConfigError('本地镜像检查失败，请检查镜像名称和 Docker 服务。')
    upload(settings, source, config)
