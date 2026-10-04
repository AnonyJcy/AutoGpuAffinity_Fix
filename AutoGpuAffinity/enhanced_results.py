"""Combine every completed real round, with separate pair and shuffled tables."""
import csv
import json
import math
from pathlib import Path
import framerate
import recommendation


def metrics(path):
    frames = []
    with Path(path).open(encoding='utf-8-sig') as source:
        for row in csv.DictReader(source):
            row = {key.lower(): value for key, value in row.items()}
            value = float(row['msbetweenpresents'])
            if not math.isfinite(value) or value < 0:
                raise ValueError(f'无效帧时间：{path}')
            if value:
                frames.append(value)
    if len(frames) < 2:
        raise ValueError(f'有效帧不足：{path}')
    fps = framerate.Fps(frames)
    return {'metrics': {'maximum': fps.maximum(), 'average': fps.average(), 'minimum': fps.minimum(),
                         'stdev': fps.stdev(), 'lows1': fps.lows(1)}, 'frames': len(frames)}


def analyze(session):
    session = Path(session).resolve()
    state = json.loads((session / 'session-status.json').read_text(encoding='utf-8'))
    plan = json.loads((session / 'execution-plan.json').read_text(encoding='utf-8'))
    topology = json.loads((session / 'cpu-topology.json').read_text(encoding='utf-8'))
    if state['status'] != 'complete' or state['completed_jobs'] != [job['id'] for job in plan['jobs']]:
        raise ValueError('增强会话未完整完成，拒绝推荐。')
    if topology != recommendation.current_topology():
        raise ValueError('处理器或机器拓扑改变，请重新测试。')
    rounds = {'normal': {}, 'paired': {}, 'shuffled': {}}
    for job in plan['jobs']:
        evidence = json.loads((session / (job['artifact'] + '-validation.json')).read_text(encoding='utf-8'))
        if evidence['job_id'] != job['id'] or evidence['cpus'] != job['cpus']:
            raise ValueError('测试身份与调度记录不一致。')
        if evidence['device_policy'] != 4 or int.from_bytes(bytes.fromhex(evidence['assignment_mask_hex']), 'little') != sum(1 << cpu for cpu in job['cpus']):
            raise ValueError('实际中断掩码与本轮测试组合不一致。')
        path = session / job['directory'] / (job['label'] + '.csv')
        value = metrics(path)
        if value['frames'] != evidence['valid_present_frames']:
            raise ValueError('CSV 帧数与实测记录不一致。')
        label = '+'.join(map(str, job['cpus']))
        rounds[job['phase']][label] = value
    combined, counts = {}, {}
    for cpu, sequential in rounds['normal'].items():
        randomized = rounds['shuffled'][cpu]
        combined[cpu] = {key: (max if key == 'stdev' else min)(sequential['metrics'][key], randomized['metrics'][key])
                         for key in sequential['metrics']}
        counts[cpu] = min(sequential['frames'], randomized['frames'])
    paired = rounds['paired'] if any(core['smt'] for core in topology['cores']) else None
    report = recommendation.recommend(combined, counts, topology, paired_results=paired)
    report.update({'status': 'heuristic', 'mode': 'enhanced', 'topology': topology,
                   'topology_recorded_with_capture': True, 'rounds': rounds, 'seed': plan['seed']})
    output = session / 'enhanced-recommendations.json'
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    from main import display_results, print_table
    print('\n第 2 轮：超线程组合实测（未开启或无完整组合时跳过）')
    if rounds['paired']:
        formatted = {}
        for job in plan['jobs']:
            if job['phase'] != 'paired':
                continue
            # The same 12-metric definition used by the normal table.
            path = session / job['directory'] / (job['label'] + '.csv')
            with path.open(encoding='utf-8-sig') as source:
                raw = [float(row['msBetweenPresents']) for row in csv.DictReader(source)]
            fps = framerate.Fps([value for value in raw if value > 0])
            values = [fps.maximum(), fps.average(), fps.minimum(), fps.stdev()]
            values += [getattr(fps, metric)(percent) for metric in ('percentile', 'lows') for percent in (1, .1, .01, .005)]
            formatted['+'.join(map(str, job['cpus']))] = {str(index): f'{value:.2f}' for index, value in enumerate(values)}
        print_table(formatted)
    else:
        print('本轮已跳过。')
    print('\n第 3 轮：乱序单核心实测（显示按 CPU 编号排列，测试顺序见 execution-plan.json）')
    display_results(str(session / 'shuffled'), False, recommend_cores=False)
    print('\n增强模式综合推荐（各轮取保守成绩，未自动应用绑定）：')
    if not report['all_logical_cpus_measured']:
        print('本次只测试部分核心，候选不代表全处理器最优。')
    names = {'gpu': '显卡', 'mouse_keyboard': '鼠标键盘', 'timer': '系统/高精度计时器'}
    for role, candidate in report['recommended'].items():
        print(f"  {names[role]}：CPU {','.join(map(str, candidate['cpus']))}，掩码 {candidate['mask_hex']}"
              if candidate else f'  {names[role]}：没有足够的合格候选')
    print(f'综合报告：{output}\n鼠标键盘与计时器仍是显卡跑分指标推导的候选，不代表已实测输入延迟。')
    return report
