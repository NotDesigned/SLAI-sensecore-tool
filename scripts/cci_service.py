"""CCI creation and instance actions, scoped to the current user."""
from scripts.ui import output as print
import argparse
import copy
import re
import time
import urllib.parse
import uuid

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
    new_name = ui.ask('新 CCI 名称', default=name[:40] + '-copy-' + uuid.uuid4().hex[:8])
    if not re.fullmatch(r'[a-z][a-z0-9-]{0,61}[a-z0-9]|[a-z]', new_name):
        raise cli.ConfigError('CCI 名称需为小写字母开头的字母、数字或连字符，最长 63 字符。')
    document['display_name'] = new_name
    path = str(plans.save('cci', new_name, document, yaml_format=True))
    from scripts import cci_api
    plan = cci_api.creation_plan(workspace, new_name, document, ','.join(ports))
    plans.save('cci', new_name + '-request', plan)
    print(f'源 CCI：{name} → 新 CCI：{new_name}\n配置文件：{path}\n服务端口：{",".join(ports) or "无"}')
    print('副本沿用原镜像、资源配置和存储挂载；不迁移原 DNAT。')
    cci.preview(document)
    if ui.choose('下一步', ['提交创建', '仅保存配置'], default='提交创建') != '提交创建':
        return
    check_selected(owned_app(config, workspace, name), source)
    cci_api.create(config, plan)
    print('复制创建请求已提交：' + new_name)


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
                   *(['保存为镜像'] if app.get('state') == 'RUNNING' else []), '镜像快照',
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
        elif action in ('保存为镜像', '镜像快照'):
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
    return ui.browse('我的 CCI · ' + workspace['name'], lambda: my_apps(config, workspace), label, selected, plain=plain, actions=actions)



def main(args):
    parser = argparse.ArgumentParser(description='CCI 服务：创建、列出并选择连接 / 保存为镜像 / 启动 / 停止 / 复制 / 删除。')
    parser.add_argument('action', nargs='?', choices=['create', 'create-last', 'list', 'delete'], default='list')
    parser.add_argument('--workspace', help='本次操作的工作空间，省略则使用已保存的默认工作空间')
    parser.add_argument('--name', help='待删除的 CCI 名称，省略则编号选择')
    parser.add_argument('--yes', action='store_true', help='跳过删除确认')
    parser.add_argument('--plain', action='store_true', help='仅打印列表，不进入实例操作页面')
    options = parser.parse_args(args)
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
    apps = my_apps(config, workspace)
    app = next((x for x in apps if x['name'] == options.name), None) if options.name else ui.choose('选择要删除的 CCI', apps, label)
    if app is None:
        raise cli.ConfigError('CCI 不存在或不属于当前用户。')
    print(label(app))
    if options.yes or ui.choose('确认删除此 CCI', ['取消', '删除'], default='取消') == '删除':
        delete_app(config, workspace, app['name'], expected=app)
        print('删除已验证：' + app['name'])
    return 0
