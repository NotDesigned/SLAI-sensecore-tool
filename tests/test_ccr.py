import contextlib
import io
import unittest
from unittest.mock import patch

from scripts import ccr, cli


class CcrTests(unittest.TestCase):
    def setUp(self):
        self.uid = '11111111-1111-4111-8111-111111111111'
        self.ns = {'region': 'cn-sh-01', 'subscription_name': 'sub', 'resource_group_name': 'default',
                   'zone': 'cn-sh-01z', 'name': 'shared'}

    def test_accessible_repositories_and_camel_case_pagination(self):
        replies = [{'repositories': [{'name': 'shared/a'}], 'totalSize': 2, 'nextPageToken': 'next'},
                   {'repositories': [{'name': 'shared/b'}], 'totalSize': 2}]
        with patch.object(ccr, 'get_json', side_effect=replies) as get:
            self.assertEqual([r['name'] for r in ccr.repositories({}, self.ns)], ['shared/a', 'shared/b'])
        self.assertIn('pageToken=next', get.call_args.args[1])
        self.assertIn('pageSize=100', get.call_args.args[1])

    def test_server_returning_everything_without_pagination(self):
        rows = [{'name': f'shared/{i}', 'id': i} for i in range(150)]
        with patch.object(ccr, 'get_json', return_value={'repositories': rows, 'totalSize': 150, 'nextPageToken': ''}) as get:
            self.assertEqual(len(ccr.repositories({}, self.ns)), 150)
        self.assertEqual(get.call_count, 1)

    def test_incomplete_and_repeated_results_fail(self):
        for replies in ([{'repositories': [], 'totalSize': 1}],
                        [{'repositories': [{'name': 'a'}], 'nextPageToken': '2'},
                         {'repositories': [{'name': 'a'}], 'nextPageToken': '3'}]):
            with patch.object(ccr, 'get_json', side_effect=replies), self.assertRaises(cli.ConfigError):
                ccr.repositories({}, self.ns)

    def test_namespace_discovery_uses_rest(self):
        rows = [{'name': 'shared', 'type': 'devtools.ccr.v1.namespace'},
                {'name': 'deleted', 'type': 'devtools.ccr.v1.namespace', 'deleted': True}]
        with patch.object(ccr, 'get_json', return_value={'resources': rows, 'total_size': 2}) as get:
            self.assertEqual(ccr.namespaces({}), [rows[0]])
        self.assertIn('/rmh/v1/resources?', get.call_args.args[1])

    def test_full_image_reference_does_not_duplicate_namespace(self):
        row = {'name': 'shared/app', 'domain': 'registry.example', 'tags': ['v1', 'v2']}
        self.assertEqual(ccr.image_references(row), ['registry.example/shared/app:v1', 'registry.example/shared/app:v2'])

    def test_menu_upload_list_and_return(self):
        with patch('builtins.input', side_effect=['bad', '1', '2', '0']), patch.object(cli, 'load_config', return_value={}):
            with patch.object(ccr, 'push_image') as upload, patch.object(ccr, 'list_images') as listing:
                with contextlib.redirect_stdout(io.StringIO()):
                    ccr.menu()
        upload.assert_called_once_with({})
        listing.assert_called_once_with({}, None)

    def test_component_failure_does_not_skip_later_components(self):
        with patch.object(cli, 'run', side_effect=[cli.ConfigError('failed'), None]) as run:
            with contextlib.redirect_stdout(io.StringIO()), self.assertRaisesRegex(cli.ConfigError, 'eip'):
                cli.install_components({'sco': {'region': 'cnsh01', 'profile': 'default'}}, {}, '/sco')
        self.assertEqual([call.args[0][-1] for call in run.call_args_list], ['eip', 'ccr'])
