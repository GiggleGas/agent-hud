"""检查有没有新版本：读作者在飞书多维表格里维护的「发版」表（用包里那个飞书应用、只读），比当前版本新就提醒。

表的地址在 feedback_to.py 的 UPDATE_TABLE（多维表格的链接，形如 https://xxx.feishu.cn/base/<app_token>?table=<table_id>）。
表里每发一版加一行，列：版本（比如 3.3）、说明（更新了什么）、安装包（附件，把 build 里的 agent-hud-<版本>.zip 拖进去）、下载（可选，链接）。
版本号最大的那一行就是最新版。
应用要有「查看多维表格」（bitable:app:readonly）和「下载云文档中的图片和附件」（docs:document.media:download）两项权限，
并且这张表要分享给应用（表的「…」→「添加文档应用」）。查不到（没联网、没权限、表格式不对）就当没有新版本，不打扰用户。

一键更新（用户在「有新版本」窗口里点「立即更新」）：download 用应用下载附件 → prepare 解压到
%USERPROFILE%\\.claude\\agent-hud\\update\\<版本>\\ 并核对 → launch 起一个更新脚本、悬浮窗退出 → 脚本等悬浮窗退干净，
把新文件换上（exe 版换 exe 和 _internal，脚本版换 app 里的程序文件），再把新版本打开。正在跑的 exe 换不了自己，所以要借脚本。
"""
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
import zipfile
from urllib.parse import urlparse, parse_qs

import feedback
import feedback_to
import install

EVERY = 6 * 3600     # 多久查一次
FIRST = 20           # 启动后多久查第一次（秒），别和启动时的一堆事挤在一起
FIELDS = {"version": ("版本", "version"), "notes": ("说明", "更新说明", "notes"), "link": ("下载", "下载链接", "link")}


def table():
    """（app_token, table_id）；没配或者认不出来是（"", ""）。"""
    url = os.environ.get("AGENT_HUD_UPDATE_TABLE") or getattr(feedback_to, "UPDATE_TABLE", "") or ""   # 环境变量：测试用
    m = re.search(r"/base/([A-Za-z0-9]+)", urlparse(url).path)
    tid = (parse_qs(urlparse(url).query).get("table") or [""])[0]
    return (m.group(1), tid) if m and tid else ("", "")


def ready():
    return feedback.app_ready() and all(table())


def version_tuple(v):
    """「3.3」「v3.10.1」-> (3, 3) / (3, 10, 1)；末尾的 0 不算，「1.0」和「1.0.0」是同一个版本 (1,)；
    认不出来是空元组（比谁都旧）。"""
    nums = [int(n) for n in re.findall(r"\d+", str(v or ""))]
    while nums and nums[-1] == 0:
        nums.pop()
    return tuple(nums)


def text(value):
    """多维表格单元格的值换成文字：文本列是 [{type, text}] 片段，链接列是 {link, text}，有的直接是字符串。"""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return str(value.get("link") or value.get("text") or "")
    if isinstance(value, list):
        return "".join(text(v) for v in value)
    return str(value)


def pick(fields, key):
    for name in FIELDS[key]:
        if name in fields:
            return text(fields[name]).strip()
    return ""


def token():
    return feedback.api("POST", "/auth/v3/tenant_access_token/internal",
                        body={"app_id": feedback_to.APP_ID, "app_secret": feedback_to.APP_SECRET})["tenant_access_token"]


def package(fields):
    """「安装包」附件列里的第一个文件：{token, name, size, url}；没有附件返回 None。"""
    for name in ("安装包", "package"):
        for f in fields.get(name) or []:
            if isinstance(f, dict) and f.get("file_token"):
                return {"token": f["file_token"], "name": f.get("name") or "agent-hud.zip", "size": f.get("size") or 0,
                        "url": f.get("url") or ""}
    return None


def latest():
    """表里最新的那一版：{version, notes, link, package}；表是空的返回 None。出错抛 feedback.AppError。"""
    app_token, table_id = table()
    items = feedback.api("GET", "/bitable/v1/apps/%s/tables/%s/records?page_size=100" % (app_token, table_id),
                         token()).get("data", {}).get("items") or []
    rows = [dict({k: pick(it.get("fields") or {}, k) for k in FIELDS}, package=package(it.get("fields") or {})) for it in items]
    rows = [r for r in rows if version_tuple(r["version"])]
    return max(rows, key=lambda r: version_tuple(r["version"])) if rows else None


# ---------- 一键更新 ----------
STAGE = os.path.join(install.TARGET, "update")
EXE = "Agent悬浮窗.exe"


def download(info, progress=None, timeout=60):
    """用飞书应用下载「安装包」附件，存成 update\\<版本>.zip，返回路径。progress(已下载, 总共) 字节。出错抛 AppError。"""
    pack = info.get("package")
    if not pack:
        raise feedback.AppError("这一版没有放安装包")
    base = os.environ.get("AGENT_HUD_FEISHU_BASE") or feedback.FEISHU
    url = pack["url"] or "%s/drive/v1/medias/%s/download" % (feedback.FEISHU, pack["token"])
    url = url.replace(feedback.FEISHU, base)   # 测试时指到假飞书
    os.makedirs(STAGE, exist_ok=True)
    path = os.path.join(STAGE, "%s.zip" % re.sub(r"[^\w.]", "_", info["version"]))
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + token()})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r, open(path, "wb") as f:
            total, done = int(r.headers.get("Content-Length") or pack["size"] or 0), 0
            while True:
                chunk = r.read(64 << 10)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                if progress:
                    progress(done, total)
    except urllib.error.HTTPError as e:
        raise feedback.AppError(feedback.rejected(e.read().decode("utf-8", "replace")) or "下载失败（%d）" % e.code)
    except OSError as e:
        raise feedback.AppError("下载失败（%s）" % (getattr(e, "reason", None) or e))
    return path


def prepare(path, version):
    """解压到 update\\<版本>\\ 并核对：是个 zip，里面是悬浮窗的包（exe 版要有 exe 和 _internal，脚本版要有 app），
    包里程序的版本号就是要更新到的那个。返回包的根目录（解压出来的 agent-hud 文件夹）。不对就抛 AppError。"""
    if not zipfile.is_zipfile(path):
        raise feedback.AppError("下载下来的不是 zip 包")
    out = os.path.join(STAGE, re.sub(r"[^\w.]", "_", version))
    shutil.rmtree(out, ignore_errors=True)
    with zipfile.ZipFile(path) as z:
        z.extractall(out)
    root = next((os.path.join(out, d) for d in os.listdir(out) if os.path.isdir(os.path.join(out, d, "app"))), out)
    need = [EXE, "_internal"] if install.FROZEN else [os.path.join("app", "agent_hud.pyw")]
    missing = [n for n in need if not os.path.exists(os.path.join(root, n))]
    if missing:
        raise feedback.AppError("包里缺东西：%s" % "、".join(missing))
    try:
        with open(os.path.join(root, "app", "agent_hud.pyw"), encoding="utf-8") as f:
            found = re.search(r'^VERSION = "([^"]+)"', f.read(), re.M)
    except OSError:
        found = None
    if found and version_tuple(found.group(1)) != version_tuple(version):
        raise feedback.AppError("包里的版本是 %s，不是 %s" % (found.group(1), version))
    return root


# 更新脚本：只用英文（PowerShell 5.1 读不带 BOM 的脚本会把中文读乱），路径都从参数传进来。
UPDATER = r"""param([int]$Wait, [string]$Src, [string]$Dst, [string]$Mode, [string]$Run, [string]$RunArgs, [string]$Log)
function Say($t) { Add-Content -Path $Log -Value ((Get-Date -Format 'yyyy-MM-dd HH:mm:ss') + ' ' + $t) -Encoding UTF8 }
Say "waiting for $Wait"
try { Wait-Process -Id $Wait -Timeout 30 -ErrorAction Stop } catch {}
Start-Sleep -Milliseconds 800
$bad = 0
for ($i = 0; $i -lt 10; $i++) {
  if ($Mode -eq 'exe') {
    robocopy $Src $Dst /E /XD _internal /R:2 /W:1 /NFL /NDL /NJH /NJS /NP | Out-Null
    $a = $LASTEXITCODE
    robocopy (Join-Path $Src '_internal') (Join-Path $Dst '_internal') /MIR /R:2 /W:1 /NFL /NDL /NJH /NJS /NP | Out-Null
    $b = $LASTEXITCODE
  } else {
    robocopy (Join-Path $Src 'app') $Dst /E /R:2 /W:1 /NFL /NDL /NJH /NJS /NP | Out-Null
    $a = $LASTEXITCODE
    $b = 0
  }
  $bad = [int]($a -ge 8 -or $b -ge 8)
  if (-not $bad) { break }
  Say "copy failed ($a, $b), retry"
  Start-Sleep -Seconds 1
}
if ($bad) { Say 'copy failed' } else { Say 'copy ok' }
if ($RunArgs) { Start-Process -FilePath $Run -ArgumentList ('"' + $RunArgs + '"') -WorkingDirectory $Dst } else { Start-Process -FilePath $Run -WorkingDirectory $Dst }
Say 'started'
"""


def launch(root, wait_pid=None, mode=None, dst=None, command=None):
    """起更新脚本（独立进程，不跟着悬浮窗退出）：等 wait_pid（悬浮窗自己）退干净，把 root 里的新文件换上，再把悬浮窗打开。
    mode / dst / command（启动用的程序和参数）默认按现在是 exe 版还是脚本版来定，测试时可以换。"""
    os.makedirs(STAGE, exist_ok=True)
    script = os.path.join(STAGE, "updater.ps1")
    with open(script, "w", encoding="ascii") as f:
        f.write(UPDATER)
    run, args = command or install.launch_command()
    mode = mode or ("exe" if install.FROZEN else "script")
    dst = dst or (os.path.dirname(sys.executable) if install.FROZEN else install.TARGET)
    cmd = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden", "-File", script,
           "-Wait", str(wait_pid or os.getpid()), "-Src", root, "-Dst", dst, "-Mode", mode, "-Run", run,
           "-RunArgs", args.strip('"'), "-Log", os.path.join(STAGE, "update.log")]
    # 不能用 DETACHED_PROCESS：PowerShell 是控制台程序，完全没有控制台时一启动就退出（实测）。不开窗口的控制台就行，
    # 父进程（悬浮窗）退出也不会带走它
    flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    return subprocess.Popen(cmd, close_fds=True, creationflags=flags, stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def cleanup():
    """更新成了以后删掉下载的包、解压出来的文件夹和更新脚本（每更新一次几十 MB），只留 update.log 好查。"""
    try:
        names = os.listdir(STAGE)
    except OSError:
        return
    for name in names:
        path = os.path.join(STAGE, name)
        if name == "update.log":
            continue
        if os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)
        else:
            try:
                os.remove(path)
            except OSError:
                pass


def check(current, skip=""):
    """有比 current 新、而且不是用户说过「先不提醒」的版本，返回它；没有（或者查不到）返回 None。"""
    if not ready():
        return None
    try:
        row = latest()
    except (feedback.AppError, OSError, ValueError):
        return None
    if row and version_tuple(row["version"]) > version_tuple(current) and row["version"] != skip:
        return row
    return None
