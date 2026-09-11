"""Edit a copy using the creation form while retaining unedited template fields."""
import copy
import shlex
import uuid

from scripts import cli, cloud, ui
from scripts.forms import CreateDraft


class CopyDraft(CreateDraft):
    def __init__(self, kind, client, workspace, document, source_name, ports=''):
        super().__init__(kind, client, workspace)
        self.document = copy.deepcopy(document)
        self.title = f'复制 {kind.upper()} · {source_name}'
        self.ssh = False  # Retain the source command, including any SSH bootstrap.
        self.values.update(name=source_name[:40] + '-copy-' + uuid.uuid4().hex[:8],
                           ports=ports, network_set=True)
        self.initial = None

    def initialize(self):
        d, v = self.document, self.values
        units = d.get('template', {}).get('containers', []) if self.kind == 'cci' else d.get('roles', [])
        if not units:
            raise cli.ConfigError('源模板缺少容器或角色，未提交复制。')
        self.unit_count = len(units)
        unit = units[0]
        if self.kind == 'cci':
            argv = unit.get('command', [])
            command = argv[2] if len(argv) == 3 and argv[:2] in (['/bin/sh','-c'], ['/bin/bash','-c']) else shlex.join(argv)
            spec_name = d.get('template', {}).get('resource_spec', {}).get('name')
            nodes, mounts = d.get('replicas',1), unit.get('volume_mounts',[])
        else:
            specs = unit.get('resource_spec', [])
            if not specs:
                raise cli.ConfigError('源角色缺少规格，未提交复制。')
            self.mixed_specs = len(specs) > 1
            spec_name = specs[0]['name']
            command = unit.get('startup_script','')
            nodes = unit.get('total_replicas') or sum(spec.get('replicas', 0) for spec in specs) or 1
            mounts = d.get('mount',[])
        pool = d.get('resource_pool', {})
        zone = pool.get('available_zone') or pool.get('zone')
        matches = [row for row in self.clusters() if row['name'] == pool.get('name') and (not zone or row['zone'] == zone)]
        if len(matches) != 1:
            raise cli.ConfigError('源资源池已不可用，无法回填复制配置。')
        cluster = matches[0]
        specs = [row for row in self.specs(cluster) if row['WORKER SPEC'] == spec_name]
        if len(specs) != 1:
            raise cli.ConfigError('源规格已不可用，无法回填复制配置。')
        v.update(cluster=cluster, spec=specs[0], image=unit.get('image_path',''), command=command,
                 nodes=nodes, mounts=copy.deepcopy(mounts), quota=d.get('scheduling',{}).get('quota_type','RESERVED'),
                 framework=d.get('framework','PYTORCH').lower(),
                 vpc=pool.get('vpc_id') or cloud.properties(cluster).get('vpc_id'))
        self.initial = copy.deepcopy(v)
        if getattr(self, 'mixed_specs', False):
            ui.output('源角色使用多个规格；修改规格或数量会统一为表中选中的规格，未修改时保留原分配。')
        if len(units) > 1:
            ui.output('镜像、命令和规格编辑作用于首个容器/角色，其他项完整保留。')
        ui.output('已回填源配置，可修改后提交。未编辑的模板字段保留；CCI 不迁移原 DNAT。')

    def rows(self):
        rows = super().rows()
        if getattr(self, 'unit_count', 1) > 1:
            rows = [(key, label + ('（首项）' if key in ('image','command','spec') or key == 'nodes' and self.kind == 'acp' else ''), value)
                    for key, label, value in rows]
        return rows

    def _edit(self, key):
        if key == 'cluster' and getattr(self, 'unit_count', 1) > 1:
            raise cli.ConfigError('多容器/多角色模板复制时保留资源池，避免其他未编辑角色的规格失配。')
        return super()._edit(key)

    def build(self):
        if self.initial is None:
            raise cli.ConfigError('源配置尚未载入，不能提交。')
        v, old = self.values, self.initial
        # An empty source command means image defaults. Allow it unless edited.
        if self.kind == 'acp' and not v['command'] and v['command'] == old['command']:
            v['command'] = ':'
            try:
                result = super().build()
            finally:
                v['command'] = old['command']
        else:
            result = super().build()
        generated = result[3] if self.kind == 'cci' else result[1]
        d = copy.deepcopy(self.document)
        d['display_name'] = v['name']
        changed = lambda key: v[key] != old[key]
        if changed('cluster') or changed('vpc'):
            d['resource_pool'] = generated['resource_pool']
        if changed('quota'):
            d.setdefault('scheduling', {})['quota_type'] = v['quota']
        if self.kind == 'cci':
            original = d['template']['containers'][0]
            updated = generated['template']['containers'][0]
            if changed('nodes'):
                d['replicas'] = v['nodes']
            if changed('spec') or changed('cluster'):
                d['template']['resource_spec'] = generated['template']['resource_spec']
                original['resource_request'] = updated['resource_request']
            for key, field in [('image','image_path'), ('command','command'), ('mounts','volume_mounts')]:
                if changed(key):
                    original[field] = updated[field]
            return result[:3] + (d, result[4])
        d['name'] = v['name']
        original, updated = d['roles'][0], generated['roles'][0]
        if changed('spec') or changed('cluster') or changed('nodes'):
            original['resource_spec'] = updated['resource_spec']
            original['total_replicas'] = v['nodes']
        if changed('nodes'):
            original['total_replicas'] = v['nodes']
            for spec in original['resource_spec']:
                spec.pop('replicas', None)
        for key, field in [('image','image_path'), ('command','startup_script')]:
            if changed(key) or (key == 'command' and changed('entrypoint')):
                original[field] = updated[field]
        if changed('mounts'):
            d['mount'] = generated['mount']
        if changed('framework'):
            d['framework'] = generated['framework']
        return v['name'], d
