"""用假数据截三张图：「最近 24 小时」、展开一条「更多」之后、「按项目」展开一个项目。存到系统临时目录。要装 Pillow。
每张图窗口在屏幕上露一两秒，不抢焦点。"""
import importlib.machinery, importlib.util, os, sys, tempfile, time
from PIL import ImageGrab

sys.stdout.reconfigure(encoding="utf-8")
APP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app")
sys.path.insert(0, APP)
loader = importlib.machinery.SourceFileLoader("agent_hud", os.path.join(APP, "agent_hud.pyw"))
spec = importlib.util.spec_from_loader("agent_hud", loader)
mod = importlib.util.module_from_spec(spec)
loader.exec_module(mod)

h = mod.Hud(["--demo"])
now = time.time()
views = mod.demo_views(now)
for v in views:
    if v["state"] in mod.hub.ENDED and now - v["end"] < 600:
        h.on_event("finished", v, False)
h.views = [v for v in views if v["state"] not in mod.hub.ENDED]
h.history = [v for v in views if v["state"] in mod.hub.ENDED]


def shot(name):
    h.render()
    h.root.update()
    time.sleep(0.8)
    h.root.update()
    x, y = h.root.winfo_rootx(), h.root.winfo_rooty()
    out = os.path.join(tempfile.gettempdir(), "hud-%s.png" % name)
    ImageGrab.grab(bbox=(x, y, x + h.root.winfo_width(), y + h.root.winfo_height()), include_layered_windows=True, all_screens=True).save(out)
    print(name, out)


shot("tab-recent")
h.expanded.add("升级依赖并修掉编译警告")
shot("tab-recent-more")
h.expanded.clear()
h.cfg["tab"] = "projects"
h.cfg["ws_open"]["shop-server"] = True
shot("tab-projects")
h.root.destroy()
