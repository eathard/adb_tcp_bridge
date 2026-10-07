#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
把本地的 stress_test.py 上传到电脑 B（经 SSH/SFTP），在 B 上执行并回传输出。
与 ssh.py 使用同一套连接参数：SSH_HOST / SSH_USER / SSH_PORT / SSH_PASSWORD。
"""
import os
import sys
import time

import paramiko

HOST = os.environ.get("SSH_HOST", "172.16.0.101")
USER = os.environ.get("SSH_USER", "mypc")
PORT = int(os.environ.get("SSH_PORT", "22"))
KEY = os.path.expanduser("~/.ssh/id_ed25519")

SCRIPT_NAME = sys.argv[1] if len(sys.argv) > 1 else "stress_test.py"
LOCAL_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            SCRIPT_NAME)
REMOTE_PATH = "/tmp/" + SCRIPT_NAME


def connect():
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    if os.path.exists(KEY):
        try:
            client.connect(HOST, port=PORT, username=USER, key_filename=KEY,
                           timeout=10, banner_timeout=15, auth_timeout=15)
            return client
        except Exception:
            client.close()
    pwd = os.environ.get("SSH_PASSWORD", "")
    if not pwd:
        raise SystemExit("需要密码：设置 SSH_PASSWORD 环境变量")
    client.connect(HOST, port=PORT, username=USER, password=pwd, timeout=15)
    return client


def main():
    client = connect()
    print("[已连接 %s@%s]" % (USER, HOST), file=sys.stderr)

    sftp = client.open_sftp()
    sftp.put(LOCAL_SCRIPT, REMOTE_PATH)
    sftp.close()
    print("[已上传 %s -> %s:%s]" % (LOCAL_SCRIPT, HOST, REMOTE_PATH), file=sys.stderr)

    extra = " ".join(sys.argv[2:])
    cmd = "cd /tmp && python3 %s %s" % (REMOTE_PATH, extra)
    print("[执行] %s" % cmd, file=sys.stderr)
    t0 = time.time()
    stdin, stdout, stderr = client.exec_command(cmd, timeout=900)

    out_buf = []
    while True:
        chunk = stdout.channel.recv(65536)
        if not chunk:
            break
        text = chunk.decode("utf-8", "replace")
        out_buf.append(text)
        sys.stdout.write(text)
        sys.stdout.flush()

    code = stdout.channel.recv_exit_status()
    err = stderr.read().decode("utf-8", "replace")
    if err.strip():
        sys.stderr.write("\n[stderr]\n" + err)

    print("\n[远端耗时 %.1fs, 退出码 %d]" % (time.time() - t0, code), file=sys.stderr)
    client.close()
    sys.exit(code)


if __name__ == "__main__":
    main()
