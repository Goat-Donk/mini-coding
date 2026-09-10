"""M3 tests: agent/context.py（记账 + cache-aware 布局 + compact 流水线）。"""
from agent.context import (
    FAILURE_BUDGET,
    SNIP_BOUNDARY_MARKER,
    SUMMARY_MARKER,
    TRUNCATE_BUDGETS,
    TRUNCATED_MARK,
    ContextManager,
    ContextStats,
    build_compact_summary_prompt,
    _PREFIX_LEN,
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
from agent.tool_result import TAG_CLOSE, TAG_OPEN


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


# ---------- M8 第 0 级：工具输出分级截断 ----------

READ_BUDGET = TRUNCATE_BUDGETS["read"]
# 省略标记的前缀（不含字符数）：用来判"这条被缩过"，不关心具体省了多少
MARK_PREFIX = TRUNCATED_MARK.split("{n}")[0]


def read_rounds(n_rounds: int, *, chars: int, mid: str = "", tail: str = "") -> list[dict]:
    """`[system, 任务]` + n 轮 `read` 调用，每条 tool 正文都是**定长 chars**。

    mid 落在正文中部、tail 落在末尾 —— 用来区分"截掉了什么、留下了什么"。
    用 `read` 是因为它的额度在 `TRUNCATE_BUDGETS` 里最小，最好构造超额。
    """
    pad = chars - len(mid) - len(tail)
    assert pad >= 0, "chars 装不下 mid + tail"
    content = "x" * (pad // 2) + mid + "x" * (pad - pad // 2) + tail
    msgs = [system("sys"), user("任务")]
    for i in range(n_rounds):
        msgs.append(assistant_tool_calls(
            [ToolCall(id=f"c{i}", name="read", arguments={"path": f"f{i}.py"})]
        ))
        msgs.append(tool_result(f"c{i}", content))
    return msgs


def pressure_manager(messages: list[dict], *, level: float, **kwargs) -> ContextManager:
    """把 `token_budget` 定成"这批消息恰好占 level"。

    写死一个 budget 数字的话，`TRUNCATE_BUDGETS` / `_CHAR_PER_TOKEN` 一变，测试
    就会**悄悄落到别的分支上继续通过** —— 看起来还在测同一件事，其实没测。
    """
    total = make_manager(budget=1).estimate_messages(messages)
    return make_manager(budget=int(total / level), **kwargs)


def tool_copies(messages: list[dict]) -> list[str]:
    return [m["content"] for m in messages if m.get("role") == "tool"]


def test_truncation_only_runs_above_warning_line():
    """**不变量**：利用率 < 0.70 时消息逐字节不变 —— 哪怕正文明显超额。

    这条是分级截断里最贵的一条。截断会改中段某条消息 → 那条之后的**前缀缓存
    全部失效**（DeepSeek 是前缀匹配）。低于线还动手的话，每个大工具结果在它被
    snip 掉之前都会破坏一次缓存，收益却只有几个百分点的 utilization —— 净亏。
    而且违反它**不会报任何错**：表现只是缓存命中率曲线不再上升、账单变贵。
    所以这里断言的是"逐字节"，不是"长度差不多"。
    """
    msgs = read_rounds(10, chars=8_000)  # 8k 字符远超 read 的 4k 额度：能缩，但不该缩
    before = [m["content"] for m in msgs]
    before_tools = tool_copies(msgs)
    state = make_state(msgs)
    mgr = pressure_manager(msgs, level=0.60, keep_recent=4, min_keep=2)

    prepared = mgr.prepare(state)

    assert mgr.last_stats.utilization < mgr.snip_threshold
    assert [m["content"] for m in prepared] == before      # 逐字节不变
    assert not any(MARK_PREFIX in text for text in before_tools)  # 前提：正文里本来没有标记
    assert state.usage_stale_reason is None                # 没动过，锚点仍然有效

    # 对照组：同一批消息只要越过线就会被缩 —— 证明上面"没变"是因为线，不是因为坏了
    tight_msgs = read_rounds(10, chars=8_000)
    tight = pressure_manager(tight_msgs, level=0.80, keep_recent=4, min_keep=2)
    tight.prepare(make_state(tight_msgs))
    assert any(MARK_PREFIX in text for text in tool_copies(tight_msgs))


def test_oversized_tool_output_is_truncated_in_place():
    """越过 0.70 且缩完就够 → **只缩正文、不删消息**。

    与 snip 的全部区别就在这条：消息不删 → `tool_call_id` 配对天然完整，
    `_find_cut` 要处理的两类孤儿边界（assistant(tool_calls) 与其结果被切开）
    在这里根本不存在。缩到线下就收手，不再白删一遍。
    """
    msgs = read_rounds(10, chars=8_000)
    state = make_state(msgs)
    mgr = pressure_manager(msgs, level=0.80, keep_recent=4, min_keep=2)
    # 先钉住"这轮落在 warning 带" —— 不然下面测的是 compact 分支，断言会假通过
    assert mgr.snip_threshold <= mgr.account(state).utilization < mgr.compact_threshold

    prepared = mgr.prepare(state)

    assert len(prepared) == len(msgs), "缩够了就不该删消息"
    assert not any(m.get("content") == SNIP_BOUNDARY_MARKER for m in prepared)
    # 配对完整：assistant 的 tool_calls 与 tool 结果一一对应，下一轮请求不会 400
    assert [c["id"] for m in prepared for c in (m.get("tool_calls") or [])] == [
        f"c{i}" for i in range(10)
    ]
    assert [m["tool_call_id"] for m in prepared if m["role"] == "tool"] == [
        f"c{i}" for i in range(10)
    ]
    # 窗口 [min_keep, len - keep_recent) = 索引 2..17 → 8 条 tool 被缩，最近 2 轮不动
    assert sum(1 for text in tool_copies(prepared) if MARK_PREFIX in text) == 8
    assert mgr.account(state).utilization < mgr.snip_threshold   # 缩到线下
    assert state.usage_stale_reason == "tool_output_truncated"


def test_tail_conclusion_survives_truncation():
    """留头 70% + **尾 30%**，不是只留头。

    grep/pytest 这类输出的结论在**尾部**（失败摘要、`N passed` 总计行），
    只留头等于把最该看的那几行丢掉。
    """
    mid, tail = "MIDDLE-gone-4c\n", "\n3 failed, 12 passed in 4.21s"
    msgs = read_rounds(10, chars=8_000, mid=mid, tail=tail)
    mgr = pressure_manager(msgs, level=0.80, keep_recent=4, min_keep=2)

    mgr.prepare(make_state(msgs))

    first = tool_copies(msgs)[0]
    assert tail.strip() in first      # 结论还在
    assert first.startswith("xxx")    # 头部也在
    assert mid not in first           # 省掉的是中部


def test_truncation_marker_says_how_much_was_omitted():
    """标记要写明**省了多少字符**：只说"已省略"模型无法判断要不要重新取一次。"""
    msgs = read_rounds(10, chars=8_000)
    mgr = pressure_manager(msgs, level=0.80, keep_recent=4, min_keep=2)

    mgr.prepare(make_state(msgs))

    assert TRUNCATED_MARK.format(n=8_000 - READ_BUDGET) in tool_copies(msgs)[0]


def test_persisted_placeholder_is_never_truncated():
    """含 `<persisted-output>` 的是**路径指针**，缩掉就等于废掉落盘的全部意义。

    `tool_result.py` 整个机制就是为了"别丢信息"：正文落盘、上下文里只留短预览 +
    文件路径。把这条预览缩了，模型就再也拿不到那个路径了。
    """
    msgs = read_rounds(10, chars=8_000)
    placeholder = (
        f"{TAG_OPEN}\n完整输出已落盘：data/tool_results/s1/c0.txt\n"
        f"预览：" + "y" * 20_000 + f"\n{TAG_CLOSE}"
    )
    msgs[3] = tool_result("c0", placeholder)
    mgr = pressure_manager(msgs, level=0.80, keep_recent=4, min_keep=2)

    mgr.prepare(make_state(msgs))

    assert msgs[3]["content"] == placeholder       # 指针原样
    assert MARK_PREFIX in msgs[5]["content"]       # 同窗口的别人被缩了 → 这轮真跑过


def test_recent_window_and_prefix_are_protected():
    """前缀（`_PREFIX_LEN`：system + 任务）与最近 `keep_recent` 条不碰。

    模型正在用的结果不能动：它刚读到一半的内容下一轮就变了。比"变了"更糟的是
    **悄悄变了** —— 模型会以为自己记错了，然后基于错误记忆继续做。
    """
    msgs = read_rounds(10, chars=8_000)
    prefix = [dict(m) for m in msgs[:_PREFIX_LEN]]
    recent = [m["content"] for m in msgs[-4:]]
    mgr = pressure_manager(msgs, level=0.80, keep_recent=4, min_keep=2)

    mgr.prepare(make_state(msgs))

    assert msgs[:_PREFIX_LEN] == prefix
    assert [m["content"] for m in msgs[-4:]] == recent
    assert MARK_PREFIX in msgs[5]["content"], "窗口内也没动 → 这条测试在空转"


def test_failure_result_keeps_a_larger_budget():
    """失败原文的额度**统一放大**：它是模型自修复的唯一线索（「失败一律文本回喂」）。

    判据只能是文案前缀 —— OpenAI 的消息格式里 tool 消息**没有 success 字段**，
    成功与否只体现在文本上。所以这里既钉额度，也钉"前缀认得出失败"。
    """
    failure = "工具执行失败：pytest 退出码 1\n" + "e" * 18_000
    success = "s" * 18_000
    msgs = read_rounds(3, chars=18_000)
    msgs[3] = tool_result("c0", failure)
    msgs[5] = tool_result("c1", success)
    mgr = pressure_manager(msgs, level=0.80, keep_recent=2, min_keep=2)

    mgr.prepare(make_state(msgs))

    assert TRUNCATED_MARK.format(n=len(failure) - FAILURE_BUDGET) in msgs[3]["content"]
    assert TRUNCATED_MARK.format(n=len(success) - READ_BUDGET) in msgs[5]["content"]
    assert READ_BUDGET < FAILURE_BUDGET  # 前提：失败额度确实更大，否则上面两条是同一件事


def test_truncation_sets_stale_reason():
    """内容变了 → 旧 provider 锚点度量的已不是当前上下文，必须置 stale。

    不置的话就会**拿旧 usage 度量新上下文** —— `context.py` 模块 docstring 里
    写过的那个经典错误：账越记越偏，而且偏得无声无息。
    """
    msgs = read_rounds(10, chars=8_000)
    state = make_state(msgs)
    mgr = pressure_manager(msgs, level=0.80, keep_recent=4, min_keep=2)

    mgr.prepare(state)

    assert state.usage_stale_reason == "tool_output_truncated"


def test_truncation_is_idempotent():
    """同一条只缩一次，判据是**标记字符串**而不是"长度变小了"。

    按长度判断的话，内容恰好等于额度时会把同一条反复缩（每缩一次都在
    `usage_stale_reason` 上再踩一脚）。这里直接调 `_truncate_oversized`
    而不是走 `prepare`：幂等是这个单元自己的性质，绕一圈反而测不准。
    """
    msgs = read_rounds(10, chars=8_000)
    state = make_state(msgs)
    mgr = pressure_manager(msgs, level=0.80, keep_recent=4, min_keep=2)

    assert mgr._truncate_oversized(state) > 0
    snapshot = [m["content"] for m in msgs]
    assert mgr._truncate_oversized(state) == 0            # 第二次一条都不动
    assert [m["content"] for m in msgs] == snapshot


def test_truncation_that_is_not_enough_still_falls_through_to_snip():
    """缩了但还越线 → 继续走 snip。截断不是 snip 的替代，是流水线的第 0 级。

    只缩不删的话这个上下文仍然超预算，必须让后面的级别接手；而且 `prepare`
    返回前不能再假装"只缩短了文本" —— 更强的失效理由（消息位置变了）要覆盖掉它。
    """
    msgs = read_rounds(10, chars=8_000)
    state = make_state(msgs)
    mgr = pressure_manager(msgs, level=0.80, keep_recent=16, min_keep=2)
    calls: list[int] = []
    real = mgr._truncate_oversized
    mgr._truncate_oversized = lambda s: (calls.append(1), real(s))[1]

    prepared = mgr.prepare(state)

    assert calls == [1], "第 0 级没跑"
    assert prepared[2] == {"role": "user", "content": SNIP_BOUNDARY_MARKER}  # 第 1 级接手
    assert state.usage_stale_reason == "snip_compact"      # 更强的理由覆盖截断
    assert mgr.account(state).utilization < mgr.snip_threshold
