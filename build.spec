# -*- mode: python ; coding: utf-8 -*-
# 打包命令：pyinstaller build.spec
from PyInstaller.utils.hooks import collect_all, collect_submodules

datas = [('static', 'static')]
binaries = []
hiddenimports = ['app', 'channels']

# f2 / pywebview / uvicorn / playwright 等需要连数据文件一起收集
# 注意：playwright 走系统 Edge/Chrome（channel=msedge），只需打包它的 node 驱动，不含 Chromium。
for pkg in ('f2', 'webview', 'uvicorn', 'winotify', 'browser_cookie3', 'playwright'):
    try:
        d, b, h = collect_all(pkg)
        datas += d
        binaries += b
        hiddenimports += h
    except Exception:
        pass
hiddenimports += collect_submodules('uvicorn')
hiddenimports += ['jsonpath_ng', 'm3u8', 'pyexecjs', 'execjs', 'gmssl', 'greenlet', 'pyee']
# 建桌面快捷方式用（pywin32）
hiddenimports += ['pythoncom', 'pywintypes', 'win32com', 'win32com.client', 'win32api']

# 【关键·视频号发布要用自带 Chromium】新版发布引擎自己启动 bundled Chromium + CDP 连接
# （仿小V猫，不被风控检测）。channels.py 用 p.chromium.executable_path 找它——必须把 Playwright
# 的 chromium 浏览器一起打进 exe，否则用户端一发布就崩（找不到浏览器）。
# 把 ms-playwright 下 Playwright 当前锁定的那个 chromium-* 目录收进包，运行时 desktop.py 设
# PLAYWRIGHT_BROWSERS_PATH 指向它。
import os as _os, glob as _glob
def _collect_chromium():
    out = []
    try:
        # Playwright 包锁定的 chromium 版本目录（executable_path 指向的那一个）
        from playwright.sync_api import sync_playwright
        with sync_playwright() as _p:
            exe_path = _p.chromium.executable_path      # ...\ms-playwright\chromium-XXXX\chrome-win\chrome.exe
        rev_dir = exe_path
        for _ in range(2):                              # 上跳两级到 chromium-XXXX
            rev_dir = _os.path.dirname(rev_dir)
        rev_name = _os.path.basename(rev_dir)           # chromium-1140
        for root, _dirs, files in _os.walk(rev_dir):
            for f in files:
                full = _os.path.join(root, f)
                rel = _os.path.relpath(full, rev_dir)
                out.append((full, _os.path.join('ms-playwright', rev_name, _os.path.dirname(rel))))
        print(f"[build] 打包 Chromium: {rev_name} ({len(out)} 个文件)")
    except Exception as _e:
        print(f"[build] ⚠ 收集 Chromium 失败(用户端发布会崩): {_e}")
    return out
datas += _collect_chromium()

a = Analysis(
    ['desktop.py'],
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
    a.binaries,
    a.datas,
    [],
    name='爆款监控',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    runtime_tmpdir=None,
    console=False,           # 无黑窗
    disable_windowed_traceback=False,
    argv_emulation=False,
    icon='app.ico',
)
