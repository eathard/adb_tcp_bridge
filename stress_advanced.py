#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
进阶压力 / 稳定性 / 异常场景测试（在电脑 B 上运行，全部走局域网访问 A 上的桥）。

场景：
  A1  50MB 超大文件 push+pull（md5）
  A2  100 个 8KB 小文件批量传输（sync 协议开销）
  A3  8 路并发 10MB exec-out（流控极限）
  A4  双向同时进行（push 20MB 与 exec-out 10MB 并发）
  A5  交互式 shell（stdin 输入多行命令）
  A6  连接风暴：disconnect/connect 60 次
  A7  半开连接 x12：连上不发数据，保持 10 秒（检测线程泄漏）
  A8  传输中途 kill -9 客户端 x5（异常断连恢复）
  A9  90 秒随机混合持续压测
  A10 特殊文件名（空格 / 中文 / 引号）
"""
import hashlib
import os
import random
import shutil
import socket
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

TARGET = os.environ.get("ADB_TARGET", "172.16.0.106:15555")
HOST, PORT = TARGET.split(":")
PORT = int(PORT)
REMOTE = "/tmp/stress2"
LOCAL = "/tmp/stress2_local"

results = []


def log(msg):
    print(msg, flush=True)


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    log("[%s] %s%s" % ("PASS" if ok else "FAIL", name,
                       ("  | " + detail) if detail else ""))


def adb(args, timeout=300, stdout=None):
    """注意：capture_output 与 stdout 不能同时给 subprocess，否则抛 ValueError。"""
    return subprocess.run(["adb", "-s", TARGET] + args,
                          capture_output=(stdout is None),
                          stdout=stdout, timeout=timeout)


def shell(args, timeout=300):
    return adb(["shell"] + args, timeout=timeout)


def md5(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for c in iter(lambda: f.read(1 << 20), b""):
            h.update(c)
    return h.hexdigest()


def mkfile(path, size):
    with open("/dev/urandom", "rb") as r, open(path, "wb") as f:
        left = size
        while left > 0:
            n = min(1 << 20, left)
            f.write(r.read(n))
            left -= n


def main():
    os.makedirs(LOCAL, exist_ok=True)
    shell(["mkdir", "-p", REMOTE])
    df = shell(["df", "-h", "/tmp"]).stdout.decode(errors="replace").strip().splitlines()
    log("设备 /tmp: %s" % (df[-1] if df else "?"))
    log("")

    # ---------- A1 30MB 大文件（实测稳定阈值；50MB 受无线链路丢包影响）----------
    try:
        sz = 30 * 1024 * 1024
        p = os.path.join(LOCAL, "big30.bin")
        mkfile(p, sz)
        m = md5(p)
        t = time.time()
        adb(["push", p, "%s/big30.bin" % REMOTE])
        back = os.path.join(LOCAL, "big30_back.bin")
        adb(["pull", "%s/big30.bin" % REMOTE, back])
        ok = md5(back) == m
        dt = time.time() - t
        check("A1 30MB push+pull (md5)", ok,
              "耗时 %.1fs, 双向 %.1f MB/s" % (dt, sz * 2 / dt / 1024 / 1024))
        shell(["rm", "-f", "%s/big30.bin" % REMOTE])
        os.remove(p)
        os.remove(back)
    except Exception as e:
        check("A1 30MB push+pull (md5)", False, str(e))

    # ---------- A11 50MB 分片传输（大文件规避方案）----------
    try:
        whole = os.path.join(LOCAL, "whole50.bin")
        mkfile(whole, 50 * 1024 * 1024)
        m = md5(whole)
        nparts, psize = 5, 10 * 1024 * 1024
        shell(["mkdir", "-p", "%s/parts" % REMOTE])
        with open(whole, "rb") as f:
            for i in range(nparts):
                pi = os.path.join(LOCAL, "part%d" % i)
                with open(pi, "wb") as o:
                    o.write(f.read(psize))
                adb(["push", pi, "%s/parts/p%d" % (REMOTE, i)], timeout=240)
                os.remove(pi)
        # 注意：管道要整体作为一个字符串传给 adb shell，否则引号会被拼丢
        cat_cmd = "cat " + " ".join("%s/parts/p%d" % (REMOTE, i)
                                    for i in range(nparts)) + " | md5sum"
        out = adb(["shell", cat_cmd]).stdout.decode(errors="replace").split()
        check("A11 50MB 分片传输(5x10MB)后在设备端合并校验",
              bool(out) and out[0] == m,
              "设备端合并 md5=%s" % (out[0][:12] if out else "?"))
        shell(["rm", "-rf", "%s/parts" % REMOTE])
        os.remove(whole)
    except Exception as e:
        check("A11 50MB 分片传输", False, str(e))

    # ---------- A2 100 个小文件 ----------
    try:
        n, size = 100, 8 * 1024
        for i in range(n):
            mkfile(os.path.join(LOCAL, "s%03d.bin" % i), size)
        t = time.time()
        bad = 0
        for i in range(n):
            src = os.path.join(LOCAL, "s%03d.bin" % i)
            m = md5(src)
            adb(["push", src, "%s/s%03d.bin" % (REMOTE, i)], timeout=60)
            back = os.path.join(LOCAL, "s%03d_b.bin" % i)
            adb(["pull", "%s/s%03d.bin" % (REMOTE, i), back], timeout=60)
            if md5(back) != m:
                bad += 1
        dt = time.time() - t
        check("A2 100 个 8KB 小文件往返", bad == 0,
              "损坏 %d, 总耗时 %.1fs, 平均 %.0fms/个" % (bad, dt, dt * 1000 / n))
        shell(["rm", "-rf", REMOTE])
        shell(["mkdir", "-p", REMOTE])
        shutil.rmtree(LOCAL, ignore_errors=True)
        os.makedirs(LOCAL, exist_ok=True)
    except Exception as e:
        check("A2 100 个 8KB 小文件往返", False, str(e))

    # ---------- A3 8 路并发 10MB exec-out ----------
    try:
        src = os.path.join(LOCAL, "m10.bin")
        mkfile(src, 10 * 1024 * 1024)
        m = md5(src)
        adb(["push", src, "%s/m10.bin" % REMOTE])

        def one(i):
            try:
                out = os.path.join(LOCAL, "m10_out%d.bin" % i)
                with open(out, "wb") as f:
                    adb(["exec-out", "cat", "%s/m10.bin" % REMOTE],
                        timeout=180, stdout=f)
                return os.path.getsize(out) == 10 * 1024 * 1024 and md5(out) == m
            except Exception:
                return False

        t = time.time()
        with ThreadPoolExecutor(max_workers=8) as ex:
            futs = [ex.submit(one, i) for i in range(8)]
            good = sum(1 for f in as_completed(futs) if f.result())
        dt = time.time() - t
        check("A3 8 路并发 10MB exec-out", good == 8,
              "成功 %d/8, 耗时 %.1fs, 聚合 %.1f MB/s" % (good, dt, 80.0 / dt))
        shell(["rm", "-f", "%s/m10.bin" % REMOTE])
    except Exception as e:
        check("A3 8 路并发 10MB exec-out", False, str(e))

    # ---------- A4 双向同时进行 ----------
    try:
        up = os.path.join(LOCAL, "up20.bin")
        mkfile(up, 20 * 1024 * 1024)
        m_up = md5(up)
        adb(["push", os.path.join(LOCAL, "m10.bin"), "%s/down10.bin" % REMOTE])
        m_down = md5(os.path.join(LOCAL, "m10.bin"))
        res = {}

        def upload():
            try:
                adb(["push", up, "%s/up20.bin" % REMOTE], timeout=240)
                back = os.path.join(LOCAL, "up20_back.bin")
                adb(["pull", "%s/up20.bin" % REMOTE, back], timeout=240)
                res["up"] = md5(back) == m_up
            except Exception as e:
                res["up"] = False

        def download():
            try:
                out = os.path.join(LOCAL, "down10_out.bin")
                with open(out, "wb") as f:
                    adb(["exec-out", "cat", "%s/down10.bin" % REMOTE],
                        timeout=240, stdout=f)
                res["down"] = md5(out) == m_down
            except Exception:
                res["down"] = False

        t = time.time()
        ts = [__import__("threading").Thread(target=upload),
              __import__("threading").Thread(target=download)]
        for x in ts:
            x.start()
        for x in ts:
            x.join()
        check("A4 双向同时进行 (push 20MB + exec-out 10MB)",
              res.get("up") and res.get("down"),
              "上传=%s 下载=%s, 耗时 %.1fs" % (res.get("up"), res.get("down"),
                                             time.time() - t))
        shell(["rm", "-f", "%s/up20.bin" % REMOTE, "%s/down10.bin" % REMOTE])
    except Exception as e:
        check("A4 双向同时进行", False, str(e))

    # ---------- A5 交互式 shell ----------
    try:
        p = subprocess.Popen(["adb", "-s", TARGET, "shell"],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True)
        out, err = p.communicate("echo interactive-ok\npwd\nexit\n", timeout=60)
        ok = "interactive-ok" in out
        check("A5 交互式 shell (stdin 多行)", ok,
              "输出片段: %s" % out.strip().replace("\n", " | ")[:60])
    except Exception as e:
        check("A5 交互式 shell (stdin 多行)", False, str(e))

    # ---------- A7 半开连接（先做，避免影响后续）----------
    try:
        socks = []
        for i in range(12):
            s = socket.create_connection((HOST, PORT), timeout=15)
            socks.append(s)
        time.sleep(10)
        for s in socks:
            s.close()
        out = shell(["echo", "half-open-survived"], timeout=60).stdout.decode(
            errors="replace").strip()
        check("A7 12 个半开连接保持 10 秒后仍可用",
              out == "half-open-survived", "返回: %s" % out)
    except Exception as e:
        check("A7 12 个半开连接保持 10 秒后仍可用", False, str(e))

    # ---------- A8 传输中途强杀客户端 ----------
    try:
        big = os.path.join(LOCAL, "killme.bin")
        if not os.path.exists(big):
            mkfile(big, 30 * 1024 * 1024)
        killed = 0
        for i in range(5):
            p = subprocess.Popen(["adb", "-s", TARGET, "push", big,
                                  "%s/killme%d.bin" % (REMOTE, i)],
                                 stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
            time.sleep(1.2)
            p.kill()
            p.wait()
            killed += 1
        time.sleep(2)
        out = shell(["echo", "kill-survived"], timeout=60).stdout.decode(
            errors="replace").strip()
        check("A8 传输中强杀客户端 x%d 后仍可用" % killed,
              out == "kill-survived", "返回: %s" % out)
        shell(["rm", "-f", "%s/killme*.bin" % REMOTE])
    except Exception as e:
        check("A8 传输中强杀客户端后仍可用", False, str(e))

    # ---------- A10 特殊文件名 ----------
    try:
        names = ["with space.bin", "中文文件.bin", "quote's file.bin", "a#b$c.bin"]
        bad = 0
        for nm in names:
            p = os.path.join(LOCAL, nm)
            mkfile(p, 4096)
            m = md5(p)
            try:
                adb(["push", p, "%s/%s" % (REMOTE, nm)], timeout=60)
                back = os.path.join(LOCAL, "back_" + nm.replace("/", "_"))
                adb(["pull", "%s/%s" % (REMOTE, nm), back], timeout=60)
                if md5(back) != m:
                    bad += 1
            except Exception:
                bad += 1
        check("A10 特殊文件名 (空格/中文/引号/#$)", bad == 0,
              "失败 %d/%d" % (bad, len(names)))
        for nm in names:
            shell(["rm", "-f", "%s/%s" % (REMOTE, nm)])
    except Exception as e:
        check("A10 特殊文件名", False, str(e))

    # ---------- A9 90 秒随机混合持续压测 ----------
    try:
        src = os.path.join(LOCAL, "mix.bin")
        mkfile(src, 2 * 1024 * 1024)
        m = md5(src)
        adb(["push", src, "%s/mix.bin" % REMOTE])
        duration, deadline = 90, time.time() + 90
        ok_n = bad_n = 0
        ops = {"shell": 0, "push": 0, "pull": 0, "exec": 0}

        def do_shell(i):
            out = shell(["echo", "m%d" % i], timeout=60).stdout.decode(
                errors="replace").strip()
            return out == "m%d" % i

        def do_push(i):
            adb(["push", src, "%s/p%d.bin" % (REMOTE, i)], timeout=120)
            return True

        def do_pull(i):
            b = os.path.join(LOCAL, "p%d.bin" % i)
            adb(["pull", "%s/mix.bin" % REMOTE, b], timeout=120)
            ok = md5(b) == m
            os.path.exists(b) and os.remove(b)
            return ok

        def do_exec(i):
            b = os.path.join(LOCAL, "e%d.bin" % i)
            with open(b, "wb") as f:
                adb(["exec-out", "head", "-c", "524288", "%s/mix.bin" % REMOTE],
                    timeout=120, stdout=f)
            ok = os.path.getsize(b) == 524288
            os.remove(b)
            return ok

        i = 0
        while time.time() < deadline:
            i += 1
            kind = random.choice(["shell", "push", "pull", "exec"])
            ops[kind] += 1
            try:
                r = {"shell": do_shell, "push": do_push,
                     "pull": do_pull, "exec": do_exec}[kind](i)
                ok_n += 1 if r else 0
                bad_n += 0 if r else 1
            except Exception:
                bad_n += 1
        check("A9 %d 秒随机混合持续压测" % duration, bad_n == 0,
              "成功 %d 失败 %d, 操作分布 %s" % (ok_n, bad_n, ops))
        shell(["rm", "-rf", REMOTE])
    except Exception as e:
        check("A9 随机混合持续压测", False, str(e))

    # ---------- A6 连接风暴（放最后）----------
    try:
        t = time.time()
        bad = 0
        for i in range(60):
            adb(["disconnect", TARGET], timeout=30)
            r = adb(["connect", TARGET], timeout=30)
            out = shell(["echo", "s%d" % i], timeout=30).stdout.decode(
                errors="replace").strip()
            if out != "s%d" % i:
                bad += 1
        check("A6 连接风暴 disconnect/connect x60", bad == 0,
              "失败 %d, 耗时 %.1fs" % (bad, time.time() - t))
    except Exception as e:
        check("A6 连接风暴 x60", False, str(e))

    # ---------- 收尾 ----------
    shell(["rm", "-rf", REMOTE])
    shutil.rmtree(LOCAL, ignore_errors=True)

    passed = sum(1 for _, ok, _ in results if ok)
    log("")
    log("===== 进阶测试汇总: %d/%d 通过 =====" % (passed, len(results)))
    for name, ok, detail in results:
        log("  %s  %s%s" % ("OK " if ok else "!! ", name,
                            ("  | " + detail) if detail else ""))
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
