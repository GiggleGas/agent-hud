"""窗口「不见了」的两种情况能不能自己恢复。在沙盒里起一个测试窗口（45 秒后自己退出），不影响正在跑的悬浮窗。

1. 被压到普通窗口下面（锁屏、UAC 之后会这样）：一两秒内自己回到最上面。
2. 跑到屏幕外面：再启动一次，窗口回到右上角；第二个实例自己退出，不会出现两个。

python test_window.py           测脚本版
python test_window.py <exe>     测打好的 exe
"""
import ctypes, os, shutil, subprocess, sys, tempfile, time
from ctypes import wintypes

sys.stdout.reconfigure(encoding="utf-8")
ctypes.windll.shcore.SetProcessDpiAwareness(1)
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CMD = [sys.argv[1]] if len(sys.argv) > 1 else [sys.executable, os.path.join(ROOT, "app", "agent_hud.pyw")]
BOX = tempfile.mkdtemp(prefix="agent-hud-window-")
ENV = dict(os.environ, AGENT_HUD_TEST_ROOT=BOX)
u = ctypes.WinDLL("user32")
u.GetWindow.restype = ctypes.c_void_p
u.GetWindow.argtypes = [ctypes.c_void_p, ctypes.c_uint]
u.SetWindowPos.argtypes = [ctypes.c_void_p, ctypes.c_void_p] + [ctypes.c_int] * 4 + [ctypes.c_uint]
u.IsWindowVisible.argtypes = [ctypes.c_void_p]
u.GetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int]
u.GetWindowRect.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.RECT)]
fails = []


def check(name, ok, detail=""):
    print(("  ok   " if ok else "  FAIL ") + name + ("" if ok else "  " + str(detail)))
    if not ok:
        fails.append(name)


def hud_windows():
    """现在屏幕上所有的悬浮窗窗口。测试窗口 = 启动之后多出来的那个，正式在跑的那个不会被碰。"""
    found = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def each(h, _):
        cls = ctypes.create_unicode_buffer(40)
        u.GetClassNameW(h, cls, 40)
        if cls.value == "TkTopLevel" and u.IsWindowVisible(h):
            found.append(h)
        return True

    u.EnumWindows(each, 0)
    return set(found)


def covered(h):
    """压在它上面的、看得见的非置顶窗口有几个。"""
    n, a = 0, u.GetWindow(h, 3)
    while a:
        if u.IsWindowVisible(a) and not u.GetWindowLongW(a, -20) & 0x8:
            n += 1
        a = u.GetWindow(a, 3)
    return n


def rect(h):
    r = wintypes.RECT()
    u.GetWindowRect(h, ctypes.byref(r))
    return r.left, r.top


before = hud_windows()
T0 = time.time()
p = subprocess.Popen(CMD + ["--no", "--exit-after", "45"], env=ENV)   # exe 每次启动要解包好几秒，存活期留足
try:
    h = None
    for _ in range(40):
        time.sleep(0.5)
        new = hud_windows() - before
        if new:
            h = new.pop()
            break
    check("测试窗口起来了", h is not None)
    print("      启动到出窗口 %.1f 秒" % (time.time() - T0))
    time.sleep(1.5)
    home = rect(h)

    print("== 被压到普通窗口下面")
    u.SetWindowPos(h, 1, 0, 0, 0, 0, 0x13)   # HWND_BOTTOM：压到最底下，同时丢掉置顶
    n = covered(h)
    check("确实被压下去了", n > 0, n)
    time.sleep(2.5)
    check("两秒多之后自己回到最上面", covered(h) == 0, covered(h))
    check("置顶标记也恢复了", bool(u.GetWindowLongW(h, -20) & 0x8))

    print("== 跑到屏幕外面，再启动一次")
    u.SetWindowPos(h, 0, -6000, -6000, 0, 0, 0x0001 | 0x0004 | 0x0010)   # 只挪位置
    check("确实挪出屏幕了", rect(h) == (-6000, -6000), rect(h))
    t = time.time()
    again = subprocess.run(CMD, env=ENV, timeout=60)
    check("第二个实例自己退出", again.returncode == 0)
    print("      第二次启动到退出 %.1f 秒；此时测试窗口还活着: %s" % (time.time() - t, p.poll() is None))
    time.sleep(2.5)
    check("窗口回到了右上角", rect(h) == home, (rect(h), home))
    check("仍然只有一个窗口", len(hud_windows() - before) == 1)
finally:
    p.wait(timeout=60)
    shutil.rmtree(BOX, ignore_errors=True)

print("\n失败 %d 项：%s" % (len(fails), fails) if fails else "\n全部通过")
sys.exit(1 if fails else 0)
