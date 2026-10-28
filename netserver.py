# -*- coding: utf-8 -*-
"""
netserver.py —— 联机版服务端（房主运行）

用法：
  python netserver.py
      房主控制台模式：在浏览器里配好 人数 / 电脑数 / 开局种子 / 游戏端口 再点「开局」，
      整局都能在控制台看战报和场面（房主只观战、不下场），打完还能直接再开一局。
  python netserver.py [人数] [电脑数] [端口] [开局种子]
      立刻按这些参数开局，打完一局就退出（脚本 / 测试用的老用法，行为不变）。
      例：python netserver.py 3 1 8080 5   → 3 人局含 1 电脑，2 人连接，开局每人 5 颗种子
  python netserver.py --console-port 9000   控制台端口（默认 8090，被占自动往后找）
  python netserver.py --no-console          不起控制台（纯命令行）
  python netserver.py --rule mult_cap=0     房间规则（可重复）—— 这里 = 倍耗不封顶

其他人运行 netclient.py（控制台版）/ webui.py（网页版）连到这台机器的 IP。

游戏逻辑完全复用 engine.py：每个网络客户端被包装成一个 RemoteController。
★ 房主自己不开客户端 —— 他是【旁观者】（engine.Observer），只收播报不下场，
  所以控制台永远不出现在决策名单里（理由见 engine.py 里 Observer 的注释）。

v0.5 ：决策采集从"逐个轮询"改成"同时下发、收满为止"。
v0.22：房主控制台（自带一个 HTTP 服务，默认 8090）+ 开局种子数（全场统一发牌）。
"""

import json
import os
import queue
import socket
import sys
import threading
import time
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import netproto
from bots import BotController
from engine import Controller, Game, Observer, PENDING
from plants_data import (DEFAULT_RULES, normalize_rules, describe_rules,
                         rule_options)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(BASE_DIR, "web")   # 控制台页面和后端同仓库，直接复用 web/

# Windows 控制台保险（v0.43 更正）—— ★ 只挂 errors="replace"，【绝不动 encoding】。
#
#   ★★ 为什么绝不能改 encoding（这是 v0.42 亲手制造的 bug）：
#      v0.42 这里跟 webui.py 学了"按 GetConsoleOutputCP() 把编码改成控制台代码页
#      （cmd 下 = cp936）"，以为能治乱码。真机实测恰恰相反 ——
#        · Python 3.6+ 在【真控制台】上，sys.stdout 走的是 UTF-8 + WriteConsoleW
#          （内部自动转 UTF-16 交给控制台），输出本来就是对的，跟控制台代码页无关；
#        · 强行 reconfigure(encoding="cp936") 后，底层仍按 UTF-8 去解释这些 cp936
#          字节 → 中文全变成乱码。
#      实测（真控制台，chcp 936）：改编码前屏幕显示正确，改完全乱。
#      ★ 结论：控制台流别碰 encoding，默认就对。
#
#   保留 errors="replace" 只为兜住一种情况：输出被重定向到文件/管道、而流编码
#   （没设 PYTHONUTF8 时 = 本地 ANSI 代码页）盖不住某个生僻字符时，print 会抛
#   UnicodeEncodeError 把常驻服务直接搞崩 —— 换成 "?" 代替就没事了。
if sys.platform == "win32":
    for _stream in (sys.stdout, sys.stderr):
        try:
            if _stream is not None:
                _stream.reconfigure(errors="replace")
        except Exception:
            pass


class RemoteController(Controller):
    """把一个网络客户端包装成引擎的决策接口（服务端视角）。

    后台读线程持续把客户端消息塞进 _inbox，dispatch / poll 因此都是非阻塞的，
    这是"同时问、收满为止"能成立的前提。

    ★【v0.15】一条硬边界：这里的 _dead 指的是【socket 掉了】，不是【玩家出局了】。
      玩家出局（hp<=0）时引擎只是不再问他，连接照旧开着、场面照旧推 ——
      出局的人留在牌桌上观战，将来加复活机制时可以直接回到 alive。
      所以：除了"连接真的断了 / 对局已经结束"，任何地方都不许因为 alive=False
      去关连接或停止推送。这两件事必须始终分开。
    """

    def __init__(self, sock, idx):
        self.sock = sock
        self.idx = idx
        self.name = "player%d" % idx
        # 【v0.43】客户端来源地址（"IP:端口"），只用于控制台的"已连接"提示，
        #   帮房主定位"是不是连错机器 / 连错端口"。连上后由 Room._run 填。
        self.addr = None
        # 【v0.35】玩家自己起的名字（没起就是 None）。
        #   ★ 与 self.name 分开存：name 始终是"当前该显示的名字"（默认值兜底），
        #     custom 用来告诉引擎"这是自定义名 → tag 要补编号"。
        self.custom = None
        self._reader = netproto.Reader(sock)
        self._inbox = queue.Queue()
        self._q = None       # 当前待答的问题
        self._dead = False
        self._err = None
        threading.Thread(target=self._read_loop, daemon=True).start()

    # ---------- 后台读线程 ----------
    def _read_loop(self):
        while True:
            try:
                m = self._reader.recv()
            except (OSError, ValueError):
                m = None
            if m is None:  # 连接关闭 / 收到非法 JSON → 标记为掉线
                self._inbox.put({"type": "conn_lost"})
                return
            # 【v0.35】"起名"是【报到消息】，不是一次决策 —— 就地收下，
            #   别塞进 _inbox 让 poll 去判（poll 只认 answer，会把它当无关消息丢掉）。
            if isinstance(m, dict) and m.get("type") == "name":
                self._set_custom_name(m.get("value"))
                continue
            self._inbox.put(m)

    def _set_custom_name(self, value):
        """收下自定义名。★ 只做"够不够干净"的基本处理，不做合法性拒绝 ——
        名字是显示名，叫什么都不影响判定（引擎认人用 idx）。
        空 / 非字符串 / 太长 → 当作没起名（退回默认）。"""
        if not isinstance(value, str):
            return
        v = value.strip()
        if not v or len(v) > 16:      # 太长会把战报挤爆；16 字够用了
            return
        self.custom = v
        self.name = v

    # ---------- 发送 ----------
    def _mark_dead(self, reason):
        self._dead = True
        if self._err is None:
            self._err = reason

    def _send(self, obj):
        if self._dead:
            raise ConnectionError(self._err or ("player%d 已掉线" % self.idx))
        try:
            netproto.send_msg(self.sock, obj)
        except OSError:
            self._mark_dead("player%d 掉线" % self.idx)
            raise ConnectionError(self._err)

    def notify(self, text):
        self._send({"type": "msg", "text": text})

    def notify_state(self, text, state):
        """【v0.13】场面消息多带一个结构化 state。

        老客户端（netclient）只读 text，多出来的字段会被忽略，
        所以这是向后兼容的；网页客户端则直接拿 state 渲染卡片，不用解析中文。
        """
        self._send({"type": "msg", "text": text, "state": state})

    def wait_hint(self, text):
        """本轮没他的决策要做 → 让客户端把界面切成"等待他人"，别干瞪着旧面板。"""
        self._send({"type": "wait", "text": text})

    # ---------- 批量决策协议 ----------
    def dispatch(self, q):
        self._drop_stale_answers()   # 丢掉上一轮残留的重复提交（防连点）
        self._q = q
        self._send(q.msg())          # 发出去就返回，不等答案

    def poll(self):
        if self._dead:
            raise ConnectionError(self._err)
        try:
            m = self._inbox.get_nowait()
        except queue.Empty:
            return PENDING
        if m.get("type") == "conn_lost":
            self._mark_dead("player%d 掉线" % self.idx)
            raise ConnectionError(self._err)
        if m.get("type") != "answer" or self._q is None:
            return PENDING           # 无关消息 → 忽略，继续等
        value = m.get("value")
        if not self._q.validate(value):
            # 答案不合法：原问题重发（图形客户端只会发合法值，此处兜底）
            self._send({"type": "msg", "text": "输入无效，请重新选择"})
            self._send(self._q.msg())
            return PENDING
        self._q = None
        return value

    def _drop_stale_answers(self):
        """只丢 answer，其它消息（比如掉线标记）塞回队列，避免误吞。"""
        kept = []
        while True:
            try:
                m = self._inbox.get_nowait()
            except queue.Empty:
                break
            if m.get("type") != "answer":
                kept.append(m)
        for m in kept:
            self._inbox.put(m)


class ConsoleObserver(Observer):
    """把引擎的播报转给房主控制台（浏览器）。

    【v0.22】它只实现 Observer 的两个方法 —— 引擎永远不会问它要决策，
    这正是 Observer 必须和 Controller 分开的理由（见 engine.Observer）。
    """

    def __init__(self, push):
        self._push = push

    def notify(self, text):
        self._push({"type": "msg", "text": text})

    def notify_state(self, text, state):
        self._push({"type": "msg", "text": text, "state": state})


# ======================================================================
#  控制台的事件总线：SSE 广播 + 回放
# ======================================================================
class ConsoleBus:
    """房主控制台的事件总线（和 webui.Bus 是同一个套路）。

    ★ 为什么必须回放：房主很可能在「开局」之后才打开（或刷新）控制台页面，
      那时前面几十条战报早就推过去了 —— 不回放的话页面一片空白，
      看着像什么都没发生，而服务端其实跑得好好的。这种静默的"看着没反应"
      是最难查的一类问题，所以宁可多存一份历史。

    事件类型：
      status  —— 房间状态（覆盖式，只留最新一条；新连接先收到它）
      msg     —— 战报一行（进历史，全量回放，有上限）
      clear   —— 新的一局：清空历史（自己也广播出去，让开着的页面跟着清屏）
    """

    MAX_HISTORY = 600

    def __init__(self):
        self._lock = threading.Lock()
        self._listeners = []
        self._history = []
        self._status = {"type": "status", "state": "idle"}

    def subscribe(self):
        q = queue.Queue()
        with self._lock:
            q.put(dict(self._status))
            for ev in self._history:
                q.put(ev)
            self._listeners.append(q)
        return q

    def unsubscribe(self, q):
        with self._lock:
            if q in self._listeners:
                self._listeners.remove(q)

    def push(self, obj):
        """所有事件的唯一入口 —— 先按类型归档，再广播。"""
        t = obj.get("type")
        with self._lock:
            if t == "status":
                self._status = dict(obj)
            elif t == "clear":
                self._history = []          # 新的一局 → 战报从头来
            else:                            # msg：进历史
                self._history.append(obj)
                del self._history[:-self.MAX_HISTORY]
            listeners = list(self._listeners)
        for q in listeners:
            q.put(obj)

    def emit(self, text):
        """推一行战报（ConsoleBus 自己产生的那些，比如"谁连上了"）。"""
        self.push({"type": "msg", "text": text})


# ======================================================================
#  房间：一局对局的状态机
# ======================================================================
class Room:
    """一局对局的房间 —— 控制台的一切都落在它身上。

    状态机：
      idle    还没开局，等房主在控制台点「开局」
      waiting 已开局，正在等客户端连进来（人数没满）
      playing 人齐了，引擎正在跑
      ended   结束（分出胜负 / 有人掉线 / 房主停掉），可以再开一局

    ★ 为什么"开局"要另起一个线程：控制台的 HTTP 服务必须一直活着
      （房主要用浏览器看完这一整局，而且打完还要能再开一局），
      所以游戏只能在后台线程里跑；主线程留给控制台。

    ★ 端口是【同步 bind】的：start() 里就把游戏端口绑好，
      绑不上立刻返回错误。要是丢给后台线程去绑，房主点「开局」只会看到
      "已开局"，然后永远等不到人 —— 错误藏在后台线程里，最难查。
    """

    def __init__(self, bus, host_ip="127.0.0.1"):
        self.bus = bus
        self.host_ip = host_ip
        self._lock = threading.Lock()
        self.state = "idle"
        self.total = 2
        self.bots = 0
        self.start_seeds = 0
        # 【v0.43】游戏端口的默认值 = 控制台端口 + 1（main() 起了控制台后回填）。
        #   旧默认 8080 与控制台的 8090 并列摆着，房主一眼看着"不一致"；
        #   改成"紧挨着控制台"就没这个困惑了 —— 反正房主也能自己改。
        self.game_port = 8080
        # 【v0.43】控制台自己的端口（http://IP:console_port/）。只用于在页面上
        #   把"控制台端口 / 游戏端口"两个数分开讲清楚。
        self.console_port = None
        # 【v0.25】房间规则集（"每套房间一套规则"）。与 人数/种子 一样，
        #   是【房主的设置】——开局时定下，整局不变。默认值见 plants_data.DEFAULT_RULES。
        self.rules = dict(DEFAULT_RULES)
        self.joined = 0
        self.winner = None
        # 【v0.45】获胜者名单（"同一连通分图 → 全体获胜"）：单人时 = [那一个]，
        #   多人共同获胜时长度 >1；平局 / 中止 = 空表。控制台按它写结局文案。
        self.winners = []
        self.error = ""
        self._srv = None
        self._thread = None
        self._stop = False
        self._controllers = []
        # ================= 【v0.63】重赛（服务端不退出）=================
        # 用户 2026-10-23："这个后面的是一个需求……'再来一局'时，需要重启服务器，
        #   这其实是很麻烦的，再来一局干脆就不要重启服务器，保留名字，座位等等
        #   一系列信息。甚至可以加个统计胜率和胜局的功能（同一次不重启服务器时）"。
        # 实现：跑完一局不关监听口，回到"等人"状态；客户端点「再来一局」重连
        #   同一个端口就是下一局。座位名、战绩都留在这个 Room 对象里。
        self.once = False            # True = 打完一局就退出（自动化测试用 --once）
        self.seat_names = []         # 上一局的座位名（按 idx-1 存），新一局没报到就沿用
        self.wins = {}               # 玩家名 -> 胜场（同一个进程内累计，不落盘）
        self.games = 0               # 这个房间一共打了多少局
        self.last_summary = ""       # 上一局的结局（一行字，给控制台和结束面板看）
        self.history = []            # 最近几局的结局（控制台展示用，最多留 10 条）

    # ---------- 控制台端口回填 ----------
    def set_console_port(self, port):
        """【v0.43】控制台起好后回填它自己的端口，并顺手把"从没动过"的默认
        游戏端口调成 控制台端口 + 1。

        ★ 为什么：房主在 cmd 里看到"控制台默认 8090"，进了页面又看到"游戏端口
          8080"，两个数并列摆着像对不上。改成紧挨着控制台端口（8090 → 8091）就
          顺眼多了；而且房主随时能自己改，这里只在"还没被改过"时兜个默认。"""
        with self._lock:
            self.console_port = port
            if self.game_port == 8080 and self.state == "idle":
                self.game_port = min(65535, (port or 8080) + 1)
        self._emit()

    # ---------- 给控制台看的状态 ----------
    def status(self):
        with self._lock:
            # 【v0.43】已连接的真人客户端（编号 + 名称 + 来源 IP）。
            #   房主"连了好几个却没见能开局"时，靠这一行就能看出到底收没收到人、
            #   收的是谁、从哪台机器连的（连错端口/连错机器的情形一眼就露馅）。
            joined_names = []
            for c in self._controllers:
                if isinstance(c, RemoteController):
                    joined_names.append({
                        "idx": c.idx,
                        "name": getattr(c, "custom", None) or ("player%d" % c.idx),
                        "addr": getattr(c, "addr", None)})
            return {"state": self.state,
                    "total": self.total, "bots": self.bots,
                    "humans": max(0, self.total - self.bots),
                    "joined": self.joined,
                    "joined_names": joined_names,
                    "start_seeds": self.start_seeds,
                    "rules": dict(self.rules),   # 【v0.25】房间规则（控制台回显用）
                    # 【v0.49】可选规则集的元数据 —— 控制台照它【动态生成】勾选框，
                    #   以后加一条规则只改 plants_data.RULE_OPTIONS，界面不用动。
                    "rule_options": rule_options(),
                    # 默认规则值：前端靠它判断"服务端这份是不是全默认"——
                    #   全默认就不该拿它覆盖浏览器里缓存的上次勾选。
                    "rules_default": dict(DEFAULT_RULES),
                    "game_port": self.game_port,
                    "console_port": self.console_port,   # 【v0.43】控制台自己的端口
                    "host_ip": self.host_ip,
                    "winner": self.winner, "error": self.error,
                    "winners": list(self.winners),   # 【v0.45】多人共同获胜靠它
                    # 控制台按钮靠它决定「开局」可不可点
                    "can_start": self.state in ("idle", "ended"),
                    "busy": self.state in ("waiting", "playing"),
                    # 【v0.63】重赛相关的三样：战绩、上一局结局、座位名。
                    #   ★ 服务端不退出 ⇒ 这些在"同一个房间"里一直累计，
                    #     控制台和结束面板都靠它们显示"本服战绩"。
                    "games": self.games,
                    "wins": dict(self.wins),
                    "last_summary": self.last_summary,
                    "history": list(self.history[-10:]),
                    "seat_names": list(self.seat_names),
                    # 打完一局后服务端仍然开着 ⇒ 这一局是"等下一批人"，语气不一样
                    "replay_ready": bool(self.games and self.state == "waiting")}

    def _emit(self):
        """推一份房间状态。★ 必须在【不持锁】时调用（status() 自己要拿锁）。"""
        self.bus.push({"type": "status", **self.status()})

    # ---------- 保存设置（只改"下次开局"用的参数，不开局）----------
    def set_config(self, total=None, bots=None, start_seeds=None,
                   game_port=None, rules=None):
        """【v0.43】房主点「确定（保存设置）」时调用 —— 把参数与规则落到房间上，
        但【不开局】。

        ★ 为什么单独有这么个动作：旧版参数只在「开局」那一刻才被读走，房主在
          界面上勾了规则 / 改了端口，界面没有任何回执，"到底存下没有"只能靠猜。
          现在「确定」= 立刻存进房间，服务端再把折好的结果原样回显（status），
          房主一眼就能看到"生效的到底是哪几条规则"。

        ★ 校验口径与 start() 完全一致（同一套 _as_int + normalize_rules），
          保证"存下去的"和"开局时真正用的"是同一份东西。
        """
        with self._lock:
            if self.state in ("waiting", "playing"):
                return False, "对局进行中，先「停止」再改设置"
            cur_total, cur_bots = self.total, self.bots
            cur_seeds, cur_port = self.start_seeds, self.game_port
        total = cur_total if total is None else _as_int(total, cur_total)
        bots = cur_bots if bots is None else _as_int(bots, cur_bots)
        start_seeds = cur_seeds if start_seeds is None else _as_int(start_seeds, cur_seeds)
        game_port = cur_port if game_port is None else _as_int(game_port, cur_port)
        rules = normalize_rules(rules)
        if total < 2:
            return False, "人数至少 2 人"
        if bots < 0 or bots > total:
            return False, "电脑数要在 0 ~ 人数 之间"
        if not (1 <= game_port <= 65535):
            return False, "游戏端口要在 1 ~ 65535 之间"
        if start_seeds < 0:
            start_seeds = 0
        with self._lock:
            self.total, self.bots = total, bots
            self.start_seeds, self.game_port = start_seeds, game_port
            self.rules = rules
            self.error = ""
        self._emit()
        return True, None

    # ---------- 开局 ----------
    def start(self, total=2, bots=0, start_seeds=0, game_port=8080, rules=None):
        # 【v0.63】服务端打完一局【不退出】，此时 state 还是 waiting（replay_ready）——
        #   房主改完参数点「开局」时，必须先把那个"等下一局"的循环收掉：
        #   端口还被他占着，不先关就 bind 不上。
        with self._lock:
            replay = bool(self.games and self.state == "waiting")
        if replay:
            self.stop()
        with self._lock:
            if self.state in ("waiting", "playing"):
                return False, "已经有一局在跑了（%s）" % self.state
        # 控制台是给人手填的，脏数据必须在【这里】挡住，
        # 别让它一路漏到引擎里去 —— 那边只该看到合法的开局参数。
        total = _as_int(total, 2)
        bots = _as_int(bots, 0)
        start_seeds = _as_int(start_seeds, 0)
        game_port = _as_int(game_port, 8080)
        # 【v0.25】房间规则一样先折好再进引擎（脏数据退回默认，见 normalize_rules）。
        rules = normalize_rules(rules)
        if total < 2:
            return False, "人数至少 2 人"
        if bots < 0 or bots > total:
            return False, "电脑数要在 0 ~ 人数 之间"
        if not (1 <= game_port <= 65535):
            return False, "游戏端口要在 1 ~ 65535 之间"
        if start_seeds < 0:
            start_seeds = 0

        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            srv.bind(("", game_port))
            srv.listen(total)
        except OSError as e:
            try:
                srv.close()
            except OSError:
                pass
            return False, "游戏端口 %d 起不来：%s" % (game_port, e)
        srv.settimeout(0.5)      # accept 要有超时，stop() 才能打断它

        with self._lock:
            self._stop = False
            self._srv = srv
            self.total, self.bots = total, bots
            self.start_seeds, self.game_port = start_seeds, game_port
            self.rules = rules
            self.joined = 0
            self.winner, self.error = None, ""
            self.winners = []
            self.state = "waiting"
        self.bus.push({"type": "clear"})      # 新的一局 → 控制台战报清屏
        self._emit()
        self._thread = threading.Thread(target=self._run, args=(srv,), daemon=True)
        self._thread.start()
        return True, None

    # ---------- 停掉 ----------
    def stop(self):
        with self._lock:
            if self.state not in ("waiting", "playing"):
                return False, "现在没有在跑的对局"
            self._stop = True
            srv = self._srv
            ctrls = list(self._controllers)
        # 关掉监听口 + 所有客户端连接：引擎正卡在等答案，
        # 连接一断 poll() 就抛 ConnectionError，_run 会走中止收尾。
        if srv is not None:
            try:
                srv.close()
            except OSError:
                pass
        for c in ctrls:
            sock = getattr(c, "sock", None)
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
        # 【v0.43】等后台游戏线程把收尾跑完（它才会把 state 置成 ended）。
        #   不等的话，HTTP 回包里的 status 还是旧的 "waiting"，控制台页面会先显示
        #   "还在等人连接"、过一下才跳到"已结束" —— 房主会以为"停止没按上"。
        #   ★ 不持锁地 join：_finish() 自己要拿锁，持锁 join 会死锁。
        t = self._thread
        if t is not None and t is not threading.current_thread():
            t.join(timeout=2.0)
        return True, None

    def wait(self):
        t = self._thread
        if t is not None:
            t.join()

    # ---------- 游戏线程 ----------
    def _accept(self, srv):
        """等一个客户端。被 stop() 打断 / 监听口被关 → 返回 (None, None)。

        【v0.43】连 addr 一起返回 —— 控制台要把"谁从哪台机器连过来的"显示出来，
        帮房主判断是不是客户端连错了机器 / 连错了端口。"""
        while not self._stop:
            try:
                conn, addr = srv.accept()
            except socket.timeout:
                continue
            except OSError:
                return None, None
            return conn, addr
        return None, None

    def _run(self, srv):
        """【v0.63】房间生命周期：一局 → 回到"等人" → 下一局（服务端【不退出】）。

        ★ 为什么改成循环：用户 2026-10-23 的反馈是"再来一局要重启服务器，
          这其实是很麻烦的"。老版本打完一局就 `srv.close()` + 线程结束 ——
          客户端点「再来一局」只能连到一个已经没人听的空端口，
          座位名与战绩也跟着那个进程一起没了。
        ★ 现在：跑完一局只把房间状态置回 waiting、把这一局的控制器清掉，
          **监听口一直开着**。客户端重连同一个端口 = 下一局开始。
        ★ `self.once=True`（`--once`）= 打完一局就退出 —— 自动化测试要的语义。
        """
        try:
            while True:
                self._play_one(srv)
                if self._stop or self.once:
                    break
                with self._lock:
                    self.state = "waiting"
                    self.joined = 0
                    self._controllers = []
                    self.winner = None
                    self.winners = []
                    self.error = ""
                self.bus.emit("—— 本局结束。服务端【不关】：直接在客户端点"
                              "「再来一局」就是下一局（人数 / 规则 / 开局种子 / "
                              "座位名全部沿用）——")
                self._emit()
        finally:
            try:
                srv.close()
            except OSError:
                pass

    def _play_one(self, srv):
        """跑完【一局】：等人 → 开局 → 打完。结束时【不关】监听口（见 _run）。"""
        with self._lock:
            total, bots, seeds = self.total, self.bots, self.start_seeds
            rules = dict(self.rules)          # 【v0.25】房间规则随开局参数一起冻结
        humans = total - bots

        self.bus.emit("房间已开：%d 人局（%d 个真人 + %d 个电脑）" % (total, humans, bots))
        if seeds:
            self.bus.emit("开局种子：每人 %d 颗" % seeds)
        # 【v0.25】房间规则也播一条：不播的话，非默认规则只有房主自己知道。
        for line in describe_rules(rules):
            self.bus.emit("房间规则：%s" % line)
        self.bus.emit("请让玩家连接 %s:%d ……" % (self.host_ip, self.game_port))

        controllers = []
        taken = set()          # 已被占用的座位号（重赛时客户端会"认领"自己的旧座位）
        try:
            for i in range(humans):
                conn, addr = self._accept(srv)
                if conn is None:
                    return self._finish(error="房主停掉了对局")
                ctrl = RemoteController(conn, i + 1)
                ctrl.addr = ("%s:%d" % (addr[0], addr[1])) if addr else None
                # 【v0.63】★ 重赛时【按名字把座位复原】。
                #   用户要求"保留名字，座位等等一系列信息"。只按连接顺序分配的话，
                #   谁先点「再来一局」谁就变 1 号位 —— 名字会跟着换位，战报前后对不上。
                #   ★ 客户端重连后会立刻把名字再报一次（webui.rematch），
                #     这里给 0.12 秒让它落地（与 v0.59 的 0.15s 同一手法）。
                if self.seat_names:
                    time.sleep(0.12)
                    nm = getattr(ctrl, "custom", None)
                    if nm and nm in self.seat_names:
                        want = self.seat_names.index(nm) + 1
                        if 1 <= want <= humans and want not in taken:
                            ctrl.idx = want
                # 座位号冲突（重名 / 名字没报上来）→ 退到最小的空位
                if ctrl.idx in taken:
                    for n in range(1, humans + 1):
                        if n not in taken:
                            ctrl.idx = n
                            break
                taken.add(ctrl.idx)
                controllers.append(ctrl)
                netproto.send_msg(conn, {"type": "hello", "idx": ctrl.idx, "total": total})
                with self._lock:
                    self.joined = len(controllers)
                    self._controllers = list(controllers)
                # 【v0.43】把来源也播出来：房主"连了人却没见能开局"时，
                #   这行能直接看出收没收到、收到的是哪台机器。
                self.bus.emit("player%d 已连接（%d/%d）%s" % (
                    ctrl.idx, len(controllers), humans,
                    ("· 来自 " + ctrl.addr) if ctrl.addr else ""))
                self._emit()
        except OSError:
            return self._finish(error="监听被中断")

        # 【v0.63】重赛认领之后，controllers 可能不是按 idx 排的；引擎按
        #   "列表第 i 个 = i+1 号玩家"建人，所以这里必须按座位号排回来。
        controllers.sort(key=lambda c: c.idx)

        for _ in range(bots):
            controllers.append(BotController())
        with self._lock:
            self._controllers = list(controllers)

        for c in controllers:
            if isinstance(c, RemoteController):
                try:
                    c.notify("====== %d 人已到齐，游戏开始！ ======" % total)
                    c.notify("说明：出牌 / 释放 / 充能 / 选目标 都是全员的【同时】进行，"
                             "答完的人请等待其他人，收齐后才会进入下一步。")
                except (OSError, ConnectionError):
                    pass

        # 【v0.59】★ 人齐之后先给"报到名字"一个极短的落地窗口。
        #   name 是【连接后的第一条消息】，走的是 RemoteController 的读线程；
        #   而 _accept 最后一个客户端返回后，这里紧接着就要快照名字。
        #   同机多开（playtest.py 的 N 个座位连同一个 127.0.0.1）时，最后一个
        #   座位的 connect 与这次 accept 几乎同时，读线程往往还没来得及把 name
        #   读出来 —— 结果最后一个座位在战报里永远是 playerN（实测确实如此）。
        #   0.15 秒对一局的开场毫无影响，但足以让局域网/本机的报到消息落地。
        time.sleep(0.15)

        # 【v0.35】座位表：谁报过名就播一条，让大家知道"几号位是谁"。
        #   ★ 走【每个客户端的 notify】（不是 bus.emit）—— 座位表是给【玩家】看的，
        #     bus 那条只到房主控制台，玩家看不到。
        #   ★ 只播【有人起了名】的情况：全默认时一个字都不多（老战报不变）。
        named = [c for c in controllers
                 if isinstance(c, RemoteController) and c.custom]
        if named:
            seat = "座位表：%s" % "、".join(
                "%d号位 %s" % (c.idx, c.custom or ("player%d" % c.idx))
                for c in controllers
                if isinstance(c, RemoteController))
            self.bus.emit(seat)          # 房主控制台也看一份
            for c in controllers:
                if isinstance(c, RemoteController):
                    try:
                        c.notify(seat)
                    except (OSError, ConnectionError):
                        pass

        # 房主作为旁观者进局：只收播报，不进决策名单。
        # 【v0.35】玩家名随建房快照一次：谁报过名、谁没报，此刻定下来。
        #   ★ 为什么不"边打边认名字"：局开到一半名字突然变，战报前后对不上号，
        #     队友也会看懵。所以后来才到的 name 消息【本局不生效】（下一局再说）——
        #     而 connect 后紧接着就报名，正常根本不会晚到。
        # 【v0.63】★ 重赛时沿用上一次的座位名：客户端万一没及时报到（重连比
        #   首次连接更容易抢跑），也不能让战报里忽然全是 playerN。
        names = []
        for i, c in enumerate(controllers):
            nm = getattr(c, "custom", None)
            if not nm and i < len(self.seat_names) and self.seat_names[i]:
                nm = self.seat_names[i]
                c.custom = nm        # 回填：战报 / 座位表 / 战绩都用它
            names.append(nm)
        self.seat_names = names
        player_names = names
        game = Game(controllers, observers=[ConsoleObserver(self.bus.push)],
                    start_seeds=seeds, rules=rules, player_names=player_names)
        with self._lock:
            self.state = "playing"
        self._emit()

        aborted = False
        try:
            game.run()
        except (ConnectionError, OSError) as e:
            aborted = True
            self.bus.emit("—— 对局中止：%s ——" % e)
        except Exception as e:      # noqa: BLE001 —— 兜底见下面的说明
            # 【v0.46】★★ 引擎里任何非 IO 异常都【不许让游戏线程静默死掉】。
            #   以前这里只捕 ConnectionError/OSError，别的异常一路冒到线程顶，
            #   客户端从此收不到任何消息 → 界面上只剩"断联"（用户 2026-10-18
            #   报的正是这个观感，真凶其实是引擎里的 AttributeError）。
            #   现在：完整 traceback 打到控制台，并把"对局中止：<异常>"发给所有人。
            aborted = True
            traceback.print_exc()
            self.bus.emit("—— 对局中止：引擎异常 %s: %s ——" % (type(e).__name__, e))

        # 【v0.45】结局：winners 可能不止一个人（同一队友链共同获胜）。
        #   winner 保留为"恰好一人获胜时的那个 idx"（老客户端的读法不变）。
        winners = list(getattr(game, "winners", []) or [])
        if aborted:
            winner_idx, end_text = None, "对局中止"
        elif len(winners) == 1:
            winner_idx, end_text = winners[0].idx, "%s 获胜" % winners[0].tag
        elif winners:
            winner_idx = None
            end_text = "%s 共同获胜（同一队友链）" % "、".join(w.tag for w in winners)
        else:
            winner_idx, end_text = None, "平局"
        self.bus.emit("=== %s ===" % end_text)
        try:
            final_state = game.state_json()
        except Exception:
            final_state = None

        # 【v0.63】战绩累计 —— 键用【玩家名】（不带编号），这样重赛换座位也能对上。
        #   ★ 局数【所有打完的局都算】（平局也是打了一局），胜场只记有赢家的。
        #   ★ 中止（有人掉线 / 房主停掉）不算一局。
        if not aborted:
            self.games += 1
            for w in winners:
                nm = w.name or ("player%d" % w.idx)
                self.wins[nm] = self.wins.get(nm, 0) + 1
            self.last_summary = end_text
            self.history.append("%s（第 %d 局）" % (end_text, self.games))
        record = {"games": self.games, "wins": dict(self.wins),
                  "last": self.last_summary}

        # 【v0.15】结束消息带上"谁赢了"和最终场面：前端要拿它渲染结束面板，
        #   光有一段中文文本，界面只能反过来解析字符串。老客户端只读 text。
        for c in controllers:
            if isinstance(c, RemoteController):
                try:
                    c._send({"type": "end", "text": "游戏结束：" + end_text,
                             "winner": winner_idx,
                             # 【v0.45】获胜者名单（多人共同获胜时才有意义的多个）
                             "winners": [w.idx for w in winners],
                             # 【v0.63】本服战绩（服务端不重启就一直累计）
                             "record": record,
                             "state": final_state})
                except (OSError, ConnectionError):
                    pass
        # 先让缓冲区把这些消息真正推出去，再关 socket。
        # 直接 send 完就 close，如果对面刚好有没读走的数据，OS 可能发 RST，
        # 把刚写进去的"游戏结束"一起丢掉 —— 结果客户端只看到断线，看不到结局。
        time.sleep(0.25)
        for c in controllers:
            if isinstance(c, RemoteController):
                try:
                    c.sock.close()
                except OSError:
                    pass
        # ★【v0.63】这里【不再关 srv】—— 由 _run 的循环决定什么时候收摊
        #   （打一局就退：--once；否则回到"等人"，客户端重连即下一局）。

        self._finish(winner=winner_idx, error=("有人掉线，对局中止" if aborted else ""),
                     winners=[w.idx for w in winners])

    def _finish(self, winner=None, error="", winners=None):
        with self._lock:
            self.state = "ended"
            self._srv = None
            self.winner = winner
            self.winners = list(winners or [])   # 【v0.45】多人共同获胜的名单
            self.error = error
        self._emit()


def _as_int(v, default):
    """控制台递来的值可能是数字、字符串、空串、"abc" —— 一律折算成 int。

    脏数据退回 default，绝不抛异常：一个填错的输入框不该把整局开局搞崩。
    """
    if v is None or v == "":
        return default
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def lan_ip():
    """本机在局域网里的地址 —— 房主要念给朋友听的那个 IP。

    ★ 不真的发包：UDP "连"一个外部地址只是让内核做一次路由查询、
      挑出"出口网卡"，并没有任何数据真的发出去；然后 getsockname() 就知道
      该网卡的地址了。比遍历网卡接口可靠得多（多网卡/虚拟网卡时尤其明显）。
      没网就退回 127.0.0.1（同机测试照样能用）。
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


# ======================================================================
#  控制台的 HTTP 层
# ======================================================================
MIME = {".html": "text/html; charset=utf-8",
        ".css": "text/css; charset=utf-8",
        ".js": "application/javascript; charset=utf-8",
        ".svg": "image/svg+xml",
        ".png": "image/png"}


class ConsoleHandler(BaseHTTPRequestHandler):
    """房主控制台的接口。页面本身是 web/host.html（复用同一套 style.css）。"""

    bus = None
    room = None

    def log_message(self, fmt, *args):
        pass              # 默认日志太吵，关掉

    def handle_one_request(self):
        """浏览器读完响应就关连接，Windows 下这次读取会抛 ConnectionAbortedError
        （WinError 10053），socketserver 会当成"处理请求时出错"往控制台打一大段
        traceback —— 看着像崩了，其实什么事都没有。直接吞掉。"""
        try:
            BaseHTTPRequestHandler.handle_one_request(self)
        except OSError:
            self.close_connection = True

    # ---------- GET ----------
    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/index.html", "/host.html"):
            return self._file("host.html")
        if path == "/host.js":
            return self._file("host.js")
        if path == "/style.css":
            return self._file("style.css")
        if path == "/api/host/status":
            return self._json(self.room.status())
        if path == "/api/host/events":
            return self._sse()
        self.send_error(404)

    # ---------- POST ----------
    def do_POST(self):
        path = self.path.split("?")[0]
        body = self._read_json()

        if path == "/api/host/config":
            # 【v0.43】「确定（保存设置）」：只存参数与规则，不开局。
            ok, err = self.room.set_config(
                body.get("total"), body.get("bots"),
                body.get("start_seeds"), body.get("game_port"),
                body.get("rules"))
            return self._json({"ok": ok, "err": err, "status": self.room.status()})

        if path == "/api/host/start":
            ok, err = self.room.start(
                _as_int(body.get("total"), 2),
                _as_int(body.get("bots"), 0),
                _as_int(body.get("start_seeds"), 0),
                _as_int(body.get("game_port"), 8080),
                body.get("rules"))       # 【v0.25】房间规则（脏数据由 normalize_rules 折）
            return self._json({"ok": ok, "err": err, "status": self.room.status()})

        if path == "/api/host/stop":
            ok, err = self.room.stop()
            return self._json({"ok": ok, "err": err, "status": self.room.status()})

        self.send_error(404)

    # ---------- 工具 ----------
    def _read_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except ValueError:
            return {}

    def _json(self, obj):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _file(self, name):
        path = os.path.join(WEB_DIR, name)
        if not os.path.isfile(path):
            return self.send_error(404)
        with open(path, "rb") as f:
            data = f.read()
        ctype = MIME.get(os.path.splitext(name)[1], "application/octet-stream")
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        # 静态文件不许缓存：改了页面立刻能看到（同 webui.py 的理由）
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _sse(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        q = self.bus.subscribe()
        try:
            while True:
                try:
                    obj = q.get(timeout=15)
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")   # 心跳，防代理掐连接
                    self.wfile.flush()
                    continue
                data = json.dumps(obj, ensure_ascii=False)
                self.wfile.write(("data: %s\n\n" % data).encode("utf-8"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            self.bus.unsubscribe(q)


def start_console(preferred_port, bus, room, tries=12):
    """起控制台的 HTTP 服务（后台守护线程）。端口被占就顺着往后找。

    为什么绑 0.0.0.0 而不是 127.0.0.1：这是【服务端】的控制台，房主可能
    在另一台机器/手机上看着方便 —— 和游戏端口本身对局域网开放是一个道理。
    控制台不能做什么危险的事，只有「开局」和「停止」两个动作。
    """
    ConsoleHandler.bus = bus
    ConsoleHandler.room = room
    for p in range(preferred_port, preferred_port + tries):
        try:
            httpd = ThreadingHTTPServer(("", p), ConsoleHandler)
        except OSError:
            continue
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        return httpd, p
    return None, None


# ======================================================================
#  启动
def _rules_usage_lines():
    """【v0.52】--rule 的帮助文本按 `rule_options()` 动态生成。

    ★ 为什么改成动态：老写法在这里**手写**了 mult_cap / charge_seg_var 两行，
      v0.49 加"丝瓜万岁"、v0.51 加"生命之神"时都没跟上 —— 房主敲 `-h`
      看到的还是两年前的帮助，压根不知道后两个开关存在。
      （与 main.py 是同一个病：界面各写一份元数据 → 必漂移。）
    """
    out = ["房间规则（v0.25 起；每套房间可以有自己的规则集，没写的用默认值）："]
    for o in rule_options():
        dv = DEFAULT_RULES[o["key"]]
        out.append("  %s=N  「%s」%s" % (o["key"], o["name"], o["desc"]))
        out.append("      默认 %d（勾选框默认%s）；勾上 = %d，不勾 = %d"
                   % (dv, "勾着" if o["default_on"] else "不勾",
                      o["on"], o["off"]))
    out.append("★ 勾选框式的 on/off 由数据层说了算，命令行直接给数值即可。")
    return out


# ======================================================================
USAGE = """用法：
  python netserver.py                          房主控制台模式（浏览器里配参数开局）
  python netserver.py --open                   同上，并自动打开浏览器（双击 bat 走这条）
  python netserver.py [人数] [电脑数] [端口] [开局种子]
                                               立刻开局，打完就退出
  python netserver.py ... --keep               同上，但打完【不退出】：客户端重连
                                               同一端口就是下一局（座位名/战绩保留）
  python netserver.py --console-port 9000      控制台端口（默认 8090，被占自动往后找）
  python netserver.py --no-console             不起控制台
  python netserver.py --rule mult_cap=0        房间规则（可重复；这里 = 丝瓜王朝不封顶）

""" + "\n".join(_rules_usage_lines())

POS_NAMES = ["人数", "电脑数", "端口", "开局种子"]


def parse_args(argv):
    opt = {"total": None, "bots": 0, "port": 8080, "seeds": 0,
           "console": True, "console_port": 8090, "open": False, "rules": {},
           # 【v0.63】带位置参数时默认"打完一局就退出"（脚本/测试依赖这条）；
           #   --keep 改成"打完回到等人、客户点重连就下一局"。
           "keep": False}
    pos = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--no-console":
            opt["console"] = False
        elif a == "--rule" and i + 1 < len(argv):
            # 【v0.25】房间规则：--rule 键=值（可重复）。值一律当数字（本版只有
            #   数值型开关）；写坏的键/值不在这里报错 —— 交给 normalize_rules
            #   折成合法值（与 total/seeds 同一套"挡住脏数据"的思路）。
            i += 1
            raw = argv[i]
            if "=" not in raw:
                print("--rule 要写成 键=值 的形式，例如 --rule mult_cap=0")
                return None
            k, v = raw.split("=", 1)
            opt["rules"][k.strip()] = v.strip()
        elif a == "--keep":
            # 【v0.63】打完一局【不退出】：服务端回到"等人"，客户端重连同一个
            #   端口就是下一局（座位名 / 战绩留在进程里）。webui.py 的本机局走这条。
            opt["keep"] = True
        elif a == "--open":
            # 只给"双击 bat"那条路用：控制台端口被占时会往后挪，
            # 所以浏览器必须由【真正拿到端口的那个人】去开，不能写死在 bat 里。
            opt["open"] = True
        elif a == "--console-port" and i + 1 < len(argv):
            i += 1
            try:
                opt["console_port"] = int(argv[i])
            except ValueError:
                print("--console-port 后面要跟数字")
                return None
        elif a in ("-h", "--help"):
            print(USAGE)
            return None
        elif a.startswith("-"):
            print("不认识的参数：%s\n%s" % (a, USAGE))
            return None
        else:
            pos.append(a)
        i += 1

    if len(pos) > 4:
        print("位置参数最多 4 个（%s）\n%s" % ("、".join(POS_NAMES), USAGE))
        return None
    vals = []
    for name, raw in zip(POS_NAMES, pos):
        try:
            vals.append(int(raw))
        except ValueError:
            print("%s 必须是数字：%r" % (name, raw))
            return None
    if vals:
        opt["total"] = vals[0]
        opt["bots"] = vals[1] if len(vals) > 1 else 0
        opt["port"] = vals[2] if len(vals) > 2 else 8080
        opt["seeds"] = vals[3] if len(vals) > 3 else 0
        # 只校验最要命的那个：人数。电脑数的边界留给 Room.start 统一判。
        if opt["total"] < 2:
            print("参数不对：人数>=2，0<=电脑数<=人数")
            return None
    return opt


def main():
    opt = parse_args(sys.argv[1:])
    if opt is None:
        return 1

    bus = ConsoleBus()
    room = Room(bus, host_ip=lan_ip())

    console, console_port = None, None
    if opt["console"]:
        console, console_port = start_console(opt["console_port"], bus, room)
        if console is None:
            print("控制台端口从 %d 起连续 %d 个都被占用了 —— 用 --console-port 换一个。"
                  % (opt["console_port"], 12))
            if opt["total"] is None:
                return 1     # 控制台模式没控制台 = 没事可做
    if console_port:
        # 【v0.43】把控制台端口回填进房间：页面要显示它，默认游戏端口也要跟着它走。
        room.set_console_port(console_port)

    if console_port and opt["open"]:
        # 稍等再开：HTTP 服务还在另一个线程里起步，抢在前面开浏览器只会看到"拒绝连接"。
        threading.Timer(0.4, lambda: webbrowser.open(
            "http://127.0.0.1:%d/" % console_port)).start()

    # ---- 老用法：带参数 = 立刻开局，打完就退出（脚本 / 测试依赖这条）----
    if opt["total"] is not None:
        ok, err = room.start(opt["total"], opt["bots"], opt["seeds"], opt["port"],
                             opt["rules"])
        if not ok:
            print("[错误] %s" % err)
            return 1
        # 【v0.63】--keep = 打完一局不退出（回到等人，重连即下一局）
        room.once = not opt["keep"]
        print("服务端已启动：端口 %d，等待 %d 名玩家客户端连接（另有 %d 个电脑）……"
              % (opt["port"], opt["total"] - opt["bots"], opt["bots"]))
        if console_port:
            print("房主控制台（只观战）：http://%s:%d/" % (room.host_ip, console_port))
        if room.once:
            print("（打完一局就退出；想让玩家连打，加 --keep）")
        else:
            print("（打完一局【不退出】：客户端点「再来一局」重连本端口即可接着打）")
        room.wait()
        print("对局结束，服务端关闭。")
        if console is not None:
            console.shutdown()
        return 0

    # ---- 控制台模式：常驻，等房主在浏览器里点「开局」----
    if console is None:
        print("既没有开局参数、又没开控制台 —— 没事可做。\n%s" % USAGE)
        return 1
    print("《植物斗争》房主控制台已启动：")
    print("  本机打开   http://127.0.0.1:%d/" % console_port)
    print("  局域网     http://%s:%d/" % (room.host_ip, console_port))
    # 【v0.43】把"两个端口"讲清楚：控制台端口 = 你自己看战报的页面；
    #   游戏端口 = 玩家客户端连的那个（页面里能改，默认紧挨着控制台）。
    print("  · 这是【控制台端口 %d】—— 你自己看战报用的页面。" % console_port)
    print("  · 玩家连的是【游戏端口】（默认 %d，可在控制台页面里改）：" % room.game_port)
    print("      netclient.py / webui.py 连 %s:游戏端口。"
          % room.host_ip)
    print("  · 在控制台里配好 人数 / 电脑数 / 开局种子 / 游戏端口，点「确定」保存、再点「开局」。")
    print("  · Ctrl+C 退出。")
    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\n正在关闭……")
    finally:
        room.stop()
        if console is not None:
            try:
                console.shutdown()
            except Exception:
                pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
