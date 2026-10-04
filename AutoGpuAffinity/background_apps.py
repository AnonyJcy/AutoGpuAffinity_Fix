"""Opt-in, reviewed graceful closure of known desktop apps; never kill processes."""
import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import psutil
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
            result.append({'pid': process.pid, 'created': process.create_time(), 'exe': executable, 'name': name, 'label': APPS[name]})
        except (psutil.Error, OSError):
            continue
    return sorted(result, key=lambda item: (item['label'], item['pid']))

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
    if input('输入 1 查看并选择；直接回车跳过：').strip() != '1':
        return
    items = candidates()
    if not items:
        print('没有识别到可辅助关闭的软件；其他后台请自行检查。')
        return
    for index, item in enumerate(items, 1):
        print(f"  {index}. {item['label']} [{item['name']}，PID {item['pid']}]\n     {item['exe']}")
    print('请先保存工作。只请求正常退出；保存提示或仍运行的后台不会被强制结束。关闭后不会自动重新打开。')
    choice = input('输入要关闭的编号（逗号分隔），或“全部”；直接回车取消：').strip()
    if not choice:
        return
    try:
        selected = items if choice == '全部' else [items[index - 1] for index in sorted({int(value.strip()) for value in choice.replace('，', ',').split(',')}) if 1 <= index <= len(items)]
        if choice != '全部' and any((not 1 <= int(value.strip()) <= len(items) for value in choice.replace('，', ',').split(','))):
            raise ValueError()
    except ValueError:
        print('编号无效，本次未关闭任何软件。')
        return
    if input('输入“确认关闭”请求退出所选软件；其他输入取消：').strip() != '确认关闭':
        print('已取消关闭。')
        return
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
    for process in alive:
        print(f'  PID {process.pid}：' + ('仍在运行，请自行处理退出/保存提示。' if process.pid in sent else '没有可接收退出请求的窗口，请自行关闭。'))
    input('确认后台状态后按回车继续跑分：')
