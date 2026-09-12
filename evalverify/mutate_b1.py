"""B1 变异测试（牙齿检查）：逐个打断 `_extract_diff` 的新不变式与尺子指纹，
确认对应测试**真的会变红**。

用法：python evalverify/mutate_b1.py

背景：B1 修的是"围栏错位"—— `_FENCE_RE` 的开围栏原本只认裸围栏 / `diff` / `patch`，
于是 diff 块前面那个 ```` ```python ```` 块让**配对整个错位一格**，真补丁抠不出来，
兜底分支还把补丁后面的散文整段喂给 `git apply`。留档 7 个 `patch_failed` 里 5 个是它。

单测全绿只证明"代码现在能跑通"，不证明"测试能抓住这个机制被改坏"。
每个变异体只改一处源码 → 只跑那一条目标测试 → 还原。
**仍绿 = 这条测试没抓住这个机制，等于白写。**

B1 的失效模式分四类，**没有一种是崩溃型的**：

1. **配对退回原样**（开围栏又只认 diff/patch）—— 只影响"diff 块前面有别的围栏"
   这一种真实形状，其余输入一字不差。
2. **兜底没有终点**（从 `diff --git` 一路切到全文结尾）—— 抠出来的东西**更长**，
   看起来"抠到了更多内容"，实际上把散文混进去了。
3. **判据放宽成"什么块都要"**（`_is_diff_block` 恒真）—— 会把 `python` 分析块
   当成补丁，方向从"抠不到"变成"抠错"。
4. **尺子指纹失真** —— 报告看起来字段俱全，但哈希对的是错的文件 / 少了决定判定的
   那一项 / 把"量不到"写成空串。这一类比前三类更隐蔽：字段在、值也在，只是**不是**
   它声称的那个东西。

`--recount` 那个参数不在这里变异：它属于 `_apply_patch`，B1 没动。
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASETEMP = "D:/RAG项目/coding_agent/.pytest_tmp/mut_b1"

RUNNER = "eval/runner.py"
T = "tests/test_eval_runner.py"

#: 这几段在源码里是逐字的，改源码时必须同步改这里（锚点找不到会报 SKIP，
#: 不会静默跳过 —— SKIP 与 MISS 一样按失败看）。
_FENCE_ANY = 'r"```[^\\n]*\\n(.*?)```"'
_FENCE_OLD = 'r"```(?:diff|patch)?[ \\t]*\\n(.*?)```"'

_FALLBACK_TAIL = """    tail = text[idx:]
    stop = _BARE_FENCE_RE.search(tail)  # 补丁后面第一个裸围栏就是它的尽头
    if stop:
        tail = tail[: stop.start()]
    return tail.strip() + "\\n\""""

_FALLBACK_OLD = """    tail = text[idx:]
    return tail.strip() + "\\n\""""

MUTATIONS = [
    # ---------- 1. 配对退回原样 ----------
    #
    # 目标测试是"补丁前后都有带 info string 的围栏"那条，**不是**归档夹具那两条。
    # 原因是一个实测出来的事实：对归档的 5 个假阴性，两处修复是**两条等效的路** ——
    # 它们的补丁后面跟的正好是**裸**围栏，所以只修兜底也能抠对（第一版变异把目标
    # 指向归档夹具，得到 MISS，那不是"测试没牙"，是**变异体与测试覆盖的形状不同**）。
    # 配对这条修复只在"补丁后面跟着带 info string 的围栏"时才不可替代，
    # 所以变异必须打在那个形状上。重叠本身写进了那条测试的 docstring。
    (
        "开围栏又只认裸围栏 / diff / patch（配对整体错位一格）",
        RUNNER,
        _FENCE_ANY,
        _FENCE_OLD,
        f"{T}::test_extract_diff_marker_mentioned_in_prose_before_the_fence",
    ),
    # ---------- 2. 兜底没有终点 ----------
    #
    # 只有一条，且**不能**拿归档夹具来打：配对修好之后，那 5 个假阴性**根本走不到
    # 兜底分支**（`findall` 直接抓到了补丁块，兜底压根不执行）。第一版变异把归档夹具
    # 也列成一条，得到 MISS —— 那不是"测试没牙"，是**这个变异体在那个形状上不可达**。
    # 兜底真正被用到的形状是"压根没有围栏、直接贴 diff"，所以打在那里。
    # 两处修复的重叠关系与鉴别条件写在
    # `test_extract_diff_marker_mentioned_in_prose_before_the_fence` 的 docstring 里。
    (
        "兜底一路切到全文结尾（散文混进补丁）",
        RUNNER,
        _FALLBACK_TAIL,
        _FALLBACK_OLD,
        f"{T}::test_extract_diff_bare_fallback_stops_at_the_first_bare_fence",
    ),
    # ---------- 3. 判据放宽成"什么块都要" ----------
    (
        "`_is_diff_block` 恒真（python 分析块也会被当成补丁）",
        RUNNER,
        '    return "diff --git" in block or block.lstrip().startswith("---")\n',
        "    return True\n",
        f"{T}::test_extract_diff_still_ignores_non_diff_fences",
    ),
    (
        "多块时取第一个而不是最长（示意块会顶掉真补丁）",
        RUNNER,
        "        return max(blocks, key=len).strip() + \"\\n\"\n",
        "        return blocks[0].strip() + \"\\n\"\n",
        f"{T}::test_extract_diff_picks_the_longest_diff_block",
    ),
    # ---------- 4. 尺子指纹失真 ----------
    (
        "指纹少一项（pytest 版本 —— 决定 judge 结论的那一项没记）",
        RUNNER,
        '        "pytest_version": pytest.__version__,\n',
        "",
        f"{T}::test_report_carries_the_ruler_fingerprint",
    ),
    (
        "两项哈希算成同一个文件（配对断掉，字段俱全但指错东西）",
        RUNNER,
        '        "golden_tasks_sha256": _file_sha256("eval/golden_tasks.py"),\n',
        '        "golden_tasks_sha256": _file_sha256("eval/runner.py"),\n',
        f"{T}::test_report_carries_the_ruler_fingerprint",
    ),
    (
        "哈希截断长度改变（与文档/复核脚本约定的 12 位不一致）",
        RUNNER,
        "        return hashlib.sha256((_PROJECT_ROOT / rel).read_bytes()).hexdigest()[:12]\n",
        "        return hashlib.sha256((_PROJECT_ROOT / rel).read_bytes()).hexdigest()\n",
        f"{T}::test_report_carries_the_ruler_fingerprint",
    ),
    (
        '"量不到"写成空串（读起来像"文件是空的"，那是另一回事）',
        RUNNER,
        "    except OSError:\n        return None\n",
        '    except OSError:\n        return ""\n',
        f"{T}::test_file_sha256_reports_unreadable_as_none_not_empty",
    ),
]


def main() -> int:
    results = []
    for label, rel, old, new, target in MUTATIONS:
        path = ROOT / rel
        original = path.read_text(encoding="utf-8")
        if old not in original:
            results.append((label, "跳过：锚点没找到（代码可能已变）"))
            print(f"SKIP  锚点没找到  {label}", flush=True)
            continue
        path.write_text(original.replace(old, new, 1), encoding="utf-8")
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
            path.write_text(original, encoding="utf-8")
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
