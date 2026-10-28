# -*- coding: utf-8 -*-
"""
engine.py —— 《植物斗争》游戏引擎（多人局，纯逻辑，与界面解耦）

回合四阶段（对应 游戏规则总结.txt）：
  phase_prep   回合开始前：清场 + 预留判定位
  phase_choose 出牌阶段：全员【同时】暗选植物 → 收齐后公示 → 校验代价（不合法变"空"）→ 支付
  phase_bluff  博弈阶段：公示场面 → 释放 / 充能 / 选目标 / 【v0.27】溅射声明，
               各步【同时问、收满为止】
  phase_settle 结算阶段（不向任何人提问）：
              ⓪-a【v0.56】[毁灭]（黑洞大王花）→ ⓪-b【v0.55】刀叶类（消除 / 废除）→
               ① 复活/加血（161）→ ② 治疗 →
               ③ 番茄连接落地 → ④ 勾人/湮灭 →
               ⑤ 噬魂 → ⑥ 有收益批 → ⑦ 普通命中
               → ⑧ 火种爆发 → ⑨ 人参果爆发 → ⑩ 猫尾草 → ⑪ [免疫]
               【v0.28】每一步都"算完当场落地"（_flush_damage）——
               v0.9~v0.27 那个 ③ 统一扣血已经取消；⑦ 的先后也换成了序列优先级。
               阶段末尾给环相关机制（溅射之类）留了位置。

v0.5 改动：决策采集从"逐个轮询"改成"同时下发、收满为止"（见 Game.gather）。
  旧写法：for 循环里挨个 controller.choose_xxx() —— 问到 player2 之前必须等 player1 答完，
          网络版里就是"必须干等前面的人操作完才轮到自己"，体验很差。
  新写法：先把所有问题一次性发出去，再轮询收答案，谁先答谁先入袋，总耗时 = 最慢的那个人。

引擎天生按 N 人设计：目标可从其余所有存活玩家里选，全体攻击打其余所有人，
最后存活者获胜。样品阶段的临时解释都标了【待确认】。
"""

import random
import time

from plants_data import (PLANTS, EMPTY, PLAYABLE, ATTR_NONE,
                         FIRE_BURNS, FIRE_BURST_AT, FIRE_BURST_BAO, WATER_FUELS,
                         TAG_BURST_AT, TAG_KEEP,
                         CORNERSTONE, DEFAULT_RULES, normalize_rules,
                         describe_rules, LOAD_MULT, LOAD_FISHY,
                        # 【v0.27】溅射 + 杨桃类（判据与数值都留在 plants_data）
                        # 【v0.41】再加 is_reward_class（"有收益批"的准入 = 得种 ∪ 击杀回血）
                        SPLASH_BAO, SPLASH_DAMAGE, is_yangtao_class, is_reward_class,
                        # 【v0.32】刀类：判据 / 数值常量
                        # 【v0.36】新增 is_bladeleaf_class（连切只给刀叶类）
                        is_knife_class, is_bladeleaf_class, is_jingang, is_durian,
                        CUT_SELF_WOUND_AT, CUT_SELF_WOUND,
                        # 【v0.38】电系：[闪电链] 数值 + [不灭]/[反馈湮灭] 判据
                        FLASH_CHAIN_BAO, FLASH_CHAIN_DAMAGE,
                        is_immortal, has_feedback)

MAX_ROUNDS = 200        # 安全阀：防止双方互不进攻导致死循环【样品保护措施】
POLL_INTERVAL = 0.02    # 收答案的轮询间隔（秒），只影响"同时问"时的收齐节奏


class _Pending:
    """poll() 返回的哨兵：答案还没到。用 is 比较，不要用 ==。"""
    __slots__ = ()

    def __repr__(self):
        return "PENDING"


PENDING = _Pending()


class _TagMark:
    """【v0.16】人参果的一个"段标记"：段 n 被埋下之后，跨回合活着的累积记录。

    为什么必须挂在 Game 上：FieldPlant 每个回合开始都会重置（见它的注释），
    而"累积到 3 才爆发"天然要跨回合活着 —— 挂 FieldPlant 等于每回合丢一次。
    """

    __slots__ = ("seg", "count", "owners", "born_round", "burst_round",
                 "reach_round")

    def __init__(self, seg, round_no):
        self.seg = seg               # 标记的编号段 = 人参果的充能数
        self.count = 0               # 累积计数【不清零】：爆过之后继续往上加
        self.owners = set()          # 本局内为这一段放过人参果的人（挨爆发时按半条命算）
        self.born_round = born = round_no  # 首个放出人参果的回合
        self.burst_round = None      # 首次爆发的回合（爆发后 TAG_KEEP 个回合移除记录）
        # 【v0.63】计数【首次达到阈值】的那一回合 —— 该回合同样豁免。
        #   ★ 用户 2026-10-23 试玩报的 bug："人参果，埋雷提前 —— 我这回合刚叠满
        #     就扣血了"。原来只豁免 born_round（埋雷那一回合），于是"雷是上回合
        #     埋的、这一回合才把计数顶到 3"这种情况【当回合就炸】了。
        #   现在多记一个 reach_round：不管雷是什么时候埋的，
        #   **计数刚跨过阈值的那一回合永远不炸，从下一回合起才炸**。
        #   这才是"当回合叠满不扣血 / 自下局起爆发"的字面口径。
        self.reach_round = None


class Question:
    """一次决策请求（v0.5 引入，用来支持"同时问、收满为止"）。

    kind       —— 客户端据此渲染界面：plant / release / charge / target
                  （v0.8 新增 targets：散连击的多选目标，答案是编号列表）
    player     —— 问的是谁（Player 对象）
    payload    —— 渲染需要的原始数据（catalog / cands / own …），会原样发成 JSON
    refs       —— 只留在服务端的 Python 对象（如候选 Player 列表），不发给客户端
    validate   —— 答案合法性校验；网络版收到非法答案会原问题重发
    """

    # ★★【v0.50】每种决策【必须】带上网的 payload 键 —— 机器检查。
    #
    #   事故来源（用户 2026-10-19 实测截图）：v0.48 的 seed_heir 只写了
    #     Question(..., refs={"cands": cands})
    #   refs 是【本地控制器】用的 Python 对象表，msg() 根本不发它。
    #   于是 web / netclient / gui 三端拿到的 cands 全是 undefined：
    #   界面上一个输入框都不渲染 → 合计永远 0/total → 确定按钮永远灰 →
    #   玩家被卡死在瓜分题上。而脚本测试全绿 —— 因为它们走的是 refs 那条路，
    #   永远看不见 payload 缺了什么。
    #   ★ 这类事故【日志里一个错都没有】，只能在构造那一刻当场喊出来。
    #   新增 kind：先往这张表登记「界面要读哪些键」，否则别处写漏了没人拦。
    PAYLOAD_CONTRACT = {
        "plant": ("own", "catalog"),
        "release": ("plant",),
        "charge": ("own",),
        "water_hp": ("plant", "own"),
        "water_def": ("own",),
        "target": ("cands", "own"),
        # 【v0.72】targets / burst_split 也要求 own —— 界面要写
        # "这是【谁的】哪株植物在做什么"，缺了它玩家只能看到一个裸的"选择攻击目标"。
        "targets": ("cands", "count", "own"),
        "burst_split": ("cands", "shots", "plant", "own"),
        "splash": ("sides", "target"),
        "absorb": ("cands",),
        # 【v0.72】flash 也加 own —— 勾取者的条件方案要在题面上写清楚
        "flash": ("sides", "target", "own"),
        "cut_target": ("cands",),
        "cut_mode": ("target", "same"),
        "cut_half": ("targets",),
        "cut_help": ("cands",),
        "cat_split": ("left", "right", "shots"),
        "friend": ("cands", "gates"),
        "seed_heir": ("cands", "defaults", "total"),
        # 【v0.51】161 生命之花的二选一（加血 / 复活）——
        #   界面要念"sell 什么植物、加多少血、有谁可救"，三样缺一不可。
        "revive": ("plant", "heal", "cands"),
    }

    def __init__(self, kind, player, game, validate=None, refs=None, **payload):
        need = self.PAYLOAD_CONTRACT.get(kind)
        if need:
            missing = [k for k in need if k not in payload]
            if missing:
                raise AssertionError(
                    "决策 %s 的 payload 缺 %s —— 这些键是【发给界面】的，"
                    "只写进 refs 不算数（refs 只喂本地控制器，msg() 不会发它）；"
                    "缺了它界面就渲染成一片空白，玩家会被卡死在这道题上。"
                    % (kind, "、".join(missing)))
        self.kind = kind
        self.player = player
        self.player_idx = player.idx
        self.game = game
        self.payload = payload
        self.refs = refs or {}
        self.validate = validate or (lambda v: True)

    def msg(self):
        """转成发给客户端的 ask 消息（协议层直接用，见 netproto）"""
        m = {"type": "ask", "kind": self.kind}
        m.update(self.payload)
        return m


class Controller:
    """界面适配器接口。引擎只通过这几个方法和玩家交互。

    决策采集分两步（v0.5 起）：
      dispatch(q) —— 下发问题。【必须非阻塞】，网络版把问题发出去就立刻返回，
                     否则"同时问"就退化成"逐个问"了。
      poll()      —— 非阻塞取答案。没到返回 PENDING；连接断开抛 ConnectionError。

    不想自己实现这套异步协议的控制器（热座 / 电脑 / 脚本测试）直接继承 SyncController，
    继续写老的 choose_plant / choose_release / choose_charge / choose_target 即可。
    """

    def notify(self, text):                 # 引擎广播一条信息（网络版逐个投递）
        raise NotImplementedError

    def notify_state(self, text, state):
        """【v0.13】广播"当前场面"：text 给人读，state 是结构化场面（给图形界面）。

        默认退化成纯文本 —— 热座 / 电脑 / 测试脚本都不用改。
        网络版覆写它，好让浏览器客户端拿到结构化数据直接渲染卡片，
        而不用去解析"—— 当前场面 ——"这段中文。"""
        self.notify(text)

    def wait_hint(self, text):
        """本轮没有你的决策要做 —— 网络客户端用它把界面切成"等待他人"。默认忽略。"""
        pass

    def dispatch(self, q):                  # -> None（非阻塞）
        raise NotImplementedError

    def poll(self):                         # -> value | PENDING
        raise NotImplementedError


class Observer:
    """旁观者接口：只收广播（战报 + 场面），【从不】参与决策。

    【v0.22】房主控制台就是它 —— 房主自己不下场打，只想在浏览器里看全场。

    为什么【不】复用 Controller：Controller 是"要被问的人"（dispatch / poll），
    而旁观者永远不该出现在决策名单里。两者混在一个列表里，迟早有人把一个
    观察者塞进 Game.controllers，引擎就会一本正经地问他"出什么牌"，
    然后把整局卡死在等一个永远不会来的答案上 —— 而且不报错，只有卡死。
    所以 Game 里 observers 是单独一列，只有 say / say_state 会走到这里。
    """

    def notify(self, text):
        pass

    def notify_state(self, text, state):
        pass


class SyncController(Controller):
    """同步控制器适配层：把老的 choose_* 写法包装成 dispatch / poll。

    dispatch 只暂存问题，poll 时才真正去问——所以热座版仍然是一个人一个人来
    （一台电脑一个键盘，本来也只能这样），而网络版不受影响。
    """

    def dispatch(self, q):
        self._q = q

    def poll(self):
        q = getattr(self, "_q", None)
        if q is None:
            raise RuntimeError("poll() 被调用，但没有待答的问题")
        self._q = None
        if q.kind == "plant":
            return self.choose_plant(q.game, q.player)
        if q.kind == "release":
            return self.choose_release(q.game, q.player)
        if q.kind == "charge":
            return self.choose_charge(q.game, q.player)
        if q.kind == "water_hp":
            return bool(self.choose_water_hp(q.game, q.player))
        if q.kind == "water_def":
            return self.choose_water_def(q.game, q.player, q.player.water)
        if q.kind == "target":
            # 统一返回玩家编号，与网络客户端回传的值保持一致。
            # 【v0.9】连击是逐发分配的，把"第几发/共几发"透传给界面，
            # 否则玩家连着被问 5 次"选谁"却不知道自己在选第几发。
            shot = q.payload.get("shot", 0)
            if shot:
                r = self.choose_target(q.game, q.player, q.refs["cands"],
                                       shot, q.payload.get("shots", 0))
            else:
                r = self.choose_target(q.game, q.player, q.refs["cands"])
            return r.idx
        if q.kind == "targets":
            # 【v0.8】散连击：返回编号列表
            picked = self.choose_targets(q.game, q.player, q.refs["cands"],
                                         q.payload["count"])
            return [t.idx for t in picked]
        if q.kind == "burst_split":
            # 【v0.44】连击发数分配：一次性把 N 发分给各目标 → 返回 [[编号, 发数], ...]
            pairs = self.choose_burst_split(q.game, q.player, q.refs["cands"],
                                            q.payload["shots"])
            return [[t.idx, c] for t, c in pairs]
        if q.kind == "splash":
            # 【v0.27】溅射声明：返回玩家编号列表（可以空 = 都不溅）
            picked = self.choose_splash(q.game, q.player, q.refs["cands"],
                                        q.payload.get("target"))
            return [t.idx for t in picked]
        # ---- 【v0.32】刀叶类的三个口 ----
        if q.kind == "cut_target":
            # 刀叶类：选要消除谁 → 返回玩家编号
            r = self.choose_cut_target(q.game, q.player, q.refs["cands"])
            return r.idx if r is not None else None
        if q.kind == "cut_mode":
            # 连切：切单株 / 全切同名卡 → 返回 "single" / "all"
            return self.choose_cut_mode(q.game, q.player,
                                        q.refs["target"], q.refs["same"])
        if q.kind == "cut_half":
            # 切一半 / 全切 → 返回 bool
            return bool(self.choose_cut_half(q.game, q.player, q.refs["targets"]))
        if q.kind == "cut_help":
            # 【v0.32】切一半 → "帮谁切"（多选，含自己；0~N）→ 返回玩家编号列表
            picked = self.choose_cut_help(q.game, q.player, q.refs["cands"])
            return [x.idx for x in picked]
        # ---- 【v0.38】电系：勾人 / 闪电链 ----
        if q.kind == "absorb":
            picked = self.choose_absorb(q.game, q.player, q.refs["cands"])
            return [x.idx for x in picked]
        if q.kind == "flash":
            r = self.choose_flash(q.game, q.player, q.refs["cands"],
                                  q.payload.get("target"))
            return r.idx if r is not None else None
        # ---- 【v0.39】猫尾草：连击分配左右两侧 ----
        if q.kind == "cat_split":
            left, right = q.refs["sides"]
            n = q.payload["shots"]
            return self.choose_cat_split(q.game, q.player, left, right, n)
        # ---- 【v0.45】队友连接（107 番茄）----
        if q.kind == "friend":
            # 返回 (目标 Player|None, 门 Player|None) ——
            # ★ 内部一律用 Player 对象（界面好排版），出口这里折成编号
            #   （与网络客户端回传的值同形）。
            # ★★【v0.61】这里【不再有"要不要触发反馈"】—— 反馈是强制的，
            #   条件（连接会让仅剩两队并成一队）由结算期自己判，见
            #   _friend_resolve_phase。玩家只回答"连谁 / 偏移到哪株门"。
            t, g = self.choose_friend(
                q.game, q.player, q.refs["cands"], q.refs["gates"],
                q.payload["allow_empty"])
            return {"target": t.idx if t is not None else None,
                    "gate": g.idx if g is not None else None}
        # ---- 【v0.48】死亡时的种子瓜分（★ 唯一会问【已出局者】的题）----
        if q.kind == "seed_heir":
            pairs = self.choose_seed_heir(
                q.game, q.player, q.refs["cands"],
                q.payload["total"], q.payload["defaults"])
            return [[r.idx, c] for r, c in pairs]
        # ---- 【v0.51】161 生命之花：加血 / 复活 二选一 ----
        if q.kind == "revive":
            # 返回 ("heal", None) 或 ("revive", Player)
            #   ★ 内部用 Player 对象（界面好排版），出口折成编号/字符串
            #     （与网络客户端回传的值同形：{"mode":..., "target": idx}）。
            mode, t = self.choose_revive(q.game, q.player, q.refs["cands"])
            return {"mode": "revive" if (mode == "revive" and t is not None)
                                  else "heal",
                    "target": t.idx if (mode == "revive" and t is not None) else None}
        raise ValueError("未知的决策类型：%s" % q.kind)

    # ---- 子类按需实现；用不到的分支给了安全默认值 ----
    def choose_plant(self, game, player):
        raise NotImplementedError

    def choose_release(self, game, player):
        return False

    def choose_target(self, game, player, candidates, shot=0, shots=0):
        """选 1 个目标。shot/shots 是连击的"第几发/共几发"，非连击时都是 0。"""
        return candidates[0]

    def choose_targets(self, game, player, candidates, count):
        """【v0.8】散连击：挑 count 个【不同】的目标。默认取前 count 个。"""
        return candidates[:count]

    def choose_burst_split(self, game, player, candidates, shots):
        """【v0.44】连击发数分配：返回 [(目标, 发数), ...]，合计必须 = shots。
        默认把全部发数压给第一个目标（与老 choose_target 的默认 candidates[0] 一致）。"""
        return [(candidates[0], shots)]

    def choose_charge(self, game, player):
        return 0

    def choose_water_hp(self, game, player):
        """【v0.10】要不要用 1 个水滴抵掉【小喷菇/大喷菇】的半条命代价。
        默认不抵 —— 水滴留着加防御更稳。"""
        return False

    def choose_water_def(self, game, player, water):
        """【v0.10】花几个水滴加防御（1:1）。默认一个都不花。"""
        return 0

    def choose_splash(self, game, player, candidates, target=None):
        """【v0.27】溅射声明：返回【要溅射的那几个玩家】（0~2 个，空表 = 都不溅）。

        target 是这一发的目标（溅射是"目标的两侧"，界面上要显示"对谁"）。

        ★ 默认【两侧都溅】：这一条与其他 choose_* 的保守默认值不同 ——
          溅射是白捡的收益，不实现就按全开走。这样老控制器（没升级的界面、
          临时脚本）不会因为 v0.27 这条新规则直接抛 NotImplementedError。
        """
        return list(candidates)

    # ---- 【v0.32】刀叶类的三个决策口 ----
    def choose_cut_target(self, game, player, candidates):
        """刀叶类：选【要消除谁】。默认取第一个候选（与 choose_target 同款兜底）。"""
        return candidates[0] if candidates else None

    def choose_cut_mode(self, game, player, target, same):
        """连切：同名卡 ≥2 株 → 选 "single"（只切这一株）或 "all"（全切同名卡）。
        默认 "single" —— 保守：不替玩家扩大打击面。"""
        return "single"

    def choose_cut_half(self, game, player, targets):
        """切一半 / 全切：目标里有丝瓜/鱼腥草这类 → 要不要"切一半"。
        默认 False = 全切（照原样消除）。"""
        return False

    def choose_cut_help(self, game, player, candidates):
        """【v0.32】切一半 → "帮谁切"（从候选里多选，含自己；0~N 人）。
        默认【空表】= 谁都不帮（不额外豁免）—— 保守：不替玩家扩大豁免面。"""
        return []

    # ---- 【v0.38】电系的四个决策口 ----
    def choose_absorb(self, game, player, candidates):
        """勾人：选【要勾走谁】（多选；0~N 人）。
        默认【空表】= 不勾任何人 —— 保守：不替玩家做"抢别人植物"这种强决策。"""
        return []

    def choose_flash(self, game, player, candidates, target=None):
        """[闪电一击] 声明：闪电链向【两侧之一】弹出。
        默认【不弹】—— 保守：不替玩家扩大打击面（链会连带伤及第三方）。"""
        return None

    def choose_cat_split(self, game, player, left, right, shots):
        """【v0.39】猫尾草：把 shots 发分配到左右两侧 → 返回 [左发数, 右发数]。
        默认【全给左侧】—— 无脑可用（左 = 环上前一个邻居），保证不缺答案。"""
        return [shots, 0]

    # ---- 【v0.45】队友连接（107 番茄）----
    def choose_friend(self, game, player, candidates, gates, allow_empty):
        """选"连谁"。返回 (目标 Player|None, 门 Player|None)。

        · candidates  —— 可连的其余存活者（Player 列表；不含自己）
        · gates       —— 场上生效中的【猪笼草】持有者（多株时由玩家择一）
        · allow_empty —— 能不能选"空目标"（场上有猪笼草时为 False）

        ★★【v0.61】[反馈逻辑] 不在答案里 —— 它是**强制**的（用户 2026-10-23）：
          只要"连接会让场上仅剩的两个分图并成一个"，结算期就必定触发，
          玩家没有"我不触发"这个选项。所以这个口子只问"连谁"。

        默认【保守】：能不连人就不连（只吃那 9 点辅助防，不替玩家背"共同胜利"
        这种大承诺）；被猪笼草逼着连时就连门；门都没有又没空目标可选才挑第一个。
        """
        if gates:
            return gates[0], gates[0]
        if allow_empty or not candidates:
            return None, None
        return candidates[0], None

    def choose_seed_heir(self, game, player, candidates, total, defaults):
        """【v0.48】死亡时瓜分种子：返回 [(接收者 Player, 颗数), ...]，合计 = total。

        · candidates —— 所有可接种子的人（活着的队友，死于噬魂时还会含噬魂者）
        · total      —— 死者留下的种子总数（答案的合计必须等于它）
        · defaults   —— 引擎已经算好的平均方案 `[{"idx": 编号, "n": 颗数}, ...]`

        ★ 默认【原样接受平均方案】—— 死亡提问是为了"可以改"，不是为了逼人表态；
          没升级的界面 / 电脑 / 脚本一律走这条默认值。
        """
        d = {x["idx"]: x["n"] for x in defaults}
        return [(c, d.get(c.idx, 0)) for c in candidates]

    # ---- 【v0.51】161 生命之花：加血 / 复活 二选一 ----
    def choose_revive(self, game, player, candidates):
        """〔161 生命之花〕释放时的二选一。返回 ("heal", None) 或 ("revive", Player)。

        · candidates —— 可复活的死人（**已经过滤掉本回合刚死的**，见
          Game._revivable；空表根本不会问，引擎直接按加血走）

        ★ 默认【加血】：复活一个队友会把他整条链拉回场上、直接影响胜负，
          替玩家做这种决定太越权；加血是"稳赚"的那一头。
        """
        return ("heal", None)


class FieldPlant:
    """某玩家本回合在场上的植物。每个回合开始都会重置。
    【待确认】植物是每回合重置，还是跨回合留存（源代码每回合重新读模板，暂按重置）。
    """
    def __init__(self, player, spec):
        self.player = player
        self.spec = spec
        self.fang = spec.defense.fang  # 当前防御值（会被攻击削减；充能植物被充能改写）
        # 【v0.30】当前暴击（削防）值 —— 与 fang 对称。默认取自植物自己的 bao，
        #   充能植物在充能阶段【直接改写它】（见 phase ②）。
        #   以前用 hit_count + bao_override 做"只有第一击才改写"的特判，用户 2026-09-24
        #   指出那套太绕："充能就是提高暴击和防御值，你充能完直接修改数值不就完了？
        #   干嘛要特判" —— 现在改成直接改 bao，resolve_hit 里再没有任何充能专属分支。
        self.bao = spec.attack.bao
        self.broken = False            # 本回合是否已被打穿【v0.26 语义变更】：
        #   它现在【只表示"这株已经破了"这个状态】（战报/界面用它显示"已被击破"），
        #   【不再】用来拦后续攻击 —— 破掉的植物本回合不再挡，多出来的子弹照打人。
        #   详见 resolve_hit 与规则文档 5.5 ①。
        # 【v0.21 建，v0.45 泛化】这一株身上的【辅助防 P₁】。
        #   原稿 p29 ①：`全防值 P ＝ 基础防 P₀ ＋ 辅助防 P₁ ＋ 充能防 P₂`；
        #   p30 词条：水滴"即时针对某一攻击充作 [辅助防 P₁]"（P₁＝P₁+n）。
        #   ★ v0.21 起它只记"水滴糊上去的那一块"，所以那时叫 fang_water；
        #     v0.45 引入队友链后，107 番茄的 +9 也是【辅助防】，
        #     于是泛化成 fang_aux（P₁）——名字改了，语义是"一切外加的防"。
        #   ★★ 为什么必须单独记一笔：噬魂类【无视全部辅助防】（原稿 p22 页标题
        #     "植之噬魂（无视[水滴]）"，用户 2026-10-15："噬魂类本质上是无视
        #     所有辅助防，不止水滴"）→ 判"暴 > 有效防御"时要把 P₁ 从 fang 里扣掉。
        #     不单独记账就永远分不出"哪些防是植物自己的、哪些是外加的"。
        self.fang_aux = 0
        # 【v0.16】人参果：充能数 = 它标记的段（tag_seg）。单独存一个字段，
        #   【不能】拿 fang 代替 —— fang 会被攻击逐次削掉（resolve_hit），
        #   而"我标记的是哪一段"从充能那一刻起就不该再变。
        self.tag_seg = None
        # 【v0.32】本回合充能消耗的种子数（充能植物）。单独记一笔是为了
        #   "充能植物前缀可变"那个房间选项 —— 前段 = cost_seed + charge_spent。
        #   ⚠️ 不能拿 fang 代替：fang 会被攻击削掉，而"充了多少"从充能那刻起不变。
        self.charge_spent = 0
        # 【v0.32】刀叶类状态：
        #   cut_gone —— 已被"全切"成 [空]（防御/暴击归零、失去一切效果，
        #                但【人还在】，不是杀死）。
        #   cut_half_done —— 本回合被"切一半"了（防御还在，但它的负面效果
        #                不附加给刀叶持有者与其指定目标）。
        #   locked —— 进入【互挡】后丧失 [废除] 能力（不能再切别人）。
        #   cuttable —— 进入互挡后变成"可被废除的目标"（非互挡默认切不动）。
        self.cut_gone = False
        self.cut_half_done = False
        self.locked = False
        self.cuttable = False
        # 【v0.38】电系「勾走」状态：
        #   absorbed —— 本回合被"勾走"了（归勾人者支配；原主已失去决策权）。
        #               被勾走的目标【成[空]】（fang/bao 归零、inert），
        #               但【人还在】（不是杀死）。
        #   absorbed_by —— 勾人者（Player），None = 没被勾。
        #   absorb_threshold —— 本株被勾后，充能时要走的"阈值分段"参数
        #               （从勾人者 57 号那里借来用的；见 _apply_charge）。
        self.absorbed = False
        self.absorbed_by = None
        self.absorb_threshold = None
        # 【v0.57】形态 + 勾取：
        #   form —— ""（不适用）/ "hook"（103 牛角菱选了勾取形态）。
        #     勾取形态【不攻击】，所以博弈阶段那条"声明攻击目标"的循环直接跳过它
        #     （见 phase_bluff 里 `p.field.form == "hook"` 那一行）。
        #   hooked_by —— 被谁勾走（Player）；与 absorbed_by 分开记：
        #     absorbed 是 57 的"勾走"（成[空]），hooked 是 103 的"勾取"
        #     （照常结算，只是支配权归勾取者）。
        self.form = ""
        self.hooked_by = None
        # 【v0.72】hollow = 被[勾取]成[空]（原稿 p09"被夺走目标成[空]"）。
        #   ★ 与 inert 的关系：涉及"成[空] 还出不出手"的三条口径打架
        #     （p30 的"0防0暴" / 3928 行"彻底作废不出手" / 16.12"被勾者照常结算"），
        #     等用户拍板；现状 = 清防御、照常结算、不设 inert（Test110 钉住这条）。
        self.hollow = False
        # 【v0.38】[闪电一击] 的"本回合临时态"：57 聚电蓝莓充能到第二段才带闪击，
        #   而 spec.attack.flash 是【共享模板】上的固定值（不能改它）——
        #   所以"本回合到底带不带闪击"读 field.flash_chain
        #   （默认 = spec 上写死的值；充能植物可被改写）。
        self.flash_chain = bool(getattr(spec.attack, "flash", False))
        # 【v0.38】[反馈湮灭]：本回合是否已因湮灭成[空]（防止同一株被重复处理）。
        self.annihilated = False
        # 【v0.38】[不灭] 植物：本回合的"释放"是强制的（原稿：使用者自己也不能
        #   决定不释放）。记一笔用于战报；判定口是 is_immortal(spec)。
        self.forced_release = False
        # 【v0.7】inert = 本回合选择"不释放"：所有属性归零（防御0、免疫与标签失效、
        # 攻击/治疗/充能均不发动），但出牌时付的种子照扣、不退还。
        self.inert = False

    def stand_down(self):
        """不释放：属性全部归零（种子已在出牌时扣掉，这里不退还）。"""
        self.fang = 0
        self.fang_aux = 0     # 【v0.21/v0.45】辅助防那部分同样归零（它挂在"这一株"身上）
        self.bao = 0          # 【v0.30】暴击同样归零（与 fang 对称）
        self.inert = True

    def to_empty(self):
        """【v0.32】被"切"成 [空]：防御、暴击全归零、一切效果失效，但人还在。

        与 stand_down() 的区别：stand_down 是"玩家自己选择不释放"，
        这里是【被别人消除】—— 种子不退（根本没有退还这回事），
        且它是【不可逆的本回合状态】（本轮内不再恢复）。
        """
        self.fang = 0
        self.fang_aux = 0
        self.bao = 0
        self.cut_gone = True
        self.inert = True


class Player:
    def __init__(self, idx, name):
        self.idx = idx          # 从 1 开始，展示用
        self.name = name
        # 【v0.35】这个名字是不是【玩家自己起的】（True）/ 系统默认的 playerN（False）。
        #   ★ 为什么要有这个标记：默认名本身就是 "player1"（自带编号），
        #     再叠一个"(1号)"就成了 "player1(1号)"，又长又蠢。
        #     所以 tag 只在【自定义名】时才补编号 —— 见 tag 属性。
        self.custom_name = False
        self.hp = 2             # 半命单位：2 = 1 条命，1 = 半条命【已确认】
        self.seeds = 0
        self.field = None       # FieldPlant
        # 【v0.10】水滴 / 火种：挂在【玩家】身上的持久资源，跨回合累积（不像植物每回合重置）
        self.water = 0
        self.fire = 0
        # 【v0.18】超防：还剩几个回合的【绝对免伤】。0 = 没有。
        #   为什么必须挂玩家：植物每回合重置（FieldPlant 注释写死了），
        #   而超防要活"本回合 + 下回合"，挂植物上第一个回合就丢了。
        #   —— 与水滴/火种同一类东西：玩家身上的持久状态。
        #   回合开始 -1（phase_prep），所以拿到手时 = 2 → 覆盖本回合与下回合。
        self.super_def = 0
        # 【v0.25】消耗加成（"永久负面"）：鱼腥味 / 倍耗 —— 挂玩家、跨回合留存，
        #   与水滴/火种/超防是同一类东西（植物每回合重置，debuff 不能挂植物上）。
        #   ★ 为什么分 active / _next 两段：原稿给鱼腥草标了"（下局有效）"，
        #     用户 2026-09-21 确认口径 = 【下一回合起生效】—— 本回合的种子在
        #     出牌时就付掉了，改不了账，只能从下一回合起加重消耗。
        #     引擎在【回合最初】把 _next 并进 active（见 phase_prep），
        #     于是"第 R 回合附加 → 第 R+1 回合起生效"这条规则落在两个字段上。
        # ★★【v0.45「丝瓜万岁」】层数改成【按来源记账】：{来源玩家idx: 层数}。
        #   规则（用户 2026-10-15）："每个[倍耗]效果指明来源对象（哪怕超过六倍也记录），
        #   当丝瓜释放者和他人成为队友后，如果他人身上存在来源于丝瓜释放者的
        #   倍耗效果，则可以消除。[鱼腥味]同理"。
        #   → 于是不能再只存一个总数：必须知道"这 2 层是谁给的"，
        #     才能在某天两人结成队友时把【那几层】单独摘掉（见 _sweep_ally_loads）。
        #   ★ 对外仍然可以用 .fishy / .mult / .fishy_next / .mult_next 读总数
        #     （下面四个只读 property）—— 老代码（引擎/界面/测试）读法一字未改。
        self.fishy_by = {}      # 生效中的【鱼腥味】：{来源idx: 层数}
        self.fishy_next_by = {} # 本回合刚被附加、下回合才生效的
        self.mult_by = {}       # 生效中的【倍耗】：{来源idx: 层数}
        self.mult_next_by = {}  # 本回合刚被附加、下回合才生效的
        # ---- 【v0.51】复活（161 生命之花）----
        # death_snapshot —— "死掉的那一刻"【固化】下来的三样东西：
        #     {"round": 死在第几回合, "seeds": 死那一刻的种子数,
        #      "hp_start": 那一回合【开局】时他有多少血（半命单位）}
        #   ★ None = 活着（或从没死过）。复活时读它、复活后清回 None ——
        #     于是"复活后再次死亡"会自然拍出一份【新的】快照，不用另写分支。
        #   ★ 为什么种子要单独固化：用户 2026-10-19
        #     "就是死亡时的种子，这里种子不守恒。瓜分/噬魂之类的不影响死亡时的
        #      种子，人在死掉的那一刻种子固化"。
        #     所以它必须在 [瓜分] 把种子转走【之前】、噬魂夺种【之前】拍下来。
        self.death_snapshot = None
        # round_start_hp —— 本回合【开局】的血量（phase_prep 里刷）。
        #   只有"生命之神"那条可选规则读它（复活者拿回他死那回合开局的血量）。
        #   ★ 不能拿 p.hp 代替：结算里已经被治疗/伤害改过了。
        self.round_start_hp = self.hp
        # ---- 【v0.54】[免疫]（84 免疫菇）----
        # 还剩几个【回合末】要清账。0 = 没有免疫。
        #   原稿 p21：`84 免疫菇　回　当局＋下两局获[免疫]；＋0.5 血　0　8`
        #   词条：「本身和[队友]身上的 [定居][鱼腥味][倍耗][血梦]… 消除」
        #   ★★ 用户 2026-10-21 更正时点："应该是笔误，你改成'回合最末'吧。
        #      其实你看原稿结算顺序里面免疫也是放在最后"（p44 进行次序表末行）。
        #   ★ 与超防同一类：挂【玩家】身上（植物每回合重置），重复获得 = 刷新不叠加。
        #   ★ 本版只清 [倍耗] / [鱼腥味]（用户 2026-10-21 定的范围）。
        self.immune_turns = 0

    # ---- 【v0.45】加成层数的"总数"读法（真正存放处是上面那四本账）----
    @property
    def fishy(self):
        return sum(self.fishy_by.values())

    @property
    def fishy_next(self):
        return sum(self.fishy_next_by.values())

    @property
    def mult(self):
        return sum(self.mult_by.values())

    @property
    def mult_next(self):
        return sum(self.mult_next_by.values())

    # ★ 下面四个 setter 是【兼容口】：老代码/测试/工具会直接写
    #   `p.mult = 3`（那时它还是普通字段）。现在归到"无来源"那一格（src=0）——
    #   总数读法不变，而"丝瓜万岁"只认【真实玩家 idx】的来源，不会误伤这一格。
    @fishy.setter
    def fishy(self, n):
        self.fishy_by = {0: n} if n else {}

    @fishy_next.setter
    def fishy_next(self, n):
        self.fishy_next_by = {0: n} if n else {}

    @mult.setter
    def mult(self, n):
        self.mult_by = {0: n} if n else {}

    @mult_next.setter
    def mult_next(self, n):
        self.mult_next_by = {0: n} if n else {}

    @property
    def alive(self):
        return self.hp > 0

    @property
    def tag(self):
        """【v0.35】战报 / 界面里用来指认这个人的显示串。

        你 2026-09-30 定："后面日志显示名字+编号就行"，格式选的是"小明(2号)"。
        ★★【v0.36 更正】用户："所有取到名字的地方都换成名字+编号，方便读啊，战报要改，
          干嘛不改 —— 全部都改"。→ **不再区分自定义名/默认名**，一律补编号：
          默认名也变成 "player1(1号)"。理由：编号是给人认人的，有没有起名都该有。
          ⚠️ 与 v0.35 的"默认名一字不差"相反，回归测试里依赖默认名文案的断言要跟着改。
        """
        return "%s(%d号)" % (self.name, self.idx)

    def hp_text(self):
        if self.hp % 2 == 0:
            return str(self.hp // 2)
        return "%g" % (self.hp / 2.0)


class Ring:
    """【v0.28】存活环 —— 一圈还活着的人，实时掉人。

    用户 2026-09-22 原话："存活环是挺重要的东西，你最好代码里就有这个东西。
    它甚至是实时的，也就是说，如果有人生命归 0 它会被移除存活环。"

    ★ 为什么不能像 v0.27 那样"每次现算一遍 [p for p in players if p.alive]"：
      现算出来的只是个名单，算不出"这一刻圈上还有谁"这个【状态】。
      变成一等对象之后，才有地方挂"谁什么时候被摘出去"，
      也才谈得上"环相关的机制必须最后结算"（它们看到的是圈的最新样子）。
    ★ 环的顺序 = 玩家的 idx 顺序 = 座位顺序，与开局几人无关。
    ★ 与"出局 ≠ 断线"是两件事：出局的人【连接一动不动】（见 Player.alive 与
      RemoteController._dead），只是不再算环上的邻居、也不再出手。
    ★ 摘人只发生在【伤害真正落地】的那一刻（Game._flush_damage），
      别在别处顺手改 members —— 多个落点必然漏。
    """

    def __init__(self, players):
        self.members = [p for p in players if p.alive]

    def __len__(self):
        return len(self.members)

    def __iter__(self):
        return iter(self.members)

    def __contains__(self, p):
        return p in self.members

    def remove(self, p):
        """生命归 0 → 摘出去、圈合拢。返回 True = 这次真的摘掉了人。"""
        if p in self.members:
            self.members.remove(p)
            return True
        return False

    def neighbors(self, target, exclude=None):
        """环上 index 相邻的两人（"两侧"）。

        ★ exclude = 【释放方自己】：用户原话"溅射目标不包括自己"——
          即你打的人恰好坐在你旁边时，那一下不会溅回自己身上。
        ★ 不足 3 人 → 两侧会重合或为空 → 返回空表 = 环相关的效果【不触发】
          （用户 2026-09-21 拍板"没有两侧就不触发"）。
          3 人局里若释放方自己占掉一侧，剩下一侧照样算。
        ★ 返回顺序固定为 [前一个, 后一个]，测试才可复现、界面才好摆。
        """
        if target not in self.members or len(self.members) < 3:
            return []
        i = self.members.index(target)
        out = []
        for j in (i - 1, i + 1):
            q = self.members[j % len(self.members)]
            if q is target or q is exclude or q in out:
                continue
            out.append(q)
        return out

    def text(self):
        return " → ".join(p.tag for p in self.members)


class Teams:
    """【v0.45】队友链 —— 一个【并查集】（union-find），管"谁和谁是一队"。

    用户 2026-10-15 的原话：
      · "[队友链] 本质上是一个并查集，是一个图"；
      · "当场上所有人都属于同一个并查集时，该方所有人获胜"；
      · "为了看得方便，任意选取一个人作为代表元，把图转化为'星形图'"。

    ★ 为什么单独成一个类（与 Ring 同一个理由）：它是"谁和谁连着"这个【状态】，
      不是每次现算的名单 —— 而且它【跨回合活着】（存活环每回合由 phase_prep
      重建，队友链不重建）。
    ★ 代表元 = 每棵树的根 idx；union 时【小 idx 当根】，于是"星形图"的中心
      可复现，战报与测试都稳定。
    ★★ 初始状态 = 人各一队（各自成组）—— 没有谁天生是队友。
    """

    def __init__(self, players):
        self._parent = {p.idx: p.idx for p in players}

    def find(self, i):
        """找根 + 路径压缩。"""
        root = i
        while self._parent.get(root, root) != root:
            root = self._parent[root]
        while self._parent.get(i, i) != root:
            nxt = self._parent[i]
            self._parent[i] = root
            i = nxt
        return root

    def union(self, a, b):
        """把两个人并进同一队。返回 True = 这一下【真的】合并了两支不同的队伍。"""
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return False
        if rb < ra:                 # 小 idx 当根 → 代表元稳定
            ra, rb = rb, ra
        self._parent[rb] = ra
        return True

    def same(self, a, b):
        return self.find(a) == self.find(b)

    def groups(self, idxs):
        """把给定 idx 列表按所属树分组 → {根idx: [idx...]}（组内按 idx 升序）。"""
        out = {}
        for i in sorted(idxs):
            out.setdefault(self.find(i), []).append(i)
        return out


class Game:
    def __init__(self, controllers, playable=None, output=None, duel_auto=True,
                 rng=None, observers=None, start_seeds=0, rules=None,
                 player_names=None):
        self.controllers = list(controllers)
        # 【v0.35】玩家名（进房时自己起的）：按 idx 顺序给；缺的一律退回 playerN。
        #   ★ 名字只是【显示名】，引擎里认人一律用 idx —— 两个人都叫"小明"
        #     也照样各打各的（见规则文档第三章 2b）。所以这里【不查重】。
        names = list(player_names) if player_names else []
        self.players = [Player(i + 1, self._name_for(i, names))
                        for i in range(len(self.controllers))]
        # 【v0.35】给"自己起了名"的人打上标记（tag 才会补编号）。
        for i, p in enumerate(self.players):
            if i < len(names) and str(names[i] or "").strip():
                p.custom_name = True
        # 【v0.22】旁观者（房主控制台）：只收 say / say_state 的广播，不进决策。
        #   为什么【必须】和 controllers 分开存，见 Observer 的注释 ——
        #   controllers 是"要被问的人"，混进去会让引擎去问一个不会答的人。
        self.observers = list(observers) if observers else []
        self.playable = list(playable) if playable else PLAYABLE
        self._output = output  # 广播输出函数；热座版传 print，网络版留空用 notify 逐个投递
        # 【v0.11】单挑模式开关：存活人数 == 2 时略过"确认释放"和"指定目标"两环节。
        #   你的原话："两人单挑时一般不涉及多人博弈"。默认开启；
        #   传 False 恢复完整流程（回归测试用它对照旧行为）。
        #   注意判定是【运行时】的：3 人局打到只剩 2 人也会自动进入单挑模式。
        self.duel_auto = duel_auto
        # 【v0.21】随机源：只用在"同一个噬魂植物抢同一个目标"时的先后顺序上。
        #   为什么做成 Game 的成员而不是直接 random.shuffle：测试要可复现 ——
        #   传一个 random.Random(0) 进来，随机顺序就固定了（见 Test56）。
        self.rng = rng if rng is not None else random.Random()
        self.round = 0
        # 【v0.22】开局种子：全场统一发牌（房主在控制台上定的那个数）。
        #   为什么放在这里（建完 Player、任何阶段之前）：这是"发牌"，不是回合行为 ——
        #   与 phase_choose 里出牌扣种是两码事，别混在一起。
        #   默认 0 = 大家空手起手（旧行为完全不变）；
        #   只夹下界（负数没意义），【上不封顶】—— v0.19 定下的规矩：
        #   数值一律 Python int，不设人为上限。
        #   容错：接口层可能递来 "5" 这种字符串，甚至脏数据；脏数据一律当 0，
        #   绝不让一个填错的输入框把整局开局搞崩。
        try:
            self.start_seeds = max(0, int(start_seeds or 0))
        except (TypeError, ValueError):
            self.start_seeds = 0
        if self.start_seeds:
            for p in self.players:
                p.seeds = self.start_seeds
        # 【v0.25】房间规则集（"像狼人杀那样，每套房间一套规则"）。
        #   ★ 引擎【只读 self.rules】，绝不把某个开关写死在结算里 ——
        #     否则以后每加一个房间选项就要改一次引擎。规则名与默认值都登记在
        #     plants_data.DEFAULT_RULES；这里只负责把脏数据折成合法值。
        #   归一化在 normalize_rules 里做（负数夹 0、未知键忽略、脏数据退回默认）。
        self.rules = normalize_rules(rules)
        self.over = False
        self.winner = None
        # 【v0.45】获胜者名单（"同一连通分图 → 该方【全体】获胜"）。
        #   ★ winner 保留为"单人获胜时的那一人"（老代码/老客户端照旧读它）；
        #     多人共同获胜时 winners 才有意义 —— 战报与 end 事件都发 winners。
        self.winners = []
        self.plans = {}      # player -> 目标列表（连击/散连击展开成多元素），或 "ALL"
        # 【v0.27】溅射声明表：(玩家编号, 第几发) -> [要溅射的玩家]。
        #   为什么键里要带"第几发"：连击/全体攻击有多个目标，而"两侧"是【目标】的
        #   两侧 —— 同一个人的第 1 发与第 2 发两侧可能不同，所以声明必须逐发记账。
        #   没声明的（= 不溅射）就不出现在表里。
        self.splash_plans = {}
        self.released = set()  # 本回合释放了主动效果（攻击/治疗/充能）的玩家
        # 【v0.9 起，v0.28 改了用途】伤害登记表：player -> 待扣的半命数。
        #   v0.9~v0.27：resolve_hit 一路累加，到结算末尾的 ③ 才【统一扣血】。
        #   v0.28 起：一株植物打完就 _flush_damage() 兑现一次 ——
        #     所以它现在的寿命只有"一株植物的一次攻击"这么长，
        #     不再横跨整个结算阶段。【详见 phase_settle 的说明】
        self._pending_dmg = {}
        # 【v0.37】静默缓冲：None = 正常播报；list = 播报先攒进这里（见 say）。
        #   用途 = 杨桃类连击的日志压缩（把 N 发合并成一行）。
        self._quiet = None
        # 【v0.28】存活环：一圈还活着的玩家，生命归 0 当场摘人（见 Ring）。
        #   建在这里（发牌之后、任何阶段之前）：环的初始成员就是全体玩家。
        self.ring = Ring(self.players)
        # 【v0.45】队友链（并查集）：谁跟谁是"队友（同一连通分图）"。
        #   ★ 与存活环分开：环每回合重建、链【跨回合留存】（一队就是一辈子）。
        #   ★ 初始 = 人各一队；连接的唯一入口是 Game.connect（见那里的说明）。
        self.teams = Teams(self.players)
        # 【v0.48】本回合"死于噬魂"的人：生命归 0 那一刻要区分"噬魂致死"与
        #   "普通致死"——只有前者才多出一个"噬魂者"分池（见 seed_pools）。
        #   ★ 判据在 _devour_hit 里落：这一口的伤害足以打空 → 才记。
        #   ★ 每回合在 phase_prep 清空（它不是"计划表"，不进 PLAN_TABLES）。
        self.death_by_devour = set()
        # 【v0.16】人参果的段标记表：段 n -> _TagMark。
        #   挂 Game（不是 Player、也不是 FieldPlant）—— 规则 10.3 定的是
        #   【全局一份】计数，同段多株人参果共用，不叠加。
        self.tag_marks = {}
        # 【v0.32】刀叶类的本回合计划表：player -> {"targets": [target...],
        #   "mode": "single"/"all", "half": bool}。
        #   ★ 与 self.plans（攻击目标）分开存 —— 刀叶不攻击，走的是"消除"这条路，
        #     两者混在一个表里迟早有人把刀叶当成攻击者（比如 _attack_order 会捞到它）。
        self.cut_plans = {}
        # ★ 2026-10-25 拆步后新增的四本"场末计划表"（全部进了 PLAN_TABLES）：
        #   water_hp_plans   —— 声明期"要不要用 1 水滴抵半条命"（效果在【加血】落地）
        #   water_def_plans  —— 声明期"花几个水滴加固"（效果在【辅助防】落地）
        #   friend_assist_plans —— 番茄的【最终目标】（+9 在【辅助防】落地）
        #   cut_exempt_plans —— 刀叶"切一半"时选的人（【附加层】施加时跳过他们）
        self.revive_mode_plans = {}
        self.water_hp_plans = {}
        self.water_def_plans = {}
        self.friend_assist_plans = {}
        self.cut_exempt_plans = {}
        # 【v0.71】见 PLAN_TABLES 里的 cond_plans（勾取者的条件方案）
        self.cond_plans = {}
        self.absorb_target_plans = {}
        # 【v0.71】_distribute_absorbed 的播报去重（同回合可能被调两次：
        #   原主人声明后一次、勾取兑现后覆盖再一次，不该把"随机分配"念两遍）
        self._dist_logged = set()
        # 【v0.45】队友连接的声明表：player -> {"target": Player|None,
        #   "gate": Player|None, "name": str}。
        #   ★ target=None 表示"空目标"（不连任何人、也不加任何辅助防）；
        #   ★ gate = 猪笼草生效时番茄选中的那一株（多株才需要选）；
        #   ★ 【v0.61】没有 feedback 字段 —— 反馈是强制的，结算期自己判。
        self.friend_plans = {}
        # 【v0.32】互挡配对表：player_idx -> player_idx（双向都记）。
        #   一对进入互挡的刀叶，双方 locked（丧失废除能力）且 cuttable（可被切）。
        self.cut_blocks = {}
        # 【v0.32】小刀叶"连出三次自扣血"的计数：player_idx -> 本局累计出过几次。
        #   ★ 按【玩家】记，不按植物 —— 规则 15.8："同一个玩家本局内累计打过三次"。
        self.knife_played = {}
        # 【v0.38】电系「勾走」计划表：吸收者(57 的持有者) -> [被勾的 FieldPlant...]。
        #   ★ 为什么是 field 而不是 player：勾的是【那一株植物】，不是那个人 ——
        #     同一个玩家本回合场上只有一株（本项目一回合出一株），但语义上
        #     记 field 更准（将来若一回合多株也无需改）。
        self.absorb_plans = {}
        # 【v0.57】[勾取]（103 牛角菱）：勾取者(Player) -> {
        #     "fields": [被勾的 FieldPlant...]（顺序 = 声明顺序，第 0 株默认先出手）
        #     "act":    真正出手几株（默认 1；「牛头无敌」= 2）
        #     "done":   已兑现的玩家编号集合（每个结算阶段只兑现一次）
        #   }
        #   ★★ 与 absorb_plans 的区别：勾取【不是独立阶段】—— 它在每个结算阶段
        #     开始时兑现一次（_hook_pass(stage)），因为"被勾的植物必须也能结算"。
        self.hook_plans = {}
        # 【v0.38】电系「闪电链」声明表：(玩家编号, 第几发) -> 选中的那一侧 Player。
        #   照 v0.27 splash_plans 的老规矩：键里带"第几发"（连击每一发的两侧可能不同）。
        #   ★ 原稿：链是"向其两边目标【之一】"弹出 —— 所以是【单选一侧】，不是两侧。
        self.flash_plans = {}
        # 【v0.72】被[聚电]收走那株的链：由支配者答，兑现时才生效
        self.absorb_flash_plans = {}

    # ---------- 工具 ----------
    @staticmethod
    def _name_for(i, names):
        """【v0.35】第 i 号玩家（0 基）该叫什么。

        给了非空名字就用它，否则退回默认的 playerN。
        ★ 这里【只做兜底、不做校验】：名字是显示名，重复/含空格/很长都不影响
          判定（引擎认人用 idx）—— 只是在空的时候别显示成空白。
        """
        if i < len(names):
            n = str(names[i] or "").strip()
            if n:
                return n
        return "player%d" % (i + 1)

    def say(self, text):
        # 【v0.37】★★ 静默开关：`_quiet` 非 None 时，播报【先攒着不发】。
        #   用途 = 杨桃类连击日志压缩（用户 2026-09-30："杨桃这类连接植物的日志
        #   太长太冗余，压缩到打了具体某个人几发就可以"）。
        #   为什么收在 say()：战报出口【只有这一个】—— 想在别处"少写两行"，
        #   迟早会漏（同 #87 的坑）。这里一刀切，逐发日志自然全进缓冲。
        #   ★ 缓冲里的内容可以是【丢弃】（逐发细节被汇总替代）或【刷出】
        #     （_flush_quiet 时按需 dump）—— 由调用方决定，say 本身不判断。
        if self._quiet is not None:
            self._quiet.append(text)
            return
        if self._output is not None:
            self._output(text)
        else:
            for c in self.controllers:
                c.notify(text)
            for o in self.observers:        # 【v0.22】旁观者只收播报，不参与决策
                o.notify(text)

    def say_state(self):
        """【v0.13】公示当前场面：文本进战报，结构化 JSON 给图形界面。

        以前是 self.say(self.state_text()) —— 网络客户端只能拿到一段中文，
        想画卡片就得自己解析文本。现在文本和结构化数据一起发，
        图形界面直接拿 state 渲染，命令行/日志读 text，两边都不吃亏。"""
        text = self.state_text()
        if self._output is not None:
            self._output(text)
            return
        state = self.state_json()
        for c in self.controllers:
            c.notify_state(text, state)
        for o in self.observers:            # 【v0.22】房主控制台靠这条画场面
            o.notify_state(text, state)

    def fire(self, event, field, ctx):
        """调用植物钩子。新机制优先考虑加钩子点，而不是往结算里塞 if。"""
        hook = field.spec.hooks.get(event)
        if hook:
            hook(self, field, ctx)

    def catalog_text(self):
        return "\n".join(PLANTS[n].line() for n in self.playable)

    def state_text(self):
        lines = ["—— 当前场面 ——"]
        for p in self.players:
            if not p.alive:
                lines.append("%s：已出局" % p.tag)
                continue
            f = p.field
            if f is None:
                fp = "（空场）"
            elif f.spec is EMPTY:
                fp = "空"
            else:
                if f.broken:
                    extra = "，已被击破"
                elif f.inert:
                    extra = "，未释放·属性归零"   # 【v0.7】
                else:
                    extra = ""
                fp = "%s（防%d%s）" % (f.spec.name, f.fang, extra)
            # 【v0.10】水滴 / 火种是持久资源，必须公示，否则玩家算不出自己能干嘛
            # 【v0.18】超防同理 —— 不公示的话，对手会以为自己打中了，
            #   而实际上伤害被吞掉了（"打不动"必须有解释，否则像 bug）。
            marks = ""
            if p.water:
                marks += "｜水滴%d" % p.water
            if p.fire:
                marks += "｜火种%d/%d" % (p.fire, FIRE_BURST_AT)
            if p.super_def:
                marks += "｜超防%d回合" % p.super_def
            # 【v0.54】[免疫] 必须公示 —— 否则玩家看见自己的倍耗"莫名其妙没了"，
            #   会当成 bug（和超防"打不动"同一个道理：机制透明才谈得上博弈）。
            if p.immune_turns:
                marks += "｜免疫%d回合" % p.immune_turns
            # 【v0.25】消耗加成也要公示：不写的话玩家算不出"他这株要花多少"，
            #   更看不出"下回合会变贵"——机制透明才谈得上博弈。
            if p.fishy:
                marks += "｜鱼腥味%d" % p.fishy
            if p.mult:
                marks += "｜倍耗%d(×%d)" % (p.mult, self.mult_rate(p))
            if p.fishy_next or p.mult_next:
                pend = []
                if p.fishy_next:
                    pend.append("鱼腥味+%d" % p.fishy_next)
                if p.mult_next:
                    pend.append("倍耗+%d" % p.mult_next)
                marks += "｜(下回合起%s)" % "/".join(pend)
            lines.append("%s：生命%s｜种子%d%s｜场上：%s"
                         % (p.tag, p.hp_text(), p.seeds, marks, fp))
        # 【v0.16】人参果的段标记必须公示 —— 不然玩家看不见"哪一段被埋了、还差几笔"，
        #   也就无从决定还要不要出那一段的植物（机制透明才谈得上博弈）。
        if self.tag_marks:
            lines.append("—— 人参果标记 ——")
            for n in sorted(self.tag_marks):
                rec = self.tag_marks[n]
                extra = ""
                if rec.burst_round is not None:
                    extra = "｜已爆发，剩 %d 回合" % max(
                        0, rec.burst_round + TAG_KEEP - self.round)
                lines.append("  段 %d：累积 %d/%d｜埋雷者：%s%s"
                             % (n, rec.count, TAG_BURST_AT,
                                "、".join(o.tag for o in
                                          sorted(rec.owners, key=lambda x: x.idx)),
                                extra))
        return "\n".join(lines)

    def state_json(self):
        """【v0.12】结构化场面 —— 给图形界面渲染用的。

        state_text() 是给人读的文本（命令行 / 日志），这个给 UI 渲染卡片用：
        界面需要"player2 的生命 / 种子 / 场上植物 / 防御值"这些字段逐个拿出来排版，
        靠解析文本太脆。
        纯新增方法，不参与任何结算逻辑 —— 加它不会影响现有行为。"""
        out = {"round": self.round, "players": []}
        # 【v0.22】开局种子也下发一份：控制台/界面要显示"本局每人起手 N 颗"，
        #   别让前端去猜或反推。纯新增字段，老客户端自动忽略。
        out["start_seeds"] = self.start_seeds
        # 【v0.25】本局生效的房间规则也下发一份：控制台/界面要显示
        #   "本房丝瓜不封顶"这类差异，别让前端去猜。纯新增字段，老客户端忽略。
        out["rules"] = dict(self.rules)
        for p in self.players:
            f = p.field
            d = {"idx": p.idx, "name": p.name, "alive": p.alive,
                 # 【v0.35】"名字+编号"的显示串（界面上要的就是这个）。
                 #   ★ 统一在引擎里拼：界面各端（web / tk / 控制台）都别自己
                 #     拼格式，不然迟早出现"网页带编号、别的端不带"。
                 #   你 2026-09-30 选的格式 = "小明(2号)"。
                 "name_tag": p.tag,
                 "hp": p.hp, "hp_text": p.hp_text(), "seeds": p.seeds,
                 "water": p.water, "fire": p.fire,
                 # 【v0.18】超防还剩几回合（0 = 没有）—— 网页靠它画"无敌"徽标
                 "super_def": p.super_def,
                 # 【v0.54】[免疫] 还剩几回合（0 = 没有）—— 界面靠它画"免疫"徽标
                 "immune_turns": p.immune_turns,
                 # 【v0.25】消耗加成：生效中的鱼腥味/倍耗 + 下回合才生效的待入账层数
                 "fishy": p.fishy, "mult": p.mult,
                 "fishy_next": p.fishy_next, "mult_next": p.mult_next,
                 "mult_rate": self.mult_rate(p),
                 "fire_burst_at": FIRE_BURST_AT,
                 # 【v0.45】这个人的队友链代表元（= 所属分队），界面用来分组/连线
                 "team": self.team_root(p)}
            if f is None:
                d["plant"] = None
            else:
                d["plant"] = {"name": f.spec.name, "fang": f.fang,
                              "broken": f.broken, "inert": f.inert,
                              "desc": f.spec.desc}
            out["players"].append(d)
        # 【v0.16】人参果段标记（给图形界面画"埋雷状态"，命令行走上面的文本）
        out["tag_marks"] = [
            {"seg": n, "count": rec.count, "threshold": TAG_BURST_AT,
             "owners": sorted(o.idx for o in rec.owners),
             "burst_round": rec.burst_round,
             "rounds_left": (max(0, rec.burst_round + TAG_KEEP - self.round)
                             if rec.burst_round is not None else None)}
            for n, rec in sorted(self.tag_marks.items())]
        # 【v0.28】存活环：按座位顺序排的"还活着的人"。界面要画圈、要标"谁在谁旁边"
        #   都靠它 —— 别在界面里自己按 hp 过滤一遍（那会漏掉"实时摘人"这个状态）。
        out["ring"] = [p.idx for p in self.ring]
        # 【v0.45】队友链分图：给界面画"星形图"用（用户："任意选取一个人作为
        #   代表元，把图转化为星形图"）。
        #   · teams   = 存活者的连通分图，每组一个列表（组内按 idx 升序，
        #               第 0 个 = 代表元/中心点）；
        #   · team_of = 每个玩家（含出局的）所属分图的代表元 idx。
        #   前端只照这两样连线，不做任何"谁跟谁算一队"的判断。
        out["teams"] = [[p.idx for p in g] for g in self.team_groups()]
        out["team_of"] = {str(p.idx): self.team_root(p) for p in self.players}
        return out

    def others_alive(self, me):
        return [p for p in self.players if p is not me and p.alive]

    def ring_neighbors(self, target, exclude=None):
        """【v0.27】"两侧"= 环上 index 相邻的玩家（溅射等环相关机制用）。

        【v0.28】判定挪进了 Ring 对象本身（存活环成了一等公民）——
        这里只留一个转发口，免得各调用点满仓库地改。

        ★ 环 = 【存活者】，出局者当场摘掉、圈合拢。
        ★ exclude = 【释放方自己】（"溅射目标不包括自己"）。
        ★ 不足 3 人 → 两侧会重合或为空 → 返回空表 = 不触发；
          3 人局里释放方自己占掉一侧，剩下一侧照样算。
        """
        return self.ring.neighbors(target, exclude)

    def _leave_ring(self, p):
        """【v0.28】某人生命归 0 → 当场退出存活环（全项目唯一的摘人口）。"""
        if self.ring.remove(p):
            self.say("  ● %s 生命归 0 → 退出存活环（环上还剩 %d 人：%s）"
                     % (p.tag, len(self.ring), self.ring.text() or "空"))

    def _sync_ring(self):
        """【v0.28】把环与"谁还活着"重新对齐 —— 兜底，不是常规路径。

        常规路径是 _leave_ring 实时摘人。这个只在【外部直接改过 hp】之后需要，
        目前有三处：
          · 测试脚本为了复用局面会手写 "p.hp = 2" 把打死的人复活（见 Test2）；
          · 【v0.51】复活机制（_revive_resolve_phase）把死人拉回场上；
          · 结算开头那道兜底。
        不对齐的后果很隐蔽：环上少一个人 → 邻居算错 → 环相关机制静默失灵。
        ★ 重建时按 `self.players` 的【编号顺序】取人 → 复活者【位置不变】
          （用户 2026-10-19 对 161 明确要求的那一条，靠这里天然满足）。
        故意不打印：它是自愈，不是战报里发生的事。
        """
        want = [p for p in self.players if p.alive]
        if want != self.ring.members:
            self.ring.members = want

    # ---------- 【v0.45】队友链（并查集）----------
    def team_root(self, p):
        """p 所在队的【代表元】（= 该树根的 idx；也就是"星形图"的中心）。"""
        return self.teams.find(p.idx)

    def same_team(self, a, b):
        """a、b 是不是队友（同一连通分图）。"""
        return self.teams.same(a.idx, b.idx)

    def team_groups(self):
        """【存活者】的连通分图：[[Player...], ...]（每组代表元 = 组内最小 idx）。

        ★ 只算存活者 —— 胜负判定、以及番茄 "[反馈逻辑]" 那个"恰好 2 个分图"的
          条件，都以"场上"为准，死人不算。
        ★ 全项目只此一处算分图，别处不许各写一份。
        """
        alive = [p for p in self.players if p.alive]
        byidx = {p.idx: p for p in self.players}
        groups = self.teams.groups([p.idx for p in alive])
        return [[byidx[i] for i in groups[root]] for root in sorted(groups)]

    def connect(self, a, b):
        """把 a、b 连成队友。返回 True = 这次真的合并了两支原本不同的队伍。

        ★★ 这是【全项目唯一的连链入口】（照 Ring.remove 那条老规矩）——
          "丝瓜万岁"的当场清账挂在这里，谁连的都会走到，不会漏。
        """
        if not self.teams.union(a.idx, b.idx):
            return False
        self._sweep_ally_loads(a, b)
        return True

    def _sweep_ally_loads(self, a, b):
        """【v0.45 · 丝瓜万岁】结成队友的【当场】清掉"来源已是队友"的倍耗/鱼腥味。

        规则（用户 2026-10-15 原话）："当丝瓜释放者和他人成为队友后，如果他人
        身上存在来源于丝瓜释放者的倍耗效果，则可以消除。[鱼腥味]同理"；
        确认时机 = 【成为队友的那一刻】（不等下一回合）。

        ★★【v0.49】整条规则受【可选规则集】开关 `loofah_wansui` 控制，
          默认【关】。关着 = 层只增不减（v0.45 之前的老行为）。
          ★ 开关关不等于"少做一步"：施加时也已经不跳过队友（见
            _release_effects 里那两段），两半一起关才是完整的一条规则。

        ★ 只扫 a、b 合并后的那一支队；生效账（*_by）与待入账账（*_next_by）
          两本都要扫（后者是本回合刚加上、下回合才生效的那些）。
        """
        if not self.rules.get("loofah_wansui"):
            return
        team = [x for x in self.players if self.same_team(x, a)]
        cleared = []
        for x in team:
            for attr, label in (("mult_by", LOAD_MULT), ("fishy_by", LOAD_FISHY),
                                ("mult_next_by", LOAD_MULT),
                                ("fishy_next_by", LOAD_FISHY)):
                d = getattr(x, attr)
                # ★ 只认真实玩家编号的来源（0 = "无来源"的兼容格，跳过）
                for src in [s for s in list(d)
                            if s != x.idx and 0 < s <= len(self.players)
                            and self.teams.same(s, x.idx)]:
                    n = d.pop(src)
                    cleared.append("%s 身上来自 %s 的 %d 层【%s】"
                                   % (x.tag, self.players[src - 1].tag, n, label))
        if cleared:
            self.say("  ※【丝瓜万岁】%s 与 %s 结成队友 → 当场清除：%s"
                     % (a.tag, b.tag, "；".join(cleared)))

    # ---------- 【v0.48】死亡时的种子瓜分（原稿 [瓜分]） ----------
    def split_evenly(self, total, n):
        """把 total 平均分成 n 份 —— ★【全项目唯一的"平分"口径】。

        不整除的余数【随机】补给其中几人、每人最多 +1。
        用户 2026-10-19："你可以把'平分'写入机制，就是所有'平分'不整除的
        情况都是一样的" —— 所以写成函数、别处要平分就来调它。
        ★ 本函数被套用两次：先"分池"（几类接收者分几份），再"池内分人"。
        随机走 self.rng（可注入），测试才能复现。
        """
        if n <= 0:
            return []
        base, rem = divmod(total, n)
        parts = [base] * n
        if rem:
            for i in self.rng.sample(range(n), rem):
                parts[i] += 1
        return parts

    @staticmethod
    def _runs_devour(p):
        """这一位【本回合】场上那株，是不是一株释放了的噬魂类。"""
        f = p.field
        if f is None or f.spec is EMPTY or f.inert or f.cut_gone:
            return False
        a = f.spec.attack
        return bool(a is not None and a.is_devour)

    def seed_pools(self, dead):
        """死亡时要参与瓜分的"类" —— 每类一池，只保留非空的类。

        ★★ 用户 2026-10-19 原话："未来可能引入三池，不要写死。当场上只存在
          一类需要分池的，当然只给一类。" → 所以返回值是【列表】，不是两个变量；
          将来加第三类（比如原稿的"叠暴者"）就在这里 append 一项，其余逻辑不动。
        ★ 目前两类：
          · 噬魂者 —— 只在"他确实是死于噬魂"时才有这一类（否则整类为空）；
          · 队友   —— 队友链里还活着的人。
        """
        pools = []
        if dead in self.death_by_devour:
            who = [p for p in self.players
                   if p.alive and p is not dead and self._runs_devour(p)]
            if who:
                pools.append(("噬魂者", who))
        mates = [p for p in self.players
                 if p.alive and p is not dead and self.same_team(p, dead)]
        if mates:
            pools.append(("队友", mates))
        return pools

    def _on_death_seeds(self, dead):
        """【v0.48】生命归 0 的瞬间 → 种子按池瓜分。

        用户 2026-10-19 拍板的口径：
          · 分给【活着的队友】；"死于噬魂"时另有"噬魂者"那一池 → 先平分给各池，
            池内再平分给人（两次都走 split_evenly，不另写一套）；
          · 只有一类接收者时，这一类拿【全部】，不平白蒸发一半；
          · 死者【可以改数量】，默认值就是平均分出来的那一份。
        ★ 没有人可接 → 种子随人消散（只播报，不记账）。
        """
        total = dead.seeds
        if total <= 0:
            return
        pools = self.seed_pools(dead)
        if not pools:
            self.say("  · %s 的 %d 颗种子没有可继承的人 → 随人消散"
                     % (dead.tag, total))
            dead.seeds = 0
            return
        amounts = [total] if len(pools) == 1 else self.split_evenly(total, len(pools))
        allot = []                       # [(池名, Player, 颗数)]
        for (label, recips), amt in zip(pools, amounts):
            for r, n in zip(recips, self.split_evenly(amt, len(recips))):
                allot.append((label, r, n))
        self.say("  · %s 的 %d 颗种子进入 [瓜分] → %s"
                 % (dead.tag, total,
                    "、".join("%s（%s）%d 颗" % (r.tag, lab, n)
                              for lab, r, n in allot)))
        # ★ 下发前先刷新一次场面：此刻死者已退出存活环、种子还没转走。
        #   不刷新的话，界面上他还站在环里，却被问"你的种子分给谁"——
        #   自相矛盾（用户 2026-10-19 截图里就是这样）。
        self.say_state()
        allot = self._ask_seed_heir(dead, total, allot)     # 可改，默认 = 上面这份
        for _lab, r, n in allot:
            if n > 0:
                r.seeds += n
        dead.seeds = 0
        self.say("  · %s 的种子分配完毕：%s"
                 % (dead.tag,
                    "；".join("%s +%d" % (r.tag, n) for _lab, r, n in allot if n > 0)
                    or "无人接收"))

    def _heir_payload(self, c):
        """[瓜分] 题的候选要发给界面的字段（与 target 的 cands 同形）。

        ★ 界面只用它排版：名字 / 生命 / 场上植物 / 防御 / 手上多少种。
          规则判断一个字都不许写在前端（"默认分几颗"由 defaults 给出）。
        """
        f = c.field
        return {"idx": c.idx, "name": c.name, "name_tag": c.tag,
                "hp": c.hp_text(), "hp_units": c.hp,
                "plant": f.spec.name if f is not None else "（无）",
                "fang": f.fang if f is not None else 0,
                "seeds": c.seeds}

    def _ask_seed_heir(self, dead, total, allot):
        """【v0.48】给【已经出局】的死者一道题：默认平均，可改数量。

        ★ 这是全项目唯一"问出局者"的题 —— 因为它就发生在他生命归 0 的那一刻。
          "出局 ≠ 断线"（v0.15 的硬边界）保证了问得到；
          但反过来，一个只是关掉页面去观战的死人**不许把整局掐了** ——
          所以这里【吞掉】连接异常，退回默认值（其余地方的断线照旧中止对局）。
        """
        cands, defaults = [], []
        for _lab, r, n in allot:
            if r not in cands:
                cands.append(r)
                defaults.append(0)
            defaults[cands.index(r)] += n
        valid = set(r.idx for r in cands)

        def _ok(v):
            try:
                pairs = [[int(k), int(c)] for k, c in v]
            except Exception:
                return False
            seen, s = set(), 0
            for k, c in pairs:
                if k in seen or c < 0 or k not in valid:
                    return False
                seen.add(k)
                s += c
            return s == total

        # 【v0.50 修】空名单绝不下发：validate 要求"合计 == total"，
        #   而 total > 0 时没有任何候选 → 永远凑不出合法答案 → 三端一起卡死。
        #   理论上 seed_pools 已经保证非空，这里是第二道闸。
        if not cands:
            self.say("  · %s 的 %d 颗种子没有可继承的人 → 随人消散"
                     % (dead.tag, total))
            return []
        q = Question("seed_heir", dead, self, validate=_ok, refs={"cands": cands},
                     total=total,
                     # ★★【v0.50 修】cands 必须【同时】进 payload。
                     #   v0.48 只写了 refs={"cands": cands} —— refs 是给【本地控制器】
                     #   （热座 / 电脑 / 脚本）用的对象列表，而 msg() 只发 payload。
                     #   于是 web / netclient / gui 三端拿到的 cands 全是 undefined：
                     #   界面一行输入框都不渲染 → 合计永远 0/total → 按钮永远灰 →
                     #   玩家被卡在瓜分题上（用户 2026-10-19 实测截图）。
                     cands=[self._heir_payload(r) for r in cands],
                     defaults=[{"idx": r.idx, "n": n}
                               for r, n in zip(cands, defaults)])
        try:
            ans = self.gather([q]).get(dead.idx)
        except (ConnectionError, OSError):
            self.say("  · %s 已断开 → 种子按默认平均分配" % dead.tag)
            return allot
        if not ans:
            return allot
        got = {}
        for k, c in ans:
            got[int(k)] = int(c)
        return [(lab, r, got.get(r.idx, n)) for lab, r, n in allot]

    # ---------- 【v0.51】复活（161 生命之花）----------
    def _mark_death(self, p):
        """记下"死掉的那一刻"的快照 —— ★ 全项目唯一拍快照的地方。

        ★ 调用点只有两处，两处都必须在"种子被拿走"【之前】：
          ① `_devour_hit` ③b：预演血量已经 ≤ 0、但 `tgt.seeds -= take` 还没执行
             —— 不抢这一下，"死亡时的种子"就被噬魂吃掉了，复活回来是空的；
          ② `_flush_damage`：伤害真正落地、`hp <= 0` 的那一刻，抢在
             `_on_death_seeds`（[瓜分] 分给队友并把死者清零）之前。
        ★ 幂等（已有快照就返回）：一次死亡会先被 ① 拍、再被 ② 摸到一次；
          若 ② 重拍，记下的就是"被噬魂挖走之后"的种子 —— 正是要避免的。
        ★ 复活时把 `death_snapshot` 清回 None（见 `_do_revive`），
          所以"活过来又死一次"会自然拍出一份新快照，不用另写分支。
        """
        if p.death_snapshot is not None:
            return
        p.death_snapshot = {"round": self.round,
                            "seeds": int(p.seeds),
                            "hp_start": int(p.round_start_hp)}

    def _revivable(self, reviver):
        """现在能被 reviver 复活的人（按座位顺序）。

        三条判据，一条都不能少：
          · 人已经出局（`hp <= 0`）；
          · 有死亡快照（确实死过；开局就没上场的人不算）；
          · **死在更早的回合**（`snapshot["round"] < self.round`）——
            原稿"（非当局死亡）"的唯一读法（用户 2026-10-19：
            "本回合刚死的不能救"）。
        ★ 不排除队友：原稿没写"只能救非队友"，救队友本来也更自然。
        ★ 也不排除"复活过又死"的人 —— 他只带着最新那份快照。
        """
        out = []
        for p in self.players:
            if p is reviver or p.hp > 0 or p.death_snapshot is None:
                continue
            if p.death_snapshot["round"] >= self.round:
                continue
            out.append(p)
        return out

    def _revive_hp(self, dead):
        """复活后给他多少血（半命单位）—— ★ 可选规则「生命之神」的【唯一】读点。

        关（默认）：1 条命（内部 2 点）—— 救回来但很脆。
        开：他死的那一回合【开局】时的血量（`round_start_hp` 的冻结值）。
          ★ 至少给 1 点：万一 hp_start 因意外记成 0，复活完还是死人就没意义了。
        """
        if self.rules.get("revive_full_hp", DEFAULT_RULES["revive_full_hp"]):
            return max(1, int(dead.death_snapshot["hp_start"]))
        return 2

    def _revive_payload(self, dead):
        """[复活] 题的候选字段（与 target / seed_heir 的 cands 同形）。

        ★ 界面只用它排版，规则判断一个字都不许写在前端：
          `hp` 是【复活后会有多少血】（不是他现在那 0 条），
          `seeds` 是【死亡时固化的种子】（不是他现在账上的 0 颗）。
        """
        snap = dead.death_snapshot
        return {"idx": dead.idx, "name": dead.name, "name_tag": dead.tag,
                "hp": self.hp_str(self._revive_hp(dead)),
                "hp_units": self._revive_hp(dead),
                "seeds": snap["seeds"],
                "died_round": snap["round"],
                "plant": (dead.field.spec.name
                          if dead.field is not None else "（无）")}

    def _q_revive(self, p, cands, by=None, cond=False):
        """161 的题：加血 or 复活（复活还要选人）。"""
        spec = p.field.spec
        valid = set(c.idx for c in cands)

        def _ok(v):
            if not isinstance(v, dict):
                return False
            mode = v.get("mode")
            if mode == "heal":
                return True
            if mode != "revive":
                return False
            t = v.get("target")
            return isinstance(t, int) and t in valid

        return Question("revive", by or p, self, validate=_ok,
                        refs={"cands": cands},
                        plant=spec.name,
                        own={"from": p.tag, "plant": spec.name,
                             "proxy": self._proxy_label(
                                 p, by, "你要让它救谁？", cond=cond)},
                        heal=self.hp_str(spec.heal),
                        # ★★ cands 必须【同时】进 payload（refs 只喂本地控制器）——
                        #   v0.50 的 seed_heir 就是漏了这一步，三端一个输入框都画不出来。
                        cands=[self._revive_payload(c) for c in cands])

    def _do_revive(self, reviver, dead):
        """把 dead 拉回场上，并与 reviver 连成队友。

        ★ 三件事的顺序写死：
          ① 复原血量与种子（快照）→ ② 清快照 → ③ 连链。
          连链放最后，是因为 `connect` 会触发【丝瓜万岁】的当场清账
          （按"现在谁是谁的队友"扫层），先把人救活、再让清账看到完整的一队。
        ★ 状态都改完再 `_sync_ring()`（在调用方统一做一次）：
          存活环按 `self.players` 的编号顺序重建 → 复活者【位置不变】。
        """
        snap = dead.death_snapshot
        hp = self._revive_hp(dead)
        seeds = int(snap["seeds"])
        dead.hp = hp
        # ★★ 种子【不守恒】（用户 2026-10-19 拍板）：[瓜分] 早已把这批种子
        #   分给队友并把死者清零，这里等于把同一批种子【再造一份】还给他。
        #   不要"从队友那里扣回来"—— 那是守恒，用户明确否掉了。
        dead.seeds = seeds
        dead.death_snapshot = None
        # 复活者本回合【没有出牌】，但人已经在场上了 —— 给一株【空】占位，
        #   免得后面任何一处 `p.field.spec` 在"活着但 field 是 None"上炸掉
        #   （结算里 ⑧ 火种爆发等是按 `for p in self.players` 扫的）。
        if dead.field is None:
            dead.field = FieldPlant(dead, EMPTY)
        self.say("  ● %s 的【%s】→ [复活] %s：生命恢复 %s 条（现有 %s 条）；"
                 "取回死亡时固化的 %d 颗种子（现有 %d）；与本回合无植物"
                 % (reviver.tag, reviver.field.spec.name, dead.tag,
                    self.hp_str(hp), dead.hp_text(), seeds, dead.seeds))
        # ★【v0.72】复活也是这株植物产出的"收益" —— 队友关系挂到【支配者】身上，
        #   不然被勾走的生命之花救活的人会去和原主连成一条链（那是胜负判据）。
        _who = self._holder(reviver)
        self.connect(_who, dead)
        self.say("     → %s 与 %s 连成队友（同一条链上共 %d 人）%s"
                 % (_who.tag, dead.tag,
                    len([x for x in self.players if self.same_team(x, _who)]),
                    "" if _who is reviver else
                    "（这一株已归 %s 支配 → 队友挂它名下）" % _who.tag))

    def _revive_phase(self):
        """【场末 3 复活】161 生命之花的"复活"这一半。

        ★★【拆分】用户 2026-10-25："一个植物的结算明明可以拆成多个，没有必要
          复活/加血放在一起，牛角菱不就拆了吗？猫尾草的超防和攻击不也拆了吗？"
          → 复活是【复活】这一步，加血并进【加血】那一步（跟所有加血一起）。
          二选一仍然只问一次（在【复活】这一步问），选"加血"就打个标记，
          轮到【加血】那一步再落地 —— 同一株只会走其中一条路。
        ★ 为什么复活排在加血之前：它救的是"上一回合及更早"死的人，和本回合
          的伤害没有冲突；放最前面，被救的人还能赶在环相关机制之前回到圈上。
        ★ 没有可救的人就【不问】，直接标记成"加血"（省一道空题，战报说明原因）。
        """
        holders = [p for p in self.players
                   if p.alive and p in self.released
                   and p.field is not None
                   and getattr(p.field.spec, "revive_one", False)]
        if not holders:
            return
        self.say("—— 复活（161 生命之花；与加血二选一）——")
        ask = []          # (植物主人, 候选, 真正答题的人)
        for p in holders:
            cands = self._revivable(p)
            if cands:
                # ★【v0.72】被[勾取]的这一株，"救谁"该问勾取者 ——
                #   `_hook_pass("revive")` 已经先跑过，此刻 hooked_by 是确定的。
                _hk = getattr(p.field, "hooked_by", None)
                ask.append((p, cands, _hk if _hk is not None else p))
            else:
                self.say("  · %s 的【%s】：没有可复活的人（本回合刚死的不算）"
                         "→ 只能选加血"
                         % (p.tag, p.field.spec.name))
                self.revive_mode_plans[p] = "heal"
        # ★★ 必须用 gather_by_batches：两株生命之花可能同时被同一个人支配
        #   （勾取者一人答两份），而 gather() 一批里一个答题人只能有一道题 ——
        #   否则 SyncController 会 poll 到空、直接 RuntimeError。
        _pos = {pp: i for i, (pp, _c, _a) in enumerate(ask)}
        ans = self.gather_by_batches(
            [self._q_revive(p, c, by=(a if a is not p else None))
             for p, c, a in ask]) if ask else {}
        for p in holders:
            v = ans.get(_pos.get(p)) or {}
            if v.get("mode") != "revive" or v.get("target") is None:
                self.revive_mode_plans[p] = "heal"
                continue
            tgt = None
            for c in self._revivable(p):     # 重新算一次：上面可能已有人被救起
                if c.idx == v.get("target"):
                    tgt = c
                    break
            if tgt is None:
                # validate 已经挡过非法编号，走到这里只能是"同一个死人被两个
                #   生命之花同时选中、前面那个已经把他救活了" —— 退回加血。
                self.revive_mode_plans[p] = "heal"
                self.say("  · %s 的【%s】想救的人已经被别人救活 → 改为加血"
                         "（在【加血】那一步落地）"
                         % (p.tag, p.field.spec.name))
                continue
            self.revive_mode_plans[p] = "revive"
            self._do_revive(p, tgt)
        self._sync_ring()

    def alive_count(self):
        return sum(1 for p in self.players if p.alive)
    def is_duel(self):
        """【v0.11】单挑：场上只剩两个人。
        用【存活人数】判定，与这局开局是几个人无关 ——
        所以 3 人局打到只剩 2 人时，下一回合也会自动进入单挑模式。"""
        return self.alive_count() == 2

    @staticmethod
    def hp_str(units):
        """半命单位 → 人类可读（2→1，1→0.5）"""
        if units % 2 == 0:
            return str(units // 2)
        return "%g" % (units / 2.0)

    # ---------- 决策采集（v0.5：同时问、收满为止）----------
    def gather(self, questions):
        """同时向所有玩家下发问题，然后"收满为止"地收答案。

        questions —— 同一个决策步骤里所有待决策玩家的 Question
        返回 {player_idx: value}

        为什么要拆成"先全发、再收齐"两步：
        旧实现在 for 循环里挨个问，网络版里 player2 的问题要等 player1 答完才发出去，
        于是 player2 全程干等。现在所有人的问题一次性发出，大家同时在答，
        总耗时 = 最慢的那个人，而不是所有人耗时之和。
        """
        if not questions:
            return {}
        asked = {q.player_idx for q in questions}

        # ① 下发：每个控制器只管把问题送出去，网络版必须立刻返回
        for q in questions:
            self.controllers[q.player_idx - 1].dispatch(q)
        # 本轮没被问到的人：给个"等待他人"的提示（热座/电脑默认忽略）
        # 【v0.15】出局的人单独给一条文案 —— 他收到的是"观战"而不是"等待"。
        #   为什么较真这一句：出局【不等于】掉线（下面那条边界是硬约束），
        #   但界面上如果只写"等待其他玩家决策……"，出局的人会以为自己卡住了、
        #   甚至以为被踢了。后面要加复活机制，出局只是"暂时下场"，不是"离桌"，
        #   所以他跟活人一样留在同一个连接上，只是没有操作要做。
        for i, c in enumerate(self.controllers, 1):
            if i in asked:
                continue
            p = self.players[i - 1] if i - 1 < len(self.players) else None
            if p is not None and not p.alive:
                c.wait_hint("你已出局，正在观战……")
            else:
                c.wait_hint("等待其他玩家决策……")

        # ② 收齐：谁先答谁先入袋，没答的下一轮再问
        pending = list(questions)
        answers = {}
        while pending:
            for q in list(pending):
                v = self.controllers[q.player_idx - 1].poll()
                if v is PENDING:
                    continue
                answers[q.player_idx] = v
                pending.remove(q)
            if pending:
                time.sleep(POLL_INTERVAL)
        return answers

    def gather_by_batches(self, questions):
        """【v0.70】一批问题里，**同一个答题人只能出现一次** —— 按人轮转分批。

        ★★ 为什么需要：`gather()` 的答案是 `answers[q.player_idx]`，
          而且 `SyncController.dispatch` 只存一个待答 q。同一个人在一批里
          出现两道题，第二道会把第一道冲掉，第二次 poll 直接
          `RuntimeError: poll() 被调用，但没有待答的问题`。
        ★ 什么时候会发生：勾取系统里"一个人替好几株答"——
          v0.70 起勾到 2 株不触发反馈的植物【都出手】，于是勾取者要替两株
          各答一道"打谁 / 充几颗 / 聚电谁"。以前 act=1 只答一题，从没暴露。
        返回：{questions 里的下标: 答案}
        """
        buckets = {}
        for i, q in enumerate(questions):
            buckets.setdefault(q.player_idx, []).append((i, q))
        out = {}
        while any(buckets.values()):
            batch = [bk.pop(0) for bk in buckets.values() if bk]
            got = self.gather([q for _i, q in batch])
            for i, q in batch:
                out[i] = got.get(q.player_idx)
        return out

    # ---------- 四个阶段 ----------
    def run(self):
        self.say("《植物斗争》%d 人局开始！（规则见 游戏规则总结.txt）" % len(self.players))
        # 【v0.22】开局种子必须公示 —— 不然战报上"第一回合就有人出得起杨桃"
        #   会显得毫无来由（默认 0 = 不吭声，旧局面的战报一字不变）。
        if self.start_seeds:
            self.say("—— 开局种子：每人 %d 颗 ——" % self.start_seeds)
        # 【v0.25】房间规则：只要和默认值不同就公示一次 —— 否则"这房丝瓜封顶开了没"
        #   全靠房主口头说，玩家无从验证（话术由 plants_data.describe_rules 给）。
        rule_lines = describe_rules(self.rules)
        if rule_lines:
            self.say("—— 本局房间规则：%s ——" % "；".join(rule_lines))
        while not self.over and self.round < MAX_ROUNDS:
            self.round += 1
            self.say("")
            self.say("================ 第 %d 回合 ================" % self.round)
            self.phase_prep()
            if self.check_end():
                break
            self.phase_choose()
            self.phase_bluff()
            self.phase_settle()
            # 【v0.47】结算完立刻自检：计划表里不许留上一回合的幽灵条目
            #   （本回合刚死的人 field 仍在，不算幽灵 —— 判据见方法注释）。
            self._audit_plans_after_settle()
            self.check_end()
        if not self.over:
            self.say("达到 %d 回合上限，判定平局（防止死循环的保护阀）。" % MAX_ROUNDS)
        return self.winner

    def phase_prep(self):
        """回合开始前：清场 + 消耗加成生效 + 人参果段标记的到期检查 + 超防倒计时
        【待确认：未来在这里塞其它持续效果判定】"""
        for p in self.players:
            p.field = None
            # 【v0.51】记下"本回合开局的血量"—— 只有「生命之神」那条可选规则
            #   会读它（复活者拿回他死那回合开局的血量）。
            #   ★ 必须在这里刷、且在这一回合任何治疗/伤害之前：
            #     结算里 p.hp 早被改过，拿它当"开局血量"是错的。
            p.round_start_hp = p.hp
        # 【v0.48】"死于噬魂"的标记每回合重来 —— 它只描述本回合的这一口。
        self.death_by_devour = set()
        # 【v0.28】环与生命对齐：正常打下去不会有偏差（_leave_ring 实时摘人），
        #   这里是给"外部直接改 hp"的兜底 —— 测试脚本为了复用局面会把打死的人
        #   写活（`p.hp = 2`），不对齐的话环上就少一个人，邻居全算错。
        self._sync_ring()
        # 【v0.25】消耗加成（鱼腥味 / 倍耗）"下回合起生效"的兑现处。
        #   上一回合释放鱼腥草 / 丝瓜时，层数只记进了 *_next；到这里才并进
        #   生效值，于是"第 R 回合附加 → 第 R+1 回合起加重消耗"。
        #   ★ 必须放在【这里】而不是 _apply_release：回合最初已经清场、
        #     还没进 phase_choose，正好赶在"这一次要付的钱"之前。
        for p in self.players:
            # 【v0.45】层数按来源入账：待入账那本账并进"生效中"那本账
            #   （来源 idx 保留 —— 丝瓜万岁要按来源消除，见 _sweep_ally_loads）。
            gain = p.fishy_next
            if gain:
                for src, k in p.fishy_next_by.items():
                    p.fishy_by[src] = p.fishy_by.get(src, 0) + k
                p.fishy_next_by = {}
                self.say("—— %s 的【鱼腥味】生效：+%d 层（共 %d 层，"
                         "非基石类植物消耗 +%d 种）——"
                         % (p.tag, gain, p.fishy, p.fishy))
            gain = p.mult_next
            if gain:
                for src, k in p.mult_next_by.items():
                    p.mult_by[src] = p.mult_by.get(src, 0) + k
                p.mult_next_by = {}
                self.say("—— %s 的【倍耗】生效：+%d 层（共 %d 层，最低消耗 ×%d）——"
                         % (p.tag, gain, p.mult, self.mult_rate(p)))
        # 【v0.18】超防倒计时：每回合开始 -1，减到 0 就是过期。
        #   拿到手时 = 2（本回合 + 下回合）——
        #   本回合的结算发生在回合末，那时还没减，所以本回合算数；
        #   下回合开始减到 1，仍然算数；再下一回合减到 0，失效。正好两回合。
        for p in self.players:
            if p.super_def > 0:
                p.super_def -= 1
                if p.super_def == 0:
                    self.say("—— %s 的【超防】已到期（本回合起不再免伤）——" % p.tag)
                else:
                    self.say("—— %s 的【超防】还剩 %d 回合 ——" % (p.tag, p.super_def))
        # 【v0.16】人参果：爆发过的段标记再活 TAG_KEEP 个回合就撤掉
        #   （原稿"爆发2局后移除"，你确认按 2 个回合执行）。
        #   倒计时用【首次爆发】那回合算，不是每次爆发都刷新 ——
        #   否则每回合都炸、每回合把倒计时推后一次，记录就永远撤不掉了。
        for n in sorted(self.tag_marks):
            rec = self.tag_marks[n]
            if rec.burst_round is not None and self.round >= rec.burst_round + TAG_KEEP:
                del self.tag_marks[n]
                self.say("—— 【%d 段】的人参果与累积记录已移除（爆发后 %d 个回合）——"
                         % (n, TAG_KEEP))

    # ---------- 消耗公式（v0.25）----------
    def mask_loads(self, spec, player):
        """这个人身上哪些消耗加成对【这株】有效。
        照原稿 p37：倍耗对"所有植物（除特别标注无视的植物）"；
        鱼腥味对"非[基石]类（除特别标注无视的）"。基石类 = 种子/木盾/地盾。
        Σ返回 (倍耗层数, 鱼腥味层数)，某株特别标注无视的那一项按 0 算。"""
        ig = spec.ignore_loads
        mult = 0 if LOAD_MULT in ig else player.mult
        fishy = 0 if LOAD_FISHY in ig else player.fishy
        if spec.name in CORNERSTONE:
            fishy = 0          # 基石类免疫鱼腥味（倍耗照算，但基础消耗本来就是 0）
        return mult, fishy

    def mult_rate(self, player):
        """倍耗率 = 2n（原稿 p37）。n 的上限由【房间规则 mult_cap】给，
        默认 3（原稿"最多 3 个，即最大 6 倍"）；mult_cap = 0 → 不封顶。
        ★ 率为【线性】2n 而不是 2^n：1 层=×2、2 层=×4、3 层=×6 ——
          只有这样才能和原稿"最大 6 倍"对上（2^3=8 对不上）。
        ★ n = 0（没有倍耗）时率 = 1（不翻倍）—— 2n 只在 n≥1 时定义。"""
        n = player.mult
        cap = self.rules.get("mult_cap", 3)
        if cap:                  # 0 = 不封顶
            n = min(n, cap)
        return 2 * n if n > 0 else 1

    def cost_of(self, spec, player):
        """这一株【现在】要花多少种子。原稿 p37 的 [消耗] 公式：
            最低消耗 = [基础消耗]×[倍耗率] + [鱼腥味负荷] (+[定居负荷]，未实现)
            [消耗]   = 最低消耗 + [充能消耗] + [其他]
        ★ 两条顺序（用户 2026-09-21："倍耗在鱼腥味之前"）：
          先 基础消耗×倍耗率，再 + 鱼腥味 —— 倍耗【不】翻鱼腥味。
        ★ 充能在括号【外】、且不参与倍耗（"充能耗种不翻倍"）：
          人参果基础 0、充能 3、身上 x 鱼腥味 y 倍耗 → 此处只算 0×2y+x 那一半，
          充能的 3 走 phase_bluff 的充能步骤（单独扣），两步合计 = 3+x。
        ★ [定居] 那条原稿也有（"非[基石]类消耗种数+n"），但定居机制未实现，
          落到这里就是【暂时没有那一项】—— 将来加定居时在这里补一个负荷即可。"""
        mult, fishy = self.mask_loads(spec, player)
        n = mult
        cap = self.rules.get("mult_cap", 3)
        if cap:
            n = min(n, cap)
        rate = 2 * n if n > 0 else 1
        return spec.cost_seed * rate + fishy

    def phase_choose(self):
        """出牌阶段：全员【同时】暗选 → 收齐后公示 → 校验代价 → 支付/变空"""
        # 【v0.15】出局只意味着"这一轮没他的操作"，【不代表掉线】：
        #   引擎依然给他推场面和 wait，连接一秒钟都不动。
        #   将来加复活机制时，就是在这里把 hp>0 的人重新放回 alive。
        alive = [p for p in self.players if p.alive]
        answers = self.gather([self._q_plant(p) for p in alive])

        self.say("—— 出牌公示 ——")
        for p in alive:
            self.say("%s 打出【%s】" % (p.tag, answers[p.idx]))

        for p in alive:
            name = answers[p.idx]
            spec = PLANTS.get(name)
            cost = None
            if spec is not None:
                # 【v0.25】代价不再只看卡面的 cost_seed，而是走 [消耗] 公式
                #   （倍耗/鱼腥味会改数）。所拥有种数 < 最低消耗 → 变"空"且不耗种。
                cost = self.cost_of(spec, p)
                broke = p.seeds < cost
                die = spec.cost_hp > 0 and p.hp - spec.cost_hp <= 0
                if broke or die:
                    why = ("种子不够（需 %d，现有 %d）" % (cost, p.seeds) if broke
                           else "生命不够（耗 %d 半命会致死）" % spec.cost_hp)
                    self.say("%s 付不起【%s】的代价（%s），植物变“空”"
                             % (p.tag, name, why))
                    spec = None
            if spec is None:
                spec = EMPTY
            field = FieldPlant(p, spec)
            p.field = field
            if spec is not EMPTY:
                # 【v0.25】加成必须讲清楚：不吭声的话，玩家只看到"突然贵了"，
                #   却不知道是鱼腥味/倍耗在加价（机制透明才谈得上博弈）。
                if cost != spec.cost_seed:
                    self.say("%s 的【%s】受消耗加成：基础 %d 种 → 实付 %d 种"
                             % (p.tag, spec.name, spec.cost_seed, cost))
                p.seeds -= cost   # 【v0.7】种子在出牌时扣掉，之后选"不释放"也不退
                p.hp -= spec.cost_hp
            self.fire("on_play", field, {})
            # 注：产种子（seed_gain）【不在这里】结算 —— v0.7 起改为释放时才给，
            #     不释放则一分钱都拿不到（你的原话："不释放没有任何效果"）。

    # ---------- 计划表：唯一登记处 + 两道自检【v0.47】 ----------
    # ★★ 教训（v0.46 那次"死亡即断联"）：v0.45 新增 friend_plans 时，没人回头看
    #   phase_bluff 开头那串 `self.xxx = {}`，于是这本新表从来没被清过 →
    #   死人条目活到下一回合 → 结算读 `p.field.spec` 撞上 None → 游戏线程崩 →
    #   客户端只看到"断联"。**"记得清"不是机制，"清单 + 断言"才是。**
    #   → 现在清空只走 _reset_round_plans()，并且加了两道机器检查。
    PLAN_TABLES = (
        "plans",          # 攻击目标（散连击列表 / 连击展开后的重复项都存这儿）
        "splash_plans",   # 溅射
        "cut_plans",      # 刀类：切谁
        "cut_blocks",     # 刀类：互挡（历史命名，不带 _plans 后缀，靠清单覆盖）
        "absorb_plans",   # 电系：吸收
        "hook_plans",     # 【v0.57】[勾取]（103 牛角菱）
        "flash_plans",    # 电系：闪击
        "friend_plans",   # 队友连接（107 番茄）
        "revive_mode_plans",  # 161 的二选一结果：revive / heal
        "water_hp_plans",     # 水滴抵命（声明）：player -> True
        "water_def_plans",    # 水滴加固（声明）：player -> 花几个
        "friend_assist_plans",  # 番茄的最终目标（+9 辅助防在【辅助防】落地）
        "cut_exempt_plans",   # 切一半的豁免名单：target -> [被帮的人]
        # 【v0.71】勾取者的【条件方案】(勾取者编号, 被勾者编号) ->
        #   {"charge": 充几颗, "absorb": [field...], "target": (kind, 答案)}
        #   ★ 只有勾取在结算阶段真的兑现时才覆盖原主人自己那份（见 _hook_pass）。
        "cond_plans",
        # 【v0.72】被[聚电]收走那株的"链弹向哪一侧"（支配者的条件答案）
        #   (原主编号, 第几发) -> 目标 Player；聚电真兑现时才盖进 flash_plans
        "absorb_flash_plans",
        # 【v0.72】"如果[聚电]成功，收来的这株打谁"
        #   (回答者编号, id(被收那株)) -> ([目标...], 播报文字)
        #   ★ 每个可能收到它的人各存一份；结算时【谁真收到】用谁那份
        "absorb_target_plans",
    )

    def _reset_round_plans(self):
        """【唯一入口】回合初清空全部计划表 —— 别处不许再逐个 `self.xxx = {}`。"""
        for name in self.PLAN_TABLES:
            setattr(self, name, {})
        self._audit_plan_tables_cleared()

    def _audit_plan_tables_cleared(self):
        """【自检一】清表之后必须真的空，且没有"漏登记的同类表"。

        ① 命名契约：任何 `plans` / `*_plans` 的 dict 属性都必须出现在 PLAN_TABLES 里。
           以后再加一本 `xxx_plans` 却忘了登记 → 这里当场抛，**不会等到下一回合崩**。
           （`cut_blocks` 是历史命名例外，不在契约内，靠清单覆盖。）
        ② 清完之后再核一遍确实为空 —— 防"清单里有、清空逻辑写漏"。
        """
        missing = []
        for name, val in self.__dict__.items():
            if not isinstance(val, dict):
                continue
            if (name == "plans" or name.endswith("_plans")) \
                    and name not in self.PLAN_TABLES:
                missing.append(name)
        if missing:
            raise AssertionError(
                "计划表 %s 没有登记进 Game.PLAN_TABLES —— 它不会被回合初清空，"
                "跨回合残留会让死人的条目活到下一回合（v0.46 断联 bug 的根因）"
                % "、".join(sorted(missing)))
        dirty = [n for n in self.PLAN_TABLES if getattr(self, n)]
        if dirty:
            raise AssertionError(
                "回合初清表后这些计划表仍非空：%s —— 清空逻辑漏了"
                % "、".join(dirty))

    def _plan_key_player(self, key):
        """把计划表的键归一化成"涉及的玩家" —— 七张表用了三种键，别想当然。

        · `plans` / `splash_plans` / `cut_plans` / `friend_plans` / `cut_blocks`
          的键是 **Player**（cut_blocks 是 player_idx，另算）；
        · `absorb_plans` 的键是 **FieldPlant**（勾的是那一株植物，v0.38）；
        · `flash_plans` 的键是 **(玩家编号, 第几发)** 的 **tuple**（v0.38）。
        返回 None 表示"认不出归属"，此时跳过、不硬判（宁可漏报也不误伤）。
        """
        if isinstance(key, Player):
            return key
        if isinstance(key, FieldPlant):
            return key.player
        idx = None
        if isinstance(key, int):            # cut_blocks 的键就是 player_idx
            idx = key
        elif isinstance(key, tuple) and key:
            idx = key[0]
        if idx is not None:
            for p in self.players:
                if p.idx == idx:
                    return p
        return None

    def _audit_plans_after_settle(self):
        """【自检二】结算完之后，计划表里不许留"上一回合的幽灵"。

        ★ 判据只认 `p.field is None`，**不许改判 alive**：
          `field` 只在 `phase_prep()` 里被清成 None，所以"本回合刚死的人"
          field 仍指向那株植物（合法，不算幽灵）；只有"上一回合就死了、
          本回合还没上场的人"才会是 None —— 这样精确命中残留，不误伤。
        ★ 键的类型三样都有（Player / FieldPlant / (编号,发数)），一律先过
          `_plan_key_player` 归一化 —— 别拿 `for p in 表` 直接当 Player 用
          （v0.47 第一版就是在这儿栽的：flash_plans 的 tuple 键没有 .field）。
        """
        ghosts = []
        for name in self.PLAN_TABLES:
            for key in getattr(self, name):
                p = self._plan_key_player(key)
                if p is not None and p.field is None:
                    ghosts.append("%s@%s" % (name, p.tag))
        if ghosts:
            raise AssertionError(
                "结算后计划表里还有 %d 条幽灵条目（玩家已无场上植物却仍在表里）："
                "%s —— 下回合排序会读 p.field.spec 直接崩"
                % (len(ghosts), "、".join(ghosts)))

    def phase_bluff(self):
        """博弈阶段：公示场面后分三步，每一步都是【同时问、收满为止】：
           ① 释放决定 → ② 充能（仅释放了充能植物的人）→ ③ 指定目标（仅攻击植物）
        【已确认】充能在"决定释放之后、指定目标之前/之后"皆可，样品取目标之前。
        【v0.11】单挑（存活 2 人）时 ③ 整步跳过：目标自动锁定（只有一个对手）；
        ② 充能与 ①b/①c 的水滴照旧要问 —— 它们涉及"花多少"的取舍，不该用默认值。
        ★【v0.73】① 释放决定【不再跟单挑挂钩】：改看房间规则「战斗！爽」
          （开 = 全员默认释放、不问；关 = 照常逐个问，1v1 也问）。"""
        self.say("—— 博弈阶段 ——")
        self.say_state()
        self._reset_round_plans()
        # ★ knife_played【不能清】：规则 15.8 是"本局累计出过三次"。
        self.released = set()
        # 【v0.11】单挑模式：存活 2 人 → ③ 目标自动锁定（充能/水滴照旧要选）
        duel = self.duel_auto and self.is_duel()
        # ★【v0.73】"战斗！爽"：全员默认释放。它取代了原来"单挑自动释放"的硬编码 ——
        #   用户 2026-10-27："现在'单挑默认释放植物'这一条取消掉，
        #   改成检测是否开启这一条规则"。
        battle = bool(self.rules.get("battle_fun", DEFAULT_RULES["battle_fun"]))

        # ① 释放决定【v0.7】：场上植物的玩家【全部】同时决定释放与否
        #    —— 不问才奇怪：问不问本身就是信息，只问有主动效果的等于泄露了谁带了攻击牌。
        #    —— 例外一：被显式声明 must_release 的植物，不询问、出牌即自动释放。
        #    —— 例外二【v0.73】：房间规则「战斗！爽」开着时【全员不询问】，
        #       一律按释放处理（原来这一条挂在"单挑"上，已按用户要求摘掉）。
        #    —— 释放 = 维持原效果；不释放 = 本回合所有属性归零，且已付的种子不退。
        onfield = [p for p in self.players
                   if p.alive and p.field.spec is not EMPTY]

        if battle:
            # 「战斗！爽」：没有"不释放"这个选项了（它本来是多人谈判里的诚意信号）
            self.say("—— 战斗！爽：所有植物默认释放，略过释放确认 ——")
            for p in onfield:
                self.released.add(p)
                self._apply_release(p)
        else:
            # 【v0.38】forced 名单里现在有【两类】"不给不释放选项"的植物：
            #   · must_release —— 出牌即自动释放（主要是为了不泄露牌型）；
            #   · immortal（[不灭]）—— 原稿词条："使用者自己也不能决定释放与否"。
            #     两者战报关切口径不同，所以分开写；都走 _apply_release。
            # 【v0.45】第三类"不给不释放选项"：场上有【队友门】（22 猪笼草，
            #   link_gate）在场时，107 番茄（link_friend）也必须释放 ——
            #   用户原话："此时，番茄既不能不释放，也不能选取空目标"。
            #   理由：门在场就是为了接管一切"增加队友连接"，番茄若靠
            #   "不释放"躲开，等于绕过这道门。
            #   ⚠️ 这里判的是"猪笼草【在场上】"（本回合出牌出来了），不是
            #      "它已生效" —— 因为此刻它自己释放与否还在同一批里问（先有鸡）。
            #      真正能不能当门用，看结算期（_live_gates），见 _friend_resolve_phase。
            gates_onfield = [p for p in onfield
                             if getattr(p.field.spec, "link_gate", False)]
            gate_bound = ([p for p in onfield if p.field.spec.link_friend]
                          if gates_onfield else [])

            for p in onfield:
                spec = p.field.spec
                if spec.must_release or is_immortal(spec):
                    self.released.add(p)
                    if is_immortal(spec):
                        p.field.forced_release = True
                        self.say("%s 的【%s】自动释放（【不灭】—— 使用者自己也不能"
                                 "决定不释放）" % (p.tag, spec.name))
                    else:
                        self.say("%s 的【%s】自动释放（该植物必须释放，"
                                 "不给不释放的选项）" % (p.tag, spec.name))
                    self._apply_release(p)
                elif p in gate_bound:
                    self.released.add(p)
                    p.field.forced_release = True
                    self.say("%s 的【%s】自动释放（场上存在【猪笼草】队友门 —— "
                             "番茄不能不释放）" % (p.tag, spec.name))
                    self._apply_release(p)

            need = [p for p in onfield
                    if not (p.field.spec.must_release or is_immortal(p.field.spec))
                    and p not in gate_bound]

            if need:
                ans = self.gather([self._q_release(p) for p in need])
                for p in need:
                    if ans[p.idx]:
                        self.released.add(p)
                        self._apply_release(p)
                    else:
                        p.field.stand_down()
                        self.say("%s 不释放【%s】→ 本回合所有属性归零、没有任何效果"
                                 "（防御0、免疫失效、不产种子；出牌时付的种子也不退）"
                                 % (p.tag, p.field.spec.name))

        # ①b【v0.10】水滴：出喷菇的人可以选择"用 1 个水滴抵掉半条命代价"
        #     ★【2026-10-25】这里【只声明】：真正退血与扣水滴在【加血】那一步
        #       （效果要看结算顺序；声明期只记意图）。
        water_hp = [p for p in self.players
                    if p.alive and p in self.released and p.water > 0
                    and p.field.spec.cost_hp > 0
                    and p.field.spec.name in WATER_FUELS]
        if water_hp:
            self.say("—— 水滴：抵命（声明）——")
            ans = self.gather([self._q_water_hp(p) for p in water_hp])
            for p in water_hp:
                if ans[p.idx]:
                    self.water_hp_plans[p] = True
                    self.say("%s 声明：用 1 个水滴抵掉【%s】的半条命代价"
                             "（退血在【加血】那一步落地）"
                             % (p.tag, p.field.spec.name))

        # ①c【v0.10】水滴：花 x 个水滴 → 本回合防御 +x（1:1，你自己定的比例）
        #     ★【2026-10-25】同样只声明。而且预算里【算上本回合刚产的水】：
        #       水滴草的 +1 在【辅助防】那一步产出，同一步里先产出、后花费 ——
        #       这才叫"本回合即可使用"（旧版是拿这句当理由把产水滴留在声明期，
        #       用户 2026-10-25 指出那条理由是错的）。
        def _water_budget(q):
            extra = q.field.spec.water_gain if q in self.released else 0
            return q.water + extra

        water_def = [p for p in self.players
                     if p.alive and p in self.released and _water_budget(p) > 0]
        if water_def:
            self.say("—— 水滴：加固（声明）——")
            ans = self.gather([self._q_water_def(p, _water_budget(p))
                               for p in water_def])
            for p in water_def:
                n = ans[p.idx]
                if not n or n <= 0:
                    continue
                self.water_def_plans[p] = int(n)
                self.say("%s 声明：花 %d 个水滴加固（真扣与加防在【辅助防】那一步落地）"
                         % (p.tag, int(n)))


        # ★★【v0.70 挪位】[勾取]（牛角菱）/ [聚电]（聚电蓝莓）声明【挪到充能之前】。
        #   用户 2026-10-26 原话："是否勾取应该放在充能阶段之前，然后牛角菱就可以
        #   在充能阶段声明充能多少。或者说继承聚电蓝莓的所有问询。"
        #   理由很硬：勾取一旦发生，被勾那株的"充能几颗 / 聚电谁 / 打谁"
        #   就都该由勾取者答 —— 而充能原来排在勾取【之前】，等于让原主人先答完了，
        #   勾取者再想改也没有机会。所以顺序必须是：
        #       释放 → 勾取/聚电 → 充能 → 目标 → 闪电一击
        #   ⚠️ 结算侧不受影响：真正的"支配权转移"仍在 phase_settle 的
        #      _hook_pass(stage) 里按步骤兑现（那是另一回事，别混）。
        self._absorb_declare_phase()
        # ★★【v0.72】这里原来紧跟一句 self._distribute_absorbed()（当场定归属）。
        #   用户 2026-10-27 从战报里抓出来："博弈阶段居然就决定闪电草归属了，
        #   [聚电]这一行为肯定在结算阶段才生效"。
        #   后果比"顺序不美观"严重得多：没抢到的那一方连"如果聚电成功打谁"
        #   这道题都不会被问（它的申请在这里就被抹掉了）。
        #   → 归属挪到 _absorb_resolve_phase 开头，那是"电系：【勾走】结算"那一步。

        # ② 充能【v0.4】：释放了充能植物的玩家同时宣布充能种子数
        chargers = [p for p in self.players
                    if p.alive and p in self.released and p.field is not None
                    and p.field.spec.charge]
        # ★【v0.71】充能【一律由原主人自己答】（理由同[聚电]：不许代答）。
        #   被勾取的充能植物，勾取者另有一套"如果勾取成功 → 充几颗"的条件方案
        #   （_hook_cond_charge），只有勾取兑现时才重做一次。
        if chargers:
            self.say("—— 充能阶段 ——")
            _cq = [self._q_charge(p) for p in chargers]
            ans = self.gather(_cq)
            for p in chargers:
                spent = ans.get(p.idx)
                if not isinstance(spent, int) or isinstance(spent, bool):
                    spent = 0
                # ★ 记下充能【之前】的数值：勾取兑现后可能要按勾取者的方案重做
                f = p.field
                f.pre_charge = (f.fang, f.bao, getattr(f, "flash_chain", False))
                # ★★【v0.72】充能【之前】的种子数 —— 勾取者的条件方案要按它算。
                #   用户 2026-10-27："牛角菱选择充能时，聚电蓝莓的种子只剩 19
                #   （算上聚电蓝莓充能的种子了）"：原主人先充了 6，题面就只剩 19，
                #   可勾取一旦兑现，那 6 颗是【要退回去】的（_apply_cond_charge），
                #   所以勾取者看到的必须是 25。
                f.pre_charge_seeds = p.seeds
                self._do_charge(p, spent)
            # 【v0.71】勾取者的条件充能
            self._hook_cond_charge()
                # ↑ 上面的应用逻辑抽到 _do_charge() 里了（v0.71）

        # ②b【v0.16】人参果：建立/更新段标记 + 给本回合的累积记账。
        #     必须排在充能【之后】—— 人参果的充能数就是它要标记的段，
        #     充能之前压根不知道该记哪一段（见规则文档 10.2）。
        self._tag_mark_phase()

        # ★★【v0.45 挪位】刀叶类声明【从"攻击之前"挪到"攻击公告之后"】——
        #   用户 2026-10-15 原话："为了防止欺骗的发生，似乎应该先广播选取目标的
        #   公告，然后刀类在选取目标。前面好像搞错了。"
        #   → 于是推翻 v0.37 的"提前"（那一版的理由是"日志读起来顺"），
        #     改回"先看到打谁、再决定切谁"：信息不完整时先表态，谈不上下注。
        #   ⚠️ 只改【何时问】；真正的"切"仍在结算阶段【一切攻击之前】
        #     （_cut_resolve_phase，规则 15.7）。挪后不影响射程：它需要的
        #     "当前前段"由充能（②）决定，而充能仍排在这之前。详见 结算顺序.txt §7。

        # ③ 目标：所有有攻击的玩家选目标。分三种：
        #      single   —— 1 发子弹，选 1 个人
        #      spreader —— 散连击 N：一次选 N 个【不同】的人
        #      burster  —— 【v0.44】连击 N：一次性把 N 发【分配】给各目标
        #                  （一道题：给每个目标填发数，合计 = N；可全压一人、可分散）
        #    ★ v0.9~v0.43 的"连击逐发问 N 次"已废弃 —— 15 发要问 15 次，
        #      用户 2026-10-12 实测后要求改成一次性分配（见 _q_burst_split）。
        #      同一批里所有人仍是【同时被问】的。
        # 【v0.72】目标声明要有自己的小标题 —— 用户 2026-10-27：
        #   "你的'声明阶段'没有和充能阶段区分开来，[选取目标]和充能
        #    怎么可能是一个行为呢？"
        self.say("—— 目标声明（谁打谁 / 谁替谁答）——")
        shooters = []     # 单选
        spreaders = []    # 散连击：一次多选
        bursters = []     # 连击：一次性分配发数
        cat_splits = []   # 【v0.39】猫尾草：连击 N 发 → 玩家分配到左右两侧
        cat_tails = []    # 【v0.39】猫尾草：声明时环上无活人 → 推迟到攻击末尾再算
        # 【v0.58】被[勾取]且这回合会出手的那几株 → 目标由【勾取者】选，
        #   所以本人这一批不问（见 _hook_proxies / _hook_target_questions）。
        proxies = self._hook_proxies()
        for p in self.players:
            if not p.alive or p not in self.released:
                continue
            atk = p.field.spec.attack
            # 【v0.57】103 牛角菱选了【勾取形态】→ 本回合不攻击（防/暴已改成勾取形态的）。
            if p.field.form == "hook":
                self.say("%s 的【%s】本回合走【勾取形态】→ 不攻击"
                         % (p.tag, p.field.spec.name))
                continue
            if p in proxies:
                # 【v0.71】★ 不再跳过：原主人照常答自己的 —— 勾取一旦没兑现
                #   （被消除 / 被收走 / 主人中途出局），落地的就是这份。
                #   勾取者那套"如果勾取成功"的条件方案在 _hook_target_questions。
                self.say("%s 的【%s】被 %s 勾取 → 若勾取成功，这一击改由勾取者决定"
                         % (p.tag, p.field.spec.name, proxies[p].tag))
            if atk.attr == ATTR_NONE:
                continue
            if atk.quan:
                self.plans[p] = "ALL"
                self.say("%s 释放【%s】→ 全体攻击！" % (p.tag, p.field.spec.name))
                continue
            if not self.others_alive(p):
                continue  # 防御性检查：没有可选目标就不释放
            # 【v0.38/v0.39】★ 目标受限的植物（134 猫尾草："目标只能是最靠近自己的
            #   仍有活着者"）。
            #   ★★ 用户 2026-10-04 更正（v0.39）：存活环上"最近的"通常【有左右两个】
            #      （等距），不能替玩家默认挑一个 —— "有三个连击，玩家可以对两侧
            #      具体选择数量"。即连击 N 发【由玩家分配到左右两侧】。
            #   ★ 环上只剩 1 侧（或只剩 2 人）→ 全部发数给那一侧，不必问。
            #   ★ 结算顺序：猫尾草排在【所有攻击之后】（用户 2026-10-04：
            #     "所有攻击（含火种/段爆发）之后"）—— 因为存活环是实时的，
            #     它的"最近两侧"必须等到本回合别人都打完、环定型之后再算。
            #     见 phase_settle 尾部 _resolve_cat_tail()。
            if p.field.spec.nearest_target:
                sides = self.ring_neighbors(p, exclude=p)
                if not sides:
                    self.say("%s 释放【%s】→ 环上没有可打的活人，暂缓到攻击末尾再算"
                             % (p.tag, p.field.spec.name))
                    cat_tails.append(p)
                    continue
                n = atk.lian if atk.lian > 1 else 1
                if len(sides) == 1 or n <= 1:
                    # 只有一侧（或只 1 发）→ 无需问，全给这一侧
                    self.plans[p] = [sides[0]] * n
                    self.say("%s 释放【%s】→ 只有一侧可选：%s（%d 发全部命中）"
                             % (p.tag, p.field.spec.name, sides[0].tag, n))
                else:
                    cat_splits.append((p, sides, n))
                continue
            if duel:
                # 【v0.11】单挑：目标唯一，直接锁定，不弹框。
                #   连击 N 发【全部】打到这一个人身上（多人局才谈得上分散）。
                t = self.others_alive(p)[0]
                n = atk.lian if atk.lian > 1 else 1
                self.plans[p] = [t] * n
                if n > 1:
                    self.say("%s 释放【%s】→ 单挑自动锁定 %s（连击%d发全部命中）"
                             % (p.tag, p.field.spec.name, t.tag, n))
                else:
                    self.say("%s 释放【%s】→ 单挑自动锁定 %s"
                             % (p.tag, p.field.spec.name, t.tag))
                continue
            if atk.lian > 1:
                bursters.append(p)
            elif atk.san > 1 and len(self.others_alive(p)) > 1:
                spreaders.append(p)
            else:
                shooters.append(p)

        # 第一批：单选 / 散连击 / 【v0.44】连击发数分配 / 【v0.46】番茄连接
        #   ★【v0.46】番茄(107) 的"连谁"也并入【同一批】—— 两者都是"选目标"，
        #     必须同时下发、收齐后再公告。用户 2026-10-18 报的原话：
        #     "我在选择目标时已经能看到杨桃选择目标的公告了" ——
        #     旧版把番茄连接单列一批，于是先问完的那批（攻击者）公告已经播出去了，
        #     轮到番茄表态时它白拿了别人的目标（信息泄漏，博弈就没了）。
        #   ★ 公告顺序仍是"先攻击、后连接"（题面同时、播报有先后）。
        qs = [self._q_target(p) for p in shooters]
        qs += [self._q_targets(p) for p in spreaders]
        qs += [self._q_burst_split(p, self.others_alive(p),
                                   p.field.spec.attack.lian) for p in bursters]
        friend_qs, friend_ctx = self._friend_questions()
        qs += friend_qs
        ans = self.gather(qs) if qs else {}
        # 【v0.58】[勾取] 的"如果勾取成功，它打谁"—— 由【勾取者】答。
        #   ★ 必须在下面那批公告【之前】收齐：否则勾取者能看到别人的目标再表态
        #     （v0.46 番茄那条踩过的同一个坑）。
        #   ★ 单独一个 gather 是因为答案按 player_idx 存，同一个人不能在同一批里
        #     出现两道题。
        # 【v0.71】不再当场落地 —— 只登记成条件方案，等勾取真的兑现再盖上去
        self._hook_target_questions()
        # 【v0.70】[聚电]收为己用：被收走的那株，这一击由最终支配者决定打谁
        absorbed_jobs = self._absorbed_target_questions()
        if qs:
            for p in shooters:
                cands = self.others_alive(p)
                idx = ans[p.idx]
                target = next((c for c in cands if c.idx == idx), cands[0])
                self.plans[p] = [target]
                self.say("%s 释放【%s】→ 瞄准 %s" % (p.tag, p.field.spec.name, target.tag))
            for p in spreaders:
                cands = self.others_alive(p)
                picked = []
                for idx in ans[p.idx]:
                    t = next((c for c in cands if c.idx == idx), None)
                    if t is not None:
                        picked.append(t)
                self.plans[p] = picked
                self.say("%s 释放【%s】→ 散连击 %d 人：%s"
                         % (p.tag, p.field.spec.name, len(picked),
                            "、".join(t.tag for t in picked)))
            # 【v0.44】连击：一道题收全部发数 → [[编号, 发数], ...] 展开成 plans[p]。
            #   ★ 展开后 plans[p] 与老"逐发"写法【完全同形】（就是 N 个目标，
            #     顺序 = 分配顺序）—— 下游 _resolve_plan / 溅射 / 闪击一字不用改。
            for p in bursters:
                cands = self.others_alive(p)
                by = {c.idx: c for c in cands}
                seq, cnt, tg = [], {}, []
                for tidx, c in ans[p.idx]:
                    t = by.get(tidx)
                    if t is None or c <= 0:
                        continue
                    if t.idx not in cnt:
                        seq.append(t)
                    cnt[t.idx] = cnt.get(t.idx, 0) + c
                    tg += [t] * c
                if not tg:                      # 兜底（正解合计必 == n；防脚本乱答）
                    tg, seq, cnt = [cands[0]], [cands[0]], {cands[0].idx: 1}
                self.plans[p] = tg
                # 【v0.37】一株一行（"连击 5 发全部打 2 号" / "连击 15 发分配：X 8发、Y 7发"）。
                if len(seq) == 1:
                    self.say("%s 释放【%s】→ 连击 %d 发全部打 %s"
                             % (p.tag, p.field.spec.name, len(tg), seq[0].tag))
                else:
                    alloc = "、".join("%s %d发" % (t.tag, cnt[t.idx]) for t in seq)
                    self.say("%s 释放【%s】→ 连击 %d 发分配：%s"
                             % (p.tag, p.field.spec.name, len(tg), alloc))

        self._apply_absorbed_targets(absorbed_jobs)
        # ★【v0.72】absorbed_attacks 原来在这儿赋值 —— 等于"收成了"当既定事实。
        #   现在由 _absorb_resolve_phase 在【分完归属之后】重建（谁真收到才出手）。

        # ③d【v0.45 建，v0.46 并批】队友连接声明（107 番茄）的【播报】。
        #    ★ 题面已经在上面跟着攻击目标【同时下发】了（_friend_questions），
        #      这里只是把收齐的答案落成 friend_plans + 战报 —— 所以放到攻击公告
        #      之后播，日志读起来仍是"先打谁、再连谁"。
        #    ★ 猪笼草的"强制偏移"【不在这里】：这里只收集意图，
        #      偏移留到结算期（_friend_resolve_phase，挂在⓪ 刀类结算之后）。
        self._friend_apply_answers(ans, friend_ctx)
        # ★【v0.72】勾取者的"连谁"条件方案（见 _friend_cond_questions）
        self._friend_cond_questions(friend_ctx)

        # ③c【v0.45 挪位】刀类声明：从 v0.37 的"攻击之前"挪到"攻击公告之后"。
        #    ★ 见上面 ★★【v0.45 挪位】那段说明；结算仍在一切攻击之前。
        self._cut_declare_phase()

        # ③b【v0.27】溅射声明：有溅射的植物，逐个目标声明"往哪侧溅"。
        #    必须排在 ③ 之后 —— 两侧是【目标】的两侧，目标都还没定，问什么。
        self._splash_declare_phase()
        # 【v0.38】[闪电一击] 声明：与溅射同批（都在 ③ 目标确定之后）。
        self._flash_declare_phase()
        # 【v0.39】猫尾草分配：连击 N 发按玩家选择的【左右两侧发数】写进 plans。
        #   ★ 只写"打算打谁"；实际出手在【所有攻击之后】（见 phase_settle 尾部），
        #     到那时会按当时的存活环重新确认这两侧是否还活着。
        if cat_splits:
            qs = [self._q_cat_split(p, sides, n) for p, sides, n in cat_splits]
            ans = self.gather(qs)
            for p, sides, n in cat_splits:
                left, right = sides[0], sides[1]
                lc, rc = ans[p.idx]
                tg = [left] * lc + [right] * rc
                self.plans[p] = tg
                if lc and rc:
                    self.say("%s 释放【%s】→ 连击 %d 发分配：%s %d发、%s %d发"
                             % (p.tag, p.field.spec.name, n,
                                left.tag, lc, right.tag, rc))
                else:
                    t = left if lc else right
                    self.say("%s 释放【%s】→ 连击 %d 发全打 %s"
                             % (p.tag, p.field.spec.name, n, t.tag))
            # ★【v0.72】勾取者的条件方案（"如果勾取成功，这 N 发怎么分"）
            hook_act = self._hook_act_map()
            for p, sides, n in cat_splits:
                hk = hook_act.get(p)
                if hk is None:
                    continue
                a2 = self.gather([self._q_cat_split(p, sides, n, by=hk, cond=True)])
                lc, rc = a2[hk.idx]
                left, right = sides[0], sides[1]
                self.cond_plans.setdefault((hk.idx, p.idx), {})["cat"] = (
                    [left] * lc + [right] * rc)
                if lc and rc:
                    self.say("%s 声明：如果勾取成功 → 【%s】连击 %d 发：%s %d发、%s %d发"
                             % (hk.tag, p.field.spec.name, n, left.tag, lc,
                                right.tag, rc))
                else:
                    self.say("%s 声明：如果勾取成功 → 【%s】连击 %d 发全打 %s"
                             % (hk.tag, p.field.spec.name, n,
                                (left if lc else right).tag))

    def _q_cat_split(self, p, sides, n, by=None, cond=False):
        """【v0.39】猫尾草：把连击 n 发分配给环上左右两侧（两侧各几发）。

        答案是 [左侧发数, 右侧发数]，两者之和必须 == n（且都 ≥ 0）。
        ★ sides 的顺序 = ring_neighbors(p, exclude=p) 的顺序：
          环上 me 的【前一个】= 左、【后一个】= 右（只剩 2 人时两侧是同一个，
          那时不会走到这里 —— 声明段已按"只有一侧"直接全给）。
        """
        left, right = sides[0], sides[1]
        def _mk(s):
            return {"idx": s.idx, "name": s.name, "name_tag": s.tag,
                    "hp": s.hp_text(), "hp_units": s.hp,
                    "plant": s.field.spec.name, "fang": s.field.fang,
                    "side": ("left" if s is left else "right")}
        def _valid(v):
            if not (isinstance(v, (list, tuple)) and len(v) == 2):
                return False
            a, b = v
            for x in (a, b):
                if isinstance(x, bool) or not isinstance(x, int) or x < 0:
                    return False
            return a + b == n
        return Question(
            "cat_split", by or p, self,
            plant=p.field.spec.name, shots=n,
            own={"from": p.tag, "plant": p.field.spec.name,
                 "proxy": self._proxy_label(
                     p, by, "这 %d 发怎么分给左右两侧？" % n, cond=cond)},
            left=_mk(left), right=_mk(right),
            refs={"sides": sides},
            validate=_valid)

    # ==================================================================
    # 【v0.45】队友链：107 番茄的"连接"声明（博弈阶段最后一步）
    # ==================================================================
    def _live_gates(self):
        """当前【能当队友门用】的猪笼草持有者（22 猪笼草）。

        ★ 判据 = 人活着 ∧ 本回合【释放】了 ∧ 场上就是猪笼草 ∧ 没被切成[空]
          ∧ 没有"不释放"（inert）。
        ★★ 这不是"在场即强制"（用户 2026-10-15 明确否掉那个读法），
          而是"效果触发时强制偏移" —— 调用点在结算期的 _friend_resolve_phase，
          那时刀类已经算完，"会不会被切空"才有定论。
        """
        return [p for p in self.players
                if p.alive and p in self.released and p.field is not None
                and getattr(p.field.spec, "link_gate", False)
                and not p.field.cut_gone and not p.field.inert]

    def _friend_questions(self):
        """107 番茄：把"连谁"的题面【准备好】，交给攻击目标那一批同时下发。

        返回 (questions, ctx)：
          questions —— 要下发的 Question 列表（可能为空）
          ctx       —— {p: {"cands", "gates", "plan"}}；plan 为 None 表示这株不用问
                       （场上没有别人可连 → 直接空目标）

        ★ 三件事在这里定（都只是"意图"，落地在结算期）：
          ① target    —— 连谁（None = 空目标 = 只给自己加防、不连人）；
          ② gate      —— 场上多株【猪笼草】时，"若门生效就偏移到哪一株"；
          ③ merge     —— 【只提示、不询问】哪些候选"连上就会强制触发[反馈逻辑]"。
                         反馈是强制的（v0.61），这里只把答案算出来写进题面，
                         让玩家在点之前就知道"这一按自己会成[空]"。
        ★ 猪笼草的约束（用户原话）："此时，番茄既不能不释放，也不能选取空目标"
          → 门在场时 allow_empty=False（"不能不释放"已在 ① 释放那一步强制）。
        ★ 多株门时"番茄再进行一次自由选择"（用户 2026-10-15）——
          因为结算阶段【不向任何人提问】，这次选择只能提前到声明期做：
          番茄在此就指定好"若门生效，偏移到哪一株"。
        ★★【v0.46】★★ 为什么拆成"出题 / 公布答案"两段：
          旧版在这里自己 gather 一次，等于【另起一批】—— 攻击者的目标公告
          已经播出去了，番茄才被问；用户 2026-10-18 报："我在选择目标时已经
          能看到杨桃选择目标的公告了"。现在题面并进 ③ 那一批，收齐后才一起公告。
        """
        friends = [p for p in self.players
                   if p.alive and p in self.released
                   and p.field is not None and p.field.spec.link_friend]
        if not friends:
            return [], {}
        two_groups = (len(self.team_groups()) == 2)
        gates = self._live_gates()          # 声明期先取一次（同一批里所有人看到同一份）
        questions, ctx = [], {}
        for p in friends:
            cands = self.others_alive(p)
            if not cands:
                ctx[p] = {"cands": [], "gates": gates, "plan": None}
                continue
            # 「会合并成最后一队」的候选 —— 只用于题面提示 + 声明战报，
            # 真正的判定在结算期重算一遍（那时可能有人已经死了）。
            merge = [c.idx for c in cands
                     if two_groups and not self.same_team(p, c)] if not gates else []
            allow_empty = not gates
            questions.append(self._q_friend(p, cands, gates, allow_empty, merge))
            ctx[p] = {"cands": cands, "gates": gates, "plan": True, "merge": merge}
        return questions, ctx

    def _friend_cond_questions(self, ctx):
        """【v0.72】被[勾取]的番茄：勾取者再答一套"如果勾取成功，让它连谁"。

        ★ 单独一批（gather_by_batches）：勾取者本人在同一批里可能还有自己的题，
          `gather()` 一批里一个答题人只能有一道题（否则答案互相覆盖）。
        ★ 答案进 `cond_plans["friend"]`，勾取兑现时由 `_apply_cond` 覆盖
          `friend_plans`（结算期 _friend_resolve_phase 读的就是它）。
        """
        if not ctx:
            return
        hook_act = self._hook_act_map()
        jobs = []
        for p, info in ctx.items():
            if info.get("plan") is None:
                continue
            hk = hook_act.get(p)
            if hk is None:
                continue
            jobs.append((p, hk, info))
        if not jobs:
            return
        got = self.gather_by_batches([
            self._q_friend(p, info["cands"], info["gates"],
                           not info["gates"], info.get("merge") or [],
                           by=hk, cond=True)
            for p, hk, info in jobs])
        for n, (p, hk, info) in enumerate(jobs):
            a = got.get(n) or {}
            idxmap = {c.idx: c for c in info["cands"]}
            t = a.get("target")
            target = idxmap.get(t) if t is not None else None
            gate = next((g for g in info["gates"] if g.idx == a.get("gate")), None)
            if info["gates"] and gate is None:
                gate = info["gates"][0]
            self.cond_plans.setdefault((hk.idx, p.idx), {})["friend"] = {
                "target": target, "gate": gate,
                "name": p.field.spec.name}
            self.say("%s 声明：如果勾取成功 → 让【%s】%s"
                     % (hk.tag, p.field.spec.name,
                        ("连 %s" % target.tag) if target is not None
                        else "空目标（不连人、也不加辅助防）"))

    def _friend_apply_answers(self, ans, ctx):
        """把收齐的"连谁"答案落成 friend_plans + 战报。

        ★ 与 _friend_questions 配对；ans 是那一批 gather 的总答案表。
        """
        if not ctx:
            return
        self.say("—— 队友连接：声明要连接谁 ——")
        for p, info in ctx.items():
            if p.field is None:                 # 防御：理论上不会（同一批内没人会死）
                continue
            spec = p.field.spec
            cands, gates = info["cands"], info["gates"]
            merge = info.get("merge") or []
            if info["plan"] is None:
                self.say("%s 的【%s】：场上没有别人可连（空目标 → 不连人、也不加防）"
                         % (p.tag, spec.name))
                self.friend_plans[p] = {"target": None, "gate": None,
                                        "name": spec.name}
                continue
            a = ans.get(p.idx) or {}
            by = {c.idx: c for c in cands}
            target = by.get(a.get("target")) if a.get("target") is not None else None
            gate = next((g for g in gates if g.idx == a.get("gate")), None)
            if gates and gate is None:
                gate = gates[0]
            plan = {"target": target, "gate": gate, "name": spec.name}
            self.friend_plans[p] = plan
            if target is None:
                self.say("%s 的【%s】声明：空目标 → 不连任何人、也不加任何辅助防"
                         % (p.tag, spec.name))
            else:
                keep = "（已是队友：只加防、不再重复连链）" if self.same_team(p, target) else ""
                self.say("%s 的【%s】声明：连接 %s%s"
                         % (p.tag, spec.name, target.tag, keep))
            if gates:
                self.say("  · 场上有【猪笼草】队友门 → 若它生效，今回的连接将被强制"
                         "偏移到 %s（多株时由你选的）" % gate.tag)
            # ★★【v0.61】反馈是强制的 → 声明期就把后果说清楚（不是选项，是预告）
            if target is not None and target.idx in merge:
                self.say("  · ⚠ 全场只剩两个队友链，连上 %s 就会并成一个 → "
                         "今回【必定】触发[反馈逻辑]（你自己成[空]，得 %d 颗种）"
                         % (target.tag, spec.feedback_value))

    def _q_friend(self, p, cands, gates, allow_empty, merge=None,
                  by=None, cond=False):
        """107 番茄的"连接"问题。

        答案 = {"target": 玩家编号|None, "gate": 玩家编号|None}
        —— 一个【复合答案】：一次问完"连谁 / 偏移到哪株门"，
        免得对同一个人连问两道题（用户一贯嫌问得多）。

        ★★【v0.61】`feedback` 这个字段**从题面里彻底删掉** —— 反馈是强制的，
          不是选项。`merge` 只是"哪些候选连上会强制反馈"的提示名单，
          前端拿去在卡片上标一句，后端不读它做判定（结算期自己重算）。
        """
        spec = p.field.spec
        cand_idx = {c.idx for c in cands}
        gate_idx = {g.idx for g in gates}
        merge = set(merge or ())

        def _mk(c):
            return {"idx": c.idx, "name": c.name, "name_tag": c.tag,
                    "hp": c.hp_text(), "hp_units": c.hp,
                    "plant": c.field.spec.name, "fang": c.field.fang,
                    "ally": self.same_team(p, c),
                    "merge": c.idx in merge}

        def ok(v):
            if not isinstance(v, dict):
                return False
            # ★ 旧客户端可能还回传 feedback —— 一律无视（不因为它判非法，
            #   否则会掉进"回 None → 判非法 → 原题重发 → 死循环"那个坑）。
            t = v.get("target")
            if t is None:
                if not allow_empty:
                    return False
            elif isinstance(t, bool) or not isinstance(t, int) or t not in cand_idx:
                return False
            g = v.get("gate")
            if gate_idx:
                if len(gate_idx) > 1 and g not in gate_idx:
                    return False
                if g is not None and g not in gate_idx:
                    return False
            elif g is not None:
                return False
            return True

        return Question(
            "friend", by or p, self,
            plant=spec.name,
            allow_empty=allow_empty,
            aux_def=spec.aux_def_gain,
            feedback_value=spec.feedback_value,
            merge=sorted(merge),
            gates=[{"idx": g.idx, "name_tag": g.tag, "plant": g.field.spec.name}
                   for g in gates],
            cands=[_mk(c) for c in cands],
            own={"from": p.tag, "plant": spec.name, "fang": p.field.fang,
                 "seeds": p.seeds, "hint": spec.friend_hint(),
                 "proxy": self._proxy_label(p, by, "你要让它连谁？", cond=cond)},
            refs={"cands": cands, "gates": gates},
            validate=ok)

    # ==================================================================
    # 【v0.32】刀叶类（消除 / 废除）—— 结算
    # ==================================================================
    def _void_one(self, target, why):
        """把一株植物打成[空]并让它的这一回合【彻底作废】——
        [消除] / [废除] / [毁灭] 三条路径共用的唯一落地口。

        三件事必须一起做（v0.33 的教训，长注释见 _cut_one 结尾）：
          ① to_empty()：防0 / 暴0 / inert / cut_gone（spec 不动 → 固有特性天然保留）；
          ② 从 released 摘掉 —— 否则后面的治疗、噬魂/有收益/普通攻击三批照样轮到它；
          ③ 从 plans / cut_plans 摘掉 —— 它声明过的出手全部作废。
        ★ why 只用于战报措辞，不影响判定。
        """
        if target.field is None or target.field.spec is EMPTY:
            return False
        target.field.to_empty()
        self.released.discard(target)
        self.plans.pop(target, None)
        self.cut_plans.pop(target, None)
        return True

    def _destroy_resolve_phase(self):
        """【v0.56】[毁灭]（151 黑洞大王花）—— 结算的【最前一步 ⓪-a】。

        ★★ 原稿 p07：
          `151 黑洞大碗花　回　强制启动，对场上所有目标产生一次[毁灭]，记被毁单位
            种子价值总和为 m，并在进入结算前最后瞬间获得⌊2m/n⌋颗种（其中 n 为场上
            存活人数）。若场上开始时超过 1 个黑洞大碗花，产生[反馈毁灭]，并获得
            [超防]当局（反馈值为 9）`
        ★ 词条（p30）：`[毁灭]：无视[不灭]，清除目标植物，使之成[空]（个别植物可
          抵抗多次）；毁灭无法去除[木盾][地质]的固有特性`。
          → 所以它【不看 immortal】，也不看超防（超防只拦伤害）。

        ★★ 为什么排在刀类（⓪-b）之前（用户 2026-10-21："显然应该排在刀类之前"）：
          黑洞一释放就把【除自己外全场】打成[空]，刀叶也在里面。若刀类先走，
          它先切掉一批，黑洞的 m 就缩水了 —— 顺序会改结果，所以这是规则不是排版。

        ★★【v0.57 改】[反馈湮灭] 挪到【这一批黑洞自己的效果之前】判
          （用户 2026-10-22："你可以把反馈和勾取一样，放在某个植物其它效果结算
          之前结算"）。同时计数口径也改了：**成[空] = 不存在**，不再数进去。
          于是：
            · 2 株黑洞 → 两株同时看到"还有 2 株" → 都反馈（各 9 种 + 当局超防），
              谁都不毁灭（原稿"解时超过 1 个 → 产生[反馈毁灭]"）。
            · 1 株黑洞 → 只看到自己 → 不反馈 → 正常毁灭全场。
            · 3 株黑洞、牛角菱勾走 2 株且只有 1 株出手 → 被勾的那 2 株里
              1 株出手、1 株成[空]（= 不存在）→ 场上"还存在"的是 2 株
              → 出手的那株反馈（9 种返给牛角菱方），与用户第 3 个例子一致。
          ★ 为什么必须【同一批一起判】而不是逐株判：逐株判时第 1 株反馈成[空]后，
            第 2 株只看到自己 → 不反馈 → 「牛头无敌」承诺的"两株都出手 = 9+9 = 18"
            永远凑不出来。
        ★ 本版还没有带"固有特性"的植物（[木盾][地质] 都还没实现），所以那条
          "毁灭动不了固有特性"只写在规则文档里 —— to_empty() 只改 FieldPlant 的
          数值、不动 spec，将来补这一条时要在 _void_one 里分叉，别在这里另开一路。
        """
        sources = [p for p in self.players
                   if p.alive and p.field is not None
                   and p.field.spec is not EMPTY
                   and getattr(p.field.spec, "destroy_all", False)
                   and p in self.released
                   and not p.field.cut_gone and not p.field.inert
                   and not p.field.annihilated]
        if not sources:
            return
        self.say("—— 毁灭：[%s]（结算最前一步 ⓪-a，排在消除 / 废除之前）——"
                 % "、".join(sorted({p.field.spec.name for p in sources})))
        n_alive = sum(1 for p in self.players if p.alive)
        # ①【v0.58】[反馈湮灭] 不在这里判了 —— 它跟[勾取]一样，挪到了
        #   "_feedback_pass(stage)"（每个结算阶段开始时，在这一批自己的效果【之前】）。
        #   ★ 为什么统一：用户 2026-10-22"把反馈和勾取一样，放在某个植物其它效果
        #     结算之前结算"。黑洞因此也在 ⓪-a 之前就被判掉 —— 不再有"独占一段"的特例。
        # ② 没被反馈掉的，才轮到[毁灭]
        for s in self._attack_order([x for x in sources if not x.field.annihilated]):
            f = s.field
            victims = [t for t in self.players
                       if t is not s and t.alive and t.field is not None
                       and t.field.spec is not EMPTY
                       and not t.field.cut_gone and not t.field.inert]
            m = sum(v.field.spec.cost_seed for v in victims)
            mul = getattr(f.spec, "destroy_reward_mul", 0) or 0
            gain = (mul * m) // n_alive if (mul and n_alive) else 0
            self.say("  ◆ %s 的【%s】强制启动：[毁灭]除自己外全场 %d 株"
                     "（被毁单位耗种总和 m=%d，存活人数 n=%d）"
                     % (s.tag, f.spec.name, len(victims), m, n_alive))
            for v in victims:
                name = v.field.spec.name
                self._void_one(v, "毁灭")
                self.say("     · %s 的【%s】被[毁灭] → 成[空]"
                         "（[不灭]挡不住；人还在，但这一回的结算作废）"
                         % (v.tag, name))
            if gain:
                # ★★【v0.72】收益归【支配者】，不是原主 ——
                #   这株被[勾取]/[聚电]之后，"收为己用"连它[毁灭]挣的种都该归支配者。
                #   用户 2026-10-27 看到的就是：黑洞已归真人3，6 颗种却回到真人1 手里。
                _boss = self._final_controller(s)
                _boss.seeds += gain
                self.say("  → 进入结算前最后瞬间：%s 获得 ⌊2m/n⌋ = %d 颗种"
                         "（现有 %d）%s"
                         % (_boss.tag, gain, _boss.seeds,
                            "" if _boss is s else
                            "（这株已被 %s 支配 —— 收益归支配者，不回原主 %s）"
                            % (_boss.tag, s.tag)))

    def _cut_resolve_phase(self):
        """把本回合声明的"切"兑现。

        ★ 落点（【v0.55】起）：结算的【第一步 ⓪】—— 排在 ① 复活/加血、
          ② 治疗【之前】，自然更在一切攻击之前（用户 2026-09-28 原话
          "在攻击发生之前"，v0.55 又往前挪了一步，见下方说明）。
        ★★ v0.55 为什么从"治疗之后"挪到"最前"（用户 2026-10-21）：
          原来只按"切在攻击之前"定位 —— 治疗不算攻击，就被顺手排在它后面，
          从没论证过。后果是治疗类（水蜜桃 / 仙桃 / 免疫菇）先加完血再被切成
          [空]：血已落袋，这一刀只剩拆防御，"消除"对治疗类等于失效。
          现在被切成 [空] 的植物会被 released.discard（见 _cut_one 结尾）→
          后面的复活 / 治疗自然轮不到它。
        ★ 超防【不挡刀】（用户 2026-10-21 拍板，待确认 #55 结案）：
          _cut_one 不看 super_def —— 仙桃 / 生命之花照样切得动。

        三步（顺序是规则的一部分）：
          ① 【互挡配对】（引擎底层自动做，玩家不声明）：
             同序列的刀叶两两互指 → 拿同序列的刀叶配成对，谁配谁【随机】；
             一把刀只能配一个、也只能被一个配（先到先得）；抢输的【无事发生】。
             配对成的一对：双方 locked（丧失废除能力）且 cuttable（可被切）。
          ② 按顺序逐株"切"：顺序与普通攻击同一套（编号降序 → priority → 随机）。
             ★ 已锁（locked）的刀叶【不能再切别人】；它要切的如果是互挡对手，
                那就切得动（cuttable），否则切不动（非互挡下切了【不破防】）。
          ③ 小刀叶"连出三次自扣血"：在它【本次出手】之后结算（规则 15.8）。
        """
        if not self.cut_plans:
            return
        self.say("—— 刀叶类：消除结算（排在一切攻击之前）——")
        # ① 互挡配对（底层自动）
        self._pair_cut_blocks()
        # ② 逐株切（顺序同普通攻击）
        #   【v0.46】排序前先把 field 已经是 None 的人剔掉（排序键要读 spec）。
        for p in self.cut_plans:
            if p.field is None:
                self.say("  · %s 已出局 → 其刀叶不再出手" % p.tag)
        order = self._attack_order([p for p in self.cut_plans if p.field is not None])
        for p in order:
            # 【v0.33】★ 用 .get()：本循环里 _cut_one() 可能把【别人】从 cut_plans
            #   摘掉（被切成空的植物会 self.cut_plans.pop(target) —— 见 _cut_one）。
            #   顺序是循环【开始前】算好的，所以这一株可能已经被前面的刀切没了。
            #   ⚠️ 别改回 self.cut_plans[p]：那是 KeyError（v0.32 的写法在这版会炸）。
            plan = self.cut_plans.get(p)
            # 【v0.46】兜底：人已经不在场（field 被回合初清成 None）→ 不能读 spec。
            #   根因（cut_plans 跨回合残留）已在 phase_bluff 开头清掉，这里只防身。
            if p.field is None:
                self.say("  · %s 已出局 → 其刀叶不再出手" % p.tag)
                continue
            if plan is None:
                self.say("  · %s 的【%s】本回合已被切除 → 不再出手"
                         % (p.tag, p.field.spec.name))
                continue
            # 【v0.70】★ 死了也能出手 —— 用户 2026-10-26 推翻了 v0.34 的攻方侧判据
            #   （"我从来没有说过出局就不出手了"）。
            if p.field.locked:
                self.say("  · %s 的【%s】处于【互挡】中 → 丧失废除能力，不再切别人"
                         % (p.tag, p.field.spec.name))
                continue
            if p.field.cut_gone:
                self.say("  · %s 的【%s】已被切成[空] → 切不动"
                         % (p.tag, p.field.spec.name))
                continue
            for t in plan["targets"]:
                self._cut_one(p, t, plan["half"], plan.get("exempt") or [])
        # ③ 小刀叶自扣血（放在最后统一判，与"它是否切成功"无关 —— 规则 15.8 是"出了三次"）
        self._knife_self_wound()
        # 兑现伤害（自扣血也是伤害）
        self._flush_damage()

    def _pair_cut_blocks(self):
        """【互挡】的底层配对（玩家不声明 —— 用户原话"互挡的发生在底层"）。

        规则 15.3：
          · 同序列的刀叶之间才谈得上互挡（不照原稿的逐株清单）。
          · 谁和谁配 = 【随机】（用户："我建议随机"）。
          · 一把刀只能配一个、也只能被一个配（先到先得）；抢输的【无事发生】。
          · 配成后双方：locked（丧失废除能力）+ cuttable（变成可被废除目标）。

        ★ 这里是"每回合重新配对"：互挡是【本回合的状态】（植物每回合重置），
          所以不跨回合留存 —— 与 FieldPlant 的生命周期一致。
        """
        # 收集"声明了要切、且是刀叶类、还活着"的人
        cutters = [p for p in self.cut_plans
                   if p.alive and p.field is not None and not p.field.cut_gone
                   and is_knife_class(p.field.spec)]
        if len(cutters) < 2:
            return
        # 按"序列"分组（同序列才能互挡）—— 序列 = no 前段（刀叶的编号是全局的，
        #   同序列 = 编号前段相同，如 12 小刀叶 与 12 小刀叶）。
        #   ★ 用 no 的前段判"同序列"：互挡是【卡面序列】的事，与"充能前缀可变"
        #     那个选项无关（那选项说的是"充能植物"，刀叶都是非充能植物）。
        groups = {}
        for p in cutters:
            if not p.field.spec.no:
                continue
            groups.setdefault(p.field.spec.no[0], []).append(p)
        for seg, members in groups.items():
            if len(members) < 2:
                continue
            shuffled = list(members)
            self.rng.shuffle(shuffled)
            # 两两配对：先到先得 → 顺序打乱后相邻配对
            for i in range(0, len(shuffled) - 1, 2):
                a, b = shuffled[i], shuffled[i + 1]
                self.cut_blocks[a.idx] = b.idx
                self.cut_blocks[b.idx] = a.idx
                a.field.locked = True
                a.field.cuttable = True
                b.field.locked = True
                b.field.cuttable = True
                self.say("  ◆ %s 的【%s】与 %s 的【%s】进入【互挡】"
                         "（同序列 %s 段）：双方丧失废除能力、且可被对方切除"
                         % (a.tag, a.field.spec.name, b.tag, b.field.spec.name, seg))

    def _cut_one(self, p, target, half, exempt=()):
        """一株刀叶对【一个】目标执行"切"。

        判定（规则 15.3 / 15.7）：
          · 非互挡状态下，切【刀叶类】的目标 = 切不动（"无视而不破切者"）。
            唯一例外：目标已在互挡中（cuttable）→ 切得动。
          · 切得动 → 目标成 [空]（消除 = 废除，同一个效果）。
          · "切一半"（half）：不把目标变成 [空]，而是标记 cut_half_done
            （防御保留、它的负面效果不外溢）—— 规则 15.6。
            ★★ 负面效果是【释放那一刻】就登记进 *_next（下回合生效）的，
              而"切"发生在结算阶段（晚于释放）—— 所以这里要【回滚】已登记的层数
              （用户 2026-09-28 拍板："回滚已登记的层数"）。
              exempt = 要豁免的人（含自己，多选）；给他们各减掉该植物刚附加的层数。
        """
        if not target.alive:
            self.say("  · %s 已出局 → %s 的【%s】这一刀落空"
                     % (target.tag, p.tag, p.field.spec.name))
            return
        tspec = target.field.spec
        # 【v0.56】[不灭]：p30 词条 `[不灭]：无视[废除][阉割]` —— 切不动。
        #   ★ 这里再兜一层（候选阶段 _is_uncuttable 已经滤过）：目标可能是本回合
        #     中途"才"获得不灭的（以后有这种牌），别指望候选名单永远是对的。
        if is_immortal(tspec):
            self.say("  · %s 的【%s】尝试消除 %s 的【%s】→ 对方有【不灭】→ 切不动"
                     "（只有[毁灭]动得了它）"
                     % (p.tag, p.field.spec.name, target.tag, tspec.name))
            return
        # 目标也是刀叶类，且没进入互挡 → 切不动（非互挡下"无视而不破切者"）
        if is_knife_class(tspec) and not target.field.cuttable:
            self.say("  · %s 的【%s】尝试消除 %s 的【%s】→ "
                     "对方也是刀叶类且未进入互挡 → 切不动（无视而不破）"
                     % (p.tag, p.field.spec.name, target.tag, tspec.name))
            return
        if half:
            target.field.cut_half_done = True
            self.say("  ◆ %s 的【%s】对 %s 的【%s】『切一半』→ "
                     "防御保留（防%d），但其负面效果不再附加给 %s"
                     % (p.tag, p.field.spec.name, target.tag, tspec.name,
                        target.field.fang,
                        "、".join(x.tag for x in exempt) if exempt else "（未指定）"))
            # 【2026-10-25】施加挪到【附加层】那一步之后，"切一半"不再需要回滚 ——
            #   把被帮的人记下来，轮到施加时直接跳过他们（见 _apply_effects_phase）。
            self.cut_exempt_plans[target] = list(exempt or ())
            return
        # 【v0.56】术语口径（用户 2026-10-21 纠正）：
        #   [消除] = 刀叶类的朴素清除行为；[废除] = 榴莲的朴素清除行为。
        #   两者【效果相同】（成[空]），差别只在来源与"免切①/②"各挡谁 ——
        #   所以这里只改措辞，效果仍然走同一个 _void_one。
        verb = "废除" if is_durian(p.field.spec) else "消除"
        self._void_one(target, verb)
        self.say("  ◆ %s 的【%s】%s %s 的【%s】→ 成[空]（防御、暴击归零；人还在）"
                 % (p.tag, p.field.spec.name, verb, target.tag, tspec.name))
        # 【v0.33 修】★ 成[空] = 这一株在本回合【彻底作废】，不只是数值归零 ——
        #   必须把它从"本回合的计划/释放名单"里【摘掉】，否则：
        #     · ⑤ 噬魂 / ⑥ 杨桃类 / ⑦ 普通攻击 照样会轮到它、照样出手
        #       （三批都只查 p.alive，不查"这株还在不在"）→ 出现"被废除的杨桃
        #       仍连击 5 发"这种冗余战报（用户 2026-09-29 实测发现）。
        #     · 它若也是刀叶、且声明了要切 → cut_plans 里还留着它（虽然
        #       _cut_resolve_phase 有 cut_gone 兜底，但摘干净更稳）。
        #   ★ 这与【火种烧菇成空】是同一个处理（见 _add_fire 结尾），
        #     那处早就这么做了 —— 这里只是补上刀叶这条路径，两处口径必须一致。
        #   ⚠️ 别改成"在三批循环里各加一次 cut_gone 判断"：那样改三处、将来还会漏
        #      （火种那条路径更会变成第四处）——"摘名单"才是唯一出口。
        #   ★【v0.56】这三步已经抽成 _void_one（[毁灭] 也走同一个口），
        #     上面那行 _void_one(target, verb) 就是它。

    def _rollback_negative(self, target, exempt):
        """【v0.32】切一半的兑现：把 target【刚登记的】负面效果从 exempt 身上撤回。

        来源 = target 本回合释放时执行的 fishy_gain / mult_gain
          （见 _apply_release：只记进 *_next，"下回合起生效"）。
        ★ 撤回的量 = 该植物卡面上的 gain（每人一份，互不影响）。
        ★ 只撤 *_next（本回合刚登记的）—— 不碰生效中的 fishy/mult
          （那是之前回合欠下的，不该被这一刀免掉）。
        """
        spec = target.field.spec
        if not exempt or (not spec.fishy_gain and not spec.mult_gain):
            return
        # 【v0.45】按【来源】撤：只能撤掉"来自 target 这个人"的那一层
        #   （层数已按来源记账，见 Player.fishy_by / mult_by）。
        src = target.idx
        for x in exempt:
            if spec.fishy_gain and x.fishy_next_by.get(src, 0) >= spec.fishy_gain:
                x.fishy_next_by[src] -= spec.fishy_gain
                if x.fishy_next_by[src] <= 0:
                    del x.fishy_next_by[src]
                self.say("    · %s 免于【%s】的鱼腥味（-1 层待入账，剩 %d）"
                         % (x.tag, spec.name, x.fishy_next))
            if spec.mult_gain and x.mult_next_by.get(src, 0) >= spec.mult_gain:
                x.mult_next_by[src] -= spec.mult_gain
                if x.mult_next_by[src] <= 0:
                    del x.mult_next_by[src]
                self.say("    · %s 免于【%s】的倍耗（-1 层待入账，剩 %d）"
                         % (x.tag, spec.name, x.mult_next))

    def _knife_self_wound(self):
        """【v0.32】小刀叶"连续出三次自扣 0.5 血"（规则 15.8）。

        口径：同一个玩家【本局内】累计释放过三次小刀叶 → 第 3 次那一刻自扣半条命。
        ★ 0.5 条命 = 1 个半命单位（用户 2026-09-28："0.5 就是半条命"）。
        ★ 只算【本回合释放了的】（未释放 = 没发动，不算"出过"）——
          规则 15.8 说的是"连出三次[小刀叶]"，不释放等于没出效果。
        """
        for p in self.players:
            if p not in self.released or not p.alive:
                continue
            if p.field is None or p.field.spec.name != "小刀叶":
                continue
            n = self.knife_played.get(p.idx, 0) + 1
            self.knife_played[p.idx] = n
            if n % CUT_SELF_WOUND_AT == 0:
                self._hurt(p, CUT_SELF_WOUND)
                self.say("  ※ %s 第 %d 次放出【小刀叶】→ 自扣半条命"
                         "（规则 15.8）" % (p.tag, n))

    def _splash_declare_phase(self):
        """【v0.27】溅射声明（用户 2026-09-21：两边分别由释放方决定）。

        ★ 为什么问在【博弈阶段】：与释放/目标同批 —— "声明意图"本来就是这一步的事；
          结算阶段一个人都不问（本项目的老约定，见 phase_settle 的文档）。
          声明了但主攻击【没造成伤害】→ 溅射自动作废（"造成伤害后"是触发条件）。
        ★ 多目标的（连击 / 全体攻击）按【第几发】逐批发问：gather 一轮只能问一个人一次。
          （★ v0.44 注：③ 的连击【目标】已改成一次性分配，但溅射仍是"逐发问两侧"——
           因为每一发的目标可能不同，两侧也就不同，这一层没法一次问完。）
        ★ 两侧为空（存活不足 3 人 / 一侧就是自己）→ 这一发不问。
        """
        slots = {}      # 第几发 -> [(玩家, 目标, [两侧])]
        for p in self.players:
            if p not in self.plans:
                continue
            if not p.field.spec.attack.splash:
                continue
            for k, t in enumerate(self._plan_targets(p)):
                sides = self.ring_neighbors(t, exclude=p)
                if sides:
                    slots.setdefault(k, []).append((p, t, sides))
        if not slots:
            return
        self.say("—— 溅射声明（两侧分别决定）——")
        for k in sorted(slots):
            batch = slots[k]
            ans = self.gather([self._q_splash(p, t, sides) for p, t, sides in batch])
            for p, t, sides in batch:
                picked = [s for s in sides if s.idx in ans[p.idx]]
                if picked:
                    self.splash_plans[(p.idx, k)] = picked
                    self.say("%s 声明：对 %s 造成伤害后 → 向 %s 溅射（暴%d·伤%s条命）"
                             % (p.tag, t.tag, "、".join(s.tag for s in picked),
                                SPLASH_BAO, self.hp_str(SPLASH_DAMAGE)))
                else:
                    self.say("%s 声明：对 %s 不溅射" % (p.tag, t.tag))

    def _flash_declare_phase(self):
        """【v0.38】[闪电一击] 声明：闪电链向【两侧之一】弹出（用户 2026-09-30：
        "释放方选一侧（像溅射）"）。

        ★ 与溅射声明的两点不同：
          · 溅射 = 两侧【各】一个（多选）；闪电链 = 两侧【之一】（单选）。
          · 闪电链还要看"这一株本回合到底带不带闪击"—— 读 field.flash_chain
            （57 聚电蓝莓充能到第二段才带；别的电系株出厂即带）。
        ★ 同溅射：按【第几发】逐批发问（连击每一发的两侧可能不同）。
        ★ 落点 = 溅射声明之后（③b 那一步的天然延续）。
        """
        # ★★【v0.72】被[勾取]且"条件充能"会把它推到第二段的株：
        #   它本回合【也带[闪电一击]】，可"弹向哪一侧"这道题原来根本没人问 ——
        #   因为题目按 field.flash_chain 筛，而那时它只按【原主人】的充能数算过
        #   （用户这局：原主人充 6 颗 = 第一段，勾取者改成 19 颗 = 第二段带闪击）。
        #   于是两株聚电蓝莓各带着一发闪击，一次都没弹过。
        #   → 让【勾取者】再答一套条件方案，兑现时才盖（同另外三道）。
        hook_act = self._hook_act_map()
        # ★★【v0.72】被[聚电]收走的那一株，它的攻击由支配者代理打出 ——
        #   "链弹向哪一侧"当然也该由【支配者】答。原来这里只按 field.flash_chain
        #   给【原主人】问了一遍：可原主人那一发被收走后根本不会打出去，
        #   收为己用的那一发带着闪击却从没弹过链。
        #   用户 2026-10-27："若[聚电]闪电草成功，要选择哪个目标 和 这个的闪电链"。
        #   ★ 声明阶段只能"乐观地"问（聚电还没兑现），所以记在 absorb_flash_plans，
        #     等 _absorb_resolve_phase 真收成了才盖进 flash_plans。
        # ★★【v0.72 二修】上一版这里是 `absorb_boss[原主] = 支配者` ——
        #   一个【按原主去重的 dict】：两株聚电蓝莓抢同一株闪电草时后者把前者
        #   直接盖掉，于是"链弹向哪一侧"只造出 1 道题（用户 2026-10-27：
        #   "闪电草的闪电链也只询问了 1，没有询问 2、3"）。
        #   而且回答者被写成"最终支配者"，把【申请人自己那份】吞掉了 ——
        #   勾取万一没兑现，原主就一份答案都没有（违反"不许代答"）。
        #   → 与 _absorbed_target_questions 一字不差地同构：每个申请人各答一份。
        absorb_apps = {}       # id(被收那株) -> [回答者, ...]
        _seen_abs = set()

        def _add_abs(_f, _answerer):
            if _f.spec is EMPTY or _f.cut_gone:
                return
            if _answerer is None or not _answerer.alive \
                    or _answerer.field is None:
                return
            _key = (_answerer.idx, id(_f))
            if _key in _seen_abs:
                return
            _seen_abs.add(_key)
            absorb_apps.setdefault(id(_f), []).append(_answerer)

        # ① 原主自己声明[聚电]的：问本人（勾取没兑现时落地的就是这份）
        for caster, fields in self.absorb_plans.items():
            for f in fields:
                if f.player is not caster:
                    _add_abs(f, caster)
        # ② 勾取者的条件方案（"如果勾取成功，让它聚电谁"）：问勾取者
        for (hk, _ow), cond in self.cond_plans.items():
            if "absorb" not in cond:
                continue
            hooker = next((pp for pp in self.players if pp.idx == hk), None)
            for f in cond["absorb"]:
                if f.player is not hooker:
                    _add_abs(f, hooker)
        jobs = []      # (p, t, sides, by, mode, k)；mode: None / "hook" / "absorb"
        for p in self.players:
            if p.field is None:
                continue
            # ★【v0.72】被[聚电]收走的那一株，它自己可能压根没声明目标
            #   （原主人没释放 / 没选目标），但"收来了打谁"这道题是支配者答的，
            #   所以不能再用 `p not in self.plans` 一刀切掉。
            ab_apps = absorb_apps.get(id(p.field), [])
            if p not in self.plans and not ab_apps:
                continue
            own_flash = bool(getattr(p.field, "flash_chain", False))
            cond_hooker = None
            if p in hook_act:
                if p.field.spec.threshold_charge is not None:
                    cond = self.cond_plans.get((hook_act[p].idx, p.idx)) or {}
                    if "charge" in cond:
                        _f, _b, _fl = self._threshold_charge_effect(
                            p.field.spec, int(cond["charge"] or 0))
                        if _fl:
                            cond_hooker = hook_act[p]
                elif own_flash:
                    # ★★【v0.72】出厂就带[闪电一击]的（闪电草）：被[勾取]之后
                    #   这一发是【勾取者】支配的，"弹向哪一侧"当然也该他答。
                    #   原来这里只处理"条件充能把它推到第二段"那一种，闪电草整个漏掉 ——
                    #   用户 2026-10-27："为什么牛角菱没有被询问闪电草的闪电链往哪里弹"。
                    cond_hooker = hook_act[p]
            # ★ 收来的那一发打谁，是【每个申请人各自】在 absorb_target_plans 里
            #   选的；两侧必须按【他那份】算（原主人的目标一兑现就作废了）。
            ab_targets = []        # [(回答者, 目标列表), ...]
            if own_flash:
                for _ans in ab_apps:
                    _ap = self.absorb_target_plans.get((_ans.idx, id(p.field)))
                    ab_targets.append(
                        (_ans, list(_ap[0]) if (_ap and _ap[0])
                         else list(self._plan_targets(p))))
            if not own_flash and cond_hooker is None and not ab_targets:
                continue
            for k, t in enumerate(self._plan_targets(p)):
                sides = self.ring_neighbors(t, exclude=p)
                if not sides:
                    continue
                if own_flash:
                    jobs.append((p, t, sides, None, None, k))
            # ★【v0.72】勾取者那份：两侧要按【他选的目标】算，不是原主人的。
            if cond_hooker is not None:
                cond_tg = self._cond_target_players(p, cond_hooker)
                if not cond_tg:
                    cond_tg = list(self._plan_targets(p))
                for k, t in enumerate(cond_tg):
                    sides = self.ring_neighbors(t, exclude=p)
                    if not sides:
                        continue
                    jobs.append((p, t, sides, cond_hooker, "hook", k))
            for _ans, _tg in ab_targets:
                for k, t in enumerate(_tg):
                    sides = self.ring_neighbors(t, exclude=p)
                    if not sides:
                        continue
                    jobs.append((p, t, sides, _ans, "absorb", k))
        if not jobs:
            return
        self.say("—— [闪电一击] 声明（闪电链向两侧之一弹出）——")
        for k in sorted({j[5] for j in jobs}):
            idxs = [i for i, j in enumerate(jobs) if j[5] == k]
            got = self.gather_by_batches(
                [self._q_flash(jobs[i][0], jobs[i][1], jobs[i][2],
                               by=jobs[i][3], cond=jobs[i][4] is not None,
                               verb="聚电" if jobs[i][4] == "absorb" else "勾取")
                 for i in idxs])
            for n, i in enumerate(idxs):
                p, t, sides, by, mode, _k = jobs[i]
                pick = next((s for s in sides if s.idx == got.get(n)), None)
                if mode == "absorb":
                    if pick is not None:
                        # ★ 键里带【回答者】—— 两株聚电蓝莓抢同一株时各答各的，
                        #   兑现时按"谁真收到它"取那一份（见 _absorb_resolve_phase）。
                        self.absorb_flash_plans[(by.idx, id(p.field), k)] = pick
                    self.say("%s 声明：如果[聚电]成功 → 对 %s 造成伤害后，闪电链%s"
                             % (by.tag, t.tag,
                                ("弹向 %s（暴%d·伤%s条命·无视超防）"
                                 % (pick.tag, FLASH_CHAIN_BAO,
                                    self.hp_str(FLASH_CHAIN_DAMAGE)))
                                if pick is not None else "不弹出"))
                elif mode == "hook":
                    if pick is not None:
                        self.cond_plans.setdefault((by.idx, p.idx), {})["flash"] = (k, pick)
                    self.say("%s 声明：如果勾取成功 → 对 %s 造成伤害后，闪电链%s"
                             % (by.tag, t.tag,
                                ("弹向 %s（暴%d·伤%s条命·无视超防）"
                                 % (pick.tag, FLASH_CHAIN_BAO,
                                    self.hp_str(FLASH_CHAIN_DAMAGE)))
                                if pick is not None else "不弹出"))
                elif pick is not None:
                    self.flash_plans[(p.idx, k)] = pick
                    self.say("%s 声明：对 %s 造成伤害后 → 闪电链弹向 %s（暴%d·伤%s条命·无视超防）"
                             % (p.tag, t.tag, pick.tag,
                                FLASH_CHAIN_BAO, self.hp_str(FLASH_CHAIN_DAMAGE)))
                else:
                    self.say("%s 声明：对 %s 的闪电链不弹出" % (p.tag, t.tag))

    # ==================================================================
    # 【v0.32】刀叶类（消除 / 废除）—— 声明
    # ==================================================================
    @staticmethod
    def _cut_plan_text(plan):
        """刀叶计划的尾注（切一半 / 全切 / 连切同名卡）—— 原主与勾取者两处共用。"""
        if plan["half"]:
            who = "、".join(x.tag for x in plan["exempt"]) if plan["exempt"] else "无"
            return "（切一半；帮 %s 免疫其负面效果）" % who
        return "（%s）" % ("全切同名卡" if plan["mode"] == "all" else "全切")

    def _ask_cut_plan(self, p, cands, by=None, cond=False):
        """问一套刀叶的四道题，返回计划；None = 本回合不切任何人。

        ★ `by` = 这一套问谁（None = 场上的主人）。勾取者的条件方案走【同一条路】——
          射程 / 连切 / 切一半 / 帮谁 这四个判据必须完全一致，不许另写一份。
        """
        asker = by or p
        ans = self.gather([self._q_cut_target(p, cands, by=by, cond=cond)])
        idx = ans[asker.idx]
        if idx is None:
            return None
        target = next((c for c in cands if c.idx == idx), None)
        if target is None:
            return None
        # ★★【v0.36】连切【只给刀叶类】（流星锤是刀类但不是刀叶类）。
        same = self._same_name_holders(p, target)
        mode = "single"
        if is_bladeleaf_class(p.field.spec) and len(same) >= 2:
            ans2 = self.gather([self._q_cut_mode(p, target, same, by=by, cond=cond)])
            mode = ans2[asker.idx]
        targets = same if mode == "all" else [target]
        half = False
        exempt = []
        if any(t.field.spec.cut_half for t in targets):
            ans3 = self.gather([self._q_cut_half(p, targets, by=by, cond=cond)])
            half = bool(ans3[asker.idx])
            if half:
                ans4 = self.gather([self._q_cut_help(p, by=by, cond=cond)])
                idxs = set(ans4[asker.idx])
                exempt = [x for x in self.players if x.idx in idxs]
        return {"targets": targets, "mode": mode, "half": half, "exempt": exempt}

    def _cut_declare_phase(self):
        """刀叶类在博弈阶段的声明：① 选目标（单株 / 全切同名卡）
        → ② 若要切的是"能被切一半"的植物（丝瓜 / 鱼腥草），再问"切一半 / 全切"。

        ★ 落点（规则 15.7）：实际的"切"发生在结算阶段【一切攻击之前】；
          这里只收集意图（与释放 / 目标 / 溅射同一个阶段 —— "声明意图"都在这儿）。
        ★★ 互挡【不在这一步声明】（用户 2026-09-28 原话："刀类只负责选择目标，
          互挡的发生在底层"）—— 玩家只选"切谁"，谁和谁同序列、要不要互挡，
          由引擎在 _cut_resolve_phase 里按序列自动配对。
        ★ 未释放的刀叶不参与（属性归零 = 它不切）。
        ★【v0.44】"切谁"允许答【不切】（None）—— 释放刀类不代表一定要选目标，
          可能只是想要它的防御值（用户 2026-10-12）。
        """
        cutters = [p for p in self.players
                   if p.alive and p in self.released
                   and is_knife_class(p.field.spec)]
        if not cutters:
            return
        self.say("—— 刀叶类：声明要消除谁 ——")
        hook_act = self._hook_act_map()
        for p in cutters:
            spec = p.field.spec
            cands = self._cut_candidates(p)
            if not cands:
                self.say("%s 的【%s】没有可切的目标" % (p.tag, spec.name))
                continue
            plan = self._ask_cut_plan(p, cands)
            if plan is None:
                self.say("%s 的【%s】声明：本回合不切任何人（保留防御）"
                         % (p.tag, spec.name))
            else:
                self.cut_plans[p] = plan
                self.say("%s 的【%s】声明：消除 %s %s"
                         % (p.tag, spec.name,
                            "、".join(t.tag for t in plan["targets"]),
                            self._cut_plan_text(plan)))
            # ★★【v0.72】勾取者的条件方案（"如果勾取成功，让它切谁"）——
            #   用户 2026-10-26："牛角菱会继承它勾取植物的【所有】轮询"。
            #   刀叶这四问原来只问原主，勾取者根本没被问过。
            #   兑现时由 _apply_cond 覆盖（没兑现就不生效）。
            hk = hook_act.get(p)
            if hk is None:
                continue
            cplan = self._ask_cut_plan(p, cands, by=hk, cond=True)
            self.cond_plans.setdefault((hk.idx, p.idx), {})["cut"] = cplan
            self.say("%s 声明：如果勾取成功 → %s"
                     % (hk.tag,
                        "不让它切任何人" if cplan is None else
                        "让它消除 %s %s"
                        % ("、".join(t.tag for t in cplan["targets"]),
                           self._cut_plan_text(cplan))))

    # ==================================================================
    # 【v0.38】电系：「勾走」与 [反馈湮灭]
    # ==================================================================
    def _absorb_declare_phase(self):
        """【v0.38】博弈阶段：有"收植物"能力的植物声明收谁。

        ★ 原稿 p20：`将[过载喇叭花][闪电草][猫尾草]收为己用，被勾走目标成[空]`。
          —— [辣椒] 是转写识别错误，用户 2026-09-30 确认为 [过载喇叭花]。
        ★★ 用户 2026-09-30 拍板：
          · 收人【在博弈阶段声明】（与刀叶同批，见 phase_bluff 的调用点）；
          · 收人者【决定被收植物的释放 / 目标 / 充能】——但
            【消耗的是原释放者的种子】（"所以一般会充满"）；
          · 被收走的目标【成[空]】；
          · 时序上【收人在 [反馈湮灭] 之前】。
        ★ 可收名单 = 收人者 spec.absorb_targets（写在 57 号自己身上）。

        ★★【v0.70 改两处，都是用户 2026-10-26 报的】：
          ① **拆成两趟**：先让牛角菱[勾取]全问完，再问聚电蓝莓的[聚电]。
             否则同一趟循环里"谁被勾了"取决于座位顺序 —— 勾取者还没声明，
             被勾的那株就已经把聚电答完了。
          ② **被勾的聚电蓝莓由勾取者代答**（原来仍由原主人答）：
             用户原话"牛角菱会继承它勾取植物的所有轮询"。
        ★ 命名：57 的这个动作从此叫【聚电】（用户 2026-10-26："电系的勾取换个名字
          叫[聚电]吧，以便区分"）—— 与牛角菱的【勾取】是两个机制，日志必须分开。
        ★ 分配细节（同种植物不够分时的平均/随机）见 _distribute_absorbed，仍未落地。
        """
        # 第一趟：[勾取]（103 牛角菱）—— 勾【两株同名】
        for p in self.players:
            if not p.alive or p not in self.released or p.field is None:
                continue
            if p.field.spec.hook_same:
                self._hook_declare(p)

        # 第二趟：[聚电]（57 聚电蓝莓）—— ★【v0.71】一律【原主人自己答】
        #   v0.70 这里让勾取者代答，用户 2026-10-26 当场否掉：
        #     "谁让你代答了？这游戏什么时候允许你代答了？？
        #      谁能在结算之前保证你勾取/聚电一定能成功？"
        #   代答的毛病不只是"越权"：勾取一旦没兑现，被勾那株【连自己的方案都没有】
        #   —— 原主人的声明被勾取者顶掉了，而顶掉它的那个前提又不成立。
        proxies = self._hook_act_map()
        for p in self.players:
            if not p.alive or p not in self.released or p.field is None:
                continue
            spec = p.field.spec
            if spec.hook_same or not spec.absorb_targets:
                continue
            cands = self._absorb_candidates(p)
            if not cands:
                self.say("%s 的【%s】想聚电，但场上没有可聚电的目标"
                         % (p.tag, spec.name))
                continue
            ans = self.gather([self._q_absorb(p, cands)])
            picked = set(ans.get(p.idx) or [])
            targets = [c for c in cands if c.idx in picked]
            self.absorb_plans[p] = [c.field for c in targets]
            if targets:
                self.say("%s 的【%s】声明【聚电】：收走 %s"
                         "（被收者成[空]，其出手由 %s 决定）"
                         % (p.tag, spec.name,
                            "、".join(c.tag for c in targets), p.tag))
        # 第三趟【v0.71】：勾取者的【条件】聚电 —— "如果勾取成功，让它聚电谁"
        self._hook_cond_absorb()

    def _absorb_candidates(self, p):
        """【v0.38】勾人系统的候选：其余存活玩家，且其场上植物名在 absorb_targets 里。

        ★ 读 g.ring 语义上与 others_alive 一致（others_alive 已按 p.alive 过滤），
          这里用 others_alive 即可（它内部就是 players 过滤 alive）——
          但【不】手写 [x for x in players if x.alive]，以免将来漏掉"已摘掉"的状态。
        """
        spec = p.field.spec
        out = []
        for c in self.others_alive(p):
            f = c.field
            if f is None or f.spec is EMPTY:
                continue
            if f.spec.name in spec.absorb_targets:
                out.append(c)
        return out

    # ==================================================================
    # 【v0.57】[勾取]（103 牛角菱）—— 不是独立阶段，而是"每个阶段之前兑一次"
    # ==================================================================
    def _hook_candidates(self, p):
        """[勾取] 的候选：场上【同名 ≥ hook_same 株】的植物（不含自己场上的）。

        ★ 原稿 p09 `(2)控制两相同植物 收为己用` —— "两相同" = 两株【同名】。
        ★ 门槛按 spec.hook_same 判，引擎里不写 2 —— 将来换个"勾三株"的植物
          只改它自己那一行（v0.17 老规矩：数字留在植物身上）。
        """
        need = p.field.spec.hook_same
        groups = {}
        for c in self.others_alive(p):
            f = c.field
            if f is None or f.spec is EMPTY:
                continue
            groups.setdefault(f.spec.name, []).append(c)
        out = []
        for name in sorted(groups):
            if len(groups[name]) >= need:
                out.extend(groups[name])
        return out

    def _hook_declare(self, p):
        """[勾取] 声明 —— ★ 顺便当【形态二选一】那道题（少一套六端界面）。

        答案语义：
          · 选中 hook_same 株【同名】植物 → 勾取形态：防 = 基础防 + 所向×hook_def_per，
            本回合【不攻击】；
          · 一株都不选 / 场上没有够数的同名株 → 攻击形态（照常散连击）。
        ★ 为什么把"形态"折进"勾谁"里：形态二选一本来就没有独立的时机 ——
          它决定的正是"这一回合打不打人、勾不勾人"，而勾谁必须在这里选。
          多造一道 kind 就要多补六端界面 + PAYLOAD_CONTRACT（铁律），
          而行为上二者等价。
        """
        spec = p.field.spec
        cands = self._hook_candidates(p)
        if not cands:
            self.say("%s 的【%s】想勾取，但场上没有 %d 株同名的植物 → 只能走攻击形态"
                     % (p.tag, spec.name, spec.hook_same))
            return
        ans = self.gather([self._q_hook(p, cands)])
        picked = [c for c in cands if c.idx in set(ans[p.idx] or [])]
        names = {c.field.spec.name for c in picked}
        if len(picked) != spec.hook_same or len(names) != 1:
            if picked:
                self.say("%s 的【%s】勾取声明无效（必须恰好 %d 株【同名】）→ 改走攻击形态"
                         % (p.tag, spec.name, spec.hook_same))
            else:
                self.say("%s 的【%s】放弃勾取 → 走攻击形态" % (p.tag, spec.name))
            return
        n = len(picked)
        # ★★★【v0.70 更正 · 真 bug】"勾到 2 株只准 1 株出手"原来是【写死】的
        #   （spec.hook_act = 1），结果连不该受这条约束的植物也被砍掉了：
        #   用户 2026-10-26 实测——牛角菱勾走两株【聚电蓝莓】，日志说
        #   "只有 1 株能真正出手 → 成[空]"，用户："哪来的只能释放一株？两个都能放！！"
        #   真正该看的判据是【会不会触发[反馈湮灭]】（原稿 p09 那句
        #   "若场上仅有 2 个此类植物，反馈逻辑将不会触发，解发时无[反馈]产生"
        #   说的就是这件事；用户这次明确回答"看有没有反馈湮灭"）：
        #     · 带反馈属性的（闪电草 9）→ 两株都出手会一起湮灭成[空]，
        #       所以默认只放 1 株出手，保住一份攻击；
        #     · 不带反馈的（聚电蓝莓、猫尾草…）→ 两株【都出手】，各打各的。
        #   ★ 规则「牛头无敌」(hook_both) 仍是"强制全出手"（吃双份反馈值）。
        has_fb = any(has_feedback(f.spec) for f in [c.field for c in picked])
        if self.rules.get("hook_both"):
            act = n
        elif has_fb:
            act = max(1, spec.hook_act)
        else:
            act = n
        p.field.form = "hook"
        p.field.fang = spec.defense.fang + spec.hook_def_per * n
        p.field.bao = spec.hook_def_per * n
        self.hook_plans[p] = {"fields": [c.field for c in picked],
                              "act": max(1, act), "done": set()}
        # ★【v0.72】点名：说"只放 N 株出手"却不说是哪几株，读战报的人没法核对
        #   （用户 2026-10-27："凭什么黑洞花1启动成功了？"）。
        _act_n = self.hook_plans[p]["act"]
        _act_names = [c.tag for i, c in enumerate(picked) if i < _act_n]
        _rest_names = [c.tag for i, c in enumerate(picked) if i >= _act_n]
        self.say("%s 的【%s】选择【勾取形态】：勾取 %s（%d 株同名）→ 防 %d、本回合不攻击"
                 "（%s）"
                 % (p.tag, spec.name, "、".join(c.tag for c in picked), n,
                    p.field.fang,
                    "「牛头无敌」：强制全出手" if self.rules.get("hook_both")
                    else ("它们会触发[反馈湮灭]，默认只放 1 株"
                          if has_fb else "它们不触发[反馈湮灭]，全部出手")))
        self.say("  · 本回合出手：%s%s"
                 % ("、".join(_act_names),
                    "；其余成[空]、不出手：%s" % "、".join(_rest_names)
                    if _rest_names else ""))

    # ==========================================================
    # 【v0.71】"不许代答" —— 声明阶段两边都答，勾取【兑现】时才覆盖
    #
    #   用户 2026-10-26 原话：
    #     "谁让你代答了？这游戏什么时候允许你代答了？？
    #      谁能在结算之前保证你勾取/聚电一定能成功？"
    #   → 于是拆成两半：
    #       · 原主人照常答自己的（聚电谁 / 充几颗 / 打谁）—— 勾取失败就用这份；
    #       · 勾取者【额外】答一套"如果勾取成功"的条件方案，存进 cond_plans；
    #       · 结算阶段 _hook_pass 真把支配权转过去时，才把条件方案盖上去。
    #   ★ 顺序不能反：声明阶段就盖 = 代答（就是被否掉的那个）。
    # ==========================================================

    def _charge_report(self, p, spent):
        """充能播报里那个括号 —— 必须是【算出来的真值】。

        ★★【v0.71】原来一律用 spec.charge_line(spent)，而 57 聚电蓝莓没有
          自己的 charge_text，走的是兜底文案"防御力%d" % spent ——
          可它走的是【分段阈值】：X=25 → 实际 暴15·防20。
          于是战报写着"充能25颗种子（防御力25）"，而场面显示"防20"：
          ★ 同一次充能，两个数字。用户 2026-10-26 那句"聚电蓝莓没有减防御"
            就是照着前一个数字对不上下一个数字。
        """
        spec = p.field.spec
        if spec.threshold_charge is not None:
            f = p.field
            return "分段充能：暴%d·防%d%s" % (
                f.bao, f.fang, "，本回合带[闪电一击]" if f.flash_chain else "")
        return spec.charge_line(spent)

    def _do_charge(self, p, spent):
        """真正落一次充能：扣种 + 改数值 + 记账（从 phase_bluff 里抽出来的）。

        ★★ 为什么要抽出来：v0.71 起勾取者的"条件充能"要在【结算阶段】重做一次
          （先把原主人那份退回去，再按新数充）。两处必须走同一段代码 ——
          抄一份就会有一处忘了改 charge_spent，然后"充了多少"两处对不上。
        ★ 顺序：先算数值、后播报 —— 播报里要念的是算出来的真值。
        """
        spec = p.field.spec
        overflow = spent > p.seeds
        if overflow:
            spent = 0
        elif spent > 0:
            p.seeds -= spent
        # 【v0.38】★ 分段阈值充能（57 聚电蓝莓）：效果走 spec 自己的分段表。
        if spec.threshold_charge is not None:
            fang, bao, flash = self._threshold_charge_effect(spec, spent)
            p.field.fang = fang
            p.field.bao = bao
            # ★ flash 是"本回合临时带[闪电一击]"——不改 spec（spec 是共享模板）
            p.field.flash_chain = flash
        else:
            p.field.fang = spent
            if spec.charge_bao is not None:
                p.field.bao = spec.attack.bao + spent + spec.charge_bao
        # 【v0.32】记下"本回合充了多少"（必须在写 fang 之后，且不随攻击削减）
        p.field.charge_spent = spent
        if spec.charge_tag and spent > 0:
            p.field.tag_seg = spent
        # 【v0.17】文案一律问植物自己（spec.charge_line）——
        #   以前这里写死了大喷菇的"攻击力=充能数+1"，人参果也被套上。
        if overflow:
            self.say("%s 的【%s】宣布充能%d颗，但只有%d颗种子，溢出 → 视为没有充能（%s）"
                     % (p.tag, spec.name, spent, p.seeds, self._charge_report(p, 0)))
        elif spent > 0:
            self.say("%s 的【%s】充能%d颗种子（%s）"
                     % (p.tag, spec.name, spent, self._charge_report(p, spent)))
        else:
            self.say("%s 的【%s】未充能（%s）"
                     % (p.tag, spec.name, self._charge_report(p, 0)))
            p.field.tag_seg = spent

    def _cond_target_text(self, owner, kind, v):
        """条件目标的人名串（只用于播报，不做任何判定）。"""
        cands = self.others_alive(owner)
        if kind == "single":
            t = next((c for c in cands if c.idx == v), None)
            return t.tag if t else "（未选）"
        if kind == "spread":
            picked = [c for c in cands if isinstance(v, list) and c.idx in v]
            return "、".join(c.tag for c in picked) or "（未选）"
        seq = []
        for tidx, c in (v or []):
            t = next((x for x in cands if x.idx == tidx), None)
            if t is not None and c > 0:
                seq.append("%s×%d" % (t.tag, c))
        return "、".join(seq) or "（未选）"

    def _hook_cond_charge(self):
        """【v0.71】勾取者答："如果勾取成功，让它充几颗"（耗的仍是被勾者的种子）。"""
        items = []
        for owner, hooker in self._hook_act_map().items():
            if owner.field.spec.charge:
                items.append((owner, hooker,
                              self._q_charge(owner, by=hooker, cond=True)))
        if not items:
            return
        buckets = {}
        for it in items:
            buckets.setdefault(it[1], []).append(it)
        while any(buckets.values()):
            batch = [bk.pop(0) for bk in buckets.values() if bk]
            got = self.gather([b[2] for b in batch])
            for owner, hooker, _q in batch:
                v = got.get(hooker.idx)
                if not isinstance(v, int) or isinstance(v, bool):
                    v = 0
                self.cond_plans.setdefault((hooker.idx, owner.idx), {})["charge"] = v
                self.say("%s 声明：如果勾取成功 → 让 %s 的【%s】充能 %d 颗"
                         "（扣的是 %s 的种子）"
                         % (hooker.tag, owner.tag, owner.field.spec.name, v,
                            owner.tag))

    def _hook_cond_absorb(self):
        """【v0.71】勾取者答："如果勾取成功，让它聚电谁"。"""
        items = []
        for owner, hooker in self._hook_act_map().items():
            spec = owner.field.spec
            if not spec.absorb_targets:
                continue
            cands = self._absorb_candidates(owner)
            if not cands:
                continue
            items.append((owner, hooker, cands,
                          self._q_absorb(owner, cands, by=hooker, cond=True)))
        if not items:
            return
        buckets = {}
        for it in items:
            buckets.setdefault(it[1], []).append(it)
        while any(buckets.values()):
            batch = [bk.pop(0) for bk in buckets.values() if bk]
            got = self.gather([b[3] for b in batch])
            for owner, hooker, cands, _q in batch:
                picked = set(got.get(hooker.idx) or [])
                fields = [c.field for c in cands if c.idx in picked]
                self.cond_plans.setdefault((hooker.idx, owner.idx), {})["absorb"] = fields
                self.say("%s 声明：如果勾取成功 → 让 %s 的【%s】%s"
                         % (hooker.tag, owner.tag, owner.field.spec.name,
                            ("聚电 %s" % "、".join(f.player.tag for f in fields))
                            if fields else "不聚电"))

    def _cond_target_players(self, owner, hooker):
        """【v0.72】把勾取者的"如果勾取成功打谁"解析成 Player 列表。

        ★ 为什么需要：**闪击的两侧要按勾取者选的目标算** —— 原主人那个目标在
          勾取兑现后就作废了，用它算出来的两侧是错的（用户 2026-10-27 这局：
          勾取者被允许"弹向目标本人"，然后结算时判声明失效，等于白问一次）。
        ★ 解析规则必须与 _apply_hook_targets 完全一致，所以只此一处。
          返回 None = 勾取者压根没答这份。
        """
        cond = self.cond_plans.get((hooker.idx, owner.idx)) or {}
        if "target" not in cond:
            return None
        kind, v = cond["target"]
        cands = self.others_alive(hooker)
        if not cands:
            return []
        if kind == "single":
            t = next((c for c in cands if c.idx == v), cands[0])
            return [t]
        if kind == "spread":
            return [c for c in cands if isinstance(v, list) and c.idx in v]
        idxmap = {c.idx: c for c in cands}
        tg = []
        for tidx, c in (v or []):
            t = idxmap.get(tidx)
            if t is None or c <= 0:
                continue
            tg += [t] * c
        return tg or [cands[0]]

    def _apply_cond(self, owner, hooker):
        """【v0.71】勾取【兑现】了 → 把勾取者那套条件方案盖上去。"""
        cond = self.cond_plans.get((hooker.idx, owner.idx))
        if not cond:
            return
        if "charge" in cond:
            self._apply_cond_charge(owner, cond["charge"])
        if "absorb" in cond:
            self.absorb_plans[owner] = list(cond["absorb"])
            self.say("  ◇ 勾取兑现 → %s 的【%s】改按 %s 的方案：%s"
                     % (owner.tag, owner.field.spec.name, hooker.tag,
                        ("聚电 %s" % "、".join(f.player.tag for f in cond["absorb"]))
                        if cond["absorb"] else "不聚电"))
            # 换了一批"要收谁" → 抢株分配必须重算（否则可能两人都记着同一株）
            self._distribute_absorbed()
        if "target" in cond:
            kind, v = cond["target"]
            self._apply_hook_targets([(owner, hooker, kind, v)])
        if "flash" in cond:
            # 【v0.72】条件充能把它推到了第二段 → 闪电链按勾取者选的那一侧
            k, side = cond["flash"]
            self.flash_plans[(owner.idx, k)] = side
            self.say("  ◇ 勾取兑现 → 闪电一击改按 %s 的选择：弹向 %s"
                     % (hooker.tag, side.tag))
        # ★★【v0.72】剩下四种"轮询"也继承 —— 用户 2026-10-26 原话：
        #   "牛角菱会继承它勾取植物的【所有】轮询"。原来只有
        #   充能 / 聚电谁 / 打谁 / 链弹哪一侧 四问有代答，刀叶 / 猫尾草 /
        #   番茄 / 复活 全是原主答的（勾取者根本没被问）。
        if "cut" in cond:
            _cp = cond["cut"]
            if _cp is None:
                self.cut_plans.pop(owner, None)
                self.say("  ◇ 勾取兑现 → 【%s】改成不切任何人" % owner.field.spec.name)
            else:
                self.cut_plans[owner] = dict(_cp)
                self.say("  ◇ 勾取兑现 → 【%s】改按 %s 的选择：消除 %s"
                         % (owner.field.spec.name, hooker.tag,
                            "、".join(t.tag for t in _cp["targets"]) or "（无）"))
        if "cat" in cond:
            self.plans[owner] = list(cond["cat"])
            self.say("  ◇ 勾取兑现 → 【%s】的连击改按 %s 的分配：%s"
                     % (owner.field.spec.name, hooker.tag,
                        "、".join(t.tag for t in cond["cat"]) or "（无）"))
        if "friend" in cond:
            self.friend_plans[owner] = dict(cond["friend"])
            _ft = cond["friend"].get("target")
            self.say("  ◇ 勾取兑现 → 【%s】的连接改按 %s 的选择：%s"
                     % (owner.field.spec.name, hooker.tag,
                        ("连 %s" % _ft.tag) if _ft is not None else "空目标（不连人）"))

    def _apply_cond_charge(self, owner, spent):
        """【v0.71】按勾取者的条件值【重做】一次充能：先把原主人那份退回去。

        ★ 为什么安全：pre_charge 是充能【之前】的 (fang, bao, flash_chain) 快照，
          而充能之后到结算之间没有任何一步会改这三个值。
        """
        f = owner.field
        pre = getattr(f, "pre_charge", None)
        if pre is None:
            return
        owner.seeds += int(getattr(f, "charge_spent", 0) or 0)
        f.fang, f.bao, f.flash_chain = pre
        self._do_charge(owner, spent)
        self.say("  ◇ 充能按勾取者的方案重做 → %d 颗（退回去再按新数扣）" % spent)

    def _first_action_stage(self, f):
        """这株植物【最早在哪一步真的做事】—— 只给 _hook_pass 用。

        ★★【v0.72】为什么不能再拿 `_stage_of` 当判据：
          `_stage_of` 回答的是"它的【主】步骤是哪一步"（[反馈湮灭]要按主步骤
          分步，所以那边不能动）。可一株植物常常在好几步里都做事 ——
          闪电草第 8 步【防御收益】发[超防]、第 12 步才攻击；`_stage_of`
          只看得到后面的攻击步 → [勾取] 拖到第 12 步才兑现，而第 8 步的
          超防已经记在原主账上了（用户 2026-10-27："为什么真人2 3 会获得超防"）。
        ★★ 判据顺序 = `phase_settle` 的步序，取【最早】那一个；两边必须一起改。
        """
        s = f.spec
        if getattr(s, "destroy_all", False):
            return "destroy"
        if getattr(s, "knife", False):
            return "cut"
        if getattr(s, "revive_one", False):
            return "revive"
        if getattr(s, "heal", 0) > 0:
            return "heal"
        if getattr(s, "link_friend", False):
            return "friend"
        if getattr(s, "absorb_targets", ()) or getattr(s, "hook_same", 0):
            return "absorb"
        if getattr(s, "water_gain", 0):
            return "assist"
        if getattr(s, "super_def", 0) or getattr(s, "immune_turns", 0):
            return "defense"
        if getattr(s, "fire_gain", 0):
            return "fire"
        if getattr(s, "mult_gain", 0) or getattr(s, "fishy_gain", 0):
            return "layer"
        if s.attack.is_devour:
            return "devour"
        if is_reward_class(s) or getattr(s, "seed_gain", 0):
            return "reward"
        if s.attack.attr != ATTR_NONE:
            return "attack"
        if getattr(s, "charge_tag", False):
            return "burst"
        if getattr(s, "nearest_target", False):
            return "cat"
        return None

    def _holder(self, p):
        """这株植物产出的收益该记在【谁】账上 = 它现在的支配者。

        没被[勾取]就是自己；被勾了就是勾取者（多级则一路追到最终支配者）。
        ★ 用户 2026-10-27 的总口径：**植物转移了，收益跟着走** ——
          [毁灭] 的 ⌊2m/n⌋、[反馈湮灭] 的 9 颗种 + [超防] 都是这么改的，
          加血 / [免疫] / 得种 / 水滴 同样不能漏。
        """
        # ★★ 两条路都要看：
        #   ① `_final_controller`（按"本回合真会出手"的勾取表推）——
        #      它是 v0.71 给"打谁"用的，勾取一旦兑现就有效；
        #   ② **已经兑现的 `hooked_by` / `absorbed_by`** —— 这一条是必须的：
        #      原主已经出局时，`_hook_act_map()` 会把它过滤掉（它要求 owner.alive），
        #      于是 `_final_controller` 退回它自己，收益又落回死人账上
        #      （实测：p2 被打死后它那株食人花仍照 v0.70"死了也能出手"，
        #        夺来的 9 颗种和一口回血全进了 p2 的口袋）。
        seen = set()
        cur = p
        while cur is not None and cur.idx not in seen:
            seen.add(cur.idx)
            nxt = None
            f = cur.field
            if f is not None:
                nxt = getattr(f, "hooked_by", None)                     or getattr(f, "absorbed_by", None)
            if nxt is None:
                nxt = self._final_controller(cur)
            if nxt is None or nxt is cur:
                break
            cur = nxt
        return cur if cur is not None else p

    def _stage_of(self, f):
        """这株植物【在哪一步结算】—— 只给 _feedback_pass 用。

        ⚠️⚠️ 这张判断是 phase_settle 的镜像：**两边必须一起改**。
          兜底在 _hook_pass(None)（结算末尾把没对上的统一收掉），
          所以就算这里漏了一种，也只会"晚一步转支配权"，不会把植物卡住 ——
          但漏掉会让"勾取刚好在它那一步之前生效"这条口径失真，别指望兜底。
        """
        s = f.spec
        if getattr(s, "destroy_all", False):
            return "destroy"
        if getattr(s, "knife", False):
            return "cut"
        if getattr(s, "revive_one", False):
            return "revive"
        if getattr(s, "heal", 0) > 0:
            return "heal"
        if getattr(s, "link_friend", False):
            return "friend"
        if getattr(s, "absorb_targets", ()) or getattr(s, "hook_same", 0):
            return "absorb"
        # 【2026-10-25】纯施加株（丝瓜 / 鱼腥草）：它们的主步骤就是【附加层】。
        #   ★ 只给"没有其它主步骤"的株用 —— 火苗花也施加，但它有攻击，
        #     主步骤必须是 attack（否则两株火苗花的 [反馈湮灭] 会挂错步）。
        if getattr(s, "mult_gain", 0) or getattr(s, "fishy_gain", 0):
            return "layer"
        if getattr(s, "nearest_target", False):
            return "cat"
        if s.attack.is_devour:
            return "devour"
        if is_reward_class(s):
            return "reward"
        if s.attack.attr != ATTR_NONE:
            return "attack"
        if getattr(s, "charge_tag", False):
            return "burst"
        return None

    def _hook_pass(self, stage=None):
        """[勾取] 兑现 —— 【每个结算阶段开始时】调一次（用户 2026-10-21 的口径）。

        ★★ 为什么不能只兑一次：被勾的植物【必须也能结算】（用户原话）。
          若像 57 的"勾走"那样在结算开头一次性把目标打成[空]，它就没有
          结算机会了 —— 那正是上一版的错。
        ★★ 为什么"提前已经选定了目标"就够了：勾谁在博弈阶段就定了，
          所以这里不问任何问题，只在"轮到它那一步【之前】"把支配权转过去
          （= 插在它那一步之前、上一步之后）。
        做的事：
          · 前 act 株：记 hooked_by = 勾取者，照常结算；它触发的[反馈湮灭]
            返种也算勾取者的；
          · 其余：成[空] —— 「勾到 2 株只能出手 1 株」就是这一句。
        ★ stage=None = 兜底：把还没兑的一律收掉。
        """
        if not self.hook_plans:
            return
        for hooker, plan in list(self.hook_plans.items()):
            if not hooker.alive or hooker.field is None:
                continue
            for i, f in enumerate(plan["fields"]):
                owner = f.player
                if owner.idx in plan["done"]:
                    continue
                # 已经被消除/毁灭/打死了 → 这一株不用再勾，也不该让整条卡住
                if not owner.alive or owner.field is not f \
                        or f.cut_gone or f.inert:
                    plan["done"].add(owner.idx)
                    continue
                # ★【v0.72】按【最早做事的那一步】兑现，不再按"主步骤"——
                #   否则闪电草的超防（第 8 步）会先记到原主头上，勾取到第 12 步才来。
                if stage is not None and self._first_action_stage(f) != stage:
                    continue
                plan["done"].add(owner.idx)
                if i < plan["act"]:
                    f.hooked_by = hooker
                    # ★【v0.71】到这里勾取才【真的兑现】—— 此刻才能把勾取者的
                    #   "如果勾取成功"方案盖上去（之前盖 = 代答，是错的）。
                    self._apply_cond(owner, hooker)
                    # ★★【v0.72 · 真 bug】勾取【也要成[空]】。
                    #   原稿 p09 白纸黑字：
                    #     `(2)控制两相同植物 收为己用，被夺走目标成[空]　所向X2　5＋所向X2`
                    #   v0.57~v0.71 只做了后半句"照常结算"，把前面"成[空]"漏了 ——
                    #   用户 2026-10-27 实测：两株聚电蓝莓被勾走后防 17 / 10 原样还在，
                    #   三株都挨了打却只死了闪电草。
                    #   ⚠️ 顺序不能反：_apply_cond 里的"条件充能"会把 fang 重新写回去，
                    #     所以必须在它【之后】才归零。
                    self._hollow_out(f, hooker)
                else:
                    self._void_one(owner, "勾取")
                    # ★【v0.72】措辞统一成【勾取】（原来这行写"勾走"，
                    #   和出手那一株的"勾取"并排出现，用户直接问"为什么一个勾取一个勾走"）。
                    self.say("  ◇ %s 的【%s】被 %s 勾取 → 成[空]、本回合不出手"
                             "（勾到 %d 株、本回合只放 %d 株出手）"
                             % (owner.tag, f.spec.name, hooker.tag,
                                len(plan["fields"]), plan["act"]))

    def _hook_proxies(self):
        """【v0.58】{被勾者的持有者: 勾取者} —— 【所有】被勾的植物。

        ★ 用途一：这些人【本批不问目标】—— 反正他们要按勾取者的决定走，
          或者干脆不出手（勾到 2 株只 1 株出手）。问了也是浪费一道题。
        ★ 用途二：其中"真会出手那几株"要由勾取者替他们答，见 _hook_target_questions。
        """
        out = {}
        for hooker, plan in self.hook_plans.items():
            if not hooker.alive or hooker.field is None:
                continue
            for f in plan["fields"]:
                owner = f.player
                if not owner.alive or owner.field is not f:
                    continue
                out[owner] = hooker
        return out

    def _hook_act_map(self):
        """【v0.70】{被勾者: 勾取者} —— 只含【本回合真会出手】的那几株。

        ★ 抽出来给三处共用：代答"打谁"（_hook_target_questions）、
          代答"聚电谁"（_absorb_declare_phase）、代答"充能几颗"（phase_bluff ②）。
          用户 2026-10-26 的口径："牛角菱会继承它勾取植物的所有轮询"——
          原来只有目标那一处做了代答，另外两处仍然让原主人自己答。
        """
        out = {}
        for hooker, plan in self.hook_plans.items():
            if not hooker.alive or hooker.field is None:
                continue
            for f in plan["fields"][:plan["act"]]:
                owner = f.player
                if owner.alive and owner.field is f:
                    out[owner] = hooker
        return out

    def _hook_target_questions(self):
        """替【真会出手】的被勾植物造"打谁"的题 —— 由勾取者答。

        ★ 用户 2026-10-22："声明那一步就要问'如果勾取成功，你要打谁'了"。
        ★ 与普通目标声明分两批下发，但都在【公告之前】：勾取者看不到别人的声明
          （v0.46 番茄那条踩过的同一个坑）。
        ★ 连击 / 散连击 / 单体三种形态各走各自的老构造器，只是把"谁来答"
          换成勾取者（`by=`），判定与候选仍按被勾的那一株算。

        ★★★【v0.70 修 · 一个勾取者要答多道题】
          v0.70 起"勾到 2 株不带反馈的植物 → 两株都出手"（按用户口径），
          于是同一个勾取者要替【两株】各答一道"打谁" —— 当场炸：
            gather() 的答案是按 `answers[q.player_idx]` 存的，同一玩家两道题
            **互相覆盖**；更早一步 dispatch 也只存一个 q（第二题把第一题冲掉），
            第二次 poll 直接 `RuntimeError: poll() 被调用，但没有待答的问题`。
          → 修法：按勾取者【轮转分批】，每批每个勾取者最多一道题。
            返回值因此改成"已经把答案落好"的形式：
              jobs = [(owner, hooker, kind, value), ...]
            （原来是 (jobs, ans) 两个值，ans 按玩家编号索引 —— 那个结构
              天生装不下"同一人多题"，所以整条链路一起换掉。）
        """
        items = []
        for owner, hooker in self._hook_act_map().items():
            atk = owner.field.spec.attack
            if atk.attr == ATTR_NONE:
                continue          # 它本来就不打人（黑洞之类），没什么可问的
            # ★★【v0.72】候选按【勾取者】算，不是按被勾的那一株算（用户 2026-10-27 截图实测）：
            #   勾取成功后这株归勾取者支配 —— 它【可以打原主人】（原主场上那张牌已经空了、
            #   0 防，正好打穿），但【不能打勾取者自己】。
            #   旧写法 `others_alive(owner)` 恰好反着来：把勾取者列进候选、把原主人剔掉 ——
            #   用户原话："牛角菱自己的攻击目标里居然有自己，但是没有聚电蓝莓"。
            cands = self.others_alive(hooker)
            if not cands:
                continue
            if atk.lian > 1:
                q = self._q_burst_split(owner, cands, atk.lian, by=hooker, cond=True)
                items.append((owner, hooker, "burst", q))
            elif atk.san > 1 and len(cands) > 1:
                items.append((owner, hooker, "spread",
                              self._q_targets(owner, by=hooker, cands=cands, cond=True)))
            else:
                items.append((owner, hooker, "single",
                              self._q_target(owner, by=hooker, cands=cands, cond=True)))
        if not items:
            return []

        buckets = {}
        for it in items:
            buckets.setdefault(it[1], []).append(it)
        jobs = []
        while any(buckets.values()):
            batch = [bk.pop(0) for bk in buckets.values() if bk]
            got = self.gather([b[3] for b in batch])
            for owner, hooker, kind, _q in batch:
                jobs.append((owner, hooker, kind, got.get(hooker.idx)))
        # 【v0.71】只登记，不落地 —— 勾取在结算阶段兑现时才盖上去（_apply_cond）
        for owner, hooker, kind, v in jobs:
            self.cond_plans.setdefault((hooker.idx, owner.idx), {})["target"] = (kind, v)
            self.say("%s 声明：如果勾取成功 → 让 %s 的【%s】打 %s"
                     % (hooker.tag, owner.tag, owner.field.spec.name,
                        self._cond_target_text(owner, kind, v)))
        return jobs

    def _apply_hook_targets(self, jobs):
        """把勾取者选的答案落到【被勾那株】的 plans 上，并按它的形态展开。

        ★【v0.70】jobs 现在自带答案（见 _hook_target_questions 的说明）。
        """
        for owner, hooker, kind, v in jobs:
            if not owner.alive or owner.field is None:
                continue
            # ★【v0.72】与 _hook_target_questions 必须用【同一份】候选，
            #   否则答案编号对不上（候选按勾取者算：不含他、含原主人）。
            cands = self.others_alive(hooker)
            spec = owner.field.spec
            if kind == "single":
                t = next((c for c in cands if c.idx == v), cands[0] if cands else None)
                if t is None:
                    continue
                self.plans[owner] = [t]
                self.say("%s 替 %s 的【%s】选目标 → %s（勾取成功就由它出手）"
                         % (hooker.tag, owner.tag, spec.name, t.tag))
            elif kind == "spread":
                picked = [c for c in cands if isinstance(v, list) and c.idx in v]
                if not picked:
                    continue
                self.plans[owner] = picked
                self.say("%s 替 %s 的【%s】选目标 → 散连击 %d 人：%s"
                         % (hooker.tag, owner.tag, spec.name, len(picked),
                            "、".join(t.tag for t in picked)))
            else:                                   # burst（连击发数分配）
                by = {c.idx: c for c in cands}
                seq, tg = [], []
                for tidx, c in (v or []):
                    t = by.get(tidx)
                    if t is None or c <= 0:
                        continue
                    if t.idx not in [x.idx for x in seq]:
                        seq.append(t)
                    tg += [t] * c
                if not tg and cands:
                    tg = [cands[0]]
                if not tg:
                    continue
                self.plans[owner] = tg
                self.say("%s 替 %s 的【%s】分配 %d 发 → %s"
                         % (hooker.tag, owner.tag, spec.name, len(tg),
                            "、".join(t.tag for t in seq)))

    def _hollow_out(self, f, by):
        """【v0.72】被[勾取]的株 → 成[空]（原稿 p09"被夺走目标成[空]"）。

        ★★ 与 [聚电] 的"勾走"【不是】同一件事，别照抄：
          · [聚电] 勾走 = 目标被收走 → 成[空]【且不再结算自己】（inert + 摘 released）；
          · [勾取] 勾走 = 支配权转移 → 成[空]【但照常结算】
            （规则文档 16.12："被勾者照常结算（这才是"收为己用"）"，
             Test110 用两株黑洞钉住：被勾的那株仍会[毁灭]全场）。
        ★ 所以这里【只清防御 / 辅助防】：
          · 防御归零 → 后面任何一击都直接打穿；
          · 不碰 inert / bao / released → 它那一击、那一步效果全都照常。
        ★ 待确认（用户 2026-10-27 提出的口径 vs 执行）：p30 原文写的是
          `[空]：虚无状态，0防0暴`（→ 暴击也该归零），但规则文档第 3928 行又写
          "成[空] = 本回合彻底作废、不再出手"，而 16.12/Test110 钉住的是
          "被勾者照常结算"。三条口径在"成[空] 还出不出手"上互相排斥，
          本样例里三株恰好全成[空] → 选哪条直接决定"零死亡 / 三人全死"。
          拍板前保持现状（防御归零、暴击保留），代码与测试八道全绿。
        ★ 别把"成[空]"顺手做成 to_empty()/stand_down()：那两个会连结算一起废掉。
        """
        f.hollow = True
        f.fang = 0
        f.fang_aux = 0
        # ★★ 只清【防御】，不碰 inert / bao / released —— 见方法头的说明。
        # 【v0.70 修】"它触发的[反馈湮灭]返种归 X"原来【无条件】拼上去 ——
        #   用户实测："聚电蓝莓哪来的反馈湮灭？？"（feedback_value = 0）。
        # ★ 措辞要点（用户 2026-10-27）：勾取是【转移】不是【消灭】——
        #   原主人场上那张牌空了（0 防、挡不住），但植物本身还在，照常结算，
        #   暴击跟着它走。这跟刀叶[废除]的"成[空]"（植物消失、连出手一起废掉）
        #   是两回事，战报里必须能一眼看出是哪一种。
        self.say("  ◇ %s 的【%s】被 %s 勾取 → 成[空]（原主场上那张牌空了：0 防、挡不住；"
                 "植物没有消失、只是转移到 %s 名下 → 本回合照常结算，收益归支配者）%s"
                 % (f.player.tag, f.spec.name, by.tag, by.tag,
                    "（它若触发[反馈湮灭]，返种归 %s）" % by.tag
                    if has_feedback(f.spec) else ""))

    @staticmethod
    def _is_empty_field(f):
        """【v0.72】这一株是不是已经成[空] / 不存在了。

        ★ 三个来路（用户 2026-10-22 的定性：**成[空] = 不存在**）：
          · cut_gone —— 被消除 / 被[毁灭] / 被[废除]；
          · absorbed —— 被[聚电]收走；
          · hollow   —— 被[勾取]夺走（原稿 p09"被夺走目标成[空]"）。
        ★ 【不许】把"主动不释放"算进来：那只把属性归零，牌还在、人还在，
          场面写的是"未释放·属性归零"，不是"成[空]"。
        """
        return bool(getattr(f, "cut_gone", False)
                    or getattr(f, "absorbed", False)
                    or getattr(f, "hollow", False))

    def _final_controller(self, p):
        """【v0.70】这株植物【最终】归谁支配：勾取 → 聚电 → … 一路往上。

        ★ 用户 2026-10-26 的四人局是两层链：
            1号 牛角菱 ─[勾取]→ 3/4号 聚电蓝莓 ─[聚电]→ 2号 闪电草
          所以闪电草这一击该由 1号 决定，不是 3/4号、更不是 2号。
        ★ 带环保护：理论上勾不成环，但结构一旦写错就会死循环，
          这里直接防住（seen）。
        """
        hooks = self._hook_act_map()
        seen = set()
        cur = p
        while cur is not None and cur.idx not in seen:
            seen.add(cur.idx)
            nxt = hooks.get(cur)
            if nxt is None or nxt is cur:
                break
            cur = nxt
        return cur

    def _absorb_effective_plans(self):
        """【v0.72】"这一回合谁想聚电谁" —— 原主人自己那份 + 勾取者的条件方案。

        返回 {caster: [FieldPlant, ...]}，供【两处】共用：
          · _absorbed_target_questions（收来之后打谁）
          · _flash_declare_phase（收来的那一发，链弹向哪一侧）
        ★ 不合并的话两处会各写一份"乐观合并"，然后某天只改了一处。

        ★【v0.71】为什么必须合并：被[勾取]的那几株，声明阶段真会落地的是
          【勾取者的条件方案】（_apply_cond 在结算时才盖上去）。只看 absorb_plans
          （原主人自己那份）的话，被勾的聚电蓝莓的表是空的 → "收为己用打谁"
          这道题根本不会被问出来。
          ⚠️ 是"乐观"：勾取万一没兑现，这份会作废，落回原主人那份。
        """
        eff = {}
        for caster, fields in self.absorb_plans.items():
            eff[caster] = list(fields)
        for (_hk, ow), cond in self.cond_plans.items():
            if "absorb" not in cond:
                continue
            _ow = next((p for p in self.players if p.idx == ow), None)
            if _ow is not None:
                eff[_ow] = list(cond["absorb"])
        return eff

    def _absorbed_target_questions(self):
        """【v0.70 建 / v0.72 重写】被[聚电]收走那株的"打谁" —— 【每一方各答一份】。

        ★★ 用户 2026-10-27（第四次）报的就是这里：
          "聚电蓝莓没有收到[如果聚电成功，你聚电的来自xx的闪电草要打哪里]，
           只有牛角菱收到了"，并点破了根因：
          "博弈阶段居然就决定闪电草归属了，[聚电]这一行为肯定在结算阶段才生效"。
        ★ 旧写法两个错：
          ① 只问 `_final_controller(caster)` 一个人 —— 而它在博弈期就把"被勾取"
             算进了支配链（支配者 = 勾取者），于是【原主自己那份】没人问；
             两株抢同一株时还会被 `_seen`（按 id(f)）去重成 1 道 —— 用户这局正是
             "两株聚电蓝莓都想要那 1 株闪电草"。
          ② 答案直接写进 `self.plans[被收那株的主人]`，把"已经收成了"当前提。

        ★ 现在的口径（与"聚电谁 / 充能几颗"完全一致：**两边都答、兑现才覆盖**）：
          · 每个声明[聚电]的人各答一份："如果[聚电]成功 —— 你聚电的 X 的【Y】打谁"；
          · 若这人自己被[勾取]，勾取者另答一份条件方案；
          · 答案只记进 `absorb_target_plans[(回答者编号, id(被收那株))]`；
            结算时【谁真正收到】就用谁那份（见 _absorb_resolve_phase）。
        ★ 返回 [(被收的 field, 回答者, kind, 答案, 候选)]，交给 _apply_absorbed_targets 记账。
        """
        items = []      # (field, 回答者, 是否条件方案, 攻击数据, 候选)
        seen = set()

        def _add(f, answerer, cond, cands):
            key = (answerer.idx, id(f))
            if key in seen:
                return
            seen.add(key)
            items.append((f, answerer, cond, f.spec.attack, cands))

        # ① 原主人自己那份（不许代答：勾取万一没兑现，落地的就是这份）
        for caster, fields in self.absorb_plans.items():
            if not caster.alive or caster.field is None:
                continue
            for f in fields:
                if f.player is caster or f.cut_gone or f.spec is EMPTY:
                    continue
                if f.spec.attack.attr == ATTR_NONE:
                    continue
                cands = self.others_alive(caster)
                if cands:
                    _add(f, caster, False, cands)

        # ② 勾取者的条件方案（"如果勾取成功，让它打谁"）
        for (hk, _ow), cond in self.cond_plans.items():
            if "absorb" not in cond:
                continue
            hooker = next((p for p in self.players if p.idx == hk), None)
            if hooker is None or not hooker.alive or hooker.field is None:
                continue
            for f in cond["absorb"]:
                if f.spec is EMPTY or f.cut_gone or f.player is hooker:
                    continue
                if f.spec.attack.attr == ATTR_NONE:
                    continue
                cands = self.others_alive(hooker)
                if cands:
                    _add(f, hooker, True, cands)

        if not items:
            return []

        qs, kinds = [], []
        for f, answerer, cond, atk, cands in items:
            # ★ 一律 cond=True：这本来就是"如果[聚电]成功"的条件方案
            #   （原主自己那份也一样 —— 它可能被抢走、被消除）。
            if atk.lian > 1:
                qs.append(self._q_burst_split(f.player, cands, atk.lian,
                                              by=answerer, cond=True, verb="聚电"))
                kinds.append("burst")
            elif atk.san > 1 and len(cands) > 1:
                qs.append(self._q_targets(f.player, by=answerer, cands=cands,
                                          cond=True, verb="聚电"))
                kinds.append("spread")
            else:
                qs.append(self._q_target(f.player, by=answerer, cands=cands,
                                         cond=True, verb="聚电"))
                kinds.append("single")
        # ★ 同一个人可能替好几株答（或两个人抢同一株各答一份）→ 一人一批一题
        ans = self.gather_by_batches(qs)
        jobs = []
        for i, (f, answerer, _cond, _atk, cands) in enumerate(items):
            jobs.append((f, answerer, kinds[i], ans.get(i), cands))
        return jobs

    def _feedback_pass(self, stage):
        """【v0.58】[反馈湮灭] —— 与[勾取]同一个形状：**每一步开始前判一次**。

        ★★ 为什么不再是独立的一段（用户 2026-10-22）：
          "把反馈和勾取一样，放在某个植物其它效果结算之前结算"。
          于是它不再是 ④ 那个"全局扫一遍"的阶段，而是紧贴每一株的结算：
          轮到哪一步，就把【这一步要结算的、带反馈属性的】植物集中看一次。
        ★★ 为什么是【同批】而不是逐株：同一名的植物必然落在同一步，
          批内逐株判的话第 1 株一反馈就成[空]，第 2 株只看得见自己 → 不反馈，
          「牛头无敌」承诺的 9+9=18 永远凑不出来。同批 = 都看到 2 株。
        ★ 计数池 `_feedback_live()`：**成[空] = 不存在**，不许数进去。
        ★★ 番茄（107）**也在这里排除，但不是因为它"可选"** —— 它的反馈同样是
          强制的（用户 2026-10-23 纠正）。排除的原因是【触发形状不同】：
          这里数的是"同一步里同名 2 株"，番茄数的是"场上只剩 2 个连通分图、
          且这一连会把它们并成一个"。所以它的强制触发落在
          `_friend_resolve_phase()` ② 里（那里才知道目标是谁）——判据看 link_friend。
        """
        groups = {}
        for p in self._feedback_live():
            if p.field.spec.link_friend:
                continue
            if self._stage_of(p.field) != stage:
                continue
            groups.setdefault(p.field.spec.name, []).append(p)
        for name, ps in groups.items():
            if len(ps) < 2:
                continue
            fv = ps[0].field.spec.feedback_value
            if not fv:
                continue
            self.say("  ◆ %d 株【%s】同时结算 → [反馈湮灭]（反馈值 %d）"
                     % (len(ps), name, fv))
            for p in ps:
                self._annihilate(p, p.field, "解时还有 %d 株同名" % len(ps))

    def _apply_absorbed_targets(self, jobs):
        """把"如果[聚电]成功，收来的这株打谁"记进 absorb_target_plans（**不写 plans**）。

        ★★【v0.72】旧版这里直接 `self.plans[被收那株的主人] = [...]`，
          等于把"已经收成了"当成前提，播报也因此写成「X 支配的【Y】→ 打 Z」
          （没有"如果"）。用户 2026-10-27 原话："[聚电]这一行为肯定在
          结算阶段才生效" —— 所以这里只记账，落地交给 _absorb_resolve_phase。
        """
        for f, answerer, kind, v, cands in jobs:
            text = ""
            if kind == "single":
                t = next((c for c in cands if c.idx == v),
                         cands[0] if cands else None)
                if t is None:
                    continue
                tg = [t]
                text = "打 %s" % t.tag
            elif kind == "spread":
                tg = [c for c in cands if isinstance(v, list) and c.idx in v]
                if not tg:
                    continue
                text = ("散连击 %d 人：%s"
                        % (len(tg), "、".join(t.tag for t in tg)))
            else:
                idxmap = {c.idx: c for c in cands}
                tg, seq = [], []
                for tidx, c in (v or []):
                    t = idxmap.get(tidx)
                    if t is None or c <= 0:
                        continue
                    if t.idx not in [x.idx for x in seq]:
                        seq.append(t)
                    tg += [t] * c
                if not tg and cands:
                    tg, seq = [cands[0]], [cands[0]]
                if not tg:
                    continue
                if len(seq) == 1:
                    text = "连击 %d 发全部打 %s" % (len(tg), seq[0].tag)
                else:
                    text = ("连击 %d 发分配：%s"
                            % (len(tg),
                               "、".join("%s %d发" % (t.tag, tg.count(t))
                                        for t in seq)))
            self.absorb_target_plans[(answerer.idx, id(f))] = (list(tg), text)
            self.say("%s 声明：如果[聚电]成功 → 收来的【%s】%s"
                     % (answerer.tag, f.spec.name, text))

    def _absorb_resolve_phase(self):
        """【v0.38】结算阶段：把本回合声明的"勾"兑现。

        ★★ 落点（用户 2026-09-30："勾人在触发之前"）：
          排在【[反馈湮灭] 之前】、且两者都在【一切攻击之前】——
          这样"被勾走的植物成[空]"能立刻影响后面的攻击判定。

        做的事（顺序是规则的一部分）：
          ① 被勾走的目标【成[空]】（to_empty 的语义 + 归勾人者支配）；
          ② 勾人者接手它的【释放 / 充能 / 目标】—— 本轮实现取"自动释放"，
             目标由勾人者后续在正常攻击声明里指定（因为它成了勾人者场上的株）；
             ⚠️ 简化口径：被勾者转成勾人者的"临时植物"，其花费用原主种子。
        ★ 留扩展性：分配规则（平均分 / 随机补）集中在 _distribute_absorbed。
        """
        # ★★【v0.72】归属【在这一步才定】（原来在博弈阶段就定了）。
        #   用户 2026-10-27："博弈阶段居然就决定闪电草归属了，
        #   [聚电]这一行为肯定在结算阶段才生效"。
        self.absorbed_attacks = []
        self._dist_logged = set()      # 本回合的"随机分配"只念一遍（_hook_pass 还会调一次）
        if not self.absorb_plans:
            return
        self._distribute_absorbed()
        self.say("—— 电系：【聚电】收为己用（排在 [反馈湮灭] 与一切攻击之前）——")
        for owner, fields in self.absorb_plans.items():
            for f in fields:
                if f.player is owner:
                    continue
                # ① 被勾者成[空]（防御/暴击归零、效果失效，但人还在）
                # ★★【v0.70】成空【之前】把它的攻击数值存一份 ——
                #   "收为己用"要用它打出去（见 phase_settle 的代理攻击）。
                #   不存的话闪电草打出去是"削防0"：成空把 bao 也清零了,
                #   而那一击本该按它自己的攻击力算（暴 7）。
                #   只存第一次（同一株被两家抢时，别被第二次的清零覆盖）。
                if not getattr(f, "absorbed", False):
                    f.bak_bao = f.bao
                    f.bak_fang = f.fang
                f.fang = 0
                f.fang_aux = 0
                f.bao = 0
                f.inert = True
                f.absorbed = True
                f.absorbed_by = owner
                # （"链弹向哪一侧"的落地挪到下面 _boss 算出来之后 —— 它要先知道
                #   真正支配这一击的是谁，才能挑那一份答案。）
                # ★★【v0.70】必须从 released 摘掉 —— 这是 _appliers() 的唯一判据。
                #   不摘的后果（用户 2026-10-26 的四人局实测）：
                #   闪电草明明"成[空]"了，却还结算了【超防】（战报里
                #   "player2 的【闪电草】→ 获得【超防】"），因为
                #   _appliers 只认"已释放 ∧ 场上还有这一株"。
                # ⚠️ 但【故意不】用 _void_one()：它会 plans.pop(target)，
                #   而这一击要留给[聚电]的"收为己用"（见 phase_settle 的代理攻击）。
                #   cut_plans 照清 —— 被收走的植物不可能再切人。
                self.released.discard(f.player)
                self.cut_plans.pop(f.player, None)
                # ★【v0.71】这里原来写的是"改由 owner 决定"—— 但 owner 自己可能
                #   也被别人[勾取]了（用户这局：真人2 的聚电蓝莓被真人1 勾取，
                #   它再聚电闪电草 → 闪电草该归真人1，不是真人2）。
                #   支配链要一路往上追（_final_controller），否则日志自相矛盾。
                _boss = self._final_controller(owner)
                self.say("  · %s 的【%s】被 %s 聚电收走 → 成[空]（该植物的出手改由 %s 决定%s）"
                         % (f.player.tag, f.spec.name, owner.tag, _boss.tag,
                            "" if _boss is owner else "（%s 已被 %s 支配，一路追到最终支配者）"
                            % (owner.tag, _boss.tag)))
                # ★★【v0.72】"收为己用"打谁 —— 到这一步才定：
                #   · 谁真收到它（owner）→ 用他声明阶段答的那份；
                #   · 他若自己也被[勾取]了（_boss 不是他）→ 用 _boss 那份条件方案；
                #   · 两份都没有（声明时它还没被声明聚电？）→ 用它原来的目标。
                _plan = (self.absorb_target_plans.get((_boss.idx, id(f)))
                         or self.absorb_target_plans.get((owner.idx, id(f))))
                if _plan and _plan[0]:
                    self.plans[f.player] = list(_plan[0])
                    self.say("  · 收为己用 → %s 支配的【%s】%s"
                             % (_boss.tag, f.spec.name, _plan[1]))
                    self.absorbed_attacks.append((f, _boss, "absorb", None, None))
                # ★★【v0.72 二修】聚电【真收成了】→ 把声明阶段答的
                #   "这一发命中后链弹向哪一侧"盖进 flash_plans。
                #   没兑现就不盖（那道题只是条件方案，见 _flash_declare_phase）。
                #   ★ 取法与上面 _plan 一字不差：先看最终支配者那份，再看原申请人
                #     自己那份 —— 两株抢同一株时，只有真收到的人那套生效。
                _fa = next((_ai for _ai in (_boss.idx, owner.idx)
                            if any(kk[0] == _ai and kk[1] == id(f)
                                   for kk in self.absorb_flash_plans)), None)
                if _fa is not None:
                    _who = next((pp for pp in self.players if pp.idx == _fa), None)
                    _n_side = 0
                    for (_ai, _fid, _k), _side in list(self.absorb_flash_plans.items()):
                        if _ai == _fa and _fid == id(f):
                            self.flash_plans[(f.player.idx, _k)] = _side
                            _n_side += 1
                    if _who is not None and _n_side:
                        self.say("  · 收为己用的这一发，闪电链按 %s 的选择（共 %d 发）"
                                 % (_who.tag, _n_side))
            # ② 勾人者随即"释放"被勾植物（原稿"收为己用"）——
            #    ★ 用户口径："所以一般会充满" → 被勾植物按【充满】处理，不改数值
            #      （它已成[空]，这里只是把"谁在支配它"记下来）。
            #    ⚠️ 本轮不把被勾植物的攻击并入勾人者的攻击序列（57 自己也攻击）——
            #      那一层要等"多株共存/多勾人者"落地时再做（见 desc 的留扩展性）。
        # 清表：本回合的勾已兑现
        #   ⚠️ 不清 self.absorb_plans 本身（战报/界面可能还要读），只标记已兑现。

    def _distribute_absorbed(self):
        """【v0.38 建占位 / v0.70 落地】多方声明要收【同一株】时，实际归谁。

        原稿 p20："如果场上存在多个同种植物，先平均分配，如果不整除则再进行一次
              随机分配（但是每个人最多在随机分配中获得一个）"。

        ★★ 什么时候真会走到：用户 2026-10-26 的四人局——
          牛角菱勾走两株聚电蓝莓，两株**都**声明要收那 1 株闪电草。
          原来两边都记在自己账上 → 闪电草被"成空"两次、日志写两遍、
          而且"收为己用"会把它算成两个人的攻击。
        ★ 随机源用 self.rng（对局种子），所以同一局可复现。
        ★ 结果【就地写回】 absorb_plans，后面所有读者都只看"真正归谁"。
        """
        if not self.absorb_plans:
            return
        claims = {}       # id(field) -> [field, [owner, ...]]
        order = []
        for owner, fields in self.absorb_plans.items():
            for f in fields:
                key = id(f)
                if key not in claims:
                    claims[key] = [f, []]
                    order.append(key)
                claims[key][1].append(owner)

        taken = {owner: [] for owner in self.absorb_plans}
        for key in order:
            f, owners = claims[key]
            winner = owners[0] if len(owners) == 1 else self.rng.choice(owners)
            taken[winner].append(f)
            # ★【v0.71】同回合可能被调用两次（原主人声明后一次、勾取兑现后
            #   覆盖再一次）—— 同一株的"随机分配"只准念一遍。
            if len(owners) > 1 and key not in self._dist_logged:
                self._dist_logged.add(key)
                losers = "、".join(o.tag for o in owners if o is not winner)
                self.say("  · 【%s】只有 1 株 → 归 %s（%s 也想要，随机分配没抢到）"
                         % (f.spec.name, winner.tag, losers))

        for owner in self.absorb_plans:
            self.absorb_plans[owner] = taken.get(owner, [])

    def _feedback_live(self):
        """【v0.57】[反馈湮灭] 的计数池：带反馈属性、且【没有成[空]】的植物。

        ★★ 成[空] = 不存在（用户 2026-10-22 原话）—— cut_gone / inert / annihilated
          三种都是"不存在"，一律不许数进去。
        ★ 它是 `_feedback_pass()` 的唯一计数入口（电系 / 黑洞 / 以后的苹果都走它）。
        """
        return [p for p in self.players
                if p.alive and p.field is not None and p.field.spec is not EMPTY
                and has_feedback(p.field.spec)
                and not p.field.cut_gone and not p.field.inert
                and not p.field.annihilated]

    def _annihilate(self, p, f, why):
        """[反馈湮灭] 落地：该株成[空] + 反馈值颗种返给【当前支配者】。

        ★ 支配者优先级：被[勾取] → 勾取者（hooked_by）；被[勾走] → 勾人者
          （absorbed_by）；都没有 → 自己。
        ★ 超防：原稿 151 写"产生[反馈毁灭]，并获得[超防]当局" —— 这个数字写在
          植物自己身上（spec.super_def_on_feedback），引擎不写死。
        ★★【v0.72】"获得超防"的那个人 = 上面"返种"的那个人（支配者）。
          原来这里写的是 p（原主）：这株被[勾取]转移到勾取者名下以后，
          种子归了勾取者、超防却挂在原主身上 —— 同一条收益两个收件人。
          用户 2026-10-27 从战报里一眼看出来："为什么真人2 3 会获得超防？？？"
        """
        f.annihilated = True
        f.fang = 0
        f.fang_aux = 0
        f.bao = 0
        f.inert = True
        owner = f.hooked_by or f.absorbed_by or p
        owner.seeds += f.spec.feedback_value
        self.say("  · %s 的【%s】%s → 触发[反馈湮灭]：成[空]，"
                 "并把 %d 颗种子返给 %s（现有种子%d）"
                 % (p.tag, f.spec.name, why, f.spec.feedback_value,
                    owner.tag, owner.seeds))
        sd = getattr(f.spec, "super_def_on_feedback", 0) or 0
        if sd:
            # ★ 与上面那 9 颗种【同一个收件人】—— 收益不许分给两个人。
            owner.super_def = max(owner.super_def, sd)
            self.say("    → %s 获得当局[超防]（%d 回合）"
                     % (owner.tag, owner.super_def))

    # ==================================================================
    # 【v0.45】队友链：107 番茄的"连接"落地（结算期，排在⓪ 刀类之后）
    # ==================================================================
    def _friend_resolve_phase(self):
        """107 番茄的连接落地 —— 【排在 ⓪ 刀类结算之后】（用户 2026-10-15 选 A）。

        ★★ 为什么偏偏在刀类之后：猪笼草"生效不生效"要看"它有没有被刀类切成
          [空]"—— 那只有刀类算完才知道。所以这里读 _live_gates()，拿到的就是
          "此刻真正还在生效的门"。
        ★ 一株番茄一次，顺序走 _attack_order（编号降序 → priority → 同株随机）：
          这条随机 tiebreak 正是"两个番茄同时在场"那个新随机点的出处
          （用户 2026-10-15 点名要记进随机接口清单）。
        ★ 步骤：
          ① 场上有【生效中的】猪笼草 → 目标【强制偏移】到它（多株 = 番茄声明期
             已选好的那一株）；门上若无人生效 → 番茄的原声明目标成立；
          ② ★★【v0.61】**强制[反馈逻辑]**：若最终目标在"对面那一支"、而场上
             恰好只剩 2 个分图（= 这一连会把全场并成一队）→ 不许连，改触发反馈
             （自身成[空] + 得【反馈值】颗种）。**这不是选项**（用户 2026-10-23：
             "反馈从来都是强制的，不是自己选的"）。
          ③ 给最终目标补【辅助防 P₁】（= 原稿的"增加 p＝9 的[当局辅助防]"）；
          ④ 目标还不是队友 → connect() 连成队友；已是队友 → 不重复连链。
        ★ 空目标（target=None）→ 【什么也不发生】（不连人、也不加防）。
          ★★【v0.63 修】原稿 p14 写的是"并为【目标】增加 p＝9 的[当局辅助防]"
          —— 辅助防是【给被连接的那个人】的。没有目标就没有人该拿到它。
          老版本在这里给自己 +9（把"空目标"脑补成了"连自己"），
          用户 2026-10-23 试玩当场抓出来："选择空目标不会给自己叠加辅助防，
          你怎么会理解成这个意思？"
        """
        if not self.friend_plans:
            return
        self.say("—— 队友连接：落地（排在刀类结算之后）——")
        # 【v0.46】★ 先把"人已经不在场"的条目剔掉再排序 —— 排序键要读
        #   p.field.spec，而 field 在回合初被清成 None（死人没有植物）。
        #   这条防御是"死亡 → 断联"那个 bug 的直接补丁；根因（friend_plans
        #   跨回合残留）已在 phase_bluff 开头清掉，这里再兜一层。
        for p, plan in list(self.friend_plans.items()):
            if not p.alive or p.field is None:
                self.say("  · %s 已出局 → 其【%s】不再结算"
                         % (p.tag, plan.get("name", "番茄")))
        todo = [p for p in self.friend_plans if p.alive and p.field is not None]
        for p in self._attack_order(todo):
            spec = p.field.spec
            plan = self.friend_plans[p]
            # 本品已成[空]（被刀类切掉 / 或本回合没释放）→ 效果不落地
            if p.field.cut_gone or p.field.inert:
                self.say("  · %s 的【%s】已成[空]（被切 / 未释放）→ 连接不再发生"
                         % (p.tag, spec.name))
                continue
            # ① 猪笼草偏移（判据 = 此刻仍生效的门）
            gates = self._live_gates()
            target = plan["target"]
            if gates:
                chosen = plan["gate"] if plan["gate"] in gates else gates[0]
                if target is not chosen:
                    self.say("  · 场上有【猪笼草】生效 → %s 的连接被【强制偏移】："
                             "%s → %s"
                             % (p.tag, target.tag if target else "空目标", chosen.tag))
                target = chosen
            # ② ★★【v0.61】[反馈逻辑] —— 【强制】，玩家没有"不触发"这个选项。
            #   判据（用户 2026-10-15 定的条件）：场上恰好 2 个连通分图、
            #   且目标在"对面那一支" ⇒ 这一连会把全场并成【一个】分图。
            #   既然不许靠一次连接直接"全员共同获胜"，就强制变成反馈自爆。
            if (target is not None and not self.same_team(p, target)
                    and len(self.team_groups()) == 2):
                self.say("  · 连接 %s 会让全场只剩【一个】队友链 → %s 的【%s】"
                         "【必定】触发[反馈逻辑]（连不了，改为自爆）"
                         % (target.tag, p.tag, spec.name))
                self._annihilate(p, p.field, "连接会把仅剩两队并成一队")
                continue
            # ③ 补【辅助防 P₁】—— ★ 只有【被连接的目标】拿得到
            if target is None:
                # 【v0.63 修】空目标 = 不连人 ⇒ 也不加任何辅助防。
                #   （原稿："为【目标】增加 p＝9 的[当局辅助防]"；
                #     没有目标就没有人该拿到这 9 点。）
                self.say("  · %s 的【%s】（空目标）→ 不连人、也不加任何辅助防"
                         % (p.tag, spec.name))
                continue
            # 【2026-10-25 拆步】+9 是【辅助防】，挪到【辅助防】那一步落地；
            #   这里只把"最终目标"记下来（它可能刚被猪笼草偏移过）。
            self.friend_assist_plans[p] = target
            self.say("  · %s 的【%s】→ 目标锁定 %s（+%d 辅助防在【辅助防】那一步落地）"
                     % (p.tag, spec.name, target.tag, spec.aux_def_gain))
            # ④ 连链（已是队友则不重复连）
            if self.same_team(p, target):
                self.say("    · %s 与 %s 已经是队友 → 不重复连链（只加防）"
                         % (p.tag, target.tag))
            elif self.connect(p, target):
                self.say("    · %s ↔ %s 连成队友（同一队友链）" % (p.tag, target.tag))

    def _cut_candidates(self, p):
        """这一株刀叶【能切】的候选人：其余存活玩家 + 射程 + 免切判定。

        ★ 射程 = 目标的【当前前段】必须落进 cut_seg（或 cut_any）。
        ★ 免切 = has_uncut(1)（挡刀叶类）或 has_uncut(2)（挡榴莲）为真 → 切不动。
          —— 判定只用植物自己的 has_uncut()，引擎不拼名单（v0.17 老规矩）。
        ★ 互挡状态里"可被切"的人（cuttable）不受"非互挡切不动"的限制，
          但这里【不过滤互挡】—— 因为"非互挡切不动"是【刀叶对刀叶】之间的事，
          在结算那一步判（见 _cut_one）。候选先给全，让玩家看得见。
        """
        spec = p.field.spec
        out = []
        for c in self.others_alive(p):
            tgt = c.field.spec
            # 目标"当前前段"
            seg = self._cur_seg(c.field)
            # 射程：cut_any 不限段；否则前段必须落进区间（算不出前段 = 切不动）
            if not spec.cut_any:
                if seg is None or not spec.can_cut_seg(seg):
                    continue
            # 目标是不是刀叶自己这一类：非互挡下切不动 → 候选里也要标注
            if self._is_uncuttable(spec, tgt, seg):
                continue
            out.append(c)
        return out

    def _is_uncuttable(self, knife_spec, tgt_spec, tgt_seg):
        """这一刀砍不砍得动对方（射程之外的那几道闸门）。

        ① 免切①：目标免疫【刀叶类】的废除（tag 或 前段∈0~6）→ 切不动。
        ② 免切②：目标是"榴莲免疫"的（只对本株是榴莲时生效）→ 切不动。
           ★ 规则 15.7：榴莲的 [免疫-②] 就是【免切②】，带免切② 的目标榴莲切不动。
           注意免切② 是"榴莲专用"—— 大刀叶去切带免切② 的目标不受它的影响。
        ③ 小刀叶的"无法清除大喷菇"：走 spec.cut_ignore_plants（植物自己的名单）。
        """
        # ③ 本株自己点的名（大喷菇对小刀叶）
        if tgt_spec.name in knife_spec.cut_ignore_plants:
            return True
        # 【v0.56】[不灭]：p30 词条 `[不灭]：无视[废除][阉割]` —— 消除 / 废除切不动。
        #   ★ 动得了它的只有[毁灭]（151 黑洞大王花，见 _destroy_resolve_phase）。
        #   ★ 这一条以前漏了：immortal 只被拿去判"强制释放"，_cut_one 根本不看它，
        #     于是 97 闪电草的[不灭]形同虚设、照样被切（用户 2026-10-21 点名）。
        if is_immortal(tgt_spec):
            return True
        # ① 免切①（无视金刚刀叶的废除）—— ★ 只挡【金刚刀叶】（2026-09-28 用户澄清：
        #   "免切①只挡金刚刀叶，其余刀叶不受"）。早先写成"挡整个刀叶类"会让
        #   大刀叶成废牌（射程 0~5 全落在免切① 的 0~6 里）。
        if is_jingang(knife_spec) and tgt_spec.has_uncut(1, tgt_seg):
            return True
        # ② 免切② —— 只有榴莲受它挡
        if is_durian(knife_spec) and tgt_spec.has_uncut(2, tgt_seg):
            return True
        return False

    def _same_name_holders(self, p, target):
        """【连切】同名卡 = 这场上【所有玩家的】这一株植物（规则 15.2）。

        ★ 用户 2026-09-28："刀叶类植物有连切机制——即它可以把场上同一种植物
          全部切了" —— "同一种" = 同名卡（同一 PlantSpec.name），不是同一玩家。
        ★ 返回的顺序：按玩家编号升序（战报/界面稳定，便于测试）。
        ★ 只收"射程内 + 砍得动"的（用和 _cut_candidates 同一套闸门）——
          否则会出现"全切把切不动的也算进去"的错觉。
        """
        spec = p.field.spec
        out = []
        for c in self.others_alive(p):
            if c.field.spec.name != target.field.spec.name:
                continue
            seg = self._cur_seg(c.field)
            if not spec.cut_any and (seg is None or not spec.can_cut_seg(seg)):
                continue
            if self._is_uncuttable(spec, c.field.spec, seg):
                continue
            out.append(c)
        out.sort(key=lambda x: x.idx)
        return out

    def _threshold_charge_effect(self, spec, spent):
        """【v0.38】分段函数式阈值充能（57 聚电蓝莓）。

        原稿 p20：
          ｛充能+X消耗(X∈[0,5])　　　　　　　单体·围·伤1　　　　X　　　5+X+[回]
          ｛充能+X消耗(X∈{7,9,11,…,2n−5})　单体·围·伤1·[闪电一击]　(X+5)/2　5+(X+5)/2+[回]

        ★★ 用户 2026-09-30 拍板的三条口径（原稿写得含糊，靠他补的）：
          1. **X=6 按 5 算** —— 落【第一段】、【不带】闪电一击；
          2. **偶数（≥8）"效果向下取成奇数"** —— 效果按 X−1 算；
          3. **"但是耗种数不变"** —— 耗种仍按【原始 X】算（耗种那笔账在调用方扣，
             本函数只算效果）。

        返回 (fang, bao, flash)：
          fang = 防御、bao = 暴击（削防）、flash = 本回合是否带 [闪电一击]。
        ★ 数值一个都不写死在引擎里 —— 全从 spec.threshold_charge 那张表读。
        """
        t = spec.threshold_charge
        c = t.get("c", 0)             # 耗种常数项（这里只用于算防护公式的偏移）
        lo = t.get("lo", 0)
        hi = t.get("hi", 5)
        odd_from = t.get("odd_from", 7)
        # 先定"归哪一段"：X ≤ hi + 1（含 X=6）→ 第一段；否则 X ≥ odd_from 的第二段。
        #   ★★ 为什么是 hi + 1：用户 2026-09-30 拍板"X=6 按 5 算（落第一段、无闪击）" ——
        #      X=6 是原稿两段之间的空档（[0,5] 与 {7,9,11,…} 都不含 6），
        #      用户把它判给第一段；而第一段的效果本身就是"暴=X、防=c+X"，
        #      所以 X=6 直接按【6 的价值】算（= 暴6、防 5+6=11），
        #      只是【不带】闪击 —— 与"按 5 算"在"落在第一段、无闪击"这两点一致。
        if spent <= hi + 1:
            # 第一段：暴 = X、防 = c + X、无闪电一击
            x = spent
            return (c + x, x, False)
        # 第二段：效果按【向下取的奇数】算（偶数 −1；奇数不变）。
        if t.get("effect_floor_to_odd") and spent % 2 == 0:
            x = spent - 1
        else:
            x = spent
        if x < odd_from:
            # 理论上到不了（spent ≥ hi+1 = 6 时偶数取 5 → 会落到这里）。
            #   X=6 已在上面按第一段返回；spent=7 起 x ≥ 7。留个兜底，别崩。
            x = odd_from
        fang = c + (x + 5) // 2
        bao = (x + 5) // 2
        flash = spent >= t.get("flash_from", odd_from)
        return (fang, bao, flash)

    def _apply_release(self, p):
        """【v0.7】释放 = 维持原效果。
        产种子的植物在这里才把种子拿到手 —— 不释放则连这颗种子都没有。
        （攻击/治疗/充能走各自的后续步骤，不在这里结算。）"""
        spec = p.field.spec
        # 【2026-10-25】种子也挪到【有收益批】那一步（与"造成伤害得种"同一语义）。
        #   这里只播报，不加种子 —— 否则同一件事有两个出口，数量会翻倍。
        self.say("%s 释放【%s】%s"
                 % (p.tag, spec.name,
                    "（产种子在结算顺序里落地）" if spec.seed_gain
                    else "（效果维持原样）"))
        # ★★【2026-10-25】这里【只】做"声明"的播报与【代价】。
        #   水滴 / 火种 / 鱼腥味 / 倍耗 / 超防 / 免疫 / 种子 全部挪到场末各自那一步，
        #   见 _apply_effects_phase（用户："释放只是声明，效果要看结算顺序"）。
        #   留在这一处的只有 self_wound_hp —— 它是【代价】，与出牌耗种同类：
        #   付不起就不该出，代价不该"等结算完再决定要不要付"。
        # 【v0.38】自扣血（47 过载喇叭花的 [自扣0.5血]）：
        #   ★ 与 cost_hp 的区别：cost_hp 是【出牌时】的代价（付不起就出不了），
        #     而 self_wound_hp 是【释放时】的自伤 —— 已在场上的人才会挨这一下。
        #   ★ 走 _hurt（统一入口）而不是直接 p.hp -= n：
        #     这样它才会计入"超防"等玩家层面的减免，也走的是唯一扣血语义。
        #     ⚠️ 但自扣血【不该】被自己的超防挡住（自己烧自己）——
        #        原稿没说，本轮按"直接扣"处理（super_pierce=True）。
        if spec.self_wound_hp:
            p.hp -= spec.self_wound_hp
            if p.hp < 0:
                p.hp = 0
            self.say("%s 的【%s】释放时自扣 %s 条命（现有%s）"
                     % (p.tag, spec.name, self.hp_str(spec.self_wound_hp), p.hp_text()))

    def _appliers(self):
        """【场末 施加】这一步要结算的"施加类"株。

        判据只有一条（与《结算顺序（现有）.md》4.2 的"作废"规则同源）：
          主人还活着 ∧ 已释放 ∧ 场上还这一株。
          · 不释放 → 不在 released → 整株跳过；
          · 被[毁灭]/被消除/被勾走/被反馈湮灭 → `_void_one` 会 released.discard
            → 这里自然也就跳过了。**不要另加 cut_gone / inert 判断**：
            摘名单已经是唯一出口（v0.33/v0.56 的教训）。
        ★ 顺序用 `_attack_order`（编号降序 → 优先级 → 随机），不是座位顺序 ——
          两株丝瓜同场时，谁先加层不该看谁坐得靠前。
        """
        return self._attack_order([p for p in self.players
                                   if p.alive and p in self.released
                                   and p.field is not None])

    def _apply_effects_phase(self, kind):
        """【2026-10-25】场末的四步"施加"：kind = assist / defense / fire / layer。

        ★★ 为什么单独成步、而且排在【一切攻击】之前：
          用户 2026-10-25："有防御收益的结算，当然一定要放在攻击结算之前，
          结果你放在[噬魂]后面。" 噬魂与有收益批也是攻击，所以这三步必须
          比它们都早；附加层是例外（下回合才入账，本回合没人读它）。
        ★ 战报措辞沿用旧版（"释放【X】→ …"），只是时点从声明期挪到了这里。
        """
        loofah_on = bool(self.rules.get("loofah_wansui"))
        if kind == "assist":
            # ① 产出：水滴草 +N 水滴 —— ★必须排在"花费"之前，见下面
            for p in self._appliers():
                spec = p.field.spec
                if not spec.water_gain:
                    continue
                # ★ 水滴是这株植物产出 → 归它现在的支配者
                #   （【花费】不挪：那是"玩家自己的操作"，用谁的水就防谁的家）。
                _who = self._holder(p)
                _who.water += spec.water_gain
                self.say("%s 获得%d个水滴（现有%d）%s"
                         % (p.tag, spec.water_gain, _who.water,
                            "" if _who is p else
                            "（这一株已归 %s 支配 → 水滴记在 %s 账上）"
                            % (_who.tag, _who.tag)))
            # ② 花费：加固（声明期只记了"花几个"，这里才真扣、真加防）
            for p in self._appliers():
                n = self.water_def_plans.get(p, 0)
                if n <= 0:
                    continue
                n = min(n, p.water)
                if n <= 0:
                    self.say("  · %s 声明加固，但手上一滴水都没有 → 不生效" % p.tag)
                    continue
                p.water -= n
                p.field.fang += n
                # 【v0.21】单独记下"这部分防是【辅助防 P₁】" —— 噬魂类无视它。
                p.field.fang_aux += n
                self.say("%s 消耗%d个水滴 → 本回合防御 +%d"
                         "（现有%d，其中辅助防%d；剩余水滴%d）"
                         % (p.tag, n, n, p.field.fang, p.field.fang_aux, p.water))
            # ③ 番茄的 +9【辅助防 P₁】—— 给【最终目标】（可能被猪笼草偏移过）
            for p in self._appliers():
                spec = p.field.spec
                if not spec.link_friend:
                    continue
                target = self.friend_assist_plans.get(p)
                if target is None or not target.alive or target.field is None:
                    continue
                target.field.fang += spec.aux_def_gain
                target.field.fang_aux += spec.aux_def_gain
                self.say("  · %s 的【%s】→ %s 获得 +%d【辅助防 P₁】"
                         "（现有防 %d，其中辅助防 %d）"
                         % (p.tag, spec.name, target.tag, spec.aux_def_gain,
                            target.field.fang, target.field.fang_aux))
        elif kind == "defense":
            for p in self._appliers():
                spec = p.field.spec
                if spec.super_def:
                    _who = self._holder(p)
                    refreshed = _who.super_def > 0
                    _who.super_def = spec.super_def
                    self.say("%s 的【%s】→ 获得【超防】：%d 回合内（从本回合起算），"
                             "期间普通暴击 / 普通穿透都打不穿%s"
                             % (p.tag, spec.name, spec.super_def,
                                "" if _who is p else
                                "（这一株已归 %s 支配 → 超防记在 %s 账上）"
                                % (_who.tag, _who.tag)))
                    if refreshed:
                        self.say("  → 已有超防 → 【刷新】为 %d 回合（不叠加）"
                                 % spec.super_def)
                if spec.immune_turns:
                    _who = self._holder(p)
                    refreshed = _who.immune_turns > 0
                    _who.immune_turns = spec.immune_turns
                    self.say("%s 的【%s】→ 获得[免疫]：当局＋下两局（共 %d 回合），"
                             "每个【回合最末】清掉自己与队友的 [倍耗]/[鱼腥味]%s"
                             % (p.tag, spec.name, spec.immune_turns,
                                "" if _who is p else
                                "（这一株已归 %s 支配 → 免疫记在 %s 账上）"
                                % (_who.tag, _who.tag)))
                    if refreshed:
                        self.say("  → 已有[免疫] → 【刷新】为 %d 回合（不叠加）"
                                 % spec.immune_turns)
        elif kind == "fire":
            for p in self._appliers():
                spec = p.field.spec
                if not spec.fire_gain:
                    continue
                for other in self.players:
                    if other is p or not other.alive:
                        continue
                    self._add_fire(other, spec.fire_gain)
        elif kind == "layer":
            exempt = self.cut_exempt_plans
            for p in self._appliers():
                spec = p.field.spec
                # ★「切一半」的豁免：刀叶在【消除/废除】那一步已经问过"帮谁"，
                #   施加挪到这里之后，直接把他们从名单里划掉 —— 不再"先加再回滚"。
                skip = set(exempt.get(p) or ())
                if spec.fishy_gain:
                    skipped = []
                    for other in self.players:
                        if other is p or not other.alive or other in skip:
                            continue
                        if loofah_on and self.same_team(p, other):
                            skipped.append(other)
                            continue
                        other.fishy_next_by[p.idx] = \
                            other.fishy_next_by.get(p.idx, 0) + spec.fishy_gain
                        self.say("%s 的【%s】→ 给 %s 附加 %d 层【鱼腥味】"
                                 "（下一回合起：非基石类消耗 +%d 种/层）"
                                 % (p.tag, spec.name, other.tag, spec.fishy_gain,
                                    spec.fishy_gain))
                    if skipped:
                        self.say("  ※【丝瓜万岁】%s 是 %s 的队友 → 不吃【鱼腥味】"
                                 % ("、".join(o.tag for o in skipped), p.tag))
                    if skip:
                        self.say("  ※ 被『切一半』保住的人不吃这一层：%s"
                                 % "、".join(x.tag for x in skip))
                if spec.mult_gain:
                    skipped = []
                    for other in self.players:
                        if other is p or not other.alive or other in skip:
                            continue
                        if loofah_on and self.same_team(p, other):
                            skipped.append(other)
                            continue
                        other.mult_next_by[p.idx] = \
                            other.mult_next_by.get(p.idx, 0) + spec.mult_gain
                        self.say("%s 的【%s】→ 给 %s 附加 %d 层【倍耗】"
                                 "（下一回合起：最低消耗 = 基础消耗 ×2/层）"
                                 % (p.tag, spec.name, other.tag, spec.mult_gain))
                    if skipped:
                        self.say("  ※【丝瓜万岁】%s 是 %s 的队友 → 不吃【倍耗】"
                                 % ("、".join(o.tag for o in skipped), p.tag))
                    if skip:
                        self.say("  ※ 被『切一半』保住的人不吃这一层：%s"
                                 % "、".join(x.tag for x in skip))

    def _add_fire(self, target, n):
        """【v0.10】给某人附加火种。三件事按顺序发生：
        ① 每 1 个火种抵消 1 个水滴（火种的作用2：烧掉你的水）
        ② 小喷菇 / 大喷菇当场被烧成"空"（火克菇，你选的"当场变空，本回合白出"）
        ③ 叠满 FIRE_BURST_AT 个 → 结算阶段火种爆发（见 _fire_burst）"""
        for _ in range(n):
            if target.water > 0:
                target.water -= 1
                self.say("  → %s 的 1 个水滴被火种抵消（剩余水滴%d）"
                         % (target.tag, target.water))
            target.fire += 1
        self.say("%s 被附加%d个火种（现有%d）" % (target.tag, n, target.fire))
        if target.field.spec.name in FIRE_BURNS:
            self.say("  → %s 的【%s】被火种烧成【空】！本回合白出（代价不退）"
                     % (target.tag, target.field.spec.name))
            target.field = FieldPlant(target, EMPTY)
            self.released.discard(target)   # 效果全无，等同没释放
            self.plans.pop(target, None)

    def _fire_burst(self, p):
        """【v0.10】火种爆发：叠满 FIRE_BURST_AT 个火种后，额外挨一次削防 4 的攻击。
        这是一次【凭空】的攻击 —— 没有攻击者，所以不走属性免疫 / 无视 / 克制那套。"""
        self.say("*** %s 的火种叠满 %d 个 → 火种爆发！ ***"
                 % (p.tag, FIRE_BURST_AT))
        p.fire = 0
        f = p.field
        # 【v0.23】★ 超防闸门排在【削防】之前 —— 与 resolve_hit 同一口径。
        #   火种爆发是一次"削防 4"的攻击，属于"普通的那一类"（没有攻击者、
        #   也没有标"无视超防"），所以挂着超防时【整个作废】：不削防、不破防、不掉血。
        #   老版本只拦伤害、削防照做 —— 与"杨桃削仙桃"是同一个 bug。
        #   ⚠️ 放在 f.broken 判定之前：超防期间植物根本不会被破防，两条不会同时命中。
        if self.super_def_blocked(p):
            self.say("  但 %s 处于【超防】状态（还剩 %d 回合）"
                     "→ 爆发的削防与伤害被完全挡下" % (p.tag, p.super_def))
            return
        # 【v0.26】★ 这是【"打穿后不打人"剩下的唯一例外】，是有意留着的：
        #   v0.26 起普通攻击（含连击的后续子弹）打穿后不再被挡、直接落在人身上；
        #   但火种爆发【不是攻击】—— 它没有攻击者、目标是"谁身上攒的火就炸谁"，
        #   而那人本回合的植物已经碎了 → 没有可炸的地方，火种就白攒了。
        #   这条口径（原稿没写）正是【待确认 #31】，还开着；等用户拍板再动这里。
        if f.broken:
            self.say("  但【%s】本回合已被击破，爆发打在空处（火种白攒）"
                     % f.spec.name)
            return
        f.fang -= FIRE_BURST_BAO
        if f.fang < 0:
            f.broken = True
            # 【v0.18】火种爆发是"普通的"那一边 —— 超防照挡（它没有攻击者，
            #   不属于"显式无视超防"的那类效果）。
            self.say("  削防%d，防御被打穿！%s"
                     % (FIRE_BURST_BAO, self._hurt_text(p, 2)))
            self.fire("on_break", f, {"attacker": None})
        else:
            self.say("  削防%d，剩余防御%d" % (FIRE_BURST_BAO, f.fang))
        # 【v0.28】一次爆发算一个"单位" → 当场落地（火种爆发是凭空攻击、
        #   不由某株植物发起，所以它自己收尾）。
        self._flush_damage()

    # ---------- 人参果【v0.16】：充能 · 标记段 · 延迟爆发 ----------
    def _cur_seg(self, field):
        """【v0.32】一株植物【当前】的编号前段 —— 全项目唯一的"算前段"入口。

        规则（游戏规则总结.txt 15.4 / 15.5）：
          · 普通植物       → no 的前段（"35" → 3）
          · 充能植物       → 0（默认，按卡面）；若房间规则 charge_seg_var 开着，
                              前段 = 【原始耗种数 + 本回合充能消耗的种子数】
          · 人参果          → 它标记的那段（充能数）；没充能落回卡面前段
          · 场上没植物 / "空" / 编号未确认 → None

        ★★ 为什么必须【只有一个】函数：免切①（前段 ∈ 0~6）与刀叶射程
          （前段 0~5 / 6~12）读的是【同一个值】—— 用户 2026-09-28 拍板 #78。
          两处各算一遍，迟早在"充能植物充能之后"这点上分叉。
        """
        if field is None or field.spec is EMPTY:
            return None
        spec = field.spec
        # 人参果（充能数 = 它标记的段）：与选项无关，它本来就是"段随充能走"。
        if spec.charge_tag:
            if field.tag_seg:
                return field.tag_seg
            return int(spec.no[0]) if spec.no else None
        # 充能类 + 房间选项开着 → 前段 = 耗种 + 充能数（本回合充了多少）。
        #   ★ charge_seg_var 是房间规则，归属"这局怎么算"，所以引擎读规则是正当的；
        #     但它只决定【用哪条公式】，不写死任何一个植物的数字。
        if spec.charge and self.rules.get("charge_seg_var"):
            return spec.cost_seed + (field.charge_spent or 0)
        if not spec.no:
            return None
        return int(spec.no[0])

    def _seg_of(self, p):
        """这个人的【当前前段】（兼容老调用）；没有则 None。"""
        return self._cur_seg(p.field)

    def cut_target_seg(self, target_player):
        """【v0.32】刀叶要切的目标，其"当前前段"（None = 算不出 → 只对 cut_any 有效）。"""
        return self._cur_seg(target_player.field)

    def _tag_mark_phase(self):
        """【v0.16】人参果的两件事，都排在充能之后：
        ① 埋标记：本回合充了 n 种子的人参果 → 给段 n 建记录（没有就建、有就共用），
           放它的人记进"埋雷者名单"（名单里的人挨爆发按半条命算）。
        ② 记账：本回合场上每一株"属于某个已标记段"的植物各 +1 笔
           —— 不看释放与否（未释放也算"出现"），人参果自己也算它标记的那一段。
        """
        # ① 埋标记
        for p in self.players:
            if p.field is None or not p.field.spec.charge_tag:
                continue
            n = p.field.tag_seg
            if not n:
                continue          # 没充能 / 充 0：不标记（0 段不能当标记段）
            rec = self.tag_marks.get(n)
            if rec is None:
                rec = _TagMark(n, self.round)
                self.tag_marks[n] = rec
                self.say("*** %s 的【人参果】充能%d → 给【%d 段】埋下标记！"
                         "该段此后每出现一次记一笔，累积≥%d 就爆发穿透伤害 ***"
                         % (p.tag, n, n, TAG_BURST_AT))
            if p not in rec.owners:
                rec.owners.add(p)
                self.say("  → %s 是第 %d 个为【%d 段】埋人参果的人"
                         "（挨爆发时按半条命算）" % (p.tag, len(rec.owners), n))
            else:
                self.say("  → %s 又为【%d 段】埋了一株人参果（同一段共用一份计数，不叠加）"
                         % (p.tag, n))

        # ② 记账
        for p in self.players:
            if not p.alive:
                continue
            seg = self._seg_of(p)
            if seg is None:
                continue
            rec = self.tag_marks.get(seg)
            if rec is None:
                continue
            rec.count += 1
            # 【v0.63】记下"计数首次达到阈值"的回合 —— 那一回合豁免不炸
            if rec.count >= TAG_BURST_AT and rec.reach_round is None:
                rec.reach_round = self.round
                self.say("  · 段 %d 累积达到 %d/%d → 本回合【不爆发】，"
                         "从下一回合起才会炸" % (seg, rec.count, TAG_BURST_AT))
            self.say("  · 段 %d 累积 +1 → %d/%d（%s 的【%s】）"
                     % (seg, rec.count, TAG_BURST_AT, p.tag, p.field.spec.name))

    def _tag_burst(self):
        """【v0.16】人参果爆发：段 n 累积 ≥ TAG_BURST_AT → 本回合场上该段植物全体挨穿透。

        分档【已确认】：为该段放过人参果的人 0.5 条命（1 半命单位），其它人 1 条命（2）。
        豁免【已确认·v0.63 收紧】：计数【首次达到阈值】的那一回合不炸，
        从下一回合起才炸 —— 见 _TagMark.reach_round 里的说明。
        （老口径只豁免"埋雷那一回合"，于是"雷是上回合埋的、这回合才顶满"
          会当回合就炸，用户 2026-10-23 报的就是这个。）
        "穿透"= 无视防御，直接登记半命数 —— 不走 resolve_hit 的"削防 → 破防"流程，
        所以也不用构造"+∞ 的大数"（那样 0.5 那一档反而会被溢出盖掉）。
        """
        for n in sorted(self.tag_marks):
            rec = self.tag_marks[n]
            if rec.count < TAG_BURST_AT:
                continue
            # 【v0.63】刚叠满的那一回合 → 不炸（reach_round）；
            #   记录里还没有 reach_round 的旧数据 → 退回 born_round 兜底。
            ready = rec.reach_round if rec.reach_round is not None else rec.born_round
            if self.round <= ready:
                self.say("  · 段 %d 累积已达 %d，但【本回合刚叠满】→ 本回合不爆发"
                         % (n, rec.count))
                continue
            victims = [p for p in self.players if p.alive and self._seg_of(p) == n]
            if not victims:
                continue
            if rec.burst_round is None:
                rec.burst_round = self.round
            self.say("*** 人参果爆发！段 %d 累积 %d ≥ %d → 场上该段植物全体吃穿透伤害 ***"
                     % (n, rec.count, TAG_BURST_AT))
            for p in victims:
                amount = 1 if p in rec.owners else 2      # 半命单位：0.5 / 1 条命
                # 【v0.18】这一发【无视超防】。原稿 06 那行原话就是"伤害[无视][超防]"，
                #   你 2026-09-16 也确认"人参果的确无视超防"——
                #   于是它成了超防的唯一例外，其余穿透（普通有限数值）一律挡。
                self._hurt(p, amount, super_pierce=True)
                self.say("  → %s 的【%s】被穿透（无视防御、无视超防）→ 损失 %s 条命%s"
                         % (p.tag, p.field.spec.name, self.hp_str(amount),
                            "（埋雷者，减半）" if p in rec.owners else ""))
            # 【v0.28】一个段的爆发 = 一个单位 → 当场落地。
            #   为什么按【段】flush、而不是整个 _tag_burst 完了一次性落地：
            #   victims 是逐段现算的（[p for p in players if p.alive and seg==n]），
            #   前一段炸死的人就不该再被后一段炸第二遍 —— 这正是"实时"的意思。
            self._flush_damage()

    def phase_settle(self):
        """结算阶段【v0.28：逐株立刻落地】。

        分这些步：⓪-a【v0.56】[毁灭]（黑洞大王花，最前）→
                ⓪-b【v0.55】刀叶类（消除 / 废除）→
                ①【v0.51】复活/加血二选一（161）→ ② 治疗 →
                ③ 番茄连接落地 → ④ 勾人 / [反馈湮灭] →
                ⑤ 噬魂吞噬（先于普通攻击，见第十二章）→
                ⑥【v0.27】有收益批（噬魂之后、普通攻击之前）→
                ⑦ 普通攻击（【v0.28】按序列优先级；【v0.70】死了也能出手）→
                ⑧ 火种爆发 → ⑨ 人参果爆发 →
                ⑩ 猫尾草 → ⑪【v0.54】[免疫]（回合末清账，全场最后一步）。

        ★★ v0.9~v0.27 的 ③「统一扣血」在 v0.28 被【取消】了。
          用户 2026-09-22 的原话："攻击行为应该是每个植物完成攻击后就结算一次。
          你可以暂且按序列弄一个优先级，这样到时候你就可以把溅射之类涉及环的
          放在最后。"
          于是现在是：一株打完 → 它的伤害【当场】从生命里扣掉 → 生命归 0 的人
          【当场退出存活环】→ 后面轮到他的植物时人已出局、不再出手。

        ★★ 代价（用户明确选的，不是漏改）：v0.9 那条"同时结算、可以同归于尽"
          没有了 —— 先手方抢先打死对方就能免遭还击，先手优势回来了。
          选"死了不打"就是他这一轮问答题里的选项 2。
          v0.9 的旧说明留在规则文档里当版本史，不删。

        ★ 这个阶段仍然【不向任何人提问】（溅射要不要溅在博弈阶段就声明完了）。
        ★ 每一步都走 _flush_damage() 兑现，所以 _pending_dmg 的寿命只有
          "一株植物的一次攻击"那么长 —— 别再指望它攒到阶段末尾。
        """
        self.say("—— 结算阶段 ——")
        self._pending_dmg = {}
        # 【v0.28】环与生命对齐一次：外部（测试/将来的复活）直接改过 hp 的话，
        #   这里是唯一能把它掰回来的地方。
        self._sync_ring()
        # ⓪-a【v0.56】[毁灭]（151 黑洞大王花）—— ★ 现在是结算的【最前一步】。
        #   ★ 为什么在刀类之前（用户 2026-10-21："显然应该排在刀类之前"）：
        #     黑洞把【除自己外全场】打成[空]，刀叶也在被毁名单里。刀类若先走，
        #     先切掉一批，黑洞的 m（= 被毁单位耗种总和）就缩水了 —— 会改结果。
        #   ★ [毁灭] 无视[不灭]、也不吃超防（超防只拦伤害）—— 判据见 _destroy_resolve_phase。
        self._hook_pass("destroy")
        self._feedback_pass("destroy")   # 【v0.58】反馈先于这一步自己的效果判
        self._destroy_resolve_phase()
        # ⓪-b【v0.55】刀叶类：消除 / 废除 —— ★ 排在 ①② 之前。
        #   ★★ 为什么挪到最前（用户 2026-10-21）：
        #     原来的落点是"治疗之后、一切攻击之前"，只按 2026-09-28 那句
        #     "在攻击发生之前"定位 —— 治疗不算攻击，就被顺手排在治疗后面，
        #     从没论证过。后果是治疗类（水蜜桃 / 仙桃 / 免疫菇）先加完血、
        #     再被切成[空]：血已经落袋，这一刀只剩拆防御，
        #     "消除"对治疗类等于失效。用户原话："这显然不符合逻辑"。
        #   ★ 被切成 [空] 的植物会被 released.discard（见 _cut_one 结尾）
        #     → 后面的复活 / 治疗自然轮不到它。这才是"消除"该有的样子。
        #   ★ 规则 15.7"切在一切攻击之前"仍然满足（比原来更早）；
        #     "切一半"要挂的豁免标记（cut_half_done）也因此更早挂上。
        #   ★ 超防【不挡刀】（用户 2026-10-21 拍板，待确认 #55 结案）：
        #     _cut_one 不看 super_def —— 仙桃 / 生命之花照样切得动。
        #   ★【v0.56】但 [不灭] 【挡】得住：p30 词条 `[不灭]：无视[废除][阉割]`，
        #     用户 2026-10-21 重申"动不了固有属性的是[毁灭]，不是消除/废除"。
        self._hook_pass("cut")
        self._feedback_pass("cut")   # 【v0.58】反馈先于这一步自己的效果判
        self._cut_resolve_phase()
        # ①【v0.51】161 生命之花：加血 / 复活【二选一】—— ★ 必须在治疗之前，
        #   而且它的加血不走治疗（治疗是无条件加血，两个都做就超模了）。
        self._hook_pass("revive")
        self._feedback_pass("revive")   # 【v0.58】反馈先于这一步自己的效果判
        self._revive_phase()
        # ②【v0.4】治疗
        self._hook_pass("heal")
        self._feedback_pass("heal")   # 【v0.58】反馈先于这一步自己的效果判
        # 【2026-10-25】"用 1 水滴抵掉喷菇那半条命"也落在【加血】这一步：
        #   声明期只回答"要不要"（见 phase_bluff ①b），退血与扣水滴在这里做。
        for p in list(self.water_hp_plans):
            if not p.alive or p in self.released is False or p.field is None:
                continue
            cost = p.field.spec.cost_hp
            if cost <= 0 or p.water <= 0:
                continue
            p.water -= 1
            p.hp += cost
            self.say("%s 用 1 个水滴抵掉【%s】的半条命代价 → 生命退回 %s，"
                     "剩余水滴%d"
                     % (p.tag, p.field.spec.name, p.hp_text(), p.water))
        for p in self.players:
            if not p.alive or p not in self.released:
                continue
            # 【v0.51 / 2026-10-25 拆步】161 的二选一在【复活】那一步问过：
            #   选了"复活"→ 这里跳过（否则就是"既复活又加血"）；
            #   选了"加血"（或没有可救的人）→ 就在这里落地，与其它加血同一批。
            if getattr(p.field.spec, "revive_one", False) \
                    and self.revive_mode_plans.get(p) == "revive":
                continue
            heal = p.field.spec.heal
            if heal > 0:
                # ★ 加血是这株植物产出的【收益】→ 记在它现在的支配者账上。
                who = self._holder(p)
                who.hp += heal  # 【待确认#16：是否设生命上限，样品暂不设】
                if who is p:
                    self.say("%s的【%s】发动加血：+%s条命（现有%s条命）"
                             % (p.tag, p.field.spec.name, self.hp_str(heal),
                                p.hp_text()))
                else:
                    self.say("%s的【%s】发动加血：+%s条命（这株已归 %s 支配）"
                             "→ 记在 %s 账上（现有%s条命）"
                             % (p.tag, p.field.spec.name, self.hp_str(heal),
                                who.tag, who.tag, who.hp_text()))
        # ③【v0.45】队友连接落地（107 番茄）—— ★ 紧跟刀类之后：
        #   猪笼草"还生不生效"要看刀类有没有把它切成[空]（用户 2026-10-15 选 A）。
        #   ★ 中间隔了 ①/② 不影响这条判据 —— 复活与加血都不会让被切的植物回来。
        self._hook_pass("friend")
        self._feedback_pass("friend")   # 【v0.58】反馈先于这一步自己的效果判
        self._friend_resolve_phase()
        # ④【v0.38】电系：勾人 → [反馈湮灭]（顺序是规则的一部分，别调换）。
        #   ★★ 用户 2026-09-30："勾人在触发之前"。
        #   ★ 落点 = 刀叶之后、【一切攻击之前】：
        #     · 被勾者成[空]要赶在攻击判定之前生效；
        #     · 湮灭把触发者成[空]、并返种给支配者，也要赶在攻击前。
        self._hook_pass("absorb")
        self._feedback_pass("absorb")   # 【v0.58】反馈先于这一步自己的效果判
        self._absorb_resolve_phase()
        # ⑦~⑩【2026-10-25】施加类四步 —— 必须排在【一切攻击】之前。
        #   ★ 用户："有防御收益的结算，当然一定要放在攻击结算之前，结果你放在
        #     [噬魂]后面。" 噬魂 / 有收益批 也是攻击，所以这三步要更早；
        #     附加层（倍耗 / 鱼腥味）降级为"下回合才入账"，本回合没人读它。
        #   ★ 每一步开头照例先兑 [勾取] 与 [反馈湮灭]（它们对任何一步都成立）。
        for _stage in ("assist", "defense", "fire", "layer"):
            self._hook_pass(_stage)
            self._feedback_pass(_stage)
            self._apply_effects_phase(_stage)
        # ⑤【v0.21】噬魂：吞噬【先于】普通攻击结算 ——
        #     你的规则"其它植物也能对人造成伤害时，一般以噬魂类优先"。
        #     为什么必须排在前面，两条理由都是硬的：
        #       ① 种子池只有一份，先吞者按"当时剩余"算，后吞的只能吃剩饭；
        #       ② 目标一旦被普通攻击登记了致死伤害，后面所有吞噬就全都落空。
        #     ⚠️ 别把它并进下面的 ⑦ 里 —— 顺序本身就是规则的一部分。
        self._hook_pass("devour")
        self._feedback_pass("devour")   # 【v0.58】反馈先于这一步自己的效果判
        devourers = [p for p in self.plans if p.field.spec.attack.is_devour]
        if devourers:
            self.say("—— 噬魂：吞噬（排在普通攻击之前）——")
            for p in self._devour_order(devourers):
                # 【v0.70】★★ 死了也能出手 —— 用户 2026-10-26 明确推翻 v0.34 的
                #   "攻方死了不出手"。本回合已声明、还没轮到的那一发【照常发出】。
                #   ⚠️ 这推翻了 v0.28/v0.29/v0.34 三次迭代的"死了不打（攻方侧）"，
                #     他知情（选项里写明了影响噬魂 / 有收益 / 普通攻击三批）。
                #     另一半"目标死了就落空"（_effectively_dead）【保留不动】。
                self._resolve_plan(p)
        # ⑥【v0.27 建 / v0.41 放宽为"有收益批"】—— 排在【噬魂之后、普通攻击之前】。
        #     用户 2026-09-21 原话："杨桃，造成一次伤害加一次种子（暂且优先级排在
        #     噬魂类后）；后面杨桃这一类植物也默认拥有优先级，按照序列顺序，
        #     如果序列相同再声明"。
        #     ★ 【v0.41】准入判据从 is_yangtao_class（只认"得种"）放宽成
        #       is_reward_class（得种 ∪ 击杀回血）—— 141 石榴机枪（击杀回血、
        #       不得种）也进这一批（用户 2026-10-11："和杨桃类同批，高的原因是
        #       序列高"）。批内仍是【序列降序】→ 141 序列最大，最先出手。
        #     ⚠️ 别并进 ⑦：那一步是按座位顺序走的，没有优先级可言；
        #        单独一批才谈得上"排在噬魂之后"。
        #     ⚠️ ⑦ 也必须把这一批排除掉，否则同一株会被结算两遍。
        self._hook_pass("reward")
        self._feedback_pass("reward")   # 【v0.58】反馈先于这一步自己的效果判
        reward = [p for p in self.plans if is_reward_class(p.field.spec)
                  and not p.field.spec.attack.is_devour]
        if reward:
            self.say("—— 有收益：得种 / 击杀回血（排在噬魂之后、普通攻击之前）——")
            for p in self._yangtao_order(reward):
                # 【v0.70】★ 死了也能出手（同 ⑤，推翻 v0.34 攻方侧）。
                # 【v0.37】★★ summary=True —— 杨桃类连击战报按【目标】压成一行
                #   （用户 2026-09-30：太长太冗余；"打了两个目标可以分两行"）。
                self._resolve_plan(p, summary=True)
        # 【2026-10-25】01 种子（+1 种）并进这一批：它和"造成伤害得种"是同一语义，
        #   不该在声明期就发。副作用（唯一一处挪了会改结果）：噬魂在这一步之前，
        #   所以本回合刚出的这 1 颗当回合抢不走 —— 已记规则文档 #113。
        for p in self._appliers():
            spec = p.field.spec
            if spec.seed_gain:
                _who = self._holder(p)
                _who.seeds += spec.seed_gain
                self.say("%s 的【%s】→ 获得%d颗种子（现有%d）%s"
                         % (p.tag, spec.name, spec.seed_gain, _who.seeds,
                            "" if _who is p else
                            "（这一株已归 %s 支配 → 种子记在 %s 账上）"
                            % (_who.tag, _who.tag)))
        # ⑦ 普通攻击【v0.28：序列优先 + 死了不打】
        #   ★ 顺序 = 编号降序 → spec.priority → 随机，与噬魂 / 杨桃类【同一套】
        #     （用户 2026-09-22："你可以暂且按序列弄一个优先级"）。
        #     为什么现在顺序才要紧：伤害逐株落地，先手就是实打实的先手。
        #   ★ 死了不打：轮到他时人已经出局 → 这颗子弹就不打了
        #     （用户 2026-09-22 明确选的口径）。
        #     v0.9~v0.27 这里写的是"不再过滤 alive —— 开火那一刻大家都还活着"，
        #     那条已被本版推翻，别再照旧注释改回来。
        self._hook_pass("attack")
        self._feedback_pass("attack")   # 【v0.58】反馈先于这一步自己的效果判
        normal = [p for p in self.plans
                  if not p.field.spec.attack.is_devour
                  and not is_reward_class(p.field.spec)]
        if normal:
            self.say("—— 普通攻击（按序列优先级）——")
        for p in self._attack_order(normal):
            # 【v0.70】★ 死了也能出手（推翻 v0.34）。
            self._resolve_plan(p)
        # ★★【v0.70】[聚电]收为己用 —— 被收走那一株的攻击，由支配者兑现。
        #   做法：临时把它的 inert 放行，走【同一条】_resolve_plan
        #   （削防 / 破防 / 闪电链 / 溅射 / 收益全都自动对上，不另写一份）。
        #   放在普通攻击【之后】：用户原话"轮询闪电草的攻击（电系攻击）"排最后。
        for f, boss, kind, _v, _c in getattr(self, "absorbed_attacks", []):
            owner = f.player
            if f.cut_gone or f.spec is EMPTY or f.player.field is not f:
                continue
            # ★【v0.71】[聚电]声明过、但最后没真收成（被抢走 / 目标被消除）→ 不打。
            #   声明阶段只能"乐观地"问（谁抢得到要等分配），兑现时这里兜底。
            if not getattr(f, "absorbed", False):
                continue
            if boss.field is None or not boss.alive:
                continue
            was_inert, was_bao = f.inert, f.bao
            f.inert = False
            # ★ 恢复它自己的攻击力（成空时被清零了，见 _absorb_resolve_phase）
            f.bao = getattr(f, "bak_bao", f.bao)
            try:
                self.say("—— [聚电]收为己用：%s 的【%s】由 %s 支配出手 ——"
                         % (owner.tag, f.spec.name, boss.tag))
                self._resolve_plan(owner)
            finally:
                f.inert, f.bao = was_inert, was_bao
        # ⑧【v0.10】火种爆发：所有攻击结算完之后，额外挨一次削防 4 的攻击。
        #     放在这里（而不是释放的瞬间）是因为它是"额外的"—— 排在正常攻击之后。
        for p in self.players:
            if p.fire >= FIRE_BURST_AT:
                self._fire_burst(p)
        # ⑨【v0.16】人参果爆发：段标记累积≥3 → 本回合场上该段植物全体吃穿透伤害。
        #     与火种爆发一样排在正常攻击之后。
        self._hook_pass("burst")
        self._feedback_pass("burst")   # 【v0.58】反馈先于这一步自己的效果判
        self._tag_burst()
        # ⑩【v0.39】猫尾草。★★ 这里是【环相关机制】该来排队的位置 ——
        #   用户 2026-09-22："到时候你就可以把溅射之类涉及环的放在最后。"
        #   到这一步，本回合所有伤害都已经落地、该死的人都已经退出存活环，
        #   从这里往后挂的机制看到的就是【圈的最新样子】。
        #   【v0.39】猫尾草就挂在这里 —— 用户 2026-10-04："猫尾草应该最后攻击
        #   （你最好也排个顺序）" + "所有攻击（含火种/段爆发）之后"。
        #   理由：它要打的"离自己最近的活人"随环实时变化，必须等全场打完再定。
        self._hook_pass("cat")
        self._feedback_pass("cat")   # 【v0.58】反馈先于这一步自己的效果判
        self._resolve_cat_tail()
        # 【v0.57】[勾取] 兜底：到这一步还没兑现的（比如它那一步根本没跑到），
        #   一律在这里收掉 —— 不让"被勾"的植物留到下一回合。
        self._hook_pass(None)
        #   （溅射仍在 v0.28 被【暂时隐藏】，见 plants_data 里 SPLASH_* 的注释。）
        # ⑪【v0.54】[免疫] —— 结算的【最后一步】（原稿 p44「结束回合」那一行）。
        #   ★ 必须排在这：它清的是"下回合才要用"的负面层，本回合的钱早在出牌时
        #     就付完了，清早清晚都不影响本回合 —— 于是照原文次序表排在最后。
        #   ★ 连 say_state 也在它之后：战报里公示的场面要已经是"清干净"的样子。
        self._immune_resolve_phase()
        self.say_state()  # 结算后公示最新场面（图形客户端靠它刷新面板）

    def _resolve_cat_tail(self):
        """【v0.39】猫尾草：排在【所有攻击（含火种 / 段爆发）之后】出手。

        ★ 为什么必须最后（用户 2026-10-04）：
          "目标只能是最靠近自己的仍有活着者" —— 存活环是实时的，
          别人死的死、退环的退环，它两侧的"最近"到这一刻才定型。
        ★ 声明阶段已把"打谁 + 各几发"写在 plans 里（左右两侧）；
          到这里【重新按当前环确认】：把每发按声明顺序映射回"当前两侧"——
            · 该发所指的人此刻还活着、且仍在环上两侧 → 保留；
            · 否则该发【作废】（与"死了不打"同一精神），不转嫁、不打尸体。
          声明时环上没活人而推迟的（cat_tails），此刻两侧重新算（其 plans 为空）。
        """
        cat_ps = [p for p in self.plans
                  if p.field.spec.nearest_target and p.alive and p in self.released]
        if not cat_ps:
            return
        self.say("—— 猫尾草（排在所有攻击之后，按当时的存活环结算）——")
        # 顺序仍按序列优先级（与普通攻击 / 噬魂同一套）
        for p in self._attack_order(cat_ps):
            # 【v0.70】★ 死了也能出手（推翻 v0.34）。
            n = p.field.spec.attack.lian if p.field.spec.attack.lian > 1 else 1
            sides = self.ring_neighbors(p, exclude=p)
            if not sides:
                self.say("  · %s 的【%s】：环上已无活人可打，不出手"
                         % (p.tag, p.field.spec.name))
                continue
            side_set = set(sides)
            wanted = self.plans.get(p) or []
            if not wanted and len(sides) == 1:
                # 声明时没人、现在有一侧了 → 全给这一侧
                wanted = [sides[0]] * n
            # 逐发映射：只在"此刻仍活着且在环上两侧"时才保留
            tg = [t for t in wanted if t.alive and t in side_set]
            if not tg:
                self.say("  · %s 的【%s】：原定目标已不在场上，不出手"
                         % (p.tag, p.field.spec.name))
                continue
            cnt = {}
            for t in tg:
                cnt[t.idx] = cnt.get(t.idx, 0) + 1
            self.say("%s 的【%s】→ %s"
                     % (p.tag, p.field.spec.name,
                        "、".join("%s %d发" % (t.tag, c)
                                  for t, c in [(s, cnt[s.idx]) for s in sides
                                               if s.idx in cnt])))
            self.plans[p] = tg
            self._resolve_plan(p)

    # ---------- 【v0.54】[免疫]：结算的【最后一步】----------
    def _immune_resolve_phase(self):
        """[免疫]（84 免疫菇）—— 每个回合的【最末】清一次账。

        ★★ 落点依据（用户 2026-10-21）：原稿词条写的是"回合最初"，但那是笔误 ——
          "应该是笔误，你改成'回合最末'吧。其实你看原稿结算顺序里面免疫也是放在最后"。
          p44「进行次序」表的最后一行就是 `结束回合：… [免疫] …`，
          所以它排在【所有攻击（含火种爆发 / 段爆发 / 猫尾草）之后】。

        ★ 清谁：免疫者【自己 + 同一支队的所有存活者】。队伍走 teams 并查集
          （same_team）—— 与 [复活] / 猪笼草那条"队友链"是同一个概念，
          不另起一套"直接队友"。

        ★ 清什么（本版）：[倍耗] 与 [鱼腥味]，**连同"下回合才生效"的待入账层**
          （*_next）。理由：这两本账描述的都是"身上已经挂了的东西"；只清生效账、
          放着待入账不管的话，它下一回合照样并进来 —— 免疫等于白做一次。

        ★ 只在免疫者【还活着】时执行（死人不替队友清账），他死的那些回合计数
          也不递减 —— 与超防"死亡不清状态"的处理一致。

        ★ 计数：拿到手 = spec.immune_turns（免疫菇 = 3 = 当局＋下两局），
          每执行一次 -1、减到 0 到期。于是 R 回合释放 → R / R+1 / R+2
          三个回合的最末各清一次，正好三次。
        """
        for p in self.players:
            if not p.alive or p.immune_turns <= 0:
                continue
            team = [x for x in self.players if x.alive and self.same_team(x, p)]
            cleared = []
            for x in team:
                nm = x.mult + x.mult_next
                nf = x.fishy + x.fishy_next
                if not nm and not nf:
                    continue
                # 四本账一起清（生效账 + 待入账账），与 _sweep_ally_loads 同一套字段
                x.mult_by = {}
                x.mult_next_by = {}
                x.fishy_by = {}
                x.fishy_next_by = {}
                bits = []
                if nm:
                    bits.append("倍耗%d层" % nm)
                if nf:
                    bits.append("鱼腥味%d层" % nf)
                cleared.append("%s 的 %s" % (x.tag, "/".join(bits)))
            if cleared:
                self.say("—— 【免疫】%s%s → 回合末清除：%s ——"
                         % (p.tag,
                            "（同队共 %d 人）" % len(team) if len(team) > 1 else "",
                            "；".join(cleared)))
            p.immune_turns -= 1
            if p.immune_turns == 0:
                self.say("—— %s 的【免疫】已到期（本回合是最末一次清除）——" % p.tag)

    def resolve_hit(self, attacker, defender, atk=None):
        """一次命中的结算，判定顺序：
           植物免疫 → 同级免疫 → 属性免疫 → ★超防闸门【v0.23】
           → 无视标签 → 特殊克制 → 噬魂 → 普通削防/破防
           ★ 超防闸门的位置是规则的一部分（必须早于削防），见规则文档第五章 5.5。

        【v0.27】两处变化：
          · 返回值 = 这一击【有没有造成伤害】（True/False）。溅射（"造成伤害后"）
            与杨桃类（"造成伤害得种"）都以它为准 —— 被免疫 / 被超防挡下 /
            只削了防没打穿，一律 False。★ 判定点只此一处，别在调用方另算一遍。
          · atk 参数 = 用【别的】攻击数据来打（溅射用：暴2·伤0.5，
            见 Attack.splash_attack）。不传就是攻方自己的主攻击（老行为）。
        ★【v0.26】"已打穿"这一步【不再短路】（用户 2026-09-21 拍板）：
          植物一旦被打穿，本回合它就【不再挡】—— 多余的子弹（连击 N 的后续发、
          以及本回合别人的补刀）继续走完整判定链，到【削防】那步时防御已是负数，
          于是每一发都算"打穿"、每一发都落在玩家身上。
          旧写法（2026-09-01 起）是第一发打穿后把后面的全 return 掉，
          结果是"连击 5 打一个裸奔的对手也只掉半条命"，连击数完全浪费
          （用户 2026-09-21 从战报里抓出来的：水滴草连击2 只掉了半条命）。
          ⚠️ 只取消"短路"，判定顺序（5.5 的 ②~⑧）一字未动。"""
        a = atk if atk is not None else attacker.spec.attack
        d = defender.spec.defense
        desc = "%s的【%s】→ %s的【%s】" % (attacker.player.tag, attacker.spec.name,
                                            defender.player.tag, defender.spec.name)
        # 【v0.7】没释放的植物所有属性归零 → 免疫和标签同样失效（地盾不释放就挡不住平/上）
        immune = () if defender.inert else d.immune_attrs
        tags = () if defender.inert else d.tags
        immune_names = () if defender.inert else d.immune_plants
        # 【v0.9】植物免疫（防御端的"特殊克制"）：守方免疫攻方这株植物 → 完全无效。
        #         大喷菇靠这条免疫小喷菇 —— 它未充能时防御是 0，不免疫就会被小喷菇一击秒。
        if attacker.spec.name in immune_names:
            self.say("%s：【%s】免疫【%s】的攻击，毫无效果"
                     % (desc, defender.spec.name, attacker.spec.name))
            return False
        # ★★【v0.72】成[空] 的一方【不算同名】—— 先于同级免疫判一次。
        #   用户 2026-10-27 拍板："成[空] 就不算同名"；他 2026-10-22 也早定性：
        #   **成[空] = 不存在**。两株同名植物里只要有一株已经成[空]，就不是
        #   "两株同种植物互打"了 —— 空牌既没有防御、也不是那株植物。
        #   实测来源：牛角菱勾走两株聚电蓝莓后，3号打2号那一下仍被判同级免疫，
        #   于是"三株都挨打只死一株"。
        #   ★ 别把"主动不释放"算进来：那只清属性（场面写"未释放·属性归零"），
        #     牌还在、人还在，仍然是那株同名植物。
        if (attacker.spec.name == defender.spec.name
                and (self._is_empty_field(attacker) or self._is_empty_field(defender))):
            self.say("%s：%s已成[空] → 不再是\"两株同名植物\"，同级免疫不适用"
                     % (desc, ("守方" if self._is_empty_field(defender) else "攻方")))
        # 【v0.8】同级免疫（你叫它"自无视"）：同种植物互相伤害不了
        #         （你举的例子：两个小喷菇之间互相打不动）。
        #         例外：攻击方"特殊克制"防守方时照打 —— 见下面的 ignore_plants 分支。
        # 【v0.27】溅射也走这里：同一株植物的溅射打在同名植物身上同样免疫
        #         （"走完整判定"的自然结果，没有为溅射开后门）。
        if (attacker.spec.name == defender.spec.name
                and defender.spec.name not in a.ignore_plants
                and not self._is_empty_field(attacker)
                and not self._is_empty_field(defender)):
            self.say("%s：两边都是【%s】，同级免疫，互相伤害不了" % (desc, defender.spec.name))
            return False
        if a.attr in immune:
            self.say("%s：%s无视了%s属性攻击，毫无效果" % (desc, defender.spec.name, a.attr))
            return False
        # 【v0.39】★★ [绝缘性-铜]（木盾）—— 免疫【一切电系伤害】。
        #   原稿 p2 木盾词条：`木盾　回　抵挡圈围的暴击，绝缘性-铜　[固有克制-①]　0　4`。
        #   用户 2026-09-30 口径："木盾免疫所有电系伤害（闪电链的伤害也属于电系伤害）"。
        #   ★ 判据 = 守方 [绝缘性-铜] ∧ 这一击是电系（a.electric）——
        #     电系包括 [闪电一击] 本体（.flash）与派生出来的 [闪电链]（.electric）。
        #   ★ 返回 False = 【没造成伤害】→ 上游 `if dealt:` 不成立 → 弹链自然不发生
        #     （正合原稿"[因绝缘]而无法[破防]时【损失链点】"）。木盾【本身仍可作为
        #     链点被瞄准】，只是打不出伤害、于是断链 —— 无需额外写"断链"代码。
        #   ★ 固有属性：守方被切成 [空] 后防御没了，但 field.spec.defense.insulate
        #     还在（to_empty 不改 spec）；主动【不释放】则守方 inert → 绝缘失效
        #     （同"未释放 = 属性归零"，见下面 immune/tags 的 inert 口径）。
        #     ⚠️ 所以这里读的必须是 inert 判定后的守方状态。
        if not defender.inert and getattr(d, "insulate", False) and a.electric:
            self.say("%s：【%s】的【绝缘性-铜】免疫电系伤害 → 这一击毫无效果"
                     "（不削防、不破防——打不出伤害，链点随之损失）"
                     % (desc, defender.spec.name))
            return False
        # 【v0.23】★★ 超防闸门 —— 必须排在【削防】和"绕过防御直击"之前。
        #   你 2026-09-20 报的 bug：仙桃挂着超防，杨桃照样把它的防御从 6 削到 1。
        #   根因：超防原先只拦在 _hurt（掉血那一步），而削防排在它前面 ——
        #   护罩还在、植物的防御却被磨光了，成了"漏水的护罩"。
        #   现在的口径（你 2026-09-20 拍板"全部挡下"）：只要攻击方【没有显式标注
        #   "无视超防"】，这一击整个作废 —— 不削防、不破防、不掉血。
        #   连下面那两条"绕过防御直击玩家"的分支（无视标签 / 特殊克制）和噬魂
        #   也一并作废：它们同样属于"普通的那一类"（都没有那个标注）。
        #   能绕过去的是原稿里单独标了"无视超防"的株（06 人参果 / 102 茄子加农炮 /
        #   106 捕蝇草 / 109 火龙果 / 94 毒雾菇）—— 它们在 Attack 上写 super_pierce=True。
        if self.super_def_blocked(defender.player, a.super_pierce):
            self.say("%s：%s 处于【超防】状态（还剩 %d 回合）→ 这一击被完全挡下"
                     "（不削防、不破防、不掉血）"
                     % (desc, defender.player.tag, defender.player.super_def))
            return False
        # 【v0.33】★★【免切①者穿透金刚刀叶的防御】—— 原稿 p06 金刚刀叶防御栏写的是
        #   「20（除使不可切/无视）」：那 20 点防御对"不可切者"（= 有免切①的植物）
        #   是【不适用】的。用户 2026-09-29 实测发现没实现，拍板口径：
        #     "有免切①者穿透其防御" + "等同【穿透】语义"。
        #   ★ 判据 = 攻方有免切① ∧ 守方是金刚刀叶（跨植物判定，不写在任一株身上）：
        #     · 攻方的免切①读【攻方当前前段】（_cur_seg，与刀叶射程/免切判定同一入口）；
        #     · 只对【金刚刀叶】生效（守方判据用 is_jingang，与 _is_uncuttable 对称）。
        #   ★ 效果 = 穿透语义：视目标防值为 0，直接按伤害落地，不走"削防→破防"
        #     （与"无视标签 / 特殊克制"那两条分支等价：broken + 直击玩家）。
        #   ★ 位置：排在【超防闸门之后】—— 超防仍然是硬闸门（#55 长挂，未结案）；
        #     排在【无视标签 / 特殊克制之前】—— 那两条是攻方对守方的另一套关系，
        #     互不排斥（谁先满足谁先返回，效果相同）。
        if is_jingang(defender.spec) and \
                attacker.spec.has_uncut(1, self._cur_seg(attacker)):
            defender.broken = True
            self.say("%s：【%s】持有【免切①】→ 无视【%s】的防御（视其防值为 0），"
                     "直击 —— %s"
                     % (desc, attacker.spec.name, defender.spec.name,
                        self._hurt_text(defender.player, a.damage, a.super_pierce)))
            self.fire("on_break", defender, {"attacker": attacker})
            self._dealt_damage(attacker)
            return True
        # 【v0.24】用 a.ignored_tags()（= 属性通用表 ∪ 本株声明），不再直接看
        #   a.ignore_tags —— 否则"上属性无视木"就只对声明过的株生效（旧 #59）。
        if set(tags) & set(a.ignored_tags()):
            defender.broken = True
            if a.is_devour:
                # 【v0.21】噬魂类走到这条 = 目标的防御已被属性绕过（上无视木），
                #   所以"暴 > 防御"这道门槛【不再卡它】—— 直接按吞噬结算。
                self.say("%s：%s属性无视%s系防御 → 噬魂的大门敞开"
                         % (desc, a.attr, "/".join(tags)))
                return self._devour_hit(attacker, defender, bypass=True)
            self.say("%s：%s属性无视%s系防御，直击玩家 —— %s"
                     % (desc, a.attr, "/".join(tags),
                        self._hurt_text(defender.player, a.damage, a.super_pierce)))
            self.fire("on_break", defender, {"attacker": attacker})
            self._dealt_damage(attacker)
            return True
        # 【v0.8】特殊克制：无视指定植物的防御（按植物名判定，效果与"无视标签"相同）
        if defender.spec.name in a.ignore_plants:
            defender.broken = True
            if a.is_devour:
                self.say("%s：【%s】特殊克制【%s】，防御形同虚设 → 噬魂的大门敞开"
                         % (desc, attacker.spec.name, defender.spec.name))
                return self._devour_hit(attacker, defender, bypass=True)
            self.say("%s：【%s】特殊克制【%s】，无视其防御直击 —— %s"
                     % (desc, attacker.spec.name, defender.spec.name,
                        self._hurt_text(defender.player, a.damage, a.super_pierce)))
            self.fire("on_break", defender, {"attacker": attacker})
            self._dealt_damage(attacker)
            return True
        # 【v0.21】噬魂（吞噬）类：到这里就是"防御还在"，按规则要拿暴值当门槛 ——
        #   暴 > 有效防御 才吞得动；【不削防】。规则见第十二章。
        if a.is_devour:
            return self._devour_hit(attacker, defender, bypass=False)
        # 【v0.9】叠暴 diebao 暂不启用：连击的每一发都用 bao 削防。
        # 【v0.30】削防值一律读【攻方的当前暴击值】：充能植物已在充能阶段把
        #   field.bao 直接改成充能数（见 phase ②），所以这里没有任何"第一击特判" ——
        #   充能与否、打第几发，用的都是同一个数。atk 不为 None 时（溅射派生的那一击）
        #   用派生数据自己的 bao（= 暴2），不走 field.bao。
        cut = a.bao if atk is not None else attacker.bao
        # 【v0.23】能走到这里 = 守方【没有】超防，或者攻击方标了"无视超防"。
        #   所以这里不用再判超防 —— 闸门统一在上面那一处（super_def_blocked）。
        defender.fang -= cut
        if defender.fang < 0:
            # 【v0.26】"打穿"可以在一回合里发生【多次】：第一次是把防御打穿，
            #   之后每一发都只是"往已经破了的地方再补一下"（防御已是负数）。
            #   on_break 钩子按【第一次】算 —— 一株植物本回合只"破"一次，
            #   别让补刀的每一发都去触发一遍。
            first_break = not defender.broken
            defender.broken = True
            if first_break:
                self.say("%s：削防%d，防御被打穿！%s"
                         % (desc, cut, self._hurt_text(defender.player, a.damage,
                                                       a.super_pierce)))
                self.fire("on_break", defender, {"attacker": attacker})
            else:
                self.say("%s：防御本回合已被打穿（多出来的子弹不再被它挡）→ "
                         "这一发也直接落在人身上！%s"
                         % (desc, self._hurt_text(defender.player, a.damage,
                                                  a.super_pierce)))
            self._dealt_damage(attacker)
            return True
        else:
            self.say("%s：削防%d，剩余防御%d" % (desc, cut, defender.fang))
            return False

    # ---------- 【v0.21】噬魂（吞噬）类 ----------
    def _devour_hit(self, attacker, defender, bypass):
        """一次【吞噬】的结算。规则见 游戏规则总结.txt 第十二章。

        它与普通攻击有三点根本不同（缺一条就不是噬魂了）：
          ① 【不削防】—— 整套暴值都不参与普通削防（原稿 p28 的[不叠暴]）；
          ② 暴值改当【门槛】：暴 > 目标有效防御，吞噬才成立；否则本击什么也不发生；
          ③ 顺序：先于普通攻击结算（见 phase_settle），所以夺种按"当时剩余"算。

        另有两条口径：
          · 有效防御 = 当前防御 − 水滴补出来的那部分（原稿 p22 页标题
            "植之噬魂（无视[水滴]）"）；
          · 目标"预演血量"（现有生命 − 本回合已登记的伤害）≤ 0 → 什么都拿不到
            （你 2026-09-20 拍板："前面生命打到0之后，后面种也夺不了"）。

        bypass=True 表示"防御已被绕过"（上属性无视木 / 特殊克制）——
        那种情况下暴门槛不再卡它，但吞的其他环节一个不少。
        """
        a = attacker.spec.attack
        spec = attacker.spec
        tgt = defender.player
        me = attacker.player
        # ★【v0.72】这一株若已被[勾取]，夺来的种 / 吸来的血归**支配者** ——
        #   与 [毁灭] 的 ⌊2m/n⌋、[反馈湮灭] 的 9 颗种同一条规则。
        recip = self._holder(me)
        desc = "%s的【%s】→ %s的【%s】" % (me.tag, spec.name,
                                           tgt.tag, defender.spec.name)
        # ① 目标还活着吗 —— 按【已登记的伤害】预演，而不是等回合末真扣血
        after = tgt.hp - self._pending_dmg.get(tgt, 0)
        if after <= 0:
            self.say("%s：目标已经被打到 0 血 → 噬魂落空（伤与种都拿不到）" % desc)
            return False
        # ② 门槛：暴值必须【大于】有效防御
        if bypass:
            eff, why = 0, "防御已被绕过"
        else:
            eff = max(0, defender.fang - defender.fang_aux)
            why = "有效防御%d%s" % (eff, "（已扣掉辅助防 P₁ 那部分）"
                                     if defender.fang_aux else "")
        if a.bao <= eff:
            self.say("%s：暴%d 不足以压过%s → 吞噬不成立"
                     "（噬魂类不削防，这一击什么也不发生）" % (desc, a.bao, why))
            return False
        # ③ 伤害：照走 _hurt —— 所以【超防】照样拦得住。
        #    不过注意【v0.23】：超防闸门其实已经提前到 resolve_hit 开头了，
        #    正常情况下根本进不到这里；_hurt 这道是"双保险"，留给直接调用它的路径。
        #    super_pierce 要跟着传下去，否则标了"无视超防"的噬魂植物会被自己的
        #    _hurt 挡掉（闸门放行、掉血又被拦 = 自相矛盾）。
        #    【v0.40】144 龙舌兰"有多少吞多少"：杀伤 = 目标【全部剩余生命】，
        #      回血 = 这口【实际扣掉的生命】（半条命就只回半条），夺种 = 【全部种子】。
        #      普通噬魂（65/76）维持 伤1 恒定 + 按比例夺种 + 恒定回血。
        if a.devour_full:
            bite = after                      # 目标全部剩余生命（预演血量，恒 > 0）
            heal = bite                       # 回血 = 实际丧失的生命
            take = tgt.seeds                  # 夺种 = 全部种子
        else:
            bite = a.damage                   # 恒定伤 1 条命
            heal = a.devour_heal              # 恒定回血
            take = int(tgt.seeds * a.devour)  # 按比例夺种，向下取整
        if not self._hurt(tgt, bite, super_pierce=a.super_pierce):
            self.say("%s：暴%d 压过了%s，但目标的【超防】把这一口整个挡下"
                     "（既没伤到，也没夺到半个种子）" % (desc, a.bao, why))
            return False
        # ③b【v0.48】这一口会不会把人打死？会 → 记一笔"死于噬魂"，
        #    等 _flush_damage 真正扣空时，[瓜分] 才会多出"噬魂者"那一池。
        #    ★ 用【预演血量】判（after 已在 ① 算好），不看 hp —— 因为伤害还没落地。
        if after - bite <= 0:
            self.death_by_devour.add(tgt)
            # 【v0.51】★ 死亡快照必须抢在 ④ 夺种【之前】拍 ——
            #   用户原话"瓜分/噬魂之类的不影响死亡时的种子"。
            #   这一行写在 ④ 下面就是把噬魂吃掉的那部分也抹掉了（复活的种子变少）。
            self._mark_death(tgt)
        # ④ 夺种
        tgt.seeds -= take
        recip.seeds += take
        # ⑤ 吸命：伤1回1，【立刻】回给自己。
        #    ★ 不看目标还剩多少血 —— 你的原话"即使对方只有半条命也是回1"，
        #      所以这里恒为 spec 里的 devour_heal。
        #    ★ v0.40 龙舌兰是例外：它按【实际丧失的生命】回血（用户口径
        #      "一个半条命的人，食人花能加1血，龙舌兰只能半血"）。
        recip.hp += heal
        if a.devour_full:
            self.say("%s：暴%d > %s → 吞噬成立（有多少吞多少）！%s 丧失【全部生命】"
                     "（%s），夺走其【全部种子】%d颗（%s 剩0颗）；"
                     "%s 获得其丧失的全部生命，回血%s（现有%s）"
                     % (desc, a.bao, why, tgt.tag, self.hp_str(bite), take,
                        tgt.tag, recip.tag, self.hp_str(heal), recip.hp_text())
                     + ("" if recip is me else
                        "（这一株已归 %s 支配 → 收益归它）" % recip.tag))
        else:
            self.say("%s：暴%d > %s → 吞噬成立！%s 损失%s点生命，夺走%d颗种子"
                     "（%s 剩%d颗）；%s 回血%s条命（现有%s条命）"
                     % (desc, a.bao, why, tgt.tag, self.hp_str(bite), take,
                        tgt.tag, tgt.seeds, recip.tag, self.hp_str(heal),
                        recip.hp_text())
                     + ("" if recip is me else
                        "（这一株已归 %s 支配 → 收益归它）" % recip.tag))
        # 【v0.27】返回值 = 这一击有没有【造成伤害】（给溅射 / 杨桃类用）
        self._dealt_damage(attacker)
        return True

    def _devour_order(self, players):
        """【v0.21】吞噬的先后顺序（你 2026-09-20 给的三条规则）：

        ① 不同植物比【编号】：大者先吞 —— 你原话"序列的大小一般能够判断优先级，
           在此例中，食人花明显大于冬虫夏草"（76 先于 65）；
        ② 同一株植物之间【随机】—— 你原话"原本是按石头剪刀布，现在可以改为
           抽奖之类的，反正是随机"；
        ③ 目标被打到 0 血之后，后来者什么都拿不到 —— 这条不在排序里，
           在 _devour_hit 的第 ① 步判。

        随机的那个 tiebreak 走 self.rng（可注入），测试才能复现。
        """
        return sorted(players,
                      key=lambda p: (-self._no_num(p.field.spec.no),
                                     self.rng.random()))

    def _yangtao_order(self, players):
        """【v0.27】杨桃类的先后顺序（用户 2026-09-21 给的两条）：

        ① 先比【序列】降序 —— 与噬魂类一致（"序列"就是这套系统里的通用优先级，
           大者先，见 10.x 的"人参果按编号第一字段"）；
        ② 序列相同 → 看 spec.priority（声明字段，大者先）—— 你说的"如果序列相同再声明"；
        ③ 还相同（同一株植物之间）→ 随机，与 _devour_order 的同株随机一致。
        """
        return sorted(players,
                      key=lambda p: (-self._no_num(p.field.spec.no),
                                     -p.field.spec.priority,
                                     self.rng.random()))

    def _attack_order(self, players):
        """【v0.28】普通攻击的先后顺序 —— "暂且按序列"那一条。

        用户 2026-09-22："你可以暂且按序列弄一个优先级，这样到时候你就可以把
        溅射之类涉及环的放在最后。"

        排序键 = 编号降序 → spec.priority → 随机（与杨桃类同一套）。
        ★ 为什么"顺序"本版才成为真问题：伤害改成【逐株立刻落地】之后，
          排在前面的人是真的先把人打死、被打死的人就没机会还手了
          （v0.9~v0.27 同时结算时，顺序只影响战报的先后）。
        ★ "暂且"是用户的原话 —— 这套键将来大概率要换（比如按声明、按座位），
          所以别把它当不可动摇的规则写进文档的"已确认"里。
        ★ 同一株之间仍是随机（与噬魂 / 杨桃类一致）；随机源是可注入的 self.rng，
          多人局的顺序才可能复现（同 Test56b）。
        """
        return sorted(players,
                      key=lambda p: (-self._no_num(p.field.spec.no),
                                     -p.field.spec.priority,
                                     self.rng.random()))

    def _resolve_plan(self, p, summary=False):
        """【v0.27 建，v0.28 改，v0.34 补"预演血量"，v0.35 改 break】逐发结算命中，收尾一次落地。

        ⑤ 噬魂 / ⑥ 杨桃类 / ⑦ 普通攻击【共用】这一段 —— 三批的行为必须
        一模一样，差别只在"什么时候轮到它"，所以别抄成三份（抄三份的下场：
        某天给某个机制加个例外，只改了其中一处）。

        【v0.28】两处变化：
          · 逐发命中后【不再等 ③】—— 这一株所有子弹打完，就地 _flush_damage()
            兑现（"每个植物完成攻击后就结算一次"）。
            所以一株的 5 发连击仍然是"一起落地"的：先全登记、再一起扣，
            _devour_hit 里的"预演血量"（⑤ 落空判定）也靠这个才有意义。
          · 目标可能在本回合【已经出局】（被前面的人打死了）→ 这一发直接落空：
            尸体不再挨打，也不去动他的防御。

        【v0.34】★★ 补上"同一株的多发之间"的【死了不打】—— 你 2026-09-29 报的 bug：
          一株自己连击 N 发时，第 1 发就把目标打空了，可 `t.alive` 要等整株算完
          （_flush_damage）才翻 False → 第 2~5 发照样打在尸体上、还逐发触发
          _dealt_damage（杨桃类因此白拿 4 颗种子）。
          修法 = 用【预演血量】判"已死"：目标当前生命 − 本回合已登记伤害 ≤ 0
          → 这一发落空。⚠️ 这正是 _devour_hit 开头一直在用的写法，
          这处只是【漏了复制】（见规则文档 #87 / 开发文档 v0.34 段）。
          ★ 判据放这里 = ⑤/⑥/⑦ 三批【一处统一】（"唯一出口"，别抄三份）。

        【v0.35】★★ continue → break：目标一死，本株【剩余子弹全部不发】。
          v0.34 虽然拦住了"打在尸体上、白拿种子"，但写法是逐发落空、连写 5 行
          一模一样的日志 —— 你 2026-09-30 原话："死了不打你不会 break 吗？
          或者日志里面就别写了"。所以：判一次 → 写一行（带剩余发数）→ break。
          ⚠️ 注意 break 只影响【这一株自己的发射序列】，不影响别的株、也不影响
          本株对其他目标 —— 单挑局多发打同一人，就是这条规则的主场。
        """
        attacker = p.field
        # ★【v0.70】这株植物【本身不存在】→ 不出手。
        #   成[空]（inert，被[聚电]/[毁灭]等）和已被切除（cut_gone）都是"不存在"。
        #   ⚠️ 原来【没有这一行】：用户 2026-10-26 的四人局实测，被聚电成[空]的
        #     闪电草照样打出了"削防0"（因为 fang 已被清零）。日志里它明明写着
        #     "成[空]"，下一步却又出手 —— 自相矛盾。
        #   ★ 千万别和"死了也能出手"搞混：那个说的是【人】出局了植物照打；
        #     这一条说的是【这片植物本身】已经被打成空/被切没了。两回事。
        if attacker is None or attacker.inert or attacker.cut_gone:
            return
        self.fire("on_attack", attacker, {})
        targets = self._plan_targets(p)
        # 【v0.37】★★ summary = 把"逐发明细"压成"按目标一行"。
        #   用户 2026-09-30 原话："杨桃这类连接植物的日志太长太冗余，压缩到打了
        #   具体某个人几发就可以"，并明确"打了两个目标可以分两行，按目标算行数"。
        #   ★ 做法：逐发的明细仍照常【算】（一行不改规则），只是把 say() 憋进
        #     _quiet 里不发；同时在下面记账（几发 / 削防多少 / 破没破防 / 掉多少血），
        #     收尾时按【目标】分组，一个目标一行。
        #   ★ 落地的"损失 X 条命"那句【不压】—— 它在 _flush_damage 里，是
        #     玩家最需要看到的结论，压掉会看不懂到底掉没掉血。
        verbose = not summary
        stats = {}          # tidx -> {"hits":n, "cut":x, "broke":bool, "dmg":y}
        quiet_buf = self._quiet
        if summary:
            self._quiet = []
        # 【v0.41】★★ 死了不打：已经播报过"这个目标死了"的 tidx 集合。
        #   同一个目标只播一行（不逐发刷屏），但【继续打别的目标】。
        dead_reported = set()
        for k, t in enumerate(targets):
            # 【v0.41】★★ 死了不打 —— 只掐【这一个目标】的剩余发数，别掐整株！
            #   ❌ v0.35 的写法是 `break`（掐掉本株全部剩余子弹）—— 单挑看不出问题，
            #      但连击分给多人时，打死第一个人会把【后面还活着的目标】的子弹一起
            #      吞掉。3 人局实测：杨桃 3发打p2 + 2发打p3，p2 死后 p3 一发没挨到。
            #      （旧 Test79b 把血拉满 20 就是为了"别触发 break"，正好绕开了它。）
            #   ✅ 现在：该目标已死 → 跳过它剩下的发数、【continue 打下一个目标】。
            #      两种"死"都要跳：
            #        · t.alive = False  → 跨株/跨批打的（已经 flush 过、人已退出环）
            #        · _effectively_dead(t) → 本株前面几发打空的（还挂在 _pending_dmg）
            #   ★ 每个目标只播一行（发数 = 【该目标自己】的剩余发数），不逐发刷屏 ——
            #     这样单挑连击 5 打 1 条命仍然只写 1 行（保留 v0.35 的初衷）。
            reason = None
            if not t.alive:
                reason = "已出局"
            elif self._effectively_dead(t):
                reason = "已被打空（本株前面几发）"
            if reason is not None:
                if t.idx not in dead_reported:
                    dead_reported.add(t.idx)
                    rest = sum(1 for x in targets[k:] if x.idx == t.idx)
                    # 【v0.37】★ 这行【绕过 summary 静默】—— 它是"本株提前停手"的
                    #   重要结论，不是逐发明细（Test75e 就盯它）。
                    #   写法：临时把 _quiet 摘掉，说完再挂回去。
                    keep = self._quiet
                    self._quiet = None
                    self.say("  · %s %s → %s 的【%s】剩余 %d 发不再发出（死了不打）"
                             % (t.tag, reason, p.tag, attacker.spec.name, rest))
                    self._quiet = keep
                continue
            # ★ resolve_hit 的返回值 = 这一击有没有【造成伤害】。
            #   溅射与杨桃类的"+1种"都以它为准 —— 被免疫/超防挡下、只削防没打穿，
            #   都不算"造成伤害"，也就不触发。
            # 【v0.37】summary 时另抓一份"本发前 / 本发后"的差异来记账。
            if summary:
                st = stats.setdefault(t.idx, {"hits": 0, "cut": 0, "broke": False,
                                              "dmg": 0, "seeds": 0})
                fang0 = t.field.fang
                pend0 = self._pending_dmg.get(t, 0)
                sd0 = p.seeds
                dealt = self.resolve_hit(attacker, t.field)
                st["hits"] += 1
                st["cut"] += fang0 - t.field.fang
                if t.field.broken:
                    st["broke"] = True
                st["dmg"] += self._pending_dmg.get(t, 0) - pend0
                st["seeds"] += p.seeds - sd0
                if dealt:
                    self._splash(attacker, t.field, (p.idx, k))
                    # 【v0.38】[闪电一击]：造成伤害后弹一条[闪电链]。
                    if attacker.flash_chain:
                        self._flash_chain(attacker, t.field, (p.idx, k))
            else:
                if self.resolve_hit(attacker, t.field):
                    self._splash(attacker, t.field, (p.idx, k))
                    # 【v0.38】[闪电一击]：造成伤害后弹一条[闪电链]。
                    if attacker.flash_chain:
                        self._flash_chain(attacker, t.field, (p.idx, k))
        # 【v0.37】summary 收尾：按目标把刚才憋住的明细换成一行汇总。
        #   ★ 杨桃类的"+1 种"（_dealt_damage 的逐条播报）也被憋住了 ——
        #     所以汇总行必须把【共得几颗种】带上，否则玩家看不到收益。
        if summary:
            self._quiet = quiet_buf
            last = None
            for tidx, st in stats.items():
                who = next(t for t in targets if t.idx == tidx)
                bits = ["%d 发" % st["hits"]]
                if st["cut"]:
                    bits.append("共削防%d" % st["cut"])
                if st["broke"]:
                    bits.append("打穿防御")
                elif not st["cut"]:
                    # 一发都没削到防 = 全被挡下（免疫 / 超防 / 同级免疫…）。
                    # 明细被 summary 憋住了，这里必须补一句，否则玩家只看到"5 发"
                    # 却不知道发生了什么（Test61a 那种"超防 5 连挡"就是典型）。
                    bits.append("未造成任何伤害")
                # ★ 掉血不在这里报 —— 紧跟着的 _flush_damage 会写
                #   "⇒ X 损失 N 条命"，两处都写就重复了（v0.37 首版犯过）。
                if st["seeds"]:
                    bits.append("得 %d 颗种" % st["seeds"])
                    last = st["seeds"]
                self.say("%s 的【%s】→ %s：%s"
                         % (p.tag, attacker.spec.name, who.tag, "，".join(bits)))
            if last is not None:
                self.say("  （%s 现有种子 %d）" % (p.tag, p.seeds))
        # 【v0.41】击杀奖励（"有收益批"里"击杀回血"那一支，141 石榴机枪）：
        #   这一株把某个目标打死 → 每死一个回 heal_on_kill 半命（可叠加）。
        #   ★ 判"是不是它杀的"：本株开打【之前】目标还活着（hp>0）、
        #     _flush_damage 之后 hp==0（本株的伤害恰恰把它打空）。
        #     ⚠️ 逐株立刻落地 → 别人先前的补刀早已兑现、不可能算到它头上；
        #        目标若在它出手前就出局，压根进不了 targets（死了不打）→ 不算它的。
        _pre_hp = None
        if attacker.spec.attack.heal_on_kill:
            _pre_hp = {t.idx: t.hp for t in targets}
        # 【v0.28】★ 这一株打完了 → 立刻兑现（含"生命归 0 的人退出存活环"）
        self._flush_damage()
        if _pre_hp is not None:
            killed = []
            for t in targets:
                if _pre_hp.get(t.idx, 0) > 0 and t.hp <= 0:
                    _pre_hp[t.idx] = -1          # 去重：同一个目标只算一次
                    killed.append(t)
            if killed:
                gain = attacker.spec.attack.heal_on_kill * len(killed)
                # ★ 击杀回血也是这一株的收益 → 归它现在的支配者
                _who = self._holder(p)
                _who.hp += gain
                self.say("%s 击杀 %s → 【%s】+%s 血（现%s）%s"
                         % (p.tag, "、".join(t.tag for t in killed),
                            attacker.spec.name, self.hp_str(gain),
                            _who.hp_text(),
                            "" if _who is p else "（这一株已归 %s 支配）" % _who.tag))

    def _splash(self, attacker, defender, slot):
        """【v0.27】溅射：主攻击【造成伤害后】，向目标两侧各产生一次"暴2·伤0.5"。

        ⚠️⚠️ 【v0.28 起这个机制被暂时隐藏】—— 正式图鉴里已经没有任何一株
        带 splash（41 大菠萝 / 51 西瓜 都归 0 了），所以这条路径目前【跑不到】。
        用户 2026-09-22 原话："你能不能先隐藏溅射机制，我还没想好。"
        代码留着（测试 Test65 改用测试专用株继续压它），等用户定了再往
        phase_settle 末尾那个"环相关机制"的位置上接。
        ★ 定了之后大概率要改的两处（先记在这里免得忘）：
          · 落点：用户要的是"涉及存活环的攻击【最后发生】"，
            现在这一步还挂在 ⑦ 的 _resolve_plan 里（打中一下立刻溅一下）；
          · #68 已结案：溅射【打不穿超防】（splash_attack 已清掉 super_pierce）。

        两侧是谁 = ring_neighbors(目标, exclude=释放方)（存活环 + 不打自己）；
        每侧溅不溅 = 博弈阶段释放方声明过的（self.splash_plans，键是 (编号, 第几发)）。

        ★ 走【完整】的 resolve_hit（用户 2026-09-21 选的口径）：
          属性免疫、木盾、超防、同级免疫全部照扣，溅射本身也可能被挡下。
          所以它【没造成伤害】就不会再触发"杨桃种"、也不会再溅射
          （splash_attack() 已把 splash 清 0，不会无限套娃）。
        """
        sides = self.splash_plans.get(slot)
        if not sides:
            return
        sp = attacker.spec.attack.splash_attack()    # 派生一回，两侧共用
        for side in sides:
            self.say("  ※ 溅射（来自 %s 的【%s】→ %s）"
                     % (attacker.player.tag, attacker.spec.name, side.tag))
            self.resolve_hit(attacker, side.field, atk=sp)

    @staticmethod
    def _no_num(no):
        """编号 → 数字（比大小用）。"65"→65；空编号（辣椒那种）→0，排最后。"""
        return int(no) if no and no.isdigit() else 0

    # ==================================================================
    # 【v0.38】电系：[闪电一击] 弹 [闪电链]
    # ==================================================================
    def _flash_chain(self, attacker, defender, slot):
        """【v0.38】[闪电一击]：本次攻击【破防/造成伤害后】，以该目标为链点，
        向它【两侧目标之一】弹出一条 [闪电链]。

        ★ 原稿出处：p33「[闪电一击]：攻击属性。[无视][超防]；该次攻击
          [破防]/造成伤害后，以该目标为链点，向其两边目标之一弹出一个[闪电链]
          （因[绝缘]而无法[破防]时损失链点）」。

        ★ 与溅射的三点区别（别抄成同一个函数）：
          · 只弹【一侧】一个（释放方选边）；溅射是两侧各一个；
          · [闪电链] 的数值 = 暴2·伤1·【无视超防】；溅射 = 暴2·伤1·【恒不穿超防】；
          · 【只弹一跳】：链出来的那一击【不再】继续弹链 —— 所以下面那一击
            用的是"已清掉 flash 的派生攻击"（用户 2026-09-30："只弹一跳（像溅射）"）。

        ★ 侧的选择：释放方在【博弈阶段】像溅射那样声明过（self.flash_plans，
          键 = (玩家编号, 第几发)）。没声明的 → 不弹（保守）。
        """
        side = self.flash_plans.get(slot)
        if side is None:
            return
        # ★★【v0.71】目标可能在【结算阶段】被改掉了 ——
        #   [勾取]的条件方案就是在结算时才盖上去的（_apply_cond），
        #   而[闪电一击]是博弈阶段按"当时登记的目标"声明的。
        #   目标一变，当初选的那一侧就不成立：它可能根本不是新目标的两侧，
        #   ★ 甚至【就是新目标自己】（实测过：闪电链弹回目标本人，荒谬）。
        #   → 重新校验两侧的合法性，不合格就作废并说清楚，不许硬弹。
        # ⓑ【v0.72】环上不足 3 人：链点的"两侧"重合/为空 → 本来就没有两侧。
        #   这跟"目标被改掉"是两件事，必须分开说 —— 否则战报会把人往"目标变了"
        #   上带（本用例：p1/p2 先后出局，闪电草打 p3 时环上只剩 p3、p4 两人）。
        if len(self.ring) < 3:
            self.say("  · 【闪电一击】没能弹链：%s 中弹那一刻存活环上只剩 %d 人"
                     " → 链点没有\"两侧\"（没有两侧就不触发）"
                     % (defender.player.tag, len(self.ring)))
            return
        _sides = self.ring_neighbors(defender.player, exclude=attacker.player)
        if side not in _sides:
            self.say("  · 【闪电一击】声明失效：目标已改为 %s，当初弹向的 %s "
                     "不再是它的两侧 → 这一发不弹链"
                     % (defender.player.tag, side.tag))
            return
        sp = attacker.spec.attack.flash_chain_attack()   # 派生一回（清 flash，防套娃）
        self.say("  ⚡ 闪电链（来自 %s 的【%s】→ %s）"
                 % (attacker.player.tag, attacker.spec.name, side.tag))
        # 走【完整】的 resolve_hit（与溅射同款口径）：属性免疫、木盾、同级免疫照扣。
        #   ★ 但 [闪电链] 标了【无视超防】→ super_pierce=True（见 flash_chain_attack）。
        self.resolve_hit(attacker, side.field, atk=sp)

    def nearest_alive(self, me):
        """【v0.38】存活环上距 me【最近】的活人（134 猫尾草的目标限制）。

        ⚠️【v0.39 起本函数不再被调用】—— 用户 2026-10-04 更正：猫尾草的"最近"
          在环上一般【有左右两个】（等距），不能替玩家默认挑一个；
          现在的口径是"连击 N 发由玩家分配到左右两侧"（见 _q_cat_split /
          _resolve_cat_tail）。这个函数留着仅作参考（等距取编号小者的老逻辑），
          将来若有别的机制需要"唯一最近"再回来用。

        原稿：`目标只能是最靠近自己的仍有活着者`。
        ★ 算法 = 在 g.ring（存活环）上取 me 的两个邻居，取……"最近"只有一个时
          直接用；两侧都有时怎么定？—— 环上"两侧距离相等"的常见解法：
          取【座位编号更小】的那一侧（确定性、可复现，不引入随机）。
          ⚠️ 这里读的是 g.ring（存活环），不是 self.players —— 与"死了不打"
             同一条铁律：别写 [p for p in players if p.alive]（看不到"已被摘掉"）。
        """
        if me not in self.ring:
            return None
        ring = list(self.ring)
        n = len(ring)
        if n <= 1:
            return None
        i = ring.index(me)
        left = ring[(i - 1) % n]
        right = ring[(i + 1) % n]
        if left is right:     # 只剩 2 人时左右是同一个
            return left
        # 取距离最近者：环上到左、到右的步数
        d_left = (i - ring.index(left)) % n
        d_right = (ring.index(right) - i) % n
        if d_left < d_right:
            return left
        if d_right < d_left:
            return right
        # 等距 → 取编号小者（确定性）
        return left if left.idx <= right.idx else right

    def _plan_targets(self, p):
        """这个人本回合的打击对象列表（全体攻击展开成所有人）"""
        if self.plans[p] == "ALL":
            return self.others_alive(p)
        return self.plans[p]

    def devour_preview(self, attacker, defender):
        """【v0.21】预览：这一口吞得动吗？—— 只给界面显示用，不改动任何状态。

        为什么要有它：噬魂类的成败【全看暴值能不能压过有效防御】，玩家在选
        目标那一步看不见这个数就只能瞎猜。引擎把答案算好递给界面，
        界面负责显示 —— 判断权仍然只在引擎这一处。

        返回：None = 攻方不是噬魂类（界面不显示这一栏）
              True / False = 吞得动 / 吞不动

        ⚠️ 它和 resolve_hit 的门是【同一套】，改一处必须改另一处 ——
            Test55 拿十几种对位两边对着跑，就是为了钉住这一点。"""
        a = attacker.spec.attack
        if not a.is_devour:
            return None
        d = defender.spec.defense
        # 【v0.7】没释放的植物属性归零 → 免疫与标签一并失效（与 resolve_hit 一致）
        immune = () if defender.inert else d.immune_attrs
        tags = () if defender.inert else d.tags
        immune_names = () if defender.inert else d.immune_plants
        if attacker.spec.name in immune_names:
            return False
        if (attacker.spec.name == defender.spec.name
                and defender.spec.name not in a.ignore_plants):
            return False
        if a.attr in immune:
            return False
        # 【v0.23】超防闸门 —— 与 resolve_hit【同一个位置】（都排在"绕防/门槛"之前）。
        #   老版本把它摆在最后，等于"暴门槛先过、超防再拦"；现在提前，
        #   保证两条路径对同一个对位给出同一个答案（Test55 就是钉这个的）。
        # ★【2026-10-25】超防挪到【防御收益】那一步之后，声明期读到的 super_def
        #   还是"上一回合剩下的"。但守方这回合已经声明释放了 —— 那一层【必定】
        #   在攻击之前挂上，所以预览必须把它算进去，否则界面会说"吞得动"、
        #   结算却吞不动（玩家没有任何办法看出来）。
        #   判据：现有超防 > 0，或者"这回合已释放、且它的 spec 带超防"。
        will_block = defender.player.super_def > 0 or (
            defender.player in self.released
            and defender.player.field is not None
            and bool(defender.spec.super_def))
        if self.super_def_blocked(defender.player, a.super_pierce) or (
                will_block and not a.super_pierce):
            return False
        # 【v0.24】与 resolve_hit 同一处口径：a.ignored_tags()（属性通用表 ∪ 本株声明）。
        if set(tags) & set(a.ignored_tags()):
            return True          # 防御被绕过 → 暴门槛不卡
        if defender.spec.name in a.ignore_plants:
            return True
        if a.bao <= max(0, defender.fang - defender.fang_aux):
            return False         # 暴门槛没过
        return True

    def super_def_blocked(self, player, super_pierce=False):
        """【v0.23】超防闸门 —— "这一下能不能碰到他"的【唯一入口】。

        起因（你 2026-09-20 报的 bug）：仙桃挂着超防，杨桃照样把它的防御
        从 6 削到 1。根因是超防原先只拦在 _hurt（掉血那一步），而"削防/破防"
        排在它前面 —— 护罩还在、植物的防御却被磨光了，成了"漏水的护罩"。

        你 2026-09-20 拍板的口径是【全部挡下】：只要攻击方没有显式标注
        "无视超防"，这一击整个作废 ——
          · 不削防（普通攻击的暴值一分不扣）
          · 不破防（broken 不置位；"无视标签/特殊克制直击"那两条也一并作废）
          · 不掉血（_hurt 那道收不到东西）
          · 噬魂不夺种、不回血（devour_preview 与 resolve_hit 同步返回否）
          · 火种爆发（无攻击者，同样算"普通的"）也整个作废

        super_pierce=True = 攻击方标了"无视超防"，闸门直接放行。
        原稿里能打穿超防的株都会单独标一句"无视超防"（06 人参果 /
        102 茄子加农炮 / 106 捕蝇草 / 109 火龙果 / 94 毒雾菇）——
        将来加它们时在 Attack(...) 里写 super_pierce=True 即可，
        不要在引擎里按植物名 if/else（v0.17 起的老规矩）。
        """
        return player.super_def > 0 and not super_pierce

    def _effectively_dead(self, player):
        """【v0.34】"算不算已经死了"的统一判据 —— 预演血量。

        判据 = `目标当前生命 − 本回合已登记的伤害 ≤ 0`。
        存在的理由：伤害是【先登记进 _pending_dmg、再 _flush_damage 才落 hp】的，
        所以在一株自己的连击【内部】，`player.alive` 还没翻 False —— 光看 alive
        会把"已经打空的人"当成活的，继续往尸体上打（你 2026-09-29 报的 bug：
        杨桃连击 5 打 1 条命的人，白拿 4 颗种子）。

        ★ 这是本项目【第二次】用到这条判据 —— 第一次在 _devour_hit 开头
          （噬魂的落空判定，一直是这么写的）。两处算法一样、副作用文案不同，
          暂时各写一份；要不要抽成一个函数见规则文档待确认 #87。
        """
        return player.hp - self._pending_dmg.get(player, 0) <= 0

    def _hurt(self, player, amount, super_pierce=False):
        """【v0.9】登记伤害，不立刻扣血 —— 但 v0.28 起只在【一株植物】的范围内攒。

        【v0.28】口径变了：以前是"全场攒到结算末尾的 ③ 一起扣"（同时结算），
        现在是"一株植物打完就 _flush_damage() 兑现一次"。
        所以这个函数只负责【算】，落地全交给 _flush_damage —— 别在这里碰 hp。

        【v0.18】超防也在这里拦一道；【v0.23】之后它是【第二道】了 ——
        主动的闸门提前到了 super_def_blocked（排在削防之前），这里留作兜底，
        接住那些不经过 resolve_hit 的伤害（人参果段爆发就是直接调它的）。
        返回值：True = 伤害记上了；False = 被【超防】完全挡下。

        super_pierce=True 的参数 = 这次伤害【无视超防】。
        规则上这叫"有特殊标注的除外"（你 2026-09-16 的原话）：
        普通暴击/普通穿透再高都打不穿超防，只有显式标了无视超防的效果可以 ——
        目前只有人参果的段爆发（原稿 06 写"伤害[无视][超防]"）。"""
        if self.super_def_blocked(player, super_pierce):
            self.say("  → %s 处于【超防】状态（还剩 %d 回合）→ 这次伤害被完全挡下"
                     % (player.tag, player.super_def))
            return False
        self._pending_dmg[player] = self._pending_dmg.get(player, 0) + amount
        return True

    def _flush_damage(self):
        """【v0.28】把登记表里的伤害【当场】落地 —— 新的"统一扣血"。

        调用时机：一株植物打完（_resolve_plan 收尾）、一次火种爆发之后、
        一个段的段爆发之后。一句话：**谁算完谁落地**。

        ★ 这里同时是【生命归 0 → 退出存活环】的唯一落点：
          扣完血当场判断，出局的人立刻从环上摘掉，
          后面轮到他的植物时就已经"不再出手"了（phase_settle ⑦ 判的就是它）。
        ★ 【v0.26】生命【不欠账】照旧：一笔伤害超过现有生命就只扣到 0 ——
          v0.26 起"打穿后多余的子弹也落在人身上"，一株杨桃连击5 打裸奔目标
          = 5 条命，不夹的话战报会出现"损失 5 条命 → 剩余 -4"，
          而且负生命会顺着 hp 传下去（加血会被负数吃掉）。
        ★ 战报文案从"本回合共损失 X 条命"改成"损失 X 条命"——
          因为现在一笔账不再等于一个回合（可能一株一落、一回合好几笔）。
        """
        if not self._pending_dmg:
            return
        for p in self.players:
            dmg = self._pending_dmg.get(p, 0)
            if not dmg:
                continue
            lost = min(dmg, p.hp) if p.hp > 0 else 0
            p.hp -= lost
            self.say("  ⇒ %s 损失 %s 条命 → 剩余 %s"
                     % (p.tag, self.hp_str(lost), p.hp_text()))
            if lost < dmg:
                # 打穿之后还挨了好几发 —— 把"实际打进来多少"如实说清楚，
                # 免得玩家看到"损失1条命"而不知道对方一共打了多少条命的量。
                self.say("     （这一笔合计 %s 条命 —— 超出剩余生命的部分不再计）"
                         % self.hp_str(dmg))
            if p.hp <= 0:
                # 【v0.51】★ 死亡快照必须抢在 [瓜分] 之前 —— 瓜分会把种子转走
                #   并把死者清零，晚一步拍下来的快照就是 0 颗。
                #   （死于噬魂时这里已经是幂等的空操作，快照在 _devour_hit 就拍了。）
                self._mark_death(p)
                self._leave_ring(p)
                # 【v0.48】生命归 0 的瞬间 → 种子按 [瓜分] 分掉
                #   （顺序：先播"退出存活环"，再播"种子怎么分"）。
                self._on_death_seeds(p)
        self._pending_dmg = {}

    def _hurt_text(self, player, amount, super_pierce=False):
        """【v0.18】伤害落地的标准说法：被超防挡下 vs 真的扣血。
        以前各分支直接拼"损失X点生命"，加了超防之后那句会说谎 ——
        所以统一走这里，让战报永远和实际发生的事一致。

        【v0.23】多了 super_pierce 参数：标了"无视超防"的攻击必须把它透传下去，
        否则会出怪事 —— 闸门放行了这一击，拼文案时又被超防拦下，
        于是战报说"被扛住了"、植物却已经破了防（自己跟自己打架）。"""
        if self._hurt(player, amount, super_pierce):
            return "%s损失%s点生命" % (player.tag, self.hp_str(amount))
        return "%s 的【超防】扛住了这一击（不掉血）" % player.tag

    def _dealt_damage(self, attacker):
        """【v0.27】"造成伤害"的统一收尾：杨桃类在这里 +N 种。

        ★ 为什么单独一个函数、而不是在各分支里顺手加种子：resolve_hit 里能造成
          伤害的地方有四处（无视标签直击 / 特殊克制直击 / 普通打穿 / 噬魂），
          散着写必漏一处 —— 这类"同一件事多个落点"的坑本项目踩过
          （v0.17 的充能文案就是这么漏掉人参果的）。判定收在 resolve_hit，
          发种子也收在一处。
        ★ attacker 可能是 None（凭空攻击，如火种爆发 / 人参果段爆发）→ 跳过。
        """
        if attacker is None:
            return
        n = attacker.spec.seed_on_damage
        if not n:
            return
        me = attacker.player
        # ★【v0.72】"造成伤害得种"也是这一株的收益 → 归支配者
        recip = self._holder(me)
        recip.seeds += n
        self.say("  → 【%s】造成伤害 → 获得%d颗种子（现有%d）%s"
                 % (attacker.spec.name, n, recip.seeds,
                    "" if recip is me else
                    "（这一株已归 %s 支配）" % recip.tag))

    def check_end(self):
        """【v0.45 改判据】胜负 = 【场上只剩一个连通分图】。

        ★ 规则（用户 2026-10-15 拍板）：
          "当场上所有人都属于同一个并查集时，该方所有人获胜" —— 即
          存活者若全部落在【同一个连通分图】里，则这一分图的【全体】获胜。
        ★ 它把老规则"最后存活者获胜"【包进来】了：人各一队时，只剩 1 人
          = 只剩 1 个分图 → 单人获胜，行为与旧版一字不差（回归测试照过）。
          真正的变化只在"几个人先连成了队友、并且把剩下的都熬死"这一种局面：
          这时他们是【共同获胜】，而不是互相残杀到只剩一个。
        ★ 仍然只在【回合边界】判（run 的两处调用），结算中途不判。
        """
        alive = [p for p in self.players if p.alive]
        if not alive:
            self.over = True
            self.winners = []
            self.say("")
            self.say("*** 游戏结束：同归于尽，平局。 ***")
            return True
        groups = self.team_groups()
        if len(groups) == 1:
            winners = groups[0]
            self.over = True
            self.winners = winners
            # 【兼容】winner 仍保留：单人获胜时 = 那一个人；多人共同获胜时 = None
            #   （老客户端只认 winner，会显示"平局"→ 新端一律改读 winners）。
            self.winner = winners[0] if len(winners) == 1 else None
            self.say("")
            if len(winners) == 1:
                self.say("*** 游戏结束：%s 获胜！ ***" % winners[0].tag)
            else:
                self.say("*** 游戏结束：%s 共同获胜（同一队友链）！ ***"
                         % "、".join(w.tag for w in winners))
            return True
        return False

    # ---------- 问题构造 ----------
    @staticmethod
    def _need_release_ask(spec):
        """【v0.7】这株植物要不要问"释放与否"。
        规则：所有植物都要问，只有被显式声明 must_release 的不问（出牌即自动释放）。
        —— 早先版本只问有主动效果的（攻击/治疗/充能），不问种子/木盾/地盾，
           已按你的原话改正：那样做会泄露"谁带了攻击牌"。"""
        return spec is not EMPTY and not spec.must_release

    def _q_plant(self, p):
        return Question(
            "plant", p, self,
            own={"hp": p.hp_text(), "seeds": p.seeds},
            catalog=[{"name": n, "line": PLANTS[n].line()} for n in self.playable],
            validate=lambda v: isinstance(v, str) and v in self.playable)

    def _q_release(self, p):
        return Question(
            "release", p, self,
            plant=p.field.spec.name,
            validate=lambda v: isinstance(v, bool))

    def _q_charge(self, p, by=None, cond=False):
        # 【v0.17】把【植物名】和它自己的充能说明一起发下去：
        #   界面以前不知道是谁在充能，只能写死大喷菇那句公式 ——
        #   结果人参果充能时也显示"攻击力 = 充能数 + 1"。
        # ★【v0.70】by = 谁来答这道题。**被勾取的充能植物，由勾取者决定充多少**
        #   （用户 2026-10-26："要让充能植物充多少种子（默认充完，因为扣的是
        #   被勾取者的种子）"）。注意 own.seeds 仍是【被勾者】的 —— 耗的就是他的种。
        spec = p.field.spec
        # ★【v0.72】题目里那个"你有多少颗种子"必须分两种口径：
        #   · 原主人自己答（cond=False）→ 此刻手上真有多少（已被扣过就是扣过）；
        #   · 勾取者答条件方案（cond=True）→ 按【本回合充能之前】的数目，
        #     因为勾取一旦兑现，原主人那一笔要退回去（见 _apply_cond_charge）。
        seeds = p.seeds
        if cond:
            seeds = int(getattr(p.field, "pre_charge_seeds", seeds))
        return Question(
            "charge", by or p, self,
            own={"from": p.tag, "seeds": seeds, "plant": spec.name,
                 "hint": spec.charge_hint,
                 "proxy": self._proxy_label(
                     p, by, "你要让它充几颗？（扣的是它的种子）", cond=cond)},
            validate=lambda v: isinstance(v, int) and not isinstance(v, bool) and v >= 0)

    def _q_water_hp(self, p):
        """【v0.10】出喷菇的人：要不要用 1 个水滴抵掉半条命代价（出牌时先扣，这里退还）"""
        return Question(
            "water_hp", p, self,
            plant=p.field.spec.name, cost_hp=p.field.spec.cost_hp,
            own={"water": p.water, "hp": p.hp_text()},
            validate=lambda v: isinstance(v, bool))

    def _q_water_def(self, p, budget=None):
        """【v0.10】花 x 个水滴 → 本回合防御 +x（1:1，你定的比例）

        ★ budget = 本次能花的【上限】。默认 = 手上现有的水；但水滴草还在场上时
          要把"本回合即将产出的那 +1"算进去 —— 产水滴与花水滴在【辅助防】同一步，
          先产出后花费，所以那 1 滴当回合真的花得掉。
        """
        if budget is None:
            budget = p.water
        return Question(
            "water_def", p, self,
            own={"water": budget, "fang": p.field.fang},
            validate=lambda v: isinstance(v, int) and not isinstance(v, bool)
            and 0 <= v <= budget)

    def _q_target(self, p, shot=0, shots=0, by=None, cands=None, cond=False,
                  verb="勾取"):
        """选 1 个目标。shot/shots 用于连击的逐发分配（界面上显示"第几发"）。
        shot=0 表示不是连击，界面不显示发数。

        【v0.21】候选里补两样东西，都是"玩家不看见就没法决策"的：
          · seeds   —— 被攻击方手上有多少种子（噬魂要按比例夺走，得看得见有多少可夺）
          · fang_eff —— 有效防御（已扣掉水滴补的那部分）：噬魂的门槛比的是它
        另外 own.hint 由【植物自己】声明（v0.17 的老规矩），噬魂才非空。

        【v0.58】by —— 谁来答这道题。默认 = 出牌的人（p）。
          只有 [勾取] 会传：**被勾的植物由勾取者选目标**（"如果勾取成功，你要打谁"）。
          → Question.player = by，但 cands / own / 判定仍按被勾的那株算。
        """
        # 【v0.70】cands 允许外部给：被[聚电]收走的植物，候选要按【支配者】算
        #   （不能包含支配者自己），但攻击数值仍用被收那株的。
        if cands is None:
            cands = self.others_alive(p)
        idxs = {c.idx for c in cands}
        spec = p.field.spec
        return Question(
            "target", by or p, self,
            shot=shot, shots=shots,
            own={"from": p.tag, "plant": spec.name, "hint": spec.devour_hint(),
                 "proxy": self._proxy_label(p, by, cond=cond, verb=verb)},
            cands=[{"idx": c.idx, "name": c.name, "name_tag": c.tag, "hp": c.hp_text(),
                    "hp_units": c.hp, "plant": c.field.spec.name,
                    "fang": c.field.fang, "seeds": c.seeds,
                    "fang_eff": max(0, c.field.fang - c.field.fang_aux),
                    # 【v0.21】噬魂类专用：这一口吞得动吗（None = 攻方不是噬魂类）。
                    #   算在引擎里、由界面显示 —— 判断只此一处。
                    "devour": self.devour_preview(p.field, c.field)}
                   for c in cands],
            refs={"cands": cands},
            validate=lambda v: isinstance(v, int) and not isinstance(v, bool) and v in idxs)

    def _proxy_label(self, p, by, what="你要攻击谁？", cond=False, verb="勾取"):
        """【v0.58】"替别人答"时给界面的一句说明；不代答就是空串。

        ★【v0.70】`what` 参数：同一套代答现在不只用于"打谁"，还有
          "充能几颗"（_q_charge）和"要聚电谁"（_q_absorb）——
          用户 2026-10-26："牛角菱会继承它勾取植物的所有轮询"。
        """
        # ★【v0.72】cond=True 时【by=None 也要出文案】——
        #   原主人自己那份同样是条件方案（"如果[聚电]成功"），只是答的人是他本人。
        if by is p or (by is None and not cond):
            return ""
        # 【v0.71】cond = 这是"如果勾取成功"的条件方案，不是替他答 ——
        #   题面必须写清楚，否则玩家以为勾取已经生效了。
        # ★【v0.72】用户 2026-10-27："询问也不够清楚，牛角菱的询问应该是
        #   '你勾取的 playerX 的玩家的聚电蓝莓，你要攻击谁？'"
        #   旧文案是"如果勾取成功：你勾取了 X 的【Y】—— 它这一回合打谁？"：
        #   主句落在"它"身上，玩家看不出【到底是谁在做决定】。
        #   现在主句一律是"你……"，被勾的那株只作为定语出现。
        # ★【v0.72】verb —— [勾取]（103 牛角菱）与 [聚电]（57 聚电蓝莓）
        #   是两回事，题面必须自己说清是哪一种（用户 2026-10-27：
        #   "聚电蓝莓为什么没有 若[聚电]闪电草成功…"）。原来一律写"勾取"。
        if verb == "聚电":
            if cond:
                return "如果[聚电]成功 —— 你聚电的 %s 的【%s】：%s" % (
                    p.tag, p.field.spec.name, what)
            return "你聚电的 %s 的【%s】—— %s" % (p.tag, p.field.spec.name, what)
        if cond:
            return "如果勾取成功 —— 你勾取的 %s 的【%s】：%s" % (
                p.tag, p.field.spec.name, what)
        return "你勾取了 %s 的【%s】—— %s" % (p.tag, p.field.spec.name, what)

    def _q_targets(self, p, by=None, cands=None, cond=False, verb="勾取"):
        """【v0.8】散连击：一次性选 count 个【不同】的目标，每个目标挨 1 击。
        与"连击"的区别：连击是把 N 下都砸在同一个人身上，散连击必须分给不同的人。
        答案是一个列表（玩家编号），count 已按存活人数收紧。"""
        if cands is None:
            cands = self.others_alive(p)
        idxs = {c.idx for c in cands}
        count = min(max(1, p.field.spec.attack.san), len(cands))

        def ok(v):
            return (isinstance(v, list) and len(v) == count
                    and len(set(v)) == len(v)
                    and all(isinstance(x, int) and not isinstance(x, bool) and x in idxs
                            for x in v))
        return Question(
            "targets", by or p, self,
            count=count,
            own={"from": p.tag, "plant": p.field.spec.name,
                 "proxy": self._proxy_label(
                     p, by, "你要让它散连击打哪几个人？", cond=cond,
                     verb=verb)},
            cands=[{"idx": c.idx, "name": c.name, "name_tag": c.tag, "hp": c.hp_text(),
                    "hp_units": c.hp, "plant": c.field.spec.name,
                    "fang": c.field.fang} for c in cands],
            refs={"cands": cands},
            validate=ok)

    def _q_burst_split(self, p, cands, n, by=None, cond=False, verb="勾取"):
        """【v0.44】连击【一次性发数分配】：把 n 发子弹一次分给各目标。

        ★★ 为什么改（用户 2026-10-12 实测）：
          老做法（v0.9 起）是【逐发问】—— 连击几发就问几次。5 发还能忍，
          141 石榴机枪 15 发就要连问 15 次，用户原话："而是询问十五次，简直弱智"。
          改成【一道题】：列出所有可选目标，玩家给每个目标填一个发数，合计 = n。
        ★ 答案 = [[目标编号, 发数], ...]（发数 > 0 的目标才要报；合计必须 == n）。
          · 全压一人 = [[x, n]]；分散 = [[x, 3], [y, 2]]。
        ★ 猫尾草（134）【不走这里】—— 它的目标受限于存活环左右两侧，
          仍用 cat_split（见 _q_cat_split）；单挑（duel）也仍自动锁定、不问。
        """
        idxs = {c.idx for c in cands}
        spec = p.field.spec

        def ok(v):
            if not isinstance(v, (list, tuple)):
                return False
            total = 0
            seen = set()
            for it in v:
                if not (isinstance(it, (list, tuple)) and len(it) == 2):
                    return False
                i, c = it
                if isinstance(i, bool) or not isinstance(i, int) or i not in idxs:
                    return False
                if isinstance(c, bool) or not isinstance(c, int) or c < 0:
                    return False
                if c == 0:
                    continue          # 发数 0 的目标可以报、等价于没报
                if i in seen:
                    return False      # 同一个目标只报一次（别拆成两条）
                seen.add(i)
                total += c
            return total == n

        return Question(
            "burst_split", by or p, self,
            plant=spec.name, shots=n,
            own={"from": p.tag, "plant": spec.name, "hint": spec.devour_hint(),
                 "proxy": self._proxy_label(
                     p, by, "你要把这 %d 发怎么分？" % n, cond=cond,
                     verb=verb)},
            cands=[{"idx": c.idx, "name": c.name, "name_tag": c.tag, "hp": c.hp_text(),
                    "hp_units": c.hp, "plant": c.field.spec.name,
                    "fang": c.field.fang, "seeds": c.seeds,
                    "fang_eff": max(0, c.field.fang - c.field.fang_aux),
                    # 与 _q_target 同形：仅噬魂类非 None（连击株一般用不到，保持一致）
                    "devour": self.devour_preview(p.field, c.field)}
                   for c in cands],
            refs={"cands": cands},
            validate=ok)

    def _q_splash(self, p, target, sides):
        """【v0.27】溅射声明：要不要向【两侧】各溅一次（两边各勾一次，可以都不勾）。

        答案是【玩家编号列表】，空表 = 都不溅。
        target 只用于显示（"对谁溅射"）—— 两侧是按【目标】算出来的，不是按攻方。
        """
        idxs = {s.idx for s in sides}
        return Question(
            "splash", p, self,
            plant=p.field.spec.name,
            target={"idx": target.idx, "name": target.name, "name_tag": target.tag},
            sides=[{"idx": s.idx, "name": s.name, "name_tag": s.tag, "hp": s.hp_text(),
                    "plant": s.field.spec.name, "fang": s.field.fang}
                   for s in sides],
            refs={"cands": sides},
            # 先判"是不是 int 列表"再 set() —— 顺序反了的话，答案里塞个 list
            # 会让 set() 直接抛 TypeError（validate 该返回 False，不该炸）。
            validate=lambda v: (isinstance(v, list)
                                and all(isinstance(x, int) and not isinstance(x, bool)
                                        and x in idxs for x in v)
                                and len(set(v)) == len(v)))

    # ---- 【v0.38】电系：勾人 / 闪电链 的决策构造 ----
    def _q_absorb(self, p, cands, by=None, cond=False):
        """【v0.38】勾人声明：选【要勾走谁】（可多选；0~N）。

        答案是【玩家编号列表】，空表 = 不勾任何人。
        ★ 候选已在服务端按 absorb_targets 过滤好了，界面只负责列出来让人挑。
        ★【v0.70】by = 谁来答：**被牛角菱勾取的聚电蓝莓，由勾取者决定它聚电谁**
          （用户 2026-10-26："牛角菱会继承它勾取植物的所有轮询"）。
          候选仍按【被勾的那一株】算（它的 absorb_targets），只是答题人换了。
        """
        idxs = {c.idx for c in cands}
        spec = p.field.spec
        return Question(
            "absorb", by or p, self,
            plant=spec.name,
            own={"from": p.tag, "plant": spec.name,
                 "hint": "可聚电：%s" % "、".join(spec.absorb_targets),
                 "proxy": self._proxy_label(
                     p, by, "你要让它聚电谁？", cond=cond)},
            cands=[self._cut_cand_payload(c) for c in cands],
            refs={"cands": cands},
            validate=lambda v: (isinstance(v, list)
                                and all(isinstance(x, int) and not isinstance(x, bool)
                                        and x in idxs for x in v)
                                and len(set(v)) == len(v)))

    def _q_hook(self, p, cands):
        """【v0.57】[勾取] 声明：选【要勾走的两株同名植物】（0 或 hook_same 株）。

        ★ 复用 "absorb" 这个 kind —— 六端界面已经有"多选候选里的一批"这套渲染，
          勾取在交互上完全是同一件事（勾走谁），只是**合法性由服务端判**：
          要么 0 株（= 攻击形态），要么恰好 hook_same 株且【同名】。
        ★ 不新造 kind 的理由：新 kind 要同时补六端 + PAYLOAD_CONTRACT + 脚本控制器，
          漏一个就是"回 None → 判非法 → 题目重发 → 死循环"（v0.50 血案）。
        ★★【v0.62】把约束【发给界面】，别再让界面自己猜：
          · pick_count  —— 必须恰好选几株（0/缺省 = 不限，电系勾人就是这一路）；
          · group_by    —— 按哪个字段分组（"plant" = 同名）；界面据此分组 + 限选；
          · none_label  —— "一株都不选"那个按钮该写什么（这里 = 换形态）。
          不带这三个字段时，界面必须退回旧的"0~N 自由多选"—— 兼容电系勾人。
          **旧版没发这些**，界面只能自由多选 → 选 3 株 / 跨组两株一律判非法 →
          服务端回"输入无效，请重新选择"并重发原题，玩家怎么点都过不了。
        """
        idxs = {c.idx for c in cands}
        spec = p.field.spec
        return Question(
            "absorb", p, self,
            plant=spec.name,
            # 【v0.62】界面照着画就行：选几株、怎么分组、"不选"叫什么
            pick_count=spec.hook_same,
            group_by="plant",
            none_label="不勾，走攻击形态",
            own={"plant": spec.name,
                 "hint": "勾取形态：防 = 基础防 + 所向×%d，本回合不攻击；"
                         "一株都不选 = 走攻击形态（照常散连击）" % spec.hook_def_per},
            cands=[self._cut_cand_payload(c) for c in cands],
            refs={"cands": cands},
            validate=lambda v: (isinstance(v, list)
                                and len(v) in (0, spec.hook_same)
                                and all(isinstance(x, int) and not isinstance(x, bool)
                                        and x in idxs for x in v)
                                and len(set(v)) == len(v)))

    def _q_flash(self, p, target, sides, by=None, cond=False, verb="勾取"):
        """【v0.38】[闪电一击] 声明：闪电链向【两边目标之一】弹出（单选一侧，或都不选）。

        答案是【玩家编号】或 None（不弹）。★ 原稿："向其两边目标【之一】弹出"。
        ★ 与溅射的区别：溅射是两侧各一个（列表），这里是【单选一侧】。
        ★【v0.72】by / cond —— 被[勾取]的那一株，如果勾取者的"条件充能"
          把它推到第二段（带[闪电一击]），这一问要由【勾取者】答，
          且只在勾取兑现时才生效（同"聚电谁 / 充几颗 / 打谁"那三道）。
        """
        idxs = {s.idx for s in sides}
        return Question(
            "flash", by or p, self,
            plant=p.field.spec.name,
            own={"from": p.tag, "plant": p.field.spec.name,
                 "proxy": self._proxy_label(
                     p, by, "这一发命中后，链要弹向哪一侧？（两边之一，可都不弹）",
                     cond=cond, verb=verb)},
            target={"idx": target.idx, "name": target.name, "name_tag": target.tag},
            sides=[{"idx": s.idx, "name": s.name, "name_tag": s.tag, "hp": s.hp_text(),
                    "plant": s.field.spec.name, "fang": s.field.fang}
                   for s in sides],
            refs={"cands": sides},
            validate=lambda v: (v is None
                                or (isinstance(v, int) and not isinstance(v, bool)
                                    and v in idxs)))

    # ---- 【v0.32】刀叶类的三个决策构造 ----
    def _cut_cand_payload(self, c):
        """候选目标要塞给界面看的字段（与 _q_target 的 cands 同形，少几项无关的）。"""
        return {"idx": c.idx, "name": c.name, "name_tag": c.tag, "hp": c.hp_text(),
                "hp_units": c.hp, "plant": c.field.spec.name,
                "fang": c.field.fang}

    def _q_cut_target(self, p, cands, by=None, cond=False):
        """刀类：选【要消除谁】。候选已在服务端按"射程 + 免切"过滤好了 ——
        界面只负责列出来让人挑（规则一个字都不许写在界面里）。

        ★【v0.44】答案【允许 None】= 本回合【不切任何人】。用户 2026-10-12：
          "刀类释放没有不切的选项，释放不代表一定要选择一个目标，因为你可能不想
           打人，但是还是想有防御值"。所以："不选目标"是一个正当答案，
          此时刀类【照常保留防御】，只是本回合不消除任何人。
        """
        idxs = {c.idx for c in cands}
        spec = p.field.spec
        return Question(
            "cut_target", by or p, self,
            own={"from": p.tag, "plant": spec.name, "hint": spec.cut_hint(),
                 "proxy": self._proxy_label(p, by, "你要让它切谁？", cond=cond)},
            # 界面据此画一个"不切"按钮（文案由服务端给，前端不复述规则）
            skip_label="不切任何人（本回合保留防御、不消除谁）",
            cands=[self._cut_cand_payload(c) for c in cands],
            refs={"cands": cands},
            validate=lambda v: (v is None
                                or (isinstance(v, int) and not isinstance(v, bool)
                                    and v in idxs)))

    def _q_cut_mode(self, p, target, same, by=None, cond=False):
        """连切：同名卡 ≥2 株 → "single"（只切这一株）/ "all"（全切同名卡）。"""
        idxs = {s.idx for s in same}
        return Question(
            "cut_mode", by or p, self,
            plant=p.field.spec.name,
            own={"from": p.tag, "plant": p.field.spec.name,
                 "proxy": self._proxy_label(
                     p, by, "只切这一株，还是连切同名卡？", cond=cond)},
            target={"idx": target.idx, "name": target.name, "name_tag": target.tag,
                    "plant": target.field.spec.name},
            same=[{"idx": s.idx, "name": s.name, "name_tag": s.tag, "plant": s.field.spec.name}
                  for s in same],
            refs={"target": target, "same": same},
            validate=lambda v: v in ("single", "all"))

    def _q_cut_half(self, p, targets, by=None, cond=False):
        """切一半 / 全切：目标里有丝瓜/鱼腥草这类（被切时负面效果外溢）。
        答案 = bool（True = 切一半，False = 全切）。"""
        return Question(
            "cut_half", by or p, self,
            plant=p.field.spec.name,
            own={"from": p.tag, "plant": p.field.spec.name,
                 "proxy": self._proxy_label(
                     p, by, "切一半，还是全切？", cond=cond)},
            targets=[{"idx": t.idx, "name": t.name, "name_tag": t.tag, "plant": t.field.spec.name}
                     for t in targets],
            refs={"targets": targets},
            validate=lambda v: isinstance(v, bool))

    def _q_cut_help(self, p, by=None, cond=False):
        """【v0.32】切一半 → "帮谁切"：从【所有存活玩家（含自己）】里多选 0~N 人，
        选中的这些人免疫该植物的负面效果（规则 15.6 + 用户 2026-09-28
        "多选（0~N 人，含自己）"）。"""
        cands = [x for x in self.players if x.alive]
        idxs = {x.idx for x in cands}
        return Question(
            "cut_help", by or p, self,
            plant=p.field.spec.name,
            own={"from": p.tag, "plant": p.field.spec.name,
                 "proxy": self._proxy_label(
                     p, by, "要帮谁免疫这个负面效果？", cond=cond)},
            hint="选择要帮的人（可多选，含自己；0 个也行）—— 他们免疫该植物的负面效果",
            cands=[self._cut_cand_payload(x) for x in cands],
            refs={"cands": cands},
            validate=lambda v: (isinstance(v, list)
                                and all(isinstance(i, int)
                                        and not isinstance(i, bool)
                                        and i in idxs for i in v)))
