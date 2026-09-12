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

    def test_selection_refreshes_record_and_copies_only_uri_even_if_failed(self):
        old=dict(name='record',state='CREATING',image_tag='v1',ccr_namespace='ns')
        current=dict(old,state='FAIL',uri='registry.example/ns/image:v1')
        with patch.object(s,'snapshots',return_value=[current]),              patch.object(s.ui,'browse',side_effect=lambda title,fetch,describe,selected,**kw:selected(old)),              patch.object(s.ui,'show_text') as show:
            s.list_page({},self.w,self.app)
        self.assertEqual(show.call_args.args,('快照地址','registry.example/ns/image:v1'))
        self.assertEqual(show.call_args.kwargs['copy_label'],'复制快照地址')
        self.assertIn('保存失败',show.call_args.kwargs['hint'])

    def test_missing_uri_has_no_copy_button(self):
        row=dict(name='record',state='CREATING',ccr_namespace='ns')
        with patch.object(s,'snapshots',return_value=[row]),              patch.object(s.ui,'browse',side_effect=lambda title,fetch,describe,selected,**kw:selected(row)),              patch.object(s.ui,'show_text') as show:
            s.list_page({},self.w,self.app)
        self.assertIsNone(show.call_args.kwargs['copy_label'])


class SnapshotClipboardUiTests(unittest.IsolatedAsyncioTestCase):
    async def test_snapshot_button_copies_uri_instead_of_status(self):
        from scripts import clipboard
        from scripts.tui import SlaiApp,Details
        from textual.widgets import Button
        uri='registry.example/ns/image:v1'
        with patch('scripts.cli.menu_title',return_value='test'),patch('scripts.proxy_settings.status',return_value='test'),patch.object(clipboard,'copy_text') as copy:
            app=SlaiApp()
            async with app.run_test() as pilot:
                await app.push_screen(Details('快照地址',uri,hint='当前状态：保存中',copy_label='复制快照地址'))
                await pilot.pause(.1)
                self.assertEqual(str(app.screen.query_one('#copy',Button).label),'复制快照地址')
                await pilot.click('#copy')
                for _ in range(100):
                    await pilot.pause(.025)
                    if copy.called:
                        break
                copy.assert_called_once_with(uri)
