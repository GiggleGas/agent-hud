"""用真的 claude 跑一个最小的后台任务再追问一句，确认真实环境下整条链路是通的。会花一点点额度（两句极短的问答）。

任务在一个临时目录里跑、关掉了 hook（不弹完成通知），跑完把它留下的会话记录删掉；悬浮窗自己的文件写在沙盒里。
"""
import glob, os, re, shutil, sys, tempfile, time

sys.stdout.reconfigure(encoding="utf-8")
DEV = os.path.dirname(os.path.abspath(__file__))
BOX = tempfile.mkdtemp(prefix="agent-hud-smoke-")
os.environ["AGENT_HUD_TEST_ROOT"] = BOX
os.environ.pop("AGENT_HUD_CLAUDE", None)
sys.path.insert(0, os.path.join(os.path.dirname(DEV), "app"))
import collector, runner, workspaces

ws = os.path.join(BOX, "hud-smoke")
os.makedirs(ws)
reg = workspaces.Registry()
reg.ensure(ws)
problems = []
r = runner.Runner("auto", lambda sid, text: problems.append(text))
r.extra = ["--settings", '{"disableAllHooks":true}']
col = collector.Collector(reg)
print("claude:", r.claude())


def wait(sid, want, seconds=120):
    end, v, seen = time.time() + seconds, None, []
    while time.time() < end:
        r.tick()
        col.owned, col.running, col.exited = r.owned(), r.active(), dict(r.exited)
        col.next_scan = 0
        v = next((x for x in col.poll() if x["id"] == sid), None)
        if v and (v["state"], v["action"]) not in seen:
            seen.append((v["state"], v["action"]))
        if problems or (v and want(v)):
            break
        time.sleep(1)
    return v, seen


ok = False
try:
    t = time.time()
    sid = r.start(ws, "这是一次连通性测试。只回复两个字：收到")
    v, seen = wait(sid, lambda v: v["state"] == "done")
    print("第一句：%.0f 秒，状态经过 %s" % (time.time() - t, seen))
    print("   标题=%r 项目=%r 后台=%r 回复=%r" % (v and v["title"], v and v["ws"], v and v["owned"], v and v["last_text"]))
    first = bool(v and v["state"] == "done" and v["owned"] and v["ws"] == "hud-smoke" and "收到" in v["last_text"])
    t = time.time()
    print("追问：", r.follow(sid, "再回复两个字：好的"))
    v, seen = wait(sid, lambda v: v["state"] == "done" and "好的" in v["last_text"])
    print("第二句：%.0f 秒，回复=%r" % (time.time() - t, v and v["last_text"]))
    ok = first and bool(v and "好的" in v["last_text"]) and not problems
    print("问题：", problems or "无")
finally:
    for s in list(r.procs):
        r.stop(s)
    # 真的 claude 把记录写在真实的 ~/.claude/projects 下，按这个临时目录的名字找到并删掉
    for d in glob.glob(os.path.join(collector.PROJECTS, "*" + re.sub(r"[^A-Za-z0-9]", "-", os.path.basename(BOX)) + "*")):
        shutil.rmtree(d, ignore_errors=True)
        print("已删除测试会话记录：", os.path.basename(d))
    shutil.rmtree(BOX, ignore_errors=True)
print("\n通过" if ok else "\n没通过")
sys.exit(0 if ok else 1)
