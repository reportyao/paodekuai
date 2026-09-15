#!/usr/bin/env python3
"""跑得快 · 两人对战 - 静态服务器 + AI 桥同源反向代理（纯 Python 标准库）

- 静态托管本目录（index.html / app.js / style.css）
- /ai/* 透明转发到本机 AI 桥（默认 http://127.0.0.1:8766），
  让网页与 AI 桥同源，避免跨域，也无需把桥端口暴露公网。
  可用环境变量 PDK_AI_BRIDGE 覆盖上游地址。
"""
import http.server
import json
import os
import socketserver
import sys
import threading
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import replay_report  # noqa: E402  对局编号/读取/人工点评（与 CLI 同一实现）

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8310
AI_UPSTREAM = os.environ.get('PDK_AI_BRIDGE', 'http://127.0.0.1:8766').rstrip('/')
ONLINE_UPSTREAM = os.environ.get('PDK_ONLINE', 'http://127.0.0.1:8311').rstrip('/')
os.chdir(os.path.dirname(os.path.abspath(__file__)))


# ---- /ai/* 代理限流（保护 AI 算力：8310 不经 nginx，所以在这一层做）----
# 网页版正常打牌约 1-3 请求/秒（含镜像+决策+提示），这里给到 300 次/分钟/IP 的宽松额度，
# 只拦"扫描/刷量/脚本滥用"；设 PDK_AI_RATE=0 可完全关闭。
AI_RATE_PER_MIN = int(os.environ.get('PDK_AI_RATE', '300'))
AI_RATE_LOCK = threading.Lock()
AI_HITS: dict = {}                      # ip -> [时间戳...]


def ai_rate_limited(ip: str) -> bool:
    """滑动 60 秒窗口计数；超限返回 True。"""
    if AI_RATE_PER_MIN <= 0:
        return False
    now = time.time()
    with AI_RATE_LOCK:
        hits = AI_HITS.setdefault(ip, [])
        cutoff = now - 60
        while hits and hits[0] < cutoff:
            hits.pop(0)
        if len(hits) >= AI_RATE_PER_MIN:
            return True
        hits.append(now)
        if len(AI_HITS) > 5000:         # 防内存膨胀：清理空桶
            for k in [k for k, v in AI_HITS.items() if not v]:
                AI_HITS.pop(k, None)
        return False


class Handler(http.server.SimpleHTTPRequestHandler):
    extensions_map = {
        **http.server.SimpleHTTPRequestHandler.extensions_map,
        '.js': 'application/javascript; charset=utf-8',
        '.css': 'text/css; charset=utf-8',
        '.html': 'text/html; charset=utf-8',
    }

    def _json(self, obj, code=200):
        data = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def end_headers(self):
        self.send_header('Cache-Control', 'no-store')
        super().end_headers()

    # ---- /ai/* -> AI 桥反向代理；/online/* -> 在线对战服务反向代理 ----
    def _is_ai(self):
        return self.path == '/ai' or self.path.startswith('/ai/')

    def _is_online(self):
        return self.path == '/online' or self.path.startswith('/online/')

    def _proxy(self, upstream, strip):
        import urllib.error
        if strip == '/ai' and ai_rate_limited(self.client_address[0]):
            body = json.dumps({'error': f'请求过于频繁（每 IP 每分钟 {AI_RATE_PER_MIN} 次），'
                                        f'请稍后重试'}).encode()
            self.send_response(429)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Retry-After', '10')
            self.end_headers()
            self.wfile.write(body)
            print(f'[rate] 429 {self.client_address[0]} {self.path}', flush=True)
            return
        target = upstream + self.path[len(strip):]
        n = int(self.headers.get('Content-Length', 0) or 0)
        body = self.rfile.read(n) if n else None
        req = urllib.request.Request(target, data=body, method=self.command)
        req.add_header('Content-Type', self.headers.get('Content-Type', 'application/json'))
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                data = r.read()
                self.send_response(r.status)
                self.send_header('Content-Type', r.headers.get('Content-Type', 'application/json'))
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)
        except urllib.error.HTTPError as e:
            data = e.read()                      # 上游 4xx/5xx 原样透传（保留真实错误）
            self.send_response(e.code)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except Exception as e:
            data = json.dumps({'error': f'upstream unreachable: {e}'}).encode()
            self.send_response(502)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    # ---- /replays/* 对局记录（编号定位 + 逐手详情 + 人工点评）----
    def _replays(self, method):
        import urllib.parse
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        try:
            if u.path == "/replays/list":
                cidx = replay_report.comments_index()
                games = []
                for d in replay_report.load_games():
                    no = str(d.get("no", ""))
                    tag = (f"{d['code']}-r{d['round']}" if d.get("source") == "online_room"
                           else str(d.get("sid", d.get("_file", "")))[:10])
                    games.append({
                        "no": no, "time": replay_report.time_of(d),
                        "kind": replay_report.kind_of(d), "tag": tag,
                        "participants": replay_report.participants(d),
                        "moves": len(d.get("moves", d.get("codes", []))),
                        "comments": cidx.get(no, 0),
                        "result": replay_report.result_of(d),
                        "live": bool(d.get("live")),
                        "source": d.get("source", "ai_bridge"),
                    })
                return {"games": games}
            if u.path == "/replays/get":
                no = (q.get("no") or [""])[0]
                d = replay_report.find_by_id(no)
                if not d:
                    return {"error": f"找不到对局 {no}"}
                game = {k: v for k, v in d.items() if not k.startswith("_")}
                game["_file"] = d.get("_file", "")
                if not game.get("moves") and game.get("codes"):
                    # 旧格式：只有动作码 -> 生成“点数文本”逐手（无花色），前端照常显示
                    game["legacyMoves"] = [
                        {"ply": i + 1, "seat": i % 2,
                         "text": replay_report.code_str(cd), "pass": cd == 0}
                        for i, cd in enumerate(game["codes"])
                    ]
                return {"game": game, "comments": replay_report.load_comments(str(d.get("no", "")))}
            if u.path == "/replays/comment" and method == "POST":
                n = int(self.headers.get("Content-Length", 0) or 0)
                raw = self.rfile.read(n) or b"{}"
                try:
                    payload = json.loads(raw.decode("utf-8"))
                except UnicodeDecodeError:                 # 少数客户端按 GBK 发中文
                    payload = json.loads(raw.decode("gbk", "replace"))
                no = str(payload.get("no", "")).upper()
                text = str(payload.get("text", "")).strip()
                if not no or not text:
                    return {"error": "no 与 text 必填"}
                ply = payload.get("ply")
                ply = int(ply) if ply not in (None, "", "null") else None
                author = str(payload.get("author", "") or "人工")[:12]
                rec = replay_report.save_comment(no, text, ply, author)
                return {"ok": True, "saved": rec,
                        "comments": replay_report.load_comments(no)}
            return {"error": "unknown replays endpoint"}
        except Exception as e:
            return {"error": str(e)}

    def do_GET(self):
        if self.path.startswith("/replays/"):
            return self._json(self._replays("GET"))
        if self._is_ai():
            return self._proxy(AI_UPSTREAM, '/ai')
        if self._is_online():
            return self._proxy(ONLINE_UPSTREAM, '/online')
        super().do_GET()

    def do_POST(self):
        if self.path.startswith("/replays/"):
            return self._json(self._replays("POST"))
        if self._is_ai():
            return self._proxy(AI_UPSTREAM, '/ai')
        if self._is_online():
            return self._proxy(ONLINE_UPSTREAM, '/online')
        self.send_error(404)


class Server(socketserver.ThreadingTCPServer):
    # Linux: 必须置 True —— 否则 TIME_WAIT 残留会让重启报 EADDRINUSE（端口却看着空闲）；
    # Linux 上 SO_REUSEADDR 不允许两个存活监听，双实例仍会失败。
    # Windows: 置 False —— SO_REUSEADDR 会允许双实例同时绑定抢连接。
    allow_reuse_address = (os.name != 'nt')


if __name__ == '__main__':
    with Server(('0.0.0.0', PORT), Handler) as httpd:
        print(f'跑得快已启动: http://127.0.0.1:{PORT}/  (AI代理 -> {AI_UPSTREAM}, Ctrl+C 退出)')
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass
