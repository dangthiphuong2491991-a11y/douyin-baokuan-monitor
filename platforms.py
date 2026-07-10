# -*- coding: utf-8 -*-
"""平台适配层：抹平各平台（抖音/TikTok/…）的差异，对上层提供统一接口。
上层核心（监控/发现/下载/播放）只跟"归一化 item"打交道，不关心是哪个平台。

统一 item（normalize 后）字段：
  platform, aweme_id, desc, author_id, author_name,
  create_time(int秒), digg, comment, share, collect,
  duration_ms, is_images, cover, video_url, image_urls[], mix{mix_id,mix_name,total}|None, web_url, raw
"""
import re
import time

import httpx

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")

# ---- 抖音（可直接 import，无联网副作用）----
from f2.apps.douyin.handler import DouyinHandler
from f2.apps.douyin.utils import (TokenManager as DyToken,
                                   SecUserIdFetcher as DySecFetcher,
                                   AwemeIdFetcher as DyAwemeFetcher)


# 禁用 f2 自带 Bark 通知（失败重试拖慢速度）
async def _no_bark(self, *a, **k):
    return None


try:
    DouyinHandler._send_bark_notification = _no_bark
except Exception:
    pass


# ---- TikTok 懒加载 ----
# 注意：f2.apps.tiktok 一 import 就会联网请求 TikTok（DeviceIdManager 在类定义时调 gen_real_msToken），
# 没 VPN 会失败。所以必须懒加载，只在真正用 TikTok 时才 import，不连累抖音用户。
_TT = {}


def _tt():
    """懒加载 TikTok 模块（需能访问 tiktok.com，即挂着 VPN）。返回 dict 或抛异常。"""
    if _TT:
        return _TT
    from f2.apps.tiktok.handler import TiktokHandler
    from f2.apps.tiktok.utils import (TokenManager as TtToken,
                                       SecUserIdFetcher as TtSecFetcher,
                                       AwemeIdFetcher as TtAwemeFetcher)
    try:
        TiktokHandler._send_bark_notification = _no_bark
    except Exception:
        pass
    _TT.update(Handler=TiktokHandler, Token=TtToken, Sec=TtSecFetcher, Aweme=TtAwemeFetcher)
    return _TT


class BaseAdapter:
    key = ""
    name = ""
    id_prefix = ""          # 用户 id 前缀，用来直接识别粘贴的 id
    login_hint = ""
    home_url = ""

    def make_kwargs(self, cookie: str = "") -> dict:
        raise NotImplementedError

    def handler(self, cookie: str = ""):
        raise NotImplementedError

    async def resolve_user_id(self, url: str) -> str:
        raise NotImplementedError

    async def resolve_aweme_id(self, url: str) -> str:
        raise NotImplementedError

    # 抓取（返回原始 aweme dict 列表 / 单个）
    async def profile(self, cookie, uid) -> dict:
        raise NotImplementedError

    async def posts(self, cookie, uid, max_count=20) -> list:
        raise NotImplementedError

    async def one_video(self, cookie, aid) -> dict:
        raise NotImplementedError

    async def mix(self, cookie, mix_id, max_count=500) -> list:
        raise NotImplementedError

    # 归一化：把平台原始 aweme dict 变成统一 item
    def normalize(self, a: dict) -> dict:
        raise NotImplementedError


# ======================= 抖音 =======================
class DouyinAdapter(BaseAdapter):
    key = "douyin"
    name = "抖音"
    id_prefix = "MS4wLjAB"
    login_hint = "扫码 / 从浏览器导入 / 粘贴 Cookie"
    home_url = "https://www.douyin.com/"
    _AID_RE = r"(?:modal_id=|/video/|/note/|/share/video/|/share/note/)(\d{6,})"

    def make_kwargs(self, cookie: str = "") -> dict:
        if cookie and "sessionid" in cookie:
            ck = cookie
        else:
            try:
                mst = DyToken.gen_real_msToken()
            except Exception:
                mst = ""
            ck = f"ttwid={DyToken.gen_ttwid()};"
            if mst:
                ck += f" msToken={mst};"
        return {"headers": {"User-Agent": UA, "Referer": "https://www.douyin.com/"},
                "cookie": ck, "proxies": {"http://": None, "https://": None},
                "mode": "post", "timeout": 30}

    def handler(self, cookie: str = ""):
        return DouyinHandler(self.make_kwargs(cookie))

    async def resolve_user_id(self, url: str) -> str:
        return await DySecFetcher.get_sec_user_id(url)

    async def resolve_aweme_id(self, url: str) -> str:
        text = (url or "").strip()
        if re.fullmatch(r"\d{6,}", text):
            return text
        m = re.search(r"https?://\S+", text)
        if not m:
            return None
        link = m.group(0).rstrip("，。、）)]】")
        probe = link
        if not re.search(self._AID_RE, probe):
            try:
                async with httpx.AsyncClient(follow_redirects=True, timeout=20,
                                             headers={"User-Agent": UA}) as c:
                    probe = str((await c.get(link)).url)
            except Exception:
                probe = link
        mm = re.search(self._AID_RE, probe)
        if mm:
            return mm.group(1)
        try:
            return await DyAwemeFetcher.get_aweme_id(link)
        except Exception:
            return None

    async def profile(self, cookie, uid) -> dict:
        p = await self.handler(cookie).fetch_user_profile(uid)
        return {"nickname": p.nickname_raw or p.nickname, "avatar": p.avatar_url,
                "follower_count": p.follower_count, "aweme_count": p.aweme_count,
                "signature": (p.signature_raw or "")[:100]}

    async def posts(self, cookie, uid, max_count=20) -> list:
        kw = self.make_kwargs(cookie)
        if max_count > 20:
            kw["timeout"] = 5
        h = DouyinHandler(kw)
        out = []
        try:
            async for posts in h.fetch_user_post_videos(sec_user_id=uid, page_counts=20, max_counts=max_count):
                out.extend(posts._to_raw().get("aweme_list") or [])
                if len(out) >= max_count:
                    break
        except UnboundLocalError:
            pass   # f2 bug：空作品号收尾发通知时引用未定义的 nickname_raw；作品已收完，忽略
        return out[:max_count]

    async def posts_pages(self, cookie, uid, max_pages=8, page_timeout=5):
        """按页产出（供时间窗智能停用）"""
        kw = self.make_kwargs(cookie)
        kw["timeout"] = page_timeout
        h = DouyinHandler(kw)
        try:
            async for posts in h.fetch_user_post_videos(sec_user_id=uid, page_counts=20, max_counts=max_pages * 20):
                yield posts._to_raw().get("aweme_list") or []
        except UnboundLocalError:
            return   # 同上：f2 收尾通知崩，作品已产出完，正常结束

    async def one_video(self, cookie, aid) -> dict:
        v = await self.handler(cookie).fetch_one_video(aweme_id=str(aid))
        return (v._to_raw() or {}).get("aweme_detail")

    async def mix(self, cookie, mix_id, max_count=500) -> list:
        kw = self.make_kwargs(cookie)
        kw["timeout"] = 10
        h = DouyinHandler(kw)
        out = []
        try:
            async for mx in h.fetch_user_mix_videos(mix_id=mix_id, page_counts=20, max_counts=max_count):
                out.extend(mx._to_raw().get("aweme_list") or [])
        except UnboundLocalError:
            pass
        return out

    def normalize(self, a: dict) -> dict:
        stats = a.get("statistics") or {}
        video = a.get("video") or {}
        aid = str(a.get("aweme_id"))
        # 无水印地址：最高码率
        vurl = None
        best = -1
        for r in (video.get("bit_rate") or []):
            urls = ((r.get("play_addr") or {}).get("url_list")) or []
            if urls and (r.get("bit_rate", 0) > best):
                vurl, best = urls[0], r.get("bit_rate", 0)
        if not vurl:
            urls = ((video.get("play_addr") or {}).get("url_list")) or []
            vurl = urls[-1] if urls else None
        cover = None
        for k in ("cover", "origin_cover", "dynamic_cover"):
            urls = ((video.get(k) or {}).get("url_list")) or []
            if urls:
                cover = urls[0]
                break
        imgs = []
        for im in (a.get("images") or []):
            urls = im.get("url_list") or []
            if urls:
                imgs.append(urls[-1])
        mi = a.get("mix_info") or {}
        mix = None
        episode = None
        if mi.get("mix_id"):
            st = mi.get("statis") or {}
            episode = st.get("current_episode")   # 真实集数（付费/隐藏集被跳过也不错位）
            mix = {"mix_id": mi["mix_id"], "mix_name": mi.get("mix_name") or "合集",
                   "total": st.get("updated_to_episode") or st.get("total_episode") or 0}
        author = a.get("author") or {}
        return {
            "platform": self.key, "aweme_id": aid,
            "desc": (a.get("desc") or "").strip(),
            "author_id": author.get("sec_uid") or "", "author_name": author.get("nickname") or "",
            "author_follower": author.get("follower_count") or 0,
            "create_time": a.get("create_time") or 0,
            "digg": stats.get("digg_count", 0), "comment": stats.get("comment_count", 0),
            "share": stats.get("share_count", 0), "collect": stats.get("collect_count", 0),
            "duration_ms": video.get("duration") or 0,
            "is_images": bool(a.get("images")),
            "cover": cover or "", "video_url": vurl, "image_urls": imgs, "mix": mix, "episode": episode,
            "web_url": f"https://www.douyin.com/video/{aid}", "raw": a,
        }


# ======================= TikTok（照 f2 接口写好，需挂 VPN 实测）=======================
class TiktokAdapter(BaseAdapter):
    key = "tiktok"
    name = "TikTok"
    id_prefix = "MS4wLjAB"   # TikTok secUid 也是 MS4 开头
    login_hint = "从浏览器导入 / 粘贴 Cookie（需已登录 tiktok.com）"
    home_url = "https://www.tiktok.com/"

    def make_kwargs(self, cookie: str = "") -> dict:
        Token = _tt()["Token"]
        if cookie and "sessionid" in cookie:
            ck = cookie
        else:
            try:
                mst = Token.gen_real_msToken()
            except Exception:
                mst = ""
            ck = f"ttwid={Token.gen_ttwid()};"
            if mst:
                ck += f" msToken={mst};"
        # 走系统全局 VPN：不单独设代理，用系统网络
        return {"headers": {"User-Agent": UA, "Referer": "https://www.tiktok.com/"},
                "cookie": ck, "proxies": {"http://": None, "https://": None},
                "mode": "post", "timeout": 30}

    def handler(self, cookie: str = ""):
        return _tt()["Handler"](self.make_kwargs(cookie))

    async def resolve_user_id(self, url: str) -> str:
        return await _tt()["Sec"].get_secuid(url)

    async def resolve_aweme_id(self, url: str) -> str:
        text = (url or "").strip()
        if re.fullmatch(r"\d{6,}", text):
            return text
        m = re.search(r"https?://\S+", text)
        link = m.group(0) if m else text
        try:
            return await _tt()["Aweme"].get_aweme_id(link)
        except Exception:
            return None

    async def profile(self, cookie, uid) -> dict:
        p = await self.handler(cookie).fetch_user_profile(secUid=uid)
        return {"nickname": getattr(p, "nickname_raw", None) or p.nickname,
                "avatar": getattr(p, "avatar_url", "") or getattr(p, "avatar", ""),
                "follower_count": getattr(p, "follower_count", "") or getattr(p, "followerCount", ""),
                "aweme_count": getattr(p, "aweme_count", "") or getattr(p, "videoCount", ""),
                "signature": (getattr(p, "signature", "") or "")[:100]}

    async def posts(self, cookie, uid, max_count=20) -> list:
        h = self.handler(cookie)
        out = []
        try:
            async for posts in h.fetch_user_post_videos(secUid=uid, cursor=0, min_cursor=0,
                                                        page_counts=20, max_counts=max_count):
                out.extend(posts._to_raw().get("itemList") or [])
                if len(out) >= max_count:
                    break
        except UnboundLocalError:
            pass   # 同抖音：f2 空作品号收尾通知 bug，作品已收完，忽略
        return out[:max_count]

    async def posts_pages(self, cookie, uid, max_pages=8, page_timeout=5):
        kw = self.make_kwargs(cookie)
        kw["timeout"] = page_timeout
        h = _tt()["Handler"](kw)
        try:
            async for posts in h.fetch_user_post_videos(secUid=uid, cursor=0, min_cursor=0,
                                                        page_counts=20, max_counts=max_pages * 20):
                yield posts._to_raw().get("itemList") or []
        except UnboundLocalError:
            return

    async def one_video(self, cookie, aid) -> dict:
        v = await self.handler(cookie).fetch_one_video(itemId=str(aid))
        raw = v._to_raw() or {}
        return (raw.get("itemInfo") or {}).get("itemStruct") or raw.get("aweme_detail")

    async def mix(self, cookie, mix_id, max_count=500) -> list:
        h = self.handler(cookie)
        out = []
        try:
            async for mx in h.fetch_user_mix_videos(mixId=mix_id, cursor=0, page_counts=20, max_counts=max_count):
                out.extend(mx._to_raw().get("itemList") or [])
        except UnboundLocalError:
            pass
        return out

    def normalize(self, a: dict) -> dict:
        stats = a.get("stats") or a.get("statsV2") or {}
        video = a.get("video") or {}
        author = a.get("author") or {}
        aid = str(a.get("id") or a.get("aweme_id") or "")
        # TikTok 无水印：优先 playAddr（登录态下多为无水印）
        vurl = video.get("playAddr") or video.get("downloadAddr") or ""
        cover = video.get("cover") or video.get("originCover") or video.get("dynamicCover") or ""
        imgs = []
        img_post = a.get("imagePost") or {}
        for im in (img_post.get("images") or []):
            u = ((im.get("imageURL") or {}).get("urlList")) or []
            if u:
                imgs.append(u[0])

        def _num(v):
            try:
                return int(v)
            except Exception:
                return 0
        return {
            "platform": self.key, "aweme_id": aid,
            "desc": (a.get("desc") or "").strip(),
            "author_id": author.get("secUid") or author.get("uniqueId") or "",
            "author_name": author.get("nickname") or author.get("uniqueId") or "",
            "author_follower": _num((a.get("authorStats") or {}).get("followerCount")
                                    or author.get("followerCount")),
            "create_time": _num(a.get("createTime")),
            "digg": _num(stats.get("diggCount")), "comment": _num(stats.get("commentCount")),
            "share": _num(stats.get("shareCount")), "collect": _num(stats.get("collectCount")),
            "duration_ms": _num(video.get("duration")) * 1000,   # TikTok 是秒
            "is_images": bool(img_post.get("images")),
            "cover": cover, "video_url": vurl, "image_urls": imgs, "mix": None, "episode": None,
            "web_url": f"https://www.tiktok.com/@{author.get('uniqueId','')}/video/{aid}", "raw": a,
        }


ADAPTERS = {a.key: a for a in (DouyinAdapter(), TiktokAdapter())}
PLATFORM_LIST = [{"key": a.key, "name": a.name} for a in ADAPTERS.values()]


def get_adapter(platform: str) -> BaseAdapter:
    return ADAPTERS.get(platform) or ADAPTERS["douyin"]
