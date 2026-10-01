#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""对指定玩家指纹的作战基线 —— 「稳定打赢某人」的可重复测量。

背景（2026-10-01）：目标锁定单个对手指纹（主力用户 pmumwpgia9…）。全局胜率会被
其他玩家稀释，且不同玩家水平差异极大（此前三档读数 43%~57%），所以**对一个人的
胜率/净分必须单独跟踪**，否则改动效果会被混合口径互相稀释。

用法：
  python opponent_scorecard.py                      # 默认目标玩家
  python opponent_scorecard.py --pid pmumwpgia907dsc7g
  python opponent_scorecard.py --all                # 列出所有玩家（按局数）
  python opponent_scorecard.py --target-win 0.55    # 自定义目标线（默认 0.55）
  python opponent_scorecard.py --min-games 30       # 正式读数所需最小样本

输出：按 data/deploy_registry.json 的部署窗口切段 + 总体读数（Wilson 95% CI），
并对照目标线给出「达标 / 未达标 / 样本不足」。
"""
from __future__ import annotations

import argparse
import glob
import json
import math
import os
from collections import defaultdict

BASE = "/home/ubuntu/paodekuai"
REPLAY_GLOB = os.path.join(BASE, "data", "replays", "*.json")
REGISTRY = os.path.join(BASE, "data", "deploy_registry.json")
DEFAULT_PID = "pmumwpgia907dsc7g"          # 主力用户（设备级匿名指纹）
TARGET_WIN = 0.55                           # 「稳定打赢」的胜率目标线
MIN_GAMES = 30                              # 正式读数最小样本（Wilson ±10pt 内）


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n <= 0:
        return (0.0, 0.0)
    p = k / n
    den = 1 + z * z / n
    c = (p + z * z / (2 * n)) / den
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return (100 * (c - h), 100 * (c + h))


def load_windows() -> list[tuple[str, str]]:
    """→ [(tag, ts)] 按时间升序。"""
    try:
        data = json.load(open(REGISTRY, encoding="utf-8"))
    except Exception:
        return []
    deps = data.get("deploys") if isinstance(data, dict) else data
    out = []
    for d in deps or []:
        ts = str(d.get("ts") or "")
        tag = str(d.get("tag") or ts[:10])
        if ts:
            out.append((tag, ts[:19]))
    out.sort(key=lambda x: x[1])
    return out


def segment_of(t: str, windows) -> str:
    label = "(无部署记录)"
    for tag, ts in windows:
        if t >= ts:
            label = tag
    return label


ATTR_PATH = os.path.join(BASE, "data", "player_attribution.json")


def load_attribution() -> dict:
    """no -> pid（启发式归属，见 attribute_history.py）。棋谱本体无 player_id 时并入基线。"""
    try:
        data = json.load(open(ATTR_PATH, encoding="utf-8"))
    except Exception:
        return {}
    return {str(k): str(v.get("pid")) for k, v in (data.get("attribution") or {}).items()
            if isinstance(v, dict) and v.get("pid")}


def load_games(pid: str | None) -> list[dict]:
    attr = load_attribution()
    rows = []
    for p in glob.glob(REPLAY_GLOB):
        try:
            d = json.load(open(p, encoding="utf-8"))
        except Exception:
            continue
        owner = d.get("player_id") or attr.get(str(d.get("no")))
        if pid and owner != pid:
            continue
        if d.get("live") or d.get("aborted") or d.get("winner") is None:
            continue
        r = d.get("result") or {}
        delta = r.get("delta") if isinstance(r, dict) else None
        if delta is None:
            delta = d.get("scores")
        if not (isinstance(delta, (list, tuple)) and len(delta) == 2):
            continue
        mv = d.get("moves") or d.get("codes") or []
        if not mv:
            continue
        rows.append({
            "t": str(d.get("timeText") or ""),
            "no": d.get("no"),
            "win": int(d["winner"]) == 1,     # True = AI 胜（座位1）
            "d_ai": int(delta[1]),
            "d_hu": int(delta[0]),
            "pid": owner if owner else "(无归属)",
            "attr": (not d.get("player_id")) and bool(owner),
        })
    rows.sort(key=lambda x: x["t"])
    return rows


def report(rows, windows, pid_label, target, min_games) -> int:
    if not rows:
        print(f"没有找到 {pid_label} 的已终局对局")
        return 1
    n = len(rows)
    ai_w = sum(1 for r in rows if r["win"])
    lo, hi = wilson(ai_w, n)
    net = sum(r["d_hu"] for r in rows)

    print(f"═══ 对手作战基线：{pid_label} ═══")
    print(f"样本：{n} 局已终局（{rows[0]['t'][:16]} ~ {rows[-1]['t'][:16]}）")
    print(f"AI 胜率：{100*ai_w/n:.1f}%   Wilson95% = [{lo:.1f}, {hi:.1f}]")
    print(f"人类净分：累计 {net:+d}   局均 {net/n:+.2f}")
    print(f"目标线：AI 胜率 ≥ {100*target:.0f}%（且人类局均净分 < 0）")
    print()

    seg = defaultdict(list)
    for r in rows:
        seg[segment_of(r["t"], windows)].append(r)
    print("── 按部署窗口切段 ──")
    for tag in sorted(seg, key=lambda k: seg[k][0]["t"]):
        g = seg[tag]
        m = len(g)
        w = sum(1 for x in g if x["win"])
        gnet = sum(x["d_hu"] for x in g)
        glo, ghi = wilson(w, m)
        print(f"  [{tag}]  n={m:<4} AI胜率={100*w/m:5.1f}% [{glo:.0f},{ghi:.0f}]  人类局均={gnet/m:+.2f}")

    print()
    print("── 判据 ──")
    if n < min_games:
        print(f"  ⚠ 样本不足（{n} < {min_games}）：CI 太宽，只能看趋势，不能当正式读数。")
    if lo >= target * 100:
        print(f"  ✅ 达标：CI 下界 {lo:.1f}% 已高于目标线 {100*target:.0f}%（稳定打赢）。")
    elif hi < target * 100:
        print(f"  ❌ 未达标：CI 上界 {hi:.1f}% 仍低于目标线 {100*target:.0f}%。")
    else:
        print(f"  ➖ 未定：CI [{lo:.1f}, {hi:.1f}] 跨过目标线，需继续积累到 n≥{min_games}。")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="对单个玩家指纹的作战基线")
    ap.add_argument("--pid", default=DEFAULT_PID, help="玩家指纹（默认主力用户）")
    ap.add_argument("--all", action="store_true", help="列出全部玩家")
    ap.add_argument("--target-win", type=float, default=TARGET_WIN)
    ap.add_argument("--min-games", type=int, default=MIN_GAMES)
    a = ap.parse_args()

    windows = load_windows()
    if a.all:
        by = defaultdict(list)
        for r in load_games(None):
            by[r["pid"]].append(r)
        print("═══ 全部玩家（含启发式归属，按有效局数） ═══")
        for pid, g in sorted(by.items(), key=lambda kv: -len(kv[1])):
            m = len(g)
            w = sum(1 for x in g if x["win"])
            net = sum(x["d_hu"] for x in g)
            lo, hi = wilson(w, m)
            print(f"  {pid:<20} n={m:<4} AI胜率={100*w/m:5.1f}% [{lo:.0f},{hi:.0f}]  人类局均={net/m:+.2f}  {g[0]['t'][:10]}~{g[-1]['t'][:10]}")
        return 0
    return report(load_games(a.pid), windows, a.pid, a.target_win, a.min_games)


if __name__ == "__main__":
    raise SystemExit(main())
