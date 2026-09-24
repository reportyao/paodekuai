"""部署补丁 D: 更新接口文档 —— 写入 A/B 灰度（variant）语义 + 现网生效范围。

三份文档:
1. docs/AI_API.md                        （技术参考, 含"内部路径对照"附录）
2. docs/跑得快AI-对外接口文档-v1.md        （对外版）
3. docs/跑得快AI-对外接口文档-v1.html      （对外版·网页）

写什么（以及为什么必须写"生效范围"）
* `variant` 字段：桥在 `/init` 返回、`/health` 有 `ab` 块、棋谱里有 `variant`/`ab_switch`；
* **现网生效范围**：`AI_API.md` §2.3 描述过"错 `len` 直接 400"的语义校验 ——
  但**线上现在还没有**（线上引擎缺 `pdk/trickguard.py`, 桥与引擎必须同批）。
  文档若不标注, 就成了"文档说 400、线上静默退化"的谎 ⇒ 必须加这张表。
"""
import sys
from pathlib import Path

FILES = sys.argv[1:] or ['docs/AI_API.md']
EXT_MD = 'docs/跑得快AI-对外接口文档-v1.md'
EXT_HTML = 'docs/跑得快AI-对外接口文档-v1.html'

AB_BULLET = ('- 服务端会做**灰度（A/B）**验证内核开关：按**局号确定性分流**（同一局固定落在同一'
             '策略组，可配对分析）。会话接口与棋谱里会出现 `variant` 字段（`A`=现行为 / '
             '`B`=灰度新策略 / `-`=非实验局）；**本对外 `/v1/*` 实例不参与实验**（该字段为 '
             '`"-"`）。调用方忽略它即可 —— 响应只增字段，不影响既有解析。')

SCOPE = '''
**现网生效范围（2026-09-23）**

| 能力 | 现网状态 | 备注 |
|---|---|---|
| 会话 A/B 灰度（`variant`、`/health.ab`） | ✅ 已生效（主站会话实例 :8766） | 按局号 md5 分流；`Environment=PDK_AB_TOP=1` |
| 棋谱 `variant` / `ab_switch` 字段 | ✅ 已生效 | 16 张棋谱由**桥**落盘（`data/replays/*.json`） |
| `trick` **语义校验**（非法四元组 → HTTP 400 并给出正确写法） | ⏳ **尚未生效** | 代码已在仓库（`53ef38e`），但线上引擎缺 `pdk/trickguard.py` ⇒ **桥与引擎必须同批部署**；在此之前，错 `len` 仍会静默退化 |
| `PDK_CFG_FROM_SESSION`（agent 与当局同规则） | ⏳ 尚未生效 | 桥仓 `ba72992`，同上（同批） |

> 排障提示：`GET /health` 的 `ab` 块里 `mismatch` 是"**开关没真正生效**"的计数
> （旧内核不认该 kwarg 时会静默丢弃 ⇒ 两臂变成同一个）。它与 `A`/`B` 计数一起看，
> `mismatch > 0` 说明本组灰度不可信。

'''

# ---- 1) AI_API.md ----
p = Path(FILES[0])
s = p.read_text(encoding='utf-8')
if 'variant' in s:
    print('  %s 已写过 variant, 跳过' % p)
else:
    a = '- 响应**只增字段不删字段**；请忽略未知字段。'
    n = s.count(a)
    assert n == 1, f'AI_API.md 锚点出现 {n} 次'
    s = s.replace(a, a + '\n' + AB_BULLET, 1)
    # 附录 C: 补两行内部路径说明
    a2 = '| `/v1/decode` | `POST /api/decode` |'
    assert s.count(a2) == 1, 'AI_API.md 附录C 锚点不唯一'
    s = s.replace(a2, a2 + '\n'
                  '| `POST /init`（会话开局，仅内网） | 响应含 `variant`（A/B 策略组）、`no`（局号）、'
                  '`sid`、`file` |\n'
                  '| `GET /health` | 含 `ab` 块：`enabled`/`switch`/`arms`（各臂计数）/`mismatch` |',
                  1)
    # 生效范围表（放在 §5 末尾）
    a3 = '- 决策行为会随内核版本迭代（这是"始终用最新最强模型"的代价）；需要冻结版本请与服务方约定。'
    if a3 not in s:
        a3 = '- 服务端内核版本（模型/求解器）由服务方滚动更新'
        i = s.index(a3)
        eol = s.index('\n\n', i)
        s = s[:eol] + '\n' + SCOPE + s[eol:]
    else:
        s = s.replace(a3, a3 + '\n' + SCOPE, 1)
    p.write_text(s, encoding='utf-8', newline='\n')
    print('  OK %s（+A/B 说明 +附录C +生效范围）' % p)

# ---- 2) 对外接口文档 md / html ----
for f in (EXT_MD, EXT_HTML):
    q = Path(f)
    if not q.exists():
        print('  跳过(不存在):', q)
        continue
    t = q.read_text(encoding='utf-8')
    if 'variant' in t:
        print('  %s 已写过 variant, 跳过' % q)
        continue
    if f.endswith('.md'):
        a = '- 响应**只增字段不删字段**，请忽略未知字段。'
        assert t.count(a) == 1, 'ext md 锚点不唯一'
        t = t.replace(a, a + '\n' + AB_BULLET, 1)
    else:
        a = '<li>响应<b>只增字段不删字段</b>，请忽略未知字段。</li>'
        assert t.count(a) == 1, 'ext html 锚点不唯一'
        li = ('<li>服务端会做<b>灰度（A/B）</b>验证内核开关：按<b>局号确定性分流</b>；会话接口与棋谱'
              '里会出现 <code>variant</code>（<code>A</code>=现行为 / <code>B</code>=灰度新策略 / '
              '<code>-</code>=非实验局）。<b>本对外实例不参与实验</b>，该字段为 <code>"-"</code>，'
              '忽略即可。</li>')
        t = t.replace(a, a + '\n' + li, 1)
    q.write_text(t, encoding='utf-8', newline='\n')
    print('  OK %s（+灰度说明）' % q)
print('文档更新完成')
