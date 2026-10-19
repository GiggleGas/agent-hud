"""Agent 悬浮窗的安装 / 卸载，以及悬浮窗右键菜单里用到的那几个开关（进度规则、开机启动）。

脚本版由「安装.bat」「卸载.bat」调用，也可以直接跑：

python install.py              安装（会问两个问题）
python install.py --yes        安装，不提问，全部用默认答案（加进度规则、开机启动保持原样）
python install.py --uninstall  卸载

环境变量 AGENT_HUD_TEST_ROOT 指向一个空目录时，所有东西都装进那个目录，开机启动写到一个
不起作用的注册表位置，不碰真实环境、不关也不启动悬浮窗（测试用）。
"""
import ctypes
import os
import shutil
import subprocess
import sys

try:
    import winreg
except ImportError:   # 不是 Windows；下面 __main__ 里会给出提示
    winreg = None

HERE = os.path.dirname(os.path.abspath(__file__))
FILES = ("agent_hud.pyw", "collector.py", "hub.py", "workspaces.py", "runner.py", "dialogs.py", "feedback.py", "feedback_to.py",
         "tray.py", "usage.py", "notify.py", "updates.py", "web.py", "install.py")
OLD_FILES = ("feishu.py", "feishu.json")   # 以前的版本装过、现在不用了的，升级时删掉
DIRS = ("segno",)   # 整个目录一起复制的：二维码库（纯 Python，原样放进来的，许可证在它的 LICENSE 里）
NAMES = ("Agent 悬浮窗", "Agent HUD")   # 快捷方式的名字；中文名建不出来时退回英文名
TEST_ROOT = os.environ.get("AGENT_HUD_TEST_ROOT")
FROZEN = getattr(sys, "frozen", False)   # 打包成 exe 运行
YES = "--yes" in sys.argv

HOME = os.path.expanduser("~")
CLAUDE = os.path.join(TEST_ROOT, ".claude") if TEST_ROOT else os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(HOME, ".claude")
TARGET = os.path.join(CLAUDE, "agent-hud")   # 脚本版的程序装在这；窗口设置和日志也放这（exe 版也是）
RULE_FILE = os.path.join(CLAUDE, "CLAUDE.md")
RUN_KEY = r"Software\AgentHudTest\Run" if TEST_ROOT else r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_NAME = "AgentHUD"

# 写进 CLAUDE.md 的那一段。首尾两行注释是给卸载认的，改正文不要动它们；
# 「进度 2/5 · …」这个格式要和 collector.py 里的 RE_MARK 对得上。
BEGIN, END = "<!-- agent-hud:begin -->", "<!-- agent-hud:end -->"
RULE = BEGIN + """
# 进度标记

做要分好几步的活时，在对话里单独用一行报现在到第几步，格式固定：

进度 2/5 · 这一步在做什么

- 开工时先估一共几步，报「进度 1/N · …」；之后每进入新的一步报一次，不用每次调用工具都报。
- 中途发现步数估错了，直接按新的总数接着报。
- 一两步就能做完的小事不用报。

这一行是给 Agent 悬浮窗（`~/.claude/agent-hud`）读的，它靠这个画进度条；格式变了它就认不出来。
""" + END + "\n"


# 从编辑器里启动（比如在 Claude Code 的对话里跑命令）时会继承编辑器塞进来的环境变量，其中有几类会坏事：
# ELECTRON_RUN_AS_NODE 让 Cursor / VS Code 被当成纯 Node 跑，跳转打不开；CLAUDE_CODE_SESSION_ID 之类让后台任务
# 以为自己是别的对话的子会话。正常从桌面、开始菜单、bat 启动不会有这些。
INHERITED = ("ELECTRON_", "VSCODE_", "CURSOR_", "CLAUDE_CODE_", "CLAUDECODE", "CLAUDE_PID", "CLAUDE_EFFORT",
             "CLAUDE_AGENT_SDK_", "MCP_CONNECTION_")


def clean_env():
    """去掉从编辑器继承来的那几类环境变量；用户自己在系统里设了的同名变量保留。之后起的子进程都用干净的环境。"""
    keep = set()
    for root, path in ((winreg.HKEY_CURRENT_USER, "Environment"),
                       (winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment")):
        try:
            with winreg.OpenKey(root, path) as key:
                for i in range(winreg.QueryInfoKey(key)[1]):
                    keep.add(winreg.EnumValue(key, i)[0].upper())
        except OSError:
            pass
    for name in list(os.environ):
        if name.upper().startswith(INHERITED) and name.upper() not in keep:
            del os.environ[name]


def ps(script, **env):
    """跑一段 PowerShell，返回它的输出；参数走环境变量，省得处理引号和中文。"""
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command", "[Console]::OutputEncoding=[Text.Encoding]::UTF8;" + script],
            capture_output=True, env=dict(os.environ, **env), timeout=60, creationflags=subprocess.CREATE_NO_WINDOW)
        return r.stdout.decode("utf-8", "replace").strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def folder(name):
    """Desktop / Programs（开始菜单）/ Startup（启动文件夹）的真实位置，桌面被挪到 OneDrive 也能找对。"""
    if TEST_ROOT:
        path = os.path.join(TEST_ROOT, name)
        os.makedirs(path, exist_ok=True)
        return path
    buf = ctypes.create_unicode_buffer(520)
    ctypes.windll.shell32.SHGetFolderPathW(None, {"Desktop": 0x10, "Programs": 0x02, "Startup": 0x07}[name], None, 0, buf)
    return buf.value


def pythonw():
    path = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    return path if os.path.exists(path) else sys.executable


def launch_command():
    """启动悬浮窗要执行的（程序, 参数）：exe 版就是它自己，脚本版是 pythonw 加装好的脚本。"""
    if FROZEN:
        return sys.executable, ""
    return pythonw(), '"%s"' % os.path.join(TARGET, "agent_hud.pyw")


def remove_shortcut(where):
    base = folder(where)
    for name in NAMES:
        try:
            os.remove(os.path.join(base, name + ".lnk"))
        except OSError:
            pass


def make_shortcut(where):
    base = folder(where)
    if not base:
        return False
    remove_shortcut(where)
    exe, args = launch_command()
    for name in NAMES:
        link = os.path.join(base, name + ".lnk")
        ps("$s=(New-Object -ComObject WScript.Shell).CreateShortcut($env:HUD_LINK);"
           "$s.TargetPath=$env:HUD_EXE;$s.Arguments=$env:HUD_ARGS;$s.WorkingDirectory=$env:HUD_DIR;"
           "$s.Description='Agent HUD';$s.Save()",
           HUD_LINK=link, HUD_EXE=exe, HUD_ARGS=args, HUD_DIR=os.path.dirname(exe))
        if os.path.exists(link):
            return True
    return False


def autostart():
    """开机启动现在登记的命令；没登记返回 None。"""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            return winreg.QueryValueEx(key, RUN_NAME)[0]
    except OSError:
        return None


def set_autostart(on):
    if on:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.SetValueEx(key, RUN_NAME, 0, winreg.REG_SZ, ('"%s" %s' % launch_command()).strip())
    else:
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
                winreg.DeleteValue(key, RUN_NAME)
        except OSError:
            pass
    remove_shortcut("Startup")   # 1.0 版的开机启动是启动文件夹里的快捷方式，顺手清掉


def stop_running():
    """关掉在跑的悬浮窗，等它真的退干净再返回：马上开新的话，新的会以为已经有一个在跑而自己退出。"""
    if not TEST_ROOT:
        ps("Get-CimInstance Win32_Process | Where-Object { $_.Name -like 'python*' -and $_.CommandLine -like '*agent_hud.pyw*' }"
           " | ForEach-Object { Stop-Process -Id $_.ProcessId -Force; Wait-Process -Id $_.ProcessId -Timeout 15 -ErrorAction SilentlyContinue }")


def start():
    if not TEST_ROOT:
        subprocess.Popen([pythonw(), os.path.join(TARGET, "agent_hud.pyw")], cwd=TARGET, close_fds=True,
                         creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP)


def has_rule():
    try:
        with open(RULE_FILE, "rb") as f:
            return b"agent-hud" in f.read()
    except OSError:
        return False


def add_rule():
    os.makedirs(CLAUDE, exist_ok=True)
    with open(RULE_FILE, "ab") as f:   # 只在末尾追加，不动文件里原有的内容
        f.write((("\n\n" if f.tell() else "") + RULE).encode("utf-8"))


def remove_rule():
    """把 add_rule 追加的那一段删掉；删完文件空了就连文件一起删。返回有没有删到东西。"""
    try:
        with open(RULE_FILE, "rb") as f:
            data = f.read()
    except OSError:
        return False
    a, b = data.find(BEGIN.encode()), data.find(END.encode())
    if a < 0 or b < a:
        return False
    rest = data[:a].rstrip() + b"\n" + data[b + len(END):].lstrip()
    if rest.strip(b"\xef\xbb\xbf \r\n\t"):
        with open(RULE_FILE, "wb") as f:
            f.write(rest)
    else:
        os.remove(RULE_FILE)
    return True


def cleanup():
    """删掉悬浮窗在这台机器上留下的所有东西（正在运行的程序文件本身除外）。返回有没有删到进度规则。"""
    set_autostart(False)
    for where in ("Desktop", "Programs"):
        remove_shortcut(where)
    removed = remove_rule()
    os.chdir(HOME)
    shutil.rmtree(TARGET, ignore_errors=True)
    return removed


def ask(question, default):
    if YES or not sys.stdin or not sys.stdin.isatty():
        return default
    answer = input("%s %s " % (question, "[Y/n]" if default else "[y/N]")).strip().lower()
    return default if not answer else answer in ("y", "yes")


def finish():
    if not YES and sys.stdin and sys.stdin.isatty():
        input("\n按回车关闭这个窗口…")


def install():
    print("\n===== 安装 Agent 悬浮窗 =====\n")
    stop_running()
    os.makedirs(TARGET, exist_ok=True)
    if os.path.normcase(HERE) != os.path.normcase(TARGET):
        for name in FILES:
            shutil.copy2(os.path.join(HERE, name), os.path.join(TARGET, name))
        for name in DIRS:
            shutil.rmtree(os.path.join(TARGET, name), ignore_errors=True)
            shutil.copytree(os.path.join(HERE, name), os.path.join(TARGET, name), ignore=shutil.ignore_patterns("__pycache__"))
    for name in OLD_FILES:
        try:
            os.remove(os.path.join(TARGET, name))
        except OSError:
            pass
    print("[1/4] 程序装到了 " + TARGET)

    made = [label for where, label in (("Desktop", "桌面"), ("Programs", "开始菜单")) if make_shortcut(where)]
    if made:
        print("[2/4] 快捷方式建在：" + "、".join(made))
    else:
        print("[2/4] 快捷方式没建成（PowerShell 可能被禁用了）。以后这样启动：")
        print("      " + ('"%s" %s' % launch_command()))

    if has_rule():
        print("[3/4] 进度规则已经在 " + RULE_FILE + " 里了，没有重复添加")
    else:
        print("[3/4] 进度条要显示「第几步 / 共几步」，得让 Claude 干多步的活时在对话里报一行")
        print("      「进度 2/5 · 这一步在做什么」。做法是在 " + RULE_FILE)
        print("      末尾追加一小段规则，对你所有项目里新开的对话生效。不加的话悬浮窗照常能用，")
        print("      只是进度条只有一个来回滑的块，没有步数。")
        if ask("      要加这条规则吗？", True):
            add_rule()
            print("      已添加。")
        else:
            print("      没有添加。以后想加，在悬浮窗的右键菜单里勾上「显示步数」。")

    # 默认保持原样；1.0 版的开机启动是启动文件夹里的快捷方式，也算开着
    was_on = autostart() is not None or any(os.path.exists(os.path.join(folder("Startup"), n + ".lnk")) for n in NAMES)
    on = ask("[4/4] 要开机自动启动吗？", was_on)
    set_autostart(on)
    print("      " + ("已设置开机自动启动。" if on else "不开机启动，需要时双击快捷方式「%s」。" % NAMES[0]))

    start()
    print("\n装好了，悬浮窗已经出现在屏幕右上角。怎么用见「使用说明.md」。")
    finish()


def uninstall():
    print("\n===== 卸载 Agent 悬浮窗 =====\n")
    if not ask("确定卸载吗？", True):
        return
    stop_running()
    if cleanup():
        print("已从 " + RULE_FILE + " 里删掉进度规则那一段。")
    print("已删除快捷方式、开机启动和 " + TARGET)
    print("\n卸载完成。")
    finish()


if __name__ == "__main__":
    if sys.platform != "win32":
        sys.exit("Agent 悬浮窗只支持 Windows。")
    uninstall() if "--uninstall" in sys.argv else install()
