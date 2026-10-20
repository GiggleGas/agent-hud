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
os.environ["CODEX_HOME"] = os.path.join(root, ".codex")
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
check("回合结束但子 agent 在跑 -> 等后台", (v["state"], v["action"]), ("bg", "等子 agent 干完"))
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
check("打断后子 agent 还在 -> 等后台", v["state"], "bg")
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
check("稳住 SETTLE 秒后报，要通知", kinds(h.update(one("done"), 101 + hub.SETTLE)), [("finished", True)])
check("不重复报", h.update(one("done"), 110), [])
h.update(one("run"), 111)
h.update(one("done"), 112)
check("刚结束马上又开工的不报", h.update(one("run"), 113) + h.update(one("run"), 120), [])
check("开始提问时报一次", kinds(h.update(one("ask"), 121) + h.update(one("ask"), 122)), [("asking", True)])
h = hub.Hub()
h.update([], 1)
check("第一次看到就已经结束的（跑得很快的后台任务）也会报", kinds(h.update(one("done"), 2) + h.update(one("done"), 6)), [("finished", True)])

# ---------- 后台命令 ----------
print("== 后台命令：这一轮说完了，后台还在跑")
bg_root = tempfile.mkdtemp(prefix="agent-hud-bg-")
collector.PROJECTS = bg_root
bproj = os.path.join(bg_root, "f--Work-Game")
os.makedirs(bproj)
tasks = os.path.join(bg_root, "tasks")
os.makedirs(tasks)


def launched(t, i, tid):   # 照真实记录的样子：编号在 toolUseResult 里，输出文件写在结果的文字里
    out = os.path.join(tasks, tid + ".output")
    return {"type": "user", "timestamp": iso(t), "toolUseResult": {"stdout": "", "stderr": "", "interrupted": False, "backgroundTaskId": tid},
            "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": i, "content":
                        "Command running in background with ID: %s. Output is being written to: %s. You will be notified when it completes." % (tid, out)}]}}


def notified(t, tid, status="completed"):
    return dict(user(t, "<task-notification>\n<task-id>%s</task-id>\n<tool-use-id>x</tool-use-id>\n<status>%s</status>\n"
                        "<summary>Background command completed (exit code 0)</summary>\n</task-notification>" % (tid, status)),
                origin={"kind": "task-notification"})


bmain = os.path.join(bproj, "sbg.jsonl")
B0 = time.time() - 120
w(bmain, [
    user(B0, "第 7 关调完后再跑 400 趟"),
    asst(B0 + 1, [tool("b1", "Bash", command="python sim.py --runs 400", description="跑 400 趟模拟", run_in_background=True)]),
    launched(B0 + 2, "b1", "bx1"),
    asst(B0 + 3, [tool("b2", "PowerShell", command="node tools/mapview/server.mjs --port 3330", description="Run the map viewer", run_in_background=True)]),
    launched(B0 + 4, "b2", "bx2"),
    asst(B0 + 5, [{"type": "text", "text": "模拟起在后台了，跑完我看胜率。"}], "end_turn"),
])
with open(os.path.join(tasks, "bx1.output"), "w", encoding="utf-8") as f:
    f.write("开始\n[1/400]\r[200/400]\r\x1b[32m[312/400] 胜率 41%\x1b[0m\n\n")
bcol = Collector(workspaces.Registry(os.path.join(bg_root, "ws.json")))
v = bcol.poll(time.time())[0]
check("这一轮说完了、后台命令还在跑 -> 等后台，不算完成", (v["state"], v["action"]), ("bg", "等后台命令：跑 400 趟模拟"))
check("后台命令：说明、输出的最后一行（去掉颜色、进度条取最后刷出来的）、服务器认成常驻的",
      [(b["desc"], b["tail"], b["resident"]) for b in v["bg"]], [("跑 400 趟模拟", "[312/400] 胜率 41%", False), ("Run the map viewer", "", True)])
check("等后台算在跑的那一类（不闪光、不推送、不进「已结束」）", v["state"] in collector.LIVE, True)
check("用户说「不用等了」：当场算完成", bcol.sessions[bmain].view(time.time(), {"bx1"})["state"], "done")
h = hub.Hub()
h.update([v], 100)
check("从在跑变成等后台：不报结束", h.update([v], 101) + h.update([v], 110), [])
w(bmain, [notified(time.time(), "bx1"), asst(time.time(), [{"type": "text", "text": "跑完了，胜率 41%。"}], "end_turn")])
v = bcol.poll(time.time())[0]
check("收到结束通知、接着说完 -> 完成；只剩开着的服务器，不等它", (v["state"], [b["desc"] for b in v["bg"]]), ("done", ["Run the map viewer"]))
check("等后台结束、真做完了：这时才报结束，要通知", kinds(h.update([v], 111) + h.update([v], 115)), [("finished", True)])

print("== 后台命令：没有结束通知的")
w(bmain, [user(time.time(), "再跑一次"),
          asst(time.time(), [tool("b3", "Bash", command="python sim.py", description="再跑一次", run_in_background=True)]),
          launched(time.time(), "b3", "bx3"), asst(time.time(), [{"type": "text", "text": "起好了。"}], "end_turn")])
v = bcol.poll(time.time())[0]
check("新的后台命令：又在等", (v["state"], v["action"]), ("bg", "等后台命令：再跑一次"))
check("对话被关掉、后台跟着没了：输出和记录都 30 分钟没动静 -> 没收尾",
      bcol.sessions[bmain].view(time.time() + collector.STALE_TOOL + 60)["state"], "stale")
with open(os.path.join(tasks, "bx3.output"), "w", encoding="utf-8") as f:
    f.write("还在输出\n")
later = time.time() + collector.STALE_TOOL + 60
os.utime(os.path.join(tasks, "bx3.output"), (later - 30, later - 30))
check("对话记录没动、但后台还在往输出里写：照样在等", bcol.sessions[bmain].view(later)["state"], "bg")
w(bmain, [notified(time.time(), "bx3", "killed")])
bcol.sessions[bmain].pump()
check("被停掉也算等完了", [b["desc"] for b in bcol.sessions[bmain].view(time.time())["bg"] if not b["resident"]], [])

print("== 排队的消息：它忙的时候你又发了一句")
qmain = os.path.join(bproj, "squeue.jsonl")
Q0 = time.time() - 30
queue = lambda t, op, **kw: dict({"type": "queue-operation", "operation": op, "timestamp": iso(t), "sessionId": "squeue"}, **kw)
w(qmain, [user(Q0, "改一下标题"), asst(Q0 + 1, [tool("q1", "Edit", file_path="a.py")]), queue(Q0 + 2, "enqueue"),
          result(Q0 + 3, "q1"), asst(Q0 + 4, [{"type": "text", "text": "改好了。"}], "end_turn")])
bcol.next_scan = 0
qv = lambda t: next(x for x in bcol.poll(t) if x["id"] == "squeue")
check("这一轮完了、你排队的话还没交给它 -> 还算在跑，不报完成", (qv(Q0 + 5)["state"], qv(Q0 + 5)["action"]), ("run", "接着处理你排队的消息"))
check("排着的消息一直没出队（比如对话被关了）：过一会儿就不等了", qv(Q0 + 4 + collector.QUEUE_WAIT + 1)["state"], "done")
w(qmain, [queue(Q0 + 6, "dequeue"), user(Q0 + 6, "顺便把副标题也改了"), asst(Q0 + 8, [{"type": "text", "text": "都改好了。"}], "end_turn")])
check("出队、接着做完 -> 完成", qv(Q0 + 9)["state"], "done")
w(qmain, [user(Q0 + 10, "再看看别的"), asst(Q0 + 11, [tool("q2", "Read", file_path="b.py")]), queue(Q0 + 12, "enqueue"),
          {"type": "attachment", "timestamp": iso(Q0 + 12), "attachment": {"type": "queued_command", "prompt": "顺手看看 c.py"}},
          queue(Q0 + 13, "remove", reason="absorbed_mid_turn"), result(Q0 + 14, "q2"),
          asst(Q0 + 15, [{"type": "text", "text": "都看了。"}], "end_turn")])
check("中途就被读掉的（移除了）：不算还排着，照常完成", qv(Q0 + 16)["state"], "done")

print("== 后台命令：悬浮窗中途打开（靠压缩后的 task_status 补上）")
cmain = os.path.join(bproj, "scomp.jsonl")
C0 = time.time() - 60
w(cmain, [
    user(C0, "接着调"),
    {"type": "attachment", "timestamp": iso(C0 + 1), "attachment": {"type": "task_status", "taskId": "bc1", "taskType": "local_bash",
     "description": "编译整个工程", "status": "running", "outputFilePath": os.path.join(tasks, "bc1.output"),
     "shell": {"command": "UnrealBuildTool Game Win64", "kind": "bash"}}},
    {"type": "attachment", "timestamp": iso(C0 + 1), "attachment": {"type": "task_status", "taskId": "ag1", "taskType": "local_agent",
     "description": "子 agent 不算在这里", "status": "running"}},
    asst(C0 + 2, [{"type": "text", "text": "等编译完。"}], "end_turn"),
])
bcol.next_scan = 0
v = next(x for x in bcol.poll(time.time()) if x["id"] == "scomp")
check("压缩前起的后台命令：从 task_status 认出来", (v["state"], [b["desc"] for b in v["bg"]]), ("bg", ["编译整个工程"]))
w(cmain, [{"type": "attachment", "timestamp": iso(time.time()), "attachment": {"type": "task_status", "taskId": "bc1",
           "taskType": "local_bash", "description": "编译整个工程", "status": "completed"}}])
v = next(x for x in bcol.poll(time.time()) if x["id"] == "scomp")
check("task_status 说结束了：划掉", (v["state"], v["bg"]), ("done", []))
collector.PROJECTS = root
shutil.rmtree(bg_root, ignore_errors=True)

shutil.rmtree(root, ignore_errors=True)
print("\n失败 %d 项" % len(fails) if fails else "\n全部通过")
sys.exit(1 if fails else 0)
