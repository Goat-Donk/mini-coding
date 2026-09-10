"""M4-2 tests: agent/tools/subagent.py（research 子代理，独立上下文 + 只读受限）。"""
from pathlib import Path

from agent.llm import LLMResult, MockLLM
from agent.loop import QueryEngine
from agent.tools.base import ToolRegistry
from agent.tools.subagent import SUBAGENT_SYSTEM_PROMPT, SubagentTool


def make_engine(tmp_path: Path, llm: MockLLM) -> QueryEngine:
    registry = ToolRegistry.default(tmp_path)
    registry.register(SubagentTool(llm, tmp_path))
    return QueryEngine(llm, registry, workspace_root=tmp_path)


def test_subagent_runs_and_returns_report(tmp_path):
    """主循环调 subagent → 子代理独立跑 glob + 结论 → 报告回主上下文。"""
    (tmp_path / "README.md").write_text("# Demo\n", encoding="utf-8")
    llm = MockLLM.script(
        MockLLM.tool("subagent", {"task": "探索并总结仓库结构"}).responses[0],
        MockLLM.tool("glob", {"pattern": "**/*"}).responses[0],   # 子代理步 1
        MockLLM.text("子代理结论: 仓库只有一个 README").responses[0],  # 子代理步 2
        MockLLM.text("主代理最终总结").responses[0],                  # 主循环步 2
    )
    engine = make_engine(tmp_path, llm)
    result = engine.run("总任务")
    assert result.terminated_reason == "completed"
    assert result.final_text == "主代理最终总结"
    # 子代理调用被记录进主轨迹（tool_call success），子步骤的 glob 也被执行
    names = [(ev["name"], ev["success"]) for ev in result.events if ev["type"] == "tool_call"]
    assert ("subagent", True) in names
    # 主轨迹只有 3 个事件：子代理内部步骤不进主上下文（独立 state/events）
    assert len(result.events) == 3


def test_subagent_only_read_tools(tmp_path):
    """子代理只能看到只读工具白名单；且不改动工作区文件。"""
    (tmp_path / "a.txt").write_text("hello\n", encoding="utf-8")
    before = {p.name for p in tmp_path.rglob("*") if p.is_file()}

    captured: dict = {}

    def sub_chat(messages, tools):
        captured["tools"] = [t["function"]["name"] for t in tools]
        return LLMResult(content="子代理完成", tool_calls=[])

    llm = MockLLM.script(
        MockLLM.tool("subagent", {"task": "调查"}).responses[0],
        sub_chat,
        MockLLM.text("主代理完成").responses[0],
    )
    result = make_engine(tmp_path, llm).run("总任务")
    assert result.terminated_reason == "completed"
    assert set(captured["tools"]) <= {"glob", "grep", "read"}
    assert "write" not in captured["tools"] and "bash" not in captured["tools"]
    # 子代理没有写任何文件
    after = {p.name for p in tmp_path.rglob("*") if p.is_file()}
    assert after == before


def test_subagent_max_steps_caps(tmp_path):
    """子代理 max_steps=3：3 次工具调用后中止，如实回喂主循环。"""
    llm = MockLLM.script(
        MockLLM.tool("subagent", {"task": "调查", "max_steps": 3}).responses[0],
        MockLLM.tool("glob", {"pattern": "**/*"}).responses[0],
        MockLLM.tool("glob", {"pattern": "**/*.py"}).responses[0],
        MockLLM.tool("glob", {"pattern": "**/*.md"}).responses[0],
        MockLLM.text("主代理收尾").responses[0],
    )
    engine = make_engine(tmp_path, llm)
    result = engine.run("总任务")
    assert result.terminated_reason == "completed"
    assert result.final_text == "主代理收尾"
    # 子代理中止被记录为失败工具调用（不假装成功），主模型可据信息调整
    sub_ev = next(
        ev for ev in result.events
        if ev["type"] == "tool_call" and ev["name"] == "subagent"
    )
    assert sub_ev["success"] is False


def test_subagent_system_prompt_mandates_read_only():
    """子代理 system prompt 明确只读定位（不得含 bash/write 暗示）。"""
    assert "只读" in SUBAGENT_SYSTEM_PROMPT
    assert "不能修改任何文件" in SUBAGENT_SYSTEM_PROMPT


def test_restricted_registry_never_recursive(tmp_path):
    """白名单构建：subagent/bash/write 不给；未知名字静默忽略。"""
    tool = SubagentTool(MockLLM.text("x"), tmp_path)
    reg = tool._restricted_registry(["glob", "subagent", "write", "read", "bash"])
    assert reg.names() == ["glob", "read"]  # 保持请求顺序
    assert tool._restricted_registry(["bash", "subagent"]).names() == []


def test_subagent_registered_as_serial_tool(tmp_path):
    """注册后 schema 完整；标记非只读（串行执行，避免嵌套并发）。"""
    llm = MockLLM.text("ok")
    registry = ToolRegistry.default(tmp_path)
    registry.register(SubagentTool(llm, tmp_path))
    assert "subagent" in registry.names()
    assert registry.get("subagent").is_read_only() is False
    sub_schema = next(
        s for s in registry.schemas()
        if s["function"]["name"] == "subagent"
    )
    params = sub_schema["function"]["parameters"]["properties"]
    assert "task" in params
    assert params["tools"]["type"] == "array"  # tools 字段存在（default 不序列化进 schema）
