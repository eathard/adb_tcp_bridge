#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
第二批 50 条命令的一致性对比（第一批 50 条见 consistency_test.py）。

在第一批已覆盖的「文本管道 / 文件查找 / 系统信息 / 哈希编码 / shell 逻辑 /
输出错误分离 / 大数据量」之外，本批扩展 6 个新维度：

  H. 二进制与编码边界  非文本字节、NUL 计数、base64 往返、cksum 交叉验证
  I. 正则与文本高级    BRE/ERE、反向引用、字符类、sed 捕获、awk 关联数组
  J. Bash 高级语法     数组、进程替换、命名管道、trap、位运算、参数展开、严格模式
  K. find/xargs 组合   -exec 多参数、-print0/xargs -0、-P 并发、-size 过滤
  L. 退出码与错误传播  exit code 透传、信号、&&/|| 短路、两通道独立不串扰
  M. 大流量与流控      MB 级 stdout、单行超长、大 stderr、EPIPE 提前关闭

所有用例均要求输出确定性：避开时间戳、PID、内存/负载波动、磁盘使用率、
网络计数器等运行时可变字段；大输出统一用 md5sum/cksum/wc -c 归约，
不依赖终端宽度或分页行为。

三种运行模式：

  # A 上跑（设备 C 的 USB 线插在 A 上）：导出 USB 基线
    python consistency_test2.py --export baseline.json

  # A 上跑：USB 与桥同机对比
    python consistency_test2.py

  # B 上跑（跨机）：与 A 导出的 USB 基线对比，验证真实跨机路径
    python consistency_test2.py --baseline baseline.json

用法:
    python consistency_test2.py [桥地址] [--raw]
    python consistency_test2.py --export <文件>
    python consistency_test2.py --baseline <文件>
"""
import subprocess
import sys
import time

USB = "b57290249a9b3206"
BRIDGE = "172.16.0.106:15555"
RAW = False
EXPORT = None
BASELINE = None

# 手工逐个消费参数：--export/--baseline 的取值必须被跳过，
# 否则会被误判为桥地址。
_av = sys.argv[1:]
while _av:
    _a = _av.pop(0)
    if _a == "--raw":
        RAW = True
    elif _a == "--export":
        EXPORT = _av.pop(0)
    elif _a == "--baseline":
        BASELINE = _av.pop(0)
    elif _a.startswith("-"):
        pass
    else:
        BRIDGE = _a

CMDS = [
    # ---------- H. 二进制与编码边界 (1-7) ----------
    # 二进制块做摘要，验证字节级无损而非按行处理
    r"head -c 65536 /usr/bin/awk | md5sum",
    r"head -c 8192 /usr/bin/awk | cksum",
    # base64 往返：编码再解码，摘要应与原文一致
    r"head -c 3000 /usr/bin/awk | base64 | md5sum",
    r"head -c 3000 /usr/bin/awk | base64 | base64 -d | md5sum",
    # NUL 字节统计与高位字节偏移对齐（必须用 od，不能用文本工具）
    r"head -c 65536 /usr/bin/awk | od -An -tx1 | tr ' ' '\n' | grep -c '^00$'",
    r"head -c 4096 /usr/bin/awk | od -An -tx1 | tail -2",
    r"printf '\x00\x01\x7f\x80\xff\xfe' | od -An -tx1 | tr -s ' '",

    # ---------- I. 正则与文本高级处理 (8-16) ----------
    r"grep -E '^(root|daemon|www)' /etc/passwd | wc -l",
    r"printf 'aabbaabbb\n' | grep -o '\(ab\)\{2,\}' | wc -l",
    r"printf 'xyx\nabc\n' | grep -cE '^(.)\1$'",
    r"cat /etc/passwd | grep -c '[0-9]\{3\}'",
    r"sed -n 's/^\([^:]*\):.*/\1/p' /etc/group | head -6 | tr '\n' ' '; echo",
    r"awk -F: '{ if ($3>=1000) printf \"U:%s\n\", $1; else if ($7 ~ /nologin$/) printf \"N:%s\n\", $1 }' /etc/passwd | sort",
    # awk 关联数组统计行长分布
    r"awk '{c[length($0)]++} END{for (n in c) printf \"%d:%d \", n, c[n]}' /etc/passwd; echo",
    r"echo abcdef | awk '{print toupper(substr($0,2,3)), length($0), index($0,\"cd\")}'",
    # diff 补丁行数（临时文件用后即删）
    r"printf 'l1\nl2\nl3\n' > /tmp/d1.txt; printf 'l1\nX\nl3\n' > /tmp/d2.txt; diff /tmp/d1.txt /tmp/d2.txt | wc -l; rm -f /tmp/d1.txt /tmp/d2.txt",

    # ---------- J. Bash 高级语法 (17-27) ----------
    # 数组：索引取值、长度、切片
    r"bash -c 'a=(x y z w); echo ${a[2]} ${#a[@]} ${a[@]:1:2}'",
    # 关联数组 + 键遍历（顺序不定，必须 sort 后再输出）
    r"bash -c 'declare -A m; for k in c a b; do m[$k]=${#k}; done; for k in \"${!m[@]}\"; do echo $k${m[$k]}; done | sort | tr \"\\n\" \" \"; echo'",
    # 进程替换与命名管道（bash 独有能力，sh 下会失败，可验证协议层透传真实错误）
    r"bash -c 'diff <(seq 1 5) <(seq 1 6) | head -3'",
    r"bash -c 'wc -l <(printf \"a\nb\nc\n\")'",
    r"bash -c 'rm -f /tmp/np.$$; mkfifo /tmp/np.$$ && { printf \"FIFO_OK\n\" > /tmp/np.$$ & } && cat /tmp/np.$$ && rm -f /tmp/np.$$'",
    # trap 与退出码组合
    r"bash -c 'trap \"echo TRAP_EXIT\" EXIT; echo BODY; exit 3'; echo rc=$?",
    r"bash -c 'trap \"echo TRAP_TERM\" TERM; kill -TERM $$; echo AFTER'",
    # 算术与位运算
    r"bash -c 'echo $((1<<20)) $((255>>4)) $((7&3)) $((7|8)) $((7^5)) $((~0 & 255))'",
    # 参数展开全套：前缀/后缀剥离、长度、切片、全局/首尾替换
    r"bash -c 'p=/a/b/c.txt; echo ${p##*/} ${p%.*} ${p%/*} ${#p}; s=abcdef; echo ${#s} ${s:1:3} ${s//b/X} ${s/#a/A} ${s/%f/Z}'",
    # 严格模式：错误必须按语义在指定点终止，且退出码一致
    r"bash -c 'set -euo pipefail; echo A; false; echo NOT_REACHED' 2>&1; echo rc=$?",
    r"bash -c 'set -o pipefail; false | true; echo rc=$?'",

    # ---------- K. find / xargs 组合 (28-34) ----------
    r"find /etc -maxdepth 2 -name '*.conf' -exec grep -l -m1 '.' {} + 2>/dev/null | sort | md5sum",
    r"find /etc -maxdepth 2 -type f -exec sh -c 'printf \"%s\n\" \"$1\"' _ {} \; 2>/dev/null | sort | head -10 | md5sum",
    # NUL 分隔传文件名，安全处理含空格路径
    r"find /etc -maxdepth 1 -name '*.conf' -print0 2>/dev/null | xargs -0 -r ls | md5sum",
    r"find /usr/bin -maxdepth 1 -type f -size +50k 2>/dev/null | wc -l",
    r"seq 1 20 | xargs -n1 sh -c 'printf \"%s:\" $0' | md5sum",
    r"seq 1 100 | xargs -P 4 -n 1 echo | sort | md5sum",
    r"find /etc -type f 2>/dev/null | wc -l",

    # ---------- L. 退出码与错误传播 (35-41) ----------
    # 退出码透传：桥必须原样传递 device exit code
    r"sh -c 'exit 42'; echo rc=$?",
    r"bash -c 'exit 255'; echo rc=$?",
    r"true && echo AND_OK || echo AND_FAIL",
    r"false && echo AND_OK || echo AND_FAIL",
    # 两通道独立：合并与丢弃后摘要必须不同，且各自稳定（验证 stdout/stderr 不串扰）
    r"{ echo o1; echo e1 >&2; } 2>/dev/null | md5sum",
    r"{ echo o1; echo e1 >&2; } 2>&1 | md5sum",
    # 管道中某一级失败，下游仍应收到空输入而非卡死
    r"false | wc -l",

    # ---------- M. 大流量与流控 (42-50) ----------
    # MB 级 stdout：验证 256KB 流控 + OKAY 匹配 + 大块转发不截断
    r"seq 1 200000 | head -c 3000000 | md5sum",
    # 连续无换行的字节流，最考验分包边界
    r"head -c 1000000 /dev/zero | tr '\0' 'a' | wc -c",
    r"head -c 2000000 /usr/bin/awk | md5sum",
    # 单行超长（20000 字符无换行）
    r"printf 'x%.0s' $(seq 1 20000) | wc -c",
    # 大 stderr 同样走流控
    r"seq 1 20000 | sed 's/^/E/' 2>&1 | md5sum",
    # 下行提前关闭触发 EPIPE，桥不应误判为失败
    r"seq 1 100000 | head -3",
    r"yes bridge | head -5 | tr -d '\n' | wc -c",
    # 空输入
    r"printf '' | wc -c",
    # 组合压力：读 + 生成 + 大流量一次完成
    r"cat /etc/passwd | seq 1 30000 | awk 'END{print NR}'",
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


def run(target, cmd, timeout=180):
    """执行一条 shell 命令，返回 (returncode, stdout, stderr)。"""
    for attempt in (1, 2):
        try:
            p = subprocess.run(["adb", "-s", target, "shell", cmd],
                               capture_output=True, timeout=timeout)
            out = p.stdout.decode("utf-8", "replace")
            err = p.stderr.decode("utf-8", "replace")
            if attempt == 1 and ("not found" in err or "device not found" in err
                                 or "device offline" in err):
                ensure_connected(target)
                continue
            return (p.returncode, out, err)
        except subprocess.TimeoutExpired:
            return ("TIMEOUT", "", "")
        except Exception as exc:
            return ("EXC", "", str(exc))
    return ("EXC", "", "retry failed")


def norm(s):
    """跨机比较前归一化换行。

    A 上是 Rockchip ADB 31.0.2，B 上是 ADB 34.0.4，两者在 shell PTY 上对
    换行的处理不同（A 侧输出 CRLF，B 侧输出 LF）。这属于 adb 客户端版本
    差异，与桥的转发无关，因此跨机模式先归一化再比较，同时把该差异记录
    下来单独报告，避免把版本差异误判成桥缺陷。
    """
    return s.replace("\r\n", "\n").replace("\r", "\n")


def main():
    ensure_connected(BRIDGE)

    # ---- 模式 1：导出 USB 基线（在 A 上执行，设备 C 的 USB 线连在 A）----
    if EXPORT:
        import json
        print("导出 USB 基线 (%d 条) -> %s\n" % (len(CMDS), EXPORT), flush=True)
        base = []
        for i, cmd in enumerate(CMDS, 1):
            rc, out, err = run(USB, cmd)
            base.append({"idx": i, "cmd": cmd, "rc": rc,
                         "out": out, "err": err})
            print("[%2d/%d] rc=%s  %s" % (i, len(CMDS), rc, cmd[:66]), flush=True)
        with open(EXPORT, "w", encoding="utf-8") as f:
            json.dump(base, f, ensure_ascii=False, indent=1)
        print("\n基线已写入 %s" % EXPORT)
        return 0

    # ---- 模式 2：B 机跨机对比（基线来自 A 的 USB 直连）----
    if BASELINE:
        import json
        with open(BASELINE, encoding="utf-8") as f:
            base = json.load(f)
        if len(base) != len(CMDS):
            print("基线条数 %d 与本机 %d 不一致，测试集版本不同"
                  % (len(base), len(CMDS)))
            return 2
        print("跨机对比: 基线=A机USB直连   被测=B机经桥 -> %s" % BRIDGE)
        print("共 %d 条命令" % len(CMDS))
        print("注: 换行已归一化 (A=Rockchip ADB31 输出CRLF, B=ADB34 输出LF，"
              "属客户端版本差异)\n", flush=True)
        same, diff, crlf_only = 0, [], 0
        for item, cmd in zip(base, CMDS):
            if item["cmd"] != cmd:
                print("!! 第 %d 条命令与基线不匹配，请同步测试脚本" % item["idx"])
                return 2
            a = (item["rc"], item["out"], item["err"])
            b = run(BRIDGE, cmd)
            na = (a[0], norm(a[1]), norm(a[2]))
            nb = (b[0], norm(b[1]), norm(b[2]))
            if na == nb:
                same += 1
                # 严格逐字节比较：仅换行符不同则单独计数
                if a != b:
                    crlf_only += 1
                print("[%2d/%d] SAME  rc=%s  %s"
                      % (item["idx"], len(CMDS), a[0], cmd[:66]), flush=True)
            else:
                diff.append((item["idx"], cmd, a, b))
                print("[%2d/%d] DIFF  rc=%s  %s"
                      % (item["idx"], len(CMDS), a[0], cmd[:66]), flush=True)
        print("\n===== 跨机汇总: %d/%d 完全一致, %d 条不一致 ====="
              % (same, len(CMDS), len(diff)))
        if crlf_only:
            print("其中 %d 条在严格逐字节比较下仅换行符不同 (CRLF vs LF)，"
                  "内容完全相同" % crlf_only)
        for i, cmd, a, b in diff:
            print("\n--- #%d %s" % (i, cmd))
            print("    A/USB    rc=%s out=%r err=%r" % (a[0], a[1][:300], a[2][:300]))
            print("    B/BRIDGE rc=%s out=%r err=%r" % (b[0], b[1][:300], b[2][:300]))
            if a[0] != b[0]:
                print("    >> 退出码不同：A=%s B=%s" % (a[0], b[0]))
            if a[1] != b[1]:
                print("    >> stdout 不同：A=%d字节 B=%d字节" % (len(a[1]), len(b[1])))
            if a[2] != b[2]:
                print("    >> stderr 不同：A=%d字节 B=%d字节" % (len(a[2]), len(b[2])))
        return 0 if not diff else 1

    # ---- 默认模式：同机 USB vs 桥 ----
    print("对比目标: USB直连=%s   桥=%s" % (USB, BRIDGE))
    print("共 %d 条命令 (第二批扩展)  raw=%s\n"
          % (len(CMDS), RAW), flush=True)

    same, diff = 0, []
    for i, cmd in enumerate(CMDS, 1):
        a = run(USB, cmd)
        b = run(BRIDGE, cmd)
        ok = (a == b)
        if ok:
            same += 1
            tag = "SAME"
        else:
            tag = "DIFF"
            diff.append((i, cmd, a, b))
        print("[%2d/%d] %s  rc=%s  %s"
              % (i, len(CMDS), tag, a[0], cmd[:70]), flush=True)
        if RAW and ok:
            print("        out=%r" % (a[1][:160],))

    print("\n===== 第二批汇总: %d/%d 完全一致, %d 条不一致 ====="
          % (same, len(CMDS), len(diff)))
    for i, cmd, a, b in diff:
        print("\n--- #%d %s" % (i, cmd))
        print("    USB    rc=%s out=%r err=%r" % (a[0], a[1][:300], a[2][:300]))
        print("    BRIDGE rc=%s out=%r err=%r" % (b[0], b[1][:300], b[2][:300]))
        if a[0] != b[0]:
            print("    >> 退出码不同：USB=%s BRIDGE=%s" % (a[0], b[0]))
        if a[1] != b[1]:
            print("    >> stdout 不同：USB=%d字节 BRIDGE=%d字节"
                  % (len(a[1]), len(b[1])))
        if a[2] != b[2]:
            print("    >> stderr 不同：USB=%d字节 BRIDGE=%d字节"
                  % (len(a[2]), len(b[2])))
    return 0 if not diff else 1


if __name__ == "__main__":
    sys.exit(main())