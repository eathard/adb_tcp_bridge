#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""自动点掉 Windows 防火墙「是否允许…访问」弹窗。

新编译出来的 exe 每次哈希变化，Windows 防火墙都会当成一个「新程序」重新
问一遍。无人值守验证界面时这个弹窗会挡住窗口，且不点「允许」局域网根本
连不上，所以需要一个能自动点掉它的工具。

做法：枚举顶层窗口找到那个任务对话框（class #32770，标题含「安全中心」
或「Windows Security」），再枚举它的按钮子窗口，按文本找到「允许 / Allow
access」并点击其中心点。

用法: python fw_allow.py [--wait 秒数]
"""
import ctypes
import ctypes.wintypes as wt
import sys
import time

user32 = ctypes.windll.user32

# DPI 感知：否则坐标被虚拟化，点击会偏
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PER_MONITOR_AWARE
except Exception:
    try:
        user32.SetProcessDPIAware()
    except Exception:
        pass

WNDENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

TITLE_HINTS = ("安全中心", "Windows Security", "Windows Defender")


def text_of(hwnd):
    n = user32.GetWindowTextLengthW(hwnd)
    if n <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


def class_of(hwnd):
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def find_dialogs():
    """找所有标题含安全中心关键字、且类名为对话框的顶层窗口。"""
    found = []

    def cb(hwnd, _lp):
        if user32.IsWindowVisible(hwnd):
            t = text_of(hwnd)
            if any(h in t for h in TITLE_HINTS):
                found.append((hwnd, t, class_of(hwnd)))
        return True

    user32.EnumWindows(WNDENUMPROC(cb), 0)
    return found


def find_child_button(hwnd, want_prefixes):
    """在窗口子树里找文本以给定前缀开头的按钮，返回 (hwnd, 文本)。"""
    hits = []

    def cb(h, _lp):
        if class_of(h) == "Button":
            t = text_of(h).strip()
            # Windows 的按钮文本常带 & 助记符，去掉再比
            tt = t.replace("&", "")
            for p in want_prefixes:
                if tt.startswith(p):
                    hits.append((h, t))
                    break
        return True

    user32.EnumChildWindows(hwnd, WNDENUMPROC(cb), 0)
    return hits


def click_at_xy(x, y):
    """按绝对屏幕坐标点一下，结束把鼠标挪回原处。

    不做任何 SetForegroundWindow —— 对 Shell_SystemDialogProxy 调前台会
    把它压到窗口后面，那一下就点到别的窗口去了。
    """
    old = wt.POINT()
    user32.GetCursorPos(ctypes.byref(old))
    user32.SetCursorPos(int(x), int(y))
    time.sleep(0.3)
    user32.mouse_event(2, 0, 0, 0, 0)  # LEFTDOWN
    time.sleep(0.1)
    user32.mouse_event(4, 0, 0, 0, 0)  # LEFTUP
    time.sleep(0.5)
    user32.SetCursorPos(old.x, old.y)


def click_center(hwnd):
    r = wt.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    x = (r.left + r.right) // 2
    y = (r.top + r.bottom) // 2

    old = wt.POINT()
    user32.GetCursorPos(ctypes.byref(old))
    user32.SetCursorPos(x, y)
    time.sleep(0.2)
    user32.mouse_event(2, 0, 0, 0, 0)  # LEFTDOWN
    time.sleep(0.08)
    user32.mouse_event(4, 0, 0, 0, 0)  # LEFTUP
    time.sleep(0.4)
    user32.SetCursorPos(old.x, old.y)
    return x, y


def main():
    wait = 0.0
    if "--wait" in sys.argv:
        wait = float(sys.argv[sys.argv.index("--wait") + 1])

    # --xy X,Y：直接按绝对屏幕坐标点。Shell_SystemDialogProxy 的
    # GetWindowRect 恒为 0x0，拿不到几何信息，只能从截图反算坐标。
    if "--xy" in sys.argv:
        i = sys.argv.index("--xy")
        x, y = (int(v) for v in sys.argv[i + 1].split(","))
        click_at_xy(x, y)
        print("已点击屏幕坐标 (%d,%d)" % (x, y))
        return 0

    deadline = time.time() + wait
    while True:
        dialogs = find_dialogs()
        if dialogs:
            break
        if time.time() >= deadline:
            print("未发现防火墙弹窗")
            return 1
        time.sleep(0.5)

    for hwnd, title, cls in dialogs:
        print("弹窗: hwnd=%d 类名=%s 标题=%r" % (hwnd, cls, title))
        # 先把弹窗拉到前台，否则点击可能落到别的窗口上
        user32.SetForegroundWindow(hwnd)
        time.sleep(0.4)
        btns = find_child_button(hwnd, ("允许", "Allow access", "Allow"))
        for _bh, bt in btns:
            print("  按钮: %r" % bt)

        r = wt.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(r))
        rw, rh = r.right - r.left, r.bottom - r.top
        print("  矩形: %dx%d @ (%d,%d)" % (rw, rh, r.left, r.top))

        if btns:
            target, tlabel = btns[0]
            x, y = click_center(target)
        else:
            # ⚠️ Win11 的防火墙弹窗类名是 Shell_SystemDialogProxy：整个对话框由
            # XAML/合成器绘制，按钮**不是子窗口**，EnumChildWindows 枚举不到，
            # 只能按经验比例点。下面这组比例是实测标定的：
            # 「允许」按钮中心 ≈ 弹窗宽度的 26%、高度的 85%。
            tlabel = "允许(按比例)"
            x = r.left + int(rw * 0.263)
            y = r.top + int(rh * 0.851)
            user32.SetCursorPos(x, y)
            time.sleep(0.25)
            user32.mouse_event(2, 0, 0, 0, 0)
            time.sleep(0.08)
            user32.mouse_event(4, 0, 0, 0, 0)
            time.sleep(0.4)

        print("  已点击 %r @ (%d,%d)" % (tlabel, x, y))
        time.sleep(1.0)
        if user32.IsWindow(hwnd):
            print("  ⚠️ 弹窗仍在")
        else:
            print("  弹窗已关闭")
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
