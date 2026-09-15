# 跑得快 AI · 对外开放方案（v1）

> **实施状态（2026-09-15）：已按第 4 节顺序落地并验证通过。**
>
> | 项 | 实际状态 |
> |---|---|
> | AI 内核 | `pdk-ai` 已升级到 `9cf7a7a`（开局搜索时间预算可配置 + 置信度画像）；四项冒烟（规则/C 对拍 17468 例/残局）全过，资产 md5 全部 OK |
> | 网页版实例 | `paodekuai-ai.service`（127.0.0.1:8766，含会话协议），重启后自动恢复进行中对局（实测 13 局） |
> | 对外实例 | `paodekuai-api.service`（127.0.0.1:8776，`--public-api`：只开 8 个接口 + health；`CPUWeight=100`、`MemoryMax=1G`、会话≤64、空闲 2h 回收、决策排队超时 30s） |
> | 网关 | `pdkai-gateway.service`（127.0.0.1:8770）：API Key 鉴权、按 Key 速率/并发/日配额、可选 IP 白名单、审计日志 `/var/log/paodekuai-api.log`（不含明文 Key） |
> | 公网入口 | `https://chinesetestsite.com/pdk-ai/v1/*`（nginx `location /pdk-ai/` + `limit_req` 5r/s 兜底；用现有证书，无需新 DNS） |
> | API Key | `/etc/paodekuai-api/keys.json`（600 权限）：`外部调用方A` → 120 次/分、并发 1、5 万次/天、`allowed_ips: []` |
> | 数据隔离 | 对外牌局落盘在 `data/external/`（主站 `data/replays/` 与人工点评不受影响；外部实例不加载任何主站会话） |
> | 回滚 | 注释 nginx `location /pdk-ai/` → `nginx -t && systemctl reload nginx`；或 `sudo systemctl stop paodekuai-api pdkai-gateway` |
> | 待办 | ① 调用方出口 IP 固定的话把 IP 填进 `allowed_ips`（改完自动热加载，无需重启）；② 若对方要多并发/更高配额，再评估独立节点 |
>
> 另已顺手加固主站入口（第 4 节步骤 6）：`server.py` 的 `/ai/*` 代理加了**按 IP 限流**
> （默认 300 次/分钟/IP，实测真人打牌峰值 42 次/分钟，7 倍余量；静态资源不受影响；
> 环境变量 `PDK_AI_RATE=0` 可关闭）。因为 8310 是 server.py 直接监听（不经 nginx），
> 这一层限流放在应用内实现。
>
> **对外对局的记录与胜负统计（已实现）**
>
> - 编号独立成段：对外实例写 `E0001…`（主站人机 `A` / 在线 `H`），互不撞号
> - 归属可查：网关按 API Key 注入调用方名（`X-PDK-Caller`，nginx 层清空客户端同名头防伪造），
>   落盘字段：`caller`（调用方名）、`api: true`、`startedTs/endedTs`、`durationSec`
> - 记录内容：双方初始手牌、底牌、逐手出牌（含具体牌面/牌型）、`winner`、`scores`、规则 `opts`
> - **网页版查看**：大厅 →「📚 对局记录」→ 切到「对外调用（E）」，顶部显示汇总
>   （局数 / 完成 / 调用方胜-AI胜 / 平均手数 / 平均用时 / 按调用方分组），点任意一局可进逐手复盘
> - **接口/命令行**（便于接你自己的后台或做定时统计）：
>   ```bash
>   curl -s 'http://127.0.0.1:8310/replays/stats?scope=api'   # 对外：summary + by_caller + by_day + recent
>   curl -s 'http://127.0.0.1:8310/replays/stats?scope=mine'  # 你自己的对局
>   curl -s 'http://127.0.0.1:8310/replays/list?scope=api'    # 对外对局列表（含胜负/用时）
>   cd /home/ubuntu/paodekuai && .venv/bin/python replay_report.py stats api   # 命令行文本报告
>   ```
>   字段口径：`seat0` = 调用方，`seat1` = AI；`seat0_wins` / `seat1_wins` / `avg_moves` / `avg_duration` 均为完成局口径。
>
> 三套自测（服务器上可直接跑，全绿）：
> ```bash
> cd /home/ubuntu/paodekuai
> .venv/bin/python selftest_api.py                     # 59 项：整局对局 + 无状态接口 + 负例 + 网页版会话链路
> PDK_BOT_ROOT=/home/ubuntu/pdk-ai-prod .venv/bin/python selftest_core.py   # 21 项：决策内核等价性/续打/计分/防穿越
> PDK_GW_KEY="$(python3 -c "import json;print(list(json.load(open('/etc/paodekuai-api/keys.json'))['keys'])[0])")" >   PDK_GW_LOG=/var/log/paodekuai-api.log .venv/bin/python selftest_gateway.py   # 18 项：鉴权/配额/路由/审计
> ```
>
> 常用运维命令：
> ```bash
> sudo systemctl status paodekuai-api pdkai-gateway
> sudo cat /etc/paodekuai-api/keys.json          # 取/改 Key（改完网关自动热加载）
> tail -f /var/log/paodekuai-api.log             # 审计：时间/Key名/接口/状态/耗时
> curl -s https://chinesetestsite.com/pdk-ai/v1/health | python3 -m json.tool
> ```

---
面向"把 AI 提供给他人调用"的落地方案：现状核查 → 开放架构 → 安全与配额 → 部署步骤 → 回滚。

---

## 0. 先回答三个问题

### ① AI 是不是部署在你们腾讯云上？

**是，但只在服务器内部可见。**

| 组件 | 位置 | 监听 | 公网可达 |
|---|---|---|---|
| 生产 AI 内核（模型 `policy_a2c_final56.pt`） | `/home/ubuntu/pdk-ai-prod`（git 仓库，只读部署密钥） | — | — |
| AI 服务进程（`paodekuai-ai.service`） | `/home/ubuntu/paodekuai/ai_bridge.py` | `127.0.0.1:8766` | ❌ 仅本机 |
| 网页版（玩法 + 反代 `/ai/*`） | `/home/ubuntu/paodekuai`（`paodekuai-web.service`） | `0.0.0.0:8310` | ✅ `http://43.128.24.244:8310/` |
| 在线房间服务 | `paodekuai-online.service` | `127.0.0.1:8311` | ❌ 仅本机 |
| 同机其他服务（勿动） | guandan:8300、Next.js:3000、nginx:80/443 | — | 各自现状 |

生产资产校验（均已通过）：
`ckpt/policy_a2c_final56.pt` = `534dcca82ee60760a1a40c546a83e5f1`；`c/pdk_core.c` = `aefb96685e0dc8a164c60bc00388919a`；`c/pdk_core.so` 已在服务器编译。

### ② 是不是 GitHub 里最新的？

**不是最新，落后 2 个提交。** 线上 `pdk-ai-prod` 在 `d6162fa`，私有仓库 `reportyao/pdk-ai` 的 `main` 已到 `9cf7a7a`：

| 提交 | 内容 | 影响 |
|---|---|---|
| `9cf7a7a` | 开局搜索：**时间预算可配置**（`opening_budget`，默认 1.5 s）+ 置信度画像 + 修 harness 两个 bug | `pdk/agents.py`（+48 行）；**决策行为微调**，长尾延迟被时间预算兜住 |
| `d3f56b8` | 人类行为拟合定位真实差距 + 结构抑制偏置实验（**默认关闭**，实测中性） | 新增分析脚本；不改线上行为 |

**接口契约未变**（`server.py` / `pdk/explain.py` / `pdk/fast.py` 无改动），所以对外文档对两个版本都成立。
**建议开放前先升级**（你的既有原则是"只用最强最新版"，且时间预算对公开 API 的尾延迟有利）：

```bash
# 服务器上
cd /home/ubuntu/pdk-ai-prod && git log --oneline -1        # 记录当前提交，便于回退
bash /home/ubuntu/paodekuai/update_ai.sh                   # 已有的一键更新脚本（pull + 重启 AI 服务）
curl -s localhost:8766/health | python3 -m json.tool | head -20   # 确认 netProbe.net = a2c-final56(56x)
```
升级后用真实牌谱回归一遍（`/api/analyze` 对同一批历史局抽样，比对 `agree` 率与 `path` 分布），确认无异常再对外发 Key。

### ③ 要不要做 IP 白名单？

**建议做，但必须和 API Key 一起用——白名单是"第二道锁"，不是主锁。**

| 手段 | 作用 | 局限 |
|---|---|---|
| **API Key（主）** | 身份识别 + 配额（每分钟/每天/并发）+ 可即时吊销 + 可审计到人 | Key 可能被转发/泄露 |
| **IP 白名单（第二道）** | 即使 Key 泄露，非白名单来源也用不了；零运行时开销 | 调用方出口 IP 常不固定（云函数、移动网络、企业 NAT 共享）：写死会误伤或失效 |
| nginx `limit_req`（兜底） | 拦截扫描/刷量，保护 2 vCPU 的机器 | 只能按 IP 维度 |

**推荐组合**：

- 调用方是**固定出口 IP 的服务器**（最常见）→ Key + IP 白名单**都开**，白名单加在腾讯云安全组**和** nginx 两层；
- 调用方 IP 不固定（浏览器/移动端/云函数）→ 只开 Key + 严格配额，白名单留空（网关支持按 Key 配置 `allowed_ips`，随时可加）；
- 无论哪种，**配额都是硬闸**：这台机器 2 vCPU，一次残局决策就吃掉一个核 0.5–2.5 秒，不设配额会被一个调用方打满、连带影响你自己的对局。

---

## 1. 目标架构

```
调用方 ──HTTPS──▶ nginx(443)  location /pdk-ai/  ──▶ 网关 127.0.0.1:8770 ──▶ AI 实例B 127.0.0.1:8776
                 chinesetestsite.com                 · 校验 X-API-Key        （--public-api：仅无状态接口）
                 · limit_req 兜底                     · 按 Key 配额/并发             │
                 · 可选 allow/deny IP                 · 审计日志                     │
                                                     · 队列/超时                    │
你的网页版 ──▶ 8310 server.py ──/ai/*──▶ AI 实例A 127.0.0.1:8766（现有，仅内网）
（对局不受外部流量影响）                        · 含会话接口(/init /act /action /suggest)
```

**为什么要独立实例（B）而不是共用 8766**：

1. **隔离**：外部突发不会排队到你自己的对局决策后面（决策是 CPU 密集）；
2. **攻击面**：实例 B 用 `--public-api` 启动，**只开无状态接口**，连会话接口都不注册 → 外部调用方无论如何都碰不到你的对局会话（sid 劫持风险归零）；
3. **可治理**：B 可以单独设 CPU 权重（`CPUWeight=100`，你的对局实例是 1000）、内存上限、并发闸、单独重启；
4. **成本**：一个实例常驻约 **500 MB RSS**（实测 522 MB）；本机空闲内存约 2.1 GB，可行，建议 `MemoryMax=1G` 兜底。

对外只开放 4 个**无状态**接口 + 健康检查：

| 接口 | 用途 |
|---|---|
| `POST /v1/decide` | 决策一手（核心），可带中文解释 |
| `POST /v1/explain` | 解释某一手（整局 + ply） |
| `POST /v1/analyze` | 单步分析（真实局面下 AI 会怎么打，人类手也支持） |
| `POST /v1/decode` | 动作码 → 具体牌面/牌型/归属（牌谱工具） |
| `GET /v1/health` | 版本与健康 |

**不开放**（保持仅内网）：`/init`、`/action`、`/act`、`/suggest`、`/legal`（会话类）、`/replays/*`（**你的真实对局数据**）、`/api/admin/retrain`。
若调用方需要"和 AI 打整局"，再单独开第二节点的上游整局 API（`new_game/play/state`），独立数据目录、独立会话上限。

---

## 2. 需要的代码改动（都很小）

1. **`ai_bridge.py` 增加 `--public-api`**：该模式下
   - 只注册 `/health`、`/api/decide`、`/api/explain`、`/api/analyze`、`/api/decode`；
   - 会话类路由一律返回 `404 {"error":"not available on public instance"}`；
   - 启动时不恢复历史对局会话（不读 `data/replays`）；
   - `_ACT_SEM = 1`（外部并发 1），`MAX_SESSIONS = 0`。
2. **新增 `api_gateway.py`（约 150 行，无第三方依赖）**
   - 反向代理到 `127.0.0.1:8776`，路径 `/v1/<name>` → `/api/<name>`（`/v1/health` → `/health`）；
   - 鉴权：`X-API-Key`（或 `Authorization: Bearer`），从 `/etc/paodekuai-api/keys.json`（600 权限，不进仓库）读 `{key: {name, per_min, per_day, concurrent, allowed_ips}}`；
   - 配额：滑动窗口（分钟/天）+ 并发计数，超限 `429` + `Retry-After`；IP 不在 `allowed_ips` → `403`；
   - 请求体上限 256 KB；上游超时 60 s；单请求头/日志只记 `key.name`，**不记明文 Key**；
   - 审计：`/var/log/paodekuai-api.log`（`ts key name endpoint status ms upstream_ms ip`）。
3. **systemd**：新增 `paodekuai-api.service`（实例 B）与 `pdkai-gateway.service`（网关），互不依赖，可单独重启。
4. **nginx**：在现有 443 块（`/etc/nginx/sites-enabled/hsk-report`）**追加**一段 location（不改动其他 location）：

```nginx
# 外部 AI API（只暴露 /pdk-ai/ 前缀；其余站点配置保持不变）
limit_req_zone $binary_remote_addr zone=pdkai:10m rate=5r/s;   # 放 http{} 层

location /pdk-ai/ {
    limit_req zone=pdkai burst=10 nodelay;
    # 可选 IP 白名单（按调用方固定出口 IP 填，先留空注释）
    # allow 1.2.3.4; deny all;
    client_max_body_size 256k;
    proxy_pass http://127.0.0.1:8770/v1/;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_read_timeout 65s;
}
```

> 追加前 `cp /etc/nginx/sites-enabled/hsk-report{,.bak}`，改完 `sudo nginx -t && sudo systemctl reload nginx`。
> 好处：用现有域名与证书（`chinesetestsite.com`，Let's Encrypt，443 已验证），**不需要新 DNS 记录、不需要新证书**。
> 若你希望完全独立（例如 `pdkai.chinesetestsite.com`），需要加一条 DNS 记录 + 给证书补 `-d` 域名，可另做。

---

## 3. 配额建议（默认档）

| 维度 | 默认 | 备注 |
|---|---|---|
| 单 Key 速率 | 60 次/分钟 | 覆盖"逐手决策"的正常节奏（一局约 20–40 手） |
| 单 Key 并发 | 1 | 决策是 CPU 密集，别让外部并发挤占 |
| 单 Key 日配额 | 20 000 次/天 | 约合 500–1000 局牌谱分析 |
| 全局并发 | 1（实例 B `_ACT_SEM=1`） | 超出排队，排满返回 `503` |
| 单 IP 速率（nginx） | 5 次/秒，burst 10 | 防扫描/刷量 |
| 请求体 | ≤ 256 KB，`history` ≤ 200 手 | 正常牌谱 < 5 KB |

**容量口径**：一次中盘决策 0.2–0.8 s，残局 PIMC 0.5–2.5 s（`opening_search` 受 1.5 s 时间预算约束）。单并发下 ≈ **1–3 次/秒**，即单 Key 60 次/分钟是舒适区；若调用方要更高吞吐，需要给它独立实例/独立核（本机只有 2 核，同时还要跑你自己的对局与其它三个服务，因此**不建议**在共享机上放开全局并发）。

---

## 4. 部署步骤（按序执行，每步可验证）

| # | 步骤 | 验证 | 影响面 |
|---|---|---|---|
| 1 | 升级 AI 到 `9cf7a7a`（`update_ai.sh`） | `/health` 的 `netProbe.net` 与 `productionConfig`、抽样回归 `agree` 率 | 你的对局实例短暂重启（进行中会话自动恢复） |
| 2 | 加 `--public-api` 支持并部署**实例 B**（8776） | `curl -s 127.0.0.1:8776/api/decide -d @sample.json` 与 `127.0.0.1:8776/init` 应返回 `404` | 新增进程，不影响现有服务 |
| 3 | 部署**网关**（8770）+ `keys.json` | `curl -H "X-API-Key: test" 127.0.0.1:8770/v1/health` | 仅本机 |
| 4 | nginx 追加 `location /pdk-ai/`（先只对白名单放开） | `curl https://chinesetestsite.com/pdk-ai/v1/health -H "X-API-Key: test"` | 只新增 location；`nginx -t` 通过后 reload |
| 5 | 冒烟 + 配额验证 + 压测（100 次连续决策，核对 `429/503` 行为与延迟分布） | 见下表验收清单 | — |
| 6 | **顺手加固**：给 8310 的 `/ai/*` 加 nginx 限流（现状：公网任何人可无鉴权驱动你的 AI） | `ab`/`curl` 连打应出现 `429` | 只影响滥用流量，正常对局无感 |
| 7 | 给调用方发 Key + 《AI_API.md》，约定配额与联系方式 | — | — |

**验收清单**

- `GET /v1/health` 返回 `netProbe.net = a2c-final56(56x)`
- `/v1/decide` 用文档里的测试向量（单张3/对子Q/顺子/炸弹）返回结果与 `trick` 一致
- 无 Key → `401`；错 Key → `401`；非白名单 IP → `403`；连打 → `429`；停掉实例 B → `503`（**不是**静默降级）
- 你自己的对局（8310）在外部压测期间延迟无可见抖动（因为实例 B 独立、CPUWeight=100）
- `data/replays`、`/replays/*` 从公网访问仍是**不可达/不暴露**

---

## 5. 安全清单（上线前逐条确认）

1. `/v1/*` 之外公网无新增入口；实例 B **只 bind 127.0.0.1**（绝不 0.0.0.0）；
2. Key 文件 600 权限、不在 git 仓库、日志不落明文 Key；
3. 网关**拒绝**转发任何会话类路径（白名单式转发，而非黑名单）；
4. 你的人工点评/牌谱（`data/`、`/replays/*`）不进入任何对外响应；
5. 请求体/字段长度上限（防超大 `history` 打内存）；
6. nginx `limit_req` + 网关按 Key 限流双层；
7. 出错**不降级**（AI 异常一律 5xx 报错，绝不用旧网络或贪心兜底给你或调用方）；
8. 滥用熔断：单 Key 连续超限自动暂停并在响应里给出原因，恢复需人工确认；
9. 备份：改动前 `cp` 原文件到 `_bak/`，nginx 改动 `nginx -t` 必须通过。

---

## 6. 回滚

- **只回滚对外入口**（最快）：注释 nginx `location /pdk-ai/` → `nginx -t && systemctl reload nginx`（秒级，外部立即不可达，内部一切照旧）；
- **回滚实例**：`systemctl stop paodekuai-api pdkai-gateway`（外部 502/503，你的对局不受影响）；
- **回滚 AI 版本**：`cd /home/ubuntu/pdk-ai-prod && git checkout <升级前提交> && systemctl restart paodekuai-ai`；
- **吊销单个调用方**：从 `keys.json` 删除该 Key（网关热加载，或 restart 网关）。

---

## 7. 待你确认的三件事

1. **调用方是谁、几个、出口 IP 是否固定**（决定白名单怎么配、配额给多少）；
2. **是否需要"整局对局"接口**（`new_game/play/state`）——需要就再加一个节点与数据目录，会明显增加会话内存占用；
3. **升级 AI 到 `9cf7a7a` 的时机**（建议开放前，且先用你自己的历史牌谱回归确认无异常）。

确认后我按第 4 节顺序实施，并把《AI_API.md》一并交付给调用方。
