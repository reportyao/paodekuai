#!/usr/bin/env python3
"""线上 AI 胜率滚动仪表盘 (2026-09-29 审查 P1: 线上无验证能力 → 制度化读数)。

数据源: data/_replay_index.json (桥侧每局自动登记, 结构 {v, rows:{key: game}})。
口径 (与 2026-09-29 三源审查一致):
  - humanSeat 恒 0; AI 胜 = winner is not None and winner != humanSeat。
  - aborted / winner 缺失局剔除; deck16 (前缀 A) 为主口径, deck15 (B) 单列。
  - 变体标签: opts 四维 (sanzhang/nobomb/red10/four3) → base/red10/nb/nb+red10/f3*。

输出 (每次运行覆盖):
  data/winrate_report.json — 机器可读全量
  data/winrate_report.md   — 人读摘要 (含 7d/14d 窗口、变体分层、日表、
                             供 ab_verdict --traffic-weights 刷新的流量构成)

部署: crontab -e 加一行 (ubuntu):
  */30 * * * * cd /home/ubuntu/paodekuai && .venv/bin/python scripts/ai_winrate_report.py >> data/winrate_report.cronlog 2>&1
用法: python3 scripts/ai_winrate_report.py [--data-dir data] [--days 14]
"""
import argparse
import datetime as _dt
import json
import math
from collections import defaultdict
from pathlib import Path


def wilson(k, n, z=1.96):
    """Wilson 区间下上界 (比例)。"""
    if n == 0:
        return (float("nan"), float("nan"))
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return ((c - h) / d, (c + h) / d)


def variant_label(opts):
    o = opts if isinstance(opts, dict) else {}
    parts = []
    if o.get("sanzhang"):
        parts.append("sz")
    if o.get("nobomb"):
        parts.append("nb")
    if o.get("red10"):
        parts.append("red10")
    if o.get("four3"):
        parts.append("f3")
    return "+".join(parts) if parts else "base"


def pct(k, n):
    if not n:
        return "-"
    lo, hi = wilson(k, n)
    return f"{100*k/n:.1f}% [{100*lo:.1f},{100*hi:.1f}]"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default="data")
    ap.add_argument("--days", type=int, default=14)
    a = ap.parse_args()
    root = Path(__file__).parent.parent
    data = root / a.data_dir
    idx = json.loads((data / "_replay_index.json").read_text(encoding="utf-8"))
    rows = idx.get("rows", {})

    today = _dt.date.today()
    horizon = _dt.date.fromordinal(today.toordinal() - (a.days - 1))

    buckets = defaultdict(lambda: [0, 0])       # (n, ai_win)
    by_day = defaultdict(lambda: [0, 0])        # deck16 only
    by_variant_all = defaultdict(lambda: [0, 0])
    by_variant_recent = defaultdict(lambda: [0, 0])
    latest = ""
    for g in rows.values():
        w = g.get("winner")
        if w is None:
            continue
        pref = g.get("_prefix", "?")
        ai_win = int(w != g.get("humanSeat", 0))
        ts = str(g.get("ts", ""))
        latest = max(latest, ts)
        key_all = ("all", pref)
        buckets[key_all][0] += 1
        buckets[key_all][1] += ai_win
        if pref == "A":
            d = ts[:10]
            by_day[d][0] += 1
            by_day[d][1] += ai_win
            lab = variant_label(g.get("opts") or {})
            by_variant_all[lab][0] += 1
            by_variant_all[lab][1] += ai_win
            try:
                if _dt.date.fromisoformat(d) >= horizon:
                    by_variant_recent[lab][0] += 1
                    by_variant_recent[lab][1] += ai_win
                    buckets[("recent", "A")][0] += 1
                    buckets[("recent", "A")][1] += ai_win
            except ValueError:
                pass

    rep = {"generated": _dt.datetime.now().isoformat(timespec="seconds"),
           "latest_game_ts": latest, "horizon_days": a.days,
           "note": "AI win = winner != humanSeat; deck16=prefix A; deck15=B; external=E(桥外部调用,多为程序对打,不代表真人)",
           "overall": {}, "variant_all": {}, "variant_recent": {},
           "by_day": {}}
    deck_name = {"A": "deck16", "B": "deck15", "E": "external"}
    for (scope, pref), (n, k) in sorted(buckets.items()):
        if pref not in deck_name:
            continue
        lo, hi = wilson(k, n)
        rep["overall"][f"{scope}_{deck_name[pref]}"] = {
            "n": n, "ai_win": k, "rate": k / n if n else None,
            "ci95": [lo, hi]}

    # ── A4: 部署窗口切片 (data/deploy_registry.json, 无则跳过) ──
    deploys = []
    dr = data / "deploy_registry.json"
    if dr.exists():
        try:
            deploys = sorted(json.loads(dr.read_text(encoding="utf-8"))
                             .get("deploys", []),
                             key=lambda d: str(d.get("ts", "")))
        except Exception as e:                                    # noqa: BLE001
            rep["deploy_error"] = f"deploy_registry 解析失败: {e}"
    win_stats = []
    for i, dep in enumerate(deploys):
        t0, t1 = str(dep.get("ts", "")), (str(deploys[i + 1]["ts"])
                                          if i + 1 < len(deploys) else "9999")
        n = k = 0
        for g in rows.values():
            if g.get("_prefix") != "A" or g.get("winner") is None:
                continue
            ts = str(g.get("ts", ""))
            if t0 <= ts < t1:
                n += 1
                k += int(g.get("winner") != g.get("humanSeat", 0))
        lo, hi = wilson(k, n)
        win_stats.append({"tag": dep.get("tag", "?"), "ts": t0, "n": n,
                          "ai_win": k, "rate": k / n if n else None,
                          "ci95": [lo, hi],
                          "official": n >= 400})
    if deploys:
        rep["deploy_windows"] = win_stats

    # ── B4: 分人画像 (player_id 就位后自动生效; A3 部署前为空) ──
    by_pid = {}
    for g in rows.values():
        pid = str(g.get("player_id") or "")
        if not pid or g.get("_prefix") != "A" or g.get("winner") is None:
            continue
        e = by_pid.setdefault(pid, {"n": 0, "ai_win": 0, "first": "", "last": ""})
        ts = str(g.get("ts", ""))
        e["n"] += 1
        e["ai_win"] += int(g.get("winner") != g.get("humanSeat", 0))
        e["first"] = min(e["first"] or ts, ts)
        e["last"] = max(e["last"] or ts, ts)
    rep["players"] = {pid: v for pid, v in
                      sorted(by_pid.items(), key=lambda kv: -kv[1]["n"])[:50]}
    for lab in sorted(by_variant_all, key=lambda x: -by_variant_all[x][0]):
        n, k = by_variant_all[lab]
        lo, hi = wilson(k, n)
        rep["variant_all"][lab] = {"n": n, "ai_win": k, "rate": k / n if n else None,
                                   "ci95": [lo, hi]}
    tw = {}
    for lab in sorted(by_variant_recent, key=lambda x: -by_variant_recent[x][0]):
        n, k = by_variant_recent[lab]
        lo, hi = wilson(k, n)
        rep["variant_recent"][lab] = {"n": n, "ai_win": k, "rate": k / n if n else None,
                                      "ci95": [lo, hi]}
        tw[lab] = round(n / max(1, sum(v[0] for v in by_variant_recent.values())), 3)
    rep["traffic_weights_last14d"] = tw
    for d in sorted(by_day):
        n, k = by_day[d]
        rep["by_day"][d] = {"n": n, "ai_win": k, "rate": k / n if n else None}

    (data / "winrate_report.json").write_text(
        json.dumps(rep, ensure_ascii=False, indent=1), encoding="utf-8")

    L = []
    L.append(f"# 线上 AI 胜率滚动报告\n\n生成: {rep['generated']} | 最新对局: {latest} | 近{a.days}天变体构成即流量权重\n")
    L.append("## 总览 (AI 视角, Wilson 95% CI)\n")
    for name, s in rep["overall"].items():
        L.append(f"- **{name}**: {pct(s['ai_win'], s['n'])}  (n={s['n']})")
    L.append("\n## 变体分层 (deck16)\n")
    L.append("| 变体 | 全量 | 近14天 |")
    L.append("|---|---|---|")
    for lab in rep["variant_all"]:
        sall = rep["variant_all"][lab]
        srec = rep["variant_recent"].get(lab, {"n": 0, "ai_win": 0})
        L.append(f"| {lab} | {pct(sall['ai_win'], sall['n'])} (n={sall['n']}) "
                 f"| {pct(srec['ai_win'], srec['n'])} (n={srec['n']}) |")
    L.append("\n## 近14天流量构成 (→ 刷新 pdk-ai/traffic_weights.json)\n")
    L.append("```json\n" + json.dumps(tw, ensure_ascii=False, indent=1) + "\n```")
    L.append("\n## 日表 (deck16)\n")
    L.append("| 日期 | 局数 | AI 胜率 |")
    L.append("|---|---|---|")
    for d in sorted(rep["by_day"], reverse=True):
        s = rep["by_day"][d]
        L.append(f"| {d} | {s['n']} | {pct(s['ai_win'], s['n'])} |")
    if win_stats:
        L.append("\n## 部署窗口 (A4: 每次部署后的正式读数, ≥400 局为有效)\n")
        L.append("| 部署 | 窗口起 | 局数 | AI 胜率 | 正式读数 |")
        L.append("|---|---|---|---|---|")
        for s in win_stats:
            mark = "✅ 正式" if s["official"] else "待满400局(现%d)" % s["n"]
            L.append("| %s | %s | %d | %s | %s |"
                     % (s["tag"], s["ts"][:16], s["n"],
                        pct(s["ai_win"], s["n"]), mark))
    if rep.get("players"):
        L.append("\n## 分人画像 (B4, player_id top50)\n")
        L.append("| player_id | 局数 | AI 胜率 | 首局 | 末局 |")
        L.append("|---|---|---|---|---|")
        for pid, e in list(rep["players"].items())[:20]:
            L.append(f"| {pid[:10]}… | {e['n']} | {pct(e['ai_win'], e['n'])} "
                     f"| {e['first'][:10]} | {e['last'][:10]} |")
    else:
        L.append("\n## 分人画像 (B4)\n\n> player_id 尚无数据（A3 采集 2026-09-29 上线，自上线局起积累）。")
    L.append("\n> 判读纪律: 单日 n<100 的读数 CI ±8pt 以上, 只看趋势; 部署收益验证用"
             " ≥400 局滚动窗口 (≈1pt 分辨)。变体间差异大 (2026-09-29 审查: 主变体 ~45%,"
             " f3 ~77%), 混合口径会互相稀释。")
    (data / "winrate_report.md").write_text("\n".join(L), encoding="utf-8")
    print(f"report written: {data/'winrate_report.md'}")


if __name__ == "__main__":
    main()
