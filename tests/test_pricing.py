"""定价快照 tests（离线，不联网）。

这些用例存在的理由：成本列是本项目唯一一个"看起来精确、实际依据会腐烂"的数字。
它出错的方式不是崩，而是**静默给出一个可信的错数** —— 所以每条不确定性都要有断言钉住。
"""
import pytest

from agent.pricing import (
    FALLBACK_SNAPSHOT_ID,
    SNAPSHOTS,
    PriceSnapshot,
    pricing_note,
    resolve,
    snapshot_for,
)


def test_snapshot_for_default_model():
    """项目当前跑的 deepseek-chat 有快照，且它是 archived（已不在定价页列示）。"""
    s = snapshot_for("deepseek-chat")
    assert s.id == "deepseek-chat@2025"
    assert s.status == "archived"
    assert s.verified_on is None          # 从未核实过 —— 不编日期
    assert (s.input_hit, s.input_miss, s.output) == (0.5, 2.0, 8.0)


def test_archived_snapshot_is_flagged_not_hidden():
    """用 archived 快照估成本时必须给出告警，不能悄悄算。"""
    s, warning = resolve("deepseek-chat")
    assert s.id == "deepseek-chat@2025"
    assert warning is not None
    assert "未经核实" in warning


def test_active_model_has_no_warning():
    """现行模型（已核实）不该有告警。"""
    s, warning = resolve("deepseek-flash")
    assert s.status == "active"
    assert s.verified_on == "2026-09-12"
    assert warning is None


def test_peak_tier_is_chosen_over_offpeak():
    """高峰/空闲两档都存在时取高峰 —— 估成本要上界，不能用偏小的那个数。"""
    s = snapshot_for("deepseek-flash")
    assert "peak" in s.id and "offpeak" not in s.id
    assert s.input_miss == 2.0            # 高峰，不是空闲的 1.0
    pro = snapshot_for("deepseek-v4-pro")
    assert pro.input_miss == 9.0


def test_unknown_model_falls_back_instead_of_raising():
    """查不到价 **不返回 None、不抛异常**：生成报告不该因为一个陌生模型名而失败。"""
    s, warning = resolve("gpt-nonexistent")
    assert isinstance(s, PriceSnapshot)
    assert s.id == FALLBACK_SNAPSHOT_ID
    assert warning is not None
    assert "仅供量级参考" in warning


def test_cost_arithmetic():
    """1M 命中×¥0.5 + 1M 未命中×¥2 + 1M 输出×¥8 = ¥10.5（与 runner 的原口径一致）。"""
    s = snapshot_for("deepseek-chat")
    assert s.cost_cny(1_000_000, 1_000_000, 1_000_000) == pytest.approx(10.5)
    assert s.cost_cny(0, 0, 0) == 0.0


def test_every_snapshot_declares_its_status_and_source():
    """每条快照都必须说清自己的状态与来源 —— 没有 note 的快照等于没有口径。"""
    for s in SNAPSHOTS:
        assert s.status in ("active", "archived"), s.id
        assert len(s.note) > 20, s.id
        if s.status == "active":
            assert s.verified_on, f"{s.id} 是现行价却没写核实日期"
            assert "http" in s.note, f"{s.id} 是现行价却没写来源链接"


def test_pricing_note_mentions_snapshot_and_uncertainty():
    """报告里的成本口径说明要能被直接读懂：说清用哪条快照、以及它可不可信。"""
    note = pricing_note("deepseek-chat")
    assert "deepseek-chat@2025" in note
    assert "从未" in note or "未经核实" in note
    active = pricing_note("deepseek-flash")
    assert "2026-09-12" in active
    assert "⚠️" not in active


def test_no_duplicate_snapshot_ids():
    """快照 id 会写进报告、也可能写进历史报告 —— 重复 id 会让两份报告无法区分。"""
    ids = [s.id for s in SNAPSHOTS]
    assert len(ids) == len(set(ids))
