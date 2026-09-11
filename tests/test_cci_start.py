import unittest
from unittest.mock import patch
from scripts import cci_api, cci_service, cli, rest, ui


class CciStartTests(unittest.TestCase):
    def setUp(self):
        self.ws = dict(name='ws', region='cn-sh-01', zone='cn-sh-01e', subscription_name='sub', resource_group_name='default')
        self.app = dict(name='test', uid='same-uid', ownership={'user_id': 'me'}, state='SUSPENDED')

    def test_start_retains_identity_and_waits_for_state_transition(self):
        running = {**self.app, 'state': 'PROGRESSING'}
        with patch.object(cci_api, 'owned', return_value=self.app), \
             patch.object(cci_api, 'optional', side_effect=[self.app, running]), \
             patch.object(cci_api.time, 'sleep'), patch.object(rest, 'request_json') as request:
            self.assertEqual(cci_api.start({}, self.ws, 'test', self.app), running)
        request.assert_called_once_with({}, cci_api.resource_url(self.ws, 'test') + ':start', method='POST', body={}, timeout=90)

    def test_already_running_does_not_submit_again(self):
        running = {**self.app, 'state': 'RUNNING'}
        with patch.object(cci_api, 'owned', return_value=running), patch.object(rest, 'request_json') as request:
            self.assertEqual(cci_api.start({}, self.ws, 'test', self.app), running)
        request.assert_not_called()

    def test_changed_identity_and_invalid_state_do_not_write(self):
        for change in ({'uid': 'replacement'}, {'state': 'PROGRESSING'}):
            with patch.object(cci_api, 'owned', return_value={**self.app, **change}), patch.object(rest, 'request_json') as request:
                with self.assertRaises(cli.ConfigError):
                    cci_api.start({}, self.ws, 'test', self.app)
            request.assert_not_called()

    def test_failure_after_submission_is_reported(self):
        with patch.object(cci_api, 'owned', return_value=self.app), \
             patch.object(cci_api, 'optional', return_value={**self.app, 'state': 'FAILED'}), \
             patch.object(rest, 'request_json') as request:
            with self.assertRaisesRegex(cli.ConfigError, '状态异常'):
                cci_api.start({}, self.ws, 'test', self.app)
        request.assert_called_once()

    def test_menu_starts_existing_cci_without_copying(self):
        with patch.object(ui, 'browse', side_effect=lambda title, fetch, label, selected, **kwargs: selected(self.app)), \
             patch.object(ui, 'choose', return_value='启动') as choose, \
             patch.object(cci_service, 'start_app') as start, patch.object(cci_service, 'copy_app') as copy:
            cci_service.list_page({}, self.ws)
        self.assertIn('启动', choose.call_args.args[1])
        self.assertNotIn('停止', choose.call_args.args[1])
        start.assert_called_once_with({}, self.ws, 'test', expected=self.app)
        copy.assert_not_called()
