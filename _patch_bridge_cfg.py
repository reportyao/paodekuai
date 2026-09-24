"""口径统一 · #3：桥/服务"规则同源"（单开关，默认=现行为）。

问题（已定位到行）：`ai_bridge.py::build_prod_agent` 建 agent 时用 `bot_server.CFG`
（引擎模块默认 Config()），而同一会话的 CGame/Game 用 `build_cfg(opts)`（当局真实规则）。
默认会话就已不一致：build_cfg({}) = triple_no_follow=False, heart_ten_double=False；
Config() = True, True ⇒ **4 条规则里 2 条相反**。
后果：AI 的候选来自真实 cg.legal()（不会非法），但它内部的**世界/rollout 模拟跑在另一套规则下**。

修法：新增 env 开关 `PDK_CFG_FROM_SESSION`（默认 "0" = 现行为），置 "1" 时把**当局 cfg**
传给 agent。单开关、可 revert、默认不变。

用法: python3 _patch_bridge_cfg.py <ai_bridge.py 路径>
"""
import ast
import sys
from pathlib import Path

p = Path(sys.argv[1] if len(sys.argv) > 1 else "ai_bridge.py")
s = p.read_text(encoding="utf-8")
if "CFG_FROM_SESSION" in s:
    print("已打过补丁, 跳过"); sys.exit(0)

EDITS = [
    # 1) 开关定义（放在 PROD_MODES 附近）
    ('''PROD_MODES = ("hybrid", "dual")''',
     '''PROD_MODES = ("hybrid", "dual")

# 口径统一 #3（2026-09-21）: 是否让 **agent 用当局 cfg**（而非引擎模块默认 Config()）。
# 默认 "0" = 现行为（agent 用 bot_server.CFG）；置 "1" 后 agent 与 CGame 同规则。
# 为什么要开关: 这是**行为改动**（改变 AI 内部模拟的规则），按纪律必须可 revert + A/B。
CFG_FROM_SESSION = __import__("os").environ.get("PDK_CFG_FROM_SESSION", "0") == "1"'''),
    # 2) 函数签名 + 用 cfg
    ('''def build_prod_agent(mode: str = "hybrid"):''',
     '''def build_prod_agent(mode: str = "hybrid", cfg=None):'''),
    ('''    mode = mode if mode in PROD_MODES else "hybrid"
    kw = prod_solver_kw()''',
     '''    mode = mode if mode in PROD_MODES else "hybrid"
    # 口径统一 #3: 开关打开且给了当局 cfg 时，agent 与游戏同规则（否则维持现行为）
    _cfg = cfg if (CFG_FROM_SESSION and cfg is not None) else bot_server.CFG
    kw = prod_solver_kw()'''),
    ('''        agent = SolverAgent(fb, bot_server.CFG, **kw)''',
     '''        agent = SolverAgent(fb, _cfg, **kw)'''),
    ('''        agent = SolverAgent(fb, bot_server.CFG, total_threshold=kw.get("total_threshold", 28),''',
     '''        agent = SolverAgent(fb, _cfg, total_threshold=kw.get("total_threshold", 28),'''),
    # 3) Shadow 调用点传当局 cfg
    ('''        self.agent, self.mode, self.net = build_prod_agent(self.prod_mode)''',
     '''        self.agent, self.mode, self.net = build_prod_agent(self.prod_mode, self.cfg)'''),
]

for i, (old, new) in enumerate(EDITS, 1):
    n = s.count(old)
    if n != 1:
        print("!! EDIT %d 锚点命中 %d 次 (期望 1) —— 未写任何改动" % (i, n)); sys.exit(1)
    s = s.replace(old, new, 1)
ast.parse(s)
p.write_text(s, encoding="utf-8")
print("已打桥补丁 (%d 处): %s" % (len(EDITS), p))
