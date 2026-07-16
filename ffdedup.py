# -*- coding: utf-8 -*-
"""纯代码去重导出：绕开剪映，用 ffmpeg 直接合成去重 mp4。
组成：主画面(集数,可顺序拼接) + 上下蒙版视频(线性渐隐融合) + 调色/变速/微裁剪/镜像 + 贴纸 + BGM。
去重强度 ≥ 剪映（蒙版/变速/调色/贴纸 全还原，另加随机微裁剪/镜像/元数据清空）。"""
import glob
import math
import os
import random
import re
import subprocess
import tempfile
import uuid
from datetime import datetime, timedelta

import imageio_ffmpeg
import numpy as np
from PIL import Image

FF = imageio_ffmpeg.get_ffmpeg_exe()
# 打包版后端 console=False 没有控制台：不加这个标志，每跑一次 ffmpeg 客户屏幕上就弹一个黑窗
NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)

_NVENC = None      # None=未检测；True/False=可用性(缓存)


def _nvenc_ok() -> bool:
    """检测 NVIDIA h264_nvenc 硬件编码是否可用(有N卡就用GPU编码,快5-10倍)。只检一次,缓存。"""
    global _NVENC
    if _NVENC is not None:
        return _NVENC
    try:
        # 用 testsrc 试编码一小段到 nul,能成=可用
        r = subprocess.run(
            [FF, "-hide_banner", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=30:d=1",
             "-c:v", "h264_nvenc", "-f", "null", "-"],
            capture_output=True, timeout=30, creationflags=NOWIN)
        _NVENC = (r.returncode == 0)
    except Exception:
        _NVENC = False
    return _NVENC


_VEXT = (".mp4", ".mov", ".mkv", ".m4v")
_AEXT = (".mp3", ".wav", ".m4a", ".aac", ".flac")
_IEXT = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp")


def scan(folder, exts):
    out = []
    if folder and os.path.isdir(folder):
        for f in sorted(os.listdir(folder)):
            if f.lower().endswith(exts):
                out.append(os.path.join(folder, f))
    return out


def _natkey(p):
    n = os.path.basename(p)
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", n)]


def _ep_num_of(p):
    """从文件名提取集号(第X集 / 开头数字 / 任意数字)；取不到返回 0。"""
    b = os.path.splitext(os.path.basename(p))[0]
    m = re.search(r"第\s*(\d+)\s*集", b) or re.match(r"(\d+)", b) or re.search(r"(\d+)", b)
    return int(m.group(1)) if m else 0


def _depad_path(path):
    """补零成品名 → 旧的不补零名(第01集→第1集, 第01-02集→第1-2集)。
    用于续跑时识别旧导出：旧成品已渲染好就直接改名迁移，绝不重渲染留下重复文件。"""
    d, b = os.path.split(path)
    b2 = re.sub(r"第0*(\d+)(?:-0*(\d+))?集",
                lambda m: (f"第{int(m.group(1))}-{int(m.group(2))}集"
                           if m.group(2) else f"第{int(m.group(1))}集"),
                b)
    return os.path.join(d, b2)


def _ffmpeg_err(stderr_bytes) -> str:
    """从 ffmpeg stderr 里挑出真正的错误行,而不是盲取最后300字符(那常只截到进度行 frame=.../time=N/A,
    把真错误藏起来)。优先含错误关键词的行,其次最后几行非进度行。"""
    s = (stderr_bytes or b"").decode("utf-8", "ignore")
    lines = [l.strip() for l in s.splitlines() if l.strip()]
    KW = ("Error", "error", "Invalid", "invalid", "failed", "Failed", "Conversion",
          "No such", "does not", "annot", "nable to", "could not", "Could not",
          "not found", "denied", "Permission", "buffer", "Overflow", "overflow")
    errs = [l for l in lines if any(k in l for k in KW) and "frame=" not in l and "time=" not in l]
    if errs:
        return " ‖ ".join(errs[-3:])[:400]
    non_prog = [l for l in lines if "frame=" not in l and "size=" not in l and "bitrate=" not in l][-4:]
    return (" ‖ ".join(non_prog) or s[-300:])[:400]


def _rr(pair, dflt):
    """[lo,hi] 里随机取一个；单值直接用；空=默认。"""
    if pair is None or pair == "":
        return dflt
    if isinstance(pair, (int, float)):
        return float(pair)
    lo, hi = float(pair[0]), float(pair[1])
    return round(random.uniform(lo, hi), 3) if lo != hi else lo


def _probe_wh(path):
    try:
        out = subprocess.run([FF, "-i", path], capture_output=True, creationflags=NOWIN).stderr.decode("utf-8", "ignore")
        m = re.search(r"Video:.*?(\d{2,5})x(\d{2,5})", out)
        if m:
            return int(m.group(1)), int(m.group(2))
    except Exception:
        pass
    return 1920, 1080


def _probe_has_audio(path):
    try:
        out = subprocess.run([FF, "-i", path], capture_output=True, creationflags=NOWIN).stderr.decode("utf-8", "ignore")
        return "Audio:" in out
    except Exception:
        return True


def _probe_dur(path):
    try:
        out = subprocess.run([FF, "-i", path], capture_output=True, creationflags=NOWIN).stderr.decode("utf-8", "ignore")
        m = re.search(r"Duration: (\d+):(\d+):([\d.]+)", out)
        if m:
            return int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3])
    except Exception:
        pass
    return 0.0


def _mix_name(chunk, prefix, start_ep=None, pad=2):
    """成品名 = 合集名_第XX-YY集(集号补零到 pad 位)。文件名带数字按数字取;不带数字按顺序位置(start_ep起)兜底。
    补零(第01集/第002集)让文件名在 Explorer/后端/发布里都天然按集数升序;发布默认短标题会去零显示成"第X集"。"""
    coll = ""
    try:
        coll = os.path.basename(os.path.dirname(chunk[0]))
    except Exception:
        pass
    nums = []
    for p in chunk:
        base = os.path.splitext(os.path.basename(p))[0]
        m = (re.match(r"(\d+)", base) or re.search(r"第\s*(\d+)\s*集", base)
             or re.search(r"(\d+)", base))
        if m:
            nums.append(int(m.group(1)))
    if len(nums) != len(chunk) and start_ep is not None:
        nums = list(range(start_ep, start_ep + len(chunk)))   # 文件名没数字→按集数顺序位置
    if nums:
        nums = sorted(set(nums))
        w = max(1, int(pad))
        rng = f"{nums[0]:0{w}d}-{nums[-1]:0{w}d}" if len(nums) > 1 else f"{nums[0]:0{w}d}"
        safe = re.sub(r'[\\/:*?"<>|]', "", coll)[:40] or prefix
        return f"{safe}_第{rng}集"
    return prefix


def _dhash_frame(video, t, size=8):
    """抽 video 在 t 秒的一帧算 dHash(64bit 感知指纹)。取不到返回 None。"""
    try:
        r = subprocess.run(
            [FF, "-v", "error", "-ss", f"{max(0.0, t):.2f}", "-i", str(video),
             "-frames:v", "1", "-vf", f"scale={size+1}:{size}", "-pix_fmt", "gray",
             "-f", "rawvideo", "-"], capture_output=True, timeout=30, creationflags=NOWIN)
        buf = r.stdout
        if len(buf) < (size + 1) * size:
            return None
        bits = 0
        for y in range(size):
            row = y * (size + 1)
            for x in range(size):
                bits = (bits << 1) | (1 if buf[row + x] < buf[row + x + 1] else 0)
        return bits
    except Exception:
        return None


def fp_diff_score(src_first_ep, out_path, spd=1.0, head=0.0):
    """【去重自检分】对齐时间轴抽3帧,算成品vs原片的感知指纹差异%(0=没变,越高改得越狠)。
    只对首集时间段采样(多集拼接跨集难对齐)。<8% 说明去重太弱该提醒。取不到返回 None。"""
    try:
        d_src = _probe_dur(src_first_ep)
        d_out = _probe_dur(out_path)
        if d_src <= 6 or d_out <= 6:
            return None
        span = min(d_out, max(1.0, (d_src - head - 2) / max(spd, 0.01)))  # 成品里对应首集的时段
        diffs = []
        for f in (0.2, 0.5, 0.8):
            t_out = span * f
            t_src = head + t_out * spd            # 成品变速spd倍→原片时间轴要乘回去
            a = _dhash_frame(src_first_ep, t_src)
            b = _dhash_frame(out_path, t_out)
            if a is None or b is None:
                continue
            diffs.append(bin(a ^ b).count("1") / 64 * 100)
        return round(sum(diffs) / len(diffs), 1) if diffs else None
    except Exception:
        return None


import threading as _threading
_MASK_ORDER = {}                      # {(folder, group_key): 素材顺序} 同一视频各变体共享,按变体号取→不重复
_MASK_ORDER_LOCK = _threading.Lock()


def _distinct_pick(vids, n):
    """给 n 个变体分配素材，尽量两两不同：素材数≥n 用 sample(严格不重复)；不够则洗牌轮询(相邻不重复)。"""
    if not vids:
        return [None] * n
    if len(vids) >= n:
        return random.sample(vids, n)
    out, pool = [], []
    while len(out) < n:
        if not pool:
            pool = random.sample(vids, len(vids))
            if out and pool and pool[0] == out[-1] and len(pool) > 1:
                pool.append(pool.pop(0))
        out.append(pool.pop(0))
    return out


def _assign_mask(vids, folder, group_key, v, nv):
    """同一视频(group_key)的 nv 个变体，按变体号 v 取不重复的蒙版素材：首个变体定这一组随机顺序并缓存，
    后续变体复用同一顺序 → 上/下各自两两不同(素材够时严格不重复，不够时轮询不相邻重复)。"""
    if not vids:
        return None
    ck = (folder, group_key)
    with _MASK_ORDER_LOCK:
        order = _MASK_ORDER.get(ck)
        if order is None:
            order = _distinct_pick(vids, max(1, int(nv or 1)))
            _MASK_ORDER[ck] = order
    return order[v % len(order)]


def dedup_render(main_paths, cfg, out_path, on_status=None, params_out=None, variant=(0, 1)):
    """把 main_paths（一集或多集顺序拼接）去重合成到 out_path。返回 out_path。
    params_out=dict 时回填本次随机抽到的 spd/head(供自检分对齐时间轴)。"""
    # 【提速44%·实测】输出分辨率跟随源：抖音源基本是720x1280，先放大到1080再跑全部重滤镜
    # (lens/rotate/eq/unsharp/overlay)纯属浪费——放大不产生画质。源≥1000宽才用1080x1920。
    # cfg.out_res: "auto"(默认,跟随源) / "1080" / "720" 可强制。视频号官方建议720p及以上。
    W, H = 1080, 1920
    _rm = str(cfg.get("out_res", "auto") or "auto").lower()
    if _rm in ("720", "720p"):
        W, H = 720, 1280
    elif _rm not in ("1080", "1080p"):          # auto：按第一集的真实宽度定
        try:
            _sw, _sh = _probe_wh(main_paths[0])
            if _sw and 0 < _sw < 1000:
                W, H = 720, 1280
        except Exception:
            pass
    inputs, fc = [], []
    n_main = len(main_paths)
    # 【实测结论】GPU 解码(-hwaccel cuda)对本滤镜链反而更慢:NVDEC→CPU下载的每帧拷贝开销
    # 大于解码省的(源是普通H.264,CPU解码本就快)。所以解码留CPU,只把编码交GPU(NVENC快1.7倍)。
    HWDEC = []

    # ---- 输入：主画面各集 ----
    for p in main_paths:
        inputs += HWDEC + ["-i", p]

    # 主画面：拼接(多集)→满屏→微裁剪→镜像→调色→变速
    if n_main > 1:
        # 各集分辨率/帧率/音频可能不同(如有720x1280),拼接前先统一,否则 concat 报错
        cat = ""
        for i in range(n_main):
            fc.append(f"[{i}:v:0]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},fps=30,setsar=1,format=yuv420p[cv{i}]")
            fc.append(f"[{i}:a:0]aresample=44100,aformat=sample_fmts=fltp:channel_layouts=stereo[ca{i}]")
            cat += f"[cv{i}][ca{i}]"
        fc.append(f"{cat}concat=n={n_main}:v=1:a=1[mv][ma]")
        vsrc, asrc = "[mv]", "[ma]"
    else:
        vsrc, asrc = "[0:v:0]", "[0:a:0]"

    color = cfg.get("color") or {}
    bri = _rr(color.get("brightness"), 0.0)
    con = _rr(color.get("contrast"), 1.0)
    sat = _rr(color.get("saturation"), 1.0)
    sharp = _rr(color.get("sharpen"), 0.3)
    hue = _rr(color.get("hue"), 0.0)
    gam = _rr(color.get("gamma"), 1.0) or 1.0                  # Gamma微调(0.97~1.03)
    spd = _rr(cfg.get("speed"), 1.0) or 1.0
    mirror = random.random() < float(cfg.get("mirror_prob", 0) or 0)

    # 轻微放大(随机区间,如1.02~1.05);兼容旧 crop 字段(0~0.03 → 1.00~1.03)
    zoom = _rr(cfg.get("zoom"), 0.0)
    if not zoom or zoom < 1.0:
        zoom = 1.0 + max(0.0, _rr(cfg.get("crop"), 0.0))
    rot = _rr(cfg.get("rotate"), 0.0) * random.choice([-1, 1])   # 微旋转(度,随机正负)
    lens = _rr(cfg.get("lens"), 0.0)                             # 广角桶形畸变 k1
    persp = max(0.0, min(0.03, _rr(cfg.get("perspective"), 0.0)))  # 透视微形变幅度(占宽高比例);比平移更能扰DCT低频,无感
    # 旋转/畸变/透视都会带进黑边,预先多放大一点作补偿,最后中心裁回 1080x1920
    margin = 1.0 + abs(math.radians(rot)) * 1.9 + max(0.0, lens) * 1.2 + persp * 1.6
    zt = max(1.0, zoom) * margin
    Wz, Hz = (int(W * zt) // 2) * 2, (int(H * zt) // 2) * 2
    vf = f"scale={Wz}:{Hz}:force_original_aspect_ratio=increase,crop={Wz}:{Hz}"
    if lens:
        vf += f",lenscorrection=k1={-lens:.4f}:k2=0:i=bilinear"   # 负k1=桶形(广角感)
    if rot:
        vf += f",rotate={rot:.3f}*PI/180:c=black"
    if persp:                                  # 透视微形变:四角各独立向内微移(<margin余量→裁切后不露黑边),比平移更能扰DCT低频
        dx = lambda: round(random.uniform(0, persp) * Wz, 1)
        dy = lambda: round(random.uniform(0, persp) * Hz, 1)
        vf += (f",perspective=x0={dx()}:y0={dy()}:x1={Wz-dx()}:y1={dy()}"
               f":x2={dx()}:y2={Hz-dy()}:x3={Wz-dx()}:y3={Hz-dy()}:sense=source")
    vf += (f",crop={W}:{H},"
           f"eq=brightness={bri}:contrast={con}:saturation={sat}:gamma={gam:.3f},hue=h={hue},"
           f"unsharp=5:5:{sharp}")
    if mirror:
        vf += ",hflip"
    vg = cfg.get("vignette") or {}
    if vg.get("enable"):
        vf += f",vignette=angle={_rr(vg.get('range'), 0.15):.3f}"  # 轻暗角
    # 【实锤·2026-07-15】noise 滤镜与音频 afreqshift(声纹去重核心武器)同处一张滤镜图会让 AAC 编码器整条报
    # -22(Error submitting audio frame)——这是"家家有本难念的经"等开了噪点的批次全失败的真凶(与掐头去尾无关)。
    # afreqshift 去重价值(确定性打穿Shazam声纹)远高于极淡噪点,且噪点与裁剪/调色/运镜/蒙版/透视/AB高度冗余,
    # 故 afreqshift 开启时(默认开)跳过 noise,规避这个 ffmpeg 滤镜冲突。
    _afreqshift_on = (cfg.get("audio") or {}).get("freqshift", True)
    if cfg.get("noise") and not _afreqshift_on:
        vf += ",noise=alls=6:allf=t"                         # 极淡噪点(仅在关掉频移时才叠,否则冲突)
    # 【动态运镜·改结构去重】放大留余量,裁切窗口按正弦缓慢漂移(x/y 不同频率→椭圆浮动,永不重复)。
    # 逐帧裁切位置都不同→彻底打乱平台的逐帧感知哈希/帧序列指纹;漂移幅度=amp(默认4%)且极慢→肉眼无感。
    motion = cfg.get("motion") or {}
    if motion.get("enable"):
        amp = max(0.0, min(0.12, _rr(motion.get("amp"), 0.04)))
        Wm = (int(W * (1 + amp)) // 2) * 2
        Hm = (int(H * (1 + amp)) // 2) * 2
        vf += (f",scale={Wm}:{Hm},crop={W}:{H}:"
               f"x=(iw-ow)*(0.5+0.5*sin(t*0.10)):y=(ih-oh)*(0.5+0.5*sin(t*0.13))")
    vf += f",setpts=PTS/{spd},fps=30"
    # 抽帧:先固定成 30fps,再每秒随机丢 N 帧,紧接第二个 fps=30 用相邻帧把空位补回——
    # 总时长/帧率都不变(音画不漂),只把"每秒某一帧"换成邻帧的重复,打乱平台的逐帧哈希与帧序列指纹。
    # 与动态运镜冗余(运镜已逐帧改裁切),多一层帧序列扰动、肉眼无感。相位 off 每条随机→各成品丢的位置不同。
    _df = cfg.get("dropframe") or {}
    if _df.get("enable"):
        _dn = max(1, min(15, int(round(_rr(_df.get("per_sec"), 1.0) or 1))))   # 每秒抽帧数,默认1
        _period = max(2, round(30 / _dn))
        _off = random.randint(0, _period - 1)
        vf += f",select=not(eq(mod(n+{_off}\\,{_period})\\,0)),fps=30"          # 逗号在表达式里要转义(\\,)
    vf += ",setsar=1,format=yuv420p"   # 强制方形像素:防广角/旋转/运镜留下歪SAR导致播放器把画面压成窄条
    fc.append(f"{vsrc}{vf}[m]")
    cur = "[m]"

    # ---- 音频链[核心去重·打穿声纹指纹] rubberband高质量变速+变调 + 频移(击碎Shazam地标) + 动态压缩 + EQ抖动 ----
    # 视频号判"重复"的真凶是音频指纹(Shazam式峰值地标哈希):它抗重编码/音量/EQ/混BGM,只怕"变调+频移"。
    # 实测:rubberband tempo=spd 与视频 setpts=PTS/spd 同向→音画同步不漂;afreqshift 确定性平移频谱峰值坐标→
    # 经典地标失配。全程人耳无感。tempo 必须=spd(勿乘随机系数,否则渐进漂移坏音画同步)。
    audio = cfg.get("audio") or {}
    has_audio = n_main > 1 or _probe_has_audio(main_paths[0])
    acur = None
    if has_audio:
        pitch = _rr(audio.get("pitch"), None)
        if not pitch:                                          # 默认也给±1.5%微变调(无感),用户可用 pitch 覆盖
            pitch = round(random.uniform(0.985, 1.015), 4)
        pitch = max(0.9, min(1.1, pitch))
        avol = max(0.5, min(1.5, _rr(audio.get("volume"), 1.0) or 1.0))
        tempo = max(0.5, min(2.0, spd))
        af = f"aresample=44100,rubberband=tempo={tempo:.4f}:pitch={pitch:.4f}"
        if audio.get("freqshift", True):                       # 【核心】频移:确定性打散Shazam声纹地标,人耳无感(±10~20Hz)
            hz = round(random.uniform(10, 20), 1) * random.choice([-1, 1])
            af += f",afreqshift=shift={hz}"
        if audio.get("compress", True):                        # 动态压缩:改响度包络/声纹另一维,无感
            af += (f",acompressor=threshold=-18dB:ratio={round(random.uniform(2.5, 3.5), 1)}"
                   f":attack=20:release=250:makeup=2")
        af += f",volume={avol:.3f}"
        if audio.get("eq", True):                              # 频谱扰动(再打碎频域指纹)
            af += (f",bass=g={round(random.uniform(-1.5, 1.5), 2)}"
                   f",treble=g={round(random.uniform(-1.5, 1.5), 2)}")
        af += ",aresample=44100"
        fc.append(f"{asrc}{af}[aud]")
        acur = "[aud]"

    # ---- 上下蒙版：从用户参考视频(1.mp4)逐行实测复刻的模型(数值验证<0.6%差,勿改) ----
    # ①素材按宽度等比缩放贴上/下边(不变形不满屏裁剪,1920x1080→1080x608);
    # ②alpha从边缘=op(0.6~0.8随机)**线性**降到0,跨度=band_h(实测430px≈1/5屏),无实心段;
    # ③中间完全无叠加。mask用PIL生成像素级精确PNG——ffmpeg gradients滤镜默认会旋转
    #   (speed=0.01)且渐变非线性(430px声明~230px到0),两个坑都实测踩过,严禁再用。
    bands = cfg.get("bands") or {}
    idx = n_main
    mask_files = []
    for key in ("top", "bottom"):
        b = bands.get(key) or {}
        vids = scan(b.get("folder"), _VEXT)
        if not b.get("enable") or not vids:
            continue
        op = max(0.0, min(1.0, _rr(bands.get("opacity"), 0.7)))   # 每条带独立随机峰值透明度
        # band_h 配置值按 1080x1920 设计——输出分辨率变了(如720x1280)按高度等比缩放
        bh = max(2, int(_rr(bands.get("band_h"), 430) * H / 1920))
        _gk = re.sub(r'(_变体\d+)?\.mp4$', '', os.path.basename(out_path))   # 同一视频各变体 group_key 相同
        bv = _assign_mask(vids, b.get("folder"), _gk, variant[0], variant[1])
        sw, sh = _probe_wh(bv)
        blk = max(2, (int(W * sh / max(sw, 1)) // 2) * 2)     # 等比缩放后的块高(取偶)
        fade = min(bh, blk)
        m = np.zeros((blk, W), np.uint8)
        ramp = (np.linspace(op, 0.0, fade) * 255).astype(np.uint8)[:, None]
        if key == "top":
            m[:fade] = ramp                                   # 顶边最亮,向下线性到0
            over = "0:0"
        else:
            m[blk - fade:] = ramp[::-1]                       # 底边最亮,向上线性到0
            over = f"0:{H - blk}"
        # 【致命坑·2026-07-12实锤】蒙版文件名必须全局唯一(uuid)：并发渲染时多个任务同时写
        # 同一路径(旧命名=pid+idx,并发下完全相同)会写出损坏PNG；ffmpeg -loop 1 每帧重读它,
        # 读到坏签名→alphamerge断供→overlay永久卡住→输出0字节+CPU空转到超时。
        mp = os.path.join(tempfile.gettempdir(), f"ffd_mask_{key}_{uuid.uuid4().hex[:12]}.png")
        Image.fromarray(m).save(mp)
        mask_files.append(mp)
        inputs += ["-stream_loop", "-1"] + HWDEC + ["-i", bv]   # 广角/8K素材是视频→GPU解码
        vin = idx
        idx += 1
        inputs += ["-loop", "1", "-i", mp]                       # 蒙版是图片→不加hwaccel
        min_ = idx
        idx += 1
        fc.append(f"[{vin}:v]scale={W}:{blk},fps=30[bv{vin}]")
        fc.append(f"[{min_}:v]format=gray[gm{vin}]")
        fc.append(f"[bv{vin}][gm{vin}]alphamerge[bd{vin}]")
        fc.append(f"{cur}[bd{vin}]overlay={over}:shortest=1[o{vin}]")
        cur = f"[o{vin}]"

    # ---- 贴纸：随机贴用户图片 ----
    stk = cfg.get("stickers") or {}
    imgs = scan(stk.get("folder"), _IEXT)
    if stk.get("enable") and imgs:
        pos = {"tl": (0.08, 0.10), "tr": (0.72, 0.10), "bl": (0.08, 0.80),
               "br": (0.72, 0.80), "center": (0.40, 0.45)}
        poskeys = stk.get("positions") or list(pos)
        for _ in range(int(stk.get("count", 2) or 2)):
            img = random.choice(imgs)
            inputs += ["-i", img]
            sc = _rr(stk.get("scale"), 0.3)
            al = _rr(stk.get("alpha"), 0.9)
            pk = random.choice(poskeys)
            px, py = pos.get(pk, (0.4, 0.45))
            px = int((px + random.uniform(-0.03, 0.03)) * W)
            py = int((py + random.uniform(-0.03, 0.03)) * H)
            fc.append(f"[{idx}:v]scale=iw*{sc}:-1,format=rgba,colorchannelmixer=aa={al}[st{idx}]")
            fc.append(f"{cur}[st{idx}]overlay={px}:{py}[o{idx}]")
            cur = f"[o{idx}]"
            idx += 1

    # ---- AB低透明度叠加：无关素材B极低透明度铺满叠在正片A上,每个像素被B混合污染→打乱画面感知哈希(pHash) ----
    # 【只碰画面②层,不碰音频③】纯像素层保险,透明度低(默认8~14%)人眼近乎无感;素材复用蒙版那套随机指派(各变体叠不同B、组内不撞)。
    ab = cfg.get("abblend") or {}
    abvids = scan(ab.get("folder"), _VEXT)
    if ab.get("enable") and abvids:
        _abk = re.sub(r'(_变体\d+)?\.mp4$', '', os.path.basename(out_path))
        bv = _assign_mask(abvids, ab.get("folder"), _abk, variant[0], variant[1])
        aa = _rr(ab.get("opacity"), None)
        if not aa:
            aa = round(random.uniform(0.08, 0.14), 3)       # 默认B层透明度8~14%(>25%会有重影,肉眼可见)
        aa = max(0.02, min(0.30, aa))
        inputs += ["-stream_loop", "-1"] + HWDEC + ["-i", bv]   # B是视频→无限循环铺满A全长+GPU解码
        fc.append(f"[{idx}:v]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},fps=30,"
                  f"format=rgba,colorchannelmixer=aa={aa:.3f}[abv{idx}]")
        fc.append(f"{cur}[abv{idx}]overlay=0:0:shortest=1[abo{idx}]")   # B循环无限、A有限→shortest收在A长度
        cur = f"[abo{idx}]"
        idx += 1

    # ---- 特效叠加：剪映式飘雪/光效/边框 —— 叠一层特效素材 ----
    # 粒子视频(黑底)走 screen 混合(黑透掉只留白色雪花/光);边框PNG或带alpha素材走 overlay(保留素材自带透明形状)。
    # 【只碰画面②层】动态粒子每帧位置都不同→逐帧pHash全打乱,且看着像有意加的创意效果(比AB鬼影更像原创)。素材复用蒙版那套随机指派。
    fx = cfg.get("fx") or {}
    fxfiles = scan(fx.get("folder"), _VEXT + (".webm",)) + scan(fx.get("folder"), _IEXT)
    if fx.get("enable") and fxfiles:
        _fxk = re.sub(r'(_变体\d+)?\.mp4$', '', os.path.basename(out_path))
        fv = _assign_mask(fxfiles, fx.get("folder"), _fxk, variant[0], variant[1])
        op = _rr(fx.get("opacity"), None)
        if not op:
            op = round(random.uniform(0.7, 0.9), 3)
        op = max(0.05, min(1.0, op))
        is_img = os.path.splitext(fv)[1].lower() in _IEXT
        mode = (fx.get("mode") or "screen").lower()
        if is_img:
            inputs += ["-loop", "1", "-i", fv]                          # 边框PNG→单帧循环铺满全长
        else:
            inputs += ["-stream_loop", "-1"] + HWDEC + ["-i", fv]       # 特效视频→无限循环
        if mode == "screen" and not is_img:                             # 黑底粒子:screen让黑透掉,只留亮部
            # 【关键·2026-07-15实锤】screen 混合必须在 RGB(gbrp)里做!在 yuv420p 里 blend=screen 会把
            # chroma(U/V)通道也按screen公式抬高(128→192),U↑+V↑=更蓝更红=整帧染成品红(实测灰底叠后变(222,80,239))。
            # 转 gbrp→screen→再转回 yuv420p 才是正确的"黑透掉只留亮部"。
            fc.append(f"[{idx}:v]scale={W}:{H},fps=30,format=gbrp[fxv{idx}]")
            fc.append(f"{cur}format=gbrp[fxb{idx}]")
            fc.append(f"[fxb{idx}][fxv{idx}]blend=all_mode=screen:all_opacity={op:.3f}:shortest=1,format=yuv420p[fxo{idx}]")
        else:                                                           # 边框/透明素材:保留自带alpha(×op调强弱)覆盖
            fc.append(f"[{idx}:v]scale={W}:{H},fps=30,format=rgba,colorchannelmixer=aa={op:.3f}[fxv{idx}]")
            fc.append(f"{cur}[fxv{idx}]overlay=0:0:shortest=1[fxo{idx}]")
        cur = f"[fxo{idx}]"
        idx += 1

    # ---- BGM：随机一首铺满，跟处理后的原声混音 ----
    bgm = cfg.get("bgm") or {}
    music = scan(bgm.get("folder"), _AEXT)
    if bgm.get("enable") and music:
        bg = random.choice(music)
        inputs += ["-stream_loop", "-1", "-i", bg]
        vol = _rr(bgm.get("volume"), 0.18)
        fc.append(f"[{idx}:a]volume={vol}[bgm{idx}]")
        if acur:                                              # 原声 + BGM 混音
            # normalize=0：原声保持 100% 不变，BGM 按设定音量(0.1~0.16)隐约叠上去。
            # 默认 normalize=1 会把两路都归一化(各除以2)→原声被砍半(-6dB),实测确认,故关掉。
            fc.append(f"{acur}[bgm{idx}]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[aout]")
            acur = "[aout]"
        else:                                                 # 原片没音轨→BGM 直接当声音
            acur = f"[bgm{idx}]"
        idx += 1

    # ---- 掐头去尾：破坏首尾帧指纹 + 时长指纹（音画一起裁）----
    trim = cfg.get("trim") or {}
    head = max(0.0, _rr(trim.get("head"), 0.0))
    tail = max(0.0, _rr(trim.get("tail"), 0.0))
    if isinstance(params_out, dict):
        params_out.update(spd=spd, head=head)     # 自检分对齐时间轴用
    if head or tail:
        rng = f"start={head:.2f}"
        if tail:
            total = sum(_probe_dur(p) for p in main_paths) / spd
            if total > head + tail + 5:
                rng += f":end={total - tail:.2f}"
        fc.append(f"{cur}trim={rng},setpts=PTS-STARTPTS[vtr]")
        cur = "[vtr]"
        if acur:
            fc.append(f"{acur}atrim={rng},asetpts=PTS-STARTPTS[atr]")
            acur = "[atr]"

    filter_complex = ";".join(fc)
    # 提速：-sws_flags fast_bilinear 让所有 scale 用最快插值(视频画质几乎无差,scale是重滤镜)。
    # 线程不限制(默认多线程)——实测限4线程单条反而慢一倍(79→155s),且总吞吐无净增(与并发此消彼长)。
    cmd = ([FF, "-y", "-sws_flags", "fast_bilinear"] + inputs
           + ["-filter_complex", filter_complex, "-map", cur])
    if acur:
        cmd += ["-map", acur]
    # 视频编码：vbitrate>0 = 上传友好(限码率,体积可控)；=0 = 原画质(CRF恒定质量,体积大)
    try:
        vbr = float(cfg.get("vbitrate", 0) or 0)
    except Exception:
        vbr = 0
    # 【编码指纹随机化·防矩阵连坐】每条变体的编码器/GOP/码率/profile/音频码率/handler 全随机——否则一批变体
    # 走同一套参数,平台可按"同一产线指纹"把矩阵号聚簇连坐限流。再删 H.264 SEI(x264版本串/取证user_data),不动像素。
    gop = random.choice([48, 60, 72, 90, 120])
    # 有N卡就用 GPU 硬件编码(h264_nvenc)——比 libx264 快 5-10 倍；默认自动用,可用 use_gpu:false 关
    use_gpu = cfg.get("use_gpu", True) and _nvenc_ok()
    if use_gpu:
        venc = ["-c:v", "h264_nvenc", "-preset", random.choice(["p3", "p4", "p5", "p6"]),
                "-tune", "hq", "-g", str(gop), "-bf", str(random.choice([2, 3])),
                "-profile:v", random.choice(["high", "main"])]
        if vbr > 0:
            jb = round(vbr * (1 + random.uniform(-0.04, 0.04)), 3)   # ±4% 码率抖动打散粗筛聚类
            venc += ["-rc", "vbr", "-b:v", f"{jb:g}M", "-maxrate", f"{jb*1.1:g}M", "-bufsize", f"{jb*2:g}M"]
        else:
            venc += ["-rc", "vbr", "-cq", str(random.choice([21, 22, 23, 24]))]
    else:
        x264p = (f"keyint={gop}:min-keyint={gop//2}:scenecut={random.choice([0, 40])}"
                 f":ref={random.choice([2, 3, 4])}:bframes={random.choice([2, 3])}"
                 f":aq-mode={random.choice([1, 2])}:sei=0:no-info=1")   # sei=0 去 x264 版本水印
        venc = ["-c:v", "libx264", "-preset", cfg.get("preset") or random.choice(["veryfast", "faster", "fast"]),
                "-profile:v", random.choice(["high", "main"]), "-x264-params", x264p]
        if vbr > 0:
            jb = round(vbr * (1 + random.uniform(-0.04, 0.04)), 3)
            venc += ["-b:v", f"{jb:g}M", "-maxrate", f"{jb:g}M", "-bufsize", f"{jb*2:g}M"]
        else:
            venc += ["-crf", str(cfg.get("crf") or random.randint(19, 23))]
    audio_br = random.choice(["96k", "112k", "128k", "160k"])   # 音频码率抖动(原恒定128k=现成指纹)
    cmd += venc + ["-c:a", "aac", "-b:a", audio_br, "-pix_fmt", "yuv420p", "-shortest",
                   "-map_metadata", "-1", "-map_chapters", "-1",     # 清空元数据+章节
                   "-metadata", "handler_name=", "-metadata:s:v", "handler_name=",
                   "-metadata:s:a", "handler_name=",                 # 清 handler(VideoHandler/SoundHandler=现成容器指纹)
                   "-bsf:v", "filter_units=remove_types=6"]          # 删 H.264 SEI(版本串/取证user_data),不动一帧像素
    if cfg.get("fake_meta", True):
        # 清空后写入随机"拍摄时间"——全空的元数据本身也是特征,伪装成普通视频更自然
        dt = datetime.now() - timedelta(days=random.uniform(1, 45), hours=random.uniform(0, 20))
        cmd += ["-metadata", "creation_time=" + dt.strftime("%Y-%m-%dT%H:%M:%S.000000Z")]
    # 【原子输出】先渲染到 .part.mp4，成功后才改名成正式文件——
    # 中途被杀/超时/失败绝不留下顶着正式名字的残片(0字节假视频拿去发布=必失败的坑)。
    part = (out_path[:-4] if out_path.lower().endswith(".mp4") else out_path) + ".part.mp4"
    cmd += ["-movflags", "+faststart", part]
    if on_status:
        on_status(f"渲染 {os.path.basename(out_path)}…")
    # 超时保护：坏/超大输入不能让 ffmpeg 永久卡死队列。按输入总时长估算上限
    # (合成大约实时 0.6x~1.5x + 固定余量),封顶 2 小时。
    try:
        dur_est = sum(_probe_dur(p) for p in main_paths) or 600
    except Exception:
        dur_est = 600
    # 去重滤镜链慢(~0.7×实时),多条并发时每条还会慢几倍。超时按"时长×12+10分钟"给足,
    # 别误杀正在跑的任务(真卡死才会到上限)。上限放到2小时。
    timeout_s = min(7200, max(1800, int(dur_est * 12 + 600)))
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout_s, creationflags=NOWIN)
    except subprocess.TimeoutExpired:
        try:
            if os.path.exists(part):
                os.remove(part)              # 删掉半成品
        except OSError:
            pass
        raise RuntimeError(f"ffmpeg超时（>{timeout_s//60}分钟），已跳过。可能输入损坏或过大")
    finally:
        for mp in mask_files:
            try:
                os.remove(mp)
            except OSError:
                pass
    if r.returncode != 0:
        try:
            if os.path.exists(part):
                os.remove(part)              # 失败也不留残片
        except OSError:
            pass
        raise RuntimeError("ffmpeg失败：" + _ffmpeg_err(r.stderr))

    # ---- 每集结束插视频：把成品按集边界切开，每集后面(含最后一集)各插一条随机视频(9:16,不去重) ----
    # 对已渲染好的干净成品做二次拼接——它的音频是规矩的 aac 立体声，concat 才稳；插入片段不走去重
    # 滤镜(不盖蒙版/贴纸/调色)，原样缩放铺满 9:16。失败不影响已出的无插入成品。
    tailcfg = cfg.get("tailvid") or {}
    tail_vids = scan(tailcfg.get("folder"), _VEXT)
    if tailcfg.get("enable") and tail_vids and _probe_has_audio(part):
        main_dur = _probe_dur(part) or 0.0
        try:
            durs = [max(0.1, _probe_dur(p)) for p in main_paths]
        except Exception:
            durs = []
        # 每集在成品里的结束时刻 ≈ 累计集时长/变速 - 掐头(差一两秒无所谓，插在集之间)
        bounds, cum = [], 0.0
        for d in durs:
            cum += d
            bounds.append(cum / (spd or 1.0) - head)
        cuts = [b for b in bounds[:-1] if 0.3 < b < main_dur - 0.3]   # 集与集之间的内部切点
        segs, prev = [], 0.0
        for c in cuts:
            segs.append((prev, c)); prev = c
        segs.append((prev, None))                                     # 最后一段到结尾
        picks = [random.choice(tail_vids) for _ in range(len(segs))]  # 每段后插一条(随机)
        ins_out = part[:-4] + ".ins.mp4"

        def _ins_cmd(silent):
            tin = ["-i", part]        # 输入0 = 成品
            tfc, order = [], []
            ii = 1                    # 下一个输入编号
            for i, (s, e) in enumerate(segs):
                rng = f"start={s:.3f}" + (f":end={e:.3f}" if e is not None else "")
                tfc.append(f"[0:v]trim={rng},setpts=PTS-STARTPTS,fps=30,setsar=1,format=yuv420p[sv{i}]")
                tfc.append(f"[0:a]atrim={rng},asetpts=PTS-STARTPTS,aresample=44100,aformat=sample_fmts=fltp,pan=stereo|c0=c0|c1=c1[sa{i}]")
                order.append(f"[sv{i}][sa{i}]")
                iv = picks[i]
                tin += ["-err_detect", "ignore_err", "-fflags", "+discardcorrupt", "-i", iv]
                vin = ii; ii += 1
                tfc.append(f"[{vin}:v:0]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},fps=30,setsar=1,format=yuv420p[iv{i}]")
                if (not silent) and _probe_has_audio(iv):
                    tfc.append(f"[{vin}:a:0]aresample=44100,aformat=sample_fmts=fltp,pan=stereo|c0=c0|c1=c1[ia{i}]")
                else:                 # 插入片段没音轨/音频损坏→补等长静音，保证画面照样接上
                    _d = _probe_dur(iv) or 3.0
                    tin += ["-f", "lavfi", "-t", f"{_d:.2f}", "-i", "anullsrc=r=44100:cl=stereo"]
                    ain = ii; ii += 1
                    tfc.append(f"[{ain}:a]aformat=sample_fmts=fltp[ia{i}]")
                order.append(f"[iv{i}][ia{i}]")
            nseg = len(segs) * 2
            tfc.append("".join(order) + f"concat=n={nseg}:v=1:a=1[v][a]")
            return ([FF, "-y", "-sws_flags", "fast_bilinear"] + tin
                    + ["-filter_complex", ";".join(tfc), "-map", "[v]", "-map", "[a]",
                       "-c:a", "aac", "-b:a", "128k"] + venc
                    + ["-pix_fmt", "yuv420p", "-movflags", "+faststart", ins_out])

        # 先带插入片段原声试；失败(有的抖音片尾音频损坏)→ 全部用静音再试一次，保证画面一定接上
        done = False
        for _silent in (False, True):
            try:
                tr = subprocess.run(_ins_cmd(_silent), capture_output=True, timeout=timeout_s,
                                    creationflags=NOWIN)
                if tr.returncode == 0 and os.path.exists(ins_out) and os.path.getsize(ins_out) > 100000:
                    os.replace(ins_out, part)
                    done = True
                    break
            except Exception:
                pass
            if os.path.exists(ins_out):
                try:
                    os.remove(ins_out)
                except OSError:
                    pass
        if not done and on_status:
            on_status("⚠ 片尾/插入拼接失败，输出无插入成品")

    os.replace(part, out_path)               # 完整成功才落正式名(原子替换)
    return out_path


def dedup_batch(episode_paths, cfg, out_dir, per=3, count=0, prefix="去重",
                on_status=None, on_task=None, concurrency=2, skip_existing=False,
                variants=1, stop_flag=None):
    """把选中的集数**按合集(素材所在文件夹)分组**，每合集每 per 集去重合成一个成品，
    输出到 out_dir/合集名/。concurrency 条成品同时渲染(默认2)。返回成品路径列表。
    on_task(rec) 每条成品生命周期回调(running/done/failed+原因+用时)，供任务记录。
    skip_existing=True(重启续跑用)：输出已存在且>1MB 的成品直接算完成，不重渲染。"""
    import time as _time
    import threading
    from concurrent.futures import ThreadPoolExecutor

    vids = [p for p in episode_paths if p.lower().endswith(_VEXT)]
    # 【护栏】0字节/损坏的空壳输入(下载失败留下的)会让渲染空转到超时——直接剔除并说明
    bad = []
    good = []
    for p in vids:
        try:
            if os.path.getsize(p) > 10240:
                good.append(p)
            else:
                bad.append(p)
        except OSError:
            bad.append(p)
    if bad and on_status:
        on_status(f"⚠ 跳过 {len(bad)} 个空/损坏素材(0字节,下载失败的空壳): "
                  + ", ".join(os.path.basename(b) for b in bad[:5]))
    vids = good
    if not vids:
        raise RuntimeError("没有可用的集数视频(所选素材全是0字节空壳，请重新下载素材)")
    # 按合集(父文件夹)分组，组内按集数自然排序
    groups = {}
    for p in vids:
        groups.setdefault(os.path.dirname(p), []).append(p)

    tasks = []          # [(chunk, out, name, safe)]
    per = max(1, int(per))
    # 成品直接放进 out_dir（上层已是「时间 剧名」文件夹），不再套一层合集子目录 = 只一级。
    # 多部剧同批时，文件名前面带上剧名以免不同剧的成品重名互相覆盖。
    multi = len(groups) > 1
    os.makedirs(out_dir, exist_ok=True)
    for folder, pool in groups.items():
        pool = sorted(pool, key=_natkey)
        safe = re.sub(r'[\\/:*?"<>|]', "", os.path.basename(folder))[:40] or prefix
        chunks = [pool[i:i + per] for i in range(0, len(pool), per)]
        if count and count > 0:
            chunks = chunks[:count]
        # 补零位数：按本合集最大集号(第01集/第002集)，让成品文件名天然按集数升序
        pad = max(2, len(str(max([_ep_num_of(p) for p in pool] + [len(pool)]))))
        nv = max(1, int(variants or 1))
        for i, chunk in enumerate(chunks):
            base = _mix_name(chunk, f"{prefix}_{i+1}", i * per + 1, pad)
            # _mix_name 正常已带「剧名_第XX集」；只有它取不到集号退化成兜底名时，多剧同批才补剧名前缀防重名
            if multi and not base.startswith(safe):
                base = f"{safe}_{base}"
            for v in range(nv):
                # 变体：同一成品渲染N个不同随机参数的版本(多账号各发一个,指纹互不相同)
                name = base if v == 0 else f"{base}_变体{v+1}"
                tasks.append((chunk, os.path.join(out_dir, name + ".mp4"), name, safe, (v, nv)))

    total = len(tasks)
    made = []
    lock = threading.Lock()
    done_n = [0]

    def _run(task):
        chunk, out, name, safe, _variant = task

        def _fmt_dur(sec):
            sec = int(sec)
            return f"{sec//60}分{sec%60:02d}秒" if sec >= 60 else f"{sec}秒"

        def _emit(status, err="", size_mb=0, elapsed="", dur="", fp=None):
            if on_task:
                on_task({"name": name, "coll": safe, "eps": len(chunk),
                         "out": out, "status": status, "err": err,
                         "size_mb": size_mb, "elapsed": elapsed, "dur": dur, "fp": fp})
        # 【停止】用户点了停止 → 还没开始的这条直接标取消,不再渲染
        if stop_flag and stop_flag():
            _emit("cancelled", err="用户已停止")
            return
        # 重启续跑：已经渲染完的成品(>1MB)直接算完成，只补没完成的
        if skip_existing:
            try:
                if os.path.exists(out) and os.path.getsize(out) > 1024 * 1024:
                    _emit("done", size_mb=round(os.path.getsize(out) / 1048576, 1),
                          elapsed="已存在·跳过", dur=_fmt_dur(_probe_dur(out)))
                    with lock:
                        made.append(out)
                        done_n[0] += 1
                        if on_status:
                            on_status(f"渲染中 {done_n[0]}/{total}（并发 {concurrency}）")
                    return
                # 补零改名前旧成品(第1集.mp4)已渲染好 → 直接改名迁移成补零名，绝不重渲染(防重复文件)
                old = _depad_path(out)
                if old != out and os.path.exists(old) and os.path.getsize(old) > 1024 * 1024:
                    os.replace(old, out)
                    _emit("done", size_mb=round(os.path.getsize(out) / 1048576, 1),
                          elapsed="旧成品·改名", dur=_fmt_dur(_probe_dur(out)))
                    with lock:
                        made.append(out)
                        done_n[0] += 1
                        if on_status:
                            on_status(f"渲染中 {done_n[0]}/{total}（并发 {concurrency}）")
                    return
            except OSError:
                pass
        _emit("running")
        t0 = _time.time()
        try:
            pr = {}
            dedup_render(chunk, cfg, out, None, params_out=pr, variant=_variant)  # 并发时不串 on_status
            try:
                mb = round(os.path.getsize(out) / 1024 / 1024, 1)
            except OSError:
                mb = 0
            el = int(_time.time() - t0)
            # 【自检分】成品vs原片感知指纹差异%(越高=改得越狠);<8%=去重太弱
            fp = fp_diff_score(chunk[0], out, pr.get("spd", 1.0), pr.get("head", 0.0))
            _emit("done", size_mb=mb, dur=_fmt_dur(_probe_dur(out)), fp=fp,
                  elapsed=(f"{el//60}分{el%60}秒" if el >= 60 else f"{el}秒"))
            with lock:
                made.append(out)
        except Exception as e:
            # 用户停止时 ffmpeg 被杀→渲染报错,这条标"取消"而非"失败"
            if stop_flag and stop_flag():
                _emit("cancelled", err="用户已停止")
            else:
                _emit("failed", err=str(e)[:160])
        with lock:
            done_n[0] += 1
            if on_status:
                on_status(f"渲染中 {done_n[0]}/{total}（并发 {concurrency}）")

    concurrency = max(1, int(concurrency or 1))
    if concurrency == 1 or total <= 1:
        for t in tasks:
            _run(t)
    else:
        with ThreadPoolExecutor(max_workers=concurrency) as ex:
            list(ex.map(_run, tasks))
    if on_status:
        on_status(f"完成 {len(made)}/{total} 个")
    return made
