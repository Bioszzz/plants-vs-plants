# -*- coding: utf-8 -*-
"""
netproto.py —— 联机消息协议：TCP 上传 JSON，每条消息一行，utf-8 编码，\n 结尾。
消息类型（详见 游戏规则总结.txt 第十三节）：
  服务端 → 客户端：hello / msg / ask / wait / end
  客户端 → 服务端：answer / name

v0.5：ask 现在是【同时】发给所有人的（出牌 / 释放 / 充能 / 选目标各自一批）。
  wait 是新增的消息，用来告诉"这一轮没你事"的客户端把界面切成等待状态，
  否则他的面板会一直停在上一轮的按钮上。

v0.8：新增 ask 类型 targets（散连击的多选目标）。
  ask  kind="targets" → {count: 要选几个, cands:[{idx,name,hp,plant,fang}...]}
  answer 的 value 是【编号列表】，例如 [2, 3]；
  必须正好 count 个、互不重复、都在候选里，否则服务端判非法并把原问题重发。
  对照：kind="target"（单选）的 value 仍然是单个整数。

v0.15：end 消息带上结局的结构化字段（老客户端只读 text，自动忽略）：
  end → {text: "游戏结束：player2 获胜", winner: 2 或 None(平局/中止),
         state: 最终场面的 state_json()}
  另外重申一条边界：wait 有两种文案 —— 活人是"等待其他玩家决策……"，
  【出局的人】是"你已出局，正在观战……"。出局 ≠ 掉线：连接一直开着，
  场面一直推，将来加复活机制时出局的人可以直接回到场上。

v0.21：噬魂（吞噬）类上线，ask kind="target" / "targets" 的载荷加了三个字段
  （全是纯新增，老客户端读不认识就跳过，行为不变）：
    ask → {kind, shot, shots,
           own:   {plant, hint},          # hint 由植物自己声明（v0.17 规矩）
           cands: [{idx, name, hp, hp_units, plant,
                    fang,        # 当前防御
                    fang_eff,    # 有效防御 = fang − 水滴补的那部分
                    seeds,       # 对方手上的种子数（噬魂按比例夺，要看得见）
                    devour}]}    # null=攻方非噬魂；true/false=吞得动/吞不动
  ★ devour 由引擎算好下发，界面【不许】自己判 —— 否则会出现
    "界面说能点、引擎说无效"的欺骗。详见 开发文档 第七章。

v0.35：起名功能。新增一条【客户端 → 服务端】的消息：
  name → {type: "name", value: "小明"}
  · 它是【报到消息】，不是一次决策 —— 客户端连上后【立刻】发一条就行，
    服务端在后台读线程里就地收下（不占 ask/answer 那套一问一答）。
  · 服务端只在【建房那一刻】快照一次名字：谁报过名就用，没报的用默认 playerN。
    晚到的 name 本局不生效（避免打到一半名字突变）。
  · 名字是【显示名】：引擎认人一律用 idx，重复 / 重名都不影响判定。
  · 另外，所有"指认某个玩家"的下发字段（state 的 players、ask 的 cands /
    target / sides / same / targets）都【多了一个 name_tag】= "小明(2号)"，
    原来是 name 的那些字段原样保留（纯名），老客户端读老字段即可。
    ★ 默认名（player1）不带编号 —— 只有自己起的名字才补 "(N号)"。
"""
import json


def send_msg(sock, obj):
    data = json.dumps(obj, ensure_ascii=False) + "\n"
    sock.sendall(data.encode("utf-8"))


class Reader:
    """按行读取 JSON 消息；连接关闭后 recv() 返回 None。"""

    def __init__(self, sock):
        self._file = sock.makefile("r", encoding="utf-8")

    def recv(self):
        line = self._file.readline()
        if not line:
            return None
        return json.loads(line)
