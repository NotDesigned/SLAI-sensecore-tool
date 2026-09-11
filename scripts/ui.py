"""Shared terminal prompts, menus, and resource actions."""
import builtins
from contextvars import ContextVar
from scripts import cli

_backend = ContextVar('slai_ui', default=None)


def backend():
    return _backend.get()


def active():
    return backend() is not None


def output(*values, sep=' ', end='\n', **kwargs):
    if active():
        backend().output(sep.join(str(v) for v in values) + end)
    else:
        builtins.print(*values, sep=sep, end=end, **kwargs)


def show_text(title, value, *, hint=""):
    if active():
        return backend().show_text(title, value, hint=hint)
    output(title + '\n' + value)
    if hint:
        output(hint)


def secret(label):
    if active():
        return backend().ask(label, '', False, password=True)
    import getpass
    return getpass.getpass(label + '：')



class Cancelled(Exception):
    pass


def choose(label, items, describe=str, default=None):
    if active():
        return backend().choose(label, items, describe, default)
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
    if active():
        return backend().ask(label, default, optional)
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
    if active():
        while True:
            try:
                action = choose(title, ['返回', *[label for _, label in actions]])
                if action == '返回':
                    return 0
                key = next(key for key, label in actions if label == action)
                backend().operation(action, lambda: execute([key]))
            except Cancelled:
                return 0
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


def creation_form(draft):
    if active():
        return backend().form(draft)
    draft.initialize()
    while True:
        rows = draft.rows()
        actions = ['提交创建' if draft.previous else '检查配置', '仅保存配置', *[label for _,label,_ in rows]]
        for _,label,value in rows:
            output(f'{label}：{value}')
        selected = choose(draft.title, actions)
        if selected in actions[:2]:
            result = draft.build()
            draft.submit_requested = selected == '提交创建'
            draft.save_requested = selected == '仅保存配置'
            return result
        draft.edit(rows[actions.index(selected)-2][0])


def select_resource(title, source):
    """Select from a searchable paged source without entering resource actions."""
    if active():
        return backend().select_resource(title, source)
    index, query, refresh = 0, '', False
    while True:
        print(getattr(source, 'loading_hint', '正在读取列表…') if source.snapshot is None or refresh else '')
        page = source.page(index, 20, query, refresh=refresh)
        refresh = False
        print(f'\n{title} · 第 {index + 1} 页 · 共 {page.total} 项')
        for i, row in enumerate(page.rows, 1):
            print(f'{i}. {source.describe(row)}')
        value = input('编号选择 / /关键词 搜索 / n 下一页 / p 上一页 / r 刷新 / 0 返回：').strip()
        if value in ('0', 'q'):
            raise Cancelled
        if value.startswith('/'):
            query, index = value[1:].strip(), 0
        elif value == 'n' and page.more:
            index += 1
        elif value == 'p' and index:
            index -= 1
        elif value == 'r':
            index, refresh = 0, True
        elif value.isascii() and value.isdecimal() and 1 <= int(value) <= len(page.rows):
            return page.rows[int(value) - 1]


def browse(title, fetch, describe, operate, *, plain=False, actions=()):
    """Refresh on request or after an action; retain the page on invalid input."""
    if active() and not plain:
        from scripts.listing import LocalSource
        return backend().browse(title, LocalSource(fetch, describe), operate, actions=actions)
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
        for key, label, _ in actions:
            print(f'{key}. {label}')
        print('r. 刷新\n0. 返回')
        while True:
            value = input('选择编号：').strip().lower()
            if value in ('0', 'q'):
                return
            if value == 'r':
                break
            selected_action = next((callback for key, _, callback in actions if value == key), None)
            if selected_action or (value.isascii() and value.isdecimal() and 1 <= int(value) <= len(rows)):
                try:
                    selected_action() if selected_action else operate(rows[int(value) - 1])
                except Cancelled:
                    print('已取消操作。')
                except (cli.ConfigError, OSError) as error:
                    print(str(error) if isinstance(error, cli.ConfigError) else '操作未完成，请刷新确认状态。')
                break
            print('请输入列表编号，或输入 0 返回。')

print = output
