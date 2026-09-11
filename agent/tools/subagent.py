"""子代理工具族（M4-2 起，M9-7 扩成 5 个）。

主循环把"探索并总结/调查某个问题"类工作**外包**给子代理（CC SubAgent 经济学：
把 (X+Y)×N 的深度探索外包出去，只把结论 Z tokens 带回主上下文，主上下文保持干净）。

- **独立上下文**：子代理用全新的 AgentState + 独立 system prompt，主循环的
  messages/usage/compact 全部不共享。
- **受限只读 registry**：默认只给 glob/grep/read（可指定），且永远不含任何
  子代理工具 → 天然禁止递归嵌套（**白名单，不是黑名单** —— 黑名单在"以后加了
  新工具"时会静默失效）。
- **复用 QueryEngine**：同一 BaseLLM、同一循环逻辑，只是 registry/system prompt/
  max_steps/abort 不同——代码零重复。
- **返回结构化报告**：`ToolResult.ok(结论)`；子代理失败/超步/被取消也如实回喂
  （不假装成功，主模型可据信息调整策略）。

## 五个工具与**唯一构造点**

| 工具 | 语义 |
|---|---|
| `subagent` | 阻塞便捷入口：spawn + 无限等（M4-2 的签名一字未改） |
| `spawn_agent` | 立刻返回句柄（`sa-1`），工作在后台跑 |
| `list_agents` | 花名册（id / 状态 / 任务摘要） |
| `wait_agent` | 等一组 worker；**超时只返回最新状态，不关闭** |
| `close_agent` | abort + 等它真停；对已终结的幂等 |

五个**共用 `_SubagentRunner.build`**：受限 registry 的构建、步数上限、系统提示词、
abort 接线、落盘 store 只写一份。`SubagentTool` 不是特例，它就是"spawn + 无限等"
这两行的适配器。

## 为什么五个全是 `is_read_only() == False`（即使是 `list_agents`）

理由不是"子代理危险"，而是**调度**：只读工具走并发池（`loop._execute_tool_calls`
的 `executor.map`），而 `map` 保证的是**输出顺序**、不是**开始顺序**。一批
`[spawn(A), spawn(B), wait([A,B])]` 若进并发池，`wait_agent` 完全可能在两个 spawn
注册 id **之前**就跑起来 —— 于是它报"未知子代理"，而模型看到的是一个它刚刚亲手
派出去的 id。这不是新造的规则：`agent/tools/goal.py` 的 `DeclareGoalDoneTool` 同样
是 `False`，理由写着「它改变的是**控制流**」。

`list_agents` 尤其要串行 —— 它**纯粹是读**，但同一批里的花名册顺序不确定的话，
连测试都没法写。

## 接线缺口不静默降级

`ctx.workers is None`（引擎没接管理器）时五个工具都返回 `ToolResult.fail` 并
说明原因，而不是抛异常、也不是假装成功。同 `DeclareGoalDoneTool` 的「不静默降级」。
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable

from pydantic import BaseModel, Field

from agent.loop import QueryEngine
from agent.subagents import (
    AGENT_DONE,
    AgentSnapshot,
    AgentWorkers,
    TooManyAgents,
    UnknownAgent,
    WorkerOutcome,
    AbortToken,
    brief_task,
)
from agent.tool_result import ToolResultStore
from agent.tools.base import Tool, ToolContext, ToolResult, ToolRegistry
from agent.tools.files import GlobTool, GrepTool, ReadTool

MAX_SUBAGENT_STEPS = 10
# 子代理可用的只读工具白名单（受限集；写工具/ bash / 任何子代理工具一律不给）
_READ_ONLY_TOOLS = {
    "glob": GlobTool,
    "grep": GrepTool,
    "read": ReadTool,
}

SUBAGENT_SYSTEM_PROMPT = """\
你是 CodeAgent 的研究子代理。你的任务是【只读探索】当前工作目录并回答用户的调查问题。

工作目录（沙箱根）：{workspace_root}
所有相对路径都以它为基准。只能访问这个目录内的路径，禁止越界。

工作方式：
- 先用 glob/grep/read 探索仓库，读到真实内容后再作答；不要臆测或编造。
- 你的工具全部只读，不能修改任何文件；也没有 bash，无法执行命令。
- 任务完成后，输出结构化结论：关键发现 + 依据（文件/行号）+ 对主代理的建议。

硬性约束：
- 不要假装完成：必须基于实际读到的内容回答。
- 你活在一个回合里 —— 派你出来的主代理正等着你的结论，**把结论写完整**，
  它没有机会再追问你。
"""

#: 引擎没接子代理管理器时的失败文案。**说清是接线缺口**，不是让模型去猜
#: "是不是我用错了参数"。
_NO_WORKERS = (
    "子代理不可用：本回合没有接线子代理管理器（这是接线缺口，不是参数错误）。"
    "请改用 glob/grep/read 自己完成这次探索。"
)


class SubagentInput(BaseModel):
    task: str = Field(description="子代理要调查/总结的问题")
    tools: list[str] = Field(
        default_factory=lambda: ["glob", "grep", "read"],
        description="允许子代理使用的只读工具名（白名单 glob/grep/read）",
    )
    max_steps: int = Field(default=10, description="子代理最大步数（≤10）")


class SpawnAgentInput(BaseModel):
    task: str = Field(
        description="子代理要调查的问题。**写得越具体，它的结论越有用** —— "
        "它看不到你现在的对话，只看到这一句话。"
    )
    tools: list[str] = Field(
        default_factory=lambda: ["glob", "grep", "read"],
        description="允许子代理使用的只读工具名（白名单 glob/grep/read）",
    )
    max_steps: int = Field(default=10, description="子代理最大步数（≤10）")


class ListAgentsInput(BaseModel):
    """无参数。留一个空模型而不是 None，是因为 `Tool.schema()` 需要它。"""


class WaitAgentInput(BaseModel):
    ids: list[str] = Field(
        default_factory=list,
        description="要等的子代理 id（如 [\"sa-1\",\"sa-2\"]）；省略 = 全部还在跑的",
    )
    timeout_ms: int | None = Field(
        default=None,
        description="最多等多少毫秒；省略 = 等到它们全部结束。"
        "超时**不会**关掉它们，只回报当下状态，可以再等一次。",
    )


class CloseAgentInput(BaseModel):
    id: str = Field(description="要停掉的子代理 id（如 \"sa-1\"）")


# ---------- 单一构造点 ----------


def _restricted_registry(requested: list[str]) -> ToolRegistry:
    """按白名单构建受限 registry；未知/不可用的名字静默忽略。

    永不包含任何子代理工具 → 子代理无法再递归启动子代理。
    """
    registry = ToolRegistry()
    for name in requested:
        cls = _READ_ONLY_TOOLS.get(name)
        if cls is not None:
            registry.register(cls())
    return registry


class _SubagentRunner:
    """把"怎么造一个子代理"收成一处，供上面五个工具共用。

    `_seq` 只在父线程自增（五个工具都是串行的，见模块 docstring），所以不需要
    锁；它只用来给每个 worker 一个**unique 的落盘目录名**。
    """

    def __init__(self, llm, workspace_root: Path) -> None:
        self.llm = llm
        self.workspace_root = Path(workspace_root).resolve()
        self._seq = 0

    def build(
        self,
        *,
        task: str,
        tool_names: list[str],
        max_steps: int,
        cwd: Path | None,
        parent_session_id: str,
    ) -> Callable[[AbortToken], WorkerOutcome]:
        registry = _restricted_registry(tool_names)
        steps = min(max(1, max_steps), MAX_SUBAGENT_STEPS)
        self._seq += 1
        # M3-2 的缺口补上：worker 没有 session，`QueryEngine._store_for` 于是
        # 返回 None —— 超大的 read/grep 结果会**无界**进 worker 的 messages。
        # 显式传一个 store。session_id **必须每个 worker 不同**：两个 worker
        # 撞同一批落盘文件的话，后写的会覆盖先写的。
        store = ToolResultStore(
            self.workspace_root, f"{parent_session_id}-sa{self._seq}"
        )

        def run(token: AbortToken) -> WorkerOutcome:
            engine = QueryEngine(
                self.llm,
                registry,
                workspace_root=self.workspace_root,
                system_prompt=SUBAGENT_SYSTEM_PROMPT,
                max_steps=steps,
                tool_result_store=store,
                abort=token,
                # 刻意**不给**的四样，各有各的理由：
                # - `session`：`Session.checkpoint` 没有锁，两个 worker 同
                #   session_id 会互相覆盖检查点文件。
                # - `on_event`：否则 worker 的工具调用会渲染成父的、步号也对不上，
                #   人分不清屏幕上那行是谁打的。
                # - `permissions` / `hooks`：只读白名单已经保证不写；接上反而是
                #   多一条要推理的路径（而它们读的是**父**的污染标记，语义不对）。
                # - `context`（compact）：worker 的上下文短，且它只活一个回合。
            )
            result = engine.run(task, cwd=cwd)
            return WorkerOutcome(
                result=result,
                usage=getattr(result, "usage", None),
                reason=getattr(result, "terminated_reason", None),
            )

        return run


# ---------- 交付渲染 ----------

_MARKS = {
    "running": "…",
    "done": "✓",
    "failed": "✗",
    "closed": "■",
}


def render_roster(snapshots: tuple[AgentSnapshot, ...]) -> str:
    """花名册。**一个地方渲染** —— `list_agents` 与 `wait_agent` 都印它。"""
    if not snapshots:
        return "（没有子代理）"
    lines = []
    for snap in snapshots:
        mark = _MARKS.get(snap.status, "?")
        line = f"{mark} {snap.id} [{snap.status}] {brief_task(snap.task)}"
        if snap.reason:
            line += f"（{snap.reason}）"
        lines.append(line)
    return "\n".join(lines)


def _report(snapshot: AgentSnapshot) -> ToolResult:
    """把一个终态句柄翻成回喂给模型的 `ToolResult`。

    三种结局分开写，因为模型该做的下一步不同：跑完了 → 用结论；
    出错/超步 → 换策略或别派了；被取消 → 要么重新派、要么就是它该停。
    """
    result = snapshot.result
    text = getattr(result, "final_text", None) or "（子代理无结论）"
    steps = getattr(result, "steps", None)
    data = {
        "agent": snapshot.id,
        "status": snapshot.status,
        "reason": snapshot.reason,
        "steps": steps,
    }
    if snapshot.status == AGENT_DONE and snapshot.reason == "completed":
        return ToolResult.ok(text, data=data)
    if snapshot.status == "failed":
        return ToolResult.fail(
            error=f"子代理 {snapshot.id} 出错: {snapshot.error}",
            output=f"[子代理 {snapshot.id} 出错] {snapshot.error}",
        )
    label = snapshot.reason or snapshot.status
    suffix = f"，{steps} 步" if steps is not None else ""
    return ToolResult.fail(
        error=f"子代理 {snapshot.id} {label}",
        output=f"[子代理 {snapshot.id} {label}{suffix}] {text}",
    )


# ---------- 五个工具 ----------


class _SubagentToolBase(Tool):
    """共同部分：持有那**一个**构造点。

    刻意**不**在这里包一个 `_workers(ctx)` 转发函数 —— 那只是把 `ctx.workers`
    改个名字，读的人还得多跳一层才知道它从哪来。
    """

    def __init__(self, runner: _SubagentRunner) -> None:
        self._runner = runner

    @staticmethod
    def _session_id(ctx: ToolContext) -> str:
        """落盘目录用的父会话标识。没有 state 时用一个固定的兜底名 ——
        它只是目录名，不承担"唯一标识一个会话"的语义。"""
        state = ctx.state
        return getattr(state, "session_id", None) or "sub"


class SubagentTool(_SubagentToolBase):
    """阻塞便捷入口（M4-2 原有工具，签名一字未改）。

    它就是 `spawn_agent` + 无限等两行的适配器 —— 有了句柄式工具之后它没有
    存在的**必要**，但它是模型已经在用的那个名字，拿掉等于让所有既有提示词
    与肌肉记忆失效。
    """

    name = "subagent"
    description = (
        "启动一个只读研究子代理，在独立上下文中探索仓库并返回结构化结论，"
        "**跑完才返回**（阻塞）。适合『总结架构/调查问题/搜索实现细节』类任务："
        "把深度探索外包出去，只把结论带回主上下文。子代理只用只读工具，"
        "不会修改文件。需要同时派多个、或想先干别的再收结论时，用 spawn_agent。"
    )
    input_model = SubagentInput

    def execute(self, args: SubagentInput, ctx: ToolContext) -> ToolResult:
        workers = ctx.workers
        if workers is None:
            return ToolResult.fail(_NO_WORKERS)
        runner = self._runner.build(
            task=args.task,
            tool_names=args.tools,
            max_steps=args.max_steps,
            cwd=ctx.cwd,
            parent_session_id=self._session_id(ctx),
        )
        snapshot = workers.spawn(args.task, runner)
        (done,) = workers.wait([snapshot.id])
        return _report(done)


class SpawnAgentTool(_SubagentToolBase):
    name = "spawn_agent"
    description = (
        "派一个只读研究子代理到后台，**立刻**返回它的句柄 id（如 sa-1）。"
        "最多同时跑 3 个，满了会直接失败（不排队）。\n"
        "**它只活这一个回合**：派出去之后你必须在**本回合内**用 wait_agent 取回"
        "结论 —— 直接给最终答复的话，它跑到一半就会被结算掉，token 花了、结论丢了。"
        "要先调查三件互不相干的事时用它：三次 spawn_agent + 一次 wait_agent。"
    )
    input_model = SpawnAgentInput

    def execute(self, args: SpawnAgentInput, ctx: ToolContext) -> ToolResult:
        workers = ctx.workers
        if workers is None:
            return ToolResult.fail(_NO_WORKERS)
        runner = self._runner.build(
            task=args.task,
            tool_names=args.tools,
            max_steps=args.max_steps,
            cwd=ctx.cwd,
            parent_session_id=self._session_id(ctx),
        )
        try:
            snapshot = workers.spawn(args.task, runner)
        except TooManyAgents as exc:
            # 把花名册一并回喂：模型下一步就知道该 wait 哪一个，而不是重试。
            return ToolResult.fail(
                error=str(exc), output=f"{exc}\n当前花名册:\n{render_roster(workers.list())}"
            )
        return ToolResult.ok(
            f"已派发子代理 {snapshot.id}（后台运行中）。\n"
            f"任务: {brief_task(args.task, 200)}\n"
            f"用 wait_agent(ids=[\"{snapshot.id}\"]) 取它的结论 —— "
            "**必须在本回合内取**，否则它会被结算掉。",
            data={"agent": snapshot.id, "status": snapshot.status},
        )


class ListAgentsTool(_SubagentToolBase):
    name = "list_agents"
    description = (
        "列出本回合派出去的所有子代理（id / 状态 / 任务摘要）。"
        "状态：running 还在跑、done 跑完了、failed 出错了、closed 被叫停了。"
    )
    input_model = ListAgentsInput

    @classmethod
    def is_read_only(cls) -> bool:
        """**看着像只读，仍然标 False（串行）。** 它确实不改任何东西，但一批
        `[spawn_agent, list_agents]` 若进并发池，花名册里有没有那个刚派出去的
        id 就成了竞态 —— 模型会看到一份"自己刚派出去却不在名单上"的花名册。
        判据是**读得干不干净**，不是**读得对不对**：见模块 docstring。"""
        return False

    def execute(self, args: ListAgentsInput, ctx: ToolContext) -> ToolResult:
        workers = ctx.workers
        if workers is None:
            return ToolResult.fail(_NO_WORKERS)
        roster = workers.list()
        return ToolResult.ok(
            render_roster(roster),
            data={"count": len(roster), "live": workers.live_ids()},
        )


class WaitAgentTool(_SubagentToolBase):
    name = "wait_agent"
    description = (
        "等一个或多个子代理结束，返回它们的最新状态与结论。\n"
        "- ids 省略 = 收全部**还没交回**的（在跑的，加上跑完了但你还没取过的）"
        "—— 所以 `spawn×3 + wait_agent()` 一条就能收齐，不会漏掉先跑完的那个。\n"
        "- timeout_ms 省略 = 一直等到它们结束；给了就是最多等这么久，"
        "**超时不会关掉它们**，只回报当下状态，你可以再等一次。\n"
        "- 已经结束的子代理会把它**完整的结论**交回来，这才是子代理的价值所在。"
    )
    input_model = WaitAgentInput

    def execute(self, args: WaitAgentInput, ctx: ToolContext) -> ToolResult:
        workers = ctx.workers
        if workers is None:
            return ToolResult.fail(_NO_WORKERS)
        timeout = None if args.timeout_ms is None else max(0.0, args.timeout_ms / 1000.0)
        try:
            snapshots = workers.wait(args.ids or None, timeout=timeout)
        except UnknownAgent as exc:
            # 抄错 id 是最常见的用法错误 —— 连花名册一起给，下一步就能改对。
            return ToolResult.fail(
                error=str(exc), output=f"{exc}\n当前花名册:\n{render_roster(workers.list())}"
            )

        if not snapshots:
            return ToolResult.ok(
                f"没有还没交付的子代理（在跑的和没取过结论的都没有）。\n"
                f"当前花名册:\n{render_roster(workers.list())}",
                data={"waited": 0},
            )

        done = [s for s in snapshots if s.is_terminal]
        running = [s for s in snapshots if not s.is_terminal]
        chunks = [render_roster(snapshots)]
        for snap in done:
            chunks.append(f"\n--- {snap.id} 的结论 ---\n{getattr(snap.result, 'final_text', None) or '（无结论）'}")
        if running:
            chunks.append(
                "\n仍然在跑: "
                + ", ".join(s.id for s in running)
                + "（这次没等到它们结束；可以再 wait 一次）"
            )
        text = "\n".join(chunks)
        if done:
            return ToolResult.ok(
                text, data={"waited": len(snapshots), "settled": len(done)}
            )
        # 一个都没拿到结论 → 如实标失败。模型据此知道这一轮白等了，
        # 而不是以为"等过了就等于拿到了"。
        return ToolResult.fail(error="等待超时，没有子代理在这一轮结束", output=text)


class CloseAgentTool(_SubagentToolBase):
    name = "close_agent"
    description = (
        "叫停一个子代理并等它真的停下来，返回它停下时的状态。"
        "已经结束/已经被关掉的再关一次是安全的（返回同一个终态）。\n"
        "**它可能要等一会儿才返回**（最多两分钟）：取消在子代理的下一个检查点"
        "生效，如果它正卡在一次网络调用或一条命令里，得等那次调用结束。"
    )
    input_model = CloseAgentInput

    def execute(self, args: CloseAgentInput, ctx: ToolContext) -> ToolResult:
        workers = ctx.workers
        if workers is None:
            return ToolResult.fail(_NO_WORKERS)
        try:
            snapshot = workers.close(args.id)
        except UnknownAgent as exc:
            return ToolResult.fail(
                error=str(exc), output=f"{exc}\n当前花名册:\n{render_roster(workers.list())}"
            )
        note = {
            "closed": "已被叫停，没有结论",
            "done": "它在被叫停之前就已经跑完了",
            "failed": "它在此之前就出错了",
        }.get(snapshot.status, snapshot.status)
        return ToolResult.ok(
            f"{snapshot.id} 已停止（{note}）。\n当前花名册:\n{render_roster(workers.list())}",
            data={"agent": snapshot.id, "status": snapshot.status},
        )


def build_subagent_tools(llm, workspace_root: Path) -> list[Tool]:
    """五个子代理工具的**唯一构造点**（仿 `build_goal_tools` / `build_web_tools`）。

    调用方（`app/cli.py` / `app/ui_streamlit.py`）拿到的是一份共享同一个
    `_SubagentRunner` 的工具列表 —— 于是"怎么造一个子代理"只有一份实现。
    """
    runner = _SubagentRunner(llm, workspace_root)
    return [
        SubagentTool(runner),
        SpawnAgentTool(runner),
        ListAgentsTool(runner),
        WaitAgentTool(runner),
        CloseAgentTool(runner),
    ]
