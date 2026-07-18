# -*- mode: python ; coding: utf-8 -*-
# 无窗口后端打包（给 Electron 外壳用）：pyinstaller backend.spec
# 产出 onedir：dist/backend/backend.exe + _internal/（含自带 Chromium）。
# electron-builder 把整个 dist/backend/ 作为 extraResources 收进 Electron 应用，main.js 启动它。
# 内容与 build.spec 完全一致（同一套后端 + 同一个 Chromium），只是入口换成 backend.py、且打成文件夹。
from PyInstaller.utils.hooks import collect_all, collect_submodules

datas = [('static', 'static'), ('version.json', '.')]
binaries = []
# channels_upload=纯后端视频号上传/发布、ytdl=链接下载引擎(yt-dlp)——都在 app.py 里条件 import,
# PyInstaller 静态分析抓不到，必须显式列，否则打包版缺这些功能。
hiddenimports = ['app', 'channels', 'channels_upload', 'ytdl']

# imageio_ffmpeg：channels_upload/ffdedup/ytdl 截封面·探尺寸·去重合成·合并音视频都靠它自带的 ffmpeg.exe，collect_all 才会把二进制收进包
# yt_dlp：链接下载引擎,几百个站点提取器是动态 import,必须 collect_all 把子模块全收进来,否则打包版下不了
for pkg in ('f2', 'webview', 'uvicorn', 'winotify', 'browser_cookie3', 'playwright', 'imageio_ffmpeg', 'yt_dlp'):
    try:
        d, b, h = collect_all(pkg)
        datas += d
        binaries += b
        hiddenimports += h
    except Exception:
        pass
hiddenimports += collect_submodules('uvicorn')
hiddenimports += ['jsonpath_ng', 'm3u8', 'pyexecjs', 'execjs', 'gmssl', 'greenlet', 'pyee']
hiddenimports += ['psutil', 'psutil._psutil_windows']   # 清理残留档案浏览器要用
hiddenimports += ['pythoncom', 'pywintypes', 'win32com', 'win32com.client', 'win32api']

# 【关键·视频号发布要用自带 Chromium】把 Playwright 当前锁定的 chromium-* 目录收进包，
# 运行时 backend.py 设 PLAYWRIGHT_BROWSERS_PATH 指向它。和 build.spec 同一做法。
import os as _os
def _collect_chromium():
    out = []
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as _p:
            exe_path = _p.chromium.executable_path
        rev_dir = exe_path
        for _ in range(2):
            rev_dir = _os.path.dirname(rev_dir)
        rev_name = _os.path.basename(rev_dir)
        for root, _dirs, files in _os.walk(rev_dir):
            # 剔除运行时污染：视频号 Playwright 曾把账号档案写进 Chromium 安装目录
            # (chrome-win64\data\channels\profiles\<aid>\…)。这些既是隐私(账号 cookie 绝不能进分发包)、
            # 又常被占用导致 PermissionError 打包中断。标准 Chromium 没有 data 目录，直接整枝剪掉。
            _dirs[:] = [d for d in _dirs if d.lower() != 'data']
            for f in files:
                full = _os.path.join(root, f)
                rel = _os.path.relpath(full, rev_dir)
                out.append((full, _os.path.join('ms-playwright', rev_name, _os.path.dirname(rel))))
        print(f"[build] 打包 Chromium: {rev_name} ({len(out)} 个文件)")
    except Exception as _e:
        print(f"[build] ⚠ 收集 Chromium 失败(视频号发布会崩): {_e}")
    return out
datas += _collect_chromium()

a = Analysis(
    ['backend.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=['tkinter', 'matplotlib', 'PyQt5', 'PySide2'],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,          # onedir：二进制/数据交给 COLLECT，不塞进单个 exe
    name='backend',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,                  # 无黑窗（Electron 也会 windowsHide）
    disable_windowed_traceback=False,
    argv_emulation=False,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='backend',                 # 产出 dist/backend/
)
