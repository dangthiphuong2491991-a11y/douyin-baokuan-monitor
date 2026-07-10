# -*- coding: utf-8 -*-
"""剪映草稿引擎：调剪映自己的 videoeditor.dll（EncryptUtils）解密/加密草稿，
读草稿列表、把视频混剪进模板主轨道生成新草稿。原理同 jy-draftc / 剪大神。
只在 Windows + 装了剪映时可用；DLL 用【当前安装版剪映】自己的，天然跟版本走。"""
import ctypes as C
import glob
import json
import os

_DEC_SYM = ("?decrypt@EncryptUtils@lvve@@QEAA?AV?$basic_string@DU?$char_traits@D@std@@"
            "V?$allocator@D@2@@std@@AEBV34@0AEA_N@Z")
_ENC_SYM = ("?encrypt@EncryptUtils@lvve@@QEAA?AV?$basic_string@DU?$char_traits@D@std@@"
            "V?$allocator@D@2@@std@@AEBV34@@Z")
_ENABLE_SYM = "?enable@EncryptUtils@lvve@@QEAAX_N@Z"

_STATE = {"dll": None, "dec": None, "enc": None, "dir": None}


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


def read_draft_json(name: str) -> dict:
    p = os.path.join(draft_root(), name, "draft_content.json")
    raw = open(p, "rb").read()
    if raw.lstrip()[:1] in (b"{", b"["):        # 老版本明文
        return json.loads(raw)
    return json.loads(decrypt(raw))


def list_drafts() -> list:
    """从 root_meta_info.json（明文）读草稿列表。"""
    rp = os.path.join(draft_root(), "root_meta_info.json")
    out = []
    try:
        rm = json.load(open(rp, encoding="utf-8"))
        for x in rm.get("all_draft_store", []):
            nm = x.get("draft_name")
            fold = x.get("draft_fold_path") or ""
            if nm and os.path.isdir(os.path.join(draft_root(), nm)):
                out.append({"name": nm, "fold": fold,
                            "modified": x.get("tm_draft_modified", 0)})
    except Exception:
        pass
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
    main = next((t for t in tj["tracks"] if t.get("type") == "video"), None)
    if not main or not main.get("segments"):
        raise RuntimeError("模板主轨道没有视频片段可参照（换个有视频的模板）")

    proto_seg = copy.deepcopy(main["segments"][0])
    proto_mat = next((m for m in mats.get("videos", []) if m.get("id") == proto_seg.get("material_id")), None)
    if not proto_mat:
        raise RuntimeError("模板片段找不到对应视频素材")
    ref_ids = set(proto_seg.get("extra_material_refs", []))       # 6 个附属素材（speed/canvas/音轨映射…）
    aux_protos = {}   # category -> 原型素材
    for cat, items in mats.items():
        if isinstance(items, list):
            for it in items:
                if it.get("id") in ref_ids:
                    aux_protos[cat] = it

    new_segs = []
    cursor = 0
    for clip in clip_paths:
        dur_us, w, h = _probe(clip)
        lo, hi = speed_range
        spd = round(random.uniform(lo, hi), 3) if lo != hi else lo
        spd = max(0.5, min(2.0, spd or 1.0))
        play_us = int(dur_us / spd)
        # 新视频素材（克隆原型，改路径/时长/尺寸）
        nm = copy.deepcopy(proto_mat)
        abspath = os.path.abspath(clip).replace("\\", "/")   # 必须绝对路径，否则剪映"媒体丢失"
        nm.update(id=_new_id(), path=abspath, duration=dur_us, width=w, height=h,
                  material_name=os.path.basename(clip), has_audio=True,
                  material_id="", local_material_id="", category_name="", category_id="", crop={})
        mats["videos"].append(nm)
        # 每个片段独立克隆一套附属素材，speed 类设成本片段速度
        new_refs = []
        for cat, proto in aux_protos.items():
            na = copy.deepcopy(proto)
            na["id"] = _new_id()
            if cat == "speeds":
                na["speed"] = spd
                if isinstance(na.get("curve_speed"), dict):
                    na["curve_speed"] = None
            mats.setdefault(cat, []).append(na)
            new_refs.append(na["id"])
        # 新片段
        ns = copy.deepcopy(proto_seg)
        ns.update(id=_new_id(), material_id=nm["id"], extra_material_refs=new_refs,
                  source_timerange={"start": 0, "duration": dur_us},
                  target_timerange={"start": cursor, "duration": play_us},
                  render_timerange={})
        new_segs.append(ns)
        cursor += play_us

    main["segments"] = new_segs
    tj["duration"] = cursor
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


def compose_batch(template_name, clips_folder, out_prefix, count=1, mode="count",
                  n_clips=5, target_sec=60, speed_range=(0.9, 1.0), on_status=None):
    """批量生成 count 个混剪草稿。mode='count'固定N条 / 'duration'按目标时长填满。
    每个草稿随机抽片段（草稿内不重复）。返回生成的草稿名列表。"""
    pool = collect_clips(clips_folder)
    if not pool:
        raise RuntimeError(f"素材文件夹里没有 mp4：{clips_folder}")
    made = []
    dur_cache = {}
    for i in range(count):
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
            picked = pool[:max(1, int(n_clips))]
        if not picked:
            continue
        name = f"{out_prefix}_{i + 1}"
        if on_status:
            on_status(f"生成 {name}（{len(picked)} 段）")
        try:
            compose_one(template_name, picked, name, speed_range)
            made.append(name)
        except Exception as e:
            if on_status:
                on_status(f"{name} 失败：{str(e)[:80]}")
    return made


def _write_draft(template_name, out_name, content):
    root = draft_root()
    src = os.path.join(root, template_name)
    dst = os.path.join(root, out_name)
    if os.path.exists(dst):
        shutil.rmtree(dst, ignore_errors=True)
    shutil.copytree(src, dst)                              # 整份复制模板(含封面/资源)
    fold = dst.replace("\\", "/")
    now = int(time.time() * 1e6)
    dur = content.get("duration", 0)
    # 覆盖 draft_content.json（加密）
    content["path"] = fold
    with open(os.path.join(dst, "draft_content.json"), "wb") as f:
        f.write(encrypt(json.dumps(content, ensure_ascii=False)))
    for bak in ("draft_content.json.bak",):
        bp = os.path.join(dst, bak)
        if os.path.exists(bp):
            os.remove(bp)
    # 更新 draft_meta_info.json（加密）
    new_id = _new_id()
    mp = os.path.join(dst, "draft_meta_info.json")
    mraw = open(mp, "rb").read()
    meta = json.loads(mraw) if mraw.lstrip()[:1] in (b"{", b"[") else json.loads(decrypt(mraw))
    meta.update(draft_id=new_id, draft_name=out_name, draft_fold_path=fold,
                tm_draft_create=now, tm_draft_modified=now, tm_duration=dur)
    with open(mp, "wb") as f:
        f.write(encrypt(json.dumps(meta, ensure_ascii=False)))
    _register(template_name, out_name, new_id, fold, now, dur)


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
