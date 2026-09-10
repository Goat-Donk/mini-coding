"""M5-1 tests: eval/golden_tasks.py（本地迷你 git 仓库离线验证，不联网）。"""
from eval.golden_tasks import build_task, discover_fix_commits, git, judge, materialize, remove_worktree


def test_discover_fix_commits(fixture_repo):
    """只发现同时改源码+tests、subject 含 fix 关键字的提交。"""
    repo, base, fix = fixture_repo
    commits = discover_fix_commits(repo)
    assert len(commits) == 1
    c = commits[0]
    assert c["sha"] == fix
    assert c["subject"] == "fix: correct adder result"
    assert c["source_files"] == ["src/app.py"]
    assert c["test_files"] == ["tests/test_app.py"]


def test_build_task_uses_parent_as_base(fixture_repo):
    """base_sha = fix 提交的父提交（bug 存在状态）；hidden_tests 是 fix 版测试。"""
    repo, base, fix = fixture_repo
    task = build_task(repo, discover_fix_commits(repo)[0])
    assert task.id == fix[:8]
    assert task.base_sha == base
    assert task.changed_sources == ["src/app.py"]
    assert "assert add(1, 2) == 3" in task.hidden_tests["tests/test_app.py"]
    # task_text 是真实 bug 报告（不造假）：含 commit subject
    assert "fix: correct adder result" in task.task_text
    assert "请定位并修复这个 bug" in task.task_text


def test_judge_without_fix_fails(tmp_path, fixture_repo):
    """base 状态（未修复）→ fix 版隐藏测试失败。"""
    repo, base, fix = fixture_repo
    task = build_task(repo, discover_fix_commits(repo)[0])
    ws = tmp_path / "ws"
    materialize(task, ws, repo)
    try:
        assert (ws / "src" / "app.py").exists()  # base 状态已检出
        r = judge(task, ws)
        assert r.passed is False
        assert r.returncode != 0
    finally:
        remove_worktree(repo, ws)


def test_judge_passes_with_correct_fix(tmp_path, fixture_repo):
    """agent 正确修复（等价于 fix commit 的源码改动）→ 隐藏测试通过。"""
    repo, base, fix = fixture_repo
    task = build_task(repo, discover_fix_commits(repo)[0])
    ws = tmp_path / "ws"
    materialize(task, ws, repo)
    try:
        fixed_src = git(repo, "show", f"{fix}:src/app.py")  # 金标准修复
        (ws / "src" / "app.py").write_text(fixed_src, encoding="utf-8")
        r = judge(task, ws)
        assert r.passed is True
        assert r.returncode == 0
        assert "passed" in r.summary or r.summary == ""
    finally:
        remove_worktree(repo, ws)


def test_materialize_isolates_worktree(tmp_path, fixture_repo):
    """materialize 不污染主仓库（worktree 隔离）。"""
    repo, base, fix = fixture_repo
    task = build_task(repo, discover_fix_commits(repo)[0])
    ws = tmp_path / "ws"
    materialize(task, ws, repo)
    try:
        assert (ws / "src" / "app.py").read_text(encoding="utf-8").startswith(
            "def add(a, b):\n    return a + b + 1"
        )
        assert "return a + b\n" in (repo / "src" / "app.py").read_text(encoding="utf-8")
    finally:
        remove_worktree(repo, ws)
