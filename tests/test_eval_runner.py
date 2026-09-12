"""M5-2 tests: eval/runner.py（离线 mock 冒烟：物化→agent→judge→清理→报告）。"""
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from agent.llm import MockLLM
from eval.golden_tasks import (
    GoldenTask,
    build_task,
    discover_fix_commits,
    git,
)
from eval.runner import _aggregate, _extract_diff, estimate_cost_cny, run_eval, run_single
from tests.real_fence_desync import REAL_FENCE_DESYNC


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
    assert report["model"] == "mock"
    assert report["leak_reachable"] is False   # 物理剥离后金标准不可达

    row = report["per_task"][0]
    assert row["id"] == fix[:8]
    assert row["fix_sha"] == fix
    assert row["passed"] is False
    assert row["error"] is None            # pipeline 本身无异常
    assert row["steps"] == 1               # mock 一步给结论
    assert row["leak_reachable"] is False
    # P4：mock agent 不改任何东西 → 判定真的跑了、确实一个用例都没通过。
    # 0 与 None 在这里必须分得开（None = 没数出来）。
    assert row["judge_passed_count"] == 0
    # 工作区已清理
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


def test_report_records_which_ruler_and_which_material(fixture_repo, tmp_path):
    """报告要能自证"哪把尺子量的哪堆料"：repo_head 必须是真的那个提交。"""
    repo, base, fix = fixture_repo
    report = run_eval(repo, ws_root=tmp_path / "ws", limit=5, mock=True)
    assert report["repo_head"] == git(repo, "rev-parse", "HEAD")
    assert report["candidates"] == 1
    assert report["repo"] == str(repo)


# ---------- 尺子指纹（B1）----------
#
# `repo_head` 钉"哪堆料"，`ruler` 钉"哪把尺子" —— 此前**只有前一半**。
# 于是"换个解析器把同一批冻结输出重算一遍"这种事（本项目真做过）在报告上看不出来：
# 两份报告长得一模一样、却是两把尺子。


def test_report_carries_the_ruler_fingerprint(fixture_repo, tmp_path):
    """报告要能机检出"这份判定是哪一版评测器做的"：五项俱全、且哈希对的是**对的文件**。

    断言"等于此刻现算的哈希"不是同义反复 —— 它钉的是**配对关系**：
    若有人把两项哈希都算成同一个文件，这条会红。
    """
    import hashlib
    import sys
    from pathlib import Path as _P

    import pytest as _pytest

    from eval.runner import JUDGE_VERSION

    repo, base, fix = fixture_repo
    report = run_eval(repo, ws_root=tmp_path / "ws", limit=5, mock=True)
    ruler = report["ruler"]

    assert set(ruler) == {
        "judge_version", "runner_sha256", "golden_tasks_sha256",
        "pytest_version", "python_version",
    }
    assert ruler["judge_version"] == JUDGE_VERSION
    assert isinstance(ruler["judge_version"], int)   # 人读的版本号，不是哈希

    root = _P(__file__).resolve().parent.parent
    for key, rel in (("runner_sha256", "eval/runner.py"),
                     ("golden_tasks_sha256", "eval/golden_tasks.py")):
        want = hashlib.sha256((root / rel).read_bytes()).hexdigest()[:12]
        assert ruler[key] == want, f"{key} 对不上 {rel}"

    assert ruler["pytest_version"] == _pytest.__version__
    assert ruler["python_version"] == sys.version


def test_file_sha256_reports_unreadable_as_none_not_empty():
    """量不到就是 `None`，不许写空串 —— 空串读起来像"文件是空的"，那是另一回事。"""
    from eval.runner import _file_sha256

    assert _file_sha256("eval/__no_such_file__.py") is None
    assert _file_sha256("eval/runner.py") not in (None, "")


# ---------- 指标诚实性（M5-5）----------
#
# 原来的口径有一个会让完成率凭空变高的洞：分子 `sum(r.passed)` 不看 `error`，
# 而分母 `n - invalid` 把 error 的任务减掉了 —— 于是一个任务可以**进分子不进分母**。
# 极端算例 n=2（一个 passed+error、一个 failed）会打印"完成率 100%（1/1）"。


def _fake_result(tmp_path, *, passed, error=None, invalid_reason=None, steps=1,
                 patch_failed=False, terminated_reason="completed",
                 budget_exhausted=False, judge_tampering=None):
    """手工构造 TaskResult（不走 LLM/磁盘）—— 这些口径测试不该依赖真实运行。"""
    from eval.runner import TaskResult
    task = GoldenTask(
        id="deadbeef", base_sha="0" * 40, fix_sha="1" * 40,
        title="t", task_text="t", changed_sources=[], hidden_tests={},
    )
    run = None if steps is None else _fake_run(steps, terminated_reason)
    return TaskResult(
        task=task, passed=passed, run=run, error=error,
        duration_s=0.0, cost_cny=0.0, invalid_reason=invalid_reason,
        patch_failed=patch_failed, budget_exhausted=budget_exhausted,
        judge_tampering=judge_tampering,
    )


def _fake_run(steps: int, terminated_reason: str = "completed"):
    from agent.llm import Usage
    from agent.loop import RunResult
    return RunResult(
        final_text="", steps=steps, usage=Usage(), events=[],
        terminated_reason=terminated_reason, task="t",
    )


def test_passed_with_error_never_counts_as_passed(tmp_path):
    """passed=True 且 error!=None → 既不进分子，也**不能被悄悄丢掉**。"""
    good = _fake_result(tmp_path, passed=False)
    bad = _fake_result(tmp_path, passed=True, error="judge 未能执行测试（pytest 退出码 4）")
    rep = _aggregate([good, bad])

    assert rep["passed"] == 0        # 分子里没有它 —— 这是原来的 bug
    assert rep["invalid"] == 1       # 明确记成"判定无效"
    assert rep["judged"] == 1        # 分母也不含它（判定器的锅，不冤枉 agent）
    assert rep["completion_rate"] == 0.0    # 而不是 100%（1/1）


def test_zero_change_pass_is_counted_as_failure(tmp_path):
    """agent 一字未改、测试却通过 → 进分母、不进分子（白送分防御）。"""
    gift = _fake_result(
        tmp_path, passed=False, invalid_reason="agent 没改工作区任何文件，测试却通过"
    )
    rep = _aggregate([gift])
    assert rep["passed"] == 0
    assert rep["not_scored"] == 1
    assert rep["invalid"] == 0
    assert rep["judged"] == 1                # **在分母里**（用户口径：只能进分母）
    assert rep["completion_rate"] == 0.0


def test_empty_results_report_no_rate():
    """零任务 → 完成率必须是 None，不是 0 也不是 1。"""
    rep = _aggregate([])
    assert rep["completion_rate"] is None
    assert rep["judged"] == 0


def test_white_gift_task_is_rejected_end_to_end(tmp_path, fixture_repo):
    """端到端：隐藏测试在 base 就能过 + agent 一字未改 → 判定不计分。

    夹具的 base 测试 `assert add(1, 2) == 4` 恰好对 buggy 源码成立 —— 正是
    「白送分」的真实形状（对应 tinydb 里那个只改类型注解的 fix 提交）。
    """
    repo, base, fix = fixture_repo
    base_test = git(repo, "show", f"{base}:tests/test_app.py")
    gift = GoldenTask(
        id="gift0000", base_sha=base, fix_sha=fix, title="白送分",
        task_text="随便看看", changed_sources=[], hidden_tests={"tests/test_app.py": base_test},
    )
    r = run_single(
        gift, repo, ws_root=tmp_path / "ws", llm=MockLLM.text("我没改任何东西")
    )
    assert r.passed is False                 # 原始判定被判为不可采信
    assert r.zero_change is True
    assert r.invalid_reason is not None and "没改" in r.invalid_reason
    assert r.effective_pass is False
    assert r.leak_reachable is False


def test_keep_flag_preserves_workspace(tmp_path, fixture_repo):
    """--keep 真的保留工作区（原来 runner.py 里是 `if not args.keep: pass` 的空实现）。"""
    repo, base, fix = fixture_repo
    task = GoldenTask(
        id="keep0000", base_sha=base, fix_sha=fix, title="t", task_text="看看",
        changed_sources=[], hidden_tests={},
    )
    ws_root = tmp_path / "ws"
    run_single(task, repo, ws_root=ws_root, llm=MockLLM.text("不动"), keep=True)
    assert (ws_root / task.id).exists()

    run_single(task, repo, ws_root=ws_root, llm=MockLLM.text("不动"), keep=False)
    assert not (ws_root / task.id).exists()


def test_materialize_failure_is_per_task_error(tmp_path, fixture_repo, monkeypatch):
    """物化失败只让这一个任务记 error，不再让整批任务中断、报告都不落盘。

    错误信息还必须**可诊断**：sha 不在仓库里时，用户要看到"这个提交不在仓库里"，
    而不是 tarfile 的 `ReadError: empty file`（那是 git 失败时 stdout 为空的副作用）。
    """
    repo, base, fix = fixture_repo
    task = GoldenTask(
        id="boom0000", base_sha="f" * 40, fix_sha=fix, title="坏 base",
        task_text="x", changed_sources=[], hidden_tests={},
    )
    r = run_single(task, repo, ws_root=tmp_path / "ws", llm=MockLLM.text("x"))
    assert r.error is not None and "不在" in r.error
    assert "ReadError: empty file" not in r.error
    assert r.judged is False
    assert r.effective_pass is False
    assert r.leak_reachable is None       # 没物化成功 → 探针没跑，如实记 None


def test_discover_fix_commits_is_used_for_candidate_count(tmp_path, fixture_repo):
    """候选数进报告 —— 闸门前后各有多少任务必须看得见。"""
    repo, base, fix = fixture_repo
    report = run_eval(repo, ws_root=tmp_path / "ws", limit=5, mock=True)
    assert report["candidates"] == len(discover_fix_commits(repo))


# ---------- 有效性闸门接入（M5-5）----------


def test_gate_rejects_white_gift_before_spending_tokens(tmp_path, fixture_repo, monkeypatch):
    """闸门在跑 agent **之前**把白送分任务拒掉：它不进 LLM、不进分子。

    这是"闸门省钱"的可观测判据：agent 一次都没被调用。
    """
    import eval.runner as runner

    repo, base, fix = fixture_repo
    base_test = git(repo, "show", f"{base}:tests/test_app.py")
    # 隐藏测试 = base 版测试 → 在 base 就通过 → 白送分
    gift = GoldenTask(
        id="gift0000", base_sha=base, fix_sha=fix, title="白送分",
        task_text="x", changed_sources=[], hidden_tests={"tests/test_app.py": base_test},
    )

    calls = []
    real = runner.run_single

    def spy(task, repo_dir, **kw):
        calls.append(task.id)
        return real(task, repo_dir, **kw)

    monkeypatch.setattr(runner, "run_single", spy)
    monkeypatch.setattr(runner, "discover_fix_commits", lambda *a, **k: [{"sha": fix}])
    monkeypatch.setattr(runner, "build_task", lambda repo_dir, c: gift)

    report = runner.run_eval(repo, ws_root=tmp_path / "ws", limit=5, mock=True)

    assert calls == []                          # agent 一次都没跑
    assert report["gate"]["enabled"] is True
    assert report["gate"]["valid"] == 0
    assert report["tasks"] == 0
    rejected = report["gate"]["rejected"]
    assert len(rejected) == 1
    assert rejected[0]["id"] == "gift0000"
    assert "白送分" in rejected[0]["reason"]


def test_gate_crash_rejects_only_that_task(tmp_path, fixture_repo, monkeypatch):
    """闸门自身异常只能让**那一个**任务被拒，不能把整批任务连报告一起带走。

    与 `materialize` 是同一个洞，只是上移了一层：闸门是在循环里跑的，
    一个异常冒出去 = 报告一个都不落盘。
    """
    import eval.runner as runner
    import eval.golden_tasks as gt

    repo, base, fix = fixture_repo
    boom = GoldenTask(
        id="boom0000", base_sha="f" * 40, fix_sha=fix, title="坏 sha",
        task_text="x", changed_sources=[], hidden_tests={"tests/test_app.py": "x = 1\n"},
    )
    monkeypatch.setattr(runner, "discover_fix_commits", lambda *a, **k: [{"sha": fix}])
    monkeypatch.setattr(runner, "build_task", lambda repo_dir, c: boom)

    report = runner.run_eval(repo, ws_root=tmp_path / "ws", limit=5, mock=True)
    assert report["gate"]["valid"] == 0
    assert len(report["gate"]["rejected"]) == 1
    assert "闸门自身异常" in report["gate"]["rejected"][0]["reason"]
    assert report["tasks"] == 0                 # 报告照样落盘


def test_no_validate_escape_hatch(tmp_path, fixture_repo):
    """--no-validate 复现旧行为：候选全部照跑，rejected 为空。"""
    repo, base, fix = fixture_repo
    report = run_eval(repo, ws_root=tmp_path / "ws", limit=5, mock=True, validate=False)
    assert report["gate"]["enabled"] is False
    assert report["gate"]["rejected"] == []
    assert report["tasks"] == report["candidates"] == 1


def test_gate_rejected_tasks_are_listed_with_reasons(tmp_path, fixture_repo):
    """被拒任务连同**逐条理由**进报告 —— 排除必须有可复核的依据，不能只有一个数。"""
    repo, base, fix = fixture_repo
    report = run_eval(repo, ws_root=tmp_path / "ws", limit=5, mock=True)
    assert report["gate"]["valid"] == 1                 # 夹具那个任务是真 bug 任务
    assert report["gate"]["rejected"] == []
    assert report["gate"]["oracle_baseline"].startswith("100%")


# ---------- 定价快照接入（M5-5）----------


def test_mock_report_does_not_claim_a_pricing_snapshot(tmp_path, fixture_repo):
    """mock 不花钱 → 成本恒为 0，快照字段如实置 None，不填一个看着像真的的 id。"""
    repo, base, fix = fixture_repo
    report = run_eval(repo, ws_root=tmp_path / "ws", limit=5, mock=True)
    assert report["total_cost_cny"] == 0.0
    assert report["pricing_snapshot_id"] is None
    assert "mock" in report["pricing_note"]


def test_cost_uses_the_passed_snapshot(tmp_path):
    """成本必须按传入的快照算，不是按某个模块级常量算。"""
    from agent.pricing import snapshot_for

    flash = snapshot_for("deepseek-flash")          # 高峰：0.04 / 2 / 8
    chat = snapshot_for("deepseek-chat")            # 0.5 / 2 / 8
    assert estimate_cost_cny(1_000_000, 0, 0, snapshot=flash) == pytest.approx(0.04)
    assert estimate_cost_cny(1_000_000, 0, 0, snapshot=chat) == pytest.approx(0.5)
    # 缺省用项目当前模型的快照
    assert estimate_cost_cny(1_000_000, 1_000_000, 1_000_000) == pytest.approx(10.5)


def test_report_carries_cost_provenance(tmp_path, fixture_repo, monkeypatch):
    """真跑的 report 要带 pricing_snapshot_id 与口径说明（含"未经核实"这类时效性）。"""
    import eval.runner as runner
    from agent.pricing import resolve

    repo, base, fix = fixture_repo
    llm = MockLLM.text("x")
    llm.model = "deepseek-chat"
    llm.base_url = "https://api.deepseek.com"
    monkeypatch.setattr(runner, "_build_llm", lambda mock: llm)

    report = runner.run_eval(repo, ws_root=tmp_path / "ws", limit=5, mock=False)
    assert report["model"] == "deepseek-chat"
    assert report["base_url"] == "https://api.deepseek.com"
    assert report["pricing_snapshot_id"] == "deepseek-chat@2025"
    assert "未经核实" in report["pricing_note"]
    assert report["pricing_warning"] is not None   # archived 快照必须给出告警
    assert resolve("deepseek-chat")[0].id == report["pricing_snapshot_id"]


# ---------- 基线臂（M5-5）----------
#
# （`_task` 与 test_golden_tasks.py 里那个同名：各文件各有一份，避免跨文件 import 测试）


def _task(repo):
    return build_task(repo, discover_fix_commits(repo)[0])

#
# 「循环到底值多少」要有对照。`one-step` 只差一个 `max_steps`（受控对比）；
# `single-shot` 无工具、一次调用、diff 由我们落地 —— 所以它最容易变成稻草人，
# 下面有几条专门钉"别把定位提示送给模型 + 补丁失败不许混进完成率"。


def test_extract_diff_prefers_fenced_block():
    from eval.runner import _extract_diff

    text = "先说明一下\n```diff\ndiff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\n```\n收尾"
    got = _extract_diff(text)
    assert got.startswith("diff --git a/x b/x")
    assert got.endswith("\n") and "收尾" not in got


def test_extract_diff_handles_bare_diff_and_absence():
    from eval.runner import _extract_diff

    assert _extract_diff("这是 diff：\ndiff --git a/x b/x\n--- a/x\n+++ b/x\n") != ""
    # 抠不出来就返回空串（→ 记 patch_failed），**不要猜**
    assert _extract_diff("我不会修，抱歉。") == ""
    assert _extract_diff("") == ""


# ---------- B1：围栏错位（`_FENCE_RE` 回写）----------
#
# 这一节是**先写红、再修**的那个红。5 段夹具见 `tests/real_fence_desync.py`：
# 逐字归档的原文（无密钥、不依赖 gitignore 的 `data/`），对应 M5-5 三臂留档里
# 5 个 `patch_failed`。它们的成因是同一个：回复里 diff 块**前面**有一个
# ```` ```python ```` 块，旧的开围栏只认裸围栏/`diff`/`patch` → 配对整体错位一格
# → 抓不到 diff 块 → 兜底分支从 `diff --git` 一路切到**全文结尾**，把补丁后面
# 那段散文也塞给了 `git apply`。
#
# 为什么这个 bug 一条测试都不变红：当时**没有任何用例**覆盖"diff 块前面还有别的
# 围栏"这个真实形状。下面第 1、2、3 条各自钉住这个形状的一个侧面。


#: diff 的合法行首。用来判断"抠出来的是不是一个**纯**补丁"。
_DIFF_HEADERS = ("diff --git ", "index ", "--- ", "+++ ", "@@ ")


def _is_patch(text: str) -> bool:
    """这段文本是不是纯补丁：每行要么是补丁头/正文，要么是空行。

    比"不含某些关键词"更强 —— 散文只要带进一行就以**任意**形态现形，
    不依赖某一段回复恰好写了 `## 说明`。
    """
    for line in text.splitlines():
        if line == "" or line[:1] in (" ", "+", "-", "\\"):
            continue  # 上下文行 / 增删行 / "\ No newline at end of file" / 空行
        if line.startswith(_DIFF_HEADERS):
            continue
        return False
    return True


def test_fence_desync_fixture_is_the_pathological_shape():
    """夹具本身也要有牙：它必须真的是"diff 块前面还有个围栏、后面还有散文"。

    没有这一条，后来的人只要把夹具里那段 ```` ```python ```` 删掉，
    上面那条回归就变成永远为真 —— 夹具被"修好"了，而 bug 还在。
    """
    assert len(REAL_FENCE_DESYNC) == 5
    for case in REAL_FENCE_DESYNC:
        raw = case.raw_output
        fences = [i for i, line in enumerate(raw.splitlines()) if line.lstrip().startswith("```")]
        assert len(fences) >= 4, f"{case.task_id}: 围栏太少，不是错位形状"
        # 第一个围栏**不是** diff/patch 块 —— 正是它当不了开围栏
        first = raw.splitlines()[fences[0]].lstrip()[3:].strip().lower()
        assert first not in ("diff", "patch"), f"{case.task_id}: 第一个围栏就是 diff 块"
        # 最后一个围栏之后还有散文（兜底分支会把它一起切进来）
        assert raw.splitlines()[fences[-1] + 1:], f"{case.task_id}: 补丁后面没有散文"


@pytest.mark.parametrize("case", REAL_FENCE_DESYNC, ids=lambda c: c.task_id)
def test_extract_diff_does_not_swallow_the_prose_after_the_patch(case):
    """**就是那 5 个假阴性**：抠出来的必须是补丁，不许连补丁后面的散文一起抠。

    散文被带进去的后果不是"少抠一点"，而是 `git apply` 整条失败 → 记
    `patch_failed` → 被摘出完成率分母 —— **一个解析器 bug 直接决定头号数字**。
    """
    diff = _extract_diff(case.raw_output)
    assert diff.startswith("diff --git"), case.task_id
    assert _is_patch(diff), f"{case.task_id}: 抠出来的不是纯补丁（混进了散文）"


def test_extract_diff_pairs_fences_across_an_info_string():
    """开围栏必须接受**任意** info string，否则配对整体错位一格。

    最小复现：`python` 块在前、`diff` 块在后。旧实现的配对视 `python` 块为无物，
    于是把 `python` 块的**闭**围栏当成 diff 块的开围栏，diff 块本身被吃掉。
    """
    text = (
        "分析如下：\n"
        "```python\n"
        "x = 1\n"
        "```\n"
        "\n"
        "```diff\n"
        "diff --git a/x b/x\n"
        "--- a/x\n"
        "+++ b/x\n"
        "@@ -1 +1 @@\n"
        "-a\n"
        "+b\n"
        "```\n"
        "\n"
        "## 说明\n"
        "改好了。\n"
    )
    assert _extract_diff(text) == (
        "diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\n"
    )


def test_extract_diff_marker_mentioned_in_prose_before_the_fence():
    """**两处修复唯一可鉴别的地方**：正文先提了一句 `diff --git`，再给围栏补丁。

    要鉴别，两个条件缺一不可 —— 我一开始只写了第一个，变异体照样溜过去：

    1. 正文里出现 `diff --git` 这个串，而且**在真补丁之前**（兜底的起点是
       `text.find("diff --git")`，取的是**第一次**出现的位置）；
    2. 真补丁前面还有一个**带 info string 的围栏**（否则旧配对根本不塌，
       ```` ```diff ```` 自己就能当开围栏）。

    两条都满足时：旧配对塌掉 → 落到兜底 → 起点落在**那句正文**上 → 抠出一段以
    正文开头的东西；新配对正常闭合 → 直接拿到补丁。

    另有两种形状实测**不能**鉴别，写在这里免得再有人绕一圈：补丁前有
    ```` ```python ````、补丁后是裸围栏（正是归档那 5 个假阴性的形状）—— 只修兜底
    也能抠对，因为兜底停在**补丁自己的闭合围栏**处；补丁后跟带 info string 的围栏
    同理，停的还是补丁自己的闭合围栏。
    ⇒ 两处修复都留着：归档数据上它们等效，但"等效"不等于"冗余"。

    ⚠️ 这个形状在归档的 21 个任务里**没有出现过**（逐个查过），所以它代表的是
    **没被观测到、但真实模型会写**的形状，不是从留档数据里裁的。
    """
    text = (
        "我会给一个 diff --git 格式的补丁。\n"
        "\n"
        "先看现在的实现：\n"
        "\n"
        "```python\n"
        "def get(self, key):\n"
        "    return self.cache.get(key)\n"
        "```\n"
        "\n"
        "补丁：\n"
        "\n"
        "```diff\n"
        "diff --git a/x b/x\n"
        "--- a/x\n"
        "+++ b/x\n"
        "@@ -1 +1 @@\n"
        "-a\n"
        "+b\n"
        "```\n"
    )
    assert _extract_diff(text) == (
        "diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\n"
    )

def test_extract_diff_bare_fallback_stops_at_the_first_bare_fence():
    """没围栏、直接贴 diff 时，兜底不许一路切到全文结尾。

    补丁后面那段散文不是补丁的一部分。这条与上一条是两处独立的修复：
    上一条管"配对"，这一条管"兜底"。只有上一条时，若模型**真的**没写开围栏，
    兜底仍会把散文带进去。
    """
    text = (
        "直接给你补丁：\n"
        "diff --git a/x b/x\n"
        "--- a/x\n"
        "+++ b/x\n"
        "@@ -1 +1 @@\n"
        "-a\n"
        "+b\n"
        "```\n"
        "\n"
        "## 说明\n"
        "改好了，跑过测试了。\n"
    )
    got = _extract_diff(text)
    assert got.startswith("diff --git a/x b/x")
    assert _is_patch(got), f"兜底把散文切进来了: {got[-40:]!r}"


def test_extract_diff_still_ignores_non_diff_fences():
    """放开 info string 不等于"什么块都要"：与补丁无关的块仍须被忽略。

    放宽开围栏的**唯一**代价面就在这里，所以正反两侧都要钉。
    """
    text = (
        "```python\n"
        "print('这段不是补丁')\n"
        "```\n"
        "```text\n"
        "这里也没有补丁体\n"
        "```\n"
    )
    assert _extract_diff(text) == ""


def test_extract_diff_a_mere_mention_is_taken_as_a_patch_pre_existing():
    """只是**提到** `diff --git` 的块也会被当成补丁 —— 这是既有判据的定义，B1 没改。

    判据是"块里含 `diff --git` 这个串"（`_is_diff_block`），B1 让它与**算出了归档
    84.2% 那个反事实修正值**的离线重算实现保持逐字一致。理由：改判据就等于换尺子，
    新报告与那个数字立刻不可比。（那份离线重算脚本在 `evalverify/` —— 该目录被
    gitignore，是本地留档，**不在仓库里**，所以这里不能把它当成可核对的路径。）

    方向是安全的：这种"块"喂给 `git apply` 只会失败 → 记 `patch_failed`
    （如实记失败），不会把散文当成一次成功的修复。

    这里只钉 B1 **确实改掉**的那一半：抠出来的东西里不再拖着那个闭合围栏。
    """
    text = "```text\ndiff --git 只是被提到了一下，没有真的补丁体\n```\n"
    got = _extract_diff(text)
    assert got.startswith("diff --git ")
    assert not any(line.startswith("```") for line in got.splitlines())


def test_extract_diff_picks_the_longest_diff_block():
    """同时有多个候选块时取**最长**的那个 —— 既有规则，B1 保留。

    模型有时先贴一小段示意、再贴真正的补丁；取第一个会把示意当补丁。
    """
    text = (
        "```diff\n"
        "diff --git a/x b/x\n"
        "--- a/x\n"
        "+++ b/x\n"
        "@@ -1 +1 @@\n"
        "-a\n"
        "+b\n"
        "```\n"
        "```diff\n"
        "diff --git a/y b/y\n"
        "--- a/y\n"
        "+++ b/y\n"
        "@@ -1,3 +1,3 @@\n"
        " a\n"
        "-b\n"
        "+B\n"
        " c\n"
        "```\n"
    )
    got = _extract_diff(text)
    assert got.startswith("diff --git a/y b/y")
    assert "a/x" not in got


#: 本地 tinydb 克隆（`eval/repos/` 被 gitignore，所以这条只在有克隆的机器上跑）。
_TINYDB = Path(__file__).resolve().parent.parent / "eval" / "repos" / "tinydb"


@pytest.mark.skipif(
    not (_TINYDB / ".git").exists(),
    reason="本地没有 eval/repos/tinydb 克隆（gitignore 的运行期数据）；"
    "这条要在真仓库上验，联网克隆属于付费/联网动作，默认不在这里做",
)
@pytest.mark.parametrize("case", REAL_FENCE_DESYNC, ids=lambda c: c.task_id)
def test_extract_diff_real_false_negatives_now_apply_to_the_real_base(case):
    """端到端：抠出来的补丁，真的能 `git apply --check` 过它的**真实基线**。

    上面几条只证"抠出来的是补丁形状"，这条证"这个补丁对真实底座是可用的"——
    中间隔着 hunk 上下文、行号、`--recount` 的宽容度，不能靠形状推出来。
    （**不跑 apply，只 `--check`**：不落盘、不改工作区。）

    旧实现在这里失败，且报错与归档 `patch_error` 逐字同形：
    `warning: recount: unexpected line: ``` `。
    """
    base = git(str(_TINYDB), "rev-parse", f"{case.fix_sha}^").strip()
    assert base, f"{case.task_id}: 本地克隆里找不到 {case.fix_sha} 的父提交"
    diff = _extract_diff(case.raw_output)

    tmp = Path(tempfile.mkdtemp(prefix="b1_fence_"))
    try:
        # 只还原补丁**碰到的**文件 —— `git archive | tar` 在本机 Defender 下会偶发
        # 建不出文件（实测），逐文件 `git show` 是确定性的。
        for rel in re.findall(r"^\+\+\+ b/(.+)$", diff, re.MULTILINE):
            blob = subprocess.run(
                ["git", "-C", str(_TINYDB), "show", f"{base}:{rel}"],
                capture_output=True,
            )
            assert blob.returncode == 0, f"{case.task_id}: 基线里没有 {rel}"
            target = tmp / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(blob.stdout)

        patch = tmp / "_patch.diff"
        patch.write_text(diff, encoding="utf-8", newline="\n")
        proc = subprocess.run(
            ["git", "apply", "--check", "--recount", str(patch)],
            cwd=str(tmp), capture_output=True, text=True,
        )
        assert proc.returncode == 0, f"{case.task_id}: {proc.stderr.strip()}"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_single_shot_prompt_feeds_sources_but_no_test_files(tmp_path, fixture_repo):
    """喂料范围是判据：给全部非 tests 源码，**不给**任何定位提示、不含测试文件。

    给 `changed_sources` 就等于把"定位"送给模型，那这个基线就不是基线的水平了。
    """
    from eval.runner import _collect_sources, single_shot_prompt
    from eval.golden_tasks import materialize, remove_workspace

    repo, base, fix = fixture_repo
    task = _task(repo)
    ws = tmp_path / "ws"
    materialize(task, ws, repo)
    try:
        prompt = single_shot_prompt(task, ws)
        assert "src/app.py" in prompt                      # 源码进去了
        assert "return a + b + 1" in prompt
        assert "tests/test_app.py" not in prompt           # 测试文件没进去
        assert "assert add(1, 2) == 4" not in prompt       # 连 base 版测试都没进去
        assert task.task_text in prompt
        assert list(_collect_sources(ws)) == ["src/app.py"]  # 只收非 tests 的 .py
    finally:
        remove_workspace(ws)


def test_apply_patch_reports_a_reason_on_garbage(tmp_path, fixture_repo):
    from eval.runner import _apply_patch

    repo, base, fix = fixture_repo
    ok, err = _apply_patch(repo, "这不是一个补丁\n")
    assert ok is False
    assert err                                   # 必须带原因，不能只说"失败了"
    assert "not a git repository" not in err.lower()


def test_one_step_arm_stops_after_one_step(tmp_path, fixture_repo):
    """`one-step` 与 agent 臂只差一个 max_steps —— 这是受控对比的前提。

    判据取两处：步数是 1，且终止原因是 `max_steps`（说明是**预算用尽**停的，
    不是模型自己说完了）。第二个 mock 响应**不该被消费**。
    """
    from agent.llm import LLMResult, ToolCall

    repo, base, fix = fixture_repo
    task = _task(repo)
    llm = MockLLM.script(
        LLMResult(content=None, tool_calls=[
            ToolCall(id="c1", name="read", arguments={"path": "src/app.py"})
        ]),
        LLMResult(content="done"),          # 消费到它 = 多跑了一轮
    )
    r = run_single(task, repo, ws_root=tmp_path / "ws", llm=llm, arm="one-step")
    assert r.run.steps == 1
    assert r.run.terminated_reason == "max_steps"
    assert llm.responses == [LLMResult(content="done")] or len(llm.responses) == 1


def test_single_shot_arm_can_actually_fix_the_bug(tmp_path, fixture_repo):
    """单发臂**测得动**：模型给出真 diff → git apply 落地 → judge 通过。

    这条是"基线不是稻草人"的正向证据：一个永远 0% 的基线可能只是补丁没落地。
    """
    repo, base, fix = fixture_repo
    task = _task(repo)
    diff = (
        "diff --git a/src/app.py b/src/app.py\n"
        "--- a/src/app.py\n"
        "+++ b/src/app.py\n"
        "@@ -1,2 +1,2 @@\n"
        " def add(a, b):\n"
        "-    return a + b + 1  # 故意 bug\n"
        "+    return a + b\n"
    )
    llm = MockLLM.text("说明如下\n```diff\n" + diff + "```\n")
    r = run_single(task, repo, ws_root=tmp_path / "ws", llm=llm, arm="single-shot")

    assert r.patch_failed is False
    assert r.patch_error is None
    assert r.zero_change is False        # 工作区真的被改了
    assert r.passed is True              # 隐藏测试通过
    assert r.effective_pass is True
    assert r.run.steps == 1              # 单发 = 一次调用
    assert r.prompt is not None and "src/app.py" in r.prompt   # prompt 留档
    assert r.raw_output is not None and "```diff" in r.raw_output


def test_mock_single_shot_reports_patch_failed_not_a_zero_score(tmp_path, fixture_repo):
    """mock 的回复里没有 diff → 记 patch_failed，**不当成"修不好"**。

    与 `judged` 的口径一致：分母不含它，单独计数。否则一个没有 diff 的基线
    会伪装成"模型一个都没修好"。
    """
    repo, base, fix = fixture_repo
    task = _task(repo)
    r = run_single(task, repo, ws_root=tmp_path / "ws", llm=MockLLM.text("我不会"), arm="single-shot")
    assert r.patch_failed is True
    assert "找不到 unified diff" in r.patch_error
    assert r.judged is False
    assert r.effective_pass is False


def test_patch_failed_is_excluded_from_the_denominator(tmp_path):
    """补丁没应用的任务不进分母、也不进分子，只单独计数。"""
    broke = _fake_result(tmp_path, passed=False, patch_failed=True)
    fixed = _fake_result(tmp_path, passed=True)
    rep = _aggregate([broke, fixed])
    assert rep["patch_failed"] == 1
    assert rep["invalid"] == 1        # 没进分母
    assert rep["judged"] == 1
    assert rep["passed"] == 1
    assert rep["completion_rate"] == 1.0   # 分母只有那个补丁落地的任务


def test_report_records_the_arm(tmp_path, fixture_repo):
    repo, base, fix = fixture_repo
    for arm in ("agent", "one-step"):
        report = run_eval(repo, ws_root=tmp_path / "ws", limit=5, mock=True, arm=arm)
        assert report["arm"] == arm


def test_unknown_arm_is_rejected(tmp_path, fixture_repo):
    repo, base, fix = fixture_repo
    with pytest.raises(ValueError, match="未知的臂"):
        run_eval(repo, ws_root=tmp_path / "ws", limit=5, mock=True, arm="bogus")


# ---------- M5-5 补充：判定守卫 + 预算耗尽标签 ----------

def test_judge_tampering_detects_conftest_and_ini_changes():
    """判定相关文件的新增/改动/删除都要被抓到 —— 这是 conftest 注入绕过的探测器。"""
    from eval.runner import _judge_tampering

    before = {"tinydb/table.py": "a", "conftest.py": "old", "pytest.ini": "ini"}
    after = {
        "tinydb/table.py": "b",          # 源码改动：**不算**判定篡改（那是正常修复）
        "conftest.py": "new",            # 改动 → 抓
        "setup.cfg": "created",          # 新增 → 抓
        # pytest.ini 在 after 里消失 → 删除也要抓
    }
    assert _judge_tampering(before, after) == ["conftest.py", "pytest.ini", "setup.cfg"]


def test_judge_tampering_ignores_ordinary_source_edits():
    """只看判定相关文件：正常改源码不该被误判成篡改（否则守卫会把真修复打成 invalid）。"""
    from eval.runner import _judge_tampering

    before = {"tinydb/table.py": "a", "tinydb/queries.py": "c", "tests/test_x.py": "t"}
    after = {"tinydb/table.py": "b", "tinydb/queries.py": "d", "tests/test_y.py": "new"}
    assert _judge_tampering(before, after) == []


def test_judge_tampering_detects_nested_conftest():
    """嵌套目录里的 conftest.py 同样有效（pytest 会在对应目录层加载它）。"""
    from eval.runner import _judge_tampering
    assert _judge_tampering({}, {"tests/conftest.py": "x"}) == ["tests/conftest.py"]


def test_guard_default_is_ON(tmp_path, fixture_repo):
    """默认必须是**开** —— 本轮把守卫口径翻了。

    这条测试是**故意**在翻转时变红的那一条：旧版这里断言的是 `is False`。
    让它红一次，是为了逼"改默认"这件事被有意识地做，而不是被一个默认参数悄悄带走。

    两个入口的默认值**都要**钉住，而且必须一致：只翻 `run_eval`、
    漏掉 `run_single`（或反过来）的话，从命令行走和从库直接调用走会变成两套口径 ——
    这正是本仓"两份拷贝迟早漂移"咬过的那一类。
    """
    import inspect

    for fn in (run_single, run_eval):
        assert inspect.signature(fn).parameters["judge_guard"].default is True, (
            f"{fn.__name__} 的 judge_guard 默认值不是 True"
        )

    repo, base, fix = fixture_repo
    report = run_eval(repo, ws_root=tmp_path / "ws", limit=5, mock=True)
    assert report["judge_guard"] is True
    assert "guard=开" in report["judge_note"]


def test_guard_off_is_still_reachable_to_reproduce_the_archived_ruler(
    tmp_path, fixture_repo
):
    """⚠️ **没检查**（None）与**查过且干净**（[]）必须能分开。

    填 [] 的话，"本轮没开守卫"会被读成"查过了没问题" —— 那正是本项目头号缺陷类
    （机制在、但读出来的意思是错的）。

    而且这个开关**必须留着**：没有它，留档三份报告跑的那套裁判再也复现不出来
    （本次口径翻转正好证明了这一点 —— 默认值一变，旧口径只剩这一个入口）。
    """
    repo, base, fix = fixture_repo
    report = run_eval(repo, ws_root=tmp_path / "ws", limit=5, mock=True,
                      judge_guard=False)
    assert report["judge_guard"] is False
    assert report["per_task"][0]["judge_tampering"] is None
    assert report["per_task"][0]["judge_restored"] is None
    assert "guard=关" in report["judge_note"]


def test_cli_exposes_no_guard_judge_as_the_escape_hatch():
    """命令行上那个开关得真的在。

    翻转默认值时最容易漏的就是它：`judge_guard` 默认变成 True 之后，
    如果只剩 `--guard-judge`（`store_true`），它就成了一个**恒真空开关**，
    而归档口径再也无法从命令行复现。所以这里走一次真 argparse。
    """
    import subprocess
    import sys
    from pathlib import Path

    proc = subprocess.run(
        [sys.executable, "-m", "eval.runner", "--help"],
        capture_output=True, text=True, cwd=str(Path(__file__).resolve().parent.parent),
    )
    assert proc.returncode == 0, proc.stderr
    assert "--no-guard-judge" in proc.stdout
    assert "--guard-judge" not in proc.stdout   # 旧开关已被取代，别留个恒真的壳


def test_guard_on_reports_a_clean_empty_list(tmp_path, fixture_repo):
    """守卫开着且没人碰判定文件 → 空列表（查过了、干净），不是 None。"""
    repo, base, fix = fixture_repo
    report = run_eval(repo, ws_root=tmp_path / "ws", limit=5, mock=True,
                      judge_guard=True)
    assert report["judge_guard"] is True
    assert report["per_task"][0]["judge_tampering"] == []
    assert "guard=开" in report["judge_note"]


def test_guard_marks_a_tampered_pass_as_invalid(tmp_path, fixture_repo, monkeypatch):
    """端到端：篡改 conftest.py 后判"通过" → 必须标 invalid、不进分子。

    这里直接把 judge 换成"永远通过"，模拟 conftest 注入的效果；守卫该独立于
    judge 怎么想，只认"判定相关文件被动过"这个事实。
    """
    import eval.runner as runner
    from eval.golden_tasks import JudgeResult

    repo, base, fix = fixture_repo
    monkeypatch.setattr(
        runner, "judge",
        lambda task, ws: JudgeResult(passed=True, returncode=0, summary="33 skipped"),
    )
    real_snapshot = runner._snapshot_tree
    state = {"n": 0}

    def snapshot(root):
        """第一次（agent 之后、judge 之前）返回被篡改的树，模拟 agent 写了 conftest.py。"""
        state["n"] += 1
        tree = dict(real_snapshot(root))
        if state["n"] == 2:
            tree["conftest.py"] = "tampered"
        return tree

    monkeypatch.setattr(runner, "_snapshot_tree", snapshot)

    guarded = run_eval(repo, ws_root=tmp_path / "ws", limit=5, mock=True, judge_guard=True)
    row = guarded["per_task"][0]
    assert row["judge_tampering"] == ["conftest.py"]
    assert row["passed"] is False                  # 守卫拦下了
    assert "判定相关文件" in row["invalid_reason"]
    assert guarded["passed"] == 0

    # 同一情形、守卫关掉 → 这就是被 PoC 证明的那个洞：照单全收。
    # **必须显式传 False**：默认已经是开的，靠默认值就复现不出旧口径了。
    state["n"] = 0
    unguarded = run_eval(repo, ws_root=tmp_path / "ws2", limit=5, mock=True,
                         judge_guard=False)
    assert unguarded["per_task"][0]["judge_tampering"] is None
    assert unguarded["per_task"][0]["passed"] is True


def test_budget_exhausted_is_derived_from_terminated_reason():
    """推导本身要能被直测：**步数完全相同**的两个 run，只有 terminated_reason 不同。

    这条是补上来的 —— 原来只测 `_aggregate` 的话，用例可以直接手工传
    `budget_exhausted=True` 绕开推导，于是"推导写成 `steps >= 25`"没有任何测试会红
    （变异测试实测记 MISS，正是本项目头号缺陷类）。
    """
    from eval.runner import _budget_exhausted
    from agent.llm import Usage
    from agent.loop import RunResult

    def run(steps, reason):
        return RunResult(final_text="", steps=steps, usage=Usage(), events=[],
                         terminated_reason=reason, task="t")

    assert _budget_exhausted(run(25, "max_steps")) is True
    assert _budget_exhausted(run(25, "completed")) is False   # 步数一样，但不是撞预算
    assert _budget_exhausted(run(3, "max_steps")) is True     # one-step 臂：1 步就撞
    assert _budget_exhausted(None) is False                   # 没跑起来（error）


def test_budget_exhausted_comes_from_terminated_reason_not_step_count(tmp_path):
    """撞预算要用 loop 给的终止原因判，**不能**用 `steps == max_steps` 猜。

    反例：模型恰好在最后一步自己收工（steps 一样，但 terminated_reason=completed）。
    """
    from eval.runner import _aggregate
    hit_budget = _fake_result(
        tmp_path, passed=False, steps=25,
        terminated_reason="max_steps", budget_exhausted=True,
    )
    finished_last_step = _fake_result(
        tmp_path, passed=False, steps=25, terminated_reason="completed",
    )
    rep = _aggregate([hit_budget, finished_last_step])
    assert rep["budget_exhausted"] == 1     # 只数真的撞了预算的那个
    assert rep["judged"] == 2               # 两个都照样进分母（确实都没修好）


def test_budget_exhausted_excludes_tasks_that_passed_anyway(tmp_path):
    """撞预算但**仍然修好了**的，不该出现在这个标签里（它是"失败的子集"）。"""
    from eval.runner import _aggregate
    lucky = _fake_result(
        tmp_path, passed=True, steps=25,
        terminated_reason="max_steps", budget_exhausted=True,
    )
    rep = _aggregate([lucky])
    assert rep["passed"] == 1
    assert rep["budget_exhausted"] == 0


def test_restore_runs_after_the_snapshot_and_before_judge(tmp_path, fixture_repo, monkeypatch):
    """端到端钉住**三个危险位置**。只看 `passed is False` 是不够的：
    恢复挪位之后"不通过"这个结果可能一模一样，而报告里的含义已经完全错了。

    - 挪到 `after` 快照**之前** → `zero_change` 会变成 True（报告把"改了裁判"读成"空转"）、
      `judge_tampering` 恒为空；
    - 挪到 `judge` **之后** → 篡改已经生效（这里在 judge 被调用的**那一刻**读盘来钉）；
    - 不记 `judge_restored` → 等于"我们悄悄把现场清干净了"。
    """
    import eval.runner as runner
    from agent.llm import Usage
    from agent.loop import RunResult
    from eval.golden_tasks import JudgeResult

    repo, base, fix = fixture_repo
    seen: dict[str, bool] = {}

    def fake_arm(arm, llm, task, ws):
        """模拟 agent 往工作区根写了一个 conftest.py（真实的 PoC 注入点）。"""
        (ws / "conftest.py").write_text(
            "def pytest_collection_modifyitems(items):\n"
            "    for i in items:\n"
            "        i.add_marker('skip')\n",
            encoding="utf-8",
        )
        return runner.ArmOutcome(
            run=RunResult(final_text="", steps=2, usage=Usage(), events=[],
                          terminated_reason="completed", task="t")
        )

    def fake_judge(task, ws):
        # ⚠️ 在被调用的**那一刻**读盘，而不是事后看结果。
        seen["conftest_at_judge"] = (ws / "conftest.py").exists()
        return JudgeResult(passed=True, returncode=0,
                           summary="1 passed in 0.10s", passed_count=1)

    monkeypatch.setattr(runner, "_run_arm", fake_arm)
    monkeypatch.setattr(runner, "judge", fake_judge)

    report = run_eval(repo, ws_root=tmp_path / "ws", limit=5, mock=True)
    row = report["per_task"][0]

    assert seen["conftest_at_judge"] is False           # 恢复点在 judge 之前
    assert row["zero_change"] is False                  # 恢复点也在 after 快照之后
    assert row["judge_tampering"] == ["conftest.py"]    # 证据保留：确实被动过
    assert row["judge_restored"] == ["conftest.py"]     # 而且记录了我们还回去了
    assert row["passed"] is False                       # 恢复 ≠ 免责
    assert "改动了判定相关文件" in row["invalid_reason"]
    assert report["passed"] == 0


def test_restore_is_off_when_the_guard_is_off(tmp_path, fixture_repo, monkeypatch):
    """guard 关着时**不查也不还** —— 恢复清单是 None（没做），不是 []（做了、没事）。"""
    import eval.runner as runner
    from agent.llm import Usage
    from agent.loop import RunResult
    from eval.golden_tasks import JudgeResult

    repo, base, fix = fixture_repo

    def fake_arm(arm, llm, task, ws):
        (ws / "conftest.py").write_text("tampered\n", encoding="utf-8")
        return runner.ArmOutcome(
            run=RunResult(final_text="", steps=2, usage=Usage(), events=[],
                          terminated_reason="completed", task="t")
        )

    def fake_judge(task, ws):
        return JudgeResult(passed=True, returncode=0,
                           summary="1 passed in 0.10s", passed_count=1)

    monkeypatch.setattr(runner, "_run_arm", fake_arm)
    monkeypatch.setattr(runner, "judge", fake_judge)

    report = run_eval(repo, ws_root=tmp_path / "ws", limit=5, mock=True,
                      judge_guard=False)
    row = report["per_task"][0]
    assert row["judge_tampering"] is None
    assert row["judge_restored"] is None
    assert row["passed"] is True          # ← 旧口径：这个洞就是这么敞着的


# ---------- B3：轨迹字段落盘（纯序列化，没有新增采集） ----------
#
# 三条都是"报告里缺一列，于是两种完全不同的东西在数据上同形"：
# 没有 `terminated_reason` → 「环境把它掐死了」与「真没修对」分不开（README S41）；
# 没有工具调用记录 → 「网络寻源未阻断」这句话无法查证；
# 没有 `gate_blocks` → 事后翻轨迹只看到 `success=False`，不知道是哪一层拦的。


def test_tool_call_records_carry_the_aborted_three_state():
    """`aborted` 必须是**三态**：被取消 / 被跳过 / 真的跑了。

    "跳过"（等用户输入时后面的调用不再执行）与"被取消"（abort 触发）都是
    **没有执行**，但成因不同；把它们压成同一个值，报告就再也分不出是哪一种。
    """
    from eval.runner import _tool_call_records

    recs = _tool_call_records([
        {"type": "tool_call", "name": "bash", "success": True, "duration_ms": 12,
         "exit_code": 0, "await_user": False},
        {"type": "tool_call", "name": "read", "success": False, "duration_ms": 0,
         "exit_code": None, "await_user": False, "aborted": True},
        {"type": "tool_call", "name": "read", "success": False, "duration_ms": 0,
         "exit_code": None, "await_user": False, "skipped": True},
        {"type": "message", "text": "非 tool_call 事件不进投影"},
    ])
    assert [r["aborted"] for r in recs] == [False, True, None]
    assert len(recs) == 3
    assert recs[0]["exit_code"] == 0 and recs[2]["exit_code"] is None


def test_only_web_tools_put_arguments_in_the_report():
    """只有 `web_fetch`/`web_search` 落参数，且截断到 300 字符。

    其余工具不落有两个理由，第二个更硬：体积，以及**参数里可能含被注入的文本**
    （把不可信内容原样搬进报告，等于把报告的读者也拉进那条信任链）。
    """
    from eval.runner import _tool_call_records

    recs = _tool_call_records([
        {"type": "tool_call", "name": "web_search", "success": True,
         "arguments": {"query": "x" * 500}},
        {"type": "tool_call", "name": "bash", "success": True,
         "arguments": {"command": "cat /etc/passwd"}},
    ])
    assert recs[0]["arguments"].startswith('{"query"')
    assert len(recs[0]["arguments"]) == 300          # 截断过
    assert "arguments" not in recs[1]                # bash 的命令不落盘


def test_gate_block_records_are_projected():
    """门禁阻断要能在报告里看见（`source` = 哪一层拦的，`reason` = 为什么）。"""
    from eval.runner import _gate_block_records

    recs = _gate_block_records([
        {"type": "gate_block", "tool": "bash", "source": "permissions",
         "reason": "危险命令，已拒绝"},
        {"type": "tool_call", "name": "bash"},
    ])
    assert recs == [{"tool": "bash", "source": "permissions",
                     "reason": "危险命令，已拒绝"}]


def test_per_task_lands_the_trajectory_fields(tmp_path, fixture_repo, monkeypatch):
    """端到端：三列都要出现在 `per_task` 里，且 `terminated_reason` 是**原样**落盘。

    用 `steps=25`（多轮臂的 max_steps）+ `terminated_reason="aborted"` 这一组故意
    错位的值，钉住"它取自 loop、不是从步数猜的" —— 这正是 `_budget_exhausted`
    docstring 里那条纪律的同一件事。
    """
    import eval.runner as runner
    from agent.llm import Usage
    from agent.loop import RunResult

    repo, base, fix = fixture_repo
    events = [
        {"type": "tool_call", "name": "bash", "success": True, "duration_ms": 5,
         "exit_code": 0, "await_user": False},
        {"type": "gate_block", "tool": "bash", "source": "permissions",
         "reason": "危险命令，已拒绝"},
        {"type": "tool_call", "name": "web_fetch", "success": False, "duration_ms": 3,
         "exit_code": None, "await_user": False,
         "arguments": {"url": "https://example.com"}},
        {"type": "tool_call", "name": "bash", "success": False, "duration_ms": 0,
         "exit_code": None, "await_user": False, "aborted": True},
    ]

    def fake_arm(arm, llm, task, ws):
        return runner.ArmOutcome(run=RunResult(
            final_text="", steps=25, usage=Usage(), events=events,
            terminated_reason="aborted", task="t",
        ))

    monkeypatch.setattr(runner, "_run_arm", fake_arm)
    report = run_eval(repo, ws_root=tmp_path / "ws", limit=5, mock=True)
    row = report["per_task"][0]

    assert row["terminated_reason"] == "aborted"      # 原始字符串，不是派生的布尔
    assert row["budget_exhausted"] is False           # 步数=25 也不许猜成"撞预算"
    assert [c["aborted"] for c in row["tool_calls"]] == [False, False, True]
    assert row["tool_calls"][1]["arguments"] == '{"url": "https://example.com"}'
    assert row["gate_blocks"] == [{"tool": "bash", "source": "permissions",
                                   "reason": "危险命令，已拒绝"}]


def test_single_shot_reports_zero_tool_calls_as_an_empty_list(tmp_path, fixture_repo):
    """`single-shot` 臂**没有工具可用** → `[]`。是 `[]` 不是 `None`。

    `None` 的意思是"没测到"，而这里是我们**确知**它一个工具都没有
    （它的 `RunResult` 是手工构造的，`events=[]`）。两者混起来，
    报告会开始怀疑一件根本不存在的测量失败。
    """
    repo, base, fix = fixture_repo
    report = run_eval(repo, ws_root=tmp_path / "ws", limit=5, mock=True,
                      arm="single-shot")
    row = report["per_task"][0]
    assert row["tool_calls"] == []
    assert row["gate_blocks"] == []
    assert row["terminated_reason"] == "completed"
