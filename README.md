# adb TCP 桥

让局域网内的电脑 B 用标准 `adb` 命令访问只连着 USB 的嵌入式设备。

设备 C（Luckfox RK 板）的 adbd 无法开启 TCP 监听，所以它自己不能被网络访问。本工具在电脑 A 上把A 通过 USB 连着的设备 C 暴露成一台"网络 adb 设备"，B 上 `adb connect A:15555` 即可正常使用 `shell` / `push` / `pull` / `logcat` 等功能。**设备 C 零改动、无需 root。**

```
电脑 B                 电脑 A                      设备 C
(Ubuntu)         (Windows/Linux)              (Luckfox RK 板)
   │                   │                            │
   │  局域网 TCP       │        USB RNDIS           │
   └──────────────────>│<───────────────────────────┘
  172.16.0.101      172.16.0.106              192.168.123.100
                        │
                  adb_tcp_bridge.py
                  监听 0.0.0.0:15555
                        │
                  adb server :5037
```

## 快速开始

```bash
# 1. 电脑 A：启动桥（启动时会自动清理占用该端口的残留进程）
python adb_tcp_bridge.py --listen-port 15555

# 2. 电脑 B：连接并使用
adb connect 172.16.0.106:15555
adb -s 172.16.0.106:15555 shell
```

免Python 的部署方式：

```bash
# 编译（一次性）
python build_nuitka.py
# 运行
adb_tcp_bridge.dist\adb_tcp_bridge.exe --listen-port 15555
```

> 前置条件：本机已安装 adb 且在 PATH 中。设备 C 的 adb 驱动由厂商定制，需用厂商提供的 adb。

## 文档

| 文档 | 内容 |
|---|---|
| [软件说明书](docs/软件说明书.md) | 原理（两侧协议不对称分析）、参数、工作原理、技术规格、故障处理、安全说明 |
| [部署指南](docs/部署指南.md) | 6 步部署流程、端到端验收、卸载、参数速查、多设备场景 |

## 文件一览

### 运行时

| 文件 | 说明 |
|---|---|
| `adb_tcp_bridge.py` | 桥本体，唯一必需的源文件 |
| `port_guard.py` | 端口占用检查与清理（也可单独运行） |
| `start_adb_bridge.bat` | Windows 一键启动脚本 |

### 构建

| 文件 | 说明 |
|---|---|
| `build_nuitka.py` | Nuitka 编译入口（自动先修 SDK 缓存） |
| `fix_nuitka_sdk_cache.py` | 修补 Scons 的 MSVC 缓存，编译前置 |

### 测试与诊断

| 文件 | 说明 |
|---|---|
| `consistency_test.py` | 50 条命令 USB 与桥的逐条比对（stdout/stderr/退出码） |
| `stress_test.py` | 基础压测 12 项 |
| `stress_advanced.py` | 进阶压测 11 项（大文件/并发/半开连接/强杀等） |
| `remote_test.py` | 上传测试脚本到 B 执行并回传输出 |
| `diag_features.py` | 对比两个设备在 adb server 眼中的 features |
| `diag_exec.py` | exec-out 并发与尺寸诊断 |
| `weaknet_proxy.py` / `weaknet_run.py` / `weaknet_client.py` | 应用层弱网代理与五档编排 |

## 常用参数

| 参数 | 默认 | 说明 |
|---|---|---|
| `--listen-port` | 5555 | 监听端口。**建议 15555**，避开 5555~5585（adb server 会把那段当模拟器端口） |
| `--listen-addr` | 0.0.0.0 | 监听地址 |
| `--serial` | 第一台在线设备 | 多设备时指定 |
| `--no-kill-port` | 关 | 端口被占用时不自动清理，只报告 |
| `--debug-packets` | 关 | 打印每个 adb 包的收发，排障用 |

## 安全提醒

本工具会在局域网内开放一个可执行**完整 root shell**的端口。任何能访问该主机的人都等同于拥有设备的 root 权限。请在受信任的局域网内使用，用完及时关闭（关闭桥进程并删除防火墙规则即可）。

## 已知限制

- 同一进程只服务一台设备；多台设备需起多个实例（用不同 `--listen-port` 和 `--serial`）
- 跨网段/NAT 需自行配置端口映射
- 不支持设备管理、权限控制或传输加密