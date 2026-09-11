"""ACP training jobs: explicit startup scripts and owner-scoped operations."""
from scripts.ui import output as print
import argparse
import json
import re
import time
import uuid

from scripts.rest import get_json
from scripts import ui, cloud, cli, network, plans, rest, templates


def validate_name(name):
    if not re.fullmatch(r'[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?', name):
        raise cli.ConfigError('任务名称必须为 1–63 位小写字母、数字或连字符，以字母开头。')
    return name


class Client(cloud.Client):
    def __init__(self, config):
        super().__init__(config)
        self.user_id = None

    def identity(self):
        if self.user_id is None:
            data = self.identity_data()
            self.user_id = rest.identity_id(data)
        return self.user_id

    def jobs_url(self, workspace):
        record = self.workspace_record(workspace)
        return (cloud.api_origin('aec2', record) + '/compute/acp/data/v2' +
                cloud.scope_path(record, 'workspaces') + '/trainingJobs')

    def fetch_jobs(self, workspace, token='1', size=100, name='', state=''):
        if not isinstance(size, int) or isinstance(size, bool) or not 1 <= size <= 500:
            raise cli.ConfigError('ACP 每页数量须为 1–500。')
        filters = "creator_id='" + self.identity() + "'"
        if state:
            if state not in ('RUNNING', 'PENDING', 'SUSPENDED', 'SUCCEEDED', 'FAILED'):
                raise cli.ConfigError('无效的任务状态。')
            filters += " AND state='" + state + "'"
        query = {'page_size': size, 'page_token': str(token), 'filter': filters}
        if name:
            if not re.fullmatch(r'[a-z][a-z0-9-]{0,62}', name):
                raise cli.ConfigError('名称前缀须以小写字母开头，只含小写字母、数字或连字符，最长 63 位。')
            query['name'] = name
        url = rest.query_url(self.jobs_url(workspace), query)
        return get_json(self.config, url, timeout=90, proxy=network.acp_proxy_url(self.config, remote_dns=True))

    def jobs(self, workspace, name=None):
        rows = rest.pages(lambda token: self.fetch_jobs(workspace, token, name=name or ''), 'training_jobs')
        return [row for row in rows if isinstance(row.get('ownership'), dict)
                and row['ownership'].get('user_id') == self.identity()]

    def jobs_page(self, workspace, index, size=20, name='', state=''):
        if not isinstance(index, int) or isinstance(index, bool) or index < 0:
            raise cli.ConfigError('ACP 页码无效。')
        data = self.fetch_jobs(workspace, str(index + 1), size, name, state)
        rows, total, following = rest.list_page(data, 'training_jobs')
        if following and (not following.isdecimal() or int(following) != index + 2):
            raise cli.ConfigError('ACP 返回了异常的下一页标识，请刷新重试。')
        if not following and total is not None and index * size + len(rows) < total:
            raise rest.IncompletePage('ACP 未返回完整页面或下一页标识。')
        owned = [r for r in rows if isinstance(r.get('ownership'), dict)
                 and r['ownership'].get('user_id') == self.identity()]
        from scripts.listing import Page
        return Page(owned, bool(following), total if len(owned) == len(rows) else None)

    def owned(self, workspace, name, expected=None):
        row = get_json(self.config, self.jobs_url(workspace) + '/' + validate_name(name), timeout=90,
                       proxy=network.acp_proxy_url(self.config, remote_dns=True))
        if (not isinstance(row, dict) or row.get('name') != name or not row.get('uid')
                or not isinstance(row.get('ownership'), dict)
                or row['ownership'].get('user_id') != self.identity()):
            raise cli.ConfigError('任务不存在或不属于当前用户。')
        if expected is not None and row['uid'] != expected.get('uid'):
            raise cli.ConfigError('任务身份已变化，请刷新列表。')
        return row

    def write(self, workspace, suffix, body, query=None):
        url = self.jobs_url(workspace) + suffix
        if query:
            url = rest.query_url(url, query)
        return rest.request_json(self.config, url, method='POST', body=body, timeout=90,
                                 proxy=network.acp_proxy_url(self.config, remote_dns=True))

    def create(self, workspace, name, document):
        validate_name(name)
        if any(row['name'] == name for row in self.jobs(workspace, name=name)):
            raise cli.ConfigError('任务名称已存在，未提交。')
        body = templates.writable('acp', document)
        body.update(name=name)
        try:
            result = self.write(workspace, '', body, {'training_job_name': name})
        except rest.RestError as error:
            details = (error.body.get('details') or []) if isinstance(error.body, dict) else []
            if any(isinstance(d, dict) and d.get('reason') == 'tjInvalidQuotaTypeInAec2' for d in details):
                raise cli.ConfigError('所选资源池不支持当前配额类型，请通过“按照上次配置”切换资源池或配额后再提交。') from None
            raise
        if isinstance(result, dict) and result.get('uid'):
            expected = result
        else:
            expected = None
        for _ in range(20):
            try:
                return self.owned(workspace, name, expected)
            except rest.RestError as error:
                if error.status != 404:
                    raise
            time.sleep(2)
        raise cli.ConfigError('ACP 已提交，但未确认创建结果；请按名称查询，勿重复提交。')

    def control(self, workspace, name, action, expected):
        current = self.owned(workspace, validate_name(name), expected)
        if action == 'stop' and current.get('state') in ('SUCCEEDED', 'FAILED', 'SUSPENDED', 'DELETED'):
            return
        if action not in ('stop', 'delete'):
            raise cli.ConfigError('未知 ACP 操作。')
        record = self.workspace_record(workspace)
        body = {key: record[key] for key in ('subscription_name', 'resource_group_name', 'zone')}
        body.update(workspace_name=workspace, training_job_names=[name])
        self.write(workspace, ':batchStop' if action == 'stop' else ':batchDelete', body)
        for _ in range(60):
            try:
                latest = self.owned(workspace, name, current)
            except rest.RestError as error:
                if action == 'delete' and error.status == 404:
                    return
                raise
            if action == 'stop' and latest.get('state') == 'SUSPENDED':
                return
            time.sleep(2)
        raise cli.ConfigError('ACP 请求已提交，但最终状态尚未确认，请刷新列表。')


def build_document(cluster, spec, name, image, command, framework, nodes, quota, mounts, vpc=None):
    validate_name(name)
    if spec['ZONE'] != cluster['zone'] or not command.strip():
        raise cli.ConfigError('资源池与规格不一致，或任务命令为空。')
    pool = {'name': cluster['name']}
    if cluster['name'] == 'public':
        if not vpc:
            raise cli.ConfigError('公共资源池需要 VPC。')
        pool.update(zone=spec['ZONE'], vpc_id=vpc)
    return dict(name=name, display_name=name, framework=framework.upper(),
        roles=[dict(name='Worker', resource_spec=[{'name': spec['WORKER SPEC']}],
                    total_replicas=nodes, startup_script=command, image_path=image)],
        resource_pool=pool, mount=[{k:v for k,v in m.items() if k != 'display_name'} for m in mounts],
        scheduling={'priority': 'NORMAL', 'quota_type': quota},
        fault_tolerance={'backoff_limit': 0})


def copy_document(source, name):
    body = templates.writable('acp', source)
    body.update(name=validate_name(name), display_name=name)
    # GET reports allocated replicas per specification as well as the requested
    # total. The create API forbids specifying both in the same request.
    for role in body.get('roles', []):
        if role.get('total_replicas', 0) > 0:
            for spec in role.get('resource_spec', []):
                spec.pop('replicas', None)
    return body


def confirm_submit(client, workspace, name, document, source=None, draft=None):
    plan = {'workspace': client.workspace_record(workspace), 'name': name, 'document': document}
    if source:
        plan['source'] = {k: source.get(k) for k in ('name', 'uid')}
    path = plans.save('acp', name, plan)
    print('计划已保存：' + str(path))
    print(f'工作空间：{workspace}\n任务名称：{name}')
    if source:
        print('复制自：' + source['name'])
    for role in document.get('roles', []):
        print(f"角色：{role.get('name')} × {role.get('total_replicas', 1)} · 镜像：{role.get('image_path')}")
        print('规格：' + ', '.join(x.get('name', '') for x in role.get('resource_spec', [])))
        print('启动命令：' + role.get('startup_script', ''))
    print('资源池：' + document.get('resource_pool', {}).get('name', ''))
    print('配额：' + cloud.quota_label(document.get('scheduling', {}).get('quota_type', '')))
    print('挂载：' + ('；'.join(m.get('subdir', '') + ' → ' + m['mount_path'] for m in document.get('mount', [])) or '无'))
    if draft:
        cli.save_config_updates('acp', {'last':draft.snapshot()}, draft.defaults)
        if draft.save_requested:
            return
    if not (draft and draft.submit_requested) and ui.choose('下一步', ['提交创建', '仅保存配置'], default='提交创建') != '提交创建':
        return
    if draft:
        draft.sync_image()
    if source:
        client.owned(workspace, source['name'], source)
    client.create(workspace, name, document)
    print('创建已核实：' + name)


def operate(client, workspace, row, action):
    if action == '返回列表':
        return
    current = client.owned(workspace, row['name'], row)
    if action == '详情':
        ui.show_text('ACP 详情 · ' + row['name'], json.dumps(current, ensure_ascii=False, indent=2))
    elif action == '复制':
        name = validate_name(ui.ask('新任务名称', row['name'][:40] + '-copy-' + uuid.uuid4().hex[:8]))
        print('沿用源任务的镜像、启动命令、资源和挂载；不会自动恢复训练 checkpoint。')
        confirm_submit(client, workspace, name, copy_document(current, name), source=current)
    elif action in ('停止', '删除'):
        if ui.choose('确认' + action + '：' + row['name'], ['取消', action], default='取消') != action:
            return
        client.control(workspace, row['name'], 'stop' if action == '停止' else 'delete', current)
        print(action + '已验证：' + row['name'])


def label(row):
    return f"{cloud.display_name(row)} · {row.get('state', '未知')}"


class JobSource:
    states = ('全部', 'RUNNING', 'PENDING', 'SUSPENDED', 'SUCCEEDED', 'FAILED')
    search_hint = '任务名称前缀（回车查询）'
    columns = ('任务名称', '状态', '创建时间 UTC')

    def __init__(self, client, workspace, name=''):
        self.client, self.workspace, self.name = client, workspace, name
        self.cache = {}

    def cells(self, row):
        import datetime
        try:
            value = datetime.datetime.fromisoformat(row['create_time'].replace('Z', '+00:00'))
            timestamp = value.astimezone(datetime.timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
        except (KeyError, ValueError, TypeError, AttributeError):
            timestamp = '—'
        return (cloud.display_name(row), row.get('state', '未知'), timestamp)

    def page(self, index, size, query='', state='', refresh=False):
        if refresh:
            self.cache.clear()
        key = (index, size, query, state)
        if key not in self.cache:
            page = self.client.jobs_page(self.workspace, index, size, query or self.name,
                                         '' if state == '全部' else state)
            ids = {r.get('uid') or r['name'] for r in page.rows}
            for old_key, old in self.cache.items():
                if old_key[1:] == key[1:] and ids and ids == {r.get('uid') or r['name'] for r in old.rows}:
                    raise cli.ConfigError('接口返回了重复页面，请刷新或缩小查询范围。')
            if len(self.cache) >= 12:
                self.cache.pop(next(iter(self.cache)))
            self.cache[key] = page
        return self.cache[key]


def list_page(client, workspace, name=None, plain=False):
    def selected(row):
        action = ui.choose(label(row), ['返回列表', '详情', '停止', '复制', '删除'], default='返回列表')
        operate(client, workspace, row, action)
    actions = [('create','创建 ACP',lambda: main(['create','--workspace',workspace]))]
    actions.append(('create-last','按照上次配置',lambda: main(['create-last','--workspace',workspace])))
    if ui.active() and not plain:
        return ui.backend().browse('我的 ACP · ' + workspace, JobSource(client, workspace, name or ''), selected, actions=actions)
    return ui.browse('我的 ACP', lambda: client.jobs(workspace, name=name), label, selected, plain=plain, actions=actions)


def main(args):
    parser = argparse.ArgumentParser(description='ACP 长任务：创建和列表内操作。')
    parser.add_argument('action', nargs='?', choices=['create', 'create-last', 'list'], default='list')
    parser.add_argument('--workspace')
    parser.add_argument('--name', help='按任务名称前缀筛选列表')
    parser.add_argument('--plain', action='store_true')
    options = parser.parse_args(args)
    client = Client(cli.load_config())
    from scripts.workspace import select
    workspace = select(client, explicit=options.workspace)
    client.scope(workspace)
    if options.action in ('create', 'create-last'):
        from scripts.forms import CreateDraft
        previous = client.config.get('acp', {}).get('last') if options.action == 'create-last' else None
        if options.action == 'create-last' and not isinstance(previous, dict):
            raise cli.ConfigError('还没有上次配置，请先正常创建并保存一次。')
        draft = CreateDraft('acp', client, workspace, previous=previous)
        name, document = ui.creation_form(draft)
        confirm_submit(client, workspace['name'], name, document, draft=draft)
    else:
        list_page(client, workspace['name'], name=options.name, plain=options.plain)
    return 0
