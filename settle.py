#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""对局结算（人类规则）—— 纯 Python，无第三方依赖，可被桥与网页服务共用。
**唯一实现处**：ai_bridge.compute_result / replay_report.save_game15 / online_server 都调它，
网页版 app.js settle() 与之逐条对齐（改口径必须两边一起改）。

规则（2026-10-12 定稿，与 app.js settle() 逐条对齐）：
- 底分 = 输家剩余张数（剩 1 张不计分）；
- 关门（输家一手未出）失分 ×2；
- **炸弹：每颗未被压掉的炸弹，让"打出它的那一方" +10 分，不扣对方**（不论输赢）。
  后手用更大炸弹**直接**压掉才不计分；隔了过牌重新领出仍算未被压掉。
  ⇒ 输家炸得够多时，**输了净分仍可能为正**（例：剩 3 张=底分 3，1 颗炸弹 ⇒ −3+10 = **+7**）。
- **红桃十翻倍（opts.red10）**：♥10 被某个玩家持有时，**底分转移 ×2**（赢家 +2×底分、
  输家 −2×底分，**输赢都翻**、双方关联）；♥10 被扣进底牌 = 无人持有 ⇒ 不翻。
  **炸弹分不参与翻倍**（永远各 +10）。
- ⚠ 分数**仍是非零和**（炸弹分只加给持有者）：sum(delta) = 10×未压炸弹总数（恒等式
  无条件成立——底分转移恒零和、炸弹恒为外加项）。读排行榜/看板按各人自己的净值看。
- 引擎侧 `pdk/engine.py::_settle` 仍是旧口径（炸弹零和 + 红十对手取反），与 H5 不一致，
  对齐需引擎线改 C 核心，见 [[pdk-ai-deploy-path]]。
"""

HEART_10 = 29                       # ♥10 的牌 id（rankIdx 7 × 4 + suit 1）
BOMB_SCORE = 10                     # 每颗未被压掉的炸弹：持有者得分（不扣对方）


def compute_result(hands, moves, opts, winner) -> dict:
    """返回本局结算 dict：winner/loser/rem/shut/base/bombs/redTxt/delta。

    hands: [[座位0初始手牌id], [座位1初始手牌id]]；moves: 逐手明细
    （[{seat, cards:[id], pass}]，combo 仅用于识别炸弹 ptype==8）。

    delta = [座位0净分, 座位1净分]（**不保证互为相反数**：炸弹分只加给持有者）。
    """
    winner = int(winner)
    loser = 1 - winner
    opts = opts or {}
    played = [0, 0]
    bombs = []                                   # [{by, beaten}]
    prev_live = None                             # 上一手非过牌且中间无过牌（= 可被直接压）
    for m in moves or []:
        if not isinstance(m, dict) or m.get("pass") or not (m.get("cards")):
            prev_live = None
            continue
        seat = int(m.get("seat") or 0)
        played[seat] += len(m["cards"])
        combo = m.get("combo") or {}
        is_bomb = (combo.get("ptype") == 8)
        if is_bomb and prev_live is not None and (prev_live.get("combo") or {}).get("ptype") == 8:
            for b in reversed(bombs):            # 更大炸弹直接压掉上家炸弹：被压的不计分
                if not b["beaten"]:
                    b["beaten"] = True
                    break
        if is_bomb:
            bombs.append({"by": seat, "beaten": False})
        prev_live = m
    rem = max(0, len(hands[loser] or []) - played[loser])
    shut = played[loser] == 0
    base = 0 if rem == 1 else rem
    if shut:
        base *= 2
    surv = [b for b in bombs if not b["beaten"]]
    bw = sum(1 for b in surv if b["by"] == winner)
    bl = len(surv) - bw

    # 底分转移（零和）：赢家 +base / 输家 −base。红十翻倍翻在**这笔转移**上——
    # 赢家翻倍赢的分就是输家付的分（关联），所以双方同时 ×2。
    # 触发条件：opts.red10 且 ♥10 真的被某个玩家持有（被扣进底牌 = 无人持有，不翻）；
    # **输赢都翻**（2026-10-12 用户拍板，传统规则：红十是风险与收益并存）。
    # 炸弹分在转移之外：各 +10 归打出者，不扣对方、**不参与翻倍**。
    mult = 1
    red_txt = ""
    if opts.get("red10"):
        holder = next((s for s in (0, 1) if HEART_10 in (hands[s] or [])), None)
        if holder is not None:
            mult = 2
            who = "你" if holder == 0 else "AI"
            red_txt = f"{who} 持有红桃十，翻倍"
    score = [0, 0]
    score[winner] += base * mult
    score[loser] -= base * mult
    score[winner] += BOMB_SCORE * bw
    score[loser] += BOMB_SCORE * bl

    delta = [int(score[0]), int(score[1])]
    return {"winner": winner, "loser": loser, "rem": rem, "shut": bool(shut),
            "base": base, "bombs": [bw, bl], "redTxt": red_txt, "delta": delta}