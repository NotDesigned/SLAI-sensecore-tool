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
        api=Mock();api.list.return_value=[RULE]
        with patch.object(cci_service,'owned_app',return_value=APP),patch.object(cci_ssh,'show_connection') as show,patch.object(ui,'choose') as choose:
            cci_service.connect_app({},WS,APP,[(api,RULE)])
        show.assert_called_once_with({},'192.0.2.1','23456','app');choose.assert_not_called()
        api.list.assert_called_once_with();api.request.assert_not_called()

    def test_migrated_rule_or_replaced_app_cannot_generate_old_command(self):
        for replaced in (True,False):
            api=Mock();api.list.return_value=[copy.deepcopy(RULE)]
            api.list.return_value[0]['properties']['internal_instance_name']='another-app'
            current=dict(APP,uid='new-uid') if replaced else APP
            with patch.object(cci_service,'owned_app',return_value=current),patch.object(cci_ssh,'show_connection') as show:
                with self.assertRaises(cli.ConfigError):cci_service.connect_app({},WS,APP,[(api,RULE)])
            show.assert_not_called()

    def test_menu_does_not_scan_and_offers_connection_for_running_app(self):
        for state,entries,wanted in [('RUNNING',[(Mock(),RULE)],True),('RUNNING',[],True),('SUSPENDED',[(Mock(),RULE)],False)]:
            app=dict(APP,state=state)
            with patch.object(cci_service,'connection_entries',return_value=entries) as scan, \
                 patch.object(ui,'browse',side_effect=lambda title,fetch,label,selected,**kw:selected(app)), \
                 patch.object(ui,'choose',return_value='返回列表') as choose:
                cci_service.list_page({},WS)
            self.assertEqual('连接' in choose.call_args.args[1],wanted)
            scan.assert_not_called()
            self.assertEqual('保存为镜像' in choose.call_args.args[1], state=='RUNNING')

    def test_permission_metadata_changes_do_not_invalidate_route(self):
        api=Mock()
        changed=copy.deepcopy(RULE)
        changed['properties'].update(has_update_permission=False, extra_status='new')
        changed['properties']['external_port']=23456
        api.list.return_value=[changed]
        with patch.object(cci_service,'owned_app',return_value=APP),patch.object(cci_ssh,'show_connection') as show:
            cci_service.connect_app({},WS,APP,[(api,RULE)])
        show.assert_called_once()

    def test_changed_port_or_replaced_rule_still_rejected(self):
        for field in ('external_port','internal_port','external_ip','uid'):
            changed=copy.deepcopy(RULE)
            if field=='uid':changed[field]='replacement'
            else:changed['properties'][field]='12345' if 'port' in field else '192.0.2.9'
            api=Mock();api.list.return_value=[changed]
            with patch.object(cci_service,'owned_app',return_value=APP),patch.object(cci_ssh,'show_connection') as show:
                with self.assertRaises(cli.ConfigError):
                    cci_service.connect_app({},WS,APP,[(api,RULE)])
            show.assert_not_called()

    def test_discovery_cached_but_each_connection_revalidated(self):
        api=Mock();api.list.return_value=[RULE]
        def browse(title,fetch,label,selected,**kw):
            selected(APP)
            selected(APP)
        with patch.object(ui,'browse',side_effect=browse),patch.object(ui,'choose',return_value='连接'), \
             patch.object(cci_service,'connection_entries',return_value=[(api,RULE)]) as scan, \
             patch.object(cci_service,'owned_app',return_value=APP),patch.object(cci_ssh,'show_connection'):
            cci_service.list_page({},WS)
        scan.assert_called_once()
        self.assertEqual(api.list.call_count,2)

    def test_missing_binding_reports_actionable_message(self):
        with patch.object(ui,'browse',side_effect=lambda title,fetch,label,selected,**kw:selected(APP)), \
             patch.object(ui,'choose',return_value='连接'),patch.object(cci_service,'connection_entries',return_value=[]):
            with self.assertRaisesRegex(cli.ConfigError,'DNAT 服务'):
                cci_service.list_page({},WS)

    def test_expired_or_failed_discovery_is_not_reused(self):
        for failure in (False, True):
            now=[0]
            def browse(title,fetch,label,selected,**kw):
                if failure:
                    with self.assertRaises(cli.ConfigError):
                        selected(APP)
                else:
                    selected(APP)
                    now[0]=31
                selected(APP)
            with self.subTest(failure=failure), patch.object(ui,'browse',side_effect=browse), \
                 patch.object(ui,'choose',return_value='连接'), \
                 patch.object(cci_service.time,'monotonic',side_effect=lambda:now[0]), \
                 patch.object(cci_service,'connection_entries',return_value=[(Mock(),RULE)]) as scan, \
                 patch.object(cci_service,'connect_app',side_effect=[cli.ConfigError('changed'),None] if failure else [None,None]):
                cci_service.list_page({},WS)
            self.assertEqual(scan.call_count,2)
