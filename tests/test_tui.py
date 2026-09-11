"""Keyboard, cancellation, paging and draft validation through the actual UI."""
import asyncio
import json
import threading
import unittest
from unittest.mock import Mock, patch

from textual.widgets import DataTable, Input, TextArea, Static
from scripts import acp, cli, ui
from scripts.forms import CreateDraft
from scripts.listing import LocalSource
from scripts.tui import SlaiApp, Browser, Form, Edit, Picker, Operation, CommandEdit


def fake_client():
    client = Mock()
    client.config = {'cci': {'ssh_enabled': False}}
    client.clusters.return_value = [dict(name='pool', zone='cn-sh-01e', properties={'vpc_id': 'vpc'})]
    client.specs.return_value = [dict(ZONE='cn-sh-01e', **{'WORKER SPEC': 'small', 'VCPU COUNT': '2',
        'MEMORY(GIB)': '4', 'CHIP COUNT': '1', 'RESOURCE KEY': 'nvidia.com/gpu'})]
    client.resources.return_value = [dict(name='afs-share-01e', id='volume', zone='cn-sh-01e')]
    client.current_username.return_value = 'test-user'
    return client


class SourcesTests(unittest.TestCase):
    def test_local_search_reuses_snapshot_and_refreshes(self):
        fetch = Mock(return_value=[f'image-{i}' for i in range(10000)])
        source = LocalSource(fetch)
        self.assertEqual(len(source.page(0, 20).rows), 20)
        self.assertEqual(source.page(0, 20, 'image-9999').rows, ['image-9999'])
        source.page(1, 20)
        fetch.assert_called_once()
        source.page(0, 20, refresh=True)
        self.assertEqual(fetch.call_count, 2)

    def test_acp_reads_one_page_and_scopes_owner(self):
        client = object.__new__(acp.Client)
        client.user_id = 'me'
        client.config = {}
        client._workspace = dict(name='ws', region='cn-sh-01', subscription_name='sub', resource_group_name='group', zone='cn-sh-01z')
        data = {'training_jobs': [{'name': 'a', 'uid': '1', 'ownership': {'user_id': 'me'}},
                {'name': 'b', 'uid': '2', 'ownership': {'user_id': 'other'}}], 'total_size': 8, 'next_page_token': '4'}
        with patch.object(acp, 'get_json', return_value=data) as get:
            page = client.jobs_page('ws', 2, 2, 'job', 'RUNNING')
        self.assertEqual([r['name'] for r in page.rows], ['a'])
        self.assertTrue(page.more)
        self.assertIsNone(page.total)
        from urllib.parse import urlsplit, parse_qs
        url = get.call_args.args[1]
        self.assertEqual(urlsplit(url).hostname, 'aec2.cn-sh-01.sensecoreapi.cn')
        query = parse_qs(urlsplit(url).query)
        self.assertEqual(query['page_token'], ['3'])
        self.assertEqual(query['filter'], ["creator_id='me' AND state='RUNNING'"])
        self.assertEqual(query['name'], ['job'])
        get.assert_called_once()

    def test_acp_uses_cli_create_time_and_keeps_state_visible(self):
        source = acp.JobSource(Mock(), 'ws')
        cells = source.cells(dict(name='test', state='SUCCEEDED', create_time='2026-09-11T10:00:00+08:00'))
        self.assertEqual(cells, ('test', 'SUCCEEDED', '2026-09-11 02:00:00'))

    def test_failed_refresh_does_not_replace_snapshot(self):
        source = LocalSource(Mock(side_effect=[['a'], cli.ConfigError('offline')]))
        source.page(0, 20)
        with self.assertRaises(cli.ConfigError):
            source.page(0, 20, refresh=True)
        self.assertEqual(source.snapshot, ['a'])


class DraftTests(unittest.TestCase):
    def draft(self, kind='cci'):
        client = fake_client()
        draft = CreateDraft(kind, client, {'name': 'ws'})
        draft.initialize()
        return draft

    def test_defaults_and_build_keep_cloud_contract(self):
        draft = self.draft()
        self.assertEqual(draft.values['mounts'][0]['subdir'], '/test-user')
        with self.assertRaisesRegex(cli.ConfigError, 'DNAT'):
            draft.build()
        draft.values['network_set'] = True
        ws, name, ports, document, network = draft.build()
        self.assertIsNone(network)
        self.assertEqual(ws, 'ws')
        self.assertEqual(document['template']['containers'][0]['resource_request']['nvidia.com/gpu'], '1')
        self.assertEqual(document['scheduling'], {'priority': 'NORMAL', 'quota_type': 'RESERVED'})
        draft.client.create.assert_not_called()

    def test_cluster_change_invalidates_network_and_failed_edit_rolls_back(self):
        draft = self.draft()
        draft.values.update(network={'body': 'selected'}, network_set=True)
        next_pool = dict(name='other', zone='cn-sh-01e', properties={'vpc_id': 'different'})
        draft.client.clusters.return_value = [draft.values['cluster'], next_pool]
        draft.cache.clear()
        with patch.object(ui, 'choose', return_value=next_pool):
            draft.edit('cluster')
        self.assertIsNone(draft.values['network'])
        self.assertFalse(draft.values['network_set'])
        self.assertEqual(draft.values['vpc'], 'different')
        draft.client.specs.side_effect = cli.ConfigError('offline')
        original = dict(draft.values)
        with patch.object(ui, 'choose', return_value=dict(name='third', zone='cn-sh-01e', properties={})), self.assertRaises(cli.ConfigError):
            draft.edit('cluster')
        self.assertEqual(draft.values, original)

    def test_acp_empty_command_blocks_and_entrypoint_preserves_arguments(self):
        draft = self.draft('acp')
        with self.assertRaisesRegex(cli.ConfigError, '命令'):
            draft.build()
        draft.values.update(entrypoint=True, command='/entrypoint.sh "hello world" "$(literal)"')
        name, args = draft.build()
        self.assertEqual(args['roles'][0]['startup_script'], "exec /entrypoint.sh 'hello world' '$(literal)'")
        draft.client.create.assert_not_called()

    def test_revisiting_pool_reuses_specs_mounts_and_identity(self):
        draft = self.draft('acp')
        first = draft.values['cluster']
        other = dict(first, name='other', properties={'vpc_id': 'other-vpc'})
        draft.set_cluster(other)
        draft.set_cluster(first)
        draft.edit('cluster')  # The only pool remains selected; no new read.
        self.assertEqual(draft.client.specs.call_count, 2)
        draft.client.clusters.assert_called_once()
        draft.client.resources.assert_called_once()
        draft.client.current_username.assert_called_once()
        self.assertEqual(draft.values['vpc'], 'vpc')

    def test_pool_specs_and_storage_reads_overlap(self):
        client = fake_client()
        started = threading.Event()
        specs = client.specs.return_value
        def read_specs(*args):
            if not started.wait(2):
                raise AssertionError('AFS was not requested alongside specs')
            return specs
        volumes = client.resources.return_value
        def read_volumes(*args):
            started.set()
            return volumes
        client.specs.side_effect = read_specs
        client.resources.side_effect = read_volumes
        draft = CreateDraft('acp', client, {'name': 'ws'})
        draft.initialize()
        self.assertEqual(draft.values['mounts'][0]['subdir'], '/test-user')


class TuiTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.identity = patch.object(cli, 'menu_title', return_value='SLAI-tool · 用户：测试 · 工作空间：demo')
        proxy = patch('scripts.proxy_settings.status',return_value='SOCKS5：测试状态')
        proxy.start();self.addCleanup(proxy.stop)
        self.identity.start()
        self.addCleanup(self.identity.stop)

    async def settle(self, pilot, check):
        for _ in range(40):
            await pilot.pause(0.025)
            if check():
                return
        self.fail('UI did not reach expected state')

    async def test_large_list_navigation_search_and_refresh(self):
        fetch = Mock(return_value=[f'image-{i:05}' for i in range(10000)])
        app = SlaiApp()
        async with app.run_test(size=(80, 24)) as pilot:
            browser = Browser('镜像', LocalSource(fetch), lambda row: None)
            await app.push_screen(browser)
            await self.settle(pilot, lambda: browser.page is not None)
            self.assertEqual(browser.query_one(DataTable).row_count, 20)
            await pilot.press('right')
            await self.settle(pilot, lambda: not browser.loading)
            self.assertEqual(browser.index, 1)
            await pilot.press('/')
            browser.query_one(Input).value = 'image-09999'
            await pilot.press('enter')
            await self.settle(pilot, lambda: not browser.loading)
            self.assertEqual(browser.query_one(DataTable).row_count, 1)
            fetch.assert_called_once()
            await pilot.press('r')
            await self.settle(pilot, lambda: not browser.loading)
            self.assertEqual(fetch.call_count, 2)
            await pilot.press('0')
            self.assertEqual(len(app.screen_stack), 1)

    async def test_search_selects_full_image_tag_and_returns_to_form(self):
        from scripts import ccr
        app = SlaiApp()
        draft = CreateDraft('acp', fake_client(), {'name': 'ws'})
        namespace = dict(name='shared', state='ACTIVE', region='cn-sh-01')
        rows = [dict(name=f'shared/image-{i}', domain='registry.example', tags=['v1', 'cuda12']) for i in range(1000)]
        with patch.object(ccr, 'namespaces', return_value=[namespace]) as namespaces, \
             patch.object(ccr, 'repositories', return_value=rows) as fetch:
            async with app.run_test(size=(80, 24)) as pilot:
                form = Form(draft)
                await app.push_screen(form)
                await self.settle(pilot, lambda: not form.busy)
                form.query_one(DataTable).move_cursor(row=3)
                await pilot.press('enter')
                await self.settle(pilot, lambda: isinstance(app.screen, Picker))
                await pilot.press('enter')  # Search CCR is the first choice.
                await self.settle(pilot, lambda: isinstance(app.screen, Browser) and app.screen.page is not None)
                self.assertEqual(app.screen.query_one(DataTable).row_count, 20)
                await pilot.press('/')
                app.screen.query_one(Input).value = 'image-999:cuda12'
                await pilot.press('enter')
                await self.settle(pilot, lambda: not app.screen.loading)
                self.assertEqual(app.screen.page.total, 1)
                await pilot.press('enter')
                await self.settle(pilot, lambda: app.screen is form and not form.busy)
                self.assertEqual(draft.values['image'], 'registry.example/shared/image-999:cuda12')
                # Reopen the picker and cancel. Both caches and the draft survive.
                await pilot.press('enter')
                await self.settle(pilot, lambda: isinstance(app.screen, Picker))
                await pilot.press('enter')
                await self.settle(pilot, lambda: isinstance(app.screen, Browser) and app.screen.page is not None)
                await pilot.press('0')
                await self.settle(pilot, lambda: app.screen is form and not form.busy)
                self.assertEqual(draft.values['image'], 'registry.example/shared/image-999:cuda12')
        fetch.assert_called_once()
        namespaces.assert_called_once()
        draft.client.create.assert_not_called()

    async def test_copy_button_uses_system_clipboard_service(self):
        from scripts import clipboard
        from scripts.tui import Details
        value = "ssh -p 1234 -o 'ProxyCommand=python helper.py %h %p' root@192.0.2.1"
        app = SlaiApp()
        with patch.object(clipboard, 'copy_text') as copy:
            async with app.run_test(size=(80, 24)) as pilot:
                await app.push_screen(Details('SSH 连接命令', value, hint='使用 Add New SSH Host 添加命令'))
                await pilot.click('#copy')
                await self.settle(pilot, lambda: copy.called)
                copy.assert_called_once_with(value)

    async def test_back_during_slow_read_discards_late_result(self):
        gate = threading.Event()
        def fetch():
            gate.wait(2)
            return ['late']
        app = SlaiApp()
        async with app.run_test() as pilot:
            browser = Browser('slow', LocalSource(fetch), lambda row: None)
            await app.push_screen(browser)
            await pilot.press('0')
            gate.set()
            await pilot.pause(0.1)
            self.assertEqual(len(app.screen_stack), 1)
            self.assertIsNone(browser.page)

    async def test_form_keyboard_cancel_validation_and_zero_input(self):
        app = SlaiApp()
        draft = CreateDraft('acp', fake_client(), {'name': 'ws'})
        async with app.run_test(size=(80, 24)) as pilot:
            form = Form(draft)
            await app.push_screen(form)
            await self.settle(pilot, lambda: not form.busy)
            await pilot.press('enter')
            await self.settle(pilot, lambda: isinstance(app.screen, Edit))
            app.screen.query_one(Input).value = 'task-'
            await pilot.press('end', '0', 'enter')
            await self.settle(pilot, lambda: app.screen is form and not form.busy)
            self.assertEqual(draft.values['name'], 'task-0')
            await pilot.press('ctrl+s')
            await self.settle(pilot, lambda: not form.busy)
            self.assertIs(app.screen, form)
            self.assertIn('命令', str(form.query_one('#status', Static).render()))
            draft.values['command'] = 'python train.py'
            # Cancel an editor preserves the previous draft and never submits.
            await pilot.press('enter')
            await self.settle(pilot, lambda: isinstance(app.screen, Edit))
            await pilot.press('escape')
            await self.settle(pilot, lambda: app.screen is form and not form.busy)
            self.assertEqual(draft.values['name'], 'task-0')
            draft.client.create.assert_not_called()

    async def test_confirm_default_cancels_and_shows_review(self):
        app = SlaiApp()
        performed = Mock()
        def work():
            ui.output('目标：test-resource')
            if ui.confirm('确认删除', '删除'):
                performed()
        async with app.run_test() as pilot:
            app.open_operation('操作', work)
            await self.settle(pilot, lambda: isinstance(app.screen, Picker))
            self.assertIn('test-resource', app.screen.query_one(TextArea).text)
            await pilot.press('enter')
            await self.settle(pilot, lambda: isinstance(app.screen, Operation) and app.screen.done)
            performed.assert_not_called()

    async def test_service_list_returns_without_extra_result_page(self):
        app = SlaiApp()
        def service(name, args):
            return ui.browse('我的资源', lambda: ['example'], str, lambda row: None)
        with patch.object(cli, 'run_service', side_effect=service):
            async with app.run_test() as pilot:
                await pilot.press('4')
                await self.settle(pilot, lambda: isinstance(app.screen, Browser) and app.screen.page is not None)
                await pilot.press('0')
                await self.settle(pilot, lambda: len(app.screen_stack) == 1)
                self.assertEqual(len(app.screen_stack), 1)

    async def test_argument_error_finishes_instead_of_leaving_busy_screen(self):
        app = SlaiApp()
        def invalid():
            raise SystemExit(2)
        async with app.run_test() as pilot:
            app.open_operation('参数检查', invalid)
            await self.settle(pilot, lambda: isinstance(app.screen, Operation) and app.screen.done)
            self.assertIn('操作未完成', str(app.screen.query_one('#status', Static).render()))

    async def test_choice_without_default_selects_first_row_on_enter(self):
        app = SlaiApp()
        selected = []
        async with app.run_test() as pilot:
            app.open_operation('选择', lambda: selected.append(ui.choose('规格', ['first', 'second'])))
            await self.settle(pilot, lambda: isinstance(app.screen, Picker))
            await pilot.press('enter')
            await self.settle(pilot, lambda: isinstance(app.screen, Operation) and app.screen.done)
            self.assertEqual(selected, ['first'])

    async def test_shutdown_releases_worker_waiting_for_input(self):
        app = SlaiApp()
        async with app.run_test() as pilot:
            app.open_operation('配置', lambda: ui.ask('名称'))
            await self.settle(pilot, lambda: isinstance(app.screen, Edit))
        self.assertFalse(app.pending)

    async def test_password_input_is_masked_and_not_logged(self):
        app = SlaiApp()
        values = []
        async with app.run_test() as pilot:
            app.open_operation('配置', lambda: values.append(ui.secret('密钥')))
            await self.settle(pilot, lambda: isinstance(app.screen, Edit))
            field = app.screen.query_one(Input)
            self.assertTrue(field.password)
            field.value = 'example-not-real-secret'
            await pilot.press('enter')
            await self.settle(pilot, lambda: isinstance(app.screen, Operation) and app.screen.done)
            self.assertEqual(values, ['example-not-real-secret'])
            log = app.screen.query_one('#output')
            self.assertFalse(any('example-not-real-secret' in str(line) for line in log.lines))

    async def test_multiline_command_preserves_literal_text(self):
        app = SlaiApp()
        async with app.run_test() as pilot:
            results = []
            await app.push_screen(CommandEdit('set -eu\npython train.py'), results.append)
            app.screen.query_one(TextArea).text = 'set -eu\npython "my script.py" --steps 100'
            await pilot.press('ctrl+s')
            self.assertEqual(results, ['set -eu\npython "my script.py" --steps 100'])

class ListActionTests(unittest.IsolatedAsyncioTestCase):
    async def test_create_button_on_empty_list_and_refresh_after_return(self):
        from textual.widgets import Button
        source=LocalSource(Mock(return_value=[]));created=Mock()
        def create():
            created()
            ui.show_text('创建表单', 'form content')
        app=SlaiApp()
        with patch.object(cli,'menu_title',return_value='SLAI-tool'), patch('scripts.proxy_settings.status',return_value='SOCKS5：测试状态'):
            async with app.run_test(size=(80,24)) as pilot:
                browser=Browser('我的 ACP',source,lambda row:None,actions=(('create','创建 ACP',create),('create-last','按照上次配置',create)))
                await app.push_screen(browser)
                for _ in range(40):
                    await pilot.pause(.025)
                    if not browser.loading:break
                self.assertEqual(len(browser.page.rows),0)
                self.assertFalse(browser.query_one('#service-create',Button).disabled)
                await pilot.click('#service-create')
                for _ in range(40):
                    await pilot.pause(.025)
                    if created.called:break
                created.assert_called_once()
                self.assertIn('form content',app.screen.query_one(TextArea).text)
                await pilot.press('escape')
                await pilot.pause(.1)
                await pilot.press('0')
                for _ in range(40):
                    await pilot.pause(.025)
                    if app.screen is browser and not browser.loading:break
                self.assertIs(app.screen,browser)
                self.assertEqual(source.fetch.call_count,2)
