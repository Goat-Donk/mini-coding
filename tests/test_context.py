"""M3 tests: agent/context.py（记账 + cache-aware 布局 + compact 流水线）。"""
from agent.context import (
    SNIP_BOUNDARY_MARKER,
    SUMMARY_MARKER,
    ContextManager,
    ContextStats,
    build_compact_summary_prompt,
    _message_text,
)
from agent.llm import LLMResult, MockLLM, ToolCall, Usage
from agent.state import (
    AgentState,
    assistant_tool_calls,
    system,
    tool_result,
    user,
)


def make_state(messages=None, *, last_usage=None) -> AgentState:
    return AgentState(
        session_id="t",
        task="任务",
        system_prompt="sys",
        messages=list(messages or []),
        last_usage=last_usage,
    )


def make_manager(budget: int = 64_000, **kwargs) -> ContextManager:
    return ContextManager(MockLLM.text("x"), token_budget=budget, **kwargs)


# ---------- 消息级估算 ----------

def test_estimate_tokens_per_role():
    mgr = make_manager()
    assert mgr.estimate_tokens({"role": "system", "content": "a" * 35}) == 10  # 35/3.5
    assert mgr.estimate_tokens({"role": "user", "content": "a" * 30}) == 10    # 30/3.0
    assert mgr.estimate_tokens({"role": "assistant", "content": "a" * 35}) == 10
    assert mgr.estimate_tokens({"role": "tool", "content": "a" * 20}) == 10    # 20/2.0
    assert mgr.estimate_tokens({"role": "user", "content": ""}) == 0


def test_estimate_tokens_counts_tool_call_arguments():
    msg = {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "c1",
                "type": "function",
                "function": {"name": "bash", "arguments": '{"command": "pytest"}'},
            }
        ],
    }
    mgr = make_manager()
    # content 为 None → 只数 arguments 文本
    assert mgr.estimate_tokens(msg) == mgr.estimate_tokens(
        {"role": "assistant", "content": '{"command": "pytest"}'}
    )
    assert mgr.estimate_tokens(msg) > 0


# ---------- provider-usage-first 记账 ----------

def test_account_anchored_on_last_usage():
    """有锚点：total = provider total + 尾部（最近 assistant 起）估算。"""
    mgr = make_manager(budget=10_000)
    messages = [
        system("sys"),
        user("任务"),
        assistant_tool_calls([ToolCall(id="c1", name="read", arguments={"path": "a"})]),
        tool_result("c1", "content1"),
        assistant_tool_calls([ToolCall(id="c2", name="read", arguments={"path": "b"})]),
        tool_result("c2", "content2"),
    ]
    state = make_state(messages, last_usage=Usage(prompt_tokens=100, completion_tokens=10))
    stats = mgr.account(state)
    tail = [assistant_tool_calls([ToolCall(id="c2", name="read", arguments={"path": "b"})]),
            tool_result("c2", "content2")]
    est_tail = sum(mgr.estimate_tokens(m) for m in tail)
    assert stats.provider_usage_tokens == 110          # provider 锚点 total
    assert stats.estimated_tokens == est_tail          # 只估尾部
    assert stats.total_tokens == 110 + est_tail        # provider + 尾部
    assert stats.utilization == (110 + est_tail) / 10_000


def test_account_full_estimate_without_anchor():
    """无锚点（未调用/compact 后）→ 全量估算。"""
    mgr = make_manager(budget=10_000)
    messages = [system("a" * 35), user("b" * 30)]
    state = make_state(messages)  # last_usage=None
    stats = mgr.account(state)
    assert stats.provider_usage_tokens == 0
    assert stats.estimated_tokens == sum(mgr.estimate_tokens(m) for m in messages)
    assert stats.total_tokens == stats.estimated_tokens


def test_account_stale_anchor_falls_back():
    """compact 置 usage_stale_reason → 锚点失效，退回全量估算。"""
    mgr = make_manager(budget=10_000)
    messages = [system("a" * 35), user("b" * 30)]
    state = make_state(messages, last_usage=Usage(prompt_tokens=500, completion_tokens=0))
    state.usage_stale_reason = "snip_compact"
    stats = mgr.account(state)
    assert stats.provider_usage_tokens == 0  # 不用旧 usage 计新上下文


# ---------- 分级告警 ----------

def test_warning_levels():
    mgr = make_manager(budget=1000)
    # 正常 / 警告 / 危急 / 阻塞 四档（无锚点，用 system 文本凑 token 数）
    cases = [
        ("a" * 100, "normal"),     # ~28 token → 2.8%
        ("a" * 1750, "warning"),   # 500 → 50%（≥0.5）
        ("a" * 3000, "critical"),  # 857 → 85.7%（≥0.85）
        ("a" * 3500, "blocked"),   # 1000 → 100%（≥0.95）
    ]
    for text, expected in cases:
        state = make_state([system(text)])
        assert mgr.account(state).warning_level == expected


def test_level_of_boundaries():
    assert ContextStats.level_of(0.49) == "normal"
    assert ContextStats.level_of(0.50) == "warning"
    assert ContextStats.level_of(0.849) == "warning"
    assert ContextStats.level_of(0.85) == "critical"
    assert ContextStats.level_of(0.949) == "critical"
    assert ContextStats.level_of(0.95) == "blocked"
    assert ContextStats.level_of(1.5) == "blocked"


# ---------- cache-aware 布局 ----------

def test_prepare_keeps_system_first_and_sets_stats():
    mgr = make_manager(budget=10_000)
    messages = [
        system("sys"),
        user("任务"),
        assistant_tool_calls([ToolCall(id="c1", name="read", arguments={"path": "a"})]),
        tool_result("c1", "out"),
    ]
    state = make_state(messages, last_usage=Usage(prompt_tokens=50, completion_tokens=5))
    prepared = mgr.prepare(state)
    # 布局稳定：system 恒在首位，消息顺序不变
    assert prepared == messages
    assert prepared[0]["role"] == "system"
    assert mgr.last_stats is not None
    assert mgr.last_stats.provider_usage_tokens == 55


def test_message_text_extraction():
    assert _message_text({"role": "user", "content": "hi"}) == "hi"
    assert _message_text({"role": "user", "content": None}) == ""
    msg = {
        "role": "assistant",
        "content": None,
        "tool_calls": [{
            "id": "c1",
            "type": "function",
            "function": {"name": "bash", "arguments": '{"x": 1}'},
        }],
    }
    assert _message_text(msg) == '{"x": 1}'


# ---------- M3-3 compact 流水线 ----------

def make_rounds(n_rounds: int, *, tool_chars: int) -> list[dict]:
    """[system, task] + n_rounds 个完整轮次（assistant tool_calls + tool 结果）。"""
    msgs = [system("sys"), user("任务")]
    for i in range(n_rounds):
        call = ToolCall(id=f"c{i}", name="bash", arguments={"command": f"echo {i}"})
        msgs.append(assistant_tool_calls([call]))
        msgs.append(tool_result(f"c{i}", "o" * tool_chars))
    return msgs


def test_snip_triggers_in_warning_band():
    """70%~85% 只触发确定性 snip（不调 LLM）：保留最近 12 条、前缀不动、裁剪后 ≤60%。"""
    mgr = make_manager(budget=10_000)  # 默认 0.70/0.85, keep_recent=12, min_keep=6
    msgs = make_rounds(10, tool_chars=1600)  # 10×756 + 2 ≈ 7562 → ~75.6%（warning 带）
    orig = list(msgs)
    state = make_state(msgs)
    assert mgr.account(state).warning_level == "warning"

    prepared = mgr.prepare(state)
    assert prepared[0] == orig[0] and prepared[1] == orig[1]  # system + task 前缀不动
    assert prepared[2] == {"role": "user", "content": SNIP_BOUNDARY_MARKER}
    assert prepared[3:] == orig[10:]  # 保留最近 12 条（len 22 - 12 = 10）
    assert len(prepared) == 2 + 1 + 12
    assert state.usage_stale_reason == "snip_compact"
    # 裁剪后 ≤ 60% 目标
    assert mgr.account(state).utilization <= 0.60
    # 没有调用 LLM：空响应 MockLLM 被调用会抛 RuntimeError


def test_snip_boundary_never_splits_tool_round():
    """naive 切割点落在 tool 消息上 → 回退到完整轮次边界，不留孤儿 tool 结果。"""
    mgr = make_manager(budget=4000, keep_recent=3, min_keep=2)
    msgs = make_rounds(4, tool_chars=1500)  # 4×756 ≈ 3026 → ~75.6%（warning 带）
    orig = list(msgs)
    state = make_state(msgs)
    assert mgr.account(state).warning_level == "warning"

    prepared = mgr.prepare(state)
    # naive cut = 10-3 = 7（落在 tool c2 上）→ 对齐后回退到 6（完整轮次 c2 起）
    assert prepared[2] == {"role": "user", "content": SNIP_BOUNDARY_MARKER}
    assert prepared[3:] == orig[6:]  # assistant c2 + tool c2 + assistant c3 + tool c3
    assert state.usage_stale_reason == "snip_compact"
    assert mgr.account(state).utilization <= 0.60


def test_llm_compact_at_critical():
    """critical(≥85%) 触发 LLM 摘要：中段压成摘要消息、system/task 保留、标记 stale。"""
    msgs = make_rounds(4, tool_chars=2000)  # 4×1006 ≈ 4026 → ~100%（critical）
    orig = list(msgs)
    seen = {}

    def completer(messages, tools):
        seen["text"] = messages[-1]["content"]
        return LLMResult(content="摘要：任务进展顺利，还剩一步验证。")

    state = make_state(msgs)
    manager = ContextManager(
        MockLLM([completer]),
        token_budget=4000,
        keep_recent=3,
        min_keep=2,
    )
    assert manager.account(state).warning_level in ("critical", "blocked")  # ≥85%
    prepared = manager.prepare(state)
    assert prepared[0] == orig[0] and prepared[1] == orig[1]
    assert prepared[2]["role"] == "user"
    assert SUMMARY_MARKER in prepared[2]["content"]
    assert "任务进展顺利" in prepared[2]["content"]
    assert prepared[3:] == orig[6:]  # 最近 3 条对齐边界 = 2 个完整轮次
    assert state.usage_stale_reason == "llm_compact"
    # 摘要只覆盖中段（round 0/1），不含最近轮次（round 3）
    assert "echo 0" in seen["text"] and "echo 3" not in seen["text"]


def test_llm_compact_falls_back_to_snip_on_error():
    """LLM 摘要失败（异常/空摘要）→ 退化确定性 snip，任务不中断。"""
    mgr = make_manager(budget=4000)
    msgs = make_rounds(4, tool_chars=2000)
    orig = list(msgs)

    def boom(messages, tools):
        raise RuntimeError("LLM 服务不可用")

    state = make_state(msgs)
    manager = ContextManager(MockLLM([boom]), token_budget=4000, keep_recent=3, min_keep=2)
    prepared = manager.prepare(state)
    assert prepared[0] == orig[0] and prepared[1] == orig[1]
    assert prepared[2] == {"role": "user", "content": SNIP_BOUNDARY_MARKER}
    assert prepared[3:] == orig[6:]
    assert state.usage_stale_reason == "snip_compact"


def test_build_compact_summary_prompt():
    msgs = build_compact_summary_prompt("对话内容")
    assert msgs[0]["role"] == "system"
    assert msgs[1]["role"] == "user"
    assert "任务目标" in msgs[0]["content"]
    assert "尚未完成的任务" in msgs[0]["content"]
    assert msgs[1]["content"] == "对话内容"


def test_prepare_noop_when_under_snip_threshold():
    """利用率 <70% → 不 compact，消息原样返回。"""
    mgr = make_manager(budget=10_000)
    msgs = [system("sys"), user("任务")]
    state = make_state(msgs)
    assert mgr.account(state).warning_level == "normal"
    assert mgr.prepare(state) == msgs
    assert state.usage_stale_reason is None
