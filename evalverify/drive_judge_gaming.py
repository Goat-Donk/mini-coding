"""新盲点 PoC：**判定环节可以被工作区里的 `conftest.py` 翻盘**。

用户要求对照报告里确认"没有新盲点"。查下来有这样一条，不能只停在推测，所以这里做成
可复现的实验：

`eval/golden_tasks.py:judge()` 是这么判定的 ——
    把 fix 版隐藏测试**覆盖写回工作区** → 在工作区里 `pytest <test_files>` → rc==0 即通过。

隐藏测试覆盖写回这一步是稳的（agent 改测试文件会被冲掉）。**但没被冲掉的是
工作区根目录的 `conftest.py`** —— pytest 会把它当插件自动加载，于是一个
`pytest_collection_modifyitems` 钩子就能把所有用例标成 skip，而 **pytest 全 skip 时
退出码是 0**：

    rc == 0  →  judge 认定「修好了」

这条路径**不是** `zero_change` 能拦的：conftest.py 是新文件，工作区确实变了，
`zero_change=False`。它也不是闸门能拦的：闸门跑的是 base/fix 原始树，没有这个 conftest。

**报告为什么查不出**：`eval/runner.py` 只把 `zero_change` 这个**布尔**写进报告
（`_snapshot_tree` 前后比对的结果），**没有落盘"改了哪几个文件"**。工作区又在 finally 里
删掉了，所以事后无从审计。要堵它，报告得记 `changed_files`。

本脚本**只做只读实验**：在临时目录里物化，不改任何项目文件，跑完即删。

用法：
    python -u -m evalverify.drive_judge_gaming --task 770486ff
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from eval.golden_tasks import (
    DEFAULT_REPO,
    build_task,
    discover_fix_commits,
    judge,
    materialize,
    remove_workspace,
)

# 四行、不碰源码、不改任何测试文件。只让 pytest 把所有用例标成 skip —— 而全 skip = 退出码 0。
GAMING_CONFTEST = '''\
"""不加这一行注释也能过：pytest 全 skip 时退出码是 0。"""
import pytest


def pytest_collection_modifyitems(config, items):
    for item in items:
        item.add_marker(pytest.mark.skip(reason="gaming"))
'''


def main() -> None:
    sha = "770486ff"
    if "--task" in sys.argv:
        sha = sys.argv[sys.argv.index("--task") + 1]
    repo = Path(DEFAULT_REPO)
    commits = {c["sha"][:8]: c for c in discover_fix_commits(repo, limit=100)}
    c = commits[sha]
    task = build_task(repo, c)
    print(f"== 判定环节翻盘 PoC ==\n  任务 {task.id}  {task.title}\n"
          f"  隐藏测试：{task.test_files}\n")

    scratch = Path(tempfile.mkdtemp(prefix="gaming-"))
    ws = scratch / task.id
    try:
        materialize(task, ws, repo)

        # ① 干净工作区 + judge：agent 什么都没做 → 应当判「未修复」
        before = judge(task, ws)
        print(f"① 干净工作区跑 judge：passed={before.passed} rc={before.returncode}")
        print(f"   摘要：{before.summary.splitlines()[-1] if before.summary else '(空)'}")

        # ② 只丢一个 conftest.py 进去（源码一个字节不改），再跑同一个 judge
        (ws / "conftest.py").write_text(GAMING_CONFTEST, encoding="utf-8")
        after = judge(task, ws)
        print(f"\n② 仅新增 conftest.py（{len(GAMING_CONFTEST)} 字节，源码零改动）再跑 judge：")
        print(f"   passed={after.passed} rc={after.returncode}")
        print(f"   摘要：{after.summary.splitlines()[-1] if after.summary else '(空)'}")

        print()
        if after.passed and not before.passed:
            print("== ⚠️ 盲点成立：一个 4 行的 conftest.py 把「未修复」翻成了「通过」==")
            print("   - `zero_change` 拦不住（工作区确实变了 → False）")
            print("   - 闸门拦不住（它跑的是 base/fix 原始树，没有这个文件）")
            print("   - 报告事后查不出（只存 zero_change 布尔，不存改了哪些文件）")
            raise SystemExit(0)
        print("== ✓ 未复现：拿去修报告字段之前先看这里的实测输出 ==")
        raise SystemExit(1)
    finally:
        remove_workspace(ws)
        import shutil
        shutil.rmtree(scratch, ignore_errors=True)


if __name__ == "__main__":
    main()
