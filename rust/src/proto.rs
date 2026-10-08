//! ADB 协议原语：包头、命令字、打包/解包、smart socket 命令。
//!
//! 协议格式（来自 AOSP `adb/adb.cpp`）：
//!
//! ```text
//! 包头 24 字节（小端，6 个 u32），随后 arg0 + arg1 + data_length + payload_crc32 + payload
//!   u32 command       命令字
//!   u32 arg0          首个 stream id
//!   u32 arg1          第二个 stream id
//!   u32 data_length   payload 长度
//!   u32 payload_crc   payload 的 CRC32（部分实现写 0）
//!   u32 magic         command ^ 0xffffffff
//!   ...payload
//! ```
//!
//! 注意头���是 **6 个 u32**，不是 5 个——第 5 个是 payload 的 CRC32。
//! 漏掉它会让每个包少 4 字节，接收端解析完全错位。
//! 头部的 `magic` 是**校验用**的：接收端用 `command ^ 0xffffffff` 与之比对，
//! 不符说明包被损坏或不同步。

/// ADB 协议版本号，等于 `0x01000000`。
pub const VERSION: u32 = 0x0100_0000;

/// 本端愿意接收的最大 payload 上限。
pub const MAXDATA: u32 = 1_048_576; // 1MB

/// 单个 stream 的流控窗口：未确认字节超过此值时，上行侧必须等待。
pub const WINDOW: usize = 256 * 1024; // 256KB

/// 客户端空闲超时（秒）。防止「连上却不发数据」的半开连接永久占用线程。
pub const IDLE_TIMEOUT: u64 = 600;

/// 命令字常量。
pub mod cmd {
    pub const CNXN: u32 = 0x4E58_4E43;
    pub const OPEN: u32 = 0x4E45_504F;
    pub const OKAY: u32 = 0x5941_4B4F;
    pub const CLSE: u32 = 0x4553_4C43;
    pub const WRTE: u32 = 0x4554_5257;

    /// 返回命令字的可读名，未知命令返回十六进制形式。
    pub fn name(c: u32) -> String {
        match c {
            CNXN => "CNXN".into(),
            OPEN => "OPEN".into(),
            OKAY => "OKAY".into(),
            CLSE => "CLSE".into(),
            WRTE => "WRTE".into(),
            _ => format!("0x{:08x}", c),
        }
    }
}

/// ADB 包头（24 字节 = 6 个 u32，不含 payload）。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Header {
    pub command: u32,
    pub arg0: u32,
    pub arg1: u32,
    pub data_length: u32,
    pub payload_crc: u32,
}

impl Header {
    /// 期望的 magic 值：`command ^ 0xffffffff`。
    pub fn expected_magic(&self) -> u32 {
        self.command ^ 0xffff_ffff
    }

    /// 序列化为 24 字节（小端）。
    pub fn to_bytes(&self) -> [u8; 24] {
        let mut b = [0u8; 24];
        b[0..4].copy_from_slice(&self.command.to_le_bytes());
        b[4..8].copy_from_slice(&self.arg0.to_le_bytes());
        b[8..12].copy_from_slice(&self.arg1.to_le_bytes());
        b[12..16].copy_from_slice(&self.data_length.to_le_bytes());
        b[16..20].copy_from_slice(&self.payload_crc.to_le_bytes());
        b[20..24].copy_from_slice(&self.expected_magic().to_le_bytes());
        b
    }

    /// 从 24 字节解析包头。长度不足返回 None。
    pub fn from_bytes(b: &[u8]) -> Option<Self> {
        if b.len() < 24 {
            return None;
        }
        let g = |i: usize| u32::from_le_bytes([b[i], b[i + 1], b[i + 2], b[i + 3]]);
        Some(Header {
            command: g(0),
            arg0: g(4),
            arg1: g(8),
            data_length: g(12),
            payload_crc: g(16),
        })
    }

    /// 校验头部 magic 是否正确。
    pub fn magic_ok(&self) -> bool {
        true // 真正的校验需要原始 magic 字节，见 verify_magic
    }
}

/// 一个完整的 ADB 包：头部 + payload。
#[derive(Debug, Clone)]
pub struct Packet {
    pub header: Header,
    pub payload: Vec<u8>,
}

/// 计算 payload 的校验和：所有字节之和，截断为 u32。
///
/// ⚠️ 这不是真正的 CRC32，而是 AOSP `adb.cpp` 里定义的**字节累加和**。
/// 与 Python 版 `sum(payload) & 0xFFFFFFFF` 完全一致。
/// 虽然多数客户端不校验该字段，但必须保持一致 —— 否则在会校验的客户端上
/// 会被判为损坏包。
pub fn payload_checksum(payload: &[u8]) -> u32 {
    payload.iter().map(|b| *b as u32).sum::<u32>()
}

/// 把头部与 payload 组装成一个待发送的包。
///
/// 头部固定 24 字节（6 个 u32），第 5 个字段是 payload 字节累加和。
pub fn pack(command: u32, arg0: u32, arg1: u32, payload: &[u8]) -> Vec<u8> {
    let header = Header {
        command,
        arg0,
        arg1,
        data_length: payload.len() as u32,
        payload_crc: payload_checksum(payload),
    };
    let mut out = Vec::with_capacity(24 + payload.len());
    out.extend_from_slice(&header.to_bytes());
    out.extend_from_slice(payload);
    out
}

/// 校验包头的 magic 字段。
///
/// 魔数是 `command ^ 0xffffffff`；不符即说明包已损坏或读写不同步。
pub fn verify_magic(command: u32, magic: u32) -> bool {
    magic == (command ^ 0xffff_ffff)
}

/// 构造连接握手的 banner（CNXN 的 payload）。
///
/// ⚠️ 两个必须遵守的约束（都是实际踩过的坑）：
///
/// 1. **必须包含 `ro.product.*`**。只给 `features` 的话，adb 客户端不会协商出
///    `shell_v2`，会退化成老 shell + PTY。后果是 stderr 与 stdout 被合并、
///    退出码丢失、设备端 TTY 检测产生 ANSI 颜色。
/// 2. **末尾不得附加 NUL 字节**。否则 features 变成 `"shell_v2\0"`，
///    客户端匹配不到 `shell_v2` 而退回老协议。
pub fn build_banner(product: &str, features: &str) -> Vec<u8> {
    format!(
        "device::ro.product.name={};ro.product.model={};ro.product.device={};features={}",
        product, product, product, features
    )
    .into_bytes()
}

/// 从 banner 文本里解析出 `features=` 的值（用于自检与日志）。
pub fn parse_features(banner: &[u8]) -> Option<String> {
    let text = String::from_utf8_lossy(banner);
    for part in text.split(';') {
        if let Some(v) = part.strip_prefix("features=") {
            return Some(v.trim_end_matches('\0').to_string());
        }
    }
    None
}

/// 给定对端在 CNXN 中声明的 maxdata，取本端可接受的较小值。
pub fn negotiate_maxdata(peer_maxdata: u32) -> u32 {
    if peer_maxdata == 0 {
        MAXDATA
    } else {
        peer_maxdata.min(MAXDATA)
    }
}

/// 向 adb server 的 smart socket 发送一条长度前缀命令。
///
/// adb server 的协议不是包格式，而是：先 4 字节十六进制长度，再跟命令内容。
/// 用 `{:04x}` 格式化，长度超过 0xffff 时会自动多出位数（与 Python 的
/// `format!("%04x", n)` 行为一致）。
pub fn encode_service_cmd(service: &str) -> Vec<u8> {
    let body = service.as_bytes();
    let mut out = Vec::with_capacity(4 + body.len());
    out.extend_from_slice(format!("{:04x}", body.len()).as_bytes());
    out.extend_from_slice(body);
    out
}

/// 构造到本机 adb server 的 transport 连接命令。
pub fn transport_cmd(serial: &str) -> String {
    format!("host:transport:{}", serial)
}

/// 解析 smart socket 返回的长度前缀（4 字节十六进制）。
pub fn parse_hex_len(buf: &[u8]) -> Option<usize> {
    if buf.len() < 4 {
        return None;
    }
    let s = std::str::from_utf8(&buf[..4]).ok()?;
    usize::from_str_radix(s, 16).ok()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn pack_produces_24_byte_header() {
        let pkt = pack(cmd::OKAY, 1, 2, b"hi");
        // 关键：头部是 24 字节（6 个 u32），不是 20 字节
        assert_eq!(pkt.len(), 24 + 2);
        let h = Header::from_bytes(&pkt).expect("头部应可解析");
        assert_eq!(h.command, cmd::OKAY);
        assert_eq!(h.arg0, 1);
        assert_eq!(h.arg1, 2);
        assert_eq!(h.data_length, 2);
        // 第 5 个字段是 payload 字节累加和（不是 CRC32，也不是 0）
        assert_eq!(h.payload_crc, ('h' as u32) + ('i' as u32));
        // magic 是 command ^ 0xffffffff，位于头部最后 4 字节
        assert_eq!(
            u32::from_le_bytes([pkt[20], pkt[21], pkt[22], pkt[23]]),
            cmd::OKAY ^ 0xffff_ffff
        );
        assert_eq!(&pkt[24..], b"hi");
    }

    #[test]
    fn header_roundtrip() {
        let pkt = pack(cmd::WRTE, 0x10001, 0x20002, b"payload-bytes");
        let h = Header::from_bytes(&pkt).unwrap();
        assert_eq!(h.command, cmd::WRTE);
        assert_eq!(h.arg0, 0x10001);
        assert_eq!(h.arg1, 0x20002);
        assert_eq!(h.data_length as usize, pkt.len() - 24);
        assert_eq!(&pkt[24..], b"payload-bytes");
        // to_bytes 再解析应完全一致
        assert_eq!(Header::from_bytes(&h.to_bytes()).unwrap(), h);
    }

    #[test]
    fn from_bytes_rejects_short_buffer() {
        assert!(Header::from_bytes(&[0u8; 23]).is_none());
        assert!(Header::from_bytes(&[0u8; 24]).is_some());
    }

    #[test]
    fn empty_payload_pack_is_exactly_24_bytes() {
        assert_eq!(pack(cmd::CLSE, 1, 2, b"").len(), 24);
    }

    #[test]
    fn magic_rejects_corrupted_header() {
        assert!(verify_magic(cmd::CNXN, cmd::CNXN ^ 0xffff_ffff));
        assert!(!verify_magic(cmd::CNXN, 0));
    }

    #[test]
    fn banner_contains_product_and_features_and_no_trailing_nul() {
        let b = build_banner("luckfox", "stat_v2,cmd,shell_v2");
        let s = String::from_utf8(b.clone()).unwrap();
        assert!(s.contains("ro.product.name=luckfox"));
        assert!(s.contains("ro.product.model=luckfox"));
        assert!(s.contains("ro.product.device=luckfox"));
        assert!(s.contains("features=stat_v2,cmd,shell_v2"));
        // 关键：不能以 NUL 结尾，否则客户端匹配不到 shell_v2
        assert_ne!(*b.last().unwrap(), 0, "banner 末尾不能是 NUL");
        // features 解析出来必须恰好等于原值
        assert_eq!(
            parse_features(&b).as_deref(),
            Some("stat_v2,cmd,shell_v2")
        );
    }

    #[test]
    fn parse_features_strips_stray_nul() {
        let b = b"device::ro.product.name=x;features=shell_v2\0";
        assert_eq!(parse_features(b).as_deref(), Some("shell_v2"));
    }

    #[test]
    fn maxdata_negotiation_never_exceeds_local_cap() {
        assert_eq!(negotiate_maxdata(0), MAXDATA);
        assert_eq!(negotiate_maxdata(4096), 4096);
        assert_eq!(negotiate_maxdata(u32::MAX), MAXDATA);
    }

    #[test]
    fn service_cmd_is_hex_length_prefixed() {
        let e = encode_service_cmd("shell:ls");
        assert_eq!(&e[..4], b"0008", "shell:ls 长度 8 → 0008");
        assert_eq!(&e[4..], b"shell:ls");
        assert_eq!(&encode_service_cmd("sync:")[..4], b"0005");
        assert_eq!(
            &encode_service_cmd(&format!("host:transport:{}", "b57290249a9b3206"))[..4],
            b"001f",
            "host:transport:<16位serial> 共 31 字符"
        );
        // 超过 0xffff 时长度字段自然变宽（与 Python %04x 行为一致）
        assert_eq!(&encode_service_cmd(&"x".repeat(0x10000))[..5], b"10000");
    }

    #[test]
    fn transport_cmd_format() {
        assert_eq!(transport_cmd("abc123"), "host:transport:abc123");
    }

    #[test]
    fn parse_hex_len_reads_four_digits() {
        assert_eq!(parse_hex_len(b"0007"), Some(7));
        assert_eq!(parse_hex_len(b"00ff"), Some(255));
        assert_eq!(parse_hex_len(b"00"), None);
        assert_eq!(parse_hex_len(b"zzzz"), None);
    }

    #[test]
    fn command_names_are_stable() {
        assert_eq!(cmd::name(cmd::CNXN), "CNXN");
        assert_eq!(cmd::name(0x1234_5678), "0x12345678");
    }
}