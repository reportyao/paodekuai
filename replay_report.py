#!/usr/bin/env python3
"""跑得快 对局复盘工具（人机局 + 在线真人局 + 人工点评）。

编号规则（唯一编号，快速定位）：
  A0001, A0002 …   人机局（data/replays，座位0=你，座位1=AI）
  H0001, H0002 …   在线真人局（data/online，房间制双人对打）
旧文件首次运行时自动补编号（按对局时间顺序），之后永久固定。

人工点评（给 AI 的学习信号，带标记）：
  data/comments/<编号>.json     按局存放（数组），每条带 source=human_review / kind / no / ply / text / ts
  data/human_reviews.jsonl      全量点评，一行一条（AI 可整份读取）

用法：
  python replay_report.py list                  # 时间倒序列出（编号/时间/类型/参与/手数/点评数/结果）
  python replay_report.py list H                # 只看真人局（A=只看人机局）
  python replay_report.py show H0003            # 按编号看完整复盘（每手牌面 + 人工点评）
  python replay_report.py show latest           # 最新一局
  python replay_report.py raw  H0003            # 该局原始 JSON（可直接喂给 AI）
  python replay_report.py comment H0003 "点评内容" [第几手]   # 记录人工点评（可针对某一手）
  python replay_report.py comments [编号]        # 查看人工点评（省略编号=全部）
  python replay_report.py stats                 # 汇总统计
  python replay_report.py export [out] [A|H]     # 合并 JSONL（自动附带 humanComments 与 human_reviews.jsonl）

牌 id 语义（与 pdk-ai 一致）：点数 = id>>2（0=3 … 11=A，12=2），花色 = id&3（0♠1♥2♣3♦）
牌型 ptype：0单 1对 2连对 3三张 4三带二 5三带一 6飞机 7顺子 8炸弹 9四带三
"""
from __future__ import annotations

import datetime
import json
import os
import threading
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
# 目录 -> 编号前缀：A=人机局（主站） H=真人局（在线房） E=外部 API 调用方
DIRS = [(BASE / "data" / "replays", "A"), (BASE / "data" / "online", "H"),
        (BASE / "data" / "external", "E")]
COMMENT_DIR = BASE / "data" / "comments"
COMMENT_JSONL = BASE / "data" / "human_reviews.jsonl"
RANK = "3456789XJQKA2"
SUIT = "♠♥♣♦"
PTYPE = {0: "单张", 1: "对子", 2: "连对", 3: "三张", 4: "三带二", 5: "三带一",
         6: "飞机", 7: "顺子", 8: "炸弹", 9: "四带三"}


# ---------------- 时间 ----------------

def _time_text_from(d: dict, fallback: float) -> str:
    ts = d.get("ts")
    if ts:
        try:
            dt = datetime.datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=datetime.timezone.utc).astimezone()
            return dt.strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            pass
    return datetime.datetime.fromtimestamp(fallback).strftime("%Y-%m-%d %H:%M:%S")


def _epoch(d: dict, fallback: float) -> float:
    ts = d.get("ts")
    if ts:
        try:
            return datetime.datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=datetime.timezone.utc).timestamp()
        except ValueError:
            pass
    tt = d.get("timeText")
    if tt:
        try:
            return datetime.datetime.strptime(tt, "%Y-%m-%d %H:%M:%S").timestamp()
        except ValueError:
            pass
    return fallback


def time_of(d: dict) -> str:
    t = d.get("timeText")
    if t:
        return t[:16]
    ts = d.get("ts")
    if ts:
        try:
            return datetime.datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=datetime.timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M")
        except ValueError:
            pass
    return datetime.datetime.fromtimestamp(d.get("_mtime", 0)).strftime("%Y-%m-%d %H:%M")


# ---------------- 编号 ----------------

def _files_with_dir():
    for d, prefix in DIRS:
        for p in d.glob("*.json"):
            yield p, prefix


def ensure_ids() -> None:
    """给缺失编号/时间的旧文件补上（按对局时间排序，一次性固定）。"""
    for d, prefix in DIRS:
        if not d.exists():
            continue
        entries = []
        for p in d.glob("*.json"):
            try:
                entries.append((p, json.loads(p.read_text(encoding="utf-8"))))
            except Exception:
                continue
        mx = 0
        for p, dd in entries:
            no = str(dd.get("no", ""))
            if no.startswith(prefix) and no[len(prefix):].isdigit():
                mx = max(mx, int(no[len(prefix):]))
        changed = []
        for p, dd in sorted(entries, key=lambda x: _epoch(x[1], x[0].stat().st_mtime)):
            need_no = not (str(dd.get("no", "")).startswith(prefix)
                           and str(dd.get("no", ""))[len(prefix):].isdigit())
            need_time = not dd.get("timeText")
            if need_no:
                mx += 1
                dd["no"] = f"{prefix}{mx:04d}"
            if need_time:
                dd["timeText"] = _time_text_from(dd, p.stat().st_mtime)
            if need_no or need_time:
                changed.append((p, dd))
        for p, dd in changed:
            ordered = {"no": dd.get("no", ""), "timeText": dd.get("timeText", "")}
            ordered.update({k: v for k, v in dd.items() if k not in ("no", "timeText")})
            try:
                p.write_text(json.dumps(ordered, ensure_ascii=False, indent=2), encoding="utf-8")
            except OSError:
                pass


def load_games(prefix_filter: str | None = None) -> list[dict]:
    ensure_ids()
    out = []
    for p, prefix in _files_with_dir():
        if prefix_filter and prefix != prefix_filter:
            continue
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        d["_file"] = p.name
        d["_path"] = str(p)
        d["_mtime"] = p.stat().st_mtime
        d["_epoch"] = _epoch(d, d["_mtime"])
        d["_prefix"] = prefix
        if prefix == "E":                     # data/external 目录里的都是外部调用方的对局
            d["api"] = True
            d.setdefault("caller", "API 调用方(未标注)")
        out.append(d)
    # 排序键必须确定：早期只用 ts（秒级），同一秒创建的两局会并列 -> 顺序随 glob 抖动，
    # 复盘"按编号找局"就会随机取到其中一局（线上重号 A1179 那次就取错了局）。
    return sorted(out, key=lambda d: (d["_epoch"], d["_mtime"], d["_file"]), reverse=True)


INDEX_PATH = BASE / "data" / "_replay_index.json"
_INDEX = {"rows": {}}
_INDEX_LOCK = threading.Lock()
_IDS_DONE = False

# 摘要里要保留的字段：够列表/统计用（不含逐手 moves，那才是慢的根源）
_SUMMARY_KEYS = ("no", "live", "aborted", "winner", "api", "caller", "source", "names",
                 "scores", "durationSec", "result", "timeText", "ts", "humanSeat",
                 "leader", "opts")


def _summary_of(d: dict, path, prefix: str, st) -> dict:
    row = {"file": path.name, "_path": str(path), "_prefix": prefix,
           "_mtime": st.st_mtime, "_size": st.st_size,
           "_epoch": _epoch(d, st.st_mtime),
           "sid": str(d.get("sid") or path.stem),
           # 手数以计数形式保存（摘要不保留逐手明细）
           "moves_n": len(d.get("moves") or d.get("codes") or [])}
    for k in _SUMMARY_KEYS:
        if k in d:
            row[k] = d[k]
    return row


def _load_index_file() -> dict:
    try:
        d = json.loads(INDEX_PATH.read_text(encoding="utf-8"))
        return d.get("rows") or {}
    except Exception:
        return {}


def _save_index(rows: dict) -> None:
    """原子写（先写临时文件再改名），避免读端拿到半截索引。"""
    try:
        INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = INDEX_PATH.with_name(INDEX_PATH.name + ".tmp")
        tmp.write_text(json.dumps({"v": 1, "rows": rows}, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, INDEX_PATH)
    except OSError:
        pass


_INDEX["rows"] = _load_index_file()


def load_summaries(refresh: bool = True) -> list[dict]:
    """轻量对局列表（新→旧）：只解析**新增/改动**的文件，其余直接用索引缓存。

    列表与统计只需要每局摘要（编号/时间/胜负/手数/点评数…）。以前每次都把 data/replays
    里 7MB 牌谱（含每一手的明细）全量解析一遍，列表打开要 260ms+ 且响应 360KB；
    现在首次建索引后按 mtime+size 增量更新，列表默认只返回最新 50 条。
    """
    global _IDS_DONE
    with _INDEX_LOCK:
        if refresh and not _IDS_DONE:
            ensure_ids()          # 旧文件补编号只需做一次（每请求都做=每次全量解析）
            _IDS_DONE = True
        rows = _INDEX["rows"]
        seen = set()
        for d, prefix in DIRS:
            if not d.exists():
                continue
            for p in d.glob("*.json"):
                try:
                    st = p.stat()
                except OSError:
                    continue
                key = f"{prefix}:{p.name}"
                seen.add(key)
                cur = rows.get(key)
                if cur and cur.get("_mtime") == st.st_mtime and cur.get("_size") == st.st_size:
                    continue
                try:
                    dd = json.loads(p.read_text(encoding="utf-8"))
                except Exception:
                    continue
                rows[key] = _summary_of(dd, p, prefix, st)
        for k in [k for k in rows if k not in seen]:
            rows.pop(k, None)
        if refresh:
            _save_index(rows)
    return sorted(rows.values(),
                  key=lambda r: (r.get("_epoch", 0), r.get("_mtime", 0), r.get("file", "")),
                  reverse=True)


def moves_count(g: dict) -> int:
    """摘要用 moves_n；完整对局用 moves/codes 的长度。"""
    if isinstance(g.get("moves_n"), int):
        return int(g["moves_n"])
    return len(g.get("moves") or g.get("codes") or [])


def find_by_id(gid: str, sid: str = "") -> dict | None:
    """按编号或文件名(sid)定位对局。

    sid 优先且精确：历史数据里存在"一个编号对应两局"（编号分配竞态遗留），
    此时只按编号找会选到其中一局（不保证是当前这局）——复盘就会和真实出牌对不上。
    前端在复盘"当局/上局"时会带上会话 sid，从而锁定唯一那一局。
    """
    gid = (gid or "").strip().upper()
    sid = (sid or "").strip().lower()
    games = load_games()
    if sid:
        for d in games:
            if str(d.get("_file", "")).lower() == sid + ".json" or                str(d.get("sid", "")).lower() == sid:
                return d
    for d in games:
        if str(d.get("no", "")).upper() == gid or d["_file"].upper() == gid:
            return d
    return None


# ---------------- 人工点评 ----------------

def comment_path(no: str) -> Path:
    return COMMENT_DIR / f"{no}.json"


def load_comments(no: str) -> list:
    p = comment_path(no)
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return []


def save_comment(no: str, text: str, ply=None, author: str = "人工",
                 kind: str = "human_comment") -> dict:
    """记录一条人工点评（按局存 + 全量追加），均带来源标记便于 AI 定位。"""
    COMMENT_DIR.mkdir(parents=True, exist_ok=True)
    rec = {
        "source": "human_review",     # 标记：人工复盘产物（AI 学习信号）
        "kind": kind,                 # human_comment / human_tag …
        "no": no,                     # 对局唯一编号（定位）
        "ply": ply,                   # 针对第几手；None=整局点评
        "text": str(text).strip(),
        "author": author,
        "ts": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    arr = load_comments(no)
    arr.append(rec)
    comment_path(no).write_text(json.dumps(arr, ensure_ascii=False, indent=2), encoding="utf-8")
    with COMMENT_JSONL.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return rec


def comments_index() -> dict:
    out = {}
    if not COMMENT_DIR.exists():
        return out
    for f in COMMENT_DIR.glob("*.json"):
        try:
            out[f.stem] = len(json.loads(f.read_text(encoding="utf-8")))
        except Exception:
            pass
    return out


def all_comments() -> list:
    out = []
    if COMMENT_DIR.exists():
        for f in COMMENT_DIR.glob("*.json"):
            out += load_comments(f.stem)
    return sorted(out, key=lambda c: c.get("ts", ""))


# ---------------- 格式化 ----------------

def card_str(cid: int) -> str:
    return SUIT[cid & 3] + RANK[cid >> 2]


def cards_str(ids) -> str:
    return " ".join(card_str(c) for c in ids) if ids else "（无）"


def combo_str(c) -> str:
    if not c:
        return ""
    return f"{PTYPE.get(c.get('ptype'), '?')}({RANK[c.get('main', 0)]})"


def code_str(code: int) -> str:
    if code == 0:
        return "过"
    parts = []
    for r in range(13):
        k = (code >> (3 * r)) & 7
        if k:
            parts.append(RANK[r] * k)
    return "+".join(parts)


def kind_of(d: dict) -> str:
    if d.get("_prefix") == "E" or d.get("api") or d.get("caller"):
        return "对外"
    return "真人" if d.get("source") == "online_room" else "人机"


def participants(d: dict) -> str:
    if d.get("api") or d.get("caller"):
        return f"{d.get('caller') or 'API 调用方'} vs AI"
    return "/".join(d.get("names", [])) if d.get("source") == "online_room" else "你 vs AI"


def result_of(d: dict) -> str:
    if d.get("live"):
        return "进行中"
    if d.get("aborted"):
        return "未完成（中断）"
    if d.get("source") == "online_room":
        r = d.get("result", {})
        names = d.get("names", ["甲", "乙"])
        w = names[r.get("winner", 0)] if r.get("winner", 0) < len(names) else "?"
        return f"{w} 胜 剩{r.get('rem')}张 分{r.get('delta')}"
    r = d.get("result") or {}
    if isinstance(r, dict) and r.get("delta") is not None:
        # 桥结算过的局（compute_result）：直接展示人类规则的本局净分
        who = ("调用方" if d.get("winner") == 0 else "AI") if (d.get("api") or d.get("caller"))             else ("你" if d.get("winner") == 0 else "AI")
        dur = d.get("durationSec")
        return (f"{who} 胜 剩{r.get('rem')}张 分{r.get('delta')}"
                + (f" 用时{dur}s" if dur else ""))
    if d.get("api") or d.get("caller"):
        who = "调用方" if d.get("winner") == 0 else "AI"
        dur = d.get("durationSec")
        return f"{who} 胜 分{d.get('scores')}" + (f" 用时{dur}s" if dur else "")
    return f"{'你' if d.get('winner') == 0 else 'AI'} 胜 分{d.get('scores')}"


# ---------------- 命令 ----------------

def stats_of(games: list) -> dict:
    """对局胜负统计（按传入的对局集合）。对外调用方按 caller 分组。"""
    def blank():
        return {"games": 0, "finished": 0, "live": 0, "aborted": 0,
                "seat0_wins": 0, "seat1_wins": 0, "scores": {},
                "moves": 0, "dur_sum": 0.0, "dur_n": 0}
    out = {"all": blank(), "by_caller": {}, "by_day": {}}
    for g in games:
        kind = "api" if (g.get("api") or g.get("caller")) else (
            "online" if g.get("source") == "online_room" else "human_vs_ai")
        for key in ("all", kind):
            b = out.setdefault(key, blank()) if key != "all" else out["all"]
            b["games"] += 1
            if g.get("live"):
                b["live"] += 1
                continue
            if g.get("aborted") or g.get("winner") is None:
                b["aborted"] += 1
                continue
            b["finished"] += 1
            b["seat0_wins" if g.get("winner") == 0 else "seat1_wins"] += 1
            b["moves"] += moves_count(g)
            if g.get("durationSec"):
                b["dur_sum"] += float(g["durationSec"])
                b["dur_n"] += 1
            sc = g.get("scores")
            if isinstance(sc, (list, tuple)) and len(sc) == 2:
                k = f"{sc[0]},{sc[1]}"
                b["scores"][k] = b["scores"].get(k, 0) + 1
        mv_n = len(g.get("moves") or g.get("codes") or [])
        if kind == "api":
            c = out["by_caller"].setdefault(g.get("caller") or "API 调用方", blank())
            c["games"] += 1
            if g.get("winner") is not None and not g.get("live"):
                c["finished"] += 1
                c["moves"] += mv_n
                c["seat0_wins" if g.get("winner") == 0 else "seat1_wins"] += 1
            if g.get("durationSec"):
                c["dur_sum"] += float(g["durationSec"])
                c["dur_n"] += 1
        day = str(g.get("timeText") or "")[:10]
        if day:
            d = out["by_day"].setdefault(day, blank())
            d["games"] += 1
            if g.get("winner") is not None and not g.get("live"):
                d["finished"] += 1
                d["moves"] += mv_n
                d["seat0_wins" if g.get("winner") == 0 else "seat1_wins"] += 1
    for b in [out["all"]] + list(out["by_caller"].values()) + list(out["by_day"].values())             + [v for k, v in out.items() if k not in ("all", "by_caller", "by_day")]:
        if not isinstance(b, dict):
            continue
        if b["finished"]:
            b["avg_moves"] = round(b["moves"] / b["finished"], 1)
            b["seat0_win_rate"] = round(b["seat0_wins"] / b["finished"], 3)
            b["avg_duration"] = round(b["dur_sum"] / b["dur_n"], 1) if b["dur_n"] else None
    return out


def external_games() -> list:
    """仅外部 API 调用方的对局（E 段）。"""
    return [g for g in load_games() if g.get("api") or g.get("caller")]


def cmd_list(prefix_filter=None):
    games = load_games(prefix_filter)
    if not games:
        print("暂无对局数据")
        return
    cidx = comments_index()
    print(f"{'编号':<7} {'时间':<17} {'类型':<5} {'房间/标识':<14} {'参与':<16} {'手数':>4} {'点评':>4}  结果")
    print("-" * 104)
    for d in games:
        tag = (f"{d['code']}-r{d['round']}" if d.get("source") == "online_room"
               else d.get("sid", d["_file"])[:10])
        moves = len(d.get("moves", d.get("codes", [])))
        note = cidx.get(str(d.get("no", "")), 0)
        print(f"{d.get('no', '?'):<7} {time_of(d):<17} {kind_of(d):<5} {tag:<14} "
              f"{participants(d):<16} {moves:>4} {(note or '-'):>4}  {result_of(d)}")


def cmd_show(gid: str):
    d = find_by_id(gid)
    if d is None:
        p = Path(gid)
        if p.exists():
            d = json.loads(p.read_text(encoding="utf-8"))
            d["_file"] = p.name
        else:
            print(f"找不到对局：{gid}（用 list 查看全部编号）")
            return
    names = d.get("names", ["human", "ai"])
    print(f"=== [{d.get('no', '?')}] {time_of(d)}  {kind_of(d)}局  {d['_file']} ===")
    if d.get("source") == "online_room":
        print(f"房号 {d['code']}  第 {d['round']}/{d['rounds']} 局  规则 {d['opts']}")
        print(f"双方: {names[0]}(座位0) vs {names[1]}(座位1)   先手: 座位{d['firstPlayer']}")
        print(f"扣底16张: {cards_str(d['kitty'])}")
        print(f"\n{names[0]} 初始16张: {cards_str(d['initialHands'][0])}")
        print(f"{names[1]} 初始16张: {cards_str(d['initialHands'][1])}")
    else:
        print(f"座位0=你  座位1=AI  模式 {d.get('mode')}  网络 {d.get('net')}  规则 {d['opts']}  先手 座位{d.get('leader')}")
        print(f"扣底16张: {cards_str(d.get('kitty', []))}")
        print(f"\n你 初始16张: {cards_str(d.get('hands', [[], []])[0])}")
        print(f"AI 初始16张: {cards_str(d.get('hands', [[], []])[1])}")
    print("\n--- 出牌序列 ---")
    if d.get("moves"):
        for m in d["moves"]:
            who = names[m["seat"]] if m["seat"] < len(names) else f"座位{m['seat']}"
            if m.get("pass"):
                extra = f"   被过:{m['pass_on']}" if m.get("pass_on") else ""
                print(f"{m['ply']:>3}. [{who}] 过{extra}")
            else:
                after = f"   剩{m['handAfter']}" if "handAfter" in m else ""
                print(f"{m['ply']:>3}. [{who}] {cards_str(m['cards'])}   {combo_str(m.get('combo'))}{after}")
    else:
        for i, code in enumerate(d.get("codes", []), 1):
            print(f"{i:>3}. 座位{i % 2}  {code_str(code)}")
    if d.get("source") == "online_room":
        r = d.get("result", {})
        print(f"\n结果: {names[r.get('winner', 0)]} 胜，对方剩 {r.get('rem')} 张，"
              f"本局 {r.get('delta')}，累计 {d.get('total')}")
    else:
        print(f"\n结果: {'你' if d.get('winner') == 0 else 'AI'} 胜，比分 {d.get('scores')}")
    cm = load_comments(str(d.get("no", "")))
    if cm:
        print(f"\n--- 人工点评（{len(cm)} 条，标记 source=human_review）---")
        for c in cm:
            ply = f"第{c['ply']}手" if c.get("ply") else "整局"
            print(f"  [{ply}] {c.get('ts', '')} {c.get('author', '')}: {c.get('text', '')}")


def cmd_comments(no=None):
    recs = load_comments(no) if no else all_comments()
    if not recs:
        print("暂无人工点评" + (f"（{no}）" if no else ""))
        return
    print(f"{'编号':<7} {'针对':<6} {'时间':<20} {'类型':<14} 内容")
    print("-" * 92)
    for c in recs:
        ply = f"第{c['ply']}手" if c.get("ply") else "整局"
        print(f"{c.get('no', '?'):<7} {ply:<6} {c.get('ts', ''):<20} {c.get('kind', ''):<14} {c.get('text', '')}")


def _fmt_block(b: dict) -> str:
    if not b["games"]:
        return "无"
    s = f"{b['games']} 局（完成 {b['finished']} / 进行中 {b['live']}）"
    if b["finished"]:
        s += (f"，座位0胜 {b['seat0_wins']} / 座位1胜 {b['seat1_wins']}"
              f"，平均 {b.get('avg_moves')} 手")
        if b.get("avg_duration"):
            s += f"，平均用时 {b['avg_duration']}s"
    return s


def cmd_stats(scope: str = "all"):
    """对局统计。scope: all | mine | api（api=外部调用方的 E 段对局）。"""
    games = load_games()
    if scope == "api":
        games = [g for g in games if g.get("api") or g.get("_prefix") == "E"]
    elif scope == "mine":
        games = [g for g in games if not (g.get("api") or g.get("_prefix") == "E")]
    st = stats_of(games)
    if scope == "api":
        print(f"对外 API 调用方对局：{_fmt_block(st['all'])}")
        for name, b in sorted(st["by_caller"].items(), key=lambda kv: -kv[1]["games"]):
            print(f"  · {name}：{_fmt_block(b)}")
        for day, b in sorted(st["by_day"].items())[-7:]:
            print(f"  {day}：{b['games']} 局（完成 {b['finished']}）")
        print("  （座位0=调用方，座位1=AI）")
        return
    games_all = load_games()
    ai = [g for g in games_all if not (g.get("api") or g.get("_prefix") == "E")
          and g.get("source") != "online_room"]
    on = [g for g in games_all if g.get("source") == "online_room"]
    ext = [g for g in games_all if g.get("api") or g.get("_prefix") == "E"]
    print(f"人机局 {len(ai)} 局", end="")
    if ai:
        win = sum(1 for g in ai if g.get("winner") == 0)
        print(f"（你胜 {win}/{len(ai)} = {win/len(ai):.0%}）", end="")
    print(f"\n真人局 {len(on)} 局")
    if on:
        print(f"  编号范围 {min(g['no'] for g in on)} ~ {max(g['no'] for g in on)}")
    ex = stats_of(ext)
    print(f"对外 API 局 {ex['all']['games']} 局", end="")
    if ex["all"]["games"]:
        print(f"（调用方胜 {ex['all']['seat0_wins']} / AI 胜 {ex['all']['seat1_wins']}）", end="")
        for name, b in sorted(ex["by_caller"].items(), key=lambda kv: -kv[1]["games"])[:5]:
            print("\n  · " + name + "：" + _fmt_block(b), end="")
    print()
    recs = all_comments()
    print(f"人工点评 {len(recs)} 条" + (f"（涉及 {len(set(c['no'] for c in recs))} 局）" if recs else ""))
    print("数据目录:")
    for d, prefix in DIRS:
        print(f"  [{prefix}] {d}")
    print(f"  [点评] {COMMENT_DIR}  +  {COMMENT_JSONL.name}")


def cmd_export(out_arg=None, prefix_filter=None):
    games = load_games(prefix_filter)
    out = Path(out_arg) if out_arg else None
    if out is not None and out.parent == Path("."):
        out = BASE / "data" / out.name
    if out is None:
        out = BASE / "data" / ("all_games.jsonl" if not prefix_filter else
                               ("ai_games.jsonl" if prefix_filter == "A" else "human_games.jsonl"))
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as fh:
        for d in games:
            dd = {k: v for k, v in d.items() if not k.startswith("_")}
            dd["_file"] = d["_file"]
            cm = load_comments(str(d.get("no", "")))
            if cm:
                dd["humanComments"] = cm       # 标记：人工复盘点评
            fh.write(json.dumps(dd, ensure_ascii=False) + "\n")
    print(f"已导出 {len(games)} 局 -> {out}")
    if games:
        print(f"  编号范围: {games[-1].get('no')} ~ {games[0].get('no')}")
    recs = all_comments()
    if recs:
        rp = out.parent / "human_reviews.jsonl"
        with rp.open("w", encoding="utf-8") as fh:
            for r in recs:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
        print(f"人工点评 {len(recs)} 条 -> {rp}")


def cmd_raw(gid: str):
    d = find_by_id(gid)
    if d is None:
        print(f"找不到对局：{gid}")
        return
    dd = {k: v for k, v in d.items() if not k.startswith("_")}
    cm = load_comments(str(d.get("no", "")))
    if cm:
        dd["humanComments"] = cm
    print(json.dumps(dd, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    args = sys.argv[1:]
    cmd = args[0] if args else "list"
    rest = args[1:]
    pf = rest[0] if (cmd == "list" and rest and rest[0] in ("A", "H")) else None
    if cmd == "list":
        cmd_list(pf)
    elif cmd == "stats":
        cmd_stats(rest[0] if rest and rest[0] in ("all", "mine", "api") else "all")
    elif cmd == "show" and rest:
        cmd_show("latest" if rest[0] == "latest" else rest[0])
    elif cmd == "raw" and rest:
        cmd_raw(rest[0])
    elif cmd == "comments":
        cmd_comments(rest[0] if rest else None)
    elif cmd == "comment" and len(rest) >= 2:
        ply = int(rest[2]) if len(rest) > 2 and rest[2].isdigit() else None
        r = save_comment(rest[0].upper(), rest[1], ply)
        print(f"已记录点评 -> {r['no']} {('第%d手' % r['ply']) if r['ply'] else '整局'}：{r['text']}")
    elif cmd == "export":
        out_arg = rest[0] if rest else None
        pf_arg = rest[1] if len(rest) > 1 else None
        if out_arg in ("A", "H"):
            pf_arg, out_arg = out_arg, None
        cmd_export(out_arg, pf_arg)
    elif cmd == "latest":
        g = load_games()
        cmd_show(g[0]["no"]) if g else print("暂无对局数据")
    else:
        print(__doc__)
