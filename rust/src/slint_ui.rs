//! Slint 界面实现。
//!
//! 这里只做三件事：把桥的状态写进界面属性、把用户操作交给桥、按节奏刷新。
//! **转发逻辑一行都不在这里** —— 桥跑在独立线程里，界面只是观察者。
//!
//! # 性能纪律（与 egui 版一致）
//!
//! 1. 桥线程与界面零耦合：数据经 [`crate::stats`] 全局注册表交接，
//!    桥侧只写原子量，不碰任何界面对象。
//! 2. 刷新按 300ms 节奏，不做 60fps 空转。
//! 3. 「停止桥」必须能真正结束监听循环 —— 用外部持有的关闭标志
//!    （[`crate::start_bridge_with_shutdown`]）而不是干等。

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use slint::{ModelRc, SharedString, VecModel};

use crate::app_state::{format_bytes, format_duration, AppState, BridgeState};
use crate::{config, log, start_bridge_with_shutdown, Config};

// 引入 build.rs 编译 `ui/main.slint` 生成的 Rust 绑定
slint::include_modules!();

/// 界面持有的运行时上下文。
struct Ctx {
    state: AppState,
    /// 桥是否在运行
    running: Arc<AtomicBool>,
    /// 传给桥的关闭标志
    shutdown: Arc<AtomicBool>,
    /// 桥线程句柄
    bridge: Option<std::thread::JoinHandle<()>>,
    /// 提示信息（显示在状态栏）
    toast: String,
    /// 诊断结果
    diag: Vec<(String, bool, String)>,
    /// 上一轮显示给界面的日志。用来判断内容是否真的变了 ——
    /// 只看条数不够，清空日志时长数会变、滚动条最短的日志被挤出时条数也不变。
    log_cache: Vec<LogRow>,
    /// 日志滚动触发计数。每次内容变化 +1，界面侧 `changed log-tick` 靠它唤醒滚动。
    log_tick: i32,
}

/// 比较两批日志是否不同（逐字段比，因为 LogRow 不实现 PartialEq）。
fn rows_differ(a: &[LogRow], b: &[LogRow]) -> bool {
    a.len() != b.len()
        || a.iter()
            .zip(b.iter())
            .any(|(x, y)| x.time != y.time || x.level != y.level || x.text != y.text)
}

/// 启动 Slint 界面。
pub fn run(cfg: Config) {
    let ui = MainWindow::new().expect("创建 Slint 窗口失败");

    let mut state = AppState::default();

    // 恢复上次保存的配置（与 egui 版同一套优先级）：
    //   **命令行显式给的值 > 配置文件 > 内置默认**
    // 判据是「用户在命令行里写没写这个开关」，而不是「值是否等于默认值」，
    // 否则用户显式写 --listen-port 15555（恰好等于默认）会被配置文件覆盖掉。
    let loaded = config::load();
    let raw_args: Vec<String> = std::env::args().collect();
    let addr_given = raw_args.iter().any(|a| a == "--listen-addr");
    let port_given = raw_args.iter().any(|a| a == "--listen-port");
    let serial_given = raw_args.iter().any(|a| a == "--serial");

    state.listen_addr = if addr_given {
        cfg.listen_addr.clone()
    } else {
        loaded.listen_addr
    };
    state.listen_port = if port_given { cfg.listen_port } else { loaded.listen_port };
    state.local_ip = crate::local_ip();
    state.no_kill_port = cfg.no_kill_port;
    state.debug_packets = cfg.debug_packets;
    state.server_port = cfg.server_port;
    state.serial = if serial_given {
        cfg.serial.clone().unwrap_or_default()
    } else {
        loaded.serial.unwrap_or_default()
    };

    let ctx = Arc::new(Mutex::new(Ctx {
        state,
        running: Arc::new(AtomicBool::new(false)),
        shutdown: Arc::new(AtomicBool::new(false)),
        bridge: None,
        toast: String::new(),
        diag: Vec::new(),
        log_cache: Vec::new(),
        log_tick: 0,
    }));

    // 首屏：把配置写进界面
    {
        let mut g = ctx.lock().unwrap();
        // 设置页初值
        ui.set_set_port(SharedString::from(g.state.listen_port.to_string()));
        ui.set_set_addr(SharedString::from(g.state.listen_addr.clone()));
        ui.set_set_serial(SharedString::from(g.state.serial.clone()));
        ui.set_set_no_kill(g.state.no_kill_port);
        ui.set_set_debug(g.state.debug_packets);
        ui.set_auto_scroll(g.state.auto_scroll);
    }

    bind_callbacks(&ui, &ctx);

    // 按节奏刷新（纪律 2）
    let weak = ui.as_weak();
    let c = ctx.clone();
    let timer = slint::Timer::default();
    timer.start(
        slint::TimerMode::Repeated,
        Duration::from_millis(300),
        move || {
            let Some(ui) = weak.upgrade() else {
                return;
            };
            let mut g = match c.lock() {
                Ok(g) => g,
                Err(_) => return,
            };
            g.state.refresh_from_stats();
            sync_ui(&ui, &mut g);
        },
    );

    ui.run().expect("Slint 事件循环退出异常");
}

/// 把上下文状态写进界面属性。
///
/// 参数是 `&mut Ctx` 而不是 `&Ctx`：这里要从界面读回「自动滚动」开关写进
/// state，并递增日志滚动触发计数，两者都是写操作。
fn sync_ui(ui: &MainWindow, g: &mut Ctx) {
    // 从界面读回自动滚动开关，以界面为准（初始化顺序上 AppState 可能是旧的）
    g.state.auto_scroll = ui.get_auto_scroll();
    let st = &g.state;
    ui.set_page(st.page as i32);
    ui.set_status_label(SharedString::from(st.bridge_state.label()));
    ui.set_listen_text(SharedString::from(format!("{}:{}", st.listen_addr, st.listen_port)));
    ui.set_device_text(SharedString::from(if st.serial.is_empty() {
        "未选择".to_string()
    } else {
        st.serial.clone()
    }));
    ui.set_product_text(SharedString::from(if st.product.is_empty() {
        "-".to_string()
    } else {
        st.product.clone()
    }));
    ui.set_features_text(SharedString::from(if st.features.is_empty() {
        "-".to_string()
    } else {
        st.features.clone()
    }));
    ui.set_connect_cmd(SharedString::from(st.connect_command()));
    ui.set_session_count(st.session_count() as i32);
    let (up, down) = st.total_traffic();
    ui.set_up_text(SharedString::from(format_bytes(up)));
    ui.set_down_text(SharedString::from(format_bytes(down)));
    ui.set_port_danger(st.port_is_dangerous());
    ui.set_suggested_port(SharedString::from(st.suggested_port().to_string()));
    ui.set_running(g.running.load(Ordering::Relaxed));
    ui.set_toast(SharedString::from(g.toast.clone()));

    // 会话表
    let rows: Vec<SessionRow> = st
        .sessions
        .iter()
        .map(|s| SessionRow {
            peer: SharedString::from(s.peer.clone()),
            elapsed: SharedString::from(format_duration(s.elapsed)),
            up: SharedString::from(format_bytes(s.up)),
            down: SharedString::from(format_bytes(s.down)),
            streams: s.streams as i32,
            active: s.active,
        })
        .collect();
    ui.set_sessions(ModelRc::new(VecModel::from(rows)));

    // 日志（只取最近 400 行）
    let logs: Vec<LogRow> = log::snapshot(400)
        .into_iter()
        .map(|r| LogRow {
            time: SharedString::from(fmt_time(r.ts_ms)),
            level: SharedString::from(format!("{:?}", r.level).to_uppercase()),
            category: SharedString::from(r.source),
            text: SharedString::from(r.message),
        })
        .collect();
    // 先比较再赋值：只有内容真的变了才通知界面滚动。
    // 每 300ms 刷一次，如果无条件递增 tick，哪怕没有新日志也会每 0.3s
    // 触发一次滚动 —— 用户想往上翻历史日志会被反复拽回底部。
    let rows_changed = rows_differ(&g.log_cache, &logs);
    if rows_changed {
        g.log_cache = logs.clone();
        // 通知界面滚到底
        g.log_tick = g.log_tick.wrapping_add(1);
    }
    ui.set_logs(ModelRc::new(VecModel::from(logs)));
    if rows_changed {
        ui.set_log_tick(g.log_tick);
    }

    // 诊断结果
    let diag: Vec<DiagRow> = g
        .diag
        .iter()
        .map(|(name, ok, detail)| DiagRow {
            name: SharedString::from(name.clone()),
            ok: *ok,
            detail: SharedString::from(detail.clone()),
        })
        .collect();
    ui.set_diag(ModelRc::new(VecModel::from(diag)));
}

/// 毫秒时间戳 → HH:MM:SS。
///
/// ⚠️ 这里显示的是 **UTC**。Rust 标准库没有时区转换，
/// 正确做法要调 Windows 的 `GetTimeZoneInformation` 或引入 chrono。
/// 已知局限，后续再修正（日志排序与时间差仍然正确）。
fn fmt_time(ts_ms: u64) -> String {
    let s = ts_ms / 1000;
    format!(
        "{:02}:{:02}:{:02}",
        (s / 3600) % 24,
        (s % 3600) / 60,
        s % 60
    )
}

/// 绑定界面回调。
fn bind_callbacks(ui: &MainWindow, ctx: &Arc<Mutex<Ctx>>) {
    // ---- 切换页面 ----
    let c = ctx.clone();
    ui.on_pick_page(move |i| {
        if let Ok(mut g) = c.lock() {
            g.state.page = i.max(0) as usize;
        }
    });

    // ---- 启动 / 停止桥 ----
    let c = ctx.clone();
    ui.on_toggle_bridge(move || {
        let mut g = match c.lock() {
            Ok(g) => g,
            Err(_) => return,
        };
        if g.running.load(Ordering::Relaxed) {
            stop_bridge(&mut g);
        } else {
            start_bridge(&mut g);
        }
    });

    // ---- 复制连接命令 ----
    let c = ctx.clone();
    ui.on_copy_command(move || {
        let mut g = match c.lock() {
            Ok(g) => g,
            Err(_) => return,
        };
        let cmd = g.state.connect_command();
        g.toast = format!("连接命令: {}（已记入日志）", cmd);
        log::info("gui", format!("连接命令: {}", cmd));
    });

    // ---- 清空日志 ----
    ui.on_clear_logs(|| {
        log::clear();
    });

    // ---- 运行自检 ----
    let c = ctx.clone();
    ui.on_run_diagnostics(move || {
        let mut g = match c.lock() {
            Ok(g) => g,
            Err(_) => return,
        };
        g.diag = run_diagnostics(&g.state);
        g.toast = "自检完成".to_string();
    });

    // ---- 保存参数 ----
    let c = ctx.clone();
    let weak = ui.as_weak();
    ui.on_save_settings(move || {
        let mut g = match c.lock() {
            Ok(g) => g,
            Err(_) => return,
        };
        // 读回界面上编辑过的值
        if let Some(ui) = weak.upgrade() {
            if let Ok(p) = ui.get_set_port().parse::<u16>() {
                g.state.listen_port = p;
            }
            g.state.listen_addr = ui.get_set_addr().to_string();
            g.state.serial = ui.get_set_serial().to_string();
            g.state.no_kill_port = ui.get_set_no_kill();
            g.state.debug_packets = ui.get_set_debug();
        }
        let cfg = build_config(&g.state);
        match config::save(&cfg) {
            Ok(p) => {
                g.toast = format!("已保存到 {}", p.display());
                log::info("gui", format!("配置已保存到 {}", p.display()));
            }
            Err(e) => {
                g.toast = format!("保存失败: {}", e);
                log::error("gui", format!("保存配置失败: {}", e));
            }
        }
        g.state.bridge_state = if g.running.load(Ordering::Relaxed) {
            BridgeState::Running
        } else {
            BridgeState::Stopped
        };
    });

    // ---- 刷新设备列表 ----
    let c = ctx.clone();
    ui.on_refresh_devices(move || {
        let mut g = match c.lock() {
            Ok(g) => g,
            Err(_) => return,
        };
        let devs = crate::list_devices();
        if let Some(first) = devs.first() {
            g.state.serial = first.clone();
            g.toast = format!("已选择 {}", first);
        } else {
            g.toast = "未检测到在线设备".to_string();
        }
    });
}

/// 由界面状态构造桥的配置。
fn build_config(st: &AppState) -> Config {
    Config {
        listen_port: st.listen_port,
        listen_addr: st.listen_addr.clone(),
        serial: if st.serial.is_empty() {
            None
        } else {
            Some(st.serial.clone())
        },
        server_port: st.server_port,
        no_kill_port: st.no_kill_port,
        debug_packets: st.debug_packets,
    }
}

/// 启动桥（在独立线程里跑，界面不受影响）。
fn start_bridge(g: &mut Ctx) {
    if g.running.load(Ordering::Relaxed) {
        return;
    }
    g.state.bridge_state = BridgeState::Starting;
    g.toast.clear();

    let cfg = build_config(&g.state);
    let running = g.running.clone();
    let shutdown = g.shutdown.clone();
    shutdown.store(false, Ordering::Relaxed);

    let handle = std::thread::spawn(move || {
        running.store(true, Ordering::Relaxed);
        log::info("bridge", "GUI 已启动桥");
        let r = start_bridge_with_shutdown(&cfg, shutdown.clone());
        running.store(false, Ordering::Relaxed);
        match r {
            Ok(p) => log::info("bridge", format!("桥已停止（端口 {}）", p)),
            Err(e) => log::error("bridge", format!("启动失败: {}", e)),
        }
    });

    g.bridge = Some(handle);
    g.state.bridge_state = BridgeState::Running;
    g.toast = "桥已启动".to_string();
}

/// 停止桥。
///
/// ⚠️ 光置关闭标志不够：`accept()` 正阻塞着，要等下一个连接进来才会返回。
/// 因此这里**主动连一下自己的监听端口**把 accept 唤醒 —— 少了这一步，
/// 停止按钮会「点了没反应」，直到有客户端连进来才生效。
fn stop_bridge(g: &mut Ctx) {
    g.shutdown.store(true, Ordering::Relaxed);

    // 唤醒阻塞中的 accept
    let port = g.state.listen_port;
    let _ = std::net::TcpStream::connect(format!("127.0.0.1:{}", port));

    if let Some(h) = g.bridge.take() {
        // 最多等 3 秒，超时就让它自己退出（不阻塞界面）
        let deadline = Instant::now() + Duration::from_secs(3);
        loop {
            if h.is_finished() {
                let _ = h.join();
                break;
            }
            if Instant::now() >= deadline {
                log::warn("bridge", "桥线程未能在 3 秒内退出，已放弃等待");
                break;
            }
            std::thread::sleep(Duration::from_millis(50));
        }
    }

    g.state.bridge_state = BridgeState::Stopped;
    g.toast = "桥已停止".to_string();
}

/// 运行自检（耗时操作，但都在秒级以内，直接同步跑）。
fn run_diagnostics(st: &AppState) -> Vec<(String, bool, String)> {
    let mut rows: Vec<(String, bool, String)> = Vec::new();

    // 1. adb 是否可用
    let adb_ok = std::process::Command::new("adb")
        .arg("version")
        .stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::null())
        .status()
        .map(|s| s.success())
        .unwrap_or(false);
    rows.push((
        "adb 可执行".into(),
        adb_ok,
        if adb_ok {
            "PATH 中找到 adb".into()
        } else {
            "未找到 adb，请确认已加入 PATH".into()
        },
    ));

    // 2. adb server 是否活着
    let srv = format!("127.0.0.1:{}", st.server_port);
    let alive = crate::server::server_alive(&srv);
    rows.push((
        "adb server".into(),
        alive,
        if alive {
            format!("{} 可连接", srv)
        } else {
            format!("{} 不可连接，启动桥时会自动拉起", srv)
        },
    ));

    // 3. 端口是否落在模拟器区间
    let danger = st.port_is_dangerous();
    rows.push((
        "端口区间".into(),
        !danger,
        if danger {
            format!("端口 {} 落在 5555~5585，会产生幽灵设备", st.listen_port)
        } else {
            format!("端口 {} 安全", st.listen_port)
        },
    ));

    // 4. 设备是否在线
    let devs = crate::list_devices();
    rows.push((
        "USB 设备".into(),
        !devs.is_empty(),
        if devs.is_empty() {
            "未检测到在线设备，请检查 USB 连接".into()
        } else {
            devs.join(", ")
        },
    ));

    rows
}
