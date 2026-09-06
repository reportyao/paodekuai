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
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

DEFAULT_BOT_ROOT = Path(__file__).resolve().parent.parent / "pdk_ai_work" / "pdk_ai"
BOT_ROOT = DEFAULT_BOT_ROOT
if not BOT_ROOT.exists():
    BOT_ROOT = Path(__file__).resolve().parent      # 允许把桥放到 pdk_ai 目录内运行
if str(BOT_ROOT) not in sys.path:
    sys.path.insert(0, str(BOT_ROOT))

import server as bot_server          # noqa: E402  (复用 build_ai / DMC 兜底内核)
from pdk import fast                 # noqa: E402
from pdk.core import Config          # noqa: E402
from pdk.engine import Game, counts_of_ids  # noqa: E402
from pdk.fast import PASS_CODE       # noqa: E402

SESSIONS: dict = {}
LOCK = threading.Lock()
MAX_SESSIONS = 60
REPLAY_DIR = Path(__file__).resolve().parent / "data" / "replays"
REPLAY_DIR.mkdir(parents=True, exist_ok=True)


def save_replay(sid: str, s: Shadow):
    if getattr(s, "saved", False) or not s.game.finished:
        return
    payload = {
        "version": 1, "source": "ai_bridge", "sid": sid,
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "hands": s.init_payload["hands"], "kitty": s.init_payload.get("kitty", []),
        "leader": s.init_payload["leader"], "opts": s.init_payload["opts"],
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

    def __init__(self, hands, kitty, leader, opts):
        self.created = time.time()
        self.cfg = build_cfg(opts)
        h0, h1, k = sorted(hands[0]), sorted(hands[1]), sorted(kitty)
        assert len(h0) == 16 and len(h1) == 16 and len(k) == 16, "需要两手16张和扣底16张"
        assert sorted(h0 + h1 + k) == sorted(bot_server.DECK), "两手牌+扣底必须恰好构成48张"
        self.game = Game(cfg=self.cfg, first_player=leader, hands=[h0, h1], kitty=k)
        self.cg = fast.CGame(counts_of_ids(h0), counts_of_ids(h1), leader, self.cfg)
        self.agent, self.mode = bot_server.build_ai(None)
        self.ai_seat = 1
        self.agent.new_game(self.ai_seat, list(self.game.cnt[self.ai_seat]))
        self.codes = []
        self.init_payload = {"hands": [h0, h1], "kitty": k, "leader": leader, "opts": opts}
        self.bad = False                     # 影子失效 -> 通知网页版降级
        self.lock = threading.Lock()

    # 与 pdk_ai server._apply 相同的镜像顺序：先 observe 后落子
    def _apply(self, code: int):
        trick_before = (None if self.cg.g.trick[0] == 255
                        else tuple(self.cg.g.trick))
        seat_abs = int(self.game.turn)
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
    s = Shadow(hands, kitty, leader, opts)
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
    rep = Shadow(init["hands"], init.get("kitty", []), init["leader"], init["opts"])
    with rep.lock:
        for code in codes:
            rep._apply(code)
        if rep.game.finished or int(rep.game.turn) != 0:
            return {"fallback": True}, 200
        code = rep.agent.act(rep.cg)
        cards = list(fast.code_to_cards(code, rep.game.hand_ids(0)))
    return {"cards": cards}, 200


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
                self._json({"ok": True, "agent": "hybrid", "sessions": n, "v": 3})
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
            else:
                body, code = {"error": "unknown endpoint"}, 404
            self._json(body, code)
        except KeyError as e:
            self._json({"error": f"missing {e}"}, 400)
        except Exception as e:
            import traceback
            self._json({"error": str(e), "tb": traceback.format_exc()[-1500:]}, 500)


class BridgeServer(ThreadingHTTPServer):
    # Windows 上 SO_REUSEADDR 会允许双实例同时绑同一端口（连接被旧实例抢走），
    # 显式关掉：端口被占时新进程直接报错退出，问题可见。
    allow_reuse_address = False


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
