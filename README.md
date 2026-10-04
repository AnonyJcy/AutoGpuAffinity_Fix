# AutoGpuAffinity_Fix 1.1.4

维护与修改：**AnonyJcy**

项目：[AnonyJcy/AutoGpuAffinity_Fix](https://github.com/AnonyJcy/AutoGpuAffinity_Fix) · [下载](https://github.com/AnonyJcy/AutoGpuAffinity_Fix/releases)

## 本版变化

- 检测全屏呈现被刷新率限制的情况，实际验证状态条合成路径；解除失败则停止排名。
- 恢复测试前的原始显卡亲和性策略，保留完整 Max、Avg、Min、STDEV、百分位和 Low 成绩。
- 隔离 PresentMon 会话，拒绝失败会话排名，修复日志文件影响 CSV 分析的问题。

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
