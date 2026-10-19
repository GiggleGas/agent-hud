"""用 Edge 模拟手机（390×844）截手机网页的列表页和对话页，检查排版。要用 dev/.venv-build 里的 Python 跑（里面有 websockets）。
用法：shot_phone.py <网页地址，带钥匙> [对话 id]。截图存在系统临时目录。用一个临时的浏览器配置，不碰你自己的 Edge。"""
import base64, json, os, shutil, subprocess, sys, tempfile, time, urllib.request
from websockets.sync.client import connect

sys.stdout.reconfigure(encoding="utf-8")
url, sid = sys.argv[1], (sys.argv[2] if len(sys.argv) > 2 else "")
edge = next(p for p in (r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
                        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe") if os.path.exists(p))
profile = tempfile.mkdtemp(prefix="hud-edge-")
proc = subprocess.Popen([edge, "--headless=new", "--disable-gpu", "--no-first-run", "--remote-debugging-port=9333",
                         "--user-data-dir=" + profile, "about:blank"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
try:
    for _ in range(50):
        try:
            pages = json.load(urllib.request.urlopen("http://127.0.0.1:9333/json", timeout=2))
            ws = next(p["webSocketDebuggerUrl"] for p in pages if p.get("type") == "page")
            break
        except Exception:
            time.sleep(0.2)
    n = [0]
    with connect(ws, max_size=None) as c:
        def call(method, **params):
            n[0] += 1
            c.send(json.dumps({"id": n[0], "method": method, "params": params}))
            while True:
                msg = json.loads(c.recv())
                if msg.get("id") == n[0]:
                    return msg.get("result", {})

        call("Emulation.setDeviceMetricsOverride", width=390, height=844, deviceScaleFactor=2, mobile=True)
        for name, target in (("list", url), ("conv", url + ("#/c/" + sid if sid else ""))):
            if name == "conv" and not sid:
                break
            call("Page.navigate", url=target)
            time.sleep(3.5)
            info = call("Runtime.evaluate", expression="JSON.stringify({w: innerWidth, scroll: document.documentElement.scrollWidth})",
                        returnByValue=True)["result"]["value"]
            out = os.path.join(tempfile.gettempdir(), "hud-phone-%s.png" % name)
            with open(out, "wb") as f:
                f.write(base64.b64decode(call("Page.captureScreenshot", format="png")["data"]))
            print(name, info, out)
finally:
    proc.kill()
    time.sleep(0.5)
    shutil.rmtree(profile, ignore_errors=True)
