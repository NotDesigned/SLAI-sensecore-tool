"""Optional DNAT plan and verified binding after CCI creation."""
import copy
import json
import time
from scripts import ui, cloud, cli, dnat


def is_selectable(row, user_id):
    p = row.get('properties', {})
    if not isinstance(p, dict):
        return False
    port = p.get('external_port', '')
    return (row.get('creator_id') == user_id and row.get('state') in ('CREATED', 'ACTIVE')
            and not row.get('deleted')
            and p.get('protocol', '').lower() == 'tcp' and isinstance(port, str)
            and port.isascii() and port.isdecimal() and 1 <= int(port) <= 65535)


def is_bound(row):
    p = row.get('properties', {})
    return bool(p.get('internal_instance_name') or p.get('internal_ip'))


def is_available(row, user_id):
    return is_selectable(row, user_id) and row.get('state') == 'CREATED' and not is_bound(row)


def endpoint_label(row):
    p = row['properties']
    target = p.get('internal_instance_name') or p.get('internal_ip')
    status = f"已绑定 → {target}:{p.get('internal_port')}" if target else '未绑定'
    return f"{p.get('external_ip', '未知 IP')}:{p['external_port']} · {status} · {row['name']}"


def plan_dnat(config, document, ports, ssh_port=None):
    mode = ui.choose('附加公网 DNAT', ['不附加', '创建新 DNAT', '选择已有 DNAT'], default='创建新 DNAT' if ssh_port else '不附加')
    if mode == '不附加':
        return None
    pool = document['resource_pool']
    eips = [e for e in cloud.Client(config).resources('network.eip.v1.eip')
            if e.get('zone') == pool['available_zone']
            and cloud.properties(e).get('vpc_id') == pool['vpc_id']]
    if not eips:
        raise cli.ConfigError('没有与 CCI 同可用区、同 VPC 的 EIP。')
    if mode == '选择已有 DNAT':
        print('正在汇总所有匹配公网 IP 下的可选规则……', flush=True)
        user_id = dnat.Api(config, eips[0]).current_user_id()
        eligible = []
        for candidate_eip in eips:
            for row in dnat.Api(config, candidate_eip).list():
                if is_selectable(row, user_id):
                    eligible.append({'eip': candidate_eip, 'rule': row})
        eligible.sort(key=lambda item: (item['rule']['properties'].get('external_ip', ''),
                                       int(item['rule']['properties']['external_port']), item['rule']['name']))
        if not eligible:
            raise cli.ConfigError('所有匹配公网 IP 下都没有当前用户可用的 DNAT 规则，可选择“创建新 DNAT”。')
        selection = ui.choose('我的可用 DNAT（全部匹配公网 IP）', eligible,
                               lambda item: endpoint_label(item['rule']))
        eip, selected = selection['eip'], selection['rule']
        if is_bound(selected):
            print('当前绑定：' + endpoint_label(selected))
            print('迁移将先解绑，原目标会失去此公网入口，再绑定到新 CCI。')
            if ui.choose('确认将此规则迁移到新 CCI', ['取消', '迁移'], default='取消') != '迁移':
                raise ui.Cancelled
        body = copy.deepcopy(selected)
        public_port = body['properties']['external_port']
        default_port = body['properties'].get('internal_port', '22')
        if default_port in ('', '0'):
            default_port = '22'
        container_port = ssh_port or ui.ask('容器端口（SSH 通常为 22）', default_port)
        body['properties']['internal_port'] = container_port
    else:
        eip = ui.choose('EIP（同可用区、同 VPC）', eips, cloud.resource_label)
        api = dnat.Api(config, eip)
        body = dnat.new_rule(api, document['display_name'][:54] + '-dnat', internal_port=ssh_port, protocol='tcp')
        public_port = body['properties']['external_port']
        container_port = body['properties']['internal_port']
    if '-' in public_port or '-' in container_port:
        raise cli.ConfigError('附加 DNAT 当前只支持单端口。')
    dnat.port_range(public_port)
    dnat.port_range(container_port)
    merged = sorted(set([p for p in ports.split(',') if p] + [container_port]), key=int)
    print(f"DNAT 计划：{body['properties']['external_ip']}:{public_port} → CCI:{container_port}（TCP）")
    return {'eip': eip, 'body': body, 'ports': ','.join(merged),
            'mode': 'existing' if mode == '选择已有 DNAT' else 'new'}


def attach_dnat(config, client, workspace, name, plan):
    # Capture the actual CCI UID instead of confusing it with the Service UID.
    try:
        app = json.loads(client.read(['cci', 'apps', 'describe', name, '--workspace-name', workspace, '-o', 'json']))
    except ValueError:
        raise cli.ConfigError('目标 CCI 已存在，但无法读取 UID，未提交 DNAT。') from None
    uid = app.get('uid') if isinstance(app, dict) else None
    if not uid:
        raise cli.ConfigError('目标 CCI 已存在，但缺少 UID，未提交 DNAT。')
    if plan.get('expected_uid') and (uid != plan['expected_uid']
            or app.get('ownership') != plan.get('expected_owner')
            or app.get('resource_pool') != plan.get('expected_pool')):
        raise cli.ConfigError('目标 CCI 已变化，未执行 DNAT 绑定。')
    api = dnat.Api(config, plan['eip'])
    if plan.get('mode') == 'existing':
        user_id = api.current_user_id()
        row = next((x for x in api.list() if x['name'] == plan['body']['name']), None)
        original = plan['body']
        if (not row or not is_selectable(row, user_id) or row.get('uid') != original.get('uid')
                or any(row['properties'].get(k) != original['properties'].get(k)
                       for k in ('external_ip', 'external_port', 'protocol', 'eip_id', 'internal_instance_name', 'internal_ip', 'internal_instance_type'))):
            raise cli.ConfigError('目标 CCI 已存在，但所选 DNAT 已变化或不再属于当前用户，未执行绑定。')
        row = copy.deepcopy(row)
        if is_bound(row):
            print('正在解绑所选 DNAT 的原目标……', flush=True)
            api.request('POST', '/' + row['name'] + '/unbind', {})
            for _ in range(10):
                latest = next((x for x in api.list() if x['name'] == row['name']), None)
                if (latest and latest.get('uid') == row.get('uid') and is_available(latest, user_id)
                        and all(latest['properties'].get(k) == original['properties'].get(k)
                                for k in ('external_ip', 'external_port', 'protocol', 'eip_id'))):
                    row = copy.deepcopy(latest)
                    break
                time.sleep(2)
            else:
                raise cli.ConfigError('目标 CCI 已存在，但原 DNAT 解绑尚未确认；未提交新绑定，请检查规则状态。')
    else:
        row = dnat.create_rule(api, plan['body'])
    props = row['properties']
    props.update(internal_instance_type='CCI_DEPLOYMENT_SERVICE', internal_instance_name=uid,
                 internal_port=plan['body']['properties']['internal_port'])
    api.request('POST', '/' + row['name'] + '/bind', row)
    for _ in range(10):
        current = next((x for x in api.list() if x['name'] == row['name']), None)
        if current:
            p = current.get('properties', {})
            if (current.get('state') == 'ACTIVE' and not current.get('deleted')
                    and current.get('uid') == row.get('uid')
                    and current.get('creator_id') == row.get('creator_id')
                    and p.get('internal_instance_type') == 'CCI_DEPLOYMENT_SERVICE'
                    and p.get('internal_instance_name') == uid
                    and all(p.get(k) == props.get(k) for k in ('external_ip', 'external_port', 'internal_port', 'protocol'))):
                print(f"DNAT 绑定已核实：{p['external_ip']}:{p['external_port']} → CCI:{p['internal_port']}。尚未验证服务连通性。")
                return
            if current.get('state') == 'FAILED':
                break
        time.sleep(2)
    raise cli.ConfigError(f'目标 CCI 已存在，DNAT {row["name"]} 已提交，但目标绑定未通过复查；请在 DNAT 服务中检查，不能视为公网可连；若迁移了已有规则，原入口可能已断开。')
