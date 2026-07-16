# AutoGpuAffinity (显卡中断绑定自动测试工具)

[![Downloads](https://img.shields.io/github/downloads/valleyofdoom/AutoGpuAffinity/total.svg)](https://github.com/valleyofdoom/AutoGpuAffinity/releases)

> **修改版 By: Anony**  
> (修复了 Windows Terminal 吞行导致表格显示不全的 Bug，并修复了真全屏分辨率参数失效的问题)

<img src="/assets/img/example-output.png" width="1000">

> [!IMPORTANT]
> 免责声明：本人不对您的电脑造成的任何损坏负责。在测试期间重启 GPU 驱动程序时，可能会出现显卡驱动无响应的风险。一个可能的解决方案是在 BIOS 中将 PCIe 链路速度设置为主板支持的最大速度（而不是 Auto）。

## 使用方法

```text
AutoGpuAffinity
GitHub - https://github.com/valleyofdoom

用法: AutoGpuAffinity [-h] [--config <配置文件>] [--analyze <CSV文件夹>] [--apply-affinity <核心编号>]

可选参数:
  -h, --help            显示此帮助信息并退出
  --config <config>     指定配置文件的路径
  --analyze <csv目录>   解析之前基准测试生成的 CSV 文件数据
  --apply-affinity <cpu>
                        为显卡驱动程序分配单个核心的亲和性 (绑定中断)
```

- 如果要使用 xperf 记录 DPC/ISR 日志，必须安装 Windows ADK 中的 Windows Performance Toolkit (这一步是完全可选的)

  - [适用于 Windows 8.1+ 的 ADK](https://docs.microsoft.com/zh-cn/windows-hardware/get-started/adk-install)
  - [适用于 Windows 7 的 ADK](http://download.microsoft.com/download/A/6/A/A6AC035D-DA3F-4F0C-ADA4-37C8E5D34E3D/setup/WinSDKPerformanceToolKit_amd64/wpt_x64.msi)

- 如果有超频，请在整个测试过程中使用 MSI Afterburner 保持超频设置
  - 将所需的超频设置保存到配置文件中（例如 profile 1）
  - 在 `config.ini` 中配置 Afterburner 的路径和要加载的 profile

- 运行 **AutoGpuAffinity.exe**，准备好开始测试后按回车键

- 工具逐一测试完每个核心后，GPU 的亲和性将被重置为 Windows 默认值，并显示包含测试结果的汇总表。**绿色的数值**表示该指标下的最高值（最好），**黄色的数值**表示第二高的值。xperf 的报告可以在对应的 session 目录中找到。

## 解析历史记录 (Analyze)

随时可以通过将包含 CSV 的文件夹路径传给 `--analyze` 参数来解析旧的测试日志（见下例）。如果用户在结果显示时意外关闭了窗口，或者遇到了显示 Bug 导致看不全，此功能会非常有用。

```bat
.\AutoGpuAffinity.exe --analyze ".\captures\AutoGpuAffinity-170523162424\CSVs\"
```

## 独立基准测试 (Standalone Benchmarking)

如果在 `config.ini` 中将 **custom_cpus** 设置为单个核心，AutoGpuAffinity 也可以作为一个常规的独立跑分工具使用。如果你通常不配置显卡驱动的中断亲和性，可以将数组设置为 `[0]`，因为图形内核通常默认运行在 CPU 0 上。请记住，一旦测试结束，AutoGpuAffinity 会将中断分配策略重置为默认的 Windows 状态（即没有指定特定的核心亲和性），因此，如果在日常使用中你有自己的一套绑定策略，请不要忘记在测试结束后重新手动配置回去。
