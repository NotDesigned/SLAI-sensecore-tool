"""Textual shell. Cloud functions run in workers; prompts cross a narrow bridge."""
from concurrent.futures import Future, TimeoutError as FutureTimeout

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.worker import get_current_worker
from textual.widgets import Button, DataTable, Footer, Input, OptionList, RichLog, Select, Static, TextArea

from scripts import cli, ui


def literal(value):
    # API text is data, never Rich markup or terminal escape sequences.
    return Text(''.join(c for c in str(value) if c.isprintable() or c in '\n\t'))


def clipped(value, width):
    text = literal(str(value).replace('\n', ' '))
    text.truncate(max(1, width), overflow='ellipsis')
    return text


def row_identity(row):
    if isinstance(row, tuple) and len(row) == 2 and isinstance(row[1], dict):
        row = row[1]
    return (row.get('uid') or row.get('id') or row.get('name')) if isinstance(row, dict) else str(row)


class Bridge:
    def __init__(self, app, sink=None, owner=None):
        self.app, self.sink = app, sink
        self.history = ''
        self.owner = owner

    def request(self, screen):
        if self.owner is not None and not self.owner.is_mounted:
            raise ui.Cancelled
        result = Future()
        self.app.pending.add(result)
        def push():
            if self.owner is not None and not self.owner.is_mounted:
                result.set_exception(ui.Cancelled())
                return
            self.app.push_screen(screen, lambda value: result.set_result(value) if not result.done() else None)
        self.app.call_from_thread(push)
        try:
            while True:
                try:
                    return result.result(timeout=0.1)
                except FutureTimeout:
                    if get_current_worker().is_cancelled or not self.app.is_running:
                        raise ui.Cancelled
        finally:
            result.cancel()
            self.app.pending.discard(result)

    def output(self, value):
        self.history = (self.history + value)[-24000:]
        if self.sink is not None:
            self.app.call_from_thread(self.sink, value)

    def choose(self, title, items, describe, default):
        if not items:
            raise cli.ConfigError(title + '没有可选项，请检查权限和上级选择。')
        back = next((x for x in items if isinstance(x, str) and x in ('返回', '返回列表', '取消')), None)
        choices = [x for x in items if x is not back]
        context = self.history if isinstance(default, str) and default in ('取消', '仅保存配置') else ''
        value = self.request(Picker(title, choices, describe, default, back, context))
        if value is None:
            if back is not None:
                return back
            raise ui.Cancelled
        return value

    def ask(self, title, default='', optional=False, password=False):
        result = self.request(Edit(title, default, optional, password))
        if result is None:
            raise ui.Cancelled
        return result

    def edit_command(self, value, title="启动命令"):
        result = self.request(CommandEdit(value, title))
        if result is None:
            raise ui.Cancelled
        return result

    def show_text(self, title, value, *, hint=""):
        return self.request(Details(title, value, hint=hint))

    def browse(self, title, source, operate, *, actions=()):
        return self.request(Browser(title, source, operate, actions=actions))

    def select_resource(self, title, source):
        value = self.request(Browser(title, source, None, select_mode=True))
        if value is None:
            raise ui.Cancelled
        return value

    def operation(self, title, callback):
        return self.request(Operation(title, callback))

    def form(self, draft):
        value = self.request(Form(draft))
        if value is None:
            raise ui.Cancelled
        return value


class BackScreen(ModalScreen):
    BINDINGS = [('escape', 'back', '返回'), ('0', 'back', '返回')]

    def action_back(self):
        self.dismiss(None)


class Picker(BackScreen):
    def __init__(self, title, choices, describe=str, default=None, back=None, context=''):
        super().__init__()
        self.title_text, self.choices, self.describe = title, choices, describe
        self.default, self.back, self.context = default, back, context
        self.indices = list(range(len(choices)))

    def compose(self) -> ComposeResult:
        with Vertical(classes='dialog'):
            yield Static(literal(self.title_text), classes='heading')
            if self.context:
                yield TextArea(self.context, read_only=True, classes='review-context')
            if len(self.choices) > 8:
                yield Input(placeholder='搜索选项', id='filter')
            yield OptionList(id='options')
            yield Button('0 返回' if self.back != '取消' else '0 取消', id='back')
        yield Footer()

    def on_mount(self):
        self.fill()
        options = self.query_one(OptionList)
        if self.default in self.choices:
            options.highlighted = self.choices.index(self.default)
        elif self.default is not None and self.default == self.back:
            self.query_one('#back', Button).focus()
            return
        options.focus()

    def fill(self, query=''):
        self.indices = [i for i, row in enumerate(self.choices) if query.casefold() in self.describe(row).casefold()]
        options = self.query_one(OptionList)
        options.clear_options()
        options.add_options([literal(self.describe(self.choices[i])) for i in self.indices])
        if self.indices:
            options.highlighted = 0

    def on_key(self, event):
        if event.key in ('up', 'down') and self.focused is self.query_one('#back', Button):
            options = self.query_one(OptionList)
            if self.indices:
                options.focus()
                options.highlighted = len(self.indices) - 1 if event.key == 'up' else 0
                event.stop()
                event.prevent_default()

    def on_input_changed(self, event: Input.Changed):
        self.fill(event.value)

    def on_input_submitted(self, event: Input.Submitted):
        self.query_one(OptionList).focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected):
        self.dismiss(self.choices[self.indices[event.option_index]])

    def on_button_pressed(self, event: Button.Pressed):
        self.action_back()


class Edit(BackScreen):
    BINDINGS = [('escape', 'back', '取消')]
    def __init__(self, title, value, optional=False, password=False):
        super().__init__()
        self.title_text, self.value, self.optional, self.password = title, str(value), optional, password

    def compose(self) -> ComposeResult:
        with Vertical(classes='dialog'):
            yield Static(literal(self.title_text), classes='heading')
            yield Input(self.value, password=self.password, id='value')
            yield Static('Enter 保存 · Esc 取消', id='error')
            with Horizontal(classes='buttons'):
                yield Button('保存', id='save', variant='primary')
                yield Button('取消', id='back')
        yield Footer()

    def on_mount(self):
        self.query_one(Input).focus()

    def save(self):
        value = self.query_one(Input).value.strip()
        if not value and not self.optional:
            self.query_one('#error', Static).update('此项不能为空。')
            return
        self.dismiss(value)

    def on_input_submitted(self, event: Input.Submitted):
        self.save()

    def on_button_pressed(self, event: Button.Pressed):
        self.save() if event.button.id == 'save' else self.action_back()


class CommandEdit(BackScreen):
    BINDINGS = [('escape', 'back', '取消'), ('ctrl+s', 'save', '保存')]

    def __init__(self, value, title="启动命令"):
        super().__init__()
        self.value, self.title_text = value, title

    def compose(self) -> ComposeResult:
        yield Static(self.title_text + ' · Ctrl+S 保存 · Esc 取消', classes='heading')
        yield TextArea(self.value, id='command')
        with Horizontal(classes='buttons'):
            yield Button('保存', id='save')
            yield Button('取消', id='back')
        yield Footer()

    def on_mount(self):
        self.query_one(TextArea).focus()

    def action_save(self):
        self.dismiss(self.query_one(TextArea).text)

    def on_button_pressed(self, event: Button.Pressed):
        self.action_save() if event.button.id == 'save' else self.action_back()


class Details(BackScreen):
    def __init__(self, title, value, *, hint=""):
        super().__init__()
        self.title_text, self.value, self.hint = title, value, hint

    def compose(self) -> ComposeResult:
        yield Static(literal(self.title_text), classes='heading')
        if self.hint:
            yield Static(literal(self.hint), classes='hint')
        yield TextArea(self.value, read_only=True, soft_wrap=True, id='text')
        with Horizontal(classes='buttons'):
            yield Button('复制全部', id='copy')
            yield Button('保存文本', id='save-text')
            yield Button('0 返回', id='back')
        yield Footer()

    def on_mount(self):
        self.query_one(TextArea).focus()

    def on_button_pressed(self, event: Button.Pressed):
        if event.button.id == 'copy':
            self.app.copy_to_clipboard(self.value)
        elif event.button.id == 'save-text':
            from scripts.clipboard import save_text
            def done(path, error):
                self.notify(error or '已保存：' + str(path), severity='error' if error else 'information', timeout=10)
            self.app.task(self, lambda: save_text(self.value), done)
        else:
            self.action_back()


class Operation(BackScreen):
    def __init__(self, title, callback, auto_close=False):
        super().__init__()
        self.title_text, self.callback, self.done = title, callback, False
        self.auto_close = auto_close

    def compose(self) -> ComposeResult:
        yield Static(literal(self.title_text), classes='heading')
        yield Static('处理中…', id='status')
        yield RichLog(wrap=True, markup=False, max_lines=2000, id='output')
        yield Button('0 返回', id='back', disabled=True)
        yield Footer()

    def on_mount(self):
        self.app.task(self, self.callback, self.finished, self.write)

    def write(self, value):
        if self.is_mounted:
            self.query_one(RichLog).write(literal(value.rstrip('\n')))

    def finished(self, result, error):
        if isinstance(result, int) and result != 0 and not error:
            error = '操作未完成，请查看输出信息。'
        self.done = True
        if self.auto_close and not error:
            self.dismiss(None)
            return
        self.query_one('#status', Static).update('操作未完成' if error else '已结束')
        if error:
            self.write(error)
        self.query_one('#back', Button).disabled = False
        self.query_one('#back', Button).focus()

    def action_back(self):
        if self.done:
            super().action_back()
        else:
            self.notify('正在处理，请等待结果。')

    def on_button_pressed(self, event: Button.Pressed):
        self.action_back()


class Browser(BackScreen):
    """Only the current page is rendered. A generation guards stale reads."""
    BINDINGS = [*BackScreen.BINDINGS, ('r', 'refresh', '刷新'), ('/', 'search', '搜索'),
                ('left', 'previous', '上一页'), ('right', 'next', '下一页')]

    def __init__(self, title, source, operate, *, select_mode=False, actions=()):
        super().__init__()
        self.title_text, self.source, self.operate = title, source, operate
        self.index, self.page_size, self.generation = 0, 20, 0
        self.page = None
        self.loading = False
        self.selection = None
        self.select_mode = select_mode
        self.actions = actions

    def compose(self) -> ComposeResult:
        yield Static(literal(self.title_text), classes='heading')
        if self.actions:
            with Horizontal(classes='buttons'):
                for key, label, _ in self.actions:
                    yield Button(label, id='service-' + key, variant='primary' if key == 'create' else 'default')
        with Horizontal(classes='filters'):
            yield Input(placeholder=self.source.search_hint, id='search')
            if self.source.states:
                yield Select([(s, s) for s in self.source.states], value=self.source.states[0], allow_blank=False, id='state')
        yield DataTable(cursor_type='row', zebra_stripes=True, id='rows')
        yield Static('加载中…', id='counter')
        with Horizontal(classes='buttons'):
            yield Button('上一页', id='previous')
            yield Button('下一页', id='next')
            yield Button('刷新', id='refresh')
            yield Button('0 返回', id='back')
        yield Footer()

    def on_mount(self):
        self.query_one(DataTable).add_columns(*self.source.columns)
        self.query_one(DataTable).focus()
        self.load()

    def load(self, refresh=False):
        # Avoid accumulating network reads while one is in flight.
        if self.loading:
            return
        self.loading = True
        self.generation += 1
        generation = self.generation
        query = self.query_one('#search', Input).value.strip()
        state = self.query_one('#state', Select).value if self.source.states else ''
        self.query_one('#counter', Static).update(getattr(self.source, 'loading_hint', '加载中…') + ' · 可按 0 返回')
        self.query_one(DataTable).disabled = True
        for button in self.query('.buttons Button'):
            button.disabled = button.id != 'back'
        self.query_one('#search', Input).disabled = True
        if self.source.states:
            self.query_one('#state', Select).disabled = True
        def finished(page, error):
            if not self.is_mounted or generation != self.generation:
                return
            self.loading = False
            self.query_one('#search', Input).disabled = False
            if self.source.states:
                self.query_one('#state', Select).disabled = False
            self.query_one('#refresh', Button).disabled = False
            for button in self.query('.buttons Button'):
                if button.id.startswith('service-'):
                    button.disabled = False
            table = self.query_one(DataTable)
            table.clear()
            self.page = page
            if error:
                self.query_one('#counter', Static).update(literal(error + ' · 刷新重试'))
                return
            table.disabled = False
            self.render_rows()
            self.query_one('#counter', Static).update(
                f'第 {self.index + 1} 页 · 本页 {len(page.rows)} 条' +
                (f' · 共 {page.total} 条' if page.total is not None else '') +
                (' · Enter 选择镜像' if self.select_mode else ' · Enter 查看操作'))
            self.query_one('#previous', Button).disabled = self.index == 0
            self.query_one('#next', Button).disabled = not page.more
            if not page.rows:
                message = '没有符合筛选条件的结果，可清空搜索或刷新。' if query or state not in ('', '全部') else ('暂无资源，可点击上方按钮创建或上传。' if self.actions else '暂无可选资源，请刷新或更换范围。')
                self.query_one('#counter', Static).update(message)
            table.focus()
        self.app.task(self, lambda: self.source.page(self.index, self.page_size, query, state, refresh), finished)

    def render_rows(self):
        if self.page is None:
            return
        table = self.query_one(DataTable)
        table.clear(columns=True)
        count = len(self.source.columns)
        width = max(30, self.size.width - 4)
        widths = ([max(12, width - 37), 14, 19] if count == 3 else
                  [max(12, width - 12), 8] if count == 2 else [width - 2])
        for label, width in zip(self.source.columns, widths):
            table.add_column(label, width=width)
        for i, row in enumerate(self.page.rows):
            table.add_row(*(clipped(value, width) for value, width in zip(self.source.cells(row), widths)), key=str(i))
            if self.selection is not None and row_identity(row) == self.selection:
                table.move_cursor(row=i)

    def on_resize(self):
        if self.is_mounted and self.page is not None and not self.loading:
            self.render_rows()

    def action_back(self):
        self.generation += 1
        super().action_back()

    def action_search(self):
        if not self.loading:
            self.query_one('#search', Input).focus()

    def on_input_submitted(self, event: Input.Submitted):
        self.index = 0
        self.load()

    def on_select_changed(self, event: Select.Changed):
        if self.is_mounted and not self.loading:
            self.index = 0
            self.load()

    def action_refresh(self):
        if not self.loading:
            self.load(refresh=True)

    def action_previous(self):
        if not self.loading and self.index > 0:
            self.index -= 1
            self.load()

    def action_next(self):
        if not self.loading and self.page and self.page.more:
            self.index += 1
            self.load()

    def on_button_pressed(self, event: Button.Pressed):
        if event.button.id.startswith('service-'):
            key = event.button.id.removeprefix('service-')
            _, title, callback = next(action for action in self.actions if action[0] == key)
            self.app.push_screen(Operation(title, callback), lambda _: self.load(refresh=True))
        else:
            getattr(self, 'action_' + event.button.id)()

    def on_data_table_row_selected(self, event: DataTable.RowSelected):
        if self.loading or self.page is None:
            return
        row = self.page.rows[int(event.row_key.value)]
        if self.select_mode:
            self.dismiss(row)
            return
        self.selection = row_identity(row)
        self.app.push_screen(Operation('资源操作', lambda: self.operate(row)), lambda _: self.load(refresh=getattr(self.source, 'refresh_after_action', True)))


class Form(BackScreen):
    BINDINGS = [*BackScreen.BINDINGS, ('ctrl+s', 'review', '检查配置')]

    def __init__(self, draft):
        super().__init__()
        self.draft, self.busy = draft, False

    def compose(self) -> ComposeResult:
        yield Static(self.draft.title, classes='heading')
        yield Static('方向键选择 · Enter 编辑 · 使用默认值可直接检查配置', classes='hint')
        yield DataTable(cursor_type='row', zebra_stripes=True, id='fields')
        yield Static('', id='status')
        with Horizontal(classes='buttons'):
            yield Button('提交创建' if self.draft.previous else '检查配置', id='review', variant='primary')
            yield Button('0 返回', id='back')
        yield Footer()

    def on_mount(self):
        table = self.query_one(DataTable)
        table.add_column('配置项', width=14)
        table.add_column('当前值')
        self.render_fields()
        table.focus()
        self.perform(self.draft.initialize)

    def render_fields(self):
        table = self.query_one(DataTable)
        position = table.cursor_row
        table.clear(columns=True)
        table.add_column('配置项', width=14)
        width = max(18, self.size.width - 21)
        table.add_column('当前值', width=width)
        for key, name, value in self.draft.rows():
            table.add_row(literal(name), clipped(value, width), key=key)
        table.move_cursor(row=position)

    def on_resize(self):
        if self.is_mounted and self.query('#fields'):
            self.render_fields()

    def perform(self, callback, review=False):
        if self.busy:
            return
        self.busy = True
        self.query_one('#status', Static).update('正在读取和检查…')
        self.query_one(DataTable).disabled = True
        self.query_one('#review', Button).disabled = True
        def finished(result, error):
            self.busy = False
            if not self.is_mounted:
                return
            self.query_one(DataTable).disabled = False
            self.query_one('#review', Button).disabled = False
            self.query_one('#status', Static).update(literal(error or '配置已更新'))
            self.render_fields()
            self.query_one(DataTable).focus()
            if not error and review:
                self.dismiss(result)
        self.app.task(self, callback, finished,
                      lambda value: self.query_one('#status', Static).update(literal(value.strip())))

    def on_data_table_row_selected(self, event: DataTable.RowSelected):
        self.perform(lambda: self.draft.edit(event.row_key.value))

    def action_review(self):
        self.draft.submit_requested = self.draft.previous is not None
        self.perform(self.draft.build, review=True)

    def action_back(self):
        super().action_back()

    def on_button_pressed(self, event: Button.Pressed):
        getattr(self, 'action_' + event.button.id)()


class SlaiApp(App):
    TITLE = 'SLAI-tool'
    ENABLE_COMMAND_PALETTE = False
    CSS_PATH = 'tui.tcss'
    BINDINGS = [Binding('ctrl+q', 'quit', '退出'), Binding('0', 'quit', '退出')]

    def __init__(self, initial=None):
        super().__init__()
        self.initial = initial
        self.pending = set()
        self.identity_cache = {}

    def compose(self) -> ComposeResult:
        yield Static('SLAI-tool', id='identity', classes='heading')
        from scripts import onboarding
        yield Static(literal(onboarding.state()[1]), classes='hint', id='getting-started')
        with Horizontal(classes='buttons'):
            yield Button('开始设置', id='home-setup', variant='primary')
            yield Button('使用指南', id='home-help')
        yield OptionList(*[literal(f'{i}. {title}') for i, (_, title) in enumerate(cli.MENU_ITEMS, 1)], id='home')
        yield Footer()

    def on_mount(self):
        self.query_one(OptionList).focus()
        self.refresh_identity()
        from scripts import onboarding
        if onboarding.state()[0] in ('account', 'workspace'):
            self.query_one('#home-setup', Button).focus()
        if isinstance(self.initial, str):
            self.open_service(self.initial)
        elif self.initial:
            self.open_operation('SLAI-tool', self.initial)

    def refresh_identity(self):
        from scripts import onboarding
        kind, hint = onboarding.state()
        self.query_one('#getting-started', Static).update(literal(hint))
        button = self.query_one('#home-setup', Button)
        button.display = kind != 'ready'
        button.label = {'account':'开始设置','workspace':'选择工作空间','error':'查看配置问题'}.get(kind,'开始设置')
        self.task(self.screen, lambda: cli.menu_title(self.identity_cache),
                  lambda value, error: self.query_one('#identity', Static).update(literal(value or 'SLAI-tool')))

    def open_service(self, name):
        from scripts import onboarding
        if onboarding.state()[0] == 'account':
            self.open_operation('首次设置', onboarding.start)
            return
        self.push_screen(Operation(name.upper(), lambda: cli.run_service(name, ['list']), auto_close=True))

    def open_operation(self, title, callback):
        self.push_screen(Operation(title, callback), lambda _: self.refresh_identity())

    def on_option_list_option_selected(self, event: OptionList.OptionSelected):
        if event.option_list.id != 'home':
            return
        self.open_item(event.option_index)

    def on_key(self, event):
        if len(self.screen_stack) == 1 and event.character and event.character in ''.join(str(i) for i in range(1,len(cli.MENU_ITEMS)+1)):
            event.stop()
            self.open_item(int(event.character) - 1)

    def open_item(self, index):
        action, title = cli.MENU_ITEMS[index]
        if action in cli.SERVICES:
            self.open_service(action)
        else:
            self.open_operation(title, lambda: cli.execute(action))

    def on_button_pressed(self, event: Button.Pressed):
        from scripts import onboarding
        if event.button.id == 'home-setup':
            self.open_operation('首次设置', onboarding.start)
        elif event.button.id == 'home-help':
            self.push_screen(Details('使用指南', onboarding.GUIDE))

    def copy_to_clipboard(self, text):
        from scripts.clipboard import copy_text
        def done(_, error):
            self.notify(error or '已复制到系统剪贴板', severity='error' if error else 'information', timeout=8)
        self.task(self.screen, lambda: copy_text(text), done)

    def task(self, owner, callback, finished, sink=None):
        bridge = Bridge(self, sink, owner)
        def work():
            token = ui._backend.set(bridge)
            result, error = None, None
            try:
                result = callback()
            except ui.Cancelled:
                error = '已取消操作。'
            except SystemExit as exc:
                result = exc.code or 0
                if result:
                    error = '参数无效，请使用服务名后加 --help 查看用法。'
            except (cli.ConfigError, OSError) as exc:
                error = str(exc) if isinstance(exc, cli.ConfigError) else '无法读取配置或执行命令，请检查网络、路径和权限。'
            except Exception:
                # Do not leak credential-bearing provider responses or command arguments.
                error = '操作未完成，请检查配置并重试。'
            finally:
                ui._backend.reset(token)
            if self.is_running:
                self.call_from_thread(lambda: finished(result, error) if owner.is_mounted else None)
        owner.run_worker(work, thread=True, exit_on_error=False)

    def action_quit(self):
        if len(self.screen_stack) > 1:
            action = getattr(self.screen, 'action_back', None)
            if action:
                action()
            return
        self.exit()

    def on_unmount(self):
        for result in list(self.pending):
            if not result.done():
                result.set_exception(ui.Cancelled())


def run(initial=None):
    SlaiApp(initial).run()
    return 0
