import contextlib
import io
import socket
import threading
import time
import unittest
from unittest.mock import Mock, patch

from scripts import ssh_probe, cci_ssh


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
            self.assertEqual(ssh_probe.check({}, '192.0.2.1', 22), (False, 'unreachable'))
        with patch.object(ssh_probe, 'check', return_value=(False, 'unreachable')), contextlib.redirect_stdout(io.StringIO()) as output:
            ssh_probe.report({}, '192.0.2.1', 22)
        self.assertIn('可能未处于 SLAI 内网', output.getvalue())
        self.assertIn('README', output.getvalue())
        self.assertIn('sshd', output.getvalue())

    def test_configured_proxy_is_used_and_child_is_cleaned_up(self):
        defaults = {'ssh_proxy': {'server': '192.0.2.2', 'username': 'u', 'password': 'hidden'}}
        child = Mock(stdin=io.BytesIO(), stdout=io.BytesIO(b'SSH-2.0-test\r\n'))
        child.poll.return_value = None
        with patch.object(cci_ssh, 'find_ncat', return_value='/ncat'), \
             patch.object(ssh_probe.subprocess, 'Popen', return_value=child) as popen, \
             patch.object(socket, 'create_connection') as direct:
            self.assertEqual(ssh_probe.check(defaults, '192.0.2.1', 22), (True, 'ssh'))
        direct.assert_not_called()
        self.assertIn('--proxy', popen.call_args.args[0])
        child.kill.assert_called_once()
        child.wait.assert_called_once()

    def test_missing_ncat_does_not_try_direct_connection(self):
        with patch.object(cci_ssh, 'find_ncat', return_value=None), patch.object(socket, 'create_connection') as direct:
            self.assertEqual(ssh_probe.check({'ssh_proxy': {'server': '192.0.2.2'}}, '192.0.2.1', 22), (False, 'ncat_missing'))
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
        with patch.object(cci_ssh, 'find_ncat', return_value='/ncat'), \
             patch.object(ssh_probe.subprocess, 'Popen', return_value=child):
            self.assertEqual(ssh_probe.check({'ssh_proxy': {'server': '192.0.2.2'}}, '192.0.2.1', 22, timeout=0.01), (False, 'timeout'))
        child.kill.assert_called_once()
