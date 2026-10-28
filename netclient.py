# -*- coding: utf-8 -*-
"""
netclient.py —— 联机版控制台客户端（复刻你 C++ 客户端的角色）
用法：python netclient.py [服务端IP] [端口] [名字]
网页版请用 webui.py（【v0.68】起 gui_client.py 已删除，图形界面走浏览器）。
"""

import queue
import socket
import sys
import threading

import netproto


def main():
    ip = sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1"
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 8080
    # 【v0.35】可选第 3 个参数 = 名字。连上立刻报给服务端（报到消息，
    #   不是决策）—— 服务端建房时快照，战报里就会显示"名字(编号)"。
    name = sys.argv[3] if len(sys.argv) > 3 else ""
    sock = socket.create_connection((ip, port))
    if name.strip():
        netproto.send_msg(sock, {"type": "name", "value": name.strip()})
    print("已连接 %s:%d，等待其他玩家……" % (ip, port))
    q = queue.Queue()
    reader = netproto.Reader(sock)

    def read_loop():
        while True:
            try:
                m = reader.recv()
            except OSError:
                m = None
            if m is None:
                q.put({"type": "conn_lost"})
                return
            q.put(m)

    threading.Thread(target=read_loop, daemon=True).start()

    while True:
        m = q.get()
        t = m["type"]
        if t == "hello":
            print("你是 player%d（共 %d 人）" % (m["idx"], m["total"]))
        elif t == "msg":
            print(m["text"])
        elif t == "ask":
            handle_ask(sock, m)
        elif t == "wait":
            print(m.get("text", "等待其他玩家……"))
        elif t == "end":
            print("*** %s ***" % m["text"])
            break
        elif t == "conn_lost":
            print("与服务端的连接断开")
            break
    sock.close()


def send_answer(sock, value):
    netproto.send_msg(sock, {"type": "answer", "value": value})
    print("（已提交，等待其他玩家……）")


# 【v0.72】题头 —— 用户 2026-10-27："光从这张图里，玩家没有足够的信息知道
#   现在自己是在为哪个植物做攻击选择；你应该报告这是来源于 xx 的 xx 植物的 xx 行为"。
#   ★ 与网页端 app.js 的 qfrom()/qproxy() 同一口径：own.from = 那株植物的主人，
#     own.plant = 植物名，own.proxy = "若是替别人答的"那句说明。
_QFROM_KINDS = ("target", "targets", "burst_split", "charge", "absorb", "flash",
                # 【v0.72】勾取者的"代答"扩到全部轮询 —— 这些题的 own 里
                # 也带着"你勾取的 X 的【Y】：…"，命令行端同样要印出来。
                "cut_target", "cut_mode", "cut_half", "cut_help",
                "cat_split", "friend", "revive")


def _print_qfrom(m, own):
    if m.get("kind") not in _QFROM_KINDS:
        return
    plant = own.get("plant")
    if plant:
        frm = own.get("from")
        print("—— %s【%s】——" % ("来自 %s 的" % frm if frm else "你的", plant))
    if own.get("proxy"):
        print("★ " + own["proxy"])


def handle_ask(sock, m):
    kind = m["kind"]
    if kind == "plant":
        own = m["own"]
        print("【轮到你出牌】生命%s条命｜种子%d" % (own["hp"], own["seeds"]))
        for item in m["catalog"]:
            print("  " + item["line"])
        send_answer(sock, input("要出的植物名：").strip())
    elif kind == "release":
        v = input("要释放【%s】吗？(y/n)：" % m["plant"]).strip().lower()
        send_answer(sock, v in ("y", "yes", "1", "是"))
    elif kind == "target":
        own = m.get("own") or {}
        _print_qfrom(m, own)
        # 【v0.9】连击逐发分配：写清"第几发"
        shot, shots = m.get("shot", 0), m.get("shots", 0)
        if shot:
            print("连击第 %d/%d 发 —— 这一发给谁（可重复选同一人）；可选："
                  % (shot, shots))
        else:
            print("可选目标：")
        for c in m["cands"]:
            print("  %d. %s｜生命%s｜场上：%s(防%d)"
                  % (c["idx"], c.get("name_tag") or c["name"], c["hp"], c["plant"], c["fang"]))
        try:
            v = int(input("目标序号(输入玩家编号)：").strip())
        except ValueError:
            v = -1
        send_answer(sock, v)
    elif kind == "targets":
        own = m.get("own") or {}
        _print_qfrom(m, own)
        # 【v0.8】散连击：一次选 count 个不同的人
        n = m["count"]
        print("散连击：请选出 %d 个不同的目标（每人挨 1 击）" % n)
        for c in m["cands"]:
            print("  %d. %s｜生命%s｜场上：%s(防%d)"
                  % (c["idx"], c.get("name_tag") or c["name"], c["hp"], c["plant"], c["fang"]))
        while True:
            s = input("输入 %d 个玩家编号，空格或逗号隔开：" % n).strip()
            parts = [x for x in s.replace(",", " ").replace("，", " ").split() if x]
            if len(parts) == n and all(x.lstrip("-").isdigit() for x in parts):
                v = [int(x) for x in parts]
                if len(set(v)) == n:
                    break
                print("不能选同一个人两次。")
            else:
                print("要正好输入 %d 个数字编号。" % n)
        send_answer(sock, v)
    elif kind == "burst_split":
        own = m.get("own") or {}
        _print_qfrom(m, own)
        # 【v0.44】连击发数分配：一次把 shots 发分给各目标
        shots = m.get("shots", 1)
        cands = m.get("cands", [])
        print("【%s】连击 %d 发 —— 请分配每发打谁（可全压一人、可分散）"
              % (m.get("plant", "连击"), shots))
        for c in cands:
            print("  %d. %s｜生命%s｜场上：%s(防%d)"
                  % (c["idx"], c.get("name_tag") or c["name"], c["hp"], c["plant"], c["fang"]))
        while True:
            s = input("按上面顺序输入每个目标的发数（%d 个数字，空格分隔，合计=%d）："
                      % (len(cands), shots)).strip()
            parts = [x for x in s.replace(",", " ").replace("，", " ").split() if x]
            if len(parts) != len(cands) or not all(x.isdigit() for x in parts):
                print("要给【每个】目标各填一个非负整数发数。")
                continue
            counts = [int(x) for x in parts]
            if sum(counts) != shots:
                print("合计必须是 %d 发，你填的是 %d 发。" % (shots, sum(counts)))
                continue
            v = [[cands[i]["idx"], counts[i]] for i in range(len(cands)) if counts[i] > 0]
            break
        send_answer(sock, v)
    elif kind == "charge":
        # 【v0.70】own.proxy 非空 = 这一株是别人[勾取]给我的，由我代答
        own = m["own"]
        _print_qfrom(m, own)
        try:
            v = int(input("充能：输入要消耗的种子数（现有%d颗，溢出视为没有充能）："
                          % m["own"]["seeds"]).strip())
        except ValueError:
            v = -1
        send_answer(sock, v)
    elif kind == "water_hp":
        # 【v0.10】用 1 个水滴抵掉喷菇的半条命代价
        s = input("用 1 个水滴抵掉【%s】的半条命代价吗？（现有水滴%d，生命%s）(y/n)："
                  % (m["plant"], m["own"]["water"], m["own"]["hp"])).strip().lower()
        send_answer(sock, s in ("y", "yes", "1", "是"))
    elif kind == "water_def":
        # 【v0.10】花 x 个水滴 → 防御 +x（1:1）
        try:
            v = int(input("水滴加固：1个水滴=+1防御（现有%d，当前防%d），花几个："
                          % (m["own"]["water"], m["own"]["fang"])).strip())
        except ValueError:
            v = 0
        send_answer(sock, v)
    elif kind == "splash":
        # 【v0.27 补漏】溅射声明：向两侧分别勾选（可都不勾）。
        #   ★ 这个分支 v0.27 加 kind 时漏了本文件 —— 当时溅射随即被隐藏
        #     （41/51 的 splash=0），所以从没暴露；刀叶类落地时一并补齐。
        who = (m.get("target") or {}).get("name_tag") or (m.get("target") or {}).get("name", "目标")
        print("若对 %s 造成伤害 → 要向两侧溅射吗？（每侧0.5条命·暴2，可都不勾）" % who)
        sides = m.get("sides", [])
        for i, s in enumerate(sides, 1):
            print("  %d. %s｜生命%s｜场上：%s(防%d)"
                  % (i, (s.get("name_tag") or s["name"]), s["hp"], s["plant"], s["fang"]))
        s = input("要溅射的序号（空格隔开，回车=都不溅）：").strip()
        parts = [x for x in s.replace(",", " ").replace("，", " ").split() if x]
        v = [sides[int(x) - 1]["idx"] for x in parts
             if x.isdigit() and 1 <= int(x) <= len(sides)]
        send_answer(sock, v)
    # ---- 【v0.32】刀叶类的三个口 ----
    elif kind == "cut_target":
        own = m.get("own") or {}
        _print_qfrom(m, own)
        print("刀类：要消除谁？（%s）" % (own.get("hint", "")))
        for c in m["cands"]:
            print("  %d. %s｜生命%s｜场上：%s(防%d)"
                  % (c["idx"], c.get("name_tag") or c["name"], c["hp"], c["plant"], c["fang"]))
        # 【v0.44】空输入 = 不切任何人（保留防御）
        s = input("要消除的玩家编号（回车=不切、保留防御）：").strip()
        try:
            v = int(s) if s else None
        except ValueError:
            v = None
        send_answer(sock, v)
    elif kind == "cut_mode":
        own = m.get("own") or {}
        _print_qfrom(m, own)
        tgt = m.get("target") or {}
        same = m.get("same", [])
        print("连切：%s 的【%s】—— 场上同名卡共 %d 株："
              % (tgt.get("name_tag") or tgt.get("name", "?"), tgt.get("plant", "?"), len(same)))
        for i, s in enumerate(same, 1):
            print("  %d. %s 的【%s】" % (i, (s.get("name_tag") or s["name"]), s["plant"]))
        v = input("1=只切这一株；2=全切同名卡：").strip()
        send_answer(sock, "all" if v == "2" else "single")
    elif kind == "cut_half":
        own = m.get("own") or {}
        _print_qfrom(m, own)
        names = "、".join("%s 的【%s】" % ((t.get("name_tag") or t["name"]), t["plant"])
                         for t in m.get("targets", []))
        v = input("对 %s 要『切一半』还是『全切』？（y=切一半 / n=全切）："
                  % names).strip().lower()
        send_answer(sock, v == "y")
    elif kind == "cut_help":
        own = m.get("own") or {}
        _print_qfrom(m, own)
        print("【%s】要帮谁免疫该植物的负面效果？（可多选，含自己；不选=直接回车）"
              % m.get("plant", "刀叶类"))
        for c in m.get("cands", []):
            print("  %d. %s｜生命%s｜场上：%s(防%d)"
                  % (c["idx"], c.get("name_tag") or c["name"], c["hp"], c["plant"], c["fang"]))
        s = input("要帮的玩家编号（空格/逗号分隔）：").strip()
        v = []
        if s:
            for x in s.replace(",", " ").split():
                if x.isdigit():
                    v.append(int(x))
        send_answer(sock, v)
    # ---- 【v0.38】电系：勾人 ／ 【v0.57】103 牛角菱：勾取 ----
    elif kind == "absorb":
        own = m.get("own") or {}
        _print_qfrom(m, own)
        # 【v0.62】勾取的约束（恰好 pick_count 株、且同名）由服务端发下来；
        #   旧版这里自由多选 → 交上去判非法 → 服务端回"输入无效"重发原题 → 卡死。
        need = int(m.get("pick_count") or 0)
        gby = m.get("group_by")
        # ★【v0.67】"不勾"叫什么由服务端给（none_label），别在客户端复述规则 ——
        #   网页端一直读这个字段，命令行走自己的固定文案，两端口径会分岔。
        none_hint = m.get("none_label") or "不勾"
        if need:
            print("勾取：%s 选中 %d 株【同名】植物（%s）"
                  % (m.get("plant", ""), need, none_hint))
            print("  ★ %s" % (m.get("own") or {}).get("hint", ""))
        else:
            # 【v0.70】57 的这个动作从此叫【聚电】（与牛角菱的[勾取]分开）；
            #   own.proxy 非空 = 这一株是别人勾给我的，由我代答
            _px = (m.get("own") or {}).get("proxy") or ""
            if _px:
                print("★ " + _px)
            print("聚电：%s 要聚电谁？（%s；%s）"
                  % (m.get("plant", "聚电蓝莓"), (m.get("own") or {}).get("hint", ""),
                     none_hint))
        for c in m["cands"]:
            print("  %d. %s｜生命%s｜场上：%s(防%d)"
                  % (c["idx"], c.get("name_tag") or c["name"], c["hp"], c["plant"], c["fang"]))
        by_idx = {c["idx"]: c for c in m["cands"]}
        while True:
            s = input("要聚电的玩家编号（空格/逗号分隔）：").strip()
            v = []
            if s:
                for x in s.replace(",", " ").split():
                    if x.isdigit():
                        v.append(int(x))
            if not v:
                break
            bad = [x for x in v if x not in by_idx]
            if bad:
                print("  编号不存在：%s" % "、".join(str(x) for x in bad))
                continue
            if need:
                names = {by_idx[x].get(gby or "plant") for x in v}
                if len(v) != need or len(names) != 1:
                    print("  勾取必须恰好 %d 株【同名】植物（现在 %d 株、%d 种），重新输入"
                          % (need, len(v), len(names)))
                    continue
            break
        send_answer(sock, v)
    elif kind == "flash":
        own = m.get("own") or {}
        _print_qfrom(m, own)
        # 【v0.67 修】payload 的键是 **sides**（和 splash 同一套"两侧"语义），
        #   这里原来读的是 "cands" —— 和 app.js 的 renderFlash 是同一个 bug。
        #   v0.66 只修了网页端，命令行端漏掉了：sides 恒为空 → 一道选项都不打印，
        #   玩家只能直接回车（不弹），闪电链永远打不出来。
        tgt = m.get("target") or {}
        print("闪电一击：对 %s 造成伤害后 → 闪电链向哪一侧弹出？（不弹=直接回车）"
              % (tgt.get("name_tag") or tgt.get("name", "目标")))
        sides = m.get("sides") or []
        if not sides:
            print("  （场上没有可弹的两侧目标 —— 这一发不会有闪电链）")
        for i, c in enumerate(sides, 1):
            print("  %s %d. %s｜生命%s｜场上：%s(防%d)"
                  % (("[前一侧]" if i == 1 else "[后一侧]") if len(sides) > 1
                     else "[唯一一侧]",
                     c["idx"], c.get("name_tag") or c["name"], c["hp"],
                     c["plant"], c["fang"]))
        s = input("要弹向的玩家编号：").strip()
        try:
            v = int(s) if s else None
        except ValueError:
            v = None
        send_answer(sock, v)
    elif kind == "cat_split":
        # 【v0.39】猫尾草：连击 shots 发分配到左右两侧 → 答案 [左发数, 右发数]
        own = m.get("own") or {}
        _print_qfrom(m, own)
        shots = m.get("shots", 1)
        left = m.get("left") or {}
        right = m.get("right") or {}
        print("猫尾草：连击 %d 发 → 左右两侧分配各打几发" % shots)
        print("  左：%s｜生命%s｜场上：%s(防%d)"
              % (left.get("name_tag") or left.get("name", "?"), left.get("hp", "?"),
                 left.get("plant", "?"), left.get("fang", "?")))
        print("  右：%s｜生命%s｜场上：%s(防%d)"
              % (right.get("name_tag") or right.get("name", "?"), right.get("hp", "?"),
                 right.get("plant", "?"), right.get("fang", "?")))
        s = input("左侧打几发（0~%d，右侧自动补足；回车=全打左侧）：" % shots).strip()
        lc = int(s) if s.isdigit() and 0 <= int(s) <= shots else shots
        send_answer(sock, [lc, shots - lc])
    elif kind == "friend":
        own = m.get("own") or {}
        _print_qfrom(m, own)
        # 【v0.45】队友连接（107 番茄）：答案 = {"target":.., "gate":..}
        #   ★ 每加一种决策类型，这里必须跟着加一条 —— 漏了就会回 None →
        #     服务端判非法 → 原问题重发 → 死循环（v0.27 溅射踩过这个坑）。
        #   ★★【v0.61】"是否改触发[反馈逻辑]"已删 —— 反馈是强制的，不用问。
        cands = m.get("cands", [])
        gates = m.get("gates", [])
        print("队友连接：%s" % ((m.get("own") or {}).get("hint", "")))
        for c in cands:
            print("  %d. %s｜生命%s｜场上：%s(防%d)%s%s"
                  % (c["idx"], c.get("name_tag") or c["name"], c["hp"], c["plant"],
                     c["fang"], "（已是队友）" if c.get("ally") else "",
                     "　⚠连上必触发[反馈逻辑]" if c.get("merge") else ""))
        s = input("要连接的玩家编号（回车=空目标：不连人、也不加防）：").strip()
        try:
            t = int(s) if s else None
        except ValueError:
            t = None
        if t is None and not m.get("allow_empty", True):
            t = cands[0]["idx"] if cands else None   # 被猪笼草逼着、不能空目标
        g = None
        if gates:
            g = gates[0]["idx"]
            if len(gates) > 1:
                for i, x in enumerate(gates, 1):
                    print("  %d. 猪笼草：%s" % (i, x.get("name_tag") or x["idx"]))
                s2 = input("若猪笼草生效，偏移到哪一株的编号（回车=第一株）：").strip()
                try:
                    g = int(s2) if s2 else g
                except ValueError:
                    pass
        send_answer(sock, {"target": t, "gate": g})
    elif kind == "seed_heir":
        # 【v0.48】死亡时的种子瓜分：答案 = [[编号, 颗数], ...]，合计必须 = total。
        #   ★ 默认值就是引擎算好的平均方案，直接回车即接受。
        total = m.get("total", 0)
        cands = m.get("cands", [])
        dflt = {x["idx"]: x["n"] for x in (m.get("defaults") or [])}
        print("种子瓜分：你留下 %d 颗种子 —— 默认这样分（合计必须 = %d）："
              % (total, total))
        for c in cands:
            print("  %d. %s +%d 颗"
                  % (c["idx"], c.get("name_tag") or c["name"],
                     dflt.get(c["idx"], 0)))
        s = input("直接回车 = 接受默认；输入 y = 逐项修改：").strip().lower()
        pairs = [[c["idx"], dflt.get(c["idx"], 0)] for c in cands]
        if s in ("y", "yes", "1", "是"):
            pairs, left = [], total
            for c in cands:
                raw = input("  %d. %s 分几颗？（默认 %d，还剩 %d）："
                            % (c["idx"], c.get("name_tag") or c["name"],
                               dflt.get(c["idx"], 0), left)).strip()
                try:
                    k = int(raw) if raw else dflt.get(c["idx"], 0)
                except ValueError:
                    k = dflt.get(c["idx"], 0)
                k = max(0, min(k, left))
                pairs.append([c["idx"], k])
                left -= k
            if pairs and left > 0:
                pairs[-1][1] += left          # 没分完的都给最后一个
        send_answer(sock, pairs)
    elif kind == "revive":
        own = m.get("own") or {}
        _print_qfrom(m, own)
        # 【v0.51】161 生命之花：加血 / 复活二选一。
        #   答案 = {"mode": "heal"} 或 {"mode": "revive", "target": 编号}。
        #   ★ 又一种新决策类型 —— 这里漏写就会回 None → 判非法 → 原题重发 → 死循环。
        cands = m.get("cands", [])
        print("【%s】二选一（只能选一个）：" % m.get("plant", "生命之花"))
        print("  1. 自己 +%s 条命" % m.get("heal", "?"))
        for i, c in enumerate(cands, 2):
            print("  %d. 复活 %s｜恢复%s条命 · 取回%d颗种（死于%d回合）"
                  % (i, c.get("name_tag") or c.get("name", "?"), c.get("hp", "?"),
                     c.get("seeds", 0), c.get("died_round", 0)))
        s = input("选 1 = 加血；选 2~%d = 复活对应的人（回车 = 加血）："
                  % (len(cands) + 1)).strip()
        if s.isdigit() and 2 <= int(s) <= len(cands) + 1:
            send_answer({"mode": "revive", "target": cands[int(s) - 2]["idx"]})
        else:
            send_answer({"mode": "heal", "target": None})


if __name__ == "__main__":
    main()
