"""M2-2 tests: agent/hooks.py（Pre/PostToolUse 分发 + block-at-submit）。"""
import sys
from pathlib import Path

from agent.hooks import (
    HookBlock,
    HookContext,
    HookEngine,
    default_engine,
    mark_tests_pass,
    mark_tests_pass_on_success,
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


# ---------- marker 由真实测试结果产生（PostToolUse） ----------

def _post(engine_hooks: HookEngine, command: str, exit_code: int) -> list[str]:
    return engine_hooks.run_post(
        "bash", {"command": command}, ToolResult.ok("out", {"exit_code": exit_code})
    )


def _marker(tmp_path: Path) -> Path:
    return tmp_path / "data" / "tests_pass.marker"


def test_passing_tests_write_marker(tmp_path):
    engine = HookEngine(post_hooks=[mark_tests_pass_on_success(tmp_path)], workspace_root=tmp_path)
    hints = _post(engine, "python -m pytest tests/ -q", 0)
    assert _marker(tmp_path).exists()
    assert hints and "已写入" in hints[0]


def test_failing_tests_clear_stale_marker(tmp_path):
    """失败要清掉旧 marker —— 不能拿上一次的通过记录去提交。"""
    mark_tests_pass(tmp_path)
    engine = HookEngine(post_hooks=[mark_tests_pass_on_success(tmp_path)], workspace_root=tmp_path)
    hints = _post(engine, "python -m pytest tests/ -q", 1)
    assert not _marker(tmp_path).exists()
    assert hints and "已清除" in hints[0]


def test_non_test_command_does_not_touch_marker(tmp_path):
    engine = HookEngine(post_hooks=[mark_tests_pass_on_success(tmp_path)], workspace_root=tmp_path)
    assert _post(engine, "ls -la", 0) == []
    assert not _marker(tmp_path).exists()


def test_various_test_runners_recognized(tmp_path):
    engine = HookEngine(post_hooks=[mark_tests_pass_on_success(tmp_path)], workspace_root=tmp_path)
    for command in ("pytest", "python -m pytest -q", "npm test", "cargo test --all", "go test ./..."):
        _marker(tmp_path).unlink(missing_ok=True)
        _post(engine, command, 0)
        assert _marker(tmp_path).exists(), command
    # 只是名字里带 test 的命令不算（别把 `git commit -m "add test"` 当测试）
    _marker(tmp_path).unlink(missing_ok=True)
    _post(engine, 'git commit -m "add tests for parser"', 0)
    assert not _marker(tmp_path).exists()


def test_default_engine_wires_both_directions(tmp_path):
    """default_engine = block-at-submit + 测试成功自动解锁，一条链走完。"""
    engine = default_engine(tmp_path)
    commit = {"command": "git commit -m 'feat: x'"}
    assert engine.run_pre("bash", commit) is not None      # 未跑测试 → 拦
    _post(engine, "python -m pytest -q", 0)                # 跑测试成功 → 解锁
    assert engine.run_pre("bash", commit) is None


def test_loop_runs_real_pytest_then_unlocks_commit(tmp_path):
    """端到端：模型先跑真 pytest（真通过）→ marker 自动写入 → git commit 不再被拦。"""
    (tmp_path / "test_ok.py").write_text("def test_ok():\n    assert 1 + 1 == 2\n", encoding="utf-8")
    engine = make_engine(
        tmp_path,
        MockLLM.script(
            MockLLM.tool("bash", {"command": f'"{sys.executable}" -m pytest -q'}).responses[0],
            MockLLM.tool("bash", {"command": "git commit -m 'feat: x'"}).responses[0],
            LLMResult(content="已提交"),
        ),
        hooks=default_engine(tmp_path),
    )

    result = engine.run("跑测试然后提交")

    assert result.terminated_reason == "completed"
    assert _marker(tmp_path).exists(), "测试真通过后 marker 应自动写入"
    calls = [e for e in result.events if e["type"] == "tool_call"]
    assert calls[0]["success"] is True and calls[0]["exit_code"] == 0
    assert calls[1]["success"] is True, "marker 已写入，提交不该再被 hook 拦"

    # 反向：测试失败 → marker 被清掉 → 提交重新被拦
    engine2 = make_engine(
        tmp_path,
        MockLLM.script(
            MockLLM.tool("bash", {"command": f'"{sys.executable}" -m pytest -q missing_test.py'}).responses[0],
            MockLLM.tool("bash", {"command": "git commit -m 'feat: y'"}).responses[0],
            LLMResult(content="被拦了"),
        ),
        hooks=default_engine(tmp_path),
    )
    result2 = engine2.run("跑一个不存在的测试再提交")
    assert not _marker(tmp_path).exists()
    calls2 = [e for e in result2.events if e["type"] == "tool_call"]
    assert calls2[1]["success"] is False, "测试失败后提交应被 hook 拦下"
