"""验证补丁 B: 新开一局的棋谱里是否带 `variant` / `ab_switch`（验完即删, 不污染语料）。"""
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
d = deck[:]
rnd.shuffle(d)
body = json.dumps({"hands": [sorted(d[:16]), sorted(d[16:32])], "kitty": sorted(d[32:]),
                   "leader": 0, "opts": {}, "mode": "hybrid"}).encode()
req = urllib.request.Request("http://127.0.0.1:8766/init", data=body,
                             headers={"Content-Type": "application/json"})
r = json.loads(urllib.request.urlopen(req, timeout=60).read())
print("  /init -> no=%s variant=%s file=%s" % (r.get("no"), r.get("variant"), r.get("file")))

fp = RD / (r.get("file") or "")
if not fp.exists():
    cand = [f for f in (set(os.listdir(RD)) - before)]
    fp = RD / cand[0] if cand else None
ok = False
if fp and fp.exists():
    rec = json.loads(fp.read_text(encoding="utf-8"))
    print("  棋谱字段: variant=%r ab_switch=%r no=%r live=%r"
          % (rec.get("variant"), rec.get("ab_switch"), rec.get("no"), rec.get("live")))
    ok = (rec.get("variant") == r.get("variant")) and bool(rec.get("ab_switch"))
else:
    print("  找不到刚落盘的棋谱文件")

# 清理
for f in (set(os.listdir(RD)) - before):
    try:
        os.remove(os.path.join(RD, f))
    except OSError:
        pass
print("清理后新增文件数:", len(set(os.listdir(RD)) - before),
      " 目录总数: %d -> %d" % (len(before), len(os.listdir(RD))))
print("结果:", "OK 棋谱已带 A/B 臂" if ok else "FAIL 棋谱没带上 variant")
sys.exit(0 if ok else 1)
