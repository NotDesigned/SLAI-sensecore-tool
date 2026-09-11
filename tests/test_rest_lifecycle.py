import unittest
from unittest.mock import Mock, patch
from scripts import acp, cci_api, cli, rest, templates

UID = '11111111-1111-4111-8111-111111111111'
WS = dict(name='ws', region='cn-sh-01', subscription_name='sub', resource_group_name='group', zone='cn-sh-01z')
APP = dict(name='app', uid='original', ownership={'user_id': UID}, state='RUNNING')


class RestLifecycleTests(unittest.TestCase):
    def client(self):
        client = object.__new__(acp.Client)
        client.config = {'account': {}}
        client._workspace = WS
        client.user_id = UID
        return client

    def test_acp_json_keeps_mount_paths_and_nonempty_command(self):
        doc = acp.build_document({'name':'pool','zone':'cn-sh-01e'},
            {'ZONE':'cn-sh-01e','WORKER SPEC':'small'}, 'job', 'registry/app:v1',
            "exec python 'a,b.py'", 'pytorch', 1, 'RESERVED',
            [dict(type='PV_AFS',id='volume',subdir='/user,a:b',mount_path='/data')])
        self.assertEqual(doc['mount'][0]['subdir'], '/user,a:b')
        self.assertEqual(doc['roles'][0]['startup_script'], "exec python 'a,b.py'")
        self.assertEqual(doc['fault_tolerance']['backoff_limit'], 0)

    def test_template_copy_removes_nested_readonly_and_preserves_settings(self):
        source = dict(name='old', uid='old-uid', ownership={'user_id':UID}, state='SUCCEEDED',
            framework='PYTORCH', roles=[dict(name='worker', image_path='registry/app:v1',
            startup_script='exec python train.py', total_replicas=1,
            resource_spec=[dict(name='small', replicas=1, limits={'cpu':'2'}, requests={'cpu':'2'}, description='small')])],
            resource_pool={'name':'pool','display_name':'Pool'}, fault_tolerance={'backoff_limit':3},
            env=[dict(key='VAR',value='a,b')], ssh={'auto_key_setup':True})
        result = acp.copy_document(source, 'copy')
        self.assertNotIn('uid', result)
        self.assertNotIn('ownership', result)
        self.assertEqual(result['roles'][0]['resource_spec'], [{'name':'small'}])
        self.assertEqual(result['fault_tolerance'], source['fault_tolerance'])
        self.assertEqual(source['name'], 'old')
        with self.assertRaisesRegex(cli.ConfigError, '未声明'):
            templates.writable('acp', {'unknown_feature':True})

    def test_uncertain_create_is_not_retried(self):
        client = self.client()
        client.jobs = Mock(return_value=[])
        client.write = Mock(side_effect=cli.ConfigError('写入结果未知'))
        with self.assertRaises(cli.ConfigError):
            client.create('ws', 'job', {'name':'job'})
        client.write.assert_called_once()

    def test_acp_stop_uses_single_name_batch_and_checks_uid(self):
        client = self.client()
        client.owned = Mock(side_effect=[APP, cli.ConfigError('任务身份已变化')])
        client.write = Mock()
        with self.assertRaisesRegex(cli.ConfigError, '身份'):
            client.control('ws', 'app', 'stop', APP)
        args = client.write.call_args.args
        self.assertEqual(args[1], ':batchStop')
        self.assertEqual(args[2]['training_job_names'], ['app'])
        self.assertEqual(args[2]['zone'], WS['zone'])

    def test_acp_delete_checks_disappearance_and_does_not_accept_other_errors(self):
        for status in (404,403):
            client = self.client()
            client.owned = Mock(side_effect=[APP,rest.RestError(status)])
            client.write = Mock()
            if status==404:
                client.control('ws','app','delete',APP)
            else:
                with self.assertRaises(rest.RestError):client.control('ws','app','delete',APP)
            self.assertEqual(client.write.call_args.args[1], ':batchDelete')

    def test_cci_application_and_service_have_separate_saved_request_ids(self):
        plan = cci_api.creation_plan(WS,'app',{'display_name':'app'},'22')
        service = dict(uid='service-uid',selector={'app':'app'},ports=[dict(port=22,target_port=22)])
        with patch.object(cci_api, 'optional', side_effect=[None,None,APP,service]), \
             patch.object(rest,'get_json',return_value={'id':UID}), patch.object(rest,'request_json') as write:
            self.assertEqual(cci_api.create({},plan),APP)
        self.assertEqual(write.call_count,2)
        self.assertIn('/apps?',write.call_args_list[0].args[1])
        self.assertIn('/services?',write.call_args_list[1].args[1])
        self.assertNotEqual(plan['request_ids']['app'],plan['request_ids']['service'])
        self.assertIn(plan['request_ids']['app'],write.call_args_list[0].args[1])

    def test_service_failure_reports_existing_app_without_deleting_or_retrying(self):
        plan=cci_api.creation_plan(WS,'app',{'display_name':'app'},'22')
        with patch.object(cci_api,'optional',side_effect=[None,None,APP]), \
             patch.object(rest,'get_json',return_value={'id':UID}), \
             patch.object(rest,'request_json',side_effect=[{},cli.ConfigError('offline')]) as write:
            with self.assertRaisesRegex(cli.ConfigError,'已创建'):
                cci_api.create({},plan)
        self.assertEqual(write.call_count,2)
        self.assertTrue(all(c.kwargs['method']=='POST' for c in write.call_args_list))

    def test_cci_delete_does_not_delete_replaced_service(self):
        original=dict(uid='service',selector={'app':'app'})
        replacement=dict(original,uid='new-service')
        with patch.object(cci_api,'owned',return_value=APP), \
             patch.object(cci_api,'optional',side_effect=[original,None,replacement]), \
             patch.object(rest,'request_json') as write:
            with self.assertRaisesRegex(cli.ConfigError,'Service 身份变化'):
                cci_api.delete({},WS,'app',APP)
        write.assert_called_once()

    def test_cci_stop_rejects_replacement_after_submission(self):
        with patch.object(cci_api,'owned',return_value=APP), \
             patch.object(cci_api,'optional',return_value=dict(APP,uid='replacement',state='SUSPENDED')), \
             patch.object(rest,'request_json'):
            with self.assertRaisesRegex(cli.ConfigError,'身份已变化'):
                cci_api.stop({},WS,'app',APP)
