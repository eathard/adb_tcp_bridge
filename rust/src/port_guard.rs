//! 端口占用检查与清理。
//!
//! 直接移植 Python 版 `port_guard.py` 的行为，保持一致的用户预期。
//!
//! Windows 上查询端口占用有两条路：
//!  1. `netstat -ano` —— 通用，但需解析文本，且慢（数百行）
//!  2. psutil —— 需第三方依赖
//!
//! 这里优先用 netstat（系统自带，无需安装），查不到 PID 时回退到 socket 探测。
//!
//! ⚠️ **编码陷阱**：Windows 上 `netstat` / `tasklist` 输出是**本地代码页**
//! （简体中文系统为 GBK），不是 UTF-8。直接按 UTF-8 解码会失败，
//! 必须逐字节容错处理。Python 版靠 `errors="replace"` 解决，
//! Rust 的 `String::from_utf8_lossy` 行为等价。

use std::net::{Ipv4Addr, SocketAddr, TcpListener};
use std::process::{Command, Stdio};
use std::time::{Duration, Instant};

/// 受保护的进程名（系统或关键服务），即使占着端口也不应被杀。
///
/// 避免误杀用户的系统进程导致桌面环境异常。
pub const PROTECTED_NAMES: &[&str] = &[
    "system",
    "system idle process",
    "services.exe",
    "lsass.exe",
    "csrss.exe",
    "wininit.exe",
    "winlogon.exe",
    "svchost.exe",
    "explorer.exe",
    "audiodg.exe",
    "dwm.exe",
];

/// 判断进程名是否受保护（不区分大小写）。
pub fn is_protected(name: &str) -> bool {
    let lower = name.to_ascii_lowercase();
    PROTECTED_NAMES.iter().any(|p| *p == lower)
}

/// 执行命令并返回 stdout 的**原始字节**。
///
/// 返回原始字节而非 String，是为了让调用方按本地代码页容错解码 ——
/// 这是 Windows 上唯一安全的做法。
fn run_bytes(cmd: &str, args: &[&str], timeout: Duration) -> Option<Vec<u8>> {
    let child = Command::new(cmd)
        .args(args)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn();

    let mut child = match child {
        Ok(c) => c,
        Err(_) => return None,
    };

    // 轮询等待，避免引入额外依赖实现带超时的读
    let deadline = Instant::now() + timeout;
    loop {
        match child.try_wait() {
            Ok(Some(_)) => break,
            Ok(None) => {
                if Instant::now() > deadline {
                    let _ = child.kill();
                    let _ = child.wait();
                    return None;
                }
                std::thread::sleep(Duration::from_millis(50));
            }
            Err(_) => return None,
        }
    }
    child.wait_with_output().ok().map(|o| o.stdout)
}

/// 执行命令并返回容错解码后的文本。
///
/// Windows 的 netstat/tasklist 是 GBK 输出，用 `from_utf8_lossy` 容错，
/// 非 UTF-8 字节会被替换为 U+FFFD，不会失败 —— 与 Python 版
/// `errors="replace"` 行为一致。
fn run_text(cmd: &str, args: &[&str], timeout: Duration) -> Option<String> {
    run_bytes(cmd, args, timeout).map(|b| String::from_utf8_lossy(&b).to_string())
}

/// netstat 的一行监听记录。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Listener {
    pub pid: u32,
    pub local_addr: String,
}

/// 解析 netstat 输出，找出监听指定端口的所有进程。
///
/// ⚠️ 实测同一个端口常同时出现**多个 PID 的 LISTENING**
/// （如残留的 Python 版 + 二进制版实例），必须全部返回，
/// 只取第一个会导致后续 bind 仍失败。
///
/// `netstat -ano` 的行示例：
/// ```text
///   TCP    0.0.0.0:15555    0.0.0.0:0    LISTENING    66380
///   TCP    [::]:15555       [::]:0       LISTENING    66380
/// ```
pub fn parse_listeners(text: &str, port: u16) -> Vec<Listener> {
    let mut found = Vec::new();
    for line in text.lines() {
        let parts: Vec<&str> = line.split_whitespace().collect();
        // TCP <local> <foreign> LISTENING <pid>  => 5 段
        if parts.len() < 5 {
            continue;
        }
        if !parts[0].eq_ignore_ascii_case("TCP") {
            continue;
        }
        if !parts[3].eq_ignore_ascii_case("LISTENING") {
            continue;
        }
        let local = parts[1];
        let (addr, lport) = match split_host_port(local) {
            Some(x) => x,
            None => continue,
        };
        if lport != port {
            continue;
        }
        if let Ok(pid) = parts[4].parse::<u32>() {
            found.push(Listener {
                pid,
                local_addr: addr.to_string(),
            });
        }
    }
    found
}

/// 从 `0.0.0.0:15555` 或 `[::]:15555` 中拆出主机与端口。
pub fn split_host_port(s: &str) -> Option<(&str, u16)> {
    if let Some(rest) = s.strip_prefix('[') {
        // [::]:15555
        let end = rest.find(']')?;
        let host = &rest[..end];
        let port = rest[end + 1..].strip_prefix(':')?;
        return port.parse().ok().map(|p| (host, p));
    }
    let idx = s.rfind(':')?;
    let host = &s[..idx];
    let port = &s[idx + 1..];
    port.parse().ok().map(|p| (host, p))
}

/// 查询监听指定端口的进程。
///
/// `None` 表示「查不了」（netstat 不可用），区别于「没人占用」（`Some(vec![])`）。
pub fn netstat_listeners(port: u16) -> Option<Vec<Listener>> {
    let text = run_text(
        "netstat",
        &["-ano", "-p", "TCP"],
        Duration::from_secs(20),
    )?;
    Some(parse_listeners(&text, port))
}

/// 取进程名，查不到返回 None。
///
/// `tasklist /FO CSV /NH` 输出形如 `"python.exe","1234","Console","1","50,000 K"`
pub fn process_name(pid: u32) -> Option<String> {
    let filter = format!("PID eq {}", pid);
    let text = run_text(
        "tasklist",
        &["/FI", &filter, "/FO", "CSV", "/NH"],
        Duration::from_secs(15),
    )?;
    let t = text.trim();
    if t.is_empty() || t.contains("没有运行的任务") || t.contains("No tasks") {
        return None;
    }
    let first = t.lines().next()?.trim();
    if first.starts_with('"') {
        // 以 " 分隔，取第2 段
        let mut it = first.split('"');
        it.next()?; // 开头的空串
        return it.next().map(|s| s.to_string()).filter(|s| !s.is_empty());
    }
    Some(first.split(',').next()?.to_string())
}

/// 进程是否还活着。
pub fn pid_alive(pid: u32) -> bool {
    let filter = format!("PID eq {}", pid);
    match run_text(
        "tasklist",
        &["/FI", &filter, "/FO", "CSV", "/NH"],
        Duration::from_secs(15),
    ) {
        Some(t) => t.contains(&pid.to_string()),
        None => false,
    }
}

/// 等待进程消失。
pub fn wait_gone(pid: u32, timeout: Duration) -> bool {
    let deadline = Instant::now() + timeout;
    loop {
        if !pid_alive(pid) {
            return true;
        }
        if Instant::now() >= deadline {
            return !pid_alive(pid);
        }
        std::thread::sleep(Duration::from_millis(250));
    }
}

/// 结束进程。
///
/// 顺序：先 `taskkill`（不带 /F），对有窗口的程序相当于请求关闭；
/// 对无窗口后台进程通常无效，随即升级为 `/F` 强杀。
///
/// 保留「温和优先」是为了避免误杀 GUI 程序时连带丢掉用户未保存的工作。
pub fn kill_pid(pid: u32) -> bool {
    crate::log::info("port_guard", format!("结束进程 PID={} ...", pid));

    let p = pid.to_string();
    let _ = run_text("taskkill", &["/PID", &p], Duration::from_secs(10));

    if wait_gone(pid, Duration::from_secs(5)) {
        crate::log::info("port_guard", format!("进程 PID={} 已退出（温和终止）", pid));
        return true;
    }

    crate::log::warn(
        "port_guard",
        format!("进程 PID={} 未响应，强制结束", pid),
    );
    let _ = run_text("taskkill", &["/F", "/PID", &p], Duration::from_secs(10));

    if wait_gone(pid, Duration::from_secs(5)) {
        crate::log::info("port_guard", format!("进程 PID={} 已强制结束", pid));
        return true;
    }
    !pid_alive(pid)
}

/// 只判断端口有没有被监听（拿不到 PID 时的回退方案）。
pub fn port_in_use(port: u16, host: &str) -> bool {
    let ip: Ipv4Addr = host.parse().unwrap_or(Ipv4Addr::UNSPECIFIED);
    if let Ok(_l) = TcpListener::bind(SocketAddr::from((ip, port))) {
        return false;
    }
    // IPv6 回退
    if let Ok(_l6) = TcpListener::bind(SocketAddr::from((
        std::net::Ipv6Addr::UNSPECIFIED,
        port,
    ))) {
        return false;
    }
    true
}

/// 等待端口释放。
pub fn free_port(port: u16, host: &str, attempts: u32, delay: Duration) -> bool {
    for _ in 0..attempts {
        if !port_in_use(port, host) {
            return true;
        }
        std::thread::sleep(delay);
    }
    !port_in_use(port, host)
}

/// 端口检查结果。
#[derive(Debug, Clone, Default)]
pub struct GuardResult {
    /// 端口最终是否可用
    pub ok: bool,
    /// 被结束的进程 PID
    pub killed: Vec<u32>,
    /// 跳过的 PID（本进程自身）
    pub skipped: Vec<u32>,
    /// 受保护而未结束的 PID
    pub protected: Vec<u32>,
}

/// 确保监听端口可用。
///
/// - `auto_kill=false` 时只报告不杀进程。
/// - `skip_pids` 里的 PID 不会被杀（把本进程 PID 放进来可避免自杀）。
///
/// ⚠️ 判定端口是否真的可用，以「清理后是否还有残留占用者」为准，
/// **不用 `port_in_use()`**——它只反映「能否 bind」，而同端口存在
/// 半关闭/TIME_WAIT 残留时 bind 可能成功，但 `bind()+listen()` 随后仍失败。
pub fn ensure_port_free(
    port: u16,
    host: &str,
    skip_pids: &[u32],
    auto_kill: bool,
) -> GuardResult {
    let mut res = GuardResult::default();

    let listeners = match netstat_listeners(port) {
        Some(l) => l,
        None => {
            // 查不到 PID，退化为「能否 bind」判断
            if !port_in_use(port, host) {
                res.ok = true;
                return res;
            }
            if !auto_kill {
                crate::log::warn(
                    "port_guard",
                    format!("端口 {} 已被占用，但无法定位占用进程", port),
                );
                return res;
            }
            crate::log::info(
                "port_guard",
                format!("端口 {} 被占用，尝试等待其释放...", port),
            );
            if free_port(port, host, 15, Duration::from_millis(400)) {
                res.ok = true;
                return res;
            }
            crate::log::warn(
                "port_guard",
                format!("端口 {} 仍被占用，且无法定位进程，无法清理", port),
            );
            return res;
        }
    };

    if listeners.is_empty() {
        // netstat 说没人监听，但可能因权限看不到，再 bind 确认
        if !port_in_use(port, host) {
            res.ok = true;
            return res;
        }
        crate::log::warn(
            "port_guard",
            format!(
                "端口 {} 仍无法绑定，可能存在 TIME_WAIT 或权限问题",
                port
            ),
        );
        return res;
    }

    for l in &listeners {
        let pid = l.pid;
        let name = process_name(pid);
        let shown = name.clone().unwrap_or_else(|| "未知进程".into());

        if skip_pids.contains(&pid) {
            crate::log::info(
                "port_guard",
                format!(
                    "端口 {} 被本进程占用(PID={} {}),跳过",
                    port, pid, shown
                ),
            );
            res.skipped.push(pid);
            continue;
        }
        if let Some(n) = &name {
            if is_protected(n) {
                crate::log::warn(
                    "port_guard",
                    format!(
                        "端口 {} 被系统进程占用(PID={} {}),出于安全不自动结束",
                        port, pid, shown
                    ),
                );
                res.protected.push(pid);
                continue;
            }
        }

        crate::log::warn(
            "port_guard",
            format!(
                "端口 {} 已被占用: PID={} 进程={} 监听={}",
                port, pid, shown, l.local_addr
            ),
        );
        if !auto_kill {
            continue;
        }
        if kill_pid(pid) {
            res.killed.push(pid);
        }
    }

    // 以「清理后是否还有残留占用者」为准
    let remaining = netstat_listeners(port).unwrap_or_default();
    if !remaining.is_empty() {
        let names: Vec<String> = remaining
            .iter()
            .map(|l| {
                format!(
                    "PID={}({})",
                    l.pid,
                    process_name(l.pid).unwrap_or_else(|| "?".into())
                )
            })
            .collect();
        crate::log::error(
            "port_guard",
            format!(
                "端口 {} 仍被以下进程占用: {}",
                port,
                names.join(", ")
            ),
        );
        return res;
    }

    if !res.killed.is_empty() {
        crate::log::info(
            "port_guard",
            format!(
                "端口 {} 已释放（清理了 {} 个进程）",
                port,
                res.killed.len()
            ),
        );
    }
    res.ok = true;
    res
}

/// 桥启动前的端口预检。
///
/// 仅监听回环地址时跳过检查（本进程独占，不可能被外部占用）。
pub fn prepare_listen_port(
    host: &str,
    port: u16,
    self_pid: u32,
    auto_kill: bool,
) -> bool {
    if !matches!(host, "0.0.0.0" | "::" | "") {
        return true;
    }
    crate::log::info("port_guard", format!("检查监听端口 {} ...", port));
    let res = ensure_port_free(port, host, &[self_pid], auto_kill);
    if res.ok {
        true
    } else if auto_kill {
        crate::log::error(
            "port_guard",
            format!(
                "端口 {} 无法释放，请手动关闭占用程序后重试",
                port
            ),
        );
        false
    } else {
        crate::log::error(
            "port_guard",
            format!("端口 {} 被占用（已关闭自动清理）", port),
        );
        false
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const SAMPLE: &str = "\
Active Connections

  Proto  Local Address          Foreign Address        State           PID
  TCP    0.0.0.0:135            0.0.0.0:0              LISTENING       1044
  TCP    0.0.0.0:15555          0.0.0.0:0              LISTENING       66380
  TCP    [::]:15555             [::]:0                 LISTENING       66380
  TCP    0.0.0.0:15555          0.0.0.0:0              LISTENING       71234
  TCP    127.0.0.1:5037         0.0.0.0:0              LISTENING       71544
  TCP    172.16.0.106:15555     172.16.0.101:52000     ESTABLISHED     66380
";

    #[test]
    fn parses_all_listeners_on_port() {
        let ls = parse_listeners(SAMPLE, 15555);
        // 必须返回全部 3 条：IPv4 + IPv6 + 第二个 PID
        assert_eq!(ls.len(), 3, "同端口多 PID 必须全部返回，实际 {}", ls.len());
    }

    #[test]
    fn ignores_other_ports_and_states() {
        let ls = parse_listeners(SAMPLE, 15555);
        for l in &ls {
            assert!(l.pid == 66380 || l.pid == 71234);
            // 不应包含 135 / 5037 的 PID，也不应包含 ESTABLISHED 那条
            assert_ne!(l.pid, 1044);
            assert_ne!(l.pid, 71544);
        }
    }

    #[test]
    fn captures_ipv6_listener() {
        let ls = parse_listeners(SAMPLE, 15555);
        assert!(
            ls.iter().any(|l| l.local_addr.contains(':')),
            "应解析出 IPv6 形式 [::]:15555"
        );
    }

    #[test]
    fn port_135_and_5037_found_correctly() {
        let a = parse_listeners(SAMPLE, 5037);
        assert_eq!(a.len(), 1);
        assert_eq!(a[0].pid, 71544);
        let b = parse_listeners(SAMPLE, 135);
        assert_eq!(b.len(), 1);
        assert_eq!(b[0].pid, 1044);
    }

    #[test]
    fn empty_when_port_free() {
        assert!(parse_listeners(SAMPLE, 9999).is_empty());
    }

    #[test]
    fn splits_host_port_forms() {
        assert_eq!(split_host_port("0.0.0.0:15555"), Some(("0.0.0.0", 15555)));
        assert_eq!(split_host_port("[::]:15555"), Some(("::", 15555)));
        assert_eq!(split_host_port("127.0.0.1:5037"), Some(("127.0.0.1", 5037)));
        assert_eq!(split_host_port("garbage"), None);
        assert_eq!(split_host_port("host:notaport"), None);
    }

    #[test]
    fn protected_processes_are_recognized() {
        for n in [
            "System",
            "SYSTEM",
            "svchost.exe",
            "explorer.exe",
            "lsass.exe",
        ] {
            assert!(is_protected(n), "{} 应受保护", n);
        }
        for n in ["adb_bridge_rs.exe", "python.exe", "adb.exe", "notepad.exe"] {
            assert!(!is_protected(n), "{} 不应受保护", n);
        }
    }

    #[test]
    fn process_name_parsing_from_csv() {
        // 模拟 tasklist CSV 输出的解析逻辑
        let line = "\"adb_bridge_rs.exe\",\"1234\",\"Console\",\"1\",\"10,076 K\"";
        let parsed = if line.starts_with('"') {
            let mut it = line.split('"');
            it.next();
            it.next().map(|s| s.to_string())
        } else {
            Some(line.split(',').next().unwrap_or("").to_string())
        };
        assert_eq!(parsed.as_deref(), Some("adb_bridge_rs.exe"));
    }

    #[test]
    fn garbled_gbk_output_does_not_panic() {
        // Windows 本地代码页输出含非 UTF-8 字节，from_utf8_lossy 必须容错
        let bytes = b"  TCP    0.0.0.0:15555    0.0.0.0:0    LISTENING    66380\r\n\xd6\xd0\xce\xc4";
        let text = String::from_utf8_lossy(bytes);
        let ls = parse_listeners(&text, 15555);
        assert_eq!(ls.len(), 1, "GBK 乱码行应被忽略，合法行仍要解析出来");
        assert_eq!(ls[0].pid, 66380);
    }

    #[test]
    fn free_port_detects_unused_port() {
        // 取一个几乎不可能被占用的高端口
        assert!(!port_in_use(45999, "0.0.0.0"), "高端口应可用");
    }

    #[test]
    fn guard_result_defaults_to_not_ok() {
        let r = GuardResult::default();
        assert!(!r.ok, "默认应认为未清理成功");
        assert!(r.killed.is_empty());
    }

    #[test]
    fn loopback_host_skips_check() {
        // 仅监听回环时不需检查
        assert!(prepare_listen_port("127.0.0.1", 45998, std::process::id(), true));
    }
}