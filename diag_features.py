#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""诊断：adb server 眼中 USB 设备与桥设备的 features / product 是否一致。"""
import socket


def query(cmd):
    s = socket.create_connection(("127.0.0.1", 5037), timeout=8)
    s.sendall(("%04x" % len(cmd)).encode() + cmd.encode())
    if s.recv(4) != b"OKAY":
        s.close()
        return "<FAIL>"
    n = int(s.recv(4), 16)
    data = s.recv(n).decode(errors="replace")
    s.close()
    return data


for dev in ["b57290249a9b3206", "172.16.0.106:15555"]:
    print("== %s" % dev)
    print("   features : %r" % query("host-serial:%s:features" % dev))
    print("   product  : %r" % query("host-serial:%s:get-product" % dev))
    print("   state    : %r" % query("host-serial:%s:get-state" % dev))
