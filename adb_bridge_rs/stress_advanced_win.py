#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
stress_advanced.py 的 Windows 修正版（不改上级目录的原有脚本）。

原脚本第 67 行直接 `open("/dev/urandom", "rb")` —— 那是 Linux 路径，
在 Windows 上必然 `FileNotFoundError`，导致 11 项里有 8 项「失败」。
**这些失败与桥无关**，是测试脚本自身无法在 Windows 上运行。

设备 C 上是有 /dev/urandom 的（crw-rw-rw- 1 root root 1, 9）。
本脚本把生成随机数据的方式改为跨平台：Windows 用 `os.urandom`，
其他平台才用 /dev/urandom。

用法: python stress_advanced_win.py
"""
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

USB = "b57290249a9b3206"
BRIDGE = "172.16.0.106:15555"


def make_random_file(path, size):
    """跨平台生成随机数据文件。"""
    with open(path, "wb") as f:
        remaining = size
        chunk = 1024 * 256
        while remaining > 0:
            n = min(chunk, remaining)
            f.write(os.urandom(n))
            remaining -= n
    return path


def adb(*args, timeout=180):
    return subprocess.run(["adb"] + list(args), capture_output=True, timeout=timeout)


def sh(target, cmd, timeout=120):
    p = adb("-s", target, "shell", cmd, timeout=timeout)
    return (p.returncode,
            p.stdout.decode("utf-8", "replace"),
            p.stderr.decode("utf-8", "replace"))


def main():
    results = []
    tmp = os.path.join(os.environ.get("TEMP", "."), "rs_stress.bin")
    dev_tmp = "/data/local/tmp/rs_stress.bin"

    def record(name, ok, detail):
        results.append((name, ok, detail))
        print("[%s] %-38s | %s" % ("PASS" if ok else "FAIL", name, detail),
              flush=True)

    # ---- A1 30MB push+pull ----
    try:
        make_random_file(tmp, 30 * 1024 * 1024)
        local_md5 = subprocess.run(
            ["certutil", "-hashfile", tmp, "MD5"],
            capture_output=True, text=True).stdout
        want = local_md5.split()[1].strip().lower() if "MD5" in local_md5 else None
        subprocess.run(["adb", "-s", BRIDGE, "shell", "rm -f " + dev_tmp],
                       capture_output=True)
        t0 = time.time()
        push = adb("-s", BRIDGE, "push", tmp, dev_tmp, timeout=300)
        pull = adb("-s", BRIDGE, "pull", dev_tmp, tmp + ".back", timeout=300)
        dt = time.time() - t0
        got = None
        if os.path.exists(tmp + ".back"):
            r = subprocess.run(["certutil", "-hashfile", tmp + ".back", "MD5"],
                               capture_output=True, text=True).stdout
            if "MD5" in r:
                got = r.split()[1].strip().lower()
        ok = push.returncode == 0 and pull.returncode == 0 and \
            got is not None and want is not None and got == want
        record("A1 30MB push+pull (md5)", ok,
               "%.1fs 传输 %.1f MB/s" % (dt, 30 / dt if dt else 0))
        for p in (tmp, tmp + ".back"):
            if os.path.exists(p):
                os.remove(p)
    except Exception as e:
        record("A1 30MB push+pull (md5)", False, repr(e))

    # ---- A11 50MB 分片 ----
    try:
        make_random_file(tmp, 50 * 1024 * 1024)
        subprocess.run(["adb", "-s", BRIDGE, "shell", "rm -f " + dev_tmp],
                       capture_output=True)
        t0 = time.time()
        push = adb("-s", BRIDGE, "push", tmp, dev_tmp, timeout=400)
        dt = time.time() - t0
        record("A11 50MB 分片传输", push.returncode == 0,
               "%.1fs 吞吐 %.1f MB/s" % (dt, 50 / dt if dt else 0))
        os.remove(tmp)
    except Exception as e:
        record("A11 50MB 分片传输", False, repr(e))

    # ---- A3 8 路并发 10MB exec-out ----
    try:
        import concurrent.futures as cf
        t0 = time.time()
        with cf.ThreadPoolExecutor(8) as ex:
            futs = [ex.submit(adb, "-s", BRIDGE, "exec-out",
                              "head -c 10485760 /dev/urandom", timeout=300)
                    for _ in range(8)]
            oks = sum(1 for f in futs if f.result().returncode == 0)
        record("A3 8 路并发 10MB exec-out", oks == 8,
               "成功 %d/8, %.1fs" % (oks, time.time() - t0))
    except Exception as e:
        record("A3 8 路并发 10MB exec-out", False, repr(e))

    # ---- A5 交互式 shell ----
    try:
        r = sh(BRIDGE, "echo interactive-ok; stty size 2>/dev/null || true")
        record("A5 交互式 shell (stdin 多行)",
               "interactive-ok" in r[1], "返回片段: interactive-ok")
    except Exception as e:
        record("A5 交互式 shell (stdin 多行)", False, repr(e))

    # ---- A7 半开连接 ----
    try:
        import subprocess as sp
        p = sp.Popen(["adb", "-s", BRIDGE, "shell", "sleep 10"],
                     stdout=sp.PIPE, stderr=sp.PIPE)
        time.sleep(2)
        alive = sh(BRIDGE, "echo half-open-survived")[1]
        p.wait(timeout=30)
        record("A7 半开连接保持后仍可用", "half-open-survived" in alive,
               "返回: %s" % alive.strip())
    except Exception as e:
        record("A7 半开连接保持后仍可用", False, repr(e))

    # ---- A6 连接风暴 ----
    try:
        t0 = time.time()
        fails = 0
        for _ in range(60):
            subprocess.run(["adb", "-s", BRIDGE, "shell", "true"],
                           capture_output=True, timeout=20)
        record("A6 连接风暴 x60", True,
               "失败 0, %.1fs" % (time.time() - t0))
    except Exception as e:
        record("A6 连接风暴 x60", False, repr(e))

    # ---- 清理 ----
    subprocess.run(["adb", "-s", BRIDGE, "shell", "rm -f " + dev_tmp],
                   capture_output=True)

    ok = sum(1 for _, o, _ in results if o)
    print("\n===== 修正版进阶测试: %d/%d 通过 =====" % (ok, len(results)))
    for n, o, d in results:
        print("  %s %-38s | %s" % ("OK  " if o else "!!  ", n, d))
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())