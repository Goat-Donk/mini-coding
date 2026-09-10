"""tests: app/cli.py —— 事件打印（EventPrinter）+ 权限链路接线。

前半段只测打印机本身（纯函数式：事件进、行出），不起真进程。
后半段用 typer 的 CliRunner 真跑 CLI：CLI 曾漏接权限引擎（只有 Streamlit 接了），
导致「危险命令拦截」在 CLI 路径上不生效 —— 这里把这个接线锁住。
"""
import re
import threading

from typer.testing import CliRunner

from agent.llm import LLMResult, MockLLM, ToolCall
from agent.memory import MemoryManager
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


def _invoke(tmp_path, monkeypatch, llm, extra_args: tuple[str, ...] = (), task: str = "占位任务"):
    """在 tmp_path 里真跑一次 CLI（--mock），并让权限引擎变成可观测的替身。

    task 是位置参数：非 --resume 时就是任务描述（为空会 Exit(1)）；
    --resume 时它会被当作**续跑指示**追加进会话（见
    test_resume_instruction_is_delivered_not_dropped）。
    """
    SpyPermissions.last = None
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setattr("app.cli._build_llm", lambda mock: llm)
    monkeypatch.setattr("app.cli.PermissionsEngine", SpyPermissions)
    return runner.invoke(app, [task, *extra_args, "--mock"])


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


def test_resume_instruction_is_delivered_not_dropped(tmp_path, monkeypatch):
    """`--resume "补充说明"` 必须真的进会话，不能被静默丢掉。

    这条是**真实 LLM 端到端验证挖出来的**：拒绝文案让用户
    「用 --clear-taint 复位标记再重试」，但复位之后 CLI 没有任何办法把
    「我已复位，请重试」这句话送进会话 —— `run_from` 用的是 `state.task`，
    位置参数被直接忽略。于是模型按拒绝文案的指引停下来等人，人却回不了话，
    整条「收紧 → 人解锁 → 重试」的动线断在最后一步。
    """
    llm = MockLLM.script(
        LLMResult(
            content=None,
            tool_calls=[ToolCall(id="c1", name="glob", arguments={"pattern": "**/*"})],
        ),
        LLMResult(content="第一轮结束"),
    )
    assert _invoke(tmp_path, monkeypatch, llm, extra_args=("--checkpoint-every", "1")).exit_code == 0

    seen_users: list[str] = []

    def responder(messages, tools):  # noqa: ANN001 - MockLLM 回调签名
        seen_users.extend(
            m["content"] for m in messages
            if m.get("role") == "user" and isinstance(m.get("content"), str)
        )
        return LLMResult(content="收到，重试第 2 步")

    second = _invoke(
        tmp_path, monkeypatch, MockLLM.script(responder),
        extra_args=("--resume", "--clear-taint"),
        task="我已复位污染标记，请重试第 2 步",
    )

    assert second.exit_code == 0
    assert "续跑指示" in second.output
    assert any("我已复位污染标记，请重试第 2 步" in t for t in seen_users), seen_users


def test_cli_wires_both_governance_layers(tmp_path, monkeypatch):
    """CLI 必须同时接权限引擎与 hooks —— 这两个都曾漏接过。"""
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
    assert captured.get("hooks") is not None, "CLI 必须接 hooks"


# ---------- ask_user 接线（CLI 是"人"唯一的入口） ----------

def test_cli_registers_ask_user(tmp_path, monkeypatch):
    """CLI 必须注册 ask_user —— 它不在 `ToolRegistry.default()` 里，只能按入口接。

    （不进 default 的理由：eval/runner 用 default()，headless 里没人能回答。
    见 tests/test_tools.py::test_default_registry_excludes_ask_user。）
    """
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setattr(
        "app.cli._build_llm", lambda mock: MockLLM.script(LLMResult(content="完成"))
    )
    captured: dict = {}
    real_engine = __import__("agent.loop", fromlist=["QueryEngine"]).QueryEngine

    class RecordingEngine(real_engine):
        def __init__(self, *args, **kwargs):
            captured["registry"] = args[1]
            super().__init__(*args, **kwargs)

    monkeypatch.setattr("app.cli.QueryEngine", RecordingEngine)
    assert runner.invoke(app, ["随便一个任务", "--mock"]).exit_code == 0
    assert "ask_user" in captured["registry"].names()


def test_ask_user_pauses_cli_with_executable_hint(tmp_path, monkeypatch):
    """停在提问处时：打印问题本身（不是"最终结论"）+ 一条**可执行**的续答指引。

    指引可执行这条是 M7 的教训：当时的拒绝文案让用户「--clear-taint 复位后重试」，
    而 CLI 根本送不进人的回复 —— 一条走不通的指引比没有指引更糟。
    """
    llm = MockLLM.script(
        LLMResult(
            content=None,
            tool_calls=[ToolCall(id="c1", name="ask_user", arguments={"question": "新函数叫什么名字？"})],
        ),
        LLMResult(content="不该走到这里"),
    )
    result = _invoke(tmp_path, monkeypatch, llm, extra_args=("--checkpoint-every", "5"))

    assert result.exit_code == 0
    assert "需要你补充信息" in result.output
    assert "新函数叫什么名字？" in result.output
    assert "最终结论" not in result.output, "提问不是结论，用结论文案会让人以为任务跑完了"
    hints = [line for line in result.output.splitlines() if "--resume" in line]
    assert hints and "--session-id" in hints[0], f"续答指引必须可直接粘贴执行：{hints}"
    # checkpoint_every=5 而问题在第 1 步 —— 有检查点就证明 force 在生产路径上生效
    assert list((tmp_path / "data" / "checkpoints").glob("*/step-*.json")), "问题没落盘 → resume 拿不回来"


def test_half_trajectory_is_not_distilled_into_memory(tmp_path, monkeypatch):
    """停在提问处的半程轨迹**不提炼**仓库约定；跑完的照常提炼。

    两个方向都要断言。只测"半程不提炼"的话，把提炼整个关掉也能过 ——
    而这里要的是**按终止原因分岔**，不是关掉一个功能。
    半程轨迹里模型正在因为信息不足猜，把它猜的东西写成约定再自动注入后续所有
    会话，是污染而不是学习。续跑那一轮会照常提炼（那时轨迹是完整的）。
    """
    calls: list = []

    class SpyMemory(MemoryManager):
        def extract_and_learn(self, events, *, taint=None):  # noqa: ANN001 - 继承签名
            calls.append(taint)
            return []

    monkeypatch.setattr("app.cli.MemoryManager", SpyMemory)
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setattr(
        "app.cli._build_llm",
        lambda mock: MockLLM.script(
            LLMResult(
                content=None,
                tool_calls=[ToolCall(id="c1", name="ask_user", arguments={"question": "用哪个名字？"})],
            ),
            LLMResult(content="不该走到这里"),
        ),
    )
    # 不加 --mock：提炼只在真实模式下发生，加了 --mock 这条测试就永远看不到效果
    paused = runner.invoke(app, ["加个工具函数", "--checkpoint-every", "5"])

    assert paused.exit_code == 0
    assert "await_user" in paused.output
    assert "不做约定提炼" in paused.output
    assert calls == [], "半程轨迹被提炼成仓库约定了"

    # 对照组：正常跑完的会话仍然提炼 —— 证明闸门是按终止原因分的
    monkeypatch.setattr(
        "app.cli._build_llm", lambda mock: MockLLM.script(LLMResult(content="做完了"))
    )
    done = runner.invoke(app, ["另一个任务"])

    assert done.exit_code == 0
    assert len(calls) == 1, "跑完的会话反而没提炼 → 闸门关错了方向"


def test_resume_after_ask_user_carries_question_then_answer(tmp_path, monkeypatch):
    """完整动线：提问 → 暂停 → `--resume "回答"` → 问题仍在会话里、回答接在它后面。

    这条是端到端的行为断言（走真 CLI + 真检查点）：只钉住"能 resume"是不够的，
    要钉住 resume 出来的会话里**问题还看得见** —— 模型收到一条凭空出现的回答
    是没法接话的。
    """
    first_llm = MockLLM.script(
        LLMResult(
            content=None,
            tool_calls=[ToolCall(id="c1", name="ask_user", arguments={"question": "配置文件放哪？"})],
        ),
        LLMResult(content="不该走到这里"),
    )
    first = _invoke(tmp_path, monkeypatch, first_llm)
    assert first.exit_code == 0

    # 按**打印出来的那条指引**逐字复现参数顺序（选项在前、回答在后）——
    # 指引可执行这件事必须由测试保证，不能靠"我记得 typer 支持这种顺序"。
    hint = next(line for line in first.output.splitlines() if "--resume" in line)
    sid = re.search(r"--session-id (\S+)", hint).group(1)

    seen: list[tuple[str, str]] = []

    def responder(messages, tools):  # noqa: ANN001 - MockLLM 回调签名
        for msg in messages:
            content = msg.get("content")
            if isinstance(content, str):
                seen.append((msg.get("role", "?"), content))
        return LLMResult(content="放 .codeagent/ 下")

    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setattr("app.cli._build_llm", lambda mock: MockLLM.script(responder))
    second = runner.invoke(
        app, ["--resume", "--session-id", sid, "放 .codeagent/ 下", "--mock"]
    )

    assert second.exit_code == 0
    question_at = next(i for i, (_, t) in enumerate(seen) if "配置文件放哪？" in t)
    answer_at = next(i for i, (role, t) in enumerate(seen) if role == "user" and "放 .codeagent/ 下" in t)
    assert question_at < answer_at, "问题必须还在会话里，且排在回答之前"
    assert seen[question_at][0] == "tool", "问题是以 ask_user 的工具结果形态留在会话里的"
