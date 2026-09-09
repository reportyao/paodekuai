#!/usr/bin/env python3
"""跑得快 · 在线双人对战服务器（房间制，纯标准库 + pdk 引擎权威）。

- 房主创建房间得到 4 位房号，好友凭房号加入，两人各在一台设备看自己的牌。
- 牌局权威在本进程（pdk.engine.Game）：发牌/必出/报单/结算全部由引擎保证，
  客户端只提交动作并轮询自己的视角（永不下发对手手牌）。
- 接口（全部 JSON，供网页同源反代 /online/* 调用）：
  POST /api/create {name, rounds, opts}      -> {code, token, seat}
  POST /api/join   {code, name}              -> {token, seat}
  GET  /api/state?token=...                  -> 该玩家视角快照（含合法动作）
  POST /api/act    {token, cards:[id]|pass:true} -> 提交动作
  POST /api/next   {token}                   -> 下一局（终局后任意一方可开）
  GET  /health                             -> {ok, rooms}

启动：  python online_server.py --port 8311
"""
from __future__ import annotations

import argparse
import json
import random
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

BOT_ROOT = Path(__file__).resolve().parent.parent / "pdk-ai-prod"
if not BOT_ROOT.exists():
    BOT_ROOT = Path(__file__).resolve().parent.parent / "pdk_ai_work" / "pdk_ai"
if str(BOT_ROOT) not in sys.path:
    sys.path.insert(0, str(BOT_ROOT))

from pdk.core import Config, DECK  # noqa: E402
from pdk.engine import Game, counts_of_ids  # noqa: E402
from pdk import fast  # noqa: E402

ROOMS: dict = {}
LOCK = threading.Lock()
ROOM_TTL = 60 * 60 * 6          # 6 小时无活动清理


def build_cfg(o: dict) -> Config:
    return Config(
        triple_no_follow=bool(o.get("sanzhang", False)),
        bomb_indivisible=bool(o.get("nobomb", True)),
        heart_ten_double=bool(o.get("red10", False)),
        four_with_three=bool(o.get("four3", False)),
    )


class Room:
    def __init__(self, code: str, name0: str, rounds: int, opts: dict):
        self.code = code
        self.lock = threading.Lock()
        self.created = time.time()
        self.lastActive = time.time()
        self.tokens = {}                      # token -> seat
        self.names = ["", ""]
        self.opts = opts
        self.rounds = rounds
        self.roundNo = 0
        self.total = [0, 0]
        self.history = []
        self.cfg = build_cfg(opts)
        self.game = None
        self.initial = [[], []]
        self.kitty = []
        self.roundMoves = []                  # [{seat, cards(pdk id)|[], pass, ts}]
        self.roundLeader = 0
        self.lastWinner = None
        self.playsMade = [0, 0]
        self.shown = [None, None]
        self.roundResult = None
        self.join(name0)

    def join(self, name: str) -> tuple[str, int]:
        with self.lock:
            seat = next((s for s in (0, 1) if not self.names[s]), None)
            if seat is None:
                raise ValueError("房间已满")
            token = uuid.uuid4().hex[:16]
            self.tokens[token] = seat
            self.names[seat] = name or f"玩家{seat + 1}"
            self.lastActive = time.time()
            if all(self.names):
                self.start_round()
            return token, seat

    def deal(self):
        deck = list(DECK)
        random.SystemRandom().shuffle(deck)
        self.kitty = sorted(deck[:16])
        hands = [sorted(deck[16:32]), sorted(deck[32:48])]
        self.game = Game(cfg=self.cfg, first_player=self.roundLeader,
                         hands=[hands[0], hands[1]], kitty=self.kitty)
        self.initial = [hands[0], hands[1]]
        self.roundMoves = []
        self.playsMade = [0, 0]
        self.shown = [None, None]
        self.roundResult = None
        self.roundNo += 1

    def start_round(self):
        if self.roundNo == 0:
            # 首局：先发牌才知道黑桃3在谁手 -> 直接发，引擎自带定先
            self.roundLeader = 0
            self.deal()
            self.roundLeader = int(self.game.turn)   # 引擎按黑桃3定先
            self.game.turn = self.roundLeader
            self.roundLeader = self.roundLeader
        else:
            self.roundLeader = self.lastWinner if self.lastWinner is not None else 0
            self.deal()

    # ---------- 动作 ----------
    def act(self, seat: int, cards: list, want_pass: bool):
        g = self.game
        if g is None or g.finished:
            return {"error": "对局不在进行中"}
        if int(g.turn) != seat:
            return {"error": "还没轮到你"}
        legal = g.legal_moves()
        if want_pass:
            if cards:
                return {"error": "过牌不应带牌"}
            code = 0
            if code not in legal:
                return {"error": "有牌必打：你不能过"}
        else:
            if not cards:
                return {"error": "空出牌（要过请传 pass）"}
            code = fast.cards_to_code(cards)
            if code not in legal:
                return {"error": "不合法的出牌"}
        trick_before = (None if g.trick is None else
                        (int(g.trick[0]), int(g.trick[1]), int(g.trick[2]), int(g.trick[3])))
        g.play(code)
        self.cg_step_safe(code)
        self.roundMoves.append({"seat": seat, "cards": list(cards) if cards else [],
                                "pass": want_pass,
                                "pass_on": trick_before if want_pass else None,
                                "ts": time.time()})
        if not want_pass:
            self.playsMade[seat] += 1
            self.shown[seat] = {"cards": list(cards), "pass": False}
        else:
            self.shown[seat] = {"pass": True}
        self.lastActive = time.time()
        if g.finished:
            self.lastWinner = int(g.winner)
            self.finish_round()
        return {"ok": True}

    def cg_step_safe(self, code):
        # 引擎为权威即可；此处无需 CGame（在线模式不跑深度 AI）
        return

    def finish_round(self):
        g = self.game
        winner = int(g.winner)
        loser = 1 - winner
        rem = g.hand_count(loser)
        shut = self.playsMade[loser] == 0
        base = 0 if rem == 1 else rem
        if shut:
            base *= 2
        scores = list(g.scores) if g.scores else [base, -base]
        self.total[0] += scores[0]
        self.total[1] += scores[1]
        self.roundResult = {
            "winner": winner, "rem": rem, "shut": shut, "base": base,
            "delta": scores,
        }
        self.history.append({"round": self.roundNo, "winner": winner,
                             "rem": rem, "shut": shut, "delta": scores})

    # ---------- 视角快照 ----------
    def state_for(self, seat: int) -> dict:
        g = self.game
        st = {
            "seat": seat,
            "names": self.names,
            "round": self.roundNo, "rounds": self.rounds,
            "total": self.total, "history": self.history[-30:],
            "phase": ("waiting" if g is None else
                      ("roundEnd" if g.finished else "playing")),
            "turn": (int(g.turn) if g else -1),
            "hand": sorted(g.hand_ids(seat)) if g else [],
            "oppN": g.hand_count(1 - seat) if g else 16,
            "last": None, "shown": self.shown,
            "oppShown": self.shown[1 - seat],
            "myShown": self.shown[seat],
            "legal": [],
            "result": self.roundResult,
            "matchEnd": bool(self.roundNo >= self.rounds and self.roundResult),
            "kittyN": len(self.kitty),
        }
        if g and not g.finished and int(g.turn) == seat:
            st["legal"] = [list(fast.code_to_cards(cd, g.hand_ids(seat)))
                           for cd in g.legal_moves()]
            st["legalPass"] = 0 in g.legal_moves()
        if g:
            t4 = None if g.trick is None else (int(g.trick[0]), int(g.trick[1]),
                                               int(g.trick[2]), int(g.trick[3]))
            st["last"] = ({"seat": 1 - seat, "trick": t4} if t4 else None)
        return st

    def seat_of(self, token: str) -> int:
        s = self.tokens.get(token)
        if s is None:
            raise ValueError("无效的 token")
        return s


def get_room_by_token(token: str) -> tuple[Room, int]:
    for room in ROOMS.values():
        if token in room.tokens:
            return room, room.tokens[token]
    raise ValueError("房间不存在或已过期")


def evict():
    now = time.time()
    for code in [c for c, r in ROOMS.items() if now - r.lastActive > ROOM_TTL]:
        ROOMS.pop(code, None)


def handle_create(p: dict):
    code = f"{random.SystemRandom().randrange(10000):04d}"
    while code in ROOMS:
        code = f"{random.SystemRandom().randrange(10000):04d}"
    room = Room(code, str(p.get("name", ""))[:12],
                int(p.get("rounds", 10)), p.get("opts", {}))
    with LOCK:
        ROOMS[code] = room
    evict()
    token = next(iter(room.tokens))          # 房主的 token（seat 0）
    return {"code": code, "token": token, "seat": 0}


def handle_join(p: dict):
    code = str(p.get("code", "")).strip()
    room = ROOMS.get(code)
    if not room:
        return {"error": "房间不存在"}
    token, seat = room.join(str(p.get("name", ""))[:12])
    return {"code": code, "token": token, "seat": seat}


def handle_state(p: dict):
    token = p.get("token", "")
    room, seat = get_room_by_token(token)
    st = room.state_for(seat)
    st["code"] = room.code
    return st


def handle_act(p: dict):
    token = p.get("token", "")
    room, seat = get_room_by_token(token)
    with room.lock:
        return room.act(seat, list(p.get("cards", []) or []), bool(p.get("pass")))


def handle_next(p: dict):
    token = p.get("token", "")
    room, _ = get_room_by_token(token)
    with room.lock:
        if room.game is None or not room.game.finished:
            return {"error": "本局尚未结束"}
        if room.roundNo >= room.rounds:
            return {"error": "比赛已全部结束"}
        room.start_round()
    return {"ok": True}


class Handler(BaseHTTPRequestHandler):
    def _json(self, obj, code=200):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
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
        u = urlparse(self.path)
        if u.path == "/health":
            with LOCK:
                n = len(ROOMS)
            self._json({"ok": True, "rooms": n})
        elif u.path == "/api/state":
            try:
                self._json(handle_state(dict(pair.split("=", 1) for pair in
                                             (u.query.split("&") if u.query else []))))
            except ValueError as e:
                self._json({"error": str(e)}, 404)
            except Exception as e:
                self._json({"error": str(e)}, 500)
        else:
            self._json({"error": "unknown endpoint"}, 404)

    def do_POST(self):
        try:
            n = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(n) or b"{}")
            u = urlparse(self.path)
            if u.path == "/api/create":
                self._json(handle_create(payload))
            elif u.path == "/api/join":
                self._json(handle_join(payload))
            elif u.path == "/api/act":
                self._json(handle_act(payload))
            elif u.path == "/api/next":
                self._json(handle_next(payload))
            else:
                self._json({"error": "unknown endpoint"}, 404)
        except ValueError as e:
            self._json({"error": str(e)}, 400)
        except Exception as e:
            self._json({"error": str(e)}, 500)


class Server(ThreadingHTTPServer):
    allow_reuse_address = False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8311)
    args = ap.parse_args()
    srv = Server(("127.0.0.1", args.port), Handler)
    print(f"paodekuai online server on 127.0.0.1:{args.port}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
