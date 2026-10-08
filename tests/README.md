# 回归测试

本目录是项目的**回归测试套件**，全部为 Python 脚本，**平铺在一个目录里**
（有意为之：跨机脚本 `remote_test.py` / `upload_to_b.py` 按同目录解析文件名，
拆子目录会让它们互相找不到对方）。

## 前置条件

- 电脑 A（Windows，172.16.0.106）：已装 adb 且在 PATH 中，USB 连着设备 C
- 电脑 B（Ubuntu，172.16.0.101）：标准 adb 34.0.4
- 桥已在 A 上运行并监听 **15555**
- 跨机脚本需要 B 的 SSH 可达（`remote_test.py` 通过 paramiko 连接，
  参数走环境变量 `SSH_HOST` / `SSH_USER` / `SSH_PORT` / `SSH_PASSWORD`）

> ⚠️ 所有脚本都按**当前网络环境**（A=172.16.0.106，B=172.16.0.101，端口 15555）
> 写死了默认值。换环境需要改脚本里的常量。

## 脚本分类

### 一、一致性与协议正确性（在 A 上运行）

| 脚本 | 用途 | 基线结果 |
|---|---|---|
| `consistency_test.py` | 第一批 50 条命令：同一条命令分别走「USB 直连」和「经桥」，逐条比对 stdout / stderr / 退出码 | **50/50** |
| `consistency_test2.py` | 第二批 50 条：二进制与编码边界、BRE/ERE 正则、Bash 高级语法、find/xargs、退出码透传、大流量流控（3MB stdout / 1MB 无换行 / 20000 字符单行） | **50/50** |
| `diag_features.py` | 对比 adb server 眼中「USB 设备」与「桥设备」的 features / product 是否一致 | 诊断用 |
| `diag_exec.py` | 诊断 `exec-out` 在并发 / 大尺寸下的失败临界点 | 诊断用 |

`consistency_test2.py` 支持导出/比对基线，用于跨机一致性验证：

```bash
# A 上用 USB 直连导出基线
python consistency_test2.py --export baseline_usb.json
python upload_to_b.py baseline_usb.json          # 传到 B
# B 上经桥跑，与基线比对
python consistency_test2.py --baseline /tmp/baseline_usb.json
```

`baseline_usb.json` 是本仓库留档的 USB 直连输出基线。它随设备固件/文件系统
变化，必要时用上面的 `--export` 重新生成。

跨机比较需先**归一化换行**：A 上是 Rockchip ADB 31（CRLF），B 上是 ADB 34（LF）。
这是客户端版本差异，与桥无关。

### 二、压力与边界

| 脚本 | 运行位置 | 用途 | 基线结果 |
|---|---|---|---|
| `stress_test.py` | A（模拟 B 的用法） | 基础压测 12 项 | **12/12**，17.6 MB/s（GUI 常开时） |
| `stress_advanced.py` | B | 进阶压测 11 项：50MB push+pull md5、100 个小文件、8 路并发 exec-out、双向并发、半开连接等 | 见下 |
| `stress_advanced_win.py` | A（Windows） | `stress_advanced.py` 的 Windows 修正版 | **6/6**，30MB/50MB MD5 均一致 |

> `stress_advanced.py` 第 67 行用 `/dev/urandom`（Linux 路径），在 Windows 上
> 必然 `FileNotFoundError`，会让 11 项里 8 项「失败」——**与桥无关**，是脚本
> 自身跑不了。Windows 上请用 `stress_advanced_win.py`（改用 `os.urandom`）。

### 三、跨机端到端（在 B 上执行）

| 脚本 | 用途 | 基线结果 |
|---|---|---|
| `bridge_check_b.py` | 项目的**验收标准**：B 用标准 adb 连 `172.16.0.106:15555`，像普通网络设备一样操作只接在 A USB 上的 C | **16/16** |
| `bridge_shellv2_b.py` | 专项诊断 shell_v2：stderr 是否合并、退出码是否透传（直接打印，不做断言） | 诊断用 |
| `diag_adb_state_b.py` | 排查 B 上 adb 卡在 `offline` 的问题（桥重启后 `already connected` 但状态 offline） | 诊断用 |
| `remote_test.py` | 经 SSH/SFTP 把本地脚本上传到 B 执行并回传输出 | 工具 |
| `run_remote_bg.py` | 在 B 上**后台**跑脚本、输出写文件，SSH 断开不影响远端 | 工具 |
| `upload_to_b.py` | 上传数据文件（如基线 json）到 B 的 `/tmp` | 工具 |

### 四、反复重启 / 强杀恢复稳定性（在 A 上运行）

| 脚本 | 用途 | 基线结果 |
|---|---|---|
| `restart_stability_a.py` | CLI 反复启停：每轮启动 → 等 LISTENING → 让 B 经桥真跑几条命令 → 停止，记录耗时 | **30/30**，启动 0.51~2.72s、停止 <0.01s |
| `restart_gui_a.py` | GUI 进程反复启停：每个循环真正结束整个 GUI 进程再拉起，验证窗口弹出、配置持久化、耗时不漂移、无进程残留 | **15/15**，窗口出现 0.25~0.50s |
| `kill_recovery_a.py` | 强杀恢复：`taskkill /F` 后端口能否被下一个实例接管、功能是否恢复 | **12/12**，强杀 ~1.0s 端口释放、重启 0.5s 恢复 |

三者合计 **57 轮零失败**。每轮都由电脑 B 经桥真实执行
`adb connect` + `get-state` + `shell`，不是只验证「端口起来了」。

> 这三个脚本要拉起 `rust/target/release/` 下的 exe。若产物不在那里
> （例如用了 `releases/` 下的副本），改脚本里的 `EXE` 常量。

### 五、长时稳定性

| 脚本 | 用途 | 基线结果 |
|---|---|---|
| `ping_stability_b.py` | 在 B 上跑 20 分钟：ping 由**设备 C** 发起，一次往返走完整条链路；每秒采样 RTT，每 60 秒做通道活性检查 | 1200/1200 往返，**0% 丢包** |

> ⚠️ **别把 `adb shell ping` 的 RTT 当成桥的性能**。ping 是设备自己发起的，
> ICMP 不经过桥（桥只承载 adb 协议）。这个测试验证的是「adb 通道长时间零中断」；
> 转发性能要看 `stress_test.py` 的 MB/s 与大文件 push-pull md5。

### 六、弱网模拟

| 脚本 | 用途 |
|---|---|
| `weaknet_proxy.py` | 在 A 上跑的弱网代理：监听一个端口转发到真桥，注入延迟/抖动/限速/随机停顿/随机断连 |
| `weaknet_client.py` | 在 B 上跑的客户端，连代理端口执行 adb 操作 |
| `weaknet_run.py` | 编排：启代理 → SSH 到 B 跑客户端 → 停代理 → 换下一档，共五档 |

之所以不用 `tc netem`：B 上 sudo 需要密码。本代理在应用层模拟弱网主要特征，
不需要 root。需要本机 16666 端口已在防火墙放行（B 才能连进来）。

## 长时测试的两条纪律（都踩过）

1. **`remote_test.py` 的 `exec_command` 有 900 秒硬编码超时**，跑 20 分钟必被掐断。
   可用 `REMOTE_TIMEOUT` 环境变量覆盖；更稳的是用 `run_remote_bg.py` 走
   「后台跑 + 结果文件」（`setsid nohup ... > log 2>&1 < /dev/null &`，SSH 断开不影响远端）。
   - 单纯 `nohup ... &` 不够：子进程仍持有 SSH 通道 fd，paramiko 的 `read()`
     会一直等通道关闭。
   - Python 重定向到文件是**块缓冲**，中途看日志是空的。
2. Windows 上 `adb shell` 传多个参数会拼丢引号，带管道的整条命令要作为
   **单个字符串**传。

## 结果留档

`results/` 下是稳定性测试的实际输出日志（是「测试通过」的证据，故不随
`*.log` 规则忽略）：

| 文件 | 内容 |
|---|---|
| `restart_30.log` | CLI 反复启停 30 轮 |
| `restart_gui_15.log` | GUI 反复启停 15 轮 |
| `kill_recovery_12.log` | 强杀恢复 12 轮 |
| `ping_stability_b.log` | 20 分钟长时连接 |
