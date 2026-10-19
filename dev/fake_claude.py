"""假装成 `claude -p` 的小程序，测后台任务用（不花额度）。照真的那样往会话记录里写：
用户的话 -> 一次读文件 -> 一句回复。任务内容里带 FAIL 就不写记录直接报错退出，带 SLOW 就磨蹭 60 秒。"""
import json, os, re, sys, time
from datetime import datetime, timezone

args = sys.argv[1:]
resume = "--resume" in args
sid = args[args.index("--resume" if resume else "--session-id") + 1]
mode = args[args.index("--permission-mode") + 1]
prompt = sys.stdin.buffer.read().decode("utf-8")
if "FAIL" in prompt:
    print(json.dumps({"is_error": True, "result": "boom: 假装登录过期了"}))
    sys.exit(3)

cwd = os.getcwd()
folder = os.path.join(os.environ["CLAUDE_CONFIG_DIR"], "projects", re.sub(r"[^A-Za-z0-9]", "-", cwd))
os.makedirs(folder, exist_ok=True)
path = os.path.join(folder, sid + ".jsonl")
if resume and not os.path.exists(path):
    sys.exit("No conversation found with session ID " + sid)


def write(entry):
    entry.update(timestamp=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"), cwd=cwd, sessionId=sid)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n")


time.sleep(1.5)   # 真的 claude 也要过一两秒才写出第一行
write({"type": "user", "permissionMode": mode, "origin": {"kind": "human"}, "message": {"role": "user", "content": prompt}})
write({"type": "assistant", "message": {"role": "assistant", "stop_reason": "tool_use", "content": [
    {"type": "tool_use", "id": "t1", "name": "Read", "input": {"file_path": os.path.join(cwd, "main.gd")}}]}})
time.sleep(60 if "SLOW" in prompt else 1.5)
write({"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]}})
write({"type": "assistant", "message": {"role": "assistant", "stop_reason": "end_turn", "content": [
    {"type": "text", "text": "做完了：%s（权限模式 %s）" % (prompt, mode)}]}})
write({"type": "ai-title", "aiTitle": "假任务 " + prompt[:10]})
print(json.dumps({"is_error": False, "result": "ok", "session_id": sid}))
