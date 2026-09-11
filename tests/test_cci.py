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

SPEC_TABLE = '''+---+
| WORKER SPEC | CHIP MODEL | CHIP COUNT | CPU | VCPU COUNT | MEMORY(GIB) | ZONE |
+---+
| cpu-small | NVIDIA card | 0 | Intel Xeon | 2 | 4 | cn-sh-01e |
| | | | 8468 | | | |
| gpu-large- | NVIDIA card | 1 | Intel | 8 | 128 | cn-sh-01e |
| wrapped | | | | | | |
+---+
'''


def spec_response(key='nvidia.com/mig-3g.40gb'):
    specs = [{'name': name, 'cpu': {'vcpu_allocatable': cpu},
              'memory': {'allocatable': memory}, 'device': {'number': count, 'resource_key': key},
              'zones': ['cn-sh-01e']}
             for name, cpu, memory, count in [('cpu-small', 2, 4, 0), ('gpu-large-wrapped', 8, 128, 1)]]
    return SPEC_TABLE + '\n' + json.dumps({'message': 'Response status: 200 OK, body: {Reader:' + json.dumps({'resource_specs': specs}) + '}'})


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
        client.flags = []
        client.executable = Path('/fake/sco')
        client.env = {}
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

    def test_wrapped_spec_table_preserves_ids(self):
        rows = cloud.parse_specs(SPEC_TABLE)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]['CPU'], 'Intel Xeon 8468')
        self.assertEqual(rows[1]['WORKER SPEC'], 'gpu-large-wrapped')
        with self.assertRaises(cli.ConfigError):
            cloud.parse_specs('unexpected output')

    def test_resource_key_from_response_without_printing_debug_credentials(self):
        for key in ['nvidia.com/gpu', 'nvidia.com/mig-3g.40gb', 'amd.com/gpu']:
            raw = json.dumps({'message': 'Authorization: SECRET'}) + '\n' + spec_response(key)
            with contextlib.redirect_stdout(io.StringIO()) as output:
                rows = cloud.enrich_specs(raw)
            self.assertEqual(rows[1]['RESOURCE KEY'], key)
            self.assertNotIn('SECRET', output.getvalue())

    def test_missing_invalid_or_mismatched_resource_details_fail_closed(self):
        for raw in [SPEC_TABLE, spec_response(''), spec_response('bad key'),
                    spec_response().replace('\\"vcpu_allocatable\\": 8', '\\"vcpu_allocatable\\": 16')]:
            with self.assertRaises(cli.ConfigError):
                cloud.enrich_specs(raw)

    def test_gpu_prepare_never_prompts_for_resource_key(self):
        client = Mock()
        client.resources.return_value = [{'name': 'ws', 'id': 'ws'}]
        client.clusters.return_value = [{'name': 'pool', 'zone': 'cn-sh-01e', 'properties': {'vpc_id': 'vpc'}}]
        client.specs.return_value = cloud.enrich_specs(spec_response())
        answers = ['1', '1', '2', 'test-gpu', 'echo ok', '1', '1', '1', '']
        with patch('builtins.input', side_effect=answers) as prompt, patch.object(cloud, 'select_image', return_value='r.test/a:v1'):
            _, _, _, doc = cci.prepare(client, {'accelerator_key': 'wrong/legacy', 'ssh_enabled': False})
        request = doc['template']['containers'][0]['resource_request']
        self.assertEqual(request['nvidia.com/mig-3g.40gb'], '1')
        self.assertNotIn('wrong/legacy', request)
        self.assertFalse(any('资源键' in call.args[0] for call in prompt.call_args_list))

    def test_pagination_and_type_filter(self):
        client = self.client()
        first = [{'name': str(i), 'type': 'test', 'id': str(i)} for i in range(100)]
        second = [{'name': 'last', 'type': 'test', 'id': 'last'}]
        with patch.object(client, 'read', side_effect=[json.dumps(first), json.dumps(second)]) as read:
            self.assertEqual(len(client.resources('test')), 101)
        self.assertEqual(read.call_args.args[0][-1], '2')
        with patch.object(client, 'read', return_value=json.dumps(first)):
            with self.assertRaisesRegex(cli.ConfigError, '重复返回整页'):
                client.resources('test')
        with patch.object(client, 'read', return_value='{}'), self.assertRaises(cli.ConfigError):
            client.resources('test')

    def test_cluster_uses_live_associations_instead_of_srm_snapshot(self):
        client = self.client()
        current = {'name': 'pool', 'state': 'ACTIVE', 'properties': {'workspace_uids': ['ws']}}
        with patch.object(client, 'resources', return_value=[{'name': 'pool', 'properties': '{}'}]):
            with patch.object(client, 'read', return_value=json.dumps(current)):
                self.assertEqual(client.clusters({'id': 'ws'}), [current])
                self.assertEqual(client.clusters({'id': 'other'}), [])

    def test_failed_query_and_timeout_do_not_echo_secrets(self):
        client = self.client()
        with patch.object(subprocess, 'run', return_value=subprocess.CompletedProcess([], 1, '', 'SECRET')):
            with self.assertRaises(cli.ConfigError) as error:
                client.read(['srm', 'resources', 'list'])
        self.assertNotIn('SECRET', str(error.exception))
        with patch.object(subprocess, 'run', side_effect=subprocess.TimeoutExpired('sco', 45)):
            with self.assertRaises(cli.ConfigError):
                client.read(['zones', 'list'])

    def test_workspace_scope_is_passed_to_commands(self):
        client = self.client()
        client.flags = ['--profile', 'custom', '--region', 'cnsh01']
        client.scope({'subscription_name': 'sub', 'resource_group_name': 'group'})
        command = client.command(['cci', 'apps', 'create', 'test'])
        self.assertEqual(command[1:9], ['--profile', 'custom', '--region', 'cnsh01',
                                       '--subscription', 'sub', '--resource-group', 'group'])

    def test_prepare_uses_selected_scope_spec_network_and_volume(self):
        client = Mock()
        client.current_username.return_value = 'test-user'
        workspace = {'name': 'ws', 'id': 'ws-id'}
        cluster = {'name': 'pool', 'zone': 'cn-sh-01e', 'properties': {'vpc_id': 'vpc'}}
        volume = {'name': 'disk', 'id': 'disk-id', 'zone': 'cn-sh-01e'}
        client.resources.side_effect = [[workspace], [volume, {**volume, 'zone': 'other'}]]
        client.clusters.return_value = [cluster]
        client.specs.return_value = cloud.parse_specs(SPEC_TABLE)
        answers = ['1', '1', '1', 'test-app', 'echo ok', '1', '2', '/data', '/', '1', '1', '8080']
        with patch('builtins.input', side_effect=answers), patch.object(cloud, 'select_image', return_value='registry.test/app:v1'):
            workspace_name, name, ports, document = cci.prepare(client, {'ssh_enabled': False})
        self.assertEqual((workspace_name, name, ports), ('ws', 'test-app', '8080'))
        self.assertEqual(document['display_name'], 'test-app')
        client.scope.assert_called_once_with(workspace)
        client.specs.assert_called_once_with('ws', 'pool')
        self.assertEqual(document['resource_pool']['vpc_id'], 'vpc')
        container = document['template']['containers'][0]
        self.assertEqual(container['resource_request'], {'cpu': '2', 'memory': '4GiB'})
        self.assertEqual(container['volume_mounts'][0]['id'], 'disk-id')
        self.assertEqual(container['command'], ['/bin/sh', '-c', 'echo ok'])

    def test_local_images_excludes_dangling_and_local_only_tags(self):
        images = [{'Repository': repo, 'Tag': tag} for repo, tag in
                  [('local', 'v1'), ('registry.test/app', 'v1'), ('registry.test/app', '<none>')]]
        with patch.object(cloud.shutil, 'which', return_value='/docker'):
            with patch.object(subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, '\n'.join(map(json.dumps, images)))):
                self.assertEqual(cloud.local_images(), ['registry.test/app:v1'])

    def test_save_only_and_cancel_never_submit(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(cli, 'ROOT', Path(directory)):
            with patch.object(cloud, 'Client'), patch.object(cci, 'prepare', return_value=('ws', 'app', '', {'replicas': 1})):
                with patch('builtins.input', return_value=''), patch.object(cli, 'run') as run:
                    cci.create({'cci': {}})
                run.assert_not_called()
                path = next((Path(directory) / '.cache' / 'cci').glob('*.yaml'))
                self.assertEqual(yaml.safe_load(path.read_text()), {'replicas': 1})
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            with patch.object(cloud, 'Client'), patch.object(cci, 'prepare', side_effect=ui.Cancelled):
                with patch.object(cli, 'run') as run:
                    cci.create({})
                run.assert_not_called()

    def test_submit_uses_exact_saved_document_and_ports(self):
        client = self.client()
        with tempfile.TemporaryDirectory() as directory, patch.object(cli, 'ROOT', Path(directory)):
            with patch.object(cloud, 'Client', return_value=client):
                with patch.object(cci, 'prepare', return_value=('ws', 'app', '8080', {'replicas': 1})):
                    with patch('builtins.input', return_value='2'), patch.object(cli, 'run') as run:
                        cci.create({})
            args = run.call_args.args[0]
            self.assertEqual(args[1:6], ['cci', 'apps', 'create', 'app', '--workspace-name'])
            self.assertEqual(args[-2:], ['--ports', '8080'])
            self.assertEqual(yaml.safe_load(Path(args[args.index('--config') + 1]).read_text()), {'replicas': 1})


if __name__ == '__main__':
    unittest.main()
