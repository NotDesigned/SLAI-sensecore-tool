"""Native client -> project helper -> Ncat -> authenticated SOCKS5 -> SSH fixture."""
import json
import os
from pathlib import Path
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from scripts import cci_ssh, cli, network


def receive(sock, size):
    data = b''
    while len(data) < size:
        part = sock.recv(size - len(data))
        if not part:
            raise EOFError('short SOCKS packet')
        data += part
    return data


@unittest.skipUnless(sys.platform == 'win32' and os.environ.get('SLAI_NATIVE_ACCEPTANCE') == '1', 'Native Windows acceptance runs in its dedicated CI step')
class WindowsProxyIntegration(unittest.TestCase):
    def test_real_ssh_over_authenticated_ncat_with_space_and_unicode_paths(self):
        import paramiko
        for tool in ('pwsh', 'ssh', 'ssh-keygen', 'uv'):
            self.assertIsNotNone(shutil.which(tool), tool + ' is required for acceptance')
        self.assertIsNotNone(network.find_ncat(), 'Ncat is required for acceptance')
        errors, seen = [], []
        finished = threading.Event()
        with tempfile.TemporaryDirectory(prefix='SLAI 中文 space ') as folder:
            root = Path(folder)
            project = root / 'project space'
            shutil.copytree(cli.ROOT / 'scripts', project / 'scripts', ignore=shutil.ignore_patterns('__pycache__'))
            for name in ('pyproject.toml', 'uv.lock'):
                shutil.copy2(cli.ROOT / name, project / name)
            subprocess.run(['uv', 'sync', '--locked', '--project', str(project)], check=True, capture_output=True, timeout=120)
            python = project / '.venv' / 'Scripts' / 'python.exe'
            key = root / 'client-key'
            subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(key)], check=True, capture_output=True)
            client_key = paramiko.Ed25519Key.from_private_key_file(str(key))
            host_key = paramiko.RSAKey.generate(2048)
            ssh_socket, proxy_socket = socket.socket(), socket.socket()
            self.addCleanup(ssh_socket.close)
            self.addCleanup(proxy_socket.close)
            for listener in (ssh_socket, proxy_socket):
                listener.bind(('127.0.0.1', 0))
                listener.listen(1)
                listener.settimeout(30)
            port, proxy_port = ssh_socket.getsockname()[1], proxy_socket.getsockname()[1]
            username, password = 'ci-user', 'fixture quote\' dollar$ percent% space'
            class Server(paramiko.ServerInterface):
                def check_auth_publickey(self, user, public_key):
                    return paramiko.AUTH_SUCCESSFUL if user == 'root' and public_key == client_key else paramiko.AUTH_FAILED
                def get_allowed_auths(self, user):
                    return 'publickey'
                def check_channel_request(self, kind, channel_id):
                    return paramiko.OPEN_SUCCEEDED if kind == 'session' else paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED
                def check_channel_exec_request(self, channel, command):
                    seen.append(command)
                    finished.set()
                    return True
            def server():
                try:
                    connection, _ = ssh_socket.accept()
                    with paramiko.Transport(connection) as transport:
                        transport.add_server_key(host_key)
                        transport.start_server(server=Server())
                        channel = transport.accept(20)
                        if channel is None or not finished.wait(20):
                            raise TimeoutError('SSH exec was not requested')
                        channel.sendall(b'windows-proxy-ok\n')
                        channel.send_exit_status(0)
                        channel.shutdown_write()
                        # Give the client time to consume EOF and close its session.
                        for _ in range(100):
                            if channel.closed:
                                break
                            threading.Event().wait(.05)
                except Exception as error:
                    errors.append(error)
            def proxy():
                try:
                    with proxy_socket.accept()[0] as incoming:
                        incoming.settimeout(20)
                        version, count = receive(incoming, 2)
                        assert version == 5 and 2 in receive(incoming, count)
                        incoming.sendall(b'\x05\x02')
                        assert receive(incoming, 1) == b'\x01'
                        user = receive(incoming, receive(incoming, 1)[0]).decode()
                        secret = receive(incoming, receive(incoming, 1)[0]).decode()
                        assert (user, secret) == (username, password)
                        incoming.sendall(b'\x01\x00')
                        version, command, _, kind = receive(incoming, 4)
                        assert (version, command, kind) == (5, 1, 1)
                        host = socket.inet_ntoa(receive(incoming, 4))
                        target = struct.unpack('!H', receive(incoming, 2))[0]
                        assert (host, target) == ('127.0.0.1', port)
                        with socket.create_connection((host, target), timeout=20) as outgoing:
                            incoming.sendall(b'\x05\x00\x00\x01\x7f\x00\x00\x01' + struct.pack('!H', port))
                            import select
                            while True:
                                ready, _, _ = select.select([incoming, outgoing], [], [], 20)
                                if not ready:
                                    raise TimeoutError('proxy stream idle')
                                for source in ready:
                                    data = source.recv(65536)
                                    if not data:
                                        return
                                    (outgoing if source is incoming else incoming).sendall(data)
                except Exception as error:
                    errors.append(error)
            threads = [threading.Thread(target=fn, daemon=True) for fn in (server, proxy)]
            for thread in threads:
                thread.start()
            config = {'account': {}, 'network': {'socks5': dict(server='127.0.0.1', port=proxy_port, username=username, password=password)}}
            (project / 'config.toml').write_text('[account]\n[network.socks5]\n' + '\n'.join(k + ' = ' + json.dumps(v) for k, v in config['network']['socks5'].items()), encoding='utf-8')
            known = root / 'known_hosts'
            known.write_text(f'[127.0.0.1]:{port} {host_key.get_name()} {host_key.get_base64()}\n', encoding='utf-8')
            from scripts.commands import format_command
            with patch.object(cci_ssh, '__file__', str(project / 'scripts' / 'cci_ssh.py')), patch.object(sys, 'executable', str(python)):
                command = cci_ssh.connection_command(config, '127.0.0.1', port)
            extra = format_command(['ssh', '-F', 'NUL', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
                                    '-o', 'UserKnownHostsFile="' + known.as_posix() + '"', '-o', 'IdentitiesOnly=yes', '-i', str(key)])
            command = extra + command[len('ssh'):] + " 'integration-command'"
            result = subprocess.run(['pwsh', '-NoProfile', '-NonInteractive', '-Command', command], capture_output=True, timeout=45)
            for thread in threads:
                thread.join(timeout=6)
            self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace'))
            self.assertEqual(result.stdout.decode('utf-8').strip(), 'windows-proxy-ok')
            self.assertEqual(seen, [b'integration-command'])
            self.assertFalse(errors, errors)
