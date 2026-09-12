import asyncio
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock,patch

from scripts import ccr,ccr_cache,cli,listing
from scripts.listing import CachedSource,LocalSource,Page,resource_key
from scripts.tui import SlaiApp,Browser
from textual.widgets import Input,DataTable

CFG={'account':{'access_key_id':'fixture','access_key_secret':'fixture'}}
NS=dict(name='ns',region='cn-sh-01',uid='uid')
OLD=[dict(name='ns/old',domain='registry.example',tags=['v1'])]
NEW=[dict(name='ns/new',domain='registry.example',tags=['v2'])]


def wait_for(check, timeout=3):
    until=time.monotonic()+timeout
    while time.monotonic()<until:
        if check():
            return
        time.sleep(.01)
    raise AssertionError('Background fetch did not complete')


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        patcher=patch.object(cli,'ROOT',Path(self.temp.name))
        patcher.start();self.addCleanup(patcher.stop)
        self.key=resource_key('cci',CFG,self.temp.name)

    def test_stale_image_cache_is_immediate_and_concurrent_reads_share_refresh(self):
        ccr_cache.load(CFG,NS,lambda:OLD)
        path,_=ccr_cache.location(CFG,NS)
        import json
        entry=json.loads(path.read_text());entry['updated_at']-=301
        path.write_text(json.dumps(entry))
        started=threading.Event();release=threading.Event()
        def fetch(*args):
            started.set();release.wait(3);return NEW
        count=Mock(side_effect=fetch)
        self.addCleanup(release.set)
        with patch.object(ccr,'repositories',count):
            a=ccr.RepositorySource(CFG,NS);b=ccr.RepositorySource(CFG,NS)
            a.enable_background();b.enable_background()
            self.assertEqual(a.page(0,20).rows,OLD)
            self.assertTrue(started.wait(1))
            self.assertEqual(b.page(0,20).rows,OLD)
            self.assertEqual(count.call_count,1)
            self.assertIn('后台',a.status_hint)
            release.set()
            wait_for(lambda:not ccr_cache.background_state(CFG,NS)[0])
            self.assertEqual(a.page(0,20).rows,NEW)

    def test_invalidation_does_not_wait_and_discards_old_refresh(self):
        ccr_cache.load(CFG,NS,lambda:OLD)
        started=threading.Event();release=threading.Event()
        def fetch():
            started.set();release.wait(3);return OLD
        self.addCleanup(release.set)
        ccr_cache.refresh_background(CFG,NS,fetch)
        self.assertTrue(started.wait(1))
        ccr_cache.invalidate(CFG,'registry.cn-sh-01.sensecore.cn','ns')
        self.assertIsNone(ccr_cache.peek(CFG,NS))
        release.set()
        wait_for(lambda:not ccr_cache.background_state(CFG,NS)[0])
        self.assertIsNone(ccr_cache.peek(CFG,NS))
        ccr_cache.refresh_background(CFG,NS,lambda:NEW)
        wait_for(lambda:not ccr_cache.background_state(CFG,NS)[0])
        self.assertEqual(ccr_cache.peek(CFG,NS)[0],NEW)

    def test_failed_update_retains_stale_data_and_backs_off(self):
        ccr_cache.load(CFG,NS,lambda:OLD)
        failure=Mock(side_effect=cli.ConfigError('offline'))
        ccr_cache.refresh_background(CFG,NS,failure)
        wait_for(lambda:not ccr_cache.background_state(CFG,NS)[0])
        for _ in range(5):
            ccr_cache.refresh_background(CFG,NS,failure)
        self.assertEqual(failure.call_count,1)
        self.assertEqual(ccr_cache.peek(CFG,NS)[0],OLD)
        self.assertIn('offline',ccr_cache.background_state(CFG,NS)[1])

    def test_resource_lists_share_snapshots_and_search_is_local(self):
        fetch=Mock(return_value=['a','b'])
        a=CachedSource(LocalSource(fetch),self.key);a.enable_background()
        a.page(0,20)
        wait_for(lambda:not a._entry()['running'])
        b=CachedSource(LocalSource(fetch),self.key);b.enable_background()
        self.assertEqual(b.page(0,20,'b').rows,['b'])
        self.assertEqual(fetch.call_count,1)
        other=CachedSource(LocalSource(fetch),resource_key('cci',{'account':dict(CFG['account'],access_key_secret='other')},self.temp.name))
        other.enable_background();other.page(0,20)
        wait_for(lambda:not other._entry()['running'])
        self.assertEqual(fetch.call_count,2)

    def test_resource_mutation_discards_inflight_result_and_refreshes(self):
        started=threading.Event();release=threading.Event()
        def first():
            started.set();release.wait(3);return ['old']
        fetch=Mock(side_effect=[first,['new']])
        def read():
            value=fetch()
            return value() if callable(value) else value
        self.addCleanup(release.set)
        source=CachedSource(LocalSource(read),self.key);source.enable_background()
        source.page(0,20);self.assertTrue(started.wait(1))
        listing.invalidate();release.set()
        wait_for(lambda:not source._entry()['running'])
        self.assertIsNone(source._entry()['data'])
        source.page(0,20)
        wait_for(lambda:not source._entry()['running'])
        self.assertEqual(source.page(0,20).rows,['new'])

    def test_prefetch_only_requests_preferred_namespace(self):
        target=dict(NS,state='ACTIVE')
        other=dict(NS,name='other',uid='other-id',state='ACTIVE')
        config={**CFG,'docker':{'registry':'registry.cn-sh-01.sensecore.cn','namespace':'ns'}}
        with patch.object(ccr,'namespaces',return_value=[target,other]),patch.object(ccr,'repositories',return_value=OLD) as fetch:
            ccr.prefetch_default(config)
            wait_for(lambda:fetch.called)
            wait_for(lambda:not ccr_cache.background_state(config,target)[0])
            ccr.prefetch_default(config)
            fetch.assert_called_once_with(config,target)

    def test_background_repository_concurrency_is_bounded(self):
        release=threading.Event();self.addCleanup(release.set)
        def fetch():
            release.wait(3);return OLD
        scopes=[dict(NS,name='scope'+str(i),uid=str(i)) for i in range(3)]
        self.assertTrue(ccr_cache.refresh_background(CFG,scopes[0],fetch))
        self.assertTrue(ccr_cache.refresh_background(CFG,scopes[1],fetch))
        self.assertFalse(ccr_cache.refresh_background(CFG,scopes[2],fetch))
        release.set()
        wait_for(lambda:all(not ccr_cache.background_state(CFG,n)[0] for n in scopes[:2]))
        self.assertTrue(ccr_cache.refresh_background(CFG,scopes[2],lambda:NEW))
        wait_for(lambda:not ccr_cache.background_state(CFG,scopes[2])[0])


class BackgroundUiTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        for target,value in [('scripts.cli.menu_title','test'),('scripts.proxy_settings.status','test')]:
            p=patch(target,return_value=value);p.start();self.addCleanup(p.stop)

    async def settle(self,pilot,check):
        for _ in range(200):
            await pilot.pause(.025)
            if check():
                return
        self.fail('UI did not settle')

    async def test_automatic_update_preserves_search_focus_and_page(self):
        release=threading.Event();self.addCleanup(release.set)
        def fetch():
            release.wait(3)
            return ['new','old']
        key=resource_key('cci',CFG,str(id(self)))
        seed=CachedSource(LocalSource(lambda:['old']),key);seed.enable_background()
        seed.page(0,20)
        wait_for(lambda:not seed._entry()['running'])
        source=CachedSource(LocalSource(fetch),key)
        app=SlaiApp()
        async with app.run_test() as pilot:
            b=Browser('CCI',source,lambda _:None)
            await app.push_screen(b)
            await self.settle(pilot,lambda:b.page is not None and b.page.rows==['old'] and not b.fetching)
            b.action_refresh()
            await self.settle(pilot,lambda:not b.fetching)
            await pilot.press('/')
            field=b.query_one(Input)
            field.value='old'
            release.set()
            await self.settle(pilot,lambda:source._entry()['data']==['new','old'])
            await self.settle(pilot,lambda:b.page.rows==['new','old'] and b.query_one(DataTable).row_count==2)
            self.assertIs(app.focused,field)
            self.assertEqual(field.value,'old')
            self.assertEqual(b.query_one(DataTable).cursor_row,1)

    async def test_cold_remote_second_page_does_not_jump_to_first(self):
        release=threading.Event();self.addCleanup(release.set)
        class Remote:
            columns=('row',);states=();search_hint='search'
            cells=staticmethod(lambda row:(row,))
            def page(self,index,size,query='',state='',refresh=False):
                release.wait(3)
                return Page(['second'],False,21)
        source=CachedSource(Remote(),resource_key('acp',CFG,str(id(self))))
        app=SlaiApp()
        async with app.run_test() as pilot:
            b=Browser('ACP',source,lambda _:None);b.index=1
            await app.push_screen(b)
            await self.settle(pilot,lambda:b.page is not None and not b.fetching)
            self.assertEqual(b.index,1)
            release.set()
            await self.settle(pilot,lambda:b.page.rows==['second'])
            self.assertEqual(b.index,1)
