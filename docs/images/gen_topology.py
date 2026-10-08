#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成 README 用的 ABC 拓扑图：docs/images/topology.svg

版式**刻意与软件「关于软件」页里的拓扑图保持一致**
（见 rust/ui/main.slint 的 TopoCircle / TopoLink 组件），配色取自同一个
全局调色板 `Pal`。改了界面里的图，就重新跑一次本脚本：

    python docs/images/gen_topology.py

之所以手写 SVG 而不是截图：截图会带窗口边框、且缩放后文字发虚；
SVG 在 GitHub（raw 返回 image/svg+xml）和高分屏下都是清晰的矢量。

注意：本仓库的 Slint 界面**渲染不出 SVG 里的文字**（usvg 的 fontdb 是空的），
但 GitHub 和浏览器没有这个问题 —— 这里生成的是给文档用的图，不进界面。
"""

import os

# ── 配色：与 rust/ui/main.slint 的 global Pal 一一对应 ──
BG        = "#14171d"   # Pal.bg
CARD      = "#21262f"   # Pal.card
HILIGHT   = "#1b2534"   # 高亮节点的圆底（TopoCircle 里写死的值）
BORDER    = "#2e3542"   # Pal.border
TEXT      = "#e6e9ef"   # Pal.text
TEXT_DIM  = "#98a2b6"   # Pal.text-dim
ACCENT    = "#4c8dff"   # Pal.accent

FONT = ('Microsoft YaHei,PingFang SC,Hiragino Sans GB,'
        'Noto Sans CJK SC,Source Han Sans SC,sans-serif')

# ── 尺寸：与 Slint 组件一致 ──
W, H = 420, 458
CX = W // 2
PAD_TOP = 20
NODE_H = 98        # TopoCircle 的 height
LINK_H = 62        # TopoLink 的 height
CIRCLE_D = 56      # 圆的直径
CAPSULE_H = 19     # 胶囊标签高度

out = []
a = out.append


def esc(s):
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def text_width(s, size):
    """粗略估算：中日韩全角字符按 1em，ASCII 按 0.56em。够用来定胶囊宽度。"""
    w = 0.0
    for ch in s:
        w += size * (1.0 if ord(ch) > 0x2000 else 0.56)
    return w


def circle_icon(cy, kind, tint):
    """圆内的图标：0 = 显示器（电脑），1 = 芯片（设备）。与 IconMonitor/IconChip 同形。"""
    if kind == 0:
        # 屏幕（描边矩形）+ 底座横条，整体高 17
        top = cy - 8.5
        a(f'<rect x="{CX-13:.1f}" y="{top:.1f}" width="26" height="15" rx="2" '
          f'fill="none" stroke="{tint}" stroke-width="2"/>')
        a(f'<rect x="{CX-5}" y="{top+15:.1f}" width="10" height="2" fill="{tint}"/>')
    else:
        # 上下各三根引脚 + 中间主体，整体高 27
        top = cy - 13.5
        for i in (-5, 0, 5):
            a(f'<rect x="{CX+i-1}" y="{top:.1f}" width="2" height="4" fill="{tint}"/>')
        a(f'<rect x="{CX-11}" y="{top+5:.1f}" width="22" height="17" rx="2" '
          f'fill="none" stroke="{tint}" stroke-width="2"/>')
        for i in (-5, 0, 5):
            a(f'<rect x="{CX+i-1}" y="{top+23:.1f}" width="2" height="4" fill="{tint}"/>')


def topo_circle(top, kind, title, sub, highlight=False):
    """TopoCircle：圆形 + 图标 + 标题 + 副标题。"""
    cy = top + CIRCLE_D / 2
    fill = HILIGHT if highlight else CARD
    stroke = ACCENT if highlight else BORDER
    tint = ACCENT if highlight else TEXT

    a(f'<circle cx="{CX}" cy="{cy:.1f}" r="{CIRCLE_D/2}" fill="{fill}" '
      f'stroke="{stroke}" stroke-width="2"/>')
    circle_icon(cy, kind, tint)

    a(f'<text x="{CX}" y="{top+75}" fill="{TEXT}" font-size="14" '
      f'font-weight="600" text-anchor="middle" font-family=\'{FONT}\'>{esc(title)}</text>')
    a(f'<text x="{CX}" y="{top+95}" fill="{TEXT_DIM}" font-size="12" '
      f'text-anchor="middle" font-family=\'{FONT}\'>{esc(sub)}</text>')


def topo_link(top, label):
    """TopoLink：竖线 → 胶囊标签 → 竖线 → 实心三角箭头（指向下一个节点）。"""
    w = max(88, int(text_width(label, 11) + 24))
    x0 = CX - w / 2

    # 竖线（两段，被中间的胶囊"遮断"——胶囊背景是不透明的页面底色）
    a(f'<rect x="{CX-0.5}" y="{top+4.5:.1f}" width="1" height="14" fill="{BORDER}"/>')
    a(f'<rect x="{x0:.1f}" y="{top+18.5:.1f}" width="{w}" height="{CAPSULE_H}" '
      f'rx="9.5" fill="{BG}" stroke="{BORDER}" stroke-width="1"/>')
    a(f'<text x="{CX}" y="{top+32:.1f}" fill="{TEXT_DIM}" font-size="11" '
      f'text-anchor="middle" font-family=\'{FONT}\'>{esc(label)}</text>')
    a(f'<rect x="{CX-0.5}" y="{top+37.5:.1f}" width="1" height="14" fill="{BORDER}"/>')

    # 三角箭头：**宽边在上、尖端在下**。Slint 里没有 rotation，是三段递减横条堆的
    for i, wd in enumerate((10, 6, 2)):
        a(f'<rect x="{CX-wd/2:.1f}" y="{top+51.5+i*2:.1f}" width="{wd}" height="2" '
          f'fill="{BORDER}"/>')


# ── 组装 ──
a(f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" '
  f'viewBox="0 0 {W} {H}" role="img" '
  f'aria-label="电脑 B 经局域网 TCP 连接电脑 A，电脑 A 经 USB 连接设备 C">')
a(f'<rect x="0" y="0" width="{W}" height="{H}" rx="12" fill="{BG}"/>')

y = PAD_TOP
topo_circle(y, 0, "电脑 B", "用 adb 命令操作")
y += NODE_H
topo_link(y, "TCP 局域网")
y += LINK_H
topo_circle(y, 0, "电脑 A（本程序）", "设备通过 USB 接在这里", highlight=True)
y += NODE_H
topo_link(y, "USB（ADB 协议）")
y += LINK_H
topo_circle(y, 1, "设备 C", "无需改动、无需 root")

a('</svg>')

here = os.path.dirname(os.path.abspath(__file__))
path = os.path.join(here, "topology.svg")
with open(path, "w", encoding="utf-8") as f:
    f.write("\n".join(out) + "\n")
print("已生成", path, os.path.getsize(path), "字节")
