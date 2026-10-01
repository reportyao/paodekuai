#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把无指纹的历史对局归属到指定玩家指纹 —— 基于 IP 交集 + 时间邻近的启发式。

为什么需要：player_id 从 2026-09-29 才上线，此前 2000+ 局无身份字段。目标锁定
「打赢某个指纹」后，需要尽可能把历史行为样本归到他名下（对手模型训练要用）。

方法（**启发式，不是事实**）：
  1. 从 web 日志解析所有 `POST /ai/init` → (时间, IP)；
  2. 「他的 IP 集」= 指纹采集上线后出现过的 IP；
     「强证据 IP」= 其中在历史期也出现过的（同设备换网络仍会复用同一批出口 IP）；
  3. 每局无指纹棋谱取其 ts，回查时间最近（±90s）的 init：
       命中强证据 IP → strong      命中他的其它 IP → probable      否则 → none

输出 data/player_attribution.json（**独立文件，不改棋谱本体**）：
  {"attribution": {"A1234": {"pid":..., "conf":"strong", "ip":..., "gap":90}}, ...}
  棋谱是权威数据，归属是推断，所以只写旁挂文件，随时可删可重算。

用法：python attribute_history.py [--pid pmumwpgia907dsc7g] [--dry-run]
"""
from __future__ import annotations

import argparse
import bisect
import glob
import json
import os
import re
from collections import Counter
from datetime import datetime, timedelta, timezone

BASE = "/home/ubuntu/paodekuai"
WEB_LOG = "/var/log/paodekuai-web.log"
OUT = os.path.join(BASE, "data", "player_attribution.json")
FINGERPRINT_FROM = "2026-09-30"      # 指纹采集起始日（含）
WINDOW_S = 90                        # init 与开局时间对齐窗口（秒）
DAY_DOMINANT = 0.5                   # 当日归属阈值：他的 IP 占当日 init 比例 ≥ 此值才归属
LOG_RE = re.compile(r'^(\S+) \S+ \S+ \[(\d{2}/\w{3}/\d{4} \d{2}:\d{2}:\d{2})(?: ([+-]\d{4}))?\] "POST /ai/init')
TZ8 = timezone(timedelta(hours=8))          # 服务器本地时区（CST）


def load_inits() -> list[tuple[float, str]]:
    out = []
    try:
        f = open(WEB_LOG, "r", errors="replace")
    except PermissionError:
        # web.log 属 root:wheel 644? —— 通常可读；若不可读，给出可执行的提示而非静默失败
        print(f"✗ 无权读取 {WEB_LOG}（需要 sudo 读日志）")
        print("  请改用： sudo /home/ubuntu/paodekuai/.venv/bin/python "
              "/home/ubuntu/paodekuai/attribute_history.py")
        raise SystemExit(1)
    except OSError as e:
        print(f"✗ 打不开 {WEB_LOG}: {e}")
        return out
    with f:
        for line in f:
            if "POST /ai/init" not in line:
                continue
            m = LOG_RE.match(line)
            if not m:
                continue
            # 日志时间无时区偏移 ⇒ 按服务器本地时区(CST)解释（http.server 的本地时间）
            try:
                dt = datetime.strptime(m.group(2), "%d/%b/%Y %H:%M:%S")
            except ValueError:
                continue
            dt = dt.replace(tzinfo=m.group(3) and None or TZ8)
            out.append((dt.timestamp(), m.group(1)))
    out.sort()
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pid", default="pmumwpgia907dsc7g")
    ap.add_argument("--dry-run", action="store_true", help="只统计不落盘")
    a = ap.parse_args()

    inits = load_inits()
    if not inits:
        print("✗ 没解析到 init 日志")
        return 1
    print(f"init 记录: {len(inits)}")

    fp_start = datetime.strptime(FINGERPRINT_FROM, "%Y-%m-%d").replace(tzinfo=TZ8).timestamp()
    his = {ip for ts, ip in inits if ts >= fp_start}
    shared = {ip for ip in his if any(ts < fp_start and ip2 == ip for ts, ip2 in inits)}
    print(f"他的 IP 集({len(his)}): {sorted(his)}")
    print(f"强证据 IP(历史期也出现, {len(shared)}): {sorted(shared)}")

    # 确认该指纹确实在采集（防止 pid 打错导致归属到空集）
    n_fp = 0
    for p in glob.glob(os.path.join(BASE, "data", "replays", "*.json")):
        try:
            d = json.load(open(p, encoding="utf-8"))
        except Exception:
            continue
        if d.get("player_id") == a.pid:
            n_fp += 1
    print(f"目标指纹 {a.pid} 现存棋谱: {n_fp} 局")
    if n_fp == 0:
        print("✗ 该指纹没有任何棋谱，归属无意义")
        return 1

    ts_list = [t for t, _ in inits]

    def nearest(target: float):
        i = bisect.bisect_left(ts_list, target)
        best = None
        for j in (i - 1, i, i + 1):
            if 0 <= j < len(inits):
                gap = abs(inits[j][0] - target)
                if best is None or gap < best[0]:
                    best = (gap, inits[j][1])
        return best

    # 当日各 IP 的 init 次数（用于「当日活跃」置信度）
    from collections import defaultdict
    day_ip = defaultdict(Counter)
    for ts, ip in inits:
        day_ip[datetime.fromtimestamp(ts, TZ8).date().isoformat()][ip] += 1

    attribution = {}
    cnt = Counter()
    ip_cnt = Counter()
    for p in glob.glob(os.path.join(BASE, "data", "replays", "*.json")):
        try:
            d = json.load(open(p, encoding="utf-8"))
        except Exception:
            continue
        if d.get("player_id") or d.get("live") or d.get("winner") is None:
            continue
        ts = d.get("ts")
        if not ts:
            continue
        try:
            gdt = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        gts = gdt.timestamp()
        day = gdt.astimezone(TZ8).date().isoformat()   # 对齐到服务器本地日（CST）

        r = nearest(gts)
        gap, ip = (r if r else (10 ** 9, None))

        # ① 时间邻近命中（最强）：±WINDOW_S 内的一条 init 属于他的 IP
        if ip and gap <= WINDOW_S and (ip in shared or ip in his):
            conf = "strong" if ip in shared else "probable"
        else:
            # ② 当日回退：当天他的 IP 有活动，且当天他（强证据 IP）的 init 量占大头
            #    —— 覆盖"init 早于开局很久 / 跨过零点"的长对局
            c = day_ip.get(day)
            if not c:
                cnt["none"] += 1
                continue
            tot = sum(c.values())
            his_n = sum(v for k, v in c.items() if k in his)
            sh_n = sum(v for k, v in c.items() if k in shared)
            if sh_n == 0:
                cnt["none"] += 1
                continue
            frac_sh = sh_n / tot
            frac_his = his_n / tot
            if frac_sh >= DAY_DOMINANT:
                conf = "day_strong" if frac_his >= DAY_DOMINANT else "day_probable"
                ip = max((k for k in c if k in shared), key=lambda k: c[k])
            elif frac_his >= DAY_DOMINANT:
                conf = "day_probable"
                ip = max((k for k in c if k in his), key=lambda k: c[k])
            else:
                cnt["none"] += 1
                continue
        cnt[conf] += 1
        ip_cnt[ip] += 1
        attribution[str(d.get("no") or os.path.basename(p))] = {
            "pid": a.pid, "conf": conf, "ip": ip, "gap": int(min(gap, 10 ** 6)),
        }

    print()
    print("=== 归属结果（无指纹历史局） ===")
    print(f"  strong       : {cnt['strong']}  ±90s 命中交集IP（最可信）")
    print(f"  day_strong   : {cnt['day_strong']}  当日交集IP占主导（可信）")
    print(f"  probable     : {cnt['probable']}  ±90s 命中他的其它IP")
    print(f"  day_probable : {cnt['day_probable']}  当日他的IP占主导（参考）")
    print(f"  none         : {cnt['none']}  未归属")
    print("  按 IP:", dict(ip_cnt.most_common()))
    print()
    print("  ⚠ 这是启发式归属：移动网络 IP 会漂移、同 IP 可能被同网段他人共享。")
    print("    strong 可当高置信样本用于对手模型训练；probable 建议只作参考。")

    if a.dry_run:
        print("\n(--dry-run，未落盘)")
        return 0
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    prev = {}
    if os.path.exists(OUT):
        try:
            prev = json.load(open(OUT, encoding="utf-8")).get("attribution", {})
        except Exception:
            prev = {}
    merged = {**prev, **attribution}
    tmp = OUT + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"_comment": "启发式 IP 归属（见 attribute_history.py）；不是事实，改口径重跑即可",
                   "pid": a.pid, "attribution": merged}, f, ensure_ascii=False, indent=1)
    os.replace(tmp, OUT)
    print(f"\n✅ 已写入 {OUT}（累计 {len(merged)} 条；棋谱本体未改）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
