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

from agent.goal import (
    CHECK_FAILED,
    CHECK_INVALID,
    CHECK_PASSED,
    GOAL_CHECK_TIMEOUT,
    GOAL_DONE,
    render_check_report,
    tail_lines,
)
from agent.hooks import HookEngine
from agent.llm import BaseLLM, ToolCall, Usage
from agent.permissions import Decision, PermissionsEngine
from agent.security import TAINT_NONE
from agent.state import (
    AgentState,
    assistant_tool_calls,
    ensure_tool_pairing,
    system,
    tool_result,
    user,
)
from agent.subagents import AbortToken, AgentWorkers
from agent.tool_result import ToolResultStore, compact_batch
from agent.tools.base import Tool, ToolContext, ToolRegistry, ToolResult


#: 目标（M9-6）与取消（M9-7）相关的终止原因。
#:
#: 前缀 `REASON_` 是**刻意**的：`agent/goal.py` 里 `GOAL_DONE` 是**状态**（"done"），
#: 这里是**终止原因**（"goal_done"），两个词都在本模块被 import —— 同名会让
#: 读代码的人以为它们是同一个东西。（既有的五个终止原因保持字面量不动：那是纯
#: 改名扫荡，会把"加一个功能"变成"加功能 + 重构核心循环"，而五个返回点正是
#: 变异体最常锚的地方。）
REASON_GOAL_DONE = "goal_done"
REASON_GOAL_CHECK_INVALID = "goal_check_invalid"

#: 子代理被取消（M9-7）。**它是 worker 专有的，父引擎结构上拿不到。**
#:
#: 父引擎的取消路径是 Ctrl+C —— 那是 `BaseException`，在 `llm.chat` 里就抛出去
#: 了，走不到 `_run_loop` 的任何返回点（父会话被中断的终止原因是 `"interrupted"`，
#: 由 `app/repl.py` 的 `run_turn` 在 `except KeyboardInterrupt` 里给，不是这里）。
#: 只有 worker 引擎装了 `abort` token，所以只有它会返回这个值。
#:
#: 那为什么还要让 `app/repl.py` 的 `BURST_STOP_REASONS` / `_STOP_REASONS` 覆盖它？
#: 因为 `tests/test_goal.py` 有两道方程逼着那个集合等于本集合 —— **那是它们该做
#: 的事**（"新增终止原因却忘了让自动推进停下来"必须变红）。所以那两条在 REPL 里
#: 是"方程要求它存在、但结构上不可达"的条目，如实记着，而不是假装它会在那儿发生。
REASON_ABORTED = "aborted"

#: 一轮 `_run_loop` 可能给出的**全部**终止原因。
#:
#: 存在的理由是一个真实的缺口：原先这些值只散落在各个返回点上，没有任何
#: 地方列举它们。于是"新增一个终止原因、但忘了让 REPL 的自动推进在它上面停
#: 下来"不会有任何东西变红 —— 表现只是 burst 白烧预算（`tests/test_goal.py`
#: 有一条测试用 `BURST_STOP_REASONS` 钉住这个集合，那是它的第二个读者）。
TERMINATED_REASONS = frozenset({
    "completed", "max_steps", "loop_detected", "error", "await_user",
    REASON_GOAL_DONE, REASON_GOAL_CHECK_INVALID, REASON_ABORTED,
})

#: 被取消时回填给模型的工具结果文本。**必须占位、不能省**：`assistant_tool_calls`
#: 在批末一次性 append 覆盖**全部** `calls`，少一条对应的 `tool` 消息就是孤儿 id，
#: 端点直接 400 —— 而报错发生在**下一次请求**时，看起来与这次取消毫无关系
#: （同 `state.PAIRING_FILLER` / `loop` 里 ask_user 跳过时的回填，同一个理由）。
#: 文案与 `PAIRING_FILLER` 一样要说实话：这次调用**没有执行**。
ABORTED_TOOL_OUTPUT = (
    "[已取消] 这条工具调用没有执行 —— 子代理被要求停止，"
    "在它之前就结束了本回合。如需它的结果，请重新派发。"
)


@dataclass
class RunResult:
    final_text: str | None
    steps: int
    usage: Usage
    events: list[dict]
    terminated_reason: str   # 取值见 loop.TERMINATED_REASONS（含 "completed" / "await_user" / …）
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
- 任务复杂时，先调 `update_plan` 排一份 3~6 步的简短计划（只写步骤与验证方式，不要冗长），
  之后每完成一步就更新它。这份清单会随检查点落盘 —— 任务中途被打断时，它就是你恢复进度的依据。
- 小改动用 edit（精确匹配），大改动用 write；完成后运行测试验证。
- 每条消息最多做必要的工具调用；工具失败时根据错误信息自行修复后重试。
- 工具返回里带 `[exit code: N]`，N≠0 表示命令没成功 —— 先看错误信息定位原因，不要重复同样的调用。
{ask_user_hint}

硬性约束：
- 只能在工作目录（沙箱）内操作，禁止访问沙箱外路径。
- 禁止执行危险命令（rm -rf、git push 等被工具拒绝）。
- 完成任务后：输出最终结论（做了什么、验证结果如何）。
- 不要假装完成：必须用测试/命令实际验证，验证失败要报告。
- 不要无意义重复同一操作；若连续多次得到相同失败，停下来向用户说明。

{repo_memory_block}

{skills_block}"""

#: `{ask_user_hint}` 槽位的两个取值 —— 取决于 `ask_user` **这次有没有被注册**。
#:
#: 为什么要分两种写法，而不是把这句话写死在 prompt 里：`ask_user` 是**按入口注册**的
#: （`eval/runner.py` 用 `ToolRegistry.default()`，里面刻意没有它）。写死的话，headless
#: 评测里模型会去调一个不存在的工具 —— 后果不是报错，而是**白白浪费一步**（`_gate_and_run`
#: 返回「未知工具」并把可用工具名回喂，模型再改道）。这正是真跑验证时发现的那类问题：
#: 机制在，但没有任何东西把模型引向它；而引错了方向同样无声无息。
_ASK_USER_HINT = (
    "- 信息不足、需求有歧义、或者有不可逆的选择要人拍板时，**调 `ask_user` 提问**，"
    "不要靠猜 —— 猜错的代价由提需求的人承担。"
)
_NO_ASK_USER_HINT = ""   # 没有这个工具时整行消失（槽位本身仍在，见 test_loop.py 的断言）


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
        skills: object | None = None,              # skills 发现结果（agent.skills.SkillDiscovery）
        context: object | None = None,             # M3 接 ContextManager
        session: object | None = None,             # M3 接 session（轨迹/检查点）
        on_event: Callable[[dict], None] | None = None,  # 实时事件回调（CLI 流式输出）
        abort: AbortToken | None = None,           # M9-7 取消标志（**仅 worker**）
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
        self.skills = skills
        self.context = context
        self.session = session
        self.on_event = on_event
        # ★ M9-7 取消标志。**只有 `AgentWorkers.spawn` 会传它，只有 worker 引擎
        # 会拿到它。** 这不是建议、是硬约束：常驻 REPL 的引擎是**跨回合复用**的
        # （`app/repl.py` 的 `_activate` 设一次、每个回合 `run_turn`），给它装一个
        # token 意味着某一回合把它 set 了之后，**后续每一个回合**都会在第一个检查点
        # 就返回 `aborted` —— 用户看到的是"我说话它不理"，而且没有任何东西能解释
        # 这件事。`tests/test_subagent.py` 有一条测试专门钉住"不带 token 的常驻
        # 引擎不受影响"。
        #
        # 它的语义是**协作式取消**，不是抢占：只在两个检查点上生效（见 `_run_loop`
        # 与 `_execute_tool_calls`），所以一个正卡在 `llm.chat`（客户端超时 120s）
        # 或 `bash` 里的 worker 最长还要跑完那一次调用才会停。
        self.abort = abort
        self.system_prompt = system_prompt or DEFAULT_SYSTEM_PROMPT

    def run(self, task: str, *, cwd: Path | None = None) -> RunResult:
        """新建会话状态并跑任务。有 session 时用其 session_id（轨迹/检查点归属）。"""
        return self.run_turn(self.new_state(task), task, cwd=cwd)

    def new_state(self, task: str) -> AgentState:
        """构造一个全新的会话状态：渲染 system prompt + 拼记忆块 + 认领 session_id。

        **抽出来的理由（M9-5）**：常驻 REPL 必须**自己持有** `AgentState` 才能跨回合
        （`run()` 把它建在局部变量里就丢了，`RunResult` 也不带它）。而"怎么建 state"
        这件事只能有一处 —— 否则 system prompt 的槽位渲染、记忆块拼装、session_id
        归属这三件事会在 REPL 里再写一遍，任何一处漏了都是静默的（模型收到的
        prompt 少一块，不报错、只是变笨）。

        `messages` 里**只有 system 一条**：第一条 user 消息由 `run_turn` 追加
        （它是"一个回合"的唯一入口）。这样 `run()` 也不必自己拼 —— 它现在就是
        `new_state` + `run_turn` 两行。
        """
        if self.memory_blocks:
            memory_block = "\n".join(f"- {block}" for block in self.memory_blocks)
        else:
            memory_block = ""
        sys_prompt = self._render_system_prompt(memory_block)

        # 有 session 时用其 session_id（轨迹/检查点归属）；_EmitProxy 之类
        # 只实现 emit 的轻量替身没有 session_id，兜底 "m1"
        session_id = getattr(self.session, "session_id", "m1")
        return AgentState(
            session_id=session_id,
            task=task,
            system_prompt=sys_prompt,
            messages=[system(sys_prompt)],
        )

    def run_turn(
        self, state: AgentState, text: str, *, cwd: Path | None = None
    ) -> RunResult:
        """**一个用户回合**的唯一入口（M9-5）：接上一条 user 消息 → 跑一轮。

        顺序是固定的，不能调换：

        1. `ensure_tool_pairing` —— 上一回合被 Ctrl+C 打断的话，`messages` 可能停在
           「assistant(tool_calls=[3 个]) + 只有 1 条 tool 结果」。**必须先修再跑**：
           下一轮请求带着孤儿 id 会直接被端点 400，而报错发生在**这一回合**，
           看起来和上次那一下 Ctrl+C 毫无关系。
        2. 追加 `user(text)` —— **由本方法追加，不由调用方追加**。`--resume` 那条路
           是 CLI 自己 append 的（还带一条 `resume_instruction` 事件）；REPL 若也
           各写一遍，漏掉的那次就是"模型收到一个没有提问方的回合"。
        3. `state.task = text` —— `run_from` 用的是 `state.task`，而轨迹里
           `RunResult.task` 也是它；不更新就会让第二回合的结论挂上第一回合的任务名。
        4. 跑一轮，**本轮一份步数预算**（`budget_start`）。
        """
        repaired = ensure_tool_pairing(state.messages)
        if repaired:
            # 不静默修：轨迹里要留下"这一回合是缝过的"，否则事后看一份对话
            # 想不通模型为什么会说"重新调用一次"。
            state.record_event("pairing_repaired", count=repaired)
        state.task = text
        state.messages.append(user(text))
        return self._run_loop(state, text, cwd, budget_start=state.step)

    def run_from(self, state: AgentState, *, cwd: Path | None = None) -> RunResult:
        """从检查点恢复的 state 继续执行（Session.resume 配合用）。

        step 计数不重置：续跑继续推进步数，新检查点不会覆盖恢复前的同名文件。

        步数**预算**却是新的一份（`budget_start` = 恢复时的 step）：`max_steps`
        的意思是"这次运行最多走几步"，不是"这个会话累计几步"。旧语义下有个
        真陷阱 —— 会话跑满 25 步后 `--resume` 会**一次模型调用都不发**，直接又
        打印「已达到最大步数（请拆分子任务）」，而错误信息指的方向还是错的。
        """
        return self._run_loop(state, state.task, cwd)

    def _run_loop(
        self,
        state: AgentState,
        task: str,
        cwd: Path | None,
        *,
        budget_start: int | None = None,
    ) -> RunResult:
        """共享主循环：think → 调工具 → 看结果 → ... → 完成。

        `budget_start`：本次运行的步数预算从哪一步算起。省略 = 进入循环时
        `state.step` 的值（`run()` 是 0、`run_from` 是恢复出来的步数）。
        `state.step` 本身**照样累计、不重置** —— 检查点文件名 `step-N.json`、
        `--fork --step K`、轨迹里的 `step` 字段都依赖它单调递增。
        """
        if budget_start is None:
            budget_start = state.step

        state.emitter = self._make_emitter()

        # 新回合：清掉上一回合的终止状态与"本回合"权限记忆（M9-5）。
        #
        # 放在这里而不是各入口，是因为**这是唯一一处**能让四个入口（CLI 单发 /
        # CLI REPL / 控制台 / eval）一起拿到它的地方；在单发路径上两者都是
        # no-op（一次运行只有一回合，terminated_reason 本来就由本次运行的
        # 5 个返回点重写），所以不改变任何既有行为。
        #
        # `terminated_reason` 不复位的话：REPL 第二回合**全程**带着上一回合的值，
        # 而中途落的检查点会把它写进 payload —— 于是"从检查点读出来的终止原因"
        # 与"这个会话真的怎么结束的"是两件事（`app/ui_streamlit.py` 就在读它）。
        # `permissions.new_turn()` 同理：不清 `_turn` 的话，确认框上的
        # 「2) 本回合允许」在常驻进程里会变成"一直允许"。
        state.terminated_reason = None
        if self.permissions is not None and hasattr(self.permissions, "new_turn"):
            self.permissions.new_turn()

        if self.skills is not None:
            # 记「本次运行带着哪些 skill」进轨迹。放在这里而不是入口，是因为
            # 入口有 3 处（CLI 新建 / CLI resume / 控制台），一处漏了就不一致；
            # 且 `record_discovery` 自己会判断索引是否真在这份 system prompt 里
            # （resume 用的是会话当初那份，可能没有），也会跳过**已经记过**的
            # 会话 —— 不给真空的会话记假事件，也不给第二回合重复记一份（M9-5）。
            from agent.skills import record_discovery, render_skills_block

            record_discovery(
                state, self.skills, render_skills_block(self.skills)
            )

        # M9-7：本回合的子代理管理器。**每回合新建一个**（生命周期 = 一次
        # `_run_loop`），所以 worker 活不过派发它的那一个回合 —— 这是刻意的，
        # 也是下面 `finally` 能放心结算它的前提。
        #
        # `on_usage` 是**唯一**把 worker 用量并进父会话的通路，且它只在父线程被
        # 调用（`AgentWorkers` 的契约）：`Usage.__iadd__` 不是原子的，在 worker
        # 线程里合并会与下一行的 `state.usage += result.usage` 赛跑。
        #
        # ⚠️ **这让父会话的缓存命中率被稀释**（worker 的 prompt 大多缓存未命中），
        # README 的缓存数字只在"没用过子代理的会话"上可比 —— 如实记着。
        def _count_subagent_usage(usage: Usage) -> None:
            state.usage += usage

        workers = AgentWorkers(on_usage=_count_subagent_usage)

        ctx = ToolContext(
            workspace_root=self.workspace_root,
            cwd=cwd,
            permissions=self.permissions,
            hooks=self.hooks,
            # state/emitter 这两个槽位曾经"声明了但没有任何入口填" —— 工具作者照
            # 声明去读会拿到 None，而且不报错。现在填上：`state` 是 update_plan
            # 写 plan 的通道，`emitter` 与 `state.emitter` 是同一个回调。
            state=state,
            emitter=state.emitter,
            workers=workers,
        )
        signatures_window: deque[list[str]] = deque(maxlen=self.loop_detection_window)
        empty_retry_count = 0

        try:
            while state.step - budget_start < self.max_steps:
                # ★ M9-7 取消检查点 #1（两个之一）。位置是**唯一正确的那一处**：
                # 不能写进 `while` 条件 —— 条件在上一行求值，而 `_prepare_messages`
                # 已经先跑完了一整轮上下文整理（compact / 截断）的活。
                #
                # 也不能更晚：再往下就是 `llm.chat` —— 全循环唯一一次可能阻塞
                # 120 秒（客户端超时）的操作。
                if self.abort is not None and self.abort.is_set():
                    return self._aborted_result(state, task)
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

                # ★ M9-6 批前复位：结构性地保证「一次声明只触发一次检查」。
                # 不复位的话，第 N 步声明过一次之后，**后面每一步**都会重跑一次
                # 完成检查（声明字段一直挂着）—— 烧钱、上下文爆，而且不报任何错。
                if state.goal is not None:
                    state.goal.declaration = None

                pending = self._execute_tool_calls(result.tool_calls, state, ctx)

                # ★ M9-6 完成检查在**批后**：「先跑测试验证、再声明完成」是模型
                # 同一步里最常见的形状，放在批前只会看到上一轮的陈旧声明。
                # 放在 checkpoint 之前返回，是为了让这一步的检查点带上**判定之后**
                # 的目标状态（done / 仍 active），否则 resume 出来的目标是错的。
                if state.goal is not None and state.goal.declaration is not None:
                    goal_end = self._verify_goal(state, task, ctx)
                    if goal_end is not None:
                        return goal_end

                if self.session is not None:
                    self.session.checkpoint(state)  # M3-4 每 N 步落盘检查点
                if pending is not None:
                    # ask_user：本轮结束，问题交给用户（注意是 `is not None` ——
                    # 问题理论上可以是空串，用真值判断会把它当成"没提问"）
                    return self._awaiting_user(state, task, pending)
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
        finally:
            # ★ M9-7 回合边界结算。**一个 `finally` 覆盖上面 6 个返回点 +
            # `KeyboardInterrupt`** —— 逐个返回点去结算，漏掉一个就是"worker
            # 活过它的回合、结论无处可去"，而那正是本项目的头号缺陷类。
            #
            # 这也是它必须写在 `finally` 而不是某个返回点上的原因：`except
            # Exception` **抓不到 `KeyboardInterrupt`**（`BaseException`），
            # 而 Ctrl+C 恰恰是最需要结算的那一刻。
            self._settle_workers(workers, state)

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
        """注入运行期槽位（工作目录 / 平台 / 记忆块 / skills 索引）。

        用逐个 replace 而不是 str.format：自定义 system_prompt 里可能含其它花括号
        （JSON 示例、代码片段），format 会直接抛 KeyError。

        **skills 索引只在这里渲染一次**，结果随 `state.system_prompt` 进检查点。
        绝不能每轮重新发现：system 是 `messages[0]`，落在 `_PREFIX_LEN` 保护区里
        —— 它每轮变一次，等于整条前缀缓存**永久失效**（DeepSeek 的缓存是前缀匹配，
        改第 0 条 = 后面全部重算）。
        """
        from agent.skills import render_skills_block

        slots = {
            "workspace_root": str(self.workspace_root),
            "platform": _platform_hint(),
            "repo_memory_block": memory_block,
            "skills_block": render_skills_block(self.skills) if self.skills else "",
            # 按**实际注册了哪些工具**填这一行：见 `_ASK_USER_HINT` 的说明。
            # 渲染一次就随 `state.system_prompt` 进检查点，所以不会每轮变。
            "ask_user_hint": (
                _ASK_USER_HINT if "ask_user" in self.registry.names() else _NO_ASK_USER_HINT
            ),
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
    ) -> str | None:
        """执行一轮工具调用。全只读 → 并发（map 保序）；含可写 → 串行。

        返回「待用户回答的问题」：非 None 表示本轮因 `ask_user` 而暂停，调用方
        应结束本轮（见 `_awaiting_user`）。
        """
        all_read_only = all(
            self.registry.get(call.name).is_read_only() if call.name in self.registry else False
            for call in calls
        )

        def invoke(call: ToolCall) -> tuple[str, str, bool]:
            """返回 (tool_call_id, output, await_user)。单条调用无论成败都封包成文本回喂模型。"""
            # ★ M9-7 取消检查点 #2。放在这里而不是调用点，是因为**串行与并发
            # 两条路都经过它**，且不用给任何签名加参数。
            #
            # 粒度要如实说：串行路是**逐调用**生效的（一个 5 步的写工具批在
            # 中途被取消，后面的不再跑）；**并发路是整批** —— `executor.map`
            # 一次性提交全部任务，等它返回时每一个 `invoke` 都已经跑过了，
            # 这个检查对整批来说恒为 False。不声称批内逐调用粒度。
            if self.abort is not None and self.abort.is_set():
                state.record_event(
                    "tool_call",
                    name=call.name,
                    arguments=call.arguments,
                    success=False,
                    duration_ms=0,
                    exit_code=None,
                    await_user=False,
                    aborted=True,   # 与"真跑了但失败"、与 skipped 都分开
                )
                return call.id, ABORTED_TOOL_OUTPUT, False
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
                await_user=result.await_user,  # 轨迹里能看出"这轮为什么停了"
            )
            return call.id, result.output, result.await_user

        if all_read_only and len(calls) > 1:
            with ThreadPoolExecutor(max_workers=min(4, len(calls))) as executor:
                # map() 保持输入顺序 → tool 结果消息顺序与 calls 一一对应（id 匹配）
                results = list(executor.map(invoke, calls))
        else:
            results = []
            for idx, call in enumerate(calls):
                outcome = invoke(call)
                results.append(outcome)
                if not outcome[2]:
                    continue
                # ask_user 命中：本轮到此为止，同批里排在它后面的调用**不再执行**。
                #
                # 但**必须给它们补上配对的失败结果**：assistant 那条消息的
                # `tool_calls` 里已经有这些 id，少一条对应 `tool` 消息，OpenAI 兼容
                # 端点会直接 400（"tool_call_id 无对应结果"）—— 会话就此再也跑不动，
                # 而且报错发生在下一轮请求时，看起来和 ask_user 毫无关系。
                # 参考实现在这里直接 break 走人，留下孤儿 id（见笔记 §「明确不吸收」）：
                # 那是它自己的 bug，我们不跟着抄。
                skipped = ToolResult.fail(
                    "本轮因等待用户输入而跳过，请在用户回答后重新调用"
                )
                for later in calls[idx + 1:]:
                    state.record_event(
                        "tool_call",
                        name=later.name,
                        arguments=later.arguments,
                        success=False,
                        duration_ms=0,
                        exit_code=None,
                        await_user=False,
                        skipped=True,  # 与"真的执行了但失败"区分开
                    )
                    results.append((later.id, skipped.output, False))
                break

        # compact_batch 的签名是 list[tuple[str, str]]，这里先拆掉第三元，不动它
        pairs = [(cid, out) for cid, out, _ in results]
        # 取问题原文要**赶在 compact_batch 之前**：万一问题被判定为超大结果落盘，
        # output 会变成 `<persisted-output>` 预览，CLI 就得打印那个占位符给用户看。
        pending = next((out for _, out, awaiting in results if awaiting), None)

        # M3-2：超大工具结果落盘（上下文替换为预览+路径，避免截断丢信息）
        store = self._store_for(state)
        if store is not None:
            pairs = compact_batch(pairs, store)

        # 消息追加：一条 assistant tool_calls + N 条 tool 结果（顺序与 calls 对应）
        state.messages.append(assistant_tool_calls(calls))
        for tool_call_id, output in pairs:
            state.messages.append(tool_result(tool_call_id, output))
        return pending

    def _aborted_result(self, state: AgentState, task: str) -> RunResult:
        """子代理被取消（M9-7）：如实收尾，**不假装完成、也不假装出错**。

        与 `"error"` 分开是刻意的：取消是**人（或父回合边界）要求的**，不是
        出了故障；把它混进 `"error"` 会让父模型以为子代理崩了并据此换个策略，
        而正确的动作是"要么重新派、要么别派了"。
        """
        state.terminated_reason = REASON_ABORTED
        state.record_event("aborted", step=state.step)
        return RunResult(
            final_text=(
                f"子代理已被取消（在第 {state.step} 步停下），没有结论。"
            ),
            steps=state.step,
            usage=state.usage,
            events=state.events,
            terminated_reason=REASON_ABORTED,
            task=task,
        )

    def _settle_workers(self, workers: AgentWorkers, state: AgentState) -> None:
        """回合边界结算（M9-7）。**整个方法异常免疫，绝不向外抛。**

        它在 `_run_loop` 的 `finally` 里被调用，而那个位置正压着两样东西：
        一个是即将返回的 `RunResult`，另一个可能是正在传播的 `KeyboardInterrupt`。
        **一个会抛的 `finally` 会把前者换成异常、把后者换成别的异常** —— 用户
        按的 Ctrl+C 变成一个看不懂的 traceback，而子代理的结论照样丢了。
        两样都比"结算失败"更糟，所以这里兜住。

        但**兜住不等于静默**：失败也尽力记一条事件。只有连事件通道本身（
        `Session.emit` 是文件 IO）都坏了才会真的沉默 —— 那时也没有别的通路了。
        """
        try:
            report = workers.settle()
            if report.any:
                state.record_event("subagent_settled", **report.as_event())
        except Exception as exc:
            try:
                state.record_event(
                    "subagent_settle_failed", error=f"{type(exc).__name__}: {exc}"
                )
            except Exception:
                pass

    def _awaiting_user(self, state: AgentState, task: str, question: str) -> RunResult:
        """收尾本轮并交还控制权给用户（`ask_user` 的回合语义）。

        **这里刻意不追加 "assistant 复述问题" 的消息**：问题原文已经是
        `ask_user` 那条 tool 结果的内容了，模型在 resume 后看到的是
        「assistant 调 ask_user(问题) → tool(问题) → user(回答)」，因果链完整。
        再补一条 assistant 文本只是同一条信息出现第二次，白占 token 还破坏前缀缓存。
        """
        state.terminated_reason = "await_user"
        state.record_event("await_user", question=question, step=state.step)
        # force：本轮结束、进程马上退出，节流检查点（默认 5 步）永远等不到下一次
        # tick —— 问题发生在第 3 步的话就根本不会落盘，`--resume` 只能恢复出一个
        # 没有问题的会话，用户面对着空白回答。**这不是优化，是正确性**。
        if self.session is not None and hasattr(self.session, "checkpoint"):
            self.session.checkpoint(state, force=True)
        return RunResult(
            final_text=question,
            steps=state.step,
            usage=state.usage,
            events=state.events,
            terminated_reason="await_user",
            task=task,
        )

    def _verify_goal(
        self, state: AgentState, task: str, ctx: ToolContext
    ) -> RunResult | None:
        """跑一次**人预先给定的**完成检查命令。返回 None = 未通过，本轮继续。

        **走 `_gate_and_run` 而不是直接 `tool.run`。** 这不是顺手复用：检查命令
        是**人写的一行 shell**，它必须和模型自己发的命令受同一套治理 ——
        PreToolUse 的 block-at-submit、权限 deny、危险命令 ask、**污染天花板**。
        顺带还换来一个明确判据：「检查被拦下了」有 `gate_block` 事件带
        `source`/`reason`，而不是靠猜（见 S19：副作用同样会走 PostToolUse，
        所以一条成功的 `pytest` 会写 `data/tests_pass.marker`、**替 agent 解锁
        `git commit`** —— 轨迹上与模型自己跑同一条命令不可区分，这是复用同一条
        链的代价，不是疏漏）。

        **绝不伪造 `assistant(tool_calls=[...]) + tool(...)` 消息对**把检查伪装成
        模型发起的一次工具调用 —— 那是在会话里写下一个模型从没发出过的调用，
        与 `PAIRING_FILLER`「必须说实话」的纪律直接冲突。判定以 **user 消息**
        回喂，同 `_reinject_plan` 的先例。

        三态判定（对齐 `eval/golden_tasks.py` 的 `JudgeResult.executed`）：
        命令**压根没跑成**时，「算完成」和「算没完成」都是错的。
        """
        goal = state.goal
        declaration = dict(goal.declaration or {})
        # 瞬态字段，取走就清：留着会让它跟着检查点走，而它下一次就会被批前复位，
        # 两处都写就成了"谁才是权威"的第二个真相源。
        goal.declaration = None

        # 合成的 call.id 只活在这一次调用里，**不进任何消息**（见 docstring）。
        call = ToolCall(
            id=f"goal_check@{state.step}",
            name="bash",
            arguments={"command": goal.check_command, "timeout": GOAL_CHECK_TIMEOUT},
        )
        before = len(state.events)
        result = self._gate_and_run(call, state, ctx)
        # 门禁阻断与"命令真的跑了但退出码非 0"必须分开：前者是**判定无效**。
        # `_gate_and_run` 的阻断路径（registry / hooks / permissions）都会先记一条
        # `gate_block` 再返回 fail，所以拿事件切片判定，而不是靠猜错误文本。
        gate_blocks = [
            event for event in state.events[before:]
            if event.get("type") == "gate_block"
        ]
        exit_code = (
            result.data.get("exit_code") if isinstance(result.data, dict) else None
        )

        if gate_blocks or not result.success or exit_code is None:
            verdict = CHECK_INVALID
            reason = (
                gate_blocks[-1].get("reason") if gate_blocks
                else (result.error or tail_lines(result.output, 5))
            )
        elif exit_code == 0:
            verdict, reason = CHECK_PASSED, None
        else:
            verdict, reason = CHECK_FAILED, None

        state.record_event(
            "goal_check",
            verdict=verdict,
            command=goal.check_command,
            exit_code=exit_code,
            duration_ms=result.duration_ms,
            declaration=declaration,
            reason=reason,
            # 事件里存**尾部**而不是全量输出：events 会进检查点，而 `--resume`
            # 每次恢复都要读它。一次 pytest 的输出可达几十万字符（bash 的兜底
            # 上限是 500_000），把全量塞进检查点等于让**每一次恢复**都为它付钱。
            output_tail=(
                "" if verdict == CHECK_PASSED else tail_lines(result.output)
            ),
            output_chars=len(result.output),
        )
        goal.last_check = {
            "verdict": verdict,
            "step": state.step,
            "exit_code": exit_code,
            "reason": reason,
        }

        if verdict == CHECK_PASSED:
            goal.status = GOAL_DONE
            goal.pause_reason = None
            state.record_event(
                "goal_completed",
                objective=goal.objective,
                command=goal.check_command,
                turns=goal.turns,
                declaration=declaration,
            )
            # **通过就把回合结束掉，不让模型再写一段结论。** 让它看到「检查通过」
            # 再自己总结，完成就又变成模型说的话了 —— 恰是本项的反面。结论由
            # 运行时给出（连同模型的声明原文，人能看到它当初声称了什么）。
            return self._goal_turn_result(
                state, task, REASON_GOAL_DONE,
                f"目标已完成：完成检查通过（`{goal.check_command}` 退出码 0）。\n"
                f"目标: {goal.objective}\n"
                f"声明: {declaration.get('summary') or '（无）'}",
            )

        if verdict == CHECK_INVALID:
            # **判定无效也结束回合**：检查命令坏了，模型**没有任何办法**修它
            # （它不能改 `check_command`）。留着它继续，只会让它反复声明、
            # 每次拿回一条无效判定、把步数烧光。
            #
            # **刻意不复用 `await_user`**：那个值连带两件事，两件都不对 ——
            # `_extract_learned` 会跳过约定提炼（而这里的轨迹是完整的），
            # REPL 会打印「需要你补充信息」（而这里没有任何人被提问）。
            # 多一个终止原因是诚实的代价。
            return self._goal_turn_result(
                state, task, REASON_GOAL_CHECK_INVALID,
                f"完成检查未能执行 —— 本次判定不计入。\n"
                f"目标: {goal.objective}\n"
                f"检查命令: {goal.check_command}\n"
                f"原因: {reason}",
            )

        # 未通过：**保持 active、本轮继续**。结束回合会让模型失去修复机会 ——
        # 而"检查没过 → 看输出 → 接着修"正是这条动线的全部价值。
        state.messages.append(
            user(render_check_report(goal, CHECK_FAILED, tail_lines(result.output)))
        )
        return None

    def _goal_turn_result(
        self, state: AgentState, task: str, reason: str, final_text: str
    ) -> RunResult:
        """目标把这一轮结束掉了（检查通过 / 判定无效）。

        与 `_awaiting_user` 同一个理由的 `force=True`：流程即将因**非步数**原因
        退出，节流的下一次 tick 永远等不来 —— 判定结果会留在内存里，`--resume`
        恢复出来的目标状态是错的（active，而它其实已经 done 了）。
        """
        state.terminated_reason = reason
        if self.session is not None and hasattr(self.session, "checkpoint"):
            self.session.checkpoint(state, force=True)
        return RunResult(
            final_text=final_text,
            steps=state.step,
            usage=state.usage,
            events=state.events,
            terminated_reason=reason,
            task=task,
        )

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

    def _preview(
        self, tool: Tool, call: ToolCall, ctx: ToolContext, state: AgentState
    ) -> str | None:
        """取这次调用"将要做什么"的预览（M9-1；目前只有 write/edit 实现）。

        **失败一律吞掉、但留一条事件**：预览是给人看的信息，不是判定依据。
        让它把整轮任务搞挂，比它想解决的问题更糟（同 `security.py` 的 fail-open
        纪律）。但"吞掉"必须是**可见**的 —— 静默地把确认框退回原样，人只会以为
        "这个工具本来就没有预览"，而不是"预览出错了"。所以记 `preview_failed`。
        """
        preview = getattr(tool, "preview", None)
        if preview is None:
            return None
        try:
            return preview(call.arguments, ctx)
        except Exception as exc:
            state.record_event(
                "preview_failed", tool=call.name, error=f"{type(exc).__name__}: {exc}"
            )
            return None

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
            # M9-1：在 check() **之前**取"将要改什么"。位置就是这一项的全部意义 ——
            # 人看到 diff 时文件还是旧的（`tests/test_loop.py` 有一条测试专门钉这个：
            # 确认回调被调用的那一刻，磁盘上必须仍是原文）。
            details = self._preview(tool, call, ctx, state)
            decision = self.permissions.check(
                call.name, call.arguments, ctx, details=details
            )
            if decision is Decision.DENY:
                return self._gate_block(
                    state,
                    call,
                    "permissions",
                    f"权限拒绝: 未获允许执行 {call.name}"
                    f"（{self.permissions.describe(call.name, call.arguments, details=details)}）",
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

        # 3.5) M9-8 工作区快照登记：**成功写盘**的 write/edit 在这里被记下来。
        #
        # 位置就是这一项的全部意义：登记挂在门禁链**之后**、`tool.run` **成功之后**。
        # 于是"权限拒绝的那次（在上面就 return 了）""edit 匹配失败（result.success
        # 为 False）""工具自己抛了（被 run 兜住）"三种情况**结构上**走不到这一行 ——
        # 不靠谁记得判断，也就不会因为将来加一条 return 就悄悄漏掉。
        #
        # `self.session is not None` 是同一条纪律的延续：子代理的引擎 session=None
        # （它连检查点都写不了），所以 worker 结构上拿不到快照对象 —— 与"worker
        # 写不了文件"是同一个保证，不另加守卫。
        if result.success and result.file_changes and self.session is not None:
            for change in result.file_changes:
                self.session.snapshots.note_write(change)

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
