# -*- coding: utf-8 -*-
import os

"""HTTP 自测：整局对局 API + 无状态接口 + 负例 + 既有网页版接口回归。

用法：先启动桥接（python ai_bridge.py --bot-root ../pdk-ai-prod2），再 python selftest_api.py
默认打 http://127.0.0.1:8766（文件顶部 B 可改）。
"""
import json
import time
import urllib.error
import urllib.request

PUBLIC_ONLY = os.environ.get("PDK_PUBLIC_ONLY") == "1"   # 1=只测对外实例（跳过 /init /action /act 等网页专有接口）
B = os.environ.get("PDK_API_BASE", "http://127.0.0.1:8766").rstrip("/")
if not B.endswith("/ai") and "127.0.0.1:8310" in B:
    B += "/ai"
OK = []
BAD = []


def call(path, obj=None, timeout=120, raw=None):
    data = raw if raw is not None else (json.dumps(obj).encode() if obj is not None else None)
    req = urllib.request.Request(B + path, data=data,
                                 headers={"Content-Type": "application/json"},
                                 method="POST" if data is not None else "GET")
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"{}"), time.time() - t0
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}"), time.time() - t0
        except Exception:
            return e.code, {}, time.time() - t0


def check(name, cond, detail=""):
    (OK if cond else BAD).append(name)
    print(f"  {'✅' if cond else '❌'} {name}{'' if cond else '  << ' + str(detail)[:220]}")


for _ in range(60):                       # 等跨重启会话恢复结束（恢复期间 get_sess 会返回 503）
    if not call("/health")[1].get("restoring"):
        break
    time.sleep(2)

print("=== 1) 整局对局 API：开新局 ===")
st, g, dt = call("/api/new_game", {"opts": {"red10": True}, "mode": "hybrid"})
check("new_game 200", st == 200, g)
gid = g.get("gid", "")
check("gid 为 24 位十六进制", len(gid) == 24 and all(c in "0123456789abcdef" for c in gid), gid)
check("you_are=0 / ai_seat=1", g.get("you_are") == 0 and g.get("ai_seat") == 1, g.get("you_are"))
check("返回时轮到调用方", g.get("turn") == 0, g.get("turn"))
check("my_hand 16 张", len(g.get("my_hand") or []) == 16, len(g.get("my_hand") or []))
check("张数自洽（我方 16 张、对手 <=16、均在 0..16）",
      g.get("my_n") == 16 and 0 <= (g.get("opp_n") or 0) <= 16,
      (g.get("my_n"), g.get("opp_n")))
check("轮到调用方且 legal 非空", len(g.get("legal") or []) > 0, len(g.get("legal") or []))
L = g.get("legal") or []
check("legal_pass 与 legal 一致（含过牌当且仅当无牌可压）",
      g.get("legal_pass") == any(len(x) == 0 for x in L),
      (g.get("legal_pass"), len(L)))
check("有牌可压时 legal_pass 必为 false",
      (not L) or g.get("legal_pass") is False or any(len(x) == 0 for x in L),
      (len(L), g.get("legal_pass")))
check("opts 回显", (g.get("opts") or {}).get("red10") is True, g.get("opts"))
check("含 new 标记", g.get("new") is True, g)

print("=== 2) 整局对局 API：打完整局（AI 自动应手）===")
plies, plays, errors = 0, 0, []
t_start = time.time()
for _ in range(120):
    if g.get("finished"):
        break
    if g.get("turn") != 0:
        errors.append(f"turn={g.get('turn')}（应为调用方 0）")
        break
    legal = g.get("legal") or []
    if not legal:
        errors.append("轮到调用方却无合法手")
        break
    # 挑一手：优先单张里最小的，否则第一手
    singles = [c for c in legal if len(c) == 1]
    move = min(singles, key=lambda c: c[0]) if singles else legal[0]
    prev_moves = g.get("moves", 0)
    st, g2, dt = call("/api/play", {"gid": gid, "cards": move})
    if st != 200:
        errors.append(f"play 失败 HTTP {st} {str(g2)[:120]}")
        break
    plays += 1
    if g2.get("moves", 0) < prev_moves + 1:
        errors.append(f"手数未增长 {prev_moves} -> {g2.get('moves')}")
        break
    if g2.get("turn") != 0 and not g2.get("finished"):
        errors.append(f"AI 未应手完（turn={g2.get('turn')}）")
        break
    if g2.get("finished") and g2.get("winner") not in (0, 1):
        errors.append(f"终局但 winner={g2.get('winner')}")
        break
    # 终局校验：赢家手牌应为 0
    if g2.get("finished"):
        cnt = {0: g2.get("my_n"), 1: g2.get("opp_n")}
        if cnt.get(g2["winner"]) != 0:
            errors.append(f"赢家剩牌 {cnt.get(g2['winner'])}")
    g = g2
check("整局无错完成", not errors, errors)
check("整局已结束且有赢家", bool(g.get("finished")) and g.get("winner") in (0, 1),
      (g.get("finished"), g.get("winner")))
check("比分零和", sum(g.get("scores") or [0, 0]) == 0, g.get("scores"))
print(f"     手数={g.get('moves')} 调用方出牌={plays} 用时={time.time()-t_start:.1f}s "
      f"比分={g.get('scores')} 剩牌={g.get('my_n')}/{g.get('opp_n')}")

print("=== 3) /api/state 只读一致 ===")
st, s1, _ = call(f"/api/state?gid={gid}")
check("state 200 且局面一致",
      st == 200 and s1.get("moves") == g.get("moves") and s1.get("finished") == g.get("finished"), st)
st, s2, _ = call(f"/api/state?gid={gid}")
check("连续两次 state 不推进对局", s2.get("moves") == s1.get("moves"), (s1.get("moves"), s2.get("moves")))

print("=== 4) /api/play 幂等/边界 ===")
st, r, _ = call("/api/play", {"gid": gid, "cards": []})
check("终局后再 play 仍 200 且 finished", st == 200 and r.get("finished") is True, (st, r.get("finished")))
st, r, _ = call("/api/play", {"gid": "x" * 24, "cards": []})
check("未知 gid -> 404", st == 404, (st, r))
st, r, _ = call("/api/play", {"gid": "../../etc/passwd", "cards": []})
check("非法 gid（路径穿越）-> 400", st == 400, (st, r))

print("=== 5) 新局 + suggest（含解释）===")
st, g3, _ = call("/api/new_game", {})
gid3 = g3.get("gid")
st, sg, dt = call("/api/suggest", {"gid": gid3, "explain": True})
check("suggest 200", st == 200, (st, sg))
cards = sg.get("cards") or []
check("suggest 结果合法（含合法过牌的情况）", cards in (g3.get("legal") or []),
      (cards, len(g3.get("legal") or [])))
check("suggest 出牌是手牌子集",
      (not cards) or set(cards) <= set(g3.get("my_hand") or []), cards)
ex = sg.get("explain") or {}
check("suggest 带解释（path/text）", bool(ex.get("path")) and bool(ex.get("text")), ex.get("path"))
check("suggest 不落子（手数不变）", call(f"/api/state?gid={gid3}")[1].get("moves") == g3.get("moves"))
st, bad, _ = call("/api/play", {"gid": gid3, "cards": [99]})
check("非法牌 id -> 400", st == 400, (st, bad))
st, bad, _ = call("/api/play", {"gid": gid3, "cards": "abc"})
check("cards 非数组 -> 400", st == 400, (st, bad))
st, bad, _ = call("/api/suggest", {"gid": gid3, "explain": True})
st, stt, _ = call(f"/api/state?gid={gid3}")
check("suggest 后局面未变", stt.get("moves") == g3.get("moves"), (stt.get("moves"), g3.get("moves")))

print("=== 6) 无状态接口负例 ===")
st, r, _ = call("/api/decide", {"my_hand": [0], "opp_n": 16, "trick": None, "history": [{}] * 600})
check("history 超长 -> 400", st == 400, (st, r))
st, r, _ = call("/api/decide", {"my_hand": [0] * 30, "opp_n": 16, "trick": None, "history": []})
check("my_hand 过多 -> 400", st == 400, (st, r))
st, r, _ = call("/api/decide", {"my_hand": [], "opp_n": 16, "trick": None, "history": []})
check("my_hand 空 -> 400", st == 400, (st, r))
st, r, _ = call("/api/decide", {"my_hand": [0], "opp_n": 99, "trick": None, "history": []})
check("opp_n 越界 -> 400", st == 400, (st, r))
st, r, _ = call("/api/decide", {"my_hand": [0], "opp_n": 16, "trick": [1, 2], "history": []})
check("trick 形状错 -> 400", st == 400, (st, r))
st, r, _ = call("/api/decode", {"initial_hands": [[0] * 15, [1] * 16], "first_player": 0, "moves": []})
check("初始手牌 15 张 -> 400", st == 400, (st, r))
st, r, _ = call("/api/decode", {"initial_hands": [[0] * 16, [0] * 16], "first_player": 0, "moves": []})
check("初始手牌重复 -> 400", st == 400, (st, r))
st, r, _ = call("/api/decode", {"initial_hands": [[0] * 16, [1] * 16], "first_player": 0,
                                "moves": [1, 1, 1]})
check("牌谱与手牌不一致 -> 4xx", st in (400, 500), (st, str(r)[:100]))
st, r, _ = call("/api/analyze", {"initial_hands": [[0] * 16, [1] * 16], "first_player": 0,
                                 "moves": [], "ply": 5})
check("ply 越界 -> 400", st == 400, (st, r))

print("=== 7) 请求体与路由 ===")
st, r, _ = call("/api/decide", None, raw=b"{" + b'"my_hand":[],' * 90000 + b'"x":1}')
check("超大请求体 -> 413", st == 413, st)
st, r, _ = call("/api/decide", None, raw=b"{not json}")
check("非 JSON 请求体 -> 400", st == 400, (st, str(r)[:80]))
st, r, _ = call("/api/nope", {})
check("未知接口 -> 404", st == 404, st)
st, r, _ = call(f"/api/state")
check("state 缺 gid -> 400", st == 400, (st, r))

if not PUBLIC_ONLY:                          # 对外实例只开无状态/整局接口，跳过网页专有段落
    print("=== 8) 网页版既有接口回归（仅网页实例）===")
    import random
    DECK48 = list(range(44)) + [44, 45, 46, 48]          # pdk 原生牌库（不含 47）
    ids = list(DECK48)
    random.Random(7).shuffle(ids)
    kitty, h0, h1 = ids[:16], sorted(ids[16:32]), sorted(ids[32:48])
    st, init, _ = call("/init", {"hands": [h0, h1], "kitty": kitty, "leader": 0,
                                 "opts": {"red10": True}, "mode": "hybrid"})
    check("init 200", st == 200, init)
    sid = init.get("sid")
    st, lg, _ = call(f"/legal?sid={sid}&seat=0")
    check("legal 出牌方有手", st == 200 and len(lg.get("legal") or []) > 0, st)
    st, lg2, _ = call(f"/legal?sid={sid}&seat=1")
    check("legal 非出牌方返回空", st == 200 and (lg2.get("legal") or []) == [], (st, lg2.get("legal")))
    st, r, _ = call(f"/legal?sid={sid}")
    check("legal 缺 seat 参数仍可用", st == 200, st)
    st, r, _ = call("/legal")
    check("legal 缺 sid -> 400", st == 400, (st, r))
    st, act, _ = call("/act", {"sid": sid})
    check("act 非 AI 回合 -> 400", st == 400, (st, act))
    mv = min([c for c in lg["legal"] if len(c) == 1] or lg["legal"], key=lambda c: (len(c), c[0]))
    st, r, _ = call("/action", {"sid": sid, "seat": 0, "cards": mv})
    check("action 200", st == 200, r)
    st, r, _ = call("/action", {"sid": sid, "seat": 0, "cards": "xx"})
    check("action cards 非法 -> 400", st == 400, (st, r))
    st, r, _ = call("/action", {"sid": sid, "seat": 9, "cards": []})
    check("action seat 非法 -> 400", st == 400, (st, r))
    st, act, _ = call("/act", {"sid": sid})
    check("act 200（AI 出牌）", st == 200 and act.get("cards"), act)
    _probe = call(f"/legal?sid={sid}&seat=0")[1]
    codes_played = []                                     # 会话真实动作码（给 decode 用）
    for _m in [mv] + ([act.get("cards")] if act.get("cards") else []):
        codes_played.append(1 if False else None)
    # 从会话重放文件读真实 codes（最可靠）
    import glob as _glob, os as _os
    _f = sorted(_glob.glob("data/replays/*.json"), key=_os.path.getmtime, reverse=True)
    codes_played = []
    for _p in _f[:5]:
        _d = json.load(open(_p, encoding="utf-8"))
        if _d.get("sid") == sid:
            codes_played = list(_d.get("codes") or [])
            break
    check("拿到会话真实动作码", len(codes_played) >= 1, codes_played)
    st, ex, dt = call("/api/explain", {"sid": sid, "ply": 2})
    check("explain 会话版（缓存瞬时）", st == 200 and ex.get("text"), (st, str(ex)[:120]))
    st, an, dt = call("/api/analyze", {"sid": sid, "ply": 1})
    check("analyze 会话版", st == 200 and an.get("recorded") and an.get("decided"), (st, str(an)[:120]))
    st, d, _ = call("/api/decode", {"initial_hands": [h0, h1], "first_player": 0, "opts": {},
                                    "moves": codes_played})
    check("decode 直传（真实牌谱）", st == 200 and len(d.get("moves") or []) == len(codes_played),
          (st, str(d)[:160]))
    if st == 200:
        m0 = d["moves"][0]
        check("decode 座位/牌面正确", m0.get("seat") == 0 and m0.get("cards") == mv,
              (m0.get("seat"), m0.get("cards"), mv))
    # 路径穿越防御
    st, r, _ = call("/init", {"hands": [h0, h1], "kitty": kitty, "leader": 0, "opts": {},
                              "sid": "../../evil"})
    check("init sid 穿越 -> 400", st == 400, (st, str(r)[:100]))
    st, r, _ = call("/init", {"hands": [h0, h1], "kitty": kitty, "leader": 0, "opts": {},
                              "sid": "abcd1234", "file": "../../evil.json"})
    check("init file 穿越 -> 400", st == 400, (st, str(r)[:100]))
    st, r, _ = call("/init", {"hands": [h0[:15], h1], "kitty": kitty, "leader": 0, "opts": {}})
    check("init 手牌 15 张 -> 400", st == 400, (st, str(r)[:100]))
    st, r, _ = call("/init", {"hands": [h0, h1], "kitty": kitty, "leader": 0,
                              "opts": {"bogus": True}})
    check("init opts 未知键 -> 400", st == 400, (st, str(r)[:100]))
    st, hs, _ = call("/health")
    check("health 版本信息", hs.get("pdkCommit") is not None
          and "openingBudget" in (hs.get("productionConfig") or {}), hs.get("productionConfig"))

print("=== 9) /api/belief 记牌猜牌（契约 + 会话两视角 + 负例）===")
A0519_HAND = [2, 13, 19, 21, 24, 27, 29, 39, 42, 44]
A0519_HIST = [
    {"seat": 0, "move": [1, 3, 4, 5], "pass_on": None},
    {"seat": 1, "move": [6, 7, 8, 9], "pass_on": None},
    {"seat": 0, "move": [16, 18, 20, 23], "pass_on": None},
    {"seat": 1, "move": [], "pass_on": None},
]
st, b, dt = call("/api/belief", {"my_hand": A0519_HAND, "opp_n": 3,
                                 "trick": [0, 2, 1, 0], "history": A0519_HIST})
NEED = {"worlds", "exhaustive", "weighted", "sharpness", "ledger", "rank_prob",
        "rank_exp", "top_hands", "facts", "lock"}
check("belief 200", st == 200, str(b)[:160])
check("belief 字段齐全", NEED <= set(b), sorted(set(b))[:10])
check("belief.worlds>0", int(b.get("worlds", 0)) > 0, b.get("worlds"))
check("belief.top_hands 概率和<=1", sum(h.get("p", 0) for h in b.get("top_hands", [])) <= 1.001,
      (b.get("top_hands") or [])[:1])
check("belief.ledger 每点 0..4", all(0 <= v <= 4 for v in (b.get("ledger") or {}).values()),
      b.get("ledger"))
check("belief.facts 为 {kind,text} 结构", all(isinstance(f, dict) and f.get("text")
      for f in (b.get("facts") or [])), (b.get("facts") or [])[:1])
check("belief.lock 结构含 leads", isinstance(b.get("lock"), dict) and "leads" in b["lock"],
      str(b.get("lock"))[:120])
# 张数分布 / 最可能的一手牌（上游 8325d96 新增：回答"到底几张"）
cp = b.get("rank_cnt_p") or {}
check("belief.rank_cnt_p 结构（点数->{k:概率}）",
      bool(cp) and all(isinstance(v, dict) and all(str(k).isdigit() for k in v)
                       for v in cp.values()), str(cp)[:110])
check("belief.rank_cnt_p 每点数概率和<=1",
      all(sum(float(x) for x in v.values()) <= 1.001 for v in cp.values()),
      {k: round(sum(float(x) for x in v.values()), 3) for k, v in list(cp.items())[:3]})
_rp = b.get("rank_prob") or {}
check("belief.rank_cnt_p 与 rank_prob 自洽（P(>=1) ≥ 1-P(0)）",
      all(abs(1.0 - float(v.get("0", 0))) <= float(_rp.get(k, 0)) + 0.02
          for k, v in cp.items() if k in _rp), str(cp)[:80])
# 上游 e037b99 新增：推理链 / 逐世界推演 / 牌型归属 / 对手牌型概率
inf = b.get("inferences")
check("belief.inferences 为数组且带 kind/text/conf",
      isinstance(inf, list) and all(isinstance(x, dict) and x.get("text") and "kind" in x for x in inf),
      str(inf)[:120])
wd = b.get("worlds_detail")
check("belief.worlds_detail 含 cands/worlds",
      isinstance(wd, dict) and isinstance(wd.get("cands"), list) and isinstance(wd.get("worlds"), list),
      str(wd)[:120])
if isinstance(wd, dict) and wd.get("cands"):
    check("worlds_detail.cands 含锁链画像(p_lock/chain/reclaim)",
          all(("p_lock" in c and "chain" in c and "reclaim" in c) for c in wd["cands"]),
          str(wd["cands"][:1]))
if isinstance(wd, dict) and wd.get("worlds"):
    check("worlds_detail.worlds 含逐世界 can_beat",
          all(isinstance(w.get("can_beat"), dict) and w.get("cards") is not None for w in wd["worlds"]),
          str(wd["worlds"][:1])[:140])
ctl = b.get("controls")
check("belief.controls 为牌型归属数组",
      isinstance(ctl, list) and all("pattern" in c and "i_hold_max" in c for c in ctl), str(ctl)[:120])
op = b.get("opp_patterns")
check("belief.opp_patterns 为牌型概率数组",
      isinstance(op, list) and all("pattern" in r and "p_has" in r for r in op), str(op)[:120])
st_w, wd_off, _ = call("/api/belief", {"my_hand": A0519_HAND, "opp_n": 3,
                                      "trick": [0, 2, 1, 0], "history": A0519_HIST,
                                      "world_detail": False})
check("belief world_detail=false 可关闭（worlds_detail 为空）",
      st_w == 200 and not (wd_off.get("worlds_detail") or {}).get("worlds"), str(wd_off.get("worlds_detail"))[:80])

ml = b.get("opp_most_likely") or {}
check("belief.opp_most_likely 为点数->张数",
      bool(ml) and all(int(v) >= 1 for v in ml.values()), str(ml)[:110])
check("belief.opp_most_likely 与 top_hands[0] 一致",
      (not (b.get("top_hands") or [])) or ml == (b["top_hands"][0].get("ranks") or {}),
      f'{ml} vs {(b.get("top_hands") or [{}])[0].get("ranks")}')
check("belief 概率与期望自洽（rank_prob >= rank_exp/4）",
      all(float((b.get("rank_prob") or {}).get(k, 0)) + 1e-6 >= float(v) / 4.0
          for k, v in (b.get("rank_exp") or {}).items()), b.get("rank_exp"))
st, r, _ = call("/api/belief", {"my_hand": [], "opp_n": 0, "trick": None, "history": []})
check("belief 空手牌 -> 400", st == 400, (st, str(r)[:90]))
st, r, _ = call("/api/belief", {"my_hand": [0], "opp_n": 99, "trick": None, "history": []})
check("belief opp_n 越界 -> 400", st == 400, (st, str(r)[:90]))
st, g2, _ = call("/api/new_game", {})
gidB = g2.get("gid") or gid
if gidB:
    st, v1, dt = call("/api/belief", {"sid": gidB, "perspective": "ai"})
    check("belief 会话版(AI 看我) 200",
          st == 200 and (v1.get("view") or {}).get("perspective") == "ai", (st, str(v1)[:120]))
    check("belief 会话版带当前局面",
          (v1.get("view") or {}).get("my_n") is not None
          and (v1.get("view") or {}).get("turn") in (0, 1), v1.get("view"))
    st, v2, dt = call("/api/belief", {"sid": gidB, "perspective": "me"})
    check("belief 会话版(我看 AI) 200",
          st == 200 and (v2.get("view") or {}).get("perspective") == "me", (st, str(v2)[:120]))
    st, r, _ = call("/api/belief", {"sid": gidB, "perspective": "xx"})
    check("belief 非法 perspective -> 400", st == 400, (st, str(r)[:90]))
    st, r, _ = call("/api/belief", {"sid": "deadbeefdeadbeefdeadbeef", "perspective": "ai"})
    check("belief 未知 sid -> 404", st == 404, st)
    if gid:
        # 已结束的局：出完牌的一侧明确报错；仍有牌的一侧给出"对手 0 张"的确定快照
        res = {}
        for persp in ("ai", "me"):
            st2, r2, _ = call("/api/belief", {"sid": gid, "perspective": persp})
            res[persp] = (st2, r2)
        empty_side = [k for k, (st2, r2) in res.items()
                      if st2 == 400 and "手牌" in str(r2.get("error", ""))]
        ok_side = [k for k, (st2, r2) in res.items() if st2 == 200]
        check("belief 结束局：出完牌的一侧 400 且说明原因", bool(empty_side), res)
        check("belief 结束局：仍有牌的一侧给出确定快照（opp_n=0 -> worlds=1）",
              all(int(r2.get("opp_n", -1)) == 0 and int(r2.get("worlds", 0)) >= 1
                  for k in ok_side for (st2, r2) in [res[k]]), res)
else:
    check("belief 会话版（无 gid 可测）", False, "new_game 未返回 gid")
print(f"\n=== 汇总：通过 {len(OK)} / 失败 {len(BAD)} ===")
if BAD:
    for b in BAD:
        print("  ❌", b)
else:
    print("  全部通过 ✅")
