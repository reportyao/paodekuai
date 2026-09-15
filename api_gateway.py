#!/usr/bin/env python3
"""跑得快 AI 对外 API 网关 —— 给外部调用方用的鉴权 + 配额 + 审计层。

放在公网 AI 实例（ai_bridge.py --public-api，127.0.0.1:8776）前面：

    调用方 ──HTTPS(nginx /pdk-ai/)──▶ 本网关(127.0.0.1:8770) ──▶ AI 实例(127.0.0.1:8776)

职责（只做这一件事，不做业务逻辑）：
  1) 鉴权：X-API-Key / Authorization: Bearer，来自 /etc/paodekuai-api/keys.json（600 权限）
  2) 配额：每分钟速率、并发数、每日次数，超限 429 + Retry-After
  3) 可选 IP 白名单：按 key 配 allowed_ips（空/未配 = 不限制）
  4) 路径白名单：/v1/<name> -> /api/<name>（或 /health），其余一律 404
  5) 请求体上限、上游超时（超时 504）
  6) 审计日志：时间 / key 名 / 接口 / 状态 / 耗时 / 来源 IP（不记录明文 Key）
  7) CORS：默认允许（浏览器调用方需要），可用配置收紧

配置（keys.json，改完自动热加载，无需重启）：
{
  "keys": {
    "pdk_xxx": {"name": "调用方A", "per_min": 60, "per_day": 20000,
                "concurrent": 1, "allowed_ips": ["1.2.3.4"]}
  },
  "defaults": {"per_min": 60, "per_day": 20000, "concurrent": 1},
  "cors_allow_origin": "*",
  "max_body": 262144,
  "upstream_timeout": 60
}

启动：
  python api_gateway.py --port 8770 --upstream http://127.0.0.1:8776 \
      --keys /etc/paodekuai-api/keys.json --log /var/log/paodekuai-api.log
不降级：上游不通返回 502；上游 5xx 原样透传（保留真实错误）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import defaultdict, deque
from urllib.parse import quote
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# ---- 公开接口白名单：/v1/<name> -> 上游路径 ----
ROUTES = {
    "health": "/health",
    "decide": "/api/decide",
    "explain": "/api/explain",
    "analyze": "/api/analyze",
    "decode": "/api/decode",
    "new_game": "/api/new_game",
    "play": "/api/play",
    "state": "/api/state",
    "suggest": "/api/suggest",
}
ANON_ROUTES = {"health"}          # 不需要 Key（监控/探活用），但仍受限流约束

CFG = {
    "keys": {},                   # key -> {name, per_min, per_day, concurrent, allowed_ips}
    "defaults": {"per_min": 60, "per_day": 20000, "concurrent": 1},
    "cors_allow_origin": "*",
    "max_body": 256 * 1024,
    "upstream_timeout": 60,
}
CFG_PATH = ""
CFG_MTIME = 0.0
CFG_LOCK = threading.Lock()
USAGE_LOCK = threading.Lock()
USAGE = defaultdict(lambda: {"min": deque(), "day": 0, "day_key": "", "inflight": 0})


def load_keys(force: bool = False) -> None:
    """读取 keys.json（按 mtime 热加载）。"""
    global CFG_MTIME
    if not CFG_PATH:
        return
    try:
        m = os.path.getmtime(CFG_PATH)
    except OSError:
        return
    if not force and m == CFG_MTIME:
        return
    try:
        data = json.loads(Path(CFG_PATH).read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[gateway] keys 读取失败（保留旧配置）: {e}", file=sys.stderr, flush=True)
        return
    with CFG_LOCK:
        if isinstance(data.get("keys"), dict):
            CFG["keys"] = data["keys"]
        if isinstance(data.get("defaults"), dict):
            CFG["defaults"] = {**CFG["defaults"], **data["defaults"]}
        for k in ("cors_allow_origin", "max_body", "upstream_timeout"):
            if k in data:
                CFG[k] = data[k]
        CFG_MTIME = m
    print(f"[gateway] keys 已加载：{len(CFG['keys'])} 个 key", flush=True)


def key_of(handler) -> str:
    k = handler.headers.get("X-API-Key") or ""
    if not k:
        auth = handler.headers.get("Authorization") or ""
        if auth.lower().startswith("bearer "):
            k = auth[7:].strip()
    return k


def quota_hit(key: str, meta: dict, now: float):
    """返回 (是否超限, 原因, 重试秒数)。滑动分钟窗口 + 日计数 + 并发。"""
    with USAGE_LOCK:
        u = USAGE[key]
        while u["min"] and now - u["min"][0] > 60:
            u["min"].popleft()
        day = time.strftime("%Y-%m-%d", time.localtime(now))
        if u["day_key"] != day:
            u["day_key"], u["day"] = day, 0
        if u["inflight"] >= int(meta.get("concurrent", 1)):
            return True, "concurrent", 2
        if len(u["min"]) >= int(meta.get("per_min", 60)):
            return True, "rate", max(1, int(60 - (now - u["min"][0])))
        if u["day"] >= int(meta.get("per_day", 20000)):
            return True, "daily", 3600
        u["min"].append(now)
        u["day"] += 1
        u["inflight"] += 1
        return False, "", 0


def quota_done(key: str) -> None:
    with USAGE_LOCK:
        USAGE[key]["inflight"] = max(0, USAGE[key]["inflight"] - 1)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):           # 关掉默认 stderr 噪声，审计走 access()
        pass

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", str(CFG.get("cors_allow_origin", "*")))
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-API-Key, Authorization")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")

    def _send(self, code: int, obj, extra: dict | None = None, raw: bytes | None = None):
        data = raw if raw is not None else json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-AI-Gateway", "pdk-gateway/1")
        self._cors()
        for k, v in (extra or {}).items():
            self.send_header(str(k), str(v))
        self.end_headers()
        self.wfile.write(data)

    def access(self, key_name: str, route: str, status: int, ms: float):
        line = (f"{time.strftime('%Y-%m-%d %H:%M:%S')}\t{self.client_address[0]}\t"
                f"{key_name}\t{route}\t{status}\t{ms:.0f}ms")
        print(line, flush=True)
        if LOG_PATH:
            try:
                with open(LOG_PATH, "a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
            except OSError:
                pass

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self._cors()
        self.end_headers()

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def _handle(self, method: str):
        t0 = time.time()
        raw_path, _, query = self.path.partition("?")
        path = (raw_path or "/").rstrip("/") or "/"
        route = path[4:] if path.startswith("/v1/") else (path[1:] if path.startswith("/v1") else "")
        route = route or ("health" if path == "/health" else "")
        if route not in ROUTES:
            return self._send(404, {"error": f"未知接口 /v1/{route or path}（可用："
                                             + ", ".join(sorted(ROUTES)) + "）"})
        load_keys()

        key = key_of(self)
        if route not in ANON_ROUTES and not key:
            self.access("(anon)", route, 401, 0)
            return self._send(401, {"error": "缺少 API Key（请求头 X-API-Key: <你的Key>）"})
        with CFG_LOCK:
            meta = dict(CFG["defaults"])
            name = "(anon)"
            if key:
                km = CFG["keys"].get(key)
                if km is None:
                    self.access(f"({hashlib.sha256(key.encode()).hexdigest()[:8]}?)", route, 401, 0)
                    return self._send(401, {"error": "API Key 无效或已吊销"})
                meta.update({k: v for k, v in km.items() if k not in ("name", "allowed_ips")})
                name = str(km.get("name") or hashlib.sha256(key.encode()).hexdigest()[:8])
                ips = km.get("allowed_ips") or []
                if ips and self.client_address[0] not in ips:
                    self.access(name, route, 403, 0)
                    return self._send(403, {"error": "来源 IP 不在白名单"})

        over, why, retry = quota_hit(key or "(anon)", meta, t0)
        if over:
            msg = {"rate": f"超过速率限制（{meta.get('per_min')} 次/分钟）",
                   "concurrent": f"并发超限（同时最多 {meta.get('concurrent')} 个请求）",
                   "daily": f"超过每日配额（{meta.get('per_day')} 次/天）"}.get(why, "超过配额")
            self.access(name, route, 429, (time.time() - t0) * 1000)
            return self._send(429, {"error": msg}, {"Retry-After": str(retry)})

        try:
            n = int(self.headers.get("Content-Length", 0) or 0)
            if n > int(CFG.get("max_body", 262144)):
                self.access(name, route, 413, (time.time() - t0) * 1000)
                return self._send(413, {"error": f"请求体过大（{n} 字节）"})
            body = self.rfile.read(n) if n else None
            target = ROUTES[route] + (("?" + query) if query else "")
            req = urllib.request.Request(UPSTREAM + target, data=body, method=method)
            req.add_header("Content-Type", "application/json")
            # 调用方归属：按 Key 名注入（覆盖客户端自带的同名头；nginx 亦会清空该头）
            req.add_header("X-PDK-Caller", quote(name, safe=""))
            try:
                with urllib.request.urlopen(req, timeout=float(CFG.get("upstream_timeout", 60))) as r:
                    data, status = r.read(), r.status
            except urllib.error.HTTPError as e:
                data, status = e.read(), e.code            # 上游 4xx/5xx 原样透传
            except Exception as e:
                self.access(name, route, 502, (time.time() - t0) * 1000)
                return self._send(502, {"error": f"AI 服务不可达: {type(e).__name__}"})
            self.access(name, route, status, (time.time() - t0) * 1000)
            return self._send(status, None, raw=data)
        finally:
            quota_done(key or "(anon)")


class Gateway(ThreadingHTTPServer):
    allow_reuse_address = (os.name != "nt")
    daemon_threads = True


UPSTREAM = "http://127.0.0.1:8776"
LOG_PATH = ""


def main():
    global UPSTREAM, LOG_PATH, CFG_PATH
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8770)
    ap.add_argument("--bind", default="127.0.0.1")
    ap.add_argument("--upstream", default="http://127.0.0.1:8776")
    ap.add_argument("--keys", default="/etc/paodekuai-api/keys.json")
    ap.add_argument("--log", default="")
    args = ap.parse_args()
    UPSTREAM = args.upstream.rstrip("/")
    LOG_PATH = args.log
    CFG_PATH = args.keys
    load_keys(force=True)
    srv = Gateway((args.bind, args.port), Handler)
    print(f"AI 网关 on http://{args.bind}:{args.port}  ->  {UPSTREAM}  "
          f"(keys={CFG_PATH}, {len(CFG['keys'])} 个 key)", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
