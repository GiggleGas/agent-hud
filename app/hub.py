"""把采集到的状态变成事件：谁刚结束、谁在问问题等人回答。界面里的「已结束」列表从这里来。"""
import time

from collector import LIVE

ENDED = ("done", "error", "interrupted")
SETTLE = 1   # 结束状态稳住这么多秒才算数，防「刚结束马上又开工」。排队的消息、后台任务现在采集时就认得出来（还算在跑 / 等后台），
             # 这里只兜底认不出来的（比如别的对话发来的消息），所以不用等太久


class Hub:
    def __init__(self):
        self.state = {}      # 会话 id -> 上一次看到的状态
        self.pending = {}    # 会话 id -> 第一次看到它结束的时间
        self.first = True

    def update(self, views, now=None):
        """返回这一次新出的事件：[(种类, 那个会话的状态, 要不要对外通知)]，种类是 finished 或 asking。
        悬浮窗启动时就已经结束 / 已经在问的，也会报一次，但不对外通知，免得一启动就刷屏。"""
        now = now or time.time()
        events = []
        for v in views:
            sid, st, old = v["id"], v["state"], self.state.get(v["id"])
            if st in ENDED:
                if self.first:
                    events.append(("finished", v, False))
                elif old in LIVE or old is None or sid in self.pending:
                    if now - self.pending.setdefault(sid, now) >= SETTLE:
                        del self.pending[sid]
                        events.append(("finished", v, True))
            else:
                self.pending.pop(sid, None)
                if st == "ask" and old != "ask":
                    events.append(("asking", v, not self.first))
            self.state[sid] = st
        alive = {v["id"] for v in views}
        for sid in [s for s in self.state if s not in alive]:
            del self.state[sid]
            self.pending.pop(sid, None)
        self.first = False
        return events
