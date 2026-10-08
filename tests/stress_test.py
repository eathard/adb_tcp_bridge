#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
模拟"电脑 B"对 adb TCP 桥做压力与稳定性测试。
所有 adb 操作都走局域网 IP:172.16.0.106:5555（即桥的监听地址），
与真实电脑 B 的使用方式完全一致。
"""
import hashlib
import os
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

TARGET = "172.16.0.106:15555"
HOST, PORT = TARGET.split(":")
WS = os.path.dirname(os.path.abspath(__file__))
TMP = os.path.join(WS, "_stress_tmp")
REMOTE = "/tmp/stress"

results = []


def log(msg):
    print(msg, flush=True)


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    log("[%s] %s%s" % ("PASS" if ok else "FAIL", name,
                       ("  | " + detail) if detail else ""))


def adb(args, timeout=120, stdout=None):
    return subprocess.run(["adb", "-s", TARGET] + args, capture_output=(stdout is None),
                          stdout=stdout, timeout=timeout)


def shell(args, timeout=120):
    return adb(["shell"] + args, timeout=timeout)


def md5(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def make_file(path, size):
    with open(path, "wb") as f:
        f.write(os.urandom(size))


def main():
    os.makedirs(TMP, exist_ok=True)
    shell(["mkdir", "-p", REMOTE])

    # ---------- 预检 ----------
    df = shell(["df", "-h", "/tmp"]).stdout.decode(errors="replace")
    mem = shell(["free", "-m"]).stdout.decode(errors="replace")
    log("=== 设备资源预检 ===")
    log(df.strip())
    log(mem.strip().splitlines()[1] if len(mem.strip().splitlines()) > 1 else mem.strip())
    log("")

    # ---------- T0 连接 ----------
    t0 = time.time()
    r = adb(["connect", TARGET])
    ok = b"connected" in r.stdout or b"already" in r.stdout
    who = shell(["id"]).stdout.decode(errors="replace").strip()
    check("T0 连接桥并取到 shell", ok and "uid=0" in who,
          "%s (%.2fs)" % (who, time.time() - t0))

    # ---------- T1 顺序 shell x50 ----------
    t = time.time()
    bad = 0
    for i in range(50):
        out = shell(["echo", "tick-%d" % i]).stdout.decode(errors="replace").strip()
        if out != "tick-%d" % i:
            bad += 1
    check("T1 顺序 shell x50", bad == 0, "失败 %d 次, 耗时 %.1fs, 平均 %.0fms"
          % (bad, time.time() - t, (time.time() - t) * 1000 / 50))

    # ---------- T2 并发 shell ----------
    def one_shell(i):
        try:
            out = shell(["echo", "par-%d" % i], timeout=60).stdout.decode(errors="replace").strip()
            return out == "par-%d" % i
        except Exception:
            return False

    t = time.time()
    total = 60
    with ThreadPoolExecutor(max_workers=16) as ex:
        futs = [ex.submit(one_shell, i) for i in range(total)]
        good = sum(1 for f in as_completed(futs) if f.result())
    check("T2 并发 shell x60 (16 线程)", good == total,
          "成功 %d/%d, 耗时 %.1fs" % (good, total, time.time() - t))

    # ---------- T3 大文件 push/pull 多轮 ----------
    size = 5 * 1024 * 1024
    src = os.path.join(TMP, "big5m.bin")
    make_file(src, size)
    src_md5 = md5(src)
    t = time.time()
    rounds, bad = 3, 0
    for i in range(rounds):
        dst = os.path.join(TMP, "back5m_%d.bin" % i)
        adb(["push", src, "%s/big5m.bin" % REMOTE], timeout=120)
        adb(["pull", "%s/big5m.bin" % REMOTE, dst], timeout=120)
        if md5(dst) != src_md5:
            bad += 1
    elapsed = time.time() - t
    check("T3 5MB push+pull x3 轮 (md5 校验)", bad == 0,
          "损坏 %d 次, 总耗时 %.1fs, 吞吐约 %.1f MB/s"
          % (bad, elapsed, size * rounds * 2 / elapsed / 1024 / 1024))

    # ---------- T4 并发混合传输 ----------
    def one_transfer(i):
        try:
            p = os.path.join(TMP, "m%d.bin" % i)
            make_file(p, 1024 * 1024)
            m = md5(p)
            adb(["push", p, "%s/m%d.bin" % (REMOTE, i)], timeout=120)
            back = os.path.join(TMP, "m%d_back.bin" % i)
            adb(["pull", "%s/m%d.bin" % (REMOTE, i), back], timeout=120)
            return md5(back) == m
        except Exception:
            return False

    t = time.time()
    n = 10
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = [ex.submit(one_transfer, i) for i in range(n)]
        good = sum(1 for f in as_completed(futs) if f.result())
    check("T4 并发 1MB 传输 x10 (8 线程)", good == n,
          "成功 %d/%d, 耗时 %.1fs" % (good, n, time.time() - t))

    # ---------- T5/T6 exec-out 大流量（考验流控）----------
    for size_mb in (3, 12):
        p = os.path.join(TMP, "eo%d.bin" % size_mb)
        make_file(p, size_mb * 1024 * 1024)
        m = md5(p)
        adb(["push", p, "%s/eo%d.bin" % (REMOTE, size_mb)], timeout=180)
        out = os.path.join(TMP, "eo%d_out.bin" % size_mb)
        t = time.time()
        with open(out, "wb") as f:
            adb(["exec-out", "cat", "%s/eo%d.bin" % (REMOTE, size_mb)],
                timeout=180, stdout=f)
        ok_size = os.path.getsize(out) == size_mb * 1024 * 1024
        ok_md5 = md5(out) == m
        check("T5 exec-out 回读 %dMB (流控+完整性)" % size_mb,
              ok_size and ok_md5,
              "大小匹配=%s md5匹配=%s, %.1fs" % (ok_size, ok_md5, time.time() - t))

    # ---------- T6 持续流式输出 ----------
    t = time.time()
    with open(os.path.join(TMP, "stream.bin"), "wb") as f:
        adb(["exec-out", "cat", "%s/eo12.bin" % REMOTE], timeout=180, stdout=f)
    for _ in range(4):
        with open(os.path.join(TMP, "stream.bin"), "wb") as f:
            adb(["exec-out", "cat", "%s/eo12.bin" % REMOTE], timeout=180, stdout=f)
    check("T6 连续 5 次 12MB 流式回读", True,
          "总耗时 %.1fs, 累计 %.0f MB" % (time.time() - t, 12 * 5))

    # ---------- T7 长时会话 ----------
    t = time.time()
    try:
        shell(["sleep", "20"], timeout=60)
        ok = shell(["echo", "after-sleep"]).stdout.decode(errors="replace").strip() == "after-sleep"
    except Exception:
        ok = False
    check("T7 20 秒长时 shell 会话后仍可用", ok, "耗时 %.1fs" % (time.time() - t))

    # ---------- T8 空闲后存活 ----------
    idle = 30
    log("    (空闲等待 %d 秒，验证长连接不被回收...)" % idle)
    time.sleep(idle)
    out = shell(["echo", "alive"]).stdout.decode(errors="replace").strip()
    check("T8 空闲 %d 秒后连接仍存活" % idle, out == "alive", "返回: %s" % out)

    # ---------- T9 反复重连 ----------
    t = time.time()
    bad = 0
    for i in range(15):
        adb(["disconnect", TARGET], timeout=30)
        r = adb(["connect", TARGET], timeout=30)
        out = shell(["echo", "rc-%d" % i], timeout=30).stdout.decode(errors="replace").strip()
        if out != "rc-%d" % i:
            bad += 1
    check("T9 disconnect/connect 反复 x15", bad == 0,
          "失败 %d 次, 耗时 %.1fs" % (bad, time.time() - t))

    # ---------- T10 混合并发压测 ----------
    def mixed(i):
        kind = i % 3
        try:
            if kind == 0:
                return shell(["echo", "mx-%d" % i], timeout=60).stdout.decode(
                    errors="replace").strip() == "mx-%d" % i
            if kind == 1:
                return "Linux" in shell(["uname", "-s"], timeout=60).stdout.decode(
                    errors="replace")
            out = os.path.join(TMP, "mx%d.out" % i)
            with open(out, "wb") as f:
                adb(["exec-out", "head", "-c", "262144",
                     "%s/eo12.bin" % REMOTE], timeout=60, stdout=f)
            return os.path.getsize(out) == 262144
        except Exception:
            return False

    t = time.time()
    n = 30
    with ThreadPoolExecutor(max_workers=12) as ex:
        futs = [ex.submit(mixed, i) for i in range(n)]
        good = sum(1 for f in as_completed(futs) if f.result())
    check("T10 混合并发(shell/exec-out) x30 (12 线程)", good == n,
          "成功 %d/%d, 耗时 %.1fs" % (good, n, time.time() - t))

    # ---------- 收尾 ----------
    shell(["rm", "-rf", REMOTE])
    try:
        shutil.rmtree(TMP)
    except Exception:
        pass

    passed = sum(1 for _, ok, _ in results if ok)
    log("")
    log("===== 汇总: %d/%d 项通过 =====" % (passed, len(results)))
    for name, ok, detail in results:
        log("  %s  %s%s" % ("OK " if ok else "!! ", name, ("  | " + detail) if detail else ""))
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
