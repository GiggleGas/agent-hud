"""模拟点击：在一个看不见的悬浮窗（假数据、全透明）上点各种东西，看各自触发了什么。不动真鼠标，不弹看得见的窗口。"""
import importlib.machinery, importlib.util, os, sys, time

sys.stdout.reconfigure(encoding="utf-8")
APP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app")
sys.path.insert(0, APP)
loader = importlib.machinery.SourceFileLoader("agent_hud", os.path.join(APP, "agent_hud.pyw"))
spec = importlib.util.spec_from_loader("agent_hud", loader)
mod = importlib.util.module_from_spec(spec)
loader.exec_module(mod)
mod.RECENT_ROWS = 3   # 假数据里结束的不多，调小一点才测得到「再显示」

fails = []


def check(name, ok, detail=""):
    print(("  ok   " if ok else "  FAIL ") + name + ("" if ok else "  " + str(detail)))
    if not ok:
        fails.append(name)


h = mod.Hud(["--demo"])
h.root.attributes("-alpha", 0.0)
now = time.time()
views = mod.demo_views(now)
for v in views:
    if v["state"] in mod.hub.ENDED and now - v["end"] < 600:
        h.on_event("finished", v, False)
h.views = [v for v in views if v["state"] not in mod.hub.ENDED]
h.history = [v for v in views if v["state"] in mod.hub.ENDED]
opened = []
h.open_session = lambda sid: opened.append(sid)
h.render()
h.root.update()

# Capture the actual header inputs so hidden unread tasks cannot affect its count.
real_header, headers = h.header, []
def header(w, live, done):
    headers.append({v["id"] for v in done})
    return real_header(w, live, done)
h.header = header
codex_done = dict(h.history[0], id="codex:hidden", provider="codex")
h.unread.append(codex_done)
h.cfg["codex_on"] = False
h.render()
check("关闭 Codex 后标题栏不计入它的未读任务", "codex:hidden" not in headers[-1], headers[-1])
h.cfg["codex_on"] = True
h.render()
check("重新开启 Codex 后标题栏恢复未读计数", "codex:hidden" in headers[-1], headers[-1])
h.header = real_header
h.unread.remove(codex_done)
h.render()
h.root.update()


clock = [100000]   # 模拟事件的时间戳（毫秒）：每次单击隔 5 秒，免得被 tkinter 当成双击


def where(tag):
    items = h.c.find_withtag(tag)
    x0, y0, x1, y1 = h.c.bbox(items[-1])   # 最后画的那个在最上面
    if y1 > h.c.winfo_height() - 12 * h.s:
        # The expanded list can exceed the viewport; scroll before simulating a click.
        h.scroll += (y1 - h.c.winfo_height()) / h.s + 24
        h.render()
        h.root.update()
        x0, y0, x1, y1 = h.c.bbox(h.c.find_withtag(tag)[-1])
    x, y = (x0 + x1) // 2, (y0 + y1) // 2
    h.c.event_generate("<Motion>", x=x, y=y, rootx=h.root.winfo_rootx() + x, rooty=h.root.winfo_rooty() + y)
    h.root.update()
    return dict(x=x, y=y, rootx=h.root.winfo_rootx() + x, rooty=h.root.winfo_rooty() + y)


def click(tag, gap=5000):
    """在带 tag 的图形正中间单击一下，返回按下时认出来的东西。gap：离上一下多少毫秒。"""
    if not h.c.find_withtag(tag):
        return "（画面上没有 %s）" % tag
    at = where(tag)
    press(at, gap)
    return click.hit


def press(at, gap):
    clock[0] += gap
    h.c.event_generate("<Motion>", **at)
    h.root.update()
    h.c.event_generate("<ButtonPress-1>", time=clock[0], **at)
    click.hit = h.drag and h.drag["hit"]
    h.c.event_generate("<ButtonRelease-1>", time=clock[0] + 50, **at)
    h.root.update()


def double(tag, second=None):
    """双击：在 tag 上按一下，200 毫秒后再按一下——默认按在原地（和真人一样，哪怕第一下改变了画面），
    给了 second 就按在 second 上（模拟快速点了两个不同的东西）。"""
    at = where(tag)
    press(at, 5000)
    press(where(second) if second else at, 200)   # 只隔 200 毫秒：tkinter 自己会把它认成双击


def height():
    return h.root.winfo_height()


print("== 最近 24 小时")
check("默认在「最近 24 小时」", h.cfg["tab"] == "recent")
print("   单击卡片：", click("open:订单导出改成后台任务"))
check("单击对话不跳走", opened == [], opened)
double("open:订单导出改成后台任务")
check("双击在干活的卡片会打开那个对话", opened == ["订单导出改成后台任务"], opened)
double("open:升级依赖并修掉编译警告")
check("双击结束的记录会打开那个对话", opened[-1:] == ["升级依赖并修掉编译警告"] and len(opened) == 2, opened)
check("结束的记录只有一行（项目和标题），没有摘要和「更多」", not [t for i in h.c.find_all() for t in h.c.gettags(i) if t.startswith("more:")])
shown = lambda: [t[5:] for t in {t for i in h.c.find_all() for t in h.c.gettags(i)} if t.startswith("open:")]
n = len(shown())
print("   再显示：", click("list:more"))
check("「再显示」把收着的也列出来", len(shown()) > n, (n, len(shown())))
click("list:more")
check("再点一下收回去", len(shown()) == n)

print("== 按项目")
print("   分栏：", click("tab:projects"))
check("点「按项目」换过去", h.cfg["tab"] == "projects")
check("项目默认收着，列表里没有对话", not [t for i in h.c.find_all() for t in h.c.gettags(i) if t.startswith("open:")])
print("   项目：", click("proj:shop-server"))
rows = [t[5:] for i in h.c.find_all() for t in h.c.gettags(i) if t.startswith("open:")]
check("点项目名展开，在跑的和结束的在同一个列表里", {"订单导出改成后台任务", "登录接口加图形验证码", "给搜索接口加缓存"} <= set(rows), set(rows))
double("open:给搜索接口加缓存")
check("双击列表里的对话会打开它", opened[-1:] == ["给搜索接口加缓存"], opened)
click("proj:shop-server")
check("再点项目名收起", not [t for i in h.c.find_all() for t in h.c.gettags(i) if t.startswith("open:")])
double("proj:web-admin", "proj:ops-tools")
check("快速点两个不同的项目，两个都展开（第二下不会被当成双击吞掉）",
      h.cfg["ws_open"].get("web-admin") is True and h.cfg["ws_open"].get("ops-tools") is True, h.cfg["ws_open"])
click("tab:recent")
check("点回「最近 24 小时」", h.cfg["tab"] == "recent")
asked = []
h.feedback = lambda: asked.append(1)
print("   反馈按钮：", click("btn_feedback"))
check("标题栏的反馈按钮打开反馈窗口", asked == [1], asked)
print("   收起按钮：", click("btn_fold"))
check("右上角的按钮也点得到", h.cfg["collapsed"] is True)
h.root.destroy()
print("\n失败 %d 项：%s" % (len(fails), fails) if fails else "\n全部通过")
sys.exit(1 if fails else 0)
