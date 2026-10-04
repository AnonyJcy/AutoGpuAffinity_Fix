# AutoGpuAffinity_Fix 1.2.0

维护与修改：**AnonyJcy**

项目：[AnonyJcy/AutoGpuAffinity_Fix](https://github.com/AnonyJcy/AutoGpuAffinity_Fix) · [下载](https://github.com/AnonyJcy/AutoGpuAffinity_Fix/releases)

## 本版变化

- 提供中文简单版、增强版和中文帮助；增强版包含普通单核心、真实超线程组合、乱序单核心三轮。
- 支持渲染显卡短测选择、键鼠中断目标识别和确认后的设备绑定。
- 绑定前备份原策略，写入后读回核验，失败回滚；提供恢复命令，并建议用户手动重启。
- 单 EXE 内置 Python、渲染器、PresentMon 和收集到的运行库，改善中文输出。

## 使用

Windows x64，以管理员权限运行，需要支持 Vulkan 的显卡驱动。提供简单版（时间短）和增强版（时间长）。自动分辨率和强制全屏保留。

首次启动从内置模板生成配置，默认存放于 EXE 目录；目录不可写时回退到 %LOCALAPPDATA%\AutoGpuAffinity_Fix。

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
