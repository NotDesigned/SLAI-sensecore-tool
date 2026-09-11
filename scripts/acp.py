"""ACP training jobs: explicit startup scripts and owner-scoped operations."""
import argparse
import json
import re
import shlex
import subprocess
import time
import uuid

from scripts.rest import get_json
from scripts import ui, cloud, cli, network, plans, rest


def validate_name(name):
    if not re.fullmatch(r'[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?', name):
        raise cli.ConfigError('任务名称必须为 1–63 位小写字母、数字或连字符，以字母开头。')
    return name


class Client(cloud.Client):
    def __init__(self, config):
        super().__init__(config)
        self.control_env = network.acp_environment(config, self.env)
        self.user_id = None

    def read(self, args, diagnostics=False):
        acp = args[:2] == ['acp', 'jobs']
        return super().read(args, diagnostics, env=self.control_env if acp else self.env, timeout=90 if acp else 45)

    def identity(self):
        if self.user_id is None:
            data = get_json(self.config, 'https://iam.sensecoreapi.cn/iam/idp/v1/me')
            self.user_id = rest.identity_id(data)
        return self.user_id

    def jobs(self, workspace, name=None):
        filters = ['--name', validate_name(name)] if name else ['--filter', "creator_id='" + self.identity() + "'"]
        rows = self.list_json(['acp', 'jobs', 'list', '--workspace-name', workspace, *filters, '-o', 'json'],
                              empty_message='No jobs found')
        return [row for row in rows if isinstance(row.get('ownership'), dict)
                and row['ownership'].get('user_id') == self.identity()]

    def owned(self, workspace, name, expected=None):
        raw = self.read(['acp', 'jobs', 'describe', validate_name(name), '--workspace-name', workspace, '-o', 'json'])
        try:
            row = json.loads(raw)
        except ValueError:
            raise cli.ConfigError('ACP 详情不是有效 JSON。') from None
        if (not isinstance(row, dict) or row.get('name') != name or not row.get('uid')
                or not isinstance(row.get('ownership'), dict)
                or row['ownership'].get('user_id') != self.identity()):
            raise cli.ConfigError('任务不存在或不属于当前用户。')
        if expected is not None and row['uid'] != expected.get('uid'):
            raise cli.ConfigError('任务身份已变化，请刷新列表。')
        return row

    def submit(self, args):
        try:
            result = subprocess.run(self.command(args), env=self.control_env, capture_output=True,
                                    text=True, encoding='utf-8', timeout=90)
        except subprocess.TimeoutExpired:
            raise cli.ConfigError('ACP 提交超时，结果未知；请按任务名刷新列表，勿重复提交。') from None
        if result.returncode:
            raise cli.ConfigError(f'ACP 操作未确认成功（退出码 {result.returncode}）；请刷新列表确认状态，检查网络和配额。')
        print('ACP 请求已提交，请刷新列表确认最终状态。')


def startup(defaults):
    mode = ui.choose('镜像是否内置完整任务启动逻辑（Entrypoint / CMD）？',
                      ['使用镜像启动逻辑', '输入任务命令'], default='输入任务命令')
    if mode == '使用镜像启动逻辑':
        print('填写镜像入口程序及任务参数，例如 /entrypoint.sh python /data/train.py。')
        print('NGC 默认 /opt/nvidia/nvidia_entrypoint.sh 只做初始化；还需传入训练程序参数。')
        try:
            value = ui.ask('镜像启动命令（参数含空格时加引号）')
            if value.startswith('['):
                raise ValueError
            argv = shlex.split(value)
        except ValueError:
            raise cli.ConfigError('请输入入口程序和参数，参数含空格时加引号；不接受 JSON 数组。') from None
        if not isinstance(argv, list) or not argv or any(not isinstance(x, str) or '\0' in x for x in argv) or not argv[0]:
            raise cli.ConfigError('镜像启动命令不能为空或含空字符。')
        return 'exec ' + shlex.join(argv)
    return ui.ask('任务命令（建议 set -eu; 后接训练命令）', cli.string_value(defaults, 'command'))


def prepare(client, workspace):
    defaults = client.config.get('acp', {})
    cluster = ui.choose('资源池', client.clusters(workspace), cloud.resource_label)
    spec = ui.choose('实例规格', client.specs(workspace['name'], cluster['name']),
                      lambda x: f"{x['WORKER SPEC']} · CPU {x['VCPU COUNT']} / 内存 {x['MEMORY(GIB)']} GiB / 加速卡 {x['CHIP COUNT']}")
    if spec['ZONE'] != cluster.get('zone'):
        raise cli.ConfigError('资源池与规格可用区不一致。')
    name = validate_name(ui.ask('任务名称', 'slai-acp-' + uuid.uuid4().hex[:12]))
    image = cloud.select_image(cli.string_value(defaults, 'image') or cloud.DEFAULT_IMAGE)
    command = startup(defaults)
    framework = ui.choose('训练框架', ['pytorch', 'tensorflow', 'mpi', 'senseparrots'], default='pytorch')
    nodes = ui.number('Worker 数量', 1)
    quota = ui.choose('配额类型', ['RESERVED', 'SPOT'], default='RESERVED')
    mounts = cloud.select_mounts(client, spec['ZONE'])
    args = ['acp', 'jobs', 'create', '--workspace-name', workspace['name'], '--aec2-name', cluster['name'],
            '--name', name, '--job-name', name, '--container-image-url', image, '--command', command,
            '--training-framework', framework, '--worker-spec', spec['WORKER SPEC'], '--worker-nodes', str(nodes),
            '--priority', 'NORMAL', '--quota-type', quota.lower(), '--retry-times', '0']
    if cluster['name'] == 'public':
        vpc = cloud.properties(cluster).get('vpc_id')
        if not vpc:
            vpc = ui.choose('VPC', [x for x in client.resources('network.vpc.v1.vpc') if x.get('zone') == spec['ZONE']], cloud.resource_label)['id']
        args += ['--vpc-id', vpc, '--az', spec['ZONE']]
    if mounts:
        values = []
        for m in mounts:
            if any(c in m['subdir'] + m['mount_path'] for c in ',:\n\r') or not m['subdir'].startswith('/'):
                raise cli.ConfigError('ACP 挂载子目录须为绝对路径，且不能包含逗号、冒号或换行。')
            values.append(m['id'] + m['subdir'] + ':' + m['mount_path'])
        args += ['--storage-mount', ','.join(values)]
    return name, args


def confirm_submit(client, workspace, name, args, source=None):
    plan = {'workspace': workspace, 'name': name, 'args': args}
    if source:
        plan['source'] = {k: source.get(k) for k in ('name', 'uid', 'roles', 'mount', 'resource_pool', 'scheduling')}
    path = plans.save('acp', name, plan)
    print('计划已保存：' + str(path))
    print(f'工作空间：{workspace}\n任务名称：{name}')
    if source:
        print('复制自：' + source['name'])
        for role in source.get('roles', []):
            print(f"角色：{role.get('name')} × {role.get('total_replicas', 1)} · 镜像：{role.get('image_path')}")
            print('规格：' + ', '.join(x.get('name', '') for x in role.get('resource_spec', [])))
            print('启动命令：' + role.get('startup_script', ''))
        print('资源池：' + source.get('resource_pool', {}).get('name', ''))
        print('存储和调度：沿用源任务，详见计划文件。')
    else:
        values = dict(zip(args[3::2], args[4::2]))
        for flag, label in (('--aec2-name', '资源池'), ('--worker-spec', '规格'),
                            ('--worker-nodes', 'Worker 数量'), ('--quota-type', '配额'),
                            ('--container-image-url', '镜像'), ('--training-framework', '训练框架'),
                            ('--command', '任务命令'), ('--storage-mount', '存储')):
            print(label + '：' + values.get(flag, '不挂载'))
    if ui.choose('下一步', ['仅保存配置', '提交创建'], default='仅保存配置') != '提交创建':
        return
    if any(row['name'] == name for row in client.jobs(workspace, name=name)):
        raise cli.ConfigError('任务名称已存在，请使用新名称。')
    if source:
        client.owned(workspace, source['name'], source)
    client.submit(args)
    print('任务名称：' + name)


def operate(client, workspace, row, action):
    if action == '返回列表':
        return
    current = client.owned(workspace, row['name'], row)
    if action == '详情':
        print(json.dumps(current, ensure_ascii=False, indent=2))
    elif action == '复制':
        name = validate_name(ui.ask('新任务名称', row['name'][:40] + '-copy-' + uuid.uuid4().hex[:8]))
        print('沿用源任务的镜像、启动命令、资源和挂载；不会自动恢复训练 checkpoint。')
        args = ['acp', 'jobs', 'copy', '--workspace-name', workspace, '--copy-job-name', row['name'], '--name', name, '--job-name', name]
        confirm_submit(client, workspace, name, args, source=current)
    elif action in ('停止', '删除'):
        if ui.choose('确认' + action + '：' + row['name'], ['取消', action], default='取消') != action:
            return
        latest = client.owned(workspace, row['name'], current)
        if action == '停止' and latest.get('state') in ('SUCCEEDED', 'FAILED', 'SUSPENDED', 'DELETED', 'DELETING'):
            print('任务已结束或停止，无需再次停止。')
            return
        client.submit(['acp', 'jobs', 'stop' if action == '停止' else 'delete', row['name'], '--workspace-name', workspace])
        for _ in range(10):
            rows = client.jobs(workspace, name=row['name'])
            found = next((x for x in rows if x['name'] == row['name']), None)
            if (action == '删除' and found is None) or (action == '停止' and found and found.get('state') == 'SUSPENDED'):
                print(action + '已验证：' + row['name'])
                return
            time.sleep(2)
        print('请求已提交，但最终状态尚未确认，请刷新列表。')


def label(row):
    return f"{cloud.display_name(row)} · {row.get('state', '未知')}"


def list_page(client, workspace, name=None, plain=False):
    def selected(row):
        action = ui.choose(label(row), ['返回列表', '详情', '停止', '复制', '删除'], default='返回列表')
        operate(client, workspace, row, action)
    return ui.browse('我的 ACP', lambda: client.jobs(workspace, name=name), label, selected, plain=plain)


def main(args):
    parser = argparse.ArgumentParser(description='ACP 长任务：创建和列表内操作。')
    parser.add_argument('action', nargs='?', choices=['create', 'list'])
    parser.add_argument('--workspace')
    parser.add_argument('--name', help='按任务名称前缀筛选列表')
    parser.add_argument('--plain', action='store_true')
    options = parser.parse_args(args)
    if options.action is None:
        from scripts.ui import menu
        return menu('ACP 服务', main, (('create', '创建'), ('list', '列出（详情 / 停止 / 复制 / 删除）')))
    client = Client(cli.load_config())
    from scripts.workspace import select
    workspace = select(client, explicit=options.workspace)
    client.scope(workspace)
    if options.action == 'create':
        name, command = prepare(client, workspace)
        confirm_submit(client, workspace['name'], name, command)
    else:
        list_page(client, workspace['name'], name=options.name, plain=options.plain)
    return 0
