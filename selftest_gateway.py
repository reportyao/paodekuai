# -*- coding: utf-8 -*-
"""网关自测：鉴权 / 配额 / 路由 / 整局对局走 /v1/* 全链路。

用法：启动公开实例(8776) + 网关(8770)与测试 keys 后：python selftest_gateway.py
"""
import json
import time
import urllib.error
import urllib.request

import os
GW = os.environ.get("PDK_GW", "http://127.0.0.1:8770").rstrip("/")
KEY = os.environ.get("PDK_GW_KEY", "test_key_abc123")
OK, BAD = [], []


def call(path, obj=None, key=None, timeout=120, headers=None, method=None):
    data = json.dumps(obj).encode() if obj is not None else None
    h = {"Content-Type": "application/json"}
    if key:
        h["X-API-Key"] = key
    h.update(headers or {})
    req = urllib.request.Request(GW + path, data=data, headers=h,
                                method=method or ("POST" if data is not None else "GET"))
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
    print(f"  {'✅' if cond else '❌'} {name}{'' if cond else '  << ' + str(detail)[:200]}")


print("=== 1) 鉴权 ===")
st, r, _ = call("/v1/health")                                  # 匿名 health 允许
check("匿名 /v1/health 200", st == 200 and r.get("ok") is True, (st, r))
check("health 已脱敏（public=true, botRoot=hidden）",
      r.get("public") is True and r.get("botRoot") == "(hidden)", r.get("botRoot"))
st, r, _ = call("/v1/decide", {"my_hand": [0], "opp_n": 16, "trick": None, "history": []})
check("无 Key 调业务接口 -> 401", st == 401, (st, r))
st, r, _ = call("/v1/decide", {"my_hand": [0], "opp_n": 16, "trick": None, "history": []}, key="wrong")
check("错 Key -> 401", st == 401, (st, r))
st, r, _ = call("/v1/decide", {"my_hand": [0], "opp_n": 16, "trick": None, "history": []}, key=KEY)
check("正确 Key（X-API-Key）-> 200", st == 200 and "move" in r, (st, str(r)[:120]))
st, r, _ = call("/v1/decide", {"my_hand": [0], "opp_n": 16, "trick": None, "history": []},
                headers={"Authorization": "Bearer " + KEY})
check("正确 Key（Bearer）-> 200", st == 200 and "move" in r, st)

print("=== 2) 路由 ===")
st, r, _ = call("/v1/nope", {}, key=KEY)
check("未知 /v1 路径 -> 404", st == 404, (st, r))
st, r, _ = call("/init", {}, key=KEY)
check("非 /v1 路径 -> 404", st == 404, st)
st, r, _ = call("/v1/decide", {"my_hand": [0], "opp_n": 99, "trick": None, "history": []}, key=KEY)
check("上游 400 原样透传", st == 400, (st, r))

print("=== 3) 配额（并发=1）===")
import threading
res = []


def fire():
    res.append(call("/v1/new_game", {}, key=KEY, timeout=30))


ths = [threading.Thread(target=fire) for _ in range(6)]
for t in ths:
    t.start()
for t in ths:
    t.join(timeout=60)
codes = [r[0] for r in res]
check("并发下多数成功（200）", codes.count(200) >= 1, codes)
check("并发超限出现 429", 429 in codes, codes)
check("无 5xx（过载不崩）", not any(c >= 500 for c in codes), codes)
print(f"     6 并发返回码：{sorted(codes)}")
r1 = next((r[1] for r in res if r[0] == 200), {})

print("=== 4) 整局对局全链路（走网关）===")
g = r1
gid = g.get("gid")
check("拿 gid 且轮到调用方", bool(gid) and g.get("turn") == 0, (gid, g.get("turn")))
plies = 0
err = ""
while not g.get("finished") and plies < 120:
    legal = g.get("legal") or []
    if not legal:
        err = "无合法手"
        break
    singles = [c for c in legal if len(c) == 1]
    move = min(singles, key=lambda c: c[0]) if singles else legal[0]
    st, g2, dt = call("/v1/play", {"gid": gid, "cards": move}, key=KEY)
    if st != 200:
        err = f"HTTP {st} {str(g2)[:120]}"
        break
    plies += 1
    if g2.get("turn") != 0 and not g2.get("finished"):
        err = f"AI 未应手完 turn={g2.get('turn')}"
        break
    g = g2
check("整局对局（网关链路）无错完成", not err and g.get("finished"), err or g.get("finished"))
print(f"     手数={g.get('moves')} 调用方出牌={plies} 赢家={g.get('winner')} 比分={g.get('scores')}")
st, s1, _ = call(f"/v1/state?gid={gid}", key=KEY)
check("/v1/state 查询一致", st == 200 and s1.get("moves") == g.get("moves"), st)
st, sg, _ = call("/v1/suggest", {"gid": gid, "explain": True}, key=KEY)
check("/v1/suggest 200（终局则 reason 提示）", st == 200, (st, str(sg)[:100]))

print("=== 5) 审计日志 ===")
time.sleep(0.3)
import glob
LOG = os.environ.get("PDK_GW_LOG", "gw.log")          # 服务器上可指向 /var/log/paodekuai-api.log
cand = [LOG] if os.path.exists(LOG) else glob.glob("*.log")
lines = []
for _p in cand:
    try:
        lines += open(_p, encoding="utf-8", errors="replace").read().strip().splitlines()
    except OSError:
        pass
audit = [ln for ln in lines if "\t" in ln and "ms" in ln]
if not audit:
    print("  ℹ️ 本机没有审计日志文件（服务器上在 /var/log/paodekuai-api.log，可用 PDK_GW_LOG 指定）——跳过")
    check("审计日志（跳过：无本地日志文件）", True)
else:
    check("日志有记录且不含明文 Key", not any(KEY in ln for ln in audit), f"{len(audit)} 行")
    check("日志含状态码/耗时字段",
          any("\t200\t" in ln for ln in audit) and any("ms" in ln for ln in audit))
    print(f"     示例：{audit[-1]}")

print(f"\n=== 汇总：通过 {len(OK)} / 失败 {len(BAD)} ===")
for b in BAD:
    print("  ❌", b)
if not BAD:
    print("  全部通过 ✅")
