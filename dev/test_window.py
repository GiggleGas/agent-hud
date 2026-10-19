"""窗口「不见了」的两种情况能不能自己恢复。在沙盒里起一个测试窗口（45 秒后自己退出），不影响正在跑的悬浮窗。

1. 被压到普通窗口下面（锁屏、UAC 之后会这样）：一两秒内自己回到最上面。
2. 跑到屏幕外面：再启动一次，窗口回到右上角；第二个实例自己退出，不会出现两个。
3. Foreign Tk windows and the glow must not count as duplicate HUDs; real duplicates must still be detected.

python test_window.py           测脚本版
python test_window.py <exe>     测打好的 exe
"""
import ctypes, json, os, shutil, subprocess, sys, tempfile, time
from ctypes import wintypes
from datetime import datetime, timezone
import tkinter as tk

sys.stdout.reconfigure(encoding="utf-8")
ctypes.windll.shcore.SetProcessDpiAwareness(1)
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CMD = [sys.argv[1]] if len(sys.argv) > 1 else [sys.executable, os.path.join(ROOT, "app", "agent_hud.pyw")]
BOX = tempfile.mkdtemp(prefix="agent-hud-window-")
ENV = dict(os.environ, AGENT_HUD_TEST_ROOT=BOX,
           CLAUDE_CONFIG_DIR=os.path.join(BOX, ".claude"), CODEX_HOME=os.path.join(BOX, ".codex"))
u = ctypes.WinDLL("user32")
u.GetWindow.restype = ctypes.c_void_p
u.GetWindow.argtypes = [ctypes.c_void_p, ctypes.c_uint]
u.SetWindowPos.argtypes = [ctypes.c_void_p, ctypes.c_void_p] + [ctypes.c_int] * 4 + [ctypes.c_uint]
u.IsWindowVisible.argtypes = [ctypes.c_void_p]
u.GetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int]
u.GetWindowRect.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.RECT)]
u.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
fails = []


def check(name, ok, detail=""):
    print(("  ok   " if ok else "  FAIL ") + name + ("" if ok else "  " + str(detail)))
    if not ok:
        fails.append(name)


def tk_windows():
    """Return visible Tk handles, process IDs and extended styles for diagnostics."""
    found = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def each(h, _):
        cls = ctypes.create_unicode_buffer(40)
        u.GetClassNameW(h, cls, 40)
        if cls.value == "TkTopLevel" and u.IsWindowVisible(h):
            pid = wintypes.DWORD()
            u.GetWindowThreadProcessId(h, ctypes.byref(pid))
            found.append((h, pid.value, u.GetWindowLongW(h, -20)))
        return True

    u.EnumWindows(each, 0)
    return found


def hud_windows(pid):
    """Count this process's interactive windows; WS_EX_TRANSPARENT marks the glow."""
    return {h for h, owner, style in tk_windows() if owner == pid and not style & 0x20}


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


# A real second window must still be detected. Keep one foreign window alive
# during the executable test so desktop activity cannot silently affect counts.
decoy = tk.Tk()
decoy.attributes("-alpha", 0.0)
duplicate = tk.Toplevel(decoy)
duplicate.attributes("-alpha", 0.0)
decoy.update()
check("同一进程的两个真正窗口都能检出", len(hud_windows(os.getpid())) == 2, tk_windows())
duplicate.destroy()
decoy.update()
check("关掉第二个窗口后计数恢复为一", len(hud_windows(os.getpid())) == 1, tk_windows())


def event(kind, payload):
    return json.dumps({"timestamp": datetime.now(timezone.utc).isoformat(), "type": kind, "payload": payload}) + "\n"


sessions = os.path.join(ENV["CODEX_HOME"], "sessions", "2026", "10", "19")
os.makedirs(sessions)
fixture = os.path.join(sessions, "rollout-window.jsonl")
with open(fixture, "w", encoding="utf-8") as f:
    f.write(event("session_meta", {"id": "window-test", "cwd": BOX, "source": "cli"}))
    f.write(event("event_msg", {"type": "task_started"}))
    f.write(event("event_msg", {"type": "user_message", "message": "window count fixture"}))

T0 = time.time()
p = subprocess.Popen(CMD + ["--no", "--exit-after", "45"], env=ENV)
try:
    h = None
    for _ in range(40):
        time.sleep(0.5)
        new = hud_windows(p.pid)
        if new:
            h = new.pop()
            break
    check("测试窗口起来了", h is not None, tk_windows())
    if h is None:
        raise RuntimeError("No HUD window for process %d" % p.pid)
    print("      启动到出窗口 %.1f 秒" % (time.time() - T0))
    time.sleep(1.5)
    home = rect(h)
    check("其他进程的 Tk 窗口不计入悬浮窗", hud_windows(p.pid) == {h} and bool(hud_windows(os.getpid())), tk_windows())
    with open(fixture, "a", encoding="utf-8") as f:
        f.write(event("event_msg", {"type": "task_complete", "last_agent_message": "done"}))

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
    for _ in range(20):
        if any(owner == p.pid and style & 0x20 for _, owner, style in tk_windows()):
            break
        time.sleep(0.5)
    check("完成提醒创建了真实的透明扫光层", any(owner == p.pid and style & 0x20 for _, owner, style in tk_windows()), tk_windows())
    check("扫光层出现后仍然只有一个主窗口", hud_windows(p.pid) == {h}, (p.pid, tk_windows()))
finally:
    decoy.destroy()
    p.wait(timeout=60)
    shutil.rmtree(BOX, ignore_errors=True)

print("\n失败 %d 项：%s" % (len(fails), fails) if fails else "\n全部通过")
sys.exit(1 if fails else 0)
