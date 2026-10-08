#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""在电脑 B 上**后台**跑一个脚本并把输出写进文件，SSH 断开也不影响它。

为什么需要：`remote_test.py` 用 paramiko 的 exec_command 流式读输出，
连接一旦超时/断开，远端进程即便还在跑，输出也全丢了。跑 20 分钟稳定性
测试时必然踩到（900 秒硬编码超时）。

本工具的做法：
  1. SFTP 上传脚本到 /tmp
  2. `nohup python3 ... > /tmp/<名字>.log 2>&1 &` 启动，立刻返回
  3. 之后用 fetch 取回结果文件

用法:
  python run_remote_bg.py start <脚本名> [参数...]   # 启动，返回 pid
  python run_remote_bg.py fetch  <日志名>           # 取回日志并打印
  python run_remote_bg.py status<日志名>            # 看是否还在跑
"""
import os
import sys
import time

import paramiko

HOST = os.environ.get("SSH_HOST", "172.16.0.101")
USER = os.environ.get("SSH_USER", "mypc")
PORT = int(os.environ.get("SSH_PORT", "22"))
KEY = os.path.expanduser("~/.ssh/id_ed25519")
HERE = os.path.dirname(os.path.abspath(__file__))


def connect():
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    if os.path.exists(KEY):
        try:
            c.connect(HOST, port=PORT, username=USER, key_filename=KEY,
                       timeout=10, banner_timeout=15, auth_timeout=15)
            return c
        except Exception:
            c.close()
    c.connect(HOST, port=PORT, username=USER,
              password=os.environ.get("SSH_PASSWORD", ""), timeout=15)
    return c


def sh(c, cmd, timeout=60):
    i, o, e = c.exec_command(cmd, timeout=timeout)
    return o.read().decode("utf-8", "replace"), e.read().decode("utf-8", "replace")


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        return 1
    action = sys.argv[1]
    arg = sys.argv[2]
    c = connect()

    if action == "start":
        script = os.path.join(HERE, arg)
        remote = "/tmp/" + os.path.basename(arg)
        log = "/tmp/%s.log" % os.path.splitext(os.path.basename(arg))[0]
        extra = " ".join(sys.argv[3:])
        c.open_sftp().put(script, remote)
        cmd = ("cd /tmp && rm -f %s && setsid nohup python3 %s %s "
               "> %s 2>&1 < /dev/null & echo started pid=$!" % (log, remote, extra, log))
        out, err = sh(c, cmd, timeout=30)
        print("已在 %s:%s 启动后台任务" % (HOST, remote))
        print("日志文件: %s" % log)
        print(out.strip())
        if err.strip():
            print("[stderr] %s" % err.strip())

    elif action == "fetch":
        log = "/tmp/%s.log" % arg if not arg.endswith(".log") else "/tmp/" + arg
        out, _ = sh(c, "cat %s" % log, timeout=120)
        print(out)
        #顺手存一份到本地，方便事后翻
        local = os.path.join(HERE, os.path.basename(log))
        with open(local, "w", encoding="utf-8") as f:
            f.write(out)
        print("[已保存本地副本] %s" % local)

    elif action == "status":
        log = "/tmp/%s.log" % arg if not arg.endswith(".log") else "/tmp/" + arg
        out, _ = sh(c,
                    "if pgrep -f 'ping_stability_b.py' >/dev/null; then "
                    "echo RUNNING; else echo DONE; fi; "
                    "echo --- 行数=$(wc -l < %s) ---; tail -3 %s"
                    % (log, log), timeout=30)
        print(out)
    else:
        print("未知动作")
        return 1

    c.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())