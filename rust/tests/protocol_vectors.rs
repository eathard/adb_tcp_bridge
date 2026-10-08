//! 协议字节与 Python 参考实现的**逐字节交叉校验**。
//!
//! 重构过程中真实翻车过两次，都靠这类外部基准才抓得住：
//!
//! 1. `pack()` 漏写包头第 5 个字段（payload_crc32），每包少 4 字节；
//! 2. 交叉校验脚本本身把命令字误写成协议版本号（0x01000000），
//!    基准全错却「看起来通过」。
//!
//! 单靠 Rust 侧自测发现不了「少一个字段」或「基准本身错了」，
//! 因此这里用 `gen_python_vectors.py` 从 `adb_tcp_bridge.py` 直接导出的
//! 固定向量做断言。
//!
//! 生成方式：python gen_python_vectors.py
//! 输出：tests/python_vectors.json
//!
//! 注意命令字按**小端**存储，例如 CNXN = 0x4E584E43 存为 `434e584e`。

use adb_bridge_rs::proto::{cmd, pack, payload_checksum, Header, MAXDATA, VERSION, WINDOW};

/// 命令字真值（来自 AOSP adb.cpp）。
const T_CNXN: u32 = 0x4E58_4E43;
const T_OPEN: u32 = 0x4E45_504F;
const T_WRTE: u32 = 0x4554_5257;
const T_OKAY: u32 = 0x5941_4B4F;
const T_CLSE: u32 = 0x4553_4C43;

/// (命令字, arg0, arg1, payload 构造, 期望包长, 期望 head24)
type Vector = (u32, u32, u32, Vec<u8>, usize, &'static str);

fn vectors() -> Vec<Vector> {
    let mut v: Vec<Vector> = Vec::new();
    v.push((
        T_CNXN,
        VERSION,
        MAXDATA,
        Vec::new(),
        24,
        "434e584e00000001000010000000000000000000bcb1a7b1",
    ));
    v.push((
        T_OPEN,
        1,
        0,
        b"shell:ls".to_vec(),
        32,
        "4f50454e01000000000000000800000031030000b0afbab1",
    ));
    v.push((
        T_WRTE,
        5,
        1,
        b"hello world".to_vec(),
        35,
        "5752544505000000010000000b0000005c040000a8adabba",
    ));
    v.push((T_OKAY, 3, 4, Vec::new(), 24, "4f4b415903000000040000000000000000000000b0b4bea6"));
    v.push((T_CLSE, 7, 8, Vec::new(), 24, "434c534507000000080000000000000000000000bcb3acba"));
    let mut big = Vec::with_capacity(1024);
    for _ in 0..4 {
        big.extend(0u8..=255);
    }
    v.push((
        T_WRTE,
        0x10001,
        0x20002,
        big,
        1048,
        "5752544501000100020002000004000000fe0100a8adabba",
    ));
    v.push((
        T_WRTE,
        0x10001,
        0x10002,
        vec![b'A'; MAXDATA as usize],
        1_048_600,
        "5752544501000100020001000000100000001004a8adabba",
    ));
    v
}

#[test]
fn command_words_are_the_real_aosp_values() {
    assert_eq!(cmd::CNXN, 0x4E58_4E43);
    assert_eq!(cmd::OPEN, 0x4E45_504F);
    assert_eq!(cmd::WRTE, 0x4554_5257);
    assert_eq!(cmd::OKAY, 0x5941_4B4F);
    assert_eq!(cmd::CLSE, 0x4553_4C43);
    // 版本号与命令字是两个不同概念，不能混淆（曾因此写错基准）
    assert_eq!(VERSION, 0x0100_0000);
    assert_ne!(VERSION, cmd::CNXN);
}

#[test]
fn packets_match_python_reference_byte_for_byte() {
    for (c, a0, a1, payload, want_len, want_head) in vectors() {
        let pkt = pack(c, a0, a1, &payload);
        assert_eq!(
            pkt.len(),
            want_len,
            "包长应与 Python 版一致：cmd={:#x} payload={}B",
            c,
            payload.len()
        );
        let head: String = pkt[..24].iter().map(|b| format!("{:02x}", b)).collect();
        assert_eq!(
            head, want_head,
            "头部 24 字节必须与 Python 版逐字节相同（cmd={:#x}）",
            c
        );
    }
}

#[test]
fn header_size_is_24_bytes_not_20() {
    // 漏掉 payload_crc 时的症状：包长少 4 字节
    assert_eq!(pack(cmd::OKAY, 1, 2, b"").len(), 24);
    assert_eq!(pack(cmd::OKAY, 1, 2, b"x").len(), 25);
    let h = Header::from_bytes(&pack(cmd::OKAY, 1, 2, b"abc")).unwrap();
    assert_eq!(h.data_length, 3);
}

#[test]
fn magic_constants_match_reference() {
    assert_eq!(T_CNXN ^ 0xFFFF_FFFF, 0xB1A7_B1BC);
    assert_eq!(T_OPEN ^ 0xFFFF_FFFF, 0xB1BA_AFB0);
    assert_eq!(T_WRTE ^ 0xFFFF_FFFF, 0xBAAB_ADA8);
    assert_eq!(T_OKAY ^ 0xFFFF_FFFF, 0xA6BE_B4B0);
    assert_eq!(T_CLSE ^ 0xFFFF_FFFF, 0xBAAC_B3BC);
}

#[test]
fn payload_checksum_is_byte_sum_not_crc32() {
    // Python: sum(payload) & 0xFFFFFFFF
    assert_eq!(payload_checksum(b""), 0);
    assert_eq!(payload_checksum(b"shell:ls"), 0x0000_0331);
    assert_eq!(payload_checksum(b"hello world"), 0x0000_045C);
    // 累加和会超过 u8 但不溢出 u32：256 个 0xff = 0xff00
    assert_eq!(payload_checksum(&[0xffu8; 256]), 0xff00);
}

#[test]
fn packet_carries_payload_checksum_in_fifth_field() {
    // 空 payload 时校验和为 0；非空时必须等于字节和。
    // 这正是交叉校验最初抓到的差异：写 0 会与 Python 版不一致。
    let empty = pack(cmd::OKAY, 3, 4, b"");
    assert_eq!(Header::from_bytes(&empty).unwrap().payload_crc, 0);

    let with = pack(cmd::OPEN, 1, 0, b"shell:ls");
    let h = Header::from_bytes(&with).unwrap();
    assert_eq!(h.payload_crc, 0x0000_0331);
}

#[test]
fn window_and_maxdata_unchanged_from_python() {
    // 改这两个值会直接影响大文件传输稳定性
    assert_eq!(WINDOW, 262_144);
    assert_eq!(MAXDATA, 1_048_576);
}