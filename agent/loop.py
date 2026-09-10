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
import sys
from typing import Callable

from agent.hooks import HookEngine
from agent.llm import BaseLLM, ToolCall, Usage
from agent.permissions import Decision, PermissionsEngine
from agent.security import TAINT_NONE
from agent.state import AgentState, assistant_tool_calls, system, tool_result, user
from agent.tool_result import ToolResultStore, compact_batch
from agent.tools.base import ToolContext, ToolRegistry, ToolResult


@dataclass
class RunResult:
    final_text: str | None
    steps: int
    usage: Usage
    events: list[dict]
    terminated_reason: str   # "completed" | "max_steps" | "loop_detected" | "error"
    task: str

    @property
    def taint(self) -> str:
        """本会话的污染级别。**派生自事件**，不是另一个要维护的字段。

        和 resume 时重算走的是同一个函数（`session.derive_taint`），所以
        「跑完的会话」与「resume 出来的同一会话」必然给出一样的答案 —— 而
        如果是各存一份，迟早会出现"轨迹说 high、返回值说 none"这种没人能解释的差异。
        """
        from agent.session import derive_taint

        return derive_taint(self.events) or TAINT_NONE


DEFAULT_SYSTEM_PROMPT = """\
你是 CodeAgent，一个在代码仓库内工作的 AI 编程代理。你的目标是高效、正确地完成用户任务。

工作目录（先读这段，能省掉大量试错）：
- 你的工作目录（沙箱根）就是：{workspace_root}
- 所有工具的相对路径都以它为基准 —— bash 的 cwd 已经设在里面，read/write/edit 的 path
  也按它解析。所以直接用相对路径即可：`python -m pytest tests/ -q`、path="src/app.py"。
- 不要猜工作目录叫什么，不要假设 /workspace、~/project 之类的路径存在，也不要为了找文件
  去遍历磁盘。需要确认位置时用 `pwd`（Windows 用 `cd`）看一眼，一次就够。
- 当前平台：{platform}

工作方式：
- 先用工具探索（glob/grep/read）理解代码，再动手修改；不要臆测文件内容。
- 修改代码前，如果任务复杂，先用 3~6 步的简短计划（仅步骤与验证方式，不要冗长）。
- 小改动用 edit（精确匹配），大改动用 write；完成后运行测试验证。
- 每条消息最多做必要的工具调用；工具失败时根据错误信息自行修复后重试。
- 工具返回里带 `[exit code: N]`，N≠0 表示命令没成功 —— 先看错误信息定位原因，不要重复同样的调用。

硬性约束：
- 只能在工作目录（沙箱）内操作，禁止访问沙箱外路径。
- 禁止执行危险命令（rm -rf、git push 等被工具拒绝）。
- 完成任务后：输出最终结论（做了什么、验证结果如何）。
- 不要假装完成：必须用测试/命令实际验证，验证失败要报告。
- 不要无意义重复同一操作；若连续多次得到相同失败，停下来向用户说明。

{repo_memory_block}"""


def _platform_hint() -> str:
    """告诉模型当前平台的命令行语法，避免 Unix/Windows 命令混用。"""
    if sys.platform == "win32":
        return (
            "Windows（bash 工具实际用 `cmd /c` 执行，请用 Windows 语法："
            "dir /b、cd /d、type、findstr；不要用 ls / find / grep / cat）。"
            "注意：`pwd` 在 Windows 上会被解析成 Git for Windows 自带的 pwd.exe，"
            "返回的是 `/d/xxx` 这种 POSIX 路径、不是有效的 Windows 路径 —— "
            "要确认当前目录请用 `cd`（不带参数）"
        )
    return "POSIX（bash 工具用 `bash -lc` 执行，可用 ls / find / grep / cat）"



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
        hooks: HookEngine | None = None,               # M2 hooks 引擎
        tool_result_store: ToolResultStore | None = None,  # M3 超大结果落盘
        memory_blocks: list[str] | None = None,    # M4 注入
        context: object | None = None,             # M3 接 ContextManager
        session: object | None = None,             # M3 接 session（轨迹/检查点）
        on_event: Callable[[dict], None] | None = None,  # 实时事件回调（CLI 流式输出）
    ) -> None:
        self.llm = llm
        self.registry = registry
        self.workspace_root = Path(workspace_root).resolve()
        self.max_steps = max_steps
        self.loop_detection_window = max(1, loop_detection_window)
        self.empty_response_retries = max(0, empty_response_retries)
        self.permissions = permissions
        self.hooks = hooks
        self.tool_result_store = tool_result_store
        # 按 session_id 缓存的落盘 store（见 _store_for）；只有显式传了
        # tool_result_store 之外的情况才用它，避免同引擎跑多会话时串目录
        self._stores: dict[str, ToolResultStore] = {}
        self.memory_blocks = list(memory_blocks or [])
        self.context = context
        self.session = session
        self.on_event = on_event
        self.system_prompt = system_prompt or DEFAULT_SYSTEM_PROMPT

    def run(self, task: str, *, cwd: Path | None = None) -> RunResult:
        """新建会话状态并跑任务。有 session 时用其 session_id（轨迹/检查点归属）。"""
        if self.memory_blocks:
            memory_block = "\n".join(f"- {block}" for block in self.memory_blocks)
        else:
            memory_block = ""
        sys_prompt = self._render_system_prompt(memory_block)

        # 有 session 时用其 session_id（轨迹/检查点归属）；_EmitProxy 之类
        # 只实现 emit 的轻量替身没有 session_id，兜底 "m1"
        session_id = getattr(self.session, "session_id", "m1")
        state = AgentState(
            session_id=session_id,
            task=task,
            system_prompt=sys_prompt,
            messages=[system(sys_prompt), user(task)],
        )
        return self._run_loop(state, task, cwd)

    def run_from(self, state: AgentState, *, cwd: Path | None = None) -> RunResult:
        """从检查点恢复的 state 继续执行（Session.resume 配合用）。

        step 计数不重置：续跑继续推进步数，新检查点不会覆盖恢复前的同名文件。
        """
        return self._run_loop(state, state.task, cwd)

    def _run_loop(self, state: AgentState, task: str, cwd: Path | None) -> RunResult:
        """共享主循环：think → 调工具 → 看结果 → ... → 完成。"""
        state.emitter = self._make_emitter()

        ctx = ToolContext(
            workspace_root=self.workspace_root,
            cwd=cwd,
            permissions=self.permissions,
            hooks=self.hooks,
        )
        signatures_window: deque[list[str]] = deque(maxlen=self.loop_detection_window)
        empty_retry_count = 0

        try:
            while state.step < self.max_steps:
                messages = self._prepare_messages(state)
                result = self.llm.chat(messages, self.registry.schemas())
                state.usage += result.usage
                state.last_usage = result.usage  # M3 provider 锚点
                state.usage_stale_reason = None  # 新 usage 锚定当前（compact 后）上下文
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
                if self.session is not None:
                    self.session.checkpoint(state)  # M3-4 每 N 步落盘检查点
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

    def _make_emitter(self) -> Callable[[dict], None] | None:
        """组合事件出口：session 落盘（JSONL） + 外部实时回调（CLI 流式）。

        两者可以同时存在；都没有时返回 None（事件只留在 state.events 里）。
        """
        session_emit = getattr(self.session, "emit", None) if self.session is not None else None
        on_event = self.on_event
        if session_emit is not None and on_event is not None:
            def emit_both(event: dict) -> None:
                session_emit(event)
                on_event(event)
            return emit_both
        return on_event if on_event is not None else session_emit

    def _render_system_prompt(self, memory_block: str) -> str:
        """注入运行期槽位（工作目录 / 平台 / 记忆块）。

        用逐个 replace 而不是 str.format：自定义 system_prompt 里可能含其它花括号
        （JSON 示例、代码片段），format 会直接抛 KeyError。
        """
        slots = {
            "workspace_root": str(self.workspace_root),
            "platform": _platform_hint(),
            "repo_memory_block": memory_block,
        }
        prompt = self.system_prompt
        for name, value in slots.items():
            token = "{" + name + "}"
            if token in prompt:
                prompt = prompt.replace(token, value)
        return prompt

    def _prepare_messages(self, state: AgentState) -> list[dict]:
        """M1：原样返回；M3 起由 ContextManager 做 cache-aware 布局 + compact。"""
        if self.context is not None:
            return self.context.prepare(state)
        return state.messages

    def _store_for(self, state: AgentState) -> ToolResultStore | None:
        """解析本次运行该用的工具结果落盘 store（M3-2 的接线兜底）。

        **为什么要有这个方法**：`tool_result_store` 是构造参数，而三个入口
        （`app/cli.py`、`app/ui_streamlit.py`、`eval/runner.py`）**全都没传它**
        —— 于是 `compact_batch` 在生产路径上从未执行过：机制写完、文档写了、
        测试绿了，但没人接上。这与项目已经修过两次的接线漂移（hooks 曾整体
        漏接 CLI、CLI 曾漏接 permissions）是同一个形状的第三次。

        所以把「谁来构造 store」收进引擎自己：显式传入优先（测试与将来需要
        自定义目录的调用方），否则**有 session 就自动按 session 落盘**。构造点
        只有一处，不会再出现"一个入口接了、另一个没接"。

        `eval/runner.py` 不建 Session → 这里返回 None → eval 不落盘，
        行为与既有的完成率/token 数字**保持不变**（这是刻意的，不是遗漏）。

        按 session_id 缓存而不是每次新建：`ToolResultStore._written` 记住
        「同 id 同内容不重复写盘」，跨步复用才有意义；每步新建会退化成每步重写。
        """
        if self.tool_result_store is not None:
            return self.tool_result_store
        if self.session is None:
            return None
        sid = state.session_id
        if sid not in self._stores:
            self._stores[sid] = ToolResultStore(self.workspace_root, sid)
        return self._stores[sid]

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
            result = self._gate_and_run(call, state, ctx)
            state.record_event(
                "tool_call",
                name=call.name,
                arguments=call.arguments,
                success=result.success,
                duration_ms=result.duration_ms,
                # bash 等工具的进程退出码：success 只表示"工具跑完了"，
                # 退出码才反映命令本身成没成（CLI 用它把 ✓ 显示得更诚实）
                exit_code=result.data.get("exit_code") if isinstance(result.data, dict) else None,
            )
            return call.id, result.output

        if all_read_only and len(calls) > 1:
            with ThreadPoolExecutor(max_workers=min(4, len(calls))) as executor:
                # map() 保持输入顺序 → tool 结果消息顺序与 calls 一一对应（id 匹配）
                results = list(executor.map(invoke, calls))
        else:
            results = [invoke(call) for call in calls]

        # M3-2：超大工具结果落盘（上下文替换为预览+路径，避免截断丢信息）
        store = self._store_for(state)
        if store is not None:
            results = compact_batch(results, store)

        # 消息追加：一条 assistant tool_calls + N 条 tool 结果（顺序与 calls 对应）
        state.messages.append(assistant_tool_calls(calls))
        for tool_call_id, output in results:
            state.messages.append(tool_result(tool_call_id, output))

    def _sync_taint(self, state: AgentState) -> None:
        """把 state 的污染标记镜像进权限引擎。

        单向、幂等：`state.taint` 是权威（只升不降，只有人的动作能复位），
        引擎只是照着设值。放在每次门禁判定的开头而不是「run_post 之后」，
        是为了让「谁在什么时候抬的标记」不影响结果 —— 无论标记来自检测 hook、
        来自 resume 出来的检查点，还是来自 `--clear-taint`，下一步判定都看得到。
        """
        if self.permissions is not None and hasattr(self.permissions, "note_taint"):
            self.permissions.note_taint(state.taint)

    def _gate_block(
        self, state: AgentState, call: ToolCall, source: str, message: str, **extra
    ) -> ToolResult:
        """门禁阻断：**先记事件，再返回失败**。

        原先这几处只 `return ToolResult.fail(...)`，于是轨迹里只剩 `success=False`
        —— 事后翻轨迹只看到「工具没成」，分不清是权限拒绝、hook 阻断、工具自己报错
        还是模型编了个不存在的工具名。`gate_block` 把 `source`（哪一层拦的）与
        `reason`（为什么）写进事件，让拒绝**第一次在轨迹里可见**，也能与
        `security_finding` 这类上游事件串成因果链。
        """
        state.record_event(
            "gate_block", tool=call.name, source=source, reason=message, **extra
        )
        return ToolResult.fail(message)

    def _gate_and_run(
        self, call: ToolCall, state: AgentState, ctx: ToolContext
    ) -> ToolResult:
        """工具调用门禁链：hooks(block-at-submit) → permissions(ask/deny) → 执行 → post hooks。"""
        tool = self.registry.get(call.name) if call.name in self.registry else None
        if tool is None:
            available = ", ".join(self.registry.names())
            return self._gate_block(
                state, call, "registry", f"未知工具 {call.name}，可用工具: {available}"
            )

        # 1) PreToolUse hooks：阻断则拦截（信息回喂模型自修复）
        if self.hooks is not None:
            block = self.hooks.run_pre(call.name, call.arguments, state)
            if block is not None:
                msg = f"[hook 阻断] {block.reason}"
                if block.hint:
                    msg += f"\n提示: {block.hint}"
                return self._gate_block(state, call, "hooks", msg)

        # 2) 权限：deny 拒绝；ask 无确认交互时安全默认拒绝（M2-3 起控制台接入）
        if self.permissions is not None:
            # 先同步污染标记，再判定：标记的权威在 state（只升不降），引擎只是
            # 照它设值。放在判定**之前**，才能保证「上一步抬的标记、这一步就生效」。
            self._sync_taint(state)
            decision = self.permissions.check(call.name, call.arguments, ctx)
            if decision is Decision.DENY:
                return self._gate_block(
                    state,
                    call,
                    "permissions",
                    f"权限拒绝: 未获允许执行 {call.name}"
                    f"（{self.permissions.describe(call.name, call.arguments)}）",
                )
            if decision is Decision.ASK:
                # 拒绝必须带**出处与解除方式**：只说"没权限"会让 agent 反复重试
                # 同一个调用、让用户不知道该改哪个文件。
                hint = (
                    self.permissions.denial_hint(call.name, call.arguments)
                    if hasattr(self.permissions, "denial_hint")
                    else None
                )
                msg = f"权限拒绝: {call.name} 需要人工确认，当前无确认交互，已按拒绝处理"
                if hint:
                    msg += f"\n出处: {hint}"
                return self._gate_block(state, call, "permissions", msg)

        # 3) 执行
        result = tool.run(call.arguments, ctx)

        # 4) PostToolUse hooks（观察/提示，非阻断）
        #    返回值必须拼进 output：这里原先丢弃了 hints，导致 PostToolUse 这一层
        #    治理**从未到达模型**（hook 跑了，但它的观察结论没人看）。hints 是回喂给
        #    模型的观察信息（如「测试通过 → marker 已写入，commit 已解锁」），
        #    不改变 success —— 提示不是结论。
        if self.hooks is not None:
            hints = self.hooks.run_post(call.name, call.arguments, result, state)
            if hints:
                result.output = result.output + "\n" + "\n".join(hints)
        return result
