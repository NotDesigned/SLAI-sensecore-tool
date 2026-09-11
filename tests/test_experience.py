"""Regression checks for action refresh, navigation and read cancellation."""
import asyncio
import threading
import tempfile
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from scripts import cli, ui, ccr_cache, docker_registry, ccr, rest
from scripts.listing import LocalSource
from scripts.tui import SlaiApp, Browser, Operation, Bridge
from textual.widgets import Button

CFG={'account':{'access_key_id':'fixture','access_key_secret':'fixture'}}
NS=dict(name='ns',region='cn-sh-01',uid='ns-id')


class DataTests(unittest.TestCase):
    def test_both_upload_flows_replace_old_namespace(self):
        for source in ('registry.cn-sh-01.sensecore.cn/old/model:v1','old/model:v1'):
            with patch.object(docker_registry,'inspect_local',return_value='sha256:'+'a'*64), patch.object(ccr,'select_upload_namespace',return_value='new'):
                plan=docker_registry.plan_sync(CFG,source)
            self.assertEqual(plan['image_name'],docker_registry.upload_defaults(source)[0])
            self.assertEqual(plan['target'],'registry.cn-sh-01.sensecore.cn/new/model:v1')

    def test_snapshot_invalidation_is_remembered_and_new_snapshot_invalidates(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(cli,'ROOT',Path(folder)):
            fetch=Mock(return_value=[{'name':'model'}])
            ccr_cache.load(CFG,NS,fetch)
            ccr_cache.invalidate_snapshot(CFG,'registry.cn-sh-01.sensecore.cn','ns',['app','snapshot1'])
            ccr_cache.load(CFG,NS,fetch)
            ccr_cache.invalidate_snapshot(CFG,'registry.cn-sh-01.sensecore.cn','ns',['app','snapshot1'])
            ccr_cache.load(CFG,NS,fetch)
            self.assertEqual(fetch.call_count,2)
            ccr_cache.invalidate_snapshot(CFG,'registry.cn-sh-01.sensecore.cn','ns',['app','snapshot2'])
            ccr_cache.load(CFG,NS,fetch)
            self.assertEqual(fetch.call_count,3)


class ExperienceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        for target,value in [('scripts.cli.menu_title','audit'),('scripts.proxy_settings.status','audit')]:
            p=patch(target,return_value=value);p.start();self.addCleanup(p.stop)

    async def settle(self,pilot,check):
        for _ in range(200):
            await pilot.pause(.025)
            if pilot.app.screen.is_mounted and check():
                return
        self.fail('UI did not settle')

    async def test_shrinking_last_page_moves_to_existing_page(self):
        source=LocalSource(Mock(side_effect=[list(range(21)),list(range(20))]))
        app=SlaiApp()
        async with app.run_test() as pilot:
            b=Browser('rows',source,lambda _:None)
            await app.push_screen(b)
            await self.settle(pilot,lambda:b.page is not None and not b.fetching)
            b.action_next()
            await self.settle(pilot,lambda:not b.fetching and b.index==1)
            b.action_refresh()
            await self.settle(pilot,lambda:not b.fetching and b.index==0)
            self.assertEqual(len(b.page.rows),20)

    async def test_failed_next_page_restores_displayed_index(self):
        source=Mock(columns=('row',),states=(),search_hint='search',loading_hint='loading',status_hint='',refresh_label='刷新')
        source.cells=lambda row:(row,)
        from scripts.listing import Page
        source.page.side_effect=[Page(['first'],True,21),cli.ConfigError('offline')]
        app=SlaiApp()
        async with app.run_test() as pilot:
            b=Browser('rows',source,lambda _:None)
            await app.push_screen(b)
            await self.settle(pilot,lambda:b.page is not None and not b.fetching)
            b.action_next()
            await self.settle(pilot,lambda:not b.fetching)
            self.assertEqual(b.index,0)
            self.assertEqual(b.page.rows,['first'])

    async def test_readonly_return_keeps_snapshot_but_uncertain_write_refreshes(self):
        for changed in (False,True):
            source=LocalSource(Mock(return_value=['row']))
            def action():
                if changed:
                    ui.mark_changed()
                    raise cli.ConfigError('写入结果未知')
            app=SlaiApp()
            async with app.run_test() as pilot:
                b=Browser('rows',source,lambda _:None)
                await app.push_screen(b)
                await self.settle(pilot,lambda:b.page is not None and not b.fetching)
                app.push_screen(Operation('action',action),lambda result:b.load(refresh=True) if result else None)
                await self.settle(pilot,lambda:isinstance(app.screen,Operation) and app.screen.done)
                await pilot.press('0')
                await self.settle(pilot,lambda:app.screen is b and not b.fetching)
                self.assertEqual(source.fetch.call_count,2 if changed else 1)

    async def test_creation_available_while_loading_and_mutation_refresh_not_lost(self):
        release=threading.Event(); self.addCleanup(release.set)
        def fetch():
            release.wait(10)
            return []
        source=LocalSource(Mock(side_effect=fetch))
        def create():
            ui.mark_changed()
        app=SlaiApp()
        async with app.run_test() as pilot:
            b=Browser('rows',source,lambda _:None,actions=(('create','Create',create),))
            await app.push_screen(b)
            await self.settle(pilot,lambda:b.fetching and b.query_one('#service-create').region.width>0)
            self.assertFalse(b.query_one('#service-create',Button).disabled)
            await pilot.click('#service-create')
            await self.settle(pilot,lambda:isinstance(app.screen,Operation) and app.screen.done)
            await pilot.press('0')
            release.set()
            await self.settle(pilot,lambda:app.screen is b and not b.fetching and source.fetch.call_count==2)

    async def test_canceled_read_cannot_later_submit_mutation(self):
        release=threading.Event();started=threading.Event(); finished=threading.Event()
        self.addCleanup(release.set)
        def action():
            started.set()
            release.wait(3)
            try:
                rest.request_json(CFG,'https://example.invalid',method='POST',body={})
            finally:
                finished.set()
        app=SlaiApp()
        with patch.object(rest.urllib.request,'urlopen') as send:
            async with app.run_test() as pilot:
                app.open_operation('read',action)
                await self.settle(pilot,lambda:started.is_set())
                await pilot.press('0')
                await self.settle(pilot,lambda:len(app.screen_stack)==1)
                release.set()
                await self.settle(pilot,lambda:finished.is_set())
        send.assert_not_called()

    async def test_catalog_cache_is_scoped_and_explicitly_invalidated(self):
        app=SlaiApp();fetch=Mock(return_value={'id':'one'})
        async with app.run_test() as pilot:
            bridge=Bridge(app,owner=app.screen)
            a=bridge.catalog_read(CFG,'catalog',None,fetch)
            a['id']='edited'
            self.assertEqual(bridge.catalog_read(CFG,'catalog',None,fetch),{'id':'one'})
            self.assertEqual(fetch.call_count,1)
            bridge.catalog_read({'account':dict(CFG['account'],access_key_secret='other')},'catalog',None,fetch)
            app.clear_catalog()
            bridge.catalog_read(CFG,'catalog',None,fetch)
            self.assertEqual(fetch.call_count,3)

    async def test_submitted_write_waits_for_result_before_return(self):
        release=threading.Event();started=threading.Event()
        self.addCleanup(release.set)
        def action():
            ui.mark_changed()
            started.set()
            release.wait(3)
        app=SlaiApp()
        async with app.run_test() as pilot:
            app.open_operation('write',action)
            await self.settle(pilot,lambda:started.is_set())
            operation=app.screen
            await pilot.press('0')
            self.assertIs(app.screen,operation)
            self.assertTrue(operation.query_one('#back',Button).disabled)
            release.set()
            await self.settle(pilot,lambda:operation.done)
            await pilot.press('0')
            await self.settle(pilot,lambda:len(app.screen_stack)==1)

    async def test_read_budget_and_catalog_expiry(self):
        import time
        app=SlaiApp();fetch=Mock(return_value={'id':'new'})
        async with app.run_test() as pilot:
            bridge=Bridge(app,owner=app.screen)
            bridge.catalog_read(CFG,'catalog',None,fetch)
            key=next(iter(app.catalog))
            app.catalog[key]=(time.monotonic()-31,{'id':'old'})
            self.assertEqual(bridge.catalog_read(CFG,'catalog',None,fetch),{'id':'new'})
            self.assertEqual(fetch.call_count,2)
            bridge.deadline=time.monotonic()-1
            with self.assertRaisesRegex(cli.ConfigError,'等待过久'):
                bridge.request_timeout(20)


class DefaultChoiceTests(unittest.TestCase):
    def test_text_menu_enter_chooses_first_action_instead_of_return(self):
        for default in (None,'返回列表'):
            with patch('builtins.input',return_value=''):
                self.assertEqual(ui.choose('操作',['返回列表','连接','停止'],default=default),'连接')

    def test_saved_selection_and_destructive_confirmation_are_preserved(self):
        with patch('builtins.input',return_value=''):
            self.assertEqual(ui.choose('配额',['预留','闲时'],default='闲时'),'闲时')
            self.assertEqual(ui.choose('确认删除',['取消','删除'],default='取消'),'取消')


class DefaultChoiceUiTests(unittest.IsolatedAsyncioTestCase):
    setUp = ExperienceTests.setUp
    settle = ExperienceTests.settle

    async def test_return_default_focuses_first_action_and_enter_selects_it(self):
        from scripts.tui import Picker
        from textual.widgets import OptionList
        app=SlaiApp();selected=[]
        async with app.run_test() as pilot:
            await app.push_screen(Picker('操作',['连接','停止'],str,'返回列表','返回列表'),selected.append)
            await self.settle(pilot,lambda:isinstance(app.screen.focused,OptionList))
            self.assertEqual(app.screen.query_one(OptionList).highlighted,0)
            await pilot.press('enter')
            self.assertEqual(selected,['连接'])

    async def test_acp_state_dropdown_shows_all_options_and_selects_last(self):
        from scripts.acp import JobSource
        from scripts.listing import Page
        from textual.widgets import Select
        client=Mock()
        client.jobs_page.return_value=Page([],False,0)
        app=SlaiApp()
        async with app.run_test(size=(80,24)) as pilot:
            browser=Browser('ACP',JobSource(client,'ws'),lambda _:None)
            await app.push_screen(browser)
            await self.settle(pilot,lambda:not browser.fetching and browser.query_one('#state').region.width>0)
            await pilot.click('#state')
            await self.settle(pilot,lambda:browser.query_one(Select).expanded and browser.query_one('SelectOverlay').region.height>0)
            overlay=browser.query_one('SelectOverlay')
            self.assertEqual(overlay.option_count,6)
            self.assertGreaterEqual(overlay.content_region.height,overlay.option_count)
            await pilot.press('end','enter')
            await self.settle(pilot,lambda:not browser.fetching and client.jobs_page.call_args.args[-1]=='FAILED')
