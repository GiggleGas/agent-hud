"""项目（workspace）清单，以及判断一条路径、一段命令属于哪个项目。

清单存在 ~/.claude/agent-hud/workspaces.json，可以手改：
    [{"name": "night-channel", "path": "F:\\AIDemo\\Something\\night-channel", "color": "#4c9aff"}, ...]
name 和 color 改了会保留；删掉一条的话，下次有对话从那个目录启动还会被自动加回来。

自动发现的规则：对话的启动目录自己没有工程标志、下面却有两个以上带工程标志的子文件夹时，
把它当成「装项目的文件夹」，它自己和每个子文件夹各算一个项目；否则启动目录自己算一个项目。
"""
import fnmatch
import json
import os
import re
import threading

import install

FILE = os.path.join(install.TARGET, "workspaces.json")
MARKERS = ("project.godot", ".git", "package.json", "pyproject.toml", "Cargo.toml", "go.mod", "pom.xml",
           ".p4config", "*.uproject", "*.sln", "*.csproj")
PALETTE = ("#4c9aff", "#3ecf8e", "#f5a623", "#b48cff", "#ff7eb6", "#2dd4bf", "#f97316", "#a3e635", "#e879f9", "#facc15")


def norm(path):
    """路径统一成小写、正斜杠、不带结尾斜杠；Git Bash 的 /f/xx 写法换成 f:/xx。"""
    p = str(path).replace("\\", "/").rstrip("/").lower()
    m = re.match(r"^/([a-z])(/.*)?$", p)
    return "%s:%s" % (m.group(1), m.group(2) or "") if m else p


def has_marker(folder):
    try:
        names = os.listdir(folder)
    except OSError:
        return False
    return any(fnmatch.fnmatch(n, pat) for n in names for pat in MARKERS)


def discover(root):
    """一个启动目录带来哪些项目目录。"""
    home = norm(os.path.expanduser("~"))
    if not os.path.isdir(root) or norm(root) == home or len(norm(root)) <= 3:   # 主目录、盘符根目录不展开
        return [root]
    try:
        kids = [os.path.join(root, d) for d in sorted(os.listdir(root))
                if d[0] not in "._$" and os.path.isdir(os.path.join(root, d))]
    except OSError:
        return [root]
    if not has_marker(root) and sum(1 for k in kids if has_marker(k)) >= 2:
        return [root] + kids
    return [root]


class Registry:
    def __init__(self, file=None):
        self.file = file or FILE
        self.lock = threading.Lock()
        self.items = []        # {"name", "path", "color", "key"}，key 是 norm 过的路径
        self.roots = set()     # 已经展开过的启动目录
        try:
            with open(self.file, encoding="utf-8") as f:
                for it in json.load(f):
                    self._add(it["path"], it.get("name"), it.get("color"))
        except (OSError, ValueError, KeyError, TypeError):
            pass

    def _add(self, path, name=None, color=None):
        key = norm(path)
        if any(it["key"] == key for it in self.items):
            return False
        name = name or os.path.basename(path.rstrip("\\/")) or path
        if any(it["name"].lower() == name.lower() for it in self.items):   # 重名就带上上一级目录
            name = "%s/%s" % (os.path.basename(os.path.dirname(path.rstrip("\\/"))), name)
        # 在命令里认这个项目：路径后面不能紧跟着字母数字，免得 agent-hud 认成 agent-hud-old
        rx = re.compile("(?:%s|/%s%s)(?![\\w.-])" % (re.escape(key), re.escape(key[:1]), re.escape(key[2:])))
        self.items.append({"name": name, "path": path, "key": key, "rx": rx,
                           "color": color or PALETTE[len(self.items) % len(PALETTE)]})
        return True

    def save(self):
        try:
            os.makedirs(os.path.dirname(self.file), exist_ok=True)
            with open(self.file, "w", encoding="utf-8") as f:
                json.dump([{k: it[k] for k in ("name", "path", "color")} for it in self.items], f, ensure_ascii=False, indent=1)
        except OSError:
            pass

    def ensure(self, root):
        """第一次见到一个启动目录时，把它带来的项目登记进清单。"""
        if not root or norm(root) in self.roots:
            return
        with self.lock:
            self.roots.add(norm(root))
            if sum([self._add(p) for p in discover(root)]):
                self.save()

    def add(self, path):
        """手动加一个项目（右键「归到项目 → 选别的文件夹…」）；已经在清单里就不重复加。返回清单里的这一项。"""
        with self.lock:
            if self._add(path):
                self.save()
            return next(it for it in self.items if it["key"] == norm(path))

    def match(self, path):
        """这条路径在哪个项目里（取最深的那个）；不在任何项目里返回 None。"""
        p = norm(path)
        best = None
        for it in self.items:
            if (p == it["key"] or p.startswith(it["key"] + "/")) and (best is None or len(it["key"]) > len(best["key"])):
                best = it
        return best

    def find_in(self, text):
        """一段命令里提到了哪些项目（按路径出现算；提到子项目就不再算它的上级）。"""
        t = str(text).replace("\\", "/").lower()
        hits = [it for it in self.items if it["rx"].search(t)]
        return [it for it in hits if not any(o is not it and o["key"].startswith(it["key"] + "/") for o in hits)]

    def by_name(self, name):
        """按名字找项目：先完全相同，再看是不是唯一一个以它开头 / 包含它的。大小写不敏感。"""
        n = name.strip().lower()
        if not n:
            return None
        for test in (lambda s: s == n, lambda s: s.startswith(n), lambda s: n in s):
            found = [it for it in self.items if test(it["name"].lower())]
            if len(found) == 1:
                return found[0]
            if found:
                return None
        return None
