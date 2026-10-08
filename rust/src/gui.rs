//! 图形界面（egui/eframe）。
//!
//! 界面分五页：控制台 / 会话与流量 / 运行日志 / 参数设置 / 诊断工具。
//!
//! # 性能纪律（三条硬性红线）
//!
//! 1. **转发线程内绝不触碰任何 GUI 对象** —— 日志只写环形缓冲，
//!    由 UI 线程按 300ms 节奏拉取快照。
//! 2. **所有耗时操作 offload 到 worker 线程** —— 端口检查要跑
//!    `netstat`/`tasklist`，设备探测要跑 `adb`。放 UI 主线程会阻塞事件循环，
//!    表现为窗口「假死」。
//! 3. **UI 刷新要稀** —— 300ms 一次（见 `REFRESH_INTERVAL`），
//!    足够跟手又不浪费 CPU。
//!
//! 桥本体跑在**独立线程**里，与 UI 通过 `AppState` 的原子标志与
//! 会话互访量通信，因此界面卡顿不会影响数据转发。

use std::sync::atomic::Ordering;
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use super::app_state::{format_bytes, format_duration, AppState, BridgeState};
use super::{config, log, port_guard, server};
use super::Config;
use eframe::egui;

/// 五个页面的标题。
const PAGES: [&str; 5] = ["控制台", "会话与流量", "运行日志", "参数设置", "诊断工具"];

/// 应用本体。
pub struct BridgeApp {
    state: AppState,
    /// 桥是否正在运行
    running: Arc<std::sync::atomic::AtomicBool>,
    /// 桥线程句柄
    handle: Option<std::thread::JoinHandle<()>>,
    /// 启动时的自检结果（诊断页展示）
    diag: Vec<(String, bool, String)>,
    /// 上次点「运行自检」的时刻
    last_diag: Option<Instant>,
    /// 待保存的提示
    toast: Option<(String, bool)>,
}

impl Default for BridgeApp {
    fn default() -> Self {
        BridgeApp {
            state: AppState::default(),
            running: Arc::new(std::sync::atomic::AtomicBool::new(false)),
            handle: None,
            diag: Vec::new(),
            last_diag: None,
            toast: None,
        }
    }
}

impl BridgeApp {
    pub fn new(cfg: &Config) -> Self {
        let loaded = config::load();
        let mut st = AppState::default();
        st.listen_addr = if cfg.listen_addr != "0.0.0.0" {
            cfg.listen_addr.clone()
        } else {
            loaded.listen_addr
        };
        // 命令行显式给了端口就用命令行的，否则用配置文件的
        let port_given = std::env::args().any(|a| a == "--listen-port");
        st.listen_port = if port_given { cfg.listen_port } else { loaded.listen_port };
        st.serial = cfg.serial.clone().or(loaded.serial).unwrap_or_default();
        st.server_port = cfg.server_port;
        st.no_kill_port = cfg.no_kill_port;
        st.debug_packets = cfg.debug_packets;
        st.local_ip = crate::local_ip();
        Self {
            state: st,
            ..Default::default()
        }
    }

    /// 启动桥（放到 worker 线程，绝不阻塞 UI）。
    fn start(&mut self) {
        if self.running.load(Ordering::Relaxed) {
            self.toast = Some(("桥已在运行".into(), false));
            return;
        }
        let cfg = Config {
            listen_addr: self.state.listen_addr.clone(),
            listen_port: self.state.listen_port,
            serial: if self.state.serial.is_empty() {
                None
            } else {
                Some(self.state.serial.clone())
            },
            server_port: self.state.server_port,
            no_kill_port: self.state.no_kill_port,
            debug_packets: self.state.debug_packets,
        };
        if self.state.port_is_dangerous() {
            self.toast = Some((
                format!("端口 {} 会被 adb server 当成模拟器，建议改用 {}", self.state.listen_port, self.state.suggested_port()),
                false,
            ));
        }

        // 保存配置
        let _ = config::save(&Config {
            serial: if self.state.serial.is_empty() {
                None
            } else {
                Some(self.state.serial.clone())
            },
            ..cfg.clone()
        });

        // ⚠️ 纪律 2：启动桥是耗时操作（端口检查 + adb 探测），必须放 worker 线程
        let running = self.running.clone();
        let port = self.state.listen_port;
        self.state.bridge_state = BridgeState::Starting;
        self.handle = Some(std::thread::spawn(move || {
            running.store(true, Ordering::Relaxed);
            log::info("bridge", "GUI 已启动桥");
            let _ = crate::start_bridge(&cfg);
            running.store(false, Ordering::Relaxed);
            log::info("bridge", format!("桥已停止（端口 {}）", port));
        }));
        self.state.bridge_state = BridgeState::Running;
        self.toast = Some(("桥已启动".into(), true));
    }

    /// 停止桥。
    fn stop(&mut self) {
        if let Some(h) = self.handle.take() {
            // 桥的监听循环以共享标志控制退出
            self.state.shutdown.store(true, Ordering::Relaxed);
            let _ = h.join();
            self.state.bridge_state = BridgeState::Stopped;
            self.toast = Some(("桥已停止".into(), true));
        }
    }

    /// 刷新设备信息（耗时，放 worker）。
    fn refresh_devices(&mut self) {
        let serial = self.state.serial.clone();
        let addr = format!("127.0.0.1:{}", self.state.server_port);
        self.state.bridge_state = BridgeState::Starting;
        std::thread::spawn(move || {
            let devs = crate::list_devices();
            let s = if serial.is_empty() {
                devs.first().cloned().unwrap_or_default()
            } else {
                serial
            };
            // 交给主线程消费：这里只算好，不碰 UI
            let features = server::query_features(&addr, &s);
            let product = server::query_prop(&addr, &s, "get-product");
            log::debug("gui", format!("设备 {} features={} product={}", s, features, product));
        });
        self.state.bridge_state = if self.running.load(Ordering::Relaxed) {
            BridgeState::Running
        } else {
            BridgeState::Stopped
        };
    }

    /// 运行自检（全部耗时操作，逐项放 worker）。
    fn run_diagnostics(&mut self) {
        if self.last_diag.map(|t| t.elapsed() < Duration::from_secs(3)).unwrap_or(false) {
            return;
        }
        self.last_diag = Some(Instant::now());
        let port = self.state.listen_port;
        let addr = self.state.listen_addr.clone();
        let serial = self.state.serial.clone();
        let srv = format!("127.0.0.1:{}", self.state.server_port);
        let running = self.running.load(Ordering::Relaxed);

        // ⚠️ 纪律 2：netstat/tasklist/adb 都是耗时命令，必须离开 UI 线程
        std::thread::spawn(move || {
            let mut rows: Vec<(String, bool, String)> = Vec::new();

            // 1 端口占用
            let ls = port_guard::netstat_listeners(port);
            match &ls {
                Some(v) if v.is_empty() => {
                    rows.push(("端口占用检查".into(), true, format!("端口 {} 空闲", port)))
                }
                Some(v) => {
                    let names: Vec<String> = v
                        .iter()
                        .map(|l| {
                            format!(
                                "PID={}({})",
                                l.pid,
                                port_guard::process_name(l.pid).unwrap_or_else(|| "?".into())
                            )
                        })
                        .collect();
                    rows.push((
                        "端口占用检查".into(),
                        false,
                        format!("端口 {} 被占用: {}", port, names.join(", ")),
                    ));
                }
                None => rows.push((
                    "端口占用检查".into(),
                    false,
                    "无法执行 netstat".into(),
                )),
            }

            // 2 adb server
            let alive = server::server_alive(&srv);
            rows.push((
                "adb server".into(),
                alive,
                format!("{} {}", srv, if alive { "可达" } else { "不可达" }),
            ));

            // 3 设备
            let devs = crate::list_devices();
            if devs.is_empty() {
                rows.push(("设备在线".into(), false, "没有检测到在线设备".into()));
            } else {
                let s = if serial.is_empty() {
                    devs[0].clone()
                } else {
                    serial.clone()
                };
                rows.push((
                    "设备在线".into(),
                    true,
                    format!("{}（共 {} 台）", s, devs.len()),
                ));
                // 4 features 协商
                let f = server::query_features(&srv, &s);
                let has_v2 = f.contains("shell_v2");
                rows.push((
                    "协议协商".into(),
                    has_v2,
                    if f.is_empty() {
                        "未获取到 features（将按老协议工作）".into()
                    } else {
                        format!("features: {}", f)
                    },
                ));
                // 5 product
                let p = server::query_prop(&srv, &s, "get-product");
                if !p.is_empty() {
                    rows.push(("设备型号".into(), true, p));
                }
            }

            // 6 桥状态
            rows.push((
                "桥运行状态".into(),
                running,
                if running { "运行中".into() } else { "未启动".to_string() },
            ));
            let _ = addr;

            for (n, ok, d) in &rows {
                log::debug("diag", format!("{} {} {}", if *ok { "OK" } else { "!!" }, n, d));
            }
        });
    }
}

/// Windows 中文字体候选（按顺序尝试）。
///
/// ⚠️ **egui 内置字体只有拉丁字形，不含中文** —— 不注入中文字体的话，
/// 界面上所有汉字都会渲染成方块（tofu）。这是 Rust GUI 最容易踩的坑之一。
///
/// 优先 `simhei.ttf`：它是独立的 TrueType（非 TTC 集合），解析最稳。
/// `msyh.ttc` 是雅黑的字体集合，部分 ttf-parser 版本处理 TTC 会失败，故放次选。
const FONT_CANDIDATES: &[&str] = &[
    "C:\\Windows\\Fonts\\simhei.ttf",
    "C:\\Windows\\Fonts\\msyh.ttc",
    "C:\\Windows\\Fonts\\NotoSansSC-VF.ttf",
    "C:\\Windows\\Fonts\\simsun.ttc",
    "C:\\Windows\\Fonts\\simkai.ttf",
];

/// 注入中文字体。
///
/// 必须在创建 UI 之前调用，否则第一帧就已经是方块了。
/// 找不到任何中文字体时不 panic，退化为内置字体（英文正常，中文方块），
/// 并记一条错误日志便于排查。
fn setup_fonts(ctx: &egui::Context) {
    let mut picked: Option<(String, Vec<u8>)> = None;
    for path in FONT_CANDIDATES {
        if let Ok(data) = std::fs::read(path) {
            let name = path
                .rsplit(['\\', '/'])
                .next()
                .unwrap_or(path)
                .to_string();
            picked = Some((name, data));
            break;
        }
    }

    let Some((name, data)) = picked else {
        log::error(
            "gui",
            "未找到系统中文字体，界面汉字将显示为方块。请确认 C:\\Windows\\Fonts 下有 simhei.ttf",
        );
        return;
    };

    let mut fonts = egui::FontDefinitions::default();
    // egui 0.31 起 font_data 存的是 Arc<FontData>（便于多 Context 共享同一份字形数据）
    fonts
        .font_data
        .insert(name.clone(), std::sync::Arc::new(egui::FontData::from_owned(data)));

    // 比例字体用于常规文本，等宽字体用于日志/serial/端口号。
    // ⚠️ 两者都要挂：只挂 Proportional 的话，日志区的中文仍是方块。
    fonts
        .families
        .entry(egui::FontFamily::Proportional)
        .or_default()
        .insert(0, name.clone());
    fonts
        .families
        .entry(egui::FontFamily::Monospace)
        .or_default()
        .insert(0, name.clone());

    ctx.set_fonts(fonts);
    log::info("gui", format!("已加载界面字体: {}", name));
}

/// 启动 GUI。
pub fn run(cfg: Config) {
    let app = BridgeApp::new(&cfg);
    let mut native_options = eframe::NativeOptions::default();
    native_options.viewport = egui::ViewportBuilder::default()
        .with_title("ADB TCP 桥控制台")
        .with_inner_size([1180.0, 760.0])
        .with_min_inner_size([980.0, 640.0]);
    eframe::run_native(
        "ADB TCP 桥控制台",
        native_options,
        Box::new(|cc| {
            // 字体必须在渲染任何一帧之前注入
            setup_fonts(&cc.egui_ctx);
            Ok(Box::new(app))
        }),
    )
    .map_err(|e| log::error("gui", format!("界面启动失败: {}", e)))
    .ok();
}

// ---------------- eframe 实现 ----------------

impl eframe::App for BridgeApp {
    fn update(&mut self, ctx: &egui::Context, _frame: &mut eframe::Frame) {
        // 纪律 3：按节奏请求重绘，而不是持续 60fps 空转。
        // 同一节拍里从全局统计注册表拉一次快照 —— 这是桥与界面唯一的
        // 数据交接点，桥侧只写原子量，因此这里几乎不占用锁。
        if self.state.should_refresh() {
            self.state.refresh_from_stats();
            self.state.mark_refreshed();
            ctx.request_repaint();
        } else {
            ctx.request_repaint_after(Duration::from_millis(200));
        }

        apply_theme(ctx);

        egui::TopBottomPanel::top("top").show(ctx, |ui| {
            self.top_bar(ui);
        });

        egui::SidePanel::left("nav")
            .exact_width(168.0)
            .show(ctx, |ui| self.nav(ui));

        egui::CentralPanel::default().show(ctx, |ui| {
            match self.state.page {
                0 => self.page_dashboard(ui),
                1 => self.page_sessions(ui),
                2 => self.page_logs(ui),
                3 => self.page_settings(ui),
                _ => self.page_diag(ui),
            }
        });

        // 状态条
        egui::TopBottomPanel::bottom("status").show(ctx, |ui| {
            ui.horizontal(|ui| {
                let st = &self.state;
                let running = self.running.load(Ordering::Relaxed);
                let color = if running {
                    egui::Color32::from_rgb(62, 207, 142)
                } else {
                    egui::Color32::from_rgb(160, 168, 184)
                };
                ui.colored_label(color, format!("● {}", st.bridge_state.label()));
                ui.separator();
                ui.label(format!("端口 {}", st.listen_port));
                ui.separator();
                ui.label(format!("设备 {}", if st.serial.is_empty() { "未选择".into() } else { st.serial.clone() }));
                ui.separator();
                ui.label(format!("会话 {}", st.session_count()));
                let (up, down) = st.total_traffic();
                ui.separator();
                ui.label(format!("↑{} ↓{}", format_bytes(up), format_bytes(down)));
                if let Some((msg, ok)) = &self.toast {
                    ui.separator();
                    ui.colored_label(
                        if *ok {
                            egui::Color32::from_rgb(62, 207, 142)
                        } else {
                            egui::Color32::from_rgb(240, 180, 41)
                        },
                        msg,
                    );
                }
            });
        });
    }

    fn on_exit(&mut self, _gl: Option<&eframe::glow::Context>) {
        // 关窗口 = 停桥（与 README 中说明的行为一致）
        self.state.shutdown.store(true, Ordering::Relaxed);
    }
}

impl BridgeApp {
    fn top_bar(&mut self, ui: &mut egui::Ui) {
        ui.horizontal(|ui| {
            ui.heading("ADB TCP 桥");
            ui.label("— 把 USB 设备共享到局域网");
            ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                let running = self.running.load(Ordering::Relaxed);
                if running {
                    if ui.button("■ 停止").clicked() {
                        self.stop();
                    }
                } else if ui.button("▶ 启动桥").clicked() {
                    self.start();
                }
            });
        });
        ui.separator();
    }

    fn nav(&mut self, ui: &mut egui::Ui) {
        ui.add_space(6.0);
        for (i, t) in PAGES.iter().enumerate() {
            let sel = self.state.page == i;
            if ui.selectable_label(sel, *t).clicked() {
                self.state.page = i;
            }
        }
        ui.add_space(12.0);
        ui.separator();
        ui.add_space(8.0);
        // 端口警示常驻
        if self.state.port_is_dangerous() {
            ui.colored_label(
                egui::Color32::from_rgb(240, 180, 41),
                format!("危险端口 {}", self.state.listen_port),
            );
            if ui.button(format!("改用 {}", self.state.suggested_port())).clicked() {
                self.state.listen_port = self.state.suggested_port();
            }
        }
        ui.with_layout(egui::Layout::bottom_up(egui::Align::Min), |ui| {
            ui.label(format!("A 的 IP: {}", self.state.local_ip));
            ui.label(format!("设备: {}", if self.state.serial.is_empty() { "-".into() } else { self.state.serial.clone() }));
            ui.label(format!("features: {}", if self.state.features.is_empty() { "-".into() } else { self.state.features.clone() }));
        });
    }

    fn page_dashboard(&mut self, ui: &mut egui::Ui) {
        let st = &self.state;
        // 运行状态横幅
        let running = self.running.load(Ordering::Relaxed);
        let (bg, fg) = if running {
            (
                egui::Color32::from_rgb(20, 37, 29),
                egui::Color32::from_rgb(62, 207, 142),
            )
        } else {
            (
                egui::Color32::from_rgb(37, 22, 26),
                egui::Color32::from_rgb(242, 85, 90),
            )
        };
        egui::Frame::group(ui.style()).fill(bg).show(ui, |ui: &mut egui::Ui| {
            ui.horizontal(|ui| {
                ui.colored_label(fg, if running { "●" } else { "○" });
                ui.label(if running {
                    "桥正在运行，设备已暴露到局域网"
                } else {
                    "桥未运行"
                });
            });
            ui.label(format!(
                "{}:{} → adb server → {}",
                st.listen_addr, st.listen_port, st.serial
            ));
        });

        ui.add_space(10.0);
        ui.columns(4, |cols| {
            let (up, down) = st.total_traffic();
            stat_card(&mut cols[0], "监听地址", &format!("{}:{}", st.listen_addr, st.listen_port), "");
            stat_card(&mut cols[1], "目标设备", if st.serial.is_empty() { "未选择" } else { &st.serial }, &format!("product: {}", if st.product.is_empty() { "?" } else { &st.product }));
            stat_card(&mut cols[2], "连接数", &st.session_count().to_string(), "");
            stat_card(&mut cols[3], "流量", &format!("↑{} ↓{}", format_bytes(up), format_bytes(down)), "");
        });

        ui.add_space(14.0);
        ui.heading("电脑 B 连接方式");
        ui.horizontal(|ui| {
            let cmd = st.connect_command();
            ui.monospace(&cmd);
            if ui.button("📋 复制").clicked() {
                ui.ctx().copy_text(cmd.clone());
                self.toast = Some(("已复制到剪贴板".into(), true));
            }
        });
        ui.label("在电脑 B 上执行后，即可像普通网络设备一样使用 adb shell / push / pull / logcat。");

        if st.port_is_dangerous() {
            ui.colored_label(
                egui::Color32::from_rgb(240, 180, 41),
                format!("当前端口 {} 落在 5555~5585，会产生幽灵设备 emulator-5554，建议改用 {}", st.listen_port, st.suggested_port()),
            );
        }

        ui.add_space(14.0);
        ui.colored_label(
            egui::Color32::from_rgb(242, 85, 90),
            "安全提醒：设备 C 的 ADB shell 为 root，开放监听端口等同于把完整 root shell 暴露给局域网。",
        );
        ui.separator();
        ui.label("诊断工具页可运行自检，确认端口、设备、协议协商均正常。");
    }

    fn page_sessions(&mut self, ui: &mut egui::Ui) {
        ui.heading("会话与流量");
        ui.separator();
        if self.state.sessions.is_empty() {
            ui.label("当前没有客户端接入。");
            ui.label("在电脑 B 上执行 adb connect 后，这里会显示连接详情。");
            return;
        }
        egui::Grid::new("sessions")
            .striped(true)
            .num_columns(6)
            .spacing([16.0, 6.0])
            .show(ui, |ui: &mut egui::Ui| {
                ui.label("客户端 IP");
                ui.label("接入时长");
                ui.label("上行");
                ui.label("下行");
                ui.label("stream");
                ui.label("状态");
                ui.end_row();
                for s in &self.state.sessions {
                    ui.label(&s.peer);
                    ui.label(format_duration(s.elapsed));
                    ui.label(format_bytes(s.up));
                    ui.label(format_bytes(s.down));
                    ui.label(s.streams.to_string());
                    if s.active {
                        ui.colored_label(
                            egui::Color32::from_rgb(62, 207, 142),
                            "进行中",
                        );
                    } else {
                        ui.colored_label(
                            egui::Color32::from_rgb(140, 148, 164),
                            "已断开",
                        );
                    }
                    ui.end_row();
                }
            });
    }

    fn page_logs(&mut self, ui: &mut egui::Ui) {
        ui.horizontal(|ui| {
            ui.heading("运行日志");
            ui.checkbox(&mut self.state.auto_refresh, "自动刷新");
            if ui.button("清空").clicked() {
                log::clear();
            }
        });
        ui.separator();
        let records = log::snapshot(2000);
        egui::ScrollArea::vertical()
            .stick_to_bottom(true)
            .auto_shrink([false, false])
            .show(ui, |ui: &mut egui::Ui| {
                for r in &records {
                    let (ts, msg) = format_record(r);
                    let color = match r.level {
                        log::Level::Error => egui::Color32::from_rgb(242, 85, 90),
                        log::Level::Warn => egui::Color32::from_rgb(240, 180, 41),
                        log::Level::Info => egui::Color32::from_rgb(220, 226, 236),
                        log::Level::Debug => egui::Color32::from_rgb(140, 150, 168),
                    };
                    ui.colored_label(color, format!("{} {}", ts, msg));
                }
            });
    }

    fn page_settings(&mut self, ui: &mut egui::Ui) {
        ui.heading("参数设置");
        ui.label("保存后需重启桥生效。");
        ui.separator();

        egui::Grid::new("settings").num_columns(2).show(ui, |ui: &mut egui::Ui| {
            ui.label("监听端口");
            let mut p = self.state.listen_port.to_string();
            if ui.text_edit_singleline(&mut p).changed() {
                if let Ok(v) = p.parse::<u16>() {
                    self.state.listen_port = v;
                }
            }
            ui.end_row();

            ui.label("监听地址");
            egui::ComboBox::from_label("")
                .selected_text(&self.state.listen_addr)
                .show_ui(ui, |ui| {
                    for a in ["0.0.0.0（局域网可访问）", "127.0.0.1（仅本机）"] {
                        let key = a.split('（').next().unwrap().to_string();
                        ui.selectable_value(
                            &mut self.state.listen_addr,
                            key.clone(),
                            a.to_string(),
                        );
                    }
                });
            ui.end_row();

            ui.label("设备 serial");
            ui.text_edit_singleline(&mut self.state.serial);
            ui.end_row();

            ui.label("adb server 端口");
            let mut sp = self.state.server_port.to_string();
            if ui.text_edit_singleline(&mut sp).changed() {
                if let Ok(v) = sp.parse::<u16>() {
                    self.state.server_port = v;
                }
            }
            ui.end_row();
        });

        ui.add_space(10.0);
        ui.checkbox(&mut self.state.no_kill_port, "端口被占用时不自动结束占用进程");
        ui.checkbox(&mut self.state.debug_packets, "记录 ADB 协议包明细（排障用，量很大）");
        ui.checkbox(&mut self.state.auto_refresh, "界面自动刷新");

        ui.add_space(14.0);
        ui.horizontal(|ui| {
            if ui.button("💾 保存配置").clicked() {
                let cfg = Config {
                    listen_addr: self.state.listen_addr.clone(),
                    listen_port: self.state.listen_port,
                    serial: if self.state.serial.is_empty() {
                        None
                    } else {
                        Some(self.state.serial.clone())
                    },
                    server_port: self.state.server_port,
                    no_kill_port: self.state.no_kill_port,
                    debug_packets: self.state.debug_packets,
                };
                match config::save(&cfg) {
                    Ok(p) => self.toast = Some((format!("已保存到 {}", p.display()), true)),
                    Err(e) => self.toast = Some((format!("保存失败: {}", e), false)),
                }
            }
            if ui.button("🔄 重新检测设备").clicked() {
                self.refresh_devices();
            }
        });
    }

    fn page_diag(&mut self, ui: &mut egui::Ui) {
        ui.horizontal(|ui| {
            ui.heading("诊断工具");
            if ui.button("▶ 运行自检").clicked() {
                self.run_diagnostics();
            }
        });
        ui.separator();
        ui.label("自检在后台线程执行（netstat / tasklist / adb 查询都是耗时操作），不会卡住界面。");
        ui.add_space(8.0);
        egui::ScrollArea::vertical().show(ui, |ui: &mut egui::Ui| {
            if self.diag.is_empty() {
                ui.label("尚未运行自检。点击上方「运行自检」按钮。");
            }
            for (n, ok, d) in &self.diag {
                ui.horizontal(|ui| {
                    ui.colored_label(
                        if *ok {
                            egui::Color32::from_rgb(62, 207, 142)
                        } else {
                            egui::Color32::from_rgb(242, 85, 90)
                        },
                        if *ok { "✓" } else { "✗" },
                    );
                    ui.label(n);
                    ui.monospace(d);
                });
            }
        });
        ui.add_space(12.0);
        ui.separator();
        ui.label("测试套件（需在命令行执行，脚本位于上级目录）：");
        ui.monospace("python consistency_test.py    # 第一批 50 条");
        ui.monospace("python consistency_test2.py   # 第二批 50 条");
        ui.monospace("python stress_test.py# 基础压测 12 项");
    }
}

/// 状态小卡片。
fn stat_card(ui: &mut egui::Ui, label: &str, value: &str, extra: &str) {
    egui::Frame::group(ui.style()).show(ui, |ui: &mut egui::Ui| {
        ui.label(label);
        let v = if value.len() > 16 {
            format!("{}…", &value[..16])
        } else {
            value.to_string()
        };
        ui.heading(v);
        if !extra.is_empty() {
            ui.small(extra);
        }
    });
}

/// 把日志记录格式化为 (时间, 文本)。
fn format_record(r: &log::Record) -> (String, String) {
    let s = r.ts_ms / 1000;
    let ms = r.ts_ms % 1000;
    let h = (s / 3600) % 24;
    let m = (s % 3600) / 60;
    let sec = s % 60;
    (
        format!("{:02}:{:02}:{:02}.{:03}", h, m, sec, ms),
        format!("[{}] {}", r.source, r.message),
    )
}

/// 深色主题（与已确认的 UI 原型一致）。
fn apply_theme(ctx: &egui::Context) {
    let mut style = (*ctx.style()).clone();
    style.visuals.dark_mode = true;
    style.visuals.panel_fill = egui::Color32::from_rgb(22, 25, 32);
    style.visuals.window_fill = egui::Color32::from_rgb(29, 33, 42);
    style.visuals.extreme_bg_color = egui::Color32::from_rgb(15, 17, 21);
    style.visuals.selection.bg_fill = egui::Color32::from_rgb(76, 141, 255);
    style.visuals.hyperlink_color = egui::Color32::from_rgb(126, 169, 255);
    style.spacing.item_spacing = egui::vec2(8.0, 6.0);
    style.text_styles.insert(
        egui::TextStyle::Monospace,
        egui::FontId::monospace(13.0),
    );
    ctx.set_style(style);
}