"""打包：把要发给别人的文件压成 build\\agent-hud-<版本>.zip（顶层是 agent-hud 文件夹），旧版本的包留在旁边。

dev、build 目录、缓存、本机的设置和日志、git 仓库的东西不进包。发之前先跑 test_collector.py 和 test_install.py。
app\\feedback_to.py（群机器人地址、飞书应用密钥）不在仓库里，但要进包：没有它就停下来。
"""
import os, re, sys, zipfile

sys.stdout.reconfigure(encoding="utf-8")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
with open(os.path.join(ROOT, "app", "agent_hud.pyw"), encoding="utf-8") as f:
    VERSION = re.search(r'^VERSION = "([\d.]+)"', f.read(), re.M).group(1)
BUILD = os.path.join(ROOT, "build")
OUT = os.path.join(BUILD, "agent-hud-%s.zip" % VERSION)
SKIP_DIRS = {"dev", "build", "__pycache__", ".git"}
SKIP_FILES = {"config.json", "hud.log", ".gitignore", ".gitattributes", "README.md"}

if not os.path.isfile(os.path.join(ROOT, "app", "feedback_to.py")):
    sys.exit("缺 app\\feedback_to.py：照 dev\\feedback_to_example.py 建一个，填上群机器人地址和飞书应用，再打包")
os.makedirs(BUILD, exist_ok=True)
with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED) as z:
    for base, dirs, files in os.walk(ROOT):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
        for name in sorted(files):
            if name not in SKIP_FILES:
                path = os.path.join(base, name)
                arc = "agent-hud/" + os.path.relpath(path, ROOT).replace(os.sep, "/")
                z.write(path, arc)
                print("  ", arc)
print("%s  %.1f KB" % (OUT, os.path.getsize(OUT) / 1024))
