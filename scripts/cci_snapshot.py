"""CCI runtime image snapshots using the console's REST contract."""
import re
import uuid

from scripts import cci_api, ccr, ccr_cache, cli, rest, ui

STATES = {'CREATING': '保存中', 'SUCCESS': '已保存', 'FAIL': '保存失败',
          'INVALID': '已失效', 'UNKNOWN': '状态未知'}
STAGES = {'COMMITPENDING': '等待保存', 'COMMITRUNNING': '保存中',
          'PUSHPENDING': '等待上传', 'PUSHRUNNING': '上传中'}


def collection(config, workspace, app, kind):
    url = cci_api.resource_url(workspace, app['name']) + '/' + kind
    return rest.pages(lambda token: rest.get_json(config, rest.query_url(url,
        {'page_size': 100, 'page_token': token})), kind)


def running(config, workspace, app):
    current = cci_api.owned(config, workspace, app['name'])
    cci_api.check_identity(current, app)
    if current.get('state') != 'RUNNING':
        raise cli.ConfigError('CCI 已不在运行中，请刷新列表。')
    names = {c['name'] for c in current.get('template', {}).get('containers', [])}
    return [(instance, container['container_name'])
            for instance in collection(config, workspace, current, 'instances')
            if instance.get('state') == 'RUNNING' and instance.get('name') and instance.get('uid')
            for container in instance.get('container_infos', [])
            if container.get('container_state') == 'RUNNING' and container.get('container_type') == 'MAIN'
            and container.get('container_name') in names]


def snapshots(config, workspace, app):
    current = cci_api.owned(config, workspace, app['name'])
    cci_api.check_identity(current, app)
    rows = collection(config, workspace, current, 'snapshots')
    for row in rows:
        if row.get('state') == 'SUCCESS' and row.get('ccr_namespace'):
            ccr_cache.invalidate_snapshot(config, f"registry.{workspace['region']}.sensecore.cn", row['ccr_namespace'],
                [workspace, current['uid'], {k: row.get(k) for k in ('uid', 'name', 'image_tag', 'uri', 'create_time')}])
    return rows


def create(config, workspace, app, instance, container, namespace, name):
    if not re.fullmatch(r'(?![_.-])(?!.*[_.-]$)(?!.*\.\.)[a-z0-9._-]{1,63}', name):
        raise cli.ConfigError('镜像名称须为 1–63 个小写字母、数字、点、下划线或连字符，首尾须为字母或数字，不能含连续点。')
    candidates = running(config, workspace, app)
    if not any(i['name'] == instance.get('name') and i.get('uid') == instance.get('uid') and c == container
               for i, c in candidates):
        raise cli.ConfigError('实例或容器已变化，请重新选择，未提交快照。')
    if not any(n['name'] == namespace and n.get('region') == workspace['region'] and n.get('state') == 'ACTIVE'
               for n in ccr.namespaces(config)):
        raise cli.ConfigError('目标命名空间不可用，请重新选择。')
    if any(row.get('name') == name and row.get('ccr_namespace') == namespace
           for row in snapshots(config, workspace, app)):
        raise cli.ConfigError('此应用已有同名镜像保存记录，请查看现有记录或使用新名称，避免重复提交。')
    url = cci_api.resource_url(workspace, app['name']) + '/snapshots?client_type=0'
    return rest.request_json(config, url, method='POST', body={
        'name': name, 'display_name': name, 'ccr_namespace': namespace,
        'container_name': container, 'instance_uuid': instance['name']}, timeout=90)


def label(row):
    image = row.get('name', '') + (':' + row['image_tag'] if row.get('image_tag') else '')
    status = STATES.get(row.get('state'), row.get('state', '状态未知'))
    if row.get('state') == 'CREATING':
        status = STAGES.get(str(row.get('reason', '')).split(':', 1)[0], status)
    return '  '.join((status, image, row.get('ccr_namespace', '')))


COLUMNS = ('状态', '镜像', '命名空间')


def cells(row):
    image = row.get('name', '') + (':' + row['image_tag'] if row.get('image_tag') else '')
    return (label(row).split('  ')[0], image, row.get('ccr_namespace', ''))


def list_page(config, workspace, app, plain=False):
    def selected(row):
        # A saved list may still say CREATING after the upload has finished.
        matches = [item for item in snapshots(config, workspace, app)
                   if item.get('name') == row.get('name')
                   and item.get('ccr_namespace') == row.get('ccr_namespace')
                   and (not row.get('uid') or item.get('uid') == row['uid'])
                   and (not row.get('image_tag') or item.get('image_tag') == row['image_tag'])]
        if len(matches) != 1:
            raise cli.ConfigError('镜像保存记录已不可用或不唯一，请刷新列表后重试。')
        current = matches[0]
        uri = current.get('uri')
        if isinstance(uri, str) and uri.strip() and not any(c.isspace() for c in uri.strip()):
            status = STATES.get(current.get('state'), '状态未知')
            hint = '可在创建 CCI / ACP 时使用此地址。' if current.get('state') == 'SUCCESS' else f'当前状态：{status}。这是平台返回的目标地址，镜像尚未确认可用。'
            ui.show_text('快照地址', uri.strip(), hint=hint, copy_label='复制快照地址')
        else:
            ui.show_text('镜像保存状态', label(current),
                         hint=str(current.get('reason') or '地址尚未生成，请稍后刷新列表。'),
                         copy_label=None)
    return ui.browse('已保存的镜像 · ' + app['name'], lambda: snapshots(config, workspace, app), label, selected, plain=plain,
                     columns=COLUMNS, cells=cells)


def create_interactive(config, workspace, app):
    choices = running(config, workspace, app)
    if not choices:
        raise cli.ConfigError('此 CCI 没有可保存的运行中主容器。')
    instance, container = choices[0] if len(choices) == 1 else ui.choose('选择要保存的容器', choices,
        lambda pair: pair[0]['name'] + ' · ' + pair[1])
    namespace = ccr.select_upload_namespace(config, f"registry.{workspace['region']}.sensecore.cn",
                                             config.get('docker', {}).get('namespace', ''))
    name = ui.ask('保存后的镜像名称', app['name'][:40] + '-snapshot-' + uuid.uuid4().hex[:8])
    if not ui.confirm(f'保存到 {namespace}/{name}\n保存时容器会暂停数分钟，完成后自动恢复；镜像须小于 100 GB。\n'
                      '镜像可能包含容器内的密钥和配置，请先清理不应保存的内容；挂载存储请另行备份。', '保存为镜像'):
        return
    create(config, workspace, app, instance, container, namespace, name)
    ui.output('镜像保存请求已提交，版本标签由平台生成。可在“已保存的镜像”中刷新状态并复制镜像地址。')
    list_page(config, workspace, app)
