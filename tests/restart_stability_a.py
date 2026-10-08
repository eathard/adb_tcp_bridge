#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""软件反复重启稳定性测试（驱动端跑在电脑 A）。

每一轮做完整闭环：
    1. 启动桥进程（CLI 版 adb_bridge_rs.exe）
    2. 等端口进入 LISTENING，记录启动耗时
    3. 让电脑 B 经桥执行几条命令，确认真的能干活
    4. 停止桥进程，记录停止耗时
    5. 确认端口已彻底释放（用 bind 试探，TIME_WAIT 不算占用）
    6. 确认没有残留进程

失败分类统计，因为「启动失败」「功能失败」「端口不释放」是三种完全不同的
故障，混在一个成功率里看不出问题。

用法:
    python restart_stability_a.py [轮数] [起始轮号]
"""
import os
import socket
import subprocess
import sys
import time

import paramiko

HOST_A = "172.16.0.106"
HOST_B = "172.16.0.101"
B_USER = "mypc"
B_PASS = os.environ.get("SSH_PASSWORD", "")
PORT = 15555
SERIAL = "b57290249a9b3206"
BRIDGE = "%s:%d" % (HOST_A, PORT)

# 本脚本位于 tests/，产物在 rust/target/release/ 下（另有一份副本在 releases/）
HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)
EXE = os.path.join(REPO_ROOT, "rust", "target", "release", "adb_bridge_rs.exe")

START_TIMEOUT = 10.0   # 端口进入 LISTENING 的上限
STOP_TIMEOUT = 10.0    # 进程退出的上限
FUNC_TIMEOUT = 40.0    # B 侧一组命令的上限


def port_listening(port=PORT):
    """能否连上 —— 比查 netstat 可靠，不受输出编码影响。"""
    try:
        s = socket.create_connection((HOST_A, port), timeout=1.5)
        s.close()
        return True
    except OSError:
        return False


def port_free(port=PORT):
    """端口能否被独占绑定。

    刻意用 bind 而不是 connect：刚关闭的连接会留在 TIME_WAIT 里，
    connect 探测受其影响、结论不可靠；bind 只关心有没有人真正占着。
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("0.0.0.0", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def stray_procs():
    """还有几个 adb_bridge 进程残留（不含本脚本自己）。"""
    out = subprocess.run(
        ["tasklist", "/FI", "IMAGENAME eq adb_bridge_rs.exe"],
        capture_output=True, text=True, errors="replace").stdout
    n = 0
    for line in out.splitlines():
        if "adb_bridge_rs.exe" in line and "信息" not in line and "Info" not in line:
            n += 1
    return n


class RemoteB:
    """电脑 B 上的操作通道，跨轮复用同一条 SSH 连接。"""

    def __init__(self):
        self.cli = paramiko.SSHClient()
        self.cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        self.cli.connect(HOST_B, port=22, username=B_USER, password=B_PASS,
                         timeout=15)

    def run(self, cmd, timeout=FUNC_TIMEOUT):
        _, out, err = self.cli.exec_command(cmd, timeout=timeout)
        rc = out.channel.recv_exit_status()
        return rc, out.read().decode("utf-8", "replace"), \
            err.read().decode("utf-8", "replace")

    def verify(self):
        """在 B 上经桥跑一组命令，返回 (是否通过, 明细)。

        必须轮询等待 state=device：`adb connect` 是异步的，返回 connected 时
        transport 往往还没握手完，固定 sleep 后立刻查会读到 device offline
        —— 那是 adb 客户端的状态，不是桥的。重启桥后这个窗口尤其明显。
        """
        B = BRIDGE
        self.run("adb disconnect %s >/dev/null 2>&1; sleep 1" % B)

        t0 = time.time()
        state = ""
        while time.time() - t0 < 30:
            _, o, _ = self.run("adb connect %s 2>&1 | tail -1" % B, timeout=25)
            if "already" in o and time.time() - t0 > 8:
                pass  # 已连接过就继续等状态
            time.sleep(1)
            _, o, _ = self.run("adb -s %s get-state 2>&1 | tail -1" % B, timeout=15)
            state = o.strip()
            if state == "device":
                break
        wait_s = time.time() - t0

        rc, o, e = self.run(
            "echo \"MARK=$(adb -s %s shell 'echo ROUND_OK-PROBE' 2>&1 | tail -1)\"; "
            "echo \"DEV=$(adb -s %s shell 'uname -n' 2>&1 | tail -1)\""
            % (B, B), timeout=30)
        mark = _field(o, "MARK")
        dev = _field(o, "DEV")
        ok = (state == "device" and mark.startswith("ROUND_OK-PROBE") and bool(dev))
        detail = "等设备 %.1fs state=%s mark=%s dev=%s" % (wait_s, state, mark, dev or "-")
        if not ok and e.strip():
            detail += " | stderr=%s" % e.strip().replace("\n", " ")[:80]
        return ok, detail


def _shq(s):
    return "'" + s.replace("'", "'\\''") + "'"


def _field(out, key):
    for line in out.splitlines():
        if line.startswith(key + "="):
            return line[len(key) + 1:].strip()
    return ""


def kill_stray_bridges():
    """结束所有在跑的桥进程（CLI / Slint 界面 / Python 三种产物），返回杀掉的个数。

    反复启停测试最容易被上一轮的残留进程污染：端口被占 -> 新桥启动失败 ->
    误报成「软件不稳定」。所以每轮前主动清场。
    """
    killed = 0
    for image in ("adb_bridge_rs.exe", "adb_bridge_slint.exe",
                  "adb_tcp_bridge.exe"):
        # 用 PID 精确结束，不用通配进程名 —— 那会连带杀掉 adb server，
        # 连带导致设备掉线（这个坑踩过）。
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq " + image, "/FO", "CSV", "/NH"],
            capture_output=True, text=True, errors="replace").stdout
        for line in out.splitlines():
            parts = [p.strip('" ') for p in line.split('","')]
            if len(parts) >= 2 and parts[0].lower() == image.lower():
                pid = parts[1]
                subprocess.run(["taskkill", "/PID", pid, "/F"],
                               capture_output=True)
                killed += 1
    return killed


def main():
    rounds = int(sys.argv[1]) if len(sys.argv) > 1 else 20
    start_no = int(sys.argv[2]) if len(sys.argv) > 2 else 1
    if not os.path.exists(EXE):
        print("找不到可执行文件: %s" % EXE)
        return 2

    print("目标: %s" % EXE)
    print("端口: %d   设备: %s" % (PORT, SERIAL))
    print("轮数: %d（从第 %d 轮开始）" % (rounds, start_no))
    print()

    # 开测前先清场：把残留的桥进程结束掉，否则端口被占会误报成「启动失败」
    n = kill_stray_bridges()
    if n:
        print("已清理残留桥进程 %d 个，等待端口释放…" % n)
        for _ in range(50):
            if port_free():
                break
            time.sleep(0.2)
    if port_listening():
        print("!! 开始前 15555 仍被占用，且不是桥进程 —— 请手动排查")
        return 2

    b = RemoteB()
    rows = []
    fails = {"start": 0, "func": 0, "stop": 0, "leak": 0}

    for i in range(rounds):
        no = start_no + i
        rec = {"no": no}
        proc = None
        try:
            # ---- 启动 ----
            t0 = time.time()
            proc = subprocess.Popen(
                [EXE, "--listen-port", str(PORT)],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, errors="replace")
            up = False
            while time.time() - t0 < START_TIMEOUT:
                if port_listening():
                    up = True
                    break
                if proc.poll() is not None:
                    break
                time.sleep(0.2)
            rec["start_s"] = time.time() - t0
            if not up:
                fails["start"] += 1
                rec["ok"] = False
                rec["why"] = "启动失败(%.1fs)%s" % (
                    rec["start_s"],
                    " 进程已退出: " + (proc.stdout.read() or "")[:200].replace("\n", " ")
                    if proc.poll() is not None else "")
                rows.append(rec)
                print("[%3d] %-4s %s" % (no, "FAIL", rec["why"]), flush=True)
                continue

            # ---- 功能 ----
            t1 = time.time()
            try:
                fok, fdetail = b.verify()
            except Exception as e:
                fok, fdetail = False, "B 侧异常: %s" % type(e).__name__
            rec["func_s"] = time.time() - t1
            rec["func_detail"] = fdetail
            if not fok:
                fails["func"] += 1

            # ---- 停止 ----
            t2 = time.time()
            proc.terminate()
            try:
                proc.wait(timeout=STOP_TIMEOUT)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
                rec["forced_kill"] = True
            rec["stop_s"] = time.time() - t2
            proc = None

            # ---- 端口释放 ----
            freed = False
            t3 = time.time()
            while time.time() - t3 < STOP_TIMEOUT:
                if port_free():
                    freed = True
                    break
                time.sleep(0.2)
            rec["free_s"] = time.time() - t3
            rec["leak"] = not freed

            stray = stray_procs()
            rec["stray"] = stray
            rec["ok"] = fok and freed and stray == 0
            if not freed:
                fails["stop"] += 1
            if stray:
                fails["leak"] += 1

            print("[%3d] %-4s 启动 %.2fs  功能 %.2fs  停止 %.2fs  释放 %.2fs  "
                  "残留进程 %d  | %s%s" % (
                      no, "OK" if rec["ok"] else "FAIL",
                      rec["start_s"], rec.get("func_s", 0), rec["stop_s"],
                      rec["free_s"], stray, fdetail,
                      "  [强杀]" if rec.get("forced_kill") else ""),
                  flush=True)
            rows.append(rec)

        except KeyboardInterrupt:
            print("\n中断，正在清理…")
            break
        except Exception as e:
            rec.setdefault("ok", False)
            rec.setdefault("why", "异常: %r" % e)
            rows.append(rec)
            print("[%3d] %-4s %s" % (no, "FAIL", rec["why"]), flush=True)
        finally:
            if proc is not None and proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)
            # 让下一轮从干净状态起步
            while not port_free():
                time.sleep(0.3)

    # ---- 汇总 ----
    total = len(rows)
    passed = sum(1 for r in rows if r.get("ok"))
    print()
    print("================ 汇总 ================")
    print("总轮数   %d" % total)
    print("完全通过 %d  (%.1f%%)" % (passed, 100.0 * passed / total if total else 0))
    print("启动失败 %d" % fails["start"])
    print("功能失败 %d" % fails["func"])
    print("端口未释放 %d" % fails["stop"])
    print("进程残留 %d" % fails["leak"])
    if rows:
        ss = [r["start_s"] for r in rows if "start_s" in r]
        fs = [r.get("func_s", 0) for r in rows]
        ts = [r["stop_s"] for r in rows if "stop_s" in r]
        print()
        print("启动耗时 min/avg/max = %.2f / %.2f / %.2f s" %
              (min(ss), sum(ss) / len(ss), max(ss)))
        print("单轮功能耗时 avg     = %.2f s" % (sum(fs) / len(fs)))
        print("停止耗时 min/avg/max = %.2f / %.2f / %.2f s" %
              (min(ts), sum(ts) / len(ts), max(ts)))
    print("=====================================")

    try:
        b.cli.close()
    except Exception:
        pass
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())