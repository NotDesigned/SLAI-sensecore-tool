import contextlib
import io
import unittest
from unittest.mock import Mock, patch

from scripts import cli, cci_service, service_menu


class CciServiceTests(unittest.TestCase):
    def setUp(self):
        self.uid = '11111111-1111-4111-8111-111111111111'
        self.ws = {'name': 'ws', 'region': 'cn-sh-01', 'subscription_name': 'sub',
                   'resource_group_name': 'group', 'zone': 'cn-sh-01z'}
        self.app = {'name': 'mine', 'ownership': {'user_id': self.uid}}

    def test_list_filters_identity_across_pages(self):
        data = [{'id': self.uid}, {'apps': [self.app], 'total_size': 2, 'next_page_token': '2'},
                {'apps': [{'name': 'other', 'ownership': {'user_id': 'other'}}], 'total_size': 2}]
        with patch.object(cci_service, 'get_json', side_effect=data) as get:
            self.assertEqual(cci_service.my_apps({}, self.ws), [self.app])
        self.assertIn('page_token=2', get.call_args.args[1])

    def test_unknown_identity_does_not_show_all_apps(self):
        with patch.object(cci_service, 'get_json', return_value={}) as get:
            with self.assertRaises(cli.ConfigError):
                cci_service.my_apps({}, self.ws)
        self.assertEqual(get.call_count, 1)

    def test_delete_rechecks_ownership_and_verifies_disappearance(self):
        client = Mock()
        client.command.side_effect = lambda args: ['/sco', *args]
        with patch.object(cci_service, 'my_apps', side_effect=[[self.app], []]):
            with patch.object(cci_service.cci, 'Client', return_value=client), patch.object(cli, 'run') as run:
                cci_service.delete_app({}, self.ws, 'mine')
        client.scope.assert_called_once_with(self.ws)
        self.assertEqual(run.call_args.args[0], ['/sco', 'cci', 'apps', 'delete', 'mine', '--workspace-name', 'ws'])
        with patch.object(cci_service, 'my_apps', return_value=[]), patch.object(cli, 'run') as run:
            with self.assertRaises(cli.ConfigError):
                cci_service.delete_app({}, self.ws, 'other')
        run.assert_not_called()

    def test_submenu_returns_after_operations_and_errors(self):
        execute = Mock(side_effect=[None, cli.ConfigError('failure'), None])
        with patch('builtins.input', side_effect=['bad', '1', '2', '3', '0']), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(service_menu.menu('service', execute), 0)
        self.assertEqual([x.args[0] for x in execute.call_args_list], [['create'], ['list'], ['delete']])

    def test_declining_delete_does_not_mutate(self):
        client = Mock()
        client.resources.return_value = [self.ws]
        with patch.object(cli, 'load_config', return_value={}), patch.object(cci_service.cci, 'Client', return_value=client):
            with patch.object(cci_service, 'my_apps', return_value=[self.app]), patch.object(cci_service, 'delete_app') as delete:
                with patch('builtins.input', return_value=''), contextlib.redirect_stdout(io.StringIO()):
                    cci_service.main(['delete', '--workspace', 'ws', '--name', 'mine'])
        delete.assert_not_called()

    def test_list_actions_refresh_and_return(self):
        with patch.object(cci_service, 'my_apps', return_value=[self.app]) as listing, \
             patch.object(cci_service, 'stop_app') as stop, \
             patch.object(cci_service, 'delete_app') as delete, \
             patch.object(cci_service, 'copy_app') as duplicate, \
             patch('builtins.input', side_effect=['1', '2', '2', '1', '3', '1', '4', '2', '0']), \
             contextlib.redirect_stdout(io.StringIO()):
            cci_service.list_page({}, self.ws)
        stop.assert_called_once_with({}, self.ws, 'mine', expected=self.app)
        delete.assert_called_once_with({}, self.ws, 'mine', expected=self.app)
        duplicate.assert_called_once_with({}, self.ws, 'mine')
        self.assertEqual(listing.call_count, 4)

    def test_stop_checks_completion_and_already_stopped(self):
        with patch.object(cci_service, 'my_apps', side_effect=[[self.app], [{**self.app, 'state': 'SUSPENDED'}]]), \
             patch.object(cci_service.cci, 'Client') as client, patch.object(cli, 'run') as run, \
             contextlib.redirect_stdout(io.StringIO()):
            cci_service.stop_app({}, self.ws, 'mine')
        self.assertEqual(client.return_value.command.call_args.args[0],
                         ['cci', 'apps', 'stop', 'mine', '--workspace-name', 'ws'])
        run.assert_called_once()
        with patch.object(cci_service, 'my_apps', return_value=[{**self.app, 'state': 'SUSPENDED'}]), \
             patch.object(cli, 'run') as run, contextlib.redirect_stdout(io.StringIO()):
            cci_service.stop_app({}, self.ws, 'mine')
        run.assert_not_called()

    def test_copy_preserves_template_without_runtime_fields(self):
        source = {**self.app, 'uid': 'old', 'state': 'SUSPENDED', 'replicas': 1,
                  'resource_pool': {'name': 'pool'},
                  'template': {'containers': [{'image_path': 'image', 'command': ['sleep', 'infinity']}]}}
        result = cci_service.copy_document(source)
        self.assertNotIn('uid', result)
        self.assertNotIn('ownership', result)
        self.assertNotIn('state', result)
        self.assertEqual(result['template'], source['template'])
        result['template']['containers'][0]['command'].append('changed')
        self.assertEqual(source['template']['containers'][0]['command'], ['sleep', 'infinity'])

    def test_copy_submission_and_save_only(self):
        import tempfile
        from pathlib import Path
        source = {**self.app, 'uid': 'old', 'replicas': 1,
                  'resource_pool': {'name': 'pool'}, 'template': {'containers': [{'image_path': 'image'}]}}
        for submit in (False, True):
            with self.subTest(submit=submit), tempfile.TemporaryDirectory() as directory, \
                 patch.object(cli, 'ROOT', Path(directory)), \
                 patch.object(cci_service, 'owned_app', return_value=source), \
                 patch.object(cci_service, 'get_json', side_effect=[source, {'ports': [{'port': 22, 'target_port': 22}]}, cci_service.RestError(404)]), \
                 patch.object(cci_service.cci, 'Client') as client, patch.object(cli, 'run') as run, \
                 patch('builtins.input', side_effect=['new-copy', '2' if submit else '']), \
                 contextlib.redirect_stdout(io.StringIO()):
                cci_service.copy_app({}, self.ws, 'mine')
                files = list(Path(directory).rglob('*.yaml'))
                self.assertEqual(len(files), 1)
                self.assertEqual(files[0].stat().st_mode & 0o777, 0o600)
                if submit:
                    run.assert_called_once()
                    args = client.return_value.command.call_args.args[0]
                    self.assertEqual(args[:4], ['cci', 'apps', 'create', 'new-copy'])
                    self.assertEqual(args[-2:], ['--ports', '22'])
                else:
                    run.assert_not_called()

    def test_stale_selection_cannot_stop_or_delete_replacement(self):
        source = {**self.app, 'uid': 'original'}
        replacement = {**self.app, 'uid': 'replacement'}
        for operation in (cci_service.stop_app, cci_service.delete_app):
            with patch.object(cci_service, 'my_apps', return_value=[replacement]), patch.object(cli, 'run') as run:
                with self.assertRaises(cli.ConfigError):
                    operation({}, self.ws, 'mine', expected=source)
            run.assert_not_called()
