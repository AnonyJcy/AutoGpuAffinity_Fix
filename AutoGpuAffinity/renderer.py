"""Probe the shipped renderer if standalone Vulkan enumeration is unavailable."""
import re
import ctypes
from ctypes import wintypes
import subprocess
import time
from pathlib import Path

def visible_windows(process):
    user32 = ctypes.WinDLL('user32', use_last_error=True)
    callback = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows.argtypes = [callback, wintypes.LPARAM]
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    windows = []

    @callback
    def collect(hwnd, unused):
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value == process.pid and user32.IsWindowVisible(hwnd):
            windows.append(hwnd)
        return True
    user32.EnumWindows(collect, 0)
    return windows

def wait_window(process, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f'Renderer exited during startup ({process.returncode})')
        if visible_windows(process):
            return
        time.sleep(0.1)
    raise RuntimeError('Renderer did not create a visible window')

def stop(process, timeout=10):
    """WM_CLOSE lets liblava flush its file sink; TerminateProcess loses logs."""
    if process.poll() is None:
        user32 = ctypes.WinDLL('user32', use_last_error=True)
        user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        for hwnd in visible_windows(process):
            user32.PostMessageW(hwnd, 16, 0, 0)
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired as error:
            process.kill()
            process.wait()
            raise RuntimeError('Renderer did not close normally; native GPU log unverified') from error
    if process.returncode != 0:
        raise RuntimeError(f'Renderer exited abnormally ({process.returncode})')

def launch(binary, args, directory):
    binary = str(Path(binary).resolve())
    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    native_log = directory / 'lava.log'
    native_log.unlink(missing_ok=True)
    with (directory / 'console.log').open('wb', buffering=0) as console:
        process = subprocess.Popen([binary, '--log=2', *args], cwd=directory, stdout=console, stderr=subprocess.STDOUT)
    return (process, native_log)

def verify_log(log_path, target):
    log = Path(log_path).read_text(encoding='utf-8', errors='replace')
    name = device_name(log)
    if not name or not same_gpu(target.name, name):
        raise RuntimeError(f'Renderer selected {name!r}, expected {target.name}. Details: {log_path}')
    return name

def device_name(log):
    match = re.search('device:\\s*(.+?)\\s+\\([^\\r\\n]*\\)\\s*-\\s*driver:', log, re.I)
    return match.group(1).strip() if match else None

def same_gpu(target_name, render_name):
    clean = lambda n: re.sub('[^a-z0-9]', '', re.sub('\\s*\\([^)]*\\)\\s*$', '', n).casefold())
    return clean(target_name) == clean(render_name)

def wait_ready(process, log_path, target, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        log = Path(log_path).read_text(encoding='utf-8', errors='replace')
        name = device_name(log)
        if process.poll() is not None:
            raise RuntimeError(f'Renderer exited ({process.returncode}). Details: {log_path}\n{log[-2000:]}')
        if name:
            if not same_gpu(target.name, name):
                raise RuntimeError(f'Renderer selected {name}, expected {target.name}. Details: {log_path}')
            return name
        time.sleep(0.1)
    raise RuntimeError(f'Renderer did not report its GPU. Details: {log_path}')

def probe_device(binary, target, count, directory, indices=None):
    Path(directory).mkdir(parents=True, exist_ok=True)
    failures = []
    for index in range(count) if indices is None else indices:
        process = None
        try:
            process, log_path = launch(binary, [f'--physical_device={index}', '--fullscreen=0', '--width=640', '--height=480', '--fps_cap=60'], Path(directory) / f'probe-{index}')
            wait_window(process, timeout=8)
            time.sleep(1)
            stop(process)
            verify_log(log_path, target)
            return index
        except (OSError, RuntimeError) as e:
            failures.append(f'{index}: {e}')
        finally:
            if process is not None and process.poll() is None:
                process.kill()
                process.wait()
    raise RuntimeError('Target GPU could not start the Vulkan renderer. See probe logs in ' + str(directory) + '\n' + '\n'.join(failures))
