#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""按现行结算口径**回填历史对局分数**（2026-10-01 炸弹口径变更）。

背景：炸弹规则改了两轮（最旧「持有者向被炸方收」→ 09-24「一律由输家承担」→
**现行「只给持有者 +10，不扣对方」**），历史棋谱的 `result`/`scores` 是当时口径算的，
与现在对不上。用户拍板：**全部回填重算**。

安全设计（改 3500+ 存档，必须可回滚）：
1. **先全量备份** data/replays、data/replays15 到 data/_backups/<时间戳>/（tar.gz），
   并写一份 manifest（文件清单 + md5）⇒ 出问题可整体还原；
2. **只写 result/scores 两个字段**，其余字段（moves/hands/opts/player_id/…）原样保留；
3. 逐局 **先算后比**：只有新分与旧分不同才落盘（避免无谓改动与索引抖动）；
4. 写盘用 **临时文件 + os.replace** 原子替换（读端永远看不到半截 JSON）；
5. 逐局校验：重算结果必须与刚写入的文件内容一致（回读验证）；
6. **不可重算的局跳过并计数**（只有 codes 无 moves 的老局判不出炸弹、红十等），
   不猜、不动，最后报告；
7. `--dry-run` 先看统计，确认无误再 `--apply`。

用法：
  python backfill_scores.py --dry-run          # 只报告，不改
  python backfill_scores.py --apply            # 备份 + 回填 + 校验
  python backfill_scores.py --apply --no-backup   # 已有备份时跳过备份（谨慎）
"""
from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import time
from pathlib import Path

BASE = Path("/home/ubuntu/paodekuai")
sys.path.insert(0, str(BASE))
import settle                                            # noqa: E402

DIRS = [BASE / "data" / "replays", BASE / "data" / "replays15"]
BACKUP_ROOT = BASE / "data" / "_backups"
INDEX = BASE / "data" / "_replay_index.json"


def md5(path: Path) -> str:
    return hashlib.md5(path.read_bytes()).hexdigest()


def backup_all(stamp: str) -> Path:
    """全量备份两个目录 + 索引，返回备份目录。"""
    dst = BACKUP_ROOT / stamp
    dst.mkdir(parents=True, exist_ok=True)
    for d in DIRS:
        if not d.exists():
            continue
        tgz = dst / (d.name + ".tar.gz")
        with tarfile.open(tgz, "w:gz") as tf:
            tf.add(d, arcname=d.name)
    manifest = []
    for d in DIRS:
        if d.exists():
            for p in sorted(d.glob("*.json")):
                manifest.append(f"{md5(p)}  {p.relative_to(BASE)}")
    (dst / "manifest.md5").write_text("\n".join(manifest) + "\n", encoding="utf-8")
    if INDEX.exists():
        shutil.copy2(INDEX, dst / "_replay_index.json")
    print(f"✅ 备份完成 → {dst}  （{len(manifest)} 个文件 + 索引）")
    return dst


def recalc(doc: dict) -> dict | None:
    """用现行口径重算一局的 result；不可重算返回 None。"""
    if doc.get("live") or doc.get("aborted") or doc.get("winner") is None:
        return None
    moves = doc.get("moves") or []
    hands = doc.get("hands") or doc.get("initialHands") or []
    if not moves or not hands or len(hands) != 2:
        return None
    try:
        h = [[int(x) for x in (hands[i] or [])] for i in (0, 1)]
        mv = [m for m in moves if isinstance(m, dict)]
        if len(mv) != len(moves):
            return None
        return settle.compute_result(h, mv, doc.get("opts") or {}, int(doc["winner"]))
    except Exception:
        return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-backup", action="store_true")
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 个（调试用）")
    a = ap.parse_args()
    if not (a.apply or a.dry_run):
        a.dry_run = True

    files = []
    for d in DIRS:
        if d.exists():
            files += sorted(d.glob("*.json"))
    if a.limit:
        files = files[: a.limit]
    print(f"扫描 {len(files)} 个棋谱文件（{'回填' if a.apply else '试算'}）")
    print(f"口径：炸弹各 +{settle.BOMB_SCORE} 分归持有者（不扣对方）；底分/关门/红十见 settle.py")
    print()

    changed = same = skip = 0
    changed_human = changed_ai = 0
    old_tot = [0, 0]
    new_tot = [0, 0]
    examples = []
    errors = []

    # ⚠ 回填前**必须**先全量备份（改 3500+ 存档，备份是唯一的回滚手段）
    bdir = None
    if a.apply and not a.no_backup:
        bdir = backup_all(time.strftime("backfill_%Y%m%d-%H%M%S"))

    for p in files:
        try:
            doc = json.loads(p.read_text(encoding="utf-8"))
        except Exception as e:
            errors.append((p.name, f"读失败 {e}"))
            continue
        res = recalc(doc)
        if res is None:
            skip += 1
            continue
        old = doc.get("result") or {}
        old_delta = old.get("delta") if isinstance(old, dict) else None
        new_delta = list(res["delta"])
        if isinstance(old_delta, (list, tuple)) and len(old_delta) == 2:
            old_tot[0] += int(old_delta[0]); old_tot[1] += int(old_delta[1])
            new_tot[0] += new_delta[0]; new_tot[1] += new_delta[1]
        else:
            old_tot[0] += 0; old_tot[1] += 0
            new_tot[0] += new_delta[0]; new_tot[1] += new_delta[1]

        if old_delta is not None and list(old_delta) == new_delta and old.get("base") == res.get("base") \
                and old.get("bombs") == res.get("bombs") and old.get("rem") == res.get("rem") \
                and old.get("shut") == res.get("shut") and old.get("redTxt") == res.get("redTxt"):
            same += 1
            continue
        changed += 1
        changed_human += new_delta[0] - (int(old_delta[0]) if isinstance(old_delta, (list, tuple)) and len(old_delta) == 2 else 0)
        changed_ai += new_delta[1] - (int(old_delta[1]) if isinstance(old_delta, (list, tuple)) and len(old_delta) == 2 else 0)
        if len(examples) < 12:
            examples.append((doc.get("no"), list(old_delta) if old_delta else None, new_delta,
                             res.get("bombs"), res.get("rem")))

        if a.apply:
            doc["result"] = res
            doc["scores"] = new_delta
            tmp = p.with_suffix(".json.tmp")
            try:
                tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
                os.replace(tmp, p)
                # 回读校验
                back = json.loads(p.read_text(encoding="utf-8"))
                if list((back.get("result") or {}).get("delta") or []) != new_delta:
                    errors.append((p.name, "回读校验不一致"))
            except Exception as e:
                errors.append((p.name, f"写失败 {e}"))

    print(f"■ {'已回填' if a.apply else '需回填'}：{changed} 局    无变化：{same}    跳过(不可重算)：{skip}")
    print()
    print("■ 分数变化合计（可重算的局）：")
    print(f"   人类：{old_tot[0]:+d} → {new_tot[0]:+d}   （差 {new_tot[0]-old_tot[0]:+d}）")
    print(f"   AI  ：{old_tot[1]:+d} → {new_tot[1]:+d}   （差 {new_tot[1]-old_tot[1]:+d}）")
    print(f"   ⚠ 新口径非零和，双方净值都会涨（炸弹分只加给持有者）")
    print()
    if examples:
        print("■ 样例（旧 → 新）：")
        for no, o, n, b, rem in examples:
            print(f"   {no or '-':8s} rem={rem} bombs={b}  {o} → {n}")
    if errors:
        print()
        print(f"✗ 错误 {len(errors)} 个：")
        for n, e in errors[:10]:
            print("   ", n, e)

    if a.apply:
        # 备份已在改写前完成（见循环前）；这里只做收尾：索引刷新 + 还原指引
        try:
            import urllib.request
            urllib.request.urlopen("http://127.0.0.1:8310/replays/list?scope=all&deck=all&limit=1", timeout=20).read()
            print("\n（已触发索引刷新）")
        except Exception:
            print("\n（索引刷新请求失败，可稍后手动访问记录页触发）")
        if bdir:
            print(f"■ 如需回滚： tar -xzf {bdir}/replays.tar.gz -C {BASE}/data "
                  f"&& tar -xzf {bdir}/replays15.tar.gz -C {BASE}/data")
    else:
        print("\n--dry-run：未做任何改动。确认无误后加 --apply 执行。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())