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
from scripts import acp, cci, cci_api, cli, cloud, docker_registry, forms, network, ui, workspace

WS=dict(name='ws',region='cn-sh-01',subscription_name='sub',resource_group_name='group',zone='cn-sh-01z')
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
