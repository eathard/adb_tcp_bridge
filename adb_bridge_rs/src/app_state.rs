//! GUI 共享状态。
//!
//! # 性能纪律（三条硬性红线）
//!
//! 1. **转发线程内绝不触碰任何 GUI 对象**，日志一律投递到环形缓冲。
//! 2. **所有耗时操作 offload 到 worker 线程** —— 端口检查要跑
//!    `netstat`/`tasklist`，设备探测要跑 `adb`，放主线程会阻塞 UI 事件循环。
//! 3. **UI 刷新要稀**：连接数/流量 200~500ms 一次；日志用环形缓冲只留最近 N 行。
//!
//! 这三条是桥在带界面的情况下性能不受影响的前提。

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use crate::log::{self, Record};

/// 桥的运行状态。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum BridgeState {
    /// 未启动
    Stopped,
    /// 正在启动
    Starting,
    /// 运行中
    Running,
    /// 启动失败
    Failed,
}

impl BridgeState {
    pub fn label(self) -> &'static str {
        match self {
            BridgeState::Stopped => "已停止",
            BridgeState::Starting => "启动中",
            BridgeState::Running => "运行中",
            BridgeState::Failed => "启动失败",
        }
    }
}

/// 一条活跃 stream 的显示信息。
#[derive(Debug, Clone)]
pub struct StreamRow {
    pub local_id: u32,
    pub service: String,
    pub up: u64,
    pub down: u64,
}

/// 一个客户端会话的显示行（由 [`AppState::refresh_from_stats`] 填充）。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SessionRow {
    pub peer: String,
    /// 接入时长（桥侧记录，已是 Duration，界面无需再算）
    pub elapsed: Duration,
    pub up: u64,
    pub down: u64,
    /// 该会话累计建立的 stream 条数
    pub streams: usize,
    /// 是否仍在进行中
    pub active: bool,
}

/// 全局应用状态，由 UI 线程独占访问，桥线程只读取其中不变的字段。
pub struct AppState {
    pub bridge_state: BridgeState,
    pub listen_addr: String,
    pub listen_port: u16,
    pub serial: String,
    pub product: String,
    pub features: String,
    pub local_ip: String,
    pub server_port: u16,
    pub no_kill_port: bool,
    pub debug_packets: bool,

    /// 是否自动刷新（关掉后可暂停观察）
    pub auto_refresh: bool,
    /// 是否自动滚动日志到最底部（关掉后可停下来翻历史日志）
    pub auto_scroll: bool,
    /// 当前选中的页面
    pub page: usize,

    /// 会话列表快照。**UI 线程独占** —— 数据由 [`Self::refresh_from_stats`]
    /// 从全局统计注册表拷贝进来，桥线程不会直接碰它。
    pub sessions: Vec<SessionRow>,
    /// 桥线程退出信号
    pub shutdown: Arc<AtomicBool>,
    /// 上次刷新时间
    last_refresh: Instant,
}

impl Default for AppState {
    fn default() -> Self {
        AppState {
            bridge_state: BridgeState::Stopped,
            listen_addr: "0.0.0.0".into(),
            listen_port: 15555,
            serial: String::new(),
            product: String::new(),
            features: String::new(),
            local_ip: "127.0.0.1".into(),
            server_port: 5037,
            no_kill_port: false,
            debug_packets: false,
            auto_refresh: true,
            auto_scroll: true,
            page: 0,
            sessions: Vec::new(),
            shutdown: Arc::new(AtomicBool::new(false)),
            last_refresh: Instant::now(),
        }
    }
}

/// UI 刷新间隔。200~500ms 足够，人眼看不出差别，
/// 而每次刷新要读取日志缓冲与会话列表。
pub const REFRESH_INTERVAL: Duration = Duration::from_millis(300);

impl AppState {
    /// 连接到电脑 B 的命令，供界面一键复制。
    pub fn connect_command(&self) -> String {
        let host = if self.listen_addr == "0.0.0.0" || self.listen_addr == "::" {
            self.local_ip.clone()
        } else {
            self.listen_addr.clone()
        };
        format!("adb connect {}:{}", host, self.listen_port)
    }

    /// 是否到了该刷新的时候。
    pub fn should_refresh(&self) -> bool {
        self.auto_refresh && self.last_refresh.elapsed() >= REFRESH_INTERVAL
    }

    /// 标记已刷新。
    pub fn mark_refreshed(&mut self) {
        self.last_refresh = Instant::now();
    }

    /// 端口是否落在 adb server 的模拟器扫描区间（会产生幽灵设备）。
    pub fn port_is_dangerous(&self) -> bool {
        crate::is_emulator_port(self.listen_port)
    }

    /// 端口建议。
    pub fn suggested_port(&self) -> u16 {
        crate::recommended_port()
    }

    /// 供 GUI 显示的日志快照。
    pub fn log_snapshot(&self, max: usize) -> Vec<Record> {
        log::snapshot(max)
    }

    /// 会话数量（只数仍在进行中的）。
    pub fn session_count(&self) -> usize {
        self.sessions.iter().filter(|s| s.active).count()
    }

    /// 汇总上行/下行字节（含已断开的历史会话）。
    pub fn total_traffic(&self) -> (u64, u64) {
        self.sessions
            .iter()
            .fold((0u64, 0u64), |(u, d), s| (u + s.up, d + s.down))
    }

    /// 从全局统计注册表拉一次快照。
    ///
    /// 这是桥与界面之间**唯一**的数据交接点，由 UI 线程按
    /// [`REFRESH_INTERVAL`] 的节奏调用。桥侧只写原子量，因此这里
    /// 持锁时间极短，不会影响转发性能。
    pub fn refresh_from_stats(&mut self) {
        let dev = crate::stats::device();
        self.serial = dev.serial;
        self.product = dev.product;
        self.features = dev.features;

        self.sessions = crate::stats::snapshot()
            .into_iter()
            .map(|v| SessionRow {
                peer: v.peer,
                elapsed: v.elapsed,
                up: v.up,
                down: v.down,
                streams: v.streams,
                active: v.active,
            })
            .collect();
    }
}

/// 把字节数格式化成人类可读形式。
pub fn format_bytes(n: u64) -> String {
    const UNITS: [&str; 5] = ["B", "KB", "MB", "GB", "TB"];
    let mut v = n as f64;
    let mut i = 0;
    while v >= 1024.0 && i < UNITS.len() - 1 {
        v /= 1024.0;
        i += 1;
    }
    if i == 0 {
        format!("{} {}", n, UNITS[0])
    } else {
        format!("{:.1} {}", v, UNITS[i])
    }
}

/// 把时长格式化为 HH:MM:SS。
pub fn format_duration(d: Duration) -> String {
    let s = d.as_secs();
    format!(
        "{:02}:{:02}:{:02}",
        s / 3600,
        (s % 3600) / 60,
        s % 60
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn connect_command_uses_local_ip_when_wildcard() {
        let mut st = AppState::default();
        st.listen_addr = "0.0.0.0".into();
        st.listen_port = 15555;
        st.local_ip = "172.16.0.106".into();
        assert_eq!(st.connect_command(), "adb connect 172.16.0.106:15555");
    }

    #[test]
    fn connect_command_uses_literal_addr_when_specific() {
        let mut st = AppState::default();
        st.listen_addr = "127.0.0.1".into();
        st.listen_port = 15000;
        st.local_ip = "172.16.0.106".into();
        assert_eq!(st.connect_command(), "adb connect 127.0.0.1:15000");
    }

    #[test]
    fn default_port_is_safe() {
        let st = AppState::default();
        assert!(!st.port_is_dangerous(), "默认端口应避开模拟器区间");
        assert_eq!(st.listen_port, 15555);
    }

    #[test]
    fn dangerous_port_detected() {
        let mut st = AppState::default();
        st.listen_port = 5555;
        assert!(st.port_is_dangerous());
        assert_eq!(st.suggested_port(), 15555);
    }

    #[test]
    fn bridge_state_labels() {
        assert_eq!(BridgeState::Running.label(), "运行中");
        assert_eq!(BridgeState::Stopped.label(), "已停止");
        assert_eq!(BridgeState::Failed.label(), "启动失败");
    }

    #[test]
    fn format_bytes_human_readable() {
        assert_eq!(format_bytes(0), "0 B");
        assert_eq!(format_bytes(512), "512 B");
        assert_eq!(format_bytes(1024), "1.0 KB");
        assert_eq!(format_bytes(1536), "1.5 KB");
        assert_eq!(format_bytes(1024 * 1024), "1.0 MB");
        assert_eq!(format_bytes(1024 * 1024 * 1024), "1.0 GB");
        assert_eq!(format_bytes(3 * 1024 * 1024 * 1024), "3.0 GB");
    }

    #[test]
    fn format_duration_hms() {
        assert_eq!(format_duration(Duration::from_secs(0)), "00:00:00");
        assert_eq!(format_duration(Duration::from_secs(61)), "00:01:01");
        assert_eq!(
            format_duration(Duration::from_secs(2 * 3600 + 14 * 60 + 37)),
            "02:14:37"
        );
    }

    #[test]
    fn refresh_interval_is_reasonable() {
        // 200~500ms：太短浪费 CPU，太长界面显得迟钝
        assert!(REFRESH_INTERVAL >= Duration::from_millis(200));
        assert!(REFRESH_INTERVAL <= Duration::from_millis(500));
    }

    #[test]
    fn refresh_gating_respects_interval() {
        let mut st = AppState::default();
        st.auto_refresh = true;
        // 刚创建时不应立即刷新
        assert!(!st.should_refresh());
        st.mark_refreshed();
        assert!(!st.should_refresh());
        // 关闭自动刷新后永不刷新
        st.auto_refresh = false;
        assert!(!st.should_refresh());
    }

    #[test]
    fn traffic_aggregates_active_and_history() {
        let mut st = AppState::default();
        st.sessions = vec![
            SessionRow {
                peer: "1.1.1.1".into(),
                elapsed: Duration::from_secs(3),
                up: 100,
                down: 2000,
                streams: 2,
                active: true,
            },
            SessionRow {
                peer: "2.2.2.2".into(),
                elapsed: Duration::from_secs(9),
                up: 50,
                down: 500,
                streams: 1,
                active: false,
            },
        ];
        assert_eq!(st.session_count(), 1, "连接数只统计进行中的会话");
        assert_eq!(st.total_traffic(), (150, 2500), "流量要含已断开的历史会话");
    }

    #[test]
    fn refresh_pulls_device_info_and_sessions() {
        // 统计数据来自全局注册表，测试间需串行
        static L: std::sync::OnceLock<std::sync::Mutex<()>> = std::sync::OnceLock::new();
        let _g = L
            .get_or_init(|| std::sync::Mutex::new(()))
            .lock()
            .unwrap_or_else(|e| e.into_inner());

        crate::stats::set_device(crate::stats::DeviceInfo {
            serial: "b57290249a9b3206".into(),
            product: "luckfox".into(),
            features: "stat_v2,cmd,shell_v2".into(),
        });
        crate::stats::reset_sessions();
        let h = crate::stats::session_begin("172.16.0.101");
        h.add_up(64);
        h.add_down(128);

        let mut st = AppState::default();
        st.refresh_from_stats();

        assert_eq!(st.serial, "b57290249a9b3206", "设备 serial 必须同步到界面");
        assert_eq!(st.product, "luckfox");
        assert_eq!(st.features, "stat_v2,cmd,shell_v2");
        assert_eq!(st.session_count(), 1);
        assert_eq!(st.total_traffic(), (64, 128));
        assert_eq!(st.sessions[0].peer, "172.16.0.101");

        h.end();
        st.refresh_from_stats();
        assert_eq!(st.session_count(), 0, "断开后连接数应归零");
        assert!(st.total_traffic().0 > 0, "历史流量仍应保留");
    }

    #[test]
    fn no_sessions_gives_zero_traffic() {
        let st = AppState::default();
        assert_eq!(st.session_count(), 0);
        assert_eq!(st.total_traffic(), (0, 0));
    }
}