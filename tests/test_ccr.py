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

    def test_searchable_image_tags_cache_and_refresh(self):
        catalog = ccr.ImageCatalog({})
        rows = [dict(name='shared/app', domain='registry.example', tags=['v1', 'cuda12']),
                dict(name='shared/untagged', domain='registry.example', tags=[])]
        with patch.object(ccr, 'repositories', return_value=rows) as fetch:
            source = catalog.source(self.ns)
            self.assertEqual(source.page(0, 20, 'cuda12').rows, ['registry.example/shared/app:cuda12'])
            self.assertIs(source, catalog.source(self.ns))
            self.assertEqual(source.page(0, 20).total, 2)
            fetch.assert_called_once()
            source.page(0, 20, refresh=True)
            self.assertEqual(fetch.call_count, 2)
            other = catalog.source({**self.ns, 'subscription_name': 'other'})
            self.assertIsNot(other, source)

    def test_image_search_scopes_accessible_namespaces_and_preserves_selection(self):
        from scripts import ui
        catalog = ccr.ImageCatalog({})
        namespace = dict(self.ns, state='ACTIVE')
        with patch.object(ccr, 'namespaces', return_value=[namespace, dict(self.ns, state='SUSPENDED')]) as fetch, \
             patch.object(ui, 'select_resource', return_value='registry.example/shared/app:v1') as select:
            self.assertEqual(catalog.select(), 'registry.example/shared/app:v1')
            catalog.select()
        fetch.assert_called_once()
        self.assertIs(select.call_args_list[0].args[1], select.call_args_list[1].args[1])

    def test_default_image_does_not_scan_local_docker_or_ccr(self):
        from scripts import cloud, ui
        with patch.object(ui, 'choose', return_value=cloud.DEFAULT_IMAGE), \
             patch.object(cloud, 'local_images') as docker, patch.object(ccr, 'namespaces') as namespaces:
            self.assertEqual(cloud.select_image('', {}), (cloud.DEFAULT_IMAGE, None))
        docker.assert_not_called()
        namespaces.assert_not_called()

    def test_upload_namespace_filters_region_and_preserves_current_choice(self):
        rows = [dict(name='current', state='ACTIVE', region='cn-sh-01'),
                dict(name='other', state='ACTIVE', region='cn-sh-01'),
                dict(name='different-region', state='ACTIVE', region='cn-sh-02'),
                dict(name='inactive', state='SUSPENDED', region='cn-sh-01')]
        with patch.object(ccr, 'namespaces', return_value=rows), patch.object(ccr, 'choose', return_value=rows[1]) as choose:
            value = ccr.select_upload_namespace({}, 'registry.cn-sh-01.sensecore.cn', 'current')
        self.assertEqual(value, 'other')
        self.assertEqual(choose.call_args.args[1], rows[:2])
        self.assertEqual(choose.call_args.kwargs['default'], rows[0])

    def test_upload_namespace_query_failure_does_not_fall_back_to_typed_name(self):
        from scripts import docker_registry, ui
        with patch.object(ccr, 'namespaces', side_effect=cli.ConfigError('offline')), \
             patch.object(docker_registry, 'ask') as ask, patch.object(docker_registry, 'save_config_updates') as save:
            with self.assertRaises(cli.ConfigError):
                docker_registry.complete_config({'docker': {'registry': 'registry.cn-sh-01.sensecore.cn', 'namespace': 'stale'}})
        ask.assert_not_called()
        save.assert_not_called()
        with patch.object(ccr, 'namespaces', return_value=[dict(name='one', region='cn-sh-01', state='ACTIVE')]), \
             patch.object(ccr, 'choose', side_effect=ui.Cancelled), patch.object(docker_registry, 'save_config_updates') as save:
            with self.assertRaises(ui.Cancelled):
                docker_registry.complete_config({'docker': {'registry': 'registry.cn-sh-01.sensecore.cn'}})
        save.assert_not_called()

    def test_service_defaults_to_list(self):
        with patch.object(cli, 'load_config', return_value={}), patch.object(ccr, 'list_images') as listing:
            ccr.main([])
        listing.assert_called_once_with({}, None, plain=False)
