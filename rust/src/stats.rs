//! 桥的运行时共享状态：**桥侧旁路上报，界面侧按节奏拉快照**。
//!
//! 为什么用进程级注册表而不是把状态塞进 `AppState`：
//! 桥与界面的生命周期不一致 —— 界面打开时桥可能还没启动，
//! 桥重启时界面还开着。用一个全局交接点最省心，也与 `log` 模块同一模式。
//!
//! # 性能纪律（这是「引入界面不能让转发变慢」的关键）
//!
//! 1. **转发热路径绝不获取全局锁**。流量统计用每会话独立的原子计数
//!    （`Relaxed` 序，纳秒级），不是往一个共享计数器上排队。
//! 2. **加锁只发生在低频点**：会话建立、会话结束、界面每 300ms 拉一次快照。
//! 3. **快照只拷贝值**（`SessionView`），UI 不持有任何桥内部结构，
//!    因此界面卡顿不会反压到转发线程。

use std::sync::atomic::{AtomicBool, AtomicU64, AtomicUsize, Ordering};
use std::sync::{Arc, Mutex, OnceLock};
use std::time::{Duration, Instant};

/// 设备信息（桥启动探测后写入）。
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct DeviceInfo {
    pub serial: String,
    pub product: String,
    pub features: String,
}

/// 一个客户端会话的共享计数。
///
/// 字段刻意做成原子量：转发线程只做 `fetch_add`，不碰互斥锁。
#[derive(Debug)]
pub struct SessionShared {
    pub peer: String,
    pub since: Instant,
    up: AtomicU64,
    down: AtomicU64,
    /// 该会话累计建立的 stream 条数（只增不减，用于展示负载特征）
    streams: AtomicUsize,
    active: AtomicBool,
}

impl SessionShared {
    /// 上行字节（客户端 → 设备）。转发线程直接调，不取锁。
    #[inline]
    pub fn add_up(&self, n: u64) {
        self.up.fetch_add(n, Ordering::Relaxed);
    }

    /// 下行字节（设备 → 客户端）。
    #[inline]
    pub fn add_down(&self, n: u64) {
        self.down.fetch_add(n, Ordering::Relaxed);
    }

    /// 记录一条新建立的 stream。
    #[inline]
    pub fn add_stream(&self) {
        self.streams.fetch_add(1, Ordering::Relaxed);
    }

    pub fn up(&self) -> u64 {
        self.up.load(Ordering::Relaxed)
    }
    pub fn down(&self) -> u64 {
        self.down.load(Ordering::Relaxed)
    }
    pub fn streams(&self) -> usize {
        self.streams.load(Ordering::Relaxed)
    }
    pub fn is_active(&self) -> bool {
        self.active.load(Ordering::Relaxed)
    }
    pub fn elapsed(&self) -> Duration {
        self.since.elapsed()
    }
}

/// 界面用的会话快照（纯值，不含任何桥内部引用）。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SessionView {
    pub peer: String,
    pub elapsed: Duration,
    pub up: u64,
    pub down: u64,
    pub streams: usize,
    pub active: bool,
}

/// 会话句柄：桥持有它来上报流量。
///
/// **刻意不实现 `Drop` 自动注销**：句柄会被 clone 给多个 stream 线程，
/// 若靠 Drop 结束会话，clone 出来的副本一析构就会把会话误标为已结束。
/// 因此结束时机由 `session::run` 显式调用 [`SessionHandle::end`] 决定。
#[derive(Clone)]
pub struct SessionHandle {
    shared: Arc<SessionShared>,
}

impl SessionHandle {
    /// 上行字节（客户端 → 设备）。
    #[inline]
    pub fn add_up(&self, n: u64) {
        self.shared.add_up(n);
    }

    /// 下行字节（设备 → 客户端）。
    #[inline]
    pub fn add_down(&self, n: u64) {
        self.shared.add_down(n);
    }

    /// 记录一条新建立的 stream。
    #[inline]
    pub fn stream_open(&self) {
        self.shared.add_stream();
    }

    /// 直接拿到共享对象，便于把计数搬进 stream 线程（避免持有句柄）。
    pub fn shared(&self) -> Arc<SessionShared> {
        self.shared.clone()
    }

    /// 标记会话已结束。重复调用无副作用。
    pub fn end(&self) {
        self.shared.active.store(false, Ordering::Relaxed);
    }
}

/// 已断开会话的保留上限（避免长时间运行后无限增长）。
const MAX_RETIRED: usize = 50;

#[derive(Default)]
struct Registry {
    device: DeviceInfo,
    /// 全部被追踪的会话：活跃的在前，已断开的在后
    sessions: Vec<Arc<SessionShared>>,
}

fn registry() -> &'static Mutex<Registry> {
    static R: OnceLock<Mutex<Registry>> = OnceLock::new();
    R.get_or_init(|| Mutex::new(Registry::default()))
}

/// 写入设备信息（桥探测完成后调用）。
pub fn set_device(info: DeviceInfo) {
    if let Ok(mut r) = registry().lock() {
        r.device = info;
    }
}

/// 读取设备信息。
pub fn device() -> DeviceInfo {
    registry()
        .lock()
        .map(|r| r.device.clone())
        .unwrap_or_default()
}

/// 清空会话记录（桥每次启动时调用，避免上一次运行的残留）。
pub fn reset_sessions() {
    if let Ok(mut r) = registry().lock() {
        r.sessions.clear();
    }
}

/// 开始一个会话。
pub fn session_begin(peer: &str) -> SessionHandle {
    let shared = Arc::new(SessionShared {
        peer: peer.to_string(),
        since: Instant::now(),
        up: AtomicU64::new(0),
        down: AtomicU64::new(0),
        streams: AtomicUsize::new(0),
        active: AtomicBool::new(true),
    });

    if let Ok(mut r) = registry().lock() {
        // 先清理超量的已断开会话，再追加新会话
        let retired = r.sessions.iter().filter(|s| !s.is_active()).count();
        if retired > MAX_RETIRED {
            let mut to_drop = retired - MAX_RETIRED;
            r.sessions.retain(|s| {
                if to_drop > 0 && !s.is_active() {
                    to_drop -= 1;
                    false
                } else {
                    true
                }
            });
        }
        r.sessions.push(shared.clone());
    }

    SessionHandle { shared }
}

/// 快照：活跃会话在前，已断开的在后。
pub fn snapshot() -> Vec<SessionView> {
    let Ok(r) = registry().lock() else {
        return Vec::new();
    };
    let mut views: Vec<SessionView> = r
        .sessions
        .iter()
        .map(|s| SessionView {
            peer: s.peer.clone(),
            elapsed: s.elapsed(),
            up: s.up(),
            down: s.down(),
            streams: s.streams(),
            active: s.is_active(),
        })
        .collect();
    // 活跃的排前面，便于界面直接截取「进行中」部分
    views.sort_by_key(|v| !v.active);
    views
}

/// 活跃会话数。
pub fn active_count() -> usize {
    registry()
        .lock()
        .map(|r| r.sessions.iter().filter(|s| s.is_active()).count())
        .unwrap_or(0)
}

/// 汇总上下行字节（含已断开的历史会话）。
pub fn totals() -> (u64, u64) {
    registry()
        .lock()
        .map(|r| {
            r.sessions.iter().fold((0u64, 0u64), |(u, d), s| {
                (u + s.up(), d + s.down())
            })
        })
        .unwrap_or((0, 0))
}

#[cfg(test)]
mod tests {
    use super::*;

    /// 测试之间共享全局注册表，必须串行执行 —— 用一把测试专用锁。
    fn serial() -> std::sync::MutexGuard<'static, ()> {
        static L: OnceLock<Mutex<()>> = OnceLock::new();
        L.get_or_init(|| Mutex::new(()))
            .lock()
            .unwrap_or_else(|e| e.into_inner())
    }

    #[test]
    fn device_info_roundtrip() {
        let _g = serial();
        let d = DeviceInfo {
            serial: "abc123".into(),
            product: "luckfox".into(),
            features: "stat_v2,cmd,shell_v2".into(),
        };
        set_device(d.clone());
        assert_eq!(device(), d);
    }

    #[test]
    fn session_registers_and_counts_traffic() {
        let _g = serial();
        reset_sessions();
        let h = session_begin("1.2.3.4");
        h.add_up(100);
        h.add_up(50);
        h.add_down(2048);
        h.stream_open();
        h.stream_open();

        assert_eq!(active_count(), 1, "应有一个活跃会话");
        let snap = snapshot();
        assert_eq!(snap.len(), 1);
        assert_eq!(snap[0].peer, "1.2.3.4");
        assert_eq!(snap[0].up, 150);
        assert_eq!(snap[0].down, 2048);
        assert_eq!(snap[0].streams, 2);
        assert!(snap[0].active);
        h.end();
    }

    #[test]
    fn end_marks_inactive_and_drops_from_active_count() {
        let _g = serial();
        reset_sessions();
        let h = session_begin("9.9.9.9");
        assert_eq!(active_count(), 1);
        h.end();
        assert_eq!(active_count(), 0, "结束后不应计入活跃");
        // 但仍保留在快照里，供界面查看历史
        let snap = snapshot();
        assert!(snap.iter().any(|v| v.peer == "9.9.9.9" && !v.active));
    }

    #[test]
    fn handle_clone_does_not_end_session_on_drop() {
        let _g = serial();
        reset_sessions();
        let h = session_begin("5.5.5.5");
        {
            // 模拟把句柄分给多条 stream 线程，随后副本析构
            let h2 = h.clone();
            let h3 = h.clone();
            drop(h2);
            drop(h3);
        }
        assert_eq!(active_count(), 1, "副本析构不应结束会话（这是 clone 语义的关键）");
        h.end();
    }

    #[test]
    fn totals_include_retired_sessions() {
        let _g = serial();
        reset_sessions();
        let a = session_begin("a");
        a.add_up(10);
        a.add_down(20);
        a.end();
        let b = session_begin("b");
        b.add_up(5);
        b.add_down(7);
        assert_eq!(totals(), (15, 27), "历史会话的流量也要累加");
        b.end();
    }

    #[test]
    fn snapshot_puts_active_first() {
        let _g = serial();
        reset_sessions();
        let old = session_begin("old");
        old.end();
        let _live = session_begin("live");
        let snap = snapshot();
        assert_eq!(snap[0].peer, "live", "活跃会话应排在前面");
        assert!(snap[0].active);
    }

    #[test]
    fn reset_clears_sessions_but_keeps_device() {
        let _g = serial();
        set_device(DeviceInfo {
            serial: "keepme".into(),
            ..Default::default()
        });
        let _h = session_begin("x");
        reset_sessions();
        assert_eq!(active_count(), 0);
        assert!(snapshot().is_empty());
        assert_eq!(device().serial, "keepme", "reset 不应清掉设备信息");
    }

    #[test]
    fn retired_sessions_are_capped() {
        let _g = serial();
        reset_sessions();
        for i in 0..(MAX_RETIRED + 20) {
            let h = session_begin(&format!("peer{}", i));
            h.end();
        }
        let snap = snapshot();
        assert!(
            snap.len() <= MAX_RETIRED + 1,
            "已断开会话必须被限制，实际 {}",
            snap.len()
        );
    }
}
