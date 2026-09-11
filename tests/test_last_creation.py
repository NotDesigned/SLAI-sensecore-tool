import copy
import unittest
from unittest.mock import Mock, patch
from textual.widgets import Button, DataTable
from scripts import cli, ui, dnat
from scripts.forms import CreateDraft
from scripts.tui import SlaiApp, Form

WS=dict(name='ws',region='cn-sh-01',subscription_name='sub',resource_group_name='group',zone='cn-sh-01z')
UID='11111111-1111-4111-8111-111111111111'

def client():
    c=Mock(config={'cci':{'ssh_enabled':False}})
    c.identity_data.return_value={'id':UID,'tenant_id':UID}
    c.clusters.return_value=[dict(name='pool',id='pool-id',uid='pool-uid',zone='cn-sh-01e',properties={'vpc_id':'vpc'})]
    c.specs.return_value=[{'ZONE':'cn-sh-01e','WORKER SPEC':'small','VCPU COUNT':'2','MEMORY(GIB)':'4','CHIP COUNT':'0'}]
    c.resources.return_value=[dict(name='afs',id='afs-id',zone='cn-sh-01e')]
    c.current_username.return_value='test-user'
    return c

def previous(c):
    draft=CreateDraft('cci',c,WS);draft.initialize()
    draft.values.update(image='registry/app:old',command='sleep infinity',nodes=2,quota='SPOT',network_set=True)
    return draft.snapshot()

class PreviousTests(unittest.TestCase):
    def test_previous_options_are_revalidated_and_filled_without_writes(self):
        c=client();saved=previous(c)
        draft=CreateDraft('cci',c,WS,previous=saved);draft.initialize()
        self.assertEqual(draft.values['image'],'registry/app:old')
        self.assertEqual(draft.values['nodes'],2)
        self.assertEqual(draft.values['quota'],'SPOT')
        self.assertTrue(draft.values['network_set'])
        self.assertEqual(draft.build()[3]['template']['containers'][0]['volume_mounts'][0]['subdir'],'/test-user')
        self.assertNotIn('name',saved)
        c.create.assert_not_called()

    def test_account_workspace_and_replaced_pool_fail_closed(self):
        for change in ('owner','workspace','pool'):
            c=client();saved=previous(c)
            if change=='owner':saved['owner_id']='other'
            elif change=='workspace':saved['workspace']['name']='other'
            else:saved['cluster']['uid']='replaced'
            with self.assertRaises(cli.ConfigError):CreateDraft('cci',c,WS,previous=saved).initialize()

    def test_reuse_network_allocates_new_rule_and_port_instead_of_moving_old_binding(self):
        c=client();saved=previous(c)
        eip=dict(name='eip',id='eip-id',zone='cn-sh-01e',region='cn-sh-01',properties={'vpc_id':'vpc'})
        saved['network']=dict(enabled=True,eip={k:eip[k] for k in ('name','id','zone','region')},internal_port='22')
        c.resources.side_effect=lambda kind:[eip] if kind=='network.eip.v1.eip' else [dict(name='afs',id='afs-id',zone='cn-sh-01e')]
        api=Mock();api.list.return_value=[dict(name='old-rule',properties={'external_port':'22222','protocol':'tcp'})]
        with patch.object(dnat,'Api',return_value=api), patch.object(dnat,'prepare',side_effect=lambda api,body,rows:body), patch.object(dnat,'create_rule') as create:
            draft=CreateDraft('cci',c,WS,previous=saved);draft.initialize()
        plan=draft.values['network']
        self.assertEqual(plan['mode'],'new')
        self.assertNotEqual(plan['body']['properties']['external_port'],'22222')
        self.assertNotEqual(plan['body']['name'],'old-rule')
        api.request.assert_not_called();create.assert_not_called()


class PreviousUiTests(unittest.IsolatedAsyncioTestCase):
    async def test_previous_form_has_direct_submit_and_remains_editable(self):
        c=client();saved=previous(c);draft=CreateDraft('cci',c,WS,previous=saved)
        app=SlaiApp();results=[]
        with patch.object(cli,'menu_title',return_value='SLAI-tool'):
            async with app.run_test(size=(80,24)) as pilot:
                form=Form(draft);await app.push_screen(form,results.append)
                for _ in range(40):
                    await pilot.pause(.025)
                    if not form.busy:break
                self.assertEqual(str(form.query_one('#review',Button).label),'提交创建')
                self.assertFalse(form.query_one(DataTable).disabled)
                await pilot.click('#review')
                for _ in range(40):
                    await pilot.pause(.025)
                    if results:break
                self.assertTrue(results)
                self.assertTrue(draft.submit_requested)
        c.create.assert_not_called()  # The form returns a validated plan to the controller.

class SubmitDefaultTests(unittest.IsolatedAsyncioTestCase):
    async def test_new_forms_submit_by_default_and_explicit_save_stays_local(self):
        for kind in ('cci','acp'):
            for button in ('review','save'):
                c=client();draft=CreateDraft(kind,c,WS);draft.values['command']='sleep 1'
                app=SlaiApp();results=[]
                with patch.object(cli,'menu_title',return_value='SLAI-tool'):
                    async with app.run_test(size=(80,24)) as pilot:
                        form=Form(draft);await app.push_screen(form,results.append)
                        for _ in range(40):
                            await pilot.pause(.025)
                            if not form.busy:break
                        draft.values['network_set']=True
                        self.assertEqual(str(form.query_one('#review',Button).label),'提交创建')
                        await pilot.click('#'+button)
                        for _ in range(40):
                            await pilot.pause(.025)
                            if results:break
                        self.assertTrue(results)
                        self.assertEqual(draft.submit_requested,button=='review')
                        self.assertEqual(draft.save_requested,button=='save')
                c.create.assert_not_called()  # The controller alone performs the mutation.

    def test_text_form_enter_selects_submit(self):
        draft=Mock(previous=None,title='创建',submit_requested=False,save_requested=False)
        draft.rows.return_value=[];draft.build.return_value=('checked-plan',)
        with patch('builtins.input',return_value=''):
            self.assertEqual(ui.creation_form(draft),('checked-plan',))
        self.assertTrue(draft.submit_requested);self.assertFalse(draft.save_requested)
