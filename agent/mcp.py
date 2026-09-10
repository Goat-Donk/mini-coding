"""MCP 客户端（M6-3，有余力项）：让工具层接入标准 MCP server。

**为什么手写而不装 `mcp` 官方 SDK**：
1. 官方 SDK 是 async（anyio）的，而我们的 QueryEngine 是同步循环——为了一个工具
   把整条循环改成 async 得不偿失；手写同步版本的阻抗最小。
2. MCP 的 stdio 传输就是「JSON-RPC 2.0 按行分隔」，协议面很窄（本文件 3 个方法），
   手写一遍比封一层 SDK 更透明，也更能讲清楚协议本身。
3. 不引入新依赖。

**安全边界（重要）**：MCP 工具来自第三方 server，**不受我们 workspace 沙箱约束**——
远端 server 想干什么都行。所以：
- MCP 工具**必须显式配置才注册**（不进 ToolRegistry.default）；
- 只读性**只信 server 自己声明的 `annotations.readOnlyHint`**，默认当可写（串行执行）；
- 但它们仍然走 `_gate_and_run` 的门禁链 → **hooks 和权限引擎照样生效**，
  这正是把权限/钩子做成独立层的回报：接新工具来源不用改循环。
"""
from __future__ import annotations

import json
import queue
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path

from pydantic import BaseModel

from agent.tools.base import Tool, ToolContext, ToolRegistry, ToolResult

# 客户端声明的协议版本（server 可回它自己的；按 spec 以 server 返回为准）
PROTOCOL_VERSION = "2025-06-18"
DEFAULT_TIMEOUT = 20.0
_CLIENT_INFO = {"name": "codeagent", "version": "0.1.0"}


class MCPError(RuntimeError):
    """MCP 协议/传输层错误。"""


class MCPClient:
    """最小 MCP stdio 客户端：启动 server 子进程 → initialize → tools/list / tools/call。

    stdout 由后台线程抽到队列（管道阻塞读没法设超时，Windows 上 select 也不支持
    pipe），请求按 id 关联响应；stderr 单独抽到环形缓冲，出错时能给出 server 的
    真实报错，而不是干巴巴一句"超时"。
    """

    def __init__(
        self,
        command: list[str],
        *,
        name: str = "mcp",
        timeout: float = DEFAULT_TIMEOUT,
        cwd: Path | None = None,
    ) -> None:
        if not command:
            raise ValueError("MCP server command 不能为空")
        self.command = list(command)
        self.name = name
        self.timeout = timeout
        self.cwd = Path(cwd) if cwd else None
        self.server_info: dict = {}
        self.protocol_version: str | None = None

        self._proc: subprocess.Popen | None = None
        self._inbox: queue.Queue = queue.Queue()
        self._stderr_tail: deque[str] = deque(maxlen=40)
        self._id = 0
        self._threads: list[threading.Thread] = []

    # ---------- 生命周期 ----------

    def start(self) -> "MCPClient":
        if self._proc is not None:
            return self
        try:
            self._proc = subprocess.Popen(
                self.command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                bufsize=1,          # 行缓冲：请求即时发出，不等缓冲满
                cwd=str(self.cwd) if self.cwd else None,
            )
        except FileNotFoundError as exc:
            raise MCPError(f"无法启动 MCP server {self.command!r}: {exc}") from exc

        self._spawn_reader(self._proc.stdout, self._inbox)
        self._spawn_reader(self._proc.stderr, self._stderr_tail)

        result = self._request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": _CLIENT_INFO,
            },
        )
        self.protocol_version = result.get("protocolVersion")
        self.server_info = result.get("serverInfo") or {}
        self._notify("notifications/initialized")  # 握手完成通知（无响应）
        return self

    def _spawn_reader(self, stream, sink) -> None:
        """后台线程：管道 → 队列/环形缓冲。不抽干管道会写满缓冲区导致 server 阻塞。"""
        def pump() -> None:
            try:
                for line in stream:
                    line = line.strip()
                    if not line:
                        continue
                    if isinstance(sink, queue.Queue):
                        try:
                            sink.put(json.loads(line))
                        except json.JSONDecodeError:
                            continue  # server 往 stdout 打日志时忽略，不断连
                    else:
                        sink.append(line)
            except Exception:
                pass
            finally:
                if isinstance(sink, queue.Queue):
                    sink.put(None)  # EOF 哨兵：让等待中的请求立刻知道 server 没了

        thread = threading.Thread(target=pump, daemon=True)
        thread.start()
        self._threads.append(thread)

    def close(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            if proc.stdin and not proc.stdin.closed:
                proc.stdin.close()
        except Exception:
            pass
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    def __enter__(self) -> "MCPClient":
        return self.start()

    def __exit__(self, *exc_info) -> None:
        self.close()

    # ---------- JSON-RPC ----------

    def _send(self, payload: dict) -> None:
        if self._proc is None or self._proc.stdin is None:
            raise MCPError("MCP server 未启动")
        try:
            self._proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
            self._proc.stdin.flush()
        except (BrokenPipeError, ValueError) as exc:
            raise MCPError(f"MCP server 已关闭，无法发送 {payload.get('method')}: {exc}") from exc

    def _request(self, method: str, params: dict | None = None) -> dict:
        self._id += 1
        request_id = self._id
        payload = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            payload["params"] = params
        self._send(payload)

        while True:
            try:
                message = self._inbox.get(timeout=self.timeout)
            except queue.Empty:
                raise MCPError(
                    f"MCP 请求超时（{self.timeout}s）: {method}{self._stderr_hint()}"
                ) from None
            if message is None:
                raise MCPError(f"MCP server 已退出: {method}{self._stderr_hint()}")
            if message.get("id") != request_id:
                continue  # 跳过通知与其它响应
            if "error" in message:
                err = message["error"] or {}
                raise MCPError(f"{method} 失败: {err.get('code')} {err.get('message')}")
            return message.get("result") or {}

    def _notify(self, method: str, params: dict | None = None) -> None:
        payload = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        self._send(payload)

    def _stderr_hint(self) -> str:
        if not self._stderr_tail:
            return ""
        return "；server stderr 末尾: " + " | ".join(list(self._stderr_tail)[-3:])

    # ---------- MCP 方法 ----------

    def list_tools(self) -> list[dict]:
        """`tools/list`：返回远端工具描述（name / description / inputSchema / annotations）。"""
        result = self._request("tools/list")
        tools = result.get("tools") or []
        return [t for t in tools if isinstance(t, dict) and t.get("name")]

    def call_tool(self, name: str, arguments: dict) -> ToolResult:
        """`tools/call`：把 MCP 的 content 数组拍平成给模型看的文本。"""
        result = self._request("tools/call", {"name": name, "arguments": arguments or {}})
        text = _flatten_content(result.get("content"))
        if result.get("isError"):
            return ToolResult.fail(error=text or "MCP 工具返回错误", output=text)
        return ToolResult.ok(text, data={"raw": result})


def _flatten_content(content) -> str:
    """MCP content 数组 → 文本：text 直接拼，其它类型给可读占位（不静默丢）。"""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return "" if content is None else str(content)
    parts: list[str] = []
    for item in content:
        if not isinstance(item, dict):
            parts.append(str(item))
            continue
        kind = item.get("type")
        if kind == "text":
            parts.append(str(item.get("text", "")))
        elif kind == "resource":
            resource = item.get("resource") or {}
            parts.append(f"[资源 {resource.get('uri', '?')}] {resource.get('text', '')}")
        else:
            parts.append(f"[{kind or '未知类型'} 内容，已省略]")
    return "\n".join(p for p in parts if p)


class MCPToolAdapter(Tool):
    """把远端 MCP 工具包装成本项目的 Tool 协议。

    两处必须覆写基类行为：
    - `schema()`：参数的 JSON Schema 由**远端**定义，不能从 pydantic 模型生成；
    - `run()`：跳过 pydantic 校验，参数原样透传给 server（远端才是权威校验方，
      本地再校验一遍只会因 schema 方言差异误杀合法调用）。
    """

    input_model = BaseModel  # 占位：schema/run 均已覆写，不走基类路径

    def __init__(self, client: MCPClient, remote: dict) -> None:
        self.name = str(remote["name"])
        self.description = str(
            remote.get("description") or f"MCP 工具 {self.name}（来自 {client.name}）"
        )
        self._client = client
        schema = remote.get("inputSchema")
        self._schema = schema if isinstance(schema, dict) else {
            "type": "object",
            "properties": {},
        }
        # 只读性只信 server 声明；没声明就当可写（保守：串行执行，绝不并发跑未知副作用）
        annotations = remote.get("annotations") or {}
        self._read_only = bool(annotations.get("readOnlyHint"))

    def is_read_only(self) -> bool:  # type: ignore[override]
        return self._read_only

    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self._schema,
            },
        }

    def run(self, arguments: dict, ctx: ToolContext) -> ToolResult:
        """跳过 pydantic 校验的统一入口（计时/异常兜底与基类一致）。"""
        start = time.perf_counter()
        try:
            result = self.execute(arguments or {}, ctx)
        except MCPError as exc:
            result = ToolResult.fail(error=str(exc))
        except Exception as exc:
            result = ToolResult.fail(error=f"{type(exc).__name__}: {exc}")
        result.duration_ms = int((time.perf_counter() - start) * 1000)
        return result

    def execute(self, args, ctx: ToolContext) -> ToolResult:
        return self._client.call_tool(self.name, dict(args or {}))


def load_mcp_servers(
    config_path: Path,
    registry: ToolRegistry,
    *,
    workspace_root: Path | None = None,
) -> tuple[list[MCPClient], list[str]]:
    """按配置文件启动若干 MCP server，把它们的工具注册进 registry。

    配置格式（`.codeagent/mcp.json`）：
        {"servers": {"<别名>": {"command": ["python", "-m", "some_server"]}}}

    返回 (clients, 注册的工具名)。**clients 由调用方负责 close()**——进程和
    连接是资源，不能交给 GC 碰运气。任何单个 server 失败都不影响其它 server 与
    主流程（MCP 是增强项，不该成为启动路径上的单点故障）。
    """
    config_path = Path(config_path)
    if not config_path.exists():
        raise FileNotFoundError(f"MCP 配置不存在: {config_path}")
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise MCPError(f"MCP 配置不是合法 JSON: {config_path}: {exc}") from exc

    clients: list[MCPClient] = []
    registered: list[str] = []
    for alias, spec in (config.get("servers") or {}).items():
        command = (spec or {}).get("command") or []
        if not command:
            print(f"[mcp] 跳过 {alias}: 缺少 command", file=sys.stderr)
            continue
        client = MCPClient(
            command,
            name=alias,
            timeout=float((spec or {}).get("timeout") or DEFAULT_TIMEOUT),
            cwd=workspace_root,
        )
        try:
            client.start()
            remote_tools = client.list_tools()
        except MCPError as exc:
            client.close()
            print(f"[mcp] {alias} 启动失败，已跳过: {exc}", file=sys.stderr)
            continue
        clients.append(client)
        for remote in remote_tools:
            adapter = MCPToolAdapter(client, remote)
            if adapter.name in registry:
                # 与内置工具同名：内置优先，MCP 工具加前缀避免覆盖（也避免静默遮蔽）
                adapter.name = f"{alias}__{adapter.name}"
                if adapter.name in registry:
                    continue
            registry.register(adapter)
            registered.append(adapter.name)
    return clients, registered
