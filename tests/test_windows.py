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

from scripts import cli, cci_ssh, commands, ncat_proxy, windows


class WindowsTests(unittest.TestCase):
    def config(self, root):
        return {'paths': {'home': str(root / 'SCO Home'), 'data_home': str(root / 'Data'),
                          'config': str(root / 'Config')}, 'sco': {}}

    def test_executable_uses_exe_and_paths_with_spaces(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(sys, 'platform', 'win32'):
            env, executable = cli.runtime(self.config(Path(directory)))
        self.assertEqual(executable.name, 'sco.exe')
        self.assertIn('SCO Home', env['SCO_HOME'])

    def test_path_is_idempotent_and_keeps_existing_entries(self):
        existing = r'%USERPROFILE%\bin;C:\Tools;C:\Users\Person\SCO\bin'
        self.assertEqual(windows.updated_path(existing, r'c:\users\person\sco\bin'), existing)
        updated = windows.updated_path(r'C:\Tools', r'C:\SCO Home\bin')
        self.assertEqual(updated, r'C:\Tools;C:\SCO Home\bin')
        self.assertEqual(windows.updated_path(updated, r'C:\SCO Home\bin'), updated)

    def test_installer_uses_official_powershell_and_persists_after_success(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(sys, 'platform', 'win32'), \
                 patch.dict(os.environ, {'PROCESSOR_ARCHITECTURE': 'AMD64', 'PROCESSOR_ARCHITEW6432': ''}), \
                 patch.object(shutil, 'which', side_effect=lambda name: name), \
                 patch('scripts.download_cache.fetch', return_value=root / "installer's file"), \
                 patch.object(cli, 'run') as run, patch.object(windows, 'configure_environment') as configure, \
                 contextlib.redirect_stdout(io.StringIO()):
                cli.install(self.config(root))
            self.assertEqual(run.call_count, 2)
            self.assertEqual(run.call_args_list[0].args[0][0], 'powershell.exe')
            self.assertIn('SLAI_WINDOWS_INSTALLER', run.call_args_list[0].args[1])
            self.assertNotIn('Bypass', ' '.join(run.call_args_list[0].args[0]))
            self.assertEqual(Path(run.call_args_list[1].args[0][0]).name, 'sco.exe')
            configure.assert_called_once()

    def test_failed_install_does_not_persist_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {'PROCESSOR_ARCHITECTURE': 'AMD64', 'PROCESSOR_ARCHITEW6432': ''}), \
                 patch.object(shutil, 'which', return_value='tool'), \
                 patch('scripts.download_cache.fetch', return_value=Path(directory) / 'installer'), \
                 patch.object(cli, 'run', side_effect=cli.ConfigError('failed')), \
                 patch.object(windows, 'configure_environment') as configure:
                with self.assertRaises(cli.ConfigError):
                    windows.install(self.config(Path(directory)))
            configure.assert_not_called()

    def test_arm64_is_not_silently_installed_as_amd64(self):
        with patch.dict(os.environ, {'PROCESSOR_ARCHITECTURE': 'ARM64', 'PROCESSOR_ARCHITEW6432': ''}):
            with self.assertRaises(cli.ConfigError):
                windows.install({})

    def test_proxy_output_is_powershell_and_hides_credentials(self):
        config = {'ssh_proxy': {'server': '192.0.2.2', 'username': 'secret-user', 'password': 'secret-pass'}}
        with patch.object(sys, 'platform', 'win32'), patch.object(sys, 'executable', r'C:\Program Files\Python\python.exe'):
            command = cci_ssh.connection_command(config, '192.0.2.1', 39587)
        self.assertTrue(command.startswith('ssh '))
        self.assertIn('ProxyCommand="C:\\Program Files', command)
        self.assertNotIn('secret-user', command)
        self.assertNotIn('secret-pass', command)

    def test_proxy_waits_for_ncat_on_windows(self):
        with patch.object(sys, 'platform', 'win32'), patch.object(sys, 'argv', ['proxy', '192.0.2.1', '22']), \
             patch.object(cci_ssh, 'find_ncat', return_value='ncat.exe'), \
             patch.object(cli, 'load_config', return_value={'cci': {'ssh_proxy': {'server': '192.0.2.2'}}}), \
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

    def test_environment_writes_only_current_user_and_preserves_path(self):
        import ctypes
        key = Mock()
        context = Mock()
        context.__enter__ = Mock(return_value=key)
        context.__exit__ = Mock(return_value=False)
        registry = Mock(HKEY_CURRENT_USER='HKCU', REG_SZ=1, REG_EXPAND_SZ=2)
        registry.CreateKey.return_value = context
        registry.QueryValueEx.return_value = (r'%USERPROFILE%\Tools', 2)
        env = {'SCO_HOME': r'C:\SCO', 'SCO_DATA_HOME': r'C:\Data', 'SCO_CONFIG': r'C:\Config'}
        with patch.dict(sys.modules, {'winreg': registry}), patch.object(ctypes, 'windll', Mock(), create=True):
            windows.configure_environment(env)
        registry.CreateKey.assert_called_once_with('HKCU', 'Environment')
        calls = registry.SetValueEx.call_args_list
        self.assertEqual([c.args[1] for c in calls], ['SCO_HOME', 'SCO_DATA_HOME', 'SCO_CONFIG', 'Path'])
        self.assertTrue(calls[-1].args[-1].startswith(r'%USERPROFILE%\Tools;'))
        self.assertEqual(calls[-1].args[-2], 2)

    @unittest.skipUnless(sys.platform == 'win32' and shutil.which('pwsh') and shutil.which('ssh'), 'Requires native Windows OpenSSH and PowerShell 7')
    def test_native_ssh_parses_generated_proxy(self):
        defaults = {'ssh_proxy': {'server': '192.0.2.2'}}
        command = cci_ssh.connection_command(defaults, '192.0.2.1', 39587)
        command = command.replace('ssh ', 'ssh -G -F NUL ', 1)
        result = subprocess.run(['pwsh', '-NoProfile', '-NonInteractive', '-Command', command],
                                capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('port 39587', result.stdout)
        self.assertIn('ncat_proxy.py', result.stdout)
