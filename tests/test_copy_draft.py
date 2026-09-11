import copy
import unittest
from unittest.mock import Mock,patch

from scripts import acp,cli,ui
from scripts.copy_draft import CopyDraft
from test_last_creation import client,WS


def cci_document():
    return dict(display_name='old',replicas=2,
        resource_pool=dict(name='pool',available_zone='cn-sh-01e',vpc_id='vpc'),
        scheduling=dict(priority='NORMAL',quota_type='SPOT'),
        template=dict(resource_spec={'name':'small'},containers=[dict(name='main',image_path='image:v1',
            command=['python','train.py','--title','two words'],env=[dict(name='KEEP',value='yes')],
            resource_request={'cpu':'2','memory':'4GiB'},volume_mounts=[])]))


def acp_document():
    return dict(name='old',display_name='old',framework='PYTORCH',
        resource_pool={'name':'pool'},mount=[],fault_tolerance={'backoff_limit':2},scheduling={'quota_type':'SPOT','priority':'NORMAL'},
        roles=[dict(name='Worker',resource_spec=[{'name':'small'}],total_replicas=2,
                    image_path='image:v1',startup_script='python train.py')])


class CopyDraftTests(unittest.TestCase):
    def test_cci_unchanged_fields_and_exec_argv_survive_edits(self):
        source=cci_document();draft=CopyDraft('cci',client(),WS,source,'old','22')
        draft.initialize()
        self.assertEqual(draft.values['image'],'image:v1')
        self.assertEqual(draft.values['quota'],'SPOT')
        self.assertIsNone(draft.values['network'])
        draft.values.update(name='new',image='image:v2',nodes=3)
        _,name,ports,result,network=draft.build()
        self.assertEqual((name,ports,network),('new','22',None))
        expected=copy.deepcopy(source);expected['display_name']='new';expected['replicas']=3
        expected['template']['containers'][0]['image_path']='image:v2'
        self.assertEqual(result,expected)
        self.assertEqual(source,cci_document())

    def test_acp_command_and_count_are_editable_without_losing_other_fields(self):
        source=acp_document();draft=CopyDraft('acp',client(),WS,source,'old')
        draft.initialize();draft.values.update(name='new',nodes=4,command='python train.py --epochs 3')
        name,result=draft.build()
        self.assertEqual(name,'new')
        self.assertEqual(result['roles'][0]['total_replicas'],4)
        self.assertEqual(result['roles'][0]['startup_script'],'python train.py --epochs 3')
        self.assertEqual(result['fault_tolerance'],source['fault_tolerance'])

    def test_default_image_command_is_preserved(self):
        source=acp_document();source['roles'][0]['startup_script']=''
        draft=CopyDraft('acp',client(),WS,source,'old')
        draft.initialize()
        self.assertEqual(draft.build()[1]['roles'][0]['startup_script'],'')

    def test_metadata_is_explicitly_omitted_but_other_unknown_fields_rejected(self):
        source=acp_document();source['metadata']={'server_info':'fixture'}
        with patch.object(ui,'output') as output:
            result=acp.copy_document(source,'new')
        self.assertNotIn('metadata',result)
        self.assertTrue(any('metadata' in str(c) for c in output.call_args_list))
        source['new_configuration']={'setting':True}
        with patch.object(ui,'output'),self.assertRaisesRegex(cli.ConfigError,'new_configuration'):
            acp.copy_document(source,'new')

    def test_acp_copy_opens_form_and_submits_edited_document(self):
        c=client();c.owned.return_value=acp_document();c.workspace_record.return_value=WS
        edited=acp_document();edited['name']='edited'
        def form(draft):
            self.assertIsInstance(draft,CopyDraft)
            draft.submit_requested=True
            return 'edited',edited
        with patch.object(ui,'creation_form',side_effect=form) as show,patch.object(acp,'confirm_submit') as submit:
            acp.operate(c,'ws',{'name':'old','uid':'source'},'复制')
        show.assert_called_once()
        self.assertEqual(submit.call_args.args[2:4],('edited',edited))
        c.create.assert_not_called()
