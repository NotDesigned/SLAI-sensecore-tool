import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, Mock
from textual.widgets import Button, Input, Static
from scripts import cli, cloud, onboarding, rest, ui, workspace
from scripts.tui import SlaiApp, Details, Edit, Picker, Operation

UID='11111111-1111-4111-8111-111111111111'
WS=dict(name='training',region='cn-sh-01',subscription_name='sub',resource_group_name='group',zone='cn-sh-01z',id='workspace-id')

class FirstRunTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        root=Path(self.tmp.name);self.config=root/'config.toml'
        (root/'config.example.toml').write_text((cli.ROOT/'config.example.toml').read_text())
        for key,value in [('ROOT',root),('CONFIG',self.config)]:
            p=patch.object(cli,key,value);p.start();self.addCleanup(p.stop)

    def test_fresh_state_does_not_write_or_call_network(self):
        with patch.object(rest,'get_json') as request:
            self.assertEqual(onboarding.state()[0],'account')
            self.assertIn('用户：未配置',cli.menu_title({}))
        request.assert_not_called();self.assertFalse(self.config.exists())

    def test_resume_workspace_without_reentering_account(self):
        self.config.write_text('[account]\naccess_key_id="test"\naccess_key_secret="test"\n')
        with patch.object(cli,'configure_account') as account, patch.object(cli,'configure_workspace') as select:
            onboarding.start()
        account.assert_not_called();select.assert_called_once()

    def test_account_can_still_be_changed_before_selecting_workspace(self):
        self.config.write_text('[account]\naccess_key_id="test"\naccess_key_secret="test"\n')
        with patch.object(cli,'configure_account') as account, patch.object(onboarding,'start') as wizard:
            self.assertEqual(cli.execute('configure'),0)
        account.assert_called_once();wizard.assert_not_called()

    def test_bad_config_is_not_overwritten(self):
        self.config.write_text('invalid = [')
        self.assertEqual(onboarding.state()[0],'error')
        with self.assertRaises(cli.ConfigError):onboarding.start()
        self.assertEqual(self.config.read_text(),'invalid = [')

    def test_no_workspace_explains_next_action(self):
        client=Mock(config={});client.resources.return_value=[]
        with self.assertRaisesRegex(cli.ConfigError,'管理员授权'):
            workspace.select(client)

    def test_failed_validation_never_creates_config(self):
        with patch.object(ui,'show_text'), patch.object(ui,'ask',return_value='test'), \
             patch.object(ui,'secret',return_value='fixture-secret'), patch.object(rest,'get_json',side_effect=rest.RestError(403)), \
             patch.object(cli,'configure_workspace') as select:
            with self.assertRaises(cli.ConfigError):onboarding.start()
        self.assertFalse(self.config.exists());select.assert_not_called()

class FirstRunUiTests(unittest.IsolatedAsyncioTestCase):
    async def wait(self,pilot,predicate):
        for _ in range(80):
            await pilot.pause(.025)
            if predicate():return
        self.fail('UI did not reach expected screen')

    async def test_fresh_download_setup_through_actual_widgets(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);config=root/'config.toml'
            (root/'config.example.toml').write_text((cli.ROOT/'config.example.toml').read_text())
            with patch.object(cli,'ROOT',root), patch.object(cli,'CONFIG',config), \
                 patch.object(rest,'get_json',return_value={'id':UID,'username':'new-user'}) as identity, \
                 patch.object(cloud.Client,'resources',return_value=[WS]):
                app=SlaiApp()
                async with app.run_test(size=(80,24)) as pilot:
                    self.assertIn('首次使用 1/2',str(app.query_one('#getting-started',Static).render()))
                    identity.assert_not_called();self.assertFalse(config.exists())
                    await pilot.click('#home-setup')
                    await self.wait(pilot,lambda:isinstance(app.screen,Details))
                    await pilot.press('escape')
                    await self.wait(pilot,lambda:isinstance(app.screen,Edit))
                    app.screen.query_one(Input).value='test-id';await pilot.press('enter')
                    await self.wait(pilot,lambda:isinstance(app.screen,Edit) and app.screen.query_one(Input).password)
                    app.screen.query_one(Input).value='fixture-secret';await pilot.press('enter')
                    await self.wait(pilot,lambda:isinstance(app.screen,Picker))
                    await pilot.press('enter')
                    await self.wait(pilot,lambda:len(app.screen_stack)==1 and not app.query_one('#home-setup',Button).display)
                    self.assertFalse(app.query_one('#home-setup',Button).display)
                    saved=cli.load_config();self.assertEqual(saved['workspace']['name'],'training')
                    self.assertEqual(saved['account']['access_key_id'],'test-id')
                    self.assertEqual(onboarding.state()[0],'ready')
