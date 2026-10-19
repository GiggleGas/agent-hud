"""悬浮窗弹出来的几个普通窗口：反馈、关闭时的询问、手机访问、发起任务。

悬浮窗本身没有边框、抢不到键盘，所以要打字的都放在这里的普通窗口里（中文输入法也正常）。
"""
import os
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk

import feedback
import notify
import runner
import updates

FONT = ("Microsoft YaHei UI", 10)
SMALL = ("Microsoft YaHei UI", 9)


class Window(tk.Toplevel):
    """共用的那部分：置顶、出现在悬浮窗旁边、Esc 关闭。"""

    def __init__(self, hud, title):
        super().__init__(hud.root)
        self.hud = hud
        self.title(title)
        self.attributes("-topmost", True)
        self.resizable(False, False)
        self.configure(padx=12, pady=10)
        self.bind("<Escape>", lambda e: self.destroy())

    def place_beside(self):
        """放到悬浮窗左边；左边放不下就放右边。"""
        self.update_idletasks()
        w, h = self.winfo_reqwidth(), self.winfo_reqheight()
        x = self.hud.cfg["x"] - w - 16
        if x < self.hud.screen_left():
            x = self.hud.cfg["x"] + self.hud.size[0] + 16
        self.geometry("+%d+%d" % (x, max(0, self.hud.cfg["y"])))

    def fail(self, error):
        messagebox.showerror(self.title(), str(error), parent=self)


class Feedback(Window):
    """反馈：写几句话，发到作者的飞书群；可以带图片（截图后粘贴，或者选文件）。
    群机器人发不了图片，所以图片在发送后放进剪贴板，让用户自己到飞书里粘贴给作者（见 feedback.py 开头）。"""

    THUMB = (150, 90)   # 缩略图最大多宽、多高

    def __init__(self, hud):
        super().__init__(hud, "反馈")
        ttk.Label(self, font=FONT, text="用着哪里别扭、出了什么问题、想要什么功能，都可以写。会发到作者的飞书。").grid(
            row=0, column=0, columnspan=2, sticky="w")
        self.body = tk.Text(self, width=58, height=8, wrap="word", font=FONT, undo=True)
        self.body.grid(row=1, column=0, columnspan=2, pady=(8, 4))
        pics = ttk.Frame(self)
        pics.grid(row=2, column=0, columnspan=2, sticky="w")
        ttk.Button(pics, text="添加图片…", command=self.pick).pack(side="left")
        ttk.Label(pics, font=SMALL, foreground="#666", justify="left", text=(
            "  也可以截图后（比如 Win+Shift+S）在上面的框里按 Ctrl+V。" + ("" if feedback.app_ready() else
            "\n  图片发不进群（群机器人只能发文字），发送后会帮你复制好，要你自己粘贴到飞书发给作者。"))).pack(side="left")
        self.thumbs = ttk.Frame(self)
        self.thumbs.grid(row=3, column=0, columnspan=2, sticky="w")
        ttk.Label(self, text="怎么称呼你", font=FONT).grid(row=4, column=0, sticky="w", pady=(8, 0))
        self.who = ttk.Entry(self, width=24, font=FONT)
        self.who.insert(0, hud.cfg.get("feedback_name") or feedback.user())
        self.who.grid(row=4, column=1, sticky="w", padx=(8, 0), pady=(8, 0))
        self.log = tk.BooleanVar(value=False)
        ttk.Checkbutton(self, variable=self.log, text="附上最近的运行日志（遇到问题时勾上，方便作者查）").grid(
            row=5, column=0, columnspan=2, sticky="w", pady=(6, 0))
        ttk.Label(self, font=SMALL, foreground="#666", text="会一起带上：你的称呼和电脑用户名、悬浮窗和系统的版本。").grid(
            row=6, column=0, columnspan=2, sticky="w")
        self.note = ttk.Label(self, font=SMALL, foreground="#666", justify="left", wraplength=self.body.winfo_reqwidth() or 470)
        self.note.grid(row=7, column=0, columnspan=2, sticky="w", pady=(6, 0))
        bar = ttk.Frame(self)
        bar.grid(row=8, column=0, columnspan=2, sticky="e", pady=(10, 0))
        self.folder_button = ttk.Button(bar, text="打开图片文件夹", command=lambda: os.startfile(self.folder))
        self.next_button = ttk.Button(bar, text="复制下一张", command=self.copy_next)
        self.button = ttk.Button(bar, text="发送（Ctrl+Enter）", command=self.send)
        self.button.pack(side="left", padx=4)
        self.close_button = ttk.Button(bar, text="取消", command=self.destroy)
        self.close_button.pack(side="left")
        self.body.bind("<Control-Return>", lambda e: self.send() or "break")
        self.body.bind("<<Paste>>", self.paste)
        self.images, self.folder, self.photos = [], None, []
        self.place_beside()
        self.body.focus_force()

    # ---------- 图片 ----------
    def ensure_folder(self):
        if not self.folder:
            self.folder = feedback.new_folder()
        return self.folder

    def paste(self, event=None):
        """剪贴板里是图片就收下（不往输入框里贴）；是文字就照常粘贴。"""
        try:
            added = feedback.paste_images(self.ensure_folder())
        except Exception as e:
            self.hud.log("粘贴图片出错：%r" % e)
            added = []
        if not added:
            return None
        self.images += added
        self.show_thumbs()
        return "break"

    def pick(self):
        from tkinter import filedialog
        paths = filedialog.askopenfilenames(parent=self, title="选图片", filetypes=[("图片", " ".join("*" + e for e in feedback.IMAGE_EXTS))])
        added = [p for p in (feedback.add_file(self.ensure_folder(), p) for p in paths) if p]
        if added:
            self.images += added
            self.show_thumbs()

    def drop(self, path):
        self.images.remove(path)
        self.show_thumbs()

    def show_thumbs(self):
        for w in self.thumbs.winfo_children():
            w.destroy()
        self.photos = []
        for i, path in enumerate(self.images):
            cell = ttk.Frame(self.thumbs, padding=(0, 6, 8, 0))
            cell.grid(row=0, column=i)
            photo = None
            if path.lower().endswith((".png", ".gif")):   # tkinter 自己只认这两种；别的格式只显示文件名
                try:
                    photo = tk.PhotoImage(master=self, file=path)
                    k = max(1, -(-photo.width() // self.THUMB[0]), -(-photo.height() // self.THUMB[1]))
                    photo = photo.subsample(k)
                except tk.TclError:
                    photo = None
            if photo:
                self.photos.append(photo)
                ttk.Label(cell, image=photo, relief="solid").pack()
            else:
                ttk.Label(cell, text=os.path.basename(path), font=SMALL, relief="solid", padding=6).pack()
            ttk.Button(cell, text="移除", width=4, command=lambda p=path: self.drop(p)).pack(pady=(2, 0))

    # ---------- 发送 ----------
    def send(self):
        text = self.body.get("1.0", "end").strip()
        if not text or str(self.button["state"]) == "disabled":
            self.bell()
            return
        if self.hud.demo:
            self.note.configure(text="假数据模式下不发送。")
            return
        who = self.who.get().strip()
        self.hud.cfg["feedback_name"] = who
        self.hud.save()
        body = feedback.compose(text, who, self.hud.version, feedback.context(self.hud.version, self.hud.s, self.log.get()))
        self.button.configure(state="disabled")
        self.note.configure(text="正在发…", foreground="#666")
        result, images = [], list(self.images)

        def work():   # 发消息要联网，放到后台线程，不卡窗口；结果由窗口自己来取（别的线程不碰界面）
            result.append(feedback.deliver(body, images))
            self.hud.log("反馈：%s %s（图片 %d 张，%s）" % ("发出去了" if result[0][0] else "没发出去", result[0][1], len(images),
                                                    "一起发了" if result[0][2] else "要自己贴"))

        threading.Thread(target=work, daemon=True).start()
        self.wait_for(result)

    def wait_for(self, result):
        if not self.winfo_exists():
            return
        if not result:
            self.after(100, self.wait_for, result)
            return
        ok, message, pictures_sent = result[0]
        if not self.images or pictures_sent:
            self.note.configure(text=message, foreground="#2a7" if ok else "#c33")
            if ok:
                self.after(1500, self.destroy)
            else:
                self.button.configure(state="normal")
            return
        # 有图片：一张张以位图放进剪贴板，窗口留着，告诉用户去飞书里粘贴
        self.head, self.next = "文字发出去了。" if ok else message, 0
        self.folder_button.pack(side="left", padx=4, before=self.button)
        if len(self.images) > 1:
            self.next_button.pack(side="left", padx=4, before=self.button)
        self.close_button.configure(text="关闭")
        if not ok:
            self.button.configure(state="normal")
        self.copy_next()

    def copy_next(self):
        i, n = self.next, len(self.images)
        self.next = (i + 1) % n
        if feedback.copy_image(self.images[i], self.winfo_id()):
            how = "图片还要你自己发：第 %d/%d 张已经复制好了，打开飞书里和作者（给你这个包的人）的对话，按 Ctrl+V 粘贴发出去。" % (i + 1, n)
            if n > 1:
                how += "粘贴完回来点「复制下一张」。" if i + 1 < n else "这是最后一张。"
        else:
            how = "第 %d 张图片没能放进剪贴板：点「打开图片文件夹」，把它拖进飞书发给作者（给你这个包的人）。" % (i + 1)
        self.note.configure(text=self.head + "\n" + how, foreground="#b45309")   # 还要用户动手，用醒目的颜色


class UpdateWindow(Window):
    """有新版本：版本号、更新说明、去下载；也可以这个版本先不提醒。"""

    def __init__(self, hud, info):
        super().__init__(hud, "有新版本")
        self.info = info
        ttk.Label(self, font=FONT, text="新版本 %s（你现在用的是 %s）" % (info["version"], hud.version)).grid(row=0, column=0, sticky="w")
        notes = tk.Text(self, width=56, height=10, wrap="word", font=FONT, relief="flat", background=self.cget("background"))
        notes.insert("1.0", info.get("notes") or "（作者没写更新说明）")
        notes.configure(state="disabled")
        notes.grid(row=1, column=0, pady=8)
        ttk.Label(self, font=SMALL, foreground="#666", justify="left", text=(
            "更新方法：exe 版退出悬浮窗，用新包里的 Agent悬浮窗.exe 和 _internal 文件夹覆盖旧的；\n"
            "脚本版解压新包后再运行一次「安装.bat」。设置都会保留。")).grid(row=2, column=0, sticky="w")
        self.note = ttk.Label(self, font=SMALL, foreground="#666", justify="left", wraplength=notes.winfo_reqwidth() or 470)
        self.note.grid(row=3, column=0, sticky="w", pady=(6, 0))
        bar = ttk.Frame(self)
        bar.grid(row=4, column=0, sticky="e", pady=(10, 0))
        self.buttons = []
        if info.get("package"):
            self.buttons.append(ttk.Button(bar, text="立即更新", command=self.update_now))
        if info.get("link"):
            self.buttons.append(ttk.Button(bar, text="去下载", command=self.download))
        self.buttons.append(ttk.Button(bar, text="这个版本先不提醒", command=self.skip))
        for b in self.buttons:
            b.pack(side="left", padx=4)
        ttk.Button(bar, text="关闭", command=self.destroy).pack(side="left")
        self.place_beside()

    def update_now(self):
        """一键更新：下载安装包、解压核对（后台线程），好了就交给更新脚本、悬浮窗退出，脚本换好文件再把新版本打开。"""
        if self.hud.demo:
            self.note.configure(text="假数据模式下不更新。")
            return
        for b in self.buttons:
            b.configure(state="disabled")
        state = {"done": 0, "total": (self.info.get("package") or {}).get("size") or 0, "result": None}

        def work():   # 结果和进度由窗口自己来取（别的线程不碰界面）
            try:
                path = updates.download(self.info, progress=lambda d, t: state.update(done=d, total=t))
                state["result"] = (True, updates.prepare(path, self.info["version"]))
            except (feedback.AppError, OSError) as e:
                state["result"] = (False, str(e))

        threading.Thread(target=work, daemon=True).start()
        self.watch(state)

    def watch(self, state):
        if not self.winfo_exists():
            return
        if state["result"] is None:
            mb = lambda n: "%.1f" % (n / 1048576)
            self.note.configure(text="正在下载… %s / %s MB" % (mb(state["done"]), mb(state["total"])) if state["total"] else "正在下载…",
                                foreground="#666")
            self.after(200, self.watch, state)
            return
        ok, value = state["result"]
        if not ok:
            self.note.configure(text="没更新成：%s。可以过会儿再试%s。" % (value, "，或者点「去下载」自己换" if self.info.get("link") else ""),
                                foreground="#c33")
            for b in self.buttons:
                b.configure(state="normal")
            return
        self.note.configure(text="下载好了。悬浮窗马上关掉、换上新版本再自己打开，十来秒。", foreground="#2a7")
        self.after(1500, lambda: self.hud.apply_update(value, self.info["version"]))

    def download(self):
        os.startfile(self.info["link"])   # 在浏览器（或者飞书）里打开下载链接
        self.destroy()

    def skip(self):
        self.hud.cfg["update_skip"] = self.info["version"]
        self.hud.update_info = None
        self.hud.save()
        self.hud.render()
        self.destroy()


class CloseAsk(Window):
    """点 ✕ 时问一句：收到托盘，还是退出。"""

    NOTES = ("·收到托盘：悬浮窗藏起来，照常在后台看着各个对话，手机访问也照常。\n"
             "  右下角托盘里的图标颜色跟着状态变，单击它悬浮窗就回来。\n"
             "  （第一次图标可能藏在任务栏右下角的 ^ 里，把它拖到外面就一直看得见。）\n"
             "·退出：悬浮窗和手机访问都停掉，正在跑的后台任务不受影响。")

    def __init__(self, hud):
        super().__init__(hud, "关闭悬浮窗")
        ttk.Label(self, font=FONT, text="收到右下角的托盘里，还是退出？").grid(row=0, column=0, sticky="w")
        ttk.Label(self, font=SMALL, foreground="#666", justify="left", text=self.NOTES).grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.remember = tk.BooleanVar(value=False)
        ttk.Checkbutton(self, variable=self.remember, text="记住我的选择，下次不再问（右键菜单「点 ✕ 时」里可以改）").grid(
            row=2, column=0, sticky="w", pady=(8, 0))
        bar = ttk.Frame(self)
        bar.grid(row=3, column=0, sticky="e", pady=(10, 0))
        ttk.Button(bar, text="收到托盘", command=lambda: self.pick("tray")).pack(side="left", padx=4)
        ttk.Button(bar, text="退出", command=lambda: self.pick("quit")).pack(side="left", padx=4)
        ttk.Button(bar, text="取消", command=self.destroy).pack(side="left")
        self.bind("<Return>", lambda e: self.pick("tray"))
        self.place_beside()
        self.focus_force()

    def pick(self, action):
        if self.remember.get():
            self.hud.cfg["close_action"] = action
            self.hud.v_close.set(action)
            self.hud.save()
        self.destroy()
        if action == "tray":
            self.hud.hide()
        else:
            self.hud.quit()


class WebWindow(Window):
    """手机访问：开关网页服务，显示地址和二维码；下面是手机通知（可选，用户自己配群机器人地址）。"""

    # 手动断好行：中文没有空格，交给 tkinter 自动折行会在「·」后面或者半句话中间断开
    NOTES = ("·手机和电脑要在同一个局域网（比如连同一个 Wi-Fi）。\n"
             "  第一次开启时，Windows 或杀毒软件可能弹出防火墙提示，选「允许」。\n"
             "·拿到这个地址的人能看到你的对话，也能让电脑替你打字，别发给别人。\n"
             "  怀疑泄露了就点「换一把钥匙」，旧地址立刻失效。\n"
             "·在手机上回复编辑器里的对话时，电脑会自动切到那个编辑器窗口替你打字，\n"
             "  所以电脑要开着、不能锁屏。")

    def __init__(self, hud):
        super().__init__(hud, "手机访问")
        self.on = tk.BooleanVar(value=bool(hud.cfg.get("web_on")))
        ttk.Checkbutton(self, text="开启手机访问", variable=self.on, command=self.toggle).grid(row=0, column=0, columnspan=2, sticky="w")
        self.qr = tk.Canvas(self, width=210, height=210, bg="white", highlightthickness=0)
        self.qr.grid(row=1, column=0, rowspan=3, pady=(8, 0))
        ttk.Label(self, text="用手机扫左边的码，或者打开这个地址：", font=FONT).grid(row=1, column=1, sticky="sw", padx=(14, 0))
        self.url = ttk.Entry(self, width=40, font=FONT)
        self.url.grid(row=2, column=1, sticky="w", padx=(14, 0))
        bar = ttk.Frame(self)
        bar.grid(row=3, column=1, sticky="nw", padx=(14, 0), pady=(6, 0))
        ttk.Button(bar, text="复制链接", command=self.copy).pack(side="left")
        ttk.Button(bar, text="换一把钥匙", command=self.rekey).pack(side="left", padx=6)
        ttk.Label(self, text=self.NOTES, font=SMALL, foreground="#666", justify="left").grid(
            row=4, column=0, columnspan=2, sticky="w", pady=(12, 0))
        self.note = ttk.Label(self, font=SMALL, foreground="#666")
        self.note.grid(row=5, column=0, columnspan=2, sticky="w")
        self.push_box(row=6)
        ttk.Button(self, text="关闭", command=self.close).grid(row=7, column=1, sticky="e", pady=(8, 0))
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.bind("<Escape>", lambda e: self.close())
        self.show()
        self.place_beside()

    # ---------- 手机通知（可选，用户自己配） ----------
    PUSH_HOW = ("任务做完、出错、或者在等你回答时，发一条消息到你自己的飞书（钉钉、企业微信也行），手机锁屏也会推送。\n"
                "开着手机访问的话，消息里带着直接打开那个对话的链接。不想要就空着，什么都不发。\n"
                "怎么配（以飞书为例，两分钟）：\n"
                "  1. 建一个你自己的通知群（别用别人也在的群，消息里有对话的标题和内容）。\n"
                "  2. 群设置 → 群机器人 → 添加机器人 → 自定义机器人；安全设置选「自定义关键词」，填「悬浮窗」。\n"
                "  3. 复制机器人的 webhook 地址，粘到下面，点「发一条测试」，手机上收到就好了。\n"
                "地址只存在这台电脑上，不会发给别人；消息会经过飞书（或钉钉、企业微信）。")

    def push_box(self, row):
        box = ttk.LabelFrame(self, text=" 手机通知（可选） ", padding=8)
        box.grid(row=row, column=0, columnspan=2, sticky="we", pady=(12, 0))
        ttk.Label(box, text=self.PUSH_HOW, font=SMALL, foreground="#666", justify="left").grid(row=0, column=0, columnspan=3, sticky="w")
        ttk.Label(box, text="群机器人地址", font=FONT).grid(row=1, column=0, sticky="w", pady=(8, 0))
        self.push_url = ttk.Entry(box, width=52, font=FONT)
        self.push_url.insert(0, self.hud.cfg.get("notify_url") or "")
        self.push_url.grid(row=1, column=1, columnspan=2, sticky="w", padx=(8, 0), pady=(8, 0))
        self.push_done = tk.BooleanVar(value=self.hud.cfg.get("notify_done", True))
        self.push_ask = tk.BooleanVar(value=self.hud.cfg.get("notify_ask", True))
        ttk.Checkbutton(box, text="做完 / 出错时", variable=self.push_done, command=self.save_push).grid(row=2, column=0, sticky="w", pady=(6, 0))
        ttk.Checkbutton(box, text="在等你回答时", variable=self.push_ask, command=self.save_push).grid(row=2, column=1, sticky="w", padx=(8, 0), pady=(6, 0))
        ttk.Button(box, text="发一条测试", command=self.test_push).grid(row=2, column=2, sticky="e", pady=(6, 0))
        self.push_note = ttk.Label(box, font=SMALL, foreground="#666")
        self.push_note.grid(row=3, column=0, columnspan=3, sticky="w", pady=(4, 0))
        self.push_url.bind("<FocusOut>", lambda e: self.save_push())

    def save_push(self):
        url = self.push_url.get().strip()
        self.hud.cfg.update(notify_url=url, notify_done=self.push_done.get(), notify_ask=self.push_ask.get())
        self.hud.save()
        return url

    def test_push(self):
        url = self.save_push()
        if not url:
            self.push_note.configure(text="先把群机器人的地址粘进来。", foreground="#c33")
            return
        self.push_note.configure(text="正在发…", foreground="#666")
        sample = {"ws": "测试", "title": "这是一条测试通知", "last_text": "以后有任务做完、或者在等你回答时，会这样发到这里。", "id": "test"}
        result = []

        def work():   # 结果由窗口自己来取（别的线程不碰界面）
            result.append(notify.send(url, notify.message("done", sample)))

        threading.Thread(target=work, daemon=True).start()
        self.wait_push(result)

    def wait_push(self, result):
        if not self.winfo_exists():
            return
        if not result:
            self.after(100, self.wait_push, result)
            return
        ok, message = result[0]
        self.push_note.configure(text=("发出去了，看看手机上收到没有。" if ok else message + "。看看地址对不对、关键词是不是「悬浮窗」。"),
                                 foreground="#2a7" if ok else "#c33")

    def close(self):
        self.save_push()   # 粘了地址直接关窗口也算数
        self.destroy()

    def toggle(self):
        web = self.hud.web
        self.hud.cfg["web_on"] = self.on.get()
        self.hud.save()
        if self.on.get() and not web.start():
            self.note.configure(text="开不起来：端口都被占了。")
        elif not self.on.get():
            web.stop()
        self.show()

    def show(self):
        running = bool(self.hud.web.server)
        url = self.hud.web.url() if running else "（没开启）"
        self.url.configure(state="normal")
        self.url.delete(0, "end")
        self.url.insert(0, url)
        self.url.configure(state="readonly")
        self.qr.delete("all")
        if running:
            try:
                import segno
                rows = segno.make(url, error="m").matrix
                cell = 190 // (len(rows) + 4)
                pad = (210 - cell * len(rows)) // 2
                for y, row in enumerate(rows):
                    for x, bit in enumerate(row):
                        if bit:
                            self.qr.create_rectangle(pad + x * cell, pad + y * cell, pad + (x + 1) * cell, pad + (y + 1) * cell,
                                                     fill="black", outline="")
            except Exception as e:
                self.qr.create_text(105, 105, text="二维码画不出来：%s" % e, width=190)
        else:
            self.qr.create_text(105, 105, text="勾上「开启手机访问」\n这里会出现二维码", justify="center", fill="#999")

    def copy(self):
        if self.hud.web.server:
            self.clipboard_clear()
            self.clipboard_append(self.hud.web.url())
            self.note.configure(text="已复制。可以粘贴到微信「文件传输助手」，在手机上点开。")

    def rekey(self):
        self.hud.web.new_key()
        self.hud.save()
        self.show()
        self.note.configure(text="已换钥匙：旧地址打开的页面会被拒绝，手机上重新扫一次码。")


class Compose(Window):
    """发起一个任务：选项目、选在哪跑（在哪个编辑器窗口里新开对话，或者后台跑）、写要做什么。"""

    BACKGROUND = "后台跑（不开窗口）"
    # 手动断好行，理由同 WebWindow.NOTES
    NOTES = {"window": "会切到这个编辑器窗口，按快捷键新开一个 Claude 对话，替你把这段话打进去发出。\n"
                       "之后就是编辑器里的普通对话，直接在里面接着聊。打字要几秒钟，这期间别动键盘鼠标。",
             "background": "任务在后台跑，不开窗口、不弹确认框，权限模式是 %s（和你在编辑器里用的一样）。\n"
                           "进展照常显示在悬浮窗里，卡片上带「后台」标记；双击它跳到编辑器里，右键可以停掉。"}

    def __init__(self, hud, ws_path="", text=""):
        super().__init__(hud, "发起任务")
        items = hud.reg.items
        ttk.Label(self, text="项目", font=FONT).grid(row=0, column=0, sticky="w")
        self.ws = ttk.Combobox(self, values=[it["name"] for it in items], state="readonly", width=34, font=FONT)
        chosen = hud.reg.match(ws_path or hud.cfg.get("last_ws") or "") or (items[0] if items else None)
        if chosen:
            self.ws.set(chosen["name"])
        self.ws.grid(row=0, column=1, sticky="w", padx=(8, 0))
        self.ws.bind("<<ComboboxSelected>>", lambda e: self.pick_where())
        ttk.Label(self, text="在哪跑", font=FONT).grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.where = ttk.Combobox(self, state="readonly", width=34, font=FONT)
        self.where.grid(row=1, column=1, sticky="w", padx=(8, 0), pady=(6, 0))
        self.where.bind("<<ComboboxSelected>>", lambda e: self.show_note())
        self.body = tk.Text(self, width=58, height=8, wrap="word", font=FONT, undo=True)
        self.body.grid(row=2, column=0, columnspan=2, pady=8)
        self.body.insert("1.0", text)
        # 说明是手动断好行的；出错时的说明可能很长，限定宽度，免得把窗口撑宽
        self.note = ttk.Label(self, font=SMALL, foreground="#666", justify="left", wraplength=self.body.winfo_reqwidth() or 520)
        self.note.grid(row=3, column=0, columnspan=2, sticky="w")
        bar = ttk.Frame(self)
        bar.grid(row=4, column=0, columnspan=2, sticky="e", pady=(10, 0))
        self.button = ttk.Button(bar, text="发起（Ctrl+Enter）", command=self.send)
        self.button.pack(side="left", padx=4)
        ttk.Button(bar, text="取消", command=self.destroy).pack(side="left")
        self.body.bind("<Control-Return>", lambda e: self.send() or "break")
        self.windows = []
        self.pick_where()
        self.place_beside()
        self.body.focus_force()

    def pick_where(self):
        """按选的项目列出能选的编辑器窗口，默认选开着这个项目（或它上级文件夹）的那个；上次选的是后台跑就还是后台跑。"""
        ws = self.hud.reg.by_name(self.ws.get())
        self.windows = runner.compose_windows(ws["path"] if ws else "")
        self.where.configure(values=[w["label"] for w in self.windows] + [self.BACKGROUND])
        near = next((w for w in self.windows if w["depth"] is not None), None)
        self.where.set(near["label"] if near and self.hud.cfg.get("compose_where") != "background" else self.BACKGROUND)
        self.show_note()

    def chosen_window(self):
        return next((w for w in self.windows if w["label"] == self.where.get()), None)

    def show_note(self):
        win = self.chosen_window()
        self.note.configure(foreground="#666", text=self.NOTES["window"] if win else self.NOTES["background"] % self.hud.runner.mode)

    def send(self):
        ws = self.hud.reg.by_name(self.ws.get())
        text = self.body.get("1.0", "end").strip()
        if not ws or not text or str(self.button["state"]) == "disabled":
            self.bell()
            return
        win = self.chosen_window()
        self.hud.cfg["compose_where"] = "window" if win else "background"
        self.hud.save()
        if not win:
            try:
                self.hud.start_task(ws, text)
            except Exception as e:
                self.fail(e)
                return
            self.destroy()
            return
        prompt = text   # 只发用户写的原话（用户定的：不要在开头加「在 xx 项目里」）；项目靠 pin_session 记下
        use_key, fresh = self.ensure_key(win["scheme"])
        self.button.configure(state="disabled")
        self.withdraw()   # 让开，别挡着编辑器
        result = []

        def work():   # 切窗口、等面板、打字、核对要十来秒，放到后台线程；结果由窗口自己来取（别的线程不碰界面）
            if fresh:
                time.sleep(1.5)   # 刚改了 keybindings.json，等编辑器读到
            try:
                result.append(runner.start_in_editor(win["hwnd"], win["scheme"], prompt, use_key=use_key))
            except Exception as e:
                result.append((False, "出错了：%r" % e, None))
            self.hud.log("在编辑器里发起（%s）：%s %s，对话 %s" % (win["label"], "成了" if result[0][0] else "没成", result[0][1], result[0][2]))

        threading.Thread(target=work, daemon=True).start()
        self.wait_for(result, ws)

    def wait_for(self, result, ws):
        if not self.winfo_exists():
            return
        if not result:
            self.after(200, self.wait_for, result, ws)
            return
        ok, message, sid = result[0]
        if sid:
            self.hud.pin_session(sid, ws)
        if ok:
            self.destroy()
            return
        self.deiconify()
        if ok is None:   # 对话开好了、话填好了，等用户自己按回车：不算失败，也别让人再点一次又开一个
            self.note.configure(text=message, foreground="#b45309")
            return
        self.note.configure(text=message, foreground="#c33")   # 没成：写的东西还在，可以再发一次或者改成后台跑
        self.button.configure(state="normal")

    NEW_TAB_ASK = ("要让悬浮窗在 %s 里新开对话、自动把话发出去，需要给它加一个快捷键：\n\n"
                   "    Ctrl+Alt+Shift+F12  →  Claude Code: Open in New Tab\n\n"
                   "只往 %s 的 keybindings.json 末尾加这一条（改之前会备份），别的设置不动。加吗？\n\n"
                   "不加的话，悬浮窗会新开对话、把话填进输入框，你自己按一下回车。")

    def ensure_key(self, scheme):
        """新开对话要用的快捷键（见 runner.NEW_TAB_KEY）：已经有了就用；没有就问一次要不要加，说不要的记下来、以后不再问。
        返回（用不用快捷键, 是不是刚加上的）。"""
        if runner.has_new_tab_key(scheme):
            return True, False
        declined = self.hud.cfg.setdefault("no_new_tab_key", [])
        if scheme in declined:
            return False, False
        name = runner.EDITOR_NAMES.get(scheme, scheme)
        if not messagebox.askyesno("给 %s 加一个快捷键" % name, self.NEW_TAB_ASK % (name, name), parent=self):
            declined.append(scheme)
            self.hud.save()
            return False, False
        ok, message = runner.add_new_tab_key(scheme)
        self.hud.log("给 %s 加新开对话的快捷键：%s" % (name, message))
        if not ok:
            messagebox.showwarning("没加上", "快捷键没加上：%s\n\n这次先新开对话、把话填好，你自己按回车。" % message, parent=self)
            return False, False
        return True, True
