"""会话状态与消息构造。

OpenAI Chat Completion 格式 dict 是 loop 与 LLM 之间的唯一契约：
本模块负责保证消息格式 100% 一致（tool_calls 的 id 对应是 OpenAI 硬性要求）。
AgentState 是 loop 的"数据面"——M3 会话检查点落盘的就是它。
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from agent.llm import ToolCall, Usage


# ---------- 消息构造器（OpenAI 格式） ----------

def system(content: str) -> dict:
    return {"role": "system", "content": content}


def user(content: str) -> dict:
    return {"role": "user", "content": content}


def assistant_text(content: str) -> dict:
    return {"role": "assistant", "content": content}


def assistant_tool_calls(calls: list[ToolCall]) -> dict:
    """assistant 发起工具调用；content=None 是 OpenAI 的合法形态。"""
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": call.id,
                "type": "function",
                "function": {
                    "name": call.name,
                    "arguments": json.dumps(call.arguments, ensure_ascii=False),
                },
            }
            for call in calls
        ],
    }


def tool_result(tool_call_id: str, content: str) -> dict:
    return {"role": "tool", "tool_call_id": tool_call_id, "content": content}


def text_of(message: dict) -> str:
    """提取消息文本，兼容 content 为 str / None / 列表（多模态响应）。"""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            part.get("text", "") for part in content if isinstance(part, dict)
        )
    return ""


# ---------- AgentState ----------

@dataclass
class AgentState:
    session_id: str
    task: str
    system_prompt: str
    messages: list[dict] = field(default_factory=list)       # OpenAI 格式消息历史
    step: int = 0
    usage: Usage = field(default_factory=Usage)              # 累计用量
    events: list[dict] = field(default_factory=list)         # 轨迹事件
    terminated_reason: Optional[str] = None
    memory_blocks: list[str] = field(default_factory=list)   # M4 注入的 repo 记忆

    # M3 上下文记账：最近一次 llm.chat 的 usage（provider 锚点）；compact 后置 stale
    last_usage: Optional[Usage] = None
    usage_stale_reason: Optional[str] = None                 # "snip_compact" | "llm_compact"

    # M3 接入 session 后设为回调；None 时事件只进内存列表
    emitter: Optional[Callable[[dict], None]] = None

    def record_event(self, type: str, **data) -> None:
        """记录轨迹事件：{ts, type, step, ...}。设了 emitter 则同时回调（写 JSONL）。"""
        event = {
            "ts": time.time(),
            "type": type,
            "step": self.step,
            **data,
        }
        self.events.append(event)
        if self.emitter is not None:
            self.emitter(event)
