#!/usr/bin/env python3
"""清理复盘里的"未成局/未完局"记录（在服务器上执行，默认 dry-run）。

判定（满足任一即视为未成局/未完局）：
  - moves == 0                  ：一局都没打过（空局）
  - live == true                ：仍标记进行中（含中断残留）
  - not live and winner is None ：从未真正分出胜负（多为人中途退出）
保护：mtime 在 30 分钟内的文件一律跳过（可能正在对局）。
动作：移动到 data/_trash_<时间戳>/（不物理删除，随时可回滚）。

用法：python clean_replays.py --apply     # 真正执行；不带 --apply 只统计
"""
import collections
import json
import os
import shutil
import sys
import time
from pathlib import Path

BASE = Path("/home/ubuntu/paodekuai")
DIRS = [BASE / "data" / "replays", BASE / "data" / "external"]
APPLY = "--apply" in sys.argv
GUARD_SEC = 30 * 60


def main():
    now = time.time()
    trash = BASE / "data" / ("_trash_" + time.strftime("%Y%m%d-%H%M%S"))
    picked, skipped_recent, kept = [], [], 0
    reasons = collections.Counter()
    for d in DIRS:
        if not d.exists():
            continue
        for f in sorted(d.glob("*.json")):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                continue
            mv = len(data.get("moves") or data.get("codes") or [])
            live = bool(data.get("live"))
            winner = data.get("winner")
            if now - f.stat().st_mtime < GUARD_SEC:
                skipped_recent.append(f)
                continue
            why = None
            if mv == 0:
                why = "空局(0手)"
            elif live:
                why = "仍未完成(标记进行中)"
            elif winner is None:
                why = "无胜负(中途退出)"
            if why:
                picked.append((f, why, mv))
                reasons[why] += 1
            else:
                kept += 1
    print(f"扫描完成：待清理 {len(picked)} 个，保留 {kept} 个，跳过(30 分钟内活动) {len(skipped_recent)} 个")
    for k, v in reasons.most_common():
        print(f"   {k:20} {v:5}")
    if not APPLY:
        print("\n[dry-run] 未做任何改动；加 --apply 执行（会先移动到回收目录）")
        return
    trash.mkdir(parents=True, exist_ok=True)
    moved = 0
    for f, why, mv in picked:
        shutil.move(str(f), str(trash / f.name))
        moved += 1
    (trash / "README.txt").write_text(
        "以下记录被判定为未成局/未完局并移入此处（未物理删除）：\n"
        + "\n".join(f"{f.name}\t{why}\t{mv}手" for f, why, mv in picked)
        + "\n如需恢复：把文件搬回 data/replays/ 或 data/external/ 即可。\n", encoding="utf-8")
    print(f"\n已移动 {moved} 个文件到 {trash}")
    print(f"回收目录占用：{sum(x.stat().st_size for x in trash.glob('*.json')) / 1024:.0f} KB")


if __name__ == "__main__":
    main()
