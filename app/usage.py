"""Claude 的用量，和 Claude Code 里 /usage 看到的一样：5 小时额度、本周额度用了多少、什么时候重置。

用本机 Claude Code 自己的登录凭据（%USERPROFILE%\\.claude\\.credentials.json 里的 claudeAiOauth）去问 Anthropic 的
/api/oauth/usage —— Claude Code 的 /usage 问的就是这个接口（从它的程序里找到的，不是公开文档里的接口，以后可能会变）。
令牌只发给这个接口，不写日志、不往别处传。
令牌过期了不替它续：续期会换掉 Claude Code 存着的那一份，可能把它自己的登录弄掉。等 Claude Code 自己续上（用它干点活就会续）
就又能查到。没有这个文件（比如没登录过，或者用的是 API key）就什么都不显示。
"""
import json
import os
import time
import urllib.error
import urllib.request
from datetime import datetime

import install

URL = "https://api.anthropic.com/api/oauth/usage"
CRED = os.path.join(install.CLAUDE, ".credentials.json")
EVERY = 300          # 多久查一次（秒）
NAMES = {"session": "5 小时", "weekly_all": "本周"}
OLD = (("five_hour", "session", "5 小时"), ("seven_day", "weekly_all", "本周"),
       ("seven_day_opus", "weekly_opus", "本周 · Opus"), ("seven_day_sonnet", "weekly_sonnet", "本周 · Sonnet"))


def credentials():
    """（令牌, 问题）：问题是 none（没有凭据）/ expired（过期了）/ 空。"""
    try:
        with open(os.environ.get("AGENT_HUD_CREDENTIALS") or CRED, encoding="utf-8") as f:
            cred = json.load(f)["claudeAiOauth"]
        token = cred["accessToken"]
    except (OSError, ValueError, KeyError, TypeError):
        return None, "none"
    if (cred.get("expiresAt") or 0) / 1000 < time.time():
        return None, "expired"
    return token, ""


def stamp(text):
    try:
        return datetime.fromisoformat(str(text).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def parse(data):
    """接口返回的 -> [{key, label（5 小时 / 本周 / 本周 · Fable）, percent, resets（时间戳）}]，5 小时和本周在前。"""
    out = []
    for item in data.get("limits") or []:
        kind = item.get("kind") or ""
        model = ((item.get("scope") or {}).get("model") or {}).get("display_name")
        label = NAMES.get(kind) or ("本周 · %s" % model if model else kind)
        out.append({"key": kind, "label": label, "percent": float(item.get("percent") or 0), "resets": stamp(item.get("resets_at"))})
    if not out:   # 早一点的版本只有这几个字段
        for field, key, label in OLD:
            v = data.get(field)
            if isinstance(v, dict) and v.get("utilization") is not None:
                out.append({"key": key, "label": label, "percent": float(v["utilization"]), "resets": stamp(v.get("resets_at"))})
    order = {key: i for i, key in enumerate(NAMES)}
    return sorted(out, key=lambda u: order.get(u["key"], len(order)))   # 排序是稳定的，别的保持接口给的顺序


def fetch(timeout=15):
    """查一次。返回 {"limits": [...], "at": 时间} 或 {"error": 一句说明, "at": 时间}；没有凭据返回 None（什么都不显示）。"""
    token, problem = credentials()
    now = time.time()
    if problem == "none":
        return None
    if problem == "expired":
        return {"error": "Claude Code 的登录凭据过期了，用它干点活会自己续上", "at": now}
    req = urllib.request.Request(os.environ.get("AGENT_HUD_USAGE_URL") or URL,
                                 headers={"Authorization": "Bearer " + token, "anthropic-beta": "oauth-2025-04-20"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return {"error": "查不到用量（%d）" % e.code, "at": now}
    except (OSError, ValueError) as e:
        return {"error": "查不到用量（%s）" % (getattr(e, "reason", None) or e), "at": now}
    limits = parse(data) if isinstance(data, dict) else []
    return {"limits": limits, "at": now} if limits else {"error": "用量的格式认不出来", "at": now}


WEEKDAYS = "一二三四五六日"


def when(ts, now=None):
    """重置时间：今天的写「15:19」，明天写「明天 9:00」，再往后写「周二 22:59」。"""
    if not ts:
        return ""
    t, today = datetime.fromtimestamp(ts), datetime.fromtimestamp(now or time.time()).date()
    days = (t.date() - today).days
    clock = "%d:%02d" % (t.hour, t.minute)
    if days <= 0:
        return clock
    if days == 1:
        return "明天 " + clock
    return "周%s %s" % (WEEKDAYS[t.weekday()], clock)


def line(info, now=None):
    """一句话：「5 小时 24%（15:19 重置）· 本周 56%（周二 22:59 重置）」，给托盘提示和手机网页用。"""
    if not info:
        return ""
    if info.get("error"):
        return info["error"]
    return " · ".join("%s %d%%%s" % (u["label"], round(u["percent"]), "（%s 重置）" % when(u["resets"], now) if u["resets"] else "")
                      for u in info["limits"][:2])
