"""Regression checks for shared routing and reviewable, concise creation flows."""
import contextlib
import io
import json
import shlex
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


from scripts import ui, acp, cci, cci_service, cli

class UsabilityTests(unittest.TestCase):
    def test_create_workspace_override_is_forwarded(self):
        with patch.object(cli, 'load_config', return_value={}), patch.object(cci, 'create') as create:
            cci_service.main(['create', '--workspace', 'other'])
        create.assert_called_once_with({}, workspace_name='other')
        with patch('scripts.workspace.select', side_effect=ui.Cancelled) as select:
            with self.assertRaises(ui.Cancelled):
                cci.prepare(Mock(), {}, workspace_name='other')
        self.assertEqual(select.call_args.kwargs, {'explicit': 'other'})

    def test_service_failure_and_cancel_return_to_main_menu(self):
        with patch.object(cli, 'menu_title', return_value='SLAI-tool'), \
             patch.object(acp, 'main', side_effect=[cli.ConfigError('网络失败'), ui.Cancelled]), \
             patch('builtins.input', side_effect=['6', '6', '0']), \
             contextlib.redirect_stdout(io.StringIO()) as output, \
             contextlib.redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(cli.menu(), 0)
        self.assertIn('网络失败', errors.getvalue())
        self.assertIn('已取消操作', output.getvalue())

    def test_entrypoint_text_preserves_literal_arguments(self):
        value = '/entrypoint.sh python "my script.py" "$(literal)"'
        with patch.object(ui, 'choose', return_value='使用镜像启动逻辑'), \
             patch.object(ui, 'ask', return_value=value), contextlib.redirect_stdout(io.StringIO()):
            result = acp.startup({})
        self.assertEqual(shlex.split(result), ['exec', '/entrypoint.sh', 'python', 'my script.py', '$(literal)'])

    def test_cci_summary_does_not_dump_bootstrap(self):
        document = {'replicas': 2, 'resource_pool': {'name': 'pool'}, 'template': {
            'resource_spec': {'name': 'gpu'}, 'containers': [{'image_path': 'registry/image:tag',
            'command': ['sh', '-c', 'VERY_LONG_BOOTSTRAP'], 'resource_request': {'cpu': '2'},
            'volume_mounts': [{'id': 'afs', 'subdir': '/user', 'mount_path': '/data'}]}]}}
        with contextlib.redirect_stdout(io.StringIO()) as output:
            cci.preview(document)
        self.assertIn('registry/image:tag', output.getvalue())
        self.assertIn('/user → /data', output.getvalue())
        self.assertNotIn('VERY_LONG_BOOTSTRAP', output.getvalue())
        self.assertEqual(document['template']['containers'][0]['command'][-1], 'VERY_LONG_BOOTSTRAP')

    def test_copy_plan_keeps_source_and_default_does_not_submit(self):
        source = {'name': 'source', 'uid': 'uid', 'roles': [], 'mount': [{'id': 'volume'}],
                  'resource_pool': {'name': 'pool'}, 'scheduling': {'quota_type': 'RESERVED'}}
        client = Mock()
        with tempfile.TemporaryDirectory() as directory, patch.object(cli, 'ROOT', Path(directory)), \
             patch.object(ui, 'choose', return_value='仅保存配置'), contextlib.redirect_stdout(io.StringIO()):
            acp.confirm_submit(client, 'ws', 'copy', ['acp', 'jobs', 'copy'], source=source)
            plan = json.loads(next((Path(directory) / '.cache/acp').glob('*.json')).read_text())
        self.assertEqual(plan['source']['mount'], source['mount'])
        client.submit.assert_not_called()
        client.jobs.assert_not_called()
