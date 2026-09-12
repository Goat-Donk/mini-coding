r"""B2 变异测试（牙齿检查）：逐个打断判定守卫的三条新不变式，确认对应测试**真的会变红**。

用法：python evalverify/mutate_b2.py

B2 收口的是三件事，**每一件的失效模式都不是崩溃型的** —— 它们全都表现为
"字段在、值也在，只是意思错了"，所以单测全绿几乎证明不了什么：

1. **P4：`rc == 0` 就判通过**。全 skip 时 pytest 的退出码也是 0，于是任何绕过
   "判定相关文件名单"的注入都照样把 `passed=True` 写进报告。变异包括：把判据退回
   `rc == 0`、把"数不出通过数"读成 0、从 6 行 `summary` 而不是完整 `out` 里解析。
2. **P1：判定前恢复判定相关文件**。它有三个危险位置（挪到 after 快照之前 /
   挪到 judge 之后 / 干脆不记 `judge_restored`），**只看 `passed is False` 一个都抓不住**
   —— 恢复挪位之后"不通过"这个结果可能一模一样。所以目标测试断言的是
   `zero_change` / `judge_tampering` / `judge_restored` / "judge 被调用那一刻的盘面"
   这四个值本身。
3. **口径与名单**：默认值翻回去、`--no-guard-judge` 被删、`tests/__init__.py` 那一档
   丢掉、以及**反向**的一格 —— 把 `"__init__.py"` 加进按名字匹配的名单（那会让
   `tinydb/__init__.py` 这种合法源码被恢复回去，制造假阴性）。

`-P` / `--rootdir` / `--confcutdir` 不在本脚本里：它们是零成本探针（见计划 §7 第 5 步），
采纳与否由实测决定，还没写进实现。

**一个被撤掉的变异体（留痕，免得下次又想起来）**：原计划里有一条"统计行不再锚定行尾
（`\bin\s+\d+(?:\.\d+)?s\b`）"。撤掉的原因是**没有任何真实输入能把它区分出来** ——
判据是从后往前找**第一个**匹配行，而真实 pytest 输出里排在统计行之后的
unraisable traceback（留档 `45a4d4b3` 的原文逐行核过）没有任何一行含 `in <数字>s`。
按本仓纪律，瞄不准的变异体要**删掉并写明原因**，不许硬凑一个假输入把它逼红。
真正被实测抓到的是**反方向**：锚定写得太死。pytest 8.4.1 在 `seconds >= 60` 时
把统计行写成 `1 passed in 65.32s (0:01:05)`（`_pytest/terminal.py:format_session_duration`），
少了 `(H:MM:SS)` 那一档就整条数不出来 → P4 静默失效。所以那一条变异体换成了它。

**第一次跑出来的 1 个 MISS 与它的处置（留痕）**："默认值翻回去"打在 `run_single` 的
默认参数上，第一轮**没被抓住** —— `test_guard_default_is_ON` 当时只跑了 `run_eval`，
而 `run_eval` 会把自己的默认值显式传下去，所以只翻 `run_single` 观察不到。
这一格的缺口在**测试**（它的名字说"默认是开"，却只钉了两个入口里的一个），
所以处置是**把测试补齐**（两个入口的签名默认值都断言、且必须一致），
而不是把变异体删掉 —— 翻默认值是真会发生的回归。

**SKIP 与 MISS 一样按失败看**（锚点找不到 = 这个变异体已经不再针对任何东西）。
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASETEMP = "D:/RAG项目/coding_agent/.pytest_tmp/mut_b2"

RUNNER = "eval/runner.py"
GOLDEN = "eval/golden_tasks.py"
T = "tests/test_eval_runner.py"
#: 判据/解析（P4）那一层的用例住在 test_golden_tasks.py —— 它测的是 golden_tasks 自己的函数，
#: 只有恢复机制（P1）与口径翻转那几条住在 test_eval_runner.py。
G = "tests/test_golden_tasks.py"

#: 恢复块（挪位/停用两个变异都要动它）。逐字抄自 `run_single`。
_RESTORE_BLOCK = """        if judge_guard:
            judge_restored, restore_failed = _restore_judge_sensitive(
                ws, captured, judge_tampering or []
            )
        else:
            judge_restored, restore_failed = None, []
"""

#: 变异体格式：`(说明, 目标测试, [(文件, 原文, 新文), ...])`。
#: 允许多条编辑，是因为"把恢复挪到 judge 之后"必须同时做两件事：
#: 先在原位停用，再在 judge 之后补上 —— 只改一处达不到那个形状。
MUTATIONS = [
    # ---------- P4：判据退回"只看退出码" ----------
    (
        "判据退回 `rc == 0`（全 skip 时退出码也是 0 → 注入照样通过）",
        f"{G}::test_run_pytest_refuses_a_session_where_nothing_actually_ran",
        [(GOLDEN,
          "    return returncode == 0 and (passed_count is None or passed_count > 0)\n",
          "    return returncode == 0\n")],
    ),
    (
        "判据退回 `rc == 0`（同一个机制，打在纯函数那一格上）",
        f"{G}::test_judged_passed_never_reads_cannot_parse_as_zero",
        [(GOLDEN,
          "    return returncode == 0 and (passed_count is None or passed_count > 0)\n",
          "    return returncode == 0\n")],
    ),
    (
        '把"数不出通过数"读成 0（真实通过会被翻成失败 —— 修 bug 制造新假阴性）',
        f"{G}::test_parse_passed_count_anchors_on_the_tally_line",
        [(GOLDEN,
          "        return int(m.group(1)) if m else 0\n    return None\n",
          "        return int(m.group(1)) if m else 0\n    return 0\n")],
    ),
    (
        "从 6 行 summary 而不是完整 out 里解析计数（45a4d4b3 那一格会翻车）",
        f"{G}::test_run_pytest_counts_a_real_pass_whose_tally_line_is_outside_the_summary",
        [(GOLDEN,
          "    passed_count = _parse_passed_count(out)\n",
          "    passed_count = _parse_passed_count(summary)\n")],
    ),
    (
        "统计行丢掉 `(H:MM:SS)` 那一档（跑满 60 秒的判定静默失去 P4 保护）",
        f"{G}::test_tally_line_is_recognised_after_a_minute",
        [(GOLDEN,
          r'(?:\s*\(\d+:\d{2}:\d{2}\))?\s*=*\s*$"',
          r'\s*=*\s*$"')],
    ),
    # ---------- P1：恢复的三个危险位置 ----------
    (
        "恢复挪到 after 快照**之前**（zero_change 变 True、篡改证据消失）",
        f"{T}::test_restore_runs_after_the_snapshot_and_before_judge",
        [(RUNNER,
          "        after = _snapshot_tree(ws)\n        zero_change = after == before\n",
          "        if judge_guard:\n"
          "            _restore_judge_sensitive(ws, captured, _judge_tampering(before, _snapshot_tree(ws)))\n"
          "        after = _snapshot_tree(ws)\n"
          "        zero_change = after == before\n")],
    ),
    (
        "恢复挪到 judge **之后**（篡改已经生效，恢复毫无意义）",
        f"{T}::test_restore_runs_after_the_snapshot_and_before_judge",
        [
            (RUNNER, _RESTORE_BLOCK,
             "        judge_restored, restore_failed = ([], []) if judge_guard else (None, [])\n"),
            (RUNNER,
             '                error = f"judge 失败: {exc}"\n',
             '                error = f"judge 失败: {exc}"\n'
             "            if judge_guard and judge_tampering:\n"
             "                judge_restored, _late = _restore_judge_sensitive(\n"
             "                    ws, captured, judge_tampering)\n"),
        ],
    ),
    (
        "不记 judge_restored（等于「我们悄悄把现场清干净了」）",
        f"{T}::test_restore_runs_after_the_snapshot_and_before_judge",
        [(RUNNER,
          '                "judge_restored": r.judge_restored,\n',
          '                "judge_restored": None,\n')],
    ),
    (
        "guard 关着时用 `[]` 而不是 `None`（「没做」被读成「做了、没事」）",
        f"{T}::test_restore_is_off_when_the_guard_is_off",
        [(RUNNER,
          "            judge_restored, restore_failed = None, []\n",
          "            judge_restored, restore_failed = [], []\n")],
    ),
    (
        "恢复失败被吞掉（盘面不可信，却照跑 judge）",
        f"{G}::test_restore_reports_failures_instead_of_raising",
        [(RUNNER,
          "        except OSError:\n            failed.append(rel)\n",
          "        except OSError:\n            pass\n")],
    ),
    # ---------- 名单：正反两向 ----------
    (
        "丢掉相对路径那一档（`tests/__init__.py` 抓不到了）",
        f"{G}::test_capture_and_restore_judge_sensitive_files",
        [(GOLDEN,
          "    return p.name in _JUDGE_SENSITIVE or p.as_posix() in _JUDGE_SENSITIVE_PATHS\n",
          "    return p.name in _JUDGE_SENSITIVE\n")],
    ),
    (
        "名字集合里加 `__init__.py`（**反向**变异：会把合法源码的修复还原回去）",
        f"{G}::test_package_init_is_not_treated_as_judge_sensitive",
        [(GOLDEN,
          '    "conftest.py", "pytest.ini", ".pytest.ini", "pyproject.toml",\n',
          '    "__init__.py", "conftest.py", "pytest.ini", ".pytest.ini", "pyproject.toml",\n')],
    ),
    (
        "闸门的前置断言被删（金标准改了根 conftest.py 也照放行）",
        f"{G}::test_gate_rejects_a_task_whose_gold_patch_touches_judge_sensitive_files",
        [(GOLDEN,
          '    blind = judge_sensitive_blind_spots(task)\n',
          '    blind = []\n')],
    ),
    (
        "判定期不再把 pytest 钉在工作区（`--rootdir`/`--confcutdir` 拿掉）",
        f"{G}::test_judge_pins_pytest_to_the_workspace",
        [(GOLDEN,
          '        f"--rootdir={workspace}", f"--confcutdir={workspace}",\n',
          '')],
    ),
    (
        "把非 .py 的测试数据文件也当节点交给 pytest（`tests/files/*.sql` 会让它"
        "以退出码 4 收场，任务被误判成'判定无效'）",
        f"{G}::test_pytest_argv_drops_non_python_test_data_files",
        [(GOLDEN,
          '    return [f for f in test_files if f.endswith(".py")]\n',
          "    return list(test_files)\n")],
    ),
    # ---------- 口径翻转本身 ----------
    (
        "默认值翻回去（guard 默认关）",
        f"{T}::test_guard_default_is_ON",
        [(RUNNER,
          "    judge_guard: bool = True,\n) -> TaskResult:\n",
          "    judge_guard: bool = False,\n) -> TaskResult:\n")],
    ),
    (
        "`--no-guard-judge` 被删（旧开关回来 → 归档口径复现不了）",
        f"{T}::test_cli_exposes_no_guard_judge_as_the_escape_hatch",
        [(RUNNER,
          '        "--no-guard-judge", dest="guard_judge", action="store_false",\n',
          '        "--guard-judge", dest="guard_judge", action="store_true",\n')],
    ),
]


def main() -> int:
    results = []
    for label, target, edits in MUTATIONS:
        # 两份：`pristine` 只读一次、永不改（还原用），`current` 随编辑走（查锚点用）。
        pristine: dict[str, str] = {}
        current: dict[str, str] = {}
        for rel, _old, _new in edits:
            if rel not in pristine:
                pristine[rel] = (ROOT / rel).read_text(encoding="utf-8")
                current[rel] = pristine[rel]
        if any(old not in current[rel] for rel, old, _new in edits):
            results.append((label, "跳过：锚点没找到（代码可能已变）"))
            print(f"SKIP  锚点没找到  {label}", flush=True)
            continue

        for rel, old, new in edits:
            current[rel] = current[rel].replace(old, new, 1)
            (ROOT / rel).write_text(current[rel], encoding="utf-8")
        try:
            proc = subprocess.run(
                [sys.executable, "-m", "pytest", target, f"--basetemp={BASETEMP}",
                 "-o", "addopts=", "-q"],
                capture_output=True,
                text=True,
                cwd=str(ROOT),
            )
            verdict = "OK  " if proc.returncode != 0 else "MISS"
        finally:
            for rel, text in pristine.items():
                (ROOT / rel).write_text(text, encoding="utf-8")
        results.append((label, verdict))
        print(f"{verdict}  {label}", flush=True)

    caught = sum(1 for _, v in results if v.startswith("OK"))
    print(f"\n{caught}/{len(results)} 个变异体被抓住")
    for label, verdict in results:
        if not verdict.startswith("OK"):
            print(f"  !!! {verdict}  {label}")
    return 0 if caught == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
