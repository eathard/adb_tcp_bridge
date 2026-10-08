# ADB TCP 桥 —— Rust 重构版

把本机 USB 设备 C 暴露成标准 TCP adb 设备，使局域网内的电脑 B 只需
`adb connect <A的局域网IP>:15555` 就能像普通网络设备一样使用 shell / push / pull / logcat。

设备 C（Buildroot / Luckfox 等嵌入式板）的 adbd 无法开启 TCP 监听
（`adb tcpip` 不生效），但它通过 USB 挂在本机 adb server 上。本桥在 A 上监听
TCP 端口并做 ADB 协议转换。

> 这是 `python/` 目录里 Python 版的 Rust 实现，**功能等价、协议字节级兼容**。
>
> 本文是 Rust 版的说明；仓库根的 [README](../README.md) 是总入口。
> Python 版继续可用，两者可并行运行做对照。

## 快速开始

```bash
cd rust
cargo build --release --offline
./target/release/adb_bridge_rs.exe --listen-port 15555
```

电脑 B 上执行：

```bash
adb connect 172.16.0.106:15555
adb -s 172.16.0.106:15555 shell
```

## 参数

| 参数 | 默认 | 说明 |
|---|---|---|
| `--listen-port` | 15555 | 监听端口。**不可用 5555~5585**，那段是 adb server 的模拟器扫描区间 |
| `--listen-addr` | 0.0.0.0 | 监听地址 |
| `--serial` | 第一台在线设备 | 多设备时指定 |
| `--server-port` | 5037 | adb server 端口 |
| `--no-kill-port` | 关 | 端口被占用时不结束占用进程 |
| `--debug-packets` | 关 | 打印每个包的收发 |

### ⚠️ 端口不能用 5555~5585

本机 adb server 会扫描该段端口查找模拟器。桥若占用其中之一，
`adb devices` 会出现幽灵设备 `emulator-5554` 并可能抢占真实设备名。
程序启动时若检测到落在该区间会给出警告。

## 安全提醒

设备 C 的 ADB shell 为 **root**，开放监听端口等同于把完整 root shell 暴露给
局域网。日志中会记录每个接入的客户端 IP。

## 架构

```
电脑 B 的 adb
  → TCP 172.16.0.106:15555
  → 本桥（协议转换 + 多路复用 + 流控）
  → A 本机 adb server 127.0.0.1:5037
  → USB
  → 设备 C
```

模块划分：

| 文件 | 职责 |
|---|---|
| `proto.rs` | 包头打包/解包、CNXN banner、smart socket 命令 |
| `server.rs` | 到本机 adb server 的连接与查询 |
| `stream.rs` | 单条 stream 的流控、写入队列、批处理 |
| `session.rs` | 单个客户端会话：握手、多路复用、数据转发 |
| `log.rs` | 分级日志 + 环形缓冲（供 GUI 读取） |
| `main.rs` | 参数解析、监听、accept 循环、看门狗 |

### 并发模型

- 主线程：读客户端包，分发处理（OPEN/WRTE/OKAY/CLSE）
- 每条 stream：上行写线程 + 下行读线程
- 每条 stream 独立的 adb server 连接，互不干扰

### 关键实现约束（都是实际踩过的坑）

1. **CNXN banner 必须带 `ro.product.*`**，否则客户端不协商 `shell_v2`，
   退化成老 shell + PTY → stderr 与 stdout 合并、退出码丢失、TTY 产生 ANSI 颜色。
2. **banner 末尾不得附加 NUL**，否则 features 变成 `"shell_v2\0"` 而匹配不到。
3. **包头是 24 字节（6 个 u32）**，第 5 个字段是 payload 字节累加和。
   漏掉它每包少 4 字节，接收端解析完全错位。
4. **OKAY 的 arg0/arg1 都要匹配**，分别是双方的 local id。
   只匹配一个会在拥塞时误判，大文件 pull 卡死。
5. **上行必须队列化 + 独立写线程**，主线程直接 `sendall` 会被大文件写入阻塞。
   队列可合并多块提吞吐，但**每块仍须单独回一个 OKAY**（ADB 按包确认）。
6. **下行读线程结束时必须发 CLSE**，否则客户端等不到结束信号，
   每条命令都要卡到 adb 客户端自身超时（实测约 90 秒）。
7. **不要用心跳保活**。历史上曾误判超时来源而加入，结果破坏流控与消息边界，
   反而让原本正常的命令被断。

## 测试

```bash
cargo test --release --offline
```

**93 项单元测试全绿**，其中 7 项是与 Python 版的**逐字节交叉校验**
（基准由 `gen_python_vectors.py` 从 Python 源码导出）：

```bash
python tools/gen_python_vectors.py   # 重新生成基准向量
```

### 真实设备回归结果

以下均为**桥运行中**、用仓库 `tests/` 目录里的脚本实测。各脚本名在归档时保持不变：

| 测试 | 结果 |
|---|---|
| `consistency_test.py`（第一批 50 条） | **50/50 完全一致** |
| `consistency_test2.py`（第二批 50 条） | **50/50 完全一致** |
| `stress_test.py`（基础压测 12 项） | **12/12 通过**，吞吐 17.6 MB/s（GUI 常开时） |
| `stress_advanced_win.py`（进阶压测 6 项） | **6/6 通过**，30MB/50MB push-pull 的 MD5 均一致 |
| 跨机 `bridge_check_b.py`（B 经桥访问只接 USB 的 C） | **16/16 通过** |
| 20 分钟长时连接（`ping_stability_b.py`） | 1200/1200 往返，**0% 丢包**，通道零中断 |

第二批覆盖：二进制边界、正则、Bash 高级语法、find/xargs、退出码透传、
大流量流控（3MB stdout / 1MB 无换行字节流 / 20000 字符单行）。

跨机比较需先归一化换行：A 上是 Rockchip ADB 31（CRLF），B 上是 ADB 34（LF），
这是客户端版本差异，与桥无关。

### 反复重启稳定性

`tests/restart_stability_a.py`（CLI 启停 + 跨机功能验证）、`tests/restart_gui_a.py`
（GUI 进程反复启停）、`tests/kill_recovery_a.py`（`taskkill /F` 强杀后恢复）：

| 维度 | 轮数 | 结果 |
|---|---|---|
| CLI 启停 + 跨机功能验证 | 30 | **30/30**，启动 0.51~2.72s、停止 <0.01s |
| GUI 进程反复启停 | 15 | **15/15**，窗口出现 0.25~0.50s，无耗时漂移 |
| 强杀恢复（`taskkill /F`） | 12 | **12/12**，强杀 ~1.0s 端口即释放、重启 0.5s 恢复 |
| 配置持久化 | 8 | **8/8**，`config.json` 内容 MD5 全程一致 |

**共 57 轮零失败。** 每轮都由电脑 B 经桥真实执行 `adb connect` + `get-state` + `shell`，
不是只验证「端口起来了」。

长时测试纪律（都踩过）：

- `remote_test.py` 的 `exec_command` 硬编码 900秒超时，跑 20 分钟必被掐断。
- 跑长任务用 `run_remote_bg.py`（`setsid nohup` 后台跑 + 结果文件），SSH 断开不影响远端。
  单纯 `nohup ... &` 不够——子进程仍持有 SSH 通道 fd，paramiko 的 `read()` 会一直等通道关闭。
- **`adb shell ping` 的 RTT 不是桥的性能**：ping 由设备发起，ICMP 不经过桥。
  它验证的是「adb 通道长时间零中断」，转发性能看压测的 MB/s 与大文件 push-pull。


## 图形界面

提供两套界面实现，二选一构建：

| 界面 | 框架 | 构建 | 产物 |
|---|---|---|---|
| **Slint**（推荐） | 声明式保留模式 | `cargo build --release --features slint-ui --offline` | `adb_bridge_slint.exe` |
| egui（旧） | 即时模式 | `cargo build --release --features gui --offline` | `adb_bridge_rs_gui.exe` |

Slint 版界面描述在 `ui/main.slint`（6 个页面：控制台 / 会话 / 运行日志 /
参数设置 / 诊断工具 / 关于软件），业务逻辑在 `src/slint_ui.rs`，
两者通过属性与回调解耦，**桥核心一行都不涉及界面**。

### 为什么有两套

egui 是即时模式，轻量、集成简单，但视觉上限偏「工具级」，复现已确认的
HTML 原型有困难；Slint 是声明式保留模式，有专门的渲染引擎与主题系统，
视觉表现力更强，且只在数据变化时重绘，性能上同样是加分项。
因此界面迁移到 Slint，egui 版保留以便回退对比（`src/gui.rs` 未删除）。

### 界面实现的四条硬约束

1. **std-widgets 控件走全局 `Palette`，不认我们自定义的 `Pal`**。
   自绘 `Text` 显式写 `color: Pal.text` 没问题，但 `CheckBox` / `LineEdit` /
   `Button` 走 `FluentPalette.foreground`，跟随 `Palette.color-scheme`。
   而 `Palette` 的 12 个 brush 属性在 Slint 1.16 里**全是只读**，唯一可写的是
   `color-scheme`——只能整体切明暗模式，无法逐项赋色：

   ```slint
   export component MainWindow inherits Window {
       init => { Palette.color-scheme = ColorScheme.dark; }
   }
   ```

2. **`alignment` 只管主轴**，交叉轴上非拉伸子元素一律左对齐。圆形节点、图标、
   竖线、胶囊标签要落在中轴上，得用左右两段 `horizontal-stretch: 1` 的空
   Rectangle 夹住。

3. **自定义组件作为 HorizontalLayout 子元素会被拉伸**到整个容器宽度，组件内部
   写死的 `width` 失效——需显式加 `horizontal-stretch: 0`。

4. **`.slint` 语法错误 4 秒就报出来，Rust 错误要 5 分钟**。调界面时值得先故意
   编译一次清语法错误。这个版本没有 `RowLayout` / `ColumnLayout`，
   只有 `HorizontalLayout` / `VerticalLayout`。

### 日志列表的滚动：必须用 ListView，不要自己拼 Flickable

日志页要「自动滚动到最底部」，**正确做法是标准 `ListView` + 一个定时器驱动
`viewport-y`**。ListView 自带滚动条与滚轮处理，不要用Flickable/ScrollView
自绘——那套会同时丢掉滚动条、丢掉滚轮，还让自动滚动完全失效。

根因在Slint 源码 `internal/compiler/passes/flickable.rs`（约185 行）：
计算 `viewport-height` 时会 `.filter(|x| x.borrow().repeated.is_none())`
**跳过 `for` 循环产生的子元素**，且只统计类型是 layout 的直接子元素。
若把 `for` 循环包在 `TouchArea` 里，Flickable 的直接子元素就不是 layout，
于是 `viewport-height == height`，可滚动距离恒为 0——自动滚动每次都把列表
**拽回顶部**，表现为「勾了也没用」。

滚动方向约定（来自 `fluent/scrollview.slint` 的 scrollbar）：**向下滚是往负方向**，
`0` 是顶、`-(viewport-height - visible-height)` 是底。写反了列表会往上跑。

另外两个细节：

- **只在 tick 变化时滚**。定时器无条件每 100ms 推到底，会把用户正在翻的
  历史日志反复拽回（表现为「滚太快、日志一行行被顶没」）。tick 由 Rust 侧在
  日志内容**真的变化**时递增（`rows_changed`）。
- **不要用一次性 Timer + `restart()`**：`restart()` 只重置计时，不会把
  `running` 从 false 拉起来，Timer 根本不触发。保持 `running: true` 的重复
  Timer，用「tick 是否变化」当触发条件。

### 本机构建 Slint 的两个前提

1. **cargo 一律加 `--offline`**。本机 588 个依赖已全部在缓存里，联网反而会卡在
   `index.crates.io` 响应上（历史上反复重试失败的真正原因，就是没加 `--offline`
   而 cargo 在干等网络）。
2. Slint 依赖通过 **path 指向本机源码检出** `D:/slint-ui/slint`（含已编译产物），
   避免重复编译上千个 crate。**换机器或发布时**，把 `Cargo.toml` 里两行改成版本号：
   `slint = "1.16"` / `slint-build = "1.16"`。

已禁用 `accessibility` 特性（依赖数 1190 → 588）。首次编译约 16 分钟，
之后增量 4~5 分钟。

### 测 Windows GUI 的三个硬约束

Slint 是整窗自绘，**Win32 枚举不到子控件句柄**，只能按屏幕坐标点击，因此：

1. **点击/截图前必须 `SetProcessDpiAwareness(2)`**。本机 2560×1600 / 缩放 150%，
   不设则坐标被虚拟化，出现「假布局 bug」。
2. **`SetForegroundWindow` 常被前台锁定策略拦掉**，点击会落到别的窗口
   （曾误点到 IDE 的输入框）。用 `SetWindowPos(HWND_TOPMOST)` 强制置顶。
   `ui_click.py` 已处理这点。
3. 坐标要从截图精确换算：真实像素 = 显示坐标 × (窗口像素宽 / 显示图宽) + 窗口原点。
   目测常差 40~50px（按钮只有 46px 高），**点空了不报错，只会「没反应」**。

配套工具（都在 `tools/` 目录）：

| 脚本 | 用途 |
|---|---|
| `tools/ui_click.py` | 按**窗口相对坐标**点击，可顺带截图（会强制置顶） |
| `tools/win_shot.py` | 截取窗口图像 |
| `tools/win_act.py` | 移动窗口到屏幕中央 |
| `tools/fw_allow.py` | 自动点掉 Win11 防火墙弹窗（`--xy X,Y` 或 `--wait N`） |

> 防火墙弹窗的类名是 `Shell_SystemDialogProxy`，`GetWindowRect` 恒返回 0×0、
> 枚举子窗口也找不到按钮（XAML 合成器绘制）。本机有管理员权限，更省事的办法是
> 直接 `netsh advfirewall firewall add rule` 建好规则，弹窗就不再出现。

## 许可证

本项目采用 **GPL-3.0-only**（见 `LICENSE`）。

选择 GPLv3 与 Slint 的授权有关：Slint 提供 GPLv3 / 免版税 / 商业三种许可，
- 走 **GPLv3**：免费，但要求应用整体开源；
- 走 **免版税**：桌面应用免费且可闭源，代价是保留一行 `Made with Slint` 归属声明。

本项目选择前者，因此**整体以 GPLv3 开源**。

## 已知限制

- 设备热插拔不支持：serial 在启动时确定。
- 日志时间显示的是 UTC（Rust 标准库无时区转换，未引入 chrono）。
- 「复制连接命令」目前只写入日志，尚未真正写入系统剪贴板。
- 托盘常驻、开机自启未实现；关闭窗口即退出。

## 目录位置

本目录是仓库里的 `rust/`。同级还有：

| 目录 | 内容 |
|---|---|
| `../python/` | Python 版实现（参考实现与对照基线） |
| `../tests/` | 回归测试脚本与留档结果，见 `../tests/README.md` |
| `../docs/` | 软件说明书、部署指南、界面原型 |
| `../releases/` | 编译产物（不入库，用于 GitHub Release） |

## 现状与后续

**已完成**：协议层、日志、server、stream、session、命令行入口、端口自动清理、
配置持久化、两套图形界面、真实设备回归、自动滚动日志页。

**未完成**（不影响使用）：剪贴板、「复制连接命令」目前只写日志、托盘常驻、
开机自启、日志本地时区。