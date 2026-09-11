"""CCI creation and instance actions, scoped to the current user."""
from scripts.ui import output as print
import argparse
import copy
import re
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
                             stop as stop_app, delete as delete_app, resource_url)


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
    if ui.choose('下一步', ['仅保存配置', '提交创建'], default='仅保存配置') != '提交创建':
        return
    check_selected(owned_app(config, workspace, name), source)
    cci_api.create(config, plan)
    print('复制创建请求已提交：' + new_name)


def list_page(config, workspace, plain=False):
    def selected(app):
        action = ui.choose(label(app), ['返回列表', '停止', '复制', '删除'], default='返回列表')
        if action == '复制':
            copy_app(config, workspace, app['name'])
        elif action in ('删除', '停止') and ui.confirm(f'确认{action}此 CCI：' + app['name'], action):
            if action == '停止':
                stop_app(config, workspace, app['name'], expected=app)
            else:
                delete_app(config, workspace, app['name'], expected=app)
                print('删除已验证：' + app['name'])
    actions = [('create','创建 CCI',lambda: cci.create(cli.load_config(), workspace_name=workspace['name']))]
    if config.get('cci', {}).get('last'):
        actions.append(('create-last','按照上次配置',lambda: cci.create(cli.load_config(), workspace_name=workspace['name'],reuse_last=True)))
    return ui.browse('我的 CCI · ' + workspace['name'], lambda: my_apps(config, workspace), label, selected, plain=plain, actions=actions)



def main(args):
    parser = argparse.ArgumentParser(description='CCI 服务：创建、列出并选择停止 / 复制 / 删除。')
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
