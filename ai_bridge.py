#!/usr/bin/env python3
"""跑得快 AI 机器人桥接服务 —— 把 pdk_ai 的深度 AI 内核接到网页版跑得快。

原理（影子牌局）：
  网页版开局时把双方手牌/先手/规则开关同步给桥 -> 桥内用 pdk.engine.Game +
  pdk.fast.CGame 建立镜像牌局（同发牌、同动作序列）；网页版 AI 座位(1)要出牌时
  调 /act，由 SolverAgent(残局PIMC+精确求解) + DMC 网络兜底决定一手并同步镜像；
  人类的每一手（出牌/过牌）通过 /action 实时镜像。/suggest 重放整局后给座位0
  出一手建议（不落子），供网页版「提示」按钮使用。

启动（放到 pdk_ai 项目旁，能 import 其 server 模块即可）：
  python ai_bridge.py                 # 默认 :8766
  python ai_bridge.py --port 8766 --bot-root <pdk_ai项目路径>

接口（全部 JSON）：
  GET  /health                      -> {ok, agent, sessions}
  POST /init    {hands:[[48内id]x2], leader, opts{sanzhang,nobomb,red10,four3}, actions?}
                                    -> {sid, ai_seat, mode}
  POST /action  {sid, seat, cards}  -> 镜像人类动作（cards=[] 过牌）-> {ok, finished}
  POST /act     {sid}               -> AI(座位1)出一手并同步 -> {cards:[id...], finished}
  POST /suggest {sid}               -> 座位0的建议（重放决策，不落子）-> {cards:[id...]}
  POST /api/explain {sid,ply} | {initial_hands,moves,...}  -> 出牌解释（AI 为什么这么出；明牌模式）
  POST /api/analyze {sid,ply} | {initial_hands,moves,...}  -> 复盘分析：该手按真实局面 AI 会怎么打 + 理由
  POST /api/decode  {initial_hands,first_player,opts,moves} -> 牌谱解码（座位/具体牌/牌型，复盘渲染用）
  POST /api/decide  {my_hand,opp_n,trick,history,explain} -> 无状态单步决策（可带解释）
  不降级策略：任何失败都返回明确错误（4xx/5xx + error 文案），
  网页版会暂停并提示重试，绝不静默换成内置贪心 AI。

牌 id 语义与 pdk_ai 相同: id = (rankIdx<<2)|suit, rankIdx 0=3..11=A, 12=2(仅黑桃)。
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

DEFAULT_BOT_ROOT = Path(__file__).resolve().parent.parent / "pdk-ai-prod"     # 生产版仓库 (github.com/reportyao/pdk-ai)
if not DEFAULT_BOT_ROOT.exists():
    DEFAULT_BOT_ROOT = Path(__file__).resolve().parent.parent / "pdk_ai_work" / "pdk_ai"  # 同源工作副本
# --bot-root 需在导入 pdk/server 之前生效，这里先解析一次（argparse 在后面还会再解析）
for _i, _a in enumerate(sys.argv):
    if _a == "--bot-root" and _i + 1 < len(sys.argv):
        DEFAULT_BOT_ROOT = Path(sys.argv[_i + 1])
    elif _a.startswith("--bot-root="):
        DEFAULT_BOT_ROOT = Path(_a.split("=", 1)[1])
BOT_ROOT = DEFAULT_BOT_ROOT
if not BOT_ROOT.exists():
    BOT_ROOT = Path(__file__).resolve().parent      # 允许把桥放到 pdk_ai 目录内运行
if str(BOT_ROOT) not in sys.path:
    sys.path.insert(0, str(BOT_ROOT))   # pdk / train 包从这里导入

# 按显式文件路径加载 pdk-ai 的服务层。不能用 `import server`：
# 本目录的网页 server.py 与之同名，sys.path 顺序稍有不符就会加载错（并因
# 读 sys.argv 崩溃），importlib 按路径加载可彻底避开同名冲突。
import importlib.util                 # noqa: E402
_spec = importlib.util.spec_from_file_location("pdk_ai_server", str(BOT_ROOT / "server.py"))
bot_server = importlib.util.module_from_spec(_spec)
sys.modules["pdk_ai_server"] = bot_server
_spec.loader.exec_module(bot_server)

from pdk import fast                 # noqa: E402
from pdk.core import Config          # noqa: E402
from pdk.engine import Game, counts_of_ids  # noqa: E402
from pdk.explain import describe_state, pattern_text  # noqa: E402
from pdk.fast import PASS_CODE       # noqa: E402

# 生产双模式 (pdk-ai README「当前生产模型与部署清单」):
#   生产模型 = ckpt/policy_a2c_final56.pt (A2C + 56维动作后特征)
#   hybrid = SolverAgent(hybrid, threshold=28) + _QFB(final56) + 规则层 R0-R3 —— 胜率优先(生产配置)
#   dual   = 同上但 SolverAgent(engine='dual', <=14张数值计分接力)      —— 积分制净分优先
PROD_MODES = {"hybrid": "c", "dual": "dual"}


def net_info(fb) -> str:
    """当前 fallback 实际加载的网络（供 /health 明示接的是不是 final56）。"""
    name = getattr(fb, "name", "?")
    use_x = getattr(fb, "use_x", None)
    if use_x is True:
        return "a2c-final56(56x)"
    if use_x is False:
        return "dmc-v1(low feature)"
    return name


# pdk-ai README「部署四件套」md5（缺一不可）；Linux 的 .so 由 pdk_core.c 本地编译
ASSET_MD5 = {
    "ckpt/policy_a2c_final56.pt": "534dcca82ee60760a1a40c546a83e5f1",
    "ckpt/qnet.pt": "4be2824a0f47c98be6fa8c80276d0777",
    "c/pdk_core.c": "aefb96685e0dc8a164c60bc00388919a",
}
if os.name == "nt":
    ASSET_MD5["c/pdk_core.dll"] = "f49c46e86313075df918caad4fa2f085"


_NET_PROBE = {"done": False, "net": None, "error": None, "ts": 0.0}


def net_probe(refresh: bool = False) -> dict:
    """探测“新会话实际会加载哪个网络”——暴露 final56 缺失时的静默回退。"""
    import time as _t
    if _NET_PROBE["done"] and not refresh and (_t.time() - _NET_PROBE["ts"] < 3600):
        return {"net": _NET_PROBE["net"], "error": _NET_PROBE["error"]}
    net, err = None, None
    try:
        fb = bot_server._QFB()          # 与每个会话构建路径完全一致
        net = net_info(fb)
        if getattr(fb, "use_x", False) is not True:
            err = "已回退旧网络（final56 加载失败）"
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
    _NET_PROBE.update({"done": True, "net": net, "error": err, "ts": _t.time()})
    return {"net": net, "error": err}


def prod_config_info() -> dict:
    """生产推理配置自述：开局搜索是否开启（线上确认用）。"""
    try:
        import inspect
        from pdk.agents import SolverAgent
        pr = inspect.signature(SolverAgent.__init__).parameters
        return {
            "openingSearch": bool(pr["opening_search"].default),   # 桥未覆盖 → 生效=类默认
            "openingWorlds": int(pr["opening_worlds"].default),
            "totalThreshold": 28,        # 桥显式传入（与生产 build_ai 同值）
            "overrides": "total_threshold=28, max_rows=400000",
        }
    except Exception as e:
        return {"error": str(e)}


def verify_assets() -> dict:
    """启动/健康检查时校验生产模型与 C 核心是否与 README 清单一致。"""
    import hashlib
    out = {"ok": True, "files": {}}
    for rel, want in ASSET_MD5.items():
        p = BOT_ROOT / rel
        if not p.exists():
            out["files"][rel] = "MISSING"
            out["ok"] = False
            continue
        h = hashlib.md5(p.read_bytes()).hexdigest()
        good = (h == want)
        out["files"][rel] = "OK" if good else f"MD5={h} (want {want})"
        if not good:
            out["ok"] = False
    if os.name != "nt":
        so = BOT_ROOT / "c" / "pdk_core.so"
        out["files"]["c/pdk_core.so"] = "OK" if so.exists() else "MISSING(需 gcc 构建)"
        if not so.exists():
            out["ok"] = False
    return out


def build_prod_agent(engine: str):
    """按生产 server.build_ai() 同源构建智能体，只有 SolverAgent 的 engine 可选。

    fallback 直接用官方 server._QFB() —— 即生产模型 final56
    (273 obs + 56 维动作后特征) + R3 开局结构守护；checkpoint 缺失时它自己回退 DMC。
    R0(一手走完)/R1(报单保权) 在 pdk/agents.py，R2(残局连续保权) 在 pdk/endgame_order.py，
    均随 SolverAgent 无条件生效。
    """
    from pdk.agents import SolverAgent
    fb = bot_server._QFB()                     # 生产 fallback: final56 + R3
    if getattr(fb, "use_x", False) is not True:
        # 不降级策略：宁可报错，也不用旧 DMC 网络糊弄玩家
        raise RuntimeError("生产模型 ckpt/policy_a2c_final56.pt 未加载成功"
                           "（检测到 pdk 内部回退到旧网络）——按不降级策略拒绝服务")
    agent = SolverAgent(fb, bot_server.CFG, total_threshold=28,
                        max_rows=400000, engine=engine)
    return agent, ("hybrid" if engine == "c" else engine), net_info(fb)


SESSIONS: dict = {}
LOCK = threading.Lock()
MAX_SESSIONS = 500          # 上限放宽；淘汰时优先丢弃已结束/最旧的会话，避免打断进行中的对局
_ACT_SEM = None             # 决策并发闸（在 main() 里按 CPU 初始化）
_NULL_SEM = contextlib.nullcontext()   # 导入期占位（无锁语义）
RESTORING = True                       # 启动恢复进行中（此期间的旧会话请求提示重试）
REPLAY_DIR = Path(__file__).resolve().parent / "data" / "replays"
REPLAY_DIR.mkdir(parents=True, exist_ok=True)


def next_replay_no(dirpath: Path, prefix: str) -> str:
    """扫描目录内已有编号，取最大值 +1（A=人机局 / H=真人对局）。"""
    mx = 0
    for f in dirpath.glob("*.json"):
        try:
            no = str(json.loads(f.read_text(encoding="utf-8")).get("no", ""))
        except Exception:
            continue
        if no.startswith(prefix) and no[len(prefix):].isdigit():
            mx = max(mx, int(no[len(prefix):]))
    return f"{prefix}{mx + 1:04d}"


_NO_LOCK = threading.Lock()


def cleanup_stale_live(hours: float = 2.0):
    """把超时未结束的 live 标记为已中断，避免“进行中”长期残留。"""
    now = time.time()
    for f in REPLAY_DIR.glob("*.json"):
        try:
            if now - f.stat().st_mtime < hours * 3600:
                continue
            d = json.loads(f.read_text(encoding="utf-8"))
            if d.get("live"):
                d["live"] = False
                d["aborted"] = True
                f.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            continue


def allocate_no(prefix: str = "A") -> str:
    """分配对局编号（进程内加锁，避免并发会话撞号）。"""
    with _NO_LOCK:
        return next_replay_no(REPLAY_DIR, prefix)


def write_replay(s: Shadow, live: bool):
    """写对局文件。开局即写（live=True，含编号），每手更新，终局改写 live=False。"""
    payload = {
        "no": s.no,                                   # 对局编号（开局即定，方便“复盘当局”定位）
        "timeText": s.time_text,
        "live": bool(live),                           # True=进行中
        "version": 2, "source": "ai_bridge", "sid": s.sid,
        "ts": s.ts_iso,
        "names": ["human", "ai"], "mode": getattr(s, "prod_mode", "hybrid"),
        "net": getattr(s, "net", ""),
        "humanSeat": 0,                       # 座位0 = 真人，座位1 = AI
        "hands": s.init_payload["hands"], "kitty": s.init_payload.get("kitty", []),
        "leader": s.init_payload["leader"], "opts": s.init_payload["opts"],
        # moves: 每一手的完整明细（牌 id 可直接读；combo.ptype 0单1对2连对3三4三带二5三带一6飞机7顺子8炸弹9四带三）
        "moves": list(getattr(s, "moves_detail", [])),
        "codes": list(s.codes),
        "winner": (None if live else s.game.winner),
        "scores": (None if live else list(s.game.scores or (0, 0))),
    }
    try:
        (REPLAY_DIR / s.file).write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                                         encoding="utf-8")
    except OSError:
        pass


def save_replay(sid: str, s: Shadow):
    """终局落盘（覆盖同编号文件，live 置 False）。"""
    if getattr(s, "saved", False) or not s.game.finished:
        return
    write_replay(s, live=False)
    s.saved = True


def build_cfg(o: dict) -> Config:
    return Config(
        triple_no_follow=bool(o.get("sanzhang", False)),
        bomb_indivisible=bool(o.get("nobomb", True)),
        heart_ten_double=bool(o.get("red10", False)),
        four_with_three=bool(o.get("four3", False)),
    )


class Shadow:
    """一局的镜像状态 + AI 内核。"""

    def __init__(self, hands, kitty, leader, opts, prod_mode="hybrid",
                 sid: str = "", reuse_no=None, reuse_file=None):
        self.created = time.time()
        self.sid = sid or uuid.uuid4().hex[:12]
        self.no = reuse_no or allocate_no("A")            # 开局即定编号（复盘当局用）
        self.file = reuse_file or f"{self.sid}.json"      # 重同步复用同一文件，避免产生幽灵局
        self.time_text = time.strftime("%Y-%m-%d %H:%M:%S")
        self.ts_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self.cfg = build_cfg(opts)
        engine = PROD_MODES.get(prod_mode, "c")
        self.prod_mode = "hybrid" if engine == "c" else prod_mode
        h0, h1, k = sorted(hands[0]), sorted(hands[1]), sorted(kitty)
        assert len(h0) == 16 and len(h1) == 16 and len(k) == 16, "需要两手16张和扣底16张"
        assert sorted(h0 + h1 + k) == sorted(bot_server.DECK), "两手牌+扣底必须恰好构成48张"
        self.game = Game(cfg=self.cfg, first_player=leader, hands=[h0, h1], kitty=k)
        self.cg = fast.CGame(counts_of_ids(h0), counts_of_ids(h1), leader, self.cfg)
        self.agent, self.mode, self.net = build_prod_agent(engine)
        self.ai_seat = 1
        self.agent.new_game(self.ai_seat, list(self.game.cnt[self.ai_seat]))
        self.codes = []
        self.moves_detail = []
        self.init_payload = {"hands": [h0, h1], "kitty": k, "leader": leader, "opts": opts}
        self.explains = {}                   # ply -> 本手 AI 决策解释（明牌/复盘即时可读，免重算）
        self.bad = False                     # 影子失效 -> 通知网页版降级
        self.restoring = False               # 恢复重放期间跳过逐步写盘（提速）
        self.lock = threading.Lock()

    def capture_explain(self, code: int, cards: list):
        """落子前抓取 agent.last_explain —— 明牌模式与复盘可即时取用（不重算、不漂移）。
        仅当解释确实是本手时缓存（内核拒绝/改打时不缓存，宁缺勿错）。"""
        try:
            ex = getattr(self.agent, "last_explain", None) or {}
            if int(ex.get("move", -1)) != int(code):
                return
            seat = self.ai_seat
            self.explains[len(self.codes) + 1] = {
                "explain": json.loads(json.dumps(ex)),      # 深拷贝（纯 JSON 结构）
                "state": {"me": [int(x) for x in self.cg.g.cnt[seat]],
                          "opp": int(self.cg.g.n[1 - seat]),
                          "trick": (None if self.cg.g.trick[0] == 255
                                    else [int(x) for x in self.cg.g.trick])},
                "decided": int(code), "recorded": int(code), "cards": list(cards),
            }
            if len(self.explains) > 200:                    # 上限保护（一局最多几十手）
                for k in sorted(self.explains)[:-200]:
                    self.explains.pop(k, None)
        except Exception:
            pass

    # 与 pdk_ai server._apply 相同的镜像顺序：先 observe 后落子
    def snapshot_stats(self):
        try:
            self.last_stats = dict(getattr(self.agent, "stats", {}) or {})
        except Exception:
            pass

    def _apply(self, code: int):
        trick_before = (None if self.cg.g.trick[0] == 255
                        else tuple(self.cg.g.trick))
        seat_abs = int(self.game.turn)
        # 复盘用明细：解码成具体牌 id + 牌型（落子前手牌完整，解码可靠）
        if code:
            try:
                cards = list(fast.code_to_cards(code, self.game.hand_ids(seat_abs)))
            except Exception:
                cards = []
            pat = fast.classify_code(code, False, self.cfg) or fast.classify_code(code, True, self.cfg)
            combo = ({"ptype": int(pat.ptype), "main": pat.main,
                      "len": pat.length, "nc": pat.nc} if pat else None)
        else:
            cards, combo = [], None
        self.moves_detail.append({"ply": len(self.moves_detail) + 1, "seat": seat_abs,
                                  "cards": cards, "combo": combo, "pass": code == 0})
        for ag in [self.agent]:
            ag.observe(seat_abs, code, trick_before)
        try:
            self.game.play(code)
        except Exception:
            # 规则差异兜底：网页版判定的被迫过牌（如双方规则集微差）绕过强校验。
            # 仅对 PASS 放行——非过牌的非法动作说明影子已失真，向上抛出。
            if code != PASS_CODE:
                raise
            self.game._play_unchecked(code)
        self.cg.step(code)
        self.codes.append(code)
        if not self.restoring:
            write_replay(self, live=True)                 # 实时落盘（复盘当局/上一局都能看到最新进度）

    def legal_ids(self, seat: int):
        """当前出牌方的合法动作（牌 id 列表）。非出牌方返回空，避免用错手牌映射。"""
        if int(self.cg.g.turn) != int(seat):
            return []
        return [list(fast.code_to_cards(c, self.game.hand_ids(seat)))
                for c in self.cg.legal()]


def get_sess(sid: str) -> Shadow:
    with LOCK:
        s = SESSIONS.get(sid)
    if s is not None:
        return s
    if RESTORING:
        raise RuntimeError("会话恢复中（服务刚重启），请稍后重试")
    raise KeyError(sid)


def evict_old():
    """达到上限时：先丢已结束的，再丢最旧的；只有万不得已才丢进行中的局（并打日志）。"""
    with LOCK:
        while len(SESSIONS) > MAX_SESSIONS:
            finished = [k for k, s in SESSIONS.items()
                        if getattr(s.game, "finished", False)]
            pool = finished or list(SESSIONS.keys())
            k = min(pool, key=lambda s: SESSIONS[s].created)
            victim = SESSIONS.pop(k)
            if not getattr(victim.game, "finished", False):
                print(f"[bridge] WARN 淘汰进行中会话 {k}（编号 {getattr(victim,'no','?')}）",
                      file=sys.stderr, flush=True)


def handle_init(p: dict):
    hands = p["hands"]
    kitty = p.get("kitty", [])
    leader = int(p.get("leader", 0))
    opts = p.get("opts", {})
    prod_mode = str(p.get("mode", "hybrid")).lower()
    if prod_mode not in PROD_MODES:
        return {"error": f"unknown mode '{prod_mode}' (choose hybrid|dual)"}, 400
    cleanup_stale_live()
    sid = str(p.get("sid") or "") or uuid.uuid4().hex[:12]     # 重连时复用同一 sid
    s = Shadow(hands, kitty, leader, opts, prod_mode, sid=sid,
               reuse_no=p.get("no"), reuse_file=p.get("file"))
    with LOCK:
        SESSIONS[sid] = s
    evict_old()
    write_replay(s, live=True)                            # 开局即写（含编号 + 初始手牌）
    # 可选：创建后即重放一串动作（断线重同步用）
    for mv in p.get("actions", []):
        ok, err = do_action(sid, int(mv["seat"]), mv.get("cards", []))
        if not ok:
            # 重放失败：不返回 sid，客户端会视为同步失败（避免拿到半重放会话）
            SESSIONS.pop(sid, None)
            return {"error": "replay failed: " + err}, 400
    return {"sid": sid, "ai_seat": s.ai_seat, "mode": s.mode,
            "no": s.no, "file": s.file}, 200


def do_action(sid: str, seat: int, cards):
    """镜像人类动作。返回 (ok, info)。

    过牌宽容：网页版可能有引擎未建模的规则差异（如报单防放水导致只能过），
    这里对 PASS 只校验轮次、不校验合法性——对局权威在网页版，桥只是影子。
    """
    s = get_sess(sid)
    with s.lock:
        if s.bad:
            return False, "session broken"
        if s.game.finished:
            return False, "game finished"
        if int(s.game.turn) != seat:
            # 幂等容忍：同一座位刚出的同一手重复到达（跨海乱序/客户端重发）按成功处理，避免误判失步
            code_try = fast.cards_to_code(cards) if cards else PASS_CODE
            if (s.codes and s.codes[-1] == code_try
                    and s.moves_detail and s.moves_detail[-1].get("seat") == seat):
                return True, {"duplicate": True}
            return False, f"not seat {seat}'s turn (turn={s.game.turn})"
        code = fast.cards_to_code(cards) if cards else PASS_CODE
        if cards and code not in s.cg.legal():
            return False, "illegal move (not in legal set)"
        try:
            s._apply(code)
            save_replay(sid, s)
        except Exception as e:                       # 引擎拒绝 -> 影子失效
            s.bad = True
            return False, f"engine rejected: {e}"
    return True, {"finished": bool(s.game.finished)}


def do_act(sid: str):
    """AI(座位1)决策一手并落子镜像。"""
    s = get_sess(sid)
    with s.lock:
        if s.bad:
            return {"error": "影子牌局已失效（本局不再降级，请重开）"}, 500
        if s.game.finished:
            return {"finished": True}, 200
        if int(s.game.turn) != s.ai_seat:
            return {"error": f"not ai turn (turn={s.game.turn})"}, 400
        try:
            _t0 = time.perf_counter()
            with (_ACT_SEM or _NULL_SEM):        # 并发闸：多局并发时排队，避免 CPU 争抢导致超时
                code = s.agent.act(s.cg)
            _dt = time.perf_counter() - _t0
            if _dt > 3.0:                        # 慢决策（多为 CPU 被其他作业抢占）留痕便于排查
                print(f"[bridge] SLOW act {_dt:.1f}s (session {sid[:8]})", file=sys.stderr, flush=True)
            s.snapshot_stats()
            # 注意顺序：先由当前手牌把 code 映射成具体牌 id，再落子镜像
            # （落子后手牌已移除，code_to_cards 会取不到牌）
            cards = list(fast.code_to_cards(code, s.game.hand_ids(s.ai_seat)))
            s.capture_explain(code, cards)        # 落子前抓取（当手局面 + 解释）
            s._apply(code)
            save_replay(sid, s)
        except Exception as e:
            import traceback
            tb = traceback.format_exc()
            return {"error": str(e), "tb": tb[-1200:]}, 500
        return {"cards": cards, "finished": bool(s.game.finished)}, 200


def do_suggest(sid: str):
    """给座位0的建议：重放整局后让内核代座位0决策，不落子。"""
    s = get_sess(sid)
    with s.lock:
        if s.bad:
            return {"error": "影子牌局已失效（不降级，请重开本局）"}, 500
        if s.game.finished:
            return {"cards": None, "reason": "本局已结束"}, 200
        init = s.init_payload
        codes = list(s.codes)
    rep = Shadow(init["hands"], init.get("kitty", []), init["leader"], init["opts"], getattr(s, "prod_mode", "hybrid"))
    with rep.lock:
        # 关键：让内核以「座位0」视角重放决策（Belief 必须以被建议方的手牌构建）
        rep.agent.new_game(0, list(rep.game.cnt[0]))
        for code in codes:
            rep._apply(code)
        if rep.game.finished:
            return {"cards": None, "reason": "本局已结束"}, 200
        if int(rep.game.turn) != 0:
            return {"cards": None, "reason": "现在不是你的回合"}, 200
        with (_ACT_SEM or _NULL_SEM):
            code = rep.agent.act(rep.cg)
        rep.snapshot_stats()
        cards = list(fast.code_to_cards(code, rep.game.hand_ids(0)))
    return {"cards": cards}, 200


def do_explain(p: dict):
    """出牌解释（明牌模式：AI 这手为什么这么出）。

    两种入参：
      1) {"sid": ..., "ply": 7}                      —— 会话版：优先取本局实时抓取的决策解释（瞬时、精确）；
                                                        无缓存时回退到内核重放解释
      2) {"initial_hands": [[id...],[id...]], "first_player": 0, "ai_seat": 1,
          "opts": {...}, "moves": [code|牌id数组...], "ply": 7}  —— 直传版（在线真人局/历史复盘）
    ply 不传=最后一手。
    """
    try:
        if p.get("sid"):
            s = get_sess(p["sid"])
            with s.lock:
                if s.bad:
                    return {"error": "影子牌局已失效"}, 500
                ply = int(p.get("ply") or len(s.codes) or 0)
                # 实时抓取的决策解释（AI 出牌那一刻记录的，状态 100% 对得上）
                hit = s.explains.get(ply)
                if hit:
                    ex = dict(hit["explain"])
                    ex["state_text"] = describe_state(hit["state"]["me"], hit["state"]["opp"],
                                                      hit["state"]["trick"])
                    ex["decided"] = pattern_text(hit["decided"], s.cfg)
                    ex["recorded"] = pattern_text(hit["recorded"], s.cfg)
                    ex["ply"] = ply
                    ex["mode"] = s.prod_mode
                    ex["cached"] = True
                    ex["note"] = explain_note(ex, sum(hit["state"]["me"]), hit["state"]["opp"])
                    return ex, 200
                # 无缓存（老会话/恢复的局）：回退内核重放；人类手锚定到最近一次 AI 决策
                ai_ply = _nearest_ai_ply(s, ply)
                if ai_ply is None:
                    return {"error": f"第 {ply} 手之前 AI 没有决策点"}, 400
                init = s.init_payload
                payload = {
                    "initial_hands": init["hands"], "first_player": init["leader"],
                    "ai_seat": s.ai_seat, "opts": init["opts"],
                    "moves": list(s.codes), "ply": ai_ply,
                }
                with (_ACT_SEM or _NULL_SEM):
                    out = bot_server._explain_replay(payload)
                out["reqPly"] = ply
                out["anchored_back"] = bool(ai_ply != ply)
                st = states_at(out.get("ply"), s)
                out["note"] = explain_note(out, st[0], st[1])
                return out, 200
        else:
            payload = {k: v for k, v in p.items() if k != "mode"}
        with (_ACT_SEM or _NULL_SEM):
            out = bot_server._explain_replay(payload)
        return out, 200
    except Exception as e:
        return {"error": f"解释生成失败：{e}"}, 500


def _nearest_ai_ply(s: Shadow, ply: int):
    """该手之前最近的一次 AI 决策手（含本身）。用于人类手点评时锚定解释对象。"""
    for i in range(min(ply, len(s.moves_detail)), 0, -1):
        if s.moves_detail[i - 1].get("seat") == s.ai_seat:
            return i
    return None


_PIMC_THRESHOLD = 28            # 与生产配置一致：双方合计 <= 28 张才启用残局精确求解


def explain_note(ex: dict, my_n: int, opp_n: int) -> str:
    """网络直接决策时的补充说明（解释面板不至于只剩一句“策略网络选择”）。"""
    path = str((ex or {}).get("path") or "")
    if (ex or {}).get("cands"):
        return ""
    if path in ("fallback_net", "lookahead"):
        return (f"此处双方合计 {my_n + opp_n} 张，超过 {_PIMC_THRESHOLD} 张阈值：未启用残局精确求解，"
                f"由策略网络(56 维动作后特征)直接估价；到残局(不超过 {_PIMC_THRESHOLD} 张)才有胜率与候选对比。")
    if path == "forced":
        return "该手只有唯一合法出法，无需搜索。"
    return ""


# ---------------- 真实牌谱重建（复盘/解释共用） ----------------

def _take_cards(code: int, pool: list):
    """从手牌池里取出该 code 对应的具体牌 id（并移除）。"""
    cards = list(fast.code_to_cards(code, sorted(pool)))
    for c in cards:
        pool.remove(c)
    return cards


def _replay_positions(hands, leader, opts, moves):
    """按真实牌谱重放（不重决策、不漂移），返回逐手明细 + 每手落子前的真实局面。

    moves 元素可以是 code(int)、牌 id 数组、或 {"cards":[...], "seat":n, "pass":bool}。
    座位一律由内核判定（过牌后同一人继续领出，不能按 i%2 猜）。
    返回 (detail, states)：
      detail[i] = {ply, seat, cards, code, pass, pass_on, combo}
      states[i] = {seat, n, trick, hand_ids}     # 第 i+1 手落子前
    """
    cfg = build_cfg(opts or {})
    h0 = sorted(int(x) for x in hands[0])
    h1 = sorted(int(x) for x in hands[1])
    pools = [list(h0), list(h1)]
    cg = fast.CGame(counts_of_ids(h0), counts_of_ids(h1), int(leader), cfg)
    detail, states = [], []
    for i, mv in enumerate(moves):
        seat = int(cg.g.turn)
        trick = None if cg.g.trick[0] == 255 else [int(x) for x in cg.g.trick]
        # 落子「前」的真实局面快照（用于复盘分析：这一手当时是什么情况）
        states.append({"seat": seat, "n": [int(cg.g.n[0]), int(cg.g.n[1])],
                       "trick": trick, "hand_ids": list(pools[seat]), "code": None})
        for s2 in (0, 1):                     # 落子前：手牌池必须与内核剩余张数一致
            if len(pools[s2]) != int(cg.g.n[s2]):
                raise ValueError(f"第 {i + 1} 手前手牌不一致（座位{s2} 解码 {len(pools[s2])} 张，"
                                 f"内核 {int(cg.g.n[s2])} 张）")
        given_seat = mv.get("seat") if isinstance(mv, dict) else None
        if given_seat is not None and int(given_seat) != seat:
            raise ValueError(f"第 {i + 1} 手座位与牌谱不符（牌谱 {given_seat}, 内核 {seat}）")
        if isinstance(mv, dict):
            if mv.get("cards"):
                cards = [int(c) for c in mv["cards"]]
                code = fast.cards_to_code(cards)
                for c in cards:
                    pools[seat].remove(c)
            elif mv.get("code"):
                code = int(mv["code"])
                cards = _take_cards(code, pools[seat])
            else:
                code, cards = PASS_CODE, []
        elif isinstance(mv, int):
            code = int(mv)
            cards = _take_cards(code, pools[seat]) if code else []
        else:
            cards = [int(c) for c in (mv or [])]
            code = fast.cards_to_code(cards) if cards else PASS_CODE
            for c in cards:
                pools[seat].remove(c)
        if code and code not in set(cg.legal()):
            raise ValueError(f"第 {i + 1} 手在真实牌谱上不合法（座位 {seat}）")
        states[-1]["code"] = code
        pat = None
        if code:
            pat = (fast.classify_code(code, False, cfg)
                   or fast.classify_code(code, True, cfg))
        detail.append({
            "ply": i + 1, "seat": seat, "cards": cards, "code": code,
            "pass": code == PASS_CODE,
            "pass_on": trick,
            "combo": ({"ptype": int(pat.ptype), "main": int(pat.main),
                       "len": int(pat.length), "nc": int(pat.nc)} if pat else None),
            "handAfter": int(cg.g.n[seat]) - len(cards),
        })
        cg.step(code)
        if cg.finished:
            break
    return detail, states, cfg


def states_at(ply, s) -> tuple:
    """（双方面向 AI 座位的剩余张数）——用于给重放版解释补 note。"""
    try:
        i = int(ply) - 1
        d = s.moves_detail[i]
        seat = d["seat"]
        # 该手前的手牌数：当前剩余 + 该手及以后该座位出的牌
        left = int(s.game.hand_count(seat)) + sum(len(x.get("cards") or []) for x in s.moves_detail[i:]
                                                 if x["seat"] == seat)
        opp = int(s.game.hand_count(1 - seat)) + sum(len(x.get("cards") or []) for x in s.moves_detail[i:]
                                                     if x["seat"] != seat)
        if seat == s.ai_seat:
            return left, opp
        return opp, left
    except Exception:
        return 0, 0


def _resolve_replay(p: dict):
    """会话版(sid) 或 直传版 -> (detail, states, cfg, ai_seat, mode)。"""
    sid = p.get("sid")
    if sid:
        s = get_sess(sid)
        with s.lock:
            init = s.init_payload
            moves = list(s.codes)
            detail, states, cfg = _replay_positions(init["hands"], init["leader"],
                                                    init["opts"], moves)
            return detail, states, cfg, s.ai_seat, s.prod_mode
    hands = p.get("initial_hands") or p.get("hands")
    if not hands or len(hands) != 2:
        raise ValueError("initial_hands 必填（两家初始手牌）")
    moves = p.get("moves") or p.get("codes") or []
    detail, states, cfg = _replay_positions(hands, int(p.get("first_player", 0)),
                                            p.get("opts") or {}, moves)
    return (detail, states, cfg, int(p.get("ai_seat", 1)),
            str(p.get("mode", "hybrid")).lower())


def do_decode(p: dict):
    """把牌谱解码成逐手明细（座位由内核判定；顺带给出真实牌面与牌型）。

    历史人机局的复盘文件只存动作码，前端拿不到“谁出的/具体哪些牌”→ 复盘手牌快照会错。
    入参：{initial_hands|hands, first_player, opts, moves|codes}
    """
    try:
        detail, states, cfg = _replay_positions(
            p.get("initial_hands") or p.get("hands"), int(p.get("first_player", 0)),
            p.get("opts") or {}, p.get("moves") or p.get("codes") or [])
        return {"moves": [{
            "ply": d["ply"], "seat": d["seat"], "cards": d["cards"], "pass": d["pass"],
            "pass_on": d["pass_on"], "combo": d["combo"], "handAfter": d["handAfter"],
            "patText": pattern_text(d["code"], cfg) if d["code"] else "不出",
        } for d in detail]}, 200
    except Exception as e:
        return {"error": f"牌谱解码失败：{e}"}, 500


def do_analyze(p: dict):
    """复盘分析：给某一手，返回「按真实局面，AI 会怎么打 + 为什么」。

    与 /api/explain 的区别：局面取自真实牌谱（不重放重决策，不漂移），
    所以 AI 手与人类手都能分析 —— 人类手就是反事实：“这手换 AI 来打会怎样”。
    入参：{sid, ply} 或 {initial_hands, first_player, opts, moves, ply, mode}
    """
    try:
        detail, states, cfg, ai_seat, mode = _resolve_replay(p)
        if not detail:
            return {"error": "本局还没有可分析的出牌"}, 400
        ply = int(p.get("ply") or len(detail))
        if not (1 <= ply <= len(detail)):
            return {"error": f"ply 超出范围（1..{len(detail)}）"}, 400
        d, st = detail[ply - 1], states[ply - 1]
        seat = int(d["seat"])
        trick = st["trick"]
        my_hand = list(st["hand_ids"])
        opp_n = int(st["n"][1 - seat])
        history = [{"seat": 0 if x["seat"] == seat else 1, "move": list(x["cards"]),
                    "pass_on": x["pass_on"]} for x in detail[:ply - 1]]
        payload = {"my_hand": my_hand, "opp_n": opp_n, "trick": trick,
                   "history": history, "explain": True, "mode": mode}
        res, code = do_decide(payload)
        if code != 200:
            return res, code
        ex = res.get("explain") or {}
        decided = [int(c) for c in (res.get("move") or [])]
        rec = [int(c) for c in d["cards"]]
        return {
            "ply": ply, "seat": seat, "ai_seat": ai_seat, "mode": res.get("mode"),
            "engine": res.get("engine"), "my_n": len(my_hand), "opp_n": opp_n,
            "state_text": describe_state(counts_of_ids(my_hand), opp_n, trick),
            "recorded": {"cards": rec, "patText": pattern_text(d["code"], cfg) if d["code"] else "不出",
                         "pass": bool(d["pass"])},
            "decided": {"cards": decided, "patText": ex.get("text") or ("不出" if not decided else ""),
                        "pass": bool(res.get("pass")) or not decided},
            "agree": decided == rec,
            "explain": ex,
            "note": explain_note(ex, len(my_hand), opp_n),
        }, 200
    except Exception as e:
        return {"error": f"复盘分析失败：{e}"}, 500


def do_decide(p: dict):
    """无状态单步决策（pdk-ai 生产内核原生接口），供双真人模式的深度提示使用。

    支持可选 "mode": "hybrid"|"dual" 与生产双模式对齐（默认 hybrid=生产配置）。
    失败直接返回 500 错误（不降级）；仅“不适用”时返回 cards:null + reason。
    """
    try:
        mode = str(p.get("mode", "hybrid")).lower()
        engine = PROD_MODES.get(mode, "c")
        with (_ACT_SEM or _NULL_SEM):
            if engine == "c":
                out = bot_server._decide(p)                 # 官方实现（hybrid）
            else:
                out = _decide_with_engine(p, engine=engine)  # dual: 残局数值计分接力
        out["mode"] = "hybrid" if engine == "c" else mode
        out["engine"] = engine
        try:
            out["openingSearch"] = prod_config_info().get("openingSearch", None)
        except Exception:
            pass
        return out, 200
    except Exception as e:
        return {"error": f"decide 失败（不降级）：{e}"}, 500


def _decide_with_engine(payload: dict, engine: str) -> dict:
    """与生产 _decide 同源，仅 SolverAgent 的 engine 可选（dual=残局数值计分）。"""
    from pdk.belief import Belief, enumerate_hands
    from pdk.core import COPIES, N_RANKS, rank_of
    from pdk.agents import SolverAgent

    my_ids = payload.get("my_hand") or []
    opp_n = int(payload.get("opp_n", 0))
    trick = payload.get("trick")
    history = payload.get("history") or []
    if not my_ids or opp_n < 0:
        raise ValueError("my_hand 与 opp_n 必填")
    my_cnt = [0] * N_RANKS
    for c in my_ids:
        my_cnt[rank_of(c)] += 1
    played_me = [0] * N_RANKS
    played_opp = [0] * N_RANKS
    pass_events = []
    for h in history:
        seat = h.get("seat")
        mv = h.get("move") or []
        tgt = played_me if seat == 0 else played_opp
        for c in mv:
            tgt[rank_of(c)] += 1
        if not mv and seat == 1 and h.get("pass_on"):
            pass_events.append(tuple(h["pass_on"]))
    unseen = [COPIES[r] - my_cnt[r] - played_me[r] - played_opp[r] for r in range(N_RANKS)]
    if any(u < 0 for u in unseen) or sum(unseen) < opp_n:
        raise ValueError("history 与手牌/对手张数不一致")
    belief = Belief(my_cnt, opp_n, bot_server.CFG)
    belief.rows = enumerate_hands(unseen, opp_n)
    import numpy as np
    belief.weights = np.ones(len(belief.rows), dtype=np.float64)
    belief.opp_n = opp_n
    for t4 in pass_events:
        belief.update_pass(t4)
    ag = SolverAgent(bot_server._QFB(), bot_server.CFG, total_threshold=28,
                     max_rows=400000, engine=engine)
    ag.new_game(0, my_cnt, [0] * N_RANKS)
    ag.belief = belief
    ag.oracle_cnt = None
    world = belief.rows[0].tolist() if len(belief.rows) else [0] * N_RANKS
    t4 = tuple(trick) if trick else None
    cg = fast.CGame(my_cnt, world, 0, bot_server.CFG)
    if t4 is not None:
        cg.g.trick[0], cg.g.trick[1] = t4[0], t4[1]
        cg.g.trick[2], cg.g.trick[3] = t4[2], t4[3]
    else:
        cg.g.trick[0] = 255
    legal = cg.legal()
    mv_code = ag.act(cg)
    cards = list(fast.code_to_cards(mv_code, sorted(my_ids))) if mv_code else []
    out = {"move": cards, "pass": mv_code == 0, "legal_count": len(legal)}
    if payload.get("explain"):
        out["explain"] = getattr(ag, "last_explain", {})     # 与生产 /api/decide 一致的 explanation
    return out


class Handler(BaseHTTPRequestHandler):
    def _json(self, obj, code=200):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")   # 网页版跨域调用
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(data)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        try:
            u = urlparse(self.path)
            if u.path == "/health":
                with LOCK:
                    n = len(SESSIONS)
                self._json({"ok": True, "agent": "prod",
                            "productionModel": "ckpt/policy_a2c_final56.pt",
                            "modes": sorted(PROD_MODES),
                            "botRoot": str(BOT_ROOT), "sessions": n, "v": 7,
                            "maxSessions": MAX_SESSIONS,
                            "liveSessions": sum(1 for x in SESSIONS.values()
                                                if not getattr(x.game, "finished", False)),
                            "productionConfig": prod_config_info(),
                            "netProbe": net_probe(),
                            "assets": verify_assets()})
            elif u.path == "/stats":
                with LOCK:
                    sess = list(SESSIONS.values())
                agg = {"sessions": len(sess), "openingSearch": 0, "openingTime": 0.0,
                       "oneShot": 0, "endgameOrder": 0, "reportDump": 0, "nets": {}}
                for s in sess:
                    agg["nets"][getattr(s, "net", "?")] = agg["nets"].get(getattr(s, "net", "?"), 0) + 1
                for s in sess:
                    st = getattr(s, "last_stats", {}) or {}
                    agg["openingSearch"] += int(st.get("opening_search", 0))
                    agg["openingTime"] += float(st.get("opening_time", 0.0))
                    agg["oneShot"] += int(st.get("one_shot", 0))
                    agg["endgameOrder"] += int(st.get("endgame_order", 0))
                    agg["reportDump"] += int(st.get("report_dump", 0))
                agg["openingTime"] = round(agg["openingTime"], 2)
                self._json(agg)
            elif u.path == "/legal":
                from urllib.parse import parse_qs
                q = parse_qs(u.query)
                s = get_sess(q["sid"][0])
                with s.lock:
                    seat = int(q["seat"][0]) if "seat" in q else int(s.game.turn)
                    self._json({"seat": seat, "turn": int(s.game.turn),
                                "legal": s.legal_ids(seat)})
            else:
                self._json({"error": "unknown endpoint"}, 404)
        except Exception as e:
            import traceback
            self._json({"error": str(e), "tb": traceback.format_exc()[-1200:]}, 500)

    def do_POST(self):
        try:
            n = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(n) or b"{}")
            u = urlparse(self.path)
            if u.path == "/init":
                body, code = handle_init(payload)
            elif u.path == "/action":
                ok, info = do_action(payload["sid"],
                                     int(payload.get("seat", 0)),
                                     payload.get("cards", []))
                body, code = (({"ok": True, **(info if isinstance(info, dict) else {})}, 200)
                              if ok else ({"error": info}, 400))
            elif u.path == "/act":
                body, code = do_act(payload["sid"])
            elif u.path == "/suggest":
                body, code = do_suggest(payload["sid"])
            elif u.path == "/api/decide":
                body, code = do_decide(payload)
            elif u.path == "/api/explain":
                body, code = do_explain(payload)
            elif u.path == "/api/analyze":
                body, code = do_analyze(payload)
            elif u.path == "/api/decode":
                body, code = do_decode(payload)
            else:
                body, code = {"error": "unknown endpoint"}, 404
            self._json(body, code)
        except KeyError as e:
            self._json({"error": f"missing {e}"}, 400)
        except Exception as e:
            import traceback
            self._json({"error": str(e), "tb": traceback.format_exc()[-1500:]}, 500)


def preflight():
    """启动预检：生产模型必须加载成功（不降级），并预热一次，避免首个请求慢。"""
    t0 = time.time()
    try:
        fb = bot_server._QFB()
    except Exception as e:
        print(f"[bridge] FATAL 生产模型加载失败：{type(e).__name__}: {e}", file=sys.stderr, flush=True)
        sys.exit(1)
    if getattr(fb, "use_x", False) is not True:
        print("[bridge] FATAL final56 未加载成功（pdk 内部回退旧网络）——不降级，拒绝启动",
              file=sys.stderr, flush=True)
        sys.exit(1)
    print(f"[bridge] production model READY: {net_info(fb)} ({time.time()-t0:.1f}s)", flush=True)


def restore_sessions():
    """跨重启恢复：把仍标记 live 的对局按动作历史重建为会话（同一 sid/编号），
    这样重启（部署/升级）不会打断正在进行的对局。"""
    global RESTORING
    n = 0
    for f in REPLAY_DIR.glob("*.json"):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not d.get("live"):
            continue
        sid = str(d.get("sid") or f.stem)
        if sid in SESSIONS:
            continue
        try:
            s = Shadow(d["hands"], d.get("kitty", []), d.get("leader", 0),
                       d.get("opts", {}), d.get("mode", "hybrid"), sid=sid,
                       reuse_no=d.get("no"), reuse_file=f.name)
            s.restoring = True                       # 重放期间不写盘
            try:
                for code in d.get("codes", []):
                    s._apply(code)
            finally:
                s.restoring = False
            if s.game.finished:
                write_replay(s, live=False)          # 恢复时才发现已结束 -> 补写终局
                continue
            write_replay(s, live=True)               # 重放完成后写一次
            with LOCK:
                SESSIONS[sid] = s
            n += 1
        except Exception as e:
            print(f"[bridge] 恢复会话 {sid} 失败：{type(e).__name__}: {e}", file=sys.stderr, flush=True)
    RESTORING = False
    if n:
        print(f"[bridge] restored {n} live session(s) across restart", flush=True)


class BridgeServer(ThreadingHTTPServer):
    # Linux 需要 SO_REUSEADDR 以避免 TIME_WAIT 阻止重启；Windows 关闭以防双实例抢连接。
    allow_reuse_address = (os.name != 'nt')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8766)
    ap.add_argument("--bot-root", default=str(DEFAULT_BOT_ROOT))
    args = ap.parse_args()

    global _ACT_SEM
    _ACT_SEM = threading.Semaphore(max(2, (os.cpu_count() or 2)))
    preflight()                                   # 不降级：模型未就绪直接退出（systemd 会拉起并留痕）
    cleanup_stale_live()                          # 先清理超时僵尸局，只恢复真正在进行的对局
    srv = BridgeServer(("127.0.0.1", args.port), Handler)
    print(f"AI bridge on http://127.0.0.1:{args.port}  (bot root: {BOT_ROOT})", flush=True)
    threading.Thread(target=restore_sessions, daemon=True).start()   # 端口先就绪，会话后台恢复
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
