"""M5-2 tests: eval/runner.py（离线 mock 冒烟：物化→agent→judge→清理→报告）。"""
import pytest

from agent.llm import MockLLM
from eval.golden_tasks import GoldenTask, build_task, discover_fix_commits, git
from eval.runner import _aggregate, estimate_cost_cny, run_eval, run_single


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


def test_guard_off_leaves_tampering_as_null_not_empty(tmp_path, fixture_repo):
    """⚠️ **没检查**（None）与**查过且干净**（[]）必须能分开。

    填 [] 的话，"本轮没开守卫"会被读成"查过了没问题" —— 那正是本项目头号缺陷类
    （机制在、但读出来的意思是错的）。
    """
    repo, base, fix = fixture_repo
    report = run_eval(repo, ws_root=tmp_path / "ws", limit=5, mock=True)
    assert report["judge_guard"] is False
    assert report["per_task"][0]["judge_tampering"] is None
    assert "guard=关" in report["judge_note"]


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

    # 同一情形、守卫关掉 → 这就是被 PoC 证明的那个洞：照单全收
    state["n"] = 0
    unguarded = run_eval(repo, ws_root=tmp_path / "ws2", limit=5, mock=True)
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
