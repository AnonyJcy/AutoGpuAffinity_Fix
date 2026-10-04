"""Read live PnP ancestry and IRQ resources; do not bind HID leaf placeholders."""
import ctypes
from ctypes import wintypes
import json
from pathlib import Path
import platform
import struct
import uuid
import winreg
import wmi


class DeviceTree:
    def __init__(self):
        self.api = ctypes.WinDLL('cfgmgr32')
        uint = ctypes.c_ulong
        handle = ctypes.c_size_t
        declarations = {
            'CM_Locate_DevNodeW': [ctypes.POINTER(uint), ctypes.c_wchar_p, uint],
            'CM_Get_Parent': [ctypes.POINTER(uint), uint, uint],
            'CM_Get_Device_IDW': [uint, ctypes.c_wchar_p, uint, uint],
            'CM_Get_First_Log_Conf': [ctypes.POINTER(handle), uint, uint],
            'CM_Get_Next_Res_Des': [ctypes.POINTER(handle), handle, uint, ctypes.POINTER(uint), uint],
            'CM_Free_Res_Des_Handle': [handle], 'CM_Free_Log_Conf_Handle': [handle],
            'CM_Get_Res_Des_Data_Size': [ctypes.POINTER(uint), handle, uint],
            'CM_Get_Res_Des_Data': [handle, ctypes.c_void_p, uint, uint],
        }
        for name, arguments in declarations.items():
            function = getattr(self.api, name)
            function.argtypes = arguments
            function.restype = uint

    def locate(self, device_id):
        node = ctypes.c_ulong()
        return node.value if self.api.CM_Locate_DevNodeW(ctypes.byref(node), device_id, 0) == 0 else None

    def parent(self, device_id):
        node = self.locate(device_id)
        if node is None:
            return None
        parent = ctypes.c_ulong()
        if self.api.CM_Get_Parent(ctypes.byref(parent), node, 0) != 0:
            return None
        output = ctypes.create_unicode_buffer(4096)
        if self.api.CM_Get_Device_IDW(parent, output, len(output), 0) != 0:
            return None
        return output.value

    def has_irq(self, device_id):
        return bool(self.irq_configuration(device_id, 2)['irqs'])

    def irq_configuration(self, device_id, flag):
        """Enumerate IRQ descriptors, adapted from spddl/GoInterruptPolicy winapi.go.

        BASIC/FILTERED are requirements, not evidence of an allocated interrupt.
        Keep CONFIGRET codes and descriptor counts for auditable diagnostics.
        """
        report = {'irqs': [], 'descriptors': [], 'errors': []}
        node = self.locate(device_id)
        if node is None:
            report['errors'].append('设备不存在')
            return report
        log = ctypes.c_size_t()
        report['log_status'] = int(self.api.CM_Get_First_Log_Conf(ctypes.byref(log), node, flag))
        if report['log_status'] != 0:
            return report
        current = log.value
        owned = None
        try:
            for _ in range(1024):
                descriptor, resource_type = ctypes.c_size_t(), ctypes.c_ulong()
                status = int(self.api.CM_Get_Next_Res_Des(ctypes.byref(descriptor), current, 4,
                                                       ctypes.byref(resource_type), 0))
                if status != 0:
                    report['end_status'] = status
                    if status != 15:  # CR_NO_MORE_RES_DES; do not use upstream's swapped constants.
                        report['errors'].append(f'枚举 IRQ CONFIGRET={status}')
                    break
                if owned is not None:
                    self.api.CM_Free_Res_Des_Handle(owned)
                current = owned = descriptor.value
                size = ctypes.c_ulong()
                status = int(self.api.CM_Get_Res_Des_Data_Size(ctypes.byref(size), current, 0))
                if status != 0 or not 16 <= size.value <= 1048576:
                    report['errors'].append(f'IRQ 数据大小无效 CONFIGRET={status} size={size.value}')
                    break
                buffer = ctypes.create_string_buffer(size.value)
                status = int(self.api.CM_Get_Res_Des_Data(current, buffer, size.value, 0))
                if status != 0:
                    report['errors'].append(f'读取 IRQ CONFIGRET={status}')
                    break
                count, kind, flags, number = struct.unpack_from('<IIII', buffer.raw)
                report['descriptors'].append({'count': count, 'type': kind, 'flags': flags, 'number': number})
                # IRQD_Count == 0 denotes a resource allocation rather than IRQ ranges.
                if count == 0 and flag in (2, 3, 4):
                    report['irqs'].append(number)
            else:
                report['errors'].append('IRQ 描述符数量超限')
        finally:
            if owned is not None:
                self.api.CM_Free_Res_Des_Handle(owned)
            self.api.CM_Free_Log_Conf_Handle(log)
        # A partial read must not qualify a device for automatic binding.
        if report['errors']:
            report['irqs'] = []
        return report

    def irq_resources(self, device_id):
        return {name: self.irq_configuration(device_id, flag) for name, flag in
                [('allocated', 2), ('filtered', 1), ('basic', 0), ('boot', 3), ('forced', 4)]}

    def bus_name(self, device_id):
        node = self.locate(device_id)
        if node is None:
            return ''
        class PropertyKey(ctypes.Structure):
            _fields_ = [('guid', ctypes.c_ubyte * 16), ('pid', wintypes.DWORD)]
        key = PropertyKey((ctypes.c_ubyte * 16).from_buffer_copy(uuid.UUID('540b947e-8b40-45bc-a8a2-6a0b894cbda2').bytes_le), 4)
        query = self.api.CM_Get_DevNode_PropertyW
        query.argtypes = [ctypes.c_ulong, ctypes.POINTER(PropertyKey), ctypes.POINTER(ctypes.c_ulong),
                          ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong), ctypes.c_ulong]
        kind, size = ctypes.c_ulong(), ctypes.c_ulong(8192)
        buffer = ctypes.create_string_buffer(size.value)
        if query(node, ctypes.byref(key), ctypes.byref(kind), buffer, ctypes.byref(size), 0) == 0 and kind.value == 18:
            return buffer.raw[:size.value].decode('utf-16-le').rstrip('\0')
        return ''


def resolve_input(device, inventory, tree):
    chain, current = [], device['id']
    for _ in range(32):
        if not current or current.upper() in {item['id'].upper() for item in chain}:
            break
        item = inventory.get(current.upper(), {'id': current, 'name': current, 'service': '', 'class': ''})
        item = dict(item, bus_name=tree.bus_name(current))
        chain.append(item)
        service = (item['service'] or '').lower()
        # Direct PS/2 or IRQ-bearing ACPI input devices; USB requires the host controller.
        usb_host = item['class'] == 'USB' and service in ('usbxhci', 'usbehci', 'usbuhci', 'usbohci', 'usbport')
        direct = len(chain) == 1 and not current.upper().startswith('HID\\')
        if (usb_host or direct) and tree.has_irq(current):
            return {'device': device, 'chain': chain, 'target': item,
                    'shared_controller': usb_host, 'reason': 'USB 主控制器共享中断' if usb_host else '直接中断设备'}
        current = tree.parent(current)
    return {'device': device, 'chain': chain, 'target': None,
            'shared_controller': False, 'reason': '未找到受支持的直接 IRQ 或 USB 主控制器；虚拟/I2C 输入不猜测绑定'}


def inventory_devices():
    tree = DeviceTree()
    inventory = {}
    for device in wmi.WMI().Win32_PnPEntity():
        if device.DeviceID and device.ConfigManagerErrorCode == 0:
            inventory[device.DeviceID.upper()] = {'id': device.DeviceID, 'name': device.Name or device.DeviceID,
                'class': device.PNPClass or '', 'service': device.Service or ''}
    inputs = [resolve_input(item, inventory, tree) for item in inventory.values()
              if item['class'] in ('Keyboard', 'Mouse')]
    timers = []
    for item in inventory.values():
        identifier = item['id'].upper()
        if identifier.startswith(('ACPI\\PNP0100\\', 'ACPI\\PNP0103\\')):
            resources = tree.irq_resources(item['id'])
            supported = bool(resources['allocated']['irqs'])
            reason = ('检测到已分配 IRQ：' + ','.join(map(str, resources['allocated']['irqs'])) if supported
                      else '未检测到已分配 IRQ，不自动绑定')
            if not supported and resources['boot']['irqs']:
                reason += '；仅启动配置 IRQ：' + ','.join(map(str, resources['boot']['irqs']))
            if not supported and any(resources[name]['descriptors'] for name in ('filtered', 'basic')):
                reason += '；资源需求存在，不代表运行时已分配'
            timers.append({'device': item, 'kind': 'hpet' if 'PNP0103' in identifier else 'system_timer',
                           'target': item if supported else None,
                           'reason': reason, 'irq_resources': resources})
    return {'machine': platform.node(), 'inputs': inputs, 'timers': timers}


def policy_path(device_id):
    if not device_id or any(part in ('', '..', '.') for part in device_id.split('\\')):
        raise ValueError('无效设备实例 ID')
    return f'SYSTEM\\CurrentControlSet\\Enum\\{device_id}\\Device Parameters\\Interrupt Management\\Affinity Policy'


def read_policy(device_id):
    values = {}
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, policy_path(device_id), 0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as key:
            for name in ('DevicePolicy', 'AssignmentSetOverride'):
                try:
                    value, kind = winreg.QueryValueEx(key, name)
                    values[name] = {'type': kind, 'value': value.hex() if isinstance(value, bytes) else value}
                except FileNotFoundError:
                    values[name] = None
    except FileNotFoundError:
        values = {name: None for name in ('DevicePolicy', 'AssignmentSetOverride')}
    return values


def write_policy(device_id, values):
    with winreg.CreateKeyEx(winreg.HKEY_LOCAL_MACHINE, policy_path(device_id), 0,
                            winreg.KEY_SET_VALUE | winreg.KEY_WOW64_64KEY) as key:
        for name, entry in values.items():
            if name not in ('DevicePolicy', 'AssignmentSetOverride'):
                raise ValueError('备份含有未知注册表值')
            if entry is None:
                try:
                    winreg.DeleteValue(key, name)
                except FileNotFoundError:
                    pass
            else:
                value = bytes.fromhex(entry['value']) if entry['type'] == winreg.REG_BINARY else entry['value']
                winreg.SetValueEx(key, name, 0, entry['type'], value)
    if read_policy(device_id) != values:
        raise RuntimeError(f'注册表写入后核验失败：{device_id}')


def desired_policy(cpus):
    if not cpus or len(set(cpus)) != len(cpus) or any(not isinstance(cpu, int) or not 0 <= cpu < 64 for cpu in cpus):
        raise ValueError('非法 CPU 亲和性组合')
    return {'DevicePolicy': {'type': winreg.REG_DWORD, 'value': 4},
            'AssignmentSetOverride': {'type': winreg.REG_BINARY,
                'value': sum(1 << cpu for cpu in cpus).to_bytes(8, 'little').hex()}}


def apply_transaction(plan, backup_path):
    """Durable backup before any write; rollback even after partial device writes."""
    if len({item['id'].upper() for item in plan}) != len(plan):
        raise ValueError('同一中断设备被重复分配')
    backup = {'machine': platform.node(), 'status': 'prepared', 'devices':
              [{'id': item['id'], 'before': read_policy(item['id']), 'after': desired_policy(item['cpus'])} for item in plan]}
    path = Path(backup_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('x', encoding='utf-8') as output:
        json.dump(backup, output, ensure_ascii=False, indent=2)
        output.flush()
        import os
        os.fsync(output.fileno())
    attempted = []
    try:
        for item in backup['devices']:
            attempted.append(item)
            write_policy(item['id'], item['after'])
        backup['status'] = 'applied_pending_reboot'
    except BaseException as error:
        failures = []
        for item in reversed(attempted):
            try:
                write_policy(item['id'], item['before'])
            except BaseException as rollback_error:
                failures.append(f"{item['id']}: {rollback_error}")
        backup.update(status='rollback_failed' if failures else 'rolled_back', error=str(error), rollback_errors=failures)
        path.write_text(json.dumps(backup, ensure_ascii=False, indent=2), encoding='utf-8')
        if failures:
            raise RuntimeError(f'写入失败且回滚不完整；请使用备份恢复：{path}；{failures}') from error
        raise
    path.write_text(json.dumps(backup, ensure_ascii=False, indent=2), encoding='utf-8')
    return backup


def restore_backup(path):
    backup = json.loads(Path(path).read_text(encoding='utf-8-sig'))
    if backup['machine'] != platform.node():
        raise ValueError('备份来自另一台电脑，拒绝恢复。')
    tree = DeviceTree()
    for item in backup['devices']:
        if tree.locate(item['id']) is None:
            raise ValueError(f"备份设备已不在本机：{item['id']}")
        if read_policy(item['id']) not in (item['before'], item['after']):
            raise ValueError(f"设备配置已被其他工具更改，拒绝覆盖：{item['id']}")
    for item in backup['devices']:
        write_policy(item['id'], item['before'])
    backup['status'] = 'restored_pending_reboot'
    Path(path).write_text(json.dumps(backup, ensure_ascii=False, indent=2), encoding='utf-8')
    return backup
