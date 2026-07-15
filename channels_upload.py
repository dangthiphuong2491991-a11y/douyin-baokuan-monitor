# -*- coding: utf-8 -*-
"""
视频号 纯后端上传+发布(照真实抓包/官方上传SDK源码逆向复刻,零点击、零浏览器驱动)。

全链路(全部普通 HTTP,无任何客户端加密签名):
  1. helper_upload_params        → authKey(CDN上传鉴权) + uin + 文件类型(视频20302/图片20304)
  2. CDN 分片上传(视频、封面各一次):
       PUT  {cdnHost}/applyuploaddfs                          → {UploadID}
       PUT  {cdnHost}/uploadpartdfs?PartNumber=N&UploadID=..  → 每片(Content-MD5=hex md5)
       POST {cdnHost}/completepartuploaddfs?UploadID=..       → {DownloadURL}
  3. post_clip_video(视频url)     → {clipKey, draftId}
  4. post_clip_video_result       → {flag:2}(转码就绪)
  5. post_create(clipKey+媒资+objectDesc.component挂剧集)  → 发布
     （开发验证用 post_draft，同结构、只存草稿不发布）

鉴权:cookies(sessionid/wxuin) + 头 finger-print-device-id + X-WECHAT-UIN。
     (由 Electron /authmat 或 webview 导出一次即可,相对稳定)
"""
import json
import time
import uuid
import hashlib
import subprocess
from urllib.parse import quote

import httpx
import imageio_ffmpeg

_FF = imageio_ffmpeg.get_ffmpeg_exe()
# 打包版后端 console=False 没有控制台：不加这个标志，每跑一次 ffmpeg 客户屏幕上就弹一个黑窗
_NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36")
_BASE = "https://channels.weixin.qq.com"
_CDN_HOSTS = [
    "finderassistancea.video.qq.com", "finderassistanceb.video.qq.com",
    "finderassistancec.video.qq.com", "finderassistanced.video.qq.com",
]
_CHUNK = 8 * 1024 * 1024   # 8MB/片(视频号SDK视频版分片大小)


class ChannelsAuth:
    """一个账号的活会话鉴权材料。"""
    def __init__(self, cookies: dict, fingerprint: str, uin: str):
        self.cookies = dict(cookies or {})      # {name: value}，至少含 sessionid/wxuin
        self.fp = fingerprint or ""
        self.uin = str(uin or "")

    @property
    def cookie_header(self) -> str:
        return "; ".join(f"{k}={v}" for k, v in self.cookies.items())


def _mm_headers(auth: ChannelsAuth, referer="/platform/post/create") -> dict:
    return {
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "Referer": _BASE + referer,
        "User-Agent": _UA,
        "X-WECHAT-UIN": auth.uin,
        "finger-print-device-id": auth.fp,
        "Cookie": auth.cookie_header,
    }


def _rid() -> str:
    return uuid.uuid4().hex[:8] + "-" + uuid.uuid4().hex[:8]


def _mm_post(client: httpx.Client, auth: ChannelsAuth, path: str, body: dict,
             referer="/platform/post/create", micro=False) -> dict:
    """打一个 mmfinderassistant 接口。micro=True 走 /micro/content 前缀(post_clip_video 用)。"""
    prefix = "/micro/content" if micro else ""
    url = (f"{_BASE}{prefix}/cgi-bin/mmfinderassistant-bin{path}"
           f"?_aid={uuid.uuid4()}&_rid={_rid()}"
           f"&_pageUrl={quote(_BASE + prefix + '/post/create', safe='')}")
    ref = ("/micro/content/post/create" if micro else referer)
    r = client.post(url, content=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                    headers=_mm_headers(auth, ref), timeout=60)
    try:
        return r.json()
    except Exception:
        return {"errCode": -999, "errMsg": "非JSON响应", "_raw": r.text[:300], "_status": r.status_code}


# ---------------- 1) 拿上传鉴权参数 ----------------
def helper_upload_params(client: httpx.Client, auth: ChannelsAuth) -> dict:
    j = _mm_post(client, auth, "/helper/helper_upload_params",
                 {"timestamp": str(int(time.time() * 1000)), "_log_finder_uin": "",
                  "_log_finder_id": "", "rawKeyBuff": "", "pluginSessionId": None,
                  "scene": 7, "reqScene": 7})
    if j.get("errCode") != 0:
        raise RuntimeError("helper_upload_params 失败: " + json.dumps(j, ensure_ascii=False)[:200])
    return j["data"]   # authKey, uin, appType, videoFileType, pictureFileType, thumbFileType...


# ---------------- 2) CDN 分片上传 ----------------
def _md5_hex(b: bytes) -> str:
    return hashlib.md5(b).hexdigest()


def _xargs(app_type, filetype, uin, filename, filesize, taskid, scene=2) -> str:
    return (f"apptype={app_type}&filetype={filetype}&weixinnum={uin}"
            f"&filekey={quote(filename)}&filesize={filesize}&taskid={taskid}&scene={scene}")


def cdn_upload(client: httpx.Client, up: dict, data: bytes, filename: str,
               filetype: int, host: str = None) -> str:
    """把 data 分片传上 CDN,返回最终 DownloadURL(已换成 finder.video.qq.com 高可用域)。"""
    host = host or _CDN_HOSTS[int(time.time()) % len(_CDN_HOSTS)]
    authKey, uin, app_type = up["authKey"], up["uin"], up.get("appType", 251)
    taskid = str(uuid.uuid4())
    parts = [data[i:i + _CHUNK] for i in range(0, len(data), _CHUNK)] or [b""]
    block_len = [len(p) for p in parts]
    xa = _xargs(app_type, filetype, uin, filename, len(data), taskid)
    base = {"Authorization": authKey, "X-Arguments": xa, "User-Agent": _UA,
            "Referer": _BASE + "/", "Accept": "application/json, text/plain, */*"}

    # a) applyuploaddfs → UploadID
    r = client.put(f"https://{host}/applyuploaddfs",
                   content=json.dumps({"BlockSum": len(parts), "BlockPartLength": block_len}).encode(),
                   headers={**base, "Content-Type": "application/json", "Content-MD5": "null"}, timeout=60)
    jid = r.json()
    upload_id = jid.get("UploadID")
    if not upload_id:
        raise RuntimeError("applyuploaddfs 未返回 UploadID: " + json.dumps(jid, ensure_ascii=False)[:200])

    # b) 逐片 PUT uploadpartdfs
    part_info = []
    for i, p in enumerate(parts, start=1):
        md5 = _md5_hex(p)
        client.put(f"https://{host}/uploadpartdfs?PartNumber={i}&UploadID={upload_id}&QuickUpload=0",
                   content=p, headers={**base, "Content-Type": "application/octet-stream", "Content-MD5": md5},
                   timeout=300)
        part_info.append({"PartNumber": i, "ETag": f'"{md5}"'})

    # c) completepartuploaddfs → DownloadURL
    r = client.post(f"https://{host}/completepartuploaddfs?UploadID={upload_id}",
                    content=json.dumps({"TransFlag": "0_0", "PartInfo": part_info}).encode(),
                    headers={**base, "Content-Type": "application/json", "Content-MD5": "null"}, timeout=120)
    jc = r.json()
    dl = jc.get("DownloadURL")
    if not dl:
        raise RuntimeError("completepartuploaddfs 未返回 DownloadURL: " + json.dumps(jc, ensure_ascii=False)[:200])
    # 页面统一把 http://wxapp.tc.qq.com 换成 https://finder.video.qq.com(同 encfilekey/token)
    dl = dl.replace("http://wxapp.tc.qq.com", "https://finder.video.qq.com")
    dl = dl.replace("https://wxapp.tc.qq.com", "https://finder.video.qq.com")
    return dl


# ---------------- 封面 + 尺寸(ffmpeg) ----------------
def probe_video(path: str) -> dict:
    """用 ffmpeg 读时长/宽高(解析 stderr,不依赖 ffprobe)。"""
    try:
        r = subprocess.run([_FF, "-hide_banner", "-i", path], capture_output=True, timeout=60,
                           creationflags=_NOWIN)
        err = r.stderr.decode("utf-8", "ignore")
    except Exception:
        err = ""
    import re
    w = h = 0
    dur = 0.0
    m = re.search(r"(\d{2,5})x(\d{2,5})", err)
    if m:
        w, h = int(m.group(1)), int(m.group(2))
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.\d+)", err)
    if m:
        dur = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    return {"width": w, "height": h, "duration": round(dur, 6)}


def make_cover(path: str, out_jpg: str, at_sec: float = 0.5) -> bytes:
    """截取一帧做封面 JPEG,返回字节。"""
    subprocess.run([_FF, "-y", "-hide_banner", "-ss", str(at_sec), "-i", path,
                    "-vframes", "1", "-q:v", "3", out_jpg], capture_output=True, timeout=60,
                   creationflags=_NOWIN)
    with open(out_jpg, "rb") as f:
        return f.read()


# ---------------- 3/4) 登记视频 ----------------
def post_clip_video(client: httpx.Client, auth: ChannelsAuth, video_url: str,
                    width: int, height: int) -> dict:
    body = {"url": video_url, "timeStart": 0, "cropDuration": 0,
            "height": height, "width": width, "x": 0, "y": 0}
    j = _mm_post(client, auth, "/post/post_clip_video", body, micro=True)
    if j.get("errCode") != 0:
        raise RuntimeError("post_clip_video 失败: " + json.dumps(j, ensure_ascii=False)[:200])
    return j["data"]   # {clipKey, draftId}


def post_clip_video_result(client: httpx.Client, auth: ChannelsAuth, clip_key: str, draft_id: str) -> dict:
    body = {"clipKey": clip_key, "draftId": draft_id, "timestamp": str(int(time.time() * 1000)),
            "_log_finder_uin": "", "_log_finder_id": "", "rawKeyBuff": "", "pluginSessionId": None,
            "scene": 7, "reqScene": 7}
    return _mm_post(client, auth, "/post/post_clip_video_result", body, micro=True)


# ---------------- 5) 组 post_create 请求体 ----------------
def _finder_topic_info(title: str) -> str:
    return ("<finder><version>1</version><valuecount>1</valuecount><style><at></at></style>"
            f"<value0><![CDATA[{title}]]></value0></finder>")


def build_post_body(clip_key, video_url, cover_url, width, height, duration, filesize,
                    title, drama=None):
    """按真实抓包结构组 post_create/post_draft 的请求体。drama={id,title} 时挂剧集。"""
    media = [{
        "url": video_url, "fileSize": filesize,
        "thumbUrl": cover_url, "fullThumbUrl": cover_url,
        "mediaType": 4, "videoPlayLen": duration,
        "width": width, "height": height,
        "md5sum": str(uuid.uuid4()),
        "coverUrl": cover_url, "fullCoverUrl": cover_url,
        "urlCdnTaskId": clip_key,
    }]
    object_desc = {
        "mpTitle": "", "description": title or "", "extReading": {},
        "mediaType": 4, "location": {"latitude": 0, "longitude": 0, "city": "", "poiClassifyId": ""},
        "topic": {"finderTopicInfo": _finder_topic_info(title or "")},
        "event": {}, "mentionedUser": [], "media": media, "member": {},
    }
    if drama and drama.get("id"):
        object_desc["component"] = {"id": drama["id"], "type": 8, "title": drama.get("title") or drama["id"]}
    return {
        "objectType": 0, "longitude": 0, "latitude": 0, "feedLongitude": 0, "feedLatitude": 0,
        "originalFlag": 0, "topics": [], "isFullPost": 1, "handleFlag": 2,
        "videoClipTaskId": clip_key,
        "traceInfo": {"traceKey": "FPT_%d_%d" % (int(time.time()), int(time.time()) % 100000000),
                      "uploadCdnStart": int(time.time()), "uploadCdnEnd": int(time.time())},
        "objectDesc": object_desc,
        "report": {"clipKey": clip_key, "draftId": clip_key, "timestamp": str(int(time.time() * 1000)),
                   "_log_finder_uin": "", "_log_finder_id": "", "rawKeyBuff": "", "pluginSessionId": None,
                   "scene": 7, "reqScene": 7, "height": height, "width": width, "duration": duration,
                   "fileSize": filesize, "uploadCost": 0},
        "postFlag": 0, "mode": 1, "clientid": str(uuid.uuid4()),
        "timestamp": str(int(time.time() * 1000)), "_log_finder_uin": "", "_log_finder_id": "",
        "rawKeyBuff": "", "pluginSessionId": None, "scene": 7, "reqScene": 7,
    }


def post_create(client: httpx.Client, auth: ChannelsAuth, body: dict, draft: bool = False) -> dict:
    """draft=True → post_draft(只存草稿、不发布,开发验证用);False → post_create(真发布)。
    实测:post_draft 走 /micro/content 前缀、body 外包一层 {"postReq":...};post_create 扁平、不走 micro。"""
    if draft:
        return _mm_post(client, auth, "/post/post_draft", {"postReq": body}, micro=True)
    return _mm_post(client, auth, "/post/post_create", body, micro=False)


# ---------------- 顶层编排 ----------------
def publish_video(auth: ChannelsAuth, video_path: str, title: str, drama: dict = None,
                  draft: bool = False, cover_path: str = None, log=print) -> dict:
    """把一个视频文件纯后端上传并发布(draft=True 时只存草稿)。返回 post_create/post_draft 响应。"""
    import os
    with httpx.Client(http2=False, verify=False, follow_redirects=True) as client:
        log("① helper_upload_params…")
        up = helper_upload_params(client, auth)
        vftype, pftype = up.get("videoFileType", 20302), up.get("pictureFileType", 20304)

        log("② 探测尺寸 + 生成封面…")
        meta = probe_video(video_path)
        w, h, dur = meta["width"], meta["height"], meta["duration"]
        cov_jpg = cover_path or (video_path + ".cover.jpg")
        if cover_path:
            cover_bytes = make_cover(video_path, cov_jpg)
        else:
            # 【防矩阵连坐】没指定封面时不再固定取 0.5s 帧——每条从不同随机时刻抽帧,
            # 让封面图哈希逐条不同,打散平台"同封面聚簇"的矩阵连坐钩子。
            import random
            at = round(random.uniform(dur * 0.15, dur * 0.75), 2) if dur and dur > 2 else 0.5
            cover_bytes = make_cover(video_path, cov_jpg, at_sec=at)

        with open(video_path, "rb") as f:
            vbytes = f.read()
        fsize = len(vbytes)

        log(f"③ 上传视频到CDN({fsize/1048576:.1f}MB)…")
        video_url = cdn_upload(client, up, vbytes, os.path.basename(video_path), vftype)
        log("   视频url: " + video_url[:80] + "…")

        log("④ 上传封面到CDN…")
        cover_url = cdn_upload(client, up, cover_bytes, "finder_video_img.jpeg", pftype)

        log("⑤ post_clip_video 登记…")
        clip = post_clip_video(client, auth, video_url, w, h)
        clip_key = clip["clipKey"]
        post_clip_video_result(client, auth, clip_key, clip.get("draftId", clip_key))
        log("   clipKey=" + str(clip_key))

        log(f"⑥ {'post_draft(存草稿)' if draft else 'post_create(发布)'} …")
        body = build_post_body(clip_key, video_url, cover_url, w, h, dur, fsize, title, drama)
        resp = post_create(client, auth, body, draft=draft)
        log("   → " + json.dumps(resp, ensure_ascii=False)[:200])
        return resp
