#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""在电脑 B 上跑 20 分钟长时稳定性测试。

链路：电脑 B --TCP-->电脑 A 的桥 --USB--> 设备 C --ICMP--> www.baidu.com

ping 是在**设备 C 上**跑的（B 通过 adb shell 触发），所以一次往返要经过
「B→桥→A→USB→设备→外网→回来→设备→USB→A→桥→B」整条链路，
任何一段卡顿都会体现在 RTT 上。

期间每秒采样一次 RTT，并每60 秒做一次通道活性检查（get-state + 一条 shell
命令），最后统计丢包率、RTT 分位数与最大延迟。

用法: python ping_stability_b.py [目标串] [持续秒数]
"""
import subprocess
import sys
import threading
import time

TARGET = sys.argv[1] if len(sys.argv) > 1 else "172.16.0.106:15555"
DURATION = int(sys.argv[2]) if len(sys.argv) > 2 else 1200
HOST = "www.baidu.com"


def run(args, timeout=30):
    p = subprocess.run(args, capture_output=True, timeout=timeout)
    return (p.returncode,
            p.stdout.decode("utf-8", "replace").strip(),
            p.stderr.decode("utf-8", "replace").strip())


def pct(sorted_list, p):
    """取第p 百分位（输入必须已排序）。"""
    if not sorted_list:
        return 0.0
    k = (len(sorted_list) - 1) * p / 100.0
    lo = int(k)
    hi = min(lo + 1, len(sorted_list) - 1)
    return sorted_list[lo] + (sorted_list[hi] - sorted_list[lo]) * (k - lo)


def main():
    print("=" * 74)
    print("长时稳定性测试：%s 持续 %d 分钟" % (TARGET, DURATION // 60))
    print("链路：电脑 B --TCP--> A 的桥 --USB--> 设备 C --ICMP--> %s" % HOST)
    print("=" * 74)

    # 开跑前先确认连上了
    run(["adb", "disconnect", TARGET], timeout=20)
    for _ in range(20):
        time.sleep(0.5)
        _rc, out, _ = run(["adb", "devices"], timeout=20)
        if TARGET not in out:
            break
    rc, out, _ = run(["adb", "connect", TARGET], timeout=30)
    for _ in range(40):
        time.sleep(0.5)
        _rc, s, _ = run(["adb", "-s", TARGET, "get-state"], timeout=20)
        if s.strip() == "device":
            break
    print("连接状态: %s" % ("device" if s.strip() == "device" else "未就绪(%s)" % s))
    if s.strip() != "device":
        print("设备未就绪，测试中止")
        return 1
    print()

    # 后台跑持续 ping。用 -c 指定次数让它**自己跑完并输出汇总** ——
    # 早先版本是靠 terminate 掐掉的，设备端的 "N packets transmitted"
    # 汇总行就永远拿不到，只能自己算丢包，权威性差一截。
    proc = subprocess.Popen(
        ["adb", "-s", TARGET, "shell",
         "ping -i 1 -c %d %s" % (DURATION, HOST)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        bufsize=1, universal_newlines=True)

    samples = []      # (相对秒, rtt_ms)
    state = {"fail": 0}

    def reader():
        """实时读 ping 的输出，把每个包的 RTT 记下来。
        ⚠️ 必须用线程读：如果主线程不消费 pipe，缓冲区写满后 ping 会卡住，
        测试就变成「测缓冲」而不是「测桥」了。
        """
        t0 = time.time()
        for line in proc.stdout:
            line = line.replace("\r", "").strip()
            if "time=" in line:
                try:
                    rtt = float(line.split("time=")[1].split("ms")[0])
                    samples.append((time.time() - t0, rtt))
                except (IndexError, ValueError):
                    pass
            elif "packets transmitted" in line:
                state["summary"] = line

    th = threading.Thread(target=reader, daemon=True)
    th.start()

    t0 = time.time()
    last = 0
    print("%-8s %-8s %-10s %-10s %-10s %s" %
          ("已过", "发包", "最近RTT", "最大RTT", "平均RTT", "通道活性"))
    print("-" * 74)

    while True:
        time.sleep(2)
        elapsed = int(time.time() - t0)
        # 先打心跳再判断退出 —— 否则最后一分钟的检查会被break 吃掉
        if elapsed - last >= 60 or elapsed >= DURATION:
            last = elapsed
            # run() 返回的是 (返回码, stdout, stderr) —— 第三个是 stderr 不是
            # 耗时，别直接拿去 %.2fs 格式化（会抛 TypeError）。耗时自己计。
            _t = time.time()
            rc1, st, _ = run(["adb", "-s", TARGET, "get-state"], timeout=20)
            rc2, o2, _ = run(["adb", "-s", TARGET, "shell",
                              "echo alive-%d" % elapsed], timeout=20)
            dt2 = time.time() - _t
            alive = "OK(%.2fs)" % dt2 if "alive-%d" % elapsed in o2 \
                else "FAIL(%s)" % (o2.replace("\n", " ") or st)
            if alive.startswith("FAIL"):
                state["fail"] += 1
            rtts = [r for _, r in samples]
            avg = sum(rtts) / len(rtts) if rtts else 0
            print("%-8s %-8d %-10.1f %-10.1f %-10.1f %s" %
                  ("%dmin%02ds" % (elapsed // 60, elapsed % 60),
                   len(samples), rtts[-1] if rtts else 0,
                   max(rtts) if rtts else 0, avg, alive))
        # ping 自己跑完（正常结束）或超时（异常）都收工
        if proc.poll() is not None or elapsed >= DURATION + 60:
            break

    # 收尾：等 ping 自己结束（它有 -c 次数，会自己打汇总行）
    try:
        proc.wait(timeout=15)
    except Exception:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
    # 读干净管道残留（reader 线程还在收尾，给它一点时间）
    th.join(timeout=3)

    print("-" * 74)
    summary = state.get("summary", "")
    print("设备端 ping 汇总: %s" % (summary or "（未拿到汇总行）"))

    rtts = sorted(r for _, r in samples)
    total = len(samples)
    # ping 每秒一发，运行 DURATION 秒，理论发送数≈ DURATION
    expected = DURATION
    lost = max(expected - total, 0)

    print()
    print("【统计】")
    print("  持续时长     : %d 分钟 %d 秒" % (DURATION // 60, DURATION % 60))
    print("  收到响应     : %d 包" % total)
    print("  估算丢包     : %d 包 (%.2f%%)" % (lost, lost * 100.0 / max(expected, 1)))
    if rtts:
        print("  RTT 最小/平均: %.2f / %.2f ms" % (rtts[0], sum(rtts) / len(rtts)))
        print("  RTT 中位数   : %.2f ms (P50)" % pct(rtts, 50))
        print("  RTT P95      : %.2f ms" % pct(rtts, 95))
        print("  RTT P99      : %.2f ms" % pct(rtts, 99))
        print("  RTT 最大     : %.2f ms" % rtts[-1])
        # 抖动：相邻采样差值的最大值，桥卡顿时会飙升
        seq = [r for _, r in sorted(samples)]
        jitter = max((abs(seq[i] - seq[i - 1]) for i in range(1, len(seq))),
                     default=0)
        print("  相邻最大跳变 : %.2f ms" % jitter)
    print("  通道检查失败 : %d 次（共 %d 次检查）"
          % (state["fail"], DURATION // 60))
    print()
    ok = total > 0 and state["fail"] == 0
    print("结论: %s" % ("通道全程可用，20 分钟无中断" if ok
                        else "存在异常，见上面的失败计数"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())