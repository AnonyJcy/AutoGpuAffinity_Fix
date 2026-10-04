"""Topology-aware recommendations; GPU captures are proxies, not HID/HPET tests."""
import ctypes
import json
import math
from pathlib import Path
import platform
import struct
import winreg


def parse_topology(data, pointer_size=8):
    if pointer_size != 8:
        raise RuntimeError('Recommendations require native 64-bit Windows.')
    cores = []
    offset = 0
    while offset < len(data):
        if len(data) - offset < 8:
            raise ValueError('Truncated processor topology')
        relation, size = struct.unpack_from('<II', data, offset)
        if size < 8 or offset + size > len(data):
            raise ValueError('Invalid processor topology record')
        if relation == 0:
            if size < 48:
                raise ValueError('Truncated processor core record')
            flags, efficiency = struct.unpack_from('<BB', data, offset + 8)
            count = struct.unpack_from('<H', data, offset + 30)[0]
            if count != 1:
                raise ValueError('Unsupported processor core group count')
            mask, group = struct.unpack_from('<QH', data, offset + 32)
            cpus = [bit for bit in range(64) if mask & (1 << bit)]
            if not cpus:
                raise ValueError('Empty physical core mask')
            cores.append({'group': group, 'cpus': cpus, 'smt': bool(flags & 1),
                          'efficiency_class': efficiency})
        offset += size
    if not cores:
        raise ValueError('No physical cores reported')
    return cores


def current_topology():
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    query = kernel.GetLogicalProcessorInformationEx
    query.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
    query.restype = ctypes.c_int
    length = ctypes.c_ulong()
    if query(0, None, ctypes.byref(length)) or ctypes.get_last_error() != 122:
        raise ctypes.WinError(ctypes.get_last_error())
    buffer = ctypes.create_string_buffer(length.value)
    if not query(0, buffer, ctypes.byref(length)):
        raise ctypes.WinError(ctypes.get_last_error())
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r'HARDWARE\DESCRIPTION\System\CentralProcessor\0') as key:
        model = winreg.QueryValueEx(key, 'ProcessorNameString')[0].strip()
    return {'machine': platform.node(), 'processor': model,
            'cores': parse_topology(buffer.raw[:length.value], ctypes.sizeof(ctypes.c_void_p))}


def recommend(results, frames, topology, paired_results=None):
    cores = topology['cores']
    if any(core['group'] != 0 for core in cores):
        raise ValueError('Multiple processor groups are not supported by this benchmark; no recommendation issued.')
    seen = [cpu for core in cores for cpu in core['cpus']]
    if len(seen) != len(set(seen)) or any(int(cpu) not in seen for cpu in results):
        raise ValueError('Capture CPU numbers do not match processor topology.')
    smt = any(core['smt'] or len(core['cpus']) > 1 for core in cores)
    candidates, excluded = [], []
    for core in sorted(cores, key=lambda item: min(item['cpus'])):
        cpus = sorted(core['cpus'])
        reason = None
        if 0 in cpus:
            reason = 'CPU 0 physical core excluded by user policy'
        elif smt and (len(cpus) != 2 or cpus[0] % 2 or cpus[1] != cpus[0] + 1):
            reason = 'Not an even-start adjacent physical SMT pair'
        elif not smt and len(cpus) != 1:
            reason = 'Unsupported physical core topology'
        elif any(str(cpu) not in results for cpu in cpus):
            reason = 'Physical core has unmeasured logical processors'
        if reason:
            excluded.append({'cpus': cpus, 'reason': reason})
            continue
        rows = [results[str(cpu)] for cpu in cpus]
        if any(not math.isfinite(value) for row in rows for value in row.values()):
            raise ValueError('Nonfinite benchmark metric')
        candidates.append({'cpus': cpus, 'mask_hex': f'0x{sum(1 << cpu for cpu in cpus):X}',
                           'efficiency_class': core['efficiency_class'],
                           'metrics': {key: min(row[key] for row in rows)
                                       for key in ('maximum', 'average', 'minimum', 'lows1')},
                           'stdev': max(abs(row['stdev']) for row in rows),
                           'frames_min': min(frames[str(cpu)] for cpu in cpus)})
        label = '+'.join(map(str, cpus))
        if paired_results is not None and len(cpus) == 2:
            pair = paired_results.get(label)
            if pair is None:
                candidates.pop()
                excluded.append({'cpus': cpus, 'reason': '增强模式缺少本组联合实测数据'})
                continue
            # Compare joint masks against both sequential and shuffled single-thread runs.
            candidate = candidates[-1]
            for key in candidate['metrics']:
                candidate['metrics'][key] = min(candidate['metrics'][key], pair['metrics'][key])
            candidate['stdev'] = max(candidate['stdev'], abs(pair['metrics']['stdev']))
            candidate['frames_min'] = min(candidate['frames_min'], pair['frames'])
            candidate['joint_mask_measured'] = True
    # Relative ranks give Max/Avg/Min equal influence despite different scales.
    # Ties receive identical scores; the weaker sibling determines pair metrics.
    def rank(value, values):
        return (sum(other < value for other in values) +
                0.5 * sum(other == value for other in values)) / len(values)
    for candidate in candidates:
        candidate['scores'] = {
            'gpu': sum(rank(candidate['metrics'][key], [c['metrics'][key] for c in candidates])
                       for key in ('maximum', 'average', 'minimum')) / 3,
            'mouse_keyboard': rank(candidate['metrics']['lows1'], [c['metrics']['lows1'] for c in candidates]),
            'timer': rank(-candidate['stdev'], [-c['stdev'] for c in candidates]),
        }
    selected, used = {}, set()
    for role in ('gpu', 'mouse_keyboard', 'timer'):
        available = [c for c in candidates if c['cpus'][0] not in used and
                     (role != 'mouse_keyboard' or c['frames_min'] >= 500)]
        if available:
            choice = max(available, key=lambda c: (c['scores'][role], -c['cpus'][0]))
            selected[role] = choice
            used.add(choice['cpus'][0])
        else:
            selected[role] = None
    return {'smt': smt, 'measured_logical_cpus': sorted(int(cpu) for cpu in results),
            'all_logical_cpus_measured': set(map(int, results)) == set(seen),
            'selection_policy': 'distinct physical cores, GPU then HID then timer',
            'recommended': selected, 'candidates': candidates, 'excluded': excluded,
            'limitations': ['GPU-only captures: HID Low FPS and timer STDEV are heuristics, not device latency tests.',
                           ('Sibling pair masks and shuffled single-thread runs were measured; device-specific validation is still needed.'
                            if paired_results is not None else 'Sibling pair masks were not benchmarked together; repeat runs and device-specific validation are needed.'),
                           'No affinity settings or HPET/QPC boot settings are changed.',
                           'HID uses existing 1% Low definition; at least 500 frames per logical CPU required.']}


def display_recommendations(directory, results, frames):
    session = Path(directory).resolve().parent
    saved = session / 'cpu-topology.json'
    output = Path(directory) / 'core-recommendations.json'
    try:
        topology = current_topology()
        if saved.exists():
            recorded = json.loads(saved.read_text(encoding='utf-8'))
            if recorded != topology:
                raise ValueError('CPU topology or machine differs from the recorded benchmark; rerun on this machine.')
            provenance = 'Topology recorded with benchmark and verified against current Windows topology.'
        else:
            provenance = 'Legacy captures: current topology assumed; same-machine origin is not verified.'
        report = recommend(results, frames, topology)
        report.update({'status': 'heuristic', 'topology': topology, 'provenance': provenance,
                       'topology_recorded_with_capture': saved.exists()})
    except (OSError, ValueError, RuntimeError, KeyError) as error:
        output.write_text(json.dumps({'status': 'unavailable', 'reason': str(error)}), encoding='utf-8')
        print(f'无法推荐绑定核心：{error}')
        return
    # Always recompute; never leave a stale successful recommendation after refusal.
    output.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding='utf-8')
    print('绑定核心候选（根据显卡跑分推导，尚未应用）：')
    print('已核对跑分时记录的 CPU 拓扑。' if saved.exists() else '旧记录未保存 CPU 拓扑：假设来自本机，来源尚未核实。')
    if not report['all_logical_cpus_measured']:
        print('仅测试部分核心，不能据此判定全处理器最优核心。')
    names = {'gpu': '显卡', 'mouse_keyboard': '鼠标键盘', 'timer': '系统/高精度计时器'}
    for role, choice in report['recommended'].items():
        if choice:
            print(f"  {names[role]}：CPU {','.join(map(str, choice['cpus']))}；掩码 {choice['mask_hex']}；评分 {choice['scores'][role]:.3f}")
        else:
            print(f'  {names[role]}：合格物理核心或样本不足，未推荐')
    print('依次为显卡、鼠标键盘、计时器选择不同物理核心。')
    print('鼠标键盘 Low 帧和计时器标准差来自显卡跑分，不代表已验证设备延迟。\n简单模式未联合测试双线程组合，建议复测。\n未修改设备或 HPET/QPC 启动设置；鼠标键盘至少需要每核心 500 个有效帧。')
    print(f'推荐详情：{output}')
