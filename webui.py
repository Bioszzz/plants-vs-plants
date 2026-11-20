# -*- coding: utf-8 -*-
"""
webui.py —— 网页版客户端（在浏览器里玩的联网客户端）

用法：
  python webui.py                        起客户端界面并自动打开浏览器
                                         → 页面上填「服务器 IP / 端口」点连接
                                         → 或点「本机开一局（你 vs 电脑）」直接开打
  python webui.py --server 1.2.3.4:8080  启动即连（跳过手填）
  python webui.py --port 8100            指定【本地页面】端口（默认 8000）
  python webui.py --no-browser           不自动开浏览器

它是什么（重要）：
  它是【纯客户端】—— 浏览器版的 netclient，不是自带一局的玩具。
        （【v0.68】原来的 tkinter 图形客户端 gui_client.py 已删除，图形界面只剩网页端。）
        浏览器  ←HTTP/SSE→  本进程(本地桥)  ←TCP JSON→  netserver.py(真正的服务端)
  规则、结算、回合推进全都在远端的 netserver.py / engine.py 里，
  本进程只做两件事：把页面发给浏览器，把浏览器和服务端之间的消息搬来搬去。
  所以「和谁打、打到几个人、谁是电脑」由【你连的那个服务端】决定。

两种开局方式：
  ① 连别人的/自己的服务器：房主运行 python netserver.py，
     其他人在页面上填房主的 IP（局域网用内网 IP）后点连接。
     【v0.22】房主不带参数运行时会自带一个【房主控制台】网页（默认 8090）：
     在浏览器里配 人数 / 电脑数 / 开局种子 / 游戏端口，点「开局」后全程观战。
  ② 本机开一局：页面上的「本机开一局」按钮会在后台起一个 netserver
     再自己连上去 —— 双击 网页版.bat 走的就是这条。
     【v0.22】人数 / 电脑数 / 开局种子 都可以在按钮旁边填（默认 2 人局、1 电脑、
     开局种子 0 = 空手起手，也就是旧行为）。

打完一局之后：
  结束面板上给「再来一局」和「换个服务器」。本机局会重新起一个 netserver 再连；
  远程局则是重连同一个地址（但 netserver 打完一局就退出了，所以要房主重开才行）。

关于"出局"（v0.15 的硬边界，别搞混）：
  出局 ≠ 掉线。玩家 hp 归零后，引擎只是【不再问他】，连接照旧开着、场面照旧推 ——
  他留在牌桌上观战 —— 而且【现在就可能被复活】（161 生命之花之类，v0.71 措辞更正）。
  真正会关连接的只有两件事：
  连接断了、或整局结束了。

为什么选网页（而不是 tkinter / pygame）：
  · 视觉上限最高 —— CSS 是唯一能做到"上架级"排版与动效的选择
  · 零第三方依赖 —— http.server + json + socket + threading 全是标准库
  · 引擎零改动复用 —— 就是标准联机协议，引擎完全不知道有"网页"这回事
"""

import atexit
import json
import os
import queue
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import netproto
import updater
from plants_data import PLANTS, PLAYABLE, rule_options

# Windows 控制台保险（v0.43 更正）—— ★ 只挂 errors="replace"，【绝不动 encoding】。
#
#   ★★ 为什么绝不能改 encoding（这是长期潜伏的乱码根源）：
#      以前这里会按 GetConsoleOutputCP() 把编码改成控制台代码页（cmd 下 = cp936），
#      以为能治乱码。真机实测恰恰相反 ——
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

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(BASE_DIR, "web")

# ---------- 【v0.19】版次与检查更新 ----------
#
# 版本号的唯一来源 = 仓库根目录的 VERSION 文件，它由 `tools/stamp_version.py`
# 从最新的 git tag 生成（见 开发文档 第九章第 5 步）。为什么不直接读 git？
#   ① 发给别人的可能不是 git 仓库（就是解压出来的一包文件），git 命令可能不存在；
#   ② 让每次发版都必须重新生成 VERSION —— 忘了生成就会显出旧号，比"悄悄发新版"好。
#
# 检查更新 = 只读查询远端 Releases 的最新 tag。
#   【v0.69 改】以前查到新版只给个发布页链接、让玩家自己下 —— 用户当场问
#   "那这个的意义何在呢？我要能自动更新的"。于是加了真正的一键更新：
#   `updater.py` 负责下载 / 校验 / 备份 / 覆盖，这里只做两件事——
#     GET  /api/update-check   查（只读，不碰磁盘）
#     POST /api/update-install 装（这是唯一会改磁盘的接口）
#   ★ 为什么现在"自动覆盖"是安全的（以前不敢做的原因逐条解决了）：
#     · 只覆盖 **zip 里真实存在的那 23 个文件**，绝不删任何东西 ——
#       names.json / dist/ / _backup/ 都不在包里，碰不到；
#     · 先校验（路径不许越界、顶层目录唯一、关键文件齐全、解压后真 import 一次），
#       校验不过一个字节都不写；
#     · 覆盖前把要动的旧文件备份到 `_backup/v旧_时间戳/`；
#     · 覆盖后必须重启才生效（当前进程跑的还是旧代码），接口会明确回报这一点。
#
# ★★【v0.65】查询目标定在 Gitee：api.github.com 对未登录请求一律 404
#   （实测 check_latest_release() → None ｜ err: HTTP 404），而 Gitee 的公开仓库
#   匿名就能查、国内下载也快，发给玩家更合适。
#   ★ 客户端只认这【一个】公开仓库；任何令牌都不许进客户端（没有例外）。
#   ★ 这个文件会随 Release 发出去 —— 注释里不要写仓库归属之外的任何东西。
GITEE_REPO = "Bioszzz/plants-vs-plants"         # 公开：纯净代码，检查更新走这里
VERSION_FILE = os.path.join(BASE_DIR, "VERSION")
RELEASES_PAGE = "https://gitee.com/%s/releases" % GITEE_REPO


def read_version():
    """"v0.19" 这样的版本号字符串；读不到就返回空串（界面显示"未知"）"""
    try:
        with open(VERSION_FILE, "r", encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def _ver_key(v):
    """把 "v0.19" / "0.19" / "1.2.3" 变成可比较的元组，别拿字符串比大小
    （字符串比大小 "v0.9" > "v0.10"，正是这个项目会踩的坑）"""
    nums = re.findall(r"\d+", v or "")
    return tuple(int(x) for x in nums) if nums else (0,)


def check_latest_release(timeout=6):
    """问 Gitee 要最新发行版。返回 (latest, err, notes, asset_url, asset_size)。

    拿到就 err=None；notes = 该发行版的说明正文（可能为空串）；
    asset_url = 那个 zip 的下载地址（没有 zip 时为空串）。

    ★ 为什么不用 GitHub：那个仓库是私有的，匿名请求一律 404（连自己的 Release
      也查不到）。详见上面常量区的说明。
    ★ 三种失败要分开说，别都糊成"网络不通"：
        404            → 发布页上还没有任何发行版（不是错，是还没发）
        其它 HTTP      → 服务端问题
        连不上/超时     → 网络问题（国内访问 GitHub 时很常见）
      失败必须安静地变成一句提示，绝不能影响界面其它功能。
    ★★【v0.69】实现搬到 `updater.fetch_latest()` —— 它还要拿 zip 的下载地址，
      两处各写一份的话"哪个字段叫什么"迟早分岔（v0.66 闪电链的教训）。
      这里保留函数名和五元组签名，调用方不用改。
    """
    info = updater.fetch_latest(timeout=timeout)
    return (info["tag"] or None, info["err"] or None, info["notes"],
            info["asset_url"], info["asset_size"])


MIME = {".html": "text/html; charset=utf-8",
        ".css": "text/css; charset=utf-8",
        ".js": "application/javascript; charset=utf-8",
        ".svg": "image/svg+xml",
        ".png": "image/png"}

LOCAL = {"proc": None}       # 「本机开一局」起的 netserver 子进程

# ---------- 【v0.35】名字缓存 ----------
# 你 2026-09-30 原话："起的名字可以缓存"，方案选的是"在本体所在文件夹弄个
# json/txt 之类的吧，也好删改"。
#   · 文件放仓库根目录（= webui.py 同级）—— 肉眼找得到，记事本能直接改 / 删；
#   · 已进 .gitignore（个人的东西，不该跟着仓库走）；
#   · 格式故意人可读：
#       {"player": "小明", "recent": ["小明", "阿花"]}
#     player = 上次用的名字（下次自动带出）；recent = 最近用过的几个（下拉备选）。
NAMES_FILE = os.path.join(BASE_DIR, "names.json")
NAMES_RECENT_MAX = 8


def read_names():
    """读名字缓存。★ 一律容错：文件不在 / 坏了 / 结构不对 → 返回空档，
    绝不让一个坏 json 把客户端启动搞崩（与 webui 里其它 I/O 一个口径）。"""
    blank = {"player": "", "recent": []}
    try:
        with open(NAMES_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return blank
    if not isinstance(data, dict):
        return blank
    player = data.get("player")
    player = player.strip() if isinstance(player, str) else ""
    recent = []
    raw = data.get("recent")
    if isinstance(raw, list):
        for x in raw:
            if isinstance(x, str) and x.strip() and x.strip() not in recent:
                recent.append(x.strip())
            if len(recent) >= NAMES_RECENT_MAX:
                break
    return {"player": player, "recent": recent}


def write_names(player, recent=None):
    """把名字落盘。★ 写失败【不抛异常】—— 缓存而已，写不进也不该影响这局。"""
    player = (player or "").strip() if isinstance(player, str) else ""
    data = read_names()
    if recent is None:
        # 保留旧备选，并把新名字插到最前（去重、截断）
        recent = [player] + [x for x in data.get("recent", []) if x != player]
        recent = [x for x in recent if x][:NAMES_RECENT_MAX]
    else:
        recent = [str(x).strip() for x in recent if str(x).strip()][:NAMES_RECENT_MAX]
    try:
        with open(NAMES_FILE, "w", encoding="utf-8") as f:
            json.dump({"player": player, "recent": recent},
                      f, ensure_ascii=False, indent=2)
        return True
    except OSError:
        return False


# 【v0.15】记住"上一局是怎么开的"，给结束面板上的「再来一局」用。
#   mode="local"  → 按 total/bots/start_seeds 重新起一个本机 netserver 再连；
#   mode="remote" → 重连同一个 host:port（能不能成，取决于房主有没有重开服务端）。
SESSION = {"mode": None, "host": None, "port": None,
           "total": 2, "bots": 1, "start_seeds": 0, "rules": {}}


def _no_prefix(no):
    """编号的【耗种段】：编号 = 耗种段 + 段内序号（末位）。
    "01"→"0"、"11"→"1"、"45"→"4"、"101"→"10"（三位编号末位也是序号）。
    前端搜索框靠它实现"输 4 搜出耗种 4 的所有植物"。"""
    return no[:-1] if len(no) >= 2 else ""


def _as_int(v, default):
    """表单递来的值可能是数字、字符串、空串、null、"abc" —— 一律折算成 int。

    【v0.22】脏数据退回 default，绝不抛异常：一个填错的输入框不该把请求打崩。
    （和 netserver.py 里那个同名函数是同一个道理，两边各自留一份，
      免得为了一个三行的小工具把两个模块绑在一起。）"""
    if v is None or v == "":
        return default
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _rule_args(rules):
    """【v0.25】把房间规则字典折成 netserver 的 --rule 参数列表。

    本进程【不解释】规则（那属于服务端 / plants_data）：这里只把前端递来的
    键值对原样转成命令行，合法性交给 netserver 的 normalize_rules 折。
    键名只允许 [A-Za-z_][A-Za-z0-9_]* —— 免得有人从表单塞进奇怪的东西。"""
    if not isinstance(rules, dict):
        return []
    out = []
    for k, v in rules.items():
        k = str(k)
        if not k or k[0].isdigit() or not k.replace("_", "").isalnum():
            continue
        out += ["--rule", "%s=%s" % (k, v)]
    return out


def update_check():
    """给 /api/update-check 用的封装：本地版次 vs Gitee 公开发行版的最新 tag。
    **只做比较，不下载任何东西**（下载/安装是下面 install_update 的事）。"""
    cur = read_version()
    latest, err, notes, asset_url, asset_size = check_latest_release()
    if err:
        return {"ok": False, "err": err, "version": cur, "page": RELEASES_PAGE}
    has = _ver_key(latest) > _ver_key(cur)
    return {"ok": True, "has_update": has,
            "version": cur, "latest": latest, "page": RELEASES_PAGE,
            # 【v0.65】把发行版说明也带上：前端只显示一行摘要，免得玩家还得点出去看
            "notes": notes,
            # 【v0.69】能不能一键更新：有 zip 地址才给按钮，否则只给发布页
            "can_update": bool(has and asset_url),
            "size": asset_size}


# ★ 同一时刻只允许一个更新在跑：两个标签页同时点"立即更新"，
#   会一个覆盖到一半、另一个又开始解压 —— 那个半新半旧的目录最难救。
UPDATE_LOCK = threading.Lock()


def install_update():
    """下载 + 校验 + 备份 + 安装最新版。给 POST /api/update-install 用。

    返回 dict(ok, from, to, backup, files, need_restart, err)。

    ★ 为什么这个接口必须 POST：它会改磁盘。GET 会被浏览器的预取、
      刷新重发、"检查更新"按钮的幂等重试轻易触发 —— 绝对不行。
    ★ 整个过程可能几秒到几十秒（下载 270 KB + import 试跑），
      HTTP 请求会一直等着；前端要把按钮改成"更新中…"并禁用。
    """
    if not UPDATE_LOCK.acquire(False):
        return {"ok": False, "err": "已经有一个更新在进行了"}
    try:
        cur = read_version()
        info = updater.fetch_latest()
        if info["err"]:
            return {"ok": False, "err": info["err"]}
        if not info["tag"]:
            return {"ok": False, "err": "发布页上还没有发行版"}
        if _ver_key(info["tag"]) <= _ver_key(cur):
            return {"ok": False, "err": "已是最新（%s）" % cur}
        if not info["asset_url"]:
            return {"ok": False, "err": "这个发行版没有附带 zip"}
        try:
            binary = updater.download(info["asset_url"],
                                      expect_size=info["asset_size"])
            res = updater.install(binary, base=BASE_DIR,
                                  expect_ver=info["tag"], old_ver=cur)
        except updater.BadPackage as exc:
            return {"ok": False, "err": "包不能用：%s" % exc}
        except Exception as exc:
            return {"ok": False, "err": "%s: %s" % (type(exc).__name__, exc)}
        return {"ok": True, "from": cur, "to": info["tag"],
                "backup": res["backup"], "files": res["files"],
                # ★ 当前进程跑的还是旧代码 —— 不重启的话，界面是新版静态文件、
                #   后端是旧逻辑，两边协议一旦有差异就是最难查的那种错。
                "need_restart": True}
    finally:
        UPDATE_LOCK.release()


def build_catalog():
    """植物图鉴（结构化）—— 前端拿它渲染选牌卡片，不用去解析中文 line 文本"""
    items = []
    for n in PLAYABLE:
        s = PLANTS[n]
        a, d = s.attack, s.defense
        items.append({
            "name": n, "desc": s.desc, "line": s.line(),
            # 编号（原稿称"序列"）：no=完整编号（如"45"），no_prefix=耗种段（如"4"）。
            # 留空 = 编号未确认（目前只剩辣椒），只能按名称搜。
            "no": s.no, "no_prefix": _no_prefix(s.no),
            "cost_seed": s.cost_seed, "cost_hp": s.cost_hp,
            "attr": a.attr, "bao": a.bao, "lian": a.lian, "san": a.san,
            "damage": a.damage, "quan": a.quan,
            # 【v0.24】填【实际】无视的标签（属性通用表 ∪ 本株声明），
            #   不是填 a.ignore_tags —— 后者对火苗花/冬虫夏草是空的，
            #   前端拿到的就会是"这株不能穿木盾"（错的）。
            #   键名保持 ignore_tags 不变（前端老代码不用动）。
            "ignore_tags": list(a.ignored_tags()),
            "ignore_plants": list(a.ignore_plants),
            "fang": d.fang, "tags": list(d.tags),
            "immune_attrs": list(d.immune_attrs),
            "immune_plants": list(d.immune_plants),
            "seed_gain": s.seed_gain, "heal": s.heal, "charge": s.charge,
            "water_gain": s.water_gain, "fire_gain": s.fire_gain,
        })
    # 【v0.49】顺带把"可选规则集"的元数据带上 —— 本地开局页照它【动态生成】
    #   勾选框，以后加一条规则只改 plants_data.RULE_OPTIONS，不用动前端。
    return {"plants": items, "rule_options": rule_options()}


# ======================================================================
#  事件总线：SSE 广播 + 回放
# ======================================================================
class Bus:
    """把服务端来的事件推给浏览器，并【缓存状态】。

    ★ 为什么必须缓存：服务端是"先把问题推出来、再阻塞等答案"的。
      浏览器要是刷新了页面、或者晚一步才连上 SSE，那一题已经推过去了 ——
      不回放的话界面会永远停在"连接中……"，而服务端在旁边干等，
      两边都没有报错，是那种最难查的静默死锁。
      所以这里存两类：历史（战报）+ 当前待答（ask / wait），新连接先回放一遍。
    """

    MAX_HISTORY = 400

    def __init__(self):
        self._lock = threading.Lock()
        self._listeners = []
        self._history = []          # msg / hello / end —— 战报类，全量回放（有上限）
        self._pending = None        # 当前待答或等待提示，覆盖式，只留最新一条
        # 【v0.77】最新一帧结构化场面。覆盖式：刷新页面只要最后那一帧，
        #   不像战报那样需要全量回放。
        self._state = None
        self._conn = {"type": "conn", "connected": False, "text": ""}

    def subscribe(self):
        q = queue.Queue()
        with self._lock:
            q.put(dict(self._conn))
            for ev in self._history:
                q.put(ev)
            if self._state is not None:
                q.put(self._state)
            if self._pending is not None:
                q.put(self._pending)
            self._listeners.append(q)
        return q

    def unsubscribe(self, q):
        with self._lock:
            if q in self._listeners:
                self._listeners.remove(q)

    def push(self, obj):
        """所有事件的唯一入口 —— 按类型分流，再广播"""
        t = obj.get("type")
        with self._lock:
            if t == "conn":
                self._conn = obj
                self._pending = None            # 连接状态变了，旧问题作废
                if obj.get("connected"):
                    # 【v0.15】新连上一次 = 新的一局：把上一局的战报清掉。
                    #   否则"再来一局"开出来的新局，日志里会连着上一局的几百条。
                    self._history = []
            elif t == "state":
                # 覆盖式，不进历史：刷新页面只要最新那一帧
                self._state = obj
            elif t in ("ask", "wait"):
                self._pending = obj
            else:                               # msg / hello / end
                if t == "end":
                    self._pending = None
                self._history.append(obj)
                del self._history[:-self.MAX_HISTORY]
            listeners = list(self._listeners)
        for q in listeners:
            q.put(obj)

    def clear_pending(self):
        """玩家已答完 —— 撤掉待答状态，免得刷新页面又冒出个答过的问题"""
        with self._lock:
            self._pending = None


# ======================================================================
#  本地桥：浏览器 ←→ 远端 netserver
# ======================================================================
class Mirror:
    """【v0.76】本地影子引擎 —— 「服务器友好」和「自动录像」的客户端那半边。

    服务端把「这一局的头部 + 增量答案」推过来，这里在本地把引擎重跑一遍，
    自己产出战报。于是"服务器友好"真的省掉了服务端的中文战报带宽：

        中文战报 50~300 KB/局   →   答案序列 2~5 KB/局

    顺带也就得到了能存盘的录像（自动录像走的是同一条通道）。

    ★ 为什么是"整段重跑"而不是"增量推进"：`replay.run_replay` 本来就有
      "答案用完就停下"的能力（ReplayIncomplete），一遍 build + run 就够，
      不用维护一个长活的 Game 对象和它的线程同步。代价是每收到一批答案
      要把前面重跑一次 —— 实测每回合几毫秒到几十毫秒，而且在后台线程，
      玩家感觉不到。
    ★ 前提：引擎确定性（同一串输入 → 逐字相同的战报）。这个由项目自己保证，
      也已经实测过跨进程一致。如果玩家私自改了 engine.py，战报会和服务端不同，
      但那只影响他自己看到的东西。
    """

    # 攒一小会儿再重跑：一回合里有好几次 gather（出牌 / 释放 / 目标 / 连击分配），
    # 每批都立刻重跑的话，一回合要跑十遍。
    COALESCE = 0.12

    def __init__(self, bus, on_broken=None):
        self.bus = bus
        # 【v0.77】本地算崩时通知外面（Bridge 会关掉「服务器友好」、让服务端恢复发战报）。
        #   ★ 这不是"防御性编程讨好用户"，是**这类失败必须能自愈**：
        #     默认已改成友好模式，算不出来又没人兜底，玩家看到的就是一片空白。
        self.on_broken = on_broken
        self._lock = threading.Lock()
        self.data = None        # 头部 + 累积的答案（None = 这一局还没开始）
        self.shown = 0          # 已经推给前端多少行战报
        self._state = None      # 最后推过的那一帧场面（服务端不再发了，这里自己算）
        self.save_file = False  # 「自动录像」：要不要顺手落盘
        # 【v0.77】要不要把本地算出来的东西推到前端。
        #   ★ 只有「服务器友好」才推：不友好的时候服务端本来就发了战报和场面，
        #     本地再推一遍就是重复（自动录像只勾了"存盘"，不需要推）。
        self.push_events = True
        self._timer = None

    # ---------- 收 ----------
    def reset(self):
        with self._lock:
            self.data = None
            self.shown = 0
            self._state = None
            t, self._timer = self._timer, None
        if t is not None:
            t.cancel()

    def head(self, data):
        """开局 / 回退重建：服务端发来本局的头（人数 / 种子 / 规则 / 牌库）。

        ★ 收到 head 就把本地累积清空重来 —— 回退后旧的那串答案已经作废了。
        """
        if not isinstance(data, dict):
            return
        with self._lock:
            d = dict(data)
            d["answers"] = []
            self.data = d
            self.shown = 0
            self._state = None
        # 【v0.77】开局那一帧：此时还没有任何答案，重放跑不出场面，
        #   所以直接建一局取初始 state —— 否则「服务器友好」下前端开局是空白卡片。
        if self.push_events:
            try:
                import replay as _replay
                st = _replay.build(dict(d)).state_json()
            except Exception:       # noqa: BLE001 —— 算不出来也不该拦着开局
                st = None
            if st:
                with self._lock:
                    self._state = st
                self.bus.push({"type": "state", "state": st})
        self._schedule()

    def rows(self, add):
        if not add:
            return
        with self._lock:
            if self.data is None:
                return          # 还没收到 head —— 丢掉，等 head 来了从头算
            self.data["answers"].extend(add)
        self._schedule()

    def finish(self):
        """局末：把最后一批跑完，再（可选）落盘。"""
        self._flush()
        if not self.save_file:
            return
        nm = self._save(running=False)
        if nm:
            self.bus.push({"type": "msg",
                           "text": "（本局录像已存到 对局录像/%s　重放：python replay.py）" % nm})

    # ---------- 算 ----------
    def _schedule(self):
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
            t = threading.Timer(self.COALESCE, self._flush)
            t.daemon = True
            self._timer = t
        t.start()

    def _snapshot(self):
        with self._lock:
            if self.data is None:
                return None
            d = dict(self.data)
            d["answers"] = list(d["answers"])
            return d

    def _flush(self):
        with self._lock:
            t, self._timer = self._timer, None
        if t is not None:
            t.cancel()
        data = self._snapshot()
        if not data:
            return
        try:
            import replay as _replay
            _g, lines = _replay.run_replay(data)
        except Exception as e:      # noqa: BLE001 —— 本地算炸了不能让页面卡死
            # 【v0.77】算不出来 = 有 Bug（同一串输入，服务端算得出、本地算不出，
            #   多半是引擎/数据不一致）。处理顺序：先说人话 → 立刻自愈 → 留证据。
            self._degrade(e)
            return
        with self._lock:
            start = self.shown
            # ★★【2026-11-20】重跑出来的战报【比上次还短】= 两边已经分叉。
            #   同一串答案重跑只会越跑越长；变短说明"这一次重跑在该问的地方
            #   没问、答案落到了别的题上"—— 2026-11-20 那次就是这么空白的：
            #   引擎把三位编号的前段算成 1 → 该出的刀题没出 → 本地和服务端
            #   的问题序列对不上 → 战报停在那儿不动，玩家看着就是"卡死"。
            #   真遇到就立刻降级，别再静默地什么都不推。
            if len(lines) < start:
                self.shown = len(lines)
                self._degrade("本地重算的战报比上一版还短（%d < %d）—— "
                              "本地和服务端的问题序列分叉了"
                              % (len(lines), start))
                return
            self.shown = len(lines)
        if not self.push_events:
            # 只存盘、不推：服务端会自己发战报和场面，别重复
            if self.save_file:
                self._save(running=True)
            return
        for t2 in lines[start:]:
            self.bus.push({"type": "msg", "text": str(t2)})
        # 【v0.77】场面也由本地算：服务端在「服务器友好」下连 state 都不发。
        #   ★ 只推最后一帧 —— 前端画卡片只需要"现在什么样"。
        frames = getattr(_g, "replay_states", None)
        if frames:
            last = frames[-1]
            with self._lock:
                changed = self._state is not last
                self._state = last
            if changed:
                self.bus.push({"type": "state", "state": last})
        if self.save_file:
            self._save(running=True)

    def _degrade(self, err):
        """本地算不出来 / 算得不对 → 说人话 + 立刻切回"服务器发战报"。"""
        self.bus.push({"type": "msg",
                       "text": "（本地重算失败：%s\n"
                               "  这是个 Bug，不是你操作错。已经切回『服务器直接发战报』，"
                               "这一局照常继续。\n"
                               "  想帮上忙的话：点右上角「⭳ 录像」把这一局导出来，发给作者。）"
                               % err})
        self.push_events = False
        if self.on_broken is not None:
            try:
                self.on_broken(err)
            except Exception:   # noqa: BLE001 —— 自愈动作本身失败也不许再炸
                pass

    # ---------- 存 ----------
    def _save(self, running=False):
        data = self._snapshot()
        if not data:
            return None
        d = os.path.join(BASE_DIR, "对局录像")
        try:
            if not os.path.isdir(d):
                os.makedirs(d)
            if running:
                nm = "客户端_进行中.json"
            else:
                nm = "客户端_%s_%d人局.json" % (
                    time.strftime("%Y%m%d_%H%M%S"), int(data.get("players") or 0))
            with open(os.path.join(d, nm), "w", encoding="utf-8", newline="\n") as f:
                json.dump(data, f, ensure_ascii=False, indent=1)
            if not running:
                busy = os.path.join(d, "客户端_进行中.json")
                if os.path.isfile(busy):
                    os.remove(busy)
            return nm
        except Exception:          # noqa: BLE001 —— 存不下来不影响对局
            return None


class Bridge:
    """一个浏览器玩家 ←→ 一个远端服务端。

    对服务端来说本进程就是个普通联网客户端（和 netclient.py 一模一样），
    只是把"在终端里 print"换成了"通过 SSE 推给浏览器"。
    """

    def __init__(self, bus):
        self.bus = bus
        self._lock = threading.Lock()
        self._sock = None
        self._conn_id = 0        # 每连一次 +1，用来识别"旧读线程"的收尾
        self.host = None
        self.port = None
        self.idx = None
        self.total = None
        # 【v0.76】玩家选项（浏览器那边勾的，连接后随时可改）。
        #   friendly = 服务器友好（自己算战报）；record = 自动录像（另存一份）。
        self.prefs = {"friendly": False, "record": False}
        self.mirror = Mirror(bus, on_broken=self._mirror_broken)
        self._mirror_broken_once = False

    # ---------- 状态 ----------
    def status(self):
        with self._lock:
            return {"connected": self._sock is not None,
                    "host": self.host, "port": self.port,
                    "idx": self.idx, "total": self.total}

    # ---------- 连接 ----------
    def connect(self, host, port, retry=0.0):
        """连远端。retry 秒内反复重试 —— 本机开一局时服务端要一点时间才 listen。"""
        self.disconnect(notify=False)
        deadline = time.time() + max(0.0, retry)
        while True:
            try:
                sock = socket.create_connection((host, port), timeout=4.0)
                break
            except OSError as e:
                if time.time() >= deadline:
                    return False, "连不上 %s:%d（%s）" % (host, port, e)
                time.sleep(0.25)
        sock.settimeout(None)

        with self._lock:
            self._sock = sock
            self._conn_id += 1
            cid = self._conn_id
            self.host, self.port = host, port
            self.idx = self.total = None

        threading.Thread(target=self._read_loop,
                         args=(sock, netproto.Reader(sock), cid),
                         daemon=True).start()
        # 【v0.76】重连之后把玩家选项再报一次 —— 服务端那边是全新的一条连接，
        #   不记得上一局勾了什么。不补这一下，断线重连就悄悄退回"什么都不要"。
        if self.prefs["friendly"] or self.prefs["record"]:
            try:
                netproto.send_msg(sock, {"type": "prefs",
                                         "friendly": self.prefs["friendly"],
                                         "record": self.prefs["record"]})
            except OSError:
                pass
        self.bus.push({"type": "conn", "connected": True,
                       "host": host, "port": port})
        return True, None

    def disconnect(self, notify=True):
        with self._lock:
            sock = self._sock
            self._sock = None
            self._conn_id += 1          # 让还在跑的旧读线程安静地退出
            self.host = self.port = None
            self.idx = self.total = None
        if sock is None:
            return
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            sock.close()
        except OSError:
            pass
        if notify:
            self.bus.push({"type": "conn", "connected": False,
                           "text": "已断开连接"})
        # 【v0.76】影子引擎的状态跟着连接一起作废（换一局/换一台服就无意义了）
        self.mirror.reset()

    # ---------- 服务端 → 浏览器 ----------
    def _read_loop(self, sock, reader, cid):
        while True:
            try:
                m = reader.recv()
            except (OSError, ValueError):
                m = None
            if m is None:
                break
            if isinstance(m, dict) and m.get("type") == "hello":
                with self._lock:
                    if cid == self._conn_id:
                        self.idx = m.get("idx")
                        self.total = m.get("total")
            # 【v0.76】影子引擎的两类消息：只喂本地重算，不往浏览器推
            #   （浏览器不认识它们，推过去只是白占 SSE 带宽）。
            if isinstance(m, dict):
                _t = m.get("type")
                if _t == "rep_head":
                    with self._lock:
                        if cid != self._conn_id:
                            continue
                    self.mirror.head(m.get("data") or {})
                    continue
                if _t == "rep_rows":
                    with self._lock:
                        if cid != self._conn_id:
                            continue
                    self.mirror.rows(m.get("rows") or [])
                    continue
                if _t == "end":
                    # 先把最后一批答案算完、战报补齐，再让前端显示结束面板
                    self.mirror.finish()
            self.bus.push(m)
        # 收尾：只有"当前这条连接"的自然断开才通知前端；
        # 手动断开 / 重连产生的旧线程保持安静，免得界面被过期事件打回去。
        with self._lock:
            mine = (cid == self._conn_id)
            if mine:
                self._sock = None
        if mine:
            self.bus.push({"type": "conn", "connected": False,
                           "text": "与服务端的连接已断开"})

    # ---------- 浏览器 → 服务端 ----------
    def send_name(self, name):
        """【v0.35】把玩家名报给服务端（报到消息，不是决策）。
        ★ 没连接就当没发生：名字纯显示用，不该因为"还没连上"报错打扰玩家。"""
        with self._lock:
            sock = self._sock
        if sock is None:
            return False, "尚未连接服务器"
        try:
            netproto.send_msg(sock, {"type": "name", "value": name})
        except OSError as e:
            return False, "发送失败：%s" % e
        return True, None

    def send_answer(self, value):
        with self._lock:
            sock = self._sock
        if sock is None:
            return False, "尚未连接服务器"
        try:
            netproto.send_msg(sock, {"type": "answer", "value": value})
        except OSError as e:
            return False, "发送失败：%s" % e
        return True, None

    def _mirror_broken(self, err):
        """【v0.77】本地算崩 → 自愈：关掉「服务器友好」，让服务端恢复发战报。

        ★ 只降一次：反复发 prefs 没有意义，而且会把界面刷满。
        """
        if self._mirror_broken_once:
            return
        self._mirror_broken_once = True
        self.mirror.push_events = False
        self.send_prefs(False, self.prefs["record"])

    def send_prefs(self, friendly, record):
        """【v0.76】把玩家选项报给服务端。中途改也生效（下一批答案起按新设置走）。

        ★ 没连接时也把值记下来：连上那一刻会自动补报（见 connect）。
          这样"先勾好再连接"和"连上再勾"两种顺序都对。
        """
        friendly, record = bool(friendly), bool(record)
        # 这条连接上已经因为"本地算不出来"降过级 → 不再接受重新打开
        #   （否则前端一刷新就又打开，界面接着空白）
        if self._mirror_broken_once:
            friendly = False
        self.prefs["friendly"], self.prefs["record"] = friendly, record
        self.mirror.save_file = record
        # 友好 = 服务端只发答案，战报和场面都靠本地算 → 本地算完要推
        self.mirror.push_events = friendly
        with self._lock:
            sock = self._sock
        if sock is None:
            return True, None        # 记下了，连上再报
        try:
            netproto.send_msg(sock, {"type": "prefs",
                                     "friendly": friendly, "record": record})
        except OSError as e:
            return False, "发送失败：%s" % e
        return True, None


# ======================================================================
#  「本机开一局」：后台起一个 netserver，再连上去
# ======================================================================
def _free_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def start_local_game(bridge, total=2, bots=1, start_seeds=0, rules=None,
                     session=None):
    """在本机起一个 netserver（默认：你 vs 电脑），然后作为客户端连上去。

    这是以前 webui.py 的行为，但现在走的是一条【真实客户端】的路 ——
    服务端是真的 netserver.py 进程，客户端是真的联网协议。
    想联机（朋友也进来）时别用这个按钮，直接跑 netserver.py 再让大家填你的 IP。

    【v0.22】两个参数上的讲究：
      · start_seeds 是 4 号位置参数（0 = 空手起手，旧行为不变）。
      · 加 --no-console：本机局已经有这个网页界面了，不该再占一个
        8090 端口起房主控制台 —— 那只会多一个空窗口让人困惑。
    """
    script = os.path.join(BASE_DIR, "netserver.py")
    if not os.path.isfile(script):
        return False, "找不到 netserver.py"
    # 【v0.22】参数先折好、先校验，再动手杀旧局。
    #   ★ 顺序很要紧：kill_local_game() 会把正在跑的那一局连根拔掉。
    #     要是把它放在校验前面，房主手滑填错一个数字，当前这局就被顺手杀了 ——
    #     报的却是"人数至少 2 人"，谁也想不到联系。
    #   为什么不能指望 netserver 那边报错：它是另一个进程，参数不对会直接退出，
    #   这边只会看到"连不上 127.0.0.1:xxxxx" —— 那句错误对玩家毫无帮助。
    total = _as_int(total, 2)
    bots = _as_int(bots, 1)
    start_seeds = max(0, _as_int(start_seeds, 0))
    if total < 2:
        return False, "人数至少 2 人"
    if bots < 0 or bots > total:
        return False, "电脑数要在 0 ~ 人数 之间"
    kill_local_game()
    port = _free_port()
    rule_args = _rule_args(rules)      # 【v0.25】房间规则 → --rule 键=值
    try:
        # 【v0.63】加 --keep：本机服务端打完一局【不退出】，
        #   客户端点「再来一局」重连同一个端口就是下一局（座位名/战绩都还在）。
        proc = subprocess.Popen([sys.executable or "python", script,
                                 str(total), str(bots), str(port),
                                 str(start_seeds), "--no-console", "--keep"]
                                + rule_args,
                                cwd=BASE_DIR)
    except OSError as e:
        return False, "启动本机服务端失败：%s" % e
    LOCAL["proc"] = proc
    extra = (" " + " ".join(rule_args)) if rule_args else ""
    print("本机服务端已启动：netserver.py %d %d %d %d --no-console%s（pid=%d，端口 %d）"
          % (total, bots, port, start_seeds, extra, proc.pid, port))
    ok, err = bridge.connect("127.0.0.1", port, retry=8.0)
    if not ok:
        kill_local_game()
        return False, err
    # 记下来，供「再来一局」复用（成功之后才记，免得记下一个连不上的参数）
    # 【v0.74.1】session 可由调用方传入（试玩台一个进程 N 个座位，各记各的）。
    ses = SESSION if session is None else session
    ses.update({"mode": "local", "host": "127.0.0.1", "port": port,
                "total": total, "bots": bots, "start_seeds": start_seeds,
                "rules": dict(rules) if isinstance(rules, dict) else {}})
    return True, None


def rematch(bridge, session=None):
    """【v0.15】再来一局 —— 按上一局的连法重开。

    · 本机局（双击 网页版.bat 走的那条）：杀掉旧的 netserver，用同样的
      参数起一个新的再连上。完全可行。
    · 远程局：直接重连同一个 host:port。注意 netserver.py 是【打完一局就退出】的，
      所以能不能重开，取决于房主有没有重新运行 netserver.py —— 连不上就如实报错，
      不要假装成功让玩家干等。
    · 【v0.74.1】试玩台局（mode="lab"）：服务端是试玩台进程里的 Room，同样直接重连；
      真连不上就明确告诉玩家回试玩台点「一键开局」。

    ★★【v0.74.1】为什么加了 session 参数：以前只有一份模块级 SESSION，而试玩台是
      **一个进程里 N 个座位**，共用同一份 —— 名字会互相覆盖。更糟的是试玩台
      直接调 bridge.connect()，从来没写过 SESSION，于是每个座位的「再来一局」
      都只会得到"还没连过服务器"（用户 2026-11-04 报的就是这个）。
      现在每个座位带自己的一份 session（见 Handler.session_ref / Seat.session）。

    【待确认】真·联机重赛（服务端不退出、大家投个票接着下一局）需要新协议，
      见 开发文档.txt 第八章。
    """
    ses = SESSION if session is None else session
    mode = ses.get("mode")
    # 【v0.63】★★ 服务端现在打完一局【不退出】（Room 回到"等人"，监听口一直开着），
    #   所以"再来一局"= 直接重连同一个地址即可 —— 不再杀进程重起。
    #   用户 2026-10-23："再来一局时，需要重启服务器，这其实是很麻烦的，
    #   再来一局干脆就不要重启服务器，保留名字，座位等等一系列信息。"
    #   ★ 顺序很重要：先连；连不上才退回"重起一个本机服务端"的老办法
    #     （兼容房主手动关掉了服务端 / 用的是不支持常驻的旧版）。
    if mode in ("local", "remote", "lab"):
        host, port = ses.get("host"), ses.get("port")
        if not host or not port:
            return False, "不知道上一局连的是哪台服务器，请在下面手动填地址重连。"
        ok, err = bridge.connect(host, port, retry=2.5)
        if ok:
            # 【v0.63】重连之后把名字再报一次：服务端据此把座位复原成上一局的
            #   号位（不然"谁先点再来一局谁就是 1 号位"，名字会跟座位错位）。
            nm = ses.get("name")
            if nm:
                bridge.send_name(nm)
            return True, None
        if mode == "local":
            # 本机服务端真的没了（手动关了 / 崩了）→ 用同样的参数重起一个
            return start_local_game(bridge, ses.get("total", 2),
                                    ses.get("bots", 1),
                                    ses.get("start_seeds", 0),
                                    ses.get("rules", {}),
                                    session=ses)
        if mode == "lab":
            return False, ("试玩台的服务端已经关了 —— 回试玩台页面点「一键开局」重开，"
                           "那里能保留同样的座位配置。")
        return False, ("重连 %s:%d 失败 —— 服务端可能已经关掉了，"
                       "要让房主重新运行 netserver.py 才能再来一局。" % (host, port))
    return False, "还没连过服务器，先在下面开一局吧。"


def kill_local_game():
    proc = LOCAL.get("proc")
    LOCAL["proc"] = None
    if proc is None:
        return
    try:
        if proc.poll() is None:
            proc.terminate()
    except Exception:
        pass


atexit.register(kill_local_game)


# ======================================================================
#  HTTP 层
# ======================================================================
class Handler(BaseHTTPRequestHandler):
    bus = None
    bridge = None

    # ---------- 【v0.59】多开（playtest.py 试玩台）用的两个钉子 ----------
    #
    # 试玩台会为每个座位派生一个 Handler 子类（各自 bus / bridge / 端口），
    # 下面两个类属性就是给那种用法准备的 —— 单开（直接跑 webui.py）时都是默认值，
    # 行为与以前一模一样。
    #
    #   name_ref —— {"name": "真人1"} 这样的可变容器：座位页进房时用它预填名字。
    #     ★ 为什么是字典而不是字符串：名字要能跟着试玩台的配置改。
    #       存成类属性的话，改一次名字就得重建 HTTP 服务（端口跟着变，页面全废）。
    #   no_local —— 【v0.76】网页版已不再提供「本机开一局」的入口（被多开试玩台
    #     整个覆盖了）。这个钉子留着是为了试玩台的座位页：**接口还在**（自动化测试
    #     拿它当起局夹具），所以座位上仍然不能让人随手点开第二个服务端。
    name_ref = None
    no_local = False
    # 【v0.76】这个页面允不允许改「玩家选项」（服务器友好 / 自动录像）。
    #   普通单开 = True；试玩台的座位页 = False（N 个座位共用进程，存 N 份录像没意义）。
    allow_prefs = True
    # 【v0.74.1】session_ref —— 「上一局是怎么连的」这份记录的容器。
    #   None = 用模块级 SESSION（普通单开就是这样，行为与以前一模一样）。
    #   试玩台会为每座位塞一个自己的 dict：N 个座位共用一个进程，共用一份
    #   SESSION 的话 host/port 还好，但 `name` 会互相覆盖 → 再来一局时报错名字。
    session_ref = None
    # 【v0.76】undo_ref —— 「回退到本回合开始」的入口。
    #   None = 这个页面不支持回退（普通单开、本机局都走这条，行为与以前一致）；
    #   试玩台会塞一个 callable，直接调到进程内那个 Room 的 request_undo。
    #   ★ 为什么不做成"发个 HTTP 去问 netserver"：本机局的 netserver 是子进程，
    #     控制台端口是多少、开没开，webui 这边一概不知道。能直连的就直连。
    undo_ref = None
    # 【v0.76】replay_ref —— 「导出本局录像」，同样是个 callable（试玩台才有）。
    #   None = 这个页面不支持导出（普通单开），前端就不显示那个按钮。
    #   返回 replay_dict() 或 None。
    replay_ref = None

    def _session(self):
        ref = self.session_ref
        return ref if ref is not None else SESSION

    def _fixed_name(self):
        ref = self.name_ref
        return ref.get("name") if ref is not None else None

    def log_message(self, fmt, *args):
        pass              # 默认日志太吵，关掉

    def handle_one_request(self):
        """浏览器读完响应就把连接关了，Windows 下这次读取会抛
        ConnectionAbortedError（WinError 10053）。socketserver 会把它当成
        "处理请求时出错"，往控制台打一大段 traceback —— 看着像程序崩了，
        其实什么事都没有。这里直接吞掉：连接断掉本来就不需要报错。"""
        try:
            BaseHTTPRequestHandler.handle_one_request(self)
        except OSError:
            self.close_connection = True

    # ---------- GET ----------
    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            return self._file("index.html")
        if path == "/app.js":
            return self._file("app.js")
        if path == "/style.css":
            return self._file("style.css")
        # 【图鉴不在这个服务里】图鉴是**独立服务** codex_server.py（独立进程、独立
        #   端口、独立入口 图鉴.bat / 图鉴.sh）。这里一条路由都不留，理由：
        #   ① 依赖方向 —— 图鉴跟对局无关，不该要求"先起对局前端才能翻图鉴"；
        #   ② 生命周期 —— 对局前端随一局开关，图鉴是想翻就翻的读物；
        #   ③ 你 2026-11-13 原话："不要让它当玩家下面的子网页"。
        #   ★ 数据仍然同源：codex_server 调的也是 build_catalog()。
        if path == "/api/status":
            return self._json(self.bridge.status())
        if path == "/api/catalog":
            return self._json(build_catalog())
        if path == "/api/version":
            return self._json({"version": read_version(), "repo": GITEE_REPO})
        if path == "/api/update-check":
            return self._json(update_check())
        if path == "/api/names":
            # 【v0.35】名字缓存：GET 读（进房页自动带出）、POST 写（见 do_POST）
            # 【v0.59】试玩台的座位页由后台钉死名字（不读缓存 —— 好几个座位共用
            #   同一个 names.json，读出来全是同一个名字，等于没起名）。
            fixed = self._fixed_name()
            if fixed is not None:
                return self._json({"player": fixed, "recent": []})
            return self._json(read_names())
        if path == "/api/events":
            return self._sse()
        # 【v0.76】这两个都是【只读查询】，必须是 GET。
        # ★★ 上一版把它们写进了 do_POST，而前端用的是 fetch() 的默认 GET ——
        #    于是 /api/caps 永远 404，前端就认为"这个页面不支持"，两个按钮都不显示。
        #    （2026-11-15 查试玩台的问题时才发现：写路由时只看了"附近有个 /api/undo"，
        #      没确认它属于哪个 do_*。）
        if path == "/api/caps":
            return self._json({"undo": self.undo_ref is not None,
                               "replay": self.replay_ref is not None,
                               # 【v0.76】这个页面要不要显示「玩家选项」。
                               #   试玩台的座位页为 False —— 那 N 个座位共用一个进程，
                               #   每个都存一份录像会让 对局录像/ 里出现 N 份同一局。
                               "prefs": self.allow_prefs})
        # 导出本局录像（回放包）。对局中途也能导：导的是"到此刻为止"的输入序列。
        if path == "/api/replay":
            fn = self.replay_ref
            if fn is None:
                return self._json({"ok": False, "err": "这一局不支持导出录像"})
            try:
                data = fn()
            except Exception as e:      # noqa: BLE001
                return self._json({"ok": False, "err": "导出失败：%s" % e})
            if not data:
                return self._json({"ok": False, "err": "这一局还没开始"})
            return self._json({"ok": True, "data": data})
        self.send_error(404)

    # ---------- POST ----------
    def do_POST(self):
        path = self.path.split("?")[0]
        body = self._read_json()

        if path == "/api/connect":
            host = str(body.get("host") or "127.0.0.1").strip() or "127.0.0.1"
            try:
                port = int(body.get("port") or 8080)
            except (TypeError, ValueError):
                return self._json({"ok": False, "err": "端口必须是数字"})
            ok, err = self.bridge.connect(host, port)
            if ok:
                # 记下来给「再来一局」用（重连同一个地址）
                # 【v0.74.1】写到"本页那份 session"（试玩台每座位一份）
                self._session().update({"mode": "remote", "host": host, "port": port})
            return self._json({"ok": ok, "err": err})

        # 【v0.76】回退到本回合开始。★ 只有这个是 POST（它会真的改变对局）；
        #   /api/caps 与 /api/replay 是只读查询，在 do_GET 里。
        #   真正的动作在引擎【等答案】那一刻发生（那边抛 UndoRequest、由服务端重建），
        #   所以 ok=True 只代表"要求已记下"，前端要靠随后的 reset 消息确认。
        if path == "/api/undo":
            fn = self.undo_ref
            if fn is None:
                return self._json({"ok": False, "err": "这一局不支持回退"})
            # 【v0.76】指定回合：{round: N} → 退到第 N 回合开始；不带 = 本回合。
            at = body.get("round") if isinstance(body, dict) else None
            try:
                ok, err = fn(at)
            except TypeError:
                # 老的 undo_ref 只收 0 个参数 —— 别为了一个按钮把整个接口打崩
                ok, err = fn()
            return self._json({"ok": ok, "err": err})

        # 【v0.76】把本局录像【直接存进项目的 对局录像/】，返回文件名。
        #   ★ 为什么不做成浏览器下载：下载目录里堆一堆长名 json，想重放还得
        #     自己拼相对路径（用户原话："还要自己输文件名，默认文件名又很长"）。
        #     存进项目里之后，`python replay.py` 不带参数就放最新那份。
        if path == "/api/replay-save":
            fn = self.replay_ref
            if fn is None:
                return self._json({"ok": False, "err": "这一局不支持导出录像"})
            try:
                data = fn()
            except Exception as e:      # noqa: BLE001
                return self._json({"ok": False, "err": "导出失败：%s" % e})
            if not data:
                return self._json({"ok": False, "err": "这一局还没开始"})
            try:
                import replay as _replay
                nm = _replay.save_to_library(data)
            except Exception as e:      # noqa: BLE001
                return self._json({"ok": False, "err": "写文件失败：%s" % e})
            return self._json({"ok": True, "name": nm})

        if path == "/api/local":
            # 【v0.76】★ 网页版**已经没有这个入口了**（原来那个「本机开一局」按钮
            #   连同人数/电脑/种子/可选规则一起删掉）—— 开本机局请用「多开试玩台」，
            #   它能开 N 个真人座位，比这里强得多。
            #   接口本身保留：test_webui 的 Test12 拿它当"起一局"的夹具，
            #   删了整条用例就没法测「再来一局」了。玩家在界面上摸不到它。
            #
            # 【v0.59】试玩台的座位页仍然不许调它 —— 那一按会在试玩台进程里另起
            #   一个服务端，把其它座位晾在旧局里（见 Handler.no_local 的注释）。
            if self.no_local:
                return self._json({
                    "ok": False,
                    "err": "这是试玩台开出来的座位页 —— 要换参数请回试玩台页面重新开局"})
            # 【v0.22】「本机开一局」不再写死 2 人 / 1 电脑 / 0 种子：
            #   页面上填什么就开什么。缺项沿用旧默认（2 人局、1 电脑、空手起手），
            #   所以老前端（只发空 body）行为完全不变 —— test_webui 的 Test12 靠这条。
            ok, err = start_local_game(
                self.bridge,
                _as_int(body.get("total"), 2),
                _as_int(body.get("bots"), 1),
                _as_int(body.get("start_seeds"), 0),
                body.get("rules"),       # 【v0.25】房间规则（可缺省 → 全默认）
                session=self._session())  # 【v0.74.1】写本页那份 session
            return self._json({"ok": ok, "err": err,
                               "session": dict(self._session())})

        if path == "/api/update-install":
            # 【v0.69】一键更新：下载 → 校验 → 备份 → 覆盖（全在 updater.py 里）
            # ★ 只允许 POST（见 install_update 的注释）；人也不传参数进来 ——
            #   "更新到哪个版本"由服务端自己查，前端说了不算。
            return self._json(install_update())

        if path == "/api/rematch":
            ok, err = rematch(self.bridge, self._session())
            return self._json({"ok": ok, "err": err})

        if path == "/api/disconnect":
            self.bridge.disconnect()
            return self._json({"ok": True})

        if path == "/api/prefs":
            # 【v0.76】玩家选项：服务器友好 / 自动录像。
            #   没连上时也会把值记在 Bridge 里，连上的那一刻自动补报 ——
            #   所以"先勾好再连"和"连上再勾"两种顺序都对。
            f = body.get("friendly")
            r = body.get("record")
            ok, err = self.bridge.send_prefs(
                f if isinstance(f, bool) else False,
                r if isinstance(r, bool) else False)
            return self._json({"ok": ok, "err": err})

        if path == "/api/names":
            # 【v0.35】存名字 + 顺手报给服务端（如果已经连上）。
            #   两件事一起做：进房页点"连接"时，名字先落盘、再随连接报过去。
            # 【v0.59】试玩台多开时【不落盘】：几个座位共用一个 names.json，
            #   谁后写谁赢，缓存立刻变成一团乱账；座位名以试玩台配置为准，
            #   只更新内存里那个容器（见 Handler.name_ref）。
            name = str(body.get("name") or "").strip()
            ref = self.name_ref
            if ref is None:
                write_names(name)
                info = read_names()
            else:
                ref["name"] = name
                info = {"player": name, "recent": []}
            # 【v0.63】重赛时要拿它把座位复原（服务端按名字认人）
            # 【v0.74.1】写本页那份 session —— 试玩台多座位才不会互相盖名字
            self._session()["name"] = name
            self.bridge.send_name(name)
            return self._json({"ok": True, "names": info})

        if path == "/api/answer":
            ok, err = self.bridge.send_answer(body.get("value"))
            if ok:
                self.bus.clear_pending()
            return self._json({"ok": ok, "err": err})

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
        # 【v0.19】静态文件不许缓存 —— 本地桥是"即时改即时看"的开发形态，
        # 浏览器缓存住旧的 app.js 会让"改了没生效"变成一场深夜悬案。
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


# ======================================================================
#  启动
# ======================================================================
USAGE = """用法：
  python webui.py                        起客户端界面（页面上填服务器 IP 连接）
  python webui.py --server 1.2.3.4:8080  启动即连
  python webui.py --port 8100            本地页面端口（默认 8000）
  python webui.py --no-browser           不自动开浏览器"""


def parse_args(argv):
    opt = {"port": 8000, "server": None, "browser": True}
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
        elif a == "--server" and i + 1 < len(argv):
            i += 1
            opt["server"] = argv[i]
        elif a in ("-h", "--help"):
            print(USAGE)
            return None
        else:
            print("不认识的参数：%s\n%s" % (a, USAGE))
            return None
        i += 1
    return opt


def split_addr(s):
    if ":" in s:
        host, _, p = s.rpartition(":")
        try:
            return host or "127.0.0.1", int(p)
        except ValueError:
            return host or "127.0.0.1", 8080
    return (s or "127.0.0.1"), 8080


def pick_port(preferred):
    """本地页面端口：优先用 preferred，被占了就顺着往后找，免得报错劝退"""
    for p in range(preferred, preferred + 12):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.bind(("127.0.0.1", p))
        except OSError:
            s.close()
            continue
        s.close()
        if p != preferred:
            print("本地端口 %d 被占用，改用 %d" % (preferred, p))
        return p
    print("从 %d 起的 12 个端口都被占用了，请用 --port 指定别的端口。" % preferred)
    return None


def main():
    opt = parse_args(sys.argv[1:])
    if opt is None:
        return
    if not os.path.isfile(os.path.join(WEB_DIR, "index.html")):
        print("缺少前端文件：%s" % os.path.join(WEB_DIR, "index.html"))
        return

    port = pick_port(opt["port"])
    if port is None:
        return

    bus = Bus()
    bridge = Bridge(bus)
    Handler.bus = bus
    Handler.bridge = bridge

    try:
        server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    except OSError as e:
        print("本地页面端口 %d 起不来：%s" % (port, e))
        return

    url = "http://127.0.0.1:%d/" % port
    print("《植物斗争》网页版客户端已启动：%s" % url)
    print("  · 在页面上填服务器 IP 后点「连接」；或点「本机开一局（你 vs 电脑）」。")
    print("  · 本进程只负责界面与转发，规则和结算都在服务端（netserver.py / engine.py）。")
    print("  · 想看植物图鉴：另外跑 `python codex_server.py`（双击 图鉴.bat / sh 图鉴.sh），")
    print("    那是独立页面，不占这个端口，也不需要对局开着。")

    if opt["server"]:
        host, sport = split_addr(opt["server"])
        threading.Thread(target=lambda: bridge.connect(host, sport, retry=8.0),
                         daemon=True).start()
    if opt["browser"]:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已关闭。")
    finally:
        bridge.disconnect(notify=False)
        kill_local_game()


if __name__ == "__main__":
    main()
