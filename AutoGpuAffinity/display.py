"""Use physical desktop pixels and enforce borderless fullscreen by process ID."""
import ctypes
from ctypes import wintypes
import time


def primary_resolution():
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    try:
        user32.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except AttributeError:
        user32.SetProcessDPIAware()
    width, height = user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)
    if width <= 0 or height <= 0:
        raise RuntimeError("Could not read primary display resolution")
    return width, height


def primary_refresh_rate():
    # GetDeviceCaps reports the active primary display's rate; drivers may
    # return 0/1 when unknown, which disables the heuristic rather than guessing.
    user32 = ctypes.WinDLL('user32', use_last_error=True)
    gdi32 = ctypes.WinDLL('gdi32', use_last_error=True)
    user32.GetDC.argtypes = [wintypes.HWND]
    user32.GetDC.restype = wintypes.HDC
    user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
    gdi32.GetDeviceCaps.argtypes = [wintypes.HDC, ctypes.c_int]
    dc = user32.GetDC(None)
    if not dc:
        return None
    try:
        refresh = gdi32.GetDeviceCaps(dc, 116)
        return refresh if refresh > 1 else None
    finally:
        user32.ReleaseDC(None, dc)


def force_fullscreen(process, width, height, timeout=15):
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_long]
    user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_uint]
    user32.SetWindowPos.restype = wintypes.BOOL
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"Benchmark exited before fullscreen (code {process.returncode})")
        windows = []
        @callback_type
        def collect(hwnd, _):
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value == process.pid and user32.IsWindowVisible(hwnd):
                windows.append(hwnd)
            return True
        user32.EnumWindows(collect, 0)
        if windows:
            hwnd = windows[0]
            style = user32.GetWindowLongW(hwnd, -16)
            user32.SetWindowLongW(hwnd, -16, style & ~0x00CF0000)  # remove frame/caption
            if not user32.SetWindowPos(hwnd, None, 0, 0, width, height, 0x0020 | 0x0040):
                raise ctypes.WinError(ctypes.get_last_error())
            rect = wintypes.RECT()
            user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
            if not user32.GetWindowRect(hwnd, ctypes.byref(rect)) or (rect.left, rect.top, rect.right, rect.bottom) != (0, 0, width, height):
                raise RuntimeError("Benchmark window did not reach the requested fullscreen bounds")
            return
        time.sleep(0.1)
    raise RuntimeError("Timed out waiting for the benchmark window")
