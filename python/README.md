# ADB TCP 桥 —— Python 版

在电脑 A 上运行，把本机 USB 设备 C 暴露成标准 TCP adb 设备，使局域网内的
电脑 B 只需 `adb connect <A的局域网IP>:15555` 即可使用
`shell` / `push` / `pull` / `logcat`。设备侧零改动、无需 root。

> 这是项目的**原始实现**，也是 Rust 版的参考实现与对照基线
> （Rust 版有 7 项单元测试与它做逐字节交叉校验）。
> 日常使用推荐 [Rust 版](../rust/README.md)（有图形界面、单文件 exe）。

## 快速开始

```bash
# 启动桥（启动时会自动清理占用该端口的残留进程）
python adb_tcp_bridge.py --listen-port 15555

# Windows 一键启动
start_adb_bridge.bat
```

电脑 B 上：

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
| `--server-port` | 5037 | 本机 adb server 端口 |
| `--no-kill-port` | 关 | 端口被占用时不结束占用进程，只报告 |
| `--debug-packets` | 关 | 打印每个 adb 包的收发 |

## 文件

| 文件 | 说明 |
|---|---|
| `adb_tcp_bridge.py` | 桥本体，**唯一必需的源文件** |
| `port_guard.py` | 端口占用检查与清理（也可独立运行） |
| `start_adb_bridge.bat` | Windows 一键启动脚本 |
| `build_nuitka.py` | Nuitka 打包入口（自动先修 SDK 缓存） |
| `fix_nuitka_sdk_cache.py` | 修补 Scons 的 MSVC 缓存，打包前置 |

## 免 Python 环境部署（Nuitka）

```bash
pip install nuitka scons pywin32 ordered-set zstandard
python build_nuitka.py
```

产物在 **`../releases/python/adb_tcp_bridge.dist/adb_tcp_bridge.exe`**
（该目录不入库，用于上传 GitHub Release）。

前置条件：Visual Studio Build Tools 2022（含 C++ 生成工具）。

> ⚠️ 若报 `FATAL: scons environment variable 'CC' is not set`：本机 `reg.exe`
> 被安全策略拦截，导致 `vcvars64.bat` 采集到的 SDK 信息残缺。
> `build_nuitka.py` 会在编译前自动调用 `fix_nuitka_sdk_cache.py` 修补。

## 工作原理（简述）

协议两侧不对称：

- **B 侧**：完整 adb 数据流 —— `CNXN` / `OPEN` / `WRTE` / `OKAY` / `CLSE`，
  带 24 字节包头（6 个 u32）
- **A 侧**：本机 adb server 的 smart socket（`host:transport:<serial>` +
  `shell:xxx`）—— 每个 stream 对应一条到 server 的连接，由桥做多路复用与流控

详细的协议不对称分析见 [软件说明书](../docs/软件说明书.md)。

## 安全提醒

设备 C 的 ADB shell 为 **root**，开放监听端口等同于把完整 root shell 暴露给局域网。
请在受信任的局域网内使用，用完及时关闭。

## 许可证

GPL-3.0-only，见 [LICENSE](../LICENSE)。
