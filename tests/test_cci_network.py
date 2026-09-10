import contextlib
import io
import json
import copy
import unittest
from unittest.mock import Mock, patch

from scripts import cci_network as network, cli


class CciNetworkTests(unittest.TestCase):
    def test_existing_rules_are_combined_without_an_eip_selection(self):
        eips = [{'name': name, 'zone': 'zone', 'properties': {'vpc_id': 'vpc'}} for name in ('empty-eip', 'has-rule-eip')]
        rule = {'name': 'mine', 'uid': 'rule-id', 'creator_id': 'user', 'state': 'CREATED',
                'properties': {'external_ip': '192.0.2.1', 'external_port': '30001', 'internal_port': '22', 'protocol': 'tcp'}}
        clients = {name: Mock() for name in ('empty-eip', 'has-rule-eip')}
        clients['empty-eip'].current_user_id.return_value = 'user'
        clients['empty-eip'].list.return_value = []
        clients['has-rule-eip'].list.return_value = [rule, {**rule, 'name': 'other', 'creator_id': 'other'}]
        client = Mock()
        client.resources.return_value = eips
        document = {'display_name': 'app', 'resource_pool': {'available_zone': 'zone', 'vpc_id': 'vpc'}}
        with patch.object(network.cci, 'Client', return_value=client):
            with patch.object(network.dnat, 'Api', side_effect=lambda config, eip: clients[eip['name']]):
                with patch('builtins.input', side_effect=['3', '1', '']), contextlib.redirect_stdout(io.StringIO()) as output:
                    plan = network.plan_dnat({}, document, '')
        self.assertEqual(plan['eip'], eips[1])
        self.assertEqual(plan['body']['name'], 'mine')
        self.assertIn('1. 192.0.2.1:30001', output.getvalue())
        self.assertNotIn('EIP（同可用区', output.getvalue())
        self.assertNotIn('empty-eip', output.getvalue())
        self.assertNotIn('has-rule-eip', output.getvalue())
        clients['empty-eip'].list.assert_called_once()
        clients['has-rule-eip'].list.assert_called_once()

    def test_existing_rule_migration_unbinds_before_binding_without_create(self):
        client, api = Mock(), Mock()
        client.read.return_value = json.dumps({'uid': 'new-app'})
        api.current_user_id.return_value = 'user'
        old = {'name': 'rule', 'uid': 'rule-id', 'creator_id': 'user', 'state': 'ACTIVE',
               'properties': {'external_ip': '192.0.2.1', 'external_port': '22222', 'protocol': 'tcp',
                              'internal_port': '22', 'internal_instance_name': 'old-app',
                              'internal_instance_type': 'CCI_DEPLOYMENT_SERVICE'}}
        unbound = copy.deepcopy(old)
        unbound['state'] = 'CREATED'
        unbound['properties'].update(internal_instance_name='', internal_instance_type='UNSPECIFIED')
        new = copy.deepcopy(old)
        new['properties']['internal_instance_name'] = 'new-app'
        api.list.side_effect = [[old], [unbound], [new]]
        with patch.object(network.dnat, 'Api', return_value=api), patch.object(network.dnat, 'create_rule') as create:
            with contextlib.redirect_stdout(io.StringIO()):
                network.attach_dnat({}, client, 'ws', 'app', {'eip': {}, 'body': copy.deepcopy(old), 'mode': 'existing'})
        create.assert_not_called()
        self.assertEqual([x.args[:2] for x in api.request.call_args_list], [('POST', '/rule/unbind'), ('POST', '/rule/bind')])
        self.assertEqual(api.request.call_args.args[2]['properties']['internal_instance_name'], 'new-app')

    def test_changed_existing_rule_is_not_unbound(self):
        client, api = Mock(), Mock()
        client.read.return_value = json.dumps({'uid': 'new-app'})
        api.current_user_id.return_value = 'user'
        old = {'name': 'rule', 'uid': 'original', 'creator_id': 'user', 'state': 'CREATED',
               'properties': {'external_port': '22222', 'protocol': 'tcp'}}
        api.list.return_value = [{**old, 'uid': 'replaced'}]
        with patch.object(network.dnat, 'Api', return_value=api):
            with self.assertRaisesRegex(cli.ConfigError, '已变化'):
                network.attach_dnat({}, client, 'ws', 'app', {'eip': {}, 'body': old, 'mode': 'existing'})
        api.request.assert_not_called()

    def test_replaced_rule_does_not_pass_bind_readback(self):
        client, api = Mock(), Mock()
        client.read.return_value = json.dumps({'uid': 'app-id'})
        api.current_user_id.return_value = 'user'
        row = {'name': 'rule', 'uid': 'original', 'creator_id': 'user', 'state': 'CREATED',
               'properties': {'external_ip': '192.0.2.1', 'external_port': '22222',
                              'internal_port': '22', 'protocol': 'tcp'}}
        replaced = copy.deepcopy(row)
        replaced.update(uid='replacement', state='ACTIVE')
        replaced['properties'].update(internal_instance_type='CCI_DEPLOYMENT_SERVICE', internal_instance_name='app-id')
        api.list.side_effect = [[row]] + [[replaced]] * 10
        with patch.object(network.dnat, 'Api', return_value=api), patch.object(network.time, 'sleep'):
            with self.assertRaises(cli.ConfigError):
                network.attach_dnat({}, client, 'ws', 'app', {'eip': {}, 'body': row, 'mode': 'existing'})

    def test_rule_selection_excludes_other_users_and_in_progress_rules(self):
        row = {'name': 'rule', 'creator_id': 'me', 'state': 'ACTIVE',
               'properties': {'protocol': 'tcp', 'external_port': '22222', 'internal_instance_name': 'old'}}
        self.assertTrue(network.is_selectable(row, 'me'))
        self.assertFalse(network.is_available(row, 'me'))
        self.assertFalse(network.is_selectable(row, 'other'))
        self.assertFalse(network.is_selectable({**row, 'state': 'UNBINDING'}, 'me'))
    def test_default_does_not_query_or_create_network_resources(self):
        with patch('builtins.input', return_value=''), patch.object(network.dnat, 'Api') as api:
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertIsNone(network.plan_dnat({}, {}, ''))
        api.assert_not_called()

    def test_ssh_defaults_to_new_dnat_and_fixes_internal_port(self):
        eip = {'name': 'same', 'zone': 'zone', 'properties': {'vpc_id': 'vpc'}}
        client, api = Mock(), Mock()
        client.resources.return_value = [eip]
        api.request.return_value = {'id': 'user', 'tenant_id': 'tenant'}
        api.list.return_value = []
        document = {'display_name': 'app', 'resource_pool': {'available_zone': 'zone', 'vpc_id': 'vpc'}}
        with patch.object(network.cci, 'Client', return_value=client), \
             patch.object(network.dnat, 'Api', return_value=api), \
             patch.object(network.dnat, 'prepare', side_effect=lambda api, body, rows: body), \
             patch('builtins.input', side_effect=['', '1', '', '192.0.2.1']) as prompt, \
             contextlib.redirect_stdout(io.StringIO()):
            plan = network.plan_dnat({}, document, '22', ssh_port='22')
        self.assertEqual(plan['body']['properties']['internal_port'], '22')
        self.assertEqual(plan['ports'], '22')
        self.assertFalse(any('容器端口' in call.args[0] for call in prompt.call_args_list))

    def test_plan_scopes_eip_and_adds_container_port(self):
        eip = {'name': 'same', 'zone': 'zone', 'properties': {'vpc_id': 'vpc'}}
        client, api = Mock(), Mock()
        client.resources.return_value = [eip, {**eip, 'zone': 'wrong'}]
        api.request.return_value = {'id': 'user', 'tenant_id': 'tenant'}
        api.list.return_value = [{'properties': {'external_ip': '192.0.2.1', 'external_port': '22220', 'protocol': 'tcp'}}]
        body = {'name': 'app-dnat', 'properties': {'external_ip': '192.0.2.1'}}
        document = {'display_name': 'app', 'resource_pool': {'available_zone': 'zone', 'vpc_id': 'vpc'}}
        with patch.object(network.cci, 'Client', return_value=client), patch.object(network.dnat, 'Api', return_value=api):
            with patch.object(network.dnat, 'prepare', return_value=body) as prepare:
                with patch('builtins.input', side_effect=['2', '1', '22222', '22']), contextlib.redirect_stdout(io.StringIO()):
                    plan = network.plan_dnat({}, document, '8080')
        self.assertEqual(plan['eip'], eip)
        self.assertEqual(plan['ports'], '22,8080')
        self.assertEqual(prepare.call_args.args[1]['creator_id'], 'user')
        self.assertTrue(all(call.args[0] == 'GET' for call in api.request.call_args_list))

    def test_unspecified_binding_is_not_reported_as_success(self):
        client, api = Mock(), Mock()
        client.read.return_value = json.dumps({'uid': 'app-uid'})
        row = {'name': 'rule', 'state': 'CREATED', 'properties': {'external_ip': '192.0.2.1',
               'external_port': '22222', 'internal_port': '22', 'protocol': 'tcp'}}
        api.list.return_value = [{'name': 'rule', 'state': 'ACTIVE', 'properties': {'internal_instance_type': 'UNSPECIFIED'}}]
        with patch.object(network.dnat, 'Api', return_value=api), patch.object(network.dnat, 'create_rule', return_value=row):
            with patch.object(network.time, 'sleep'), self.assertRaisesRegex(cli.ConfigError, '未通过复查'):
                network.attach_dnat({}, client, 'ws', 'app', {'eip': {}, 'body': row})
        self.assertEqual(api.request.call_args.args[:2], ('POST', '/rule/bind'))
