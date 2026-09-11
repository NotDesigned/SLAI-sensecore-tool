import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import cli, onboarding, rest, ui
from scripts.tui import SlaiApp, Operation
from textual.widgets import Static

UID = '11111111-1111-4111-8111-111111111111'


class AccountErrorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'config.toml'
        p = patch.object(cli, 'CONFIG', self.path)
        p.start()
        self.addCleanup(p.stop)
        self.config = {'account': {'access_key_id': 'fixture', 'access_key_secret': 'fixture'}}

    def test_bom_is_accepted_and_utf16_is_actionable(self):
        text = '[account]\naccess_key_id="fixture"\naccess_key_secret="fixture"\n'
        self.path.write_text(text, encoding='utf-8-sig')
        self.assertEqual(cli.load_config(), self.config)
        self.path.write_text(text, encoding='utf-16')
        with self.assertRaises(cli.ConfigError) as caught:
            cli.load_config()
        self.assertEqual(caught.exception.title, '配置文件编码错误')
        self.assertIn('UTF-8', str(caught.exception))

    def test_validation_and_save_failures_have_different_titles(self):
        with patch.object(ui, 'choose', return_value='验证当前账户'), patch.object(rest, 'get_json', side_effect=rest.RestError(403)), patch.object(cli, 'save_config_updates') as save:
            with self.assertRaises(cli.ConfigError) as caught:
                cli.configure_account(self.config)
        self.assertEqual(caught.exception.title, '账户验证失败')
        save.assert_not_called()
        with patch.object(ui, 'choose', return_value='验证当前账户'), patch.object(rest, 'get_json', return_value={'id': UID}), patch.object(cli, 'save_config_updates', side_effect=PermissionError('private details')):
            with self.assertRaises(cli.ConfigError) as caught:
                cli.configure_account(self.config)
        self.assertEqual(caught.exception.title, '账户配置保存失败')
        self.assertNotIn('private details', str(caught.exception))

    def test_workspace_failure_preserves_saved_account(self):
        with patch.object(ui, 'show_text'), patch.object(ui, 'ask', return_value='fixture'), patch.object(ui, 'secret', return_value='fixture'), patch.object(rest, 'get_json', return_value={'id': UID}), patch.object(cli, 'configure_workspace', side_effect=cli.ConfigError('网络不可达')):
            with self.assertRaises(cli.ConfigError) as caught:
                onboarding.start()
        self.assertEqual(caught.exception.title, '账户已保存，工作空间未完成')
        self.assertEqual(cli.load_config()['account'], self.config['account'])
        self.assertIn('网络不可达', str(caught.exception))

    def test_legacy_windows_output_is_utf8(self):
        raw = io.BytesIO()
        stream = io.TextIOWrapper(raw, encoding='cp1252')
        with patch.object(cli.sys, 'platform', 'win32'), patch.object(cli.sys, 'stdout', stream):
            ui.output('中文测试')
        stream.flush()
        self.assertEqual(raw.getvalue().decode('utf-8'), '中文测试' + os.linesep)


class AccountErrorUiTests(unittest.IsolatedAsyncioTestCase):
    async def test_operation_displays_stage_and_reason(self):
        def fail():
            raise cli.ConfigError('凭据已保存；工作空间网络不可达。', title='账户已保存，工作空间未完成')
        app = SlaiApp()
        async with app.run_test() as pilot:
            app.open_operation('配置账户', lambda: cli.guarded(fail))
            for _ in range(100):
                await pilot.pause(.03)
                if isinstance(app.screen, Operation) and app.screen.done:
                    break
            self.assertTrue(app.screen.done)
            self.assertIn('账户已保存，工作空间未完成', str(app.screen.query_one('#status', Static).render()))

    async def test_operation_worker_starts_after_mount(self):
        import threading
        started = threading.Event()
        mounted = []
        original_mount = Operation.on_mount
        def slow_mount(operation):
            original_mount(operation)
            # Let an incorrectly eager worker observe the pre-mount state.
            started.wait(.05)
        app = SlaiApp()
        async with app.run_test() as pilot:
            operation = Operation('mount probe', lambda: (mounted.append(operation.is_mounted), started.set()))
            with patch.object(Operation, 'on_mount', slow_mount):
                await app.push_screen(operation)
            for _ in range(100):
                await pilot.pause(.025)
                if operation.done:
                    break
            self.assertTrue(operation.done)
            self.assertEqual(mounted, [True])
