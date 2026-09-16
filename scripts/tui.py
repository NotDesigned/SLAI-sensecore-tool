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
        self.deadline = None

    def catalog_read(self, config, url, proxy, fetch):
        import copy
        import time
        from scripts.ccr_cache import account_key
        self.request_timeout(20)
        key = (account_key(config), url, proxy)
        with self.app.catalog_lock:
            cached = self.app.catalog.get(key)
            if cached and time.monotonic() - cached[0] < 30:
                return copy.deepcopy(cached[1])
            generation = self.app.catalog_generation
        value = fetch()
        with self.app.catalog_lock:
            if generation == self.app.catalog_generation:
                if len(self.app.catalog) >= 64:
                    self.app.catalog.pop(next(iter(self.app.catalog)))
                self.app.catalog[key] = (time.monotonic(), copy.deepcopy(value))
        return value

    def mark_changed(self):
        def mark():
            if self.owner is not None and not self.owner.is_mounted:
                raise ui.Cancelled()
            self.app.clear_catalog()
            if isinstance(self.owner, Operation):
                self.owner.changed = True
                self.owner.query_one('#back', Button).disabled = not self.owner.done
        self.app.call_from_thread(mark)

    def request_timeout(self, timeout, writing=False):
        import time
        if self.owner is not None and not self.owner.is_mounted:
            raise ui.Cancelled()
        if writing:
            self.mark_changed()
        if isinstance(self.owner, Operation) and self.owner.changed:
            return timeout
        if self.deadline is None:
            self.deadline = time.monotonic() + max(60, timeout)
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise cli.ConfigError('本次查询等待过久，请返回后重试。')
        return min(timeout, remaining)

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
                    value = result.result(timeout=0.1)
                    self.deadline = None  # User input must not consume a query budget.
                    return value
                except FutureTimeout:
                    if get_current_worker().is_cancelled or not self.app.is_running:
                        raise ui.Cancelled
        finally:
            result.cancel()
            self.app.pending.discard(result)

    def output(self, value):
        self.history = (self.history + value)[-24000:]
        if self.sink is not None:
            self.app.call_from_thread(lambda: self.sink(value) if self.owner is None or self.owner.is_mounted else None)

    def choose(self, title, items, describe, default, header=''):
        if not items:
            raise cli.ConfigError(title + '没有可选项，请检查权限和上级选择。')
        back = next((x for x in items if isinstance(x, str) and x in ('返回', '返回列表', '取消')), None)
        choices = [x for x in items if x is not back]
        context = self.history if isinstance(default, str) and default in ('取消', '仅保存配置', '提交创建') else ''
        value = self.request(Picker(title, choices, describe, default, back, context, header))
        if value is None:
            if isinstance(self.owner, Operation):
                self.owner.cancelled = True
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

    def show_text(self, title, value, *, hint="", copy_label="复制全部"):
        result = self.request(Details(title, value, hint=hint, copy_label=copy_label))
        if isinstance(self.owner, Operation):
            self.owner.result_seen = True
        return result

    def browse(self, title, source, operate, *, actions=()):
        result = self.request(Browser(title, source, operate, actions=actions))
        if isinstance(self.owner, Operation):
            self.owner.result_seen = True
        return result

    def select_resource(self, title, source):
        value = self.request(Browser(title, source, None, select_mode=True))
        if value is None:
            raise ui.Cancelled
        return value

    def operation(self, title, callback):
        return self.request(Operation(title, callback, auto_close=True, keep_output=True))

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
    def __init__(self, title, choices, describe=str, default=None, back=None, context='', header=''):
        super().__init__()
        self.title_text, self.choices, self.describe = title, choices, describe
        self.header = header
        self.default = ui.choice_default([*choices, *([back] if back is not None else [])], default)
        self.back, self.context = back, context
        self.indices = list(range(len(choices)))

    def compose(self) -> ComposeResult:
        with Vertical(classes='dialog'):
            yield Static(literal(self.title_text), classes='heading')
            if self.context:
                yield TextArea(self.context, read_only=True, classes='review-context')
            if len(self.choices) > 8:
                yield Input(placeholder='搜索选项', id='filter')
            if self.header:
                yield Static(literal(self.header), classes='table-head')
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
    def __init__(self, title, value, *, hint="", copy_label="复制全部"):
        super().__init__()
        self.title_text, self.value, self.hint = title, value, hint
        self.copy_label = copy_label

    def compose(self) -> ComposeResult:
        yield Static(literal(self.title_text), classes='heading')
        if self.hint:
            yield Static(literal(self.hint), classes='hint')
        yield TextArea(self.value, read_only=True, soft_wrap=True, id='text')
        with Horizontal(classes='buttons'):
            if self.copy_label is not None:
                yield Button(self.copy_label, id='copy')
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
    def __init__(self, title, callback, auto_close=False, keep_output=False, notify_success=False):
        super().__init__()
        self.title_text, self.callback, self.done = title, callback, False
        self.auto_close, self.keep_output, self.notify_success = auto_close, keep_output, notify_success
        self.has_output = self.result_seen = self.cancelled = False
        self.failure_title = '操作未完成'
        self.changed = False

    def compose(self) -> ComposeResult:
        yield Static(literal(self.title_text), classes='heading')
        yield Static('处理中…', id='status')
        yield RichLog(wrap=True, markup=False, max_lines=2000, id='output')
        yield Button('0 返回', id='back')
        yield Footer()

    def on_mount(self):
        self.app.task(self, self.callback, self.finished, self.write)

    def write(self, value):
        self.has_output |= bool(value.strip())
        if self.is_mounted:
            self.query_one(RichLog).write(literal(value.rstrip('\n')))

    def finished(self, result, error):
        if type(result) is int and result != 0 and not error:
            error = '操作未完成，请查看输出信息。'
        self.done = True
        if self.cancelled and not self.changed:
            self.dismiss(False)
            return
        if self.auto_close and not error and (not self.keep_output or not self.has_output or self.result_seen or self.changed):
            if self.changed or self.notify_success:
                self.notify(self.title_text + '已完成')
            self.dismiss(self.changed)
            return
        self.query_one('#status', Static).update(self.failure_title if error else '已结束')
        if error:
            self.write(error)
        self.query_one('#back', Button).disabled = False
        self.query_one('#back', Button).focus()

    def action_back(self):
        if self.done or not self.changed:
            self.dismiss(self.changed)
        else:
            self.notify('请求已提交，正在确认结果，请稍候。')

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
        self.fetching = False
        self.selection = None
        self.displayed_index = 0
        self.pending_refresh = False
        self.applied_query, self.applied_state = '', ''
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
            yield Button(getattr(self.source, 'refresh_label', '刷新'), id='refresh')
            yield Button('0 返回', id='back')
        yield Footer()

    def on_mount(self):
        self.query_one(DataTable).add_columns(*self.source.columns)
        self.query_one(DataTable).focus()
        if getattr(type(self.source),'background_capable',False):
            self.source.enable_background()
            self.set_interval(.5,self.poll_source)
        self.load()

    def poll_source(self):
        if self.fetching or self.app.screen is not self:
            return
        if self.source.poll_background() != self.source.background_token:
            table = self.query_one(DataTable)
            if self.page and self.page.rows and table.cursor_row < len(self.page.rows):
                self.selection = row_identity(self.page.rows[table.cursor_row])
            self.load(background=True)

    def load(self, refresh=False, background=False):
        # Avoid accumulating network reads while one is in flight.
        if self.fetching:
            self.pending_refresh |= refresh
            return
        self.fetching = True
        self.generation += 1
        generation = self.generation
        if background:
            query, state = self.applied_query, self.applied_state
        else:
            query = self.query_one('#search', Input).value.strip()
            state = self.query_one('#state', Select).value if self.source.states else ''
            self.applied_query, self.applied_state = query, state
        self.query_one('#counter', Static).update(getattr(self.source, 'loading_hint', '加载中…') + ' · 可按 0 返回')
        self.query_one(DataTable).disabled = not background
        for button in self.query('.buttons Button'):
            button.disabled = not background and button.id != 'back' and not button.id.startswith('service-')
        self.query_one('#search', Input).disabled = not background
        if self.source.states:
            self.query_one('#state', Select).disabled = not background
        def finished(page, error):
            if not self.is_mounted or generation != self.generation:
                return
            self.fetching = False
            if self.pending_refresh:
                self.pending_refresh = False
                self.load(refresh=True)
                return
            self.query_one('#search', Input).disabled = False
            if self.source.states:
                self.query_one('#state', Select).disabled = False
            self.query_one('#refresh', Button).disabled = False
            for button in self.query('.buttons Button'):
                if button.id.startswith('service-'):
                    button.disabled = False
            table = self.query_one(DataTable)
            if error:
                self.index = self.displayed_index
                table.disabled = self.page is None
                if self.page is not None:
                    self.query_one('#previous', Button).disabled = self.index == 0
                    self.query_one('#next', Button).disabled = not self.page.more
                self.query_one('#counter', Static).update(literal(error + (' · 保留上次结果 · ' + getattr(self.source,'status_hint','') if self.page else '') + ' · 刷新重试'))
                return
            if (not page.rows and page.total is None and self.page is not None
                    and (self.page.rows or self.page.total is not None)
                    and (getattr(self.source,'pending',False) or getattr(self.source,'background_error',False))):
                failed = getattr(self.source,'background_error',False)
                if failed:
                    self.index = self.displayed_index
                table.disabled = not failed
                self.query_one('#counter', Static).update(literal(self.source.status_hint + ' · 保留上次结果'))
                self.query_one('#previous', Button).disabled = self.index == 0
                self.query_one('#next', Button).disabled = not self.page.more
                return
            if not page.rows and self.index > 0 and not getattr(self.source,'pending',False) and not getattr(self.source,'background_error',False):
                self.index = max(0, (page.total - 1) // self.page_size) if page.total is not None else 0
                self.load()
                return
            self.displayed_index = self.index
            table.clear()
            self.page = page
            table.disabled = False
            self.render_rows()
            self.query_one('#counter', Static).update(
                f'第 {self.index + 1} 页 · 本页 {len(page.rows)} 条' +
                (f' · 共 {page.total} 条' if page.total is not None else '') +
                (' · Enter 选择镜像' if self.select_mode else ' · Enter 查看操作') +
                (' · ' + self.source.status_hint if getattr(self.source,'status_hint','') else ''))
            self.query_one('#previous', Button).disabled = self.index == 0
            self.query_one('#next', Button).disabled = not page.more
            if not page.rows:
                if getattr(type(self.source),'background_capable',False) and (self.source.pending or getattr(self.source,'background_error',False)):
                    message = self.source.status_hint
                else:
                    message = '没有符合筛选条件的结果，可清空搜索或刷新。' if query or state not in ('', '全部') else ('暂无资源，可点击上方按钮创建或上传。' if self.actions else '暂无可选资源，请刷新或更换范围。')
                self.query_one('#counter', Static).update(message)
            if not background and self.app.screen is self:
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
        if self.is_mounted and self.page is not None and not self.fetching:
            self.render_rows()

    def action_back(self):
        self.generation += 1
        super().action_back()

    def action_search(self):
        if not self.fetching:
            self.query_one('#search', Input).focus()

    def on_input_submitted(self, event: Input.Submitted):
        self.index = 0
        self.load()

    def on_select_changed(self, event: Select.Changed):
        if self.is_mounted and not self.fetching:
            self.index = 0
            self.load()

    def action_refresh(self):
        if not self.fetching:
            self.app.clear_catalog()
            self.load(refresh=True)

    def action_previous(self):
        if not self.fetching and self.index > 0:
            self.index -= 1
            self.load()

    def action_next(self):
        if not self.fetching and self.page and self.page.more:
            self.index += 1
            self.load()

    def on_button_pressed(self, event: Button.Pressed):
        if event.button.id.startswith('service-'):
            key = event.button.id.removeprefix('service-')
            _, title, callback = next(action for action in self.actions if action[0] == key)
            def completed(changed):
                if changed:
                    if self.fetching:
                        self.pending_refresh = True
                        return
                    if getattr(self.source, 'reuse_cache_after_action', False):
                        self.source.snapshot = None
                        self.load()
                    else:
                        self.load(refresh=True)
            self.app.push_screen(Operation(title, callback), completed)
        else:
            getattr(self, 'action_' + event.button.id)()

    def on_data_table_row_selected(self, event: DataTable.RowSelected):
        if self.fetching or self.page is None:
            return
        row = self.page.rows[int(event.row_key.value)]
        if self.select_mode:
            self.dismiss(row)
            return
        self.selection = row_identity(row)
        self.app.push_screen(Operation('资源操作', lambda: self.operate(row), auto_close=True, keep_output=True), lambda changed: self.load(refresh=True) if changed else None)


class Form(BackScreen):
    BINDINGS = [*BackScreen.BINDINGS, ('ctrl+s', 'review', '提交创建')]

    def __init__(self, draft):
        super().__init__()
        self.draft, self.busy = draft, False

    def compose(self) -> ComposeResult:
        yield Static(self.draft.title, classes='heading')
        yield Static('方向键选择 · Enter 编辑 · 填写完成后提交创建；仅保存可使用旁边按钮', classes='hint')
        yield DataTable(cursor_type='row', zebra_stripes=True, id='fields')
        yield Static('', id='status')
        with Horizontal(classes='buttons'):
            yield Button('提交创建', id='review', variant='primary')
            yield Button('仅保存配置', id='save')
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
        self.query_one('#save', Button).disabled = True
        def finished(result, error):
            self.busy = False
            if not self.is_mounted:
                return
            self.query_one(DataTable).disabled = False
            self.query_one('#review', Button).disabled = False
            self.query_one('#save', Button).disabled = False
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
        if self.busy:
            return
        self.draft.submit_requested, self.draft.save_requested = True, False
        self.perform(self.draft.build, review=True)

    def action_save(self):
        if self.busy:
            return
        self.draft.submit_requested, self.draft.save_requested = False, True
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
        self.proxy_generation = 0
        import threading
        self.catalog = {}
        self.catalog_lock = threading.Lock()
        self.catalog_generation = 0

    def clear_catalog(self):
        from scripts import listing
        listing.invalidate()
        with self.catalog_lock:
            self.catalog.clear()
            self.catalog_generation += 1

    def compose(self) -> ComposeResult:
        yield Static('SLAI-tool', id='identity', classes='heading')
        from scripts import onboarding
        yield Static(literal(onboarding.state()[1]), classes='hint', id='getting-started')
        with Horizontal(classes='buttons'):
            yield Button('开始设置', id='home-setup', variant='primary')
            yield Button('配置账户', id='home-account')
            yield Button('工作空间', id='home-workspace')
            yield Button('配置 SOCKS5', id='home-proxy')
            yield Button('使用指南', id='home-help')
        with Horizontal(classes='proxy-bar'):
            yield Static('SOCKS5：等待检测', id='proxy-status')
            yield Button('检测代理', id='home-proxy-check')
        yield OptionList(*[literal(f'{i}. {title}\n') for i, (_, title) in enumerate(cli.MENU_ITEMS, 1)], id='home')
        yield Footer()

    def on_mount(self):
        self.query_one(OptionList).focus()
        self.refresh_identity()
        self.refresh_proxy()
        if not self.is_headless:
            try:
                from scripts.ccr import prefetch_default
                prefetch_default(cli.load_config())
            except (cli.ConfigError,OSError):
                pass
        from scripts import onboarding
        if onboarding.state()[0] in ('account', 'workspace'):
            self.query_one('#home-setup', Button).focus()
        if isinstance(self.initial, str):
            self.open_service(self.initial)
        elif self.initial:
            self.push_screen(Operation('SLAI-tool', self.initial, auto_close=True, keep_output=True), lambda _: self.refresh_identity())

    def refresh_identity(self):
        from scripts import onboarding
        kind, hint = onboarding.state()
        self.query_one('#getting-started', Static).update(literal(hint))
        button = self.query_one('#home-setup', Button)
        button.display = kind != 'ready'
        button.label = {'account':'开始设置','workspace':'选择工作空间','error':'查看配置问题'}.get(kind,'开始设置')
        self.task(self.screen, lambda: cli.menu_title(self.identity_cache),
                  lambda value, error: self.query_one('#identity', Static).update(literal(value or 'SLAI-tool')))

    def refresh_proxy(self):
        from scripts import proxy_settings
        self.proxy_generation += 1
        generation = self.proxy_generation
        self.query_one('#proxy-status', Static).update('SOCKS5：正在检测（最多约 8 秒）…')
        self.query_one('#home-proxy-check', Button).disabled = True
        def finished(value, error):
            if generation != self.proxy_generation:
                return
            self.query_one('#proxy-status', Static).update(literal(value or 'SOCKS5：检测未完成'))
            self.query_one('#home-proxy-check', Button).disabled = False
        self.task(self.screen_stack[0], proxy_settings.status, finished)

    def open_service(self, name):
        from scripts import onboarding
        if onboarding.state()[0] == 'account':
            self.open_operation('首次设置', onboarding.start, auto_close=True)
            return
        self.push_screen(Operation(name.upper(), lambda: cli.run_service(name, ['list']), auto_close=True))

    def open_operation(self, title, callback, *, auto_close=False):
        self.push_screen(Operation(title, callback, auto_close=auto_close, notify_success=auto_close), lambda _: self.refresh_identity())

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
            self.open_operation('首次设置', onboarding.start, auto_close=True)
        elif event.button.id == 'home-account':
            self.open_operation('配置账户', lambda: cli.execute('configure'), auto_close=True)
        elif event.button.id == 'home-workspace':
            self.open_operation('选择工作空间', lambda: cli.execute('workspace'), auto_close=True)
        elif event.button.id == 'home-help':
            self.push_screen(Details('使用指南', onboarding.GUIDE))
        elif event.button.id == 'home-proxy-check':
            self.refresh_proxy()
        elif event.button.id == 'home-proxy':
            from scripts import proxy_settings
            def finished(_):
                self.refresh_identity()
                self.refresh_proxy()
            self.push_screen(Operation('配置 SOCKS5 代理', proxy_settings.configure, auto_close=True), finished)

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
                if isinstance(owner, Operation):
                    owner.cancelled = True
                error = '操作已中断，已有请求可能已提交，请刷新列表确认。' if isinstance(owner, Operation) and owner.changed else '已取消操作。'
            except SystemExit as exc:
                result = exc.code or 0
                if result:
                    error = '参数无效，请使用服务名后加 --help 查看用法。'
            except (cli.ConfigError, OSError) as exc:
                if isinstance(owner, Operation) and isinstance(exc, cli.ConfigError) and exc.title:
                    owner.failure_title = exc.title
                error = str(exc) if isinstance(exc, cli.ConfigError) else '无法读取配置或执行命令，请检查网络、路径和权限。'
            except Exception as exc:
                # Do not leak credential-bearing provider responses or command arguments.
                error = f'程序异常（{type(exc).__name__}），请记录异常类型并反馈；未显示可能含密钥的异常内容。'
            finally:
                ui._backend.reset(token)
            if self.is_running:
                self.call_from_thread(lambda: finished(result, error) if owner.is_mounted else None)
        def launch():
            if owner.is_mounted:
                owner.run_worker(work, thread=True, exit_on_error=False)
        # on_mount runs before is_mounted becomes true. A fast worker would
        # otherwise cancel its first prompt or lose its completion callback.
        if owner.is_mounted:
            launch()
        else:
            owner.call_after_refresh(launch)

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
