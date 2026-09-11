import contextlib
import io
import unittest
from unittest.mock import Mock, patch



from scripts import cloud, ui, cli, cci_service, cci_api, rest

class CciServiceTests(unittest.TestCase):
    def setUp(self):
        self.uid = '11111111-1111-4111-8111-111111111111'
        self.ws = {'name': 'ws', 'region': 'cn-sh-01', 'subscription_name': 'sub',
                   'resource_group_name': 'group', 'zone': 'cn-sh-01z'}
        self.app = {'name': 'mine', 'uid': 'app-uid', 'ownership': {'user_id': self.uid}}

    def test_list_filters_identity_across_pages(self):
        data = [{'id': self.uid}, {'apps': [self.app], 'total_size': 2, 'next_page_token': '2'},
                {'apps': [{'name': 'other', 'ownership': {'user_id': 'other'}}], 'total_size': 2}]
        with patch.object(cci_service, 'get_json', side_effect=data) as get:
            self.assertEqual(cci_service.my_apps({}, self.ws), [self.app])
        self.assertIn('page_token=2', get.call_args.args[1])
        self.assertIn('/appsOwn?', get.call_args.args[1])

    def test_invalid_rows_and_overlapping_pages(self):
        with patch.object(cci_service, 'get_json', side_effect=[{'id': self.uid}, {'apps': [{}]}]):
            with self.assertRaisesRegex(cli.ConfigError, '缺少名称'):
                cci_service.my_apps({}, self.ws)
        other = {**self.app, 'name': 'second', 'uid': 'second-uid'}
        pages = [{'id': self.uid}, {'apps': [self.app], 'next_page_token': '2'},
                 {'apps': [self.app, other]}]
        with patch.object(cci_service, 'get_json', side_effect=pages):
            self.assertEqual(cci_service.my_apps({}, self.ws), [self.app, other])
        pages[-1] = {'apps': [self.app], 'next_page_token': '3'}
        with patch.object(cci_service, 'get_json', side_effect=pages):
            with self.assertRaisesRegex(cli.ConfigError, '重复返回整页'):
                cci_service.my_apps({}, self.ws)

    def test_unknown_identity_does_not_show_all_apps(self):
        with patch.object(cci_service, 'get_json', return_value={}) as get:
            with self.assertRaises(cli.ConfigError):
                cci_service.my_apps({}, self.ws)
        self.assertEqual(get.call_count, 1)

    def test_delete_rechecks_ownership_and_verifies_disappearance(self):
        with patch.object(cci_api,'owned',return_value=self.app), patch.object(cci_api,'optional',return_value=None), patch.object(rest,'request_json') as write:
            cci_service.delete_app({},self.ws,'mine')
        write.assert_called_once()
        self.assertEqual(write.call_args.kwargs['method'],'DELETE')
        with patch.object(cci_api,'owned',side_effect=cli.ConfigError('不属于当前用户')), patch.object(rest,'request_json') as write:
            with self.assertRaises(cli.ConfigError):cci_service.delete_app({},self.ws,'other')
        write.assert_not_called()

    def test_submenu_returns_after_operations_and_errors(self):
        execute = Mock(side_effect=[None, cli.ConfigError('failure'), None])
        with patch('builtins.input', side_effect=['bad', '1', '2', '3', '0']), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(ui.menu('service', execute), 0)
        self.assertEqual([x.args[0] for x in execute.call_args_list], [['create'], ['list'], ['delete']])

    def test_declining_delete_does_not_mutate(self):
        client = Mock()
        client.resources.return_value = [self.ws]
        with patch.object(cli, 'load_config', return_value={}), patch.object(cloud, 'Client', return_value=client):
            with patch.object(cci_service, 'my_apps', return_value=[self.app]), patch.object(cci_service, 'delete_app') as delete:
                with patch('builtins.input', return_value=''), contextlib.redirect_stdout(io.StringIO()):
                    cci_service.main(['delete', '--workspace', 'ws', '--name', 'mine'])
        delete.assert_not_called()

    def test_list_actions_refresh_and_return(self):
        with patch.object(cci_service, 'my_apps', return_value=[self.app]) as listing, \
             patch.object(cci_service, 'stop_app') as stop, \
             patch.object(cci_service, 'delete_app') as delete, \
             patch.object(cci_service, 'copy_app') as duplicate, \
             patch('builtins.input', side_effect=['1', '1', '1', '1', '2', '1', '3', '1', '0']), \
             contextlib.redirect_stdout(io.StringIO()):
            cci_service.list_page({}, self.ws)
        stop.assert_called_once_with({}, self.ws, 'mine', expected=self.app)
        delete.assert_called_once_with({}, self.ws, 'mine', expected=self.app)
        duplicate.assert_called_once_with({}, self.ws, 'mine')
        self.assertEqual(listing.call_count, 4)

    def test_stop_checks_completion_and_already_stopped(self):
        stopped=dict(self.app,state='SUSPENDED')
        with patch.object(cci_api,'owned',return_value=self.app), patch.object(cci_api,'optional',return_value=stopped), patch.object(rest,'request_json') as write:
            cci_service.stop_app({},self.ws,'mine')
        self.assertTrue(write.call_args.args[1].endswith('/apps/mine:stop'))
        with patch.object(cci_api,'owned',return_value=stopped), patch.object(rest,'request_json') as write:
            cci_service.stop_app({},self.ws,'mine')
        write.assert_not_called()

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
                 patch.object(cci_api, 'create') as create, \
                 patch('builtins.input', side_effect=['new-copy', '' if submit else '2']), \
                 contextlib.redirect_stdout(io.StringIO()):
                cci_service.copy_app({}, self.ws, 'mine')
                files = list(Path(directory).rglob('*.yaml'))
                self.assertEqual(len(files), 1)
                import yaml
                self.assertEqual(yaml.safe_load(files[0].read_text())['display_name'], 'new-copy')
                self.assertEqual(files[0].stat().st_mode & 0o777, 0o600)
                if submit:
                    create.assert_called_once()
                    plan = create.call_args.args[1]
                    self.assertEqual(plan['name'], 'new-copy')
                    self.assertEqual(plan['ports'], '22')
                else:
                    create.assert_not_called()

    def test_stale_selection_cannot_stop_or_delete_replacement(self):
        for operation in (cci_service.stop_app,cci_service.delete_app):
            with patch.object(cci_api,'owned',return_value=dict(self.app,uid='new')), patch.object(rest,'request_json') as write:
                with self.assertRaises(cli.ConfigError):operation({},self.ws,'mine',expected=self.app)
            write.assert_not_called()

    def test_stop_does_not_accept_a_replacement_resource(self):
        with patch.object(cci_api,'owned',return_value=self.app), patch.object(cci_api,'optional',return_value=dict(self.app,uid='replacement',state='SUSPENDED')), patch.object(rest,'request_json'):
            with self.assertRaisesRegex(cli.ConfigError,'身份已变化'):cci_service.stop_app({},self.ws,'mine',self.app)

    def test_missing_uid_is_not_accepted(self):
        with self.assertRaisesRegex(cli.ConfigError, 'UID'):
            cci_service.check_selected({'name':'missing'}, None)
