"""子代理工具族测试（M4-2 起，M9-7 扩到 5 个工具 + 并发句柄契约）。

三段：

1. **工具面**（M4-2 原有）：受限只读、不递归、串行、如实回喂。
2. **并发契约**（M9-7）：spawn 立刻返回 / 上限只数在跑的 / wait 超时不关 /
   close 真停且幂等 / settle 三种情形分开报 / 用量恰好合并一次。
3. **接线**（M9-7）：5 个工具都串行、没接管理器时如实失败、父回合不 wait
   会留下结算事件、worker 的落盘 store 用独立目录。

并发用例一律用 `tests/fake_llm.py` 的假 LLM —— **不要用 `MockLLM`**：它是
`pop(0)` 的有状态队列，两个 worker 会互相吃掉对方的脚本（见那个模块的
docstring）。需要"worker 还在跑"这个状态时，就用一个睡一小会儿的 responder
制造它 —— 不靠 `time.sleep` 之外的时序假设，每条断言的裕度都远大于抖动。
"""
from __future__ import annotations

import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from agent.llm import LLMResult, MockLLM, ToolCall, Usage
from agent.loop import QueryEngine
from agent.state import ensure_tool_pairing
from agent.subagents import (
    AGENT_CLOSED,
    AGENT_DONE,
    AGENT_FAILED,
    AGENT_RUNNING,
    AbortToken,
    AgentWorkers,
    TooManyAgents,
    UnknownAgent,
    WorkerOutcome,
)
from agent.tools.base import Tool, ToolContext, ToolRegistry, ToolResult
from agent.tools.subagent import (
    SUBAGENT_SYSTEM_PROMPT,
    _restricted_registry,
    build_subagent_tools,
)
from tests.fake_llm import FakeLLM, keyed_llm, text, tool

# ---------- 1. 工具面（M4-2 原有，适配到新的构造点） ----------


def make_engine(tmp_path: Path, llm) -> QueryEngine:
    registry = ToolRegistry.default(tmp_path)
    for subagent_tool in build_subagent_tools(llm, tmp_path):
        registry.register(subagent_tool)
    return QueryEngine(llm, registry, workspace_root=tmp_path)


def test_subagent_runs_and_returns_report(tmp_path):
    """主循环调 subagent → 子代理独立跑 glob + 结论 → 报告回主上下文。"""
    (tmp_path / "README.md").write_text("# Demo\n", encoding="utf-8")
    llm = MockLLM.script(
        MockLLM.tool("subagent", {"task": "探索并总结仓库结构"}).responses[0],
        MockLLM.tool("glob", {"pattern": "**/*"}).responses[0],      # 子代理步 1
        MockLLM.text("子代理结论: 仓库只有一个 README").responses[0],  # 子代理步 2
        MockLLM.text("主代理最终总结").responses[0],                   # 主循环步 2
    )
    result = make_engine(tmp_path, llm).run("总任务")
    assert result.terminated_reason == "completed"
    assert result.final_text == "主代理最终总结"
    # 子代理调用被记录进主轨迹（tool_call success），子步骤的 glob 也被执行
    names = [(ev["name"], ev["success"]) for ev in result.events if ev["type"] == "tool_call"]
    assert ("subagent", True) in names
    # 主轨迹只有 3 个事件：子代理内部步骤不进主上下文（独立 state/events）。
    # 4 个也不行 —— `settle()` 在"没东西可结算"时**必须是免费的**，多一条
    # `subagent_settled` 就说明它无条件记事件了（下面有专门一条测试钉这个）。
    assert len(result.events) == 3
    assert "subagent_settled" not in [ev["type"] for ev in result.events]


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
    result = make_engine(tmp_path, llm).run("总任务")
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


def test_subagent_system_prompt_carries_workspace_root(tmp_path):
    """M9-7 补的缺口：子代理 prompt 有 `{workspace_root}` 槽位且**真被渲染**。

    它此前没有这个槽位 —— 而 `_render_system_prompt` 对自定义 prompt 也会做
    替换，所以加上是零新机制的收益：worker 不用靠 `pwd` 去猜自己在哪。
    """
    assert "{workspace_root}" in SUBAGENT_SYSTEM_PROMPT
    captured: dict = {}

    def worker_responder(messages, tools):
        captured["system"] = messages[0]["content"]
        return text("结论")

    llm = keyed_llm({
        "总任务": [
            LLMResult(content=None, tool_calls=[
                ToolCall(id="a", name="subagent", arguments={"task": "调查"})
            ]),
            text("收工"),
        ],
        "调查": [worker_responder],
    })
    make_engine(tmp_path, llm).run("总任务")
    assert str(tmp_path.resolve()) in captured["system"]
    # 槽位真的被替换掉了，不是原样留在 prompt 里
    assert "{workspace_root}" not in captured["system"]


def test_restricted_registry_never_recursive():
    """白名单构建：任何子代理工具/bash/write 不给；未知名字静默忽略。"""
    reg = _restricted_registry(["glob", "subagent", "write", "read", "bash"])
    assert reg.names() == ["glob", "read"]  # 保持请求顺序
    assert _restricted_registry(["bash", "subagent"]).names() == []
    # 五个子代理工具名一个都进不去（白名单而非黑名单：新工具默认进不来）
    for name in ("spawn_agent", "list_agents", "wait_agent", "close_agent"):
        assert _restricted_registry([name]).names() == []


def test_subagent_registered_as_serial_tool(tmp_path):
    """注册后 schema 完整；标记非只读（串行执行，避免嵌套并发）。"""
    registry = ToolRegistry.default(tmp_path)
    for subagent_tool in build_subagent_tools(MockLLM.text("ok"), tmp_path):
        registry.register(subagent_tool)
    assert "subagent" in registry.names()
    assert registry.get("subagent").is_read_only() is False
    sub_schema = next(
        s for s in registry.schemas() if s["function"]["name"] == "subagent"
    )
    params = sub_schema["function"]["parameters"]["properties"]
    assert "task" in params
    assert params["tools"]["type"] == "array"  # tools 字段存在（default 不序列化进 schema）


# ---------- 2. 并发契约（M9-7） ----------


class _Gate:
    """让 worker 卡住的闸门。测试用它精确制造"worker 还在跑"这个状态。"""

    def __init__(self) -> None:
        self._event = threading.Event()

    def open(self) -> None:
        self._event.set()

    def wait(self, timeout: float = 5.0) -> bool:
        return self._event.wait(timeout)


def _blocking_runner(gate: _Gate, conclusion: str = "结论", usage: Usage | None = None):
    """跑到闸门放行才交结论 —— 交结论时**不看 token**（模拟卡在一次网络调用里）。"""

    def run(token: AbortToken) -> WorkerOutcome:
        gate.wait()
        return WorkerOutcome(
            result=SimpleNamespace(final_text=conclusion, steps=1),
            usage=usage,
            reason="completed",
        )

    return run


def _token_aware_runner(gate: _Gate, conclusion: str = "结论"):
    """跑到闸门放行才交结论，**并且看 token** —— 被叫停就报 `aborted`。

    与 `_blocking_runner` 成对，两种 worker 形状都得有：

    - 那个（不看 token）模拟"卡在一次网络调用里" —— **谁也停不下它**；
    - 这个（看 token）模拟"正跑在检查点之间" —— **停得下来**。

    「`wait` 超时不关它」这条契约**只有在后者身上才可观测**。对着一个停不下来的
    worker，`wait` 就算偷偷把 token 置了位，状态也照样是 `running`，光看快照
    分辨不出来 —— 变异测试里"超时顺手关掉"那条最初就是因此 MISS 的。
    """

    def run(token: AbortToken) -> WorkerOutcome:
        gate.wait()
        if token.is_set():
            return WorkerOutcome(result=None, usage=None, reason="aborted")
        return WorkerOutcome(
            result=SimpleNamespace(final_text=conclusion, steps=1),
            usage=None,
            reason="completed",
        )

    return run


def _instant_runner(conclusion: str = "结论", usage: Usage | None = None):
    def run(token: AbortToken) -> WorkerOutcome:
        return WorkerOutcome(
            result=SimpleNamespace(final_text=conclusion, steps=1),
            usage=usage,
            reason="completed",
        )

    return run


def _await_terminal(workers: AgentWorkers, agent_id: str, timeout: float = 5.0) -> None:
    """等句柄进终态。**不走被测量的 `wait()`** —— 用它测量它自己就没有意义了。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        snap = next(s for s in workers.list() if s.id == agent_id)
        if snap.is_terminal:
            return
        time.sleep(0.01)
    raise AssertionError(f"{agent_id} 没有在 {timeout}s 内结束")


def _join_thread_named(name: str, timeout: float = 3.0) -> bool:
    """等一个具名线程消失，返回它是否真的消失了（worker 线程名见 `spawn`）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not any(t.name == name for t in threading.enumerate()):
            return True
        time.sleep(0.02)
    return False


def test_spawn_returns_before_worker_finishes():
    """契约 1：`spawn` **立刻**返回句柄，工作在后头跑。"""
    gate = _Gate()
    workers = AgentWorkers()
    snapshot = workers.spawn("调查 A", _blocking_runner(gate))
    assert snapshot.id == "sa-1"
    assert snapshot.status == AGENT_RUNNING
    assert snapshot.result is None           # 还没结论 —— 这本身就是"没阻塞"的证据
    assert not snapshot.is_terminal
    gate.open()
    _await_terminal(workers, "sa-1")
    assert workers.list()[0].status == AGENT_DONE


def test_capacity_counts_only_running_workers():
    """契约 1b：上限数的是**还在跑**的，不是"曾经派过"的。

    数后者的话，一次长会话在成功跑过 3 个之后会永久锁死 —— 而表现只是
    "子代理坏了"，没有任何线索指向计数方式。
    """
    gates = [_Gate() for _ in range(3)]
    workers = AgentWorkers()
    for idx, gate in enumerate(gates):
        workers.spawn(f"任务 {idx}", _blocking_runner(gate))
    with pytest.raises(TooManyAgents) as excinfo:
        workers.spawn("第四个", _instant_runner())
    # 文案要带在跑的花名册 —— 只说"满了"会让模型反复重试同一个 spawn
    assert "sa-1" in str(excinfo.value) and "sa-3" in str(excinfo.value)

    # 放掉一个 → 它进终态 → 第四个能派出去（**不排队，是空位**）
    gates[0].open()
    _await_terminal(workers, "sa-1")
    assert workers.spawn("第四个", _instant_runner()).id == "sa-4"
    for gate in gates:
        gate.open()
    workers.settle()


def test_wait_timeout_does_not_close_the_worker():
    """契约 2：「等」和「关」是两件事 —— 超时只回报状态，**不关**。

    两段用的是**两种 worker 形状**，缺一不可（见 `_token_aware_runner`）。
    """
    # 第一段：卡在网络调用里、**不看 token** 的 worker。
    gate = _Gate()
    workers = AgentWorkers()
    workers.spawn("慢任务", _blocking_runner(gate, conclusion="终于好了"))
    (timed_out,) = workers.wait(timeout=0.05)
    assert timed_out.status == AGENT_RUNNING
    assert timed_out.result is None
    # 闸门由**另一个线程**稍后放行 —— 这样"接下去那次不带超时的 wait 真的等了"
    # 才有牙齿：它若不等就返回，拿到的会是 `running`（0.2s 的裕度远大于抖动）。
    threading.Timer(0.2, gate.open).start()
    (done,) = workers.wait()
    assert done.status == AGENT_DONE
    assert done.result.final_text == "终于好了"

    # 第二段：跑在检查点之间、**会看 token** 的 worker —— 「超时顺手把它关掉」
    # 只有在这里才分辨得出来：token 一旦被置位，它交回来的就是 `aborted`，
    # 状态会被记成 `closed`，而不是"还在跑、后来又跑完了"。
    #
    # 要**三个**：`remaining <= 0` 那条分支要等预算被前面的 handle 等光之后才
    # 轮得到。只有一个 handle 时循环只走一圈、超时全被 `finished.wait` 吃掉，
    # 那条分支根本到不了 —— 这就是这条变异体最初 MISS 的原因（不是断言太松，
    # 是场景压根没覆盖到）。三个才保证"前面那个慢的耗光预算"这件事真的发生。
    gates = [_Gate() for _ in range(3)]
    workers2 = AgentWorkers()
    for idx, gate in enumerate(gates):
        workers2.spawn(f"检查点之间的任务 {idx}", _token_aware_runner(gate))
    assert [s.status for s in workers2.wait(timeout=0.05)] == [AGENT_RUNNING] * 3
    # 「等够了」没有让任何一个停下 —— 三个都还交得回结论
    for gate in gates:
        gate.open()
    assert [s.status for s in workers2.wait()] == [AGENT_DONE] * 3


def test_close_stops_the_worker_and_is_idempotent():
    """契约 3：`close` = abort + **等它真停**；再关一次返回同一个终态。"""
    gate = _Gate()
    workers = AgentWorkers()
    workers.spawn("停不下来的任务", _blocking_runner(gate))

    # close 会在 join 上等 —— 从另一个线程放行闸门，模拟"它在下一次检查点停下"
    threading.Timer(0.05, gate.open).start()
    closed = workers.close("sa-1")
    assert closed.status == AGENT_CLOSED
    assert closed.is_terminal
    # 返回时线程**真的死了**。`AgentSnapshot` 刻意不暴露 `thread`（冻结副本，
    # 锁的边界不外泄），所以按名字查：join 回来了，它就该已经消失。
    assert _join_thread_named("codeagent-sa-1", timeout=1.0)

    # 幂等：再关一次，状态不变、不重复做任何事
    again = workers.close("sa-1")
    assert again.status == AGENT_CLOSED
    assert again.finished_at == closed.finished_at


def test_close_on_a_finished_worker_reports_what_actually_happened():
    """已经跑完的再关：**不改写成 closed**，如实报它跑完了、结论还在。"""
    workers = AgentWorkers()
    workers.spawn("快任务", _instant_runner("已经好了"))
    _await_terminal(workers, "sa-1")
    snapshot = workers.close("sa-1")
    assert snapshot.status == AGENT_DONE
    assert snapshot.result.final_text == "已经好了"


def test_settle_separates_killed_from_unclaimed():
    """`settle` 把三种情形**分开报** —— 它们的含义完全不同。

    - killed：还在跑、被结算叫停、**确实停下来了** → 没有结论
    - unclaimed：跑完了但没人来取 → **有结论，只是丢了**
    - still_running：有界 join 没等到它停 → 如实说，不假装清干净了
    """
    gate = _Gate()
    workers = AgentWorkers()
    workers.spawn("跑完没人取的任务", _instant_runner("被丢掉的结论"))
    _await_terminal(workers, "sa-1")
    workers.spawn("还在跑的任务", _blocking_runner(gate))

    threading.Timer(0.05, gate.open).start()   # 让它在结算的 join 预算内停下来
    report = workers.settle(timeout=1.0)
    assert [s.id for s in report.unclaimed] == ["sa-1"]
    assert [s.id for s in report.killed] == ["sa-2"]
    assert report.still_running == ()
    assert report.any

    # 说明文案把两种情形**分开写**，且带上被丢掉的结论（事后可追）
    notes = " ".join(report.as_event()["notes"])
    assert "没有结论" in notes
    assert "从没被取走" in notes
    assert "被丢掉的结论" in notes


def test_settle_reports_still_running_instead_of_lying():
    """有界 join 超时 → 如实报 `still_running`，不假装它停了、也不报成 killed。"""
    gate = _Gate()
    workers = AgentWorkers()
    workers.spawn("怎么也停不下来", _blocking_runner(gate))
    report = workers.settle(timeout=0.05)
    assert report.killed == ()                       # 它**没有**停下来，不能记成"已叫停"
    assert [s.id for s in report.still_running] == ["sa-1"]
    assert report.any
    assert "仍在后台运行" in " ".join(report.as_event()["notes"])
    gate.open()


def test_settle_is_free_when_nothing_was_spawned():
    """没 spawn 过就是**完全免费**的：不记事件、不写盘、不报任何东西。

    每个普通回合都会走到 `settle()`。无条件记一条事件会让"没用子代理的会话"
    平白多出噪音 —— 而噪音一旦常在，人就再也不看它了。
    """
    report = AgentWorkers().settle()
    assert not report.any
    assert report.as_event()["notes"] == []


def test_usage_is_merged_exactly_once():
    """用量**恰好合并一次**：`wait` 报过的不能再被 `settle` 计一遍。

    合并两次的话父会话的账目会翻倍；一次都不合的话 worker 烧的钱凭空消失。
    两条都难发现 —— 所以用一个列表直接钉住"合并了几次、每次多少"。
    """
    merged: list[Usage] = []
    workers = AgentWorkers(on_usage=merged.append)
    workers.spawn("甲", _instant_runner(usage=Usage(prompt_tokens=7)))
    gate = _Gate()
    workers.spawn("乙", _blocking_runner(gate, usage=Usage(prompt_tokens=11)))
    _await_terminal(workers, "sa-1")

    workers.wait(["sa-1"])                          # 这一次把它计掉
    assert [u.prompt_tokens for u in merged] == [7]
    gate.open()
    _await_terminal(workers, "sa-2")
    workers.settle()                                # 剩下的由 settle 收尾，sa-1 不能重复
    assert sorted(u.prompt_tokens for u in merged) == [7, 11]


def test_unknown_agent_id_names_the_roster():
    """抄错 id 是最常见的用法错误 → 报错要带已知 id，下一步就能改对。"""
    workers = AgentWorkers()
    workers.spawn("甲", _instant_runner())
    with pytest.raises(UnknownAgent) as excinfo:
        workers.wait(["sa-9"])
    assert "sa-9" in str(excinfo.value) and "sa-1" in str(excinfo.value)
    with pytest.raises(UnknownAgent):
        workers.close("sa-9")


def test_list_and_live_ids_keep_spawn_order():
    """花名册按**派发顺序**排（确定性：`list_agents` 的输出要能写进断言）。"""
    gate = _Gate()
    workers = AgentWorkers()
    workers.spawn("甲", _instant_runner())
    workers.spawn("乙", _blocking_runner(gate))
    _await_terminal(workers, "sa-1")
    assert [s.id for s in workers.list()] == ["sa-1", "sa-2"]
    assert workers.live_ids() == ["sa-2"]
    gate.open()
    workers.settle()


def test_worker_failure_is_recorded_not_swallowed():
    """worker 抛异常 → 记 failed + 异常摘要，不是静默消失。"""

    def boom(token: AbortToken) -> WorkerOutcome:
        raise ValueError("子代理炸了")

    workers = AgentWorkers()
    workers.spawn("会炸的任务", boom)
    _await_terminal(workers, "sa-1")
    snapshot = workers.list()[0]
    assert snapshot.status == AGENT_FAILED
    assert "ValueError" in snapshot.error and "子代理炸了" in snapshot.error


# ---------- 3. 接线与循环契约（M9-7） ----------


class _NoArgs(BaseModel):
    pass


class _AbortTool(Tool):
    """测试用工具：执行时把取消 token set 掉（模拟"有人要求它停下"）。"""

    name = "aborter"
    description = "set the abort token"
    input_model = _NoArgs

    def __init__(self, token: AbortToken) -> None:
        self.token = token

    def execute(self, args: _NoArgs, ctx: ToolContext) -> ToolResult:
        self.token.set()
        return ToolResult.ok("已请求取消")


def test_abort_stops_before_the_next_llm_call(tmp_path):
    """取消检查点 #1：token 已 set → **一次 llm.chat 都不发**就收尾。"""
    token = AbortToken()
    calls = {"n": 0}

    def responder(messages, tools):
        calls["n"] += 1
        token.set()          # 第一次调用之后就被要求停
        return tool("glob", {"pattern": "**/*"})

    engine = QueryEngine(
        FakeLLM(responder),
        ToolRegistry.default(tmp_path),
        workspace_root=tmp_path,
        abort=token,
    )
    result = engine.run("任务")
    assert result.terminated_reason == "aborted"
    assert calls["n"] == 1, "第二次 llm.chat 不该发生"
    assert any(ev["type"] == "aborted" for ev in result.events)
    # 取消**不假装成功、也不假装出错**：它是一条独立的事实
    assert "没有结论" in result.final_text


def test_abort_mid_batch_still_pairs_every_tool_call(tmp_path):
    """取消检查点 #2 的**硬要求**：同批剩下的调用必须补上配对结果。

    少一条 `tool` 消息就是孤儿 id —— 端点直接 400，而报错发生在**下一次请求**
    时，看起来与这次取消毫无关系（同 ask_user 跳过时的回填）。
    """
    token = AbortToken()
    registry = ToolRegistry.default(tmp_path)
    registry.register(_AbortTool(token))

    def responder(messages, tools):
        return LLMResult(content=None, tool_calls=[
            ToolCall(id=f"call_{i}", name=name, arguments=args)
            for i, (name, args) in enumerate([
                ("aborter", {}),
                ("write", {"path": "a.txt", "content": "x"}),
                ("write", {"path": "b.txt", "content": "y"}),
            ])
        ])

    engine = QueryEngine(
        FakeLLM(responder), registry, workspace_root=tmp_path, abort=token
    )
    state = engine.new_state("任务")
    result = engine.run_turn(state, "任务")
    assert result.terminated_reason == "aborted"

    # 后两条被跳过 —— 它们**没有执行**，磁盘上不该有文件
    assert not (tmp_path / "a.txt").exists()
    assert not (tmp_path / "b.txt").exists()
    # 但配对必须完整：`ensure_tool_pairing` 什么都没得修（== 0 才算没孤儿）
    assert ensure_tool_pairing(state.messages) == 0

    calls = [ev for ev in state.events if ev["type"] == "tool_call"]
    assert [ev["name"] for ev in calls] == ["aborter", "write", "write"]
    assert calls[0]["success"] is True and "aborted" not in calls[0]
    assert [ev.get("aborted") for ev in calls[1:]] == [True, True]


def test_reused_engine_without_token_is_never_aborted(tmp_path):
    """**不变式**：常驻引擎（跨回合复用、不带 token）不受任何影响。

    给复用引擎装 token 会让它**后续每一个回合**都在第一个检查点返回 aborted
    —— 用户看到的是"我说话它不理"，而且没有任何东西能解释这件事。
    """
    def by_input(messages, tools):
        last_user = [m for m in messages if m.get("role") == "user"][-1]["content"]
        return text(f"{last_user}好了")

    engine = QueryEngine(
        FakeLLM(by_input), ToolRegistry.default(tmp_path), workspace_root=tmp_path
    )
    state = engine.new_state("甲")
    first = engine.run_turn(state, "甲")
    second = engine.run_turn(state, "乙")
    assert first.terminated_reason == "completed"
    assert second.terminated_reason == "completed"
    assert second.final_text == "乙好了"


def test_aborted_worker_reports_aborted_through_the_manager(tmp_path):
    """经**真正的消费者**（worker 的 RunResult）验一遍 `"aborted"`。

    上面那条是拿引擎单测的；这条走 `spawn` → runner → 引擎，证明这条通路在
    句柄上留下的是 `reason == "aborted"`，而不是 `error`（父模型看到"出错"
    会换策略，看到"被取消"才知道该做的动作是"要么重派、要么别派了"）。
    """
    def runner(token: AbortToken) -> WorkerOutcome:
        engine = QueryEngine(
            FakeLLM(lambda m, t: tool("glob", {"pattern": "*"})),
            ToolRegistry.default(tmp_path),
            workspace_root=tmp_path,
            abort=token,
        )
        result = engine.run("任务")
        return WorkerOutcome(
            result=result, usage=result.usage, reason=result.terminated_reason
        )

    workers = AgentWorkers()
    workers.spawn("会被叫停的任务", runner)
    snapshot = workers.close("sa-1")      # 立刻叫停
    assert snapshot.status == AGENT_CLOSED
    assert snapshot.reason == "aborted"   # 终止原因是引擎给的，不是猜的
    assert snapshot.error is None         # 取消**不是**错误


def test_all_five_tools_are_registered_and_serial(tmp_path):
    """5 个工具都在，且**全部**非只读（串行）。

    看着像只读的 `list_agents` 也算：只读工具走并发池，而 `executor.map` 保证的
    是输出顺序、不是开始顺序 —— 一批 `[spawn, spawn, wait]` 里 `wait_agent`
    完全可能在两个 spawn 注册 id 之前就跑起来。判据是**调度**，不是"读得多不多"。
    """
    tools = build_subagent_tools(MockLLM.text("x"), tmp_path)
    assert [t.name for t in tools] == [
        "subagent", "spawn_agent", "list_agents", "wait_agent", "close_agent",
    ]
    for item in tools:
        assert item.is_read_only() is False, f"{item.name} 必须是串行的"
        assert item.schema()["function"]["parameters"]["type"] == "object"


def test_tools_fail_honestly_without_the_manager(tmp_path):
    """没接管理器 → 如实失败（接线缺口不能静默降级成"工具坏了"）。"""
    ctx = ToolContext(workspace_root=tmp_path)   # workers 默认 None
    assert ctx.workers is None
    by_name = {t.name: t for t in build_subagent_tools(MockLLM.text("x"), tmp_path)}
    for name, args in [
        ("subagent", {"task": "t"}),
        ("spawn_agent", {"task": "t"}),
        ("list_agents", {}),
        ("wait_agent", {}),
        ("close_agent", {"id": "sa-1"}),
    ]:
        result = by_name[name].run(args, ctx)
        assert result.success is False, name
        assert "接线" in result.output, name


def test_sibling_spawns_in_one_batch_are_visible_to_wait(tmp_path):
    """一批 `[spawn, spawn, wait]` 串行执行 → `wait_agent` 看见了那两个 id。

    这条同时钉住两件事：三个工具都串行（并发池里开始顺序不保），以及
    `wait_agent` 省略 ids 时等的是"全部还在跑的"。
    """
    llm = keyed_llm({
        "总任务": [
            LLMResult(content=None, tool_calls=[
                ToolCall(id="call_a", name="spawn_agent", arguments={"task": "调查甲"}),
                ToolCall(id="call_b", name="spawn_agent", arguments={"task": "调查乙"}),
                ToolCall(id="call_c", name="wait_agent", arguments={}),
            ]),
            text("两个都收齐了"),
        ],
        "调查甲": [text("甲的结论")],
        "调查乙": [text("乙的结论")],
    })
    engine = make_engine(tmp_path, llm)
    state = engine.new_state("总任务")
    result = engine.run_turn(state, "总任务")
    assert result.terminated_reason == "completed"
    assert result.final_text == "两个都收齐了"

    wait_msg = next(m for m in state.messages if m.get("tool_call_id") == "call_c")
    out = wait_msg["content"]
    # 两个 id 都在花名册里，且两份**结论**都被交回了模型
    assert "sa-1" in out and "sa-2" in out and "甲的结论" in out and "乙的结论" in out
    # 全被取走了 → 结算时无事可报（这正是"用了子代理但没浪费"的样子）
    assert "subagent_settled" not in [ev["type"] for ev in result.events]


def test_spawn_without_wait_is_reported_at_turn_end(tmp_path):
    """**头号缺陷类的反面**：派了不取，回合结束必须有人知道。

    模型 spawn 完直接给最终答复 —— worker 会活过它的回合、结论无处可去。
    结算事件是人唯一的通路（事件不进 messages，模型看不到），所以要钉住它
    **确实被记下来了**、点了 id、且 worker 真的在回合边界被叫停。

    worker 的 responder 先睡一会儿：这让"结算时它还在跑"成为确定事实（父回合
    那几步是毫秒级的），于是它必然落进 `killed` 而不是 `unclaimed`。
    """
    def sleepy_worker(messages, tools):
        time.sleep(0.4)
        return text("辛苦算出来的结论")

    llm = keyed_llm({
        "总任务": [
            LLMResult(content=None, tool_calls=[
                ToolCall(id="a", name="spawn_agent", arguments={"task": "派出去就不管了"}),
            ]),
            text("我直接收工了"),
        ],
        "派出去就不管了": [sleepy_worker],
    })
    result = make_engine(tmp_path, llm).run("总任务")
    assert result.terminated_reason == "completed"
    assert result.final_text == "我直接收工了"

    settled = [ev for ev in result.events if ev["type"] == "subagent_settled"]
    assert len(settled) == 1, "派了不取必须留下结算事件"
    event = settled[0]
    assert event["killed"] == ["sa-1"]
    assert event["unclaimed"] == []
    assert any("没有结论" in note for note in event["notes"])
    # 它**真的**被叫停了：回合边界之后那个线程必须消失（worker 只活一个回合）
    assert _join_thread_named("codeagent-sa-1"), "worker 线程活过了回合边界"


def test_finished_but_unclaimed_worker_is_reported(tmp_path):
    """跑完了但结论从没被取走 —— 与"跑到一半被杀"**分开报**。

    这里反过来控制时序：worker 是瞬时的，而**父回合的第二步故意慢一拍**，
    于是"父回合结束时 worker 已经跑完"成为确定事实 → 必然落进 `unclaimed`。
    """
    def slow_parent(messages, tools):
        time.sleep(0.3)          # 让 worker 先跑完
        return text("我直接收工了")

    llm = keyed_llm({
        "总任务": [
            LLMResult(content=None, tool_calls=[
                ToolCall(id="a", name="spawn_agent", arguments={"task": "白跑一趟"}),
            ]),
            slow_parent,
        ],
        "白跑一趟": [text("辛苦算出来的结论")],
    })
    result = make_engine(tmp_path, llm).run("总任务")
    settled = [ev for ev in result.events if ev["type"] == "subagent_settled"]
    assert len(settled) == 1
    event = settled[0]
    assert event["unclaimed"] == ["sa-1"]
    assert event["killed"] == []
    notes = " ".join(event["notes"])
    assert "从没被取走" in notes
    assert "辛苦算出来的结论" in notes     # 结论尾部进了轨迹，事后能看出丢了什么


def test_subagent_token_usage_is_folded_into_the_parent(tmp_path):
    """用户拍板：子代理的 token **计入父会话**（并指出代价：稀释缓存命中率）。"""
    child_usage = Usage(
        prompt_tokens=30, completion_tokens=3,
        prompt_cache_hit_tokens=0, prompt_cache_miss_tokens=30,
    )
    llm = keyed_llm({
        "总任务": [
            LLMResult(
                content=None,
                tool_calls=[ToolCall(id="a", name="subagent", arguments={"task": "调查甲"})],
                usage=Usage(prompt_tokens=100, completion_tokens=10,
                            prompt_cache_hit_tokens=90, prompt_cache_miss_tokens=10),
            ),
            text("收工", usage=Usage(prompt_tokens=200, completion_tokens=20,
                                     prompt_cache_hit_tokens=198, prompt_cache_miss_tokens=2)),
        ],
        "调查甲": [
            LLMResult(
                content=None,
                tool_calls=[ToolCall(id="g", name="glob", arguments={"pattern": "**/*"})],
                usage=child_usage,
            ),
            text("甲的结论", usage=child_usage),
        ],
    })
    engine = make_engine(tmp_path, llm)
    state = engine.new_state("总任务")
    result = engine.run_turn(state, "总任务")

    expected = 100 + 200 + 30 + 30          # 父两步 + worker 两步，各恰好一次
    assert state.usage.prompt_tokens == expected
    assert result.usage.prompt_tokens == expected
    assert state.usage.prompt_cache_miss_tokens == 10 + 2 + 30 + 30
    # 父自己的锚点**不能**被 worker 的用量写脏：它描述的是父上下文
    assert state.last_usage.prompt_tokens == 200


def test_worker_oversized_result_is_persisted_under_its_own_dir(tmp_path):
    """M9-7 补的 M3-2 缺口：worker 没有 session → `_store_for` 返回 None →
    超大结果**无界**进 worker 的 messages。现在每个 worker 有自己的落盘目录。"""
    (tmp_path / "big.txt").write_text("x" * 60_000, encoding="utf-8")
    llm = keyed_llm({
        "总任务": [
            LLMResult(content=None, tool_calls=[
                ToolCall(id="a", name="subagent", arguments={"task": "读大文件"}),
            ]),
            text("收工"),
        ],
        "读大文件": [
            LLMResult(content=None, tool_calls=[
                ToolCall(id="r", name="read", arguments={"path": "big.txt"}),
            ]),
            text("读完了"),
        ],
    })
    result = make_engine(tmp_path, llm).run("总任务")
    assert result.terminated_reason == "completed"
    persisted = sorted((tmp_path / "data" / "tool-results").glob("*"))
    # 父会话是 "m1"（引擎没有 session）→ worker 的目录必须与它**分开**
    assert [p.name for p in persisted] == ["m1-sa1"]
    assert list(persisted[0].glob("*.txt"))


def test_plain_turn_without_subagents_gains_no_events(tmp_path):
    """接线不能给普通回合加噪音：不用子代理的会话，事件不多、盘上也是一片空白。"""
    engine = make_engine(tmp_path, FakeLLM(lambda m, t: text("好了")))
    result = engine.run("甲")
    assert [ev["type"] for ev in result.events] == ["llm_call"]
    assert not (tmp_path / "data").exists(), "没用子代理就不该建任何目录"


def test_all_five_tools_are_registered_by_the_production_runtime(tmp_path, monkeypatch):
    """**接线契约**：生产装配里五个工具一个不少。

    「机制在、测试绿、没有任何东西路由到它」是这个项目的头号缺陷类。上面那条
    测的是 `build_subagent_tools` **返回**了五个；这条测的是 `_build_runtime`
    **真的把它们全注册进 registry** 了 —— 只注册第一个的话，`spawn_agent`
    在真实会话里根本不存在，而所有单测照样全绿。

    脚手架与 `tests/test_goal.py` 的 `_runtime` 同款（走真的 `_build_runtime`）。
    """
    monkeypatch.setattr("app.cli._build_llm", lambda mock: MockLLM.text("x"))
    from agent.skills import discover_skills

    monkeypatch.setattr(
        "app.cli.discover_skills",
        lambda ws: discover_skills(ws, home=tmp_path / "home"),
    )
    from app.cli import _build_runtime

    runtime = _build_runtime(tmp_path, mock=True, mcp=None, review_edits=False)
    for name in (
        "subagent", "spawn_agent", "list_agents", "wait_agent", "close_agent",
    ):
        assert name in runtime.registry.names(), f"{name} 没有在生产装配里注册"
