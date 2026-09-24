#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""结算口径单测：炸弹分由输家承担（2026-09-24 用户拍板）。
用法：python test_settle_rule.py    （在 paodekuai 仓库根目录）
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import settle

H0 = [3, 4, 6, 9, 22, 24, 28, 29, 30, 31, 32, 34, 38, 40, 42, 45]     # A1737 人类(输)
H1 = [10, 11, 12, 14, 16, 17, 21, 23, 26, 35, 37, 39, 41, 43, 46, 48]  # A1737 AI(赢)
B = lambda seat, cards, pt: {"seat": seat, "cards": cards, "pass": False, "combo": {"ptype": pt}}
P = lambda seat: {"seat": seat, "cards": [], "pass": True, "combo": None}

# --- 1) A1737 实局重建：AI 胜、人类剩 10 张、人类一颗未压炸弹（旧口径 → 0 分）
moves_a1737 = [B(1, [10, 11, 12, 14, 16, 17, 21, 23], 2), B(0, [28, 29, 30, 31], 8), P(1),
               B(0, [3], 0), B(1, [26], 0), B(0, [38], 0), B(1, [46], 0), P(0),
               B(1, [48], 0), P(0), B(1, [37, 39, 41, 43], 2), P(0), B(1, [35], 0)]
r = settle.compute_result([H0, H1], moves_a1737, {}, 1)
assert (r["rem"], r["base"], r["bombs"]) == (10, 10, [0, 1]), r
assert r["delta"] == [-20, 20], f"A1737 新口径应为 [-20,20]，实际 {r['delta']}"
print("1) A1737 重建: rem=%d base=%d bombs=%s delta=%s  ✅（旧口径为 [0,0]）"
      % (r["rem"], r["base"], r["bombs"], r["delta"]))

# --- 2) 赢家持有未被压炸弹：输家多付（输家出过牌，不触发关门）
r2 = settle.compute_result([[1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16],
                            [17, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27, 28, 29, 30, 31, 32]],
                           [B(1, [17, 18, 19, 20], 8), B(0, [1], 0)], {}, 1)
assert (r2["rem"], r2["base"], r2["bombs"]) == (15, 15, [1, 0]), r2
assert r2["delta"] == [-25, 25], r2          # 底分 15 + 赢家炸弹 10
print("2) 赢家炸弹: rem=%d base=%d bombs=%s delta=%s" % (r2["rem"], r2["base"], r2["bombs"], r2["delta"]))

# --- 3) 无炸弹：只有底分
r3 = settle.compute_result([[1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16],
                            [17, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27, 28, 29, 30, 31, 32]],
                           [B(1, [17, 18], 1), B(0, [1, 2], 1), B(1, [19, 20], 1)], {}, 1)
assert (r3["rem"], r3["base"], r3["bombs"], r3["delta"]) == (14, 14, [0, 0], [-14, 14]), r3
print("3) 无炸弹: rem=%d base=%d delta=%s" % (r3["rem"], r3["base"], r3["delta"]))

# --- 4) 炸弹互压：先出的那颗被更大的炸弹直接压掉 → 不计分；后出的按规则必然未被压（无人再压）
r4 = settle.compute_result([[1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16],
                            [17, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27, 28, 29, 30, 31, 32]],
                           [B(1, [17, 18, 19, 20], 8), B(0, [1, 2, 3, 4], 8)], {}, 1)
assert (r4["bombs"], r4["delta"]) == ([0, 1], [-22, 22]), r4
print("4) 炸弹互压: bombs=%s（先出那颗被压掉不计）delta=%s" % (r4["bombs"], r4["delta"]))

# --- 5) 隔了过牌重新领出：不算被压，两颗都计
r5 = settle.compute_result([[1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16],
                            [17, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27, 28, 29, 30, 31, 32]],
                           [B(1, [17, 18, 19, 20], 8), P(0), B(1, [21, 22, 23, 24], 8),
                            B(0, [1], 0)], {}, 1)
assert (r5["bombs"], r5["delta"]) == ([2, 0], [-35, 35]), r5    # 15 + 10*2
print("5) 过牌后再出炸弹: bombs=%s delta=%s" % (r5["bombs"], r5["delta"]))

print("\n全部断言通过 ✅  新公式: dW = base + 10*(bw + bl)")
