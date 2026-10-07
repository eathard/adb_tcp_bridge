#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""窗口操作辅助：移动窗口到屏幕中间 / 截图 / 点击按钮。

用于在无人值守下验证 GUI，不需要人工操作。

用法:
    python win_act.py move            移动到屏幕中央并截图
    python win_act.py click-start     点击「启动桥」按钮
    python win_act.py shot <file>     截图
"""
import ctypes
import ctypes.wintypes as wt
import sys
import time

from PIL import ImageGrab

user32 = ctypes.windll.user32

# DPI 感知：否则坐标是虚拟化的，点击会偏到别处
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PER_MONITOR_AWARE
except Exception:
    try:
        user32.SetProcessDPIAware()
    except Exception:
        pass

TITLE = "ADB TCP 桥控制台"


def hwnd_of(title=TITLE):
    h = user32.FindWindowW(None, title)
    return h if h else None


def rect_of(hwnd):
    r = wt.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(r)):
        return None
    return (r.left, r.top, r.right, r.bottom)


def focus(hwnd):
    """还原并置顶。"""
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 9)
        time.sleep(0.6)
    user32.SetForegroundWindow(hwnd)
    time.sleep(0.8)


def shot(path, hwnd, inset=0):
    r = rect_of(hwnd)
    box = (r[0] + inset, r[1] + inset, r[2] - inset, r[3] - inset)
    img = ImageGrab.grab(bbox=box, all_screens=True)
    img.save(path)
    return box, img.size


def click_at(x, y):
    """在屏幕物理坐标点击一次，结束后把鼠标挪回原处。"""
    old = wt.POINT()
    user32.GetCursorPos(ctypes.byref(old))

    user32.SetCursorPos(int(x), int(y))
    time.sleep(0.25)
    user32.mouse_event(2, 0, 0, 0, 0)  # LEFTDOWN
    time.sleep(0.08)
    user32.mouse_event(4, 0, 0, 0, 0)  # LEFTUP
    time.sleep(0.4)

    user32.SetCursorPos(old.x, old.y)


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    cmd = sys.argv[1]
    hwnd = hwnd_of()
    if not hwnd:
        print("未找到窗口")
        return 1

    if cmd == "move":
        # 移到屏幕中央，四周留出桌面，排除相邻窗口干扰
        sw = user32.GetSystemMetrics(0)
        sh = user32.GetSystemMetrics(1)
        r = rect_of(hwnd)
        w, h = r[2] - r[0], r[3] - r[1]
        nx, ny = max(0, (sw - w) // 2), max(0, (sh - h) // 3)
        user32.SetWindowPos(hwnd, 0, nx, ny, 0, 0, 0x0001 | 0x0004)  # NOSIZE|NOZORDER
        time.sleep(0.8)
        focus(hwnd)
        r, size = shot("gui_moved.png", hwnd, inset=16)
        print("已移动到 (%d,%d) 矩形=%s 截图=%s" % (nx, ny, r, size))
        return 0

    if cmd == "click-start":
        focus(hwnd)
        r = rect_of(hwnd)
        # 「启动桥」按钮在客户区右上角。
        # 实测（180% 缩放的 2560x1600 屏）：中心距右边缘约 72px、距顶约 62px
        x = r[2] - 72
        y = r[1] + 62
        print("点击坐标 (%d,%d)  窗口=%s" % (x, y, r))
        click_at(x, y)
        time.sleep(1.5)
        return 0

    if cmd == "clickxy":
        # clickxy <x> <y> —— 绝对屏幕坐标点击
        focus(hwnd)
        x, y = int(sys.argv[2]), int(sys.argv[3])
        click_at(x, y)
        print("clicked %d,%d" % (x, y))
        return 0

    if cmd == "fullshot":
        # 全屏截图（物理像素）
        from PIL import ImageGrab as _G
        sw = user32.GetSystemMetrics(0)
        sh = user32.GetSystemMetrics(1)
        img = _G.grab(bbox=(0, 0, sw, sh), all_screens=True)
        path = sys.argv[2] if len(sys.argv) > 2 else "full.png"
        img.save(path)
        print("全屏 %dx%d -> %s" % (img.width, img.height, path))
        return 0

    if cmd == "shot":
        path = sys.argv[2] if len(sys.argv) > 2 else "gui_shot.png"
        inset = int(sys.argv[3]) if len(sys.argv) > 3 else 16
        focus(hwnd)
        r, size = shot(path, hwnd, inset=inset)
        print("已保存 %s 矩形=%s" % (path, r))
        return 0

    print("未知命令")
    return 1


if __name__ == "__main__":
    sys.exit(main())
