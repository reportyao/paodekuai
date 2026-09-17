# 跑得快 AI 策略服务 · 接口文档 v1

> **服务内容**：整局对局托管（服务端发牌、AI 自动应手、你只需出自己的手）+ 单步决策 / 复盘分析 / 牌谱解码接口。
> **内核**：生产模型 `policy_a2c_final56.pt`（A2C 策略网络 + 残局精确求解器 + 规则层），与线上人机对战同一套内核。
> **牌规**：两人跑得快（48 张牌；去大小王、三张 2、一张 A；黑桃 2 为最大单张；有牌必打；三张/三带一仅末手；顺子不含 2；炸弹 3333–KKKK）。

---

## 0. 5 分钟跑通（推荐路径：整局对局）

```python
import requests

KEY  = "在此填入服务方提供的 API Key"          # 形如 pdk_live_xxxx
BASE = "https://chinesetestsite.com/pdk-ai/v1"
H    = {"X-API-Key": KEY, "Content-Type": "application/json"}

# 1) 开新局：服务端发牌，返回你的手牌与全部合法出法
g = requests.post(f"{BASE}/new_game", headers=H, json={"opts": {"red10": True}}, timeout=60).json()
gid = g["gid"]
print("我的牌:", g["my_hand"], "| 要压:", g.get("trick_text") or "（我领出）")

# 2) 循环：你出一手 → 服务端让 AI 连续应手 → 再轮到你
while not g["finished"]:
    legal = g["legal"]                      # 全部合法出法（牌 id 数组的数组），[] 表示过牌
    move  = legal[0]                        # ← 换成你自己的策略
    r = requests.post(f"{BASE}/play", headers=H, json={"gid": gid, "cards": move}, timeout=60)
    if r.status_code in (429, 502, 503):
        continue                            # 限流/抖动：稍等重发**同一请求**即可（幂等，不会重复落子）
    r.raise_for_status()
    g = r.json()                            # 返回时一定又轮到你（或 finished=true）

print("终局：赢家座位", g["winner"], "比分", g["scores"])
```

服务端保证：**每次响应返回时都轮到你**（若 AI 持黑桃 3 先手，它的开局动作已在服务端走完）。

---

## 1. 接入信息

| 项 | 值 |
|---|---|
| 公网基址 | `https://chinesetestsite.com/pdk-ai/v1` |
| 传输 | HTTPS（TLS 1.2/1.3），请求与响应均为 `application/json; charset=utf-8` |
| 鉴权 | 请求头 `X-API-Key: <你的Key>`（也支持 `Authorization: Bearer <你的Key>`） |
| 免鉴权接口 | `GET /v1/health`（可用于探活） |
| CORS | 允许任意来源，浏览器可直接调用 |
| 请求体上限 | 256 KB；`history` ≤ 500 条、`my_hand` ≤ 20 张 |
| 超时 | 服务端到内核 60 秒；nginx 读超时 65 秒 |
| 服务版本 | `GET /v1/health` 的 `pdkCommit.hash` / `productionConfig` 会给出当前内核版本与配置 |
| 数据留存 | 对局过程会被服务端留存（用于服务运行与质量分析），**不会对外提供**；如你的场景要求不留存，请提前告知 |

### 1.1 限流与配额（当前档，可按需调整）

| 限制项 | 当前值 | 超限返回 |
|---|---|---|
| 速率 | 120 次/分钟 | `429` + `Retry-After` |
| 并发 | 1 个进行中请求 | `429` + `Retry-After: 2` |
| 日配额 | 50 000 次/天 | `429` + `Retry-After: 3600` |
| 单 IP 兜底 | 5 次/秒（burst 10） | `429` |

> AI 决策是 CPU 密集型（残局会做枚举求解），**请顺序调用、不要并发轰炸**；需要更高吞吐请与服务方联系。

### 1.2 错误响应

所有错误统一为 `{"error": "说明文字"}`：

| HTTP | 含义 | 处理建议 |
|---|---|---|
| 200 | 成功 | — |
| 400 | 参数缺失/格式错误/牌 id 越界/牌谱不合法 | 修正请求，不要重试 |
| 401 | 缺少或无效 API Key | 检查请求头 |
| 403 | 来源 IP 不在白名单 | 联系服务方 |
| 404 | 未知接口，或 `gid` 不存在/已过期（会话空闲 2 小时回收） | 重新 `/v1/new_game` |
| 409 | 现在不是你的回合 | 先 `/v1/state` 对账 |
| 413 | 请求体或字段过大 | 精简 `history` |
| 429 | 速率/并发/日配额超限 | 按 `Retry-After` 退避重试 |
| 500 | AI 内核异常（服务**不做降级**，绝不返回猜测结果） | 可重试 1–2 次；持续失败请联系服务方 |
| 502 | 服务端到 AI 内核不可达/超时 | 指数退避重试 |
| 503 | 服务繁忙（全局并发已满，排队超时） | 按 `Retry-After` 重试 |

---

## 2. 数据模型（全局约定）

### 2.1 牌 ID

- `id ∈ [0, 48]`，共 **48** 张（**含 48，不含 47**）
- 点数 = `id >> 2`：`0=3, 1=4, 2=5, 3=6, 4=7, 5=8, 6=9, 7=10, 8=J, 9=Q, 10=K, 11=A, 12=2`
- 花色 = `id & 3`：`0=黑桃, 1=红桃, 2=梅花, 3=方片`
- 牌库：3~K 各 4 张（id 0–43）、A 3 张（id 44/45/46）、黑桃 2（**id 48**）；id 47 与 49/50/51 不在牌库中
- 花色只影响显示与红桃 10 翻倍，**策略与合法性只看点数**

```python
rank_of = lambda c: c >> 2          # 0=3 … 11=A, 12=2
RANK_CN = "3456789XJQKA2"           # X = 10
SUIT_CN = "♠♥♣♦"
```

### 2.2 出牌与过牌

- 出牌 = 牌 id 数组，例如 `[36, 37]` 表示"一对 Q"
- 过牌 = 空数组 `[]`（仅当当前无牌可压时合法，见响应字段 `legal_pass`）

### 2.3 牌型元组 `trick`（"台上要压的牌"）

`trick = [ptype, main, len, nc]`；`null` 表示**你是领出方**（自由出牌）。

| ptype | 名称 | 说明 | main | len |
|---|---|---|---|---|
| 0 | 单张 | — | 点数 | 1 |
| 1 | 对子 | — | 点数 | 2 |
| 2 | 连对 | ≥2 连对（7788） | **最大**点数 | 组数（3 对 = 3） |
| 3 | 三张 | 仅末手可出/接 | 点数 | 3 |
| 4 | 三带二 | 555+3+4 | 三张点数 | 5 |
| 5 | 三带一 | 仅末手 | 三张点数 | 4 |
| 6 | 飞机 | 三张连（需变体开关） | 最大点数 | — |
| 7 | 顺子 | ≥5 连张，不含 2 | **最大**点数 | 张数 |
| 8 | 炸弹 | 3333–KKKK | 点数 | 4 |
| 9 | 四带三 | 需变体开关 `four3` | 四张点数 | 7 |

> 两点提示：① 顺子/连对的 `main` 是**最大点数**（顺子 3-4-5-6-7 → `main=4`，即 7）；② `nc` 是内核附加计数（炸弹为 1，其余为 0）。
> **正常情况下你不需要自己构造 `trick`** —— 整局对局接口的每个响应都会带给你当前 `trick` 与 `legal`。

**联调测试向量**（可直接用于对齐实现）：

| 出牌 | 牌 id | trick |
|---|---|---|
| 单张 3 | `[0]` | `[0, 0, 1, 0]` |
| 单张 2 | `[48]` | `[0, 12, 1, 0]` |
| 对子 Q | `[36, 37]` | `[1, 9, 2, 0]` |
| 连对 7788 | `[16,17,20,21]` | `[2, 5, 2, 0]` |
| 三带二 555+3+4 | `[0,4,8,9,10]` | `[4, 2, 5, 0]` |
| 顺子 3-4-5-6-7 | `[0,4,8,12,16]` | `[7, 4, 5, 0]` |
| 炸弹 5555 | `[8,9,10,11]` | `[8, 2, 4, 1]` |

### 2.4 出牌历史 `history`（无状态接口用；整局对局由服务端自己维护）

```json
[
  {"seat": 0, "move": [0]},
  {"seat": 1, "move": [36, 37]},
  {"seat": 0, "move": [], "pass_on": [1, 9, 2, 0]}
]
```

- `seat`：**相对座位**，`0` = 提问方，`1` = 对手
- `move`：牌 id 数组；`[]` = 过牌
- `pass_on`：过牌时被"过"掉的牌型（建议填写，用于对手手牌推断）
- 必须是**从开局第一手开始的完整历史**

### 2.5 规则变体 `opts`

```json
{"red10": true, "four3": false, "nobomb": true, "sanzhang": false}
```

| 键 | 默认 | 含义 |
|---|---|---|
| `red10` | `false` | 红桃 10 翻倍（拿到 ♥10 的一方输赢 ×2） |
| `four3` | `false` | 允许"四带三"（4 张 + 任意 3 张） |
| `nobomb` | `true` | 炸弹不可拆开当其他牌型打出 |
| `sanzhang` | `false` | 三张不可接（三张/三带一仅末手，且不能管三带二） |

> 请让 `opts` 与你实际使用的规则一致，否则 AI 会按错误规则决策；整局对局里同一个 `gid` 的规则在开局时固定。

---

## 3. 接口

### 3.0 `GET /v1/health` — 探活与版本（免 Key）

```json
{
  "ok": true,
  "agent": "prod",
  "productionModel": "ckpt/policy_a2c_final56.pt",
  "modes": ["dual", "hybrid"],
  "netProbe": {"net": "a2c-final56(56x)", "error": null},
  "productionConfig": {"openingSearch": true, "openingWorlds": 16,
                       "totalThreshold": 28, "openingBudget": 1.5},
  "pdkCommit": {"hash": "9cf7a7a", "date": "2026-09-15", "subject": "开局搜索: 时间预算可配置 + 置信度画像"},
  "public": true, "sessions": 0, "maxSessions": 64,
  "endpoints": ["/api/analyze", "/api/decide", "/api/decode", "/api/explain",
                "/api/new_game", "/api/play", "/api/state", "/api/suggest", "/health"]
}
```

> 建议：上线时把 `pdkCommit.hash` 记入你的日志，便于对账"当时用的是哪版模型"。

---

### 3.1 `POST /v1/new_game` — 开新局（整局对局）

**请求**

```json
{"opts": {"red10": true}, "mode": "hybrid"}
```

| 字段 | 必填 | 说明 |
|---|---|---|
| `opts` | ⬜ | 规则变体（见 §2.5），默认 = 生产默认规则 |
| `mode` | ⬜ | `hybrid`（默认，**胜率优先**）/ `dual`（**净分优先**，按得分估值） |

**响应**（字段与后续 `/v1/play`、`/v1/state` 完全一致）

```json
{
  "gid": "81658db5598f4953a866be4a",
  "you_are": 0,
  "ai_seat": 1,
  "turn": 0,
  "finished": false,
  "winner": null,
  "my_hand": [0,1,12,15,20,22,23,32,34,35,37,38,39,40,46,48],
  "my_n": 16,
  "opp_n": 11,
  "trick": [4, 2, 5, 0],
  "trick_text": "三带二5",
  "last_moves": [{"seat": 1, "cards": [8,9,10,0,4]}],
  "legal": [[20,21,22], [8,9,10,0,4], []],
  "legal_pass": false,
  "scores": [0, 0],
  "mode": "hybrid",
  "opts": {"red10": true},
  "moves": 1,
  "new": true
}
```

| 字段 | 含义 |
|---|---|
| `gid` | 本局会话 id（后续所有调用都用它）；**空闲 2 小时回收** |
| `you_are` / `ai_seat` | 你固定坐 `0`，AI 坐 `1` |
| `turn` / `finished` / `winner` | 当前该谁出（返回时恒为 `0`）、是否终局、赢家座位 |
| `my_hand` / `my_n` / `opp_n` | 你的手牌与双方剩余张数 |
| `trick` / `trick_text` | 台上要压的牌型（`null` = 你是领出方） |
| `last_moves` | 最近 6 手（含双方），便于展示 |
| `legal` | **你的全部合法出法**（牌 id 数组的数组）；`[]` 出现时表示可以过牌 |
| `legal_pass` | 是否允许过牌 |
| `scores` | 比分（`finished=true` 时有效；底分 1 分/张、炸弹 10 分/颗、红桃 10 与关门翻倍） |
| `moves` | 本局已进行的手数 |

**耗时参考**：0.3–1.6 秒（若 AI 先手，包含它的开局决策）。

---

### 3.2 `POST /v1/play` — 你出一手，AI 立即应手

**请求**

```json
{"gid": "81658db5598f4953a866be4a", "cards": [20, 21]}
```

- `cards=[]` 表示过牌（仅当 `legal_pass=true` 时合法）
- **响应**：与 §3.1 完全相同的快照；返回时 `turn` 必为 `0`（或 `finished=true`）

**关键语义：幂等续打。** 若上一次调用在 AI 应手中途失败（网络中断、服务瞬时错误），**原样重发同一个请求**即可继续——此时轮次已在 AI 侧，服务只补完 AI 的应手，不会重复落下你那一手。

- 非法出牌 → `400`；不是你的回合 → `409`
- 终局后再调用 → 返回同一份终局快照（`200`）
- **耗时参考**：0.2–2.5 秒（残局枚举求解偏慢）

---

### 3.3 `GET /v1/state?gid=...` — 查询局面（只读）

返回与 §3.1 相同的快照，不会推进对局。适合断线重连、页面刷新、对账。

---

### 3.4 `POST /v1/suggest` — 让 AI 给"当前出牌方"一手建议

可用来做"AI 帮我这一手"，也可以做"AI 托管整局"（把返回的 `cards` 交给 `/v1/play`）。

**请求**：`{"gid": "81658db5...", "explain": true}`

```json
{
  "cards": [36, 37],
  "pass": false,
  "seat": 0,
  "mode": "hybrid",
  "legal_count": 33,
  "explain": {
    "path": "pimc_c",
    "text": "对子QQ",
    "reason": "在 32 个采样世界里求解, 对子QQ 的胜率最高 (62%)。 次优: 对子KK 55%",
    "cands": [{"move": 268435456, "text": "对子QQ", "win_prob": 0.62},
              {"move": 335544320, "text": "对子KK", "win_prob": 0.55}]
  }
}
```

- `cards=[]` + `pass=true` 表示建议过牌（此时 `legal_pass` 必为 `true`）
- 本局已结束时返回 `{"cards": null, "reason": "本局已结束"}`

---

### 3.5 `POST /v1/decide` — 无状态单步决策（自己实现牌局时用）

给一个局面快照，返回 AI 的选择；`explain=true` 时附中文依据。

**请求**

```json
{
  "my_hand": [0,4,8,12,16,20,24,28,32,36,40,44,45,46,48,5],
  "opp_n": 16,
  "trick": null,
  "history": [],
  "opts": {"red10": true},
  "explain": true,
  "mode": "hybrid"
}
```

**响应**

```json
{
  "move": [4, 8, 12, 16, 20, 24, 28, 32, 36, 40],
  "pass": false,
  "legal_count": 1080,
  "mode": "hybrid",
  "engine": "c",
  "openingSearch": true,
  "explain": {
    "path": "opening_search",
    "text": "顺子 4+5+6+7+8+9+X+J+Q+K",
    "reason": "在 16 个采样世界里求解, 顺子 4+5+6+7+8+9+X+J+Q+K 的胜率最高 (100%)。…",
    "cands": [{"move": 123, "text": "…", "win_prob": 1.0}],
    "belief": {"source": "读牌网络", "facts": ["对手高置信至少 2 张A"]},
    "worlds": 16
  }
}
```

`explain.path` 取值与含义：

| path | 含义 |
|---|---|
| `opening_search` | 开局搜索（多世界 rollout） |
| `pimc_c` / `pimc_cn` | 残局精确求解（定胜负 / 按净分） |
| `endgame_order` | 残局连续保权 |
| `report_dump` | 对手报单时保出牌权 |
| `one_shot` | 一手直接走完 |
| `lookahead` | 前瞻搜索（价值网 2-ply） |
| `fallback_net` | 策略网络直接估值（局面较大、未启用残局求解） |
| `forced` | 只有唯一合法手 |

---

### 3.6 `POST /v1/explain` — 解释整局里的某一手（复盘用）

```json
请求 {"initial_hands": [[16张],[16张]], "first_player": 0, "ai_seat": 1,
      "opts": {"red10": true}, "moves": [code 或 牌id数组...], "ply": 7}
响应 {"ply": 7, "seat": 1, "text": "三带二 6+7+8+JJJJ", "path": "pimc_c",
      "reason": "在 32 个采样世界里求解, …胜率最高 (62%)。",
      "cands": [...], "state_text": "我剩 16 张, 对手剩 16 张; 我是领出方",
      "recorded": "三带二 6+7+8+JJJJ", "decided": "三带二 6+7+8+JJJJ",
      "anchored_back": false, "note": "…"}
```

- `ply` 从 1 开始；不传 = 最后一手；若指向的不是 AI 决策点，会自动回退到 AI 最近一次决策并置 `anchored_back: true`
- 该接口会**让 AI 重算它自己的每一手**，`decided`（重算）可能与 `recorded`（牌谱）不同（残局搜索带世界采样，属正常波动）

### 3.7 `POST /v1/analyze` — 单步分析（真实局面下 AI 会怎么打）

给一手（AI 手或人类手皆可），返回"按真实牌谱的局面，AI 会怎么打 + 为什么"。

```json
请求 {"initial_hands": [[16张],[16张]], "first_player": 0, "opts": {},
      "moves": [[0], [36,37], []], "ply": 3}
响应 {"ply": 3, "seat": 0, "state_text": "我剩 15 张, 对手剩 15 张; 台上: 单张6",
      "recorded": {"cards": [18], "patText": "单张7", "pass": false},
      "decided":  {"cards": [20], "patText": "单张8", "pass": false},
      "agree": false,
      "explain": {"path": "fallback_net", "reason": "…", "cands": []},
      "note": "此处双方合计 30 张，超过 28 张阈值：未启用残局精确求解…"}
```

`agree=false` 的点即"AI 认为有更好选择"的位置，配合 `explain.reason` 可直接产出复盘结论。

### 3.8 `POST /v1/decode` — 牌谱解码

把动作码序列还原成"每一手谁出的、具体哪些牌、牌型、出后剩几张"。座位由内核判定（过牌后同一人继续领出，不能按奇偶交替推断）。

```json
请求 {"initial_hands": [[16张],[16张]], "first_player": 0, "opts": {}, "moves": [1, 268435456, 0]}
响应 {"moves": [
  {"ply": 1, "seat": 0, "cards": [0], "pass": false, "pass_on": null,
   "combo": {"ptype":0,"main":0,"len":1,"nc":0}, "handAfter": 15, "patText": "单张3"},
  {"ply": 2, "seat": 1, "cards": [36,37], "pass": false, "pass_on": null,
   "combo": {"ptype":1,"main":9,"len":2,"nc":0}, "handAfter": 15, "patText": "对子Q"},
  {"ply": 3, "seat": 0, "cards": [], "pass": true, "pass_on": [1,9,2,0],
   "combo": null, "handAfter": 15, "patText": "不出"}
]}
```

### 3.9 `POST /v1/belief` — 记牌猜牌快照（AI 对对手手牌的推断，只读）

请求体与 §3.5 `/v1/decide` **完全一致**（`my_hand` / `opp_n` / `trick` / `history`）。
不产生决策、无副作用，用于在你自己的界面上展示"AI 认为对手手里大概是什么 / 为什么敢这么领出"。

```json
请求 {"my_hand": [2,13,19,21,24,27,29,39,42,44], "opp_n": 3,
      "trick": [0,2,1,0], "history": [{"seat":0,"move":[1,3,4,5]}, ...]}
响应 {
  "opp_n": 3, "my_n": 10,
  "worlds": 313,              // 与公开信息一致的可能手牌（按点数构成的向量）数量
  "exhaustive": true,         // true=全量枚举（残局可严格认证）；false=采样
  "weighted": true,           // 是否启用对手建模加权（算牌规则）
  "sharpness": 0.0255,        // top-1 权重占比：越高信念越尖（接近 0 = 信息不足）
  "ledger": {"3":1,"5":2,"6":3,"K":3,"A":2,"2":1},   // 记牌台账：各点数"未露面"张数
  "rank_prob": {"K":0.31,"A":0.12},                  // P(对手持有 >=1 张该点数)
  "rank_exp": {"K":0.33,"A":0.12},                   // 该点数期望张数
  "rank_cnt_p": {"K":{"0":0.69,"1":0.21,"2":0.10}},  // 张数分布 P(恰有 k 张), k=0..4（份额<0.5% 的档不列）
  "opp_most_likely": {"K":2,"8":1},                  // 最可能的一手牌（点数->张数，= top_hands[0].ranks）
  "top_hands": [{"cards":"44","ranks":{"4":2},"p":0.0198}],   // 最可能的若干构成
  "facts": [{"kind":"lead_rule1","text":"对手领对9: 通常没有对8/对X (实测命中 91%)"}],
  "lock": {"vs_trick": {"p_beat":0.0,"certified_locked":true},
           "leads": [{"cards":"K","ptype":0}]},
  "incomplete_snapshot": false
}
```

字段与用法：
- `ledger` 是"记牌"：`总量 − 我的当前手牌 − 双方已出`，可直接做成台账 UI。
- `rank_prob`（有没有）/ `rank_cnt_p`（**各几张**）/ `rank_exp`（期望张数）/ `opp_most_likely`（最可能的一手牌）/ `top_hands`（前 N 种构成）是"猜牌"四件套：
  `rank_prob` 只回答"是否持有"，`rank_cnt_p` 才回答"到底几张"（`{"K":{"0":0.69,"1":0.21,"2":0.10}}` 读作：69% 没有 K、21% 恰好 1 张、10% 恰好 2 张）；`opp_most_likely` 是最可能那一手的点数→张数。
- `facts` 是**可解释推断链**，每条带实测命中率（`lead_rule1/2/3`=领出规则、`pass`=过牌硬约束、`gap`=断7/断10、`tell`=三带暗示被打破）。`kind` 便于分类展示。
- `lock.leads` 是**锁牌认证**：这些领出在全部可能手牌上严格枚举后"对手必压不住"（保牌权）；`certified_locked` 需要 `exhaustive=true` 才有严格含义。
- `worlds` 很大（中盘 10^5~10^6）说明该时刻信息不足，`sharpness` 低属正常，不要当成故障。
- 延迟参考：实测开场 ~0.3s、中盘 ~0.8s、残局 ~0.25s；建议 UI 上加 1 秒内的 loading 态。

---

---

## 4. 最佳实践

1. **顺序调用**：并发上限 1，请勿对同一个 `gid` 并发调用；需要多局并行请与服务方沟通配额。
2. **重试策略**：`429` 按 `Retry-After` 退避；`502/503` 指数退避（1s/2s/4s）并**原样重发同一请求**（`/v1/play` 幂等）；`400/401/403/404/409` 不要重试（先修正）。
3. **会话过期**：`gid` 空闲 2 小时回收，过期返回 `404`，重新 `/v1/new_game` 即可；长期展示建议自行保存 `last_moves`。
4. **决策带随机性**：残局 PIMC 与开局搜索含世界采样，同一局面多次调用可能给出不同但同样合理的选择（设计特性，用于覆盖不确定性）。
5. **保留版本信息**：把 `/v1/health` 的 `pdkCommit.hash` 记入日志；内核版本更新会滚动生效，`/v1/*` 契约向后兼容。
6. **不要高频轮询** `/v1/state`：整局对局的每次 `/v1/play` 已返回完整快照，`/v1/state` 只用于断线恢复。
7. **上线自检**：`GET /v1/health` 应返回 `netProbe.net = "a2c-final56(56x)"`；`POST /v1/selftest` 会跑金丝雀（7 条：决策/顶牌/必压/错误契约 + 新字段契约/推理链/合理枚举闸门），返回 `cases[]`（每条含 `name/ok/ms/check|err`）与 `ok` 总判，可直接接进你们的 CI；`GET /v1/health` 的 `components` 是 6 个组件探针（C 核心/网络/智能体/推理引擎/牌型地图/枚举闸门），`prod_agent.kw` 会列出**当前生效的生产配方**（如 `opening_seeds`/`bomb_only_when_forced`），便于核对版本；用 §2.3 的测试向量核对你的牌型实现。

---

## 5. 版本与兼容

- 路径版本 `/v1`；破坏性变更会另开 `/v2`，`v1` 至少保留 3 个月。
- 响应**只增字段不删字段**，请忽略未知字段。
- 决策行为会随内核版本迭代（这是"始终用最新最强模型"的代价）；需要冻结版本请与服务方约定。

---

## 附录 A：规则要点（服务假设的规则）

1. 牌库 48 张 = 去掉大小王、三张 2（♥2 ♣2 ♦2）、一张 A；**黑桃 2 保留且为全场最大单张**
2. 发牌：随机弃 16 张底牌，双方各 16 张；持黑桃 3 者先出（在底牌则随机）
3. 牌型：单张、对子、连对（≥2 对）、三张、三带二、三带一、飞机、顺子（≥5 张且不含 2）、炸弹（4 张同点）、四带三（变体）
4. **有牌必打**：能压必须压；确实压不上时才可过牌
5. 炸弹可压任意牌型；炸弹之间比点数；炸弹不可拆（`nombomb`）
6. 三张 / 三带一仅最后一手（`sanzhang` 变体下更严：不能管三带二）
7. 计分：底分 1 分/张，按输家剩余张数计；对家一张未出（关门）翻倍；炸弹每颗 10 分；红桃 10 翻倍（可选）
8. 顺子中不能出现 2；A 只作大牌

## 附录 B：curl 示例

```bash
KEY="pdk_live_xxxx"
BASE="https://chinesetestsite.com/pdk-ai/v1"

# 探活（免 Key）
curl -s "$BASE/health" | python3 -m json.tool

# 开新局
curl -s -X POST "$BASE/new_game" -H "X-API-Key: $KEY" -H 'Content-Type: application/json' \
     -d '{"opts":{"red10":true}}' | python3 -m json.tool

# 出一手（把 <GID>/<CARDS> 换成上一步返回的值）
curl -s -X POST "$BASE/play" -H "X-API-Key: $KEY" -H 'Content-Type: application/json' \
     -d '{"gid":"<GID>","cards":[20,21]}' | python3 -m json.tool

# 单步决策 + 解释
curl -s -X POST "$BASE/decide" -H "X-API-Key: $KEY" -H 'Content-Type: application/json' \
     -d '{"my_hand":[0,4,8,12,16,20,24,28,32,36,40,44,45,46,48,5],"opp_n":16,"trick":null,"history":[],"explain":true}' \
     | python3 -m json.tool
```

## 附录 C：联调清单

| # | 事项 | 说明 |
|---|---|---|
| 1 | 拿到 API Key | 服务方单独提供；请勿提交到公开仓库/前端代码 |
| 2 | 出口 IP | 若你的服务器出口 IP 固定，告知服务方加入白名单（可显著降低 Key 泄露风险） |
| 3 | 预计调用量 | 整局对局一局约 20–40 次调用；若需提高速率/并发/日配额，提前沟通 |
| 4 | 联调验收 | 用 §0 的示例跑通一局；用 §2.3 测试向量核对牌型；`/v1/health` 确认 `netProbe.net` |
| 5 | 异常路径 | 验证 `401`（无 Key）、`404`（过期 gid）、`429`（超限）与重试逻辑 |
| 6 | 问题反馈 | 提供 `gid`、时间点、请求与响应原文，便于服务方在审计日志中定位 |

---

**服务方联系方式**：<在此填写你的联系人 / 邮箱 / IM>（Key 与配额调整也走这里）
