"""在沙盒目录里跑真实的 安装.bat / 卸载.bat，检查装了什么、删了什么。不碰真实环境，也不会关掉正在跑的悬浮窗。

python test_install.py            测源目录
python test_install.py <目录>     测别的目录（比如压缩包解压出来的那份）
"""
import ast, glob, os, re, shutil, subprocess, sys, tempfile, winreg

sys.stdout.reconfigure(encoding="utf-8")
SRC = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BOX = os.path.join(tempfile.mkdtemp(prefix="agent-hud-install-"), "box")
fails = []


def check(name, ok, detail=""):
    print(("  ok   " if ok else "  FAIL ") + name + ("" if ok else "  " + str(detail)))
    if not ok:
        fails.append(name)


def run(bat, *args, box=BOX):
    env = dict(os.environ, AGENT_HUD_TEST_ROOT=box, PYTHONIOENCODING="utf-8")
    r = subprocess.run(["cmd", "/c", os.path.join(SRC, bat), *args], env=env, capture_output=True, stdin=subprocess.DEVNULL)
    return r.returncode, r.stdout.decode("utf-8", "replace") + r.stderr.decode("utf-8", "replace")


def autostart():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\AgentHudTest\Run") as k:
            return winreg.QueryValueEx(k, "AgentHUD")[0]
    except OSError:
        return None


def lnk_target(path):
    r = subprocess.run(["powershell", "-NoProfile", "-Command",
                        "[Console]::OutputEncoding=[Text.Encoding]::UTF8;$s=(New-Object -ComObject WScript.Shell).CreateShortcut($env:L);$s.TargetPath+' | '+$s.Arguments"],
                       env=dict(os.environ, L=path), capture_output=True)
    return r.stdout.decode("utf-8", "replace").strip()


# 0) 批处理必须是 CRLF + 纯 ASCII；Python 源码要能按 3.8 的语法解析
for bat in ("安装.bat", "卸载.bat"):
    data = open(os.path.join(SRC, bat), "rb").read()
    check(bat + " 是纯 ASCII", all(b < 128 for b in data))
    check(bat + " 换行是 CRLF", b"\n" in data and data.count(b"\r\n") == data.count(b"\n"))
for py in glob.glob(os.path.join(SRC, "app", "*.py*")):
    try:
        ast.parse(open(py, encoding="utf-8").read(), feature_version=(3, 8))
        check(os.path.basename(py) + " 符合 Python 3.8 语法", True)
    except SyntaxError as ex:
        check(os.path.basename(py) + " 符合 Python 3.8 语法", False, ex)

# 1) 已有 CLAUDE.md 的机器：安装 -> 再装一次 -> 卸载
print("== 已有 CLAUDE.md")
shutil.rmtree(BOX, ignore_errors=True)
claude = os.path.join(BOX, ".claude")
os.makedirs(claude)
mine = "# 我的规则\n\n- 用中文回答".encode("utf-8")   # 故意不带结尾换行
open(os.path.join(claude, "CLAUDE.md"), "wb").write(mine)
code, out = run("安装.bat", "--yes")
print("\n".join("      | " + l for l in out.strip().splitlines()))
check("安装退出码 0", code == 0, code)
target = os.path.join(claude, "agent-hud")
with open(os.path.join(SRC, "app", "install.py"), encoding="utf-8") as f:   # 该装哪些，以 install.py 里的 FILES 和 DIRS 为准
    text = f.read()
    wanted = ast.literal_eval(re.search(r"^FILES = (\(.*?\))$", text, re.M | re.S).group(1)) + \
        ast.literal_eval(re.search(r"^DIRS = (\(.*?\))", text, re.M).group(1))
check("程序文件都装上了，没有多余的", sorted(os.listdir(target)) == sorted(wanted), os.listdir(target))
missing = [n for n in os.listdir(os.path.join(SRC, "app")) if n.endswith((".py", ".pyw")) and n not in wanted]
check("app 目录里每个程序文件都在安装清单里", not missing, missing)
desk = glob.glob(os.path.join(BOX, "Desktop", "*.lnk"))
check("桌面快捷方式用中文名", [os.path.basename(p) for p in desk] == ["Agent 悬浮窗.lnk"], desk)
check("开始菜单快捷方式", len(glob.glob(os.path.join(BOX, "Programs", "*.lnk"))) == 1)
check("默认不开机启动", autostart() is None, autostart())
t = lnk_target(desk[0]) if desk else ""
print("      快捷方式指向：", t)
check("快捷方式指向 pythonw + 装好的脚本", "pythonw.exe" in t.lower() and os.path.join(target, "agent_hud.pyw").lower() in t.lower(), t)
text = open(os.path.join(claude, "CLAUDE.md"), "rb").read()
check("原有内容原样保留在最前面", text.startswith(mine + b"\n\n<!-- agent-hud:begin -->"), text[:80])
check("规则只有一份", text.count(b"agent-hud:begin") == 1 and "进度 2/5 · 这一步在做什么".encode() in text)
code, out = run("安装.bat", "--yes")
check("重复安装不重复加规则", open(os.path.join(claude, "CLAUDE.md"), "rb").read() == text and "没有重复添加" in out, out)
open(os.path.join(target, "config.json"), "w").write('{"x": 5}')
run("安装.bat", "--yes")
check("重装保留窗口位置等设置", os.path.exists(os.path.join(target, "config.json")))

# 开机启动登记在注册表里（测试时写到一个不起作用的位置）；重装默认保持原样
ENV = dict(os.environ, AGENT_HUD_TEST_ROOT=BOX)
subprocess.run([sys.executable, "-c", "import install; install.set_autostart(True)"], cwd=os.path.join(SRC, "app"), env=ENV, check=True)
v = autostart() or ""
check("开机启动登记的是 pythonw + 装好的脚本", "pythonw.exe" in v.lower() and os.path.join(target, "agent_hud.pyw").lower() in v.lower(), v)
run("安装.bat", "--yes")
check("重装后开机启动还开着", autostart() == v, autostart())
subprocess.run([sys.executable, "-c", "import install; install.set_autostart(False)"], cwd=os.path.join(SRC, "app"), env=ENV, check=True)
old = os.path.join(BOX, "Startup", "Agent 悬浮窗.lnk")
open(old, "w").close()
run("安装.bat", "--yes")
check("1.0 版留在启动文件夹里的快捷方式换成了注册表登记", not os.path.exists(old) and autostart() == v, autostart())

code, out = run("卸载.bat", "--yes")
print("\n".join("      | " + l for l in out.strip().splitlines()))
check("卸载后程序目录没了", not os.path.exists(target))
check("卸载后快捷方式没了", not glob.glob(os.path.join(BOX, "*", "*.lnk")))
check("卸载后开机启动没了", autostart() is None, autostart())
left = open(os.path.join(claude, "CLAUDE.md"), "rb").read()
check("卸载后 CLAUDE.md 只剩原有内容", left.strip() == mine and b"agent-hud" not in left, left)

# 2) 没有 CLAUDE.md 的机器：安装会新建，卸载会连文件一起删
print("== 没有 CLAUDE.md")
shutil.rmtree(BOX, ignore_errors=True)
os.makedirs(BOX)
run("安装.bat", "--yes")
rule = os.path.join(BOX, ".claude", "CLAUDE.md")
check("新建了 CLAUDE.md", os.path.exists(rule) and open(rule, "rb").read().startswith(b"<!-- agent-hud:begin -->"))
run("卸载.bat", "--yes")
check("卸载后 CLAUDE.md 也删了", not os.path.exists(rule))

# 3) 装好的那份能不能自己跑起来读数据（不开窗口）
shutil.rmtree(BOX, ignore_errors=True)
os.makedirs(BOX)
run("安装.bat", "--yes")
r = subprocess.run([sys.executable, os.path.join(BOX, ".claude", "agent-hud", "collector.py")], capture_output=True,
                   env=dict(os.environ, PYTHONIOENCODING="utf-8", AGENT_HUD_TEST_ROOT=BOX))
check("装好的 collector.py 能跑", r.returncode == 0 and r.stdout.lstrip().startswith(b"["), r.stderr[-300:])

env = dict(os.environ, AGENT_HUD_TEST_ROOT=BOX, PYTHONIOENCODING="utf-8")
subprocess.run([sys.executable, os.path.join(BOX, ".claude", "agent-hud", "install.py"), "--uninstall", "--yes"], env=env, capture_output=True)
check("解压的文件夹删了，也能用装好的那份 install.py 卸载", not os.path.exists(os.path.join(BOX, ".claude", "agent-hud")))

print("== 没解压就在压缩包里双击")
lone = os.path.join(BOX, "lone")
os.makedirs(lone)
shutil.copy(os.path.join(SRC, "安装.bat"), lone)
r = subprocess.run(["cmd", "/c", os.path.join(lone, "安装.bat")], env=env, capture_output=True, stdin=subprocess.DEVNULL)
check("旁边没有 app 目录时提示先解压", r.returncode != 0 and b"Extract the whole zip" in r.stdout, r.stdout[:200])

# 4) 没装 Python 的机器：要打印中文提示并以非 0 退出
print("== 没有 Python")
env = dict(os.environ, AGENT_HUD_TEST_ROOT=BOX, PATH=r"C:\Windows\System32;C:\Windows\System32\WindowsPowerShell\v1.0", PATHEXT=".COM;.EXE;.BAT;.CMD")
nopy = os.path.join(BOX, "nopy")
shutil.copytree(SRC, nopy, ignore=shutil.ignore_patterns("dev", "*.exe"))
r = subprocess.run(["cmd", "/c", os.path.join(nopy, "安装.bat")], env=env, capture_output=True, stdin=subprocess.DEVNULL)
out = r.stdout.decode("utf-8", "replace") + r.stdout.decode("gbk", "replace")
print("      退出码", r.returncode, "| 输出前两行:", [l for l in r.stdout.decode("utf-8", "replace").splitlines() if l.strip()][:2])
check("找不到 Python 时退出码非 0", r.returncode != 0, r.returncode)
check("打印了中文提示", "没找到可用的 Python" in out, out[:200])

shutil.rmtree(os.path.dirname(BOX), ignore_errors=True)
for key in (r"Software\AgentHudTest\Run", r"Software\AgentHudTest"):
    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key)
    except OSError:
        pass
print("\n失败 %d 项：%s" % (len(fails), fails) if fails else "\n全部通过")
sys.exit(1 if fails else 0)
