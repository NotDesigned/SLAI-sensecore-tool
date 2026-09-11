import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch



from scripts import cloud, ui, cli, workspace

class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.row = dict(name='ws', region='cn-sh-01', subscription_name='sub',
                        resource_group_name='default', zone='cn-sh-01z', id='id')
        self.client = Mock(config={'workspace':dict(self.row)})
        self.client.resources.return_value = [self.row]

    def test_saved_selection_skips_prompt(self):
        with patch.object(ui,'choose') as choose:
            self.assertEqual(workspace.select(self.client),self.row)
        choose.assert_not_called()

    def test_stale_identity_prompts_instead_of_auto_selecting(self):
        self.client.config['workspace']['id']='old-id'
        with patch.object(ui,'choose',return_value=self.row) as choose:
            workspace.select(self.client)
        choose.assert_called_once()

    def test_explicit_override_does_not_save(self):
        other={**self.row,'name':'other','id':'other'}
        self.client.resources.return_value.append(other)
        with patch.object(cli,'save_config_updates') as save:
            self.assertEqual(workspace.select(self.client,explicit='other'),other)
        save.assert_not_called()

    def test_same_name_different_subscription_not_selected(self):
        self.client.resources.return_value=[{**self.row,'subscription_name':'different'}]
        with patch.object(ui,'choose',return_value=self.row) as choose:
            workspace.select(self.client)
        choose.assert_called_once()

    def test_persistence_and_cancel(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'config.toml'
            path.write_text('[account]\n')
            self.client.config={}
            with patch.object(cli,'CONFIG',path), patch.object(cloud,'Client',return_value=self.client), patch.object(ui,'choose',return_value=self.row):
                workspace.configure({})
                loaded=cli.load_config()
            self.assertEqual(loaded['workspace'],self.row)
            before=path.read_text()
            with patch.object(cli,'CONFIG',path), patch.object(cloud,'Client',return_value=self.client), patch.object(ui,'choose',side_effect=workspace.ui.Cancelled):
                with self.assertRaises(workspace.ui.Cancelled):
                    workspace.configure({})
            self.assertEqual(before,path.read_text())
