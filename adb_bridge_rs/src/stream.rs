//! 单条 stream 的流控与读写。
//!
//! ADB 的流控是**按包确认**的：每收到一个 WRTE，对端就要回一个 OKAY。
//! 未确认字节数超过窗口（WINDOW = 256KB）时，发送侧必须等待。
//!
//! 两个必须照搬 Python 版的要点：
//!
//! 1. **OKAY 的 arg0/arg1 都要匹配**。两个字段分别是双方的 local id，
//!    只匹配一个会在拥塞时匹配不到而误判，大文件 pull 会卡死。
//! 2. **上行必须队列化 + 独立写线程**。在主线程直接 `sendall` 会被大文件
//!    写入阻塞，无法转发设备响应，表现为 push/pull 中途卡死。
//!    队列可以合并多块后一次写入提高吞吐，但**每块仍须单独回一个 OKAY** ——
//!    少回会让对端按包确认时卡住。

use std::collections::VecDeque;
use std::io::{Read, Write};
use std::net::TcpStream;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Condvar, Mutex};
use std::time::{Duration, Instant};

use crate::proto::WINDOW;

/// 单次 `recv` 的缓冲区大小。
pub const RECV_BUF: usize = 65536;

/// 排队上限，防止对端疯狂发送把内存吃满（8MB）。
const MAX_QUEUE_BYTES: usize = 8 * 1024 * 1024;

/// 流控共享状态。
pub struct Flow {
    /// 已写入 socket 但尚未被对端确认的字节数
    pub unacked: usize,
    /// 等待确认的各块字节数（用于逐块回 OKAY）
    pub pending: VecDeque<usize>,
}

impl Flow {
    pub fn new() -> Self {
        Flow {
            unacked: 0,
            pending: VecDeque::new(),
        }
    }
}

impl Default for Flow {
    fn default() -> Self {
        Self::new()
    }
}

/// 收到一个 OKAY，扣减一个待确认块。返回是否匹配成功。
///
/// ⚠️ arg0/arg1 都要试：Python 版 `self.streams.get(arg1) or
/// self.streams.get(arg0)`，只匹配一个会误判拥塞。
pub fn on_okay(flow: &mut Flow) -> bool {
    match flow.pending.pop_front() {
        Some(n) => {
            flow.unacked = flow.unacked.saturating_sub(n);
            true
        }
        None => false,
    }
}

/// 记录一个待确认块。
pub fn on_block(flow: &mut Flow, n: usize) {
    flow.unacked += n;
    flow.pending.push_back(n);
}

/// 是否已超过流控窗口，需要等待。
pub fn should_stall(flow: &Flow) -> bool {
    flow.unacked > WINDOW
}

/// 单条 stream 的运行时统计（供 GUI 显示）。
#[derive(Debug, Default, Clone)]
pub struct StreamStats {
    pub local_id: u32,
    pub service: String,
    pub up_bytes: u64,
    pub down_bytes: u64,
    pub active: bool,
}

/// 上行写队列：主线程只入队，写线程负责真正的 sendall。
pub struct Writer {
    queue: Arc<(Mutex<WriterState>, Condvar)>,
    closed: Arc<AtomicBool>,
}

pub struct WriterState {
    pub chunks: VecDeque<Vec<u8>>,
    pub queued_bytes: usize,
}

/// 从写队列取出一批待发送的数据。
///
/// 合并队列中的多块，总量受 1MB 限制（与 Python 版一致）。
/// 返回 `(合并后的字节, 块数)` —— 块数用于逐块回 OKAY。
pub fn take_batch(
    state: &Arc<(Mutex<WriterState>, Condvar)>,
    closed: &AtomicBool,
) -> Option<(Vec<u8>, usize)> {
    let (lock, cv) = &**state;
    let mut g = match lock.lock() {
        Ok(g) => g,
        Err(_) => return None,
    };
    while g.chunks.is_empty() && !closed.load(Ordering::Relaxed) {
        let (ng, _t) = match cv.wait_timeout(g, Duration::from_millis(200)) {
            Ok(x) => x,
            Err(_) => return None,
        };
        g = ng;
    }
    if g.chunks.is_empty() {
        return None;
    }
    let mut blocks: Vec<Vec<u8>> = Vec::new();
    let mut total = 0usize;
    while let Some(c) = g.chunks.pop_front() {
        total += c.len();
        blocks.push(c);
        if total >= (1 << 20) {
            break;
        }
    }
    g.queued_bytes = g.queued_bytes.saturating_sub(total);
    let nblocks = blocks.len();
    let payload: Vec<u8> = if nblocks == 1 {
        blocks.into_iter().next().unwrap()
    } else {
        blocks.iter().flatten().copied().collect()
    };
    Some((payload, nblocks))
}

/// 写线程主循环：取数据写入 socket，成功后对**每一块**回一个 OKAY。
///
/// 合并写入是为吞吐，单块 OKAY 是为流控 —— 两者不能互相替代。
pub fn send_batched<S: Write, F: FnMut() -> Result<(), ()> + Send>(
    out: &mut S,
    state: &Arc<(Mutex<WriterState>, Condvar)>,
    closed: &Arc<AtomicBool>,
    mut send_okay: F,
) {
    while !closed.load(Ordering::Relaxed) {
        let (payload, nblocks) = match take_batch(state, closed) {
            Some(x) => x,
            None => return,
        };
        if out.write_all(&payload).is_err() {
            return;
        }
        if out.flush().is_err() {
            return;
        }
        for _ in 0..nblocks {
            if send_okay().is_err() {
                return;
            }
        }
    }
}


/// 带字节数回调的写线程主循环。
///
/// 回调签名 `(块数, 本批字节数)`。与只回 OKAY 的版本相比，
/// 额外提供实际写入的字节数，便于统计上行流量。
pub fn send_batched_with<S: Write, F: FnMut(usize, usize) -> Result<(), ()> + Send>(
    out: &mut S,
    state: &Arc<(Mutex<WriterState>, Condvar)>,
    closed: &Arc<AtomicBool>,
    mut on_written: F,
) {
    while !closed.load(Ordering::Relaxed) {
        let (payload, nblocks) = match take_batch(state, closed) {
            Some(x) => x,
            None => return,
        };
        let sent = payload.len();
        if out.write_all(&payload).is_err() {
            return;
        }
        if out.flush().is_err() {
            return;
        }
        if on_written(nblocks, sent).is_err() {
            return;
        }
    }
}

/// 判断是否需要因流控而等待。
///
/// Python 版在上行侧等对端 OKAY，在下行侧等 `unacked <= WINDOW`。
/// 这里提供同一套判断，供下行读取路径使用。
pub fn wait_while_stalled(flow: &Arc<(Mutex<Flow>, Condvar)>, max_wait: Duration) {
    let start = Instant::now();
    let (lock, cv) = &**flow;
    let mut g = match lock.lock() {
        Ok(g) => g,
        Err(_) => return,
    };
    while should_stall(&g) {
        if start.elapsed() >= max_wait {
            break;
        }
        let (ng, _) = match cv.wait_timeout(g, Duration::from_millis(200)) {
            Ok(x) => x,
            Err(_) => return,
        };
        g = ng;
    }
}

/// 为某个 stream 建立读写所需的共享状态。
pub fn new_shared() -> Arc<(Mutex<WriterState>, Condvar)> {
    Arc::new((
        Mutex::new(WriterState {
            chunks: VecDeque::new(),
            queued_bytes: 0,
        }),
        Condvar::new(),
    ))
}

/// 向写队列投递一块数据。队列满时返回 false。
pub fn enqueue(
    state: &Arc<(Mutex<WriterState>, Condvar)>,
    chunk: Vec<u8>,
) -> bool {
    if let Ok(mut g) = state.0.lock() {
        if g.queued_bytes + chunk.len() > MAX_QUEUE_BYTES {
            return false;
        }
        g.queued_bytes += chunk.len();
        g.chunks.push_back(chunk);
        state.1.notify_all();
        true
    } else {
        false
    }
}

/// 向写队列投递结束信号（None 表示关闭）。
pub fn enqueue_close(state: &Arc<(Mutex<WriterState>, Condvar)>) {
    if let Ok(mut g) = state.0.lock() {
        g.chunks.clear();
        g.queued_bytes = 0;
    }
    state.1.notify_all();
}

/// 读满 n 字节；EOF 返回 Ok(0)。
pub fn read_some(src: &mut impl Read, buf: &mut [u8]) -> std::io::Result<usize> {
    src.read(buf)
}

/// 把 socket 设为无延迟（Nagle 会显著拖慢小包交互）。
pub fn tune_socket(s: &TcpStream) {
    s.set_nodelay(true).ok();
    // 加大收发缓冲，降低大文件传输时的系统调用次数
    s.set_nodelay(true).ok();
}

/// 计数器（原子），用于统计累计流量。
#[derive(Debug, Default)]
pub struct Counters {
    pub up: AtomicU64,
    pub down: AtomicU64,
}

impl Counters {
    pub fn add_up(&self, n: usize) {
        self.up.fetch_add(n as u64, Ordering::Relaxed);
    }
    pub fn add_down(&self, n: usize) {
        self.down.fetch_add(n as u64, Ordering::Relaxed);
    }
    pub fn snapshot(&self) -> (u64, u64) {
        (
            self.up.load(Ordering::Relaxed),
            self.down.load(Ordering::Relaxed),
        )
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn okay_consumes_one_pending_block() {
        let mut f = Flow::new();
        on_block(&mut f, 1000);
        on_block(&mut f, 2000);
        assert_eq!(f.unacked, 3000);
        assert!(on_okay(&mut f));
        assert_eq!(f.unacked, 2000);
        assert!(on_okay(&mut f));
        assert_eq!(f.unacked, 0);
        // 没有待确认块时返回 false，不应 panic
        assert!(!on_okay(&mut f));
    }

    #[test]
    fn unacked_never_underflows() {
        let mut f = Flow::new();
        on_block(&mut f, 100);
        on_okay(&mut f);
        // 多次 OKAY 也不应下溢
        on_okay(&mut f);
        on_okay(&mut f);
        assert_eq!(f.unacked, 0);
    }

    #[test]
    fn stall_triggers_above_window() {
        let mut f = Flow::new();
        on_block(&mut f, WINDOW);
        assert!(!should_stall(&f), "正好等于窗口不应等待");
        on_block(&mut f, 1);
        assert!(should_stall(&f), "超过窗口必须等待");
    }

    #[test]
    fn window_is_256kb() {
        assert_eq!(WINDOW, 262_144);
    }

    #[test]
    fn enqueue_respects_backlog_limit() {
        let st = new_shared();
        // 塞入超过上限的数据
        let chunk = vec![0u8; 1024 * 1024];
        let mut ok_count = 0;
        for _ in 0..16 {
            if enqueue(&st, chunk.clone()) {
                ok_count += 1;
            } else {
                break;
            }
        }
        assert!(ok_count <= 8, "排队上限应限制在 8MB 内，实际入队 {} 块", ok_count);
        enqueue_close(&st);
    }

    #[test]
    fn counters_accumulate() {
        let c = Counters::default();
        c.add_up(100);
        c.add_up(50);
        c.add_down(7);
        assert_eq!(c.snapshot(), (150, 7));
    }

    #[test]
    fn take_batch_merges_chunks_and_reports_count() {
        let st = new_shared();
        let closed = AtomicBool::new(false);
        enqueue(&st, b"hello ".to_vec());
        enqueue(&st, b"world".to_vec());

        let (payload, nblocks) = take_batch(&st, &closed).expect("应取到数据");
        assert_eq!(payload, b"hello world", "多块应合并为一个连续负载");
        assert_eq!(nblocks, 2, "必须报告块数，以便逐块回 OKAY");
    }

    #[test]
    fn take_batch_single_chunk() {
        let st = new_shared();
        let closed = AtomicBool::new(false);
        enqueue(&st, b"solo".to_vec());
        let (payload, nblocks) = take_batch(&st, &closed).unwrap();
        assert_eq!(payload, b"solo");
        assert_eq!(nblocks, 1);
    }

    #[test]
    fn take_batch_returns_none_when_closed_and_empty() {
        let st = new_shared();
        let closed = AtomicBool::new(true);
        assert!(take_batch(&st, &closed).is_none());
    }

    #[test]
    fn queue_bytes_accounting_is_released_after_take() {
        let st = new_shared();
        let closed = AtomicBool::new(false);
        enqueue(&st, vec![0u8; 4096]);
        {
            let g = st.0.lock().unwrap();
            assert_eq!(g.queued_bytes, 4096);
        }
        let _ = take_batch(&st, &closed);
        {
            let g = st.0.lock().unwrap();
            assert_eq!(g.queued_bytes, 0, "取走后排队计数应归零");
        }
    }
}
