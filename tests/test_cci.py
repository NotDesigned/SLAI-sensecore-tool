import contextlib
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

import yaml



from scripts import network, ui, cloud, cci, cli

def spec_response(key='nvidia.com/mig-3g.40gb'):
    return {'resource_specs': [{'name': name, 'cpu': {'vcpu_allocatable': cpu, 'type': 'Intel Xeon 8468'},
              'memory': {'allocatable': memory}, 'device': {'number': count, 'resource_key': key},
              'zones': ['cn-sh-01e']}
             for name, cpu, memory, count in [('cpu-small', 2, 4, 0), ('gpu-large-wrapped', 8, 128, 1)]]}


class CciTests(unittest.TestCase):
    def test_mount_defaults_use_current_username_and_matching_afs(self):
        client = Mock()
        client.current_username.return_value = 'L202599999'
        client.resources.return_value = [
            {'name': 'other', 'id': 'other', 'zone': 'cn-sh-01e'},
            {'name': 'afs-share-01e', 'id': 'chosen', 'zone': 'cn-sh-01e'},
            {'name': 'afs-share-01g', 'id': 'wrong-zone', 'zone': 'cn-sh-01g'}]
        with patch('builtins.input', side_effect=['', '', '', '', '']):
            mounts = cloud.select_mounts(client, 'cn-sh-01e')
        self.assertEqual(mounts[0]['id'], 'chosen')
        self.assertEqual(mounts[0]['subdir'], '/L202599999')
        self.assertEqual(mounts[0]['mount_path'], '/data')

    def test_only_storage_in_selected_zone_is_automatic(self):
        client = Mock()
        client.current_username.return_value = 'my-user'
        client.resources.return_value = [
            {'name': 'only-volume', 'id': 'chosen', 'zone': 'cn-sh-01e'},
            {'name': 'other-zone', 'id': 'other', 'zone': 'cn-sh-01g'}]
        with patch('builtins.input', side_effect=['', '', '', '']) as prompt:
            mounts = cloud.select_mounts(client, 'cn-sh-01e')
        self.assertEqual(mounts[0]['id'], 'chosen')
        self.assertEqual(mounts[0]['mount_path'], '/data')
        self.assertEqual(mounts[0]['subdir'], '/my-user')
        self.assertEqual(prompt.call_count, 4)

    def test_no_mount_skips_identity_and_storage_requests(self):
        client = Mock()
        with patch('builtins.input', return_value='1'):
            self.assertEqual(cloud.select_mounts(client, 'cn-sh-01e'), [])
        client.current_username.assert_not_called()
        client.resources.assert_not_called()

    def test_invalid_cloud_username_is_config_error(self):
        client = object.__new__(cloud.Client)
        client.config = {}
        client._identity = None
        with patch('scripts.rest.get_json', return_value={'username': '../invalid'}):
            with self.assertRaises(cli.ConfigError):
                client.current_username()

    def setUp(self):
        network = patch('scripts.cci_network.plan_dnat', return_value=None)
        network.start()
        self.addCleanup(network.stop)
        self.output = contextlib.redirect_stdout(io.StringIO())
        self.output.__enter__()
        self.addCleanup(self.output.__exit__, None, None, None)

    def client(self):
        client = cloud.Client.__new__(cloud.Client)
        client.config = {}
        client._catalog, client._clusters = {}, {}
        client._workspace, client._identity = None, None
        return client

    def test_choose_retries_and_returns_original_object(self):
        items = [{'name': 'a'}, {'name': 'b'}]
        with patch('builtins.input', side_effect=['3', '-1', 'x', '2']):
            self.assertIs(ui.choose('资源', items), items[1])

    def test_cancel_label_is_generic_and_not_duplicated(self):
        with patch('builtins.input', return_value='1'), contextlib.redirect_stdout(io.StringIO()) as output:
            ui.choose('确认删除', ['取消', '删除'], default='取消')
        self.assertNotIn('q.', output.getvalue())
        self.assertNotIn('取消创建', output.getvalue())
        with patch('builtins.input', return_value='1'), contextlib.redirect_stdout(io.StringIO()) as output:
            ui.choose('资源', ['a'])
        self.assertIn('0. 返回', output.getvalue())

    def test_zero_returns_from_resource_and_action_menus(self):
        with patch('builtins.input', return_value='0'), self.assertRaises(ui.Cancelled):
            ui.choose('资源', ['a'])
        for label in ('取消', '返回列表'):
            with patch('builtins.input', return_value='0'):
                self.assertEqual(ui.choose('操作', [label, '删除'], default=label), label)
            with patch('builtins.input', return_value='1'):
                self.assertEqual(ui.choose('操作', [label, '删除'], default=label), '删除')

    def test_empty_and_cancel_and_default(self):
        with self.assertRaises(cli.ConfigError):
            ui.choose('资源', [])
        with patch('builtins.input', return_value='q'), self.assertRaises(ui.Cancelled):
            ui.choose('资源', ['a'])
        with patch('builtins.input', return_value=''):
            self.assertEqual(ui.choose('资源', ['a', 'b'], default='b'), 'b')

    def test_json_specs_preserve_ids_and_quantities(self):
        rows = cloud.decode_specs(spec_response(), 'cn-sh-01e')
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]['CPU'], 'Intel Xeon 8468')
        self.assertEqual(rows[1]['WORKER SPEC'], 'gpu-large-wrapped')
        with self.assertRaises(cli.ConfigError):
            cloud.decode_specs('unexpected output', 'cn-sh-01e')

    def test_resource_key_comes_directly_from_json_without_debug(self):
        for key in ['nvidia.com/gpu', 'nvidia.com/mig-3g.40gb', 'amd.com/gpu']:
            with contextlib.redirect_stdout(io.StringIO()) as output:
                rows = cloud.decode_specs(spec_response(key), 'cn-sh-01e')
            self.assertEqual(rows[1]['RESOURCE KEY'], key)
            self.assertEqual(output.getvalue(), '')

    def test_missing_invalid_or_mismatched_resource_details_fail_closed(self):
        for data in [{}, spec_response(''), spec_response('bad key')]:
            with self.assertRaises(cli.ConfigError):
                cloud.decode_specs(data, 'cn-sh-01e')
        with self.assertRaises(cli.ConfigError):
            cloud.decode_specs(spec_response(), 'cn-sh-01g')
        data = spec_response()
        data['resource_specs'][0]['cpu']['vcpu_allocatable'] = True
        with self.assertRaises(cli.ConfigError):
            cloud.decode_specs(data, 'cn-sh-01e')


    def test_rest_catalog_pagination_and_type_filter(self):
        client = self.client()
        first = [{'name': str(i), 'type': 'test', 'id': str(i)} for i in range(100)]
        second = [{'name': 'last', 'type': 'test', 'id': 'last'}]
        with patch('scripts.rest.get_json', side_effect=[{'resources': first, 'next_page_token': '2'}, {'resources': second}]) as get:
            self.assertEqual(len(client.resources('test')), 101)
            self.assertEqual(len(client.resources('test')), 101)
        self.assertEqual(get.call_count, 2)
        self.assertIn('page_token=2', get.call_args.args[1])
        client._catalog.clear()
        with patch('scripts.rest.get_json', return_value={'resources': first, 'next_page_token': '2'}):
            with self.assertRaises(cli.ConfigError):
                client.resources('test')
        with patch('scripts.rest.get_json', return_value={}), self.assertRaises(cli.ConfigError):
            client.resources('test')

    def test_cluster_uses_workspace_binding_instead_of_srm_snapshot(self):
        client = self.client()
        workspace = dict(name='ws', region='cn-sh-01', subscription_name='sub', resource_group_name='default', zone='cn-sh-01z')
        binding = dict(name='pool', uid='uid', state='ACTIVE', vpc_id='vpc',
                       id='/subscriptions/sub/resourceGroups/default/zones/cn-sh-01e/aec2s/pool')
        with patch('scripts.rest.get_json', return_value={'aec2s': [binding], 'total_size': 1}) as get:
            pool = client.clusters(workspace)[0]
            self.assertEqual(pool['zone'], 'cn-sh-01e')
            self.assertEqual(pool['properties']['vpc_id'], 'vpc')
            client.clusters(workspace)
        get.assert_called_once()



    def test_local_images_includes_local_tags_excludes_dangling(self):
        images = [{'Repository': repo, 'Tag': tag} for repo, tag in
                  [('local', 'v1'), ('registry.test/app', 'v1'), ('registry.test/app', '<none>')]]
        with patch.object(cloud.shutil, 'which', return_value='/docker'):
            with patch.object(subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, '\n'.join(map(json.dumps, images)))):
                self.assertEqual(cloud.local_images(), ['local:v1', 'registry.test/app:v1'])

    def test_save_only_and_cancel_never_submit(self):
        from scripts import cci_api
        ws=dict(name='ws',region='cn-sh-01',subscription_name='sub',resource_group_name='group',zone='cn-sh-01z')
        client=Mock();client.config={'cci':{'ssh_enabled':False}};client.workspace_record.return_value=ws
        def complete(draft):
            draft.snapshot=Mock(return_value={'image':'example'})
            return 'ws','app','',{'replicas':1},None
        with tempfile.TemporaryDirectory() as directory, patch.object(cli,'ROOT',Path(directory)), patch.object(cloud,'Client',return_value=client), patch.object(cci_api,'create') as create, patch('scripts.workspace.select',return_value=ws), patch.object(cli,'save_config_updates') as save:
            with patch.object(ui,'creation_form',side_effect=complete), patch.object(ui,'choose',return_value='仅保存配置'):
                cci.create({'cci':{}})
            create.assert_not_called();save.assert_called_once()
            path=next((Path(directory)/'.cache/cci').glob('*.yaml'))
            self.assertEqual(yaml.safe_load(path.read_text()),{'replicas':1})
            with patch.object(ui,'creation_form',side_effect=ui.Cancelled):cci.create({})
            create.assert_not_called()

    def test_submit_uses_exact_saved_document_and_ports(self):
        from scripts import cci_api
        ws=dict(name='ws',region='cn-sh-01',subscription_name='sub',resource_group_name='group',zone='cn-sh-01z')
        client=Mock();client.config={'cci':{'ssh_enabled':False}};client.workspace_record.return_value=ws
        def complete(draft):
            draft.snapshot=Mock(return_value={'image':'example'})
            return 'ws','app','8080',{'replicas':1},None
        with tempfile.TemporaryDirectory() as directory, patch.object(cli,'ROOT',Path(directory)), patch.object(cloud,'Client',return_value=client), patch.object(cci_api,'create') as create, patch('scripts.workspace.select',return_value=ws), patch.object(cli,'save_config_updates'):
            with patch.object(ui,'creation_form',side_effect=complete), patch.object(ui,'choose',return_value='提交创建'):
                cci.create({})
            plan=create.call_args.args[1]
            saved=json.loads(next((Path(directory)/'.cache/cci').glob('*request*.json')).read_text())
            self.assertEqual(plan,saved)
            self.assertEqual(plan['ports'],'8080')
