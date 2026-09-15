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
  POST /api/new_game {opts?,mode?}  -> 整局对局开新局（调用方坐座位0，AI 坐座位1；返回时必轮到调用方）
  POST /api/play     {gid,cards}    -> 调用方出一手（[] 过牌），AI 连续应手到轮回调用方或终局
  GET  /api/state?gid=...           -> 整局对局局面（只读）
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
import random
import re
import subprocess
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
from pdk.core import SPADE_3                       # noqa: E402
from pdk.explain import PTYPE_CN, describe_state, pattern_text, rank_name  # noqa: E402
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


_PDK_COMMIT = {"done": False, "hash": "", "date": "", "subject": ""}


def pdk_commit_info() -> dict:
    """生产 AI 仓库（pdk-ai）当前提交：前端"模型版本号"与对外开放的版本标识。"""
    if not _PDK_COMMIT["done"]:
        try:
            r = subprocess.run(
                ["git", "-C", str(BOT_ROOT), "log", "-1", "--format=%h|%cs|%s"],
                capture_output=True, text=True, timeout=8)
            if r.returncode == 0 and r.stdout.strip():
                parts = (r.stdout.strip().split("|", 2) + ["", ""])[:3]
                _PDK_COMMIT.update({"hash": parts[0], "date": parts[1],
                                    "subject": parts[2][:120]})
        except Exception as e:
            _PDK_COMMIT.update({"hash": "", "date": "",
                                "subject": f"({type(e).__name__})"})
        _PDK_COMMIT["done"] = True
    return {k: v for k, v in _PDK_COMMIT.items() if k != "done"}


def prod_config_info() -> dict:
    """生产推理配置自述：开局搜索是否开启（线上确认用）。"""
    try:
        import inspect
        from pdk.agents import SolverAgent
        pr = inspect.signature(SolverAgent.__init__).parameters
        info = {
            "openingSearch": bool(pr["opening_search"].default),   # 桥未覆盖 → 生效=类默认
            "openingWorlds": int(pr["opening_worlds"].default),
            "totalThreshold": 28,        # 桥显式传入（与生产 build_ai 同值）
            "overrides": "total_threshold=28, max_rows=400000",
            "commit": pdk_commit_info().get("hash", ""),
        }
        if "opening_budget" in pr:       # 新版开局搜索时间预算（9cf7a7a+）
            info["openingBudget"] = float(pr["opening_budget"].default)
        return info
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

PUBLIC_MODE = False          # --public-api：只暴露"无状态 + 整局对局"接口，不暴露网页版会话协议
REPLAY_ON = True            # --no-replay 关闭对局落盘（公共实例可选）
MAX_BODY = 1 << 20           # 请求体上限 1MB（超出 413）
MAX_HISTORY = 500            # history/moves 条数上限（防超大请求打内存）
MAX_HANDS_N = 20             # my_hand 张数上限（正常 16）
DECIDE_TIMEOUT = 30.0        # 决策排队超时（秒）→ 503（对外服务不无限排队）
IDLE_TTL = 4 * 3600          # 会话空闲回收（秒）
FINISHED_TTL = 1800          # 已结束会话回收（秒）
PLAY_MAX_AI_PLY = 64         # 一次 /api/play 内 AI 连续行动上限（防死循环）


class ApiError(Exception):
    """带状态码的接口错误：4xx 属调用方问题，5xx 属服务问题（不再一律 500）。"""

    def __init__(self, msg: str, status: int = 400, extra: dict | None = None):
        super().__init__(msg)
        self.status = int(status)
        self.extra = dict(extra or {})


class SessionNotFound(ApiError):
    def __init__(self, sid: str = ""):
        super().__init__(f"会话不存在或已过期（sid={sid}）", 404)


class Busy(ApiError):
    def __init__(self, msg: str = "服务繁忙，请稍后重试"):
        super().__init__(msg, 503, {"Retry-After": "5"})


_ID_RE = re.compile(r"[A-Za-z0-9_-]{4,64}")
_FILE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
_NO_RE = re.compile(r"[AH][0-9]{3,6}")


def check_ident(v, what: str = "sid") -> str:
    """sid/gid 校验：仅 [A-Za-z0-9_-]，4..64 位（同时挡住路径穿越与超长键）。"""
    s = str(v or "")
    if not _ID_RE.fullmatch(s):
        raise ApiError(f"{what} 格式非法（仅允许字母数字与 _-，长度 4..64）")
    return s


def check_file(v) -> str:
    """对局文件名校验：禁止 .. 与路径分隔符（该值来自请求，属路径穿越风险点）。"""
    s = str(v or "")
    if ".." in s or not _FILE_RE.fullmatch(s):
        raise ApiError("file 名非法（仅允许字母数字 . _ -，且不含 ..）")
    return s


def check_no(v) -> str:
    s = str(v or "")
    if not _NO_RE.fullmatch(s):
        raise ApiError("对局编号非法（形如 A0001 / H0002）")
    return s


def check_cards(cards) -> list:
    """出牌数组校验：必须是 0..48 的整数数组（挡字符串/浮点/越界值进内核）。"""
    if cards is None:
        return []
    if not isinstance(cards, (list, tuple)):
        raise ApiError("cards 必须是数组（牌 id 列表，[] 表示过牌）")
    out = []
    for c in cards:
        if isinstance(c, bool) or not isinstance(c, int):
            raise ApiError("cards 必须是整数数组（牌 id）")
        if not (0 <= c <= 48):
            raise ApiError(f"牌 id 越界: {c}（合法范围 0..48）")
        out.append(int(c))
    return out


def check_opts(o) -> dict:
    if o in (None, ""):
        return {}
    if not isinstance(o, dict):
        raise ApiError("opts 必须是对象")
    bad = [k for k in o if k not in ("red10", "four3", "nobomb", "sanzhang")]
    if bad:
        raise ApiError("未知 opts 键: " + ", ".join(map(str, bad)))
    return {k: bool(v) for k, v in o.items()}


@contextlib.contextmanager
def decide_gate(timeout: float | None = None):
    """决策并发闸：带超时，外部请求不无限排队（超时 503 + Retry-After）。"""
    sem = _ACT_SEM
    if sem is None:
        yield
        return
    t = DECIDE_TIMEOUT if timeout is None else float(timeout)
    if not sem.acquire(timeout=t):
        raise Busy(f"决策排队超时（{t:.0f}s），服务繁忙")
    try:
        yield
    finally:
        sem.release()


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
    if not REPLAY_ON:
        return
    try:
        fn = check_file(s.file)                      # 二次防御：文件名不得越出 REPLAY_DIR
        path = (REPLAY_DIR / fn).resolve()
        if path.parent != REPLAY_DIR.resolve():
            raise ApiError("replay 路径越界")
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except (OSError, ApiError) as e:
        print(f"[bridge] replay 写盘跳过: {type(e).__name__}: {e}", file=sys.stderr, flush=True)


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
        self.last_used = self.created                     # 空闲回收依据
        self.sid = check_ident(sid or uuid.uuid4().hex[:12], "sid")
        self.no = check_no(reuse_no) if reuse_no else allocate_no("A")            # 开局即定编号
        self.file = check_file(reuse_file) if reuse_file else f"{self.sid}.json"  # 重同步复用同一文件
        self.time_text = time.strftime("%Y-%m-%d %H:%M:%S")
        self.ts_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self.cfg = build_cfg(opts)
        engine = PROD_MODES.get(prod_mode, "c")
        self.prod_mode = "hybrid" if engine == "c" else prod_mode
        h0, h1, k = sorted(hands[0]), sorted(hands[1]), sorted(kitty)
        if not (len(h0) == len(h1) == 16):
            raise ApiError(f"两手牌各需 16 张（当前 {len(h0)}/{len(h1)}）")
        if k and len(k) != 16:
            raise ApiError(f"底牌需 16 张（当前 {len(k)}）")
        if sorted(h0 + h1 + k) != sorted(bot_server.DECK):
            raise ApiError("手牌+底牌必须恰好构成 48 张固定牌库（牌 id 见文档 §2.1）")
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

    def touch(self):
        """记录最近活动时间（空闲 TTL 回收用）。"""
        self.last_used = time.time()

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
                                  "cards": cards, "combo": combo, "pass": code == 0,
                                  "pass_on": (list(trick_before) if (code == 0 and trick_before)
                                              else None)})
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
        s.touch()
        return s
    if RESTORING:
        raise ApiError("会话恢复中（服务刚重启），请稍后重试", 503, {"Retry-After": "3"})
    raise SessionNotFound(sid)


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
    if not isinstance(hands, (list, tuple)) or len(hands) != 2:
        raise ApiError("hands 必须是 [座位0手牌, 座位1手牌]")
    h0, h1 = check_cards(hands[0]), check_cards(hands[1])
    kitty = check_cards(p.get("kitty") or [])
    if not (len(h0) == len(h1) == 16):
        raise ApiError(f"两手牌各需 16 张（当前 {len(h0)}/{len(h1)}）")
    if kitty and len(kitty) != 16:
        raise ApiError(f"底牌需 16 张（当前 {len(kitty)}）")
    if len(set(h0 + h1 + kitty)) != len(h0 + h1 + kitty):
        raise ApiError("手牌/底牌存在重复牌 id")
    try:
        leader = int(p.get("leader", 0))
    except (TypeError, ValueError):
        raise ApiError("leader 必须是 0 或 1")
    if leader not in (0, 1):
        raise ApiError("leader 必须是 0 或 1")
    opts = check_opts(p.get("opts"))
    prod_mode = str(p.get("mode", "hybrid")).lower()
    if prod_mode not in PROD_MODES:
        return {"error": f"unknown mode '{prod_mode}' (choose hybrid|dual)"}, 400
    cleanup_stale_live()
    sid = check_ident(p.get("sid"), "sid") if p.get("sid") else uuid.uuid4().hex[:12]
    s = Shadow([h0, h1], kitty, leader, opts, prod_mode, sid=sid,
               reuse_no=p.get("no"), reuse_file=p.get("file"))
    with LOCK:
        SESSIONS[sid] = s
    evict_old()
    write_replay(s, live=True)                            # 开局即写（含编号 + 初始手牌）
    # 可选：创建后即重放一串动作（断线重同步用）
    actions = p.get("actions") or []
    if len(actions) > MAX_HISTORY:
        SESSIONS.pop(sid, None)
        raise ApiError(f"actions 过长（{len(actions)} > {MAX_HISTORY}）")
    for mv in actions:
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
    sid = check_ident(sid, "sid")
    if seat not in (0, 1):
        return False, "seat 必须是 0 或 1"
    try:
        cards = check_cards(cards)
    except ApiError as e:
        return False, str(e)
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
            with decide_gate():                  # 并发闸（带超时）：多局并发排队，外部请求不无限等
                code = s.agent.act(s.cg)
            code = _legal_or_fallback(s, code)   # 内核非法手防御（不写非法手进牌谱）
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
        except ApiError:
            raise                                    # 503 排队超时等要保留状态码
        except Exception as e:
            import traceback
            tb = traceback.format_exc()
            return {"error": str(e), "tb": tb[-1200:]}, 500
        return {"cards": cards, "finished": bool(s.game.finished)}, 200


def do_suggest(p):
    """给"当前出牌方"的建议（网页版提示 / 整局对局里调用方自己的手），不落子。

    入参：{"sid"|"gid": ..., "explain": true?}（兼容旧调用 do_suggest(sid)）。
    实现：直接用**本会话真实局面**构造单步决策（规则 opts 与本局完全一致），
    不再重放整局、不再新建 Agent —— 既快又不会与对局规则脱节。
    """
    if isinstance(p, str):
        p = {"sid": p}
    sid = check_ident(p.get("sid") or p.get("gid"), "sid")
    s = get_sess(sid)
    with s.lock:
        if s.bad:
            return {"error": "影子牌局已失效（不降级，请重开本局）"}, 500
        if s.game.finished:
            return {"cards": None, "reason": "本局已结束"}, 200
        seat = int(s.cg.g.turn)
        payload = {
            "my_hand": list(s.game.hand_ids(seat)),
            "opp_n": int(s.game.hand_count(1 - seat)),
            "trick": (None if s.cg.g.trick[0] == 255 else [int(x) for x in s.cg.g.trick]),
            "history": [{"seat": 0 if d["seat"] == seat else 1, "move": list(d["cards"]),
                         "pass_on": d.get("pass_on")} for d in s.moves_detail],
            "explain": bool(p.get("explain")),
            "mode": s.prod_mode,
        }
        cfg, mode = s.cfg, s.prod_mode
    with decide_gate():
        out = _decide_core(payload, cfg, PROD_MODES.get(mode, "c"))
    res = {"cards": out.get("move") or [], "pass": bool(out.get("pass")),
           "seat": seat, "mode": mode, "legal_count": out.get("legal_count")}
    if out.get("explain"):
        res["explain"] = out["explain"]
    return res, 200





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
            sid = check_ident(p.get("sid"), "sid")
            s = get_sess(sid)
            with s.lock:
                if s.bad:
                    return {"error": "影子牌局已失效"}, 500
                try:
                    ply = int(p.get("ply") or len(s.codes) or 0)
                except (TypeError, ValueError):
                    raise ApiError("ply 必须是整数（1 起）")
                if ply < 0:
                    raise ApiError("ply 必须是正整数")
                if ply > len(s.codes):
                    ply = len(s.codes)
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
                with decide_gate():
                    out = bot_server._explain_replay(payload)
                out["reqPly"] = ply
                out["anchored_back"] = bool(ai_ply != ply)
                st = states_at(out.get("ply"), s)
                out["note"] = explain_note(out, st[0], st[1])
                return out, 200
        else:
            payload = {k: v for k, v in p.items() if k != "mode"}
        with decide_gate():
            out = bot_server._explain_replay(payload)
        return out, 200
    except ApiError:
        raise
    except ValueError as e:
        # 上游重放类错误（如"ply 之前 AI 没有决策点""牌谱不合法"）属调用方参数问题
        return {"error": f"无法解释该手：{e}"}, 400
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
    """从手牌池里取出该 code 对应的具体牌 id（并移除）。

    严格校验：池子凑不出该 code 的点数组合时报错（否则会静默返回残缺牌面，
    让复盘/解码输出与实际不符）。"""
    cards = list(fast.code_to_cards(code, sorted(pool)))
    if fast.cards_to_code(cards) != code:
        raise ApiError(f"牌谱第 {code} 动作在手牌里凑不出（点数组合不足）")
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
                raise ApiError(f"第 {i + 1} 手前手牌不一致（座位{s2} 解码 {len(pools[s2])} 张，"
                               f"内核 {int(cg.g.n[s2])} 张）")
        given_seat = mv.get("seat") if isinstance(mv, dict) else None
        if given_seat is not None and int(given_seat) != seat:
            raise ApiError(f"第 {i + 1} 手座位与牌谱不符（牌谱 {given_seat}, 内核 {seat}）")
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
            raise ApiError(f"第 {i + 1} 手在牌谱上不合法（座位 {seat}）")
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


def check_initial_hands(hands):
    """初始手牌校验（decode/analyze/explain 直传共用）：16+16、牌 id 合法且不重复。"""
    if not hands or len(hands) != 2:
        raise ApiError("initial_hands 必填（两家初始手牌各 16 张）")
    h0, h1 = check_cards(hands[0]), check_cards(hands[1])
    if len(h0) != 16 or len(h1) != 16:
        raise ApiError(f"两家初始手牌各需 16 张（当前 {len(h0)}/{len(h1)}）")
    if len(set(h0 + h1)) != 32:
        raise ApiError("初始手牌存在重复牌 id")
    return h0, h1


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
    h0, h1 = check_initial_hands(hands)
    moves = p.get("moves") or p.get("codes") or []
    if not isinstance(moves, list):
        raise ApiError("moves 必须是数组（code 或 牌 id 数组）")
    if len(moves) > MAX_HISTORY:
        raise ApiError(f"moves 过长（{len(moves)} > {MAX_HISTORY}）")
    try:
        first = int(p.get("first_player", 0))
    except (TypeError, ValueError):
        raise ApiError("first_player 必须是 0 或 1")
    if first not in (0, 1):
        raise ApiError("first_player 必须是 0 或 1")
    detail, states, cfg = _replay_positions([h0, h1], first,
                                            check_opts(p.get("opts")), moves)
    ai_seat = int(p.get("ai_seat", 1))
    if ai_seat not in (0, 1):
        raise ApiError("ai_seat 必须是 0 或 1")
    mode = str(p.get("mode", "hybrid")).lower()
    if mode not in PROD_MODES:
        raise ApiError(f"未知 mode {mode!r}（可选 hybrid|dual）")
    return detail, states, cfg, ai_seat, mode


def do_decode(p: dict):
    """把牌谱解码成逐手明细（座位由内核判定；顺带给出真实牌面与牌型）。

    历史人机局的复盘文件只存动作码，前端拿不到“谁出的/具体哪些牌”→ 复盘手牌快照会错。
    入参：{initial_hands|hands, first_player, opts, moves|codes}
    """
    try:
        h0, h1 = check_initial_hands(p.get("initial_hands") or p.get("hands"))
        moves = p.get("moves") or p.get("codes") or []
        if not isinstance(moves, list):
            raise ApiError("moves 必须是数组（code 或 牌 id 数组）")
        if len(moves) > MAX_HISTORY:
            raise ApiError(f"moves 过长（{len(moves)} > {MAX_HISTORY}）")
        try:
            first = int(p.get("first_player", 0))
        except (TypeError, ValueError):
            raise ApiError("first_player 必须是 0 或 1")
        if first not in (0, 1):
            raise ApiError("first_player 必须是 0 或 1")
        detail, states, cfg = _replay_positions([h0, h1], first,
                                                check_opts(p.get("opts")), moves)
        return {"moves": [{
            "ply": d["ply"], "seat": d["seat"], "cards": d["cards"], "pass": d["pass"],
            "pass_on": d["pass_on"], "combo": d["combo"], "handAfter": d["handAfter"],
            "patText": pattern_text(d["code"], cfg) if d["code"] else "不出",
        } for d in detail]}, 200
    except ApiError:
        raise
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
        try:
            ply = int(p.get("ply") or len(detail))
        except (TypeError, ValueError):
            raise ApiError("ply 必须是整数（1 起）")
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
    except ApiError:
        raise
    except Exception as e:
        return {"error": f"复盘分析失败：{e}"}, 500


def do_decide(p: dict):
    """无状态单步决策（生产内核）：{my_hand, opp_n, trick, history, opts?, explain?, mode?}。

    规则由 opts 决定（默认 = 网页版默认规则：炸弹不可拆/红十翻倍/三张可接/四带三关闭），
    因此调用方与 AI 用同一套规则；失败直接报错（不降级）。
    """
    mode = str(p.get("mode", "hybrid")).lower()
    if mode not in PROD_MODES:
        raise ApiError(f"未知 mode {mode!r}（可选 hybrid|dual）")
    engine = PROD_MODES[mode]
    cfg = build_cfg(check_opts(p.get("opts")))
    with decide_gate():
        out = _decide_core(p, cfg, engine)
    out["mode"] = "hybrid" if engine == "c" else mode
    out["engine"] = engine
    try:
        out["openingSearch"] = prod_config_info().get("openingSearch", None)
    except Exception:
        pass
    return out, 200





def _check_trick(trick):
    """trick 校验：[ptype, main, len, nc] 或 null（=领出）。"""
    if trick in (None, "", [], "null"):
        return None
    if not isinstance(trick, (list, tuple)) or len(trick) != 4:
        raise ApiError("trick 必须是 [ptype, main, len, nc] 或 null")
    try:
        pt, main, ln, nc = (int(x) for x in trick)
    except (TypeError, ValueError):
        raise ApiError("trick 必须是 4 个整数")
    if not (0 <= pt <= 9) or not (0 <= main <= 12) or not (1 <= ln <= 20) or not (0 <= nc <= 8):
        raise ApiError("trick 取值越界（ptype 0..9 / main 0..12 / len 1..20 / nc 0..8）")
    return [pt, main, ln, nc]


def _decide_core(payload: dict, cfg, engine: str = "c") -> dict:
    """与 pdk-ai server._decide 同源的单步决策，但 **规则 cfg 与 engine 可选**。

    为什么自己实现：调用方（网页版/外部）可能带 opts（如"炸弹不可拆"），而上游 /api/decide
    用的是模块级 CFG（= Config() 默认）。这里把上游 _decide 的完整流程复刻一遍，关键点
    一个不少：belief 由 _replay_decide_history 增量重建（含对手 choice 似然更新）、
    played/plays_cnt/trick 一并还原进 CGame、fallback 的 last/opp_p/opening 门控与
    _lead_idx（残缺快照不伪开局）与生产一致、非法动作回退网络再回退首个合法手。
    """
    from pdk.agents import SolverAgent
    from pdk.core import N_RANKS

    my_ids = check_cards(payload.get("my_hand"))
    if not my_ids:
        raise ApiError("my_hand 必填（我方当前手牌，牌 id 数组）")
    if len(my_ids) > MAX_HANDS_N:
        raise ApiError(f"my_hand 张数过多（{len(my_ids)} > {MAX_HANDS_N}）")
    try:
        opp_n = int(payload.get("opp_n", -1))
    except (TypeError, ValueError):
        raise ApiError("opp_n 必须是整数")
    if not (0 <= opp_n <= 19):
        raise ApiError(f"opp_n 非法: {opp_n}（应为对手剩余张数 0..19）")
    history = payload.get("history") or []
    if not isinstance(history, list):
        raise ApiError("history 必须是数组")
    if len(history) > MAX_HISTORY:
        raise ApiError(f"history 过长（{len(history)} > {MAX_HISTORY}）")
    t4 = _check_trick(payload.get("trick"))

    st = bot_server._replay_decide_history(my_ids, opp_n, history, cfg, choice_alpha=0.4)
    belief, my_cnt = st["belief"], st["my_cnt"]

    fb = bot_server._QFB()
    fb.last = st["last_move"]
    fb.opp_p = st["last_was_pass"]
    fb.opening = (st["my_lead_count"] == 0) and (not st["incomplete"])

    ag = SolverAgent(fb, cfg, total_threshold=28, max_rows=400000, engine=engine)
    ag.new_game(0, my_cnt, [0] * N_RANKS)
    ag.belief = belief
    ag.oracle_cnt = None
    # 已领出次数写入门控（残缺快照绝不伪开局）
    ag._lead_idx = 10 ** 9 if st["incomplete"] else st["my_lead_count"]

    world = belief.rows[0].tolist()
    cg = fast.CGame(my_cnt, world, 0, cfg)
    for r in range(N_RANKS):
        cg.g.played[0][r] = st["played_me"][r]
        cg.g.played[1][r] = st["played_opp"][r]
    cg.g.plays_cnt[0] = st["plays_me"]
    cg.g.plays_cnt[1] = st["plays_opp"]
    if t4 is not None:
        cg.g.trick[0], cg.g.trick[1] = t4[0], t4[1]
        cg.g.trick[2], cg.g.trick[3] = t4[2], t4[3]
    else:
        cg.g.trick[0] = 255

    legal = cg.legal()
    mv_code = ag.act(cg)
    if mv_code not in set(legal):                    # 防御：非法 → 网络 → 首个合法手
        mv_code = fb.act(cg)
        if mv_code not in set(legal):
            mv_code = legal[0]
    cards = list(fast.code_to_cards(mv_code, sorted(my_ids))) if mv_code else []
    out = {"move": cards, "pass": mv_code == 0, "legal_count": len(legal)}
    if payload.get("explain"):
        out["explain"] = getattr(ag, "last_explain", {})
    return out


# ---------------- 整局对局 API（对外：服务端持局，AI 自动应手） ----------------

def _deal_new_game():
    """48 张洗牌 → 弃 16 张底牌 → 双方各 16 张；持黑桃3者先出（在底牌则随机）。"""
    deck = list(bot_server.DECK)                     # 固定 48 张牌库（确定性顺序）
    rnd = random.SystemRandom()
    rnd.shuffle(deck)
    h0, h1 = sorted(deck[16:32]), sorted(deck[32:48])
    kitty = sorted(deck[:16])
    if SPADE_3 in h0:
        leader = 0
    elif SPADE_3 in h1:
        leader = 1
    else:
        leader = rnd.randrange(2)
    return h0, h1, kitty, leader


def _trick_text(trick):
    if trick is None:
        return None
    pt, main = int(trick[0]), int(trick[1])
    return f"{PTYPE_CN.get(pt, pt)}{rank_name(main)}"


def _snapshot(s: Shadow, me: int = 0, new: bool = False) -> dict:
    """整局对局快照（字段与 pdk-ai 原生 /api/new_game 对齐，便于调用方复用文档与代码）。"""
    g, cg = s.game, s.cg
    turn, finished = int(cg.g.turn), bool(g.finished)
    legal = s.legal_ids(me) if (not finished and turn == me) else []
    trick = None if cg.g.trick[0] == 255 else [int(x) for x in cg.g.trick]
    snap = {
        "gid": s.sid,
        "you_are": me,
        "ai_seat": s.ai_seat,
        "turn": turn,
        "finished": finished,
        "winner": (int(g.winner) if g.winner is not None else None),
        "my_hand": sorted(g.hand_ids(me)),
        "my_n": int(g.hand_count(me)),
        "opp_n": int(g.hand_count(1 - me)),
        "trick": trick,
        "trick_text": _trick_text(trick),
        "last_moves": [{"seat": int(r.player), "cards": list(r.cards)} for r in g.log.plays[-6:]],
        "legal": legal,
        "legal_pass": any(len(x) == 0 for x in legal),
        "scores": list(g.scores) if g.scores else [0, 0],
        "mode": s.prod_mode,
        "opts": dict(s.init_payload.get("opts") or {}),
        "moves": len(s.moves_detail),
        "server": "ai_bridge",
    }
    if new:
        snap["new"] = True
    return snap


def _legal_or_fallback(s: Shadow, code: int) -> int:
    """内核给出的 code 必须落在本手合法集内。

    历史上（旧版镜像竞态）出现过内核基于过期局面给出"过牌"而当时并不允许过牌的情况，
    旧代码会宽容落子，把非法手写进牌谱并污染后续信念。这里改为：留痕 + 回退到最小合法手。
    """
    legal = set(s.cg.legal())
    if code in legal:
        return code
    pick = min(legal, key=lambda c: (fast.move_size(c), c)) if legal else code
    print(f"[bridge] WARN 内核给出非法手 code={code}（合法 {len(legal)} 个）→ 回退 {pick}",
          file=sys.stderr, flush=True)
    return pick


def _advance_ai(s: Shadow) -> int:
    """AI（座位1）连续应手，直到轮回调用方或终局。调用方须持有 s.lock。"""
    n = 0
    while (not s.game.finished) and int(s.cg.g.turn) == s.ai_seat and n < PLAY_MAX_AI_PLY:
        with decide_gate():
            code = s.agent.act(s.cg)
        code = _legal_or_fallback(s, code)           # 内核非法手防御
        cards = list(fast.code_to_cards(code, s.game.hand_ids(s.ai_seat)))
        s.capture_explain(code, cards)               # 明牌/复盘可即时读取
        s._apply(code)
        n += 1
    s.snapshot_stats()
    if s.game.finished:
        save_replay(s.sid, s)
    return n


def do_new_game(p: dict):
    """开新局（整局对局 API）：服务端发牌；调用方固定坐座位0，AI 坐座位1。

    返回时**保证轮到调用方**（若 AI 持黑桃3先手，它的开局动作已在服务端走完）。
    """
    opts = check_opts(p.get("opts"))
    mode = str(p.get("mode") or "hybrid").lower()
    if mode not in PROD_MODES:
        raise ApiError(f"未知 mode {mode!r}（可选 hybrid|dual）")
    h0, h1, kitty, leader = _deal_new_game()
    gid = uuid.uuid4().hex[:24]                      # 96 位随机，防枚举
    s = Shadow([h0, h1], kitty, leader, opts, prod_mode=mode, sid=gid)
    with LOCK:
        SESSIONS[gid] = s
    evict_old()
    write_replay(s, live=True)
    with s.lock:
        _advance_ai(s)
        return _snapshot(s, new=True), 200


def do_play(p: dict):
    """调用方（座位0）出一手；随后 AI 连续应手直到轮回调用方或终局。

    幂等语义：若上一次调用在 AI 应手中途中断（AI 异常/超时），**原样重发本请求**即可继续——
    此时轮次已是 AI，服务只补完 AI 的应手，不会重复落下你那一手。
    """
    gid = check_ident(p.get("gid"), "gid")
    s = get_sess(gid)
    with s.lock:
        if s.bad:
            raise ApiError("影子牌局已失效（不降级策略，请重开一局）", 500)
        if s.game.finished:
            return _snapshot(s), 200
        turn = int(s.cg.g.turn)
        if turn == s.ai_seat:                        # 上次中断 → 补完 AI 应手
            _advance_ai(s)
            return _snapshot(s), 200
        if turn != 0:
            raise ApiError(f"现在不是你的回合（turn={turn}）", 409)
        cards = check_cards(p.get("cards"))
        code = fast.cards_to_code(cards) if cards else PASS_CODE
        if code not in set(s.cg.legal()):
            raise ApiError("非法出牌（不在合法动作集内）")
        s._apply(code)
        _advance_ai(s)
        return _snapshot(s), 200


def do_state(gid):
    """查询整局对局局面（只读，不推进 AI）。"""
    gid = check_ident(gid, "gid")
    s = get_sess(gid)
    with s.lock:
        if s.bad:
            raise ApiError("影子牌局已失效（不降级策略，请重开一局）", 500)
        return _snapshot(s), 200


def session_sweeper(interval: float = 300.0):
    """空闲会话回收：进行中 > IDLE_TTL、已结束 > FINISHED_TTL 即释放（防内存堆积）。"""
    while True:
        time.sleep(interval)
        now = time.time()
        victims = []
        with LOCK:
            for k, s in list(SESSIONS.items()):
                idle = now - float(getattr(s, "last_used", getattr(s, "created", now)))
                ttl = FINISHED_TTL if getattr(s.game, "finished", False) else IDLE_TTL
                if idle > ttl:
                    victims.append(k)
            for k in victims:
                SESSIONS.pop(k, None)
        if victims:
            print(f"[bridge] 空闲回收 {len(victims)} 个会话", flush=True)





PUBLIC_GET = {"/health", "/api/state"}
PUBLIC_POST = {"/api/decide", "/api/explain", "/api/analyze", "/api/decode",
               "/api/new_game", "/api/play", "/api/suggest"}


class Handler(BaseHTTPRequestHandler):
    def _json(self, obj, code=200, headers: dict | None = None):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")   # 网页版跨域调用
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        for k, v in (headers or {}).items():
            self.send_header(str(k), str(v))
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
            if PUBLIC_MODE and u.path not in PUBLIC_GET:
                return self._json({"error": "该接口不在公开实例上提供"}, 404)
            if u.path == "/health":
                with LOCK:
                    n = len(SESSIONS)
                body = {"ok": True, "agent": "prod",
                        "productionModel": "ckpt/policy_a2c_final56.pt",
                        "modes": sorted(PROD_MODES),
                        "sessions": n, "v": 8,
                        "restoring": bool(RESTORING),
                        "maxSessions": MAX_SESSIONS,
                        "liveSessions": sum(1 for x in SESSIONS.values()
                                            if not getattr(x.game, "finished", False)),
                        "pdkCommit": pdk_commit_info(),      # 生产 AI 仓库提交（模型版本号）
                        "productionConfig": prod_config_info(),
                        "netProbe": net_probe(),
                        "assets": verify_assets()}
                if PUBLIC_MODE:
                    body["public"] = True
                    body["botRoot"] = "(hidden)"
                    body["endpoints"] = sorted(PUBLIC_POST | PUBLIC_GET)
                else:
                    body["botRoot"] = str(BOT_ROOT)
                self._json(body)
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
            elif u.path == "/api/state":
                from urllib.parse import parse_qs
                q = parse_qs(u.query)
                if "gid" not in q:
                    raise ApiError("缺少 gid 参数")
                body, code = do_state(q["gid"][0])
                self._json(body, code)
            elif u.path == "/legal":
                from urllib.parse import parse_qs
                q = parse_qs(u.query)
                if "sid" not in q:
                    raise ApiError("缺少 sid 参数")
                s = get_sess(q["sid"][0])
                with s.lock:
                    try:
                        seat = int(q["seat"][0]) if "seat" in q else int(s.game.turn)
                    except (TypeError, ValueError):
                        raise ApiError("seat 必须是 0 或 1")
                    if seat not in (0, 1):
                        raise ApiError("seat 必须是 0 或 1")
                    self._json({"seat": seat, "turn": int(s.game.turn),
                                "legal": s.legal_ids(seat)})
            else:
                self._json({"error": "unknown endpoint"}, 404)
        except ApiError as e:
            self._json({"error": str(e)}, e.status, e.extra)
        except Exception as e:
            import traceback
            tb = traceback.format_exc()
            print(f"[bridge] GET {self.path} 失败: {tb[-800:]}", file=sys.stderr, flush=True)
            body = {"error": f"{type(e).__name__}: {e}"}
            if not PUBLIC_MODE:
                body["tb"] = tb[-1200:]
            self._json(body, 500)

    def do_POST(self):
        try:
            try:
                n = int(self.headers.get("Content-Length", 0) or 0)
            except (TypeError, ValueError):
                n = 0
            if n > MAX_BODY:
                return self._json({"error": f"请求体过大（{n} 字节 > 上限 {MAX_BODY}）"}, 413)
            payload = json.loads(self.rfile.read(n) or b"{}")
            if not isinstance(payload, dict):
                raise ApiError("请求体必须是 JSON 对象")
            u = urlparse(self.path)
            if PUBLIC_MODE and u.path not in PUBLIC_POST:
                return self._json({"error": "该接口不在公开实例上提供"}, 404)
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
            elif u.path in ("/suggest", "/api/suggest"):
                body, code = do_suggest(payload)
            elif u.path == "/api/new_game":
                body, code = do_new_game(payload)
            elif u.path == "/api/play":
                body, code = do_play(payload)
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
        except ApiError as e:
            self._json({"error": str(e)}, e.status, e.extra)
        except KeyError as e:
            self._json({"error": f"缺少参数 {e}"}, 400)
        except json.JSONDecodeError as e:
            self._json({"error": f"JSON 解析失败: {e}"}, 400)
        except Exception as e:
            import traceback
            tb = traceback.format_exc()
            print(f"[bridge] POST {self.path} 失败: {tb[-800:]}", file=sys.stderr, flush=True)
            body = {"error": f"{type(e).__name__}: {e}"}
            if not PUBLIC_MODE:
                body["tb"] = tb[-1500:]
            self._json(body, 500)


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
    ap.add_argument("--public-api", action="store_true",
                    help="对外实例：只暴露无状态接口 + 整局对局接口（不含网页版会话协议）")
    ap.add_argument("--replay-dir", default="", help="对局落盘目录（对外实例建议 data/external）")
    ap.add_argument("--no-replay", dest="replay", action="store_false", default=True,
                    help="不落盘对局文件")
    ap.add_argument("--max-sessions", type=int, default=0, help="会话上限（默认 500；对外实例 64）")
    ap.add_argument("--idle-ttl", type=float, default=0.0, help="会话空闲回收秒数")
    ap.add_argument("--decide-timeout", type=float, default=0.0, help="决策排队超时秒数")
    args = ap.parse_args()

    global _ACT_SEM, PUBLIC_MODE, MAX_SESSIONS, REPLAY_DIR, IDLE_TTL, DECIDE_TIMEOUT, REPLAY_ON
    PUBLIC_MODE = bool(args.public_api)
    REPLAY_ON = bool(args.replay)
    if args.max_sessions:
        MAX_SESSIONS = max(1, int(args.max_sessions))
    elif PUBLIC_MODE:
        MAX_SESSIONS = 64
    IDLE_TTL = float(args.idle_ttl) if args.idle_ttl else (2 * 3600 if PUBLIC_MODE else 4 * 3600)
    if args.decide_timeout:
        DECIDE_TIMEOUT = float(args.decide_timeout)
    if args.replay_dir:
        REPLAY_DIR = (Path(__file__).resolve().parent / args.replay_dir).resolve() \
            if not os.path.isabs(args.replay_dir) else Path(args.replay_dir).resolve()
        REPLAY_DIR.mkdir(parents=True, exist_ok=True)
    _ACT_SEM = threading.Semaphore(1 if PUBLIC_MODE else max(2, (os.cpu_count() or 2)))
    preflight()                                   # 不降级：模型未就绪直接退出（systemd 会拉起并留痕）
    if not PUBLIC_MODE:
        cleanup_stale_live()                      # 先清理超时僵尸局，只恢复真正在进行的对局
    srv = BridgeServer(("127.0.0.1", args.port), Handler)
    print(f"AI bridge on http://127.0.0.1:{args.port}  (bot root: {BOT_ROOT})", flush=True)
    if PUBLIC_MODE:
        print(f"[bridge] 公开实例：{sorted(PUBLIC_POST | PUBLIC_GET)}  replay={REPLAY_ON} "
              f"dir={REPLAY_DIR} sessions<={MAX_SESSIONS} idleTTL={IDLE_TTL:.0f}s "
              f"decideTimeout={DECIDE_TIMEOUT:.0f}s", flush=True)
    else:
        threading.Thread(target=restore_sessions, daemon=True).start()   # 端口先就绪，会话后台恢复
    threading.Thread(target=session_sweeper, daemon=True).start()       # 空闲会话回收
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
