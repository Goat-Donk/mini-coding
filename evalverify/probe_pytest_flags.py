r"""零成本探针：`-P` / `--rootdir` / `--confcutdir` 到底要不要采纳（**不发一次 LLM 请求**）。

用法：python evalverify/probe_pytest_flags.py

计划 §7 第 5 步要求"先实测再决定"。这里量的是两件**互相独立**的事：

1. **`-P`（`PYTHONSAFEPATH`）会不会把判定跑坏** —— 它从启动期就不把 cwd 放进 `sys.path`。
   风险很实在：`import tinydb` 在很多仓里正是靠 cwd 那条路径成立的。
   判据：同一棵树、同一批测试文件，带 `-P` 与不带 `-P` 的 `(退出码, 通过数)` 必须一致。
   ⚠️ 只比对结论，不比对耗时与告警文本 —— 那些本来就会抖。

2. **rootdir 会不会爬到评测器自己身上** —— 真实风险不在 tinydb（它自带 `pytest.ini`，
   rootdir 本来就落在工作区），而在**换一个不自带 inifile 的仓**：rootdir 会一路爬到
   `coding_agent/`，那里有 `conftest.py`，且 `pyproject.toml` 带 `[tool.pytest.ini_options]`
   —— **评测器自己的 conftest 会在判定期被加载**。探针把那个场景**造出来**
   （把 inifile 全删掉的副本），分别量"加不加 `--rootdir/--confcutdir`"两种情形下
   pytest 实际加载了哪些 conftest。

3. **`-P` 对"flat 包 + `tests/` 没有 `__init__.py`"这一格的影响** —— 这是 tinydb
   **量不出来**的那一格：tinydb 有 `tests/__init__.py`，pytest 本来就会把工作区塞进
   `sys.path`，`-P` 在它身上看起来无害。换成没有那个文件的仓，`import <包名>` 就只剩
   "`python -m` 把 cwd 放进 `sys.path`"这一条路，而 `-P` 砍的正是它。
   在一个**自己造的迷你仓**上量（离线、可复现、不依赖任何候选仓）。

探针只报事实，不自己下"采纳"的结论 —— 采纳与否要连着闸门（39→21 不变）一起看。
"""
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from eval.golden_tasks import _parse_passed_count  # noqa: E402

SRC_REPO = ROOT / "eval/repos/tinydb"

#: 造"没有 inifile"的副本时要摘掉的东西（pytest 的 inifile 候选）。
INIFILES = ("pytest.ini", ".pytest.ini", "pyproject.toml", "setup.cfg", "tox.ini")

#: 一个够小、但真的用到 `tests/conftest.py` 的 fixture、且 `import tinydb` 的测试文件。
PROBE_TESTS = ["tests/test_tables.py", "tests/test_storages.py"]

PLUGIN_NAME = "_probe_plugin"
PLUGIN_SRC = '''
import json
import sys


def pytest_addoption(parser):
    parser.addoption("--probe-out", action="store", default=None)


def pytest_configure(config):
    sink = config.getoption("--probe-out")
    if not sink:
        return
    confests = sorted(
        (getattr(m, "__file__", "") or "")
        for m in list(sys.modules.values())
        if (getattr(m, "__file__", "") or "").endswith("conftest.py")
    )
    with open(sink, "w", encoding="utf-8") as f:
        json.dump({
            "rootdir": str(config.rootpath),
            "confcutdir": str(config.known_args_namespace.confcutdir or ""),
            "conftests": confests,
            "sys_path_head": sys.path[:4],
        }, f, ensure_ascii=False, indent=2)
'''


def _run(workspace: Path, extra: list[str], probe_out: Path | None = None):
    """跑一次 pytest，返回 `(退出码, 完整输出, 探针记录 | None)`。

    `-P` **不放这里**：它是**解释器**开关，必须写在 `-m pytest` 之前，
    塞进 pytest 参数里会被当成未知选项（退出码 4）—— 那会把探针自己弄成假红。
    """
    argv = [sys.executable, "-m", "pytest", *PROBE_TESTS, "-q", "--no-header",
            "-o", "addopts=", *extra]
    if probe_out is not None:
        (workspace / f"{PLUGIN_NAME}.py").write_text(PLUGIN_SRC, encoding="utf-8")
        argv += ["-p", PLUGIN_NAME, f"--probe-out={probe_out}"]
    try:
        proc = subprocess.run(argv, cwd=workspace, capture_output=True, text=True)
    finally:
        (workspace / f"{PLUGIN_NAME}.py").unlink(missing_ok=True)
    text = (proc.stdout or "") + (proc.stderr or "")
    info = None
    if probe_out is not None and probe_out.exists():
        info = json.loads(probe_out.read_text(encoding="utf-8"))
        probe_out.unlink()
    return proc.returncode, text, info


def _verdict(rc: int, text: str) -> dict:
    """只留判据：退出码 + 通过数（从完整输出解析，和判定层用同一把尺子）。"""
    return {"rc": rc, "passed_count": _parse_passed_count(text)}


def probe_dash_p(tmp: Path) -> dict:
    """`-P` 会不会把判定跑坏：同一棵树，两种跑法，结论必须一致。"""
    ws = tmp / "with_inifile"
    shutil.copytree(SRC_REPO, ws, ignore=shutil.ignore_patterns(".git", "docs"))
    rc_plain, text_plain, _ = _run(ws, [])
    proc = subprocess.run(
        [sys.executable, "-P", "-m", "pytest", *PROBE_TESTS, "-q", "--no-header",
         "-o", "addopts="],
        cwd=ws, capture_output=True, text=True,
    )
    rc_p, text_p = proc.returncode, (proc.stdout or "") + (proc.stderr or "")
    out = {"不带 -P": _verdict(rc_plain, text_plain),
           "带 -P": _verdict(rc_p, text_p)}
    out["结论一致"] = out["不带 -P"] == out["带 -P"]
    return out


def probe_rootdir(tmp: Path, ws: Path) -> dict:
    """把"目标仓不带 inifile"这个场景造出来，量 rootdir 与**实际加载的 conftest**。

    ⚠️ 副本必须落在**本仓目录之下**（`ws` 由调用方建在 `ROOT` 里）。第一版探针把副本
    建在 `%TEMP%`，于是"向上找 inifile"那一趟永远走不到本仓 —— 得出的 rootdir 是副本
    自己，结论是假绿。真实工作区在 `data/eval/ws/<任务>/`，**祖先链里就是有本仓**。
    """
    shutil.copytree(SRC_REPO, ws, ignore=shutil.ignore_patterns(".git", "docs"))
    removed = []
    for name in INIFILES:
        p = ws / name
        if p.exists():
            p.unlink()
            removed.append(name)

    out: dict = {"摘掉的 inifile": removed,
                 "评测器自己的根目录": str(ROOT)}
    variants = (
        ("只 -o addopts=", []),
        ("加 --rootdir/--confcutdir", [f"--rootdir={ws}", f"--confcutdir={ws}"]),
    )
    our_conftest = (ROOT / "conftest.py").resolve()
    for i, (label, extra) in enumerate(variants):
        rc, text, info = _run(ws, extra, probe_out=tmp / f"probe_{i}.json")
        info = info or {}
        info.update(_verdict(rc, text))
        raw_conftests = [Path(c).resolve() for c in info.get("conftests", [])]
        # ⚠️ 判据必须是**路径相等**，不能是"字符串里含本仓目录" ——
        # 工作区本来就建在本仓之下（`data/eval/ws/<任务>/`），子串判断会把
        # 工作区自己的 conftest 全算成"我们的"，得出假结论。
        info["加载了评测器自己的 conftest"] = our_conftest in raw_conftests
        info["加载的 conftest"] = [
            str(c.relative_to(ROOT)) if ROOT in c.parents else str(c)
            for c in raw_conftests
        ]
        # sys.path 里有没有本仓根：有的话，隐藏测试能 `import eval.*` / `import agent.*`。
        info["sys_path 含评测器根目录"] = any(
            Path(p).resolve() == ROOT for p in info.get("sys_path_head", []) if p
        )
        info["sys_path_head"] = [str(Path(p).name or p) for p in info.get("sys_path_head", [])]
        out[label] = info
    return out


def _mini_repo(root: Path, *, tests_init: bool) -> Path:
    """造一个"flat 包 + 一个测试文件"的迷你仓，用来量 `-P` 的那一格。

    形状照着真实候选仓来：包目录在**仓根**（`pkg/`），测试在 `tests/`，
    `tests/__init__.py` 由参数决定有没有 —— 这一个文件就是 `-P` 的开关。
    """
    (root / "pkg").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "pkg/__init__.py").write_text("VALUE = 1\n", encoding="utf-8")
    (root / "tests/test_ok.py").write_text(
        "from pkg import VALUE\n\n\ndef test_ok():\n    assert VALUE == 1\n",
        encoding="utf-8",
    )
    if tests_init:
        (root / "tests/__init__.py").write_text("", encoding="utf-8")
    return root


def _run_mini(ws: Path, *, dash_p: bool) -> int:
    argv = [sys.executable, *(["-P"] if dash_p else []), "-m", "pytest",
            "tests", "-q", "--no-header", "-o", "addopts="]
    return subprocess.run(argv, cwd=ws, capture_output=True, text=True).returncode


def probe_dash_p_without_tests_init(tmp: Path) -> dict:
    """`-P` 在"没有 `tests/__init__.py`"的 flat 仓上会不会把 import 打断。"""
    out: dict = {}
    for label, tests_init in (("有 tests/__init__.py（tinydb 那种）", True),
                              ("没有 tests/__init__.py", False)):
        ws = _mini_repo(tmp / ("mini_init" if tests_init else "mini_noinit"),
                        tests_init=tests_init)
        rc_plain = _run_mini(ws, dash_p=False)
        rc_p = _run_mini(ws, dash_p=True)
        out[label] = {"不带 -P 的退出码": rc_plain, "带 -P 的退出码": rc_p,
                      "结论一致": rc_plain == rc_p}
    return out


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="probe_flags_"))
    # 探针 2 的副本要落在本仓目录下（见 probe_rootdir 的说明），跑完连根删掉。
    # 用固定名字而不是随机名：万一上一轮崩在中途留了残骸，这里先清干净，
    # 免得 copytree 撞上已存在的目录再报一个与探针无关的错。
    under_repo = ROOT / "_probe_ws"
    shutil.rmtree(under_repo, ignore_errors=True)
    try:
        print("=== 探针 1：-P（PYTHONSAFEPATH）会不会把判定跑坏 ===")
        print(json.dumps(probe_dash_p(tmp), ensure_ascii=False, indent=2))
        print("\n=== 探针 2：目标仓不带 inifile 时，rootdir 爬到哪、谁被加载 ===")
        print(json.dumps(probe_rootdir(tmp, under_repo / "no_inifile"),
                         ensure_ascii=False, indent=2))
        print("\n=== 探针 3：-P 打在「flat 包 + 没有 tests/__init__.py」上 ===")
        print("（退出码 0=判定有效；2=收集期报错，会让诚实的修复被判成没修好）")
        print(json.dumps(probe_dash_p_without_tests_init(tmp),
                         ensure_ascii=False, indent=2))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.rmtree(under_repo, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
