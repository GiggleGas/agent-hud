"""手机网页：在同一个局域网里，用手机浏览器看各个 agent 在干什么、它最后说了什么，并且能回复它。

悬浮窗开着、并且在「手机访问…」里开启了，才会在后台起这个小网页服务（默认 8765 端口）。
地址里带一把钥匙，第一次用带钥匙的地址打开后浏览器会记住；没有钥匙的请求一律拒绝——公司网络里
别人也连得上这个端口，而有钥匙的人能让电脑替你打字。

回复怎么送进去：
- 悬浮窗 / 手机发起的后台任务，而且编辑器里没开着它：直接接着跑（runner.follow）。
- 别的（编辑器里开着的对话）：在电脑上替你打进编辑器里那个对话（runner.type_into_editor），电脑要开着、没锁屏。
"""
import json
import os
import secrets
import socket
import threading
import time
import traceback
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import collector
import runner
import usage
from collector import LIVE, first_line

PORT = 8765
# 对局域网开放。测试时只开在本机：新的程序第一次对外开端口，Windows 会弹防火墙的对话框
HOST = "127.0.0.1" if os.environ.get("AGENT_HUD_TEST_ROOT") else "0.0.0.0"


def lan_ip():
    """这台电脑在局域网里的地址（手机要用它来连）。"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))   # 不会真的发包，只是让系统选出走局域网的那块网卡
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "127.0.0.1"


class Web:
    def __init__(self, hud, log=None):
        self.hud = hud
        self.log = log or (lambda text: None)
        self.server = None
        self.port = None
        self.jobs = {}     # 回复的编号 -> {"state": sending / sent / failed, "message": 一句说明}
        self.misses = {}   # 来访地址 -> [钥匙不对的次数, 第一次的时间]

    # ---------- 开关 ----------
    def key(self):
        if not self.hud.cfg.get("web_key"):
            self.hud.cfg["web_key"] = secrets.token_hex(4)
            self.hud.save()
        return self.hud.cfg["web_key"]

    def new_key(self):
        self.hud.cfg["web_key"] = ""
        return self.key()

    def url(self):
        return "http://%s:%d/?k=%s" % (lan_ip(), self.port or self.hud.cfg.get("web_port", PORT), self.key())

    def start(self):
        """起网页服务；端口被占了就往后找几个。返回成没成。"""
        if self.server:
            return True
        web = self
        base = self.hud.cfg.get("web_port", PORT)
        for port in range(base, base + 10):
            try:
                server = ThreadingHTTPServer((HOST, port),type("Handler", (Handler,), {"web": web}))
                break
            except OSError:
                continue
        else:
            self.log("手机访问：%d 到 %d 的端口都被占了" % (base, base + 9))
            return False
        server.daemon_threads = True
        self.server, self.port = server, port
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.log("手机访问：已开启 " + self.url().split("?")[0])
        return True

    def stop(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.server = None

    # ---------- 数据 ----------
    def item(self, v, unread):
        m = v.get("marker")
        return {"id": v["id"], "title": v["title"], "ws": v["ws"], "color": v["ws_color"], "state": v["state"],
                "owned": bool(v.get("owned")), "new": v["id"] in unread, "marker": list(m) if m else None,
                "action": v.get("action") or v.get("line") or "",
                "bg": [{k: b[k] for k in ("desc", "start", "tail", "resident")} for b in v.get("bg") or []],   # 还在跑的后台命令
                "start": v["start"], "end": v["end"], "when": self.hud.when(v)}

    def state(self):
        hud, now = self.hud, time.time()
        unread = {u["id"] for u in hud.unread}
        live = sorted([v for v in hud.views if v["state"] in LIVE and not collector.is_codex(v)], key=lambda v: v["start"])
        recent = sorted([v for v in hud.ended_items() if now - hud.when(v) <= 86400 and not collector.is_codex(v)], key=lambda v: -hud.when(v))
        projects = []
        for g in hud.groups():
            items = [v for v in g["items"] if not collector.is_codex(v)]
            if items:
                projects.append({"name": g["name"], "color": g["color"],
                                 "live": sum(v["state"] in LIVE for v in items),
                                 "new": sum(v["id"] in unread for v in items if v["state"] not in LIVE),
                                 "items": [self.item(v, unread) for v in items[:30]]})
        return {
            "now": now,
            "live": [self.item(v, unread) for v in live],
            "recent": [self.item(v, unread) for v in recent],
            "projects": projects,
            "workspaces": [it["name"] for it in hud.reg.items],
            "usage": usage.line(hud.usage),   # Claude 的用量，一句话；查不到是空的
        }

    def conversation(self, sid):
        v = self.hud.find(sid)
        if not v:
            return None
        if collector.is_codex(v):
            return None
        self.hud.events.put(("read", {"id": sid}, False))   # 在手机上看过了，就算已读
        out = self.item(v, set())
        out["messages"] = collector.conversation(sid)
        out["background"] = bool(v.get("owned"))
        out["question"] = v.get("question")   # 在等你回答的选择题：手机上显示成能点的选项
        return out

    @staticmethod
    def answer_text(questions, picks):
        """手机上点的选项 -> 发给它的一句话。picks 和 questions 一一对应：{"labels": [选中的选项], "other": 自己写的}。"""
        lines = ["我回答你刚才的问题："]
        for q, p in zip(questions, picks):
            chosen = [str(x) for x in (p.get("labels") or [])] + ([p["other"].strip()] if (p.get("other") or "").strip() else [])
            if chosen:
                lines.append("· %s → %s" % (q.get("question") or q.get("header") or "", "、".join(chosen)))
        return "\n".join(lines) if len(lines) > 1 else ""

    def answer(self, sid, tool_id, picks):
        """回答选择题：在电脑上打开那个对话，按 Esc 撤下提问，再把选择当成一句话发过去（runner.answer_in_editor）。"""
        v = self.hud.find(sid)
        question = (v or {}).get("question")
        if not question or question["tool_id"] != tool_id:
            raise ValueError("这个问题已经不在等回答了（可能在电脑上答过了）")
        text = self.answer_text(question["questions"], picks)
        if not text:
            raise ValueError("还没选")
        return self.reply(sid, text, tool_id)

    def reply(self, sid, text, tool_id=None):
        """回复一句话；tool_id 不为空表示这是在回答那道选择题。"""
        if collector.is_codex(self.hud.find(sid) or {"id": sid}):
            raise ValueError("Codex 目前仅支持在悬浮窗中查看状态，请在 Codex 中回复。")
        job = uuid.uuid4().hex[:8]
        self.jobs[job] = {"state": "sending", "message": "正在发送…"}

        def work():
            try:
                v = self.hud.find(sid)
                path = collector.find_transcript(sid)
                if not v:
                    ok, msg = False, "找不到这个对话了。"
                elif tool_id:
                    ok, msg = (runner.answer_in_editor(sid, v.get("root") or v.get("ws_path") or "", tool_id, text, path) if path
                               else (False, "找不到这个对话的记录文件。"))
                elif v["state"] in ("ask", "confirm"):   # 这时候按回车可能被当成「同意」，不替你按
                    ok, msg = False, "它正在等你确认权限或批准计划，这种时候不替你在电脑上按回车。回到电脑前处理一下。"
                elif v.get("owned") and not runner.editor_holds(sid):
                    queued = self.hud.runner.follow(sid, text) == "queued"
                    ok, msg = True, "上一轮还没跑完，这句已经排队，跑完自动发。" if queued else "已经发给后台任务，它接着跑了。"
                elif not path:
                    ok, msg = False, "找不到这个对话的记录文件。"
                else:
                    ok, msg = runner.type_into_editor(sid, v.get("root") or v.get("ws_path") or "", text, path)
            except Exception as e:
                self.log("手机回复出错：" + traceback.format_exc())
                ok, msg = False, "出错了：%s" % e
            self.jobs[job] = {"state": "sent" if ok else "failed", "message": msg}
            self.log("手机回复 %s：%s %s" % (sid, "成功" if ok else "没成", msg))

        threading.Thread(target=work, daemon=True).start()
        return job

    def new_task(self, ws_name, text):
        ws = self.hud.reg.by_name(ws_name)
        if not ws:
            raise ValueError("没有这个项目：%s" % ws_name)
        return self.hud.runner.start(ws["path"], text, "web")


class Handler(BaseHTTPRequestHandler):
    web = None   # 起服务时换成真的 Web

    def log_message(self, *args):
        pass

    def send(self, code, body, kind="application/json; charset=utf-8", headers=()):
        data = body if isinstance(body, bytes) else (body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for name, value in headers:
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(data)

    def allowed(self, query):
        """钥匙对不对：看地址里的 k，或者浏览器记住的 cookie。错太多次的地址先挡十分钟。"""
        ip, web = self.client_address[0], self.web
        miss = web.misses.get(ip)
        if miss and miss[0] >= 20 and time.time() - miss[1] < 600:
            return False
        cookies = dict(p.strip().split("=", 1) for p in (self.headers.get("Cookie") or "").split(";") if "=" in p)
        given = (query.get("k") or [""])[0] or cookies.get("hudk", "")
        if given and secrets.compare_digest(given, web.key()):
            return True
        if not miss or time.time() - miss[1] >= 600:
            miss = [0, time.time()]
        web.misses[ip] = [miss[0] + 1, miss[1]]
        return False

    def do_GET(self):
        url = urlparse(self.path)
        query = parse_qs(url.query)
        if not self.allowed(query):
            self.send(403, LOCKED, "text/html; charset=utf-8")
            return
        if url.path == "/":
            if "k" in query:   # 第一次用带钥匙的地址打开：记进 cookie，再跳到不带钥匙的地址，免得钥匙留在历史记录里
                self.send(302, b"", headers=(("Set-Cookie", "hudk=%s; Path=/; Max-Age=2592000; HttpOnly; SameSite=Strict" % query["k"][0]),
                                             ("Location", "/")))
            else:
                self.send(200, PAGE, "text/html; charset=utf-8")
        elif url.path == "/api/state":
            self.send(200, self.web.state())
        elif url.path == "/api/conv":
            out = self.web.conversation((query.get("id") or [""])[0])
            self.send(200 if out else 404, out or {"error": "找不到这个对话"})
        elif url.path == "/api/job":
            self.send(200, self.web.jobs.get((query.get("id") or [""])[0], {"state": "failed", "message": "没有这次回复的记录"}))
        else:
            self.send(404, {"error": "没有这个地址"})

    def do_POST(self):
        url = urlparse(self.path)
        if not self.allowed(parse_qs(url.query)):
            self.send(403, {"error": "钥匙不对"})
            return
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode("utf-8") or "{}")
            if url.path == "/api/answer":
                self.send(200, {"job": self.web.answer(body.get("id") or "", body.get("tool_id") or "", body.get("picks") or [])})
                return
            text = (body.get("text") or "").strip()
            if not text:
                raise ValueError("没写内容")
            if url.path == "/api/reply":
                self.send(200, {"job": self.web.reply(body.get("id") or "", text)})
            elif url.path == "/api/new":
                self.send(200, {"id": self.web.new_task(body.get("ws") or "", text)})
            else:
                self.send(404, {"error": "没有这个地址"})
        except Exception as e:
            self.send(400, {"error": str(e)})


LOCKED = """<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Agents</title><body style="background:#15171c;color:#e8eaf0;font:16px system-ui,sans-serif;padding:24px">
<p>需要带钥匙的地址才能打开。</p><p style="color:#9aa1b0">在电脑上右键悬浮窗 →「手机访问…」，扫码或者复制那里的链接。</p></body>"""

PAGE = r"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>Agents</title>
<style>
:root{--bg:#15171c;--card:#1e2129;--edge:#30343f;--text:#e8eaf0;--dim:#9aa1b0;--faint:#636a79;--run:#4c9aff;--ask:#f5a623;--done:#3ecf8e;--err:#ef5350;--track:#383d4a}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:15px/1.5 -apple-system,"PingFang SC","Microsoft YaHei",system-ui,sans-serif;-webkit-text-size-adjust:100%}
header{position:sticky;top:0;z-index:2;background:var(--bg);padding:12px 16px 0;border-bottom:1px solid var(--edge)}
.top{display:flex;align-items:center;gap:8px}.top h1{font-size:17px;margin:0}.sum{color:var(--dim);font-size:13px;flex:1;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
button{font:inherit;color:inherit;background:none;border:0;padding:0;cursor:pointer}
.plus{font-size:22px;color:var(--dim);width:36px;height:36px}
.tabs{display:flex;gap:22px;margin-top:8px}.tabs button{padding:6px 0 8px;color:var(--faint);border-bottom:2px solid transparent}.tabs button.on{color:var(--text);border-color:var(--run)}
main{padding:10px 12px 120px}.empty{color:var(--faint);padding:24px 6px}
.card{background:var(--card);border-radius:10px;padding:10px 12px;margin:8px 0}
.row{display:flex;align-items:center;gap:8px}.title{font-weight:600;flex:1;min-width:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.chip{font-size:12px;padding:1px 7px;border-radius:5px;white-space:nowrap;max-width:40%;overflow:hidden;text-overflow:ellipsis}
.dot{width:8px;height:8px;border-radius:50%;flex:none}.new{width:6px;height:6px;border-radius:50%;background:var(--done);flex:none}
.muted{color:var(--dim);font-size:13px}.faint{color:var(--faint);font-size:12px;white-space:nowrap}
.line{color:var(--dim);font-size:13px;margin-top:4px;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.bar{height:5px;background:var(--track);border-radius:3px;margin-top:8px;overflow:hidden;display:flex;gap:2px}.bar i{display:block;flex:1;background:var(--track)}.bar i.d{background:var(--run)}
.sec{color:var(--faint);font-size:12px;margin:16px 4px 4px}
.item{padding:10px 4px;border-bottom:1px solid var(--edge)}.item:last-child{border:0}
.proj{display:flex;align-items:center;gap:8px;padding:12px 4px;font-weight:600}.proj .n{margin-left:auto;font-weight:400;font-size:13px}
.back{color:var(--run);padding:6px 0}
.msg{margin:10px 0;padding:10px 12px;border-radius:10px;word-wrap:break-word;overflow-wrap:anywhere}.msg.user{background:#24324a;margin-left:12%}.msg.assistant{background:var(--card);margin-right:4%}
.msg pre{background:#0f1115;padding:8px;border-radius:6px;overflow-x:auto;font-size:12.5px;white-space:pre}.msg code{background:#0f1115;padding:0 4px;border-radius:4px;font-size:13px}
.steps{margin-top:6px}.steps summary{color:var(--faint);font-size:12px}
.reply{position:fixed;left:0;right:0;bottom:0;background:var(--bg);border-top:1px solid var(--edge);padding:8px 12px calc(8px + env(safe-area-inset-bottom))}
.reply textarea,.form textarea,.form select{width:100%;background:var(--card);color:var(--text);border:1px solid var(--edge);border-radius:8px;padding:8px;font:inherit}
.reply textarea{height:64px;resize:none}.send{background:var(--run);color:#fff;border-radius:8px;padding:8px 18px;margin-left:auto}
.note{font-size:12px;color:var(--faint);flex:1}.status{font-size:13px;margin-top:6px}.ok{color:var(--done)}.bad{color:var(--err)}
.form{display:flex;flex-direction:column;gap:10px;margin-top:10px}.form textarea{height:140px}
.quota{color:var(--dim);font-size:12px;margin-top:2px}.quota:empty{display:none}
.ask{background:var(--card);border:1px solid color-mix(in srgb,var(--ask) 40%,transparent);border-radius:10px;padding:10px 12px;margin:12px 0 24px}
.q{margin:10px 0}.qh{display:flex;gap:6px;align-items:center;flex-wrap:wrap;margin-bottom:6px}
.opt{display:block;width:100%;text-align:left;border:1px solid var(--edge);border-radius:8px;padding:9px 10px;margin:6px 0;color:var(--text);font:inherit}
.opt b{font-weight:600}.opt span{display:block;color:var(--dim);font-size:12px;margin-top:3px}
.opt.on{border-color:var(--ask);background:color-mix(in srgb,var(--ask) 16%,transparent)}
.other{width:100%;background:var(--bg);color:var(--text);border:1px solid var(--edge);border-radius:8px;padding:8px;font:inherit;margin-top:2px}
</style></head><body>
<header><div class="top"><span class="dot" id="hdot"></span><h1>Agents</h1><span class="sum" id="sum"></span><button class="plus" onclick="go('#/new')">＋</button></div>
<div class="quota" id="quota"></div>
<div class="tabs" id="tabs"><button data-t="recent">最近 24 小时</button><button data-t="projects">按项目</button></div></header>
<main id="main"><div class="empty">正在连接电脑…</div></main>
<script>
const C={run:'var(--run)',bg:'#8b7bff',ask:'var(--ask)',confirm:'var(--ask)',done:'var(--done)',error:'var(--err)',interrupted:'#7a8191',stale:'#7a8191'};
const MARK={bg:'◐',done:'✓',error:'✕',interrupted:'■',stale:'…'};
const LIVE=['run','bg','ask','confirm'];
let S=null,tab=localStorage.getItem('tab')||'recent',open=JSON.parse(localStorage.getItem('open')||'{}'),conv=null,job=null,draft='';
const $=s=>document.querySelector(s);
const esc=t=>String(t).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
function md(t){const parts=String(t).split('```');return parts.map((p,i)=>i%2?'<pre>'+esc(p.replace(/^\w*\n/,''))+'</pre>':
  esc(p).replace(/`([^`\n]+)`/g,'<code>$1</code>').replace(/\*\*([^*\n]+)\*\*/g,'<b>$1</b>').replace(/^#{1,6} (.*)$/gm,'<b>$1</b>').replace(/\n/g,'<br>')).join('')}
function dur(s){s=Math.max(0,s|0);return s>=3600?`${s/3600|0}:${String((s%3600)/60|0).padStart(2,'0')}:${String(s%60).padStart(2,'0')}`:`${s/60|0}:${String(s%60).padStart(2,'0')}`}
function ago(s){return s<60?'刚刚':s<3600?`${s/60|0} 分钟前`:s<86400?`${s/3600|0} 小时前`:`${s/86400|0} 天前`}
function chip(name,color){return `<span class="chip" style="color:${color};background:color-mix(in srgb,${color} 18%,transparent)">${esc(name)}</span>`}
function right(v){const now=S.now;if(LIVE.includes(v.state))return (v.marker?`第 ${v.marker[0]}/${v.marker[1]} 步 · `:v.state==='bg'?'等后台 · ':'')+dur(now-v.start);
  if(v.state==='done')return ago(now-v.end)+(v.end>v.start?' · 用时 '+dur(v.end-v.start):'');if(v.state==='stale')return '没收尾 · '+ago(now-v.when);
  return (v.state==='error'?'出错了':'被打断')+' · '+ago(now-v.end)}
function card(v){const m=v.marker,cells=m?Array.from({length:m[1]},(_,i)=>`<i class="${i<m[0]?'d':''}"></i>`).join(''):'';
  const wait=v.state==='ask'||v.state==='confirm';
  return `<div class="card" onclick="go('#/c/${v.id}')"><div class="row"><span class="dot" style="background:${C[v.state]}"></span>${chip(v.ws,v.color)}${v.owned?chip('后台','var(--dim)'):''}<span class="title">${esc(v.title)}</span><span class="faint">${right(v)}</span></div>
  ${m?`<div class="muted" style="color:${C[v.state]}">第 ${m[0]}/${m[1]} 步${m[2]?' · '+esc(m[2]):''}</div>`:''}${m?`<div class="bar">${cells}</div>`:''}
  <div class="line" style="${wait?'color:var(--ask)':''}">▸ ${esc(v.action)}</div>${bgs(v)}</div>`}
function bgs(v){const w=(v.bg||[]).filter(b=>!b.resident),r=(v.bg||[]).filter(b=>b.resident);   // 后台命令：在等的带输出的最后一行；常驻的合成一行
  return w.map(b=>`<div class="line" style="color:var(--text)"><span style="color:${C.bg}">◐</span> ${esc(b.desc)} <span class="faint">${dur(S.now-b.start)}</span>${b.tail?`<br><span class="faint">输出：${esc(b.tail)}</span>`:''}</div>`).join('')
    +(r.length?`<div class="line faint">后台开着：${esc(r.map(b=>b.desc).join('、'))}</div>`:'')}
function item(v,withWs){return `<div class="item" onclick="go('#/c/${v.id}')"><div class="row">${v.new?'<span class="new"></span>':''}<span style="color:${C[v.state]}">${MARK[v.state]||'●'}</span>
  ${withWs?chip(v.ws,v.color):''}${v.owned?chip('后台','var(--dim)'):''}${!LIVE.includes(v.state)&&(v.bg||[]).some(b=>b.resident)?chip('后台开着',C.bg):''}<span class="title" style="${v.new?'':'color:var(--dim)'}">${esc(v.title)}</span><span class="faint">${right(v)}</span></div></div>`}
function renderList(){const L=S.live,R=S.recent;let h='';
  if(tab==='recent'){h+=L.map(card).join('');if(R.length)h+=`<div class="sec">24 小时内结束 · ${R.length} 条</div><div class="card">`+R.map(v=>item(v,true)).join('')+'</div>';
    if(!L.length&&!R.length)h='<div class="empty">现在没有在跑的，24 小时内也没有结束的对话。</div>'}
  else{h=S.projects.map(p=>{const o=open[p.name];const n=[p.live?`<span style="color:var(--run)">${p.live} 在跑</span>`:'',p.new?`<span style="color:var(--done)">${p.new} 新结束</span>`:''].filter(Boolean).join(' · ')||`<span class="faint">${p.items.length} 条</span>`;
    return `<div class="card" style="padding:0 10px"><div class="proj" onclick="toggle('${esc(p.name)}')"><span class="muted">${o?'▾':'▸'}</span><span class="dot" style="background:${p.color}"></span><span style="color:${p.color}">${esc(p.name)}</span><span class="n">${n}</span></div>${o?p.items.map(v=>item(v,false)).join(''):''}</div>`}).join('')||'<div class="empty">最近几天没有对话。</div>'}
  $('#main').innerHTML=h}
let shownConv=null,picks={},note=['',''];
// 它在用选项问你（AskUserQuestion）：每个问题一组能点的选项，单选点一个、多选点几个，也能自己写；提交后电脑上撤下提问、把选择当成一句话发过去
function ask(v){const q=v.question;
  if(!q)return v.state==='ask'?'<div class="ask"><div class="muted" style="color:var(--ask)">它在等你批准计划（或者等你在电脑上处理别的事），这个在手机上做不了，回到电脑前处理。</div></div>':'';
  const P=picks[q.tool_id]||(picks[q.tool_id]=q.questions.map(()=>({labels:[],other:''})));
  return '<div class="ask"><div class="muted" style="color:var(--ask)">它在等你选：</div>'+q.questions.map((x,i)=>`<div class="q"><div class="qh">${x.header?chip(x.header,'var(--ask)'):''}<span>${esc(x.question||'')}</span>${x.multiSelect?'<span class="faint">可多选</span>':''}</div>`+
    (x.options||[]).map((o,j)=>`<button class="opt${P[i].labels.includes(o.label)?' on':''}" onclick="pick(${i},${j})"><b>${esc(o.label)}</b>${o.description?'<span>'+esc(o.description)+'</span>':''}</button>`).join('')+
    `<input class="other" placeholder="都不是？自己写…" value="${esc(P[i].other)}" oninput="picks[conv.question.tool_id][${i}].other=this.value"></div>`).join('')+
    `<button class="send" style="margin:8px 0 0" onclick="answer()">把选择发过去</button><div class="note" style="margin-top:6px">会在电脑上撤下这个提问，再把你的选择当成一句话发给它。电脑要开着、没锁屏。</div><div class="${note[0]}" id="as">${esc(note[1])}</div></div>`}
function pick(i,j){const q=conv.question,P=picks[q.tool_id],o=q.questions[i].options[j].label,L=P[i].labels;
  if(q.questions[i].multiSelect){const k=L.indexOf(o);if(k<0)L.push(o);else L.splice(k,1)}else P[i].labels=L[0]===o?[]:[o];renderConv()}
async function answer(){const q=conv&&conv.question;if(!q)return;say('','正在发送…');
  try{const j=await api('/api/answer',{id:conv.id,tool_id:q.tool_id,picks:picks[q.tool_id]});job=j.job;poll()}catch(e){say('bad','没发出去：'+e.message)}}
// 发送结果写在哪：回答选择题时写在选项下面，回复时写在回复框下面；两边都记着，免得页面刷新后（问题答完、换成回复框）看不到
function say(c,t){note=['status '+c,t];for(const s of [$('#as'),$('#rs')])if(s){s.className='status '+c;s.textContent=t}}
function renderConv(){const v=conv;if(!v){$('#main').innerHTML='<div class="empty">正在读这个对话…</div>';return}
  if(shownConv===v.id&&document.activeElement&&document.activeElement.classList.contains('other'))return;   // 正在「自己写」：先别刷新，免得打字被打断
  const atBottom=innerHeight+scrollY>=document.body.scrollHeight-200,first=shownConv!==v.id;shownConv=v.id;
  if(first)note=['',''];
  const msgs=v.messages.map(m=>`<div class="msg ${m.role}">${md(m.text)}${m.steps&&m.steps.length?`<details class="steps"><summary>这一轮中间说的 ${m.steps.length} 段</summary>${m.steps.map(s=>'<div style="margin-top:6px;color:var(--dim)">'+md(s)+'</div>').join('')}</details>`:''}</div>`).join('');
  const live=LIVE.includes(v.state);
  $('#main').innerHTML=`<button class="back" onclick="go('#/')">‹ 返回</button><div class="row" style="margin:6px 0">${chip(v.ws,v.color)}<span class="title">${esc(v.title)}</span></div>
  <div class="muted">${live?(MARK[v.state]||'●')+' '+esc(v.action||'在跑')+' · '+dur(S.now-v.start):right(v)}</div>${live?bgs(v):''}${msgs||'<div class="empty">还没有对话内容。</div>'}${ask(v)}`;
  if(v.state==='ask'){$('.reply')&&$('.reply').remove()}   // 在等你选 / 批准：不给普通的回复框
  else{if(!$('.reply')){document.body.insertAdjacentHTML('beforeend',`<div class="reply"><textarea id="rt" placeholder="回复它…（可以按输入法上的麦克风直接说）"></textarea><div class="row" style="margin-top:6px"><span class="note" id="rn"></span><button class="send" onclick="send()">发送</button></div><div class="${note[0]||'status'}" id="rs">${esc(note[1])}</div></div>`)}
    $('#rn').textContent=v.background?'这是后台任务，回复后它直接接着跑。':'会在电脑上的编辑器（Cursor / VS Code）里替你打进这个对话，电脑要开着、没锁屏。';
    const t=$('#rt');if(t.value!==draft&&document.activeElement!==t)t.value=draft}
  // 一打开就停在最后那条回复的开头（最终输出）；之后有新内容时，本来就在底部的话跟着滚下去
  const ms=document.querySelectorAll('.msg'),card=$('.ask'),head=$('header').offsetHeight+10;
  if(first&&card)scrollTo(0,card.getBoundingClientRect().top+scrollY-head);   // 在等你选：直接停在选项卡片开头
  else if(first&&ms.length)scrollTo(0,ms[ms.length-1].getBoundingClientRect().top+scrollY-130);
  else if(atBottom&&!first&&!card)scrollTo(0,document.body.scrollHeight)}   // 在选的时候别自己跳
function renderNew(){$('.reply')&&$('.reply').remove();$('#main').innerHTML=`<button class="back" onclick="go('#/')">‹ 返回</button><div class="form"><div class="muted">在电脑上发起一个后台任务</div>
  <select id="nw">${S.workspaces.map(w=>`<option>${esc(w)}</option>`).join('')}</select><textarea id="nt" placeholder="要做什么（可以按输入法上的麦克风直接说）"></textarea>
  <button class="send" style="margin:0" onclick="create()">发起</button><div class="status" id="ns"></div></div>`;const last=localStorage.getItem('ws');if(last)$('#nw').value=last}
function toggle(n){open[n]=!open[n];localStorage.setItem('open',JSON.stringify(open));renderList()}
function go(h){location.hash=h}
async function api(p,b){const r=await fetch(p,b?{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b)}:{});const j=await r.json();if(!r.ok)throw new Error(j.error||r.status);return j}
async function send(){const t=$('#rt').value.trim();if(!t||!conv)return;say('','正在发送…');
  try{const j=await api('/api/reply',{id:conv.id,text:t});job=j.job;draft='';$('#rt').value='';poll()}catch(e){say('bad','没发出去：'+e.message)}}
async function poll(){if(!job)return;try{const j=await api('/api/job?id='+job);if(j.state==='sending'){setTimeout(poll,1000);return}
  say(j.state==='sent'?'ok':'bad',j.message);job=null;refresh()}catch(e){setTimeout(poll,2000)}}
async function create(){const t=$('#nt').value.trim(),w=$('#nw').value;if(!t)return;localStorage.setItem('ws',w);$('#ns').textContent='正在发起…';
  try{const j=await api('/api/new',{ws:w,text:t});go('#/c/'+j.id)}catch(e){$('#ns').className='status bad';$('#ns').textContent='没发起：'+e.message}}
async function refresh(){try{S=await api('/api/state');const need=S.live.filter(v=>v.state==='ask'||v.state==='confirm').length,run=S.live.length-need,nw=S.recent.filter(v=>v.new).length;
  $('#sum').textContent=[run?run+' 个在干活':'',need?need+' 个等你':'',nw?nw+' 个新结束':''].filter(Boolean).join(' · ')||'空闲';
  $('#hdot').style.background=need?'var(--ask)':run?'var(--run)':'var(--faint)';
  $('#quota').textContent=S.usage?'Claude 用量：'+S.usage:'';
  const h=location.hash;if(h.startsWith('#/c/')){try{conv=await api('/api/conv?id='+h.slice(4))}catch(e){conv={id:h.slice(4),title:'找不到这个对话',ws:'',color:'var(--faint)',state:'stale',messages:[],start:S.now,end:S.now,when:S.now}}renderConv()}
  else if(h==='#/new'){if(!$('#nw'))renderNew()}else renderList()}catch(e){$('#sum').textContent='连不上电脑（电脑要开着、悬浮窗要开着，并且在同一个局域网）'}}
document.querySelectorAll('#tabs button').forEach(b=>{b.onclick=()=>{tab=b.dataset.t;localStorage.setItem('tab',tab);if(location.hash!=='#/'&&location.hash!=='')go('#/');syncTabs();S&&renderList()}});
function syncTabs(){document.querySelectorAll('#tabs button').forEach(b=>b.classList.toggle('on',b.dataset.t===tab))}
document.addEventListener('input',e=>{if(e.target.id==='rt')draft=e.target.value});
window.onhashchange=()=>{conv=null;shownConv=null;if(!location.hash.startsWith('#/c/')&&$('.reply'))$('.reply').remove();if(location.hash==='#/new'&&S)renderNew();else refresh();window.scrollTo(0,0)};
syncTabs();refresh();setInterval(()=>{if(location.hash!=='#/new')refresh()},3000);
</script></body></html>"""
