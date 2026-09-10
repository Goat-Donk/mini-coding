"""M3-4 tests: agent/session.py（JSONL 轨迹 + 检查点 + resume）。"""
import json
from pathlib import Path

from agent.llm import MockLLM
from agent.loop import QueryEngine
from agent.session import Session, latest_session
from agent.tools.base import ToolRegistry


def make_engine(tmp_path: Path, llm: MockLLM, *, session: Session | None = None) -> QueryEngine:
    return QueryEngine(
        llm,
        ToolRegistry.default(tmp_path),
        workspace_root=tmp_path,
        session=session,
    )


def tool_rounds(n: int) -> list:
    """n 轮不同参数的 glob 调用（避免循环检测误杀）。"""
    return [MockLLM.tool("glob", {"pattern": f"*{i}"}).responses[0] for i in range(n)]


def test_trajectory_jsonl(tmp_path):
    """每个事件一行 JSONL：含 llm_call 与 tool_call，可逐行解析。"""
    session = Session(tmp_path, "t1")
    engine = make_engine(
        tmp_path,
        MockLLM.script(
            MockLLM.tool("glob", {"pattern": "**/*"}).responses[0],
            MockLLM.text("完成").responses[0],
        ),
        session=session,
    )
    result = engine.run("任务")
    assert result.terminated_reason == "completed"

    path = tmp_path / "data" / "sessions" / "t1.jsonl"
    assert path.exists()
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    types = [json.loads(line)["type"] for line in lines]
    assert "llm_call" in types
    assert "tool_call" in types
    assert len(lines) >= 3  # llm_call + tool_call + llm_call(最终)


def test_checkpoint_every_n_steps(tmp_path):
    """每 N=2 步落盘一个检查点。"""
    session = Session(tmp_path, "t2", checkpoint_every=2)
    script = tool_rounds(5) + [MockLLM.text("完成").responses[0]]
    engine = make_engine(tmp_path, MockLLM.script(*script), session=session)
    engine.run("任务")
    assert session.list_checkpoints() == [2, 4]
    # 轨迹不受检查点影响：仍是完整事件流
    n_lines = len(
        (tmp_path / "data" / "sessions" / "t2.jsonl").read_text(encoding="utf-8").strip().splitlines()
    )
    assert n_lines >= 11  # 5×2 + 1


def test_resume_restores_and_continues(tmp_path):
    """resume：恢复 step/task/messages/usage，续跑推进 step 且不覆盖旧检查点。"""
    session = Session(tmp_path, "r1", checkpoint_every=1)
    script = tool_rounds(3) + [MockLLM.text("完成").responses[0]]
    engine = make_engine(tmp_path, MockLLM.script(*script), session=session)
    result = engine.run("测试任务")
    assert result.terminated_reason == "completed"
    assert session.list_checkpoints() == [1, 2, 3]

    # 检查点文件内容自检
    cp = json.loads(
        (tmp_path / "data" / "checkpoints" / "r1" / "step-2.json").read_text(encoding="utf-8")
    )
    assert cp["task"] == "测试任务"
    assert cp["step"] == 2
    assert cp["messages"][0]["role"] == "system"
    assert cp["messages"][0]["content"]  # system prompt 真实内容

    # 从 step=2 恢复 → 续跑（最终结论是新 mock 的回答）
    session2, restored = Session.from_checkpoint(tmp_path, "r1", step=2)
    assert restored.step == 2
    assert restored.task == "测试任务"
    assert len(restored.messages) == len(cp["messages"])  # 消息与检查点一致
    engine2 = make_engine(tmp_path, MockLLM.text("继续完成"), session=session2)
    result2 = engine2.run_from(restored)
    assert result2.terminated_reason == "completed"
    assert result2.steps == 3  # 从 2 续到 3
    assert result2.final_text == "继续完成"

    # 续跑的新检查点 step-3 写入同一目录，旧 step-2 不被覆盖
    assert (tmp_path / "data" / "checkpoints" / "r1" / "step-2.json").exists()


def test_resume_latest_checkpoint_when_step_none(tmp_path):
    session = Session(tmp_path, "r2", checkpoint_every=1)
    script = tool_rounds(3) + [MockLLM.text("完成").responses[0]]
    engine = make_engine(tmp_path, MockLLM.script(*script), session=session)
    engine.run("任务")
    _, restored = Session.from_checkpoint(tmp_path, "r2")  # step=None → 最近
    assert restored.step == 3


def test_latest_session_by_mtime(tmp_path):
    """latest_session 返回检查点更新时间最近的 session。"""
    for sid in ("s_old", "s_new"):
        session = Session(tmp_path, sid, checkpoint_every=1)
        engine = make_engine(
            tmp_path,
            MockLLM.script(
                MockLLM.tool("glob", {"pattern": "**/*"}).responses[0],
                MockLLM.text("ok").responses[0],
            ),
            session=session,
        )
        engine.run("任务")
    assert latest_session(tmp_path) == "s_new"


def test_from_checkpoint_missing_raises(tmp_path):
    import pytest

    with pytest.raises(FileNotFoundError):
        Session.from_checkpoint(tmp_path, "nope", step=1)


def test_emit_is_thread_safe(tmp_path):
    """只读工具并发执行时 emit 会被多线程同时调用 —— JSONL 必须一行一个完整事件。

    没有锁的话，两行可能交错成半个 JSON（轨迹就没法逐行解析了）。
    """
    import threading

    session = Session(tmp_path, "s-threads")
    n_threads, per_thread = 8, 50

    def worker(tid: int) -> None:
        for i in range(per_thread):
            session.emit({"type": "tool_call", "step": i, "tid": tid, "name": "read"})

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    lines = session.trajectory_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == n_threads * per_thread
    for line in lines:  # 每行都必须是完整可解析的 JSON
        json.loads(line)
