# -*- mode: python ; coding: utf-8 -*-
# 打包命令：pyinstaller build.spec
from PyInstaller.utils.hooks import collect_all, collect_submodules

datas = [('static', 'static')]
binaries = []
hiddenimports = ['app']

# f2 / pywebview / uvicorn 等需要连数据文件一起收集
for pkg in ('f2', 'webview', 'uvicorn', 'winotify', 'browser_cookie3'):
    try:
        d, b, h = collect_all(pkg)
        datas += d
        binaries += b
        hiddenimports += h
    except Exception:
        pass
hiddenimports += collect_submodules('uvicorn')
hiddenimports += ['jsonpath_ng', 'm3u8', 'pyexecjs', 'execjs', 'gmssl']
# 建桌面快捷方式用（pywin32）
hiddenimports += ['pythoncom', 'pywintypes', 'win32com', 'win32com.client', 'win32api']

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
