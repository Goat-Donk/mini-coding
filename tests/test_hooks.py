"""M2-2 tests: agent/hooks.py（Pre/PostToolUse 分发 + block-at-submit）。"""
from pathlib import Path

from agent.hooks import (
    HookBlock,
    HookContext,
    HookEngine,
    mark_tests_pass,
    require_tests_before_commit,
)
from agent.llm import LLMResult, MockLLM
from agent.loop import QueryEngine
from agent.tools.base import ToolContext, ToolRegistry, ToolResult


def make_engine(tmp_path: Path, llm: MockLLM, **kwargs) -> QueryEngine:
    return QueryEngine(
        llm,
        ToolRegistry.default(tmp_path),
        workspace_root=tmp_path,
        **kwargs,
    )


def test_mark_tests_pass_writes_marker(tmp_path):
    marker = mark_tests_pass(tmp_path)
    assert marker == (tmp_path / "data" / "tests_pass.marker")
    assert marker.exists()
    assert "tests pass" in marker.read_text(encoding="utf-8")


def test_require_tests_blocked_without_marker(tmp_path):
    engine = HookEngine([require_tests_before_commit(tmp_path)], workspace_root=tmp_path)
    block = engine.run_pre("bash", {"command": "git commit -m 'feat: x'"})
    assert block is not None
    assert isinstance(block, HookBlock)
    assert "测试" in block.reason
    assert block.hint and "pytest" in block.hint


def test_marker_unblocks(tmp_path):
    mark_tests_pass(tmp_path)
    engine = HookEngine([require_tests_before_commit(tmp_path)], workspace_root=tmp_path)
    assert engine.run_pre("bash", {"command": "git commit -m 'feat: x'"}) is None


def test_non_commit_command_passes(tmp_path):
    engine = HookEngine([require_tests_before_commit(tmp_path)], workspace_root=tmp_path)
    assert engine.run_pre("bash", {"command": "git status"}) is None
    assert engine.run_pre("bash", {"command": "git push origin main"}) is None  # 只拦 commit
    assert engine.run_pre("read", {"path": "a.txt"}) is None  # 非 bash 直接放行


def test_multiple_hooks_first_block_wins(tmp_path):
    called = []

    def h1(ctx):
        called.append("h1")
        return None  # 放行

    def h2(ctx):
        called.append("h2")
        return HookBlock(reason="第二个钩子阻断", hint="改道")

    engine = HookEngine([h1, h2], workspace_root=tmp_path)
    block = engine.run_pre("bash", {"command": "git commit -m x"})
    assert block is not None and block.reason == "第二个钩子阻断"
    assert called == ["h1", "h2"]


def test_run_post_receives_result(tmp_path):
    seen = {}

    def post(ctx):
        seen["tool"] = ctx.tool_name
        seen["success"] = ctx.result.success
        seen["event"] = ctx.event_name
        return "提示: 记得跑测试"

    engine = HookEngine(post_hooks=[post], workspace_root=tmp_path)
    hints = engine.run_post("bash", {"command": "git status"}, ToolResult.ok("ok"))
    assert hints == ["提示: 记得跑测试"]
    assert seen["tool"] == "bash"
    assert seen["success"] is True
    assert seen["event"] == "PostToolUse"


def test_loop_hook_blocks_commit(tmp_path):
    """git commit 被 hook 阻断 → 失败回喂模型（含 [hook 阻断]），agent 改道完成。"""
    seen = {}

    def then_answer(messages, tools):
        if any(m["role"] == "tool" for m in messages):
            seen["feedback"] = messages[-1]["content"]
        return LLMResult(content="先跑测试再提交")

    engine = make_engine(
        tmp_path,
        MockLLM.script(
            MockLLM.tool("bash", {"command": "git commit -m 'feat: x'"}).responses[0],
            then_answer,
        ),
        hooks=HookEngine([require_tests_before_commit(tmp_path)], workspace_root=tmp_path),
    )
    result = engine.run("提交代码")
    assert result.terminated_reason == "completed"
    calls = [e for e in result.events if e["type"] == "tool_call"]
    assert calls[0]["success"] is False
    assert "[hook 阻断]" in seen["feedback"]
    assert "pytest" in seen["feedback"]


def test_loop_hook_allows_after_marker(tmp_path):
    """marker 存在 → 阻断消失，git commit 真的执行。"""
    mark_tests_pass(tmp_path)
    engine = make_engine(
        tmp_path,
        MockLLM.script(
            MockLLM.tool("bash", {"command": "git commit -m 'feat: x'"}).responses[0],
            LLMResult(content="已提交"),
        ),
        hooks=HookEngine([require_tests_before_commit(tmp_path)], workspace_root=tmp_path),
    )
    result = engine.run("提交代码")
    calls = [e for e in result.events if e["type"] == "tool_call"]
    assert calls[0]["success"] is True  # 命令真的执行（非 git 仓库会 exit 非 0，但执行本身算成功）
