"""Shared SCO resource discovery for all cloud services."""
import json
import re
import shutil
import subprocess
from scripts import cli, ui, rest

DEFAULT_IMAGE = ('registry.cn-sh-01.sensecore.cn/lepton-trainingjob/'
                 'ngc-pytorch:25.06-cu12.9-py3.12-ubuntu24.04')


def properties(resource):
    value = resource.get('properties', {})
    try:
        value = json.loads(value) if isinstance(value, str) else value
    except ValueError:
        raise cli.ConfigError('云端资源属性格式无效。') from None
    if not isinstance(value, dict):
        raise cli.ConfigError('云端资源属性格式无效。')
    return value


def display_name(item):
    name, display = item['name'], item.get('display_name')
    return f'{display} ({name})' if display and display != name else name


def resource_label(item):
    return f"{display_name(item)} · {item.get('zone', '')}"


def parse_specs(output):
    # This SCO command only exposes a table. Detect its schema, and merge wrapped
    # lines instead of treating continuation lines as separate specifications.
    headers = None
    rows = []
    for line in output.splitlines():
        if not line.strip().startswith('|'):
            continue
        cells = [cell.strip() for cell in line.strip().strip('|').split('|')]
        if 'WORKER SPEC' in cells:
            headers = cells
            continue
        if headers is None:
            continue
        if len(cells) != len(headers):
            raise cli.ConfigError('SCO 规格列表格式发生变化，无法安全解析。')
        row = dict(zip(headers, cells))
        if row.get('VCPU COUNT'):
            rows.append(row)
        elif rows:
            for key, value in row.items():
                if value:
                    rows[-1][key] += ('' if key in ('WORKER SPEC', 'ZONE') else ' ') + value
    required = {'WORKER SPEC', 'CHIP MODEL', 'CHIP COUNT', 'VCPU COUNT', 'MEMORY(GIB)', 'ZONE'}
    if headers is None or not required.issubset(headers):
        raise cli.ConfigError('SCO 规格列表格式发生变化，无法安全解析。')
    for row in rows:
        if any(not row[key].isascii() or not row[key].isdecimal()
               for key in ('CHIP COUNT', 'VCPU COUNT', 'MEMORY(GIB)')):
            raise cli.ConfigError('SCO 规格资源数量格式无效。')
    return rows


def enrich_specs(output):
    """Read the API body hidden by SCO's table formatter; never emit diagnostics."""
    rows = parse_specs(output)
    by_name = {}
    for line in output.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        message = event.get('message', '') if isinstance(event, dict) else ''
        prefix = 'Response status: 200 OK, body: {Reader:'
        if not isinstance(message, str) or not message.startswith(prefix):
            continue
        try:
            body, _ = json.JSONDecoder().raw_decode(message[len(prefix):])
        except ValueError:
            continue
        if not isinstance(body, dict) or not isinstance(body.get('resource_specs'), list):
            continue
        for spec in body['resource_specs']:
            if not isinstance(spec, dict) or not isinstance(spec.get('name'), str):
                raise cli.ConfigError('云端规格详情格式无效。')
            name = spec['name']
            if name in by_name and by_name[name] != spec:
                raise cli.ConfigError('云端返回了互相冲突的同名规格。')
            by_name[name] = spec
    for row in rows:
        spec = by_name.get(row['WORKER SPEC'])
        if not spec:
            raise cli.ConfigError('SCO 未返回完整规格详情，无法自动获取资源键，请检查 SCO 版本。')
        cpu, memory, device = (spec.get(key) for key in ('cpu', 'memory', 'device'))
        if not all(isinstance(value, dict) for value in (cpu, memory, device)):
            raise cli.ConfigError('云端规格资源详情格式无效。')
        if (cpu.get('vcpu_allocatable') != int(row['VCPU COUNT'])
                or memory.get('allocatable') != int(row['MEMORY(GIB)'])
                or device.get('number') != int(row['CHIP COUNT'])
                or row['ZONE'] not in spec.get('zones', [])):
            raise cli.ConfigError('规格列表与原始资源详情不一致，已停止创建。')
        key = device.get('resource_key', '')
        if int(row['CHIP COUNT']) and (not isinstance(key, str) or not re.fullmatch(r'[A-Za-z0-9.-]+/[A-Za-z0-9_.-]+', key)):
            raise cli.ConfigError('云端加速卡规格缺少有效 resource_key，已停止创建。')
        row['RESOURCE KEY'] = key
    return rows


class Client:
    def __init__(self, config):
        self.config = config
        self.env, self.executable = cli.runtime(config)
        if not self.executable.is_file():
            raise cli.ConfigError('找不到 SCO，请先安装并配置 SCO。')
        self.flags = []
        for key in ('profile', 'region'):
            value = cli.string_value(config['sco'], key)
            if value:
                self.flags.extend(['--' + key, value])

    def command(self, args):
        return [str(self.executable), *self.flags, *args]

    def current_username(self):
        from scripts.rest import get_json
        identity = get_json(self.config, 'https://iam.sensecoreapi.cn/iam/idp/v1/me')
        username = identity.get('username') if isinstance(identity, dict) else None
        if (not isinstance(username, str) or not username.strip() or username in ('.', '..')
                or any(c in username for c in '/\\') or any(ord(c) < 32 for c in username)):
            raise cli.ConfigError('无法取得有效的当前用户名，未配置默认 AFS 子目录。')
        return username

    def read(self, args, diagnostics=False, *, env=None, timeout=45):
        try:
            result = subprocess.run(self.command(args), env=self.env if env is None else env, capture_output=True,
                                    text=True, timeout=timeout, encoding='utf-8')
        except subprocess.TimeoutExpired:
            raise cli.ConfigError('SCO 列表查询超时，请检查网络后重试。') from None
        if result.returncode:
            raise cli.ConfigError(f'SCO 查询失败（退出码 {result.returncode}），请检查权限、配置和网络。')
        # Debug output may contain authentication data. Keep it in memory only;
        # callers must extract the response body and never print/save raw output.
        return result.stdout + '\n' + result.stderr if diagnostics else result.stdout

    def list_json(self, args, *, empty_message=None):
        def fetch(token):
            raw = self.read([*args, '--page-size', '100', '--page-token', token])
            if empty_message is not None and raw.strip() == empty_message:
                rows = []
            else:
                try:
                    rows = json.loads(raw)
                except ValueError:
                    raise cli.ConfigError('SCO 列表不是有效 JSON。') from None
            if not isinstance(rows, list):
                raise cli.ConfigError('SCO 列表格式无效。')
            return {'items': rows, 'next_page_token': str(int(token) + 1) if len(rows) >= 100 else ''}
        return rest.pages(fetch, 'items')

    def resources(self, resource_type):
        rows = self.list_json(['srm', 'resources', 'list', '--format', 'json',
                               '--filter', f"resource_type='{resource_type}'"])
        return [row for row in rows if row.get('type') == resource_type and not row.get('deleted')]

    def scope(self, workspace):
        for key, field in (('subscription', 'subscription_name'), ('resource-group', 'resource_group_name')):
            if workspace.get(field):
                self.flags.extend(['--' + key, workspace[field]])

    def specs(self, workspace, cluster):
        return enrich_specs(self.read(['aec2', 'clusters', 'list-workerspec',
                                       '--workspace-name', workspace, '--aec2-name', cluster,
                                       '--debug'], diagnostics=True))

    def clusters(self, workspace):
        result = []
        workspace_id = workspace.get('uid') or workspace.get('id')
        for resource in self.resources('compute.aec2.v1.instance'):
            # SRM's properties may lag AEC2's current workspace associations.
            raw = self.read(['aec2', 'clusters', 'describe', '--name', resource['name'], '-o', 'json'])
            try:
                cluster = json.loads(raw)
            except ValueError:
                raise cli.ConfigError('SCO 资源池详情不是有效 JSON。') from None
            if not isinstance(cluster, dict) or not cluster.get('name'):
                raise cli.ConfigError('SCO 资源池详情格式无效。')
            if workspace_id in properties(cluster).get('workspace_uids', []) and cluster.get('state') == 'ACTIVE':
                result.append(cluster)
        return result


def local_images():
    docker = shutil.which('docker')
    if not docker:
        return []
    try:
        result = subprocess.run([docker, 'image', 'ls', '--format', '{{json .}}'],
                                capture_output=True, text=True, timeout=15, encoding='utf-8')
        if result.returncode:
            print('本地 Docker 镜像列表不可用，可填写远端镜像地址。')
            return []
        images = []
        for line in result.stdout.splitlines():
            item = json.loads(line)
            repository, tag = item['Repository'], item['Tag']
            # Local-only names and dangling images cannot be pulled by CCI.
            if '/' in repository and ('.' in repository.split('/')[0] or ':' in repository.split('/')[0]):
                if '<none>' not in (repository, tag):
                    images.append(repository + ':' + tag)
        return sorted(set(images))
    except (OSError, subprocess.TimeoutExpired, ValueError, KeyError, TypeError):
        print('本地 Docker 镜像列表不可用，可填写远端镜像地址。')
        return []


def select_image(default):
    default = default.strip() or DEFAULT_IMAGE
    images = local_images()
    if default and default not in images:
        images.insert(0, default)
    manual = '手动输入远端镜像地址'
    selected = ui.choose('容器镜像（本地标签不代表已推送或有拉取权限）',
                      [*images, manual], default=default if default in images else manual)
    image = ui.ask('完整远端镜像地址') if selected == manual else selected
    if image.startswith('-') or any(c.isspace() for c in image):
        raise cli.ConfigError('镜像地址格式无效。')
    return image


def select_mounts(client, zone):
    mounts = []
    if ui.choose('挂载存储', ['不挂载', '选择 AFS 存储'], default='选择 AFS 存储') == '不挂载':
        return mounts
    volumes = [x for x in client.resources('storage.afs.v2.volume') if x.get('zone') == zone]
    username = client.current_username()
    preferred_name = 'afs-share-' + zone.rsplit('-', 1)[-1]
    matches = [v for v in volumes if v['name'] == preferred_name]
    default_volume = matches[0] if len(matches) == 1 else None
    while True:
        if len(volumes) == 1:
            volume = volumes[0]
            print('已自动选择唯一的 AI 文件存储：' + resource_label(volume))
        else:
            volume = ui.choose('AI 文件存储（当前可用区）', volumes, resource_label, default=default_volume)
        path = ui.ask('容器挂载路径', '/data')
        if not path.startswith('/') or any(x['mount_path'] == path for x in mounts):
            raise cli.ConfigError('挂载路径必须是绝对路径，且不能重复。')
        mounts.append({'type': 'PV_AFS', 'id': volume['id'], 'zone': zone,
                       'mount_path': path, 'subdir': ui.ask('存储子目录', '/' + username),
                       'display_name': volume.get('display_name') or volume['name']})
        if ui.choose('继续挂载', ['完成', '再选一个'], default='完成') == '完成':
            return mounts
