"""M3-2 tests: agent/tool_result.py（超大结果落盘 + 预览替换 + 批预算）。"""
from pathlib import Path

from agent.llm import LLMResult, MockLLM
from agent.loop import QueryEngine
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
