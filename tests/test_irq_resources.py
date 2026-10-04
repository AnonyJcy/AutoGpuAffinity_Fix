import ctypes
from pathlib import Path
import struct
import sys
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'AutoGpuAffinity'))
from devices import DeviceTree


class IRQResourceTests(unittest.TestCase):
    def query(self, count=0, flag=2, data_status=0, end_status=15):
        tree = DeviceTree.__new__(DeviceTree)
        tree.locate = lambda identifier: 7
        api = tree.api = Mock()
        def set_pointer(pointer, kind, value):
            ctypes.cast(pointer, ctypes.POINTER(kind)).contents.value = value
        def log(output, node, configuration):
            set_pointer(output, ctypes.c_size_t, 10)
            return 0
        calls = []
        def next_resource(output, current, kind, returned_kind, flags):
            calls.append(current)
            if len(calls) == 1:
                set_pointer(output, ctypes.c_size_t, 11)
                return 0
            return end_status
        def size(output, descriptor, flags):
            set_pointer(output, ctypes.c_ulong, 24)
            return 0
        def data(descriptor, buffer, size, flags):
            ctypes.memmove(buffer, struct.pack('<IIIIQ', count, 12, 2, 0, 1), 24)
            return data_status
        api.CM_Get_First_Log_Conf.side_effect = log
        api.CM_Get_Next_Res_Des.side_effect = next_resource
        api.CM_Get_Res_Des_Data_Size.side_effect = size
        api.CM_Get_Res_Des_Data.side_effect = data
        report = tree.irq_configuration('ACPI\\PNP0100\\TEST', flag)
        api.CM_Free_Res_Des_Handle.assert_called_once_with(11)
        api.CM_Free_Log_Conf_Handle.assert_called_once()
        return report

    def test_irq_zero_is_valid_allocation(self):
        self.assertEqual(self.query()['irqs'], [0])

    def test_requirement_or_boot_is_not_runtime_allocation(self):
        self.assertEqual(self.query(count=1)['irqs'], [])
        self.assertEqual(self.query(flag=0)['irqs'], [])
        tree = DeviceTree.__new__(DeviceTree)
        tree.irq_configuration = Mock(return_value={'irqs': []})
        self.assertFalse(tree.has_irq('timer'))
        tree.irq_configuration.assert_called_once_with('timer', 2)

    def test_partial_descriptor_failure_cannot_qualify(self):
        self.assertEqual(self.query(data_status=6)['irqs'], [])
        result = self.query(end_status=6)
        self.assertEqual(result['irqs'], [])
        self.assertTrue(result['errors'])
