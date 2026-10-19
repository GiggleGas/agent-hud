"""画 exe 的图标 dev/icon.ico：深色圆角方块上三条进度条。要装 Pillow；图标已经生成好放在旁边，想改样子才需要重跑。"""
import os
from PIL import Image, ImageDraw

S = 4   # 先画 4 倍大再缩小，边缘才平滑
im = Image.new("RGBA", (256 * S, 256 * S), (0, 0, 0, 0))
d = ImageDraw.Draw(im)


def box(x0, y0, x1, y1, r, **kw):
    d.rounded_rectangle((x0 * S, y0 * S, x1 * S, y1 * S), radius=r * S, **kw)


box(8, 8, 248, 248, 52, fill="#15171c", outline="#3a3f4c", width=5 * S)
for y, color, length in ((66, "#4c9aff", 128), (113, "#8b7bff", 76), (160, "#3ecf8e", 156)):
    box(48, y, 208, y + 30, 15, fill="#383d4a")
    box(48, y, 48 + length, y + 30, 15, fill=color)
out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icon.ico")
im.resize((256, 256), Image.LANCZOS).save(out, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
im.resize((256, 256), Image.LANCZOS).save(os.path.splitext(out)[0] + ".png")
print(out)
