# -*- coding: utf-8 -*-
"""无窗口后端入口：给 Electron 外壳用（界面窗口由 Electron 提供，这里只跑 FastAPI）。
打包成 backend.exe，由 electron/main.js spawn 起来。逻辑=desktop.py 里跑后端那部分，去掉 pywebview 窗口。"""
import os
import socket
import sys
import time

# 【关键·视频号发布靠它】打包后让 Playwright 用打进 exe 里的自带 Chromium。
# 不设 → channels.py 的 _launch_persistent 找不到浏览器 → 视频号拉剧集/发布直接崩。
# 与 desktop.py 完全一致，保证发布行为不变。
if getattr(sys, "frozen", False):
    _bp = os.path.join(getattr(sys, "_MEIPASS", os.path.dirname(sys.executable)), "ms-playwright")
    if os.path.isdir(_bp):
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = _bp

import uvicorn

from app import app, PORT


def _wait_port_free(host: str = "127.0.0.1", port: int = PORT, timeout: int = 25) -> bool:
    """自动更新重启时，旧进程可能还占着端口——等它释放再起，避免端口冲突起不来。"""
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


if __name__ == "__main__":
    _wait_port_free()
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")
