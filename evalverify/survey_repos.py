"""扩样本调研（**零 LLM 成本**）：候选仓库逐个量，量完再选，不靠印象。

用法：python -u -m evalverify.survey_repos                 # 量默认那批候选
      python -u -m evalverify.survey_repos --only packaging

选第二个仓不是"随便挑一个 Python 项目"，它要同时满足四组约束，缺一个都白跑。
前三组是计划阶段就想清楚的，**第四组是量出来的**（第一轮表跑完才发现，见下）：

1. **S37 的规模约束**：`single-shot` 臂喂的是**整个包的全部源码**，S37 已记
   "结论只在整包塞得下（≈35k tokens）的规模上成立"。所以包体积必须与 tinydb 相当或更小。
2. **判定层的形状约束**：`discover_fix_commits` 认的是 `tests/` 这个**路径前缀**，
   且要求提交同时改源码与 `tests/`。测试目录叫 `testing/`、`test/`、
   或者测试放在包内部的仓（如 tenacity 的 `tenacity/tests/`）**直接出 0 个候选** ——
   这一条不看代码是猜不出来的，所以这里真的跑一次发现。
3. **闸门通过率**：tinydb 上游观测是 53.8%（39 → 21）。要凑到 50 个有效任务，
   tinydb 已出 21，第二个仓还需要 **≈29 个有效 → ≈54 个候选**。
   （⚠️ 53.8% 是 **tinydb 上量出来的**，不能当成候选仓的通过率 —— 真实产出只有闸门能给出，
   见 README「闸门在每次 arm 调用里都重跑」。所以这里的候选数只用来**排除明显不够的仓**。）
4. **必须能"原地跑"** —— 第一轮的表漏了这一格，而漏掉的后果是整仓归零：
   判定期的 pytest 是 `cwd=工作区`、**不装任何东西**直接跑的（`golden_tasks._pytest_argv`）。
   包在 `src/` 下的仓（flake8 / requests / attrs / packaging / build / filelock / itsdangerous /
   markupsafe 都是）于是 `import <包名>` 直接失败 —— 实测 flake8 的 `tests/conftest.py:6`
   就写着 `import flake8`，一个用例都收集不到。**这种仓的每个候选在闸门里都会被拒**，
   候选数再多也是 0。判据：包目录在**仓根**（flat layout）或仓根有同名单模块。

体积这一列现在是**实测**（直接调 `eval.runner._collect_sources` —— 就是 single-shot
真正喂出去的那一份），不再是"排除了 tests/docs 的 .py 总和"。
`字符/4` 仍只是**上界估计**：真实 token 数要真送一次才知道，这里只用它排序。
"""
from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from eval.golden_tasks import discover_fix_commits  # noqa: E402

REPOS_DIR = ROOT / "eval/repos"

#: 候选仓。挑的都是**纯 Python + pytest + `tests/`** 这一档，
#: 且每个都有相当数量的 bugfix 提交（否则凑不出候选）。
CANDIDATES: dict[str, str] = {
    # —— 第一轮（体积偏大的那几个，用来确定"大仓能不能用"）——
    "packaging": "https://github.com/pypa/packaging.git",
    "filelock": "https://github.com/tox-dev/filelock.git",
    "tomlkit": "https://github.com/python-poetry/tomlkit.git",
    "bidict": "https://github.com/jab/bidict.git",
    "itsdangerous": "https://github.com/pallets/itsdangerous.git",
    # —— 第二轮（冲着"体积接近 tinydb 且提交数够"去的）——
    "markupsafe": "https://github.com/pallets/markupsafe.git",
    "flake8": "https://github.com/PyCQA/flake8.git",
    "attrs": "https://github.com/python-attrs/attrs.git",
    "pip-tools": "https://github.com/jazzband/pip-tools.git",
    "jmespath.py": "https://github.com/jmespath/jmespath.py.git",
    "zipp": "https://github.com/jaraco/zipp.git",
    "build": "https://github.com/pypa/build.git",
    "requests": "https://github.com/psf/requests.git",
    # —— 第三轮（第一轮的表量出"src/ 布局原地跑不起来"之后，改冲 flat 布局的小仓）——
    "more-itertools": "https://github.com/more-itertools/more-itertools.git",
    "sqlparse": "https://github.com/andialbrecht/sqlparse.git",
    "natsort": "https://github.com/SethMMorton/natsort.git",
    "funcy": "https://github.com/Suor/funcy.git",
    "chardet": "https://github.com/chardet/chardet.git",
    "idna": "https://github.com/kjd/idna.git",
    "mistune": "https://github.com/lepture/mistune.git",
    "markdown": "https://github.com/Python-Markdown/markdown.git",
    "fastjsonschema": "https://github.com/horejsek/python-fastjsonschema.git",
    "python-json-patch": "https://github.com/stefankoegl/python-json-patch.git",
    "prettytable": "https://github.com/jazzband/prettytable.git",
    "python-slugify": "https://github.com/un33k/python-slugify.git",
    "munch": "https://github.com/Infinidat/munch.git",
    "arrow": "https://github.com/arrow-py/arrow.git",
    "invoke": "https://github.com/pyinvoke/invoke.git",
    "schedule": "https://github.com/dbader/schedule.git",
    "pyparsing": "https://github.com/pyparsing/pyparsing.git",
    "xmltodict": "https://github.com/martinblech/xmltodict.git",
    "backoff": "https://github.com/litl/backoff.git",
    "loguru": "https://github.com/Delgan/loguru.git",
    "tenacity": "https://github.com/jd/tenacity.git",
    "pyjwt": "https://github.com/jpadilla/pyjwt.git",
    "tabulate": "https://github.com/astanin/python-tabulate.git",
    "termcolor": "https://github.com/termcolor/termcolor.git",
    "wcwidth": "https://github.com/jquast/wcwidth.git",
}

#: 基准仓：所有体积比较都用**同一把尺子**量它，别拿 S37 记的 ≈35k 直接比 ——
#: 那个数是"真送进 prompt 的量"（含 tokenizer 与包装开销），与这里的 `字符/4`
#: 不是同一把尺子。自己量一遍，比较才成立。
BASELINE = ("tinydb", "https://github.com/msiemens/tinydb.git")

_INIFILE_TITLES = ("pytest.ini", ".pytest.ini", "pyproject.toml", "setup.cfg", "tox.ini")


def _clone(name: str, url: str) -> Path:
    dest = REPOS_DIR / name
    if dest.exists():
        return dest
    REPOS_DIR.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        ["git", "clone", "--quiet", url, str(dest)],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"clone {url} 失败: {(proc.stderr or '')[:200]}")
    return dest


def _source_stats(repo: Path) -> dict:
    """**实测** single-shot 会喂出去的那一份：直接调 `eval.runner._collect_sources`。

    早先这里是自己写的"排除 tests/docs 的 .py 总和"，那是**另一把尺子** ——
    它排除了 `setup.py`、`scripts/` 之类，而 `_collect_sources` 只排除 `tests/`。
    两把尺子量出来的倍数不可比，所以换成真的那份。
    """
    from eval.runner import _collect_sources

    src = _collect_sources(repo)
    chars = sum(len(v) for v in src.values())
    return {"源文件数": len(src), "字符数": chars, "估算 tokens（字符/4）": chars // 4}


def _inifiles(repo: Path) -> list[str]:
    """真正会被 pytest 当配置读的那些（`pyproject.toml` 要有 `[tool.pytest.ini_options]`）。"""
    found = []
    for name in _INIFILE_TITLES:
        p = repo / name
        if not p.exists():
            continue
        if name == "pyproject.toml":
            txt = p.read_text(encoding="utf-8", errors="replace")
            if "[tool.pytest.ini_options]" not in txt:
                continue
            found.append(f"{name}[tool.pytest.ini_options]")
        elif name in ("setup.cfg", "tox.ini"):
            txt = p.read_text(encoding="utf-8", errors="replace")
            if not re.search(r"^\[(tool:)?pytest\]", txt, re.M):
                continue
            found.append(f"{name}[pytest]")
        else:
            found.append(name)
    return found


def _tests_shape(repo: Path) -> dict:
    tests = repo / "tests"
    if not tests.is_dir():
        return {"有 tests/": False}
    py = list(tests.rglob("test_*.py"))
    return {
        "有 tests/": True,
        "测试文件数": len(py),
        "tests/__init__.py": (tests / "__init__.py").exists(),
        "tests/conftest.py": (tests / "conftest.py").exists(),
    }


def _layout(repo: Path) -> str:
    """包目录在哪 —— 决定"判定期装都不装能不能 import"。

    判定期的 pytest 是 `cwd=工作区` 直接跑的（`python -m pytest` 会把 cwd 放进 `sys.path`），
    所以**包目录必须在仓根**。`src/` 布局的仓要 `pip install -e .` 才 import 得到，
    而工作区是逐任务复制出来的，装不进一个固定路径的 editable 安装。
    """
    if (repo / "src").is_dir():
        return "src/"
    flat = [p.name for p in sorted(repo.iterdir())
            if p.is_dir() and (p / "__init__.py").exists() and p.name != "tests"]
    if flat:
        return "flat:" + ",".join(flat[:3])
    mods = [p.name for p in sorted(repo.glob("*.py")) if p.name not in ("setup.py", "conftest.py")]
    return "单模块:" + ",".join(mods[:3]) if mods else "无包"


def _inplace_probe(repo: Path) -> dict:
    """**原地**收集一遍测试（不装任何东西、`-o addopts=` 清掉仓自带的 addopts）。

    这是 `_layout` 的实测版：`rc=0` 说明这个仓在判定期那条路上走得通。
    `rc=4/2` 往往是缺测试期依赖（hypothesis 之类）或 import 不到自己的包 —— 两种都致命：
    **候选再多，过不了闸门也是 0**。所以这一格和候选数一样重要。
    """
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", "tests", "-q", "--no-header",
             "--collect-only", "-o", "addopts=", "-p", "no:cacheprovider"],
            capture_output=True, text=True, cwd=str(repo), timeout=300,
        )
    except subprocess.TimeoutExpired:
        return {"原地收集rc": "TIMEOUT", "原地收集末行": ""}
    lines = [ln for ln in (proc.stdout + proc.stderr).strip().splitlines() if ln.strip()]
    return {"原地收集rc": proc.returncode, "原地收集末行": (lines[-1][:70] if lines else "")}


def survey(name: str, url: str, *, limit: int) -> dict:
    repo = _clone(name, url)
    out: dict = {"仓": name, "体积": _source_stats(repo), "布局": _layout(repo),
                 "inifile": _inifiles(repo), "测试形状": _tests_shape(repo)}
    out.update(_inplace_probe(repo))
    try:
        commits = discover_fix_commits(repo, limit=limit)
    except Exception as exc:  # noqa: BLE001 —— 一个仓量不动不该带走整张表
        out["候选数"] = f"发现失败: {type(exc).__name__}: {exc}"
        return out
    out["候选数"] = len(commits)
    # 第二个仓的判据不是"有没有候选"，而是"够不够 54 个"（tinydb 已出 21，还差 ≈29）。
    out["够 54 个候选？"] = len(commits) >= 54
    if commits:
        out["最近一条"] = f"{commits[0]['sha'][:8]}  {commits[0]['subject'][:60]}"
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="扩样本候选仓调研（零 LLM 成本）")
    ap.add_argument("--only", action="append", default=None, help="只量这几个仓（可重复）")
    ap.add_argument("--limit", type=int, default=300, help="每个仓最多发现多少候选")
    args = ap.parse_args()

    # 基准先量：表里的"倍数"是相对**同一把尺子量出来的** tinydb，不是相对 S37 那句 ≈35k。
    base_repo = _clone(*BASELINE)
    base_stats = _source_stats(base_repo)
    base_tok = base_stats["估算 tokens（字符/4）"]
    print(f"基准 {BASELINE[0]}：{base_stats['源文件数']} 个源文件 · "
          f"{base_stats['字符数']} 字符 · 估算 {base_tok} tokens（字符/4）", flush=True)

    names = args.only or list(CANDIDATES)
    rows = []
    for name in names:
        url = CANDIDATES[name]
        print(f"\n=== {name}  {url} ===", flush=True)
        try:
            row = survey(name, url, limit=args.limit)
        except Exception as exc:  # noqa: BLE001 —— 克隆失败也如实记一行
            row = {"仓": name, "错误": f"{type(exc).__name__}: {exc}"}
        rows.append(row)
        for k, v in row.items():
            if k != "仓":
                print(f"  {k}: {v}", flush=True)

    print("\n=== 一张表 ===")
    print("（`原地rc`：不装任何东西、cwd=仓根收集一遍测试的退出码。0 才说明这个仓在判定期走得通。）")
    print(f"{'仓':<15}{'源文件':>6}{'实测tok':>8}{'基准倍数':>9}{'原地rc':>7}"
          f"{'布局':>22}{'inifile':>26}{'候选数':>7}")
    for r in rows:
        if "错误" in r:
            print(f"{r['仓']:<15}  {r['错误']}")
            continue
        vol = r["体积"]
        tok = vol["估算 tokens（字符/4）"]
        ratio = f"{tok / base_tok:.2f}x" if base_tok else "n/a"
        print(f"{r['仓']:<15}{vol['源文件数']:>6}{tok:>8}{ratio:>9}"
              f"{str(r['原地收集rc']):>7}{r['布局']:>22}"
              f"{','.join(r['inifile']) or '无':>26}{str(r['候选数']):>7}")
        if r["原地收集rc"] != 0:
            print(f"{'':15}  └─ {r['原地收集末行']}")
    print(f"\n（基准 {BASELINE[0]} 的候选数是上游已知的 39，不在本表重跑；本仓自带 pytest.ini，原地 rc=0。）")
    print("（选仓要同时满足：原地rc=0、候选数≥54、基准倍数≲1.3 —— 三条都印在同一行上，别只看一列。）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
