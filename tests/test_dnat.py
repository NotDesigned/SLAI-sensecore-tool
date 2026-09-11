import json
import unittest
from unittest.mock import Mock, patch



from scripts import network, dnat, cli, rest

class DnatTests(unittest.TestCase):
    def test_random_port_excludes_all_users_protocols_and_port_ranges(self):
        rows = [{'creator_id': 'other', 'properties': {'protocol': 'tcp', 'external_port': '20000-20002'}},
                {'properties': {'protocol': 'udp', 'external_port': '20003'}}]
        self.assertEqual(dnat.random_free_port(rows, 20000, 20004), '20004')
        with self.assertRaises(cli.ConfigError):
            dnat.random_free_port(rows, 20000, 20003)

    def test_random_port_uses_random_choice_and_fails_on_unknown_occupancy(self):
        with patch.object(dnat.secrets, 'choice', side_effect=lambda values: values[-1]) as choice:
            self.assertEqual(dnat.random_free_port([], 20000, 20003), '20003')
        choice.assert_called_once_with([20000, 20001, 20002, 20003])
        with self.assertRaises(cli.ConfigError):
            dnat.random_free_port([{'properties': {}}])

    def test_port_taken_after_selection_stops_before_post(self):
        api = Mock()
        api.eip = self.api.eip
        api.scope = self.api.scope
        api.list.return_value = [{'name': 'concurrent', 'properties': {'external_port': '22223', 'protocol': 'tcp'}}]
        with self.assertRaises(cli.ConfigError):
            dnat.create_rule(api, self.body)
        api.request.assert_not_called()

    def setUp(self):
        self.api = dnat.Api({'sco': {}}, {'name': 'eip', 'region': 'cn-sh-01', 'zone': 'cn-sh-01e',
            'id': 'eip-id', 'subscription_name': 'sub', 'resource_group_name': 'default',
            'properties': {'association_id': 'gateway'}})
        self.body = {'name': 'test', 'creator_id': '11111111-1111-4111-8111-111111111111',
            'owner_id': '11111111-1111-4111-8111-111111111111',
            'tenant_id': '22222222-2222-4222-8222-222222222222',
            'properties': {'external_ip': '192.0.2.1', 'external_port': '22223', 'internal_port': '22', 'protocol': 'tcp'}}

    def test_prepare_requires_ownership_and_does_not_reuse_state_or_uid(self):
        for key in ('creator_id', 'owner_id', 'tenant_id'):
            body = {k: v for k, v in self.body.items() if k != key}
            with self.assertRaises(cli.ConfigError):
                dnat.prepare(self.api, body, [])
        body = dnat.prepare(self.api, {**self.body, 'state': 'ACTIVE', 'uid': 'old'}, [])
        self.assertNotIn('state', body)
        self.assertNotEqual(body['uid'], 'old')
        self.assertEqual(body['properties']['nat_gateway_id'], 'gateway')

    def test_port_overlap_ranges_and_protocol(self):
        existing = [{'name': 'other', 'properties': {'external_port': '22220-22230', 'protocol': 'tcp'}}]
        with self.assertRaises(cli.ConfigError):
            dnat.prepare(self.api, self.body, existing)
        existing[0]['properties']['protocol'] = 'udp'
        dnat.prepare(self.api, self.body, existing)
        for value in ('0', '65536', '2-1', '1-501', 'abc'):
            with self.assertRaises(cli.ConfigError):
                dnat.port_range(value)

    def test_bound_creation_rejected(self):
        self.body['properties']['internal_instance_name'] = 'app'
        with self.assertRaises(cli.ConfigError):
            dnat.prepare(self.api, self.body, [])

    def test_pagination_and_repeated_page(self):
        first = {'dnat_rules': [{'name': 'a'}], 'total_size': 2, 'next_page_token': '2'}
        second = {'dnat_rules': [{'name': 'b'}], 'total_size': 2}
        with patch.object(self.api, 'request', side_effect=[first, second]):
            self.assertEqual([x['name'] for x in self.api.list()], ['a', 'b'])
        with patch.object(self.api, 'request', side_effect=[first, first]):
            with self.assertRaises(cli.ConfigError):
                self.api.list()

    def test_create_polls_and_does_not_retry_post(self):
        row = {**self.body, 'state': 'CREATED'}
        with patch.object(self.api, 'list', side_effect=[[], [], [row]]), patch.object(self.api, 'request') as request:
            with patch.object(dnat.time, 'sleep'):
                self.assertEqual(dnat.create_rule(self.api, self.body), row)
        self.assertEqual(request.call_count, 1)
        self.assertEqual(request.call_args.args[0], 'POST')

    def test_delete_checks_target_and_verifies_disappearance(self):
        row = {'name': 'test', 'properties': {}}
        with patch.object(self.api, 'list', side_effect=[[row], [row], []]), patch.object(self.api, 'request') as request:
            with patch.object(dnat.time, 'sleep'):
                dnat.delete_rule(self.api, 'test')
        request.assert_called_once_with('DELETE', '/test')
        row['properties']['internal_instance_name'] = 'app'
        with patch.object(self.api, 'list', return_value=[row]), patch.object(self.api, 'request') as request:
            with self.assertRaises(cli.ConfigError):
                dnat.delete_rule(self.api, 'test')
            request.assert_not_called()

    def test_http_error_is_failure_without_leaking_body(self):
        error = rest.RestError(403, {'message': 'SECRET'})
        self.assertNotIn('SECRET', str(error))
        self.assertEqual(error.status, 403)

    def test_only_creator_matches_not_owner_or_missing_creator(self):
        uid = self.body['creator_id']
        rows = [{'name': 'mine', 'creator_id': uid},
                {'name': 'owned-not-created', 'creator_id': 'other', 'owner_id': uid},
                {'name': 'unknown', 'creator_id': ''}, {'name': 'missing'}]
        with patch.object(self.api, 'current_user_id', return_value=uid), patch.object(self.api, 'list', return_value=rows):
            self.assertEqual(dnat.my_rules(self.api), [rows[0]])

    def test_current_identity_comes_from_authenticated_me(self):
        with patch.object(self.api, 'request', return_value={'id': self.body['creator_id']}) as request:
            self.assertEqual(self.api.current_user_id(), self.body['creator_id'])
            request.assert_called_once_with('GET', identity=True)

    def test_identity_failure_never_falls_back_to_all_rules(self):
        for response in ({}, {'id': ''}, {'id': 'invalid'}, []):
            with patch.object(self.api, 'request', return_value=response), patch.object(self.api, 'list') as listing:
                with self.assertRaises(cli.ConfigError):
                    dnat.my_rules(self.api)
                listing.assert_not_called()

    def test_aggregate_all_eips_filters_creator_and_keeps_source(self):
        apis = [Mock(base='one'), Mock(base='two')]
        uid = self.body['creator_id']
        apis[0].current_user_id.return_value = uid
        apis[0].list.return_value = [self.body, {**self.body, 'creator_id': 'other'}]
        second = {**self.body, 'properties': {**self.body['properties'], 'external_port': '22224'}}
        apis[1].list.return_value = [second, {**self.body, 'deleted': True}]
        with patch.object(dnat, 'Client') as client, patch.object(dnat, 'Api', side_effect=apis):
            client.return_value.resources.return_value = [{'name': 'one'}, {'name': 'two'}]
            self.assertEqual(dnat.all_my_rules({}), [(apis[0], self.body), (apis[1], second)])
        apis[0].current_user_id.assert_called_once()
        apis[1].current_user_id.assert_not_called()
        self.assertTrue(dnat.label(self.body).startswith('192.0.2.1:22223'))

    def test_list_page_selects_rule_deletes_and_refreshes(self):
        import contextlib
        import io
        with patch.object(dnat, 'all_my_rules', side_effect=[[(self.api, self.body)], []]) as listing, \
             patch.object(dnat, 'delete_rule') as delete, \
             patch.object(dnat, 'selected_rule', return_value=self.body), \
             patch('builtins.input', side_effect=['1', '1', '1', '0']), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            dnat.list_page({})
        delete.assert_called_once_with(self.api, 'test', expected=self.body)
        self.assertEqual(listing.call_count, 2)
        self.assertNotIn('选择 EIP', output.getvalue())

    def test_list_delete_default_cancel(self):
        import contextlib
        import io
        with patch.object(dnat, 'all_my_rules', return_value=[(self.api, self.body)]), \
             patch.object(dnat, 'delete_rule') as delete, \
             patch.object(dnat, 'selected_rule', return_value=self.body), \
             patch('builtins.input', side_effect=['1', '1', '', '0']), \
             contextlib.redirect_stdout(io.StringIO()):
            dnat.list_page({})
        delete.assert_not_called()

    def test_delete_rechecks_creator_and_selected_uid(self):
        for row in ({**self.body, 'creator_id': 'other'}, {**self.body, 'uid': 'replacement'}):
            with patch.object(self.api, 'list', return_value=[row]), \
                 patch.object(self.api, 'current_user_id', return_value=self.body['creator_id']), \
                 patch.object(self.api, 'request') as request:
                with self.assertRaises(cli.ConfigError):
                    dnat.delete_rule(self.api, 'test', expected=self.body)
                request.assert_not_called()

    def test_list_main_does_not_select_eip(self):
        with patch.object(cli, 'load_config', return_value={}), \
             patch.object(dnat, 'list_page') as page, patch.object(dnat, 'choose') as choose:
            dnat.main(['list'])
        page.assert_called_once_with({}, None, False)
        choose.assert_not_called()

    def test_bind_existing_filters_network_checks_service_and_reuses_rule(self):
        from scripts import cci_service, cci_network, cci_ssh, rest
        ws = {'name': 'ws', 'subscription_name': 'sub'}
        app = {'name': 'app', 'uid': 'app-id', 'ownership': {'user_id': 'me'},
               'resource_pool': {'available_zone': 'cn-sh-01e', 'vpc_id': 'vpc'}}
        self.api.eip['properties']['vpc_id'] = 'vpc'
        row = {**self.body, 'state': 'CREATED', 'uid': 'rule-id'}
        client = Mock()
        client.resources.return_value = [ws]
        with patch.object(dnat, 'Client', return_value=client), \
             patch.object(self.api, 'current_user_id', return_value=row['creator_id']), \
             patch.object(cci_service, 'my_apps', return_value=[app, {**app, 'name': 'wrong', 'resource_pool': {'vpc_id': 'other'}}]), \
             patch.object(cci_service, 'owned_app', return_value=app), \
             patch.object(cci_service, 'resource_url', return_value='https://example.test/service'), \
             patch.object(rest, 'get_json', return_value={'ports': [{'port': 22}]}), \
             patch.object(cci_network, 'attach_dnat') as attach, \
             patch.object(cci_ssh, 'show_connection') as show, \
             patch('builtins.input', side_effect=['1', '', '1']):
            dnat.bind_existing_cci({}, self.api, row)
        plan = attach.call_args.args[-1]
        self.assertEqual(plan['mode'], 'existing')
        self.assertEqual(plan['expected_uid'], 'app-id')
        self.assertEqual(plan['body']['properties']['internal_port'], '22')
        show.assert_called_once()

    def test_binding_changed_target_stops_before_mutation(self):
        from scripts import cci_network
        client = Mock()
        client.read.return_value = json.dumps({'uid': 'replacement'})
        with patch.object(dnat, 'Api') as api:
            with self.assertRaises(cli.ConfigError):
                cci_network.attach_dnat({}, client, 'ws', 'app', {'expected_uid': 'original'})
        api.assert_not_called()

    def test_unbind_polls_once_and_preserves_rule(self):
        import copy
        row = {**self.body, 'uid': 'same', 'state': 'ACTIVE'}
        row['properties'] = {**row['properties'], 'internal_instance_name': 'app'}
        unbound = copy.deepcopy(row)
        unbound['state'] = 'CREATED'
        unbound['properties']['internal_instance_name'] = ''
        with patch.object(dnat, 'selected_rule', return_value=row), \
             patch.object(self.api, 'request') as request, \
             patch.object(self.api, 'list', side_effect=[[row], [unbound]]), \
             patch.object(dnat.time, 'sleep'):
            self.assertEqual(dnat.unbind_rule(self.api, row), unbound)
        request.assert_called_once_with('POST', '/test/unbind', {})

    def test_delete_bound_rule_unbinds_before_delete_and_stops_on_failure(self):
        import contextlib
        import io
        bound = {**self.body, 'properties': {**self.body['properties'], 'internal_instance_name': 'app'}}
        events = []
        with patch.object(dnat, 'selected_rule', return_value=bound), \
             patch.object(dnat, 'unbind_rule', side_effect=lambda *a: events.append('unbind') or self.body), \
             patch.object(dnat, 'delete_rule', side_effect=lambda *a, **k: events.append('delete')), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(dnat.remove_rule(self.api, bound, confirmed=True))
        self.assertEqual(events, ['unbind', 'delete'])
        with patch.object(dnat, 'selected_rule', return_value=bound), \
             patch.object(dnat, 'unbind_rule', side_effect=cli.ConfigError('pending')), \
             patch.object(dnat, 'delete_rule') as delete, contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(cli.ConfigError):
                dnat.remove_rule(self.api, bound, confirmed=True)
        delete.assert_not_called()

    def test_unbind_replacement_cannot_pass_verification(self):
        row = {**self.body, 'uid': 'original', 'state': 'ACTIVE',
               'properties': {**self.body['properties'], 'internal_instance_name': 'app'}}
        with patch.object(dnat, 'selected_rule', return_value=row), \
             patch.object(self.api, 'request'), \
             patch.object(self.api, 'list', return_value=[{**row, 'uid': 'other'}]):
            with self.assertRaises(cli.ConfigError):
                dnat.unbind_rule(self.api, row)

    def test_detail_rechecks_owner(self):
        with patch.object(dnat, 'selected_rule', return_value=self.body), \
             patch.object(self.api, 'request', return_value={**self.body, 'creator_id': 'other'}):
            with self.assertRaises(cli.ConfigError):
                dnat.show_rule(self.api, self.body)

    def test_create_readback_must_match_internal_port(self):
        row = {**self.body, 'state':'CREATED', 'properties':{**self.body['properties'], 'internal_port':'2223'}}
        api = Mock(); api.list.side_effect = [[], [row]]
        with patch.object(dnat,'prepare',return_value=self.body), self.assertRaisesRegex(cli.ConfigError,'不一致'):
            dnat.create_rule(api,self.body)
        self.assertEqual(api.request.call_count,1)
