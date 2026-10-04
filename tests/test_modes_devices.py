import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'AutoGpuAffinity'))
import benchmark_modes
import devices
import binding


class ModeTests(unittest.TestCase):
    def test_argument_errors_and_interrupts_preserve_failure_exit(self):
        import main
        for error, expected in [(SystemExit(2), 2), (KeyboardInterrupt(), 1), (EOFError(), 1)]:
            with patch('main.main', side_effect=error), patch('main.ctypes.WinDLL') as native, \
                 patch('main.sys.stdin.isatty', return_value=False):
                native.return_value.GetConsoleProcessList.return_value = 5
                with self.assertRaises(SystemExit) as result:
                    main._main()
                self.assertEqual(result.exception.code, expected)

    def test_real_smt_pairs_and_recorded_shuffle(self):
        cores = [{'cpus': [0, 1], 'group': 0, 'smt': True}, {'cpus': [2, 3], 'group': 0, 'smt': True},
                 {'cpus': [4], 'group': 0, 'smt': False}]
        plan = benchmark_modes.schedule(range(5), {'cores': cores}, 'enhanced', seed=10)
        self.assertEqual([job['cpus'] for job in plan['jobs'] if job['phase'] == 'paired'], [[0, 1], [2, 3]])
        shuffled = [job['cpus'][0] for job in plan['jobs'] if job['phase'] == 'shuffled']
        self.assertEqual(sorted(shuffled), list(range(5)))
        self.assertNotEqual(shuffled, list(range(5)))
        self.assertEqual(plan, benchmark_modes.schedule(range(5), {'cores': cores}, 'enhanced', seed=10))

    def test_no_smt_and_incomplete_pair_skip(self):
        topology = {'cores': [{'cpus': [0], 'group': 0, 'smt': False}, {'cpus': [1], 'group': 0, 'smt': False}]}
        self.assertEqual(len(benchmark_modes.schedule([0, 1], topology, 'enhanced')['jobs']), 4)
        topology['cores'] = [{'cpus': [0, 1], 'group': 0, 'smt': True}]
        plan = benchmark_modes.schedule([0], topology, 'enhanced')
        self.assertEqual(len(plan['jobs']), 2)
        self.assertEqual(len(plan['skipped_pairs']), 1)


class DeviceTests(unittest.TestCase):
    def test_hid_resolves_to_usb_irq_controller_not_hub(self):
        tree = unittest.mock.Mock()
        tree.parent.side_effect = lambda device: {'HID\\A': 'USB\\A', 'USB\\A': 'PCI\\A'}.get(device)
        tree.bus_name.return_value = ''
        tree.has_irq.return_value = True
        hid = {'id': 'HID\\A', 'name': 'keyboard', 'class': 'Keyboard', 'service': 'kbdhid'}
        inventory = {'HID\\A': hid, 'USB\\A': {'id': 'USB\\A', 'name': 'hub', 'class': 'USB', 'service': 'usbhub'},
                     'PCI\\A': {'id': 'PCI\\A', 'name': 'host', 'class': 'USB', 'service': 'USBXHCI'}}
        result = devices.resolve_input(hid, inventory, tree)
        self.assertEqual(result['target']['id'], 'PCI\\A')
        tree.has_irq.assert_called_once_with('PCI\\A')

    def test_failed_second_write_rolls_back_even_partial_device(self):
        before = {'DevicePolicy': None, 'AssignmentSetOverride': None}
        writes = []
        def write(device, values):
            writes.append((device, values))
            if len(writes) == 2:
                raise OSError('partial write')
        with tempfile.TemporaryDirectory() as directory, patch('devices.read_policy', return_value=before), \
             patch('devices.write_policy', side_effect=write):
            path = Path(directory) / 'backup.json'
            with self.assertRaises(OSError):
                devices.apply_transaction([{'id': 'PCI\\A', 'cpus': [2, 3]}, {'id': 'PCI\\B', 'cpus': [4, 5]}], path)
            self.assertEqual([device for device, values in writes], ['PCI\\A', 'PCI\\B', 'PCI\\B', 'PCI\\A'])
            self.assertEqual(json.loads(path.read_text())['status'], 'rolled_back')

    def test_backup_must_exist_before_first_write(self):
        before = {'DevicePolicy': None, 'AssignmentSetOverride': None}
        with tempfile.TemporaryDirectory() as directory, patch('devices.read_policy', return_value=before):
            path = Path(directory) / 'backup.json'
            def write(device, values):
                self.assertTrue(path.exists())
                self.assertEqual(json.loads(path.read_text())['status'], 'prepared')
            with patch('devices.write_policy', side_effect=write):
                result = devices.apply_transaction([{'id': 'PCI\\A', 'cpus': [2, 3]}], path)
            self.assertEqual(result['status'], 'applied_pending_reboot')

    def test_unattended_does_not_enter_binding_or_write(self):
        with patch('binding.build_plan') as plan, patch('devices.apply_transaction') as write, \
             contextlib.redirect_stdout(io.StringIO()):
            binding.confirm_binding('unused', True)
        plan.assert_not_called()
        write.assert_not_called()
