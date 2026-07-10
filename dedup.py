# -*- coding: utf-8 -*-
"""视频去重处理：ffmpeg 滤镜链，每项独立开关 + 参数。
打乱 文件哈希/感知指纹(pHash)/音频指纹 三层（防不住 AI 语义层，那需要换台词/换画面）。
ffmpeg 用 imageio_ffmpeg 自带的，无需系统装 ffmpeg。"""
import asyncio
import os
import random
import re
import subprocess

import imageio_ffmpeg

# 优先用环境变量指定的完整 ffmpeg（imageio 自带的那个 AAC 解码器有问题，处理不了音频/变速）。
# 想要变速+音频去重的，装个完整 ffmpeg 并设 FFMPEG_BIN 指过去即可；否则自动退化成"视频去重"。
FF = os.environ.get("FFMPEG_BIN") or imageio_ffmpeg.get_ffmpeg_exe()

# 各处理项默认值（前端可逐项开关+改参数）
DEFAULTS = {
    "metadata": True,                 # 改元数据/MD5（重编码本身就变MD5，再随机化元数据）
    "crop": True, "crop_pct": 4.0,    # 中心裁剪 N%（改分辨率+像素→打乱pHash）
    "color": True, "bright": 0.02, "contrast": 1.03, "sat": 1.05,   # 微调色
    "speed": True, "speed_factor": 1.03,   # 变速（视频+音频一起，0.5~2.0）
    "fps": False, "fps_value": 30,
    "hflip": False,                   # 镜像翻转（很猛但肉眼可见）
    "audio_pitch": False, "pitch": 1.0,    # 音频变调（半音）
    "trim": False, "trim_start": 0.0, "trim_end": 0.0,   # 掐头去尾（秒）
    "crf": 23,                        # 输出画质（越小越清晰越大）
    "randomize": False,               # 每条视频参数随机微抖（批量时各不相同）
}


def _rand_tag():
    return "".join(random.choice("abcdefghijklmnopqrstuvwxyz0123456789") for _ in range(16))


def probe_duration(path) -> float:
    """读时长（秒）。掐头去尾要用。"""
    try:
        r = subprocess.run([FF, "-i", str(path)], capture_output=True, text=True, errors="ignore")
        m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.?\d*)", r.stderr)
        if m:
            h, mi, s = m.groups()
            return int(h) * 3600 + int(mi) * 60 + float(s)
    except Exception:
        pass
    return 0.0


def _jit(v, pct, lo=None, hi=None):
    d = abs(v) * pct if v else pct
    x = v + random.uniform(-d, d)
    if lo is not None:
        x = max(lo, x)
    if hi is not None:
        x = min(hi, x)
    return round(x, 4)


def build_cmd(inp, outp, o, duration=0.0, audio_copy=False):
    """audio_copy=True：不处理音频(直接复制)，同时放弃变速(避免音画不同步)——
    给自带 ffmpeg 音频解码坏掉时的降级路径。"""
    o = {**DEFAULTS, **(o or {})}
    if o.get("randomize"):   # 每条随机微抖，批量各不相同
        o["crop_pct"] = _jit(o["crop_pct"], 0.35, 1.0, 12.0)
        o["speed_factor"] = _jit(o["speed_factor"], 0.02, 0.9, 1.12)
        o["bright"] = _jit(o["bright"], 0.6, -0.06, 0.08)
        o["contrast"] = _jit(o["contrast"], 0.03, 0.95, 1.1)
        o["sat"] = _jit(o["sat"], 0.05, 0.9, 1.2)
    args = [FF, "-y"]
    ss = float(o.get("trim_start") or 0) if o.get("trim") else 0.0
    if ss > 0:
        args += ["-ss", f"{ss:.3f}"]
    args += ["-i", str(inp)]
    if o.get("trim") and duration:
        t = max(0.5, duration - ss - float(o.get("trim_end") or 0))
        args += ["-t", f"{t:.3f}"]

    vf, af = [], []
    if o.get("crop"):
        p = max(0.0, 1 - float(o["crop_pct"]) / 100.0)
        vf.append(f"crop=trunc(iw*{p:.4f}/2)*2:trunc(ih*{p:.4f}/2)*2")
    if o.get("hflip"):
        vf.append("hflip")
    if o.get("color"):
        vf.append(f"eq=brightness={o['bright']}:contrast={o['contrast']}:saturation={o['sat']}")
    spd = float(o.get("speed_factor", 1.0)) if o.get("speed") else 1.0
    spd = min(2.0, max(0.5, spd))
    if spd != 1.0 and not audio_copy:       # 变速要音画一起改，降级时放弃变速
        vf.append(f"setpts=PTS/{spd}")
    if o.get("fps"):
        vf.append(f"fps={int(o.get('fps_value', 30))}")
    if not audio_copy:
        if spd != 1.0:
            af.append(f"atempo={spd}")
        if o.get("audio_pitch"):
            factor = 2 ** (float(o.get("pitch", 0)) / 12.0)
            af.append(f"asetrate=44100*{factor:.5f},aresample=44100,atempo={1/factor:.5f}")

    if vf:
        args += ["-vf", ",".join(vf)]
    if af:
        args += ["-af", ",".join(af)]
    args += ["-c:v", "libx264", "-preset", "veryfast", "-crf", str(int(o.get("crf", 23)))]
    args += (["-c:a", "copy"] if audio_copy else ["-c:a", "aac", "-b:a", "128k"])
    if o.get("metadata"):
        args += ["-map_metadata", "-1",
                 "-metadata", f"comment={_rand_tag()}", "-metadata", f"title={_rand_tag()}"]
    args += ["-movflags", "+faststart", str(outp)]
    return args


async def _run(cmd):
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
    _, err = await proc.communicate()
    return proc.returncode, (err or b"").decode("utf-8", "ignore")


async def source_ok(inp) -> bool:
    """抽一帧判断源能否正常解码。付费/加密短剧下载下来是"灰帧"（能过 -i 但解不出画面），
    去重也救不了，提前拦下别输出垃圾。检测本身失败就放行，不误伤。"""
    tmp = str(inp) + ".probe.png"
    try:
        proc = await asyncio.create_subprocess_exec(
            FF, "-y", "-ss", "2", "-i", str(inp), "-frames:v", "1", tmp,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await proc.communicate()
        from PIL import Image
        with Image.open(tmp) as im:
            lo, hi = im.convert("L").getextrema()
        return (hi - lo) > 18          # 灰帧 min≈max；有画面则明暗跨度大
    except Exception:
        return True
    finally:
        try:
            os.remove(tmp)
        except Exception:
            pass


async def process(inp, outp, opts, on_status=None):
    """跑一条去重。返回 (ok, msg)。音频处理失败(自带ffmpeg解码坏)会自动降级成视频去重。"""
    def st(m):
        if on_status:
            on_status(m)
    o = {**DEFAULTS, **(opts or {})}
    st("检查源视频…")
    if not await source_ok(inp):
        return False, "源视频损坏/无法解码（大概率是付费或加密短剧，下载下来画面就是坏的，去重救不了）"
    dur = probe_duration(inp) if o.get("trim") else 0.0
    wants_audio = (o.get("speed") and float(o.get("speed_factor", 1)) != 1) or o.get("audio_pitch")
    st("ffmpeg 处理中…")
    try:
        rc, err = await _run(build_cmd(inp, outp, o, dur))
        if rc == 0:
            return True, "处理完成"
        if wants_audio:   # 大概率是音频解码失败 → 降级视频去重(音频直接复制,放弃变速)
            st("音频处理不可用，改视频去重…")
            rc2, err2 = await _run(build_cmd(inp, outp, o, dur, audio_copy=True))
            if rc2 == 0:
                return True, "处理完成（音频未改，仅视频去重）"
            err = err2
        tail = err.strip().splitlines()[-1:] or [""]
        return False, "ffmpeg 失败: " + tail[0][:120]
    except Exception as e:
        return False, str(e)[:140]
