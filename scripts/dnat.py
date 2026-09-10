"""Create, list and delete DNAT rules with HTTP and read-back validation."""
import argparse
import base64
import email.utils
import hashlib
import hmac
import json
from pathlib import Path
import re
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from scripts import cli
from scripts.cci import Client, Cancelled, choose, ask, properties


class ApiError(cli.ConfigError):
    def __init__(self, status, body):
        self.status, self.body = status, body
        # Do not echo response bodies: authentication errors may include secrets.
        super().__init__(f'DNAT 接口失败（HTTP {status}）。请检查权限、配置和参数。')


class Api:
    def __init__(self, config, eip):
        self.credentials = config['sco']
        self.eip = eip
        region = eip.get('region', '')
        if not re.fullmatch(r'cn-[a-z]+-\d+', region):
            raise cli.ConfigError('EIP Region 格式无效。')
        fields = [eip.get(k) for k in ('subscription_name', 'resource_group_name', 'zone', 'name')]
        if not all(isinstance(x, str) and x for x in fields):
            raise cli.ConfigError('EIP 缺少订阅、资源组、可用区或名称。')
        sub, group, zone, name = map(lambda x: urllib.parse.quote(x, safe=''), fields)
        self.scope = f'/subscriptions/{sub}/resourceGroups/{group}/zones/{zone}/eips/{name}/dnatRules'
        self.base = f'https://network.{region}.sensecoreapi.cn/network/eip/data/v1' + self.scope

    def request(self, method, suffix='', body=None, *, identity=False):
        date = email.utils.formatdate(usegmt=True)
        key = cli.string_value(self.credentials, 'access_key_id', required=True)
        secret = cli.string_value(self.credentials, 'access_key_secret', required=True)
        signature = base64.b64encode(hmac.new(secret.encode(), ('x-date: ' + date).encode(), hashlib.sha256).digest()).decode()
        auth = f'hmac accesskey="{key}", algorithm="hmac-sha256", headers="x-date", signature="{signature}"'
        url = 'https://iam.sensecoreapi.cn/iam/idp/v1/me' if identity else self.base + suffix
        request = urllib.request.Request(url, method=method,
            data=None if body is None else json.dumps(body).encode(),
            headers={'X-Date': date, 'Authorization': auth, 'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                raw = response.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as error:
            try:
                body = json.load(error)
            except ValueError:
                body = {}
            raise ApiError(error.code, body) from None
        except (urllib.error.URLError, TimeoutError):
            raise cli.ConfigError('DNAT 请求超时或网络异常；写入结果可能未知，请先列出规则再重试。') from None
        except ValueError:
            raise cli.ConfigError('DNAT 返回格式异常，请重新查询确认状态。') from None

    def current_user_id(self):
        data = self.request('GET', identity=True)
        user_id = data.get('id') if isinstance(data, dict) else None
        try:
            parsed = uuid.UUID(user_id)
            if not parsed.int:
                raise ValueError
        except (ValueError, TypeError, AttributeError):
            raise cli.ConfigError('无法确认当前用户 ID，未展示或选择任何规则。') from None
        return user_id

    def list(self):
        result, seen, token = [], set(), '1'
        for _ in range(1000):
            data = self.request('GET', '?' + urllib.parse.urlencode({'page_size': 100, 'page_token': token}))
            rows = data.get('dnat_rules') if isinstance(data, dict) else None
            if not isinstance(rows, list) or any(not isinstance(x, dict) or not x.get('name') for x in rows):
                raise cli.ConfigError('DNAT 列表格式无效。')
            for row in rows:
                if row['name'] in seen:
                    raise cli.ConfigError('DNAT 分页重复，未能取得完整列表。')
                seen.add(row['name'])
                result.append(row)
            total = data.get('total_size')
            if isinstance(total, int) and len(result) >= total:
                return [x for x in result if not x.get('deleted')]
            following = data.get('next_page_token')
            if following not in (None, '', '0'):
                token = str(following)
            elif total is not None and len(result) < total:
                if not rows:
                    raise cli.ConfigError('DNAT 分页提前结束。')
                token = str(int(token) + 1)
            else:
                return [x for x in result if not x.get('deleted')]
        raise cli.ConfigError('DNAT 分页超出上限。')


def port_range(value):
    if not isinstance(value, str) or not re.fullmatch(r'\d+(?:-\d+)?', value, re.ASCII):
        raise cli.ConfigError('端口应为数字或端口段，例如 22222 或 22000-22009。')
    parts = list(map(int, value.split('-')))
    low, high = parts[0], parts[-1]
    if not 1 <= low <= high <= 65535 or high - low >= 500:
        raise cli.ConfigError('端口范围无效，端口段最多包含 500 个端口。')
    return low, high


def random_free_port(rows, low=20000, high=65535):
    """Choose an unused high port from the EIP's complete visible rule list."""
    occupied = set()
    for row in rows:
        if row.get('deleted'):
            continue
        properties = row.get('properties', {})
        start, end = port_range(properties.get('external_port'))
        occupied.update(range(max(start, low), min(end, high) + 1))
    available = [port for port in range(low, high + 1) if port not in occupied]
    if not available:
        raise cli.ConfigError('该 EIP 的随机端口范围已无空闲端口，请选择其他 EIP。')
    return str(secrets.choice(available))


def prepare(api, template, existing):
    if not isinstance(template, dict) or not isinstance(template.get('properties'), dict):
        raise cli.ConfigError('JSON 必须是含 properties 的规则对象。')
    body = json.loads(json.dumps(template))
    name = body.get('name', '')
    if not re.fullmatch(r'[a-z][a-z0-9-]{0,61}[a-z0-9]|[a-z]', name):
        raise cli.ConfigError('规则名称须为 1–63 位小写字母、数字或连字符，以字母开头。')
    for key in ('creator_id', 'owner_id', 'tenant_id'):
        try:
            uuid.UUID(body[key])
        except (KeyError, ValueError, TypeError, AttributeError):
            raise cli.ConfigError(f'必须提供有效 {key}；缺失归属信息会造成规则无法由本人删除。') from None
    p = body['properties']
    if p.get('internal_instance_name') or p.get('internal_ip'):
        raise cli.ConfigError('当前创建功能仅创建未绑定规则，请清空目标实例和内部 IP；绑定不在本次功能范围内。')
    protocol = p.get('protocol', 'tcp')
    if not isinstance(protocol, str):
        raise cli.ConfigError('协议必须是字符串。')
    p['protocol'] = protocol.lower()
    if p['protocol'] not in ('tcp', 'udp'):
        raise cli.ConfigError('协议只能是 tcp 或 udp。')
    low, high = port_range(p.get('external_port'))
    ilow, ihigh = port_range(p.get('internal_port'))
    if high - low != ihigh - ilow:
        raise cli.ConfigError('内部和外部端口段长度必须一致。')
    for row in existing:
        if row['name'] == name:
            raise cli.ConfigError('同名规则已存在，不会覆盖。')
        other = row.get('properties', {})
        a, b = port_range(other.get('external_port'))
        if other.get('protocol', '').lower() == p['protocol'] and max(a, low) <= min(b, high):
            raise cli.ConfigError(f'公网端口与已有规则 {row["name"]} 冲突。')
    ep = properties(api.eip)
    if not ep.get('association_id') or not api.eip.get('id'):
        raise cli.ConfigError('EIP 缺少网关或资源 ID。')
    # Existing rules are an observed source of the public IP; first rule needs
    # the explicit public IP from the EIP console/detail response.
    ips = {x.get('properties', {}).get('external_ip') for x in existing} - {None, ''}
    if len(ips) == 1:
        p['external_ip'] = next(iter(ips))
    import ipaddress
    try:
        ipaddress.ip_address(p['external_ip'])
    except (KeyError, ValueError):
        raise cli.ConfigError('请提供 EIP 的实际 external_ip。') from None
    p.update(nat_gateway_id=ep['association_id'], eip_id=api.eip['id'], internal_ip='',
             internal_instance_name='', internal_instance_type='UNSPECIFIED')
    for key in ('instance_type', 'has_update_permission'):
        p.pop(key, None)
    body.update(id=api.scope + '/' + name, uid=str(uuid.uuid4()), resource_type='network.eip.v1.dnatRule',
                zone=api.eip['zone'], display_name=body.get('display_name') or name)
    for key in ('state', 'deleted', 'create_time', 'update_time', 'order_info'):
        body.pop(key, None)
    return body


def create_rule(api, template):
    body = prepare(api, template, api.list())
    api.request('POST', '/' + body['name'], body)
    # Reconcile by name after a mutation; never blindly repeat a POST.
    for _ in range(30):
        rows = api.list()
        found = next((r for r in rows if r['name'] == body['name']), None)
        if found and found.get('state') == 'CREATED':
            p = found.get('properties', {})
            if any(p.get(k) != body['properties'][k] for k in ('external_ip', 'external_port', 'protocol')):
                raise cli.ConfigError('创建后的规则与请求不一致，请检查云端状态。')
            if any(found.get(k) != body[k] for k in ('creator_id', 'owner_id', 'tenant_id')):
                raise cli.ConfigError('创建后的归属信息不一致，请检查云端规则，勿重复创建。')
            return found
        if found and found.get('state') in ('FAILED', 'DELETED'):
            raise cli.ConfigError('规则创建失败，请检查云端状态。')
        time.sleep(2)
    raise cli.ConfigError(f'规则 {body["name"]} 已提交，但未在等待期内确认 CREATED；请列出规则继续确认，勿重复创建。')


def delete_rule(api, name, *, expected=None):
    rows = api.list()
    row = next((x for x in rows if x['name'] == name), None)
    if row is None:
        raise cli.ConfigError('规则不存在，不执行删除。')
    if expected is not None:
        if row.get('creator_id') != api.current_user_id() or any(
                row.get(key) != expected.get(key) for key in ('uid', 'properties')):
            raise cli.ConfigError('规则归属或内容已变化，请刷新列表后重试。')
    p = row.get('properties', {})
    if p.get('internal_instance_name') or p.get('internal_ip'):
        raise cli.ConfigError('规则仍绑定目标，请先解绑后再删除。')
    api.request('DELETE', '/' + urllib.parse.quote(name, safe=''))
    for _ in range(10):
        if not any(r['name'] == name for r in api.list()):
            return
        time.sleep(2)
    raise cli.ConfigError('删除请求已提交，但规则仍在列表中，请稍后重新查询。')


def selected_rule(api, expected):
    user_id = api.current_user_id()
    row = next((r for r in api.list() if r['name'] == expected['name']), None)
    if (not row or row.get('deleted') or row.get('creator_id') != user_id
            or any(row.get(k) != expected.get(k) for k in ('uid', 'properties'))):
        raise cli.ConfigError('规则归属或内容已变化，请刷新列表后重试。')
    return row


def unbind_rule(api, expected):
    from scripts.cci_network import is_bound
    row = selected_rule(api, expected)
    if not is_bound(row):
        if row.get('state') != 'CREATED':
            raise cli.ConfigError('规则正在变更或状态异常，请刷新后重试。')
        return row
    if row.get('state') != 'ACTIVE':
        raise cli.ConfigError('仅能解绑 ACTIVE 规则，请等待当前操作完成。')
    api.request('POST', '/' + urllib.parse.quote(row['name'], safe='') + '/unbind', {})
    for _ in range(10):
        latest = next((r for r in api.list() if r['name'] == row['name']), None)
        if latest:
            if (latest.get('uid') != row.get('uid') or latest.get('creator_id') != row.get('creator_id')
                    or latest.get('deleted') or any(latest.get('properties', {}).get(k) != row['properties'].get(k)
                        for k in ('external_ip', 'external_port', 'protocol', 'eip_id'))):
                raise cli.ConfigError('解绑后规则身份或入口发生变化，已停止后续操作。')
            if latest.get('state') == 'CREATED' and not is_bound(latest):
                return latest
            if latest.get('state') == 'FAILED':
                raise cli.ConfigError('解绑失败，未执行后续操作，请检查云端状态。')
        time.sleep(2)
    raise cli.ConfigError('解绑请求已提交，但尚未确认完成；未执行后续操作，请刷新查看。')


def remove_rule(api, expected, *, confirmed=False):
    from scripts.cci_network import is_bound
    row = selected_rule(api, expected)
    bound = is_bound(row)
    action = '解绑并删除' if bound else '删除'
    print(label(row))
    if bound:
        print('此规则已绑定；将先解绑再删除，原目标会失去此入口。')
    if not confirmed and choose('确认' + action + '此规则', ['取消', action], default='取消') != action:
        return False
    if bound:
        row = unbind_rule(api, row)
    try:
        delete_rule(api, row['name'], expected=row)
    except cli.ConfigError:
        if bound:
            print('解绑已确认，但删除未确认完成；入口已断开，请刷新列表检查规则。')
        raise
    print('删除已验证：' + row['name'])
    return True


def show_rule(api, expected):
    row = selected_rule(api, expected)
    detail = api.request('GET', '/' + urllib.parse.quote(row['name'], safe=''))
    if (not isinstance(detail, dict) or detail.get('uid') != row.get('uid')
            or detail.get('creator_id') != row.get('creator_id') or detail.get('deleted')):
        raise cli.ConfigError('规则详情归属或身份不匹配，未展示。')
    print(json.dumps(detail, indent=2, ensure_ascii=False))


def label(row):
    p = row.get('properties', {})
    target = p.get('internal_instance_name') or p.get('internal_ip') or '未绑定'
    return f"{p.get('external_ip')}:{p.get('external_port')} | {p.get('protocol')} → {target}:{p.get('internal_port')} | {row.get('state')} | {row['name']}"


def my_rules(api):
    user_id = api.current_user_id()
    return [row for row in api.list() if row.get('creator_id') == user_id]


def all_my_rules(config, eip_name=None):
    eips = Client(config).resources('network.eip.v1.eip')
    if eip_name:
        eips = [eip for eip in eips if eip['name'] == eip_name]
        if len(eips) != 1:
            raise cli.ConfigError('EIP 名称不存在或不唯一。')
    result = []
    user_id = None
    for eip in eips:
        api = Api(config, eip)
        if user_id is None:
            user_id = api.current_user_id()
        # Do not silently return a partial list when an EIP query fails.
        for row in api.list():
            if not row.get('deleted') and row.get('creator_id') == user_id:
                result.append((api, row))
    return sorted(result, key=lambda item: (label(item[1]), item[0].base))


def bind_existing_cci(config, api, row):
    import copy
    from scripts import cci_service, cci_network, cci_ssh
    from scripts.rest import get_json
    if not cci_network.is_selectable(row, api.current_user_id()):
        raise cli.ConfigError('仅支持当前用户的 CREATED/ACTIVE TCP 单端口规则。')
    eip = api.eip
    vpc = properties(eip).get('vpc_id')
    if not vpc:
        raise cli.ConfigError('EIP 缺少 VPC 信息，未绑定。')
    client = Client(config)
    candidates = []
    for ws in client.resources('compute.workspace.v1.instance'):
        if ws.get('subscription_name') != eip.get('subscription_name'):
            continue
        for app in cci_service.my_apps(config, ws):
            pool = app.get('resource_pool', {})
            if isinstance(pool, dict) and pool.get('available_zone') == eip['zone'] and pool.get('vpc_id') == vpc:
                candidates.append((ws, app))
    ws, app = choose('目标 CCI（当前用户、同可用区、同 VPC）', candidates,
                     lambda entry: cci_service.label(entry[1]) + ' · ' + entry[0]['name'])
    current = cci_service.owned_app(config, ws, app['name'])
    if not current.get('uid') or current.get('uid') != app.get('uid') or current.get('resource_pool') != app.get('resource_pool'):
        raise cli.ConfigError('CCI 已变化，请刷新后重试。')
    service = get_json(config, cci_service.resource_url(ws, app['name'], service=True))
    if not isinstance(service, dict) or not isinstance(service.get('ports'), list):
        raise cli.ConfigError('CCI 服务端口格式无效，未绑定。')
    ports = [str(p['port']) for p in service.get('ports', []) if isinstance(p, dict)
             and isinstance(p.get('port'), int) and not isinstance(p['port'], bool) and 1 <= p['port'] <= 65535
             and str(p.get('protocol', 'TCP')).upper() == 'TCP']
    port = choose('CCI 已开放的 TCP 服务端口', ports, default='22' if '22' in ports else (ports[0] if ports else None))
    print(label(row) + ' → ' + app['name'] + ':' + port)
    if cci_network.is_bound(row):
        print('此规则已有绑定，继续会先解绑；原目标将失去此入口。')
    if choose('确认绑定到此 CCI', ['取消', '绑定'], default='取消') != '绑定':
        return
    if port == '22':
        cci_ssh.connection_command(config.get('cci', {}), row['properties']['external_ip'], row['properties']['external_port'])
    body = copy.deepcopy(row)
    body['properties']['internal_port'] = port
    client.scope(ws)
    cci_network.attach_dnat(config, client, ws['name'], app['name'],
        {'eip': eip, 'body': body, 'mode': 'existing', 'expected_uid': current['uid'],
         'expected_owner': current.get('ownership'), 'expected_pool': current['resource_pool']})
    if port == '22':
        cci_ssh.show_connection(config.get('cci', {}), row['properties']['external_ip'],
                               row['properties']['external_port'], app['name'])


def list_page(config, eip_name=None, plain=False):
    while True:
        print('正在汇总当前用户的 DNAT 规则……', flush=True)
        entries = all_my_rules(config, eip_name)
        for index, (_, row) in enumerate(entries, 1):
            print(f'{index}. {label(row)}')
        print(f'共 {len(entries)} 条当前用户创建的规则。')
        if plain:
            return
        print('r. 刷新\n0. 返回')
        value = input('选择 DNAT 规则：').strip()
        if value in ('0', 'q'):
            return
        if value == 'r':
            continue
        if not value.isascii() or not value.isdecimal() or not 1 <= int(value) <= len(entries):
            print('请输入列表中的编号。')
            continue
        api, row = entries[int(value) - 1]
        try:
            action = choose(label(row), ['返回列表', '删除', '绑定 CCI', '解绑', '查看详情'], default='返回列表')
            if action == '绑定 CCI':
                bind_existing_cci(config, api, row)
            if action == '删除':
                remove_rule(api, row)
            elif action == '解绑':
                print(label(row))
                print('解绑会断开原目标的此入口，规则及其端口保留。')
                if choose('确认解绑此规则', ['取消', '解绑'], default='取消') == '解绑':
                    unbind_rule(api, row)
                    print('解绑已验证：' + row['name'])
            elif action == '查看详情':
                show_rule(api, row)
        except Cancelled:
            print('已取消操作。')
        except cli.ConfigError as error:
            print(error)


def main(args):
    parser = argparse.ArgumentParser(description='DNAT 规则管理：创建、列出、绑定已有 CCI、解绑、删除。')
    parser.add_argument('action', nargs='?', choices=['create', 'list', 'delete'])
    parser.add_argument('--eip', help='可选 EIP 范围；创建时省略则编号选择，列表默认汇总全部')
    parser.add_argument('--plain', action='store_true', help='仅打印列表，不进入规则操作页面')
    parser.add_argument('--file', help='创建用的 JSON 文件')
    parser.add_argument('--name', help='待删除规则名称；省略则编号选择')
    parser.add_argument('--yes', action='store_true', help='跳过创建/删除确认；删除已绑定规则会先解绑')
    options = parser.parse_args(args)
    if not options.action:
        from scripts.service_menu import menu
        return menu('DNAT 服务', main, actions=(('create', '创建'), ('list', '列出（选择规则操作）')))
    config = cli.load_config()
    try:
        action = options.action
        if action == 'list':
            list_page(config, options.eip, options.plain)
            return 0
        if action == 'delete':
            entries = all_my_rules(config, options.eip)
            if options.name:
                matches = [entry for entry in entries if entry[1]['name'] == options.name]
                if len(matches) != 1:
                    raise cli.ConfigError('规则不存在或名称不唯一，请从列表中选择。')
                api, row = matches[0]
            else:
                api, row = choose('选择待删除规则', entries, lambda item: label(item[1]))
            remove_rule(api, row, confirmed=options.yes)
            return 0
        eips = Client(config).resources('network.eip.v1.eip')
        if options.eip:
            eips = [x for x in eips if x['name'] == options.eip]
            if len(eips) != 1:
                raise cli.ConfigError('EIP 名称不存在或不唯一，请使用交互选择。')
            eip = eips[0]
        else:
            eip = choose('EIP', eips, lambda x: f"{x['name']} · {x.get('display_name')} · {x.get('zone')}")
        api = Api(config, eip)
        try:
            template = json.loads(Path(options.file or ask('规则 JSON 文件路径')).expanduser().read_text(encoding='utf-8'))
        except ValueError:
            raise cli.ConfigError('规则文件不是有效 JSON。') from None
        body = prepare(api, template, api.list())
        print(json.dumps(body, indent=2, ensure_ascii=False))
        if not options.yes and choose('确认创建', ['取消', '创建'], default='取消') != '创建':
            return 0
        print('正在创建并核实云端状态……', flush=True)
        print('创建已验证：' + label(create_rule(api, body)))
        return 0
    except Cancelled:
        print('已取消 DNAT 操作。')
        return 0
