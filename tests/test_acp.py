import unittest
from unittest.mock import patch, Mock


from scripts import cloud, ui, acp, cli

class ACPTests(unittest.TestCase):
    def client(self):
        client = object.__new__(acp.Client)
        client.user_id = '019d05a5-ea37-7ec1-95c4-eba56ea2ab04'
        client.config = {}
        client._workspace = dict(name='ws', region='cn-sh-01', subscription_name='sub', resource_group_name='group', zone='cn-sh-01z')
        return client

    def test_empty_and_ownership(self):
        c = self.client()
        with patch.object(acp, 'get_json', return_value={'training_jobs': []}) as get:
            self.assertEqual(c.jobs('ws'), [])
            get.return_value = {'training_jobs': [{'name': 'other', 'ownership': {'user_id': 'other'}}]}
            self.assertEqual(c.jobs('ws'), [])
            get.return_value = ''
            with self.assertRaises(cli.ConfigError):
                c.jobs('ws')

    def test_search_prefix_can_end_with_hyphen(self):
        c=self.client()
        with patch.object(acp,'get_json',return_value={'training_jobs':[]}) as get:
            c.jobs('ws',name='test-')
        self.assertIn('name=test-',get.call_args.args[1])

    def test_repeated_page(self):
        c = self.client()
        with patch.object(acp, 'get_json', return_value={'training_jobs': [{'name': str(i)} for i in range(100)], 'next_page_token': '2'}):
            with self.assertRaises(cli.ConfigError):
                c.jobs('ws')

    def test_owner_and_uid_rechecked(self):
        c=self.client();row={'name':'job','uid':'new','ownership':{'user_id':c.user_id}}
        with patch.object(acp,'get_json',return_value=row):
            with self.assertRaises(cli.ConfigError):c.owned('ws','job',{'uid':'old'})
            row['ownership']['user_id']='other'
            with self.assertRaises(cli.ConfigError):c.owned('ws','job')

    def draft(self):
        from scripts.forms import CreateDraft
        c = Mock(config={})
        draft = CreateDraft('acp', c, {'name':'ws'})
        draft.values.update(cluster={'name':'pool','zone':'cn-sh-01e'},
            spec={'ZONE':'cn-sh-01e','WORKER SPEC':'small'}, mounts=[])
        return draft

    def test_entrypoint_quoting(self):
        import shlex
        draft = self.draft()
        draft.values.update(entrypoint=True, command='"/entry point" "a; b" "$(evil)"')
        value = draft.build()[1]['roles'][0]['startup_script']
        self.assertEqual(shlex.split(value), ['exec','/entry point','a; b','$(evil)'])

    def test_invalid_entrypoint(self):
        for value in ('[]','[1]', '"unclosed', '   '):
            draft = self.draft(); draft.values.update(entrypoint=True, command=value)
            with self.assertRaises(cli.ConfigError):
                draft.build()

    def test_unknown_submission_redacted(self):
        from scripts import rest
        c=self.client()
        with patch.object(rest,'request_json',side_effect=rest.RestError(400,{'message':'PASSWORD'})):
            with self.assertRaises(cli.ConfigError) as caught:c.write('ws','',{})
        self.assertNotIn('PASSWORD',str(caught.exception))

    def test_unsupported_pool_quota_has_actionable_message(self):
        from scripts import rest
        c=self.client();c.jobs=Mock(return_value=[])
        c.write=Mock(side_effect=rest.RestError(400,{'details':[{'reason':'tjInvalidQuotaTypeInAec2'}]}))
        with self.assertRaisesRegex(cli.ConfigError,'切换资源池或配额'):
            c.create('ws','new',{})
        c.write.assert_called_once()

    def test_cancel_delete_no_submission(self):
        c=self.client(); c.owned=Mock(return_value={'uid':'id'}); c.control=Mock()
        with patch.object(ui,'choose',return_value='取消'):
            acp.operate(c,'ws',{'name':'job'},'删除')
        c.control.assert_not_called()

    def test_menu_back(self):
        from scripts import workspace
        with patch.object(cli,'load_config',return_value={}), patch.object(acp,'Client'), \
             patch.object(workspace,'select',return_value={'name':'ws'}), patch.object(acp,'list_page') as listing:
            self.assertEqual(acp.main([]),0)
        self.assertEqual(listing.call_args.args[1],'ws')

    def test_names(self):
        for value in ('--flag','../x','x y','x'*64):
            with self.assertRaises(cli.ConfigError):
                acp.validate_name(value)

    def test_default_spot_and_mount(self):
        draft = self.draft()
        mount={'id':'volume','subdir':'/user','mount_path':'/data'}
        draft.values.update(command='set -eu; python train.py', mounts=[mount])
        name,args=draft.build()
        self.assertEqual(args['scheduling']['quota_type'],'SPOT')
        self.assertEqual(args['scheduling']['priority'],'NORMAL')
        self.assertEqual(args['mount'],[mount])
        self.assertNotIn('sshd',args['roles'][0]['startup_script'])

    def test_proxy_is_opt_in_and_scoped(self):
        c=self.client();c.config={'account':{},'network':{'socks5':{'server':'127.0.0.1','port':1080,'username':'u','password':'p'}}}
        row={'name':'job','uid':'uid','ownership':{'user_id':c.user_id}}
        with patch.object(acp,'get_json',return_value=row) as get:
            c.owned('ws','job');self.assertIsNone(get.call_args.kwargs['proxy'])
            c.config['network']['acp_proxy']=True
            c.owned('ws','job');self.assertEqual(get.call_args.kwargs['proxy'],'socks5h://u:p@127.0.0.1:1080')

    def test_overlapping_pages_deduplicate_uid(self):
        import json
        c=self.client()
        rows=[{'name':str(i),'uid':str(i),'ownership':{'user_id':c.user_id}} for i in range(100)]
        extra={'name':'extra','uid':'extra','ownership':{'user_id':c.user_id}}
        with patch.object(acp, 'get_json', side_effect=[{'training_jobs': rows, 'next_page_token': '2'}, {'training_jobs': [rows[-1], extra]}]):
            self.assertEqual(len(c.jobs('ws')),101)
