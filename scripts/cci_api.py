"""CCI application and Service lifecycle through REST."""
import copy
import re
import time
import uuid
from scripts import cli, cloud, rest, templates


def resource_url(workspace, name, service=False):
    if not re.fullmatch(r'[a-z][a-z0-9-]{0,62}', name):
        raise cli.ConfigError('CCI 名称格式无效。')
    cloud.api_origin('cci', workspace)
    path = cloud.scope_path(workspace, 'workspaces')
    kind, collection = ('service', 'services') if service else ('cci', 'apps')
    return f"https://cci.{workspace['region']}.sensecore.cn/compute/{kind}/data/v2{path}/{collection}/{name}"


def optional(config, url):
    try:
        return rest.get_json(config, url)
    except rest.RestError as error:
        if error.status == 404:
            return None
        raise


def optional_service(config, url):
    service = optional(config, url)
    # GetService synthesizes an empty view for an app without exposed ports.
    # It is not a persisted Service and has no UID/ID to delete.
    if service and not service.get('uid') and not service.get('id') and service.get('ports') == []:
        return None
    return service


def check_identity(app, expected=None):
    if not isinstance(app, dict) or not app.get('uid'):
        raise cli.ConfigError('CCI 缺少资源 UID，请刷新列表。')
    if expected and (app.get('uid') != expected.get('uid') or app.get('ownership') != expected.get('ownership')):
        raise cli.ConfigError('CCI 身份已变化，请刷新列表。')


def owned(config, workspace, name):
    uid = rest.identity_id(rest.get_json(config, 'https://iam.sensecoreapi.cn/iam/idp/v1/me'))
    app = optional(config, resource_url(workspace, name))
    if not app or app.get('name') != name or app.get('ownership', {}).get('user_id') != uid:
        raise cli.ConfigError('CCI 不存在或不属于当前用户。')
    check_identity(app)
    return app


def creation_plan(workspace, name, document, ports):
    resource_url(workspace, name)
    if ports and any(not p.isascii() or not p.isdecimal() or not 1 <= int(p) <= 65535 for p in ports.split(',')):
        raise cli.ConfigError('容器端口须为 1–65535 的整数，用逗号分隔。')
    return dict(workspace=workspace, name=name, document=copy.deepcopy(document), ports=ports,
                request_ids={'app': str(uuid.uuid4()), 'service': str(uuid.uuid4())})


def create(config, plan):
    workspace, name = plan['workspace'], plan['name']
    url, service_url = resource_url(workspace, name), resource_url(workspace, name, True)
    if optional(config, url) is not None or optional_service(config, service_url) is not None:
        raise cli.ConfigError('同名 CCI 或端口 Service 已存在，未提交创建。')
    uid = rest.identity_id(rest.get_json(config, 'https://iam.sensecoreapi.cn/iam/idp/v1/me'))
    body = templates.writable('cci', plan['document'])
    body['name'] = name
    rest.request_json(config, rest.query_url(url.rsplit('/', 1)[0],
        {'app_name': name, 'request_id': plan['request_ids']['app']}), method='POST', body=body, timeout=90)
    for _ in range(20):
        app = optional(config, url)
        if app:
            check_identity(app)
            if app.get('name') != name or app.get('ownership', {}).get('user_id') != uid:
                raise cli.ConfigError('CCI 创建后身份不一致，请检查云端状态。')
            if app.get('state') in ('FAILED', 'DELETING', 'DELETED'):
                raise cli.ConfigError('CCI 已创建但状态异常，请检查实例。')
            break
        time.sleep(2)
    else:
        raise cli.ConfigError('CCI 已提交，但未查到创建结果；请按名称查询，勿重复创建。')
    if plan['ports']:
        ports = [dict(port=int(p), target_port=int(p)) for p in plan['ports'].split(',')]
        body = dict(name=name, selector={'app': name}, ports=ports)
        try:
            rest.request_json(config, rest.query_url(service_url.rsplit('/', 1)[0],
                {'name': name, 'request_id': plan['request_ids']['service']}), method='POST', body=body, timeout=90)
            for _ in range(20):
                service = optional_service(config, service_url)
                if service:
                    if (service.get('selector') != body['selector'] or not service.get('uid') or
                        sorted((p['port'], p['target_port']) for p in service.get('ports', [])) !=
                        sorted((p['port'], p['target_port']) for p in ports)):
                        raise cli.ConfigError('端口 Service 与请求不一致。')
                    break
                time.sleep(2)
            else:
                raise cli.ConfigError('尚未查到端口 Service。')
        except cli.ConfigError as error:
            raise cli.ConfigError(f'CCI {name} 已创建，但端口 Service 未确认完成：{error} 请查询后处理。') from None
    return app


def stop(config, workspace, name, expected=None):
    app = owned(config, workspace, name)
    check_identity(app, expected)
    if app.get('state') == 'SUSPENDED':
        return
    url = resource_url(workspace, name)
    rest.request_json(config, url + ':stop', method='POST', body={}, timeout=90)
    for _ in range(30):
        current = optional(config, url)
        check_identity(current, app)
        if current.get('state') == 'SUSPENDED':
            return
        time.sleep(2)
    raise cli.ConfigError('停止已提交，尚未确认 SUSPENDED，请稍后刷新列表。')


def delete(config, workspace, name, expected=None):
    app = owned(config, workspace, name)
    check_identity(app, expected)
    url, service_url = resource_url(workspace, name), resource_url(workspace, name, True)
    service = optional_service(config, service_url)
    if service and (not service.get('uid') or service.get('selector') != {'app': name}):
        raise cli.ConfigError('同名端口 Service 身份异常，未删除。')
    rest.request_json(config, url, method='DELETE', timeout=90)
    for _ in range(30):
        current = optional(config, url)
        if current is None:
            break
        check_identity(current, app)
        time.sleep(2)
    else:
        raise cli.ConfigError('删除已提交，CCI 尚未消失，请稍后刷新列表。')
    remaining = optional_service(config, service_url)
    if remaining:
        if not service or remaining.get('uid') != service['uid'] or remaining.get('selector') != {'app': name}:
            raise cli.ConfigError('CCI 已删除，但端口 Service 身份变化，未删除该 Service。')
        rest.request_json(config, service_url, method='DELETE', timeout=90)
        for _ in range(20):
            current = optional_service(config, service_url)
            if current is None:
                return
            if current.get('uid') != service['uid']:
                raise cli.ConfigError('CCI 已删除，但同名 Service 已被替换，请检查。')
            time.sleep(2)
        raise cli.ConfigError('CCI 已删除，端口 Service 删除结果尚未确认。')
