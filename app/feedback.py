"""反馈：用户写几句话，发到作者的飞书群（群自定义机器人的 webhook，地址在 feedback_to.py）。

消息开头是「【关键词】【Agent悬浮窗】版本」：作者的几个作品共用一个反馈群，群机器人的安全设置要求消息里带关键词。
发之前先在本机存一份（%USERPROFILE%\\.claude\\agent-hud\\feedback\\），发出去了就删掉，没发出去的留着不会丢。
测试时可以用环境变量 AGENT_HUD_FEEDBACK_URL 换接收地址。

图片：群机器人能发图文消息，但里面的图片编号（image_key）得先用飞书应用的身份上传。feedback_to.py 里配了飞书应用，
就先用应用传图、再由群机器人把文字和图片发成一条（deliver → upload_images → send）；没配或者传图出错，文字照常走群机器人，
图片存在本机（feedback\\images\\<这次的时间>\\），发送后一张张以位图放进剪贴板，让用户自己到飞书里粘贴发给作者。
"""
import ctypes
import getpass
import glob
import json
import os
import platform
import re
import shutil
import struct
import time
import urllib.error
import urllib.request
import uuid
import zlib
from ctypes import wintypes

import feedback_to
import install

NAME = "Agent悬浮窗"
MAX_TEXT = 2000        # 正文最多这么多字，再多截掉（飞书群机器人一条消息最大 20 KB）
LOG_CHARS = 4000       # 附上的日志最多这么多字
DIR = os.path.join(install.TARGET, "feedback")
EXTENSIONS = (("Cursor", "~/.cursor/extensions"), ("VS Code", "~/.vscode/extensions"))
REASONS = {19024: "消息里没有群机器人要求的关键词", 19001: "接收地址不对（机器人可能被删了）", 9499: "发得太频繁了"}


def url():
    return os.environ.get("AGENT_HUD_FEEDBACK_URL") or feedback_to.URL


def user():
    try:
        return getpass.getuser()
    except Exception:
        return ""


def context(version, scale=1.0, with_log=False):
    """自动附上的现场：版本、系统、各编辑器里的 Claude Code 扩展版本；勾了的话再加最近的运行日志。"""
    exts = []
    for editor, root in EXTENSIONS:
        found = [tuple(int(x) for x in m.groups()) for p in glob.glob(os.path.join(os.path.expanduser(root), "anthropic.claude-code-*"))
                 for m in [re.search(r"claude-code-(\d+)\.(\d+)\.(\d+)", p)] if m]
        if found:
            exts.append("%s %s" % (editor, ".".join(map(str, max(found)))))
    lines = ["%s %s（%s）· Windows %s · 屏幕缩放 %d%% · Claude Code 扩展：%s" % (
        NAME, version, "exe 版" if install.FROZEN else "脚本版 Python %s" % platform.python_version(),
        platform.version(), round(scale * 100), "、".join(exts) or "没找到")]
    if with_log:
        tail = log_tail()
        lines.append("最近的运行日志：\n" + tail if tail else "最近的运行日志：（没有日志）")
    return lines


def log_tail(path=None):
    """运行日志（hud.log）最后一段，最多 LOG_CHARS 个字；从中间截的那半行不要。"""
    path = path or os.path.join(install.TARGET, "hud.log")
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - LOG_CHARS * 3))
            text = f.read().decode("utf-8", "replace")
    except OSError:
        return ""
    rows = text.splitlines()
    if size > LOG_CHARS * 3 and len(rows) > 1:
        rows = rows[1:]
    out = []
    for row in reversed(rows):   # 从最新的往前拿，拿满为止
        row = row.rstrip()
        if row:
            out.insert(0, "  " + (row[:200] + "…" if len(row) > 200 else row))
        if sum(len(r) + 1 for r in out) > LOG_CHARS:
            out.pop(0)
            break
    return "\n".join(out)


def compose(text, who, version, extra=()):
    """拼成发出去的那一整段。"""
    text = text.strip()
    if len(text) > MAX_TEXT:
        text = text[:MAX_TEXT] + "…"
    me, who = user(), (who or "").strip()
    if who and me and who != me:
        who = "%s（电脑用户名 %s）" % (who, me)   # 填的称呼可能撞名，带上电脑用户名好找人
    lines = ["【%s】【%s】%s" % (feedback_to.KEYWORD, NAME, version), "来自：%s" % (who or me or "（没留名字）")]
    return "\n".join(lines + ["————", text, "————"] + [x for x in extra if x])


def payload(to, body, keys=()):
    """按接收地址是谁家的拼它认的格式；不认识的地址收到普通 JSON。
    keys：飞书应用上传好的图片编号；有的话发成一条图文消息（第一行当标题，正文一行一段，图片排在最后）。"""
    if "feishu.cn" in to or "larksuite.com" in to:
        if not keys:
            return {"msg_type": "text", "content": {"text": body}}
        lines = body.splitlines()
        content = [[{"tag": "text", "text": line or " "}] for line in lines[1:]] + [[{"tag": "img", "image_key": k}] for k in keys]
        return {"msg_type": "post", "content": {"post": {"zh_cn": {"title": lines[0] if lines else NAME, "content": content}}}}
    return {"text": body, "product": NAME}


def rejected(reply):
    """接收端回了 200，但内容里说不收（飞书：code 不是 0；钉钉 / 企业微信：errcode 不是 0）。收了返回空字符串。"""
    try:
        d = json.loads(reply)
    except ValueError:
        return ""
    if not isinstance(d, dict):
        return ""
    for key in ("code", "StatusCode", "errcode"):
        code = d.get(key)
        if isinstance(code, int) and code != 0:
            return "%s（%d %s）" % (REASONS.get(code, "对方不收"), code, d.get("msg") or d.get("StatusMessage") or d.get("errmsg") or "")
    return ""


def save_local(body):
    try:
        os.makedirs(DIR, exist_ok=True)
        path = os.path.join(DIR, time.strftime("%Y%m%d-%H%M%S") + ".txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write(body)
        return path
    except OSError:
        return ""


def send(body, keys=(), timeout=12):
    """发一条：先存本机，再发；发出去了删掉本机那份。keys：飞书应用上传好的图片编号。返回（成没成，一句给人看的话）。"""
    saved = save_local(body)
    kept = "内容存在了 %s，可以直接把这个文件发给作者。" % saved if saved else "（存到本机也失败了。）"
    to = url()
    if not to:
        return False, "这个版本没有配反馈的接收地址。" + kept
    req = urllib.request.Request(to, data=json.dumps(payload(to, body, keys), ensure_ascii=False).encode("utf-8"),
                                 headers={"Content-Type": "application/json; charset=utf-8"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            err = rejected(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        err = rejected(e.read().decode("utf-8", "replace")) or "对方回了 %d" % e.code
    except (OSError, ValueError) as e:
        err = "连不上（%s）" % getattr(e, "reason", e)
    if err:
        return False, "没发出去：%s。%s" % (err, kept)
    if saved:
        try:
            os.remove(saved)
        except OSError:
            pass
    return True, "发出去了，谢谢！"


# ---------- 用飞书应用上传图片（群机器人发图片要的 image_key 得由应用上传） ----------
FEISHU = "https://open.feishu.cn/open-apis"
APP_REASONS = {10003: "App ID 或 App Secret 不对", 10014: "App ID 或 App Secret 不对", 99991663: "应用的令牌失效了",
               99991672: "应用缺权限（缺哪项见后面）", 234001: "图片格式不支持", 234006: "图片太大（超过 10 MB）"}


class AppError(Exception):
    pass


def app_ready():
    return bool(feedback_to.APP_ID and feedback_to.APP_SECRET)


def multipart(fields):
    """{名字: 文字 或 (文件名, 字节)} -> (请求体, Content-Type)。"""
    boundary = uuid.uuid4().hex
    out = b""
    for name, value in fields.items():
        out += b"--" + boundary.encode() + b"\r\n"
        if isinstance(value, tuple):
            out += ('Content-Disposition: form-data; name="%s"; filename="%s"\r\nContent-Type: application/octet-stream\r\n\r\n'
                    % (name, value[0])).encode() + value[1] + b"\r\n"
        else:
            out += ('Content-Disposition: form-data; name="%s"\r\n\r\n%s\r\n' % (name, value)).encode("utf-8")
    return out + b"--" + boundary.encode() + b"--\r\n", "multipart/form-data; boundary=" + boundary


def api(method, path, token=None, body=None, files=None, timeout=20):
    """调飞书开放平台的接口，返回整个回复；code 不是 0 就抛 AppError（带上能看懂的原因）。"""
    headers, data = {}, None
    if token:
        headers["Authorization"] = "Bearer " + token
    if files:
        data, headers["Content-Type"] = multipart(files)
    elif body is not None:
        data, headers["Content-Type"] = json.dumps(body, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8"
    req = urllib.request.Request((os.environ.get("AGENT_HUD_FEISHU_BASE") or FEISHU) + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            reply = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            reply = json.loads(e.read().decode("utf-8"))
        except ValueError:
            raise AppError("对方回了 %d" % e.code)
    except (OSError, ValueError) as e:
        raise AppError("连不上飞书（%s）" % (getattr(e, "reason", None) or e))
    code = reply.get("code")
    if code != 0:
        raise AppError("%s（%s %s）" % (APP_REASONS.get(code, "飞书不收"), code, reply.get("msg") or ""))
    return reply


def upload_images(images):
    """用飞书应用把图片传上去：换令牌 → 一张张上传。返回 image_key 列表；失败抛 AppError。"""
    token = api("POST", "/auth/v3/tenant_access_token/internal",
                body={"app_id": feedback_to.APP_ID, "app_secret": feedback_to.APP_SECRET})["tenant_access_token"]
    keys = []
    for i, path in enumerate(images, 1):
        with open(path, "rb") as f:
            data = f.read()
        name = "image%d%s" % (i, os.path.splitext(path)[1].lower())   # 文件名只用英文，免得上传时出岔子
        keys.append(api("POST", "/im/v1/images", token, files={"image_type": "message", "image": (name, data)})["data"]["image_key"])
    return keys


def deliver(body, images=()):
    """发一条反馈，能带图片。返回（成没成, 一句说明, 图片发出去了没有）。
    配了飞书应用：先用应用把图片传上去，再用群机器人把文字和图片发成一条图文消息。
    没配或者传图出错：文字照常走群机器人，写上附了几张图，图片要用户自己贴。"""
    keys, problem = [], ""
    if images and app_ready():
        try:
            keys = upload_images(images)
        except (AppError, OSError) as e:
            problem = str(e)
    if images and not keys:
        body += "\n附图 %d 张（%s）：会在飞书里另外发给你。" % (len(images), "、".join(os.path.basename(p) for p in images))
    ok, message = send(body, keys)
    if problem:
        message += "（图片没能直接发：%s）" % problem
    elif ok and keys:
        message += "图片也一起发过去了。"
    return ok, message, not images or (ok and bool(keys))


# ---------- 图片 ----------
IMAGES = os.path.join(DIR, "images")
IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp")
CF_DIB, CF_HDROP = 8, 15

user32 = ctypes.WinDLL("user32")
kernel32 = ctypes.WinDLL("kernel32")
shell32 = ctypes.WinDLL("shell32")
user32.OpenClipboard.argtypes = [wintypes.HWND]
user32.GetClipboardData.restype = user32.SetClipboardData.restype = wintypes.HANDLE
user32.GetClipboardData.argtypes = [wintypes.UINT]
user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalLock.argtypes = kernel32.GlobalUnlock.argtypes = kernel32.GlobalSize.argtypes = kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalSize.restype = ctypes.c_size_t
shell32.DragQueryFileW.argtypes = [wintypes.HANDLE, wintypes.UINT, wintypes.LPWSTR, wintypes.UINT]


def png(width, height, rows):
    """rows：从上到下每行 width*4 个字节的 RGBA。返回 PNG 文件的字节（不用另装图片库）。"""
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    head = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)   # 8 位、RGBA
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", head) + chunk(b"IDAT", zlib.compress(b"".join(b"\x00" + r for r in rows), 6)) + chunk(b"IEND", b"")


def dib_to_png(dib):
    """剪贴板里的截图（CF_DIB：BITMAPINFOHEADER + 像素，一般是 32 位或 24 位、从下往上存）转成 PNG。认不出的格式返回 None。"""
    if len(dib) < 40:
        return None
    size, w, h, _, bpp, comp = struct.unpack_from("<IiiHHI", dib, 0)
    used = struct.unpack_from("<I", dib, 32)[0]
    if bpp not in (24, 32) or comp not in (0, 3) or w <= 0 or h == 0:
        return None
    if comp == 3 and struct.unpack_from("<III", dib, 40) != (0xFF0000, 0xFF00, 0xFF):   # 颜色不是常见的 BGR 排法
        return None
    off = size + (12 if comp == 3 and size == 40 else 0) + used * 4
    stride, n, rows = (w * bpp + 31) // 32 * 4, bpp // 8, []
    if len(dib) < off + stride * abs(h):
        return None
    alpha = bpp == 32 and any(dib[off + 3:off + stride * abs(h):4])   # 大多数截图的透明度全是 0，那就当不透明
    for y in range(abs(h)):
        start = off + (y if h < 0 else abs(h) - 1 - y) * stride
        line = dib[start:start + w * n]
        out = bytearray(w * 4)
        out[0::4], out[1::4], out[2::4] = line[2::n], line[1::n], line[0::n]
        out[3::4] = line[3::4] if alpha else b"\xff" * w
        rows.append(bytes(out))
    return png(w, abs(h), rows)


def bmp(dib):
    """认不出的位图原样存成 BMP：前面补一个文件头就行。"""
    size, _, _, _, bpp, comp = struct.unpack_from("<IiiHHI", dib, 0)
    used = struct.unpack_from("<I", dib, 32)[0]
    colors = used or (1 << bpp if bpp <= 8 else 0)
    off = 14 + size + (12 if comp == 3 and size == 40 else 0) + colors * 4
    return b"BM" + struct.pack("<IHHI", 14 + len(dib), 0, 0, off) + dib


def new_folder():
    """这次反馈放图片的文件夹（按时间起名）；顺带清掉一周以前的。"""
    now = time.time()
    for old in glob.glob(os.path.join(IMAGES, "*")):
        try:
            if now - os.path.getmtime(old) > 7 * 86400:
                shutil.rmtree(old, ignore_errors=True)
        except OSError:
            pass
    folder = os.path.join(IMAGES, time.strftime("%Y%m%d-%H%M%S"))
    os.makedirs(folder, exist_ok=True)
    return folder


def add_file(folder, path):
    """把选的 / 复制的图片文件复制一份到这次的文件夹里，返回新路径；不是图片返回 None。"""
    if not path.lower().endswith(IMAGE_EXTS) or not os.path.isfile(path):
        return None
    name = os.path.basename(path)
    target = os.path.join(folder, name)
    n = 1
    while os.path.exists(target):
        stem, ext = os.path.splitext(name)
        n += 1
        target = os.path.join(folder, "%s-%d%s" % (stem, n, ext))
    shutil.copy2(path, target)
    return target


def paste_images(folder):
    """剪贴板里的图片存进这次的文件夹：截图（位图）存成 PNG；在资源管理器里复制的图片文件就复制过来。
    返回存下的路径列表；剪贴板里没有图片返回空列表（这时让输入框照常粘贴文字）。"""
    if not user32.OpenClipboard(None):
        return []
    try:
        if user32.IsClipboardFormatAvailable(CF_HDROP):
            h = user32.GetClipboardData(CF_HDROP)
            files = []
            for i in range(shell32.DragQueryFileW(h, 0xFFFFFFFF, None, 0) if h else 0):
                buf = ctypes.create_unicode_buffer(1024)
                shell32.DragQueryFileW(h, i, buf, 1024)
                files.append(buf.value)
            return [p for p in (add_file(folder, f) for f in files) if p]
        if not user32.IsClipboardFormatAvailable(CF_DIB):
            return []
        h = user32.GetClipboardData(CF_DIB)
        p = kernel32.GlobalLock(h) if h else None
        if not p:
            return []
        try:
            dib = ctypes.string_at(p, kernel32.GlobalSize(h))
        finally:
            kernel32.GlobalUnlock(h)
    finally:
        user32.CloseClipboard()
    data, ext = dib_to_png(dib), ".png"
    if data is None:
        data, ext = bmp(dib), ".bmp"
    path = os.path.join(folder, "截图 %s%s" % (time.strftime("%H%M%S"), ext))
    n = 1
    while os.path.exists(path):
        n += 1
        path = os.path.join(folder, "截图 %s-%d%s" % (time.strftime("%H%M%S"), n, ext))
    with open(path, "wb") as f:
        f.write(data)
    return [path]


class GdiplusStartupInput(ctypes.Structure):
    _fields_ = [("GdiplusVersion", ctypes.c_uint32), ("DebugEventCallback", ctypes.c_void_p),
                ("SuppressBackgroundThread", wintypes.BOOL), ("SuppressExternalCodecs", wintypes.BOOL)]


def load_bitmap(path):
    """用 Windows 自带的 GDI+ 把图片文件（png / jpg / bmp / gif）读成位图句柄；读不了返回 None。"""
    gp = ctypes.WinDLL("gdiplus")
    gp.GdiplusStartup.argtypes = [ctypes.POINTER(ctypes.c_size_t), ctypes.POINTER(GdiplusStartupInput), ctypes.c_void_p]
    gp.GdipCreateBitmapFromFile.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_void_p)]
    gp.GdipCreateHBITMAPFromBitmap.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.HBITMAP), wintypes.DWORD]
    gp.GdipDisposeImage.argtypes = [ctypes.c_void_p]
    gp.GdiplusShutdown.argtypes = [ctypes.c_size_t]
    token = ctypes.c_size_t()
    if gp.GdiplusStartup(ctypes.byref(token), ctypes.byref(GdiplusStartupInput(1, None, False, False)), None) != 0:
        return None
    try:
        image, hbm = ctypes.c_void_p(), wintypes.HBITMAP()
        if gp.GdipCreateBitmapFromFile(path, ctypes.byref(image)) != 0:
            return None
        try:
            ok = gp.GdipCreateHBITMAPFromBitmap(image, ctypes.byref(hbm), 0xFFFFFFFF) == 0   # 透明的地方垫白色
        finally:
            gp.GdipDisposeImage(image)
        return hbm.value if ok and hbm.value else None
    finally:
        gp.GdiplusShutdown(token.value)


def copy_image(path, hwnd):
    """把一张图片以位图的形式放进剪贴板（和截图工具复制出来的一样），在飞书、微信里 Ctrl+V 就是一张图片。
    复制成「文件」的话，飞书会当成文件附件或者不认。hwnd：自己的一个窗口（打开剪贴板时不给窗口，清空后就写不进去）。返回成没成。"""
    gdi = ctypes.WinDLL("gdi32")
    gdi.DeleteObject.argtypes = [wintypes.HGDIOBJ]
    hbm = load_bitmap(path)
    if not hbm:
        return False
    if not user32.OpenClipboard(hwnd):
        gdi.DeleteObject(hbm)
        return False
    try:
        user32.EmptyClipboard()
        if user32.SetClipboardData(2, hbm):   # CF_BITMAP；放进去以后归剪贴板管，不用自己删
            return True
        gdi.DeleteObject(hbm)
        return False
    finally:
        user32.CloseClipboard()


