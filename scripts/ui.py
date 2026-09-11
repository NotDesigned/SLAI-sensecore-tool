"""Shared terminal prompts, menus, and resource actions."""
from scripts import cli


class Cancelled(Exception):
    pass


def choose(label, items, describe=str, default=None):
    if not items:
        raise cli.ConfigError(f'{label}没有可选项，请检查权限和上级选择。')
    print(f'\n{label}：')
    back = next((item for item in items if isinstance(item, str)
                 and item in ('返回', '返回列表', '取消')), None)
    choices = [item for item in items if item is not back]
    for index, item in enumerate(choices, 1):
        suffix = ' [默认]' if item == default else ''
        print(f'{index}. {describe(item)}{suffix}')
    suffix = ' [默认]' if back is not None and back == default else ''
    print(f'0. {back or "返回"}{suffix}')
    while True:
        answer = input('输入编号（默认项可回车）：').strip()
        if answer.lower() in ('0', 'q'):
            if back is not None:
                return back
            raise Cancelled
        if not answer and default in items:
            return default
        if answer.isascii() and answer.isdecimal() and 1 <= int(answer) <= len(choices):
            return choices[int(answer) - 1]
        print('请输入有效编号。')


def ask(label, default='', optional=False):
    while True:
        answer = input(label + (f' [{default}]' if default else '') + '：').strip()
        if answer.lower() == 'q':
            raise Cancelled
        answer = answer or default
        if answer or optional:
            return answer
        print('此项不能为空。')


def number(label, default):
    while True:
        value = ask(label, str(default))
        if value.isascii() and value.isdecimal() and int(value) > 0:
            return int(value)
        print('请输入正整数。')


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


def confirm(message, action):
    return choose(message, ['取消', action], default='取消') == action


def browse(title, fetch, describe, operate, *, plain=False):
    """Refresh on request or after an action; retain the page on invalid input."""
    while True:
        try:
            rows = fetch()
        except (cli.ConfigError, OSError) as error:
            if plain:
                raise
            print(str(error) if isinstance(error, cli.ConfigError) else '读取失败，请检查网络后重试。')
            if input('r. 重试 / 0. 返回：').strip().lower() == 'r':
                continue
            return
        print(f'\n{title}：共 {len(rows)} 项')
        for index, row in enumerate(rows, 1):
            print(f'{index}. {describe(row)}')
        if plain:
            return
        print('r. 刷新\n0. 返回')
        while True:
            value = input('选择编号：').strip().lower()
            if value in ('0', 'q'):
                return
            if value == 'r':
                break
            if value.isascii() and value.isdecimal() and 1 <= int(value) <= len(rows):
                try:
                    operate(rows[int(value) - 1])
                except Cancelled:
                    print('已取消操作。')
                except (cli.ConfigError, OSError) as error:
                    print(str(error) if isinstance(error, cli.ConfigError) else '操作未完成，请刷新确认状态。')
                break
            print('请输入列表编号，或输入 0 返回。')
