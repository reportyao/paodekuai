"""部署补丁 B: 把 A/B 臂写进对局文件（16 张的棋谱由**桥**落盘, 不走 /replays/save）。

为什么必须在桥里写
----------------
* `server.py` 的 `/replays/save` **只接受 deck=15**（注释原文: "16 张由桥在服务端落盘,
  不走这里, 避免两套写入打架"）⇒ 16 张棋谱的 `variant` 只能由桥写；
* 不写进去, 事后就无法把对局配到臂上, 灰度白跑（`ab_prod_analysis.py` 正是按 `variant` 分臂）。

改动: 在 `write_replay()` 的 payload 里, `"no"` 那一行之后插入 `variant` / `ab_switch`。
锚点用**正则**匹配（行尾有中文注释, 纯字符串锚点会匹配不上 —— 已踩过）。
"""
from pathlib import Path
import ast
import re

BR = Path('ai_bridge.py')
s = BR.read_text(encoding='utf-8')
if '"variant": getattr(s' in s:
    print('已写过 variant 字段, 跳过')
    raise SystemExit(0)

PAT = re.compile(r'^(        "no": s\.no,.*\n)(        "timeText": s\.time_text,\n)', re.M)
ms = list(PAT.finditer(s))
assert len(ms) == 1, f'锚点(write_replay 的 no 行)匹配到 {len(ms)} 处（必须 1 处）——先核对文件版本'
ADD = ('        "variant": getattr(s, "variant", "-"),          '
       '# A/B 臂: A=现行为, B=开 single_top_rule（- = 非实验局）\n'
       '        "ab_switch": "single_top_rule",                  '
       '# 本组灰度的开关名（便于日后区分多组实验）\n')
s = s[:ms[0].end(1)] + ADD + s[ms[0].end(1):]

try:
    ast.parse(s)
except SyntaxError as e:
    print('!! 语法不通过, 已放弃写入:', e)
    raise SystemExit(1)
BR.write_text(s, encoding='utf-8', newline='\n')
print('OK: write_replay 已写入 variant / ab_switch')
