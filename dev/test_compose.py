"""「发起任务」窗口的回归测试：在哪跑的默认选择、在编辑器窗口里发起（开头补上项目）、后台跑、没成时窗口回来。
窗口列表和「在编辑器里新开对话」都换成假的，不碰真的编辑器、不弹窗口。"""
import os, sys, tempfile, time

sys.stdout.reconfigure(encoding="utf-8")
BOX = tempfile.mkdtemp(prefix="agent-hud-compose-")
os.environ.update(AGENT_HUD_TEST_ROOT=BOX, CLAUDE_CONFIG_DIR=os.path.join(BOX, ".claude"))
APP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app")
sys.path.insert(0, APP)
import importlib.machinery, importlib.util
loader = importlib.machinery.SourceFileLoader("agent_hud", os.path.join(APP, "agent_hud.pyw"))
spec = importlib.util.spec_from_loader("agent_hud", loader)
mod = importlib.util.module_from_spec(spec)
loader.exec_module(mod)
import dialogs, runner, workspaces

fails = []


def check(name, ok, detail=""):
    print(("  ok   " if ok else "  FAIL ") + name + ("" if ok else "  " + str(detail)))
    if not ok:
        fails.append(name)


root = os.path.join(BOX, "Something")
for n in ("agent-hud", "shop"):
    os.makedirs(os.path.join(root, n))
h = mod.Hud(["--demo"])
h.root.withdraw()
h.reg = workspaces.Registry(os.path.join(BOX, "ws.json"))
for n in ("agent-hud", "shop"):
    h.reg._add(os.path.join(root, n))
hud_ws = h.reg.by_name("agent-hud")


class Runner:
    mode = "auto"

    def __init__(self):
        self.calls = []

    def start(self, path, text, origin):
        self.calls.append(("start", path, text))
        return "bg-id"


h.runner = Runner()
WINDOWS = {"agent-hud": [{"hwnd": 2, "scheme": "cursor", "label": "Something · Cursor", "folder": "Something", "depth": 1},
                         {"hwnd": 1, "scheme": "cursor", "label": "F1DEV1 · Cursor", "folder": "F1DEV1", "depth": None}],
           "shop": [{"hwnd": 3, "scheme": "vscode", "label": "shop · VS Code", "folder": "shop", "depth": 0},
                    {"hwnd": 2, "scheme": "cursor", "label": "Something · Cursor", "folder": "Something", "depth": 1}]}
runner.compose_windows = lambda p: WINDOWS[os.path.basename(p)]
sent, answer = [], [(True, "已经在编辑器里新开了一个对话，任务发出去了。", "new-id")]
keys_used = []


def fake_start(hwnd, scheme, text, use_key=True):   # 真的要十来秒；这里等一下，好看清发起时窗口让开了
    time.sleep(0.3)
    sent.append((hwnd, scheme, text))
    keys_used.append(use_key)
    return answer[0]


runner.start_in_editor = fake_start
has_key = {"cursor": True, "vscode": True}
runner.has_new_tab_key = lambda scheme: has_key[scheme]
added, asked, add_result = [], [], [(True, "已经加上了")]
runner.add_new_tab_key = lambda scheme: added.append(scheme) or add_result[0]
asks = []
dialogs.messagebox.askyesno = lambda title, text, parent=None: asks.append(text) or asked[0]
warnings = []
dialogs.messagebox.showwarning = lambda title, text, parent=None: warnings.append(text)


def finish(w):
    end = time.time() + 5
    while time.time() < end and w.winfo_exists() and str(w.button["state"]) == "disabled":
        h.root.update()
        time.sleep(0.05)
    h.root.update()


print("== 在哪跑")
h.cfg["last_ws"] = hud_ws["path"]
w = dialogs.Compose(h)
check("默认选开着这个项目上级文件夹的窗口", w.where.get() == "Something · Cursor", w.where.get())
check("下拉框里有开着的窗口和「后台跑」", list(w.where["values"]) == ["Something · Cursor", "F1DEV1 · Cursor", dialogs.Compose.BACKGROUND],
      w.where["values"])
check("说明写的是在编辑器里新开对话", "新开一个 Claude 对话" in str(w.note["text"]))
w.ws.set("shop")
w.ws.event_generate("<<ComboboxSelected>>")
h.root.update()
check("换项目：跟着换成开着那个项目的窗口", w.where.get() == "shop · VS Code", w.where.get())
w.where.set(dialogs.Compose.BACKGROUND)
w.where.event_generate("<<ComboboxSelected>>")
h.root.update()
check("选后台跑：说明跟着换", "后台跑" in str(w.note["text"]) and "auto" in str(w.note["text"]))
w.destroy()

print("== 在编辑器窗口里发起")
w = dialogs.Compose(h)
w.body.insert("1.0", "检查当前项目进度")
w.send()
check("发起时窗口让开，免得挡住编辑器", w.state() == "withdrawn")
finish(w)
check("只发写的原话，开头不加「在 xx 项目里」（用户定的）", sent[-1] == (2, "cursor", "检查当前项目进度"), sent[-1:])
check("成了：窗口关掉，记下这个对话属于选的项目", not w.winfo_exists() and h.cfg["pinned"].get("new-id") == hud_ws["path"], h.cfg["pinned"])
check("没走后台任务", h.runner.calls == [])
w = dialogs.Compose(h, ws_path=h.reg.by_name("shop")["path"])
w.body.insert("1.0", "加个缓存")
w.send()
finish(w)
check("窗口开的就是这个项目：原话发出去", sent[-1] == (3, "vscode", "加个缓存"), sent[-1:])

print("== 没成")
answer[0] = (False, "焦点被别的窗口抢走了，没有打字。", None)
w = dialogs.Compose(h)
w.body.insert("1.0", "再试一次")
w.send()
finish(w)
check("没成：窗口回来，说明原因，写的东西还在，按钮能再点", w.winfo_exists() and w.state() == "normal" and "抢走" in str(w.note["text"])
      and w.body.get("1.0", "end").strip() == "再试一次" and str(w.button["state"]) != "disabled", (w.state(), w.note["text"]))
w.destroy()

print("== 新开对话用的快捷键")
answer[0] = (True, "发出去了", "id-2")
check("编辑器里已经有快捷键：不问，直接用", keys_used and all(keys_used) and not asks)


def compose_once(text="再来一个"):
    w = dialogs.Compose(h, ws_path=hud_ws["path"])   # agent-hud：默认窗口是「Something · Cursor」
    w.body.insert("1.0", text)
    w.send()
    finish(w)
    return w


has_key["cursor"] = False
asked[:] = [True]
compose_once()
check("没有快捷键：问一次，说要就加上、用它", len(asks) == 1 and "Ctrl+Alt+Shift+F12" in asks[0] and added == ["cursor"] and keys_used[-1] is True,
      (asks, added, keys_used[-1:]))
asks.clear()
added.clear()
asked[:] = [False]
compose_once()
check("说不要：不加，这次改成填好让用户按回车", added == [] and keys_used[-1] is False and h.cfg["no_new_tab_key"] == ["cursor"])
asks.clear()
compose_once()
check("说过不要的：以后不再问", not asks and keys_used[-1] is False)
h.cfg["no_new_tab_key"] = []
asked[:] = [True]
add_result[0] = (False, "keybindings.json 的格式认不出来，没敢改")
compose_once()
check("加不上：说一声原因，这次改成填好让用户按回车", warnings and "认不出来" in warnings[-1] and keys_used[-1] is False, (warnings, keys_used[-1:]))
has_key["cursor"] = True
answer[0] = (None, "新对话开好了，要做的事也填进了输入框，去那个窗口按一下回车就发出去。", None)
w = compose_once()
check("填好了等用户按回车：窗口回来说一声（不算失败，不是红字），按钮不让再点（免得又开一个）",
      w.winfo_exists() and "按一下回车" in str(w.note["text"]) and str(w.note["foreground"]) != "#c33" and str(w.button["state"]) == "disabled",
      (w.note["text"], w.note["foreground"], w.button["state"]))
w.destroy()
answer[0] = (True, "发出去了", "id-3")

print("== 后台跑")
w = dialogs.Compose(h)
w.where.set(dialogs.Compose.BACKGROUND)
w.body.insert("1.0", "跑一遍测试")
w.send()
check("选后台跑：照旧发起后台任务（项目默认是上次用的 agent-hud）", h.runner.calls[-1] == ("start", hud_ws["path"], "跑一遍测试")
      and not w.winfo_exists(), h.runner.calls)
check("记住这次选的是后台跑", h.cfg["compose_where"] == "background")
w = dialogs.Compose(h)
check("下次打开默认还是后台跑", w.where.get() == dialogs.Compose.BACKGROUND, w.where.get())
w.destroy()
runner.compose_windows = lambda p: []
h.cfg["compose_where"] = "window"
w = dialogs.Compose(h)
check("一个编辑器窗口都没开：只能后台跑", list(w.where["values"]) == [dialogs.Compose.BACKGROUND] and w.where.get() == dialogs.Compose.BACKGROUND)
w.destroy()

print("== 右键「归到项目」")
ended = next(v for v in mod.demo_views(time.time()) if v["state"] in mod.hub.ENDED)
v = dict(ended, id="s-9", ws="agent-hud", ws_path=hud_ws["path"])
h.on_event("finished", v, False)
shop_ws = h.reg.by_name("shop")


def assign_labels():
    m = h.assign_menu(h.root, "s-9", v)
    return m, [m.entrycget(i, "label") for i in range(m.index("end") + 1) if m.type(i) == "command"]


def kept(sid="s-9"):
    return next(u for u in h.unread if u["id"] == sid)["ws"]


m, labels = assign_labels()
check("子菜单：每个项目一项、现在归属的打勾，然后「选别的文件夹…」，最后是「自动判断」（没手动指定时打勾）",
      labels == ["✓ agent-hud", "　 shop", "　 选别的文件夹…", "✓ 自动判断"], labels)
m.invoke(1)
check("选 shop：记进 assigned，「已结束」里存着的那份也改成 shop",
      h.cfg["assigned"].get("s-9") == shop_ws["path"] and kept() == "shop", (h.cfg["assigned"], h.unread[:1]))
m, labels = assign_labels()
check("再打开：shop 打勾、「自动判断」不打勾", labels == ["　 agent-hud", "✓ shop", "　 选别的文件夹…", "　 自动判断"], labels)
m.invoke(m.index("end"))
check("选「自动判断」：撤掉手动指定，「已结束」里那份改回原来的 agent-hud",
      "s-9" not in h.cfg["assigned"] and kept() == "agent-hud", (h.cfg["assigned"], kept()))

print("== 「选别的文件夹…」")
tools = os.path.join(BOX, "Elsewhere", "tools")
os.makedirs(tools)
picked = []


def fake_ask(**kw):
    picked.append(kw)
    return tools.replace("\\", "/")   # 对话框给的是正斜杠


mod.filedialog.askdirectory = fake_ask
m, labels = assign_labels()
m.invoke(labels.index("　 选别的文件夹…") + 1)   # 菜单的序号把分隔线也算一个
item = h.reg.by_name("tools")
check("挑的文件夹加进了项目清单，并且存到清单文件里",
      bool(item) and os.path.normcase(item["path"]) == os.path.normcase(tools) and "tools" in open(h.reg.file, encoding="utf-8").read(), h.reg.items)
check("对话归到了这个文件夹；对话框从对话现在的项目开始找",
      h.cfg["assigned"].get("s-9") == (item or {}).get("path") and kept() == "tools" and picked[-1].get("initialdir") == hud_ws["path"], (h.cfg["assigned"], kept(), picked))
mod.filedialog.askdirectory = lambda **kw: ""
m, labels = assign_labels()
m.invoke(labels.index("　 选别的文件夹…") + 1)
check("对话框点了取消：什么都不改", h.cfg["assigned"].get("s-9") == (item or {}).get("path") and len(h.reg.items) == 3, (h.cfg["assigned"], len(h.reg.items)))
m, labels = assign_labels()
m.invoke(m.index("end"))
h.assign_session("s-9", shop_ws["path"])
h.assign_session("s-9", (item or {}).get("path"))
h.assign_session("s-9", None)
check("连着换了两次再撤掉：回到最早自动判断的 agent-hud，不是中间选过的 shop", kept() == "agent-hud", kept())

print("== 右键「不用等后台了」")
bgv = next(x for x in mod.demo_views(time.time()) if x["state"] == "bg" and x["bg"])
flashes = []
h.start_flash = lambda: flashes.append(1)
h.stop_waiting(bgv["id"], bgv)
check("记下在等的后台命令；开着的服务器本来就不等，不用记", h.cfg["bg_ignored"], [b["id"] for b in bgv["bg"] if not b["resident"]])
before = len(h.unread)
h.on_event("finished", dict(bgv, state="done", last_text=""), True)
check("随后那次结束：不闪光、不进「已结束」（用户自己说的不用等）", (flashes, len(h.unread)), ([], before))
h.on_event("finished", dict(bgv, state="done", last_text="", end=bgv["end"] + 60), True)
check("以后真的又做完了：照常闪光、进「已结束」", (flashes, len(h.unread)), ([1], before + 1))
h.root.destroy()
print("\n失败 %d 项：%s" % (len(fails), fails) if fails else "\n全部通过")
sys.exit(1 if fails else 0)
