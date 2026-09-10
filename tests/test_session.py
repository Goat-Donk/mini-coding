"""M3-4 tests: agent/session.py（JSONL 轨迹 + 检查点 + resume）。"""
import json
from pathlib import Path

from agent.llm import MockLLM
from agent.loop import QueryEngine
from agent.state import AgentState
from agent.session import Session, latest_session, state_dict
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
    st = state_dict(cp)
    assert st["task"] == "测试任务"
    assert st["step"] == 2
    assert st["messages"][0]["role"] == "system"
    assert st["messages"][0]["content"]  # system prompt 真实内容

    # 从 step=2 恢复 → 续跑（最终结论是新 mock 的回答）
    session2, restored = Session.from_checkpoint(tmp_path, "r1", step=2)
    assert restored.step == 2
    assert restored.task == "测试任务"
    assert len(restored.messages) == len(st["messages"])  # 消息与检查点一致
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


# ---------- 全字段往返（H5：手写白名单会让新字段静默不落盘） ----------

def test_checkpoint_round_trips_every_state_field(tmp_path):
    """AgentState 的**每一个**字段都要能落盘再恢复。

    这个测试的写法是有意的：它从 `dataclasses.fields()` 反推字段全集，所以
    **给 AgentState 加字段会让它失败**，除非把新字段也填进 `values`。
    这正是想要的效果 —— H5 那个坑的形状是「加了字段看起来对、实际悄悄不生效，
    而且没有任何提示」：`_write` 与 `from_checkpoint` 各有一份手写字段表，
    加字段时忘了同步哪一份都不会报错。结构上改成自动推导之后，这个测试负责
    钉住「自动推导」这件事本身没被改回去。
    """
    import dataclasses

    from agent.llm import Usage
    from agent.state import AgentState

    # 每个字段给一个**非默认**值；emitter 例外（运行时对象，故意不落盘）
    values = {
        "session_id": "s-round",
        "task": "往返任务",
        "system_prompt": "系统提示词",
        "messages": [{"role": "user", "content": "你好"}],
        "step": 7,
        "usage": Usage(prompt_tokens=11, completion_tokens=22),
        "events": [{"type": "tool_call", "step": 1, "name": "read"}],
        "terminated_reason": "completed",
        "memory_blocks": ["记忆块 A"],
        "plan": [{"text": "读 README", "status": "done"}],
        "last_usage": Usage(prompt_tokens=3, completion_tokens=4),
        "usage_stale_reason": "snip_compact",
        "taint": "high",
    }
    all_fields = {f.name for f in dataclasses.fields(AgentState)}
    assert set(values) | {"emitter"} == all_fields, (
        f"AgentState 新增/删除了字段，本测试未覆盖：{all_fields ^ (set(values) | {'emitter'})}"
    )

    state = AgentState(**values)
    state.emitter = lambda event: None  # 运行时对象，必须被排除在检查点之外

    session = Session(tmp_path, "s-round", checkpoint_every=1)
    session.checkpoint(state)
    _, restored = Session.from_checkpoint(tmp_path, "s-round", step=7)

    for name in all_fields - {"emitter"}:
        assert getattr(restored, name) == values[name], name
    assert restored.emitter is None  # 回调不落盘（恢复后由入口重新接）
    assert isinstance(restored.usage, Usage)  # 类型也回来了，不只是 dict


def test_dump_state_names_the_unserializable_field():
    """不可序列化的字段 → 报错**指出字段名**，而不是静默丢掉或抛个看不懂的 TypeError。

    新增字段若忘了加进 _SKIP_FIELDS，应该在这里响一声，而不是等到某次
    resume 时才发现状态少了一块。
    """
    import dataclasses

    import pytest as _pytest

    from agent.session import dump_state
    from agent.state import AgentState

    @dataclasses.dataclass
    class WeirdState(AgentState):
        handle: object = None

    state = WeirdState(
        session_id="s", task="t", system_prompt="p", handle=object()
    )
    with _pytest.raises(TypeError, match="handle"):
        dump_state(state)


def test_load_state_accepts_legacy_flat_checkpoint(tmp_path):
    """M7 之前落盘的老检查点（字段平铺）仍能 resume。

    磁盘上有真实的老文件（本地会话与评估跑的都在），换格式不等于可以把它们
    作废 —— 所以旧格式的解码器留着，且是**冻结**的。
    """
    from agent.session import dump_state, load_state

    legacy = {
        "session_id": "old1",
        "step": 3,
        "ts": 1.0,
        "task": "老任务",
        "system_prompt": "老提示词",
        "messages": [{"role": "user", "content": "hi"}],
        "events": [{"type": "llm_call", "step": 1}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 6},
        "last_usage": None,
        "usage_stale_reason": "llm_compact",
        "terminated_reason": None,
        "memory_blocks": ["老记忆块"],
    }
    state = load_state(legacy, "old1")
    assert state.task == "老任务"
    assert state.step == 3
    assert state.usage.prompt_tokens == 5          # dict → Usage
    assert state.last_usage is None
    assert state.memory_blocks == ["老记忆块"]

    # 老格式经 state_dict 也能被读者拿到（回放 UI 走的这条路）
    assert state_dict(legacy)["task"] == "老任务"

    # 磁盘级：把老格式原样写进检查点目录，`from_checkpoint` 必须能续跑
    cp_dir = tmp_path / "data" / "checkpoints" / "old1"
    cp_dir.mkdir(parents=True)
    (cp_dir / "step-3.json").write_text(
        json.dumps(legacy, ensure_ascii=False), encoding="utf-8"
    )
    _, restored = Session.from_checkpoint(tmp_path, "old1", step=3)
    assert restored.task == "老任务"
    assert restored.step == 3
    assert restored.usage.prompt_tokens == 5
    assert restored.memory_blocks == ["老记忆块"]

    # 反过来：老格式 load 出来再 dump，就是新格式，且值不变
    again = load_state({"state": dump_state(restored)}, "old1")
    assert again.task == "老任务" and again.usage.prompt_tokens == 5


def test_state_dict_returns_state_subobject_for_new_format():
    """新格式走 state 子对象；两种格式对读者是同一个接口。"""
    from agent.session import dump_state
    from agent.state import AgentState

    state = AgentState(session_id="s", task="新任务", system_prompt="p")
    payload = {"session_id": "s", "step": 0, "ts": 1.0, "state": dump_state(state)}
    assert state_dict(payload)["task"] == "新任务"
    assert "state" not in state_dict({"task": "扁平", "step": 0})


def test_checkpoint_force_bypasses_interval(tmp_path):
    """force=True 绕过节流立刻落盘（await_user 的正确性依赖它）。

    节流本身是优化：`checkpoint_every=5` 时前 4 次调用不写盘。但「本轮结束、
    进程马上退出」的场合等不到第 5 次 —— 问题发生在第 3 步就永远不落盘，
    `--resume` 恢复出来的会话里没有问题。所以这里把 force 的语义单独钉住。
    """
    session = Session(tmp_path, "s-force", checkpoint_every=5)
    state = AgentState(session_id="s-force", task="t", system_prompt="sys")
    state.messages.append({"role": "user", "content": "问题在这"})

    session.checkpoint(state)                       # 第 1 次：节流生效，不写
    assert session.list_checkpoints() == []

    session.checkpoint(state, force=True)           # 强制：立刻写
    assert len(session.list_checkpoints()) == 1
    _, restored = Session.from_checkpoint(tmp_path, "s-force")
    assert restored.messages[-1]["content"] == "问题在这"
