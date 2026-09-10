import contextlib
import importlib.util
import io
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'cli.py'
spec = importlib.util.spec_from_file_location('sco_tool', SCRIPT)
sco = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sco)


class ScoTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.config = {
            'paths': {'home': 'sco home', 'data_home': 'data', 'config': 'profiles'},
            'sco': {'access_key_id': 'test-id', 'access_key_secret': 'secret $() ; with spaces',
                    'region': 'test-region', 'zone': 'cn-sh-01', 'profile': 'default', 'language': 'zh-CN'},
        }
        self.patch_root = patch.object(sco, 'ROOT', self.root)
        self.patch_root.start()
        self.addCleanup(self.patch_root.stop)

    def test_paths_override_environment_and_resolve_from_repo(self):
        with patch.dict(os.environ, {'SCO_HOME': '/unrelated'}):
            env, executable = sco.runtime(self.config)
        self.assertEqual(env['SCO_HOME'], str(self.root / 'sco home'))
        self.assertEqual(env['SCO_CONFIG'], str(self.root / 'profiles'))
        self.assertEqual(env['SCO_DATA_HOME'], str(self.root / 'data'))
        self.assertEqual(executable.parent, self.root / 'sco home' / 'bin')

    def test_init_argv_and_optional_fields(self):
        for zone, profile in [('cn-sh-01', 'default'), ('zone a', 'named profile')]:
            self.config['sco'].update(zone=zone, profile=profile)
            with patch.object(Path, 'is_file', return_value=True), patch.object(sco, 'run') as run:
                with contextlib.redirect_stdout(io.StringIO()) as output:
                    sco.initialize(self.config)
            argv, env = run.call_args_list[0].args
            self.assertEqual(argv[1], 'init')
            self.assertEqual(argv[argv.index('--access-key-secret') + 1], 'secret $() ; with spaces')
            self.assertEqual(argv[argv.index('--zone') + 1], zone or 'cn-sh-01')
            self.assertEqual('--profile' in argv, bool(profile))
            self.assertNotIn('secret', output.getvalue())

    def test_missing_required_prompts_before_execution(self):
        self.config['sco']['region'] = ''
        self.config['regions'] = {'cnsh01': 'cn-sh-01'}
        with patch('builtins.input', side_effect=EOFError), patch.object(sco, 'run') as run:
            with self.assertRaises(EOFError):
                sco.initialize(self.config)
        run.assert_not_called()

    def test_bad_language_and_type(self):
        for language in ('xx', 123):
            self.config['sco']['language'] = language
            with self.assertRaises(sco.ConfigError):
                sco.initialize(self.config)

    def test_missing_executable(self):
        with self.assertRaisesRegex(sco.ConfigError, '找不到 SCO'):
            sco.initialize(self.config)

    def test_parse_errors_do_not_expose_secret(self):
        path = self.root / 'config.toml'
        path.write_text('[sco]\naccess_key_secret = TOPSECRET\n')
        with patch.object(sco, 'CONFIG', path), self.assertRaises(sco.ConfigError) as error:
            sco.load_config()
        self.assertNotIn('TOPSECRET', str(error.exception))

    def test_missing_config(self):
        with patch.object(sco, 'CONFIG', self.root / 'absent.toml'):
            with self.assertRaisesRegex(sco.ConfigError, 'config.example.toml'):
                sco.load_config()

    def test_process_error_does_not_include_argv(self):
        with patch.object(subprocess, 'run', return_value=subprocess.CompletedProcess([], 9)):
            with self.assertRaises(sco.ConfigError) as error:
                sco.run(['sco', '--access-key-secret', 'TOPSECRET'], {})
        self.assertNotIn('TOPSECRET', str(error.exception))
        self.assertIn('9', str(error.exception))

    @unittest.skipIf(os.name == 'nt', 'POSIX installer path')
    def test_install_uses_cache_and_cleans_temporary_shim(self):
        profiles = self.root / 'profiles' / 'profiles'
        profiles.mkdir(parents=True)
        (profiles / 'default.toml').write_text('')
        installer = self.root / 'cached-installer'
        with patch.object(sco, 'fetch', return_value=installer) as fetch:
            with patch.object(sco, 'run') as run, patch.object(sco.shutil, 'which', return_value='/mock/curl'):
                with patch('scripts.shell_env.configure', return_value=[]), contextlib.redirect_stdout(io.StringIO()):
                    sco.install(self.config)
        self.assertEqual(run.call_count, 2)
        self.assertEqual(run.call_args_list[0].args[0], ['bash', str(installer)])
        self.assertEqual(run.call_args_list[1].args[0][1], 'version')
        env = run.call_args_list[0].args[1]
        self.assertEqual(env['SLAI_DOWNLOAD_CACHE'], str(self.root / 'vendor/sco'))
        self.assertFalse(Path(env['PATH'].split(os.pathsep)[0]).exists())
        self.assertEqual(fetch.call_count, 1)

    @unittest.skipIf(os.name == 'nt', 'POSIX installer path')
    def test_failed_download_stops_install(self):
        with patch.object(sco.shutil, 'which', return_value='/mock/curl'):
            with patch.object(sco, 'fetch', side_effect=RuntimeError('download failed')):
                with patch.object(sco, 'run') as run:
                    with self.assertRaises(sco.ConfigError):
                        sco.install(self.config)
        run.assert_not_called()

    def test_fresh_install_defers_eip_until_init(self):
        with patch.object(sco, 'fetch', return_value=self.root / 'installer'), patch.object(sco, 'run') as run:
            with patch.object(sco.shutil, 'which', return_value='/curl'), patch('scripts.shell_env.configure', return_value=[]):
                with contextlib.redirect_stdout(io.StringIO()) as output:
                    sco.install(self.config)
        self.assertEqual(run.call_count, 2)
        self.assertIn('SCO CLI 安装完成', output.getvalue())
        self.assertNotIn('均已安装', output.getvalue())

    def test_eip_runs_only_after_successful_init(self):
        with patch.object(Path, 'is_file', return_value=True), contextlib.redirect_stdout(io.StringIO()):
            with patch.object(sco, 'run') as run:
                sco.initialize(self.config)
            self.assertEqual(run.call_args_list[0].args[0][1], 'init')
            self.assertEqual(run.call_args_list[1].args[0][-3:], ['components', 'install', 'eip'])
            self.assertEqual(run.call_args_list[2].args[0][-3:], ['components', 'install', 'ccr'])
            with patch.object(sco, 'run', side_effect=sco.ConfigError('init failed')) as run:
                with self.assertRaises(sco.ConfigError):
                    sco.initialize(self.config)
            self.assertEqual(run.call_count, 1)

    def test_unsupported_platform_stops_before_install(self):
        with patch.object(sco.sys, 'platform', 'freebsd'), patch.object(sco, 'fetch') as fetch:
            with self.assertRaisesRegex(sco.ConfigError, 'Linux 和 macOS'):
                sco.install(self.config)
        fetch.assert_not_called()

    def test_missing_zone_uses_default(self):
        del self.config['sco']['zone']
        def save(updates, original):
            return {**self.config, 'sco': {**original, **updates}}
        with patch('builtins.input', return_value=''), patch.object(sco, 'save_sco_updates', side_effect=save):
            with patch.object(Path, 'is_file', return_value=True), patch.object(sco, 'run') as run:
                with contextlib.redirect_stdout(io.StringIO()):
                    sco.initialize(self.config)
        argv = run.call_args_list[0].args[0]
        self.assertEqual(argv[argv.index('--zone') + 1], 'cn-sh-01')

    def test_uninstall_requires_confirmation_after_preview(self):
        for answer, expected_calls in [('no', 1), ('yes', 2)]:
            with patch.object(Path, 'is_file', return_value=True), patch.object(sco, 'run') as run:
                with patch('builtins.input', return_value=answer), contextlib.redirect_stdout(io.StringIO()):
                    sco.uninstall(self.config)
            self.assertEqual(run.call_count, expected_calls)
            self.assertEqual(run.call_args_list[0].args[0][1:], ['uninstall', '--dry-run'])
            if answer == 'yes':
                self.assertEqual(run.call_args_list[1].args[0][1:], ['uninstall', '--yes'])

    def test_failed_uninstall_preview_never_deletes(self):
        with patch.object(Path, 'is_file', return_value=True):
            with patch.object(sco, 'run', side_effect=sco.ConfigError('preview failed')) as run:
                with patch('builtins.input') as prompt, contextlib.redirect_stdout(io.StringIO()):
                    with self.assertRaises(sco.ConfigError):
                        sco.uninstall(self.config)
        self.assertEqual(run.call_count, 1)
        prompt.assert_not_called()

    def test_menu_dispatch_and_invalid_input(self):
        with patch('builtins.input', side_effect=['bad', '1', '2', '3', '4', '0']):
            with patch.object(sco, 'execute') as execute, contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(sco.menu(), 0)
        self.assertEqual([call.args[0] for call in execute.call_args_list], ['install', 'uninstall', 'ccr', 'cci'])

    def test_menu_recovers_after_operation_error_and_reloads_config(self):
        with patch('builtins.input', side_effect=['1', '1', '0']):
            with patch.object(sco, 'load_config', return_value=self.config) as load:
                with patch.object(sco, 'install_and_configure', side_effect=[sco.ConfigError('failed'), None]):
                    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                        self.assertEqual(sco.menu(), 0)
        self.assertEqual(load.call_count, 2)

    def test_menu_eof_exits_without_action(self):
        with patch.object(sco.sys, 'argv', ['main.py']), patch('builtins.input', side_effect=EOFError):
            with patch.object(sco, 'execute') as execute, contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(sco.main(), 130)
        execute.assert_not_called()

    def test_combined_install_initializes_after_install_and_stops_on_failure(self):
        events = []
        with patch.object(sco, 'install', side_effect=lambda config: events.append('install')), \
             patch.object(sco, 'initialize', side_effect=lambda config: events.append('init')), \
             contextlib.redirect_stdout(io.StringIO()):
            sco.install_and_configure(self.config)
        self.assertEqual(events, ['install', 'init'])
        with patch.object(sco, 'install', side_effect=sco.ConfigError('failed')), \
             patch.object(sco, 'initialize') as initialize:
            with self.assertRaises(sco.ConfigError):
                sco.install_and_configure(self.config)
        initialize.assert_not_called()

    def test_install_entry_allows_missing_config_and_uses_combined_flow(self):
        with patch.object(sco, 'load_config', return_value=self.config) as load, \
             patch.object(sco, 'install_and_configure') as setup:
            self.assertEqual(sco.execute('install'), 0)
        load.assert_called_once_with(for_init=True)
        setup.assert_called_once_with(self.config)


if __name__ == '__main__':
    unittest.main()
