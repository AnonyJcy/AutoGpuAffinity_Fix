# AutoGpuAffinity_Fix 1.2.2

维护与修改：**AnonyJcy**

项目：[AnonyJcy/AutoGpuAffinity_Fix](https://github.com/AnonyJcy/AutoGpuAffinity_Fix) · [下载](https://github.com/AnonyJcy/AutoGpuAffinity_Fix/releases)

## 本版变化

- 默认配置、日志、成绩与绑定备份迁入 %LOCALAPPDATA%\AutoGpuAffinity_Fix。
- 首次启动从内置模板生成 config.ini，显示具体路径，后续保留用户修改。
- 两种模式介绍标注时间短、时间长。

## 使用

Windows x64，以管理员权限运行，需要支持 Vulkan 的显卡驱动。提供简单版（时间短）和增强版（时间长）。自动分辨率和强制全屏保留。

首次启动从内置模板生成 %LOCALAPPDATA%\AutoGpuAffinity_Fix\config.ini，之后保留用户修改。成绩与备份存放于同一数据目录。

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
