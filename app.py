# -*- coding: utf-8 -*-
"""抖音博主更新监控 — 定时检查博主新作品，弹窗提醒 + 自动下载无水印视频 + 本地面板"""
import asyncio
import json
import base64
import os
import re
import subprocess
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

import platforms
from platforms import get_adapter, PLATFORM_LIST


async def resolve_aweme_id(text: str, platform: str = "douyin"):
    """从链接/口令/纯ID里解析出作品ID（走对应平台的适配器）"""
    return await get_adapter(platform).resolve_aweme_id(text)

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
VERSION = "1.0.10"
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

# 点赞数据快照（起飞预警/日报用）：aweme_id -> {desc,nickname,platform,create_time,alerted,pts:[[ts,digg]]}
SNAPS_FILE = DATA / "snaps.json"
SNAPS = _load(SNAPS_FILE, {})


def _migrate_config():
    """老配置迁移到多平台结构：cookie→cookies.douyin；博主补 platform=douyin"""
    changed = False
    if "cookies" not in config:
        config["cookies"] = {}
        changed = True
    if config.get("cookie"):     # 老的单 cookie 归到抖音
        config["cookies"].setdefault("douyin", config.pop("cookie"))
        changed = True
    for b in config.get("bloggers", []):
        if "platform" not in b:
            b["platform"] = "douyin"
            changed = True
    for k, v in (("mix_follows", []), ("takeoff_vel", 3000), ("digest_hour", 9)):
        if k not in config:
            config[k] = v
            changed = True
    if changed:
        _save(CONFIG_FILE, config)


_migrate_config()


def get_dl() -> Path:
    """当前下载根目录（可在设置里自定义，默认 ./downloads）"""
    d = Path(config.get("download_dir") or DL)
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception:
        d = DL
        d.mkdir(exist_ok=True)
    return d


def platform_dir(platform: str) -> Path:
    """某平台的下载根：downloads/平台名/"""
    names = {"douyin": "抖音", "tiktok": "TikTok"}
    d = get_dl() / names.get(platform, platform)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _cookie(platform: str) -> str:
    return (config.get("cookies") or {}).get(platform, "") or ""


def is_logged_in(platform: str = "douyin") -> bool:
    return "sessionid" in _cookie(platform)


def make_kwargs(platform: str = "douyin"):
    return get_adapter(platform).make_kwargs(_cookie(platform))


def set_login_cookie(cookie_str: str, platform: str = "douyin"):
    config.setdefault("cookies", {})[platform] = (cookie_str or "").strip()
    _save(CONFIG_FILE, config)


def clear_login_cookie(platform: str = "douyin"):
    (config.get("cookies") or {}).pop(platform, None)
    _save(CONFIG_FILE, config)


def sanitize(name: str, maxlen=50) -> str:
    name = re.sub(r'[\\/:*?"<>|\r\n#]+', "_", name or "").strip()
    return (name[:maxlen] or "未命名").rstrip(". ")


def fmt_ts(ts) -> str:
    try:
        return datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return str(ts)


def _to_int(v) -> int:
    """把 '1.2万' / '12000' / 12000 之类统一成整数"""
    if isinstance(v, (int, float)):
        return int(v)
    try:
        s = str(v).strip()
        if s.endswith(("万", "w", "W")):
            return int(float(s[:-1]) * 10000)
        return int(float(re.sub(r"[^\d.]", "", s) or 0))
    except Exception:
        return 0


def rank_metrics(it: dict, age_hours: float, follower: int = 0) -> dict:
    """三个榜的指标：
    ⚡涨赞速度 vel=点赞÷发布小时数(抓正在起飞的);
    🐴黑马比 dark=点赞÷粉丝数(越级=内容强可复制);
    💬互动质量 qual=(评论+转发+收藏)÷点赞×100%(挡搬运/擦边)。"""
    digg = it.get("digg") or 0
    ah = max(float(age_hours or 0), 0.1)
    vel = round(digg / ah, 1)
    dark = round(digg / follower, 2) if follower and follower > 0 else None
    inter = (it.get("comment") or 0) + (it.get("share") or 0) + (it.get("collect") or 0)
    qual = round(inter / digg * 100, 1) if digg > 0 else 0.0
    return {"vel": vel, "follower": follower or 0, "dark": dark, "qual": qual}


async def fetch_posts_raw(sec_uid: str, max_count: int = 20, platform: str = "douyin") -> list:
    """抓取博主作品（可多页累积），返回 aweme 原始 dict 列表"""
    return await get_adapter(platform).posts(_cookie(platform), sec_uid, max_count)


async def fetch_posts_windowed(sec_uid: str, hours: int, max_pages: int = 8,
                               platform: str = "douyin") -> list:
    """按时间窗抓：一页里最新的都超出时间范围就停（作品按时间倒序，够准又不浪费翻页）。"""
    ad = get_adapter(platform)
    cutoff = time.time() - hours * 3600 if hours else 0
    out = []
    pages = 0
    async for page in ad.posts_pages(_cookie(platform), sec_uid, max_pages=max_pages, page_timeout=5):
        page = page or []
        out.extend(page)
        pages += 1
        if cutoff and page:
            newest = max((ad.normalize(a).get("create_time") or 0) for a in page)
            if newest < cutoff:   # 整页都比截止时间早 → 后面更早，停
                break
        if pages >= max_pages:
            break
    return out


async def fetch_profile(sec_uid: str, platform: str = "douyin") -> dict:
    return await get_adapter(platform).profile(_cookie(platform), sec_uid)


def pick_video_url(aweme: dict, platform: str = "douyin"):
    return get_adapter(platform).normalize(aweme).get("video_url")


def pick_cover_url(aweme: dict, platform: str = "douyin"):
    return get_adapter(platform).normalize(aweme).get("cover")


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
    platform = blogger.get("platform", "douyin")
    it = get_adapter(platform).normalize(aweme)
    aid = it["aweme_id"]
    nickname = blogger.get("nickname", "")
    dl = get_dl()
    folder = platform_dir(platform) / sanitize(nickname, 30)
    date_tag = datetime.fromtimestamp(int(it.get("create_time") or time.time())).strftime("%Y%m%d")
    stem = f"{date_tag}_{sanitize(it['desc'], 40)}_{aid[-6:]}"
    dur_ms = it["duration_ms"]

    POSTS_CACHE.setdefault(blogger["sec_user_id"], {})[aid] = aweme

    rec = {
        "aweme_id": aid, "sec_user_id": blogger["sec_user_id"], "platform": platform,
        "nickname": nickname, "desc": it["desc"] or "(无标题)",
        "type": "图集" if it["is_images"] else "视频",
        "create_time": fmt_ts(it.get("create_time")),
        "found_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "duration": f"{dur_ms // 60000}:{dur_ms % 60000 // 1000:02d}" if dur_ms else "",
        "digg": it["digg"], "comment": it["comment"], "collect": it["collect"], "share": it["share"],
        "url": it["web_url"], "cover": "",
    }

    if it["cover"]:
        cover_path = folder / f"{stem}.jpg"
        if await download_file(it["cover"], cover_path):
            rec["cover"] = f"/files/{cover_path.relative_to(dl).as_posix()}"

    state["updates"].insert(0, rec)
    state["updates"] = state["updates"][:500]
    _save(STATE_FILE, state)

    notify(f"📢 {nickname} 更新了新{rec['type']}",
           f"{rec['desc']}\n发布于 {rec['create_time']}",
           icon=cover_path if rec["cover"] else None)


async def download_aweme_media(nickname: str, aweme: dict, platform: str = "douyin") -> tuple:
    """手动下载单个作品（视频或图集）到 downloads/平台/昵称/。返回 (ok, 文件url)"""
    it = get_adapter(platform).normalize(aweme)
    aid = it["aweme_id"]
    dl = get_dl()
    folder = platform_dir(platform) / sanitize(nickname, 30)
    date_tag = datetime.fromtimestamp(int(it.get("create_time") or time.time())).strftime("%Y%m%d")
    stem = f"{date_tag}_{sanitize(it['desc'], 40)}_{aid[-6:]}"

    if it["cover"]:
        await download_file(it["cover"], folder / f"{stem}.jpg")

    if it["is_images"]:
        n = 0
        for i, u in enumerate(it["image_urls"], 1):
            if await download_file(u, folder / f"{stem}_{i:02d}.jpeg"):
                n += 1
        return (n > 0, f"/files/{folder.relative_to(dl).as_posix()}" if n else "")
    else:
        if it["video_url"]:
            vp = folder / f"{stem}.mp4"
            if await download_file(it["video_url"], vp):
                return True, f"/files/{vp.relative_to(dl).as_posix()}"
    return False, ""


def _downloaded_tags(nickname: str, platform: str = "douyin") -> set:
    """已下载作品的 id 尾6位集合。只认正片（.mp4 视频 / _NN.jpeg 图集），封面 .jpg 不算下载。"""
    folder = platform_dir(platform) / sanitize(nickname, 30)
    tags = set()
    if folder.exists():
        for f in folder.iterdir():
            m = re.search(r"_(\d{6})\.mp4$", f.name) or re.search(r"_(\d{6})_\d+\.jpe?g$", f.name)
            if m:
                tags.add(m.group(1))
    return tags


def _blogger(sec_uid: str) -> dict:
    for b in config["bloggers"]:
        if b["sec_user_id"] == sec_uid:
            return b
    return {}


def _blogger_nickname(sec_uid: str) -> str:
    b = _blogger(sec_uid)
    return b.get("nickname") or (sec_uid[:16] if sec_uid else "")


def _platform_of(sec_uid: str) -> str:
    return _blogger(sec_uid).get("platform", "douyin")


async def resolve_aweme(aid: str, platform: str = "douyin") -> dict:
    """按作品ID找到完整 aweme：先查各缓存，缺失/无地址则重新拉详情"""
    aid = str(aid)
    for cache in POSTS_CACHE.values():
        a = cache.get(aid)
        if a and _has_media(a, platform):
            return a
    try:
        full = await get_adapter(platform).one_video(_cookie(platform), aid)
        if full:
            POSTS_CACHE.setdefault("_resolved", {})[aid] = full
            return full
    except Exception as e:
        log_err(f"取作品 {aid} 详情失败: {e}")
    return None


# ---------------- 数据快照 + 起飞预警 + 每日日报 ----------------
SNAP_MAX_AGE_DAYS = 7      # 只追踪发布 7 天内的新作品（老作品数据已稳定，不是起飞候选）


def _maybe_takeoff(aid: str, s: dict):
    """看最近 2 小时窗口的涨赞速度，超阈值就预警（每条作品只报一次）"""
    vel_min = int(config.get("takeoff_vel") or 0)
    if vel_min <= 0 or s.get("alerted"):
        return
    pts = s["pts"]
    now_ts, now_digg = pts[-1]
    base = None
    for ts, dg in pts:                       # 找 2 小时窗口内最早的点
        if now_ts - ts <= 2 * 3600:
            base = (ts, dg)
            break
    if not base or now_ts - base[0] < 1800:  # 窗口至少跨半小时才能算速度
        return
    dt_h = (now_ts - base[0]) / 3600
    gained = now_digg - base[1]
    vel = gained / dt_h
    if vel >= vel_min and gained >= vel_min / 2:
        s["alerted"] = True
        ev = {"aweme_id": aid, "desc": s["desc"], "nickname": s["nickname"], "platform": s["platform"],
              "gained": int(gained), "hours": round(dt_h, 1), "vel": int(vel), "digg": int(now_digg),
              "time": datetime.now().strftime("%m-%d %H:%M"), "pts": pts[-48:]}
        state.setdefault("takeoffs", []).insert(0, ev)
        state["takeoffs"] = state["takeoffs"][:50]
        _save(STATE_FILE, state)
        notify(f"🚀 起飞预警：@{s['nickname']}",
               f"{s['desc']}\n{ev['hours']}小时涨了 {gained:,} 赞（{int(vel):,} 赞/时），当前 {now_digg:,} 赞")


def _record_snapshots(blogger: dict, its: list):
    """监控循环顺手记每条新作品的 (时间, 点赞) 序列，供起飞预警/日报用"""
    now = time.time()
    changed = False
    for it in its:
        ct = it.get("create_time") or 0
        if not ct or now - ct > SNAP_MAX_AGE_DAYS * 86400:
            continue
        aid = it["aweme_id"]
        s = SNAPS.get(aid)
        if s is None:
            s = SNAPS[aid] = {"desc": (it["desc"] or "")[:60] or "(无标题)",
                              "nickname": blogger.get("nickname", ""), "platform": it["platform"],
                              "create_time": ct, "alerted": False, "pts": []}
        pts = s["pts"]
        if pts and now - pts[-1][0] < 600:   # 10 分钟内不重复记点
            continue
        pts.append([int(now), int(it.get("digg") or 0)])
        if len(pts) > 500:
            del pts[:len(pts) - 500]
        changed = True
        _maybe_takeoff(aid, s)
    stale = [k for k, v in SNAPS.items() if now - (v.get("create_time") or 0) > SNAP_MAX_AGE_DAYS * 86400]
    for k in stale:
        SNAPS.pop(k, None)
        changed = True
    if changed:
        _save(SNAPS_FILE, SNAPS)


def _maybe_daily_digest():
    """每天到点弹一条汇总：24h 更新数、破10万赞数、涨赞最快"""
    hour = int(config.get("digest_hour", 9))
    if hour < 0:
        return
    today = datetime.now().strftime("%Y-%m-%d")
    if state.get("digest_date") == today or datetime.now().hour < hour:
        return
    cutoff = time.time() - 86400
    ups = []
    for u in state.get("updates", []):
        try:
            fa = time.mktime(time.strptime(u.get("found_at", ""), "%Y-%m-%d %H:%M:%S"))
        except Exception:
            continue
        if fa >= cutoff:
            ups.append(u)
    fastest = None
    for aid, s in SNAPS.items():
        pts = [p for p in s["pts"] if p[0] >= cutoff]
        if len(pts) < 2:
            continue
        dt_h = (pts[-1][0] - pts[0][0]) / 3600
        if dt_h < 0.5:
            continue
        vel = (pts[-1][1] - pts[0][1]) / dt_h
        if fastest is None or vel > fastest[0]:
            fastest = (vel, s, pts[-1][1])
    big = [u for u in ups if (u.get("digg") or 0) >= 100000]
    if not ups and not fastest:
        text = "过去24小时：库里博主没有新作品，也没有明显起飞的视频"
    else:
        lines = [f"过去24小时：库里博主更新 {len(ups)} 条作品"]
        if big:
            lines.append(f"其中 {len(big)} 条已破 10 万赞")
        if fastest and fastest[0] >= 100:
            v, s, dg = fastest
            lines.append(f"涨赞最快：@{s['nickname']}《{s['desc'][:20]}》 {int(v):,} 赞/时（当前 {dg:,} 赞）")
        text = "\n".join(lines)
    state["digest_date"] = today
    state["last_digest"] = {"date": today, "time": datetime.now().strftime("%H:%M"), "text": text}
    _save(STATE_FILE, state)
    notify("📰 每日爆款日报", text)


async def check_blogger(blogger: dict, baseline: bool = False) -> int:
    platform = blogger.get("platform", "douyin")
    ad = get_adapter(platform)
    sec_uid = blogger["sec_user_id"]
    awemes = await fetch_posts_raw(sec_uid, platform=platform)
    if not awemes:
        log_err(f"{blogger.get('nickname', sec_uid)}: 未获取到作品（可能是风控/需VPN，稍后自动重试）")
        return 0
    # 归一化一遍，拿到 (aid, create_time, raw)
    items = [(ad.normalize(a), a) for a in awemes]
    try:
        _record_snapshots(blogger, [it for it, _a in items])   # 顺手记点赞快照（起飞预警）
    except Exception as e:
        log_err(f"记录数据快照失败: {e}")
    seen = set(state["seen"].get(sec_uid, []))
    new_items = [(it, a) for (it, a) in items if it["aweme_id"] not in seen]
    if baseline or not seen:
        state["seen"][sec_uid] = list(seen | {it["aweme_id"] for (it, a) in items})
        _save(STATE_FILE, state)
        return 0
    for it, a in sorted(new_items, key=lambda x: x[0].get("create_time", 0)):
        state["seen"][sec_uid].append(it["aweme_id"])
        await process_new_aweme(blogger, a)
    if new_items:
        _save(STATE_FILE, state)
    return len(new_items)


_check_now = asyncio.Event()


async def monitor_loop():
    await asyncio.sleep(3)
    while True:
        for b in list(config["bloggers"]):
            if not b.get("notify", True):   # 未开启"更新动态提醒"的博主不检查
                continue
            try:
                n = await check_blogger(b)
                if n:
                    print(f"[NEW] {b.get('nickname')}: {n} 条新作品", flush=True)
            except Exception as e:
                log_err(f"检查 {b.get('nickname')} 出错: {e}")
            await asyncio.sleep(2)
        try:
            await _check_mix_follows_due()   # 合集追更（每个合集最多 6 小时查一次）
        except Exception as e:
            log_err(f"合集追更检查出错: {e}")
        try:
            _maybe_daily_digest()            # 每日日报（到点弹一次）
        except Exception as e:
            log_err(f"日报生成出错: {e}")
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
async def api_stream(aid: str, request: Request, platform: str = "douyin"):
    """在线播放代理：带 Referer 抓视频流，支持 Range 拖动进度条"""
    aweme = await resolve_aweme(aid, platform)
    if not aweme:
        return JSONResponse({"error": "取不到该视频"}, status_code=404)
    url = pick_video_url(aweme, platform)
    if not url:
        return JSONResponse({"error": "该作品没有视频（可能是图集）"}, status_code=404)

    up_headers = {"User-Agent": UA, "Referer": get_adapter(platform).home_url}
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
async def api_aweme_images(aid: str, platform: str = "douyin"):
    """图集的图片地址（前端用 no-referrer 直接展示）"""
    aweme = await resolve_aweme(aid, platform)
    imgs = get_adapter(platform).normalize(aweme).get("image_urls", []) if aweme else []
    return {"images": imgs}


@app.on_event("startup")
async def _startup():
    asyncio.create_task(monitor_loop())
    for _ in range(2):                       # 两个下载工人：并发温和，防限流
        asyncio.create_task(_dl_worker())


@app.get("/", response_class=HTMLResponse)
def index():
    return (BASE / "static" / "index.html").read_text(encoding="utf-8")


@app.get("/favicon.ico")
def favicon():
    return FileResponse(str(BASE / "static" / "favicon.ico"))


@app.get("/api/status")
def api_status():
    tags = {}
    updates = []
    for u in state["updates"][:200]:
        nk = u.get("nickname", "")
        pf = u.get("platform", "douyin")
        key = (pf, nk)
        if key not in tags:
            tags[key] = _downloaded_tags(nk, pf)
        u = dict(u)
        u.setdefault("platform", pf)
        u["downloaded"] = str(u.get("aweme_id", ""))[-6:] in tags[key]
        updates.append(u)
    return {
        "interval_minutes": config["interval_minutes"],
        "bloggers": config["bloggers"],
        "updates": updates,
        "last_check": state.get("last_check"),
        "errors": state.get("errors", [])[:5],
        "download_dir": config.get("download_dir") or "",
        "dir_chosen": bool(config.get("download_dir")),
        "logged_in": is_logged_in("douyin"),
        "logged_in_map": {a["key"]: is_logged_in(a["key"]) for a in PLATFORM_LIST},
        "platforms": PLATFORM_LIST,
        "version": VERSION,
        "mix_progress": list(MIX_PROGRESS.values()),
        "tasks": {k: sum(1 for t in DL_TASKS.values() if t["status"] == k)
                  for k in ("queued", "running", "done", "failed")},
        "takeoffs": state.get("takeoffs", [])[:20],
        "last_digest": state.get("last_digest"),
        "takeoff_vel": config.get("takeoff_vel", 3000),
        "digest_hour": config.get("digest_hour", 9),
    }


class AddBody(BaseModel):
    url: str
    platform: str = "douyin"


@app.post("/api/bloggers")
async def api_add_blogger(body: AddBody):
    platform = body.platform or "douyin"
    ad = get_adapter(platform)
    url = body.url.strip()
    sec_uid = None
    if re.fullmatch(r"MS4wLjAB[\w\-=]+", url):    # 抖音/TikTok 的 secUid 都是 MS4 开头
        sec_uid = url
    else:
        m = re.search(r"https?://\S+", url)
        if not m:
            return JSONResponse({"error": "没找到链接。请把「分享 → 复制链接」的整段文字粘进来"}, status_code=400)
        link = m.group(0).rstrip("，。、）)]】")
        # ① 先按博主主页解析
        try:
            sec_uid = await ad.resolve_user_id(link)
        except Exception:
            sec_uid = None
        # ② 主页解析不了 → 可能是单条视频链接，反查作者
        if not sec_uid:
            try:
                aid = await ad.resolve_aweme_id(link)
                if aid:
                    full = await ad.one_video(_cookie(platform), aid)
                    if full:
                        sec_uid = ad.normalize(full).get("author_id")
            except Exception:
                sec_uid = None
        if not sec_uid:
            return JSONResponse(
                {"error": "无法识别博主。请粘贴「分享 → 复制链接」的主页或视频链接整段文字"},
                status_code=400)
    if any(b["sec_user_id"] == sec_uid and b.get("platform", "douyin") == platform
           for b in config["bloggers"]):
        return JSONResponse({"error": "该博主已在监控列表中"}, status_code=400)
    try:
        prof = await fetch_profile(sec_uid, platform)
    except Exception as e:
        prof = {"nickname": sec_uid[:16], "avatar": "", "follower_count": "", "aweme_count": "", "signature": ""}
        log_err(f"获取博主资料失败(不影响监控): {e}")
    blogger = {"sec_user_id": sec_uid, "platform": platform,
               "added_at": datetime.now().strftime("%Y-%m-%d %H:%M"), "notify": True, **prof}
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


class NotifyBody(BaseModel):
    sec_user_id: str
    notify: bool


@app.post("/api/blogger_notify")
async def api_blogger_notify(body: NotifyBody):
    """开/关某博主的更新动态提醒。首次开启时先建基线（避免把历史作品当新的刷屏）"""
    hit = None
    for b in config["bloggers"]:
        if b["sec_user_id"] == body.sec_user_id:
            b["notify"] = body.notify
            hit = b
            break
    if not hit:
        return JSONResponse({"error": "博主不在库里"}, status_code=404)
    # 开启且还没建过基线 → 建基线
    if body.notify and not state["seen"].get(body.sec_user_id):
        try:
            await check_blogger(hit, baseline=True)
        except Exception as e:
            log_err(f"开启提醒建基线失败: {e}")
    _save(CONFIG_FILE, config)
    return {"ok": True, "notify": body.notify}


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
async def api_blogger_posts(sec_uid: str, cursor: int = 0, platform: str = "douyin"):
    """抓取博主的作品列表（供浏览+手动挑选下载），并缓存完整数据"""
    platform = _platform_of(sec_uid) or platform
    ad = get_adapter(platform)
    try:
        awemes = await ad.posts(_cookie(platform), sec_uid, max_count=20)
    except Exception as e:
        return JSONResponse({"error": f"获取作品失败: {e}"}, status_code=400)

    cache = POSTS_CACHE.setdefault(sec_uid, {})
    nickname = _blogger_nickname(sec_uid)
    if not nickname and awemes:
        nickname = ad.normalize(awemes[0]).get("author_name") or sec_uid[:16]
    done = _downloaded_tags(nickname, platform)

    items = []
    for a in awemes:
        it = ad.normalize(a)
        aid = it["aweme_id"]
        cache[aid] = a
        dur = it["duration_ms"]
        items.append({
            "aweme_id": aid,
            "desc": it["desc"] or "(无标题)",
            "type": "图集" if it["is_images"] else "视频",
            "cover": it["cover"] or "",
            "create_time": fmt_ts(it["create_time"]),
            "duration": f"{dur // 60000}:{dur % 60000 // 1000:02d}" if dur else "",
            "digg": it["digg"],
            "downloaded": aid[-6:] in done,
        })
    return {"nickname": nickname, "count": len(items), "posts": items}


class DownloadBody(BaseModel):
    sec_user_id: str
    aweme_ids: list[str]
    platform: str = "douyin"


def _has_media(a: dict, platform: str = "douyin") -> bool:
    """该 aweme 是否带真正可下载的媒体地址（相关推荐的预览版没有 play_addr）"""
    if not a:
        return False
    it = get_adapter(platform).normalize(a)
    if it["is_images"]:
        return bool(it["image_urls"])
    return bool(it["video_url"])


# ---------------- 下载任务中心：所有下载统一进队列，排队/进度/失败重试一目了然 ----------------
DL_TASKS = {}                    # tid -> 任务记录（内存态，重启清空）
DL_QUEUE: asyncio.Queue = asyncio.Queue()
_task_seq = 0


def enqueue_download(platform, nickname, aid, desc="", kind="单条", mix_id=None, mix_name=""):
    global _task_seq
    _task_seq += 1
    tid = f"t{_task_seq}"
    DL_TASKS[tid] = {"id": tid, "platform": platform, "nickname": nickname or "",
                     "aweme_id": str(aid), "desc": (desc or "")[:60], "kind": kind,
                     "mix_id": mix_id, "mix_name": mix_name,
                     "status": "queued", "err": "", "ts": datetime.now().strftime("%H:%M:%S")}
    DL_QUEUE.put_nowait(tid)
    return tid


async def _dl_worker():
    while True:
        tid = await DL_QUEUE.get()
        t = DL_TASKS.get(tid)
        if not t or t["status"] != "queued":
            continue
        t["status"] = "running"
        ok = False
        try:
            platform = t["platform"]
            aweme = await resolve_aweme(t["aweme_id"], platform)
            if aweme:
                nm = t["nickname"] or get_adapter(platform).normalize(aweme).get("author_name") or "未知博主"
                t["nickname"] = nm
                ok, _u = await download_aweme_media(nm, aweme, platform)
                t["status"] = "done" if ok else "failed"
                if not ok:
                    t["err"] = "没拿到下载地址或写文件失败"
            else:
                t["status"] = "failed"
                t["err"] = "拿不到作品详情（可能已删除/风控）"
        except Exception as e:
            t["status"] = "failed"
            t["err"] = str(e)[:100]
        if t.get("mix_id") and t["mix_id"] in MIX_PROGRESS:
            mp = MIX_PROGRESS[t["mix_id"]]
            mp["done"] += 1
            if mp["done"] >= mp["total"]:
                asyncio.create_task(_mix_progress_cleanup(t["mix_id"]))
        await asyncio.sleep(1.2)   # 每个任务之间温和停一下，防限流/封号


async def _mix_progress_cleanup(mid):
    await asyncio.sleep(8)
    MIX_PROGRESS.pop(mid, None)


@app.get("/api/tasks")
def api_tasks():
    counts = {k: 0 for k in ("queued", "running", "done", "failed")}
    for t in DL_TASKS.values():
        counts[t["status"]] = counts.get(t["status"], 0) + 1
    tasks = list(DL_TASKS.values())[-300:]
    tasks.reverse()   # 最新的在前
    return {"counts": counts, "tasks": tasks}


@app.post("/api/tasks/retry_failed")
def api_tasks_retry():
    n = 0
    for t in DL_TASKS.values():
        if t["status"] == "failed":
            t["status"] = "queued"
            t["err"] = ""
            DL_QUEUE.put_nowait(t["id"])
            n += 1
    return {"ok": True, "requeued": n}


@app.post("/api/tasks/clear")
def api_tasks_clear():
    for tid in [k for k, t in DL_TASKS.items() if t["status"] in ("done", "failed")]:
        DL_TASKS.pop(tid, None)
    return {"ok": True}


@app.post("/api/download_selected")
async def api_download_selected(body: DownloadBody):
    """所有手动下载统一进任务队列，接口秒回；进度看任务中心"""
    platform = getattr(body, "platform", None) or "douyin"
    nickname = _blogger_nickname(body.sec_user_id)
    cache = POSTS_CACHE.get(body.sec_user_id, {})
    n = 0
    for aid in body.aweme_ids:
        aid = str(aid)
        a = cache.get(aid)
        desc, nm = "", nickname
        if a:
            it = get_adapter(platform).normalize(a)
            desc = it["desc"]
            nm = nm or it["author_name"]
        enqueue_download(platform, nm, aid, desc)
        n += 1
    return {"ok": True, "queued": n, "success": n, "total": n}


MIX_PROGRESS = {}   # mix_id -> {name, done, total}


def _mix_of(aweme: dict, platform: str = "douyin"):
    return get_adapter(platform).normalize(aweme).get("mix") if aweme else None


@app.get("/api/mix_info/{aid}")
async def api_mix_info(aid: str, platform: str = "douyin"):
    a = await resolve_aweme(aid, platform)
    m = _mix_of(a, platform)
    return {"in_mix": bool(m), **(m or {})}


class MixBody(BaseModel):
    aweme_id: str
    platform: str = "douyin"


@app.post("/api/download_mix")
async def api_download_mix(body: MixBody):
    platform = body.platform or "douyin"
    a = await resolve_aweme(body.aweme_id, platform)
    m = _mix_of(a, platform)
    if not m:
        return JSONResponse({"error": "这条视频不属于任何合集"}, status_code=400)
    it = get_adapter(platform).normalize(a)
    nickname = _blogger_nickname(it["author_id"]) or it["author_name"] or "未知博主"
    if m["mix_id"] in MIX_PROGRESS:
        return {"ok": True, "total": m["total"], "mix_name": m["mix_name"], "already": True}
    # 抓集 + 下载都放后台，接口立即返回（集数用合集元数据里的 total）
    asyncio.create_task(_download_mix_bg(m["mix_id"], m["mix_name"], nickname, m["total"], platform))
    return {"ok": True, "total": m["total"], "mix_name": m["mix_name"]}


async def _download_mix_bg(mix_id, mix_name, nickname, total_hint, platform="douyin"):
    """抓合集全集列表 → 跳过本地已有的集 → 缺的集进下载队列。下过的合集自动进「追更列表」。"""
    ad = get_adapter(platform)
    MIX_PROGRESS[mix_id] = {"name": mix_name, "done": 0, "total": total_hint or 0}
    episodes = []
    try:
        episodes = await ad.mix(_cookie(platform), mix_id, max_count=500)
    except Exception as e:
        log_err(f"抓合集失败: {e}")
    if not episodes:
        MIX_PROGRESS.pop(mix_id, None)
        return
    done_tags = _downloaded_tags(nickname, platform)
    stash = POSTS_CACHE.setdefault("_mix", {})   # 暂存完整数据，worker 下载时免重拉详情
    new_eps = []
    for ep in episodes:
        it = ad.normalize(ep)
        stash[it["aweme_id"]] = ep
        if it["aweme_id"][-6:] in done_tags:
            continue                              # 本地已有的集跳过 → 补齐只下新集
        new_eps.append((it["aweme_id"], it["desc"]))
    _upsert_mix_follow(mix_id, mix_name, nickname, platform, len(episodes),
                       sample_aid=ad.normalize(episodes[0])["aweme_id"])
    if not new_eps:
        MIX_PROGRESS.pop(mix_id, None)
        print(f"[MIX] 《{mix_name}》没有新集要下（本地已齐 {len(episodes)} 集）", flush=True)
        return
    MIX_PROGRESS[mix_id]["total"] = len(new_eps)
    for aid, desc in new_eps:
        enqueue_download(platform, nickname, aid, desc,
                         kind=f"合集《{mix_name}》", mix_id=mix_id, mix_name=mix_name)
    print(f"[MIX] 《{mix_name}》{len(new_eps)} 集已进下载队列（云端共 {len(episodes)} 集）", flush=True)


# ---------------- 合集追更：下过的合集自动盯更新，新集弹窗+一键补齐 ----------------
MIX_FOLLOW_INTERVAL = 6 * 3600   # 每个合集最多 6 小时自动查一次


def _upsert_mix_follow(mix_id, mix_name, nickname, platform, total, sample_aid):
    fl = config.setdefault("mix_follows", [])
    hit = next((f for f in fl if f["mix_id"] == mix_id), None)
    now_s = datetime.now().strftime("%Y-%m-%d %H:%M")
    if hit:   # 刚下载/补齐过 → 本地追平云端
        hit.update(mix_name=mix_name or hit.get("mix_name"), cloud_total=total,
                   known_total=total, last_check=now_s, last_check_ts=time.time(),
                   sample_aid=str(sample_aid) or hit.get("sample_aid"))
    else:
        fl.append({"mix_id": mix_id, "mix_name": mix_name, "nickname": nickname,
                   "platform": platform, "sample_aid": str(sample_aid),
                   "cloud_total": total, "known_total": total,
                   "last_check": now_s, "last_check_ts": time.time(), "added_at": now_s})
    _save(CONFIG_FILE, config)


async def _check_mix_follow(f) -> int:
    """查一个合集的云端集数（1 次请求：拉样本集详情读 mix 元数据）。涨了→弹窗，返回新增集数。"""
    platform = f.get("platform", "douyin")
    ad = get_adapter(platform)
    full = await ad.one_video(_cookie(platform), f["sample_aid"])
    f["last_check"] = datetime.now().strftime("%Y-%m-%d %H:%M")
    f["last_check_ts"] = time.time()
    m = _mix_of(full, platform) if full else None
    if not m or not m.get("total"):
        _save(CONFIG_FILE, config)
        return 0
    old_cloud = f.get("cloud_total") or 0
    f["cloud_total"] = m["total"]
    _save(CONFIG_FILE, config)
    gained = m["total"] - (f.get("known_total") or 0)
    if m["total"] > old_cloud and gained > 0:
        notify(f"📺 合集更新：《{f['mix_name']}》",
               f"更新到 {m['total']} 集（新出 {m['total'] - old_cloud} 集）\n到「下载库 → 合集追更」一键补齐")
    return gained


async def _check_mix_follows_due(force: bool = False):
    for f in list(config.get("mix_follows", [])):
        if not force and time.time() - (f.get("last_check_ts") or 0) < MIX_FOLLOW_INTERVAL:
            continue
        try:
            await _check_mix_follow(f)
        except Exception as e:
            log_err(f"追更检查《{f.get('mix_name')}》失败: {e}")
        await asyncio.sleep(2)


@app.get("/api/mix_follows")
def api_mix_follows(platform: str = ""):
    out = []
    for f in config.get("mix_follows", []):
        if platform and f.get("platform", "douyin") != platform:
            continue
        d = dict(f)
        d["new_count"] = max(0, (f.get("cloud_total") or 0) - (f.get("known_total") or 0))
        out.append(d)
    return {"follows": out}


class MixCheckBody(BaseModel):
    mix_id: str = ""      # 空 = 检查全部
    platform: str = "douyin"


@app.post("/api/mix_follows/check")
async def api_mix_follows_check(body: MixCheckBody):
    targets = [f for f in config.get("mix_follows", []) if not body.mix_id or f["mix_id"] == body.mix_id]
    gained = {}
    for i, f in enumerate(targets):
        try:
            gained[f["mix_name"]] = await _check_mix_follow(f)
        except Exception as e:
            log_err(f"追更检查失败: {e}")
            gained[f["mix_name"]] = -1
        if i < len(targets) - 1:
            await asyncio.sleep(1.5)
    return {"ok": True, "gained": gained}


@app.post("/api/mix_follows/pull")
async def api_mix_follow_pull(body: MixCheckBody):
    f = next((x for x in config.get("mix_follows", []) if x["mix_id"] == body.mix_id), None)
    if not f:
        return JSONResponse({"error": "这个合集不在追更列表里"}, status_code=404)
    if f["mix_id"] in MIX_PROGRESS:
        return {"ok": True, "already": True}
    asyncio.create_task(_download_mix_bg(f["mix_id"], f["mix_name"], f["nickname"],
                                         f.get("cloud_total") or 0, f.get("platform", "douyin")))
    return {"ok": True}


@app.delete("/api/mix_follows/{mix_id}")
def api_mix_follow_del(mix_id: str):
    config["mix_follows"] = [f for f in config.get("mix_follows", []) if f["mix_id"] != mix_id]
    _save(CONFIG_FILE, config)
    return {"ok": True}


class RadarBody(BaseModel):
    takeoff_vel: int = 3000
    digest_hour: int = 9


@app.post("/api/radar_settings")
def api_radar_settings(body: RadarBody):
    config["takeoff_vel"] = max(0, body.takeoff_vel)
    config["digest_hour"] = max(-1, min(23, body.digest_hour))
    _save(CONFIG_FILE, config)
    return {"ok": True}


class DiscoverBody(BaseModel):
    seeds: list[str]          # 种子视频链接/口令/ID
    hours: int = 24           # 只要最近 N 小时内发布的
    min_like: int = 20000     # 点赞门槛（代理播放量）
    pages: int = 2            # 每个种子抓几页相关推荐（每页约20条，页间有30秒防风控间隔）
    platform: str = "douyin"


@app.post("/api/discover")
async def api_discover(body: DiscoverBody):
    platform = body.platform or "douyin"
    if platform != "douyin":
        return JSONResponse(
            {"error": "「发现」基于抖音的相关推荐，暂只支持抖音。TikTok 请用「库内查找」或「博主监控」。"},
            status_code=400)
    ad = get_adapter(platform)
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
        aid = await resolve_aweme_id(seed, platform)
        if not aid:
            log_err(f"发现：种子无法解析 {seed[:40]}")
            continue
        try:
            h = ad.handler(_cookie(platform))
            async for rel in h.fetch_related_videos(aweme_id=aid, page_counts=20, max_counts=pages * 20):
                for a in (rel._to_raw().get("aweme_list") or []):
                    scanned += 1
                    it = ad.normalize(a)
                    rid = it["aweme_id"]
                    if rid in found:
                        continue
                    ct = it["create_time"]
                    age_h = (now - ct) / 3600 if ct else 1e9
                    digg = it["digg"]
                    if (body.hours and age_h > body.hours) or digg < body.min_like:
                        continue
                    nick = it["author_name"] or "未知博主"
                    if nick not in seen_authors_tags:
                        seen_authors_tags[nick] = _downloaded_tags(nick, platform)
                    cache[rid] = a
                    dur = it["duration_ms"]
                    found[rid] = {
                        "aweme_id": rid,
                        "desc": it["desc"] or "(无标题)",
                        "author": nick,
                        "sec_user_id": it["author_id"],
                        "type": "图集" if it["is_images"] else "视频",
                        "cover": it["cover"] or "",
                        "digg": digg,
                        "comment": it["comment"], "share": it["share"], "collect": it["collect"],
                        "create_time": fmt_ts(ct),
                        "age_hours": round(age_h, 1),
                        "duration": f"{dur // 60000}:{dur % 60000 // 1000:02d}" if dur else "",
                        "downloaded": rid[-6:] in seen_authors_tags[nick],
                        "is_monitored": any(b["sec_user_id"] == it["author_id"]
                                            and b.get("platform", "douyin") == platform
                                            for b in config["bloggers"]),
                        **rank_metrics(it, age_h, _to_int(it.get("author_follower"))),
                    }
        except Exception as e:
            log_err(f"发现：抓取相关推荐失败 {e}")

    items = sorted(found.values(), key=lambda x: x["digg"], reverse=True)
    return {"scanned": scanned, "hours": body.hours, "min_like": body.min_like,
            "count": len(items), "items": items}


class LibrarySearchBody(BaseModel):
    hours: int = 24
    min_like: int = 20000
    scan: int = 20   # 免登录每个博主最多约20条可见，翻页无效，不做无谓翻页
    platform: str = "douyin"


@app.post("/api/library_search")
async def api_library_search(body: LibrarySearchBody):
    """库里博主：时间窗内的作品，按点赞从高到低排，取前 N。
    提速：博主并发抓 + 按时间窗智能停页（够准又快）。只查当前平台的博主。"""
    platform = body.platform or "douyin"
    ad = get_adapter(platform)
    now = time.time()
    bloggers = [b for b in config["bloggers"] if b.get("platform", "douyin") == platform]
    hours = body.hours
    sem = asyncio.Semaphore(4)   # 并发上限压低：登录账号并发太多易被判机器人→封号

    async def one(b):
        sec_uid = b["sec_user_id"]
        nickname = b.get("nickname") or sec_uid[:16]
        async with sem:
            try:
                if hours:   # 有时间窗：抓到超出范围就停
                    awemes = await fetch_posts_windowed(sec_uid, hours, max_pages=8, platform=platform)
                else:       # 不限时间：抓最新一页够排序
                    awemes = await fetch_posts_raw(sec_uid, max_count=20, platform=platform)
            except Exception as e:
                log_err(f"库内查找 {nickname} 失败: {e}")
                return []
        cache = POSTS_CACHE.setdefault(sec_uid, {})
        tags = _downloaded_tags(nickname, platform)
        follower = _to_int(b.get("follower_count"))
        out = []
        for a in awemes:
            it = ad.normalize(a)
            ct = it["create_time"]
            age_h = (now - ct) / 3600 if ct else 1e9
            if hours and age_h > hours:
                continue
            aid = it["aweme_id"]
            cache[aid] = a
            dur = it["duration_ms"]
            out.append({
                "aweme_id": aid,
                "desc": it["desc"] or "(无标题)",
                "author": nickname,
                "sec_user_id": sec_uid,
                "type": "图集" if it["is_images"] else "视频",
                "cover": it["cover"] or "",
                "digg": it["digg"],
                "comment": it["comment"], "share": it["share"], "collect": it["collect"],
                "create_time": fmt_ts(ct),
                "age_hours": round(age_h, 1),
                "duration": f"{dur // 60000}:{dur % 60000 // 1000:02d}" if dur else "",
                "downloaded": aid[-6:] in tags,
                "is_monitored": True,
                **rank_metrics(it, age_h, follower),
            })
        return out

    results = await asyncio.gather(*[one(b) for b in bloggers])
    items = [it for sub in results for it in sub]
    items.sort(key=lambda x: x["digg"], reverse=True)
    total = len(items)
    items = items[:30]
    return {"bloggers": len(bloggers), "hours": hours,
            "total": total, "count": len(items), "items": items}


@app.get("/api/downloads")
def api_downloads(platform: str = "douyin"):
    groups = []
    base = get_dl()
    root = platform_dir(platform)
    if root.exists():
        for d in sorted(root.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
            if not d.is_dir():
                continue
            files = []
            for f in sorted(d.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
                if f.is_file() and not f.name.endswith(".part"):
                    files.append({
                        "name": f.name,
                        "url": f"/files/{f.relative_to(base).as_posix()}",
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
def api_reveal_blogger(name: str, platform: str = "douyin"):
    root = platform_dir(platform)
    target = root / sanitize(name, 30)
    os.startfile(str(target if target.exists() else root))
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
    platform: str = "douyin"


@app.post("/api/set_cookie")
def api_set_cookie(body: CookieBody):
    c = (body.cookie or "").strip()
    if "sessionid" not in c:
        return JSONResponse({"error": "这段 cookie 里没有 sessionid，可能没登录成功"}, status_code=400)
    set_login_cookie(c, body.platform or "douyin")
    return {"ok": True, "logged_in": True}


@app.post("/api/logout")
def api_logout(platform: str = "douyin"):
    clear_login_cookie(platform)
    return {"ok": True, "logged_in": False}


@app.post("/api/login_qr")
def api_login_qr(platform: str = "douyin"):
    """触发桌面端弹出登录窗口扫码（由 desktop.py 注入的回调实现）"""
    cb = globals().get("_login_callback")
    if not cb:
        return JSONResponse(
            {"error": "扫码登录仅桌面版可用。请改用「粘贴 Cookie 登录」，或在设置里从浏览器导入。"},
            status_code=400)
    try:
        ok = cb(platform)  # 阻塞直到扫码完成或超时
        return {"ok": bool(ok), "logged_in": is_logged_in(platform)}
    except Exception as e:
        return JSONResponse({"error": f"登录失败: {e}"}, status_code=400)


@app.post("/api/login_browser")
def api_login_browser(platform: str = "douyin"):
    """从系统浏览器(Chrome/Edge/Firefox)读取已登录的 cookie"""
    try:
        import browser_cookie3 as bc
    except Exception:
        return JSONResponse({"error": "缺少 browser_cookie3"}, status_code=400)
    domain = {"douyin": "douyin.com", "tiktok": "tiktok.com"}.get(platform, "douyin.com")
    for name in ("edge", "chrome", "firefox"):
        try:
            cj = getattr(bc, name)(domain_name=domain)
            jar = {c.name: c.value for c in cj}
            if "sessionid" in jar:
                set_login_cookie("; ".join(f"{k}={v}" for k, v in jar.items()), platform)
                return {"ok": True, "logged_in": True, "source": name}
        except Exception:
            continue
    return JSONResponse(
        {"error": f"没在浏览器里找到已登录的 {domain} cookie。请先在 Chrome/Edge 里登录 {domain} 再点此按钮。"},
        status_code=400)


@app.get("/api/export_bloggers")
def api_export_bloggers():
    """导出监控博主列表（不含任何登录/密钥信息）"""
    keys = ("sec_user_id", "platform", "nickname", "avatar", "follower_count", "aweme_count", "signature")
    return {
        "app": "爆款监控",
        "type": "bloggers",
        "version": VERSION,
        "exported_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "bloggers": [{k: b.get(k) for k in keys} for b in config["bloggers"]],
    }


@app.post("/api/export_bloggers_file")
def api_export_bloggers_file():
    """弹原生保存对话框，让用户选位置保存导出文件（仅桌面版）"""
    try:
        import webview
        win = webview.active_window() or (webview.windows[0] if webview.windows else None)
        if not win:
            raise RuntimeError("no window")
        res = win.create_file_dialog(
            webview.SAVE_DIALOG,
            save_filename=f"监控博主_{datetime.now().strftime('%Y%m%d')}.json",
            file_types=("JSON 文件 (*.json)", "所有文件 (*.*)"),
        )
        if not res:
            return {"ok": False, "cancelled": True}
        path = res if isinstance(res, str) else res[0]
        if not path.lower().endswith(".json"):
            path += ".json"
        data = api_export_bloggers()
        Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        return {"ok": True, "path": path, "count": len(data["bloggers"])}
    except Exception as e:
        return JSONResponse({"error": f"保存对话框不可用（可能非桌面模式）：{e}"}, status_code=400)


class ImportBody(BaseModel):
    bloggers: list = []
    mode: str = "append"   # append=追加  replace=替换现有


@app.post("/api/import_bloggers")
def api_import_bloggers(body: ImportBody):
    keys = ("sec_user_id", "platform", "nickname", "avatar", "follower_count", "aweme_count", "signature")
    if body.mode == "replace":
        config["bloggers"] = []
    existing = {(b["sec_user_id"], b.get("platform", "douyin")) for b in config["bloggers"]}
    added = 0
    for b in body.bloggers or []:
        sid = (b or {}).get("sec_user_id")
        pf = (b or {}).get("platform") or "douyin"
        if not sid or not str(sid).startswith("MS4wLjAB") or (sid, pf) in existing:
            continue
        rec = {k: b.get(k) for k in keys}
        rec["platform"] = pf
        rec["notify"] = True
        rec["added_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")
        config["bloggers"].append(rec)
        existing.add((sid, pf))
        added += 1
    if added or body.mode == "replace":
        _save(CONFIG_FILE, config)
    return {"ok": True, "added": added, "total": len(config["bloggers"]), "mode": body.mode}


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
                "notes": info.get("notes", ""), "url": info.get("url") or RELEASE_PAGE,
                "exe_url": info.get("exe_url", ""),
                "can_auto": bool(getattr(sys, "frozen", False))}
    except Exception as e:
        return {"current": VERSION, "latest": VERSION, "has_update": False, "error": str(e)[:80]}


def _ver_tuple(v: str):
    try:
        return tuple(int(x) for x in str(v).strip().lstrip("vV").split("."))
    except Exception:
        return (0,)


def _ulog(msg: str):
    """更新流程日志，写到 exe 旁边的 update.log，方便排查"""
    try:
        base = Path(sys.executable).parent if getattr(sys, "frozen", False) else BASE
        with open(base / "update.log", "a", encoding="utf-8") as f:
            f.write(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  {msg}\n")
    except Exception:
        pass


@app.post("/api/do_update")
async def api_do_update():
    """自动更新：下载新版 exe → 重命名运行中的自己 → 新版就位 → 重启。全程写 update.log。"""
    _ulog("==== do_update 开始 ====")
    if not getattr(sys, "frozen", False):
        return JSONResponse({"error": "源码运行不支持自动覆盖更新（开发时请用 git pull）"}, status_code=400)
    if not UPDATE_RAW_URL:
        return JSONResponse({"error": "未配置更新源"}, status_code=400)
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as c:
            info = (await c.get(UPDATE_RAW_URL)).json()
        exe_url = info.get("exe_url")
        _ulog(f"目标版本 {info.get('version')}  exe_url={exe_url}")
        if not exe_url:
            return JSONResponse({"error": "更新信息里没有 exe 下载地址"}, status_code=400)

        cur = Path(sys.executable)
        newf = cur.with_name(cur.stem + "_new.exe")
        _ulog(f"当前 exe = {cur}")
        # 下载新 exe
        _ulog("开始下载…")
        async with httpx.AsyncClient(timeout=None, follow_redirects=True) as c:
            async with c.stream("GET", exe_url) as r:
                if r.status_code != 200:
                    _ulog(f"下载失败 HTTP {r.status_code}")
                    return JSONResponse({"error": f"下载新版失败 HTTP {r.status_code}"}, status_code=400)
                tmp = newf.with_suffix(".part")
                with open(tmp, "wb") as f:
                    async for chunk in r.aiter_bytes(1 << 16):
                        f.write(chunk)
                tmp.replace(newf)
        size = newf.stat().st_size
        _ulog(f"下载完成，大小 {size} 字节")
        if size < 1_000_000:
            newf.unlink(missing_ok=True)
            return JSONResponse({"error": "下载的文件异常（过小），可能网络中断了"}, status_code=400)

        # 等新 exe 可读（杀毒扫描完、释放锁）
        readable = False
        for i in range(60):
            try:
                with open(newf, "rb") as _f:
                    _f.read(1)
                readable = True
                _ulog(f"新 exe 可读（第 {i} 次尝试）")
                break
            except Exception as e:
                if i == 0:
                    _ulog(f"新 exe 暂不可读，等待…（{e}）")
                await asyncio.sleep(1)
        if not readable:
            _ulog("新 exe 60 秒内一直不可读 → 判定被锁")
            os.startfile(str(cur.parent))
            return JSONResponse(
                {"error": f"新版已下载好，但一直读不了（可能被杀毒锁住）。\n请手动：关闭软件 → 把「{newf.name}」改名成「{cur.name}」。\n（已打开文件夹）"},
                status_code=400)

        # 重命名运行中的自己 → _old.exe，再把新版就位
        oldf = cur.with_name(cur.stem + "_old.exe")
        try:
            if oldf.exists():
                oldf.unlink()
        except Exception as e:
            _ulog(f"删旧的 _old.exe 失败（忽略）：{e}")
        try:
            os.replace(str(cur), str(oldf))
            _ulog("步骤1 OK：已把运行中的 exe 改名为 _old.exe")
        except Exception as e:
            _ulog(f"步骤1 失败：重命名当前 exe 出错：{e}")
            os.startfile(str(cur.parent))
            return JSONResponse(
                {"error": f"重命名当前程序失败（{e}）。新版已下载，请手动替换。（已打开文件夹）"},
                status_code=400)
        try:
            os.replace(str(newf), str(cur))
            _ulog("步骤2 OK：新版已就位为正式 exe")
        except Exception as e:
            _ulog(f"步骤2 失败：新版就位出错：{e} → 回滚")
            try:
                os.replace(str(oldf), str(cur))
            except Exception:
                pass
            os.startfile(str(cur.parent))
            return JSONResponse(
                {"error": f"新版就位失败（{e}）。已回滚，请手动替换。（已打开文件夹）"},
                status_code=400)

        # 直接启动新 exe（不经 PowerShell 中转——从冻结 exe spawn PowerShell 不可靠）。
        # 新 exe 启动时会等旧进程退出、端口释放（见 desktop.py 的 _wait_port_free）。
        DETACHED = 0x00000008
        NEW_GROUP = 0x00000200
        try:
            subprocess.Popen([str(cur)], creationflags=DETACHED | NEW_GROUP,
                             cwd=str(cur.parent), close_fds=True)
            _ulog("已直接启动新 exe")
        except Exception as e:
            _ulog(f"启动新 exe 失败：{e}")
        _ulog("==== do_update 成功收尾，1.5 秒后退出本进程 ====")
        threading.Timer(1.5, lambda: os._exit(0)).start()
        return {"ok": True, "version": info.get("version")}
    except Exception as e:
        _ulog(f"do_update 异常：{e}")
        return JSONResponse({"error": f"更新失败: {e}"}, status_code=400)


@app.post("/api/test_notify")
def api_test_notify():
    notify("🔔 测试通知", "如果你看到这条弹窗并听到声音，说明通知功能正常。")
    return {"ok": True}


if __name__ == "__main__":
    print(f"抖音博主更新监控面板: http://127.0.0.1:{PORT}", flush=True)
    threading.Timer(1.5, lambda: os.startfile(f"http://127.0.0.1:{PORT}")).start()
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")
