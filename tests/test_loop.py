"""M1-6 tests: agent/loop.py（QueryEngine 循环）。全部用 MockLLM，无网络。"""
from pathlib import Path

import pytest

from agent.llm import LLMResult, MockLLM, ToolCall, Usage
from agent.loop import DEFAULT_SYSTEM_PROMPT, QueryEngine
from agent.tools.base import ToolRegistry


def make_engine(tmp_path: Path, llm: MockLLM, **kwargs) -> QueryEngine:
    return QueryEngine(
        llm,
        ToolRegistry.default(tmp_path),
        workspace_root=tmp_path,
        **kwargs,
    )


def test_simple_answer(tmp_path):
    engine = make_engine(tmp_path, MockLLM.text("完成了", usage=Usage(prompt_tokens=5, completion_tokens=3)))
    result = engine.run("总结一下")
    assert result.terminated_reason == "completed"
    assert result.steps == 1
    assert result.final_text == "完成了"
    assert result.usage.prompt_tokens == 5
    assert len(result.events) == 1
    assert result.events[0]["type"] == "llm_call"


def test_tool_then_answer(tmp_path):
    (tmp_path / "a.txt").write_text("hello\n", encoding="utf-8")
    engine = make_engine(
        tmp_path,
        MockLLM.script(
            MockLLM.tool("read", {"path": "a.txt"}).responses[0],
            LLMResult(content="读到了"),
        ),
    )
    result = engine.run("读 a.txt")
    assert result.terminated_reason == "completed"
    assert result.steps == 2
    # 消息历史包含工具结果消息
    tool_messages = [m for m in result.events if m["type"] == "tool_call"]
    assert len(tool_messages) == 1
    assert tool_messages[0]["name"] == "read"
    assert tool_messages[0]["success"] is True


def test_unknown_tool_recovered(tmp_path):
    """模型先调未知工具 → 收到"未知工具"fail 回喂 → 再给文本答案 → 完成。"""
    seen = {}

    def unknown_then_answer(messages, tools):
        # 第二次调用时断言：错误确实回喂给了模型
        if any(m["role"] == "tool" for m in messages):
            seen["recovered"] = messages[-1]["content"]
        return LLMResult(content="已修复路径")

    engine = make_engine(
        tmp_path,
        MockLLM.script(
            LLMResult(content=None, tool_calls=[ToolCall(id="c1", name="no_such_tool", arguments={})]),
            unknown_then_answer,
        ),
    )
    result = engine.run("试试未知工具")
    assert result.terminated_reason == "completed"
    assert seen.get("recovered")
    assert "未知工具" in seen["recovered"]
    assert "no_such_tool" in seen["recovered"]


def test_read_only_concurrent(tmp_path):
    """一次返回 2 个只读工具调用 → 两者都执行且结果都回填。"""
    (tmp_path / "a.txt").write_text("AAA\n", encoding="utf-8")
    (tmp_path / "b.txt").write_text("BBB\n", encoding="utf-8")
    engine = make_engine(
        tmp_path,
        MockLLM.script(
            LLMResult(
                content=None,
                tool_calls=[
                    ToolCall(id="c1", name="read", arguments={"path": "a.txt"}),
                    ToolCall(id="c2", name="read", arguments={"path": "b.txt"}),
                ],
            ),
            LLMResult(content="都读完了"),
        ),
        max_steps=5,
    )
    result = engine.run("读两个文件")
    assert result.terminated_reason == "completed"
    calls = [e for e in result.events if e["type"] == "tool_call"]
    assert [c["name"] for c in calls] == ["read", "read"]
    assert all(c["success"] for c in calls)
    # 两个结果都回填到消息（id 对应）
    tool_msgs = [m for m in result.events if m["type"] == "tool_call"]
    assert len(tool_msgs) == 2


def test_max_steps(tmp_path):
    tool_result_llm = MockLLM.tool("glob", {"pattern": "*.txt"}).responses[0]
    engine = make_engine(
        tmp_path,
        MockLLM([tool_result_llm] * 20),
        max_steps=3,
        loop_detection_window=10,  # 排除循环检测干扰
    )
    result = engine.run("一直调工具")
    assert result.terminated_reason == "max_steps"
    assert result.steps == 3
    assert "最大步数" in result.final_text


def test_loop_detected(tmp_path):
    tool_result_llm = MockLLM.tool("glob", {"pattern": "*.txt"}).responses[0]
    engine = make_engine(
        tmp_path,
        MockLLM([tool_result_llm] * 20),
        max_steps=25,
        loop_detection_window=4,
    )
    result = engine.run("重复循环")
    assert result.terminated_reason == "loop_detected"
    assert "重复循环" in result.final_text


def test_e2e_real_files(tmp_path):
    """脚本化工具链在真实文件上跑通：glob → read → edit → 文本回答。"""
    (tmp_path / "a.txt").write_text("hello\n", encoding="utf-8")
    engine = make_engine(
        tmp_path,
        MockLLM.script(
            MockLLM.tool("glob", {"pattern": "*.txt"}).responses[0],
            MockLLM.tool("read", {"path": "a.txt"}).responses[0],
            MockLLM.tool("edit", {"path": "a.txt", "old_string": "hello", "new_string": "hello world"}).responses[0],
            LLMResult(content="已修改并验证"),
        ),
        max_steps=10,
    )
    result = engine.run("把 hello 改成 hello world")
    assert result.terminated_reason == "completed"
    assert result.steps == 4
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "hello world\n"
    assert result.final_text == "已修改并验证"


def test_default_system_prompt_has_placeholder():
    assert "{repo_memory_block}" in DEFAULT_SYSTEM_PROMPT


def test_empty_response_recovered(tmp_path):
    """M1-9：模型第一次返回空白文本 → push continuation prompt 重试 → 第二次正常回答。"""
    engine = make_engine(
        tmp_path,
        MockLLM.script(
            LLMResult(content="", usage=Usage(prompt_tokens=1, completion_tokens=0)),
            LLMResult(content="完成", usage=Usage(prompt_tokens=1, completion_tokens=1)),
        ),
    )
    result = engine.run("任务")
    assert result.terminated_reason == "completed"
    assert result.steps == 2
    assert result.final_text == "完成"
    retries = [e for e in result.events if e["type"] == "empty_response_retry"]
    assert len(retries) == 1
    assert retries[0]["attempt"] == 1


def test_empty_response_gives_up(tmp_path):
    """M1-9：空响应超过重试上限 → 按 completed（空结论）结束，不无限重试。"""
    empty = LLMResult(content="", usage=Usage(prompt_tokens=1, completion_tokens=0))
    engine = make_engine(tmp_path, MockLLM([empty] * 10), empty_response_retries=2)
    result = engine.run("任务")
    assert result.terminated_reason == "completed"
    assert result.steps == 3  # 初始 1 次 + 2 次重试
    retries = [e for e in result.events if e["type"] == "empty_response_retry"]
    assert len(retries) == 2
    assert result.final_text == ""
