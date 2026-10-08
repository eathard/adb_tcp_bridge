//! 分级日志 + 环形缓冲。
//!
//! 两种消费方式同时满足：
//! - **命令行模式**：直接打印到 stdout，保持与 Python 版一致的行为；
//! - **GUI 模式**：写入环形缓冲，界面按需拉取快照并实时刷新。
//!
//! 转发线程**只投递日志、绝不直接打印**，这样日志量再大也不会阻塞数据转发
//! （GUI 性能纪律的第一条）。

use std::collections::VecDeque;
use std::io::Write;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Mutex, OnceLock};
use std::time::{SystemTime, UNIX_EPOCH};

/// 日志级别。
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
pub enum Level {
    Debug,
    Info,
    Warn,
    Error,
}

impl Level {
    fn tag(self) -> &'static str {
        match self {
            Level::Debug => "DEBUG",
            Level::Info => "INFO",
            Level::Warn => "WARN",
            Level::Error => "ERROR",
        }
    }
}

/// 一条日志记录。
#[derive(Debug, Clone)]
pub struct Record {
    /// 毫秒时间戳（自 Unix epoch）
    pub ts_ms: u64,
    pub level: Level,
    pub source: &'static str,
    pub message: String,
}

/// 全局日志器。默认容量 5000 行，超出后丢弃最旧的。
struct Logger {
    inner: Mutex<Inner>,
    capacity: usize,
    /// 是否同时打印到 stdout（命令行模式开，GUI 模式关）
    echo_stdout: AtomicBool,
    /// 当前是否记录 Debug 级别
    debug_enabled: AtomicBool,
}

struct Inner {
    buf: VecDeque<Record>,
    /// 累计丢弃的行数，用于在界面提示「日志已滚动」
    dropped: u64,
    total: AtomicU64,
}

static LOGGER: OnceLock<Logger> = OnceLock::new();

fn logger() -> &'static Logger {
    LOGGER.get_or_init(|| {
        Logger {
            inner: Mutex::new(Inner {
                buf: VecDeque::with_capacity(5000),
                dropped: 0,
                total: AtomicU64::new(0),
            }),
            capacity: 5000,
            echo_stdout: AtomicBool::new(true),
            debug_enabled: AtomicBool::new(false),
        }
    })
}

fn now_ms() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_millis() as u64)
        .unwrap_or(0)
}

/// 设置是否同时输出到 stdout。命令行模式 true，GUI 模式 false。
pub fn set_echo_stdout(on: bool) {
    logger().echo_stdout.store(on, Ordering::Relaxed);
}

/// 开关 Debug 级日志。对应命令行 `--debug-packets` 之外的普通调试开关。
pub fn set_debug(on: bool) {
    logger().debug_enabled.store(on, Ordering::Relaxed);
}

/// 读取当前 Debug 开关状态。
pub fn debug_enabled() -> bool {
    logger().debug_enabled.load(Ordering::Relaxed)
}

fn emit(level: Level, source: &'static str, msg: String) {
    let lg = logger();
    let rec = Record {
        ts_ms: now_ms(),
        level,
        source,
        message: msg.clone(),
    };

    let echo = lg.echo_stdout.load(Ordering::Relaxed);
    if echo {
        let ts = &format_hms(rec.ts_ms);
        // 用 stderr? 不，统一 stdout，保持与 Python 版一致
        let mut out = std::io::stdout().lock();
        let _ = writeln!(out, "[{}] {} {}", ts, level.tag(), msg);
        if level == Level::Error {
            let _ = out.flush();
        }
    }

    if let Ok(mut inner) = lg.inner.lock() {
        inner.total.fetch_add(1, Ordering::Relaxed);
        if inner.buf.len() >= lg.capacity {
            inner.buf.pop_front();
            inner.dropped += 1;
        }
        inner.buf.push_back(rec);
    }
}

fn format_hms(ts_ms: u64) -> String {
    let secs = ts_ms / 1000;
    let ms = ts_ms % 1000;
    let day_secs = secs % 86_400;
    let (h, m, s) = (day_secs / 3600, (day_secs % 3600) / 60, day_secs % 60);
    format!("{:02}:{:02}:{:02}.{:03}", h, m, s, ms)
}

/// 记录一条 Info 日志。
pub fn info(source: &'static str, msg: impl Into<String>) {
    emit(Level::Info, source, msg.into());
}

/// 记录一条 Warn 日志。
pub fn warn(source: &'static str, msg: impl Into<String>) {
    emit(Level::Warn, source, msg.into());
}

/// 记录一条 Error 日志。
pub fn error(source: &'static str, msg: impl Into<String>) {
    emit(Level::Error, source, msg.into());
}

/// 记录一条 Debug 日志（仅在 set_debug(true) 时保留）。
pub fn debug(source: &'static str, msg: impl Into<String>) {
    if debug_enabled() {
        emit(Level::Debug, source, msg.into());
    }
}

/// 拉取日志快照（最近 `max` 条），供 GUI 渲染。
pub fn snapshot(max: usize) -> Vec<Record> {
    match logger().inner.lock() {
        Ok(inner) => {
            let skip = inner.buf.len().saturating_sub(max);
            inner.buf.iter().skip(skip).cloned().collect()
        }
        Err(_) => Vec::new(),
    }
}

/// 返回 (当前行数, 累计丢弃行数)。
pub fn stats() -> (usize, u64) {
    match logger().inner.lock() {
        Ok(inner) => (inner.buf.len(), inner.dropped),
        Err(_) => (0, 0),
    }
}

/// 清空缓冲。
pub fn clear() {
    if let Ok(mut inner) = logger().inner.lock() {
        inner.buf.clear();
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    // 注意：这些测试共享同一个全局 LOGGER，
    // 因此只验证不依赖顺序的通用性质。

    #[test]
    fn records_are_retained_and_readable() {
        set_echo_stdout(false);
        info("test", "hello-记录");
        let snap = snapshot(100);
        assert!(
            snap.iter().any(|r| r.message == "hello-记录"),
            "刚写入的日志应能被快照读到"
        );
    }

    #[test]
    fn snapshot_respects_max_limit() {
        set_echo_stdout(false);
        for i in 0..50 {
            info("test", format!("bulk-{}", i));
        }
        assert!(snapshot(10).len() <= 10, "snapshot 必须遵守 max 上限");
    }

    #[test]
    fn level_ordering_places_error_highest() {
        assert!(Level::Error > Level::Warn);
        assert!(Level::Warn > Level::Info);
        assert!(Level::Info > Level::Debug);
    }

    #[test]
    fn debug_suppressed_when_disabled() {
        set_debug(false);
        let before = stats().0;
        debug("test", "should-not-appear");
        assert_eq!(stats().0, before, "Debug 关闭时不应写入记录");

        set_debug(true);
        debug("test", "should-appear");
        assert!(snapshot(20).iter().any(|r| r.message == "should-appear"));
        set_debug(false);
    }

    #[test]
    fn time_format_is_hms_with_millis() {
        assert_eq!(format_hms(0), "00:00:00.000");
        assert_eq!(format_hms(3_661_500), "01:01:01.500");
    }
}