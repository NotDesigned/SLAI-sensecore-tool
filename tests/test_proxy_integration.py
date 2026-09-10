"""Loopback-only protocol check using a real Ncat; no cloud credentials."""
import concurrent.futures
import contextlib
import io
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from scripts import cci_ssh, ncat_proxy, cli


class ProxyTests(unittest.TestCase):
    def test_missing_ncat_reports_platform_install_command(self):
        with patch.object(sys, 'argv', ['proxy', '192.0.2.1', '22']), \
             patch.object(shutil, 'which', return_value=None), \
             patch.object(cci_ssh, 'ncat_install_hint', return_value='brew install nmap'), \
             contextlib.redirect_stderr(io.StringIO()) as output:
            self.assertEqual(ncat_proxy.main(), 1)
        self.assertIn('brew install nmap', output.getvalue())

    def test_proxy_exec_uses_config_and_preserves_streams(self):
        config = {'cci': {'ssh_proxy': {'server': '192.0.2.2', 'username': 'user', 'password': 'test%h'}}}
        with patch.object(sys, 'argv', ['proxy', '192.0.2.1', '22']), \
             patch.object(shutil, 'which', return_value='/ncat'), \
             patch.object(cli, 'load_config', return_value=config), patch.object(os, 'execv') as execute:
            ncat_proxy.main()
        executable, args = execute.call_args.args
        self.assertEqual(executable, '/ncat')
        self.assertEqual(args[args.index('--proxy-auth') + 1], 'user:test%h')
        self.assertEqual(args[-2:], ['192.0.2.1', '22'])

    def test_generated_command_handles_percent_and_spaces_in_paths(self):
        import shlex
        defaults = {'ssh_proxy': {'server': '192.0.2.2'}}
        with patch.object(sys, 'executable', '/tmp/Python %h/bin/python'), \
             patch.object(cci_ssh, '__file__', '/tmp/project %p/cci_ssh.py'):
            command = cci_ssh.connection_command(defaults, '192.0.2.1', 22)
        args = shlex.split(command)
        result = subprocess.run(['ssh', '-G', '-F', '/dev/null', *args[1:]], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('%%h', result.stdout)
        self.assertIn('project %%p', result.stdout)

    @unittest.skipUnless(shutil.which('ncat'), 'Ncat is optional for unit tests')
    def test_real_ncat_socks5_auth_and_bidirectional_relay(self):
        def recv_exact(conn, count):
            result = b''
            while len(result) < count:
                data = conn.recv(count - len(result))
                if not data:
                    raise AssertionError('unexpected EOF')
                result += data
            return result

        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            listener.listen(1)
            listener.settimeout(8)
            proxy_port = listener.getsockname()[1]

            def serve():
                with listener.accept()[0] as conn:
                    conn.settimeout(8)
                    version, count = recv_exact(conn, 2)
                    self.assertEqual(version, 5)
                    self.assertIn(2, recv_exact(conn, count))
                    conn.sendall(b'\x05\x02')
                    version, length = recv_exact(conn, 2)
                    self.assertEqual(version, 1)
                    self.assertEqual(recv_exact(conn, length), b'user')
                    length = recv_exact(conn, 1)[0]
                    self.assertEqual(recv_exact(conn, length), b"test%h ' $()")
                    conn.sendall(b'\x01\x00')
                    version, cmd, _, kind = recv_exact(conn, 4)
                    self.assertEqual((version, cmd), (5, 1))
                    if kind == 1:
                        self.assertEqual(socket.inet_ntoa(recv_exact(conn, 4)), '192.0.2.1')
                    elif kind == 3:
                        length = recv_exact(conn, 1)[0]
                        self.assertEqual(recv_exact(conn, length), b'192.0.2.1')
                    else:
                        self.fail('unexpected address type')
                    self.assertEqual(int.from_bytes(recv_exact(conn, 2), 'big'), 22)
                    conn.sendall(b'\x05\x00\x00\x01\x7f\x00\x00\x01\x00\x16')
                    conn.sendall(b'SSH-2.0-loopback-test\r\n')
                    self.assertEqual(recv_exact(conn, 5), b'hello')
                    conn.sendall(b'world')
                    conn.shutdown(socket.SHUT_WR)

            defaults = {'ssh_proxy': {'server': '127.0.0.1', 'port': proxy_port, 'username': 'user', 'password': "test%h ' $()"}}
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(serve)
                args = cci_ssh.ncat_args(defaults, '192.0.2.1', 22)
                # File stdin exercises EOF/half-close behavior, not just a banner probe.
                with tempfile.TemporaryFile() as stream:
                    stream.write(b'hello')
                    stream.seek(0)
                    result = subprocess.run(args, stdin=stream, capture_output=True, timeout=10)
                future.result(timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, b'SSH-2.0-loopback-test\r\nworld')
