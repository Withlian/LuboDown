"""LuboDown —— 无边框圆角窗口的原生行为。

隐藏系统标题栏后，窗口按钮由前端绘制；这里用 Win32 API 补回拖动、边缘缩放、
最大化/还原（不遮挡任务栏），并让窗口呈圆角：

- Win11（非远程桌面）：请求 DWM 原生圆角，边缘抗锯齿并带系统阴影与细边框。
  依赖 DWM 绘制窗口框架，创建窗口时须 shadow=True（见 native_corners）。
- 其他环境（Win10、远程桌面——系统在远程会话中关闭圆角）：用窗口区域裁成圆角矩形。
  区域只能按整像素裁剪，无抗锯齿与阴影，深色背景下角外会透出背后内容。

不用颜色键透明：WebView2 内容不在窗体的分层表面里，整个窗口都会被判定为透明而点击穿透；
窗口区域则同时裁剪显示与命中测试，圆角外侧穿透、内侧正常响应。
"""
from __future__ import annotations

import ctypes
import sys
import threading
import time
import winreg
from ctypes import wintypes


class _MonitorInfo(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]


_user32 = ctypes.WinDLL("user32", use_last_error=True)
_gdi32 = ctypes.WinDLL("gdi32")
_user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                 ctypes.c_int, ctypes.c_int, ctypes.c_uint]
_user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
_user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
_user32.MonitorFromWindow.restype = wintypes.HMONITOR
_user32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
_user32.GetMonitorInfoW.argtypes = [wintypes.HMONITOR, ctypes.POINTER(_MonitorInfo)]
_user32.GetDpiForWindow.argtypes = [wintypes.HWND]
_user32.IsIconic.argtypes = [wintypes.HWND]
_user32.SetWindowRgn.argtypes = [wintypes.HWND, wintypes.HRGN, wintypes.BOOL]
_gdi32.CreateRoundRectRgn.restype = wintypes.HRGN
_gdi32.CreateRoundRectRgn.argtypes = [ctypes.c_int] * 6

SWP_NOZORDER, SWP_NOACTIVATE = 0x4, 0x10
MONITOR_DEFAULTTONEAREST = 2
SM_SWAPBUTTON, SM_CXDRAG, SM_CYDRAG, SM_REMOTESESSION = 23, 68, 69, 0x1000
DWMWA_NCRENDERING_POLICY, DWMNCRP_DISABLED = 2, 1
DWMWA_WINDOW_CORNER_PREFERENCE, DWMWCP_DONOTROUND, DWMWCP_ROUND = 33, 1, 2


class WindowFrame:
    """前端窗口按钮背后的原生实现；公开方法可在任意线程调用。"""

    TRACK_HZ = 120
    RADIUS = 20          # 与 base.css 中 .shell 的圆角一致（逻辑像素）

    def __init__(self, window, min_size: tuple[int, int], on_close, resizable: bool = True):
        self._window = window
        self._min_size = min_size
        self._on_close = on_close
        self._resizable = resizable  # False：固定大小，不能拖边缘缩放或最大化，尺寸只经 set_size 修改
        self._hwnd = None
        self._maximized = False
        self._normal = None          # 最大化前的窗口矩形
        self._region_size = None     # 上次裁剪时的窗口尺寸，尺寸不变不重复设置
        self._corner_pref = None     # 上次设置的 DWM 圆角偏好
        self._native = self.native_corners()
        self._tracking = threading.Lock()   # 同一时刻只跟随一次拖动/缩放

    @staticmethod
    def native_corners() -> bool:
        """能否用 DWM 原生圆角：Win11（build 22000+）且不在远程桌面会话中。"""
        return sys.getwindowsversion().build >= 22000 and not _user32.GetSystemMetrics(SM_REMOTESESSION)

    @staticmethod
    def system_dark() -> bool:
        """系统是否为深色应用模式：用于窗口底色，避免页面加载前闪一下反色。"""
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as k:
                return winreg.QueryValueEx(k, "AppsUseLightTheme")[0] == 0
        except OSError:
            return False

    # ============ 初始化 ============
    def attach(self) -> None:
        """窗体创建后、显示前调用（before_show 事件）：设置圆角，区域裁剪方式下尺寸变化时重裁。"""
        if self._hwnd:
            return
        self._hwnd = wintypes.HWND(int(self._window.native.Handle.ToInt64()))
        if not self._native:
            # 关闭 DWM 非客户区绘制：开启时 DWM 按方形窗口矩形渲染，窗口区域只裁剪点击不裁剪画面
            self._dwm_set(DWMWA_NCRENDERING_POLICY, DWMNCRP_DISABLED)
            self._window.events.resized += self._update_shape
        self._fit_work_area()
        self._update_shape()

    def state(self) -> dict:
        return {"maximized": self._maximized, "native_corners": self._native, "resizable": self._resizable}

    def set_size(self, width: int, height: int) -> tuple[int, int]:
        """按逻辑像素设置窗口大小并保持中心不动；超出屏幕工作区时缩到工作区内。返回实际逻辑尺寸。"""
        if not self._hwnd or self._maximized:
            return width, height
        scale = self._scale()
        l, t, r, b = self._rect()
        wl, wt, wr, wb = self._work_area()
        w = min(int(width * scale), wr - wl)
        h = min(int(height * scale), wb - wt)
        x = min(max(wl, (l + r - w) // 2), wr - w)
        y = min(max(wt, (t + b - h) // 2), wb - h)
        self._move_to(x, y, x + w, y + h)
        self._update_shape()
        return round(w / scale), round(h / scale)

    def _fit_work_area(self) -> None:
        """启动时窗口大于屏幕工作区（小屏或高缩放）则缩小并居中：固定大小的窗口用户无法自行拖小。"""
        l, t, r, b = self._rect()
        wl, wt, wr, wb = self._work_area()
        w, h = min(r - l, wr - wl), min(b - t, wb - wt)
        if (w, h) != (r - l, b - t):
            x, y = wl + (wr - wl - w) // 2, wt + (wb - wt - h) // 2
            self._move_to(x, y, x + w, y + h)

    # ============ 窗口按钮 ============
    def minimize(self) -> None:
        self._window.minimize()

    def toggle_maximize(self) -> dict:
        if not self._hwnd or not self._resizable:
            return self.state()
        if self._maximized:
            self._maximized = False
            self._move_to(*self._normal)
        else:
            self._normal = self._rect()
            self._maximized = True
            self._move_to(*self._work_area())
        self._update_shape()
        return self.state()

    def close(self) -> None:
        self._on_close()

    # ============ 拖动与缩放 ============
    # 按住鼠标期间轮询光标并 SetWindowPos 跟随：不依赖系统移动循环，
    # 由前端 mousedown 经 JS API（后台线程）触发也能稳定工作。
    def drag(self) -> dict:
        """按住标题区拖动；最大化时移动超过系统拖动阈值才还原（单击、双击不还原）。"""
        if self._hwnd and self._button_down():
            self._track("")
        return self.state()

    def resize(self, edge: str) -> None:
        """按住边缘缩放；edge 为 n/s/e/w 及其组合（如 "se"）。"""
        if self._hwnd and self._resizable and not self._maximized and self._button_down():
            self._track(edge)

    def _track(self, edge: str) -> None:
        """跟随光标移动（edge 为空）或缩放窗口，直到松开鼠标。"""
        if not self._tracking.acquire(blocking=False):
            return
        try:
            if not edge and self._maximized:
                if not self._wait_drag_threshold():
                    return
                self._restore_under_cursor()
            l, t, r, b = self._rect()
            sx, sy = self._cursor()
            scale = self._scale()
            min_w, min_h = (int(v * scale) for v in self._min_size)
            last = None
            while self._button_down():
                x, y = self._cursor()
                dx, dy = x - sx, y - sy
                if not edge:
                    rect = (l + dx, t + dy, r + dx, b + dy)
                else:
                    rect = (
                        min(l + dx, r - min_w) if "w" in edge else l,
                        min(t + dy, b - min_h) if "n" in edge else t,
                        max(r + dx, l + min_w) if "e" in edge else r,
                        max(b + dy, t + min_h) if "s" in edge else b,
                    )
                if rect != last:
                    self._move_to(*rect)
                    if edge:
                        self._update_shape()
                    last = rect
                time.sleep(1 / self.TRACK_HZ)
        finally:
            self._tracking.release()

    def _wait_drag_threshold(self) -> bool:
        """按住期间光标移出系统拖动阈值返回 True；未移动就松开返回 False。"""
        sx, sy = self._cursor()
        tx, ty = _user32.GetSystemMetrics(SM_CXDRAG), _user32.GetSystemMetrics(SM_CYDRAG)
        while self._button_down():
            x, y = self._cursor()
            if abs(x - sx) >= tx or abs(y - sy) >= ty:
                return True
            time.sleep(1 / self.TRACK_HZ)
        return False

    def _restore_under_cursor(self) -> None:
        """从最大化还原到原尺寸，并保持光标在标题区的相对横向位置。"""
        x, _ = self._cursor()
        l, t, r, _ = self._rect()
        nl, nt, nr, nb = self._normal
        w, h = nr - nl, nb - nt
        left = int(x - w * (x - l) / max(1, r - l))
        self._maximized = False
        self._move_to(left, t, left + w, t + h)
        self._update_shape()

    # ============ Win32 辅助 ============
    def _update_shape(self) -> None:
        """按当前状态更新圆角：原生方式最大化时取消圆角，区域方式按尺寸重裁。"""
        if not self._hwnd:
            return
        if not self._native:
            self._update_region()
            return
        pref = DWMWCP_DONOTROUND if self._maximized else DWMWCP_ROUND
        if pref != self._corner_pref:
            self._corner_pref = pref
            self._dwm_set(DWMWA_WINDOW_CORNER_PREFERENCE, pref)

    def _dwm_set(self, attr: int, value: int) -> None:
        v = ctypes.c_int(value)
        try:
            ctypes.windll.dwmapi.DwmSetWindowAttribute(self._hwnd, attr, ctypes.byref(v), ctypes.sizeof(v))
        except OSError:
            pass

    def _update_region(self) -> None:
        """按当前尺寸裁出圆角；最大化时取消裁剪贴满工作区。"""
        if not self._hwnd or _user32.IsIconic(self._hwnd):
            return
        l, t, r, b = self._rect()
        size = (r - l, b - t, self._maximized)
        if size == self._region_size:
            return
        self._region_size = size
        if self._maximized:
            _user32.SetWindowRgn(self._hwnd, None, True)
            return
        d = round(self.RADIUS * 2 * self._scale())
        rgn = _gdi32.CreateRoundRectRgn(0, 0, r - l + 1, b - t + 1, d, d)
        _user32.SetWindowRgn(self._hwnd, rgn, True)   # 成功后区域归系统所有，无需释放

    def _scale(self) -> float:
        return (_user32.GetDpiForWindow(self._hwnd) or 96) / 96

    def _rect(self) -> tuple[int, int, int, int]:
        rc = wintypes.RECT()
        _user32.GetWindowRect(self._hwnd, ctypes.byref(rc))
        return rc.left, rc.top, rc.right, rc.bottom

    def _work_area(self) -> tuple[int, int, int, int]:
        info = _MonitorInfo(cbSize=ctypes.sizeof(_MonitorInfo))
        mon = _user32.MonitorFromWindow(self._hwnd, MONITOR_DEFAULTTONEAREST)
        _user32.GetMonitorInfoW(mon, ctypes.byref(info))
        rc = info.rcWork
        return rc.left, rc.top, rc.right, rc.bottom

    def _move_to(self, l: int, t: int, r: int, b: int) -> None:
        _user32.SetWindowPos(self._hwnd, None, l, t, r - l, b - t, SWP_NOZORDER | SWP_NOACTIVATE)

    @staticmethod
    def _cursor() -> tuple[int, int]:
        pt = wintypes.POINT()
        _user32.GetCursorPos(ctypes.byref(pt))
        return pt.x, pt.y

    @staticmethod
    def _button_down() -> bool:
        vk = 0x02 if _user32.GetSystemMetrics(SM_SWAPBUTTON) else 0x01   # 左右键互换时主键为 VK_RBUTTON
        return bool(_user32.GetAsyncKeyState(vk) & 0x8000)
