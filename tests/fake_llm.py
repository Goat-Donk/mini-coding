"""并发安全的假 LLM（M9-7 用）。

**为什么不能复用 `agent.llm.MockLLM`**：它是 `self.responses.pop(0)` —— 一个
**有状态的队列**。两个 worker 共享同一个实例时会互相吃掉对方的脚本，脚本耗尽
后 `RuntimeError("MockLLM 响应已耗尽")` 在**错误的线程**里炸出来。那个错误的
样子（"响应没了"）与真正的问题（"两个 worker 在抢同一条脚本"）看起来毫无关系，
排查会往完全错误的方向走 —— 而 M9-7 的测试**全都是并发的**。

所以这里换成**无状态分派 + 调用方自己保证线程安全**：

- `FakeLLM(responder)`：`responder(messages, tools) -> LLMResult` 每个线程都会调，
  **它必须自己能并发调用**（要断言共享状态就自己加锁）。
- `keyed_llm(scripts)`：按"第一条 user 消息"分派，每个 key 一份**独立**的脚本队列。
  父代理与每个 worker 的 `messages[1]` 各不相同，所以天然分开 —— 这才是并发测试
  里想要的那种确定性。

与 `MockLLM` 一样，脚本耗尽会抛 —— 且抛的是 `AssertionError`：它几乎总是
**测试自己写错了**（少准备了一条响应），不是被测代码的问题。
"""
from __future__ import annotations

import threading
from typing import Callable, Sequence

from agent.llm import BaseLLM, LLMResult, Usage

Responder = Callable[[list[dict], list[dict]], LLMResult]


def first_user(messages: Sequence[dict]) -> str:
    """第一条 user 消息的正文 —— 用作"这是谁在说话"的分派键。"""
    for message in messages:
        if message.get("role") == "user":
            content = message.get("content")
            return content if isinstance(content, str) else str(content)
    return ""


class FakeLLM(BaseLLM):
    """把 `chat` 直接交给一个可调用对象；记录调用历史（加锁）。"""

    model = "fake"

    def __init__(self, responder: Responder) -> None:
        self.responder = responder
        self._lock = threading.Lock()
        self.calls: list[list[dict]] = []

    def chat(
        self,
        messages: list[dict],
        tools: list[dict],
        *,
        temperature: float = 0.0,
    ) -> LLMResult:
        with self._lock:
            # 只留 system/user 的文本摘要：完整 messages 在并发下会拷很多份，
            # 而这些测试要看的是"谁被调到了、拿到了什么任务"。
            self.calls.append(
                [{"role": m.get("role"), "content": m.get("content")} for m in messages]
            )
        return self.responder(messages, tools)


def keyed_llm(scripts: dict[str, list]) -> FakeLLM:
    """按第一条 user 消息分派的多份独立脚本。

    `scripts[key]` 是一个列表，元素是 `LLMResult` 或 `(messages, tools) -> LLMResult`。
    **每个 key 的队列独立推进** —— 这正是并发子代理测试要的：三个 worker 同时跑，
    各拿各的脚本，互不干扰。
    """
    queues: dict[str, list] = {key: list(items) for key, items in scripts.items()}
    lock = threading.Lock()

    def responder(messages: list[dict], tools: list[dict]) -> LLMResult:
        key = first_user(messages)
        with lock:
            queue = queues.get(key)
            if not queue:
                raise AssertionError(
                    f"没有为 {key!r} 准备更多响应（已有队列的 key: {sorted(queues)}）"
                )
            item = queue.pop(0)
        if callable(item):
            return item(messages, tools)
        return item

    return FakeLLM(responder)


def text(content: str, usage: Usage | None = None) -> LLMResult:
    return LLMResult(content=content, usage=usage or Usage())


def tool(
    name: str,
    arguments: dict,
    *,
    call_id: str = "call_0001",
    usage: Usage | None = None,
) -> LLMResult:
    from agent.llm import ToolCall

    return LLMResult(
        content=None,
        tool_calls=[ToolCall(id=call_id, name=name, arguments=dict(arguments))],
        usage=usage or Usage(),
    )
