# -*- coding: utf-8 -*-
"""抖音博主更新监控 — 定时检查博主新作品，弹窗提醒 + 自动下载无水印视频 + 本地面板"""
import asyncio
import json
import os
import re
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse, StreamingResponse
from pydantic import BaseModel

from f2.apps.douyin.handler import DouyinHandler
from f2.apps.douyin.utils import TokenManager, SecUserIdFetcher, AwemeIdFetcher

AID_RE = r"(?:modal_id=|/video/|/note/|/share/video/|/share/note/)(\d{6,})"


async def resolve_aweme_id(text: str):
    """从链接/口令/纯ID里解析出作品ID（短链会跟随重定向）"""
    text = text.strip()
    if re.fullmatch(r"\d{6,}", text):
        return text
    m = re.search(r"https?://\S+", text)
    if not m:
        return None
    link = m.group(0).rstrip("，。、）)]】")
    probe = link
    if not re.search(AID_RE, probe):
        try:
            import httpx as _httpx
            async with _httpx.AsyncClient(follow_redirects=True, timeout=20,
                                          headers={"User-Agent": UA}) as c:
                probe = str((await c.get(link)).url)
        except Exception:
            probe = link
    mm = re.search(AID_RE, probe)
    if mm:
        return mm.group(1)
    try:
        return await AwemeIdFetcher.get_aweme_id(link)
    except Exception:
        return None

# 打包后(PyInstaller)与源码运行的路径不同：
#   静态资源(static)在包内(_MEIPASS)；data/downloads 要放在 exe 旁边(持久、可写)
if getattr(sys, "frozen", False):
    BASE = Path(sys._MEIPASS)                 # 只读资源
    APP_DIR = Path(sys.executable).parent     # 可写数据
else:
    BASE = Path(__file__).parent
    APP_DIR = BASE
DATA = APP_DIR / "data"
DL = APP_DIR / "downloads"
DATA.mkdir(exist_ok=True)
DL.mkdir(exist_ok=True)
CONFIG_FILE = DATA / "config.json"
STATE_FILE = DATA / "state.json"
PORT = 8790
VERSION = "1.0.0"
# 更新检查：指向 GitHub 上的 version.json
UPDATE_RAW_URL = "https://raw.githubusercontent.com/dangthiphuong2491991-a11y/douyin-baokuan-monitor/master/version.json"
RELEASE_PAGE = "https://github.com/dangthiphuong2491991-a11y/douyin-baokuan-monitor/releases"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")

_lock = threading.Lock()


def _load(path: Path, default):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return default


def _save(path: Path, obj):
    with _lock:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(path)


config = _load(CONFIG_FILE, {"interval_minutes": 5, "bloggers": []})
state = _load(STATE_FILE, {"seen": {}, "updates": [], "last_check": None, "errors": []})
POSTS_CACHE = {}  # sec_uid -> {aweme_id: 完整 aweme dict}，供手动下载取新鲜地址


def get_dl() -> Path:
    """当前下载根目录（可在设置里自定义，默认 ./downloads）"""
    d = Path(config.get("download_dir") or DL)
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception:
        d = DL
        d.mkdir(exist_ok=True)
    return d


def is_logged_in() -> bool:
    c = config.get("cookie") or ""
    return "sessionid" in c


def make_kwargs():
    # 已登录：直接用用户真实 cookie（能翻页拿更多作品）
    login_cookie = config.get("cookie")
    if login_cookie and "sessionid" in login_cookie:
        cookie = login_cookie
    else:
        try:
            mstoken = TokenManager.gen_real_msToken()
        except Exception:
            mstoken = ""
        cookie = f"ttwid={TokenManager.gen_ttwid()};"
        if mstoken:
            cookie += f" msToken={mstoken};"
    return {
        "headers": {"User-Agent": UA, "Referer": "https://www.douyin.com/"},
        "cookie": cookie,
        "proxies": {"http://": None, "https://": None},
        "mode": "post",
        "timeout": 30,
    }


def set_login_cookie(cookie_str: str):
    config["cookie"] = (cookie_str or "").strip()
    _save(CONFIG_FILE, config)


def clear_login_cookie():
    config.pop("cookie", None)
    _save(CONFIG_FILE, config)


def sanitize(name: str, maxlen=50) -> str:
    name = re.sub(r'[\\/:*?"<>|\r\n#]+', "_", name or "").strip()
    return (name[:maxlen] or "未命名").rstrip(". ")


def fmt_ts(ts) -> str:
    try:
        return datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return str(ts)


async def fetch_posts_raw(sec_uid: str, max_count: int = 20) -> list:
    """抓取博主作品（可多页累积），返回 aweme 原始 dict 列表"""
    kw = make_kwargs()
    if max_count > 20:
        kw["timeout"] = 10  # f2 用 timeout 值当翻页间隔，多页时调短些（兼顾请求超时）
    h = DouyinHandler(kw)
    out = []
    async for posts in h.fetch_user_post_videos(
        sec_user_id=sec_uid, page_counts=20, max_counts=max_count
    ):
        out.extend(posts._to_raw().get("aweme_list") or [])
        if len(out) >= max_count:
            break
    return out[:max_count]


async def fetch_profile(sec_uid: str) -> dict:
    h = DouyinHandler(make_kwargs())
    p = await h.fetch_user_profile(sec_uid)
    return {
        "nickname": p.nickname_raw or p.nickname,
        "avatar": p.avatar_url,
        "follower_count": p.follower_count,
        "aweme_count": p.aweme_count,
        "signature": (p.signature_raw or "")[:100],
    }


def pick_video_url(aweme: dict) -> str | None:
    video = aweme.get("video") or {}
    rates = video.get("bit_rate") or []
    best, best_br = None, -1
    for r in rates:
        urls = ((r.get("play_addr") or {}).get("url_list")) or []
        if urls and r.get("bit_rate", 0) > best_br:
            best, best_br = urls[0], r.get("bit_rate", 0)
    if best:
        return best
    urls = ((video.get("play_addr") or {}).get("url_list")) or []
    return urls[-1] if urls else None


def pick_cover_url(aweme: dict) -> str | None:
    video = aweme.get("video") or {}
    for key in ("cover", "origin_cover", "dynamic_cover"):
        urls = ((video.get(key) or {}).get("url_list")) or []
        if urls:
            return urls[0]
    return None


async def download_file(url: str, dest: Path) -> bool:
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=300,
                                     headers={"User-Agent": UA, "Referer": "https://www.douyin.com/"}) as c:
            async with c.stream("GET", url) as r:
                if r.status_code != 200:
                    return False
                dest.parent.mkdir(parents=True, exist_ok=True)
                tmp = dest.with_suffix(dest.suffix + ".part")
                with open(tmp, "wb") as f:
                    async for chunk in r.aiter_bytes(1 << 16):
                        f.write(chunk)
                tmp.replace(dest)
                return True
    except Exception as e:
        log_err(f"下载失败 {dest.name}: {e}")
        return False


def notify(title: str, msg: str, icon: Path | None = None):
    try:
        from winotify import Notification, audio
        t = Notification(app_id="抖音博主监控", title=title, msg=msg[:180],
                         icon=str(icon) if icon and icon.exists() else "",
                         launch=f"http://127.0.0.1:{PORT}/")
        t.set_audio(audio.Default, loop=False)
        t.show()
    except Exception as e:
        log_err(f"弹窗通知失败: {e}")


def log_err(msg: str):
    print(f"[ERR] {msg}", flush=True)
    state["errors"] = ([{"time": datetime.now().strftime("%H:%M:%S"), "msg": str(msg)[:300]}]
                       + state.get("errors", []))[:20]


async def process_new_aweme(blogger: dict, aweme: dict):
    """发现新作品：只记录信息 + 下封面（缩略图）+ 弹窗提醒，不自动下正片。缓存 aweme 供手动下载/播放。"""
    aid = str(aweme.get("aweme_id"))
    nickname = blogger.get("nickname", "")
    desc = (aweme.get("desc") or "").strip()
    stats = aweme.get("statistics") or {}
    is_images = bool(aweme.get("images"))
    dur_ms = (aweme.get("video") or {}).get("duration") or 0

    dl = get_dl()
    folder = dl / sanitize(nickname, 30)
    date_tag = datetime.fromtimestamp(int(aweme.get("create_time", time.time()))).strftime("%Y%m%d")
    stem = f"{date_tag}_{sanitize(desc, 40)}_{aid[-6:]}"

    # 缓存完整 aweme，供手动下载 / 在线播放取地址
    POSTS_CACHE.setdefault(blogger["sec_user_id"], {})[aid] = aweme

    rec = {
        "aweme_id": aid,
        "sec_user_id": blogger["sec_user_id"],
        "nickname": nickname,
        "desc": desc or "(无标题)",
        "type": "图集" if is_images else "视频",
        "create_time": fmt_ts(aweme.get("create_time")),
        "found_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "duration": f"{dur_ms // 60000}:{dur_ms % 60000 // 1000:02d}" if dur_ms else "",
        "digg": stats.get("digg_count", 0),
        "comment": stats.get("comment_count", 0),
        "collect": stats.get("collect_count", 0),
        "share": stats.get("share_count", 0),
        "url": f"https://www.douyin.com/video/{aid}",
        "cover": "",
    }

    cover_url = pick_cover_url(aweme)
    if cover_url:
        cover_path = folder / f"{stem}.jpg"
        if await download_file(cover_url, cover_path):
            rec["cover"] = f"/files/{cover_path.relative_to(dl).as_posix()}"

    state["updates"].insert(0, rec)
    state["updates"] = state["updates"][:500]
    _save(STATE_FILE, state)

    notify(f"📢 {nickname} 更新了新{rec['type']}",
           f"{rec['desc']}\n发布于 {rec['create_time']}",
           icon=cover_path if rec["cover"] else None)


async def download_aweme_media(nickname: str, aweme: dict) -> tuple:
    """手动下载单个作品（视频或图集）到 downloads/昵称/。返回 (ok, 文件url)"""
    aid = str(aweme.get("aweme_id"))
    desc = (aweme.get("desc") or "").strip()
    is_images = bool(aweme.get("images"))
    dl = get_dl()
    folder = dl / sanitize(nickname, 30)
    date_tag = datetime.fromtimestamp(int(aweme.get("create_time", time.time()))).strftime("%Y%m%d")
    stem = f"{date_tag}_{sanitize(desc, 40)}_{aid[-6:]}"

    cover_url = pick_cover_url(aweme)
    if cover_url:
        await download_file(cover_url, folder / f"{stem}.jpg")

    if is_images:
        n = 0
        for i, img in enumerate(aweme.get("images") or [], 1):
            urls = img.get("url_list") or []
            if urls and await download_file(urls[-1], folder / f"{stem}_{i:02d}.jpeg"):
                n += 1
        return (n > 0, f"/files/{folder.relative_to(dl).as_posix()}" if n else "")
    else:
        vurl = pick_video_url(aweme)
        if vurl:
            vp = folder / f"{stem}.mp4"
            if await download_file(vurl, vp):
                return True, f"/files/{vp.relative_to(dl).as_posix()}"
    return False, ""


def _downloaded_tags(nickname: str) -> set:
    """已下载作品的 id 尾6位集合。只认正片（.mp4 视频 / _NN.jpeg 图集），封面 .jpg 不算下载。"""
    folder = get_dl() / sanitize(nickname, 30)
    tags = set()
    if folder.exists():
        for f in folder.iterdir():
            # 视频 {stem}.mp4 或 图集 {stem}_01.jpeg，排除封面 {stem}.jpg
            m = re.search(r"_(\d{6})\.mp4$", f.name) or re.search(r"_(\d{6})_\d+\.jpe?g$", f.name)
            if m:
                tags.add(m.group(1))
    return tags


def _blogger_nickname(sec_uid: str) -> str:
    for b in config["bloggers"]:
        if b["sec_user_id"] == sec_uid:
            return b.get("nickname") or sec_uid[:16]
    return ""


async def resolve_aweme(aid: str) -> dict:
    """按作品ID找到完整 aweme：先查各缓存，缺失/无地址则重新拉详情"""
    aid = str(aid)
    for cache in POSTS_CACHE.values():
        a = cache.get(aid)
        if a and _has_media(a):
            return a
    try:
        v = await DouyinHandler(make_kwargs()).fetch_one_video(aweme_id=aid)
        full = (v._to_raw() or {}).get("aweme_detail")
        if full:
            POSTS_CACHE.setdefault("_resolved", {})[aid] = full
            return full
    except Exception as e:
        log_err(f"取作品 {aid} 详情失败: {e}")
    return None


async def check_blogger(blogger: dict, baseline: bool = False) -> int:
    sec_uid = blogger["sec_user_id"]
    awemes = await fetch_posts_raw(sec_uid)
    if not awemes:
        log_err(f"{blogger.get('nickname', sec_uid)}: 未获取到作品（可能是风控，稍后自动重试）")
        return 0
    seen = set(state["seen"].get(sec_uid, []))
    new_items = [a for a in awemes if str(a.get("aweme_id")) not in seen]
    if baseline or not seen:
        # 首次添加：只记录现有作品，不提醒不下载
        state["seen"][sec_uid] = list(seen | {str(a.get("aweme_id")) for a in awemes})
        _save(STATE_FILE, state)
        return 0
    for a in sorted(new_items, key=lambda x: x.get("create_time", 0)):
        state["seen"][sec_uid].append(str(a.get("aweme_id")))
        await process_new_aweme(blogger, a)
    if new_items:
        _save(STATE_FILE, state)
    return len(new_items)


_check_now = asyncio.Event()


async def monitor_loop():
    await asyncio.sleep(3)
    while True:
        for b in list(config["bloggers"]):
            try:
                n = await check_blogger(b)
                if n:
                    print(f"[NEW] {b.get('nickname')}: {n} 条新作品", flush=True)
            except Exception as e:
                log_err(f"检查 {b.get('nickname')} 出错: {e}")
            await asyncio.sleep(2)
        state["last_check"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        _save(STATE_FILE, state)
        try:
            await asyncio.wait_for(_check_now.wait(), timeout=max(60, config["interval_minutes"] * 60))
        except asyncio.TimeoutError:
            pass
        _check_now.clear()


app = FastAPI()


@app.get("/files/{path:path}")
def serve_file(path: str):
    """从当前下载目录读取文件（目录可自定义，故用动态路由而非静态挂载）"""
    base = get_dl().resolve()
    target = (base / path).resolve()
    if target.is_file() and target.is_relative_to(base):
        return FileResponse(str(target))
    return JSONResponse({"error": "not found"}, status_code=404)


@app.get("/api/stream/{aid}")
async def api_stream(aid: str, request: Request):
    """在线播放代理：带 Referer 抓抖音视频流，支持 Range 拖动进度条"""
    aweme = await resolve_aweme(aid)
    if not aweme:
        return JSONResponse({"error": "取不到该视频"}, status_code=404)
    url = pick_video_url(aweme)
    if not url:
        return JSONResponse({"error": "该作品没有视频（可能是图集）"}, status_code=404)

    up_headers = {"User-Agent": UA, "Referer": "https://www.douyin.com/"}
    rng = request.headers.get("range")
    if rng:
        up_headers["Range"] = rng
    client = httpx.AsyncClient(follow_redirects=True, timeout=None)
    r = await client.send(client.build_request("GET", url, headers=up_headers), stream=True)
    passthru = {}
    for h in ("content-range", "content-length", "accept-ranges"):
        if h in r.headers:
            passthru[h] = r.headers[h]
    passthru.setdefault("accept-ranges", "bytes")

    async def gen():
        try:
            async for chunk in r.aiter_bytes(1 << 16):
                yield chunk
        finally:
            await r.aclose()
            await client.aclose()

    return StreamingResponse(gen(), status_code=r.status_code, headers=passthru,
                             media_type=r.headers.get("content-type", "video/mp4"))


@app.get("/api/aweme_images/{aid}")
async def api_aweme_images(aid: str):
    """图集的图片地址（前端用 no-referrer 直接展示）"""
    aweme = await resolve_aweme(aid)
    imgs = []
    for im in ((aweme or {}).get("images") or []):
        urls = im.get("url_list") or []
        if urls:
            imgs.append(urls[-1])
    return {"images": imgs}


@app.on_event("startup")
async def _startup():
    asyncio.create_task(monitor_loop())


@app.get("/", response_class=HTMLResponse)
def index():
    return (BASE / "static" / "index.html").read_text(encoding="utf-8")


@app.get("/api/status")
def api_status():
    tags = {}
    updates = []
    for u in state["updates"][:200]:
        nk = u.get("nickname", "")
        if nk not in tags:
            tags[nk] = _downloaded_tags(nk)
        u = dict(u)
        u["downloaded"] = str(u.get("aweme_id", ""))[-6:] in tags[nk]
        updates.append(u)
    return {
        "interval_minutes": config["interval_minutes"],
        "bloggers": config["bloggers"],
        "updates": updates,
        "last_check": state.get("last_check"),
        "errors": state.get("errors", [])[:5],
        "download_dir": str(get_dl()),
        "logged_in": is_logged_in(),
        "version": VERSION,
    }


class AddBody(BaseModel):
    url: str


@app.post("/api/bloggers")
async def api_add_blogger(body: AddBody):
    url = body.url.strip()
    # 允许直接贴 sec_user_id
    if re.fullmatch(r"MS4wLjAB[\w\-=]+", url):
        sec_uid = url
    else:
        # 从整段分享口令里提取链接（短链/主页/视频都可）
        m = re.search(r"https?://\S+", url)
        if not m:
            return JSONResponse({"error": "没找到链接。请把抖音「分享 → 复制链接」的整段文字粘进来"}, status_code=400)
        link = m.group(0).rstrip("，。、）)]】")
        sec_uid = None
        # ① 先按博主主页解析
        try:
            sec_uid = await SecUserIdFetcher.get_sec_user_id(link)
        except Exception:
            sec_uid = None
        # ② 主页解析不了 → 可能是单条视频链接，反查作者
        if not sec_uid:
            AID_RE = r"(?:modal_id=|/video/|/note/|/share/video/|/share/note/)(\d{6,})"
            probe = link
            # 链接里没有内嵌作品ID（如短链 v.douyin.com）→ 跟随重定向拿真实地址
            if not re.search(AID_RE, probe):
                try:
                    async with httpx.AsyncClient(follow_redirects=True, timeout=20,
                                                 headers={"User-Agent": UA}) as c:
                        probe = str((await c.get(link)).url)
                except Exception:
                    probe = link
            mm = re.search(AID_RE, probe)
            aid = mm.group(1) if mm else None
            if not aid:
                try:
                    aid = await AwemeIdFetcher.get_aweme_id(link)
                except Exception:
                    aid = None
            if aid:
                try:
                    v = await DouyinHandler(make_kwargs()).fetch_one_video(aweme_id=aid)
                    sec_uid = v.sec_user_id
                except Exception:
                    sec_uid = None
        if not sec_uid:
            return JSONResponse(
                {"error": "无法识别博主。请粘贴抖音「分享 → 复制链接」的主页或视频口令整段文字"},
                status_code=400)
    if any(b["sec_user_id"] == sec_uid for b in config["bloggers"]):
        return JSONResponse({"error": "该博主已在监控列表中"}, status_code=400)
    try:
        prof = await fetch_profile(sec_uid)
    except Exception as e:
        prof = {"nickname": sec_uid[:16], "avatar": "", "follower_count": "", "aweme_count": "", "signature": ""}
        log_err(f"获取博主资料失败(不影响监控): {e}")
    blogger = {"sec_user_id": sec_uid, "added_at": datetime.now().strftime("%Y-%m-%d %H:%M"), **prof}
    config["bloggers"].append(blogger)
    _save(CONFIG_FILE, config)
    try:
        await check_blogger(blogger, baseline=True)  # 基线：现有作品不算更新
    except Exception as e:
        log_err(f"基线抓取失败: {e}")
    return {"ok": True, "blogger": blogger}


@app.delete("/api/bloggers/{sec_uid}")
def api_del_blogger(sec_uid: str):
    config["bloggers"] = [b for b in config["bloggers"] if b["sec_user_id"] != sec_uid]
    _save(CONFIG_FILE, config)
    return {"ok": True}


class IntervalBody(BaseModel):
    minutes: int


@app.post("/api/interval")
def api_interval(body: IntervalBody):
    config["interval_minutes"] = max(1, min(240, body.minutes))
    _save(CONFIG_FILE, config)
    return {"ok": True}


@app.post("/api/check")
def api_check():
    _check_now.set()
    return {"ok": True}


@app.get("/api/blogger_posts/{sec_uid}")
async def api_blogger_posts(sec_uid: str, cursor: int = 0):
    """抓取博主的作品列表（供浏览+手动挑选下载），并缓存完整数据"""
    try:
        h = DouyinHandler(make_kwargs())
        awemes = []
        async for posts in h.fetch_user_post_videos(
            sec_user_id=sec_uid, max_cursor=cursor, page_counts=20, max_counts=20
        ):
            awemes = posts._to_raw().get("aweme_list") or []
            break
    except Exception as e:
        return JSONResponse({"error": f"获取作品失败: {e}"}, status_code=400)

    cache = POSTS_CACHE.setdefault(sec_uid, {})
    nickname = _blogger_nickname(sec_uid)
    if not nickname and awemes:
        nickname = ((awemes[0].get("author") or {}).get("nickname")) or sec_uid[:16]
    done = _downloaded_tags(nickname)

    items = []
    for a in awemes:
        aid = str(a.get("aweme_id"))
        cache[aid] = a
        stats = a.get("statistics") or {}
        dur = (a.get("video") or {}).get("duration") or 0
        items.append({
            "aweme_id": aid,
            "desc": (a.get("desc") or "").strip() or "(无标题)",
            "type": "图集" if a.get("images") else "视频",
            "cover": pick_cover_url(a) or "",
            "create_time": fmt_ts(a.get("create_time")),
            "duration": f"{dur // 60000}:{dur % 60000 // 1000:02d}" if dur else "",
            "digg": stats.get("digg_count", 0),
            "downloaded": aid[-6:] in done,
        })
    return {"nickname": nickname, "count": len(items), "posts": items}


class DownloadBody(BaseModel):
    sec_user_id: str
    aweme_ids: list[str]


def _has_media(a: dict) -> bool:
    """该 aweme 是否带真正可下载的媒体地址（相关推荐的预览版没有 play_addr）"""
    if not a:
        return False
    if a.get("images"):
        return any((img.get("url_list")) for img in a["images"])
    return bool(pick_video_url(a))


@app.post("/api/download_selected")
async def api_download_selected(body: DownloadBody):
    nickname = _blogger_nickname(body.sec_user_id)
    cache = POSTS_CACHE.get(body.sec_user_id, {})
    results = []
    for aid in body.aweme_ids:
        aid = str(aid)
        aweme = cache.get(aid)
        # 缓存缺失，或（相关推荐预览版）没有下载地址 → 按 ID 重新拉完整详情
        if aweme is None or not _has_media(aweme):
            try:
                v = await DouyinHandler(make_kwargs()).fetch_one_video(aweme_id=aid)
                full = (v._to_raw() or {}).get("aweme_detail")
                if _has_media(full):
                    aweme = full
            except Exception as e:
                if aweme is None:
                    results.append({"aweme_id": aid, "ok": False, "err": str(e)[:80]})
                    continue
        if not aweme:
            results.append({"aweme_id": aid, "ok": False, "err": "无数据"})
            continue
        nm = nickname or ((aweme.get("author") or {}).get("nickname")) or "未知博主"
        try:
            ok, _ = await download_aweme_media(nm, aweme)
            results.append({"aweme_id": aid, "ok": ok})
        except Exception as e:
            log_err(f"手动下载 {aid} 失败: {e}")
            results.append({"aweme_id": aid, "ok": False, "err": str(e)[:80]})
    ok_n = sum(1 for r in results if r["ok"])
    return {"ok": True, "success": ok_n, "total": len(results), "results": results}


class DiscoverBody(BaseModel):
    seeds: list[str]          # 种子视频链接/口令/ID
    hours: int = 24           # 只要最近 N 小时内发布的
    min_like: int = 20000     # 点赞门槛（代理播放量）
    pages: int = 2            # 每个种子抓几页相关推荐（每页约20条，页间有30秒防风控间隔）


@app.post("/api/discover")
async def api_discover(body: DiscoverBody):
    seeds = [s for s in (body.seeds or []) if s.strip()]
    if not seeds:
        return JSONResponse({"error": "请至少粘贴一条种子视频链接"}, status_code=400)
    pages = max(1, min(4, body.pages))
    now = time.time()
    seen_authors_tags = {}
    found = {}   # aweme_id -> item
    cache = POSTS_CACHE.setdefault("discover", {})
    scanned = 0

    for seed in seeds:
        aid = await resolve_aweme_id(seed)
        if not aid:
            log_err(f"发现：种子无法解析 {seed[:40]}")
            continue
        try:
            h = DouyinHandler(make_kwargs())
            async for rel in h.fetch_related_videos(aweme_id=aid, page_counts=20, max_counts=pages * 20):
                for a in (rel._to_raw().get("aweme_list") or []):
                    scanned += 1
                    rid = str(a.get("aweme_id"))
                    if rid in found:
                        continue
                    ct = a.get("create_time") or 0
                    age_h = (now - ct) / 3600 if ct else 1e9
                    digg = (a.get("statistics") or {}).get("digg_count") or 0
                    if (body.hours and age_h > body.hours) or digg < body.min_like:
                        continue
                    author = a.get("author") or {}
                    nick = author.get("nickname") or "未知博主"
                    if nick not in seen_authors_tags:
                        seen_authors_tags[nick] = _downloaded_tags(nick)
                    cache[rid] = a
                    dur = (a.get("video") or {}).get("duration") or 0
                    found[rid] = {
                        "aweme_id": rid,
                        "desc": (a.get("desc") or "").strip() or "(无标题)",
                        "author": nick,
                        "sec_user_id": author.get("sec_uid") or "",
                        "type": "图集" if a.get("images") else "视频",
                        "cover": pick_cover_url(a) or "",
                        "digg": digg,
                        "create_time": fmt_ts(ct),
                        "age_hours": round(age_h, 1),
                        "duration": f"{dur // 60000}:{dur % 60000 // 1000:02d}" if dur else "",
                        "downloaded": rid[-6:] in seen_authors_tags[nick],
                        "is_monitored": any(b["sec_user_id"] == author.get("sec_uid") for b in config["bloggers"]),
                    }
        except Exception as e:
            log_err(f"发现：抓取相关推荐失败 {e}")

    items = sorted(found.values(), key=lambda x: x["digg"], reverse=True)
    return {"scanned": scanned, "hours": body.hours, "min_like": body.min_like, "count": len(items), "items": items}


class LibrarySearchBody(BaseModel):
    hours: int = 24
    min_like: int = 20000
    scan: int = 20   # 免登录每个博主最多约20条可见，翻页无效，不做无谓翻页


@app.post("/api/library_search")
async def api_library_search(body: LibrarySearchBody):
    """库里博主：时间窗内的作品，按点赞从高到低排，取前 N（点赞是排序键，不是门槛）"""
    now = time.time()
    items = []
    tag_cache = {}
    for b in list(config["bloggers"]):
        sec_uid = b["sec_user_id"]
        nickname = b.get("nickname") or sec_uid[:16]
        # 登录后可翻页拿更多作品，才排得出真正的前N；未登录抖音最多给约20条
        scan = max(body.scan, 150) if is_logged_in() else 20
        try:
            awemes = await fetch_posts_raw(sec_uid, max_count=scan)
        except Exception as e:
            log_err(f"库内查找 {nickname} 失败: {e}")
            continue
        cache = POSTS_CACHE.setdefault(sec_uid, {})
        if nickname not in tag_cache:
            tag_cache[nickname] = _downloaded_tags(nickname)
        for a in awemes:
            ct = a.get("create_time") or 0
            age_h = (now - ct) / 3600 if ct else 1e9
            digg = (a.get("statistics") or {}).get("digg_count") or 0
            if body.hours and age_h > body.hours:   # 只卡时间；点赞不过滤，只用于排序
                continue
            aid = str(a.get("aweme_id"))
            cache[aid] = a
            dur = (a.get("video") or {}).get("duration") or 0
            items.append({
                "aweme_id": aid,
                "desc": (a.get("desc") or "").strip() or "(无标题)",
                "author": nickname,
                "sec_user_id": sec_uid,
                "type": "图集" if a.get("images") else "视频",
                "cover": pick_cover_url(a) or "",
                "digg": digg,
                "create_time": fmt_ts(ct),
                "age_hours": round(age_h, 1),
                "duration": f"{dur // 60000}:{dur % 60000 // 1000:02d}" if dur else "",
                "downloaded": aid[-6:] in tag_cache[nickname],
                "is_monitored": True,
            })
        await asyncio.sleep(1)
    items.sort(key=lambda x: x["digg"], reverse=True)
    total = len(items)
    items = items[:50]   # 按点赞取前 50（够看前30，还有余）
    return {"bloggers": len(config["bloggers"]), "hours": body.hours,
            "total": total, "count": len(items), "items": items}


@app.get("/api/downloads")
def api_downloads():
    groups = []
    dl = get_dl()
    if dl.exists():
        for d in sorted(dl.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
            if not d.is_dir():
                continue
            files = []
            for f in sorted(d.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
                if f.is_file() and not f.name.endswith(".part"):
                    files.append({
                        "name": f.name,
                        "url": f"/files/{d.name}/{f.name}",
                        "size": round(f.stat().st_size / 1024 / 1024, 2),
                        "is_video": f.suffix.lower() == ".mp4",
                    })
            if files:
                groups.append({"blogger": d.name, "count": len(files), "files": files})
    return {"groups": groups}


@app.post("/api/reveal")
def api_reveal():
    os.startfile(str(get_dl()))
    return {"ok": True}


@app.post("/api/reveal_blogger/{name}")
def api_reveal_blogger(name: str):
    dl = get_dl()
    target = dl / sanitize(name, 30)
    os.startfile(str(target if target.exists() else dl))
    return {"ok": True}


class SetDirBody(BaseModel):
    path: str


@app.post("/api/set_dir")
def api_set_dir(body: SetDirBody):
    p = body.path.strip().strip('"')
    if not p:
        return JSONResponse({"error": "路径不能为空"}, status_code=400)
    try:
        Path(p).mkdir(parents=True, exist_ok=True)
    except Exception as e:
        return JSONResponse({"error": f"该路径不可用: {e}"}, status_code=400)
    config["download_dir"] = str(Path(p))
    _save(CONFIG_FILE, config)
    return {"ok": True, "download_dir": config["download_dir"]}


@app.post("/api/choose_dir")
def api_choose_dir():
    """弹出系统原生文件夹选择框（仅桌面模式可用）"""
    try:
        import webview
        win = webview.active_window() or (webview.windows[0] if webview.windows else None)
        if not win:
            raise RuntimeError("no window")
        res = win.create_file_dialog(webview.FOLDER_DIALOG)
        if not res:
            return {"ok": False, "cancelled": True}
        path = res[0] if isinstance(res, (list, tuple)) else res
        Path(path).mkdir(parents=True, exist_ok=True)
        config["download_dir"] = str(Path(path))
        _save(CONFIG_FILE, config)
        return {"ok": True, "download_dir": config["download_dir"]}
    except Exception as e:
        return JSONResponse(
            {"error": f"当前环境无法弹出文件夹选择框，请手动粘贴路径。({e})"}, status_code=400)


class CookieBody(BaseModel):
    cookie: str


@app.post("/api/set_cookie")
def api_set_cookie(body: CookieBody):
    c = (body.cookie or "").strip()
    if "sessionid" not in c:
        return JSONResponse({"error": "这段 cookie 里没有 sessionid，可能没登录成功"}, status_code=400)
    set_login_cookie(c)
    return {"ok": True, "logged_in": True}


@app.post("/api/logout")
def api_logout():
    clear_login_cookie()
    return {"ok": True, "logged_in": False}


@app.post("/api/login_qr")
def api_login_qr():
    """触发桌面端弹出抖音登录窗口扫码（由 desktop.py 注入的回调实现）"""
    cb = globals().get("_login_callback")
    if not cb:
        return JSONResponse(
            {"error": "扫码登录仅桌面版可用。请改用「粘贴 Cookie 登录」，或在设置里从浏览器导入。"},
            status_code=400)
    try:
        ok = cb()  # 阻塞直到扫码完成或超时
        return {"ok": bool(ok), "logged_in": is_logged_in()}
    except Exception as e:
        return JSONResponse({"error": f"登录失败: {e}"}, status_code=400)


@app.post("/api/login_browser")
def api_login_browser():
    """从系统浏览器(Chrome/Edge/Firefox)读取已登录的抖音 cookie"""
    try:
        import browser_cookie3 as bc
    except Exception:
        return JSONResponse({"error": "缺少 browser_cookie3"}, status_code=400)
    for name in ("edge", "chrome", "firefox"):
        try:
            cj = getattr(bc, name)(domain_name="douyin.com")
            jar = {c.name: c.value for c in cj}
            if "sessionid" in jar:
                set_login_cookie("; ".join(f"{k}={v}" for k, v in jar.items()))
                return {"ok": True, "logged_in": True, "source": name}
        except Exception:
            continue
    return JSONResponse(
        {"error": "没在浏览器里找到已登录的抖音 cookie。请先在 Chrome/Edge 里登录 douyin.com 再点此按钮。"},
        status_code=400)


@app.get("/api/export_bloggers")
def api_export_bloggers():
    """导出监控博主列表（不含任何登录/密钥信息）"""
    keys = ("sec_user_id", "nickname", "avatar", "follower_count", "aweme_count", "signature")
    return {
        "app": "爆款监控",
        "type": "bloggers",
        "version": VERSION,
        "exported_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "bloggers": [{k: b.get(k) for k in keys} for b in config["bloggers"]],
    }


class ImportBody(BaseModel):
    bloggers: list = []


@app.post("/api/import_bloggers")
def api_import_bloggers(body: ImportBody):
    keys = ("sec_user_id", "nickname", "avatar", "follower_count", "aweme_count", "signature")
    existing = {b["sec_user_id"] for b in config["bloggers"]}
    added = 0
    for b in body.bloggers or []:
        sid = (b or {}).get("sec_user_id")
        if not sid or not str(sid).startswith("MS4wLjAB") or sid in existing:
            continue
        rec = {k: b.get(k) for k in keys}
        rec["added_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")
        config["bloggers"].append(rec)
        existing.add(sid)
        added += 1
    if added:
        _save(CONFIG_FILE, config)
    return {"ok": True, "added": added, "total": len(config["bloggers"])}


@app.get("/api/check_update")
async def api_check_update():
    """检查软件是否有新版本（对比 GitHub 上的 version.json）"""
    if not UPDATE_RAW_URL:
        return {"current": VERSION, "latest": VERSION, "has_update": False, "note": "未配置更新源"}
    try:
        async with httpx.AsyncClient(timeout=12, follow_redirects=True) as c:
            r = await c.get(UPDATE_RAW_URL)
            info = r.json()
        latest = str(info.get("version", VERSION))
        has = _ver_tuple(latest) > _ver_tuple(VERSION)
        return {"current": VERSION, "latest": latest, "has_update": has,
                "notes": info.get("notes", ""), "url": info.get("url") or RELEASE_PAGE}
    except Exception as e:
        return {"current": VERSION, "latest": VERSION, "has_update": False, "error": str(e)[:80]}


def _ver_tuple(v: str):
    try:
        return tuple(int(x) for x in str(v).strip().lstrip("vV").split("."))
    except Exception:
        return (0,)


@app.post("/api/test_notify")
def api_test_notify():
    notify("🔔 测试通知", "如果你看到这条弹窗并听到声音，说明通知功能正常。")
    return {"ok": True}


if __name__ == "__main__":
    print(f"抖音博主更新监控面板: http://127.0.0.1:{PORT}", flush=True)
    threading.Timer(1.5, lambda: os.startfile(f"http://127.0.0.1:{PORT}")).start()
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")
