"""手机通知的回归测试：消息格式（飞书 / 钉钉 / 企业微信）、发送、哪些事件会发、链接、「手机访问…」窗口里的配置。
接收端是本机起的假服务，不往真的群里发东西。"""
import http.server, json, os, sys, tempfile, threading, time

sys.stdout.reconfigure(encoding="utf-8")
BOX = tempfile.mkdtemp(prefix="agent-hud-notify-")
os.environ.update(AGENT_HUD_TEST_ROOT=BOX, CLAUDE_CONFIG_DIR=os.path.join(BOX, ".claude"))
APP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app")
sys.path.insert(0, APP)
import importlib.machinery, importlib.util
loader = importlib.machinery.SourceFileLoader("agent_hud", os.path.join(APP, "agent_hud.pyw"))
spec = importlib.util.spec_from_loader("agent_hud", loader)
mod = importlib.util.module_from_spec(spec)
loader.exec_module(mod)
import dialogs, notify, web

fails = []


def check(name, ok, detail=""):
    print(("  ok   " if ok else "  FAIL ") + name + ("" if ok else "  " + str(detail)))
    if not ok:
        fails.append(name)


got, reply = [], {"body": {"code": 0}}


class Fake(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        got.append(json.loads(self.rfile.read(int(self.headers["Content-Length"])).decode("utf-8")))
        data = json.dumps(reply["body"]).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Fake)
threading.Thread(target=server.serve_forever, daemon=True).start()
URL = "http://127.0.0.1:%d/feishu.cn/hook" % server.server_port   # 地址里带 feishu.cn，按飞书的格式拼（假服务不看路径）

print("== 消息")
check("飞书的格式", notify.payload("https://open.feishu.cn/open-apis/bot/v2/hook/x", "hi") == {"msg_type": "text", "content": {"text": "hi"}})
check("钉钉 / 企业微信的格式", notify.payload("https://oapi.dingtalk.com/robot/send?access_token=x", "hi") == {"msgtype": "text", "text": {"content": "hi"}}
      and notify.payload("https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=x", "hi") == {"msgtype": "text", "text": {"content": "hi"}})
v = {"id": "s1", "ws": "agent-hud", "title": "加闪光", "last_text": "改好了，\n  已经装到你电脑上。", "action": "在问你问题，等你回答"}
m = notify.message("done", v, "http://10.0.0.2:8765/#/c/s1").splitlines()
check("做完了：开头带关键词「悬浮窗」、状态、项目、标题；下面是最后一句（压成一行）和链接",
      m == ["【Agent悬浮窗】✅ 做完了 · agent-hud · 加闪光", "改好了， 已经装到你电脑上。", "在手机上看：http://10.0.0.2:8765/#/c/s1"], m)
check("在等你回答：说的是在问什么", notify.message("ask", v).splitlines() == ["【Agent悬浮窗】❓ 在等你回答 · agent-hud · 加闪光", "在问你问题，等你回答"])
check("最后一句太长截断", len(notify.message("error", dict(v, last_text="字" * 500)).splitlines()[1]) == 201)

print("== 发送")
check("发出去了", notify.send(URL, "x") == (True, "发出去了") and got[-1] == {"msg_type": "text", "content": {"text": "x"}})
reply["body"] = {"code": 19024, "msg": "Key Words Not Found"}
ok, msg = notify.send(URL, "x")
check("群机器人不收（没带关键词）：说清楚", not ok and "关键词" in msg, msg)
reply["body"] = {"errcode": 310000, "errmsg": "keywords not in content"}
check("钉钉那种 errcode 也认", not notify.send(URL, "x")[0])
reply["body"] = {"code": 0}
check("连不上：说清楚", "连不上" in notify.send("http://127.0.0.1:1/x", "x", timeout=3)[1])

print("== 什么时候发")
h = mod.Hud(["--demo"])
h.root.withdraw()
pushed = []
h.push = lambda kind, view, url=None: pushed.append((kind, view["id"]))
done = dict(mod.demo_views(time.time())[0], id="d1", state="done", end=time.time(), last_text="好了")
h.on_event("finished", done, True)
check("没配地址：不发", pushed == [])
h.cfg["notify_url"] = URL
h.on_event("finished", done, False)
check("悬浮窗刚打开时就已经结束的：不发", pushed == [])
h.on_event("finished", dict(done, id="d2"), True)
h.on_event("finished", dict(done, id="d3", state="error"), True)
h.on_event("finished", dict(done, id="d4", state="interrupted"), True)
h.on_event("asking", dict(done, id="d5", state="ask"), True)
check("做完、出错、在等你回答：发；你自己打断的：不发", pushed == [("done", "d2"), ("error", "d3"), ("ask", "d5")], pushed)
pushed.clear()
h.cfg.update(notify_done=False)
h.on_event("finished", dict(done, id="d6"), True)
h.on_event("asking", dict(done, id="d7", state="ask"), True)
check("只勾了「在等你回答时」：做完的不发", pushed == [("ask", "d7")], pushed)
h.cfg.update(notify_done=True, notify_ask=False)
pushed.clear()
h.on_event("asking", dict(done, id="d8", state="ask"), True)
check("没勾「在等你回答时」：在问的不发", pushed == [])
h.cfg["notify_ask"] = True

del h.push   # 换回真的
h.web = type("W", (), {"server": object(), "port": 8765})()
n = len(got)
h.push("done", dict(done, id="d9", title="真的发一条"))
for _ in range(50):
    if len(got) > n:
        break
    time.sleep(0.05)
text = got[-1]["content"]["text"] if len(got) > n else ""
check("开着手机访问：消息里带直接打开这个对话的链接", "真的发一条" in text and "#/c/d9" in text and ":8765/" in text, text)

print("== 「手机访问…」窗口里的配置")
h.web = web.Web(h)
h.cfg["notify_url"] = ""
w = dialogs.WebWindow(h)
h.root.update()
check("说明里写着怎么配、关键词填「悬浮窗」", "自定义机器人" in dialogs.WebWindow.PUSH_HOW and "「悬浮窗」" in dialogs.WebWindow.PUSH_HOW)
w.test_push()
check("没填地址就点测试：提示先填", "先把" in str(w.push_note["text"]))
w.push_url.insert(0, URL)
w.push_ask.set(False)
n = len(got)
w.test_push()
for _ in range(100):
    h.root.update()
    if "发出去了" in str(w.push_note["text"]):
        break
    time.sleep(0.05)
check("发一条测试：发出去了，地址和勾选都记下了", "发出去了" in str(w.push_note["text"]) and len(got) == n + 1
      and "测试通知" in got[-1]["content"]["text"] and h.cfg["notify_url"] == URL and h.cfg["notify_ask"] is False, (w.push_note["text"], h.cfg))
w.push_url.delete(0, "end")
w.close()
check("清空地址再关窗口：记成不发", h.cfg["notify_url"] == "")
h.root.destroy()
server.shutdown()
print("\n失败 %d 项：%s" % (len(fails), fails) if fails else "\n全部通过")
sys.exit(1 if fails else 0)
