# -*- coding: utf-8 -*-
"""
console_ui.py —— 控制台热座界面（一台电脑轮流操作）

继承 engine.SyncController，继续用老的 choose_* 写法即可。
热座是一台电脑一个键盘，本来就只能一个人一个人来：
引擎"同时下发"时这里只是把问题暂存，真正弹输入发生在 poll() 阶段。
"""

from engine import SyncController


class ConsoleController(SyncController):
    def notify(self, text):
        pass  # 热座版广播由 Game(output=print) 统一输出一份；notify 留给网络版

    # --- 内部小工具 ---
    @staticmethod
    def _ask(prompt, valid=None):
        while True:
            s = input(prompt).strip()
            if valid is None or s in valid:
                return s
            print("输入无效，请重新输入。")

    # --- 引擎会调用的三个决策 ---
    def choose_plant(self, game, player):
        input("—— 请 %s 操作（其他人请回避），按回车继续 ——" % player.tag)
        print("\n" * 6)  # 简单遮挡上一位玩家输入的植物名
        print("【%s 的出牌阶段】" % player.tag)
        print(game.state_text())
        print("可选植物：")
        print(game.catalog_text())
        return self._ask("要出的植物名（回车确认）：", valid=set(game.playable))

    def choose_release(self, game, player):
        print("\n【%s】场上是【%s】，要释放吗？" % (player.tag, player.field.spec.name))
        print("  y = 释放   → 维持原效果")
        print("  n = 不释放 → 本回合所有属性归零、没有任何效果")
        print("               （防御0、免疫失效、效果不发动、产种子的也不产），")
        print("                 且出牌时付的种子【不退】")
        s = self._ask("请选择(y/n)：", valid={"y", "n"})
        return s == "y"

    def choose_target(self, game, player, candidates, shot=0, shots=0):
        # 【v0.9】连击的"第几发"提示 —— ★ v0.44 起连击不再走这里
        #   （改成 choose_burst_split 一次性分配），此分支仅为兼容保留。
        if shot:
            print("【%s】连击第 %d/%d 发 —— 分配这一发给谁（可以重复选同一个人）："
                  % (player.field.spec.name, shot, shots))
        else:
            print("可选目标：")
        # 【v0.21】噬魂类：挑人之前先把"这一口吞不吞得动"摊开。
        #   说明文字由【植物自己】声明（v0.17 的老规矩），这里只是搬运工。
        hint = player.field.spec.devour_hint()
        if hint:
            print("  ★ 【%s】%s" % (player.field.spec.name, hint))
        for i, t in enumerate(candidates, 1):
            f = t.field
            line = "  %d. %s｜生命%s｜场上：%s(防%d)" % (
                i, t.tag, t.hp_text(), f.spec.name, f.fang)
            ok = game.devour_preview(player.field, f)
            if ok is not None:
                # 有效防御 = 扣掉【辅助防 P₁】那部分（噬魂无视全部辅助防）
                eff = max(0, f.fang - f.fang_aux)
                line += "｜有效防%d｜种子%d｜%s" % (
                    eff, t.seeds, "★吞得动" if ok else "×吞不动")
            print(line)
        while True:
            s = input("目标序号：").strip()
            if s.isdigit() and 1 <= int(s) <= len(candidates):
                return candidates[int(s) - 1]
            print("输入无效，请重新输入。")

    def choose_targets(self, game, player, candidates, count):
        """【v0.8】散连击：一次选 count 个【不同】的人，每个挨 1 击"""
        print("散连击：请选出【%d 个不同】的目标（每人挨 1 击，不能同一个人选两次）" % count)
        for i, t in enumerate(candidates, 1):
            f = t.field
            print("  %d. %s｜生命%s｜场上：%s(防%d)"
                  % (i, t.tag, t.hp_text(), f.spec.name, f.fang))
        while True:
            s = input("请输入 %d 个序号，空格或逗号隔开：" % count).strip()
            parts = [x for x in s.replace(",", " ").replace("，", " ").split() if x]
            if len(parts) != count:
                print("要正好选 %d 个，你输入了 %d 个。" % (count, len(parts)))
                continue
            if not all(x.isdigit() for x in parts):
                print("请只输入数字序号。")
                continue
            idxs = [int(x) for x in parts]
            if len(set(idxs)) != count:
                print("散连击必须选【不同】的人，不能重复。")
                continue
            if all(1 <= i <= len(candidates) for i in idxs):
                return [candidates[i - 1] for i in idxs]
            print("序号超出范围，请重新输入。")

    def choose_burst_split(self, game, player, candidates, shots):
        """【v0.44】连击发数分配：一次把 shots 发分给各目标。
        按候选顺序输入每个目标的【发数】（空格分隔；可给 0；合计必须 = shots）。"""
        print("【%s】连击 %d 发 —— 请【分配】每发打谁（可全压一人、可分散）："
              % (player.field.spec.name, shots))
        for i, t in enumerate(candidates, 1):
            f = t.field
            print("  %d. %s｜生命%s｜场上：%s(防%d)"
                  % (i, t.tag, t.hp_text(), f.spec.name, f.fang))
        while True:
            s = input("按上面顺序输入每个目标的发数（%d 个数字，空格分隔，合计=%d）："
                      % (len(candidates), shots)).strip()
            parts = [x for x in s.replace(",", " ").replace("，", " ").split() if x]
            if len(parts) != len(candidates) or not all(x.isdigit() for x in parts):
                print("要给【每个】目标各填一个非负整数发数。")
                continue
            counts = [int(x) for x in parts]
            if sum(counts) != shots:
                print("合计必须是 %d 发，你填的是 %d 发。" % (shots, sum(counts)))
                continue
            return [(candidates[i], counts[i]) for i in range(len(candidates))
                    if counts[i] > 0]

    def choose_charge(self, game, player):
        while True:
            s = input("【%s】充能：输入要消耗的种子数（现有%d颗；宣布数溢出 → 视为没有充能）："
                      % (player.tag, player.seeds)).strip()
            if s.isdigit():
                return int(s)
            print("请输入非负整数。")

    def choose_water_hp(self, game, player):
        """【v0.10】用 1 个水滴抵掉喷菇的半条命代价（出牌时先扣，选是则退还）"""
        s = self._ask("【%s】用 1 个水滴抵掉【%s】的半条命代价吗？"
                      "（现有水滴%d，生命%s）(y/n)："
                      % (player.tag, player.field.spec.name,
                         player.water, player.hp_text()),
                      valid={"y", "n"})
        return s == "y"

    def choose_water_def(self, game, player, water):
        """【v0.10】花 x 个水滴 → 本回合防御 +x（1:1）"""
        print("【%s】现有水滴%d，当前防御%d" % (player.tag, water, player.field.fang))
        while True:
            s = input("    花几个水滴加固？（1 个水滴 = +1 防御，直接回车=不花）：").strip()
            if s == "":
                return 0
            if s.isdigit() and 0 <= int(s) <= water:
                return int(s)
            print("    请输入 0~%d 之间的整数。" % water)

    def choose_splash(self, game, player, candidates, target=None):
        """【v0.27】溅射声明：对目标【两侧】各勾一次（可以都不勾）。

        "两侧是谁"由服务端算好发下来（存活环上目标的 index 相邻者，且不含你自己），
        界面只负责排版与问 —— 环、摘除、排除自己这些规则一个字都不许写在界面里。
        """
        who = (target or {}).get("name_tag") or (target or {}).get("name", "目标")
        print("【%s】若对 %s 造成伤害 → 要向两侧溅射吗？（每侧 0.5 条命·暴2）"
              % (player.field.spec.name, who))
        for i, t in enumerate(candidates, 1):
            f = t.field
            print("  %d. %s｜生命%s｜场上：%s(防%d)"
                  % (i, t.tag, t.hp_text(), f.spec.name, f.fang))
        while True:
            s = input("    要溅射的序号（空格隔开；直接回车 = 都不溅）：").strip()
            parts = [x for x in s.replace(",", " ").replace("，", " ").split() if x]
            if not parts:
                return []
            if not all(x.isdigit() for x in parts):
                print("    请只输入数字序号。")
                continue
            idxs = [int(x) for x in parts]
            if len(set(idxs)) != len(idxs):
                print("    同一个人只能选一次。")
                continue
            if all(1 <= i <= len(candidates) for i in idxs):
                return [candidates[i - 1] for i in idxs]
            print("    序号超出范围，请重新输入。")

    # ---- 【v0.32】刀叶类的三个口 ----
    def choose_cut_target(self, game, player, candidates):
        """刀类：选要消除谁。候选已在服务端按射程+免切过滤好了。
        【v0.44】输入 0（或直接回车）= 本回合不切任何人（保留防御）。"""
        spec = player.field.spec
        print("【%s】%s" % (spec.name, spec.cut_hint()))
        for i, t in enumerate(candidates, 1):
            f = t.field
            print("  %d. %s｜生命%s｜场上：%s(防%d)"
                  % (i, t.tag, t.hp_text(), f.spec.name, f.fang))
        while True:
            s = input("    要消除哪一位的植物？（输入序号；0 或回车 = 不切、保留防御）：").strip()
            if s in ("0", ""):
                return None
            if s.isdigit() and 1 <= int(s) <= len(candidates):
                return candidates[int(s) - 1]
            print("    序号超出范围，请重新输入。")

    def choose_cut_mode(self, game, player, target, same):
        """连切：同名卡 ≥2 株 → 切单株 / 全切同名卡。"""
        print("【%s】要消除 %s 的【%s】—— 场上同名卡共 %d 株："
              % (player.field.spec.name, target.tag,
                 target.field.spec.name, len(same)))
        for i, s in enumerate(same, 1):
            print("  %d. %s 的【%s】" % (i, s.tag, s.field.spec.name))
        while True:
            v = input("    1=只切这一株；2=全切同名卡（连切）：").strip()
            if v == "1":
                return "single"
            if v == "2":
                return "all"
            print("    请输入 1 或 2。")

    def choose_cut_half(self, game, player, targets):
        """切一半 / 全切：目标里有丝瓜/鱼腥草这类。"""
        names = "、".join("%s 的【%s】" % (t.tag, t.field.spec.name) for t in targets)
        while True:
            v = input("【%s】对 %s 要『切一半』还是『全切』？"
                      "（切一半=防御保留、负面效果不外溢；全切=成[空]）(y=切一半/n=全切)："
                      % (player.field.spec.name, names)).strip().lower()
            if v in ("y", "n"):
                return v == "y"
            print("    请输入 y 或 n。")

    def choose_cut_help(self, game, player, candidates):
        """【v0.32】切一半 → 帮谁切（多选，含自己；0~N；空输入=不帮）。"""
        print("【%s】要帮谁免疫该植物的负面效果？（可多选，含自己）"
              % player.field.spec.name)
        for i, t in enumerate(candidates, 1):
            f = t.field
            print("  %d. %s｜生命%s｜场上：%s(防%d)"
                  % (i, t.tag, t.hp_text(), f.spec.name, f.fang))
        while True:
            s = input("    输入序号（空格/逗号分隔，可直接回车=不帮任何人）：").strip()
            if not s:
                return []
            parts = s.replace(",", " ").split()
            if all(x.isdigit() and 1 <= int(x) <= len(candidates) for x in parts):
                seen, out = set(), []
                for x in parts:
                    i = int(x)
                    if i not in seen:
                        seen.add(i)
                        out.append(candidates[i - 1])
                return out
            print("    序号超出范围，请重新输入。")

    # ---- 【v0.38】电系：勾人 ／ 【v0.57】103 牛角菱：勾取 ----
    def choose_absorb(self, game, player, candidates):
        """聚电：多选要收走谁（0~N；空输入=不收）。

        ★【v0.62】勾取（103）复用这里，但它的合法性是"恰好 hook_same 株、
          且【同名】"。旧版本这里一问都不问就收下答案，玩家选 3 株 /
          跨组两株 → 网络客户端那边判非法重发 → 看着像"输入无效"死循环。
          这里先在本地按同一口径挡住（数字全部读植物自己身上的属性）。
        """
        spec = player.field.spec
        need = getattr(spec, "hook_same", 0) or 0
        if need:
            print("【%s】勾取 —— 选中 %d 株【同名】植物（回车 = 不勾，走攻击形态）"
                  % (spec.name, need))
        else:
            print("【%s】要聚电谁？（可收：%s）"
                  % (spec.name, "、".join(spec.absorb_targets)))
        for i, t in enumerate(candidates, 1):
            f = t.field
            print("  %d. %s｜生命%s｜场上：%s(防%d)"
                  % (i, t.tag, t.hp_text(), f.spec.name, f.fang))
        while True:
            s = input("    输入序号（空格/逗号分隔，可直接回车=%s）："
                      % ("走攻击形态" if need else "不勾任何人")).strip()
            if not s:
                return []
            parts = s.replace(",", " ").split()
            if all(x.isdigit() and 1 <= int(x) <= len(candidates) for x in parts):
                seen, out = set(), []
                for x in parts:
                    i = int(x)
                    if i not in seen:
                        seen.add(i)
                        out.append(candidates[i - 1])
                if need:
                    names = {c.field.spec.name for c in out}
                    if len(out) != need or len(names) != 1:
                        print("    勾取必须恰好 %d 株【同名】的植物（现在选了 %d 株、"
                              "%d 种），请重新输入。" % (need, len(out), len(names)))
                        continue
                return out
            print("    序号超出范围，请重新输入。")

    def choose_flash(self, game, player, candidates, target=None):
        """[闪电一击] 声明：闪电链向两侧之一弹出（单选，或回车=不弹）。"""
        who = target.tag if target else "目标"
        print("【%s】对 %s 造成伤害后 → 闪电链向哪一侧弹出？（两边之一，可都不弹）"
              % (player.field.spec.name, who))
        for i, s in enumerate(candidates, 1):
            print("  %d. %s｜生命%s｜场上：%s(防%d)"
                  % (i, s.tag, s.hp_text(), s.field.spec.name, s.field.fang))
        while True:
            s = input("    输入序号（可直接回车=不弹闪电链）：").strip()
            if not s:
                return None
            if s.isdigit() and 1 <= int(s) <= len(candidates):
                return candidates[int(s) - 1]
            print("    序号超出范围，请重新输入。")

    def choose_cat_split(self, game, player, left, right, shots):
        """【v0.39】猫尾草：连击 shots 发分配到左右两侧。返回 [左发数, 右发数]。"""
        print("【%s】连击 %d 发 → 存活环上离你最近的是左右这两个，"
              "请分配各打几发："
              % (player.field.spec.name, shots))
        print("  左：%s｜生命%s｜场上：%s(防%d)"
              % (left.tag, left.hp_text(), left.field.spec.name, left.field.fang))
        print("  右：%s｜生命%s｜场上：%s(防%d)"
              % (right.tag, right.hp_text(), right.field.spec.name, right.field.fang))
        while True:
            s = input("    左侧打几发（0~%d，右侧自动补足）：" % shots).strip()
            if s.isdigit() and 0 <= int(s) <= shots:
                lc = int(s)
                return [lc, shots - lc]
            print("    范围 0~%d，请重新输入。" % shots)

    # ---- 【v0.45】队友连接（107 番茄）----
    def choose_friend(self, game, player, candidates, gates, allow_empty):
        """选"连谁"。返回 (目标 Player|None, 门 Player|None)。

        ★★【v0.61】[反馈逻辑] 不再问 —— 它是**强制的**（用户 2026-10-23）。
          这里只在选完之后**预告**一句"这一连会触发反馈"，不给 y/n。
        """
        spec = player.field.spec
        print("【%s】%s" % (spec.name, spec.friend_hint()))
        for i, t in enumerate(candidates, 1):
            f = t.field
            ally = "（已是队友：只加防、不再连链）" if game.same_team(player, t) else ""
            print("  %d. %s｜生命%s｜场上：%s(防%d)%s"
                  % (i, t.tag, t.hp_text(), f.spec.name, f.fang, ally))
        # ① 连谁（或空目标）
        target = None
        while True:
            tip = ("    要连接谁？（输入序号%s）："
                   % ("；0 或回车 = 空目标（不连人、也不加防）" if allow_empty
                      else "；★场上有【猪笼草】→ 不能选空目标"))
            s = input(tip).strip()
            if s in ("0", ""):
                if allow_empty:
                    target = None
                    break
                print("    场上有【猪笼草】队友门，不能选空目标。")
                continue
            if s.isdigit() and 1 <= int(s) <= len(candidates):
                target = candidates[int(s) - 1]
                break
            print("    序号超出范围，请重新输入。")
        # ② 猪笼草偏移（多株才需要选）
        gate = None
        if gates:
            if len(gates) == 1:
                gate = gates[0]
                print("    （场上只有一株猪笼草 → 若它生效，连接会被偏移到 %s）"
                      % gate.tag)
            else:
                for i, g in enumerate(gates, 1):
                    print("  %d. 猪笼草：%s｜生命%s｜防%d"
                          % (i, g.tag, g.hp_text(), g.field.fang))
                while True:
                    s = input("    若猪笼草生效，连接偏移到哪一株？（1~%d）："
                              % len(gates)).strip()
                    if s.isdigit() and 1 <= int(s) <= len(gates):
                        gate = gates[int(s) - 1]
                        break
                    print("    序号超出范围，请重新输入。")
        # ③ ★ 反馈是强制的 → 只在"这一连会把两队并成一队"时预告后果，不问 y/n
        #   （有猪笼草时不预告：目标会被偏移，是否合并到结算期才有定论）
        if (not gates and target is not None and not game.same_team(player, target)
                and len(game.team_groups()) == 2):
            print("    ⚠ 全场只剩两个队友链，连上 %s 就会并成一个 → "
                  "今回【必定】触发[反馈逻辑]（你自己成[空]，得 %d 颗种）"
                  % (target.tag, spec.feedback_value))
        return target, gate

    def choose_seed_heir(self, game, player, candidates, total, defaults):
        """【v0.48】死亡时的 [瓜分]：默认平均，可以改。

        ★ 这是唯一一道"问死人"的题 —— 他刚失去最后一条命，
          界面上要让人一眼看出"你的种子要分给谁"。
        """
        d = {x["idx"]: x["n"] for x in defaults}
        plan = [(c, d.get(c.idx, 0)) for c in candidates]
        print("【种子瓜分】你留下 %d 颗种子，默认这样分：" % total)
        for i, (c, n) in enumerate(plan, 1):
            print("  %d. %s +%d 颗" % (i, c.tag, n))
        s = self._ask("    回车 = 接受；输入 y = 逐项修改：", valid={"", "y"})
        if s != "y":
            return plan
        out, left = [], total
        for i, (c, n) in enumerate(plan, 1):
            raw = input("  %d. %s 分几颗？（默认 %d，还剩 %d 颗可分配）："
                        % (i, c.tag, n, left)).strip()
            k = int(raw) if raw.isdigit() else n
            k = max(0, min(k, left))
            out.append((c, k))
            left -= k
        if out and left > 0:                 # 没分完的都给最后一个，别让种子凭空蒸发
            c, k = out[-1]
            out[-1] = (c, k + left)
        return out

    # ---- 【v0.51】161 生命之花：加血 / 复活 二选一 ----
    def choose_revive(self, game, player, candidates):
        """返回 ("heal", None) 或 ("revive", Player)。

        · candidates —— 可复活的人（引擎已过滤掉"本回合刚死的"）
        """
        spec = player.field.spec
        print("【%s】二选一（只能选一个）：" % spec.name)
        print("  (1) 自己 +%g 条命（现有 %s 条）"
              % (spec.heal / 2.0, player.hp_text()))
        for i, t in enumerate(candidates, 1):
            snap = t.death_snapshot or {}
            print("  (%d) 复活 %s：恢复 %s 条命、取回 %d 颗种子"
                  "（他死在 %d 回合）"
                  % (i + 1, t.tag, game.hp_str(game._revive_hp(t)),
                     snap.get("seeds", 0), snap.get("round", 0)))
        while True:
            s = input("    选 1 = 加血；选 2~%d = 复活对应的人（回车 = 加血）："
                      % (len(candidates) + 1)).strip()
            if s in ("", "1"):
                return ("heal", None)
            if s.isdigit() and 2 <= int(s) <= len(candidates) + 1:
                return ("revive", candidates[int(s) - 2])
            print("    范围 1~%d，请重新输入。" % (len(candidates) + 1))
