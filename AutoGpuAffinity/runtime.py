"""Persistent user files are separate from one-file bundled resources."""
import ctypes
import json
import os
from pathlib import Path
import sys


def configure_console():
    kernel = ctypes.WinDLL('kernel32')
    kernel.SetConsoleCP(65001)
    kernel.SetConsoleOutputCP(65001)
    # Python's Windows console I/O uses Unicode; redirected files use UTF-8.
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors='backslashreplace')
    if sys.stdin is not None and hasattr(sys.stdin, 'reconfigure'):
        sys.stdin.reconfigure(encoding='utf-8', errors='strict')


def resources():
    return Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent))


def initialize():
    home = Path(os.environ['LOCALAPPDATA']) / 'AutoGpuAffinity_Fix'
    home.mkdir(parents=True, exist_ok=True)
    config = home / 'config.ini'
    if not config.exists():
        source = resources() / 'config.ini'
        try:
            with config.open('x', encoding='utf-8-sig') as output:
                output.write(source.read_text(encoding='utf-8-sig'))
        except FileExistsError:
            pass
    marker = home / 'first-run.json'
    if not marker.exists():
        print(f'首次使用：配置文件位于 {config}\n可用记事本修改测试时长、核心范围和显卡选择；修改后重新启动程序。')
        try:
            with marker.open('x', encoding='utf-8') as output:
                json.dump({'config': str(config)}, output, ensure_ascii=False)
        except FileExistsError:
            pass
    return home
