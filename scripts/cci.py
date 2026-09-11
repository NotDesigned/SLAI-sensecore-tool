"""CCI creation: collect a template, review, and submit."""
import datetime
import json
import re
from scripts import cli, cloud, ui, plans


def prepare(client, defaults, workspace_name=None):
    from scripts.workspace import select
    workspace = select(client, explicit=workspace_name)
    client.scope(workspace)
    cluster = ui.choose('资源池（已关联当前工作空间）', client.clusters(workspace), cloud.resource_label)
    spec = ui.choose('实例规格', client.specs(workspace['name'], cluster['name']),
                  lambda x: f"{x['WORKER SPEC']} · CPU {x['VCPU COUNT']} / 内存 {x['MEMORY(GIB)']} GiB / 加速卡 {x['CHIP COUNT']} · {x['CHIP MODEL']} · {x['ZONE']}")
    zone = spec['ZONE']
    if cluster.get('zone') != zone:
        raise cli.ConfigError('资源池与规格的可用区不一致，请重新查询。')
    vpc = cloud.properties(cluster).get('vpc_id')
    if not vpc:
        network = ui.choose('VPC（当前可用区）',
                         [x for x in client.resources('network.vpc.v1.vpc') if x.get('zone') == zone], cloud.resource_label)
        vpc = network['id']
    print(f'已自动匹配可用区 {zone}、VPC {vpc}。')
    name = ui.ask('任务名称', 'slai-' + datetime.datetime.now().strftime('%Y%m%d%H%M%S'))
    if not re.fullmatch(r'[a-z][a-z0-9-]{0,61}[a-z0-9]|[a-z]', name):
        raise cli.ConfigError('任务名称须为 1–63 位小写字母、数字或连字符，以字母开头、字母或数字结尾。')
    image = cloud.select_image(cli.string_value(defaults, 'image'))
    request = {'cpu': spec['VCPU COUNT'], 'memory': spec['MEMORY(GIB)'] + 'GiB'}
    if int(spec['CHIP COUNT']):
        key = spec.get('RESOURCE KEY')
        if not isinstance(key, str) or not re.fullmatch(r'[A-Za-z0-9.-]+/[A-Za-z0-9_.-]+', key):
            raise cli.ConfigError('所选规格缺少有效资源键，已停止创建。')
        request[key] = spec['CHIP COUNT']
        print(f'已自动获取加速卡资源：{key} = {spec["CHIP COUNT"]}。')
    from scripts import cci_ssh
    ssh = cci_ssh.enabled(defaults)
    if ssh:
        command = cci_ssh.startup(cci_ssh.public_key(defaults), cli.string_value(defaults, 'command'))
    else:
        command = ui.ask('容器启动命令（由 /bin/sh -c 执行）', cli.string_value(defaults, 'command').strip() or 'sleep infinity')
    replicas = ui.number('副本数', 1)
    mounts = cloud.select_mounts(client, zone)
    priority = 'NORMAL'
    quota = ui.choose('配额类型', ['RESERVED', 'SPOT'], default='RESERVED')
    ports = '22' if ssh else ui.ask('开放端口（逗号分隔；回车不开放）', optional=True)
    if ports and any(not x.isascii() or not x.isdecimal() or not 1 <= int(x) <= 65535 for x in ports.split(',')):
        raise cli.ConfigError('端口必须为 1–65535 的整数，用逗号分隔。')
    document = {'display_name': name, 'resource_pool': {'name': cluster['name'], 'available_zone': zone, 'vpc_id': vpc},
                'replicas': replicas, 'template': {'containers': [{'name': 'main', 'image_path': image,
                'resource_request': request, 'command': ['/bin/sh', '-c', command], 'env': [], 'volume_mounts': mounts}],
                'resource_spec': {'name': spec['WORKER SPEC']}},
                'scheduling': {'priority': priority, 'quota_type': quota}, 'termination_grace_period_seconds': 30}
    return workspace['name'], name, ports, document


def preview(document):
    pool = document.get('resource_pool', {})
    template = document.get('template', {})
    scheduling = document.get('scheduling', {})
    print(f"资源池：{pool.get('name', '未指定')} · 规格：{template.get('resource_spec', {}).get('name', '未指定')}")
    print(f"副本数：{document.get('replicas', 1)} · 配额：{scheduling.get('quota_type', 'RESERVED')}")
    for container in template.get('containers', []):
        print('镜像：' + container.get('image_path', '未指定'))
        resources = container.get('resource_request', {})
        print('资源：' + ' / '.join(f'{k}={v}' for k, v in resources.items()))
        mounts = container.get('volume_mounts', [])
        print('存储：' + ('；'.join(f"{m.get('display_name') or m.get('id', '')}{m.get('subdir', '')} → {m.get('mount_path', '')}" for m in mounts) or '不挂载'))
    print('完整启动命令及其他参数见配置文件。')


def create(config, workspace_name=None):
    client = cloud.Client(config)
    defaults = config.get('cci', {})
    if not isinstance(defaults, dict):
        raise cli.ConfigError('[cci] 必须是配置表。')
    print('创建 CCI：编号列表输入 0 返回；文字输入可用 q 取消，Ctrl-C 退出。')
    try:
        workspace, name, ports, document = (prepare(client, defaults, workspace_name=workspace_name)
                                             if workspace_name else prepare(client, defaults))
        from scripts.cci_network import plan_dnat, attach_dnat
        from scripts import cci_ssh
        if cci_ssh.enabled(defaults):
            cci_ssh.connection_command(config, '127.0.0.1', 22)  # Validate proxy config before any submission.
        network = plan_dnat(config, document, ports, ssh_port='22') if cci_ssh.enabled(defaults) else plan_dnat(config, document, ports)
        if network:
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
        args = ['cci', 'apps', 'create', name, '--workspace-name', workspace, '--config', str(path)]
        if ports:
            args.extend(['--ports', ports])
        if cci_ssh.enabled(defaults):
            print('SSH：启用，公钥登录 root，端口 22。')
        if ui.choose('下一步', ['仅保存配置', '提交创建'], default='仅保存配置') == '仅保存配置':
            return
        cli.run(client.command(args), client.env)
        print(f'创建已提交：{name}。可在 CCI 服务 → 列出中查看状态。')
        if network:
            attach_dnat(config, client, workspace, name, network)
            if cci_ssh.enabled(defaults):
                p = network['body']['properties']
                cci_ssh.show_connection(config, p['external_ip'], p['external_port'], name)
    except ui.Cancelled:
        print('已取消创建 CCI。')
