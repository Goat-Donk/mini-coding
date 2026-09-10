"""ask_user：请用户澄清（MiniCode `awaitUser` 语义移植）。

**这一项的设计要点不是工具本身**（它几乎是空实现），而是「把打断建模成
数据标志，而不是阻塞式控制流」：工具只声明意图 `await_user=True`，由
`QueryEngine` 决定回合语义（结束本轮并把问题交给用户），工具自己**不阻塞等人**。
这样换来两件事：

1. 「人怎么进来」与工具解耦 —— CLI 复用已有的 `--resume "…"` 动线（它会把
   位置参数作为一条 user 消息追加进会话），工具与循环都不需要知道有"人"；
2. headless 通路不会挂住 —— 但我们的做法更硬：**不注册**（见下）。

**为什么不进 `ToolRegistry.default()`**：`eval/runner.py` 用的正是 `default()`，
而 headless 评测里没有人能回答。模型一旦提问，eval 就提前终止 —— 完成率被一个
「没人在那儿」的机制拉低，而且是**静默的**（judge 只跑测试，只会说"没修好"）。
所以它与 `SubagentTool` 一样，在两个交互入口各自注册。

判据（值得复用到下一个工具）：**只依赖 `workspace_root` 的零副作用工具 → 进
`default()` 元组（一处生效、无漂移）；需要运行时对象（llm）、外部配置
（mcp.json）或交互通道（人）的 → 分入口注册。**
"""
from __future__ import annotations

from pydantic import BaseModel, Field

from agent.tools.base import Tool, ToolContext, ToolResult


class AskUserInput(BaseModel):
    question: str = Field(min_length=1, description="要问用户的问题，一句话说清并带必要上下文")
    options: list[str] = Field(
        default_factory=list,
        description="可选的候选回答（拿不准用户偏好时给几个选项，没有就留空）",
    )


class AskUserTool(Tool):
    name = "ask_user"
    description = (
        "暂停本轮并向用户提问。用于：需求有歧义、缺少只有用户知道的信息、"
        "或有不可逆的选择需要人拍板。提问后本轮结束，用户回答后任务继续。"
    )
    input_model = AskUserInput

    @classmethod
    def is_read_only(cls) -> bool:
        """**保持 False**（默认值）——这条是刻意的。

        只读工具会进并发线程池（`loop._execute_tool_calls`），而「提问 = 本轮
        终止」是个串行决策：把它放进并发批会让"本轮到此为止"的语义无法表达。
        提问本身没有副作用，但它**改变控制流**，所以按可写工具走串行路径。
        """
        return False

    def execute(self, args: AskUserInput, ctx: ToolContext) -> ToolResult:
        # output 就是问题原文：它作为 tool 结果进会话，模型下一轮看到人类的回答时
        # 上下文是「我问了 X」→「人答了 Y」，接得上。所以**不需要**再追加一条
        # assistant 消息复述问题（那只是同一条信息的第二次出现）。
        text = args.question
        if args.options:
            text += "\n候选: " + " / ".join(args.options)
        return ToolResult.ok(
            text,
            data={"question": args.question, "options": list(args.options)},
            await_user=True,
        )


def build_ask_tool() -> AskUserTool:
    """两个入口共用的**唯一**构造点（同 `hooks.default_engine()` 的模式）。

    本函数目前没有参数、看着像废话，但它存在的理由是接线漂移：这个项目已经
    犯过三次同一形状的错（hooks 曾整体漏接 CLI、CLI 曾漏接 permissions、
    tool_result_store 三个入口全没接）。把「谁把它造出来」收成一处之后，
    将来要给它加配置（比如要不要带候选答案的默认值）只会改这里，不会出现
    「CLI 加了、控制台没加」。
    """
    return AskUserTool()
