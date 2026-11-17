"""Backrank desktop app: the server plus a native window. Build with build.bat."""
import os
import sys
import threading
import time
import webbrowser

import server

PORT = int(os.environ.get("BACKRANK_PORT") or 8765)
TITLE = "Backrank"
MIN_SIZE = (900, 640)


class WindowApi:
    """Title-bar buttons, dragging and resizing for the frameless window (Windows).

    The page draws its own title bar, so it calls these through pywebview.
    Everything works in physical screen pixels straight on the window handle.
    """

    def __init__(self):
        import ctypes
        from ctypes import wintypes
        self._ct, self._wt, self._u32 = ctypes, wintypes, ctypes.windll.user32
        u = self._u32
        u.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        u.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND] + [ctypes.c_int] * 4 + [wintypes.UINT]
        u.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        u.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
        u.MonitorFromWindow.restype = ctypes.c_void_p
        u.GetMonitorInfoW.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        u.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        u.IsWindowVisible.argtypes = [wintypes.HWND]
        u.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        self._window = None
        self._hwnd = None
        self._lock = threading.Lock()
        self._drag, self._seq, self._edge, self._rect, self._restore = 0, 0, None, None, None

    # -- helpers (underscore names are not exposed to the page) -----------
    def _handle(self):
        if self._hwnd:
            return self._hwnd
        ct, wt, u, found = self._ct, self._wt, self._u32, []

        @ct.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
        def each(hwnd, _):
            pid, buf = wt.DWORD(), ct.create_unicode_buffer(64)
            u.GetWindowThreadProcessId(hwnd, ct.byref(pid))
            u.GetWindowTextW(hwnd, buf, 64)
            if pid.value == os.getpid() and u.IsWindowVisible(hwnd) and buf.value == TITLE:
                found.append(hwnd)
            return True

        u.EnumWindows(each, 0)
        self._hwnd = found[0] if found else None
        return self._hwnd

    def _get(self):
        rect = self._wt.RECT()
        self._u32.GetWindowRect(self._handle(), self._ct.byref(rect))
        return [rect.left, rect.top, rect.right, rect.bottom]

    def _set(self, left, top, right, bottom):
        self._u32.SetWindowPos(self._handle(), None, left, top, right - left, bottom - top, 0x0014)

    def _min_size(self):
        """The minimum window size in physical pixels (MIN_SIZE is in 96-dpi units)."""
        try:
            scale = self._u32.GetDpiForWindow(self._handle()) / 96
        except (AttributeError, OSError):
            scale = 1
        return int(MIN_SIZE[0] * (scale or 1)), int(MIN_SIZE[1] * (scale or 1))

    def _work_area(self):
        ct, wt = self._ct, self._wt

        class MonitorInfo(ct.Structure):
            _fields_ = [("cbSize", wt.DWORD), ("rcMonitor", wt.RECT), ("rcWork", wt.RECT), ("dwFlags", wt.DWORD)]

        info = MonitorInfo()
        info.cbSize = ct.sizeof(MonitorInfo)
        self._u32.GetMonitorInfoW(self._u32.MonitorFromWindow(self._handle(), 2), ct.byref(info))
        w = info.rcWork
        return [w.left, w.top, w.right, w.bottom]

    # -- called from the page ----------------------------------------------
    def minimize(self):
        if self._handle():
            self._u32.ShowWindow(self._handle(), 6)

    def toggle_max(self):
        """Fill the screen's work area (keeps the taskbar visible), or go back."""
        with self._lock:
            if not self._handle():
                return False
            if self._restore:
                self._set(*self._restore)
                self._restore = None
            else:
                self._restore = self._get()
                self._set(*self._work_area())
            return bool(self._restore)

    def close(self):
        if self._window:
            self._window.destroy()

    def grab(self, edge):
        """Start a drag: 'move', or the edge/corner being resized (n, s, e, w, ne...)."""
        with self._lock:
            self._drag += 1
            self._seq, self._edge, self._rect = 0, str(edge), self._get() if self._handle() else None
            return self._drag

    def track(self, drag, seq, dx, dy):
        """Apply the mouse offset since grab(). Late or out-of-order calls are dropped."""
        with self._lock:
            if drag != self._drag or seq <= self._seq or self._restore or not self._rect:
                return
            self._seq = seq
            left, top, right, bottom = self._rect
            dx, dy, edge = int(dx), int(dy), self._edge
            min_w, min_h = self._min_size()
            if edge == "move":
                left, right, top, bottom = left + dx, right + dx, top + dy, bottom + dy
            else:
                if "e" in edge:
                    right = max(left + min_w, right + dx)
                if "w" in edge:
                    left = min(right - min_w, left + dx)
                if "s" in edge:
                    bottom = max(top + min_h, bottom + dy)
                if "n" in edge:
                    top = min(bottom - min_h, top + dy)
            self._set(left, top, right, bottom)


def main():
    try:
        httpd = server.serve(PORT)
    except OSError:
        # Port busy: most likely Backrank is already running. Show that copy.
        httpd = None
    url = f"http://127.0.0.1:{PORT}"
    try:
        if os.environ.get("BACKRANK_NO_WINDOW"):
            raise ImportError
        import webview
    except ImportError:
        # No pywebview: fall back to the default browser and keep serving.
        if httpd is None:
            webbrowser.open(url)
            return
        if not os.environ.get("BACKRANK_NO_WINDOW"):
            webbrowser.open(url)
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            return
    # On Windows the page draws its own dark title bar, so the native frame is dropped.
    api = WindowApi() if os.name == "nt" else None
    window = webview.create_window(TITLE, url, width=1400, height=940, min_size=MIN_SIZE,
                                   background_color="#191b1f", frameless=api is not None,
                                   easy_drag=False, js_api=api)
    if api:
        api._window = window
    icon = server.RES / "icon.ico"
    webview.start(icon=str(icon) if icon.is_file() else None)   # blocks until the window is closed
    if httpd:
        httpd.shutdown()
    server.close_engines()
    sys.exit(0)


if __name__ == "__main__":
    main()
