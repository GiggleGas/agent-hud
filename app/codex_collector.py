"""Read local Codex rollouts without changing sessions or Codex configuration."""
import glob
import hashlib
import json
import os
import time

import install
from collector import INIT_CHUNK, LIVE, Session, Tail, first_line, last_line_with, stamp_of


def codex_home():
    return os.environ.get("CODEX_HOME") or os.path.join(install.TEST_ROOT or os.path.expanduser("~"), ".codex")


def identity_key(account, user):
    return hashlib.sha256(json.dumps(["chatgpt", account, user], separators=(",", ":"), ensure_ascii=False).encode("utf-8")).hexdigest()


class ReadState:
    """Read Desktop's account-scoped unread list; never write its global state."""
    LOCAL_HOST = "local:" + hashlib.sha256(b'["local","local",null]').hexdigest()

    def __init__(self):
        self.version = None
        self.identities = {}
        self.modified = 0

    def is_read(self, view):
        if not view.get("codex_desktop") or not view.get("read_identity") or view["state"] in LIVE:
            return False
        path = os.path.join(codex_home(), ".codex-global-state.json")
        try:
            stat = os.stat(path)
            version = (path, stat.st_mtime_ns, stat.st_size)
            if version != self.version:
                with open(path, encoding="utf-8") as stream:
                    state = json.load(stream).get("electron-thread-read-state-v1", {})
                self.identities = state.get("unreadByIdentity", {}) if state.get("version") == 1 else {}
                self.version, self.modified = version, stat.st_mtime
        except (OSError, ValueError):
            return False
        # A snapshot from before completion says nothing about this new result.
        if self.modified < view["end"]:
            return False
        hosts = self.identities.get(view["read_identity"], {})
        unread = hosts.get(self.LOCAL_HOST)
        return isinstance(unread, list) and view["id"][6:] not in unread


def session_files():
    return glob.glob(os.path.join(codex_home(), "sessions", "*", "*", "*", "*.jsonl"))


_title_cache = {}
_title_version = None


def session_title(sid):
    global _title_cache, _title_version
    path = os.path.join(codex_home(), "session_index.jsonl")
    try:
        version = (path, os.stat(path).st_mtime_ns)
    except FileNotFoundError:
        return ""
    if version != _title_version:
        titles = {}
        with open(path, encoding="utf-8") as stream:
            for line in stream:
                try:
                    item = json.loads(line)
                except ValueError:
                    continue  # The writer may not have finished the last line yet.
                titles[item["id"]] = item.get("thread_name") or ""
        _title_cache, _title_version = titles, version
    return _title_cache.get(sid, "")


def text_content(content):
    return "\n".join(b.get("text", "") for b in content or []
                     if isinstance(b, dict) and b.get("type", "").lower() in ("text", "input_text", "output_text"))


def group_views(views):
    """Attach child rollouts to their root conversation without changing cached views."""
    items = {v["id"]: dict(v, subs=list(v["subs"])) for v in views}
    children = set()
    for v in views:
        parent = items.get(v.get("parent_id"))
        if parent is None:
            continue
        while parent.get("parent_id") in items:
            parent = items[parent["parent_id"]]
        children.add(v["id"])
        if v["state"] not in LIVE and v["start"] < parent["start"] - 5:
            continue
        parent["subs"].append({
            "id": v["id"], "desc": v["agent_name"] or v["prompt"] or v["title"],
            "state": "run" if v["state"] in LIVE else v["state"],
            "action": v["action"], "steps": v["steps"],
            "start": v["start"], "end": v["end"], "last": v["last"],
        })
        parent["last"] = max(parent["last"], v["last"])
        if v["state"] in LIVE and parent["state"] not in LIVE:
            parent.update(state="subs", action="等子任务完成")
    return [v for sid, v in items.items() if sid not in children]


class CodexSession(Session):
    @staticmethod
    def recently_active(path, cutoff):
        # Windows can retain an old mtime while the rollout writer holds its file open.
        size = os.path.getsize(path)
        for chunk in (64 << 10, INIT_CHUNK):
            pos = max(0, size - chunk)
            for raw in reversed(Tail(path, pos, pos > 0).lines()):
                try:
                    stamp = stamp_of(json.loads(raw))
                except ValueError:
                    continue
                if stamp:
                    return stamp >= cutoff
            if pos == 0:
                break
        return False

    def __init__(self, path, reg, tail=None):
        super().__init__(path, reg, tail=tail or INIT_CHUNK)
        with open(path, "rb") as stream:
            meta = json.loads(stream.readline())["payload"]
        self.sid = "codex:" + (meta.get("id") or meta["session_id"])
        self.cwd = self.root = meta.get("cwd") or ""
        self.desktop = meta.get("originator") == "Codex Desktop"
        account, user = meta.get("creator_account_id"), meta.get("creator_user_id")
        self.read_identity = identity_key(account, user) if account and user else None
        self.reg.ensure(self.root)
        source = meta.get("source")
        self.child = meta.get("thread_source") == "subagent" or (isinstance(source, dict) and "subagent" in source)
        self.agent_path = meta.get("agent_path") or ""
        spawn = source.get("subagent", {}).get("thread_spawn", {}) if isinstance(source, dict) else {}
        self.parent_sid = "codex:" + spawn["parent_thread_id"] if spawn.get("parent_thread_id") else None
        self.agent_name = (self.agent_path or spawn.get("agent_path") or "").rsplit("/", 1)[-1]
        if tail is None:
            pos = last_line_with(path, b'"task_started"')
            if pos is not None:
                self.tail.pos, self.tail.skip = pos, False

    def scan_subs(self):
        # The shared collector groups Codex rollouts by their recorded parent id.
        pass

    def begin(self, stamp):
        self.job_ts = stamp or self.last_ts
        self.steps, self.marker, self.note, self.last_text = 0, None, "", ""
        self.pending.clear()
        self.questions.clear()
        self.hits.clear()
        self.phase = "think"
        self.resume()

    def user_text(self, text):
        if text.strip():
            self.prompt = first_line(text)

    def tool_call(self, item, stamp):
        name = item.get("name") or "?"
        raw = item.get("arguments", item.get("input", ""))
        try:
            args = json.loads(raw) if isinstance(raw, str) else raw
        except ValueError:
            args = {}
        if not isinstance(args, dict):
            args = {}
        short = name.rsplit(".", 1)[-1]
        action = {
            "exec": "执行脚本", "exec_command": "跑命令", "write_stdin": "等待命令结果",
            "apply_patch": "修改文件", "spawn_agent": "启动子任务", "wait_agent": "等待子任务",
            "wait": "等待执行结果", "request_user_input": "等你回答", "update_plan": "更新计划",
        }.get(short, "调用工具：" + name)
        if short == "exec_command":
            action += "：" + first_line(args.get("cmd") or args.get("command") or "")
            self.on_tool("PowerShell", {"command": args.get("cmd") or args.get("command") or ""})
        elif short == "update_plan":
            plan = args.get("plan") or []
            done = sum(p.get("status") == "completed" for p in plan)
            current = next((p.get("step", "") for p in plan if p.get("status") == "in_progress"), "")
            if plan:
                self.marker = (min(done + bool(current), len(plan)), len(plan), current)
        self.steps += 1
        self.pending[item.get("call_id") or item.get("id")] = (short, action, stamp or time.time())

    def feed(self, e):
        stamp = stamp_of(e)
        if stamp:
            self.last_ts = max(self.last_ts, stamp)
        p = e.get("payload") or {}
        kind = p.get("type")
        if e.get("type") == "turn_context":
            if p.get("cwd"):
                self.cwd = p["cwd"]
            return
        if e.get("type") == "event_msg":
            if kind in ("task_started", "turn_started"):
                self.begin(p.get("started_at") or stamp)
            elif kind == "user_message":
                if not self.active:
                    self.begin(stamp)
                self.user_text(p.get("message") or "")
            elif kind in ("agent_message", "agent_reasoning"):
                self.resume()
                if kind == "agent_message":
                    self.on_text(p.get("message") or "")
            elif kind in ("task_complete", "turn_complete"):
                self.job_ts = p.get("started_at") or self.job_ts
                if p.get("last_agent_message"):
                    self.on_text(p["last_agent_message"])
                self.finish(stamp, "done")
            elif kind in ("turn_aborted", "task_aborted"):
                self.finish(stamp, "interrupted")
            elif kind in ("error", "task_failed"):
                self.on_text(p.get("message") or "任务出错")
                self.finish(stamp, "error")
            elif kind == "item_completed":
                item = p.get("item") or {}
                if item.get("type") == "UserMessage":
                    self.user_text(text_content(item.get("content")))
                elif item.get("type") == "AgentMessage":
                    self.on_text(text_content(item.get("content")))
                    self.phase = "write"
                    if item.get("phase") == "final_answer":
                        self.finish(stamp, "done")
            return
        if e.get("type") != "response_item":
            return
        if kind in ("function_call", "custom_tool_call"):
            self.resume()
            self.tool_call(p, stamp)
        elif kind in ("function_call_output", "custom_tool_call_output"):
            self.pending.pop(p.get("call_id"), None)
            self.phase = "think"
        elif kind == "message" and p.get("role") == "assistant":
            self.resume()
            self.on_text(text_content(p.get("content")))
            self.phase = "write"
            if p.get("phase") == "final_answer":
                self.finish(stamp, "done")

    def view(self, now):
        v = super().view(now)
        prefix = "[Codex 子任务] " if self.child else "[Codex] "
        v["title"] = prefix + (session_title(self.sid[6:]) or self.prompt or self.agent_path or "任务")
        v["provider"] = "codex"
        v["codex_desktop"], v["read_identity"] = self.desktop, self.read_identity
        v["parent_id"], v["agent_name"] = self.parent_sid, self.agent_name
        # A transcript can identify a pending question, but it cannot prove approval waits.
        if self.active and any(c[0] == "request_user_input" for c in self.pending.values()):
            v.update(state="ask", action="等你回答（请在 Codex 中处理）")
        return v
