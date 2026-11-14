#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""codex_server.py —— 植物图鉴（**独立服务**，不属于对局）

为什么单独一个服务，而不是塞进 webui.py 当 /codex.html：
  ① 依赖方向错了。图鉴跟对局毫无关系 —— 没开局、没连服务器、没起 netserver
     也该能翻。挂在 webui 上就等于"想看植物介绍，先起一个对局前端"。
  ② 生命周期不同。对局前端随一局开始/结束被开开关关；图鉴是想翻就翻的读物，
     不该跟着对局一起没。
  ③ 端口独立 → 图鉴和对局可以同时开着，互不抢 8000。手机上一个进程一个端口
     本来就紧张，别再往里挤。

★★★ 但它跟对局的【数据】必须是同一份（这条是硬约束，别改）：
   卡片数值、机制描述全部现读 plants_data（经 webui.build_catalog），
   图鉴自己只多一份"说法"（codex_text.py：风味 + 档案），
   **一个数字都不抄**。抄了就是本项目踩过三次的那个坑：
   `.gitattributes` 两份模板、PLANTS/PLAYABLE 两份清单、文档重复的规则段 ——
   每一次都是"改了一边忘了另一边"。
   所以这里 import webui 只借三样东西：WEB_DIR（静态文件在哪）、
   build_catalog()（数值与机制的**唯一**来源）、read_version()（页脚版本号）。
   借函数不复制实现，规则一改图鉴自动跟着变。

用法：
    python codex_server.py                 # 默认 8400，自动开浏览器
    python codex_server.py --port 8500     # 指定端口（占用会自动往后找）
    python codex_server.py --no-browser    # 手机 / 无桌面环境，只打印地址
"""

import argparse
import json
import os
import sys
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import codex_text
import webui

DEFAULT_PORT = 8400
PORT_TRY_MAX = 20          # 端口被占用时往后试几个（手机上一个进程一个口，很容易撞）

# 与 webui.MIME 一致，但**不 import 它**：这是 5 行的传输层常量，不是业务数据。
#   共用一份反而会把图鉴和 webui 的内部结构绑在一起（将来 webui 换 MIME 表就出事）。
MIME = {".html": "text/html; charset=utf-8",
        ".css": "text/css; charset=utf-8",
        ".js": "application/javascript; charset=utf-8",
        ".svg": "image/svg+xml",
        ".png": "image/png"}


class CodexHandler(BaseHTTPRequestHandler):
    """只认 5 条路由，其余一律 404 —— 图鉴没有 POST、没有 SSE、没有状态。"""

    server_version = "PlantCodex/1.0"

    # ---------- GET ----------
    def do_GET(self):
        path = self.path.split("?")[0]
        # 根路径就是图鉴本身：独立服务没有"首页"和"图鉴页"之分
        if path in ("/", "/index.html", "/codex.html", "/codex"):
            return self._file("codex.html")
        if path == "/codex.css":
            return self._file("codex.css")
        if path == "/codex.js":
            return self._file("codex.js")
        # ★ style.css 必须一起给：codex.html 引它只是为了 `:root` 里的配色变量
        #   （一套颜色只写一处，图鉴跟对局同一个色系）。少了它页面不报错、
        #   只是全部颜色失效 —— 这种"看起来是设计不对"的锅最难查。
        if path == "/style.css":
            return self._file("style.css")
        if path == "/api/catalog":
            # 数值 / 机制：与对局界面完全同源
            return self._json(webui.build_catalog())
        if path == "/api/codex-text":
            # 只是"说法"，不参与任何判定
            return self._json(codex_text.CODEX_TEXT)
        if path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
            return
        return self.send_error(404)

    def do_POST(self):
        # 图鉴是只读的：没有任何接口需要写。明确拒绝，别让人误以为它能改东西。
        return self.send_error(405)

    # ---------- 输出 ----------
    def _json(self, obj):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _file(self, name):
        path = os.path.join(webui.WEB_DIR, name)
        if not os.path.isfile(path):
            return self.send_error(404)
        with open(path, "rb") as f:
            data = f.read()
        ctype = MIME.get(os.path.splitext(name)[1], "application/octet-stream")
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        # 与对局前端同一条理由（webui 的 v0.19 注释）：本地是"即时改即时看"，
        # 缓存住旧的 codex.js 会让"改了没生效"变成深夜悬案。
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    # ---------- 日志 ----------
    def log_message(self, fmt, *args):
        # 正常请求不刷屏（图鉴一次打开 5 个请求，全打出来反而盖住启动提示）；
        # 只留出错的那几条 —— 404 往往意味着文件没跟着发出去。
        if args and str(args[1]).startswith(("4", "5")):
            sys.stderr.write("[%s] %s\n" % (self.log_date_time_string(), fmt % args))

    def handle_one_request(self):
        """连接被浏览器提前断掉时不要抛栈（用户关标签页而已，不是错误）。"""
        try:
            BaseHTTPRequestHandler.handle_one_request(self)
        except OSError:
            self.close_connection = True


def bind_server(port):
    """从 port 开始往后找一个能绑的端口。返回 (httpd, 实际端口)。

    ★ 为什么不直接失败：手机上一个进程一个端口，8300/8000 常被别的窗口占着，
      报"端口被占用"然后退出，用户还得自己去查谁占了 —— 往后挪一个更省事。
    """
    last = None
    for p in range(port, port + PORT_TRY_MAX):
        try:
            httpd = ThreadingHTTPServer(("127.0.0.1", p), CodexHandler)
            httpd.daemon_threads = True
            # 用 server_port 而不是 p：--port 0 时由系统分配，p 还是 0
            return httpd, httpd.server_port
        except OSError as exc:
            last = exc
            continue
    raise SystemExit("[错误] %d~%d 全被占用了，换一个端口：--port 8500\n（%s）"
                     % (port, port + PORT_TRY_MAX - 1, last))


def _open_browser(url):
    """返回是否真的调起了浏览器。Termux / 无桌面环境里 webbrowser.open 是
    **静默失败**（返回 False 不抛异常），所以必须收返回值，别假装打开了。"""
    try:
        return bool(webbrowser.open(url))
    except Exception:
        return False


def parse_args(argv=None):
    ap = argparse.ArgumentParser(
        description="植物图鉴（独立页面，不连对局）",
        add_help=True)
    ap.add_argument("--port", type=int, default=DEFAULT_PORT,
                    help="监听端口，默认 %d（占用会自动往后找）" % DEFAULT_PORT)
    ap.add_argument("--no-browser", action="store_true",
                    help="不自动打开浏览器（手机 / Termux 用这个）")
    return ap.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    httpd, port = bind_server(args.port)
    url = "http://127.0.0.1:%d/" % port

    print()
    print("植物图鉴 —— 独立页面（跟对局无关，没开局也能看）")
    print("  · 地址：%s" % url)
    print("  · %d 株可玩植物；按编号（如 45）或名称（如 噬魂）搜索"
          % len(webui.build_catalog()["plants"]))
    print("  · 只监听 127.0.0.1 —— 只有本机能看，别的设备连不上")
    print("  （Ctrl+C 退出）")
    print()

    if not args.no_browser:
        if _open_browser(url):
            print("  已在浏览器里打开。")
        else:
            print("  [提示] 没能自动打开浏览器（手机 / 无桌面环境都这样）。")
            print("         请自己复制上面的地址到浏览器打开。")
        print()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n  已退出。")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
