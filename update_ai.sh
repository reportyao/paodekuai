#!/usr/bin/env bash
# 线上 AI 代码一键同步：git pull -> 校验资产 -> 必要时重建 C 核心 -> 冒烟测试 -> 重启本服务
# 只操作 paodekuai-ai.service 与 /home/ubuntu/pdk-ai-prod，不触碰其他服务。
set -euo pipefail

PROD=/home/ubuntu/pdk-ai-prod
VENV=/home/ubuntu/paodekuai/.venv/bin/python
SVC=paodekuai-ai.service

echo "== 1) git pull =="
cd "$PROD"
git pull --ff-only
git log -1 --format='   HEAD: %h %ad %s' --date=short

echo "== 2) 资产 md5 校验（README 四件套）=="
md5sum ckpt/policy_a2c_final56.pt ckpt/qnet.pt c/pdk_core.c
python3 - <<'PY'
import hashlib, sys
want = {
    "ckpt/policy_a2c_final56.pt": "534dcca82ee60760a1a40c546a83e5f1",
    "ckpt/qnet.pt": "4be2824a0f47c98be6fa8c80276d0777",
    "c/pdk_core.c": "aefb96685e0dc8a164c60bc00388919a",
}
bad = []
for f, w in want.items():
    h = hashlib.md5(open(f, "rb").read()).hexdigest()
    ok = (h == w)
    print(f"   {'OK ' if ok else 'BAD'} {f}")
    if not ok:
        bad.append(f)
sys.exit(1 if bad else 0)
PY

echo "== 3) C 核心重建（源码比 .so 新时）=="
cd "$PROD/c"
if [ ! -f pdk_core.so ] || [ pdk_core.c -nt pdk_core.so ]; then
  gcc -O2 -shared -fPIC -o pdk_core.so pdk_core.c
  echo "   已重建 pdk_core.so"
else
  echo "   pdk_core.so 已是最新"
fi

echo "== 4) 冒烟测试（规则/C 对拍/残局）=="
cd "$PROD"
for t in test_moves test_cross_c test_rules_r1r3 test_endgame_order; do
  out=$($VENV tests/$t.py 2>&1 | tail -1)
  echo "   $t: $out"
done

echo "== 5) 重启 $SVC 并验证 =="
sudo systemctl restart "$SVC"
for i in $(seq 1 12); do
  sleep 3
  if curl -s --max-time 5 http://127.0.0.1:8766/health >/dev/null; then break; fi
done
curl -s --max-time 10 http://127.0.0.1:8766/health | head -c 600
echo
echo "== 完成（其他服务未触碰）=="
systemctl is-active guandan.service paohuzi-bot.service paohuzi-node.service nginx.service paodekuai-web.service paodekuai-online.service | tr '\n' ' '
echo
