"""M3-1 tests: agent/context.py（provider-usage-first 记账 + cache-aware 布局）。"""
from agent.context import ContextManager, ContextStats, _message_text
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
