//! ADB TCP 桥 —— Rust 重构版入口。
//!
//! 把本机 USB 设备 C 暴露成标准 TCP adb 设备，使电脑 B 只需
//! `adb connect <A的局域网IP>:15555` 就能像普通网络设备一样使用。
//!
//! 用法：
//! ```text
//! adb_bridge_rs [选项]
//!   --listen-port <端口>    监听端口（默认 5555，建议 15555）
//!   --listen-addr <地址>    监听地址（默认 0.0.0.0）
//!   --serial <serial>       目标设备（默认第一台在线设备）
//!   --server-port <端口>    adb server 端口（默认 5037）
//!   --no-kill-port          端口被占用时不结束占用进程
//!   --debug-packets         打印每个包的收发（排障用）
//! ```
//!
//! ⚠️ **端口必须避开 5555~5585**：本机 adb server 会扫描该段查找模拟器，
//! 桥若占用其中之一，`adb devices` 会出现幽灵设备 emulator-5554。

use std::sync::atomic::{AtomicBool, AtomicU32, Ordering};
use std::sync::{Arc, Mutex};

use adb_bridge_rs::{log, server, session};

/// 桥的运行配置。
#[derive(Debug, Clone)]
pub struct Config {
    pub listen_addr: String,
    pub listen_port: u16,
    pub serial: Option<String>,
    pub server_port: u16,
    pub no_kill_port: bool,
    pub debug_packets: bool,
}

impl Default for Config {
    fn default() -> Self {
        Config {
            listen_addr: "0.0.0.0".into(),
            listen_port: 5555,
            serial: None,
            server_port: 5037,
            no_kill_port: false,
            debug_packets: false,
        }
    }
}

/// 极简参数解析（避免引入 clap 依赖，GUI 阶段再统一处理）。
pub fn parse_args() -> Config {
    let mut c = Config::default();
    let args: Vec<String> = std::env::args().collect();
    let mut i = 1;
    while i < args.len() {
        match args[i].as_str() {
            "--listen-port" => {
                i += 1;
                if let Some(v) = args.get(i).and_then(|s| s.parse().ok()) {
                    c.listen_port = v;
                }
            }
            "--listen-addr" => {
                i += 1;
                if let Some(v) = args.get(i) {
                    c.listen_addr = v.clone();
                }
            }
            "--serial" => {
                i += 1;
                if let Some(v) = args.get(i) {
                    c.serial = Some(v.clone());
                }
            }
            "--server-port" => {
                i += 1;
                if let Some(v) = args.get(i).and_then(|s| s.parse().ok()) {
                    c.server_port = v;
                }
            }
            "--no-kill-port" => c.no_kill_port = true,
            "--debug-packets" => c.debug_packets = true,
            "--help" | "-h" => {
                print_help();
                std::process::exit(0);
            }
            _ => {}
        }
        i += 1;
    }
    c
}

fn print_help() {
    println!("ADB TCP 桥 —— Rust 重构版");
    println!();
    println!("用法: adb_bridge_rs [选项]");
    println!("  --listen-port <端口>   监听端口（默认 5555，建议 15555）");
    println!("  --listen-addr <地址>   监听地址（默认 0.0.0.0）");
    println!("  --serial <serial>      目标设备（默认第一台在线设备）");
    println!("  --server-port <端口>   adb server 端口（默认 5037）");
    println!("  --no-kill-port         端口被占用时不结束占用进程");
    println!("  --debug-packets        打印每个包的收发（排障用）");
    println!();
    println!("警告: 端口不要用 5555~5585，否则会被 adb server 当成模拟器。");
}

/// 列出在线的 adb 设备 serial。
pub fn list_devices() -> Vec<String> {
    let out = std::process::Command::new("adb").arg("devices").output();
    let mut res = Vec::new();
    if let Ok(o) = out {
        let text = String::from_utf8_lossy(&o.stdout).to_string();
        for line in text.lines().skip(1) {
            let parts: Vec<&str> = line.split_whitespace().collect();
            if parts.len() >= 2 && parts[1] == "device" {
                res.push(parts[0].to_string());
            }
        }
    }
    res
}

/// 本机局域网 IP（界面显示给 B 的连接命令用）。
///
/// 通过创建一个 UDP socket 并 connect 到公网地址来查询本机出口 IP，
/// **不会实际发送数据包**。
pub fn local_ip() -> String {
    let s = match std::net::UdpSocket::bind("0.0.0.0:0") {
        Ok(s) => s,
        Err(_) => return "127.0.0.1".into(),
    };
    if s.connect("8.8.8.8:80").is_err() {
        return "127.0.0.1".into();
    }
    match s.local_addr() {
        Ok(a) => a.ip().to_string(),
        Err(_) => "127.0.0.1".into(),
    }
}

/// 端口是否落在 adb server 的模拟器扫描区间。
///
/// 落在 5555~5585 会让本机 `adb devices` 出现幽灵设备 emulator-5554，
/// 并可能抢占真实设备名 —— 这是硬约束，界面上应给出警告。
pub fn is_emulator_port(port: u16) -> bool {
    (5555..=5585).contains(&port)
}

/// 端口建议值：返回一个不含警示的推荐端口。
pub fn recommended_port() -> u16 {
    15555
}

/// 启动桥。
pub fn start_bridge(cfg: &Config) -> std::io::Result<u16> {
    let _ = std::process::Command::new("adb")
        .arg("start-server")
        .stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::null())
        .status();

    let serial = match &cfg.serial {
        Some(s) => s.clone(),
        None => match list_devices().into_iter().next() {
            Some(s) => s,
            None => {
                log::error("bridge", "没有检测到在线 adb 设备，请检查 USB 连接");
                std::process::exit(1);
            }
        },
    };

    let server_addr = format!("127.0.0.1:{}", cfg.server_port);
    if !server::ensure_server(&server_addr) {
        log::warn("bridge", "adb server 拉起失败，连接时会自动重试");
    }
    let features = server::query_features(&server_addr, &serial);
    let product = server::query_prop(&server_addr, &serial, "get-product");

    log::info("bridge", format!("目标设备: {}", serial));
    log::info(
        "bridge",
        format!(
            "设备 features: {}",
            if features.is_empty() {
                "(未获取到，按老协议工作)"
            } else {
                &features
            }
        ),
    );

    if is_emulator_port(cfg.listen_port) {
        log::warn(
            "bridge",
            format!(
                "端口 {} 落在 5555~5585 区间，会被 adb server 当成模拟器端口，\
                 建议改用 {}",
                cfg.listen_port,
                recommended_port()
            ),
        );
    }

    let srv = match std::net::TcpListener::bind((cfg.listen_addr.as_str(), cfg.listen_port)) {
        Ok(s) => s,
        Err(e) => {
            log::error(
                "bridge",
                format!(
                    "无法绑定 {}:{} —— {}",
                    cfg.listen_addr, cfg.listen_port, e
                ),
            );
            log::error(
                "bridge",
                "提示: 上一次实例可能还在运行，请先关闭后重试",
            );
            std::process::exit(1);
        }
    };

    let shown_addr = if cfg.listen_addr == "0.0.0.0" {
        local_ip()
    } else {
        cfg.listen_addr.clone()
    };
    log::info(
        "bridge",
        format!(
            "监听 {}:{} —— 电脑 B 执行: adb connect {}:{}",
            cfg.listen_addr, cfg.listen_port, shown_addr, cfg.listen_port
        ),
    );

    let shared = Arc::new(session::Shared {
        serial,
        server_addr,
        features,
        product,
        closed: Arc::new(AtomicBool::new(false)),
        next_id: AtomicU32::new(0x10000),
        sessions: Arc::new(Mutex::new(Vec::new())),
    });

    // adb server 看门狗：部分环境会回收 adb server，断了要自动拉起
    {
        let addr = shared.server_addr.clone();
        let closed = shared.closed.clone();
        std::thread::spawn(move || {
            while !closed.load(Ordering::Relaxed) {
                std::thread::sleep(std::time::Duration::from_secs(10));
                if closed.load(Ordering::Relaxed) {
                    return;
                }
                if !server::server_alive(&addr) {
                    log::info("watchdog", "adb server 不可达，正在拉起 ...");
                    let _ = server::ensure_server(&addr);
                    log::info("watchdog", "adb server 已恢复");
                }
            }
        });
    }

    for stream in srv.incoming() {
        if shared.closed.load(Ordering::Relaxed) {
            break;
        }
        match stream {
            Ok(sock) => {
                let sh = shared.clone();
                std::thread::spawn(move || session::run(sh, sock));
            }
            Err(e) => log::warn("bridge", format!("accept 失败: {}", e)),
        }
    }
    Ok(cfg.listen_port)
}

fn main() {
    let cfg = parse_args();
    if cfg.debug_packets {
        log::set_debug(true);
    }
    log::info("bridge", "ADB TCP 桥启动（Rust 版）");
    match start_bridge(&cfg) {
        Ok(p) => log::info("bridge", format!("桥已停止（端口 {}）", p)),
        Err(e) => log::error("bridge", format!("启动失败: {}", e)),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn emulator_port_range_is_detected() {
        // 硬约束：这些端口会引发幽灵设备
        assert!(is_emulator_port(5555));
        assert!(is_emulator_port(5570));
        assert!(is_emulator_port(5585));
        assert!(!is_emulator_port(15555));
        assert!(!is_emulator_port(5554));
        assert!(!is_emulator_port(5586));
    }

    #[test]
    fn recommended_port_avoids_emulator_range() {
        assert!(!is_emulator_port(recommended_port()));
    }

    #[test]
    fn default_config_is_sane() {
        let c = Config::default();
        assert_eq!(c.listen_port, 5555);
        assert_eq!(c.listen_addr, "0.0.0.0");
        assert_eq!(c.server_port, 5037);
        assert!(!c.no_kill_port);
        assert!(!c.debug_packets);
    }

    #[test]
    fn local_ip_returns_valid_form() {
        let ip = local_ip();
        assert!(ip.contains('.'), "应形如 x.x.x.x，实际 {}", ip);
    }

    #[test]
    fn list_devices_does_not_panic() {
        let _ = list_devices();
    }
}