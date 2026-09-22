"""烟测: `/init` -> variant 是否端到端打通（含清理, 不污染复盘语料）。

背景: 主站实例 REPLAY_PREFIX="A", 每次 /init 都会在 data/replays/ 落一个 A 段文件
⇒ 烟测必须**自己清理掉**这些文件, 否则会污染"真人 vs AI"语料（那是项目最核心的资产）。
做法: 记录前后目录快照, 只删掉本次新建的文件（按 sid / no 双重匹配）。
"""
import importlib.util
import json
import os
import random
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.argv = [sys.argv[0]]
spec = importlib.util.spec_from_file_location("ai_bridge", os.path.join(HERE, "ai_bridge.py"))
br = importlib.util.module_from_spec(spec)
spec.loader.exec_module(br)

RD = br.REPLAY_DIR
before = set(os.listdir(RD))
deck = list(br.bot_server.DECK)
rnd = random.Random(20260923)
made = []
seen = set()
for i in range(6):
    if {"A", "B"} <= seen:
        break
    d = deck[:]
    rnd.shuffle(d)
    body = json.dumps({"hands": [sorted(d[:16]), sorted(d[16:32])],
                       "kitty": sorted(d[32:]), "leader": 0,
                       "opts": {}, "mode": "hybrid"}).encode()
    req = urllib.request.Request("http://127.0.0.1:8766/init", data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        r = json.loads(urllib.request.urlopen(req, timeout=60).read())
    except Exception as e:                                            # noqa: BLE001
        print("  init 失败:", e)
        break
    v = r.get("variant")
    seen.add(v)
    made.append((r.get("no"), v, r.get("sid"), r.get("file")))
    print("  第%d 次 /init: no=%s variant=%s sid=%s file=%s"
          % (i + 1, r.get("no"), v, r.get("sid"), r.get("file")))

print("本次见到的臂: %s（%d 次 init）" % (sorted(seen), len(made)))
if not ({"A", "B"} <= seen) and len(made) < 6:
    print("  提示: 6 次内没同时见到 A/B —— 分流是 50/50, 属正常波动, 不算故障")

# ---- 清理: 只删本次新建、且与本次 sid/no 对得上的文件 ----
after = set(os.listdir(RD))
new_files = after - before
killed = []
for f in new_files:
    stem = f[:-5] if f.endswith(".json") else f
    if any(stem in (str(m[2]), str(m[0])) for m in made):
        try:
            os.remove(os.path.join(RD, f))
            killed.append(f)
        except OSError as e:
            print("  删除失败", f, e)
left_new = (set(os.listdir(RD)) - before)
print("清理: 新建 %d 个, 已删 %d 个, 剩余新建 %d 个 %s"
      % (len(new_files), len(killed), len(left_new), sorted(left_new)[:4]))
print("目录文件数: 前 %d -> 后 %d（应回落到前值）" % (len(before), len(os.listdir(RD))))
