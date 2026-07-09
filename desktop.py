# -*- coding: utf-8 -*-
"""桌面软件入口：后台跑 FastAPI，前台开原生窗口（造梦工坊风格 UI）"""
import base64
import glob
import os
import subprocess
import sys
import threading
import time
import socket

import uvicorn
import webview

import app as appmod
from app import app, PORT

_DETACHED = 0x00000008
_NO_WINDOW = 0x08000000


def _cleanup_leftovers():
    """启动时清理自动更新残留：*_old.exe / *_new.exe / *.part"""
    if not getattr(sys, "frozen", False):
        return
    folder = os.path.dirname(sys.executable)
    for pat in ("*_old.exe", "*_new.exe", "*.part"):
        for f in glob.glob(os.path.join(folder, pat)):
            try:
                os.remove(f)
            except Exception:
                pass


def _ensure_desktop_shortcut():
    """确保桌面有个「爆款监控」快捷方式指向当前 exe（每次启动刷新，更新后依然可用）"""
    if not getattr(sys, "frozen", False):
        return
    try:
        exe = sys.executable
        workdir = os.path.dirname(exe)
        ps = (
            "$W=New-Object -ComObject WScript.Shell\n"
            "$p=[System.IO.Path]::Combine([Environment]::GetFolderPath('Desktop'),'爆款监控.lnk')\n"
            "$s=$W.CreateShortcut($p)\n"
            f'$s.TargetPath="{exe}"\n'
            f'$s.WorkingDirectory="{workdir}"\n'
            "$s.Save()\n"
        )
        enc = base64.b64encode(ps.encode("utf-16-le")).decode()
        subprocess.Popen(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-WindowStyle", "Hidden", "-EncodedCommand", enc],
            creationflags=_DETACHED | _NO_WINDOW)
    except Exception:
        pass


def _port_ready(host="127.0.0.1", port=PORT, timeout=15):
    end = time.time() + timeout
    while time.time() < end:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            if s.connect_ex((host, port)) == 0:
                return True
        time.sleep(0.2)
    return False


def _wait_port_free(host="127.0.0.1", port=PORT, timeout=25):
    """启动时等端口释放——自动更新重启时，旧进程可能还占着端口"""
    end = time.time() + timeout
    while time.time() < end:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.bind((host, port))
            s.close()
            return True
        except OSError:
            try:
                s.close()
            except Exception:
                pass
            time.sleep(0.4)
    return False


def _serve():
    _wait_port_free()
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")


def _extract_cookies(cookies) -> dict:
    """把 pywebview get_cookies() 的返回统一成 {name: value}"""
    jar = {}
    for c in cookies or []:
        if hasattr(c, "name") and hasattr(c, "value"):        # http.cookiejar.Cookie
            jar[c.name] = c.value
        elif hasattr(c, "items"):                              # SimpleCookie / dict
            for k, m in c.items():
                jar[k] = getattr(m, "value", m)
    return jar


class JsApi:
    """暴露给前端 window.pywebview.api 调用（跑在 pywebview 桥线程，可安全操作窗口）"""

    def login_qr(self):
        try:
            win = webview.create_window(
                "登录抖音 · 用手机抖音扫码或账号登录",
                "https://www.douyin.com/",
                width=1040, height=760,
            )
        except Exception as e:
            return {"ok": False, "error": f"打开登录窗口失败: {e}"}

        found = None
        for _ in range(180):  # 最多等 3 分钟
            time.sleep(1)
            try:
                jar = _extract_cookies(win.get_cookies())
            except Exception:
                continue
            if "sessionid" in jar:
                found = "; ".join(f"{k}={v}" for k, v in jar.items())
                break
        try:
            win.destroy()
        except Exception:
            pass

        if found:
            appmod.set_login_cookie(found)
            return {"ok": True}
        return {"ok": False, "error": "超时未检测到登录（3分钟内没扫码或没登录成功）"}


if __name__ == "__main__":
    _cleanup_leftovers()          # 清掉上次更新残留的旧包
    _ensure_desktop_shortcut()    # 保证桌面快捷方式存在且指向当前 exe
    threading.Thread(target=_serve, daemon=True).start()
    _port_ready()
    jsapi = JsApi()
    # 给 /api/login_qr 兜底用（前端优先直接调 pywebview.api）
    appmod._login_callback = lambda: jsapi.login_qr().get("ok")
    webview.create_window(
        "爆款监控 · 抖音博主更新雷达",
        f"http://127.0.0.1:{PORT}/",
        width=1200,
        height=780,
        min_size=(940, 620),
        background_color="#12141c",
        js_api=jsapi,
    )
    webview.start()
