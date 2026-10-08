#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
弱网条件下的 adb 测试客户端（在电脑 B 上运行）。
用法: python3 weaknet_client.py 172.16.0.106:16666 [标签]
"""
import hashlib
import os
import shutil
import subprocess
import sys
import time

TARGET = sys.argv[1] if len(sys.argv) > 1 else "172.16.0.106:16666"
LABEL = sys.argv[2] if len(sys.argv) > 2 else "unknown"
REMOTE = "/tmp/wn"
LOCAL = "/tmp/wn_local"

rows = []


def md5(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for c in iter(lambda: f.read(1 << 20), b""):
            h.update(c)
    return h.hexdigest()


def adb(args, timeout=180, stdout=None):
    return subprocess.run(["adb", "-s", TARGET] + args,
                          capture_output=(stdout is None),
                          stdout=stdout, timeout=timeout)


def record(name, ok, detail=""):
    rows.append((name, ok, detail))
    print("    %-14s %s  %s" % (name, "OK  " if ok else "FAIL", detail), flush=True)


def mkfile(path, size):
    with open("/dev/urandom", "rb") as r, open(path, "wb") as f:
        left = size
        while left:
            n = min(1 << 20, left)
            f.write(r.read(n))
            left -= n


def main():
    print("=== 档位 %s (target=%s) ===" % (LABEL, TARGET), flush=True)
    os.makedirs(LOCAL, exist_ok=True)

    # 0. 连接
    t0 = time.time()
    try:
        r = adb(["connect", TARGET], timeout=60)
        connected = ("connected" in r.stdout.decode(errors="replace")
                     or "already" in r.stdout.decode(errors="replace"))
    except Exception as e:
        connected = False
    record("connect", connected, "%.2fs" % (time.time() - t0))

    # 1. shell x3（延迟敏感）
    lats, okc = [], 0
    for i in range(3):
        try:
            t = time.time()
            r = adb(["shell", "echo", "wn%d" % i], timeout=60)
            dt = (time.time() - t) * 1000
            if ("wn%d" % i) in r.stdout.decode(errors="replace"):
                okc += 1
                lats.append(dt)
        except Exception:
            pass
    record("shell x3", okc == 3,
           "成功 %d/3, 平均 %.0fms" % (okc, sum(lats) / len(lats) if lats else 0))

    # 2. push 512KB
    try:
        p = os.path.join(LOCAL, "a512.bin")
        mkfile(p, 512 * 1024)
        m = md5(p)
        t = time.time()
        adb(["push", p, "%s/a512.bin" % REMOTE], timeout=180)
        dm = adb(["shell", "md5sum", "%s/a512.bin" % REMOTE], timeout=60).stdout.decode(
            errors="replace").split()
        record("push 512KB", bool(dm) and dm[0] == m,
               "%.2fs" % (time.time() - t))
    except Exception as e:
        record("push 512KB", False, str(e)[:50])

    # 3. exec-out 512KB
    try:
        out = os.path.join(LOCAL, "eo512.bin")
        t = time.time()
        with open(out, "wb") as f:
            adb(["exec-out", "cat", "%s/a512.bin" % REMOTE], timeout=180, stdout=f)
        record("exec-out 512KB", os.path.getsize(out) == 512 * 1024,
               "%.2fs, %dB" % (time.time() - t, os.path.getsize(out)))
    except Exception as e:
        record("exec-out 512KB", False, str(e)[:50])

    # 4. pull 512KB
    try:
        back = os.path.join(LOCAL, "a512_back.bin")
        t = time.time()
        adb(["pull", "%s/a512.bin" % REMOTE, back], timeout=180)
        record("pull 512KB", md5(back) == md5(os.path.join(LOCAL, "a512.bin")),
               "%.2fs" % (time.time() - t))
    except Exception as e:
        record("pull 512KB", False, str(e)[:50])

    # 5. push 2MB（较大传输）
    try:
        p = os.path.join(LOCAL, "b2m.bin")
        mkfile(p, 2 * 1024 * 1024)
        m = md5(p)
        t = time.time()
        adb(["push", p, "%s/b2m.bin" % REMOTE], timeout=180)
        dm = adb(["shell", "md5sum", "%s/b2m.bin" % REMOTE], timeout=60).stdout.decode(
            errors="replace").split()
        record("push 2MB", bool(dm) and dm[0] == m,
               "%.2fs (%.1f MB/s)" % (time.time() - t,
                                      2.0 / max(time.time() - t, 0.001)))
    except Exception as e:
        record("push 2MB", False, str(e)[:50])

    # 6. 重连能力
    try:
        ok = True
        for i in range(2):
            adb(["disconnect", TARGET], timeout=60)
            adb(["connect", TARGET], timeout=60)
            r = adb(["shell", "echo", "rc"], timeout=60)
            if "rc" not in r.stdout.decode(errors="replace"):
                ok = False
        record("重连 x2", ok, "")
    except Exception as e:
        record("重连 x2", False, str(e)[:50])

    try:
        adb(["shell", "rm", "-rf", REMOTE], timeout=60)
        adb(["disconnect", TARGET], timeout=30)
    except Exception:
        pass
    shutil.rmtree(LOCAL, ignore_errors=True)

    passed = sum(1 for _, ok, _ in rows if ok)
    print("SUMMARY %s: %d/%d" % (LABEL, passed, len(rows)), flush=True)


if __name__ == "__main__":
    main()
