# -*- coding: utf-8 -*-
"""
《植物斗争》Python 样品入口（多人热座）
运行：python main.py
联机版请用 netserver.py + netclient.py / webui.py（【v0.68】起没有图形客户端了）。
规则与样品采用的解释见 游戏规则总结.txt。
"""
from bots import BotController
from console_ui import ConsoleController
from engine import Game
from plants_data import DEFAULT_RULES, RULE_OPTIONS, rules_of


def ask_int(prompt, lo, hi, default):
    while True:
        s = input(prompt).strip()
        if s == "" and default is not None:
            return default
        if s.isdigit() and lo <= int(s) <= hi:
            return int(s)
        print("请输入 %d-%d 的数字。" % (lo, hi))


def main():
    print("==========《植物斗争》Python 样品（多人热座） ==========")
    print("每回合：出牌 → 公示 → 博弈（是否释放/选目标）→ 结算。")
    n = ask_int("玩家人数（2-8，回车默认2）：", 2, 8, 2)
    controllers = []
    for i in range(n):
        t = input("player%d 是 人类(h) 还是 电脑(b)？（回车默认h）：" % (i + 1)).strip().lower()
        if t == "b":
            controllers.append(BotController())
        else:
            controllers.append(ConsoleController())
    # 【v0.22】开局种子（全场统一）：默认 0 = 空手起手，和以前完全一样。
    #   为什么要有它：开局种子为 0 时人人第一回合只能出种子，开局千篇一律
    #   （原稿的思路见 Python游戏开发优势.txt）。给几颗种子，第一回合就有选择。
    seeds = ask_int("开局种子（每人几颗，回车默认0）：", 0, 9999, 0)
    # 【v0.25】房间规则（默认 = 原稿口径）。热座版逐个问。
    #   ★★【v0.52】改成【按 RULE_OPTIONS 动态提问】。老写法是这里手写两行
    #     （丝瓜不封顶 / 充能前缀可变）；v0.49 加"丝瓜万岁"、v0.51 加"生命之神"
    #     时都忘了往这里补 —— 于是热座版**根本开不了那两条**，而
    #     《可选规则集.txt》第四节还写着"四个入口都支持"。这是典型的
    #     "界面各写一份元数据"导致的漂移，正解同上：以后加规则只动数据层。
    #   ★ 回车 = 取该项的**默认值**（不是一律"关"）—— mult_cap 这种反向开关
    #     默认值非 0，写死"回车=关"会把默认封顶 3 层变成不封顶。
    picks = {}
    for o in RULE_OPTIONS:
        default_on = (DEFAULT_RULES[o["key"]] == o["on"])
        ans = input("房间规则「%s」—— %s？（y/N，回车默认%s）："
                    % (o["name"], o["desc"],
                       "开" if default_on else "关")).strip().lower()
        if ans in ("y", "yes"):
            picks[o["key"]] = True
        elif ans in ("n", "no"):
            picks[o["key"]] = False
        else:
            picks[o["key"]] = default_on
    rules = rules_of(picks)
    Game(controllers, output=print, start_seeds=seeds, rules=rules).run()
    input("\n回车退出……")


if __name__ == "__main__":
    main()
