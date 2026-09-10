"""上下文管理（M3）——★ 差异化核心：provider-usage-first 记账 + cache-aware 布局。

移植 MiniCode 的 token-estimator 思路（assistant 消息附 usage，从尾部锚定
provider 真实用量 + 尾部估算），叠加我们独有的 cache-aware 布局目标：
- 稳定前缀（system + 记忆块 + 工具 schema）恒在最前不被 compact 破坏，
  最大化 DeepSeek 磁盘缓存命中。
- 分级告警：normal(<50%) / warning(≥50%) / critical(≥85%) / blocked(≥95%)。
- compact 流水线（M3-3，M8 加第 0 级）：分级截断（只缩 tool 正文）→ 确定性 snip
  （无 LLM，删中段）→ LLM 摘要（critical 才触发）。顺序即"代价从低到高"。

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

# ---------- 分级截断（compact 的第 0 级） ----------

#: 截断标记。**下一轮见到它就知道这条已经缩过了** —— 幂等的判据是这个字符串，
#: 不是"长度有没有变小"（后者在内容恰好等于额度时会把同一条反复缩）。
TRUNCATED_MARK = "[... 省略 {n} 字符 ...]"

#: 头尾比例：只留头部是错的。`grep`/`pytest` 这类输出的**结论在尾部**
#: （失败摘要、`N passed` 总计行、`[exit code]`），只留头等于把最该看的丢掉。
TRUNCATE_HEAD_RATIO = 0.7

#: 单条 tool 结果的截断额度（字符），按**工具**分级。
#: 依据是"这条输出还能不能重新取一次"：read/grep/glob 的探索型输出可以再调一次
#: 工具拿到（而且往往只需要其中一段），额度给小；bash 的输出常含测试摘要，给中。
TRUNCATE_BUDGETS = {
    "read": 4_000,
    "grep": 3_000,
    "glob": 2_000,
    "bash": 6_000,
    "default": 4_000,
}

#: 失败结果的额度，**统一放大**。理由：失败原文是模型自修复的唯一线索
#: （TECH_SPEC 的「失败一律文本回喂」原则），把它缩掉会让 agent 反复撞同一面墙。
FAILURE_BUDGET = 16_000

#: 判定"这条是不是失败结果"的文案前缀。
#:
#: 这里必须说清楚一件事：**OpenAI 的消息格式里 tool 消息没有 success 字段**，
#: 成功与否只体现在文本上。所以判据只能是这些前缀 —— 它们全部由 gate 链
#: （`ToolResult.fail` / `loop._gate_block`）产出，是一组封闭的、可枚举的文案。
#:
#: 判据刻意写**宽**（多认几种失败文案）：漏判的后果是"一条错误信息被按成功额度
#: 截断"，而模型看不出自己看到的不是全部错误；误判的后果只是少省一点 token。
#: 两个方向的代价不对等，所以往安全的那边偏。
_FAILURE_PREFIXES = (
    "工具执行失败",
    "权限拒绝",
    "未知工具",
    "[hook 阻断]",
    "本轮因等待用户输入而跳过",
)

#: 缩不得的占位文本（**路径指针**）。把它缩掉就毁了回读能力，等于废掉
#: `tool_result.py` 落盘的全部意义 —— 它整个机制就是为了"别丢信息"。
#: 只认这两种尖括号/方括号形式；不把 SNIP/SUMMARY 标记也算进来，是因为它们
#: 出现在 user 角色消息上（本函数只看 tool 消息），且 `read` 一个源码文件时
#: 正文里可能**合法地**含有那些字面量，按包含关系判断会误伤。
SKIP_TRUNCATE_MARKERS = ("<persisted-output>", "[... 省略")


# 稳定前缀长度：messages[0] 恒为 system、messages[1] 恒为首条 user（任务）。
# compact 只删/改前缀之后的"中段"，永不触碰前缀（cache-aware 约定）。
#
# 由此引出的一条规则，写在最容易被违反的地方：**`messages[0]` 必须在一次会话内
# 逐字节不变**。它承载 system prompt，而 system 里有若干"运行期槽位"（工作目录、
# 平台、记忆块、skills 索引）。这些只能在**开局渲染一次**并随 `state.system_prompt`
# 进检查点 —— 一旦有人图省事在每轮重新组装（或每轮重新扫描 skills），前缀就会
# 每轮变一次，而后缀全部重算：缓存命中率会掉，但**不会报任何错**，表现只是"账单
# 变贵了"。`--resume` 用的也是会话当初那份 system（从检查点读回来），这正是我们要的语义。
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

        三级 compact（"先缩内容、缩不够再删、删不够再摘要"）：
        - utilization ≥ 0.70（warning 带）→ **第 0 级：分级截断**（只改 tool 消息的
          正文，不删消息）；缩到线下就返回，这一级不进就是最便宜的一手；
        - 仍 ≥ 0.70 → 确定性 snip：中段删除 + boundary 标记，成本≈0；
        - ≥ 0.85（critical/blocked）→ LLM 摘要 compact：中段压成摘要消息
          （信息保真更高），LLM 失败退化为 snip。
        cache-aware 约定：system + 任务恒在首位、compact 只动中段不碰前缀。

        **第 0 级只在越过 0.70 时才跑**，这是刻意的、也是必须守住的：截断会改
        中段某个 tool 消息 → 那条之后的**前缀缓存全部失效**（DeepSeek 是前缀匹配）。
        每步都跑的话，每个大工具结果在它被 snip 掉之前至少破坏一次缓存，而收益
        只是几个百分点的 utilization —— 净亏。**而这条规则一旦被违反，不会有任何
        报错**：表现只是缓存命中率曲线不再上升。所以下面那条 `if` 是一条不变量，
        不是优化开关。
        """
        self.last_stats = self.account(state)
        if self.last_stats.utilization >= self.snip_threshold:
            freed = self._truncate_oversized(state)
            if freed:
                self.last_stats = self.account(state)
                if self.last_stats.utilization < self.snip_threshold:
                    # 缩到线下 → 不删消息（保住 tool_call_id 配对与大部分内容）
                    state.usage_stale_reason = "tool_output_truncated"
                    return state.messages
                # 缩了但不够 → 继续走 snip/摘要（截断结果保留，不白做）
        if self.last_stats.utilization >= self.compact_threshold:
            self._compact_with_summary(state, state.messages)
        elif self.last_stats.utilization >= self.snip_threshold:
            self._snip(state, state.messages)
        return state.messages

    # ---------- 第 0 级：分级截断 ----------

    def _truncate_oversized(self, state) -> int:
        """把中段超额的 tool 消息正文缩成 head 70% + tail 30%，返回释放的估算 token。

        为什么改正文而不是删消息（与 snip 的区别，也是它更便宜的原因）：
        - **消息不删** → `tool_call_id` 配对天然完整，`_find_cut` 要处理的两类
          孤儿边界问题（assistant(tool_calls) 与它右侧的 tool 结果被切开）在这里
          根本不存在；
        - 保留头尾 → "这条命令跑没跑成、最后报了什么"仍在；
        - 动作范围小 → 缓存失效的起点更靠后。

        不碰两处：前缀（`_PREFIX_LEN`）与最近 `keep_recent` 条 —— 模型正在用的
        结果不能动它，否则它刚读到一半的内容下一轮就变了（比截断更糟的是**悄悄变了**）。
        """
        messages = state.messages
        end = len(messages) - self.keep_recent
        start = max(_PREFIX_LEN, self.min_keep)
        if end <= start:
            return 0

        names = self._tool_names(messages)
        freed = 0
        for idx in range(start, end):
            message = messages[idx]
            if message.get("role") != "tool":
                continue
            content = message.get("content")
            if not isinstance(content, str) or not content:
                continue
            if any(marker in content for marker in SKIP_TRUNCATE_MARKERS):
                continue
            tool_name = names.get(message.get("tool_call_id"), "")
            budget = self._truncate_budget(tool_name, content)
            if len(content) <= budget:
                continue
            before = self.estimate_tokens(message)
            message["content"] = self._head_tail(content, budget)
            freed += before - self.estimate_tokens(message)
        return freed

    @staticmethod
    def _tool_names(messages: list[dict]) -> dict[str, str]:
        """`tool_call_id → 工具名`（从 assistant 消息的 tool_calls 反查）。

        一次性建表而不是每条 tool 消息回扫一遍：回扫是 O(n²)，而 prepare 每步都跑。
        """
        names: dict[str, str] = {}
        for message in messages:
            for call in message.get("tool_calls") or []:
                call_id = call.get("id")
                fn = call.get("function") or {}
                if call_id and fn.get("name"):
                    names[call_id] = fn["name"]
        return names

    @staticmethod
    def _truncate_budget(tool_name: str, content: str) -> int:
        """按 [失败 | 工具类别] 取额度。失败优先 —— 见 `_FAILURE_PREFIXES` 的说明。"""
        if content.startswith(_FAILURE_PREFIXES):
            return FAILURE_BUDGET
        return TRUNCATE_BUDGETS.get(tool_name, TRUNCATE_BUDGETS["default"])

    @staticmethod
    def _head_tail(text: str, budget: int) -> str:
        """head 70% + 省略标记 + tail 30%。省略标记写明**省了多少字符** ——
        只说"已省略"模型无法判断要不要重新取一次，说了数字它就能决定。"""
        head = int(budget * TRUNCATE_HEAD_RATIO)
        tail = budget - head
        omitted = len(text) - budget
        middle = TRUNCATED_MARK.format(n=omitted)
        if tail <= 0:
            return text[:head] + "\n" + middle + "\n"
        return text[:head] + "\n" + middle + "\n" + text[-tail:]

    # ---------- 第 1/2 级 compact ----------

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
