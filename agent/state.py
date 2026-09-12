"""会话状态与消息构造。

OpenAI Chat Completion 格式 dict 是 loop 与 LLM 之间的唯一契约：
本模块负责保证消息格式 100% 一致（tool_calls 的 id 对应是 OpenAI 硬性要求）。
AgentState 是 loop 的"数据面"——M3 会话检查点落盘的就是它。
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable, Optional

from agent.llm import ToolCall, Usage
from agent.security import TAINT_NONE, higher

if TYPE_CHECKING:
    from agent.goal import Goal


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


#: 补齐配对时填进 tool 结果的内容。**必须说实话**：这条结果不是工具跑出来的。
#: 写成空串或 "ok" 会让模型以为那次调用成功了，然后基于一个不存在的结果往下走。
PAIRING_FILLER = (
    "[未执行] 这条工具调用没有留下结果（回合被中断）。"
    "如需它的结果，请重新调用。"
)


def ensure_tool_pairing(messages: list[dict]) -> int:
    """补齐被中断的回合留下的孤儿 tool_call，返回补了几条。

    **为什么必须有**：OpenAI 兼容端点要求 assistant 消息里每个 `tool_calls[].id`
    都要有一条对应的 `tool` 消息，否则整个请求 400。单发进程里这从不是问题 ——
    回合中途 Ctrl+C，进程就死了，下次靠检查点恢复，而检查点是在**完整的步边界**
    上落的。REPL 要接着用**同一个** `messages` 列表，中断就会留下
    「assistant(tool_calls=[3 个]) + 只有 1 条 tool 结果」这种形状，下一回合的
    请求直接 400 —— 而且报错发生在**下一回合**，看起来和中断那一下毫无关系。

    这里复用 `tool_result()` 而不是手写 dict：消息格式是这个模块的契约，
    多一处手拼就多一处漂移点。

    只补不删：多余的 tool 结果（找不到对应 id）不动 —— 那种形状不会让端点报错，
    而删掉它就等于替模型丢掉了它真跑出来的证据。
    """
    repaired = 0
    i = 0
    while i < len(messages):
        message = messages[i]
        if message.get("role") != "assistant" or not message.get("tool_calls"):
            i += 1
            continue
        expected = [
            call.get("id") for call in message["tool_calls"] if isinstance(call, dict)
        ]
        # 紧跟在后面的连续 tool 消息就是这个 assistant 的配对结果
        j = i + 1
        answered: set[str] = set()
        while j < len(messages) and messages[j].get("role") == "tool":
            answered.add(messages[j].get("tool_call_id"))
            j += 1
        missing = [cid for cid in expected if cid not in answered]
        if missing:
            messages[j:j] = [tool_result(cid, PAIRING_FILLER) for cid in missing]
            repaired += len(missing)
            j += len(missing)
        i = j
    return repaired


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

    # M8 agent 自己的任务计划（`update_plan` 工具写它）。**不是**仓库根的
    # TASKS.md（那是人类开发者的清单），两者只是中文都叫"计划"。
    # 就是权威状态本身，不是从事件派生的值 —— 所以**没有** `derive_plan` 这类
    # 对应物（对比下面的 `taint`，那个是从事件重放的）。
    plan: list[dict] = field(default_factory=list)           # [{"text": str, "status": str}]

    # M9-6 进程内目标（人用 `/goal` 创建，`agent/goal.py`）。与 plan 一样是
    # **权威状态本身**、随检查点走，不是从事件派生的值。
    # **绝不能**放进 `terminated_reason` —— 那个是**每回合一份**、由 `_run_loop`
    # 入口复位（M9-5 修的正是这个），把跨回合的东西放进去等于重造那个 bug。
    goal: Optional["Goal"] = None

    # M9-8 最近一次工作区回滚（`--rewind` / REPL `/rewind` 写进 meta，恢复时读回来）。
    # 形状 `{"step": int, "files": int}` 或 None（从没回滚过）。
    #
    # 它和 `taint` 一样是**权威状态本身**、随检查点走 —— 但不落检查点 payload 的
    # 顶层，而是走 meta：回滚是**人的动作**、发生在两个进程之间（`--rewind` 一个
    # 进程、`--resume` 另一个），写进检查点会造出一个"回滚本身就是一次 agent 工作"
    # 的假象。
    #
    # 为什么必须有这个字段：不告诉模型"盘面被抹回去过"，它就会照着历史里那些
    # 已经不存在的改动往下推理（`_reinject_rewind` 是它在上下文里的出口）。
    last_rewind: Optional[dict] = None

    # M3 上下文记账：最近一次 llm.chat 的 usage（provider 锚点）；compact 后置 stale
    last_usage: Optional[Usage] = None
    usage_stale_reason: Optional[str] = None                 # "tool_output_truncated" | "snip_compact" | "llm_compact"

    # M7 会话级污染标记：粗粒度（一个会话一个级别），**不是逐值污点追踪**。
    # 只升不降 —— 模型自己无法下调，唯一复位者是人的动作（CLI --clear-taint）。
    taint: str = TAINT_NONE

    # M3 接入 session 后设为回调；None 时事件只进内存列表
    emitter: Optional[Callable[[dict], None]] = None

    def raise_taint(self, level: str) -> str:
        """把污染级别抬到 `level`（取 max，**只升不降**）。返回抬升后的级别。

        不提供「降低」的同名接口是刻意的：如果有一个 `set_taint`，那么任何
        一段拿到 state 的代码（包括工具、hook、将来某个插件）都能把标记抹掉，
        于是这个标记就退化成一个"建议"。降级只有一条路 —— `clear_taint()`，
        而它只由 CLI 的人类操作调用。
        """
        self.taint = higher(self.taint, level)
        return self.taint

    def clear_taint(self, reason: str = "human") -> None:
        """复位污染标记。**只应由人的动作调用**（CLI `--clear-taint`）。

        恢复一个被误判的会话靠它，而不是靠模型自己辩解 —— 这正是「标记不由
        被标记者清除」的意思。
        """
        self.taint = TAINT_NONE
        self.record_event("taint_cleared", reason=reason)

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
