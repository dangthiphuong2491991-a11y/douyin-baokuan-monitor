# -*- coding: utf-8 -*-
"""抖音博主更新监控 — 定时检查博主新作品，弹窗提醒 + 自动下载无水印视频 + 本地面板"""
import asyncio
import json
import base64
import os
import random
import re
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse, StreamingResponse
from pydantic import BaseModel

import platforms
from platforms import get_adapter, PLATFORM_LIST
import channels
import dedup
import jianying


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
VERSION = "1.0.13"
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

# 合集集下载台账：合集集存成 1.mp4/2.mp4 会丢掉 aweme_id，靠文件名认不出「已下载」，
# 所以按真实 aweme_id 单独记一笔。aweme_id -> {mix_id,mix_name,ep_no,nickname,platform,ts}
MIX_DL_FILE = DATA / "mix_dl.json"
MIX_DL = _load(MIX_DL_FILE, {})

# 视频号发布
CH_STATE_FILE = DATA / "channels_state.json"   # 老单账号 cookie（迁移用）
CH_DIR = DATA / "channels"                     # 多账号：每个账号一个 {id}.json cookie
CH_DIR.mkdir(exist_ok=True)
CH_LOGIN = {"running": False, "status": "", "for": ""}   # for=正在登录的账号id
UPLOAD_TASKS = {}
UPLOAD_QUEUE: asyncio.Queue = asyncio.Queue()
_up_seq = 0

# 视频去重
DEDUP_TASKS = {}
DEDUP_QUEUE: asyncio.Queue = asyncio.Queue()
_dd_seq = 0


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
    for k, v in (("mix_follows", []), ("takeoff_vel", 3000), ("digest_hour", 9), ("ch_accounts", []),
                 ("jy_gen", []), ("jy_output_dir", ""),
                 ("jy_autodel", {"enable": True, "hours": 1})):
        if k not in config:
            config[k] = v
            changed = True
    # 老 jy_made(纯名字列表) → jy_gen(带时间戳)
    if config.get("jy_made"):
        have = {g["name"] for g in config["jy_gen"]}
        for nm in config.pop("jy_made"):
            if nm not in have:
                config["jy_gen"].append({"name": nm, "ts": time.time(), "src": "tpl"})
        changed = True
    # 老单账号 cookie → 迁移成第一个多账号
    if CH_STATE_FILE.exists() and not config.get("ch_accounts"):
        aid = "a1"
        try:
            CH_STATE_FILE.replace(CH_DIR / f"{aid}.json")
        except Exception:
            pass
        config["ch_accounts"] = [{"id": aid, "name": "视频号1", "note": "", "group": "默认分组",
                                  "added": datetime.now().strftime("%Y-%m-%d %H:%M")}]
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


async def download_aweme_media(nickname: str, aweme: dict, platform: str = "douyin",
                               subdir: str = "", ep_no=None) -> tuple:
    """下载单个作品（视频或图集）。subdir=合集名(进子文件夹) ; ep_no=集数(文件名直接叫 1,2,3)。返回 (ok, 文件url)"""
    it = get_adapter(platform).normalize(aweme)
    aid = it["aweme_id"]
    dl = get_dl()
    folder = platform_dir(platform) / sanitize(nickname, 30)
    if subdir:
        folder = folder / sanitize(subdir, 40)
    if ep_no is not None:
        stem = str(int(ep_no))     # 合集：干净的集数命名 1 / 2 / 3
    else:
        date_tag = datetime.fromtimestamp(int(it.get("create_time") or time.time())).strftime("%Y%m%d")
        stem = f"{date_tag}_{sanitize(it['desc'], 40)}_{aid[-6:]}"

    # 下载正片不再存封面 .jpg（库里只留视频/图集，不产生多余缩略图）
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


def _mix_episodes_done(nickname: str, mix_name: str, platform: str = "douyin") -> set:
    """某合集子文件夹里已下载的集数集合（文件名形如 1.mp4）—— 用于合集追更的"只下缺集"。"""
    folder = platform_dir(platform) / sanitize(nickname, 30) / sanitize(mix_name, 40)
    done = set()
    if folder.exists():
        for f in folder.iterdir():
            m = re.fullmatch(r"(\d+)\.mp4", f.name)
            if m:
                done.add(int(m.group(1)))
    return done


def _mix_dl_record(aid: str, mix_id, mix_name, ep_no, nickname, platform):
    """合集集下载成功后记一笔（按真实 aweme_id），供「已下载」标志识别。"""
    MIX_DL[aid] = {"mix_id": mix_id, "mix_name": mix_name, "ep_no": ep_no,
                   "nickname": nickname, "platform": platform,
                   "ts": datetime.now().strftime("%Y-%m-%d %H:%M")}
    _save(MIX_DL_FILE, MIX_DL)


def _is_downloaded(aid: str, done_tags: set) -> bool:
    """单条是否已下载：普通作品看文件名尾6位；合集集看 aweme_id 台账。"""
    aid = str(aid)
    return aid[-6:] in done_tags or aid in MIX_DL


def _mix_annot(it: dict, platform: str = "douyin") -> dict:
    """给一条作品算合集状态：属于哪个合集、云端多少集、本地有几集、新增几集可补齐。
    只对「下载过/在追更列表」的合集返回，避免给每条陌生视频都塞一坨。返回 {} 表示不显示。"""
    mix = it.get("mix") or {}
    mid = mix.get("mix_id")
    if not mid:
        return {}
    f = next((x for x in config.get("mix_follows", []) if x.get("mix_id") == mid), None)
    if not f:                       # 这个合集从没下载过 → 不打标（避免噪音）
        return {}
    done_eps = _mix_episodes_done(f.get("nickname", ""), f.get("mix_name", ""), platform)
    local = len(done_eps)
    cloud = f.get("cloud_total") or mix.get("total") or local
    ep = _to_int(it.get("episode"))
    return {"mix_id": mid, "mix_name": f.get("mix_name") or mix.get("mix_name") or "",
            "local_have": local, "cloud_total": cloud,
            "new_count": max(0, cloud - local), "followed": True,
            # 这一条本身下没下：磁盘上已有这集号（老下载也认得出）
            "this_downloaded": bool(ep and ep in done_eps)}


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
        try:
            _purge_expired_drafts()          # 定时删除过期的生成草稿
        except Exception as e:
            log_err(f"定时删草稿出错: {e}")
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
    asyncio.create_task(_upload_worker())    # 视频号上传：单工人串行，防风控
    asyncio.create_task(_dedup_worker())     # 视频去重：单工人（ffmpeg 吃 CPU）


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
        u["downloaded"] = _is_downloaded(u.get("aweme_id", ""), tags[key])
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
        "uploads": {k: sum(1 for t in UPLOAD_TASKS.values() if t["status"] == k)
                    for k in ("queued", "running", "done", "failed")},
        "dedups": {k: sum(1 for t in DEDUP_TASKS.values() if t["status"] == k)
                   for k in ("queued", "running", "done", "failed")},
        "ch_logged_in": ch_logged_in(),
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
        annot = _mix_annot(it, platform)
        items.append({
            "aweme_id": aid,
            "desc": it["desc"] or "(无标题)",
            "type": "图集" if it["is_images"] else "视频",
            "cover": it["cover"] or "",
            "create_time": fmt_ts(it["create_time"]),
            "duration": f"{dur // 60000}:{dur % 60000 // 1000:02d}" if dur else "",
            "digg": it["digg"],
            "downloaded": _is_downloaded(aid, done) or annot.get("this_downloaded", False),
            "mix": annot,
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


def enqueue_download(platform, nickname, aid, desc="", kind="单条", mix_id=None, mix_name="", ep_no=None):
    global _task_seq
    _task_seq += 1
    tid = f"t{_task_seq}"
    DL_TASKS[tid] = {"id": tid, "platform": platform, "nickname": nickname or "",
                     "aweme_id": str(aid), "desc": (desc or "")[:60], "kind": kind,
                     "mix_id": mix_id, "mix_name": mix_name, "ep_no": ep_no,
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
                if t.get("mix_name"):   # 合集集：进《合集名》子文件夹，文件名直接叫集数
                    ep = t.get("ep_no") or get_adapter(platform).normalize(aweme).get("episode")
                    ok, _u = await download_aweme_media(nm, aweme, platform,
                                                        subdir=t["mix_name"], ep_no=ep)
                    if ok:                       # 按真实 id 记台账，下次查找能认出「已下载」
                        _mix_dl_record(str(t["aweme_id"]), t.get("mix_id"), t["mix_name"],
                                       ep, nm, platform)
                else:
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
    m0 = ad.normalize(episodes[0])                # 用真实合集名当子文件夹名
    mix_name = (m0.get("mix") or {}).get("mix_name") or mix_name
    MIX_PROGRESS[mix_id]["name"] = mix_name
    done_eps = _mix_episodes_done(nickname, mix_name, platform)   # 该合集已下到哪几集
    stash = POSTS_CACHE.setdefault("_mix", {})   # 暂存完整数据，worker 下载时免重拉详情
    new_eps = []
    for i, ep in enumerate(episodes):
        it = ad.normalize(ep)
        stash[it["aweme_id"]] = ep
        ep_no = it.get("episode") or (i + 1)     # 真实集数，拿不到就用列表位置兜底
        if ep_no in done_eps:
            continue                              # 已有的集跳过 → 补齐只下缺集
        new_eps.append((it["aweme_id"], it["desc"], ep_no))
    _upsert_mix_follow(mix_id, mix_name, nickname, platform, len(episodes), sample_aid=m0["aweme_id"])
    if not new_eps:
        MIX_PROGRESS.pop(mix_id, None)
        print(f"[MIX] 《{mix_name}》没有新集要下（本地已齐 {len(episodes)} 集）", flush=True)
        return
    MIX_PROGRESS[mix_id]["total"] = len(new_eps)
    for aid, desc, ep_no in new_eps:
        enqueue_download(platform, nickname, aid, desc,
                         kind=f"合集《{mix_name}》第{ep_no}集", mix_id=mix_id, mix_name=mix_name, ep_no=ep_no)
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
        d["local_have"] = len(_mix_episodes_done(f.get("nickname", ""), f.get("mix_name", ""),
                                                 f.get("platform", "douyin")))
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
                        "downloaded": _is_downloaded(rid, seen_authors_tags[nick]) or _mix_annot(it, platform).get("this_downloaded", False),
                        "mix": _mix_annot(it, platform),
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
                "downloaded": _is_downloaded(aid, tags) or _mix_annot(it, platform).get("this_downloaded", False),
                "mix": _mix_annot(it, platform),
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

    def _ep_key(f: Path):
        m = re.match(r"(\d+)", f.stem)
        return (int(m.group(1)) if m else 10 ** 9, f.name)

    def collect(folder: Path, label: str, is_mix: bool):
        files, subs = [], []
        for f in folder.iterdir():
            if f.is_dir():
                subs.append(f)
            elif f.is_file() and not f.name.endswith(".part") \
                    and f.suffix.lower() != ".jpg":     # .jpg=监控缩略图，不进下载库（图集是 .jpeg 照留）
                files.append(f)
        files.sort(key=_ep_key) if is_mix else \
            files.sort(key=lambda p: p.stat().st_mtime, reverse=True)   # 合集按集数，其余按时间
        items = [{"name": f.name, "url": f"/files/{f.relative_to(base).as_posix()}",
                  "size": round(f.stat().st_size / 1024 / 1024, 2),
                  "is_video": f.suffix.lower() == ".mp4"} for f in files]
        if items:
            groups.append({"blogger": label, "count": len(items), "files": items, "is_mix": is_mix,
                           "rel": folder.relative_to(root).as_posix()})
        return subs

    if root.exists():
        for d in sorted(root.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
            if not d.is_dir():
                continue
            subs = collect(d, d.name, False)                       # 博主直属单条
            for sd in sorted(subs, key=lambda p: p.stat().st_mtime, reverse=True):
                collect(sd, f"{d.name} › {sd.name}", True)         # 合集子文件夹，按集数排
    return {"groups": groups}


@app.post("/api/reveal")
def api_reveal():
    os.startfile(str(get_dl()))
    return {"ok": True}


@app.post("/api/reveal_blogger/{name}")
def api_reveal_blogger(name: str, platform: str = "douyin", rel: str = ""):
    root = platform_dir(platform)
    if rel:   # 合集子文件夹用相对路径打开（带越界保护）
        target = (root / rel).resolve()
        if target.is_dir() and str(target).startswith(str(root.resolve())):
            os.startfile(str(target))
            return {"ok": True}
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


@app.post("/api/pick_folder")
def api_pick_folder():
    """通用：弹原生文件夹选择框，只返回选中的路径，不改任何配置。给素材库/输出位置等用。"""
    try:
        import webview
        win = webview.active_window() or (webview.windows[0] if webview.windows else None)
        if not win:
            raise RuntimeError("no window")
        res = win.create_file_dialog(webview.FOLDER_DIALOG)
        if not res:
            return {"ok": False, "cancelled": True}
        path = res[0] if isinstance(res, (list, tuple)) else res
        return {"ok": True, "path": str(Path(path))}
    except Exception as e:
        return JSONResponse(
            {"error": f"当前环境无法弹出文件夹选择框，请手动粘贴路径。({e})"}, status_code=400)


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


# ============ 视频号发布：多账号批量登录 + 批量发布（仿小V猫） ============
def _ch_state(aid: str) -> Path:
    return CH_DIR / f"{aid}.json"


def ch_online(aid: str) -> bool:
    return _ch_state(aid).exists()


def _ch_accounts() -> list:
    return config.setdefault("ch_accounts", [])


def _ch_account(aid: str):
    return next((a for a in _ch_accounts() if a["id"] == aid), None)


def ch_logged_in() -> bool:
    return any(ch_online(a["id"]) for a in _ch_accounts())


def _accs_with_online() -> list:
    return [{**a, "online": ch_online(a["id"])} for a in _ch_accounts()]


async def _ch_login_bg(aid: str, is_new: bool, note: str, group: str):
    CH_LOGIN["for"] = aid
    CH_LOGIN["running"] = True
    CH_LOGIN["status"] = "启动中…"
    try:
        ok, info = await channels.login(_ch_state(aid), timeout=240,
                                        on_status=lambda m: CH_LOGIN.update(status=m))
        info = info if isinstance(info, dict) else {"nickname": info or ""}
        nickname, wxid = info.get("nickname", ""), info.get("wxid", "")
        if ok and is_new:
            n = len(_ch_accounts()) + 1
            _ch_accounts().append({"id": aid, "name": nickname or f"视频号{n}", "note": note,
                                   "group": group or "默认分组", "wxid": wxid,
                                   "avatar": info.get("avatar", False),
                                   "added": datetime.now().strftime("%Y-%m-%d %H:%M")})
            _save(CONFIG_FILE, config)
        elif ok:
            acc = _ch_account(aid)
            if acc:
                if nickname and not acc.get("name_locked"):
                    acc["name"] = nickname
                if wxid:
                    acc["wxid"] = wxid
                if info.get("avatar"):
                    acc["avatar"] = True
                _save(CONFIG_FILE, config)
    except Exception as e:
        CH_LOGIN["status"] = f"登录出错：{str(e)[:120]}"
        log_err(f"视频号登录出错: {e}")
    finally:
        CH_LOGIN["running"] = False


class ChAddBody(BaseModel):
    note: str = ""
    group: str = "默认分组"


@app.post("/api/channels/account/add")
async def api_ch_add(body: ChAddBody):
    if CH_LOGIN["running"]:
        return {"ok": True, "already": True}
    aid = os.urandom(4).hex()
    asyncio.create_task(_ch_login_bg(aid, True, body.note, body.group))
    return {"ok": True, "id": aid}


@app.post("/api/channels/account/{aid}/relogin")
async def api_ch_relogin(aid: str):
    if not _ch_account(aid):
        return JSONResponse({"error": "账号不存在"}, status_code=404)
    if CH_LOGIN["running"]:
        return {"ok": True, "already": True}
    asyncio.create_task(_ch_login_bg(aid, False, "", ""))
    return {"ok": True}


CH_LISTS = {"running": False, "data": {}, "for": ""}


async def _ch_fetch_lists_bg(aid: str):
    CH_LISTS.update({"running": True, "for": aid, "data": {}})
    try:
        CH_LISTS["data"] = await channels.fetch_lists(_ch_state(aid))
    except Exception as e:
        log_err(f"拉取视频号列表失败: {e}")
        CH_LISTS["data"] = {"error": str(e)[:120]}
    finally:
        CH_LISTS["running"] = False


@app.post("/api/channels/account/{aid}/fetch_lists")
async def api_ch_fetch_lists(aid: str):
    """用该账号登录态拉它的 合集/剧集/活动 列表。"""
    if not _ch_account(aid):
        return JSONResponse({"error": "账号不存在"}, status_code=404)
    if not ch_online(aid):
        return JSONResponse({"error": "该账号离线，请先重新扫码登录"}, status_code=400)
    if CH_LISTS["running"]:
        return {"ok": True, "already": True}
    asyncio.create_task(_ch_fetch_lists_bg(aid))
    return {"ok": True}


@app.get("/api/channels/lists_status")
def api_ch_lists_status():
    return CH_LISTS


@app.get("/api/channels/account/{aid}/avatar")
def api_ch_avatar(aid: str):
    """账号头像（登录时抓存的 {aid}.jpg）。"""
    p = _ch_state(aid).with_suffix(".jpg")
    if p.exists():
        return FileResponse(str(p))
    return JSONResponse({"error": "无头像"}, status_code=404)


@app.delete("/api/channels/account/{aid}")
def api_ch_del(aid: str):
    config["ch_accounts"] = [a for a in _ch_accounts() if a["id"] != aid]
    _save(CONFIG_FILE, config)
    try:
        _ch_state(aid).unlink(missing_ok=True)
        _ch_state(aid).with_suffix(".jpg").unlink(missing_ok=True)
    except Exception:
        pass
    return {"ok": True}


class ChEditBody(BaseModel):
    name: str = None
    note: str = None
    group: str = None


@app.post("/api/channels/account/{aid}/edit")
def api_ch_edit(aid: str, body: ChEditBody):
    a = _ch_account(aid)
    if not a:
        return JSONResponse({"error": "账号不存在"}, status_code=404)
    if body.name is not None:
        a["name"], a["name_locked"] = body.name, True
    if body.note is not None:
        a["note"] = body.note
    if body.group is not None:
        a["group"] = body.group
    _save(CONFIG_FILE, config)
    return {"ok": True}


@app.get("/api/channels/accounts")
def api_ch_accounts():
    return {"accounts": _accs_with_online(), "login_running": CH_LOGIN["running"],
            "login_for": CH_LOGIN.get("for", ""), "login_status": CH_LOGIN["status"]}


@app.get("/api/channels/status")
def api_channels_status():
    counts = {k: 0 for k in ("queued", "running", "done", "failed")}
    for t in UPLOAD_TASKS.values():
        counts[t["status"]] = counts.get(t["status"], 0) + 1
    tasks = list(UPLOAD_TASKS.values())[-200:]
    tasks.reverse()
    return {"logged_in": ch_logged_in(), "accounts": _accs_with_online(),
            "login_running": CH_LOGIN["running"], "login_for": CH_LOGIN.get("for", ""),
            "login_status": CH_LOGIN["status"], "counts": counts, "tasks": tasks}


@app.get("/api/channels/videos")
def api_channels_videos():
    """列出可挑选上传的 mp4：下载目录 + 剪映成片输出文件夹（jy_output_dir）。"""
    out = []
    seen = set()
    plat_names = {"抖音", "TikTok"}

    def _scan(root: Path, top_label: str):
        if not root.exists():
            return
        for f in root.rglob("*.mp4"):
            if not f.is_file() or f.name.endswith(".part") or str(f) in seen:
                continue
            seen.add(str(f))
            parts = list(f.relative_to(root).parts[:-1])
            if parts and parts[0] in plat_names:
                parts = parts[1:]
            blogger = parts[0] if len(parts) >= 1 else top_label
            out.append({"path": str(f), "name": f.name,
                        "blogger": blogger,
                        "mix": parts[1] if len(parts) >= 2 else "",
                        "size": round(f.stat().st_size / 1024 / 1024, 1),
                        "mtime": f.stat().st_mtime})

    _scan(get_dl(), "")
    od = config.get("jy_output_dir")
    if od:
        _scan(Path(od), "🎬 剪映成片")
    out.sort(key=lambda x: x["mtime"], reverse=True)
    return {"videos": out[:800]}


class UploadItem(BaseModel):
    video_path: str
    title: str = ""        # 视频号短标题
    tags: list[str] = []   # 话题
    desc: str = ""         # 简介
    cover_path: str = ""   # 封面图
    link: str = ""         # 扩展链接
    original: bool = False
    schedule: str = ""     # "YYYY-MM-DD HH:MM" 或空=立即


# ---------------- 封面：ffmpeg 抽帧 / 本地选图（每条视频一张封面） ----------------
COVERS_DIR = DATA / "covers"


def _capture_cover(video_path: str, mode: str = "first", sec: float = 0.0) -> str:
    """从视频抽一帧当封面。mode: first/last/random/at(指定秒)。返回封面 jpg 绝对路径。"""
    COVERS_DIR.mkdir(exist_ok=True)
    dur = 0.0
    try:
        dur = dedup.probe_duration(video_path) or 0.0
    except Exception:
        pass
    if mode == "first":
        t = 0.1
    elif mode == "last":
        t = max(0.0, dur - 0.3)
    elif mode == "random":
        t = random.uniform(0.0, max(0.2, dur - 0.3)) if dur else 0.1
    else:                       # at 指定秒
        t = max(0.0, float(sec or 0))
    stem = sanitize(Path(video_path).stem, 20)
    out = COVERS_DIR / f"{stem}_{int(t * 1000)}_{random.randint(1000, 9999)}.jpg"
    try:
        subprocess.run([dedup.FF, "-y", "-ss", str(t), "-i", str(video_path),
                        "-frames:v", "1", "-q:v", "3", str(out)],
                       capture_output=True, timeout=40)
    except Exception as e:
        log_err(f"抽帧封面失败: {e}")
    return str(out) if out.exists() else ""


class CaptureBody(BaseModel):
    video_path: str
    mode: str = "first"        # first/last/random/at
    sec: float = 0.0


@app.post("/api/channels/capture_frame")
def api_ch_capture(body: CaptureBody):
    if not Path(body.video_path).exists():
        return JSONResponse({"error": "视频不存在"}, status_code=400)
    p = _capture_cover(body.video_path, body.mode, body.sec)
    if not p:
        return JSONResponse({"error": "抽帧失败（视频可能损坏）"}, status_code=400)
    return {"ok": True, "cover_path": p, "url": f"/api/localimg?path={quote(p)}"}


@app.post("/api/channels/samename_cover")
def api_ch_samename(body: CaptureBody):
    """读取同名封面：找和视频同名的图片文件（同目录）。"""
    v = Path(body.video_path)
    for ext in (".jpg", ".jpeg", ".png", ".webp"):
        cand = v.with_suffix(ext)
        if cand.exists():
            return {"ok": True, "cover": str(cand), "url": f"/api/localimg?path={quote(str(cand))}"}
    return {"ok": False, "cover": ""}


@app.get("/api/localimg")
def api_localimg(path: str):
    """本地图片直读（封面预览用）。只放行图片扩展名。"""
    p = Path(path)
    if p.suffix.lower() not in (".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif") or not p.exists():
        return JSONResponse({"error": "不是图片或不存在"}, status_code=404)
    return FileResponse(str(p))


_VIDEO_EXTS = (".mp4", ".mov", ".mkv", ".avi", ".webm", ".flv", ".m4v")


@app.post("/api/channels/pick_videos")
def api_ch_pick_videos():
    """选择文件添加：弹原生多选视频框，返回路径列表。"""
    try:
        import webview
        win = webview.active_window() or (webview.windows[0] if webview.windows else None)
        if not win:
            raise RuntimeError("no window")
        res = win.create_file_dialog(webview.OPEN_DIALOG, allow_multiple=True,
                                     file_types=("视频 (*.mp4;*.mov;*.mkv;*.avi;*.webm)", "所有文件 (*.*)"))
        if not res:
            return {"ok": False, "cancelled": True}
        paths = list(res) if isinstance(res, (list, tuple)) else [res]
        return {"ok": True, "paths": [str(p) for p in paths]}
    except Exception as e:
        return JSONResponse({"error": f"无法弹出选择框：{e}"}, status_code=400)


@app.post("/api/channels/pick_video_dir")
def api_ch_pick_video_dir():
    """选择目录添加：弹文件夹框，递归扫出里面所有视频。"""
    try:
        import webview
        win = webview.active_window() or (webview.windows[0] if webview.windows else None)
        if not win:
            raise RuntimeError("no window")
        res = win.create_file_dialog(webview.FOLDER_DIALOG)
        if not res:
            return {"ok": False, "cancelled": True}
        folder = res[0] if isinstance(res, (list, tuple)) else res
        vids = [str(f) for f in Path(folder).rglob("*")
                if f.is_file() and f.suffix.lower() in _VIDEO_EXTS and not f.name.endswith(".part")]
        return {"ok": True, "paths": sorted(vids)}
    except Exception as e:
        return JSONResponse({"error": f"无法弹出选择框：{e}"}, status_code=400)


@app.post("/api/channels/pick_cover")
def api_ch_pick_cover():
    """弹原生图片选择框，返回选中的图片路径。"""
    try:
        import webview
        win = webview.active_window() or (webview.windows[0] if webview.windows else None)
        if not win:
            raise RuntimeError("no window")
        res = win.create_file_dialog(
            webview.OPEN_DIALOG, file_types=("图片 (*.jpg;*.jpeg;*.png;*.webp)", "所有文件 (*.*)"))
        if not res:
            return {"ok": False, "cancelled": True}
        path = res[0] if isinstance(res, (list, tuple)) else res
        return {"ok": True, "path": str(path), "url": f"/api/localimg?path={quote(str(path))}"}
    except Exception as e:
        return JSONResponse({"error": f"无法弹出选择框：{e}"}, status_code=400)


class UploadBody(BaseModel):
    items: list[UploadItem] = []
    account_ids: list[str] = []      # 发到哪些账号（空=所有在线账号）
    distribute: str = "multi"        # multi=多对一(每号发全部) / one2one=一对一 / even=均分
    shuffle: bool = False            # 打乱作品顺序
    time_mode: str = "now"           # now=立即 / schedule=平台定时 / local=本机定时
    base_time: str = ""              # "YYYY-MM-DD HH:MM"（定时/本机定时的起始时间）
    clip_gap_sec: int = 0            # 作品间隔（秒）：同账号发多条之间
    acct_gap_sec: int = 0            # 账号间隔（秒）：不同账号之间
    # —— 均分模式（仿小V猫）——
    even_per_day: int = 1            # 每个号一天发几条
    even_dispatch: str = "single"    # single=单号优先 / rotate=顺序循环分派
    even_multiday: bool = True       # 多日循环：一天发不完滚到下一天(时间点平移)
    # —— 视频号发布设置（选账号后那一栏，套用所有作品）——
    ch_statement: str = ""           # 视频声明：无需申明/含AI生成内容/…
    ch_at: str = ""                  # @视频号（昵称/id）
    ch_at_pos: str = "tail"          # @追加到简介位置：tail末尾 / head开头
    ch_location: str = ""            # 地理位置
    ch_collection: str = ""          # 添加到合集
    ch_drama: str = ""               # 扩展链接·视频号剧集
    ch_activity: str = ""            # 活动


def _plan_upload(body: "UploadBody"):
    """算出分发计划：[(account_id, name, item, publish_at)]。不入队，供预览/发布共用。"""
    online = [a["id"] for a in _ch_accounts() if ch_online(a["id"])]
    targets = [a for a in body.account_ids if a in online] if body.account_ids else online
    items = [it for it in body.items if Path(it.video_path).exists()]
    if body.shuffle:
        items = items[:]
        random.shuffle(items)
    base = None
    if body.time_mode in ("schedule", "local") and body.base_time:
        try:
            base = datetime.strptime(body.base_time, "%Y-%m-%d %H:%M")
        except Exception:
            base = None
    base = base or datetime.now()

    def _name(aid):
        acc = _ch_account(aid)
        return acc["name"] if acc else aid

    plan = []
    A = len(targets)
    if not A or not items:
        return targets, plan

    if body.distribute == "even":              # 均分：每号一天 N 条，滚天
        N = max(1, int(body.even_per_day or 1))
        cap = A * N                            # 一天总容量
        for g, it in enumerate(items):
            day = (g // cap) if body.even_multiday else 0
            wd = (g % cap) if body.even_multiday else g
            if body.even_dispatch == "rotate":         # 顺序循环：文件轮流发给各号
                acct_i, slot = wd % A, wd // A
            else:                                      # 单号优先：先把一个号填满 N 条
                acct_i, slot = min(wd // N, A - 1), wd % N
            aid = targets[acct_i]
            at = base + timedelta(days=day,
                                  seconds=acct_i * body.acct_gap_sec + slot * body.clip_gap_sec)
            plan.append((aid, _name(aid), it, at))
    else:                                       # multi=每号发全部 / one2one=平摊
        for ai, aid in enumerate(targets):
            my = [items[i] for i in range(len(items)) if i % A == ai] \
                if body.distribute == "one2one" else items
            for ci, it in enumerate(my):
                at = base + timedelta(seconds=ai * body.acct_gap_sec + ci * body.clip_gap_sec)
                plan.append((aid, _name(aid), it, at))
    return targets, plan


@app.post("/api/channels/upload/preview")
def api_channels_preview(body: UploadBody):
    targets, plan = _plan_upload(body)
    rows = [{"account": aname, "title": it.title or Path(it.video_path).stem,
             "when": at.strftime("%m-%d %H:%M") if body.time_mode != "now" else "立即"}
            for (aid, aname, it, at) in plan]
    return {"accounts": len(targets), "count": len(plan), "rows": rows[:200]}


@app.post("/api/channels/upload")
def api_channels_upload(body: UploadBody):
    targets, plan = _plan_upload(body)
    if not targets:
        return JSONResponse({"error": "没有在线的视频号账号，请先添加/登录账号"}, status_code=400)
    if not plan:
        return JSONResponse({"error": "没有有效视频"}, status_code=400)
    global _up_seq
    for aid, aname, it, at in plan:
        sched_str = at.strftime("%Y-%m-%d %H:%M") if body.time_mode == "schedule" else ""
        # @视频号 直接拼进简介（这个不用视频号选择器，纯文本）
        desc = it.desc
        if body.ch_at:
            at_str = "@" + body.ch_at.lstrip("@")
            desc = (at_str + " " + desc) if body.ch_at_pos == "head" else (desc + " " + at_str).strip()
        _up_seq += 1
        tid = f"u{_up_seq}"
        UPLOAD_TASKS[tid] = {"id": tid, "account_id": aid, "account_name": aname,
                             "title": it.title or Path(it.video_path).stem,
                             "video_path": it.video_path, "tags": it.tags, "desc": desc,
                             "cover_path": it.cover_path, "link": it.link, "original": it.original,
                             "statement": body.ch_statement, "location": body.ch_location,
                             "collection": body.ch_collection, "drama": body.ch_drama,
                             "activity": body.ch_activity,
                             "schedule": sched_str,
                             "publish_at": at.timestamp() if body.time_mode == "local" else 0,
                             "when": at.strftime("%m-%d %H:%M") if body.time_mode != "now" else "",
                             "status": "queued", "stage": "", "err": "",
                             "ts": datetime.now().strftime("%H:%M:%S")}
        UPLOAD_QUEUE.put_nowait(tid)
    return {"ok": True, "queued": len(plan), "accounts": len(targets)}


class PubDraftBody(BaseModel):
    name: str = ""
    data: dict = {}          # 前端整包配置（选的视频/账号/分发设置/文案）


@app.get("/api/channels/pubdrafts")
def api_ch_pubdrafts():
    return {"drafts": [{"name": d.get("name"), "at": d.get("at"), "count": len((d.get("data") or {}).get("items", []))}
                       for d in config.get("ch_pub_drafts", [])]}


@app.post("/api/channels/pubdrafts/save")
def api_ch_pubdraft_save(body: PubDraftBody):
    name = body.name.strip() or datetime.now().strftime("草稿_%m%d_%H%M")
    dl = config.setdefault("ch_pub_drafts", [])
    dl[:] = [d for d in dl if d.get("name") != name]
    dl.insert(0, {"name": name, "at": datetime.now().strftime("%m-%d %H:%M"), "data": body.data})
    config["ch_pub_drafts"] = dl[:30]
    _save(CONFIG_FILE, config)
    return {"ok": True, "name": name}


@app.get("/api/channels/pubdrafts/{name}")
def api_ch_pubdraft_load(name: str):
    d = next((x for x in config.get("ch_pub_drafts", []) if x.get("name") == name), None)
    return {"data": (d or {}).get("data", {})} if d else JSONResponse({"error": "草稿不存在"}, status_code=404)


@app.delete("/api/channels/pubdrafts/{name}")
def api_ch_pubdraft_del(name: str):
    config["ch_pub_drafts"] = [x for x in config.get("ch_pub_drafts", []) if x.get("name") != name]
    _save(CONFIG_FILE, config)
    return {"ok": True}


@app.post("/api/channels/tasks/retry_failed")
def api_channels_retry():
    n = 0
    for t in UPLOAD_TASKS.values():
        if t["status"] == "failed":
            t.update(status="queued", err="", stage="")
            UPLOAD_QUEUE.put_nowait(t["id"])
            n += 1
    return {"ok": True, "requeued": n}


@app.post("/api/channels/tasks/clear")
def api_channels_clear():
    for tid in [k for k, t in UPLOAD_TASKS.items() if t["status"] in ("done", "failed")]:
        UPLOAD_TASKS.pop(tid, None)
    return {"ok": True}


async def _upload_worker():
    while True:
        tid = await UPLOAD_QUEUE.get()
        t = UPLOAD_TASKS.get(tid)
        if not t or t["status"] != "queued":
            continue
        aid = t.get("account_id")
        if not aid or not ch_online(aid):
            t.update(status="failed", err="该账号未登录")
            continue
        pa = t.get("publish_at") or 0     # 本机定时：软件等到点再发
        if pa:
            while t["status"] == "queued":
                wait = pa - time.time()
                if wait <= 0:
                    break
                t["stage"] = f"本机定时等待中（还 {int(wait)}s）"
                await asyncio.sleep(min(wait, 10))
        t["status"] = "running"
        sched = None
        if t.get("schedule"):
            try:
                sched = datetime.strptime(t["schedule"], "%Y-%m-%d %H:%M")
            except Exception:
                sched = None
        try:
            ok, msg = await channels.upload(
                _ch_state(aid), t["video_path"], title=t["title"], tags=t["tags"],
                desc=t["desc"], cover_path=t.get("cover_path", ""), original=t["original"],
                link=t.get("link", ""), statement=t.get("statement", ""),
                location=t.get("location", ""), collection=t.get("collection", ""),
                drama=t.get("drama", ""), activity=t.get("activity", ""),
                schedule=sched, headless=False, err_dir=DATA,
                on_status=lambda m: t.update(stage=m))
            t.update(status="done" if ok else "failed", err="" if ok else msg, stage=msg)
        except Exception as e:
            t.update(status="failed", err=str(e)[:160])
        await asyncio.sleep(3)   # 每条之间多停一会，降低风控/封号概率


# ==================== 视频去重处理（ffmpeg 滤镜链） ====================
def _dedup_out_dir() -> Path:
    d = get_dl() / "去重导出"
    d.mkdir(parents=True, exist_ok=True)
    return d


class DedupBody(BaseModel):
    video_paths: list[str] = []
    options: dict = {}


@app.post("/api/dedup")
def api_dedup(body: DedupBody):
    global _dd_seq
    n = 0
    for vp in body.video_paths:
        if not Path(vp).exists():
            continue
        _dd_seq += 1
        tid = f"d{_dd_seq}"
        DEDUP_TASKS[tid] = {"id": tid, "name": Path(vp).name, "video_path": vp,
                            "options": body.options or {}, "status": "queued", "stage": "",
                            "err": "", "out": "", "ts": datetime.now().strftime("%H:%M:%S")}
        DEDUP_QUEUE.put_nowait(tid)
        n += 1
    return {"ok": True, "queued": n, "out_dir": str(_dedup_out_dir())}


@app.get("/api/dedup/tasks")
def api_dedup_tasks():
    counts = {k: 0 for k in ("queued", "running", "done", "failed")}
    for t in DEDUP_TASKS.values():
        counts[t["status"]] = counts.get(t["status"], 0) + 1
    tasks = list(DEDUP_TASKS.values())[-200:]
    tasks.reverse()
    return {"counts": counts, "tasks": tasks, "out_dir": str(_dedup_out_dir()),
            "defaults": dedup.DEFAULTS}


@app.post("/api/dedup/retry_failed")
def api_dedup_retry():
    n = 0
    for t in DEDUP_TASKS.values():
        if t["status"] == "failed":
            t.update(status="queued", err="", stage="")
            DEDUP_QUEUE.put_nowait(t["id"])
            n += 1
    return {"ok": True, "requeued": n}


@app.post("/api/dedup/clear")
def api_dedup_clear():
    for tid in [k for k, t in DEDUP_TASKS.items() if t["status"] in ("done", "failed")]:
        DEDUP_TASKS.pop(tid, None)
    return {"ok": True}


@app.post("/api/dedup/reveal")
def api_dedup_reveal():
    os.startfile(str(_dedup_out_dir()))
    return {"ok": True}


async def _dedup_worker():
    while True:
        tid = await DEDUP_QUEUE.get()
        t = DEDUP_TASKS.get(tid)
        if not t or t["status"] != "queued":
            continue
        t["status"] = "running"
        src = Path(t["video_path"])
        out = _dedup_out_dir() / src.name
        if out.resolve() == src.resolve() or out.exists():   # 防覆盖源/重名
            out = _dedup_out_dir() / f"{src.stem}_{tid}{src.suffix}"
        try:
            ok, msg = await dedup.process(str(src), str(out), t["options"],
                                          on_status=lambda m: t.update(stage=m))
            t.update(status="done" if ok else "failed", err="" if ok else msg, stage=msg,
                     out=str(out) if ok else "")
        except Exception as e:
            t.update(status="failed", err=str(e)[:150])


# ==================== 剪映混剪（调剪映 DLL 解密→改主轨道→加密写新草稿） ====================
JY = {"running": False, "status": "", "made": [], "error": ""}


def _mark_jy_gen(names, src="tpl"):
    """记录本工具生成的草稿（带时间戳，供只认这些 + 定时删除）。src: tpl=做剪映模版 / mix=剪映混剪。"""
    gen = config.setdefault("jy_gen", [])
    now = time.time()
    for n in names:
        gen[:] = [g for g in gen if g.get("name") != n]
        gen.insert(0, {"name": n, "ts": now, "src": src})
    config["jy_gen"] = gen[:500]
    _save(CONFIG_FILE, config)


@app.get("/api/jy/drafts")
def api_jy_drafts(made_only: bool = False):
    """made_only=True 只返回「做剪映模版」(src=tpl) 生成过的草稿。"""
    try:
        installed = bool(jianying.find_install_dir())
        drafts = jianying.list_drafts()
        if made_only:
            tpl = {g["name"] for g in config.get("jy_gen", []) if g.get("src") == "tpl"}
            drafts = [d for d in drafts if d["name"] in tpl]
        return {"ok": True, "installed": installed, "drafts": drafts}
    except Exception as e:
        return {"ok": False, "installed": False, "error": str(e)[:150], "drafts": []}


def _purge_expired_drafts():
    """定时删除：把超过设定小时数的"本工具生成草稿"删掉（导出成片后清理剪映用）。"""
    ad = config.get("jy_autodel") or {}
    if not ad.get("enable"):
        return
    hours = float(ad.get("hours") or 1)
    cutoff = time.time() - hours * 3600
    gen = config.get("jy_gen", [])
    keep, removed = [], []
    for g in gen:
        if g.get("ts", 0) < cutoff:
            try:
                jianying.delete_draft(g["name"])
                removed.append(g["name"])
            except Exception as e:
                log_err(f"定时删草稿 {g.get('name')} 失败: {e}")
        else:
            keep.append(g)
    if removed:
        config["jy_gen"] = keep
        _save(CONFIG_FILE, config)
        print(f"[JY] 定时删除 {len(removed)} 个过期草稿: {removed}", flush=True)


class JyComposeBody(BaseModel):
    template: str
    clips_folder: str = ""       # 空 = 去重导出文件夹
    out_prefix: str = "混剪"
    count: int = 1
    mode: str = "count"          # count 固定N条 / duration 按时长
    n_clips: int = 5
    target_sec: int = 60
    speed_min: float = 0.9
    speed_max: float = 1.0


async def _jy_compose_bg(body: "JyComposeBody"):
    JY.update(running=True, status="开始…", made=[], error="")
    try:
        folder = body.clips_folder or str(_dedup_out_dir())
        made = await asyncio.to_thread(
            jianying.compose_batch, body.template, folder, body.out_prefix,
            body.count, body.mode, body.n_clips, body.target_sec,
            (body.speed_min, body.speed_max), lambda m: JY.update(status=m))
        _mark_jy_gen(made, "mix")                # 记进"本工具生成"，供定时删除
        JY.update(made=made, status=f"完成，生成 {len(made)} 个草稿（打开剪映查看）")
    except Exception as e:
        JY.update(error=str(e)[:200], status=f"出错：{str(e)[:120]}")
        log_err(f"剪映混剪出错: {e}")
    finally:
        JY["running"] = False


@app.post("/api/jy/compose")
async def api_jy_compose(body: JyComposeBody):
    if JY["running"]:
        return {"ok": True, "already": True}
    if not jianying.find_install_dir():
        return JSONResponse({"error": "没检测到剪映专业版，请先装剪映"}, status_code=400)
    asyncio.create_task(_jy_compose_bg(body))
    return {"ok": True}


@app.get("/api/jy/status")
def api_jy_status():
    return JY


# ---------------- 做剪映模版：读基准草稿的效果层 + 勾选保留生成新模版 ----------------
@app.get("/api/jy/template_layers")
def api_jy_template_layers(name: str):
    try:
        return {"ok": True, **jianying.template_layers(name)}
    except Exception as e:
        return JSONResponse({"error": f"读取模板失败：{str(e)[:150]}"}, status_code=400)


@app.get("/api/jy/template_params")
def api_jy_template_params(name: str):
    """深度读取：每层视频的透明度/缩放/位移/音量/蒙版参数、特效强度、滤镜值、调整各项、BGM。"""
    try:
        return {"ok": True, **jianying.template_params(name)}
    except Exception as e:
        log_err(f"读模板参数失败: {e}")
        return JSONResponse({"error": f"读取失败：{str(e)[:150]}"}, status_code=400)


class JyCompose2Body(BaseModel):
    """cfg.layers[轨道下标] = {action: main/overlay/keep/drop, alpha:[lo,hi], scale:[lo,hi],
    tx/ty:[lo,hi], volume:[lo,hi], flip_prob:0~1, mask_on:bool, mask:{width/feather/centerX/centerY:[lo,hi]},
    value:[lo,hi](特效强度/滤镜), params:{brightness:[lo,hi],...}(调整)}
    cfg.extra_overlays = {count:N, alpha/scale/tx/ty/mask 同上} —— 次轨道加几层视频
    cfg.bgm = {enable:bool, volume:[lo,hi]} —— 素材库音乐随机选
    cfg.speed=[lo,hi] cfg.n_clips/mode/target_sec 同老混剪; cfg.library=素材库文件夹"""
    template: str
    out_prefix: str = "去重"
    count: int = 1
    cfg: dict = {}


async def _jy_compose2_bg(body: "JyCompose2Body"):
    JY.update(running=True, status="开始…", made=[], error="")
    try:
        made = await asyncio.to_thread(
            jianying.compose_batch_v2, body.template, body.cfg, body.out_prefix,
            body.count, lambda m: JY.update(status=m))
        _mark_jy_gen(made, "tpl")                # 记进"本工具生成"清单
        JY.update(made=made, status=f"完成，生成 {len(made)} 个草稿（打开剪映查看）")
    except Exception as e:
        JY.update(error=str(e)[:200], status=f"出错：{str(e)[:120]}")
        log_err(f"剪映模版生成出错: {e}")
    finally:
        JY["running"] = False


class JyOutDirBody(BaseModel):
    path: str = ""


@app.get("/api/jy/output_dir")
def api_jy_output_dir_get():
    return {"path": config.get("jy_output_dir", "")}


@app.post("/api/jy/output_dir")
def api_jy_output_dir_set(body: JyOutDirBody):
    """设置"生成输出文件夹"：你在剪映把成片导出到这里，视频号发布会从这里读。"""
    p = (body.path or "").strip()
    if p:
        try:
            Path(p).mkdir(parents=True, exist_ok=True)
        except Exception as e:
            return JSONResponse({"error": f"路径不可用：{e}"}, status_code=400)
    config["jy_output_dir"] = p
    _save(CONFIG_FILE, config)
    return {"ok": True, "path": p}


class JyAutoDelBody(BaseModel):
    enable: bool = False
    hours: float = 1


@app.get("/api/jy/autodel")
def api_jy_autodel_get():
    ad = config.get("jy_autodel") or {"enable": False, "hours": 1}
    pending = len(config.get("jy_gen", []))
    return {**ad, "pending": pending}


@app.post("/api/jy/autodel")
def api_jy_autodel_set(body: JyAutoDelBody):
    config["jy_autodel"] = {"enable": bool(body.enable), "hours": max(0.1, float(body.hours or 1))}
    _save(CONFIG_FILE, config)
    return {"ok": True, **config["jy_autodel"]}


@app.post("/api/jy/purge_now")
def api_jy_purge_now():
    """立即删除所有本工具生成的草稿（手动清理）。"""
    gen = config.get("jy_gen", [])
    removed = []
    for g in list(gen):
        try:
            jianying.delete_draft(g["name"])
            removed.append(g["name"])
        except Exception as e:
            log_err(f"手动删草稿 {g.get('name')} 失败: {e}")
    config["jy_gen"] = []
    _save(CONFIG_FILE, config)
    return {"ok": True, "removed": len(removed)}


@app.post("/api/jy/compose2")
async def api_jy_compose2(body: JyCompose2Body):
    if JY["running"]:
        return {"ok": True, "already": True}
    if not jianying.find_install_dir():
        return JSONResponse({"error": "没检测到剪映专业版，请先装剪映"}, status_code=400)
    if not (body.cfg.get("library") or "").strip():
        return JSONResponse({"error": "先填素材库文件夹"}, status_code=400)
    asyncio.create_task(_jy_compose2_bg(body))
    return {"ok": True}


class JyMakeTplBody(BaseModel):
    base: str                    # 基准草稿（你在剪映里手搭好的）
    out_name: str                # 新模版名
    drop_indexes: list[int] = [] # 要去掉的效果层轨道下标（没勾选保留的）


@app.post("/api/jy/make_template")
def api_jy_make_template(body: JyMakeTplBody):
    if not jianying.find_install_dir():
        return JSONResponse({"error": "没检测到剪映专业版，请先装剪映"}, status_code=400)
    if not (body.out_name or "").strip():
        return JSONResponse({"error": "请填模版名"}, status_code=400)
    try:
        name = jianying.make_template(body.base, body.out_name.strip(), body.drop_indexes)
        return {"ok": True, "name": name}
    except Exception as e:
        log_err(f"做剪映模版出错: {e}")
        return JSONResponse({"error": f"生成失败：{str(e)[:150]}"}, status_code=400)


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
