#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""对局结算（人类规则）—— 纯 Python，无第三方依赖，可被桥与网页服务共用。

规则（与 app.js settle() 逐条对齐）：
- 底分 = 输家剩余张数（剩 1 张不计分）；
- 关门（输家一手未出）失分 ×2；
- 未被压掉的炸弹每颗 ±10（后手用更大炸弹**直接**压掉才不算，隔了过牌重新领出不算压）；
- 红桃十翻倍（opts.red10：♥10 持有者所在一方输赢 ×2，随后锁定）；
- 选项开关按传入 opts 解释（sanzhang/four3 不影响计分）。
"""

HEART_10 = 29                       # ♥10 的牌 id（rankIdx 7 × 4 + suit 1）


def compute_result(hands, moves, opts, winner) -> dict:
    """返回本局结算 dict：winner/loser/rem/shut/base/bombs/redTxt/delta。

    hands: [[座位0初始手牌id], [座位1初始手牌id]]；moves: 逐手明细
    （[{seat, cards:[id], pass}]，combo 仅用于识别炸弹 ptype==8）。
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
    dW = base + 10 * (bw - bl)
    dL = -dW
    red_txt = ""
    if opts.get("red10"):
        holder = next((s for s in (0, 1) if HEART_10 in (hands[s] or [])), None)
        if holder is not None:
            who = "你" if holder == 0 else "AI"
            if holder == winner:
                dW *= 2
                dL = -dW
            else:
                dL *= 2
                dW = -dL
            red_txt = f"{who} 持有红桃十，翻倍"
    delta = [0, 0]
    delta[winner] = dW
    delta[loser] = dL
    return {"winner": winner, "loser": loser, "rem": rem, "shut": bool(shut),
            "base": base, "bombs": [bw, bl], "redTxt": red_txt, "delta": delta}
