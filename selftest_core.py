# -*- coding: utf-8 -*-
"""进程内自测：决策内核与上游 _decide 等价性、/api/play 断线续打语义、引擎计分与网页版一致。

用法（在 paodekuai 目录下）：python selftest_core.py
需要 pdk-ai-prod2（或 --bot-root 指向的 pdk-ai 副本）已同步到线上版本。
"""
import glob
import json
import os
import random
import sys

BOT_ROOT = os.environ.get(
    "PDK_BOT_ROOT",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "pdk-ai-prod2"))
sys.argv = ["x", "--bot-root", BOT_ROOT]
print(f"[selftest_core] bot-root = {BOT_ROOT}")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ai_bridge as B                                                   # noqa: E402
from pdk.engine import Game, counts_of_ids                              # noqa: E402

OK, BAD = [], []


def check(name, cond, detail=""):
    (OK if cond else BAD).append(name)
    print(f"  {'✅' if cond else '❌'} {name}{'' if cond else '  << ' + str(detail)[:260]}")


print("=== 1) _decide_core 与上游 _decide 等价性（同 cfg）===")
REPLAYS = os.environ.get("PDK_REPLAYS", "data/replays")
reps = []
for f in sorted(glob.glob(os.path.join(REPLAYS, "*.json"))):
    try:
        d = json.load(open(f, encoding="utf-8"))
    except Exception:
        continue
    if len(d.get("codes") or []) >= 8 and len(d.get("hands") or []) == 2:
        reps.append(d)
reps = reps[:8]
print(f"  样本：{len(reps)} 局真实牌谱")
cmp_moves, cmp_legal, paths = 0, 0, {}
for d in reps:
    detail, states, cfg = B._replay_positions(d["hands"], d["leader"], d.get("opts") or {}, d["codes"])
    for ply in random.Random(1).sample(range(1, len(detail) + 1), min(6, len(detail))):
        st = states[ply - 1]
        seat = int(detail[ply - 1]["seat"])
        my_hand = list(st["hand_ids"])
        payload = {
            "my_hand": my_hand,
            "opp_n": int(st["n"][1 - seat]),
            "trick": st["trick"],
            "history": [{"seat": 0 if x["seat"] == seat else 1, "move": list(x["cards"]),
                         "pass_on": x["pass_on"]} for x in detail[:ply - 1]],
            "explain": True,
        }
        mine = B._decide_core(dict(payload), B.bot_server.CFG, "c")
        theirs = B.bot_server._decide(dict(payload))
        cmp_legal += 1
        if mine["legal_count"] != theirs["legal_count"]:
            check("legal_count 一致", False, (ply, mine["legal_count"], theirs["legal_count"]))
            break
        p_mine = (mine.get("explain") or {}).get("path")
        p_theirs = (theirs.get("explain") or {}).get("path")
        paths[p_mine] = paths.get(p_mine, 0) + 1
        if mine["move"] == theirs["move"]:
            cmp_moves += 1
        elif p_mine in ("forced", "one_shot", "fallback_net", "endgame_order", "report_dump") \
                and p_theirs == p_mine:
            check("确定性路径下选择一致", False, (ply, p_mine, mine["move"], theirs["move"]))
check("legal_count 全部一致（局面重建正确）", cmp_legal > 0, cmp_legal)
print(f"  决策路径分布: {paths}")
print(f"  选择完全一致的样本: {cmp_moves}/{cmp_legal}（残局搜索带随机世界采样，存在合理波动）")
check("一致率 >= 60%", cmp_moves >= 0.6 * cmp_legal, f"{cmp_moves}/{cmp_legal}")

print("=== 2) /api/play 断线续打：AI 应手中断后原样重发 ===")
snap, st = B.do_new_game({"opts": {"red10": True}})      # 返回 (body, code)
check("new_game 200", st == 200, snap)
gid = snap["gid"]
s = B.get_sess(gid)
with s.lock:
    legal = s.legal_ids(0)
    cand = [c for c in legal if len(c) == 1] or [c for c in legal if c] or legal
    move = min(cand, key=lambda c: (len(c), c[0] if c else -1))
    mine_n_before = int(s.game.hand_count(0))
    code = B.fast.cards_to_code(move) if move else B.PASS_CODE
    s._apply(code)                                   # 只落子，不推进 AI（模拟应手中断）
    turn_after = int(s.cg.g.turn)
    n_after = int(s.game.hand_count(0))
check("模拟中断：轮次已到 AI", turn_after == s.ai_seat, turn_after)
check("模拟中断：我的手牌已减少", n_after == mine_n_before - len(move), (mine_n_before, n_after))
snap2, st2 = B.do_play({"gid": gid, "cards": move})      # 原样重发同一请求
check("重发返回 200", st2 == 200, snap2)
check("重发未重复落子（手牌不再减少）", snap2.get("my_n") == n_after, (n_after, snap2.get("my_n")))
check("重发后已轮到调用方或终局",
      snap2.get("turn") == 0 or snap2.get("finished"), (snap2.get("turn"), snap2.get("finished")))
check("重发后 AI 至少应了一手（moves >= 2）", snap2.get("moves", 0) >= 2, snap2.get("moves"))

print("=== 3) 引擎计分与网页版记录一致（scores 字段可信度）===")
same, diff = 0, []
for d in reps:
    cfg = B.build_cfg(d.get("opts") or {})
    k = sorted(d.get("kitty") or [])
    g = Game(cfg=cfg, first_player=int(d.get("leader", 0)),
             hands=[sorted(d["hands"][0]), sorted(d["hands"][1])], kitty=k)
    for code in d["codes"]:
        try:
            g.play(code)                          # 与桥接一致：旧局里的"过牌宽容"要照走
        except Exception:
            g._play_unchecked(code)
    if not g.finished:
        continue
    want = tuple(d.get("scores") or ())
    got = tuple(g.scores or ())
    if want == got:
        same += 1
    else:
        diff.append((d.get("no"), want, got))
check("完局计分与网页版一致", not diff, diff[:4])
print(f"  可比对完局: {same} 局一致 / {len(diff)} 局不一致")

print("=== 4) 会话/文件标识防穿越 ===")
for bad_val, tag in [("../../evil", "sid"), ("..\\..\\evil", "sid"), ("a" * 200, "sid"), ("", "sid")]:
    try:
        B.check_ident(bad_val, "sid")
        check(f"拒绝非法 {tag}={bad_val[:14]!r}", False, "未拦截")
    except B.ApiError:
        check(f"拒绝非法 {tag}={bad_val[:14]!r}", True)
for bad_file in ["../../evil.json", "a/../b.json", "x" * 100, ".hidden"]:
    try:
        B.check_file(bad_file)
        check(f"拒绝非法 file={bad_file[:16]!r}", False, "未拦截")
    except B.ApiError:
        check(f"拒绝非法 file={bad_file[:16]!r}", True)
check("合法 sid 通过", B.check_ident("abcd1234efgh") == "abcd1234efgh")
check("合法 file 通过", B.check_file("abcd1234.json") == "abcd1234.json")
check("合法编号通过", B.check_no("A0001") == "A0001")

print(f"\n=== 汇总：通过 {len(OK)} / 失败 {len(BAD)} ===")
for b in BAD:
    print("  ❌", b)
if not BAD:
    print("  全部通过 ✅")
