"""用假数据重新截「预览.png」（说明文档里那张图）。改了悬浮窗的样子之后跑一遍。要装 Pillow。

顺带截一张 150% 缩放的 preview-150.png 放在系统临时目录，只用来检查高缩放屏幕下的排版。
"""
import os, subprocess, sys, tempfile
from PIL import Image, ImageDraw

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(ROOT, "app", "agent_hud.pyw")
for scale in ("1.0", "1.5"):
    raw = os.path.join(tempfile.gettempdir(), "preview-%d.png" % (float(scale) * 100))
    subprocess.run([sys.executable, APP, "--demo", "--scale", scale, "--shot", raw], check=True)
    im = Image.open(raw).convert("RGBA")
    print(scale, im.size, raw)
    if scale == "1.0":
        # 截图是从屏幕上抓的，圆角外面带着桌面上的东西，抠成透明
        mask = Image.new("L", im.size, 0)
        ImageDraw.Draw(mask).rounded_rectangle((0, 0, im.width - 1, im.height - 1), radius=12, fill=255)
        im.putalpha(mask)
        im.save(os.path.join(ROOT, "预览.png"))
