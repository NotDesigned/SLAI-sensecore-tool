"""CCR service menu and accessible repository listing through REST."""
from scripts.ui import output as print
import re
import urllib.parse

from scripts import cli, rest, ui
from scripts.rest import get_json
from scripts.ui import Cancelled, choose
from scripts.docker_registry import push_image
from scripts.listing import LocalSource


def pages(config, base, field, *, camel=False):
    def fetch(token):
        query = {'pageSize': 100, 'pageToken': token} if camel else {'page_size': 100, 'page_token': token}
        separator = '&' if '?' in base else '?'
        return get_json(config, base + separator + urllib.parse.urlencode(query), timeout=120)
    return rest.pages(fetch, field)


def namespaces(config):
    base = 'https://management.sensecoreapi.cn/rmh/v1/resources?' + urllib.parse.urlencode(
        {'filter': "resource_type='devtools.ccr.v1.namespace'"})
    return [row for row in pages(config, base, 'resources')
            if row.get('type') == 'devtools.ccr.v1.namespace' and not row.get('deleted')]


def select_upload_namespace(config, registry, default=''):
    """Select a visible active namespace matching the destination registry region.

    SRM visibility is not a push permission check; Registry remains authoritative.
    """
    match = re.fullmatch(r'registry\.(cn-[a-z]+-\d+)\.sensecore\.cn', registry)
    rows = [row for row in namespaces(config) if row.get('state') == 'ACTIVE'
            and (not match or row.get('region') == match.group(1))]
    if not rows:
        raise cli.ConfigError('此 Registry 下没有可访问且处于 ACTIVE 状态的 CCR 命名空间，请检查区域和权限。')
    preferred = next((row for row in rows if row['name'] == default), None)
    selected = choose('目标命名空间（当前账号可访问；推送权限由 Registry 校验）', rows,
        lambda row: f"{row['name']} · {row.get('region', '')}" +
                    (f" · {row['display_name']}" if row.get('display_name') and row['display_name'] != row['name'] else ''),
        default=preferred)
    return selected['name']


def repositories(config, namespace):
    region = namespace.get('region', '')
    if not re.fullmatch(r'cn-[a-z]+-\d+', region):
        raise cli.ConfigError('命名空间 Region 格式无效。')
    fields = [namespace.get(key) for key in ('subscription_name', 'resource_group_name', 'zone', 'name')]
    if not all(isinstance(value, str) and value for value in fields):
        raise cli.ConfigError('命名空间缺少订阅、资源组、可用区或名称。')
    sub, group, zone, name = [urllib.parse.quote(value, safe='') for value in fields]
    base = f'https://ccr.{region}.sensecoreapi.cn/devtools/ccr/data/v1/subscriptions/{sub}/resourceGroups/{group}/zones/{zone}/namespaces/{name}/repositories'
    return pages(config, base, 'repositories', camel=True)


def image_references(row):
    name = row['name']  # REST name already includes the namespace.
    domain = row.get('domain', '').rstrip('/')
    repository = domain + '/' + name if domain else name
    tags = row.get('tags', [])
    if not isinstance(tags, list) or any(not isinstance(tag, str) for tag in tags):
        raise cli.ConfigError('CCR 镜像标签格式无效。')
    return [repository + ':' + tag for tag in tags] or [repository + '（无标签）']


class RepositorySource(LocalSource):
    def __init__(self, config, namespace, *, images=False):
        self.config, self.namespace, self.images = config, namespace, images
        self.force_refresh = False
        self.status_hint = ''
        super().__init__(self.read,
            str if images else lambda row: ' '.join(image_references(row)),
            columns=('镜像与标签',) if images else ('镜像仓库','标签数'),
            cells=(lambda ref:(ref.split('/',2)[-1],)) if images else
                  (lambda row:(row['name'],str(len(row.get('tags',[]))))))
        self.search_hint = '输入镜像名或标签，Enter 搜索'
        self.loading_hint = '正在读取镜像；缓存未命中或更新云端时可能需要 30–120 秒'
        self.refresh_after_action = False
        self.reuse_cache_after_action = True
        self.refresh_label = '更新云端'

    def read(self):
        from datetime import datetime
        from scripts import ccr_cache
        rows, timestamp, cached = ccr_cache.load(self.config,self.namespace,
            lambda: repositories(self.config,self.namespace),refresh=self.force_refresh)
        self.status_hint = ('本地缓存' if cached else '云端已更新') + ' · ' + datetime.fromtimestamp(timestamp).strftime('%m-%d %H:%M:%S')
        if not self.images:
            return rows
        refs=[]
        for row in rows:
            values=image_references(row)
            if row.get('tags'):
                if not row.get('domain'):
                    raise cli.ConfigError('CCR 镜像缺少 Registry 地址，请刷新重试。')
                refs.extend(values)
        return sorted(set(refs))

    def page(self, index, size, query='', state='', refresh=False):
        self.force_refresh = refresh
        try:
            return super().page(index,size,query,state,refresh)
        finally:
            self.force_refresh = False


class ImageCatalog:
    """One creation draft's accessible namespaces and searchable tag snapshots."""
    def __init__(self, config):
        self.config, self.available, self.selected = config, None, None
        self.sources = {}

    def source(self, namespace):
        key = tuple(namespace.get(k) for k in ('region', 'subscription_name', 'resource_group_name', 'zone', 'name'))
        if key not in self.sources:
            self.sources[key] = RepositorySource(self.config, namespace, images=True)
        return self.sources[key]

    def select(self, default=''):
        if self.available is None:
            ui.output('正在读取可访问的 CCR 命名空间…')
            self.available = [row for row in namespaces(self.config) if row.get('state') == 'ACTIVE']
        preferred = self.selected or next((row for row in self.available
            if default.startswith(f"registry.{row['region']}.sensecore.cn/{row['name']}/")), None)
        namespace = self.available[0] if len(self.available) == 1 else choose(
            '搜索范围：可访问的 CCR 命名空间', self.available,
            lambda row: f"{row['name']} · {row.get('display_name') or row['name']} · {row.get('region')}",
            default=preferred)
        self.selected = namespace
        return ui.select_resource('选择镜像 · ' + namespace['name'] + ' · 当前账号可访问', self.source(namespace))


def list_images(config, namespace_name=None, plain=False):
    if namespace_name:
        available = namespaces(config)
        matches = [row for row in available if row['name'] == namespace_name]
        if len(matches) != 1:
            raise cli.ConfigError('命名空间不存在或名称不唯一，请使用交互选择。')
        namespace = matches[0]
    else:
        return ui.browse('CCR · 可访问的命名空间', lambda: namespaces(config),
            lambda n: f"{n['name']} · {n.get('display_name') or n['name']} · {n.get('region')}",
            lambda n: list_images(config, n['name']), plain=plain,
            actions=(('upload','上传镜像',lambda: push_image(cli.load_config())),))
    print(f"范围：可访问命名空间 {namespace['name']} 内的镜像（不代表由当前用户创建）。", flush=True)
    source = RepositorySource(config, namespace)
    if ui.active() and not plain:
        return ui.backend().browse('CCR · ' + namespace['name'] + ' · 可访问镜像', source,
            lambda row: ui.show_text(row['name'], '\n'.join(image_references(row))),
            actions=(('upload','上传镜像',lambda: push_image(cli.load_config())),))
    rows = source.read()
    print(source.status_hint)
    for index, row in enumerate(rows, 1):
        print(f"{index}. " + ' | '.join(image_references(row)))
    print(f"共 {len(rows)} 个可访问镜像仓库。")


def main(args):
    parser = cli.service_parser('ccr', {'list':'列出可访问的命名空间或指定命名空间内的镜像',
        'upload':'交互选择本地 Docker 镜像、命名空间和目标名称后上传'},
        'CCR 镜像服务。范围是当前账号可访问，并非仅本人创建。',
        '示例：uv run main.py ccr list --plain --namespace my-namespace')
    parser.add_argument('--namespace', help='列表查询的命名空间；省略则编号选择')
    parser.add_argument('--plain', action='store_true', help='仅 list：打印后退出；未指定 --namespace 时列出命名空间')
    options = parser.parse_args(args)
    if options.action == 'upload' and (options.namespace or options.plain):
        parser.error('--namespace/--plain 仅适用于 list；上传命名空间在交互中选择')
    config = cli.load_config(for_setup=True)
    if options.action == 'upload':
        push_image(config)
    else:
        try:
            list_images(config, options.namespace, plain=options.plain)
        except Cancelled:
            print('已取消查询。')
    return 0
