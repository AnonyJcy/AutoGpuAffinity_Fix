# AutoGpuAffinity_Fix 1.2.1（界面更新）

维护与修改：**AnonyJcy**

项目：[AnonyJcy/AutoGpuAffinity_Fix](https://github.com/AnonyJcy/AutoGpuAffinity_Fix) · [下载](https://github.com/AnonyJcy/AutoGpuAffinity_Fix/releases)

## 本版变化

- 增强模式菜单改为“三轮综合测试”。
- 顶部加入维护者的“禁止以本软件参与任何形式的收费优化”提示。
- 这是 1.2.1 的独立界面更新构建，程序版本号仍为 1.2.1。

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
