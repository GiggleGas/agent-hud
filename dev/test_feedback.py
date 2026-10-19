"""反馈的回归测试：拼消息、发送成功 / 被拒 / 连不上、没发出去的存在本机、反馈窗口。
接收端是本机起的一个假服务，不会往真的飞书群里发东西。"""
import http.server, json, os, sys, tempfile, threading, time

sys.stdout.reconfigure(encoding="utf-8")
BOX = tempfile.mkdtemp(prefix="agent-hud-feedback-")
os.environ.update(AGENT_HUD_TEST_ROOT=BOX, CLAUDE_CONFIG_DIR=os.path.join(BOX, ".claude"))
APP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app")
sys.path.insert(0, APP)
import importlib.machinery, importlib.util
loader = importlib.machinery.SourceFileLoader("agent_hud", os.path.join(APP, "agent_hud.pyw"))
spec = importlib.util.spec_from_loader("agent_hud", loader)
mod = importlib.util.module_from_spec(spec)
loader.exec_module(mod)
import dialogs, feedback, feedback_to, install
feedback_to.APP_ID = feedback_to.APP_SECRET = ""   # 配置里是真的飞书应用；测试先当没配，用到时填假的、指到假飞书

fails = []


def check(name, ok, detail=""):
    print(("  ok   " if ok else "  FAIL ") + name + ("" if ok else "  " + str(detail)))
    if not ok:
        fails.append(name)


# 假的接收端：记下收到的，按 reply 回话
got, reply = [], {"status": 200, "body": {"code": 0, "msg": "success"}}


class Fake(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        got.append(json.loads(self.rfile.read(int(self.headers["Content-Length"])).decode("utf-8")))
        data = json.dumps(reply["body"]).encode()
        self.send_response(reply["status"])
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Fake)
threading.Thread(target=server.serve_forever, daemon=True).start()
os.environ["AGENT_HUD_FEEDBACK_URL"] = "http://127.0.0.1:%d/hook" % server.server_port
kept = lambda: sorted(os.listdir(feedback.DIR)) if os.path.isdir(feedback.DIR) else []

print("== 拼消息")
body = feedback.compose("  双击跳不过去  ", "小王", "3.1", ["附加的一行"])
lines = body.splitlines()
check("开头带群机器人要的关键词和产品名", lines[0] == "【%s】【Agent悬浮窗】3.1" % feedback_to.KEYWORD, lines[0])
check("带上称呼和电脑用户名", lines[1] == "来自：小王（电脑用户名 %s）" % feedback.user(), lines[1])
check("正文去掉首尾空白，夹在分隔线中间，后面是附加的现场", lines[2:] == ["————", "双击跳不过去", "————", "附加的一行"], lines[2:])
check("称呼没填就用电脑用户名", feedback.compose("x", "", "3.1").splitlines()[1] == "来自：" + feedback.user())
long = feedback.compose("字" * 5000, "", "3.1")
check("正文太长截断", "字" * feedback.MAX_TEXT + "…" in long and "字" * (feedback.MAX_TEXT + 1) not in long)
ctx = feedback.context("3.1", 1.5)
check("现场里有版本、系统、缩放", len(ctx) == 1 and "3.1" in ctx[0] and "Windows" in ctx[0] and "150%" in ctx[0], ctx)
os.makedirs(install.TARGET, exist_ok=True)
with open(os.path.join(install.TARGET, "hud.log"), "w", encoding="utf-8") as f:
    f.write("".join("2026-10-17 10:%02d 第 %d 行日志 %s\n" % (i % 60, i, "x" * 80) for i in range(500)))
ctx = feedback.context("3.1", 1.0, with_log=True)
check("勾了日志：附上最近的，不超过上限", len(ctx) == 2 and "第 499 行" in ctx[1] and len(ctx[1]) <= feedback.LOG_CHARS + 20, len(ctx[1]))
check("飞书地址用飞书的格式", feedback.payload("https://open.feishu.cn/open-apis/bot/v2/hook/x", "hi") == {"msg_type": "text", "content": {"text": "hi"}})

print("== 发送")
ok, msg = feedback.send(body)
check("发出去了", ok and msg == "发出去了，谢谢！" and got and got[-1]["text"] == body, (ok, msg, got[-1:]))
check("发出去了就不在本机留底", kept() == [], kept())
reply["body"] = {"code": 19024, "msg": "Key Words Not Found"}
ok, msg = feedback.send(body)
check("对方回 200 但说不收：算没发出去，说清原因", not ok and "关键词" in msg and "19024" in msg, msg)
check("没发出去的存在本机，说了存在哪", len(kept()) == 1 and kept()[0] in msg, (kept(), msg))
time.sleep(1.1)   # 本机留底按秒命名
reply.update(status=500, body={"msg": "boom"})
ok, msg = feedback.send(body)
check("对方出错：没发出去", not ok and "对方回了 500" in msg, msg)
os.environ["AGENT_HUD_FEEDBACK_URL"] = "http://127.0.0.1:1/hook"
ok, msg = feedback.send(body, timeout=3)
check("连不上：没发出去", not ok and "连不上" in msg, msg)
os.environ["AGENT_HUD_FEEDBACK_URL"] = "http://127.0.0.1:%d/hook" % server.server_port
reply.update(status=200, body={"code": 0})

print("== 反馈窗口")
h = mod.Hud(["--demo"])
h.root.withdraw()
h.demo = False   # 假数据模式下窗口不发送；这里要真的走一遍发送
w = dialogs.Feedback(h)
h.root.update()
check("称呼默认是电脑用户名", w.who.get() == feedback.user(), w.who.get())
n = len(got)
w.send()
h.root.update()
check("什么都没写：不发", len(got) == n, len(got))
w.body.insert("1.0", "希望能按项目筛选")
w.who.delete(0, "end")
w.who.insert(0, "小李")
w.log.set(True)
w.send()
check("发送时按钮变灰，免得连点两次", str(w.button["state"]) == "disabled")
for _ in range(100):
    h.root.update()
    if len(got) == n + 1 and "发出去了" in str(w.note["text"]):
        break
    time.sleep(0.05)
sent = got[-1]["text"] if len(got) == n + 1 else ""
check("窗口里发出去的：正文、称呼、日志都在", "希望能按项目筛选" in sent and "来自：小李" in sent and "最近的运行日志" in sent, sent[:300])
check("记住称呼，下次不用再填", h.cfg.get("feedback_name") == "小李")
check("提示发出去了", "发出去了" in str(w.note["text"]), w.note["text"])
end = time.time() + 3
while time.time() < end and w.winfo_exists():
    h.root.update()
    time.sleep(0.05)
check("发出去后窗口自己关掉", not w.winfo_exists())
h.demo = True
w = dialogs.Feedback(h)
w.body.insert("1.0", "x")
w.send()
check("假数据模式下不发送", len(got) == n + 1 and "不发送" in str(w.note["text"]))
w.destroy()

print("== 图片：编码和剪贴板格式")
import io, struct
from PIL import Image
pixels = lambda im: [[im.getpixel((x, y)) for x in range(im.width)] for y in range(im.height)]
px = [[(255, 0, 0, 255), (0, 255, 0, 255), (0, 0, 255, 128)], [(10, 20, 30, 255), (40, 50, 60, 255), (70, 80, 90, 255)]]
im = Image.open(io.BytesIO(feedback.png(3, 2, [bytes(c for p in row for c in p) for row in px]))).convert("RGBA")
check("自己编码的 PNG 能被别的程序读，像素一样", pixels(im) == px)


def dib(w, h, bpp, rows_bgra, comp=0, top_down=False):
    """拼一个剪贴板里那种位图：BITMAPINFOHEADER（+ 颜色掩码）+ 从下往上（或从上往下）的像素，每行补齐到 4 字节。"""
    stride = (w * bpp + 31) // 32 * 4
    head = struct.pack("<IiiHHIIiiII", 40, w, -h if top_down else h, 1, bpp, comp, 0, 0, 0, 0, 0)
    if comp == 3:
        head += struct.pack("<III", 0xFF0000, 0xFF00, 0xFF)
    order = rows_bgra if top_down else rows_bgra[::-1]
    data = b"".join(bytes(c for p in r for c in (p if bpp == 32 else p[:3])).ljust(stride, b"\0") for r in order)
    return head + data


bgra = [[(0, 0, 255, 0), (0, 255, 0, 0), (255, 0, 0, 0)], [(30, 20, 10, 0), (60, 50, 40, 0), (90, 80, 70, 0)]]
want = [[(255, 0, 0, 255), (0, 255, 0, 255), (0, 0, 255, 255)], [(10, 20, 30, 255), (40, 50, 60, 255), (70, 80, 90, 255)]]
for name, d in (("32 位、从下往上、透明度全 0（截图常见）", dib(3, 2, 32, bgra)), ("24 位（每行要补齐）", dib(3, 2, 24, bgra)),
                ("32 位、带颜色掩码", dib(3, 2, 32, bgra, comp=3)), ("从上往下存的", dib(3, 2, 32, bgra, top_down=True))):
    im = Image.open(io.BytesIO(feedback.dib_to_png(d))).convert("RGBA")
    check("截图转 PNG：" + name, pixels(im) == want, pixels(im))
odd = dib(3, 2, 32, bgra)[:14] + struct.pack("<H", 16) + dib(3, 2, 32, bgra)[16:]
check("认不出的格式不转（存成 BMP）", feedback.dib_to_png(odd) is None and feedback.bmp(dib(3, 2, 24, bgra))[:2] == b"BM")
check("BMP 能被别的程序读", Image.open(io.BytesIO(feedback.bmp(dib(3, 2, 24, bgra)))).size == (3, 2))
folder = feedback.new_folder()
src = os.path.join(BOX, "pic.png")
with open(src, "wb") as f:
    f.write(feedback.png(3, 2, [bytes(c for p in row for c in p) for row in px]))
a, b = feedback.add_file(folder, src), feedback.add_file(folder, src)
check("选的图片复制进这次的文件夹，重名的自动改名", os.path.dirname(a) == folder and os.path.basename(b) == "pic-2.png", (a, b))
check("不是图片的文件不收", feedback.add_file(folder, os.path.join(install.TARGET, "hud.log")) is None)
jpg = os.path.join(BOX, "pic.jpg")
Image.new("RGB", (40, 30), (200, 30, 30)).save(jpg)
for p in (a, jpg):
    hbm = feedback.load_bitmap(p)
    check("Windows 自带的 GDI+ 能读 %s，转成位图（复制进剪贴板用）" % os.path.splitext(p)[1], bool(hbm))
    import ctypes
    ctypes.windll.gdi32.DeleteObject(ctypes.c_void_p(hbm))
check("读不了的文件返回空", feedback.load_bitmap(os.path.join(install.TARGET, "hud.log")) is None)

print("== 图片：反馈窗口")
h = mod.Hud(["--demo"])
h.root.withdraw()
h.demo = False
real_paste, real_copy = feedback.paste_images, feedback.copy_image
copied = []
try:
    w = dialogs.Feedback(h)
    feedback.paste_images = lambda folder: []
    check("剪贴板里是文字：照常粘贴（不拦）", w.paste() is None and w.images == [])
    feedback.paste_images = lambda folder: [feedback.add_file(folder, src)]
    w.body.focus_force()
    check("剪贴板里是图片：收下，不往输入框里贴", w.paste() == "break" and len(w.images) == 1 and w.body.get("1.0", "end").strip() == "")
    h.root.update()
    check("显示缩略图（大图缩小）", len(w.photos) == 1 and w.photos[0].width() <= dialogs.Feedback.THUMB[0], w.photos)
    w.paste()
    w.drop(w.images[0])
    check("「移除」去掉一张", len(w.images) == 1 and len(w.photos) == 1)
    feedback.copy_image = lambda path, hwnd: copied.append(path) or True
    w.paste()   # 一共两张
    w.body.insert("1.0", "看图")
    n = len(got)
    w.send()
    for _ in range(100):
        h.root.update()
        if "Ctrl+V" in str(w.note["text"]):
            break
        time.sleep(0.05)
    check("文字照常发，里面写着附了几张图", len(got) == n + 1 and "附图 2 张" in got[-1]["text"], got[-1:])
    check("发完把第一张图片放进剪贴板，醒目地告诉用户还要自己去飞书里粘贴",
          copied == [w.images[0]] and "第 1/2 张" in str(w.note["text"]) and "复制下一张" in str(w.note["text"]), w.note["text"])
    check("有图片时窗口不自己关，多出「打开图片文件夹」「复制下一张」，按钮变成「关闭」",
          w.winfo_exists() and w.folder_button.winfo_manager() == "pack" and w.next_button.winfo_manager() == "pack"
          and str(w.close_button["text"]) == "关闭")
    w.copy_next()
    check("「复制下一张」：放第二张，说这是最后一张", copied == w.images and "第 2/2 张" in str(w.note["text"]) and "最后一张" in str(w.note["text"]))
    feedback.copy_image = lambda path, hwnd: False
    w.copy_next()
    check("放不进剪贴板：让用户打开图片文件夹拖过去", "打开图片文件夹" in str(w.note["text"]))
    w.destroy()
finally:
    feedback.paste_images, feedback.copy_image = real_paste, real_copy
h.root.destroy()

print("== 用飞书应用传图，群机器人发图文消息")
from urllib.parse import urlparse
calls = []


class Lark(http.server.BaseHTTPRequestHandler):
    """假的飞书开放平台：换令牌、传图片。"""

    def reply(self, data):
        raw = json.dumps(data).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        path = urlparse(self.path).path
        calls.append(("POST", self.path, self.headers.get("Authorization"), body))
        if path.endswith("/tenant_access_token/internal"):
            ok = json.loads(body)["app_secret"] == "good"
            self.reply({"code": 0, "tenant_access_token": "t-1", "expire": 7200} if ok else {"code": 10014, "msg": "app secret invalid"})
        else:
            self.reply({"code": 0, "data": {"image_key": "img_%d" % sum(1 for c in calls if c[1].endswith("/im/v1/images"))}})

    def log_message(self, *a):
        pass


lark = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Lark)
threading.Thread(target=lark.serve_forever, daemon=True).start()
os.environ["AGENT_HUD_FEISHU_BASE"] = "http://127.0.0.1:%d/open-apis" % lark.server_port
# 地址里带 feishu.cn，好让消息拼成飞书群机器人的格式（假接收端不看路径）
os.environ["AGENT_HUD_FEEDBACK_URL"] = "http://127.0.0.1:%d/feishu.cn/hook" % server.server_port
pics = [feedback.add_file(feedback.new_folder(), src), jpg]
body = feedback.compose("按钮点不动", "小王", "3.1", ["Windows 10"])
n = len(got)
ok, msg, sent = feedback.deliver(body, pics)
check("没配应用：文字走群机器人，写上附了几张图，图片要人自己贴",
      ok and not sent and len(got) == n + 1 and got[-1]["msg_type"] == "text" and "附图 2 张" in got[-1]["content"]["text"] and not calls, (ok, msg, sent))
feedback_to.APP_ID, feedback_to.APP_SECRET = "cli_x", "good"
ok, msg, sent = feedback.deliver(body, pics)
token = json.loads(calls[0][3]) if calls else {}
ups = [c for c in calls if c[1].endswith("/im/v1/images")]
check("配了应用：用 App ID / Secret 换令牌", token == {"app_id": "cli_x", "app_secret": "good"}, token)
check("两张图都用令牌上传（类型 message，文件名只用英文）", len(ups) == 2 and all(c[2] == "Bearer t-1" for c in ups)
      and b'name="image_type"\r\n\r\nmessage' in ups[0][3] and b'filename="image1.png"' in ups[0][3] and b"\x89PNG" in ups[0][3]
      and b'filename="image2.jpg"' in ups[1][3], [c[3][:200] for c in ups])
post = got[-1].get("content", {}).get("post", {}).get("zh_cn", {}) if len(got) == n + 2 else {}
check("群机器人发一条图文消息：第一行当标题（带关键词），正文一行一段，两张图在后面",
      got[-1]["msg_type"] == "post" and post["title"] == body.splitlines()[0] and feedback_to.KEYWORD in post["title"]
      and post["content"][-2:] == [[{"tag": "img", "image_key": "img_1"}], [{"tag": "img", "image_key": "img_2"}]]
      and [p[0]["text"] for p in post["content"][:-2]] == body.splitlines()[1:], got[-1])
check("说图片也一起发过去了，不再写「附图会另外发」", ok and sent and "图片也一起发过去了" in msg and "附图" not in json.dumps(got[-1], ensure_ascii=False),
      (ok, msg, sent))
calls.clear()
feedback.deliver(body, [])
check("没带图片：不找应用，照常发文字", not calls and got[-1]["msg_type"] == "text")
feedback_to.APP_SECRET = "bad"
ok, msg, sent = feedback.deliver(body, pics)
check("传图出错（Secret 不对）：退回发文字，说清哪里不对，图片要人自己贴",
      ok and not sent and "App ID 或 App Secret 不对" in msg and "附图 2 张" in got[-1]["content"]["text"], (ok, msg, sent))
feedback_to.APP_SECRET = "good"

h = mod.Hud(["--demo"])
h.root.withdraw()
h.demo = False
w = dialogs.Feedback(h)
hint = " ".join(str(c["text"]) for c in w.winfo_children()[2].winfo_children() if c.winfo_class() == "TLabel")
check("配了应用：窗口里不再说「图片要自己贴」", "发不进群" not in hint, hint)
w.images = [pics[0]]
w.body.insert("1.0", "带图")
w.send()
for _ in range(100):
    h.root.update()
    if not w.winfo_exists() or "图片也一起" in str(w.note["text"]):
        break
    time.sleep(0.05)
check("图片一起发出去了：提示一下，窗口自己关", "图片也一起" in str(w.note["text"]))
end = time.time() + 3
while time.time() < end and w.winfo_exists():
    h.root.update()
    time.sleep(0.05)
check("窗口自己关了", not w.winfo_exists())
h.root.destroy()
feedback_to.APP_ID = feedback_to.APP_SECRET = ""
lark.shutdown()
server.shutdown()
print("\n失败 %d 项：%s" % (len(fails), fails) if fails else "\n全部通过")
sys.exit(1 if fails else 0)
