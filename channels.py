# -*- coding: utf-8 -*-
"""视频号（微信 Channels）发布：Playwright 驱动【系统 Edge/Chrome】，扫码登录 + 批量上传。
腾讯没有开放上传接口，只能模拟人在 channels.weixin.qq.com 后台操作。选择器蓝本取自
社区成熟项目 social-auto-upload 的 tencent_uploader，腾讯改版可能需要跟着修。"""
import asyncio
import json
import socket
import subprocess
import time
from pathlib import Path

def _free_port() -> int:
    """让系统分配一个真正空闲的端口(bind 到 0 拿端口),避免自增计数在并发/负载下撞端口。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


def _wait_port(port: int, tries: int = 120) -> bool:
    """等 chromium 调试端口起来。机器有负载时启动慢,给到 ~60 秒(120×0.5)。"""
    for _ in range(tries):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return True
        except Exception:
            time.sleep(0.5)
    return False

LOGIN_URL = "https://channels.weixin.qq.com"
CREATE_URL = "https://channels.weixin.qq.com/platform/post/create"
MANAGE_URL = "https://channels.weixin.qq.com/platform/post/list"

# 【关键】视频号后台是 wujie 微前端：发布页整个 .post-view 封在 <wujie-app> 的 shadowRoot 里。
# 原生 document.querySelector 不穿透 shadow DOM → 查不到「发表」按钮等控件（这是发布点不动的根因）。
# 下面三个函数深度递归穿透所有 open shadowRoot（含 wujie-app），与小V猫 server.jsc 同思路：
#   小V猫: document.querySelector('wujie-app').shadowRoot.querySelectorAll('.post-view .form-btns button')
# 把它作为前缀拼进 page.evaluate 的箭头函数体（函数声明会被提升，合法）。
_DEEP_JS = r"""
  function _dq(sel, root){ root=root||document;
    let el=root.querySelector(sel); if(el) return el;
    const hs=root.querySelectorAll('*');
    for(let i=0;i<hs.length;i++){ if(hs[i].shadowRoot){ const f=_dq(sel,hs[i].shadowRoot); if(f) return f; } }
    return null; }
  function _dqa(sel, root){ root=root||document; const out=[];
    out.push(...root.querySelectorAll(sel));
    root.querySelectorAll('*').forEach(h=>{ if(h.shadowRoot) out.push(..._dqa(sel,h.shadowRoot)); });
    return out; }
  function _dall(root){ root=root||document; const out=[...root.querySelectorAll('*')];
    root.querySelectorAll('*').forEach(h=>{ if(h.shadowRoot) out.push(..._dall(h.shadowRoot)); });
    return out; }
"""


async def _launch(p, headless: bool, offscreen: bool = False):
    """优先用系统 Edge，其次系统 Chrome，最后回落 Playwright 自带 Chromium。
    offscreen=True：有头模式(视频号会挡headless)，把窗口移到屏幕外，用户看不到=等效"不弹窗"。
    【坑】绝不能加 --start-minimized：最小化窗口不渲染/不合成，Playwright 的 .click() 命中测试
    (elementFromPoint) 会返回空 → 点任何元素都超时。只靠 -32000 移到屏幕外即可，窗口照常渲染。"""
    args = ["--disable-blink-features=AutomationControlled"]
    if offscreen:
        args += ["--window-position=-32000,-32000", "--window-size=1280,900"]
    last = None
    for ch in ("msedge", "chrome"):
        try:
            return await p.chromium.launch(channel=ch, headless=headless, args=args)
        except Exception as e:
            last = e
    try:
        return await p.chromium.launch(headless=headless, args=args)
    except Exception:
        raise last or RuntimeError("无法启动浏览器（系统没装 Edge/Chrome，且未装 Playwright 浏览器）")


# ==================== 持久档案（仿小V猫 persist:partition，根治"每次都掉线"） ====================
# 旧方案 storage_state=JSON快照 有三个致命伤：①快照不含 IndexedDB（视频号会话材料在里面）
# ②每次都开全新临时浏览器→视频号单会话轮换→一用就掉 ③fetch_lists 用 headless 被视频号封。
# 新方案：每账号一个磁盘档案目录 data/channels/profiles/{aid}/，cookie/localStorage/IndexedDB
# 全落盘，登录/拉列表/发布/看后台全用同一个档案=同一个连续会话 → 关软件重开照样在线。
_PROFILE_LOCKS: dict = {}     # profile路径 -> asyncio.Lock（Chromium 同一档案只能开一个实例）


def _profile_lock(state_file: Path) -> asyncio.Lock:
    k = str(_profile_dir(state_file))
    if k not in _PROFILE_LOCKS:
        _PROFILE_LOCKS[k] = asyncio.Lock()
    return _PROFILE_LOCKS[k]


def _profile_dir(state_file: Path) -> Path:
    d = Path(state_file).parent / "profiles" / Path(state_file).stem
    d.mkdir(parents=True, exist_ok=True)
    return d


async def _launch_persistent(p, state_file: Path, visible: bool = False):
    """打开该账号的持久浏览器档案，返回 BrowserContext（.close() 即关整个浏览器）。
    visible=False → 有头但移到屏幕外(后台无窗口)；True → 正常显示(登录/看后台用)。
    内部先拿该账号的档案锁，context 关闭时自动放锁——同账号操作天然串行，不同账号并行。
    首次使用时用旧快照 cookie 播种（能救活就不用重扫）。"""
    lk = _profile_lock(state_file)
    await lk.acquire()
    ctx = None
    proc = None
    try:
        prof = _profile_dir(state_file)
        fresh = not (prof / "Default").exists()
        # 【核心·仿小V猫】不用 Playwright 启动浏览器(带自动化特征被视频号风控检测→弹验证)，
        # 而是自己启动一个"干净"的 bundled Chromium(无 --enable-automation、navigator.webdriver=false)，
        # 再用 CDP 从外部连接驱动 —— 和小V猫的内嵌 webview 一样是"正常浏览器"，不被检测。
        exe = p.chromium.executable_path
        posarg = "--window-position=120,60" if visible else "--window-position=-32000,-32000"
        browser = None
        last_err = None
        for _try in range(3):     # 启动重试:机器有负载时端口可能起得慢/撞,换端口重来
            port = _free_port()
            args = [exe, f"--user-data-dir={prof}", f"--remote-debugging-port={port}",
                    "--no-first-run", "--no-default-browser-check", "--no-service-autorun",
                    "--disable-background-timer-throttling", "--disable-backgrounding-occluded-windows",
                    "--disable-renderer-backgrounding", "--disable-ipc-flooding-protection",
                    "--disable-features=Translate", "--window-size=1280,900", posarg, "about:blank"]
            proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if _wait_port(port):
                try:
                    browser = await p.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
                    break
                except Exception as e:
                    last_err = e
            else:
                last_err = RuntimeError("浏览器调试端口未就绪")
            try:
                proc.terminate()
            except Exception:
                pass
            proc = None
            await asyncio.sleep(1)
        if browser is None:
            raise last_err or RuntimeError("浏览器启动失败(重试3次)")
        ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
        # 【关键】自启动浏览器用 proc.terminate() 硬杀，chromium 来不及把 cookie flush 进档案 →
        # 档案里的登录态是旧的。所以**每次都从 JSON 快照播种最新 cookie**(快照在每次关闭前都存过=活的)。
        if Path(state_file).exists():
            try:
                data = json.loads(Path(state_file).read_text(encoding="utf-8"))
                if data.get("cookies"):
                    await ctx.add_cookies(data["cookies"])
            except Exception:
                pass
        # 上传时静音预览视频(别外放)
        try:
            await ctx.add_init_script("""
                setInterval(()=>{ try{ document.querySelectorAll('video,audio').forEach(m=>{
                    m.muted=true; m.volume=0; if(!m.paused) m.pause(); }); }catch(_){} }, 400);
            """)
        except Exception:
            pass
    except Exception:
        try:
            if proc:
                proc.terminate()
        except Exception:
            pass
        if lk.locked():
            lk.release()
        raise
    def _unlock(*_):
        try:
            if proc:
                proc.terminate()
        except Exception:
            pass
        if lk.locked():
            lk.release()
    ctx.on("close", _unlock)
    try:
        ctx._vcat_proc = proc     # 存着进程句柄，close 时杀掉
    except Exception:
        pass
    return ctx


async def _ctx_page(ctx):
    """持久 context 启动自带一个空白页，直接复用它（省一个标签页）。"""
    return ctx.pages[0] if ctx.pages else await ctx.new_page()


async def login(state_file: Path, timeout: int = 240, on_status=None) -> bool:
    """打开可见浏览器，用户扫码；登录成功后把 cookie 存到 state_file。"""
    from playwright.async_api import async_playwright

    def st(m):
        if on_status:
            on_status(m)

    st("正在打开视频号登录页…")
    async with async_playwright() as p:
        # 持久档案：登录写进磁盘档案，关软件重开仍在线；若档案里已是登录态，秒过无需扫码
        ctx = await _launch_persistent(p, state_file, visible=True)
        browser = ctx                      # 兼容旧引用：close()=关整个持久浏览器
        page = await _ctx_page(ctx)
        try:
            await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=40000)
        except Exception as e:
            await browser.close()
            st(f"打开登录页失败：{e}")
            return False, {"nickname": "", "wxid": "", "avatar": False}
        st("请在弹出的浏览器窗口里用微信扫码登录…")
        ok = False
        closed = {"v": False}
        page.on("close", lambda *_: closed.update(v=True))
        ctx.on("close", lambda *_: closed.update(v=True))
        end = time.time() + timeout
        while time.time() < end:
            await asyncio.sleep(2)
            # 用户把登录窗口关了 → 立刻结束，别傻等到超时(那会一直卡住 login_running=True)
            if closed["v"] or not ctx.pages:
                st("登录窗口被关闭，已取消登录")
                try:
                    await browser.close()
                except Exception:
                    pass
                return False, {"nickname": "", "wxid": "", "avatar": False}
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
            # 【关键】存登录态前先真正跳到发表页、确认没被踢回 login，
            # 保证抓到的是能用的完整 session（否则会存到半截 session→拉列表/发布时又跳login）
            try:
                await page.goto(CREATE_URL, wait_until="domcontentloaded", timeout=30000)
                await page.wait_for_timeout(3500)
                if "login" in (page.url or "").lower():
                    st("登录没完成：发表页仍要求登录，请重扫")
                    ok = False
            except Exception:
                pass
            if ok:
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


def _harvest_dramas(obj, out, seen, depth=0):
    """递归找视频号剧集条目：凡是含 exportId 的 dict 就是一条剧集。
    提 name(剧名)/count(共N集,feedCount)/exportId(扩展链接ID)/cover(封面)。
    (小V猫 getDramaComponent 返回同款字段，反编译确认)"""
    if depth > 8 or len(out) > 200:
        return
    if isinstance(obj, dict):
        exp = obj.get("exportId") or obj.get("export_id")
        # 【关键】exportId 不是剧集独有——已发布的视频作品(feed)也有 exportId。
        # 剧集才有 feedCount(共N集);feed 作品有 objectNonce/fullPlayRate 这些播放数据。
        # 所以必须"有 exportId + 有集数字段 + 没有 feed 播放字段"才算剧集,否则会把作品当剧集。
        is_feed = ("objectNonce" in obj) or ("fullPlayRate" in obj) or ("avgPlayTimeSec" in obj)
        has_cnt = any(k in obj for k in ("feedCount", "episodeCount"))
        if exp and has_cnt and not is_feed and str(exp) not in seen:
            def _s(*keys):   # 取第一个非空字符串(视频号有的字段是dict,得防 .strip 崩)
                for k in keys:
                    v = obj.get(k)
                    if isinstance(v, str) and v.strip():
                        return v.strip()
                return ""
            nm = _s("name", "title", "albumName", "description", "desc")
            cnt = (obj.get("feedCount") or obj.get("episodeCount")
                   or obj.get("cnt") or obj.get("count") or 0)
            cover = _s("coverUrl", "albumThumbUrl", "thumbUrl", "cover")
            try:
                cnt = int(cnt)
            except Exception:
                cnt = 0
            seen.add(str(exp))
            out.append({"name": nm or "未命名剧集", "count": cnt,
                        "exportId": str(exp), "cover": cover,
                        "link": f"event/{exp}"})
        for v in obj.values():
            _harvest_dramas(v, out, seen, depth + 1)
    elif isinstance(obj, list):
        for v in obj:
            _harvest_dramas(v, out, seen, depth + 1)


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
        # 持久档案+有头离屏：headless 会被视频号封(重定向login)→这就是以前"拉不到剧集/合集"的根因
        ctx = await _launch_persistent(p, state_file, visible=False)
        browser = ctx
        page = await _ctx_page(ctx)
        page.on("response", lambda r: asyncio.create_task(_on_resp(r)))
        try:
            await page.goto(CREATE_URL, wait_until="domcontentloaded", timeout=40000)
            await page.wait_for_timeout(6500)   # 合集接口(/collection/get_collection_list)随页面自动发，先等它
        except Exception as e:
            await browser.close()
            out["debug"].append(f"打开发表页失败: {str(e)[:80]}")
            return out
        # 登录态失效检测：没登录会被重定向到 login.html（视频号服务端不认 session）
        if "login" in (page.url or "").lower():
            await browser.close()
            out["logged_out"] = True
            out["debug"].append("登录态已失效(被重定向到login.html)")
            st("登录态已失效，请重新扫码登录")
            return out
        # 视频号剧集(CPS分销剧)：直接调真接口 post/search_drama_component 分页拉全部。
        # (逆向小V猫 server.jsc 得到:getDramaComponent→此接口;字段 name/mediaCount/id(event链接)/coverUrl)
        st("拉取视频号剧集…")
        try:
            drama = await page.evaluate(
                """async () => {
                  // 【关键】必须和发布弹窗完全一样的参数：currentPage(不是pageNum) + pageSize:5 + scene:7。
                  // 视频号这接口 pageSize 会改变返回的剧集内容(坑)：pageSize:5 才是弹窗那份真列表，
                  // pageSize:20 返回的是另一份不同排序 → 软件选的剧发布弹窗里没有 → 挂不上。
                  const url='/cgi-bin/mmfinderassistant-bin/post/search_drama_component';
                  let all=[], currentPage=1, total=1;
                  while (all.length < total && currentPage <= 80) {
                    const res=await fetch(url,{method:'POST',headers:{'content-type':'application/json'},
                      body:JSON.stringify({currentPage,pageSize:5,sceneType:3,scene:7,reqScene:7,
                        rawKeyBuff:'',pluginSessionId:null,timestamp:String(Date.now())}),credentials:'include'});
                    const j=await res.json();
                    const d=(j&&j.data)||{}; total=d.totalCount||0;
                    const lst=d.list||[]; if(!lst.length) break;
                    all=all.concat(lst); currentPage++;
                  }
                  return all;
                }""")
            for it in (drama or []):
                nm = (it.get("name") or "").strip()
                out["dramas"].append({
                    "name": nm or "未命名剧集",
                    "count": int(it.get("mediaCount") or 0),
                    "exportId": str(it.get("id") or ""),
                    "cover": it.get("coverUrl") or "",
                    "usable": bool(it.get("usable", True)),
                })
            out["saw_drama_api"] = True
            out["debug"].append(f"剧集{len(out['dramas'])}")
        except Exception as e:
            out["debug"].append(f"剧集接口失败:{str(e)[:60]}")
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
        # 【关键】视频号 session 每次访问会轮换,用完必须把最新登录态存回,否则文件里的
        # 旧 session 用一次就作废→下次拉列表/发布又"失效"。(upload/open_backend 已这么做)
        try:
            await ctx.storage_state(path=str(state_file))
        except Exception:
            pass
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
        # 剧集已用真接口 search_drama_component 直接拉(上面),这里不再靠 sniff 猜
        acc = {}
        _harvest_names(j, acc)
        names = acc.get("items", [])
        all_eps.append(f"{ep}({len(names)})")
        if not names:
            continue
        if "event" in low or "activit" in low:
            seen = {x["name"] for x in out["activities"]}
            for it in names:
                if it["name"] not in seen:
                    out["activities"].append(it)
                    seen.add(it["name"])
    out["saw_collection_api"] = saw_collection_ep
    out["debug"] = all_eps + out.get("debug", [])
    out["debug"] = out["debug"][:30]
    st("读取完成")
    return out


async def fetch_post_stats(state_file: Path, limit: int = 30) -> dict:
    """【数据回查】拉这个账号已发作品的播放/点赞等数据(视频号自己的 post_list 接口)。
    返回 {ok, posts:[{id,desc,create_time,read,like,comment,forward,fav}], raw_keys, err}。
    连续多条0播放=可能被限流,由上层判定。"""
    from playwright.async_api import async_playwright
    out = {"ok": False, "posts": [], "raw_keys": [], "err": ""}
    if not Path(state_file).exists():
        out["err"] = "该账号未登录"
        return out
    async with async_playwright() as p:
        ctx = await _launch_persistent(p, state_file, visible=False)
        page = await _ctx_page(ctx)
        try:
            await page.goto(MANAGE_URL, wait_until="domcontentloaded", timeout=40000)
            await page.wait_for_timeout(3000)
            if "login" in (page.url or "").lower():
                out["err"] = "登录态已失效"
                await ctx.close()
                return out
            data = await page.evaluate(
                """async (lim) => {
                  const url='/cgi-bin/mmfinderassistant-bin/post/post_list';
                  let all=[], currentPage=1;
                  while (all.length < lim && currentPage <= 5) {
                    const res=await fetch(url,{method:'POST',headers:{'content-type':'application/json'},
                      body:JSON.stringify({currentPage,pageSize:20,onlyUnread:false,userpageType:3,
                        pluginSessionId:null,rawKeyBuff:null,reqScene:7,scene:7,
                        timestamp:String(Date.now())}),credentials:'include'});
                    const j=await res.json();
                    const lst=(j&&j.data&&j.data.list)||[];
                    if(!lst.length) break;
                    all=all.concat(lst); currentPage++;
                    if(lst.length<20) break;
                  }
                  const keys=all.length?Object.keys(all[0]):[];
                  const num=(...v)=>{for(const x of v){if(typeof x==='number')return x;} return 0;};
                  return {keys, posts: all.slice(0,lim).map(x=>({
                    id:String(x.exportId||x.objectId||x.id||''),
                    desc:((x.desc&&(x.desc.description||x.desc.shortTitle))||x.description||'').slice(0,60),
                    create_time:num(x.createTime,x.create_time),
                    read:num(x.readCount,x.playCount,x.viewCount),
                    like:num(x.likeCount,x.praiseCount,x.diggCount),
                    comment:num(x.commentCount),
                    forward:num(x.forwardCount,x.shareCount),
                    fav:num(x.favCount,x.collectCount),
                  }))};
                }""", limit)
            out["posts"] = (data or {}).get("posts") or []
            out["raw_keys"] = (data or {}).get("keys") or []
            out["ok"] = True
            try:
                await ctx.storage_state(path=str(state_file))   # session 轮换,存回最新
            except Exception:
                pass
        except Exception as e:
            out["err"] = str(e)[:120]
        try:
            await ctx.close()
        except Exception:
            pass
    return out


async def open_backend(state_file: Path, on_status=None):
    """用账号登录态打开它的视频号后台（可见浏览器，用户直接操作），保持到用户关掉窗口。"""
    from playwright.async_api import async_playwright

    def st(m):
        if on_status:
            on_status(m)

    if not Path(state_file).exists():
        st("该账号未登录，先扫码登录")
        return
    async with async_playwright() as p:
        # 持久档案：看后台=同一个会话，不再跟发布/拉列表互踢
        ctx = await _launch_persistent(p, state_file, visible=True)
        browser = ctx
        page = await _ctx_page(ctx)
        closed = {"v": False}
        page.on("close", lambda *_: closed.update(v=True))
        ctx.on("close", lambda *_: closed.update(v=True))
        try:
            await page.goto("https://channels.weixin.qq.com/platform",
                            wait_until="domcontentloaded", timeout=40000)
            st("后台已打开")
        except Exception as e:
            st(f"打开后台失败：{str(e)[:80]}")
        # 保持窗口开着，直到用户关掉浏览器/页面
        while not closed["v"]:
            await asyncio.sleep(2)
        try:
            await ctx.storage_state(path=str(state_file))   # 关前把登录态存回（续期）
        except Exception:
            pass
        try:
            await browser.close()
        except Exception:
            pass
    st("后台窗口已关闭")


async def check_login(state_file: Path) -> bool:
    """用持久档案静默打开管理页，判断登录是否还有效（顺带续期 cookie=保活）。"""
    if not Path(state_file).exists():
        return False
    from playwright.async_api import async_playwright
    async with async_playwright() as p:
        # 有头离屏：headless 被视频号封，会把活session误判成掉线
        ctx = await _launch_persistent(p, state_file, visible=False)
        browser = ctx
        page = await _ctx_page(ctx)
        good = False
        try:
            await page.goto(MANAGE_URL, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(2500)
            good = "login" not in (page.url or "")
        except Exception:
            good = False
        if good:
            try:
                await ctx.storage_state(path=str(state_file))   # session 轮换,存回续期
            except Exception:
                pass
        await browser.close()
        return good


async def _verify_block_msg(page) -> str:
    """检测视频号真正弹出的「安全验证/实名验证」拦截弹窗。
    【关键】页面 DOM 里常驻隐藏的"实名验证"模板文字，必须判元素**真的可见**(有尺寸)才算，
    否则会误报→触发无谓的刷新/等扫码，把上传流程搞乱。命中返回提示，否则 ""。"""
    try:
        hit = await page.evaluate("()=>{" + _DEEP_JS + r"""
            const kws=['实名验证','实名信息核验','管理员微信扫码','身份验证','安全验证'];
            for(const e of _dall()){
              let t=''; try{ t=(e.innerText||'').trim(); }catch(_){ continue; }
              if(t.length>40) continue;
              for(const k of kws){
                if(t.includes(k)){
                  let r=null; try{ r=e.getBoundingClientRect(); }catch(_){}
                  // 必须可见(有宽高) + 不是 display:none/visibility:hidden
                  let st=null; try{ st=getComputedStyle(e); }catch(_){}
                  const vis = r && r.width>0 && r.height>0 && st && st.visibility!=='hidden' && st.display!=='none';
                  if(vis) return k;
                }
              }
            }
            return '';
        }""")
    except Exception:
        hit = ''
    m = {"实名验证": "该账号需完成实名验证才能发表", "实名信息核验": "该账号需完成实名验证才能发表",
         "管理员微信扫码": "视频号弹出安全验证，需微信扫码", "身份验证": "视频号要求身份验证",
         "安全验证": "视频号弹出安全验证弹窗"}
    return m.get(hit, "")


async def _set_window_onscreen(page, onscreen: bool):
    """把离屏(−32000)的自动化窗口临时移到屏幕上/移回屏幕外。
    用 CDP Browser.setWindowBounds —— 视频号弹实名/安全验证时得让用户能看见并扫码。"""
    try:
        cdp = await page.context.new_cdp_session(page)
        info = await cdp.send("Browser.getWindowForTarget")
        wid = info["windowId"]
        if onscreen:
            await cdp.send("Browser.setWindowBounds", {"windowId": wid,
                "bounds": {"left": 140, "top": 80, "width": 1180, "height": 880, "windowState": "normal"}})
            try:
                await page.bring_to_front()
            except Exception:
                pass
        else:
            await cdp.send("Browser.setWindowBounds", {"windowId": wid,
                "bounds": {"left": -32000, "top": -32000}})
        await cdp.detach()
    except Exception:
        pass


async def _handle_verify_if_any(page, st) -> str:
    """视频号偶发弹"实名/安全验证"框，但账号早已实名。【关键】**绝不 reload**——reload 会把已上传
    的视频一起清掉，导致反复重传/丢视频。只用 ×/Escape/点蒙层空白 关掉它；关不掉也直接继续
    (不丢视频，让后续照常发表)。返回始终 ""(不因验证而失败)。"""
    if not await _verify_block_msg(page):
        return ""
    st("检测到验证弹窗，关闭它（不刷新，保住视频）…")
    for _ in range(5):
        try:
            await page.evaluate("()=>{" + _DEEP_JS + r"""
                const modal=[..._dall()].find(e=>{try{const t=(e.innerText||'');
                    return (t.includes('实名')||t.includes('验证'))&&e.getBoundingClientRect().width>200&&e.getBoundingClientRect().width<900;}catch(_){return false;}});
                if(!modal) return;
                // 找关闭×：class含close / svg图标 / 标题行最后一个子元素
                let x=modal.querySelector('[class*="close"],[class*="Close"],.weui-desktop-dialog__close');
                if(!x) x=modal.querySelector('svg,i.icon,use,[class*="icon"]');
                if(!x){ const hdr=modal.children[0]; if(hdr) x=hdr.lastElementChild; }
                if(x){ ['mouseenter','mousedown','mouseup','click'].forEach(t=>
                    x.dispatchEvent(new MouseEvent(t,{bubbles:true,cancelable:true,view:window}))); }
                // 再点一下遮罩层空白处(很多弹窗点外部即关)
                const mask=[..._dall()].find(e=>{try{const c=(e.className||'').toString();
                    return (c.includes('mask')||c.includes('overlay')||c.includes('dialog__wrp'))&&e.getBoundingClientRect().width>600;}catch(_){return false;}});
                if(mask){ ['mousedown','mouseup','click'].forEach(t=>
                    mask.dispatchEvent(new MouseEvent(t,{bubbles:true,cancelable:true,view:window,clientX:5,clientY:5}))); }
            }""")
        except Exception:
            pass
        try:
            await page.keyboard.press("Escape")
        except Exception:
            pass
        await page.wait_for_timeout(600)
        if not await _verify_block_msg(page):
            st("✅ 验证弹窗已关闭，视频保留，继续…")
            return ""
    st("验证弹窗关不掉，直接继续发表（不刷新，不丢视频）")
    return ""    # 关不掉也不 reload、不失败——继续走后续流程


async def _set_file_via_cdp(page, file_path) -> bool:
    """通过 CDP DOM.setFileInputFiles 给 file input 塞本地文件路径(仿小V猫)。
    connect_over_cdp 模式下 Playwright 的 set_input_files 会尝试传输文件字节(限50MB)，大视频必失败；
    DOM.setFileInputFiles 传的是**路径**(浏览器在本机直接读),不受大小限制。"""
    cdp = None
    try:
        cdp = await page.context.new_cdp_session(page)
        # file input 常在 wujie-app 的 shadow DOM 里，用深度穿透查询找它(document.querySelector不穿透)
        expr = ("(function(){" + _DEEP_JS +
                " var a=_dqa('input[type=file]');"
                " return a.find(function(x){return (x.accept||'').indexOf('video')>=0;})||a[0]||null;})()")
        r = await cdp.send("Runtime.evaluate", {"expression": expr, "returnByValue": False})
        oid = (r.get("result") or {}).get("objectId")
        if not oid:
            return False
        await cdp.send("DOM.setFileInputFiles", {"files": [str(file_path)], "objectId": oid})
        return True
    except Exception:
        return False
    finally:
        try:
            if cdp:
                await cdp.detach()
        except Exception:
            pass


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
                 headless: bool = False, show_browser: bool = False,
                 on_status=None, err_dir: Path = None):
    """上传一条视频到视频号。schedule=datetime|None。返回 (ok:bool, msg:str)。
    show_browser=True：调试用，浏览器摆屏幕上让用户看全过程；False：离屏后台(用户看不到)。"""
    from playwright.async_api import async_playwright
    tags = tags or []

    def st(m):
        if on_status:
            on_status(m)

    # 【护栏】空文件(0字节)/文件不存在=去重导出失败的空壳，任何发布方式都传不上CDN(会白等8分钟超时)。
    # 上传前先卡掉，给明确可操作的报错，别让用户误以为是发布逻辑坏了。
    try:
        _vp = Path(video_path)
        if not _vp.exists():
            return False, f"视频文件不存在：{video_path}"
        _sz = _vp.stat().st_size
        if _sz < 10240:   # <10KB 基本就是空壳/损坏
            return False, (f"视频文件是空的({_sz}字节)，是去重/导出时生成失败的空壳，"
                           f"任何方式都发不出去。请重新去重导出这条视频再发。")
    except Exception:
        pass

    async with async_playwright() as p:
        # 持久档案：show_browser=True 显示在屏幕上(调试)，否则离屏后台(用户看不到)
        ctx = await _launch_persistent(p, state_file, visible=show_browser)
        browser = ctx
        page = await _ctx_page(ctx)
        page.set_default_timeout(15000)     # 任何未显式设超时的操作最多等15秒,不再卡死30秒
        # 【仿小V猫·用API响应当可靠信号】监听视频号后端接口:
        #   post_clip_video_result = 视频传到CDN+登记完成(真正的"上传完成"信号)
        #   post_create            = 发布结果(errCode=0 才是真成功)
        sig = {"clip_ready": False, "create_done": False, "create_ok": False, "create_msg": ""}
        async def _on_resp(resp):
            try:
                u = resp.url or ""
                if "post_clip_video_result" in u:
                    sig["clip_ready"] = True
                elif "/post/post_create" in u and "micro/content" not in u:
                    sig["create_done"] = True
                    try:
                        j = await resp.json()
                        ec = j.get("errCode", j.get("errcode", -1))
                        sig["create_ok"] = (ec == 0)
                        sig["create_msg"] = j.get("errMsg") or j.get("errmsg") or str(j)[:120]
                    except Exception:
                        sig["create_ok"] = (resp.status == 200)
            except Exception:
                pass
        page.on("response", lambda r: asyncio.create_task(_on_resp(r)))
        try:
            await page.goto(CREATE_URL, wait_until="domcontentloaded", timeout=40000)
            await page.wait_for_timeout(2000)

            # 登录态失效会被视频号跳回 login.html —— 立刻明确报错，别空转重试
            cur = (page.url or "").lower()
            if "login" in cur or "/login.html" in cur:
                await browser.close()
                return False, "登录已失效，请重新扫码登录该账号"

            # 【剧集·仿小V猫】不点视频号弹窗，而是拦截 post_create 把 component 注入进去
            if drama:
                st("关联视频号剧集…")
                await _setup_drama_injection(page, drama, st)

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
            if not await _set_file_via_cdp(page, video_path):   # CDP塞路径(大文件不受50MB限制)
                await fi.set_input_files(str(video_path))         # 兜底

            # 【仿小V猫·可靠信号】等 post_clip_video_result 响应 = 视频真正传到CDN+登记完成。
            # (不靠 DOM 猜。245MB约2-3分钟,给8分钟上限。期间关掉冒出的验证弹窗(不reload)。
            #  若上传区始终空=视频没进去,补塞一次。)
            st("上传视频到CDN(约2-3分钟)…")
            last_reset = 0
            for _w in range(240):
                if sig["clip_ready"]:
                    st("视频已上传完成(CDN登记成功)")
                    break
                await asyncio.sleep(2)
                await _handle_verify_if_any(page, st)      # 弹验证就关(不reload,不丢视频)
                # 每30秒检查一次:上传区若还空(视频没进去),补塞
                if _w - last_reset >= 15:
                    last_reset = _w
                    try:
                        has_del = await page.evaluate("()=>{" + _DEEP_JS +
                            "return _dall().some(e=>(e.innerText||'').trim()==='删除');}")
                    except Exception:
                        has_del = True
                    if not has_del:
                        await _set_file_via_cdp(page, video_path)
            if not sig["clip_ready"]:
                await browser.close()
                return False, "视频上传超时(>8分钟未收到CDN登记完成)"

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
            # 聚焦编辑器：普通 click 偶发命中测试失败(离屏/被盖)→ force点 → JS聚焦 三级兜底
            try:
                await editor.click(timeout=8000)
            except Exception:
                # 点不动多半是被验证弹窗的蒙版盖住了 → 显示窗口让用户扫码，完成后重试聚焦
                blk = await _handle_verify_if_any(page, st)
                if blk:
                    await browser.close()
                    return False, blk
                try:
                    await editor.click(force=True, timeout=4000)
                except Exception:
                    try:
                        await editor.evaluate("el=>el.focus()")
                    except Exception:
                        pass
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
            # 位置：填了就用；没填=默认「不显示位置」(视频号会自动定位，主动清掉)
            st("设置地理位置")
            await _try_location(page, location or "")
            if collection:
                st("添加到合集")
                await _try_collection(page, collection)
            # 剧集关联在上传视频前就已设好路由注入(见前面 _setup_drama_injection)，这里无需再操作
            if cover_path:
                st("设置封面")
                await _try_cover(page, cover_path)
            if schedule:
                st("设置定时发布")
                await _try_schedule(page, schedule)

            # 发表前：关掉一切残留弹窗(选剧/合集的全屏蒙版会挡住发表按钮)，再处理可能的验证。
            # 视频已传完、「发表」按钮本就可点，不需要"等转码"——直接进发表。
            await _close_drama_modal(page)
            blk = await _handle_verify_if_any(page, st)
            if blk:
                await browser.close()
                return False, blk

            st("发表")
            # 【仿小V猫·可靠信号】点发表按钮触发页面发 post_create，用 post_create 的**响应**判成败
            # (errCode=0=成功)，不靠"是否跳转"。反复点直到监听到 post_create 响应。剧集已由路由注入。
            published = False
            # 【关键·实测发现】post_clip_video_result(CDN登记完成)后，视频号服务端还要处理视频~1-2分钟，
            # 期间「发表」按钮 disabled。旧代码固定点10次几乎耗尽才成功→视频稍慢就失败。改成**时间驱动**：
            # 持续等按钮真正 enabled 再点，最多等6分钟；每轮先关净弹窗蒙版。按钮一旦可点→真实可信点击。
            import time as _t
            deadline = _t.time() + 480      # 最多等8分钟(视频号处理大视频/高峰期可能久,留足余量)
            waited_disabled = 0
            while _t.time() < deadline:
                if sig["create_done"]:
                    break
                await _close_drama_modal(page)          # 关掉可能挡住的弹窗蒙版
                await _handle_verify_if_any(page, st)   # 关掉验证弹窗(不reload)
                # 查发表按钮：是否存在 + 是否 disabled(视频号处理中会禁用)
                try:
                    bs = await page.evaluate("()=>{" + _DEEP_JS + r"""
                        let b=_dall().find(e=>e.tagName==='BUTTON'&&(e.innerText||'').trim()==='发表');
                        if(!b){ let btns=_dqa('.post-view .form-btns button')||[];
                                b=btns.find(x=>(x.innerText||'').trim()==='发表'); }
                        if(!b) return {found:false};
                        const c=(b.className||'').toString();
                        const dis=!!b.disabled||b.getAttribute('aria-disabled')==='true'||/disabled|disable/.test(c);
                        return {found:true, disabled:dis};
                    }""")
                except Exception:
                    bs = {"found": False}
                if not bs.get("found"):
                    await asyncio.sleep(2)
                    continue
                if bs.get("disabled") and waited_disabled < 30:
                    # 按钮仍禁用=视频号还在处理视频，等它启用(每15秒报一次进度)。
                    # 但最多只等90秒(30×3s)——万一 disabled 判定误报，超时后照样尝试点(交给Playwright actionability仲裁)
                    waited_disabled += 1
                    if waited_disabled % 5 == 1:
                        st("视频号处理视频中，等发表按钮就绪…")
                    await asyncio.sleep(3)
                    continue
                # —— 按钮已启用 → 真实可信点击(Playwright locator 穿透 open shadow，isTrusted=true) ——
                clicked = False
                try:
                    loc = page.get_by_role("button", name="发表", exact=True)
                    if await loc.count():
                        b = loc.first
                        try:
                            await b.scroll_into_view_if_needed(timeout=3000)
                        except Exception:
                            pass
                        await b.click(timeout=5000)
                        clicked = True
                except Exception:
                    clicked = False
                if not clicked:
                    # 兜底：文本定位(穿透 shadow)真实点击
                    try:
                        loc = page.locator("button:has-text('发表')")
                        n = await loc.count()
                        for i in range(n):
                            bb = loc.nth(i)
                            try:
                                if (await bb.inner_text()).strip() == "发表":
                                    await bb.scroll_into_view_if_needed(timeout=2000)
                                    await bb.click(timeout=4000)
                                    clicked = True
                                    break
                            except Exception:
                                pass
                    except Exception:
                        pass
                await page.wait_for_timeout(1200)
                # 二次确认框：点它的主按钮
                try:
                    cf = page.locator('.post-check-dialog .weui-desktop-btn_primary, '
                                      '.weui-desktop-dialog__ft .weui-desktop-btn_primary')
                    if await cf.count():
                        await cf.first.click(timeout=3000)
                except Exception:
                    pass
                # 等这一轮的 post_create 响应(最多8秒)
                for _ in range(8):
                    await asyncio.sleep(1)
                    if sig["create_done"]:
                        break
            # 用 post_create 响应判定成败
            if sig["create_done"]:
                if sig["create_ok"]:
                    published = True
                else:
                    await browser.close()
                    return False, f"视频号拒绝发布：{sig['create_msg']}"
            if not published:
                # 兜底：跳转到作品列表也算成功
                if "post/list" in (page.url or "") or "post_list" in (page.url or ""):
                    published = True
            if not published:
                # 【诊断】发表没触发 post_create——dump 按钮真实状态+截图，下次据此定位
                diag = {}
                try:
                    diag = await page.evaluate("()=>{" + _DEEP_JS + r"""
                        let b=_dall().find(e=>e.tagName==='BUTTON'&&(e.innerText||'').trim()==='发表');
                        let hasVideo=_dall().some(e=>(e.innerText||'').trim()==='删除');
                        let mask=_dall().some(e=>{try{const c=(e.className||'').toString();
                          const r=e.getBoundingClientRect();
                          return (c.includes('mask')||c.includes('overlay'))&&r.width>500&&r.height>400;}catch(_){return false;}});
                        let r=b?b.getBoundingClientRect():null;
                        return {found:!!b, disabled:b?!!b.disabled:null,
                                cls:b?(b.className||''):'', ariaDis:b?b.getAttribute('aria-disabled'):null,
                                rect:r?{w:Math.round(r.width),h:Math.round(r.height),x:Math.round(r.x),y:Math.round(r.y)}:null,
                                hasVideo:hasVideo, maskOver:mask, url:location.href};
                    }""")
                except Exception as _e:
                    diag = {"evalErr": str(_e)[:80]}
                st(f"[诊断] 发表未触发 post_create: {diag}")
                if err_dir:
                    try:
                        await page.screenshot(path=str(Path(err_dir) / "channels_publish_fail.png"))
                    except Exception:
                        pass
                await browser.close()
                return False, f"点了发表但未收到发布响应 诊断={diag}"
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
    """派发完整鼠标事件序列点一个元素（视频号 React 控件普通 click 点不动，仿小V猫源码）。
    【穿透 wujie-app shadowRoot】：视频号后台是微前端，控件在 shadow DOM 里，原生 querySelector 查不到。"""
    try:
        return await page.evaluate(
            "(sel)=>{" + _DEEP_JS + """
               const el=_dq(sel); if(!el) return false;
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
    """地理位置：填了 location 就选它；没填(空)=选「不显示位置」清掉自动定位。"""
    try:
        # 打开位置下拉/输入
        opened = False
        for sel in ('#location', '.position-display', '[class*="location"]', 'text=位置'):
            try:
                loc = page.locator(sel)
                if await loc.count():
                    await loc.first.click(timeout=2500)
                    opened = True
                    await page.wait_for_timeout(800)
                    break
            except Exception:
                pass
        if not location:
            # 选「不显示位置」项
            for t in ("不显示位置", "不显示"):
                opt = page.get_by_text(t, exact=False)
                if await opt.count():
                    try:
                        await opt.first.click(timeout=2500)
                        break
                    except Exception:
                        pass
            await page.keyboard.press("Escape")
            return
        inp = page.locator('#location input, input[placeholder*="位置"], input[placeholder*="搜索"]').first
        if await inp.count():
            await inp.fill(location, timeout=3000)
            await page.wait_for_timeout(1500)   # 等 POI 搜索
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
                "()=>{" + _DEEP_JS + """
                   const list=_dq('.choose-finder-area .finder-list');
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


async def _setup_drama_injection(page, drama: str, st):
    """【仿小V猫】关联视频号剧集 = 拿到剧集 exportId，拦截 /post/post_create 请求，
    把 objectDesc.component = {id:exportId, type:8, title:剧名} 注入进去。
    完全不点视频号那个不可靠的剧集弹窗。drama 传剧名(自动查exportId)或直接传exportId。"""
    if not drama:
        return
    export_id, title = "", drama
    if drama.startswith("event/") or drama.startswith("UzF") or len(drama) > 60:
        export_id = drama
    else:
        try:
            res = await page.evaluate("""async (name)=>{
                const url='/cgi-bin/mmfinderassistant-bin/post/search_drama_component';
                for(let cp=1; cp<=80; cp++){
                    const r=await fetch(url,{method:'POST',headers:{'content-type':'application/json'},
                        body:JSON.stringify({currentPage:cp,pageSize:5,sceneType:3,scene:7,reqScene:7,
                            rawKeyBuff:'',pluginSessionId:null,timestamp:String(Date.now())}),credentials:'include'});
                    const j=await r.json(); const d=(j&&j.data)||{}; const lst=d.list||[];
                    for(const it of lst){ if((it.name||'')===name) return {id:it.id,name:it.name}; }
                    for(const it of lst){ if((it.name||'').includes(name)) return {id:it.id,name:it.name}; }
                    if(lst.length<5) break;
                }
                return null;
            }""", drama)
            if res:
                export_id, title = res.get("id", ""), res.get("name", drama)
        except Exception:
            pass
    if not export_id:
        st(f"没找到剧集「{drama}」，跳过挂剧继续发布")
        return
    st(f"剧集「{title}」将随发布注入")
    comp = {"id": export_id, "type": 8, "title": title}

    async def _route(route):
        try:
            body = route.request.post_data
            if body:
                data = json.loads(body)
                od = data.get("objectDesc")
                if isinstance(od, dict):
                    od["component"] = comp
                    await route.continue_(post_data=json.dumps(data, ensure_ascii=False))
                    return
        except Exception:
            pass
        try:
            await route.continue_()
        except Exception:
            pass
    try:
        await page.route("**/post/post_create*", _route)
    except Exception:
        pass


async def _try_drama(page, drama: str):
    """扩展链接·视频号剧集(CPS分销剧)：真机摸清的流程(发表页实测)：
    ①「链接」下拉点「选择链接」→②菜单选「视频号剧集」→③点新出现的「选择需要添加的视频号剧集」开弹窗
    →④弹窗(标题"选择需要关联的视频号剧集")里搜剧名→⑤**直接点剧名那一行**(视频号弹窗点行即选中并关闭,
    没有单选圈、没有确定按钮)。drama 传剧名。弹窗偶发不开,重试几次。"""
    if not drama:
        return
    try:
        # ① 链接下拉
        for t in ("选择链接", "链接"):
            b = page.get_by_text(t, exact=True)
            if await b.count():
                await b.first.click(timeout=5000)
                await page.wait_for_timeout(1000)
                break
        # ② 选类型「视频号剧集」
        vt = page.get_by_text("视频号剧集", exact=True)
        if await vt.count():
            await vt.first.click(timeout=5000)
            await page.wait_for_timeout(1800)
        # ③ 点「选择需要添加的视频号剧集」开弹窗(偶发不开,重试)
        opened = False
        for _ in range(3):
            trig = page.get_by_text("选择需要添加的视频号剧集", exact=False)
            if await trig.count():
                await trig.first.click()
                await page.wait_for_timeout(3000)
            if await page.get_by_text("选择需要关联的视频号剧集", exact=False).count():
                opened = True
                break
            await page.wait_for_timeout(1200)
        if not opened:
            return
        # ④ 弹窗里搜剧名：每页只显示5个、靠翻页，所以必须用搜索框直接搜(别翻页)。
        #    用键盘逐字真实输入(fill偶发不触发视频号React搜索) + 回车。
        try:
            sbox = page.get_by_placeholder("搜索内容")
            if not await sbox.count():
                sbox = page.locator('input[placeholder*="搜索"]')
            if await sbox.count():
                await sbox.first.click(timeout=4000)
                try:
                    await sbox.first.fill("", timeout=2000)      # 先清空
                except Exception:
                    pass
                await page.keyboard.type(drama, delay=35)        # 逐字输入→触发搜索
                await page.wait_for_timeout(500)
                await page.keyboard.press("Enter")
                await page.wait_for_timeout(2500)                # 等搜索结果
        except Exception:
            pass
        # ⑤ 点剧名那一行——【关键】视频号剧集行是 React 控件，普通 click 点不动，
        #    必须派发完整鼠标事件序列(mousedown/mouseup/click)到那一行(含剧名+集数+封面的行)。
        for attempt in range(3):
            ok = await page.evaluate(
                "(name)=>{" + _DEEP_JS + r"""
                  const all=_dall();
                  const modal=all.find(e=>(e.innerText||'').includes('选择需要关联的视频号剧集'));
                  const scope=modal?_dall(modal.shadowRoot||undefined):all;
                  const pool=modal?[...modal.querySelectorAll('*')]:all;
                  let target=null;
                  pool.forEach(el=>{
                    if(target) return;
                    const t=(el.innerText||'').trim();
                    if(t.includes(name) && /\d+\s*集/.test(t) && t.length<40 && el.querySelector('img')) target=el;
                  });
                  if(!target){
                    const cand=pool.filter(e=>(e.innerText||'').trim()===name);
                    const span=cand[cand.length-1];
                    target=span?(span.closest('li')||span.parentElement):null;
                  }
                  if(!target) return false;
                  ['mouseenter','mouseover','mousedown','mouseup','click'].forEach(t=>
                    target.dispatchEvent(new MouseEvent(t,{bubbles:true,cancelable:true,view:window})));
                  return true;
                }""", drama)
            await page.wait_for_timeout(1500)
            # 弹窗关了=选中成功
            if not await page.get_by_text("选择需要关联的视频号剧集", exact=False).count():
                break
    except Exception:
        pass
    # 兜底：弹窗若还开着(挂剧失败/剧不在列表)，务必关掉它——否则全屏蒙版挡死后续转码检测/发表。
    await _close_drama_modal(page)


async def _close_drama_modal(page):
    """把"选择需要关联的视频号剧集"弹窗关干净(深度穿透 shadow 找×派发点击 + Escape，循环确认)。"""
    for _ in range(6):
        try:
            if not await page.get_by_text("选择需要关联的视频号剧集", exact=False).count():
                return
        except Exception:
            return
        try:
            await page.evaluate("()=>{" + _DEEP_JS + r"""
                const all=_dall();
                const modal=all.find(e=>{
                  try{ return (e.innerText||'').includes('选择需要关联的视频号剧集')
                       && e.getBoundingClientRect && e.getBoundingClientRect().width>200; }catch(_){ return false; }
                });
                if(!modal) return 'nomodal';
                // 关闭×：class含close / 标题行右上角的图标(svg/i/use) / 标题父节点最后一个子元素
                let x=modal.querySelector('[class*="close"],[class*="Close"],.weui-desktop-dialog__close');
                if(!x) x=modal.querySelector('svg,i.icon,use');
                if(!x){ const hdr=[...modal.querySelectorAll('*')].find(e=>(e.innerText||'').trim().startsWith('选择需要关联'));
                        if(hdr&&hdr.parentElement) x=hdr.parentElement.lastElementChild; }
                if(x){ ['mouseenter','mousedown','mouseup','click'].forEach(t=>
                        x.dispatchEvent(new MouseEvent(t,{bubbles:true,cancelable:true,view:window}))); return 'clicked'; }
                return 'nox';
            }""")
        except Exception:
            pass
        try:
            await page.keyboard.press("Escape")
        except Exception:
            pass
        await page.wait_for_timeout(700)


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
