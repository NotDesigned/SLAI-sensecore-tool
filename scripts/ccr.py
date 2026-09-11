"""CCR service menu and accessible repository listing through REST."""
import argparse
import re
import urllib.parse

from scripts import cli, rest
from scripts.rest import get_json
from scripts.ui import Cancelled, choose
from scripts.docker_registry import push_image


def pages(config, base, field, *, camel=False):
    def fetch(token):
        query = {'pageSize': 100, 'pageToken': token} if camel else {'page_size': 100, 'page_token': token}
        separator = '&' if '?' in base else '?'
        return get_json(config, base + separator + urllib.parse.urlencode(query))
    return rest.pages(fetch, field)


def namespaces(config):
    base = 'https://management.sensecoreapi.cn/rmh/v1/resources?' + urllib.parse.urlencode(
        {'filter': "resource_type='devtools.ccr.v1.namespace'"})
    return [row for row in pages(config, base, 'resources')
            if row.get('type') == 'devtools.ccr.v1.namespace' and not row.get('deleted')]


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


def list_images(config, namespace_name=None):
    available = namespaces(config)
    if namespace_name:
        matches = [row for row in available if row['name'] == namespace_name]
        if len(matches) != 1:
            raise cli.ConfigError('命名空间不存在或名称不唯一，请使用交互选择。')
        namespace = matches[0]
    else:
        namespace = choose('可访问的 CCR 命名空间', available,
            lambda n: f"{n['name']} · {n.get('display_name') or n['name']} · {n.get('region')}")
    print(f"范围：可访问命名空间 {namespace['name']} 内的镜像（不代表由当前用户创建）。", flush=True)
    print('正在通过 REST 查询；接口可能一次返回全部仓库，最多等待 120 秒……', flush=True)
    rows = repositories(config, namespace)
    for index, row in enumerate(rows, 1):
        print(f"{index}. " + ' | '.join(image_references(row)))
    print(f"共 {len(rows)} 个可访问镜像仓库。")


def menu(config=None):
    from scripts.ui import menu as service_menu
    return service_menu('CCR 服务', main, (('upload', '上传镜像'), ('list', '列出可访问镜像')))


def main(args):
    parser = argparse.ArgumentParser(description='CCR 服务：上传镜像或通过 REST 列出可访问镜像。')
    parser.add_argument('action', nargs='?', choices=['upload', 'list'])
    parser.add_argument('--namespace', help='列表查询的命名空间；省略则编号选择')
    options = parser.parse_args(args)
    if not options.action:
        menu()
    else:
        config = cli.load_config(for_init=True)
        if options.action == 'upload':
            push_image(config)
        else:
            try:
                list_images(config, options.namespace)
            except Cancelled:
                print('已取消查询。')
    return 0
