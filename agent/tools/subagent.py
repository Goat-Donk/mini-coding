"""research 子代理工具（M4-2，对应 Claude Code 的 SubAgent）。

主循环把"探索并总结/调查某个问题"类工作**外包**给子代理（CC SubAgent 经济学：
把 (X+Y)×N 的深度探索外包出去，只把结论 Z tokens 带回主上下文，主上下文保持干净）。

- **独立上下文**：子代理用全新的 AgentState + 独立 system prompt，主循环的
  messages/usage/compact 全部不共享。
- **受限只读 registry**：默认只给 glob/grep/read（可指定），且永远不含 subagent
  自身 → 天然禁止递归嵌套。
- **复用 QueryEngine**：同一 BaseLLM、同一循环逻辑，只是 registry/system prompt/
  max_steps 不同——代码零重复。
- **返回结构化报告**：`ToolResult.ok(结论)`；子代理失败/超步也如实回喂
  （不假装成功，主模型可据信息调整策略）。
"""
from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

from agent.loop import QueryEngine
from agent.tools.base import Tool, ToolContext, ToolResult, ToolRegistry
from agent.tools.files import GlobTool, GrepTool, ReadTool

MAX_SUBAGENT_STEPS = 10
# 子代理可用的只读工具白名单（受限集；写工具/ bash/ subagent 一律不给）
_READ_ONLY_TOOLS = {
    "glob": GlobTool,
    "grep": GrepTool,
    "read": ReadTool,
}

SUBAGENT_SYSTEM_PROMPT = """\
你是 CodeAgent 的研究子代理。你的任务是【只读探索】当前工作目录并回答用户的调查问题。

工作方式：
- 先用 glob/grep/read 探索仓库，读到真实内容后再作答；不要臆测或编造。
- 你的工具全部只读，不能修改任何文件；也没有 bash，无法执行命令。
- 任务完成后，输出结构化结论：关键发现 + 依据（文件/行号）+ 对主代理的建议。

硬性约束：
- 只能访问工作目录（沙箱）内路径，禁止越界。
- 不要假装完成：必须基于实际读到的内容回答。
"""


class SubagentInput(BaseModel):
    task: str = Field(description="子代理要调查/总结的问题")
    tools: list[str] = Field(
        default_factory=lambda: ["glob", "grep", "read"],
        description="允许子代理使用的只读工具名（白名单 glob/grep/read）",
    )
    max_steps: int = Field(default=10, description="子代理最大步数（≤10）")


class SubagentTool(Tool):
    name = "subagent"
    description = (
        "启动一个只读研究子代理，在独立上下文中探索仓库并返回结构化结论。"
        "适合『总结架构/调查问题/搜索实现细节』类任务：把深度探索外包出去，"
        "只把结论带回主上下文。子代理只用只读工具，不会修改文件。"
    )
    input_model = SubagentInput

    def __init__(self, llm, workspace_root: Path) -> None:
        self.llm = llm
        self.workspace_root = Path(workspace_root).resolve()

    def execute(self, args: SubagentInput, ctx: ToolContext) -> ToolResult:
        registry = self._restricted_registry(args.tools)
        engine = QueryEngine(
            self.llm,
            registry,
            workspace_root=self.workspace_root,
            system_prompt=SUBAGENT_SYSTEM_PROMPT,
            max_steps=min(max(1, args.max_steps), MAX_SUBAGENT_STEPS),
        )
        result = engine.run(args.task, cwd=ctx.cwd)

        if result.terminated_reason == "completed":
            return ToolResult.ok(
                result.final_text or "（子代理无结论）",
                data={"steps": result.steps, "reason": "completed"},
            )
        # 失败/超步/循环检测：如实回喂，主模型可据此调整
        return ToolResult.fail(
            error=f"子代理 {result.terminated_reason}",
            output=(
                f"[子代理 {result.terminated_reason}，{result.steps} 步] "
                + (result.final_text or "无结论")
            ),
        )

    @staticmethod
    def _restricted_registry(requested: list[str]) -> ToolRegistry:
        """按白名单构建受限 registry；未知/不可用的名字静默忽略。

        永不包含 subagent 自身 → 子代理无法再递归启动子代理。
        """
        registry = ToolRegistry()
        for name in requested:
            cls = _READ_ONLY_TOOLS.get(name)
            if cls is not None:
                registry.register(cls())
        return registry
