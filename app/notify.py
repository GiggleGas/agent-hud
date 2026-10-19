"""手机通知（可选，用户自己配）：任务做完 / 出错、或者在等你回答时，往用户自己的群机器人发一条消息，
手机上的飞书 / 钉钉 / 企业微信就会推送（锁屏也收得到）。网页自己没法推送：浏览器只给 https 的页面推送，局域网页面是 http。

地址填在「手机访问…」窗口里，只存在这台电脑的 config.json，不打进包。按地址的域名认是哪家，拼它认的格式。
消息开头是「【Agent悬浮窗】」，用户在机器人的安全设置里填关键词「悬浮窗」就能收。
"""
import json
import urllib.error
import urllib.request

import feedback

HEAD = "【Agent悬浮窗】"
KINDS = {"done": "✅ 做完了", "error": "❌ 出错了", "ask": "❓ 在等你回答"}


def payload(url, text):
    """按群机器人是谁家的拼它认的格式；不认识的地址收到普通 JSON。"""
    if "feishu.cn" in url or "larksuite.com" in url:
        return {"msg_type": "text", "content": {"text": text}}
    if "dingtalk.com" in url or "qyapi.weixin.qq.com" in url:
        return {"msgtype": "text", "text": {"content": text}}
    return {"text": text}


def message(kind, v, link=""):
    """一条通知的内容：做完了 / 出错了 / 在等你回答 · 项目 · 标题，下面是它最后说的一句（或者在问什么），再下面是链接。"""
    lines = ["%s%s · %s · %s" % (HEAD, KINDS.get(kind, kind), v.get("ws") or "?", v.get("title") or "")]
    said = (v.get("action") if kind == "ask" else v.get("last_text")) or ""
    said = " ".join(said.split())
    if said:
        lines.append(said[:200] + ("…" if len(said) > 200 else ""))
    if link:
        lines.append("在手机上看：" + link)
    return "\n".join(lines)


def send(url, text, timeout=10):
    """发一条，返回（成没成, 一句说明）。"""
    req = urllib.request.Request(url, data=json.dumps(payload(url, text), ensure_ascii=False).encode("utf-8"),
                                 headers={"Content-Type": "application/json; charset=utf-8"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            err = feedback.rejected(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        err = feedback.rejected(e.read().decode("utf-8", "replace")) or "对方回了 %d" % e.code
    except (OSError, ValueError) as e:
        err = "连不上（%s）" % (getattr(e, "reason", None) or e)
    return (False, "没发出去：" + err) if err else (True, "发出去了")
