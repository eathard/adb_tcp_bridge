#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把本地文件上传到电脑 B 的 /tmp（与 remote_test.py 共用同一套 SSH 参数）。

用途：跨机一致性测试需要把 A 上导出的「USB 基线」json 送到 B 上，
remote_test.py 只能传脚本，传不了数据文件，所以单独提供这个小工具。

用法: python upload_to_b.py <本地路径> [远端路径]
"""
import os
import sys

import paramiko

HOST = os.environ.get("SSH_HOST", "172.16.0.101")
USER = os.environ.get("SSH_USER", "mypc")
PORT = int(os.environ.get("SSH_PORT", "22"))
KEY = os.path.expanduser("~/.ssh/id_ed25519")


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
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    local = sys.argv[1]
    remote = sys.argv[2] if len(sys.argv) > 2 else "/tmp/" + os.path.basename(local)
    client = connect()
    sftp = client.open_sftp()
    sftp.put(local, remote)
    size = sftp.stat(remote).st_size
    sftp.close()
    client.close()
    print("已上传 %s (%d 字节) -> %s:%s" % (local, size, HOST, remote))
    return 0


if __name__ == "__main__":
    sys.exit(main())