"""Reviewable device IRQ plans, affirmative confirmation, durable recovery."""
import datetime
import contextlib
import io
import json
from pathlib import Path
import devices
import recommendation
import wmi


def show_inventory(data=None):
    data = devices.inventory_devices() if data is None else data
    print('当前输入设备及中断目标：')
    for item in data['inputs']:
        names = list(dict.fromkeys(node['bus_name'] for node in item['chain'] if node.get('bus_name')))
        print(f"  {item['device']['name']} {' / '.join(names)}\n    实例：{item['device']['id']}")
        print(f"    中断目标：{item['target']['name']} [{item['target']['id']}]；{item['reason']}"
              if item['target'] else f"    跳过：{item['reason']}")
    print('当前计时器：')
    for item in data['timers']:
        print(f"  {item['device']['name']} [{item['device']['id']}]：{item['reason']}")
    if not data['timers']:
        print('  未发现系统计时器或 HPET PnP 实例；不猜测绑定。')
    return data


def build_plan(session):
    session = Path(session).resolve()
    state = json.loads((session / 'session-status.json').read_text(encoding='utf-8'))
    if state['status'] != 'complete' or not state.get('driver_recovery_verified'):
        raise ValueError('跑分或驱动恢复未完整成功，不能生成绑定方案。')
    if not (session / 'cpu-topology.json').exists():
        raise ValueError('旧会话没有采集时的 CPU 拓扑，不能自动应用。请重新测试。')
    topology = recommendation.current_topology()
    if topology != json.loads((session / 'cpu-topology.json').read_text(encoding='utf-8')):
        raise ValueError('当前机器或 CPU 拓扑不同，不能应用旧成绩。')
    if state.get('mode') == 'enhanced':
        from enhanced_results import analyze
        with contextlib.redirect_stdout(io.StringIO()):
            report = analyze(session)
    else:
        from main import display_results
        with contextlib.redirect_stdout(io.StringIO()):
            display_results(str(session / 'CSVs'), False)
        report = json.loads((session / 'CSVs/core-recommendations.json').read_text(encoding='utf-8'))
    if report.get('status') != 'heuristic' or not report['topology_recorded_with_capture']:
        raise ValueError('推荐没有通过本机拓扑核验。')
    inventory = show_inventory()
    evidence_path = session / f"CPU-{state['requested_cpus'][0]}-validation.json"
    evidence = json.loads(evidence_path.read_text(encoding='utf-8'))
    gpu_id = evidence['target_pnp_id']
    active_gpus = {g.PnPDeviceID.upper(): g for g in wmi.WMI().Win32_VideoController() if g.PnPDeviceID and g.ConfigManagerErrorCode == 0}
    if gpu_id.upper() not in active_gpus:
        raise ValueError('跑分显卡当前不在本机或设备异常，拒绝绑定另一张显卡。')
    plan = []
    tree = devices.DeviceTree()
    candidate = report['recommended']['gpu']
    if candidate and tree.has_irq(gpu_id):
        plan.append({'role': '显卡', 'id': gpu_id, 'name': active_gpus[gpu_id.upper()].Name,
                     'cpus': candidate['cpus'], 'sources': [gpu_id]})
    elif candidate:
        print('显卡没有可枚举的已分配 IRQ，跳过绑定。')
    inputs = report['recommended']['mouse_keyboard']
    if inputs:
        grouped = {}
        for item in inventory['inputs']:
            if item['target']:
                target = item['target']
                grouped.setdefault(target['id'], {'role': '鼠标键盘中断目标', 'id': target['id'], 'name': target['name'],
                     'cpus': inputs['cpus'], 'sources': [], 'shared_controller': item['shared_controller']})['sources'].append(item['device']['id'])
        plan.extend(grouped.values())
    timers = report['recommended']['timer']
    if timers:
        plan.extend({'role': '高精度计时器' if item['kind'] == 'hpet' else '系统计时器',
                     'id': item['target']['id'], 'name': item['target']['name'], 'cpus': timers['cpus'],
                     'sources': [item['device']['id']]} for item in inventory['timers'] if item['target'])
    return plan, inventory, report


def confirm_binding(session, non_interactive=False):
    if non_interactive:
        print('无人值守模式不应用设备绑定；请交互运行 --bind-from <会话目录> 审核确认。')
        return 0
    plan, inventory, report = build_plan(session)
    output = Path(session).resolve() / 'binding-plan.json'
    output.write_text(json.dumps({'plan': plan, 'inventory': inventory}, ensure_ascii=False, indent=2), encoding='utf-8')
    print('\n请核对绑定方案：')
    for item in plan:
        print(f"  {item['role']}：{item['name']}\n    {item['id']}\n    CPU {','.join(map(str, item['cpus']))}，掩码 0x{sum(1 << cpu for cpu in item['cpus']):X}")
        if item.get('shared_controller'):
            print('    注意：绑定 USB 主控制器会影响该控制器上所有设备，不仅是鼠标键盘。')
    print(f'完整设备对应关系与方案：{output}')
    if not plan:
        print('没有可绑定的合格设备，本次不修改配置。')
        return 0
    if not report['all_logical_cpus_measured']:
        print('本次只测了部分核心，候选不代表全 CPU 最优。')
    print('鼠标键盘/计时器方案依据 FPS 指标，尚未测量实际设备延迟。')
    if input('设备和核心确认无误后，输入“确认绑定”应用；其他输入取消：').strip() != '确认绑定':
        print('已取消，未修改设备绑定。')
        return 0
    # Refresh the live device tree after the user reviewed the plan.
    current = devices.DeviceTree()
    for item in plan:
        if current.locate(item['id']) is None or not current.has_irq(item['id']):
            raise ValueError(f"设备已拔出或 IRQ 资源发生变化：{item['id']}；请重新生成方案。")
    if report['topology'] != recommendation.current_topology():
        raise ValueError('确认期间处理器拓扑发生变化，未应用绑定。')
    backup = Path(session).resolve() / ('affinity-backup-' + datetime.datetime.now().strftime('%Y%m%d-%H%M%S-%f') + '.json')
    devices.apply_transaction(plan, backup)
    print(f'已写入并核验注册表策略，等待设备重新启动后生效。\n建议立即手动重启电脑，之后验证实际效果。\n恢复备份：{backup}\n恢复命令：AutoGpuAffinity.exe --restore-affinity "{backup}"')
    return 0


def confirm_restore(path, non_interactive=False):
    if non_interactive:
        raise ValueError('恢复绑定需要交互确认，不支持无人值守恢复。')
    backup = json.loads(Path(path).read_text(encoding='utf-8-sig'))
    print('待恢复原中断策略的设备：')
    for item in backup['devices']:
        print(f"  {item['id']}")
    if input('输入“确认恢复”执行；其他输入取消：').strip() != '确认恢复':
        print('已取消恢复。')
        return 0
    devices.restore_backup(path)
    print('原策略已恢复并核验；建议立即手动重启电脑。')
    return 0
