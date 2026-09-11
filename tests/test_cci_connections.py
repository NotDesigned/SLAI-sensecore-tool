import copy
import unittest
from unittest.mock import Mock,patch
from scripts import cci_service,cci_ssh,cli,cloud,dnat,ui

WS=dict(name='ws',subscription_name='sub')
APP=dict(name='app',uid='app-uid',state='RUNNING',ownership={'user_id':'me'},
         resource_pool={'available_zone':'cn-sh-01e','vpc_id':'vpc'})
RULE=dict(name='rule',uid='rule-uid',state='ACTIVE',creator_id='administrator',properties=dict(
    internal_instance_type='CCI_DEPLOYMENT_SERVICE',internal_instance_name='app-uid',
    protocol='tcp',external_ip='192.0.2.1',external_port='23456',internal_port='22'))

class ConnectionTests(unittest.TestCase):
    def test_binding_requires_exact_uid_type_active_tcp_and_single_ports(self):
        self.assertTrue(cci_service.bound_tcp_rule(RULE,APP))
        for field,value in [('internal_instance_name','other'),('internal_instance_name','app'),
                            ('internal_instance_type','UNSPECIFIED'),('protocol','udp'),
                            ('external_port','22000-22010'),('internal_port','0'),('external_ip','invalid')]:
            row=copy.deepcopy(RULE);row['properties'][field]=value
            self.assertFalse(cci_service.bound_tcp_rule(row,APP),(field,value))
        for field,value in [('state','CREATED'),('deleted',True),('uid','')]:
            self.assertFalse(cci_service.bound_tcp_rule(dict(RULE,**{field:value}),APP))

    def test_scan_only_matching_eips_and_include_visible_admin_binding(self):
        eip=dict(name='matching',subscription_name='sub',zone='cn-sh-01e',properties={'vpc_id':'vpc'})
        c=Mock();c.resources.return_value=[eip,dict(eip,zone='other'),dict(eip,subscription_name='other'),dict(eip,properties={'vpc_id':'other'})]
        api=Mock();api.list.return_value=[RULE,dict(RULE,state='CREATED')]
        with patch.object(cci_service,'owned_app',return_value=APP),patch.object(cloud,'Client',return_value=c),patch.object(dnat,'Api',return_value=api) as factory:
            self.assertEqual(cci_service.connection_entries({},WS,APP),[(api,RULE)])
        factory.assert_called_once_with({},eip);api.list.assert_called_once_with()

    def test_connect_rechecks_binding_then_uses_shared_ssh_probe_and_command(self):
        api=Mock();api.request.return_value=RULE
        with patch.object(cci_service,'owned_app',return_value=APP),patch.object(cci_ssh,'show_connection') as show,patch.object(ui,'choose') as choose:
            cci_service.connect_app({},WS,APP,[(api,RULE)])
        show.assert_called_once_with({},'192.0.2.1','23456','app');choose.assert_not_called()
        api.request.assert_called_once_with('GET','/rule')

    def test_migrated_rule_or_replaced_app_cannot_generate_old_command(self):
        for replaced in (True,False):
            api=Mock();api.request.return_value=copy.deepcopy(RULE)
            api.request.return_value['properties']['internal_instance_name']='another-app'
            current=dict(APP,uid='new-uid') if replaced else APP
            with patch.object(cci_service,'owned_app',return_value=current),patch.object(cci_ssh,'show_connection') as show:
                with self.assertRaises(cli.ConfigError):cci_service.connect_app({},WS,APP,[(api,RULE)])
            show.assert_not_called()

    def test_menu_only_offers_connection_for_running_bound_app(self):
        for state,entries,wanted in [('RUNNING',[(Mock(),RULE)],True),('RUNNING',[],False),('SUSPENDED',[(Mock(),RULE)],False)]:
            app=dict(APP,state=state)
            with patch.object(cci_service,'connection_entries',return_value=entries) as scan, \
                 patch.object(ui,'browse',side_effect=lambda title,fetch,label,selected,**kw:selected(app)), \
                 patch.object(ui,'choose',return_value='返回列表') as choose:
                cci_service.list_page({},WS)
            self.assertEqual('连接' in choose.call_args.args[1],wanted)
            self.assertEqual(scan.call_count,int(state=='RUNNING'))
