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
        info = {"nickname": "", "wxid": "", "avatar": False}
        if ok:
            await page.wait_for_timeout(1800)
            try:
                await ctx.storage_state(path=str(state_file))
            except Exception as e:
                st(f"保存登录态失败：{e}")
                ok = False
            if ok:
                try:
                    info = await _grab_profile(page, ctx, state_file, st)
                except Exception as e:
                    st(f"读账号资料失败（不影响登录）：{str(e)[:60]}")
        await browser.close()
        st("登录成功" if ok else "超时未检测到登录")
        return ok, info


async def _grab_profile(page, ctx, state_file: Path, st=None) -> dict:
    """登录后到首页抓：昵称 / 视频号ID(sph...) / 头像；头像下载到 {aid}.jpg 本地存。"""
    import re
    info = {"nickname": "", "wxid": "", "avatar": False}
    try:
        await page.goto("https://channels.weixin.qq.com/platform", wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(2500)
    except Exception:
        pass
    # 昵称
    for sel in ('.finder-nickname', '.account-info-nickname', 'span.finder-nickname',
                'div.finder-info-nickname', '.header-account-name', '.name-wrap .nickname',
                'h1', '.profile-name', '.account-name'):
        try:
            loc = page.locator(sel)
            if await loc.count():
                t = (await loc.first.inner_text()).strip()
                if t and len(t) < 40:
                    info["nickname"] = t
                    break
        except Exception:
            pass
    # 视频号ID：整页文本里找 sph 开头的串
    try:
        body = await page.locator("body").inner_text()
        m = re.search(r"sph[0-9A-Za-z_\-]{8,}", body or "")
        if m:
            info["wxid"] = m.group(0)
    except Exception:
        pass
    # 头像：找 qlogo 头像图，用登录态下载到本地 {aid}.jpg
    avatar_url = ""
    try:
        srcs = await page.eval_on_selector_all(
            "img", "els => els.map(e => e.src).filter(Boolean)")
        for s in srcs or []:
            if "qlogo" in s or "wx.qlogo" in s or "/head" in s:
                avatar_url = s
                break
    except Exception:
        pass
    if avatar_url:
        try:
            resp = await ctx.request.get(avatar_url, timeout=15000)
            if resp.ok:
                data = await resp.body()
                Path(state_file).with_suffix(".jpg").write_bytes(data)
                info["avatar"] = True
        except Exception:
            pass
    return info


def _harvest_names(obj, acc, depth=0):
    """从任意 JSON 里挖出 名字/标题 类字段（视频号接口返回结构各异，通用兜底）。"""
    if depth > 6 or acc.get("_n", 0) > 300:
        return
    if isinstance(obj, dict):
        nm = obj.get("name") or obj.get("title") or obj.get("nickname") \
            or obj.get("description") or obj.get("album_name") or obj.get("drama_name")
        if isinstance(nm, str) and 0 < len(nm) < 60:
            _id = obj.get("id") or obj.get("album_id") or obj.get("export_id") \
                or obj.get("drama_id") or obj.get("collection_id") or ""
            acc.setdefault("items", []).append({"name": nm.strip(), "id": str(_id)})
            acc["_n"] = acc.get("_n", 0) + 1
        for v in obj.values():
            _harvest_names(v, acc, depth + 1)
    elif isinstance(obj, list):
        for v in obj:
            _harvest_names(v, acc, depth + 1)


async def fetch_lists(state_file: Path, on_status=None) -> dict:
    """用账号登录态打开发表页，抓视频号自己发的 mmfinderassistant-bin 接口返回，
    解析出这个号的：合集/专辑、视频号剧集。附带 debug（抓到哪些接口）方便对不上时调。"""
    from playwright.async_api import async_playwright
    import json as _json
    out = {"collections": [], "dramas": [], "activities": [], "debug": []}
    if not Path(state_file).exists():
        out["debug"].append("登录态文件不存在")
        return out

    def st(m):
        if on_status:
            on_status(m)

    st("打开发表页、抓视频号接口…")
    hits = []   # [(url, json)]

    async def _on_resp(resp):
        u = resp.url or ""
        if "mmfinderassistant-bin" not in u:
            return
        try:
            j = await resp.json()
            hits.append((u, j))
        except Exception:
            pass

    async with async_playwright() as p:
        browser = await _launch(p, headless=True)
        ctx = await browser.new_context(storage_state=str(state_file))
        page = await ctx.new_page()
        page.on("response", lambda r: asyncio.create_task(_on_resp(r)))
        try:
            await page.goto(CREATE_URL, wait_until="domcontentloaded", timeout=40000)
            await page.wait_for_timeout(6500)   # 合集接口(/collection/get_collection_list)随页面自动发，先等它
        except Exception as e:
            await browser.close()
            out["debug"].append(f"打开发表页失败: {str(e)[:80]}")
            return out
        # 主动点开各下拉触发它们的接口：先按标签点，再兜底点所有下拉控件
        async def _click(loc):
            try:
                if await loc.count():
                    await loc.first.click(timeout=2000)
                    await page.wait_for_timeout(1300)
                    await page.keyboard.press("Escape")
                    await page.wait_for_timeout(300)
                    return True
            except Exception:
                pass
            return False

        for label in ("添加到合集", "选择视频号剧集", "扩展链接", "活动", "请选择"):
            # 点标签同一行的下拉控件（label 的后一个可点区域）
            await _click(page.locator(
                f"xpath=//*[contains(text(),'{label}')]/following::*[contains(@class,'select') or "
                f"contains(@class,'dropdown') or contains(@class,'placeholder') or "
                f"self::input][1]"))
            await _click(page.locator(f"text={label}"))
        # 兜底：把页面上所有下拉控件都点一遍
        for sel in ('.weui-desktop-form__control', '[class*="select"]', '[class*="dropdown"]',
                    '.weui-desktop-select'):
            loc = page.locator(sel)
            try:
                n = min(await loc.count(), 12)
            except Exception:
                n = 0
            for i in range(n):
                try:
                    await loc.nth(i).click(timeout=1200)
                    await page.wait_for_timeout(700)
                    await page.keyboard.press("Escape")
                except Exception:
                    pass
        await page.wait_for_timeout(1500)
        await browser.close()

    # 按接口 url 关键词分桶解析
    all_eps = []
    saw_collection_ep = False
    for u, j in hits:
        ep = u.split("mmfinderassistant-bin", 1)[-1].split("?")[0]
        low = u.lower()
        # 合集：视频号真实接口 /collection/get_collection_list → data.collectionList
        if "get_collection_list" in low or ep.endswith("/collection/get_collection_list"):
            saw_collection_ep = True
            lst = (((j or {}).get("data") or {}).get("collectionList")) or []
            for it in lst:
                nm = it.get("name") or it.get("collectionName") or it.get("desc") or ""
                if nm:
                    out["collections"].append({"name": nm.strip(),
                                               "id": str(it.get("collectionId") or it.get("id") or "")})
            all_eps.append(f"{ep}(合集{len(lst)})")
            continue
        acc = {}
        _harvest_names(j, acc)
        names = acc.get("items", [])
        all_eps.append(f"{ep}({len(names)})")
        if not names:
            continue
        bucket = None
        if "drama" in low or "episode" in low:
            bucket = "dramas"
        elif "event" in low or "activit" in low:
            bucket = "activities"
        if bucket:
            seen = {x["name"] for x in out[bucket]}
            for it in names:
                if it["name"] not in seen:
                    out[bucket].append(it)
                    seen.add(it["name"])
    out["saw_collection_api"] = saw_collection_ep
    out["debug"] = all_eps[:30]
    st("读取完成")
    return out


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
                 cover_path: str = "", original: bool = False, schedule=None, link: str = "",
                 statement: str = "", location: str = "", collection: str = "",
                 drama: str = "", activity: str = "",
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
            # 内容编辑器：小V猫源码确认现版为 .text-editor-content，旧版 div.input-editor 兜底
            editor = None
            for _ in range(30):
                for sel in (".text-editor-content", "div.input-editor", ".input-editor"):
                    loc = page.locator(sel).first
                    try:
                        if await loc.count():
                            editor = loc
                            break
                    except Exception:
                        pass
                if editor:
                    break
                await asyncio.sleep(1)
            if editor is None:
                editor = page.locator("div.input-editor").first
            await editor.wait_for(timeout=15000)
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
            if statement:
                st("视频声明")
                await _try_statement(page, statement)
            if location:
                st("设置地理位置")
                await _try_location(page, location)
            if collection:
                st("添加到合集")
                await _try_collection(page, collection)
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


async def _dispatch_click(page, selector: str) -> bool:
    """派发完整鼠标事件序列点一个元素（视频号 React 控件普通 click 点不动，仿小V猫源码）。"""
    try:
        return await page.evaluate(
            """(sel)=>{const el=document.querySelector(sel); if(!el) return false;
               ['mouseenter','mousedown','mouseup','click'].forEach(t=>
                 el.dispatchEvent(new MouseEvent(t,{bubbles:true,cancelable:true,view:window})));
               return true;}""", selector)
    except Exception:
        return False


async def _try_original(page):
    try:
        lbl = page.get_by_label("视频为原创")
        if await lbl.count():
            await lbl.first.check()
            return
    except Exception:
        pass
    # 派发鼠标事件序列点复选框（React 控件）
    for sel in ('div.declare-original-checkbox input.ant-checkbox-input',
                '.original-declaration-checkbox', 'label:has-text("视频为原创")'):
        if await _dispatch_click(page, sel):
            break
    await page.wait_for_timeout(600)
    # 弹层里同意条款 + 声明原创
    for sel in ('.weui-desktop-dialog input.ant-checkbox-input',
                '.declare-original-checkbox input'):
        await _dispatch_click(page, sel)
    for txt in ("声明原创", "确定", "确认"):
        try:
            b = page.locator(f'.weui-desktop-dialog button:has-text("{txt}")')
            if await b.count():
                await b.first.click()
                break
        except Exception:
            pass


async def _try_location(page, location: str):
    """地理位置：往 #location input 里填，选第一个候选（仿小V猫 #location input）。"""
    if not location:
        return
    try:
        inp = page.locator('#location input, input[placeholder*="位置"]').first
        if not await inp.count():
            # 先点开位置控件
            await _dispatch_click(page, '.position-display, [class*="location"]')
            await page.wait_for_timeout(600)
            inp = page.locator('#location input, input[placeholder*="位置"]').first
        if await inp.count():
            await inp.click()
            await inp.fill(location)
            await page.wait_for_timeout(1500)   # 等 POI 搜索
            # 选第一个候选（派发鼠标事件）
            await _dispatch_click(page, '.location-list .location-item, .poi-list li, .getpoint-info')
    except Exception:
        pass


async def _try_at(page, at: str):
    """@视频号：搜索后选第一个（小V猫源码：.choose-finder-area .finder-list .finder-item + 派发鼠标事件）。"""
    if not at:
        return
    try:
        inp = page.locator('input[placeholder*="关键词"], .choose-finder-area input').first
        if await inp.count():
            await inp.fill(at.lstrip("@"))
            await page.wait_for_timeout(1500)
            await page.evaluate(
                """()=>{const list=document.querySelector('.choose-finder-area .finder-list');
                   if(!list) return false; const it=list.querySelector('.finder-item'); if(!it) return false;
                   ['mouseenter','mousedown','mouseup','click'].forEach(t=>
                     it.dispatchEvent(new MouseEvent(t,{bubbles:true,cancelable:true,view:window}))); return true;}""")
    except Exception:
        pass


async def _try_statement(page, statement: str):
    """视频声明：展开下拉选对应项。"""
    if not statement:
        return
    try:
        await _dispatch_click(page, '.declare-select, [class*="statement"] .weui-desktop-select')
        await page.wait_for_timeout(600)
        opt = page.locator(f'li:has-text("{statement}"), .option-item:has-text("{statement}")')
        if await opt.count():
            await opt.first.click()
    except Exception:
        pass


async def _try_collection(page, collection: str):
    """添加到合集：展开选对应合集。"""
    if not collection:
        return
    try:
        await _dispatch_click(page, '[class*="collection"] .weui-desktop-select, .collection-select')
        await page.wait_for_timeout(600)
        opt = page.locator(f'li:has-text("{collection}"), .option-item:has-text("{collection}")')
        if await opt.count():
            await opt.first.click()
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
