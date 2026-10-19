"""LuboDown —— 程序入口。

窗口关闭时隐藏到托盘驻留（定时任务继续运行），
托盘菜单：显示主界面 / 立即检查新回放 / 退出。
"""
from __future__ import annotations

import ctypes
import os
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path

import pystray
import webview
from PIL import Image, ImageDraw

from . import __version__
from .api import Api
from .bilibili import BiliClient
from .downloader import DownloadManager
from .scheduler import Scheduler
from .store import LOG_FILE, SETTING_SPECS, Store, clamp_setting
from .window_frame import WindowFrame

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


def log_crash(where: str, exc: BaseException) -> None:
    """未捕获异常写入 lubo.log：pythonw 下没有控制台，否则崩溃不留任何痕迹。"""
    text = "".join(traceback.format_exception(exc))
    try:
        LOG_FILE.parent.mkdir(exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%m-%d %H:%M:%S')}] [error] {where}未捕获异常：\n{text}")
    except OSError:
        pass


def _thread_excepthook(args) -> None:
    if args.exc_type is SystemExit:
        return
    log_crash(f"线程 {args.thread.name if args.thread else '?'} ", args.exc_value)


def build_app():
    store = Store()
    client = BiliClient(store)
    manager = DownloadManager(store, client)
    scheduler = Scheduler(store, client, manager)
    api = Api(store, client, manager, scheduler)
    return store, manager, scheduler, api


def run():
    threading.excepthook = _thread_excepthook
    try:
        _run()
    except Exception as e:
        log_crash("主线程", e)
        ctypes.windll.user32.MessageBoxW(
            0, f"LuboDown 运行出错：{e}\n\n详细信息见 config/lubo.log", "LuboDown", 0x10)
        raise


def _run():
    if not acquire_single_instance():
        ctypes.windll.user32.MessageBoxW(
            0, "LuboDown 已经在运行（请查看系统托盘）。", "LuboDown", 0x40)
        return

    store, manager, scheduler, api = build_app()
    store.log(f"LuboDown v{__version__} 启动")

    if getattr(sys, "frozen", False):
        ui_base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    else:
        ui_base = Path(__file__).parent

    # 无边框圆角窗口：标题栏与窗口按钮由前端绘制，拖动/缩放/圆角见 WindowFrame
    # 窗口大小固定（不能拖边缘或最大化），尺寸在设置中修改；下限取设置区间的下界
    min_size = (SETTING_SPECS["window_width"][1], SETTING_SPECS["window_height"][1])
    native_corners = WindowFrame.native_corners()
    try:
        width = clamp_setting("window_width", store.settings.get("window_width"))
        height = clamp_setting("window_height", store.settings.get("window_height"))
    except (TypeError, ValueError):
        width, height = 1240, 820
    window = webview.create_window(
        f"LuboDown v{__version__} · B站直播回放下载",
        str(ui_base / "ui" / "index.html"),
        js_api=api,
        width=width, height=height,
        min_size=min_size, resizable=False,
        frameless=True, easy_drag=False,
        # 原生圆角依赖 DWM 绘制窗口框架（即 shadow）；区域裁剪方式下 DWM 框架会把窗口画成方形，须关闭
        shadow=native_corners,
        background_color="#2B3135" if WindowFrame.system_dark() else "#FAFAFB",   # 与 tokens.css 的 --bg 一致
    )

    def hide_to_tray():
        window.hide()
        store.log("窗口已最小化到托盘，任务继续运行")

    frame = WindowFrame(window, min_size, hide_to_tray, resizable=False)
    api.attach(window, webview, frame)
    window.events.before_show += frame.attach

    exiting = threading.Event()

    def on_closing(window):  # 参数名必须是 window，pywebview 才会注入窗口对象
        if exiting.is_set():   # 托盘退出时 destroy 同样触发本事件，放行且不记"最小化到托盘"
            return True
        hide_to_tray()
        return False  # 阻止真正关闭

    window.events.closing += on_closing

    def show_window():
        window.show()
        try:
            window.restore()
        except Exception:
            pass

    def on_exit():
        exiting.set()
        store.log("退出 LuboDown")
        manager.shutdown()
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

    # 固定 WebView2 数据目录：避免 pywebview 隐私模式默认行为
    # （每次启动 tempfile.mkdtemp 新建目录，进程被强杀时残留泄漏）。
    # storage_path 与 private_mode=True 可共存：隐私开关独立于目录（IsInPrivateModeEnabled）。
    webview_data_dir = Path(tempfile.gettempdir()) / "LuboDown_WebView2"
    webview.start(debug=False, storage_path=str(webview_data_dir))


if __name__ == "__main__":
    run()
