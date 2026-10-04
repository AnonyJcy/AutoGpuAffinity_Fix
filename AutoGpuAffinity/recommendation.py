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
            cpus = [bit for bit in range(64) if mask & 1 << bit]
            if not cpus:
                raise ValueError('Empty physical core mask')
            cores.append({'group': group, 'cpus': cpus, 'smt': bool(flags & 1), 'efficiency_class': efficiency})
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
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, 'HARDWARE\\DESCRIPTION\\System\\CentralProcessor\\0') as key:
        model = winreg.QueryValueEx(key, 'ProcessorNameString')[0].strip()
    return {'machine': platform.node(), 'processor': model, 'cores': parse_topology(buffer.raw[:length.value], ctypes.sizeof(ctypes.c_void_p))}

def recommend(results, frames, topology):
    cores = topology['cores']
    if any((core['group'] != 0 for core in cores)):
        raise ValueError('Multiple processor groups are not supported by this benchmark; no recommendation issued.')
    seen = [cpu for core in cores for cpu in core['cpus']]
    if len(seen) != len(set(seen)) or any((int(cpu) not in seen for cpu in results)):
        raise ValueError('Capture CPU numbers do not match processor topology.')
    smt = any((core['smt'] or len(core['cpus']) > 1 for core in cores))
    candidates, excluded = ([], [])
    for core in sorted(cores, key=lambda item: min(item['cpus'])):
        cpus = sorted(core['cpus'])
        reason = None
        if 0 in cpus:
            reason = 'CPU 0 physical core excluded by user policy'
        elif smt and (len(cpus) != 2 or cpus[0] % 2 or cpus[1] != cpus[0] + 1):
            reason = 'Not an even-start adjacent physical SMT pair'
        elif not smt and len(cpus) != 1:
            reason = 'Unsupported physical core topology'
        elif any((str(cpu) not in results for cpu in cpus)):
            reason = 'Physical core has unmeasured logical processors'
        if reason:
            excluded.append({'cpus': cpus, 'reason': reason})
            continue
        rows = [results[str(cpu)] for cpu in cpus]
        if any((not math.isfinite(value) for row in rows for value in row.values())):
            raise ValueError('Nonfinite benchmark metric')
        candidates.append({'cpus': cpus, 'mask_hex': f'0x{sum((1 << cpu for cpu in cpus)):X}', 'efficiency_class': core['efficiency_class'], 'metrics': {key: min((row[key] for row in rows)) for key in ('maximum', 'average', 'minimum', 'lows1')}, 'stdev': max((abs(row['stdev']) for row in rows)), 'frames_min': min((frames[str(cpu)] for cpu in cpus))})

    def rank(value, values):
        return (sum((other < value for other in values)) + 0.5 * sum((other == value for other in values))) / len(values)
    for candidate in candidates:
        candidate['scores'] = {'gpu': sum((rank(candidate['metrics'][key], [c['metrics'][key] for c in candidates]) for key in ('maximum', 'average', 'minimum'))) / 3, 'mouse_keyboard': rank(candidate['metrics']['lows1'], [c['metrics']['lows1'] for c in candidates]), 'timer': rank(-candidate['stdev'], [-c['stdev'] for c in candidates])}
    selected, used = ({}, set())
    for role in ('gpu', 'mouse_keyboard', 'timer'):
        available = [c for c in candidates if c['cpus'][0] not in used and (role != 'mouse_keyboard' or c['frames_min'] >= 500)]
        if available:
            choice = max(available, key=lambda c: (c['scores'][role], -c['cpus'][0]))
            selected[role] = choice
            used.add(choice['cpus'][0])
        else:
            selected[role] = None
    return {'smt': smt, 'measured_logical_cpus': sorted((int(cpu) for cpu in results)), 'all_logical_cpus_measured': set(map(int, results)) == set(seen), 'selection_policy': 'distinct physical cores, GPU then HID then timer', 'recommended': selected, 'candidates': candidates, 'excluded': excluded, 'limitations': ['GPU-only captures: HID Low FPS and timer STDEV are heuristics, not device latency tests.', 'Sibling pair masks were not benchmarked together; repeat runs and device-specific validation are needed.', 'No affinity settings or HPET/QPC boot settings are changed.', 'HID uses existing 1% Low definition; at least 500 frames per logical CPU required.']}

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
        report.update({'status': 'heuristic', 'topology': topology, 'provenance': provenance, 'topology_recorded_with_capture': saved.exists()})
    except (OSError, ValueError, RuntimeError, KeyError) as error:
        output.write_text(json.dumps({'status': 'unavailable', 'reason': str(error)}), encoding='utf-8')
        print(f'Core recommendations unavailable: {error}')
        return
    output.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding='utf-8')
    print('Core recommendations (GPU benchmark heuristics; not applied):')
    print(provenance)
    if not report['all_logical_cpus_measured']:
        print('Partial CPU coverage: these candidates cannot establish the best core across the entire CPU.')
    for role, choice in report['recommended'].items():
        if choice:
            print(f"  {role}: CPU {','.join(map(str, choice['cpus']))}; mask {choice['mask_hex']}; score {choice['scores'][role]:.3f}")
        else:
            print(f'  {role}: insufficient eligible measured physical cores / samples')
    print('Distinct cores selected in GPU -> mouse/keyboard -> timer priority order.')
    for note in report['limitations']:
        print(f'  {note}')
    print(f'Recommendation details: {output}')
