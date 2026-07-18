# -*- coding: utf-8 -*-
"""链接下载引擎:用 yt-dlp 下 YouTube/TikTok/抖音/Instagram/X/B站/微博 等 1000+ 站点。
单条链接 + 主页/频道/播放列表/合集整批都吃。进度落 YTDL_TASKS,前端轮询显示。
TikTok/抖音/YouTube 需要代理(VPN)时传 proxy(和监控同一套代理设置)。"""
import datetime
import os
import tempfile
import threading
import uuid

import yt_dlp

try:
    import imageio_ffmpeg
    # yt-dlp 合并音视频/转音频要用 ffmpeg。传【完整 exe 路径】而不是目录:imageio 的二进制叫
    # ffmpeg-win-x86_64-v7.1.exe,不是标准 ffmpeg.exe;给目录 yt-dlp 会扫不到→静默跳过合并(留一堆 .f*.mp4/.m4a 碎片)。
    _FFEXE = imageio_ffmpeg.get_ffmpeg_exe()
except Exception:
    _FFEXE = None

YTDL_TASKS: dict = {}          # tid -> {id,url,title,status,progress,speed,eta,file,err,created}
_LOCK = threading.Lock()


def _now() -> str:
    return datetime.datetime.now().strftime("%H:%M:%S")


def _base_opts(proxy: str = "") -> dict:
    o = {"quiet": True, "no_warnings": True, "noprogress": True,
         "nocheckcertificate": True, "ignoreerrors": True,
         "retries": 5, "fragment_retries": 5, "socket_timeout": 30}
    if _FFEXE:
        o["ffmpeg_location"] = _FFEXE
    if proxy:
        o["proxy"] = proxy
    return o


def _cookie_domain(url: str) -> str:
    u = (url or "").lower()
    if "tiktok.com" in u:
        return ".tiktok.com"
    if "douyin.com" in u:
        return ".douyin.com"
    if "bilibili.com" in u:
        return ".bilibili.com"
    if "weibo.c" in u:
        return ".weibo.com"
    return ""


def _write_cookiefile(cookie_str: str, domain: str) -> str:
    """把 "k=v; k=v" 的 cookie 串写成 Netscape cookies.txt(yt-dlp 的抖音/TikTok 解析器要读 cookiejar)。"""
    lines = ["# Netscape HTTP Cookie File"]
    for part in (cookie_str or "").split(";"):
        part = part.strip()
        if "=" not in part:
            continue
        k, v = part.split("=", 1)
        k, v = k.strip(), v.strip()
        if not k:
            continue
        lines.append("\t".join([domain, "TRUE", "/", "FALSE", "2147483647", k, v]))
    fd, path = tempfile.mkstemp(suffix=".txt", prefix="ytck_")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return path


def _apply_cookies(o: dict, url: str, cookies: str):
    if not cookies:
        return
    dom = _cookie_domain(url)
    if not dom:
        return
    try:
        o["cookiefile"] = _write_cookiefile(cookies, dom)
    except Exception:
        pass


def extract(url: str, proxy: str = "", cookies: str = "") -> dict:
    """解析链接(不下载)。单条→视频信息;主页/播放列表→列出所有条目。"""
    url = (url or "").strip()
    if not url:
        return {"ok": False, "error": "链接是空的"}
    o = _base_opts(proxy)
    _apply_cookies(o, url, cookies)
    o["skip_download"] = True
    o["extract_flat"] = "in_playlist"      # 主页/列表只快速列条目,不逐条深挖(否则几百条会很慢)
    try:
        with yt_dlp.YoutubeDL(o) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as e:
        return {"ok": False, "error": str(e)[:220]}
    if not info:
        return {"ok": False, "error": "没解析到内容(链接不对 / 需登录 / 需连VPN代理)"}
    # 主页/播放列表/合集
    if info.get("_type") == "playlist" or info.get("entries") is not None:
        entries = []
        for e in (info.get("entries") or []):
            if not e:
                continue
            entries.append({
                "id": e.get("id", ""),
                "title": e.get("title") or e.get("id") or "(无标题)",
                "url": e.get("url") or e.get("webpage_url") or "",
                "duration": e.get("duration"),
                "thumbnail": e.get("thumbnail", ""),
            })
        return {"ok": True, "kind": "playlist",
                "title": info.get("title") or info.get("uploader") or "主页/播放列表",
                "uploader": info.get("uploader") or info.get("channel") or "",
                "count": len(entries), "entries": entries}
    # 单条视频
    return {"ok": True, "kind": "video",
            "title": info.get("title") or "(无标题)",
            "thumbnail": info.get("thumbnail", ""),
            "duration": info.get("duration"),
            "uploader": info.get("uploader") or info.get("channel") or "",
            "url": info.get("webpage_url") or url,
            "qualities": sorted(set(f["height"] for f in (info.get("formats") or []) if f.get("height")), reverse=True)}


def _fmt_for(quality: str, url: str = "") -> str:
    tt = "tiktok.com" in (url or "").lower()
    # TikTok 大坑:它的 HEVC(bytevc1/h265)格式其实是【哑的·没音轨】,但 yt-dlp 误把它标成带 aac。
    # 于是 best 会挑到分辨率最高的 720p HEVC → 下出来的视频没声音。只有 H264(h264/avc1)格式真带音轨。
    # 所以 TikTok 一律强制选带音轨的 H264:宁可 576p 有声,也不要 720p 没声(声音是刚需)。
    if quality == "audio":
        if tt:
            return "bestaudio[vcodec*=264]/best[vcodec*=264]/bestaudio/best"
        return "bestaudio/best"
    if tt:
        if str(quality).isdigit():
            h = int(quality)
            return f"best[vcodec*=264][height<={h}]/best[height<={h}]/best[vcodec*=264]/best"
        return "best[vcodec*=264]/best"
    if str(quality).isdigit():
        h = int(quality)
        return f"bestvideo[height<={h}]+bestaudio/best[height<={h}]/best"
    return "bestvideo+bestaudio/best"      # 默认最高清


def mk_task(url: str, title: str = "") -> str:
    tid = uuid.uuid4().hex[:12]
    with _LOCK:
        YTDL_TASKS[tid] = {"id": tid, "url": url, "title": title or url[:50],
                           "status": "queued", "progress": 0, "speed": "", "eta": "",
                           "file": "", "err": "", "created": _now()}
    return tid


def _hook(tid: str):
    def h(d):
        with _LOCK:
            t = YTDL_TASKS.get(tid)
            if not t:
                return
            stt = d.get("status")
            if stt == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                dl = d.get("downloaded_bytes") or 0
                t["status"] = "downloading"
                t["progress"] = round(dl / total * 100, 1) if total else 0
                sp = d.get("speed")
                t["speed"] = f"{sp / 1048576:.1f}MB/s" if sp else ""
                eta = d.get("eta")
                t["eta"] = f"{int(eta)}s" if eta else ""
                ttl = (d.get("info_dict") or {}).get("title")
                if ttl:
                    t["title"] = ttl
            elif stt == "finished":
                t["status"] = "merging"        # 分段下完→ffmpeg 合并/转码中
                t["progress"] = 99
    return h


def download_one(url: str, quality: str = "best", save_dir: str = ".", proxy: str = "",
                 cookies: str = "", tid: str = "") -> bool:
    """下单条(阻塞·放线程里跑)。成功返回 True。进度/结果写进 YTDL_TASKS[tid]。"""
    tid = tid or mk_task(url)
    try:
        os.makedirs(save_dir, exist_ok=True)
    except Exception:
        pass
    o = _base_opts(proxy)
    _apply_cookies(o, url, cookies)
    o.update({
        "format": _fmt_for(quality, url),
        # 关键:优先挑 H264 视频 + AAC 音频。否则 YouTube 等站的 bestvideo+bestaudio 会挑到
        # av1 视频 + opus 音频塞进 mp4——文件里其实有音轨,但 Windows 自带播放器/多数手机
        # 解不了 opus-in-mp4/av1 → 放出来【有画面没声音】。h264+aac 是各处都能放的万能组合。
        "format_sort": ["vcodec:h264", "acodec:aac"],
        "outtmpl": os.path.join(save_dir, "%(title).80B [%(id)s].%(ext)s"),
        "merge_output_format": "mp4",
        "progress_hooks": [_hook(tid)],
        "windowsfilenames": True,
        "noplaylist": True,               # 单条就是单条,别被 ?list= 带出一整个列表
        "concurrent_fragment_downloads": 4,
    })
    if quality == "audio":
        o["postprocessors"] = [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}]
    try:
        with yt_dlp.YoutubeDL(o) as ydl:
            info = ydl.extract_info(url, download=True)
        if not info:
            raise RuntimeError("下载失败(没拿到内容,可能需要登录/代理)")
        fp = ""
        try:
            reqs = info.get("requested_downloads") or []
            if reqs:
                fp = reqs[0].get("filepath") or reqs[0].get("_filename") or ""
        except Exception:
            pass
        with _LOCK:
            t = YTDL_TASKS.get(tid)
            if t:
                t["status"] = "done"
                t["progress"] = 100
                t["file"] = fp
                t["title"] = info.get("title") or t["title"]
        return True
    except Exception as e:
        with _LOCK:
            t = YTDL_TASKS.get(tid)
            if t:
                t["status"] = "failed"
                t["err"] = str(e)[:220]
        return False


def tasks_snapshot() -> list:
    with _LOCK:
        return sorted(YTDL_TASKS.values(), key=lambda x: x.get("created", ""), reverse=True)


def clear_finished():
    with _LOCK:
        for k in [k for k, t in YTDL_TASKS.items() if t.get("status") in ("done", "failed")]:
            YTDL_TASKS.pop(k, None)
