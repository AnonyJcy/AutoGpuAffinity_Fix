"""Timer classification and post-reboot binding verification.

These modules import Windows-only libraries, so the file skips on Linux while
still executing everything on Windows.
"""
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'AutoGpuAffinity'))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import windows_support
windows_support.require('winreg', 'wmi', 'win32api')
import binding
import devices


def report(irqs=(), descriptors=(), errors=(), log_status=0):
    return {'irqs': list(irqs), 'descriptors': list(descriptors),
            'errors': list(errors), 'log_status': log_status}


def resources(allocated=(), boot=(), forced=(), descriptors=False, errors=(), log_status=0):
    needs = [{'count': 1}] if descriptors else []
    return {
        'allocated': report(allocated, errors=errors, log_status=log_status),
        'filtered': report(errors=errors, log_status=log_status),
        'basic': report(descriptors=needs, errors=errors, log_status=log_status),
        'boot': report(boot, errors=errors, log_status=log_status),
        'forced': report(forced, errors=errors, log_status=log_status),
    }


class FakeTree:
    def __init__(self, present=True):
        self.present = present

    def locate(self, device_id):
        return 0 if self.present else None


class TimerClassificationTests(unittest.TestCase):
    def describe(self, identifier, **kwargs):
        tree = type('Tree', (), {'irq_resources': lambda self, device_id: resources(**kwargs)})()
        item = {'id': identifier, 'name': identifier, 'class': 'System', 'service': ''}
        return devices.describe_timer(item, tree)

    def test_allocated_irq_is_the_only_bindable_case(self):
        timer = self.describe(r'ACPI\PNP0103\2&DABA3FF&0', allocated=[8])
        self.assertEqual(timer['status'], 'bindable')
        self.assertEqual(timer['target']['id'], r'ACPI\PNP0103\2&DABA3FF&0')
        self.assertEqual(timer['kind'], 'hpet')
        self.assertEqual(timer['name'], '高精度计时器')

    def test_system_timer_boot_irq0_is_refused_with_its_own_reason(self):
        timer = self.describe(r'ACPI\PNP0100\4&1FEB96E6&0', boot=[0], descriptors=True)
        self.assertEqual(timer['status'], 'missing_irq')
        self.assertIsNone(timer['target'])
        self.assertIn('IRQ 0', timer['reason'])
        self.assertIn('系统时基', timer['reason'])

    def test_hpet_without_descriptors_names_the_bios_cause(self):
        timer = self.describe(r'ACPI\PNP0103\2&DABA3FF&0')
        self.assertEqual(timer['status'], 'missing_irq')
        self.assertIn('BIOS', timer['reason'])
        self.assertIn('未报告任何 IRQ 描述符', timer['reason'])

    def test_requirement_only_is_not_reported_as_absence(self):
        timer = self.describe(r'ACPI\PNP0100\4&1FEB96E6&0', descriptors=True)
        self.assertIn('没有已分配 IRQ', timer['reason'])
        self.assertNotIn('BIOS', timer['reason'])

    def test_forced_irq_is_not_treated_as_runtime_allocation(self):
        timer = self.describe(r'ACPI\PNP0100\4&1FEB96E6&0', forced=[0])
        self.assertEqual(timer['status'], 'missing_irq')
        self.assertIn('强制', timer['reason'])

    def test_distinct_reasons_for_system_timer_and_hpet(self):
        system = self.describe(r'ACPI\PNP0100\4&1FEB96E6&0', boot=[0], descriptors=True)
        hpet = self.describe(r'ACPI\PNP0103\2&DABA3FF&0')
        self.assertNotEqual(system['reason'], hpet['reason'])


class VerifyBindingTests(unittest.TestCase):
    def backup(self, before=None, after=None, status='applied_pending_reboot'):
        return {
            'machine': devices.platform.node(),
            'status': status,
            'devices': [{
                'id': r'PCI\VEN_10DE&DEV_1C07\4&1A2B3C4D&0&0008',
                'before': before if before is not None else {'DevicePolicy': None, 'AssignmentSetOverride': None},
                'after': after if after is not None else {
                    'DevicePolicy': {'type': 4, 'value': 4},
                    'AssignmentSetOverride': {'type': 3, 'value': (1 << 2).to_bytes(8, 'little').hex()}},
            }],
        }

    def live_patches(self, live):
        return patch.object(devices, 'read_policy', return_value=live)

    def test_applied_policy_is_confirmed_and_mask_decoded(self):
        data = self.backup()
        with self.live_patches(data['devices'][0]['after']):
            rows = binding.describe_applied(data, FakeTree())
        self.assertEqual(rows[0]['state'], '策略已生效')
        self.assertEqual(rows[0]['mask'], 1 << 2)

    def test_reverted_policy_is_reported_not_confirmed(self):
        data = self.backup()
        with self.live_patches(data['devices'][0]['before']):
            rows = binding.describe_applied(data, FakeTree())
        self.assertEqual(rows[0]['state'], '策略未生效（已回退到原值）')

    def test_foreign_change_is_distinguished_from_revert(self):
        data = self.backup()
        with self.live_patches({'DevicePolicy': {'type': 4, 'value': 3}, 'AssignmentSetOverride': None}):
            rows = binding.describe_applied(data, FakeTree())
        self.assertEqual(rows[0]['state'], '策略被其他工具更改')

    def test_missing_device_is_reported(self):
        data = self.backup()
        with self.live_patches(data['devices'][0]['after']):
            rows = binding.describe_applied(data, FakeTree(present=False))
        self.assertEqual(rows[0]['state'], '设备不在本机')

    def test_verify_without_backup_explains_what_to_do(self):
        with patch.object(binding, 'latest_backup', return_value=None):
            self.assertEqual(binding.verify_binding(None, True), 0)

    def test_verify_marks_backup_verified_only_when_all_match(self):
        import contextlib
        import io
        import json
        import tempfile
        data = self.backup()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'affinity-backup.json'
            path.write_text(json.dumps(data), encoding='utf-8')
            with patch.object(devices, 'read_policy', return_value=data['devices'][0]['after']), \
                 patch.object(devices.DeviceTree, 'locate', return_value=0), \
                 contextlib.redirect_stdout(io.StringIO()):
                code = binding.verify_binding(str(path), True, FakeTree())
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(path.read_text(encoding='utf-8'))['status'], 'verified')

    def test_verify_fails_non_interactively_when_policy_missing(self):
        import contextlib
        import io
        import json
        import tempfile
        data = self.backup()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'affinity-backup.json'
            path.write_text(json.dumps(data), encoding='utf-8')
            with patch.object(devices, 'read_policy', return_value=data['devices'][0]['before']), \
                 patch.object(devices.DeviceTree, 'locate', return_value=0), \
                 contextlib.redirect_stdout(io.StringIO()):
                code = binding.verify_binding(str(path), True, FakeTree())
            self.assertEqual(code, 1)
            self.assertEqual(json.loads(path.read_text(encoding='utf-8'))['status'], 'applied_pending_reboot')

    def test_pointer_round_trip_uses_localappdata(self):
        import os
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {'LOCALAPPDATA': directory}):
                target = Path(directory) / 'session' / 'affinity-backup-x.json'
                target.parent.mkdir(parents=True)
                target.write_text('{}', encoding='utf-8')
                binding.remember_backup(target)
                self.assertEqual(binding.latest_backup(), target.resolve())
                target.unlink()
                self.assertIsNone(binding.latest_backup())


if __name__ == '__main__':
    unittest.main()
