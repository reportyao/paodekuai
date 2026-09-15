# 跑得快 AI 策略服务 · 接口文档（v1）

> 本文档面向**外部调用方**：给你一套 HTTP 接口，输入"手牌 + 出牌历史"，返回 **AI 的出牌决策**、**决策理由（中文解释）**、**复盘分析**，以及**牌谱解码**工具。
> 服务由生产模型 `ckpt/policy_a2c_final56.pt`（A2C 策略网 + 残局精确求解器 + 规则层）驱动，与线上人机对战使用的是同一套内核。

- 文档版本：v1（对应 AI 仓库 `reportyao/pdk-ai` 提交 `d6162fa` 及以后，含 `9cf7a7a`）
- 牌规：两人跑得快（48 张牌，去大小王、三张 2、黑桃 A；黑桃 2 为最大单张；有牌必打；三张/三带一仅末手；顺子不含 2；炸弹 3333–KKKK）

---

## 1. 接入信息

| 项 | 值 |
|---|---|
| 公网基址（HTTPS） | `https://chinesetestsite.com/pdk-ai/v1` |
| 内网地址（同机调用，免鉴权） | `http://127.0.0.1:8766/api`（网页版对局实例） |
| 服务形态 | 对外走**独立 AI 实例 + 网关**：`paodekuai-api`(127.0.0.1:8776) + `pdkai-gateway`(127.0.0.1:8770)，由 nginx 的 `/pdk-ai/` 路由对外，与网页版对局实例（8766）物理隔离 |
| 传输 | HTTPS（TLS 1.2/1.3）；请求/响应均为 `application/json; charset=utf-8` |
| 鉴权 | 请求头 `X-API-Key: <你的Key>`（同时接受 `Authorization: Bearer <Key>`）；`/v1/health` 免 Key |
| 超时 | 网关到上游 60 秒；nginx 读超时 65 秒；单次决策通常 0.2–2.5 秒 |
| 请求体上限 | 256 KB（牌谱 history 通常 < 5 KB）；`history` ≤ 500 条、`my_hand` ≤ 20 张 |
| CORS | 默认允许任意来源（`Access-Control-Allow-Origin: *`），浏览器可直接调用 |
| 审计 | 每次调用按 `时间 / Key 名 / 接口 / 状态码 / 耗时` 记入 `/var/log/paodekuai-api.log`（**不记录明文 Key**） |

### 1.1 鉴权

```
X-API-Key: pdk_live_xxxxxxxxxxxxxxxx
```

Key 由服务方签发，绑定：配额（每分钟/每天/并发）+ 可选 IP 白名单。Key 泄露可由服务方即时吊销。

### 1.2 限流与配额（当前生效值，可按 Key 调整）

| 限制项 | 当前值 | 超限响应 |
|---|---|---|
| 单 Key 速率 | 120 次/分钟 | `429` + `Retry-After` |
| 单 Key 并发 | 1 个进行中请求 | `429` + `Retry-After: 2` |
| 单 Key 日配额 | 50 000 次/天 | `429` + `Retry-After: 3600` |
| 单 IP 速率（nginx 兜底） | 5 次/秒（burst 10） | `429` |
| 外部实例全局并发 | 1（整局对局与单步决策共用） | `503` + `Retry-After: 5` |
| 会话数上限 | 64（对外实例），空闲 2 小时自动回收 | — |
| 未知 Key / 无 Key | — | `401` |
| 来源 IP 不在白名单 | 白名单当前为空（不限制），可随时加 | `403` |

> 服务跑在 2 vCPU 的共享服务器上，且与你自己的对局实例物理隔离（对方实例 `CPUWeight=100`，你的对局实例 `1000`）：**外部流量再大也不会抢占你自己对局的算力**。整局对局是"服务端持局 + AI 自动应手"，一次 `/v1/play` 可能连续算多手，请按顺序调用（不要并发打同一个 gid）。

### 1.3 错误响应

所有错误统一为：

```json
{"error": "human readable message"}
```

| HTTP | 含义 | 处理建议 |
|---|---|---|
| 200 | 成功 | — |
| 400 | 参数缺失/格式错误/牌谱不合法/牌 id 越界 | 修正请求，不要重试 |
| 401 | 缺少或无效 API Key | 检查请求头 |
| 403 | 来源 IP 不在白名单 | 联系服务方加白 |
| 404 | 未知 `/v1` 接口；或 `gid`/`sid` 不存在或已过期（会话空闲 2h 回收） | 重新 `/v1/new_game` |
| 409 | 现在不是你的回合（整局对局里轮次不在调用方） | 先 `/v1/state` 对账，再决定是否重发 |
| 413 | 请求体/字段过大 | 精简 history |
| 429 | 速率/并发/日配额超限 | 按 `Retry-After` 退避重试 |
| 500 | AI 内核异常（**不降级**：绝不返回"猜测结果"） | 可重试 1–2 次；持续失败请联系服务方 |
| 502 | 网关到 AI 服务不可达/超时 | 指数退避重试 |
| 503 | 服务繁忙或排队超时（全局并发已满） | 按 `Retry-After` 重试 |

---

## 1.4 接口总览

| 接口 | 用途 | 形态 |
|---|---|---|
| `GET  /v1/health` | 健康与模型版本（`pdkCommit`、`netProbe`、`openingBudget`） | 免 Key |
| `POST /v1/new_game` | **整局对局**：开新局（服务端发牌，AI 自动应手） | 会话 |
| `POST /v1/play` | **整局对局**：你出一手，AI 立即应手 | 会话 |
| `GET  /v1/state?gid=` | **整局对局**：查询局面（只读） | 会话 |
| `POST /v1/suggest` | **整局对局**：让 AI 给当前出牌方一手建议（可带解释） | 会话 |
| `POST /v1/decide` | 决策一手（给局面快照，返回 AI 的选择 + 解释） | 无状态 |
| `POST /v1/explain` | 解释整局里某一手（`moves` + `ply`） | 无状态 |
| `POST /v1/analyze` | 单步分析：真实局面下 AI 会怎么打（人类手=反事实对照） | 无状态 |
| `POST /v1/decode` | 动作码 → 具体牌面/牌型/归属（牌谱工具） | 无状态 |

**主要需要"AI 出牌"就只用前四个**：`new_game` 开局 → 你 `play` 一手 → AI 自动应手 → 循环到终局。

---

## 2. 数据模型

### 2.1 牌 ID（全局约定）

- `id ∈ [0, 48]`，共 **48** 张牌（注意：**包含 48，不含 47**）
- **点数** = `id >> 2`：`0=3, 1=4, 2=5, 3=6, 4=7, 5=8, 6=9, 7=10, 8=J, 9=Q, 10=K, 11=A, 12=2`
- **花色** = `id & 3`：`0=黑桃, 1=红桃, 2=梅花, 3=方片`
- 牌库构成：3~K 各 4 张（id 0–43）、A 3 张（id 44/45/46）、黑桃 2 一张（**id 48**）
- 花色只影响显示与红桃 10 翻倍；**策略与合法性只看点数**
- 等价说明：id 47（♦A 槽位）与 id 49/50/51（♥2/♣2/♦2 槽位）**不在牌库中**。花色字段仅供显示，所以"被扣掉的是哪一张 A / 哪几张 2"不影响任何决策与合法性判定（网页版按"去掉黑桃 A"显示，与这里的槽位口径只是花色标签不同）

```python
rank_of = lambda c: c >> 2
suit_of = lambda c: c & 3
RANK_CN = "3456789XJQKA2"      # X = 10
SUIT_CN = "♠♥♣♦"
```

### 2.2 动作

- **出牌** = 牌 id 数组，例如 `[36, 37]` 表示"一对 Q"
- **过牌/不出** = 空数组 `[]`（仅当无牌可压时合法）

### 2.3 牌型元组 `trick`（"台上要压的牌"）

`trick = [ptype, main, len, nc]`，`null` = **我方领出**（自由出牌）。

| ptype | 名称 | 说明 | main | len |
|---|---|---|---|---|
| 0 | 单张 SINGLE | — | 点数 | 1 |
| 1 | 对子 PAIR | — | 点数 | 2 |
| 2 | 连对 PAIRS | ≥2 连对（如 7788） | **最大**点数 | 组数（3 对=3） |
| 3 | 三张 TRIPLE | 仅末手可出/接 | 点数 | 3 |
| 4 | 三带二 TRIPLE2 | 555+3+4 → `[4, 2, 5, 0]` | 三张点数 | 5 |
| 5 | 三带一 TRIPLE1 | 仅末手 | 三张点数 | 4 |
| 6 | 飞机 AIRPLANE | 三张连（需变体开关） | 最大点数 | — |
| 7 | 顺子 STRAIGHT | ≥5 连张，不含 2 | **最大**点数 | 张数 |
| 8 | 炸弹 BOMB | 3333–KKKK | 点数 | 4 |
| 9 | 四带三 FOUR3 | 需变体开关 `four3` | 四张点数 | 7 |

> ⚠️ 两个容易踩的点：
> 1. **`main` 对顺子/连对是"最大点数"**（顺子 3-4-5-6-7 → `main=4` 即 7）。解释文案里"顺子起X"用的是同一字段，属上游措辞问题。
> 2. `nc` 是内核附加计数（炸弹为 1，其余为 0）。**推荐做法：调用方不要自己构造 `trick`** —— 让服务端产出（见 §3.5 `/v1/legal`）或原样回传上一次服务响应里的值。

对照示例（可直接用作测试向量）：

| 出牌 | 牌 id | code | trick |
|---|---|---|---|
| 单张 3 | `[0]` | 1 | `[0, 0, 1, 0]` |
| 单张 2 | `[48]` | 68719476736 | `[0, 12, 1, 0]` |
| 对子 Q | `[36, 37]` | 268435456 | `[1, 9, 2, 0]` |
| 连对 7788 | `[16,17,20,21]` | 73728 | `[2, 5, 2, 0]` |
| 三带二 555+3+4 | `[0,4,8,9,10]` | 201 | `[4, 2, 5, 0]` |
| 顺子 3-4-5-6-7 | `[0,4,8,12,16]` | 4681 | `[7, 4, 5, 0]` |
| 炸弹 5555 | `[8,9,10,11]` | 256 | `[8, 2, 4, 1]` |

### 2.4 出牌历史 `history`（数组，按时间升序）

```json
[
  {"seat": 0, "move": [0]},                                  // 我出的单张3
  {"seat": 1, "move": [36, 37]},                             // 对手出一对Q
  {"seat": 0, "move": [], "pass_on": [1, 9, 2, 0]}           // 我过牌（被过牌型=一对Q）
]
```

| 字段 | 说明 |
|---|---|
| `seat` | **相对座位**：`0` = 提问方（本次决策的执行者），`1` = 对手 |
| `move` | 牌 id 数组；`[]` = 过牌 |
| `pass_on` | 过牌时被"过"掉的牌型元组（可选，但**建议给**：用于对手信念推断） |

- 每次调用都要带**完整历史**（从开局第一手开始），服务端据此重建局面、推断对手手牌分布。
- 若只想问"当前局面怎么打"，也必须把之前所有手都带上；缺失会导致判断质量下降。

### 2.5 规则变体 `opts`

```json
{"red10": true, "four3": false, "nobomb": true, "sanzhang": false}
```

| 键 | 默认 | 含义 |
|---|---|---|
| `red10` | `false` | 红桃 10 翻倍（拿到 ♥10 的玩家输赢 ×2） |
| `four3` | `false` | 允许"四带三"（4 张 + 任意 3 张） |
| `nobomb` | `true` | 炸弹不可拆开当作其他牌型打出 |
| `sanzhang` | `false` | 三张不可接（三张/三带一仅末手，且不能管三带二） |

> `opts` 必须与调用方实际使用的规则一致，否则 AI 会用错误规则决策。

---

## 3. 接口

### 3.0 `GET /v1/health` — 健康与版本

```bash
curl -s https://chinesetestsite.com/pdk-ai/v1/health -H "X-API-Key: $KEY"
```

```json
{
  "ok": true,
  "agent": "prod",
  "productionModel": "ckpt/policy_a2c_final56.pt",
  "netProbe": {"net": "a2c-final56(56x)", "error": null},
  "modes": ["dual", "hybrid"],
  "productionConfig": {"openingSearch": true, "openingWorlds": 16, "totalThreshold": 28},
  "sessions": 0
}
```

- `netProbe.net` 必须为 `a2c-final56(56x)`：若变成 `dmc-v1(low feature)` 说明生产模型未加载（服务方会拒绝服务，不会静默降级）。
- `sessions` 是公共实例上的会话数（无状态接口调用不会占用会话）。

---

### 3.1 `POST /v1/decide` — 决策一手（核心接口）

输入当前局面，返回 AI 的策略选择（可选带中文解释）。

**请求**

```json
{
  "my_hand": [0, 4, 12, 16, 20, 24, 28, 32, 36, 40, 44, 45, 46, 48, 5, 9],
  "opp_n": 16,
  "trick": null,
  "history": [],
  "opts": {"red10": true},
  "explain": true,
  "mode": "hybrid"
}
```

| 字段 | 必填 | 说明 |
|---|---|---|
| `my_hand` | ✅ | 我方（`seat=0`）当前手牌 id 数组，**已扣除已出的牌** |
| `opp_n` | ✅ | 对手剩余张数 |
| `trick` | ✅ | 待压牌型元组；`null` = 我方领出 |
| `history` | ✅ | 完整出牌历史（见 §2.4），无历史传 `[]` |
| `opts` | ⬜ | 规则变体（见 §2.5），默认全默认配置 |
| `explain` | ⬜ | `true` 时附带中文解释（见下） |
| `mode` | ⬜ | `hybrid`（默认，**胜率优先**，生产配置）/ `dual`（**净分优先**，积分制场合残局按得分估值） |

**响应**

```json
{
  "move": [36, 37],
  "pass": false,
  "legal_count": 43,
  "mode": "hybrid",
  "engine": "c",
  "openingSearch": true,
  "explain": {
    "path": "opening_search",
    "seat": 0,
    "move": 268435456,
    "text": "对子QQ",
    "reason": "在 16 个采样世界里求解, 对子QQ 的胜率最高 (68%)。 次优: …",
    "cands": [
      {"move": 268435456, "text": "对子QQ", "win_prob": 0.68},
      {"move": 268435497, "text": "对子KK", "win_prob": 0.56}
    ],
    "belief": {"source": "读牌网络", "facts": ["对手高置信至少 2 张A"]},
    "worlds": 16
  }
}
```

- `move` 为牌 id 数组；`pass: true` 时 `move` 为 `[]`（无牌可压才会过牌）
- `legal_count`：当前合法动作总数（便于对账）
- `explain.path` 取值与中文含义：

| path | 含义 |
|---|---|
| `opening_search` | 开局搜索（多世界 rollout，早期领出） |
| `pimc_c` / `pimc_cn` | 残局精确求解（定胜负 / 按净分） |
| `endgame_order` | 残局连续保权（对手压不住时按顺序走完） |
| `report_dump` | 对手报单时保出牌权 |
| `one_shot` | 一手直接走完 |
| `lookahead` | 前瞻搜索（价值网 2-ply） |
| `fallback_net` | 策略网络直接估值（局面较大、未启用残局求解） |
| `forced` | 只有唯一合法手 |

- `explain.cands`：候选动作与胜率（仅在残局求解/开局搜索路径给出）
- `explain.belief`：读牌结论（对手手牌推断），可能为空

**性能参考**（2 vCPU 实测）：中盘网络决策 0.2–0.8 s；残局 PIMC 0.5–2.5 s；`opening_search` 受时间预算约束（新版默认 1.5 s）。

---

### 3.2 `POST /v1/explain` — 解释某一手（复盘用）

两种用法：

**(a) 整局牌谱 + ply** ：解释"第 ply 手 AI 的决策依据"

```json
{
  "initial_hands": [[0,4,...16张], [1,2,...16张]],
  "first_player": 0,
  "ai_seat": 1,
  "opts": {"red10": true},
  "moves": [1, 134217728, 0, 268435456, ...],
  "ply": 7
}
```

- `moves`：按时间升序的动作序列，元素可以是 **code（整数）** 或 **牌 id 数组**
- `ply` 从 1 开始；不传 = 最后一手
- 若 `ply` 指向的不是 AI 决策点（例如人类那一手），服务会**自动回退到 AI 最近一次决策**并置 `anchored_back: true`

**响应**

```json
{
  "ply": 7, "seat": 1, "move": 67146240,
  "text": "三带二 6+7+8+JJJJ",
  "path": "pimc_c",
  "reason": "在 32 个采样世界里求解, 三带二 6+7+8+JJJJ 的胜率最高 (62%)。 次优: …",
  "cands": [{"move": 123, "text": "三带二 7+8+JJJJ+Q", "win_prob": 0.56}],
  "belief": {"source": "读牌网络", "facts": ["对手高置信至少 2 张A"]},
  "state_text": "我剩 16 张, 对手剩 16 张; 我是领出方",
  "recorded": "三带二 6+7+8+JJJJ",
  "decided": "三带二 6+7+8+JJJJ",
  "anchored_back": false,
  "replay": [{"ply": 1, "seat": 0, "text": "单张7"}]
}
```

> 注意：该接口在重放时会**让 AI 重新决策它自己的每一手**，因此 `decided`（重算结果）可能与 `recorded`（牌谱记录）不同——残局搜索有世界采样，属正常波动。要看"在真实局面下 AI 会怎么打"，用 §3.3 `/v1/analyze`。

---

### 3.3 `POST /v1/analyze` — 单步分析（复盘/点评用，**基于真实局面**）

给一手（AI 手或人类手皆可），返回"**按真实牌谱的局面，AI 会怎么打 + 为什么**"。

```json
{
  "initial_hands": [[...], [...]],
  "first_player": 0,
  "opts": {"red10": true},
  "moves": [[0], [36,37], [], [8,9,10,20,21]],
  "ply": 3,
  "mode": "hybrid"
}
```

`moves` 元素可以是牌 id 数组、code（整数），或 `{"seat":0,"cards":[...],"pass":false}`。

**响应**

```json
{
  "ply": 3,
  "seat": 0,
  "ai_seat": 1,
  "mode": "hybrid",
  "engine": "c",
  "my_n": 15,
  "opp_n": 15,
  "state_text": "我剩 15 张, 对手剩 15 张; 台上: 单张6",
  "recorded": {"cards": [18], "patText": "单张7", "pass": false},
  "decided":  {"cards": [20], "patText": "单张8", "pass": false},
  "agree": false,
  "explain": {"path": "fallback_net", "reason": "…", "cands": []},
  "note": "此处双方合计 30 张，超过 28 张阈值：未启用残局精确求解，由策略网络(56 维动作后特征)直接估价；到残局(不超过 28 张)才有胜率与候选对比。"
}
```

| 字段 | 含义 |
|---|---|
| `recorded` | 牌谱里**实际出的一手**（`patText` 为中文牌型描述，`cards` 为牌 id） |
| `decided` | AI 在这个局面下**会出的一手** |
| `agree` | 两者是否一致（一致的次数 = AI 认可该手；不一致 = 值得复盘的差异点） |
| `state_text` | 中文局面描述（双方剩余张数 + 台上牌型） |
| `note` | 补充说明（如"为何这次没有胜率候选"） |

**典型用途**：人工点评时定位到某一手 → `agree=false` 的点就是"AI 认为更好的选择"，配合 `explain.reason` 形成可读的复盘结论。

---

### 3.4 `POST /v1/decode` — 牌谱解码

把**动作码序列**还原成"每一手谁出的、具体哪些牌、什么牌型、出后剩几张"。历史牌谱常只存 code，这个接口用于渲染/对账。

```json
{
  "initial_hands": [[...16张...], [...16张...]],
  "first_player": 0,
  "opts": {"red10": true},
  "moves": [1, 134217728, 0, 268435456]
}
```

**响应**

```json
{"moves": [
  {"ply": 1, "seat": 0, "cards": [0],   "pass": false, "pass_on": null,        "combo": {"ptype":0,"main":0,"len":1,"nc":0}, "handAfter": 15, "patText": "单张3"},
  {"ply": 2, "seat": 1, "cards": [36,37], "pass": false, "pass_on": null,      "combo": {"ptype":1,"main":9,"len":2,"nc":0}, "handAfter": 15, "patText": "对子Q"},
  {"ply": 3, "seat": 0, "cards": [],    "pass": true,  "pass_on": [1,9,2,0],   "combo": null,                                  "handAfter": 15, "patText": "不出"}
]}
```

- **座位由内核判定**（过牌后同一人继续领出，不能按"奇偶交替"猜）
- 牌谱与手牌不一致（张数不符/动作非法）时返回 `400`，**不输出猜测结果**

---

### 3.5 合法动作从哪来？

对外**没有**单独的 `/v1/legal` 接口——合法动作直接随整局对局快照返回：`/v1/new_game`、`/v1/play`、`/v1/state` 的响应里都带 `legal`（数组的数组，牌 id 列表）与 `legal_pass`（是否可以过牌），
且 `legal` 只在"轮到调用方"时非空。若你的客户端自己实现牌局、需要"给局面判合法手"，可申请开通无状态 `/v1/legal`（服务端用同一内核判定）。

### 3.6 整局对局接口（推荐给"主要需要 AI 出牌"的调用方）

服务端持有牌局状态并自动发牌、自动让 AI 应手；调用方只需要出自己的那一手。**调用方固定坐座位 0，AI 坐座位 1。**

| 接口 | 用途 |
|---|---|
| `POST /v1/new_game` | 开新局；返回你的手牌、全部合法动作、当前要压的牌型 |
| `POST /v1/play` | 你出一手（或过牌），**服务端立即让 AI 连续应手**，返回新局面 |
| `GET  /v1/state?gid=` | 查询当前局面（只读，不推进） |
| `POST /v1/suggest` | 让 AI 替**当前出牌方**给一手建议（不动局面），可带中文解释 |

**`POST /v1/new_game`**

```json
请求 {"opts": {"red10": true}, "mode": "hybrid"}     // opts/mode 均可省略
响应 {
  "gid": "81658db5598f4953a866be4a",
  "you_are": 0, "ai_seat": 1,
  "turn": 0, "finished": false, "winner": null,
  "my_hand": [0,1,12,15,20,22,23,32,34,35,37,38,39,40,46,48],
  "my_n": 16, "opp_n": 11,
  "trick": [4, 2, 5, 0], "trick_text": "三带二5",
  "last_moves": [{"seat": 1, "cards": [8,9,10,0,4]}],
  "legal": [[...], ...], "legal_pass": false,
  "scores": [0, 0], "mode": "hybrid", "opts": {"red10": true},
  "moves": 1, "new": true
}
```

- **保证**：返回时 `turn == 0`（轮到你）。若 AI 持黑桃 3 先手，它的开局动作已在服务端走完（`moves=1`、`opp_n` 已减少）。
- `trick = null` 表示你是领出方（自由出牌）；否则按 §2.3 解读。

**`POST /v1/play`**

```json
请求 {"gid": "81658db5...", "cards": [20, 21]}    // cards=[] 表示过牌（仅 legal_pass=true 时合法）
响应 = 与 /v1/state 相同的完整快照（turn 必回到 0，或 finished=true）
```

- 非法出牌 → `400`；不是你的回合 → `409`。
- **幂等/续打语义**：若上一次调用在 AI 应手中途失败（AI 异常、网络中断），**原样重发同一个请求**即可继续——此时轮次已是 AI，服务只补完 AI 的应手，不会重复落下你那一手。
- `scores` 在 `finished=true` 时有效：底分 1 分/张、炸弹 10 分/颗、红桃 10 翻倍（`red10`）、对手一张未出翻倍（关门），与网页版计分逐局对账一致。

**`POST /v1/suggest`**（给"当前该出牌的一方"，一般就是你自己）

```json
请求 {"gid": "81658db5...", "explain": true}
响应 {"cards": [36, 37], "pass": false, "seat": 0, "mode": "hybrid", "legal_count": 33,
      "explain": {"path": "pimc_c", "text": "对子QQ",
                  "reason": "在 32 个采样世界里求解, 对子QQ 的胜率最高 (62%)。…"}}
```

可用于"AI 帮我这一手"（把 `cards` 交给 `/v1/play` 即可）或"AI 自动托管整局"。
`cards=[]` + `pass=true` 表示 AI 建议过牌（合法前提：`legal_pass=true`）。

**完整对局示例（Python）**

```python
import requests
KEY = "pdk_live_xxxxxxxx"
BASE = "https://chinesetestsite.com/pdk-ai/v1"
H = {"X-API-Key": KEY, "Content-Type": "application/json"}

g = requests.post(f"{BASE}/new_game", headers=H, json={"opts": {"red10": True}}).json()
gid = g["gid"]
print("我的牌:", g["my_hand"], "| 要压:", g.get("trick_text") or "（我领出）")

while not g["finished"]:
    legal = g["legal"]
    move = max(legal, key=lambda c: sum(c))          # 这里随便挑一手；真实客户端按自己的策略选
    r = requests.post(f"{BASE}/play", headers=H, json={"gid": gid, "cards": move}, timeout=60)
    if r.status_code in (429, 502, 503):
        continue                                      # 限流/抖动：稍等重发同一请求即可（幂等）
    r.raise_for_status()
    g = r.json()

print("终局：赢家", g["winner"], "比分", g["scores"])
```

> 数据归属：对外实例把牌局落盘在**独立目录**（`data/external/`），与主站对局记录、人工点评完全隔离；如果你的对局希望被留存分析，可与服务方约定。

---

## 4. 最佳实践

1. **别把它当规则裁判**（除 §3.5 开启的情况）：规则合法性请在你的客户端判定；本服务是**策略服务**，输入非法牌谱会返回 `400/500` 而不是"纠正"。
2. **一次请求一次决策**：不要用并发代替批处理；需要多手连续决策时，每手把上一手结果并入 `history` 再问。
3. **`history` 必须完整**：这是 AI 推断对手手牌的唯一信息源，省略会明显降低质量。
4. **重试策略**：`429/503` 按 `Retry-After` 或指数退避重试；`400` 不要重试（先修参数）。
5. **决策不确定性**：残局 PIMC 与开局搜索带随机世界采样，同一局面多次调用**可能出现不同选择**（属设计特性，用于覆盖不确定性）；需要可复现可用 `opts`+固定 `seed` 的私有部署（另行沟通）。
6. **不要缓存过期决策**：`my_hand`/`opp_n`/`history` 任一变化都必须重新请求。
7. **上生产前**：先用 `/v1/health` 确认 `netProbe.net = a2c-final56(56x)`，并做一次 100 次的压测对账（`legal_count`、`pass` 分布）。
8. **整局对局别并发同一个 gid**：AI 应手串行计算（对外实例内部并发闸=1），并发调用会拿到 `429/503`；顺序调用即可。
9. **会话会过期**：`gid` 空闲 2 小时自动回收，过期后 `/v1/play` 返回 `404`，重新 `/v1/new_game` 即可。
10. **决策带随机性**：残局 PIMC 与开局搜索带世界采样，同一局面多次调用可能给出不同但同样正确的选择（设计特性）；需要完全可复现请与 service 方约定固定种子实例。

---

## 5. 版本与兼容

- 路径版本：`/v1/*`；破坏性变更会升级为 `/v2/*`，`v1` 至少保留 3 个月。
- 响应**只增字段不删字段**；请忽略未知字段。
- 服务端内核版本（模型/求解器）由服务方滚动更新，**决策行为可能随版本变化**（这是"用最新最强模型"的代价）。`/v1/health` 会给出模型标识与配置摘要；如需冻结版本，可申请专属实例。

---

## 附录 A：两人跑得快规则要点（服务假设的规则）

1. 牌库：48 张 = 去掉大小王、三张 2（♥2 ♣2 ♦2）、黑桃 A；**黑桃 2 保留且为全场最大单张**
2. 发牌：随机弃 16 张（底牌不可见），双方各 16 张；首局由持黑桃 3 者先出
3. 牌型：单张、对子、连对（≥2 对）、三张、三带二、三带一、飞机、顺子（≥5 张，不含 2）、炸弹（4 张同点，3333–KKKK）、四带三（变体）
4. **有牌必打**：能压必须压；只有确实压不上时才可过牌
5. 炸弹可压任意牌型；炸弹之间比点数；炸弹不可拆（`nobomb`）
6. 三张/三带一仅最后一手（`sanzhang` 变体下更严：不能管三带二）
7. 计分：底分 1 分/张，按输家剩余张数计；对手一张未出（关门）翻倍；炸弹每颗 10 分；红桃 10 翻倍（可选）
8. 顺子中不能出现 2；A 只作大牌（不作为小牌接 2）

## 附录 B：Python 最小调用示例

```python
import requests

KEY = "pdk_live_xxxxxxxx"
BASE = "https://chinesetestsite.com/pdk-ai/v1"
H = {"X-API-Key": KEY, "Content-Type": "application/json"}

def decide(my_hand, opp_n, trick, history, explain=True):
    r = requests.post(f"{BASE}/decide", headers=H, timeout=60, json={
        "my_hand": my_hand, "opp_n": opp_n, "trick": trick,
        "history": history, "opts": {"red10": True},
        "explain": explain, "mode": "hybrid",
    })
    r.raise_for_status()
    return r.json()

d = decide([0,4,12,16,20,24,28,32,36,40,44,45,46,48,5,9], 16, None, [])
print(d["move"], d["pass"], d.get("explain", {}).get("reason"))
```

## 附录 C：内部路径对照（服务方排障用，外部请用 `/v1/*`）

| 公开路径 | 内网路径（127.0.0.1:8766） |
|---|---|
| `/v1/health` | `GET /health` |
| `/v1/new_game` | `POST /api/new_game` |
| `/v1/play` | `POST /api/play` |
| `/v1/state` | `GET /api/state?gid=` |
| `/v1/suggest` | `POST /api/suggest`（网页版同源：`POST /suggest {sid}`） |
| `/v1/decide` | `POST /api/decide` |
| `/v1/explain` | `POST /api/explain`（会话版：`POST /api/explain {sid, ply}`） |
| `/v1/analyze` | `POST /api/analyze`（会话版：`POST /api/analyze {sid, ply}`） |
| `/v1/decode` | `POST /api/decode` |
| `/v1/legal`（申请开启） | `GET /legal?sid=`（会话） |
| `/v1/new_game` `| /play` `| /state`（申请开启） | 由 AI 仓库原生 `server.py` 提供 |

§3.1–3.4 均为**无状态**接口，不会读写对局会话；对局会话接口（`/init`、`/action`、`/act`、`/suggest`）**仅内网可用**，不对外开放。
