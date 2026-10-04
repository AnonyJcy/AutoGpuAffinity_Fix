"""Auto select the fastest available Vulkan adapter in the built-in workload."""
import json
from pathlib import Path
import time
import display
from gpu import match_vulkan, query_vulkan_adapters, select_gpu
import presentation
import renderer


def auto_select(gpus, selector, binary, presentmon, directory):
    if selector != 'auto':
        return select_gpu(gpus, selector)
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    adapters = query_vulkan_adapters()
    candidates, rejected = [], []
    for gpu in gpus:
        if not gpu.hwid.upper().startswith('PCI\\'):
            rejected.append({'name': gpu.name, 'reason': '虚拟或非 PCI 显卡'})
            continue
        try:
            candidates.append((gpu, match_vulkan(gpu, adapters)))
        except ValueError as error:
            rejected.append({'name': gpu.name, 'reason': str(error)})
    scores = []
    if len(candidates) == 1:
        selected = candidates[0][0]
        reason = '仅一张可唯一匹配且支持本 Vulkan 测试的物理显卡；不宣称跨显卡性能比较。'
    else:
        width, height = display.primary_resolution()
        for number, (gpu, index) in enumerate(candidates):
            process = overlay = None
            try:
                print(f'显卡自动筛选：实测 {gpu.name}（同分辨率、全屏、3 秒）')
                process, native = renderer.launch(binary, [f'--physical_device={index}', '--fullscreen=0',
                    f'--width={width}', f'--height={height}', '--fps_cap=0', '--v_sync=0'], directory / f'gpu-{number}')
                renderer.wait_window(process)
                display.force_fullscreen(process, width, height)
                overlay = presentation.StatusOverlay(process.pid, f'AutoGpuAffinity GPU {number}')
                time.sleep(2)
                stats = presentation.capture(presentmon, process, 3, directory / f'gpu-{number}.csv')
                overlay.check()
                if presentation.refresh_paced(stats, display.primary_refresh_rate()):
                    raise RuntimeError('被显示刷新率限制，不能据此排名')
                renderer.stop(process)
                renderer.verify_log(native, gpu)
                scores.append({'name': gpu.name, 'id': gpu.hwid, 'average_fps': stats['average_fps']})
            except (OSError, RuntimeError) as error:
                rejected.append({'name': gpu.name, 'reason': str(error)})
            finally:
                if overlay:
                    overlay.close()
                if process and process.poll() is None:
                    renderer.stop(process)
        if not scores:
            raise RuntimeError('没有能完成自动筛选的显卡，请检查驱动或手动设置 settings.gpu。')
        best = max(scores, key=lambda item: item['average_fps'])
        selected = next(gpu for gpu in gpus if gpu.hwid == best['id'])
        reason = '本内置测试场景实测平均帧率最高；不是通用显卡性能排名。'
    report = {'selected': {'name': selected.name, 'id': selected.hwid}, 'reason': reason,
              'scores': scores, 'rejected': rejected}
    (directory / 'selection.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'自动选择显卡：{selected.name}\n{reason}')
    return selected
