"""M5-1 tests: eval/golden_tasks.py（本地迷你 git 仓库离线验证，不联网）。"""
import os
import stat
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

from eval.golden_tasks import (
    EVAL_COMMIT_MESSAGE,
    GoldenTask,
    JudgeResult,
    TaskValidity,
    _archive_to,
    _force_rmtree,
    _run_pytest,
    build_task,
    discover_fix_commits,
    git,
    judge,
    leak_probe,
    materialize,
    remove_workspace,
    render_task_text,
    validate_task,
    validate_tasks,
)


def _task(repo):
    return build_task(repo, discover_fix_commits(repo)[0])


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
        remove_workspace(ws)


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
        remove_workspace(ws)


def test_materialize_does_not_touch_source_repo(tmp_path, fixture_repo):
    """materialize 不污染主仓库。"""
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
        remove_workspace(ws)


def test_judge_ignores_repo_addopts(tmp_path, fixture_repo):
    """目标仓库 pytest.ini 的 addopts 不能让判定失效（真实事故回归测试）。

    tinydb 的 pytest.ini 写死 `--cov-append --cov-report term --cov tinydb`；本机没装
    pytest-cov 时 pytest 会以 usage error（退出码 4）直接退出——测试一次都没跑，
    却会被 judge 当成「agent 没修好」，把假阴性算进完成率。judge 必须用
    `-o addopts=` 清掉仓库自带的 addopts。
    """
    repo, base, fix = fixture_repo
    task = build_task(repo, discover_fix_commits(repo)[0])
    ws = tmp_path / "ws"
    materialize(task, ws, repo)
    try:
        # 模拟目标仓库带一个本机不存在的插件参数
        (ws / "pytest.ini").write_text(
            "[pytest]\naddopts=--no-such-plugin-flag\n", encoding="utf-8"
        )
        r = judge(task, ws)
        assert r.executed is True     # 测试真的执行了
        assert r.returncode == 1      # base 未修复 → 跑完并失败，而非 usage error 4
        assert r.error is None
    finally:
        remove_workspace(ws)


def test_judge_result_flags_invalid_judgment():
    """退出码 2/3/4/5 = 压根没跑成 → 必须报 error，不能算作「没修好」。"""
    ok = JudgeResult(passed=False, returncode=1, summary="1 failed, 32 passed")
    assert ok.executed is True
    assert ok.error is None

    for code in (2, 3, 4, 5):
        bad = JudgeResult(passed=False, returncode=code, summary="ERROR: usage: ...")
        assert bad.executed is False
        assert bad.error is not None and str(code) in bad.error


# ---------- 物理剥离（M5-5）----------
#
# 为什么这些测试必须存在：原来的 `git worktree add` 让工作区与主仓库共享对象库与
# refs，agent 一条 `git show <fix_sha>:tests/test_app.py` 就拿到隐藏测试全文。
# 下面这几条把"没有未来"钉成可观测的事实，而不是一句声称。


def test_archive_to_is_faithful_on_a_large_archive(tmp_path):
    """大归档：文件集必须与 `git ls-tree -r` 精确相等（物化不能少文件）。

    用 100 000 B 单文件把归档撑过若干 record —— 小夹具走不到的分支在这里走到。
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "blob.bin").write_bytes(bytes((i * 37 + 11) % 251 for i in range(100_000)))
    git(repo, "init", "-q", "-b", "master")
    git(repo, "add", ".")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "base")
    sha = git(repo, "rev-parse", "HEAD")

    target = tmp_path / "out"
    _archive_to(sha, repo, target)
    assert (target / "blob.bin").stat().st_size == 100_000
    in_tree = git(repo, "ls-tree", "-r", "--name-only", sha).split()
    got = sorted(p.relative_to(target).as_posix() for p in target.rglob("*") if p.is_file())
    assert got == sorted(in_tree)


def test_archive_to_survives_symlink_members(tmp_path, capsys):
    """归档里有 symlink 条目时也必须能物化（**真实 tinydb 老提交就是这一支**）。

    `git archive` 把 symlink 存成 `120000` blob；tarfile 的**流模式**（`r|`）解析不了
    它 —— 要回到归档开头重读，而流模式不允许 seek → `seeking backwards is not allowed`。
    实测代价：全量闸门里有 7 个候选（树里都有 `CONTRIBUTING.rst` 这条 `120000` 条目）
    整条被记成"闸门自身异常"，一个都没验成。**同一个归档换成可 seek 的 fileobj 就好**。

    用例结构（顺序是有意的）：
    1. 先证明"陷阱还在"—— 同一份归档，流模式必须炸。这是本用例**自己的牙齿检查**：
       哪天换个写法不再踩这个坑，它会在这里红，而不是让第 2 步悄悄退化成一句虚话。
    2. 再断言 `_archive_to` 不抛异常、正常文件都在。
    3. 最后钉住**不许静默丢失**：symlink 建不出来时必须报出来（静默丢失是本项目
       头号缺陷类）；建得出来（权限/开发者模式允许的机器）则必须真的建出来。两种都对，
       "既没建出来又没吭声"才是错。
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "master")
    (repo / "app.py").write_text("X = 1\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "feat: init")

    # 造一条 symlink 条目。**刻意不在磁盘上建真链接**：Windows 建 symlink 要权限，
    # 而这正是要测的那件事 —— 直接用 cacheinfo 把条目写进 index。
    blob = subprocess.run(
        ["git", "-C", str(repo), "hash-object", "-w", "--stdin"],
        input="docs/CONTRIBUTING.md", text=True, capture_output=True, check=True,
    ).stdout.strip()
    git(repo, "update-index", "--add", "--cacheinfo", f"120000,{blob},NOTES.rst")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "docs: add NOTES")
    sha = git(repo, "rev-parse", "HEAD")
    assert git(repo, "ls-tree", "-r", sha, "NOTES.rst").startswith("120000")

    # 1) 陷阱还在（流模式 → StreamError，TarError 的子类）
    proc = subprocess.Popen(
        ["git", "-C", str(repo), "archive", "--format=tar", sha],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    stream_failed = False
    try:
        with tarfile.open(fileobj=proc.stdout, mode="r|") as tf:
            tf.extractall(tmp_path / "naive", filter="data")
    except tarfile.TarError:
        stream_failed = True
    finally:
        proc.stdout.read()
        proc.stdout.close()
        proc.wait()
    assert stream_failed, "流模式不再被 symlink 打中 → 这个用例失去了牙齿"

    # 2) 正主：同一份归档，_archive_to 不许抛
    target = tmp_path / "out"
    _archive_to(sha, repo, target)
    assert (target / "app.py").read_text(encoding="utf-8") == "X = 1\n"

    # 3) 静默丢失不行：建不出来就得说，建得出来就得在
    link = target / "NOTES.rst"
    assert os.path.lexists(link) or "NOTES.rst" in capsys.readouterr().out


def test_materialize_strips_repo_history(tmp_path, fixture_repo):
    """工作区是一个**只有 base 一个提交**的新仓库：无原始历史、无 fix 提交、无远端。"""
    repo, base, fix = fixture_repo
    task = _task(repo)
    ws = tmp_path / "ws"
    materialize(task, ws, repo)
    try:
        assert git(ws, "rev-list", "--all", "--count") == "1"
        assert git(ws, "log", "-1", "--format=%s") == EVAL_COMMIT_MESSAGE
        assert git(ws, "remote", "-v") == ""
        # 不是"不可达"，是对象**根本不在这个仓库里**
        p = subprocess.run(
            ["git", "-C", str(ws), "cat-file", "-e", fix], capture_output=True
        )
        assert p.returncode != 0
        # base 的内容确实在（不是把一个空壳交出去）
        assert (ws / "src" / "app.py").read_text(encoding="utf-8").startswith(
            "def add(a, b):\n    return a + b + 1"
        )
    finally:
        remove_workspace(ws)


def test_leak_probe_has_teeth(tmp_path, fixture_repo):
    """探针不能是恒假的摆设：同一个 task，在主仓库里必须报泄漏、在隔离区必须不报。"""
    repo, base, fix = fixture_repo
    task = _task(repo)
    assert leak_probe(task, repo) is True     # 主仓库里 fix 提交当然存在
    ws = tmp_path / "ws"
    materialize(task, ws, repo)
    try:
        assert leak_probe(task, ws) is False
    finally:
        remove_workspace(ws)


def test_materialize_leaves_git_usable(tmp_path, fixture_repo):
    """剥离后 agent 仍能用 git 看自己的改动 —— 这是不选"直接拷文件"的理由。"""
    repo, base, fix = fixture_repo
    task = _task(repo)
    ws = tmp_path / "ws"
    materialize(task, ws, repo)
    try:
        (ws / "src" / "app.py").write_text(
            "def add(a, b):\n    return a + b\n", encoding="utf-8"
        )
        assert "src/app.py" in git(ws, "status", "--porcelain")
        assert "-    return a + b + 1" in git(ws, "diff")
    finally:
        remove_workspace(ws)


def test_materialize_self_heals_existing_target(tmp_path, fixture_repo):
    """目标目录已有残留（上一轮 --keep / 崩溃）→ 先清空重建，而不是整批任务死掉。"""
    repo, base, fix = fixture_repo
    task = _task(repo)
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "stale.txt").write_text("上一轮的残骸", encoding="utf-8")
    materialize(task, ws, repo)
    try:
        assert not (ws / "stale.txt").exists()
        assert (ws / "src" / "app.py").exists()
    finally:
        remove_workspace(ws)


def test_remove_workspace_clears_readonly_files(tmp_path):
    """只读文件也要能删掉。

    真实场景：agent 在工作区跑一次 `git gc` → git 把 pack 文件标成只读；
    `shutil.rmtree` 撞上会抛 `PermissionError`。`ignore_errors=True` 不算修好 ——
    它把异常吞掉、残骸留在原地，下一轮物化又要面对非空目录。
    """
    d = tmp_path / "ro"
    (d / "sub").mkdir(parents=True)
    f = d / "sub" / "pack.idx"
    f.write_text("x", encoding="utf-8")
    os.chmod(f, stat.S_IREAD)          # Windows 上就是置只读属性
    try:
        _force_rmtree(d)
        assert not d.exists()
    finally:
        if d.exists():                  # 断言失败时别把不可删的目录留给 pytest 清理
            os.chmod(f, stat.S_IWRITE)


def test_render_task_text_does_not_promise_a_directory():
    """任务文本不能说"只改 src/ 下的源码" —— tinydb 根本没有 src/。

    这句话对全部 39 个真实任务都是错的，而它当时不在任何断言里；
    夹具恰好用 `src/app.py`，所以单测也照不出来。这里直接钉住"不点目录名"。
    """
    text = render_task_text({"subject": "fix: something", "body": ""})
    assert "src/" not in text
    assert "fix: something" in text
    assert "请定位并修复这个 bug" in text


def test_discover_excludes_packaging_files(tmp_path):
    """`setup.py` / `conftest.py` 不是"被测的源码"。

    排除前 `not f.startswith("tests/") and f.endswith(".py")` 会把它们当源码，
    于是"只改打包配置 + 改测试"的提交也会混进候选集。
    """
    repo = tmp_path / "r"
    repo.mkdir()
    g = lambda *a: git(repo, *a)
    g("init")
    (repo / "tests").mkdir()
    (repo / "setup.py").write_text("VERSION = '1'\n", encoding="utf-8")
    (repo / "tests" / "test_x.py").write_text(
        "def test_x():\n    assert True\n", encoding="utf-8"
    )
    g("add", ".")
    g("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-m", "feat: init")

    (repo / "setup.py").write_text("VERSION = '2'\n", encoding="utf-8")
    (repo / "tests" / "test_x.py").write_text(
        "def test_x():\n    assert 1\n", encoding="utf-8"
    )
    g("add", ".")
    g("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-m", "fix: bump version")

    assert discover_fix_commits(repo) == []


# ---------- 有效性闸门（M5-5）：隐藏测试必须在 base 失败、在 fix 通过 ----------
#
# 关键字只负责缩小候选集，**筛掉谁由闸门用事说**："一个 typo 提交该不该算任务"
# 不再是一个人的口味判断。零 LLM 成本，只跑 pytest。


def test_validate_accepts_real_bug_task(fixture_repo):
    """真 bug 任务：base 跑隐藏测试失败（rc=1）、金标准修复后通过（rc=0）。"""
    repo, base, fix = fixture_repo
    v = validate_task(_task(repo), repo)
    assert v.valid is True
    assert v.base_rc == 1
    assert v.fix_rc == 0
    assert v.base_collect_error is False
    assert v.reason is None


def test_validate_rejects_white_gift_task(fixture_repo):
    """隐藏测试在 base 就通过 → 拒。这正是 tinydb `e70f9b1d` 那个只改类型注解的提交。

    不拒的话 agent 一个字不改也能拿分，完成率是被自己记账方式抬高的。
    """
    repo, base, fix = fixture_repo
    base_test = git(repo, "show", f"{base}:tests/test_app.py")
    task = GoldenTask(
        id="gift0000", base_sha=base, fix_sha=fix, title="白送分",
        task_text="x", changed_sources=[], hidden_tests={"tests/test_app.py": base_test},
    )
    v = validate_task(task, repo)
    assert v.valid is False
    assert v.base_rc == 0
    assert "白送分" in v.reason


def test_validate_rejects_unrunnable_oracle(fixture_repo):
    """金标准补丁在本机跑不过 → 拒。这个任务谁都拿不到分，不该占样本。

    fix 侧同时就是 **oracle 基线**：没有它，一个 0% 的完成率和「judge 坏了」
    在报告上长得一模一样。
    """
    repo, base, fix = fixture_repo
    fixed_test = git(repo, "show", f"{fix}:tests/test_app.py")
    task = GoldenTask(
        id="oracle00", base_sha=base, fix_sha=base,  # fix 侧故意指向 bug 状态的树
        title="坏金标准", task_text="x", changed_sources=[],
        hidden_tests={"tests/test_app.py": fixed_test},
    )
    v = validate_task(task, repo)
    assert v.valid is False
    assert v.base_rc == 1
    assert v.fix_rc == 1
    assert "金标准" in v.reason


def test_validate_accepts_collect_error_on_base(tmp_path):
    """base 侧**收集错误**（rc=2）要被接受并打标，不能误杀合法任务。

    pytest 的收集错误是 rc=2（`Session.Interrupted` 继承 `KeyboardInterrupt`）。
    写成"必须 rc==1"会把这一类任务全砍掉 —— 而 fix 提交新增了 base 没有的符号时
    正是这一支。敢接受是因为 fix 侧在守：环境真坏了 fix 侧也过不了。
    """
    repo = tmp_path / "r"
    repo.mkdir()
    g = lambda *a: git(repo, *a)
    g("init")
    (repo / "src").mkdir()
    (repo / "tests").mkdir()
    (repo / "src" / "app.py").write_text(
        "def add(a, b):\n    return a + b\n", encoding="utf-8"
    )
    g("add", ".")
    g("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-m", "feat: adder")
    base = g("rev-parse", "HEAD")
    (repo / "src" / "app.py").write_text(
        "def add(a, b):\n    return a + b\n\n\ndef mul(a, b):\n    return a * b\n",
        encoding="utf-8",
    )
    g("add", ".")
    g("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-m", "fix: add mul")
    fix = g("rev-parse", "HEAD")

    hidden = (
        "import sys, os\n"
        "sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))\n"
        "from app import mul\n\n\ndef test_mul():\n    assert mul(2, 3) == 6\n"
    )
    task = GoldenTask(
        id="collect0", base_sha=base, fix_sha=fix, title="新增符号",
        task_text="x", changed_sources=["src/app.py"],
        hidden_tests={"tests/test_app.py": hidden},
    )
    v = validate_task(task, repo)
    assert v.valid is True
    assert v.base_rc == 2
    assert v.base_collect_error is True     # 接受，但必须打标
    assert v.fix_rc == 0


def test_validate_rejects_other_base_failures(fixture_repo):
    """base 侧 rc ∈ {3,4,5} = 压根没跑成 → 拒（判定无效，不是"agent 没修好"）。

    用"隐藏测试文件里一个 test 函数都没有"造 rc=5（没收集到用例）。
    注意**不能靠往仓库里塞一个未提交的 pytest.ini** 来造 usage error：
    闸门读的是 `git archive` 出来的树，工作区里未提交的文件不在里面。
    """
    repo, base, fix = fixture_repo
    task = GoldenTask(
        id="usage000", base_sha=base, fix_sha=fix, title="空测试文件",
        task_text="x", changed_sources=[],
        hidden_tests={"tests/test_app.py": "HELPER = 1\n"},   # 没有 test_ 函数
    )
    v = validate_task(task, repo)
    assert v.valid is False
    assert v.base_rc == 5
    assert "压根没跑成" in v.reason


def test_validate_rejects_empty_hidden_tests(fixture_repo):
    """hidden_tests 为空 → 直接拒，连 pytest 都不跑。

    可达状态：fix 提交把测试文件改名/删了，`build_task` 会 `continue` 掉它。
    此时 `judge` 会跑**整个套件**（无路径参数）→ 通常全绿 → 白送分。
    """
    repo, base, fix = fixture_repo
    task = GoldenTask(
        id="empty000", base_sha=base, fix_sha=fix, title="无测试",
        task_text="x", changed_sources=[], hidden_tests={},
    )
    v = validate_task(task, repo)
    assert v.valid is False
    assert v.base_rc is None and v.fix_rc is None   # 一次 pytest 都没跑
    assert "无可判定内容" in v.reason


def test_validate_uses_throwaway_dirs(fixture_repo, monkeypatch):
    """闸门必须用一次性临时目录，**绝不能复用 ws_root/task.id**。

    因为 `judge` 会把隐藏测试**写进传入的目录** —— 复用就等于闸门自己制造泄漏，
    把答案提前放进 agent 的工作区。这里同时钉住"用完删干净"。
    """
    import tempfile

    repo, base, fix = fixture_repo
    created: list[Path] = []
    real = tempfile.mkdtemp

    def spy(*a, **k):
        p = real(*a, **k)
        created.append(Path(p))
        return p

    monkeypatch.setattr(tempfile, "mkdtemp", spy)
    validate_task(_task(repo), repo)

    assert len(created) == 2                       # base 侧 + fix 侧
    assert all("evalval" in p.name for p in created)
    assert all(not p.exists() for p in created)    # 用完就删，不留残骸


def test_cli_validate_tolerates_a_broken_candidate(tmp_path, fixture_repo, monkeypatch, capsys):
    """CLI 的 `--validate` 必须**逐条容错 + 逐条打印**。

    这不是假想的缺陷：真实跑全量闸门时踩过两次（一次 `git archive` 报 141、
    一次 `GoldenTask` 下标写错），两次都是**整个离线批次直接中止**。而这个批次要跑
    几十分钟，崩掉就是全白跑，前面跑完的结果一条都看不到。

    判据取三处：不抛异常、坏的那条有**它自己的**理由、汇总行的算术对得上。
    """
    import sys
    import eval.golden_tasks as gt

    repo, base, fix = fixture_repo
    real_task = _task(repo)
    real_build = gt.build_task
    real_validate = gt.validate_task
    seen = {"validate": 0}

    def flaky_build(repo_dir, c):
        return real_build(repo_dir, c)

    def flaky_validate(task, repo_dir):
        seen["validate"] += 1
        if seen["validate"] == 1:
            raise RuntimeError("闸门内部爆炸")
        return real_validate(task, repo_dir)

    # 两个候选（夹具只有一个真提交，喂同一个两次即可）—— 第一个由闸门炸，第二个正常
    monkeypatch.setattr(gt, "discover_fix_commits", lambda *a, **k: [
        {"sha": fix, "subject": "候选甲", "source_files": [], "test_files": []},
        {"sha": fix, "subject": "候选乙", "source_files": [], "test_files": []},
    ])
    monkeypatch.setattr(gt, "build_task", flaky_build)
    monkeypatch.setattr(gt, "validate_task", flaky_validate)
    monkeypatch.setattr(sys, "argv", ["golden_tasks", "--repo", str(repo), "--validate"])

    gt.main()                                  # ← 不许抛

    out = capsys.readouterr().out
    assert "闸门自身异常" in out               # 坏的那条有自己的理由，不是"任务不合格"
    assert "候选 2 → 闸门后有效" in out
    assert real_task is not None


def test_validate_tasks_streams_instead_of_batching(tmp_path, fixture_repo, monkeypatch):
    """闸门批跑必须**边跑边产出**，不能先跑完整批再一次性返回。

    为什么钉这条：`run_eval` 是在这个循环里逐条打印进度的，而原来的实现是列表推导
    （`[validate(t, repo_dir) for t in tasks]`）—— 39 个候选、每个要解两次包跑两次
    pytest，于是闸门的**十几分钟里日志一个字节都没有**，看不出是在跑还是挂了。
    这正是本项目记录在案的头号缺陷类（静默），而 `golden_tasks.py` 的注释当时还写着
    "CLI（要逐条流式打印进度）" —— 机制在、注释在、接线断了。

    判据不靠"看日志有没有输出"（那要靠跑真闸门、几分钟起），而是直接测**惰性**：
    取走第一个结果之后，`validate` 只该被调用过一次。
    """
    import eval.golden_tasks as gt

    repo, _base, _fix = fixture_repo
    calls: list[str] = []

    def counting_validate(task, repo_dir):
        calls.append(task.id)
        return TaskValidity(True, base_rc=1, fix_rc=0)

    monkeypatch.setattr(gt, "validate", counting_validate)
    tasks = [build_task(repo, discover_fix_commits(repo)[0]) for _ in range(3)]

    it = gt.validate_tasks(tasks, repo)
    first_task, first_v = next(it)

    assert first_v.valid and first_task.id == tasks[0].id
    assert calls == [tasks[0].id], (
        f"取走第一个结果时 validate 已被调用 {len(calls)} 次 —— "
        "validate_tasks 又变回一次性跑完整批了，闸门期间日志会是空的"
    )

    assert [t.id for t, _ in it] == [tasks[1].id, tasks[2].id]
    assert calls == [t.id for t in tasks]


# ---------- B2 / P4：判定必须真的跑过测试 ----------
# 这几条测的是 eval/golden_tasks.py 这一层（判据与解析），
# 恢复机制（P1）那一层在 tests/test_eval_runner.py 的同一节里。


def test_judged_passed_never_reads_cannot_parse_as_zero():
    """⚠️ 这一格是整条修复里最容易做错的地方。

    `passed_count is None` 是"没数出来"，**不是**"一个都没通过"。把 `None` 读成 0
    会把一次**真实通过**翻成失败 —— 修 bug 的动作本身制造一个新的假阴性。
    留档的 `45a4d4b3` 正是这一格（见下一条）。
    """
    from eval.golden_tasks import _judged_passed

    assert _judged_passed(0, None) is True    # 数不出来 → 保持通过（不许当 0）
    assert _judged_passed(0, 0) is False      # 数出来了、确实零通过（全 skip 那个洞）
    assert _judged_passed(0, 3) is True
    assert _judged_passed(1, 0) is False
    assert _judged_passed(5, None) is False   # 没收集到用例：本来就不是通过


def test_parse_passed_count_anchors_on_the_tally_line():
    """统计行必须按 pytest 的固定格式认（以 `in <秒>s` 收尾），不能"找第一个 N passed"。

    后半段是真实的失败模式而不是假想：`_run_pytest` 把 stderr 拼在 stdout 后面，
    而 pytest 在**统计行之后**还会向 stderr 打 unraisable traceback
    （留档 `45a4d4b3` 就是这么来的）。那里面出现的 `2 passed` 是**异常消息的正文**。
    """
    from eval.golden_tasks import _parse_passed_count

    assert _parse_passed_count("3 passed, 1 warning in 0.12s\n") == 3
    assert _parse_passed_count("2 failed, 3 passed in 0.50s\n") == 3
    assert _parse_passed_count("===== 4 passed in 0.2s =====\n") == 4
    assert _parse_passed_count("4 skipped in 0.10s\n") == 0     # 统计行在、一个都没通过
    assert _parse_passed_count("no tests ran in 0.01s\n") == 0
    assert _parse_passed_count("ImportError: cannot import name 'x'\n") is None

    # 统计行之后的异常正文里再出现一次 → 不能顶掉统计行
    sneaky = (
        "1 passed in 0.06s\n"
        "Traceback (most recent call last):\n"
        '  File "conftest.py", line 5, in __del__\n'
        "    raise ValueError('2 passed')\n"
    )
    assert _parse_passed_count(sneaky) == 1


def test_tally_line_is_recognised_after_a_minute():
    """跑满 60 秒之后统计行长这样：`1 passed in 65.32s (0:01:05)`。

    这一格是**实测**来的，不是手抄：pytest 8.4.1 的
    `_pytest/terminal.py:format_session_duration()` 在 `seconds >= 60` 时返回
    `f"{seconds:.2f}s ({dt})"` —— 时长后面**还有 `(H:MM:SS)` 一串**。
    少了这一档，任何跑满一分钟的判定的 `passed_count` 都会是 `None`，
    P4 在那类任务上**静默失效**（不制造假阴性，但那道闸门等于没装）。

    所以这里直接调 pytest 自己的格式化函数取真串。pytest 哪天改了格式，
    第一行断言会红 —— 那正是该回来重对实现的时候。
    """
    from _pytest.terminal import format_session_duration

    from eval.golden_tasks import _parse_passed_count

    dur = format_session_duration(65.32)
    assert dur == "65.32s (0:01:05)"
    assert _parse_passed_count(f"===== 1 passed in {dur} =====\n") == 1
    assert _parse_passed_count(f"2 passed in {dur}\n") == 2
    assert _parse_passed_count(f"3 skipped in {dur}\n") == 0


def test_ensure_repo_refuses_to_guess_a_url_for_an_unregistered_repo(tmp_path):
    """换仓时**不许猜 URL** —— 猜错会安静地克隆出一个不相干的仓。

    这一格的分量在于失效模式：猜错了不会报错，闸门照样能跑出"有效任务"来，
    于是第二轮评估整个建在**错的仓库**上而没人发现。所以未登记的仓名必须响亮地报错，
    并且把已登记的仓列出来（人要能当场看出正确写法）。
    """
    from eval.golden_tasks import REPOS, ensure_repo

    with pytest.raises(ValueError) as ei:
        ensure_repo(tmp_path / "__nope__")
    msg = str(ei.value)
    assert "__nope__" in msg
    assert all(name in msg for name in REPOS)   # 报错要能自解释


def test_ensure_repo_is_idempotent_and_the_default_is_registered():
    """默认仓必须在登记表里（否则 `--clone` 默认就不可用）；已存在的仓直接返回、不联网。"""
    from eval.golden_tasks import DEFAULT_REPO, REPOS, ensure_repo

    assert DEFAULT_REPO.name in REPOS
    # 已克隆的仓：不联网、原样返回同一个路径（`--clone` 因此可以幂等地重复跑）
    assert ensure_repo(DEFAULT_REPO) == DEFAULT_REPO


def test_judge_pins_pytest_to_the_workspace(tmp_path):
    """判定期跑 pytest 必须**钉在工作区**里（`--rootdir` / `--confcutdir`）。

    不钉的后果是实测出来的（`evalverify/probe_pytest_flags.py`）：目标仓不自带 inifile 时，
    rootdir 会爬回本仓根，于是**评测器自己的 `conftest.py` 被当插件加载**，
    而且**本仓根目录进了 `sys.path`**（隐藏测试可以 `import eval.*`）。
    工作区建在本仓的 `data/eval/ws/<任务>/` 下，祖先链里就是有本仓。

    这条只钉 argv（真的钉住行为要造出"副本落在本仓之下"的几何，那是探针的活）——
    argv 是这层实现的全部，值指向 `resolve()` 之后的绝对路径。
    """
    from eval.golden_tasks import _pytest_argv

    ws = tmp_path / "ws"
    ws.mkdir()
    argv = _pytest_argv(ws, ["tests/test_x.py"])

    assert f"--rootdir={ws.resolve()}" in argv
    assert f"--confcutdir={ws.resolve()}" in argv
    assert argv[:2] == [sys.executable, "-m"]
    assert "tests/test_x.py" in argv
    assert argv[argv.index("-o") + 1] == "addopts="   # 目标仓的 addopts 必须被清掉


# ---------- 非 .py 的测试数据文件不许当 pytest 节点 ----------
#
# 实测来源：sqlparse 的 143 个候选里有 5 个的 fix 提交同时改了 `tests/files/*.sql`
# （新增一个 SQL 夹具当用例输入）。把这些路径当节点交给 pytest，它报
# `not found: .../x.sql (no match in any of [<Dir files>])` 并以**退出码 4** 收场 ——
# 用例一次都没跑，而闸门把它记成"判定无效"，于是一个有效任务凭空消失。
# tinydb 的 `tests/` 全是 `.py`，所以这个形状在第一个仓上没出现过。


def test_pytest_argv_drops_non_python_test_data_files(tmp_path):
    """`.sql` 夹具不能进 argv，`.py` 一个都不能少（顺序也保持）。"""
    from eval.golden_tasks import _pytest_argv, _pytest_targets

    ws = tmp_path / "ws"
    ws.mkdir()
    files = ["tests/files/casewhen_procedure.sql", "tests/test_split.py",
             "tests/files/function_psql4.sql"]
    argv = _pytest_argv(ws, files)

    assert "tests/test_split.py" in argv
    assert not any(a.endswith(".sql") for a in argv), "非 .py 的夹具被当成 pytest 节点了"
    # 过滤只删不加，且保持原顺序 —— 否则"跑了哪几个测试文件"就不可比对
    assert _pytest_targets(files) == ["tests/test_split.py"]


def test_judge_refuses_a_target_set_with_no_python_file(tmp_path):
    """只有非 `.py` 时**不许**去跑 pytest：没有节点参数 = 收集整个套件 = 白送通过。

    判据落在"到底跑没跑"上：这里传一个**不存在的工作区**，只要它没去起子进程，
    就会安静地返回一个"无效"结论，而不是抛 FileNotFoundError。
    """
    from eval.golden_tasks import _NO_TESTS_RC, _run_pytest

    missing = tmp_path / "not-created-workspace"
    res = _run_pytest(missing, ["tests/files/a.sql"])

    assert res.returncode == _NO_TESTS_RC
    assert res.passed is False
    assert res.passed_count == 0
    assert not missing.exists(), "判定器不该为了这次调用去建工作区（说明它真的起了子进程）"


def test_run_pytest_refuses_a_session_where_nothing_actually_ran(tmp_path):
    """**全 skip 时 pytest 退出码是 0** —— 这条断言就是那个洞本身。

    这是留档 188 字节 PoC 的翻盘原理：conftest 注入一个钩子把用例全标 skip，
    退出码 0 → 旧判据判"修好了"。所以这里不放 conftest（那是名单要管的事），
    只钉住最底下那一层：**退出码 0 不足以证明任何事**。
    """
    ws = tmp_path / "ws"
    (ws / "tests").mkdir(parents=True)
    (ws / "tests" / "test_all_skipped.py").write_text(
        "import pytest\n"
        "\n"
        "pytestmark = pytest.mark.skip(reason='全部跳过')\n"
        "\n"
        "def test_would_fail_if_it_ran():\n"
        "    assert False\n",
        encoding="utf-8",
    )
    r = _run_pytest(ws, ["tests/test_all_skipped.py"])
    assert r.returncode == 0          # ← 退出码是 0，这正是那个洞
    assert r.passed_count == 0        # 但一个用例都没通过
    assert r.passed is False          # → 不许判通过
    assert r.executed is True         # 测试确实跑了 → 这是"失败"，不是"判定无效"
    assert r.error is None            # 所以进分母，不冤枉 agent


def test_run_pytest_counts_a_real_pass_whose_tally_line_is_outside_the_summary(tmp_path):
    """留档 `45a4d4b3` 的形状：**统计行落在最后 6 行之外**。

    那次 `passed=True`，而落盘的 `judge_summary`（= 输出的最后 6 行）被
    `PytestUnraisableExceptionWarning` 的 traceback 整段占满，搜不到任何 `N passed`。
    这里是它的**真实复现**（session 级 fixture 在收尾时漏一个 `__del__` 会抛的对象，
    GC 发生在 pytest 打完统计行之后，traceback 走 stderr、被拼到 `out` 末尾）——
    不是手写的假输出。

    它钉的是"**必须从完整 `out` 解析，不能从 `summary` 解析**"：拿 `summary` 当数据源，
    再加上"数不出就当 0"，会把这一格真实通过翻成失败。
    """
    ws = tmp_path / "ws"
    (ws / "tests").mkdir(parents=True)
    (ws / "conftest.py").write_text(
        "import pytest\n"
        "\n"
        "class _Boom:\n"
        "    def __del__(self):\n"
        "        raise ValueError('I/O operation on closed file.')\n"
        "\n"
        "@pytest.fixture(scope='session', autouse=True)\n"
        "def _leak():\n"
        "    yield\n"
        "    b = _Boom()\n"
        "    b.cycle = b          # 活到解释器退出，GC 落在统计行之后\n",
        encoding="utf-8",
    )
    (ws / "tests" / "test_ok.py").write_text(
        "def test_ok():\n    assert True\n", encoding="utf-8"
    )
    r = _run_pytest(ws, ["tests/test_ok.py"])

    assert r.returncode == 0
    assert " passed in " not in r.summary     # 6 行摘要里就是没有统计行
    assert r.passed_count == 1                # 但完整输出里数得出来
    assert r.passed is True                   # → 真实通过不许被翻成失败


# ---------- B2 / P1：判定前把判定相关文件恢复回去 ----------

def test_capture_and_restore_judge_sensitive_files(tmp_path):
    """抓 → 篡改 → 还。三种改动（改内容 / 新增 / 删掉）都要能还原。"""
    from eval.runner import _capture_judge_sensitive, _restore_judge_sensitive

    ws = tmp_path
    (ws / "conftest.py").write_text("original\n", encoding="utf-8")
    (ws / "tests").mkdir()
    (ws / "tests" / "__init__.py").write_text("", encoding="utf-8")
    (ws / "tinydb").mkdir()
    (ws / "tinydb" / "__init__.py").write_text("VERSION = '1'\n", encoding="utf-8")

    cap = _capture_judge_sensitive(ws)
    # `tests/__init__.py` 靠**相对路径**那一档才抓得到（它按名字叫 __init__.py）；
    # `tinydb/__init__.py` 是合法源码，**绝不能**进这一档 —— 见下一条测试。
    assert set(cap) == {"conftest.py", "tests/__init__.py"}

    (ws / "conftest.py").write_text("tampered\n", encoding="utf-8")
    (ws / "tests" / "__init__.py").write_text("import os; os._exit(0)\n", encoding="utf-8")
    (ws / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")

    changed = ["conftest.py", "pytest.ini", "tests/__init__.py"]
    restored, failed = _restore_judge_sensitive(ws, cap, changed)

    assert failed == []
    assert restored == changed
    assert (ws / "conftest.py").read_text(encoding="utf-8") == "original\n"
    assert (ws / "tests" / "__init__.py").read_text(encoding="utf-8") == ""
    assert not (ws / "pytest.ini").exists()          # 新增的 → 删掉


def test_package_init_is_not_treated_as_judge_sensitive(tmp_path):
    """`tinydb/__init__.py` 是**合法源码**，不是判定相关文件。

    这一条防的是一个具体会犯的错：把 `"__init__.py"` 加进按名字匹配的名单
    （为了抓 `tests/__init__.py`）—— 那样 P1 的恢复会把 agent 对 `tinydb/__init__.py`
    的**真实修复还原回去**，直接制造假阴性。所以那一档只能用相对路径。
    """
    from eval.golden_tasks import _is_judge_sensitive
    from eval.runner import _capture_judge_sensitive, _restore_judge_sensitive

    assert _is_judge_sensitive("tests/__init__.py") is True
    assert _is_judge_sensitive("tinydb/__init__.py") is False
    assert _is_judge_sensitive("tinydb/storages/__init__.py") is False

    (tmp_path / "tinydb").mkdir()
    init = tmp_path / "tinydb" / "__init__.py"
    init.write_text("VERSION = '3.15'\n", encoding="utf-8")
    cap = _capture_judge_sensitive(tmp_path)
    init.write_text("VERSION = '3.15'\n__all__ = ['a']\n", encoding="utf-8")  # agent 的修复

    restored, failed = _restore_judge_sensitive(tmp_path, cap, [])
    assert (restored, failed) == ([], [])
    assert init.read_text(encoding="utf-8").endswith("__all__ = ['a']\n")  # 修复还在


def test_restore_reports_failures_instead_of_raising(tmp_path):
    """恢复失败必须**能被读出来**（调用方要据此作废这次判定），不是抛异常糊过去。"""
    from eval.runner import _restore_judge_sensitive

    (tmp_path / "conftest.py").mkdir()   # 目标是个目录 → write_bytes 抛 OSError
    restored, failed = _restore_judge_sensitive(
        tmp_path, {"conftest.py": b"x"}, ["conftest.py"]
    )
    assert restored == []
    assert failed == ["conftest.py"]


def test_gate_rejects_a_task_whose_gold_patch_touches_judge_sensitive_files(
    tmp_path, fixture_repo
):
    """P1 唯一的真风险面：金标准自己改了判定相关文件、而它不在隐藏测试里。

    那种任务在恢复之后连金标准都跑不过 —— 一个诚实的通过会被判成失败。
    所以闸门要在**花一分钱之前**把它拒掉（纯元数据，不跑 pytest）。
    """
    from eval.golden_tasks import judge_sensitive_blind_spots, validate_task

    repo, base, fix = fixture_repo
    hidden = {"tests/test_app.py": "def test_add():\n    assert True\n"}

    risky = GoldenTask(
        id="risky", base_sha=base, fix_sha=fix, title="t", task_text="t",
        changed_sources=["conftest.py", "src/app.py"],   # 根 conftest.py 不在 tests/ 下
        hidden_tests=hidden,
    )
    assert judge_sensitive_blind_spots(risky) == ["conftest.py"]
    v = validate_task(risky, repo)
    assert v.valid is False
    assert "判定相关文件" in v.reason

    # 隐藏测试**覆盖到**的判定相关文件不算风险：judge 本来就会把它覆盖写回
    covered = GoldenTask(
        id="covered", base_sha=base, fix_sha=fix, title="t", task_text="t",
        changed_sources=["src/app.py"],
        hidden_tests={"tests/conftest.py": "x", "tests/test_app.py": "y"},
    )
    assert judge_sensitive_blind_spots(covered) == []

    # 合法源码（tinydb/__init__.py）永远不在这份名单里 —— 否则会制造假阴性
    ordinary = GoldenTask(
        id="ordinary", base_sha=base, fix_sha=fix, title="t", task_text="t",
        changed_sources=["tinydb/__init__.py"], hidden_tests=hidden,
    )
    assert judge_sensitive_blind_spots(ordinary) == []
