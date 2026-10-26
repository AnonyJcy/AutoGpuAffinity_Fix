"""Reviewable device IRQ plans, affirmative confirmation, durable recovery."""
import datetime
import contextlib
import io
import json
import os
from pathlib import Path
import platform
import devices
import recommendation
import wmi


def show_inventory(data=None):
    data = devices.inventory_devices() if data is None else data
    print('输入设备：')
    for item in data['inputs']:
        names = list(dict.fromkeys(node['bus_name'] for node in item['chain'] if node.get('bus_name')))
        label = f"{item['device']['name']}" + (f"（{' / '.join(names)}）" if names else '')
        if item['target']:
            print(f"  {label} → {item['target']['name']}；{item['reason']}")
        else:
            print(f"  {label} → 跳过；{item['reason']}")
    print('计时器：')
    for item in data['timers']:
        print(f"  {item['name']}：{item['reason']}")
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
        plan.extend({'role': item['name'],
                     'id': item['target']['id'], 'name': item['target']['name'], 'cpus': timers['cpus'],
                     'sources': [item['device']['id']]} for item in inventory['timers'] if item['target'])
    return plan, inventory, report


def confirm_binding(session, non_interactive=False):
    if non_interactive:
        print('无人值守模式不应用设备绑定；请交互运行 --bind-from <会话目录> 审核确认。')
        return 0
    plan, inventory = build_plan(session)[:2]
    output = Path(session).resolve() / 'binding-plan.json'
    output.write_text(json.dumps({'plan': plan, 'inventory': inventory}, ensure_ascii=False, indent=2), encoding='utf-8')
    if not plan:
        print('没有可绑定的合格设备，本次不修改配置。')
        return 0
    print('\n绑定方案：')
    for item in plan:
        mask = sum(1 << cpu for cpu in item['cpus'])
        print(f"  {item['role']}  {item['name']}  →  CPU {','.join(map(str, item['cpus']))}（0x{mask:X}）")
    notes = []
    if any(item.get('shared_controller') for item in plan):
        notes.append('USB 主控制器为整条控制器绑定，其上所有设备同时受影响。')
    if not report['all_logical_cpus_measured']:
        notes.append('本次只测了部分核心，候选不代表全 CPU 最优。')
    notes.append('鼠标键盘/计时器方案依据 FPS 指标，未测量实际设备延迟。')
    for note in notes:
        print(f'  注意：{note}')
    print(f'完整设备对应关系：{output}')
    if input('确认无误后输入“确认绑定”应用；其他输入取消：').strip() != '确认绑定':
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
    remember_backup(backup)
    print('\n绑定已写入注册表，重启后才会生效。')
    print(f'  重启后运行 --verify-binding 检测是否生效')
    print(f'  恢复原设置：--restore-affinity "{backup}"')
    return 0


BACKUP_POINTER = 'last-affinity-backup.txt'

def pointer_path():
    """Remember the latest backup outside the session folder so a reboot can find it."""
    home = Path(os.environ.get('LOCALAPPDATA') or Path.home()) / 'AutoGpuAffinity_Fix'
    return home / BACKUP_POINTER

def remember_backup(path):
    try:
        target = pointer_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(str(Path(path).resolve()), encoding='utf-8')
    except OSError:
        pass  # Verification still works; --verify-binding would need the explicit path.

def latest_backup():
    try:
        path = Path(pointer_path().read_text(encoding='utf-8').strip())
    except (OSError, UnicodeError):
        return None
    return path if path.is_file() else None

def describe_applied(backup, tree=None):
    """Effective mask per device, comparing the live registry against the backup."""
    tree = devices.DeviceTree() if tree is None else tree
    rows = []
    for item in backup['devices']:
        live = devices.read_policy(item['id'])
        after, before = item.get('after') or {}, item.get('before') or {}
        mask = None
        raw = (live.get('AssignmentSetOverride') or {}).get('value')
        if isinstance(raw, str):
            try:
                mask = int.from_bytes(bytes.fromhex(raw), 'little')
            except ValueError:
                mask = None
        matches_after = live == after
        reverted = live == before
        if tree.locate(item['id']) is None:
            state = '设备不在本机'
        elif matches_after and mask is not None:
            state = '策略已生效'
        elif matches_after:
            state = '策略未设置亲和性'
        elif reverted:
            state = '策略未生效（已回退到原值）'
        else:
            state = '策略被其他工具更改'
        rows.append({'id': item['id'], 'mask': mask, 'state': state, 'live': live})
    return rows

def verify_binding(backup_path=None, non_interactive=False, tree=None):
    """Post-reboot check: does the live registry still hold what we wrote?"""
    path = Path(backup_path).resolve() if backup_path else latest_backup()
    if path is None or not path.is_file():
        print(f'未找到绑定备份记录。请用 --verify-binding "<备份.json>" 指定，'
              f'或先执行一次绑定。')
        return 0
    backup = json.loads(path.read_text(encoding='utf-8-sig'))
    if backup.get('machine') != platform.node():
        raise ValueError('备份来自另一台电脑，拒绝检测。')
    if backup.get('status') not in ('applied_pending_reboot', 'verified'):
        print(f'该备份状态为 {backup.get("status")}，没有待验证的绑定。')
        return 0
    rows = describe_applied(backup, tree)
    print(f'绑定检测（备份：{path.name}）')
    for item, row in zip(backup['devices'], rows):
        mask = f"CPU 掩码 0x{row['mask']:X}" if row['mask'] is not None else '无亲和性掩码'
        print(f"  {item['id']}")
        print(f"    {row['state']}；{mask}")
    confirmed = [row for row in rows if row['state'] == '策略已生效']
    print(f'结果：{len(confirmed)}/{len(rows)} 个设备策略仍在。')
    print('提示：此处核对的是持久化的中断亲和性策略；中断到核心的实际映射由 Windows 内核负责。')
    if len(confirmed) != len(rows):
        print('若有设备未生效，请确认已经重启，或重新运行一次绑定。')
    else:
        backup['status'] = 'verified'
        path.write_text(json.dumps(backup, ensure_ascii=False, indent=2), encoding='utf-8')
    if non_interactive:
        return 0 if len(confirmed) == len(rows) else 1
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
