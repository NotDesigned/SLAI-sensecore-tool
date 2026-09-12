"""CCI creation: collect a template, review, and submit."""
from scripts.ui import output as print
import json
import re
from scripts import cli, cloud, ui, plans


def build_document(cluster, spec, vpc, name, image, command, replicas, mounts, quota, ports, *, ssh=False):
    zone = spec['ZONE']
    if zone != cluster.get('zone'):
        raise cli.ConfigError('资源池与规格的可用区不一致。')
    request = {'cpu': spec['VCPU COUNT'], 'memory': spec['MEMORY(GIB)'] + 'GiB'}
    if int(spec['CHIP COUNT']):
        key = spec.get('RESOURCE KEY')
        if not isinstance(key, str) or not re.fullmatch(r'[A-Za-z0-9.-]+/[A-Za-z0-9_.-]+', key):
            raise cli.ConfigError('所选规格缺少有效资源键，已停止创建。')
        request[key] = spec['CHIP COUNT']
    if ports and any(not x.isascii() or not x.isdecimal() or not 1 <= int(x) <= 65535 for x in ports.split(',')):
        raise cli.ConfigError('端口必须为 1–65535 的整数，用逗号分隔。')
    priority = 'NORMAL'
    document = {'display_name': name, 'resource_pool': {'name': cluster['name'], 'available_zone': zone, 'vpc_id': vpc},
                'replicas': replicas, 'template': {'containers': [{'name': 'main', 'image_path': image,
                'resource_request': request, 'command': ['/bin/sh', '-c', command], 'env': [],
                'volume_mounts': [{k:v for k,v in m.items() if k != 'display_name'} for m in mounts]}],
                'resource_spec': {'name': spec['WORKER SPEC']}},
                'scheduling': {'priority': priority, 'quota_type': quota}, 'termination_grace_period_seconds': 30}
    if ssh:
        document['template']['containers'][0]['readiness_probe'] = {
            'probe_type':'EXEC', 'exec':{'command':['/bin/sh','-c',
                'ssh-keyscan -T 1 -t ed25519 -p 22 127.0.0.1 >/dev/null 2>&1']},
            'period_seconds':3, 'timeout_seconds':2, 'failure_threshold':3}
    return document


def preview(document):
    pool = document.get('resource_pool', {})
    template = document.get('template', {})
    scheduling = document.get('scheduling', {})
    print(f"资源池：{pool.get('name', '未指定')} · 规格：{template.get('resource_spec', {}).get('name', '未指定')}")
    print(f"副本数：{document.get('replicas', 1)} · 配额：{cloud.quota_label(scheduling.get('quota_type', 'RESERVED'))}")
    for container in template.get('containers', []):
        print('镜像：' + container.get('image_path', '未指定'))
        resources = container.get('resource_request', {})
        print('资源：' + ' / '.join(f'{k}={v}' for k, v in resources.items()))
        mounts = container.get('volume_mounts', [])
        print('存储：' + ('；'.join(f"{m.get('display_name') or m.get('id', '')}{m.get('subdir', '')} → {m.get('mount_path', '')}" for m in mounts) or '不挂载'))
    print('完整启动命令及其他参数见配置文件。')


def create(config, workspace_name=None, reuse_last=False):
    client = cloud.Client(config)
    defaults = config.get('cci', {})
    if not isinstance(defaults, dict):
        raise cli.ConfigError('[cci] 必须是配置表。')
    if not ui.active():
        print('创建 CCI：编号列表输入 0 返回；文字输入可用 q 取消，Ctrl-C 退出。')
    try:
        from scripts.workspace import select
        from scripts.forms import CreateDraft
        from scripts.cci_network import attach_dnat
        from scripts import cci_ssh
        previous = defaults.get('last') if reuse_last else None
        if reuse_last and not isinstance(previous,dict):
            raise cli.ConfigError('还没有上次配置，请先正常创建并保存一次。')
        ws = select(client, explicit=workspace_name)
        client.scope(ws)
        draft = CreateDraft('cci', client, ws, previous=previous)
        workspace, name, ports, document, network = ui.creation_form(draft)
        if network:
            props = network['body']['properties']
            print(f"DNAT：{props['external_ip']}:{props['external_port']} → 容器:{props['internal_port']}")
            ports = network['ports']
        path = plans.save('cci', name, document, yaml_format=True)
        if network:
            network_path = path.with_suffix('.dnat.json')
            network_path.touch(mode=0o600)
            network_path.write_text(json.dumps({'eip_name': network['eip']['name'],
                'rule': network['body'], 'ports': network['ports'], 'mode': network.get('mode', 'new')}, indent=2, ensure_ascii=False), encoding='utf-8')
            print(f'DNAT 计划已保存：{network_path}')
        print(f'\n工作空间：{workspace}\n端口：{ports or "无"}\n配置文件：{path}')
        preview(document)
        from scripts import cci_api
        plan = cci_api.creation_plan(client.workspace_record(workspace), name, document, ports)
        plans.save('cci', name + '-request', plan)
        if cci_ssh.enabled(defaults):
            print('SSH：启用，公钥登录 root，端口 22。')
        # Store options, not resource identities or a previous DNAT binding.
        cli.save_config_updates('cci', {'last':draft.snapshot()}, defaults)
        if draft.save_requested:
            return
        if not draft.submit_requested and ui.choose('下一步', ['提交创建', '仅保存配置'], default='提交创建') == '仅保存配置':
            return
        draft.sync_image()
        cci_api.create(config, plan)
        print(f'创建已提交：{name}。可在 CCI 服务 → 列出中查看状态。')
        if network:
            attach_dnat(config, client, workspace, name, network)
            if cci_ssh.enabled(defaults):
                p = network['body']['properties']
                cci_ssh.show_connection(config, p['external_ip'], p['external_port'], name)
    except ui.Cancelled:
        if ui.active():
            raise
        print('已取消创建 CCI。')
