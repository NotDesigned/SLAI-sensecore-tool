"""CLI dispatch checks: cloud writes and interactive forms are mocked."""
import contextlib
import io
import subprocess
import sys
import unittest
from unittest.mock import Mock,patch
from scripts import cli,cci_service,cci_snapshot,acp,dnat,ccr,workspace

WS=dict(name='ws',region='cn-sh-01',subscription_name='sub',resource_group_name='group',zone='cn-sh-01z')
ROW=dict(name='target',uid='uid',state='RUNNING')
CFG={'account':{'access_key_id':'fixture','access_key_secret':'fixture'}}


class CliRoutes(unittest.TestCase):
    def setUp(self):
        for target,value in [('scripts.cli.load_config',CFG),('scripts.workspace.select',WS)]:
            p=patch(target,return_value=value);p.start();self.addCleanup(p.stop)

    def test_all_help_commands_are_standalone(self):
        for service in ('','cci','acp','dnat','ccr','configure','workspace','proxy','guide'):
            result=subprocess.run([sys.executable,str(cli.ROOT/'main.py'),*([service] if service else []),'--help'],
                                  capture_output=True,text=True,encoding='utf-8',timeout=10)
            self.assertEqual(result.returncode,0,(service,result.stderr))
            self.assertIn('--help',result.stdout)

    def test_cci_direct_actions(self):
        for action,target in [('start','start_app'),('stop','stop_app'),('delete','delete_app'),('copy','copy_app')]:
            with self.subTest(action=action),patch.object(cci_service,'owned_app',return_value=ROW),patch.object(cci_service,target) as invoke,contextlib.redirect_stdout(io.StringIO()):
                cci_service.main([action,'--name','target',*(['--yes'] if action in ('stop','delete') else [])])
            invoke.assert_called_once()
        for action in ('snapshot','snapshots'):
            with patch.object(cci_service,'owned_app',return_value=ROW),patch.object(cci_snapshot,'create_interactive') as create,patch.object(cci_snapshot,'list_page') as listing:
                cci_service.main([action,'--name','target',*(['--plain'] if action=='snapshots' else [])])
            (create if action=='snapshot' else listing).assert_called_once()
        with patch.object(cci_service,'owned_app',return_value=ROW),patch.object(cci_service,'connection_entries',return_value=['entry']),patch.object(cci_service,'connect_app') as connect:
            cci_service.main(['connect','--name','target'])
        connect.assert_called_once_with(CFG,WS,ROW,['entry'])

    def test_acp_direct_actions_and_paged_filter(self):
        client=Mock(config=CFG);client.owned.return_value=ROW
        with patch.object(acp,'Client',return_value=client),contextlib.redirect_stdout(io.StringIO()):
            for action in ('stop','delete'):
                acp.main([action,'--name','target','--yes'])
                client.control.assert_called_with('ws','target',action,ROW)
            with patch.object(acp,'operate') as operate:
                acp.main(['copy','--name','target'])
                operate.assert_called_once_with(client,'ws',ROW,'复制')
            from scripts.listing import Page
            client.jobs_page.return_value=Page([],False,0)
            acp.main(['list','--plain','--page','2','--page-size','10','--state','FAILED','--name','prefix'])
            client.jobs_page.assert_called_once_with('ws',1,10,'prefix','FAILED')

    def test_dnat_direct_operations(self):
        api=Mock()
        for action,target in [('bind','bind_existing_cci'),('unbind','unbind_rule'),('describe','show_rule'),('delete','remove_rule')]:
            with self.subTest(action=action),patch.object(dnat,'all_my_rules',return_value=[(api,ROW)]),patch.object(dnat,target) as invoke,contextlib.redirect_stdout(io.StringIO()):
                dnat.main([action,'--name','target',*(['--yes'] if action in ('unbind','delete') else [])])
            invoke.assert_called_once()

    def test_invalid_combinations_fail_before_reading_account(self):
        commands=[(cci_service,['copy','--yes']),(cci_service,['create','--name','x']),
                  (acp,['list','--page','2']),(acp,['list','--plain','--page','0']),
                  (dnat,['bind','--yes']),(ccr,['upload','--plain'])]
        for module,args in commands:
            with self.subTest(args=args),patch.object(cli,'load_config') as load,contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as error:
                    module.main(args)
                self.assertEqual(error.exception.code,2)
            load.assert_not_called()
