import unittest
from unittest.mock import patch, Mock


from scripts import cloud, ui, acp, cli

class ACPTests(unittest.TestCase):
    def client(self):
        client = object.__new__(acp.Client)
        client.user_id = '019d05a5-ea37-7ec1-95c4-eba56ea2ab04'
        return client

    def test_empty_and_ownership(self):
        c = self.client()
        c.read = Mock(return_value='No jobs found\n')
        self.assertEqual(c.jobs('ws'), [])
        c.read.return_value = '[{"name":"other","ownership":{"user_id":"other"}}]'
        self.assertEqual(c.jobs('ws'), [])
        c.read.return_value = ''
        with self.assertRaises(cli.ConfigError):
            c.jobs('ws')

    def test_repeated_page(self):
        import json
        c = self.client()
        c.read = Mock(return_value=json.dumps([{'name':str(i)} for i in range(100)]))
        with self.assertRaises(cli.ConfigError):
            c.jobs('ws')

    def test_owner_and_uid_rechecked(self):
        import json
        c = self.client()
        row = {'name':'job','uid':'new','ownership':{'user_id':c.user_id}}
        c.read = Mock(return_value=json.dumps(row))
        with self.assertRaises(cli.ConfigError):
            c.owned('ws', 'job', {'uid':'old'})
        row['ownership']['user_id'] = 'other'
        c.read.return_value = json.dumps(row)
        with self.assertRaises(cli.ConfigError):
            c.owned('ws','job')

    def test_entrypoint_quoting(self):
        import shlex
        with patch.object(ui,'choose',return_value='使用镜像启动逻辑'), patch.object(ui,'ask',return_value='"/entry point" "a; b" "$(evil)"'):
            value = acp.startup({})
        self.assertEqual(shlex.split(value), ['exec','/entry point','a; b','$(evil)'])

    def test_invalid_entrypoint(self):
        for value in ('[]','[1]', '"unclosed', '   '):
            with patch.object(ui,'choose',return_value='使用镜像启动逻辑'), patch.object(ui,'ask',return_value=value), self.assertRaises(cli.ConfigError):
                acp.startup({})

    def test_unknown_submission_redacted(self):
        c = self.client(); c.control_env={}; c.command=Mock(return_value=['sco'])
        with patch.object(acp.subprocess,'run',return_value=Mock(returncode=1,stderr='PASSWORD')):
            with self.assertRaises(cli.ConfigError) as caught:
                c.submit([])
        self.assertNotIn('PASSWORD',str(caught.exception))

    def test_cancel_delete_no_submission(self):
        c=self.client(); c.owned=Mock(return_value={'uid':'id'}); c.submit=Mock()
        with patch.object(ui,'choose',return_value='取消'):
            acp.operate(c,'ws',{'name':'job'},'删除')
        c.submit.assert_not_called()

    def test_menu_back(self):
        with patch('builtins.input',return_value='0'):
            self.assertEqual(acp.main([]),0)

    def test_names(self):
        for value in ('--flag','../x','x y','x'*64):
            with self.assertRaises(cli.ConfigError):
                acp.validate_name(value)

    def test_create_args_and_mount(self):
        c=Mock(); c.config={}
        cluster={'name':'pool','zone':'cn-sh-01e'}
        spec={'ZONE':'cn-sh-01e','WORKER SPEC':'gpu-small'}
        mount={'id':'volume','subdir':'/user','mount_path':'/data'}
        with patch.object(ui,'choose',side_effect=[cluster,spec,'pytorch','RESERVED']), \
             patch.object(ui,'ask',return_value='test-job'), \
             patch.object(cloud,'select_image',return_value='registry/image:tag'), \
             patch.object(acp,'startup',return_value='set -eu; python train.py'), \
             patch.object(ui,'number',return_value=1), \
             patch.object(cloud,'select_mounts',return_value=[mount]):
            name,args=acp.prepare(c,{'name':'ws'})
        self.assertEqual(name,'test-job')
        self.assertEqual(args[args.index('--quota-type')+1],'reserved')
        self.assertEqual(args[args.index('--priority')+1],'NORMAL')
        self.assertEqual(args[args.index('--storage-mount')+1],'volume/user:/data')
        self.assertNotIn('sshd',' '.join(args))

    def test_proxy_is_opt_in_and_scoped(self):
        from pathlib import Path
        config={'sco': {}, 'cci': {}, 'network': {'socks5': {'server': '127.0.0.1', 'port': 1080, 'username': 'u', 'password': 'p'}}}
        with patch.object(cli,'runtime',return_value=({'PATH':'x'},Path(__file__))), patch.object(cli,'string_value',side_effect=lambda d,k: d.get(k,'')):
            c=acp.Client(config)
            self.assertNotIn('HTTPS_PROXY',c.control_env)
            config['network']['acp_proxy']=True
            c=acp.Client(config)
            self.assertIn('HTTPS_PROXY',c.control_env)
            self.assertNotIn('HTTPS_PROXY',c.env)

    def test_overlapping_pages_deduplicate_uid(self):
        import json
        c=self.client()
        rows=[{'name':str(i),'uid':str(i),'ownership':{'user_id':c.user_id}} for i in range(100)]
        extra={'name':'extra','uid':'extra','ownership':{'user_id':c.user_id}}
        c.read=Mock(side_effect=[json.dumps(rows),json.dumps([rows[-1],extra])])
        self.assertEqual(len(c.jobs('ws')),101)
