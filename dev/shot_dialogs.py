"""把「发起任务」「反馈」「手机访问」三个窗口截下来看样子。在沙盒里起一个测试悬浮窗（12 秒后自己退出），
数据是 fake_claude.py 造的一个后台任务，不碰真实环境。截图存在系统临时目录。要装 Pillow。"""
import ctypes, json, os, shutil, subprocess, sys, tempfile, time, uuid
from ctypes import wintypes
from PIL import ImageGrab

sys.stdout.reconfigure(encoding="utf-8")
ctypes.windll.shcore.SetProcessDpiAwareness(1)
DEV = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(os.path.dirname(DEV), "app")
BOX = tempfile.mkdtemp(prefix="agent-hud-dialogs-")
env = dict(os.environ, AGENT_HUD_TEST_ROOT=BOX, CLAUDE_CONFIG_DIR=os.path.join(BOX, ".claude"),
           AGENT_HUD_CLAUDE=os.path.join(DEV, "fake_claude.py"), PYTHONIOENCODING="utf-8")
hud_dir = os.path.join(BOX, ".claude", "agent-hud")
os.makedirs(hud_dir)
names = ("shop-server", "web-admin", "pay-gateway")
for n in names:
    os.makedirs(os.path.join(BOX, "work", n))
with open(os.path.join(hud_dir, "workspaces.json"), "w", encoding="utf-8") as f:
    json.dump([{"name": n, "path": os.path.join(BOX, "work", n)} for n in names], f)
sid = str(uuid.uuid4())
ws = os.path.join(BOX, "work", "pay-gateway")
with open(os.path.join(hud_dir, "tasks.json"), "w", encoding="utf-8") as f:
    json.dump({sid: {"ws": ws, "prompt": "排查支付回调偶发超时", "created": time.time(), "origin": "hud"}}, f)
subprocess.run([sys.executable, os.path.join(DEV, "fake_claude.py"), "-p", "--session-id", sid, "--permission-mode", "auto"],
               input="排查支付回调偶发超时，先看日志里超时集中在什么时间。".encode("utf-8"), cwd=ws, env=env, check=True)

u = ctypes.WinDLL("user32")
u.GetWindowRect.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.RECT)]
u.FindWindowW.restype = ctypes.c_void_p
try:
    for flag, name in (("--compose", "compose"), ("--feedback", "feedback"), ("--web", "web")):   # 几个窗口出现在同一个位置，分开开
        p = subprocess.Popen([sys.executable, os.path.join(APP, "agent_hud.pyw"), flag, "--exit-after", "9"], env=env)
        time.sleep(6)
        found = []

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def each(h, _):
            t = ctypes.create_unicode_buffer(200)
            u.GetWindowTextW(h, t, 200)
            if u.IsWindowVisible(h) and (t.value in ("发起任务", "反馈", "手机访问") or t.value.startswith("pay-gateway · ")):
                found.append((h, t.value))
            return True

        u.EnumWindows(each, 0)
        for h, title in found:
            r = wintypes.RECT()
            u.GetWindowRect(h, ctypes.byref(r))
            out = os.path.join(tempfile.gettempdir(), "hud-dialog-%s.png" % name)
            ImageGrab.grab(bbox=(r.left, r.top, r.right, r.bottom), include_layered_windows=True, all_screens=True).save(out)
            print(title, "->", out)
        p.wait(timeout=60)
    log = os.path.join(hud_dir, "hud.log")
    print("报错日志：", open(log, encoding="utf-8").read()[-1500:] if os.path.exists(log) else "无")
finally:
    shutil.rmtree(BOX, ignore_errors=True)
