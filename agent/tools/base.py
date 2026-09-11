"""工具基类与注册表。

设计对齐 Claude Code 的工具接口：每个工具声明 {name, description, input_schema}，
由 pydantic 输入模型自动生成 OpenAI function schema；run() 统一做
校验→执行→计时→异常兜底。ToolRegistry 集中注册，供 loop 查询
只读/可写工具分组（决定并发还是串行）。
"""
from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, ClassVar, Iterable, Type

from pydantic import BaseModel, ValidationError

if TYPE_CHECKING:
    from agent.state import AgentState
    from agent.subagents import AgentWorkers


@dataclass
class ToolResult:
    """工具执行结果。output 是给模型看的文本（已截断），data 是结构化数据。"""

    success: bool
    output: str
    data: dict | None = None
    error: str | None = None
    duration_ms: int = 0
    # 工具声明「本轮到此为止，等人回答」（目前只有 ask_user）。**是数据标志，不是
    # 阻塞**：工具自己不等人，由 QueryEngine 决定回合语义 —— 于是 headless 通路
    # 只要不注册这个工具，就完全不受影响，不需要在循环里写任何 if headless。
    await_user: bool = False

    @staticmethod
    def ok(
        output: str, data: dict | None = None, *, await_user: bool = False
    ) -> "ToolResult":
        return ToolResult(
            success=True, output=output, data=data, await_user=await_user
        )

    @staticmethod
    def fail(error: str, output: str | None = None) -> "ToolResult":
        """失败结果；output 默认含错误信息，回喂模型可自修复。"""
        return ToolResult(
            success=False,
            output=output if output is not None else f"工具执行失败: {error}",
            error=error,
        )


@dataclass
class ToolContext:
    """工具执行环境。所有路径操作必须在 workspace_root 沙箱内。"""

    workspace_root: Path
    cwd: Path | None = None
    emitter: Callable[[dict], None] | None = None   # 轨迹事件回调（= state.emitter）
    permissions: object | None = None               # M2 接入
    hooks: object | None = None                     # M2 接入
    # M8：会话状态（`AgentState`）。工具要写"跨回合的状态"（当前只有
    # `update_plan` 写 `state.plan`）时经由它，而不是各自去摸全局。
    # 用字符串注解 + TYPE_CHECKING：base 是 tools 包的底座，不该在运行期
    # 依赖 agent.state（虽然目前没有环，但底座依赖上层是反的）。
    state: "AgentState | None" = None
    # M9-7：本回合的子代理管理器（`agent.subagents.AgentWorkers`）。**引擎每回合
    # 新建一个**，所以工具拿到的是"这一个回合的" —— 子代理活不过派发它的那一个回合。
    # 读它的只有子代理工具族（`agent/tools/subagent.py`），它们**只在 `ctx.workers`
    # 非 None 时才工作**、为 None 时如实返回失败（同 `DeclareGoalDoneTool` 的
    # 「不静默降级」：接了引擎却没接管理器是一种接线缺口，不能装作没事）。
    workers: "AgentWorkers | None" = None

    def __post_init__(self) -> None:
        if self.cwd is None:
            self.cwd = self.workspace_root
        self.workspace_root = Path(self.workspace_root).resolve()
        self.cwd = Path(self.cwd).resolve()


class Tool(ABC):
    """工具基类。子类声明 name/description/input_model 并实现 execute。"""

    name: ClassVar[str]
    description: ClassVar[str]
    input_model: ClassVar[Type[BaseModel]]

    @classmethod
    def is_read_only(cls) -> bool:
        """只读工具可并发执行；默认 False（可写）。"""
        return False

    @classmethod
    def is_external(cls) -> bool:
        """是否来自第三方（如 MCP server）。

        外部工具不受 workspace 沙箱约束，权限引擎默认**不放行**它们，
        必须在配置里显式列出才允许（见 permissions.py 的 external 规则）。
        """
        return False

    def needs_permission(self, arguments: dict) -> bool:
        """工具自声明是否需要人工确认（M1：bash 危险命令返回 True）。"""
        return False

    def preview(self, arguments: dict, ctx: ToolContext) -> str | None:
        """声明这次调用**将要做什么**，供权限确认在动手之前查看（M9-1）。

        与 `execute` 的分工：`execute` 是"做"，`preview` 是"把要做的事说清楚"。
        默认返回 None（没有可预览的东西）—— 目前只有 `write` / `edit` 实现它，
        因为**整份覆盖**和**精确替换**是两类误伤了就收不回来的动作，人要看的
        正是"改完会变成什么样"。

        **必须是纯函数：绝不写盘、不改状态。** 它的产物会进入人工确认文案，
        调用方（`_gate_and_run`）不会、也没办法为它准备回滚。

        为什么放在工具上，而不是让权限引擎拿 `arguments` 自己算：edit 的
        "唯一匹配"语义属于工具（见 `files.EditTool`）。权限引擎要是重算一遍，
        就有了**两个真相源**，迟早出现"预览说能改、执行说匹配不唯一"——
        而那时人已经照着预览点过允许了。越界与匹配失败的口径也只能有一份。

        参数是**原始 dict** 而非校验过的模型：门禁链跑在 `run()` 之前，
        此时还没有 pydantic 校验，所以实现方要自己容忍缺字段（返回 None 即可）。
        """
        return None

    def schema(self) -> dict:
        """OpenAI function calling 格式 schema（由 pydantic 输入模型自动生成）。"""
        raw = self.input_model.model_json_schema()
        # 清理 pydantic 噪音；扁平参数不应产生 $defs
        raw.pop("title", None)
        raw.pop("$defs", None)
        properties = {k: {kk: vv for kk, vv in v.items() if kk != "title"}
                      for k, v in (raw.get("properties") or {}).items()}
        raw["properties"] = properties
        raw.setdefault("properties", {})
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": raw,
            },
        }

    def run(self, arguments: dict, ctx: ToolContext) -> ToolResult:
        """统一入口：pydantic 校验 → execute → 计时 → 异常兜底。"""
        t0 = time.perf_counter()
        try:
            args = self.input_model(**arguments)
        except ValidationError as exc:
            details = []
            for err in exc.errors():
                loc = ".".join(str(part) for part in err["loc"])
                details.append(f"{loc}: {err['msg']}")
            return ToolResult.fail(
                error=f"参数校验失败: {'; '.join(details)}",
            )
        try:
            result = self.execute(args, ctx)
        except Exception as exc:  # 工具内部未预期异常，兜底回喂模型
            result = ToolResult.fail(error=f"{type(exc).__name__}: {exc}")
        result.duration_ms = int((time.perf_counter() - t0) * 1000)
        return result

    @abstractmethod
    def execute(self, args: BaseModel, ctx: ToolContext) -> ToolResult: ...


class ToolRegistry:
    """工具注册中心：name 唯一，提供 schema 列表与读写分组。"""

    def __init__(self, tools: Iterable[Tool] = ()) -> None:
        self._tools: dict[str, Tool] = {}
        for tool in tools:
            self.register(tool)

    def register(self, tool: Tool) -> None:
        if not tool.name:
            raise ValueError("tool name 不能为空")
        if tool.name in self._tools:
            raise ValueError(f"工具名重复: {tool.name}")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool:
        return self._tools[name]

    def __contains__(self, name: str) -> bool:
        return name in self._tools

    def schemas(self) -> list[dict]:
        return [tool.schema() for tool in self._tools.values()]

    def names(self) -> list[str]:
        return list(self._tools)

    def read_only(self) -> list[Tool]:
        return [tool for tool in self._tools.values() if tool.is_read_only()]

    def writable(self) -> list[Tool]:
        return [tool for tool in self._tools.values() if not tool.is_read_only()]

    def external(self) -> list[Tool]:
        """第三方工具（MCP 等）。权限引擎对它们默认不放行，须显式授权。"""
        return [tool for tool in self._tools.values() if tool.is_external()]

    @classmethod
    def default(cls, workspace_root: Path) -> "ToolRegistry":
        """默认工具集：bash + read/write/edit/glob/grep（M4 加 subagent，M8 加 plan，M9 加 web）。

        延迟导入避免循环依赖（base 是 tools 包的底座）。

        `update_plan` 在这里而不是按入口注册：它只依赖 `ctx.state`（引擎总会填），
        零副作用（不碰工作区、不需要外部配置、不需要人），符合「进 default()」的
        判据。副作用要说清楚：`eval/runner.py` 用 `default()`，所以评测里的 agent
        也会拿到它 —— 那是能力不是负担（eval 是单发任务，写不写计划都不失真），
        而且它**不需要**任何外部配合，不存在 `ask_user` 那种"没人在就挂住"的问题。

        `web_fetch` / `web_search` 同样进这里，但理由不同，且**代价要说明**：
        它们让 `permissions.py` 污染天花板里「网络外发」那一类第一次有了真实对象
        （判据见 `_irreversible_kind`）—— 这是本项的主要目的。代价是评测也拿到了
        联网能力，**因此 README 里的 token/耗时/缓存命中率数字与 M9-2 之前不可比**，
        必须重跑 `python -m eval.runner` 后再引用（已记进 TASKS.md 的 M9-2 条目）。
        """
        from agent.tools.bash import BashTool
        from agent.tools.files import EditTool, GlobTool, GrepTool, ReadTool, WriteTool
        from agent.tools.plan import UpdatePlanTool
        from agent.tools.web import build_web_tools

        registry = cls()
        for tool_cls in (
            BashTool, ReadTool, WriteTool, EditTool, GlobTool, GrepTool, UpdatePlanTool,
        ):
            registry.register(tool_cls())
        for tool in build_web_tools():
            registry.register(tool)
        return registry
