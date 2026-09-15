# 跑得快 · 两人对战（网页版）

两人跑得快的本地网页游戏，风格与 [掼蛋·在线对战](http://43.128.24.244/) 一致。纯前端实现（无后端逻辑），`server.py` 只负责静态托管。

> **原创声明**：本项目为原创实现（代码/样式/逻辑手写），仅供个人本地学习研究。
> 规则采用通用两人跑得快打法；与任何商业棋牌 App 的内部代码无关。

## 快速开始

```bash
cd paodekuai
python server.py          # 默认 8310 端口，可改：python server.py 8080
```

或直接双击 `run.bat`，浏览器打开 <http://127.0.0.1:8310>。
（也可以直接双击 index.html 用 file:// 打开，功能完全一致。）

## 🤖 AI 机器人桥接（深度模型，可选）

接入私有仓库 [reportyao/pdk-ai](https://github.com/reportyao/pdk-ai) 的**生产版**跑得快 AI（`pdk-ai-prod/` 本地副本，同源于 pdk_ai_work 工作副本），实现 README「生产配置定版」的**双模式**：

| 模式 | 构造 | 定位 |
|---|---|---|
| `hybrid` | `SolverAgent(engine='c')` + DMC-v1(qnet.pt) fallback | 胜率优先（生产配置，布尔定胜负） |
| `dual` | `SolverAgent(engine='dual')` 同上 | 积分制净分优先（≤14张数值计分接力） |

大厅「AI 打法」下拉可切换，**同时作用于人机对战的 AI 对手、以及所有模式的出牌提示**：

| 选项 | 含义 | 适用 |
|---|---|---|
| `胜率优先 · hybrid`（默认） | 残局按**胜/负**求解（P(胜) 最大） | 常规对局，pdk-ai 生产配置 |
| `净分优先 · dual` | ≤14 张残局改用**数值计分**求解（E[得分] 最大：少输分/多赢分） | 计分制、想压净分时 |

两者**共用同一生产网络**（`ckpt/policy_a2c_final56.pt` 作为 fallback），差异在残局求解目标；实测 40 局中 42% 的对局两种打法在残局出现不同决策。AI 座位标签与提示条都会标注当前打法（如 `AI·胜率优先`、`建议[深度模型·净分优先]`）。

```bash
python ai_bridge.py       # 默认 :8766；--bot-root 指定 pdk-ai 生产仓库路径
```

或双击 `start_all.bat` 一键启动网页 + 桥。行为要点：

- **人机对战**：桥就绪时电脑由生产深度模型驱动；桥未启动或中途退出，自动无缝降级为内置贪心 AI（「AI·内置」），对局不中断。
- **出牌提示**：优先走生产模型（含"建议不出"），残局求解偶发较慢时会先显示"深度模型思考中…"（45s 超时后回退内置 AI）。
- 原理是「影子牌局」：每局把初始发牌（16+16+16，含扣底）/规则/先手/模式同步给桥，双方落子实时镜像，桥内由 pdk 引擎决策；任何失步自动带完整动作历史重建会话；AI 行动会等待桥初始化完成，消除先手竞态。
- 牌 id 映射与 pdk_ai 全局约定一致（`id>>2` 点数、`id&3` 花色；A 槽位 44/45/46、黑桃2 槽位 48）。
- 桥接口（JSON，带 CORS）：`POST /init`（含 `mode: hybrid|dual`）、`POST /action`、`POST /act`、`POST /suggest`、`GET /legal`、`GET /health`（返回 `modes` 与 `botRoot`）。
- 已验证：双模式自博弈完整局、hybrid vs dual 进程内对打（行为差异明显、胜负与净分曲线不同）、浏览器双模式端到端与提示来源压测。

### 生产模型（pdk-ai README「当前生产模型与部署清单」）

| 件 | 值 |
|---|---|
| 生产模型 | `ckpt/policy_a2c_final56.pt`（A2C + 56 维动作后特征）md5 `534dcca82ee60760a1a40c546a83e5f1` |
| 推理配置 | `SolverAgent(hybrid, total_threshold=28)` + `_QFB`(final56) + 规则层 R0-R3 |
| C 核心 | `c/pdk_core.so`（Linux，由 `pdk_core.c` 编译，源码 md5 `aefb96685e0dc8a164c60bc00388919a`） |
| 可选回退 | `ckpt/qnet.pt`（旧 DMC）md5 `4be2824a0f47c98be6fa8c80276d0777` |

桥的 `/health` 会回报实际加载的网络与四件套 md5 校验结果，可据此确认「接的是不是 final56」：

```bash
curl -s http://127.0.0.1:8766/health | python3 -m json.tool   # 看 productionModel / assets
```

已按 README 验证步骤复核：规则/C/引擎/求解器测试全绿（17468 对拍 0 不一致）、
网络强度 random 84% / greedy 74% / l2 72%（150 局）、生产组合 vs greedy 胜率 84.5% 净分 +4.25（200 局）。

### ⛔ 不降级策略（重要）

本项目的 AI **只用生产模型**（`ckpt/policy_a2c_final56.pt`）。任何环节失败都**直接报错并暂停对局**，绝不静默降级：

| 环节 | 失败时的行为 |
|---|---|
| 桥启动时 final56 未加载成功 | 桥拒绝为该会话服务（返回 500，文案指明“按不降级策略拒绝服务”），不再回退旧 DMC 网络 |
| 开局 `/init` 失败 | 弹「⛔ AI 服务异常（已暂停，不降级）」+ 「🔄 重试连接 AI 服务」；对局不开始 |
| AI 回合 `/act` 失败 | 同上：**电脑不会用内置 AI 代打**，手数保持不变，等你点重试或等自动重连 |
| 出牌提示失败 | 提示条显示错误原因，**不提供内置建议** |
| 自动恢复 | 后台每 6 秒用本局动作历史重建影子局，恢复后自动继续（编号不变） |

对局页座位标签会明确状态：`AI·胜率优先 / 净分优先`（正常，生产模型）、`AI·已暂停（待重试）`、`AI·未连接（已暂停）`。
（`aiCandidates` 等内置贪心代码仍保留在文件里，但**已不再接线**，仅作为离线调试的备用实现。）

### 可用性保障（最强模型"每次都能正常"）

| 机制 | 说明 |
|---|---|
| 启动预检 | 服务启动先加载 `ckpt/policy_a2c_final56.pt` 并打印 `production model READY`；加载失败**直接退出**（systemd 会拉起并留痕），不会带病服务 |
| **跨重启会话恢复** | 启动时扫描 `data/replays/*.json` 中 `live:true` 的对局，按动作历史**重建为同一 sid 的会话**——升级/重启 AI 服务不会打断正在进行的对局（实测：重启前 sid 直接继续 `/act` 成功） |
| 会话容量 | 上限 500（原 60 常被打满）；淘汰**优先丢弃已结束**的会话，万不得已淘汰进行中会话时打 WARN 日志 |
| 决策并发闸 | 按 CPU 核数设信号量，多局并发时排队而非抢占（2 核机器上避免请求被拖到超时） |
| 前端兜底 | 万一请求正好落在重启窗口：前端暂停 + 自动重试（每 6s 按动作历史重建），重连复用同 sid/编号，不产生重复会话 |

### 与线上同步 AI 代码（一键）

线上目录 `/home/ubuntu/pdk-ai-prod` 已是 **git 仓库**（origin=私有仓库，通过**只读部署密钥** `~/.ssh/pdk_ai_deploy` 拉取，服务器上不保存 token）。更新只需一条命令：

```bash
bash /home/ubuntu/paodekuai/update_ai.sh
```

它按顺序做（只动 `paodekuai-ai.service`，不触碰掼米/跑胡子/nginx 等其他服务）：

1. `git pull --ff-only` 拉最新代码
2. 校验**四件套 md5**（final56 / qnet / pdk_core.c）
3. C 核心源码变新时自动 `gcc` 重建 `pdk_core.so`
4. 跑冒烟测试（生成器对拍 / C 对拍 / R1+R3 / 残局顺序）
5. 重启 AI 服务并打印 `/health`（含生产配置与资产校验）

### 开局搜索（opening search）

最新版 pdk-ai 已把 **开局搜索默认开启**（`SolverAgent(opening_search=True)`，首手且手牌 ≥12 张时触发，约 0.45~0.8s/局）：

```
/health  -> productionConfig.openingSearch = true     # 线上配置自述
/stats   -> openingSearch: N, openingTime: T          # 实际触发次数与累计耗时
```

桥的 `/api/decide`（双真人提示）同样使用该配置，响应里也带 `openingSearch` 标记。

### 跨平台提示

pdk-ai 的 `pdk/fast.py` 默认只找 `c/pdk_core.dll`（Windows）。Linux 部署需按 `os.name` 选择 `pdk_core.dll / pdk_core.so`（pdk-ai-prod 副本已含此补丁），并编译 C 核心：`gcc -O2 -shared -fPIC -o pdk_core.so pdk_core.c -lm`；未编译时自动回退纯 Python（慢但可用）。

## 🚀 线上部署（腾讯云 43.128.24.244）

与掼蛋站并存，全部为**增量独立资源**，不改动其他服务：

| 项 | 值 |
|---|---|
| 测试地址 | <http://43.128.24.244:8310/> |
| 代码目录 | `/home/ubuntu/paodekuai/`（网页）、`/home/ubuntu/pdk-ai-prod/`（生产 AI） |
| 网页服务 | `paodekuai-web.service`（系统 Python，:8310，内置 `/ai/*` 反向代理到桥） |
| AI 服务 | `paodekuai-ai.service`（独立 venv `.venv`：numpy + CPU torch，仅监听 127.0.0.1:8766，不暴露公网） |
| 对局回放 | `/home/ubuntu/paodekuai/data/replays/*.json`（每局自动落盘） |
| 日志 | `/var/log/paodekuai-web.log`、`/var/log/paodekuai-ai.log` |

网页前端通过同源 `/ai/*` 反代调用桥，浏览器无跨域、8766 端口无需开放公网。C 核心 `pdk_core.so` 已在服务器编译。

常用命令：

```bash
systemctl status paodekuai-web paodekuai-ai
sudo systemctl restart paodekuai-web paodekuai-ai
ls -t /home/ubuntu/paodekuai/data/replays/*.json | head   # 最新对局
python3 -m json.tool "$(ls -t /home/ubuntu/paodekuai/data/replays/*.json | head -1)"
```

## 对局记录 / 复盘（网页内查看 + 人工点评）

**网页里**：大厅点「📚 对局记录」→ 服务器对局列表（每局**唯一编号** A=人机 / H=真人 + 时间 + 参与 + 手数 + 结果）；
点任意一局进入**逐手复盘**：⏮开局 / ◀上一手 / 下一手▶ / 末手⏭ 逐步查看**双方手牌**与**每一手出牌详情**（牌面、牌型、剩余张数）。

**对局中随时复盘**（人机对战页面底部两个按钮）：

- 「📝 复盘当局」：打开**正在进行的这一局**（开局即分配编号并实时落盘，`live:true`），自动定位到最新一手，可就当前局面直接写点评；
- 「📝 复盘上局」：打开**上一局**（本场上一轮，或最近一局已结束的对局）；
- 顶部标签常显 `本局 A0123 / 上局 A0122`，方便对照；复盘页返回即回到牌桌，不打断对局；
- 进行中的对局在记录列表里显示为「进行中」；超时未结束的会在下次开局时标记为「未完成（中断）」。

**人工点评**（人来复盘、帮 AI 快速学习）：

- 复盘页底部输入点评，可选「整局」或「针对第 N 手」；
- 落盘为 `data/comments/<编号>.json`（按局）与 `data/human_reviews.jsonl`（全量一行一条）；
- 每条都带标记，AI 可直奔定位：

```json
{"source": "human_review", "kind": "human_comment",
 "no": "H0002", "ply": 3, "text": "第3手应保留顺子结构", "author": "人工", "ts": "…"}
```

- 网页 API：`GET /replays/list`、`GET /replays/get?no=H0002`、`POST /replays/comment {no,text,ply}`。

## 🌐 对外 API（把 AI 提供给其他调用方）

对外走**独立实例 + 网关**，与自己玩的对局实例物理隔离（外部流量抢不到你游戏的算力）：

```
调用方 ──HTTPS──▶ nginx /pdk-ai/ ──▶ 网关 127.0.0.1:8770 ──▶ 对外 AI 实例 127.0.0.1:8776
                     (limit_req)      Key/配额/审计            --public-api（无会话协议）
你的网页版 ──▶ 8310 ──/ai/*──▶ 本机 AI 实例 127.0.0.1:8766（含会话协议，仅内网）
```

- 入口：`https://chinesetestsite.com/pdk-ai/v1/*`（HTTPS，现有域名与证书）
- 鉴权：`X-API-Key`（Key 与配额在 `/etc/paodekuai-api/keys.json`，改完自动热加载）
- 接口：`/v1/new_game`、`/v1/play`、`/v1/state`、`/v1/suggest`（整局对局，AI 自动应手）、
  `/v1/decide`、`/v1/explain`、`/v1/analyze`、`/v1/decode`（无状态）、`/v1/health`（免 Key，含模型版本号）
- 服务：`paodekuai-api.service`（对外实例）、`pdkai-gateway.service`（网关）
- 审计：`/var/log/paodekuai-api.log`（时间/Key名/接口/状态/耗时，不含明文 Key）
- 文档：[docs/AI_API.md](docs/AI_API.md)（给调用方）；[docs/API开放方案.md](docs/API开放方案.md)（架构与运维）
- 数据隔离：对外牌局落盘 `data/external/`，主站对局记录与人工点评不受影响

## 🤖 AI 出牌解释（明牌看牌 + 复盘分析）

AI 模型自带解释能力（pdk-ai 的 `POST /api/explain`、`POST /api/decide` + `"explain": true`），网页版把它接到两个场景：

**1）人机对局「👁 明牌」时：AI 每出一手，牌桌上方自动给出这手的依据**

```
🤖 局面：我剩 15 张, 对手剩 14 张; 台上: 单张7
   AI 出牌 单张Q
   依据 残局求解(定胜负)
   理由 在 32 个采样世界里求解, 单张Q 的胜率最高 (62%)。 次优: 单张J 58%
   候选 单张J 58%、单张10 44%
   算牌 对手高置信至少 2 张A
```

- 走桥接的 `/api/explain`：**AI 出牌那一刻就抓取**（`Shadow.explains`），点开是瞬时返回、局面与实际对局 100% 一致（无缓存时才回退内核重放）；
- AI **过牌**同样给解释（为什么不出）。

**2）复盘页「🤖 AI 解析这手」：任意一手都能分析**

| 这一手是 | 面板给出 |
|---|---|
| AI 自己出的 | 当时实际出牌 vs AI 重算选择（一致会标 ✓），外加依据/理由/候选胜率/算牌 |
| **人类（你）出的** | **反事实对照**：你实际出了什么 → **换 AI 来打会出什么**（不同会标黄），同一套依据/理由/候选 |

- 关键点：分析基于**真实牌谱**（`/api/analyze` 用引擎重放真实局面再问 AI），不做"重放重决策"，因此不会漂移、人类手也能分析——这正是人工点评时最需要的"这手该不该这么出"；
- 历史人机局只存动作码，进入复盘时由 `/api/decode` 解码成**具体牌面 + 每手归属**（过牌后同一人继续领出，座位由引擎判定），复盘页的手牌快照与"谁出的"因此完全正确；
- 中盘（双方合计 > 28 张）不启用残局精确求解时，面板会补一句说明，避免只看到"策略网络选择"这一句。

桥接新增接口（`ai_bridge.py`，全部 JSON，失败直接报错不降级）：

```
POST /ai/api/explain  {sid,ply} | {initial_hands,first_player,opts,moves,ply,ai_seat}
      -> {ply,seat,text,path,reason,cands,belief,state_text,decided,recorded,anchored_back,cached,note}
POST /ai/api/analyze  {sid,ply} | {initial_hands,first_player,opts,moves,ply,mode}
      -> {ply,seat,state_text,recorded{cards,patText,pass},decided{...},agree,explain{path,reason,cands,belief},note}
POST /ai/api/decode   {initial_hands,first_player,opts,moves|codes}
      -> {moves:[{ply,seat,cards,pass,pass_on,combo,handAfter,patText}]}
```

`path` 取值中文对照（前端 `EXPLAIN_PATH_CN`）：`opening_search` 开局搜索 / `pimc_c`、`pimc_cn` 残局求解 / `endgame_order` 残局连续保权 / `report_dump` 报单保权 / `one_shot` 一手打完 / `lookahead` 前瞻搜索 / `fallback_net` 策略网络 / `forced` 唯一合法手。

## 对局记录 / 复盘（供 AI 读取）

所有对局以**完整、自包含**的 JSON 落盘（无需解码即可读牌面），并附复盘工具 `replay_report.py`：

| 目录 | 内容 |
|---|---|
| `data/replays/*.json` | **人机局**：座位0=你、座位1=AI，含双方初始手牌、扣底、每一手牌面/牌型、胜负比分、使用的模式与网络 |
| `data/online/*.json` | **在线真人局**：房号、双方昵称、初始手牌、扣底、每一手（牌面+牌型+剩余张数）、先手、结算与累计 |
| `data/all_games.jsonl` | `export` 生成的全量合并文件，**一行一局**，可直接整份喂给 AI |

**对局编号与时间**（方便快速定位后丢给 AI）：

| 编号 | 含义 |
|---|---|
| `A0001, A0002 …` | 人机局（`data/replays`，座位0=你，座位1=AI） |
| `H0001, H0002 …` | 在线真人局（`data/online`，两人房间对打） |

每局 JSON 开头即 `no`(编号) 与 `timeText`(本地可读时间)；`list`/`show`/`export` 都按 **时间倒序** 展示，可用编号直接定位：

```bash
python3 replay_report.py list          # 全部对局（编号/时间/类型/参与/手数/结果）
python3 replay_report.py list H        # 只看真人局（A=只看人机局）
python3 replay_report.py show H0003    # 按编号看整局复盘（每手牌面可读）
python3 replay_report.py raw H0003     # 该局原始 JSON，直接喂给 AI
python3 replay_report.py export        # 全量 -> data/all_games.jsonl
python3 replay_report.py export human_games.jsonl H   # 只导真人局
```

每手记录字段：`ply`(第几手) `seat`(座位) `cards`(牌 id 列表，过牌为空) `combo`(牌型: ptype/main/len/nc) `pass` `pass_on`(被过的牌型) `handAfter`(出牌后剩余张数)。
牌 id：点数 = `id>>2`（0=3 … 11=A，12=2），花色 = `id&3`（0♠1♥2♣3♦）；ptype：0单 1对 2连对 3三张 4三带二 5三带一 6飞机 7顺子 8炸弹 9四带三。

```bash
python3 replay_report.py list          # 列出全部对局
python3 replay_report.py stats         # 统计（局数/胜率）
python3 replay_report.py latest        # 最新一局完整复盘（含每手牌面）
python3 replay_report.py show data/online/xxxx-r1-....json
python3 replay_report.py export        # 合并为 data/all_games.jsonl（AI 直读）
```

## 对局记录 / 复盘

- 每局结束自动写入浏览器本地 `localStorage`（`pdk_replays_v1`，最多保留 100 局），无需手动保存。
- 记录内容：规则开关、双方**初始手牌**、**扣底 16 张**、先手、每一步动作（座位 + 牌 id + 牌型 + 时间戳）、胜负与累计分。
- 大厅「📚 对局记录」入口可查看历史局、**逐局下载 JSON**、**一键导出全部**；导出的 JSON 即可用于 AI 行为克隆/复盘。
- AI 桥接端同时把每局完整回放写入 `data/replays/`，即使网页刷新也不丢已完成对局。

## 模式

| 模式 | 说明 |
|---|---|
| 🤖 人机对战 | 你 VS AI，你拥有 AI 出牌提示 |
| 👥 双人热座 | 一台设备轮流操作，玩家一有 AI 提示（回合间遮罩防偷看） |
| 🌐 在线对战 | **创建房间得 4 位房号，好友输房号加入**；两台设备各看各的牌，互不相同 |

### 在线对战说明

- 房间与牌局权威在服务器（`online_server.py`，pdk 引擎保证必出/报单/结算），网页只提交动作并轮询自己的视角（对手手牌永不下发）。
- 双方都有 AI 提示（走 pdk-ai 生产内核无状态 `/api/decide`）。
- 刷新页面自动恢复进行中的对局（token 存 localStorage）；退出房间即清除。
- 局数与规则选项沿用大厅设置（创建房间时生效）。

## 规则要点（两人版）

- 一副牌 48 张：拿走大小王、三个2（♥2 ♣2 ♦2）和黑桃A；**黑桃2保留**、为最大单张。**随机抽走 16 张作为扣底，剩余 32 张两人各 16 张**。手牌按**从大到小**（左 2 → 右 3）排列。
- 首局黑桃3持有者先出；之后每局上局赢家先出。**有牌必打**（能管上必须出）。
- 点数：2 > A > K > Q > J > 10 > … > 3；2 不能进顺子。
- 牌型：单张 / 对子 / 连对(≥2对) / 顺子(5~12张) / 三带二 / 飞机(≥2连三张，每副带2张) / 炸弹(4张同点，3333最小、KKKK最大)。
- **三个A（AAA）只是三张牌，不算炸弹**（A只有3张，凑不出四个A）。
- 纯三张、三带一只能在最后一手打出（可选「三张不可接」后也不能管三带二）。
- 报单：剩1张须报单；对方报单后你出单张必须出最大单张（防放水/包赔）。

## 计分（底分 1分/张）

- 输家 = 剩余张数 × 底分，**剩1张不计分**；**关门**（输家一张未出）失分翻倍。
- 每颗未被压掉的炸弹向对方收 10 分。
- 可选「红桃十翻倍」：♥10 持有者本局输赢×2。
- 默认 10 局（可选 5/10/15/20），累计计分，终局出总成绩表。

## 可选选项

- 三张不可接：最后一手三张/三带一不能管三带二
- 炸弹不可拆：炸弹不能拆开打别的牌型
- 红桃十翻倍：♥10 持有者输赢×2
- 四带三：可以出四张带任意三张

## 部署（可选）

与掼蛋站相同套路：整目录上传服务器后用任意静态服务器（nginx alias / `python server.py`）托管即可。

## 机器人模型接口（预留）

AI 出牌走 `aiMove()`，默认内置贪心算法。已预留可插拔模型入口，后续接自定义模型时无需改动牌局代码：

```js
// 在控制台或自己的脚本里注册（同步返回或 Promise 均可）
pdkRegisterAI(async (ctx) => {
  // ctx = {
  //   seat: 1,                  // AI 固定坐 1 号位
  //   hand: [...],              // AI 当前手牌（副本）
  //   oppCount: 17,             // 对手剩余张数
  //   last: combo | null,       // 桌面上一手牌型（null = 自由出牌）
  //   legal: [{cards, combo}],  // 全部合法候选（已过规则校验）
  //   opts: {...},              // 本局规则开关
  // }
  // 返回：legal 中某个候选的 cards 数组；null/[] 表示不出（自由出牌时无效）
  return ctx.legal[0]?.cards ?? null;
});
```

安全约束：

- 返回的牌按 **id 集合**与 `legal` 候选精确匹配，匹配不上（越权/幻觉出牌）自动回退内置 AI；
- 模型抛异常同样回退内置 AI，牌局不中断；
- 自由出牌时返回「不出」无效，回退内置 AI。

## 代码结构（app.js 单文件分区块）

| 区块 | 内容 |
| --- | --- |
| 状态 `S` | 局面单一数据源：手牌、回合、累计分、历史、开关 |
| 基础工具 | `buildDeck`（48 张编码 `{i,r,s}`）、`shuffle`（crypto 均匀随机）、牌面 HTML |
| 牌型分析 | `analyzeShape`：single/pair/triple/t1/t2/straight/pairseq/plane/planeBare/bomb/quad3 |
| 比较 | `canBeat`：同型同长比 key；炸弹压一切；三张家族交叉规则 |
| 上下文校验 | `comboContextOK`（最后一手限制）、`breaksBomb`（炸弹不可拆）、`violatesBaodan`（防放水） |
| 候选生成 | `genLeads`（自由出牌）/ `genBeats`（跟牌）→ `legalPlays` 统一过滤去重 |
| AI | `estimatePlays`（剩余手数）+ `scoreCandidate`（打分）+ `aiCandidates`；外挂入口 `pdkRegisterAI` |
| AI 桥接 | `bridge*` 系列函数：影子牌局同步、`/act` 决策、`/suggest` 提示、失步重放、断桥降级 |
| 流程 | `startMatch → startRound → beginTurn → applyPlay/applyPass → endRound` |
| 结算 | `settle`（底分/关门/炸弹/红桃十）→ 单局弹窗 / 总成绩表 / 计分板 |
| 渲染 | `render`（座位/出牌区/手牌/按钮态），热座按当前座位视角渲染 |
