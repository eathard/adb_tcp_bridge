//! 单个客户端会话：CNXN 握手 + stream 多路复用 + 数据转发。
//!
//! 与 Python 版 `Session` 一一对应。
//!
//! 握手：客户端先发 CNXN，我们回应 CNXN（banner 必须带 `ro.product.*` 且
//! 末尾不能有 NUL，否则客户端不协商 `shell_v2`，会退化成老 shell + PTY，
//! 后果是 stderr 与 stdout 合并、退出码丢失、TTY 检测产生 ANSI 颜色）。
//!
//! 之后每个 OPEN 都独立建立一条到 adb server 的连接，互不干扰。

use std::io::{Read, Write};
use std::net::TcpStream;
use std::sync::atomic::{AtomicBool, AtomicU32, AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant};

use crate::log;
use crate::proto::{self, cmd};
use crate::server;
use crate::stats;
use crate::stream::{self, Flow, StreamStats};

/// 会话共享状态。
pub struct Shared {
    pub serial: String,
    pub server_addr: String,
    pub features: String,
    pub product: String,
    pub closed: Arc<AtomicBool>,
    pub next_id: AtomicU32,
    pub sessions: Arc<Mutex<Vec<SessionInfo>>>,
}

#[derive(Debug, Clone)]
pub struct SessionInfo {
    pub peer: String,
    pub since_ms: u64,
    pub streams: usize,
    pub up: u64,
    pub down: u64,
}

/// 一条活跃 stream。
struct ActiveStream {
    local_id: u32,
    our_id: u32,
    service: String,
    /// 下行（设备→客户端）流控状态：已发给客户端但未收到 OKAY 的字节。
    /// 收到客户端 OKAY 时在此扣减并唤醒等待的读线程。
    flow: Arc<(Mutex<stream::Flow>, std::sync::Condvar)>,
    /// 上行写队列
    queue: Arc<(Mutex<stream::WriterState>, std::sync::Condvar)>,
    up: Arc<AtomicU64>,
    down: Arc<AtomicU64>,
    closed: Arc<AtomicBool>,
}

/// 读取一个完整的 ADB 包（24 字节头 + payload）。
pub fn read_packet(sock: &mut TcpStream) -> std::io::Result<Option<(proto::Header, Vec<u8>)>> {
    let mut hdr = [0u8; 24];
    // 先读满 24 字节头部；对端关闭时返回 Ok(None)
    let mut got = 0;
    while got < 24 {
        match sock.read(&mut hdr[got..]) {
            Ok(0) => {
                if got == 0 {
                    return Ok(None); // 干净的 EOF
                }
                return Err(std::io::Error::new(
                    std::io::ErrorKind::UnexpectedEof,
                    "包头未读满即断开",
                ));
            }
            Ok(n) => got += n,
            Err(e) => return Err(e),
        }
    }
    let header = match proto::Header::from_bytes(&hdr) {
        Some(h) => h,
        None => {
            return Err(std::io::Error::new(
                std::io::ErrorKind::InvalidData,
                "包头解析失败",
            ))
        }
    };
    // magic 校验：头部最后 4 字节应是 command ^ 0xffffffff
    let magic = u32::from_le_bytes([hdr[20], hdr[21], hdr[22], hdr[23]]);
    if !proto::verify_magic(header.command, magic) {
        return Err(std::io::Error::new(
            std::io::ErrorKind::InvalidData,
            "包头 magic 校验失败（包损坏或不同步）",
        ));
    }
    let mut payload = vec![0u8; header.data_length as usize];
    if !payload.is_empty() {
        sock.read_exact(&mut payload)?;
    }
    Ok(Some((header, payload)))
}

/// 会话主循环。
pub fn run(shared: Arc<Shared>, mut client: TcpStream) {
    let peer = client
        .peer_addr()
        .map(|a| a.ip().to_string())
        .unwrap_or_else(|_| "?".into());
    client.set_nodelay(true).ok();
    // 客户端空闲超时：防止「连上却不发数据」的半开连接永久占用线程
    client
        .set_read_timeout(Some(Duration::from_secs(proto::IDLE_TIMEOUT)))
        .ok();

    log::info("session", format!("{} 已接入 -> {}", peer, shared.serial));

    // ---- 握手：读首个包，必须是 CNXN ----
    let first = match read_packet(&mut client) {
        Ok(Some(p)) => p,
        Ok(None) => return,
        Err(e) => {
            log::warn("session", format!("{} 握手失败: {}", peer, e));
            return;
        }
    };
    if first.0.command != cmd::CNXN {
        log::warn("session", format!("{} 首个包不是 CNXN，放弃", peer));
        return;
    }

    let mut active: std::collections::HashMap<u32, ActiveStream> =
        std::collections::HashMap::new();
    let send_lock = Arc::new(Mutex::new(()));

    // 回应 CNXN：banner 必须带 ro.product.* 且末尾无 NUL
    let product = if shared.product.is_empty() {
        "unknown".to_string()
    } else {
        shared.product.clone()
    };
    let banner = proto::build_banner(&product, &shared.features);
    let peer_maxdata = if first.0.arg1 == 0 {
        proto::MAXDATA
    } else {
        first.0.arg1
    };
    let maxdata = proto::negotiate_maxdata(peer_maxdata);
    if !send_packet(&client, &send_lock, cmd::CNXN, proto::VERSION, maxdata, &banner) {
        return;
    }

    // 注册到界面可读的运行时统计。放在握手成功之后 ——
    // 这样早期 return（CNXN 不合法等）不会在界面上留下幽灵会话。
    // 注意：这是**旁路上报**，不参与任何转发逻辑，失败也不影响收发。
    let sess = stats::session_begin(&peer);

    let mut total_up: u64 = 0;
    let mut total_down: u64 = 0;
    let started = Instant::now();

    // ---- 主循环 ----
    loop {
        if shared.closed.load(Ordering::Relaxed) {
            break;
        }
        let (header, payload) = match read_packet(&mut client) {
            Ok(Some(p)) => p,
            Ok(None) => break,
            Err(e) => {
                log::debug("session", format!("{} 读包结束: {}", peer, e));
                break;
            }
        };

        match header.command {
            cmd::OPEN => {
                let service = String::from_utf8_lossy(&payload).to_string();
                handle_open(
                    &shared,
                    &client,
                    &send_lock,
                    &mut active,
                    &sess,
                    header.arg0,
                    &service,
                    &peer,
                );
            }
            cmd::WRTE => {
                let local_id = header.arg0;
                if let Some(st) = active.get(&local_id) {
                    // 上行：只入队，由写线程真正发送（避免主线程阻塞）
                    let n = payload.len();
                    let _ = stream::enqueue(&st.queue, payload);
                    st.up.fetch_add(n as u64, Ordering::Relaxed);
                    total_up += n as u64;
                    sess.add_up(n as u64);
                }
            }
            cmd::OKAY => {
                // ⚠️ arg0/arg1 两个字段都要试：分别是双方的 local id。
                // 只匹配一个会在拥塞时匹配不到而误判，大文件 pull 会卡死。
                //
                // 扣减的是**下行**流控状态：设备→客户端的数据发出后，
                // 客户端每确认一块就回一个 OKAY，必须据此释放窗口，
                // 否则读线程会一直卡在窗口上限（256KB）不再前进。
                let local_id = if active.contains_key(&header.arg1) {
                    header.arg1
                } else {
                    header.arg0
                };
                if let Some(st) = active.get(&local_id) {
                    let (lock, cv) = &*st.flow;
                    if let Ok(mut g) = lock.lock() {
                        stream::on_okay(&mut g);
                        cv.notify_all();
                    }
                }
            }
            cmd::CLSE => {
                if let Some(st) = active.remove(&header.arg0) {
                    st.closed.store(true, Ordering::Relaxed);
                    stream::enqueue_close(&st.queue);
                    log::info(
                        "session",
                        format!(
                            "{} stream#{} 关闭 [{}] 上行={}B 下行={}B",
                            peer, st.local_id, st.service,
                            st.up.load(Ordering::Relaxed),
                            st.down.load(Ordering::Relaxed)
                        ),
                    );
                }
            }
            cmd::CNXN => continue, // 忽略重复的 CNXN
            other => {
                log::warn("session", format!("{} 未知包 cmd={:#x}", peer, other));
            }
        }
    }

    // ---- 收尾：拆掉所有 stream ----
    for (_, st) in active.drain() {
        st.closed.store(true, Ordering::Relaxed);
        stream::enqueue_close(&st.queue);
    }
    // 标记会话结束，界面据此把它从「进行中」移到「已断开」
    sess.end();

    let secs = started.elapsed().as_secs();
    log::info(
        "session",
        format!(
            "{} 断开（{}秒，上行={}B 下行={}B）",
            peer,
            secs,
            total_up,
            total_down
        ),
    );
}

/// 处理一个 OPEN：为该 stream 建立独立的上游连接并启动读写线程。
fn handle_open(
    shared: &Arc<Shared>,
    client: &TcpStream,
    send_lock: &Arc<Mutex<()>>,
    active: &mut std::collections::HashMap<u32, ActiveStream>,
    sess: &stats::SessionHandle,
    local_id: u32,
    service: &str,
    peer: &str,
) {
    // 上游连接失败时先确认 adb server 是否活着，必要时拉起
    if !server::server_alive(&shared.server_addr) {
        let _ = server::ensure_server(&shared.server_addr);
    }

    let up = match server::open_stream(&shared.server_addr, &shared.serial, service) {
        Ok(s) => s,
        Err(e) => {
            log::warn("session", format!("{} 建立 stream 失败: {}", peer, e));
            send_packet(client, send_lock, cmd::CLSE, 0, local_id, b"");
            return;
        }
    };

    let our_id = shared.next_id.fetch_add(1, Ordering::Relaxed);
    let closed = Arc::new(AtomicBool::new(false));
    let queue = stream::new_shared();
    // 下行流控：设备→客户端，客户端的 OKAY 在主循环里扣减
    let flow = Arc::new((Mutex::new(stream::Flow::new()), std::sync::Condvar::new()));
    let up_bytes = Arc::new(AtomicU64::new(0));
    let down_bytes = Arc::new(AtomicU64::new(0));

    // ---- 上行写线程：队列 → adb server ----
    // 每写入一块就回客户端一个 OKAY（ADB 按包确认，少回会让对端卡住）
    match (up.try_clone(), client.try_clone()) {
        (Ok(mut upw), Ok(cl2)) => {
            let q = queue.clone();
            let cl = closed.clone();
            let sl = send_lock.clone();
            let ub = up_bytes.clone();
            std::thread::spawn(move || {
                stream::send_batched_with(&mut upw, &q, &cl, |_nblocks, sent| {
                    // sent = 本批实际写入 socket 的字节数
                    ub.fetch_add(sent as u64, Ordering::Relaxed);
                    // 每块回一个 OKAY（ADB 按包确认）
                    if send_packet(&cl2, &sl, cmd::OKAY, our_id, local_id, b"") {
                        Ok(())
                    } else {
                        Err(())
                    }
                });
            });
        }
        _ => {
            log::warn("session", format!("stream#{} 无法克隆 socket", local_id));
            return;
        }
    }

    // ---- 下行读线程：adb server → 客户端，受 256KB 窗口约束 ----
    match client.try_clone() {
        Ok(cl2) => {
            let sl = send_lock.clone();
            let cl = closed.clone();
            let fl = flow.clone();
            let db = down_bytes.clone();
            let ub2 = up_bytes.clone();
            let our = our_id;
            let q = queue.clone();
            // 会话级下行计数（界面用），与 stream 级 db 各记各的
            let sh = sess.shared();
            std::thread::spawn(move || {
                let mut upr = up;
                let mut buf = vec![0u8; stream::RECV_BUF];
                loop {
                    if cl.load(Ordering::Relaxed) {
                        break;
                    }
                    match upr.read(&mut buf) {
                        Ok(0) => {
                            log::debug(
                                "session",
                                format!("stream#{} 上游 EOF：adb server 关闭了这条流", local_id),
                            );
                            break;
                        }
                        Ok(n) => {
                            // 窗口检查：未确认字节超过 256KB 时等客户端 OKAY
                            // （最多等 3 秒，与 Python 版一致）
                            stream::wait_while_stalled(&fl, Duration::from_secs(3));
                            if cl.load(Ordering::Relaxed) {
                                break;
                            }
                            // 记账为待确认
                            if let Ok(mut g) = fl.0.lock() {
                                stream::on_block(&mut g, n);
                            }
                            if !send_packet(
                                &cl2,
                                &sl,
                                cmd::WRTE,
                                our,
                                local_id,
                                &buf[..n],
                            ) {
                                break;
                            }
                            db.fetch_add(n as u64, Ordering::Relaxed);
                            sh.add_down(n as u64);
                            let _ = &ub2;
                        }
                        Err(e) => {
                            log::debug("session", format!("stream#{} 读异常: {}", local_id, e));
                            break;
                        }
                    }
                }
                // ⚠️ 必须通知客户端本 stream 已结束：回一个 CLSE。
                // 少了这一步，客户端会一直等不到结束信号，
                // 直到 adb 客户端自身的超时（表现为每条命令都要卡 ~90 秒）。
                cl.store(true, Ordering::Relaxed);
                stream::enqueue_close(&q);
                send_packet(&cl2, &sl, cmd::CLSE, our, local_id, b"");
            });
        }
        Err(_) => {
            log::warn("session", format!("stream#{} 无法克隆客户端 socket", local_id));
            return;
        }
    }

    // 放入活跃表（必须在两个线程启动之后，避免主循环提前处理 OKAY）
    active.insert(
        local_id,
        ActiveStream {
            local_id,
            our_id,
            service: service.to_string(),
            flow,
            queue,
            up: up_bytes,
            down: down_bytes,
            closed,
        },
    );

    // 最后才应答 OKAY，表示 stream 建立成功
    sess.stream_open();
    send_packet(client, send_lock, cmd::OKAY, our_id, local_id, b"");
    log::info(
        "session",
        format!("{} stream#{} -> {}", peer, local_id, truncate(service, 40)),
    );
}

/// 截断字符串用于日志。
fn truncate(s: &str, n: usize) -> String {
    s.chars().take(n).collect()
}

/// 向客户端发送一个包（加锁保证多线程下包不交错）。
fn send_packet(
    sock: &TcpStream,
    lock: &Arc<Mutex<()>>,
    command: u32,
    arg0: u32,
    arg1: u32,
    payload: &[u8],
) -> bool {
    let data = proto::pack(command, arg0, arg1, payload);
    let mut w = match sock.try_clone() {
        Ok(s) => s,
        Err(_) => return false,
    };
    let _g = lock.lock();
    w.write_all(&data).is_ok() && w.flush().is_ok()
}

/// 收集当前所有会话的统计（供 GUI 显示）。
pub fn collect_stats(shared: &Arc<Shared>) -> Vec<SessionInfo> {
    shared
        .sessions
        .lock()
        .map(|s| s.clone())
        .unwrap_or_default()
}

/// 会话统计条目的构造辅助（供 GUI/测试使用）。
pub fn new_session_info(peer: &str, since_ms: u64) -> SessionInfo {
    SessionInfo {
        peer: peer.to_string(),
        since_ms,
        streams: 0,
        up: 0,
        down: 0,
    }
}

/// StreamStats 的格式化（供 GUI 显示）。
pub fn format_stream_stat(s: &StreamStats) -> String {
    format!(
        "#{} {} ↑{} ↓{}",
        s.local_id, s.service, s.up_bytes, s.down_bytes
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Write as _;

    /// 起一个临时监听，写入 `payload`，然后让客户端用 read_packet 读回。
    /// 返回 read_packet 的结果。
    fn roundtrip(payload: Vec<u8>) -> std::io::Result<Option<(proto::Header, Vec<u8>)>> {
        let l = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let addr = l.local_addr().unwrap();
        let h = thread::spawn(move || {
            let (mut c, _) = l.accept().unwrap();
            c.write_all(&payload).unwrap();
            c.flush().unwrap();
            thread::sleep(Duration::from_millis(300));
        });
        let mut cl = TcpStream::connect(addr).unwrap();
        cl.set_read_timeout(Some(Duration::from_secs(2))).unwrap();
        let r = read_packet(&mut cl);
        let _ = h.join();
        r
    }

    #[test]
    fn read_packet_parses_valid_cnxn() {
        let pkt = proto::pack(cmd::CNXN, proto::VERSION, proto::MAXDATA, b"banner");
        let got = roundtrip(pkt).unwrap().expect("应读到包");
        assert_eq!(got.0.command, cmd::CNXN);
        assert_eq!(got.0.data_length, 6);
        assert_eq!(got.1, b"banner".to_vec());
    }

    #[test]
    fn read_packet_parses_wrtc_with_payload() {
        let pkt = proto::pack(cmd::WRTE, 5, 1, &vec![0xABu8; 1000]);
        let got = roundtrip(pkt).unwrap().unwrap();
        assert_eq!(got.0.command, cmd::WRTE);
        assert_eq!(got.0.arg0, 5);
        assert_eq!(got.1.len(), 1000);
        assert!(got.1.iter().all(|b| *b == 0xAB));
    }

    #[test]
    fn read_packet_rejects_corrupted_magic() {
        let mut pkt = proto::pack(cmd::OKAY, 1, 2, b"");
        pkt[20] ^= 0xff; // 破坏 magic
        assert!(roundtrip(pkt).is_err(), "magic 错误必须被拒绝");
    }

    #[test]
    fn read_packet_returns_none_on_clean_eof() {
        let l = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let addr = l.local_addr().unwrap();
        let h = thread::spawn(move || {
            let (_c, _) = l.accept().unwrap(); // 立即关闭
        });
        let mut cl = TcpStream::connect(addr).unwrap();
        cl.set_read_timeout(Some(Duration::from_secs(2))).unwrap();
        let r = read_packet(&mut cl);
        assert!(matches!(r, Ok(None)), "对端立即关闭时应返回 Ok(None)");
        let _ = h.join();
    }

    #[test]
    fn read_packet_errors_on_truncated_header() {
        // 只发 10 字节（头部需 24 字节）后关闭 → UnexpectedEof
        assert!(roundtrip(vec![0u8; 10]).is_err());
    }

    #[test]
    fn session_info_defaults() {
        let si = new_session_info("1.2.3.4", 1000);
        assert_eq!(si.peer, "1.2.3.4");
        assert_eq!(si.since_ms, 1000);
        assert_eq!(si.streams, 0);
    }

    #[test]
    fn format_stream_stat_contains_ids_and_bytes() {
        let s = StreamStats {
            local_id: 3,
            service: "shell:ls".into(),
            up_bytes: 100,
            down_bytes: 2000,
            active: true,
        };
        let out = format_stream_stat(&s);
        assert!(out.contains("#3"));
        assert!(out.contains("shell:ls"));
        assert!(out.contains("2000"), "应包含下行字节数: {}", out);
    }
}
