import unittest
from unittest.mock import patch
from scripts import cli, rest


class MenuTitleTests(unittest.TestCase):
    def test_account_cached_but_workspace_reloaded(self):
        config={'account':{'access_key_id':'ak','access_key_secret':'sk'},'workspace':{'name':'first'}}
        cache={}
        with patch.object(cli,'load_config',return_value=config), patch.object(rest,'get_json',return_value={'username':'L202500224'}) as get:
            self.assertIn('用户：L202500224 · 工作空间：first',cli.menu_title(cache))
            config['workspace']['name']='second'
            self.assertIn('工作空间：second',cli.menu_title(cache))
            get.assert_called_once()
            config['account']['access_key_id']='other'
            cli.menu_title(cache)
            self.assertEqual(get.call_count,2)

    def test_unconfigured_and_unavailable(self):
        with patch.object(cli,'load_config',return_value={'account':{}}), patch.object(rest,'get_json') as get:
            self.assertIn('用户：未配置 · 工作空间：未选择',cli.menu_title({}))
            get.assert_not_called()
        with patch.object(cli,'load_config',return_value={'account':{'access_key_id':'ak','access_key_secret':'sk'}}), patch.object(rest,'get_json',side_effect=cli.ConfigError('secret')):
            title=cli.menu_title({})
            self.assertIn('暂时无法确认',title)
            self.assertNotIn('secret',title)
