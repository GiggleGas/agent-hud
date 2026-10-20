"""后台任务：悬浮窗或手机发起的、不开界面在后台跑的 Claude Code 对话，以及「带着一句话跳到编辑器里的对话」。

每个任务是一次 `claude -p`：发起时就定好会话 id，之后的进展和别的对话一样从会话记录里读，
这里只管起、追问、停，并记住哪些会话是自己发起的（tasks.json）。

只对自己发起的任务追问。编辑器里开着的对话不能从这里发消息：两个程序同时写一个对话，消息会搅在一起。
"""
import ctypes
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from ctypes import wintypes
from urllib.parse import quote

import collector
import install

FILE =os.path.join(install.TARGET, "tasks.json")
LOGS = os.path.join(install.TARGET, "tasks")   # 每个任务一份输出，任务没起来时从这里看原因
KEEP = 200                                     # tasks.json 里最多记多少个任务
EXTENSIONS = ("~/.cursor/extensions", "~/.vscode/extensions")
NO_WINDOW = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP


def find_claude():
    """找一个能用的 claude 程序，返回（启动它的命令, 版本）；有好几个就用版本最新的。找不到返回 None。
    候选：命令行装的那个，和编辑器扩展自带的那个（只装了扩展的人没有前者）。"""
    fake = os.environ.get("AGENT_HUD_CLAUDE")   # 测试用：一个假装成 claude 的 Python 脚本
    if fake:
        return [sys.executable, fake], "test"
    found = []
    for root in EXTENSIONS:
        for exe in glob.glob(os.path.join(os.path.expanduser(root), "anthropic.claude-code-*", "resources", "native-binary", "claude.exe")):
            m = re.search(r"claude-code-(\d+)\.(\d+)\.(\d+)", exe)
            if m:
                found.append((tuple(int(x) for x in m.groups()), exe))
    cli = shutil.which("claude")
    if cli:
        try:
            out = subprocess.run([cli, "--version"], capture_output=True, timeout=20, creationflags=NO_WINDOW).stdout.decode("utf-8", "replace")
            m = re.search(r"(\d+)\.(\d+)\.(\d+)", out)
            found.append((tuple(int(x) for x in m.groups()) if m else (0, 0, 0), cli))
        except (OSError, subprocess.SubprocessError):
            pass
    if not found:
        return None
    version, exe = max(found)
    return [exe], ".".join(str(x) for x in version)


def alive(pid, name):
    """这个进程号是不是还活着、而且确实是我们起的那个程序（进程号会被系统重新分给别的程序）。"""
    k = ctypes.WinDLL("kernel32")
    k.OpenProcess.restype = wintypes.HANDLE
    h = k.OpenProcess(0x1000, False, int(pid))   # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return False
    try:
        code = wintypes.DWORD()
        if not k.GetExitCodeProcess(wintypes.HANDLE(h), ctypes.byref(code)) or code.value != 259:   # STILL_ACTIVE
            return False
        buf, n = ctypes.create_unicode_buffer(520), wintypes.DWORD(520)
        k.QueryFullProcessImageNameW(wintypes.HANDLE(h), 0, buf, ctypes.byref(n))
        return os.path.basename(buf.value).lower() == name.lower()
    finally:
        k.CloseHandle(wintypes.HANDLE(h))


EDITORS = {"cursor.exe": "cursor", "code.exe": "vscode", "code - insiders.exe": "vscode-insiders"}   # 编辑器的程序名 -> 它的链接协议
# 编辑器主窗口的标题以编辑器名结尾；同一个程序还有输入法候选框（CandidateWindow）之类的窗口，不算
TITLE_ENDS = {"cursor": "Cursor", "vscode": "Visual Studio Code", "vscode-insiders": "Visual Studio Code - Insiders"}
CLIS = {   # 编辑器的命令行（链接协议 -> 可能装在哪），项目没开着窗口时用它打开；「装给所有用户」的装在 Program Files 下
    "cursor": (r"%LOCALAPPDATA%\Programs\cursor\resources\app\bin\cursor.cmd", r"%ProgramFiles%\cursor\resources\app\bin\cursor.cmd"),
    "vscode": (r"%LOCALAPPDATA%\Programs\Microsoft VS Code\bin\code.cmd", r"%ProgramFiles%\Microsoft VS Code\bin\code.cmd"),
    "vscode-insiders": (r"%LOCALAPPDATA%\Programs\Microsoft VS Code Insiders\bin\code-insiders.cmd",
                        r"%ProgramFiles%\Microsoft VS Code Insiders\bin\code-insiders.cmd"),
}


EXT_DIRS = {".cursor": "cursor", ".vscode": "vscode", ".vscode-insiders": "vscode-insiders"}   # 扩展装在哪 -> 哪个编辑器


def editor_cli(prefer=None):
    """用哪个编辑器打开项目：prefer（对话所在的那个编辑器）优先；其次是现在开着窗口的那个编辑器
    （Cursor 和 VS Code 都装了的人，用他正在用的）；一个窗口都没开就按 Cursor、VS Code 的顺序找装了的。"""
    open_now = [s for _, _, s in editor_windows()]
    for scheme in ([prefer] if prefer else []) + open_now + list(CLIS):
        for p in map(os.path.expandvars, CLIS.get(scheme, ())):
            if os.path.exists(p):
                return p
    return None


def editor_windows():
    """开着的 Cursor / VS Code 窗口：[(窗口句柄, 标题, 链接协议)]，最前面的在前。"""
    u, k = ctypes.WinDLL("user32"), ctypes.WinDLL("kernel32")
    k.OpenProcess.restype = wintypes.HANDLE
    found = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def each(hwnd, _):
        title = ctypes.create_unicode_buffer(400)
        if not u.IsWindowVisible(hwnd) or not u.GetWindowTextW(hwnd, title, 400):
            return True
        pid = wintypes.DWORD()
        u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        h = k.OpenProcess(0x1000, False, pid.value)
        if h:
            buf, n = ctypes.create_unicode_buffer(520), wintypes.DWORD(520)
            if k.QueryFullProcessImageNameW(wintypes.HANDLE(h), 0, buf, ctypes.byref(n)):
                scheme = EDITORS.get(os.path.basename(buf.value).lower())
                if scheme and title.value.endswith(TITLE_ENDS[scheme]):
                    found.append((hwnd, title.value, scheme))
            k.CloseHandle(wintypes.HANDLE(h))
        return True

    u.EnumWindows(each, 0)
    return found


def tab_of(title, scheme):
    """编辑器窗口标题「当前标签 - 文件夹名 - 编辑器名」拆成 (当前标签, 文件夹名)。Claude 对话的标签名就是对话标题。
    标签名里可以有「 - 」，所以从右边拆；只有两段（没开文件夹 / 没开标签）分不清是哪个，返回 None。"""
    end = " - " + TITLE_ENDS[scheme]
    if not title.endswith(end):
        return None
    parts = title[:-len(end)].lstrip("● ").rsplit(" - ", 1)
    return (parts[0].strip(), parts[1].strip()) if len(parts) == 2 else None


def focused_tab():
    """最前面的窗口如果是编辑器，返回它的 (当前标签, 文件夹名)；不是编辑器（或者看不出来）返回 None。
    只看最前面这一个窗口，悬浮窗每 0.5 秒问一次也不费事。"""
    u, k = ctypes.WinDLL("user32"), ctypes.WinDLL("kernel32")
    u.GetForegroundWindow.restype = ctypes.c_void_p
    k.OpenProcess.restype = wintypes.HANDLE
    hwnd = u.GetForegroundWindow()
    title = ctypes.create_unicode_buffer(400)
    if not hwnd or not u.GetWindowTextW(ctypes.c_void_p(hwnd), title, 400):
        return None
    pid = wintypes.DWORD()
    u.GetWindowThreadProcessId(ctypes.c_void_p(hwnd), ctypes.byref(pid))
    h = k.OpenProcess(0x1000, False, pid.value)
    if not h:
        return None
    try:
        buf, n = ctypes.create_unicode_buffer(520), wintypes.DWORD(520)
        if not k.QueryFullProcessImageNameW(wintypes.HANDLE(h), 0, buf, ctypes.byref(n)):
            return None
        scheme = EDITORS.get(os.path.basename(buf.value).lower())
    finally:
        k.CloseHandle(wintypes.HANDLE(h))
    return tab_of(title.value, scheme) if scheme else None


def claude_processes():
    """正在跑的 claude 程序的命令行。"""
    try:   # wmic 快；新版 Windows 去掉了它，就退回 PowerShell
        out = subprocess.run(["wmic", "process", "where", "name='claude.exe'", "get", "CommandLine"], capture_output=True,
                             timeout=20, creationflags=NO_WINDOW).stdout.decode("mbcs", "replace")
    except (OSError, subprocess.SubprocessError):
        out = install.ps("Get-CimInstance Win32_Process -Filter \"Name='claude.exe'\" | ForEach-Object { $_.CommandLine }")
    return [line.strip() for line in out.splitlines() if line.strip() and line.strip() != "CommandLine"]


def ext_editor(cmd):
    """claude 程序的命令行 -> (它是哪个编辑器的扩展起的, 扩展版本)；不是编辑器扩展起的返回 None。"""
    m = re.search(r"[\\/](\.cursor|\.vscode|\.vscode-insiders)[\\/]extensions[\\/]anthropic\.claude-code-(\d+\.\d+\.\d+)", cmd, re.I)
    return (EXT_DIRS[m.group(1).lower()], m.group(2)) if m else None


def session_editor(sid, version="", cmds=None):
    """这个对话开在哪个编辑器里：(链接协议, 确定不确定)，认不出返回 (None, False)。
    编辑器里开着的对话，背后有一个扩展起的常驻 claude 进程，程序就在那个编辑器的扩展目录里（.cursor / .vscode）。
    命令行带 --resume=<这个 id> 的是确定的；新建后没关过的对话命令行里没有 id，只能看哪个编辑器里有不带 id、
    扩展版本又和对话记录里一样的进程——只有一个编辑器对得上才算，而且只是猜。"""
    guess = set()
    for cmd in claude_processes() if cmds is None else cmds:
        ed = ext_editor(cmd)
        if not ed:
            continue
        if ("--resume=" + sid) in cmd or ("--resume " + sid) in cmd:
            return ed[0], True
        if "--resume" not in cmd and version and ed[1] == version:
            guess.add(ed[0])
    return (guess.pop(), False) if len(guess) == 1 else (None, False)


R_FILES = re.compile(r'opened the file (.+?) in the IDE|selected the lines \d+ to \d+ from (.+?):\\n|"file_path":"(.+?)"')


def transcript_hints(path, tail=8 << 20):
    """从对话记录里取认窗口用的线索：Claude Code 的版本，和对话碰过的文件名（编辑器里打开 / 选中的、工具读写的）。"""
    hints = {"version": "", "files": set()}
    try:
        with open(path, "rb") as f:
            f.seek(max(0, os.path.getsize(path) - tail))
            text = f.read().decode("utf-8", "replace")
    except (OSError, TypeError):
        return hints
    versions = re.findall(r'"version":"(\d+\.\d+\.\d+)"', text)
    hints["version"] = versions[-1] if versions else ""
    for m in R_FILES.finditer(text):
        p = next(g for g in m.groups() if g)
        hints["files"].add(re.split(r"[\\/]+", p)[-1])
    return hints


def is_home(root):
    return bool(root) and os.path.normcase(os.path.abspath(root)) == os.path.normcase(os.path.expanduser("~"))


def locate(sid, root, transcript=None):
    """开着这个对话的编辑器窗口。返回 ((窗口句柄, 链接协议) 或 None, 对话所在的编辑器或 None)。
    先认编辑器（session_editor），再在它的窗口里按标题找。标题是「当前标签 - 项目文件夹名 - 编辑器名」，文件夹名对上
    启动目录的就是。只打开了文件、没打开文件夹的窗口里开的对话，启动目录是用户目录，标题里也没有文件夹名
    （「当前标签 - 编辑器名」），这时找标题只有两段的窗口。好几个都像时，挑当前标签是这个对话碰过的文件的那个。"""
    hints = transcript_hints(transcript or collector.find_transcript(sid))
    editor, sure = session_editor(sid, hints["version"])
    name = os.path.basename(str(root).rstrip("\\/")) if root else ""
    loose = is_home(root)
    windows = editor_windows()
    pools = [windows]
    if editor:   # 认出了编辑器就只在它的窗口里找；只是猜的，找不到再放宽到所有编辑器
        pools = [[w for w in windows if w[2] == editor]] + ([] if sure else [windows])
    for pool in pools:
        best = None
        for hwnd, title, scheme in pool:
            parts = title.lstrip("● ").split(" - ")
            if name and (" - %s - " % name in title or title.startswith(name + " - ")):
                score = 2
            elif loose and len(parts) <= 2:
                score = 2
            else:
                continue
            if len(parts) >= 2 and parts[0] in hints["files"]:
                score += 1
            if not best or score > best[0]:
                best = (score, hwnd, scheme)
        if best:
            return best[1:], editor
    return None, editor


def force_foreground(hwnd):
    """把一个窗口切到前台，返回成没成。Windows 不让后台程序随便抢焦点，所以借当前前台窗口的线程来切
    （和完成通知脚本 notify.ps1 点通知唤起 Cursor 用的是同一招）。"""
    u, k = ctypes.WinDLL("user32"), ctypes.WinDLL("kernel32")
    u.GetForegroundWindow.restype = ctypes.c_void_p
    u.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    for f in (u.IsIconic, u.ShowWindow, u.BringWindowToTop, u.SetForegroundWindow, u.SwitchToThisWindow):
        f.argtypes = [ctypes.c_void_p] + ([ctypes.c_int] if f in (u.ShowWindow, u.SwitchToThisWindow) else [])
    if u.IsIconic(hwnd):
        u.ShowWindow(hwnd, 9)   # SW_RESTORE
    if u.GetForegroundWindow() == hwnd:
        return True
    lock = wintypes.UINT()
    u.SystemParametersInfoW(0x2000, 0, ctypes.byref(lock), 0)   # 记下「前台锁」的超时，临时清零，事后还原
    u.SystemParametersInfoW(0x2001, 0, None, 2)
    me, fg = k.GetCurrentThreadId(), u.GetWindowThreadProcessId(u.GetForegroundWindow(), None)
    attached = bool(fg and fg != me and u.AttachThreadInput(me, fg, True))
    u.BringWindowToTop(hwnd)
    u.SetForegroundWindow(hwnd)
    u.SwitchToThisWindow(hwnd, True)
    if attached:
        u.AttachThreadInput(me, fg, False)
    u.SystemParametersInfoW(0x2001, 0, ctypes.c_void_p(lock.value), 2)
    time.sleep(0.2)
    return u.GetForegroundWindow() == hwnd


def open_in_editor(sid, root, prompt=""):
    """跳到编辑器里的那个对话，输入框就在眼前；prompt 不为空时预填进去（不会自动发出去）。
    Claude Code 扩展的链接入口总是在最前面的那个编辑器窗口里打开对话，所以先把开着这个项目（root）的窗口
    顶到前面；这个项目一个窗口都没开的话，先用编辑器的命令行把它打开。放在后台线程里做，不卡界面。"""
    threading.Thread(target=_open_in_editor, args=(sid, root, prompt), daemon=True).start()


log = None   # 悬浮窗会把它的日志函数放进来，跳转的每一步都记一笔，点了没反应时好查


def _open_in_editor(sid, root, prompt):
    try:
        return _jump(sid, root, prompt)
    except Exception as e:
        if log:
            log("跳转出错：%r" % e)
        return False


def _jump(sid, root, prompt):
    say = log or (lambda text: None)
    hit, editor = locate(sid, root)
    say("跳转 %s：启动目录 %s，对话在 %s 里，找到的窗口 %s" % (sid, root or "（不知道）", editor or "（认不出）", hit and hit[1]))
    if not hit:
        say("跳转：这个对话没有开着的编辑器窗口，先打开一个")
    hit = open_project(root, hit, say, editor)
    # 编辑器把链接交给「最近被激活的那个窗口」，目标窗口没切到前台就发，对话会被打开到别的窗口里去
    if hit and not force_foreground(hit[0]):
        say("跳转：没能把编辑器窗口切到前台，没发链接")
        return False
    windows = editor_windows()
    scheme = hit[1] if hit else (editor or (windows[0][2] if windows else "cursor"))
    url = "%s://anthropic.claude-code/open?session=%s" % (scheme, quote(sid))
    before = log_sizes()
    os.startfile(url + ("&prompt=" + quote(prompt) if prompt else ""))
    say("跳转：已发链接 %s" % url)
    if not panel_focused(before, 6):
        # 编辑器第一次收到外面来的链接时会先问「允许 Claude Code 扩展打开此 URI 吗？」，勾上不再询问后就不问了
        say("跳转：6 秒内 Claude 面板没拿到焦点，可能编辑器在问「允许打开此 URI 吗」")
    return True


def open_project(root, hit, say, editor=None):
    """这个对话没有开着的编辑器窗口，就用编辑器的命令行打开它的启动目录，等新窗口出来；返回窗口（还是没有就返回 None）。
    启动目录是用户目录的（在没打开文件夹的窗口里开的对话），开一个空窗口，不把整个用户目录当项目打开。"""
    if hit or not root or not os.path.isdir(root):
        return hit
    cli = editor_cli(editor)
    if not cli:
        say("跳转：没找到编辑器的命令行")
        return None
    before = {w[0] for w in editor_windows()}
    say("用 %s 打开 %s" % (cli, "一个空窗口" if is_home(root) else root))
    subprocess.Popen([cli, "--new-window"] if is_home(root) else [cli, root], creationflags=NO_WINDOW)
    for _ in range(40):   # 等新窗口出来，最多 20 秒
        time.sleep(0.5)
        new = [w for w in editor_windows() if w[0] not in before]
        if new:
            time.sleep(2)   # 窗口出来了，扩展还要一会儿才加载好
            return new[0][0], new[0][2]
    return None


# ---------- 替你在编辑器里打字回复（手机网页用） ----------
# 编辑器里开着的对话，背后有一个常驻的 claude 进程把整段对话握在内存里；从外面另起一个进程往同一个对话里写，
# 编辑器看不到，之后两边还会写岔。所以回复要真的打进编辑器里那个对话：用扩展的链接入口打开它、把话填进输入框，
# 确认是 Claude 面板拿到了焦点再按回车，最后去对话记录里核对这句话真的进去了。
LOGS_GLOB = (r"%APPDATA%\Cursor\logs\*\window*\exthost\Anthropic.claude-code\*.log",
             r"%APPDATA%\Code\logs\*\window*\exthost\Anthropic.claude-code\*.log")
TYPE_LOCK = threading.Lock()   # 一次只替一句话打字


def locked():
    """电脑是不是锁屏了（锁屏时没法替用户打字）。"""
    u = ctypes.WinDLL("user32")
    u.OpenInputDesktop.restype = ctypes.c_void_p
    u.SwitchDesktop.argtypes = u.CloseDesktop.argtypes = [ctypes.c_void_p]
    desk = u.OpenInputDesktop(0, False, 0x0100)   # DESKTOP_SWITCHDESKTOP
    if not desk:
        return True
    try:
        return not u.SwitchDesktop(desk)
    finally:
        u.CloseDesktop(desk)


def editor_holds(sid):
    """编辑器里是不是开着这个对话：开着的对话，扩展会起一个带 --resume=<这个 id> 的常驻 claude 进程。
    新建后没关过的对话命令行里没有 id，认不出来——所以这个函数说「没开着」不一定准，说「开着」是准的。"""
    return any(("--resume=" + sid) in cmd or ("--resume " + sid) in cmd for cmd in claude_processes())


def log_sizes():
    sizes = {}
    for pattern in LOGS_GLOB:
        for path in glob.glob(os.path.expandvars(pattern)):
            try:
                if time.time() - os.path.getmtime(path) < 7 * 86400:
                    sizes[path] = os.path.getsize(path)
            except OSError:
                pass
    return sizes


def wait_log(before, needles, seconds):
    """等编辑器里 Claude 扩展的日志出现 needles 里的任意一个（只看 before 之后新写的部分）。before 是开始等之前各日志的长度。"""
    end = time.time() + seconds
    while time.time() < end:
        time.sleep(0.2)
        for path, size in log_sizes().items():
            if size > before.get(path, 0):
                with open(path, "rb") as f:
                    f.seek(before.get(path, 0))
                    data = f.read()
                if any(n in data for n in needles):
                    return True
    return False


def panel_focused(before, seconds):
    """等 Claude 面板拿到焦点：扩展会在日志里记一条 webview_focused。"""
    return wait_log(before, (b'"webview_focused"',), seconds)


class _KEY(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.c_size_t)]


class _MOUSE(ctypes.Structure):   # 只为了让下面的 union 大小和系统的 INPUT 一致
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class _INPUT(ctypes.Structure):
    class _U(ctypes.Union):
        _fields_ = [("ki", _KEY), ("mi", _MOUSE)]
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _U)]


def _keys(events):
    """events：[(虚拟键码, 字符编码, 标志)]，一次性发出去。"""
    arr = (_INPUT * len(events))()
    for i, (vk, scan, flags) in enumerate(events):
        arr[i].type = 1   # INPUT_KEYBOARD
        arr[i].ki = _KEY(vk, scan, flags, 0, 0)
    ctypes.WinDLL("user32").SendInput(len(events), arr, ctypes.sizeof(_INPUT))


def type_text(text):
    """模拟键盘把一段话打进当前有焦点的输入框：每个字用 Unicode 方式送（不经过输入法，中文也能打），换行用 Shift+回车。"""
    events = []
    for ch in text.replace("\r\n", "\n"):
        if ch == "\n":
            events += [(0x10, 0, 0), (0x0D, 0, 0), (0x0D, 0, 2), (0x10, 0, 2)]   # Shift 按下、回车、松开
            continue
        data = ch.encode("utf-16-le")
        for j in range(0, len(data), 2):   # 表情之类的字要拆成两个 UTF-16 单元
            unit = int.from_bytes(data[j:j + 2], "little")
            events += [(0, unit, 4), (0, unit, 4 | 2)]   # KEYEVENTF_UNICODE，按下 / 松开
    for i in range(0, len(events), 200):
        _keys(events[i:i + 200])
        time.sleep(0.02)


def press_enter():
    _keys([(0x0D, 0, 0), (0x0D, 0, 2)])


def has_user_text(data, text):
    """一段对话记录（字节）里有没有一条用户说的、开头和 text 一样的话。"""
    head = " ".join(text.split())[:24]
    for raw in data.split(b"\n"):
        if not head or not any(k in raw for k in (b'"type":"user"', b'"queued_command"', b'"queue-operation"')):
            continue
        try:
            e = json.loads(raw)
        except ValueError:
            continue
        # 对话闲着时，这句记成一条用户发言；它正忙时，记成「排队的命令」，等手上这一步做完再交给它
        content = (e.get("message") or {}).get("content") or (e.get("attachment") or {}).get("prompt") or e.get("content")
        if isinstance(content, list):
            content = " ".join(b.get("text") or "" for b in content if isinstance(b, dict) and b.get("type") == "text")
        if isinstance(content, str) and head in " ".join(content.split()):
            return True
    return False


def said(path, start, text, seconds):
    """对话记录里从 start 往后，有没有出现一条用户说的、开头和 text 一样的话。"""
    end = time.time() + seconds
    while time.time() < end:
        time.sleep(0.5)
        try:
            with open(path, "rb") as f:
                f.seek(start)
                if has_user_text(f.read(), text):
                    return True
        except OSError:
            continue
    return False


NO_PANEL = "如果电脑上的编辑器在问「允许 Claude Code 扩展打开此 URI 吗」，勾上「不再询问」点「打开」，再发一次。"


def foreground():
    u = ctypes.WinDLL("user32")
    u.GetForegroundWindow.restype = ctypes.c_void_p
    return u.GetForegroundWindow()


def type_and_send(hwnd, text, settle):
    """Claude 面板已经拿到焦点：等 settle 秒让输入框准备好，打字（text 为空就是话已经在输入框里了）、按回车。
    焦点中途跑掉就停手，返回说明；都做完返回 None。"""
    time.sleep(settle)
    if foreground() != hwnd:
        return "焦点被别的窗口抢走了，没有打字。"
    if text:
        type_text(text)
        time.sleep(0.4)
        if foreground() != hwnd:
            return "打字的时候焦点被别的窗口抢走了，没有按回车；去电脑上看一眼那个对话的输入框。"
    press_enter()
    return None


def type_into_editor(sid, root, text, transcript):
    """把 text 当成用户在编辑器里对这个对话说的一句话发出去。返回（成没成，一句说明）。"""
    say = log or (lambda t: None)
    with TYPE_LOCK:
        if locked():
            return False, "电脑锁屏了，没法替你在编辑器里打字。解锁后再发。"
        hit, editor = locate(sid, root, transcript)
        hit = open_project(root, hit, say, editor)
        if not hit:
            return False, "找不到开着这个对话的编辑器窗口。"
        if not force_foreground(hit[0]):
            return False, "没能把编辑器窗口切到前面，没有发送。"
        before, start = log_sizes(), os.path.getsize(transcript)
        # 不用链接的 prompt= 预填：对话面板已经开着时扩展只把它切到前面，预填的字会被丢掉。改成面板拿到焦点后自己打字
        os.startfile("%s://anthropic.claude-code/open?session=%s" % (hit[1], quote(sid)))
        if not panel_focused(before, 6):
            say("回复 %s：Claude 面板没拿到焦点，没打字" % sid)
            return False, "编辑器里的 Claude 面板没有拿到焦点。为了不把字打到别处，这句没有发送。" + NO_PANEL
        problem = type_and_send(hit[0], text, 0.8)   # 等面板切过来、输入框拿到焦点
        if problem:
            return False, problem
        say("回复 %s：已在编辑器里打字并按下回车" % sid)
        if said(transcript, start, text, 20):
            return True, "已经在电脑上的编辑器里发出去了。"
        return False, "按了回车，但对话记录里 20 秒内没看到这句话。去电脑上看一眼那个对话的输入框。"


def answered(path, start, tool_id, seconds):
    """对话记录里 start 往后，这个提问（tool_use_id）有没有出结果（答了或者撤下了）。"""
    needle = ('"tool_use_id":"%s"' % tool_id).encode()
    end = time.time() + seconds
    while time.time() < end:
        time.sleep(0.3)
        try:
            with open(path, "rb") as f:
                f.seek(start)
                if any(needle in raw and b'"tool_result"' in raw for raw in f.read().split(b"\n")):
                    return True
        except OSError:
            pass
    return False


def answer_in_editor(sid, root, tool_id, text, transcript):
    """在手机上回答编辑器里那道选择题（AskUserQuestion）。那个选择界面由 Claude Code 自己画、靠面板内部的消息来回，
    从外面点不进去，所以：打开对话（同 type_into_editor）→ 按 Esc 撤下提问 → 在对话记录里确认它真的撤下了 →
    把选择当成一句话打进去发出。返回（成没成，一句说明）。"""
    say = log or (lambda t: None)
    with TYPE_LOCK:
        if locked():
            return False, "电脑锁屏了，没法替你在编辑器里回答。解锁后再发。"
        hit, editor = locate(sid, root, transcript)
        hit = open_project(root, hit, say, editor)
        if not hit:
            return False, "找不到开着这个对话的编辑器窗口。"
        if not force_foreground(hit[0]):
            return False, "没能把编辑器窗口切到前面，没有发送。"
        before, start = log_sizes(), os.path.getsize(transcript)
        os.startfile("%s://anthropic.claude-code/open?session=%s" % (hit[1], quote(sid)))
        if not panel_focused(before, 6):
            return False, "编辑器里的 Claude 面板没有拿到焦点，没有动它。" + NO_PANEL
        time.sleep(0.8)
        if foreground() != hit[0]:
            return False, "焦点被别的窗口抢走了，没有动它。"
        _keys([(0x1B, 0, 0), (0x1B, 0, 2)])   # Esc：撤下这个提问
        if not answered(transcript, start, tool_id, 8):
            say("回答 %s：按了 Esc，提问没撤下来" % sid)
            return False, "按了 Esc，但电脑上那个提问没撤下来，没有发你的选择。回到电脑前直接点选。"
        time.sleep(0.8)   # 撤下以后界面回到输入框
        start = os.path.getsize(transcript)
        problem = type_and_send(hit[0], text, 0.3)
        if problem:
            return False, problem + "（提问已经撤下了，可以直接在手机上回复你的选择。）"
        say("回答 %s：撤下提问、打字并按下回车" % sid)
        if said(transcript, start, text, 20):
            return True, "已经把你的选择发过去了，它会接着做。"
        return False, "按了回车，但对话记录里 20 秒内没看到你的选择。提问已经撤下了，可以直接在手机上回复你的选择。"


# ---------- 编辑器里的一个快捷键：新开一个 Claude 对话，光标在输入框里 ----------
# 扩展的链接入口（primaryEditor.open）开出来的新对话不聚焦输入框，替用户打字就打不进去；界面上的「Claude Code: Open in New Tab」
# （editor.open，不带参数调用时 focusInput 为真）会聚焦。它默认的快捷键 Ctrl+Shift+Esc 被 Windows 拿去开任务管理器了，
# 所以征得用户同意后，往编辑器的 keybindings.json 末尾加一条我们自己的快捷键（10-17 用户定的）。
NEW_TAB_KEY = "ctrl+alt+shift+f12"
NEW_TAB_COMMAND = "claude-vscode.editor.open"
KEYBINDINGS = {"cursor": r"%APPDATA%\Cursor\User\keybindings.json", "vscode": r"%APPDATA%\Code\User\keybindings.json",
               "vscode-insiders": r"%APPDATA%\Code - Insiders\User\keybindings.json"}


def strip_jsonc(text):
    """去掉 // 和 /* */ 注释（字符串里的不算）和尾随逗号，好用 json 读编辑器的设置文件。"""
    out, i, n, in_str = [], 0, len(text), False
    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if c == "\\":
                out.append(text[i + 1:i + 2])
                i += 2
                continue
            in_str = c != '"'
        elif c == '"':
            in_str = True
            out.append(c)
        elif text.startswith("//", i):
            j = text.find("\n", i)
            i = n if j < 0 else j
            continue
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            i = n if j < 0 else j + 2
            continue
        else:
            out.append(c)
        i += 1
    return re.sub(r",(\s*[\]}])", r"\1", "".join(out))


def keybindings_file(scheme):
    return os.path.expandvars(KEYBINDINGS[scheme]) if scheme in KEYBINDINGS else ""


def read_keybindings(path):
    """keybindings.json 里的快捷键列表；没有这个文件是空列表；读不懂返回 None。"""
    try:
        with open(path, encoding="utf-8-sig") as f:
            text = f.read()
    except FileNotFoundError:
        return []
    except OSError:
        return None
    try:
        items = json.loads(strip_jsonc(text) or "[]")
    except ValueError:
        return None
    return items if isinstance(items, list) else None


def has_new_tab_key(scheme):
    items = read_keybindings(keybindings_file(scheme)) or []
    return any(isinstance(b, dict) and b.get("command") == NEW_TAB_COMMAND and not b.get("when")
               and str(b.get("key", "")).replace(" ", "").lower() == NEW_TAB_KEY for b in items)


def add_new_tab_key(scheme):
    """往编辑器的 keybindings.json 末尾加上我们的快捷键，别的内容一个字不动；改之前备份一份，改完读回来核对，不对就还原。
    返回（成没成, 一句说明）。"""
    path = keybindings_file(scheme)
    if not path:
        return False, "不认识这个编辑器"
    if has_new_tab_key(scheme):
        return True, "已经有了"
    items = read_keybindings(path)
    if items is None:
        return False, "%s 的格式认不出来，没敢改" % path
    entry = '{\n    "key": "%s",\n    "command": "%s"\n  }' % (NEW_TAB_KEY, NEW_TAB_COMMAND)
    try:
        with open(path, encoding="utf-8-sig") as f:
            text = f.read()
    except FileNotFoundError:
        text = ""
    end = text.rfind("]")
    if not text.strip() or end < 0:
        new = "[\n  %s\n]\n" % entry
    else:
        head = text[:end].rstrip()
        last_line = head.splitlines()[-1] if head else ""
        if not items or strip_jsonc(head).rstrip().endswith((",", "[")):
            sep = ""
        elif strip_jsonc(last_line).rstrip() == last_line.rstrip():
            sep = ","             # 最后一行没有注释：逗号直接跟在后面
        else:
            sep = "\n  ,"         # 最后一行带注释（注释里也可能以 } 结尾）：逗号另起一行，免得被注释吃掉
        new = head + sep + "\n  " + entry + "\n" + text[end:]
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if text:
            shutil.copy2(path, path + ".agent-hud.bak")
        with open(path + ".agent-hud.tmp", "w", encoding="utf-8") as f:
            f.write(new)
        os.replace(path + ".agent-hud.tmp", path)
    except OSError as e:
        return False, "写不进 %s（%s）" % (path, e)
    if has_new_tab_key(scheme):
        return True, "已经加上了"
    if text:   # 读回来不对：还原
        shutil.copy2(path + ".agent-hud.bak", path)
    return False, "加完读回来不对，已经还原"


def press_new_tab_key():
    """按一下 Ctrl+Alt+Shift+F12。"""
    keys = (0x11, 0x12, 0x10, 0x7B)   # Ctrl、Alt、Shift、F12
    _keys([(k, 0, 0) for k in keys] + [(k, 0, 2) for k in reversed(keys)])


def wait_log_after(before, first, then, seconds):
    """等扩展的日志里先出现 first、在它后面又出现 then（同一个日志文件里，只看 before 之后新写的）。"""
    end = time.time() + seconds
    while time.time() < end:
        time.sleep(0.2)
        for path, size in log_sizes().items():
            if size > before.get(path, 0):
                with open(path, "rb") as f:
                    f.seek(before.get(path, 0))
                    data = f.read()
                i = data.find(first)
                if i >= 0 and data.find(then, i) >= 0:
                    return True
    return False


# ---------- 在指定的编辑器窗口里新开一个对话（发起任务时选了窗口） ----------
EDITOR_NAMES = {"cursor": "Cursor", "vscode": "VS Code", "vscode-insiders": "VS Code Insiders"}


def window_folder(title):
    """编辑器窗口标题里的文件夹名。标题是「当前标签 - 文件夹名 - 编辑器名」；只有两段时（没开文件夹、或者没开标签）
    分不清第一段是标签还是文件夹，也当它是文件夹名——拿它和项目路径比，标签名一般对不上。"""
    parts = title.lstrip("● ").split(" - ")
    if len(parts) >= 3:
        return parts[-2].strip()
    return parts[0].strip() if len(parts) == 2 else ""


def compose_windows(project):
    """发起任务时能选的编辑器窗口：[{hwnd, scheme, label, folder, depth}]。depth：窗口开的文件夹是这个项目本身（0）、
    上一级（1）……，不包含这个项目时是 None。包含项目的排前面，越贴近越前。"""
    names = []
    p = os.path.normpath(project) if project else ""
    while p and os.path.basename(p):
        names.append(os.path.basename(p).lower())
        p = os.path.dirname(p)
    out, seen = [], {}
    for hwnd, title, scheme in editor_windows():
        folder = window_folder(title)
        label = "%s · %s" % (folder or title, EDITOR_NAMES.get(scheme, scheme))
        seen[label] = seen.get(label, 0) + 1
        if seen[label] > 1:   # 同一个文件夹开了两个窗口
            label += "（%d）" % seen[label]
        depth = names.index(folder.lower()) if folder and folder.lower() in names else None
        out.append({"hwnd": hwnd, "scheme": scheme, "label": label, "folder": folder, "depth": depth})
    return sorted(out, key=lambda w: (w["depth"] is None, w["depth"] or 0))


def new_session(known, text, seconds):
    """等一个新对话的记录出现：known 是开始前已有的记录文件，新出现的里面第一句是 text 的那个就是。返回它的 id。"""
    end = time.time() + seconds
    while time.time() < end:
        time.sleep(0.5)
        for path in set(glob.glob(os.path.join(collector.PROJECTS, "*", "*.jsonl"))) - known:
            try:
                with open(path, "rb") as f:
                    if has_user_text(f.read(256 << 10), text):
                        return os.path.basename(path)[:-len(".jsonl")]
            except OSError:
                pass
    return None


PREFILL_MAX = 1800   # 链接再长，有的系统会截断；话太长就不预填


def start_in_editor(hwnd, scheme, text, wait=30, use_key=True):
    """在指定的编辑器窗口里新开一个 Claude 对话，把 text 发出去。返回（成没成, 一句说明, 新对话的 id）；
    成没成是 None 表示「对话开好了、话填好了，等用户自己按回车」。

    use_key（编辑器里加了我们的快捷键）：按快捷键 → 扩展新开一个对话标签、把光标放进输入框，日志里先记新面板的 init、
    接着记 webview_focused → 确认到了才替用户打字、按回车 → 去对话记录里找到这个新对话。
    没有快捷键：扩展的链接入口开新对话并预填（只预填不发送，光标也不在输入框里，10-17 试过三次都按不出去），
    话填好就交给用户按回车。"""
    say = log or (lambda t: None)
    with TYPE_LOCK:
        if locked():
            return False, "电脑锁屏了，没法在编辑器里新开对话。解锁后再发起。", None
        if not force_foreground(hwnd):
            return False, "没能把那个编辑器窗口切到前面，没有发起。", None
        time.sleep(0.4)   # 窗口切过来时，原来拿着焦点的面板会记一条 webview_focused；等它记完再开始看，免得当成新面板的
        before = log_sizes()
        known = set(glob.glob(os.path.join(collector.PROJECTS, "*", "*.jsonl")))
        if not use_key:
            url = "%s://anthropic.claude-code/open" % scheme
            prefill = len(url) + len(quote(text)) + 8 <= PREFILL_MAX
            os.startfile(url + ("?prompt=" + quote(text) if prefill else ""))
            if not wait_log(before, (b'"engine_ui_attach"',), 15):
                return False, "编辑器里没开出新的 Claude 对话。" + NO_PANEL, None
            return None, ("新对话开好了，要做的事也填进了输入框，去那个窗口按一下回车就发出去。" if prefill else
                          "新对话开好了，但要做的事太长放不进去，复制过去再按回车。"), None
        press_new_tab_key()
        if not wait_log_after(before, b'"type":"init"', b'"webview_focused"', 15):
            say("新对话：按了快捷键，15 秒内没看到新对话的输入框拿到焦点")
            return False, ("按了新开对话的快捷键，但没看到新对话的输入框拿到焦点，没有打字。"
                           "可能焦点在编辑器的终端里，或者快捷键没生效（编辑器的 keybindings.json 里要有 %s）。" % NEW_TAB_KEY), None
        wait_log(before, (b'"engine_ui_attach"',), 5)   # 一般这时已经挂上了；没有也不硬等
        problem = type_and_send(hwnd, text, 0.5)
        if problem:
            return False, problem, None
        sid = new_session(known, text, wait)
        say("新对话：已打字并按下回车，找到的对话 %s" % sid)
        if sid:
            return True, "已经在编辑器里新开了一个对话，任务发出去了。", sid
        return False, "按了回车，但 %d 秒内没找到这个新对话。去电脑上看一眼那个窗口。" % wait, None


class Runner:
    def __init__(self, mode="auto", on_fail=None):
        self.mode = mode          # 后台任务用的权限模式，和用户平时在编辑器里用的保持一致
        self.on_fail = on_fail    # 任务没起来 / 异常退出时调用：on_fail(会话 id, 说明)
        self.lock = threading.RLock()
        self.extra = []           # 额外带给 claude 的参数（测试时用来关掉 hook）
        self.exe = None           # find_claude() 的结果，第一次用到时才找
        self.procs = {}           # 会话 id -> Popen（这次启动以来起的）
        self.queue = {}           # 会话 id -> [排着队的追问]（上一轮还没跑完时先存着）
        self.exited = {}          # 会话 id -> (结果, 时间)：进程已经没了。结果是 done / error / interrupted
        try:
            with open(FILE, encoding="utf-8") as f:
                self.tasks = json.load(f)   # 会话 id -> {"ws": 项目路径, "prompt", "created", "pid", "origin"}
        except (OSError, ValueError):
            self.tasks = {}

    def save(self):
        newest = sorted(self.tasks.items(), key=lambda kv: -kv[1].get("created", 0))[:KEEP]
        self.tasks = dict(newest)
        try:
            os.makedirs(install.TARGET, exist_ok=True)
            with open(FILE, "w", encoding="utf-8") as f:
                json.dump(self.tasks, f, ensure_ascii=False)
        except OSError:
            pass

    def claude(self):
        if self.exe is None:
            self.exe = find_claude() or ()
        return self.exe

    def owned(self):
        """自己发起的会话：id -> 项目路径。"""
        with self.lock:
            return {sid: t["ws"] for sid, t in self.tasks.items()}

    def active(self):
        """进程还活着的那些会话：id -> 这一轮是什么时候起的。"""
        with self.lock:
            return {sid: t.get("spawned", t["created"]) for sid, t in self.tasks.items() if self.running(sid)}

    def running(self, sid):
        with self.lock:
            p = self.procs.get(sid)
            if p:
                return p.poll() is None
            t = self.tasks.get(sid)   # 上一次开悬浮窗时起的任务，没有 Popen，只能按进程号认
            return bool(t and t.get("pid") and sid not in self.exited and self.claude()
                        and alive(t["pid"], os.path.basename(self.claude()[0][0])))

    def _spawn(self, sid, prompt, resume):
        exe = self.claude()
        if not exe:
            raise RuntimeError("没找到 claude 程序。需要装了 Claude Code 的编辑器扩展，或者命令行里能运行 claude。")
        task = self.tasks[sid]
        if not os.path.isdir(task["ws"]):
            raise RuntimeError("项目目录不存在：" + task["ws"])
        args = exe[0] + ["-p", "--permission-mode", self.mode, "--output-format", "json"] + self.extra
        args += ["--resume", sid] if resume else ["--session-id", sid]
        os.makedirs(LOGS, exist_ok=True)
        with open(os.path.join(LOGS, sid + ".log"), "ab") as out:
            p = subprocess.Popen(args, cwd=task["ws"], stdin=subprocess.PIPE, stdout=out, stderr=subprocess.STDOUT,
                                 creationflags=NO_WINDOW)
        p.stdin.write(prompt.encode("utf-8"))   # 任务内容走标准输入，不放命令行：不怕引号、换行和长度
        p.stdin.close()
        self.procs[sid] = p
        self.exited.pop(sid, None)
        task.update(pid=p.pid, spawned=time.time())
        self.save()

    def start(self, ws_path, prompt, origin="hud"):
        """在一个项目里发起后台任务，返回它的会话 id。"""
        with self.lock:
            sid = str(uuid.uuid4())
            self.tasks[sid] = {"ws": ws_path, "prompt": prompt.strip()[:300], "created": time.time(), "origin": origin}
            try:
                self._spawn(sid, prompt, resume=False)
            except Exception:
                del self.tasks[sid]
                raise
            return sid

    def follow(self, sid, prompt):
        """对自己发起的任务追问。上一轮还在跑就先排队，跑完自动发；返回 "sent" 或 "queued"。"""
        with self.lock:
            if sid not in self.tasks:
                raise RuntimeError("这个对话不是悬浮窗发起的，不能从这里往里发消息。")
            if self.running(sid) or self.queue.get(sid):
                self.queue.setdefault(sid, []).append(prompt)
                return "queued"
            self._spawn(sid, prompt, resume=True)
            return "sent"

    def stop(self, sid):
        with self.lock:
            self.queue.pop(sid, None)
            if not self.running(sid):
                return False
            pid = self.procs[sid].pid if sid in self.procs else self.tasks[sid]["pid"]
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, creationflags=NO_WINDOW)
            self.procs.pop(sid, None)
            self.exited[sid] = ("interrupted", time.time())
            return True

    def tick(self):
        """每秒叫一次：收走跑完的进程（异常退出的报出去），把排着队的追问发出去。"""
        fails = []
        with self.lock:
            for sid, p in list(self.procs.items()):
                code = p.poll()
                if code is None:
                    continue
                del self.procs[sid]
                self.exited[sid] = ("done" if code == 0 else "error", time.time())
                if code != 0:
                    fails.append((sid, self.log_tail(sid) or "claude 退出码 %d" % code))
            for sid, t in self.tasks.items():   # 上一次开悬浮窗时起的任务：进程没了就记一笔
                if sid not in self.procs and sid not in self.exited and t.get("pid") and not self.running(sid):
                    self.exited[sid] = ("done", time.time())
            for sid in [s for s, q in self.queue.items() if q and not self.running(s)]:
                try:
                    self._spawn(sid, self.queue[sid].pop(0), resume=True)
                except Exception as e:
                    fails.append((sid, str(e)))
                    self.queue.pop(sid, None)
        for sid, text in fails:
            if self.on_fail:
                self.on_fail(sid, text)

    def log_tail(self, sid, limit=400):
        try:
            with open(os.path.join(LOGS, sid + ".log"), "rb") as f:
                text = f.read()[-4000:].decode("utf-8", "replace").strip()
        except OSError:
            return ""
        try:   # 正常情况下输出是一段 JSON，出错原因在 result 里
            text = json.loads(text.splitlines()[-1]).get("result") or text
        except (ValueError, IndexError, AttributeError):
            pass
        return text[-limit:]

    def starting(self, known):
        """刚起、会话记录还没出现的任务：给界面先画一张「正在启动」的卡片用。known 是已经有记录的会话 id。"""
        with self.lock:
            return [(sid, self.tasks[sid]) for sid in self.procs if sid not in known and self.procs[sid].poll() is None]
