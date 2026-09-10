"""Interactive CCI creation using live SCO resource and worker-spec lists."""
import datetime
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import tempfile

import yaml

from scripts import cli

DEFAULT_IMAGE = ('registry.cn-sh-01.sensecore.cn/lepton-trainingjob/'
                 'ngc-pytorch:25.06-cu12.9-py3.12-ubuntu24.04')


class Cancelled(Exception):
    pass


def choose(label, items, describe=str, default=None):
    if not items:
        raise cli.ConfigError(f'{label}没有可选项，请检查权限和上级选择。')
    print(f'\n{label}：')
    for index, item in enumerate(items, 1):
        suffix = ' [默认]' if item == default else ''
        print(f'{index}. {describe(item)}{suffix}')
    if not any(isinstance(item, str) and item == '取消' for item in items):
        print('q. 取消操作')
    while True:
        answer = input('输入编号（默认项可回车）：').strip()
        if answer.lower() == 'q':
            raise Cancelled
        if not answer and default in items:
            return default
        if answer.isascii() and answer.isdecimal() and 1 <= int(answer) <= len(items):
            return items[int(answer) - 1]
        print('请输入有效编号。')


def ask(label, default='', optional=False):
    while True:
        answer = input(label + (f' [{default}]' if default else '') + '：').strip()
        if answer.lower() == 'q':
            raise Cancelled
        answer = answer or default
        if answer or optional:
            return answer
        print('此项不能为空。')


def number(label, default):
    while True:
        value = ask(label, str(default))
        if value.isascii() and value.isdecimal() and int(value) > 0:
            return int(value)
        print('请输入正整数。')


def properties(resource):
    value = resource.get('properties', {})
    try:
        value = json.loads(value) if isinstance(value, str) else value
    except ValueError:
        raise cli.ConfigError('云端资源属性格式无效。') from None
    if not isinstance(value, dict):
        raise cli.ConfigError('云端资源属性格式无效。')
    return value


def resource_label(item):
    return f"{item.get('display_name') or item['name']} ({item['name']}) · {item.get('zone', '')}"


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

    def read(self, args, diagnostics=False):
        try:
            result = subprocess.run(self.command(args), env=self.env, capture_output=True,
                                    text=True, timeout=45)
        except subprocess.TimeoutExpired:
            raise cli.ConfigError('SCO 列表查询超时，请检查网络后重试。') from None
        if result.returncode:
            raise cli.ConfigError(f'SCO 查询失败（退出码 {result.returncode}），请检查权限、配置和网络。')
        # Debug output may contain authentication data. Keep it in memory only;
        # callers must extract the response body and never print/save raw output.
        return result.stdout + '\n' + result.stderr if diagnostics else result.stdout

    def resources(self, resource_type):
        result, seen = [], set()
        for page in range(1, 1001):
            raw = self.read(['srm', 'resources', 'list', '--format', 'json',
                             '--filter', f"resource_type='{resource_type}'",
                             '--page-size', '100', '--page-token', str(page)])
            try:
                items = json.loads(raw)
            except ValueError:
                raise cli.ConfigError('SCO 资源列表不是有效 JSON。') from None
            if not isinstance(items, list) or any(not isinstance(x, dict) or not x.get('name') for x in items):
                raise cli.ConfigError('SCO 资源列表格式无效。')
            fresh = [x for x in items if (x.get('rid') or x.get('id') or x['name']) not in seen]
            if items and not fresh:
                raise cli.ConfigError('SCO 分页重复，停止查询以避免遗漏资源。')
            for item in fresh:
                seen.add(item.get('rid') or item.get('id') or item['name'])
                if item.get('type') == resource_type and not item.get('deleted', False):
                    result.append(item)
            if len(items) < 100:
                return result
        raise cli.ConfigError('资源列表超过分页上限，请缩小 SCO 配置中的资源范围。')

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
                                capture_output=True, text=True, timeout=15)
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
    selected = choose('容器镜像（本地标签不代表已推送或有拉取权限）',
                      [*images, manual], default=default if default in images else manual)
    image = ask('完整远端镜像地址') if selected == manual else selected
    if image.startswith('-') or any(c.isspace() for c in image):
        raise cli.ConfigError('镜像地址格式无效。')
    return image


def select_mounts(client, zone):
    mounts = []
    if choose('挂载存储', ['不挂载', '选择 AFS 存储'], default='选择 AFS 存储') == '不挂载':
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
            volume = choose('AI 文件存储（当前可用区）', volumes, resource_label, default=default_volume)
        path = ask('容器挂载路径', '/data')
        if not path.startswith('/') or any(x['mount_path'] == path for x in mounts):
            raise cli.ConfigError('挂载路径必须是绝对路径，且不能重复。')
        mounts.append({'type': 'PV_AFS', 'id': volume['id'], 'zone': zone,
                       'mount_path': path, 'subdir': ask('存储子目录', '/' + username),
                       'display_name': volume.get('display_name') or volume['name']})
        if choose('继续挂载', ['完成', '再选一个'], default='完成') == '完成':
            return mounts


def prepare(client, defaults):
    workspace = choose('工作空间', client.resources('compute.workspace.v1.instance'), resource_label)
    client.scope(workspace)
    cluster = choose('资源池（已关联当前工作空间）', client.clusters(workspace), resource_label)
    spec = choose('实例规格', client.specs(workspace['name'], cluster['name']),
                  lambda x: f"{x['WORKER SPEC']} · CPU {x['VCPU COUNT']} / 内存 {x['MEMORY(GIB)']} GiB / 加速卡 {x['CHIP COUNT']} · {x['CHIP MODEL']} · {x['ZONE']}")
    zone = spec['ZONE']
    if cluster.get('zone') != zone:
        raise cli.ConfigError('资源池与规格的可用区不一致，请重新查询。')
    vpc = properties(cluster).get('vpc_id')
    if not vpc:
        network = choose('VPC（当前可用区）',
                         [x for x in client.resources('network.vpc.v1.vpc') if x.get('zone') == zone], resource_label)
        vpc = network['id']
    print(f'已自动匹配可用区 {zone}、VPC {vpc}。')
    name = ask('任务名称', 'slai-' + datetime.datetime.now().strftime('%Y%m%d%H%M%S'))
    if not re.fullmatch(r'[a-z][a-z0-9-]{0,61}[a-z0-9]|[a-z]', name):
        raise cli.ConfigError('任务名称须为 1–63 位小写字母、数字或连字符，以字母开头、字母或数字结尾。')
    image = select_image(cli.string_value(defaults, 'image'))
    request = {'cpu': spec['VCPU COUNT'], 'memory': spec['MEMORY(GIB)'] + 'GiB'}
    if int(spec['CHIP COUNT']):
        key = spec.get('RESOURCE KEY')
        if not isinstance(key, str) or not re.fullmatch(r'[A-Za-z0-9.-]+/[A-Za-z0-9_.-]+', key):
            raise cli.ConfigError('所选规格缺少有效资源键，已停止创建。')
        request[key] = spec['CHIP COUNT']
        print(f'已自动获取加速卡资源：{key} = {spec["CHIP COUNT"]}。')
    from scripts import cci_ssh
    ssh = cci_ssh.enabled(defaults)
    if ssh:
        command = cci_ssh.startup(cci_ssh.public_key(defaults), cli.string_value(defaults, 'command'))
    else:
        command = ask('容器启动命令（由 /bin/sh -c 执行）', cli.string_value(defaults, 'command').strip() or 'sleep infinity')
    replicas = number('副本数', 1)
    mounts = select_mounts(client, zone)
    priority = 'NORMAL'
    quota = choose('配额类型', ['RESERVED', 'SPOT'], default='RESERVED')
    ports = '22' if ssh else ask('开放端口（逗号分隔；回车不开放）', optional=True)
    if ports and any(not x.isascii() or not x.isdecimal() or not 1 <= int(x) <= 65535 for x in ports.split(',')):
        raise cli.ConfigError('端口必须为 1–65535 的整数，用逗号分隔。')
    document = {'display_name': name, 'resource_pool': {'name': cluster['name'], 'available_zone': zone, 'vpc_id': vpc},
                'replicas': replicas, 'template': {'containers': [{'name': 'main', 'image_path': image,
                'resource_request': request, 'command': ['/bin/sh', '-c', command], 'env': [], 'volume_mounts': mounts}],
                'resource_spec': {'name': spec['WORKER SPEC']}},
                'scheduling': {'priority': priority, 'quota_type': quota}, 'termination_grace_period_seconds': 30}
    return workspace['name'], name, ports, document


def create(config):
    client = Client(config)
    defaults = config.get('cci', {})
    if not isinstance(defaults, dict):
        raise cli.ConfigError('[cci] 必须是配置表。')
    print('创建 CCI：列表输入编号；输入 q 或按 Ctrl-C 取消。')
    try:
        workspace, name, ports, document = prepare(client, defaults)
        from scripts.cci_network import plan_dnat, attach_dnat
        from scripts import cci_ssh
        if cci_ssh.enabled(defaults):
            cci_ssh.connection_command(defaults, '127.0.0.1', 22)  # Validate proxy config before any submission.
        network = plan_dnat(config, document, ports, ssh_port='22') if cci_ssh.enabled(defaults) else plan_dnat(config, document, ports)
        if network:
            ports = network['ports']
        directory = cli.ROOT / '.cache' / 'cci'
        directory.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', prefix=name + '-',
                                         suffix='.yaml', dir=directory, delete=False) as stream:
            os.chmod(stream.name, 0o600)
            yaml.safe_dump(document, stream, allow_unicode=True, sort_keys=False)
            path = Path(stream.name)
        if network:
            network_path = path.with_suffix('.dnat.json')
            network_path.touch(mode=0o600)
            network_path.write_text(json.dumps({'eip_name': network['eip']['name'],
                'rule': network['body'], 'ports': network['ports'], 'mode': network.get('mode', 'new')}, indent=2, ensure_ascii=False))
            print(f'DNAT 计划已保存：{network_path}')
        print(f'\n工作空间：{workspace}\n端口：{ports or "无"}\n配置文件：{path}')
        print(yaml.safe_dump(document, allow_unicode=True, sort_keys=False))
        args = ['cci', 'apps', 'create', name, '--workspace-name', workspace, '--config', str(path)]
        if ports:
            args.extend(['--ports', ports])
        print('提交命令（需使用 README 中的 SCO 环境变量）：' + shlex.join(client.command(args)))
        if choose('下一步', ['仅保存配置', '提交创建'], default='仅保存配置') == '仅保存配置':
            return
        cli.run(client.command(args), client.env)
        print(f'创建请求已成功。请用 sco cci apps describe {name} --workspace-name {workspace} 查询运行状态。')
        if network:
            attach_dnat(config, client, workspace, name, network)
            if cci_ssh.enabled(defaults):
                p = network['body']['properties']
                cci_ssh.show_connection(defaults, p['external_ip'], p['external_port'], name)
    except Cancelled:
        print('已取消创建 CCI。')
