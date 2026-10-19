"""collector.py 的回归测试：python test_collector.py，全过退出码为 0。"""
import json, os, shutil, sys, tempfile, time
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))
import collector
import hub
import workspaces
from collector import Collector, Session

sys.stdout.reconfigure(encoding="utf-8")
fails = []


def check(name, got, want):
    ok = got == want
    print(("  ok   " if ok else "  FAIL ") + name + ("" if ok else "  got=%r want=%r" % (got, want)))
    if not ok:
        fails.append(name)


# 仿造一个会话，把各种状态走一遍：进度标记、子 agent 交回 / 失败、提问、等确认、打断
# 记录格式照 Claude Code 2.1.296 写的；升级后悬浮窗认错状态，先对照真实记录改这里的仿造数据
root = tempfile.mkdtemp(prefix="agent-hud-test-")
proj = os.path.join(root, "f--Work-Game")
os.makedirs(os.path.join(proj, "sess1", "subagents"))
collector.PROJECTS = root
T0 = time.time() - 300


def iso(t):
    return datetime.fromtimestamp(t, timezone.utc).isoformat().replace("+00:00", "Z")


def w(path, entries):
    with open(path, "a", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e, ensure_ascii=False, separators=(",", ":")) + "\n")


def user(t, text, **kw):
    return dict({"type": "user", "timestamp": iso(t), "cwd": "F:\\Work\\Game\\art\\out", "permissionMode": "auto",
                 "origin": {"kind": "human"}, "message": {"role": "user", "content": [{"type": "text", "text": text}]}}, **kw)


def asst(t, blocks, stop="tool_use"):
    return {"type": "assistant", "timestamp": iso(t), "message": {"role": "assistant", "content": blocks, "stop_reason": stop}}


def tool(i, name, **inp):
    return {"type": "tool_use", "id": i, "name": name, "input": inp}


def result(t, i, **kw):
    return dict({"type": "user", "timestamp": iso(t),
                 "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": i, "content": "ok"}]}}, **kw)


main = os.path.join(proj, "sess1.jsonl")
w(main, [
    user(T0 - 900, "更早的一个问题"),
    asst(T0 - 890, [{"type": "text", "text": "进度 4/4 · 旧任务"}], "end_turn"),
    user(T0, "做三把枪的模型\n第二行"),
    asst(T0 + 1, [{"type": "thinking", "thinking": ""}]),
    asst(T0 + 2, [{"type": "text", "text": "**进度 1/3 · 写需求文档**\n\n先把三把枪的需求写下来。"}]),
    asst(T0 + 3, [tool("t1", "Write", file_path="F:/Work/Game/art/req/guns.md")]),
    result(T0 + 4, "t1"),
    asst(T0 + 5, [{"type": "text", "text": "进度 2/3 · 分给子 agent 并行做"}]),
    asst(T0 + 6, [tool("a1", "Agent", description="Build rifle", prompt="x", run_in_background=True)]),
    result(T0 + 7, "a1", toolUseResult={"status": "async_launched", "agentId": "aaa1"}),
    asst(T0 + 8, [tool("a2", "Agent", description="Build pistol", prompt="x", run_in_background=True)]),
    result(T0 + 9, "a2", toolUseResult={"status": "async_launched", "agentId": "aaa2"}),
    asst(T0 + 10, [tool("a3", "Agent", description="Build sniper", prompt="x", run_in_background=True)]),
    result(T0 + 11, "a3", toolUseResult={"status": "async_launched", "agentId": "aaa3"}),
    asst(T0 + 12, [{"type": "text", "text": "三个都派出去了，等它们交回来。"}], "end_turn"),
    {"type": "system", "subtype": "stop_hook_summary", "timestamp": iso(T0 + 13)},
    {"type": "ai-title", "aiTitle": "三把枪建模", "sessionId": "sess1"},
])
for aid, desc in (("aaa1", "Build rifle"), ("aaa2", "Build pistol"), ("aaa3", "Build sniper")):
    base = os.path.join(proj, "sess1", "subagents", "agent-" + aid)
    with open(base + ".meta.json", "w", encoding="utf-8") as f:
        json.dump({"agentType": "general-purpose", "description": desc}, f)
    w(base + ".jsonl", [
        {"type": "user", "isSidechain": True, "agentId": aid, "timestamp": iso(T0 + 7), "message": {"role": "user", "content": "做 " + desc}},
        asst(T0 + 20, [{"type": "thinking", "thinking": ""}], None),
        asst(T0 + 21, [tool(aid + "x", "Bash", command="blender -b --python make.py", description="Blender 生成模型")], None),
    ])

reg = workspaces.Registry(os.path.join(root, "workspaces.json"))
col = Collector(reg)
now = time.time()
v = col.poll(now)[0]
check("标题", v["title"], "三把枪建模")
check("启动目录不存在时，它自己算一个项目", v["ws"], "Game")
check("回合结束但子 agent 在跑 -> subs", v["state"], "subs")
check("进度标记取当前任务里最后一个", v["marker"], (2, 3, "分给子 agent 并行做"))
check("最近一句话", v["line"], "三个都派出去了，等它们交回来。")
check("步数只算当前任务", v["steps"], 4)
check("三个子 agent 都在跑", [s["state"] for s in v["subs"]], ["run"] * 3)
check("子 agent 的名字和动作", (v["subs"][0]["desc"], v["subs"][0]["action"]), ("Build rifle", "跑命令：Blender 生成模型"))

# rifle 自己交回；pistol 主会话收到失败通知；sniper 还在跑
t = time.time()
w(os.path.join(proj, "sess1", "subagents", "agent-aaa1.jsonl"), [
    result(t, "aaa1x"), asst(t, [tool("hb", "SubagentHandback", message="做完了")], None), result(t, "hb")])
w(main, [
    dict(user(t, "<agent-message>[Subagent hand-back] 做完了", isMeta=True), origin={"kind": "peer", "from": "aaa1"}),
    asst(t, [{"type": "text", "text": "rifle 交回来了，我先验收它。\n\n进度 3/3 · 验收"}]),
    asst(t, [tool("r1", "Read", file_path="F:/Work/Game/art/out/rifle.png")]),
    dict(user(t, "<task-notification>\n<task-id>aaa2</task-id>\n<status>failed</status>\n</task-notification>"),
         origin={"kind": "task-notification"}),
])
v = col.poll(time.time())[0]
check("主会话被唤醒 -> run", v["state"], "run")
check("子 agent 状态", [s["state"] for s in v["subs"]], ["done", "error", "run"])
check("标记更新", v["marker"], (3, 3, "验收"))
check("通知不清零步数", v["steps"], 5)

# 提问 -> ask；Read 卡 30 秒 -> confirm；打断 -> interrupted
QS = [{"question": "用哪个方案？", "header": "方案", "multiSelect": False, "options": [{"label": "A", "description": "快"}, {"label": "B"}]}]
w(main, [result(time.time(), "r1"), asst(time.time(), [tool("q1", "AskUserQuestion", questions=QS)])])
v = col.poll(time.time())[0]
check("提问 -> ask", (v["state"], v["action"]), ("ask", "在问你问题，等你回答"))
check("还没回答的选择题带出来（手机上点选用）", v["question"], {"tool_id": "q1", "questions": QS})
w(main, [result(time.time(), "q1"), asst(time.time(), [tool("r2", "Edit", file_path="a/b/c.py")])])
v = col.poll(time.time() + 30)[0]
check("Edit 卡住 -> confirm", (v["state"], v["action"]), ("confirm", "可能在等你点确认 · 改文件：c.py"))
check("答完了：选择题没了", v["question"], None)
w(main, [{"type": "user", "timestamp": iso(time.time()), "message": {"role": "user", "content": [{"type": "text", "text": "[Request interrupted by user]"}]}}])
v = col.poll(time.time())[0]
check("打断后子 agent 还在 -> subs", v["state"], "subs")
w(main, [dict(user(time.time(), "<task-notification>\n<task-id>aaa3</task-id>\n<status>stopped</status>\n</task-notification>"),
              origin={"kind": "task-notification"}),
         asst(time.time(), [{"type": "text", "text": "都停了。"}], "end_turn")])
v = col.poll(time.time())[0]
check("全部结束 -> done", (v["state"], [s["state"] for s in v["subs"]]), ("done", ["done", "error", "interrupted"]))
check("结束 10 分钟后不再显示", col.poll(time.time() + 601), [])
check("新开的悬浮窗只读最近一次发话之后", Session(main, reg).tail.pos > 0, True)

# ---------- 项目清单 ----------
print("== 项目清单")
box = os.path.join(root, "Box")   # 装项目的文件夹：自己没有工程标志，下面有两个带标志的
for name, marker in (("alpha", "project.godot"), ("beta", "package.json"), ("gamma", None)):
    os.makedirs(os.path.join(box, name))
    if marker:
        open(os.path.join(box, name, marker), "w").close()
solo = os.path.join(root, "Solo")   # 自带工程标志的目录
os.makedirs(os.path.join(solo, "Source"))
open(os.path.join(solo, "UE5.sln"), "w").close()
reg2 = workspaces.Registry(os.path.join(root, "ws2.json"))
reg2.ensure(box)
reg2.ensure(solo)
slash = box.replace("\\", "/")
check("装项目的文件夹：自己和每个子文件夹各算一个", sorted(it["name"] for it in reg2.items), ["Box", "Solo", "alpha", "beta", "gamma"])
check("自带工程标志的目录不展开", reg2.match(os.path.join(solo, "Source", "a.cpp"))["name"], "Solo")
check("路径归到最深的项目", reg2.match(os.path.join(box, "gamma", "x", "y.txt"))["name"], "gamma")
check("直接放在外层的文件归外层", reg2.match(os.path.join(box, "notes.zip"))["name"], "Box")
check("不在任何项目里", reg2.match(r"C:\Windows\notepad.exe"), None)
check("命令里的 Git Bash 写法也认得", [it["name"] for it in reg2.find_in('cd "/%s%s/beta" && npm test' % (slash[0].lower(), slash[2:]))], ["beta"])
check("名字相近的目录不误认", reg2.find_in("ls %s/alpha-old/x" % slash), [it for it in reg2.items if it["name"] == "Box"])
check("按名字找：开头唯一就行，有歧义不猜", (reg2.by_name("GAM")["name"], reg2.by_name("b")), ("gamma", None))
check("清单存盘后读回来一样", [it["name"] for it in workspaces.Registry(os.path.join(root, "ws2.json")).items], [it["name"] for it in reg2.items])

# ---------- 对话归哪个项目 ----------
print("== 对话归哪个项目")
proj2 = os.path.join(root, collector.re.sub(r"[^A-Za-z0-9]", "-", box))
os.makedirs(proj2)
main2 = os.path.join(proj2, "sess2.jsonl")
t = time.time()
w(main2, [
    dict(user(t, "先看看 alpha，再改 gamma"), cwd=box),
    asst(t, [tool("p1", "Read", file_path=os.path.join(box, "alpha", "main.gd"))]),
    asst(t, [tool("p2", "Bash", command='cd "%s/beta" && npm test' % slash)]),
    asst(t, [tool("p3", "Edit", file_path=os.path.join(box, "gamma", "a.txt"))]),
])
col2 = Collector(reg2)
view = lambda: next(v for v in col2.poll(time.time()) if v["id"] == "sess2")
check("改文件比读文件分量重", view()["ws"], "gamma")
w(main2, [dict(user(time.time(), "接着弄"), cwd=box)])
check("新一轮还没碰文件时沿用上一轮的项目", view()["ws"], "gamma")
w(main2, [asst(time.time(), [tool("p4", "Read", file_path=os.path.join(box, "alpha", "b.gd"))])])
check("新一轮碰了别的项目就换过去", view()["ws"], "alpha")
check("不是悬浮窗发起的", view()["owned"], False)
col2.owned = {"sess2": os.path.join(box, "beta")}
check("悬浮窗发起的任务：项目以发起时选的为准", (view()["ws"], view()["owned"]), ("beta", True))

# ---------- 历史记录 ----------
print("== 历史记录")
hist = collector.History(reg2, days=3)
old = time.time() - 2 * 3600
os.utime(main2, (old, old))   # 两小时没动过：不再跟踪，但还在三天的历史里
items = hist.recent(set())
check("最近几天的对话都在历史里", "sess2" in {v["id"] for v in items}, True)
v2 = next(v for v in items if v["id"] == "sess2")
check("历史记录也按读写的文件归项目", v2["ws"], "alpha")
check("历史里没收尾的显示成没动静，不当成在干活", v2["state"], "stale")
check("正在跟踪的不重复出现在历史里", [v["id"] for v in hist.recent({"sess2"})].count("sess2"), 0)
given = hist.recent(set())
for v in given:
    v["ws"] = "手动指定的"   # 外面按发起方式 / 右键「归到项目」改项目
check("历史里有已经收尾的（不是副本的话会被改到缓存里）", any(v["state"] != "stale" for v in given), True)
check("给出去的是副本：外面改了项目，缓存里不跟着变（撤掉手动指定能改回来）",
      [v["ws"] for v in hist.recent(set()) if v["ws"] == "手动指定的"], [])
very_old = time.time() - 5 * 86400
os.utime(main2, (very_old, very_old))
hist.next = 0
check("超过天数的不在历史里", "sess2" in {v["id"] for v in hist.recent(set())}, False)

# ---------- 环境变量 ----------
print("== 从编辑器继承来的环境变量")
import install
os.environ["ELECTRON_RUN_AS_NODE"] = "1"
os.environ["CLAUDE_CODE_SESSION_ID"] = "x"
os.environ["AGENT_HUD_KEEP_ME"] = "1"
install.clean_env()
check("会让 Cursor 打不开、让后台任务认错爹的变量去掉了",
      "ELECTRON_RUN_AS_NODE" in os.environ or "CLAUDE_CODE_SESSION_ID" in os.environ, False)
check("别的变量不动", os.environ.get("AGENT_HUD_KEEP_ME") == "1" and "PATH" in os.environ, True)

# ---------- 事件 ----------
print("== 事件")
one = lambda state: [{"id": "x", "state": state}]
kinds = lambda events: [(e[0], e[2]) for e in events]
h = hub.Hub()
check("悬浮窗启动时已经结束的：列出来，但不对外通知", kinds(h.update(one("done"), 100)), [("finished", False)])
h = hub.Hub()
h.update(one("run"), 100)
check("刚结束先不报", h.update(one("done"), 101), [])
check("稳住 3 秒后报，要通知", kinds(h.update(one("done"), 104.5)), [("finished", True)])
check("不重复报", h.update(one("done"), 110), [])
h.update(one("run"), 111)
h.update(one("done"), 112)
check("刚结束马上又开工的不报", h.update(one("run"), 113) + h.update(one("run"), 120), [])
check("开始提问时报一次", kinds(h.update(one("ask"), 121) + h.update(one("ask"), 122)), [("asking", True)])
h = hub.Hub()
h.update([], 1)
check("第一次看到就已经结束的（跑得很快的后台任务）也会报", kinds(h.update(one("done"), 2) + h.update(one("done"), 6)), [("finished", True)])

shutil.rmtree(root, ignore_errors=True)
print("\n失败 %d 项" % len(fails) if fails else "\n全部通过")
sys.exit(1 if fails else 0)
