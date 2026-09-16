"""Shared REST resource discovery for cloud services."""
from scripts.ui import output as print
import json
import math
import re
import shutil
import subprocess
import copy
import urllib.parse
from scripts import cli, ui, rest

DEFAULT_IMAGE = ('registry.cn-sh-01.sensecore.cn/lepton-trainingjob/'
                 'ngc-pytorch:25.06-cu12.9-py3.12-ubuntu24.04')


DEFAULT_CCI_IMAGE = 'registry.cn-sh-01.sensecore.cn/ccr-zhicheng-02/slai-cci-pytorch-ssh:25.06-20260911'

QUOTA_LABELS = {'RESERVED': '预留资源', 'SPOT': '闲时资源'}

# The binding's own wording: a remaining count, and a separately reported spot allowance.
POOL_FIGURES = (('remaining', '剩余卡数'), ('spot', '闲时额度'))

def quota_label(value):
    return QUOTA_LABELS.get(value, value)


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


def card_count(value):
    """Counts arrive as strings; anything unusable stays unknown rather than zero."""
    try:
        count = float(value)
    except (TypeError, ValueError):
        return None
    return count if math.isfinite(count) and count >= 0 else None


def pool_cards(pool):
    # The binding reports one remaining-card figure plus a separate spot allowance.
    # It does not split remaining cards by quota type, so neither do we.
    # A share we cannot read makes the total unknown; dropping it would understate it.
    shares = pool.get('spot_status')
    shares = [card_count((row.get('spot_quota') or {}).get('device')) if isinstance(row, dict) else None
              for row in shares] if isinstance(shares, list) else []
    return {'remaining': card_count(pool.get('reserved_number')),
            'spot': sum(shares) if shares and None not in shares else None}


def card_text(count):
    return str(int(count)) if count == int(count) else f'{count:.2f}'


def pool_order(pool):
    """Most remaining cards first; an unknown count sorts last rather than as zero."""
    remaining = pool_cards(pool)['remaining']
    return (remaining is None, -remaining if remaining is not None else 0, display_name(pool))


def pool_label(pool):
    cards = pool_cards(pool)
    shown = ' / '.join(f'{name} {card_text(cards[key])}' for key, name in POOL_FIGURES if cards[key] is not None)
    return resource_label(pool) + (' · ' + shown if shown else '')


def spec_label(spec):
    return (f"{spec['WORKER SPEC']} · {spec['VCPU COUNT']} CPU / {spec['MEMORY(GIB)']} GiB"
            f" / 加速卡 {spec['CHIP COUNT']}")


def table_describe(columns, rows, cells, right, fallback):
    """Column widths span the whole list, so build the describe callable per picker."""
    header, lines = ui.aligned(columns, [cells(row) for row in rows], right=right)
    pairs = list(zip(rows, lines))  # Rows are unhashable dicts; match on identity.
    return header, lambda row: next((line for item, line in pairs if item is row), fallback(row))


POOL_COLUMNS = ('别名', '名称', '可用区', '剩余卡数', '闲时额度')


def pool_alias(pool):
    # Most aliases only respell the name with underscores. Such an alias says nothing,
    # and giving it a column crowds out the name the user actually picks a pool by.
    alias = (pool.get('display_name') or '').strip()
    respelt = alias.replace('_', '-').casefold() == pool['name'].replace('_', '-').casefold()
    return '' if respelt else alias


def pool_cells(pool):
    cards = pool_cards(pool)
    return (pool_alias(pool), pool['name'], pool.get('zone', ''),
            *(card_text(cards[key]) if cards[key] is not None else '-' for key, _ in POOL_FIGURES))


def pool_table(pools):
    if any(pool_alias(pool) for pool in pools):
        return table_describe(POOL_COLUMNS, pools, pool_cells, (3, 4), pool_label)
    return table_describe(POOL_COLUMNS[1:], pools, lambda pool: pool_cells(pool)[1:], (2, 3), pool_label)


SPEC_COLUMNS = ('名称', 'vCPU', '内存(GiB)', '加速卡', '卡型号')


def spec_cells(spec):
    cards = spec.get('CHIP COUNT', '')
    # A CPU-only specification still carries a chip model; showing it would imply cards.
    model = spec.get('CHIP MODEL', '') if cards not in ('', '0') else ''
    return (spec['WORKER SPEC'], spec.get('VCPU COUNT', ''),
            spec.get('MEMORY(GIB)', ''), cards or '-', model or '-')


def spec_table(specs):
    return table_describe(SPEC_COLUMNS, specs, spec_cells, (1, 2, 3), spec_label)


def scope_path(resource, collection):
    values = [resource.get(key) for key in ('subscription_name', 'resource_group_name', 'zone', 'name')]
    if any(not isinstance(value, str) or not value or value in ('.', '..') or '/' in value for value in values):
        raise cli.ConfigError('资源范围缺少有效的订阅、资源组、可用区或名称。')
    sub, group, zone, name = (urllib.parse.quote(value, safe='') for value in values)
    return f'/subscriptions/{sub}/resourceGroups/{group}/zones/{zone}/{collection}/{name}'


def api_origin(service, resource):
    region = resource.get('region', '')
    if not re.fullmatch(r'cn-[a-z]+-\d+', region):
        raise cli.ConfigError('资源 Region 格式无效。')
    return f'https://{service}.{region}.sensecoreapi.cn'


def decode_specs(data, zone):
    raw, _, _ = rest.list_page(data, 'resource_specs')
    rows, names = [], set()
    for spec in raw:
        if spec['name'] in names:
            raise cli.ConfigError('云端返回重复的规格名称。')
        names.add(spec['name'])
        cpu, memory, device = (spec.get(k) for k in ('cpu', 'memory', 'device'))
        if not all(isinstance(value, dict) for value in (cpu, memory, device)):
            raise cli.ConfigError('云端规格资源详情格式无效。')
        quantities = [cpu.get('vcpu_allocatable'), memory.get('allocatable'), device.get('number')]
        if any(isinstance(n, bool) or not isinstance(n, int) or n < 0 for n in quantities) or not all(quantities[:2]):
            raise cli.ConfigError('云端规格 CPU、内存或加速卡数量无效。')
        if not isinstance(spec.get('zones'), list) or zone not in spec['zones']:
            raise cli.ConfigError('规格与资源池可用区不一致。')
        key = device.get('resource_key', '')
        if quantities[2] and (not isinstance(key, str) or not re.fullmatch(r'[A-Za-z0-9.-]+/[A-Za-z0-9_.-]+', key)):
            raise cli.ConfigError('云端加速卡规格缺少有效资源键。')
        rows.append({'WORKER SPEC': spec['name'], 'ZONE': zone, 'VCPU COUNT': str(quantities[0]),
            'MEMORY(GIB)': str(quantities[1]), 'CHIP COUNT': str(quantities[2]),
            'RESOURCE KEY': key, 'CHIP MODEL': str(device.get('type', '')), 'CPU': str(cpu.get('type', ''))})
    return sorted(rows, key=lambda row: (int(row['CHIP COUNT']), int(row['VCPU COUNT']), row['WORKER SPEC']))


class Client:
    def __init__(self, config):
        self.config = config
        self._catalog = {}
        self._clusters = {}
        self._workspace = None
        self._identity = None

    def identity_data(self):
        if self._identity is None:
            data = rest.get_json(self.config, 'https://iam.sensecoreapi.cn/iam/idp/v1/me')
            rest.identity_id(data)
            self._identity = data
        return self._identity

    def current_username(self):
        identity = self.identity_data()
        username = identity.get('username')
        if (not isinstance(username, str) or not username.strip() or username in ('.', '..')
                or any(c in username for c in '/\\') or any(ord(c) < 32 for c in username)):
            raise cli.ConfigError('无法取得有效的当前用户名，未配置默认 AFS 子目录。')
        return username

    def resources(self, resource_type):
        if not re.fullmatch(r'[a-zA-Z0-9_.]+', resource_type):
            raise cli.ConfigError('资源类型格式无效。')
        scope = self._workspace or {}
        cache_key = (resource_type, scope.get('subscription_name'), scope.get('resource_group_name'))
        if cache_key not in self._catalog:
            base = 'https://management.sensecoreapi.cn/rmh/v1/resources'
            rows = rest.pages(lambda token: rest.get_json(self.config, rest.query_url(base,
                {'filter': f"resource_type='{resource_type}'", 'page_size': 100, 'page_token': token})), 'resources')
            self._catalog[cache_key] = rows
        return [copy.deepcopy(row) for row in self._catalog[cache_key]
                if row.get('type') == resource_type and not row.get('deleted')
                and all(not scope.get(key) or row.get(key) == scope[key]
                        for key in ('subscription_name', 'resource_group_name'))]

    def scope(self, workspace):
        self._workspace = copy.deepcopy(workspace)

    def workspace_record(self, name):
        if self._workspace and self._workspace.get('name') == name:
            return self._workspace
        rows = [row for row in self.resources('compute.workspace.v1.instance') if row['name'] == name]
        if len(rows) != 1:
            raise cli.ConfigError('工作空间不存在或名称不唯一，请重新选择。')
        return rows[0]

    def specs(self, workspace, cluster):
        record = self.workspace_record(workspace)
        matches = [row for row in self.clusters(record) if row['name'] == cluster]
        if len(matches) != 1:
            raise cli.ConfigError('资源池未关联到工作空间，或名称不唯一。')
        pool = matches[0]
        url = api_origin('aec2', pool) + '/compute/aec2/data/v1' + scope_path(pool, 'aec2s') + '/resourceSpecs'
        return decode_specs(rest.get_json(self.config, url), pool['zone'])

    def clusters(self, workspace):
        path = scope_path(workspace, 'workspaces')
        base = api_origin('aec2', workspace) + '/compute/workspace/data/v1' + path + '/workspaceAEC2Bindings'
        if base not in self._clusters:
            rows = rest.pages(lambda token: rest.get_json(self.config, rest.query_url(base,
                              {'page_size': 100, 'page_token': token})), 'aec2s', numbered=True)
            result = []
            names = set()
            for row in rows:
                if row.get('state') != 'ACTIVE':
                    continue
                match = re.fullmatch(r'/subscriptions/([^/]+)/resourceGroups/([^/]+)/zones/([^/]+)/aec2s/([^/]+)', row.get('id', ''))
                if not match or not row.get('uid'):
                    raise cli.ConfigError('关联资源池缺少有效资源 ID，无法确定范围。')
                sub, group, zone, name = map(urllib.parse.unquote, match.groups())
                if (name != row['name'] or name in names or sub != workspace['subscription_name']
                        or group != workspace['resource_group_name']
                        or not re.fullmatch(re.escape(workspace['region']) + r'[a-z]', zone)):
                    raise cli.ConfigError('关联资源池的名称、订阅或可用区不一致。')
                names.add(name)
                result.append({**row, 'region': workspace['region'], 'zone': zone,
                    'subscription_name': sub, 'resource_group_name': group,
                    'properties': {'vpc_id': row.get('vpc_id', '')}})
            result.sort(key=pool_order)
            self._clusters[base] = result
        return copy.deepcopy(self._clusters[base])


def local_images():
    docker = shutil.which('docker')
    if not docker:
        return []
    try:
        result = subprocess.run([docker, 'image', 'ls', '--format', '{{json .}}'],
                                capture_output=True, text=True, timeout=15, encoding='utf-8')
        if result.returncode:
            print('本地 Docker 镜像列表不可用，请检查 Docker 是否启动，或手动指定镜像。')
            return []
        images = []
        for line in result.stdout.splitlines():
            item = json.loads(line)
            repository, tag = item['Repository'], item['Tag']
            if '<none>' not in (repository, tag):
                images.append(repository + ':' + tag)
        return sorted(set(images))
    except (OSError, subprocess.TimeoutExpired, ValueError, KeyError, TypeError):
        print('本地 Docker 镜像列表不可用，请检查 Docker 是否启动，或手动指定镜像。')
        return []


def select_image(default, config, *, catalog=None):
    default = default.strip() or DEFAULT_IMAGE
    search, local = '搜索 CCR 镜像', '选择本地 Docker 标签'
    manual = '手动输入远端镜像地址'
    selected = ui.choose('容器镜像', [search, default, local, manual], default=search)
    upload = None
    if selected == search:
        from scripts.ccr import ImageCatalog
        image = (catalog or ImageCatalog(config)).select(default)
    elif selected == local:
        from scripts import docker_registry
        source = ui.choose('本地 Docker 镜像（提交时自动同步）', local_images())
        upload = docker_registry.plan_sync(config, source)
        image = upload['target']
    else:
        image = ui.ask('完整远端镜像地址') if selected == manual else selected
    if not image or image.startswith('-') or any(c.isspace() for c in image):
        raise cli.ConfigError('镜像地址格式无效。')
    return image, upload


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
