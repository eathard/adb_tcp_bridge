# ADB TCP 桥

把**只连着 USB** 的 adb 设备，暴露成局域网内任何机器都能用标准 `adb` 命令访问的网络设备。

设备侧**零改动、无需 root**。

---

## 它解决什么问题

嵌入式板（如 Luckfox RK，Buildroot 系统）的 `adbd` 无法开启 TCP 监听 ——
`adb tcpip` 不生效，所以设备本身不能被网络访问。

本工具跑在插着 USB 的那台电脑（A）上：对外监听一个 TCP 端口，对内连本机
adb server，在两侧做 ADB 协议转换与多路复用。局域网里的另一台电脑（B）
只要 `adb connect A:15555`，就能像访问普通网络设备一样使用
`shell` / `push` / `pull` / `logcat` / `install` 等全部功能。

![电脑 B 经局域网 TCP 连接电脑 A，电脑 A 经 USB 连接设备 C](docs/images/topology.svg)

> 上图与软件「**关于软件**」页面里的拓扑图是同一版式，由
> [`docs/images/gen_topology.py`](docs/images/gen_topology.py) 生成。
>
> A↔C 这一段是 **USB 上的 ADB 批量传输**，不是网络。
> 设备有没有网卡、网卡 IP 是多少，都与桥无关 —— 这正是本软件存在的意义。
> （这块板另外有个 RNDIS 网卡 `usb0`，但那是设备自己的上行，桥一条包都不走它。）

---

## 两个实现，同一套协议

| | Python 版 | Rust 版 |
|---|---|---|
| 位置 | [`python/`](python/) | [`rust/`](rust/) |
| 形态 | 单文件脚本 / Nuitka 打包 exe | `cargo build` 产出单文件 exe |
| 界面 | 无（命令行） | **有**（Slint，6 个页面） |
| 状态 | 参考实现与对照基线 | **推荐**，功能等价、协议字节级兼容 |

两版可并行运行做 A/B 对照。Rust 版有 7 项单元测试是与 Python 版的**逐字节交叉校验**，
确保重构没有改变协议行为。

---

## 快速开始

### Rust 版（推荐）

```bash
cd rust
cargo build --release --features slint-ui --offline
./target/release/adb_bridge_slint.exe          # 图形界面，双击即可
# 或命令行版：
cargo build --release --offline
./target/release/adb_bridge_rs.exe --listen-port 15555
```

### Python 版

```bash
cd python
python adb_tcp_bridge.py --listen-port 15555
```

免 Python 环境部署（Nuitka 打包）：

```bash
cd python
python build_nuitka.py
../releases/python/adb_tcp_bridge.dist/adb_tcp_bridge.exe --listen-port 15555
```

### 电脑 B 上连接

```bash
adb connect 172.16.0.106:15555
adb -s 172.16.0.106:15555 shell
```

> **前置条件**：电脑 A 已安装 adb 且在 PATH 中。设备 C 的 adb 驱动由厂商定制
> （Rockchip 改版），必须用厂商提供的 adb —— 官方 platform-tools 认不了这块板。

> ⚠️ **首次运行前务必确认防火墙**：若 A 本机能连而 B 连不上，多半是入站放行规则的
> 端口字段写错了。用
> `netsh advfirewall firewall show rule name=<规则名> dir=in verbose`
> 核对本地端口确实是 **15555**。
> 判据：`连接超时` = 被防火墙丢包；`连接被拒绝` = 没在监听。

---

## 目录结构

```
.
├── README.md              总入口（本文件）
├── LICENSE                GPL-3.0-only
├── CHANGELOG.md           版本记录
├── CONTRIBUTING.md        参与贡献
│
├── python/                Python 版实现
│   ├── adb_tcp_bridge.py      桥本体，唯一必需的源文件
│   ├── port_guard.py          端口占用检查与清理
│   ├── start_adb_bridge.bat   Windows 一键启动
│   ├── build_nuitka.py        Nuitka 打包入口
│   └── fix_nuitka_sdk_cache.py  修补 Scons MSVC 缓存（打包前置）
│
├── rust/                  Rust 版实现
│   ├── Cargo.toml
│   ├── src/                   桥核心 + 两套入口（CLI / Slint）
│   ├── ui/main.slint          Slint 界面描述
│   ├── tests/                 Rust 单元测试与交叉校验基准
│   ├── tools/                 Windows GUI 自动化、基准生成等辅助脚本
│   └── README.md              架构、协议约束、界面约束、测试结果
│
├── tests/                 回归测试（Python，全部脚本平铺，见 tests/README.md）
│   ├── consistency_test*.py   一致性与协议正确性
│   ├── stress_*.py            压力与边界
│   ├── *_b.py / remote_*.py   跨机测试
│   ├── restart_*_a.py         反复重启 / 强杀恢复稳定性
│   ├── weaknet_*.py           弱网模拟
│   ├── baseline_usb.json      USB 直连输出基线
│   └── results/               留档的稳定性测试结果日志
│
├── docs/                  文档
│   ├── 软件说明书.md           原理、参数、技术规格、故障处理、安全说明
│   ├── 部署指南.md             部署流程、验收、卸载、参数速查
│   └── prototypes/            界面原型 HTML（迭代界面时的参考稿）
│
└── releases/              编译产物（**不入库**，上传 GitHub Release）
    ├── adb_bridge_slint.exe   Rust 版 · Slint 图形界面
    ├── adb_bridge_rs.exe      Rust 版 · 命令行
    └── python/                Python 版 Nuitka 打包产物
```

---

## 文档

| 文档 | 内容 |
|---|---|
| [Rust 版说明](rust/README.md) | 架构、模块划分、协议实现的 7 条硬约束、界面实现约束、完整测试结果 |
| [软件说明书](docs/软件说明书.md) | 两侧协议不对称分析、参数、工作原理、技术规格、故障处理、安全说明 |
| [部署指南](docs/部署指南.md) | 6 步部署流程、端到端验收、卸载、参数速查、多设备场景 |
| [回归测试说明](tests/README.md) | 每个脚本的用途、前置条件、运行方式与基线结果 |
| [界面原型](docs/prototypes/) | 界面设计过程稿，后续迭代界面时的参考 |

---

## 常用参数

| 参数 | 默认 | 说明 |
|---|---|---|
| `--listen-port` | **15555** | 监听端口。**不可用 5555~5585**（adb server 会把那段当模拟器端口，产生 `emulator-5554` 幽灵设备） |
| `--listen-addr` | 0.0.0.0 | 监听地址 |
| `--serial` | 第一台在线设备 | 多设备时指定 |
| `--server-port` | 5037 | 本机 adb server 端口 |
| `--no-kill-port` | 关 | 端口被占用时不自动清理，只报告 |
| `--debug-packets` | 关 | 打印每个 adb 包的收发，排障用 |

---

## 测试

```bash
# Rust 单元测试（93 项，含 7 项与 Python 版的逐字节交叉校验）
cd rust && cargo test --release --offline

# 回归测试（需真实设备 + 桥运行中，详见 tests/README.md）
cd tests && python consistency_test.py
```

已验证的基线结果：

| 测试 | 结果 |
|---|---|
| 一致性第一批（USB vs 桥，50 条） | **50/50** |
| 一致性第二批（B 经桥 vs A 的 USB 基线，50 条） | **50/50** |
| 基础压测 12 项 | **12/12**，17.6 MB/s |
| 进阶压测（30MB / 50MB push-pull MD5） | **6/6**，三者一致 |
| 跨机端到端（B 经桥访问只接 USB 的 C） | **16/16** |
| 20 分钟长时连接 | 1200/1200 往返，**0% 丢包** |
| 反复重启 / 强杀恢复 | **57 轮零失败** |

---

## 安全提醒

本工具会在局域网内开放一个可执行**完整 root shell** 的端口。任何能访问该主机的
人都等同于拥有设备的 root 权限。请在受信任的局域网内使用，用完及时关闭
（关闭桥进程并删除防火墙规则即可）。

日志中会记录每个接入的客户端 IP。

---

## 已知限制

- 同一进程只服务一台设备；多台设备需起多个实例（不同 `--listen-port` 和 `--serial`）
- 设备热插拔不支持：serial 在启动时确定
- 跨网段 / NAT 需自行配置端口映射
- 不支持设备管理、权限控制或传输加密
- 日志时间显示 UTC（Rust 标准库无时区转换，未引入 chrono）
- 托盘常驻、开机自启未实现；关闭窗口即退出

---

## 许可证

**GPL-3.0-only**，见 [LICENSE](LICENSE)。

选择 GPLv3 与 Slint 的授权有关：Slint 提供 GPLv3 / 免版税 / 商业三种许可。
走 GPLv3 免费但要求应用整体开源；走免版税则桌面应用可闭源，代价是保留一行
`Made with Slint` 归属声明。本项目选择前者。
