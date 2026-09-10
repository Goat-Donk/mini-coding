"""测试用的假 MCP server（stdio JSON-RPC）：给 tests/test_mcp.py 当对端。

**不是 mock**：这是一个真实的子进程、真实的管道、真实的 JSON-RPC 往返——
测试因此能覆盖握手、跨进程读超时、server 崩溃等只有真进程才会暴露的问题。

支持：initialize / notifications/initialized / tools/list / tools/call。
工具：echo（只读，声明 readOnlyHint）、write_note（可写）、boom（返回 isError）。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

PROTOCOL_VERSION = "2025-06-18"
NOTE_PATH = Path(__file__).parent / "_mcp_note.txt"  # 由 --note-path 覆盖

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


def _send(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _handle(request: dict, note_path: Path) -> None:
    method = request.get("method")
    request_id = request.get("id")

    if method == "initialize":
        _send({"jsonrpc": "2.0", "id": request_id, "result": {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "fake-mcp", "version": "0.0.1"},
        }})
    elif method == "notifications/initialized":
        pass  # 通知无需响应
    elif method == "tools/list":
        _send({"jsonrpc": "2.0", "id": request_id, "result": {"tools": TOOLS}})
    elif method == "tools/call":
        params = request.get("params") or {}
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if name == "echo":
            _send({"jsonrpc": "2.0", "id": request_id, "result": {
                "content": [{"type": "text", "text": f"echo: {arguments.get('text', '')}"}],
                "isError": False,
            }})
        elif name == "write_note":
            note_path.write_text(str(arguments.get("text", "")), encoding="utf-8")
            _send({"jsonrpc": "2.0", "id": request_id, "result": {
                "content": [{"type": "text", "text": f"已写入 {note_path.name}"}],
                "isError": False,
            }})
        elif name == "boom":
            _send({"jsonrpc": "2.0", "id": request_id, "result": {
                "content": [{"type": "text", "text": "炸了"}],
                "isError": True,
            }})
        else:
            _send({"jsonrpc": "2.0", "id": request_id, "error": {
                "code": -32601, "message": f"未知工具 {name}",
            }})
    elif request_id is not None:
        _send({"jsonrpc": "2.0", "id": request_id, "error": {
            "code": -32601, "message": f"未知方法 {method}",
        }})


def main() -> None:
    note_path = NOTE_PATH
    if "--note-path" in sys.argv:
        note_path = Path(sys.argv[sys.argv.index("--note-path") + 1])
    if "--crash-immediately" in sys.argv:
        sys.stderr.write("故意崩溃\n")
        sys.exit(3)

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            continue
        if "--crash-on-call" in sys.argv and request.get("method") == "tools/call":
            sys.stderr.write("调用时崩溃\n")
            sys.exit(4)
        if "--silent-on" in sys.argv:
            # 故意不响应：给"客户端读超时"路径造真实场景
            if request.get("method") == sys.argv[sys.argv.index("--silent-on") + 1]:
                continue
        _handle(request, note_path)


if __name__ == "__main__":
    main()
