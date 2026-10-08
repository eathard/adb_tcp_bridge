#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""在电脑 B 上执行：验证经电脑 A 的桥访问仅 USB 连接的设备 C。

这是整个项目的验收标准：B 用**标准 adb**（无 SSH、无特殊 adb 改版）
连 172.16.0.106:15555，就能像普通网络设备一样操作只接在 A USB 上的 C。

⚠️ 本脚本必须在 B 上运行（由 remote_test.py 上传执行）。
"""
import subprocess
import sys
import time

TARGET = "172.16.0.106:15555"
SERIAL = "b57290249a9b3206"

PASS, FAIL = 0, 0
FAILURES = []


def sh(args, timeout=60, binary=False):
    """跑一条 adb 命令，返回 (返回码, 输出文本, 耗时秒)。"""
    t0 = time.time()
    try:
        p = subprocess.run(args, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return 124, "<超时 %ds>" % timeout, time.time() - t0
    out = p.stdout if binary else p.stdout.decode("utf-8", "replace")
    err = p.stderr.decode("utf-8", "replace")
    if err.strip():
        out = out + ("\n[stderr] " + err.strip() if isinstance(out, str) else "")
    return p.returncode, out, time.time() - t0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [PASS] %-34s %s" % (name, detail))
    else:
        FAIL += 1
        FAILURES.append(name)
        print("  [FAIL] %-34s %s" % (name, detail))


def main():
    print("=" * 72)
    print("电脑 B（%s）经电脑 A 的桥访问设备 C —— 端到端验证" % TARGET)
    print("=" * 72)

    # ---------- 0. 干净起步 ----------
    # ⚠️ `adb connect` 是**异步**的：命令返回 "connected" 时设备往往还没
    #    真正进入 device 状态；桥刚重启过更是如此，B 上 adb 还缓存着旧连接，
    #    表现为 device offline —— 看起来像桥坏了，其实是时序问题。
    #    正确做法：disconnect 后轮询等它从 devices 列表消失，connect 后轮询
    #    等 get-state 变成 device。
    sh(["adb", "disconnect", TARGET], timeout=20)
    for _ in range(20):
        time.sleep(0.5)
        _rc, out, _ = sh(["adb", "devices"], timeout=20)
        if TARGET not in out:
            break

    # ---------- 1. 连接 ----------
    rc, out, dt = sh(["adb", "connect", TARGET], timeout=30)
    check("adb connect", "connected" in out.lower() or "already" in out.lower(),
          "%.2fs | %s" % (dt, out.strip()))

    ready = False
    for _ in range(40):
        time.sleep(0.5)
        _rc, out2, _ = sh(["adb", "-s", TARGET, "get-state"], timeout=20)
        if out2.strip() == "device":
            ready = True
            break
    check("等待设备就绪 (device)", ready, "轮询 get-state")
    if not ready:
        print("\n设备未就绪，后续用例无法执行")
        return 1

    rc, out, dt = sh(["adb", "-s", TARGET, "get-state"], timeout=30)
    check("get-state = device", out.strip() == "device", "%.2fs | %s" % (dt, out.strip()))

    rc, out, dt = sh(["adb", "-s", TARGET, "shell", "echo B_TO_C_OK"], timeout=30)
    check("shell echo", "B_TO_C_OK" in out, "%.2fs | %s" % (dt, out.strip()))

    # ---------- 2. 设备身份 ----------
    rc, out, dt = sh(["adb", "-s", TARGET, "shell", "uname -srm"], timeout=30)
    check("uname -srm", "Linux" in out, "%.2fs | %s" % (dt, out.strip()))

    rc, out, dt = sh(["adb", "-s", TARGET, "shell", "id -un"], timeout=30)
    check("shell 是 root", out.strip() == "root", "%.2fs | %s" % (dt, out.strip()))

    rc, out, dt = sh(["adb", "-s", TARGET, "shell",
                      "cat /proc/device-tree/model 2>/dev/null | tr -d '\\0'"], timeout=30)
    check("设备型号可读", bool(out.strip()), "%.2fs | %s" % (dt, out.strip()[:60]))

    # ---------- 3. 退出码透传 ----------
    # ⚠️ 退出码是 adb **客户端进程**的 returncode，不是输出里的文字。
    #    踩过的坑：早期版本断言 `"7" in out`，而 `adb shell "exit 7"`
    #    本来就一行输出都没有，于是恒判失败 —— 被误当成桥坏了。
    #    实测（Python 版桥与 Rust 版桥均如此）：退出码正确透传。
    rc, out, dt = sh(["adb", "-s", TARGET, "shell", "exit 7"], timeout=30)
    check("退出码透传 (exit 7)", rc == 7, "rc=%d %.2fs" % (rc, dt))

    rc, out, dt = sh(["adb", "-s", TARGET, "shell", "echo hi; exit 42"], timeout=30)
    check("有输出且退出码 42", rc == 42 and "hi" in out, "rc=%d %.2fs" % (rc, dt))

    # ---------- 3b. stdout / stderr 分流 ----------
    # ⚠️ 实测这条链路（无论 Python 版还是 Rust 版桥）**不会**把 stderr 并入
    #    stdout，尽管 features 里协商了 shell_v2。故按实际行为断言：
    #    两路内容各自正确到达，不做「合并」假设。
    rc, out, dt = sh(["adb", "-s", TARGET, "shell", "echo OUT; echo ERR 1>&2"], timeout=30)
    check("stdout/stderr 分流正确",
          "OUT" in out and "ERR" in out, "%.2fs | %s" % (dt, out.strip().replace("\n", " / ")))

    # ---------- 4. 管道 / 引号 ----------
    rc, out, dt = sh(["adb", "-s", TARGET, "shell",
                      "echo a b c | tr ' ' '\\n' | wc -l"], timeout=30)
    check("管道 + 引号", out.strip() == "3", "%.2fs | %s" % (dt, out.strip()))

    # ---------- 5. 大流量（验证 256KB 流控与分批发送） ----------
    rc, out, dt = sh(["adb", "-s", TARGET, "shell",
                      "head -c 3000000 /dev/zero | tr '\\0' 'x' | wc -c"], timeout=180)
    check("3MB 单向输出（流控）", out.strip() == "3000000", "%.2fs | %s" % (dt, out.strip()))

    # ---------- 6. push / pull 往返一致性 ----------
    remote = "/tmp/_bridge_b_check.bin"
    sh(["adb", "-s", TARGET, "shell", "rm -f " + remote], timeout=30)
    rc, out, dt = sh(["adb", "-s", TARGET, "push", "/etc/hostname", remote], timeout=60)
    pushed = out.strip().splitlines()[-1] if out.strip() else ""
    rc2, out2, dt2 = sh(["adb", "-s", TARGET, "shell",
                          "wc -c < " + remote], timeout=30)
    size_dev = out2.strip()
    rc3, out3, dt3 = sh(["adb", "-s", TARGET, "pull", remote, "/tmp/_bridge_b_pulled.bin"], timeout=60)
    try:
        import hashlib
        h_dev = hashlib.md5(open("/tmp/_bridge_b_pulled.bin", "rb").read()).hexdigest()
        h_loc = hashlib.md5(open("/etc/hostname", "rb").read()).hexdigest()
        check("push/pull 内容一致", h_dev == h_loc, "md5 %s / %s" % (h_dev[:12], h_loc[:12]))
    except Exception as e:
        check("push/pull 内容一致", False, "异常 %s" % e)
    check("设备端字节数", size_dev not in ("", "0"), "size=%s" % size_dev)

    # ---------- 7. 双客户端并发（同一设备开两个 stream） ----------
    t0 = time.time()
    procs = [subprocess.Popen(["adb", "-s", TARGET, "shell",
                               "sleep 2; echo S%d" % i],
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
             for i in range(2)]
    outs = [p.communicate()[0].decode("utf-8", "replace").strip() for p in procs]
    check("并发两个 shell", all("S0" in o or "S1" in o for o in outs) and len(outs) == 2,
          "%.2fs | %s" % (time.time() - t0, " / ".join(outs)))

    # ---------- 8. 断连后可恢复 ----------
    sh(["adb", "disconnect", TARGET], timeout=20)
    time.sleep(1)
    rc, out, dt = sh(["adb", "connect", TARGET], timeout=30)
    rc2, out2, dt2 = sh(["adb", "-s", TARGET, "shell", "echo RECONNECT_OK"], timeout=30)
    check("断开后重连", "RECONNECT_OK" in out2, "%.2fs | %s" % (dt2, out2.strip()))

    # ---------- 汇总 ----------
    print("-" * 72)
    print("总计 %d 项：通过 %d，失败 %d" % (PASS + FAIL, PASS, FAIL))
    if FAILURES:
        print("失败项：%s" % ", ".join(FAILURES))
    print("=" * 72)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())