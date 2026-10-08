#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""强杀恢复测试：模拟程序崩溃 / 用户用任务管理器结束进程。

比正常重启更严苛也更贴近现实：进程没机会释放资源，端口会留在
TIME_WAIT 或被内核挂住。如果软件在强杀后不能被下一个实例接管、
或者残留的会话把状态搞乱，这一轮就会暴露出来。

两阶段：
    A. 强杀（taskkill /F，无条件终止）后重启，验证端口可接管、功能可用
    B. 强杀时**连接还挂着**（B侧正在跑长命令），验证重启后能恢复

用法:
    python kill_recovery_a.py [轮数]
"""
import os
import socket
import subprocess
import sys
import time

import paramiko

HOST_A = "172.16.0.106"
HOST_B = "172.16.0.101"
PORT = 15555
BRIDGE = "%s:%d" % (HOST_A, PORT)
HERE = os.path.dirname(os.path.abspath(__file__))
EXE = os.path.join(HERE, "target", "release", "adb_bridge_rs.exe")


def listening():
    try:
        s = socket.create_connection((HOST_A, PORT), timeout=1.5)
        s.close()
        return True
    except OSError:
        return False


def port_free():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("0.0.0.0", PORT))
        return True
    except OSError:
        return False
    finally:
        s.close()


def start_bridge(timeout=12.0):
    log = open(os.path.join(HERE, "kill_run.log"), "w",
               encoding="utf-8", errors="replace")
    p = subprocess.Popen([EXE, "--listen-port", str(PORT)],
                         stdout=log, stderr=subprocess.STDOUT)
    t0 = time.time()
    while time.time() - t0 < timeout:
        if listening():
            return p, time.time() - t0
        if p.poll() is not None:
            return p, time.time() - t0
        time.sleep(0.2)
    return p, time.time() - t0


class RemoteB:
    def __init__(self):
        self.cli = paramiko.SSHClient()
        self.cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        self.cli.connect(HOST_B, port=22, username="mypc", password=SSH_PASSWORD,
                         timeout=15)

    def run(self, cmd, timeout=60):
        _, o, e = self.cli.exec_command(cmd, timeout=timeout)
        rc = o.channel.recv_exit_status()
        return rc, o.read().decode("utf-8", "replace"), \
            e.read().decode("utf-8", "replace")

    def wait_device(self, tries=25):
        for _ in range(tries):
            self.run("adb connect %s >/dev/null 2>&1" % BRIDGE, timeout=25)
            time.sleep(1)
            _, o, _ = self.run("adb -s %s get-state 2>&1 | tail -1" % BRIDGE,
                               timeout=15)
            if o.strip() == "device":
                return True
        return False

    def probe(self):
        _, o, _ = self.run(
            "adb -s %s shell 'echo AFTER_KILL_OK; uname -n' 2>&1 | tail -2"
            % BRIDGE, timeout=30)
        ok = "AFTER_KILL_OK" in o
        dev = o.strip().splitlines()[-1].strip() if ok else "-"
        return ok, "dev=%s" % dev

    def start_bg_cmd(self):
        """在 B 上后台起一个长命令（占住一个 stream 挂着不断）。"""
        self.run(
            "nohup adb -s %s shell 'sleep 120' >/dev/null 2>&1 & echo bg_started"
            % BRIDGE, timeout=20)

    def close(self):
        try:
            self.cli.close()
        except Exception:
            pass


def main():
    rounds = int(sys.argv[1]) if len(sys.argv) > 1 else 10

    # 清场
    subprocess.run(["taskkill", "/IM", "adb_bridge_rs.exe", "/F"],
                   capture_output=True)
    time.sleep(1.0)

    b = RemoteB()
    ok_cnt = 0
    print("目标: %s" % EXE)
    print("强杀轮数: %d" % rounds)
    print()
    print("每轮：启动 -> B 接入并挂一个长命令 -> taskkill /F 强杀 -> "
          "重启 -> 验证端口可接管且功能恢复")
    print()

    for i in range(1, rounds + 1):
        row = {"no": i}
        try:
            # ---- 1. 启动 ----
            p, boot_s = start_bridge()
            if not listening():
                print("[%2d] FAIL 启动失败 %.1fs" % (i, boot_s), flush=True)
                continue
            row["boot_s"] = boot_s

            # ---- 2. B 接入并挂住一个 stream ----
            if not b.wait_device():
                print("[%2d] FAIL 设备未上线" % i, flush=True)
                p.kill()
                continue
            b.start_bg_cmd()          # 故意留一个活着的连接/stream
            time.sleep(2)

            # ---- 3. 强杀（模拟崩溃）----
            t0 = time.time()
            subprocess.run(["taskkill", "/PID", str(p.pid), "/F"],
                           capture_output=True)
            kill_s = time.time() - t0
            row["kill_s"] = kill_s
            p.wait(timeout=10)

            # ---- 4. 端口是否立即可接管 ----
            t1 = time.time()
            freed = False
            while time.time() - t1 < 12:
                if port_free():
                    freed = True
                    break
                time.sleep(0.2)
            row["free_s"] = time.time() - t1
            row["freed"] = freed
            if not freed:
                print("[%2d] FAIL 强杀后 %.1fs 端口仍未释放" % (i, row["free_s"]),
                      flush=True)
                continue

            # ---- 5. 重启并验证功能恢复 ----
            p2, boot2 = start_bridge()
            row["reboot_s"] = boot2
            if not listening():
                print("[%2d] FAIL 重启失败 %.1fs" % (i, boot2), flush=True)
                continue
            got_dev = b.wait_device()
            pok, pdetail = b.probe()
            row["func_ok"] = got_dev and pok
            row["detail"] = pdetail

            # 清理
            subprocess.run(["taskkill", "/PID", str(p2.pid), "/F"],
                           capture_output=True)
            p2.wait(timeout=10)

            ok = row["func_ok"]
            ok_cnt += 1 if ok else 0
            print("[%2d] %-4s 启动 %.2fs  强杀 %.2fs  释放 %.2fs  "
                  "重启 %.2fs  恢复 %.1fs  | %s" % (
                      i, "OK" if ok else "FAIL", boot_s, kill_s,
                      row["free_s"], boot2,
                      0.0, pdetail), flush=True)

        except Exception as e:
            print("[%2d] FAIL 异常 %r" % (i, e), flush=True)
        finally:
            subprocess.run(["taskkill", "/IM", "adb_bridge_rs.exe", "/F"],
                           capture_output=True)
            time.sleep(0.6)

    print()
    print("============ 强杀恢复汇总 ============")
    print("轮数 %d   通过 %d  (%.1f%%)" %
          (rounds, ok_cnt, 100.0 * ok_cnt / rounds if rounds else 0))
    print("=====================================")
    b.close()
    return 0 if ok_cnt == rounds else 1


if __name__ == "__main__":
    sys.exit(main())