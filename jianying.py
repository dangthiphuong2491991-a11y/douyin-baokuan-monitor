# -*- coding: utf-8 -*-
"""剪映草稿引擎：调剪映自己的 videoeditor.dll（EncryptUtils）解密/加密草稿，
读草稿列表、把视频混剪进模板主轨道生成新草稿。原理同 jy-draftc / 剪大神。
只在 Windows + 装了剪映时可用；DLL 用【当前安装版剪映】自己的，天然跟版本走。"""
import ctypes as C
import glob
import json
import os
import re

_DEC_SYM = ("?decrypt@EncryptUtils@lvve@@QEAA?AV?$basic_string@DU?$char_traits@D@std@@"
            "V?$allocator@D@2@@std@@AEBV34@0AEA_N@Z")
_ENC_SYM = ("?encrypt@EncryptUtils@lvve@@QEAA?AV?$basic_string@DU?$char_traits@D@std@@"
            "V?$allocator@D@2@@std@@AEBV34@@Z")
_ENABLE_SYM = "?enable@EncryptUtils@lvve@@QEAAX_N@Z"

_STATE = {"dll": None, "dec": None, "enc": None, "dir": None}

# 烤进软件的"效果源"：做剪映模版不再依赖剪映里的某个草稿，特效/滤镜/调整/HSL 都从这份自带壳里克隆
BASE_SHELL_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "base_shell")


def find_install_dir() -> str:
    """找剪映安装目录（含 videoeditor.dll），取版本号最大的。"""
    pats = [r"C:\Program Files\JianyingPro\*\videoeditor.dll",
            r"C:\Program Files (x86)\JianyingPro\*\videoeditor.dll",
            r"D:\**\JianyingPro\*\videoeditor.dll",
            os.path.expandvars(r"%LOCALAPPDATA%\JianyingPro\Apps\*\videoeditor.dll")]
    hits = []
    for p in pats:
        try:
            hits += glob.glob(p, recursive=True)
        except Exception:
            pass
    hits = sorted(set(hits))
    return os.path.dirname(hits[-1]) if hits else ""


def draft_root() -> str:
    return os.path.expandvars(
        r"%LOCALAPPDATA%\JianyingPro\User Data\Projects\com.lveditor.draft")


def _mk_str(content: bytes):
    """x64 MSVC std::string（32字节）。返回(32字节buffer, keepalive)。"""
    b = (C.c_char * 32)()
    n = len(content)
    if n < 16:
        C.memmove(b, content, n)
        C.memmove(C.byref(b, 16), C.byref(C.c_size_t(n)), 8)
        C.memmove(C.byref(b, 24), C.byref(C.c_size_t(15)), 8)
        return b, None
    heap = C.create_string_buffer(content, n + 1)
    C.memmove(b, C.byref(C.c_void_p(C.addressof(heap))), 8)
    C.memmove(C.byref(b, 16), C.byref(C.c_size_t(n)), 8)
    C.memmove(C.byref(b, 24), C.byref(C.c_size_t(n)), 8)
    return b, heap


def _rd_str(b) -> bytes:
    res = C.c_size_t.from_buffer_copy(bytes(b[24:32])).value
    size = C.c_size_t.from_buffer_copy(bytes(b[16:24])).value
    if res >= 16:
        ptr = C.c_void_p.from_buffer_copy(bytes(b[0:8])).value
        return C.string_at(ptr, size)
    return bytes(b[0:size])


def _ensure_dll():
    if _STATE["dll"] is not None:
        return
    d = find_install_dir()
    if not d:
        raise RuntimeError("没找到剪映安装目录（videoeditor.dll）。请确认装了剪映专业版。")
    os.add_dll_directory(d)
    dll = C.WinDLL(os.path.join(d, "videoeditor.dll"), winmode=0x00000008)
    dec = C.CFUNCTYPE(C.c_void_p, C.c_void_p, C.c_void_p, C.c_void_p, C.c_void_p, C.c_void_p)(
        C.cast(dll[_DEC_SYM], C.c_void_p).value)
    enc = C.CFUNCTYPE(C.c_void_p, C.c_void_p, C.c_void_p, C.c_void_p)(
        C.cast(dll[_ENC_SYM], C.c_void_p).value)
    try:
        en = C.CFUNCTYPE(None, C.c_void_p, C.c_bool)(C.cast(dll[_ENABLE_SYM], C.c_void_p).value)
        en(0, True)
    except Exception:
        pass
    _STATE.update(dll=dll, dec=dec, enc=enc, dir=d)


def decrypt(cipher: bytes) -> str:
    _ensure_dll()
    in_b, _k = _mk_str(cipher)
    pm_b, _k2 = _mk_str(b"{}")
    out_b = (C.c_char * 32)()
    good = C.c_bool(False)
    _STATE["dec"](0, C.addressof(out_b), C.addressof(in_b), C.addressof(pm_b), C.addressof(good))
    if not good.value:
        raise RuntimeError("解密失败（版本不匹配或非本机草稿）")
    return _rd_str(out_b).decode("utf-8", "ignore")


def encrypt(plain: str) -> bytes:
    _ensure_dll()
    in_b, _k = _mk_str(plain.encode("utf-8"))
    out_b = (C.c_char * 32)()
    _STATE["enc"](0, C.addressof(out_b), C.addressof(in_b))
    return _rd_str(out_b)


def _index_path() -> str:
    return os.path.join(draft_root(), "root_meta_info.json")


def _index() -> dict:
    try:
        return json.load(open(_index_path(), encoding="utf-8"))
    except Exception:
        return {"all_draft_store": []}


def draft_dir(name: str) -> str:
    """草稿真实文件夹：草稿文件可能在自定义位置(如 D:\\soft)，从索引读 draft_fold_path。"""
    for x in _index().get("all_draft_store", []):
        if x.get("draft_name") == name:
            fp = (x.get("draft_fold_path") or "").replace("/", os.sep)
            if fp and os.path.isdir(fp):
                return fp
    return os.path.join(draft_root(), name)     # 兜底：索引没有就按默认位置


def drafts_base() -> str:
    """新草稿写到哪个父目录（跟现有草稿放一起）。取索引里最常见的父目录。"""
    from collections import Counter
    c = Counter()
    for x in _index().get("all_draft_store", []):
        fp = x.get("draft_fold_path")
        if fp:
            c[os.path.dirname(fp.replace("/", os.sep))] += 1
    return c.most_common(1)[0][0] if c else draft_root()


def read_draft_json(name: str) -> dict:
    p = os.path.join(draft_dir(name), "draft_content.json")
    raw = open(p, "rb").read()
    if raw.lstrip()[:1] in (b"{", b"["):        # 老版本明文
        return json.loads(raw)
    return json.loads(decrypt(raw))


def read_shell_json() -> dict:
    """读软件自带的效果壳 draft_content（做剪映模版的特效/滤镜/调整/HSL 来源）。"""
    p = os.path.join(BASE_SHELL_DIR, "draft_content.json")
    raw = open(p, "rb").read()
    if raw.lstrip()[:1] in (b"{", b"["):
        return json.loads(raw)
    return json.loads(decrypt(raw))


def list_drafts() -> list:
    """从 root_meta_info.json（明文）读草稿列表。按真实 fold_path 判存在（草稿文件可能在 D:\\soft）。"""
    out = []
    for x in _index().get("all_draft_store", []):
        nm = x.get("draft_name")
        fold = (x.get("draft_fold_path") or "").replace("/", os.sep)
        if nm and fold and os.path.isdir(fold):
            out.append({"name": nm, "fold": fold, "modified": x.get("tm_draft_modified", 0)})
    out.sort(key=lambda d: d["modified"], reverse=True)
    return out


# ---------------- 混剪合成 ----------------
import copy
import random
import shutil
import time
import uuid


def _new_id() -> str:
    return str(uuid.uuid4()).upper()


def _probe(clip):
    """拿视频 (时长微秒, 宽, 高)。用 pyJianYingDraft 的探测。"""
    import pyJianYingDraft as pjd
    m = pjd.VideoMaterial(clip)
    return int(m.duration), int(getattr(m, "width", 0) or 1080), int(getattr(m, "height", 0) or 1920)


def compose_one(template_name, clip_paths, out_name, speed_range=(0.9, 1.0)):
    """以 template_name 为模板，把 clip_paths 塞进主视频轨道生成新草稿。
    做法：克隆模板自己的片段/素材原型（schema 天然匹配本版剪映），只改 路径/时长/速度。"""
    tj = read_draft_json(template_name)
    mats = tj["materials"]
    vtracks = [t for t in tj["tracks"] if t.get("type") == "video"]
    proto_track = next((t for t in vtracks if t.get("segments")), None)   # 参照：有片段的视频轨道
    if not proto_track:
        raise RuntimeError("模板没有可参照的视频片段（换个有视频的模板）")

    def _alpha(t):
        s = t.get("segments") or []
        return (s[0].get("clip", {}).get("alpha", 1) if s else 0) or 0

    def _has_mask(t):
        for s in t.get("segments") or []:
            for rid in s.get("extra_material_refs", []):
                if _mat_of(mats, rid)[0] == "common_mask":
                    return True
        return False

    # 主内容轨道 = 不透明(alpha≥0.5)且【无蒙版】的满屏轨道；上下蒙版是装饰轨，不动
    targets = [t for t in vtracks if t.get("segments") and _alpha(t) >= 0.5 and not _has_mask(t)]
    add_center = False
    if not targets:
        empty = next((t for t in vtracks if not t.get("segments")), None)
        if empty:
            targets = [empty]
        else:
            add_center = True     # 模板只有蒙版装饰轨 → 新建一条中间主轨道放集数（放最底层当主画面）

    proto_seg = copy.deepcopy(proto_track["segments"][0])
    ref_ids = set(proto_seg.get("extra_material_refs", []))       # 附属素材（speed/canvas/蒙版/音轨映射…）
    aux_protos = {}
    for cat, items in mats.items():
        if isinstance(items, list):
            for it in items:
                if it.get("id") in ref_ids:
                    aux_protos[cat] = it
    proto_mat = next((m for m in mats.get("videos", []) if m.get("id") == proto_seg.get("material_id")), None) \
        or (mats.get("videos") or [{}])[0]

    def _build_segs(center):
        """把 clip_paths 顺序拼成一条轨道的片段。center=True：满屏居中、去蒙版、不透明（主画面）。"""
        segs, cursor = [], 0
        for clip in clip_paths:
            dur_us, w, h = _probe(clip)
            lo, hi = speed_range
            spd = max(0.5, min(2.0, round(random.uniform(lo, hi), 3) if lo != hi else (lo or 1.0)))
            play_us = int(dur_us / spd)
            nm = copy.deepcopy(proto_mat)
            abspath = os.path.abspath(clip).replace("\\", "/")   # 绝对路径，否则剪映"媒体丢失"
            nm.update(id=_new_id(), path=abspath, duration=dur_us, width=w, height=h,
                      material_name=os.path.basename(clip), has_audio=True,
                      material_id="", local_material_id="", category_name="", category_id="", crop={})
            mats["videos"].append(nm)
            new_refs = []
            for cat, proto in aux_protos.items():
                if center and cat == "common_mask":
                    continue                          # 主画面不要蒙版
                na = copy.deepcopy(proto)
                na["id"] = _new_id()
                if cat == "speeds":
                    na["speed"] = spd
                    if isinstance(na.get("curve_speed"), dict):
                        na["curve_speed"] = None
                mats.setdefault(cat, []).append(na)
                new_refs.append(na["id"])
            ns = copy.deepcopy(proto_seg)
            ns.update(id=_new_id(), material_id=nm["id"], extra_material_refs=new_refs,
                      source_timerange={"start": 0, "duration": dur_us},
                      target_timerange={"start": cursor, "duration": play_us},
                      render_timerange={})
            ns["volume"] = 1.0                        # 主内容要有声（原型来自静音的蒙版，得改回来）
            if center:                                # 满屏居中、不透明
                cl = ns.setdefault("clip", {})
                cl["transform"] = {"x": 0.0, "y": 0.0}
                cl["scale"] = {"x": 1.0, "y": 1.0}
                cl["alpha"] = 1.0
                cl["rotation"] = 0.0
            segs.append(ns)
            cursor += play_us
        return segs, cursor

    total = 0
    if add_center:
        segs, cur = _build_segs(center=True)
        # 【关键】去掉 flag（剪映用 flag=2 标记副轨道/画中画；没 flag = 主轨道）→ 集数进主轨道
        nt = copy.deepcopy({k: v for k, v in proto_track.items() if k not in ("segments", "flag")})
        nt["id"] = _new_id()
        nt["segments"] = segs
        nt["attribute"] = 0                          # 主轨道不锁，方便你在剪映里换/调
        tj["tracks"].insert(0, nt)                   # 放轨道列表最前=主轨道，蒙版(副轨道)在上面当装饰
        total = cur
    else:
        for track in targets:                        # 填已有主内容轨道
            segs, cur = _build_segs(center=True)      # 集数永远满屏主画面
            track["segments"] = segs
            track.pop("flag", None)                  # 确保是主轨道(去掉副轨道 flag)
            track["attribute"] = 0                   # 主轨道不锁
            total = max(total, cur)

    tj["duration"] = total
    _write_draft(template_name, out_name, tj)
    return out_name


def collect_clips(folder: str) -> list:
    """递归收集文件夹里的 mp4（绝对路径）。"""
    out = []
    for root, _dirs, files in os.walk(folder):
        for f in files:
            if f.lower().endswith(".mp4") and not f.endswith(".part"):
                out.append(os.path.abspath(os.path.join(root, f)))
    return out


def _natkey(p):
    """自然排序键：让 1.mp4 < 2.mp4 < 10.mp4（按集数顺序）。"""
    name = os.path.basename(p)
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", name)]


def _sanitize_name(s):
    return re.sub(r'[\\/:*?"<>|]', "", str(s)).strip()[:50] or "mix"


def _mix_name(picked, prefix, idx):
    """混剪草稿名 = 合集文件夹名_第X-Y集（取自选中素材）。取不到就退回 前缀_序号。"""
    try:
        coll = os.path.basename(os.path.dirname(picked[0]))
        nums = []
        for p in picked:
            m = re.match(r"(\d+)", os.path.splitext(os.path.basename(p))[0])
            if m:
                nums.append(int(m.group(1)))
        if coll and nums:
            nums = sorted(set(nums))
            rng = f"{nums[0]}-{nums[-1]}" if len(nums) > 1 else f"{nums[0]}"
            return _sanitize_name(f"{coll}_第{rng}集")
    except Exception:
        pass
    return f"{prefix}_{idx + 1}"


def compose_batch(template_name, clips_folder, out_prefix, count=1, mode="count",
                  n_clips=5, target_sec=60, speed_range=(0.9, 1.0), on_status=None,
                  clip_paths=None, sequential=False):
    """批量生成混剪草稿。sequential=True：按集数顺序每 n_clips 集一个草稿(1-3、4-6…)，覆盖所有集；
    否则每个草稿随机抽片段。clip_paths 给了就直接用这些视频(下载库多选)，否则扫 clips_folder。返回草稿名列表。"""
    if clip_paths:
        pool = [p for p in clip_paths if os.path.isfile(p) and p.lower().endswith((".mp4", ".mov", ".mkv"))]
        if not pool:
            raise RuntimeError("勾选的文件夹里没有可用视频")
    else:
        pool = collect_clips(clips_folder)
        if not pool:
            raise RuntimeError(f"素材文件夹里没有 mp4：{clips_folder}")
    made = []
    dur_cache = {}
    per = max(1, int(n_clips))
    if sequential:
        pool = sorted(pool, key=_natkey)                       # 按集数顺序
        count = max(1, (len(pool) + per - 1) // per)           # 覆盖所有集，自动算草稿数
    for i in range(count):
        if sequential:
            picked = pool[i * per:(i + 1) * per]               # 顺序切块：1,2,3 / 4,5,6 …
            if not picked:
                break                                          # 集数用完就停
        else:
            random.shuffle(pool)
            if mode == "duration":
                picked, total = [], 0
                for c in pool:
                    if c not in dur_cache:
                        try:
                            dur_cache[c] = _probe(c)[0]
                        except Exception:
                            dur_cache[c] = 0
                    if dur_cache[c] <= 0:
                        continue
                    picked.append(c)
                    total += dur_cache[c]
                    if total >= target_sec * 1e6:
                        break
            else:
                picked = pool[:per]
        if not picked:
            continue
        name = _mix_name(picked, out_prefix, i)        # 合集名_第X-Y集
        if on_status:
            on_status(f"生成 {name}（{len(picked)} 段）")
        try:
            compose_one(template_name, picked, name, speed_range)
            made.append(name)
        except Exception as e:
            if on_status:
                on_status(f"{name} 失败：{str(e)[:80]}")
    return made


# ---------------- 做剪映模版：读取一个手搭好的草稿里的效果层，可勾选保留 ----------------
# 轨道类型 → 友好分类（截图里：广角=特效 / 8K画质=滤镜 / 调整=调整 / BGM=音频）
_LAYER_KIND = {"effect": ("特效", "✨"), "filter": ("滤镜 / 画质", "🎨"),
               "adjust": ("调整 / 调色", "🎛"), "audio": ("背景音乐", "🎵"),
               "sticker": ("贴纸", "🏷"), "text": ("花字 / 字幕", "🅣")}


def _mat_name(mats: dict, mid: str) -> str:
    for cat, items in mats.items():
        if isinstance(items, list):
            for it in items:
                if it.get("id") == mid:
                    return it.get("name") or it.get("material_name") or it.get("effect_name") \
                        or it.get("category_name") or ""
    return ""


def template_layers(name: str) -> dict:
    """读一个草稿，把它的效果层（特效/滤镜/调整/BGM…）挑出来，供界面勾选。
    视频轨道(复合片段)算主内容，混剪时替换，这里只作信息展示、不做开关。"""
    tj = read_draft_json(name)
    mats = tj.get("materials", {})
    layers, video_tracks = [], 0
    for ti, tr in enumerate(tj.get("tracks", [])):
        tp = tr.get("type")
        segs = tr.get("segments") or []
        if tp == "video":
            if segs:
                video_tracks += 1
            continue
        if not segs or tp not in _LAYER_KIND:
            continue
        kind, icon = _LAYER_KIND[tp]
        labels = [_mat_name(mats, s.get("material_id")) for s in segs]
        labels = [x for x in labels if x]
        label = labels[0] if labels else kind
        layers.append({"index": ti, "type": tp, "kind": kind, "icon": icon,
                       "label": label, "count": len(segs)})
    return {"name": name, "video_tracks": video_tracks, "layers": layers}


def make_template(base_name: str, out_name: str, drop_indexes=None) -> str:
    """基于 base_name 生成一个新模版草稿：去掉 drop_indexes 里的效果层轨道，其余（含主视频轨/勾选保留的效果）照留。
    生成的模版会出现在混剪的模板列表里。"""
    drop = set(drop_indexes or [])
    tj = read_draft_json(base_name)
    tracks = tj.get("tracks", [])
    tj["tracks"] = [tr for ti, tr in enumerate(tracks) if ti not in drop]
    _write_draft(base_name, out_name, tj)
    return out_name


# ---------------- 深度读参数 + 素材库随机批量生成 ----------------
def _mat_of(mats: dict, mid: str):
    for cat, items in mats.items():
        if isinstance(items, list):
            for it in items:
                if it.get("id") == mid:
                    return cat, it
    return None, {}


def template_params(name: str) -> dict:
    """把草稿里每一层的具体参数全读出来：视频层(透明度/缩放/位移/音量/内部蒙版/贴纸)、
    特效强度、滤镜值、调整各项、BGM音量循环数。给界面做成逐项设置。"""
    tj = read_draft_json(name)
    mats = tj.get("materials", {})
    cw = (tj.get("canvas_config") or {}).get("width", 1080)
    ch = (tj.get("canvas_config") or {}).get("height", 1920)
    out = {"name": name, "canvas": {"w": cw, "h": ch},
           "video_layers": [], "effects": [], "filters": [], "adjusts": [], "audios": []}

    for ti, tr in enumerate(tj.get("tracks", [])):
        tp = tr.get("type")
        segs = tr.get("segments") or []
        if not segs:
            continue
        s0 = segs[0]
        cat, m = _mat_of(mats, s0.get("material_id"))

        if tp == "video":
            clip = s0.get("clip") or {}
            layer = {"index": ti,
                     "label": m.get("material_name") or m.get("name") or f"视频层{ti}",
                     "alpha": clip.get("alpha", 1.0),
                     "scale": (clip.get("scale") or {}).get("x", 1.0),
                     "tx": (clip.get("transform") or {}).get("x", 0.0),
                     "ty": (clip.get("transform") or {}).get("y", 0.0),
                     "volume": s0.get("volume"), "seg_count": len(segs),
                     "inner_videos": 0, "inner_stickers": 0, "masks": []}
            # 复合片段：子草稿挂在片段 extra_material_refs 的 drafts 引用里，钻进去看蒙版/贴纸
            for rid in s0.get("extra_material_refs", []):
                c2, m2 = _mat_of(mats, rid)
                if c2 != "drafts":
                    continue
                dm = (m2.get("draft") or {}).get("materials", {})
                layer["inner_videos"] = len(dm.get("videos") or [])
                layer["inner_stickers"] = len(dm.get("stickers") or [])
                for mk in (dm.get("common_mask") or [])[:1]:
                    cfgm = mk.get("config") or {}
                    layer["masks"].append({"name": mk.get("name", "蒙版"),
                                           "width": cfgm.get("width"), "feather": cfgm.get("feather"),
                                           "centerX": cfgm.get("centerX"), "centerY": cfgm.get("centerY")})
                layer["mask_count"] = len(dm.get("common_mask") or [])
                break
            # 角色猜测：不透明 + 无贴纸 = 主内容候选；透明的/贴纸的 = 干扰层
            if layer["inner_stickers"]:
                layer["role_guess"] = "sticker"       # 贴纸干扰层 → 保留+透明度随机
            elif layer["alpha"] >= 0.5:
                layer["role_guess"] = "main"          # 主内容 → 替换成素材库拼接
            else:
                layer["role_guess"] = "overlay"       # 叠加干扰层 → 换素材库随机视频
            out["video_layers"].append(layer)

        elif tp == "effect":
            aps = [{"name": p.get("name"), "value": p.get("value")}
                   for p in (m.get("adjust_params") or [])]
            out["effects"].append({"index": ti, "name": m.get("name", "特效"),
                                   "value": m.get("value"), "adjust_params": aps})
        elif tp == "filter":
            out["filters"].append({"index": ti, "name": m.get("name", "滤镜"),
                                   "value": m.get("value")})
        elif tp == "adjust":
            # 调整层的各项数值挂在同一个 placeholder 片段的 extra_material_refs 里（brightness/sharpen/…）
            params = []
            for rid in s0.get("extra_material_refs", []):
                c2, m2 = _mat_of(mats, rid)
                if c2 == "effects" and m2.get("type") not in (None, "filter"):
                    params.append({"type": m2.get("type"), "value": m2.get("value")})
            out["adjusts"].append({"index": ti, "label": m.get("name", "调整"), "params": params})
        elif tp == "audio":
            out["audios"].append({"index": ti, "name": m.get("name", "音频"),
                                  "volume": s0.get("volume"), "loops": len(segs)})
    return out


# 素材库扫描：视频 + 音乐 按扩展名分
_V_EXT = (".mp4", ".mov", ".mkv", ".avi", ".webm")
_A_EXT = (".mp3", ".wav", ".m4a", ".flac", ".aac", ".ogg")


def scan_library(folder: str) -> dict:
    vids, auds = [], []
    for root, _d, files in os.walk(folder):
        for f in files:
            p = os.path.abspath(os.path.join(root, f))
            lf = f.lower()
            if lf.endswith(_V_EXT) and not lf.endswith(".part"):
                vids.append(p)
            elif lf.endswith(_A_EXT):
                auds.append(p)
    return {"videos": vids, "audios": auds}


# 线性蒙版原型（取自真实草稿「7月10日」，同版本剪映资源）。生成时 deepcopy 后改 id/config。
_LINE_MASK_PROTO = {
    "id": "", "type": "mask", "category": "video", "category_name": "基础", "category_id": "jichu",
    "resource_id": "7356933362960831003",
    "constant_material_id": "A09A5286-93D3-4a97-B084-87843E754BBF",
    "name": "线性", "resource_type": "line",
    "path": "C:/Users/Administrator/AppData/Local/JianyingPro/User Data/Cache/effect/82432976/4c6a0ef5de6a844342d40330e00c59eb",
    "config": {"width": 0.28, "centerX": 0.0, "centerY": 0.0, "feather": 1.0, "invert": False},
    "text_config": {"align_type": 15},
}


def _rr(pair, dflt=None):
    """[lo,hi] 里随机取一个；None/空 = 用默认。"""
    if not pair and pair != 0:
        return dflt
    if isinstance(pair, (int, float)):
        return float(pair)
    lo, hi = float(pair[0]), float(pair[1])
    return round(random.uniform(lo, hi), 4) if lo != hi else lo


def _probe_audio(path):
    """音频时长（微秒）。探测不到就当 3 分钟。"""
    try:
        import pyJianYingDraft as pjd
        for cls in ("AudioMaterial", "Audio_material"):
            c = getattr(pjd, cls, None)
            if c:
                return int(c(path).duration)
    except Exception:
        pass
    return 180 * 10**6


# 贴纸可选位置 → 画面坐标（x正=右, y正=上；竖屏1080x1920，角落留边不出框）
_STK_POS = {"tl": (-0.33, 0.72), "tr": (0.33, 0.72),
            "bl": (-0.33, -0.72), "br": (0.33, -0.72), "center": (0.0, 0.0)}


def _apply_semantic(cfg, tracks, mats):
    """界面语义化大项(特效强度/滤镜/调节/贴纸)→ 对应类型轨道，按区间随机写数值。
    模板本身已含这些层，这里只小幅改数值，每次生成都不同 → 去重。所有改动都控制在小区间。"""
    fx = cfg.get("fx") or {}
    flt = cfg.get("filter") or {}
    adj = cfg.get("adjust") or {}
    stk = cfg.get("stickers") or {}
    for tr in tracks:
        tp = tr.get("type")
        segs = tr.get("segments") or []
        if not segs:
            continue

        if tp == "effect" and fx.get("enable"):
            # 特效（广角等）：调节强度 effects_adjust_intensity 按区间随机
            for s in segs:
                _c, m = _mat_of(mats, s.get("material_id"))
                for p in m.get("adjust_params") or []:
                    p["value"] = _rr(fx.get("strength"), p.get("value"))

        elif tp == "filter" and flt.get("enable"):
            # 滤镜（8K画质等）：强度值按区间随机
            for s in segs:
                _c, m = _mat_of(mats, s.get("material_id"))
                m["value"] = _rr(flt.get("value"), m.get("value"))

        elif tp == "adjust" and adj.get("enable"):
            # 调节：亮度/锐化/清晰/对比/饱和 各按小区间随机；HSL 色相每通道小幅抖动
            s0 = segs[0]
            have, proto_eff = {}, None
            hj = float(adj.get("hue_jitter", 0) or 0)
            for rid in list(s0.get("extra_material_refs", [])):
                c2, m2 = _mat_of(mats, rid)
                if c2 == "effects":
                    have[m2.get("type")] = m2
                    proto_eff = proto_eff or m2
                elif c2 == "hsl" and hj:
                    base = m2.get("hue", 0) or 0
                    m2["hue"] = int(max(-100, min(100, base + random.uniform(-hj, hj))))
            for typ in ("brightness", "sharpen", "clear", "particle", "contrast", "saturation"):
                rng = adj.get(typ)
                if rng and typ in have:
                    have[typ]["value"] = _rr(rng, have[typ].get("value"))
            # 模板里没有的调整项(对比度/饱和度)：克隆一个已有子材料改 type 补上（同 effect_id，安全）
            if proto_eff is not None:
                for typ in ("contrast", "saturation"):
                    rng = adj.get(typ)
                    if rng and typ not in have:
                        nm = copy.deepcopy(proto_eff)
                        nm["id"] = _new_id()
                        nm["type"] = typ
                        nm["value"] = _rr(rng, 0.0)
                        mats.setdefault("effects", []).append(nm)
                        s0.setdefault("extra_material_refs", []).append(nm["id"])
        # 贴纸不在这里处理：模板贴纸复合片段已丢弃，改成把用户自己的贴纸图片贴上去（见 compose_v2 贴纸块）


def compose_v2(template_name, out_name, cfg):
    """按 cfg 的逐层设置+随机区间，用素材库批量生成去重草稿（单个）。
    cfg 结构见 app.py JyCompose2Body 注释。核心思路：
    - 主内容层：素材库随机抽 N 条拼接（变速/缩放/位移/翻转/音量 全随机区间）
    - 叠加层：素材库随机抽 1 条盖上去（透明度/缩放/位移随机，可挂蒙版随机）
    - 贴纸层：保留模板的，外层透明度随机
    - 特效/滤镜/调整：数值按区间随机重写
    - BGM：素材库音乐随机选一首循环铺满，音量随机"""
    # 主素材库已取消：上下蒙版/BGM 各用自己的文件夹（下面按需扫描）。lib 仅作老配置兜底
    lib = scan_library(cfg.get("library") or "")
    tj = read_shell_json()          # 效果源=软件自带壳（不再依赖剪映里的某个草稿）
    mats = tj["materials"]
    tracks = tj.get("tracks", [])
    layer_cfg = {int(k): v for k, v in (cfg.get("layers") or {}).items()}

    # ---- 原型：克隆模板自己的片段/素材 schema（和 compose_one 同一套路） ----
    vtracks = [t for t in tracks if t.get("type") == "video" and t.get("segments")]
    proto_track = vtracks[0]
    proto_seg = copy.deepcopy(proto_track["segments"][0])
    ref_ids = set(proto_seg.get("extra_material_refs", []))
    aux_protos = {}
    for cat, items in mats.items():
        if isinstance(items, list):
            for it in items:
                if it.get("id") in ref_ids:
                    aux_protos[cat] = it
    proto_mat = next((mm for mm in mats.get("videos", []) if mm.get("id") == proto_seg.get("material_id")),
                     None) or (mats.get("videos") or [{}])[0]
    # 蒙版原型：优先从模板顶层/复合片段里借；都没有就用内置线性蒙版原型（取自真实草稿）
    mask_proto = None
    if mats.get("common_mask"):
        mask_proto = copy.deepcopy(mats["common_mask"][0])
    else:
        for it in mats.get("drafts", []):
            cms = (it.get("draft") or {}).get("materials", {}).get("common_mask") or []
            if cms:
                mask_proto = copy.deepcopy(cms[0])
                break
    if mask_proto is None:
        mask_proto = copy.deepcopy(_LINE_MASK_PROTO)

    def _mk_video_seg(clip_path, start_us, speed_pair, alpha=None, scale=None,
                      tx=None, ty=None, flip=False, volume=None, mask_cfg=None, rotation=None):
        """造一个视频片段（素材+附属+蒙版），返回 (segment, 播放时长)。"""
        dur_us, w, h = _probe(clip_path)
        spd = max(0.5, min(2.0, _rr(speed_pair, 1.0) or 1.0))
        play_us = int(dur_us / spd)
        nm = copy.deepcopy(proto_mat)
        nm.update(id=_new_id(), path=os.path.abspath(clip_path).replace("\\", "/"),
                  duration=dur_us, width=w, height=h,
                  material_name=os.path.basename(clip_path), has_audio=True,
                  material_id="", local_material_id="", category_name="", category_id="", crop={})
        mats["videos"].append(nm)
        new_refs = []
        for cat, proto in aux_protos.items():
            na = copy.deepcopy(proto)
            na["id"] = _new_id()
            if cat == "speeds":
                na["speed"] = spd
                if isinstance(na.get("curve_speed"), dict):
                    na["curve_speed"] = None
            if cat == "drafts":
                continue          # 复合引用不能带到普通片段上
            mats.setdefault(cat, []).append(na)
            new_refs.append(na["id"])
        if mask_cfg and mask_proto:
            mk = copy.deepcopy(mask_proto)
            mk["id"] = _new_id()
            c = mk.setdefault("config", {})
            for key in ("width", "feather", "centerX", "centerY"):
                v = _rr(mask_cfg.get(key), c.get(key))
                if v is not None:
                    c[key] = v
            if "invert" in mask_cfg:
                c["invert"] = bool(mask_cfg["invert"])
            mats.setdefault("common_mask", []).append(mk)
            new_refs.append(mk["id"])
        ns = copy.deepcopy(proto_seg)
        clip = ns.setdefault("clip", {})
        if alpha is not None:
            clip["alpha"] = alpha
        sc = _rr(scale) if scale is not None else None
        if sc is not None:
            clip["scale"] = {"x": sc, "y": sc}
        txv = _rr(tx) if tx is not None else None
        tyv = _rr(ty) if ty is not None else None
        if txv is not None or tyv is not None:
            t0 = clip.get("transform") or {}
            clip["transform"] = {"x": txv if txv is not None else t0.get("x", 0),
                                 "y": tyv if tyv is not None else t0.get("y", 0)}
        if rotation:
            clip["rotation"] = float(_rr(rotation, 0) or 0)
        if flip:
            clip["flip"] = {"horizontal": True, "vertical": False}
        ns.update(id=_new_id(), material_id=nm["id"], extra_material_refs=new_refs,
                  source_timerange={"start": 0, "duration": dur_us},
                  target_timerange={"start": start_us, "duration": play_us},
                  render_timerange={})
        if volume is not None:
            ns["volume"] = volume
        return ns, play_us

    # ---- 抽主内容片段 ----
    pool = lib["videos"][:]
    random.shuffle(pool)
    if cfg.get("mode") == "duration":
        picked, tot = [], 0
        for c in pool:
            picked.append(c)
            tot += _probe(c)[0]
            if tot >= int(cfg.get("target_sec", 60)) * 10**6:
                break
    else:
        picked = pool[:max(1, int(cfg.get("n_clips", 5)))]

    total_dur = 0
    new_tracks = []
    used_overlay = set()
    for ti, tr in enumerate(tracks):
        tp = tr.get("type")
        lc = layer_cfg.get(ti) or {}
        act = lc.get("action")          # main / overlay / keep / drop
        if tp == "video" and not tr.get("segments"):
            continue                    # 模板里的空视频占位轨，直接丢
        if tp == "video" and tr.get("segments"):
            # 做剪映模版：模板自带的主内容/贴纸复合片段一律丢掉（画面只由下面新加的上下蒙版视频构成，
            # 否则旧内容满屏不透明会盖住/干扰画面）。只有显式 keep 才保留。
            if act == "drop" or (act is None and not lc):
                continue
            if act == "keep":
                if lc.get("alpha"):     # 保留但外层透明度随机（贴纸层）
                    a = _rr(lc["alpha"])
                    for s in tr["segments"]:
                        s.setdefault("clip", {})["alpha"] = a
                new_tracks.append(tr)
                continue
            if act == "main":
                new_segs, cursor = [], 0
                flip_all = random.random() < float(lc.get("flip_prob", 0) or 0)
                for cp in picked:
                    ns, pu = _mk_video_seg(cp, cursor, cfg.get("speed") or [1, 1],
                                           alpha=_rr(lc.get("alpha")) if lc.get("alpha") else None,
                                           scale=lc.get("scale"), tx=lc.get("tx"), ty=lc.get("ty"),
                                           flip=flip_all, volume=_rr(lc.get("volume"), None))
                    new_segs.append(ns)
                    cursor += pu
                tr = copy.deepcopy({k: v for k, v in tr.items() if k != "segments"})
                tr["segments"] = new_segs
                tr["id"] = _new_id()
                new_tracks.append(tr)
                total_dur = max(total_dur, cursor)
                continue
            if act == "overlay":
                remain = [v for v in lib["videos"] if v not in used_overlay] or lib["videos"]
                ov = random.choice(remain)
                used_overlay.add(ov)
                ns, _pu = _mk_video_seg(ov, 0, cfg.get("speed") or [1, 1],
                                        alpha=_rr(lc.get("alpha"), 0.06),
                                        scale=lc.get("scale"), tx=lc.get("tx"), ty=lc.get("ty"),
                                        volume=0.0, mask_cfg=lc.get("mask") if lc.get("mask_on") else None)
                tr = copy.deepcopy({k: v for k, v in tr.items() if k != "segments"})
                tr["segments"] = [ns]
                tr["id"] = _new_id()
                new_tracks.append(tr)
                continue
            new_tracks.append(tr)
        elif tp in ("effect", "filter", "adjust"):
            if act == "drop":
                continue
            for s in tr.get("segments") or []:
                _cat, m = _mat_of(mats, s.get("material_id"))
                if tp == "effect" and lc.get("value"):
                    for p in m.get("adjust_params") or []:
                        p["value"] = _rr(lc["value"], p.get("value"))
                if tp == "filter" and lc.get("value"):
                    m["value"] = _rr(lc["value"], m.get("value"))
                if tp == "adjust":
                    prange = lc.get("params") or {}
                    for rid in s.get("extra_material_refs", []):
                        c2, m2 = _mat_of(mats, rid)
                        if c2 == "effects" and m2.get("type") in prange:
                            m2["value"] = _rr(prange[m2["type"]], m2.get("value"))
            new_tracks.append(tr)
        elif tp == "audio":
            if act == "drop":
                continue
            new_tracks.append(tr)     # BGM 轨道最后统一处理（要知道总时长）
        else:
            new_tracks.append(tr)

    # ---- 叠加层追加：次轨道加几层视频 ----
    extra = cfg.get("extra_overlays") or {}
    n_extra = int(extra.get("count", 0) or 0)
    for _ in range(n_extra):
        remain = [v for v in lib["videos"] if v not in used_overlay] or lib["videos"]
        ov = random.choice(remain)
        used_overlay.add(ov)
        ns, _pu = _mk_video_seg(ov, 0, cfg.get("speed") or [1, 1],
                                alpha=_rr(extra.get("alpha"), 0.06),
                                scale=extra.get("scale"), tx=extra.get("tx"), ty=extra.get("ty"),
                                volume=0.0, mask_cfg=extra.get("mask") if extra.get("mask_on") else None)
        nt = copy.deepcopy({k: v for k, v in proto_track.items() if k != "segments"})
        nt["segments"] = [ns]
        nt["id"] = _new_id()
        new_tracks.append(nt)

    # ---- 上下蒙版视频（主视频上/下各压一条，线性蒙版渐隐；不断循环拼素材填满到最短时长） ----
    bands = cfg.get("bands") or {}
    # 蒙版素材池：共用的 bands.folder（老配置兼容），每条 band 可再用自己的 top/bottom 文件夹覆盖
    band_pool = lib["videos"]
    if bands.get("folder"):
        try:
            bp = scan_library(bands["folder"])["videos"]
            if bp:
                band_pool = bp
        except Exception:
            pass
    # 全局时长（默认15分钟）：上下蒙版素材循环拼到这个长度，草稿总时长也随之拉到这里
    g_min = float((cfg.get("global") or {}).get("duration_min") or bands.get("min_minutes") or 15)
    band_min_us = int(g_min * 60 * 10**6)
    for key in ("top", "bottom"):
        b = bands.get(key) or {}
        if not b.get("enable"):
            continue
        # 该 band 的素材池：优先它自己的文件夹，否则共用池/主素材库
        pool_b = band_pool
        if b.get("folder"):
            try:
                bp = scan_library(b["folder"])["videos"]
                if bp:
                    pool_b = bp
            except Exception:
                pass
        if not pool_b:
            raise RuntimeError(f"{'上' if key == 'top' else '下'}方蒙版没找到视频素材，请给它选一个有视频的素材文件夹")
        mk = b.get("mask") or {}
        mask_cfg = ({"width": mk.get("width", 0.28), "centerX": mk.get("centerX", 0.0),
                     "centerY": mk.get("centerY", 0.0), "feather": mk.get("feather", 1.0),
                     "invert": bool(mk.get("invert", False))} if b.get("mask_on", True) else None)
        # 整条 band 只选【一个】视频 + 【一个】不透明度（循环外定），把这同一个视频循环铺满到时长；
        # 不透明度在 [lo,hi] 里随机取一次，整条统一（不逐段闪）。不同草稿会各自重新随机。
        bv = random.choice(pool_b)
        band_alpha = float(_rr(b.get("alpha"), 1.0) or 1.0)
        segs, cursor, guard = [], 0, 0
        while cursor < band_min_us and guard < 2000:
            guard += 1
            ns, pu = _mk_video_seg(
                bv, cursor, [1, 1],
                alpha=band_alpha,
                scale=b.get("scale", 1.0), tx=b.get("x", 0.0), ty=b.get("y"),
                rotation=b.get("rotation"), volume=0.0, mask_cfg=mask_cfg)
            segs.append(ns)
            cursor += pu
        nt = copy.deepcopy({k: v for k, v in proto_track.items() if k != "segments"})
        nt["segments"] = segs
        nt["id"] = _new_id()
        new_tracks.append(nt)
        total_dur = max(total_dur, cursor)   # 草稿总时长跟着拉到 ≥ 最短时长

    if not total_dur:
        total_dur = tj.get("duration") or 0

    # ---- 叠加层时长对齐主内容 ----
    for tr in new_tracks:
        if tr.get("type") == "video":
            for s in tr.get("segments") or []:
                tt = s.get("target_timerange") or {}
                if tt.get("start", 0) == 0 and len(tr["segments"]) == 1 and total_dur:
                    src = s.get("source_timerange") or {}
                    cap = min(src.get("duration", total_dur), total_dur)
                    s["target_timerange"] = {"start": 0, "duration": cap}
                    s["source_timerange"] = {"start": 0, "duration": cap}

    # ---- 贴纸：把用户贴纸文件夹里的图片随机贴上去（每张一条轨道，随机大小/位置/不透明度，铺满时长，贴在最上层） ----
    stk = cfg.get("stickers") or {}
    if stk.get("enable") and stk.get("folder") and total_dur:
        exts = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp")
        imgs = []
        try:
            for root, _dirs, files in os.walk(stk["folder"]):
                for f in files:
                    if f.lower().endswith(exts):
                        imgs.append(os.path.join(root, f))
        except Exception:
            imgs = []
        if imgs:
            try:
                import pyJianYingDraft as pjd
            except Exception:
                pjd = None
            n_stk = max(1, int(stk.get("count", 2) or 2))
            for _ in range(n_stk):
                img = random.choice(imgs)
                w = h = 512
                if pjd:
                    try:
                        pm0 = pjd.VideoMaterial(img)
                        w = int(getattr(pm0, "width", 512) or 512)
                        h = int(getattr(pm0, "height", 512) or 512)
                    except Exception:
                        pass
                # 照片素材：克隆模板视频素材原型再改成 photo（同版本 schema 最稳）
                pm = copy.deepcopy(proto_mat)
                pm.update(id=_new_id(), type="photo", has_audio=False, duration=10800000000,
                          path=os.path.abspath(img).replace("\\", "/"),
                          material_name=os.path.basename(img), width=w, height=h,
                          is_set_beauty_mode=False)
                pm.pop("audio_fade", None)
                mats.setdefault("videos", []).append(pm)
                # 片段：克隆band片段原型；clip 随机(大小/位置/透明度)；铺满整条时长
                seg = copy.deepcopy(proto_seg)
                new_refs = []
                for cat, proto in aux_protos.items():
                    if cat == "drafts":
                        continue
                    na = copy.deepcopy(proto)
                    na["id"] = _new_id()
                    mats.setdefault(cat, []).append(na)
                    new_refs.append(na["id"])
                sc = float(_rr(stk.get("scale"), 0.3) or 0.3)
                clip = seg.setdefault("clip", {})
                clip["scale"] = {"x": sc, "y": sc}
                clip["alpha"] = float(_rr(stk.get("alpha"), 1.0) or 1.0)
                # 位置：在勾选的位置(四角+中间)里随机选一个，带一点小抖动
                poskeys = stk.get("positions") or ["tl", "tr", "bl", "br", "center"]
                bx, by = _STK_POS.get(random.choice(poskeys), (0.0, 0.0))
                clip["transform"] = {"x": round(bx + random.uniform(-0.04, 0.04), 3),
                                     "y": round(by + random.uniform(-0.04, 0.04), 3)}
                clip["rotation"] = 0.0
                clip["flip"] = {}
                seg.update(id=_new_id(), material_id=pm["id"], extra_material_refs=new_refs,
                           source_timerange={"start": 0, "duration": total_dur},
                           target_timerange={"start": 0, "duration": total_dur},
                           render_timerange={}, volume=0.0)
                nt = copy.deepcopy({k: v for k, v in proto_track.items() if k != "segments"})
                nt["id"] = _new_id()
                nt["segments"] = [seg]
                new_tracks.append(nt)

    # ---- BGM：优先用专门的背景音乐文件夹，否则用素材库里的音乐；随机一首循环铺满，音量随机 ----
    bgm = cfg.get("bgm") or {}
    bgm_pool = lib["audios"]
    if bgm.get("folder"):
        try:
            bgm_pool = scan_library(bgm["folder"])["audios"] or lib["audios"]
        except Exception:
            pass
    if bgm.get("enable") and bgm_pool:
        music = random.choice(bgm_pool)
        adur = _probe_audio(music)
        vol = _rr(bgm.get("volume"), 0.17)
        for tr in new_tracks:
            if tr.get("type") != "audio":
                continue
            segs = tr.get("segments") or []
            if not segs:
                continue
            proto_a_seg = copy.deepcopy(segs[0])
            _c, proto_a_mat = _mat_of(mats, proto_a_seg.get("material_id"))
            na = copy.deepcopy(proto_a_mat)
            na.update(id=_new_id(), path=os.path.abspath(music).replace("\\", "/"),
                      name=os.path.splitext(os.path.basename(music))[0], duration=adur)
            mats.setdefault("audios", []).append(na)
            new_asegs, cur = [], 0
            while cur < total_dur:
                seg = copy.deepcopy(proto_a_seg)
                take = min(adur, total_dur - cur)
                new_refs = []
                for rid in seg.get("extra_material_refs", []):
                    c2, m2 = _mat_of(mats, rid)
                    if c2:
                        n2 = copy.deepcopy(m2)
                        n2["id"] = _new_id()
                        mats.setdefault(c2, []).append(n2)
                        new_refs.append(n2["id"])
                seg.update(id=_new_id(), material_id=na["id"], extra_material_refs=new_refs,
                           source_timerange={"start": 0, "duration": take},
                           target_timerange={"start": cur, "duration": take})
                seg["volume"] = vol
                new_asegs.append(seg)
                cur += take
            tr["segments"] = new_asegs
            tr["id"] = _new_id()
            break

    # ---- 特效/滤镜/调整层时长铺满 ----
    for tr in new_tracks:
        if tr.get("type") in ("effect", "filter", "adjust") and total_dur:
            for s in tr.get("segments") or []:
                s["target_timerange"] = {"start": 0, "duration": total_dur}

    # ---- 语义化大项：特效强度/滤镜/调节(锐化清晰HSL)/贴纸 按区间随机 ----
    _apply_semantic(cfg, new_tracks, mats)

    # ---- 锁定所有轨道（attribute bit2=4 锁定）：去重层不被误动，主内容留给剪映混剪往主轨道加 ----
    if cfg.get("lock_tracks", True):
        for tr in new_tracks:
            tr["attribute"] = int(tr.get("attribute") or 0) | 4

    tj["tracks"] = new_tracks
    tj["duration"] = total_dur
    _write_draft(template_name, out_name, tj, src_dir=BASE_SHELL_DIR)   # 从自带效果壳整包复制
    return out_name


def compose_batch_v2(template_name, cfg, out_prefix, count=1, on_status=None):
    """批量：每个草稿重新随机（素材/音乐/所有区间参数都各不相同）。"""
    made = []
    stamp = time.strftime("%m%d%H%M")
    for i in range(int(count)):
        if on_status:
            on_status(f"生成第 {i + 1}/{count} 个…")
        name = f"{out_prefix}_{stamp}_{i + 1}"
        compose_v2(template_name, name, cfg)
        made.append(name)
    if on_status:
        on_status(f"完成 {len(made)} 个")
    return made


def _write_draft(template_name, out_name, content, src_dir=None):
    src = src_dir or draft_dir(template_name)             # 效果壳(自带) 或 模板真实位置
    base = drafts_base()
    dst = os.path.join(base, out_name)                    # 新草稿写到现有草稿同目录
    tmp = os.path.join(base, "." + out_name + ".building")  # 先在临时目录搭好，最后原子替换
    fold = dst.replace("\\", "/")
    now = int(time.time() * 1e6)
    dur = content.get("duration", 0)
    # ---- 1) 整份复制到临时目录，改写内容 ----
    if os.path.exists(tmp):
        shutil.rmtree(tmp, ignore_errors=True)
    shutil.copytree(src, tmp)
    content["path"] = fold
    with open(os.path.join(tmp, "draft_content.json"), "wb") as f:
        f.write(encrypt(json.dumps(content, ensure_ascii=False)))
    bp = os.path.join(tmp, "draft_content.json.bak")
    if os.path.exists(bp):
        os.remove(bp)
    new_id = _new_id()
    mp = os.path.join(tmp, "draft_meta_info.json")
    mraw = open(mp, "rb").read()
    meta = json.loads(mraw) if mraw.lstrip()[:1] in (b"{", b"[") else json.loads(decrypt(mraw))
    meta.update(draft_id=new_id, draft_name=out_name, draft_fold_path=fold,
                tm_draft_create=now, tm_draft_modified=now, tm_duration=dur)
    with open(mp, "wb") as f:
        f.write(encrypt(json.dumps(meta, ensure_ascii=False)))
    # ---- 2) 原子替换：先把旧草稿改名挪走（被剪映打开会失败，但绝不会删坏它） ----
    if os.path.exists(dst):
        grave = os.path.join(base, "." + out_name + ".old")
        if os.path.exists(grave):
            shutil.rmtree(grave, ignore_errors=True)
        try:
            os.rename(dst, grave)                         # 原子操作：占用则直接抛错，不留半残
        except Exception:
            shutil.rmtree(tmp, ignore_errors=True)
            raise RuntimeError(f"草稿「{out_name}」正在剪映里打开，请先在剪映关掉它再生成")
        shutil.rmtree(grave, ignore_errors=True)
    os.rename(tmp, dst)                                   # 临时目录就绪 → 一步变成正式草稿
    _register(template_name, out_name, new_id, fold, now, dur)


def delete_draft(name: str) -> bool:
    """删掉一个草稿：删草稿文件夹 + 从 root_meta 注销。只给自动清理调用。"""
    removed = False
    try:
        fold = draft_dir(name)
        if fold and os.path.isdir(fold):
            shutil.rmtree(fold, ignore_errors=True)
            removed = True
    except Exception:
        pass
    try:
        rp = os.path.join(draft_root(), "root_meta_info.json")
        if os.path.exists(rp):
            try:
                shutil.copy(rp, rp + ".bak")
            except Exception:
                pass
            rm = json.load(open(rp, encoding="utf-8"))
            store = rm.get("all_draft_store", [])
            n0 = len(store)
            store[:] = [x for x in store if x.get("draft_name") != name]
            if len(store) != n0:
                removed = True
            with open(rp, "w", encoding="utf-8") as f:
                json.dump(rm, f, ensure_ascii=False)
    except Exception:
        pass
    return removed


def _register(template_name, out_name, new_id, fold, now, dur):
    """注册进 root_meta_info.json（剪映靠它显示草稿列表）。先备份，防改坏。"""
    rp = os.path.join(draft_root(), "root_meta_info.json")
    try:
        shutil.copy(rp, rp + ".bak")
    except Exception:
        pass
    rm = json.load(open(rp, encoding="utf-8"))
    store = rm.setdefault("all_draft_store", [])
    store[:] = [x for x in store if x.get("draft_name") != out_name]     # 去掉同名旧的
    tpl = next((x for x in store if x.get("draft_name") == template_name), None) or (store[0] if store else {})
    e = copy.deepcopy(tpl)
    e.update(draft_id=new_id, draft_name=out_name, draft_fold_path=fold,
             draft_cover=fold + "/draft_cover.jpg",
             tm_draft_create=now, tm_draft_modified=now, tm_draft_removed=0, tm_duration=dur)
    store.insert(0, e)
    with open(rp, "w", encoding="utf-8") as f:
        json.dump(rm, f, ensure_ascii=False)
