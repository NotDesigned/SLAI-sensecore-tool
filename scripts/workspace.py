"""Shared, explicitly saved workspace selection for workspace-scoped services."""
from scripts.ui import output as print

from scripts import ui, cloud, cli

FIELDS = ('name', 'region', 'subscription_name', 'resource_group_name', 'zone')


def select(client, explicit=None, force=False):
    rows = client.resources('compute.workspace.v1.instance')
    if not rows:
        raise cli.ConfigError('当前账户没有可访问的工作空间。请联系 SLAI 管理员授权，或通过“配置账户”按钮 切换账户后重试。')
    if explicit:
        matches = [row for row in rows if row.get('name') == explicit]
        if len(matches) != 1:
            raise cli.ConfigError('工作空间不存在或名称不唯一，请通过主菜单选择。')
        return matches[0]
    config = client.config if isinstance(client.config, dict) else {}
    saved = config.get('workspace', {})
    if not isinstance(saved, dict):
        raise cli.ConfigError('[workspace] 必须是配置表。')
    matches = []
    if saved.get('name'):
        matches = [row for row in rows if all(row.get(k) == saved.get(k) for k in FIELDS)
                   and (not saved.get('id') or (row.get('uid') or row.get('id')) == saved['id'])]
    if not force and len(matches) == 1:
        print('默认工作空间：' + cloud.resource_label(matches[0]) + '（顶部“工作空间”按钮 可切换）')
        return matches[0]
    if not force and saved.get('name'):
        print('保存的工作空间不可用或范围已变化，请重新选择；顶部“工作空间”按钮 可更新默认值。')
    return ui.choose('工作空间', rows, cloud.resource_label,
                      default=matches[0] if len(matches) == 1 else None)


def configure(config):
    client = cloud.Client(config)
    selected = select(client, force=True)
    if not all(isinstance(selected.get(k), str) and selected[k] for k in FIELDS):
        raise cli.ConfigError('工作空间资源范围不完整，未保存。')
    values = {k: selected[k] for k in FIELDS}
    values['id'] = selected.get('uid') or selected.get('id') or ''
    cli.save_config_updates('workspace', values, config.get('workspace', {}))
    print('已保存默认工作空间：' + cloud.resource_label(selected))
