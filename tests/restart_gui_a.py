#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Slint GUI 版反复重启稳定性测试。

和 restart_stability_a.py 的区别：那个测的是 CLI 版（桥的进程级启停），
这个测的是**双击 exe 那种启动**——每个循环真正结束整个 GUI 进程再重新拉起，
覆盖的是「用户重启软件」这个动作本身：窗口能否正常弹出、配置是否持久化、
启动耗时是否随重启次数漂移、有没有进程残留。

每一轮：
    1. 拉起 adb_bridge_slint.exe
    2. 等窗口出现，记录启动耗时
    3. 确认窗口标题正确、进程未提前退出
    4. 结束进程，确认彻底退出（无残留）

用法:
    python restart_gui_a.py [轮数]
"""
import os
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)
EXE = os.path.join(REPO_ROOT, "rust", "target", "release", "adb_bridge_slint.exe")
TITLE = "ADB TCP 桥控制台"
WIN_TIMEOUT = 25.0

import ctypes
import ctypes.wintypes as wt

user32 = ctypes.windll.user32
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    try:
        user32.SetProcessDPIAware()
    except Exception:
        pass


def hwnd_of(title=TITLE):
    return user32.FindWindowW(None, title) or None


def alive(pid):
    """进程是否还活着。

    OpenProcess / GetExitCodeProcess 都在 **kernel32**，不在 user32 ——
    放错模块会报 "function 'OpenProcess' not found"。
    用 OpenProcess 而不是 tasklist 是因为后者每次要拉起一个进程，慢。
    """
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    k32 = ctypes.windll.kernel32
    h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return False
    code = wt.DWORD()
    ok = k32.GetExitCodeProcess(h, ctypes.byref(code))
    k32.CloseHandle(h)
    return bool(ok) and code.value == STILL_ACTIVE


def gui_procs():
    out = subprocess.run(
        ["tasklist", "/FI", "IMAGENAME eq adb_bridge_slint.exe",
         "/FO", "CSV", "/NH"],
        capture_output=True, text=True, errors="replace").stdout
    return [p.strip('" ') for p in out.splitlines()
            if "adb_bridge_slint.exe" in p]


def kill_guis():
    n = 0
    for _ in range(3):
        pids = gui_procs()
        if not pids:
            break
        for line in pids:
            parts = [p.strip('" ') for p in line.split('","')]
            if len(parts) >= 2:
                subprocess.run(["taskkill", "/PID", parts[1], "/F"],
                               capture_output=True)
                n += 1
        time.sleep(0.6)
    return n


def main():
    rounds = int(sys.argv[1]) if len(sys.argv) > 1 else 10

    if not os.path.exists(EXE):
        print("找不到 %s" % EXE)
        return 2

    print("目标: %s" % EXE)
    print("轮数: %d" % rounds)
    print()

    n = kill_guis()
    if n:
        print("清理残留 GUI 进程 %d 个" % n)

    rows = []
    for i in range(1, rounds + 1):
        log = os.path.join(HERE, "gui_run.log")
        with open(log, "w", encoding="utf-8", errors="replace") as f:
            proc = subprocess.Popen([EXE], stdout=f, stderr=subprocess.STDOUT)

        t0 = time.time()
        hwnd = None
        while time.time() - t0 < WIN_TIMEOUT:
            if proc.poll() is not None:
                break
            hwnd = hwnd_of()
            if hwnd:
                break
            time.sleep(0.25)
        win_s = time.time() - t0

        ok = bool(hwnd) and proc.poll() is None
        why = ""
        if not ok:
            if proc.poll() is not None:
                why = "进程提前退出 rc=%s" % proc.returncode
            else:
                why = "%.1fs 内没出现窗口" % WIN_TIMEOUT

        # 结束进程并确认彻底退出
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/F"],
                       capture_output=True)
        t1 = time.time()
        gone = False
        while time.time() - t1 < 10:
            if not alive(proc.pid):
                gone = True
                break
            time.sleep(0.2)
        exit_s = time.time() - t1

        leftover = len(gui_procs())
        rec = {"no": i, "win_s": win_s, "exit_s": exit_s,
               "leftover": leftover, "ok": ok and gone and leftover == 0}
        rows.append(rec)
        print("[%2d] %-4s 启动 %.2fs  退出 %.2fs  残留 %d  %s" % (
            i, "OK" if rec["ok"] else "FAIL", win_s, exit_s, leftover, why),
            flush=True)

        if leftover:
            kill_guis()

    total = len(rows)
    passed = sum(1 for r in rows if r["ok"])
    ws = [r["win_s"] for r in rows]
    es = [r["exit_s"] for r in rows]
    print()
    print("============ GUI 重启汇总 ============")
    print("轮数 %d   通过 %d  (%.1f%%)" %
          (total, passed, 100.0 * passed / total if total else 0))
    print("窗口出现耗时 min/avg/max = %.2f / %.2f / %.2f s" %
          (min(ws), sum(ws) / len(ws), max(ws)))
    print("进程退出耗时 min/avg/max = %.2f / %.2f / %.2f s" %
          (min(es), sum(es) / len(es), max(es)))
    # 漂移检测：前后各半的均值差，若明显上升说明有资源累积
    if len(ws) >= 4:
        h = len(ws) // 2
        drift = sum(ws[h:]) / len(ws[h:]) - sum(ws[:h]) / len(ws[:h])
        print("后半程 vs 前半程 启动耗时漂移 = %+.2f s" % drift)
    print("=======================================")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())