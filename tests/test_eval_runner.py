"""M5-2 tests: eval/runner.py（离线 mock 冒烟：物化→agent→judge→清理→报告）。"""
import pytest

from eval.runner import estimate_cost_cny, run_eval


def test_estimate_cost_cny():
    """1M 命中×¥0.5 + 1M 未命中×¥2 + 1M 输出×¥8 = ¥10.5。"""
    assert estimate_cost_cny(1_000_000, 1_000_000, 1_000_000) == pytest.approx(10.5)
    assert estimate_cost_cny(0, 0, 0) == 0.0


def test_run_eval_mock_smoke(tmp_path, fixture_repo):
    """mock 冒烟：整条管线（物化→agent→judge→清理→报告）离线跑通。

    mock agent 不会修 bug → judge 失败（passed=0），但 pipeline 无异常、报告完整。
    """
    repo, base, fix = fixture_repo
    report = run_eval(repo, ws_root=tmp_path / "ws", limit=5, mock=True)

    assert report["mode"] == "mock"
    assert report["tasks"] == 1            # fixture 只有一个 fix commit
    assert report["passed"] == 0           # mock 不修 bug，诚实判定失败
    assert report["completion_rate"] == 0.0
    assert report["total_tokens"] == 0     # mock usage 全零
    assert report["total_cost_cny"] == 0.0

    row = report["per_task"][0]
    assert row["id"] == fix[:8]
    assert row["passed"] is False
    assert row["error"] is None            # pipeline 本身无异常
    assert row["steps"] == 1               # mock 一步给结论
    # 工作区已清理（worktree remove）
    assert not (tmp_path / "ws" / row["id"]).exists()


def test_runner_report_fields(tmp_path, fixture_repo):
    """报告含逐任务明细（id/title/tokens/cost/duration），可 JSON 序列化。"""
    repo, base, fix = fixture_repo
    report = run_eval(repo, ws_root=tmp_path / "ws", limit=5, mock=True)
    row = report["per_task"][0]
    assert row["title"] == "fix: correct adder result"
    assert isinstance(row["duration_s"], float)
    assert isinstance(row["tokens"], int) or row["tokens"] is None
    import json
    json.dumps(report, ensure_ascii=False)  # 可落盘
