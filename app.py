# -*- coding: utf-8 -*-
"""抖音博主更新监控 — 定时检查博主新作品，弹窗提醒 + 自动下载无水印视频 + 本地面板"""
import asyncio
import json
import base64
import hashlib
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
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse, StreamingResponse, Response
from pydantic import BaseModel

import platforms
from platforms import get_adapter, PLATFORM_LIST
import channels
import dedup
import ffdedup
import jianying


async def resolve_aweme_id(text: str, platform: str = "douyin"):
    """从链接/口令/纯ID里解析出作品ID（走对应平台的适配器）"""
    return await get_adapter(platform).resolve_aweme_id(text)

# 打包后(PyInstaller)与源码运行的路径不同：
#   静态资源(static)在包内(_MEIPASS，只读)。
#   数据(data/downloads)【固定放 %LOCALAPPDATA%\爆款监控\】——不再放 exe 旁边。
#   这样以后换外壳(pywebview→Electron)、exe 换位置/自动更新，数据都在同一处、绝不丢
#   (尤其视频号登录 cookie 和账号档案)。老版本(数据在 exe 旁边)首次跑新版会自动迁移过来。
if getattr(sys, "frozen", False):
    BASE = Path(sys._MEIPASS)                 # 只读资源(static/version.json)
    _local = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    APP_DIR = Path(_local) / "爆款监控"
else:
    BASE = Path(__file__).parent
    APP_DIR = BASE


def _migrate_data_dir():
    """老版本把 data/downloads 存在 exe 旁边；新版改存 %LOCALAPPDATA%\\爆款监控。
    首次跑新版时把老位置的 data 原样搬过来(尤其视频号登录/账号档案)，绝不让用户重扫码。
    只在【新位置还没数据、老位置有数据】时迁一次；迁完不删老的(留底防意外)。"""
    if not getattr(sys, "frozen", False):
        return
    try:
        import shutil
        old_dir = Path(sys.executable).parent
        if old_dir.resolve() == APP_DIR.resolve():
            return
        old_data, new_data = old_dir / "data", APP_DIR / "data"
        if (new_data / "config.json").exists():        # 新位置已在用 → 不动
            return
        if not (old_data / "config.json").exists():     # 老位置也没数据 → 全新安装，不用迁
            return
        APP_DIR.mkdir(parents=True, exist_ok=True)
        # 逐文件复制 data/(个别文件被锁住也不影响整体；关键的 config/channels 一定过去)
        for root, _dirs, files in os.walk(old_data):
            rel = Path(root).relative_to(old_data)
            (new_data / rel).mkdir(parents=True, exist_ok=True)
            for f in files:
                dst = new_data / rel / f
                if dst.exists():
                    continue
                try:
                    shutil.copy2(Path(root) / f, dst)
                except Exception:
                    pass
    except Exception:
        pass


_migrate_data_dir()
DATA = APP_DIR / "data"
DL = APP_DIR / "downloads"
DATA.mkdir(parents=True, exist_ok=True)
DL.mkdir(parents=True, exist_ok=True)
CONFIG_FILE = DATA / "config.json"
STATE_FILE = DATA / "state.json"
PORT = 8790
def _read_version():
    """版本号统一从 version.json 读，避免和 GitHub 上的脱节(打包时 version.json 一并进包)。
    以前写死常量、bump 时忘改→exe 内部版本永远落后→死循环提示更新。改成读文件根治。
    Electron 安装版由外壳用环境变量 BAOKUAN_VER 传入自己的版本(和 pywebview 渠道的 version.json 分开)。"""
    ev = os.environ.get("BAOKUAN_VER")
    if ev:
        return str(ev).strip()
    try:
        import json as _json
        return str(_json.loads((BASE / "version.json").read_text(encoding="utf-8")).get("version", "1.0.16"))
    except Exception:
        return "1.0.16"


VERSION = _read_version()
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
# 迁移善后：老用户若用的是【默认下载目录(exe 旁边的 downloads)】，迁数据后把 download_dir
# 接到老位置，避免"下载库突然空了"（大文件不搬、只把路径接过去；只做一次）。
if getattr(sys, "frozen", False) and not (config.get("download_dir") or "").strip():
    _old_dl = Path(sys.executable).parent / "downloads"
    try:
        if _old_dl.exists() and _old_dl.resolve() != DL.resolve() and any(_old_dl.iterdir()):
            config["download_dir"] = str(_old_dl)
            _save(CONFIG_FILE, config)
    except Exception:
        pass
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
# 多账号并发上传：每个账号一条独立队列 + 一个独立工人协程（同账号内部串行、带随机间隔防封号），
# 不同账号并行；全局用一个信号量把「同时在传的账号数」压在 ch_max_concurrent（默认3）以内。
UP_ACCT_QUEUES: dict = {}     # aid -> asyncio.Queue
UP_ACCT_WORKERS: dict = {}    # aid -> asyncio.Task
UP_SEM = None                 # 全局并发闸；事件循环里懒创建
MAIN_LOOP = None              # 主事件循环句柄；同步端点(线程池)里派任务要用它 call_soon_threadsafe
_up_seq = 0
UP_TASKS_FILE = DATA / "upload_tasks.json"     # 任务记录持久化(重启不丢)


def _save_upload_tasks():
    try:
        items = list(UPLOAD_TASKS.values())[-500:]
        UP_TASKS_FILE.write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def _load_upload_tasks():
    global _up_seq
    try:
        if not UP_TASKS_FILE.exists():
            return
        for t in json.loads(UP_TASKS_FILE.read_text(encoding="utf-8")):
            if t.get("status") in ("queued", "running"):   # 上次没跑完的→回到排队,startup 自动续跑(断点续传)
                t["status"] = "queued"
                t["stage"] = "软件重启，排队续跑…"
                t["err"] = ""
            UPLOAD_TASKS[t["id"]] = t
            try:
                _up_seq = max(_up_seq, int(str(t["id"]).lstrip("u")))
            except Exception:
                pass
    except Exception:
        pass

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


async def download_file(url: str, dest: Path, min_bytes: int = 4096, retries: int = 2) -> bool:
    """下载到 dest。【完整性校验】CDN 偶尔返回 200 但空/半截 body → 产出0字节假文件，
    后面整条链路(去重0字节→发布超时)全被带崩。下完校验大小，太小删掉自动重试 retries 次。"""
    for attempt in range(retries + 1):
        tmp = dest.with_suffix(dest.suffix + ".part")
        try:
            async with httpx.AsyncClient(follow_redirects=True, timeout=300,
                                         headers={"User-Agent": UA, "Referer": "https://www.douyin.com/"}) as c:
                async with c.stream("GET", url) as r:
                    if r.status_code != 200:
                        log_err(f"下载HTTP{r.status_code} {dest.name}(第{attempt+1}次)")
                        await asyncio.sleep(1.5)
                        continue
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    with open(tmp, "wb") as f:
                        async for chunk in r.aiter_bytes(1 << 16):
                            f.write(chunk)
            sz = tmp.stat().st_size if tmp.exists() else 0
            if sz < min_bytes:                    # 空壳/半截 → 删掉重试
                try:
                    tmp.unlink()
                except OSError:
                    pass
                log_err(f"下载校验失败 {dest.name}: 只有{sz}字节，重试(第{attempt+1}次)")
                await asyncio.sleep(1.5)
                continue
            tmp.replace(dest)
            return True
        except Exception as e:
            log_err(f"下载失败 {dest.name}(第{attempt+1}次): {e}")
            try:
                if tmp.exists():
                    tmp.unlink()
            except OSError:
                pass
            await asyncio.sleep(1.5)
    return False


def notify(title: str, msg: str, icon: Path | None = None):
    if not config.get("notify_enabled", True):   # 一键关闭弹窗监控提醒
        return
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
            if await download_file(it["video_url"], vp, min_bytes=102400):   # 正片<100KB=必坏
                return True, f"/files/{vp.relative_to(dl).as_posix()}"
    return False, ""


_DL_TAGS_CACHE = {}   # {(platform,nickname): (expire_ts, tags)} —— /api/status 每10秒轮询,别每次都扫盘


def _downloaded_tags(nickname: str, platform: str = "douyin") -> set:
    """已下载作品的 id 尾6位集合。只认正片（.mp4 视频 / _NN.jpeg 图集），封面 .jpg 不算下载。
    【性能】前端每10秒轮询 /api/status,每次对每个博主扫一遍下载文件夹(iterdir+正则)——
    下载越多越慢。加 12s TTL 缓存;下载完成时主动清缓存(_dl_tags_invalidate),角标依旧即时。"""
    key = (platform, nickname)
    hit = _DL_TAGS_CACHE.get(key)
    if hit and hit[0] > time.time():
        return hit[1]
    folder = platform_dir(platform) / sanitize(nickname, 30)
    tags = set()
    if folder.exists():
        for f in folder.iterdir():
            m = re.search(r"_(\d{6})\.mp4$", f.name) or re.search(r"_(\d{6})_\d+\.jpe?g$", f.name)
            if m:
                tags.add(m.group(1))
    _DL_TAGS_CACHE[key] = (time.time() + 12, tags)
    return tags


def _dl_tags_invalidate():
    _DL_TAGS_CACHE.clear()


def _mix_episodes_done(nickname: str, mix_name: str, platform: str = "douyin") -> set:
    """某合集子文件夹里已下载的集数集合（文件名形如 1.mp4）—— 用于合集追更的"只下缺集"。"""
    folder = platform_dir(platform) / sanitize(nickname, 30) / sanitize(mix_name, 40)
    done = set()
    if folder.exists():
        for f in folder.iterdir():
            m = re.fullmatch(r"(\d+)\.mp4", f.name)
            if m:
                try:
                    if f.stat().st_size < 102400:   # 0字节/半截空壳≠已下载→追更时自动补下
                        continue
                except OSError:
                    continue
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


def _mix_raw(it: dict) -> dict:
    """作品自带的合集身份(不管有没有下载过都给)——发现页靠它把同一合集的多条折叠成一条。
    _mix_annot 只对下载过的合集返回,陌生合集拿不到 mix_id,所以单独给一份裸数据。"""
    m = it.get("mix") or {}
    if not m.get("mix_id"):
        return {"mix_id": "", "mix_name": "", "mix_total": 0, "episode": 0}
    return {"mix_id": str(m.get("mix_id") or ""), "mix_name": m.get("mix_name") or "",
            "mix_total": _to_int(m.get("total")) or 0, "episode": _to_int(it.get("episode")) or 0}


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
    global MAIN_LOOP
    MAIN_LOOP = asyncio.get_running_loop()   # 记下主循环：同步端点入队时靠它调度回来
    _load_upload_tasks()                     # 视频号发布任务记录：重启恢复
    _load_fd_tasks()                         # 去重任务记录：重启恢复
    asyncio.create_task(monitor_loop())
    for _ in range(2):                       # 两个下载工人：并发温和，防限流
        asyncio.create_task(_dl_worker())
    # 视频号上传：多账号并发（每账号一个工人，全局≤ch_max_concurrent）。重启恢复未发完的任务
    for _tid in [k for k, t in UPLOAD_TASKS.items() if t.get("status") == "queued"]:
        _enqueue_upload(_tid)
    asyncio.create_task(_ch_keepalive_loop())  # 视频号登录保活：每4小时静默续期一次cookie
    asyncio.create_task(_dedup_worker())     # 视频去重：单工人（ffmpeg 吃 CPU）
    _fd_resume_pending()                     # 去重导出：重启自动续跑上次没完成的批次
    # 【清理】f2 抓取日志会无限堆积(一天好几个文件),启动时删 7 天前的
    try:
        cutoff = time.time() - 7 * 86400
        for lf in (APP_DIR / "logs").glob("*.log*"):
            if lf.is_file() and lf.stat().st_mtime < cutoff:
                lf.unlink(missing_ok=True)
    except Exception:
        pass


@app.get("/", response_class=HTMLResponse)
def index():
    html = (BASE / "static" / "index.html").read_text(encoding="utf-8")
    # 绝不缓存首页（改完前端立即生效，避免 Electron 用旧缓存）
    return HTMLResponse(html, headers={"Cache-Control": "no-store, no-cache, must-revalidate",
                                       "Pragma": "no-cache", "Expires": "0"})


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
                if ok:
                    _dl_tags_invalidate()   # 下载落盘→清"已下载"缓存,角标即时变绿
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
    top: int = 50             # 排序后取前 N（用户可在发现页自定义）
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
                        **_mix_raw(it),   # 发现页折叠合集用(mix_id/mix_name/mix_total/episode)
                        "is_monitored": any(b["sec_user_id"] == it["author_id"]
                                            and b.get("platform", "douyin") == platform
                                            for b in config["bloggers"]),
                        **rank_metrics(it, age_h, _to_int(it.get("author_follower"))),
                    }
        except Exception as e:
            log_err(f"发现：抓取相关推荐失败 {e}")

    items = sorted(found.values(), key=lambda x: x["digg"], reverse=True)
    top = max(1, min(500, body.top or 50))   # 取前 N：用户可自定义（1~500）
    items = items[:top]
    return {"scanned": scanned, "hours": body.hours, "min_like": body.min_like,
            "count": len(items), "items": items}


class LibrarySearchBody(BaseModel):
    hours: int = 24
    min_like: int = 20000
    scan: int = 20   # 免登录每个博主最多约20条可见，翻页无效，不做无谓翻页
    top: int = 50    # 按点赞排序后取前 N（用户可在发现页自定义）
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
                **_mix_raw(it),   # 发现页折叠合集用(mix_id/mix_name/mix_total/episode)
                "is_monitored": True,
                **rank_metrics(it, age_h, follower),
            })
        return out

    results = await asyncio.gather(*[one(b) for b in bloggers])
    items = [it for sub in results for it in sub]
    items.sort(key=lambda x: x["digg"], reverse=True)
    total = len(items)
    top = max(1, min(500, body.top or 50))   # 取前 N：用户可自定义（1~500）
    items = items[:top]
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
            elif f.is_file() and not _is_incomplete(f.name) \
                    and f.suffix.lower() != ".jpg":     # .jpg=监控缩略图，不进下载库（图集是 .jpeg 照留）
                files.append(f)
        files.sort(key=_ep_key) if is_mix else \
            files.sort(key=lambda p: p.stat().st_mtime, reverse=True)   # 合集按集数，其余按时间
        items = [{"name": f.name, "url": f"/files/{f.relative_to(base).as_posix()}",
                  "size": round(f.stat().st_size / 1024 / 1024, 2),
                  "mtime": f.stat().st_mtime,
                  "is_video": f.suffix.lower() == ".mp4"} for f in files]
        if items:
            mt = max((it["mtime"] for it in items), default=0)
            groups.append({"blogger": label, "count": len(items), "files": items, "is_mix": is_mix,
                           "mtime": mt, "rel": folder.relative_to(root).as_posix()})
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
    if not _ch_state(aid).exists():
        return False
    acc = _ch_account(aid)
    return not (acc and acc.get("session_bad"))    # 被动校验:检测到失效就不算在线


def _mark_session(aid: str, ok: bool):
    """任何操作发现登录态死/活时更新标记（被动校验，不额外开浏览器）。"""
    acc = _ch_account(aid)
    if acc and bool(acc.get("session_bad")) == ok:   # 状态有变才写盘
        acc["session_bad"] = not ok
        _save(CONFIG_FILE, config)


async def _ch_keepalive_loop():
    """视频号登录保活：每4小时用各账号的持久档案静默访问一次后台（离屏无窗口），
    cookie 被服务端续期 → 登录态长期有效（100小时+）。只报活不报死：
    偶发网络失败不把账号误标下线（真死了会在下次发布/拉列表时被标）。"""
    while True:
        await asyncio.sleep(4 * 3600)
        for a in list(_ch_accounts()):
            aid = a.get("id")
            if not aid or not _ch_state(aid).exists():
                continue
            try:
                ok = await channels.check_login(_ch_state(aid))
                if ok:
                    _mark_session(aid, True)
            except Exception:
                pass
            await asyncio.sleep(30)     # 账号间隔开，别同时开一堆浏览器


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
                                   "avatar": info.get("avatar", False), "session_bad": False,
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
                acc["session_bad"] = False          # 重新登录成功→清失效标记
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


class SaveStateBody(BaseModel):
    cookies: list = []              # [{name,value,domain,path,secure,httpOnly,sameSite,expires}]
    localStorage: list = []         # [[name,value], ...] 或 [{name,value}]


@app.post("/api/channels/account/{aid}/check_login")
async def api_ch_check_login(aid: str):
    """真机校验登录态是否有效（开无头浏览器，看会不会被踢到 login 页）。"""
    if not _ch_account(aid):
        return JSONResponse({"error": "账号不存在"}, status_code=404)
    if not _ch_state(aid).exists():
        return {"ok": True, "valid": False}
    try:
        valid = await channels.check_login(_ch_state(aid))
    except Exception as e:
        return {"ok": True, "valid": False, "error": str(e)[:120]}
    return {"ok": True, "valid": bool(valid)}


@app.post("/api/channels/account/{aid}/save_state")
def api_ch_save_state(aid: str, body: SaveStateBody):
    """把 webview 后台分区里同步出来的 cookie+localStorage 写成 Playwright storage_state 文件，
    这样发布/剧集(fetch_lists/upload)就复用 webview 的登录态，两套登录统一。"""
    if not _ch_account(aid):
        return JSONResponse({"error": "账号不存在"}, status_code=404)
    _by = {}
    for c in (body.cookies or []):
        if not c.get("name"):
            continue
        _by[(c["name"], c.get("domain", ".weixin.qq.com"))] = {   # 按(名,域)去重,保留最后一个
            "name": c["name"], "value": c.get("value", ""),
            "domain": c.get("domain", ".weixin.qq.com"), "path": c.get("path", "/"),
            "expires": c.get("expires", -1), "httpOnly": bool(c.get("httpOnly")),
            "secure": bool(c.get("secure")), "sameSite": c.get("sameSite", "None"),
        }
    cks = list(_by.values())
    ls = []
    for kv in (body.localStorage or []):
        if isinstance(kv, dict):
            ls.append({"name": kv.get("name", ""), "value": kv.get("value", "")})
        elif isinstance(kv, (list, tuple)) and len(kv) >= 2:
            ls.append({"name": kv[0], "value": kv[1]})
    has_sess = any(c["name"] in ("sessionid", "wxuin") for c in cks)
    if not has_sess:
        return JSONResponse({"error": "后台还没登录成功（没读到 sessionid），先在后台扫码登录进去再同步"}, status_code=400)
    state = {"cookies": cks, "origins": [
        {"origin": "https://channels.weixin.qq.com", "localStorage": ls}]}
    _ch_state(aid).parent.mkdir(parents=True, exist_ok=True)
    _ch_state(aid).write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    _mark_session(aid, True)     # 从已登录的 webview 同步到 sessionid = 登录有效 → 标在线(不用再开浏览器校验)
    return {"ok": True, "cookies": len(cks), "localStorage": len(ls), "online": True}


CH_LISTS = {"running": False, "data": {}, "for": ""}


def _ch_lists_cache_file(aid: str) -> Path:
    return CH_DIR / f"{aid}_lists_cache.json"


async def _ch_fetch_lists_bg(aid: str):
    CH_LISTS.update({"running": True, "for": aid, "data": {}})
    try:
        data = await channels.fetch_lists(_ch_state(aid))
        CH_LISTS["data"] = data
        _mark_session(aid, not data.get("logged_out"))   # 被动校验
        # 拉到了就缓存下来：下次打开秒显缓存(后台再刷)，不用每次干等十几秒
        if not data.get("logged_out") and (data.get("dramas") or data.get("collections")):
            try:
                _ch_lists_cache_file(aid).write_text(json.dumps(
                    {"dramas": data.get("dramas", []), "collections": data.get("collections", []),
                     "activities": data.get("activities", []), "ts": datetime.now().strftime("%Y-%m-%d %H:%M")},
                    ensure_ascii=False), encoding="utf-8")
            except Exception:
                pass
    except Exception as e:
        log_err(f"拉取视频号列表失败: {e}")
        CH_LISTS["data"] = {"error": str(e)[:120]}
    finally:
        CH_LISTS["running"] = False


class DramasCacheBody(BaseModel):
    dramas: list = []


@app.post("/api/channels/account/{aid}/cache_dramas")
def api_ch_cache_dramas(aid: str, body: DramasCacheBody):
    """前端从内嵌 webview 的活会话里直接拉到剧集后，POST 过来存缓存（顺带标记登录有效）。"""
    try:
        f = _ch_lists_cache_file(aid)
        d = {}
        if f.exists():
            try:
                d = json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                d = {}
        d["dramas"] = body.dramas or []
        d["ts"] = datetime.now().strftime("%Y-%m-%d %H:%M")
        f.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
        _mark_session(aid, True)                 # webview 能拉到剧集 = 登录态有效
        return {"ok": True, "count": len(d["dramas"])}
    except Exception as e:
        return JSONResponse({"error": str(e)[:100]}, status_code=400)


@app.get("/api/channels/account/{aid}/lists_cache")
def api_ch_lists_cache(aid: str):
    """秒返回上次拉到的剧集/合集缓存(供打开选剧弹窗时立刻显示，同时后台再刷新)。"""
    try:
        f = _ch_lists_cache_file(aid)
        if f.exists():
            d = json.loads(f.read_text(encoding="utf-8"))
            return {"cached": True, "dramas": d.get("dramas", []), "collections": d.get("collections", []),
                    "activities": d.get("activities", []), "ts": d.get("ts", "")}
    except Exception:
        pass
    return {"cached": False, "dramas": [], "collections": [], "activities": []}


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


@app.post("/api/channels/account/{aid}/open_backend")
async def api_ch_open_backend(aid: str):
    """打开这个账号的视频号后台（可见浏览器，用它的登录态）。"""
    if not _ch_account(aid):
        return JSONResponse({"error": "账号不存在"}, status_code=404)
    if not ch_online(aid):
        return JSONResponse({"error": "该账号离线，请先重新扫码登录"}, status_code=400)
    asyncio.create_task(channels.open_backend(_ch_state(aid)))
    return {"ok": True}


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
    counts = {k: 0 for k in ("queued", "running", "done", "failed", "cancelled")}
    for t in UPLOAD_TASKS.values():
        counts[t["status"]] = counts.get(t["status"], 0) + 1
    tasks = list(UPLOAD_TASKS.values())[-500:]
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
            if not f.is_file() or _is_incomplete(f.name) or str(f) in seen:
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
                       capture_output=True, timeout=40, creationflags=dedup.NOWIN)
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


@app.get("/api/channels/thumb")
def api_ch_thumb(path: str):
    """视频缩略图（首帧，带缓存）。发布卡片直接 <img src> 用，懒加载不阻塞。"""
    v = Path(path)
    if not v.exists() or v.suffix.lower() not in _VIDEO_EXTS:
        return JSONResponse({"error": "视频不存在"}, status_code=404)
    THUMBS_DIR = COVERS_DIR / "_thumbs"
    THUMBS_DIR.mkdir(parents=True, exist_ok=True)
    key = hashlib.md5(f"{v}|{v.stat().st_mtime_ns}".encode("utf-8")).hexdigest()
    tp = THUMBS_DIR / f"{key}.jpg"
    if not tp.exists():
        try:
            subprocess.run([dedup.FF, "-y", "-ss", "0.5", "-i", str(v),
                            "-frames:v", "1", "-vf", "scale=240:-1", "-q:v", "4", str(tp)],
                           capture_output=True, timeout=40, creationflags=dedup.NOWIN)
        except Exception as e:
            log_err(f"缩略图失败: {e}")
    if not tp.exists():
        return JSONResponse({"error": "抽帧失败"}, status_code=404)
    return FileResponse(str(tp))


_VIDEO_EXTS = (".mp4", ".mov", ".mkv", ".avi", ".webm", ".flv", ".m4v")


def _is_incomplete(name: str) -> bool:
    """去重/下载正在渲染的半成品：ffdedup 写 X.part.mp4，下载写 X.mp4.part。
    两种都要挡在列表外，别让用户挑到没渲染完的坏文件（如第15集.part.mp4）。"""
    n = name.lower()
    return n.endswith(".part") or ".part." in n


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
                if f.is_file() and f.suffix.lower() in _VIDEO_EXTS and not _is_incomplete(f.name)]
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
    ch_drama: str = ""               # 扩展链接·视频号剧集(exportId 或剧名)
    ch_drama_title: str = ""         # 剧集显示名(前端一起带,注入 component 的 title 用真剧名而非 exportId)
    ch_activity: str = ""            # 活动


def _plan_upload(body: "UploadBody"):
    """算出分发计划：[(account_id, name, item, publish_at)]。不入队，供预览/发布共用。"""
    online = [a["id"] for a in _ch_accounts() if ch_online(a["id"])]
    targets = [a for a in body.account_ids if a in online] if body.account_ids else online
    items = [it for it in body.items if Path(it.video_path).exists()]
    if body.shuffle:
        items = items[:]
        random.shuffle(items)
    else:
        # 【按集数顺序发布】第1-2集最先发。按(所属剧文件夹, 集数)自然排序；没勾打乱才排。
        def _ep_key(it):
            base = Path(it.video_path).stem
            m = (re.search(r"第\s*(\d+)", base) or re.match(r"(\d+)", base)
                 or re.search(r"(\d+)", base))
            return (Path(it.video_path).parent.name, int(m.group(1)) if m else 10**9, base)
        items = sorted(items, key=_ep_key)
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
    elif body.distribute == "folder":           # 按文件夹一对一：剧文件夹1→账号1、文件夹2→账号2…
        folders, fmap = [], {}
        for it in items:                        # 文件夹按 items 顺序(已按剧名+集数排)首次出现记序
            fn = Path(it.video_path).parent.name
            if fn not in fmap:
                fmap[fn] = len(folders)
                folders.append(fn)
        slot_of = {}                            # 每账号内的第几条(排作品间隔用)
        for it in items:
            ai = fmap[Path(it.video_path).parent.name] % A    # 文件夹多于账号→轮回分派
            aid = targets[ai]
            ci = slot_of.get(aid, 0)
            slot_of[aid] = ci + 1
            at = base + timedelta(seconds=ai * body.acct_gap_sec + ci * body.clip_gap_sec)
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


# ---- 发布前敏感词检查：踩词会白传一遍视频才被拒，发前本地过一遍省时间 ----
_SENS_FILE = DATA / "sensitive_words.txt"          # 一行一个词，用户可自己加
_SENS_DEFAULT = [
    # 广告法极限词(视频号常拒)
    "最便宜", "全网第一", "第一名", "国家级", "世界级", "顶级", "绝对", "史上最",
    "最优秀", "最先进", "100%有效", "无副作用", "根治", "治愈", "药到病除", "秒杀全网",
    # 导流/交易类(高危)
    "加微信", "加V", "加v", "私聊我", "加QQ", "加群", "返利", "刷单", "兼职赚钱",
    "日赚", "月入过万", "贷款", "代开发票", "博彩", "赌博", "彩票内幕",
    # 违禁内容
    "色情", "裸聊", "约炮", "一夜情", "毒品", "枪支", "爆炸物", "代孕", "办证",
]


def _load_sens_words():
    words = list(_SENS_DEFAULT)
    try:
        if _SENS_FILE.exists():
            for ln in _SENS_FILE.read_text(encoding="utf-8").splitlines():
                ln = ln.strip()
                if ln and not ln.startswith("#"):
                    words.append(ln)
    except Exception:
        pass
    return words


def _check_sensitive(items) -> list:
    """检查各条视频的标题/简介/话题，返回 ['视频名: 命中「词」', ...]"""
    words = _load_sens_words()
    hits = []
    for it in items:
        text = " ".join([it.title or "", it.desc or ""] + [str(t) for t in (it.tags or [])])
        bad = [w for w in words if w and w in text]
        if bad:
            hits.append(f"{Path(it.video_path).stem}: 命中敏感词 {('「' + '」「'.join(bad[:5]) + '」')}")
    return hits


@app.post("/api/channels/upload")
def api_channels_upload(body: UploadBody):
    targets, plan = _plan_upload(body)
    if not targets:
        return JSONResponse({"error": "没有在线的视频号账号，请先添加/登录账号"}, status_code=400)
    if not plan:
        return JSONResponse({"error": "没有有效视频"}, status_code=400)
    sens = _check_sensitive(body.items)
    if sens:
        return JSONResponse({"error": "发布前敏感词检查未通过（踩词会白传视频才被拒）：\n"
                            + "\n".join(sens[:8])
                            + "\n改掉标题/简介/话题里的词再发；专业术语误报可精简 data/sensitive_words.txt"},
                            status_code=400)
    global _up_seq
    batch = datetime.now().strftime("批次 %m-%d %H:%M:%S")     # 一次「一键分发」= 一个批次
    for aid, aname, it, at in plan:
        sched_str = at.strftime("%Y-%m-%d %H:%M") if body.time_mode == "schedule" else ""
        # @视频号 直接拼进简介（这个不用视频号选择器，纯文本）
        desc = it.desc
        if body.ch_at:
            at_str = "@" + body.ch_at.lstrip("@")
            desc = (at_str + " " + desc) if body.ch_at_pos == "head" else (desc + " " + at_str).strip()
        _up_seq += 1
        tid = f"u{_up_seq}"
        acc = _ch_account(aid) or {}
        try:
            size_mb = round(Path(it.video_path).stat().st_size / 1024 / 1024, 1)
        except Exception:
            size_mb = 0
        UPLOAD_TASKS[tid] = {"id": tid, "account_id": aid, "account_name": aname,
                             "account_wxid": acc.get("wxid", ""),
                             "title": it.title or Path(it.video_path).stem,
                             "video_path": it.video_path, "tags": it.tags, "desc": desc,
                             "cover_path": it.cover_path, "link": it.link, "original": it.original,
                             "statement": body.ch_statement, "location": body.ch_location,
                             "collection": body.ch_collection, "drama": body.ch_drama,
                             "drama_title": body.ch_drama_title,
                             "activity": body.ch_activity,
                             "schedule": sched_str,
                             "publish_at": at.timestamp() if body.time_mode == "local" else 0,
                             "gap_base": int(body.clip_gap_sec or 0),   # 选的作品间隔(秒)→worker按此随机化条间延迟
                             "when": at.strftime("%m-%d %H:%M") if body.time_mode != "now" else "",
                             "status": "queued", "stage": "", "err": "",
                             "created": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                             "exec_at": "", "done_at": "", "elapsed": "",
                             "size_mb": size_mb, "batch": batch, "vtype": "视频",
                             "ts": datetime.now().strftime("%H:%M:%S")}
        _enqueue_upload(tid)
    _save_upload_tasks()
    return {"ok": True, "queued": len(plan), "accounts": len(targets)}


# ---------------- 矩阵分发：手动把「剧」分配给「账号」(账号1发这几部、账号2发另几部) ----------------
class MatrixAssign(BaseModel):
    account_id: str
    folders: list[str] = []          # 该账号要发的剧文件夹名(输出目录下一级子文件夹)
    dramas: dict = {}                # {文件夹名: 剧集exportId} 每部剧关联的视频号剧集(可选)
    drama_titles: dict = {}          # {文件夹名: 剧名}


class MatrixBody(BaseModel):
    assigns: list[MatrixAssign] = []
    root: str = ""                   # 剧文件夹根,默认去重成品输出目录
    title: str = ""                  # 标题模板(空=用文件名)
    tags: list[str] = []
    desc: str = ""
    per_day: int = 0                 # 每个号一天最多发几条(0=不限,全部立即排队);超出的滚到后续天
    start_time: str = ""             # 排期起始 "YYYY-MM-DD HH:MM"(空=现在)
    clip_gap_sec: int = 0            # 同号相邻两条的间隔秒(防频控)


def _folder_vids_sorted(root: Path, folder: str):
    """列一个剧文件夹下的视频，按集数升序(第1集..第N集)，排除 .part 半成品。"""
    d = root / folder
    if not d.exists() or not d.is_dir():
        return []
    vids = [f for f in d.rglob("*.mp4") if f.is_file() and not _is_incomplete(f.name)]

    def _epk(f):
        b = f.stem
        m = re.search(r"第\s*(\d+)", b) or re.match(r"(\d+)", b) or re.search(r"(\d+)", b)
        return (int(m.group(1)) if m else 10 ** 9, b)
    vids.sort(key=_epk)
    return vids


def _matrix_root(explicit: str = "") -> Path:
    """矩阵分发的剧文件夹根：和「一键去重导出」输出目录一致(fd_out→jy_output_dir→默认)，
    否则默认路径可能是空的、列不出剧。"""
    if explicit:
        return Path(explicit)
    fc = config.get("ffdedup_cfg") or {}
    return Path((fc.get("fd_out") or "").strip()
                or (config.get("jy_output_dir") or "").strip()
                or str(_dedup_out_dir()))


def _matrix_build(body: "MatrixBody"):
    """把分配表展开成发布计划：[(account_id, video_path, folder, drama, drama_title)]。
    已发成功过的(视频×账号)自动跳过(幂等)。"""
    root = _matrix_root(body.root)
    plan, skipped = [], 0
    for a in body.assigns:
        if not a.account_id:
            continue
        for folder in (a.folders or []):
            for v in _folder_vids_sorted(root, folder):
                vp = str(v)
                if _already_done(a.account_id, vp):
                    skipped += 1
                    continue
                plan.append((a.account_id, vp, folder,
                             (a.dramas or {}).get(folder, ""),
                             (a.drama_titles or {}).get(folder, "")))
    return root, plan, skipped


@app.get("/api/channels/matrix/folders")
def api_ch_matrix_folders(root: str = ""):
    """列出可分配的剧文件夹(去重成品输出目录下的一级子文件夹)+每个的集数。"""
    base = _matrix_root(root)
    out = []
    if base.exists():
        for d in sorted([x for x in base.iterdir() if x.is_dir()], key=lambda p: p.name):
            n = len([f for f in d.rglob("*.mp4") if f.is_file() and not _is_incomplete(f.name)])
            if n:
                out.append({"folder": d.name, "count": n})
    return {"root": str(base), "folders": out}


@app.post("/api/channels/matrix/preview")
def api_ch_matrix_preview(body: MatrixBody):
    """预览：每个账号发哪几部剧、共多少条、跳过多少(已发过的)。发前核对用。"""
    root, plan, skipped = _matrix_build(body)
    online = {a["id"] for a in _ch_accounts() if ch_online(a["id"])}
    by_acct = {}
    for aid, vp, folder, drama, dtitle in plan:
        d = by_acct.setdefault(aid, {"account_id": aid,
                                     "account_name": (_ch_account(aid) or {}).get("name", aid),
                                     "online": aid in online, "count": 0, "folders": {}})
        d["count"] += 1
        d["folders"][folder] = d["folders"].get(folder, 0) + 1
    return {"total": len(plan), "skipped": skipped, "root": str(root),
            "accounts": list(by_acct.values())}


@app.post("/api/channels/matrix/upload")
def api_ch_matrix_upload(body: MatrixBody):
    """执行：按分配表建发布任务并入队。每账号独立 worker 并行发；已发过的自动跳过。"""
    global _up_seq
    root, plan, skipped = _matrix_build(body)
    if not plan:
        return {"ok": True, "queued": 0, "skipped": skipped,
                "note": "没有可发的(可能都已发过、或所选文件夹为空)"}
    per_day = max(0, int(body.per_day or 0))
    gap = max(0, int(body.clip_gap_sec or 0))
    base = datetime.now()
    if body.start_time:
        try:
            base = datetime.strptime(body.start_time, "%Y-%m-%d %H:%M")
        except Exception:
            pass
    timed = per_day > 0 or gap > 0 or bool(body.start_time)   # 要按时间排期(本机定时到点发)
    batch = datetime.now().strftime("矩阵 %m-%d %H:%M:%S")
    now_s = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    hm = datetime.now().strftime("%H:%M:%S")
    # 按账号分组，每账号内按顺序排期：每天最多 per_day 条，超出滚到后续天
    by_acct = {}
    for item in plan:
        by_acct.setdefault(item[0], []).append(item)
    n, max_day = 0, 0
    for aid, items in by_acct.items():
        acc = _ch_account(aid) or {}
        for k, (aid2, vp, folder, drama, dtitle) in enumerate(items):
            day, slot = (k // per_day, k % per_day) if per_day > 0 else (0, k)
            max_day = max(max_day, day)
            at = base + timedelta(days=day, seconds=slot * gap)
            publish_at = at.timestamp() if timed else 0
            when = at.strftime("%m-%d %H:%M") if timed else ""
            _up_seq += 1
            tid = f"u{_up_seq}"
            try:
                size_mb = round(Path(vp).stat().st_size / 1024 / 1024, 1)
            except Exception:
                size_mb = 0
            UPLOAD_TASKS[tid] = {"id": tid, "account_id": aid, "account_name": acc.get("name", aid),
                                 "account_wxid": acc.get("wxid", ""),
                                 "title": body.title or Path(vp).stem,
                                 "video_path": vp, "tags": body.tags, "desc": body.desc,
                                 "cover_path": "", "link": "", "original": False,
                                 "statement": "", "location": "", "collection": "",
                                 "drama": drama, "drama_title": dtitle, "activity": "",
                                 "schedule": "", "publish_at": publish_at, "when": when,
                                 "gap_base": gap,   # 选的作品间隔(秒)→worker按此随机化条间延迟
                                 "status": "queued", "stage": "", "err": "",
                                 "created": now_s, "exec_at": "", "done_at": "", "elapsed": "",
                                 "size_mb": size_mb, "batch": batch, "vtype": "视频", "ts": hm}
            _enqueue_upload(tid)
            n += 1
    _save_upload_tasks()
    return {"ok": True, "queued": n, "skipped": skipped, "accounts": len(by_acct), "days": max_day + 1}


class PubSettingsBody(BaseModel):
    max_concurrent: int = 3
    show_browser: bool | None = None


# ---- 发布后数据回查：拉各账号已发作品的播放/点赞，连续0播=限流预警 ----
CH_STATS_FILE = DATA / "channels_stats.json"


def _load_ch_stats() -> dict:
    try:
        return json.loads(CH_STATS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _stats_verdict(posts: list) -> dict:
    """限流判定：最近3条(发布>2小时的)全是0播放 → 预警。"""
    now = time.time()
    mature = [p for p in posts if p.get("create_time") and now - p["create_time"] > 7200]
    recent = mature[:3]
    warn = bool(recent) and len(recent) >= 2 and all((p.get("read") or 0) == 0 for p in recent)
    total_read = sum(p.get("read") or 0 for p in posts)
    return {"warn": warn,
            "warn_msg": "最近发布的作品播放全为0，账号可能被限流" if warn else "",
            "total_read": total_read, "post_count": len(posts)}


@app.get("/api/channels/stats")
async def api_channels_stats(refresh: int = 0, account_id: str = ""):
    """数据回查。refresh=1 现场拉取(每账号开一次后台浏览器,较慢)；否则回缓存。"""
    stats = _load_ch_stats()
    if refresh:
        accts = [a for a in _ch_accounts()
                 if (not account_id or a["id"] == account_id) and ch_online(a["id"])]
        for a in accts:
            r = await channels.fetch_post_stats(_ch_state(a["id"]))
            if r.get("ok"):
                stats[a["id"]] = {"ts": datetime.now().strftime("%Y-%m-%d %H:%M"),
                                  "name": a["name"], "posts": r["posts"],
                                  **_stats_verdict(r["posts"])}
            else:
                stats.setdefault(a["id"], {})["err"] = r.get("err", "")
                stats[a["id"]]["name"] = a["name"]
        try:
            CH_STATS_FILE.write_text(json.dumps(stats, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass
    return {"stats": stats}


@app.get("/api/channels/pubsettings")
def api_ch_pubsettings_get():
    return {"max_concurrent": int(config.get("ch_max_concurrent", 3)),
            "show_browser": bool(config.get("ch_show_browser", False))}


@app.post("/api/channels/pubsettings")
def api_ch_pubsettings_set(body: PubSettingsBody):
    config["ch_max_concurrent"] = max(1, min(20, int(body.max_concurrent or 3)))
    if body.show_browser is not None:
        config["ch_show_browser"] = bool(body.show_browser)
    _save(CONFIG_FILE, config)
    return {"ok": True, "max_concurrent": config["ch_max_concurrent"],
            "show_browser": bool(config.get("ch_show_browser", False))}


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
            _enqueue_upload(t["id"])
            n += 1
    _save_upload_tasks()
    return {"ok": True, "requeued": n}


@app.post("/api/channels/tasks/clear")
def api_channels_clear():
    for tid in [k for k, t in UPLOAD_TASKS.items() if t["status"] in ("done", "failed", "cancelled")]:
        UPLOAD_TASKS.pop(tid, None)
    _save_upload_tasks()
    return {"ok": True}


class TaskIdsBody(BaseModel):
    ids: list[str] = []


@app.post("/api/channels/tasks/retry")
def api_channels_tasks_retry(body: TaskIdsBody):
    """立即重发/批量重发：把指定任务重新排队（失败/已取消/已完成都能重发）。"""
    n = 0
    for tid in body.ids:
        t = UPLOAD_TASKS.get(tid)
        if t and t["status"] in ("failed", "cancelled", "done"):
            t.update(status="queued", err="", stage="", exec_at="", done_at="", elapsed="")
            _enqueue_upload(tid)
            n += 1
    _save_upload_tasks()
    return {"ok": True, "requeued": n}


@app.get("/api/channels/verify_blocked")
def api_channels_verify_blocked():
    """返回被实名验证拦住的账号(前端常驻弹窗轮询它)。"""
    out = []
    for aid, info in CH_VERIFY_BLOCK.items():
        cnt = sum(1 for t in UPLOAD_TASKS.values()
                  if t.get("account_id") == aid and t.get("verify_blocked"))
        out.append({"aid": aid, "name": info.get("name", aid), "msg": info.get("msg", ""),
                    "at": info.get("at", ""), "blocked_count": cnt})
    return {"blocked": out}


class VerifyReBody(BaseModel):
    aid: str = ""


@app.post("/api/channels/verify_republish")
def api_channels_verify_republish(body: VerifyReBody):
    """用户已完成该号实名验证 → 解除拦截 + 把被暂停的任务全部按原设置(含随机间隔)重新排队。"""
    aid = (body.aid or "").strip()
    if not aid:
        return {"ok": False, "msg": "缺 aid"}
    CH_VERIFY_BLOCK.pop(aid, None)
    n = 0
    for t in UPLOAD_TASKS.values():
        if t.get("account_id") == aid and t.get("verify_blocked"):
            t.update(status="queued", err="", stage="实名后重发·排队中",
                     verify_blocked=False, exec_at="", done_at="", elapsed="")
            _enqueue_upload(t["id"])
            n += 1
    _save_upload_tasks()
    return {"ok": True, "requeued": n}


@app.post("/api/channels/tasks/pause")
def api_channels_tasks_pause(body: TaskIdsBody):
    """批量暂停：把还在排队的任务标记已取消（进行中的不打断）。"""
    n = 0
    for tid in body.ids:
        t = UPLOAD_TASKS.get(tid)
        if t and t["status"] == "queued":
            t.update(status="cancelled", err="手动取消")
            n += 1
    _save_upload_tasks()
    return {"ok": True, "cancelled": n}


@app.post("/api/channels/tasks/delete")
def api_channels_tasks_delete(body: TaskIdsBody):
    """批量删除记录（进行中的不删）。"""
    n = 0
    for tid in body.ids:
        t = UPLOAD_TASKS.get(tid)
        if t and t["status"] != "running":
            UPLOAD_TASKS.pop(tid, None)
            n += 1
    _save_upload_tasks()
    return {"ok": True, "deleted": n}


@app.get("/api/channels/tasks/export")
def api_channels_tasks_export():
    """导出记录 CSV。"""
    import io, csv
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["ID", "账号", "账号ID", "标题", "简介", "状态", "失败原因", "批次",
                "创建时间", "执行时间", "完成时间", "用时", "定时", "类型", "文件", "大小MB"])
    stmap = {"queued": "排队中", "running": "上传中", "done": "已同步", "failed": "失败", "cancelled": "已取消"}
    for t in UPLOAD_TASKS.values():
        w.writerow([t["id"], t.get("account_name", ""), t.get("account_wxid", ""),
                    t.get("title", ""), t.get("desc", ""), stmap.get(t["status"], t["status"]),
                    t.get("err", ""), t.get("batch", ""), t.get("created", ""),
                    t.get("exec_at", ""), t.get("done_at", ""), t.get("elapsed", ""),
                    t.get("schedule", "") or t.get("when", "") or "立即",
                    t.get("vtype", "视频"), t.get("video_path", ""), t.get("size_mb", "")])
    data = "﻿" + buf.getvalue()      # BOM 让 Excel 正确识别中文
    return Response(content=data, media_type="text/csv",
                    headers={"Content-Disposition": "attachment; filename=upload_tasks.csv"})


# ============ 原生发布（Electron 隐藏窗口·同一登录会话·不弹可见浏览器） ============
_ELECTRON_PUB = "http://127.0.0.1:8791"


async def _electron_pub_up() -> bool:
    """Electron 原生发布服务(8791)在跑吗——在就用它(隐藏1px窗口驱动，同一登录会话，不弹浏览器)。"""
    try:
        async with httpx.AsyncClient(timeout=2) as c:
            r = await c.get(_ELECTRON_PUB + "/ping")
            return r.status_code == 200
    except Exception:
        return False


def _resolve_drama_exportid(aid: str, drama: str) -> str:
    """挂剧集：剧名→exportId(从缓存查)；已经是 exportId 就原样返回。"""
    if not drama:
        return ""
    if drama.startswith("event/") or drama.startswith("UzF") or len(drama) > 60:
        return drama
    try:
        f = _ch_lists_cache_file(aid)
        if f.exists():
            for d in json.loads(f.read_text(encoding="utf-8")).get("dramas", []):
                if (d.get("name") or "").strip() == drama.strip():
                    return d.get("exportId") or drama
    except Exception:
        pass
    return drama


async def _publish_via_electron(t: dict, aid: str) -> tuple:
    """走 Electron 原生发布器(8791)：隐藏窗口 + 同一登录会话 + CDP 挂剧集。返回 (ok, msg)。"""
    params = {
        "aid": aid, "tid": t.get("id", ""),
        "video_path": t["video_path"], "title": t.get("title", ""),
        "tags": t.get("tags", []), "desc": t.get("desc", ""),
        "location": t.get("location", ""),
        "drama": _resolve_drama_exportid(aid, t.get("drama", "")),
        "drama_title": t.get("drama_title", "") or t.get("drama", ""),
    }
    async with httpx.AsyncClient(timeout=None) as c:
        r = await c.post(_ELECTRON_PUB + "/publish", json=params)
        j = r.json()
    return bool(j.get("ok")), (j.get("msg") or "")


# ============ 纯后端上传+发布(逆向复刻官方SDK协议,零浏览器零点击) ============
try:
    import channels_upload as _CU
    _HAS_CU = True
except Exception as _cu_e:                      # 缺依赖等异常不阻断启动,回退老路
    _CU, _HAS_CU = None, False

_ch_auth_cache = {}                             # aid -> ChannelsAuth(cookies+设备指纹+uin)


def _bpub_log(msg: str):
    """纯后端发布的详细日志(便于监控/排查),落 data/backend_publish.log。"""
    try:
        with open(DATA / "backend_publish.log", "a", encoding="utf-8") as f:
            f.write("[" + datetime.now().strftime("%H:%M:%S") + "] " + str(msg) + "\n")
    except Exception:
        pass


async def _get_channels_auth(aid: str, force: bool = False):
    """从 Electron /authmat 取活会话鉴权材料(cookies + finger-print-device-id + uin),缓存复用。"""
    if not force and aid in _ch_auth_cache:
        return _ch_auth_cache[aid]
    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.post(_ELECTRON_PUB + "/authmat", json={"aid": aid})
        j = r.json()
    if not j.get("ok"):
        raise RuntimeError("取会话材料失败:" + str(j.get("msg", ""))[:80])
    cookies = {ck["name"]: ck["value"] for ck in (j.get("cookies") or [])}
    ls = j.get("localStorage") or {}
    fp = ls.get("_finger_print_device_id", "")
    uin = ls.get("finder_uin", "") or cookies.get("wxuin", "")
    auth = _CU.ChannelsAuth(cookies, fp, uin)
    _ch_auth_cache[aid] = auth
    return auth


async def _publish_via_backend(t: dict, aid: str) -> tuple:
    """纯后端发布:helper_upload_params→CDN分片上传视频/封面→post_clip_video→post_create(挂剧集)。
    全程 httpx,不弹浏览器、不点按钮。返回 (ok, msg)。"""
    # 组文案(短标题 + #话题 + 简介)。短标题里已含某话题就不重复追加(用户可把整段带#文案放进短标题=一字不差)。
    caption = (t.get("title") or "")
    for tg in (t.get("tags") or []):
        tag = "#" + str(tg).lstrip("#")
        if tag not in caption:
            caption += " " + tag
    if t.get("desc") and t["desc"] not in caption:
        caption += "\n" + t["desc"]
    exportid = _resolve_drama_exportid(aid, t.get("drama", ""))
    drama = {"id": exportid, "title": (t.get("drama_title") or t.get("drama") or exportid)} if exportid else None
    cover = t.get("cover_path") or None
    _bpub_log(f"===== 开始纯后端发布 aid={aid} video={t.get('video_path','')}")
    _bpub_log(f"  标题={caption[:40]!r}  剧集入参 drama={t.get('drama','')!r} title={t.get('drama_title','')!r} → 解析exportId={exportid!r}")
    _bpub_log(f"  最终 component = {drama}")
    for attempt in range(2):                    # 鉴权失效时刷新一次重试
        try:
            auth = await _get_channels_auth(aid, force=(attempt > 0))
            _bpub_log(f"  会话: uin={auth.uin} fp={auth.fp[:10]}… cookies={list(auth.cookies.keys())}")
        except Exception as e:
            _bpub_log(f"  取会话失败: {e}")
            return False, "取会话失败:" + str(e)[:100]
        try:
            resp = await asyncio.to_thread(
                _CU.publish_video, auth, t["video_path"], caption, drama, False, cover,
                lambda m: (_bpub_log("  " + m), t.update(stage=m)))
        except Exception as e:
            _bpub_log(f"  publish_video 异常(attempt={attempt}): {e}")
            if attempt == 0:
                _ch_auth_cache.pop(aid, None)
                continue
            return False, str(e)[:160]
        ec = resp.get("errCode")
        _bpub_log(f"  post_create 响应: {json.dumps(resp, ensure_ascii=False)[:300]}")
        if ec == 0:
            _bpub_log(f"  ✅ 发布成功 objectId={((resp.get('data') or {}).get('objectId'))}")
            return True, ("发布成功" + (f"(已挂剧集 {drama['title']})" if drama else ""))
        # 实名验证类:直接报,交给上层停号 + 常驻提醒(纯后端看不到弹窗,靠 errMsg 关键词识别)
        if _is_verify_msg(resp.get("errMsg", "")):
            return False, "实名验证：该账号需完成实名验证才能发表（errCode=%s）" % ec
        # 300xxx 类多为登录态失效 → 刷新会话再试一次
        if attempt == 0 and (str(ec).startswith("3000") or "登录" in str(resp.get("errMsg", ""))):
            _bpub_log("  errCode 疑似登录态失效 → 刷新会话重试")
            _ch_auth_cache.pop(aid, None)
            continue
        return False, f"视频号拒绝(errCode={ec} {resp.get('errMsg', '')})"
    return False, "发布失败(会话刷新后仍失败)"


def _up_sem() -> asyncio.Semaphore:
    """全局并发闸：最多 ch_max_concurrent 个账号同时上传（默认3）。事件循环里懒创建。"""
    global UP_SEM
    if UP_SEM is None:
        try:
            n = int(config.get("ch_max_concurrent", 3))
        except Exception:
            n = 3
        UP_SEM = asyncio.Semaphore(max(1, n))
    return UP_SEM


def _already_done(aid: str, video_path: str, cur_tid: str = "") -> bool:
    """幂等：这条视频对这个账号是否已发成功过(status=done)。
    防断点续跑/重复入队/重复点击导致重复发(重复发同一条会触发视频号实名验证)。"""
    if not video_path:
        return False
    for x in UPLOAD_TASKS.values():
        if x.get("id") != cur_tid and x.get("account_id") == aid \
                and x.get("video_path") == video_path and x.get("status") == "done":
            return True
    return False


def _enqueue_upload(tid: str):
    """把一条上传任务派到它所属账号的队列，并确保该账号的工人在跑。
    不同账号 → 不同工人 → 并行；同账号 → 同一工人 → 串行+间隔。
    【关键】同步端点在线程池里跑，那里没有事件循环 + asyncio.Queue 非线程安全，
    所以队列操作和建 worker 都必须回到主循环线程执行。"""
    def _do():
        t = UPLOAD_TASKS.get(tid)
        if not t:
            return
        aid = t.get("account_id") or "_"
        q = UP_ACCT_QUEUES.get(aid)
        if q is None:
            q = asyncio.Queue()
            UP_ACCT_QUEUES[aid] = q
        q.put_nowait(tid)
        w = UP_ACCT_WORKERS.get(aid)
        if w is None or w.done():
            UP_ACCT_WORKERS[aid] = asyncio.create_task(_acct_upload_worker(aid))
    try:
        asyncio.get_running_loop()      # 已在主循环(async 端点/startup)→ 直接执行
        _do()
    except RuntimeError:                # 在线程池(同步端点)→ 调度回主循环，线程安全
        if MAIN_LOOP is not None:
            MAIN_LOOP.call_soon_threadsafe(_do)


# ==================== 实名验证拦截：某号触发实名验证 → 停该号、常驻提醒、实名后批量重发 ====================
CH_VERIFY_BLOCK: dict = {}     # aid -> {"name","msg","at"}  被实名验证拦住的账号(前端常驻弹窗读它)


def _is_verify_msg(msg: str) -> bool:
    """判断一条发布失败信息是不是'需要实名验证'类(纯后端errMsg / 老Playwright的DOM检测 都覆盖)。"""
    s = str(msg or "")
    return any(k in s for k in ("实名验证", "实名信息核验", "身份验证", "身份核验", "安全验证", "验证弹窗"))


def _mark_verify_block(aid: str, msg: str):
    """标记该号被实名验证拦住 + 把它队列里还没发的任务全部暂停(标 verify_blocked，实名后可批量重发)。"""
    acc = _ch_account(aid) or {}
    CH_VERIFY_BLOCK[aid] = {"name": acc.get("name", aid), "msg": str(msg or "")[:80],
                            "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
    for t in UPLOAD_TASKS.values():
        if t.get("account_id") == aid and t.get("status") in ("queued", "running"):
            t.update(status="failed", verify_blocked=True,
                     err="⚠该账号需实名验证，已暂停（实名后可一键批量重发）",
                     stage="等待实名验证")
    _save_upload_tasks()


def _rand_gap(g: int) -> int:
    """把界面选的间隔秒数 g 转成随机秒,避免固定间隔的机器特征(防封号):
    选 1分钟(60)→随机 40~90、2分钟(120)→80~180、依此类推(约 2/3g ~ 3/2g)。"""
    g = int(g or 0)
    if g <= 0:
        return 0
    lo, hi = round(g * 2 / 3), round(g * 3 / 2)
    return random.randint(min(lo, hi), max(lo, hi))


async def _acct_upload_worker(aid: str):
    """单个账号的上传工人：串行处理该账号的队列，条间带随机间隔防封号。
    真正上传时占用全局信号量 → 同时在传的账号数被压在上限内。空闲即退出（下次入队再拉起）。"""
    q = UP_ACCT_QUEUES.get(aid)
    if q is None:
        return
    while True:
        try:
            tid = await asyncio.wait_for(q.get(), timeout=20)
        except asyncio.TimeoutError:
            UP_ACCT_WORKERS.pop(aid, None)     # 空闲20秒无任务→退出，省资源
            return
        t = UPLOAD_TASKS.get(tid)
        if not t or t["status"] != "queued":
            continue
        # 该号已被实名验证拦住 → 这条也别发(继续发只会一直撞验证)，标暂停等实名后批量重发
        if aid in CH_VERIFY_BLOCK:
            t.update(status="failed", verify_blocked=True, stage="等待实名验证",
                     err="⚠该账号需实名验证，已暂停（实名后可一键批量重发）",
                     done_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
            _save_upload_tasks()
            continue
        # 【幂等】这条视频对这个账号已发成功过 → 直接跳过，绝不重复发(防断点续跑/重复入队重发)
        if _already_done(aid, t.get("video_path", ""), tid):
            t.update(status="done", err="", stage="该账号已发过这条·自动跳过",
                     done_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"), elapsed="跳过")
            _save_upload_tasks()
            continue
        if not aid or aid == "_" or not ch_online(aid):
            t.update(status="failed", err="该账号未登录",
                     done_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
            _save_upload_tasks()
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
        t["exec_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        _t0 = time.time()
        sched = None
        if t.get("schedule"):
            try:
                sched = datetime.strptime(t["schedule"], "%Y-%m-%d %H:%M")
            except Exception:
                sched = None
        async with _up_sem():             # 占用一个并发名额（离开 with 立即释放，间隔期不占名额）
            # 【自动重试】临时性失败(网络/超时/浏览器起不来)自动重试,不用手点「立即重发」。
            # 拒绝类(登录失效/平台拒绝/文件是空的)重试也没用,直接报,不浪费时间。
            RETRYABLE = ("超时", "timeout", "端口", "浏览器", "未收到", "net::", "断开",
                         "connection", "target", "closed", "启动失败")
            # 实名验证类绝不重试：重试=重传同一条视频,恰恰是继续刷风控的行为
            FATAL = ("登录", "login", "拒绝", "空的", "不存在", "敏感", "实名验证", "验证弹窗")
            ok, msg = False, ""
            for _try in range(3):                 # 首发 + 最多2次重试
                if _try:
                    t.update(stage=f"发布失败({msg[:40]})，自动重试第{_try}次…")
                    await asyncio.sleep(10 * _try)
                try:
                    # ①优先纯后端(逆向官方协议·httpx直传·不弹浏览器不点按钮·最快最稳)
                    #  需要 Electron 8791 在跑(仅用它导出活会话 cookies/设备指纹,不驱动窗口)
                    if _HAS_CU and await _electron_pub_up():
                        t.update(stage="纯后端上传发布中(不弹浏览器)…")
                        ok, msg = await _publish_via_backend(t, aid)
                    # ②回退:Electron 原生窗口发布器(隐藏窗口·CDP)
                    elif await _electron_pub_up():
                        t.update(stage="用原生会话发布中(不弹浏览器)…")
                        ok, msg = await _publish_via_electron(t, aid)
                    # ③最后回退:老 Playwright
                    else:
                        ok, msg = await channels.upload(
                            _ch_state(aid), t["video_path"], title=t["title"], tags=t["tags"],
                            desc=t["desc"], cover_path=t.get("cover_path", ""), original=t["original"],
                            link=t.get("link", ""), statement=t.get("statement", ""),
                            location=t.get("location", ""), collection=t.get("collection", ""),
                            drama=t.get("drama", ""), drama_title=t.get("drama_title", ""),
                            activity=t.get("activity", ""),
                            schedule=sched, headless=False, err_dir=DATA,
                            show_browser=bool(config.get("ch_show_browser", False)),  # 调试:显示浏览器
                            on_status=lambda m: t.update(stage=m))
                except Exception as e:
                    ok, msg = False, str(e)[:160]
                if ok:
                    break
                low = (msg or "").lower()
                if any(k in (msg or "") or k in low for k in FATAL):
                    break                          # 拒绝类：重试无意义
                if not any(k in (msg or "") or k in low for k in RETRYABLE) and _try >= 1:
                    break                          # 未知错误：只多试一次
            t.update(status="done" if ok else "failed", err="" if ok else msg, stage=msg)
            # 被动校验：登录态失效类报错→标记账号失效
            if not ok and ("login" in (msg or "").lower() or "登录" in (msg or "")):
                _mark_session(aid, False)
            # 实名验证：这条发不了 + 停掉该号后续所有任务(继续发只会一直撞验证/加重风控)，前端常驻提醒
            if not ok and _is_verify_msg(msg):
                t["verify_blocked"] = True
                _mark_verify_block(aid, msg)
        _el = int(time.time() - _t0)
        t["done_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        t["elapsed"] = (f"{_el//60}分{_el%60}秒" if _el >= 60 else f"{_el}秒")
        _save_upload_tasks()
        # 防封号：同账号条间随机间隔（只在该账号还有待发时才等）
        gb = int(t.get("gap_base") or 0)
        if t.get("publish_at"):
            wait_s = 0                       # 本机定时:间隔已排进 publish_at(到点发),不再重复等
        elif gb > 0:
            wait_s = _rand_gap(gb)           # 选了作品间隔:选值→随机秒(60→40~90),而非固定值
        else:
            gap = config.get("ch_gap_range") or [30, 90]   # 无间隔:防封号默认随机
            try:
                lo, hi = int(gap[0]), int(gap[1])
            except Exception:
                lo, hi = 30, 90
            wait_s = random.randint(min(lo, hi), max(lo, hi)) if hi > 0 else 0
        if not q.empty() and wait_s > 0:
            t["stage"] = f"防封号间隔：随机等待 {wait_s}s 再发下一条"
            await asyncio.sleep(wait_s)


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
    clip_paths: list[str] = []   # 从下载库勾选的多个文件夹里的视频路径（优先于 clips_folder 扫描）
    sequential: bool = False     # 按集数顺序每 n_clips 集一个草稿(1-3、4-6…)，自动覆盖全部
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
        clip_paths = body.clip_paths if (not body.clips_folder and body.clip_paths) else None
        made = await asyncio.to_thread(
            jianying.compose_batch, body.template, folder, body.out_prefix,
            body.count, body.mode, body.n_clips, body.target_sec,
            (body.speed_min, body.speed_max), lambda m: JY.update(status=m), clip_paths, body.sequential)
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


# ---------------- ffmpeg 纯代码去重导出（绕开剪映，直接出 mp4） ----------------
FD = {"running": False, "status": "", "made": [], "error": ""}
FD_TASKS = {}                                  # 去重任务记录 {name: {...}}
_fd_seq = 0
_fd_lock = threading.Lock()                    # 并发渲染时 FD_TASKS 写盘加锁
FD_TASKS_FILE = DATA / "dedup_tasks.json"


def _save_fd_tasks():
    try:
        FD_TASKS_FILE.write_text(json.dumps(list(FD_TASKS.values())[-500:], ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def _load_fd_tasks():
    global _fd_seq
    try:
        if not FD_TASKS_FILE.exists():
            return
        for t in json.loads(FD_TASKS_FILE.read_text(encoding="utf-8")):
            if t.get("status") in ("queued", "running"):
                t["status"] = "cancelled"
                t["err"] = t.get("err") or "软件重启，任务中断(将自动续跑)"
            # 【清垃圾】被中断/失败的任务留下的 <10KB 空壳输出和 .part 残片删掉——
            # 0字节假视频混进发布流程会白等8分钟超时(用户拿它当真视频发，怎么发都失败)
            if t.get("status") in ("cancelled", "failed"):
                try:
                    o = t.get("out") or ""
                    if o and os.path.exists(o) and os.path.getsize(o) < 10240:
                        os.remove(o)
                    if o.lower().endswith(".mp4"):
                        pt = o[:-4] + ".part.mp4"
                        if os.path.exists(pt):
                            os.remove(pt)
                except OSError:
                    pass
            FD_TASKS[t["id"]] = t
            try:
                _fd_seq = max(_fd_seq, int(str(t["id"]).lstrip("f")))
            except Exception:
                pass
    except Exception:
        pass


def _fd_on_task(rec):
    """ffdedup 每条成品生命周期回调 → 落进 FD_TASKS。并发线程调用，加锁。"""
    global _fd_seq
    with _fd_lock:
        tid = FD.get("_id_map", {}).get(rec["name"])
        if not tid:
            _fd_seq += 1
            tid = f"f{_fd_seq}"
            FD.setdefault("_id_map", {})[rec["name"]] = tid
            FD_TASKS[tid] = {"id": tid, "created": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                             "batch": FD.get("_batch", "")}
        t = FD_TASKS[tid]
        t.update(name=rec["name"], coll=rec.get("coll", ""), eps=rec.get("eps", 0),
                 out=rec.get("out", ""), status=rec["status"], err=rec.get("err", ""),
                 size_mb=rec.get("size_mb", 0), elapsed=rec.get("elapsed", ""),
                 dur=rec.get("dur", ""), fp=rec.get("fp"))
        if rec["status"] == "running" and not t.get("exec_at"):
            t["exec_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if rec["status"] in ("done", "failed"):
            t["done_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        _save_fd_tasks()


class FdBody(BaseModel):
    """cfg = {bands:{band_h,opacity,top:{enable,folder},bottom:{...}}, color:{brightness/contrast/saturation/sharpen/hue:[lo,hi]},
    speed:[lo,hi], crop:[lo,hi], mirror_prob, noise, stickers:{...}, bgm:{enable,folder,volume:[lo,hi]}}"""
    clip_paths: list[str] = []
    per: int = 3
    count: int = 0                # 0=全部
    prefix: str = "去重"
    out_dir: str = ""             # 空=剪映成片输出文件夹(jy_output_dir)
    concurrency: int = 2          # 同时渲染几条成品
    variants: int = 1             # 每条成品渲染几个随机变体(多账号各发一个,指纹互不相同)
    cfg: dict = {}


FD_PENDING_FILE = DATA / "dedup_pending.json"   # 正在跑的渲染请求(软件重启后自动续跑)


async def _fd_bg(body: "FdBody", resume: bool = False):
    FD.update(running=True, status=("续跑上次中断的批次…" if resume else "开始…"), made=[], error="",
              stop=False,   # 每次开始清停止标志
              _id_map={}, _batch=datetime.now().strftime("批次 %m-%d %H:%M:%S"))
    # 每次导出都新建一个「年月日-时分」子文件夹，绝不追加进上一次的文件夹（多次导出混一起会乱）。
    # 续跑(resume)时沿用上次已定好的时间戳文件夹，才能跳过已完成的成品、只补没跑完的。
    if not resume:
        base_out = (body.out_dir or "").strip() or config.get("jy_output_dir") or str(_dedup_out_dir())
        stamp = datetime.now().strftime("%Y-%m-%d %H：%M")   # 全角冒号(Windows 文件夹名合法)，形如 2026-07-13 14：05
        # 时间戳后面带上剧名，一眼看出是哪部剧的成品；多部剧取「第一部等N部」
        _dramas, _seen = [], set()
        for _p in (body.clip_paths or []):
            _nm = os.path.basename(os.path.dirname(_p))
            if _nm and _nm not in _seen:
                _seen.add(_nm); _dramas.append(_nm)
        if len(_dramas) == 1:
            _label = _dramas[0]
        elif len(_dramas) > 1:
            _label = f"{_dramas[0]}等{len(_dramas)}部"
        else:
            _label = ""
        _label = re.sub(r'[\\/:*?"<>|\r\n\t]', "", _label).strip()[:50]
        folder_name = (f"{stamp} {_label}").strip()
        run_dir = os.path.join(base_out, folder_name)
        k = 2
        while os.path.exists(run_dir):            # 同一分钟内又点一次导出 → 加后缀防撞
            run_dir = os.path.join(base_out, f"{folder_name}_{k}"); k += 1
        try:
            os.makedirs(run_dir, exist_ok=True)
        except Exception:
            run_dir = base_out                    # 万一建不了就退回原目录，别让导出失败
        body.out_dir = run_dir                    # 写进 body：续跑落盘后用同一个时间戳文件夹
    # 把请求落盘：批次没跑完软件就被关/重启时，下次启动自动续跑(已完成的成品跳过)
    try:
        FD_PENDING_FILE.write_text(json.dumps(
            body.model_dump() if hasattr(body, "model_dump") else body.dict(),
            ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass
    try:
        out_dir = (body.out_dir or "").strip() or config.get("jy_output_dir") or str(_dedup_out_dir())
        made = await asyncio.to_thread(
            ffdedup.dedup_batch, body.clip_paths, body.cfg, out_dir,
            body.per, body.count, body.prefix,
            lambda m: FD.update(status=m), _fd_on_task, max(1, int(body.concurrency or 2)),
            resume,                                   # resume=True → 已存在成品跳过，只补缺
            max(1, int(body.variants or 1)),          # 每条成品渲染N个随机变体
            lambda: FD.get("stop"))                   # 停止标志:用户点停止时中断批次
        FD.update(made=made, status=f"完成，导出 {len(made)} 个成品到 {out_dir}")
    except Exception as e:
        FD.update(error=str(e)[:200], status=f"出错：{str(e)[:150]}")
        log_err(f"ffmpeg去重出错: {e}")
    finally:
        FD["running"] = False
        try:
            FD_PENDING_FILE.unlink()                  # 正常跑完(或明确失败)就清掉，不再续跑
        except OSError:
            pass


def _fd_resume_pending():
    """启动时检查上次没跑完的去重批次，自动续跑(已完成的成品跳过，只补没渲染完的)。"""
    try:
        if not FD_PENDING_FILE.exists():
            return
        data = json.loads(FD_PENDING_FILE.read_text(encoding="utf-8"))
        body = FdBody(**data)
        if not body.clip_paths:
            FD_PENDING_FILE.unlink()
            return
        print(f"[FD] 检测到上次中断的去重批次({len(body.clip_paths)}集)，自动续跑", flush=True)
        asyncio.create_task(_fd_bg(body, resume=True))
    except Exception as e:
        log_err(f"去重批次续跑失败: {e}")
        try:
            FD_PENDING_FILE.unlink()
        except OSError:
            pass


@app.post("/api/ffdedup/render")
async def api_ffdedup_render(body: FdBody):
    if FD["running"]:
        return {"ok": True, "already": True}
    if not body.clip_paths:
        return JSONResponse({"error": "先在上面勾选素材文件夹"}, status_code=400)
    b = body.cfg.get("bands") or {}
    for key, cn in (("top", "上"), ("bottom", "下")):
        bb = b.get(key) or {}
        if bb.get("enable") and not (bb.get("folder") or "").strip():
            return JSONResponse({"error": f"{cn}方蒙版已启用，请先选它的素材文件夹"}, status_code=400)
    # BGM / 片尾 默认是开的：启用了但没选文件夹 → 当没开(自动跳过)，不拦着不让导出。
    bg = body.cfg.get("bgm") or {}
    if bg.get("enable") and not (bg.get("folder") or "").strip():
        bg["enable"] = False
    tvc = body.cfg.get("tailvid") or {}
    if tvc.get("enable") and not (tvc.get("folder") or "").strip():
        tvc["enable"] = False
    asyncio.create_task(_fd_bg(body))
    return {"ok": True}


@app.post("/api/ffdedup/stop")
def api_ffdedup_stop():
    """停止去重导出：置停止标志(不再渲染没开始的)+杀掉正在跑的 ffmpeg(中断当前这条)。已完成的成品保留。"""
    FD["stop"] = True
    FD["status"] = "正在停止…"
    try:
        exe = os.path.basename(ffdedup.FF)   # imageio 的 ffmpeg exe 名
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/IM", exe, "/F"], capture_output=True,
                           creationflags=dedup.NOWIN)
        else:
            subprocess.run(["pkill", "-f", exe], capture_output=True)
    except Exception as e:
        log_err(f"停止去重杀ffmpeg失败: {e}")
    # 停止后不再自动续跑这个批次
    try:
        FD_PENDING_FILE.unlink()
    except OSError:
        pass
    return {"ok": True}


@app.get("/api/ffdedup/status")
def api_ffdedup_status():
    counts = {k: 0 for k in ("running", "done", "failed", "cancelled")}
    for t in FD_TASKS.values():
        counts[t["status"]] = counts.get(t["status"], 0) + 1
    tasks = list(FD_TASKS.values())[-500:]
    tasks.reverse()
    return {"running": FD["running"], "status": FD["status"], "made": FD["made"],
            "error": FD["error"], "counts": counts, "tasks": tasks}


@app.post("/api/ffdedup/tasks/delete")
def api_ffdedup_tasks_delete(body: TaskIdsBody):
    n = 0
    for tid in body.ids:
        t = FD_TASKS.get(tid)
        if t and t["status"] != "running":
            FD_TASKS.pop(tid, None)
            n += 1
    _save_fd_tasks()
    return {"ok": True, "deleted": n}


@app.post("/api/ffdedup/tasks/clear")
def api_ffdedup_tasks_clear():
    for tid in [k for k, t in FD_TASKS.items() if t["status"] in ("done", "failed", "cancelled")]:
        FD_TASKS.pop(tid, None)
    _save_fd_tasks()
    return {"ok": True}


@app.get("/api/ffdedup/tasks/export")
def api_ffdedup_tasks_export():
    import io, csv
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["ID", "成品名", "合集", "集数", "状态", "失败原因", "批次", "创建", "完成", "用时", "大小MB", "文件"])
    stmap = {"running": "处理中", "done": "成功", "failed": "失败", "cancelled": "已取消"}
    for t in FD_TASKS.values():
        w.writerow([t["id"], t.get("name", ""), t.get("coll", ""), t.get("eps", ""),
                    stmap.get(t["status"], t["status"]), t.get("err", ""), t.get("batch", ""),
                    t.get("created", ""), t.get("done_at", ""), t.get("elapsed", ""),
                    t.get("size_mb", ""), t.get("out", "")])
    return Response(content="﻿" + buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": "attachment; filename=dedup_tasks.csv"})


class FdCfgBody(BaseModel):
    cfg: dict = {}


@app.get("/api/ffdedup/cfg")
def api_ffdedup_cfg_get():
    return {"cfg": config.get("ffdedup_cfg") or {}}


@app.get("/api/ffdedup/outdir")
def api_ffdedup_outdir():
    """去重成品实际落地的输出目录（视频号发布"添加视频"默认定位到这里）。
    优先用"一键去重导出"页设的"导出到"(ffdedup_cfg.fd_out)，与实际落地一致。"""
    fc = config.get("ffdedup_cfg") or {}
    d = ((fc.get("fd_out") or "").strip()
         or (config.get("jy_output_dir") or "").strip()
         or str(_dedup_out_dir()))
    return {"path": d}


@app.get("/api/ffdedup/videos")
def api_ffdedup_videos():
    """列出导出目录里的去重成品(按合集子文件夹分组)，供视频号发布直接挑选。"""
    fc = config.get("ffdedup_cfg") or {}
    root = ((fc.get("fd_out") or "").strip()
            or (config.get("jy_output_dir") or "").strip()
            or str(_dedup_out_dir()))
    out = []
    p = Path(root)
    if p.exists():
        for f in p.rglob("*.mp4"):
            if not f.is_file() or _is_incomplete(f.name):
                continue
            parts = list(f.relative_to(p).parts[:-1])
            out.append({"path": str(f), "name": f.name,
                        "blogger": parts[0] if parts else "导出根目录",
                        "mix": parts[1] if len(parts) >= 2 else "",
                        "size": round(f.stat().st_size / 1024 / 1024, 1),
                        "mtime": f.stat().st_mtime})
    out.sort(key=lambda x: x["mtime"], reverse=True)
    return {"videos": out[:800], "root": root}


@app.get("/api/ffdedup/materials")
def api_ffdedup_materials():
    """去重「素材来源」：只实时扫描下载库（不含成片输出目录），与资源管理器里的下载文件夹一一对应。"""
    out = []
    seen = set()
    plat_names = {"抖音", "TikTok"}
    root = get_dl()
    if root.exists():
        for f in root.rglob("*.mp4"):
            if not f.is_file() or _is_incomplete(f.name) or str(f) in seen:
                continue
            seen.add(str(f))
            parts = list(f.relative_to(root).parts[:-1])
            if parts and parts[0] in plat_names:      # 去掉「抖音/TikTok」平台层，露出真实博主/剧名
                parts = parts[1:]
            blogger = parts[0] if len(parts) >= 1 else "(未分类)"
            out.append({"path": str(f), "name": f.name,
                        "blogger": blogger,
                        "mix": parts[1] if len(parts) >= 2 else "",
                        "size": round(f.stat().st_size / 1024 / 1024, 1),
                        "mtime": f.stat().st_mtime})
    out.sort(key=lambda x: x["mtime"], reverse=True)
    return {"videos": out, "root": str(root)}


def _ffd_probe_one(path: str) -> dict:
    """一次 ffmpeg -i 拿到 时长/分辨率/像素宽高比(SAR)/显示比例(DAR)。"""
    info = {"path": path, "name": os.path.basename(path),
            "size": 0.0, "dur": 0.0, "w": 0, "h": 0, "sar": "", "dar": "", "narrow": False}
    try:
        info["size"] = round(os.path.getsize(path) / 1024 / 1024, 1)
    except Exception:
        pass
    try:
        txt = subprocess.run([dedup.FF, "-i", path], capture_output=True,
                             timeout=25, creationflags=dedup.NOWIN).stderr.decode("utf-8", "ignore")
        m = re.search(r"Duration: (\d+):(\d+):([\d.]+)", txt)
        if m:
            info["dur"] = int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3])
        v = re.search(r"Video:.*?(\d{2,5})x(\d{2,5})", txt)
        if v:
            info["w"], info["h"] = int(v[1]), int(v[2])
        s = re.search(r"SAR (\d+):(\d+) DAR (\d+):(\d+)", txt)
        if s:
            info["sar"] = f"{s[1]}:{s[2]}"
            info["dar"] = f"{s[3]}:{s[4]}"
            info["narrow"] = (int(s[1]) != int(s[2]))   # 像素宽高比≠1:1 → 播放器会压成窄条
    except Exception:
        pass
    return info


class ProbePathsBody(BaseModel):
    paths: list[str] = []


@app.post("/api/ffdedup/probe")
def api_ffdedup_probe(body: ProbePathsBody):
    """探测一批视频的详细信息（时长/分辨率/宽高比），供「点文件夹看视频」用。"""
    from concurrent.futures import ThreadPoolExecutor
    paths = [p for p in (body.paths or []) if p][:80]
    if not paths:
        return {"items": []}
    with ThreadPoolExecutor(max_workers=6) as ex:
        items = list(ex.map(_ffd_probe_one, paths))
    return {"items": items}


class RevealFolderBody(BaseModel):
    path: str = ""


@app.post("/api/ffdedup/reveal_folder")
def api_ffdedup_reveal_folder(body: RevealFolderBody):
    """在资源管理器打开某个素材文件夹（限下载库内，防越权）。"""
    try:
        rp = Path(body.path or "").resolve()
        root = get_dl().resolve()
        if rp == root or root in rp.parents:
            target = rp if rp.is_dir() else rp.parent
            os.startfile(str(target))
            return {"ok": True}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return JSONResponse({"error": "路径不在下载库内"}, status_code=400)


@app.post("/api/ffdedup/cfg")
def api_ffdedup_cfg_set(body: FdCfgBody):
    config["ffdedup_cfg"] = body.cfg or {}
    _save(CONFIG_FILE, config)
    return {"ok": True}


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
    cfg.global = {duration_min:15} —— 全局时长(分钟)，上下蒙版循环拼到这里、草稿总时长随之
    cfg.bands = {top/bottom:{enable,mask_on,alpha,scale,x,y,rotation,mask:{...}}} —— 上下蒙版视频
    cfg.bgm = {enable, folder:背景音乐文件夹, volume:[lo,hi]} —— 有文件夹用文件夹，否则用素材库
    cfg.stickers = {enable, scale:[lo,hi], alpha:[lo,hi]} —— 贴纸层大小/不透明度随机
    cfg.fx = {enable, strength:[lo,hi]} —— 特效(广角)强度随机
    cfg.filter = {enable, value:[lo,hi]} —— 滤镜强度随机
    cfg.adjust = {enable, brightness/sharpen/clear/particle/contrast/saturation:[lo,hi], hue_jitter:N}
                 —— 调节各项小幅随机 + HSL色相每通道±N抖动
    cfg.speed=[lo,hi] cfg.n_clips/mode/target_sec 同老混剪; cfg.library=素材库文件夹
    template 已废弃：效果源改用软件自带的 base_shell，不再依赖剪映里的某个草稿。"""
    template: str = ""          # 已忽略，仅为兼容旧前端保留
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


class JyTplCfgBody(BaseModel):
    cfg: dict = {}


@app.get("/api/jy/tpl_cfg")
def api_jy_tpl_cfg_get():
    return {"cfg": config.get("jy_tpl_cfg") or {}}


@app.post("/api/jy/tpl_cfg")
def api_jy_tpl_cfg_set(body: JyTplCfgBody):
    """做剪映模版页的设置持久化（蒙版设置等），重启还在。"""
    config["jy_tpl_cfg"] = body.cfg or {}
    _save(CONFIG_FILE, config)
    return {"ok": True}


@app.get("/api/jy/mix_cfg")
def api_jy_mix_cfg_get():
    return {"cfg": config.get("jy_mix_cfg") or {}}


@app.post("/api/jy/mix_cfg")
def api_jy_mix_cfg_set(body: JyTplCfgBody):
    """剪映混剪页的设置持久化（按集数顺序/每N集/变速/命名/勾选的下载库文件夹等）。"""
    config["jy_mix_cfg"] = body.cfg or {}
    _save(CONFIG_FILE, config)
    return {"ok": True}


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
    # 主素材库已取消，改校验上下蒙版各自的素材文件夹（启用的必须有文件夹）
    bands = body.cfg.get("bands") or {}
    top, bot = bands.get("top") or {}, bands.get("bottom") or {}
    if not top.get("enable") and not bot.get("enable"):
        return JSONResponse({"error": "上下方视频至少启用一个"}, status_code=400)
    if top.get("enable") and not (top.get("folder") or "").strip():
        return JSONResponse({"error": "「上方视频」已启用，请先选它的素材文件夹"}, status_code=400)
    if bot.get("enable") and not (bot.get("folder") or "").strip():
        return JSONResponse({"error": "「下方视频」已启用，请先选它的素材文件夹"}, status_code=400)
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
        # Electron 安装版(main.js 注入 BAOKUAN_VER)看 desktop_version;pywebview 老版看 version
        is_electron = bool(os.environ.get("BAOKUAN_VER"))
        latest = str((info.get("desktop_version") if is_electron else "") or info.get("version", VERSION))
        has = _ver_tuple(latest) > _ver_tuple(VERSION)
        return {"current": VERSION, "latest": latest, "has_update": has, "is_electron": is_electron,
                "notes": (info.get("desktop_notes") if is_electron else info.get("notes")) or "",
                "url": info.get("url") or RELEASE_PAGE,
                "exe_url": info.get("exe_url", ""),
                # Electron 版的真正更新走外壳(启动自动增量)，不用后端 pywebview 下载
                "can_auto": bool(getattr(sys, "frozen", False)) and not is_electron}
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


# 自动更新的实时进度（前端轮询 /api/update_progress 画进度条）
UPDATE_STATE = {"running": False, "stage": "", "downloaded": 0, "total": 0,
                "error": "", "done": False, "version": ""}


def _upd_fail(msg: str):
    _ulog(f"更新失败：{msg}")
    UPDATE_STATE.update(running=False, error=str(msg)[:200], stage="error")


@app.get("/api/update_progress")
def api_update_progress():
    """前端轮询它画下载进度条。"""
    return UPDATE_STATE


@app.post("/api/do_update")
async def api_do_update():
    """启动自动更新（后台跑，立即返回）。前端用 /api/update_progress 轮询进度。"""
    if UPDATE_STATE["running"]:
        return {"ok": True, "already": True}
    if not getattr(sys, "frozen", False):
        return JSONResponse({"error": "源码运行不支持自动覆盖更新（开发时请用 git pull）"}, status_code=400)
    if not UPDATE_RAW_URL:
        return JSONResponse({"error": "未配置更新源"}, status_code=400)
    UPDATE_STATE.update(running=True, stage="preparing", downloaded=0, total=0,
                        error="", done=False, version="")
    asyncio.create_task(_do_update_bg())
    return {"ok": True, "started": True}


async def _do_update_bg():
    """自动更新：下载新版 exe(带进度) → 重命名运行中的自己 → 新版就位 → 重启。全程写 update.log。"""
    _ulog("==== do_update 开始 ====")
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as c:
            info = (await c.get(UPDATE_RAW_URL)).json()
        exe_url = info.get("exe_url")
        UPDATE_STATE["version"] = str(info.get("version", ""))
        _ulog(f"目标版本 {info.get('version')}  exe_url={exe_url}")
        if not exe_url:
            _upd_fail("更新信息里没有 exe 下载地址")
            return

        cur = Path(sys.executable)
        newf = cur.with_name(cur.stem + "_new.exe")
        _ulog(f"当前 exe = {cur}")
        # 下载新 exe（逐块累加进度，供前端进度条）
        _ulog("开始下载…")
        UPDATE_STATE["stage"] = "downloading"
        async with httpx.AsyncClient(timeout=None, follow_redirects=True) as c:
            async with c.stream("GET", exe_url) as r:
                if r.status_code != 200:
                    _ulog(f"下载失败 HTTP {r.status_code}")
                    _upd_fail(f"下载新版失败 HTTP {r.status_code}（更新源在你网络下可能不通/太慢）")
                    return
                UPDATE_STATE["total"] = int(r.headers.get("content-length") or 0)
                tmp = newf.with_suffix(".part")
                got = 0
                with open(tmp, "wb") as f:
                    async for chunk in r.aiter_bytes(1 << 16):
                        f.write(chunk)
                        got += len(chunk)
                        UPDATE_STATE["downloaded"] = got
                tmp.replace(newf)
        size = newf.stat().st_size
        _ulog(f"下载完成，大小 {size} 字节")
        if size < 1_000_000:
            newf.unlink(missing_ok=True)
            _upd_fail("下载的文件异常（过小），可能网络中断了")
            return

        UPDATE_STATE["stage"] = "installing"
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
            _upd_fail(f"新版已下载好，但一直读不了（可能被杀毒锁住）。请手动：关闭软件 → 把「{newf.name}」改名成「{cur.name}」。（已打开文件夹）")
            return

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
            _upd_fail(f"重命名当前程序失败（{e}）。新版已下载，请手动替换。（已打开文件夹）")
            return
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
            _upd_fail(f"新版就位失败（{e}）。已回滚，请手动替换。（已打开文件夹）")
            return

        UPDATE_STATE.update(stage="restarting", done=True)
        # 直接启动新 exe（不经 PowerShell 中转——从冻结 exe spawn PowerShell 不可靠）。
        # 新 exe 启动时会等旧进程退出、端口释放（见 desktop.py 的 _wait_port_free），
        # 并后台重试清掉这个 _old.exe（见 desktop.py 的 _cleanup_leftovers）。
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
    except Exception as e:
        _upd_fail(f"更新失败: {e}")


@app.post("/api/test_notify")
def api_test_notify():
    notify("🔔 测试通知", "如果你看到这条弹窗并听到声音，说明通知功能正常。")
    return {"ok": True}


class NotifyToggleBody(BaseModel):
    enabled: bool = True


@app.get("/api/notify_enabled")
def api_get_notify_enabled():
    return {"enabled": bool(config.get("notify_enabled", True))}


@app.post("/api/notify_enabled")
def api_set_notify_enabled(body: NotifyToggleBody):
    config["notify_enabled"] = bool(body.enabled)
    _save(CONFIG_FILE, config)
    return {"ok": True, "enabled": config["notify_enabled"]}


if __name__ == "__main__":
    print(f"抖音博主更新监控面板: http://127.0.0.1:{PORT}", flush=True)
    threading.Timer(1.5, lambda: os.startfile(f"http://127.0.0.1:{PORT}")).start()
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")
