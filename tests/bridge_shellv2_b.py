#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""在电脑 B 上专项诊断 shell_v2 行为：stderr 是否合并、退出码是否透传。

用来区分两件事：
  1. 桥（谁在跑）的 CNXN banner 有问题
  2. adb 客户端本来就是这样（测试预期写错了）

直接打印原始的 stdout / stderr / returncode，不做断言。
"""
import subprocess
import sys

TARGET = sys.argv[1] if len(sys.argv) > 1 else "172.16.0.106:15555"


def raw(args, timeout=40):
    p = subprocess.run(args, capture_output=True, timeout=timeout)
    return (p.returncode,
            p.stdout.decode("utf-8", "replace"),
            p.stderr.decode("utf-8", "replace"))


print("目标设备串: %s" % TARGET)
print("adb 版本: %s" % raw(["adb", "version"])[1].splitlines()[0])
print()

# ⚠️ 必须先重连：换桥（或桥重启）之后，B 上 adb 仍缓存着旧连接，
# 设备会变成 offline，测出来的全是 "device offline"，容易误判成桥坏了。
raw(["adb", "disconnect", TARGET], timeout=20)
import time
time.sleep(1)
_rc, _out, _err = raw(["adb", "connect", TARGET], timeout=30)
print("重连: rc=%d %s%s" % (_rc, _out.strip(), _err.strip()))
print()

cases = [
    ("stderr 是否合并",
     ["adb", "-s", TARGET, "shell", "echo OUT; echo ERR 1>&2"]),
    ("退出码 7",
     ["adb", "-s", TARGET, "shell", "exit 7"]),
    ("退出码 0",
     ["adb", "-s", TARGET, "shell", "exit 0"]),
    ("退出码 42 + 输出",
     ["adb", "-s", TARGET, "shell", "echo hello; exit 42"]),
    ("exec-out 退出码",
     ["adb", "-s", TARGET, "exec-out", "sh -c", "exit 5"]),
    ("带 -x 标记的 shell",
     ["adb", "-s", TARGET, "shell", "-x", "echo XMARK"]),
]

for name, args in cases:
    rc, out, err = raw(args)
    print("── %s" % name)
    print("   命令      : %s" % " ".join(args[3:]))
    print("   returncode: %d" % rc)
    print("   stdout    : %r" % out)
    print("   stderr    : %r" % err)
    print()

# features 视图：客户端（这里就是 B 的 adb）眼中的设备能力
print("── 客户端眼里的 features")
try:
    rc, out, err = raw(["adb", "-s", TARGET, "shell", "echo $ADB_SERVER_SOCKET"], timeout=20)
    print("   (仅探测连通性) rc=%d" % rc)
except Exception as e:
    print("   异常 %s" % e)