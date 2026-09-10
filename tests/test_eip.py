from pathlib import Path
import subprocess
import tempfile
import tomllib
import unittest
from unittest.mock import patch

from scripts import cli, eip


class EipTests(unittest.TestCase):
    def test_temporary_zone_profile_preserves_original_and_forwards_args(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'profiles').mkdir()
            source = root / 'profiles' / 'custom.toml'
            text = 'access_key_secret="SECRET"\n[default]\nregion="cn-sh-01"\n[regions.cn-sh-01]\nzone="cn-sh-01"\n'
            source.write_text(text)
            config = {'sco': {'profile': 'custom', 'region': 'cnsh01'}, 'regions': {'cnsh01': 'cn-sh-01'}}
            def run(command, **kwargs):
                temporary = Path(kwargs['env']['SCO_CONFIG'])
                self.assertNotEqual(temporary, root)
                target = temporary / 'profiles/custom.toml'
                data = tomllib.loads(target.read_text())
                self.assertEqual(data['regions']['cn-sh-01']['zone'], 'cn-sh-01e')
                self.assertEqual(data['access_key_secret'], 'SECRET')
                self.assertEqual(target.stat().st_mode & 0o777, 0o600)
                self.assertNotIn('--zone', command)
                self.assertNotIn('SECRET', str(command))
                self.assertEqual(command[-4:], ['eip', 'dnat', 'list', 'example'])
                self.temp_path = temporary
                return subprocess.CompletedProcess(command, 7)
            with patch.object(cli, 'load_config', return_value=config), patch.object(cli, 'runtime', return_value=({'SCO_CONFIG': directory}, Path('/sco'))):
                with patch.object(subprocess, 'run', side_effect=run):
                    self.assertEqual(eip.main(['--zone', 'cn-sh-01e', 'dnat', 'list', 'example']), 7)
            self.assertEqual(source.read_text(), text)
            self.assertFalse(self.temp_path.exists())
