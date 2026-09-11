"""假 MCP server 的**协议实现**，stdio 与 HTTP 两个壳共用（M9-4 抽出）。

两个壳（`fake_mcp_server.py` 走管道、`fake_mcp_http_server.py` 走 HTTP）如果各写
一份工具/资源/提示词定义，就是本项目记录在案的「两处各写一遍 → 漂移」：改一处忘
另一处，于是"stdio 能读资源、HTTP 读不到"这种差异会以**测试全绿**的方式存在。
所以协议面只有这一份，壳只负责搬运字节。

**不是 mock**：这是真实的 JSON-RPC 语义实现，被真子进程 / 真 socket 调用。
"""
from __future__ import annotations

import base64
import json
from pathlib import Path

PROTOCOL_VERSION = "2025-06-18"
SERVER_NAME = "fake-mcp"

TOOLS = [
    {
        "name": "echo",
        "description": "回显给定文本",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "write_note",
        "description": "写一行到笔记文件（有副作用，不声明只读）",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    },
    {
        "name": "boom",
        "description": "总是失败（测 isError 路径）",
        "inputSchema": {"type": "object", "properties": {}},
    },
]

RESOURCES = [
    {
        "uri": "note://current",
        "name": "当前笔记",
        "mimeType": "text/plain",
        "description": "笔记文件的当前内容",
    },
    {
        "uri": "doc://readme",
        "name": "说明",
        "mimeType": "text/markdown",
    },
    {
        "uri": "bin://blob",
        "name": "二进制样例",
        "mimeType": "application/octet-stream",
    },
]

PROMPTS = [
    {
        "name": "summarize",
        "description": "把一段文本总结成给定风格",
        "arguments": [
            {"name": "text", "description": "要总结的文本", "required": True},
            {"name": "style", "description": "风格，默认一句话"},
        ],
    },
    {
        "name": "noargs",
        "description": "不需要参数的模板",
    },
]


class FakeServer:
    """把一条 JSON-RPC 请求变成一条响应（或 None = 通知无需响应）。"""

    def __init__(
        self,
        note_path: Path,
        *,
        resources: bool = True,
        prompts: bool = True,
        empty_resources: bool = False,
        empty_prompts: bool = False,
        require_initialized: bool = False,
    ) -> None:
        self.note_path = Path(note_path)
        self.resources = resources
        self.prompts = prompts
        #: 声明了能力但列表为空 —— 用来钉「列表为空就不注册那个工具面」：
        #: 注册了的话模型会拿到一个**永远调不通**的工具。
        self.empty_resources = empty_resources
        self.empty_prompts = empty_prompts
        #: 没收到 `notifications/initialized` 就拒绝后续请求。spec 里这条是 MUST，
        #: 而一个"什么通知都收"的假 server 永远测不出漏发 —— 漏发时我们这侧一切正常。
        self.require_initialized = require_initialized
        self.initialized = False

    def capabilities(self) -> dict:
        """能力声明 —— 客户端据此决定要不要注册 resources / prompts 工具面。"""
        caps: dict = {"tools": {}}
        if self.resources:
            caps["resources"] = {"subscribe": False, "listChanged": False}
        if self.prompts:
            caps["prompts"] = {"listChanged": False}
        return caps

    def handle(self, request: dict) -> dict | None:
        method = request.get("method")
        request_id = request.get("id")

        if method == "notifications/initialized":
            self.initialized = True
            return None                      # 通知无需响应

        if self.require_initialized and not self.initialized and method != "initialize":
            return {"jsonrpc": "2.0", "id": request_id, "error": {
                "code": -32002, "message": "还没有收到 notifications/initialized",
            }}

        if method == "initialize":
            return self._ok(request_id, {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": self.capabilities(),
                "serverInfo": {"name": SERVER_NAME, "version": "0.0.1"},
            })
        if method == "tools/list":
            return self._ok(request_id, {"tools": TOOLS})
        if method == "tools/call":
            return self._call_tool(request_id, request.get("params") or {})
        if method == "resources/list":
            # 能力没声明时按 spec 回 -32601：**客户端不该调，但真调了要能看见**
            if not self.resources:
                return self._unknown(request_id, method)
            return self._ok(request_id, {"resources": [] if self.empty_resources else RESOURCES})
        if method == "resources/read":
            if not self.resources:
                return self._unknown(request_id, method)
            return self._read_resource(request_id, request.get("params") or {})
        if method == "prompts/list":
            if not self.prompts:
                return self._unknown(request_id, method)
            return self._ok(request_id, {"prompts": [] if self.empty_prompts else PROMPTS})
        if method == "prompts/get":
            if not self.prompts:
                return self._unknown(request_id, method)
            return self._get_prompt(request_id, request.get("params") or {})
        if method == "tools/slow":
            return None                      # 故意不响应：给"客户端读超时"造真实场景
        return self._unknown(request_id, method)

    # ---------- 工具 ----------

    def _call_tool(self, request_id, params: dict) -> dict | None:
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if name == "echo":
            return self._ok(request_id, {
                "content": [{"type": "text", "text": f"echo: {arguments.get('text', '')}"}],
                "isError": False,
            })
        if name == "write_note":
            self.note_path.write_text(str(arguments.get("text", "")), encoding="utf-8")
            return self._ok(request_id, {
                "content": [{"type": "text", "text": f"已写入 {self.note_path.name}"}],
                "isError": False,
            })
        if name == "boom":
            return self._ok(request_id, {
                "content": [{"type": "text", "text": "炸了"}],
                "isError": True,
            })
        return {"jsonrpc": "2.0", "id": request_id, "error": {
            "code": -32601, "message": f"未知工具 {name}",
        }}

    # ---------- 资源 ----------

    def _read_resource(self, request_id, params: dict) -> dict:
        uri = params.get("uri")
        if uri == "note://current":
            text = self.note_path.read_text(encoding="utf-8") if self.note_path.exists() else ""
            contents = [{"uri": uri, "mimeType": "text/plain", "text": text}]
        elif uri == "doc://readme":
            contents = [{"uri": uri, "mimeType": "text/markdown", "text": "# 假 server 的说明"}]
        elif uri == "bin://blob":
            blob = base64.b64encode(b"\x00\x01\x02\x03").decode("ascii")
            contents = [{"uri": uri, "mimeType": "application/octet-stream", "blob": blob}]
        else:
            return {"jsonrpc": "2.0", "id": request_id, "error": {
                "code": -32002, "message": f"资源不存在 {uri}",
            }}
        return self._ok(request_id, {"contents": contents})

    # ---------- 提示词 ----------

    def _get_prompt(self, request_id, params: dict) -> dict:
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if name == "summarize":
            text = arguments.get("text", "")
            style = arguments.get("style") or "一句话"
            return self._ok(request_id, {
                "description": "总结模板",
                "messages": [{
                    "role": "user",
                    "content": {"type": "text", "text": f"用{style}总结：{text}"},
                }],
            })
        if name == "noargs":
            return self._ok(request_id, {
                "messages": [{"role": "user",
                              "content": {"type": "text", "text": "固定模板正文"}}],
            })
        return {"jsonrpc": "2.0", "id": request_id, "error": {
            "code": -32602, "message": f"未知提示词 {name}",
        }}

    # ---------- 整形 ----------

    @staticmethod
    def _ok(request_id, result: dict) -> dict:
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    @staticmethod
    def _unknown(request_id, method) -> dict:
        return {"jsonrpc": "2.0", "id": request_id, "error": {
            "code": -32601, "message": f"未知方法 {method}",
        }}


def dump(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False)
