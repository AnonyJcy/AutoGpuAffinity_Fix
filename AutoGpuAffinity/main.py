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

        if apply and cpu > -1:
            mask = 1 << cpu
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
        "Max",
        "Avg",
        "Min",
        "STDEV",
        "1 %ile",
        "0.1 %ile",
        "0.01 %ile",
        "0.005 %ile",
        "1% Low",
        "0.1% Low",
        "0.01% Low",
        "0.005% Low",
    ):
        print(f"{metric:<12}", end="")

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


def display_results(csv_directory: str, enable_color: bool) -> None:
    status_path = os.path.join(os.path.dirname(os.path.abspath(csv_directory)), 'session-status.json')
    if os.path.exists(status_path):
        with open(status_path, encoding='utf-8') as report:
            status = json.load(report)
        if status.get('status') != 'complete':
            raise RuntimeError('This session failed or is incomplete; refusing to rank its captures. See session-status.json.')
    results: dict[str, dict[str, float]] = {}

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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--version",
        action="version",
        version=f"AutoGpuAffinity v{consts.VERSION}",
    )
    parser.add_argument(
        "--config",
        metavar="<config>",
        type=str,
        help="path to config file",
    )
    parser.add_argument(
        "--analyze",
        metavar="<csv directory>",
        type=str,
        help="analyze csv files from a previous benchmark",
    )
    parser.add_argument(
        "--apply-affinity",
        metavar="<cpu>",
        type=int,
        help="assign a single core affinity to graphics drivers",
    )
    parser.add_argument('--non-interactive', action='store_true',
                        help='skip start and exit prompts for unattended validation')

    return parser.parse_args()


def is_admin() -> bool:
    return ctypes.windll.shell32.IsUserAnAdmin()


def kill_processes(*targets: str) -> None:
    targets_set = set(targets)

    for process in psutil.process_iter():
        if process.name().lower() in targets_set:
            process.kill()


def main() -> int:
    logging.basicConfig(format="[%(name)s] %(levelname)s: %(message)s", level=logging.INFO)

    print(
        f"AutoGpuAffinity Version {consts.VERSION} - GPLv3\nGitHub - https://github.com/valleyofdoom\n",
    )

    if not is_admin():
        LOG_CLI.error("administrator privileges required")
        return 1

    full_program_dir = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) else os.path.dirname(__file__)
    os.chdir(full_program_dir)

    args = parse_args()

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
        display_results(args.analyze, winver.major >= 10)
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
    if cfg.validate_config() != 0:
        return 1
    if cpu_count >= 64:
        raise ValueError('Systems with more than 64 logical CPUs need processor-group affinity support; this version will not guess a mask.')
    target_gpu = select_gpu(gpus, cfg.settings.gpu)
    hwids_gpu = [target_gpu.hwid]
    LOG_CLI.info("Target GPU: %s [%s]", target_gpu.name, target_gpu.hwid)
    lock_benchmark()

    if args.apply_affinity is not None:
        if not 0 <= args.apply_affinity <= cpu_count:
            LOG_CLI.error("invalid affinity specified %d", args.apply_affinity)
            return 1

        if apply_affinity(hwids_gpu, args.apply_affinity) != 0:
            LOG_CLI.error(f"failed to apply affinity to CPU {args.apply_affinity}")
            return 1

        LOG_CLI.info("set gpu driver affinity to: CPU %d", args.apply_affinity)
        return 0

    presentmon_version = "1.10.0" if winver.major >= 10 and winver.product_type != 3 else "1.6.0"
    presentmon_binary = f"PresentMon-{presentmon_version}-x64.exe"

    api_binpaths: dict[Api, str] = {
        Api.LIBLAVA: "bin\\liblava\\lava-triangle.exe",
        Api.D3D9: "bin\\D3D9-benchmark.exe",
    }

    api_binpath = api_binpaths[cfg.settings.api]
    api_binname = os.path.basename(api_binpath)

    if cfg.settings.custom_cpus:
        # remove duplicates and sort
        benchmark_cpus = sorted(set(cfg.settings.custom_cpus))

        if not all(0 <= cpu <= cpu_count for cpu in benchmark_cpus):
            LOG_CLI.error("invalid cpus in custom_cpus array")
            return 1
    else:
        benchmark_cpus = list(range(cpu_count + 1))

    session_directory = f"captures\\AutoGpuAffinity-{time.strftime('%d%m%y%H%M%S')}"

    estimated_time_seconds = (
        10
        + cfg.settings.cache_duration
        + cfg.settings.benchmark_duration
        + (5 if cfg.msi_afterburner.profile > 0 else 0)
    ) * len(benchmark_cpus)

    estimated_time = datetime.timedelta(seconds=estimated_time_seconds)
    finish_time = datetime.datetime.now() + estimated_time

    print(
        textwrap.dedent(
            f"""        Session Directory        {session_directory}
        Cache Duration           {cfg.settings.cache_duration}
        Benchmark Duration       {cfg.settings.benchmark_duration}
        Benchmark CPUs           {"All" if not cfg.settings.custom_cpus else ",".join([str(cpu) for cpu in benchmark_cpus])}
        Subject                  {os.path.splitext(api_binname)[0]}
        Estimated Time           {estimated_time}
        Estimated End Time       {finish_time.strftime("%H:%M:%S")}
        Load Afterburner         {cfg.msi_afterburner.profile > 0}
        DPC/ISR Logging          {cfg.xperf.enabled}
        Save ETLs                {cfg.xperf.save_etls}
        Sync Affinity            {cfg.settings.sync_driver_affinity}
        """,
        ),
    )

    if not cfg.settings.skip_confirmation and not args.non_interactive:
        input("press enter to start benchmarking...")

    width, height = (display.primary_resolution() if cfg.settings.auto_resolution
                     else (cfg.liblava.x_resolution, cfg.liblava.y_resolution))
    if width <= 0 or height <= 0:
        raise ValueError("Display dimensions must be positive")
    LOG_CLI.info("Resolution: %dx%d; forced fullscreen: %s", width, height, cfg.settings.force_fullscreen)
    refresh_rate = display.primary_refresh_rate()
    LOG_CLI.info('Primary display refresh: %s Hz; fullscreen composition: %s',
                 refresh_rate, cfg.settings.fullscreen_composition)
    subject_args: list[str] = []
    if cfg.settings.api == Api.LIBLAVA:
        try:
            physical_device = match_vulkan(target_gpu, query_vulkan_adapters())
        except (OSError, RuntimeError) as e:
            LOG_CLI.warning("Standalone Vulkan query failed: %s. Probing renderer directly.", e)
            physical_device = renderer.probe_device(api_binpath, target_gpu, len(gpus) + 2,
                                                    os.path.join(session_directory, "renderer-logs"))
        LOG_CLI.info("Vulkan physical device index: %d", physical_device)
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
    session_state = {'status': 'running', 'requested_cpus': benchmark_cpus,
                     'target_gpu': target_gpu.name, 'refresh_rate': refresh_rate}
    status_path = os.path.join(session_directory, 'session-status.json')
    def save_status():
        session_state['completed_cpus'] = completed_cpus
        session_state['fullscreen_composition'] = composition_mode
        with open(status_path, 'w', encoding='utf-8') as report:
            json.dump(session_state, report, ensure_ascii=False, indent=2)
    save_status()
    try:
        for cpu in benchmark_cpus:
            LOG_CLI.info("benchmarking CPU %d", cpu)

            if apply_affinity(hwids_gpu, cpu) != 0:
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
                    LOG_CLI.warning("%s. Trying the renderer directly.", error)
                    physical_device = renderer.probe_device(
                        api_binpath, target_gpu, len(gpus) + 2,
                        os.path.join(session_directory, "renderer-logs", f"recovery-CPU-{cpu}"))
                subject_args[0] = f"--physical_device={physical_device}"
                # This binary buffers lava.log until normal exit. A short real
                # renderer probe verifies its selected GPU before timed capture.
                renderer.probe_device(api_binpath, target_gpu, len(gpus) + 2,
                    os.path.join(session_directory, 'renderer-logs', f'preflight-CPU-{cpu}'),
                    indices=[physical_device])
            log_directory = os.path.join(session_directory, "renderer-logs")
            os.makedirs(log_directory, exist_ok=True)
            if cfg.settings.api == Api.LIBLAVA:
                subject, log_path = renderer.launch(api_binpath, subject_args,
                    os.path.join(log_directory, f'CPU-{cpu}'))
            else:
                log_path = os.path.join(log_directory, f'CPU-{cpu}.log')
                with open(log_path, 'wb', buffering=0) as render_log:
                    subject = subprocess.Popen([api_binpath, *subject_args],
                        stdout=render_log, stderr=subprocess.STDOUT)
            render_name = None
            if cfg.settings.api == Api.LIBLAVA:
                renderer.wait_window(subject)
            if cfg.settings.sync_driver_affinity:
                psutil.Process(subject.pid).cpu_affinity([cpu])
            if cfg.settings.force_fullscreen:
                display.force_fullscreen(subject, width, height)
                LOG_CLI.info('Verified borderless fullscreen bounds: (0, 0, %d, %d)', width, height)
                if composition_mode and cfg.settings.api == Api.LIBLAVA:
                    overlay = presentation.StatusOverlay(subject.pid, f'AutoGpuAffinity - CPU {cpu} / composed')

            # 5s offset to allow subject to launch
            time.sleep(5 + cfg.settings.cache_duration)

            if subject.poll() is not None:
                raise RuntimeError(f"Benchmark exited during warmup. See {log_path}")

            pm_binary = f'bin\\PresentMon\\{presentmon_binary}'
            if cfg.settings.api == Api.LIBLAVA and cfg.settings.force_fullscreen and composition_mode is None:
                preflight_directory = os.path.join(session_directory, 'presentation-preflight')
                initial = presentation.capture(pm_binary, subject, 3,
                    os.path.join(preflight_directory, 'direct.csv'))
                capped = cfg.liblava.fps_cap == 0 and presentation.refresh_paced(initial, refresh_rate)
                initial_report = {'direct': presentation.public_stats(initial), 'refresh_limit_suspected': capped}
                composition_mode = False
                if capped:
                    LOG_CLI.warning('Fullscreen trace is paced at %s Hz (%.2f FPS). Testing the status-overlay composition path.',
                                    refresh_rate, initial['average_fps'])
                    overlay = presentation.StatusOverlay(subject.pid, f'AutoGpuAffinity - CPU {cpu} / composed')
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
                    LOG_CLI.info('Fullscreen composition verified: %.2f -> %.2f FPS. Keeping this presentation mode for all CPUs.',
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
                f'{session_directory}\\CSVs\\CPU-{cpu}.csv')
            if overlay:
                overlay.check()
            if (cfg.settings.api == Api.LIBLAVA and cfg.settings.force_fullscreen
                    and cfg.liblava.fps_cap == 0 and presentation.refresh_paced(stats, refresh_rate)):
                raise RuntimeError(f'CPU {cpu} capture is refresh-paced; session will not be ranked. See CSV and native logs.')

            if subject.poll() is not None:
                raise RuntimeError(f"Benchmark exited during capture. See {log_path}")
            csv_path = f"{session_directory}\\CSVs\\CPU-{cpu}.csv"
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
                    LOG_CLI.info('Actual capture renderer GPU: %s (PID %d)', render_name, subject.pid)
                if overlay:
                    overlay.close()
                    overlay = None
                evidence = {
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
                with open(os.path.join(session_directory, f'CPU-{cpu}-validation.json'), 'w', encoding='utf-8') as report:
                    json.dump(evidence, report, ensure_ascii=False, indent=2)
                LOG_CLI.info('Captured %d valid present frames from PID %d', len(valid_frames), subject.pid)
                completed_cpus.append(cpu)
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
                        f"{session_directory}\\xperf\\CPU-{cpu}.etl",
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
                            f"{session_directory}\\xperf\\CPU-{cpu}.etl",
                            "-o",
                            f"{session_directory}\\xperf\\CPU-{cpu}.txt",
                            "-a",
                            "dpcisr",
                        ],
                        check=True,
                    )
                except subprocess.CalledProcessError:
                    LOG_CLI.error("unable to generate dpcisr report")
                    return 1

                if not cfg.xperf.save_etls:
                    os.remove(f"{session_directory}\\xperf\\CPU-{cpu}.etl")

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
                LOG_CLI.info('Original target GPU affinity restored; driver recovery verified')
            except BaseException as error:
                session_state['status'] = 'failed'
                session_state['cleanup_error'] = str(error) or type(error).__name__
                save_status()
                raise
            if completed_cpus != benchmark_cpus:
                session_state['status'] = 'failed'
                session_state.setdefault('error', 'Not every requested CPU completed')
                save_status()

    if completed_cpus != benchmark_cpus:
        session_state['status'] = 'failed'
        save_status()
        raise RuntimeError('Session did not finish every requested CPU')
    session_state['status'] = 'complete'
    session_state['driver_recovery_verified'] = True
    save_status()

    if os.path.exists("C:\\kernel.etl"):
        os.remove("C:\\kernel.etl")

    print()  # new line
    display_results(f"{session_directory}\\CSVs", winver.major >= 10)

    return 0


def _main() -> NoReturn:
    exit_code = 0

    try:
        exit_code = main()
    except KeyboardInterrupt:
        sys.exit(1)
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
            input("press enter to exit")

        sys.exit(exit_code)


if __name__ == "__main__":
    # Dispatch before elevation/config/WMI and the interactive exit pause.
    if sys.argv[1:] == ['--vulkan-query']:
        sys.exit(vulkan_query_worker())
    if len(sys.argv) == 5 and sys.argv[1] == '--presentation-overlay':
        sys.exit(presentation.overlay_worker(int(sys.argv[2]), int(sys.argv[3]), sys.argv[4]))
    _main()
