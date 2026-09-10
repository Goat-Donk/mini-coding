"""M3-2 tests: agent/tool_result.py（超大结果落盘 + 预览替换 + 批预算）。"""
from pathlib import Path

from agent.llm import LLMResult, MockLLM
from agent.loop import QueryEngine
from agent.session import Session
from agent.tool_result import (
    BATCH_BUDGET,
    PERSIST_THRESHOLD,
    PREVIEW_CHARS,
    TAG_OPEN,
    ToolResultStore,
    compact_batch,
)
from agent.tools.base import ToolRegistry


def make_engine(tmp_path: Path, llm: MockLLM, **kwargs) -> QueryEngine:
    return QueryEngine(
        llm,
        ToolRegistry.default(tmp_path),
        workspace_root=tmp_path,
        **kwargs,
    )


def make_store(tmp_path: Path, session: str = "s1") -> ToolResultStore:
    return ToolResultStore(tmp_path, session)


def test_small_output_unchanged(tmp_path):
    store = make_store(tmp_path)
    res = store.persist("c1", "小输出")
    assert res.persisted is False
    assert res.text == "小输出"
    assert res.rel_path is None
    assert not (tmp_path / "data").exists()


def test_large_output_persisted(tmp_path):
    store = make_store(tmp_path, "sess_a")
    big = "y" * (PERSIST_THRESHOLD + 10)
    res = store.persist("c_big", big)
    assert res.persisted is True
    # 替换文本：标签 + 总量 + 路径 + 预览
    assert TAG_OPEN in res.text
    assert f"Output too large ({len(big)} chars)" in res.text
    assert "data/tool-results/sess_a/c_big.txt" in res.text
    assert big[:PREVIEW_CHARS] in res.text
    # 文件真实落盘
    file = tmp_path / "data" / "tool-results" / "sess_a" / "c_big.txt"
    assert file.read_text(encoding="utf-8") == big
    assert res.rel_path == "data/tool-results/sess_a/c_big.txt"


def test_persist_reuses_same_id(tmp_path):
    store = make_store(tmp_path)
    big = "z" * (PERSIST_THRESHOLD + 10)
    res1 = store.persist("dup", big)
    res2 = store.persist("dup", big)
    assert res1.rel_path == res2.rel_path
    assert len(list((tmp_path / "data").rglob("*.txt"))) == 1  # 只写一次


def test_session_isolated(tmp_path):
    store_a = make_store(tmp_path, "sa")
    store_b = make_store(tmp_path, "sb")
    big = "w" * (PERSIST_THRESHOLD + 10)
    res_a = store_a.persist("c1", big)
    res_b = store_b.persist("c1", big)
    assert res_a.rel_path != res_b.rel_path
    assert (tmp_path / "data" / "tool-results" / "sa" / "c1.txt").exists()
    assert (tmp_path / "data" / "tool-results" / "sb" / "c1.txt").exists()


def test_batch_budget_largest_first(tmp_path):
    """5×45K 都低于单条阈值，但总和 225K > 200K → 最大优先落盘直到 ≤ 预算。"""
    store = make_store(tmp_path)
    size = 45_000
    texts = {f"c{i}": "q" * size for i in range(5)}
    results = [(cid, text) for cid, text in texts.items()]  # 全部等长
    replaced = compact_batch(results, store)
    persisted = [t for _, t in replaced if TAG_OPEN in t]
    inline = [t for _, t in replaced if TAG_OPEN not in t]
    assert len(persisted) == 1  # 落盘 1 条后 inline = 4×45K=180K ≤ 200K
    assert len(inline) == 4
    assert sum(len(t) for t in inline) <= BATCH_BUDGET
    assert sum(len(t) for t in inline) + size > BATCH_BUDGET  # 刚好卡预算边界


def test_batch_budget_prefers_larger(tmp_path):
    """不等长时优先落盘更大的。"""
    store = make_store(tmp_path)
    small = "a" * 40_000
    big = "b" * 45_000  # 都 < 50K 阈值
    results = [("small", small), ("big", big)]  # 85K ≤ 200K → 都不落盘
    replaced = compact_batch(results, store)
    assert all(TAG_OPEN not in t for _, t in replaced)
    # 放大到超预算：big*5 + small 总共 265K
    results2 = [("big1", big), ("big2", big), ("big3", big), ("big4", big), ("big5", big), ("small1", small)]
    replaced2 = compact_batch(results2, store)
    inline = [t for _, t in replaced2 if TAG_OPEN not in t]
    persisted = [t for _, t in replaced2 if TAG_OPEN in t]
    assert len(persisted) >= 1
    assert sum(len(t) for t in inline) <= BATCH_BUDGET
    # 最大（45K）的一定被优先落盘
    assert any("big1.txt" in t for t in persisted)


def test_loop_persists_big_read(tmp_path):
    """loop 集成：读大文件 → 结果落盘 + 上下文里是 <persisted-output> 替换。"""
    big = "x" * 60_000
    (tmp_path / "big.txt").write_text(big, encoding="utf-8")
    seen = {}

    def then_answer(messages, tools):
        for m in messages:
            if m["role"] == "tool":
                seen["last_tool"] = m["content"]
        return LLMResult(content="读完")

    store = make_store(tmp_path, "sess_loop")
    engine = make_engine(
        tmp_path,
        MockLLM.script(
            MockLLM.tool("read", {"path": "big.txt"}).responses[0],
            then_answer,
        ),
        tool_result_store=store,
    )
    result = engine.run("读大文件")
    assert result.terminated_reason == "completed"
    assert TAG_OPEN in seen["last_tool"]
    files = list((tmp_path / "data" / "tool-results" / "sess_loop").glob("*.txt"))
    assert len(files) == 1
    assert len(files[0].read_text(encoding="utf-8")) > PERSIST_THRESHOLD


# ---------- 接线回归（第三个「机制写了但没人接」的实例） ----------

def _run_big_read(tmp_path: Path, **kwargs) -> tuple[str, list[str]]:
    """按给定接线方式跑一次「读大文件」，返回 (terminated_reason, 所有 tool 消息)。

    kwargs 决定入口怎么接线 —— 这正是被测变量。
    """
    (tmp_path / "big.txt").write_text("x" * 60_000, encoding="utf-8")
    captured: list[dict] = []

    def then_answer(messages, tools):
        captured.extend(messages)
        return LLMResult(content="读完")

    engine = make_engine(
        tmp_path,
        MockLLM.script(
            MockLLM.tool("read", {"path": "big.txt"}).responses[0], then_answer
        ),
        **kwargs,
    )
    result = engine.run("读大文件")
    return result.terminated_reason, [m["content"] for m in captured if m["role"] == "tool"]


def test_store_is_wired_when_entry_passes_only_a_session(tmp_path):
    """入口只给 session、不传 tool_result_store 时，落盘必须照样发生。

    **这条测试的形状是有意的。** 上面那条 `test_loop_persists_big_read` 是
    **手工**把 store 传进去的，所以它只能证明「store 好用」，证明不了
    「有人接上它」—— 而真实缺陷恰好就落在这一格：`app/cli.py`、
    `app/ui_streamlit.py`、`eval/runner.py` 三处 `QueryEngine` 构造
    **全都没传 `tool_result_store`**，于是 `compact_batch` 在生产路径上
    从未执行过。机制写完、文档写了、单测全绿，只有真跑才看得出来。

    所以这里**不加 `tool_result_store=`**，按入口的构造方式建引擎。
    它与「hooks 曾整体漏接 CLI」「CLI 曾漏接 permissions」是同一形状的
    第三次，这条测试就是钉住它不再有第四次。
    """
    reason, tool_msgs = _run_big_read(tmp_path, session=Session(tmp_path, "sess_wired"))

    assert reason == "completed"
    assert any(TAG_OPEN in text for text in tool_msgs), "工具结果没被替换 → 接线断了"
    files = list((tmp_path / "data" / "tool-results" / "sess_wired").glob("*.txt"))
    assert len(files) == 1, "session 在场却没落盘 → 接线又断了"
    assert len(files[0].read_text(encoding="utf-8")) > PERSIST_THRESHOLD


def test_no_session_means_no_persist(tmp_path):
    """无 session（eval/runner 的构造方式）→ 不落盘。

    这是**刻意**的：eval 不建 Session，所以它的行为与既有完成率/token
    数字保持不变。这条测试把「刻意」钉成可执行的断言，免得将来有人
    「顺手」把它也接上、悄悄改掉 README 的实测数字。
    """
    reason, tool_msgs = _run_big_read(tmp_path)  # 无 session

    assert reason == "completed"
    assert all(TAG_OPEN not in text for text in tool_msgs)
    assert not (tmp_path / "data" / "tool-results").exists()


def test_store_reused_across_steps_not_rebuilt(tmp_path):
    """store 必须跨步复用：`_written` 记住同 id 同内容不重复写盘。

    每步新建 store 会退化成每步重写同一个文件 —— 功能看起来一样，
    但磁盘行为不同，而这类差异只会在长会话里显形。
    """
    session = Session(tmp_path, "sess_reuse")
    engine = make_engine(
        tmp_path,
        MockLLM.script(MockLLM.tool("read", {"path": "big.txt"}).responses[0]),
        session=session,
    )
    engine.run("第一次")
    snapshot = dict(engine._stores)
    engine.run("第二次")
    assert engine._stores == snapshot, "同一 session 的 store 被重建了"
    assert list(snapshot) == ["sess_reuse"]  # 键是 session_id，不是别的
