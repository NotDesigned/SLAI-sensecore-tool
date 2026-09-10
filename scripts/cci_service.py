"""CCI creation and instance actions, scoped to the current user."""
import argparse
import copy
import tempfile
import yaml
import re
import time
import urllib.parse
import uuid

from scripts import cli, cci
from scripts.rest import get_json
from scripts.rest import RestError


def my_apps(config, workspace):
    identity = get_json(config, 'https://iam.sensecoreapi.cn/iam/idp/v1/me')
    uid = identity.get('id') if isinstance(identity, dict) else None
    try:
        if not uuid.UUID(uid).int:
            raise ValueError
    except (TypeError, ValueError, AttributeError):
        raise cli.ConfigError('无法确认当前用户，未展示 CCI。') from None
    region = workspace.get('region', '')
    if not re.fullmatch(r'cn-[a-z]+-\d+', region):
        raise cli.ConfigError('工作空间 Region 格式无效。')
    values = [workspace.get(key) for key in ('subscription_name', 'resource_group_name', 'zone', 'name')]
    if not all(isinstance(x, str) and x for x in values):
        raise cli.ConfigError('工作空间缺少完整资源范围。')
    sub, group, zone, name = [urllib.parse.quote(x, safe='') for x in values]
    base = f'https://cci.{region}.sensecore.cn/compute/cci/data/v2/subscriptions/{sub}/resourceGroups/{group}/zones/{zone}/workspaces/{name}/apps'
    result, seen, count, token = [], set(), 0, '1'
    for _ in range(1000):
        data = get_json(config, base + '?' + urllib.parse.urlencode({'page_size': 100, 'page_token': token}))
        rows = data.get('apps') if isinstance(data, dict) else None
        if not isinstance(rows, list):
            raise cli.ConfigError('CCI 列表格式无效。')
        for row in rows:
            if not isinstance(row, dict) or not row.get('name') or row['name'] in seen:
                raise cli.ConfigError('CCI 列表无效或分页重复。')
            seen.add(row['name'])
            ownership = row.get('ownership', {})
            if isinstance(ownership, dict) and ownership.get('user_id') == uid:
                result.append(row)
        count += len(rows)
        total = data.get('total_size')
        if isinstance(total, int) and count >= total:
            return result
        following = data.get('next_page_token')
        if following not in (None, '', '0'):
            token = str(following)
        elif isinstance(total, int) and count < total:
            raise cli.ConfigError('CCI 列表不完整，未返回下一页标识。')
        else:
            return result
    raise cli.ConfigError('CCI 列表超过分页上限。')


def label(app):
    return f"{app['name']} · {app.get('display_name', '')} · {app.get('state')} · 就绪 {app.get('ready_replicas', 0)}/{app.get('replicas', 0)}"


def delete_app(config, workspace, name, expected=None):
    app = owned_app(config, workspace, name)
    check_selected(app, expected)
    client = cci.Client(config)
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
    if expected is not None and (app.get('uid') != expected.get('uid')
                                 or app.get('ownership') != expected.get('ownership')):
        raise cli.ConfigError('CCI 身份已变化，请刷新列表后重试。')


def stop_app(config, workspace, name, expected=None):
    app = owned_app(config, workspace, name)
    check_selected(app, expected)
    if app.get('state') == 'SUSPENDED':
        print('此 CCI 已停止。')
        return
    client = cci.Client(config)
    client.scope(workspace)
    cli.run(client.command(['cci', 'apps', 'stop', name, '--workspace-name', workspace['name']]), client.env)
    for _ in range(10):
        if owned_app(config, workspace, name).get('state') == 'SUSPENDED':
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
    new_name = cci.ask('新 CCI 名称', default=name[:40] + '-copy-' + uuid.uuid4().hex[:8])
    if not re.fullmatch(r'[a-z][a-z0-9-]{0,61}[a-z0-9]|[a-z]', new_name):
        raise cli.ConfigError('CCI 名称需为小写字母开头的字母、数字或连字符，最长 63 字符。')
    directory = cli.ROOT / '.cache' / 'cci'
    directory.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', prefix=new_name + '-',
                                     suffix='.yaml', dir=directory, delete=False) as stream:
        yaml.safe_dump(document, stream, allow_unicode=True, sort_keys=False)
        path = stream.name
    print(f'源 CCI：{name} → 新 CCI：{new_name}\n配置文件：{path}\n服务端口：{",".join(ports) or "无"}')
    print('副本沿用原镜像、资源配置和存储挂载；不迁移原 DNAT。')
    print(yaml.safe_dump(document, allow_unicode=True, sort_keys=False))
    if cci.choose('下一步', ['仅保存配置', '提交创建'], default='仅保存配置') != '提交创建':
        return
    check_selected(owned_app(config, workspace, name), source)
    try:
        get_json(config, resource_url(workspace, new_name))
    except RestError as error:
        if error.status != 404:
            raise
    else:
        raise cli.ConfigError('新 CCI 名称已存在，未提交复制。')
    client = cci.Client(config)
    client.scope(workspace)
    args = ['cci', 'apps', 'create', new_name, '--workspace-name', workspace['name'], '--config', path]
    if ports:
        args.extend(['--ports', ','.join(ports)])
    cli.run(client.command(args), client.env)
    print('复制创建请求已提交：' + new_name)


def list_page(config, workspace):
    while True:
        apps = my_apps(config, workspace)
        print(f"\n工作空间：{workspace['name']} · 共 {len(apps)} 个当前用户的 CCI。")
        for i, app in enumerate(apps, 1):
            print(f'{i}. {label(app)}')
        print('r. 刷新\n0. 返回')
        value = input('选择 CCI：').strip()
        if value in ('0', 'q'):
            return
        if value == 'r':
            continue
        if not value.isascii() or not value.isdecimal() or not 1 <= int(value) <= len(apps):
            print('请输入列表中的编号。')
            continue
        app = apps[int(value) - 1]
        try:
            action = cci.choose(label(app), ['返回列表', '停止', '复制', '删除'], default='返回列表')
            if action == '复制':
                copy_app(config, workspace, app['name'])
            elif action in ('删除', '停止'):
                if cci.choose(f'确认{action}此 CCI：' + app['name'], ['取消', action], default='取消') == action:
                    if action == '停止':
                        stop_app(config, workspace, app['name'], expected=app)
                    else:
                        delete_app(config, workspace, app['name'], expected=app)
                        print('删除已验证：' + app['name'])
        except cci.Cancelled:
            print('已取消操作。')
        except cli.ConfigError as error:
            print(error)


def service_menu():
    while True:
        print('\nCCI 服务\n1. 创建\n2. 列出（停止 / 复制 / 删除）\n0. 返回')
        value = input('请选择 [0-2]：').strip()
        if value in ('0', 'q'):
            return 0
        if value not in ('1', '2'):
            print('请输入 0 至 2。')
            continue
        try:
            main(['create' if value == '1' else 'list'])
        except cci.Cancelled:
            print('已取消操作。')
        except (cli.ConfigError, OSError) as error:
            print(str(error) if isinstance(error, cli.ConfigError) else '无法读取配置或执行命令。')


def main(args):
    parser = argparse.ArgumentParser(description='CCI 服务：创建、列出并选择停止 / 复制 / 删除。')
    parser.add_argument('action', nargs='?', choices=['create', 'list', 'delete'])
    parser.add_argument('--workspace', help='列出/删除的工作空间，省略则编号选择')
    parser.add_argument('--name', help='待删除的 CCI 名称，省略则编号选择')
    parser.add_argument('--yes', action='store_true', help='跳过删除确认')
    parser.add_argument('--plain', action='store_true', help='仅打印列表，不进入实例操作页面')
    options = parser.parse_args(args)
    if not options.action:
        return service_menu()
    config = cli.load_config()
    if options.action == 'create':
        cci.create(config)
        return 0
    client = cci.Client(config)
    workspaces = client.resources('compute.workspace.v1.instance')
    if options.workspace:
        matches = [x for x in workspaces if x['name'] == options.workspace]
        if len(matches) != 1:
            raise cli.ConfigError('工作空间不存在或名称不唯一，请交互选择。')
        workspace = matches[0]
    else:
        workspace = cci.choose('工作空间', workspaces, cci.resource_label)
    if options.action == 'list' and not options.plain:
        list_page(config, workspace)
        return 0
    apps = my_apps(config, workspace)
    if options.action == 'list':
        for i, app in enumerate(apps, 1):
            print(f'{i}. {label(app)}')
        print(f'共 {len(apps)} 个当前用户的 CCI。')
    else:
        app = next((x for x in apps if x['name'] == options.name), None) if options.name else cci.choose('选择要删除的 CCI', apps, label)
        if app is None:
            raise cli.ConfigError('CCI 不存在或不属于当前用户。')
        print(label(app))
        if options.yes or cci.choose('确认删除此 CCI', ['取消', '删除'], default='取消') == '删除':
            delete_app(config, workspace, app['name'], expected=app)
            print('删除已验证：' + app['name'])
    return 0
