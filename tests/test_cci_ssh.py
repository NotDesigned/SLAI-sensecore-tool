import contextlib
import io
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from scripts import cci, cci_ssh, cli


class SshTests(unittest.TestCase):
    def test_default_and_invalid_flag(self):
        self.assertTrue(cci_ssh.enabled({}))
        self.assertFalse(cci_ssh.enabled({'ssh_enabled': False}))
        with self.assertRaises(cli.ConfigError):
            cci_ssh.enabled({'ssh_enabled': 'false'})

    def test_bootstrap_shell_syntax_and_public_key_only_config(self):
        for command in ('', 'sleep infinity', 'echo "custom"; sleep infinity'):
            script = cci_ssh.startup('ssh-ed25519 AAAA comment', command)
            result = subprocess.run(['sh', '-n'], input=script, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('PasswordAuthentication no', script)
            self.assertIn('AuthenticationMethods publickey', script)
            self.assertIn('PermitRootLogin prohibit-password', script)
            if command in ('', 'sleep infinity'):
                self.assertIn('exec /usr/sbin/sshd -D', script)
            else:
                self.assertIn('exec /bin/sh -c', script)

    def test_key_validation_with_actual_openssh(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'key'
            subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(path)], check=True)
            with contextlib.redirect_stdout(io.StringIO()):
                key = cci_ssh.public_key({'ssh_public_key': str(path) + '.pub'})
            self.assertTrue(key.startswith('ssh-ed25519 '))
            with self.assertRaises(cli.ConfigError):
                cci_ssh.public_key({'ssh_public_key': str(path)})

    def test_prepare_ssh_skips_command_and_ports_prompts(self):
        client = Mock()
        client.resources.return_value = [{'name': 'ws'}]
        client.clusters.return_value = [{'name': 'pool', 'zone': 'cn-sh-01e', 'properties': {'vpc_id': 'vpc'}}]
        client.specs.return_value = [{'WORKER SPEC': 'cpu', 'ZONE': 'cn-sh-01e', 'VCPU COUNT': '2',
                                     'MEMORY(GIB)': '4', 'CHIP COUNT': '0', 'CHIP MODEL': 'cpu'}]
        with patch.object(cci, 'select_image', return_value=cci.DEFAULT_IMAGE), \
             patch.object(cci, 'select_mounts', return_value=[]), \
             patch.object(cci_ssh, 'public_key', return_value='ssh-ed25519 AAAA'), \
             patch('builtins.input', side_effect=['1', '1', '1', 'test-ssh', '', '']) as prompt, \
             contextlib.redirect_stdout(io.StringIO()):
            _, _, ports, doc = cci.prepare(client, {})
        self.assertEqual(ports, '22')
        self.assertEqual(doc['scheduling']['priority'], 'NORMAL')
        self.assertEqual(prompt.call_count, 6)
        self.assertIn('exec /usr/sbin/sshd -D', doc['template']['containers'][0]['command'][2])
        self.assertFalse(any('启动命令' in call.args[0] or '开放端口' in call.args[0] for call in prompt.call_args_list))

    def test_proxy_command_quoting_and_ssh_percent_expansion(self):
        defaults = {'ssh_proxy': {'server': '192.0.2.2', 'port': 1080,
                                 'username': 'user', 'password': "space ' $() %h"}}
        proxy = cci_ssh.ncat_args(defaults, '192.0.2.1', '39587')
        self.assertEqual(proxy[proxy.index('--proxy-auth') + 1], "user:space ' $() %h")
        self.assertEqual(proxy[-2:], ['192.0.2.1', '39587'])
        self.assertEqual(cci_ssh.connection_command({}, '192.0.2.1', 22), 'ssh -p 22 root@192.0.2.1')

    def test_linux_install_hint_and_single_command_output(self):
        import platform
        import sys
        import shutil
        for distro, expected in [('ubuntu', 'apt install -y ncat'), ('rocky', 'dnf install -y nmap-ncat'), ('arch', 'pacman -S nmap')]:
            with patch.object(sys, 'platform', 'linux'), patch.object(platform, 'freedesktop_os_release', return_value={'ID': distro}):
                self.assertIn(expected, cci_ssh.ncat_install_hint())
        defaults = {'ssh_proxy': {'server': '192.0.2.2', 'username': 'user', 'password': 'test'}}
        with patch.object(shutil, 'which', return_value=None), patch.object(cci_ssh, 'ncat_install_hint', return_value='install ncat'), contextlib.redirect_stdout(io.StringIO()) as output:
            cci_ssh.show_connection(defaults, '192.0.2.1', 39587, 'app')
        text = output.getvalue()
        self.assertIn('install ncat', text)
        self.assertIn('ssh -p 39587', text)
        self.assertIn('ncat_proxy.py', text)
        self.assertNotIn('Host slai-app', text)
        self.assertEqual(text.count('ssh -p 39587'), 1)

    def test_display_hides_credentials_and_proxy_reads_config(self):
        from scripts.ncat_proxy import proxy_args
        defaults = {'ssh_proxy': {'server': '192.0.2.2', 'username': 'private-user', 'password': 'hidden%h $()'}}
        command = cci_ssh.connection_command(defaults, '192.0.2.1', 22)
        self.assertNotIn('private-user', command)
        self.assertNotIn('hidden', command)
        self.assertIn('ncat_proxy.py', command)
        args = proxy_args({'cci': defaults}, '192.0.2.1', '22')
        self.assertEqual(args[args.index('--proxy-auth') + 1], 'private-user:hidden%h $()')
        self.assertEqual(args[-2:], ['192.0.2.1', '22'])

    def test_bad_proxy_config_is_a_user_error(self):
        for proxy in ({'server': 'not-an-ip'}, {'server': '192.0.2.2', 'port': True},
                      {'server': '192.0.2.2', 'username': 'user'},
                      {'server': '192.0.2.2', 'username': 'u', 'password': 'x' * 256}):
            with self.subTest(proxy_keys=list(proxy)), self.assertRaises(cli.ConfigError):
                cci_ssh.connection_command({'ssh_proxy': proxy}, '192.0.2.1', 22)
        with self.assertRaises(cli.ConfigError):
            cci_ssh.connection_command({}, 'bad-target', 22)

    def test_public_key_validation_timeout_is_a_user_error(self):
        with tempfile.TemporaryDirectory() as directory:
            key = Path(directory) / 'key.pub'
            key.write_text('ssh-ed25519 AAAA')
            with patch.object(subprocess, 'run', side_effect=subprocess.TimeoutExpired('ssh-keygen', 10)):
                with self.assertRaises(cli.ConfigError):
                    cci_ssh.public_key({'ssh_public_key': str(key)})
