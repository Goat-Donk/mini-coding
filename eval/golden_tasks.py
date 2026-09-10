"""黄金任务集（M5-1，SWE-bench 思路：真实仓库 + 真实 bug + 隐藏测试判定）。

从 tinydb（小型纯 Python 数据库，bug 密集、仓库小）的 git history 构造"修 bug"任务：

- `discover_fix_commits`：git log 找 commit message 含 fix/bug/… 关键字、且同时改了
  源码与 tests/ 的提交（与 SWE-bench 的 FAIL_TO_PASS 测试同源）。
- `GoldenTask`：base_sha（bug 存在状态）/ task_text（真实 bug 报告）/
  changed_sources（提示）/ hidden_tests（fix commit 时的测试文件内容，**判定用隐藏测试**）。
- `materialize`：把 base_sha 用 `git worktree add` 检出到干净工作区，agent 在这里修 bug。
- `judge`：把 hidden tests（fix 版测试文件）写回工作区覆盖后跑 pytest 判定——
  agent 全程看不到 hidden tests，**判据不造假**。

真实 tinydb 克隆/发现通过 `python -m eval.golden_tasks --clone` 手动触发（网络）；
单元测试用 tmp_path 造的本地迷你仓库离线验证（tests/test_golden_tasks.py）。
"""
from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

TINYDB_REPO = "https://github.com/msiemens/tinydb.git"
DEFAULT_REPO = Path("eval/repos/tinydb")

FIX_KEYWORDS = (
    "fix", "fixes", "fixed", "bug", "error", "crash", "issue",
    "regression", "broken", "incorrect",
)


def git(repo_dir: Path, *args: str) -> str:
    """在 repo 里跑 git，返回 stdout（去首尾空白）。失败抛 CalledProcessError。"""
    return subprocess.check_output(
        ["git", "-C", str(repo_dir), *args],
        text=True, encoding="utf-8", errors="replace",
    ).strip()


# ---------- 克隆 ----------

def ensure_repo(repo_dir: Path = DEFAULT_REPO, *, clone_url: str = TINYDB_REPO) -> Path:
    """克隆 tinydb（幂等 + 网络重试）。返回 repo 根。"""
    repo_dir = Path(repo_dir)
    if (repo_dir / ".git").exists():
        return repo_dir
    repo_dir.parent.mkdir(parents=True, exist_ok=True)
    proc: subprocess.CompletedProcess | None = None
    for attempt in range(3):
        proc = subprocess.run(
            ["git", "clone", "--quiet", clone_url, str(repo_dir)],
            capture_output=True, text=True,
        )
        if proc.returncode == 0:
            return repo_dir
        # 半成品清理后重试（网络抖动常见）
        import shutil

        shutil.rmtree(repo_dir, ignore_errors=True)
        time.sleep(3)
    raise RuntimeError(f"clone tinydb 失败: {(proc.stderr if proc else '')[:300]}")


# ---------- 发现 fix commits ----------

def discover_fix_commits(
    repo_dir: Path, *, limit: int = 20, keywords: tuple[str, ...] = FIX_KEYWORDS
) -> list[dict]:
    """找出"修 bug"提交：subject 含关键字 且 同时改了源码(.py 非 tests)与 tests/。

    返回按时间倒序的 [{sha, subject, body, source_files, test_files}, ...]。
    """
    repo_dir = Path(repo_dir)
    meta = git(repo_dir, "log", "--no-merges", "--format=%H%x1f%s%x1f%b%x1e")
    candidates: list[tuple[str, str, str]] = []
    for entry in meta.split("\x1e"):
        entry = entry.strip()
        if not entry:
            continue
        parts = entry.split("\x1f", 2)
        sha, subject = parts[0], parts[1]
        body = parts[2] if len(parts) > 2 else ""
        if any(k in subject.lower() for k in keywords):
            candidates.append((sha, subject, body))

    found: list[dict] = []
    for sha, subject, body in candidates:
        files = [
            f for f in git(repo_dir, "show", "--name-only", "--format=", sha).splitlines()
            if f.strip()
        ]
        source = [f for f in files if not f.startswith("tests/") and f.endswith(".py")]
        tests = [f for f in files if f.startswith("tests/")]
        if not source or not tests:
            continue  # 没同时改源码+测试 → 不是可判定的 bug fix
        found.append({
            "sha": sha, "subject": subject, "body": body,
            "source_files": source, "test_files": tests,
        })
        if len(found) >= limit:
            break
    return found


# ---------- 任务构造 ----------

@dataclass
class GoldenTask:
    id: str
    base_sha: str                    # bug 存在状态的提交
    title: str
    task_text: str
    changed_sources: list[str]       # 提示用（修复涉及的文件）
    hidden_tests: dict[str, str]     # test 路径 -> fix commit 时的内容（判定用，不给 agent）

    @property
    def test_files(self) -> list[str]:
        return list(self.hidden_tests)


def render_task_text(commit: dict) -> str:
    """真实 bug 报告（commit subject+body），不虚构任何内容。"""
    report = commit["subject"]
    if (commit.get("body") or "").strip():
        report += "\n\n" + commit["body"].strip()
    return f"""以下是 tinydb 仓库中一个真实 bug 的报告：

{report}

请定位并修复这个 bug。要求：
1. 只修改源码文件（src/ 下），不要修改 tests/ 下的测试文件；
2. 修复后用 `python -m pytest tests/ -q` 验证原有测试仍然通过；
3. 完成时输出：修复了什么、改了哪些文件、测试结果。"""


def build_task(repo_dir: Path, commit: dict) -> GoldenTask:
    """从 fix commit 构造任务：base = 父提交（bug 存在），hidden_tests = fix 版测试内容。"""
    repo_dir = Path(repo_dir)
    sha = commit["sha"]
    base_sha = git(repo_dir, "rev-parse", f"{sha}^")
    hidden_tests: dict[str, str] = {}
    for test_file in commit["test_files"]:
        try:
            hidden_tests[test_file] = git(repo_dir, "show", f"{sha}:{test_file}")
        except subprocess.CalledProcessError:
            continue  # fix 提交里测试被改名/删除 → 跳过该文件
    return GoldenTask(
        id=sha[:8],
        base_sha=base_sha,
        title=commit["subject"],
        task_text=render_task_text(commit),
        changed_sources=list(commit["source_files"]),
        hidden_tests=hidden_tests,
    )


# ---------- 工作区物化 + 判定 ----------

def materialize(task: GoldenTask, target_dir: Path, repo_dir: Path) -> Path:
    """把 task.base_sha 检出到干净工作区（git worktree）。返回工作区路径。"""
    target = Path(target_dir).resolve()
    git(repo_dir, "worktree", "add", "--detach", str(target), task.base_sha)
    return target


def remove_worktree(repo_dir: Path, target_dir: Path) -> None:
    """清理 worktree（judge 后调用，release 工作区）。"""
    git(repo_dir, "worktree", "remove", "--force", str(Path(target_dir).resolve()))


# pytest 退出码语义：0=全部通过、1=有用例失败 —— 两者都说明测试真的跑完了；
# 2=被中断、3=内部错误、4=用法错误、5=没收集到用例 —— 这几种是「压根没跑成」，
# 判定无效，绝不能当成「agent 没修好」记入完成率。
_PYTEST_EXECUTED = (0, 1)


@dataclass
class JudgeResult:
    passed: bool
    returncode: int
    summary: str                      # pytest 输出摘要（诚实展示，不造假）

    @property
    def executed(self) -> bool:
        """测试是否真的执行了。False 表示这次判定无效（不是 agent 的锅）。"""
        return self.returncode in _PYTEST_EXECUTED

    @property
    def error(self) -> str | None:
        """判定无效时给出原因，供 runner 记入报告 error 字段。"""
        if self.executed:
            return None
        return f"judge 未能执行测试（pytest 退出码 {self.returncode}）: {self.summary}"


def judge(task: GoldenTask, workspace: Path) -> JudgeResult:
    """把 hidden tests 覆盖写回 → 跑 pytest 判定。

    agent 全程看不到 hidden tests（物化时工作区只有 base 版测试）；
    用 fix commit 的测试文件判定修复是否与金标准行为一致。
    """
    workspace = Path(workspace).resolve()
    for rel, content in task.hidden_tests.items():
        p = workspace / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")

    proc = subprocess.run(
        # -o addopts= 清掉目标仓库 pytest.ini 自带的 addopts：tinydb 写死了
        # `--cov-append --cov-report --cov tinydb`，本机没装 pytest-cov 时 pytest 会
        # 以 usage error（退出码 4）直接退出——测试一次都没跑，却会被误判成「没修好」。
        ["python", "-m", "pytest", *task.test_files, "-q", "--no-header", "-o", "addopts="],
        cwd=workspace, capture_output=True, text=True,
    )
    out = (proc.stdout or "") + (proc.stderr or "")
    lines = [ln for ln in out.splitlines() if ln.strip()]
    summary = "\n".join(lines[-6:])
    return JudgeResult(passed=proc.returncode == 0, returncode=proc.returncode, summary=summary)


# ---------- CLI（手动跑真实 tinydb） ----------

def main() -> None:
    import argparse

    ap = argparse.ArgumentParser(description="发现 tinydb 黄金修 bug 任务")
    ap.add_argument("--repo", type=Path, default=DEFAULT_REPO, help="tinydb 仓库路径")
    ap.add_argument("--limit", type=int, default=20, help="最多返回几个 fix commit")
    ap.add_argument("--clone", action="store_true", help="先克隆 tinydb（需要网络）")
    args = ap.parse_args()

    repo = ensure_repo(args.repo) if args.clone else Path(args.repo)
    commits = discover_fix_commits(repo, limit=args.limit)
    print(f"发现 {len(commits)} 个可判定的 fix commit（{repo}）：")
    for c in commits:
        print(f"  {c['sha'][:8]}  {c['subject'][:70]}")
        print(f"        源码 {len(c['source_files'])} 个 / 测试 {len(c['test_files'])} 个")


if __name__ == "__main__":
    main()
