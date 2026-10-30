# -*- coding: utf-8 -*-
"""
playtest.py —— 本地试玩台：一个人开 N 个真人座位，一键打开 N 个网页

用途：
  有些机制【必须几个人同时在场】才看得出来（同时决策、勾取目标、队友链、
  反馈湮灭、存活环摘人……）。以前自测得手动跑一个 netserver + 好几个 webui，
  还得记住谁连的是哪个端口。这个脚本把那一串动作收成一个页面：
  配好人数 / 种子 / 规则，点一下，服务端和所有座位页一起就绪。

用法：
  python playtest.py                起试玩台并自动打开配置页（默认 8300）
  python playtest.py --port 8400    换配置页端口
  python playtest.py --no-browser   不自动开浏览器

点「一键开局」之后它做了什么：
  1. 在本进程里起一个【真的】netserver.Room（游戏端口自动挑，也可以自己指定）；
  2. 为每个真人座位起一个 webui.Handler 的派生子类 —— 每个座位有【自己的端口、
     自己的 Bus/Bridge】，也就是 N 个货真价实的网页客户端，和你手动跑 N 个
     webui.py 完全等价（协议、渲染、答题路径一模一样，不是模拟出来的）；
  3. 按座位顺序依次连上去、各自报上座位名（顺序 = 座位号，所以 1 号位就是名单
     里的第一个名字），然后打开 N 个浏览器标签；
  4. 战报直接显示在试玩台页面上（订阅的就是 netserver 的 ConsoleBus，
     和房主控制台同一份）。

  你在这 N 个标签之间来回切，每个标签就是一个独立的真人。谁在等答案、
  谁已经答完，各看各的页面（引擎本来就是"同时下发、收满为止"）。

★ 为什么不是"起 N 个 webui.py 子进程"：
  那样 N 个进程各持一份 SESSION / names.json，互相覆盖；而且试玩台看不到
  "哪个座位连上了、谁还没答"，停局还得挨个 terminate 掉。一个进程内 N 个
  Bridge 则状态统一、停局干净、进程只有一个。
★ 为什么不是"一个页面里切 N 个座位"：
  前端所有 fetch 都是绝对路径（/api/...），走路径前缀等于同时动四端
  （webui / app.js / 协议 / 测试）。而"每座位一个端口"本来就是 webui.py
  现成的形态 —— 零改动，风险最低。
★ 座位页里的「本机开一局」被后端挡掉了（Handler.no_local）：那一按会在
  试玩台进程里另起一个服务端，把其它座位晾在旧局里。
"""

import json
import os
import queue
import socket
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import netserver                      # ConsoleBus / Room：服务端那一半直接复用
import webui                          # Bus / Bridge / Handler：客户端那一半直接复用
from netserver import ConsoleBus, Room
from plants_data import normalize_rules
from webui import Bridge, Bus

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(BASE_DIR, "web")

# Windows 控制台保险（与 netserver.py / webui.py 同一套）——
# ★ 只挂 errors="replace"，【绝不动 encoding】。理由见 netserver.py 顶部那段长注释
#   （强行改成 cp936 会把中文全变乱码；Python 3.6+ 在真控制台上本来就是对的）。
if sys.platform == "win32":
    for _stream in (sys.stdout, sys.stderr):
        try:
            if _stream is not None:
                _stream.reconfigure(errors="replace")
        except Exception:
            pass

# 座位页端口从 8001 起（避开 webui.py 默认的 8000，两个脚本可以同时开着）；
# 游戏端口自动挑时从 8100 起（避开 netserver 控制台默认的 8090）。
SEAT_PORT_BASE = 8001
GAME_PORT_BASE = 8100
MAX_SEATS = 8

# 座位名默认值。★ 名字只是显示用（引擎认人用 idx），但多开时【必须】区分开，
#   否则战报里一排 player1/player2 谁也认不出哪个标签是哪个。
def _default_name(i):
    return "真人%d" % i


def _as_int(v, default):
    """配置页递来的值可能是数字、字符串、空串、"abc" —— 一律折算成 int。
    （与 netserver._as_int / webui._as_int 同一个口径：脏数据退回默认，绝不抛异常。）"""
    if v is None or v == "":
        return default
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _free_port(preferred, tries=40):
    """从 preferred 起找一个本机空闲端口；找不到返回 0。"""
    for p in range(preferred, preferred + tries):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.bind(("127.0.0.1", p))
        except OSError:
            s.close()
            continue
        s.close()
        return p
    return 0


# ======================================================================
#  座位：一个真·网页客户端（自己的端口 + 自己的 Bus/Bridge）
# ======================================================================
class Seat:
    """一个座位 = 一个 webui.Handler 的派生实例。

    ★ 名字存成 dict（name_ref）而不是字符串：页面进房时要读它预填名字，
      而配置里的名字随时可能改 —— 存字符串的话改一次名字就得重建 HTTP 服务，
      端口跟着变，之前打开的标签页全成废纸。
    """

    def __init__(self, idx, port, name):
        self.idx = idx                  # 1-based 座位号（= 引擎里的 player idx）
        self.port = port
        self.name = name
        self.name_ref = {"name": name}
        self.bus = Bus()                # webui 的事件总线（缓存状态 + 回放）
        self.bridge = Bridge(self.bus)  # 真正的 TCP 客户端
        self.httpd = None

    def url(self):
        return "http://127.0.0.1:%d/" % self.port

    def serve(self):
        """起这个座位的 HTTP 服务（页面 + SSE + 答题接口）。"""
        cls = type("SeatHandler%d" % self.idx, (webui.Handler,), {
            "bus": self.bus,
            "bridge": self.bridge,
            "name_ref": self.name_ref,   # 预填名字、且不写 names.json
            "no_local": True,            # 禁止「本机开一局」（会拆台）
        })
        self.httpd = ThreadingHTTPServer(("127.0.0.1", self.port), cls)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def rename(self, name):
        self.name = name
        self.name_ref["name"] = name      # 下一个打开/刷新的进房页就用新名字

    def close(self):
        self.bridge.disconnect(notify=True)
        if self.httpd is not None:
            try:
                self.httpd.shutdown()
            except Exception:
                pass
            self.httpd = None


# ======================================================================
#  试玩台：房间 + 座位池
# ======================================================================
class Lab:
    """一局试玩 = 一个 netserver.Room + N 个 Seat。

    ★ 座位 HTTP 服务【建了就常驻】，停局只断连接不开端口：
      打完一局你还想看着结束面板、点刷新，端口一关页面就成"无法访问"。
      座位数变少时多出来的服务留着不用（页面上显示"未参赛"），无害。
    """

    def __init__(self, host_ip="127.0.0.1"):
        self.bus = ConsoleBus()                  # netserver 的战报总线（和房主控制台同一份）
        self.room = Room(self.bus, host_ip=host_ip)
        self.seats = []
        self.game_port = 0
        self.error = ""
        self._lock = threading.Lock()

    # ---------- 座位池 ----------
    def _ensure_seat(self, idx, name):
        """确保第 idx 个座位的 HTTP 服务存在，并把名字改成 name。"""
        while len(self.seats) < idx:
            n = len(self.seats) + 1
            port = _free_port(SEAT_PORT_BASE + (n - 1))
            if not port:
                raise RuntimeError("从 %d 起找不到空闲端口（座位 %d）"
                                   % (SEAT_PORT_BASE + (n - 1), n))
            seat = Seat(n, port, _default_name(n))
            seat.serve()
            self.seats.append(seat)
        seat = self.seats[idx - 1]
        seat.rename(name)
        return seat

    def seats_status(self):
        out = []
        for s in self.seats:
            bs = s.bridge.status()
            out.append({"idx": s.idx, "name": s.name, "url": s.url(),
                        "port": s.port, "connected": bool(bs.get("connected")),
                        "server_idx": bs.get("idx")})
        return out

    # ---------- 状态 ----------
    def status(self):
        st = self.room.status()
        st["seats"] = self.seats_status()
        st["lab_error"] = self.error
        st["max_seats"] = MAX_SEATS
        st["seat_port_base"] = SEAT_PORT_BASE
        # ★ 规则元数据本来就在 room.status() 里（rule_options / rules_default），
        #   配置页照它动态生成勾选框 —— 加一条规则只改 plants_data.RULE_OPTIONS。
        return st

    def _emit(self):
        """把最新状态推给试玩台页面。

        ★ 为什么推 type="status"（而不是自造一个 lab_status）：ConsoleBus 只认
          status / clear / msg 三种 —— 别的类型会当成战报进历史，几百条状态快照
          瞬间把战报淹掉。status 是【覆盖式】的，既不进历史，新开的页面
          subscribe 时又能直接拿到一份（还带着 seats）。
        """
        self.bus.push({"type": "status", **self.status()})

    # ---------- 开局 ----------
    def start(self, total=3, bots=0, start_seeds=0, game_port=0,
              rules=None, names=None, open_tabs=True):
        with self._lock:
            if self.room.state in ("waiting", "playing"):
                return False, "已经有一局在跑了，先「停止」"
        total = _as_int(total, 3)
        bots = _as_int(bots, 0)
        start_seeds = max(0, _as_int(start_seeds, 0))
        game_port = _as_int(game_port, 0)
        rules = normalize_rules(rules)          # 与 netserver 同一套折叠口径
        if total < 2:
            return False, "人数至少 2 人"
        if bots < 0 or bots > total:
            return False, "电脑数要在 0 ~ 人数 之间"
        humans = total - bots
        if humans < 1:
            return False, "至少要留 1 个真人座位（全是电脑就不必开试玩台了）"
        if humans > MAX_SEATS:
            return False, "真人座位最多 %d 个" % MAX_SEATS
        if not (0 <= game_port <= 65535):
            return False, "游戏端口要在 0 ~ 65535 之间（0 = 自动挑）"
        if game_port == 0:
            game_port = _free_port(GAME_PORT_BASE)
            if not game_port:
                return False, "从 %d 起找不到空闲的游戏端口" % GAME_PORT_BASE

        # 名字：不够就补默认名，太长截断（服务端那一侧也只收 16 字）
        raw = names if isinstance(names, list) else []
        picked = []
        for i in range(humans):
            v = raw[i] if i < len(raw) else ""
            v = str(v).strip()[:16] if v is not None else ""
            picked.append(v or _default_name(i + 1))

        # ★ 顺序要紧：先建座位服务（端口问题在这一步暴露，还没碰房间），
        #   再开局、再连接。反过来会出现"房间已在等 3 个人，可第 2 个座位端口起不来"。
        try:
            seats = [self._ensure_seat(i + 1, picked[i]) for i in range(humans)]
        except RuntimeError as e:
            self.error = str(e)
            return False, self.error

        # 上一局的连接先断干净，免得 connect() 里那点残留把新局搅混
        for s in self.seats:
            s.bridge.disconnect(notify=True)

        ok, err = self.room.start(total, bots, start_seeds, game_port, rules)
        if not ok:
            self.error = err
            self._emit()
            return False, err
        self.game_port = game_port

        # ---- 串行连接：连接顺序 = 座位号 ----
        # ★ 必须串行。并发连的话谁先被 accept 谁就是 1 号位，标签页和座位号
        #   就对不上了（"我点的是 1 号位，怎么成了 3 号"）。
        for s in seats:
            ok, err = s.bridge.connect("127.0.0.1", game_port, retry=6.0)
            if not ok:
                self.error = "座位 %d 连不上：%s" % (s.idx, err)
                self.room.stop()          # 别留一个永远等人连接的房间
                self._emit()
                return False, self.error
            # ★★ 名字必须在【人齐之前】报给服务端：Room 开局时会把名字快照一次
            #    （player_names），晚到的名字本局不生效 —— 战报里就全是 player1/2/3。
            #    这时连接刚建好，服务端的读线程一 accept 就能读到这条报到消息。
            s.bridge.send_name(s.name)

        self.error = ""
        self._emit()
        if open_tabs:
            threading.Thread(target=open_seat_tabs, args=(seats,), daemon=True).start()
        return True, None

    # ---------- 停止 ----------
    def stop(self):
        ok, err = self.room.stop()        # 内部会关监听口 + 所有 socket，并 join 收尾
        for s in self.seats:
            s.bridge.disconnect(notify=True)
        # ★ Room.stop() 只 join 2 秒就返回，引擎可能还在收尾 —— 这时立刻回 status
        #   会拿到 "playing"，页面看着像"没停下来"。等它真的落到 ended 再回。
        deadline = time.time() + 3.0
        while time.time() < deadline and self.room.state in ("waiting", "playing"):
            time.sleep(0.05)
        self._emit()
        return True, err


# ======================================================================
#  打开标签页
# ======================================================================
def _open_tab(url):
    """试一下真的能调起浏览器 —— 返回 True/False，绝不抛异常。

    ★ 为什么不能直接 webbrowser.open 就完事：在 Termux（手机）和任何无桌面
      环境里，webbrowser.open 是【静默失败】—— 返回 False，也不抛异常。
      用户那边看到的就是"点了开局，一个标签都没开"，还以为程序坏了。
      所以这里把返回值收上来，没人开得出来就改成打印可点的链接。"""
    try:
        return bool(webbrowser.open(url))
    except Exception:
        return False


def open_seat_tabs(seats):
    """依次打开座位页。★ 中间要 sleep：连续调 webbrowser.open 时，某些浏览器
    会把后几次合并成一个标签（用户看起来就是"只开了 1 个页"）。

    【v0.74】返回成功打开的数量；一个都没打开（手机 Termux / 无桌面）时
    打印一句"去页面上点座位链接"，否则整件事就是静默无声的。"""
    opened = 0
    for s in seats:
        if _open_tab(s.url()):
            opened += 1
        time.sleep(0.45)
    if seats and not opened:
        print("\n[提示] 没能自动打开浏览器（手机 / 无桌面环境都这样）。")
        print("       请在试玩台页面的「座位」表里点链接 —— 每个座位一行，")
        print("       点开就是一个独立标签页，一个标签 = 一个真人。")
    return opened


# ======================================================================
#  试玩台的 HTTP 层（配置页 + 战报 SSE）
# ======================================================================
MIME = {".html": "text/html; charset=utf-8",
        ".css": "text/css; charset=utf-8",
        ".js": "application/javascript; charset=utf-8",
        ".svg": "image/svg+xml",
        ".png": "image/png"}


class LabHandler(BaseHTTPRequestHandler):
    lab = None

    def log_message(self, fmt, *args):
        pass

    def handle_one_request(self):
        """浏览器读完响应就关连接 → Windows 下抛 WinError 10053，
        socketserver 会当成"处理请求出错"打一大段 traceback。直接吞掉
        （与 netserver.py / webui.py 同一个处理）。"""
        try:
            BaseHTTPRequestHandler.handle_one_request(self)
        except OSError:
            self.close_connection = True

    # ---------- GET ----------
    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/index.html", "/lab.html"):
            return self._file("lab.html")
        if path == "/lab.js":
            return self._file("lab.js")
        if path == "/style.css":
            return self._file("style.css")
        if path == "/api/lab/status":
            return self._json(self.lab.status())
        if path == "/api/lab/events":
            return self._sse()
        self.send_error(404)

    # ---------- POST ----------
    def do_POST(self):
        path = self.path.split("?")[0]
        body = self._read_json()

        if path == "/api/lab/start":
            ok, err = self.lab.start(
                body.get("total"), body.get("bots"),
                body.get("start_seeds"), body.get("game_port"),
                body.get("rules"), body.get("names"),
                open_tabs=body.get("open_tabs", True) is not False)
            return self._json({"ok": ok, "err": err, "status": self.lab.status()})

        if path == "/api/lab/stop":
            ok, err = self.lab.stop()
            return self._json({"ok": ok, "err": err, "status": self.lab.status()})

        if path == "/api/lab/open":
            # 把已经建好的座位页再打开一遍（标签页被关掉 / 打完一局想重看时用）
            seats = [s for s in self.lab.seats if s.bridge.status().get("connected")]
            if not seats:
                seats = list(self.lab.seats)
            threading.Thread(target=open_seat_tabs, args=(seats,), daemon=True).start()
            return self._json({"ok": True, "err": None, "count": len(seats)})

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
        self.send_header("Cache-Control", "no-store")   # 改页面立刻生效
        self.end_headers()
        self.wfile.write(data)

    def _sse(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        q = self.lab.bus.subscribe()      # 订阅的是 ConsoleBus：新连接先拿状态、再回放战报
        try:
            while True:
                try:
                    obj = q.get(timeout=15)
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    continue
                data = json.dumps(obj, ensure_ascii=False)
                self.wfile.write(("data: %s\n\n" % data).encode("utf-8"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            self.lab.bus.unsubscribe(q)


# ======================================================================
#  启动
# ======================================================================
USAGE = """用法：
  python playtest.py                起试玩台并自动打开配置页（默认 8300）
  python playtest.py --port 8400    配置页端口
  python playtest.py --no-browser   不自动开浏览器

页面里配好 人数 / 电脑数 / 开局种子 / 游戏端口 / 座位名 / 房间规则，
点「一键开局」：服务端 + N 个座位页（真·网页客户端）一起就绪，并自动开标签。"""


def parse_args(argv):
    opt = {"port": 8300, "browser": True}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--no-browser":
            opt["browser"] = False
        elif a == "--port" and i + 1 < len(argv):
            i += 1
            try:
                opt["port"] = int(argv[i])
            except ValueError:
                print("--port 后面要跟数字")
                return None
        elif a in ("-h", "--help"):
            print(USAGE)
            return None
        else:
            print("不认识的参数：%s\n%s" % (a, USAGE))
            return None
        i += 1
    return opt


def pick_port(preferred):
    """配置页端口：优先用 preferred，被占就顺着往后找（与 webui.pick_port 同一口径）。"""
    for p in range(preferred, preferred + 12):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.bind(("127.0.0.1", p))
        except OSError:
            s.close()
            continue
        s.close()
        if p != preferred:
            print("配置页端口 %d 被占用，改用 %d" % (preferred, p))
        return p
    print("从 %d 起的 12 个端口都被占用了，请用 --port 指定别的端口。" % preferred)
    return None


def main():
    opt = parse_args(sys.argv[1:])
    if opt is None:
        return 1
    if not os.path.isfile(os.path.join(WEB_DIR, "lab.html")):
        print("缺少前端文件：%s" % os.path.join(WEB_DIR, "lab.html"))
        return 1

    port = pick_port(opt["port"])
    if port is None:
        return 1

    lab = Lab()
    LabHandler.lab = lab
    try:
        httpd = ThreadingHTTPServer(("127.0.0.1", port), LabHandler)
    except OSError as e:
        print("配置页端口 %d 起不来：%s" % (port, e))
        return 1

    url = "http://127.0.0.1:%d/" % port
    print("《植物斗争》本地试玩台已启动：%s" % url)
    print("  · 在这里配 人数 / 电脑数 / 种子 / 端口 / 规则，点「一键开局」。")
    print("  · 每个真人座位会开一个独立的网页客户端（端口从 %d 起），"
          "全是真人 = 全是你在不同标签里操作。" % SEAT_PORT_BASE)
    print("  · 手机（Termux）：浏览器打不开标签是正常的 —— "
          "在页面的「座位」表里点链接，一个标签一个真人。")
    print("  · Ctrl+C 退出。")

    if opt["browser"]:
        def _open_lab():
            if not _open_tab(url):
                print("[提示] 浏览器没能自动打开，请手动访问上面的地址。")
        threading.Timer(0.4, _open_lab).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n正在关闭……")
    finally:
        try:
            lab.stop()
        except Exception:
            pass
        for s in lab.seats:
            s.close()
        try:
            httpd.shutdown()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
