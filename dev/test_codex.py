"""Exercise rollout parsing and the shared collector with isolated fixture files."""
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

APP = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP))
import collector
import codex_collector
import workspaces


class CodexTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.env = patch.dict(os.environ, {"CODEX_HOME": str(self.base)})
        self.env.start()
        self.now = time.time()
        self.reg = workspaces.Registry(str(self.base / "workspaces.json"))
        self.path = self.base / "sessions/2026/10/19/rollout-test.jsonl"
        self.path.parent.mkdir(parents=True)
        self.meta = {"type": "session_meta", "payload": {"id": "same-id", "cwd": str(self.base), "source": "vscode"}}
        self.path.write_text(json.dumps(self.meta) + "\n", encoding="utf-8")
        self.session = codex_collector.CodexSession(str(self.path), self.reg)

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def event(self, kind, **data):
        return {"timestamp": datetime.fromtimestamp(self.now, timezone.utc).isoformat(),
                "type": "event_msg", "payload": dict(type=kind, **data)}

    def response(self, kind, **data):
        e = self.event(kind, **data)
        e["type"] = "response_item"
        return e

    def feed(self, *events):
        for e in events:
            self.session.feed(e)
        return self.session.view(self.now)

    def test_desktop_lifecycle(self):
        v = self.feed(self.event("task_started", started_at=self.now),
                      self.event("item_completed", item={"type": "UserMessage", "content": [{"type": "text", "text": "修复状态显示"}]}),
                      self.response("custom_tool_call", name="exec", call_id="call", input="await tools.exec_command({cmd:'pwd'})"))
        self.assertEqual((v["state"], v["title"], v["steps"]), ("run", "[Codex] 修复状态显示", 1))
        self.assertIn("执行脚本", v["action"])
        self.feed(self.response("custom_tool_call_output", call_id="call", output="ok"))
        self.assertFalse(self.session.pending)
        v = self.feed(self.event("item_completed", item={"type": "AgentMessage", "content": [{"type": "Text", "text": "完成"}], "phase": "final_answer"}),
                      self.event("task_complete", last_agent_message="完成"))
        self.assertEqual((v["state"], v["last_text"]), ("done", "完成"))

    def test_cli_events(self):
        v = self.feed(self.event("task_started"), self.event("user_message", message="检查文件"),
                      self.response("function_call", name="exec_command", call_id="cli", arguments=json.dumps({"cmd": "pwd"})))
        self.assertEqual(v["action"], "跑命令：pwd")
        self.assertEqual(v["state"], "run")
        v = self.feed(self.response("function_call_output", call_id="cli", output="ok"),
                      self.event("task_complete", last_agent_message="检查完成"))
        self.assertEqual(v["state"], "done")

    def test_abort_failure_and_resume(self):
        self.feed(self.event("task_started"), self.event("turn_aborted"))
        self.assertEqual(self.session.view(self.now)["state"], "interrupted")
        v = self.feed(self.event("task_started"), self.event("task_failed", message="失败"))
        self.assertEqual(v["state"], "error")
        v = self.feed(self.event("task_started"), self.event("user_message", message="继续"))
        self.assertEqual((v["state"], v["last_text"], v["steps"]), ("run", "", 0))

    def test_pending_question_only(self):
        v = self.feed(self.event("task_started"),
                      self.response("function_call", name="request_user_input", call_id="q", arguments="{}"))
        self.assertEqual(v["state"], "ask")
        v = self.feed(self.response("function_call_output", call_id="q", output="answer"))
        self.assertEqual(v["state"], "run")

    def test_plan_marker(self):
        plan = [{"step": "读取", "status": "completed"}, {"step": "修改", "status": "in_progress"}]
        v = self.feed(self.event("task_started"), self.response("function_call", name="update_plan", call_id="plan", arguments=json.dumps({"plan": plan})))
        self.assertEqual(v["marker"], (2, 2, "修改"))

    def test_partial_append_and_complete(self):
        start = json.dumps(self.event("task_started")) + "\n"
        data = json.dumps(self.event("user_message", message="增量读取"), ensure_ascii=False).encode("utf-8")
        with self.path.open("ab") as stream:
            stream.write(start.encode("utf-8") + data[:-4])
        self.session.pump()
        self.assertEqual(self.session.prompt, "")
        with self.path.open("ab") as stream:
            stream.write(data[-4:] + b"\n")
        self.session.pump()
        self.assertEqual(self.session.prompt, "增量读取")
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(self.event("task_complete")) + "\n")
        self.session.pump()
        self.assertEqual(self.session.view(self.now)["state"], "done")

    def test_title_index_and_subagent(self):
        index = self.base / "session_index.jsonl"
        index.write_text(json.dumps({"id": "same-id", "thread_name": "实际对话名称"}, ensure_ascii=False) + '\n{"id":', encoding="utf-8")
        self.assertEqual(self.session.view(self.now)["title"], "[Codex] 实际对话名称")
        self.meta["payload"]["thread_source"] = "subagent"
        self.path.write_text(json.dumps(self.meta) + "\n", encoding="utf-8")
        child = codex_collector.CodexSession(str(self.path), self.reg)
        self.assertTrue(child.view(self.now)["title"].startswith("[Codex 子任务]"))

    def test_latest_turn_reset(self):
        events = [self.meta, self.event("task_started"), self.event("user_message", message="旧任务"),
                  self.event("task_complete"), self.event("task_started"), self.event("user_message", message="新任务")]
        self.path.write_text("".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")
        s = codex_collector.CodexSession(str(self.path), self.reg)
        s.pump()
        self.assertEqual((s.prompt, s.view(self.now)["state"]), ("新任务", "run"))

    def test_shared_collector_and_history_keep_claude(self):
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(self.event("task_started")) + "\n")
            stream.write(json.dumps(self.event("user_message", message="Codex task")) + "\n")
        claude_dir = self.base / "claude/project"
        claude_dir.mkdir(parents=True)
        claude = [{"timestamp": self.event("x")["timestamp"], "type": "user", "cwd": str(self.base),
                   "origin": {"kind": "human"}, "message": {"content": "Claude task"}},
                  {"timestamp": self.event("x")["timestamp"], "type": "assistant",
                   "message": {"content": [{"type": "text", "text": "Done"}], "stop_reason": "end_turn"}}]
        (claude_dir / "same-id.jsonl").write_text("".join(json.dumps(e) + "\n" for e in claude), encoding="utf-8")
        with patch.object(collector, "PROJECTS", str(self.base / "claude")):
            views = collector.Collector(self.reg, sources=(codex_collector,)).poll(self.now)
            self.assertEqual({v["id"]: v["state"] for v in views}, {"same-id": "done", "codex:same-id": "run"})
            past = collector.History(self.reg, sources=(codex_collector,)).recent(set(), self.now)
            self.assertEqual({v["id"] for v in past}, {"same-id", "codex:same-id"})

    def test_no_false_approval_wait(self):
        self.feed(self.event("task_started"), self.response("function_call", name="exec_command", call_id="c", arguments="{}"))
        self.assertEqual(self.session.view(self.now + 25)["state"], "run")
        self.assertEqual(self.session.view(self.now + collector.STALE_TOOL + 1)["state"], "stale")

    def test_codex_does_not_open_claude(self):
        loader = importlib.machinery.SourceFileLoader("hud_test", str(APP / "agent_hud.pyw"))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        module = importlib.util.module_from_spec(spec)
        loader.exec_module(module)
        v = self.session.view(self.now)
        hud = object.__new__(module.Hud)
        hud.demo = False
        hud.find = lambda sid: v
        hud.mark_read = lambda sid: None
        with patch.object(module.runner, "open_in_editor") as open_claude, patch.object(module, "log"):
            hud.open_session(v["id"])
            open_claude.assert_not_called()
        import web
        with self.assertRaisesRegex(ValueError, "Codex"):
            web.Web(hud).reply(v["id"], "test")

    def add_child(self, sid="child", parent="same-id", finished=False):
        path = self.path.parent / ("rollout-" + sid + ".jsonl")
        meta = {"type": "session_meta", "payload": {
            "id": sid, "cwd": str(self.base), "thread_source": "subagent", "agent_path": "/root/" + sid,
            "source": {"subagent": {"thread_spawn": {"parent_thread_id": parent}}}}}
        events = [meta, self.event("task_started"), self.event("user_message", message=sid)]
        if finished:
            events.append(self.event("task_complete"))
        path.write_text("".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")
        return path

    def write_parent(self, finished=False):
        events = [self.meta, self.event("task_started"), self.event("user_message", message="主任务")]
        if finished:
            events.append(self.event("task_complete"))
        self.path.write_text("".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")

    def test_child_grouped_without_duplicate_history(self):
        self.write_parent()
        self.add_child(finished=True)
        with patch.object(collector, "PROJECTS", str(self.base / "no-claude")):
            col = collector.Collector(self.reg, sources=(codex_collector,))
            views = col.poll(self.now)
            self.assertEqual([v["id"] for v in views], ["codex:same-id"])
            self.assertEqual([(v["desc"], v["state"]) for v in views[0]["subs"]], [("child", "done")])
            history = collector.History(self.reg, sources=(codex_collector,))
            self.assertEqual(history.recent({"codex:same-id"}, self.now), [])
            self.assertEqual([v["id"] for v in history.copies(set())], ["codex:same-id"])
            self.assertEqual(history.cache[str(self.path)][1]["subs"], [])

    def test_running_child_revives_inactive_parent(self):
        self.write_parent(finished=True)
        self.add_child()
        os.utime(self.path, (self.now - 4000, self.now - 4000))
        # Parent lookup uses the normal rollout filename suffix, not a guessed title.
        parent_path = self.path.with_name("rollout-same-id.jsonl")
        self.path.rename(parent_path)
        with patch.object(collector, "PROJECTS", str(self.base / "no-claude")):
            views = collector.Collector(self.reg, sources=(codex_collector,)).poll(self.now)
        self.assertEqual(len(views), 1)
        self.assertEqual((views[0]["id"], views[0]["state"]), ("codex:same-id", "bg"))
        self.assertEqual(views[0]["subs"][0]["state"], "run")

    def test_nested_children_share_root(self):
        self.write_parent()
        self.add_child()
        self.add_child("grandchild", "child")
        with patch.object(collector, "PROJECTS", str(self.base / "no-claude")):
            views = collector.Collector(self.reg, sources=(codex_collector,)).poll(self.now)
        self.assertEqual([v["id"] for v in views], ["codex:same-id"])
        self.assertEqual({s["id"] for s in views[0]["subs"]}, {"codex:child", "codex:grandchild"})

    def test_previous_turn_child_does_not_reappear(self):
        self.write_parent()
        self.now -= 60
        self.add_child(finished=True)
        self.now += 60
        with patch.object(collector, "PROJECTS", str(self.base / "no-claude")):
            views = collector.Collector(self.reg, sources=(codex_collector,)).poll(self.now)
        self.assertEqual(len(views), 1)
        self.assertEqual(views[0]["subs"], [])

    def native_view(self):
        self.session.desktop = True
        self.session.read_identity = codex_collector.identity_key("account", "user")
        return self.feed(self.event("task_started"), self.event("task_complete"))

    def write_read_state(self, unread, identity=None, host=None, modified=None):
        path = self.base / ".codex-global-state.json"
        identity = identity or codex_collector.identity_key("account", "user")
        host = host or codex_collector.ReadState.LOCAL_HOST
        path.write_text(json.dumps({"electron-thread-read-state-v1": {
            "version": 1, "unreadByIdentity": {identity: {host: unread}}}}), encoding="utf-8")
        stamp = modified if modified is not None else self.now + 1
        os.utime(path, (stamp, stamp))
        return path

    def test_native_read_and_unread_changes(self):
        view = self.native_view()
        reads = codex_collector.ReadState()
        self.write_read_state(["same-id"])
        self.assertFalse(reads.is_read(view))
        self.write_read_state([], modified=self.now + 2)
        self.assertTrue(reads.is_read(view))
        self.write_read_state(["same-id"], modified=self.now + 3)
        self.assertFalse(reads.is_read(view))

    def test_native_read_identity_and_host_isolation(self):
        view = self.native_view()
        self.write_read_state([], identity="other-account")
        self.assertFalse(codex_collector.ReadState().is_read(view))
        self.write_read_state([], host="ssh:other-machine")
        self.assertFalse(codex_collector.ReadState().is_read(view))

    def test_native_snapshot_before_completion_is_unknown(self):
        view = self.native_view()
        self.write_read_state([], modified=self.now - 2)
        self.assertFalse(codex_collector.ReadState().is_read(view))

    def test_native_read_missing_or_inapplicable_state(self):
        view = self.native_view()
        reads = codex_collector.ReadState()
        self.assertFalse(reads.is_read(view))
        path = self.write_read_state([])
        self.assertFalse(reads.is_read(dict(view, codex_desktop=False)))
        self.assertFalse(reads.is_read(dict(view, read_identity=None)))
        self.assertFalse(reads.is_read(dict(view, state="run")))
        self.assertFalse(reads.is_read(dict(view, state="bg")))
        path.write_text('{"electron-thread-read-state-v1":{"version":2}}', encoding="utf-8")
        self.assertFalse(reads.is_read(view))
        path.write_text('{"electron-thread-', encoding="utf-8")
        self.assertFalse(reads.is_read(view))

    def make_hud(self):
        import queue
        loader = importlib.machinery.SourceFileLoader("read_hud_test", str(APP / "agent_hud.pyw"))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        module = importlib.util.module_from_spec(spec)
        loader.exec_module(module)
        hud = object.__new__(module.Hud)
        hud.unread = []
        hud.cfg = {"unread": hud.unread, "seen": [], "flash_on_done": True, "notify_url": "", "bg_ignored": []}
        hud.quiet = {}
        hud.codex_reads = codex_collector.ReadState()
        hud.views, hud.history = [], []
        hud.events = queue.Queue()
        hud.ws_map = {}
        hud.demo, hud.shot, hud.gone = False, True, False
        hud.render = lambda: None
        hud.start_flash = lambda: None
        hud.save = lambda: (self.base / "hud-config.json").write_text(json.dumps(hud.cfg), encoding="utf-8")
        hud.test_module = module
        return hud

    def test_native_read_clears_existing_badge_and_persists(self):
        view = self.native_view()
        self.write_read_state(["same-id"])
        hud = self.make_hud()
        hud.views = [view]
        hud.on_event("finished", view, False)
        self.assertEqual(len(hud.unread), 1)
        self.write_read_state([], modified=self.now + 2)
        hud.sync()
        self.assertEqual(hud.unread, [])
        saved = json.loads((self.base / "hud-config.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["unread"], [])
        restarted = self.make_hud()
        restarted.cfg["seen"] = saved["seen"]
        restarted.on_event("finished", view, False)
        self.assertEqual(restarted.unread, [])

    def test_native_read_suppresses_initial_unread_event(self):
        view = self.native_view()
        self.write_read_state([])
        hud = self.make_hud()
        with patch.object(hud, "start_flash") as flash:
            hud.on_event("finished", view, True)
            flash.assert_not_called()
        self.assertEqual(hud.unread, [])

    def test_next_turn_gets_new_unread_badge(self):
        view = self.native_view()
        self.write_read_state([])
        hud = self.make_hud()
        hud.on_event("finished", view, False)
        next_view = dict(view, start=self.now + 4, end=self.now + 5)
        # The old Desktop snapshot cannot acknowledge a newer result.
        hud.on_event("finished", next_view, False)
        self.assertEqual(len(hud.unread), 1)
        self.write_read_state(["same-id"], modified=self.now + 6)
        hud.views = [next_view]
        hud.sync()
        self.assertEqual(len(hud.unread), 1)

    def test_manual_read_still_works_without_desktop_state(self):
        view = self.native_view()
        hud = self.make_hud()
        hud.on_event("finished", view, False)
        hud.mark_read(view["id"])
        hud.on_event("finished", view, False)
        self.assertEqual(hud.unread, [])

    def test_old_unread_snapshot_resolves_metadata_after_live_retention(self):
        from types import SimpleNamespace
        self.meta["payload"].update(originator="Codex Desktop", creator_account_id="account", creator_user_id="user")
        self.now -= collector.DONE_KEEP + 60
        self.write_parent(finished=True)
        self.now += collector.DONE_KEEP + 60
        self.write_read_state([])
        hud = self.make_hud()
        hud.reg = self.reg
        hud.cfg.update(history_days=3, pinned={}, assigned={})
        hud.runner = SimpleNamespace(tick=lambda: None, owned=lambda: {}, active=lambda: {}, exited={}, starting=lambda ids: [])
        old = codex_collector.CodexSession(str(self.path), self.reg)
        old.pump()
        snapshot = old.view(self.now)
        snapshot.pop("codex_desktop")
        snapshot.pop("read_identity")
        snapshot["key"] = "%s@%d" % (snapshot["id"], snapshot["end"])
        hud.unread.append(snapshot)
        with patch.object(collector, "PROJECTS", str(self.base / "no-claude")), \
                patch.object(hud.test_module.time, "sleep", side_effect=StopIteration):
            with self.assertRaises(StopIteration):
                hud.worker()
        self.assertFalse(hud.views)
        self.assertEqual(len(hud.history), 1)
        hud.sync()
        self.assertFalse(hud.unread)

    def test_open_codex_rollout_with_stale_windows_mtime(self):
        self.write_parent()
        os.utime(self.path, (self.now - 4000, self.now - 4000))
        with patch.object(collector, "PROJECTS", str(self.base / "no-claude")):
            views = collector.Collector(self.reg, sources=(codex_collector,)).poll(self.now)
        self.assertEqual([(v["id"], v["state"]) for v in views], [("codex:same-id", "run")])
        self.assertFalse(codex_collector.CodexSession.recently_active(str(self.path), self.now + 1))

    def claude_record(self, finished=None):
        directory = self.base / "claude/project"
        directory.mkdir(parents=True, exist_ok=True)
        stamp = datetime.fromtimestamp(finished or self.now, timezone.utc).isoformat()
        messages = [
            {"timestamp": stamp, "type": "user", "cwd": str(self.base), "origin": {"kind": "human"},
             "message": {"content": "Claude task"}},
            {"timestamp": stamp, "type": "assistant", "message": {
                "content": [{"type": "text", "text": "Claude done"}], "stop_reason": "end_turn"}},
        ]
        path = directory / "claude-id.jsonl"
        path.write_text("".join(json.dumps(e) + "\n" for e in messages), encoding="utf-8")
        return path

    def check_bad_header_isolated(self, raw):
        self.write_parent()
        claude = self.claude_record()
        bad = self.path.with_name("rollout-bad.jsonl")
        bad.write_text(raw, encoding="utf-8")
        with patch.object(collector, "PROJECTS", str(self.base / "claude")), patch.object(collector, "log") as log:
            col = collector.Collector(self.reg, sources=(codex_collector,))
            self.assertEqual({v["id"] for v in col.poll(self.now)}, {"claude-id", "codex:same-id"})
            self.assertEqual(col.next_scan, self.now + collector.RESCAN)
            with claude.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps({"timestamp": self.event("x")["timestamp"], "type": "assistant", "message": {
                    "content": [{"type": "text", "text": "Claude still updates"}], "stop_reason": "end_turn"}}) + "\n")
            views = {v["id"]: v for v in col.poll(self.now + 1)}
            self.assertEqual(views["claude-id"]["last_text"], "Claude still updates")
            history = collector.History(self.reg, sources=(codex_collector,))
            self.assertEqual({v["id"] for v in history.recent(set(), self.now)}, {"claude-id", "codex:same-id"})
            log.assert_called()
            repaired = dict(self.meta, payload=dict(self.meta["payload"], id="bad"))
            bad.write_text(json.dumps(repaired) + "\n" + json.dumps(self.event("task_started")) + "\n", encoding="utf-8")
            self.assertIn("codex:bad", {v["id"] for v in col.poll(self.now + collector.RESCAN + 1)})

    def test_partial_header_does_not_stop_claude_and_recovers(self):
        self.check_bad_header_isolated('{"type":"session_meta","payl')

    def test_missing_session_id_does_not_stop_claude_and_recovers(self):
        self.check_bad_header_isolated('{"type":"session_meta","payload":{}}\n')

    def test_non_object_payload_item_and_plan_are_ignored(self):
        self.write_parent()
        self.claude_record()
        malformed = [dict(self.event("x"), payload=[1]), dict(self.event("x"), payload="wrong"),
                     self.event("item_completed", item=[1]), self.event("item_completed", item="wrong"),
                     self.response("function_call", name="update_plan", arguments=json.dumps({"plan": [None, "wrong"]})),
                     self.event("task_complete")]
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write("".join(json.dumps(e) + "\n" for e in malformed))
        with patch.object(collector, "PROJECTS", str(self.base / "claude")):
            views = collector.Collector(self.reg, sources=(codex_collector,)).poll(self.now)
            self.assertEqual({v["id"]: v["state"] for v in views}, {"claude-id": "done", "codex:same-id": "done"})

    def test_pump_failure_isolated_and_retried_at_next_scan(self):
        self.write_parent()
        self.claude_record()
        with patch.object(collector, "PROJECTS", str(self.base / "claude")), patch.object(collector, "log"):
            col = collector.Collector(self.reg, sources=(codex_collector,))
            col.poll(self.now)
            with patch.object(col.sessions[str(self.path)], "pump", side_effect=RuntimeError("broken record")):
                self.assertEqual([v["id"] for v in col.poll(self.now + 1)], ["claude-id"])
            self.assertIn("codex:same-id", {v["id"] for v in col.poll(self.now + collector.RESCAN + 1)})

    def test_history_parser_failure_isolated_and_recovers(self):
        self.write_parent(finished=True)
        self.claude_record()
        with patch.object(collector, "PROJECTS", str(self.base / "claude")), patch.object(collector, "log"):
            history = collector.History(self.reg, sources=(codex_collector,))
            with patch.object(codex_collector.CodexSession, "view", side_effect=AttributeError("broken record")):
                self.assertEqual([v["id"] for v in history.recent(set(), self.now)], ["claude-id"])
            self.assertEqual({v["id"] for v in history.recent(set(), self.now + history.EVERY + 1)},
                             {"claude-id", "codex:same-id"})

    def test_unreadable_old_parent_does_not_stop_child_or_claude(self):
        self.write_parent()
        self.claude_record()
        self.add_child(parent="bad")
        bad = self.path.with_name("rollout-bad.jsonl")
        bad.write_text('{"type":"session_meta","payload":{}}\n', encoding="utf-8")
        os.utime(bad, (self.now - 4000, self.now - 4000))
        with patch.object(collector, "PROJECTS", str(self.base / "claude")), patch.object(collector, "log"):
            views = collector.Collector(self.reg, sources=(codex_collector,)).poll(self.now)
        self.assertEqual({v["id"] for v in views}, {"claude-id", "codex:same-id", "codex:child"})

    def test_parent_cycles_return_without_hiding_records(self):
        base = self.session.view(self.now)
        cases = [[dict(base, id="codex:a", parent_id="codex:b"), dict(base, id="codex:b", parent_id="codex:a")],
                 [dict(base, id="codex:a", parent_id="codex:a")]]
        code = ("import json, sys, codex_collector; "
                "print(json.dumps([codex_collector.group_views(v) for v in json.load(sys.stdin)]))")
        proc = subprocess.run([sys.executable, "-c", code], input=json.dumps(cases), text=True,
                              capture_output=True, timeout=5, env=dict(os.environ, PYTHONPATH=str(APP)))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual([{v["id"] for v in vs} for vs in json.loads(proc.stdout)],
                         [{"codex:a", "codex:b"}, {"codex:a"}])

    def test_started_at_seconds_milliseconds_iso_and_unknown(self):
        iso = datetime.fromtimestamp(self.now, timezone.utc).isoformat()
        for value in (self.now, self.now * 1000, iso, None, "unknown", [], True):
            with self.subTest(value=value):
                v = self.feed(self.event("task_started", started_at=value), self.event("task_complete", started_at=value))
                self.assertAlmostEqual(v["start"], self.now, delta=1e-5)
                self.assertAlmostEqual(v["end"] - v["start"], 0, delta=1e-5)

    def test_old_rollout_tail_cache_invalidates_on_append(self):
        self.write_parent()
        codex_collector.CodexSession.latest_stamp.cache_clear()
        original = codex_collector.Tail.lines
        with patch.object(codex_collector.Tail, "lines", autospec=True, side_effect=original) as lines:
            active = codex_collector.CodexSession.recently_active
            self.assertTrue(active(str(self.path), self.now - 1))
            self.assertFalse(active(str(self.path), self.now + 1))
            self.assertEqual(lines.call_count, 1)
            self.now += 2
            with self.path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(self.event("agent_message", message="new line")) + "\n")
            self.assertTrue(active(str(self.path), self.now - 1))
            self.assertEqual(lines.call_count, 2)

    def test_unrecognized_read_state_shapes_stay_unread(self):
        view = self.native_view()
        identity = view["read_identity"]
        documents = [[], None, {"electron-thread-read-state-v1": []},
                     {"electron-thread-read-state-v1": {"version": 1, "unreadByIdentity": []}},
                     {"electron-thread-read-state-v1": {"version": 1, "unreadByIdentity": {identity: []}}}]
        path = self.base / ".codex-global-state.json"
        reads = codex_collector.ReadState()
        for i, document in enumerate(documents):
            with self.subTest(document=document):
                path.write_text(json.dumps(document), encoding="utf-8")
                os.utime(path, (self.now + i + 1, self.now + i + 1))
                self.assertFalse(reads.is_read(view))

    def run_worker_once(self, hud):
        from types import SimpleNamespace
        hud.reg = self.reg
        hud.cfg.update(history_days=3, pinned={}, assigned={}, live_only=False)
        hud.runner = SimpleNamespace(tick=lambda: None, owned=lambda: {}, active=lambda: {}, exited={}, starting=lambda ids: [])
        with patch.object(collector, "PROJECTS", str(self.base / "claude")), \
                patch.object(hud.test_module.time, "sleep", side_effect=StopIteration):
            with self.assertRaises(StopIteration):
                hud.worker()

    def test_claude_history_has_no_gap_between_ten_and_thirty_minutes(self):
        self.claude_record(finished=self.now - collector.DONE_KEEP - 60)
        hud = self.make_hud()
        self.run_worker_once(hud)
        self.assertEqual(hud.views, [])
        self.assertEqual([v["id"] for v in hud.history], ["claude-id"])
        hud.unread.append(dict(hud.history[0], key="old"))
        self.assertEqual([v["id"] for v in hud.ended_items()], ["claude-id"])

    def test_disable_codex_does_not_scan_read_or_flash(self):
        self.write_parent(finished=True)
        self.claude_record()
        hud = self.make_hud()
        hud.cfg["codex_on"] = False
        with patch.object(codex_collector, "session_files") as scan:
            self.run_worker_once(hud)
            scan.assert_not_called()
        self.assertEqual([v["id"] for v in hud.views], ["claude-id"])
        view = self.native_view()
        hud.unread.append(dict(view, key="hidden"))
        with patch.object(hud.codex_reads, "is_read") as read, patch.object(hud, "start_flash") as flash:
            hud.on_event("finished", view, True)
            hud.sync()
            read.assert_not_called()
            flash.assert_not_called()
        self.assertEqual(hud.ended_items()[0]["id"], "claude-id")
        self.assertEqual([u["key"] for u in hud.unread if collector.is_codex(u)], ["hidden"])

    def fixture_session(self, name):
        fixture = Path(__file__).resolve().parent / "fixtures/codex" / (name + ".jsonl")
        path = self.base / (name + ".jsonl")
        path.write_bytes(fixture.read_bytes())
        return codex_collector.CodexSession(str(path), self.reg)

    def test_redacted_real_desktop_and_child_records(self):
        parent, child = self.fixture_session("desktop"), self.fixture_session("child")
        parent.pump()
        child.pump()
        grouped = codex_collector.group_views([parent.view(self.now), child.view(self.now)])
        self.assertEqual([v["id"] for v in grouped], ["codex:fixture-parent"])
        self.assertEqual(grouped[0]["start"], 1760000000)
        self.assertEqual(grouped[0]["end"] - grouped[0]["start"], 118)
        self.assertEqual([(s["id"], s["state"]) for s in grouped[0]["subs"]], [("codex:fixture-child", "done")])

    def test_redacted_real_question_lifecycle(self):
        session = self.fixture_session("question")
        with open(session.path, encoding="utf-8") as stream:
            events = [json.loads(line) for line in stream][1:]
        states = []
        for event in events:
            session.feed(event)
            states.append(session.view(collector.stamp_of(event))["state"])
        self.assertEqual(states, ["run", "ask", "run", "done"])

    def test_codex_view_forwards_ignored_and_preserves_background_fields(self):
        self.feed(self.event("task_started"), self.event("task_complete"))
        self.session.add_bg("command-id", "fixture command", "echo fixture", "", self.now)
        view = self.session.view(self.now)
        self.assertEqual(view["state"], "bg")
        self.assertEqual([b["id"] for b in view["bg"]], ["command-id"])
        ignored = self.session.view(self.now, {"command-id"})
        self.assertEqual((ignored["state"], ignored["bg"]), ("done", []))

    def test_parent_waiting_in_bg_survives_completion_retention(self):
        import hub
        self.now -= collector.TRACK_WINDOW + 60
        self.write_parent(finished=True)
        self.now += collector.TRACK_WINDOW + 60
        self.path.rename(self.path.with_name("rollout-same-id.jsonl"))
        self.add_child()
        events = hub.Hub()
        with patch.object(collector, "PROJECTS", str(self.base / "no-claude")):
            col = collector.Collector(self.reg, sources=(codex_collector,))
            for now in (self.now, self.now + 1):
                views = col.poll(now)
                self.assertEqual([(v["id"], v["state"]) for v in views], [("codex:same-id", "bg")])
                self.assertIn(views[0]["state"], collector.LIVE)
                self.assertGreater(now - views[0]["end"], collector.DONE_KEEP)
                self.assertEqual(views[0]["bg"], [])
                self.assertEqual(events.update(views, now), [])

    def test_stop_waiting_for_codex_child_is_quiet_and_keeps_child_running(self):
        self.write_parent(finished=True)
        child = self.add_child()
        hud = self.make_hud()
        with patch.object(collector, "PROJECTS", str(self.base / "no-claude")):
            col = collector.Collector(self.reg, sources=(codex_collector,))
            waiting = col.poll(self.now)[0]
            self.assertEqual(waiting["state"], "bg")
            with patch.object(hud.test_module, "log"):
                hud.stop_waiting(waiting["id"], waiting)
            self.assertEqual(hud.cfg["bg_ignored"], ["codex:child"])
            col.ignored = set(hud.cfg["bg_ignored"])
            finished = col.poll(self.now + 1)[0]
            self.assertEqual(finished["state"], "done")
            self.assertTrue(col.sessions[str(child)].active)
            with patch.object(hud, "start_flash") as flash:
                hud.on_event("finished", finished, True)
                flash.assert_not_called()
            self.assertEqual(hud.unread, [])
            self.assertEqual(hud.quiet, {})

    def test_ignored_child_does_not_hide_other_waiting_children(self):
        self.write_parent(finished=True)
        self.add_child()
        self.add_child("second-child")
        with patch.object(collector, "PROJECTS", str(self.base / "no-claude")):
            col = collector.Collector(self.reg, sources=(codex_collector,))
            col.ignored = {"codex:child"}
            self.assertEqual(col.poll(self.now)[0]["state"], "bg")
            col.ignored.add("codex:second-child")
            self.assertEqual(col.poll(self.now + 1)[0]["state"], "done")

    def test_worker_passes_ignored_codex_children_to_collector(self):
        self.write_parent(finished=True)
        self.add_child()
        hud = self.make_hud()
        hud.cfg["bg_ignored"] = ["codex:child"]
        self.run_worker_once(hud)
        self.assertEqual([(v["id"], v["state"]) for v in hud.views], [("codex:same-id", "done")])

    def test_codex_toggle_preserves_claude_completion_notification(self):
        from types import SimpleNamespace
        path = self.claude_record()
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
        path.write_text(lines[0], encoding="utf-8")
        hud = self.make_hud()
        hud.reg = self.reg
        hud.cfg.update(history_days=3, pinned={}, assigned={})
        hud.runner = SimpleNamespace(tick=lambda: None, owned=lambda: {}, active=lambda: {}, exited={}, starting=lambda ids: [])
        sleeps = []

        def toggle(_):
            if sleeps:
                raise StopIteration
            sleeps.append(True)
            hud.cfg["codex_on"] = False
            path.write_text("".join(lines), encoding="utf-8")

        with patch.object(collector, "PROJECTS", str(self.base / "claude")), \
                patch.object(hud.test_module.hub, "SETTLE", 0), patch.object(hud.test_module.time, "sleep", side_effect=toggle):
            with self.assertRaises(StopIteration):
                hud.worker()
        events = list(hud.events.queue)
        self.assertEqual([(kind, view["id"], notify) for kind, view, notify in events], [("finished", "claude-id", True)])


if __name__ == "__main__":
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(CodexTests))
    print("全部通过" if result.wasSuccessful() else "测试失败")
    sys.exit(0 if result.wasSuccessful() else 1)
