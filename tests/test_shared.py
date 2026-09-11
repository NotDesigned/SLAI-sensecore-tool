"""Shared routing, transport, and review boundaries after the breaking cleanup."""
import contextlib
import io
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import Mock, patch
from scripts import cli, cloud, dnat, network, rest, ui


class SharedTests(unittest.TestCase):
    def test_old_commands_fail_without_loading_credentials(self):
        for command in ('init', 'docker-push', 'cci-create', 'eip'):
            with patch.object(cli.sys, 'argv', ['main.py', command]), patch.object(cli, 'load_config') as load, contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(cli.main(), 2)
            load.assert_not_called()

    def test_old_proxy_config_is_rejected_not_silently_direct(self):
        with tempfile.TemporaryDirectory() as directory:
            p=Path(directory)/'config.toml'
            p.write_text('[paths]\n[sco]\n[cci.ssh_proxy]\nserver="192.0.2.2"\n')
            with patch.object(cli,'CONFIG',p), self.assertRaisesRegex(cli.ConfigError,'network.socks5'):
                cli.load_config()

    def test_network_applies_only_to_copy_of_environment(self):
        config={'network':{'acp_proxy':True,'socks5':{'server':'::1','username':'a','password':'a@b'}}}
        env={'PATH':'unchanged'}
        result=network.acp_environment(config,env)
        self.assertEqual(result['HTTPS_PROXY'],'socks5://a:a%40b@[::1]:1080')
        self.assertEqual(env,{'PATH':'unchanged'})
        with self.assertRaises(cli.ConfigError):
            network.acp_environment({'network':{'acp_proxy':True}},env)

    def test_browse_retry_and_invalid_input_do_not_repeat_queries(self):
        fetch=Mock(side_effect=[cli.ConfigError('offline'),[{'name':'one'}]])
        with patch('builtins.input',side_effect=['r','bad','0']), contextlib.redirect_stdout(io.StringIO()):
            ui.browse('items',fetch,lambda r:r['name'],Mock())
        self.assertEqual(fetch.call_count,2)

    def test_mutation_transport_and_error_redaction(self):
        config={'sco':{'access_key_id':'test-id','access_key_secret':'test-secret'}}
        response=Mock(); response.read.return_value=b'{}'; response.__enter__=Mock(return_value=response); response.__exit__=Mock(return_value=False)
        with patch.object(rest.urllib.request,'urlopen',return_value=response) as call:
            rest.request_json(config,'https://example.test/resource',method='POST',body={'name':'test'})
        request=call.call_args.args[0]
        self.assertEqual(request.method,'POST')
        self.assertEqual(json.loads(request.data),{'name':'test'})
        self.assertIn('hmac accesskey=',request.get_header('Authorization'))
        error=urllib.error.HTTPError('https://example.test',403,'denied',{},io.BytesIO(b'{"message":"SECRET"}'))
        with patch.object(rest.urllib.request,'urlopen',side_effect=error):
            with self.assertRaises(rest.RestError) as caught:
                rest.request_json(config,'https://example.test',method='POST',body={})
        self.assertEqual(caught.exception.status,403)
        self.assertNotIn('SECRET',str(caught.exception))

    def test_pagination_token_cycle_fails(self):
        fetch=Mock(side_effect=[{'items':[{'name':'one'}],'next_page_token':'2'},
                               {'items':[{'name':'two'}],'next_page_token':'1'}])
        with self.assertRaisesRegex(cli.ConfigError,'标识重复'):
            rest.pages(fetch,'items')
        self.assertEqual(fetch.call_count,2)

    def test_dnat_uses_live_identity_and_never_writes_during_preparation(self):
        api=Mock(); api.request.return_value={'id':'id','tenant_id':'tenant'}
        api.list.return_value=[{'properties':{'external_ip':'192.0.2.1','external_port':'23000'}}]
        with patch.object(dnat,'ask',side_effect=['23001','22']), patch.object(dnat,'choose',return_value='tcp'), patch.object(dnat,'prepare',side_effect=lambda a,b,r:b):
            body=dnat.new_rule(api,'new-rule')
        self.assertEqual(body['creator_id'],'id')
        self.assertEqual(body['tenant_id'],'tenant')
        self.assertEqual(body['properties']['external_port'],'23001')
        self.assertEqual(api.request.call_args.args,('GET',))
