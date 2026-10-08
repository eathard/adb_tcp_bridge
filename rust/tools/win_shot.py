#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""截取 adb_bridge_rs GUI 窗口，用于验证中文字体是否正常渲染。

只捕获目标窗口矩形区域，不触碰其他窗口内容。
用法: python win_shot.py <输出png路径>
"""
import ctypes
import ctypes.wintypes as wt
import sys
import time

from PIL import ImageGrab

user32 = ctypes.windll.user32

# ⚠️ 必须先声明 DPI 感知，否则 GetWindowRect 返回的是被系统虚拟化过的
# 逻辑坐标，而抓屏拿到的是物理像素 —— 两者不成比例，会截到放大后的
# 左上角区域（表现为「右侧内容看不见」的假布局 bug）。
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PER_MONITOR_AWARE
except Exception:
    try:
        user32.SetProcessDPIAware()
    except Exception:
        pass

TITLE = "ADB TCP 桥控制台"


def screen_metrics():
    w = user32.GetSystemMetrics(0)
    h = user32.GetSystemMetrics(1)
    dc = user32.GetDC(0)
    dpi = ctypes.windll.gdi32.GetDeviceCaps(dc, 88)  # LOGPIXELSX
    user32.ReleaseDC(0, dc)
    return w, h, dpi


def find_window(title):
    """按窗口标题精确查找。"""
    hwnd = user32.FindWindowW(None, title)
    return hwnd if hwnd else None


def get_rect(hwnd):
    r = wt.RECT()
    # 用 GetWindowRect 拿到的是含边框的外框
    if not user32.GetWindowRect(hwnd, ctypes.byref(r)):
        return None
    return (r.left, r.top, r.right, r.bottom)


def main():
    out = sys.argv[1] if len(sys.argv) > 1 else "gui_shot.png"
    w, h, dpi = screen_metrics()
    print("屏幕 %dx%d  缩放 %d%%" % (w, h, int(dpi * 100 / 96)))

    hwnd = find_window(TITLE)
    if not hwnd:
        print("未找到窗口: %s" % TITLE)
        return 1

    # 若窗口最小化则先还原，再置顶，确保截到内容而不是空白
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        time.sleep(0.8)
    user32.SetForegroundWindow(hwnd)
    time.sleep(1.0)

    rect = get_rect(hwnd)
    if not rect:
        print("无法获取窗口矩形")
        return 1
    w = rect[2] - rect[0]
    h = rect[3] - rect[1]
    print("窗口矩形: %dx%d @ (%d,%d)" % (w, h, rect[0], rect[1]))

    img = ImageGrab.grab(bbox=rect, all_screens=True)
    img.save(out)
    print("已保存: %s (%dx%d)" % (out, img.width, img.height))
    return 0


if __name__ == "__main__":
    sys.exit(main())
