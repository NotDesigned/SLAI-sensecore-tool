import unittest
from unittest.mock import Mock,patch
from scripts import cli,ui
from scripts.listing import LocalSource
from scripts.tui import SlaiApp,Browser,Picker,Details,Operation,Edit
from textual.widgets import DataTable,Static


class ReturnFlowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        for target,value in [('scripts.cli.menu_title','test'),('scripts.proxy_settings.status','test')]:
            p=patch(target,return_value=value);p.start();self.addCleanup(p.stop)

    async def settle(self,pilot,check):
        for _ in range(200):
            await pilot.pause(.025)
            if pilot.app.screen.is_mounted and check():
                return
        self.fail('UI did not settle')

    async def browser(self,app,pilot,callback,actions=()):
        fetch=Mock(return_value=['row'])
        browser=Browser('列表',LocalSource(fetch),lambda _:callback(),actions=actions)
        await app.push_screen(browser)
        await self.settle(pilot,lambda:browser.page is not None and not browser.fetching and browser.query_one(DataTable).region.width>0)
        return browser,fetch

    async def test_return_from_action_menu_skips_finished_screen(self):
        app=SlaiApp()
        async with app.run_test() as pilot:
            browser,fetch=await self.browser(app,pilot,lambda:ui.choose('操作',['返回列表','详情']))
            await pilot.press('enter')
            await self.settle(pilot,lambda:isinstance(app.screen,Picker))
            await pilot.press('0')
            await self.settle(pilot,lambda:app.screen is browser)
            self.assertEqual(fetch.call_count,1)

    async def test_closing_details_or_connection_returns_directly_to_list(self):
        app=SlaiApp()
        async with app.run_test() as pilot:
            def show():
                ui.output('正在检查…')
                ui.show_text('连接信息','ssh example')
            browser,fetch=await self.browser(app,pilot,show)
            await pilot.press('enter')
            await self.settle(pilot,lambda:isinstance(app.screen,Details))
            await pilot.press('0')
            await self.settle(pilot,lambda:app.screen is browser)
            self.assertEqual(fetch.call_count,1)

    async def test_successful_mutation_returns_and_refreshes(self):
        app=SlaiApp()
        async with app.run_test() as pilot:
            def mutate():
                ui.mark_changed()
                ui.output('停止已验证')
            browser,fetch=await self.browser(app,pilot,mutate)
            await pilot.press('enter')
            await self.settle(pilot,lambda:app.screen is browser and fetch.call_count==2 and not browser.fetching)

    async def test_cancel_creation_closes_wrapper_without_refresh(self):
        app=SlaiApp()
        async with app.run_test() as pilot:
            browser,fetch=await self.browser(app,pilot,lambda:None,actions=(('create','创建',lambda:ui.ask('名称')),))
            await pilot.click('#service-create')
            await self.settle(pilot,lambda:isinstance(app.screen,Edit))
            await pilot.press('escape')
            await self.settle(pilot,lambda:app.screen is browser)
            self.assertEqual(fetch.call_count,1)

    async def test_saved_path_and_errors_remain_visible(self):
        for fail in (False,True):
            app=SlaiApp()
            async with app.run_test() as pilot:
                def result():
                    if fail:
                        raise cli.ConfigError('网络异常')
                    ui.output('配置文件：saved.yaml')
                browser,_=await self.browser(app,pilot,result)
                await pilot.press('enter')
                await self.settle(pilot,lambda:isinstance(app.screen,Operation) and app.screen.done)
                self.assertIn('操作未完成' if fail else '已结束',str(app.screen.query_one('#status',Static).render()))
                await pilot.press('0')
                await self.settle(pilot,lambda:app.screen is browser)

    async def test_cancel_after_write_still_requires_result_review(self):
        app=SlaiApp()
        async with app.run_test() as pilot:
            def action():
                ui.mark_changed()
                raise ui.Cancelled()
            browser,_=await self.browser(app,pilot,action)
            await pilot.press('enter')
            await self.settle(pilot,lambda:isinstance(app.screen,Operation) and app.screen.done)
            self.assertTrue(app.screen.changed)
