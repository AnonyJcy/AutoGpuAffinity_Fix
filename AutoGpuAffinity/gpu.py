"""Select one interrupt target and match it to Vulkan's own adapter order."""
import ctypes
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass


@dataclass(frozen=True)
class Gpu:
    name: str
    hwid: str


def select_gpu(gpus, selector="auto"):
    if selector != "auto":
        matches = [g for g in gpus if selector.casefold() in g.name.casefold()
                   or selector.casefold() == g.hwid.casefold()]
    else:
        matches = [g for g in gpus if re.search(r"\b(?:P106(?:-\d+)?|(?:CMP\s*)?(?:30|40)\s*HX)\b", g.name, re.I)]
        if not matches and len(gpus) == 1:
            matches = list(gpus)
    if len(matches) != 1:
        raise ValueError("Cannot select one GPU. Set settings.gpu to its unique name or full PnPDeviceID. Detected: "
                         + "; ".join(f"{g.name} [{g.hwid}]" for g in gpus))
    return matches[0]


def match_vulkan(gpu, adapters):
    pci = re.search(r"VEN_([0-9A-F]{4})&DEV_([0-9A-F]{4})", gpu.hwid, re.I)
    matches = [a for a in adapters if pci and (a[1], a[2]) == tuple(int(v, 16) for v in pci.groups())]
    if len(matches) != 1:
        raise ValueError(f"Cannot uniquely match {gpu.name} to a Vulkan adapter: {adapters}. Check the GPU's Vulkan driver.")
    return matches[0][0]


def vulkan_adapters(loader=None):
    # Allow the actual Vulkan ABI to be exercised on Linux in development too.
    vk = loader if loader is not None else ctypes.WinDLL(
        os.path.join(os.environ['SystemRoot'], 'System32', 'vulkan-1.dll'))
    class ApplicationInfo(ctypes.Structure):
        _fields_ = [("sType", ctypes.c_uint32), ("pNext", ctypes.c_void_p),
                    ("pApplicationName", ctypes.c_char_p), ("applicationVersion", ctypes.c_uint32),
                    ("pEngineName", ctypes.c_char_p), ("engineVersion", ctypes.c_uint32),
                    ("apiVersion", ctypes.c_uint32)]
    class InstanceInfo(ctypes.Structure):
        _fields_ = [("sType", ctypes.c_uint32), ("pNext", ctypes.c_void_p),
                    ("flags", ctypes.c_uint32), ("pApplicationInfo", ctypes.POINTER(ApplicationInfo)),
                    ("enabledLayerCount", ctypes.c_uint32), ("ppEnabledLayerNames", ctypes.c_void_p),
                    ("enabledExtensionCount", ctypes.c_uint32), ("ppEnabledExtensionNames", ctypes.c_void_p)]
    vk.vkCreateInstance.argtypes = [ctypes.POINTER(InstanceInfo), ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
    vk.vkCreateInstance.restype = ctypes.c_int32
    vk.vkEnumeratePhysicalDevices.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32), ctypes.POINTER(ctypes.c_void_p)]
    vk.vkEnumeratePhysicalDevices.restype = ctypes.c_int32
    vk.vkGetPhysicalDeviceProperties.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    vk.vkGetPhysicalDeviceProperties.restype = None
    vk.vkDestroyInstance.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    vk.vkDestroyInstance.restype = None
    instance = ctypes.c_void_p()
    app = ApplicationInfo(sType=0, pApplicationName=b"AutoGpuAffinity",
                          pEngineName=b"AutoGpuAffinity", apiVersion=1 << 22)
    info = InstanceInfo(sType=1, pApplicationInfo=ctypes.pointer(app))
    code = vk.vkCreateInstance(ctypes.byref(info), None, ctypes.byref(instance))
    if code != 0:
        names = {-3: "VK_ERROR_INITIALIZATION_FAILED", -9: "VK_ERROR_INCOMPATIBLE_DRIVER",
                 -6: "VK_ERROR_LAYER_NOT_PRESENT", -7: "VK_ERROR_EXTENSION_NOT_PRESENT"}
        raise RuntimeError(f"Vulkan instance creation failed: {names.get(code, 'VkResult')} ({code})")
    try:
        count = ctypes.c_uint32()
        if vk.vkEnumeratePhysicalDevices(instance, ctypes.byref(count), None) != 0 or not count.value:
            raise RuntimeError("No Vulkan adapters found")
        devices = (ctypes.c_void_p * count.value)()
        if vk.vkEnumeratePhysicalDevices(instance, ctypes.byref(count), devices) != 0:
            raise RuntimeError("Vulkan enumeration failed; retry after the driver settles")
        result = []
        for index in range(count.value):
            # Oversized, aligned storage for VkPhysicalDeviceProperties (including limits).
            properties = (ctypes.c_uint64 * 512)()
            vk.vkGetPhysicalDeviceProperties(devices[index], properties)
            header = ctypes.cast(properties, ctypes.POINTER(ctypes.c_uint32))
            name = ctypes.string_at(ctypes.addressof(properties) + 20, 256).split(b"\0", 1)[0].decode("utf-8", "replace")
            result.append((index, header[2], header[3], name))
        return result
    finally:
        vk.vkDestroyInstance(instance, None)


def query_vulkan_adapters(timeout=10):
    """Never retain a loader/ICD across a PnP restart in the controller process.

    Destroying VkInstance does not clear all driver DLL state on Windows. A
    fresh worker also bounds a hung ICD query, and works in the one-file EXE.
    """
    command = ([sys.executable, '--vulkan-query'] if getattr(sys, 'frozen', False)
               else [sys.executable, os.path.abspath(__file__), '--vulkan-query'])
    try:
        result = subprocess.run(command, capture_output=True, text=True,
                                encoding='utf-8', timeout=timeout,
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    except subprocess.TimeoutExpired as error:
        raise RuntimeError(f'Fresh Vulkan worker timed out after {timeout}s') from error
    if result.returncode != 0:
        raise RuntimeError(f'Fresh Vulkan worker failed ({result.returncode}): '
                           + (result.stderr or result.stdout).strip())
    try:
        adapters = json.loads(result.stdout)
        if not isinstance(adapters, list) or any(
                not isinstance(a, list) or len(a) != 4
                or not all(isinstance(v, int) for v in a[:3])
                or not isinstance(a[3], str) for a in adapters):
            raise ValueError('Invalid adapter payload')
        return [tuple(a) for a in adapters]
    except (ValueError, TypeError) as error:
        raise RuntimeError(f'Invalid Vulkan worker response: {result.stdout!r}') from error


def vulkan_query_worker():
    try:
        print(json.dumps(vulkan_adapters(), ensure_ascii=True))
        return 0
    except (OSError, RuntimeError) as error:
        print(str(error), file=sys.stderr)
        return 1


def wait_vulkan_device(target, timeout=30, query=None):
    """Driver enable returning success does not mean its Vulkan ICD is ready."""
    import logging
    import time
    deadline = time.monotonic() + timeout
    last_error = None
    while True:
        try:
            adapters = query() if query is not None else query_vulkan_adapters(
                timeout=max(0.1, min(10, deadline - time.monotonic())))
            index = match_vulkan(target, adapters)
            logging.getLogger('CLI').info('已用新进程核验 Vulkan：%s；目标索引：%d', adapters, index)
            return index
        except (OSError, RuntimeError, ValueError) as error:
            last_error = error
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError(f"Vulkan did not recover within {timeout}s after driver restart: {last_error}") from error
            logging.getLogger('CLI').warning('等待显卡驱动/Vulkan 恢复：%s', error)
            time.sleep(min(2, remaining))


if __name__ == '__main__':
    sys.exit(vulkan_query_worker())
