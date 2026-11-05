# -*- coding: utf-8 -*-
"""生成 backfilltransfer 图标：512x512，圆角 22%，深蓝→青绿竖向渐变
图形：一个「下载目录（虚线框）」+ 两个文件方块，其中一个是实心（已整理），
一个带箭头指向右侧「媒体库」（实线框）—— 表达「把漏掉的存量补进库」。"""
import os

from PIL import Image, ImageDraw

S = 512
SS = 4  # 超采样
W = S * SS
img = Image.new("RGBA", (W, W), (0, 0, 0, 0))
d = ImageDraw.Draw(img)

TOP = (25, 48, 77)
BOT = (24, 120, 127)
for y in range(W):
    t = y / (W - 1)
    c = tuple(int(TOP[i] + (BOT[i] - TOP[i]) * t) for i in range(3)) + (255,)
    d.line([(0, y), (W, y)], fill=c, width=1)

mask = Image.new("L", (W, W), 0)
ImageDraw.Draw(mask).rounded_rectangle([0, 0, W - 1, W - 1],
                                       radius=int(W * 0.22), fill=255)
img.putalpha(mask)

d = ImageDraw.Draw(img)
WHITE = (255, 255, 255, 255)
DIM = (255, 255, 255, 135)
ACCENT = (110, 231, 183, 255)   # 青绿
u = W / 512.0


def L(*v):
    return [int(x * u) for x in v]


# 左侧：下载目录（虚线框）
x0, y0, x1, y1 = L(58, 128, 226, 384)
step = int(15 * u)
dash = int(9 * u)
for x in range(x0, x1, step):
    d.rectangle([x, y0, min(x + dash, x1), y0 + int(5 * u)], fill=DIM)
    d.rectangle([x, y1 - int(5 * u), min(x + dash, x1), y1], fill=DIM)
for y in range(y0, y1, step):
    d.rectangle([x0, y, x0 + int(5 * u), min(y + dash, y1)], fill=DIM)
    d.rectangle([x1 - int(5 * u), y, x1, min(y + dash, y1)], fill=DIM)

# 目录里三个文件条：上两个已整理（描边），下一个待补（实心+强调色）
def bar(cx0, cy0, cx1, cy1, filled, color):
    d.rounded_rectangle([cx0, cy0, cx1, cy1], radius=int(7 * u),
                        fill=color if filled else None,
                        outline=color, width=int(4 * u))

bar(*L(84, 168, 200, 208), True, DIM)
bar(*L(84, 232, 200, 272), True, DIM)
bar(*L(84, 296, 200, 344), True, ACCENT)

# 右侧：媒体库（实线圆角框）
box = L(300, 168, 456, 344)
d.rounded_rectangle(box, radius=int(18 * u), outline=WHITE, width=int(6 * u))
# 库内两行「条目」
for i, yy in enumerate((206, 250)):
    d.rounded_rectangle(L(322, yy, 434, yy + 26), radius=int(6 * u),
                        fill=(255, 255, 255, 200 if i == 0 else 110))

# 中间箭头：从待补文件指向媒体库
ax0, ax1 = L(206, 0)[0], L(298, 0)[0]
ay = L(0, 320)[1]
d.line([(ax0, ay), (ax1 - int(10 * u), ay)], fill=ACCENT, width=int(9 * u))
d.polygon([(ax1 - int(2 * u), ay),
           (ax1 - int(26 * u), ay - int(16 * u)),
           (ax1 - int(26 * u), ay + int(16 * u))], fill=ACCENT)

img = img.resize((S, S), Image.LANCZOS)
out = os.path.join("icons", "backfilltransfer.png")
os.makedirs("icons", exist_ok=True)
img.save(out, "PNG")
print("saved", out, img.size, os.path.getsize(out), "bytes")
