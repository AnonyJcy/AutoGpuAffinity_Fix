"""Opt-in, reviewed graceful closure of known desktop apps; never kill processes."""
import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import psutil
import time
APPS = {'chrome.exe': 'Chrome 浏览器', 'msedge.exe': 'Edge 浏览器', 'firefox.exe': 'Firefox 浏览器', 'brave.exe': 'Brave 浏览器', 'opera.exe': 'Opera 浏览器', 'steam.exe': 'Steam', 'epicgameslauncher.exe': 'Epic 游戏启动器', 'discord.exe': 'Discord', 'qq.exe': 'QQ', 'wechat.exe': '微信', 'weixin.exe': '微信', 'spotify.exe': 'Spotify', 'cloudmusic.exe': '网易云音乐', 'qqmusic.exe': 'QQ 音乐', 'bilibili.exe': '哔哩哔哩'}

def session_id(pid):
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.ProcessIdToSessionId.argtypes = [wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    kernel.ProcessIdToSessionId.restype = wintypes.BOOL
    value = wintypes.DWORD()
    if not kernel.ProcessIdToSessionId(pid, ctypes.byref(value)):
        raise ctypes.WinError(ctypes.get_last_error())
    return value.value

def candidates():
    owner = psutil.Process()
    protected = {owner.pid, *(process.pid for process in owner.parents())}
    username, session = (owner.username(), session_id(owner.pid))
    windows = Path(os.environ['WINDIR']).resolve()
    result = []
    for process in psutil.process_iter():
        try:
            name = process.name().lower()
            executable = process.exe()
            if process.pid in protected or name not in APPS or (not executable) or (process.username() != username) or (session_id(process.pid) != session) or Path(executable).resolve().is_relative_to(windows):
                continue
            result.append({'pid': process.pid, 'created': process.create_time(), 'exe': executable, 'name': name, 'label': APPS[name], 'memory_bytes': process.memory_info().rss, 'cpu_seconds': sum(process.cpu_times()[:2])})
        except (psutil.Error, OSError):
            continue
    return result

def ranked_apps(items):
    """Measure a shared interval, aggregate software processes, show at most ten."""
    if not items:
        return []
    started = time.monotonic()
    time.sleep(0.3)
    groups = {}
    for item in items:
        try:
            process = psutil.Process(item['pid'])
            if process.create_time() != item['created'] or process.exe() != item['exe']:
                continue
            seconds = sum(process.cpu_times()[:2])
            cpu = max(0, seconds - item['cpu_seconds']) / (time.monotonic() - started) * 100 / (os.cpu_count() or 1)
            memory = process.memory_info().rss
        except (psutil.Error, OSError):
            continue
        group = groups.setdefault(item['label'], {'label': item['label'], 'items': [], 'cpu': 0, 'memory': 0})
        group['items'].append(item)
        group['cpu'] += cpu
        group['memory'] += memory
    return sorted(groups.values(), key=lambda group: (-group['cpu'], -group['memory'], group['label']))[:10]

def close_windows(pids):
    """Send WM_CLOSE to top-level windows; applications retain save/exit dialogs."""
    user = ctypes.WinDLL('user32', use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
    user.EnumWindows.restype = wintypes.BOOL
    user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user.GetWindowThreadProcessId.restype = wintypes.DWORD
    user.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user.PostMessageW.restype = wintypes.BOOL
    sent = set()

    @callback_type
    def visit(window, parameter):
        pid = wintypes.DWORD()
        user.GetWindowThreadProcessId(window, ctypes.byref(pid))
        if pid.value in pids and user.PostMessageW(window, 16, 0, 0):
            sent.add(pid.value)
        return True
    if not user.EnumWindows(visit, 0):
        raise ctypes.WinError(ctypes.get_last_error())
    return sent

def assist(non_interactive=False):
    if non_interactive:
        return
    print('\n是否辅助关闭后台软件？减少后台干扰有助于比较成绩。')
    print('仅处理识别到的常见浏览器、聊天、音乐和游戏启动器；系统、安全、驱动、同步及未知程序不处理。')
    groups = ranked_apps(candidates())
    if not groups:
        print('没有识别到可辅助关闭的软件；其他后台请自行检查。')
        return
    print('按软件合并，按 CPU 占用排序，同占用时比较内存；最多列出前 10 个软件。')
    for group in groups:
        print(f"  {group['label']}：{len(group['items'])} 个进程，CPU {group['cpu']:.1f}%，内存合计 {group['memory'] / 1024 ** 2:.0f} MB")
    print('请先保存工作。只请求正常退出；保存提示或仍运行的后台不会被强制结束。关闭后不会自动重新打开。')
    if input('输入 1 全部关闭上述软件；回车跳过：').strip() != '1':
        return
    selected = [item for group in groups for item in group['items']]
    current = {(item['pid'], item['created'], item['exe']): item for item in candidates()}
    processes = []
    for item in selected:
        identity = (item['pid'], item['created'], item['exe'])
        if identity in current:
            try:
                process = psutil.Process(item['pid'])
                if process.create_time() == item['created'] and process.exe() == item['exe']:
                    processes.append(process)
            except psutil.Error:
                pass
    sent = close_windows({process.pid for process in processes})
    gone, alive = psutil.wait_procs(processes, timeout=5)
    print(f'已退出 {len(gone)} 个进程。')
    alive_ids = {process.pid for process in alive}
    for group in groups:
        remaining = [item['pid'] for item in group['items'] if item['pid'] in alive_ids]
        if remaining:
            print(f"  {group['label']}：仍有 {len(remaining)} 个进程，请自行处理退出/保存提示。")
    input('确认后台状态后按回车继续跑分：')
