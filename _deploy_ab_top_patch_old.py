"""桥接侧 A/B 灰度补丁 —— **针对线上已部署的那版 ai_bridge.py**（不是仓库里的 ba72992 版）。

为什么单独一版（2026-09-23 部署时踩到的真问题）
--------------------------------------------
仓库版桥接（ba72992）在导入期就 `from pdk.trickguard import parse_trick`,
而**线上引擎 /home/ubuntu/pdk-ai-prod 里没有 pdk/trickguard.py**
⇒ 把仓库版桥直接铺上去, 一旦重启就是 `ModuleNotFoundError` —— **AI 整个挂掉**。
（实测: 先上传仓库版 -> `_verify_ab_top.py` 导入即报 ModuleNotFoundError;
 幸好是"先验证后重启", 没等到 systemd 加载。）
⇒ 结论: **桥与引擎必须同批部署**（这就是"16 张同批"那条纪律的真实约束）。
本次只做**外科式 A/B 注入**, 基于线上现有桥 + 线上现有引擎（引擎里 `single_top_rule` 已存在,
只是默认关 —— 已逐字节核对过 `_single_top_choice`）。

改动（6 处, 全部锚点先断言唯一；写盘前先过 ast）
---------------------------------------------
1. `import hashlib`
2. AB 常量 + `ab_variant()`（插在 `PROD_MODES` 行后）
3. `build_prod_agent(..., extra=None)` + `if extra: kw = {**kw, **extra}`
4. `Shadow.__init__`: 算 variant -> 注入 B 臂 -> **断言开关真开上了**（防无声 A/A）
5. `/init` 返回体加 `variant`（H5 据此写进复盘记录）
6. `/health` 加 `ab` 计数器（观测 + 事后分析）
"""
from pathlib import Path
import ast
import re

BR = Path('ai_bridge.py')
s = BR.read_text(encoding='utf-8')
if 'AB_TOP' in s:
    print('已打过 A/B 补丁, 跳过')
    raise SystemExit(0)


def sub_unique(text, anchor, new, what):
    """替换前断言锚点**唯一**：踩过 —— 4 空格缩进的锚点在 8 空格缩进行里
    也能当子串匹配, 结果把代码插进了 try 块中间, 造成 SyntaxError。"""
    n = text.count(anchor)
    assert n == 1, f'锚点[{what}]出现 {n} 次（必须恰好 1 次）——先核对文件版本'
    return text.replace(anchor, new, 1)


# 1) hashlib
if not re.search(r'^import hashlib$', s, re.M):
    ms = list(re.finditer(r'^import json$', s, re.M))
    assert len(ms) == 1, f'锚点[import json]出现 {len(ms)} 次'
    s = s[:ms[0].start()] + 'import hashlib\n' + s[ms[0].start():]

# 2) AB 常量 + 分流函数
a2 = 'PROD_MODES = ("hybrid", "dual")'
i = s.index(a2)
eol = s.index('\n', i) + 1
BLOCK = '''
# ── A/B 灰度（2026-09-23）: 按**局号**确定性分流, 只在 B 臂打开 single_top_rule ──
# 同一局永远落在同一臂（可配对）; PDK_AB_TOP 未置 "1" 时 **完全等同现行为**。
# 单开关 / 默认=现行为 / 可 revert（去掉 drop-in 里的 Environment 并 restart）。
AB_TOP = __import__("os").environ.get("PDK_AB_TOP", "0") == "1"
AB_SWITCH = "single_top_rule"
AB_ARM_B = {"single_top_rule": True}          # B 臂相对 A 臂的**唯一**差别
AB_STAT = {"A": 0, "B": 0, "mismatch": 0}     # 供 /health; mismatch="无声 A/A" 计数


def ab_variant(no) -> str:
    """按局号确定性分流: 'B'=开该开关, 'A'=现行为, '-'=未启用（不是实验局）。"""
    if not AB_TOP or no is None:
        return "-"
    h = int(hashlib.md5(str(no).encode("utf-8")).hexdigest()[:8], 16)
    return "B" if h % 2 == 0 else "A"
'''
s = s[:eol] + BLOCK + s[eol:]

# 3) build_prod_agent 支持 extra（锚点含 mode= 那行, 避免命中另外 3 处 prod_solver_kw）
a3 = 'def build_prod_agent(mode: str = "hybrid"):'
s = sub_unique(s, a3, 'def build_prod_agent(mode: str = "hybrid", extra=None):',
               'build_prod_agent 签名')
a3b = ('    mode = mode if mode in PROD_MODES else "hybrid"\n'
       '    kw = prod_solver_kw()\n')
s = sub_unique(s, a3b, a3b + ('    if extra:                                  '
                              '# A/B 注入（本会话唯一差别）\n'
                              '        kw = {**kw, **extra}\n'), 'prod_solver_kw 调用点')

# 4) Shadow: 算 variant -> 注入 -> 防无声 A/A
a4 = '        self.agent, self.mode, self.net = build_prod_agent(self.prod_mode)'
NEW4 = ('''        self.variant = ab_variant(self.no)               # A/B 分流（局号哈希, 确定性）
        self.agent, self.mode, self.net = build_prod_agent(
            self.prod_mode, AB_ARM_B if self.variant == "B" else None)
        if self.variant == "B":
            # 防"无声 A/A": 旧内核对未知 kwarg 会 TypeError 回退并静默丢掉该键
            if getattr(self.agent, AB_SWITCH, None) is not True:
                AB_STAT["mismatch"] += 1
                print(f"[bridge] ERROR A/B {AB_SWITCH} 未生效（编号 {self.no}）",
                      file=sys.stderr, flush=True)
            else:
                AB_STAT["B"] += 1
        elif self.variant == "A":
            AB_STAT["A"] += 1''')
s = sub_unique(s, a4, NEW4, 'Shadow 里建 agent')

# 5) /init 返回 variant
a5 = ('    return {"sid": sid, "ai_seat": s.ai_seat, "mode": s.mode,\n'
      '            "no": s.no, "file": s.file}, 200')
s = sub_unique(s, a5, ('    return {"sid": sid, "ai_seat": s.ai_seat, "mode": s.mode,\n'
                       '            "no": s.no, "file": s.file, '
                       '"variant": s.variant}, 200'), 'handle_init 返回')

# 6) /health 暴露 A/B（锚点含原行尾注释, 免得把注释挤到新块尾巴上）
a6 = '"pdkCommit": pdk_commit_info(),      # 生产 AI 仓库提交（模型版本号）'
s = sub_unique(s, a6, '''"pdkCommit": pdk_commit_info(),      # 生产 AI 仓库提交（模型版本号）
                        "ab": {"enabled": AB_TOP, "switch": AB_SWITCH,
                               "arms": dict(AB_STAT),
                               "rule": "局号 md5%2: 0=B(开), 1=A(现行为)"},''',
               '/health pdkCommit 行')

# ★ 先校验后落盘（systemd 是 Restart=always, 坏文件一旦重启就起不来）
try:
    ast.parse(s)
except SyntaxError as e:
    print('!! 补丁后语法不通过, 已放弃写入:', e)
    raise SystemExit(1)
BR.write_text(s, encoding='utf-8', newline='\n')
print('OK: 线上桥 A/B 补丁已落地（6 处锚点唯一, 写盘前过语法校验）')
