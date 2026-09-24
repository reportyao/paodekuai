"""最小判据: A/B 注入是否真的生效（部署后立刻跑; 不通过就不该开 PDK_AB_TOP=1）。

为什么必须有这个脚本（项目纪律"花钱测之前先证明会触发"）:
* `build_prod_agent` 里有 `except TypeError:` 回退分支 —— 若线上内核不认 `single_top_rule`,
  它会**静默丢掉**这个 kwarg, 于是 A/B 两臂完全相同（**无声 A/A**）, 我们会得到
  一个假的"中性"结论。本脚本直接断言 **B 臂 agent 的该属性为 True**、A 臂为 False。

用法（在 ~/paodekuai 下, 用桥自己的 venv 跑）:
    ./.venv/bin/python _verify_ab_top.py
"""
import importlib.util
import os
import sys
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
sys.argv = [sys.argv[0]]          # 不让本脚本的参数被桥当成 --bot-root
spec = importlib.util.spec_from_file_location(
    "ai_bridge", os.path.join(HERE, "ai_bridge.py"))
br = importlib.util.module_from_spec(spec)
spec.loader.exec_module(br)                       # 导入期不绑端口（服务在 main() 里起）

bad = 0
print("=== 1) 分流函数 ===")
br.AB_TOP = False
if br.ab_variant("A0250") != "-":
    print("  FAIL 未启用时应当恒 '-'"); bad += 1
else:
    print("  OK   未启用 -> '-'（= 不是实验局, 完全现行为）")
br.AB_TOP = True
ids = ["A%04d" % i for i in range(2000)]
c = Counter(br.ab_variant(x) for x in ids)
again = Counter(br.ab_variant(x) for x in ids)
det = all(br.ab_variant(x) == br.ab_variant(x) for x in ids)
print("  分布 %s  确定可复现=%s" % (dict(c), det))
if not det:
    print("  FAIL 同一局号两次分流不同（不可配对）"); bad += 1
if set(c) - {"A", "B"}:
    print("  FAIL 出现非法臂名"); bad += 1
ratio = c["B"] / max(1, (c["A"] + c["B"]))
if not (0.45 <= ratio <= 0.55):
    print("  FAIL 分流失衡 B=%.1f%%" % (100 * ratio)); bad += 1
else:
    print("  OK   50/50 分流（B=%.1f%%）" % (100 * ratio))

print("=== 2) 关键断言: kwarg 真的落到 agent 上 ===")
# 注意: 两版桥的第二个**位置**参数不同（仓库版=cfg, 线上版=extra）——必须用关键字调用,
# 否则会把 Config 当成 extra 传进去（实测踩到: TypeError: 'Config' object is not a mapping）。
kw_extra = {}
try:
    ag_a, _, _ = br.build_prod_agent("hybrid")                       # A 臂
    ag_b, _, _ = br.build_prod_agent("hybrid", extra=br.AB_ARM_B)     # B 臂（唯一差别）
except TypeError as e:
    print("  FAIL build_prod_agent 不接受 extra=（补丁没生效？）:", e)
    sys.exit(1)
va = getattr(ag_a, br.AB_SWITCH, None)
vb = getattr(ag_b, br.AB_SWITCH, None)
print("  A 臂 %s=%s   B 臂 %s=%s" % (br.AB_SWITCH, va, br.AB_SWITCH, vb))
if va is not False:
    print("  FAIL A 臂不是现行为（应为 False）"); bad += 1
else:
    print("  OK   A 臂 = 现行为（关闭）")
if vb is not True:
    print("  *** FAIL B 臂没开上 -> 开了就是无声 A/A, 禁止启用 PDK_AB_TOP ***")
    bad += 1
else:
    print("  OK   B 臂 = 开启（注入生效, 不是无声 A/A）")

print("=== 3) 环境与版本 ===")
print("  PDK_AB_TOP=%r  PDK_CFG_FROM_SESSION=%r"
      % (os.environ.get("PDK_AB_TOP"), os.environ.get("PDK_CFG_FROM_SESSION")))
try:
    print("  引擎: %s" % (br.pdk_commit_info(),))
except Exception as e:                                            # noqa: BLE001
    print("  引擎版本读取失败:", e)

print("结果: %s" % ("全部通过" if bad == 0 else "%d 项失败 —— 不要开启 A/B" % bad))
sys.exit(1 if bad else 0)
