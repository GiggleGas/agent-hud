"""把悬浮窗打包成不需要 Python 的 exe：上一级目录里的「Agent悬浮窗.exe」和它旁边的运行库文件夹「_internal」。

不打成单个文件：单文件 exe 每次启动都要把近千个文件解包到临时目录，机器在编译、杀毒软件在扫的时候
实测要 8～18 秒才出窗口；带文件夹的打法不用解包，0.4 秒。代价是 exe 不能离开 _internal 单独拿走。

第一次运行会在 dev/.venv-build 里建一个独立环境并装 PyInstaller（要联网），不动全局的 Python。
exe 没有数字签名：别的电脑上第一次运行时 Windows 可能提示「已保护你的电脑」，个别杀毒软件会拦。
打完跑 test_exe.py 和 test_window.py <exe>。
"""
import os, re, shutil, subprocess, sys, tempfile

sys.stdout.reconfigure(encoding="utf-8")
DEV = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(DEV)
APP = os.path.join(ROOT, "app")
VENV = os.path.join(DEV, ".venv-build")
PY = os.path.join(VENV, "Scripts", "python.exe")
OUT = os.path.join(ROOT, "Agent悬浮窗.exe")

# exe 的「属性 - 详细信息」里显示的内容，版本号取自 agent_hud.pyw
VERSION_INFO = """VSVersionInfo(
  ffi=FixedFileInfo(filevers=%(nums)r, prodvers=%(nums)r, mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('080404b0', [
      StringStruct('FileDescription', 'Agent 悬浮窗'),
      StringStruct('ProductName', 'Agent 悬浮窗'),
      StringStruct('FileVersion', '%(version)s'),
      StringStruct('ProductVersion', '%(version)s'),
      StringStruct('OriginalFilename', 'Agent悬浮窗.exe'),
    ])]),
    VarFileInfo([VarStruct('Translation', [2052, 1200])])
  ]
)
"""

with open(os.path.join(APP, "agent_hud.pyw"), encoding="utf-8") as f:
    version = re.search(r'^VERSION = "([\d.]+)"', f.read(), re.M).group(1)
nums = tuple(int(x) for x in (version.split(".") + ["0", "0", "0"])[:4])

if not os.path.exists(PY):
    print("建打包用的独立环境，装 PyInstaller…")
    subprocess.run([sys.executable, "-m", "venv", VENV], check=True)
    subprocess.run([PY, "-m", "pip", "install", "--quiet", "--disable-pip-version-check", "pyinstaller"], check=True)

work = tempfile.mkdtemp(prefix="agent-hud-build-")
info = os.path.join(work, "version.txt")
with open(info, "w", encoding="utf-8") as f:
    f.write(VERSION_INFO % {"nums": nums, "version": version})
# 名字先用英文，打完再改成中文名，免得打包工具在中文名上出岔子
subprocess.run([PY, "-m", "PyInstaller", "--noconfirm", "--clean", "--log-level", "WARN", "--onedir", "--windowed",
                "--name", "AgentHUD", "--icon", os.path.join(DEV, "icon.ico"), "--version-file", info,
                "--exclude-module", "PIL",   # 只有 --shot 截图用得到，exe 里不带
                "--distpath", os.path.join(work, "dist"), "--workpath", os.path.join(work, "build"), "--specpath", work,
                os.path.join(APP, "agent_hud.pyw")], check=True)
LIBS = os.path.join(ROOT, "_internal")
if os.path.exists(OUT):
    os.remove(OUT)
shutil.rmtree(LIBS, ignore_errors=True)
shutil.move(os.path.join(work, "dist", "AgentHUD", "AgentHUD.exe"), OUT)
shutil.move(os.path.join(work, "dist", "AgentHUD", "_internal"), LIBS)
shutil.rmtree(work, ignore_errors=True)
count = sum(len(files) for _, _, files in os.walk(LIBS))
size = sum(os.path.getsize(os.path.join(b, f)) for b, _, files in os.walk(LIBS) for f in files)
print("%s  版本 %s  %.1f MB；_internal %d 个文件 %.1f MB" % (OUT, version, os.path.getsize(OUT) / 1048576, count, size / 1048576))
