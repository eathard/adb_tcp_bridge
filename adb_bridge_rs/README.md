# ADB TCP 桥 —— Rust 重构版

把本机 USB 设备 C 暴露成标准 TCP adb 设备，使局域网内的电脑 B 只需
`adb connect <A的局域网IP>:15555` 就能像普通网络设备一样使用 shell / push / pull / logcat。

设备 C（Buildroot / Luckfox 等嵌入式板）的 adbd 无法开启 TCP 监听
（`adb tcpip` 不生效），但它通过 USB 挂在本机 adb server 上。本桥在 A 上监听
TCP 端口并做 ADB 协议转换。

> 这是 Python 版（上级目录）的 Rust 重构，**功能等价、协议字节级兼容**。
> Python 版继续可用，两者可并行运行做对照。

## 快速开始

```bash
cargo build --release
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

66 项单元测试，其中 7 项是与 Python 版的**逐字节交叉校验**
（基准由 `gen_python_vectors.py` 从 Python 源码导出）：

```bash
python gen_python_vectors.py   # 重新生成基准向量
```

### 真实设备回归结果

以下均为**桥运行中**、用上级目录的现有测试脚本实测（脚本一行未改）：

| 测试 | 结果 |
|---|---|
| `consistency_test.py`（第一批 50 条） | **50/50 完全一致** |
| `consistency_test2.py`（第二批 50 条） | **50/50 完全一致** |
| `stress_test.py`（基础压测 12 项） | **12/12 通过**，吞吐 17 MB/s |
| 30MB push/pull 端到端 MD5 | 源文件/设备端/回拉**三者一致**，push 23.5 MB/s |
| 跨机：B 机经桥 vs A 机 USB 基线 | **50/50 完全一致** |

第二批覆盖：二进制边界、正则、Bash 高级语法、find/xargs、退出码透传、
大流量流控（3MB stdout / 1MB 无换行字节流 / 20000 字符单行）。

跨机比较需先归一化换行：A 上是 Rockchip ADB 31（CRLF），B 上是 ADB 34（LF），
这是客户端版本差异，与桥无关。

## 图形界面

提供两套界面实现，二选一构建：

| 界面 | 框架 | 构建 | 产物 |
|---|---|---|---|
| **Slint**（推荐） | 声明式保留模式 | `cargo build --release --features slint-ui` | `adb_bridge_slint.exe` |
| egui（旧） | 即时模式 | `cargo build --release --features gui` | `adb_bridge_rs_gui.exe` |

Slint 版界面描述在 `ui/main.slint`，业务逻辑在 `src/slint_ui.rs`，
两者通过属性与回调解耦，**桥核心一行都不涉及界面**。

### 为什么有两套

egui 是即时模式，轻量、集成简单，但视觉上限偏「工具级」，复现已确认的
HTML 原型有困难；Slint 是声明式保留模式，有专门的渲染引擎与主题系统，
视觉表现力更强，且只在数据变化时重绘，性能上同样是加分项。
因此界面迁移到 Slint，egui 版保留以便回退对比（`src/gui.rs` 未删除）。

### 本机构建 Slint 的两个前提

1. **必须用 crates.io 镜像**：本机直连下载 `.crate` 稳定超时；
   cargo 走本机代理又会拿到 502。镜像配置见 `.cargo/config.toml`。
2. **必须关闭 HTTP/2 多路复用**：同一代理下 `CARGO_HTTP_MULTIPLEXING=false`
   才不会 502（已写进配置文件与 `build-slint.bat`）。

Slint 依赖通过 **path 指向本机源码检出** `D:/slint-ui/slint`（含已编译产物），
避免重复下载上千个 crate。**换机器或发布时，把 Cargo.toml 里两行改成版本号**：
`slint = "1.16"` / `slint-build = "1.16"`。

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

## 现状与后续

已完成：协议层、日志、server、stream、session、命令行入口、端口自动清理、
配置持久化、两套图形界面、真实设备回归。
未完成：剪贴板、托盘常驻、开机自启、日志本地时区。