pub mod app_state;
pub mod config;
pub mod log;
pub mod port_guard;
pub mod proto;
pub mod server;
pub mod session;
#[cfg(feature = "slint-ui")]
pub mod slint_ui;
pub mod stats;
pub mod stream;

// 模块已在文件头用 `pub mod` 声明，此处仅补充 std 依赖
use std::sync::atomic::{AtomicBool, AtomicU32, Ordering};
use std::sync::{Arc, Mutex};

// ---- 以下函数原在 main.rs，为便于图形界面与测试调用而迁入 lib ----

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
            // ⚠️ 默认值必须是 15555 而不是 adb 惯用的 5555：5555~5585 是
            // adb server 的模拟器扫描区间，监听它会让本机 `adb devices`
            // 冒出幽灵设备 emulator-5554 并抢走真实设备名。
            // 详见 is_emulator_port()。
            listen_port: recommended_port(),
            serial: None,
            server_port: 5037,
            no_kill_port: false,
            debug_packets: false,
        }
    }
}

/// 极简参数解析（避免引入 clap 依赖）。
///
/// `it` 应传入**不含程序名**的参数序列（如 `std::env::args().skip(1)`）。
pub fn parse_args_from<I: Iterator<Item = String>>(it: I) -> Config {
    let mut c = Config::default();
    let args: Vec<String> = it.collect();
    let mut i = 0;
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
    println!("  --listen-port <端口>   监听端口（默认 15555）");
    println!("  --listen-addr <地址>   监听地址（默认 0.0.0.0）");
    println!("  --serial <serial>      目标设备（默认第一台在线设备）");
    println!("  --server-port <端口>   adb server 端口（默认 5037）");
    println!("  --no-kill-port         端口被占用时不结束占用进程（默认自动清理）");
    println!("  --debug-packets        打印每个包的收发（排障用）");
    println!("  --save-config          把当前参数保存到配置文件");
    println!();
    println!("警告: 端口不要用 5555~5585，否则会被 adb server 当成模拟器。");
}


/// 从进程命令行解析配置。
pub fn parse_args() -> Config {
    parse_args_from(std::env::args().skip(1))
}

/// 端口是否落在 adb server 的模拟器扫描区间。
///
/// ⚠️ 落在 5555~5585 会让本机 `adb devices` 出现幽灵设备 emulator-5554，
/// 并可能抢占真实设备名 —— 这是硬约束，界面上应给出警告。
pub fn is_emulator_port(port: u16) -> bool {
    (5555..=5585).contains(&port)
}

/// 推荐端口：不含警示的默认值。
pub fn recommended_port() -> u16 {
    15555
}

#[cfg(test)]
mod arg_tests {
    use super::*;

    #[test]
    fn parses_listen_port() {
        let c = parse_args_from(["--listen-port".to_string(), "16666".to_string()].into_iter());
        assert_eq!(c.listen_port, 16666);
    }

    #[test]
    fn parses_listen_addr_and_serial() {
        let c = parse_args_from(
            [
                "--listen-addr".to_string(),
                "127.0.0.1".to_string(),
                "--serial".to_string(),
                "abc123".to_string(),
            ]
            .into_iter(),
        );
        assert_eq!(c.listen_addr, "127.0.0.1");
        assert_eq!(c.serial.as_deref(), Some("abc123"));
    }

    #[test]
    fn parses_boolean_flags() {
        let c = parse_args_from(
            ["--no-kill-port".to_string(), "--debug-packets".to_string()]
                .into_iter(),
        );
        assert!(c.no_kill_port);
        assert!(c.debug_packets);
    }

    #[test]
    fn invalid_port_falls_back_to_default() {
        let c = parse_args_from(["--listen-port".to_string(), "abc".to_string()].into_iter());
        assert_eq!(c.listen_port, 15555, "非法值应保持默认");
    }

    #[test]
    fn emulator_range_and_recommendation() {
        assert!(is_emulator_port(5555));
        assert!(is_emulator_port(5585));
        assert!(!is_emulator_port(15555));
        assert!(!is_emulator_port(recommended_port()));
    }
}

// ---- 以下函数原在 main.rs，为便于图形界面与测试调用而迁入 lib ----

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

/// 启动桥（关闭标志由内部创建，调用方无法主动停止）。
pub fn start_bridge(cfg: &Config) -> std::io::Result<u16> {
    start_bridge_with_shutdown(cfg, Arc::new(AtomicBool::new(false)))
}

/// 启动桥，并接受一个外部持有的关闭标志。
///
/// 为什么需要它：GUI 的「停止桥」必须能主动结束监听循环。
/// 原 `start_bridge` 内部自己创建 `closed`，外部拿不到，
/// 停止按钮就只能干等（表现为点了没反应）。
pub fn start_bridge_with_shutdown(
    cfg: &Config,
    closed: Arc<AtomicBool>,
) -> std::io::Result<u16> {
    // ---- 先确保监听端口可用（清理上次残留的实例）----
    //
    // 桥重启时上一次的实例常还占着端口（尤其 Windows 上 taskkill 失败、
    // 或进程处于 TIME_WAIT 的情况），直接 bind 会抛 OSError。
    // ⚠️ 库函数不能调用 process::exit —— 那是 CLI 的职责。
    // GUI 模式下 exit 会直接杀死整个进程（连界面一起），必须改为返回错误。
    if !port_guard::prepare_listen_port(
        &cfg.listen_addr,
        cfg.listen_port,
        std::process::id(),
        !cfg.no_kill_port,
    ) {
        return Err(std::io::Error::new(
            std::io::ErrorKind::AddrInUse,
            format!("监听端口 {} 无法释放", cfg.listen_port),
        ));
    }

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
                // ⚠️ 不要在此直接退出：设备可能只是暂时掉线（例如 adb server
                // 被回收后正在重启）。给60 秒窗口，仍无设备才失败。
                log::warn(
                    "bridge",
                    "未检测到在线 adb 设备，等待设备就绪（最多 60 秒）...",
                );
                let mut found = None;
                for _ in 0..60 {
                    std::thread::sleep(std::time::Duration::from_secs(1));
                    if let Some(s) = list_devices().into_iter().next() {
                        found = Some(s);
                        break;
                    }
                }
                match found {
                    Some(s) => s,
                    None => {
                        return Err(std::io::Error::new(
                            std::io::ErrorKind::NotFound,
                            "没有检测到在线 adb 设备，请检查 USB 连接",
                        ));
                    }
                }
            }
        },
    };

    let server_addr = format!("127.0.0.1:{}", cfg.server_port);
    if !server::ensure_server(&server_addr) {
        log::warn("bridge", "adb server 拉起失败，连接时会自动重试");
    }
    let features = server::query_features(&server_addr, &serial);
    let product = server::query_prop(&server_addr, &serial, "get-product");

    // 把设备信息交给界面（界面通过全局注册表读取，桥不直接依赖 GUI）
    stats::set_device(stats::DeviceInfo {
        serial: serial.clone(),
        product: product.clone(),
        features: features.clone(),
    });
    // 清掉上一次运行的会话记录，避免界面上出现幽灵会话
    stats::reset_sessions();

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
                "提示: 上一次实例可能还在运行。可加 --no-kill-port 关闭自动清理，\
                 或手动结束占用进程后重试",
            );
            return Err(e);
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
        closed: closed.clone(),
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

    // accept 循环：不能用 `for stream in srv.incoming()`。
    //
    // ⚠️ `TcpListener::incoming()` 一旦遇到错误（如 Windows 上的 WSAEINTR、
    // 或监听 socket 被系统回收）就会**结束迭代并返回**，导致桥静默退出 ——
    // 表现为「跑完一批命令后进程消失，且没有任何错误日志」。
    // 正确做法是循环 accept，单次错误只记录并继续。
    loop {
        if shared.closed.load(Ordering::Relaxed) {
            break;
        }
        match srv.accept() {
            Ok((sock, _addr)) => {
                let sh = shared.clone();
                std::thread::spawn(move || session::run(sh, sock));
            }
            Err(e) => {
                // 连接数打满(EADDRNOTAVAIL)等情况是瞬时的，稍等再试；
                // 只有监听 socket 本身失效才退出。
                if e.kind() == std::io::ErrorKind::Interrupted {
                    continue;
                }
                log::warn("bridge", format!("accept 失败（将继续重试）: {}", e));
                std::thread::sleep(std::time::Duration::from_millis(200));
            }
        }
    }
    Ok(cfg.listen_port)
}

