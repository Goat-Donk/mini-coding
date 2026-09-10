"""共享测试夹具：本地迷你 git 仓库（离线构造，eval 相关测试用，不联网）。"""
from __future__ import annotations

from pathlib import Path

import pytest

from eval.golden_tasks import git


def make_fixture_repo(tmp_path: Path) -> tuple[Path, str, str]:
    """造迷你仓库：buggy commit(A) → fix commit(B) → 两个非 fix commit。

    布局：src/app.py（add 故意 +1 的 bug）+ tests/test_app.py。
    返回 (repo, base_sha, fix_sha)。
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    g = lambda *a: git(repo, *a)
    g("init")
    (repo / "src").mkdir()
    (repo / "tests").mkdir()

    buggy_src = "def add(a, b):\n    return a + b + 1  # 故意 bug\n"
    fixed_src = "def add(a, b):\n    return a + b\n"
    test_head = (
        "import sys, os\n"
        "sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))\n"
        "from app import add\n"
    )
    base_test = test_head + "\ndef test_add_base():\n    assert add(1, 2) == 4\n"
    fixed_test = test_head + "\ndef test_add():\n    assert add(1, 2) == 3\n"

    (repo / "src" / "app.py").write_text(buggy_src, encoding="utf-8")
    (repo / "tests" / "test_app.py").write_text(base_test, encoding="utf-8")
    g("add", ".")
    g("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-m", "feat: add adder")
    base_sha = g("rev-parse", "HEAD")

    (repo / "src" / "app.py").write_text(fixed_src, encoding="utf-8")
    (repo / "tests" / "test_app.py").write_text(fixed_test, encoding="utf-8")
    g("add", ".")
    g("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-m", "fix: correct adder result")
    fix_sha = g("rev-parse", "HEAD")

    # 非 fix 提交：只改源码（无 tests）→ 排除
    (repo / "src" / "app.py").write_text(fixed_src + "\n# refactor note\n", encoding="utf-8")
    g("add", ".")
    g("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-m", "refactor: add comment")
    # 只改测试（无源码）→ 排除
    (repo / "tests" / "test_app.py").write_text(
        fixed_test + "\ndef test_extra():\n    assert add(2, 3) == 5\n", encoding="utf-8"
    )
    g("add", ".")
    g("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-m", "docs: expand tests")
    return repo, base_sha, fix_sha


@pytest.fixture
def fixture_repo(tmp_path):
    """返回 (repo, base_sha, fix_sha)。"""
    return make_fixture_repo(tmp_path)
