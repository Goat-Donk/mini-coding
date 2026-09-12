"""黄金任务集（M5-1，SWE-bench 思路：真实仓库 + 真实 bug + 隐藏测试判定）。

从 tinydb（小型纯 Python 数据库，bug 密集、仓库小）的 git history 构造"修 bug"任务：

- `discover_fix_commits`：git log 找 commit message 含 fix/bug/… 关键字、且同时改了
  源码与 tests/ 的提交。
- `GoldenTask`：base_sha（bug 存在状态）/ fix_sha（金标准）/ task_text（真实 bug 报告）/
  hidden_tests（fix commit 时的测试文件内容，**判定用隐藏测试**）。
- `materialize`：把 base_sha **物理剥离**成隔离工作区（见下）。
- `validate_task`：SWE-bench 式的有效性闸门——隐藏测试必须在 base 失败、在 fix 通过。
- `judge`：把 hidden tests（fix 版测试文件）写回工作区覆盖后跑 pytest 判定——
  agent 全程看不到 hidden tests，**判据不造假**。

**为什么不能用 `git worktree`**：worktree 与主仓库**共享对象库与 refs**，而 fix 提交就在
主仓库的历史里。agent 在自己的工作区里一条 `git show <fix_sha>:tests/test_xxx.py` 就能
拿到隐藏测试全文、`git show <fix_sha>:tinydb/table.py` 就是金标准补丁 —— 而 bash 工具
只校验 cwd、不校验命令文本。所以物化改成 `git archive` 导出 tree + 在隔离区新建一个
**只有 base 一个提交**的仓库：agent 仍有 git 可用（能 `git diff` 自己的改动），但没有未来。

真实 tinydb 克隆/发现通过 `python -m eval.golden_tasks --clone` 手动触发（网络）；
单元测试用 tmp_path 造的本地迷你仓库离线验证（tests/test_golden_tasks.py）。
"""
from __future__ import annotations

import io
import os
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

TINYDB_REPO = "https://github.com/msiemens/tinydb.git"
DEFAULT_REPO = Path("eval/repos/tinydb")

FIX_KEYWORDS = (
    "fix", "fixes", "fixed", "bug", "error", "crash", "issue",
    "regression", "broken", "incorrect",
)

# 这些文件名会出现在 fix 提交的改动里，但它们不是"被测的源码"：
# setup.py 是打包配置，conftest.py 是测试夹具。把它们算成源码会让
# "改了源码 + 改了测试"这个筛选条件失真。
_NON_SOURCE_NAMES = ("setup.py", "conftest.py")

# 物化后在隔离区里新建的那个初始提交的 message。它**刻意不冒充**原仓库历史：
# 评测工作区的 git 历史是评测生成的，与原仓库无关。
EVAL_COMMIT_MESSAGE = "Base commit for evaluation"


def git(repo_dir: Path, *args: str) -> str:
    """在 repo 里跑 git，返回 stdout（去首尾空白）。失败抛 CalledProcessError。"""
    return subprocess.check_output(
        ["git", "-C", str(repo_dir), *args],
        text=True, encoding="utf-8", errors="replace",
    ).strip()


# ---------- 删除（含只读位） ----------

def _force_rmtree(path: Path) -> None:
    """递归删除，并清掉只读位。

    必要：git 在 Windows 上把 pack 文件标成只读（`-r--r--r--`），`shutil.rmtree`
    撞上会抛 `PermissionError`。全新 `git init` 写的是 loose object（可写），所以
    平时没事 —— 但 agent 只要在自己的工作区里跑一次 `git gc` / `git repack`，
    下一轮清理就会炸。

    **不用 `ignore_errors=True`**：那只是把 `PermissionError` 吞掉，文件留在原地，
    下一轮物化又要面对一个非空目录。
    """
    def _chmod_then_retry(func, p, _exc) -> None:
        # onexc 传异常实例、onerror 传 exc_info 三元组；两者都不用，签名兼容。
        try:
            os.chmod(p, stat.S_IWRITE)
            func(p)
        except OSError:
            pass

    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=_chmod_then_retry)
    else:  # 3.11：onexc 还没有（onerror 在 3.12 起 deprecated）
        shutil.rmtree(path, onerror=_chmod_then_retry)


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
        shutil.rmtree(repo_dir, ignore_errors=True)
        time.sleep(3)
    raise RuntimeError(f"clone tinydb 失败: {(proc.stderr if proc else '')[:300]}")


# ---------- 发现 fix commits ----------

def discover_fix_commits(
    repo_dir: Path, *, limit: int = 20, keywords: tuple[str, ...] = FIX_KEYWORDS
) -> list[dict]:
    """找出"修 bug"提交：subject 含关键字 且 同时改了源码(.py 非 tests)与 tests/。

    返回按时间倒序的 [{sha, subject, body, source_files, test_files}, ...]。

    关键字只负责**缩小候选集**，不负责判定谁是"真 bug 修复" —— 那由
    `validate_task`（隐藏测试必须在 base 失败）用事实说了算。所以
    `chore: fix a lot of typos` 这类提交会走进来这里，然后在闸门处被拒。
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
        source = [
            f for f in files
            if not f.startswith("tests/") and f.endswith(".py")
            and Path(f).name not in _NON_SOURCE_NAMES
        ]
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
    fix_sha: str                     # 金标准修复提交（闸门与 oracle 基线用）
    title: str
    task_text: str
    changed_sources: list[str]       # 金标准改过的源码文件（元数据；当前不注入 prompt）
    hidden_tests: dict[str, str]     # test 路径 -> fix commit 时的内容（判定用，不给 agent）

    @property
    def test_files(self) -> list[str]:
        return list(self.hidden_tests)


def render_task_text(commit: dict) -> str:
    """真实 bug 报告（commit subject+body），不虚构任何内容。

    ⚠️ 这里**不点任何目录名**。原实现写的是「只修改源码文件（src/ 下）」，
    而 tinydb 根本没有 `src/`（包目录是 `tinydb/`，最老三个任务的 base 树里
    连 `tinydb/` 都没有、模块直接在仓库根）—— 对全部 39 个任务都是错的。
    单测测不出来，因为夹具恰好就用 `src/app.py`，而这句话当时不在任何断言里。
    """
    report = commit["subject"]
    if (commit.get("body") or "").strip():
        report += "\n\n" + commit["body"].strip()
    return f"""以下是 tinydb 仓库中一个真实 bug 的报告：

{report}

请定位并修复这个 bug。要求：
1. 只修改源码文件，不要修改 tests/ 下的测试文件；
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
        fix_sha=sha,
        title=commit["subject"],
        task_text=render_task_text(commit),
        changed_sources=list(commit["source_files"]),
        hidden_tests=hidden_tests,
    )


# ---------- 工作区物化（物理剥离）+ 判定 ----------

def _archive_to(sha: str, repo_dir: Path, target: Path) -> None:
    """把 `sha` 的 tree 导出到 target。

    **归档本身绝不落进工作区**：git 的 tar 流以 `pax_global_header` 开头，里面写着
    `comment=<被归档的 sha>` —— 先存成文件再解包就等于在工作区里留一个含 sha 的
    明文二进制。所以整条流读进内存（`BytesIO`）再解包，磁盘上不留任何中间产物。

    **必须用可 seek 的 fileobj（`io.BytesIO` + `mode="r:"`），不能用流模式 `r|`**：
    归档里有 symlink 条目（git 存成 `120000` blob）时，tarfile 要回到归档开头重读
    才能解析它，而流模式不允许 seek —— 直接
    `StreamError: seeking backwards is not allowed`。实测：全量闸门里 **7 个候选**
    因为这条被记成"闸门自身异常"，一个都没验成（`gate_all_buggy_archive.log`）。
    改用 `communicate()` 还顺带**消掉了**上一版手写"排空 pipe 再 close"要防的那个
    SIGPIPE 坑（退出码 141）：这里压根不会提前关管道。

    `filter="data"` 是防 CVE-2007-4559（路径穿越）的那个过滤器；不传会触发
    DeprecationWarning（Python 3.14 起默认过滤）。老版本没有这个参数 → 回退。
    """
    target = Path(target)
    target.mkdir(parents=True, exist_ok=True)
    try:
        # 先问一句"这个提交在不在"：仓库被重新克隆过时，报告里记的 sha 会失效，
        # 这时候要给的是一句人话，不是 tarfile 的 "empty file"。
        git(repo_dir, "cat-file", "-e", f"{sha}^{{commit}}")
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(
            f"提交 {sha[:12]} 不在 {repo_dir} 里（仓库被重新克隆过？报告里的 sha 可能已失效）"
        ) from exc

    proc = subprocess.Popen(
        ["git", "-C", str(repo_dir), "archive", "--format=tar", sha],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    # communicate 会同时收干两个管道再 wait：既不会截断 git 的 stdout（上一版
    # close 早了 → EPIPE → 退出码 141），也不会因为 stderr 写满而与 git 互锁。
    out, err = proc.communicate()
    if proc.returncode != 0:
        detail = (err or b"").decode("utf-8", "replace").strip()[:300]
        raise RuntimeError(
            f"git archive {sha[:12]} 失败（退出码 {proc.returncode}）: {detail}"
        )

    try:
        with tarfile.open(fileobj=io.BytesIO(out), mode="r:") as tf:
            try:
                tf.extractall(target, filter="data")
            except TypeError:  # Python < 3.11.4：extractall 还没有 filter 参数
                tf.extractall(target)
            # 静默丢失是本项目头号缺陷类，别让它在物化这一步重现：目标串不是路径的
            # symlink（tinydb 的 `CONTRIBUTING.rst` 就是一条 —— linkname 是一整篇
            # 文档）在 Windows 上建不出来，tarfile 只当非致命错误跳过、一声不吭。
            # 用 lexists（不跟随链接）免得把悬空 symlink 误报成丢失。
            missing = [
                m.name for m in tf.getmembers()
                if not m.isdir() and not os.path.lexists(target / m.name)
            ]
    except tarfile.TarError as exc:
        # git 失败时 stdout 是空的 → tarfile 先炸出 "empty file"，而真正的原因
        # （比如权限、磁盘满、sha 不是 tree-ish）只在 git 的 stderr 里。
        raise RuntimeError(f"解包 git archive {sha[:12]} 的输出失败: {exc}") from exc

    if missing:
        print(f"  （归档里 {len(missing)} 个条目在本机建不出来，已跳过：{missing[:5]}）")


def materialize(task: GoldenTask, target_dir: Path, repo_dir: Path) -> Path:
    """把 task.base_sha **物理剥离**成隔离工作区（不含原仓库历史）。返回工作区路径。

    步骤：`git archive base_sha` 流式解包 → 在隔离区 `git init` + 一个初始提交。
    结果：agent 有可用的 `git diff`，但 `git log --all` 只有一个提交、
    fix 提交的对象**根本不存在** —— 「隐藏测试」不再只靠"没写进工作区"。

    目标已存在时**自愈**（先清空再建）：目标路径是派生的（`ws_root / task.id`）、
    是纯可再生草稿区，上一轮 `--keep` 或崩溃残留都不该让整批任务死掉。
    ⚠️ `--keep` 只保证"本次 run 之后不清理"，**不保证跨 run 保留**。
    """
    target = Path(target_dir).resolve()
    if target.exists():
        print(f"  （目标已存在，先清空：{target}）")
        _force_rmtree(target)

    _archive_to(task.base_sha, repo_dir, target)
    git(target, "init", "-q", "-b", "master")  # 钉住分支名：别的机器默认可能是 main
    git(target, "add", ".")
    git(
        target,
        "-c", "user.name=CodeAgent", "-c", "user.email=eval@localhost",
        "-c", "commit.gpgsign=false",
        "commit", "-q", "-m", EVAL_COMMIT_MESSAGE,  # 必须显式 -m（环境里可能有 GIT_EDITOR）
    )
    return target


def remove_workspace(target_dir: Path) -> None:
    """清理隔离工作区。物理剥离后不再需要 git，也就不需要 repo_dir 参数。"""
    target = Path(target_dir)
    if target.exists():
        _force_rmtree(target)


def leak_probe(task: GoldenTask, workspace: Path) -> bool:
    """金标准在 workspace 里是否可达？True = 泄漏（**不该发生**）。

    用 `cat-file -e`：它问的是"这个对象在不在这个仓库里"，比"可不可达"更硬。
    物理剥离后应当非零退出（对象根本不存在），而 `git worktree` 版本是 0。
    每次跑都测一遍 —— 把"不泄漏"从一句声称变成一条可观测的量。
    """
    proc = subprocess.run(
        ["git", "-C", str(Path(workspace)), "cat-file", "-e", task.fix_sha],
        capture_output=True,
    )
    return proc.returncode == 0


# pytest 退出码语义：0=全部通过、1=有用例失败 —— 两者都说明测试真的跑完了；
# 2=被中断、3=内部错误、4=用法错误、5=没收集到用例 —— 这几种是「压根没跑成」，
# 判定无效，绝不能当成「agent 没修好」记入完成率。
_PYTEST_EXECUTED = (0, 1)

# 没有测试文件可跑时用的合成退出码（对齐 pytest 的 5 = 没收集到用例）。
_NO_TESTS_RC = 5


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


def _run_pytest(workspace: Path, test_files: list[str]) -> JudgeResult:
    """在 workspace 里跑这几个测试文件，返回判定结果。

    `judge` 与 `validate_task` **共用这一处**（两处各写一遍 → 漂移）。

    用 `sys.executable` 而不是字面量 `"python"`：调用方多起来之后，这能保证
    "判定器"与"跑测试的解释器"永远是同一个。

    ⚠️ `test_files` 为空时**不跑**：无路径参数的 pytest 会收集整个测试套件，
    通常全绿 → `passed=True`，那是一条零验证的自由通过路径。
    """
    if not test_files:
        return JudgeResult(
            passed=False, returncode=_NO_TESTS_RC,
            summary="hidden_tests 为空：没有测试文件可判定（拒绝零验证通过）",
        )

    workspace = Path(workspace).resolve()
    proc = subprocess.run(
        # -o addopts= 清掉目标仓库 pytest.ini 自带的 addopts：tinydb 写死了
        # `--cov-append --cov-report --cov tinydb`，本机没装 pytest-cov 时 pytest 会
        # 以 usage error（退出码 4）直接退出——测试一次都没跑，却会被误判成「没修好」。
        [
            sys.executable, "-m", "pytest", *test_files,
            "-q", "--no-header", "-o", "addopts=",
        ],
        cwd=workspace, capture_output=True, text=True,
    )
    out = (proc.stdout or "") + (proc.stderr or "")
    lines = [ln for ln in out.splitlines() if ln.strip()]
    summary = "\n".join(lines[-6:])
    return JudgeResult(passed=proc.returncode == 0, returncode=proc.returncode, summary=summary)


def _write_hidden_tests(workspace: Path, hidden_tests: dict[str, str]) -> None:
    """把 fix 版测试文件写进工作区（覆盖同名文件）。"""
    workspace = Path(workspace)
    for rel, content in hidden_tests.items():
        p = workspace / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")


def judge(task: GoldenTask, workspace: Path) -> JudgeResult:
    """把 hidden tests 覆盖写回 → 跑 pytest 判定。

    agent 全程看不到 hidden tests（物化时工作区只有 base 版测试）；
    用 fix commit 的测试文件判定修复是否与金标准行为一致。
    """
    workspace = Path(workspace).resolve()
    _write_hidden_tests(workspace, task.hidden_tests)
    return _run_pytest(workspace, task.test_files)


# ---------- 有效性闸门 ----------

@dataclass
class TaskValidity:
    valid: bool
    reason: str | None = None
    base_rc: int | None = None
    fix_rc: int | None = None
    base_collect_error: bool = False   # base 侧是靠"收集错误"失败的（见 validate_task）


def _stage_and_run(
    sha: str, repo_dir: Path, target: Path,
    hidden_tests: dict[str, str], test_files: list[str],
) -> JudgeResult:
    """导出 `sha` 的树 → 写回 hidden tests → 跑 pytest。

    **闸门两侧共用这一处**。fix 侧也必须写回 hidden tests，不能"跑 fix 树里
    恰好有的那个测试文件"：金标准正常时那两者恰好等价，但一旦 `fix_sha` 指错
    （仓库重新克隆、sha 抄错），它就会拿一个**不相干的测试**跑绿而放行 —— 闸门
    变成摆设，而且看不出来。两侧动作完全对称，才谈得上"同一把尺子量两次"。
    """
    _archive_to(sha, repo_dir, target)
    _write_hidden_tests(target, hidden_tests)
    return _run_pytest(target, test_files)


def validate_task(task: GoldenTask, repo_dir: Path) -> TaskValidity:
    """SWE-bench 式的有效性闸门：隐藏测试必须在 base **失败**、在 fix **通过**。

    零 LLM 成本，只跑 pytest。两个方向都要查：

    - base 侧不是 fail → 这个任务在 base 上就已经是绿的，agent 什么都不做也能拿分（**白送分**）。
    - fix 侧不是 pass → 金标准补丁在本机跑不过，这个任务谁都拿不到分（**假阴性**）。
      后者同时就是 **oracle 基线**：金标准 = 100%。没有它，一个 0% 的完成率
      和「judge 坏了」在报告上长得一模一样。

    ⚠️ 两侧都跑在 `tempfile.mkdtemp()` 里，**绝不复用 `ws_root / task.id`**：
    `judge` 会把隐藏测试**写进传入的目录**（见 `judge`），复用就等于闸门自己制造泄漏。
    """
    if not task.test_files:
        return TaskValidity(
            False, "fix 提交的测试文件在 fix 提交里已不存在 → 无可判定内容"
        )

    base_dir = Path(tempfile.mkdtemp(prefix=f"evalval-base-{task.id}-"))
    fix_dir = Path(tempfile.mkdtemp(prefix=f"evalval-fix-{task.id}-"))
    try:
        # ---- base 侧：隐藏测试必须失败 ----
        base = _stage_and_run(
            task.base_sha, repo_dir, base_dir, task.hidden_tests, task.test_files
        )

        if base.returncode == 0:
            return TaskValidity(
                False,
                "隐藏测试在 base 就通过 → 这个任务不测任何东西（白送分）",
                base_rc=0,
            )
        if base.returncode == 1:
            collect_error = False          # 跑了、且有用例失败：最强信号
        elif base.returncode == 2:
            # 收集错误（pytest 里 Session.Interrupted 继承 KeyboardInterrupt →
            # ExitCode.INTERRUPTED=2，2026-09-12 实测复核过）。**接受但打标**：
            # fix 提交新增了 base 没有的符号时正是这一支 —— 那是 fix 依赖的强证据，
            # 不是坏任务。敢接受是因为 fix 侧在守：环境真坏了，fix 侧也过不了。
            collect_error = True
        else:
            return TaskValidity(
                False,
                f"base 侧测试压根没跑成（pytest 退出码 {base.returncode}）→ 判定无效",
                base_rc=base.returncode,
            )

        # ---- fix 侧：金标准必须通过 ----
        fix = _stage_and_run(
            task.fix_sha, repo_dir, fix_dir, task.hidden_tests, task.test_files
        )
        if fix.returncode != 0:
            return TaskValidity(
                False,
                f"金标准补丁在本机跑不过（pytest 退出码 {fix.returncode}）"
                f"→ 这个任务谁都拿不到分",
                base_rc=base.returncode, fix_rc=fix.returncode,
            )

        return TaskValidity(
            True, base_rc=base.returncode, fix_rc=fix.returncode,
            base_collect_error=collect_error,
        )
    finally:
        _force_rmtree(base_dir)
        _force_rmtree(fix_dir)


def validate(task: GoldenTask, repo_dir: Path) -> TaskValidity:
    """单个候选过闸门，**自身异常也变成"拒"**。

    唯一的容错实现：`validate_tasks`（批量）与 CLI（要逐条流式打印进度）都走它。
    各写一份迟早会漂移成"批量那边容错、CLI 这边不容错"—— 而 CLI 正是那个要跑
    几十分钟的长任务，它最不能被一个候选带走。

    理由里如实写明是闸门自身的问题，与"任务不合格"分得开。
    """
    try:
        return validate_task(task, repo_dir)
    except Exception as exc:
        return TaskValidity(
            False, f"闸门自身异常（{type(exc).__name__}: {exc}）→ 无法验证，不予采用"
        )


def validate_tasks(
    tasks: list[GoldenTask], repo_dir: Path
) -> Iterator[tuple[GoldenTask, TaskValidity]]:
    """对一批任务逐个跑闸门，**边跑边产出** `(task, 结果)`。

    **必须是生成器，不能是列表推导**：调用方（`runner.run_eval`）是在这个循环里
    逐条打印进度的，而列表推导会先把整批跑完才返回 —— 39 个候选、每个要解两次包跑两次
    pytest，于是闸门的十几分钟里日志**一个字节都没有**。"看不出是在跑还是挂了"
    正是本项目记录在案的头号缺陷类（静默），不能让它出现在闸门这一步。

    **闸门自己异常也算"拒"**，绝不让异常冒出去：一个任务把异常抛出去 = 整批任务中断、
    报告一个都不落盘 —— 这正是 `materialize` 刚修掉的那个洞，闸门不能把它重新引入一遍。
    """
    for t in tasks:
        yield t, validate(t, repo_dir)


# ---------- CLI（手动跑真实 tinydb） ----------

def main() -> None:
    import argparse

    ap = argparse.ArgumentParser(description="发现 tinydb 黄金修 bug 任务")
    ap.add_argument("--repo", type=Path, default=DEFAULT_REPO, help="tinydb 仓库路径")
    ap.add_argument("--limit", type=int, default=20, help="最多返回几个 fix commit")
    ap.add_argument("--clone", action="store_true", help="先克隆 tinydb（需要网络）")
    ap.add_argument(
        "--validate", action="store_true",
        help="对每个候选跑有效性闸门（零 LLM 成本，只跑 pytest）",
    )
    args = ap.parse_args()

    repo = ensure_repo(args.repo) if args.clone else Path(args.repo)
    commits = discover_fix_commits(repo, limit=args.limit)
    print(f"发现 {len(commits)} 个候选 fix commit（{repo}）：")
    for c in commits:
        print(f"  {c['sha'][:8]}  {c['subject'][:70]}")
        print(f"        源码 {len(c['source_files'])} 个 / 测试 {len(c['test_files'])} 个")

    if not args.validate:
        return

    print("\n== 有效性闸门（base 必败 + fix 必过；零 LLM 成本）==")
    # 逐个来、**逐条 try**：一个候选把异常冒出去 = 整批结果一个都看不到
    # （真踩过：第二个候选的 `git archive` 报 141，前面跑完的结果全白跑）。
    # 这与 `run_eval` 里用 `validate_tasks` 是同一条道理，两处口径必须一致。
    tasks: list[GoldenTask] = []
    commits_of: list[dict] = []
    build_failed: list[tuple[dict, str]] = []
    for c in commits:
        try:
            tasks.append(build_task(repo, c))
            commits_of.append(c)
        except Exception as exc:  # noqa: BLE001 —— 构造失败只该影响这一个候选
            build_failed.append((c, f"{type(exc).__name__}: {exc}"))

    kept = 0
    for c, t in zip(commits_of, tasks):
        # 逐条打印：闸门要跑几十分钟，全跑完再一次性输出等于没有进度可言
        # （而且中途被 Ctrl+C / 崩溃就什么都看不到）。
        v = validate(t, repo)
        mark = "✓ 有效" if v.valid else "✗ 拒"
        note = "" if v.valid else f"  —— {v.reason}"
        flag = "  [base 侧收集错误]" if v.base_collect_error else ""
        print(f"  {mark} {c['sha'][:8]}  {c['subject'][:52]:<52}"
              f" base_rc={v.base_rc} fix_rc={v.fix_rc}{flag}{note}")
        kept += 1 if v.valid else 0
    for c, why in build_failed:
        print(f"  ✗ 拒 {c['sha'][:8]}  {c['subject'][:52]:<52}  —— 构造任务失败（{why}）")
    print(f"\n  候选 {len(commits)} → 闸门后有效 {kept} → 拒 {len(commits) - kept}")


if __name__ == "__main__":
    main()
