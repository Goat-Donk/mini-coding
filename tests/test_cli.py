"""M6-6 tests: app/cli.py 的实时事件打印（EventPrinter）。

只测打印机本身（纯函数式：事件进、行出），不起真进程。
"""
import threading

from app.cli import EventPrinter


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
