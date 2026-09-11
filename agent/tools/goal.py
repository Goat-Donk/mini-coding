"""declare_goal_done：模型**声明**目标完成（唯一的目标工具，M9-6）。

**这个工具刻意不叫 `complete_goal`。** 它的 description 是这条能力在**唯一
常驻请求**（工具 schema）里的全部说明，而 `complete_goal` 读起来像"调用它 =
完成"—— 那正是本项要避免的误解。真实语义是：模型**声明**，运行时拿**人预先
给定的可执行命令**去跑，**退出码**说了算（见 `agent/goal.py` 的三态判定）。

**为什么只有一个工具**（没有 pause / clear / status 工具）：与 `clear_taint`
的纪律完全一致 —— **标记不由被标记者清除**。暂停、清空、换判据都是人的动作
（`/goal pause|resume|clear`）。理由不是"不信任模型"，是**判分权**：只要模型
能改判据或撤销目标，那套检查就退化成模型自己跟自己打分。

**为什么它进不了 `ToolRegistry.default()`**：eval 用的正是 `default()`，而
headless 会话里**没人能创建目标** → 模型只会看到一个永远失败的诱饵（和
`ask_user` 同一个形状：机制在、路径不存在）。按入口注册。
"""
from __future__ import annotations

from pydantic import BaseModel, Field

from agent.goal import GOAL_DONE, GOAL_PAUSED, goal_can_advance, render_goal
from agent.tools.base import Tool, ToolContext, ToolResult


class DeclareGoalDoneInput(BaseModel):
    summary: str = Field(
        min_length=1,
        description="一句话总结你做了什么来达成目标（会写进轨迹，给人看）",
    )
    evidence: list[str] = Field(
        default_factory=list,
        description=(
            "支撑这个判断的证据，每项一句话（如「pytest 全绿」「已改 textstat.py:12」）。"
            "**没有证据就别声明** —— 运行时随后会跑人给的检查命令，谎报会被当场拆穿。"
        ),
    )


class DeclareGoalDoneTool(Tool):
    name = "declare_goal_done"
    description = (
        "声明你**认为**当前目标已经完成。这只表示「可以来验了」，不代表目标已完成："
        "运行时随即会执行**人预先给定的**检查命令，退出码为 0 才算完成，非 0 会把命令"
        "输出回给你、让你继续修。**当前会话没有目标时不要调用它**（目标只能由人用 "
        "`/goal` 设定，你不能自造）。"
    )
    input_model = DeclareGoalDoneInput

    @classmethod
    def is_read_only(cls) -> bool:
        """**不是只读** —— 它写 `state.goal.declaration`。

        只读标记的含义是"可以和别的只读工具并发跑"。声明触发的是**随后一次真实
        的命令执行**（`_verify_goal`），把它混进并发批就是在并发里发起一次不该
        并发的动作。与 `ask_user` 同理，它改变的是**控制流**。
        """
        return False

    def execute(self, args: DeclareGoalDoneInput, ctx: ToolContext) -> ToolResult:
        state = ctx.state
        if state is None or not hasattr(state, "goal"):
            # 不静默降级：接不上 state = 声明根本传不到运行时 = 完成检查不会跑，
            # 而那样一来这个工具就退化成一个"看起来成功了"的空壳。这是本项目
            # 最高发的一类缺陷（接线缺口），所以报错要响。
            return ToolResult.fail(
                "未接上会话状态，完成声明无法送达运行时（本次声明不会触发检查）"
            )

        goal = state.goal
        if goal is None:
            # **模型不能自造目标。** 目标由人用 `/goal <目标> --check <命令>` 创建，
            # 判据也由人给 —— 允许模型自造等于把判分权交回给它。
            return ToolResult.fail(
                "当前会话没有目标，无法声明完成。目标只能由人用 "
                "`/goal <目标> --check <命令>` 设定。如果你认为任务已经做完，"
                "直接在结论里说明即可。"
            )
        if goal.status == GOAL_DONE:
            return ToolResult.fail(
                "这个目标已经完成（检查已通过），不需要再次声明。\n" + render_goal(goal)
            )
        if goal.status == GOAL_PAUSED:
            # 暂停期间不跑检查：暂停的语义就是"停掉自动推进、由人接手"，
            # 悄悄跑一次检查会让它变成一个没人知道的副作用。
            return ToolResult.fail(
                "目标当前是暂停状态，本次声明不会触发完成检查。"
                "需要继续请让人执行 `/goal resume`。\n" + render_goal(goal)
            )
        if not goal_can_advance(goal):
            # 兜底：将来多一个状态时，这里给出一句可读的话而不是静默通过。
            return ToolResult.fail(f"目标当前状态 {goal.status}，不能声明完成。")

        # ★ 写进 state 而不是 ToolResult 上的第二个标志。理由见模块 docstring
        # 与 `agent/loop.py::_run_loop` 的注释：ToolResult 在批结束时已经
        # 被 `compact_batch` 成字符串了，要带出第二个标志就得改
        # `_execute_tool_calls` 的返回类型 —— 那是四个入口共用的核心循环契约。
        goal.declaration = {
            "summary": args.summary,
            "evidence": list(args.evidence),
            "step": state.step,
        }
        return ToolResult.ok(
            "已记录你的完成声明。运行时现在会用**人预先给定的**检查命令验证一次：\n"
            f"    {goal.check_command}\n"
            "退出码为 0 才算完成；非 0 会把命令输出回给你，你可以继续修。",
            data={"declaration": dict(goal.declaration)},
        )


def build_goal_tools() -> list[Tool]:
    """目标工具的唯一构造点（同 `build_ask_tool` / `hooks.default_engine` 的模式）。

    这一点看着像废话，但它防的是接线漂移：本项目已经犯过三次同一形状的错
    （hooks 漏接 CLI、CLI 漏接 permissions、`tool_result_store` 三个入口全没接）。
    构造点收成一处之后，将来给它加配置只会改这里，不会出现"CLI 注册了、
    控制台没注册"。
    """
    return [DeclareGoalDoneTool()]
