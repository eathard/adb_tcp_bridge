#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""诊断 exec-out 在并发 / 大尺寸下的失败临界点（在电脑 B 上运行）。"""
import os
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

TARGET = "172.16.0.106:15555"
REMOTE = "/tmp/dg"
SIZE = 10 * 1024 * 1024


def adb(args, timeout=180):
    return subprocess.run(["adb", "-s", TARGET] + args,
                          capture_output=True, timeout=timeout)


def one(i, size):
    t = time.time()
    try:
        p = subprocess.run(
            ["adb", "-s", TARGET, "exec-out", "head", "-c", str(size),
             REMOTE + "/d10.bin"], capture_output=True, timeout=180)
        return "n=%d i=%d rc=%d bytes=%d err=%s %.1fs" % (
            -1, i, p.returncode, len(p.stdout),
            p.stderr.decode(errors="replace").strip()[:100], time.time() - t)
    except Exception as e:
        return "n=%d i=%d EXC %s %.1fs" % (-1, i, str(e)[:80], time.time() - t)


def main():
    os.system("mkdir -p %s" % REMOTE)
    os.system("head -c %d /dev/urandom > /tmp/dg10.bin" % SIZE)
    r = adb(["push", "/tmp/dg10.bin", REMOTE + "/d10.bin"])
    print("push rc=%d err=%s" % (r.returncode,
                                 r.stderr.decode(errors="replace").strip()[:150]))
    chk = adb(["shell", "ls", "-l", REMOTE + "/d10.bin"])
    print("设备端: %s" % chk.stdout.decode(errors="replace").strip())

    for n in (1, 2, 4, 8):
        t = time.time()
        with ThreadPoolExecutor(max_workers=n) as ex:
            futs = [ex.submit(one, i, SIZE) for i in range(n)]
            for f in as_completed(futs):
                print(f.result().replace("n=-1", "n=%d" % n))
        print("  -> 并发 %d 路总耗时 %.1fs" % (n, time.time() - t))

    # 单路不同尺寸
    for mb in (1, 5, 10, 20):
        t = time.time()
        print("单路 %dMB: %s" % (mb, one(0, mb * 1024 * 1024).replace("n=-1", "n=1")))
        print("  %.1fs" % (time.time() - t))

    adb(["shell", "rm", "-rf", REMOTE])
    os.system("rm -f /tmp/dg10.bin")


if __name__ == "__main__":
    main()
