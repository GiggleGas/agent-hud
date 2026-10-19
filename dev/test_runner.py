"""后台任务的回归测试：发起、追问、排队、停止、没跑起来。用 fake_claude.py 顶替真的 claude，
全部在临时目录里，不花额度、不碰真实的会话记录。约一分钟。"""
import json, os, shutil, sys, tempfile, time
from urllib.parse import quote

sys.stdout.reconfigure(encoding="utf-8")
DEV = os.path.dirname(os.path.abspath(__file__))
BOX = tempfile.mkdtemp(prefix="agent-hud-runner-")
os.environ.update(AGENT_HUD_TEST_ROOT=BOX, CLAUDE_CONFIG_DIR=os.path.join(BOX, ".claude"),
                  AGENT_HUD_CLAUDE=os.path.join(DEV, "fake_claude.py"))   # 要赶在导入下面这些模块之前设好
sys.path.insert(0, os.path.join(os.path.dirname(DEV), "app"))
import collector, hub, runner, workspaces
from collector import LIVE

fails = []


def check(name, ok, detail=""):
    print(("  ok   " if ok else "  FAIL ") + name + ("" if ok else "  " + str(detail)))
    if not ok:
        fails.append(name)


ws = os.path.join(BOX, "proj", "alpha")
os.makedirs(ws)
reg = workspaces.Registry()
reg.ensure(ws)
problems, events = [], []
r = runner.Runner("auto", lambda sid, text: problems.append((sid, text)))
col = collector.Collector(reg)
h = hub.Hub()
h.update([])   # 悬浮窗已经开着的状态


def step():
    """照悬浮窗采集线程每秒做的那样走一轮，返回这一轮的会话列表。"""
    r.tick()
    col.owned, col.running, col.exited = r.owned(), r.active(), dict(r.exited)
    col.next_scan = 0
    views = col.poll()
    starting = r.starting({v["id"] for v in views})
    views += [collector.placeholder(sid, t, reg, time.time()) for sid, t in starting]
    events.extend(h.update(views))
    return views


def wait(sid, want, seconds=20):
    """等到这个会话满足 want，返回当时的样子；超时返回最后看到的。"""
    end, v = time.time() + seconds, None
    while time.time() < end:
        v = next((x for x in step() if x["id"] == sid), None)
        if v and want(v):
            return v
        time.sleep(0.5)
    return v


try:
    print("== 找 claude 程序")
    del os.environ["AGENT_HUD_CLAUDE"]
    real = runner.find_claude()
    print("      真实环境里会用：", real)
    os.environ["AGENT_HUD_CLAUDE"] = os.path.join(DEV, "fake_claude.py")
    check("没装的话返回 None 而不是报错；装了就给出命令和版本", real is None or (len(real) == 2 and os.path.exists(real[0][0])))

    print("== 发起")
    sid = r.start(ws, "第一句\n还有第二行")
    v = wait(sid, lambda v: True, 3)
    check("会话记录还没出现时先有一张「正在启动」", v and (v["state"], v["action"], v["owned"], v["ws"]) == ("run", "正在启动…", True, "alpha"), v)
    v = wait(sid, lambda v: v["steps"] >= 1)
    check("记录出现后接上真实进展", v and v["state"] == "run" and v["action"] == "读文件：main.gd", v)
    v = wait(sid, lambda v: v["state"] == "done")
    check("跑完变成已结束，带着最后的回复", v and "做完了：第一句" in v["last_text"] and "还有第二行" in v["last_text"], v)
    check("用的是配置的权限模式", v and "权限模式 auto" in v["last_text"])
    check("标题、项目、后台标记", v and (v["title"], v["ws"], v["owned"]) == ("假任务 第一句\n还有第二行"[:14], "alpha", True), v and v["title"])
    wait(sid, lambda v: False, 4)
    check("结束时报了一次事件，要对外通知", [(e[0], e[1]["id"], e[2]) for e in events] == [("finished", sid, True)], events)

    print("== 追问")
    check("上一轮跑完了就直接发", r.follow(sid, "第二句") == "sent")
    v = wait(sid, lambda v: True, 2)
    check("发出去之后马上显示在跑", v and v["state"] == "run", v and v["state"])
    v = wait(sid, lambda v: v["state"] == "done" and "第二句" in v["last_text"])
    check("追问接在同一个对话里", v is not None and "做完了：第二句" in v["last_text"], v and v["last_text"])

    print("== 排队和停止")
    slow = r.start(ws, "SLOW 的任务")
    wait(slow, lambda v: v["steps"] >= 1)
    check("还在跑的时候追问会排队", r.follow(slow, "排队的一句") == "queued" and r.queue[slow] == ["排队的一句"])
    t = time.time()
    check("停止成功", r.stop(slow) is True)
    v = wait(slow, lambda v: v["state"] not in LIVE, 6)
    check("停掉后几秒内显示被打断，不用等十分钟", v and v["state"] == "interrupted" and time.time() - t < 6, v and v["state"])
    check("排着队的追问一起取消", not r.queue.get(slow) and not r.running(slow))
    check("没在跑的任务再停一次返回 False", r.stop(slow) is False)

    print("== 没跑起来")
    bad = r.start(ws, "FAIL 一下")
    wait(bad, lambda v: False, 3)
    check("异常退出会报出来，带原因", [p[0] for p in problems] == [bad] and "boom" in problems[0][1], problems)
    try:
        r.follow("not-mine", "x")
        check("不是自己发起的对话不让追问", False)
    except RuntimeError as e:
        check("不是自己发起的对话不让追问", "不是悬浮窗发起的" in str(e))
    try:
        r.start(os.path.join(BOX, "nowhere"), "x")
        check("项目目录不存在时报错，不留下空任务", False)
    except RuntimeError:
        check("项目目录不存在时报错，不留下空任务", len(r.tasks) == 3, len(r.tasks))

    print("== 重开悬浮窗之后")
    r2 = runner.Runner("auto")
    check("还认得之前发起的任务", r2.owned() == r.owned() and len(r2.owned()) == 3)
    r2.tick()
    check("进程已经没了的不会被当成还在跑", not r2.active())

    print("== 悬浮窗关掉以后")
    # 另起一个进程扮演悬浮窗：发起一个慢任务就退出。任务应该接着跑，重开的悬浮窗应该认得它、停得掉它。
    code = ("import sys; sys.path.insert(0, r'%s'); import runner; r = runner.Runner('auto'); print(r.start(r'%s', 'SLOW 留下的任务'))"
            % (os.path.join(os.path.dirname(DEV), "app"), ws))
    import subprocess
    left = subprocess.run([sys.executable, "-c", code], capture_output=True, env=os.environ).stdout.decode().strip()
    time.sleep(1)
    r3 = runner.Runner("auto")
    check("发起它的进程退出后，任务还在跑", bool(left) and r3.running(left), left)
    check("重开的悬浮窗能停掉它", r3.stop(left) and not r3.running(left))

    print("== 项目没开着时用哪个编辑器打开")
    real_windows, real_exists = runner.editor_windows, os.path.exists
    both = lambda p: "cursor.cmd" in p or "code.cmd" in p   # 两个编辑器都装了
    try:
        os.path.exists = both
        runner.editor_windows = lambda: [(1, "a.py - shop - Visual Studio Code", "vscode")]
        check("开着 VS Code 窗口：用 VS Code 打开", runner.editor_cli().endswith("code.cmd"), runner.editor_cli())
        runner.editor_windows = lambda: []
        check("一个窗口都没开：先找 Cursor", runner.editor_cli().endswith("cursor.cmd"), runner.editor_cli())
        os.path.exists = lambda p: p.startswith(os.path.expandvars("%ProgramFiles%")) and "code.cmd" in p
        check("只有装给所有用户的 VS Code 也能找到", (runner.editor_cli() or "").endswith("code.cmd"), runner.editor_cli())
        os.path.exists = lambda p: False
        check("编辑器都没装：返回空", runner.editor_cli() is None)
        os.path.exists = both
        runner.editor_windows = lambda: [(1, "a.py - shop - Cursor", "cursor")]
        check("知道对话在 VS Code 里：用 VS Code 打开，哪怕开着的是 Cursor", runner.editor_cli("vscode").endswith("code.cmd"))
    finally:
        runner.editor_windows, os.path.exists = real_windows, real_exists

    print("== 跳转时找哪个窗口")
    sid2 = "aaaaaaaa-1111-2222-3333-444444444444"
    vs = r"c:\Users\me\.vscode\extensions\anthropic.claude-code-2.1.300-win32-x64\resources\native-binary\claude.exe --output-format stream-json"
    cu = r"c:\Users\me\.cursor\extensions\anthropic.claude-code-2.1.301-win32-x64\resources\native-binary\claude.exe --output-format stream-json"
    check("命令行带这个对话 id 的：确定在那个编辑器", runner.session_editor(sid2, "", [cu + " --resume=x", vs + " --resume=" + sid2]) == ("vscode", True))
    check("新建没关过的对话：按扩展版本猜", runner.session_editor(sid2, "2.1.300", [cu, vs]) == ("vscode", False))
    check("两个编辑器版本一样：认不出", runner.session_editor(sid2, "2.1.300", [vs, vs.replace(".vscode", ".cursor")]) == (None, False))
    check("后台任务的 claude 不算编辑器的", runner.ext_editor(r"C:\Users\me\.local\bin\claude.exe -p --session-id x") is None)
    tr = os.path.join(BOX, "hint.jsonl")
    with open(tr, "w", encoding="utf-8") as f:   # 和真的会话记录一样写成紧凑格式（冒号后面没空格）
        f.write(json.dumps({"type": "user", "version": "2.1.300", "message": {"content": [{"type": "text", "text":
                "<ide_opened_file>The user opened the file d:\\Game\\Tasks\\AddTag.as in the IDE. This may or may not be related.</ide_opened_file>"}]}},
                ensure_ascii=False, separators=(",", ":")) + "\n")
        f.write(json.dumps({"type": "assistant", "version": "2.1.300", "message": {"content": [{"type": "tool_use", "name": "Edit",
                "input": {"file_path": "d:\\Game\\存档.json"}}]}}, ensure_ascii=False, separators=(",", ":")) + "\n")
    hints = runner.transcript_hints(tr)
    check("从对话记录里读出版本和碰过的文件", hints == {"version": "2.1.300", "files": {"AddTag.as", "存档.json"}}, hints)
    home = os.path.expanduser("~")
    real = runner.editor_windows, runner.claude_processes
    try:
        runner.editor_windows = lambda: [(1, "a.py - shop - Cursor", "cursor"), (2, "Welcome - %s - Visual Studio Code" % os.path.basename(home), "vscode"),
                                         (3, "AddTag.as - Visual Studio Code", "vscode"), (4, "Untitled-1 - Cursor", "cursor")]
        runner.claude_processes = lambda: [vs, cu + " --resume=other"]
        check("只开了文件的 VS Code 窗口里的对话：找到那个窗口，不新开、不跑去 Cursor", runner.locate(sid2, home, tr) == ((3, "vscode"), "vscode"),
              runner.locate(sid2, home, tr))
        runner.claude_processes = lambda: [cu + " --resume=" + sid2]
        check("确定在 Cursor 里：只在 Cursor 的窗口里找", runner.locate(sid2, home, tr) == ((4, "cursor"), "cursor"), runner.locate(sid2, home, tr))
        check("按项目文件夹名找（老办法照旧）", runner.locate(sid2, os.path.join(BOX, "shop"), tr) == ((1, "cursor"), "cursor"))
        check("确定的编辑器里没开这个项目：不拿别的编辑器的窗口凑数", runner.locate(sid2, os.path.join(BOX, "nowhere"), tr) == (None, "cursor"))
        runner.claude_processes = lambda: []
        check("认不出编辑器：所有窗口里找", runner.locate(sid2, os.path.join(BOX, "shop"), tr) == ((1, "cursor"), None))
    finally:
        runner.editor_windows, runner.claude_processes = real

    print("== 发起任务时选窗口")
    check("标题里的文件夹名", [runner.window_folder(t) for t in ("a.py - Something - Cursor", "● a.py - b - c - Something - Cursor",
                                                              "Something - Cursor", "Cursor")] == ["Something", "Something", "Something", ""])
    real_windows = runner.editor_windows
    try:
        runner.editor_windows = lambda: [(1, "x.cs - F1DEV1 - Cursor", "cursor"), (2, "a.py - Something - Cursor", "cursor"),
                                         (3, "b.py - agent-hud - Visual Studio Code", "vscode"), (4, "c.py - Something - Cursor", "cursor")]
        wins = runner.compose_windows(r"F:\AIDemo\Something\agent-hud")
        check("开着项目本身的排第一，开着上级文件夹的其次，不相干的最后",
              [(w["hwnd"], w["depth"]) for w in wins] == [(3, 0), (2, 1), (4, 1), (1, None)], [(w["hwnd"], w["depth"]) for w in wins])
        check("窗口的名字是「文件夹 · 编辑器」，同一个文件夹开了两个窗口的分得开",
              [w["label"] for w in wins] == ["agent-hud · VS Code", "Something · Cursor", "Something · Cursor（2）", "F1DEV1 · Cursor"],
              [w["label"] for w in wins])
    finally:
        runner.editor_windows = real_windows

    print("== 在编辑器窗口里新开对话")
    projects = collector.PROJECTS
    os.makedirs(os.path.join(projects, "f--Work"), exist_ok=True)
    old = os.path.join(projects, "f--Work", "old.jsonl")
    with open(old, "w", encoding="utf-8") as f:   # 以前就有、第一句也一样的对话：不能被当成新开的
        f.write(json.dumps({"type": "user", "message": {"content": "在 shop 项目里：加个缓存"}}, ensure_ascii=False, separators=(",", ":")) + "\n")
    calls, typed_now = [], [""]
    real_fns = {k: getattr(runner, k) for k in ("locked", "force_foreground", "wait_log", "wait_log_after", "press_new_tab_key", "type_text",
                                                "press_enter", "foreground", "log_sizes")}
    real_startfile = os.startfile
    new_file = os.path.join(projects, "f--Work", "new-session-id.jsonl")

    def fake_enter():   # 按下回车 = 扩展建了一个新对话、记下这句话
        calls.append("enter")
        with open(new_file, "w", encoding="utf-8") as f:
            f.write(json.dumps({"type": "user", "message": {"content": [{"type": "text", "text": "<ide_opened_file>x</ide_opened_file>"},
                                {"type": "text", "text": typed_now[0]}]}}, ensure_ascii=False, separators=(",", ":")) + "\n")

    def start(text, wait=5, use_key=True):
        calls.clear()
        typed_now[0] = text
        if os.path.exists(new_file):
            os.remove(new_file)
        return runner.start_in_editor(7, "cursor", text, wait=wait, use_key=use_key)

    try:
        runner.locked, runner.force_foreground, runner.log_sizes = (lambda: False), (lambda h: True), (lambda: {})
        runner.press_new_tab_key = lambda: calls.append("key")
        after = []
        runner.wait_log_after = lambda before, first, then, s: after.append((first, then)) or calls.append("focused") or True
        runner.wait_log = lambda before, needles, s: True
        runner.type_text = lambda t: calls.append(("type", t))
        runner.press_enter, runner.foreground = fake_enter, (lambda: 7)
        os.startfile = lambda url: calls.append(url)
        text = "加个缓存"
        ok, msg, sid3 = start(text)
        check("有快捷键：按快捷键新开对话、确认输入框拿到焦点、打字、回车，找到新对话的 id",
              ok and sid3 == "new-session-id" and calls == ["key", "focused", ("type", text), "enter"], (ok, msg, sid3, calls))
        check("确认的是：新面板 init 之后又记了 webview_focused", after == [(b'"type":"init"', b'"webview_focused"')], after)
        runner.wait_log_after = lambda before, first, then, s: False
        ok, msg, sid3 = start(text, wait=1)
        check("新对话的输入框没拿到焦点：不打字、不回车，说可能是快捷键的事", not ok and calls == ["key"] and "快捷键" in msg, (msg, calls))
        runner.wait_log_after = lambda before, first, then, s: True
        runner.foreground = lambda: 8   # 别的窗口抢了焦点
        ok, msg, sid3 = start(text, wait=1)
        check("焦点被抢走：不打字、不回车", not ok and "抢走" in msg and calls == ["key"], (msg, calls))
        runner.foreground = lambda: 7
        ok, msg, sid3 = start(text, use_key=False)
        check("没有快捷键：用链接开新对话、把话预填进去，交给用户按回车（不算失败）",
              ok is None and calls == ["cursor://anthropic.claude-code/open?prompt=" + quote(text)] and "按一下回车" in msg, (ok, msg, calls))
        long_text = "很长的一段话" * 100
        ok, msg, sid3 = start(long_text, use_key=False)
        check("没有快捷键、话又太长：开新对话，说放不进去", ok is None and calls == ["cursor://anthropic.claude-code/open"] and "太长" in msg, (msg, calls))
        runner.wait_log = lambda before, needles, s: False
        ok, msg, sid3 = start(text, use_key=False)
        check("没有快捷键、新对话没开出来：说清楚", ok is False and "没开出" in msg, msg)
        runner.locked = lambda: True
        check("锁屏：什么都不做", not start("x", wait=1)[0] and calls == [])
    finally:
        for k, f in real_fns.items():
            setattr(runner, k, f)
        os.startfile = real_startfile

    print("== 手机上回答编辑器里的选择题")
    tr2 = os.path.join(projects, "f--Work", "asking.jsonl")
    with open(tr2, "w", encoding="utf-8") as f:
        f.write(json.dumps({"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "toolu_9", "name": "AskUserQuestion",
                            "input": {"questions": []}}]}}, separators=(",", ":")) + "\n")
    acts, typed_answer, esc_works = [], [""], [True]
    names = ("locked", "locate", "open_project", "force_foreground", "log_sizes", "panel_focused", "foreground", "_keys", "type_text", "press_enter")
    real2 = {k: getattr(runner, k) for k in names}
    real_startfile = os.startfile

    def append(entry):
        with open(tr2, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n")

    def keys(events):   # 按 Esc：编辑器把提问撤下，记录里出现这个提问的结果
        acts.append(("keys", events))
        if esc_works[0] and events and events[0][0] == 0x1B:
            append({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "toolu_9", "is_error": True,
                                                              "content": "The user doesn't want to proceed"}]}})

    def enter():
        acts.append("enter")
        append({"type": "user", "message": {"content": typed_answer[0]}})

    try:
        runner.locked, runner.force_foreground, runner.log_sizes, runner.foreground = (lambda: False), (lambda h: True), (lambda: {}), (lambda: 5)
        runner.locate = lambda s, root, t=None: ((5, "cursor"), "cursor")
        runner.open_project = lambda root, hit, say, editor=None: hit
        runner.panel_focused = lambda before, s: acts.append("focused") or True
        runner._keys, runner.press_enter = keys, enter
        runner.type_text = lambda t: acts.append(("type", t)) or typed_answer.__setitem__(0, t)
        os.startfile = lambda url: acts.append(url)
        ok, msg = runner.answer_in_editor("sid-9", "F:/x", "toolu_9", "我回答你刚才的问题：\n· 用哪个？ → A", tr2)
        check("打开对话 → 面板拿到焦点 → 按 Esc 撤下提问 → 记录里确认撤下了 → 打字、回车 → 核对发出去了",
              ok and acts[0] == "cursor://anthropic.claude-code/open?session=sid-9" and acts[1] == "focused" and acts[2][1][0][0] == 0x1B
              and acts[3] == ("type", "我回答你刚才的问题：\n· 用哪个？ → A") and acts[4] == "enter", (ok, msg, acts))
        acts.clear()
        esc_works[0] = False
        with open(tr2, "a", encoding="utf-8") as f:
            f.write(json.dumps({"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "toolu_10", "name": "AskUserQuestion",
                                "input": {"questions": []}}]}}, separators=(",", ":")) + "\n")
        ok, msg = runner.answer_in_editor("sid-9", "F:/x", "toolu_10", "x", tr2)
        check("按了 Esc 提问没撤下来：不打字，叫用户回电脑前点", not ok and "没撤下来" in msg and not [a for a in acts if a == "enter" or isinstance(a, tuple) and a[0] == "type"],
              (msg, acts))
        acts.clear()
        runner.panel_focused = lambda before, s: False
        ok, msg = runner.answer_in_editor("sid-9", "F:/x", "toolu_10", "x", tr2)
        check("面板没拿到焦点：什么键都不按", not ok and not [a for a in acts if isinstance(a, tuple)], (msg, acts))
    finally:
        for k, f in real2.items():
            setattr(runner, k, f)
        os.startfile = real_startfile

    print("== 给编辑器加「新开 Claude 对话」的快捷键")
    kb = os.path.join(BOX, "kb", "keybindings.json")
    real_kb = dict(runner.KEYBINDINGS)
    runner.KEYBINDINGS["cursor"] = kb
    try:
        check("没有这个文件：没有快捷键", not runner.has_new_tab_key("cursor"))
        ok, msg = runner.add_new_tab_key("cursor")
        check("没有文件：新建一个，只有这一条", ok and json.loads(open(kb, encoding="utf-8").read()) ==
              [{"key": runner.NEW_TAB_KEY, "command": runner.NEW_TAB_COMMAND}], open(kb, encoding="utf-8").read())
        check("再加一次：不重复", runner.add_new_tab_key("cursor")[0] and open(kb, encoding="utf-8").read().count(runner.NEW_TAB_COMMAND) == 1)
        cases = {
            "已有几条、带注释": '// 我的快捷键\n[\n  { "key": "ctrl+k", "command": "a" }, // 注释\n  { "key": "ctrl+j", "command": "b" }\n]\n',
            "最后一行是注释": '[\n  { "key": "ctrl+k", "command": "a" }\n  // { "key": "ctrl+j", "command": "b" }\n]',
            "最后有个多余的逗号": '[\n  { "key": "ctrl+k", "command": "a" },\n]',
            "空列表": "[]",
            "字符串里有 // 和 ]": '[\n  { "key": "ctrl+k", "command": "a", "args": "http://x/]" }\n]',
        }
        for name, text in cases.items():
            with open(kb, "w", encoding="utf-8") as f:
                f.write(text)
            before_items = json.loads(runner.strip_jsonc(text))
            ok, msg = runner.add_new_tab_key("cursor")
            after = open(kb, encoding="utf-8").read()
            items = runner.read_keybindings(kb)
            check("加在末尾，原来的一条不少（%s）" % name, ok and items == before_items + [{"key": runner.NEW_TAB_KEY, "command": runner.NEW_TAB_COMMAND}]
                  and after.startswith(text[:text.rfind("]")].rstrip()) and open(kb + ".agent-hud.bak", encoding="utf-8").read() == text, (msg, after))
        with open(kb, "w", encoding="utf-8") as f:
            f.write("[ { 这不是 json")
        ok, msg = runner.add_new_tab_key("cursor")
        check("看不懂的文件：不改，说清楚", not ok and "认不出" in msg and open(kb, encoding="utf-8").read() == "[ { 这不是 json")
    finally:
        runner.KEYBINDINGS.clear()
        runner.KEYBINDINGS.update(real_kb)

    print("== 在编辑器窗口里发起的对话归到选的项目")
    preg = workspaces.Registry(os.path.join(BOX, "pin-ws.json"))
    box_ws = os.path.join(BOX, "Something", "agent-hud")
    os.makedirs(box_ws, exist_ok=True)
    preg._add(box_ws)
    pcol = collector.Collector(preg)
    pcol.pinned = {"s1": box_ws}
    cases = [("", "还没归到项目"), (os.path.join(BOX, "Something"), "只归到了窗口开的上级文件夹"), (os.path.join(BOX, "Other"), "碰了别的项目")]
    got = []
    for path, _ in cases:
        v = {"id": "s1", "ws": "x", "ws_path": path, "ws_color": "#000"}
        pcol.pin(v)
        got.append(v["ws_path"])
    check("没归到项目 / 只归到上级文件夹：按选的项目算；碰了别的项目：按实际的", got == [box_ws, box_ws, os.path.join(BOX, "Other")], got)

    print("== 右键「归到项目」手动指定：盖过一切")
    other_ws = os.path.join(BOX, "Something", "extraction-godot")
    os.makedirs(other_ws, exist_ok=True)
    preg._add(other_ws)
    pcol.assigned = {"s1": other_ws, "s2": os.path.join(BOX, "删掉的项目")}
    got = []
    for sid, path in [("s1", ""), ("s1", os.path.join(BOX, "Other")), ("s2", os.path.join(BOX, "Other"))]:
        v = {"id": sid, "ws": "x", "ws_path": path, "ws_color": "#000"}
        pcol.place(v)
        got.append(v["ws_path"])
    check("手动指定的盖过发起时选的和实际碰的；指定的项目不在清单里就不管", got == [other_ws, other_ws, os.path.join(BOX, "Other")], got)
finally:
    for s in list(r.procs):
        r.stop(s)
    shutil.rmtree(BOX, ignore_errors=True)

print("\n失败 %d 项：%s" % (len(fails), fails) if fails else "\n全部通过")
sys.exit(1 if fails else 0)
