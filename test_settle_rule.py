#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""结算口径单测：**炸弹只给持有者 +10，不扣对方**（2026-10-01 用户拍板）。
关键性质：**输家炸得够多时，输了净分仍可能为正**；整体**不是零和**。

用法：python test_settle_rule.py    （在 paodekuai 仓库根目录）
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import settle

H0 = [3, 4, 6, 9, 22, 24, 28, 29, 30, 31, 32, 34, 38, 40, 42, 45]
H1 = [10, 11, 12, 14, 16, 17, 21, 23, 26, 35, 37, 39, 41, 43, 46, 48]


def B(seat, cards, pt):
    return {"seat": seat, "cards": cards, "pass": False, "combo": {"ptype": pt}}


def P(seat):
    return {"seat": seat, "cards": [], "pass": True, "combo": None}


FULL16 = [[i for i in range(0, 16)], [i for i in range(16, 32)]]
BOMB = B(0, [0, 1, 2, 3], 8)          # 四张同点 = 炸弹
OTHER = B(1, [16, 17], 1)             # 一手对子

# --- 1) A1737 实局：AI 胜、人类剩 10 张、人类打出 1 颗未被压的炸弹
#        人类净分 = -10(底分) + 10(自己的炸弹) = 0；AI = +10(底分)，**不再被扣**
moves_a1737 = [B(1, [10, 11, 12, 14, 16, 17, 21, 23], 2), B(0, [28, 29, 30, 31], 8), P(1),
               B(0, [3], 0), B(1, [26], 0), B(0, [38], 0), B(1, [46], 0), P(0),
               B(1, [48], 0), P(0), B(1, [37, 39, 41, 43], 2), P(0), B(1, [35], 0)]
r = settle.compute_result([H0, H1], moves_a1737, {}, 1)
assert (r["rem"], r["base"], r["bombs"]) == (10, 10, [0, 1]), r
assert r["delta"] == [0, 10], f"新口径应为 [0,10]（旧两种口径都是 [0,0]），实际 {r['delta']}"
print(f"1) A1737 重建: rem={r['rem']} base={r['base']} bombs={r['bombs']} delta={r['delta']}"
      f"  ✅（人类自己的炸弹回到自己头上，不再倒扣）")

# --- 2) ★核心性质：输了但炸弹够多 ⇒ 净分为正
#        输家剩 3 张（底分 3）+ 1 颗未压炸弹 ⇒ -3 + 10 = **+7**
LOSE_POS_MOVES = [B(0, [0, 1, 2, 3], 8),          # 炸弹
                  B(1, [16, 17], 1), B(0, [4, 5, 6, 7, 8], 4),
                  B(1, [18, 19], 1), B(0, [9, 10], 1),
                  B(1, [20, 21], 1), B(0, [11, 12], 1)]
r2 = settle.compute_result([[i for i in range(16)], [i for i in range(16, 32)]],
                          LOSE_POS_MOVES, {}, 1)
assert (r2["rem"], r2["base"], r2["bombs"]) == (3, 3, [0, 1]), r2
assert r2["delta"] == [7, 3], f"输了净分必须为正（-3+10=+7），实际 {r2['delta']}"
print(f"2) 输了但炸弹加分后净正: delta={r2['delta']} (rem={r2['rem']} base={r2['base']})  ✅")

# --- 3) 赢家持有炸弹：赢家加分，输家**不被扣**
h3b = [[16] + list(range(17, 32)), [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15]]
m3 = [B(1, [0, 1, 2, 3], 8), B(0, [16, 17], 1), B(1, [4, 5], 1)]
r3 = settle.compute_result(h3b, m3, {}, 1)
assert r3["bombs"] == [1, 0] and r3["delta"][1] > -r3["base"], r3
print(f"3) 赢家炸弹: bombs={r3['bombs']} delta={r3['delta']}  ✅（输家不受炸弹影响，只是底分）")

# --- 4) 非零和校验：双方炸弹分相加 = 总炸弹数 × 10（不互为相反数）
r4 = settle.compute_result([[i for i in range(16)], [i for i in range(16, 32)]],
                          [BOMB, OTHER], {}, 1)
assert r4["delta"][0] + r4["delta"][1] == 10 * sum(r4["bombs"]), r4
print(f"4) 非零和性质: delta={r4['delta']} 和={r4['delta'][0]+r4['delta'][1]}"
      f" = 10×炸弹{r4['bombs']}  ✅")

# --- 5) 炸弹互压：先出的那颗被更大炸弹压掉 ⇒ 不计分；后出的按规则必然未被压
# 座位1 先出炸弹、座位0 紧接着用更大炸弹压掉 ⇒ 先出那颗被压掉不计分
m5 = [B(0, [0, 1, 2, 3], 8), B(1, [16, 17, 18, 19], 8)]
r5 = settle.compute_result([[i for i in range(16)], [i for i in range(16, 32)]], m5, {}, 1)
assert r5["bombs"] == [1, 0], r5
print(f"5) 炸弹互压: bombs={r5['bombs']}（先出那颗被压掉不计）delta={r5['delta']}")

# --- 6) 红十翻倍：底分转移 ×2（输赢都翻、双方关联）；♥10 在底牌则不翻
#        场景：人类(座位0)持 ♥10(29)、打出 1 颗未压炸弹且只剩 3 张
h0 = [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 11, 12, 13, 14, 15, 29]
h1 = list(range(16, 32))
assert settle.HEART_10 in h0
m6 = [B(0, [0, 1, 2, 3], 8),      # 炸弹（rank0 四张）
      P(1), B(0, [4, 5], 1), P(1),  # rank1 对子
      B(0, [8, 9], 1), P(1),       # rank2 对子
      B(0, [6, 7], 1), P(1),       # rank1 对子
      B(0, [12, 13], 1), P(1),     # rank3 对子
      B(0, [14], 0)]               # rank3 单张 → 人类共出 13 张，剩 3 张（11/15/29）
plain = settle.compute_result([h0, h1], m6, {}, 1)
r6 = settle.compute_result([h0, h1], m6, {"red10": True}, 1)
assert (plain["rem"], plain["bombs"]) == (3, [0, 1]), plain   # bombs=[赢家,输家]：人类是输家
assert plain["delta"] == [7, 3], plain                       # 无翻倍：-3+10=+7 / +3
assert r6["redTxt"], r6
# 输赢都翻：人类是输家 ⇒ 底分转移 ×2 ⇒ 人类 -6+10=+4，AI +6（关联：AI 的 +6 就是人类付的 6）
assert r6["delta"] == [4, 6], r6
assert r6["delta"][1] == 2 * plain["delta"][1], (r6["delta"], plain["delta"])   # AI 侧 ×2
assert r6["delta"][0] == -2 * plain["base"] + 10, r6                            # 人类 -2×3+10
print(f"6) 红十翻倍(输赢都翻): 无翻倍={plain['delta']} → 翻倍后={r6['delta']}"
      f"（底分转移×2，双方关联）  ✅")

# --- 7) ♥10 被扣进底牌：无人持有 ⇒ 不翻倍（settle 只按 hands 判 holder）
k0 = [x for x in h0 if x != 29] + [16]     # 仍是 16 张、不含 29
k1 = [x for x in h1 if x != 29] + [17]
assert settle.HEART_10 not in k0 and settle.HEART_10 not in k1
r7 = settle.compute_result([k0, k1], m6, {"red10": True}, 1)
assert r7["delta"] == plain["delta"], (r7["delta"], plain["delta"])   # 与无红十完全一致
assert not r7["redTxt"], r7
print(f"7) ♥10 在底牌: delta={r7['delta']}（不翻倍，redTxt 空）  ✅")

# --- 8) 恒等式现在无条件成立：sum(delta) == 10×未压炸弹数（红十局也成立）
for opts in ({}, {"red10": True}):
    rr = settle.compute_result([h0, h1], m6, opts, 1)
    assert rr["delta"][0] + rr["delta"][1] == 10 * sum(rr["bombs"]), (opts, rr)
print(f"8) 恒等式无条件成立（含红十局）: sum == 10×炸弹  ✅")

print("\n全部断言通过 ✅")
print(f"现行公式： 底分转移 m×base（红十被持有时 m=2，输赢都翻、双方关联）"
      f"+ 炸弹 各+10×自身未压炸弹数（不扣对方、不参与翻倍）⇒ 恒等式 sum==10×炸弹 无条件成立")