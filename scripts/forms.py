"""Editable CCI/ACP drafts. Editing never submits a cloud mutation."""
import copy
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import uuid

from scripts import cli, cloud, ui, cci_ssh


class CreateDraft:
    def __init__(self, kind, client, workspace, previous=None):
        self.kind, self.client, self.workspace = kind, client, workspace
        self.title = f'创建 {kind.upper()} · {workspace["name"]}'
        self.defaults = client.config.get(kind, {})
        self.previous = previous
        self.submit_requested = self.save_requested = False
        if previous is not None:
            self.title = f'按照上次配置创建 {kind.upper()} · ' + workspace['name']
        self.values = dict(name='slai-' + kind + '-' + uuid.uuid4().hex[:12], cluster=None, spec=None,
            image=cli.string_value(self.defaults, 'image') or (cloud.DEFAULT_CCI_IMAGE if kind == 'cci' else cloud.DEFAULT_IMAGE),
            command=cli.string_value(self.defaults, 'command'), nodes=1, quota='SPOT' if kind == 'acp' else 'RESERVED', upload=None,
            mounts=None, vpc=None, framework='pytorch', entrypoint=False, network=None,
            network_set=False, ports='22' if kind == 'cci' and cci_ssh.enabled(self.defaults) else '',
            ssh_key=cli.string_value(self.defaults, 'ssh_public_key'))
        self.ssh = kind == 'cci' and cci_ssh.enabled(self.defaults)
        if self.ssh and not self.values['ssh_key']:
            keys = sorted((Path.home() / '.ssh').glob('*.pub'))
            if len(keys) == 1:
                self.values['ssh_key'] = str(keys[0])
            else:
                preferred = next((p for p in keys if p.name == 'id_ed25519.pub'), None)
                if preferred:
                    self.values['ssh_key'] = str(preferred)
        self.cache = {}
        from scripts.ccr import ImageCatalog
        self.images = ImageCatalog(client.config)

    def clusters(self):
        if 'clusters' not in self.cache:
            self.cache['clusters'] = self.client.clusters(self.workspace)
        return self.cache['clusters']

    def resources(self, kind):
        if kind not in self.cache:
            self.cache[kind] = self.client.resources(kind)
        return self.cache[kind]

    def initialize(self):
        if ui.active():
            from scripts.ccr import prefetch_default
            prefetch_default(self.client.config,self.values['image'])
        ui.output('正在读取工作空间关联的资源池…')
        clusters = self.clusters()
        if self.previous is not None:
            self.restore(self.previous, clusters)
            return
        if len(clusters) == 1:
            self.set_cluster(clusters[0])

    def snapshot(self):
        v = self.values
        result = {key:copy.deepcopy(v[key]) for key in ('image','command','nodes','quota','ports','ssh_key','mounts','framework','entrypoint','upload') if v[key] is not None}
        result.update(owner_id=self.client.identity_data()['id'],
            workspace={k:self.workspace[k] for k in ('name','region','subscription_name','resource_group_name','zone')},
            cluster={k:v['cluster'][k] for k in ('name','zone','id','uid') if k in v['cluster']},
            spec=v['spec']['WORKER SPEC'])
        plan = v['network']
        result['network'] = dict(enabled=bool(plan))
        if plan:
            result['network'].update(eip={k:plan['eip'][k] for k in ('name','zone','id','region')},
                                     internal_port=plan['body']['properties']['internal_port'])
        return result

    def restore(self, saved, clusters):
        if saved.get('owner_id') != self.client.identity_data()['id']:
            raise cli.ConfigError('上次配置属于其他账号，请重新创建。')
        if any(saved.get('workspace', {}).get(k) != self.workspace.get(k)
               for k in ('name','region','subscription_name','resource_group_name','zone')):
            raise cli.ConfigError('上次配置不属于当前工作空间，请切回对应工作空间或正常创建。')
        matches = [p for p in clusters if all(p.get(k)==v for k,v in saved.get('cluster',{}).items())]
        if len(matches) != 1:
            raise cli.ConfigError('上次资源池已不可用，请正常创建并重新选择。')
        self.set_cluster(matches[0])
        specs = [s for s in self.specs(matches[0]) if s['WORKER SPEC']==saved.get('spec')]
        if len(specs) != 1:
            raise cli.ConfigError('上次规格已不可用，请重新选择规格。')
        volumes = {r['id'] for r in self.resources('storage.afs.v2.volume') if r.get('zone')==matches[0]['zone']}
        if any(m.get('id') not in volumes for m in saved.get('mounts', [])):
            raise cli.ConfigError('上次挂载的存储已不可用，请重新选择。')
        for key in ('image','command','nodes','quota','ports','ssh_key','mounts','framework','entrypoint','upload'):
            if key in saved:
                self.values[key] = copy.deepcopy(saved[key])
        self.values['spec'] = specs[0]
        if self.ssh:
            self.values['ports'] = ','.join(sorted((set(self.values['ports'].split(',')) | {'22'}) - {''},key=int))
        network = saved.get('network', {})
        if self.kind == 'cci' and network.get('enabled'):
            from scripts import dnat
            eips = [r for r in self.resources('network.eip.v1.eip')
                    if all(r.get(k)==v for k,v in network.get('eip',{}).items())
                    and r.get('zone')==matches[0]['zone']
                    and cloud.properties(r).get('vpc_id')==self.values['vpc']]
            if len(eips) != 1:
                raise cli.ConfigError('上次使用的 EIP 不再可用，请重新配置 DNAT。')
            api = dnat.Api(self.client.config,eips[0])
            rows = api.list()
            identity = self.client.identity_data()
            port = '22' if self.ssh else network['internal_port']
            body = dnat.prepare(api, dict(name=self.values['name']+'-dnat', creator_id=identity['id'],
                owner_id=identity['id'], tenant_id=identity['tenant_id'], properties=dict(
                external_port=dnat.random_free_port(rows), internal_port=port, protocol='tcp')), rows)
            ports = ','.join(sorted((set(self.values['ports'].split(',')) | {port}) - {''}, key=int))
            self.values['network'] = dict(eip=eips[0],body=body,ports=ports,mode='new')
        self.values['network_set'] = True
        upload = self.values['upload']
        if upload:
            from scripts.docker_registry import inspect_local
            # Reuse means the current local tag; bind the new draft to its ID.
            upload['source_id'] = inspect_local(upload['source_image'])
        ui.output('已回填上次配置；名称自动更新。' + ('附加 DNAT 使用新的空闲端口。' if self.kind == 'cci' else ''))

    def set_cluster(self, cluster):
        if self.values['cluster'] == cluster:
            return
        ui.output('正在读取所选资源池的规格与默认 AFS；已读取的数据会复用…')
        # Independent reads overlap. Publish the new selection only after both
        # succeed; revisiting a pool reuses its specifications within this draft.
        with ThreadPoolExecutor(max_workers=2) as executor:
            specifications = executor.submit(self.specs, cluster)
            mounts = executor.submit(self.default_mounts, cluster)
            specs, defaults = specifications.result(), mounts.result()
        self.values.update(cluster=cluster, spec=specs[0] if len(specs) == 1 else None,
                           mounts=defaults, network=None, network_set=False,
                           vpc=cloud.properties(cluster).get('vpc_id'))

    def specs(self, cluster):
        key = ('specs', cluster.get('id'), cluster.get('uid'), cluster['zone'], cluster['name'])
        if key not in self.cache:
            self.cache[key] = self.client.specs(self.workspace['name'], cluster['name'])
        return self.cache[key]

    def default_mounts(self, cluster):
        zone = cluster['zone']
        volumes = [r for r in self.resources('storage.afs.v2.volume') if r.get('zone') == zone]
        preferred = [r for r in volumes if r['name'] == 'afs-share-' + zone.rsplit('-', 1)[-1]]
        volume = volumes[0] if len(volumes) == 1 else preferred[0] if len(preferred) == 1 else None
        if volume:
            if 'username' not in self.cache:
                self.cache['username'] = self.client.current_username()
            return [dict(type='PV_AFS', id=volume['id'], zone=zone, mount_path='/data',
                subdir='/' + self.cache['username'], display_name=volume.get('display_name') or volume['name'])]

    def rows(self):
        v = self.values
        rows = [('name', '名称', v['name']), ('cluster', '资源池', cloud.display_name(v['cluster']) if v['cluster'] else '待选择'),
            ('spec', '规格', v['spec']['WORKER SPEC'] if v['spec'] else '待选择'), ('image', '镜像', (v['upload']['source_image'] + ' → 提交时同步到 ' if v['upload'] else '') + v['image'])]
        if self.kind == 'acp':
            rows.append(('entrypoint', '镜像入口', '使用入口程序及参数' if v['entrypoint'] else '自定义任务命令'))
        rows.append(('command', ('入口程序及参数' if v['entrypoint'] else '任务命令') if self.kind == 'acp' else '启动命令', v['command'] or ('启动 SSH 服务' if self.ssh else '待填写' if self.kind == 'acp' else 'sleep infinity')))
        if self.ssh:
            rows.append(('ssh_key', 'SSH 公钥', v['ssh_key'] or '待选择 .pub 文件'))
        if self.kind == 'acp':
            rows.append(('framework', '框架', v['framework']))
        rows += [('nodes', 'Worker 数量' if self.kind == 'acp' else '副本数', v['nodes']),
                 ('quota', '配额', cloud.quota_label(v['quota']))]
        mounts = v['mounts']
        rows.append(('mounts', 'AI 文件存储', '待选择' if mounts is None else
                     '；'.join(m.get('subdir', '') + ' → ' + m.get('mount_path', '') for m in mounts) or '不挂载'))
        automatic_vpc = bool(v['cluster'] and cloud.properties(v['cluster']).get('vpc_id'))
        rows.append(('vpc', 'VPC（自动）' if automatic_vpc else 'VPC', v['vpc'] or '待匹配'))
        if self.kind == 'cci':
            if not self.ssh:
                rows.append(('ports', '容器端口', v['ports'] or '无'))
            plan = v['network']
            endpoint = (plan['body']['properties']['external_ip'] + ':' + plan['body']['properties']['external_port']) if plan else None
            rows.append(('network', 'DNAT 入口', endpoint or ('不附加' if v['network_set'] else '待配置（可新建或选已有）')))
        return rows

    def edit(self, key):
        # Roll back an edit if a dependent fetch or validation fails.
        before = copy.deepcopy(self.values)
        cache_before = dict(self.cache)
        try:
            self._edit(key)
        except Exception:
            self.values = before
            self.cache = cache_before
            raise

    def _edit(self, key):
        v = self.values
        if key == 'cluster':
            rows = self.clusters()
            cluster = rows[0] if len(rows) == 1 else ui.choose('资源池', rows, cloud.resource_label, v['cluster'])
            self.set_cluster(cluster)
        elif key == 'spec':
            self.need_cluster()
            specs = self.specs(v['cluster'])
            v['spec'] = ui.choose('实例规格', specs,
                lambda x: f"{x['WORKER SPEC']} · {x['VCPU COUNT']} CPU / {x['MEMORY(GIB)']} GiB / 加速卡 {x['CHIP COUNT']}", v['spec'])
        elif key in ('quota', 'framework'):
            choices = ['RESERVED', 'SPOT'] if key == 'quota' else ['pytorch', 'tensorflow', 'mpi', 'senseparrots']
            v[key] = ui.choose(dict((k, n) for k, n, _ in self.rows())[key], choices, describe=cloud.quota_label if key == 'quota' else str, default=v[key])
        elif key == 'entrypoint':
            choices = ['自定义任务命令', '使用入口程序及参数']
            v[key] = ui.choose('启动方式', choices, default=choices[int(v[key])]) == choices[1]
        elif key == 'nodes':
            v[key] = ui.number('数量', v[key])
        elif key == 'command':
            label = dict((k, n) for k, n, _ in self.rows())[key]
            v[key] = ui.backend().edit_command(v[key], title=label) if ui.active() else ui.ask(label,v[key],optional=True)
        elif key == 'image':
            v[key], v['upload'] = cloud.select_image(v[key], self.client.config, catalog=self.images)
        elif key == 'mounts':
            self.need_cluster()
            v[key] = cloud.select_mounts(self.client, v['cluster']['zone'])
        elif key == 'vpc':
            self.need_cluster()
            if cloud.properties(v['cluster']).get('vpc_id'):
                ui.show_text('VPC 随资源池自动匹配', v['vpc'])
                return
            rows = [r for r in self.resources('network.vpc.v1.vpc') if r.get('zone') == v['cluster']['zone']]
            v[key] = ui.choose('VPC', rows, cloud.resource_label)['id']
            v.update(network=None, network_set=False)
        elif key == 'network':
            self.need_cluster()
            if not v['vpc']:
                raise cli.ConfigError('请先选择 VPC。')
            from scripts.cci_network import plan_dnat
            document = {'display_name': v['name'], 'resource_pool': dict(name=v['cluster']['name'],
                available_zone=v['cluster']['zone'], vpc_id=v['vpc'])}
            v['network'] = plan_dnat(self.client.config, document, v['ports'], ssh_port='22' if self.ssh else None)
            v['network_set'] = True
        else:
            label = dict((k, n) for k, n, _ in self.rows())[key]
            v[key] = ui.ask(label, v[key], optional=key in ('command', 'ports'))
            if key == 'name':
                from scripts.acp import validate_name
                validate_name(v[key])
            if key == 'ports':
                v.update(network=None, network_set=False)

    def sync_image(self):
        if self.values['upload']:
            from scripts.docker_registry import sync_image
            if sync_image(self.client.config, self.values['upload']) != self.values['image']:
                raise cli.ConfigError('镜像同步目标与任务不一致，未创建任务。')

    def need_cluster(self):
        if not self.values['cluster']:
            raise cli.ConfigError('请先选择资源池。')

    def build(self):
        from scripts.acp import validate_name, build_document as acp_document
        from scripts.cci import build_document
        v = self.values
        self.need_cluster()
        if isinstance(v['nodes'], bool) or not isinstance(v['nodes'], int) or v['nodes'] < 1 or v['quota'] not in ('RESERVED','SPOT'):
            raise cli.ConfigError('实例数量或配额类型无效。')
        if not v['spec']:
            raise cli.ConfigError('请选择实例规格。')
        if v['spec']['ZONE'] != v['cluster']['zone']:
            raise cli.ConfigError('资源池与规格可用区不一致。')
        if not v['vpc'] and (self.kind == 'cci' or v['cluster']['name'] == 'public'):
            raise cli.ConfigError('请选择 VPC。')
        if v['mounts'] is None:
            raise cli.ConfigError('请选择 AI 文件存储，或明确选择不挂载。')
        validate_name(v['name'])
        expected_vpc = cloud.properties(v['cluster']).get('vpc_id')
        if expected_vpc and v['vpc'] != expected_vpc:
            raise cli.ConfigError('VPC 与资源池不一致，请重新选择资源池。')
        if v['upload'] and v['upload'].get('target') != v['image']:
            raise cli.ConfigError('镜像同步目标与任务不一致，请重新选择。')
        if not v['image'] or v['image'].startswith('-') or any(c.isspace() for c in v['image']):
            raise cli.ConfigError('镜像地址格式无效。')
        if '\0' in v['command']:
            raise cli.ConfigError('启动命令不能含空字符。')
        if self.kind == 'acp':
            import shlex
            command = v['command'].strip()
            if not command:
                raise cli.ConfigError('请填写任务命令或镜像入口程序及参数。')
            if v['entrypoint']:
                try:
                    argv = shlex.split(command)
                    if command.startswith('[') or not argv or not argv[0] or '\0' in command:
                        raise ValueError
                except ValueError:
                    raise cli.ConfigError('入口命令格式无效，参数含空格时请加引号。') from None
                command = 'exec ' + shlex.join(argv)
            return v['name'], acp_document(v['cluster'], v['spec'], v['name'],
                v['image'], command, v['framework'], v['nodes'], v['quota'], v['mounts'], v['vpc'])
        if not v['network_set']:
            raise cli.ConfigError('请配置 DNAT 入口，或明确选择不附加。')
        command = v['command']
        if self.ssh:
            if not v['ssh_key']:
                raise cli.ConfigError('请选择 SSH 公钥文件。')
            key = cci_ssh.public_key({'ssh_public_key': v['ssh_key']})
            command = cci_ssh.startup(key, command)
            cci_ssh.connection_command(self.client.config, '127.0.0.1', 22)
        else:
            command = command or 'sleep infinity'
        ports = v['network']['ports'] if v['network'] else v['ports']
        document = build_document(v['cluster'], v['spec'], v['vpc'], v['name'], v['image'],
                                  command, v['nodes'], v['mounts'], v['quota'], ports, ssh=self.ssh)
        return self.workspace['name'], v['name'], ports, document, v['network']
