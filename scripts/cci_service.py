"""CCI creation and instance actions, scoped to the current user."""
from scripts.ui import output as print
import copy
import re
import time
import urllib.parse

from scripts.rest import get_json
from scripts.rest import RestError
from scripts import ui, cloud, cli, cci, rest, plans


def my_apps(config, workspace):
    identity = get_json(config, 'https://iam.sensecoreapi.cn/iam/idp/v1/me')
    uid = rest.identity_id(identity)
    region = workspace.get('region', '')
    if not re.fullmatch(r'cn-[a-z]+-\d+', region):
        raise cli.ConfigError('工作空间 Region 格式无效。')
    values = [workspace.get(key) for key in ('subscription_name', 'resource_group_name', 'zone', 'name')]
    if not all(isinstance(x, str) and x for x in values):
        raise cli.ConfigError('工作空间缺少完整资源范围。')
    sub, group, zone, name = [urllib.parse.quote(x, safe='') for x in values]
    base = f'https://cci.{region}.sensecore.cn/compute/cci/data/v2/subscriptions/{sub}/resourceGroups/{group}/zones/{zone}/workspaces/{name}/appsOwn'
    rows = rest.pages(lambda token: get_json(config, base + '?' + urllib.parse.urlencode(
        {'page_size': 100, 'page_token': token})), 'apps')
    return [row for row in rows if isinstance(row.get('ownership'), dict) and row['ownership'].get('user_id') == uid]


def label(app):
    return f"{cloud.display_name(app)} · {app.get('state')} · 就绪 {app.get('ready_replicas', 0)}/{app.get('replicas', 0)}"


# Shared identity checks also protect DNAT target binding.
from scripts.cci_api import (owned as owned_app, check_identity as check_selected,
                             start as start_app, stop as stop_app, delete as delete_app, resource_url)


def copy_document(source):
    fields = ('display_name', 'resource_pool', 'replicas', 'template', 'scheduling', 'elastic_scaling',
              'rolling_update_strategy', 'termination_grace_period_seconds')
    if not all(source.get(k) for k in ('resource_pool', 'template')):
        raise cli.ConfigError('源 CCI 缺少资源池或容器模板，未提交复制。')
    return {k: copy.deepcopy(source[k]) for k in fields if k in source}


def copy_app(config, workspace, name):
    owned = owned_app(config, workspace, name)
    source = get_json(config, resource_url(workspace, name))
    if not isinstance(source, dict):
        raise cli.ConfigError('源 CCI 详情格式无效。')
    if (source.get('ownership') != owned.get('ownership') or source.get('uid') != owned.get('uid')):
        raise cli.ConfigError('源 CCI 已变化，请刷新列表后重试。')
    document = copy_document(source)
    try:
        service = get_json(config, resource_url(workspace, name, service=True))
    except RestError as error:
        if error.status != 404:
            raise
        service = {'ports': []}
    if not isinstance(service, dict) or not isinstance(service.get('ports'), list):
        raise cli.ConfigError('源服务端口格式无效，未提交复制。')
    ports = []
    for port in service['ports']:
        if not isinstance(port, dict):
            raise cli.ConfigError('源服务端口格式无效，未提交复制。')
        value = port.get('port')
        if (not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 65535
                or port.get('target_port', value) != value
                or str(port.get('protocol', 'TCP')).upper() != 'TCP'):
            raise cli.ConfigError('源服务含非等值端口映射或非 TCP 端口，当前复制无法完整保留，未提交。')
        ports.append(str(value))
    from scripts.copy_draft import CopyDraft
    from scripts import cci_api
    from scripts.cci_network import attach_dnat
    client = cloud.Client(config)
    client.scope(workspace)
    draft = CopyDraft('cci', client, workspace, document, name, ','.join(ports))
    _, new_name, ports, document, network = ui.creation_form(draft)
    path = plans.save('cci', new_name, document, yaml_format=True)
    plan = cci_api.creation_plan(workspace, new_name, document, ports)
    plans.save('cci', new_name + '-request', plan)
    if network:
        plans.save('cci', new_name + '-dnat', network)
    print(f'复制配置已保存：{path}')
    if draft.save_requested:
        return
    check_selected(owned_app(config, workspace, name), source)
    draft.sync_image()
    cci_api.create(config, plan)
    print('复制创建请求已提交：' + new_name)
    if network:
        attach_dnat(config, client, workspace['name'], new_name, network)


def bound_tcp_rule(row, app):
    from scripts.network import validate_destination
    props = row.get('properties', {})
    if (not isinstance(props, dict) or not app.get('uid') or not row.get('uid')
            or row.get('deleted') or row.get('state') != 'ACTIVE'
            or props.get('internal_instance_type') != 'CCI_DEPLOYMENT_SERVICE'
            or props.get('internal_instance_name') != app['uid']
            or str(props.get('protocol', '')).lower() != 'tcp'):
        return False
    try:
        validate_destination(props.get('external_ip'), props.get('external_port'))
        validate_destination(props.get('external_ip'), props.get('internal_port'))
    except cli.ConfigError:
        return False
    return True


def connection_entries(config, workspace, app):
    from concurrent.futures import ThreadPoolExecutor
    from scripts import dnat
    current = owned_app(config, workspace, app['name'])
    check_selected(current, app)
    if current.get('state') != 'RUNNING':
        return []
    pool = current.get('resource_pool') or {}
    if not pool.get('vpc_id') or not pool.get('available_zone'):
        return []
    eips = [e for e in cloud.Client(config).resources('network.eip.v1.eip')
            if e.get('zone') == pool['available_zone']
            and e.get('subscription_name') == workspace['subscription_name']
            and cloud.properties(e).get('vpc_id') == pool['vpc_id']]
    def read(eip):
        api = dnat.Api(config, eip)
        # A visible rule may have been bound to this user's CCI by an administrator.
        return [(api, row) for row in api.list() if bound_tcp_rule(row, current)]
    with ThreadPoolExecutor(max_workers=3) as executor:
        pages = list(executor.map(read, eips))
    return sorted([entry for page in pages for entry in page], key=lambda entry: (
        str(entry[1]['properties']['internal_port']) != '22',
        entry[1]['properties']['external_ip'], int(entry[1]['properties']['external_port'])))


def connect_app(config, workspace, app, entries):
    from scripts import cci_ssh
    api, selected = entries[0] if len(entries) == 1 else ui.choose('选择 SSH 连接入口', entries,
        lambda entry: f"{entry[1]['properties']['external_ip']}:{entry[1]['properties']['external_port']} → 容器端口 {entry[1]['properties']['internal_port']}")
    current = owned_app(config, workspace, app['name'])
    check_selected(current, app)
    if current.get('state') != 'RUNNING':
        raise cli.ConfigError('CCI 已不在运行中，请刷新列表。')
    # GetDNATRule currently loses the CCI target type (UNSPECIFIED), while
    # ListDNATRules preserves it. Recheck only the selected EIP via that view.
    rule = next((row for row in api.list() if row.get('name') == selected['name']), None)
    def route(row):
        props = row.get('properties', {})
        return tuple(str(props.get(key, '')).lower() if key == 'protocol' else str(props.get(key, ''))
                     for key in ('external_ip', 'external_port', 'internal_port', 'protocol',
                                 'internal_instance_type', 'internal_instance_name', 'internal_ip', 'eip_id'))
    if (not isinstance(rule, dict) or rule.get('uid') != selected['uid']
            or route(rule) != route(selected) or not bound_tcp_rule(rule, current)):
        raise cli.ConfigError('DNAT 绑定或端口已变化，请返回列表重新选择，未生成旧入口的连接命令。')
    props = rule['properties']
    cci_ssh.show_connection(config, props['external_ip'], props['external_port'], app['name'])


def list_page(config, workspace, plain=False):
    # Scoped to this account/workspace/list session. Cached routes are discovery
    # hints only; connect_app always revalidates the selected rule and CCI.
    entry_cache = {}
    def selected(app):
        choices = ['返回列表', *(['连接'] if app.get('state') == 'RUNNING' else []),
                   *(['保存为镜像'] if app.get('state') == 'RUNNING' else []), '已保存的镜像',
                   '启动' if app.get('state') == 'SUSPENDED' else '停止', '复制', '删除']
        action = ui.choose(label(app), choices, default='返回列表')
        if action == '连接':
            key = (app['name'], app['uid'])
            try:
                cached = entry_cache.get(key)
                if cached and time.monotonic() - cached[0] < 30:
                    entries = cached[1]
                else:
                    print('正在查找此 CCI 的连接入口…')
                    entries = connection_entries(config, workspace, app)
                    if entries:
                        entry_cache[key] = (time.monotonic(), entries)
                if not entries:
                    raise cli.ConfigError('此 CCI 没有可用的 TCP DNAT 入口，请先在 DNAT 服务中绑定规则。')
                connect_app(config, workspace, app, entries)
            except (cli.ConfigError, OSError):
                entry_cache.pop(key, None)
                raise
        elif action == '启动':
            start_app(config, workspace, app['name'], expected=app)
            print('启动请求已确认，请在列表刷新运行状态：' + app['name'])
        elif action in ('保存为镜像', '已保存的镜像'):
            from scripts import cci_snapshot
            if action == '保存为镜像':
                cci_snapshot.create_interactive(config, workspace, app)
            else:
                cci_snapshot.list_page(config, workspace, app)
        elif action == '复制':
            copy_app(config, workspace, app['name'])
        elif action in ('删除', '停止'):
            message = f'确认{action}此 CCI：' + app['name']
            if action == '停止':
                message += '\n需要保留的容器内修改请先保存为镜像，或写入挂载存储；启动不会恢复临时文件。'
            if not ui.confirm(message, action):
                return
            if action == '停止':
                stop_app(config, workspace, app['name'], expected=app)
            else:
                delete_app(config, workspace, app['name'], expected=app)
                print('删除已验证：' + app['name'])
    actions = [('create','创建 CCI',lambda: cci.create(cli.load_config(), workspace_name=workspace['name']))]
    actions.append(('create-last','按照上次配置',lambda: cci.create(cli.load_config(), workspace_name=workspace['name'],reuse_last=True)))
    from scripts.listing import resource_key
    return ui.browse('我的 CCI · ' + workspace['name'], lambda: my_apps(config, workspace), label, selected, plain=plain, actions=actions, cache_key=resource_key('cci',config,workspace))



def main(args):
    parser = cli.service_parser('cci', {
        'list':'列出本人 CCI；选择实例可进入操作菜单', 'create':'创建配置表',
        'create-last':'回填上次配置后编辑创建', 'describe':'打印实例详情', 'connect':'查找 DNAT 并显示 SSH 命令',
        'start':'启动已停止的 CCI', 'stop':'停止 CCI', 'copy':'回填源模板，在配置表编辑后创建',
        'delete':'删除 CCI 及仍归属它的端口 Service', 'snapshot':'将运行容器保存为镜像',
        'snapshots':'列出已保存的镜像及保存状态'},
        'CCI 服务：通过 REST 管理本人实例。', '示例：uv run main.py --text cci copy --name my-cci')
    parser.add_argument('--workspace', help='工作空间名称；省略则使用默认项或交互选择')
    parser.add_argument('--name', help='目标 CCI 的完整名称；省略则交互选择（不用于创建命名）')
    parser.add_argument('--yes', action='store_true', help='仅 stop/delete：跳过确认，仍执行身份检查')
    parser.add_argument('--plain', action='store_true', help='仅 list/snapshots：打印列表后退出')
    options = parser.parse_args(args)
    if options.yes and options.action not in ('stop','delete'):
        parser.error('--yes 仅适用于 stop/delete')
    if options.plain and options.action not in ('list','snapshots'):
        parser.error('--plain 仅适用于 list/snapshots')
    if options.name and options.action in ('create','create-last','list'):
        parser.error('--name 用于指定操作目标；创建名称请在配置表中填写')
    config = cli.load_config()
    if options.action in ('create','create-last'):
        cci.create(config, workspace_name=options.workspace, reuse_last=options.action=='create-last')
        return 0
    client = cloud.Client(config)
    from scripts.workspace import select
    workspace = select(client, explicit=options.workspace)
    if options.action == 'list':
        list_page(config, workspace, plain=options.plain)
        return 0
    app = owned_app(config, workspace, options.name) if options.name else ui.choose(
        '选择目标 CCI', my_apps(config, workspace), label)
    action = options.action
    if action == 'describe':
        import json
        ui.show_text('CCI 详情', json.dumps(app, ensure_ascii=False, indent=2))
    elif action == 'copy':
        copy_app(config, workspace, app['name'])
    elif action == 'start':
        start_app(config, workspace, app['name'], expected=app)
        print('启动已验证：' + app['name'])
    elif action == 'connect':
        entries = connection_entries(config, workspace, app)
        if not entries:
            raise cli.ConfigError('没有可用的连接入口；请确认 CCI 正在运行，并在 DNAT 服务绑定 TCP 规则。')
        connect_app(config, workspace, app, entries)
    elif action in ('snapshot','snapshots'):
        from scripts import cci_snapshot
        if action == 'snapshot':
            cci_snapshot.create_interactive(config, workspace, app)
        else:
            cci_snapshot.list_page(config, workspace, app, plain=options.plain)
    elif options.yes or ui.confirm('确认' + ('停止' if action == 'stop' else '删除') + '此 CCI：' + app['name'] +
            ('\n需要保留的容器内修改请先保存为镜像或写入挂载存储。' if action == 'stop' else ''), '停止' if action == 'stop' else '删除'):
        (stop_app if action == 'stop' else delete_app)(config, workspace, app['name'], expected=app)
        print(('停止' if action == 'stop' else '删除') + '已验证：' + app['name'])
    return 0
