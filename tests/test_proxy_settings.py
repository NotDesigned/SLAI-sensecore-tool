import copy
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock,patch
from scripts import cli,proxy_settings,ui

class HandshakeTests(unittest.TestCase):
    def probe(self, handler, *, auth=False, timeout=1):
        errors=[]
        with socket.socket() as server:
            server.bind(('127.0.0.1',0));server.listen(1);server.settimeout(2)
            def serve():
                try:
                    with server.accept()[0] as connection:
                        connection.settimeout(2)
                        handler(connection)
                except Exception as error:errors.append(error)
            thread=threading.Thread(target=serve);thread.start()
            config={'network':{'socks5':{'server':'127.0.0.1','port':server.getsockname()[1],
                                      'username':'user' if auth else '', 'password':'secret' if auth else ''}}}
            result=proxy_settings.check(config,timeout)
            thread.join(3);self.assertFalse(thread.is_alive());self.assertEqual(errors,[])
            return result

    def test_no_auth_handshake_and_no_target_connection(self):
        def handler(c):
            self.assertEqual(c.recv(3),b'\x05\x01\x00')
            c.sendall(b'\x05');time.sleep(.01);c.sendall(b'\x00')
            self.assertEqual(c.recv(1),b'')
        self.assertIn('可用（握手通过，无需认证）',self.probe(handler))

    def test_username_password_authentication(self):
        def handler(c):
            self.assertEqual(c.recv(3),b'\x05\x01\x02');c.sendall(b'\x05\x02')
            data=b''
            while len(data)<13:data+=c.recv(13-len(data))
            self.assertEqual(data,b'\x01\x04user\x06secret');c.sendall(b'\x01\x00')
        self.assertIn('握手、认证通过',self.probe(handler,auth=True))

    def test_protocol_mismatch_and_auth_rejection(self):
        def reply(packet):
            def handler(c):c.recv(3);c.sendall(packet)
            return handler
        self.assertIn('不是 SOCKS5',self.probe(reply(b'HT')))
        self.assertIn('认证方式不匹配',self.probe(reply(b'\x05\xff')))
        def reject(c):
            c.recv(3);c.sendall(b'\x05\x02');c.recv(255);c.sendall(b'\x01\x01')
        self.assertIn('认证失败',self.probe(reject,auth=True))

    def test_timeout_and_unconfigured(self):
        def silence(c):c.recv(3);time.sleep(.1)
        self.assertIn('连接超时',self.probe(silence,timeout=.025))
        with patch.object(socket,'create_connection') as connect:
            self.assertIn('未配置',proxy_settings.check({}))
        connect.assert_not_called()

class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'config.toml'
        self.original='[account]\naccess_key_id="ak"\naccess_key_secret="sk"\n[network]\nacp_proxy=true # keep\n[network.socks5]\nserver="127.0.0.1"\nport=1080\nusername="user"\npassword="secret"\n[custom]\nvalue="keep"\n'
        self.path.write_text(self.original)
        p=patch.object(cli,'CONFIG',self.path);p.start();self.addCleanup(p.stop)

    def test_same_endpoint_can_preserve_password_and_other_settings(self):
        with patch.object(ui,'choose',side_effect=['配置代理','用户名和密码','保留已保存的密码']),patch.object(ui,'ask',side_effect=['127.0.0.1','user']),patch.object(ui,'number',return_value=1080),patch.object(ui,'secret') as secret:
            proxy_settings.configure()
        config=cli.load_config();self.assertEqual(config['network']['socks5']['password'],'secret')
        self.assertTrue(config['network']['acp_proxy']);self.assertEqual(config['custom']['value'],'keep')
        self.assertIn('# keep',self.path.read_text());secret.assert_not_called()

    def test_new_endpoint_requires_new_secret_and_cancellation_does_not_save(self):
        with patch.object(ui,'choose',side_effect=['配置代理','用户名和密码']),patch.object(ui,'ask',side_effect=['192.0.2.1','user']),patch.object(ui,'number',return_value=1080),patch.object(ui,'secret',side_effect=ui.Cancelled):
            with self.assertRaises(ui.Cancelled):proxy_settings.configure()
        self.assertEqual(self.path.read_text(),self.original)

    def test_disable_also_turns_off_acp_proxy(self):
        with patch.object(ui,'choose',return_value='关闭代理'):proxy_settings.configure()
        config=cli.load_config()['network'];self.assertEqual(config['socks5']['server'],'')
        self.assertFalse(config['acp_proxy'])

    def test_invalid_port_and_unhidden_secret_never_save(self):
        import getpass
        with patch.object(ui,'choose',side_effect=['配置代理','无需认证']),patch.object(ui,'ask',return_value='127.0.0.1'),patch.object(ui,'number',return_value=70000):
            with self.assertRaises(cli.ConfigError):proxy_settings.configure()
        with patch.object(ui,'choose',side_effect=['配置代理','用户名和密码']),patch.object(ui,'ask',side_effect=['192.0.2.1','user']),patch.object(ui,'number',return_value=1080),patch.object(ui,'secret',side_effect=getpass.GetPassWarning):
            with self.assertRaises(cli.ConfigError):proxy_settings.configure()
        self.assertEqual(self.path.read_text(),self.original)

class HomeSettingsUiTests(unittest.IsolatedAsyncioTestCase):
    async def test_settings_are_buttons_and_only_services_are_numbered(self):
        from scripts.tui import SlaiApp,Operation
        from textual.widgets import Button,OptionList,Static
        from scripts import onboarding
        with patch.object(onboarding,'state',return_value=('ready','选择服务')),patch.object(cli,'menu_title',return_value='SLAI-tool'),patch.object(proxy_settings,'status',return_value='SOCKS5：可用（握手、认证通过）') as check,patch.object(cli,'execute') as execute,patch.object(proxy_settings,'configure') as configure:
            app=SlaiApp()
            async with app.run_test(size=(80,24)) as pilot:
                await pilot.pause(.1)
                self.assertEqual(app.query_one('#home',OptionList).option_count,4)
                self.assertEqual([action for action,_ in cli.MENU_ITEMS],['ccr','cci','dnat','acp'])
                for key in ('home-account','home-workspace','home-proxy','home-proxy-check'):
                    self.assertTrue(app.query_one('#'+key,Button).display)
                self.assertIn('认证通过',str(app.query_one('#proxy-status',Static).render()))
                for button,action in [('home-account','configure'),('home-workspace','workspace')]:
                    await pilot.click('#'+button)
                    for _ in range(100):
                        await pilot.pause(.025)
                        if execute.call_args and execute.call_args.args==(action,) and len(app.screen_stack)==1:break
                    execute.assert_called_with(action)
                    self.assertEqual(len(app.screen_stack),1)
                await pilot.click('#home-proxy')
                for _ in range(100):
                    await pilot.pause(.025)
                    if configure.called and len(app.screen_stack)==1:break
                configure.assert_called_once()
                self.assertGreaterEqual(check.call_count,2)
