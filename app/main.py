"""LuboDown —— 程序入口。

窗口关闭时隐藏到托盘驻留（定时任务继续运行），
托盘菜单：显示主界面 / 立即检查新回放 / 退出。
"""
from __future__ import annotations

import ctypes
import os
import sys
import threading
import time
from pathlib import Path

import pystray
import webview
from PIL import Image, ImageDraw

from . import __version__
from .api import Api
from .bilibili import BiliClient
from .downloader import DownloadManager
from .scheduler import Scheduler
from .store import Store

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_kernel32.CreateMutexW.restype = ctypes.c_void_p
_kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
_kernel32.CloseHandle.restype = ctypes.c_bool
_kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
_mutex_handle = None


def acquire_single_instance() -> bool:
    """用 Windows 命名互斥体保证单实例：无文件残留，进程退出自动释放。"""
    global _mutex_handle
    handle = _kernel32.CreateMutexW(None, False, "LuboDown_SingleInstance_Mutex")
    if not handle:
        return False
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
        _kernel32.CloseHandle(handle)
        return False
    _mutex_handle = handle
    return True


def make_icon_image() -> Image.Image:
    """托盘图标：粉底圆角块 + 白色下载箭头 + 直播圆点。"""
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([2, 2, 62, 62], radius=16, fill=(251, 114, 153, 255))
    d.line([(32, 14), (32, 40)], fill="white", width=7)
    d.polygon([(18, 36), (46, 36), (32, 52)], fill="white")
    d.ellipse([42, 8, 56, 22], fill=(0, 174, 236, 255))
    return img


def build_app():
    store = Store()
    client = BiliClient(store)
    manager = DownloadManager(store, client)
    scheduler = Scheduler(store, client, manager)
    api = Api(store, client, manager, scheduler)
    return store, api


def run():
    if not acquire_single_instance():
        ctypes.windll.user32.MessageBoxW(
            0, "LuboDown 已经在运行（请查看系统托盘）。", "LuboDown", 0x40)
        return

    store, api = build_app()
    store.log(f"LuboDown v{__version__} 启动")

    if getattr(sys, "frozen", False):
        ui_base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    else:
        ui_base = Path(__file__).parent

    window = webview.create_window(
        f"LuboDown v{__version__} · B站直播回放下载",
        str(ui_base / "ui" / "index.html"),
        js_api=api,
        width=1240, height=820,
        min_size=(980, 620),
        background_color="#0f1115",
    )
    api.attach(window, webview)

    def on_closing(window):  # 参数名必须是 window，pywebview 才会注入窗口对象
        window.hide()
        store.log("窗口已最小化到托盘，任务继续运行")
        return False  # 阻止真正关闭

    window.events.closing += on_closing

    def show_window():
        window.show()
        try:
            window.restore()
        except Exception:
            pass

    def on_exit():
        store.log("退出 LuboDown")
        try:
            icon.stop()
        except Exception:
            pass
        try:
            window.destroy()
        except Exception:
            pass
        time.sleep(0.3)
        os._exit(0)

    icon = pystray.Icon(
        "LuboDown", make_icon_image(), "LuboDown · B站直播回放下载",
        menu=pystray.Menu(
            pystray.MenuItem("显示主界面", lambda *_: show_window(), default=True),
            pystray.MenuItem("立即检查新回放", lambda *_: scheduler.trigger_now()),
            pystray.MenuItem("退出", lambda *_: on_exit()),
        ))
    threading.Thread(target=icon.run, daemon=True).start()

    webview.start(debug=False)


if __name__ == "__main__":
    run()
