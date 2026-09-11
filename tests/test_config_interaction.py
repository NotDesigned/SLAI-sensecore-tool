import contextlib
import getpass
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from scripts import cli, rest, ui, cloud

UID='11111111-1111-4111-8111-111111111111'

class ConfigInteractionTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.path=self.root/'config.toml'
        for name,value in [('CONFIG',self.path),('ROOT',self.root)]:
            p=patch.object(cli,name,value);p.start();self.addCleanup(p.stop)
        self.original='# user comment\n[account]\naccess_key_id="existing-id" # keep\naccess_key_secret=""\n[custom]\nitems=["中文","a"]\n'
        self.path.write_text(self.original)
        (self.root/'config.example.toml').write_text('[account]\naccess_key_id=""\naccess_key_secret=""\n')

    def test_validate_before_save_preserves_comments_and_hides_secret(self):
        secret='quote" slash\\ 中文 $()'
        with patch.object(ui,'secret',return_value=secret), patch.object(rest,'get_json',return_value={'id':UID,'username':'example'}), contextlib.redirect_stdout(io.StringIO()) as output:
            cli.configure_account(cli.load_config())
        saved=cli.load_config()
        self.assertEqual(saved['account']['access_key_secret'],secret)
        self.assertEqual(saved['custom']['items'],['中文','a'])
        self.assertIn('# keep',self.path.read_text())
        self.assertNotIn(secret,output.getvalue())
        if cli.sys.platform!='win32':self.assertEqual(self.path.stat().st_mode & 0o777,0o600)

    def test_cancel_or_failed_validation_does_not_save(self):
        with patch.object(ui,'secret',side_effect=ui.Cancelled), patch.object(rest,'get_json') as get:
            with self.assertRaises(ui.Cancelled):cli.configure_account(cli.load_config())
        get.assert_not_called()
        with patch.object(ui,'secret',return_value='secret'), patch.object(rest,'get_json',side_effect=rest.RestError(403)):
            with self.assertRaises(cli.ConfigError):cli.configure_account(cli.load_config())
        self.assertEqual(self.path.read_text(),self.original)

    def test_concurrent_edit_is_not_overwritten(self):
        original=cli.load_config()['account'];changed=self.original.replace('existing-id','another-id');self.path.write_text(changed)
        with self.assertRaises(cli.ConfigError):cli.save_config_updates('account',{'access_key_id':'new'},original)
        self.assertEqual(self.path.read_text(),changed)

    def test_secret_cannot_fall_back_to_echoed_input(self):
        with patch.object(getpass,'getpass',side_effect=getpass.GetPassWarning):
            with self.assertRaises(cli.ConfigError):cli.configure_account(cli.load_config())
        self.assertEqual(self.path.read_text(),self.original)

    def test_missing_file_and_account_table_can_be_configured(self):
        self.path.unlink()
        with patch.object(ui,'ask',return_value='new-id'), patch.object(ui,'secret',return_value='secret'), patch.object(rest,'get_json',return_value={'id':UID}), contextlib.redirect_stdout(io.StringIO()):
            cli.configure_account(cli.load_config(for_setup=True))
        self.assertEqual(cli.load_config()['account']['access_key_id'],'new-id')
        self.path.write_text('[custom]\nvalue="keep"\n')
        self.assertEqual(cli.load_config(for_setup=True)['account'],{})

    def test_rest_client_does_not_require_cli_or_installation_paths(self):
        c=cloud.Client({'account':{'access_key_id':'ak','access_key_secret':'sk'}})
        self.assertFalse(hasattr(c,'executable'))
        self.assertFalse(hasattr(c,'command'))
