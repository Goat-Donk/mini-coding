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
import re
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

#: 能当评测目标仓的仓库登记表（简称 → clone URL）。
#:
#: 存在的理由：`single-shot` 臂喂的是**整个包的全部源码**，S37 记的
#: "结论只在整包塞得下（≈35k tokens）的规模上成立"是一条**规模约束** ——
#: 一个仓的有效任务数不够（tinydb 上游实测：39 个候选 → 闸门后 21 个）时，
#: 补样本的唯一办法就是加仓，而加仓就得有个地方记"加的是哪个、有多大"。
#: 每个仓的体积/候选数是**实测**的，见 `evalverify/survey_repos.py`。
REPOS: dict[str, str] = {
    # 基准：10 个源文件 · 71,659 字符 · 估算 17.9k tokens（字符/4） · 39 个候选
    "tinydb": TINYDB_REPO,
    # 第二个目标仓（M5-6 选定并实跑过闸门）：flat 布局、**没有 inifile**、
    # 原地 `python -m pytest` 就能收集（不用装任何东西）—— 正是它把 `--rootdir`/
    # `--confcutdir` 那条边界逼了出来（tinydb 自带 pytest.ini，对那两个参数是 no-op）。
    # 实测：143 个候选 → 闸门后 54 个有效；single-shot 的输入按 `_collect_sources` 逐任务
    # 实测 12.4 万 ~ 15.1 万字符（随提交变化），故这里不写单一字符数。
    "sqlparse": "https://github.com/andialbrecht/sqlparse.git",
    # 下面这些只登记"量过"，不代表都已入库当目标仓（体积/候选数的取舍见 README）。
    "flake8": "https://github.com/PyCQA/flake8.git",
    "tomlkit": "https://github.com/python-poetry/tomlkit.git",
    "pip-tools": "https://github.com/jazzband/pip-tools.git",
    "attrs": "https://github.com/python-attrs/attrs.git",
    "requests": "https://github.com/psf/requests.git",
}

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

def ensure_repo(repo_dir: Path | str = DEFAULT_REPO, *, clone_url: str | None = None) -> Path:
    """克隆目标仓（幂等 + 网络重试）。返回 repo 根。

    `clone_url` 不给时按 `repo_dir` 的**目录名**去 `REPOS` 里查 —— 这样
    `--repo eval/repos/flake8 --clone` 就能work，不用再多记一个 URL 参数。
    查不到就报错并列出已知的仓，**不猜**：猜错 URL 会安静地克隆出一个不相干的仓，
    而闸门照样能跑出"有效任务"来，那是把整个第二轮评估建在错仓库上。
    """
    repo_dir = Path(repo_dir)
    if clone_url is None:
        name = repo_dir.name
        if name not in REPOS:
            raise ValueError(
                f"{repo_dir} 不在 REPOS 登记表里（已知：{sorted(REPOS)}）。"
                "克隆别的仓要么先登记，要么显式传 clone_url。"
            )
        clone_url = REPOS[name]
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
    raise RuntimeError(f"clone {clone_url} 失败: {(proc.stderr if proc else '')[:300]}")


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
    #: pytest 自报的通过用例数。**三态**：`None` = 没数出来（不是 0），
    #: `0` = 数出来了、确实一个都没通过。把前者读成后者会把一次真实通过翻成失败。
    passed_count: int | None = None

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


#: pytest 最后那行统计行：`5 passed in 0.12s` / `1 failed, 2 passed in 0.5s` /
#: 非 -q 模式下的 `======== 3 passed in 0.2s ========` /
#: 以及**跑够 60 秒之后**的 `1 passed in 65.32s (0:01:05)`。
#:
#: 判据钉在**行尾**（`in <秒>s` 之后只允许可选的 `(H:MM:SS)`、空白与 `=`）：这样它
#: 不可能命中 warning traceback 里的 `File "...", line 97, in write` —— 那种行不以时长收尾。
#: 反过来（"从后往前找第一个 `N passed`"）会更糟：`-q` 下每个失败用例也打
#: `FAILED tests/x.py::t - ...`，用例自己的输出里更可能整行出现 "3 passed" 这种字样。
#:
#: ⚠️ `(?:\(\d+:\d{2}:\d{2}\))?` 那一档**不是修饰，是实测来的**：pytest 8.4.1 的
#: `_pytest/terminal.py:format_session_duration()` 在 `seconds >= 60` 时返回
#: `f"{seconds:.2f}s ({dt})"`，也就是 `65.32s (0:01:05)` —— 时长后面**还有一串**。
#: 少了这一档，任何跑满一分钟的判定都会数不出通过数（→ `passed_count = None`），
#: P4 在那种任务上**静默失效**（不制造假阴性，但那道闸门等于没装）。
_PYTEST_TALLY_RE = re.compile(
    r"=*\s*in\s+\d+(?:\.\d+)?s(?:\s*\(\d+:\d{2}:\d{2}\))?\s*=*\s*$"
)
_PASSED_COUNT_RE = re.compile(r"(\d+)\s+passed\b")


def _parse_passed_count(out: str) -> int | None:
    """从 pytest 完整输出里数"通过了几个用例"。数不出来返回 `None`。

    ⚠️ **必须喂完整 `out`，不能喂 `summary`**：`summary` 只是输出的最后 6 行，
    而 pytest 的 unraisable / warning 摘要打在统计行**之后** ——
    留档的 `45a4d4b3`（`report-20260912-144037.json`）就是这一例：它 `passed=True`，
    但最后 6 行被 `PytestUnraisableExceptionWarning` 的 traceback 占满，
    统计行一个字都看不到。用 `summary` 当数据源 + 把"数不出"读成 0，
    会把一次**真实通过**翻成失败 —— 修 bug 的动作本身制造一个新的假阴性。
    """
    for line in reversed(out.splitlines()):
        if not _PYTEST_TALLY_RE.search(line):
            continue
        m = _PASSED_COUNT_RE.search(line)
        return int(m.group(1)) if m else 0
    return None


def _judged_passed(returncode: int, passed_count: int | None) -> bool:
    """P4 的判据本身：**退出码 0，且至少有一个用例真的通过**。

    抽成独立函数是为了能被单测**直接**钉住 —— 其中最关键的一格
    （`returncode == 0` 而 `passed_count is None`）在真实磁盘上很难造出来，
    埋在 `_run_pytest` 里就只能靠一个假造的运行去碰，或者干脆没人测它。

    `None`（没数出来）**必须**保持通过：留档的 `45a4d4b3` 正是这一格
    —— 它的统计行落在 6 行摘要之外。把 `None` 读成 0 会把一次真实通过翻成失败，
    即"修 bug 的动作本身制造一个新的假阴性"。
    """
    return returncode == 0 and (passed_count is None or passed_count > 0)


def _pytest_argv(workspace: Path, test_files: list[str]) -> list[str]:
    """判定时跑 pytest 用的完整 argv。抽出来是为了能被单测**直接**钉住。

    ⚠️ `--rootdir` / `--confcutdir` 钉在**工作区**上，这是实测换来的，不是修饰：

    目标仓自带 inifile（tinydb 就有 `pytest.ini`）时 rootdir 本来就落在工作区，
    这两个参数等于没写。但**换一个不自带 inifile 的仓**，rootdir 会一路向上爬 ——
    工作区建在本仓的 `data/eval/ws/<任务>/` 下，所以祖先链里就是有本仓的
    `pyproject.toml`（带 `[tool.pytest.ini_options]`）。`evalverify/probe_pytest_flags.py`
    把那个场景造出来量过，后果有两条，都不是小事：

    1. **评测器自己的 `conftest.py` 被当插件加载**（实测：不带参数时加载了 2 个 conftest，
       带参数时只剩工作区自己那 1 个）—— 判定期悄悄跑起我们的夹具；
    2. **本仓根目录进了 `sys.path`**（实测：不带参数时 `sys.path` 里有 `coding_agent`），
       于是隐藏测试可以 `import eval.*` / `import agent.*`。

    对 tinydb 的判定结论**零改动**（实测同批测试文件 `rc` 与通过数都不变）。

    ⚠️ `-P`（`PYTHONSAFEPATH`）**实测后不采纳**。探针在 tinydb 上量到两边结论一致（rc 都是 0），
    但 tinydb 恰好是 `tests/__init__.py` 存在、pytest 会把工作区自己插进 `sys.path` 的那种仓 ——
    对 `-P` **最有利**的一格。探针于是**合成**了一格不利的（flat 包 + 没有 `tests/__init__.py`，
    sqlparse 就是这种布局）：**不带 `-P` 退出码 0，带 `-P` 退出码 2** —— 收集期就报错，
    会把一次**诚实的修复**判成「没修好」。`-P` 砍掉的正是 `python -m pytest` 提供的 cwd 插入。
    这一格是零成本的（合成迷你仓，不依赖任何候选仓），所以不必等第二个仓定下来。

    `-o addopts=` 清掉目标仓 inifile 自带的 addopts：tinydb 写死了
    `--cov-append --cov-report --cov tinydb`，本机没装 pytest-cov 时 pytest 会
    以 usage error（退出码 4）直接退出 —— 测试一次都没跑，却会被误判成「没修好」。
    """
    workspace = Path(workspace).resolve()
    return [
        sys.executable, "-m", "pytest", *_pytest_targets(test_files),
        "-q", "--no-header", "-o", "addopts=",
        f"--rootdir={workspace}", f"--confcutdir={workspace}",
    ]


def _pytest_targets(test_files: list[str]) -> list[str]:
    """从 `test_files` 里挑出**能交给 pytest 当节点**的那些（就是 `.py`）。

    ⚠️ 这一层过滤是实测换来的，不是修饰。`test_files` 是"fix 提交改动过的
    `tests/**` 文件"，而**改动过的测试数据文件也算**：sqlparse 的 143 个候选里有 5 个
    带着 `tests/files/*.sql`（fix 提交新增了一个 SQL 夹具当测试用例的输入）。
    把这些路径当节点交给 pytest，它会直接报

        ERROR: not found: .../tests/files/multiple_case_in_begin.sql
        (no match in any of [<Dir files>])

    然后以**退出码 4（usage error）**收场 —— 一次用例都没跑，而闸门会把它记成
    "base 侧测试压根没跑成 → 判定无效"，于是一个**本来有效**的任务被丢掉。
    失效模式是安静型的：报告里那一行自洽，只是任务凭空少了。

    tinydb 的 `tests/` 全是 `.py`，所以这个形状在第一个仓上**一次都没出现过**
    （实测：tinydb 39 个候选里 0 个带非 `.py` 测试文件 → 对已留档的报告零改动）。
    """
    return [f for f in test_files if f.endswith(".py")]


def _run_pytest(workspace: Path, test_files: list[str]) -> JudgeResult:
    """在 workspace 里跑这几个测试文件，返回判定结果。

    `judge` 与 `validate_task` **共用这一处**（两处各写一遍 → 漂移）。

    用 `sys.executable` 而不是字面量 `"python"`：调用方多起来之后，这能保证
    "判定器"与"跑测试的解释器"永远是同一个。

    ⚠️ `test_files` 为空时**不跑**：无路径参数的 pytest 会收集整个测试套件，
    通常全绿 → `passed=True`，那是一条零验证的自由通过路径。

    ⚠️ `passed` 的判据是 `(rc == 0) and (passed_count is None or passed_count > 0)`。
    单看 `rc == 0` 有一个洞：**全 skip 时 pytest 的退出码也是 0**（`drive_judge_gaming`
    那条 conftest 注入路径正是这么翻盘的）。而按名单盯着"谁改了文件"永远追不上
    （`sitecustomize.py` / `.pth` / 根级 `pytest/` 模块遮蔽 / 往 site-packages 塞 conftest，
    全是名单外的写法）—— 所以这里改盯**"这次判定到底跑没跑测试"**，
    那是名单绕不过去的一层。
    """
    targets = _pytest_targets(test_files)
    if not targets:
        return JudgeResult(
            passed=False, returncode=_NO_TESTS_RC, passed_count=0,
            summary="hidden_tests 为空或全是非 .py 的测试数据文件："
                    "没有能交给 pytest 的节点（拒绝零验证通过）",
        )

    workspace = Path(workspace).resolve()
    proc = subprocess.run(
        _pytest_argv(workspace, targets),
        cwd=workspace, capture_output=True, text=True,
    )
    out = (proc.stdout or "") + (proc.stderr or "")
    lines = [ln for ln in out.splitlines() if ln.strip()]
    summary = "\n".join(lines[-6:])
    passed_count = _parse_passed_count(out)
    return JudgeResult(
        passed=_judged_passed(proc.returncode, passed_count),
        returncode=proc.returncode,
        summary=summary,
        passed_count=passed_count,
    )


# ---------- 判定相关文件 ----------
#
# **判定层（runner 的 guard）与闸门（validate_task 的前置断言）共用这一份判据。**
# 两处各写一份名单迟早漂移 —— 而漂移的后果是"闸门放行的任务、判定层会把它恢复回去"，
# 直接制造假阴性（本项目头号缺陷类）。
#
# 实测的翻盘路径（`evalverify/drive_judge_gaming.py`，188 字节 PoC 已验证）：
# `judge` 是"把修复版隐藏测试覆盖写回工作区 → 在工作区里 `pytest <test_files>`"。
# 隐藏测试被覆盖这一步是稳的，**但工作区根目录的 `conftest.py` 不会被冲掉** ——
# pytest 把它当插件自动加载，于是一个 `pytest_collection_modifyitems` 钩子把用例
# 全标成 skip，而**全 skip 时 pytest 退出码是 0** → 判"修好了"。
#
# 这条路径三层防守都拦不住：`zero_change` 拦不住（新文件，工作区确实变了）；
# 闸门拦不住（它跑的是 base/fix 原始树，没有这个文件）；报告事后也查不出
# （此前只存 `zero_change` 布尔，不存改了哪几个文件）。
#
# 名单**只加这两类，不继续加名字**：这是一场追不上的打地鼠（同 `_irreversible_kind`
# 当年放弃枚举读动词的教训）。剩下的靠 `_run_pytest` 的计数判据（"这次判定到底
# 跑没跑测试"）从机制上收口，越出名单的那一类如实记进 README 的边界表。
_JUDGE_SENSITIVE = frozenset({
    "conftest.py", "pytest.ini", ".pytest.ini", "pyproject.toml",
    "setup.cfg", "tox.ini",
})

#: 按**相对路径**匹配的那一批。存在的唯一理由是 `tests/__init__.py`：
#:
#: 它按名字匹配的名字是 `__init__.py` —— 而**绝不能**把 `"__init__.py"` 加进名字集合，
#: 那会让 `tinydb/__init__.py` 这类**合法源码**变成"判定相关文件"，于是 P1 的恢复
#: 会把 agent 的真实修复**还原回去**，直接制造假阴性。`tests/__init__.py` 在 tinydb 里
#: 存在且为空，一行 `os._exit(0)` 就能让整个 tests 包静默不执行 —— 严重度最高的一格，
#: 必须覆盖，所以用相对路径单独钉。
_JUDGE_SENSITIVE_PATHS = frozenset({"tests/__init__.py"})


def _is_judge_sensitive(rel: str) -> bool:
    """这个工作区相对路径是不是"改了就能翻盘判定"的文件。"""
    p = Path(rel)
    return p.name in _JUDGE_SENSITIVE or p.as_posix() in _JUDGE_SENSITIVE_PATHS


def judge_sensitive_blind_spots(task: GoldenTask) -> list[str]:
    """金标准自己改过的判定相关文件里，**不在隐藏测试里**的那些。

    这是 P1（判定前恢复判定相关文件）唯一的真风险面：`judge` 会把 `hidden_tests`
    覆盖写回，所以那些文件被恢复也不影响判定；但一个**只改了根 `conftest.py`
    而没动 tests/** 的金标准提交，会被恢复得连自己都跑不过 —— 诚实的通过变成不通过。

    零成本、纯元数据，所以放在闸门里当**前置断言**：这种任务直接拒，不进 LLM。
    """
    touched = set(task.changed_sources) | set(task.hidden_tests)
    return sorted(
        p for p in touched if _is_judge_sensitive(p) and p not in task.hidden_tests
    )


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

    # P1 的前置断言（零成本，纯元数据）：金标准自己改过判定相关文件、而那个文件
    # **不在**隐藏测试里 → 判定前的恢复会把它还原回去，金标准连自己都跑不过。
    # 拒掉它，而不是让它以"agent 修不好"的面目进报告。
    blind = judge_sensitive_blind_spots(task)
    if blind:
        return TaskValidity(
            False,
            f"金标准改了判定相关文件 {blind}，而它不在隐藏测试里 → 判定前的恢复"
            "会把这个改动还原，诚实的通过会被误判成失败（恢复机制的风险面）",
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

    ap = argparse.ArgumentParser(description="发现并验证黄金修 bug 任务")
    ap.add_argument("--repo", type=Path, default=DEFAULT_REPO, help="目标仓库路径")
    ap.add_argument("--limit", type=int, default=20, help="最多返回几个 fix commit")
    ap.add_argument(
        "--clone", action="store_true",
        help="先克隆（需要网络）。仓名按 `--repo` 的目录名去 REPOS 登记表里查，"
             f"已知：{sorted(REPOS)}",
    )
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
