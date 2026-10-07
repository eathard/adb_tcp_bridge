#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""在电脑 B 上排查 adb 连接卡在 offline 的问题。

现象：桥在 A 上重启过之后，B 上 `adb connect <A>:15555` 返回
"already connected"，但设备状态是 offline，所有命令都失败。
A 上同一台桥却是好的 —— 所以要弄清是 B 的 adb 缓存问题还是桥的问题。

用法: python diag_adb_state_b.py [目标串]
"""
import subprocess
import sys
import time

TARGET = sys.argv[1] if len(sys.argv) > 1 else "172.16.0.106:15555"


def run(args, timeout=30):
    p = subprocess.run(args, capture_output=True, timeout=timeout)
    return (p.returncode,
            p.stdout.decode("utf-8", "replace").strip(),
            p.stderr.decode("utf-8", "replace").strip())


def show(tag, args, timeout=30):
    rc, out, err = run(args, timeout)
    print("-- %s  ($ %s)" % (tag, " ".join(args)))
    print("   rc=%d" % rc)
    if out:
        for line in out.splitlines():
            print("   | %s" % line)
    if err:
        for line in err.splitlines():
            print("   ! %s" % line)
    print()
    return rc, out, err


print("adb 版本: %s" % run(["adb", "version"])[1].splitlines()[0])
print("目标: %s" % TARGET)
print()

show("初始 devices", ["adb", "devices", "-l"])
show("disconnect", ["adb", "disconnect", TARGET])
time.sleep(1.5)
show("disconnect 后的 devices", ["adb", "devices", "-l"])

show("connect", ["adb", "connect", TARGET])
# 逐步轮询，看状态什么时候变
for i in range(12):
    time.sleep(1)
    rc, out, err = run(["adb", "-s", TARGET, "get-state"], timeout=15)
    print("   轮询 %2d: get-state rc=%d %r" % (i, rc, out or err))
    if out.strip() == "device":
        break

show("最终 devices", ["adb", "devices", "-l"])
show("试一条命令", ["adb", "-s", TARGET, "shell", "echo DIAG_OK"])