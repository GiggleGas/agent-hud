"""Agent 悬浮窗的数据部分：读 ~/.claude/projects 下的会话记录，推断每个会话 / 子 agent 现在在干什么。

只读文件，不装 hook、不改任何配置。单独运行本文件会把当前看到的状态打印成 JSON。
"""
import glob
import json
import os
import re
import sys
import time
from collections import Counter
from datetime import datetime

import workspaces

PROJECTS = os.path.join(os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude"), "projects")

TRACK_WINDOW = 30 * 60   # 记录文件多久没动就不再跟踪
DONE_KEEP = 10 * 60      # 回合结束后还在窗口里留多久
STALE = 10 * 60          # 回合没结束、也没有工具在跑，这么久没动静就算断了
STALE_TOOL = 30 * 60     # 有工具在跑时放宽（长命令、前台子 agent）
CONFIRM_AFTER = 20       # 本该瞬间完成的工具卡了这么久，多半是在等用户点确认
QUEUE_WAIT = 10          # 一轮结束时还有排队的消息：这么久内当它在接着做；过了还没出队（比如对话被关了）就不等了
RESCAN = 5               # 多久重新找一遍新会话 / 新子 agent
INIT_CHUNK = 8 << 20
INIT_MAX = 128 << 20
SUB_INIT = 32 << 20

HUMAN = b'"origin":{"kind":"human"}'
ASK_TOOLS = {"AskUserQuestion": "在问你问题，等你回答", "ExitPlanMode": "计划写好了，等你批准"}
INSTANT_TOOLS = {"Read", "Edit", "Write", "Glob", "Grep", "NotebookEdit"}
LIVE = ("run", "ask", "confirm", "bg")
NOTE_KIND = {"completed": "done", "failed": "error"}
BG_TOOLS = ("Bash", "PowerShell")
OUT_TAIL = 4096          # 后台命令的输出只读最后这么多字节，取最后一行当进度

# 常驻的后台命令（开着的服务器、盯着文件变的）：起了就不会自己结束，不等它，对话照常算完成
RE_RESIDENT = re.compile(r"\b(?:server|serve|runserver|watch|nodemon|live-server|uvicorn|gunicorn|vite)\b|--port\b|--watch\b|"
                         r"http\.server|webpack-dev-server|flask run|tail -f|(?:npm|pnpm|yarn)(?: run)? (?:dev|start)\b", re.I)
RE_OUT_FILE = re.compile(r"Output is being written to: (.+?\.output)")
RE_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")

# 对话里的进度标记：单独一行「进度 2/5 · 这一步在做什么」，取最后一个
RE_MARK = re.compile(r"^[ \t>*_`#\-]*进度\s*[:：]?\s*(\d{1,3})\s*/\s*(\d{1,3})[ \t]*(?:[·•\-—–:：,，|][ \t]*)?(.*)$", re.M)
RE_NOTE = re.compile(r"<task-id>([^<]+)</task-id>.*?<status>([^<]+)</status>", re.S)


def stamp_of(e):
    try:
        return datetime.fromisoformat(e["timestamp"].replace("Z", "+00:00")).timestamp()
    except (KeyError, AttributeError, ValueError):
        return None


def first_line(text, limit=200):
    for line in str(text).splitlines():
        line = line.strip().lstrip("#>-*• ").replace("**", "").replace("`", "").strip()
        if line:
            return line[:limit]
    return ""


def describe(name, inp):
    """把一次工具调用说成一句人话。"""
    def g(k):
        return str(inp.get(k) or "").strip()

    def base(k):
        p = g(k).replace("\\", "/").rstrip("/")
        return p.rsplit("/", 1)[-1]

    if name in ("Bash", "PowerShell"):
        return "跑命令：" + (g("description") or first_line(g("command")))
    if name == "Read":
        return "读文件：" + base("file_path")
    if name == "Edit":
        return "改文件：" + base("file_path")
    if name == "NotebookEdit":
        return "改文件：" + base("notebook_path")
    if name == "Write":
        return "写文件：" + base("file_path")
    if name == "Grep":
        return "搜内容：" + g("pattern")
    if name == "Glob":
        return "找文件：" + g("pattern")
    if name == "Agent":
        return "派子 agent：" + g("description")
    if name == "Skill":
        return "用技能：" + g("skill")
    if name == "WebSearch":
        return "搜网页：" + g("query")
    if name == "WebFetch":
        return "读网页：" + g("url")
    if name == "SendMessage":
        return "给 agent 发消息：" + g("to")
    if name in ASK_TOOLS:
        return ASK_TOOLS[name]
    fixed = {"Workflow": "跑工作流", "Artifact": "发布页面", "ToolSearch": "找工具",
             "TaskStop": "停后台任务", "SubagentHandback": "交回结果"}
    if name in fixed:
        return fixed[name]
    if name.startswith("mcp__"):
        parts = name.split("__")
        return "调用 %s：%s" % (parts[1], parts[-1])
    return name


def out_tail(path):
    """后台命令输出文件的最后一行（去掉颜色控制符；进度条用 \\r 刷新的，取最后刷出来的那一截）。"""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            f.seek(max(0, size - OUT_TAIL))
            data = f.read().decode("utf-8", "replace")
    except OSError:
        return ""
    for line in reversed(data.splitlines()):
        line = RE_ANSI.sub("", line.rsplit("\r", 1)[-1]).strip()
        if line:
            return line[:200]
    return ""


def launch_root(cwd, folder):
    """从当前目录往上找出对话的启动目录。folder 是 projects 下的目录名，它就是启动目录把非字母数字
    换成 - 得来的。当前目录已经跑到启动目录外面去了就找不到，返回 None。"""
    parts = [p for p in re.split(r"[\\/]+", cwd) if p]
    for i in range(len(parts), 0, -1):
        root = "\\".join(parts[:i])
        if re.sub(r"[^A-Za-z0-9]", "-", root).lower() == folder.lower():
            return root
    return None


def last_line_with(path, needle):
    """从文件尾往回找最后一个含 needle 的行，返回它的行首偏移；INIT_MAX 以内找不到返回 None。"""
    size = os.path.getsize(path)
    end = size
    with open(path, "rb") as f:
        while end > 0 and size - end < INIT_MAX:
            start = max(0, end - INIT_CHUNK)
            f.seek(start)
            data = f.read(min(size, end + len(needle)) - start)
            i = data.rfind(needle)
            if i >= 0:
                pos = start + i
                while pos > 0:   # 这一行可能很长（带图），继续往回找换行
                    a = max(0, pos - (1 << 20))
                    f.seek(a)
                    j = f.read(pos - a).rfind(b"\n")
                    if j >= 0:
                        return a + j + 1
                    pos = a
                return 0
            end = start
    return None


class Tail:
    """增量读一个只会追加的 jsonl 文件。"""

    def __init__(self, path, pos=0, skip_partial=False):
        self.path, self.pos, self.skip, self.buf = path, pos, skip_partial, b""

    def lines(self):
        try:
            size = os.path.getsize(self.path)
            if size < self.pos:   # 文件被重写了，从头来
                self.pos, self.buf, self.skip = 0, b"", False
            if size == self.pos:
                return []
            with open(self.path, "rb") as f:
                f.seek(self.pos)
                data = f.read(size - self.pos)
        except OSError:
            return []
        self.pos += len(data)
        parts = (self.buf + data).split(b"\n")
        self.buf = parts.pop()
        if self.skip and parts:
            parts.pop(0)
            self.skip = False
        return [p for p in parts if p.strip()]


class Track:
    """一个记录文件（主会话或子 agent）共用的状态：在不在干活、在跑哪些工具、走了几步。"""

    def __init__(self, path, pos, skip_partial):
        self.path = path
        self.tail = Tail(path, pos, skip_partial)
        self.pending = {}      # tool_use_id -> (工具名, 说明, 开始时间)
        self.questions = {}    # 还没回答的 AskUserQuestion：tool_use_id -> 问题列表（原样）
        self.steps = 0
        self.phase = "think"   # think / write
        self.active = False
        self.start_ts = None
        self.end_ts = None
        self.end_kind = None   # done / interrupted / error
        self.last_ts = 0.0

    def pump(self):
        for raw in self.tail.lines():
            try:
                e = json.loads(raw)
            except ValueError:
                continue
            if isinstance(e, dict):
                self.feed(e)

    def feed(self, e):
        raise NotImplementedError

    def on_text(self, text):
        pass

    def on_tool(self, name, inp):
        pass

    def resume(self):
        self.active, self.end_ts, self.end_kind = True, None, None

    def finish(self, stamp, kind):
        self.active, self.end_ts, self.end_kind = False, stamp or self.last_ts, kind
        self.pending.clear()
        self.questions.clear()

    def on_assistant(self, e, msg, stamp):
        self.resume()
        handback = False
        for b in msg.get("content") or []:
            if not isinstance(b, dict):
                continue
            kind = b.get("type")
            if kind == "thinking":
                self.phase = "think"
            elif kind == "text":
                self.phase = "write"
                self.on_text(b.get("text") or "")
            elif kind == "tool_use":
                name = b.get("name") or "?"
                inp = b.get("input") if isinstance(b.get("input"), dict) else {}
                self.steps += 1
                self.pending[b.get("id")] = (name, describe(name, inp), stamp or time.time())
                if name == "AskUserQuestion" and isinstance(inp.get("questions"), list):
                    self.questions[b.get("id")] = inp["questions"]   # 手机上要显示问题和选项
                self.on_tool(name, inp)
                handback = handback or name == "SubagentHandback"
        if e.get("isApiErrorMessage"):
            self.finish(stamp, "error")
        elif handback or msg.get("stop_reason") in ("end_turn", "stop_sequence"):
            self.finish(stamp, "done")

    def on_results(self, content):
        """content 是工具结果就把对应的工具从 pending 里拿掉并返回 True。"""
        if not isinstance(content, list):
            return False
        ids = [b.get("tool_use_id") for b in content if isinstance(b, dict) and b.get("type") == "tool_result"]
        for i in ids:
            self.pending.pop(i, None)
            self.questions.pop(i, None)
        if ids:
            self.phase = "think"
        return bool(ids)

    def doing(self):
        if self.pending:
            calls = list(self.pending.values())
            more = "（共 %d 个在跑）" % len(calls) if len(calls) > 1 else ""
            return calls[0][1] + more
        return "写回复" if self.phase == "write" else "思考中"


class Sub(Track):
    def __init__(self, path, session):
        size = os.path.getsize(path)
        pos = max(0, size - SUB_INIT)
        super().__init__(path, pos, pos > 0)
        self.on_tool = session.on_tool   # 子 agent 动了哪个项目的文件，算在主会话头上
        self.aid = os.path.basename(path)[len("agent-"):-len(".jsonl")]
        self.desc = ""
        try:
            with open(path, "rb") as f:
                self.start_ts = stamp_of(json.loads(f.readline()))
        except (OSError, ValueError):
            pass
        self.prompt_ts = self.start_ts or 0.0   # 最近一次接到活的时间（被 SendMessage 续上也算）

    def read_meta(self):
        try:
            with open(self.path[:-len(".jsonl")] + ".meta.json", encoding="utf-8") as f:
                self.desc = json.load(f).get("description") or ""
        except (OSError, ValueError):
            pass

    def feed(self, e):
        stamp = stamp_of(e)
        if stamp:
            self.last_ts = max(self.last_ts, stamp)
        msg = e.get("message")
        if not isinstance(msg, dict):
            return
        if e.get("type") == "assistant":
            self.on_assistant(e, msg, stamp)
        elif e.get("type") == "user" and not self.on_results(msg.get("content")):
            self.prompt_ts = stamp or self.last_ts
            self.pending.clear()
            self.questions.clear()
            self.resume()

    def view(self, now, signal):
        if not self.desc:
            self.read_meta()
        # 主会话那边看到它交回 / 失败 / 被停，比它自己的记录更准
        if self.active and signal and signal[1] >= self.prompt_ts:
            self.finish(signal[1], signal[0])
        if self.active:
            state = "stale" if now - self.last_ts > STALE_TOOL else "run"
        else:
            state = self.end_kind or "done"
        return {
            "id": self.aid, "desc": self.desc or self.aid[:8], "state": state,
            "action": self.doing() if state == "run" else "", "steps": self.steps,
            "start": self.start_ts or self.last_ts, "end": self.end_ts or self.last_ts, "last": self.last_ts,
        }


class Session(Track):
    def __init__(self, path, reg, tail=None):
        """tail 给了就只读文件最后这么多字节（看历史记录用，够拿到标题和结尾）；
        不给就从最近一次用户发话开始读（跟踪正在进行的对话用）。"""
        pos = None if tail else last_line_with(path, HUMAN)
        skip = False
        if pos is None:
            pos = max(0, os.path.getsize(path) - (tail or INIT_CHUNK))
            skip = pos > 0
        super().__init__(path, pos, skip)
        self.reg = reg
        self.sid = os.path.basename(path)[:-len(".jsonl")]
        self.sub_dir = os.path.join(path[:-len(".jsonl")], "subagents")
        self.title = self.prompt = self.cwd = self.note = self.last_text = ""
        self.root = None      # 启动目录
        self.hits = Counter() # 这次任务里各项目被碰了多少下（项目的 key -> 分）
        self.ws = None        # 判断出来的项目；这次任务还没碰任何文件时沿用上一次的
        self.marker = None    # (第几步, 共几步, 这一步叫什么)
        self.job_ts = None    # 最近一次用户发话的时间
        self.signals = {}     # 子 agent id -> (结果, 时间)
        self.subs = {}
        self.queued = 0       # 排着队的消息条数（进队 - 出队 / 移除）：一轮结束时还有，说明它马上接着做
        self.launching = {}   # 刚发出去的后台命令：tool_use_id -> (说明, 命令)，等工具结果给出任务编号
        self.bg = {}          # 还在跑的后台命令：任务编号 -> {desc, start, out 输出文件, resident 常驻的}；跨回合，收到结束通知才划掉

    def scan_subs(self):
        since = (self.job_ts or self.last_ts or time.time()) - 60
        for p in glob.glob(os.path.join(self.sub_dir, "agent-*.jsonl")):
            try:
                if p not in self.subs and os.path.getmtime(p) >= since:
                    self.subs[p] = Sub(p, self)
            except OSError:
                pass

    def on_tool(self, name, inp):
        """这次工具调用碰的是哪个项目：看它带的路径、命令里提到的路径；都没有就按当时所在的目录算。"""
        found = [self.reg.match(inp[k]) for k in ("file_path", "notebook_path", "path") if inp.get(k)]
        if name in ("Bash", "PowerShell"):
            found += self.reg.find_in(inp.get("command") or "")
        found = [it for it in found if it] or [it for it in [self.reg.match(self.cwd)] if it]
        for it in found:
            self.hits[it["key"]] += 3 if name in ("Edit", "Write", "NotebookEdit") else 1
        if self.hits:
            top = max(self.hits, key=self.hits.get)
            self.ws = next((it for it in self.reg.items if it["key"] == top), self.ws)

    def workspace(self):
        return self.ws or self.reg.match(self.cwd) or (self.reg.match(self.root) if self.root else None)

    def pump(self):
        super().pump()
        for s in self.subs.values():
            s.pump()

    def on_text(self, text):
        marks = RE_MARK.findall(text)
        if marks:
            n, m, label = marks[-1]
            if 0 < int(m) and int(n) <= int(m):
                self.marker = (int(n), int(m), label.strip(" *_`"))
        line = first_line(RE_MARK.sub("", text))
        if line:
            self.note = line
            self.last_text = text.strip()[:4000]

    def feed(self, e):
        stamp = stamp_of(e)
        if stamp:
            self.last_ts = max(self.last_ts, stamp)
        if e.get("isSidechain"):
            return
        if e.get("cwd"):
            self.cwd = e["cwd"]
            if not self.root:
                self.root = launch_root(self.cwd, os.path.basename(os.path.dirname(self.path)))
                self.reg.ensure(self.root)
        kind = e.get("type")
        msg = e.get("message")
        if kind == "ai-title":
            self.title = e.get("aiTitle") or self.title
        elif kind == "last-prompt":
            self.prompt = self.prompt or first_line(e.get("lastPrompt") or "")
        elif kind == "system":
            if e.get("subtype") == "stop_hook_summary" and self.active:
                self.finish(stamp, "done")
        elif kind == "assistant" and isinstance(msg, dict):
            for b in msg.get("content") or []:   # 放到后台跑的命令：先记下说明和命令，工具结果里才有任务编号
                if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name") in BG_TOOLS:
                    inp = b.get("input") if isinstance(b.get("input"), dict) else {}
                    if inp.get("run_in_background"):
                        self.launching[b.get("id")] = (str(inp.get("description") or first_line(inp.get("command") or "")),
                                                       str(inp.get("command") or ""))
            self.on_assistant(e, msg, stamp)
        elif kind == "user" and isinstance(msg, dict):
            self.on_user(e, msg.get("content"), stamp)
        elif kind == "attachment" and isinstance(e.get("attachment"), dict):
            self.on_task_status(e["attachment"], stamp)
        elif kind == "queue-operation":   # 用户发的每句话都先进队；它忙的时候发的，要等这一轮完了才出队（或者中途被读掉、移除）
            self.queued = self.queued + 1 if e.get("operation") == "enqueue" else max(0, self.queued - 1)

    def add_bg(self, tid, desc, command, out, stamp):
        self.bg[tid] = {"desc": desc or tid, "start": stamp or self.last_ts, "out": out or "",
                        "resident": bool(RE_RESIDENT.search(desc + "\n" + command))}

    def on_task_status(self, att, stamp):
        """对话压缩后会把还在跑的后台任务重新列一遍（task_status）：悬浮窗中途才打开、没看到起命令那一下的，靠它补上。"""
        tid = att.get("taskId")
        if att.get("type") != "task_status" or att.get("taskType") != "local_bash" or not tid:
            return
        if att.get("status") != "running":
            self.bg.pop(tid, None)
        elif tid not in self.bg:
            shell = att.get("shell") if isinstance(att.get("shell"), dict) else {}
            self.add_bg(tid, str(att.get("description") or ""), str(shell.get("command") or ""), att.get("outputFilePath"), stamp)

    def on_user(self, e, content, stamp):
        if self.on_results(content):
            res = e.get("toolUseResult")
            # 前台子 agent：Agent 工具的结果回来就是它干完了
            if isinstance(res, dict) and res.get("agentId") and res.get("status") not in (None, "async_launched"):
                self.signals[res["agentId"]] = (NOTE_KIND.get(res["status"], "interrupted"), stamp or self.last_ts)
            # 后台命令起来了：工具结果里给了任务编号，输出文件的位置写在结果的文字里
            if isinstance(res, dict) and res.get("backgroundTaskId"):
                for b in content:
                    if isinstance(b, dict) and b.get("tool_use_id") in self.launching:
                        desc, command = self.launching.pop(b["tool_use_id"])
                        said = b.get("content")
                        said = said if isinstance(said, str) else " ".join(
                            x.get("text") or "" for x in said or [] if isinstance(x, dict))
                        found = RE_OUT_FILE.search(said)
                        self.add_bg(res["backgroundTaskId"], desc, command, found.group(1) if found else "", stamp)
            return
        if isinstance(content, str):
            text = content
        else:
            text = " ".join(b.get("text") or "" for b in content or [] if isinstance(b, dict) and b.get("type") == "text")
        text = text.strip()
        origin = e.get("origin") if isinstance(e.get("origin"), dict) else {}
        if origin.get("kind") == "peer":                  # 后台子 agent 把结果交回来了
            self.signals[origin.get("from")] = ("done", stamp or self.last_ts)
        elif origin.get("kind") == "task-notification":   # 后台任务结束 / 失败 / 被停
            m = RE_NOTE.search(text)
            if m:
                self.signals[m.group(1)] = (NOTE_KIND.get(m.group(2), "interrupted"), stamp or self.last_ts)
                self.bg.pop(m.group(1), None)
        elif text.startswith("[Request interrupted"):
            self.finish(stamp, "interrupted")
            return
        elif origin.get("kind") == "human" or ("origin" not in e and "permissionMode" in e):
            self.prompt = first_line(text) or self.prompt
            self.job_ts = stamp or self.last_ts
            self.steps, self.marker, self.note, self.last_text = 0, None, "", ""
            self.hits.clear()
        else:
            return   # 系统塞进来的说明（图片尺寸、压缩摘要等），不代表新回合
        self.pending.clear()
        self.questions.clear()
        self.phase = "think"
        self.resume()

    def bg_view(self, ignored):
        """还在跑的后台命令（用户说过「不用等」的不算）：输出文件最后动的时间和最后一行，文件没动就不重读。"""
        out = []
        for tid, b in self.bg.items():
            if tid in ignored:
                continue
            try:
                moved = os.path.getmtime(b["out"]) if b["out"] else b["start"]
            except OSError:
                moved = b["start"]
            if b.get("_moved") != moved:
                b["_moved"], b["_tail"] = moved, out_tail(b["out"]) if b["out"] else ""
            out.append({"id": tid, "desc": b["desc"], "start": b["start"], "moved": max(moved, b["start"]),
                        "tail": b["_tail"], "resident": b["resident"]})
        return sorted(out, key=lambda b: b["start"])

    def view(self, now, ignored=()):
        """ignored：用户在右键菜单里说过「不用等后台了」的后台命令 / 子 agent 编号，不再因为它们算「等后台」。"""
        subs = []
        for s in self.subs.values():
            v = s.view(now, self.signals.get(s.aid))
            if v["state"] == "run" or (self.job_ts and v["start"] >= self.job_ts - 5):
                subs.append(v)
        subs.sort(key=lambda v: v["start"])
        bg = self.bg_view(ignored)
        waiting = [b for b in bg if not b["resident"]]
        running = [v for v in subs if v["state"] == "run" and v["id"] not in ignored]
        last = max([self.last_ts] + [v["last"] for v in subs] + [b["moved"] for b in waiting])
        calls = list(self.pending.values())
        quiet = now - last
        action = ""
        if self.active:
            asking = [c for c in calls if c[0] in ASK_TOOLS]
            if asking:
                state, action = "ask", asking[0][1]
            elif quiet > (STALE_TOOL if calls else STALE):
                state, action = "stale", "可能已中断，%d 分钟没动静" % (quiet // 60)
            elif calls and all(c[0] in INSTANT_TOOLS for c in calls) and now - min(c[2] for c in calls) > CONFIRM_AFTER:
                state, action = "confirm", "可能在等你点确认 · " + calls[0][1]
            else:
                state, action = "run", self.doing()
        elif self.queued and now - (self.end_ts or 0) < QUEUE_WAIT:   # 这一轮完了，但你排队的话还没交给它：马上接着做，不算完成
            state, action = "run", "接着处理你排队的消息"
        elif waiting and quiet > STALE_TOOL:   # 对话被关掉时后台命令跟着没了，不会有结束通知
            state, action = "stale", "后台可能已经停了，%d 分钟没动静" % (quiet // 60)
        elif waiting or running:   # 这一轮说完了，但后台还有活：等它们结束会自己接着干，不算完成
            if waiting and running:
                state, action = "bg", "等后台：%d 个命令、%d 个子 agent" % (len(waiting), len(running))
            elif waiting:
                state, action = "bg", "等后台命令：" + waiting[0]["desc"] + ("（共 %d 个）" % len(waiting) if len(waiting) > 1 else "")
            else:
                state, action = "bg", "等子 agent 干完"
        else:
            state = self.end_kind or "done"
        start = self.job_ts or self.start_ts or last
        ws = self.workspace()
        asked = next(((k, q) for k, q in self.questions.items() if k in self.pending), None) if state == "ask" else None
        return {
            "question": {"tool_id": asked[0], "questions": asked[1]} if asked else None,   # 还没回答的选择题（手机上点选）
            "id": self.sid,
            "title": self.title or self.prompt or "（还没有标题）",
            "ws": ws["name"] if ws else os.path.basename(self.cwd.rstrip("\\/")) or "?",
            "ws_path": ws["path"] if ws else "", "ws_color": ws["color"] if ws else workspaces.PALETTE[0],
            "root": self.root or "",   # 启动目录：跳到编辑器时靠它找开着这个项目的那个窗口
            "owned": False,   # 是不是悬浮窗自己发起的后台任务，由 Collector 填
            "state": state, "action": action, "steps": self.steps,
            "line": self.note or ("任务：" + self.prompt if self.prompt else ""),
            "prompt": self.prompt, "last_text": self.last_text,
            "marker": self.marker,
            "start": start, "end": self.end_ts or last, "last": last,
            "timed": bool(self.job_ts),   # 读到了这次任务开始的那句话，用时才算得准
            "subs": subs,
            "bg": bg,   # 还在跑的后台命令（常驻的也在里面，resident 为真）
        }


def find_transcript(sid):
    hits = glob.glob(os.path.join(PROJECTS, "*", sid + ".jsonl"))
    return hits[0] if hits else None


def conversation(sid, turns=12, tail=4 << 20):
    """一个对话最近几轮的往来，给手机网页看：[{role: user, text, ts} | {role: assistant, text: 这一轮最后说的话,
    steps: [中间说的话…], ts}]，旧的在前。只读文件最后 tail 字节。"""
    path = find_transcript(sid)
    if not path:
        return []
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        f.seek(max(0, size - tail))
        lines = f.read().split(b"\n")[1 if size > tail else 0:]
    out = []
    for raw in lines:
        try:
            e = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(e, dict) or e.get("isSidechain"):
            continue
        att = e.get("attachment") if isinstance(e.get("attachment"), dict) else {}
        if att.get("type") == "queued_command" and att.get("commandMode", "prompt") == "prompt":
            # 用户在它忙的时候发的话：先排队，记成这种附件，等它手上这一步做完再交给它
            prompt = att.get("prompt")
            text = prompt if isinstance(prompt, str) else "\n".join(
                b.get("text") or "" for b in prompt or [] if isinstance(b, dict) and b.get("type") == "text")
            if text.strip():
                out.append({"role": "user", "text": text.strip(), "ts": stamp_of(e)})
            continue
        if not isinstance(e.get("message"), dict):
            continue
        content, ts = e["message"].get("content"), stamp_of(e)
        if e.get("type") == "user":
            if isinstance(content, list) and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
                continue
            origin = e.get("origin") if isinstance(e.get("origin"), dict) else {}
            if not (origin.get("kind") == "human" or ("origin" not in e and "permissionMode" in e)) or e.get("isMeta"):
                continue
            text = content if isinstance(content, str) else "\n".join(
                b.get("text") or "" for b in content or [] if isinstance(b, dict) and b.get("type") == "text")
            if text.strip():
                out.append({"role": "user", "text": text.strip(), "ts": ts})
        elif e.get("type") == "assistant":
            for b in content or []:
                if isinstance(b, dict) and b.get("type") == "text" and (b.get("text") or "").strip():
                    if out and out[-1]["role"] == "assistant":   # 同一轮里先说的挪进「过程」，最后一句当这一轮的结果
                        out[-1]["steps"].append(out[-1]["text"])
                        out[-1].update(text=b["text"].strip(), ts=ts)
                    else:
                        out.append({"role": "assistant", "text": b["text"].strip(), "steps": [], "ts": ts})
    users = [i for i, m in enumerate(out) if m["role"] == "user"]
    return out[users[-turns]:] if len(users) > turns else out


def placeholder(sid, task, reg, now):
    """后台任务刚起、会话记录还没写出来时，先顶上的一张「正在启动」。字段和 Session.view() 的一样。"""
    ws = reg.match(task["ws"])
    return {
        "id": sid, "title": first_line(task["prompt"]) or "（后台任务）",
        "ws": ws["name"] if ws else os.path.basename(task["ws"].rstrip("\\/")), "ws_path": task["ws"],
        "ws_color": ws["color"] if ws else workspaces.PALETTE[0], "owned": True,
        "state": "run", "action": "正在启动…", "steps": 0, "root": task["ws"],
        "line": "任务：" + first_line(task["prompt"]), "prompt": task["prompt"], "last_text": "", "marker": None,
        "start": task["created"], "end": now, "last": now, "subs": [], "bg": [],
    }


def session_files(sources):
    return ([(p, Session) for p in glob.glob(os.path.join(PROJECTS, "*", "*.jsonl"))]
            + [item for source in sources for item in source.session_files()])


def is_codex(view):
    return view.get("provider") == "codex" or view.get("id", "").startswith("codex:")


def grouped(views, sources, ignored=()):
    for source in sources:
        views = source.group_views(views, ignored)
    return views


log = lambda message: print(message, file=sys.stderr)


def read_failed(errors, path, error):
    message = "%s: %s" % (type(error).__name__, error)
    if errors.get(path) != message:
        log("读取记录失败，稍后重试：%s：%s" % (path, message))
    errors[path] = message


class History:
    """最近几天的对话（不管现在还跟不跟踪），给按项目的列表看历史用。
    每个文件只读最后一段，按修改时间缓存：文件没动过就不再读。"""
    TAIL = 1 << 20
    EVERY = 15   # 多少秒重新看一遍哪些文件动过

    def __init__(self, reg, days=3, sources=()):
        self.reg, self.days = reg, days
        self.sources, self.errors = sources, {}
        self.cache = {}   # 文件路径 -> (修改时间, 那时候的样子)
        self.next = 0.0
        self.items = []

    def recent(self, skip, now=None):
        """最近 days 天里动过的对话，除了 skip 里那些（正在跟踪的，由 Collector 给出更新的样子）。"""
        now = now or time.time()
        if now < self.next:
            return self.copies(skip)
        self.next = now + self.EVERY
        items, cache = [], {}
        for path, session_type in session_files(self.sources):
            try:
                mtime = os.path.getmtime(path)
            except OSError:
                continue
            if now - mtime > self.days * 86400:
                continue
            old = self.cache.get(path)
            if old and old[0] == mtime:
                v = old[1]
            else:
                try:
                    s = session_type(path, self.reg, tail=self.TAIL)
                    Track.pump(s)
                    v = s.view(now) if s.last_ts else None   # 只有标题之类、没有一句对话的文件不算
                except Exception as error:
                    read_failed(self.errors, path, error)
                    continue
                self.errors.pop(path, None)
            cache[path] = (mtime, v)
            if v:
                items.append(v)
        self.cache, self.items = cache, items
        return self.copies(skip)

    def copies(self, skip):
        """给出去的都是副本：外面会按发起方式、手动指定改项目，不能改到缓存里（不然撤掉手动指定也改不回来）。
        真正在干活的对话都在 skip 里（Collector 跟踪着）；剩下看着还在干活的，是停在半截没收尾的。"""
        views = grouped(self.items, self.sources)
        return [dict(v, state="stale") if v["state"] in LIVE else dict(v)
                for v in views if v["id"] not in skip and v.get("parent_id") not in skip]


class Collector:
    def __init__(self, reg=None, sources=()):
        self.reg = reg or workspaces.Registry()
        self.sources, self.errors = sources, {}
        self.owned = {}      # 悬浮窗自己发起的后台任务：会话 id -> 项目路径（发起时就定了，不用猜）
        self.pinned = {}     # 从悬浮窗选了编辑器窗口发起的对话：会话 id -> 发起时选的项目路径
        self.assigned = {}   # 用户在右键菜单里手动指定的：会话 id -> 项目路径（不管碰了什么文件，一律按它算）
        self.ignored = set()   # 用户说过「不用等后台了」的后台命令 / 子 agent 编号
        self.running = {}    # 其中进程还活着的：会话 id -> 这一轮是什么时候起的
        self.exited = {}     # 其中进程已经没了的：会话 id -> (done / error / interrupted, 时间)
        self.sessions = {}
        self.next_scan = 0.0

    def scan(self, now):
        cutoff = now - TRACK_WINDOW
        recent = {}
        files = session_files(self.sources)
        for path, session_type in files:
            try:
                if (os.path.getmtime(path) >= cutoff
                        or session_type is not Session and session_type.recently_active(path, cutoff)):
                    recent[path] = session_type
            except Exception as error:
                read_failed(self.errors, path, error)
        # 主会话在等一个跑很久的子 agent 时，自己的记录不动，靠子 agent 的记录把它找出来
        for sub in glob.glob(os.path.join(PROJECTS, "*", "*", "subagents", "agent-*.jsonl")):
            try:
                if os.path.getmtime(sub) >= cutoff:
                    recent[os.path.dirname(os.path.dirname(sub)) + ".jsonl"] = Session
            except OSError:
                pass
        for path, session_type in recent.items():
            if path not in self.sessions and os.path.isfile(path):
                try:
                    s = session_type(path, self.reg)
                    Track.pump(s)
                    self.sessions[path] = s
                    self.errors.pop(path, None)
                except Exception as error:
                    read_failed(self.errors, path, error)
        # A running child can keep a parent alive after its own rollout stops changing.
        known = {s.sid for s in self.sessions.values()}
        pending = list(self.sessions.values())
        while pending:
            child = pending.pop()
            parent = getattr(child, "parent_sid", None)
            if not parent or parent in known:
                continue
            match = next(((p, cls) for p, cls in files if p.endswith(child.parent_file_id + ".jsonl")), None)
            if match:
                path, session_type = match
                try:
                    s = session_type(path, self.reg)
                    Track.pump(s)
                    self.sessions[path] = s
                    known.add(s.sid)
                    pending.append(s)
                    self.errors.pop(path, None)
                except Exception as error:
                    read_failed(self.errors, path, error)
        for s in self.sessions.values():
            s.scan_subs()

    def adopt(self, v):
        """悬浮窗自己发起的任务：项目用发起时选的；在不在跑以进程为准，比会话记录快，也不怕进程被杀后记录停在半截。"""
        ws = self.reg.match(self.owned[v["id"]])
        v["owned"] = True
        if ws:
            v.update(ws=ws["name"], ws_path=ws["path"], ws_color=ws["color"])
        end, spawned = self.exited.get(v["id"]), self.running.get(v["id"])
        if spawned and v["state"] not in LIVE and v["end"] < spawned:   # 追问刚发出去，记录里还没出现
            v.update(state="run", action="正在启动…")
        elif end and not spawned and v["state"] in LIVE:
            v.update(state=end[0], action="", end=end[1])

    def pin(self, v):
        """在编辑器窗口里发起的对话，工作目录是窗口开的文件夹，可能是项目的上一级（比如开着整个 Something）。
        还没归到项目、或者只归到了那个上级文件夹时，按发起时选的项目算；碰了别的项目的文件，就按实际的算。"""
        ws = self.reg.match(self.pinned[v["id"]])
        now = os.path.normcase(v.get("ws_path") or "").rstrip("\\/")
        if ws and (not now or os.path.normcase(ws["path"]).startswith(now + os.sep) or os.path.normcase(ws["path"]) == now):
            v.update(ws=ws["name"], ws_path=ws["path"], ws_color=ws["color"])

    def assign(self, v):
        """用户手动指定的项目：盖过一切猜测（碰了哪些文件、启动目录、发起时选的）。项目从清单里删了就不管它。"""
        ws = self.reg.match(self.assigned[v["id"]])
        if ws:
            v.update(ws=ws["name"], ws_path=ws["path"], ws_color=ws["color"])

    def place(self, v):
        """一个对话最后归到哪个项目：先按发起方式，手动指定的最后盖上去。"""
        if v["id"] in self.owned:
            self.adopt(v)
        elif v["id"] in self.pinned:
            self.pin(v)
        if v["id"] in self.assigned:
            self.assign(v)

    def poll(self, now=None):
        """返回要显示的会话列表：在干活的在前（按开始时间），刚结束的在后（最近结束的在前）。"""
        now = now or time.time()
        if now >= self.next_scan:
            self.next_scan = now + RESCAN
            self.scan(now)
        live, rest = [], []
        raw_views, paths, expired_children = [], {}, []
        for path, s in list(self.sessions.items()):
            try:
                s.pump()
                v = s.view(now, self.ignored)
                self.errors.pop(path, None)
            except Exception as error:
                read_failed(self.errors, path, error)
                del self.sessions[path]
                continue
            raw_views.append(v)
            paths[v["id"]] = path
            if getattr(s, "parent_sid", None) and v["state"] not in LIVE and now - v["last"] > TRACK_WINDOW:
                expired_children.append(path)
        for v in grouped(raw_views, self.sources, self.ignored):
            path = paths[v["id"]]
            self.place(v)
            if v["state"] in LIVE:
                live.append(v)
            elif v["state"] == "stale":
                if now - v["last"] > TRACK_WINDOW:
                    del self.sessions[path]
                else:
                    rest.append(v)
            elif now - v["end"] <= DONE_KEEP:
                rest.append(v)
            elif now - v["last"] > TRACK_WINDOW:
                del self.sessions[path]
        live.sort(key=lambda v: v["start"])
        rest.sort(key=lambda v: -v["end"])
        for path in expired_children:
            self.sessions.pop(path, None)
        return live + rest


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    t0 = time.time()
    views = Collector().poll()
    print(json.dumps(views, ensure_ascii=False, indent=1))
    print("耗时 %.2fs，%d 个会话" % (time.time() - t0, len(views)), file=sys.stderr)
