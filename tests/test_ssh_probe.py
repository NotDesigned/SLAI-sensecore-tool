import contextlib
import io
import socket
import threading
import time
import unittest
from unittest.mock import Mock, patch



from scripts import network, ssh_probe, cci_ssh

class ProbeTests(unittest.TestCase):
    def test_direct_ssh_banner_over_loopback(self):
        with socket.socket() as server:
            server.bind(('127.0.0.1', 0))
            server.listen(1)
            server.settimeout(2)
            def serve():
                with server.accept()[0] as conn:
                    conn.sendall(b'notice\r\nSSH-2.0-test\r\n')
                    conn.settimeout(2)
                    conn.recv(128)
            thread = threading.Thread(target=serve)
            thread.start()
            success, reason = ssh_probe.check({}, '127.0.0.1', server.getsockname()[1], timeout=2)
            thread.join(2)
        self.assertTrue(success)
        self.assertEqual(reason, 'ssh')

    def test_direct_failure_and_guidance_does_not_claim_definite_cause(self):
        with patch.object(socket, 'create_connection', side_effect=TimeoutError):
            self.assertEqual(ssh_probe.check({}, '192.0.2.1', 22), (False, 'timeout'))
        with patch.object(ssh_probe, 'check', return_value=(False, 'unreachable')), contextlib.redirect_stdout(io.StringIO()) as output:
            ssh_probe.report({}, '192.0.2.1', 22)
        self.assertIn('可能未处于 SLAI 内网', output.getvalue())
        self.assertIn('README', output.getvalue())
        self.assertIn('sshd', output.getvalue())

    def test_configured_proxy_is_used_and_child_is_cleaned_up(self):
        defaults = {'network': {'socks5': {'server': '192.0.2.2', 'username': 'u', 'password': 'hidden'}}}
        child = Mock(stdin=io.BytesIO(), stdout=io.BytesIO(b'SSH-2.0-test\r\n'))
        child.poll.return_value = None
        with patch.object(network, 'find_ncat', return_value='/ncat'), \
             patch.object(ssh_probe.subprocess, 'Popen', return_value=child) as popen, \
             patch.object(socket, 'create_connection') as direct:
            self.assertEqual(ssh_probe.check(defaults, '192.0.2.1', 22), (True, 'ssh'))
        direct.assert_not_called()
        self.assertIn('--proxy', popen.call_args.args[0])
        child.kill.assert_called_once()
        child.wait.assert_called_once()

    def test_missing_ncat_does_not_try_direct_connection(self):
        with patch.object(network, 'find_ncat', return_value=None), patch.object(socket, 'create_connection') as direct:
            self.assertEqual(ssh_probe.check({'network': {'socks5': {'server': '192.0.2.2'}}}, '192.0.2.1', 22), (False, 'ncat_missing'))
        direct.assert_not_called()

    def test_banner_check_rejects_http(self):
        self.assertFalse(ssh_probe.ssh_banner(io.BytesIO(b'HTTP/1.1 200 OK\r\n')))

    def test_unresponsive_proxy_is_bounded(self):
        class SlowStream(io.BytesIO):
            def readline(self, *args):
                time.sleep(0.05)
                return b''
        child = Mock(stdin=io.BytesIO(), stdout=SlowStream())
        child.poll.return_value = None
        with patch.object(network, 'find_ncat', return_value='/ncat'), \
             patch.object(ssh_probe.subprocess, 'Popen', return_value=child):
            self.assertEqual(ssh_probe.check({'network': {'socks5': {'server': '192.0.2.2'}}}, '192.0.2.1', 22, timeout=0.01), (False, 'timeout'))
        child.kill.assert_called_once()


    def test_timeout_guidance_is_visible_in_tui_without_exposing_proxy_secrets(self):
        from scripts import ui
        config={'network':{'socks5':{'server':'192.0.2.2','username':'user','password':'do-not-print'}}}
        with patch.object(network,'ensure_ncat'), patch.object(ssh_probe,'check',return_value=(False,'timeout')), \
             patch.object(ui,'active',return_value=True), patch.object(ssh_probe,'print',side_effect=print), patch.object(ui,'show_text') as details, \
             contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertFalse(ssh_probe.report(config,'192.0.2.1',22))
        self.assertIn('SSH 连接超时',details.call_args.args[1])
        self.assertIn('已经使用配置的 SOCKS5',details.call_args.args[1])
        self.assertNotIn('do-not-print',str(details.call_args)+output.getvalue())
        self.assertNotIn('添加 SOCKS5',details.call_args.args[1])

    def test_direct_timeout_offers_proxy_configuration(self):
        with patch.object(ssh_probe,'check',return_value=(False,'timeout')), contextlib.redirect_stdout(io.StringIO()) as output:
            ssh_probe.report({},'192.0.2.1',22)
        self.assertIn('添加 SOCKS5',output.getvalue())
        self.assertIn('[network.socks5]',output.getvalue())
        self.assertIn('不要重复添加',output.getvalue())

    def test_real_socket_without_banner_times_out(self):
        stop=threading.Event()
        with socket.socket() as server:
            server.bind(('127.0.0.1',0));server.listen(1);server.settimeout(2)
            def serve():
                with server.accept()[0]:stop.wait(2)
            thread=threading.Thread(target=serve);thread.start()
            try:
                self.assertEqual(ssh_probe.check({},'127.0.0.1',server.getsockname()[1],timeout=.1),(False,'timeout'))
            finally:
                stop.set();thread.join(2)
