"""update_plan：agent 自己的任务计划清单，落盘、跨回合（MiniCode 计划持久化移植）。

**先划清一件事**：这里的 plan 是 **agent 给自己排的任务计划**，与仓库根的
`TASKS.md`（人类开发者的任务清单）**毫无关系**。两者都叫"计划"，混起来会让
后来的人以为 agent 在改 TASKS.md。

只吸收参考实现的两件事（它的两套待办系统都没接好：`todo_write.py` 有真 bug ——
`_tasks.clear()` 在遍历之前执行，更新分支是死代码；`task_tracker.py` 无调用方）：

1. **每次传完整列表**的工具契约；
2. **落盘跨回合** —— 存在 `state.plan`，随检查点一起走。

三条设计决定，逐条都有理由：

**① 两个平行数组，不是嵌套对象列表。** 项目硬约束是「工具参数扁平化、schema 无
`$defs`」，而 `base.py` 的 `raw.pop("$defs", None)` 会把嵌套模型**静默**削成坏
schema（模型收到的参数说明是错的，但没有任何报错）。`list[str]` 生成内联 array，
安全。

**② 全量覆盖，不是增量。** 增量需要工具自己维护"第 3 条是哪个"的对应关系，而
工具的每次调用都是独立的 —— 一旦模型对序号的记忆与工具不一致，改的就是错的
那条，而且不报错。全量覆盖让"模型想表达什么"没有歧义，状态也只有一个写入者。

**③ 不把计划快照注入每轮消息。** 参考实现在第 5 步做这件事（把当前计划贴进
对话）。在我们的 cache-aware 布局下这是负收益：计划每变一次就改一段消息 → 那段
之后的**前缀缓存全部失效**。计划的可见性本来就够 —— `update_plan` 的工具结果
（渲染出的清单）就在会话里，模型自己刚刚写的那份还在眼前。
"""
from __future__ import annotations

from pydantic import BaseModel, Field

from agent.tools.base import Tool, ToolContext, ToolResult

#: 合法状态。**刻意只有三个**：清单每次都是重新全量提交的，"放弃/作废"直接把它
#: 从清单里删掉就行，再加一个状态等于给同一件事两种说法（而两种说法之间不一致时
#: 没人知道该信哪个）。
PLAN_STATUSES = ("pending", "in_progress", "done")


class UpdatePlanInput(BaseModel):
    items: list[str] = Field(
        min_length=0,
        description=(
            "计划条目的文本，**完整清单**（不是只传变化的那几条）。传空数组 = 清空计划。"
        ),
    )
    statuses: list[str] = Field(
        min_length=0,
        description=(
            "与 items **一一对应**的状态，取值只能是 pending / in_progress / done。"
        ),
    )


def render_plan(plan: list[dict]) -> str:
    """把计划渲染成清单文本。

    这里是**唯一**一处定义"计划长什么样"：工具结果、`--plan` 的打印、控制台回放
    都用它。分散成三份的话，三处的格式会各自漂移，而"计划显示得对不对"没人会
    专门去测（它不影响任何功能，只影响人看不看得懂）。
    """
    if not plan:
        return "计划清单已清空。"
    done = sum(1 for item in plan if item.get("status") == "done")
    lines = [f"计划清单（{done}/{len(plan)} 完成）"]
    for index, item in enumerate(plan, 1):
        lines.append(f"  {index}. [{item.get('status', '?')}] {item.get('text', '')}")
    return "\n".join(lines)


class UpdatePlanTool(Tool):
    name = "update_plan"
    description = (
        "声明或更新**本任务自己的**计划清单。多步任务开始时先把清单列出来，之后每"
        "完成一步就用**完整清单**再调一次（把刚做完那条改成 done）。计划会写进会话"
        "检查点，所以中途被打断、或换会话续跑时不会丢。"
    )
    input_model = UpdatePlanInput

    @classmethod
    def is_read_only(cls) -> bool:
        """**不是只读** —— 它写 `state.plan`。

        只读标记的含义是"可以和别的只读工具并发跑"（`loop._execute_tool_calls`
        按它决定并发还是串行）。让一个写状态的工具混进并发批，就是让"这轮的执行
        顺序"变得不确定，而计划的意义恰恰依赖于顺序。
        """
        return False

    def execute(self, args: UpdatePlanInput, ctx: ToolContext) -> ToolResult:
        items = [text.strip() for text in args.items]
        statuses = [status.strip() for status in args.statuses]

        if len(items) != len(statuses):
            # 回喂具体差多少条：模型看到"items 3 条 / statuses 2 条"就能自己改对，
            # 只说"参数不合法"它会原样重试（这是我们自己的工具，不是外部 API，
            # 没有理由让模型去猜）。
            return ToolResult.fail(
                f"items 与 statuses 必须一一对应：收到 {len(items)} 条文本、"
                f"{len(statuses)} 个状态"
            )
        if any(not text for text in items):
            return ToolResult.fail("计划条目的文本不能为空")
        unknown = [status for status in statuses if status not in PLAN_STATUSES]
        if unknown:
            return ToolResult.fail(
                f"非法状态 {unknown}，只能是 {' / '.join(PLAN_STATUSES)}"
            )

        plan = [
            {"text": text, "status": status} for text, status in zip(items, statuses)
        ]
        state = ctx.state
        if state is None or not hasattr(state, "plan"):
            # **不静默降级。** 接不上会话状态 = 计划落不了盘 = 跨回合这件事整个失效，
            # 而那正是这个工具存在的理由。悄悄返回一份渲染好的清单会让它看起来
            # 一切正常（模型照样往下做），但计划其实已经丢了。
            # 这是接线缺口（本项目最高发的一类缺陷），所以报错要响。
            return ToolResult.fail(
                "未接上会话状态，计划无法落盘 —— 本次运行不会保留计划"
            )
        state.plan = plan
        return ToolResult.ok(render_plan(plan), data={"plan": plan})
