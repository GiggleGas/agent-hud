"""Claude 用量的回归测试：读凭据、问接口（本机假接口）、认两种返回格式、重置时间的写法、悬浮窗里那一行、托盘提示、手机网页。
不碰真的登录凭据，不联网。"""
import http.server, json, os, sys, tempfile, threading, time
from datetime import datetime, timedelta, timezone

sys.stdout.reconfigure(encoding="utf-8")
BOX = tempfile.mkdtemp(prefix="agent-hud-usage-")
CRED = os.path.join(BOX, "cred.json")
os.environ.update(AGENT_HUD_TEST_ROOT=BOX, CLAUDE_CONFIG_DIR=os.path.join(BOX, ".claude"), AGENT_HUD_CREDENTIALS=CRED)
APP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app")
sys.path.insert(0, APP)
import importlib.machinery, importlib.util
loader = importlib.machinery.SourceFileLoader("agent_hud", os.path.join(APP, "agent_hud.pyw"))
spec = importlib.util.spec_from_loader("agent_hud", loader)
mod = importlib.util.module_from_spec(spec)
loader.exec_module(mod)
import usage, web

fails = []


def check(name, ok, detail=""):
    print(("  ok   " if ok else "  FAIL ") + name + ("" if ok else "  " + str(detail)))
    if not ok:
        fails.append(name)


now = time.time()
iso = lambda t: datetime.fromtimestamp(t, timezone.utc).isoformat()
NEW = {"five_hour": {"utilization": 24.0, "resets_at": iso(now + 3600)},
       "limits": [{"kind": "weekly_scoped", "percent": 3, "resets_at": iso(now + 86400 * 3), "scope": {"model": {"display_name": "Fable"}}},
                  {"kind": "weekly_all", "percent": 56, "resets_at": iso(now + 86400 * 3)},
                  {"kind": "session", "percent": 24, "resets_at": iso(now + 3600), "scope": None}]}
OLD = {"five_hour": {"utilization": 91.0, "resets_at": iso(now + 600)}, "seven_day": {"utilization": 40.0, "resets_at": None},
       "seven_day_opus": None}
seen, reply = [], {"status": 200, "body": NEW}


class Fake(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        seen.append(dict(self.headers))
        data = json.dumps(reply["body"]).encode()
        self.send_response(reply["status"])
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Fake)
threading.Thread(target=server.serve_forever, daemon=True).start()
os.environ["AGENT_HUD_USAGE_URL"] = "http://127.0.0.1:%d/api/oauth/usage" % server.server_port


def write_cred(expires):
    with open(CRED, "w", encoding="utf-8") as f:
        json.dump({"claudeAiOauth": {"accessToken": "tok-123", "expiresAt": int(expires * 1000)}}, f)


print("== 读凭据、问接口")
check("没有凭据文件：什么都不显示", usage.fetch() is None)
write_cred(now - 60)
info = usage.fetch()
check("凭据过期了：说一句，不去续期", info and "过期" in info["error"] and not seen, info)
write_cred(now + 3600)
info = usage.fetch()
sent = {k.lower(): v for k, v in seen[-1].items()} if seen else {}   # 请求头名字不分大小写
check("带着令牌和 /usage 一样的请求头去问", sent.get("authorization") == "Bearer tok-123" and sent.get("anthropic-beta") == "oauth-2025-04-20", sent)
check("新格式：5 小时、本周排在前面，按模型的周额度在后", [(u["label"], u["percent"]) for u in info["limits"]] ==
      [("5 小时", 24.0), ("本周", 56.0), ("本周 · Fable", 3.0)], info)
check("重置时间换成时间戳", abs(info["limits"][0]["resets"] - (now + 3600)) < 1)
reply["body"] = OLD
info = usage.fetch()
check("老格式（只有 five_hour / seven_day）也认", [(u["label"], u["percent"], u["resets"]) for u in info["limits"]][1] == ("本周", 40.0, None)
      and info["limits"][0]["percent"] == 91.0, info)
reply.update(status=401, body={"error": "x"})
check("对方拒绝：说查不到", "401" in usage.fetch()["error"])
reply.update(status=200, body={"something": "else"})
check("格式认不出来：说一句", "认不出" in usage.fetch()["error"])
reply["body"] = NEW

print("== 重置时间怎么写")
base = datetime(2026, 10, 17, 11, 0).timestamp()   # 周六上午
check("今天：只写几点", usage.when(base + 4 * 3600, base) == "15:00")
check("明天：写「明天」", usage.when(base + 86400, base) == "明天 11:00")
check("再往后：写星期几", usage.when(datetime(2026, 10, 20, 22, 59).timestamp(), base) == "周二 22:59")
info = usage.fetch()
check("一句话的写法（托盘、手机用），只要前两项", usage.line(info, now).startswith("5 小时 24%（") and "本周 56%（" in usage.line(info, now)
      and "Fable" not in usage.line(info, now), usage.line(info, now))
check("出错时一句话就是出错的说明", usage.line({"error": "查不到用量（401）"}) == "查不到用量（401）")

print("== 悬浮窗")


class FakeTray:
    shown, tips = True, []

    def update(self, state, tip):
        self.tips.append(tip)


h = mod.Hud(["--demo"])
h.root.attributes("-alpha", 0.0)
h.usage = info
h.tray = FakeTray()
h.render()
h.root.update()
tags = {t for i in h.c.find_all() for t in h.c.gettags(i)}
check("标题栏下面多一行用量（点它会重新查）", "btn_usage" in tags)
tab_top = min(h.c.bbox(i)[1] for i in h.c.find_withtag("tab:recent")) / h.s
check("分栏按钮往下挪了一行", tab_top >= 34 + mod.USAGE_H, tab_top)
check("托盘提示里也有用量", "5 小时 24%" in h.tray.tips[-1], h.tray.tips[-1:])
h.usage_wake.clear()
h.drag = {"hit": "btn_usage", "moved": False}   # 和真的点了一下松开一样
h.on_release(None)
check("点用量那一行：马上再查一次", h.usage_wake.is_set())
h.usage = {"error": "查不到用量（401）", "at": now}
h.render()
texts = [h.c.itemcget(i, "text") for i in h.c.find_withtag("btn_usage") if h.c.type(i) == "text"]
check("查不到时那一行写原因", texts == ["查不到用量（401）"], texts)
h.usage = None
h.render()
tags = {t for i in h.c.find_all() for t in h.c.gettags(i)}
tab_top = min(h.c.bbox(i)[1] for i in h.c.find_withtag("tab:recent")) / h.s
check("没有凭据：不占那一行", "btn_usage" not in tags and tab_top < 34 + mod.USAGE_H, tab_top)
h.cfg["collapsed"] = True
h.usage = info
h.render()
check("收成一条时不显示用量", "btn_usage" not in {t for i in h.c.find_all() for t in h.c.gettags(i)})

print("== 手机网页")
h.reg = type("R", (), {"items": []})()
h.web = web.Web(h)
check("网页的状态里带着用量那句话", h.web.state()["usage"] == usage.line(info))
h.root.destroy()
server.shutdown()
print("\n失败 %d 项：%s" % (len(fails), fails) if fails else "\n全部通过")
sys.exit(1 if fails else 0)
