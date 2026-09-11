"""测试用的假 MCP server（HTTP 壳，Streamable HTTP）：给 tests/test_mcp.py 当对端。

**不是 mock**：真实进程、真实 socket、真实 HTTP 往返。协议面与 stdio 壳共用
`fake_mcp_core.py` 那一份。

它能造出几种只有真 socket 才会暴露的场景，每一种都对应客户端一条分支：

- `--mode sse`：用 `text/event-stream` 回响应（而不是 `application/json`）。
  spec 允许两种，客户端必须都认。
- `--noise`：在 SSE 流里先塞一条**通知**再发本次响应 —— 客户端按 id 关联，
  多余的必须先跳过再命中（顺序错了会拿错消息）。
- `--require-session`：请求不带有效 `Mcp-Session-Id` 就回 404。钉住"initialize
  拿到的 session id 真的被带在后续每个请求上"。
- `--expire-after N`：带 session 的请求满 N 次后会话失效，再请求回 404 ——
  钉住"会话过期 → 重新 initialize → 重试"这条自愈路径。
- `--require-accept`：`Accept` 里必须同时有 `application/json` 与
  `text/event-stream`（spec 的 MUST），否则 406。
- `--require-protocol-header`：后续请求必须带 `MCP-Protocol-Version`，否则 400。
- `--require-initialized`：没先发 `notifications/initialized` 就拒绝后续请求
  （spec 里那条通知是 MUST；"什么通知都收"的 server 永远测不出漏发）。
- `GET` 一律 405：独立 GET SSE 流我们**明确没实现**，钉住客户端不会偷偷去开它。

端口写进 `--port-file`（绑 0 让内核分配，避免测试之间抢端口）。
"""
from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from fake_mcp_core import PROTOCOL_VERSION, FakeServer, dump  # noqa: E402

NOTE_PATH = Path(__file__).parent / "_mcp_note.txt"


def _flag_value(flag: str, default=None):
    return sys.argv[sys.argv.index(flag) + 1] if flag in sys.argv else default


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    # ---- 由 main() 注入 ----
    server_impl: FakeServer
    mode: str = "json"
    noise: bool = False
    require_session: bool = False
    require_accept: bool = False
    require_protocol_header: bool = False
    expire_after: int | None = None
    redirect_to: str | None = None

    session_id: str | None = None
    session_requests: int = 0

    def log_message(self, *args) -> None:      # 别把访问日志打到 stderr
        pass

    # ---- 协议 ----

    def do_GET(self) -> None:                  # noqa: N802
        """独立 GET SSE 流：**故意不实现** —— 客户端也不该来开它。"""
        self._send_json(405, {"jsonrpc": "2.0", "id": None, "error": {
            "code": -32601, "message": "本假 server 不提供 GET SSE 流",
        }})

    def do_POST(self) -> None:                 # noqa: N802
        if self.redirect_to:
            # 故意重定向：客户端**不能**跟随（urllib 会把 POST 改写成 GET，
            # 一次 tools/call 就变成一次静默的读请求），要如实报出来让人改配置。
            self.send_response(302)
            self.send_header("Location", self.redirect_to)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        try:
            request = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            self._send_json(400, {"jsonrpc": "2.0", "id": None, "error": {
                "code": -32700, "message": f"请求不是合法 JSON: {exc}",
            }})
            return

        if self.require_accept:
            accept = self.headers.get("Accept") or ""
            if "application/json" not in accept or "text/event-stream" not in accept:
                self._send_json(406, {"jsonrpc": "2.0", "id": None, "error": {
                    "code": -32600,
                    "message": f"Accept 必须同时列出 application/json 与 "
                               f"text/event-stream，收到 {accept!r}",
                }})
                return

        is_initialize = request.get("method") == "initialize"
        if not is_initialize and self.require_protocol_header:
            if not self.headers.get("MCP-Protocol-Version"):
                self._send_json(400, {"jsonrpc": "2.0", "id": None, "error": {
                    "code": -32600, "message": "缺少 MCP-Protocol-Version 头",
                }})
                return

        if not is_initialize and self.require_session:
            if self.headers.get("Mcp-Session-Id") != self.session_id:
                self._send_session_gone()
                return

        if not is_initialize and self.session_id is not None:
            type(self).session_requests += 1
            if self.expire_after is not None and self.session_requests > self.expire_after:
                type(self).session_id = None       # 会话作废：后续一律 404
                self._send_session_gone()
                return

        response = self.server_impl.handle(request)

        extra: list[tuple[str, str]] = []
        if is_initialize:
            type(self).session_id = f"sess-{id(self) & 0xFFFF:x}"
            type(self).session_requests = 0
            extra.append(("Mcp-Session-Id", type(self).session_id))

        if response is None:                       # 通知：202 且无正文
            self.send_response(202)
            for key, value in extra:
                self.send_header(key, value)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        if self.mode == "sse":
            self._send_sse(response, extra)
        else:
            self._send_json(200, response, extra)

    # ---- 响应 ----

    def _send_session_gone(self) -> None:
        """404 + 正文里也放一条 JSON-RPC error。

        **正文里有 error 是刻意的**：客户端的错误整形若先匹配"JSON-RPC 报错"
        再判 404，就会把"会话过期"报成一次普通调用失败、永远走不到自愈那条路。
        真实 server 两种写法都有，所以假 server 挑更难的那种。
        """
        self._send_json(404, {"jsonrpc": "2.0", "id": None, "error": {
            "code": -32001, "message": "会话不存在或已过期",
        }})

    def _send_json(self, status: int, payload: dict, extra=()) -> None:
        body = dump(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        for key, value in extra:
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_sse(self, response: dict, extra=()) -> None:
        events: list[str] = []
        if self.noise:
            # 先来一条通知（没有 id）：客户端必须跳过它再命中本次响应
            events.append(_sse_event({"jsonrpc": "2.0", "method": "notifications/message",
                                      "params": {"level": "info", "data": "噪音"}}))
        events.append(_sse_event(response))
        body = "".join(events).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        for key, value in extra:
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _sse_event(payload: dict) -> str:
    return f"event: message\ndata: {dump(payload)}\n\n"


def main() -> None:
    if "--crash-immediately" in sys.argv:
        sys.stderr.write("故意崩溃\n")
        sys.exit(3)

    Handler.server_impl = FakeServer(
        Path(_flag_value("--note-path", str(NOTE_PATH))),
        resources="--no-resources" not in sys.argv,
        prompts="--no-prompts" not in sys.argv,
        empty_resources="--empty-resources" in sys.argv,
        empty_prompts="--empty-prompts" in sys.argv,
        require_initialized="--require-initialized" in sys.argv,
    )
    Handler.mode = _flag_value("--mode", "json")
    Handler.noise = "--noise" in sys.argv
    Handler.require_session = "--require-session" in sys.argv
    Handler.require_accept = "--require-accept" in sys.argv
    Handler.require_protocol_header = "--require-protocol-header" in sys.argv
    Handler.redirect_to = _flag_value("--redirect-to")
    expire = _flag_value("--expire-after")
    Handler.expire_after = int(expire) if expire else None
    Handler.session_id = None
    Handler.session_requests = 0

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port_file = _flag_value("--port-file")
    if port_file:
        Path(port_file).write_text(str(httpd.server_address[1]), encoding="utf-8")
    else:
        sys.stdout.write(f"{httpd.server_address[1]}\n")
        sys.stdout.flush()
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
