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
