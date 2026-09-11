"""CCI creation and instance actions, scoped to the current user."""
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


def delete_app(config, workspace, name, expected=None):
    app = owned_app(config, workspace, name)
    check_selected(app, expected)
    client = cloud.Client(config)
    client.scope(workspace)
    cli.run(client.command(['cci', 'apps', 'delete', name, '--workspace-name', workspace['name']]), client.env)
    for _ in range(10):
        if not any(app['name'] == name for app in my_apps(config, workspace)):
            return
        time.sleep(2)
    raise cli.ConfigError('删除已提交，CCI 尚未从列表消失，请稍后列出确认。')


def owned_app(config, workspace, name):
    app = next((x for x in my_apps(config, workspace) if x['name'] == name), None)
    if app is None:
        raise cli.ConfigError('CCI 不存在或不属于当前用户。')
    return app


def check_selected(app, expected):
    if not app.get('uid'):
        raise cli.ConfigError('CCI 缺少资源 UID，请刷新列表后重试。')
    if expected is not None and (app.get('uid') != expected.get('uid')
                                 or app.get('ownership') != expected.get('ownership')):
        raise cli.ConfigError('CCI 身份已变化，请刷新列表后重试。')


def stop_app(config, workspace, name, expected=None):
    app = owned_app(config, workspace, name)
    check_selected(app, expected)
    if app.get('state') == 'SUSPENDED':
        print('此 CCI 已停止。')
        return
    client = cloud.Client(config)
    client.scope(workspace)
    cli.run(client.command(['cci', 'apps', 'stop', name, '--workspace-name', workspace['name']]), client.env)
    for _ in range(10):
        current = owned_app(config, workspace, name)
        check_selected(current, app)
        if current.get('state') == 'SUSPENDED':
            print('停止已验证：' + name)
            return
        time.sleep(2)
    raise cli.ConfigError('停止已提交，尚未确认 SUSPENDED，请稍后刷新列表。')


def resource_url(workspace, name, service=False):
    sub, group, zone, ws, name = [urllib.parse.quote(workspace[k] if k else name, safe='')
                                for k in ('subscription_name', 'resource_group_name', 'zone', 'name', None)]
    kind, collection = ('service', 'services') if service else ('cci', 'apps')
    return (f"https://cci.{workspace['region']}.sensecore.cn/compute/{kind}/data/v2/"
            f'subscriptions/{sub}/resourceGroups/{group}/zones/{zone}/workspaces/{ws}/{collection}/{name}')


def copy_document(source):
    fields = ('display_name', 'resource_pool', 'replicas', 'template', 'scheduling',
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
    print(f'源 CCI：{name} → 新 CCI：{new_name}\n配置文件：{path}\n服务端口：{",".join(ports) or "无"}')
    print('副本沿用原镜像、资源配置和存储挂载；不迁移原 DNAT。')
    cci.preview(document)
    if ui.choose('下一步', ['仅保存配置', '提交创建'], default='仅保存配置') != '提交创建':
        return
    check_selected(owned_app(config, workspace, name), source)
    try:
        get_json(config, resource_url(workspace, new_name))
    except RestError as error:
        if error.status != 404:
            raise
    else:
        raise cli.ConfigError('新 CCI 名称已存在，未提交复制。')
    client = cloud.Client(config)
    client.scope(workspace)
    args = ['cci', 'apps', 'create', new_name, '--workspace-name', workspace['name'], '--config', path]
    if ports:
        args.extend(['--ports', ','.join(ports)])
    cli.run(client.command(args), client.env)
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
    return ui.browse('我的 CCI · ' + workspace['name'], lambda: my_apps(config, workspace), label, selected, plain=plain)


def service_menu():
    from scripts.ui import menu
    return menu('CCI 服务', main, (('create', '创建'), ('list', '列出（停止 / 复制 / 删除）')))


def main(args):
    parser = argparse.ArgumentParser(description='CCI 服务：创建、列出并选择停止 / 复制 / 删除。')
    parser.add_argument('action', nargs='?', choices=['create', 'list', 'delete'])
    parser.add_argument('--workspace', help='本次操作的工作空间，省略则使用已保存的默认工作空间')
    parser.add_argument('--name', help='待删除的 CCI 名称，省略则编号选择')
    parser.add_argument('--yes', action='store_true', help='跳过删除确认')
    parser.add_argument('--plain', action='store_true', help='仅打印列表，不进入实例操作页面')
    options = parser.parse_args(args)
    if not options.action:
        return service_menu()
    config = cli.load_config()
    if options.action == 'create':
        if options.workspace:
            cci.create(config, workspace_name=options.workspace)
        else:
            cci.create(config)
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
