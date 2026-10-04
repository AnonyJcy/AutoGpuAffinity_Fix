# AutoGpuAffinity_Fix

[![Windows checks](https://github.com/AnonyJcy/AutoGpuAffinity_Fix/actions/workflows/check.yml/badge.svg)](https://github.com/AnonyJcy/AutoGpuAffinity_Fix/actions/workflows/check.yml)
[![Downloads](https://img.shields.io/github/downloads/AnonyJcy/AutoGpuAffinity_Fix/total.svg)](https://github.com/AnonyJcy/AutoGpuAffinity_Fix/releases)

维护与修改：**AnonyJcy** · 当前版本：**1.2.5**

在 Windows 上实际测试不同 CPU 核心的显卡中断亲和性，保留完整帧率成绩，并生成显卡、鼠标键盘及计时器的候选绑定方案。针对 P106 等显卡驱动重启后 Vulkan 查询失败的问题，使用独立进程重新加载 Vulkan 并核验实际渲染卡。

## 下载与使用

从 [Releases](https://github.com/AnonyJcy/AutoGpuAffinity_Fix/releases) 下载 EXE 或 ZIP。需要 **Windows 10/11 x64、管理员权限，以及正常支持 Vulkan 的显卡驱动**。

单个 EXE 内置 Python、所需模块、渲染器、PresentMon 和收集到的 C++ 运行库，无需另外安装 Python。Windows 系统组件和显卡驱动仍由系统提供。

```text
1. 简单版：按顺序单核心测试（时间短）
2. 增强版：三轮综合测试（时间长）
```

简单版依次测试每个逻辑核心。增强版依次进行普通单核心、真实超线程配对和乱序单核心测试；未启用超线程时跳过配对轮。每项默认预热 5 秒、正式采集 10 秒，默认测试全部逻辑核心。整体耗时还包括驱动重启、恢复检查和渲染器探测。

自动采用主显示器的物理分辨率并强制无边框全屏。若呈现路径被刷新率限制，会实测验证状态条合成路径；无法验证解除限制时停止，避免使用锁帧结果推荐核心。会话记录实际渲染显卡、采集 PID、进程亲和性与中断掩码。

测试会重启目标显卡驱动，屏幕可能暂时闪烁。请先保存工作；程序退出或失败时尝试恢复目标显卡测试前的亲和性策略。

## 辅助关闭后台软件

跑分前动态识别当前用户会话中的桌面软件及其后台子进程，重复进程合并成一个软件。按合计 CPU 占用排序，同占用时比较内存，最多显示前 10 个软件，界面只显示名字：

```text
是否辅助关闭后台软件？
当前占用较高的软件：
  QQ
  Chrome 浏览器
请先保存工作，仅请求正常退出。
输入 1 全部关闭上述软件；回车跳过：
```

输入 `1` 只请求关闭清单中的软件；回车跳过。保留保存提示，不强制杀进程，不自动重开。排除 Windows 服务、系统目录、常见安全/驱动/同步组件以及本程序和启动它的进程；没有可识别窗口来源的独立后台进程不自动处理。

## 配置和成绩位置

配置模板内置在 EXE，首次启动自动生成 `%LOCALAPPDATA%\AutoGpuAffinity_Fix\config.ini`，以后保留用户修改。首次运行会提示具体路径。默认成绩、日志和绑定备份位于同一数据目录下的 `captures` 会话文件夹。

配置支持 UTF-8 和 UTF-8 BOM；控制台与重定向日志使用 Unicode/UTF-8。

```ini
[settings]
gpu=auto
auto_resolution=true
force_fullscreen=true
cache_duration=5
benchmark_duration=10
custom_cpus=[]
```

将 `custom_cpus` 改为 `[0]` 可先做短时验证。配置还可指定显卡、核心范围、呈现路径和可选的 xperf/MSI Afterburner。多张兼容显卡使用相同内置场景短测选择，只有一张时不声称完成跨卡比较。

## 推荐与设备绑定

每项保留 Max、Avg、Min、STDEV、百分位和 Low 等 12 项统计。使用真实物理核心拓扑，避开 CPU 0 所属的整个物理核心：

- **显卡**：Max、Avg、Min 的相对名次等权综合。
- **鼠标键盘**：优先比较 1% Low，每项至少 500 个有效帧。
- **计时器**：优先比较 FPS STDEV，越小越优先。
- **超线程**：只选择真实同一物理核心、偶数起始的相邻配对，例如 2+3；未启用超线程时选择单个真实物理核心。

增强版综合各轮保守成绩，并纳入双线程联合测试。候选不足、会话不完整、机器或拓扑变化时不应用自动方案。超过 64 个逻辑处理器的系统需要处理器组支持，当前版本明确拒绝测试。

跑分后显示设备和核心方案。输入 **确认绑定** 才写入 `DevicePolicy=4` 和 `AssignmentSetOverride`；写入前保存原值，写入后读回核验，失败时回滚。**写入策略后建议保存工作并手动重启，再验证实际效果。**

USB 键鼠通常映射到主控制器，绑定会影响该控制器上的所有设备。多个 HID 接口合并到真实中断目标，虚拟输入和不支持的路径不猜测绑定。

系统计时器/HPET 查询 allocated、filtered、basic、boot、forced 配置，只有完整读取的已分配 IRQ 才进入自动绑定。资源需求或启动 IRQ 不代表运行时已分配。未找到可靠目标时明确跳过，不改变 HPET/QPC 或启动计时器设置。

推荐由显卡 FPS 指标推导，**不等于实际键鼠延迟或计时器中断性能测试**。短测名次不保证长期最优，注册表读回也不能证明重启后实际 IRQ 已迁移。

## 常用命令

```text
AutoGpuAffinity.exe --mode enhanced
AutoGpuAffinity.exe --prepare-background
AutoGpuAffinity.exe --list-devices
AutoGpuAffinity.exe --config "自定义配置.ini"
AutoGpuAffinity.exe --analyze "会话目录\CSVs"
AutoGpuAffinity.exe --bind-from "会话目录"
AutoGpuAffinity.exe --restore-affinity "会话目录\affinity-backup-时间.json"
```

`--non-interactive` 用于无人值守测试，跳过后台软件关闭，不应用最终设备绑定。应用和恢复策略需要交互确认。程序不会自动重启电脑。

## 源码构建与验证

Windows x64 上使用 Python 3.12，运行 `BUILD.cmd` 安装依赖并构建；也可执行：

```powershell
python -m pip install -r requirements.txt pyinstaller
python -m unittest discover -s tests -v
.\build.ps1
```

产物为 `build\AutoGpuAffinity\AutoGpuAffinity.exe`。`START.cmd` 用于以管理员权限运行源码。GitHub Actions 提供 Windows 检查和手动发布流程。

本机验证覆盖 P106-100、20 个逻辑处理器的 Mode 2 全部 50 项，以及后续版本的实际 EXE 配置、界面、设备查询和后台辅助退出。测试版本与范围见 [VALIDATION.md](VALIDATION.md)，版本变化见 [CHANGELOG.md](CHANGELOG.md)。自动化检查与硬件实测分别记录，未在所有硬件上验证。

## 作者与许可

项目：[AnonyJcy/AutoGpuAffinity_Fix](https://github.com/AnonyJcy/AutoGpuAffinity_Fix)。基于 [valleyofdoom/AutoGpuAffinity](https://github.com/valleyofdoom/AutoGpuAffinity)，IRQ 枚举参考 [spddl/GoInterruptPolicy](https://github.com/spddl/GoInterruptPolicy)。

本项目继续采用 [GPLv3](LICENSE)，第三方许可及署名见 [NOTICE.md](NOTICE.md)。启动界面保留维护者的“禁止以本软件参与任何形式的收费优化”提示。
