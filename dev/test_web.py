"""手机网页的回归测试：钥匙、列表数据、对话内容、回复走哪条路、发起任务。
在本机起一个真的网页服务（随机端口），悬浮窗、后台任务、替你打字都换成假的，不弹窗口、不碰编辑器、不花额度。"""
import http.cookiejar, json, os, sys, tempfile, time, urllib.error, urllib.request

sys.stdout.reconfigure(encoding="utf-8")
BOX = tempfile.mkdtemp(prefix="agent-hud-web-")
os.environ.update(AGENT_HUD_TEST_ROOT=BOX, CLAUDE_CONFIG_DIR=os.path.join(BOX, ".claude"))
APP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app")
sys.path.insert(0, APP)
import importlib.machinery, importlib.util
loader = importlib.machinery.SourceFileLoader("agent_hud", os.path.join(APP, "agent_hud.pyw"))
spec = importlib.util.spec_from_loader("agent_hud", loader)
mod = importlib.util.module_from_spec(spec)
loader.exec_module(mod)
import collector, runner, web, workspaces

fails = []


def check(name, ok, detail=""):
    print(("  ok   " if ok else "  FAIL ") + name + ("" if ok else "  " + str(detail)))
    if not ok:
        fails.append(name)


# 一个假的对话记录：两轮，第二轮中间说过一句、最后又说了一句
proj = os.path.join(BOX, ".claude", "projects", "f--Work-Game")
os.makedirs(proj)
sid = "11111111-2222-3333-4444-555555555555"
now = time.time()


def iso(t):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(t, timezone.utc).isoformat().replace("+00:00", "Z")


entries = [
    {"type": "user", "origin": {"kind": "human"}, "permissionMode": "auto", "timestamp": iso(now - 100), "message": {"role": "user", "content": "第一个问题"}},
    {"type": "assistant", "timestamp": iso(now - 90), "message": {"role": "assistant", "stop_reason": "end_turn", "content": [{"type": "text", "text": "第一个回答"}]}},
    {"type": "user", "origin": {"kind": "human"}, "permissionMode": "auto", "timestamp": iso(now - 60), "message": {"role": "user", "content": [{"type": "text", "text": "第二个问题"}]}},
    {"type": "assistant", "timestamp": iso(now - 55), "message": {"role": "assistant", "stop_reason": "tool_use", "content": [{"type": "text", "text": "我先看看文件"}, {"type": "tool_use", "id": "t1", "name": "Read", "input": {"file_path": "a.txt"}}]}},
    {"type": "user", "timestamp": iso(now - 54), "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "x"}]}},
    {"type": "user", "isMeta": True, "timestamp": iso(now - 53), "message": {"role": "user", "content": "系统塞进来的说明"}},
    {"type": "assistant", "timestamp": iso(now - 50), "message": {"role": "assistant", "stop_reason": "end_turn", "content": [{"type": "text", "text": "**改好了**，见 `a.txt`。"}]}},
]
queued = [   # 它正忙的时候用户又说了一句：记成「排队的命令」附件
    {"type": "user", "origin": {"kind": "human"}, "permissionMode": "auto", "timestamp": iso(now - 40), "message": {"role": "user", "content": "第三个问题"}},
    {"type": "attachment", "timestamp": iso(now - 35), "attachment": {"type": "queued_command", "prompt": [{"type": "text", "text": "忙的时候插一句"}], "commandMode": "prompt", "origin": {"kind": "human"}}},
    {"type": "queue-operation", "operation": "remove", "timestamp": iso(now - 34), "content": "忙的时候插一句"},
    {"type": "assistant", "timestamp": iso(now - 30), "message": {"role": "assistant", "stop_reason": "end_turn", "content": [{"type": "text", "text": "两件都办了"}]}},
]
with open(os.path.join(proj, sid + ".jsonl"), "w", encoding="utf-8") as f:
    for e in entries:
        f.write(json.dumps(e, ensure_ascii=False) + "\n")

print("== 对话内容")
msgs = collector.conversation(sid)
check("一问一答排好，系统说明和工具结果不算", [(m["role"], m["text"]) for m in msgs] ==
      [("user", "第一个问题"), ("assistant", "第一个回答"), ("user", "第二个问题"), ("assistant", "**改好了**，见 `a.txt`。")], msgs)
check("一轮里中间说的话放进「过程」，最后一句当结果", msgs[-1]["steps"] == ["我先看看文件"], msgs[-1])
check("只要最近几轮", [m["text"] for m in collector.conversation(sid, turns=1)] == ["第二个问题", "**改好了**，见 `a.txt`。"])
path = os.path.join(proj, sid + ".jsonl")
start = os.path.getsize(path)
with open(path, "a", encoding="utf-8") as f:
    for e in queued:
        f.write(json.dumps(e, ensure_ascii=False) + "\n")
check("它忙的时候插的话也显示在对话里", [m["text"] for m in collector.conversation(sid)][-3:] == ["第三个问题", "忙的时候插一句", "两件都办了"],
      [m["text"] for m in collector.conversation(sid)])
check("核对「这句进没进对话」也认得排队的那种", runner.said(path, start, "忙的时候插一句", 2) and not runner.said(path, start, "没说过的话", 1))
with open(path, "w", encoding="utf-8") as f:   # 后面的测试用回原来那份
    for e in entries:
        f.write(json.dumps(e, ensure_ascii=False) + "\n")

# 假悬浮窗：只要网页用到的那几样
h = mod.Hud(["--demo"])
h.root.withdraw()
ts = time.time()
views = mod.demo_views(ts)
h.views = [v for v in views if v["state"] not in mod.hub.ENDED]
h.history = [v for v in views if v["state"] in mod.hub.ENDED] + [dict(views[0], id=sid, title="假的对话", state="done", end=ts - 50, last=ts - 50, root=BOX)]
h.reg = workspaces.Registry(os.path.join(BOX, "ws.json"))
h.reg._add(os.path.join(BOX, "shop-server"))


class Runner:
    def __init__(self):
        self.calls = []

    def follow(self, s, t):
        self.calls.append(("follow", s, t))
        return "sent"

    def start(self, ws, t, origin):
        self.calls.append(("start", ws, t, origin))
        return "new-task-id"


h.runner = Runner()
typed = []
runner.type_into_editor = lambda s, root, t, path: (typed.append((s, root, t, path)) or (True, "已经在电脑上的编辑器里发出去了。"))
held = set()
runner.editor_holds = lambda s: s in held
h.cfg["web_port"] = 18765
w = web.Web(h)
h.web = w
check("服务起来了", w.start())
base = "http://127.0.0.1:%d" % w.port


def get(path, opener=None, data=None):
    req = urllib.request.Request(base + path, data=json.dumps(data).encode() if data is not None else None,
                                 headers={"Content-Type": "application/json"})
    try:
        r = (opener or urllib.request.build_opener(NoRedirect)).open(req, timeout=10)
        return r.status, r.read().decode("utf-8"), r
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8"), e


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


print("== 钥匙")
check("没钥匙打不开首页", get("/")[0] == 403)
check("没钥匙拿不到数据", get("/api/state")[0] == 403)
check("钥匙不对也不行", get("/api/state?k=wrong")[0] == 403)
code, body, r = get("/?k=" + w.key())
check("带钥匙打开：记进 cookie，再跳到不带钥匙的地址", code == 302 and "hudk=" + w.key() in r.headers.get("Set-Cookie", "") and r.headers["Location"] == "/", (code, r.headers))
jar = http.cookiejar.CookieJar()
phone = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
check("之后不带钥匙也能用（靠 cookie）", get("/?k=" + w.key(), phone)[0] == 200 and get("/api/state", phone)[0] == 200)
old = w.key()
w.new_key()
check("换钥匙后旧的立刻失效", get("/api/state?k=" + old)[0] == 403 and get("/api/state", phone)[0] == 403)
get("/?k=" + w.key(), phone)

print("== 列表")
state = json.loads(get("/api/state", phone)[1])
check("在跑的、24 小时内结束的、按项目、项目清单都有", {"live", "recent", "projects", "workspaces"} <= set(state) and len(state["live"]) == 4, state.keys())
check("24 小时内结束的不含一天以前的", all(state["now"] - v["when"] <= 86400 for v in state["recent"]) and "清理过期的发布分支" in {v["title"] for v in state["recent"]})
check("每条带项目、颜色、标题，不带摘要", all({"ws", "color", "title", "state"} <= set(v) and "summary" not in v for v in state["recent"]))

print("== 对话页")
conv = json.loads(get("/api/conv?id=" + sid, phone)[1])
check("对话页有消息和最后的回复", conv["messages"][-1]["text"].startswith("**改好了**"), conv)
check("没有的对话返回 404", get("/api/conv?id=nope", phone)[0] == 404)


def wait_job(job):
    for _ in range(50):
        j = json.loads(get("/api/job?id=" + job, phone)[1])
        if j["state"] != "sending":
            return j
        time.sleep(0.1)
    return j


print("== 回复")
job = json.loads(get("/api/reply", phone, {"id": sid, "text": "再改一下"})[1])["job"]
j = wait_job(job)
check("编辑器里的对话：替你在编辑器里打字", j["state"] == "sent" and typed and typed[-1][:3] == (sid, BOX, "再改一下"), (j, typed))
h.history[-1]["owned"] = True
job = json.loads(get("/api/reply", phone, {"id": sid, "text": "后台接着跑"})[1])["job"]
check("后台任务、编辑器里没开着：直接接着跑", wait_job(job)["state"] == "sent" and h.runner.calls[-1] == ("follow", sid, "后台接着跑"))
held.add(sid)
job = json.loads(get("/api/reply", phone, {"id": sid, "text": "它已经在编辑器里开着"})[1])["job"]
wait_job(job)
check("后台任务但编辑器里开着它：也走编辑器，免得两边写岔", typed[-1][2] == "它已经在编辑器里开着")
h.history[-1]["state"] = "ask"
n = len(typed)
job = json.loads(get("/api/reply", phone, {"id": sid, "text": "这时候别按回车"})[1])["job"]
j = wait_job(job)
check("对话在等你确认 / 回答时，不替你按回车", j["state"] == "failed" and "等你确认" in j["message"] and len(typed) == n, j)
h.history[-1]["state"] = "done"
check("空内容不收", get("/api/reply", phone, {"id": sid, "text": "  "})[0] == 400)
check("没钥匙不能回复", get("/api/reply", None, {"id": sid, "text": "x"})[0] == 403)

print("== 回答选择题")
QS = [{"question": "用哪个方案？", "header": "方案", "multiSelect": False, "options": [{"label": "A 方案", "description": "快"}, {"label": "B 方案"}]},
      {"question": "要哪些平台？", "header": "平台", "multiSelect": True, "options": [{"label": "Windows"}, {"label": "Mac"}]}]
h.history[-1].update(state="ask", question={"tool_id": "toolu_1", "questions": QS})
conv = json.loads(get("/api/conv?id=" + sid, phone)[1])
check("对话页带着还没回答的选择题", conv["question"] == {"tool_id": "toolu_1", "questions": QS}, conv.get("question"))
text = web.Web.answer_text(QS, [{"labels": ["A 方案"]}, {"labels": ["Windows", "Mac"], "other": " Linux 也要 "}])
check("选择拼成一句话：每个问题一行，多选和自己写的连在一起",
      text == "我回答你刚才的问题：\n· 用哪个方案？ → A 方案\n· 要哪些平台？ → Windows、Mac、Linux 也要", text)
check("一个都没选：拼不出话", web.Web.answer_text(QS, [{"labels": []}, {}]) == "")
answers = []
runner.answer_in_editor = lambda s, root, tool, t, path: (answers.append((s, root, tool, t, path)) or (True, "已经把你的选择发过去了，它会接着做。"))
out = get("/api/answer", phone, {"id": sid, "tool_id": "toolu_old", "picks": [{"labels": ["A 方案"]}]})
check("问题已经换了（或者在电脑上答过了）：不发", out[0] == 400 and "不在等回答" in out[1] and not answers, out)
out = get("/api/answer", phone, {"id": sid, "tool_id": "toolu_1", "picks": [{"labels": []}, {"labels": []}]})
check("什么都没选：不发", out[0] == 400 and not answers, out)
job = json.loads(get("/api/answer", phone, {"id": sid, "tool_id": "toolu_1", "picks": [{"labels": ["B 方案"]}, {"labels": []}]})[1])["job"]
j = wait_job(job)
check("在电脑上回答：撤下提问、把选择发过去", j["state"] == "sent" and answers and answers[-1][:4] == (sid, BOX, "toolu_1", "我回答你刚才的问题：\n· 用哪个方案？ → B 方案")
      and answers[-1][4].endswith(sid + ".jsonl"), (j, answers[-1:]))
check("没钥匙不能回答", get("/api/answer", None, {"id": sid, "tool_id": "toolu_1", "picks": [{"labels": ["B 方案"]}]})[0] == 403)
n = len(typed)
job = json.loads(get("/api/reply", phone, {"id": sid, "text": "直接回一句"})[1])["job"]
check("在等你选的时候，普通回复不替你按回车（要用选项）", wait_job(job)["state"] == "failed" and len(typed) == n)
h.history[-1].update(state="done", question=None)

print("== 发起任务")
out = get("/api/new", phone, {"ws": "shop-server", "text": "加个缓存"})
check("在选的项目里发起后台任务", out[0] == 200 and h.runner.calls[-1] == ("start", os.path.join(BOX, "shop-server"), "加个缓存", "web"), (out, h.runner.calls[-1:]))
check("项目不存在时报错", get("/api/new", phone, {"ws": "nowhere", "text": "x"})[0] == 400)

print("== 已读")
h.unread[:] = [{"id": sid, "key": sid + "@1", "title": "x", "ws": "w", "ws_color": "#fff", "state": "done", "start": 0, "end": 1}]
get("/api/conv?id=" + sid, phone)
h.sync()
check("在手机上看过的，悬浮窗里也算已读", not h.unread, h.unread)

print("== 错太多次先挡住")
for _ in range(25):
    get("/api/state?k=bad")
check("同一个地址错 20 次以上，连对的钥匙也先挡十分钟", get("/api/state?k=" + w.key())[0] == 403)

w.stop()
h.root.destroy()
print("\n失败 %d 项：%s" % (len(fails), fails) if fails else "\n全部通过")
sys.exit(1 if fails else 0)
