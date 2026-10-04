"""Dynamically discover desktop apps and request reviewed graceful closure."""
import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import psutil
import time
import win32api


def window_processes():
    user = ctypes.WinDLL('user32', use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
    user.EnumWindows.restype = wintypes.BOOL
    user.IsWindowVisible.argtypes = [wintypes.HWND]
    user.IsWindowVisible.restype = wintypes.BOOL
    user.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user.GetWindowTextLengthW.restype = ctypes.c_int
    user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user.GetWindowThreadProcessId.restype = wintypes.DWORD
    result = set()
    @callback_type
    def visit(window, parameter):
        if user.IsWindowVisible(window) and user.GetWindowTextLengthW(window):
            pid = wintypes.DWORD()
            user.GetWindowThreadProcessId(window, ctypes.byref(pid))
            result.add(pid.value)
        return True
    if not user.EnumWindows(visit, 0):
        raise ctypes.WinError(ctypes.get_last_error())
    return result


def application_name(executable):
    if Path(executable).name.lower() in APPS:
        return APPS[Path(executable).name.lower()]
    try:
        translations = win32api.GetFileVersionInfo(executable, '\\VarFileInfo\\Translation')
        for language, codepage in translations:
            value = win32api.GetFileVersionInfo(executable,
                f'\\StringFileInfo\\{language:04x}{codepage:04x}\\ProductName').strip()
            if value and value.lower() not in ('microsoft® windows® operating system', 'microsoft windows operating system'):
                return value
    except Exception:
        pass
    return Path(executable).stem


def protected_executable(executable):
    # These are exclusions, not an application allowlist. Services are excluded separately.
    parts = {part.lower() for part in Path(executable).parts}
    name = Path(executable).name.lower()
    return (bool(parts & {'nvidia corporation', 'nvidia', 'amd', 'intel', 'windows defender',
                         'windows security', 'kaspersky lab', 'eset', 'huorong', '360', 'codex',
                         'avast software', 'avg', 'bitdefender', 'malwarebytes', 'norton', 'mcafee',
                         'sophos', 'avira', 'trend micro', 'f-secure', 'comodo'})
            or name in {'codex.exe', 'codexapp.exe', 'autogpuaffinity.exe', 'explorer.exe',
                        'securityhealthsystray.exe', 'msmpeng.exe', 'hwsysmon.exe', 'hipstray.exe',
                        'onedrive.exe', 'dropbox.exe', 'googledrivefs.exe', 'nvcontainer.exe',
                        'audiodg.exe', 'dwm.exe', 'ctfmon.exe', 'sihost.exe', 'textinputhost.exe'})

APPS = {
    'chrome.exe': 'Chrome 浏览器', 'msedge.exe': 'Edge 浏览器',
    'firefox.exe': 'Firefox 浏览器', 'brave.exe': 'Brave 浏览器',
    'opera.exe': 'Opera 浏览器', 'steam.exe': 'Steam',
    'epicgameslauncher.exe': 'Epic 游戏启动器', 'discord.exe': 'Discord',
    'qq.exe': 'QQ', 'wechat.exe': '微信', 'weixin.exe': '微信',
    'spotify.exe': 'Spotify', 'cloudmusic.exe': '网易云音乐',
    'qqmusic.exe': 'QQ 音乐', 'bilibili.exe': '哔哩哔哩',
}


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
    username, session = owner.username(), session_id(owner.pid)
    windows = Path(os.environ['WINDIR']).resolve()
    windows_apps = window_processes()
    service_pids = set()
    # A failed service inventory must not expand the automatic closure scope.
    for service in psutil.win_service_iter():
        try:
            pid = service.pid()
            if pid:
                service_pids.add(pid)
        except psutil.Error:
            raise RuntimeError('无法核验系统服务，跳过后台关闭。')
    ancestor_paths = {process.exe().lower() for process in [owner, *owner.parents()] if process.exe()}
    inventory = {}
    for process in psutil.process_iter():
        try:
            name = process.name().lower()
            executable = process.exe()
            if (process.pid in protected or process.pid in service_pids or not executable
                    or executable.lower() in ancestor_paths or protected_executable(executable)
                    or process.username() != username or session_id(process.pid) != session
                    or Path(executable).resolve().is_relative_to(windows)):
                continue
            inventory[process.pid] = {'pid': process.pid, 'parent': process.ppid(), 'created': process.create_time(),
                           'exe': executable, 'name': name,
                           'memory_bytes': process.memory_info().rss,
                           'cpu_seconds': sum(process.cpu_times()[:2]), 'sample_at': time.monotonic()}
        except (psutil.Error, OSError):
            continue
    result, labels = [], {}
    for pid, item in inventory.items():
        current, anchor, seen = pid, None, set()
        while current in inventory and current not in seen and len(seen) < 32:
            seen.add(current)
            if current in windows_apps:
                anchor = inventory[current]
                break
            current = inventory[current]['parent']
        if anchor is None:
            continue  # Unknown headless processes have no reliable normal-exit target.
        key = anchor['exe'].lower()
        labels.setdefault(key, application_name(anchor['exe']))
        result.append(dict(item, label=labels[key], app_key=key))
    return result


def ranked_apps(items):
    """Measure a shared interval, aggregate software processes, show at most ten."""
    if not items:
        return []
    started = time.monotonic()
    time.sleep(.3)
    groups = {}
    for item in items:
        try:
            process = psutil.Process(item['pid'])
            if process.create_time() != item['created'] or process.exe() != item['exe']:
                continue
            seconds = sum(process.cpu_times()[:2])
            cpu = max(0, seconds - item['cpu_seconds']) / (time.monotonic() - item.get('sample_at', started)) * 100 / (os.cpu_count() or 1)
            memory = process.memory_info().rss
        except (psutil.Error, OSError):
            continue
        group = groups.setdefault(item.get('app_key', item['label']), {'label': item['label'], 'items': [], 'cpu': 0, 'memory': 0})
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
        if pid.value in pids and user.PostMessageW(window, 0x0010, 0, 0):
            sent.add(pid.value)
        return True
    if not user.EnumWindows(visit, 0):
        raise ctypes.WinError(ctypes.get_last_error())
    return sent


def assist(non_interactive=False):
    if non_interactive:
        return
    print('\n是否辅助关闭后台软件？')
    try:
        groups = ranked_apps(candidates())
    except (OSError, psutil.Error, RuntimeError) as error:
        print(f'无法完成后台检测，已跳过：{error}')
        return
    if not groups:
        print('没有可辅助关闭的软件。')
        return
    print('当前占用较高的软件：')
    for group in groups:
        print(f"  {group['label']}")
    print('请先保存工作，仅请求正常退出。')
    if input('输入 1 全部关闭上述软件；回车跳过：').strip() != '1':
        return
    selected = [item for group in groups for item in group['items']]
    # Re-enumerate after consent: PID reuse, session changes and new apps are excluded.
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
