"""REST query contracts, scoped caches, proxy transport and desktop clipboard."""
import ssl
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from scripts import acp, cli, clipboard, cloud, dnat, network, rest

UID = '11111111-1111-4111-8111-111111111111'
WS = dict(name='ws', region='cn-sh-01', subscription_name='sub', resource_group_name='group', zone='cn-sh-01z')
POOL = dict(name='pool', uid='pool-uid', state='ACTIVE', vpc_id='vpc',
            id='/subscriptions/sub/resourceGroups/group/zones/cn-sh-01e/aec2s/pool')
SPEC = {'name': 'gpu', 'cpu': {'vcpu_allocatable': 2}, 'memory': {'allocatable': 32},
        'device': {'number': 1, 'resource_key': 'nvidia.com/mig-3g.40gb'}, 'zones': ['cn-sh-01e']}


def client(kind=cloud.Client, config=None):
    return kind(config or {'account': {}})


class RestMigrationTests(unittest.TestCase):
    def test_numbered_binding_pages_without_next_token(self):
        fetch = Mock(side_effect=[{'aec2s': [{'name': 'a'}], 'total_size': 2, 'next_page_token': ''},
                                 {'aec2s': [{'name': 'b'}], 'total_size': 2, 'next_page_token': ''}])
        self.assertEqual([r['name'] for r in rest.pages(fetch, 'aec2s', numbered=True)], ['a', 'b'])
        self.assertEqual([c.args[0] for c in fetch.call_args_list], ['1', '2'])
        with self.assertRaises(rest.IncompletePage):
            rest.pages(lambda _: {'aec2s': [], 'total_size': 2}, 'aec2s', numbered=True)

    def test_malformed_metadata_and_repeated_pages_fail(self):
        for data in ({'items': [{'name': 'a'}], 'total_size': 0},
                     {'items': [], 'total_size': True}, {'items': [], 'next_page_token': True}):
            with self.assertRaises(cli.ConfigError):
                rest.pages(lambda _: data, 'items')
        with self.assertRaisesRegex(cli.ConfigError, '重复返回整页'):
            rest.pages(lambda _: {'items': [{'name': 'a'}], 'total_size': 2}, 'items', numbered=True)

    def test_incomplete_catalog_errors_without_cli_fallback(self):
        c=client();row={'name':'a','type':'test'}
        with patch.object(rest,'get_json',return_value={'resources':[row],'total_size':2}):
            with self.assertRaises(rest.IncompletePage):c.resources('test')
        self.assertFalse(hasattr(c,'read'))

    def test_catalog_scope_and_returned_mutations_do_not_poison_cache(self):
        c = client()
        rows = [dict(name=k, type='test', subscription_name='sub', resource_group_name=k) for k in ('a', 'b')]
        with patch.object(rest, 'get_json', return_value={'resources': rows, 'total_size': 2}) as get:
            c.scope({**WS, 'resource_group_name': 'a'})
            result = c.resources('test');result[0]['name'] = 'changed'
            self.assertEqual(c.resources('test')[0]['name'], 'a')
            c.scope({**WS, 'resource_group_name': 'b'})
            self.assertEqual(c.resources('test')[0]['name'], 'b')
            self.assertEqual(get.call_count, 2)
        self.assertEqual(c._workspace['resource_group_name'], 'b')

    def test_identity_is_cached_only_in_client(self):
        c = client()
        with patch.object(rest, 'get_json', return_value={'id': UID, 'username': 'example'}) as get:
            self.assertEqual(c.current_username(), 'example')
            self.assertEqual(c.identity_data()['id'], UID)
            get.assert_called_once()
            client().identity_data()
            self.assertEqual(get.call_count, 2)

    def test_specs_use_associated_pool_scope_and_preserve_resource_key(self):
        c = client();c.scope(WS)
        with patch.object(rest, 'get_json', side_effect=[{'aec2s': [POOL], 'total_size': 1},
                                                     {'resource_specs': [SPEC]}]) as get:
            c.clusters(WS)
            result = c.specs('ws', 'pool')
        self.assertEqual(result[0]['RESOURCE KEY'], SPEC['device']['resource_key'])
        self.assertEqual(result[0]['ZONE'], 'cn-sh-01e')
        self.assertIn('/zones/cn-sh-01e/aec2s/pool/resourceSpecs', get.call_args.args[1])
        self.assertEqual(get.call_count, 2)
        for bad in ({**POOL, 'id': POOL['id'].replace('cn-sh-01e', 'cn-sh-02a')},
                    {**POOL, 'id': POOL['id'].replace('/sub/', '/other/')}, {**POOL, 'uid': ''}):
            with patch.object(rest, 'get_json', return_value={'aec2s': [bad]}), self.assertRaises(cli.ConfigError):
                client().clusters(WS)

    def test_acp_rest_total_last_page_and_explicit_proxy(self):
        config = {'account': {}, 'network': {'acp_proxy': True, 'socks5': {'server': '127.0.0.1', 'port': 1080,
                  'username': 'u', 'password': 'p@ss'}}}
        c = client(acp.Client, config);c.scope(WS);c.user_id = UID
        row = dict(name='job', uid='job-uid', ownership={'user_id': UID})
        with patch.object(acp, 'get_json', return_value={'training_jobs': [row], 'total_size': 21, 'next_page_token': ''}) as get:
            page = c.jobs_page('ws', 1, 20)
        self.assertFalse(page.more)
        self.assertEqual(page.total, 21)
        self.assertEqual(get.call_args.kwargs['proxy'], 'socks5h://u:p%40ss@127.0.0.1:1080')
        with patch.object(acp, 'get_json', return_value={'training_jobs': [row], 'total_size': 21, 'next_page_token': '1'}):
            with self.assertRaises(cli.ConfigError):
                c.jobs_page('ws', 0, 20)

    def test_dnat_filtered_read_never_changes_default_conflict_query(self):
        eip = dict(name='eip', region='cn-sh-01', subscription_name='sub', resource_group_name='group', zone='cn-sh-01e')
        api = dnat.Api({'account': {}}, eip)
        with patch.object(api, 'request', return_value={'dnat_rules': [], 'total_size': 0}) as request:
            api.list(creator_id=UID)
            filtered = request.call_args.args[1]
            api.list()
            unfiltered = request.call_args.args[1]
        self.assertIn('creator_id', filtered)
        self.assertNotIn('filter', unfiltered)

    def test_dnat_failure_never_returns_partial_results(self):
        first, second = Mock(base='one'), Mock(base='two')
        first.current_user_id.return_value = UID
        first.list.return_value = []
        second.list.side_effect = cli.ConfigError('offline')
        with patch.object(dnat, 'Client') as c, patch.object(dnat, 'Api', side_effect=[first, second]):
            c.return_value.resources.return_value = [{'name': 'one'}, {'name': 'two'}]
            with self.assertRaises(cli.ConfigError):
                dnat.all_my_rules({})
        first.list.assert_called_once_with(creator_id=UID)
        second.list.assert_called_once_with(creator_id=UID)

    def test_proxy_verifies_tls_does_not_retry_redirect_or_fall_back(self):
        from urllib3.exceptions import ProxyError
        cfg = {'account': {'access_key_id': 'fake', 'access_key_secret': 'fake-secret'}}
        with patch('urllib3.contrib.socks.SOCKSProxyManager') as cls, patch.object(rest.urllib.request, 'urlopen') as direct:
            manager = cls.return_value.__enter__.return_value
            manager.request.side_effect = ProxyError('secret-proxy-url', OSError('failed'))
            with self.assertRaises(cli.ConfigError) as caught:
                rest.get_json(cfg, 'https://example.test', proxy='socks5h://localhost:1080')
        self.assertNotIn('secret-proxy-url', str(caught.exception))
        context = cls.call_args.kwargs['ssl_context']
        self.assertTrue(context.check_hostname)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertFalse(manager.request.call_args.kwargs['retries'])
        self.assertFalse(manager.request.call_args.kwargs['redirect'])
        direct.assert_not_called()


class ClipboardTests(unittest.TestCase):
    def test_macos_copies_exact_utf8_via_stdin(self):
        text = 'ssh -o \'ProxyCommand=python /project/helper.py %h %p\' root@192.0.2.1\n中文'
        with patch.object(clipboard.sys, 'platform', 'darwin'), patch.object(clipboard.subprocess, 'run', return_value=Mock(returncode=0)) as run:
            clipboard.copy_text(text)
        self.assertEqual(run.call_args.args[0], ['/usr/bin/pbcopy'])
        self.assertEqual(run.call_args.kwargs['input'], text.encode())
        self.assertNotIn('shell', run.call_args.kwargs)

    def test_windows_and_linux_clipboards(self):
        with patch.object(clipboard.sys, 'platform', 'win32'), patch.object(clipboard.shutil, 'which', return_value='pwsh'), \
             patch.object(clipboard.subprocess, 'run', return_value=Mock(returncode=0)) as run:
            clipboard.copy_text('literal $() "quoted"')
        self.assertIn('Set-Clipboard', run.call_args.args[0][-1])
        self.assertEqual(run.call_args.kwargs['input'], b'literal $() "quoted"')
        with patch.object(clipboard.sys, 'platform', 'linux'), patch.dict(clipboard.os.environ, {'WAYLAND_DISPLAY': 'wayland-0'}), \
             patch.object(clipboard.shutil, 'which', return_value='/usr/bin/wl-copy'), \
             patch.object(clipboard.subprocess, 'run', return_value=Mock(returncode=0)) as run:
            clipboard.copy_text('text')
        self.assertEqual(run.call_args.args[0], ['/usr/bin/wl-copy'])

    def test_failure_is_reported_and_file_fallback_is_private(self):
        with patch.object(clipboard.sys, 'platform', 'darwin'), patch.object(clipboard.subprocess, 'run', return_value=Mock(returncode=1)):
            with self.assertRaisesRegex(cli.ConfigError, '复制未完成'):
                clipboard.copy_text('text')
        with tempfile.TemporaryDirectory() as directory, patch.object(cli, 'ROOT', Path(directory)):
            path = clipboard.save_text('ssh -p 1234 user@host')
            self.assertEqual(path.read_text(), 'ssh -p 1234 user@host')
            if clipboard.sys.platform != 'win32':
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
