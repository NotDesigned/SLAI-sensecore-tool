import contextlib
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from scripts import docker_registry as registry


class DockerRegistryTests(unittest.TestCase):
    def setUp(self):
        self.settings = {'registry': 'registry.cn-sh-01.sensecore.cn', 'namespace': 'project',
                         'source_image': 'local:v1', 'image_name': 'app', 'tag': 'v1'}
        self.config = {'docker': self.settings}

    def test_always_prompts_for_upload_details(self):
        with patch('builtins.input', side_effect=['', '', '', '']) as prompt:
            with patch.object(registry, 'save_config_updates') as save:
                self.assertEqual(registry.complete_config(self.config), self.settings)
        self.assertEqual(prompt.call_count, 4)
        save.assert_not_called()

    def test_new_details_are_saved(self):
        def save(section, updates, original):
            return {'docker': {**original, **updates}}
        with patch('builtins.input', side_effect=['other', 'other:v2', 'next', 'v2']):
            with patch.object(registry, 'save_config_updates', side_effect=save) as save_call:
                result = registry.complete_config(self.config)
        self.assertEqual(result['namespace'], 'other')
        self.assertEqual(save_call.call_count, 1)

    def flow(self, saved, push_results):
        with patch.object(registry.shutil, 'which', return_value='/docker'):
            with patch.object(registry, 'complete_config', return_value=self.settings):
                with patch.object(registry, 'has_credentials', return_value=saved):
                    with patch.object(subprocess, 'run', return_value=subprocess.CompletedProcess([], 0)):
                        with patch.object(registry, 'login') as login:
                            with patch.object(registry, 'run_push', side_effect=push_results) as push:
                                with contextlib.redirect_stdout(io.StringIO()):
                                    registry.push_image(self.config)
        return login.call_count, push.call_count

    def test_saved_credentials_skip_login(self):
        self.assertEqual(self.flow(True, [(0, False)]), (0, 1))

    def test_missing_credentials_login(self):
        self.assertEqual(self.flow(False, [(0, False)]), (1, 1))

    def test_expired_credentials_login_and_retry_once(self):
        self.assertEqual(self.flow(True, [(1, True), (0, False)]), (1, 2))

    def test_non_auth_errors_do_not_request_login(self):
        with self.assertRaises(registry.ConfigError):
            self.flow(True, [(1, False)])

    def test_password_only_sent_via_stdin(self):
        with patch('builtins.input', return_value='user'):
            with patch.object(registry.getpass, 'getpass', return_value='secret'):
                with patch.object(subprocess, 'run', return_value=subprocess.CompletedProcess([], 0)) as run:
                    registry.login('/docker', self.settings, {})
        self.assertEqual(run.call_args.kwargs['input'], 'secret\n')
        self.assertNotIn('secret', str(run.call_args.args))

    def test_credentials_are_registry_specific(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'config.json'
            path.write_text(json.dumps({'auths': {'https://registry.cn-sh-01.sensecore.cn/v1/': {'auth': 'encoded'}}}))
            self.assertTrue(registry.has_credentials(self.settings['registry'], {'DOCKER_CONFIG': tmp}))
            self.assertFalse(registry.has_credentials('different.example', {'DOCKER_CONFIG': tmp}))
            path.write_text(json.dumps({'auths': {self.settings['registry']: {}}}))
            self.assertFalse(registry.has_credentials(self.settings['registry'], {'DOCKER_CONFIG': tmp}))

    def test_specific_helper_overrides_global_store(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp)/'config.json').write_text(json.dumps({'credsStore': 'global', 'credHelpers': {self.settings['registry']: 'specific'}}))
            with patch.object(registry.shutil, 'which', return_value='/helper') as which:
                with patch.object(subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, '{"Username":"u","Secret":"s"}')):
                    self.assertTrue(registry.has_credentials(self.settings['registry'], {'DOCKER_CONFIG':tmp}))
            which.assert_called_once_with('docker-credential-specific')

    def test_bad_destination_rejected(self):
        for key, value in [('registry', 'https://example.com/path'), ('namespace', '../oops'),
                           ('image_name', 'INVALID'), ('tag', 'bad tag'), ('source_image', '--help')]:
            with self.assertRaises(registry.ConfigError):
                registry.validate({**self.settings, key: value})

    def test_missing_docker_does_not_prompt(self):
        with patch.object(registry.shutil, 'which', return_value=None), patch.object(registry, 'complete_config') as prompt:
            with self.assertRaises(registry.ConfigError):
                registry.push_image(self.config)
        prompt.assert_not_called()
