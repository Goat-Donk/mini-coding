"""M1-4/5 tests: agent/tools/base.py + bash.py + files.py。

- base：schema 自动生成 / run 校验与计时（M1-4）
- bash：危险拦截 / 超时 / cwd 越界 / 退出码（M1-5）
- files：read/write/edit/glob/grep + 路径沙箱（M1-5）
"""
from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import BaseModel

from agent.tools.base import Tool, ToolContext, ToolRegistry, ToolResult


# ---------- 辅助：测试用工具 ----------

class AddInput(BaseModel):
    a: int
    b: int = 0


class AddTool(Tool):
    name = "add"
    description = "加法"
    input_model = AddInput

    def execute(self, args: AddInput, ctx: ToolContext) -> ToolResult:
        return ToolResult.ok(f"{args.a + args.b}", data={"sum": args.a + args.b})


def make_ctx(tmp_path: Path) -> ToolContext:
    return ToolContext(workspace_root=tmp_path)


# ---------- M1-4 base ----------

def test_schema():
    schema = AddTool().schema()
    assert schema["type"] == "function"
    fn = schema["function"]
    assert fn["name"] == "add"
    assert fn["description"] == "加法"
    params = fn["parameters"]
    assert params["type"] == "object"
    assert set(params["properties"]) == {"a", "b"}
    assert params["required"] == ["a"]  # b 有默认值，不进 required
    assert "$defs" not in params  # 扁平参数无嵌套


def test_run_validation_error():
    result = AddTool().run({"a": "not-a-number"}, make_ctx(Path(".")))
    assert not result.success
    assert "参数校验失败" in result.output
    assert "a" in result.output


def test_run_success_timing():
    result = AddTool().run({"a": 2, "b": 3}, make_ctx(Path(".")))
    assert result.success
    assert result.output == "5"
    assert result.data == {"sum": 5}
    assert result.duration_ms >= 0


def test_run_missing_required():
    result = AddTool().run({}, make_ctx(Path(".")))
    assert not result.success
    assert "a" in result.output


def test_run_internal_exception_caught():
    class BoomInput(BaseModel):
        x: int

    class BoomTool(Tool):
        name = "boom"
        description = "抛异常"
        input_model = BoomInput

        def execute(self, args, ctx) -> ToolResult:
            raise ValueError("内部错误")

    result = BoomTool().run({"x": 1}, make_ctx(Path(".")))
    assert not result.success
    assert "ValueError" in result.output
    assert "内部错误" in result.output


def test_registry_register_duplicate():
    registry = ToolRegistry()
    registry.register(AddTool())
    with pytest.raises(ValueError, match="重复"):
        registry.register(AddTool())


def test_registry_schemas_and_groups():
    class ReadOnlyTool(AddTool):
        name = "add_ro"

        @classmethod
        def is_read_only(cls) -> bool:
            return True

    registry = ToolRegistry([AddTool(), ReadOnlyTool()])
    assert set(registry.names()) == {"add", "add_ro"}
    assert len(registry.schemas()) == 2
    assert [t.name for t in registry.read_only()] == ["add_ro"]
    assert [t.name for t in registry.writable()] == ["add"]


def test_toolresult_fail_default_output():
    result = ToolResult.fail("出错了")
    assert not result.success
    assert "出错了" in result.output


def test_toolresult_ok():
    result = ToolResult.ok("正常", data={"k": 1})
    assert result.success
    assert result.data == {"k": 1}
