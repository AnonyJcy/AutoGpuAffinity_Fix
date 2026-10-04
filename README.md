# AutoGpuAffinity_Fix 1.1.3

维护与修改：**AnonyJcy**

项目：[AnonyJcy/AutoGpuAffinity_Fix](https://github.com/AnonyJcy/AutoGpuAffinity_Fix) · [下载](https://github.com/AnonyJcy/AutoGpuAffinity_Fix/releases)

## 本版变化

- 修复显卡驱动重启后 Vulkan 查询返回 -9：独立进程重新加载加载器与 ICD。
- 按实际渲染显卡、进程 PID 和中断掩码保存采集证据；保留自动分辨率、强制全屏。
- 增加无人值守参数与驱动重新启用保护。此旧版尚未包含后续刷新率锁帧修复，日常使用请下载最新版。

## 使用

Windows x64，以管理员权限运行，需要支持 Vulkan 的显卡驱动。本版按顺序测试单核心。自动分辨率和强制全屏保留。

配置与成绩默认位于 EXE 所在目录；需要解压完整 ZIP 并保留 bin 文件夹。

测试会重启目标显卡驱动，请先保存工作。历史版本供查看演进，日常使用请选最新版。

## 源码构建

安装 Python 3.12 或更新版本，运行 BUILD.cmd；源码启动入口为 START.cmd。

```powershell
python -m pip install -r requirements.txt pyinstaller
python -m unittest discover -s tests -v
.\build.ps1
```

历史源码与对应 EXE 的核验方式见 [SOURCE-RECOVERY.md](SOURCE-RECOVERY.md)。本次整理没有重新进行所有版本的硬件全核跑分。

继续使用 [GPLv3](LICENSE)，保留上游 valleyofdoom/AutoGpuAffinity 与第三方署名，详见 [NOTICE.md](NOTICE.md)。
