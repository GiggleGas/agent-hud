"""新版本提醒的回归测试：认表的链接、读多维表格（本机假飞书）、比版本、悬浮窗里那一行、「有新版本」窗口。不联网。"""
import http.server, json, os, sys, tempfile, threading, time
from urllib.parse import urlparse

sys.stdout.reconfigure(encoding="utf-8")
BOX = tempfile.mkdtemp(prefix="agent-hud-updates-")
os.environ.update(AGENT_HUD_TEST_ROOT=BOX, CLAUDE_CONFIG_DIR=os.path.join(BOX, ".claude"))
APP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app")
sys.path.insert(0, APP)
import importlib.machinery, importlib.util
loader = importlib.machinery.SourceFileLoader("agent_hud", os.path.join(APP, "agent_hud.pyw"))
spec = importlib.util.spec_from_loader("agent_hud", loader)
mod = importlib.util.module_from_spec(spec)
loader.exec_module(mod)
import dialogs, feedback_to, updates

fails = []


def check(name, ok, detail=""):
    print(("  ok   " if ok else "  FAIL ") + name + ("" if ok else "  " + str(detail)))
    if not ok:
        fails.append(name)


calls = []
import io, zipfile


def make_zip(version, frozen=False):
    """拼一个假的悬浮窗安装包：agent-hud/app/agent_hud.pyw 里写着版本号；exe 版的再带 exe 和 _internal。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("agent-hud/app/agent_hud.pyw", 'VERSION = "%s"\nprint("new")\n' % version)
        z.writestr("agent-hud/app/newmod.py", "x = 1\n")
        z.writestr("agent-hud/使用说明.md", "新说明")
        if frozen:
            z.writestr("agent-hud/Agent悬浮窗.exe", b"NEW-EXE")
            z.writestr("agent-hud/_internal/a.dll", b"NEW-DLL")
    return buf.getvalue()


ZIP = {"data": make_zip("3.10")}
ROWS = [{"fields": {"版本": "3.2", "说明": "老版本", "下载": {"link": "https://x/3.2", "text": "下载"}}},
        {"fields": {"版本": [{"type": "text", "text": "3.10"}], "说明": [{"type": "text", "text": "加了闪光\n修了跳转"}],
                    "下载": {"link": "https://x/3.10", "text": "agent-hud.zip"},
                    "安装包": [{"file_token": "boxPack310", "name": "agent-hud.zip", "size": 0,
                              "url": "https://open.feishu.cn/open-apis/drive/v1/medias/boxPack310/download"}]}},
        {"fields": {"版本": "3.9", "说明": "中间版本"}},
        {"fields": {"说明": "没填版本的行"}}]
reply = {"records": {"code": 0, "data": {"items": ROWS}}}


class Lark(http.server.BaseHTTPRequestHandler):
    def send_json(self, data):
        raw = json.dumps(data).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self):
        self.rfile.read(int(self.headers["Content-Length"]))
        calls.append(("POST", self.path))
        self.send_json({"code": 0, "tenant_access_token": "t-1", "expire": 7200})

    def do_GET(self):
        calls.append(("GET", self.path, self.headers.get("Authorization")))
        if "/drive/v1/medias/" in self.path:   # 下载附件
            data = ZIP["data"]
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        self.send_json(reply["records"])

    def log_message(self, *a):
        pass


lark = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Lark)
threading.Thread(target=lark.serve_forever, daemon=True).start()
os.environ["AGENT_HUD_FEISHU_BASE"] = "http://127.0.0.1:%d/open-apis" % lark.server_port
feedback_to.APP_ID, feedback_to.APP_SECRET = "cli_x", "secret"

print("== 认表、比版本")
feedback_to.UPDATE_TABLE = ""
check("没配表：不查", not updates.ready() and updates.check("3.2") is None and not calls)
feedback_to.UPDATE_TABLE = "https://abc.feishu.cn/base/BasCnTok123?table=tblXyz&view=vewA"
check("认得多维表格的链接", updates.table() == ("BasCnTok123", "tblXyz"))
check("版本号按数字比（3.10 比 3.9 新）", updates.version_tuple("3.10") > updates.version_tuple("3.9") > updates.version_tuple("v3.2"))
check("三段的版本号：1.0.1 比 1.0.0 新，1.1 比 1.0.9 新，1.0 和 1.0.0 算同一个",
      updates.version_tuple("1.0.1") > updates.version_tuple("1.0.0") and updates.version_tuple("1.1") > updates.version_tuple("1.0.9")
      and updates.version_tuple("1.0") == updates.version_tuple("1.0.0") and updates.version_tuple("2") > updates.version_tuple("1.9.9"))
check("单元格的值换成文字：文本片段、链接", updates.text([{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]) == "ab"
      and updates.text({"link": "https://x", "text": "下载"}) == "https://x")

print("== 读表")
info = updates.check("3.2")
get = [c for c in calls if c[0] == "GET"]
check("用应用的令牌读这张表的记录", get and get[-1][1].startswith("/open-apis/bitable/v1/apps/BasCnTok123/tables/tblXyz/records")
      and get[-1][2] == "Bearer t-1", get)
check("最新的是版本号最大的那一行，说明、下载链接、安装包都读出来",
      {k: info[k] for k in ("version", "notes", "link")} == {"version": "3.10", "notes": "加了闪光\n修了跳转", "link": "https://x/3.10"}
      and info["package"]["token"] == "boxPack310", info)
check("已经是最新：不提醒", updates.check("3.10") is None)
check("用户说过这个版本先不提醒：不提醒", updates.check("3.2", skip="3.10") is None)
reply["records"] = {"code": 99991672, "msg": "no permission"}
check("没权限 / 出错：安静地当没有新版本", updates.check("3.2") is None)
reply["records"] = {"code": 0, "data": {"items": ROWS}}

print("== 一键更新：下载、解压、核对")
import feedback, install, subprocess
seen = []
path = updates.download(info, progress=lambda d, t: seen.append((d, t)))
dl = [c for c in calls if "/drive/v1/medias/boxPack310/download" in c[1]]
check("用应用的令牌下载「安装包」附件，有进度", dl and dl[-1][2] == "Bearer t-1" and open(path, "rb").read() == ZIP["data"]
      and seen and seen[-1] == (len(ZIP["data"]), len(ZIP["data"])), (dl, seen[-1:]))
root = updates.prepare(path, "3.10")
check("解压出来、核对通过（脚本版要有 app）", os.path.isfile(os.path.join(root, "app", "agent_hud.pyw")) and os.path.basename(root) == "agent-hud", root)
try:
    updates.prepare(path, "3.11")
    check("包里的版本号不对：不认", False)
except feedback.AppError as e:
    check("包里的版本号不对：不认", "3.10" in str(e), e)
one = os.path.join(BOX, "one.zip")
open(one, "wb").write(make_zip("1.0.0"))
check("表里写 1.0、包里是 1.0.0：算对得上", os.path.isfile(os.path.join(updates.prepare(one, "1.0"), "app", "agent_hud.pyw")))
bad =os.path.join(BOX, "bad.zip")
open(bad, "wb").write(b"not a zip")
try:
    updates.prepare(bad, "3.10")
    check("下载下来不是 zip：不认", False)
except feedback.AppError as e:
    check("下载下来不是 zip：不认", "zip" in str(e), e)
install.FROZEN = True   # 装作 exe 版
try:
    updates.prepare(path, "3.10")
    check("exe 版的包里没有 exe：不认", False)
except feedback.AppError as e:
    check("exe 版的包里没有 exe：不认", "Agent悬浮窗.exe" in str(e), e)
ZIP["data"] = make_zip("3.10", frozen=True)
exe_root = updates.prepare(updates.download(info), "3.10")
install.FROZEN = False
check("exe 版的包：有 exe 和 _internal，核对通过", os.path.isfile(os.path.join(exe_root, "Agent悬浮窗.exe")))


def run_updater(mode, root, dst):
    """真的跑一遍更新脚本：等一个已经退出的进程，换文件，再启动一个会留下记号的小脚本（假装是悬浮窗）。"""
    mark = os.path.join(BOX, "started-%s.txt" % mode)
    starter = os.path.join(BOX, "starter.py")
    open(starter, "w", encoding="utf-8").write("import sys; open(sys.argv[1] if len(sys.argv) > 1 else %r, 'w').write('ok')\n" % mark)
    gone = subprocess.Popen([sys.executable, "-c", "pass"])
    gone.wait()
    updates.launch(root, wait_pid=gone.pid, mode=mode, dst=dst, command=(sys.executable, '"%s"' % starter))
    for _ in range(100):
        if os.path.exists(mark):
            return True
        time.sleep(0.2)
    return False


print("== 一键更新：真的跑一遍更新脚本")
dst = os.path.join(BOX, "installed")
os.makedirs(dst)
open(os.path.join(dst, "agent_hud.pyw"), "w").write('VERSION = "3.2"\n')
open(os.path.join(dst, "config.json"), "w").write("{}")
check("脚本版：换好文件以后把悬浮窗打开", run_updater("script", root, dst))
check("脚本版：程序文件换成新的（新加的文件也有），设置文件还在",
      'VERSION = "3.10"' in open(os.path.join(dst, "agent_hud.pyw")).read() and os.path.isfile(os.path.join(dst, "newmod.py"))
      and os.path.isfile(os.path.join(dst, "config.json")))
exe_dst = os.path.join(BOX, "exe-installed")
os.makedirs(os.path.join(exe_dst, "_internal"))
open(os.path.join(exe_dst, "Agent悬浮窗.exe"), "wb").write(b"OLD-EXE")
open(os.path.join(exe_dst, "_internal", "a.dll"), "wb").write(b"OLD-DLL")
open(os.path.join(exe_dst, "_internal", "stale.dll"), "wb").write(b"OLD")
open(os.path.join(exe_dst, "我的笔记.txt"), "w", encoding="utf-8").write("别删")
check("exe 版：换好文件以后把悬浮窗打开", run_updater("exe", exe_root, exe_dst))
check("exe 版：exe 换成新的，_internal 整个换成新的（旧版多出来的文件删掉），旁边用户自己的文件不动",
      open(os.path.join(exe_dst, "Agent悬浮窗.exe"), "rb").read() == b"NEW-EXE"
      and open(os.path.join(exe_dst, "_internal", "a.dll"), "rb").read() == b"NEW-DLL"
      and not os.path.exists(os.path.join(exe_dst, "_internal", "stale.dll")) and os.path.isfile(os.path.join(exe_dst, "我的笔记.txt")))
log = open(os.path.join(updates.STAGE, "update.log"), encoding="utf-8-sig").read()
check("更新脚本记了日志：换文件成了、启动了", log.count("copy ok") == 2 and log.count("started") == 2, log)

print("== 悬浮窗里")
h = mod.Hud(["--demo"])
h.root.attributes("-alpha", 0.0)
h.update_info = info
h.render()
h.root.update()
tags = {t for i in h.c.find_all() for t in h.c.gettags(i)}
check("标题栏下面多一行「有新版本」", "btn_update" in tags)
tab_top = min(h.c.bbox(i)[1] for i in h.c.find_withtag("tab:recent")) / h.s
check("分栏按钮往下挪", tab_top >= 34 + mod.UPDATE_H, tab_top)
opened = []
real = dialogs.UpdateWindow
dialogs.UpdateWindow = lambda hud, i: opened.append(i)
h.drag = {"hit": "btn_update", "moved": False}
h.on_release(None)
check("点它打开「有新版本」窗口", opened == [info])
dialogs.UpdateWindow = real
w = dialogs.UpdateWindow(h, info)
h.root.update()
started = []
real_start = os.startfile
os.startfile = lambda link: started.append(link)
w.download()
os.startfile = real_start
check("「去下载」：打开下载链接", started == ["https://x/3.10"])
applied = []
h.apply_update = lambda r, v: applied.append((r, v))
real_dl, real_prep = updates.download, updates.prepare
h.demo = False
w = dialogs.UpdateWindow(h, info)
check("有安装包：窗口里有「立即更新」", [str(b["text"]) for b in w.buttons][0] == "立即更新")
updates.download = lambda i, progress=None: (progress and progress(5 << 20, 14 << 20)) or path
updates.prepare = lambda p, v: root
w.update_now()
for _ in range(60):
    h.root.update()
    if applied:
        break
    time.sleep(0.05)
check("立即更新：下载、核对好了，交给更新脚本（悬浮窗随后退出）", applied == [(root, "3.10")], applied)
w.destroy()
w = dialogs.UpdateWindow(h, info)
updates.download = lambda i, progress=None: (_ for _ in ()).throw(feedback.AppError("下载失败（403）"))
w.update_now()
for _ in range(40):
    h.root.update()
    if "没更新成" in str(w.note["text"]):
        break
    time.sleep(0.05)
check("下载失败：说清原因、按钮能再点、不退出", "403" in str(w.note["text"]) and str(w.buttons[0]["state"]) == "normal" and len(applied) == 1,
      w.note["text"])
w.destroy()
updates.download, updates.prepare = real_dl, real_prep
left = [os.path.join(updates.STAGE, n) for n in ("3.10.zip", "3.10", "updater.ps1")]
check("（前提）下载的包、解压的文件夹、更新脚本都还在", all(os.path.exists(p) for p in left), os.listdir(updates.STAGE))
h.cfg["update_pending"] = "9.99"
h.check_updated()
check("重启后核对：没更新成，清掉记号，但下载的东西留着好查", h.cfg["update_pending"] == "" and all(os.path.exists(p) for p in left))
h.cfg["update_pending"] = mod.VERSION
h.check_updated()
check("重启后核对：确实更新到了，记一笔、清掉", h.cfg["update_pending"] == "")
check("更新成了：下载的包、解压的文件夹、更新脚本都删掉，只留 update.log", os.listdir(updates.STAGE) == ["update.log"], os.listdir(updates.STAGE))
w = dialogs.UpdateWindow(h, info)
w.skip()
h.root.update()
tags = {t for i in h.c.find_all() for t in h.c.gettags(i)}
check("「这个版本先不提醒」：记下来，那一行消失", h.cfg["update_skip"] == "3.10" and h.update_info is None and "btn_update" not in tags)
check("之后的检查也跳过这个版本", updates.check(mod.VERSION, h.cfg["update_skip"]) is None)
h.root.destroy()
lark.shutdown()
print("\n失败 %d 项：%s" % (len(fails), fails) if fails else "\n全部通过")
sys.exit(1 if fails else 0)
