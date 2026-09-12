"""C 变异测试（牙齿检查）：多仓参数化那一处，逐条打断它的不变式。

用法：python evalverify/mutate_c.py

C 这一轮改的代码很少（`REPOS` 登记表 + `ensure_repo` 的 URL 解析 + `--clone` 的文案），
所以变异体也少 —— 但**只留真正有牙齿的那一格**：

`ensure_repo` 在仓名没登记时**不能猜 URL**。猜错的失效模式是安静型的：
不报错、不中断，克隆出一个不相干的仓，而闸门照常跑出"有效任务"来 ——
于是整个第二轮评估建在错的仓库上，报告里每一行都还是自洽的。
这正是本项目记录在案的头号缺陷类（机制在、意思错了），所以要有一条把它钉住。

**故意不加的变异体**：`ensure_repo` 里"已存在 `.git` 就直接返回"那一格。
把它改成 `exists()` 确实是个真缺陷（半成品目录会被当成可用仓库），但要测它就得
真的去 clone（网络 + 3 次重试 × 3 秒），为一个 2 行的改动引入一个依赖网络的慢测试
不划算；而且它的失效模式**不安静** —— 后续 `git -C` 会立刻报错。留痕在此，不假装测过。

**SKIP 与 MISS 一样按失败看**（锚点找不到 = 这个变异体已经不再针对任何东西）。
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASETEMP = "D:/RAG项目/coding_agent/.pytest_tmp/mut_c"

GOLDEN = "eval/golden_tasks.py"
T = "tests/test_golden_tasks.py"

MUTATIONS = [
    (
        "仓名没登记时回落到 tinydb（安静地克隆错仓库，闸门照样出'有效任务'）",
        f"{T}::test_ensure_repo_refuses_to_guess_a_url_for_an_unregistered_repo",
        [(GOLDEN,
          "        name = repo_dir.name\n"
          "        if name not in REPOS:\n"
          "            raise ValueError(\n"
          '                f"{repo_dir} 不在 REPOS 登记表里（已知：{sorted(REPOS)}）。"\n'
          '                "克隆别的仓要么先登记，要么显式传 clone_url。"\n'
          "            )\n"
          "        clone_url = REPOS[name]\n",
          "        clone_url = REPOS.get(repo_dir.name, TINYDB_REPO)\n")],
    ),
]


def main() -> int:
    results = []
    for label, target, edits in MUTATIONS:
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
                capture_output=True, text=True, cwd=str(ROOT),
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
