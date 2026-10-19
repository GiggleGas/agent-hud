"""右下角托盘里的图标：悬浮窗在跑，它就一直在。

图标是深色圆角方块上一个圆点，颜色和悬浮窗标题栏的那个点一样跟着状态变（蓝：有在干活的；黄：有在等你的；
绿：有结束了没看的；灰：空闲），鼠标停上去显示「几个在干活 · 几个等你」。左键单击：把悬浮窗叫出来（只出来，不藏起：用户定的）；右键：菜单。

托盘图标要一个自己的 Win32 窗口来收鼠标消息，所以单独开一个线程跑它的消息循环。点了什么不直接动界面（别的线程不碰 Tk），
交给 on_click(命令) 回调，悬浮窗把它放进队列，由界面线程来取。只用系统自带的接口，不另装库。
"""
import ctypes
import threading
from ctypes import wintypes

u32 = ctypes.WinDLL("user32", use_last_error=True)
gdi = ctypes.WinDLL("gdi32")
shell = ctypes.WinDLL("shell32")
k32 = ctypes.WinDLL("kernel32")

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
WM_DESTROY, WM_CLOSE, WM_NULL, WM_APP = 0x0002, 0x0010, 0x0000, 0x8000
WM_LBUTTONUP, WM_RBUTTONUP = 0x0202, 0x0205
CALLBACK = WM_APP + 1
NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP = 1, 2, 4
MENU = (("toggle", None), ("compose", "发起任务…"), ("feedback", "反馈给作者…"), ("-", None), ("quit", "退出"))


class WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", wintypes.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON), ("hCursor", wintypes.HANDLE),
                ("hbrBackground", wintypes.HBRUSH), ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR)]


class GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("hWnd", wintypes.HWND), ("uID", wintypes.UINT), ("uFlags", wintypes.UINT),
                ("uCallbackMessage", wintypes.UINT), ("hIcon", wintypes.HICON), ("szTip", wintypes.WCHAR * 128),
                ("dwState", wintypes.DWORD), ("dwStateMask", wintypes.DWORD), ("szInfo", wintypes.WCHAR * 256),
                ("uVersion", wintypes.UINT), ("szInfoTitle", wintypes.WCHAR * 64), ("dwInfoFlags", wintypes.DWORD),
                ("guidItem", GUID), ("hBalloonIcon", wintypes.HICON)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD), ("biSizeImage", wintypes.DWORD),
                ("biXPelsPerMeter", wintypes.LONG), ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD)]


class ICONINFO(ctypes.Structure):
    _fields_ = [("fIcon", wintypes.BOOL), ("xHotspot", wintypes.DWORD), ("yHotspot", wintypes.DWORD),
                ("hbmMask", wintypes.HBITMAP), ("hbmColor", wintypes.HBITMAP)]


# 64 位系统上句柄和返回值不声明类型会被截断
u32.DefWindowProcW.restype = LRESULT
u32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
u32.CreateWindowExW.restype = wintypes.HWND
u32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
u32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
u32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
u32.DestroyWindow.argtypes = u32.SetForegroundWindow.argtypes = [wintypes.HWND]
u32.CreatePopupMenu.restype = wintypes.HMENU
u32.AppendMenuW.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_size_t, wintypes.LPCWSTR]
u32.TrackPopupMenu.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HWND, wintypes.LPVOID]
u32.DestroyMenu.argtypes = [wintypes.HMENU]
u32.CreateIconIndirect.restype = wintypes.HICON
u32.CreateIconIndirect.argtypes = [ctypes.POINTER(ICONINFO)]
u32.DestroyIcon.argtypes = [wintypes.HICON]
u32.GetDC.restype = wintypes.HDC
u32.GetDC.argtypes = [wintypes.HWND]
u32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
gdi.CreateDIBSection.restype = gdi.CreateBitmap.restype = wintypes.HBITMAP
gdi.CreateDIBSection.argtypes = [wintypes.HDC, ctypes.POINTER(BITMAPINFOHEADER), wintypes.UINT, ctypes.POINTER(ctypes.c_void_p),
                                 wintypes.HANDLE, wintypes.DWORD]
gdi.CreateBitmap.argtypes = [ctypes.c_int, ctypes.c_int, wintypes.UINT, wintypes.UINT, ctypes.c_void_p]
gdi.DeleteObject.argtypes = [wintypes.HGDIOBJ]
shell.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.POINTER(NOTIFYICONDATAW)]
k32.GetModuleHandleW.restype = wintypes.HMODULE


def rgb(color):
    return tuple(int(color[i:i + 2], 16) for i in (1, 3, 5))


def pixels(size, dot, back="#15171c", edge="#3a3f4c"):
    """画图标：深色圆角方块（和程序图标一样的配色）+ 中间一个 dot 色的圆点。返回从上到下一行行的 BGRA 字节。
    每个像素取 4×4 个点算覆盖率，边缘才平滑。"""
    out = bytearray()
    n, r, ring, rad = 4, size * 0.22, max(1.0, size / 16), size * 0.27
    c = (size - 1) / 2
    colors = [rgb(edge), rgb(back), rgb(dot)]
    for y in range(size):
        for x in range(size):
            acc = [0.0, 0.0, 0.0, 0.0]   # 方块覆盖、方块里面（去掉描边）覆盖、圆点覆盖
            for sy in range(n):
                for sx in range(n):
                    px, py = x + (sx + 0.5) / n, y + (sy + 0.5) / n
                    inside = in_rounded(px, py, 0, 0, size, size, r)
                    acc[0] += inside
                    acc[1] += inside and in_rounded(px, py, ring, ring, size - ring, size - ring, max(0.0, r - ring))
                    acc[2] += (px - 0.5 - c) ** 2 + (py - 0.5 - c) ** 2 <= rad * rad
            box, body, spot = (a / (n * n) for a in acc[:3])
            col = [colors[0][i] * (box - body) + colors[1][i] * (body - spot) + colors[2][i] * spot for i in range(3)]
            col = [v / box if box else 0 for v in col]
            out += bytes((round(col[2]), round(col[1]), round(col[0]), round(box * 255)))
    return bytes(out)


def in_rounded(px, py, x0, y0, x1, y1, r):
    if not (x0 <= px <= x1 and y0 <= py <= y1):
        return False
    cx = min(max(px, x0 + r), x1 - r)
    cy = min(max(py, y0 + r), y1 - r)
    return (px - cx) ** 2 + (py - cy) ** 2 <= r * r


def make_icon(size, data):
    """BGRA 字节 -> 图标句柄（带透明度）。"""
    head = BITMAPINFOHEADER(biSize=ctypes.sizeof(BITMAPINFOHEADER), biWidth=size, biHeight=-size, biPlanes=1, biBitCount=32)
    bits = ctypes.c_void_p()
    dc = u32.GetDC(None)
    color = gdi.CreateDIBSection(dc, ctypes.byref(head), 0, ctypes.byref(bits), None, 0)
    u32.ReleaseDC(None, dc)
    if not color:
        return None
    ctypes.memmove(bits, data, len(data))
    mask = gdi.CreateBitmap(size, size, 1, 1, bytes((size + 15) // 16 * 2 * size))
    info = ICONINFO(fIcon=True, hbmMask=mask, hbmColor=color)
    icon = u32.CreateIconIndirect(ctypes.byref(info))
    gdi.DeleteObject(mask)
    gdi.DeleteObject(color)
    return icon


class Tray:
    def __init__(self, colors, on_click, tip="Agent 悬浮窗"):
        """colors：状态 -> 颜色（#rrggbb）；on_click(命令)：命令是 show（左键）/ toggle / compose / feedback / quit（菜单）。"""
        self.colors, self.on_click = colors, on_click
        self.state, self.tip, self.shown = next(iter(colors)), tip, True
        self.hwnd = None
        self.recreated = None           # 资源管理器重启后发的消息号：收到就重新加图标
        self.icons = {}
        self.ok = False                 # 图标加上了没有（资源管理器没在跑之类的情况会加不上）
        self.ready = threading.Event()
        self.thread = None

    def start(self, wait=5):
        self.thread = threading.Thread(target=self.loop, daemon=True)
        self.thread.start()
        self.ready.wait(wait)
        return self.ok

    def data(self):
        d = NOTIFYICONDATAW(cbSize=ctypes.sizeof(NOTIFYICONDATAW), hWnd=self.hwnd, uID=1,
                            uFlags=NIF_MESSAGE | NIF_ICON | NIF_TIP, uCallbackMessage=CALLBACK, hIcon=self.icons.get(self.state))
        d.szTip = self.tip[:127]
        return d

    def update(self, state, tip):
        """换颜色和提示文字；没变就什么都不做。可以从别的线程调。"""
        if (state, tip) == (self.state, self.tip) or state not in self.colors:
            return
        self.state, self.tip = state, tip
        if self.ok:
            shell.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(self.data()))

    def stop(self):
        """拿掉图标、结束消息循环。悬浮窗退出时一定要调，不然图标会在托盘里留到鼠标划过才消失。"""
        if self.hwnd:
            u32.PostMessageW(self.hwnd, WM_CLOSE, 0, 0)
            if self.thread:
                self.thread.join(3)

    def loop(self):
        try:
            size = u32.GetSystemMetrics(49) or 16   # SM_CXSMICON：托盘用的小图标尺寸（随缩放变）
            self.icons = {k: make_icon(size, pixels(size, c)) for k, c in self.colors.items()}
            self.proc = WNDPROC(self.wndproc)       # 回调要一直有人引用着，不然会被回收
            inst = k32.GetModuleHandleW(None)
            name = "AgentHudTray%d" % id(self)
            self.recreated = u32.RegisterWindowMessageW("TaskbarCreated")   # 要在建窗口之前：建窗口的过程中就会调 wndproc
            u32.RegisterClassW(ctypes.byref(WNDCLASSW(lpfnWndProc=self.proc, hInstance=inst, lpszClassName=name)))
            self.hwnd = u32.CreateWindowExW(0, name, name, 0, 0, 0, 0, 0, None, None, inst, None)
            self.ok = bool(self.hwnd and shell.Shell_NotifyIconW(NIM_ADD, ctypes.byref(self.data())))
        except Exception:
            self.ok = False
        finally:
            self.ready.set()
        if not self.hwnd:
            return
        msg = wintypes.MSG()
        while u32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            u32.TranslateMessage(ctypes.byref(msg))
            u32.DispatchMessageW(ctypes.byref(msg))
        for icon in self.icons.values():
            if icon:
                u32.DestroyIcon(icon)

    def wndproc(self, hwnd, msg, wparam, lparam):
        if msg == CALLBACK:
            if lparam & 0xFFFF == WM_LBUTTONUP:
                self.on_click("show")
            elif lparam & 0xFFFF == WM_RBUTTONUP:
                self.menu()
            return 0
        if msg == self.recreated:
            self.ok = bool(shell.Shell_NotifyIconW(NIM_ADD, ctypes.byref(self.data())))
            return 0
        if msg == WM_CLOSE:
            shell.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self.data()))
            self.ok = False
            u32.DestroyWindow(hwnd)
            return 0
        if msg == WM_DESTROY:
            u32.PostQuitMessage(0)
            return 0
        return u32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def menu(self):
        menu = u32.CreatePopupMenu()
        for i, (cmd, label) in enumerate(MENU, 1):
            if cmd == "-":
                u32.AppendMenuW(menu, 0x800, 0, None)   # MF_SEPARATOR
            else:
                u32.AppendMenuW(menu, 0, i, label or ("藏起悬浮窗" if self.shown else "显示悬浮窗"))
        pt = wintypes.POINT()
        u32.GetCursorPos(ctypes.byref(pt))
        u32.SetForegroundWindow(self.hwnd)   # 不先切到前台，菜单点别处不会消失
        chosen = u32.TrackPopupMenu(menu, 0x0100 | 0x0080 | 0x0002, pt.x, pt.y, 0, self.hwnd, None)   # 返回选中的项、不发通知、右键也能选
        u32.PostMessageW(self.hwnd, WM_NULL, 0, 0)
        u32.DestroyMenu(menu)
        if chosen:
            self.on_click(MENU[chosen - 1][0])
