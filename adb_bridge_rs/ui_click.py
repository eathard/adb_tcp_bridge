"""按窗口相对坐标点击界面元素，并可选截图。

用法:
    python ui_click.py <dx> <dy> [shotfile]
其中 dx/dy 是相对窗口左上角的**屏幕像素**偏移。
"""
import ctypes
import ctypes.wintypes as wt
import subprocess
import sys
import time

from PIL import ImageGrab

u = ctypes.windll.user32
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    pass

TITLE = "ADB TCP 桥控制台"


def win_rect():
    h = u.FindWindowW(None, TITLE)
    if not h:
        raise SystemExit("窗口未找到")
    r = wt.RECT()
    u.GetWindowRect(h, ctypes.byref(r))
    return r.left, r.top, r.right, r.bottom


def click(x, y):
    old = wt.POINT()
    u.GetCursorPos(ctypes.byref(old))
    u.SetCursorPos(int(x), int(y))
    time.sleep(0.25)
    u.mouse_event(2, 0, 0, 0, 0)
    time.sleep(0.08)
    u.mouse_event(4, 0, 0, 0, 0)
    time.sleep(0.4)
    u.SetCursorPos(old.x, old.y)


def main():
    dx, dy = int(sys.argv[1]), int(sys.argv[2])
    shot = sys.argv[3] if len(sys.argv) > 3 else None
    l, t, r, b = win_rect()
    h = u.FindWindowW(None, TITLE)
    # 必须用 SetWindowPos 提到最前：SetForegroundWindow 在本机常被
    # 前台锁定策略拦掉，导致点击落到别的窗口（曾误点到 IDE 的输入框）。
    u.ShowWindow(h, 9)  # SW_RESTORE
    u.SetWindowPos(h, -1, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0040)  # TOPMOST|NOSIZE|NOMOVE|SHOWWINDOW
    u.SetForegroundWindow(h)
    time.sleep(0.9)
    click(l + dx, t + dy)
    print("窗口@%d,%d  点击偏移(%d,%d) -> 屏幕(%d,%d)" % (l, t, dx, dy, l + dx, t + dy))
    if shot:
        time.sleep(1.2)
        h = u.FindWindowW(None, TITLE)
        u.SetWindowPos(h, -1, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0040)
        u.SetForegroundWindow(h)
        time.sleep(0.8)
        ImageGrab.grab(bbox=(l + 8, t + 8, r - 8, b - 8),
                       all_screens=True).save(shot)
        print("已保存", shot)


if __name__ == "__main__":
    main()