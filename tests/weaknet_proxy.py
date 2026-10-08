#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
弱网代理（在电脑 A 上运行）：监听一个端口，转发到真正的 adb 桥，
在转发过程中注入延迟 / 抖动 / 限速 / 随机停顿 / 随机断连，用来模拟弱网。

之所以不用 tc netem：B 上 sudo 需要密码。本代理在应用层模拟弱网的主要
特征（延迟、抖动、带宽受限、丢包导致的停顿、连接中断），不需要 root。

用法：
  python weaknet_proxy.py --listen 16666 --target 15555 \
      --delay-ms 100 --jitter-ms 30 --rate-kbps 800 \
      --stall-prob 0.02 --stall-ms 300
"""
import argparse
import random
import socket
import sys
import threading
import time

stop_flag = False


def log(msg):
    print(msg, flush=True)


class Cfg(object):
    pass


def pump(src, dst, cfg, tag):
    try:
        while True:
            data = src.recv(65536)
            if not data:
                break
            if stop_flag:
                break
            wait = cfg.delay_ms + random.uniform(0, cfg.jitter_ms)
            if random.random() < cfg.stall_prob:
                wait += cfg.stall_ms
            if wait:
                time.sleep(wait / 1000.0)
            if cfg.rate_kbps > 0:
                time.sleep(len(data) * 8 / (cfg.rate_kbps * 1000.0))
            dst.sendall(data)
    except Exception:
        pass
    finally:
        try:
            dst.shutdown(socket.SHUT_RDWR)
        except Exception:
            pass


def handle(client, cfg):
    start = time.time()
    try:
        up = socket.create_connection((cfg.target_host, cfg.target_port), timeout=10)
    except Exception as exc:
        log("无法连接后端桥: %s" % exc)
        client.close()
        return
    ts = [threading.Thread(target=pump, args=(client, up, cfg, "up"), daemon=True),
          threading.Thread(target=pump, args=(up, client, cfg, "down"), daemon=True)]
    for t in ts:
        t.start()
    if cfg.drop_after > 0:
        # 模拟连接被中断
        def killer():
            time.sleep(cfg.drop_after)
            try:
                client.close()
            except Exception:
                pass
            try:
                up.close()
            except Exception:
                pass
            log("按配置中断了一条连接 (drop_after=%.1fs)" % cfg.drop_after)
        threading.Thread(target=killer, daemon=True).start()
    for t in ts:
        t.join()
    log("连接结束 %.1fs" % (time.time() - start))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--listen", type=int, default=16666)
    ap.add_argument("--target", type=int, default=15555)
    ap.add_argument("--target-host", default="127.0.0.1")
    ap.add_argument("--delay-ms", type=float, default=0.0)
    ap.add_argument("--jitter-ms", type=float, default=0.0)
    ap.add_argument("--rate-kbps", type=float, default=0.0, help="0 表示不限速")
    ap.add_argument("--stall-prob", type=float, default=0.0)
    ap.add_argument("--stall-ms", type=float, default=200.0)
    ap.add_argument("--drop-after", type=float, default=0.0, help=">0 时在该秒数后断开连接")
    args = ap.parse_args()

    cfg = Cfg()
    cfg.target_host = args.target_host
    cfg.target_port = args.target
    cfg.delay_ms = args.delay_ms
    cfg.jitter_ms = args.jitter_ms
    cfg.rate_kbps = args.rate_kbps
    cfg.stall_prob = args.stall_prob
    cfg.stall_ms = args.stall_ms
    cfg.drop_after = args.drop_after

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("0.0.0.0", args.listen))
    srv.listen(32)
    log("弱网代理已启动: 0.0.0.0:%d -> %s:%d | delay=%.0fms jitter=%.0fms "
        "rate=%.0fkbps stall=%.2f%%/%0.fms drop_after=%.1fs"
        % (args.listen, cfg.target_host, cfg.target_port, cfg.delay_ms,
           cfg.jitter_ms, cfg.rate_kbps, cfg.stall_prob * 100, cfg.stall_ms,
           cfg.drop_after))
    try:
        while not stop_flag:
            try:
                client, _ = srv.accept()
            except Exception:
                break
            threading.Thread(target=handle, args=(client, cfg), daemon=True).start()
    except KeyboardInterrupt:
        pass
    finally:
        srv.close()


if __name__ == "__main__":
    main()
