# -*- coding: utf-8 -*-
"""视频号（微信 Channels）发布：Playwright 驱动【系统 Edge/Chrome】，扫码登录 + 批量上传。
腾讯没有开放上传接口，只能模拟人在 channels.weixin.qq.com 后台操作。选择器蓝本取自
社区成熟项目 social-auto-upload 的 tencent_uploader，腾讯改版可能需要跟着修。"""
import asyncio
import time
from pathlib import Path

LOGIN_URL = "https://channels.weixin.qq.com"
CREATE_URL = "https://channels.weixin.qq.com/platform/post/create"
MANAGE_URL = "https://channels.weixin.qq.com/platform/post/list"


async def _launch(p, headless: bool):
    """优先用系统 Edge，其次系统 Chrome，最后回落 Playwright 自带 Chromium。"""
    last = None
    for ch in ("msedge", "chrome"):
        try:
            return await p.chromium.launch(channel=ch, headless=headless,
                                           args=["--disable-blink-features=AutomationControlled"])
        except Exception as e:
            last = e
    try:
        return await p.chromium.launch(headless=headless)
    except Exception:
        raise last or RuntimeError("无法启动浏览器（系统没装 Edge/Chrome，且未装 Playwright 浏览器）")


async def login(state_file: Path, timeout: int = 240, on_status=None) -> bool:
    """打开可见浏览器，用户扫码；登录成功后把 cookie 存到 state_file。"""
    from playwright.async_api import async_playwright

    def st(m):
        if on_status:
            on_status(m)

    st("正在打开视频号登录页…")
    async with async_playwright() as p:
        browser = await _launch(p, headless=False)
        ctx = await browser.new_context(viewport={"width": 1200, "height": 860})
        page = await ctx.new_page()
        try:
            await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=40000)
        except Exception as e:
            await browser.close()
            st(f"打开登录页失败：{e}")
            return False
        st("请在弹出的浏览器窗口里用微信扫码登录…")
        ok = False
        end = time.time() + timeout
        while time.time() < end:
            await asyncio.sleep(2)
            try:
                if "login" not in (page.url or ""):        # 登录后会跳离 login 页
                    ok = True
                    break
                if await page.locator('div:has-text("发表视频")').count() or \
                   await page.locator('button:has-text("发表")').count():
                    ok = True
                    break
            except Exception:
                pass
        nickname = ""
        if ok:
            await page.wait_for_timeout(1800)
            try:
                await ctx.storage_state(path=str(state_file))
            except Exception as e:
                st(f"保存登录态失败：{e}")
                ok = False
            for sel in ('.finder-nickname', '.account-info-nickname', 'span.finder-nickname',
                        'div.finder-info-nickname', '.header-account-name', '.name-wrap .nickname'):
                try:
                    loc = page.locator(sel)
                    if await loc.count():
                        nickname = (await loc.first.inner_text()).strip()
                        if nickname:
                            break
                except Exception:
                    pass
        await browser.close()
        st("登录成功" if ok else "超时未检测到登录")
        return ok, nickname


async def check_login(state_file: Path) -> bool:
    """用已存 cookie 静默打开管理页，判断登录是否还有效。"""
    if not Path(state_file).exists():
        return False
    from playwright.async_api import async_playwright
    async with async_playwright() as p:
        browser = await _launch(p, headless=True)
        ctx = await browser.new_context(storage_state=str(state_file))
        page = await ctx.new_page()
        good = False
        try:
            await page.goto(MANAGE_URL, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(2500)
            good = "login" not in (page.url or "")
        except Exception:
            good = False
        await browser.close()
        return good


async def _find_file_input(page):
    for fr in page.frames:
        loc = fr.locator('input[type="file"]')
        try:
            if await loc.count():
                return loc.first
        except Exception:
            pass
    return None


async def upload(state_file: Path, video_path: str, title: str = "", tags=None, desc: str = "",
                 cover_path: str = "", original: bool = False, schedule=None,
                 headless: bool = False, on_status=None, err_dir: Path = None):
    """上传一条视频到视频号。schedule=datetime|None。返回 (ok:bool, msg:str)。"""
    from playwright.async_api import async_playwright
    tags = tags or []

    def st(m):
        if on_status:
            on_status(m)

    async with async_playwright() as p:
        browser = await _launch(p, headless=headless)
        ctx = await browser.new_context(storage_state=str(state_file))
        page = await ctx.new_page()
        try:
            await page.goto(CREATE_URL, wait_until="domcontentloaded", timeout=40000)
            await page.wait_for_timeout(2000)

            st("找上传入口")
            fi = await _find_file_input(page)
            if fi is None:
                try:
                    await page.get_by_text("发表视频").first.click()
                    await page.wait_for_timeout(1500)
                except Exception:
                    pass
                fi = await _find_file_input(page)
            if fi is None:
                raise RuntimeError("没找到上传按钮（页面可能已改版）")

            st("上传视频文件…")
            await fi.set_input_files(str(video_path))
            await page.wait_for_timeout(2000)

            st("填标题/话题/简介")
            editor = page.locator("div.input-editor").first
            await editor.wait_for(timeout=30000)
            await editor.click()
            if title:
                await page.keyboard.type(title[:30])
            for tg in tags:
                await page.keyboard.type("#" + str(tg).lstrip("#"))
                await page.keyboard.press("Space")
            if desc:
                await page.keyboard.press("Enter")
                await page.keyboard.type(desc)

            if original:
                st("勾选原创声明")
                await _try_original(page)
            if cover_path:
                st("设置封面")
                await _try_cover(page, cover_path)
            if schedule:
                st("设置定时发布")
                await _try_schedule(page, schedule)

            st("等视频上传/转码完成…")
            ready = False
            for _ in range(150):        # 最多约 5 分钟
                await asyncio.sleep(2)
                btn = page.get_by_role("button", name="发表")
                try:
                    if await btn.count():
                        cls = await btn.first.get_attribute("class") or ""
                        if "weui-desktop-btn_disabled" not in cls:
                            ready = True
                            break
                except Exception:
                    pass
            if not ready:
                raise RuntimeError("视频上传/转码超时（>5分钟）")

            st("发表")
            await page.locator('div.form-btns button:has-text("发表")').first.click()
            await page.wait_for_url("**/post/list**", timeout=40000)
            try:
                await ctx.storage_state(path=str(state_file))
            except Exception:
                pass
            await browser.close()
            return True, "发表成功"
        except Exception as e:
            if err_dir:
                try:
                    await page.screenshot(path=str(Path(err_dir) / "channels_error.png"))
                except Exception:
                    pass
            await browser.close()
            return False, str(e)[:160]


async def _try_original(page):
    try:
        lbl = page.get_by_label("视频为原创")
        if await lbl.count():
            await lbl.first.check()
    except Exception:
        pass
    for sel in ('div.declare-original-checkbox input.ant-checkbox-input',
                'label:has-text("我已阅读并同意 《视频号原创声明使用条款》")'):
        try:
            loc = page.locator(sel)
            if await loc.count():
                await loc.first.click()
        except Exception:
            pass
    try:
        btn = page.locator("button[name='声明原创']")
        if await btn.count():
            await btn.first.click()
    except Exception:
        pass


async def _try_cover(page, cover_path):
    try:
        fi = page.locator('.single-cover-uploader-wrap input[type="file"]')
        if not await fi.count():
            for t in ("编辑封面", "封面"):
                b = page.get_by_text(t, exact=False)
                if await b.count():
                    await b.first.click()
                    await page.wait_for_timeout(1200)
                    break
            fi = page.locator('.single-cover-uploader-wrap input[type="file"]')
        if await fi.count():
            await fi.first.set_input_files(str(cover_path))
            await page.wait_for_timeout(1500)
            for t in ("确定", "确认"):
                b = page.locator(f'div.weui-desktop-dialog__ft button.weui-desktop-btn_primary:has-text("{t}")')
                if await b.count():
                    await b.first.click()
                    await page.wait_for_timeout(800)
    except Exception:
        pass


async def _try_schedule(page, dt):
    try:
        await page.locator("label").filter(has_text="定时").nth(1).click()
        await page.wait_for_timeout(600)
        di = page.locator('input[placeholder="请选择发表时间"]')
        if await di.count():
            await di.first.click()
            await page.wait_for_timeout(600)
            day = page.locator("table.weui-desktop-picker__table a").filter(
                has_text=str(dt.day))
            if await day.count():
                await day.first.click()
                await page.wait_for_timeout(400)
        ti = page.locator('input[placeholder="请选择时间"]')
        if await ti.count():
            await ti.first.click()
            await page.keyboard.press("Control+A")
            await page.keyboard.type(dt.strftime("%H:%M"))
            await page.keyboard.press("Enter")
            await page.wait_for_timeout(400)
    except Exception:
        pass
