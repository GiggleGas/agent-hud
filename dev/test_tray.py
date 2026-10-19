"""托盘图标和「关闭」的回归测试：图标画得对不对、点 ✕ 按选择办、藏起来 / 拿出来、托盘菜单的命令、
上次藏着关的下次启动接着藏、再启动一次会把藏着的窗口叫回来。
会真的往托盘里加几秒钟图标（不抢焦点）；起的测试悬浮窗在沙盒里，几秒后自己退出。"""
import ctypes, json, os, subprocess, sys, tempfile, time
from ctypes import wintypes

sys.stdout.reconfigure(encoding="utf-8")
BOX = tempfile.mkdtemp(prefix="agent-hud-tray-")
os.environ.update(AGENT_HUD_TEST_ROOT=BOX, CLAUDE_CONFIG_DIR=os.path.join(BOX, ".claude"))
DEV = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(os.path.dirname(DEV), "app")
sys.path.insert(0, APP)
import importlib.machinery, importlib.util
loader = importlib.machinery.SourceFileLoader("agent_hud", os.path.join(APP, "agent_hud.pyw"))
spec = importlib.util.spec_from_loader("agent_hud", loader)
mod = importlib.util.module_from_spec(spec)
loader.exec_module(mod)
import dialogs, install, tray

fails = []


def check(name, ok, detail=""):
    print(("  ok   " if ok else "  FAIL ") + name + ("" if ok else "  " + str(detail)))
    if not ok:
        fails.append(name)


print("== 图标")
data = tray.pixels(16, "#4c9aff")
px = lambda x, y: tuple(data[(y * 16 + x) * 4:(y * 16 + x) * 4 + 4])   # BGRA
check("16×16，每个像素 4 个字节", len(data) == 16 * 16 * 4)
check("角上透明（圆角）", px(0, 0)[3] == 0, px(0, 0))
check("中间是状态色", px(8, 8) == (0xff, 0x9a, 0x4c, 255), px(8, 8))
check("圆点外面是深色底", px(2, 8)[:3] != (0xff, 0x9a, 0x4c) and px(2, 8)[3] == 255, px(2, 8))
clicks = []
t = tray.Tray(mod.TRAY_COLORS, clicks.append)
check("真的加进托盘", t.start() and t.ok)
t.update("ask", "测试\n1 个等你")
check("换状态和提示", (t.state, t.tip) == ("ask", "测试\n1 个等你"))
t.stop()
check("拿掉：图标没了，线程结束", not t.ok and not t.thread.is_alive())

print("== 点 ✕")


class FakeTray:
    def __init__(self):
        self.shown, self.updates = True, []

    def update(self, state, tip):
        self.updates.append((state, tip))


h = mod.Hud(["--demo"])
h.root.attributes("-alpha", 0.0)
h.root.update()
quits, asks = [], []
h.quit = lambda: quits.append(1)
real_ask = dialogs.CloseAsk
dialogs.CloseAsk = lambda hud: asks.append(1) or type("W", (), {"winfo_exists": lambda self: True, "lift": lambda self: asks.append("lift")})()
h.tray = None
h.on_close()
check("托盘图标没加上：直接退出（藏起来就找不回来了）", quits == [1] and not asks)
h.tray = FakeTray()
h.on_close()
check("没记住选择：问一句", asks == [1] and quits == [1])
h.on_close()
check("问的窗口还开着：再点 ✕ 只是把它顶上来", asks == [1, "lift"])
h.close_ask = None
h.cfg["close_action"] = "quit"
h.on_close()
check("记住了「退出」：直接退出", quits == [1, 1])
h.cfg["close_action"] = "tray"
h.on_close()
h.root.update()
check("记住了「收到托盘」：藏起来", h.hidden and h.root.state() == "withdrawn" and h.cfg["hidden"] and h.tray.shown is False)
h.on_tray("show")
h.root.update()
check("左键单击托盘图标：拿出来", not h.hidden and h.root.state() == "normal" and not h.cfg["hidden"] and h.tray.shown)
h.on_tray("show")
h.root.update()
check("再左键单击：还是显示着，不会藏起（用户定的）", not h.hidden and h.root.state() == "normal")
h.on_tray("toggle")
check("右键菜单「藏起悬浮窗」：藏起来", h.hidden)
h.on_tray("quit")
check("托盘菜单「退出」", quits == [1, 1, 1])
opened = []
h.compose, h.feedback = (lambda: opened.append("compose")), (lambda: opened.append("feedback"))
h.on_tray("compose")
h.on_tray("feedback")
check("托盘菜单「发起任务」「反馈」", opened == ["compose", "feedback"])
h.tray_q.put("show")
h.tick()
check("托盘线程放进队列的命令，界面线程来取", not h.hidden)
views = mod.demo_views(time.time())   # 假数据里有一个在等你回答的
h.views = [v for v in views if v["state"] not in mod.hub.ENDED]
h.render()
check("托盘图标跟着状态：有在等你的就是黄色，提示里写着几个在干活、几个等你",
      h.tray.updates and h.tray.updates[-1][0] == "ask" and "个等你" in h.tray.updates[-1][1], h.tray.updates[-1:])

print("== 任务完成时闪光")
h.flash_since = None
done = dict(mod.demo_views(time.time())[0], id="f1", state="done", end=time.time(), last_text="好了")
h.on_event("finished", done, False)
check("悬浮窗启动时就已经结束的（不通知）：不闪", h.flash_since is None)
h.on_event("finished", dict(done, id="f2", state="interrupted"), True)
check("用户自己打断的：不闪", h.flash_since is None)
h.on_event("finished", dict(done, id="f3"), True)
check("新做完的：开始闪", h.flash_since is not None)
h.render()
h.root.update()
h.flash_since = time.time() - mod.FLASH_SWEEP / 2   # 停在一次扫光的正中间
h.draw_flash(time.time())
check("扫到中间：一道细光（亮线加渐暗的尾巴）画在盖在上面的那层透明窗口里",
      h.glow is not None and len(h.glow_c.find_all()) == len(mod.FLASH_SHAPE), h.glow and h.glow_c.find_all())
h.flash_since = time.time() - (mod.FLASH_SWEEP + mod.FLASH_EVERY) / 2   # 两次扫光之间
h.draw_flash(time.time())
check("两次扫光之间：什么都不画（每秒一次）", not h.glow_c.find_all())
h.root.update()
h.c.event_generate("<ButtonPress-1>", x=20, y=200)
h.c.event_generate("<ButtonRelease-1>", x=20, y=200)
h.root.update()
check("点一下悬浮窗：停", h.flash_since is None and not h.glow_c.find_all())
h.on_event("finished", dict(done, id="f4", state="error"), True)
check("出错结束的：也闪", h.flash_since is not None)
h.stop_flash()
shown = []
real_show = h.show
h.hidden, h.show = True, lambda: shown.append(1)
h.on_event("finished", dict(done, id="f5"), True)
check("藏在托盘里：先拿出来再闪", shown == [1] and h.flash_since is not None)
h.hidden, h.show = False, real_show
h.v_flash.set(False)
h.on_option()
check("在菜单里关掉：马上停", h.flash_since is None and h.cfg["flash_on_done"] is False)
h.on_event("finished", dict(done, id="f6"), True)
check("关掉以后：做完了也不闪", h.flash_since is None)
h.v_flash.set(True)
h.on_option()

print("== 问的那个窗口")
dialogs.CloseAsk = real_ask
hides = []
h.hide = lambda: hides.append(1)
w = dialogs.CloseAsk(h)
w.remember.set(True)
w.pick("tray")
check("勾了「记住」再选收到托盘：记下来，菜单里的选项也跟着变", h.cfg["close_action"] == "tray" and h.v_close.get() == "tray" and hides == [1])
w = dialogs.CloseAsk(h)
w.pick("quit")
check("没勾「记住」：只这一次", h.cfg["close_action"] == "tray" and quits[-1] == 1 and len(quits) == 4)
h.root.destroy()

print("== 真的启动一个悬浮窗")
hud_dir = os.path.join(BOX, ".claude", "agent-hud")
os.makedirs(hud_dir, exist_ok=True)
with open(os.path.join(hud_dir, "config.json"), "w", encoding="utf-8") as f:
    json.dump({"rule_asked": True, "hidden": True}, f)
pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
p = subprocess.Popen([pyw if os.path.exists(pyw) else sys.executable, os.path.join(APP, "agent_hud.pyw"), "--no", "--exit-after", "9"],
                     env=dict(os.environ))
u = ctypes.WinDLL("user32")


def visible():
    found = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def each(hwnd, _):
        pid = wintypes.DWORD()
        u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value == p.pid and u.IsWindowVisible(hwnd):
            found.append(hwnd)
        return True

    u.EnumWindows(each, 0)
    return found


time.sleep(3.5)
check("上次藏着关的：启动后接着藏着", visible() == [], visible())
open(os.path.join(hud_dir, "show"), "w").close()   # 和用户再启动一次一样：留下 SHOW
time.sleep(2)
check("再启动一次：藏着的窗口回来了", len(visible()) >= 1)
check("自己退出", p.wait(timeout=30) == 0)
cfg = json.load(open(os.path.join(hud_dir, "config.json"), encoding="utf-8"))
log = os.path.join(hud_dir, "hud.log")
check("回来以后记成没藏着", cfg.get("hidden") is False, cfg.get("hidden"))
check("没有报错", not os.path.exists(log) or "Traceback" not in open(log, encoding="utf-8").read(),
      open(log, encoding="utf-8").read()[-500:] if os.path.exists(log) else "")
print("\n失败 %d 项：%s" % (len(fails), fails) if fails else "\n全部通过")
sys.exit(1 if fails else 0)
