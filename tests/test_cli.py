"""tests: app/cli.py —— 事件打印（EventPrinter）+ 权限链路接线。

前半段只测打印机本身（纯函数式：事件进、行出），不起真进程。
后半段用 typer 的 CliRunner 真跑 CLI：CLI 曾漏接权限引擎（只有 Streamlit 接了），
导致「危险命令拦截」在 CLI 路径上不生效 —— 这里把这个接线锁住。
"""
import threading

from typer.testing import CliRunner

from agent.llm import LLMResult, MockLLM, ToolCall
from agent.permissions import Decision, PermissionsEngine
from app.cli import EventPrinter, app


def _capture(monkeypatch) -> list[str]:
    lines: list[str] = []
    monkeypatch.setattr("app.cli.typer.echo", lambda msg="": lines.append(msg))
    return lines


def test_prints_tool_call_with_status_and_duration(monkeypatch):
    lines = _capture(monkeypatch)
    printer = EventPrinter()
    printer({
        "type": "tool_call", "step": 2, "name": "read",
        "arguments": {"path": "a.txt"}, "success": True, "duration_ms": 12,
    })
    assert lines == ["  [2] ✓ read(path=a.txt) [12ms]"]


def test_shows_nonzero_exit_code(monkeypatch):
    """success 只表示"工具跑完了"；退出码非 0 要显式标出来，别让人以为命令成功了。"""
    lines = _capture(monkeypatch)
    printer = EventPrinter()
    printer({
        "type": "tool_call", "step": 1, "name": "bash",
        "arguments": {"command": "pytest -q"}, "success": True,
        "duration_ms": 900, "exit_code": 1,
    })
    assert "[exit code: 1]" in lines[0]
    assert "✓" in lines[0]  # 工具本身没崩，仍然是 ✓ + 退出码


def test_zero_exit_code_is_not_shown(monkeypatch):
    lines = _capture(monkeypatch)
    EventPrinter()({
        "type": "tool_call", "step": 1, "name": "bash",
        "arguments": {"command": "echo hi"}, "success": True,
        "duration_ms": 5, "exit_code": 0,
    })
    assert "exit code" not in lines[0]


def test_llm_call_is_not_printed(monkeypatch):
    """llm_call 事件太吵（每步一条），只留在轨迹 JSONL 里。"""
    lines = _capture(monkeypatch)
    EventPrinter()({"type": "llm_call", "step": 1, "usage": {"prompt_tokens": 10}})
    assert lines == []


def test_empty_response_retry_is_printed(monkeypatch):
    lines = _capture(monkeypatch)
    EventPrinter()({"type": "empty_response_retry", "step": 3, "attempt": 1, "limit": 2})
    assert "重试 1/2" in lines[0]


def test_long_arguments_are_truncated(monkeypatch):
    lines = _capture(monkeypatch)
    EventPrinter()({
        "type": "tool_call", "step": 1, "name": "write",
        "arguments": {"content": "x" * 500}, "success": True, "duration_ms": 1,
    })
    assert len(lines[0]) < 200


def test_concurrent_events_do_not_interleave(monkeypatch):
    """只读工具并发 → record_event 从多个线程回调；打印必须整行原子输出。"""
    lines = _capture(monkeypatch)
    printer = EventPrinter()

    def worker(tid: int) -> None:
        for i in range(40):
            printer({
                "type": "tool_call", "step": i, "name": f"read{tid}",
                "arguments": {"path": "a"}, "success": True, "duration_ms": 1,
            })

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(lines) == 320
    for line in lines:  # 每行都是完整的一行（没有两条事件被切碎混在一起）
        assert line.startswith("  [") and line.endswith("ms]")


# ---------- 权限链路接线（M2） ----------

runner = CliRunner()


class SpyPermissions(PermissionsEngine):
    """记录每次判定的权限引擎替身，用来证明 CLI 真把工具调用送进了引擎。

    只包一层 check()，判定逻辑完全走真引擎 —— 测的是接线，不是重写规则。
    """

    last: "SpyPermissions | None" = None

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.checks: list[tuple[str, dict, Decision]] = []
        SpyPermissions.last = self

    def check(self, tool_name, arguments, ctx):  # noqa: ANN001 - 继承签名
        decision = super().check(tool_name, arguments, ctx)
        self.checks.append((tool_name, arguments, decision))
        return decision


def _invoke(tmp_path, monkeypatch, llm, extra_args: tuple[str, ...] = ()):
    """在 tmp_path 里真跑一次 CLI（--mock），并让权限引擎变成可观测的替身。

    task 是必填位置参数（非 --resume 时为空会 Exit(1)）；--resume 时它被忽略
    （任务从检查点恢复），传一个占位串不影响断言。
    """
    SpyPermissions.last = None
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setattr("app.cli._build_llm", lambda mock: llm)
    monkeypatch.setattr("app.cli.PermissionsEngine", SpyPermissions)
    return runner.invoke(app, ["占位任务", *extra_args, "--mock"])


def _capture_tool_messages() -> tuple[list[str], object]:
    """构造一个「第二次调用」响应：把模型收到的 tool 消息抓下来再收尾。"""
    seen: list[str] = []

    def responder(messages, tools):  # noqa: ANN001 - MockLLM 回调签名
        seen.extend(m["content"] for m in messages if m.get("role") == "tool")
        return LLMResult(content="收到")

    return seen, responder


def test_dangerous_bash_is_denied_by_permission_engine(tmp_path, monkeypatch):
    """危险命令要由**权限引擎**判定为 ask，再由 loop 落成拒绝（CLI 无确认交互）。

    关键区分：bash 工具自身也有一层危险命令兜底，但那时引擎不在场；
    这里的断言要求拒绝理由来自引擎（"需要人工确认"），不是工具兜底。
    """
    seen, responder = _capture_tool_messages()
    llm = MockLLM.script(
        LLMResult(
            content=None,
            tool_calls=[ToolCall(id="c1", name="bash", arguments={"command": "rm -rf build"})],
        ),
        responder,
    )

    result = _invoke(tmp_path, monkeypatch, llm)

    assert result.exit_code == 0
    spy = SpyPermissions.last
    assert spy is not None
    assert [name for name, _, _ in spy.checks] == ["bash"]
    # 引擎给的是 ask（"需要人工确认"）；CLI 不传 confirm，于是 loop 按安全默认拒绝
    assert spy.checks[0][2] is Decision.ASK
    assert any("需要人工确认" in text for text in seen), seen
    assert not any("命令命中危险模式" in text for text in seen), "走到了工具兜底，说明引擎没接上"


def test_normal_tool_calls_still_allowed(tmp_path, monkeypatch):
    """默认规则保持 allow —— 接引擎不能改变正常流程的行为。"""
    seen, responder = _capture_tool_messages()
    llm = MockLLM.script(
        LLMResult(
            content=None,
            tool_calls=[ToolCall(id="c1", name="glob", arguments={"pattern": "**/*"})],
        ),
        responder,
    )

    result = _invoke(tmp_path, monkeypatch, llm)

    assert result.exit_code == 0
    spy = SpyPermissions.last
    assert spy is not None
    assert [name for name, _, _ in spy.checks] == ["glob"]
    assert spy.checks[0][2] is Decision.ALLOW
    assert not any("权限拒绝" in text for text in seen), seen


def test_path_escape_denied(tmp_path, monkeypatch):
    """路径越界沙箱 → 权限引擎硬 deny（第一优先级）。"""
    seen, responder = _capture_tool_messages()
    llm = MockLLM.script(
        LLMResult(
            content=None,
            tool_calls=[ToolCall(id="c1", name="read", arguments={"path": "../outside.txt"})],
        ),
        responder,
    )

    result = _invoke(tmp_path, monkeypatch, llm)

    assert result.exit_code == 0
    spy = SpyPermissions.last
    assert spy is not None
    assert spy.checks[0][2] is Decision.DENY
    assert any("权限拒绝" in text for text in seen), seen


def test_resume_branch_also_wires_permissions(tmp_path, monkeypatch):
    """--resume 分支是第二处 QueryEngine 构造点，同样必须传 permissions。"""
    llm = MockLLM.script(
        LLMResult(
            content=None,
            tool_calls=[ToolCall(id="c1", name="glob", arguments={"pattern": "**/*"})],
        ),
        LLMResult(content="第一次跑完了"),
    )
    first = _invoke(tmp_path, monkeypatch, llm, extra_args=("--checkpoint-every", "1"))
    assert first.exit_code == 0

    seen, responder = _capture_tool_messages()
    resumed_llm = MockLLM.script(
        LLMResult(
            content=None,
            tool_calls=[ToolCall(id="c2", name="bash", arguments={"command": "git push"})],
        ),
        responder,
    )
    second = _invoke(tmp_path, monkeypatch, resumed_llm, extra_args=("--resume",))

    assert second.exit_code == 0
    assert "续跑" in second.output
    spy = SpyPermissions.last
    assert spy is not None and spy.checks, "resume 分支没有把调用送进权限引擎"
    assert spy.checks[0][2] is Decision.ASK
    assert any("需要人工确认" in text for text in seen), seen


def test_cli_does_not_wire_hooks(tmp_path, monkeypatch):
    """本轮明确不接 hooks（默认 hook 会改变正常流程，需另行决定）。"""
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setattr(
        "app.cli._build_llm", lambda mock: MockLLM.script(LLMResult(content="完成"))
    )
    captured: dict = {}
    real_engine = __import__("agent.loop", fromlist=["QueryEngine"]).QueryEngine

    class RecordingEngine(real_engine):
        def __init__(self, *args, **kwargs):
            captured.update(kwargs)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr("app.cli.QueryEngine", RecordingEngine)
    result = runner.invoke(app, ["随便一个任务", "--mock"])

    assert result.exit_code == 0
    assert captured.get("permissions") is not None, "CLI 必须接权限引擎"
    assert captured.get("hooks") is None, "本轮不接 hooks"
