"""Audit real PresentMon pacing and keep fullscreen fallback consistent."""
import csv
import ctypes
from ctypes import wintypes
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import time


def capture(binary, process, seconds, path):
    path = Path(path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    session_name = f'AutoGpuAffinity-{os.getpid()}-{time.monotonic_ns()}'
    with path.with_suffix('.presentmon.log').open('wb') as log:
        try:
            subprocess.run([str(Path(binary).resolve()), '-session_name', session_name, '-no_top',
                '-timed', str(seconds), '-process_id', str(process.pid), '-output_file', str(path),
                '-terminate_after_timed', '-terminate_on_proc_exit'], stdout=log, stderr=subprocess.STDOUT,
                check=True, timeout=seconds + 20)
        except (subprocess.SubprocessError, OSError):
            try:
                subprocess.run([str(Path(binary).resolve()), '-session_name', session_name,
                    '-terminate_existing'], stdout=log, stderr=subprocess.STDOUT, timeout=5, check=False)
            except (subprocess.SubprocessError, OSError):
                pass
            raise
    if process.poll() is not None:
        raise RuntimeError(f'Renderer exited during capture: {path}')
    return audit(path, process.pid)


def audit(path, pid):
    with Path(path).open(encoding='utf-8-sig', newline='') as file:
        rows = [{str(k).lower(): v for k, v in row.items()} for row in csv.DictReader(file)]
    if not rows:
        raise RuntimeError(f'Empty PresentMon capture: {path}')
    frames = []
    for row in rows:
        if row.get('processid') != str(pid):
            raise RuntimeError(f'Capture contains another process: {path}')
        try:
            interval = float(row['msbetweenpresents'])
        except (KeyError, ValueError, TypeError) as error:
            raise RuntimeError(f'Invalid frame time in {path}') from error
        if not math.isfinite(interval) or interval < 0:
            raise RuntimeError(f'Invalid frame time in {path}')
        if interval > 0:
            frames.append(interval)
    if len(frames) < 2:
        raise RuntimeError(f'Fewer than two valid frames: {path}')
    return {'valid_frames': len(frames), 'average_fps': 1000 * len(frames) / sum(frames),
            'present_modes': sorted({row.get('presentmode', 'unknown') for row in rows}),
            'sync_intervals': sorted({row.get('syncinterval', 'unknown') for row in rows}),
            'allows_tearing': sorted({row.get('allowstearing', 'unknown') for row in rows}),
            'refresh_paced_fraction': None, '_frametimes': frames}


def refresh_paced(stats, refresh):
    # A near-refresh average alone is not proof: require a tightly paced trace
    # and an independent-flip, non-tearing path before testing composition.
    if not refresh or refresh <= 1:
        return False
    interval = 1000 / refresh
    fraction = sum(abs(value - interval) <= interval * 0.08
                   for value in stats['_frametimes']) / stats['valid_frames']
    stats['refresh_paced_fraction'] = fraction
    return (abs(stats['average_fps'] / refresh - 1) < 0.035 and fraction > 0.80
            and any('independent flip' in mode.lower() for mode in stats['present_modes'])
            and stats['allows_tearing'] == ['0'])


def public_stats(stats):
    return {key: value for key, value in stats.items() if not key.startswith('_')}


class StatusOverlay:
    """A visible informational banner, owned only by this benchmark run."""
    def __init__(self, renderer_pid, text):
        import os
        command = ([sys.executable, '--presentation-overlay'] if getattr(sys, 'frozen', False)
                   else [sys.executable, str(Path(__file__).resolve()), '--presentation-overlay'])
        self.process = subprocess.Popen([*command, str(renderer_pid), str(os.getpid()), text],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        # The worker prints readiness only after a real visible HWND exists.
        import threading
        reply = []
        thread = threading.Thread(target=lambda: reply.append(self.process.stdout.readline()), daemon=True)
        thread.start()
        thread.join(10)
        if thread.is_alive() or not reply or not reply[0].startswith(b'READY '):
            self.close()
            raise RuntimeError('Fullscreen status overlay failed to start')
        self.hwnd = int(reply[0].split()[1])

    def check(self):
        user32 = ctypes.WinDLL('user32')
        user32.IsWindowVisible.argtypes = [wintypes.HWND]
        if self.process.poll() is not None or not user32.IsWindowVisible(self.hwnd):
            raise RuntimeError('Fullscreen status overlay disappeared during capture')

    def close(self):
        if self.process.poll() is None:
            if hasattr(self, 'hwnd'):
                user32 = ctypes.WinDLL('user32')
                user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
                user32.PostMessageW(self.hwnd, 0x10, 0, 0)
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        self.process.stdout.close()
        self.process.stderr.close()


def overlay_worker(renderer_pid, parent_pid, text):
    user32 = ctypes.WinDLL('user32', use_last_error=True)
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    handles = (wintypes.HANDLE * 2)(kernel32.OpenProcess(0x100000, False, renderer_pid),
                                  kernel32.OpenProcess(0x100000, False, parent_pid))
    user32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR,
        wintypes.DWORD, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, ctypes.c_void_p]
    user32.CreateWindowExW.restype = wintypes.HWND
    user32.UpdateWindow.argtypes = [wintypes.HWND]
    user32.DestroyWindow.argtypes = [wintypes.HWND]
    user32.IsWindow.argtypes = [wintypes.HWND]
    user32.PeekMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT, wintypes.UINT]
    user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
    user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
    user32.DispatchMessageW.restype = ctypes.c_ssize_t
    user32.MsgWaitForMultipleObjects.argtypes = [wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE), wintypes.BOOL, wintypes.DWORD, wintypes.DWORD]
    hwnd = None
    try:
        if not all(handles):
            raise ctypes.WinError(ctypes.get_last_error())
        hwnd = user32.CreateWindowExW(0x08000088, 'STATIC', text, 0x90000000,
                                     16, 16, 300, 36, None, None, None, None)
        if not hwnd:
            raise ctypes.WinError(ctypes.get_last_error())
        user32.UpdateWindow(hwnd)
        print(f'READY {hwnd}', flush=True)
        message = wintypes.MSG()
        while user32.IsWindow(hwnd):
            status = user32.MsgWaitForMultipleObjects(2, handles, False, 250, 0x04FF)
            if status in (0, 1):
                break
            if status == 0xFFFFFFFF:
                raise ctypes.WinError(ctypes.get_last_error())
            while user32.PeekMessageW(ctypes.byref(message), None, 0, 0, 1):
                user32.TranslateMessage(ctypes.byref(message))
                user32.DispatchMessageW(ctypes.byref(message))
        return 0
    finally:
        if hwnd and user32.IsWindow(hwnd):
            user32.DestroyWindow(hwnd)
        for handle in handles:
            if handle:
                kernel32.CloseHandle(handle)


if __name__ == '__main__':
    sys.exit(overlay_worker(int(sys.argv[2]), int(sys.argv[3]), sys.argv[4]))
