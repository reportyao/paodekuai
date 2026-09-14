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
  各端点失败时尽量返回 {"fallback": true}，网页版据此降级到内置贪心 AI。

牌 id 语义与 pdk_ai 相同: id = (rankIdx<<2)|suit, rankIdx 0=3..11=A, 12=2(仅黑桃)。
"""
from __future__ import annotations

import argparse
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
    agent = SolverAgent(fb, bot_server.CFG, total_threshold=28,
                        max_rows=400000, engine=engine)
    return agent, ("hybrid" if engine == "c" else engine), net_info(fb)


SESSIONS: dict = {}
LOCK = threading.Lock()
MAX_SESSIONS = 60
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


def save_replay(sid: str, s: Shadow):
    if getattr(s, "saved", False) or not s.game.finished:
        return
    payload = {
        "no": next_replay_no(REPLAY_DIR, "A"),        # 对局编号（A0001…），方便定位
        "timeText": time.strftime("%Y-%m-%d %H:%M:%S"),  # 本地可读时间
        "version": 2, "source": "ai_bridge", "sid": sid,
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "names": ["human", "ai"], "mode": getattr(s, "prod_mode", "hybrid"),
        "net": getattr(s, "net", ""),
        "humanSeat": 0,                       # 座位0 = 真人，座位1 = AI
        "hands": s.init_payload["hands"], "kitty": s.init_payload.get("kitty", []),
        "leader": s.init_payload["leader"], "opts": s.init_payload["opts"],
        # moves: 每一手的完整明细（牌 id 可直接读；combo.ptype 0单1对2连对3三4三带二5三带一6飞机7顺子8炸弹9四带三）
        "moves": getattr(s, "moves_detail", []),
        "codes": s.codes, "winner": s.game.winner, "scores": list(s.game.scores or (0, 0)),
    }
    (REPLAY_DIR / f"{sid}.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
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

    def __init__(self, hands, kitty, leader, opts, prod_mode="hybrid"):
        self.created = time.time()
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
        self.bad = False                     # 影子失效 -> 通知网页版降级
        self.lock = threading.Lock()

    # 与 pdk_ai server._apply 相同的镜像顺序：先 observe 后落子
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

    def legal_ids(self, seat: int):
        return [list(fast.code_to_cards(c, self.game.hand_ids(seat)))
                for c in self.cg.legal()]


def get_sess(sid: str) -> Shadow:
    with LOCK:
        return SESSIONS[sid]


def evict_old():
    with LOCK:
        while len(SESSIONS) > MAX_SESSIONS:
            k = min(SESSIONS, key=lambda s: SESSIONS[s].created)
            SESSIONS.pop(k, None)


def handle_init(p: dict):
    hands = p["hands"]
    kitty = p.get("kitty", [])
    leader = int(p.get("leader", 0))
    opts = p.get("opts", {})
    prod_mode = str(p.get("mode", "hybrid")).lower()
    if prod_mode not in PROD_MODES:
        return {"error": f"unknown mode '{prod_mode}' (choose hybrid|dual)"}, 400
    s = Shadow(hands, kitty, leader, opts, prod_mode)
    sid = uuid.uuid4().hex[:12]
    with LOCK:
        SESSIONS[sid] = s
    evict_old()
    # 可选：创建后即重放一串动作（断线重同步用）
    for mv in p.get("actions", []):
        ok, err = do_action(sid, int(mv["seat"]), mv.get("cards", []))
        if not ok:
            # 重放失败：不返回 sid，客户端会视为同步失败（避免拿到半重放会话）
            SESSIONS.pop(sid, None)
            return {"error": "replay failed: " + err}, 400
    return {"sid": sid, "ai_seat": s.ai_seat, "mode": s.mode}, 200


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
            return {"fallback": True}, 200
        if s.game.finished:
            return {"finished": True}, 200
        if int(s.game.turn) != s.ai_seat:
            return {"error": f"not ai turn (turn={s.game.turn})"}, 400
        try:
            code = s.agent.act(s.cg)
            # 注意顺序：先由当前手牌把 code 映射成具体牌 id，再落子镜像
            # （落子后手牌已移除，code_to_cards 会取不到牌）
            cards = list(fast.code_to_cards(code, s.game.hand_ids(s.ai_seat)))
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
        if s.bad or s.game.finished:
            return {"fallback": True}, 200
        init = s.init_payload
        codes = list(s.codes)
    rep = Shadow(init["hands"], init.get("kitty", []), init["leader"], init["opts"], getattr(s, "prod_mode", "hybrid"))
    with rep.lock:
        # 关键：让内核以「座位0」视角重放决策（Belief 必须以被建议方的手牌构建）
        rep.agent.new_game(0, list(rep.game.cnt[0]))
        for code in codes:
            rep._apply(code)
        if rep.game.finished or int(rep.game.turn) != 0:
            return {"fallback": True}, 200
        code = rep.agent.act(rep.cg)
        cards = list(fast.code_to_cards(code, rep.game.hand_ids(0)))
    return {"cards": cards}, 200


def do_decide(p: dict):
    """无状态单步决策（pdk-ai 生产内核原生接口），供双真人模式的深度提示使用。

    失败返回 fallback:true，前端回退内置提示。
    """
    try:
        return bot_server._decide(p), 200
    except Exception as e:
        return {"error": str(e), "fallback": True}, 200


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
                            "botRoot": str(BOT_ROOT), "sessions": n, "v": 5,
                            "assets": verify_assets()})
            elif u.path == "/legal":
                from urllib.parse import parse_qs
                q = parse_qs(u.query)
                s = get_sess(q["sid"][0])
                with s.lock:
                    seat = int(q["seat"][0]) if "seat" in q else int(s.game.turn)
                    self._json({"seat": seat, "legal": s.legal_ids(seat)})
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
            else:
                body, code = {"error": "unknown endpoint"}, 404
            self._json(body, code)
        except KeyError as e:
            self._json({"error": f"missing {e}"}, 400)
        except Exception as e:
            import traceback
            self._json({"error": str(e), "tb": traceback.format_exc()[-1500:]}, 500)


class BridgeServer(ThreadingHTTPServer):
    # Linux 需要 SO_REUSEADDR 以避免 TIME_WAIT 阻止重启；Windows 关闭以防双实例抢连接。
    allow_reuse_address = (os.name != 'nt')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8766)
    ap.add_argument("--bot-root", default=str(DEFAULT_BOT_ROOT))
    args = ap.parse_args()

    srv = BridgeServer(("127.0.0.1", args.port), Handler)
    print(f"AI bridge on http://127.0.0.1:{args.port}  (bot root: {BOT_ROOT})", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
