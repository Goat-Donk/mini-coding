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
from typing import Any, Callable, ClassVar, Iterable, Type

from pydantic import BaseModel, ValidationError


@dataclass
class ToolResult:
    """工具执行结果。output 是给模型看的文本（已截断），data 是结构化数据。"""

    success: bool
    output: str
    data: dict | None = None
    error: str | None = None
    duration_ms: int = 0

    @staticmethod
    def ok(output: str, data: dict | None = None) -> "ToolResult":
        return ToolResult(success=True, output=output, data=data)

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
    settings: dict = field(default_factory=dict)
    emitter: Callable[[dict], None] | None = None   # 轨迹事件回调（M3 接 session）
    permissions: object | None = None               # M2 接入
    hooks: object | None = None                     # M2 接入

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

    def needs_permission(self, arguments: dict) -> bool:
        """工具自声明是否需要人工确认（M1：bash 危险命令返回 True）。"""
        return False

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

    @classmethod
    def default(cls, workspace_root: Path) -> "ToolRegistry":
        """默认工具集：bash + read/write/edit/glob/grep（M4 加 subagent）。

        延迟导入避免循环依赖（base 是 tools 包的底座）。
        """
        from agent.tools.bash import BashTool
        from agent.tools.files import EditTool, GlobTool, GrepTool, ReadTool, WriteTool

        registry = cls()
        for tool_cls in (BashTool, ReadTool, WriteTool, EditTool, GlobTool, GrepTool):
            registry.register(tool_cls())
        return registry
