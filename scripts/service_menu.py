"""Persistent create/list/delete submenu shared by cloud services."""
from scripts import cli
from scripts.cci import Cancelled


def menu(title, execute, actions=(('create', '创建'), ('list', '列出'), ('delete', '删除'))):
    while True:
        print(f'\n{title}')
        for index, (_, label) in enumerate(actions, 1):
            print(f'{index}. {label}')
        print('0. 返回')
        value = input(f'请选择 [0-{len(actions)}]：').strip()
        if value in ('0', 'q'):
            return 0
        action = {str(i): item[0] for i, item in enumerate(actions, 1)}.get(value)
        if action is None:
            print(f'请输入 0 至 {len(actions)}。')
            continue
        try:
            execute([action])
        except Cancelled:
            print('已取消操作。')
        except (cli.ConfigError, OSError) as error:
            print(str(error) if isinstance(error, cli.ConfigError) else '无法读取配置或执行命令。')
