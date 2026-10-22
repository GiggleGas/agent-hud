"""实测打好的 exe。全部在沙盒里跑：规则写进沙盒里的 CLAUDE.md，开机启动写到一个不起作用的注册表位置，
不碰真实环境，也不影响正在跑的悬浮窗。每个测试窗口几秒后自己退出。截图要装 Pillow。
"""
import ctypes, glob, json, os, shutil, subprocess, sys, time, winreg

sys.stdout.reconfigure(encoding="utf-8")
DEV = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(DEV)
BOX = os.path.join(DEV, ".exe-test")   # 不放系统临时目录：exe 在临时目录里时不会登记开机启动
TEST_KEY = r"Software\AgentHudTest"
fails = []


def check(name, ok, detail=""):
    print(("  ok   " if ok else "  FAIL ") + name + ("" if ok else "  " + str(detail)))
    if not ok:
        fails.append(name)


def place(sub):
    """把 exe 连同运行库文件夹复制到沙盒里一个带中文和空格的目录，返回 exe 的路径。"""
    d = os.path.join(BOX, sub)
    os.makedirs(d, exist_ok=True)
    shutil.copytree(os.path.join(ROOT, "_internal"), os.path.join(d, "_internal"))
    return shutil.copy2(os.path.join(ROOT, "Agent悬浮窗.exe"), d)


def run(exe, root, *args, seconds=4):
    env = dict(os.environ, AGENT_HUD_TEST_ROOT=os.path.join(BOX, root))
    t = time.time()
    code = subprocess.run([exe, *args, "--exit-after", str(seconds)], env=env, timeout=90).returncode
    return code, time.time() - t


def autostart():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, TEST_KEY + r"\Run") as k:
            return winreg.QueryValueEx(k, "AgentHUD")[0]
    except OSError:
        return None


def drop_test_key():
    for key in (TEST_KEY + r"\Run", TEST_KEY):
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key)
        except OSError:
            pass


shutil.rmtree(BOX, ignore_errors=True)
drop_test_key()
try:
    exe = place("放 工具 的目录")

    print("== 假数据模式能起来、能画")
    env = dict(os.environ, AGENT_HUD_TEST_ROOT=os.path.join(BOX, "demo"))
    t = time.time()
    p = subprocess.Popen([exe, "--demo", "--exit-after", "9"], env=env)
    time.sleep(6)
    try:
        from PIL import ImageGrab
        sw = ctypes.windll.user32.GetSystemMetrics(0)
        shot = os.path.join(DEV, ".exe-test-shot.png")
        ImageGrab.grab(bbox=(sw - 464, 90, sw - 24, 690), include_layered_windows=True).save(shot)
        print("      截图:", shot)
    except ImportError:
        print("      没装 Pillow，跳过截图")
    check("自己退出，退出码 0", p.wait(timeout=60) == 0)
    check("假数据模式不写任何东西", not os.path.exists(os.path.join(BOX, "demo")))

    print("== 第一次运行：问一次要不要加进度规则")
    code, took = run(exe, "yes", "--yes")
    print("      从启动到退出 %.1f 秒（其中 4 秒是测试等待）" % took)
    rule = os.path.join(BOX, "yes", ".claude", "CLAUDE.md")
    cfg = os.path.join(BOX, "yes", ".claude", "agent-hud", "config.json")
    check("选「是」：规则写进了 CLAUDE.md", os.path.exists(rule) and b"agent-hud:begin" in open(rule, "rb").read())
    check("记下问过了", os.path.exists(cfg) and json.load(open(cfg)).get("rule_asked") is True)
    hud_log = os.path.join(os.path.dirname(cfg), "hud.log")
    check("读真实会话记录时没有报错", code == 0 and not os.path.exists(hud_log),
          (code, open(hud_log, encoding="utf-8", errors="replace").read()[-1500:] if os.path.exists(hud_log) else ""))
    os.remove(rule)
    run(exe, "yes", "--yes")
    check("第二次运行不再问、不再加", not os.path.exists(rule))
    run(exe, "no", "--no")
    check("选「否」：不建 CLAUDE.md", not os.path.exists(os.path.join(BOX, "no", ".claude", "CLAUDE.md")))
    check("选「否」也记下问过了", json.load(open(os.path.join(BOX, "no", ".claude", "agent-hud", "config.json"))).get("rule_asked") is True)

    print("== 反馈能从 exe 里发出去")
    # 发到一个不存在的飞书机器人：走真的 HTTPS 连上飞书、被拒绝，证明 exe 里的网络和证书没问题，又不会真往群里发东西
    fake = "https://open.feishu.cn/open-apis/bot/v2/hook/00000000-0000-0000-0000-000000000000"
    env = dict(os.environ, AGENT_HUD_TEST_ROOT=os.path.join(BOX, "fb"), AGENT_HUD_FEEDBACK_URL=fake)
    subprocess.run([exe, "--no", "--send-feedback", "exe 测试", "--exit-after", "10"], env=env, timeout=90)
    fb_dir = os.path.join(BOX, "fb", ".claude", "agent-hud")
    log = os.path.join(fb_dir, "hud.log")
    text = open(log, encoding="utf-8").read() if os.path.exists(log) else ""
    print("      " + (text.strip().splitlines() or ["（没有日志）"])[-1][:150])
    check("连上了飞书、被拒绝（19001：地址不对）", "测试反馈：False" in text and "19001" in text, text[-300:])
    kept = glob.glob(os.path.join(fb_dir, "feedback", "*.txt"))
    check("没发出去的存在本机，内容开头是【游戏反馈】【Agent悬浮窗】", len(kept) == 1 and
          open(kept[0], encoding="utf-8").read().startswith("【游戏反馈】【Agent悬浮窗】"), kept)

    print("== 手机访问在 exe 里能用")
    # 测试时网页服务只开在本机地址（见 web.HOST），不会弹防火墙对话框；顺带打开「手机访问」窗口，画一次二维码
    import http.cookiejar, urllib.request
    web_dir = os.path.join(BOX, "web", ".claude", "agent-hud")
    os.makedirs(web_dir)
    with open(os.path.join(web_dir, "config.json"), "w") as f:
        json.dump({"rule_asked": True, "web_on": True, "web_key": "abcd1234", "web_port": 18799}, f)
    p = subprocess.Popen([exe, "--no", "--web", "--exit-after", "12"], env=dict(os.environ, AGENT_HUD_TEST_ROOT=os.path.join(BOX, "web")))
    phone = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    page = state = None
    for _ in range(40):
        time.sleep(0.25)
        try:
            page = phone.open("http://127.0.0.1:18799/?k=abcd1234", timeout=3).read().decode("utf-8")
            state = json.loads(phone.open("http://127.0.0.1:18799/api/state", timeout=3).read())
            break
        except OSError:
            continue
    check("带钥匙能打开页面、拿到数据", bool(page) and "<html" in page.lower() and isinstance(state, dict) and "live" in state, (page or "")[:200])
    check("自己退出，退出码 0", p.wait(timeout=60) == 0)
    log = os.path.join(web_dir, "hud.log")
    text = open(log, encoding="utf-8").read() if os.path.exists(log) else ""
    check("「手机访问」窗口（二维码）画出来没报错", "Traceback" not in text, text[-500:])

    print("== 一键更新（exe 自己下载新包、退出、换文件、再打开）")
    # 假飞书：发版表里有一版 9.9，安装包就是这个 exe 加一个记号文件，包里的版本号写成 9.9
    import io, threading, zipfile, http.server
    from urllib.parse import urlparse
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.write(exe, "agent-hud/Agent悬浮窗.exe")
        for base, _, files in os.walk(os.path.join(os.path.dirname(exe), "_internal")):
            for name in files:
                full = os.path.join(base, name)
                z.write(full, "agent-hud/" + os.path.relpath(full, os.path.dirname(exe)).replace(os.sep, "/"))
        z.writestr("agent-hud/_internal/update-marker.txt", "9.9")
        z.writestr("agent-hud/app/agent_hud.pyw", 'VERSION = "9.9"\n')
    package = buf.getvalue()

    class Lark(http.server.BaseHTTPRequestHandler):
        def reply(self, data, kind="application/json"):
            self.send_response(200)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            self.reply(json.dumps({"code": 0, "tenant_access_token": "t", "expire": 7200}).encode())

        def do_GET(self):
            if "/medias/" in self.path:
                self.reply(package, "application/zip")
                return
            fields = {"版本": "9.9", "说明": "测试", "安装包": [{"file_token": "boxT", "name": "agent-hud.zip", "size": len(package),
                                                         "url": "https://open.feishu.cn/open-apis/drive/v1/medias/boxT/download"}]}
            self.reply(json.dumps({"code": 0, "data": {"items": [{"fields": fields}]}}).encode())

        def log_message(self, *a):
            pass

    lark = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Lark)
    threading.Thread(target=lark.serve_forever, daemon=True).start()
    upd = place("自动 更新")
    env = dict(os.environ, AGENT_HUD_TEST_ROOT=os.path.join(BOX, "upd"), AGENT_HUD_FEISHU_BASE="http://127.0.0.1:%d/open-apis" % lark.server_port,
               AGENT_HUD_UPDATE_TABLE="https://x.feishu.cn/base/BasT?table=tblT")
    hud_dir = os.path.join(BOX, "upd", ".claude", "agent-hud")
    os.makedirs(hud_dir)
    with open(os.path.join(hud_dir, "config.json"), "w") as f:
        json.dump({"rule_asked": True}, f)
    p = subprocess.Popen([upd, "--no", "--update-now"], env=env)
    code = p.wait(timeout=60)
    marker = os.path.join(os.path.dirname(upd), "_internal", "update-marker.txt")
    for _ in range(100):   # 更新脚本等 exe 退干净、换文件、再把它打开
        if os.path.exists(marker):
            break
        time.sleep(0.3)
    time.sleep(3)
    log_path = os.path.join(hud_dir, "update", "update.log")
    up_log = open(log_path, encoding="utf-8-sig").read() if os.path.exists(log_path) else ""
    check("exe 下载好新包、交给更新脚本后自己退出", code == 0)
    check("更新脚本把 _internal 换成了新包里的（有记号文件）", os.path.exists(marker), up_log)
    check("换完把 exe 重新打开了", "copy ok" in up_log and "started" in up_log, up_log)
    cfg = json.load(open(os.path.join(hud_dir, "config.json"), encoding="utf-8"))
    check("记下了正在更新到 9.9（新版本启动时会核对）", cfg.get("update_pending") in ("9.9", ""), cfg.get("update_pending"))
    subprocess.run(["powershell", "-NoProfile", "-Command",
                    "Get-CimInstance Win32_Process | Where-Object { $_.ExecutablePath -eq '%s' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"
                    % upd.replace("'", "''")], capture_output=True)   # 关掉更新后重新打开的那个
    lark.shutdown()

    print("== 开机启动")
    check("默认没有登记", autostart() is None, autostart())
    run(exe, "no", "--autostart", "on", seconds=3)
    check("开启后登记的就是这个 exe", autostart() == '"%s"' % exe, autostart())
    moved = place("换了个 位置")
    run(moved, "no", seconds=3)
    check("exe 换位置后再运行，登记自动跟过去", autostart() == '"%s"' % moved, autostart())
    run(moved, "no", "--autostart", "off", seconds=3)
    check("关闭后登记没了", autostart() is None, autostart())
finally:
    drop_test_key()
    shutil.rmtree(BOX, ignore_errors=True)

print("\n失败 %d 项：%s" % (len(fails), fails) if fails else "\n全部通过")
sys.exit(1 if fails else 0)
