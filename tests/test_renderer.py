import ctypes
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'AutoGpuAffinity'))
import subprocess
from gpu import Gpu, vulkan_adapters, wait_vulkan_device, query_vulkan_adapters
from renderer import device_name, same_gpu, wait_ready, probe_device, verify_log


class Function:
    def __init__(self, f): self.f = f
    def __call__(self, *args): return self.f(*args)


class FakeVulkan:
    def __init__(self, code=0):
        self.destroyed = False
        self.vkCreateInstance = Function(self.create)
        self.vkEnumeratePhysicalDevices = Function(self.enumerate)
        self.vkGetPhysicalDeviceProperties = Function(self.properties)
        self.vkDestroyInstance = Function(self.destroy)
        self.code = code
    def create(self, info, allocator, instance):
        assert info._obj.sType == 1
        assert info._obj.pApplicationInfo.contents.apiVersion == 1 << 22
        instance._obj.value = 123
        return self.code
    def enumerate(self, instance, count, devices):
        count._obj.value = 1
        if devices is not None: devices[0] = 456
        return 0
    def properties(self, device, storage):
        fields = ctypes.cast(storage, ctypes.POINTER(ctypes.c_uint32))
        fields[2], fields[3] = 0x10DE, 0x1C07
        ctypes.memmove(ctypes.addressof(storage) + 20, b'NVIDIA P106-100\0', 16)
    def destroy(self, *args): self.destroyed = True


class RendererTests(unittest.TestCase):
    def test_worker_errors_and_invalid_payload_are_rejected(self):
        for result in [subprocess.CompletedProcess([], 1, '', 'VK_ERROR_INCOMPATIBLE_DRIVER (-9)'),
                       subprocess.CompletedProcess([], 0, 'null', ''),
                       subprocess.CompletedProcess([], 0, '[[0, 1, "bad", "GPU"]]', '')]:
            with patch('gpu.subprocess.run', return_value=result), self.assertRaises(RuntimeError):
                query_vulkan_adapters()
        with patch('gpu.subprocess.run', side_effect=subprocess.TimeoutExpired([], 1)):
            with self.assertRaisesRegex(RuntimeError, 'timed out'):
                query_vulkan_adapters(timeout=1)

    def test_frozen_worker_uses_exe_without_benchmark_entrypoint(self):
        result = subprocess.CompletedProcess([], 0, '[[0,4318,7175,"NVIDIA P106-100"]]', '')
        with patch('gpu.sys.frozen', True, create=True), patch('gpu.subprocess.run', return_value=result) as run:
            self.assertEqual(query_vulkan_adapters(), [(0, 4318, 7175, 'NVIDIA P106-100')])
            self.assertEqual(run.call_args.args[0], [sys.executable, '--vulkan-query'])

    def test_restart_recovers_after_transient_driver_failure(self):
        target = Gpu('NVIDIA P106-100', r'PCI\VEN_10DE&DEV_1C07\B')
        replies = [RuntimeError('VK_ERROR_INCOMPATIBLE_DRIVER (-9)'), [],
                   [(1, 0x10DE, 0x1C07, 'NVIDIA P106-100')]]
        def query():
            reply = replies.pop(0)
            if isinstance(reply, Exception): raise reply
            return reply
        with patch('time.sleep'):
            self.assertEqual(wait_vulkan_device(target, query=query), 1)
        self.assertEqual(replies, [])

    def test_restart_timeout_is_bounded(self):
        target = Gpu('NVIDIA P106-100', r'PCI\VEN_10DE&DEV_1C07\B')
        with self.assertRaisesRegex(RuntimeError, 'did not recover'):
            wait_vulkan_device(target, timeout=0, query=lambda: [])

    def test_vulkan_abi_and_instance_cleanup(self):
        vk = FakeVulkan()
        self.assertEqual(vulkan_adapters(vk), [(0, 0x10DE, 0x1C07, 'NVIDIA P106-100')])
        self.assertTrue(vk.destroyed)

    def test_driver_error_reports_exact_code(self):
        with self.assertRaisesRegex(RuntimeError, r'INCOMPATIBLE_DRIVER.*-9'):
            vulkan_adapters(FakeVulkan(-9))

    def test_renderer_name_not_output_card(self):
        log = '[2026] [info] device: NVIDIA P106-100 (discrete gpu) - driver: 1.2.3\n'
        self.assertEqual(device_name(log), 'NVIDIA P106-100')
        self.assertTrue(same_gpu('NVIDIA P106-100 (RainCandy Technology)', device_name(log)))
        self.assertFalse(same_gpu('NVIDIA P106-100', 'AMD Radeon HD 7450'))
        self.assertFalse(same_gpu('NVIDIA CMP 30HX', 'NVIDIA CMP 40HX'))

    def test_wrong_gpu_and_early_exit_rejected(self):
        target = Gpu('NVIDIA P106-100', 'pci')
        class Process:
            returncode = 1
            def poll(self): return None
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / 'log.txt'
            log.write_text('device: AMD Radeon HD 7450 (integrated gpu) - driver: 1.2.3')
            with self.assertRaisesRegex(RuntimeError, 'expected NVIDIA'):
                wait_ready(Process(), log, target)
            log.write_text('device: NVIDIA P106-100 (discrete gpu) - driver: 1.2.3')
            process = Process()
            process.poll = lambda: 1
            with self.assertRaisesRegex(RuntimeError, 'exited'):
                wait_ready(process, log, target)

    def test_probe_skips_wrong_adapter_and_cleans_processes(self):
        processes = []
        class Process:
            returncode = None
            def poll(self): return self.returncode
            def terminate(self): self.returncode = 0
            def wait(self, timeout=None): return self.returncode
        def launch(binary, args, directory):
            name = 'AMD Radeon HD 7450' if args[0].endswith('=0') else 'NVIDIA P106-100'
            Path(directory).mkdir(parents=True)
            path = Path(directory) / 'lava.log'
            path.write_text(f'device: {name} (discrete gpu) - driver: 1.2.3\n')
            p = Process(); processes.append(p); return p, path
        with tempfile.TemporaryDirectory() as directory:
            with patch('renderer.launch', side_effect=launch), patch('renderer.time.sleep'), \
                 patch('renderer.wait_window'), patch('renderer.stop', side_effect=lambda p: p.terminate()):
                self.assertEqual(probe_device('lava.exe', Gpu('NVIDIA P106-100', 'pci'), 2, directory), 1)
        self.assertTrue(all(p.poll() == 0 for p in processes))

if __name__ == '__main__': unittest.main()
