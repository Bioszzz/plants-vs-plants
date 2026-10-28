# -*- coding: utf-8 -*-
"""
bots.py —— 极简电脑对手（热座版 main.py 与联机服务端 netserver.py 共用）
只为了单人试玩/凑人数用，不追求强度。

继承 SyncController：引擎下发问题后，电脑在 poll() 时瞬间算好答案，
不占用"同时问"的等待时间。
"""
import random

from engine import SyncController


class BotController(SyncController):

    def notify(self, text):
        pass  # 电脑保持安静；想看引擎发给它的所有广播，把这行改成 print(text)

    def choose_plant(self, game, player):
        if player.seeds >= 5:
            return "杨桃"
        if player.seeds >= 2 and random.random() < 0.35:
            return "白菜"
        if random.random() < 0.35:
            return "木盾"
        return "种子"

    def choose_release(self, game, player):
        # 【v0.7】不释放 = 本回合属性全归零、出牌时付的种子还不退，
        # 对电脑这种不会谈判骗人的玩家毫无收益，所以一律释放。
        return True

    def choose_target(self, game, player, candidates, shot=0, shots=0):
        # 【v0.9】连击的每一发都打血最少的人（等于把子弹全堆给最脆的那个）
        return min(candidates, key=lambda t: t.hp)

    def choose_targets(self, game, player, candidates, count):
        """【v0.8】散连击：血最少的 count 个人，一人挨一下"""
        return sorted(candidates, key=lambda t: t.hp)[:count]

    def choose_charge(self, game, player):
        return player.seeds  # 有多少充多少

    def choose_water_hp(self, game, player):
        # 【v0.10】电脑一律用水滴抵命 —— 生命是硬资源，水滴还能再攒
        return True

    def choose_water_def(self, game, player, water):
        # 【v0.10】电脑把水滴全砸在防御上（它不会谈判，稳一点没坏处）
        return water
