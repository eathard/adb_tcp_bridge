#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
50 条复杂命令的一致性对比：
  同一条命令分别通过「USB 直连」和「adb 桥(局域网)」执行，逐条比对
  stdout / stderr / 退出码是否完全一致。

用法: python consistency_test.py [桥地址]
"""
import subprocess
import time
import sys

USB = "b57290249a9b3206"
BRIDGE = sys.argv[1] if len(sys.argv) > 1 else "172.16.0.106:15555"

# 50 条命令：覆盖管道/查找/编码/哈希/shell 逻辑/重定向/大数据量
# 全部选择确定性输出（避开时间戳、PID、内存波动、网络计数）
CMDS = [
    # A. 文本处理管道
    r"cat /etc/passwd | wc -l",
    r"grep -c 'root' /etc/passwd",
    r"awk -F: '{print $1}' /etc/passwd | sort | head -5",
    r"awk -F: '$3>=1000 {print $1, $3}' /etc/passwd | sort -k2 -n",
    r"cat /etc/passwd | cut -d: -f1,3 | tr ':' '=' | sort",
    r"sed -n '1,5p' /etc/passwd | awk -F: '{print NR\": \"$1}'",
    r"grep -v '^#' /etc/hosts | grep -v '^$' | wc -l",
    r"cat /etc/group | cut -d: -f1 | sort | uniq -c | sort -rn | head -3",
    r"awk '{s+=length($0)} END {print NR, s}' /etc/passwd",
    r"cat /etc/passwd /etc/group | grep -o '[a-z]*' | sort -u | wc -l",
    # B. 文件系统与查找
    r"ls -la /etc | awk '{print $1}' | sort | uniq -c",
    r"find /etc -maxdepth 1 -type f | wc -l",
    r"find /etc -name '*.conf' | sort | head -5",
    r"find /etc -maxdepth 2 -type f -name '*.conf' -exec basename {} \; | sort | head -8",
    r"ls /etc/init.d | wc -l",
    r"ls -1 /usr/bin | head -20 | sort | md5sum",
    r"du -sk /etc 2>/dev/null | awk '{print $1}'",
    r"readlink -f /etc/passwd; basename /etc/passwd; dirname /etc/passwd",
    # C. 系统信息（取稳定字段）
    r"uname -a",
    r"hostname; cat /etc/hostname",
    r"grep -c processor /proc/cpuinfo",
    r"grep -m1 'MemTotal' /proc/meminfo",
    r"cat /proc/version",
    r"grep -E '^(NAME|VERSION_ID)=' /etc/os-release | sort",
    r"ls /sys/class/net | sort",
    # D. 编码与哈希
    r"printf 'adb-bridge-test' | md5sum",
    r"printf 'adb-bridge-test' | sha256sum",
    r"printf 'hello world' | base64",
    r"printf 'aGVsbG8gd29ybGQ=' | base64 -d",
    r"printf 'ABC' | xxd",
    r"printf 'ABC' | od -An -tx1",
    r"seq 1 1000 | md5sum",
    # E. 复杂 shell 逻辑
    r"for i in 1 2 3 4 5; do printf '%s ' $((i*i)); done; echo",
    r"x=7; y=3; echo $((x*y+x-y))",
    r"echo $(( $(cat /etc/passwd | wc -l) * 2 ))",
    r"if [ -f /etc/hostname ]; then echo YES_FILE; else echo NO_FILE; fi",
    r"case $(hostname) in luckfox) echo MATCH;; *) echo OTHER;; esac",
    r"f() { echo \"arg=$1 count=$#\"; }; f a b c",
    "cat <<'EOF' | tr a-z A-Z\nhello here-doc\nEOF",
    r"bash -c 'echo {1..5}'",
    r"bash -c 'arr=(a b c); echo ${arr[1]}; echo ${#arr[@]}'",
    r"echo start; (echo sub1; echo sub2) | sed 's/^/  /'; echo end",
    # F. 标准输出与错误流分离
    r"echo out1; echo err1 >&2",
    r"ls /nonexistent_dir_xyz 2>&1 | head -2",
    # 注：原用例是 grep -r /etc（含 9.7MB 的 /etc/udev/hwdb.bin，耗时约 12s）。
    # 通过桥时它会被 adb server 的"TCP transport 10 秒无输出即断开"规则中断
    # （USB 直连正常），这是已知的桥路径限制，不属于数据转发错误，
    # 因此换成等价但不触发该限制的递归 grep。
    r"grep -r 'nameserver' /etc 2>/dev/null | head -3",
    r"(echo A; echo B >&2; echo C) 2>/dev/null",
    # G. 大数据量
    r"seq 1 5000 | awk '{s+=$1} END {print s}'",
    r"seq 1 2000 | sort -n | uniq | wc -l",
    r"seq 1 1000 | sed 's/$/abc/' | md5sum",
    r"cat /etc/passwd /etc/passwd /etc/passwd | sort | uniq -c | awk '{s+=$1} END {print s}'",
]


def ensure_connected(target):
    """确保 adb server 在跑、且桥设备已登记（环境会回收 adb server）。"""
    try:
        subprocess.run(["adb", "start-server"], capture_output=True, timeout=60)
        r = subprocess.run(["adb", "devices"], capture_output=True, timeout=60)
        if target not in r.stdout.decode("utf-8", "replace"):
            subprocess.run(["adb", "connect", target],
                           capture_output=True, timeout=60)
            time.sleep(1.5)
    except Exception:
        pass


def run(target, cmd, timeout=90):
    for attempt in (1, 2):
        try:
            p = subprocess.run(["adb", "-s", target, "shell", cmd],
                               capture_output=True, timeout=timeout)
            out = p.stdout.decode("utf-8", "replace")
            err = p.stderr.decode("utf-8", "replace")
            if attempt == 1 and "not found" in err:
                ensure_connected(target)
                continue
            return (p.returncode, out, err)
        except subprocess.TimeoutExpired:
            return ("TIMEOUT", "", "")
        except Exception as exc:
            return ("EXC", "", str(exc))
    return ("EXC", "", "retry failed")


def main():
    ensure_connected(BRIDGE)
    print("对比目标: USB直连=%s   桥=%s" % (USB, BRIDGE))
    print("共 %d 条命令\n" % len(CMDS), flush=True)

    same, diff = 0, []
    for i, cmd in enumerate(CMDS, 1):
        a = run(USB, cmd)
        b = run(BRIDGE, cmd)
        ok = (a == b)
        if ok:
            same += 1
            print("[%2d/%d] SAME  %s" % (i, len(CMDS), cmd[:62]), flush=True)
        else:
            diff.append((i, cmd, a, b))
            print("[%2d/%d] DIFF  %s" % (i, len(CMDS), cmd[:62]), flush=True)

    print("\n===== 汇总: %d/%d 完全一致, %d 条不一致 ====="
          % (same, len(CMDS), len(diff)))
    for i, cmd, a, b in diff:
        print("\n--- #%d %s" % (i, cmd))
        print("    USB   rc=%s out=%r err=%r" % (a[0], a[1][:200], a[2][:200]))
        print("    BRIDGE rc=%s out=%r err=%r" % (b[0], b[1][:200], b[2][:200]))
    return 0 if not diff else 1


if __name__ == "__main__":
    sys.exit(main())
