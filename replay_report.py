#!/usr/bin/env python3
"""跑得快 对局复盘读取工具（人机局 + 在线真人局）。

用法：
  python replay_report.py list            # 列出所有对局（时间/来源/比分）
  python replay_report.py stats           # 汇总统计
  python replay_report.py show <file>     # 打印某局完整复盘（含每手牌面）
  python replay_report.py latest          # 打印最新一局
  python replay_report.py export [out]    # 全部对局合并为一个 JSONL（默认 data/all_games.jsonl）

数据目录（相对本文件）：
  data/replays/*.json   人机对局：你在人机模式打的每一局（座位0=你，座位1=AI）
  data/online/*.json    在线真人局：房间制双人对打（两名真人）

牌 id 语义（与 pdk-ai 一致）：点数 = id>>2（0=3 … 11=A，12=2），花色 = id&3（0♠1♥2♣3♦）
牌型 ptype：0单 1对 2连对 3三张 4三带二 5三带一 6飞机 7顺子 8炸弹 9四带三
"""
from __future__ import annotations

import datetime
import glob
import json
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
DIRS = [BASE / "data" / "replays", BASE / "data" / "online"]
RANK = "3456789XJQKA2"
SUIT = "♠♥♣♦"
PTYPE = {0: "单张", 1: "对子", 2: "连对", 3: "三张", 4: "三带二", 5: "三带一",
         6: "飞机", 7: "顺子", 8: "炸弹", 9: "四带三"}


def card_str(cid: int) -> str:
    return SUIT[cid & 3] + RANK[cid >> 2]


def cards_str(ids) -> str:
    return " ".join(card_str(c) for c in ids) if ids else "（无）"


def combo_str(c) -> str:
    if not c:
        return ""
    return f"{PTYPE.get(c.get('ptype'), '?')}({RANK[c.get('main', 0)]})"


def code_str(code: int) -> str:
    """pdk 动作码 -> 点数文本（旧格式兼容）。"""
    if code == 0:
        return "过"
    parts = []
    for r in range(13):
        k = (code >> (3 * r)) & 7
        if k:
            parts.append(RANK[r] * k)
    return "+".join(parts)


def find_files() -> list[Path]:
    files = []
    for d in DIRS:
        files += [Path(p) for p in glob.glob(str(d / "*.json"))]
    return sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)


def load(p: Path) -> dict:
    return json.loads(p.read_text(encoding="utf-8"))


def cmd_list():
    files = find_files()
    if not files:
        print("暂无对局数据")
        return
    print(f"{'时间':<17} {'来源':<9} {'标识':<14} {'参与':<16} {'手数':>4}  结果")
    for p in files:
        try:
            d = load(p)
        except Exception:
            continue
        ts = datetime.datetime.fromtimestamp(p.stat().st_mtime).strftime("%m-%d %H:%M:%S")
        if d.get("source") == "online_room":
            src, tag = "在线真人", f"{d['code']}-r{d['round']}"
            who = "/".join(d.get("names", []))
            r = d.get("result", {})
            res = f"{d.get('names', ['甲','乙'])[r.get('winner', 0)]} 胜 剩{r.get('rem')}张 分{r.get('delta')}"
        else:
            src, tag = "人机", p.stem[:10]
            who = "你 vs AI"
            res = f"{'你' if d.get('winner') == 0 else 'AI'} 胜 分{d.get('scores')}"
        print(f"{ts:<17} {src:<9} {tag:<14} {who:<16} {len(d.get('moves', d.get('codes', []))):>4}  {res}")


def cmd_show(path: Path):
    d = load(path)
    print(f"=== 复盘 {path.name}  (version {d.get('version')}) ===")
    if d.get("source") == "online_room":
        print(f"在线真人局  房号 {d['code']}  第 {d['round']}/{d['rounds']} 局")
        print(f"双方: {d['names'][0]}(座位0) vs {d['names'][1]}(座位1)   先手: 座位{d['firstPlayer']}")
        print(f"规则: {d['opts']}")
        print(f"扣底16张: {cards_str(d['kitty'])}")
        print(f"\n{d['names'][0]} 初始: {cards_str(d['initialHands'][0])}")
        print(f"{d['names'][1]} 初始: {cards_str(d['initialHands'][1])}")
        names = d["names"]
    else:
        print(f"人机局  座位0=你  座位1=AI  模式 {d.get('mode')}  网络 {d.get('net')}")
        print(f"规则: {d['opts']}   先手: 座位{d['leader']}")
        print(f"扣底16张: {cards_str(d['kitty'])}")
        print(f"\n你 初始: {cards_str(d['hands'][0])}")
        print(f"AI 初始: {cards_str(d['hands'][1])}")
        names = d.get("names", ["human", "ai"])
    print("\n--- 出牌序列 ---")
    if d.get("moves"):
        for m in d["moves"]:
            who = names[m["seat"]] if m["seat"] < len(names) else f"座位{m['seat']}"
            if m.get("pass"):
                extra = f"  被过:{m['pass_on']}" if m.get("pass_on") else ""
                print(f"{m['ply']:>3}. [{who}] 过{extra}")
            else:
                combo = combo_str(m.get("combo"))
                after = f"  剩{m['handAfter']}" if "handAfter" in m else ""
                print(f"{m['ply']:>3}. [{who}] {cards_str(m['cards'])}  {combo}{after}")
    else:
        for i, code in enumerate(d.get("codes", []), 1):
            print(f"{i:>3}. 座位{i % 2}  {code_str(code)}")
    r = d.get("result", {})
    if d.get("source") == "online_room":
        print(f"\n结果: {names[r.get('winner', 0)]} 胜，对方剩 {r.get('rem')} 张，本局 {r.get('delta')}，累计 {d.get('total')}")
    else:
        print(f"\n结果: {'你' if d.get('winner') == 0 else 'AI'} 胜，比分 {d.get('scores')}")


def cmd_stats():
    files = find_files()
    ai = [p for p in files if load(p).get("source") != "online_room"]
    on = [p for p in files if load(p).get("source") == "online_room"]
    print(f"人机局: {len(ai)} 局")
    if ai:
        win = sum(1 for p in ai if load(p).get("winner") == 0)
        print(f"  你(座位0)胜率 {win}/{len(ai)} ({win/len(ai):.0%})")
    print(f"在线真人局: {len(on)} 局")
    print(f"数据目录:\n  {DIRS[0]}\n  {DIRS[1]}")


def cmd_export(out_arg: str | None):
    files = find_files()
    out = Path(out_arg) if out_arg else (BASE / "data" / "all_games.jsonl")
    out.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with out.open("w", encoding="utf-8") as fh:
        for p in files:                      # 按时间倒序（新->旧）
            d = load(p)
            d["_file"] = p.name
            fh.write(json.dumps(d, ensure_ascii=False) + "\n")
            n += 1
    print(f"已导出 {n} 局 -> {out}")
    print("（每行一局完整 JSON，可直接喂给 AI 阅读/分析）")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "list"
    if cmd == "list":
        cmd_list()
    elif cmd == "stats":
        cmd_stats()
    elif cmd == "show" and len(sys.argv) > 2:
        cmd_show(Path(sys.argv[2]))
    elif cmd == "latest":
        fs = find_files()
        cmd_show(fs[0]) if fs else print("暂无对局数据")
    elif cmd == "export":
        cmd_export(sys.argv[2] if len(sys.argv) > 2 else None)
    else:
        print(__doc__)
