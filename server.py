#!/usr/bin/env python3
"""跑得快 · 两人对战 - 静态服务器（纯 Python 标准库，无需第三方依赖）"""
import http.server
import os
import socketserver
import sys

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8310
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


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True


if __name__ == '__main__':
    with Server(('0.0.0.0', PORT), Handler) as httpd:
        print(f'跑得快已启动: http://127.0.0.1:{PORT}/  (Ctrl+C 退出)')
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass
