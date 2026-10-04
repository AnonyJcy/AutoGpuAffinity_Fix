# AutoGpuAffinity_Fix 1.2.4

维护与修改：**AnonyJcy**

项目：[AnonyJcy/AutoGpuAffinity_Fix](https://github.com/AnonyJcy/AutoGpuAffinity_Fix) · [下载](https://github.com/AnonyJcy/AutoGpuAffinity_Fix/releases)

## 本版变化

- 重复进程按软件合并，按 CPU 占用排序，同占用时比较内存，最多显示前 10 个软件。
- 简化操作为输入 1 关闭清单中的软件，回车跳过。
- 保留正常退出与进程身份复核。

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
