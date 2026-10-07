#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
弱网测试编排（在本机 A 上运行）：
  1) 本地启动 weaknet_proxy.py（注入延迟/抖动/限速/停顿/断连）
  2) SSH 到电脑 B，执行 weaknet_client.py（B 连 A:16666）
  3) 停代理，换下一档
需要：本机 16666 端口已在防火墙放行（B 才能连进来）。
"""
import os
import subprocess
import sys
import time

import paramiko

HOST = os.environ.get("SSH_HOST", "172.16.0.101")
USER = os.environ.get("SSH_USER", "mypc")
PORT = int(os.environ.get("SSH_PORT", "22"))
KEY = os.path.expanduser("~/.ssh/id_ed25519")

HERE = os.path.dirname(os.path.abspath(__file__))
PROXY = os.path.join(HERE, "weaknet_proxy.py")
CLIENT = os.path.join(HERE, "weaknet_client.py")
REMOTE_CLIENT = "/tmp/weaknet_client.py"
PROXY_PORT = 16666
TARGET_PORT = 15555

PROFILES = [
    ("baseline(无损伤)", ["--delay-ms", "0"]),
    ("mild 30ms", ["--delay-ms", "30", "--jitter-ms", "10"]),
    ("medium 150ms+2Mbps", ["--delay-ms", "150", "--jitter-ms", "50",
                            "--rate-kbps", "2000", "--stall-prob", "0.02",
                            "--stall-ms", "150"]),
    ("bad 400ms+600k+停顿", ["--delay-ms", "400", "--jitter-ms", "150",
                             "--rate-kbps", "600", "--stall-prob", "0.05",
                             "--stall-ms", "400"]),
    ("terrible 800ms+200k+断连", ["--delay-ms", "800", "--jitter-ms", "300",
                                  "--rate-kbps", "200", "--stall-prob", "0.10",
                                  "--stall-ms", "800", "--drop-after", "25"]),
]


def connect_b():
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    if os.path.exists(KEY):
        client.connect(HOST, port=PORT, username=USER, key_filename=KEY,
                       timeout=15, banner_timeout=20, auth_timeout=20)
        return client
    pwd = os.environ.get("SSH_PASSWORD", "")
    if not pwd:
        raise SystemExit("需要 SSH_PASSWORD")
    client.connect(HOST, port=PORT, username=USER, password=pwd, timeout=15)
    return client


def main():
    b = connect_b()
    print("[已连接 %s@%s]" % (USER, HOST), file=sys.stderr)
    sftp = b.open_sftp()
    sftp.put(CLIENT, REMOTE_CLIENT)
    sftp.close()
    print("[已上传 %s]" % REMOTE_CLIENT, file=sys.stderr)

    summaries = []
    for label, opts in PROFILES:
        print("\n########## 档位: %s ##########" % label, flush=True)
        proxy = subprocess.Popen(
            [sys.executable, PROXY, "--listen", str(PROXY_PORT),
             "--target", str(TARGET_PORT)] + opts,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(2)
        t0 = time.time()
        try:
            cmd = "python3 %s %s:%d '%s'" % (REMOTE_CLIENT, "172.16.0.106",
                                             PROXY_PORT, label)
            stdin, stdout, stderr = b.exec_command(cmd, timeout=900)
            while True:
                chunk = stdout.channel.recv(65536)
                if not chunk:
                    break
                sys.stdout.write(chunk.decode("utf-8", "replace"))
                sys.stdout.flush()
            code = stdout.channel.recv_exit_status()
            err = stderr.read().decode("utf-8", "replace")
            if err.strip():
                print("[stderr] %s" % err.strip()[:300], file=sys.stderr)
        except Exception as exc:
            print("[档位执行异常] %s" % exc, file=sys.stderr)
        finally:
            print("    (该档耗时 %.1fs)" % (time.time() - t0), flush=True)
            proxy.terminate()
            try:
                proxy.wait(timeout=10)
            except Exception:
                proxy.kill()
            time.sleep(1.5)

    try:
        b.exec_command("rm -f %s" % REMOTE_CLIENT, timeout=30)
    except Exception:
        pass
    b.close()
    print("\n===== 弱网测试结束，共 %d 档 =====" % len(PROFILES))


if __name__ == "__main__":
    main()
