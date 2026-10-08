#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成 Python 参考实现的包字节向量，供 Rust 侧交叉校验。

这是 Rust 重构的关键防线：协议字节必须与已验证的 Python 版**逐字节一致**，
否则桥根本无法工作（重构过程中已因漏掉第 5 个字段 payload_crc 而真实翻车一次）。

用法（在项目上级目录执行）：
    python gen_python_vectors.py
输出：
    tests/python_vectors.json
"""
import hashlib
import json
import os
import struct

HEADER = struct.Struct("<6I")  # 24 字节：cmd,arg0,arg1,len,crc,magic

# 真实 ADB 命令字，来自 AOSP adb.cpp。
# 注意：这些不是版本号一类的常量，四字节按 ASCII 顺序排列，
# 小端存储后就是 "CNXN" / "OPEN" 等字样的字节序反转。
CNXN = 0x4E584E43
OPEN = 0x4E45504F
OKAY = 0x59414B4F
CLSE = 0x45534C43
WRTE = 0x45545257

VERSION = 0x01000000
MAXDATA = 1 << 20


def pack(cmd, a0, a1, payload=b""):
    """与 adb_tcp_bridge.py 中的 pack() 完全一致。"""
    return (
        HEADER.pack(
            cmd,
            a0,
            a1,
            len(payload),
            sum(payload) & 0xFFFFFFFF,
            cmd ^ 0xFFFFFFFF,
        )
        + payload
    )


CASES = [
    # (name, cmd, arg0, arg1, payload)
    ("CNXN", CNXN, VERSION, MAXDATA, b""),
    ("OPEN", OPEN, 1, 0, b"shell:ls"),
    ("WRTE", WRTE, 5, 1, b"hello world"),
    ("OKAY", OKAY, 3, 4, b""),
    ("CLSE", CLSE, 7, 8, b""),
    ("WRTE_256x4", WRTE, 0x10001, 0x20002, bytes(range(256)) * 4),
    ("WRTE_1MB", WRTE, 0x10001, 0x10002, b"A" * MAXDATA),
]


def main():
    out = []
    for name, c, a0, a1, pl in CASES:
        p = pack(c, a0, a1, pl)
        out.append(
            {
                "name": name,
                "cmd": c,
                "arg0": a0,
                "arg1": a1,
                "payload_len": len(pl),
                "packet_len": len(p),
                "head24_hex": p[:24].hex(),
                "packet_md5": hashlib.md5(p).hexdigest(),
            }
        )
        print(
            "%-12s cmd=%08x 包长=%-8d head24=%s"
            % (name, c, len(p), p[:24].hex())
        )

    here = os.path.dirname(os.path.abspath(__file__))
    dst = os.path.join(here, "tests", "python_vectors.json")
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    with open(dst, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=1, ensure_ascii=False)
    print("\n已写入 %s" % dst)

    # 供 Rust 测试内联断言的关键常量
    print("\n关键常量（Rust 测试直接断言这些值）：")
    print("  CNXN  = 0x%08X  小端=%s" % (CNXN, CNXN.to_bytes(4, "little").hex()))
    print("  OPEN  = 0x%08X  小端=%s" % (OPEN, OPEN.to_bytes(4, "little").hex()))
    print("  WRTE  = 0x%08X  小端=%s" % (WRTE, WRTE.to_bytes(4, "little").hex()))
    print("  OKAY  = 0x%08X  小端=%s" % (OKAY, OKAY.to_bytes(4, "little").hex()))
    print("  CLSE  = 0x%08X  小端=%s" % (CLSE, CLSE.to_bytes(4, "little").hex()))
    print("  CNXN_MAGIC = 0x%08X" % (CNXN ^ 0xFFFFFFFF))
    print("  WRTE_MAGIC = 0x%08X" % (WRTE ^ 0xFFFFFFFF))
    print("  HEADER_SIZE = 24")


if __name__ == "__main__":
    main()