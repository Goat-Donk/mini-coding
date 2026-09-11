"""M6-3 / M9-4 tests: MCP 客户端（真子进程 + 真 socket + 真 JSON-RPC，不是 mock）。

覆盖三层：
1. **协议层**（M6-3）：握手与 serverInfo / tools/list / tools/call（成功·isError·
   未知工具）/ 只读注解透传 / 参数不做本地 pydantic 校验 / 与内置工具重名加前缀 /
   server 崩溃与启动失败不断主流程。
2. **传输层**（M9-4，HTTP）：JSON 与 SSE 两种响应 / SSE 流里夹通知 / session id
   与协议版本头 / 会话过期后重新 initialize / 重定向如实报错 / 缺 Accept 头被拒。
3. **能力面**（M9-4，resources + prompts）：读文本与二进制 / 模板参数 / 描述里
   列出可用项 / 能力缺失或列表为空则不注册 / 与 allow 白名单的接线。
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from agent.mcp import (
    DEFAULT_TIMEOUT,
    MAX_LISTED_ITEMS,
    HttpTransport,
    MCPClient,
    MCPError,
    MCPPromptTool,
    MCPResourceTool,
    MCPSessionExpired,
    MCPToolAdapter,
    StdioTransport,
    _flatten_content,
    _parse_sse,
    _render_prompts,
    _render_resources,
    build_transport,
    load_mcp_servers,
)
from agent.tools.base import ToolContext, ToolRegistry
from agent.tools.files import ReadTool

SERVER = Path(__file__).parent / "fake_mcp_server.py"
HTTP_SERVER = Path(__file__).parent / "fake_mcp_http_server.py"


def server_command(*extra: str) -> list[str]:
    return [sys.executable, str(SERVER), *extra]


def stdio_client(tmp_path, *extra: str, name: str = "fake", timeout: float = 20.0) -> MCPClient:
    return MCPClient(
        StdioTransport(server_command("--note-path", str(tmp_path / "note.txt"), *extra),
                       timeout=timeout),
        name=name,
    )


@pytest.fixture
def client(tmp_path):
    c = stdio_client(tmp_path)
    c.start()
    yield c
    c.close()


# ---------------------------------------------------------------- HTTP 夹具


@pytest.fixture
def http_server(tmp_path):
    """起一个**真的**假 HTTP MCP server（真进程、真 socket），返回端点 URL 工厂。

    端口由内核分配（绑 0）后写进文件，测试之间不会抢端口。
    """
    procs: list[subprocess.Popen] = []

    def start(*extra: str) -> str:
        index = len(procs)
        port_file = tmp_path / f"port{index}.txt"
        proc = subprocess.Popen(
            [sys.executable, str(HTTP_SERVER),
             "--port-file", str(port_file),
             "--note-path", str(tmp_path / "note.txt"), *extra],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        procs.append(proc)
        for _ in range(200):                       # 最多等 10s
            if port_file.exists() and port_file.read_text(encoding="utf-8").strip():
                break
            time.sleep(0.05)
        else:
            raise RuntimeError("假 HTTP MCP server 没起来")
        return f"http://127.0.0.1:{port_file.read_text(encoding='utf-8').strip()}/mcp"

    yield start
    for proc in procs:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def http_client(url: str, *, name: str = "remote", timeout: float = 10.0) -> MCPClient:
    return MCPClient(HttpTransport(url, timeout=timeout), name=name)


# ---------------------------------------------------------------- 协议层（stdio）


def test_handshake_exposes_server_info(client):
    """initialize 往返：拿到 protocolVersion 与 serverInfo。"""
    assert client.protocol_version == "2025-06-18"
    assert client.server_info["name"] == "fake-mcp"


def test_handshake_ends_with_the_initialized_notification(tmp_path):
    """握手最后必须发 `notifications/initialized` —— spec 里这是 **MUST**。

    服务端没收到它就拒绝后续每一次请求。对着一个"什么通知都收"的假 server
    测是永远看不出来的：漏发通知时，我们这一侧的日志一切正常，只有真实
    server 会开始报错。所以假 server 特意加了这个开关把这条 MUST 钉住。
    """
    c = stdio_client(tmp_path, "--require-initialized")
    c.start()
    try:
        assert c.list_tools(), "没发 initialized 通知的话，这次调用会被服务端拒掉"
    finally:
        c.close()


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


# ---------------------------------------------------------------- 传输层（HTTP）


def test_http_transport_handshakes_and_calls_tools(http_server):
    """JSON 响应的 HTTP 传输：与 stdio **同样的调用序列**，同样能跑通。"""
    c = http_client(http_server())
    c.start()
    try:
        assert c.server_info["name"] == "fake-mcp"
        assert {t["name"] for t in c.list_tools()} == {"echo", "write_note", "boom"}
        assert c.call_tool("echo", {"text": "走 HTTP"}).output == "echo: 走 HTTP"
        assert c.transport.session_id, "initialize 回了 Mcp-Session-Id，应当被记住"
    finally:
        c.close()


def test_http_transport_reads_an_sse_response(http_server):
    """`text/event-stream` 响应（而不是 application/json）也要认。

    spec 允许 server 二选一，所以**两种都必须是可用的**——只测 JSON 的话，
    换成 SSE 服务端就是全面不可用，而单测会全绿。
    """
    c = http_client(http_server("--mode", "sse"))
    c.start()
    try:
        assert c.call_tool("echo", {"text": "走 SSE"}).output == "echo: 走 SSE"
        assert c.read_resource("doc://readme").output == "# 假 server 的说明"
    finally:
        c.close()


def test_http_transport_skips_notifications_before_the_response(http_server):
    """SSE 流里先来一条通知再来本次响应 —— 按 id 关联必须跳过它。

    顺序错了（拿第一条就用）会拿到那条通知，表现为 KeyError 或者一个空的
    result；而 server 端完全合规，错全在客户端。
    """
    c = http_client(http_server("--mode", "sse", "--noise"))
    c.start()
    try:
        assert c.call_tool("echo", {"text": "有噪音"}).output == "echo: 有噪音"
    finally:
        c.close()


def test_http_transport_sends_session_and_protocol_headers(http_server):
    """后续请求必须带 `Mcp-Session-Id` 与 `MCP-Protocol-Version`（都被服务端强制）。

    服务端只有在**两个头都对**时才正常应答（缺 session → 404，缺版本头 → 400），
    所以"能跑通"本身就是这两个头真的发出去了的证据。
    """
    c = http_client(http_server("--require-session", "--require-protocol-header",
                                "--require-accept"))
    c.start()
    try:
        assert c.list_tools(), "缺任一必需头都会让这次调用失败"
    finally:
        c.close()


def test_http_transport_reinitializes_when_the_session_expires(http_server):
    """会话过期（404）→ 重新 initialize → 重试，**用户不该看见这个错误**。

    服务端每 2 个带 session 的请求作废一次会话（握手那条通知算一次），所以：
    第 1 次调用还在旧会话上，第 2 次撞上过期、走自愈。
    断言里**两头都有**——只断言"调用成功"的话，一个把 404 吞掉不重试的实现
    也能过；只看换没换会话的话，一个**每次调用都重新握手**的实现也能过
    （那会让每个请求多一次往返）。两条一起才钉住"只在需要时重建"。
    """
    c = http_client(http_server("--require-session", "--expire-after", "2"))
    c.start()
    try:
        first_session = c.transport.session_id
        assert c.call_tool("echo", {"text": "第一次"}).output == "echo: 第一次"
        assert c.transport.session_id == first_session, "这次还没过期，不该白白重建会话"
        assert c.call_tool("echo", {"text": "第二次"}).output == "echo: 第二次"
        assert c.transport.session_id != first_session, "过期后应当重新 initialize 拿新会话"
    finally:
        c.close()


def test_http_transport_reports_a_redirect_instead_of_following_it(http_server):
    """3xx 不跟随，如实报出来。

    `urllib` 默认会把重定向上的 POST **改写成 GET**：一次 tools/call 于是变成
    一次静默的读请求（假 server 的 GET 恰好返回 405，但真 server 未必）。
    这里断言错误里带目标地址——那是用户唯一能照着改配置的信息。
    """
    url = http_server("--redirect-to", "http://127.0.0.1:9/mcp")
    c = http_client(url)
    with pytest.raises(MCPError, match="重定向到 http://127.0.0.1:9/mcp"):
        c.start()


def test_http_transport_reports_a_dead_endpoint(tmp_path):
    """连不上就报端点地址（而不是一句空泛的 URLError）。"""
    c = http_client("http://127.0.0.1:9/mcp", timeout=2.0)
    with pytest.raises(MCPError, match="连不上 MCP 端点"):
        c.start()


def test_both_transports_agree_on_the_same_sequence(tmp_path, http_server):
    """**同一条操作序列走两种传输，结果必须一致。**

    这是"协议层复用、只换传输"这句话的唯一证明方式：只要有一处逻辑被写进
    传输里（比如超时的整形、id 的关联），两种传输就会出现分歧，而那种分歧
    在只测单一传输的套件里**看不见**。
    """
    over_stdio = stdio_client(tmp_path)
    over_http = http_client(http_server())
    over_stdio.start()
    over_http.start()
    try:
        assert over_stdio.server_info == over_http.server_info
        assert over_stdio.protocol_version == over_http.protocol_version
        assert over_stdio.capabilities == over_http.capabilities
        assert over_stdio.list_tools() == over_http.list_tools()
        assert over_stdio.list_resources() == over_http.list_resources()
        assert over_stdio.list_prompts() == over_http.list_prompts()
        assert (over_stdio.call_tool("echo", {"text": "x"}).output
                == over_http.call_tool("echo", {"text": "x"}).output)
        assert (over_stdio.read_resource("doc://readme").output
                == over_http.read_resource("doc://readme").output)
        # 错误整形也要一致：同一个未知工具，两条传输给出同一句话
        errors = []
        for c in (over_stdio, over_http):
            with pytest.raises(MCPError) as exc:
                c.call_tool("nope", {})
            errors.append(str(exc.value))
        assert errors[0] == errors[1]
    finally:
        over_stdio.close()
        over_http.close()


def test_build_transport_picks_by_config_and_rejects_both(tmp_path):
    """配置 → 传输：`command` 走 stdio、`url` 走 HTTP，两个都给则是配置错误。"""
    assert isinstance(build_transport({"command": ["python", "-V"]}), StdioTransport)
    assert isinstance(build_transport({"url": "http://x/mcp"}), HttpTransport)
    with pytest.raises(MCPError, match="只能给一个"):
        build_transport({"command": ["python"], "url": "http://x/mcp"})
    with pytest.raises(MCPError, match="缺少 command"):
        build_transport({})


def test_build_transport_carries_timeout_and_workspace(tmp_path):
    """`timeout` 与 `workspace_root` 都要落到传输上。

    超时只在传输层存一份（两处各存一份迟早对不上，表现成"有时报超时有时不报"）；
    cwd 决定了 stdio server 眼里什么是相对路径 —— 配 `["./server.py"]` 这种命令时，
    不传 cwd 就等于让 server 在进程的当前目录里找，而那是**用户启动 CLI 的目录**，
    不是工作区。
    """
    assert build_transport({"url": "http://x/mcp", "timeout": 3}).timeout == 3
    assert build_transport({"url": "http://x/mcp"}).timeout == DEFAULT_TIMEOUT
    stdio = build_transport({"command": ["python", "-V"], "timeout": 3}, workspace_root=tmp_path)
    assert stdio.timeout == 3 and stdio.cwd == tmp_path


def test_http_headers_cannot_override_protocol_headers():
    """用户配置的 headers 不能覆盖 `Accept` / `Content-Type` / 协议版本头。

    这三条是协议要求，配错会让握手直接失败；让它们"可配置"等于提供一个
    必然把自己配坏的口子。认证头之类照常补充。
    """
    t = HttpTransport("http://x/mcp", headers={
        "Accept": "text/plain",           # 想覆盖？不生效
        "Authorization": "Bearer t",      # 正常补充
    })
    assert t._extra_headers == {"Authorization": "Bearer t"}


# ---------------------------------------------------------------- 纯解析函数
#
# 这几条不走 socket：它们钉的是"字节怎么变成消息"这一步的形状。放在这里而不是
# 只靠假 server，是因为假 server 只会产出**规范**的 SSE —— 边界（末尾没有空行、
# 多行 data、心跳注释）在真实 server 上天天出现，却一条都造不出来。


def test_parse_sse_handles_the_edge_cases():
    """SSE → 消息：空行才是分发点、多行 data 要拼、注释与非 data 字段要跳过、
    **流末尾没有空行时也要收下**（server 直接关流是常见写法，漏收等于让请求白等超时）。
    """
    text = (
        ": 心跳注释\n"                       # 以 : 开头 → 整行跳过
        "event: message\n"                   # event / id 不是我们要的字段
        "id: 7\n"
        'data: {"a": 1}\n'
        "\n"                                 # 空行 = 分发点
        'data: {"b":\n'                      # 一条消息的 data 可以跨多行
        "data: 2}\n"
        "\n"
        "data: 这不是 JSON\n"                 # 坏数据丢掉，不炸整条流
        "\n"
        'data: {"c": 3}'                     # 末尾**没有**空行
    )
    assert _parse_sse(text) == [{"a": 1}, {"b": 2}, {"c": 3}]


def test_flatten_content_covers_every_block_type():
    """非文本 content block 也要给出**可读占位**，不能静默丢。

    本地是纯文本模型、看不了图片，但"这里有一张 image/png"和"什么都没有"是
    两件完全不同的事：前者模型会去换条路，后者它只会以为这一处是空的。
    """
    assert _flatten_content([{"type": "text", "text": "正文"}]) == "正文"
    assert _flatten_content("直接是字符串") == "直接是字符串"
    # PromptMessage.content 是**单个** block（spec 就是这么定的），不是数组
    assert _flatten_content({"type": "text", "text": "单块"}) == "单块"
    assert "image/png" in _flatten_content([{"type": "image", "mimeType": "image/png"}])
    assert "doc://x" in _flatten_content(
        [{"type": "resource", "resource": {"uri": "doc://x", "text": "内嵌正文"}}])
    assert "已省略" in _flatten_content([{"type": "audio"}])
    assert _flatten_content(None) == ""       # 空值不该炸


def test_list_drops_malformed_items_instead_of_failing(client, monkeypatch):
    """一条没有 name 的记录不该让**整台 server** 不可用（同"一个坏 meta 不该藏起
    另外九个会话"）。server 版本五花八门，多回一条脏记录是常事。
    """
    monkeypatch.setattr(client, "_request", lambda *a, **k: {"tools": [
        {"name": "ok"}, {"description": "没有 name"}, "一个字符串", {"name": ""},
    ]})
    assert [t["name"] for t in client.list_tools()] == ["ok"]


# ---------------------------------------------------------------- 资源与提示词


def test_list_and_read_resources(client):
    """resources/list 与 resources/read（文本资源）。"""
    uris = {r["uri"] for r in client.list_resources()}
    assert uris == {"note://current", "doc://readme", "bin://blob"}
    assert client.read_resource("doc://readme").output == "# 假 server 的说明"


def test_read_resource_marks_binary_instead_of_decoding_it(client, tmp_path):
    """二进制资源（`blob`，base64）如实说明，**不解码成乱码喂给模型**。

    与 web 工具"认不出的编码如实报错"同一条：给一坨看着像内容的乱码，
    比给一句"这里有个二进制、多大"糟糕得多。
    """
    (tmp_path / "note.txt").write_text("笔记内容", encoding="utf-8")
    result = client.read_resource("bin://blob")
    assert result.success is True
    assert "二进制内容" in result.output and "4 字节" in result.output
    assert "\x00" not in result.output

    assert client.read_resource("note://current").output == "笔记内容"


def test_read_resource_unknown_uri_raises(client):
    with pytest.raises(MCPError, match="资源不存在"):
        client.read_resource("nope://x")


def test_list_and_get_prompts(client):
    """prompts/list 与 prompts/get：模板参数透传，回的是渲染后的消息。"""
    names = {p["name"] for p in client.list_prompts()}
    assert names == {"summarize", "noargs"}
    result = client.get_prompt("summarize", {"text": "很长的一段", "style": "三句话"})
    assert result.success is True
    assert "[user] 用三句话总结：很长的一段" in result.output


def test_get_prompt_tool_checks_parallel_arrays(client, tmp_path):
    """工具面的两个平行数组必须一一对应，否则**回喂原因**让模型自修复。"""
    tool = MCPPromptTool(client, client.list_prompts())
    ctx = ToolContext(workspace_root=tmp_path)

    bad = tool.run({"name": "summarize", "keys": ["text", "style"], "values": ["只有一条"]}, ctx)
    assert bad.success is False
    assert bad.error is not None and "一一对应" in bad.error

    ok = tool.run({"name": "summarize", "keys": ["text"], "values": ["短文本"]}, ctx)
    assert ok.success is True and "短文本" in ok.output


def test_resource_tool_description_lists_available_uris(client, tmp_path):
    """**工具描述里要列出可用资源** —— 这一条钉的是接线，不是格式。

    不列的话模型不知道有哪些 uri 可读，只能瞎猜一个试试；那正是 M8
    `update_plan` 那个 elicitation gap 的形状（机制全在，没有任何东西把模型
    引向它）。描述恒在上下文里，列出来是零额外往返。
    """
    (tmp_path / "note.txt").write_text("机密正文XYZ", encoding="utf-8")
    tool = MCPResourceTool(client, client.list_resources())
    description = tool.schema()["function"]["description"]
    assert "note://current" in description and "doc://readme" in description
    assert "text/plain" in description            # 顺带给了 mimeType
    # 只列元数据、**不读正文**：描述每轮都发，把正文塞进去等于每轮都在烧 token
    assert "机密正文XYZ" not in description


def test_prompt_tool_description_lists_argument_names(client):
    """提示词描述要列出**参数名与必填性**，否则模型只能靠试错猜参数。"""
    tool = MCPPromptTool(client, client.list_prompts())
    description = tool.schema()["function"]["description"]
    assert "summarize" in description
    assert "text(必填)" in description and "style(可选)" in description


def test_long_resource_list_is_capped():
    """资源列表封顶：一个挂了 500 处资源的 server 不该把 schema 撑爆。"""
    many = [{"uri": f"r://{i}", "name": f"资源{i}"} for i in range(MAX_LISTED_ITEMS + 5)]
    rendered = _render_resources("big", many)
    assert "r://0" in rendered
    assert f"r://{MAX_LISTED_ITEMS}" not in rendered      # 超出部分不列
    assert f"另有 {5} 处未列出" in rendered                # 但要如实说还有多少

    prompts = [{"name": f"p{i}"} for i in range(MAX_LISTED_ITEMS + 3)]
    assert "另有 3 则未列出" in _render_prompts("big", prompts)


def test_capability_tools_are_read_only_and_external(client):
    """两个能力工具都是**只读 + 外部**：进并发只读批，且权限默认不放行。"""
    for tool in (MCPResourceTool(client, client.list_resources()),
                 MCPPromptTool(client, client.list_prompts())):
        assert tool.is_read_only() is True
        assert tool.is_external() is True


# ---------------------------------------------------------------- 配置加载


def test_load_servers_registers_tools_and_renames_conflicts(tmp_path):
    """配置加载：工具进 registry；与内置工具重名时加别名前缀（不静默覆盖）。"""
    config = tmp_path / "mcp.json"
    config.write_text(
        json.dumps({"servers": {"fake": {"command": server_command("--note-path", str(tmp_path / "n.txt"))}}}),
        encoding="utf-8",
    )
    registry = ToolRegistry([ReadTool()])  # 内置 read 先占位
    clients, registered, allowed = load_mcp_servers(config, registry, workspace_root=tmp_path)
    try:
        assert set(registered) == {"echo", "write_note", "boom", "read_resource", "get_prompt"}
        assert "echo" in registry and registry.get("echo").is_read_only() is True
        assert "read" in registry and isinstance(registry.get("read"), ReadTool)
        # 没配 allow → 一个都不授权（第三方工具默认不放行）
        assert allowed == []
    finally:
        for c in clients:
            c.close()


def test_load_servers_registers_capability_tools_over_http(tmp_path, http_server):
    """HTTP server 的能力工具同样要注册（传输换了，接线不能漏）。"""
    config = tmp_path / "mcp.json"
    config.write_text(
        json.dumps({"servers": {"remote": {
            "url": http_server(),
            "allow": ["read_resource"],
        }}}),
        encoding="utf-8",
    )
    registry = ToolRegistry()
    clients, registered, allowed = load_mcp_servers(config, registry, workspace_root=tmp_path)
    try:
        assert "read_resource" in registered and "get_prompt" in registered
        assert allowed == ["read_resource"], "allow 白名单对能力工具同样生效"
    finally:
        for c in clients:
            c.close()


def test_capability_tools_are_skipped_when_capability_is_absent(tmp_path, capsys):
    """server 没声明能力 → 不注册那个工具面，而且**根本不去问**。

    "不去问"这一点只能从 stderr 看出来：能力没声明时我们直接跳过，声明了却
    列不出来时才打那句 `…/list 失败`。少了能力判断的话，一台本来就不支持
    resources 的 server 每启动一次就会多一行警告 —— 而每台 server 都报一行
    噪声，等于训练用户无视警告，真正该看的那行也一起被无视了。
    """
    config = tmp_path / "mcp.json"
    config.write_text(
        json.dumps({"servers": {"bare": {"command": server_command(
            "--no-resources", "--no-prompts", "--note-path", str(tmp_path / "n.txt"))}}}),
        encoding="utf-8",
    )
    registry = ToolRegistry()
    clients, registered, _ = load_mcp_servers(config, registry, workspace_root=tmp_path)
    try:
        assert set(registered) == {"echo", "write_note", "boom"}
        assert "read_resource" not in registry and "get_prompt" not in registry
    finally:
        for c in clients:
            c.close()
    assert "list 失败" not in capsys.readouterr().err


def test_capability_tools_are_skipped_when_the_list_is_empty(tmp_path):
    """声明了能力但列表为空 → 也不注册。

    注册了的话模型会拿到一个**永远调不通**的工具：白占 schema token，
    而且每次调用都失败一次。判能力还不够，要连列表一起判。
    """
    config = tmp_path / "mcp.json"
    config.write_text(
        json.dumps({"servers": {"empty": {"command": server_command(
            "--empty-resources", "--empty-prompts", "--note-path", str(tmp_path / "n.txt"))}}}),
        encoding="utf-8",
    )
    registry = ToolRegistry()
    clients, registered, _ = load_mcp_servers(config, registry, workspace_root=tmp_path)
    try:
        assert set(registered) == {"echo", "write_note", "boom"}
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
    clients, registered, _ = load_mcp_servers(config, registry, workspace_root=tmp_path)
    try:
        assert "echo" in registered          # good 正常注册
        assert "echo" in registry
    finally:
        for c in clients:
            c.close()
    assert "已跳过" in capsys.readouterr().err


def test_load_servers_skips_a_server_with_both_command_and_url(tmp_path, capsys):
    """`command` 与 `url` 同时给 → 跳过这台 server 并说清原因，不静默选一个。"""
    config = tmp_path / "mcp.json"
    config.write_text(
        json.dumps({"servers": {
            "confused": {"command": server_command(), "url": "http://127.0.0.1:9/mcp"},
            "good": {"command": server_command("--note-path", str(tmp_path / "n.txt"))},
        }}),
        encoding="utf-8",
    )
    registry = ToolRegistry()
    clients, registered, _ = load_mcp_servers(config, registry, workspace_root=tmp_path)
    try:
        assert "echo" in registered                     # 另一台不受影响
    finally:
        for c in clients:
            c.close()
    err = capsys.readouterr().err
    assert "confused" in err and "只能给一个" in err


def test_load_servers_only_allows_configured_tools(tmp_path):
    """`allow` 白名单：只有列出的工具免确认，其余照旧默认不放行。

    这里钉住的是**配置值的语义**（哪些名字算被授权），不是权限引擎的判定——
    后者由 test_permissions.py 的 external 用例覆盖。
    """
    config = tmp_path / "mcp.json"
    config.write_text(
        json.dumps({"servers": {"fake": {
            "command": server_command("--note-path", str(tmp_path / "n.txt")),
            "allow": ["echo"],
        }}}),
        encoding="utf-8",
    )
    registry = ToolRegistry()
    clients, registered, allowed = load_mcp_servers(config, registry, workspace_root=tmp_path)
    try:
        assert set(registered) == {"echo", "write_note", "boom", "read_resource", "get_prompt"}
        assert allowed == ["echo"]           # 其余未被授权
    finally:
        for c in clients:
            c.close()


def test_load_servers_allow_supports_glob(tmp_path):
    """`allow` 支持通配（写 `"*"` 等于整台 server 全放行，是显式选择而非默认）。"""
    config = tmp_path / "mcp.json"
    config.write_text(
        json.dumps({"servers": {"fake": {
            "command": server_command("--note-path", str(tmp_path / "n.txt")),
            "allow": ["e*"],             # 只命中 echo
        }}}),
        encoding="utf-8",
    )
    registry = ToolRegistry()
    clients, registered, allowed = load_mcp_servers(config, registry, workspace_root=tmp_path)
    try:
        assert set(registered) == {"echo", "write_note", "boom", "read_resource", "get_prompt"}
        assert allowed == ["echo"]
    finally:
        for c in clients:
            c.close()


def test_allow_matches_final_name_when_prefixed():
    """重名加前缀后，配**注册名**写法（`fake__echo`）也能命中。

    远端名与注册名只在 load 那一处同时可见，所以匹配在那里做——否则用户得去
    猜前缀，配置就成了实现细节的泄漏。这里直接测匹配函数，因为 fake server
    没有与内置工具同名的工具，造不出真实的重名场景。
    """
    from agent.mcp import _is_allowed

    # 未加前缀：远端名直配
    assert _is_allowed("echo", "echo", ["echo"]) is True
    # 加了前缀：用注册名配
    assert _is_allowed("echo", "fake__echo", ["fake__echo"]) is True
    # 加了前缀但用户仍按远端名配 —— 也认（避免用户被迫理解前缀规则）
    assert _is_allowed("echo", "fake__echo", ["echo"]) is True
    # 都不匹配 → 不授权
    assert _is_allowed("echo", "fake__echo", ["write_note"]) is False


def test_capability_tool_prefixes_on_a_name_clash(tmp_path):
    """能力工具与内置工具重名时，**同样**加前缀（前缀逻辑只有一处）。"""
    registry = ToolRegistry([ReadTool()])          # 内置 read 先占位
    clash = MCPResourceTool(
        MCPClient(StdioTransport(server_command()), name="fake"),
        [{"uri": "x://1"}],
    )
    clash.name = "read"                            # 冒充内置 read
    from agent.mcp import _register

    registered: list[str] = []
    _register(registry, clash, "fake", [], registered, [])
    assert registered == ["fake__read"]
    assert isinstance(registry.get("read"), ReadTool), "内置工具不能被顶掉"


# ---------------------------------------------------------------- 端到端


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
    clients, _, _ = load_mcp_servers(config, registry, workspace_root=tmp_path)
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


def test_resource_tool_runs_through_query_engine_over_http(tmp_path, http_server):
    """端到端（HTTP 传输）：模型调 `read_resource`，远端资源正文**真的到了模型眼前**。

    需要显式授权（`allow`）——第三方工具默认不放行，这条路径上权限引擎照样生效。

    断言写在**第二次 LLM 调用收到的 messages** 上，而不是 `tool_call` 事件里：
    事件按设计不存工具输出（只记 name/success/duration），输出只存在于那条
    `tool` 消息里。去事件里找 `output` 会直接 KeyError —— 而真正要证明的是
    "正文进了上下文"，那正是 messages。
    """
    from agent.llm import LLMResult, MockLLM, ToolCall
    from agent.loop import QueryEngine

    config = tmp_path / "mcp.json"
    config.write_text(
        json.dumps({"servers": {"remote": {"url": http_server(), "allow": ["read_resource"]}}}),
        encoding="utf-8",
    )
    registry = ToolRegistry.default(tmp_path)
    clients, _, allowed = load_mcp_servers(config, registry, workspace_root=tmp_path)

    def sees_the_resource(messages, tools):
        tool_texts = [m.get("content") or "" for m in messages if m.get("role") == "tool"]
        assert any("# 假 server 的说明" in t for t in tool_texts), \
            f"资源正文没有进上下文: {tool_texts}"
        return LLMResult(content="读到了说明")

    try:
        assert allowed == ["read_resource"]
        llm = MockLLM.script(
            LLMResult(content=None, tool_calls=[
                ToolCall(id="c1", name="read_resource", arguments={"uri": "doc://readme"})
            ]),
            sees_the_resource,
        )
        engine = QueryEngine(llm, registry, workspace_root=tmp_path)
        result = engine.run("读一下说明")
        assert result.terminated_reason == "completed"
        assert result.final_text == "读到了说明"
        tool_events = [e for e in result.events if e["type"] == "tool_call"]
        assert tool_events[0]["name"] == "read_resource"
        assert tool_events[0]["success"] is True
    finally:
        for c in clients:
            c.close()


# ---------------------------------------------------------------- 故障路径


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
    c = stdio_client(tmp_path, "--crash-on-call")
    c.start()
    try:
        with pytest.raises(MCPError) as exc:
            c.call_tool("echo", {"text": "hi"})
        assert "已退出" in str(exc.value) or "超时" in str(exc.value)
    finally:
        c.close()


def test_request_timeout_reports_method(tmp_path):
    """server 不回响应 → 超时错误指明是哪个方法（可诊断）。"""
    slow = stdio_client(tmp_path, timeout=0.3)
    slow.start()
    try:
        with pytest.raises(MCPError, match="超时.*tools/slow"):
            slow._request("tools/slow")
    finally:
        slow.close()


def test_http_timeout_names_the_endpoint(http_server):
    """HTTP 超时要报出**端点地址**（stdio 那边给的是 server stderr 末尾，两者不同）。

    `tools/slow` 是假 server 里故意不回响应的方法：HTTP 壳回 202 无正文，
    于是客户端等不到那条消息 —— 与 stdio 壳"干脆不写"是同一个场景。
    """
    c = http_client(http_server(), timeout=0.5)
    c.start()
    try:
        assert c.timeout == 0.5, "超时值只有传输层一个真相源，别在协议层另存一份"
        with pytest.raises(MCPError, match="超时.*127.0.0.1"):
            c._request("tools/slow")
    finally:
        c.close()


def test_session_expiry_is_not_swallowed_as_a_plain_error(http_server):
    """会话过期必须是 `MCPSessionExpired`（可自愈），不能退化成普通 MCPError。

    假 server 的 404 正文里**也放了一条 JSON-RPC error** —— 错误整形若先匹配
    正文，这条就会变成"普通调用失败"，自愈那条路永远走不到，
    而错误信息看上去完全合理。
    """
    c = http_client(http_server("--require-session"))
    c.start()
    try:
        c.transport.session_id = "stale-session"      # 冒充一个服务端不认的会话
        with pytest.raises(MCPSessionExpired):
            c.transport.send({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    finally:
        c.close()
