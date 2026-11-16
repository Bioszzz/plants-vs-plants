# -*- coding: utf-8 -*-
"""replay.py —— 对局录像：导出、重放、校验。

为什么要有它：
    引擎里大量用 `id(植物对象)` 当字典键（absorb_target_plans 等十几处），
    所以**不能**靠"存一份局面快照、以后再还原"来复现 —— 反序列化后 id 全变，
    那些表会静默失配，不报错、只是行为悄悄变了。
    于是走另一条路：**记输入，重跑**。回合、场面、发牌全由引擎自己长出来。

回放包里有什么（`pvsp-replay/1`）：
    players / names / start_seeds / rules / rng_seed / duel_auto
        —— 重建这一局需要的全部参数（种子缺一个，重跑出来就是另一局）
    answers[]
        —— 按时间顺序，每次 gather 收到了什么：{r 回合, ph 阶段, k 题型,
           who 谁, v 答案}。v 里的 Player 已折成 {"p": 编号}

用法（在项目目录里直接跑，不用打路径）：
    python replay.py                    # 重放最新一局
    python replay.py --list             # 看看有哪些录像
    python replay.py 2248               # 名字里带 2248 的那份（片段匹配）
    python replay.py 对局录像/xxx.json   # 完整路径当然也行
    python replay.py --out 战报.txt      # 战报写文件（不刷屏）
    python replay.py --verify 原战报.txt # 重放并与原战报逐字比对
"""
import io
import json
import os
import random
import sys
import time

import engine
from engine import Game, Controller, PENDING, answer_in


class HybridController(Controller):
    """【v0.76】前半段吃录像、后半段问真人 —— 回退就靠它。

    为什么不是"重建一局、跑一半、再换控制器"：
      引擎的 run() 是一条道跑到黑的循环，没有"跑一半停下"的合法出口（唯一的
      打断点只有等答案那一刻）。与其在外面反复接管，不如换个思路 ——
      **让控制器自己决定这一题是背答案还是问人**：
        前 K 题 → 直接吐录像里的答案（瞬间过，等于快进）
        第 K+1 题起 → 原样转发给真实客户端
    于是整局仍然是"一次 run() 跑到底"，引擎一行都不用改。

    ★ 前提：录像那 K 条的 rng 种子必须和原来一样，否则快进出来的局面是另一局。
      调用方负责把同一个 seed 传给 Game。
    """

    def __init__(self, real, rows, players=None):
        self.real = real
        self.rows = list(rows)
        self.players = players
        self.i = 0
        self._ans = PENDING
        self._fast = False
        self.fed = 0

    # ---- 广播类：一律原样转发（战报照常发给玩家）----
    def notify(self, text):
        self.real.notify(text)

    def notify_state(self, text, state):
        self.real.notify_state(text, state)

    def wait_hint(self, text):
        self.real.wait_hint(text)

    # ---- 决策类 ----
    def dispatch(self, q):
        if self.i < len(self.rows):
            row = self.rows[self.i]
            self.i += 1
            self.fed += 1
            # 题型对不上说明录像和当前版本已经不同 —— 宁可报错，也别喂错答案
            if row.get("k") and q.kind and row["k"] != q.kind:
                raise ReplayError(
                    "回退失败：第 %d 条答案对不上（录像【%s】/ 当前【%s】）"
                    % (self.i, row["k"], q.kind))
            self._ans = answer_in(row.get("v"), self.players or [])
            self._fast = True
            return
        self._fast = False
        self.real.dispatch(q)

    def poll(self):
        if self._fast:
            v = self._ans
            self._ans = PENDING
            return v
        return self.real.poll()

    @property
    def done(self):
        return self.i >= len(self.rows)


class ReplayError(Exception):
    """录像和当前版本对不上（题型错位 / 参数缺失）。"""


class ReplayIncomplete(ReplayError):
    """录像只到一半就没了 —— 对局中途导出的那种。

    ★ 这【不是】错误：中途导出本来就是"重放到当时那个位置"。
      调用方应当把它当成"到此为止"处理，而不是报失败。
    """


class ReplayController(Controller):
    """按录像吐答案，不参与任何决策。

    ★ 每个控制器只拿"自己那个人"的答案序列，按顺序消费。
      题型对不上 = 立刻报错：这说明录像和当前版本的问题序列已经不同
      （通常是规则改过），继续跑下去只会得到一局"看着像、其实不对"的战报。
    """

    def __init__(self, who, rows, players):
        self.who = who
        self.rows = rows
        self.players = players
        self.i = 0
        self._ans = PENDING
        self.consumed = 0

    def notify(self, text):
        pass

    def dispatch(self, q):
        if self.i >= len(self.rows):
            raise ReplayIncomplete(
                "录像里 %d 号只答了 %d 题就结束了（引擎又问了一题：%s）"
                % (self.who, len(self.rows), q.kind))
        row = self.rows[self.i]
        self.i += 1
        self.consumed += 1
        if row.get("k") and q.kind and row["k"] != q.kind:
            raise ReplayError(
                "第 %d 题对不上：录像里 %d 号答的是【%s】，当前版本问的是【%s】。\n"
                "  这通常是规则/流程改过了 —— 用当时的版本才能重放这一局。"
                % (self.i, self.who, row["k"], q.kind))
        self._ans = answer_in(row.get("v"), self.players)

    def poll(self):
        v = self._ans
        self._ans = PENDING
        return v


def clean_playable(raw):
    """录像里的牌库 → 当前版本能用的那几株。

    ★ 为什么必须过滤：录像可能是在"带测试专用植物"的进程里打的（test_scripted
      会往 PLANTS 里加几株假植物）。直接拿去建局会 KeyError，而且报得莫名其妙。
      过一遍只留现存的名字，并打印一句提示 —— 不静默吞掉。
    """
    if not raw:
        return None
    from plants_data import PLANTS as _PLANTS
    keep = [x for x in raw if x in _PLANTS]
    missing = [x for x in raw if x not in _PLANTS]
    if missing:
        print("[提示] 录像里有当前版本不存在的植物，已忽略：%s"
              % "、".join(missing[:5]))
    return keep or None


def build(data, output=None, extra_delay=0.0):
    """照录像重建一局（还没跑）。返回 game。"""
    if not isinstance(data, dict):
        raise ReplayError("录像格式不对：顶层应是对象")
    fmt = data.get("format")
    if fmt != "pvsp-replay/1":
        raise ReplayError("不认识的录像格式：%r（本版只认 pvsp-replay/1）" % (fmt,))

    n = int(data.get("players") or 0)
    if n <= 0:
        raise ReplayError("录像里没有玩家数")

    rows = list(data.get("answers") or [])
    by_who = {}
    for row in rows:
        by_who.setdefault(int(row.get("who") or 0), []).append(row)

    seed = data.get("rng_seed")
    # ★ 没有种子就别装能重放：random.Random(None) 等于"另开一局"，
    #   跑几条答案之后就会题型错位，报出来的错误看着像"规则改过了"，其实是被坑了。
    if not isinstance(seed, int):
        raise ReplayError(
            "这份录像没记下随机种子（rng_seed=%r），重放不出同一局。\n"
            "  早于 v0.76 修复版导出的录像都有这个问题 —— 请用新版本重新导一份。"
            % (seed,))
    rng = random.Random(seed)

    ctrls = [ReplayController(i + 1, by_who.get(i + 1, []), None)
             for i in range(n)]
    # 牌库：录像里记的是"当时能出哪几株"。当前版本里没有的名字要剔掉
    #   （例如测试专用植物只在测试进程里存在）—— 否则 _q_plant 会 KeyError。
    playable = clean_playable(data.get("playable"))
    g = Game(ctrls,
             output=output,
             playable=playable,                          # 当时用的牌库
             duel_auto=bool(data.get("duel_auto", True)),
             rng=rng,
             start_seeds=data.get("start_seeds") or 0,
             rules=data.get("rules") or {},
             player_names=data.get("names") or [])
    # 控制器要拿到 g.players 才能把编号查回对象 —— 建完局再回填
    for c in ctrls:
        c.players = g.players
        c.rows = by_who.get(c.who, [])
    return g


def run_replay(data, output=None):
    """跑完一整局，返回 (game, 战报行列表)。

    录像只到一半（中途导出）时**不抛异常**：跑到没答案为止就停，
    并在 `game.replay_partial` 上留一句说明。这是正常用法，不是错误。
    """
    cap = []
    sink = output if output is not None else cap.append
    g = build(data, output=sink)
    g.replay_partial = None
    try:
        g.run()
    except ReplayIncomplete as e:
        g.replay_partial = str(e)
    return g, cap


def load(path):
    with io.open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save(data, path):
    with io.open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    return path


def truncate_record(game, target):
    """把一局的录像截成"第 target 回合开始之前"能用的续玩前缀。

    局终回退（打完一局再按回退）和"从录像续玩"用的是同一份东西：
    只保留 r < target 的答案，种子 / 规则 / 牌库原样带着。
    返回 dict；这局没开录制或缺种子时返回 None（调用方据此报错，不要硬跑）。
    """
    try:
        data = game.replay_dict()
    except Exception:            # noqa: BLE001 —— 导出失败不该把回退搞崩
        return None
    if not data or not isinstance(data.get("rng_seed"), int):
        return None
    rows = [dict(r) for r in (data.get("answers") or []) if r.get("r", 0) < target]
    data = dict(data)
    data["answers"] = rows
    data["final"] = {"round": target, "winner": None, "winners": []}
    return data


def save_to_library(data):
    """把一份录像存进 `对局录像/`，返回文件名（不含目录）。

    ★ 为什么不用浏览器下载：玩家的下载目录里堆一堆
      `对局录像_20261115_2248.json`，想重放还得自己拼相对路径。
      直接落到项目的 对局录像/ 里，`python replay.py` 不带参数就能放最新那份。
    """
    if not os.path.isdir(REPLAY_DIR):
        os.makedirs(REPLAY_DIR)
    n = int(data.get("players") or 0)
    base = "%s_%d人局" % (time.strftime("%Y%m%d_%H%M%S"), n)
    path = os.path.join(REPLAY_DIR, base + ".json")
    k = 2
    while os.path.exists(path):
        path = os.path.join(REPLAY_DIR, "%s-%d.json" % (base, k))
        k += 1
    save(data, path)
    return os.path.basename(path)


# ----------------------------------------------------------
# 命令行
# ----------------------------------------------------------
USAGE = __doc__.split("用法：", 1)[1] if "用法：" in __doc__ else ""

REPLAY_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "对局录像")


def find_replays():
    """列出可重放的录像：[(mtime, 文件名, 完整路径)]，新的在前。

    「进行中.json」也算 —— 它是自动保存的当前进度，照样能重放到断点。
    """
    out = []
    for d in (REPLAY_DIR, os.path.dirname(os.path.abspath(__file__))):
        if not os.path.isdir(d):
            continue
        for n in os.listdir(d):
            if not n.lower().endswith(".json"):
                continue
            p = os.path.join(d, n)
            if not os.path.isfile(p):
                continue
            if d != REPLAY_DIR and not n.startswith("对局录像"):
                continue        # 项目根目录只认自己导出来的那些
            if any(p == q[2] for q in out):
                continue
            out.append((os.path.getmtime(p), n, p))
    out.sort(reverse=True)
    return out


def resolve_path(arg):
    """把"一句人话"变成录像文件路径。

    依次尝试：原样路径 → 加 .json → 在 对局录像/ 里找 → 按片段模糊匹配（新的优先）。
    返回 (路径, None) 或 (None, 错误说明)。
    """
    if os.path.isfile(arg):
        return os.path.abspath(arg), None
    if os.path.isfile(arg + ".json"):
        return os.path.abspath(arg + ".json"), None
    cand = os.path.join(REPLAY_DIR, arg)
    if os.path.isfile(cand):
        return cand, None
    if os.path.isfile(cand + ".json"):
        return cand + ".json", None
    hits = [x for x in find_replays() if arg in x[1]]
    if len(hits) == 1:
        return hits[0][2], None
    if hits:
        return hits[0][2], "匹配到 %d 个，用了最新的：%s" % (len(hits), hits[0][1])
    return None, "找不到录像：%s" % arg


def print_list():
    items = find_replays()
    if not items:
        print("对局录像/ 里还没有录像（打一局就会自动存一份）")
        return
    print("对局录像/（新的在前，共 %d 份）：" % len(items))
    for i, (mt, name, path) in enumerate(items, 1):
        size = os.path.getsize(path)
        print("  %2d. %-38s %6.1f KB  %s"
              % (i, name, size / 1024.0,
                 time.strftime("%m-%d %H:%M", time.localtime(mt))))


def main(argv):
    if argv and argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    if argv and argv[0] in ("-l", "--list"):
        print_list()
        return 0
    if argv and argv[0] == "--export-help":
        print(
            "导出录像的三种方式：\n"
            "  1. 网页右下角「⭳ 录像」按钮（对局中/结束后都能点）——\n"
            "     直接存进项目的 对局录像/ 文件夹，不用去下载目录翻\n"
            "  2. 房主控制台同样的按钮\n"
            "  3. 命令行局加 --record 录像.json\n"
            "导出的文件就是本脚本能吃的那种，发给别人也能重放。")
        return 0

    path = None
    out = None
    verify = None
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--out" and i + 1 < len(argv):
            out = argv[i + 1]; i += 2
        elif a == "--verify" and i + 1 < len(argv):
            verify = argv[i + 1]; i += 2
        elif path is None and not a.startswith("-"):
            path = a; i += 1
        else:
            print("不认识的参数：%s（--help 看用法）" % a)
            return 2

    # 不带参数 = 重放最新一局（「进行中.json」除外，它多半还没打完）
    if path is None:
        items = find_replays()
        live = [x for x in items if x[1] == "进行中.json"]
        done = [x for x in items if x[1] != "进行中.json"]
        pick = done[0] if done else (live[0] if live else None)
        if pick is None:
            print("对局录像/ 里还没有录像。先打一局，或 python replay.py --list 看看。")
            return 1
        path = pick[2]
        print("（没指定文件，重放最新的：%s）\n" % pick[1])
    else:
        path, note = resolve_path(path)
        if path is None:
            print(note)
            print("试试：python replay.py --list")
            return 2
        if note:
            print("（%s）\n" % note)

    data = load(path)
    try:
        g, lines = run_replay(data)
    except ReplayError as e:
        print("[重放失败] %s" % e)
        return 1

    text = "\n".join(str(x) for x in lines)
    if out:
        io.open(out, "w", encoding="utf-8", newline="\n").write(text + "\n")
        print("战报已写到 %s（%d 行）" % (out, len(lines)))
    else:
        print(text)

    if getattr(g, "replay_partial", None):
        print("\n—— 重放到此结束（%d 回合，%d 行战报）——" % (g.round, len(lines)))
        print("   录像只到一半：%s" % g.replay_partial)
        print("   这是中途导出的正常结果，不是错误。")
        return 0

    print("\n—— 重放结束：%d 回合，%d 行战报 ——" % (g.round, len(lines)))

    if verify:
        want = io.open(verify, "r", encoding="utf-8").read().strip()
        got = text.strip()
        if want == got:
            print("[校验通过] 与原战报逐字一致")
            return 0
        wa, wb = want.splitlines(), got.splitlines()
        for k, (x, y) in enumerate(zip(wa, wb), 1):
            if x != y:
                print("[校验失败] 第 %d 行开始不同：" % k)
                print("  原：%s" % x[:160])
                print("  重放：%s" % y[:160])
                break
        else:
            print("[校验失败] 行数不同：原 %d 行 / 重放 %d 行" % (len(wa), len(wb)))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
