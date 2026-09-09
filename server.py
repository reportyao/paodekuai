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
import urllib.request

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8310
AI_UPSTREAM = os.environ.get('PDK_AI_BRIDGE', 'http://127.0.0.1:8766').rstrip('/')
ONLINE_UPSTREAM = os.environ.get('PDK_ONLINE', 'http://127.0.0.1:8311').rstrip('/')
os.chdir(os.path.dirname(os.path.abspath(__file__)))


class Handler(http.server.SimpleHTTPRequestHandler):
    extensions_map = {
        **http.server.SimpleHTTPRequestHandler.extensions_map,
        '.js': 'application/javascript; charset=utf-8',
        '.css': 'text/css; charset=utf-8',
        '.html': 'text/html; charset=utf-8',
    }

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

    def do_GET(self):
        if self._is_ai():
            return self._proxy(AI_UPSTREAM, '/ai')
        if self._is_online():
            return self._proxy(ONLINE_UPSTREAM, '/online')
        super().do_GET()

    def do_POST(self):
        if self._is_ai():
            return self._proxy(AI_UPSTREAM, '/ai')
        if self._is_online():
            return self._proxy(ONLINE_UPSTREAM, '/online')
        self.send_error(404)


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = False   # 端口被占时报错可见，避免双实例抢连接


if __name__ == '__main__':
    with Server(('0.0.0.0', PORT), Handler) as httpd:
        print(f'跑得快已启动: http://127.0.0.1:{PORT}/  (AI代理 -> {AI_UPSTREAM}, Ctrl+C 退出)')
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass
