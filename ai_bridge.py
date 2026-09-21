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
import collections
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

# ---- 推理线程数：2 核机器上 torch 默认开满线程会互相抢核（小模型尤甚）。
# 实测同 25 个决策点：threads=2 平均 455ms/决策 -> threads=1 345ms（快 24%），
# 且 25/25 决策完全一致（单线程同时更确定，无浮点归约顺序差异）。不改算法与逻辑。
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")
try:
    import torch as _torch
    _torch.set_num_threads(1)
    try:
        _torch.set_num_interop_threads(1)
    except Exception:
        pass
except Exception:
    pass

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
# trick=[ptype,main,len,nc] 的语义校验（引擎侧唯一实现 pdk/trickguard.py）。
# 无状态接口必须过这一道：len 写错（典型: 连对传张数）不报错的话，引擎会按错的长度
# 去找解，静默退化成"只剩炸弹/过牌"，看起来像 AI 变保守。规则相关分支按调用方 opts。
from pdk.trickguard import parse_trick as _parse_trick   # noqa: E402

# 生产双模式 (pdk-ai README「当前生产模型与部署清单」):
#   生产模型 = ckpt/policy_a2c_final56.pt (A2C + 56维动作后特征)
#   hybrid = SolverAgent(hybrid, threshold=28) + _QFB(final56) + 规则层 R0-R3 —— 胜率优先(生产配置)
#   dual   = 同上但 SolverAgent(engine='dual', <=14张数值计分接力)      —— 积分制净分优先
PROD_MODES = ("hybrid", "dual")

# 口径统一 #3（2026-09-21）: 是否让 **agent 用当局 cfg**（而非引擎模块默认 Config()）。
# 默认 "0" = 现行为（agent 用 bot_server.CFG）；置 "1" 后 agent 与 CGame 同规则。
# 为什么要开关: 这是**行为改动**（改变 AI 内部模拟的规则），按纪律必须可 revert + A/B。
CFG_FROM_SESSION = __import__("os").environ.get("PDK_CFG_FROM_SESSION", "0") == "1"      # hybrid=上游生产配方（胜率优先→净分）；dual=同配方关胜率带（纯净分）


# 可选覆盖（默认严格跟随上游配方；用于 A/B 与上游修复后的快速验证）：
#   PDK_NODE_CAP=200000000        求解节点预算（注意：C 侧是**整批共享**预算）
#   PDK_EXACT_WORLDS_CAP=120      残局穷举世界上限（世界数×候选数 ≈ 批量单元数）
#   PDK_WIN_RATE_TOL=2.0          胜率容差带（越大越偏净分）
def _env_int(name):
    v = os.environ.get(name)
    try:
        return int(v) if v not in (None, "") else None
    except ValueError:
        return None


def _env_float(name):
    v = os.environ.get(name)
    try:
        return float(v) if v not in (None, "") else None
    except ValueError:
        return None


def prod_solver_kw() -> dict:
    """上游生产配方参数（server.PROD_SOLVER_KW）+ 可选环境覆盖。

    fca98a0 起上游把生产配方改为：engine='dual' + num_threshold<=16 数值计分 +
    exact_worlds_total<=16 残局穷举世界 + 盲顶牌偏置关闭。旧版内核没有该常量，
    这里自动退回旧参数（并在 /health 标注真实生效值），避免"悄悄用旧配方"。

    注意（已实测的上游缺陷）：`node_cap` 在 C 侧是**整批共享**预算，
    440 世界×8 候选≈3520 个求解单元时，生产默认 2M 只能解出约 0.4%，
    未解单元被跳过 ⇒ 期望分建立在有偏子集上、且结果随调用历史变化（不可复现）。
    若要让它真正生效，应同时放大预算并缩小世界集（见 README 的对外文档/评审结论）。
    """
    kw = getattr(bot_server, "PROD_SOLVER_KW", None)
    if not (isinstance(kw, dict) and kw):
        kw = {"total_threshold": 28, "max_rows": 400000, "engine": "c"}
    ncap = _env_int("PDK_NODE_CAP")
    if ncap:
        kw["node_cap"] = ncap
    ecap = _env_int("PDK_EXACT_WORLDS_CAP")
    if ecap:
        kw["exact_worlds_cap"] = ecap
    return kw


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
        kw = prod_solver_kw()
        info = {
            "nodeCap": int(kw.get("node_cap", 2_000_000)),
            "openingSearch": bool(pr["opening_search"].default),   # 桥未覆盖 → 生效=类默认
            "openingWorlds": int(pr["opening_worlds"].default),
            "totalThreshold": int(kw.get("total_threshold", 28)),
            "engine": kw.get("engine", "c"),                       # fca98a0 起生产用 dual
            "numThreshold": int(kw.get("num_threshold", 0)),       # ≤N 张走数值计分
            "exactWorldsTotal": int(kw.get("exact_worlds_total", 0)),   # ≤N 张残局穷举世界
            "exactWorldsCap": int(kw.get("exact_worlds_cap", 0)),
            "topPull": float(kw.get("top_pull", 0.0)),
            "topGuardOpp": int(kw.get("top_guard_opp", 0)),
            "overrides": "PROD_SOLVER_KW（上游生产配方）",
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


def build_prod_agent(mode: str = "hybrid", cfg=None):
    """按上游生产配方构建智能体（server.PROD_SOLVER_KW，与 server.build_ai 同源）。

    mode="hybrid"：上游生产配方（dual 引擎 + 残局穷举 + 数值计分；胜率优先、同胜率比净分）
    mode="dual"  ：同配方但关掉胜率带 → 纯期望净分（积分制场合）
    fallback 用官方 server._QFB()（final56 + R3）；checkpoint 缺失直接报错（不降级）。
    R0(一手走完)/R1(报单保权)/R2(残局连续保权)/R3(开局结构守护) 随 SolverAgent 无条件生效。
    """
    from pdk.agents import SolverAgent
    fb = bot_server._QFB()                     # 生产 fallback: final56 + R3
    if getattr(fb, "use_x", False) is not True:
        # 不降级策略：宁可报错，也不用旧 DMC 网络糊弄玩家
        raise RuntimeError("生产模型 ckpt/policy_a2c_final56.pt 未加载成功"
                           "（检测到 pdk 内部回退到旧网络）——按不降级策略拒绝服务")
    mode = mode if mode in PROD_MODES else "hybrid"
    # 口径统一 #3: 开关打开且给了当局 cfg 时，agent 与游戏同规则（否则维持现行为）
    _cfg = cfg if (CFG_FROM_SESSION and cfg is not None) else bot_server.CFG
    kw = prod_solver_kw()
    if mode == "dual":
        kw.setdefault("win_rate_tol", 2.0)     # hmm: 见下方 setattr（旧内核无此参数）
    try:
        agent = SolverAgent(fb, _cfg, **kw)
    except TypeError:
        # 旧内核不认新参数：退回最小公共集（并保留引擎选择）
        agent = SolverAgent(fb, _cfg, total_threshold=kw.get("total_threshold", 28),
                            max_rows=kw.get("max_rows", 400000),
                            engine=kw.get("engine", "c"))
    if mode == "dual":
        # 关掉"先按胜率筛"的容差带 → 候选池=全部，按期望净分选优
        try:
            agent.win_rate_tol = 2.0
        except Exception:
            pass
    return agent, mode, net_info(fb)


SESSIONS: dict = {}
LOCK = threading.Lock()
MAX_SESSIONS = 500          # 上限放宽；淘汰时优先丢弃已结束/最旧的会话，避免打断进行中的对局
_ACT_SEM = None             # 决策并发闸（在 main() 里按 CPU 初始化）
_NULL_SEM = contextlib.nullcontext()   # 导入期占位（无锁语义）
RESTORING = True                       # 启动恢复进行中（此期间的旧会话请求提示重试）
REPLAY_DIR = Path(__file__).resolve().parent / "data" / "replays"
REPLAY_DIR.mkdir(parents=True, exist_ok=True)

PUBLIC_MODE = False          # --public-api：只暴露"无状态 + 整局对局"接口，不暴露网页版会话协议
REPLAY_PREFIX = "A"         # 对局编号前缀（主站 A/H 由各自写入；对外实例用 E，便于统计区分）
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
_NO_RE = re.compile(r"[AHE][0-9]{3,6}")


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


_NO_USED: set = set()          # 本进程已发出的编号（含尚未落盘的）


def note_no_used(no: str):
    """登记编号（恢复会话复用旧号时也要登记，避免新局重复发出）。"""
    if no:
        with _NO_LOCK:
            _NO_USED.add(str(no))


def allocate_no(prefix: str = "A") -> str:
    """分配对局编号。

    只看目录扫描是不够的：编号在会话创建时分配、文件在之后才写出来，
    两个会话在同一秒创建时（多设备/多标签同时开局）后一个扫不到前一个的文件，
    会把同一个号发两次 —— 线上实测出现过两个 A1179，导致"复盘当局"按号找文件找错局。
    因此叠加进程内"已发出"集合，保证同进程内绝不复用；跨进程靠不同前缀/目录隔离。
    """
    with _NO_LOCK:
        no = next_replay_no(REPLAY_DIR, prefix)
        while no in _NO_USED:
            no = f"{prefix}{int(no[len(prefix):]) + 1:04d}"
        _NO_USED.add(no)
        return no


HEART_10 = 29                       # ♥10 的牌 id（rankIdx 7 × 4 + suit 1）；红十翻倍用


def compute_result(hands, moves, opts, winner) -> dict:
    """按网页版计分规则结算（与 app.js settle() 逐条对齐）。

    底分 = 输家剩余张数（剩 1 张不计分）；关门（输家一手未出）失分 ×2；
    未被压掉的炸弹每颗 ±10（后手用更大炸弹直接压掉才不算，隔了过牌重新领出不算压）；
    红桃十翻倍（opts.red10，持有者所在一方输赢 ×2）。

    为什么在桥里算：内核 Game.scores 对这套人类规则从不计分（恒 [0,0]），而网页版只把
    结果存本地。棋谱是对局记录/复盘的权威数据，必须自带可展示的结算结果。
    返回 dict（写进棋谱 result 字段；scores 取 delta）。任何异常由调用方兜底，不影响落盘。
    """
    loser = 1 - int(winner)
    played = [0, 0]
    bombs = []                                   # [{by, beaten}]
    prev_live = None                             # 上一手非过牌且中间无过牌（= 可被直接压）
    for m in moves or []:
        if not isinstance(m, dict) or m.get("pass") or not (m.get("cards")):
            prev_live = None
            continue
        seat = int(m.get("seat") or 0)
        played[seat] += len(m["cards"])
        combo = m.get("combo") or {}
        is_bomb = (combo.get("ptype") == 8)
        if is_bomb and prev_live is not None and (prev_live.get("combo") or {}).get("ptype") == 8:
            for b in reversed(bombs):            # 更大炸弹直接压掉上家炸弹：被压的不计分
                if not b["beaten"]:
                    b["beaten"] = True
                    break
        if is_bomb:
            bombs.append({"by": seat, "beaten": False})
        prev_live = m
    rem = max(0, len(hands[loser] or []) - played[loser])
    shut = played[loser] == 0
    base = 0 if rem == 1 else rem
    if shut:
        base *= 2
    surv = [b for b in bombs if not b["beaten"]]
    bw = sum(1 for b in surv if b["by"] == int(winner))
    bl = len(surv) - bw
    dW = base + 10 * (bw - bl)
    dL = -dW
    red_txt = ""
    if opts.get("red10"):
        holder = next((seat for seat in (0, 1) if HEART_10 in (hands[seat] or [])), None)
        if holder is not None:
            who = "你" if holder == 0 else "AI"
            if holder == int(winner):
                dW *= 2
                dL = -dW
            else:
                dL *= 2
                dW = -dL
            red_txt = f"{who} 持有红桃十，翻倍"
    delta = [0, 0]
    delta[int(winner)] = dW
    delta[loser] = dL
    return {"winner": int(winner), "loser": loser, "rem": rem, "shut": bool(shut),
            "base": base, "bombs": [bw, bl], "redTxt": red_txt, "delta": delta}


def write_replay(s: Shadow, live: bool):
    """写对局文件。开局即写（live=True，含编号），每手更新，终局改写 live=False。"""
    res = None
    if not live and getattr(s.game, "winner", None) is not None:
        try:
            res = compute_result(s.init_payload["hands"], getattr(s, "moves_detail", []),
                                 s.init_payload.get("opts") or {}, s.game.winner)
        except Exception as e:
            print(f"[bridge] 结算失败（{type(e).__name__}: {e}），棋谱无 result 字段",
                  file=sys.stderr, flush=True)
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
        # scores：人类规则的本局净分（delta）。内核 g.scores 对这套规则恒为 [0,0]，不再直写
        "scores": (None if live else (list(res["delta"]) if res else [0, 0])),
    }
    if res:
        payload["result"] = res
    if getattr(s, "caller", ""):                      # 外部调用方的对局：标注归属与耗时
        payload["caller"] = s.caller
        payload["api"] = True
        payload["startedTs"] = getattr(s, "ts_iso", "")
        payload["endedTs"] = ("" if live else time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                                            time.gmtime(s.ended_ts or time.time())))
        payload["durationSec"] = (None if live else round(
            (s.ended_ts or time.time()) - s.created, 1))
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
    if s.ended_ts is None:
        s.ended_ts = time.time()
    write_replay(s, live=False)
    s.saved = True


def build_cfg(o: dict) -> Config:
    # nobomb（炸弹不可拆）已弃用为默认关：网页版已固定炸弹可拆；
    # 显式传 nobomb=true 仍生效（旧棋谱/外部调用方按其记录规则解释）。
    return Config(
        triple_no_follow=bool(o.get("sanzhang", False)),
        bomb_indivisible=bool(o.get("nobomb", False)),
        heart_ten_double=bool(o.get("red10", False)),
        four_with_three=bool(o.get("four3", False)),
    )


class Shadow:
    """一局的镜像状态 + AI 内核。"""

    def __init__(self, hands, kitty, leader, opts, prod_mode="hybrid",
                 sid: str = "", reuse_no=None, reuse_file=None):
        self.created = time.time()
        self.last_used = self.created                     # 空闲回收依据
        self.caller = ""                                  # 对外调用方名（来自网关 X-PDK-Caller）
        self.ended_ts = None                              # 终局时间（统计用时用）
        self.sid = check_ident(sid or uuid.uuid4().hex[:12], "sid")
        self.no = check_no(reuse_no) if reuse_no else allocate_no(REPLAY_PREFIX)  # 开局即定编号
        self.file = check_file(reuse_file) if reuse_file else f"{self.sid}.json"  # 重同步复用同一文件
        self.time_text = time.strftime("%Y-%m-%d %H:%M:%S")
        self.ts_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self.cfg = build_cfg(opts)
        self.prod_mode = prod_mode if prod_mode in PROD_MODES else "hybrid"
        h0, h1, k = sorted(hands[0]), sorted(hands[1]), sorted(kitty)
        if not (len(h0) == len(h1) == 16):
            raise ApiError(f"两手牌各需 16 张（当前 {len(h0)}/{len(h1)}）")
        if k and len(k) != 16:
            raise ApiError(f"底牌需 16 张（当前 {len(k)}）")
        if sorted(h0 + h1 + k) != sorted(bot_server.DECK):
            raise ApiError("手牌+底牌必须恰好构成 48 张固定牌库（牌 id 见文档 §2.1）")
        self.game = Game(cfg=self.cfg, first_player=leader, hands=[h0, h1], kitty=k)
        self.cg = fast.CGame(counts_of_ids(h0), counts_of_ids(h1), leader, self.cfg)
        self.agent, self.mode, self.net = build_prod_agent(self.prod_mode, self.cfg)
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
            note_decision(getattr(s.agent, "last_explain", None))
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
        out = _decide_core(payload, cfg, mode)
    res = {"cards": out.get("move") or [], "pass": bool(out.get("pass")),
           "seat": seat, "mode": mode, "legal_count": out.get("legal_count")}
    if out.get("explain"):
        res["explain"] = out["explain"]
    return res, 200





# ---------------- 记牌猜牌（belief 快照） ----------------
# 上游 pdk-ai 8db4fb79 起提供 /api/belief：给定"我手牌 + 对手张数 + 待跟牌型 + 动作史"，
# 返回 AI 对对手手牌的推断快照（记牌台账/点数概率/最可能手牌/推断链/锁牌认证）。
# 这里镜像同一契约，但规则 cfg 取"当局真实 opts"——上游用模块级 CFG，而网页版可能带
# 自定义开关（三张不可接/红十翻倍等；炸弹一律可拆），记牌与过牌推断必须按当局规则算才对得上。
BELIEF_LOCK_MAX_WORLDS = _env_int("PDK_BELIEF_LOCK_MAX") or 8000      # lock.vs_trick 认证的世界数上限（性能保护）
BELIEF_LEADS_MAX_WORLDS = _env_int("PDK_BELIEF_LEADS_MAX") or 1200    # lock.leads 候选认证的世界数上限
BELIEF_LEADS_MAX_CANDS = _env_int("PDK_BELIEF_LEADS_CANDS") or 24     # 最多认证多少个合法领出候选
BELIEF_LOCK_DEADLINE = float(_env_int("PDK_BELIEF_LOCK_MS") or 1500) / 1000.0   # 认证总预算（秒）
BELIEF_LOCK_WAIT = float(_env_int("PDK_BELIEF_LOCK_WAIT_MS") or 2500) / 1000.0   # 会话锁等待上限（AI 决策中要快速返回）
BELIEF_SEM = threading.Semaphore(2)          # 记牌猜牌独立闸：不与"AI 决策"共用排队（否则会排在长决策后面）


def _belief_core(payload: dict, cfg) -> dict:
    """与上游 server._belief_report 同字段同口径的记牌猜牌快照（cfg 可选）。"""
    import numpy as np
    from pdk.core import COPIES, N_RANKS, rank_of

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
    trick = _check_trick(payload.get("trick"), cfg)

    facts: list = []
    try:
        st = bot_server._replay_decide_history(my_ids, opp_n, history, cfg,
                                               choice_alpha=0.4, facts=facts)
    except TypeError:                       # 旧上游无 facts 出参
        st = bot_server._replay_decide_history(my_ids, opp_n, history, cfg,
                                               choice_alpha=0.4)
        facts = []
    b = st["belief"]
    my_cnt = st["my_cnt"]
    try:
        bot_server._fact_gaps(my_cnt, st["played_me"], st["played_opp"], facts)
    except Exception:
        pass
    # tell（用户规则③后半）：先前的三带暗示被更小单张打破 -> 对手在抢着走牌
    implied = [f.get("implied_min") for f in facts
               if isinstance(f, dict) and f.get("kind") == "lead_rule3"
               and "implied_min" in f]
    if implied:
        mn = max(implied)
        small = []
        for h in history:
            if int(h.get("seat", -1)) != 1:
                continue
            mv = h.get("move") or []
            if len(mv) == 1 and rank_of(mv[0]) < mn:
                small.append(bot_server.RANK_TXT[rank_of(mv[0])])
        if small:
            facts.append({
                "kind": "tell",
                "text": f"tell: 他先前的三带带牌最小 {bot_server.RANK_TXT[mn]}, "
                        f"之后却出了更小的单张 {'/'.join(sorted(set(small)))} "
                        f"-> 要么在抢着走牌要么在制造过牌假象 (用户规则③: 这轮要用大牌顶)",
            })

    rows, w = b.rows, b.weights
    z = float(w.sum()) if len(w) else 0.0
    if z <= 0:
        z = 1.0
    rank_prob, rank_exp = {}, {}
    rp_flag = None
    if len(rows):
        m1 = (rows >= 1)
        rp = (m1 * w[:, None]).sum(axis=0) / z
        re_ = (rows * w[:, None]).sum(axis=0) / z
        rp_flag = rp
        for r in range(N_RANKS):
            if rp[r] > 1e-6 or re_[r] > 1e-6:
                rank_prob[bot_server.RANK_TXT[r]] = round(float(rp[r]), 4)
                rank_exp[bot_server.RANK_TXT[r]] = round(float(re_[r]), 3)
    # 逐点数"张数分布" P(对手该点数恰有 k 张), k=0..4（上游 8325d96 新增）：
    # 光有"有没有"(rank_prob) 不够，用户要"到底几张"。门控与阈值与上游一致。
    rank_cnt_p = {}
    if rp_flag is not None:
        for r in range(N_RANKS):
            if not (rp_flag[r] > 1e-6):
                continue
            dist = {}
            for k in range(0, 5):
                share = float(w[rows[:, r] == k].sum()) / z
                if share > 0.005:
                    dist[int(k)] = round(share, 4)
            if dist:
                rank_cnt_p[bot_server.RANK_TXT[r]] = dist
    order = np.argsort(-w)[:8] if len(rows) else []
    top_hands = []
    for i in order:
        cnt = rows[i]
        cards = "".join(bot_server.RANK_TXT[r] * int(cnt[r]) for r in range(N_RANKS))
        top_hands.append({
            "cards": cards,
            "ranks": {bot_server.RANK_TXT[r]: int(cnt[r]) for r in range(N_RANKS) if cnt[r]},
            "p": round(float(w[i] / z), 5),
        })
    ledger = {}
    for r in range(N_RANKS):
        rem = COPIES[r] - my_cnt[r] - st["played_me"][r] - st["played_opp"][r]
        if rem > 0:
            ledger[bot_server.RANK_TXT[r]] = int(rem)

    # 锁牌认证：世界集上"对手不可能压住"的严格判定。
    # 上游对 vs_trick / leads 会遍历全部世界并逐候选求值（实测：1.7 万世界 + 40 候选 ≈ 3s，
    # 开局 30 万世界 ≈ 分钟级）——前端面板必须秒回，故按世界数设上限；被跳过时明确标注
    # skipped（不给"看似认证、实则采样"的假结论），残局（构成数小）照常严格认证。
    lock = {"vs_trick": None, "leads": [], "worlds_capped": False,
            "limits": {"vs_trick_max_worlds": BELIEF_LOCK_MAX_WORLDS,
                       "leads_max_worlds": BELIEF_LEADS_MAX_WORLDS}}
    _deadline = time.time() + BELIEF_LOCK_DEADLINE

    def _beatable(pat4):
        from pdk import fast as _f
        beat = 0
        tot = 0.0
        pat = tuple(int(x) for x in pat4)
        for i, wt in enumerate(w):
            if wt <= 0:
                continue
            tot += float(wt)
            if _f.gen_beats_codes(rows[i], pat, cfg):
                beat += float(wt)
        return {"p_beat": round(beat / tot, 4) if tot > 0 else None,
                "certified_locked": bool(tot > 0 and beat == 0)}

    if trick:
        if len(rows) <= BELIEF_LOCK_MAX_WORLDS:
            lock["vs_trick"] = _beatable(tuple(int(x) for x in trick))
        else:
            lock["worlds_capped"] = True
            lock["vs_trick"] = {"p_beat": None, "certified_locked": False,
                                "skipped": f"可能构成 {len(rows)} 种 > {BELIEF_LOCK_MAX_WORLDS}，未做严格认证"}
    total = sum(my_cnt) + opp_n
    if total <= 16:                          # 残局：枚举下认证候选领出
        from pdk import fast as _f
        if len(rows) > BELIEF_LEADS_MAX_WORLDS:
            lock["leads_skipped"] = (f"可能构成 {len(rows)} 种 > {BELIEF_LEADS_MAX_WORLDS}，"
                                     f"未做领出认证")
        else:
            try:
                leads = list(_f.gen_leads_codes(my_cnt, cfg))
            except Exception:
                leads = []
            for m in leads[:BELIEF_LEADS_MAX_CANDS]:
                if m == 0 or time.time() > _deadline:
                    break
                pm = (_f.classify_code(m, False, cfg) or _f.classify_code(m, True, cfg))
                if pm is None:
                    continue
                info = _beatable((int(pm.ptype), int(pm.main), int(pm.length), int(pm.nc)))
                if info.get("certified_locked"):
                    cards = "".join(bot_server.RANK_TXT[r] * ((m >> (3 * r)) & 7)
                                    for r in range(N_RANKS))
                    lock["leads"].append({"cards": cards, "ptype": int(pm.ptype)})
                    if len(lock["leads"]) >= 8:
                        break
    # ---- 牌型归属（当前最大单张/对子/三条/连对/顺子各在谁手里）----
    controls = None
    try:
        from pdk.agents import SolverAgent as _SA
        from pdk.core import PType as _PT
        _ag2 = _SA(bot_server._QFB(), cfg, **prod_solver_kw())
        _ag2.new_game(0, my_cnt, [0] * N_RANKS)
        _ag2._played = [list(st["played_me"]), list(st["played_opp"])]
        _cg2 = fast.CGame(my_cnt, [0] * N_RANKS, 0, cfg)
        for _r in range(N_RANKS):
            _cg2.g.played[0][_r] = st["played_me"][_r]
            _cg2.g.played[1][_r] = st["played_opp"][_r]
        _pc = _ag2._pattern_controls(_cg2)
        _NM = {"0": "单张", "1": "对子", "3": "三条", "4": "三带二", "8": "炸弹"}
        controls = []
        for _k, _v in _pc.items():
            _mine, _theirs, _hold = _v
            if isinstance(_k, tuple):
                _name = ("连对" if _k[0] == int(_PT.PAIRS) else "顺子") + str(_k[1])
            else:
                _name = _NM.get(str(_k), str(_k))
            if _mine < 0 and _theirs < 0:
                continue
            controls.append({
                "pattern": _name,
                "my_max": (bot_server.RANK_TXT[_mine] if _mine >= 0 else None),
                "opp_possible_max": (bot_server.RANK_TXT[_theirs] if _theirs >= 0 else None),
                "i_hold_max": bool(_hold),
            })
        controls.sort(key=lambda x: (not x["i_hold_max"], x["pattern"]))
    except Exception as _e:
        controls = {"error": repr(_e)[:120]}

    # ---- 对手每类牌型概率分布（开局对所有牌型预演，实时更新）----
    opp_patterns = None
    try:
        from pdk import patternmap as _pm
        if len(rows):
            _step = max(1, len(rows) // 64)
            _ws = [list(x) for x in rows[::_step][:64]]
            _wt = [float(w[i]) for i in range(0, len(rows), _step)][:64]
        else:
            _ws, _wt = [], []
        _op = _pm.opp_patterns(_ws, _wt, my_cnt)
        for _row in _op:
            for _k in ("my_max", "max_p50", "max_p90"):
                if _k in _row and _row[_k] is not None and _row[_k] >= 0:
                    _row[_k] = bot_server.RANK_TXT[_row[_k]]
        opp_patterns = [r for r in _op if r.get("p_has", 0) > 0.005]
        opp_patterns.sort(key=lambda r: -r["p_has"])
    except Exception as _e2:
        opp_patterns = {"error": repr(_e2)[:120]}

    # ---- 逐世界推演明细（逐世界"能否压住" + 每候选锁链画像；world_detail=false 可关以省时延）----
    worlds_detail = None
    try:
        if payload.get("world_detail", True):
            from pdk.agents import SolverAgent as _SA3
            _ag3 = _SA3(bot_server._QFB(), cfg, **prod_solver_kw())
            _wsel = [list(r) for r in rows[:16]]
            _wtsel = [float(x) for x in (w[:16] if len(w) >= len(rows) else [1.0] * len(_wsel))]
            if len(_wtsel) != len(_wsel):
                _wtsel = [1.0] * len(_wsel)
            _cgp = fast.CGame(my_cnt, _wsel[0] if _wsel else [0] * 13, 0, cfg)
            if not trick:
                _legal = [int(m) for m in fast.gen_leads_codes(my_cnt, cfg) if m]
            else:
                _legal = [int(m) for m in fast.gen_beats_codes(my_cnt, tuple(int(x) for x in trick), cfg) if m]
            _legal.sort(key=lambda m: -fast.move_size(m))
            _cands = _legal[:6]
            _prof = {}
            if _cands and _wsel:
                try:
                    _prof = _ag3._lock_chain(_cgp, _wsel, _wtsel, _cands)
                except Exception:
                    _prof = {}
            _cand_rows = []
            for _m in _cands:
                _pt = (fast.classify_code(_m, False, cfg) or fast.classify_code(_m, True, cfg))
                if _pt is None:
                    continue
                _pp, _cc, _rr = _prof.get(int(_m), (None, None, None))
                _cand_rows.append({
                    "move": "".join(bot_server.RANK_TXT[r] * ((_m >> (3 * r)) & 7)
                                    for r in range(N_RANKS) if (_m >> (3 * r)) & 7),
                    "ptype": int(_pt.ptype), "size": int(fast.move_size(_m)),
                    "p_lock": (round(float(_pp), 4) if _pp is not None else None),
                    "chain": (round(float(_cc), 2) if _cc is not None else None),
                    "reclaim": (round(float(_rr), 2) if _rr is not None else None),
                })
            _wrows = []
            for _i, (_wr, _wtv) in enumerate(zip(_wsel, _wtsel)):
                _cb = {}
                for _cr, _m in zip(_cand_rows, _cands):
                    _pt2 = (fast.classify_code(_m, False, cfg) or fast.classify_code(_m, True, cfg))
                    if _pt2 is None:
                        continue
                    _t4m = (int(_pt2.ptype), int(_pt2.main), int(_pt2.length), int(_pt2.nc))
                    _cb[_cr["move"]] = int(bool(fast.gen_beats_codes(list(_wr), _t4m, cfg)))
                _wrows.append({
                    "i": int(_i),
                    "cards": "".join(bot_server.RANK_TXT[r] * int(_wr[r]) for r in range(N_RANKS)),
                    "w": round(float(_wtv), 5),
                    "can_beat": _cb,
                })
            worlds_detail = {"n": int(len(rows)), "exhaustive": bool(getattr(b, "exhaustive", False)),
                             "shown": len(_wrows), "cands": _cand_rows, "worlds": _wrows}
    except Exception as _e3:
        worlds_detail = {"error": repr(_e3)[:120]}

    # ---- 推理链 / 残局候选点数（来自重放阶段构建的 reasoner）----
    _rs = st.get("reasoner")
    inferences = []
    cand = None
    if _rs is not None:
        try:
            inferences = _rs.infer()
        except Exception:
            inferences = []
        if opp_n <= 2:
            try:
                cand = _rs.candidates(opp_n)
            except Exception:
                cand = None

    return {
        "opp_n": opp_n,
        "my_n": sum(my_cnt),
        "worlds": int(len(rows)),
        "exhaustive": bool(getattr(b, "exhaustive", False)),
        "weighted": bool(getattr(b, "has_weights", False)),
        "sharpness": round(float(w[order[0]] / z), 5) if len(order) else None,
        "ledger": ledger,
        "rank_prob": rank_prob,
        "rank_exp": rank_exp,
        "rank_cnt_p": rank_cnt_p,        # 张数分布: P(恰有 k 张), k=0..4
        "opp_most_likely": (top_hands[0]["ranks"] if top_hands else {}),   # 最可能的一手牌（点数->张数）
        "top_hands": top_hands,
        "facts": facts,
        "lock": lock,
        "incomplete_snapshot": bool(st.get("incomplete")),
        "inferences": inferences,        # 完整推理链（L1/L2 确定 + B1-B7 行为层，带置信度）
        "candidates": cand,              # 对手剩<=2 张时的候选点数（台账 + 排除后）
        "controls": controls,            # 牌型归属：当前最大单张/对子/三条/连对/顺子在谁手里
        "opp_patterns": opp_patterns,    # 对手每类牌型的概率分布（预演推理）
        "worlds_detail": worlds_detail,  # 逐世界推演明细（可 world_detail=false 关闭）
    }


def _session_belief_payload(s: Shadow, persp: str) -> dict:
    """把会话局面转成无状态口径的 belief 请求（perspective 决定"谁是我"）。

    persp="ai" -> 以 AI 自己为"我"（看它怎么猜玩家、它认哪些领出必压不住）
    persp="me" -> 以玩家为"我"（玩家的猜牌助手：AI 手里大概有什么）
    """
    if persp not in ("ai", "me"):
        raise ApiError('perspective 只能是 "ai"（AI 看我）或 "me"（我看 AI）')
    me_seat = 1 if persp == "ai" else 0
    opp_seat = 1 - me_seat
    if not s.game.hand_ids(me_seat):
        raise ApiError("该视角当前已无手牌（本局结束或已出完），没有记牌猜牌快照可算")
    turn = int(s.cg.g.turn)
    trick = None
    if turn == me_seat and int(s.cg.g.trick[0]) != 255:
        trick = [int(x) for x in s.cg.g.trick]
    history = []
    for mv in s.moves_detail:
        web_seat = int(mv["seat"])
        up_seat = (1 - web_seat) if persp == "ai" else web_seat
        history.append({"seat": up_seat,
                        "move": [int(x) for x in (mv.get("cards") or [])],
                        "pass_on": mv.get("pass_on")})
    return {"my_hand": [int(x) for x in s.game.hand_ids(me_seat)],
            "opp_n": int(s.game.hand_count(opp_seat)),
            "trick": trick, "history": history}


def do_belief(p: dict):
    """AI 猜测对手手牌（记牌/猜牌/锁牌）快照。

    两种入参：
      1) {"sid": ..., "perspective": "ai"|"me"}   —— 会话版：按当局真实手牌与动作史现算（H5 面板用）
      2) {"my_hand": [...], "opp_n": 8, "trick": null|[ptype,main,len,nc],
          "history": [{"seat":0|1,"move":[id...]},...]}  —— 无状态版（与上游 /api/belief 同契约）
    """
    if p.get("sid"):
        sid = check_ident(p.get("sid"), "sid")
        s = get_sess(sid)
        if s.bad:
            return {"error": "影子牌局已失效", "type": "ShadowBad"}, 500
        # 只在锁内做"快照"（毫秒级），计算放到锁外：AI 决策期间锁被长时间持有，
        # 若整个计算都塞在锁内，前端会一直转圈（实测 >8s 无响应）。
        if not s.lock.acquire(timeout=BELIEF_LOCK_WAIT):
            raise Busy(f"AI 正在算这一手（决策中），记牌猜牌稍后再看（已等 {BELIEF_LOCK_WAIT:.1f}s）")
        try:
            persp = str(p.get("perspective") or "ai")
            payload = _session_belief_payload(s, persp)
            view = {"perspective": persp,
                    "my_seat": 1 if persp == "ai" else 0,
                    "ply": len(s.moves_detail),
                    "turn": int(s.cg.g.turn)}
        finally:
            s.lock.release()
        if not BELIEF_SEM.acquire(timeout=6):
            raise Busy("记牌猜牌排队中，请稍后重试")
        try:
            out = _belief_core(payload, s.cfg)
        finally:
            BELIEF_SEM.release()
        out["view"] = dict(view, my_n=len(payload["my_hand"]), opp_n=payload["opp_n"])
        return out, 200
    cfg = build_cfg(p.get("opts") or {})
    if not BELIEF_SEM.acquire(timeout=6):
        raise Busy("记牌猜牌排队中，请稍后重试")
    try:
        out = _belief_core(p, cfg)
    finally:
        BELIEF_SEM.release()
    return out, 200


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

    规则由 opts 决定（默认 = 网页版默认规则：炸弹可拆/红十不翻倍/三张可接/四带三关闭），
    因此调用方与 AI 用同一套规则；失败直接报错（不降级）。
    """
    mode = str(p.get("mode", "hybrid")).lower()
    if mode not in PROD_MODES:
        raise ApiError(f"未知 mode {mode!r}（可选 hybrid|dual）")
    cfg = build_cfg(check_opts(p.get("opts")))
    with decide_gate():
        out = _decide_core(p, cfg, mode)
    note_decision({"path": out.get("decision_path")})
    out["mode"] = mode
    out["engine"] = prod_solver_kw().get("engine", "c")
    try:
        out["openingSearch"] = prod_config_info().get("openingSearch", None)
    except Exception:
        pass
    return out, 200





def _check_trick(trick, cfg=None):
    """trick 校验：[ptype, main, len, nc] 或 null（=领出）。

    结构 + **语义**双重校验：语义交给引擎自己的 classify 反查（pdk/trickguard.parse_trick），
    不可能牌型直接 400 并给出正确写法。len 随牌型而变：连对=对数、飞机=三张组数、
    其余=张数；nc 不参与判定（仅局面哈希）。见 docs/AI_API.md §2.3。
    规则相关分支（四带三/三张不可接）按调用方 opts 判定，缺省 = 网页默认。
    """
    if trick in (None, "", [], "null"):
        return None
    if not isinstance(trick, (list, tuple)) or len(trick) != 4:
        raise ApiError("trick 必须是 [ptype, main, len, nc] 或 null")
    try:
        parsed = _parse_trick(list(trick), cfg if cfg is not None else build_cfg({}))
    except (ValueError, TypeError) as e:
        raise ApiError(str(e) or "trick 非法")
    return list(parsed)


def _decide_core(payload: dict, cfg, mode: str = "hybrid") -> dict:
    """与 pdk-ai server._decide 同源的单步决策，但 **规则 cfg 与模式可选**。

    为什么自己实现：调用方（网页版/外部）可能带 opts（如"四带三""三张不可接"），而上游 /api/decide
    用的是模块级 CFG（= Config() 默认；上游 2026-09-20 起也支持 payload.opts 覆盖，见 pdk/trickguard.py，
    此处保留自实现是为了 mode=dual 与逐候选展示等桥专有字段）。这里把上游 _decide 的完整流程复刻一遍，关键点
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
    t4 = _check_trick(payload.get("trick"), cfg)

    kitty = check_cards(payload.get("kitty") or [])
    st = bot_server._replay_decide_history(my_ids, opp_n, history, cfg, choice_alpha=0.4,
                                           kitty_ids=kitty or None)
    belief, my_cnt = st["belief"], st["my_cnt"]

    fb = bot_server._QFB()
    fb.last = st["last_move"]
    fb.opp_p = st["last_was_pass"]
    fb.opening = (st["my_lead_count"] == 0) and (not st["incomplete"])

    kw = prod_solver_kw()
    try:
        ag = SolverAgent(fb, cfg, **kw)
    except TypeError:                          # 旧内核不认新参数
        ag = SolverAgent(fb, cfg, total_threshold=kw.get("total_threshold", 28),
                         max_rows=kw.get("max_rows", 400000),
                         engine=kw.get("engine", "c"))
    if mode == "dual":                         # 净分优先：关胜率带
        try:
            ag.win_rate_tol = 2.0
        except Exception:
            pass
    ag.new_game(0, my_cnt, [0] * N_RANKS)
    ag.belief = belief
    ag.oracle_cnt = None
    # 记牌账本：残局穷举"未见面牌池"必需（无状态路径没有 observe，需从 history 注入；
    # 上游 server._decide 同样处理 —— 漏掉会让穷举按"双方都没出过牌"的错误牌池计算）
    ag._played = [list(st["played_me"]), list(st["played_opp"])]
    # 批次33 对齐上游 server._decide: 已知扣底要从"未见面牌池"再扣一次 (残局穷举必需);
    # 此前桥只注入 _played 不注入 _known -> unseen 偏大, 穷举世界集不是真全集。
    ag._known = list(st.get("known_cnt") or [0] * N_RANKS)
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
    ex = getattr(ag, "last_explain", None) or {}
    out = {"move": cards, "pass": mv_code == 0, "legal_count": len(legal),
           "decision_path": ex.get("path")}
    if payload.get("explain"):
        out["explain"] = ex
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
        note_decision(getattr(s.agent, "last_explain", None))
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
    s.caller = str(p.get("_caller") or "")[:40]      # 由网关注入（服务端字段，客户端伪造无效）
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





# ---------------- 可观测性: 指标 / 审计日志 / 金丝雀 ----------------
API_LOG_DIR = Path(__file__).resolve().parent / "data" / "api_log"
API_LOG_DIR.mkdir(parents=True, exist_ok=True)
_MET_LOCK = threading.Lock()
_MET = {
    "ep": {},                 # endpoint -> [count, bad]
    "lat": {},                # endpoint -> [ms] (环形 512)
    "err": {},                # "类型:端点" -> n
    "paths": {},              # 决策路径 -> n（近 500 次）
    "decisions": 0,
}
_PATHS_DEQ = collections.deque(maxlen=500)


def _pct(vals, q):
    if not vals:
        return None
    xs = sorted(vals)
    return xs[min(len(xs) - 1, int(len(xs) * q))]


def met_record(ep, ms, status, rid, dpath=None, nlegal=None, err=None):
    """滚动指标 + 审计日志（一请求一行 JSONL，rid 贯穿前后端）。"""
    with _MET_LOCK:
        e = _MET["ep"].setdefault(ep, [0, 0])
        e[0] += 1
        if status >= 400:
            e[1] += 1
        lat = _MET["lat"].setdefault(ep, [])
        lat.append(round(ms, 1))
        if len(lat) > 512:
            del lat[: len(lat) - 512]
        if err:
            k = f"{err}:{ep}"
            _MET["err"][k] = _MET["err"].get(k, 0) + 1
        if dpath:
            _MET["paths"][dpath] = _MET["paths"].get(dpath, 0) + 1
            _PATHS_DEQ.append(dpath)
            _MET["decisions"] += 1
    try:
        import datetime as _dt
        line = json.dumps({
            "ts": _dt.datetime.now().isoformat(timespec="milliseconds"),
            "ep": ep, "ms": round(ms, 1), "status": status, "rid": rid,
            **({"path": dpath} if dpath else {}),
            **({"nlegal": nlegal} if nlegal is not None else {}),
            **({"err": err} if err else {}),
        }, ensure_ascii=False)
        with open(API_LOG_DIR / f"{_dt.date.today().isoformat()}.jsonl", "a",
                  encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def met_snapshot() -> dict:
    with _MET_LOCK:
        eps = {}
        for ep, (n, bad) in _MET["ep"].items():
            lat = _MET["lat"].get(ep, [])
            eps[ep] = {"n": n, "bad": bad,
                       "ms_p50": _pct(lat, 0.5), "ms_p95": _pct(lat, 0.95),
                       "ms_max": max(lat) if lat else None}
        return {"endpoints": eps, "errors": dict(_MET["err"]),
                "decision_paths": dict(_MET["paths"]),
                "decisions": _MET["decisions"]}


def note_decision(explain: dict | None):
    """把一次决策的路径汇入指标（联调时看"各部分是否真实运作"）。"""
    dpath = (explain or {}).get("path")
    if dpath:
        with _MET_LOCK:
            _MET["paths"][dpath] = _MET["paths"].get(dpath, 0) + 1
            _PATHS_DEQ.append(dpath)
            _MET["decisions"] += 1


def component_probes() -> dict:
    """组件探针：C 核心 / fallback 网络 / 生产智能体（真实构建，非静态标志）。"""
    comp = {}
    try:
        v = fast.solve_batch_n([([1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
                                 [0, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
                                 0, None, (0, 0))], bot_server.CFG, 100000)[0]
        comp["c_core"] = {"ok": v[0] != -101, "probe": v[0]}
    except Exception as e:
        comp["c_core"] = {"ok": False, "err": repr(e)[:80]}
    try:
        fb = bot_server._QFB()
        comp["fallback_net"] = {"ok": True,
                                "kind": "a2c56" if getattr(fb, "use_x", False)
                                else "dmc-v1"}
        if getattr(fb, "use_x", False) is not True:
            comp["fallback_net"]["warn"] = "已回退旧网络（不降级策略下会拒绝服务）"
    except Exception as e:
        comp["fallback_net"] = {"ok": False, "err": repr(e)[:80]}
    try:
        agent, mode, _ = build_prod_agent("hybrid")
        kw = prod_solver_kw()
        # 配方键全量显示：上游会随版本调整生产配方（如 e037b99 新增 opening_seeds/opening_margin），
        # 自检面板要能一眼看出"线上到底跑的是哪套配方"，所以不再白名单过滤。
        comp["prod_agent"] = {"ok": True, "mode": mode,
                              "kw": {k: v for k, v in kw.items()}}
    except Exception as e:
        comp["prod_agent"] = {"ok": False, "err": repr(e)[:80]}
    # 新增组件探针（e037b99 起的推理融合链路）：推理引擎 / 牌型地图 / 合理枚举闸门
    try:
        st_probe = bot_server._replay_decide_history(
            [44], 1, [], build_cfg({}), choice_alpha=0.4)
        rs = st_probe.get("reasoner")
        comp["reasoning"] = {"ok": rs is not None,
                             "kinds": "L1/L2/B1-B7"}
        if rs is None:
            comp["reasoning"]["warn"] = "推理引擎未构建（推理链字段会为空）"
    except Exception as e:
        comp["reasoning"] = {"ok": False, "err": repr(e)[:80]}
    try:
        from pdk import patternmap as _pm
        op = _pm.opp_patterns([[1] + [0] * 12], [1.0], [0] * 13)
        comp["patternmap"] = {"ok": isinstance(op, list) and len(op) > 0,
                              "rows": len(op)}
    except Exception as e:
        comp["patternmap"] = {"ok": False, "err": repr(e)[:80]}
    # 推理线程数自证（2 核机器上设 1 可减少抢核；实测决策一致且更快，见 README）
    try:
        import torch as _t
        nt = int(_t.get_num_threads())
        comp["threads"] = {"ok": nt == 1, "torch_threads": nt,
                           "env_omp": os.environ.get("OMP_NUM_THREADS", "")}
        if nt != 1:
            comp["threads"]["warn"] = "线程数>1 会与求解器抢核（2 核机器上更慢）"
    except Exception as e:
        comp["threads"] = {"ok": True, "err": repr(e)[:60]}
    try:
        from pdk import candgen as _cg
        leads = [int(m) for m in fast.gen_leads_codes([1] + [0] * 12, bot_server.CFG) if m]
        ok_rows = _cg.filter_reasonable(leads, [1] + [0] * 12, bot_server.CFG,
                                        max_per_type=2, budget=10)
        comp["candgen"] = {"ok": len(ok_rows) > 0, "kept": len(ok_rows), "of": len(leads)}
    except Exception as e:
        comp["candgen"] = {"ok": False, "err": repr(e)[:80]}
    return comp


def run_selftest() -> dict:
    """金丝雀：固定场景走真实决策路径，断言"记牌→穷举→数值→顶牌"链路。

    用例与上游 server._selftest 同源（fixture 取自真实牌谱 A0519）。
    """
    results = []

    def case(name, fn):
        t0 = time.perf_counter()
        try:
            detail = fn()
            results.append({"name": name, "ok": True,
                            "ms": round((time.perf_counter() - t0) * 1000, 1),
                            **(detail or {})})
        except Exception as e:
            results.append({"name": name, "ok": False,
                            "ms": round((time.perf_counter() - t0) * 1000, 1),
                            "err": f"{type(e).__name__}: {e}"})

    # 场景 1: A0519 ply10 —— 必须顶牌（Q/K/A），一条用例贯穿"记牌→穷举→数值→选择"
    def s_a0519():
        hand_ids = [2, 13, 19, 21, 24, 27, 29, 39, 42, 44]   # 3 6 7 8 99 X Q K A
        hist = [
            {"seat": 0, "move": [1, 3, 4, 5], "pass_on": None},
            {"seat": 1, "move": [6, 7, 8, 9], "pass_on": None},
            {"seat": 0, "move": [16, 18, 20, 23], "pass_on": None},
            {"seat": 1, "move": [], "pass_on": None},
            {"seat": 0, "move": [25, 26], "pass_on": None},
            {"seat": 1, "move": [32, 33], "pass_on": None},
            {"seat": 0, "move": [36, 37], "pass_on": None},
            {"seat": 1, "move": [], "pass_on": None},
            {"seat": 0, "move": [10], "pass_on": None}]
        out, code = do_decide({"my_hand": hand_ids, "opp_n": 3,
                               "trick": [0, 2, 1, 0], "history": hist})
        if code != 200:
            raise AssertionError(f"decide 失败: {out}")
        cards = out.get("move") or []
        played = sorted(c >> 2 for c in cards) if cards else []
        if not (played and played[0] >= 10):
            raise AssertionError(f"未顶牌: 出了 rank={played}")
        note_decision((out.get("explain") or {}) or None)
        return {"move": out.get("move"), "legal_count": out.get("legal_count"),
                "check": "A0519 顶牌（记牌→穷举→数值 链路）"}

    def s_must_beat():
        out, code = do_decide({"my_hand": [44], "opp_n": 1,
                               "trick": [0, 5, 1, 0], "history": []})
        if code != 200 or not out.get("move"):
            raise AssertionError("未返回动作")
        note_decision((out.get("explain") or {}) or None)
        return {"move": out["move"], "check": "必压/forced 链路"}

    def s_lead():
        out, code = do_decide({"my_hand": [44], "opp_n": 1, "trick": None,
                               "history": []})
        if code != 200 or "legal_count" not in out:
            raise AssertionError("领出 decide 异常")
        return {"check": "领出 decide 正常"}

    def s_bad():
        try:
            do_decide({"my_hand": [], "opp_n": 0, "trick": None, "history": []})
        except ApiError as e:
            if e.status != 400:
                raise AssertionError(f"空手牌应 400，实际 {e.status}")
            return {"check": "错误契约（400 + type + rid）"}
        raise AssertionError("空手牌未被拒绝")

    # 场景 5: 推理链 + 新字段契约（e037b99 起：记牌 x 世界推理融合）
    def s_belief_fields():
        payload = {"my_hand": [2, 13, 19, 21, 24, 27, 29, 39, 42, 44], "opp_n": 3,
                   "trick": [0, 2, 1, 0],
                   "history": [{"seat": 0, "move": [1, 3, 4, 5]},
                               {"seat": 1, "move": [6, 7, 8, 9]},
                               {"seat": 0, "move": [16, 18, 20, 23]},
                               {"seat": 1, "move": []}]}
        out = _belief_core(payload, build_cfg({}))
        need = ("inferences", "controls", "opp_patterns", "worlds_detail",
                "candidates", "rank_cnt_p", "top_hands", "lock")
        miss = [k for k in need if k not in out]
        if miss:
            raise AssertionError(f"新字段缺失: {miss}")
        wd = out.get("worlds_detail") or {}
        if not (wd.get("cands") and wd.get("worlds")):
            raise AssertionError("worlds_detail 为空（世界推演链路异常）")
        if not isinstance(out.get("opp_patterns"), list) or not out["opp_patterns"]:
            raise AssertionError("opp_patterns 为空（牌型地图链路异常）")
        if not isinstance(out.get("controls"), list) or not out.get("controls"):
            raise AssertionError("controls 为空（牌型归属链路异常）")
        rcp = out.get("rank_cnt_p") or {}
        bad = [k for k, v in rcp.items() if abs(sum(float(x) for x in v.values()) - 1.0) > 0.02]
        if bad:
            raise AssertionError(f"张数分布概率和不为 1: {bad[:3]}")
        return {"check": "新字段契约（推理链/牌型地图/归属/世界推演/张数分布）",
                "worlds": out.get("worlds"), "patterns": len(out["opp_patterns"]),
                "controls": len(out["controls"])}

    # 场景 6: 推理链非空且含确定类（L1 台账 / L2 过牌硬推理）
    def s_reasoning():
        payload = {"my_hand": [2, 13, 19, 21, 24, 27, 29, 39, 42, 44], "opp_n": 3,
                   "trick": [0, 2, 1, 0],
                   "history": [{"seat": 0, "move": [1, 3, 4, 5]},
                               {"seat": 1, "move": [6, 7, 8, 9]},
                               {"seat": 0, "move": [16, 18, 20, 23]},
                               {"seat": 1, "move": []}]}
        out = _belief_core(payload, build_cfg({}))
        inf = out.get("inferences") or []
        if not inf:
            raise AssertionError("推理链为空（推理引擎链路异常）")
        sure = [x for x in inf if str(x.get("kind", "")).upper() in ("L1", "L2")]
        if not sure:
            raise AssertionError("推理链缺少确定类结论（L1/L2）")
        for x in inf:
            if not x.get("text") or "kind" not in x:
                raise AssertionError(f"推理链条目结构异常: {x}")
        return {"check": "推理链（台账/过牌硬推理/行为层）", "n": len(inf),
                "sure": len(sure), "sample": str(inf[0].get("text"))[:60]}

    # 场景 7: 合理枚举闸门（candgen）——三带只留合理带法、按牌型家族分配额度
    def s_candgen():
        from pdk import candgen as _cg
        my_cnt = [0] * 13
        for r in (0, 1, 2, 11):          # 3 3 3 A：三带家族的"不合理带法"应被剔除
            my_cnt[r] += 1 if r != 0 else 3
        legal = [int(m) for m in fast.gen_leads_codes(my_cnt, bot_server.CFG) if m]
        if not legal:
            raise AssertionError("该手牌没有合法领出（构造错误）")
        kept = _cg.filter_reasonable(legal, my_cnt, bot_server.CFG,
                                     max_per_type=2, budget=10)
        if not kept:
            raise AssertionError("合理枚举闸门把候选筛空了")
        if not set(kept) <= set(legal):
            raise AssertionError("闸门返回了非法候选")
        return {"check": "合理枚举闸门（三带合理带法 + 家族额度）",
                "kept": len(kept), "of": len(legal)}

    case("decide_schema", s_lead)
    case("a0519_top", s_a0519)
    case("must_beat", s_must_beat)
    case("error_contract", s_bad)
    case("belief_fields", s_belief_fields)
    case("reasoning_chain", s_reasoning)
    case("candgen_gate", s_candgen)
    return {"ok": all(x["ok"] for x in results), "cases": results,
            "hint": "任一 case 失败 => 对应链路异常；先看 /health 的 components，"
                    "再按 rid 查 data/api_log/<日期>.jsonl"}


PUBLIC_GET = {"/health", "/api/health", "/api/state"}
PUBLIC_POST = {"/api/decide", "/api/explain", "/api/analyze", "/api/decode",
               "/api/belief",
               "/api/new_game", "/api/play", "/api/suggest", "/api/selftest"}


class Handler(BaseHTTPRequestHandler):
    def _json(self, obj, code=200, headers: dict | None = None, rid: str | None = None):
        if isinstance(obj, dict) and rid:
            obj = dict(obj)
            obj.setdefault("rid", rid)
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")   # 网页版跨域调用
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Request-Id")
        self.send_header("Access-Control-Expose-Headers", "X-Request-Id")
        if rid:
            self.send_header("X-Request-Id", rid)
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
        t0 = time.perf_counter()
        rid = self.headers.get("X-Request-Id") or f"r{time.time_ns() % 10**10}"
        status_holder = {"code": 200}
        try:
            u = urlparse(self.path)
            if PUBLIC_MODE and u.path not in PUBLIC_GET:
                status_holder["code"] = 404
                met_record(u.path, (time.perf_counter()-t0)*1000, 404, rid, err="NotFound")
                return self._json({"error": "该接口不在公开实例上提供", "rid": rid}, 404)
            if u.path in ("/health", "/api/health"):   # 上游用 /api/health：别名以兼容共用联调脚本
                with LOCK:
                    n = len(SESSIONS)
                comp = component_probes()
                body = {"ok": bool(all(c.get("ok") for c in comp.values())),
                        "agent": "prod",
                        "productionModel": "ckpt/policy_a2c_final56.pt",
                        "modes": sorted(PROD_MODES),
                        "components": comp,
                        "metrics": met_snapshot(),
                        "selftest": "POST /api/selftest（金丝雀：记牌→穷举→数值→顶牌链路）",
                        "sessions": n, "v": 9,
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
                status_holder["code"] = 404
                self._json({"error": "unknown endpoint", "rid": rid}, 404, rid=rid)
                return
        except ApiError as e:
            status_holder["code"] = e.status
            self._json({"error": str(e)}, e.status, e.extra, rid=rid)
        except Exception as e:
            import traceback
            tb = traceback.format_exc()
            status_holder["code"] = 500
            print(f"[bridge] GET {self.path} 失败: {tb[-800:]}", file=sys.stderr, flush=True)
            body = {"error": f"{type(e).__name__}: {e}"}
            if not PUBLIC_MODE:
                body["tb"] = tb[-1200:]
            self._json(body, 500, rid=rid)
        finally:
            met_record(urlparse(self.path).path, (time.perf_counter()-t0)*1000,
                       status_holder["code"], rid)

    def do_POST(self):
        t0 = time.perf_counter()
        rid = self.headers.get("X-Request-Id") or f"r{time.time_ns() % 10**10}"
        status_holder = {"code": 200, "dpath": None, "nlegal": None, "err": None}
        try:
            try:
                n = int(self.headers.get("Content-Length", 0) or 0)
            except (TypeError, ValueError):
                n = 0
            if n > MAX_BODY:
                status_holder["code"] = 413
                return self._json({"error": f"请求体过大（{n} 字节 > 上限 {MAX_BODY}）",
                                   "type": "TooLarge", "rid": rid}, 413, rid=rid)
            payload = json.loads(self.rfile.read(n) or b"{}")
            if not isinstance(payload, dict):
                raise ApiError("请求体必须是 JSON 对象")
            rid = (payload.get("rid") if isinstance(payload, dict) else None) or rid
            u = urlparse(self.path)
            if PUBLIC_MODE and u.path not in PUBLIC_POST:
                status_holder["code"] = 404
                return self._json({"error": "该接口不在公开实例上提供",
                                   "type": "NotFound", "rid": rid}, 404, rid=rid)
            # 调用方归属：由网关按 API Key 注入（外部请求自带的同名头在 nginx 层被清空）
            caller = self.headers.get("X-PDK-Caller") or ""
            if caller:
                from urllib.parse import unquote
                payload["_caller"] = unquote(caller)
            if u.path == "/init":
                body, code = handle_init(payload)
            elif u.path == "/action":
                ok, info = do_action(payload["sid"],
                                     int(payload.get("seat", 0)),
                                     payload.get("cards", []))
                body, code = (({"ok": True, **(info if isinstance(info, dict) else {})}, 200)
                              if ok else ({"error": info, "type": "BadRequest"}, 400))
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
            elif u.path == "/api/belief":
                body, code = do_belief(payload)
            elif u.path == "/api/analyze":
                body, code = do_analyze(payload)
            elif u.path == "/api/selftest":
                body, code = run_selftest(), 200
            elif u.path == "/api/decode":
                body, code = do_decode(payload)
            else:
                status_holder["code"] = 404
                body, code = {"error": "unknown endpoint", "type": "NotFound"}, 404
            if isinstance(body, dict):
                ex = body.get("explain")
                status_holder["dpath"] = (body.get("decision_path")
                                          or (ex.get("path") if isinstance(ex, dict) else None))
                if u.path == "/api/decide":
                    status_holder["nlegal"] = body.get("legal_count")
                if code >= 400:
                    status_holder["err"] = body.get("type") or "Error"
            status_holder["code"] = code
            self._json(body, code, rid=rid)
        except ApiError as e:
            status_holder["code"] = e.status
            status_holder["err"] = "ApiError"
            self._json({"error": str(e), "type": "ApiError"}, e.status, e.extra, rid=rid)
        except KeyError as e:
            status_holder["code"] = 404
            status_holder["err"] = "NotFound"
            self._json({"error": f"缺少字段/资源: {e}", "type": "NotFound", "rid": rid},
                       404, rid=rid)
        except json.JSONDecodeError as e:
            status_holder["code"] = 400
            status_holder["err"] = "JSONDecodeError"
            self._json({"error": f"非法 JSON: {e}", "type": "JSONDecodeError", "rid": rid},
                       400, rid=rid)
        except Exception as e:
            import traceback
            tb = traceback.format_exc()
            status_holder["code"] = 500
            status_holder["err"] = type(e).__name__
            print(f"[bridge] POST {self.path} 失败: {tb[-800:]}", file=sys.stderr, flush=True)
            body = {"error": f"{type(e).__name__}: {e}", "type": type(e).__name__}
            if not PUBLIC_MODE:
                body["tb"] = tb[-1500:]
            self._json(body, 500, rid=rid)
        finally:
            met_record(urlparse(self.path).path, (time.perf_counter()-t0)*1000,
                       status_holder["code"], rid,
                       dpath=status_holder["dpath"], nlegal=status_holder["nlegal"],
                       err=status_holder["err"])


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
    try:
        n = _restore_sessions_inner()
    except Exception as e:                    # 兜底：绝不让 RESTORING 卡住（否则未知 sid 永久 503）
        print(f"[bridge] 会话恢复异常：{type(e).__name__}: {e}", file=sys.stderr, flush=True)
    finally:
        RESTORING = False
    if n:
        print(f"[bridge] restored {n} live session(s) across restart", flush=True)


def _restore_sessions_inner() -> int:
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
            note_no_used(s.no)                       # 旧号登记，避免新局重复发出
            if s.game.finished:
                write_replay(s, live=False)          # 恢复时才发现已结束 -> 补写终局
                continue
            write_replay(s, live=True)               # 重放完成后写一次
            with LOCK:
                SESSIONS[sid] = s
            n += 1
        except Exception as e:
            print(f"[bridge] 恢复会话 {sid} 失败：{type(e).__name__}: {e}", file=sys.stderr, flush=True)
    return n


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
    ap.add_argument("--no-prefix", default="", help="对局编号前缀（对外实例建议 E）")
    ap.add_argument("--max-sessions", type=int, default=0, help="会话上限（默认 500；对外实例 64）")
    ap.add_argument("--idle-ttl", type=float, default=0.0, help="会话空闲回收秒数")
    ap.add_argument("--decide-timeout", type=float, default=0.0, help="决策排队超时秒数")
    args = ap.parse_args()

    global _ACT_SEM, PUBLIC_MODE, MAX_SESSIONS, REPLAY_DIR, IDLE_TTL, DECIDE_TIMEOUT, REPLAY_ON, REPLAY_PREFIX, RESTORING
    PUBLIC_MODE = bool(args.public_api)
    REPLAY_ON = bool(args.replay)
    if args.no_prefix:
        REPLAY_PREFIX = str(args.no_prefix)[:1].upper() or "A"
        if REPLAY_PREFIX not in ("A", "H", "E"):
            raise SystemExit("--no-prefix 只支持 A / H / E")
    elif PUBLIC_MODE:
        REPLAY_PREFIX = "E"                            # 对外实例默认 E 段，统计上一眼可辨
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
    if PUBLIC_MODE:
        # 对外实例按设计不恢复历史会话（隔离 CPU/内存），必须立刻放行：
        # 否则 RESTORING 永远为真 -> 未知/过期 sid 会一直回 503「恢复中」而不是 404。
        RESTORING = False
    else:
        threading.Thread(target=restore_sessions, daemon=True).start()   # 端口先就绪，会话后台恢复
    threading.Thread(target=session_sweeper, daemon=True).start()       # 空闲会话回收
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
