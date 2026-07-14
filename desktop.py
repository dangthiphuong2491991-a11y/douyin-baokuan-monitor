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

# 【关键·必须在 import app/channels(→playwright) 之前设置】打包后让 Playwright 用打进 exe 里的
# 自带 Chromium（build.spec 已把它收进 ms-playwright/）。视频号发布引擎启动这个 chromium，
# 不设的话 executable_path 指向系统缺失路径→用户端一发布就崩。
if getattr(sys, "frozen", False):
    _bp = os.path.join(getattr(sys, "_MEIPASS", os.path.dirname(sys.executable)), "ms-playwright")
    if os.path.isdir(_bp):
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = _bp

import uvicorn
import webview

import app as appmod
from app import app, PORT

_DETACHED = 0x00000008
_NO_WINDOW = 0x08000000


def _cleanup_leftovers():
    """启动时清理自动更新残留：*_old.exe / *_new.exe / *.part。
    【根治「更新后目录里留着两个 exe」】刚更新完时，旧进程还没退干净、_old.exe 还被锁着，
    立刻删会失败。这里改成后台线程反复重试删 ~40 秒——等旧进程一退出、锁一释放就删掉。"""
    if not getattr(sys, "frozen", False):
        return
    folder = os.path.dirname(sys.executable)

    def _sweep():
        for _ in range(20):                      # 最多重试 ~40 秒
            leftover = False
            for pat in ("*_old.exe", "*_new.exe", "*.part"):
                for f in glob.glob(os.path.join(folder, pat)):
                    try:
                        os.remove(f)
                    except Exception:
                        leftover = True          # 还被锁 → 待会再试
            if not leftover:
                return
            time.sleep(2)

    threading.Thread(target=_sweep, daemon=True).start()


def _ensure_desktop_shortcut():
    """只在【第一次运行】给桌面建一个「爆款监控」快捷方式；之后绝不重建/覆盖，
    尊重用户自己管理的图标（删了不会自己长回来，你自己的入口也不碰）。"""
    if not getattr(sys, "frozen", False):
        return
    try:
        marker = os.path.join(os.path.dirname(sys.executable), ".shortcut_done")
        import pythoncom
        from win32com.client import Dispatch
        pythoncom.CoInitialize()
        shell = Dispatch("WScript.Shell")
        desktop = shell.SpecialFolders("Desktop")   # 兼容 OneDrive 重定向的桌面
        lnk = os.path.join(desktop, "爆款监控.lnk")
        # 建过一次 / 桌面已存在同名快捷方式 → 一律不动
        if os.path.exists(marker) or os.path.exists(lnk):
            try:
                open(marker, "w").close()
            except Exception:
                pass
            return
        sc = shell.CreateShortCut(lnk)
        sc.Targetpath = sys.executable
        sc.WorkingDirectory = os.path.dirname(sys.executable)
        sc.IconLocation = sys.executable
        sc.save()
        try:
            open(marker, "w").close()
        except Exception:
            pass
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


_PLATFORM_LOGIN = {
    "douyin": ("登录抖音 · 用手机抖音扫码或账号登录", "https://www.douyin.com/"),
    "tiktok": ("Login TikTok · 需已连 VPN，用账号或扫码登录", "https://www.tiktok.com/login"),
}


class JsApi:
    """暴露给前端 window.pywebview.api 调用（跑在 pywebview 桥线程，可安全操作窗口）"""

    def login_qr(self, platform="douyin"):
        title, url = _PLATFORM_LOGIN.get(platform, _PLATFORM_LOGIN["douyin"])
        try:
            win = webview.create_window(title, url, width=1040, height=760)
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
            appmod.set_login_cookie(found, platform)
            return {"ok": True}
        return {"ok": False, "error": "超时未检测到登录（3分钟内没扫码或没登录成功）"}


if __name__ == "__main__":
    _cleanup_leftovers()          # 清掉上次更新残留的旧包
    _ensure_desktop_shortcut()    # 保证桌面快捷方式存在且指向当前 exe
    threading.Thread(target=_serve, daemon=True).start()
    _port_ready()
    jsapi = JsApi()
    # 给 /api/login_qr 兜底用（前端优先直接调 pywebview.api）
    appmod._login_callback = lambda platform="douyin": jsapi.login_qr(platform).get("ok")
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
