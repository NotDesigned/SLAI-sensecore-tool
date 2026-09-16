"""Creation defaults, upload boundaries and automatic local dependency handling."""
import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from scripts import acp, cci, cci_api, cli, cloud, copy_draft, docker_registry, forms, network, ui, workspace

WS=dict(name='ws',region='cn-sh-01',subscription_name='sub',resource_group_name='group',zone='cn-sh-01z')
# A recorded workspaceAEC2Bindings row, trimmed only of fields the form never reads.
POOLS=[dict(name='computing-cluster-01e',display_name='computing_cluster_01e',id='pool-id',uid='pool-uid',
            zone='cn-sh-01e',state='ACTIVE',properties={'vpc_id':'vpc'},quota_type='ALL',
            reserved_cpu='2662.00',reserved_number='69',reserved_memory='23251.00Gi',
            spot_status=[{'spot_name':'default','spot_quota':{'cpu':'1514.00','device':'6','memory':'10088.00Gi'}}]),
       dict(name='debug-mig-cluster-01e',display_name='debug_mig_cluster_01e',id='debug-id',uid='debug-uid',
            zone='cn-sh-01e',state='ACTIVE',properties={'vpc_id':'vpc'},quota_type='SPOT',
            spot_status=[{'spot_name':'default','spot_quota':{'device':'2'}}])]
SPECS=[{'WORKER SPEC':'small','ZONE':'cn-sh-01e','VCPU COUNT':'2','MEMORY(GIB)':'4','CHIP COUNT':'0','CHIP MODEL':'N6lS'},
       {'WORKER SPEC':'gpu-8','ZONE':'cn-sh-01e','VCPU COUNT':'176','MEMORY(GIB)':'1840','CHIP COUNT':'8','CHIP MODEL':'N6lS'}]
ACP_DOC={'resource_pool':{'name':'computing-cluster-01e','available_zone':'cn-sh-01e'},
         'roles':[{'resource_spec':[{'name':'small'}],'total_replicas':1,'startup_script':'run','image_path':'img'}]}
ID='sha256:'+'a'*64

def client():
    c=Mock(config={'cci':{'ssh_enabled':False}})
    c.identity_data.return_value={'id':'owner'}
    c.workspace_record.return_value=WS
    c.clusters.return_value=[dict(name='pool',id='pool-id',uid='pool-uid',zone='cn-sh-01e',properties={'vpc_id':'vpc'})]
    c.specs.return_value=[{'ZONE':'cn-sh-01e','WORKER SPEC':'small','VCPU COUNT':'2','MEMORY(GIB)':'4','CHIP COUNT':'0'}]
    c.resources.return_value=[]
    return c

class CreationTests(unittest.TestCase):
    def test_quota_labels_defaults_and_acp_last_options(self):
        c=client()
        for kind,quota,label in [('cci','RESERVED','预留资源'),('acp','SPOT','闲时资源')]:
            draft=forms.CreateDraft(kind,c,WS);draft.initialize()
            self.assertEqual(draft.values['quota'],quota)
            self.assertEqual(next(value for key,_,value in draft.rows() if key=='quota'),label)
        draft.values.update(command='/entry.sh python "my train.py"',entrypoint=True,mounts=[],framework='mpi',nodes=3)
        saved=draft.snapshot()
        import tomlkit
        tomlkit.dumps({'acp':{'last':saved}})  # No TOML null values.
        restored=forms.CreateDraft('acp',c,WS,previous=saved);restored.initialize()
        body=restored.build()[1]
        self.assertEqual(body['framework'],'MPI')
        self.assertEqual(body['roles'][0]['total_replicas'],3)
        self.assertEqual(body['roles'][0]['startup_script'],"exec /entry.sh python 'my train.py'")
        self.assertEqual(next(label for key,label,_ in restored.rows() if key=='command'),'入口程序及参数')
        self.assertNotEqual(restored.values['name'],draft.values['name'])

    def test_cci_upload_only_after_submit_and_failure_prevents_creation(self):
        for save,fail in [(True,False),(False,True),(False,False)]:
            c=client();draft=Mock(save_requested=save,submit_requested=True,defaults=c.config['cci'])
            draft.snapshot.return_value={}
            if fail:draft.sync_image.side_effect=cli.ConfigError('upload failed')
            doc={'template':{'containers':[]}}
            with tempfile.TemporaryDirectory() as tmp, patch.object(cli,'ROOT',Path(tmp)), \
                 patch.object(cloud,'Client',return_value=c), patch.object(workspace,'select',return_value=WS), \
                 patch.object(forms,'CreateDraft',return_value=draft), patch.object(ui,'creation_form',return_value=('ws','new-cci','',doc,None)), \
                 patch.object(cli,'save_config_updates'), patch.object(cci_api,'create') as create:
                if fail:
                    with self.assertRaisesRegex(cli.ConfigError,'upload failed'):cci.create(c.config)
                else:cci.create(c.config)
                if save:draft.sync_image.assert_not_called()
                else:draft.sync_image.assert_called_once()
                self.assertEqual(create.call_count, int(not save and not fail))

    def test_acp_save_and_failed_upload_never_create(self):
        for save in (True,False):
            c=client();draft=Mock(save_requested=save,submit_requested=True,defaults={})
            draft.snapshot.return_value={};draft.sync_image.side_effect=cli.ConfigError('upload failed')
            with tempfile.TemporaryDirectory() as tmp, patch.object(cli,'ROOT',Path(tmp)), patch.object(cli,'save_config_updates'):
                if save:acp.confirm_submit(c,'ws','new',{},draft=draft)
                else:
                    with self.assertRaises(cli.ConfigError):acp.confirm_submit(c,'ws','new',{},draft=draft)
            c.create.assert_not_called()

class PickerTableTests(unittest.TestCase):
    def counts(self, pool):
        """Read the card columns by header, so a new column cannot silently shift the assertion."""
        cells=dict(zip(cloud.POOL_COLUMNS,cloud.pool_cells(pool)))
        return (cells['\u5269\u4f59\u5361\u6570'],cells['\u95f2\u65f6\u989d\u5ea6'])

    def picker(self, draft, key):
        captured={}
        def choose(label,rows,describe,default=None,header=''):
            captured.update(label=label,header=header,default=default,lines=[describe(row) for row in rows])
            return rows[0]
        with patch.object(ui,'choose',side_effect=choose):draft.edit(key)
        return captured

    def test_acp_create_and_copy_pickers_render_a_pool_table(self):
        c=client();c.clusters.return_value=copy.deepcopy(POOLS)
        for draft in (forms.CreateDraft('acp',c,WS),copy_draft.CopyDraft('acp',c,WS,ACP_DOC,'src')):
            shown=self.picker(draft,'cluster')
            self.assertEqual(shown['label'],'\u8d44\u6e90\u6c60')
            self.assertEqual(shown['header'],
                '\u540d\u79f0                   \u6807\u8bc6                   \u53ef\u7528\u533a     \u5269\u4f59\u5361\u6570  \u95f2\u65f6\u989d\u5ea6')
            # Alias and submitted name get their own columns instead of one parenthesised cell.
            self.assertEqual(shown['lines'],
                ['computing_cluster_01e  computing-cluster-01e  cn-sh-01e        69         6',
                 'debug_mig_cluster_01e  debug-mig-cluster-01e  cn-sh-01e         -         2'])

    def test_copy_keeps_the_source_pool_selected_and_blocks_multi_role_edits(self):
        c=client();c.clusters.return_value=copy.deepcopy(POOLS);c.specs.return_value=copy.deepcopy(SPECS)
        draft=copy_draft.CopyDraft('acp',c,WS,ACP_DOC,'src');draft.initialize()
        # The picker must open on the pool the source used, not silently on the first row.
        self.assertEqual(self.picker(draft,'cluster')['default']['name'],'computing-cluster-01e')
        document=copy.deepcopy(ACP_DOC);document['roles']=document['roles']*2
        multi=copy_draft.CopyDraft('acp',c,WS,document,'src');multi.initialize()
        with self.assertRaisesRegex(cli.ConfigError,'\u4fdd\u7559\u8d44\u6e90\u6c60'):multi.edit('cluster')

    def test_spec_picker_renders_a_table(self):
        c=client();c.clusters.return_value=copy.deepcopy(POOLS);c.specs.return_value=copy.deepcopy(SPECS)
        draft=forms.CreateDraft('acp',c,WS);self.picker(draft,'cluster')
        shown=self.picker(draft,'spec')
        self.assertEqual(shown['label'],'\u5b9e\u4f8b\u89c4\u683c')
        self.assertEqual(shown['header'],'\u540d\u79f0   vCPU  \u5185\u5b58(GiB)  \u52a0\u901f\u5361  \u5361\u578b\u53f7')
        # A CPU-only specification must not advertise a chip model.
        self.assertEqual(shown['lines'],['small     2          4       0  -',
                                         'gpu-8   176       1840       8  N6lS'])

    def test_columns_align_by_terminal_width_not_character_count(self):
        header,lines=ui.aligned(('\u540d\u79f0','\u5269\u4f59'),[('\u8d44\u6e90\u6c60\u7532','7'),('pool','70')],right=(1,))
        self.assertEqual([header,*lines],['\u540d\u79f0      \u5269\u4f59',
                                          '\u8d44\u6e90\u6c60\u7532     7','pool        70'])
        # Every row ends flush, which len() on CJK text would silently break.
        self.assertEqual({ui.cell_width(line) for line in (header,*lines)},{14})

    def test_overlong_names_are_clipped_instead_of_wrapping_the_table(self):
        _,lines=ui.aligned(('\u540d\u79f0',),[('\u8d44\u6e90\u6c60\u7532\u4e59\u4e19\u4e01\u620a\u5df1\u5e9a\u8f9b',)],limit=12)
        self.assertEqual(lines,['\u8d44\u6e90\u6c60\u7532\u4e59\u2026'])
        self.assertLessEqual(ui.cell_width(lines[0]),12)

    def test_unusable_counts_read_as_unknown_and_zero_is_still_reported(self):
        base=dict(name='pool',zone='cn-sh-01e')
        # Negative control: nothing known must read as unknown, never as zero.
        self.assertEqual(self.counts(base),('-','-'))
        for bad in ('n/a','','-3','nan','inf','1e400'):
            self.assertEqual(self.counts({**base,'reserved_number':bad}),('-','-'),bad)
        self.assertEqual(self.counts({**base,'reserved_number':'0','spot_status':[]}),('0','-'))
        # Fractions are reported, not truncated into a tidier-looking integer.
        self.assertEqual(self.counts({**base,'reserved_number':'6.9'})[0],'6.90')
        self.assertEqual(self.counts({**base,'reserved_number':'69.00'})[0],'69')

    def test_unreadable_spot_shares_make_the_total_unknown_instead_of_understating_it(self):
        base=dict(name='pool',zone='cn-sh-01e',reserved_number='69')
        self.assertEqual(self.counts({**base,'spot_status':[{'spot_quota':{'device':'4'}},
                                                            {'spot_quota':{'device':'2'}}]})[1],'6')
        for broken in ([{'spot_quota':None}],[{'spot_quota':{'device':'4'}},'oops'],
                       [{'spot_quota':{}}],{'spot_quota':{'device':'4'}},'spot'):
            self.assertEqual(self.counts({**base,'spot_status':broken})[1],'-',broken)

    def test_remaining_cards_are_not_split_by_quota_type(self):
        # The binding reports one remaining figure; a SPOT-only pool still has one.
        pool=dict(name='pool',zone='cn-sh-01e',quota_type='SPOT',reserved_number='9',
                  spot_status=[{'spot_quota':{'device':'2'}}])
        self.assertEqual(self.counts(pool),('9','2'))
        self.assertEqual(cloud.POOL_COLUMNS[-2:],('\u5269\u4f59\u5361\u6570','\u95f2\u65f6\u989d\u5ea6'))

    def test_pools_are_ordered_by_remaining_cards_with_unknown_last(self):
        pool=lambda name,remaining: dict(name=name,zone='cn-sh-01e',
            **({'reserved_number':remaining} if remaining is not None else {}))
        rows=[pool('b-few','26'),pool('a-unknown',None),pool('c-most','192'),
              pool('d-zero','0'),pool('a-tie','26'),pool('b-unknown',None)]
        order=[p['name'] for p in sorted(rows,key=cloud.pool_order)]
        # Unknown sorts last, not as zero; ties fall back to the name.
        self.assertEqual(order,['c-most','a-tie','b-few','d-zero','a-unknown','b-unknown'])

    def test_specs_are_ordered_by_card_count_then_vcpu(self):
        def spec(name,vcpu,cards):
            return {'name':name,'cpu':{'vcpu_allocatable':vcpu,'type':'x'},'memory':{'allocatable':16},
                    'device':{'number':cards,'resource_key':'nvidia.com/gpu' if cards else '','type':'N6lS'},
                    'zones':['cn-sh-01e']}
        raw={'resource_specs':[spec('gpu8',176,8),spec('cpu2',2,0),spec('gpu1-big',22,1),
                               spec('cpu64',64,0),spec('gpu1-small',8,1)]}
        rows=cloud.decode_specs(raw,'cn-sh-01e')
        self.assertEqual([r['WORKER SPEC'] for r in rows],
                         ['cpu2','cpu64','gpu1-small','gpu1-big','gpu8'])

    def test_pool_without_an_alias_repeats_its_name_rather_than_blanking_a_column(self):
        cells=dict(zip(cloud.POOL_COLUMNS,cloud.pool_cells(dict(name='public',zone='cn-sh-01e'))))
        self.assertEqual((cells['\u540d\u79f0'],cells['\u6807\u8bc6']),('public','public'))
        alias=dict(name='share-cluster',display_name='computing_cluster',zone='cn-sh-01g')
        cells=dict(zip(cloud.POOL_COLUMNS,cloud.pool_cells(alias)))
        self.assertEqual((cells['\u540d\u79f0'],cells['\u6807\u8bc6']),('computing_cluster','share-cluster'))

    def test_client_orders_the_binding_list_before_caching_it(self):
        rows=[dict(name=name,uid='uid-'+name,state='ACTIVE',reserved_number=remaining,
                   id=f'/subscriptions/sub/resourceGroups/group/zones/cn-sh-01e/aec2s/{name}')
              for name,remaining in (('low','5'),('high','50'),('unknown',None))]
        api=cloud.Client({})
        with patch.object(cloud.rest,'pages',return_value=copy.deepcopy(rows)):
            self.assertEqual([p['name'] for p in api.clusters(WS)],['high','low','unknown'])

    def test_row_outside_the_table_falls_back_to_a_single_line_label(self):
        _,describe=cloud.pool_table(copy.deepcopy(POOLS))
        self.assertEqual(describe({'name':'other','zone':'cn-sh-01e','reserved_number':'3'}),
                         'other \u00b7 cn-sh-01e \u00b7 \u5269\u4f59\u5361\u6570 3')


class ImageSyncTests(unittest.TestCase):
    def plan(self):
        return dict(registry='registry.example',namespace='shared',source_image='my/app:v2',
                    image_name='app',tag='v2',source_id=ID,target='registry.example/shared/app:v2')

    def test_plan_preserves_name_tag_without_upload(self):
        from scripts import ccr
        with patch.object(docker_registry,'inspect_local',return_value=ID), \
             patch.object(ccr,'select_upload_namespace',return_value='shared'), patch.object(docker_registry,'run_push') as push:
            plan=docker_registry.plan_sync({'docker':{'registry':'registry.example'}},'my/app:v2')
        self.assertEqual(plan,self.plan());push.assert_not_called()

    def test_push_output_redacts_signed_storage_urls(self):
        import contextlib,io
        child=Mock(stdout=iter(['failed https://storage.example/blob?signature=secret&token=hidden 403\n']))
        child.wait.return_value=1
        manager=Mock();manager.__enter__=Mock(return_value=child);manager.__exit__=Mock(return_value=False)
        with patch.object(subprocess,'Popen',return_value=manager), contextlib.redirect_stdout(io.StringIO()) as output:
            code,_=docker_registry.run_push('docker','registry/image:v1',{})
        self.assertEqual(code,1)
        self.assertIn('storage.example/blob?[redacted]',output.getvalue())
        self.assertNotIn('secret',output.getvalue());self.assertNotIn('hidden',output.getvalue())

    def test_changed_local_tag_blocks_before_tag_or_push(self):
        with patch.object(docker_registry,'inspect_local',return_value='sha256:'+'b'*64), \
             patch.object(subprocess,'run') as tag, patch.object(docker_registry,'run_push') as push:
            with self.assertRaises(cli.ConfigError):docker_registry.sync_image({},self.plan())
        tag.assert_not_called();push.assert_not_called()

    def test_upload_uses_selected_id_and_existing_credentials(self):
        with patch.object(docker_registry,'inspect_local',return_value=ID), \
             patch.object(docker_registry,'has_credentials',return_value=True), \
             patch.object(docker_registry,'login') as login, patch.object(subprocess,'run',return_value=Mock(returncode=0)) as tag, \
             patch.object(docker_registry,'run_push',return_value=(0,False)), patch('shutil.which',return_value='/docker'):
            result=docker_registry.sync_image({},self.plan())
        self.assertEqual(tag.call_args.args[0],['/docker','tag',ID,self.plan()['target']])
        self.assertEqual(result,self.plan()['target']);login.assert_not_called()

    def test_arm_image_requires_rebuild(self):
        result=Mock(returncode=0,stdout=json.dumps(ID)+' "linux" "arm64"')
        with patch('shutil.which',return_value='/docker'), patch.object(subprocess,'run',return_value=result):
            with self.assertRaisesRegex(cli.ConfigError,'linux/amd64'):docker_registry.inspect_local('local:v1')

class NcatInstallTests(unittest.TestCase):
    def test_existing_ncat_does_not_install(self):
        with patch.object(network,'find_ncat',return_value='/ncat'), patch.object(subprocess,'run') as run:
            self.assertEqual(network.ensure_ncat(),'/ncat')
        run.assert_not_called()

    def test_macos_installs_nmap_then_rescans(self):
        with patch.object(sys,'platform','darwin'), patch('shutil.which',return_value='/brew'), \
             patch.object(network,'find_ncat',side_effect=[None,'/ncat']), patch.object(subprocess,'run',return_value=Mock(returncode=0)) as run:
            self.assertEqual(network.ensure_ncat(),'/ncat')
        self.assertEqual(run.call_args.args[0],['/brew','install','nmap'])
        self.assertEqual(run.call_args.kwargs['stdin'],subprocess.DEVNULL)

    def test_linux_never_waits_for_sudo_password(self):
        with patch.object(sys,'platform','linux'), patch.object(os,'geteuid',return_value=1000,create=True), \
             patch('shutil.which',side_effect=lambda name:'/usr/bin/'+name):
            commands=network.ncat_install_commands()
        self.assertEqual(commands[-1],['/usr/bin/sudo','-n','/usr/bin/apt-get','install','-y','ncat'])

    def test_windows_uses_explicit_package(self):
        with patch.object(sys,'platform','win32'), patch('shutil.which',return_value='winget.exe'):
            command=network.ncat_install_commands()[0]
        self.assertIn('Insecure.Nmap',command);self.assertIn('--disable-interactivity',command)

    def test_failed_install_displays_manual_command(self):
        with patch.object(network,'find_ncat',return_value=None), patch.object(network,'ncat_install_commands',return_value=[['installer']]), \
             patch.object(subprocess,'run',return_value=Mock(returncode=1)), patch.object(ui,'show_text') as show:
            self.assertIsNone(network.ensure_ncat())
        show.assert_called_once()
