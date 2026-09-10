"""M6-3 tests: MCP 客户端（真子进程 + 真管道 + 真 JSON-RPC，不是 mock）。

覆盖：握手与 serverInfo / tools/list / tools/call（成功·isError·未知工具）/
只读注解透传 / 参数不做本地 pydantic 校验 / 与内置工具重名加前缀 /
server 崩溃与启动失败不断主流程。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from agent.mcp import MCPClient, MCPError, MCPToolAdapter, load_mcp_servers
from agent.tools.base import ToolContext, ToolRegistry
from agent.tools.files import ReadTool

SERVER = Path(__file__).parent / "fake_mcp_server.py"


def server_command(*extra: str) -> list[str]:
    return [sys.executable, str(SERVER), *extra]


@pytest.fixture
def client(tmp_path):
    c = MCPClient(server_command("--note-path", str(tmp_path / "note.txt")), name="fake")
    c.start()
    yield c
    c.close()


def test_handshake_exposes_server_info(client):
    """initialize 往返：拿到 protocolVersion 与 serverInfo。"""
    assert client.protocol_version == "2025-06-18"
    assert client.server_info["name"] == "fake-mcp"


def test_list_tools(client):
    """tools/list 返回远端工具描述。"""
    tools = client.list_tools()
    assert {t["name"] for t in tools} == {"echo", "write_note", "boom"}


def test_call_tool_success_and_readonly_hint(client):
    """tools/call 成功路径：content 拍平成文本；只读注解被透传。"""
    result = client.call_tool("echo", {"text": "你好"})
    assert result.success is True
    assert result.output == "echo: 你好"

    adapters = {t["name"]: MCPToolAdapter(client, t) for t in client.list_tools()}
    assert adapters["echo"].is_read_only() is True        # 声明了 readOnlyHint
    assert adapters["write_note"].is_read_only() is False  # 没声明 → 保守当可写


def test_call_tool_is_error_becomes_failure(client):
    """server 回 isError → ToolResult.fail（不静默当成功）。"""
    result = client.call_tool("boom", {})
    assert result.success is False
    assert result.error == "炸了"


def test_unknown_tool_raises_mcp_error(client):
    """未知工具 → JSON-RPC error → MCPError（带 code/message）。"""
    with pytest.raises(MCPError, match="未知工具"):
        client.call_tool("nope", {})


def test_adapter_skips_local_validation_and_calls_remote(client, tmp_path):
    """适配器不做本地 pydantic 校验，参数原样透传（远端才是权威校验方）。"""
    remote = next(t for t in client.list_tools() if t["name"] == "write_note")
    adapter = MCPToolAdapter(client, remote)
    ctx = ToolContext(workspace_root=tmp_path)

    # schema 用远端 inputSchema，而不是从 pydantic 模型生成
    params = adapter.schema()["function"]["parameters"]
    assert params["required"] == ["text"]
    assert adapter.schema()["function"]["name"] == "write_note"

    # 多传一个 schema 里没声明的字段也不会被本地拦住（远端自己决定怎么处理）
    result = adapter.run({"text": "hello mcp", "extra": 1}, ctx)
    assert result.success is True
    assert (tmp_path / "note.txt").read_text(encoding="utf-8") == "hello mcp"
    assert result.duration_ms >= 0


def test_load_servers_registers_tools_and_renames_conflicts(tmp_path):
    """配置加载：工具进 registry；与内置工具重名时加别名前缀（不静默覆盖）。"""
    config = tmp_path / "mcp.json"
    config.write_text(
        json.dumps({"servers": {"fake": {"command": server_command("--note-path", str(tmp_path / "n.txt"))}}}),
        encoding="utf-8",
    )
    registry = ToolRegistry([ReadTool()])  # 内置 read 先占位
    clients, registered = load_mcp_servers(config, registry, workspace_root=tmp_path)
    try:
        assert set(registered) == {"echo", "write_note", "boom"}
        assert "echo" in registry and registry.get("echo").is_read_only() is True
        assert "read" in registry and isinstance(registry.get("read"), ReadTool)
    finally:
        for c in clients:
            c.close()


def test_load_servers_skips_broken_server_without_raising(tmp_path, capsys):
    """单个 server 挂了不影响其它 server，也不炸主流程（MCP 是增强项）。"""
    config = tmp_path / "mcp.json"
    config.write_text(
        json.dumps({"servers": {
            "broken": {"command": server_command("--crash-immediately")},
            "good": {"command": server_command("--note-path", str(tmp_path / "n.txt"))},
            "nocommand": {},
        }}),
        encoding="utf-8",
    )
    registry = ToolRegistry()
    clients, registered = load_mcp_servers(config, registry, workspace_root=tmp_path)
    try:
        assert "echo" in registered          # good 正常注册
        assert "echo" in registry
    finally:
        for c in clients:
            c.close()
    assert "已跳过" in capsys.readouterr().err


def test_mcp_tool_runs_through_query_engine(tmp_path):
    """端到端：MCP 工具经**真实 QueryEngine 循环**被调用，结果回喂给模型。

    这一步验证的是集成而非协议：注册表 → loop 门禁链 → 适配器 → 远端 server
    → ToolResult 回喂。MCP 工具和内置工具在循环眼里没有区别。
    """
    from agent.llm import LLMResult, MockLLM, ToolCall
    from agent.loop import QueryEngine

    config = tmp_path / "mcp.json"
    config.write_text(
        json.dumps({"servers": {"fake": {"command": server_command("--note-path", str(tmp_path / "n.txt"))}}}),
        encoding="utf-8",
    )
    registry = ToolRegistry.default(tmp_path)
    clients, _ = load_mcp_servers(config, registry, workspace_root=tmp_path)
    try:
        llm = MockLLM.script(
            LLMResult(content=None, tool_calls=[
                ToolCall(id="c1", name="echo", arguments={"text": "来自循环"})
            ]),
            LLMResult(content="远端已回显"),
        )
        engine = QueryEngine(llm, registry, workspace_root=tmp_path)
        result = engine.run("调一下 echo")
        assert result.terminated_reason == "completed"
        assert result.final_text == "远端已回显"
        tool_events = [e for e in result.events if e["type"] == "tool_call"]
        assert tool_events[0]["name"] == "echo" and tool_events[0]["success"] is True
    finally:
        for c in clients:
            c.close()


def test_missing_config_raises(tmp_path):
    """配置缺失/非法 → 明确报错（不是静默不加载）。"""
    with pytest.raises(FileNotFoundError):
        load_mcp_servers(tmp_path / "nope.json", ToolRegistry())
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(MCPError, match="不是合法 JSON"):
        load_mcp_servers(bad, ToolRegistry())


def test_call_after_server_crash_raises(tmp_path):
    """server 中途崩溃 → 抛 MCPError 并带上 stderr 线索（不无限等）。"""
    c = MCPClient(server_command("--note-path", str(tmp_path / "n.txt"), "--crash-on-call"))
    c.start()
    try:
        with pytest.raises(MCPError) as exc:
            c.call_tool("echo", {"text": "hi"})
        assert "已退出" in str(exc.value) or "超时" in str(exc.value)
    finally:
        c.close()


def test_request_timeout_reports_method(tmp_path):
    """server 不回响应 → 超时错误指明是哪个方法（可诊断）。"""
    slow = MCPClient(
        server_command("--note-path", str(tmp_path / "n.txt"), "--silent-on", "tools/slow"),
        timeout=0.3,
    )
    slow.start()
    try:
        with pytest.raises(MCPError, match="超时.*tools/slow"):
            slow._request("tools/slow")
    finally:
        slow.close()
