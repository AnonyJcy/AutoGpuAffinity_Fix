import argparse
import csv
import ctypes
import datetime
import logging
import json
import os
import re
import math
import subprocess
import sys
import textwrap
import time
import traceback
import winreg
from typing import NoReturn

import consts
import framerate
import display
import renderer
import presentation
import recommendation
import runtime
import benchmark_modes
import gpu_selection
from pathlib import Path
from gpu import Gpu, select_gpu, match_vulkan, query_vulkan_adapters, wait_vulkan_device, vulkan_query_worker
import psutil
import setupapi
import wmi
from config import Api, Config

LOG_CLI = logging.getLogger("CLI")
INSTANCE_GUARD = None


def lock_benchmark():
    global INSTANCE_GUARD
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.wintypes.BOOL, ctypes.c_wchar_p]
    kernel32.CreateMutexW.restype = ctypes.wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [ctypes.wintypes.HANDLE]
    handle = kernel32.CreateMutexW(None, False, r'Local\AutoGpuAffinity-benchmark')
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    if ctypes.get_last_error() == 183:
        kernel32.CloseHandle(handle)
        raise RuntimeError('Another AutoGpuAffinity benchmark is running; close it before starting this session.')
    INSTANCE_GUARD = handle


def start_afterburner(path: str, profile: int) -> None:
    with subprocess.Popen([path, f"/Profile{profile}", "/Q"]) as process:
        time.sleep(5)
        process.kill()


def set_driver_state(hwid: str, state: int) -> int:
    device_info_handle = setupapi.SetupDiGetClassDevsW(
        None, ctypes.c_wchar_p(hwid), None, setupapi.DIGCF_ALLCLASSES | setupapi.DIGCF_DEVICEINTERFACE
    )

    if device_info_handle == ctypes.c_void_p(-1).value:
        LOG_CLI.error(f"SetupDiGetClassDevsW failed: {ctypes.get_last_error()}")
        return 1

    try:
        dev_info_data = setupapi.SP_DEVINFO_DATA()
        dev_info_data.cbSize = ctypes.sizeof(setupapi.SP_DEVINFO_DATA)

        if not setupapi.SetupDiEnumDeviceInfo(device_info_handle, 0, ctypes.byref(dev_info_data)):
            LOG_CLI.error(f"SetupDiEnumDeviceInfo failed: {ctypes.get_last_error()}")
            return 1

        params = setupapi.SP_PROPCHANGE_PARAMS()

        params.ClassInstallHeader.cbSize = ctypes.sizeof(params.ClassInstallHeader)
        params.ClassInstallHeader.InstallFunction = setupapi.DIF_PROPERTYCHANGE
        params.StateChange = state
        params.Scope = setupapi.DICS_FLAG_GLOBAL
        params.HwProfile = 0

        if not setupapi.SetupDiSetClassInstallParamsA(
            device_info_handle,
            ctypes.byref(dev_info_data),
            ctypes.byref(params.ClassInstallHeader),
            ctypes.sizeof(params),
        ):
            LOG_CLI.error(f"SetupDiSetClassInstallParamsA failed: {ctypes.get_last_error()}")
            return 1

        if not setupapi.SetupDiCallClassInstaller(
            setupapi.DIF_PROPERTYCHANGE, device_info_handle, ctypes.byref(dev_info_data)
        ):
            LOG_CLI.error(f"SetupDiCallClassInstaller failed: {ctypes.get_last_error()}")
            return 1

        return 0

    finally:
        setupapi.SetupDiDestroyDeviceInfoList(device_info_handle)


def restart_driver(hwid: str) -> int:
    if set_driver_state(hwid, setupapi.DICS_DISABLE) != 0:
        LOG_CLI.error("failed to disable driver while restarting")
        return 1

    try:
        time.sleep(2)
    finally:
        # Even Ctrl+C during the disabled interval must re-enable the device.
        enable_result = set_driver_state(hwid, setupapi.DICS_ENABLE)
    if enable_result != 0:
        raise RuntimeError(f'Failed to re-enable GPU {hwid}')

    time.sleep(2)

    return 0


def apply_affinity(hwids: list[str], cpu: int = -1, apply: bool = True) -> int:
    for hwid in hwids:
        policy_path = f"SYSTEM\\CurrentControlSet\\Enum\\{hwid}\\Device Parameters\\Interrupt Management\\Affinity Policy"

        if apply and cpu != -1:
            members = [cpu] if isinstance(cpu, int) else cpu
            if not members or len(set(members)) != len(members) or any(not 0 <= item < 64 for item in members):
                raise ValueError('无效核心组合')
            mask = sum(1 << item for item in members)
            le_hex = mask.to_bytes(8, "little").rstrip(b"\x00")

            with winreg.CreateKey(winreg.HKEY_LOCAL_MACHINE, policy_path) as key:
                winreg.SetValueEx(key, "DevicePolicy", 0, winreg.REG_DWORD, 4)
                winreg.SetValueEx(
                    key,
                    "AssignmentSetOverride",
                    0,
                    winreg.REG_BINARY,
                    le_hex,
                )

        else:
            try:
                with winreg.OpenKey(
                    winreg.HKEY_LOCAL_MACHINE,
                    policy_path,
                    0,
                    winreg.KEY_SET_VALUE | winreg.KEY_WOW64_64KEY,
                ) as key:
                    for value in ('DevicePolicy', 'AssignmentSetOverride'):
                        try:
                            winreg.DeleteValue(key, value)
                        except FileNotFoundError:
                            pass
            except FileNotFoundError:
                LOG_CLI.debug("affinity policy has already been removed for %s", hwid)

        if restart_driver(hwid) != 0:
            LOG_CLI.error("failed to restart driver")
            return 1

    return 0


def snapshot_affinity(hwid):
    path = f'SYSTEM\\CurrentControlSet\\Enum\\{hwid}\\Device Parameters\\Interrupt Management\\Affinity Policy'
    values = {}
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path, 0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as key:
            for name in ('DevicePolicy', 'AssignmentSetOverride'):
                try:
                    values[name] = winreg.QueryValueEx(key, name)
                except FileNotFoundError:
                    values[name] = None
    except FileNotFoundError:
        values = {name: None for name in ('DevicePolicy', 'AssignmentSetOverride')}
    return values


def restore_affinity(hwid, values):
    path = f'SYSTEM\\CurrentControlSet\\Enum\\{hwid}\\Device Parameters\\Interrupt Management\\Affinity Policy'
    with winreg.CreateKey(winreg.HKEY_LOCAL_MACHINE, path) as key:
        for name, original in values.items():
            if original is None:
                try:
                    winreg.DeleteValue(key, name)
                except FileNotFoundError:
                    pass
            else:
                winreg.SetValueEx(key, name, 0, original[1], original[0])
    if snapshot_affinity(hwid) != values:
        raise RuntimeError('Target GPU original affinity could not be restored')
    if restart_driver(hwid) != 0:
        raise RuntimeError('Failed to restart target GPU after restoring original affinity')


def print_table(formatted_results: dict[str, dict[str, str]]):
    # print table headings
    print(f"{'CPU':<5}", end="")

    for metric in (
        "最大帧",
        "平均帧",
        "最低帧",
        "标准差",
        "1 %ile",
        "0.1 %ile",
        "0.01 %ile",
        "0.005 %ile",
        "1% Low",
        "0.1% Low",
        "0.01% Low",
        "0.005% Low",
    ):
        print(metric + ' ' * max(1, 12 - sum(2 if ord(char) > 127 else 1 for char in metric)), end='')

    print()  # new line

    # print values for each heading
    for _cpu, _results in formatted_results.items():
        print(f"{_cpu:<5}", end="")
        for metric_value in _results.values():
            # padding needs to be larger to compensate for color chars
            right_padding = 21 if "[" in metric_value else 12
            print(f"{metric_value:<{right_padding}}", end="")

        print()  # new line

    print()  # new line


def display_results(csv_directory: str, enable_color: bool, recommend_cores=True) -> None:
    status_path = os.path.join(os.path.dirname(os.path.abspath(csv_directory)), 'session-status.json')
    if os.path.exists(status_path):
        with open(status_path, encoding='utf-8') as report:
            status = json.load(report)
        if status.get('status') != 'complete':
            raise RuntimeError('This session failed or is incomplete; refusing to rank its captures. See session-status.json.')
    results: dict[str, dict[str, float]] = {}
    recommendation_results = {}
    frame_counts = {}

    # each index represents the rank (e.g. index 0 is 1st)
    colors: list[str] = [
        "\x1b[92m",  # Green
        "\x1b[93m",  # Yellow
    ]

    if enable_color:
        default = "\x1b[0m"
        os.system("color")
    else:
        default = ""

    cpus = sorted(int(match.group(1)) for file in os.listdir(csv_directory)
                  if (match := re.fullmatch(r'CPU-(\d+)\.csv', file)))
    if not cpus:
        raise RuntimeError('No CPU capture CSVs found in the session')
    num_cpus = len(cpus)
    # 1 CPUs means no ranking will be done
    # 2 CPUs means only one metric will be ranked since it can be either or
    # always leave last place unranked

    top_n_values = num_cpus - 1 if num_cpus < 3 else len(colors)

    for cpu in cpus:
        csv_file = f"CPU-{cpu}.csv"

        frametimes: list[float] = []

        with open(f"{csv_directory}\\{csv_file}", encoding="utf-8") as file:
            for row in csv.DictReader(file):
                # convert key names to lowercase because column names changed in a newer version of PresentMon
                row_lower = {key.lower(): value for key, value in row.items()}

                if (ms_between_presents := row_lower.get("msbetweenpresents")) is not None:
                    value = float(ms_between_presents)
                    if not math.isfinite(value) or value < 0:
                        raise RuntimeError(f'Invalid frame time in {csv_file}')
                    if value > 0:
                        frametimes.append(value)

        if len(frametimes) < 2:
            raise RuntimeError(f'Fewer than two valid frames in {csv_file}')

        fps = framerate.Fps(frametimes)
        frame_counts[str(cpu)] = len(frametimes)
        recommendation_results[str(cpu)] = {
            'maximum': fps.maximum(), 'average': fps.average(),
            'minimum': fps.minimum(), 'stdev': fps.stdev(), 'lows1': fps.lows(1),
        }

        # results of current CPU in results dict
        results[str(cpu)] = {
            "maximum": round(fps.maximum(), 2),
            "average": round(fps.average(), 2),
            "minimum": round(fps.minimum(), 2),
            # negate positive value so that highest negative value will be the lowest absolute value
            "stdev": round(-fps.stdev(), 2),
            **{
                f"{metric}{value}": round(getattr(fps, metric)(value), 2)
                for metric in ("percentile", "lows")
                for value in (1, 0.1, 0.01, 0.005)
            },
        }

    formatted_results: dict[str, dict[str, str]] = {cpu: {} for cpu in results}

    # analyze best values for each metric
    for metric in (
        "maximum",
        "average",
        "minimum",
        "stdev",
        # "percentile1", "percentile0.1" etc
        *(tuple(f"{metric}{value}" for metric in ("percentile", "lows") for value in (1, 0.1, 0.01, 0.005))),
    ):
        # set of all values within the metric
        values = {_results[metric] for _results in results.values()}

        # create ordered list without duplicates of top n values
        top_values = list(dict.fromkeys(sorted(values, reverse=True)[:top_n_values]))

        for _cpu, _results in results.items():
            metric_value = _results[metric]

            # abs is for negative values such as stdev
            # :.2f is for .00 numerical formatting
            new_value = f"{abs(metric_value):.2f}"

            # determine rank of value
            if enable_color:
                try:
                    nth_best = top_values.index(metric_value)
                    color = colors[nth_best]
                    new_value = f"{color}{new_value}{default}"
                except ValueError:
                    # don't highlight value as top n by leaving it unmodified
                    pass

            formatted_results[_cpu][metric] = new_value

    # os.system("<nul set /p=\x1b[8;50;1000t")

    print_table(formatted_results)
    if recommend_cores:
        recommendation.display_recommendations(csv_directory, recommendation_results, frame_counts)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='自动显卡亲和性跑分、核心推荐与设备中断绑定工具', add_help=False)
    parser.add_argument('-h', '--help', action='help', help='显示中文帮助并退出')
    parser._positionals.title = '位置参数'
    parser._optionals.title = '可选参数'

    parser.add_argument(
        "--version",
        action="version",
        version=f"AutoGpuAffinity v{consts.VERSION}",
        help='显示版本并退出',
    )
    parser.add_argument(
        "--config",
        metavar="<config>",
        type=str,
        help="配置文件路径（UTF-8）",
    )
    parser.add_argument(
        "--analyze",
        metavar="<csv directory>",
        type=str,
        help="分析历史跑分 CSV 文件夹",
    )
    parser.add_argument(
        "--apply-affinity",
        metavar="<cpu>",
        type=int,
        help="手动指定显卡单核心绑定，需要交互确认",
    )
    parser.add_argument('--non-interactive', action='store_true',
                        help='无人值守测试，不显示提示且不应用最终设备绑定')
    parser.add_argument('--mode', choices=('simple', 'enhanced'), help='模式：simple 简单版 / enhanced 三轮增强版')
    parser.add_argument('--list-devices', action='store_true', help='只读列出输入设备、中断目标和计时器')
    parser.add_argument('--prepare-background', action='store_true', help='仅辅助关闭后台软件，不开始跑分')
    parser.add_argument('--bind-from', metavar='<session>', help='从完整实测会话生成绑定方案，确认后应用')
    parser.add_argument('--restore-affinity', metavar='<backup.json>', help='核对并恢复本机绑定备份，确认后执行')

    return parser.parse_args()


def is_admin() -> bool:
    return ctypes.windll.shell32.IsUserAnAdmin()


def kill_processes(*targets: str) -> None:
    targets_set = set(targets)

    for process in psutil.process_iter():
        if process.name().lower() in targets_set:
            process.kill()


def main() -> int:
    runtime.configure_console()
    logging.basicConfig(format="[%(name)s] %(levelname)s: %(message)s", level=logging.INFO)

    print(
        f"AutoGpuAffinity 版本 {consts.VERSION} - GPLv3\n维护与修改：{consts.AUTHOR}\nGitHub - {consts.PROJECT_URL}\n禁止以本软件参与任何形式的收费优化\n",
    )

    if not is_admin():
        LOG_CLI.error("需要管理员权限，请以管理员身份运行。")
        return 1

    args = parse_args()
    # Resolve user paths before changing CWD; bundled assets never live beside config.
    for attribute in ('config', 'analyze', 'bind_from', 'restore_affinity'):
        value = getattr(args, attribute)
        if value:
            setattr(args, attribute, os.path.abspath(value))
    work_directory = runtime.initialize()
    os.chdir(work_directory)
    if args.prepare_background:
        import background_apps
        background_apps.assist(args.non_interactive)
        return 0
    if args.list_devices:
        import binding
        binding.show_inventory()
        return 0
    if args.restore_affinity:
        import binding
        return binding.confirm_restore(args.restore_affinity, args.non_interactive)
    if args.bind_from:
        import binding
        return binding.confirm_binding(args.bind_from, args.non_interactive)

    winver = sys.getwindowsversion()

    gpus = [Gpu(g.Name, g.PnPDeviceID) for g in wmi.WMI().Win32_VideoController() if g.PnPDeviceID]
    hwids_gpu = [g.hwid for g in gpus]

    if not hwids_gpu:
        LOG_CLI.error("no graphics cards found")
        return 1

    cpu_count = os.cpu_count()
    if cpu_count is None:
        LOG_CLI.error("failed to get CPU cores count")
        return 1

    cpu_count -= 1  # adjust for zero-based indexing

    if args.analyze:
        plan_path = Path(args.analyze).parent / 'execution-plan.json'
        enhanced = plan_path.exists() and json.loads(plan_path.read_text(encoding='utf-8')).get('mode') == 'enhanced'
        display_results(args.analyze, winver.major >= 10, recommend_cores=not enhanced)
        if enhanced:
            import enhanced_results
            enhanced_results.analyze(Path(args.analyze).parent)
        return 0

    bd_start = None
    try:
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            "SYSTEM\\CurrentControlSet\\Services\\BasicDisplay",
            0,
            winreg.KEY_READ | winreg.KEY_WOW64_64KEY,
        ) as key:
            bd_start = winreg.QueryValueEx(key, "Start")[0]
    except FileNotFoundError:
        pass

    if bd_start is None:
        LOG_CLI.error("unable to get BasicDisplay start type")
        return 1

    if bd_start == 4:
        LOG_CLI.error(
            "enable the BasicDisplay driver to prevent issues with restarting the GPU driver",
        )
        return 1

    config_path = args.config if args.config is not None else "config.ini"
    cfg = Config(config_path)
    print(f'配置文件：{os.path.abspath(config_path)}（UTF-8，可用记事本修改）')
    if cfg.validate_config() != 0:
        return 1
    if cpu_count >= 64:
        raise ValueError('Systems with more than 64 logical CPUs need processor-group affinity support; this version will not guess a mask.')
    presentmon_version = "1.10.0" if winver.major >= 10 and winver.product_type != 3 else "1.6.0"
    presentmon_binary = f"PresentMon-{presentmon_version}-x64.exe"
    bundled = runtime.resources()
    api_binpaths = {Api.LIBLAVA: str(bundled / 'bin/liblava/lava-triangle.exe'),
                    Api.D3D9: str(bundled / 'bin/D3D9-benchmark.exe')}
    api_binpath = api_binpaths[cfg.settings.api]
    pm_binary = str(bundled / 'bin/PresentMon' / presentmon_binary)
    mode = args.mode or 'simple'
    if not args.mode and not args.non_interactive:
        print('请选择测试模式：\n  1. 简单版：按顺序单核心测试（时间短）\n  2. 增强版：三轮综合测试（时间长）')
        while True:
            choice = input('输入 1 或 2：').strip()
            if choice in ('1', '2'):
                mode = 'enhanced' if choice == '2' else 'simple'
                break
            print('请输入 1 或 2。')
    lock_benchmark()
    import background_apps
    background_apps.assist(args.non_interactive)
    session_directory = f"captures\\AutoGpuAffinity-{time.strftime('%d%m%y%H%M%S')}-{os.getpid()}"
    target_gpu = (gpu_selection.auto_select(gpus, cfg.settings.gpu, api_binpath, pm_binary,
                   os.path.join(session_directory, 'gpu-selection'))
                  if cfg.settings.api == Api.LIBLAVA else select_gpu(gpus, cfg.settings.gpu))
    hwids_gpu = [target_gpu.hwid]
    LOG_CLI.info('目标显卡：%s [%s]', target_gpu.name, target_gpu.hwid)

    if args.apply_affinity is not None:
        if not 0 <= args.apply_affinity <= cpu_count:
            LOG_CLI.error("invalid affinity specified %d", args.apply_affinity)
            return 1

        if args.non_interactive:
            raise ValueError('设备绑定必须交互确认，不支持无人值守应用。')
        import devices
        if input(f'将 {target_gpu.name} 绑定到 CPU {args.apply_affinity}。输入“确认绑定”执行：').strip() != '确认绑定':
            print('已取消，未修改绑定。')
            return 0
        backup_path = Path(session_directory) / 'manual-affinity-backup.json'
        devices.apply_transaction([{'id': target_gpu.hwid, 'cpus': [args.apply_affinity]}], backup_path)
        print(f'已写入并核验，建议立即手动重启电脑。恢复备份：{backup_path.resolve()}')
        return 0

    api_binname = os.path.basename(api_binpath)

    if cfg.settings.custom_cpus:
        # remove duplicates and sort
        benchmark_cpus = sorted(set(cfg.settings.custom_cpus))

        if not all(0 <= cpu <= cpu_count for cpu in benchmark_cpus):
            LOG_CLI.error("invalid cpus in custom_cpus array")
            return 1
    else:
        benchmark_cpus = list(range(cpu_count + 1))

    topology = recommendation.current_topology()
    execution_plan = benchmark_modes.schedule(benchmark_cpus, topology, mode)
    jobs = execution_plan['jobs']
    if mode == 'enhanced' and not any(job['phase'] == 'paired' for job in jobs):
        print('未发现可测的完整超线程组：第 2 轮跳过，其余两轮照常进行。')

    estimated_time_seconds = (
        10
        + cfg.settings.cache_duration
        + cfg.settings.benchmark_duration
        + (5 if cfg.msi_afterburner.profile > 0 else 0)
    ) * len(jobs)

    estimated_time = datetime.timedelta(seconds=estimated_time_seconds)
    finish_time = datetime.datetime.now() + estimated_time

    print(
        textwrap.dedent(
            f"""        会话目录：{session_directory}
        测试模式：{'增强版' if mode == 'enhanced' else '简单版'}
        预热时长（秒）：{cfg.settings.cache_duration}
        每次采集时长（秒）：{cfg.settings.benchmark_duration}
        测试核心：{"全部" if not cfg.settings.custom_cpus else ",".join([str(cpu) for cpu in benchmark_cpus])}
        计划测试次数：{len(jobs)}
        渲染程序：{os.path.splitext(api_binname)[0]}
        预计耗时：{estimated_time}
        预计结束时间：{finish_time.strftime("%H:%M:%S")}
        加载小飞机预设：{cfg.msi_afterburner.profile > 0}
        DPC/ISR 记录：{cfg.xperf.enabled}
        保存 ETL：{cfg.xperf.save_etls}
        同步进程亲和性：{cfg.settings.sync_driver_affinity}
        """,
        ),
    )

    if not cfg.settings.skip_confirmation and not args.non_interactive:
        input('按回车开始测试；测试期间会多次重启目标显卡驱动，请勿进行其他跑分。')

    width, height = (display.primary_resolution() if cfg.settings.auto_resolution
                     else (cfg.liblava.x_resolution, cfg.liblava.y_resolution))
    if width <= 0 or height <= 0:
        raise ValueError("Display dimensions must be positive")
    LOG_CLI.info('分辨率：%dx%d；强制全屏：%s', width, height, cfg.settings.force_fullscreen)
    refresh_rate = display.primary_refresh_rate()
    LOG_CLI.info('主显示器刷新率：%s Hz；全屏合成策略：%s',
                 refresh_rate, cfg.settings.fullscreen_composition)
    subject_args: list[str] = []
    if cfg.settings.api == Api.LIBLAVA:
        try:
            physical_device = match_vulkan(target_gpu, query_vulkan_adapters())
        except (OSError, RuntimeError) as e:
            LOG_CLI.warning('独立 Vulkan 查询失败：%s。改用渲染器直接探测。', e)
            physical_device = renderer.probe_device(api_binpath, target_gpu, len(gpus) + 2,
                                                    os.path.join(session_directory, "renderer-logs"))
        LOG_CLI.info('Vulkan 物理设备索引：%d', physical_device)
        # Borderless mode permits cross-adapter presentation on mining GPU drivers.
        subject_args = [
            f"--physical_device={physical_device}",
            f"--fullscreen={int(cfg.liblava.fullscreen and not cfg.settings.force_fullscreen)}",
            f"--width={width}", f"--height={height}",
            f"--fps_cap={cfg.liblava.fps_cap}",
            '--v_sync=0',
            f"--triple_buffering={int(cfg.liblava.triple_buffering)}",
        ]
    elif len(gpus) > 1:
        raise ValueError("D3D9 benchmark cannot select its rendering GPU. Use api=1 for multi-GPU systems.")

    # this will create all of the required folders
    os.makedirs(f"{session_directory}\\CSVs", exist_ok=True)
    with open(os.path.join(session_directory, 'cpu-topology.json'), 'w', encoding='utf-8') as report:
        json.dump(topology, report, indent=2, ensure_ascii=False)
    with open(os.path.join(session_directory, 'execution-plan.json'), 'w', encoding='utf-8') as report:
        json.dump(execution_plan, report, indent=2, ensure_ascii=False)

    # stop any existing trace sessions and processes
    if cfg.xperf.enabled:
        os.mkdir(f"{session_directory}\\xperf")

        try:
            subprocess.run(
                [cfg.xperf.location, "-stop"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=True,
            )
        except subprocess.CalledProcessError as e:
            # ignore if already stopped
            if e.returncode != 2147946601:
                LOG_CLI.exception(e)
                raise

    overlay = None
    subject = None
    original_affinity = snapshot_affinity(target_gpu.hwid)
    composition_mode = True if cfg.settings.fullscreen_composition == 'always' else (
        False if cfg.settings.fullscreen_composition == 'never' else None)
    completed_cpus = []
    completed_jobs = []
    session_state = {'status': 'running', 'requested_cpus': benchmark_cpus,
                     'target_pnp_id': target_gpu.hwid,
                     'mode': mode, 'requested_jobs': [job['id'] for job in jobs],
                     'target_gpu': target_gpu.name, 'refresh_rate': refresh_rate}
    status_path = os.path.join(session_directory, 'session-status.json')
    def save_status():
        session_state['completed_cpus'] = completed_cpus
        session_state['completed_jobs'] = completed_jobs
        session_state['fullscreen_composition'] = composition_mode
        with open(status_path, 'w', encoding='utf-8') as report:
            json.dump(session_state, report, ensure_ascii=False, indent=2)
    save_status()
    try:
        for job in jobs:
            members = job['cpus']
            cpu = members[0]
            label = job['artifact']
            csv_path = os.path.join(session_directory, job['directory'], job['label'] + '.csv')
            LOG_CLI.info('正在测试 [%s] CPU %s', {'normal': '第 1 轮普通', 'paired': '第 2 轮超线程组合', 'shuffled': '第 3 轮乱序'}[job['phase']], '+'.join(map(str, members)))

            if apply_affinity(hwids_gpu, members) != 0:
                LOG_CLI.error(f"failed to apply affinity to CPU {cpu}")
                return 1

            time.sleep(5)

            if (profile := cfg.msi_afterburner.profile) > 0:
                start_afterburner(cfg.msi_afterburner.location, profile)

            if cfg.settings.api == Api.LIBLAVA:
                # Re-enabling the device may precede Vulkan ICD recovery by many seconds.
                # Apply recovery and renderer fallback on every CPU, not just startup.
                try:
                    physical_device = wait_vulkan_device(target_gpu)
                except RuntimeError as error:
                    LOG_CLI.warning('%s；改用渲染器直接探测。', error)
                    physical_device = renderer.probe_device(
                        api_binpath, target_gpu, len(gpus) + 2,
                        os.path.join(session_directory, "renderer-logs", f"recovery-{label}"))
                subject_args[0] = f"--physical_device={physical_device}"
                # This binary buffers lava.log until normal exit. A short real
                # renderer probe verifies its selected GPU before timed capture.
                renderer.probe_device(api_binpath, target_gpu, len(gpus) + 2,
                    os.path.join(session_directory, 'renderer-logs', f'preflight-{label}'),
                    indices=[physical_device])
            log_directory = os.path.join(session_directory, "renderer-logs")
            os.makedirs(log_directory, exist_ok=True)
            if cfg.settings.api == Api.LIBLAVA:
                subject, log_path = renderer.launch(api_binpath, subject_args,
                    os.path.join(log_directory, label))
            else:
                log_path = os.path.join(log_directory, label + '.log')
                with open(log_path, 'wb', buffering=0) as render_log:
                    subject = subprocess.Popen([api_binpath, *subject_args],
                        stdout=render_log, stderr=subprocess.STDOUT)
            render_name = None
            if cfg.settings.api == Api.LIBLAVA:
                renderer.wait_window(subject)
            if cfg.settings.sync_driver_affinity:
                psutil.Process(subject.pid).cpu_affinity(members)
            if cfg.settings.force_fullscreen:
                display.force_fullscreen(subject, width, height)
                LOG_CLI.info('已核验无边框全屏范围：(0, 0, %d, %d)', width, height)
                if composition_mode and cfg.settings.api == Api.LIBLAVA:
                    overlay = presentation.StatusOverlay(subject.pid, f"AutoGpuAffinity - CPU {'+'.join(map(str, members))} / 合成模式")

            # 5s offset to allow subject to launch
            time.sleep(5 + cfg.settings.cache_duration)

            if subject.poll() is not None:
                raise RuntimeError(f"Benchmark exited during warmup. See {log_path}")

            if cfg.settings.api == Api.LIBLAVA and cfg.settings.force_fullscreen and composition_mode is None:
                preflight_directory = os.path.join(session_directory, 'presentation-preflight')
                initial = presentation.capture(pm_binary, subject, 3,
                    os.path.join(preflight_directory, 'direct.csv'))
                capped = cfg.liblava.fps_cap == 0 and presentation.refresh_paced(initial, refresh_rate)
                initial_report = {'direct': presentation.public_stats(initial), 'refresh_limit_suspected': capped}
                composition_mode = False
                if capped:
                    LOG_CLI.warning('全屏帧率被 %s Hz 刷新率限制（%.2f FPS）；正在验证状态条合成路径。',
                                    refresh_rate, initial['average_fps'])
                    overlay = presentation.StatusOverlay(subject.pid, f"AutoGpuAffinity - CPU {'+'.join(map(str, members))} / 合成模式")
                    time.sleep(1)
                    alternative = presentation.capture(pm_binary, subject, 3,
                        os.path.join(preflight_directory, 'composed.csv'))
                    overlay.check()
                    initial_report['composed'] = presentation.public_stats(alternative)
                    recovered = (not presentation.refresh_paced(alternative, refresh_rate)
                                 and alternative['average_fps'] > initial['average_fps'] * 1.10)
                    initial_report['recovery_verified'] = recovered
                    with open(os.path.join(preflight_directory, 'result.json'), 'w', encoding='utf-8') as report:
                        json.dump(initial_report, report, indent=2)
                    if not recovered:
                        raise RuntimeError('Fullscreen refresh pacing could not be removed. See presentation-preflight; this session will not be ranked.')
                    composition_mode = True
                    LOG_CLI.info('已核验全屏合成：%.2f → %.2f FPS；所有轮次统一使用此呈现模式。',
                                 initial['average_fps'], alternative['average_fps'])
                else:
                    with open(os.path.join(preflight_directory, 'result.json'), 'w', encoding='utf-8') as report:
                        json.dump(initial_report, report, indent=2)
                save_status()

            if cfg.xperf.enabled:
                subprocess.run(
                    [cfg.xperf.location, "-on", "base+interrupt+dpc"],
                    check=True,
                )

            if overlay:
                overlay.check()
            stats = presentation.capture(pm_binary, subject, cfg.settings.benchmark_duration,
                csv_path)
            if overlay:
                overlay.check()
            if (cfg.settings.api == Api.LIBLAVA and cfg.settings.force_fullscreen
                    and cfg.liblava.fps_cap == 0 and presentation.refresh_paced(stats, refresh_rate)):
                raise RuntimeError(f'CPU {cpu} capture is refresh-paced; session will not be ranked. See CSV and native logs.')

            if subject.poll() is not None:
                raise RuntimeError(f"Benchmark exited during capture. See {log_path}")
            if os.path.exists(csv_path):
                with open(csv_path, encoding="utf-8-sig") as capture:
                    frame_rows = list(csv.DictReader(capture))
                valid_frames = [row for row in frame_rows if any(
                    key and key.lower() == "msbetweenpresents" and value and float(value) > 0
                    for key, value in row.items())]
                if len(valid_frames) < 2:
                    raise RuntimeError(f"Capture contains fewer than two valid frames: {csv_path}")
                process_affinity = psutil.Process(subject.pid).cpu_affinity()
                if cfg.settings.api == Api.LIBLAVA:
                    renderer.stop(subject)
                    render_name = renderer.verify_log(log_path, target_gpu)
                    LOG_CLI.info('实测渲染显卡：%s（进程 PID %d）', render_name, subject.pid)
                if overlay:
                    overlay.close()
                    overlay = None
                evidence = {
                    'cpus': members, 'job_id': job['id'], 'phase': job['phase'],
                    'cpu': cpu, 'target_name': target_gpu.name, 'target_pnp_id': target_gpu.hwid,
                    'renderer_name': render_name, 'renderer_pid': subject.pid,
                    'vulkan_device_index': physical_device if cfg.settings.api == Api.LIBLAVA else None,
                    'resolution': [width, height], 'forced_fullscreen': cfg.settings.force_fullscreen,
                    'process_affinity': process_affinity,
                    'capture_seconds': cfg.settings.benchmark_duration,
                    'valid_present_frames': len(valid_frames), 'csv_path': os.path.abspath(csv_path),
                    'refresh_rate': refresh_rate, 'fullscreen_composition': composition_mode,
                    'presentation': presentation.public_stats(stats),
                }
                policy_path = f'SYSTEM\\CurrentControlSet\\Enum\\{target_gpu.hwid}\\Device Parameters\\Interrupt Management\\Affinity Policy'
                with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, policy_path) as key:
                    evidence['device_policy'] = winreg.QueryValueEx(key, 'DevicePolicy')[0]
                    evidence['assignment_mask_hex'] = winreg.QueryValueEx(key, 'AssignmentSetOverride')[0].hex()
                with open(os.path.join(session_directory, label + '-validation.json'), 'w', encoding='utf-8') as report:
                    json.dump(evidence, report, ensure_ascii=False, indent=2)
                LOG_CLI.info('采集了 %d 个有效呈现帧，进程 PID %d', len(valid_frames), subject.pid)
                if job['phase'] == 'normal':
                    completed_cpus.append(cpu)
                completed_jobs.append(job['id'])
                save_status()

            if not os.path.exists(csv_path):
                LOG_CLI.error(
                    "csv log unsuccessful, this may be due to a missing dependency or windows component",
                )
                return 1

            if cfg.xperf.enabled:
                subprocess.run(
                    [
                        cfg.xperf.location,
                        "-d",
                        f"{session_directory}\\xperf\\{label}.etl",
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=True,
                )

                try:
                    subprocess.run(
                        [
                            cfg.xperf.location,
                            "-quiet",
                            "-i",
                            f"{session_directory}\\xperf\\{label}.etl",
                            "-o",
                            f"{session_directory}\\xperf\\{label}.txt",
                            "-a",
                            "dpcisr",
                        ],
                        check=True,
                    )
                except subprocess.CalledProcessError:
                    LOG_CLI.error("unable to generate dpcisr report")
                    return 1

                if not cfg.xperf.save_etls:
                    os.remove(f"{session_directory}\\xperf\\{label}.etl")

            if subject is not None and subject.poll() is None:
                renderer.stop(subject)

    except BaseException as error:
        session_state['status'] = 'failed'
        session_state['error'] = str(error) or type(error).__name__
        save_status()
        raise
    finally:
        try:
            if overlay:
                overlay.close()
            if subject is not None and subject.poll() is None:
                try:
                    renderer.stop(subject)
                except RuntimeError:
                    LOG_CLI.exception('Failed to close renderer normally')
            if cfg.xperf.enabled:
                subprocess.run([cfg.xperf.location, "-stop"], check=False,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        finally:
            try:
                restore_affinity(target_gpu.hwid, original_affinity)
                if cfg.settings.api == Api.LIBLAVA:
                    wait_vulkan_device(target_gpu)
                LOG_CLI.info('已恢复目标显卡原亲和性，并核验驱动/Vulkan 恢复。')
            except BaseException as error:
                session_state['status'] = 'failed'
                session_state['cleanup_error'] = str(error) or type(error).__name__
                save_status()
                raise
            if completed_jobs != session_state['requested_jobs']:
                session_state['status'] = 'failed'
                session_state.setdefault('error', 'Not every requested CPU completed')
                save_status()

    if completed_jobs != session_state['requested_jobs']:
        session_state['status'] = 'failed'
        save_status()
        raise RuntimeError('Session did not finish every requested CPU')
    session_state['status'] = 'complete'
    session_state['driver_recovery_verified'] = True
    save_status()

    print()  # new line
    display_results(f"{session_directory}\\CSVs", winver.major >= 10, recommend_cores=mode != 'enhanced')
    if mode == 'enhanced':
        import enhanced_results
        enhanced_results.analyze(session_directory)
    if not args.non_interactive:
        import binding
        binding.confirm_binding(session_directory, False)

    return 0


def _main() -> NoReturn:
    exit_code = 0

    try:
        exit_code = main()
    except KeyboardInterrupt:
        LOG_CLI.warning('用户中止操作。')
        exit_code = 1
    except EOFError:
        LOG_CLI.warning('输入已结束，操作取消；需要无人值守测试时请使用 --non-interactive。')
        exit_code = 1
    except SystemExit as error:
        exit_code = error.code if isinstance(error.code, int) else 1
    except (RuntimeError, ValueError, OSError) as e:
        LOG_CLI.error("%s", e)
        exit_code = 1
    except Exception:
        print(traceback.format_exc())
        exit_code = 1
    finally:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        if INSTANCE_GUARD is not None:
            kernel32.CloseHandle.argtypes = [ctypes.wintypes.HANDLE]
            kernel32.CloseHandle(INSTANCE_GUARD)
        process_array = (ctypes.c_uint * 1)()
        num_processes = kernel32.GetConsoleProcessList(process_array, 1)

        # only pause if script was ran by double-clicking
        if num_processes < 3 and sys.stdin.isatty() and '--non-interactive' not in sys.argv:
            try:
                input('按回车退出')
            except (KeyboardInterrupt, EOFError):
                pass

        sys.exit(exit_code)


if __name__ == "__main__":
    # Dispatch before elevation/config/WMI and the interactive exit pause.
    if sys.argv[1:] == ['--vulkan-query']:
        sys.exit(vulkan_query_worker())
    if len(sys.argv) == 5 and sys.argv[1] == '--presentation-overlay':
        sys.exit(presentation.overlay_worker(int(sys.argv[2]), int(sys.argv[3]), sys.argv[4]))
    _main()
