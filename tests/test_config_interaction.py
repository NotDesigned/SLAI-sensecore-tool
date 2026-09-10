import contextlib
import getpass
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import cli


class ConfigInteractionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = self.root / 'config.toml'
        for name, value in [('CONFIG', self.path), ('ROOT', self.root)]:
            p = patch.object(cli, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.original = '''# user comment
[paths]
home = "~/custom sco"
data_home = "data"
config = "profiles"
[sco]
access_key_id = "existing-id" # keep this comment
access_key_secret = ""
region = ""
zone = ""
profile = ""
language = ""
[regions]
cnsh01 = "cn-sh-01"
cnsh02 = "cn-sh-02"
cnyc01 = "cn-yc-01"
[custom]
items = ["中文", "a"]
'''
        self.path.write_text(self.original)

    def test_prompts_save_special_characters_preserve_comments_and_permissions(self):
        secret = 'quote" slash\\ and 中文 $()'
        config = cli.load_config()
        with patch('builtins.input', side_effect=['', '2', '', '', 'invalid', 'en-US']):
            with patch.object(getpass, 'getpass', return_value=secret) as hidden:
                with contextlib.redirect_stdout(io.StringIO()) as output:
                    cli.complete_sco_config(config)
        self.assertEqual(hidden.call_count, 1)
        saved = cli.load_config()
        self.assertEqual(saved['sco']['access_key_secret'], secret)
        self.assertEqual(saved['sco']['access_key_id'], 'existing-id')
        self.assertEqual(saved['sco']['region'], 'cnsh02')
        self.assertEqual(saved['sco']['zone'], 'cn-sh-01')
        self.assertEqual(saved['sco']['profile'], 'default')
        self.assertEqual(saved['sco']['language'], 'en-US')
        self.assertEqual(saved['custom']['items'], ['中文', 'a'])
        self.assertIn('# keep this comment', self.path.read_text())
        self.assertNotIn(secret, output.getvalue())
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        with patch('builtins.input', side_effect=AssertionError('must not prompt')):
            cli.complete_sco_config(saved)

    def test_cancel_does_not_save_partial_input(self):
        with patch.object(getpass, 'getpass', return_value='secret'):
            with patch('builtins.input', side_effect=KeyboardInterrupt):
                with self.assertRaises(KeyboardInterrupt):
                    cli.complete_sco_config(cli.load_config())
        self.assertEqual(self.path.read_text(), self.original)

    def test_concurrent_edit_is_not_overwritten(self):
        original = cli.load_config()['sco']
        changed = self.original.replace('region = ""', 'region = "edited"')
        self.path.write_text(changed)
        with self.assertRaises(cli.ConfigError):
            cli.save_sco_updates({'region': 'new'}, original)
        self.assertEqual(self.path.read_text(), changed)

    def test_secret_cannot_fallback_to_echoed_input(self):
        with patch.object(getpass, 'getpass', side_effect=getpass.GetPassWarning):
            with self.assertRaises(cli.ConfigError):
                cli.complete_sco_config(cli.load_config())
        self.assertEqual(self.path.read_text(), self.original)

    def test_missing_file_can_be_created_from_template(self):
        self.path.unlink()
        (self.root / 'config.example.toml').write_text(self.original)
        config = cli.load_config(for_init=True)
        config.setdefault('regions', {'cnsh01': 'cn-sh-01'})
        with patch.object(getpass, 'getpass', return_value='secret'):
            with patch('builtins.input', side_effect=['1', '', '', '']):
                with contextlib.redirect_stdout(io.StringIO()):
                    cli.complete_sco_config(config)
        self.assertTrue(self.path.exists())
        self.assertEqual(cli.load_config()['sco']['region'], 'cnsh01')

    def test_missing_sco_table_is_added(self):
        self.path.write_text('[paths]\nhome="home"\ndata_home="data"\nconfig="config"\n')
        config = cli.load_config(for_init=True)
        config.setdefault('regions', {'cnsh01': 'cn-sh-01'})
        with patch.object(getpass, 'getpass', return_value='secret'):
            with patch('builtins.input', side_effect=['id', '1', '', '', '']):
                with contextlib.redirect_stdout(io.StringIO()):
                    cli.complete_sco_config(config)
        self.assertEqual(cli.load_config()['sco']['access_key_id'], 'id')

    def test_region_selection_rejects_invalid_numbers(self):
        with patch('builtins.input', side_effect=['4', 'cnsh01', 'abc', '3']):
            with contextlib.redirect_stdout(io.StringIO()) as output:
                selected = cli.choose_region(cli.load_config())
        self.assertEqual(selected, 'cnyc01')
        self.assertIn('1. cn-sh-01 (cnsh01)', output.getvalue())
        self.assertIn('3. cn-yc-01 (cnyc01)', output.getvalue())
