#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""对局结算（人类规则）—— 纯 Python，无第三方依赖，可被桥与网页服务共用。
**唯一实现处**：ai_bridge.compute_result / replay_report.save_game15 / online_server 都调它，
网页版 app.js settle() 与之逐条对齐（改口径必须两边一起改）。

规则（2026-10-01 用户拍板，与 app.js settle() 逐条对齐）：
- 底分 = 输家剩余张数（剩 1 张不计分）；
- 关门（输家一手未出）失分 ×2；
- **炸弹：每颗未被压掉的炸弹，让"打出它的那一方" +10 分，不扣对方**（不论输赢）。
  后手用更大炸弹**直接**压掉才不计分；隔了过牌重新领出仍算未被压掉。
  ⚠ 与旧口径的差别（这正是 09-24/09-25 两次误判的根源）：
    - 最旧口径「炸弹持有者向被炸方收 10」= 零和（输家炸了反而倒扣）；
    - 09-24 中途口径「炸弹一律由输家承担」= 输家炸了更亏；
    - **现行：只给持有者加分** ⇒ 输家炸得够多时，**输了净分仍可能为正**（例：剩 3 张=底分 3，
      1 颗炸弹 ⇒ −3+10 = **+7**）。分数**不再零和**，双方都可能为正。
- 红桃十翻倍（opts.red10）：♥10 持有者**本人的本局净分** ×2（可正可负）。

影响面（口径非零和后必须一起看）：
- 累计分/排行榜/仪表盘的"净分差"不再是两人互为相反数，读数按各人自己的净值看；
- 引擎侧 `pdk/engine.py::_settle` 仍是"炸弹持有者向被炸方收"的旧口径（引擎线领地），
  H5 展示与 AI 内部目标函数因此不一致，见 [[pdk-ai-deploy-path]]。
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

    # 底分仍是零和（赢家 +base / 输家 −base）；炸弹分**只加给持有者**，不扣对方。
    score = [0, 0]
    score[winner] += base
    score[loser] -= base
    score[winner] += BOMB_SCORE * bw
    score[loser] += BOMB_SCORE * bl

    red_txt = ""
    if opts.get("red10"):
        holder = next((s for s in (0, 1) if HEART_10 in (hands[s] or [])), None)
        if holder is not None:
            who = "你" if holder == 0 else "AI"
            score[holder] *= 2                        # 持有者本人净分 ×2（不再强制对手取反）
            red_txt = f"{who} 持有红桃十，翻倍"

    delta = [int(score[0]), int(score[1])]
    return {"winner": winner, "loser": loser, "rem": rem, "shut": bool(shut),
            "base": base, "bombs": [bw, bl], "redTxt": red_txt, "delta": delta}