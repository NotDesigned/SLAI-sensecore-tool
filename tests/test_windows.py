"""Windows behavior checks; cloud writes and user registry changes are mocked."""
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch



from scripts import network, cloud, cli, cci_ssh, commands, ncat_proxy

class WindowsTests(unittest.TestCase):






    def test_proxy_output_is_powershell_and_hides_credentials(self):
        config = {'network': {'socks5': {'server': '192.0.2.2', 'username': 'secret-user', 'password': 'secret-pass'}}}
        with patch.object(sys, 'platform', 'win32'), patch.object(sys, 'executable', r'C:\Program Files\Python\python.exe'):
            command = cci_ssh.connection_command(config, '192.0.2.1', 39587)
        self.assertTrue(command.startswith('ssh '))
        self.assertIn('ProxyCommand="C:\\Program Files', command)
        self.assertNotIn('secret-user', command)
        self.assertNotIn('secret-pass', command)

    def test_proxy_rejects_unsafe_windows_paths(self):
        config = {'network': {'socks5': {'server': '192.0.2.2'}}}
        for executable in ('C:/percent%h/python.exe', 'C:/quote"/python.exe', 'C:/line\n/python.exe'):
            with self.subTest(executable=executable), patch.object(sys, 'platform', 'win32'), patch.object(sys, 'executable', executable):
                with self.assertRaises(cli.ConfigError):
                    cci_ssh.connection_command(config, '192.0.2.1', 22)

    def test_proxy_waits_for_ncat_on_windows(self):
        with patch.object(sys, 'platform', 'win32'), patch.object(sys, 'argv', ['proxy', '192.0.2.1', '22']), \
             patch.object(network, 'find_ncat', return_value='ncat.exe'), \
             patch.object(cli, 'load_config', return_value={'cci': {}, 'network': {'socks5': {'server': '192.0.2.2'}}}), \
             patch.object(subprocess, 'run', return_value=Mock(returncode=7)) as run, \
             patch.object(os, 'execv') as execute:
            self.assertEqual(ncat_proxy.main(), 7)
        run.assert_called_once()
        execute.assert_not_called()

    @unittest.skipUnless(sys.platform == 'win32' and shutil.which('pwsh'), 'Requires Windows PowerShell 7')
    def test_native_powershell_roundtrip(self):
        values = ['spaces here', 'nested "quotes"', "apostrophe's", '$HOME; & echo BAD', r'C:\some folder\script.py']
        command = commands.format_command([sys.executable, '-c', 'import json,sys;print(json.dumps(sys.argv[1:]))', *values])
        result = subprocess.run(['pwsh', '-NoProfile', '-NonInteractive', '-Command', command], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), values)


    @unittest.skipUnless(sys.platform == 'win32' and shutil.which('pwsh') and shutil.which('ssh'), 'Requires native Windows OpenSSH and PowerShell 7')
    def test_native_ssh_parses_generated_proxy(self):
        defaults = {'network': {'socks5': {'server': '192.0.2.2'}}}
        command = cci_ssh.connection_command(defaults, '192.0.2.1', 39587)
        command = command.replace('ssh ', 'ssh -G -F NUL ', 1)
        result = subprocess.run(['pwsh', '-NoProfile', '-NonInteractive', '-Command', command],
                                capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('port 39587', result.stdout)
        self.assertIn('ncat_proxy.py', result.stdout)
