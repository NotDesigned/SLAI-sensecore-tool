"""Create, list and delete DNAT rules with HTTP and read-back validation."""
from scripts.ui import output as print
from concurrent.futures import ThreadPoolExecutor
import json
import re
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from scripts import cli, ui, rest, plans
from scripts.cloud import Client, properties
from scripts.ui import Cancelled, choose, ask


class Api:
    def __init__(self, config, eip):
        self.credentials = config['account']
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
        url = 'https://iam.sensecoreapi.cn/iam/idp/v1/me' if identity else self.base + suffix
        return rest.request_json({'account': self.credentials}, url, method=method, body=body)

    def current_user_id(self):
        data = self.request('GET', identity=True)
        return rest.identity_id(data)

    def list(self, creator_id=None):
        query = {'page_size': 100}
        if creator_id is not None:
            rest.identity_id({'id': creator_id})
            query['filter'] = "creator_id='" + creator_id + "'"
        rows = rest.pages(lambda token: self.request('GET', '?' + urllib.parse.urlencode(
            {**query, 'page_token': token})), 'dnat_rules')
        return [row for row in rows if not row.get('deleted')]


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
        raise cli.ConfigError('创建时不设置目标；创建后可在列表中选择绑定 CCI。')
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


def new_rule(api, name, internal_port=None, protocol=None):
    identity = api.request('GET', identity=True)
    if not isinstance(identity, dict):
        raise cli.ConfigError('无法确认当前用户，未创建 DNAT。')
    rows = api.list()
    public_port = ask('公网端口', random_free_port(rows))
    container_port = internal_port or ask('容器端口', '22')
    protocol = protocol or choose('协议', ['tcp', 'udp'], default='tcp')
    body = {'name': name, 'creator_id': identity.get('id'), 'owner_id': identity.get('id'),
            'tenant_id': identity.get('tenant_id'), 'properties': {
                'external_port': public_port, 'internal_port': container_port, 'protocol': protocol}}
    ips = {row.get('properties', {}).get('external_ip') for row in rows} - {None, ''}
    if len(ips) != 1:
        body['properties']['external_ip'] = ask('EIP 公网 IP')
    return prepare(api, body, rows)


def create_rule(api, template):
    body = prepare(api, template, api.list())
    if template.get('uid'):
        body['uid'] = template['uid']
    api.request('POST', '/' + body['name'], body)
    # Reconcile by name after a mutation; never blindly repeat a POST.
    for _ in range(30):
        rows = api.list()
        found = next((r for r in rows if r['name'] == body['name']), None)
        if found and found.get('state') == 'CREATED':
            p = found.get('properties', {})
            if any(p.get(k) != body['properties'][k] for k in ('external_ip', 'external_port', 'internal_port', 'protocol')):
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
    ui.show_text('DNAT 详情', json.dumps(detail, indent=2, ensure_ascii=False))


def label(row):
    p = row.get('properties', {})
    target = p.get('internal_instance_name') or p.get('internal_ip') or '未绑定'
    return f"{p.get('external_ip')}:{p.get('external_port')} | {p.get('protocol')} → {target}:{p.get('internal_port')} | {row.get('state', '待创建')} | {row['name']}"


def my_rules(api):
    user_id = api.current_user_id()
    return [row for row in api.list() if row.get('creator_id') == user_id]


def all_my_rules(config, eip_name=None):
    eips = Client(config).resources('network.eip.v1.eip')
    if eip_name:
        eips = [eip for eip in eips if eip['name'] == eip_name]
        if len(eips) != 1:
            raise cli.ConfigError('EIP 名称不存在或不唯一。')
    if not eips:
        return []
    apis = [Api(config, eip) for eip in eips]
    user_id = apis[0].current_user_id()

    def read_owned(api):
        # Filtering is only for this owner list. Port conflict checks call list()
        # without a creator filter so other users' allocations remain visible.
        return [(api, row) for row in api.list(creator_id=user_id)
                if not row.get('deleted') and row.get('creator_id') == user_id]

    with ThreadPoolExecutor(max_workers=min(3, len(apis))) as executor:
        pages = list(executor.map(read_owned, apis))
    # Propagate any failure rather than presenting a partial aggregate as complete.
    return sorted([entry for page in pages for entry in page], key=lambda item: (label(item[1]), item[0].base))


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
        cci_ssh.connection_command(config, row['properties']['external_ip'], row['properties']['external_port'])
    body = copy.deepcopy(row)
    body['properties']['internal_port'] = port
    client.scope(ws)
    cci_network.attach_dnat(config, client, ws['name'], app['name'],
        {'eip': eip, 'body': body, 'mode': 'existing', 'expected_uid': current['uid'],
         'expected_owner': current.get('ownership'), 'expected_pool': current['resource_pool']})
    if port == '22':
        cci_ssh.show_connection(config, row['properties']['external_ip'],
                               row['properties']['external_port'], app['name'])


def list_page(config, eip_name=None, plain=False):
    def selected(entry):
        api, row = entry
        action = choose(label(row), ['返回列表', '删除', '绑定 CCI', '解绑', '查看详情'], default='返回列表')
        if action == '绑定 CCI':
            bind_existing_cci(config, api, row)
        elif action == '删除':
            remove_rule(api, row)
        elif action == '解绑':
            print(label(row))
            print('解绑会断开原目标的此入口，规则及其端口保留。')
            if ui.confirm('确认解绑此规则', '解绑'):
                unbind_rule(api, row)
                print('解绑已验证：' + row['name'])
        elif action == '查看详情':
            show_rule(api, row)
    return ui.browse('我的 DNAT', lambda: all_my_rules(config, eip_name),
                     lambda entry: label(entry[1]), selected, plain=plain, actions=(
                         ('create','创建 DNAT',lambda: main(['create'] + (['--eip',eip_name] if eip_name else []))),))


def main(args):
    parser = cli.service_parser('dnat', {'list':'汇总本人创建的规则', 'create':'交互创建规则',
        'describe':'查看规则详情', 'bind':'选择已有 CCI 并绑定（迁移会确认）',
        'unbind':'解绑规则，保留公网端口', 'delete':'删除规则；已有绑定会先解绑'},
        'DNAT 端口规则管理；列表默认汇总所有可访问 EIP 下本人创建的规则。',
        '示例：uv run main.py --text dnat bind --name rule-name --eip eip-name')
    parser.add_argument('--eip', help='限定 EIP 名称；创建省略时交互选择，列表省略时汇总全部')
    parser.add_argument('--plain', action='store_true', help='仅 list：打印规则列表后退出')
    parser.add_argument('--name', help='规则完整名称；创建时作为新名称，其他操作省略则交互选择')
    parser.add_argument('--yes', action='store_true', help='仅 create/delete/unbind：跳过确认；创建仍需填写端口')
    options = parser.parse_args(args)
    if options.yes and options.action not in ('create','delete','unbind'):
        parser.error('--yes 仅适用于 create/delete/unbind')
    if options.plain and options.action != 'list':
        parser.error('--plain 仅适用于 list')
    if options.name and options.action == 'list':
        parser.error('--name 用于指定单条规则；查看详情请使用 describe')
    config = cli.load_config()
    try:
        action = options.action
        if action == 'list':
            list_page(config, options.eip, options.plain)
            return 0
        if action in ('delete','describe','bind','unbind'):
            entries = all_my_rules(config, options.eip)
            if options.name:
                matches = [entry for entry in entries if entry[1]['name'] == options.name]
                if len(matches) != 1:
                    raise cli.ConfigError('规则不存在或名称不唯一，请从列表中选择。')
                api, row = matches[0]
            else:
                api, row = choose('选择目标规则', entries, lambda item: label(item[1]))
            if action == 'delete':
                remove_rule(api, row, confirmed=options.yes)
            elif action == 'describe':
                show_rule(api, row)
            elif action == 'bind':
                bind_existing_cci(config, api, row)
            elif options.yes or ui.confirm('确认解绑此规则（原目标将失去此入口）', '解绑'):
                unbind_rule(api, row)
                print('解绑已验证：' + row['name'])
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
        name = options.name or ask('规则名称', 'slai-dnat-' + uuid.uuid4().hex[:12])
        body = new_rule(api, name)
        path = plans.save('dnat', body['name'], body)
        print(label(body))
        print('完整计划：' + str(path))
        if not options.yes and ui.choose('确认创建未绑定的 DNAT 规则', ['提交创建', '取消'], default='提交创建') != '提交创建':
            return 0
        print('正在创建并核实云端状态……', flush=True)
        print('创建已验证：' + label(create_rule(api, body)))
        return 0
    except Cancelled:
        print('已取消 DNAT 操作。')
        return 0
