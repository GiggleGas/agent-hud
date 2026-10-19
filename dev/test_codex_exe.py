"""Verify both providers and Desktop read state in the same packaged executable."""
from datetime import datetime, timezone
import json
import hashlib
import os
from pathlib import Path
import re
import subprocess
import time
import tempfile

BASE = Path(__file__).resolve().parents[1]
EXE = BASE / "Agent悬浮窗.exe"
SANDBOX = tempfile.TemporaryDirectory(prefix="agent-hud-codex-exe-")
RESULTS = []


def run_profile(label, finished=False, custom_home=False, desktop_sync=False):
    profile = Path(SANDBOX.name) / label
    profile.mkdir(parents=True, exist_ok=True)
    project = profile / (label + "项目")
    project.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).isoformat()
    codex = profile / ("自定义Codex目录" if custom_home else ".codex")
    sessions = codex / "sessions/2026/10/19"
    sessions.mkdir(parents=True, exist_ok=True)
    sid = "portable-" + label
    events = [
        {"type": "session_meta", "payload": {"id": sid, "cwd": str(project), "source": "vscode",
            "originator": "Codex Desktop", "creator_account_id": "test-account", "creator_user_id": "test-user"}},
        {"timestamp": stamp, "type": "event_msg", "payload": {"type": "task_started", "started_at": time.time()}},
        {"timestamp": stamp, "type": "event_msg", "payload": {"type": "user_message", "message": label + "的任务"}},
    ]
    if finished:
        events.append({"timestamp": stamp, "type": "event_msg", "payload": {"type": "task_complete", "last_agent_message": label + "已完成"}})
    (sessions / "rollout-fixture.jsonl").write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in events), encoding="utf-8")
    child_sid = sid + "-child"
    child_events = [dict(e) for e in events]
    child_events[0] = {"type": "session_meta", "payload": {"id": child_sid, "cwd": str(project),
        "thread_source": "subagent", "agent_path": "/root/review",
        "source": {"subagent": {"thread_spawn": {"parent_thread_id": sid}}}}}
    (sessions / ("rollout-" + child_sid + ".jsonl")).write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in child_events), encoding="utf-8")
    settings = profile / ".claude/agent-hud"
    settings.mkdir(parents=True, exist_ok=True)
    (settings / "config.json").write_text(json.dumps({"rule_asked": True, "unread": []}), encoding="utf-8")
    (codex / "session_index.jsonl").write_text(json.dumps({"id": sid, "thread_name": label + "本机对话"}, ensure_ascii=False) + "\n", encoding="utf-8")
    expected = {"codex:" + sid}
    if not finished:
        claude = profile / ".claude/projects" / re.sub(r"[^A-Za-z0-9]", "-", str(project))
        claude.mkdir(parents=True, exist_ok=True)
        messages = [
            {"timestamp": stamp, "type": "user", "cwd": str(project), "origin": {"kind": "human"}, "message": {"content": label + "的Claude任务"}},
            {"timestamp": stamp, "type": "assistant", "message": {"content": [{"type": "text", "text": "完成"}], "stop_reason": "end_turn"}},
        ]
        (claude / (label + "-claude.jsonl")).write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in messages), encoding="utf-8")
        expected.add(label + "-claude")
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTHON") and k not in ("CODEX_HOME", "CLAUDE_CONFIG_DIR", "AGENT_HUD_TEST_ROOT")}
    env["USERPROFILE"] = str(profile)
    env["AGENT_HUD_TEST_ROOT"] = str(profile)
    env["PATH"] = os.path.join(os.environ["SystemRoot"], "System32")
    if custom_home:
        env["CODEX_HOME"] = str(codex)
    if desktop_sync:
        identity = hashlib.sha256(b'["chatgpt","test-account","test-user"]').hexdigest()
        host = "local:" + hashlib.sha256(b'["local","local",null]').hexdigest()
        read_path = codex / ".codex-global-state.json"
        def native_state(unread):
            read_path.write_text(json.dumps({"electron-thread-read-state-v1": {"version": 1,
                "unreadByIdentity": {identity: {host: unread}}}}), encoding="utf-8")
        native_state([sid])
        proc = subprocess.Popen([str(EXE), "--no", "--exit-after", "8"], env=env,
                                cwd=profile, creationflags=subprocess.CREATE_NO_WINDOW)
        def wait_badge(expected):
            deadline = time.monotonic() + 4
            while time.monotonic() < deadline:
                try:
                    cfg = json.loads((settings / "config.json").read_text(encoding="utf-8"))
                    if any(v["id"] == "codex:" + sid for v in cfg["unread"]) == expected:
                        return
                except (FileNotFoundError, json.JSONDecodeError):
                    pass
                time.sleep(0.1)
            raise AssertionError(("badge did not change", label, expected))
        try:
            wait_badge(True)
            native_state([])
            wait_badge(False)
        finally:
            proc.wait(timeout=12)
        assert json.loads(read_path.read_text(encoding="utf-8"))["electron-thread-read-state-v1"]["unreadByIdentity"][identity][host] == []
    else:
        proc = subprocess.run([str(EXE), "--no", "--exit-after", "5"], env=env,
                              cwd=profile, timeout=20, creationflags=subprocess.CREATE_NO_WINDOW)
    assert proc.returncode == 0, (label, proc.returncode)
    settings = profile / ".claude/agent-hud"
    observed = json.loads((settings / "sessions.json").read_text(encoding="utf-8"))
    assert set(observed) == expected, (label, observed, expected)
    config = json.loads((settings / "config.json").read_text(encoding="utf-8"))
    assert all(v["id"] != "codex:" + child_sid for v in config["unread"]), config["unread"]
    if finished and not desktop_sync:
        assert any(v["id"] == "codex:" + sid and v["state"] == "done" and label in v["title"] for v in config["unread"]), config["unread"]
    log = settings / "hud.log"
    if log.exists():
        assert "Traceback" not in log.read_text(encoding="utf-8")
    RESULTS.append({"profile": label, "custom_codex_home": custom_home, "visible_session_ids": sorted(observed),
                    "child_not_counted_separately": True,
                    "desktop_read_changed_while_running": desktop_sync,
                    "settings_saved_in_target_profile": True, "no_python_on_path": True, "exit_code": proc.returncode})


run_profile("电脑A")
run_profile("电脑B", finished=True, custom_home=True)
run_profile("电脑C已读同步", finished=True, desktop_sync=True)
SANDBOX.cleanup()
print(json.dumps(RESULTS, ensure_ascii=False, indent=2))
print("全部通过")
