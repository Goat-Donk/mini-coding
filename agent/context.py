"""上下文管理（M3）——★ 差异化核心：provider-usage-first 记账 + cache-aware 布局。

移植 MiniCode 的 token-estimator 思路（assistant 消息附 usage，从尾部锚定
provider 真实用量 + 尾部估算），叠加我们独有的 cache-aware 布局目标：
- 稳定前缀（system + 记忆块 + 工具 schema）恒在最前不被 compact 破坏，
  最大化 DeepSeek 磁盘缓存命中。
- 分级告警：normal(<50%) / warning(≥50%) / critical(≥85%) / blocked(≥95%)。
- compact 流水线（M3-3）：确定性 snip（无 LLM）→ LLM 摘要（critical 才触发）。

记账锚点：
- loop 每次 llm.chat 后把 `result.usage` 存入 `state.last_usage`（provider 侧
  对"当时整个 prompt"的真实 token 数）。
- account()：`total = provider_usage.total_tokens + estimate(尾部新增消息)`；
  尾部 = 最近一次 assistant tool_calls 消息及其 tool 结果（compact 未运行时
  这就是上次调用后新增的全部内容）。
- compact 后置 `state.usage_stale_reason`，锚点失效 → 退回全量估算。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agent.llm import BaseLLM

# 每角色 字符/token 比例（MiniCode 同款；估算用，非精确）
_CHAR_PER_TOKEN = {
    "system": 3.5,
    "user": 3.0,
    "assistant": 3.5,
    "tool": 2.0,
}

# 分级阈值
WARNING_LEVELS = (
    ("normal", 0.5),
    ("warning", 0.85),
    ("critical", 0.95),
    ("blocked", 1.01),  # 终点哨兵，≥0.95 即 blocked
)

SNIP_BOUNDARY_MARKER = "[已裁剪的历史消息，见会话轨迹 JSONL]"
SUMMARY_MARKER = "[历史对话已摘要，完整轨迹见会话 JSONL]"

# 稳定前缀长度：messages[0] 恒为 system、messages[1] 恒为首条 user（任务）。
# compact 只删/改前缀之后的"中段"，永不触碰前缀（cache-aware 约定）。
_PREFIX_LEN = 2


@dataclass
class ContextStats:
    total_tokens: int
    provider_usage_tokens: int   # 最近一次 provider usage 的 total（锚点；无锚点为 0）
    estimated_tokens: int        # 尾部（或全量）估算
    utilization: float           # total / budget
    warning_level: str           # normal | warning | critical | blocked

    @staticmethod
    def level_of(utilization: float) -> str:
        for name, threshold in WARNING_LEVELS:
            if utilization < threshold:
                return name
        return "blocked"


def _message_text(message: dict) -> str:
    """提取消息的可计 token 文本：content + tool_calls 的 arguments。"""
    parts: list[str] = []
    content = message.get("content")
    if isinstance(content, str):
        parts.append(content)
    for call in message.get("tool_calls") or []:
        fn = call.get("function") or {}
        args = fn.get("arguments")
        if isinstance(args, str) and args:
            parts.append(args)
    return "\n".join(parts)


def build_compact_summary_prompt(conversation_text: str) -> list[dict]:
    """构造 LLM 摘要 compact 用的 messages（system 指令 + 待压缩对话文本）。

    压缩要求（TECH_SPEC §6.3）：保留任务目标、关键决策、错误与修复、未完成任务。
    """
    return [
        {
            "role": "system",
            "content": (
                "你是 CodeAgent 的上下文压缩器。请把给定的对话历史压缩成一段精炼的"
                "中文摘要，必须保留：当前任务目标、已经做出的关键决策、遇到的错误与修复、"
                "尚未完成的任务。只输出摘要本身，不要附加任何解释。"
            ),
        },
        {"role": "user", "content": conversation_text},
    ]


class ContextManager:
    """上下文记账 + 布局 + compact（M3-3 补 snip/LLM 摘要）。"""

    def __init__(
        self,
        llm: "BaseLLM",
        *,
        token_budget: int = 64_000,
        snip_threshold: float = 0.70,
        compact_threshold: float = 0.85,
        keep_recent: int = 12,
        min_keep: int = 6,
        snip_target: float = 0.60,
    ) -> None:
        self.llm = llm
        self.token_budget = token_budget
        self.snip_threshold = snip_threshold
        self.compact_threshold = compact_threshold
        self.keep_recent = keep_recent
        self.min_keep = min_keep
        self.snip_target = snip_target
        self.last_stats: ContextStats | None = None  # 控制台读它画指标

    # ---------- 估算 ----------

    def estimate_tokens(self, message: dict) -> int:
        text = _message_text(message)
        if not text:
            return 0
        ratio = _CHAR_PER_TOKEN.get(message.get("role"), 3.0)
        return max(1, int(len(text) / ratio))

    def estimate_messages(self, messages: list[dict]) -> int:
        return sum(self.estimate_tokens(m) for m in messages)

    # ---------- 记账（provider-usage-first） ----------

    def account(self, state) -> ContextStats:
        """按 6.2：有锚点 → provider total + 尾部估算；无锚点/已 stale → 全量估算。"""
        anchor = None if state.usage_stale_reason else getattr(state, "last_usage", None)
        if anchor is not None:
            tail = self._tail_after_last_assistant(state.messages)
            estimated = self.estimate_messages(tail)
            provider_usage_tokens = anchor.total_tokens
            total = provider_usage_tokens + estimated
        else:
            estimated = self.estimate_messages(state.messages)
            provider_usage_tokens = 0
            total = estimated
        utilization = total / self.token_budget if self.token_budget else 0.0
        return ContextStats(
            total_tokens=total,
            provider_usage_tokens=provider_usage_tokens,
            estimated_tokens=estimated,
            utilization=utilization,
            warning_level=ContextStats.level_of(utilization),
        )

    @staticmethod
    def _tail_after_last_assistant(messages: list[dict]) -> list[dict]:
        """尾部 = 最近一条 assistant 消息及其后的 tool 结果（上次调用的新增内容）。"""
        for idx in range(len(messages) - 1, -1, -1):
            if messages[idx].get("role") == "assistant":
                return messages[idx:]
        return messages

    # ---------- cache-aware 布局 + compact 流水线 ----------

    def prepare(self, state, tool_schemas: list[dict] | None = None) -> list[dict]:
        """M3 流水线入口：记账 → 分级 compact → 返回布局稳定的消息。

        两级 compact（TECH_SPEC §6.3，"先确定性后 LLM"）：
        - utilization ≥ 0.70（warning 带）→ 确定性 snip：中段删除 + boundary 标记，
          成本≈0；
        - utilization ≥ 0.85（critical/blocked）→ LLM 摘要 compact：中段压成摘要
          消息（信息保真更高），LLM 失败退化为 snip。
        cache-aware 约定：system + 任务恒在首位、compact 只动中段不碰前缀。
        """
        self.last_stats = self.account(state)
        if self.last_stats.utilization >= self.compact_threshold:
            self._compact_with_summary(state, state.messages)
        elif self.last_stats.utilization >= self.snip_threshold:
            self._snip(state, state.messages)
        return state.messages

    # ---------- 两级 compact ----------

    def _snip(self, state, messages: list[dict]) -> bool:
        """确定性裁剪：保留最近 keep_recent 条（轮次边界对齐），中段删除。"""
        cut = self._find_cut(messages)
        if cut is None:
            return False
        recent = messages[cut:]
        del messages[_PREFIX_LEN:cut]
        messages.insert(_PREFIX_LEN, {"role": "user", "content": SNIP_BOUNDARY_MARKER})
        state.usage_stale_reason = "snip_compact"  # 旧 provider usage 不再适配新上下文
        return True

    def _compact_with_summary(self, state, messages: list[dict]) -> bool:
        """LLM 摘要 compact：中段压成 context_summary 消息；LLM 失败退化 snip。"""
        cut = self._find_cut(messages)
        if cut is None:
            return False
        middle = messages[_PREFIX_LEN:cut]
        recent = messages[cut:]
        if not middle:
            return False
        try:
            summary = self.llm.complete(
                build_compact_summary_prompt(self._conversation_to_text(middle))
            )
        except Exception:
            return self._snip(state, messages)  # 摘要失败不阻塞任务
        summary = (summary or "").strip()
        if not summary:
            return self._snip(state, messages)

        marker = {"role": "user", "content": f"{SUMMARY_MARKER}\n{summary}"}
        del messages[_PREFIX_LEN:cut]
        messages.insert(_PREFIX_LEN, marker)
        state.usage_stale_reason = "llm_compact"
        return True

    def _find_cut(self, messages: list[dict]) -> int | None:
        """计算保留窗口起点（从尾部保留 keep_recent 条），并对齐 API 轮次边界。

        无效切割点两种（会破坏 OpenAI 消息格式）：
        - 前一条是 assistant 且带 tool_calls：它的 tool 结果在右侧，会孤儿化；
        - 首条是 tool 消息：它的 assistant 在左侧被删，tool 结果成孤儿。
        历史太短（窗口起点 ≤ min_keep，无中段可裁）或无合法边界 → 返回 None。
        """
        cut = len(messages) - self.keep_recent
        if cut <= self.min_keep:
            return None
        while cut > self.min_keep and not self._is_round_boundary(messages, cut):
            cut -= 1
        if not self._is_round_boundary(messages, cut):
            return None  # 保守放弃：找不到合法切割点
        return cut

    @staticmethod
    def _is_round_boundary(messages: list[dict], idx: int) -> bool:
        """idx 是否落在完整 API 轮次边界（不切开 assistant(tool_calls)+tool 整组）。"""
        if idx <= 0 or idx >= len(messages):
            return True
        prev = messages[idx - 1]
        if prev.get("role") == "assistant" and prev.get("tool_calls"):
            return False
        if messages[idx].get("role") == "tool":
            return False
        return True

    @staticmethod
    def _conversation_to_text(messages: list[dict]) -> str:
        """把中段消息拍平成可读文本（供摘要 prompt）。"""
        lines = []
        for message in messages:
            text = _message_text(message)
            if not text:
                continue
            lines.append(f"[{message.get('role')}] {text}")
        return "\n\n".join(lines)
