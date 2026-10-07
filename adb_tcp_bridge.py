#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
adb TCP bridge —— 在电脑 A 上运行，把本机 USB 设备 C 暴露成标准 TCP adb 设备。

背景：设备 C（Buildroot / Luckfox 等嵌入式板）的 adbd 无法开启 TCP 监听
      （adb tcpip 不生效），但它通过 USB 挂在本机 adb server 上。
      本桥在 A 上监听 TCP 端口，做 adb 协议转换，使电脑 B 只需：
          adb connect <A的局域网IP>:5555
      即可像普通网络设备一样使用 shell / push / pull / logcat。

协议转换：
  B 侧  : 完整 adb 数据流（CNXN / OPEN / WRTE / OKAY / CLSE，带 24 字节包头）
  A 侧  : 本机 adb server 的 smart socket（host:transport:<serial> + "shell:xxx"）
          —— 每个 stream 对应一条到 server 的连接，桥负责多路复用与流控。
"""
import argparse
import socket
import struct
import subprocess
import sys
import queue
import threading
import time
from collections import deque

A_CNXN = 0x4E584E43
A_OPEN = 0x4E45504F
A_OKAY = 0x59414B4F
A_CLSE = 0x45534C43
A_WRTE = 0x45545257

CMD_NAME = {A_CNXN: "CNXN", A_OPEN: "OPEN", A_OKAY: "OKAY",
            A_CLSE: "CLSE", A_WRTE: "WRTE"}
DEBUG_PACKETS = False


def cmd_name(cmd):
    return CMD_NAME.get(cmd, "0x%08x" % cmd)

VERSION = 0x01000000
MAXDATA = 1048576
WINDOW = 256 * 1024
HEADER = struct.Struct("<6I")
# 客户端空闲超时：防止"连上却不发数据"的半开连接永久占用线程
IDLE_TIMEOUT = 600


def log(msg):
    print("[%s] %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


def adb(args):
    try:
        return subprocess.run(["adb"] + args, capture_output=True, text=True,
                              timeout=15).stdout
    except Exception as exc:
        log("adb 调用失败: %s" % exc)
        return ""


def list_devices():
    out = adb(["devices"])
    res = []
    for line in out.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "device":
            res.append(parts[0])
    return res


def pack(cmd, arg0, arg1, payload=b""):
    return HEADER.pack(cmd, arg0, arg1, len(payload),
                       sum(payload) & 0xFFFFFFFF, cmd ^ 0xFFFFFFFF) + payload


def read_exact(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("closed")
        buf += chunk
    return buf


def send_server_cmd(sock, cmd):
    if isinstance(cmd, str):
        cmd = cmd.encode()
    sock.sendall(("%04x" % len(cmd)).encode() + cmd)


def server_alive(server_addr):
    try:
        s = socket.create_connection(server_addr, timeout=2)
        s.close()
        return True
    except Exception:
        return False


def ensure_server(server_addr, quiet=False):
    """确认本机 adb server 在跑；不在就拉起来（有些环境下 server 会被回收）。"""
    if server_alive(server_addr):
        return True
    if not quiet:
        log("adb server 不可达，正在拉起 adb start-server ...")
    adb(["start-server"])
    for _ in range(10):
        time.sleep(0.5)
        if server_alive(server_addr):
            if not quiet:
                log("adb server 已恢复")
            return True
    return False


def watchdog(server_addr, interval=10):
    """常驻守护：定期确认 adb server 存活，断了就拉起。"""
    while True:
        time.sleep(interval)
        try:
            ensure_server(server_addr)
        except Exception:
            pass


def query_simple(serial, server_addr, what):
    """向 adb server 查询设备属性（get-product / get-serialno 等）。"""
    try:
        s = socket.create_connection(server_addr, timeout=5)
        send_server_cmd(s, "host-serial:%s:%s" % (serial, what))
        if read_exact(s, 4) != b"OKAY":
            s.close()
            return ""
        size = int(read_exact(s, 4), 16)
        payload = read_exact(s, size).decode(errors="replace")
        s.close()
        return payload
    except Exception:
        return ""


def query_features(serial, server_addr):
    try:
        s = socket.create_connection(server_addr, timeout=5)
        send_server_cmd(s, "host-serial:%s:features" % serial)
        if read_exact(s, 4) != b"OKAY":
            s.close()
            return ""
        size = int(read_exact(s, 4), 16)
        payload = read_exact(s, size).decode(errors="replace")
        s.close()
        return payload
    except Exception:
        return ""


class Stream(object):
    __slots__ = ("sock", "local_id", "our_id", "unacked", "pending", "cond",
                 "wq", "up", "down", "service", "last_active")

    def __init__(self, sock, local_id, our_id):
        self.sock = sock
        self.local_id = local_id
        self.our_id = our_id
        self.unacked = 0
        self.pending = deque()
        self.cond = threading.Condition()
        self.up = 0
        self.down = 0
        self.service = ""
        self.last_active = time.time()
        # 上行队列：主线程只入队，由 stream_writer 负责真正的 sendall，
        # 避免主线程阻塞在 sendall 上而无法转发设备的响应（大文件传输会卡死）。
        self.wq = queue.Queue()


class Session(object):
    def __init__(self, client, serial, server_addr, features, product="",
                 upstream_keepalive=False, enable_heartbeat=False):
        self.client = client
        self.serial = serial
        self.server_addr = server_addr
        self.features = features
        self.product = product
        self.upstream_keepalive = upstream_keepalive
        self.enable_heartbeat = enable_heartbeat
        self.streams = {}
        self.lock = threading.Lock()
        self.send_lock = threading.Lock()
        self.closed = False
        self.peer = client.getpeername()[0]
        self.next_id = 0x10000
        try:
            client.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            client.settimeout(IDLE_TIMEOUT)
        except Exception:
            pass

    # ---------- 基础收发 ----------
    def send(self, cmd, arg0, arg1, payload=b""):
        if self.closed:
            return
        if DEBUG_PACKETS:
            log("  → %-4s a0=%-6d a1=%-6d len=%d" %
                (cmd_name(cmd), arg0, arg1, len(payload)))
        try:
            with self.send_lock:
                self.client.sendall(pack(cmd, arg0, arg1, payload))
        except Exception:
            self.close()

    def close(self):
        if self.closed:
            return
        self.closed = True
        with self.lock:
            streams = list(self.streams.values())
            self.streams.clear()
        for st in streams:
            try:
                st.wq.put(None)
            except Exception:
                pass
            try:
                st.sock.close()
            except Exception:
                pass
        try:
            self.client.close()
        except Exception:
            pass
        log("%s 断开" % self.peer)

    def new_our_id(self):
        self.next_id += 1
        return self.next_id

    # ---------- stream 处理 ----------
    def on_open(self, local_id, service):
        if not server_alive(self.server_addr):
            ensure_server(self.server_addr)
        try:
            up = socket.create_connection(self.server_addr, timeout=10)
            send_server_cmd(up, "host:transport:" + self.serial)
            if read_exact(up, 4) != b"OKAY":
                up.close()
                self.send(A_CLSE, 0, local_id)
                return
            send_server_cmd(up, service)
            resp = read_exact(up, 4)
            if resp != b"OKAY":
                size = int(read_exact(up, 4), 16)
                read_exact(up, size)
                up.close()
                self.send(A_CLSE, 0, local_id)
                log("stream 被拒: %s" % service.decode(errors="replace"))
                return
        except Exception as exc:
            log("建立 stream 失败: %s" % exc)
            self.send(A_CLSE, 0, local_id)
            return

        try:
            # 关键：create_connection 的 timeout 会被保留到 socket 上，之后所有
            # recv 都会在超时后抛 socket.timeout。设备端执行超过约 10 秒无输出的
            # 命令（如 sleep 12、扫大文件）时，这里会静默超时断开——曾被误判为
            # "adb server 的硬超时"。建连后必须把超时恢复为阻塞模式。
            up.settimeout(None)
            up.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            up.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1 << 20)
            up.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 20)
        except Exception:
            pass
        st = Stream(up, local_id, self.new_our_id())
        st.service = service.decode(errors="replace")
        with self.lock:
            self.streams[local_id] = st
        threading.Thread(target=self.stream_reader, args=(st,), daemon=True).start()
        threading.Thread(target=self.stream_writer, args=(st,), daemon=True).start()
        self.send(A_OKAY, st.our_id, local_id)
        log("%s stream#%d -> %s" % (self.peer, local_id,
                                    service.decode(errors="replace")[:40]))

    def on_write(self, local_id, data):
        with self.lock:
            st = self.streams.get(local_id)
        if not st or self.closed:
            return
        # 只入队，不在这里 sendall；发送成功后由 stream_writer 回 OKAY（背压）
        st.wq.put(data)
        st.last_active = time.time()

    def on_okay(self, arg0, arg1):
        # OKAY 的 arg0/arg1 分别是双方的 local id，两个都试，避免匹配不到而误判拥塞
        with self.lock:
            st = self.streams.get(arg1) or self.streams.get(arg0)
        if not st:
            return
        with st.cond:
            if st.pending:
                st.unacked -= st.pending.popleft()
            st.cond.notify_all()

    def on_close(self, local_id):
        log("stream#%d 收到客户端 CLSE" % local_id)
        with self.lock:
            st = self.streams.pop(local_id, None)
        if st:
            self.drop_stream(st, notify=False)

    def drop_stream(self, st, notify=True):
        try:
            st.sock.close()
        except Exception:
            pass
        try:
            st.wq.put(None)
        except Exception:
            pass
        with self.lock:
            self.streams.pop(st.local_id, None)
        log("stream#%d 关闭 [%s] 上行=%dB 下行=%dB" %
            (st.local_id, st.service[:24], st.up, st.down))
        if notify:
            self.send(A_CLSE, st.our_id, st.local_id)

    def heartbeat(self):
        """心跳（**默认不启用，已证实有害**）。

        历史教训：这个超时问题曾被误判为"adb server 对 TCP transport 的硬性
        超时"，因而加了心跳保活，结果适得其反——不但无效，加上心跳后连原本
        正常的"每 6 秒输出一行、共 18 秒"命令也会在 10 秒处被断（无心跳时它是
        成功的），因为这些额外的 OKAY/空 WRTE 破坏了 adb 的流控与消息边界。

        真因：建立上游 stream 时用了 socket.create_connection(addr, timeout=10)，
        Python 会把这个超时保留在 socket 上，导致 stream_reader 里的 recv 在
        设备端 10 秒无输出时抛 socket.timeout，被 except Exception: pass 静默
        吞掉后拆掉连接。已在 on_open 中用 settimeout(None) 修复。

        因此心跳保留为默认关闭，仅供实验。
        """
        while not self.closed:
            time.sleep(2)
            if self.closed:
                return
            now = time.time()
            with self.lock:
                streams = list(self.streams.values())
            for st in streams:
                if now - st.last_active >= 2:
                    self.send(A_OKAY, st.our_id, st.local_id)

    def stream_writer(self, st):
        """上行方向：从队列取数据写给 adb server，成功后回 OKAY 做流控。

        会把队列里等待的多个块合并成一次 sendall 以提高吞吐，但每个块仍
        单独回一个 OKAY —— adb 的流控是按包确认的，少回会让对端卡住。
        """
        try:
            while not self.closed:
                data = st.wq.get()
                if data is None:
                    return
                blocks = [data]
                total = len(data)
                while total < (1 << 20):
                    try:
                        more = st.wq.get_nowait()
                    except queue.Empty:
                        break
                    if more is None:
                        return
                    blocks.append(more)
                    total += len(more)
                chunk = b"".join(blocks) if len(blocks) > 1 else data
                try:
                    st.sock.sendall(chunk)
                    st.up += len(chunk)
                    st.last_active = time.time()
                    for _ in blocks:
                        self.send(A_OKAY, st.our_id, st.local_id)
                except Exception as exc:
                    log("stream#%d 上行写入失败: %s (已上行 %dB)" %
                        (st.local_id, exc, st.up))
                    self.drop_stream(st)
                    return
        except Exception:
            pass

    def stream_reader(self, st):
        try:
            while not self.closed:
                data = st.sock.recv(65536)
                if not data:
                    log("stream#%d 上游 EOF：adb server 主动关闭了这条流 "
                        "(已下行 %dB)" % (st.local_id, st.down))
                    break
                st.down += len(data)
                st.last_active = time.time()
                with st.cond:
                    st.unacked += len(data)
                    waited = 0.0
                    while (st.unacked > WINDOW and not self.closed
                           and waited < 3.0):
                        st.cond.wait(0.2)
                        waited += 0.2
                if self.closed:
                    return
                st.pending.append(len(data))
                self.send(A_WRTE, st.our_id, st.local_id, data)
        except socket.timeout:
            log("stream#%d 上游 recv 超时（已下行 %dB）" % (st.local_id, st.down))
        except Exception as exc:
            log("stream#%d 读取异常: %r（已下行 %dB）"
                % (st.local_id, exc, st.down))
        self.drop_stream(st)

    # ---------- 主循环 ----------
    def run(self):
        try:
            header = read_exact(self.client, 24)
            cmd, arg0, arg1, dlen = struct.unpack("<4I", header[:16])
            if cmd == A_CNXN:
                if dlen:
                    read_exact(self.client, dlen)
                # banner 必须带 ro.product.*：只给 features 的话 adb 客户端不会
                # 协商出 shell_v2，会退化成老 shell + PTY，导致 stderr 与 stdout
                # 合并、退出码丢失、设备端 TTY 检测产生 ANSI 颜色。
                # 注意：banner 不能以 NUL 结尾，否则 features 会变成 "shell_v2\0"，
                # 客户端匹配不到 shell_v2 就会退回老协议。
                prod = self.product or "unknown"
                payload = ("device::ro.product.name=%s;ro.product.model=%s;"
                           "ro.product.device=%s;features=%s"
                           % (prod, prod, prod, self.features)).encode()
                self.send(A_CNXN, VERSION, min(arg1 or MAXDATA, MAXDATA), payload)
                log("%s 已接入 -> %s" % (self.peer, self.serial))
                if self.enable_heartbeat:
                    threading.Thread(target=self.heartbeat, daemon=True).start()
            else:
                log("%s 首个包不是 CNXN，放弃" % self.peer)
                self.client.close()
                return

            while not self.closed:
                header = read_exact(self.client, 24)
                cmd, arg0, arg1, dlen = struct.unpack("<4I", header[:16])
                data = read_exact(self.client, dlen) if dlen else b""
                if DEBUG_PACKETS:
                    log("  ← %-4s a0=%-6d a1=%-6d len=%d" %
                        (cmd_name(cmd), arg0, arg1, dlen))
                if cmd == A_OPEN:
                    self.on_open(arg0, data)
                elif cmd == A_WRTE:
                    self.on_write(arg0, data)
                elif cmd == A_OKAY:
                    self.on_okay(arg0, arg1)
                elif cmd == A_CLSE:
                    self.on_close(arg0)
                elif cmd == A_CNXN:
                    continue
                else:
                    log("未知包 cmd=%08x" % cmd)
        except Exception as exc:
            log("%s 会话结束: %s" % (self.peer, exc))
        finally:
            self.close()


def main():
    ap = argparse.ArgumentParser(description="Expose a USB adb device over TCP")
    ap.add_argument("--listen-port", type=int, default=5555)
    ap.add_argument("--listen-addr", default="0.0.0.0")
    ap.add_argument("--serial", default=None, help="设备 serial，默认第一台在线设备")
    ap.add_argument("--server-port", type=int, default=5037)
    ap.add_argument("--heartbeat", action="store_true",
                    help="启用空闲心跳（默认关闭；实测会破坏流控，仅供实验）")
    ap.add_argument("--debug-packets", action="store_true",
                    help="打印每个 adb 包的收发（排障用）")
    args = ap.parse_args()

    global DEBUG_PACKETS
    DEBUG_PACKETS = args.debug_packets

    adb(["start-server"])
    serial = args.serial or (list_devices() or [None])[0]
    if not serial:
        log("没有检测到在线 adb 设备，请检查 USB 连接")
        sys.exit(1)

    server_addr = ("127.0.0.1", args.server_port)
    if not ensure_server(server_addr):
        log("警告: adb server 拉起失败，桥仍会监听，连接时会自动重试")
    features = query_features(serial, server_addr)
    product = query_simple(serial, server_addr, "get-product")
    threading.Thread(target=watchdog, args=(server_addr,), daemon=True).start()
    log("目标设备: %s" % serial)
    log("设备 features: %s" % (features or "(未获取到，按老协议工作)"))
    log("设备 product: %s" % (product or "(未知)"))

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((args.listen_addr, args.listen_port))
    srv.listen(16)
    log("监听 %s:%d —— 电脑 B 执行: adb connect <本机IP>:%d"
        % (args.listen_addr, args.listen_port, args.listen_port))

    try:
        while True:
            client, _ = srv.accept()
            session = Session(client, serial, server_addr, features, product,
                             enable_heartbeat=args.heartbeat)
            threading.Thread(target=session.run, daemon=True).start()
    except KeyboardInterrupt:
        log("退出")
    finally:
        srv.close()


if __name__ == "__main__":
    main()
