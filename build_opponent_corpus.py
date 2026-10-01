#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""对手模型训练语料清单 —— 「117 局主导 + 2063 局副」口径的可执行产物。

用户 2026-10-01 拍板的口径：
  - **primary（主导）**：带 player_id 的目标指纹对局 + 启发式归属到他的（strong/probable）
    ——身份确定，直接高权重；
  - **secondary（副）**：指纹采集上线前的无归属对局 ——**极可能也主要是他**（历史期他是
    主力玩家、玩法特征与指纹期差异 <0.02），但无法逐局证明，故降权使用。

输出 `data/opponent_corpus.json`：
  {
    "target_pid": "pmumwpgia907dsc7g",
    "generated": "...",
    "weights": {"primary": 1.0, "secondary": 0.3},
    "primary":   [{"no": "...", "date": "...", "source": "fingerprint|attribution:strong", "moves": 13}, ...],
    "secondary": [{"no": "...", "date": "...", "moves": 13}, ...],
    "stats": {...}
  }

训练侧（引擎线）用法：按 `weights` 给每局行为样本加权；**primary 是语料主干**，
secondary 只用于提升对手模型的覆盖度（尤其防守/应对风格），不应主导分布。

为什么降权而不是丢弃：secondary 有 2000+ 局、覆盖大量局面与牌型，量级优势明显；
但身份未证实，若与真实他本人差异较大（存在其他玩家），等权使用会把噪声灌进模型。
0.3 是一个「有用但不主导」的折中，可在 A/B 时按 0.15 / 0.3 / 0.6 扫。

用法：python build_opponent_corpus.py [--pid ...] [--secondary-weight 0.3]
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import time
from collections import Counter

BASE = "/home/ubuntu/paodekuai"
REPLAY_GLOB = os.path.join(BASE, "data", "replays", "*.json")
ATTR_PATH = os.path.join(BASE, "data", "player_attribution.json")
OUT = os.path.join(BASE, "data", "opponent_corpus.json")
DEFAULT_PID = "pmumwpgia907dsc7g"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pid", default=DEFAULT_PID)
    ap.add_argument("--secondary-weight", type=float, default=0.3)
    a = ap.parse_args()

    attr = {}
    try:
        data = json.load(open(ATTR_PATH, encoding="utf-8"))
        attr = {str(k): v for k, v in (data.get("attribution") or {}).items() if isinstance(v, dict)}
    except Exception:
        print("(没有 player_attribution.json，secondary 段将包含全部历史局)")

    primary, secondary = [], []
    for p in glob.glob(REPLAY_GLOB):
        try:
            d = json.load(open(p, encoding="utf-8"))
        except Exception:
            continue
        if d.get("live") or d.get("winner") is None:
            continue
        mv = d.get("moves") or d.get("codes") or []
        if not mv:
            continue
        no = str(d.get("no") or os.path.basename(p))
        row = {"no": no, "date": str(d.get("timeText") or "")[:10],
               "moves": len(mv), "file": os.path.basename(p)}
        pid = d.get("player_id")
        if pid == a.pid:
            row["source"] = "fingerprint"
            primary.append(row)
            continue
        at = attr.get(no)
        if at and at.get("pid") == a.pid:
            row["source"] = f"attribution:{at.get('conf', '?')}"
            primary.append(row)
            continue
        row["source"] = "unattributed_history"
        secondary.append(row)

    primary.sort(key=lambda r: (r["date"], r["no"]))
    secondary.sort(key=lambda r: (r["date"], r["no"]))

    src = Counter(r["source"] for r in primary)
    doc = {
        "_comment": "对手模型训练语料清单（build_opponent_corpus.py 生成）。"
                    "primary=身份确定的主导语料；secondary=指纹前历史局，降权使用。"
                    "归属是启发式推断（见 attribute_history.py），不是事实。",
        "target_pid": a.pid,
        "generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "weights": {"primary": 1.0, "secondary": a.secondary_weight},
        "primary": primary,
        "secondary": secondary,
        "stats": {
            "primary_n": len(primary),
            "primary_by_source": dict(src),
            "primary_moves": sum(r["moves"] for r in primary),
            "secondary_n": len(secondary),
            "secondary_moves": sum(r["moves"] for r in secondary),
            "effective_weight_ratio": (len(primary) * 1.0) /
                                      max(1e-9, len(secondary) * a.secondary_weight),
        },
    }

    tmp = OUT + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=1)
    os.replace(tmp, OUT)

    st = doc["stats"]
    print(f"✅ 已写入 {OUT}")
    print(f"   primary   : {st['primary_n']} 局 / {st['primary_moves']} 手  {dict(src)}")
    print(f"   secondary : {st['secondary_n']} 局 / {st['secondary_moves']} 手  (权重 {a.secondary_weight})")
    print(f"   有效权重比 : primary : secondary = {st['effective_weight_ratio']:.2f} : 1")
    print()
    print("   训练侧用法：按 weights 逐局加权；primary 是分布主干，secondary 只补充覆盖度。")
    print("   建议在 A/B 里扫 secondary 权重 0.15 / 0.3 / 0.6，别默认就当定值。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
