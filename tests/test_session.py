"""M3-4 tests: agent/session.py（JSONL 轨迹 + 检查点 + resume）。"""
import json
from pathlib import Path

import pytest

from agent.llm import MockLLM
from agent.loop import QueryEngine
from agent.state import AgentState
from agent.session import (
    DEFAULT_CHECKPOINT_EVERY,
    Session,
    SessionNotFound,
    latest_session,
    list_sessions,
    meta_path,
    read_meta,
    resolve_session,
    session_name,
    set_session_name,
    state_dict,
    update_meta,
    validate_name,
)
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


def test_checkpoint_records_the_cadence(tmp_path):
    """节拍是**会话的事实**，要跟着检查点落盘。

    不落盘的话，`--resume` 只能靠"没传就用 5"这个隐式回落 —— 而 5 是 CLI 的
    默认值，不是这个会话的事实。会话当初按 1 步一存起的，恢复后按 5 走，
    唯一的证据是检查点数不涨：命令成功、输出正常、退出码 0。
    """
    session = Session(tmp_path, "cad1", checkpoint_every=3)
    engine = make_engine(
        tmp_path,
        MockLLM.script(*tool_rounds(3), MockLLM.text("完成").responses[0]),
        session=session,
    )
    engine.run("任务")
    cp = json.loads(
        (tmp_path / "data" / "checkpoints" / "cad1" / "step-3.json").read_text(encoding="utf-8")
    )
    assert cp["checkpoint_every"] == 3
    # 放在 payload 顶层而不是 state 里：它不该被 `AgentState(**raw)` 吃到
    assert "checkpoint_every" not in state_dict(cp)


def test_resume_inherits_the_session_cadence(tmp_path):
    """不传 `checkpoint_every` 时，`--resume` 沿用**该会话当初的**节拍，不回落 5。

    这是本次修的第二半：前半是"传了不生效"（见 test_cli 的
    test_resume_honors_checkpoint_every），后半是"不传就用 CLI 默认值"——
    后半同样是错的，而且更难发现，因为 5 凑巧也是个合法节拍。
    """
    session = Session(tmp_path, "cad2", checkpoint_every=1)
    engine = make_engine(
        tmp_path,
        MockLLM.script(*tool_rounds(2), MockLLM.text("完成").responses[0]),
        session=session,
    )
    engine.run("任务")
    assert session.list_checkpoints() == [1, 2]

    resumed, _ = Session.from_checkpoint(tmp_path, "cad2")   # 不传节拍
    assert resumed.checkpoint_every == 1, "没沿用它当初的 1，而是回落成了默认值"

    # 显式传参仍然优先（否则等于节拍再也改不了）
    overridden, _ = Session.from_checkpoint(tmp_path, "cad2", checkpoint_every=7)
    assert overridden.checkpoint_every == 7


def test_resume_falls_back_for_legacy_checkpoints(tmp_path):
    """本次改动之前写下的检查点没有这个字段 → 回落 `DEFAULT_CHECKPOINT_EVERY`。

    必须单独钉：老检查点在磁盘上是**真实存在**的（M1–M8 跑出来的全都没有这个
    字段），读到 None 时如果直接把 None 塞给构造器，`max(1, None)` 会当场抛
    `TypeError` —— 那不是"兼容旧格式"，是"旧会话再也恢复不了"。
    """
    session = Session(tmp_path, "cad3", checkpoint_every=1)
    engine = make_engine(
        tmp_path,
        MockLLM.script(*tool_rounds(1), MockLLM.text("完成").responses[0]),
        session=session,
    )
    engine.run("任务")

    path = tmp_path / "data" / "checkpoints" / "cad3" / "step-1.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload.pop("checkpoint_every", None)   # 伪装成 M8 之前的检查点
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    resumed, _ = Session.from_checkpoint(tmp_path, "cad3")
    assert resumed.checkpoint_every == DEFAULT_CHECKPOINT_EVERY


def test_resume_ignores_a_corrupt_cadence(tmp_path):
    """检查点被手改过（0 / 负数 / 字符串）→ 回落默认，而不是夹成"每步都写"。

    夹成 1 是错的方向：对一个已经损坏的检查点做出比默认更激进的行为
    （每步一个检查点文件），会把磁盘写满。
    """
    session = Session(tmp_path, "cad4", checkpoint_every=1)
    engine = make_engine(
        tmp_path,
        MockLLM.script(*tool_rounds(1), MockLLM.text("完成").responses[0]),
        session=session,
    )
    engine.run("任务")

    path = tmp_path / "data" / "checkpoints" / "cad4" / "step-1.json"
    for bad in (0, -3, "3", True):
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["checkpoint_every"] = bad
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        resumed, _ = Session.from_checkpoint(tmp_path, "cad4")
        assert resumed.checkpoint_every == DEFAULT_CHECKPOINT_EVERY, f"{bad!r} 没被挡住"


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

    from agent.goal import Goal
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
        # 目标用**每个字段都非默认**的一份：漏了 `_FIELD_DECODERS` 的 `"goal"`
        # 时它会以 `dict` 形态回来，`==` 立刻不等 —— 而那正是本项目最阴的一类
        # 故障（dataclass 不查类型，直到有人读 `.status` 才炸，炸点还被
        # `_run_loop` 的 except 吞成 terminated_reason="error"）。
        "goal": Goal(
            objective="让 tests/test_textstat.py 全绿",
            check_command="python -m pytest tests/test_textstat.py -q",
            status="paused",
            pause_reason="等人工确认口径",
            created_step=3,
            turns=2,
            declaration={"summary": "修好了", "evidence": ["pytest 绿"], "step": 9},
            last_check={"verdict": "failed", "step": 9, "exit_code": 1, "reason": None},
        ),
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
    assert isinstance(restored.goal, Goal)  # 同上：漏解码器时这里是个 dict


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


# ---------- M9-3 分叉（fork）：从任意一步另开一条路 ----------

def seed_steps(
    tmp_path: Path,
    sid: str,
    steps: int,
    *,
    finding_at: int | None = None,
    cadence: int = 1,
) -> Session:
    """手工落 `steps` 个检查点（每步一条消息 + 一个事件），可插一条 security_finding。

    不用真引擎是因为本组测试要控制的是**检查点里的消息与事件流**，不是模型行为；
    手写才能精确指定"污染发生在第几步"、以及"第 N 步的历史长什么样"。

    `cadence` 是给"检查点不是逐步都有的"那类测试用的（`checkpoint_every` 按
    **调用次数**节流，所以 cadence=2 走 3 步只留下第 2 步的检查点）。
    """
    session = Session(tmp_path, sid, checkpoint_every=cadence)
    state = AgentState(session_id=sid, task="任务", system_prompt="sys")
    state.emitter = session.emit
    for step in range(1, steps + 1):
        state.step = step
        state.messages.append({"role": "assistant", "content": f"第 {step} 步"})
        state.record_event("llm_call", n=step)
        if finding_at == step:
            state.record_event("security_finding", level="high")
        session.checkpoint(state)
    return session


def _checkpoint_file(tmp_path: Path, sid: str, n: int) -> Path:
    return tmp_path / "data" / "checkpoints" / sid / f"step-{n}.json"


def test_fork_copies_only_checkpoints_up_to_the_fork_point(tmp_path):
    """fork@3：新会话拿到 step 1..3，源会话 1..5 一个不动、内容也不变。"""
    source = seed_steps(tmp_path, "src", 5)
    before = {n: _checkpoint_file(tmp_path, "src", n).read_text(encoding="utf-8")
              for n in source.list_checkpoints()}

    fork, restored = Session.fork(tmp_path, "src", step=3, new_id="f1")

    assert fork.list_checkpoints() == [1, 2, 3]
    assert source.list_checkpoints() == [1, 2, 3, 4, 5]   # 源会话不受影响
    assert restored.step == 3
    assert fork.checkpoint_every == source.checkpoint_every   # 节拍沿用源会话
    # 复制不是移动：源会话的检查点逐字节未变（含没被搬走的那两个）
    after = {n: _checkpoint_file(tmp_path, "src", n).read_text(encoding="utf-8")
             for n in source.list_checkpoints()}
    assert after == before


def test_a_step_without_a_checkpoint_lists_the_steps_that_exist(tmp_path):
    """`--checkpoint-every 2` 的会话只有偶数步；`--fork --step 3` 要说清有哪几步。

    为什么值得单独一条：step 级分叉是我们**对外宣传**的能力，而节拍是每个会话
    自己的事实（还是跟着检查点落盘的）。撞上"这一步没落盘"时，裸的
    `FileNotFoundError: .../step-3.json` 既不像步数写错了、也不像节拍问题，
    用户只能去翻目录。这条钉的是**报错内容**而不是"会不会抛错"：去掉那句提示
    照样抛 FileNotFoundError，但用户拿到的东西完全不同。分叉和续跑走的是同一个
    `_load_payload`，所以两条都验。
    """
    import pytest

    seed_steps(tmp_path, "src", 3, cadence=2)
    assert Session(tmp_path, "src").list_checkpoints() == [2]   # 节拍 2 → 只落了第 2 步

    with pytest.raises(FileNotFoundError, match=r"第 3 步.*可用: \[2\]"):
        Session.fork(tmp_path, "src", step=3, new_id="f1")
    with pytest.raises(FileNotFoundError, match=r"第 3 步.*可用: \[2\]"):
        Session.from_checkpoint(tmp_path, "src", step=3)


def test_fork_recovers_the_message_history_of_that_step(tmp_path):
    """分叉点那一步的历史要和源会话整份一致 —— 不是空壳、也不是最新一步。"""
    seed_steps(tmp_path, "src", 5)
    _, restored = Session.fork(tmp_path, "src", step=3, new_id="f1")
    _, source_state = Session.from_checkpoint(tmp_path, "src", step=3)
    assert [m["content"] for m in restored.messages] == ["第 1 步", "第 2 步", "第 3 步"]
    assert restored.messages == source_state.messages
    assert restored.step == 3        # 特别钉住：不是源会话的最新一步 5


def test_forked_checkpoints_do_not_claim_to_be_the_source(tmp_path):
    """副本里的 `session_id` 必须是**新会话**的。

    它是「从别处读来的 payload」唯一一处不能原样搬的字段：今天 `load_state`
    会把它 pop 掉所以无害，但谁哪天直接读 `payload["session_id"]` 就会拿到源会话
    —— 一个当前无人读、将来必有人读的错值，正是最该在写入时修掉的那种。
    """
    seed_steps(tmp_path, "src", 5)
    Session.fork(tmp_path, "src", step=3, new_id="f1")
    for n in (1, 2, 3):
        payload = json.loads(
            (tmp_path / "data" / "checkpoints" / "f1" / f"step-{n}.json").read_text(
                encoding="utf-8"
            )
        )
        assert payload["session_id"] == "f1"


def test_fork_trajectory_stops_at_the_fork_point(tmp_path):
    """轨迹只搬 `step <= fork_step` 的行。

    整份复制的话，源会话在第 4、5 步发生的事会跟着进新会话的轨迹 ——
    那条分支上**根本没发生过**这些事。这是分叉最容易漏的一处：
    检查点裁对了、轨迹忘了裁，看上去一切正常。
    """
    seed_steps(tmp_path, "src", 5)
    Session.fork(tmp_path, "src", step=3, new_id="f1")
    lines = [
        json.loads(line)
        for line in (tmp_path / "data" / "sessions" / "f1.jsonl")
        .read_text(encoding="utf-8")
        .strip()
        .splitlines()
    ]
    assert lines, "分叉会话应该有轨迹"
    assert {e["step"] for e in lines} == {1, 2, 3}   # 4、5 步的事件不在
    # 血统记在轨迹里：光看 f1.jsonl 的前几行看不出它是分叉来的
    forked = [e for e in lines if e["type"] == "session_forked"]
    assert len(forked) == 1
    assert forked[0]["from_session"] == "src"
    assert forked[0]["from_step"] == 3


def test_fork_inherits_taint_only_from_the_events_it_copied(tmp_path):
    """污染标记按**搬过来的事件**重算，不是照抄源会话的当前值。

    这条是"轨迹/检查点必须以 fork_step 为界"的判据面：源会话在第 4 步被标了 high，
    从第 3 步分叉出去的那条路上，那件事还没发生。
    """
    seed_steps(tmp_path, "src", 5, finding_at=4)

    _, early = Session.fork(tmp_path, "src", step=3, new_id="early")
    assert early.taint == "none"        # 第 4 步的 finding 不在它的世界里

    _, late = Session.fork(tmp_path, "src", step=5, new_id="late")
    assert late.taint == "high"


def test_fork_records_provenance_and_an_auto_name(tmp_path):
    """meta 里记下来源 + 步数，并给一个能一眼看懂的名字（可被 --rename 覆盖）。"""
    seed_steps(tmp_path, "src", 5)
    Session.fork(tmp_path, "src", step=3, new_id="f1")
    meta = read_meta(tmp_path, "f1")
    assert meta["forked_from"] == {"session": "src", "step": 3}
    assert meta["name"] == "src @3 分叉"


def test_fork_auto_name_uses_the_source_name_when_it_has_one(tmp_path):
    """源会话起过名 → 分叉的默认名沿用**名字**，不是 id。

    一排 `s20260911-153012 @3 分叉` 之间看不出谁是谁；沿用名字才让人一眼看出
    "这两条路是同一个任务分出来的"。**源会话没名字**时回落 id —— 那种情况
    `test_fork_records_provenance_and_an_auto_name` 已经钉住，所以两条路都覆盖到了。
    """
    seed_steps(tmp_path, "src", 5)
    set_session_name(tmp_path, "src", "基线方案")
    Session.fork(tmp_path, "src", step=3, new_id="f1")
    assert read_meta(tmp_path, "f1")["name"] == "基线方案 @3 分叉"


def test_two_forks_in_the_same_second_get_distinct_ids_and_names(tmp_path):
    """同一秒里分叉两次：id 要让开（否则两个会话共用一个检查点目录），名字也要。

    `new_session_id()` 的粒度是秒，撞了不会有任何报错 —— 后一个会话的检查点
    直接写进前一个的目录。名字撞了则更隐蔽：`resolve_session` 按名字只能返回
    一个，`--resume --session-id <名字>` 会安静地跑到另一个会话上去。
    """
    seed_steps(tmp_path, "src", 5)
    f1, _ = Session.fork(tmp_path, "src", step=3)
    f2, _ = Session.fork(tmp_path, "src", step=3)
    assert f1.session_id != f2.session_id
    assert session_name(tmp_path, f1.session_id) != session_name(tmp_path, f2.session_id)


# ---------- M9-3 改名（rename）+ 会话清单 ----------

def test_rename_round_trip_and_resolution_by_name(tmp_path):
    seed_steps(tmp_path, "src", 2)
    set_session_name(tmp_path, "src", "基线方案")
    assert session_name(tmp_path, "src") == "基线方案"
    assert resolve_session(tmp_path, "基线方案") == "src"
    assert resolve_session(tmp_path, "src") == "src"     # id 照常可用


def test_rename_rejects_a_name_that_is_another_sessions_id(tmp_path):
    """名字不许等于任何已有 session_id。

    允许的话，`resolve_session` 先按 id 解析，那个 id 就永远解析不到自己的会话了
    —— 失败方式是"静默跑到另一个会话上去"，所以要在**写入侧**拒绝，
    而不是在解析侧加优先级去猜。
    """
    seed_steps(tmp_path, "src", 2)
    seed_steps(tmp_path, "other", 2)
    with pytest.raises(ValueError, match="已被 other 占用"):
        set_session_name(tmp_path, "src", "other")


def test_rename_rejects_a_duplicate_name(tmp_path):
    seed_steps(tmp_path, "a", 2)
    seed_steps(tmp_path, "b", 2)
    set_session_name(tmp_path, "a", "同一个名字")
    with pytest.raises(ValueError, match="已被 a 占用"):
        set_session_name(tmp_path, "b", "同一个名字")
    # 改成自己当前的名字是幂等的（否则"再设一次"会莫名失败）
    set_session_name(tmp_path, "a", "同一个名字")


def test_rename_rejects_empty_and_pathlike_names(tmp_path):
    """每条判据都钉**拒绝理由**，不只钉"抛了 ValueError"。

    为什么较这个真：`_name_taken` 里有一条 `_checkpoints_root / name` 的存在性检查，
    而 `_checkpoints_root / ""` 正好解析回检查点根目录本身（它当然存在）。于是
    "空名字"这条判据**去掉之后照样抛 ValueError** —— 只是理由从"不能为空"变成
    "已被 占用"，用户看到的是句鬼话。只断言异常类型的测试抓不住这种退化。
    """
    seed_steps(tmp_path, "src", 2)
    for bad in ("", "   "):
        with pytest.raises(ValueError, match="不能为空"):
            set_session_name(tmp_path, "src", bad)
    for bad in ("a/b", "a\\b"):
        with pytest.raises(ValueError, match="路径分隔符"):
            set_session_name(tmp_path, "src", bad)
    with pytest.raises(ValueError, match="最长"):
        set_session_name(tmp_path, "src", "a" * 61)
    assert session_name(tmp_path, "src") is None      # 一次都没写进去

    # 前后空白是**去掉再存**，不是原样存。否则：（a）`--resume --session-id 基线方案`
    # 找不到一个叫 "  基线方案  " 的会话；（b）查重那两条路都是按字面比的，
    # "基线方案" 与 "  基线方案  " 会被当成两个不同的名字，同一个名字能存两份。
    set_session_name(tmp_path, "src", "  基线方案  ")
    assert session_name(tmp_path, "src") == "基线方案"
    assert read_meta(tmp_path, "src")["name"] == "基线方案"


def test_rename_refuses_a_session_that_does_not_exist(tmp_path):
    """不给幽灵会话建 meta：`--sessions` 里看着像回事，`--resume` 却没有检查点。"""
    with pytest.raises(ValueError, match="会话不存在"):
        set_session_name(tmp_path, "nope", "名字")
    assert not meta_path(tmp_path, "nope").exists()


def test_rename_keeps_the_fork_provenance(tmp_path):
    """改元数据是**合并**不是覆盖：改完名字，分叉来源不能消失。"""
    seed_steps(tmp_path, "src", 5)
    Session.fork(tmp_path, "src", step=3, new_id="f1")
    set_session_name(tmp_path, "f1", "另一条路")
    meta = read_meta(tmp_path, "f1")
    assert meta["name"] == "另一条路"
    assert meta["forked_from"] == {"session": "src", "step": 3}


def test_sessions_lists_trajectory_only_sessions(tmp_path):
    """只有轨迹、没有检查点的会话也要出现在清单里。

    那是"跑到一半被 kill、还没到第一个检查点"的会话 —— 恰恰是最需要被看见的
    一种（「我明明跑过」和「列表里没有」之间不该有落差）。
    """
    session = Session(tmp_path, "alive", checkpoint_every=5)
    session.emit({"ts": 1.0, "type": "llm_call", "step": 0})
    seed_steps(tmp_path, "full", 2)

    by_id = {i.session_id: i for i in list_sessions(tmp_path)}
    assert set(by_id) == {"alive", "full"}
    assert by_id["alive"].latest_step == 0 and by_id["alive"].checkpoint_count == 0
    assert by_id["full"].latest_step == 2


def test_sessions_reports_a_corrupt_meta_without_hiding_others(tmp_path):
    """一个坏 meta 不该让整张表挂掉，但也不能被静默当成"没有名字"。"""
    seed_steps(tmp_path, "good", 2)
    seed_steps(tmp_path, "bad", 2)
    meta_path(tmp_path, "bad").write_text("{ 这不是 JSON", encoding="utf-8")

    by_id = {i.session_id: i for i in list_sessions(tmp_path)}
    assert by_id["bad"].meta_error is not None
    assert by_id["good"].meta_error is None


def test_session_name_is_quiet_about_a_corrupt_meta_but_the_listing_is_not(tmp_path):
    """同一个坏 meta，两处刻意给出不同反应 —— 所以两处都要钉住。

    `session_name` 的调用方是"要个显示名"（打印标签、拼分叉默认名），为一个坏
    meta 让整条命令失败不成比例；`--sessions` 那边的调用方是"名字丢了要响"，
    静默显示"(未命名)"会让人以为从没起过名字。
    """
    seed_steps(tmp_path, "bad", 2)
    meta_path(tmp_path, "bad").write_text("{ 这不是 JSON", encoding="utf-8")
    assert session_name(tmp_path, "bad") is None
    assert list_sessions(tmp_path)[0].meta_error is not None


def test_resolve_session_lists_candidates_when_unknown(tmp_path):
    seed_steps(tmp_path, "src", 2)
    with pytest.raises(SessionNotFound) as exc:
        resolve_session(tmp_path, "没这个会话")
    assert "src" in exc.value.available


def test_update_meta_is_atomic_and_self_describing(tmp_path):
    seed_steps(tmp_path, "src", 2)
    update_meta(tmp_path, "src", name="x")
    payload = json.loads(meta_path(tmp_path, "src").read_text(encoding="utf-8"))
    assert payload["session_id"] == "src"
    assert not meta_path(tmp_path, "src").with_suffix(".json.tmp").exists()


def test_update_meta_writes_through_a_temp_file(tmp_path, monkeypatch):
    """原子写：先写 `.json.tmp`、再 `replace` 到目标名，**绝不直接写目标文件**。

    为什么不能靠比对结果来测：直接写与原子写在**成功路径上结果完全相同**
    （文件都在、内容都对），差别只在进程被 kill 的那一瞬间。所以只能钉住
    "走了哪条路径" —— 否则把 `tmp.replace(path)` 删成 `path.write_text(...)`
    测试照样全绿，而那道原子性保证已经没了。
    """
    seed_steps(tmp_path, "src", 2)
    writes: list[str] = []
    replaces: list[str] = []
    real_write, real_replace = Path.write_text, Path.replace

    def spy_write(self, *args, **kwargs):
        writes.append(self.name)
        return real_write(self, *args, **kwargs)

    def spy_replace(self, target):
        replaces.append(self.name)
        return real_replace(self, target)

    monkeypatch.setattr(Path, "write_text", spy_write)
    monkeypatch.setattr(Path, "replace", spy_replace)
    update_meta(tmp_path, "src", name="x")

    assert writes == ["src.meta.json.tmp"]      # 写的是临时文件
    assert replaces == ["src.meta.json.tmp"]    # 由它原子替换过去
    assert not meta_path(tmp_path, "src").with_suffix(".json.tmp").exists()
    assert read_meta(tmp_path, "src")["name"] == "x"


def test_validate_name_rejects_an_id_shape_that_exists(tmp_path):
    seed_steps(tmp_path, "s20260911-000000", 1)
    with pytest.raises(ValueError):
        validate_name(tmp_path, "s20260911-000000")

