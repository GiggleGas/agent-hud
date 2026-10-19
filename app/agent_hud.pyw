"""Agent 悬浮窗：一个置顶的小窗口，显示每个 Claude Code 会话 / 子 agent 现在在干什么、进度到哪了。

用法：pythonw agent_hud.pyw          正常启动（已经有一个在跑就直接退出）；打包成 exe 后就是双击它
      python agent_hud.pyw --demo    用假数据把各种状态都画一遍
      再加 --shot out.png            画好后截一张图就退出（验收用，不会留窗口；要装 Pillow，exe 里没有）
      再加 --scale 1.5               按 150% 屏幕缩放来画（验收用）

测试用：--exit-after 5 过 5 秒自己退出；--yes / --no 弹窗一律按「是」/「否」回答；
        --autostart on / off 启动时把开机启动设好；--compose 启动后打开「发起任务」；
        --feedback 启动后打开「反馈」；--send-feedback 文字  启动后直接发一条反馈，结果记进日志。

两个分栏：「最近 24 小时」上面是在干活的、下面是 24 小时内结束的（每条一行：项目和标题）；
「按项目」每个项目一个列表，点项目名展开 / 收起。双击一个对话跳到编辑器里的它。
拖动任意位置移动，滚轮滚动；右键有菜单（在对话上右键是这个对话的菜单）。
"""
import ctypes
import json
import math
import os
import queue
import sys
import tempfile
import threading
import time
import traceback
import tkinter as tk
from ctypes import wintypes
from tkinter import font as tkfont
from tkinter import filedialog, messagebox

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import collector  # noqa: E402
import dialogs  # noqa: E402
import feedback  # noqa: E402
import hub  # noqa: E402
import install  # noqa: E402
import notify  # noqa: E402
import runner  # noqa: E402
import tray  # noqa: E402
import updates  # noqa: E402
import usage  # noqa: E402
import web  # noqa: E402
import workspaces  # noqa: E402
from collector import LIVE  # noqa: E402

VERSION = "1.0.0"
NAME = "Agent 悬浮窗"
CONFIG = os.path.join(install.TARGET, "config.json")   # 脚本版和 exe 版共用同一份设置
LOG = os.path.join(install.TARGET, "hud.log")
SHOW = os.path.join(install.TARGET, "show")            # 再启动一次时留下这个空文件，正在跑的那个看到就把窗口叫回来
SESSIONS = os.path.join(install.TARGET, "sessions.json")   # 对话 id -> 项目名，给别的程序（比如完成通知的脚本）查

# 自己加载一份 user32 并声明参数类型，窗口句柄在 64 位系统上才不会被截断
USER = ctypes.WinDLL("user32")
USER.GetAncestor.restype = USER.GetWindow.restype = ctypes.c_void_p
USER.GetAncestor.argtypes = USER.GetWindow.argtypes = [ctypes.c_void_p, ctypes.c_uint]
USER.IsWindowVisible.argtypes = [ctypes.c_void_p]
USER.GetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int]
USER.SetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_long]
USER.SetWindowPos.argtypes = [ctypes.c_void_p, ctypes.c_void_p] + [ctypes.c_int] * 4 + [ctypes.c_uint]
USER.GetWindowRect.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.RECT)]
FIRST_RUN = """要让进度条显示「第几步 / 共几步」吗？

选「是」会在
%s
末尾加一小段规则，让 Claude 做多步任务时在对话里报一行「进度 2/5 · 这一步在做什么」，对你所有项目里新开的对话生效。

选「否」悬浮窗照常能用，只是进度条没有步数。
以后可以在悬浮窗的右键菜单里改。"""
UNINSTALL = """卸载会删掉悬浮窗留在这台电脑上的东西：

· CLAUDE.md 里它加的进度规则
· 开机启动
· 窗口位置等设置

然后悬浮窗退出。程序所在的文件夹请你自己删。

确定卸载吗？"""

W, W_FOLDED = 440, 250   # 窗口宽度（100% 缩放下的像素）
PAD = 10
MAX_SUB = 6              # 一个会话最多列几个在跑的子 agent
MAX_DONE = 20            # 「按项目」里一个项目展开后最多列几条，多的折成一句「还有 N 条」
RECENT_ROWS = 10         # 「最近 24 小时」里先显示几条结束了的，多的点「再显示」
MAX_HEIGHT = 0.8         # 窗口最高占屏幕高度的多少，再多就靠滚轮滚
USAGE_H = 24             # Claude 用量那一行的高度
UPDATE_H = 24            # 「有新版本」那一行的高度
FLASH_EVERY, FLASH_SWEEP = 1.0, 0.3   # 任务完成的闪光：每几秒扫一次、一次扫多久（秒）
FLASH_ALPHA = 0.75       # 闪光那一层的不透明度
# 光的样子，沿着扫的方向从前往后：(这一段多宽, 颜色)，宽度是 100% 缩放下 x+y 方向的像素（垂直方向大约再乘 0.7）。
# 一条细白线，两边各一窄条淡蓝过渡。试过拖一条渐暗的尾巴：整层只有一个透明度，尾巴会分出一节节色带、暗色还会把字压脏，弃了
FLASH_SHAPE = ((2, "#8fb3ff"), (3, "#dce8ff"), (4, "#ffffff"), (3, "#dce8ff"), (2, "#8fb3ff"))
FLASH_FRAME = 16         # 扫光的时候多少毫秒画一帧
FONT = "Microsoft YaHei UI"
KEY = "#010203"          # 这个颜色的像素是透明的，用来做圆角
C = {
    "panel": "#15171c", "edge": "#30343f", "card": "#1e2129", "track": "#383d4a",
    "text": "#e8eaf0", "dim": "#9aa1b0", "faint": "#636a79",
    "run": "#4c9aff", "subs": "#8b7bff", "ask": "#f5a623", "confirm": "#f5a623",
    "done": "#3ecf8e", "error": "#ef5350", "interrupted": "#7a8191", "stale": "#7a8191",
}
TRAY_COLORS = {"idle": C["faint"], "run": C["run"], "ask": C["ask"], "done": C["done"]}   # 标题栏的点和托盘图标的颜色


def log(text):
    try:
        if os.path.exists(LOG) and os.path.getsize(LOG) > 200_000:
            os.remove(LOG)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + text.rstrip() + "\n")
    except OSError:
        pass


def fmt_dur(sec):
    sec = max(0, int(sec))
    if sec >= 3600:
        return "%d:%02d:%02d" % (sec // 3600, sec % 3600 // 60, sec % 60)
    return "%d:%02d" % (sec // 60, sec % 60)


def fmt_ago(sec):
    if sec < 60:
        return "刚刚"
    if sec < 3600:
        return "%d 分钟前" % (sec // 60)
    return "%d 小时前" % (sec // 3600)


def mix(a, b, t):
    pa = [int(a[i:i + 2], 16) for i in (1, 3, 5)]
    pb = [int(b[i:i + 2], 16) for i in (1, 3, 5)]
    return "#%02x%02x%02x" % tuple(round(pa[i] + (pb[i] - pa[i]) * t) for i in range(3))


def round_points(x0, y0, x1, y1, r):
    r = max(0, min(r, (x1 - x0) / 2, (y1 - y0) / 2))
    return [x0 + r, y0, x0 + r, y0, x1 - r, y0, x1 - r, y0, x1, y0, x1, y0 + r, x1, y0 + r,
            x1, y1 - r, x1, y1 - r, x1, y1, x1 - r, y1, x1 - r, y1, x0 + r, y1, x0 + r, y1,
            x0, y1, x0, y1 - r, x0, y1 - r, x0, y0 + r, x0, y0 + r, x0, y0]


def rounded(x0, y0, x1, y1, r, steps=6):
    """圆角矩形的轮廓（每个角用几段折线逼近），顺时针，是个凸多边形。"""
    r = max(0.0, min(r, (x1 - x0) / 2, (y1 - y0) / 2))
    pts = []
    for cx, cy, a0 in ((x1 - r, y0 + r, -90), (x1 - r, y1 - r, 0), (x0 + r, y1 - r, 90), (x0 + r, y0 + r, 180)):
        for i in range(steps + 1):
            a = math.radians(a0 + 90 * i / steps)
            pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    return pts


def clip(poly, frame):
    """把凸多边形 poly 裁到凸多边形 frame（顺时针）里面，返回裁剪后的顶点（Sutherland–Hodgman）。"""
    out = list(poly)
    for i in range(len(frame)):
        (ax, ay), (bx, by) = frame[i], frame[(i + 1) % len(frame)]
        side = lambda p: (bx - ax) * (p[1] - ay) - (by - ay) * (p[0] - ax)   # 屏幕坐标 y 朝下：顺时针时里面 >= 0
        points, out = out, []
        for j in range(len(points)):
            p, q = points[j], points[(j + 1) % len(points)]
            sp, sq = side(p), side(q)
            if sp >= 0:
                out.append(p)
            if (sp >= 0) != (sq >= 0):
                k = sp / (sp - sq)
                out.append((p[0] + (q[0] - p[0]) * k, p[1] + (q[1] - p[1]) * k))
        if not out:
            break
    return out


def demo_usage(now):
    """假数据模式下的 Claude 用量（预览图里用）。"""
    return {"limits": [{"key": "session", "label": "5 小时", "percent": 24.0, "resets": now + 3 * 3600},
                       {"key": "weekly_all", "label": "本周", "percent": 56.0, "resets": now + 3.5 * 86400}], "at": now}


def demo_views(now):
    colors = {}

    def sub(desc, state, action, age, steps):
        return {"id": desc, "desc": desc, "state": state, "action": action, "steps": steps,
                "start": now - age, "end": now - 60, "last": now}

    def ses(title, ws, state, action, line, age, steps, marker=None, subs=(), ended=0, owned=False):
        color = colors.setdefault(ws, workspaces.PALETTE[len(colors) % len(workspaces.PALETTE)])
        return {"id": title, "title": title, "ws": ws, "ws_color": color, "ws_path": "", "root": "", "owned": owned,
                "state": state, "action": action, "line": line, "prompt": title, "last_text": line,
                "marker": marker, "steps": steps, "subs": list(subs),
                "start": now - age - ended, "end": now - ended, "last": now - ended}

    return [
        ses("订单导出改成后台任务", "shop-server", "run", "跑命令：跑导出模块的单元测试",
            "", 754, 38, marker=(3, 6, "补单元测试")),
        ses("六个设置页迁到新组件库", "web-admin", "subs", "等子 agent 干完",
            "六个页面的改法写好了，已经分给 6 个子 agent 并行改，改完我逐页对截图验收。", 1385, 21, subs=[
                sub("Migrate profile page", "done", "", 960, 88),
                sub("Migrate billing page", "done", "", 955, 74),
                sub("Migrate members page", "run", "跑命令：启动本地服务并截图", 950, 61),
                sub("Migrate security page", "run", "读文件：SecurityForm.tsx", 948, 97),
                sub("Migrate webhooks page", "error", "", 940, 12),
                sub("Migrate audit log page", "run", "思考中", 300, 9),
            ]),
        ses("排查支付回调偶发超时", "pay-gateway", "run", "读文件：callback_handler.py",
            "日志里超时都集中在整点，先看定时任务和回调有没有抢同一个连接池。", 190, 12, owned=True),
        ses("登录接口加图形验证码", "shop-server", "ask", "在问你问题，等你回答",
            "验证码有两种接法，差别在要不要改前端的登录表单。", 420, 17),
        ses("升级依赖并修掉编译警告", "web-admin", "done", "",
            "依赖都升到了最新的小版本，编译警告从 37 条清到 0。\n有两个包的大版本变了，改动面太大没动，"
            "列在 UPGRADE.md 里等你定：\n- react-router 6 → 7：路由写法要改\n- vite 5 → 6：配置项改名", 310, 54, ended=140),
        ses("给搜索接口加缓存", "shop-server", "interrupted", "", "先看了搜索接口的调用量，热门关键词占了七成。", 95, 6, ended=400),
        ses("商品详情页的价格格式", "shop-server", "done", "", "价格统一成两位小数、千分位加逗号，促销价旁边补了原价划线。", 600, 22, ended=5400),
        ses("整理部署脚本", "ops-tools", "stale", "可能已中断，14 分钟没动静", "把三个环境的部署脚本合成一个，正在改测试环境那份。", 900, 30, ended=840),
        ses("清理过期的发布分支", "ops-tools", "done", "", "删了 23 个合并过的发布分支，没合并的 4 个留着。", 200, 9, ended=86000),
    ]


class Hud:
    def __init__(self, args):
        self.demo = "--demo" in args
        self.shot = args[args.index("--shot") + 1] if "--shot" in args else None
        self.args = args
        self.gone = False   # 卸载之后不再往磁盘上写东西
        self.cfg = {"x": None, "y": None, "collapsed": False, "live_only": False, "alpha": 100, "unread": [], "seen": [],
                    "permission_mode": "auto", "last_ws": "",
                    "history_days": 3,   # 「按项目」里看几天内的记录
                    "tab": "recent",     # 当前分栏：recent（最近 24 小时）/ projects（按项目）
                    "ws_open": {},       # 「按项目」里项目名 -> 展开没有（默认收起）
                    "web_on": False,     # 手机访问开没开（默认关，在右键「手机访问…」里开）
                    "web_key": "",       # 手机访问的钥匙
                    "web_port": web.PORT,
                    "compose_where": "window",   # 发起任务时上次选的：window（在编辑器窗口里开对话）/ background（后台跑）
                    "close_action": "",  # 点 ✕ 时：空 = 每次问；tray = 收到托盘；quit = 退出
                    "flash_on_done": True,   # 任务完成时悬浮窗闪光，点一下才停
                    "notify_url": "",    # 手机通知：用户自己的群机器人地址（飞书 / 钉钉 / 企业微信），空着就不发
                    "notify_done": True, # 做完 / 出错时发
                    "notify_ask": True,  # 在等你回答时发
                    "update_skip": "",   # 用户说「这个版本先不提醒」的那个版本号
                    "update_pending": "",   # 一键更新正在更新到的版本（重启后核对成没成）
                    "hidden": False,     # 藏在托盘里没有（下次启动接着藏）
                    "pinned": {},        # 选了窗口发起的对话：会话 id -> 发起时选的项目路径（窗口开的可能是项目的上级文件夹）
                    "assigned": {}}      # 右键「归到项目」手动指定的：会话 id -> 项目路径（盖过自动判断）
        if not (self.demo or self.shot):
            os.makedirs(install.TARGET, exist_ok=True)
            try:
                with open(CONFIG, encoding="utf-8") as f:
                    self.cfg.update(json.load(f))
            except (OSError, ValueError):
                pass
        self.views = []
        self.history = []               # 最近几天、现在没在跟踪的对话
        self.scroll = 0                 # 列表往上滚了多少（100% 缩放下的像素）
        self.last_hit = None            # 上一次单击点中的东西（分辨「双击」和「快速点了两个不同的东西」）
        self.recent_all = False         # 「最近 24 小时」里是不是全部显示（否则只显示前 RECENT_ROWS 条）
        self.events = queue.Queue()     # 采集线程发现的事件，界面线程来取
        self.unread = self.cfg["unread"]   # 已结束、还没点掉的会话（存的是结束那一刻的样子），新的在前
        self.ws_map = None              # 上次写进 SESSIONS 的内容
        self.reg = None if self.demo else workspaces.Registry()
        self.runner = None if self.demo else runner.Runner(
            self.cfg["permission_mode"], lambda sid, text: self.events.put(("failed", {"id": sid, "text": text}, True)))
        self.web = None if self.demo or self.shot else web.Web(self, log)
        self.version, self.log = VERSION, log   # 给弹出来的窗口用
        self.tray = None                # 托盘图标（假数据模式、截图模式下没有；加不上时也是 None）
        self.tray_q = queue.Queue()     # 托盘图标上点了什么（托盘自己的线程放进来，界面线程来取）
        self.hidden = False             # 藏在托盘里了
        self.close_ask = None           # 点 ✕ 时问「收到托盘还是退出」的那个窗口
        self.flash_since = None         # 任务完成的闪光是从什么时候开始的；不在闪是 None
        self.glow = self.glow_c = self.glow_hwnd = None   # 画闪光的那层透明窗口（第一次闪的时候才建）
        self.usage = demo_usage(time.time()) if self.demo else None   # Claude 的用量（usage.fetch 的结果），后台线程更新
        self.usage_wake = threading.Event()   # 点用量那一行：叫后台线程马上再查
        self.update_info = None         # 有新版本时是它的 {version, notes, link}（updates.check 的结果），后台线程更新
        self.ready = self.demo
        self.anim = []
        self.fit_cache = {}
        self.drag = None
        self.size = (0, 0)

        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except (AttributeError, OSError):
            pass
        self.root = root = tk.Tk()
        root.report_callback_exception = lambda *a: log("".join(traceback.format_exception(*a)))
        self.s = float(args[args.index("--scale") + 1]) if "--scale" in args else root.winfo_fpixels("1i") / 96.0
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        root.attributes("-alpha", self.cfg["alpha"] / 100)
        root.attributes("-transparentcolor", KEY)
        root.configure(bg=KEY)
        self.c = tk.Canvas(root, bg=KEY, highlightthickness=0, bd=0)
        self.c.pack(fill="both", expand=True)

        def font(px, bold=False):
            return tkfont.Font(family=FONT, size=-round(px * self.s), weight="bold" if bold else "normal")
        self.f_title, self.f_body, self.f_small = font(13, True), font(12), font(11)

        self.v_live = tk.BooleanVar(value=self.cfg["live_only"])
        self.v_alpha = tk.IntVar(value=self.cfg["alpha"])
        self.v_rule, self.v_auto = tk.BooleanVar(), tk.BooleanVar()
        self.menu = menu = tk.Menu(root, tearoff=0, postcommand=self.sync_menu)
        menu.add_checkbutton(label="只看在干活的", variable=self.v_live, command=self.on_option)
        self.v_flash = tk.BooleanVar(value=self.cfg["flash_on_done"])
        menu.add_checkbutton(label="任务完成时闪光提醒", variable=self.v_flash, command=self.on_option)
        alpha = tk.Menu(menu, tearoff=0)
        for a in (100, 90, 80, 65):
            alpha.add_radiobutton(label="%d%%" % a, value=a, variable=self.v_alpha, command=self.on_option)
        menu.add_cascade(label="不透明度", menu=alpha)
        menu.add_command(label="收起 / 展开", command=self.toggle)
        menu.add_command(label="回到右上角", command=self.reset_pos)
        menu.add_command(label="已结束的全部标为已读", command=self.mark_all_read)
        menu.add_separator()
        menu.add_command(label="发起任务…", command=self.compose, state="disabled" if self.demo else "normal")
        menu.add_command(label="手机访问…", command=lambda: dialogs.WebWindow(self), state="normal" if self.web else "disabled")
        menu.add_command(label="反馈给作者…", command=self.feedback)
        menu.add_separator()
        system = "disabled" if self.demo else "normal"   # 假数据模式下不让碰真实环境
        menu.add_checkbutton(label="显示步数（往 CLAUDE.md 加进度规则）", variable=self.v_rule, command=self.on_rule, state=system)
        menu.add_checkbutton(label="开机自动启动", variable=self.v_auto, command=self.on_auto, state=system)
        menu.add_separator()
        if install.FROZEN:   # 脚本版用「卸载.bat」
            menu.add_command(label="卸载…", command=self.uninstall, state=system)
        self.v_close = tk.StringVar(value=self.cfg["close_action"])
        close = tk.Menu(menu, tearoff=0)
        for value, label in (("", "每次问我"), ("tray", "收到托盘"), ("quit", "直接退出")):
            close.add_radiobutton(label=label, value=value, variable=self.v_close, command=self.on_option)
        menu.add_cascade(label="点 ✕ 时", menu=close, state=system)
        menu.add_command(label="收到托盘", command=self.hide, state=system)
        menu.add_command(label="退出", command=self.quit)
        menu.add_command(label="%s %s" % (NAME, VERSION), state="disabled")

        self.c.bind("<ButtonPress-1>", self.on_press)
        self.c.bind("<B1-Motion>", self.on_move)
        self.c.bind("<ButtonRelease-1>", self.on_release)
        self.c.bind("<Double-Button-1>", self.on_double)
        self.c.bind("<Button-3>", self.on_right)
        self.c.bind("<MouseWheel>", self.on_wheel)

        if self.cfg["x"] is None or not self.on_screen(self.cfg["x"], self.cfg["y"]):
            self.cfg["x"], self.cfg["y"] = self.default_pos()
        if not self.demo:
            threading.Thread(target=self.worker, daemon=True).start()
        self.refresh()
        self.tick()
        root.update_idletasks()
        self.hwnd = USER.GetAncestor(root.winfo_id(), 2)   # 最外层那个窗口
        self.keep_on_top()
        if self.shot:
            root.after(3500, self.take_shot)
        elif not self.demo:
            threading.Thread(target=self.usage_loop, daemon=True).start()
            threading.Thread(target=self.update_loop, daemon=True).start()
            self.check_updated()
            self.tray = tray.Tray(TRAY_COLORS, self.tray_q.put)
            if not self.tray.start():
                log("托盘图标没加上，点 ✕ 就直接退出")
                self.tray = None
            elif self.cfg["hidden"]:   # 上次是藏在托盘里关的（比如关机），开机启动后接着藏着
                self.hide()
            if "--autostart" in args:
                install.set_autostart(args[args.index("--autostart") + 1] == "on")
            elif install.autostart() not in (None, self.launch_line()) and not self.in_temp():
                install.set_autostart(True)   # 程序被挪了地方：让开机启动指到现在的位置
            root.after(600, self.first_run)
            if self.cfg["web_on"]:
                self.web.start()
            if "--web" in args:
                root.after(2500, lambda: dialogs.WebWindow(self))
            if "--compose" in args:
                root.after(2500, self.compose)
            if "--update-now" in args:   # 测试用：启动后马上查新版本，有就直接一键更新（不开窗口）
                root.after(1500, self.update_now)
            if "--send-feedback" in args:   # 测试用：不开窗口直接发一条，看打包后的程序能不能连上接收端
                text = args[args.index("--send-feedback") + 1]
                threading.Thread(target=lambda: log("测试反馈：%s %s" % feedback.send(feedback.compose(
                    text, "测试", VERSION, feedback.context(VERSION, self.s)))), daemon=True).start()
        if "--feedback" in args:
            root.after(2500, self.feedback)
        if "--exit-after" in args:
            root.after(int(float(args[args.index("--exit-after") + 1]) * 1000), root.destroy)

    # ---------- 进度规则 / 开机启动 / 卸载 ----------
    @staticmethod
    def launch_line():
        return ('"%s" %s' % install.launch_command()).strip()

    @staticmethod
    def in_temp():
        """exe 是不是在临时目录里跑（多半是在压缩包里直接双击的），这种位置不能登记成开机启动。"""
        return install.FROZEN and os.path.normcase(sys.executable).startswith(os.path.normcase(tempfile.gettempdir()))

    def confirm(self, text):
        if "--yes" in self.args or "--no" in self.args:
            return "--yes" in self.args
        return messagebox.askyesno(NAME, text, parent=self.root)

    def first_run(self):
        """exe 版没有安装步骤，第一次运行时在这里问要不要加进度规则。"""
        if not install.FROZEN or self.cfg.get("rule_asked"):
            return
        self.cfg["rule_asked"] = True
        self.save()
        if not install.has_rule() and self.confirm(FIRST_RUN % install.RULE_FILE):
            install.add_rule()

    def sync_menu(self):
        self.v_rule.set(install.has_rule())
        self.v_auto.set(install.autostart() is not None)

    def on_rule(self):
        if self.v_rule.get():
            install.add_rule()
            messagebox.showinfo(NAME, "已在\n%s\n末尾加了进度规则，对新开的对话生效。" % install.RULE_FILE, parent=self.root)
        elif not install.remove_rule():
            messagebox.showinfo(NAME, "%s\n里的进度规则不是悬浮窗加的那种写法，没法自动删。\n请手动删掉里面「进度标记」那一段。" % install.RULE_FILE,
                                parent=self.root)

    def on_auto(self):
        if self.v_auto.get() and self.in_temp():
            messagebox.showwarning(NAME, "这个 exe 现在在临时目录里（多半是在压缩包里直接双击的）。\n\n"
                                         "先把它解压到一个固定的位置，从那里运行，再开启开机启动。", parent=self.root)
        else:
            install.set_autostart(self.v_auto.get())

    def uninstall(self):
        if self.confirm(UNINSTALL):
            self.gone = True
            install.cleanup()
            self.root.destroy()

    # ---------- 数据 ----------
    def worker(self):
        col = collector.Collector(self.reg)
        history = collector.History(self.reg, self.cfg["history_days"])
        events = hub.Hub()
        while True:
            try:
                self.runner.tick()
                col.owned, col.running, col.exited = self.runner.owned(), self.runner.active(), dict(self.runner.exited)
                col.pinned, col.assigned = dict(self.cfg["pinned"]), dict(self.cfg["assigned"])
                views = col.poll()
                starting = self.runner.starting({v["id"] for v in views})
                if starting:   # 刚起的任务还没有会话记录：先顶一张「正在启动」，并且每轮都重新找文件，不等 5 秒一次的扫描
                    col.next_scan = 0
                    live = sum(1 for v in views if v["state"] in LIVE)
                    views[live:live] = [collector.placeholder(sid, t, self.reg, time.time()) for sid, t in starting]
                for event in events.update(views):
                    self.events.put(event)
                past = history.recent({s.sid for s in col.sessions.values()} | {v["id"] for v in views})
                for v in past:
                    col.place(v)
                self.views, self.history = views, past
                self.ready = True
            except Exception:
                log(traceback.format_exc())
            time.sleep(1.0)

    def on_event(self, kind, v, notify):
        """采集线程报上来的事件。finished：记进「已结束」列表，留到用户点掉为止；failed：后台任务没跑起来，也记进去；
        read：在手机上看过了（网页服务的线程报上来，界面上的东西只能在这个线程里动）。"""
        if kind == "read":
            self.mark_read(v["id"])
            return
        if kind == "failed":
            reason, task, now = v["text"], self.runner.tasks.get(v["id"], {}), time.time()
            v = collector.placeholder(v["id"], dict({"ws": "", "prompt": "", "created": now}, **task), self.reg, now)
            v.update(state="error", title="没跑起来：" + v["title"], last_text=reason)
            kind = "finished"
        if kind == "finished":
            key = "%s@%d" % (v["id"], v["end"])
            if key not in self.cfg["seen"]:
                snap = {k: v[k] for k in ("id", "title", "ws", "ws_color", "ws_path", "owned", "state", "start", "end", "prompt")}
                snap.update(key=key, last_text=v["last_text"][:2000], root=v.get("root", ""), timed=v.get("timed", True))
                rest = [u for u in self.unread if u["id"] != v["id"]]
                self.unread[:] = sorted([snap] + rest, key=lambda u: -u["end"])[:20]
                self.save()
            # 悬浮窗开着以后新结束的（notify 为真；启动时就已经结束的不算），做完了或者出错了就闪；用户自己打断的不闪
            if notify and v["state"] in ("done", "error") and self.cfg["flash_on_done"]:
                self.start_flash()
        # 手机通知（用户在「手机访问…」里配了自己的群机器人才发）
        state = "ask" if kind == "asking" else v.get("state")
        if notify and self.cfg.get("notify_url") and (state in ("done", "error") and self.cfg["notify_done"]
                                                      or state == "ask" and self.cfg["notify_ask"]):
            self.push(state, v)

    def push(self, kind, v, url=None):
        """往用户自己的群机器人发一条通知（后台线程，不卡界面）。开着手机访问的话，带上直接打开这个对话的链接。"""
        link = "http://%s:%d/#/c/%s" % (web.lan_ip(), self.web.port, v["id"]) if self.web and self.web.server else ""
        text, to = notify.message(kind, v, link), url or self.cfg["notify_url"]

        def work():
            ok, message = notify.send(to, text)
            if not ok:
                log("手机通知：%s" % message)

        threading.Thread(target=work, daemon=True).start()

    # ---------- 任务完成时的闪光 ----------
    def start_flash(self):
        """开始闪：藏在托盘里就先拿出来。已经在闪就接着闪。"""
        if self.hidden:
            self.show()
        if self.flash_since is None:
            self.flash_since = time.time()
            self.flash_tick()

    def flash_tick(self):
        """闪光自己的动画循环：扫光那一小会儿每 FLASH_FRAME 毫秒画一帧，扫完就睡到下一次开扫，不白费。"""
        if self.flash_since is None:
            return
        now = time.time()
        self.draw_flash(now)
        t = (now - self.flash_since) % FLASH_EVERY
        wait = FLASH_FRAME if t < FLASH_SWEEP else max(FLASH_FRAME, int((FLASH_EVERY - t) * 1000))
        self.root.after(wait, self.flash_tick)

    def stop_flash(self):
        self.flash_since = None
        if self.glow:
            self.glow_c.delete("all")

    def make_glow(self):
        """闪光画在一个盖在悬浮窗上面的透明小窗口里：整个窗口半透明（-alpha），所以光带是真的半透明、看得见底下的字；
        没画的地方是透明色，鼠标穿透（WS_EX_TRANSPARENT），不抢焦点、不进任务栏，点击照样落到悬浮窗上。
        建一次就一直留着，不闪的时候里面是空的（完全看不见）。"""
        g = tk.Toplevel(self.root)
        g.overrideredirect(True)
        g.attributes("-topmost", True)
        g.attributes("-alpha", FLASH_ALPHA)
        g.attributes("-transparentcolor", KEY)
        g.configure(bg=KEY)
        c = tk.Canvas(g, bg=KEY, highlightthickness=0, bd=0)
        c.pack(fill="both", expand=True)
        g.update_idletasks()
        hwnd = USER.GetAncestor(g.winfo_id(), 2)
        USER.SetWindowLongW(hwnd, -20, USER.GetWindowLongW(hwnd, -20) | 0x20 | 0x80 | 0x08000000)   # 穿透、工具窗口、不激活
        self.glow, self.glow_c, self.glow_hwnd = g, c, hwnd

    def draw_flash(self, now):
        """一道斜着的细光从左上角扫到右下角，每 FLASH_EVERY 秒一次，直到用户点一下悬浮窗。
        只画在圆角框里面；样子见 FLASH_SHAPE。"""
        if self.flash_since is None or self.hidden:
            if self.glow:
                self.glow_c.delete("all")
            return
        w, h = self.size
        if not w or not h:
            return
        if not self.glow:
            self.make_glow()
        c = self.glow_c
        c.delete("all")
        t = (now - self.flash_since) % FLASH_EVERY
        if t > FLASH_SWEEP:
            return
        r = wintypes.RECT()
        USER.GetWindowRect(self.hwnd, ctypes.byref(r))
        if self.glow.winfo_geometry() != "%dx%d+%d+%d" % (w, h, r.left, r.top):
            self.glow.geometry("%dx%d+%d+%d" % (w, h, r.left, r.top))
        USER.SetWindowPos(self.glow_hwnd, -1, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010)   # 压在悬浮窗上面；不挪、不改大小、不抢焦点
        length = sum(width for width, _ in FLASH_SHAPE) * self.s
        d = (w + h + length) * (t / FLASH_SWEEP)   # 光的最前沿在 x + y = d 这条斜线上，从左上角外面扫到右下角外面
        frame = rounded(1, 1, w - 1, h - 1, 12 * self.s)
        for width, color in FLASH_SHAPE:
            back = d - width * self.s
            strip = [(back, 0), (d, 0), (d - h, h), (back - h, h)]
            inside = clip(strip, frame)
            if len(inside) >= 3:
                c.create_polygon([v for p in inside for v in p], fill=color, outline="")
            d = back

    def mark_read(self, sid):
        keys = [u["key"] for u in self.unread if u["id"] == sid]
        if keys:
            self.unread[:] = [u for u in self.unread if u["id"] != sid]
            self.cfg["seen"] = (self.cfg["seen"] + keys)[-300:]
            self.save()
            self.render()

    def mark_all_read(self):
        self.cfg["seen"] = (self.cfg["seen"] + [u["key"] for u in self.unread])[-300:]
        del self.unread[:]
        self.save()
        self.render()

    def sync(self):
        """每次刷新前：收事件；又开工了的对话从「已结束」里拿掉；把「对话 -> 项目名」写给完成通知用。"""
        while not self.events.empty():
            self.on_event(*self.events.get())
        live = {v["id"] for v in self.views if v["state"] in LIVE}
        if any(u["id"] in live for u in self.unread):
            self.unread[:] = [u for u in self.unread if u["id"] not in live]
            self.save()
        ws_map = {u["id"]: u["ws"] for u in self.unread}
        ws_map.update((v["id"], v["ws"]) for v in self.views)
        if ws_map != self.ws_map and not (self.demo or self.shot or self.gone):
            self.ws_map = ws_map
            try:
                with open(SESSIONS, "w", encoding="utf-8") as f:
                    json.dump(ws_map, f, ensure_ascii=False)
            except OSError:
                pass

    # ---------- 对话 / 后台任务 ----------
    def find(self, sid):
        """一个对话现在的样子：正在跟踪的最新，其次是历史记录，再次是结束那一刻存下来的。"""
        return next((v for src in (self.views, self.history, self.unread) for v in src if v["id"] == sid), None)

    def open_session(self, sid):
        """跳到编辑器里的这个对话（输入框就在眼前），顺手标为已读。"""
        v = self.find(sid)
        log("点击：%s（%s）" % (sid, v["title"] if v else "列表里找不到这个对话"))
        if self.demo or not v:
            return
        self.mark_read(sid)
        runner.open_in_editor(sid, v.get("root") or v.get("ws_path") or "")

    def stop_task(self, sid):
        if messagebox.askyesno(NAME, "停掉这个后台任务？它做到一半的改动会留在原地。", parent=self.root):
            self.runner.stop(sid)

    def on_right(self, e):
        """右键：在某个对话上是这个对话的菜单，别的地方是整个悬浮窗的菜单。"""
        if self.flash_since is not None:
            self.stop_flash()
        sid = next((t[5:] for t in self.c.gettags("current") if t.startswith("open:")), None)
        v = self.find(sid) if sid else None
        if not v or self.demo:
            self.menu.tk_popup(e.x_root, e.y_root)
            return
        m = tk.Menu(self.root, tearoff=0)
        m.add_command(label="在编辑器里打开", command=lambda: self.open_session(sid))
        if v.get("owned") and v["state"] in LIVE:
            m.add_command(label="停止这个后台任务…", command=lambda: self.stop_task(sid))
        if any(u["id"] == sid for u in self.unread):
            m.add_command(label="标为已读", command=lambda: self.mark_read(sid))
        m.add_cascade(label="归到项目", menu=self.assign_menu(m, sid, v))
        m.tk_popup(e.x_root, e.y_root)

    def assign_menu(self, parent, sid, v):
        """「归到项目」子菜单：清单里的每个项目一项（当前归属打勾）；「选别的文件夹…」= 清单里没有的；
        最后一项「自动判断」= 撤掉手动指定。"""
        sub = tk.Menu(parent, tearoff=0)
        mine = self.cfg["assigned"].get(sid)
        now = workspaces.norm(mine or v.get("ws_path") or "")
        for it in sorted(self.reg.items, key=lambda it: it["name"].lower()):
            mark = "✓ " if it["key"] == now else "　 "
            sub.add_command(label=mark + it["name"], command=lambda p=it["path"]: self.assign_session(sid, p))
        sub.add_separator()
        sub.add_command(label="　 选别的文件夹…", command=lambda: self.assign_folder(sid, v))
        sub.add_command(label=("　 " if mine else "✓ ") + "自动判断", command=lambda: self.assign_session(sid, None))
        return sub

    def assign_folder(self, sid, v):
        """「选别的文件夹…」：项目还没进清单（没从那儿启动过对话）时，挑一个文件夹，加进清单再归过去。"""
        start = v.get("ws_path") or v.get("root") or os.path.expanduser("~")
        path = filedialog.askdirectory(parent=self.root, title="把这个对话归到哪个文件夹", initialdir=start, mustexist=True)
        if path:
            self.assign_session(sid, self.reg.add(os.path.normpath(path))["path"])

    def assign_session(self, sid, path):
        """手动把对话归到某个项目（path 为空 = 撤掉，回到自动判断）。「已结束」里存着的那份也跟着改，
        改之前记下它原来的项目，撤掉时改回去。"""
        assigned = self.cfg["assigned"]
        assigned.pop(sid, None)
        ws = self.reg.match(path) if path else None
        if path:
            assigned[sid] = path
            for old in list(assigned)[:-200]:
                del assigned[old]
        for u in self.unread:
            if u["id"] != sid:
                continue
            if ws:
                u.setdefault("auto_ws", {k: u.get(k) for k in ("ws", "ws_path", "ws_color")})
                u.update(ws=ws["name"], ws_path=ws["path"], ws_color=ws["color"])
            elif not path and "auto_ws" in u:
                u.update(u.pop("auto_ws"))
        log("归到项目：%s -> %s" % (sid, path or "自动判断"))
        self.save()
        self.render()

    def on_wheel(self, e):
        self.scroll = max(0, self.scroll - e.delta / 120 * 60)
        self.render()

    # ---------- 关闭 / 托盘 ----------
    def on_close(self):
        """点了 ✕：按记住的选择办；没记住就问一句。托盘图标没加上时藏起来就找不回来了，直接退出。"""
        action = self.cfg.get("close_action") or ""
        if not self.tray or action == "quit":
            self.quit()
        elif action == "tray":
            self.hide()
        elif not (self.close_ask and self.close_ask.winfo_exists()):
            self.close_ask = dialogs.CloseAsk(self)
        else:
            self.close_ask.lift()

    def hide(self):
        """藏到托盘里：悬浮窗不显示，别的照常（看着各个对话、手机访问、后台任务）。"""
        if not self.tray or self.hidden:
            return
        self.root.withdraw()
        self.hidden, self.tray.shown, self.cfg["hidden"] = True, False, True
        self.save()

    def show(self):
        """从托盘里拿出来（藏着的话），再挪回屏幕上、顶到最上面。"""
        if self.hidden:
            self.root.deiconify()
            self.hidden, self.cfg["hidden"] = False, False
            if self.tray:
                self.tray.shown = True
            self.save()
        self.come_back()

    def quit(self):
        self.root.destroy()   # 托盘图标在 run() 里收尾时拿掉

    def on_tray(self, cmd):
        if cmd == "show" or cmd == "toggle" and self.hidden:   # 左键单击只叫出来，不藏起；藏起走菜单或 ✕
            self.show()
        elif cmd == "toggle":
            self.hide()
        elif cmd == "compose":
            self.compose()
        elif cmd == "feedback":
            self.feedback()
        elif cmd == "quit":
            self.quit()

    def compose(self):
        if not self.demo:
            dialogs.Compose(self)

    def feedback(self):
        dialogs.Feedback(self)

    def start_task(self, ws, prompt, origin="hud"):
        sid = self.runner.start(ws["path"], prompt, origin)
        self.cfg["last_ws"] = ws["path"]
        self.save()
        return sid

    def pin_session(self, sid, ws):
        """记下在编辑器窗口里发起的对话属于哪个项目：它还没碰项目里的文件时，悬浮窗也能把它归对。"""
        pinned = self.cfg["pinned"]
        pinned[sid] = ws["path"]
        for old in list(pinned)[:-200]:
            del pinned[old]
        self.cfg["last_ws"] = ws["path"]
        self.save()

    def screen_left(self):
        return ctypes.windll.user32.GetSystemMetrics(76)

    # ---------- 窗口位置 / 配置 ----------
    def default_pos(self):
        return self.root.winfo_screenwidth() - round((W + 24) * self.s), round(90 * self.s)

    def on_screen(self, x, y):
        m = ctypes.windll.user32.GetSystemMetrics
        left, top = m(76), m(77)
        return left <= x <= left + m(78) - 80 and top <= y <= top + m(79) - 40

    def keep_on_top(self):
        """置顶只设一次不保险：锁屏、UAC 弹窗之后，窗口可能掉到普通窗口下面，而「置顶」标记还在。
        所以每秒看一眼：上面压着看得见的非置顶窗口，就重新要求置顶。顺带响应「再启动一次」留下的 SHOW。"""
        self.root.after(1000, self.keep_on_top)
        above = None if self.hidden else USER.GetWindow(self.hwnd, 3)   # GW_HWNDPREV：叠在上面的那一个；藏着的时候不管
        while above:
            if USER.IsWindowVisible(above) and not USER.GetWindowLongW(above, -20) & 0x8:   # WS_EX_TOPMOST
                self.raise_window()
                break
            above = USER.GetWindow(above, 3)
        if not self.demo and os.path.exists(SHOW):
            try:
                os.remove(SHOW)
            except OSError:
                pass
            self.show()

    def raise_window(self):
        USER.SetWindowPos(self.hwnd, -1, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010)   # HWND_TOPMOST；不改大小、不移动、不抢焦点

    def come_back(self):
        """用户又启动了一次，多半是找不到窗口了：跑到屏幕外面就挪回右上角，再顶到最上面。
        位置直接问系统、直接让系统挪，tkinter 自己记的位置不知道窗口被外部动过。"""
        r = wintypes.RECT()
        USER.GetWindowRect(self.hwnd, ctypes.byref(r))
        if not self.on_screen(r.left, r.top):
            self.cfg["x"], self.cfg["y"] = self.default_pos()
            self.save()
        USER.SetWindowPos(self.hwnd, -1, self.cfg["x"], self.cfg["y"], 0, 0, 0x0001 | 0x0010)   # 置顶并放回原位；不改大小、不抢焦点

    def save(self):
        if self.demo or self.shot or self.gone:
            return
        try:
            with open(CONFIG, "w", encoding="utf-8") as f:
                json.dump(self.cfg, f)
        except OSError:
            pass

    def place(self, w, h):
        self.size = (w, h)
        self.root.geometry("%dx%d+%d+%d" % (w, h, self.cfg["x"], self.cfg["y"]))

    def on_option(self):
        self.cfg["live_only"], self.cfg["alpha"] = self.v_live.get(), self.v_alpha.get()
        self.cfg["close_action"] = self.v_close.get()
        self.cfg["flash_on_done"] = self.v_flash.get()
        if not self.cfg["flash_on_done"]:
            self.stop_flash()
        self.root.attributes("-alpha", self.cfg["alpha"] / 100)
        self.save()
        self.render()

    def toggle(self):
        self.cfg["collapsed"] = not self.cfg["collapsed"]
        self.save()
        self.render()

    def reset_pos(self):
        self.cfg["x"], self.cfg["y"] = self.default_pos()
        self.save()
        self.place(*self.size)

    def on_press(self, e):
        if self.flash_since is not None:   # 在闪：点一下就停（这一下照常算数，该拖就拖、该点就点）
            self.stop_flash()
        # 按下的东西可能同时带几种标记（比如「已读」的勾画在整行里面），按这个顺序取最具体的那个
        tags = self.c.gettags("current")
        hit = next((t for kind in ("btn_", "tab:", "proj:", "list:", "open:") for t in tags if t.startswith(kind)), "")
        self.drag = {"dx": e.x_root - self.cfg["x"], "dy": e.y_root - self.cfg["y"], "moved": False, "hit": hit}

    def on_move(self, e):
        d = self.drag
        if not d:
            return
        x, y = e.x_root - d["dx"], e.y_root - d["dy"]
        if d["moved"] or abs(x - self.cfg["x"]) + abs(y - self.cfg["y"]) > 3:
            d["moved"] = True
            self.cfg["x"], self.cfg["y"] = x, y
            self.place(*self.size)

    def on_double(self, e):
        """双击：在对话上就跳到编辑器里的它。两次单击挨得太近也会被当成双击——点的是同一个东西（比如双击了项目名）
        就只算一下；点的是两个不同的东西（比如连着展开两个项目），第二下照普通单击处理，不吞掉。"""
        tags = self.c.gettags("current")
        sid = next((t[5:] for t in tags if t.startswith("open:")), None)
        hit = next((t for kind in ("btn_", "tab:", "proj:", "list:") for t in tags if t.startswith(kind)), "")
        if sid and not hit and (self.last_hit or "").startswith("open:"):
            self.open_session(sid)   # 两下都点在对话上才算：第一下点的东西改变了画面时，第二下落到的对话不能算
        elif hit and hit != self.last_hit:
            self.on_press(e)   # 接下来的松开会按单击处理它

    def on_release(self, e):
        d, self.drag = self.drag, None
        if not d:
            return
        hit = d["hit"]
        self.last_hit = None if d["moved"] else hit
        if d["moved"]:
            self.save()
        elif hit == "btn_close":
            self.on_close()
        elif hit == "btn_fold":
            self.toggle()
        elif hit == "btn_new":
            self.compose()
        elif hit == "btn_feedback":
            self.feedback()
        elif hit == "btn_usage":
            self.usage_wake.set()
        elif hit == "btn_update" and self.update_info:
            dialogs.UpdateWindow(self, self.update_info)
        elif hit.startswith("tab:"):   # 换分栏
            self.cfg["tab"], self.scroll = hit[4:], 0
            self.save()
            self.render()
        elif hit.startswith("proj:"):   # 展开 / 收起一个项目
            self.cfg["ws_open"][hit[5:]] = not self.cfg["ws_open"].get(hit[5:], False)
            self.save()
            self.render()
        elif hit == "list:more":        # 「最近 24 小时」里全部显示 / 只看前几条
            self.recent_all = not self.recent_all
            self.render()
        # 单击对话什么都不做，双击才跳过去（on_double）：单击太容易误触，按住拖窗口时也常常按在对话上

    # ---------- 画 ----------
    def fit(self, text, font, maxw):
        """把 text 截到 maxw（100% 缩放下的像素）以内，截了就加省略号。"""
        key = (text, id(font), int(maxw))
        hit = self.fit_cache.get(key)
        if hit is not None:
            return hit
        limit = maxw * self.s
        out = text
        if font.measure(text) > limit:
            lo, hi = 0, len(text)
            while lo < hi:
                mid = (lo + hi + 1) // 2
                if font.measure(text[:mid] + "…") <= limit:
                    lo = mid
                else:
                    hi = mid - 1
            out = text[:lo].rstrip() + "…"
        if len(self.fit_cache) > 3000:
            self.fit_cache.clear()
        self.fit_cache[key] = out
        return out

    def width(self, text, font):
        return font.measure(text) / self.s

    def text(self, x, y, text, font, fill, anchor="w", **kw):
        return self.c.create_text(x, y, text=text, font=font, fill=fill, anchor=anchor, **kw)

    def rr(self, x0, y0, x1, y1, r, **kw):
        return self.c.create_polygon(round_points(x0, y0, x1, y1, r), smooth=True, **kw)

    def refresh(self):
        self.sync()
        self.render()
        self.root.after(500, self.refresh)

    def render(self):
        c = self.c
        c.delete("all")
        self.anim = []
        now = time.time()
        folded = self.cfg["collapsed"]
        w = W_FOLDED if folded else W
        live = [v for v in self.views if v["state"] in LIVE]
        unread = {u["id"] for u in self.unread}
        quota = 0 if folded or not self.usage else USAGE_H   # Claude 用量那一行（查不到用量就没有）
        fresh = 0 if folded or not self.update_info else UPDATE_H   # 「有新版本」那一行
        top = 34 if folded else 62 + fresh + quota   # 标题栏、新版本、用量、分栏按钮；这一块不跟着滚

        # 先画列表（可能比窗口高），再按滚动量整体挪上去，最后在上面盖住标题栏和底边
        back = self.rr(0, 0, w, 10, 12, fill=C["panel"], outline="")
        y = top
        if not folded:
            if not self.ready:
                self.text(PAD + 4, y + 10, "正在读会话记录…", self.f_body, C["faint"])
                y += 26
            elif self.cfg["tab"] == "projects":
                groups = self.groups()
                if not groups:
                    self.text(PAD + 4, y + 10, "最近 %d 天没有对话" % self.cfg["history_days"], self.f_body, C["faint"])
                    y += 26
                for g in groups:
                    y = self.group(g, y, w, now, unread)
            else:
                y = self.recent(live, y, w, now, unread)
        full = y + 6
        h = min(full, self.max_height())
        self.scroll = max(0, min(self.scroll, full - h))
        if self.scroll:
            c.move("all", 0, -self.scroll)
            c.move(back, 0, self.scroll)
            self.anim = [(k, i, [b[0], b[1] - self.scroll, b[2], b[3] - self.scroll] if b else b, x) for k, i, b, x in self.anim]
        self.rr(1, 1, w - 1, 30, 11, fill=C["panel"], outline="")
        c.create_rectangle(1, 14, w - 1, top, fill=C["panel"], outline="")
        done = [u for u in self.unread if not self.cfg["live_only"]]
        self.header(w, live, done)
        if self.tray:
            parts, state = self.summary(live, done)
            extra = [usage.line(self.usage), "有新版本 %s" % self.update_info["version"] if self.update_info else ""]
            self.tray.update(state, "\n".join([NAME, " · ".join(parts) or "空闲"] + [x for x in extra if x]))
        if not folded:
            if fresh:
                self.update_row(w, 34)
            if quota:
                self.usage_row(w, 34 + fresh)
            self.tabs(w, 34 + fresh + quota)
        if full > h:   # 内容比窗口高：盖住底边，右边画一条滚动位置
            c.create_rectangle(1, h - 12, w - 1, h - 6, fill=C["panel"], outline="")
            self.rr(1, h - 12, w - 1, h - 1, 11, fill=C["panel"], outline="")
            track = h - top - 14
            c.create_rectangle(w - 5, top + track * self.scroll / full, w - 3, top + track * (self.scroll + h) / full,
                               fill=C["track"], outline="")
        c.coords(back, *round_points(0.5, 0.5, w - 0.5, h - 0.5, 12))
        # 描边最后画、盖在最上面；它中间虽然是空的，tkinter 仍然把整块当成它的地盘，不设 disabled 会把所有点击都吃掉
        self.rr(0.5, 0.5, w - 0.5, h - 0.5, 12, fill="", outline=C["edge"], state="disabled")
        c.scale("all", 0, 0, self.s, self.s)
        self.anim = [(kind, item, [v * self.s for v in box], extra) for kind, item, box, extra in self.anim]
        self.animate(now)
        size = (round(w * self.s), round(h * self.s))
        if size != self.size:
            self.place(*size)
        self.draw_flash(now)   # 整个重画会把闪光也删掉，马上补上，免得闪一下断一下

    def max_height(self):
        return ctypes.windll.user32.GetSystemMetrics(1) / self.s * MAX_HEIGHT

    def ended_items(self):
        """已经结束的对话（不在干活的），同一个对话几处都有时取最新的：结束时存下的 < 历史记录 < 正在跟踪的。"""
        if self.cfg["live_only"]:
            return []
        live = {v["id"] for v in self.views if v["state"] in LIVE}
        ended = {}
        for v in list(self.unread) + list(self.history) + [v for v in self.views if v["state"] not in LIVE]:
            ended[v["id"]] = v
        return [v for v in ended.values() if v["id"] not in live]

    @staticmethod
    def when(v):
        """一个对话最近一次有动静的时间：在干活的看最后一条记录，结束了的看结束时间。"""
        return v.get("last", v["end"]) if v["state"] in LIVE or v["state"] == "stale" else v["end"]

    def groups(self):
        """按项目分组：[{name, color, live: [在干活的], done: [结束了的], items: [全部，最近有动静的在前]}]。
        有在干活的项目排在前面（按它最早那个任务开始的时间，位置稳定），其余按最近一次有动静的时间。"""
        live = [v for v in self.views if v["state"] in LIVE]
        out = {}
        for v in live + self.ended_items():
            g = out.setdefault(v["ws"], {"name": v["ws"], "color": v["ws_color"], "live": [], "done": []})
            g["live" if v["state"] in LIVE else "done"].append(v)
        for g in out.values():
            g["items"] = sorted(g["live"] + g["done"], key=lambda v: -self.when(v))
        return sorted(out.values(), key=lambda g: (0, min(v["start"] for v in g["live"])) if g["live"]
                      else (1, -self.when(g["items"][0])))

    def tabs(self, w, y0):
        """标题栏下面的两个分栏按钮：「最近 24 小时」和「按项目」。y0：这一行从哪开始。"""
        x = PAD + 4
        for key, label in (("recent", "最近 24 小时"), ("projects", "按项目")):
            on = self.cfg["tab"] == key
            tw = self.width(label, self.f_body)
            self.c.create_rectangle(x - 6, y0 + 2, x + tw + 6, y0 + 26, fill=C["panel"], outline="", tags=("tab:" + key,))
            self.text(x, y0 + 13, label, self.f_body, C["text"] if on else C["faint"], tags=("tab:" + key,))
            if on:
                self.c.create_rectangle(x, y0 + 23, x + tw, y0 + 25, fill=C["run"], outline="", tags=("tab:" + key,))
            x += tw + 22
        self.c.create_line(PAD, y0 + 26, w - PAD, y0 + 26, fill=C["edge"])

    def usage_row(self, w, y0):
        """标题栏下面一行 Claude 的用量（和 /usage 里的一样）：5 小时和本周两项，各一个小进度条，快满了变黄、变红。
        点这一行马上重新查一次。"""
        info, cy, tags = self.usage, y0 + 11, ("btn_usage",)
        self.c.create_rectangle(PAD, y0, w - PAD, y0 + USAGE_H - 2, fill=C["panel"], outline="", tags=tags)
        if info.get("error"):
            self.text(PAD + 4, cy, self.fit(info["error"], self.f_small, w - 2 * PAD - 8), self.f_small, C["faint"], tags=tags)
            return
        col = (w - 2 * PAD) / 2
        for i, u in enumerate(info["limits"][:2]):
            x0 = PAD + 4 + i * col
            pct = max(0.0, min(100.0, u["percent"]))
            color = C["error"] if pct >= 90 else C["ask"] if pct >= 70 else C["run"]
            self.text(x0, cy, u["label"], self.f_small, C["dim"], tags=tags)
            bx = x0 + self.width(u["label"], self.f_small) + 6
            self.rr(bx, cy - 3, bx + 44, cy + 3, 3, fill=C["track"], outline="", tags=tags)
            if pct > 0:
                self.rr(bx, cy - 3, bx + max(6, 44 * pct / 100), cy + 3, 3, fill=color, outline="", tags=tags)
            tail = "%d%%" % round(pct) + ("  %s 重置" % usage.when(u["resets"]) if u["resets"] else "")
            self.text(bx + 50, cy, self.fit(tail, self.f_small, x0 + col - bx - 56), self.f_small, C["dim"] if pct < 70 else color, tags=tags)

    def update_row(self, w, y0):
        """标题栏下面一行「有新版本」，点它看更新说明、去下载。"""
        tags, cy = ("btn_update",), y0 + 11
        self.c.create_rectangle(PAD, y0 + 1, w - PAD, y0 + UPDATE_H - 3, fill=mix(C["panel"], C["done"], 0.16), outline="", tags=tags)
        label = "有新版本 %s（你现在是 %s），点这里看看更新了什么" % (self.update_info["version"], VERSION)
        self.text(PAD + 8, cy, self.fit(label, self.f_small, w - 2 * PAD - 16), self.f_small, C["done"], tags=tags)

    def apply_update(self, root, version):
        """一键更新的最后一步：起更新脚本，记下「正在更新到哪个版本」，悬浮窗退出；脚本换好文件会把新版本打开。"""
        updates.launch(root)
        self.cfg["update_pending"] = version
        self.save()
        log("开始更新到 %s：更新脚本已经起来，悬浮窗退出" % version)
        self.quit()

    def update_now(self):
        """测试用（--update-now）：查新版本，有安装包就下载、核对、交给更新脚本，和窗口里点「立即更新」走同一条路。"""
        info = updates.check(VERSION)
        if not info or not info.get("package"):
            log("测试更新：没查到带安装包的新版本")
            return
        try:
            self.apply_update(updates.prepare(updates.download(info), info["version"]), info["version"])
        except (feedback.AppError, OSError) as e:
            log("测试更新：没成 %s" % e)

    def check_updated(self):
        """启动时看看上次的一键更新成没成（记在 update_pending 里），记一笔日志就清掉。成了就删掉下载的包；
        没成的话留着，好查原因。"""
        pending = self.cfg.get("update_pending")
        if not pending:
            return
        if pending == VERSION:
            log("已经更新到 %s" % VERSION)
            updates.cleanup()
        else:
            log("要更新到 %s，但现在还是 %s：换文件可能没成，见 %s" % (pending, VERSION, os.path.join(updates.STAGE, "update.log")))
        self.cfg["update_pending"] = ""
        self.save()

    def update_loop(self):
        """后台线程：启动后过一会儿查一次有没有新版本，之后每隔几小时查一次（见 updates.py）。"""
        time.sleep(updates.FIRST)
        while True:
            try:
                self.update_info = updates.check(VERSION, self.cfg.get("update_skip") or "")
            except Exception:
                log(traceback.format_exc())
            time.sleep(updates.EVERY)

    def usage_loop(self):
        """后台线程：每 5 分钟查一次 Claude 的用量；点用量那一行会马上再查。"""
        while True:
            try:
                self.usage = usage.fetch()
            except Exception:
                log(traceback.format_exc())
            self.usage_wake.wait(usage.EVERY)
            self.usage_wake.clear()

    def recent(self, live, y, w, now, unread):
        """「最近 24 小时」：上面是在干活的（卡片），下面是 24 小时内结束的（一行一个），新的在前。"""
        done = sorted([v for v in self.ended_items() if now - self.when(v) <= 86400], key=lambda v: -self.when(v))
        if not (live or done):
            self.text(PAD + 4, y + 14, "现在没有在跑的，24 小时内也没有结束的对话", self.f_body, C["faint"])
            return y + 30
        y += 6
        for v in sorted(live, key=lambda v: v["start"]):
            y = self.card(v, y, w, now, chip=True)
        if done:
            self.text(PAD + 4, y + 10, "24 小时内结束 · %d 条" % len(done), self.f_small, C["faint"])
            y += 22
            shown = done if self.recent_all else done[:RECENT_ROWS]
            for v in shown:
                y = self.row(v, y, w, now, v["id"] in unread, chip=True)
            if len(done) > RECENT_ROWS:   # 多了先收着，点这一行全部显示 / 再收起来
                label = "收起，只看最近 %d 条 ▴" % RECENT_ROWS if self.recent_all else "再显示 %d 条 ▾" % (len(done) - RECENT_ROWS)
                self.c.create_rectangle(PAD - 4, y, w - PAD + 4, y + 24, fill=C["panel"], outline="", tags=("list:more",))
                self.text(w // 2, y + 12, label, self.f_small, C["run"], anchor="center", tags=("list:more",))
                y += 26
        return y

    def group(self, g, y, w, now, unread):
        """「按项目」里的一个项目：标题行（点它展开 / 收起），展开后是这个项目的全部对话，一行一个，最近有动静的在前。"""
        name, x0, x1 = g["name"], PAD, w - PAD
        news = [d for d in g["done"] if d["id"] in unread]
        need = sum(1 for v in g["live"] if v["state"] in ("ask", "confirm"))
        opened = self.cfg["ws_open"].get(name, False)
        tag = ("proj:" + name,)
        self.c.create_rectangle(x0 - 4, y, x1 + 4, y + 28, fill=C["panel"], outline="", tags=tag)
        self.text(x0 + 2, y + 14, "▾" if opened else "▸", self.f_small, C["dim"], tags=tag)
        self.c.create_oval(x0 + 16, y + 10, x0 + 24, y + 18, fill=g["color"], outline="", tags=tag)
        parts = [(t, c) for t, c in (("%d 在跑" % (len(g["live"]) - need), C["run"]), ("%d 等你" % need, C["ask"]),
                                     ("%d 新结束" % len(news), C["done"])) if not t.startswith("0 ")]
        parts = parts or [("%d 条记录" % len(g["done"]), C["faint"])]
        x = x1
        for i, (label, color) in enumerate(reversed(parts)):   # 右对齐，从右往左摆
            label = label + ("  " if i else "")
            self.text(x, y + 14, label, self.f_small, color, anchor="e", tags=tag)
            x -= self.width(label, self.f_small)
        self.text(x0 + 30, y + 14, self.fit(name, self.f_title, x - x0 - 40), self.f_title, g["color"], tags=tag)
        y += 30
        if not opened:
            return y
        for v in g["items"][:MAX_DONE]:
            y = self.row(v, y, w, now, v["id"] in unread)
        if len(g["items"]) > MAX_DONE:
            self.text(x0 + 18, y + 9, "…还有 %d 条" % (len(g["items"]) - MAX_DONE), self.f_small, C["faint"])
            y += 19
        return y + 6

    @staticmethod
    def summary(live, done):
        """标题栏和托盘图标共用的状态：（「3 个在干活 · 1 个等你」这样的几段，ask / run / done / idle）。"""
        need = sum(1 for v in live if v["state"] in ("ask", "confirm"))
        parts = []
        if len(live) - need:
            parts.append("%d 个在干活" % (len(live) - need))
        if need:
            parts.append("%d 个等你" % need)
        if done:
            parts.append("%d 个结束未读" % len(done))
        return parts, "ask" if need else "run" if live else "done" if done else "idle"

    def header(self, w, live, done):
        parts, state = self.summary(live, done)
        self.c.create_oval(PAD + 4, 13, PAD + 12, 21, fill=TRAY_COLORS[state], outline="")
        self.text(PAD + 20, 17, "Agents", self.f_title, C["text"])
        x0 = PAD + 28 + self.width("Agents", self.f_title)
        self.text(x0, 17, self.fit(" · ".join(parts) or "空闲", self.f_small, w - PAD - 116 - x0), self.f_small,
                  C["ask"] if state == "ask" else C["dim"])
        buttons = (("btn_close", "✕", w - PAD - 12, 11, self.f_body), ("btn_fold", "▾" if self.cfg["collapsed"] else "▴", w - PAD - 36, 11, self.f_body),
                   ("btn_new", "＋", w - PAD - 60, 11, self.f_body), ("btn_feedback", "反馈", w - PAD - 91, 17, self.f_small))
        for tag, label, x, half, f in buttons:
            self.c.create_rectangle(x - half, 6, x + half, 28, fill=C["panel"], outline="", tags=(tag,))
            self.text(x, 17, label, f, C["dim"], anchor="center", tags=(tag,))
        return 34

    def chip(self, x, cy, label, color, back, maxw=120, **kw):
        """一个带底色的小标签，返回它右边缘的 x。"""
        label = self.fit(label, self.f_small, maxw)
        x1 = x + self.width(label, self.f_small) + 12
        self.rr(x, cy - 9, x1, cy + 9, 5, fill=mix(back, color, 0.2), outline="", **kw)
        self.text(x + 6, cy, label, self.f_small, color, **kw)
        return x1

    def card(self, v, y, w, now, chip=False):
        x0, x1 = PAD, w - PAD
        ix0, ix1 = x0 + 10, x1 - 10
        color = C[v["state"]]
        waiting = v["state"] in ("ask", "confirm")
        back = self.rr(x0, y, x1, y + 10, 8, fill=C["card"], outline="")
        top = y
        y += 8

        # 第一行：状态点、项目、（后台任务的标记、）标题、这件事已经跑了多久
        elapsed = fmt_dur(now - v["start"])
        self.c.create_oval(ix0, y + 6, ix0 + 8, y + 14, fill=color, outline="")
        self.text(ix1, y + 10, elapsed, self.f_small, C["dim"], anchor="e")
        x = ix0 + 14
        if chip:   # 「按项目」里项目已经写在标题行上了，卡片上不再重复
            x = self.chip(x, y + 10, v["ws"], v["ws_color"], C["card"]) + 6
        if v["owned"]:
            x = self.chip(x, y + 10, "后台", C["dim"], C["card"]) + 6
        self.text(x, y + 10, self.fit(v["title"], self.f_title, ix1 - x - 10 - self.width(elapsed, self.f_small)),
                  self.f_title, C["text"])
        y += 23

        # 第二行：有进度标记就写第几步，没有就写它最近说的一句话
        m = v["marker"]
        line = ("第 %d/%d 步" % m[:2] + (" · " + m[2] if m[2] else "")) if m else v["line"]
        if line:
            self.text(ix0, y + 8, self.fit(line, self.f_body, ix1 - ix0), self.f_body, color if m else C["dim"])
            y += 20

        self.bar(v, ix0, y + 3, ix1, y + 8, color)
        y += 14

        # 第四行：此刻在做的动作
        right = "%d 步" % v["steps"]
        self.text(ix1, y + 8, right, self.f_small, C["faint"], anchor="e")
        self.text(ix0, y + 8, self.fit("▸ " + v["action"], self.f_small,
                                       ix1 - ix0 - 12 - self.width(right, self.f_small)),
                  self.f_small, color if waiting else C["dim"])
        y += 19

        subs = v["subs"]
        if subs:
            y = self.subs(subs, ix0, ix1, y + 3, now)
        y += 5
        self.c.coords(back, *round_points(x0, top, x1, y, 8))
        for item in self.c.find_all():   # 整张卡片都能点：跳到编辑器里的这个对话
            if item >= back:
                self.c.addtag_withtag("open:" + v["id"], item)
        return y + 6

    def subs(self, subs, x0, x1, y, now):
        running = [s for s in subs if s["state"] == "run"]
        done = [s for s in subs if s["state"] == "done"]
        bad = [s for s in subs if s["state"] not in ("run", "done")]
        self.c.create_line(x0, y, x1, y, fill=C["track"])
        y += 5
        name_w = (x1 - x0) * 0.42
        for s in running[:MAX_SUB]:
            elapsed = fmt_dur(now - s["start"])
            self.c.create_oval(x0 + 1, y + 6, x0 + 7, y + 12, fill=C["run"], outline="")
            self.text(x0 + 13, y + 9, self.fit(s["desc"], self.f_small, name_w), self.f_small, C["text"])
            self.text(x1, y + 9, elapsed, self.f_small, C["faint"], anchor="e")
            self.text(x0 + 21 + name_w, y + 9,
                      self.fit(s["action"], self.f_small, x1 - x0 - 29 - name_w - self.width(elapsed, self.f_small)),
                      self.f_small, C["dim"])
            y += 18
        if len(running) > MAX_SUB:
            self.text(x0 + 13, y + 9, "…还有 %d 个在跑" % (len(running) - MAX_SUB), self.f_small, C["faint"])
            y += 18
        for group, mark, word, color in ((done, "✓", "干完", C["done"]), (bad, "✕", "没干完", C["error"])):
            if group:
                head = "%s %s %d/%d" % (mark, word, len(group), len(subs))
                self.text(x0, y + 9, head, self.f_small, color)
                hw = self.width(head, self.f_small)
                self.text(x0 + hw + 8, y + 9, self.fit("、".join(s["desc"] for s in group), self.f_small, x1 - x0 - hw - 8),
                          self.f_small, C["faint"])
                y += 18
        return y

    def bar(self, v, x0, y0, x1, y1, color):
        c = self.c
        m, subs = v["marker"], v["subs"]
        if m and m[1] <= 30:      # 分步的活：做完的格子实心，正在做的那格呼吸
            cells = [("done" if i < m[0] - 1 else "now" if i == m[0] - 1 else "todo") for i in range(m[1])]
        elif m:
            cells = None
        elif subs:                # 派了子 agent：一个子 agent 一格
            cells = [{"done": "done", "run": "now", "error": "bad"}.get(s["state"], "off") for s in subs]
        else:
            cells = None
        if cells:
            gap = 3 if len(cells) <= 16 else 1
            cw = (x1 - x0 - gap * (len(cells) - 1)) / len(cells)
            for i, kind in enumerate(cells):
                a = x0 + i * (cw + gap)
                fill = {"done": color, "now": color, "bad": C["error"], "off": C["faint"]}.get(kind, C["track"])
                item = c.create_rectangle(a, y0, a + cw, y1, fill=fill, outline="")
                if kind == "now":
                    self.anim.append(("pulse", item, [], color))
            return
        c.create_rectangle(x0, y0, x1, y1, fill=C["track"], outline="")
        if m:                     # 步数太多画不成格子，就画成一条
            c.create_rectangle(x0, y0, x0 + (x1 - x0) * (m[0] - 0.5) / m[1], y1, fill=color, outline="")
        elif v["state"] in ("ask", "confirm"):
            c.create_rectangle(x0, y0, x1, y1, fill=mix(C["track"], color, 0.55), outline="")
        else:                     # 不知道一共几步：只表示“还在动”
            item = c.create_rectangle(x0, y0, x0, y1, fill=color, outline="")
            self.anim.append(("slide", item, [x0, y0, x1, y1], None))

    def row(self, v, y, w, now, new, chip=False):
        """列表里的一个对话，一行：状态记号、（项目、）标题、时间；双击跳到编辑器里。
        new：结束了还没看过的，标题亮一点、前面有个绿点。chip：带项目标签（「最近 24 小时」里用）。"""
        x0, x1 = PAD + 18, w - PAD - 4
        state = v["state"] if v["state"] in C else "stale"
        live = state in LIVE
        if live:
            mark = "●"
            m = v.get("marker")
            right = ("第 %d/%d 步" % tuple(m[:2]) if m else {"ask": "等你回答", "confirm": "可能等你确认", "subs": "等子 agent"}.get(state, "在跑"))
            right += " · " + fmt_dur(now - v["start"])
        elif state == "done":
            mark, right = "✓", fmt_ago(now - v["end"])
            if v.get("timed", True) and v["end"] > v["start"]:   # 只读了文件末尾、没看到任务开头的，不写用时
                right += " · 用时 " + fmt_dur(v["end"] - v["start"])
        elif state == "stale":
            mark, right = "…", "没收尾 · %s" % fmt_ago(now - v["last"])
        else:
            mark = "✕" if state == "error" else "■"
            right = "%s · %s" % ("出错了" if state == "error" else "被打断", fmt_ago(now - v["end"]))
        tag = ("open:" + v["id"],)
        self.c.create_rectangle(PAD - 4, y, w - PAD + 4, y + 21, fill=C["panel"], outline="", tags=tag)
        if new:
            self.c.create_oval(x0 - 12, y + 7, x0 - 6, y + 13, fill=C["done"], outline="", tags=tag)
        self.text(x0, y + 10, mark, self.f_small, C[state], tags=tag)
        self.text(x1, y + 10, right, self.f_small, C["faint"], anchor="e", tags=tag)
        x = x0 + 16
        if chip:
            x = self.chip(x, y + 10, v["ws"], v["ws_color"], C["panel"], maxw=110, tags=tag) + 6
        if v.get("owned"):
            x = self.chip(x, y + 10, "后台", C["dim"], C["panel"], tags=tag) + 6
        self.text(x, y + 10, self.fit(v["title"], self.f_small, x1 - x - 10 - self.width(right, self.f_small)),
                  self.f_small, C["text"] if new or live else C["dim"], tags=tag)
        return y + 21

    def animate(self, now):
        for kind, item, box, color in self.anim:
            if kind == "pulse":
                self.c.itemconfigure(item, fill=mix(C["track"], color, 0.45 + 0.55 * (0.5 + 0.5 * math.sin(now * 5.2))))
            else:
                x0, y0, x1, y1 = box
                seg = (x1 - x0) * 0.28
                a = x0 - seg + (x1 - x0 + seg) * (now % 1.8 / 1.8)
                self.c.coords(item, max(x0, a), y0, min(x1, a + seg), y1)

    def tick(self):
        now = time.time()
        self.animate(now)
        while not self.tray_q.empty():
            self.on_tray(self.tray_q.get())
        self.root.after(40, self.tick)

    def take_shot(self):
        from PIL import ImageGrab
        m = ctypes.windll.user32.GetSystemMetrics
        x, y = self.cfg["x"] - m(76), self.cfg["y"] - m(77)
        ImageGrab.grab(bbox=(x, y, x + self.size[0], y + self.size[1]),
                       include_layered_windows=True, all_screens=True).save(self.shot)
        self.root.destroy()

    def run(self):
        if self.demo:
            now = time.time()
            views = demo_views(now)
            for v in views:
                if v["state"] in hub.ENDED and now - v["end"] < 600:   # 十分钟内结束的算「新结束」
                    self.on_event("finished", v, False)
            self.views = [v for v in views if v["state"] not in hub.ENDED]
            self.history = [v for v in views if v["state"] in hub.ENDED]
        try:
            self.root.mainloop()
        finally:
            if self.tray:   # 不拿掉的话，图标会在托盘里留到鼠标划过才消失
                self.tray.stop()


def acquire(wait_answer=3.0, wait_exit=15.0):
    """拿「只开一个」的锁，拿到返回锁的句柄（进程活着就一直攥着）；已经有一个好好开着的，返回 None。

    锁已经被占了：先留下 SHOW 请那一个把窗口亮出来，它每秒看一次，会把 SHOW 删掉。
    过了 wait_answer 秒还没人接，说明占着锁的只是个正在退出、已经没有窗口的空进程（比如刚被关掉），
    那就等它把锁放掉再自己开；不这样的话，关旧开新的时候两个都没了。"""
    name = "ClaudeAgentHud" + ("-test" if install.TEST_ROOT else "")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateMutexW.restype = wintypes.HANDLE
    mutex = kernel.CreateMutexW(None, False, name)
    if ctypes.get_last_error() != 183:
        return mutex
    try:
        open(SHOW, "w").close()
    except OSError:
        pass
    end = time.time() + wait_answer
    while time.time() < end and os.path.exists(SHOW):
        time.sleep(0.1)
    if not os.path.exists(SHOW):
        kernel.CloseHandle(mutex)
        return None
    end = time.time() + wait_exit
    while time.time() < end:
        kernel.CloseHandle(mutex)
        time.sleep(0.3)
        mutex = kernel.CreateMutexW(None, False, name)
        if ctypes.get_last_error() != 183:
            try:
                os.remove(SHOW)
            except OSError:
                pass
            return mutex
    kernel.CloseHandle(mutex)
    return None


def main():
    args = sys.argv[1:]
    install.clean_env()
    runner.log = log
    if not ("--demo" in args or "--shot" in args):
        mutex = acquire()  # noqa: F841  进程活着就得一直攥着它
        if mutex is None:
            return
    try:
        Hud(args).run()
    except Exception:
        log(traceback.format_exc())
        raise


if __name__ == "__main__":
    main()
