import unittest
from unittest.mock import patch
from scripts import cci_snapshot as s, cli


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.w = dict(name='ws', region='cn-sh-01', zone='cn-sh-01e', subscription_name='sub', resource_group_name='default')
        self.app = dict(name='app', uid='app-id', ownership={'user_id': 'me'}, state='RUNNING', template={'containers': [{'name': 'main'}]})
        self.instance = dict(name='pod', uid='pod-id', state='RUNNING', container_infos=[
            {'container_name': 'main', 'container_type': 'MAIN', 'container_state': 'RUNNING'},
            {'container_name': 'init', 'container_state': 'RUNNING'}])

    def test_only_running_regular_containers(self):
        stopped = {**self.instance, 'name': 'stopped', 'state': 'STOPPED'}
        with patch.object(s.cci_api, 'owned', return_value=self.app), patch.object(s, 'collection', return_value=[self.instance, stopped]):
            self.assertEqual(s.running({}, self.w, self.app), [(self.instance, 'main')])

    def test_replaced_app_is_rejected(self):
        with patch.object(s.cci_api, 'owned', return_value={**self.app, 'uid': 'replaced'}):
            with self.assertRaises(cli.ConfigError):
                s.running({}, self.w, self.app)

    def submit(self, candidates=None, records=None):
        with patch.object(s, 'running', return_value=candidates if candidates is not None else [(self.instance, 'main')]), \
             patch.object(s.ccr, 'namespaces', return_value=[dict(name='ns', region='cn-sh-01', state='ACTIVE')]), \
             patch.object(s, 'snapshots', return_value=records or []), \
             patch.object(s.rest, 'request_json', return_value={'state': 'CREATING'}) as request:
            result = s.create({}, self.w, self.app, self.instance, 'main', 'ns', 'test-image')
        return result, request

    def test_submit_uses_instance_name_and_no_user_supplied_tag(self):
        result, request = self.submit()
        self.assertEqual(result['state'], 'CREATING')
        self.assertEqual(request.call_args.kwargs['body'], dict(name='test-image', display_name='test-image',
            ccr_namespace='ns', container_name='main', instance_uuid='pod'))
        self.assertTrue(request.call_args.args[1].endswith('/apps/app/snapshots?client_type=0'))
        self.assertEqual(request.call_args.kwargs['method'], 'POST')

    def test_changed_instance_and_duplicate_snapshot_rejected(self):
        with self.assertRaises(cli.ConfigError):
            self.submit(candidates=[({**self.instance, 'uid': 'replacement'}, 'main')])
        with self.assertRaises(cli.ConfigError):
            self.submit(records=[dict(name='test-image', ccr_namespace='ns', state='CREATING')])

    def test_cancel_does_not_create(self):
        with patch.object(s, 'running', return_value=[(self.instance, 'main')]), \
             patch.object(s.ccr, 'select_upload_namespace', return_value='ns'), \
             patch.object(s.ui, 'ask', return_value='test-image'), \
             patch.object(s.ui, 'confirm', return_value=False), patch.object(s, 'create') as create:
            s.create_interactive({}, self.w, self.app)
        create.assert_not_called()

    def test_progress_label_does_not_hide_failure(self):
        row = dict(name='image', state='CREATING', reason='PUSHPENDING: WorkerPodCreated')
        self.assertIn('等待上传', s.label(row))
        self.assertIn('保存失败', s.label({**row, 'state': 'FAIL'}))
