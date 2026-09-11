"""MCP 客户端（M6-3 起步；M9-4 补传输与能力面）：把标准 MCP server 接进工具层。

**为什么手写而不装 `mcp` 官方 SDK**：
1. 官方 SDK 是 async（anyio）的，而我们的 QueryEngine 是同步循环——为了一个工具
   把整条循环改成 async 得不偿失；手写同步版本的阻抗最小。
2. MCP 的协议面很窄（JSON-RPC 2.0 + 十来个方法），手写一遍比封一层 SDK 更透明，
   也更能讲清楚协议本身。
3. 不引入新依赖（HTTP 传输用标准库 `urllib`，与 `tools/web.py` 同一手法）。

**分层（M9-4）**：`Transport`（怎么把一条 JSON-RPC 消息送出去、拿回来）与
`MCPClient`（说什么）**分开**。加 HTTP 传输时协议层一行未改——`_request` /
`_notify` / `list_tools` / `call_tool` 全都不知道自己跑在管道还是 socket 上。
这不是设计洁癖，是**同一个错误不想修两遍**：超时、id 关联、错误整形、
会话重建这几件事在两种传输上必须完全一致。

- `StdioTransport`：起子进程，stdout 由后台线程抽到队列（管道阻塞读没法设超时，
  Windows 上 select 也不支持 pipe）；stderr 单独抽到环形缓冲，出错时能给出 server
  的**真实报错**而不是干巴巴一句"超时"。
- `HttpTransport`：Streamable HTTP（MCP 2025-06-18 的传输）——单个端点收 POST，
  响应可能是 `application/json`（一条消息），也可能是 `text/event-stream`
  （若干条消息，最后一条通常是本次请求的响应）。POST 本身**同时承担收发**，
  所以它不需要后台线程：`send()` 把响应解析进队列，`receive()` 从队列取。

**HTTP 传输明确未做**（如实标注，别当成已实现）：独立 GET SSE 流（server 主动
发起请求用的那条长连接）与 `Last-Event-ID` 断点续传。tools/resources/prompts
三件事都走 POST 请求-响应，用不到它们；真需要时再加。

**安全边界（重要）**：MCP 工具来自第三方 server，**不受我们 workspace 沙箱约束**——
远端 server 想干什么都行。所以：
- MCP 工具**必须显式配置才注册**（不进 ToolRegistry.default）；
- 只读性**只信 server 自己声明的 `annotations.readOnlyHint`**，默认当可写（串行执行）；
- 但它们仍然走 `_gate_and_run` 的门禁链 → **hooks 和权限引擎照样生效**，
  这正是把权限/钩子做成独立层的回报：接新工具来源不用改循环。
- `url` **不做 SSRF 校验**，与 `web_fetch` 刻意不同：那个 URL 是**模型**给的，
  这个是人写在 `mcp.json` 里的显式 opt-in。校验一个用户自己填的地址没有意义。
"""
from __future__ import annotations

import fnmatch
import json
import queue
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from collections import deque
from pathlib import Path

from pydantic import BaseModel, Field

from agent.tools.base import Tool, ToolContext, ToolRegistry, ToolResult

# 客户端声明的协议版本（server 可回它自己的；按 spec 以 server 返回为准）
PROTOCOL_VERSION = "2025-06-18"
DEFAULT_TIMEOUT = 20.0
_CLIENT_INFO = {"name": "codeagent", "version": "0.1.0"}

#: 工具描述里最多列出多少条资源/提示词。描述**恒在上下文里**（每轮都发给模型），
#: 不封顶的话，一个挂了 500 处资源的 server 会把 schema 撑成主要开销。
MAX_LISTED_ITEMS = 20


class MCPError(RuntimeError):
    """MCP 协议/传输层错误。"""


class MCPTimeout(MCPError):
    """等响应等超时了。

    单独一个类型，是为了让**报错能带上方法名**：传输层只知道"没人来"，
    知道"在等哪个方法"的只有协议层。合成一句放在 `_request` 里，
    两种传输的诊断信息就一致了（否则 stdio 报得出方法、HTTP 报不出）。
    """


class MCPSessionExpired(MCPError):
    """HTTP 会话过期（server 对带 session id 的请求回了 404）。

    spec 要求此时客户端**重新 initialize** 开一个新会话再重试，而不是把错误
    直接甩给上层——多半能自愈，甩出去则一次网络抖动就废掉整个任务。
    """


# ---------------------------------------------------------------- 传输层


class Transport(ABC):
    """一条 JSON-RPC 消息的收发通道。MCPClient 只依赖这四个方法 + `timeout`。

    `timeout` 放在传输层是刻意的：**只有一个真相源**。socket 连接超时与
    "等响应等到什么时候"必须是同一个值，两处各存一份迟早对不上（一个 20s
    一个 30s，表现是"有时报超时有时不报"）。
    """

    def __init__(self, *, timeout: float = DEFAULT_TIMEOUT) -> None:
        self.timeout = timeout

    @abstractmethod
    def start(self) -> None:
        """建立连接（stdio：起子进程；HTTP：无需动作）。"""

    @abstractmethod
    def send(self, payload: dict) -> None:
        """送出一条 JSON-RPC 消息。响应（若有）进内部队列，由 `receive` 取。"""

    @abstractmethod
    def receive(self, timeout: float) -> dict | None:
        """取下一条消息；`None` 表示通道已结束（server 没了）。超时抛 `MCPError`。"""

    @abstractmethod
    def close(self) -> None:
        """释放资源（幂等）。"""

    def hint(self) -> str:
        """出错时补充的上下文（stdio 给 server stderr 末尾；HTTP 没有）。"""
        return ""


class StdioTransport(Transport):
    """stdin/stdout 上的按行 JSON-RPC（子进程）。"""

    def __init__(
        self,
        command: list[str],
        *,
        timeout: float = DEFAULT_TIMEOUT,
        cwd: Path | None = None,
    ) -> None:
        if not command:
            raise ValueError("MCP server command 不能为空")
        super().__init__(timeout=timeout)
        self.command = list(command)
        self.cwd = Path(cwd) if cwd else None

        self._proc: subprocess.Popen | None = None
        self._inbox: queue.Queue = queue.Queue()
        self._stderr_tail: deque[str] = deque(maxlen=40)
        self._threads: list[threading.Thread] = []

    def start(self) -> None:
        if self._proc is not None:
            return
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

    def send(self, payload: dict) -> None:
        if self._proc is None or self._proc.stdin is None:
            raise MCPError("MCP server 未启动")
        try:
            self._proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
            self._proc.stdin.flush()
        except (BrokenPipeError, ValueError) as exc:
            raise MCPError(f"MCP server 已关闭，无法发送 {payload.get('method')}: {exc}") from exc

    def receive(self, timeout: float) -> dict | None:
        try:
            return self._inbox.get(timeout=timeout)
        except queue.Empty:
            raise MCPTimeout(f"MCP 请求超时（{timeout}s）{self.hint()}") from None

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

    def hint(self) -> str:
        if not self._stderr_tail:
            return ""
        return "；server stderr 末尾: " + " | ".join(list(self._stderr_tail)[-3:])


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """不跟随重定向，直接把 3xx 当错误抛出去。

    `urllib` 默认会把 301/302/303 上的 POST **改写成 GET** —— 那会把一次
    `tools/call` 变成一次静默的读请求：server 大概率回 405，而用户看到的是
    "工具调用失败"，真正的病因（配置里的 url 少了个斜杠 / 写的还是 http）
    一个字都不会出现。宁可报一句能照着改的话。
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(
            req.full_url, code,
            f"{msg}（重定向到 {newurl}）；请把 mcp.json 里的 url 直接写成最终地址",
            headers, fp,
        )


class HttpTransport(Transport):
    """Streamable HTTP：POST 一条 JSON-RPC，响应可能是 JSON 也可能是 SSE 流。

    **POST 同时承担收发**，所以不需要后台读线程：`send()` 把响应里的每条消息
    推进队列，`receive()` 从队列取。这与 stdio 的形态不同，但对 `MCPClient`
    完全一样——这正是把传输抽出来的意义。
    """

    def __init__(
        self,
        url: str,
        *,
        headers: dict | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        if not str(url or "").strip():
            raise ValueError("MCP server url 不能为空")
        super().__init__(timeout=timeout)
        self.url = str(url)
        provided = dict(headers or {})
        # 头里已有的话以用户配置为准？不——这两条是协议要求，配错会让握手失败，
        # 所以用户配置**不能覆盖**它们（只做补充：认证头、租户头之类）。
        lowered = {k.lower() for k in provided}
        self._extra_headers = {k: v for k, v in provided.items()
                               if k.lower() not in ("content-type", "accept", "mcp-protocol-version")}
        self._accept_overridden = "accept" in lowered
        self.session_id: str | None = None
        self._inbox: queue.Queue = queue.Queue()
        self._closed = False

    def start(self) -> None:
        return  # HTTP 无连接可建：每次请求自带上下文

    def send(self, payload: dict) -> None:
        if self._closed:
            raise MCPError("MCP HTTP 传输已关闭")
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(self.url, data=body, method="POST")
        request.add_header("Content-Type", "application/json")
        # spec 要求两个都要列出：server 才知道客户端能不能收 SSE 流
        request.add_header("Accept", "application/json, text/event-stream")
        request.add_header("MCP-Protocol-Version", PROTOCOL_VERSION)
        if self.session_id:
            request.add_header("Mcp-Session-Id", self.session_id)
        for key, value in self._extra_headers.items():
            request.add_header(key, str(value))

        try:
            opener = urllib.request.build_opener(_NoRedirect())
            with opener.open(request, timeout=self.timeout) as response:
                content_type = (response.headers.get_content_type() or "").lower()
                raw = response.read()
                self._remember_session(response.headers)
        except urllib.error.HTTPError as exc:
            self._raise_for_http_error(exc)
            return
        except urllib.error.URLError as exc:
            raise MCPError(f"连不上 MCP 端点 {self.url}: {exc.reason}") from exc

        for message in _decode_body(raw, content_type):
            self._inbox.put(message)

    def _remember_session(self, headers) -> None:
        session_id = headers.get("Mcp-Session-Id")
        if session_id:
            self.session_id = session_id

    def _raise_for_http_error(self, exc: urllib.error.HTTPError) -> None:
        """把 HTTP 层的失败整形**成一次 MCPError**（含 server 给的 JSON-RPC 错误）。

        **判断顺序是有讲究的：404 + 带 session id 必须排在"正文里有 JSON-RPC
        error"之前。** 真实 server 回 404 时常常在正文里也放一条 error，先匹配
        正文就会把"会话过期"报成一次普通调用失败 —— 错误信息看着挺合理，
        但自愈那条路（重新 initialize + 重试）永远走不到，一次 server 重启
        就能废掉整个任务。假 server 特意用这种正文来钉住顺序。
        """
        detail = ""
        try:
            detail = exc.read().decode("utf-8", errors="replace").strip()
        except Exception:
            pass
        if exc.code == 404 and self.session_id:
            raise MCPSessionExpired(
                f"HTTP 404：会话 {self.session_id} 在服务端已不存在（可能已过期或重启）"
            )
        parsed = None
        if detail.startswith("{"):
            try:
                parsed = json.loads(detail)
            except json.JSONDecodeError:
                parsed = None
        if isinstance(parsed, dict) and "error" in parsed:
            err = parsed["error"] or {}
            raise MCPError(f"HTTP {exc.code}: {err.get('code')} {err.get('message')}")
        suffix = f": {detail[:300]}" if detail else ""
        raise MCPError(f"HTTP {exc.code} {exc.reason}{suffix}")

    def receive(self, timeout: float) -> dict | None:
        try:
            return self._inbox.get(timeout=timeout)
        except queue.Empty:
            raise MCPTimeout(
                f"MCP 请求超时（{timeout}s，端点 {self.url}）：响应里没有对应的消息"
            ) from None

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._inbox.put(None)  # 唤醒等待中的 receive

    def hint(self) -> str:
        return ""


def _decode_body(raw: bytes, content_type: str) -> list[dict]:
    """HTTP 响应体 → JSON-RPC 消息列表（JSON 单条，或 SSE 若干条）。"""
    if not raw:
        return []          # 202 Accepted：通知类请求的正常响应，没有消息
    text = raw.decode("utf-8", errors="replace")
    if content_type == "text/event-stream":
        return _parse_sse(text)
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        raise MCPError(f"MCP 端点回了非 JSON 响应（{content_type or '无 Content-Type'}）: {text[:200]}") from None
    if isinstance(parsed, list):        # 批量（2025-06-18 已移除，容错收下）
        return [m for m in parsed if isinstance(m, dict)]
    return [parsed] if isinstance(parsed, dict) else []


def _parse_sse(text: str) -> list[dict]:
    """SSE → JSON-RPC 消息列表。

    只认 `data:` 字段（`event:` / `id:` / `retry:` 与以 `:` 开头的心跳都跳过）。
    一条消息的 data 可以跨多行，**空行才是分发点**；流末尾没有空行时也要收下
    —— server 直接关流是常见写法，漏收会让请求白白超时。
    """
    messages: list[dict] = []
    data_lines: list[str] = []

    def flush() -> None:
        if not data_lines:
            return
        payload = "\n".join(data_lines)
        data_lines.clear()
        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError:
            return
        if isinstance(parsed, dict):
            messages.append(parsed)

    for line in text.splitlines():
        if line.startswith(":"):
            continue
        if not line.strip():
            flush()
            continue
        field, _, value = line.partition(":")
        if field == "data":
            data_lines.append(value[1:] if value.startswith(" ") else value)
    flush()
    return messages


# ---------------------------------------------------------------- 协议层


class MCPClient:
    """JSON-RPC + MCP 方法。**不知道自己跑在管道还是 socket 上**（M9-4）。

    生命周期、id 关联、超时整形、错误包装都在这一层，两种传输因此行为一致。
    """

    def __init__(
        self,
        transport: Transport,
        *,
        name: str = "mcp",
    ) -> None:
        self.transport = transport
        self.name = name
        self.server_info: dict = {}
        self.protocol_version: str | None = None
        self.capabilities: dict = {}
        self._id = 0

    @property
    def timeout(self) -> float:
        return self.transport.timeout

    # ---------- 生命周期 ----------

    def start(self) -> "MCPClient":
        self.transport.start()
        self._handshake()
        return self

    def _handshake(self) -> None:
        """initialize + initialized 通知。**HTTP 会话过期后会再走一次**，所以抽出来。"""
        result = self._request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": _CLIENT_INFO,
            },
            retry=False,        # 握手本身不能再触发"重建会话"，否则会无限递归
        )
        self.protocol_version = result.get("protocolVersion")
        self.server_info = result.get("serverInfo") or {}
        self.capabilities = result.get("capabilities") or {}
        self._notify("notifications/initialized")  # 握手完成通知（无响应）

    def close(self) -> None:
        self.transport.close()

    def __enter__(self) -> "MCPClient":
        return self.start()

    def __exit__(self, *exc_info) -> None:
        self.close()

    # ---------- JSON-RPC ----------

    def _request(self, method: str, params: dict | None = None, *, retry: bool = True) -> dict:
        self._id += 1
        request_id = self._id
        payload = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            payload["params"] = params

        try:
            self.transport.send(payload)
        except MCPSessionExpired:
            if not retry:
                raise
            # spec：会话没了就重新 initialize 开一个新的，再重试一次。
            # 只重试一次 —— server 每次都回 404 时，无限重试等于把超时改成死循环。
            self._handshake()
            self._id += 1
            request_id = self._id
            payload["id"] = request_id
            self.transport.send(payload)

        while True:
            try:
                message = self.transport.receive(self.timeout)
            except MCPTimeout as exc:
                # 传输层不知道在等哪个方法，补上再抛 —— 一句"超时了"没法排查，
                # 一句"等 tools/call 超时了"能直接定位到是哪一次交互。
                raise MCPTimeout(f"{exc}（等待 {method} 的响应）") from None
            if message is None:
                raise MCPError(f"MCP server 已退出: {method}{self.transport.hint()}")
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
        self.transport.send(payload)

    # ---------- MCP 方法 ----------

    def _list(self, method: str, key: str, required: str) -> list[dict]:
        """`*/list` 三处形状一致，收成一处：取 key、丢掉不合规的条目。

        **坏条目丢掉而不是抛错**：一个 server 多返回一条没有 name/uri 的记录，
        不该让整台 server 不可用（与"一个坏 meta 不该藏起另外九个会话"同一条）。
        """
        result = self._request(method)
        items = result.get(key) or []
        return [i for i in items if isinstance(i, dict) and i.get(required)]

    def list_tools(self) -> list[dict]:
        """`tools/list`：返回远端工具描述（name / description / inputSchema / annotations）。"""
        return self._list("tools/list", "tools", "name")

    def call_tool(self, name: str, arguments: dict) -> ToolResult:
        """`tools/call`：把 MCP 的 content 数组拍平成给模型看的文本。"""
        result = self._request("tools/call", {"name": name, "arguments": arguments or {}})
        text = _flatten_content(result.get("content"))
        if result.get("isError"):
            return ToolResult.fail(error=text or "MCP 工具返回错误", output=text)
        return ToolResult.ok(text, data={"raw": result})

    # ---------- resources / prompts（M9-4） ----------

    def list_resources(self) -> list[dict]:
        """`resources/list`：远端可读的资源（uri / name / mimeType / description）。"""
        return self._list("resources/list", "resources", "uri")

    def read_resource(self, uri: str) -> ToolResult:
        """`resources/read`：资源正文。

        `contents` 里每一项**要么有 `text` 要么有 `blob`（base64）**。二进制不
        解码成乱码塞给模型，而是给一句说明 + 字节数——**如实说"这里有个二进制、
        多大"，比给一坨看似内容的乱码有用**（与 web 工具认不出编码时的处理同一条）。
        """
        result = self._request("resources/read", {"uri": uri})
        contents = result.get("contents")
        if not isinstance(contents, list) or not contents:
            return ToolResult.ok("", data={"raw": result})
        parts: list[str] = []
        for item in contents:
            if not isinstance(item, dict):
                parts.append(str(item))
                continue
            if "text" in item:
                parts.append(str(item.get("text") or ""))
            elif "blob" in item:
                mime = item.get("mimeType") or "未知类型"
                parts.append(f"[二进制内容 {mime}，约 {_blob_size(str(item.get('blob') or ''))} 字节，未解码]")
            else:
                parts.append(f"[资源 {item.get('uri', uri)} 没有内容字段]")
        return ToolResult.ok("\n".join(p for p in parts if p), data={"raw": result})

    def list_prompts(self) -> list[dict]:
        """`prompts/list`：远端提示词模板（name / description / arguments）。"""
        return self._list("prompts/list", "prompts", "name")

    def get_prompt(self, name: str, arguments: dict | None = None) -> ToolResult:
        """`prompts/get`：取回模板渲染后的消息。"""
        result = self._request("prompts/get", {"name": name, "arguments": arguments or {}})
        messages = result.get("messages")
        if not isinstance(messages, list):
            return ToolResult.ok(_flatten_content(result.get("description")), data={"raw": result})
        parts: list[str] = []
        for message in messages:
            if not isinstance(message, dict):
                parts.append(str(message))
                continue
            role = message.get("role") or "?"
            parts.append(f"[{role}] {_flatten_content(message.get('content'))}")
        return ToolResult.ok("\n".join(parts), data={"raw": result})


def _blob_size(blob: str) -> int:
    """base64 文本 → 原始字节数。

    **不能写成 `len(blob) * 3 // 4`** —— 那没算末尾的 `=` 填充，4 字节的
    `AAECAw==` 会被报成 6 字节。一个纯粹的算术错误，错得毫无提示。
    """
    padding = len(blob) - len(blob.rstrip("="))
    return max(0, (len(blob) // 4) * 3 - padding)


def _flatten_content(content) -> str:
    """MCP content → 文本：text 直接拼，其它类型给可读占位（不静默丢）。

    入参**两种形状都要收**：`CallToolResult.content` 是**数组**，而
    `PromptMessage.content` 是**单个** content block（spec 就是这么定的）。
    只处理数组的话，提示词模板的内容会以 `str(dict)` 的原样喂给模型 ——
    一眼看去像"内容就是长这样"，不会报错。
    """
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        content = [content]
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
        elif kind == "image":
            mime = item.get("mimeType") or "图片"
            parts.append(f"[{mime} 内容，本地模型无法查看]")
        else:
            parts.append(f"[{kind or '未知类型'} 内容，已省略]")
    return "\n".join(p for p in parts if p)


# ---------------------------------------------------------------- 工具适配


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

    @classmethod
    def is_external(cls) -> bool:  # type: ignore[override]
        """第三方 server 的工具：不受 workspace 沙箱约束，权限默认不放行。"""
        return True

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


class ReadResourceInput(BaseModel):
    uri: str = Field(description="要读取的资源 URI，取值见工具说明里列出的可用资源")


class MCPResourceTool(Tool):
    """`resources/read` 的工具面（M9-4）。

    **为什么描述里要列出可用资源**：不列的话，模型不知道有哪些 uri 可读，
    唯一能做的就是瞎猜一个试试 —— 这正是 M8 `update_plan` 那个 elicitation gap
    的形状（工具实现了、能落盘，但没有任何东西把模型引向它）。描述恒在上下文里，
    列出来等于**零额外往返**地把"你能读什么"告诉模型。
    """

    name = "read_resource"
    description = "读取 MCP server 上的一处资源"
    input_model = ReadResourceInput

    def __init__(self, client: MCPClient, resources: list[dict]) -> None:
        self._client = client
        self._resources = resources
        self.description = _render_resources(client.name, resources)

    @classmethod
    def is_read_only(cls) -> bool:  # type: ignore[override]
        return True

    @classmethod
    def is_external(cls) -> bool:  # type: ignore[override]
        return True

    def execute(self, args: ReadResourceInput, ctx: ToolContext) -> ToolResult:
        return self._client.read_resource(args.uri)


class GetPromptInput(BaseModel):
    name: str = Field(description="提示词名字，取值见工具说明里列出的可用提示词")
    keys: list[str] = Field(
        default_factory=list,
        description="提示词参数名，与 values 一一对应；没有参数就传空数组",
    )
    values: list[str] = Field(
        default_factory=list,
        description="与 keys 一一对应的参数值，顺序必须对齐",
    )


class MCPPromptTool(Tool):
    """`prompts/get` 的工具面（M9-4）。

    参数用**两个平行数组**而不是一个对象：项目硬约束是「工具参数扁平化、
    schema 无 `$defs`」，而 `base.py` 的 `raw.pop("$defs", None)` 会把嵌套模型
    **静默**削成坏 schema。同 `update_plan` 的理由。
    """

    name = "get_prompt"
    description = "取用 MCP server 上的一则提示词模板"
    input_model = GetPromptInput

    def __init__(self, client: MCPClient, prompts: list[dict]) -> None:
        self._client = client
        self._prompts = prompts
        self.description = _render_prompts(client.name, prompts)

    @classmethod
    def is_read_only(cls) -> bool:  # type: ignore[override]
        return True

    @classmethod
    def is_external(cls) -> bool:  # type: ignore[override]
        return True

    def execute(self, args: GetPromptInput, ctx: ToolContext) -> ToolResult:
        if len(args.keys) != len(args.values):
            # 回喂原因让模型自修复（与 update_plan 的长度校验同一条）
            return ToolResult.fail(
                error=f"keys 与 values 必须一一对应（收到 {len(args.keys)} 个名字、"
                      f"{len(args.values)} 个值）"
            )
        return self._client.get_prompt(args.name, dict(zip(args.keys, args.values)))


def _render_resources(client_name: str, resources: list[dict]) -> str:
    """列资源。**只列 uri 与一句话描述**，不把正文拉进来——描述每轮都发。"""
    lines = [f"读取 MCP server `{client_name}` 上的一处资源。可用资源："]
    for item in resources[:MAX_LISTED_ITEMS]:
        uri = item.get("uri")
        tail = "；".join(str(x) for x in (item.get("name"), item.get("mimeType"),
                                          item.get("description")) if x)
        lines.append(f"- {uri}" + (f"（{tail}）" if tail else ""))
    if len(resources) > MAX_LISTED_ITEMS:
        lines.append(f"…另有 {len(resources) - MAX_LISTED_ITEMS} 处未列出")
    return "\n".join(lines)


def _render_prompts(client_name: str, prompts: list[dict]) -> str:
    """列提示词。**把声明的参数名一并列出**，否则模型只能靠试错去猜参数。"""
    lines = [f"取用 MCP server `{client_name}` 上的一则提示词模板。可用提示词："]
    for item in prompts[:MAX_LISTED_ITEMS]:
        name = item.get("name")
        desc = item.get("description") or ""
        head = f"- {name}" + (f"（{desc}）" if desc else "")
        declared = [a for a in (item.get("arguments") or []) if isinstance(a, dict) and a.get("name")]
        if declared:
            args = "、".join(
                f"{a['name']}{'(必填)' if a.get('required') else '(可选)'}" for a in declared
            )
            head += f" 参数: {args}"
        lines.append(head)
    if len(prompts) > MAX_LISTED_ITEMS:
        lines.append(f"…另有 {len(prompts) - MAX_LISTED_ITEMS} 则未列出")
    return "\n".join(lines)


# ---------------------------------------------------------------- 配置加载


def build_transport(spec: dict, *, workspace_root: Path | None = None) -> Transport:
    """按配置项造传输：`command` → stdio，`url` → HTTP。

    **只此一处判断用哪种传输**。放在这里而不是 `load_mcp_servers` 里，是因为
    "command 与 url 二选一"是个契约：两处各判一遍（一处管加载、一处管报错）
    必然漂移，最后表现成"配置了 url 却被当成 stdio 去起子进程"。
    """
    command = spec.get("command")
    url = spec.get("url")
    timeout = float(spec.get("timeout") or DEFAULT_TIMEOUT)
    if command and url:
        raise MCPError("command 与 url 只能给一个（stdio 与 HTTP 二选一）")
    if url:
        return HttpTransport(str(url), headers=spec.get("headers"), timeout=timeout)
    if command:
        return StdioTransport(list(command), timeout=timeout, cwd=workspace_root)
    raise MCPError("缺少 command（stdio）或 url（HTTP）")


def load_mcp_servers(
    config_path: Path,
    registry: ToolRegistry,
    *,
    workspace_root: Path | None = None,
) -> tuple[list[MCPClient], list[str], list[str]]:
    """按配置文件启动若干 MCP server，把它们的工具注册进 registry。

    配置格式（`.codeagent/mcp.json`）：
        {"servers": {"<别名>": {
            "command": ["python", "-m", "some_server"],   # stdio
            "url": "https://host/mcp",                    # 或 HTTP（二选一）
            "headers": {"Authorization": "Bearer ..."},   # HTTP 可选
            "allow": ["get_current_time"]                 # 可选：允许免确认调用的工具
        }}}

    返回 `(clients, 注册的工具名, 已授权免确认的工具名)`。**clients 由调用方负责
    close()**——进程和连接是资源，不能交给 GC 碰运气。任何单个 server 失败都不影响
    其它 server 与主流程（MCP 是增强项，不该成为启动路径上的单点故障）。

    **`allow` 未列出 = 不放行**：MCP 工具来自第三方 server，不受 workspace 沙箱
    约束，所以权限引擎对它们默认 ASK（CLI 无确认交互 → 拒绝）。这是刻意的行为
    变更：接第三方 server 必须显式说明允许它干什么，而不是接上就放行。

    `allow` 里写**远端工具名**（也支持 fnmatch 通配，如 `"time__*"`）；匹配在
    这里完成——因为只有这一处同时知道「远端名」和「注册名」（与内置工具重名时
    会加 `<别名>__` 前缀），权限引擎拿到的就是两个扁平的最终名单。
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
    allowed: list[str] = []
    for alias, spec in (config.get("servers") or {}).items():
        spec = spec or {}
        try:
            transport = build_transport(spec, workspace_root=workspace_root)
        except (MCPError, ValueError) as exc:
            print(f"[mcp] 跳过 {alias}: {exc}", file=sys.stderr)
            continue
        allow_patterns = [str(p) for p in (spec.get("allow") or [])]
        client = MCPClient(transport, name=alias)
        try:
            client.start()
            remote_tools = client.list_tools()
        except MCPError as exc:
            client.close()
            print(f"[mcp] {alias} 启动失败，已跳过: {exc}", file=sys.stderr)
            continue
        clients.append(client)
        for remote in remote_tools:
            _register(registry, MCPToolAdapter(client, remote), alias,
                      allow_patterns, registered, allowed)
        _register_capability_tools(client, registry, alias, allow_patterns, registered, allowed)
        if not allow_patterns:
            print(
                f"[mcp] {alias} 未配置 allow：其工具默认需人工确认，"
                f'请在 mcp.json 里加 "allow": ["<工具名>"]',
                file=sys.stderr,
            )
    return clients, registered, allowed


def _register_capability_tools(
    client: MCPClient,
    registry: ToolRegistry,
    alias: str,
    allow_patterns: list[str],
    registered: list[str],
    allowed: list[str],
) -> None:
    """注册 resources / prompts 两个工具面（M9-4）。

    **两条都满足才注册**：server 声明了该能力，**且列表非空**。
    只判能力不判列表的话，一个声明了 `resources` 却一处资源都没有的 server 会拿到
    一个"永远调不通"的工具——白占 schema token，且模型每次调用都失败一次。
    能力没声明就更不该注册：那说明这台 server 根本不支持，请求只会换回一个
    JSON-RPC error（`-32601`）。
    """
    caps = client.capabilities or {}
    for cap, lister, builder in (
        ("resources", client.list_resources, MCPResourceTool),
        ("prompts", client.list_prompts, MCPPromptTool),
    ):
        if cap not in caps:
            continue
        try:
            items = lister()
        except MCPError as exc:
            # 声明了能力却列不出来（server 半坏）：不影响它已经注册的工具
            print(f"[mcp] {alias} 的 {cap}/list 失败，跳过该工具面: {exc}", file=sys.stderr)
            continue
        if not items:
            continue
        _register(registry, builder(client, items), alias, allow_patterns, registered, allowed)


def _register(
    registry: ToolRegistry,
    tool: Tool,
    alias: str,
    allow_patterns: list[str],
    registered: list[str],
    allowed: list[str],
) -> None:
    """注册一个 MCP 工具：与内置同名则加别名前缀，再按 allow 决定是否免确认。

    前缀与授权**必须一起做**：`allow` 匹配要用到最终注册名，分开写就会出现
    "授权的是前缀名、注册的是原名"这类漂移。三处调用共用这一个入口。
    """
    raw_name = tool.name
    if tool.name in registry:
        # 与内置工具同名：内置优先，MCP 工具加前缀避免覆盖（也避免静默遮蔽）
        tool.name = f"{alias}__{tool.name}"
        if tool.name in registry:
            return
    registry.register(tool)
    registered.append(tool.name)
    if _is_allowed(raw_name, tool.name, allow_patterns):
        allowed.append(tool.name)


def _is_allowed(raw_name: str, final_name: str, patterns: list[str]) -> bool:
    """远端名或注册名命中任一 allow 模式即免确认（支持 `time__*` 这类通配）。"""
    return any(
        fnmatch.fnmatch(raw_name, pat) or fnmatch.fnmatch(final_name, pat)
        for pat in patterns
    )
