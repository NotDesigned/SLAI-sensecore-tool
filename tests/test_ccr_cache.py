import json
from pathlib import Path
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock, patch
from scripts import ccr,ccr_cache,cli

CFG={'account':{'access_key_id':'fixture-ak','access_key_secret':'fixture-secret'}}
NS=dict(name='shared',region='cn-sh-01',subscription_name='sub',resource_group_name='group',zone='cn-sh-01z',uid='ns-uid')
ROWS=[dict(name='shared/app',domain='registry.example',tags=['v1','v2'])]

class RepositoryCacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        patcher=patch.object(cli,'ROOT',Path(self.tmp.name));patcher.start();self.addCleanup(patcher.stop)

    def test_new_source_and_image_catalog_share_persisted_snapshot(self):
        with patch.object(ccr,'repositories',return_value=ROWS) as fetch:
            self.assertEqual(ccr.RepositorySource(CFG,NS).page(0,20).total,1)
            source=ccr.ImageCatalog(CFG).source(NS)
            self.assertEqual(source.page(0,20,'v2').rows,['registry.example/shared/app:v2'])
            self.assertIn('本地缓存',source.status_hint)
        fetch.assert_called_once()
        for path in cli.ROOT.rglob('*.json'):
            self.assertNotIn('fixture-secret',path.read_text());self.assertNotIn('fixture-ak',path.read_text())

    def test_force_refresh_and_expiry_query_again(self):
        fetch=Mock(return_value=ROWS)
        with patch.object(ccr_cache.time,'time',return_value=1000):
            ccr_cache.load(CFG,NS,fetch);ccr_cache.load(CFG,NS,fetch)
            self.assertEqual(fetch.call_count,1)
            ccr_cache.load(CFG,NS,fetch,refresh=True)
            self.assertEqual(fetch.call_count,2)
        with patch.object(ccr_cache.time,'time',return_value=1000+ccr_cache.TTL+1):
            ccr_cache.load(CFG,NS,fetch)
        self.assertEqual(fetch.call_count,3)

    def test_account_region_and_replaced_namespace_are_isolated(self):
        fetch=Mock(return_value=ROWS)
        ccr_cache.load(CFG,NS,fetch)
        ccr_cache.load({'account':dict(CFG['account'],access_key_secret='different')},NS,fetch)
        ccr_cache.load(CFG,dict(NS,region='cn-sh-02'),fetch)
        ccr_cache.load(CFG,dict(NS,uid='replaced'),fetch)
        self.assertEqual(fetch.call_count,4)

    def test_failed_refresh_keeps_previous_complete_result(self):
        source=ccr.RepositorySource(CFG,NS)
        with patch.object(ccr,'repositories',return_value=ROWS):source.page(0,20)
        with patch.object(ccr,'repositories',side_effect=cli.ConfigError('offline')):
            with self.assertRaises(cli.ConfigError):source.page(0,20,refresh=True)
            self.assertEqual(source.page(0,20).rows,ROWS)
            self.assertEqual(ccr.RepositorySource(CFG,NS).page(0,20).rows,ROWS)

    def test_successful_upload_invalidates_only_matching_namespace(self):
        fetch=Mock(return_value=ROWS)
        ccr_cache.load(CFG,NS,fetch);ccr_cache.load(CFG,dict(NS,name='other'),fetch)
        ccr_cache.invalidate(CFG,'registry.cn-sh-01.sensecore.cn','shared')
        ccr_cache.load(CFG,dict(NS,name='other'),fetch)
        self.assertEqual(fetch.call_count,2)
        ccr_cache.load(CFG,NS,fetch);self.assertEqual(fetch.call_count,3)

    def test_corrupt_cache_is_refetched(self):
        fetch=Mock(return_value=ROWS);ccr_cache.load(CFG,NS,fetch)
        next(cli.ROOT.rglob('*.json')).write_text('invalid')
        self.assertEqual(ccr_cache.load(CFG,NS,fetch)[0],ROWS)
        self.assertEqual(fetch.call_count,2)

    def test_concurrent_readers_share_one_fetch(self):
        started=threading.Event();release=threading.Event()
        def fetch():started.set();self.assertTrue(release.wait(2));return ROWS
        counted=Mock(side_effect=fetch)
        with ThreadPoolExecutor(max_workers=2) as pool:
            first=pool.submit(ccr_cache.load,CFG,NS,counted)
            self.assertTrue(started.wait(2))
            second=pool.submit(ccr_cache.load,CFG,NS,counted)
            release.set();self.assertEqual(first.result()[0],second.result()[0])
        counted.assert_called_once()

class RefreshUiTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_refresh_leaves_visible_rows_and_reports_old_result(self):
        from scripts.tui import SlaiApp,Browser
        from scripts.listing import LocalSource
        from textual.widgets import DataTable,Static
        source=LocalSource(Mock(side_effect=[['existing-image'],cli.ConfigError('offline')]))
        app=SlaiApp()
        with patch.object(cli,'menu_title',return_value='SLAI-tool'), patch('scripts.proxy_settings.status',return_value='SOCKS5：测试状态'):
            async with app.run_test() as pilot:
                browser=Browser('镜像',source,lambda row:None)
                await app.push_screen(browser)
                for _ in range(40):
                    await pilot.pause(.025)
                    if not browser.fetching:break
                await pilot.press('r')
                for _ in range(40):
                    await pilot.pause(.025)
                    if not browser.fetching and source.fetch.call_count==2:break
                self.assertEqual(browser.page.rows,['existing-image'])
                self.assertEqual(browser.query_one(DataTable).row_count,1)
                self.assertIn('保留上次结果',str(browser.query_one('#counter',Static).render()))
