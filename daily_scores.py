#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""按天汇总实际总分（现行炸弹口径）。

用法：python daily_scores.py [天数]      默认 5 天
口径提醒：炸弹分只加给持有者 ⇒ **非零和**，两人净值之和 = 10×当日未压炸弹总数（可能为正）。
"""
import json, glob, os, sys
from collections import defaultdict
from datetime import datetime, timedelta

BASE = "/home/ubuntu/paodekuai"
DAYS = int(sys.argv[1]) if len(sys.argv) > 1 else 5

by_day = defaultdict(lambda: {"games": 0, "ai_wins": 0, "hu": 0, "ai": 0,
                              "bombs": 0, "rem_sum": 0, "bomb_games": 0, "red10_games": 0,
                              "pos_loser": 0, "shut": 0})

for p in glob.glob(f"{BASE}/data/replays/*.json") + glob.glob(f"{BASE}/data/replays15/*.json"):
    try:
        d = json.load(open(p, encoding="utf-8"))
    except Exception:
        continue
    if d.get("live") or d.get("aborted") or d.get("winner") is None:
        continue
    r = d.get("result") or {}
    delta = r.get("delta")
    if not (isinstance(delta, (list, tuple)) and len(delta) == 2):
        continue
    day = str(d.get("timeText") or "")[:10]
    if not day:
        continue
    e = by_day[day]
    e["games"] += 1
    e["ai_wins"] += 1 if d["winner"] == 1 else 0
    e["hu"] += int(delta[0]); e["ai"] += int(delta[1])
    b = r.get("bombs") or [0, 0]
    e["bombs"] += sum(b)
    if sum(b) > 0:
        e["bomb_games"] += 1
    if r.get("shut"):
        e["shut"] += 1
    if (d.get("opts") or {}).get("red10"):
        e["red10_games"] += 1
    if r.get("rem") is not None:
        e["rem_sum"] += int(r["rem"])
    loser = 1 - int(d["winner"])
    if int(delta[loser]) > 0:
        e["pos_loser"] += 1

days = sorted(by_day)[-DAYS:]
print("=" * 100)
print(f"最近 {len(days)} 天实际总分汇总（口径：炸弹 +10 归持有者、不扣对方 ⇒ 非零和）")
print("=" * 100)
print("%-12s %5s %8s %9s %9s %8s %7s %7s %8s" %
      ("日期", "局数", "AI胜率", "人类总分", "AI总分", "炸弹颗数", "含炸局", "关门", "输了净正"))
print("-" * 100)
for day in days:
    e = by_day[day]
    print("%-12s %5d %7.1f%% %+9d %+9d %8d %7d %7d %8d" % (
        day, e["games"], 100.0 * e["ai_wins"] / e["games"], e["hu"], e["ai"],
        e["bombs"], e["bomb_games"], e["shut"], e["pos_loser"]))
print("-" * 100)
tg = sum(by_day[d]["games"] for d in days)
th = sum(by_day[d]["hu"] for d in days)
ta = sum(by_day[d]["ai"] for d in days)
tb = sum(by_day[d]["bombs"] for d in days)
taw = sum(by_day[d]["ai_wins"] for d in days)
print("%-12s %5d %7.1f%% %+9d %+9d %8d" % ("合计", tg, 100.0 * taw / max(1, tg), th, ta, tb))
print()
print("校验：两人分和 %+d  vs  10×炸弹总数 %+d  ⇒ %s"
      % (th + ta, 10 * tb, "一致 ✅" if th + ta == 10 * tb else "不一致 ❌"))
print("说明：非零和 ⇒ 每局两人分之和 = 10×该局未压炸弹数（无炸弹的局为 0），累计同理。")
print("     「人类总分 / AI 总分」是各自净值，不是互为相反数。")