//! 到本机 adb server 的 smart socket 客户端。
//!
//! adb server（默认 127.0.0.1:5037）的协议不是 ADB 包格式，而是：
//! 先 4 字节十六进制长度，再跟 ASCII 命令。响应同样是长度前缀，
//! 或以 `OKAY` / `FAIL` 开头。
//!
//! 典型流程（对应 Python 版 `Session.on_open`）：
//! 1. 连上 server
//! 2. 发 `host:transport:<serial>`，读 4 字节，应为 `OKAY`
//! 3. 发服务名（如 `shell:ls`、`sync:`），读 4 字节
//!    - `OKAY` → 这条 stream 可用
//!    - `FAIL` → 后续跟 4 字节长度 + 错误文本，stream 被拒

use std::io::{self, Read, Write};
use std::net::TcpStream;
use std::time::Duration;

use crate::proto;

/// 与 adb server 通信的错误类型。
#[derive(Debug)]
pub enum ServerError {
    Io(io::Error),
    /// server 拒绝了请求，后附错误文本
    Rejected(String),
    /// 响应格式不符合预期
    Protocol(String),
}

impl std::fmt::Display for ServerError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            ServerError::Io(e) => write!(f, "IO 错误: {}", e),
            ServerError::Rejected(m) => write!(f, "请求被拒: {}", m),
            ServerError::Protocol(m) => write!(f, "协议错误: {}", m),
        }
    }
}

impl From<io::Error> for ServerError {
    fn from(e: io::Error) -> Self {
        ServerError::Io(e)
    }
}

pub type Result<T> = std::result::Result<T, ServerError>;

/// 建连超时。这是**建连阶段**的超时，建好后必须清除（见 `open_stream`）。
const CONNECT_TIMEOUT: Duration = Duration::from_secs(10);

/// 单次响应读取超时。
const READ_TIMEOUT: Duration = Duration::from_secs(15);

/// 精确读取 n 字节，短读或对端关闭都算失败。
pub fn read_exact(r: &mut impl Read, n: usize) -> Result<Vec<u8>> {
    let mut buf = vec![0u8; n];
    r.read_exact(&mut buf)?;
    Ok(buf)
}

/// 读取一条长度前缀的响应（4 字节十六进制长度 + 负载）。
pub fn read_sized(r: &mut impl Read) -> Result<Vec<u8>> {
    let len_buf = read_exact(r, 4)?;
    let n = proto::parse_hex_len(&len_buf)
        .ok_or_else(|| ServerError::Protocol("响应长度前缀非法".into()))?;
    read_exact(r, n)
}

/// 确认 adb server 是否可达。
pub fn server_alive(addr: &str) -> bool {
    TcpStream::connect_timeout(
        &match addr.parse() {
            Ok(a) => a,
            Err(_) => return false,
        },
        Duration::from_secs(2),
    )
    .is_ok()
}

/// 尝试拉起 adb server。返回是否成功。
///
/// 部分环境会回收 adb server（如用户退出登录），桥需要能自愈。
pub fn ensure_server(addr: &str) -> bool {
    if server_alive(addr) {
        return true;
    }
    let _ = std::process::Command::new("adb")
        .arg("start-server")
        .stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::null())
        .spawn();
    for _ in 0..10 {
        std::thread::sleep(Duration::from_millis(500));
        if server_alive(addr) {
            return true;
        }
    }
    false
}

/// 向 server 发送一条长度前缀命令。
pub fn send_cmd(s: &mut impl Write, service: &str) -> Result<()> {
    s.write_all(&proto::encode_service_cmd(service))?;
    s.flush()?;
    Ok(())
}

/// 查询设备属性（get-product / get-serialno 等）。
pub fn query_prop(addr: &str, serial: &str, what: &str) -> String {
    let sock = addr.parse().ok();
    let mut s = match sock.and_then(|a| TcpStream::connect_timeout(&a, Duration::from_secs(5)).ok()) {
        Some(s) => s,
        None => return String::new(),
    };
    let _ = s.set_read_timeout(Some(READ_TIMEOUT));
    let cmd = format!("host-serial:{}:{}", serial, what);
    if send_cmd(&mut s, &cmd).is_err() {
        return String::new();
    }
    match read_exact(&mut s, 4) {
        Ok(v) if v == b"OKAY" => match read_sized(&mut s) {
            Ok(t) => String::from_utf8_lossy(&t).to_string(),
            Err(_) => String::new(),
        },
        _ => String::new(),
    }
}

/// 查询设备 features（协商 shell_v2 用）。
pub fn query_features(addr: &str, serial: &str) -> String {
    query_prop(addr, serial, "features")
}

/// 建立一条到设备服务的 stream，返回已就绪的 socket。
///
/// ⚠️ **最容易踩的坑就在这里**：
///
/// Python 版写的是
/// ```python
/// up = socket.create_connection(self.server_addr, timeout=10)
/// # ... 曾在此处误以为「需要心跳」，加了心跳反而破坏流控
/// ```
/// `socket.create_connection(..., timeout=10)` 会把超时**保留到 socket 上**，
/// 于是之后所有 `recv` 在 10 秒无输出时抛 `socket.timeout`，
/// 被 `except Exception: pass` 静默吞掉后拆掉连接。
///
/// 表现就是「设备端执行超过约 10 秒无输出的命令（`sleep 12`、扫大文件）
/// 就会断」，曾被误判为「adb server 对 TCP transport 的硬性超时」。
///
/// 真因是超时残留。Rust 里对应 `set_read_timeout(None)`——
/// **建连成功后立即恢复为阻塞模式**，之后不再有任何读超时。
///
/// 静默超时是这个 bug 最阴险的地方：日志里什么都不报错，连接悄悄消失。
/// 历史上为它加的心跳保活是错的——额外的 OKAY/空 WRTE 会破坏 ADB 的
/// 流控与消息边界，连原本正常的「每 6 秒输出一行、共 18 秒」命令也会被断。
pub fn open_stream(addr: &str, serial: &str, service: &str) -> Result<TcpStream> {
    let a = addr
        .parse()
        .map_err(|_| ServerError::Protocol(format!("无法解析 server 地址: {}", addr)))?;

    // 建连阶段允许超时
    let mut s = TcpStream::connect_timeout(&a, CONNECT_TIMEOUT)?;

    // 步骤 1：切到目标设备
    send_cmd(&mut s, &proto::transport_cmd(serial))?;
    let resp = read_exact(&mut s, 4)?;
    if &resp != b"OKAY" {
        return Err(ServerError::Rejected(format!(
            "无法切换到设备 {}: {:?}",
            serial,
            String::from_utf8_lossy(&resp)
        )));
    }

    // 步骤 2：请求具体服务
    send_cmd(&mut s, service)?;
    let resp = read_exact(&mut s, 4)?;
    if &resp != b"OKAY" {
        // FAIL 的格式是：FAIL + 4字节长度 + 错误文本
        let detail = match read_sized(&mut s) {
            Ok(t) => String::from_utf8_lossy(&t).to_string(),
            Err(_) => String::from_utf8_lossy(&resp).to_string(),
        };
        return Err(ServerError::Rejected(detail));
    }

    // ⚠️ 关键：把建连时的超时彻底清掉，改为阻塞模式。
    // 少这一行，长时间无输出的命令（sleep、大文件扫描）会被静默断连。
    s.set_read_timeout(None).ok();
    s.set_write_timeout(None).ok();
    s.set_nodelay(true).ok();

    Ok(s)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn read_exact_fails_on_short_stream() {
        // 只有 3 字节，请求 4 字节 → 应报错而非静默返回
        let mut d: &[u8] = &[1, 2, 3];
        assert!(read_exact(&mut d, 4).is_err());
    }

    #[test]
    fn read_exact_returns_requested_bytes() {
        let mut d: &[u8] = &[9, 8, 7, 6];
        assert_eq!(read_exact(&mut d, 4).unwrap(), vec![9, 8, 7, 6]);
    }

    #[test]
    fn read_sized_parses_hex_length_prefix() {
        // 注意：长度前缀是十六进制 ASCII（"0003"），不是二进制字节
        let mut d: &[u8] = b"0003abc";
        assert_eq!(read_sized(&mut d).unwrap(), b"abc".to_vec());
    }

    #[test]
    fn read_sized_handles_empty_payload() {
        let mut d: &[u8] = b"0000";
        assert_eq!(read_sized(&mut d).unwrap(), Vec::<u8>::new());
    }

    #[test]
    fn read_sized_rejects_non_hex_prefix() {
        let mut d: &[u8] = b"zzzzrest";
        assert!(matches!(read_sized(&mut d), Err(ServerError::Protocol(_))));
    }

    #[test]
    fn server_alive_rejects_bad_address() {
        assert!(!server_alive("not-an-addr"));
    }

    #[test]
    fn open_stream_on_dead_port_returns_io_error() {
        // 端口 1 几乎不可能有服务在监听
        let r = open_stream("127.0.0.1:1", "nosuchserial", "shell:id");
        assert!(matches!(r, Err(ServerError::Io(_))));
    }

    #[test]
    fn error_messages_are_human_readable() {
        let e = ServerError::Rejected("device offline".into());
        assert!(format!("{}", e).contains("device offline"));
        let e = ServerError::Protocol("bad length".into());
        assert!(format!("{}", e).contains("协议错误"));
    }
}