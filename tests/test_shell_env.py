import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from scripts import shell_env


class ShellEnvTests(unittest.TestCase):
    def test_preserves_content_quotes_paths_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / '.bash_profile').write_text('# my settings\nexport EXISTING=yes\n')
            env = {key: str(home / "space ' $(echo BAD)" / key) for key in ('SCO_HOME', 'SCO_DATA_HOME', 'SCO_CONFIG')}
            with patch.object(Path, 'home', return_value=home), patch.dict(os.environ, {'ZDOTDIR': str(home / 'zsh')}):
                paths = shell_env.configure(env)
                first = [p.read_text() for p in paths]
                shell_env.configure(env)
                self.assertEqual(first, [p.read_text() for p in paths])
            self.assertTrue((home / '.bash_profile').read_text().startswith('# my settings\nexport EXISTING=yes\n'))
            for shell in ('bash', 'zsh'):
                executable = shutil.which(shell)
                if not executable:
                    continue
                file = home / '.bashrc' if shell == 'bash' else home / 'zsh/.zshrc'
                result = subprocess.run([executable, '-c', '. "$1"; . "$1"; printf "%s\\n%s" "$SCO_HOME" "$PATH"', 'check', str(file)],
                                        env={'PATH': '/usr/bin:/bin', 'HOME': str(home)}, text=True, capture_output=True, check=True)
                actual, path = result.stdout.split('\n', 1)
                self.assertEqual(actual, env['SCO_HOME'])
                self.assertEqual(path.split(':').count(env['SCO_HOME'] + '/bin'), 1)

    def test_bash_login_precedence_and_broken_marker(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / '.profile').write_text('# login\n')
            env = {key: '/tmp/sco' for key in ('SCO_HOME', 'SCO_DATA_HOME', 'SCO_CONFIG')}
            with patch.object(Path, 'home', return_value=home), patch.dict(os.environ, {'ZDOTDIR': str(home)}):
                shell_env.configure(env)
                self.assertFalse((home / '.bash_profile').exists())
                self.assertIn(shell_env.START, (home / '.profile').read_text())
                (home / '.bashrc').write_text(shell_env.START)
                with self.assertRaises(ValueError):
                    shell_env.configure(env)
