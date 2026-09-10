"""QueryEngine —— agent 查询循环（项目心脏）。

流程：think → 调工具 → 看结果 → 再想 → ... → 完成。对应 Claude Code 的
QueryEngine.ts / EvoAgent 的 BoundedRole / offer-Master 的 LoopAgentController：
都是"运行时拥有循环、模型只输出动作"。

三大坍缩防护：max_steps 硬上限 + system prompt 硬约束 + 循环检测（signature 窗口）。

★ 差异化：只读工具并发执行（CC 特有，串行参考项目没有）；可写工具串行保持顺序。
"""
from __future__ import annotations

from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from agent.llm import BaseLLM, ToolCall, Usage
from agent.permissions import Decision, PermissionsEngine
from agent.state import AgentState, assistant_tool_calls, system, tool_result, user
from agent.tools.base import ToolContext, ToolRegistry, ToolResult


@dataclass
class RunResult:
    final_text: str | None
    steps: int
    usage: Usage
    events: list[dict]
    terminated_reason: str   # "completed" | "max_steps" | "loop_detected" | "error"
    task: str


DEFAULT_SYSTEM_PROMPT = """\
你是 CodeAgent，一个在代码仓库内工作的 AI 编程代理。你的目标是高效、正确地完成用户任务。

工作方式：
- 先用工具探索（glob/grep/read）理解代码，再动手修改；不要臆测文件内容。
- 修改代码前，如果任务复杂，先用 3~6 步的简短计划（仅步骤与验证方式，不要冗长）。
- 小改动用 edit（精确匹配），大改动用 write；完成后运行测试验证。
- 每条消息最多做必要的工具调用；工具失败时根据错误信息自行修复后重试。

硬性约束：
- 只能在工作目录（沙箱）内操作，禁止访问沙箱外路径。
- 禁止执行危险命令（rm -rf、git push 等被工具拒绝）。
- 完成任务后：输出最终结论（做了什么、验证结果如何）。
- 不要假装完成：必须用测试/命令实际验证，验证失败要报告。
- 不要无意义重复同一操作；若连续多次得到相同失败，停下来向用户说明。

{repo_memory_block}"""


class QueryEngine:
    def __init__(
        self,
        llm: BaseLLM,
        registry: ToolRegistry,
        *,
        workspace_root: Path,
        system_prompt: str | None = None,          # None → 内置默认
        max_steps: int = 25,
        loop_detection_window: int = 4,            # 最近 N 步工具签名相同即停
        empty_response_retries: int = 2,           # 空响应重试上限（M1-9）
        permissions: PermissionsEngine | None = None,  # M2 权限引擎
        memory_blocks: list[str] | None = None,    # M4 注入
        context: object | None = None,             # M3 接 ContextManager
        session: object | None = None,             # M3 接 session（轨迹/检查点）
    ) -> None:
        self.llm = llm
        self.registry = registry
        self.workspace_root = Path(workspace_root).resolve()
        self.max_steps = max_steps
        self.loop_detection_window = max(1, loop_detection_window)
        self.empty_response_retries = max(0, empty_response_retries)
        self.permissions = permissions
        self.memory_blocks = list(memory_blocks or [])
        self.context = context
        self.session = session
        self.system_prompt = system_prompt or DEFAULT_SYSTEM_PROMPT

    def run(self, task: str, *, cwd: Path | None = None) -> RunResult:
        if self.memory_blocks:
            memory_block = "\n".join(f"- {block}" for block in self.memory_blocks)
        else:
            memory_block = ""
        # 自定义 system_prompt 里可能没有占位符 → 只在有占位符时注入
        if "{repo_memory_block}" in self.system_prompt:
            sys_prompt = self.system_prompt.format(repo_memory_block=memory_block)
        else:
            sys_prompt = self.system_prompt

        state = AgentState(
            session_id="m1",
            task=task,
            system_prompt=sys_prompt,
            messages=[system(sys_prompt), user(task)],
        )
        if self.session is not None:
            state.emitter = getattr(self.session, "emit", None)

        ctx = ToolContext(
            workspace_root=self.workspace_root, cwd=cwd, permissions=self.permissions
        )
        signatures_window: deque[list[str]] = deque(maxlen=self.loop_detection_window)
        empty_retry_count = 0

        try:
            while state.step < self.max_steps:
                messages = self._prepare_messages(state)
                result = self.llm.chat(messages, self.registry.schemas())
                state.usage += result.usage
                state.step += 1
                state.record_event(
                    "llm_call",
                    tool_calls=[call.signature() for call in result.tool_calls],
                    usage={
                        "prompt_tokens": result.usage.prompt_tokens,
                        "completion_tokens": result.usage.completion_tokens,
                        "cache_hit_tokens": result.usage.prompt_cache_hit_tokens,
                        "cache_miss_tokens": result.usage.prompt_cache_miss_tokens,
                    },
                )

                if not result.tool_calls:
                    # 空响应恢复（M1-9）：无工具调用且内容空白 → push continuation prompt 重试
                    if not (result.content or "").strip() and empty_retry_count < self.empty_response_retries:
                        empty_retry_count += 1
                        state.record_event(
                            "empty_response_retry",
                            attempt=empty_retry_count,
                            limit=self.empty_response_retries,
                        )
                        state.messages.append(
                            user("上次返回为空，继续完成下一步或给出最终结论")
                        )
                        continue
                    state.terminated_reason = "completed"
                    return RunResult(
                        final_text=result.content,
                        steps=state.step,
                        usage=state.usage,
                        events=state.events,
                        terminated_reason="completed",
                        task=task,
                    )

                # 循环检测：窗口内每次的工具调用集合是否完全一致
                signatures = sorted(call.signature() for call in result.tool_calls)
                signatures_window.append(signatures)
                if (
                    len(signatures_window) == self.loop_detection_window
                    and len(set(map(tuple, signatures_window))) == 1
                ):
                    state.terminated_reason = "loop_detected"
                    return RunResult(
                        final_text=(
                            f"检测到重复循环（连续 {self.loop_detection_window} 次相同工具调用），"
                            "任务已中止。请检查任务是否可行，或补充更多信息。"
                        ),
                        steps=state.step,
                        usage=state.usage,
                        events=state.events,
                        terminated_reason="loop_detected",
                        task=task,
                    )

                self._execute_tool_calls(result.tool_calls, state, ctx)
        except Exception as exc:
            # 意外错误：不回吐，给用户一个明确的失败结论（不假装成功）
            state.terminated_reason = "error"
            return RunResult(
                final_text=f"执行出错: {type(exc).__name__}: {exc}",
                steps=state.step,
                usage=state.usage,
                events=state.events,
                terminated_reason="error",
                task=task,
            )

        state.terminated_reason = "max_steps"
        return RunResult(
            final_text=f"已达到最大步数（{self.max_steps}），任务未完成。请拆分子任务或补充信息。",
            steps=state.step,
            usage=state.usage,
            events=state.events,
            terminated_reason="max_steps",
            task=task,
        )

    # ---------- 内部 ----------

    def _prepare_messages(self, state: AgentState) -> list[dict]:
        """M1：原样返回；M3 起由 ContextManager 做 cache-aware 布局 + compact。"""
        if self.context is not None:
            return self.context.prepare(state)
        return state.messages

    def _execute_tool_calls(
        self, calls: list[ToolCall], state: AgentState, ctx: ToolContext
    ) -> None:
        """执行一轮工具调用。全只读 → 并发（map 保序）；含可写 → 串行。"""
        all_read_only = all(
            self.registry.get(call.name).is_read_only() if call.name in self.registry else False
            for call in calls
        )

        def invoke(call: ToolCall) -> tuple[str, str]:
            """返回 (tool_call_id, output)。单条调用无论成败都封包成文本回喂模型。"""
            tool = self.registry.get(call.name) if call.name in self.registry else None
            if tool is None:
                available = ", ".join(self.registry.names())
                result = ToolResult.fail(f"未知工具 {call.name}，可用工具: {available}")
            elif self.permissions is not None:
                decision = self.permissions.check(call.name, call.arguments, ctx)
                if decision is Decision.DENY:
                    result = ToolResult.fail(
                        f"权限拒绝: 未获允许执行 {call.name}（{self.permissions.describe(call.name, call.arguments)}）"
                    )
                elif decision is Decision.ASK:
                    # headless 无确认回调 → 安全默认拒绝（M2-3 控制台接入交互后走 ask）
                    result = ToolResult.fail(
                        f"权限拒绝: {call.name} 需要人工确认，当前无确认交互，已按拒绝处理"
                    )
                else:
                    result = tool.run(call.arguments, ctx)
            else:
                result = tool.run(call.arguments, ctx)
            state.record_event(
                "tool_call",
                name=call.name,
                arguments=call.arguments,
                success=result.success,
                duration_ms=result.duration_ms,
            )
            return call.id, result.output

        if all_read_only and len(calls) > 1:
            with ThreadPoolExecutor(max_workers=min(4, len(calls))) as executor:
                # map() 保持输入顺序 → tool 结果消息顺序与 calls 一一对应（id 匹配）
                results = list(executor.map(invoke, calls))
        else:
            results = [invoke(call) for call in calls]

        # 消息追加：一条 assistant tool_calls + N 条 tool 结果（顺序与 calls 对应）
        state.messages.append(assistant_tool_calls(calls))
        for tool_call_id, output in results:
            state.messages.append(tool_result(tool_call_id, output))
