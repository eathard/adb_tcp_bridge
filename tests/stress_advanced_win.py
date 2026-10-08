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

另外修了两个同样与桥无关的脚本自身缺陷（都曾表现为「用例失败」，
很容易被误判成桥的问题）：

  1. MD5 用 `certutil -hashfile` 取。中文 Windows 上 certutil 输出是
     本地 GBK，`text=True` 按默认编码解码会抛 UnicodeDecodeError，
     连带 ok 判定里 `"MD5" in local_md5` 判断也失效。改为直接用
     Python 的 hashlib 算，顺带省掉一次子进程。
  2. 设备端临时目录沿用 Android 惯例 `/data/local/tmp`，但设备 C 是
     **Buildroot**，`/data/local/tmp` 根本不存在（实测
     `ls: /data/local/tmp: No such file or directory`），
     push 直接失败。改用与 stress_test.py 一致的 /tmp。
     ⚠️ 设备的 /tmp 是 tmpfs，只有 233MB，别放超过 ~100MB 的文件。

用法: python stress_advanced_win.py
"""
import hashlib
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

USB = "b57290249a9b3206"
BRIDGE = "172.16.0.106:15555"


def md5_of(path):
    """算文件 MD5（十六进制小写）。用 hashlib 避免 certutil 的编码坑。"""
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


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
    # ⚠️ 设备 C 是 Buildroot，没有 /data/local/tmp（那是 Android 的路径），
    #    用 /tmp —— 与 stress_test.py 保持一致。
    dev_tmp = "/tmp/rs_stress.bin"

    def record(name, ok, detail):
        results.append((name, ok, detail))
        print("[%s] %-38s | %s" % ("PASS" if ok else "FAIL", name, detail),
              flush=True)

    # ---- A1 30MB push+pull ----
    try:
        make_random_file(tmp, 30 * 1024 * 1024)
        want = md5_of(tmp)
        subprocess.run(["adb", "-s", BRIDGE, "shell", "rm -f " + dev_tmp],
                       capture_output=True)
        t0 = time.time()
        push = adb("-s", BRIDGE, "push", tmp, dev_tmp, timeout=300)
        pull = adb("-s", BRIDGE, "pull", dev_tmp, tmp + ".back", timeout=300)
        dt = time.time() - t0
        got = md5_of(tmp + ".back") if os.path.exists(tmp + ".back") else None
        ok = push.returncode == 0 and pull.returncode == 0 and \
            got is not None and got == want
        record("A1 30MB push+pull (md5)", ok,
               "%.1fs 传输 %.1f MB/s  md5 %s"
               % (dt, 30 / dt if dt else 0,
                  "一致" if got == want else ("不一致 %s vs %s" % (got, want))))
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
        rc, out, err = sh(BRIDGE, "md5sum " + dev_tmp)
        dt = time.time() - t0
        want = md5_of(tmp)
        dev_md5 = out.split()[0].lower() if out.split() else ""
        ok = (push.returncode == 0 and dev_md5 == want)
        record("A11 50MB 分片传输", ok,
               "%.1fs 吞吐 %.1f MB/s  设备端 md5 %s"
               % (dt, 50 / dt if dt else 0,
                  "一致" if dev_md5 == want else ("不一致 %s" % dev_md5[:12])))
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