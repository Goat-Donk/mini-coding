"""single-shot 臂的**离线全量重算**：用修好的抠取器，就着已存盘的 `raw_output` 重抠、重落、重判。

## 这是什么、不是什么

**是**：把模型**当时真实产出的文本**（`raw_output`，已存在报告里）用修好的解析器重新过一遍，
按 `eval/runner.py` 里**一模一样的判定顺序**（物化 → 落补丁 → 快照 → judge → 白送分防御）
重算一遍。**没有调用任何 LLM**，模型一个 token 都没重跑。

**不是**：不是"重跑一遍 single-shot 臂"。模型当时的输出是既成事实；变的只有**我这一侧的解析**。
所以它回答的问题是「**如果解析器没那个 bug，这批产物会被判成什么**」，而不是「模型能打多少分」。

⇒ 引用时两个数字**必须并列**：留档的 52.4% / 78.6%（当时的解析器）与修正的
76.2% / 84.2%（现在的解析器）。只报修正值 = 抹掉自己犯过的错；只报原值 = 拿自己的
bug 当模型的能力。两个都报，并把**差额归因写清楚**，才是诚实的做法。

## 为什么分母变了

`patch_failed` 会被摘出完成率分母（`TaskResult.judged`）。修好解析器后，7 个补丁未落地
变成 2 个 ⇒ 分母从 14 涨到 19。**分子分母同时变**，所以两个百分比都要重新算。

用法：
    python -u -m evalverify.rescore_single_shot > evalverify/rescore_single_shot.log 2>&1
"""
from __future__ import annotations

import copy
import json
import tempfile
from pathlib import Path

from eval.golden_tasks import (
    DEFAULT_REPO,
    build_task,
    discover_fix_commits,
    ensure_repo,
    judge,
    leak_probe,
    materialize,
    remove_workspace,
)
from eval.runner import _apply_patch, _extract_diff, _snapshot_tree
from evalverify.diff_extract_fixed import extract_diff_fixed

RECORDED = Path("data/eval/report-20260912-153044.json")
OUT = Path("evalverify/report_single_shot_rescored.json")
CANDIDATES = 39

#: 从原报告**原样搬运**的顶层字段：它们是"哪把尺子"的自证，重算不该动它们。
CARRIED = (
    "arm", "base_url", "candidates", "gate", "judge_guard", "judge_note", "leak_reachable",
    "leaked_tasks", "mode", "model", "pricing_note", "pricing_snapshot_id", "pricing_warning",
    "repo", "repo_head", "ts", "total_tokens", "total_cost_cny", "total_setup_s",
    "avg_cache_hit_ratio",
)

#: 从原报告**原样搬运**的逐任务字段：这些是"模型当时干了什么"的记录，重算不该动它们。
#: token / 成本是**真花掉的**（那次调用确实发生了），必须保留 —— 修解析器不退款。
PT_CARRIED = (
    "id", "title", "fix_sha", "steps", "tokens", "cost_cny", "duration_s", "setup_s",
    "raw_output", "single_shot_prompt", "budget_exhausted", "leak_reachable",
    "judge_tampering",
)


def effective(passed: bool, error, invalid_reason, patch_failed: bool) -> bool:
    """与 `TaskResult.effective_pass` 逐字同义。"""
    judged = error is None and not patch_failed
    return judged and passed and invalid_reason is None


def rescore_one(task, repo_dir: Path, rec: dict, fixed: bool) -> dict:
    """重跑一个任务（**无 LLM**）。`fixed=False` 时用现行解析器 —— 那是自校验基线。

    判定顺序**逐行照抄** `run_single` 的应用成功分支，顺序不许动
    （尤其：快照必须在 `judge` 之前，`judge` 会往工作区写隐藏测试）。
    """
    entry = {k: rec.get(k) for k in PT_CARRIED}
    raw = rec.get("raw_output") or ""
    diff = extract_diff_fixed(raw) if fixed else _extract_diff(raw)

    error = None
    passed = False
    judge_summary = None
    invalid_reason = None
    zero_change = None
    patch_failed = True
    patch_error = None

    ws = Path(tempfile.mkdtemp(prefix="rescore_"))
    try:
        materialize(task, ws, repo_dir)
        entry["leak_reachable"] = leak_probe(task, ws)
        before = _snapshot_tree(ws)
        if not diff:
            patch_error = "抠不出 unified diff（修正后的抠取器也抠不到）"
        else:
            ok, err = _apply_patch(ws, diff)
            if ok:
                patch_failed = False
                after = _snapshot_tree(ws)
                zero_change = after == before
                try:
                    judged = judge(task, ws)
                    passed = judged.passed
                    judge_summary = judged.summary
                    if judged.error is not None:
                        error = judged.error
                except Exception as exc:
                    error = f"judge 失败: {exc}"
            else:
                patch_error = err
        if patch_failed and patch_error is None:
            patch_error = "补丁未应用"

        # 白送分防御 —— 与 run_single 同序（steps==0 → zero_change → 篡改）。
        if passed and error is None:
            steps = entry.get("steps")
            if steps == 0:
                invalid_reason = "agent 一步都没执行，测试却通过"
            elif zero_change:
                invalid_reason = "agent 没改工作区任何文件，测试却通过"
            elif entry.get("judge_tampering"):
                invalid_reason = "agent 改动了判定相关文件，测试通过不可采信"
            if invalid_reason:
                passed = False
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
    finally:
        remove_workspace(ws)

    entry.update({
        "passed": passed,
        "passed_effective": effective(passed, error, invalid_reason, patch_failed),
        "error": error,
        "invalid_reason": invalid_reason,
        "judge_summary": judge_summary,
        "zero_change": zero_change,
        "patch_failed": patch_failed,
        "patch_error": None if not patch_failed else patch_error,
    })
    return entry


def aggregate(pt: list[dict]) -> dict:
    """与 `_aggregate` 同口径，**从逐任务行独立重算**（不复用报告自报的数）。"""
    judged = sum(1 for r in pt if r["error"] is None and not r["patch_failed"])
    passed = sum(1 for r in pt if r["passed_effective"])
    return {
        "tasks": len(pt),
        "judged": judged,
        "invalid": len(pt) - judged,
        "not_scored": sum(1 for r in pt if r["invalid_reason"]),
        "passed": passed,
        "completion_rate": round(passed / judged, 3) if judged else None,
        "patch_failed": sum(1 for r in pt if r["patch_failed"]),
        "budget_exhausted": sum(
            1 for r in pt if r.get("budget_exhausted") and not r["passed_effective"]),
    }


def main() -> None:
    rec_report = json.loads(RECORDED.read_text(encoding="utf-8"))
    rec_pt = rec_report["per_task"]
    print("== single-shot 离线全量重算（**不调 LLM**）==")
    print(f"  源报告：{RECORDED}")
    print(f"  任务数：{len(rec_pt)}   臂：{rec_report.get('arm')}   模型：{rec_report.get('model')}")
    print()
    print("  ⚠️ 这是**重算**不是**重跑**：模型的输出是既成事实，变的只有解析器。")
    print("     结果回答「如果解析器没这个 bug，这批产物会被判成什么」，")
    print("     而不是「模型能打多少分」。两个数字必须并列引用。")
    print()

    repo_dir = ensure_repo(DEFAULT_REPO)
    by_sha: dict[str, object] = {}
    for c in discover_fix_commits(repo_dir, limit=CANDIDATES):
        try:
            t = build_task(repo_dir, c)
        except Exception:
            continue
        by_sha[t.fix_sha] = t

    # ---- 自校验：先用**现行**解析器重算一遍，必须复现留档数字 ----
    # 不复现就说明这套重算流程本身有问题，那么"修正后"的数字也不能信。
    print("--- 0. 自校验：用现行解析器重算，必须复现留档数字 ---\n")
    base_pt = []
    for r in rec_pt:
        t = by_sha.get(r["fix_sha"])
        base_pt.append(rescore_one(t, repo_dir, r, fixed=False) if t else dict(r))
    base = aggregate(base_pt)
    rec_agg = aggregate(rec_pt)
    ok = True
    for k in ("tasks", "judged", "passed", "patch_failed", "not_scored"):
        same = base[k] == rec_agg[k]
        ok &= same
        print(f"  {'✓' if same else '✗'} {k:<14} 重算 {base[k]!r:<8} 留档 {rec_agg[k]!r}")
    if not ok:
        print("\n  ✗ 自校验失败 —— 重算流程本身有问题，修正后的数字也不能信。终止。")
        raise SystemExit(1)
    print("\n  ✓ 逐字段复现 —— 重算流程与当时的 runner 同义，修正值有可信基础\n")

    # ---- 修正版 ----
    print("--- 1. 修正版重算（修好的抠取器）---\n")
    fixed_pt = []
    for r in rec_pt:
        t = by_sha.get(r["fix_sha"])
        fixed_pt.append(rescore_one(t, repo_dir, r, fixed=True) if t else dict(r))
    new = aggregate(fixed_pt)

    print(f"  {'任务':<10} {'留档':<16} {'修正后':<16}")
    print("  " + "-" * 44)
    for a, b in zip(rec_pt, fixed_pt):
        def cell(x):
            if x.get("patch_failed"):
                return "补丁未应用"
            if x["error"]:
                return "判定无效"
            if x["invalid_reason"]:
                return "不计分"
            return "✓ 通过" if x["passed_effective"] else "✗ 没修对"
        ca, cb = cell(a), cell(b)
        mark = "  ← 变了" if ca != cb else ""
        print(f"  {a['id']:<10} {ca:<16} {cb:<16}{mark}")
        if a.get("patch_failed") and not b.get("patch_failed"):
            print(f"      ↑ 原先：{(a.get('patch_error') or '')[:76]}")

    print()
    print("== 汇总 ==")
    for label, s in (("留档（当时的解析器）", rec_agg), ("修正（修好的解析器）", new)):
        rate = f"{s['completion_rate']:.1%}" if s["completion_rate"] is not None else "N/A"
        print(f"  {label:<22} passed {s['passed']:2d}/{s['tasks']}   judged {s['judged']:2d}"
              f"   patch_failed {s['patch_failed']}   朴素 {s['passed']/s['tasks']:.1%}"
              f"   能力 {rate}")

    out = {k: copy.deepcopy(rec_report.get(k)) for k in CARRIED}
    out.update(new)
    out["per_task"] = fixed_pt
    out["rescored"] = {
        "from": str(RECORDED),
        "by": "evalverify/rescore_single_shot.py",
        "why": "eval/runner.py 的 _FENCE_RE 围栏配对错位，single-shot 的 diff 块抠不出来",
        "llm_reruns": 0,
        "recorded_aggregate": {k: rec_agg[k] for k in
                               ("tasks", "judged", "passed", "patch_failed", "not_scored")},
        "note": "重算值，非重跑值。与留档数字必须并列引用。",
    }
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n  → 修正版报告已写入 {OUT}")
    print("     （顶层字段与逐任务字段沿用原报告；只重算了判定结果）")

    # ---- 归因：修正回来的任务，是"我解析器"还是别的 ----
    print("\n== 归因（差分出来的每个任务，逐个查清责任方）==")
    for a, b in zip(rec_pt, fixed_pt):
        if a.get("patch_failed") and not b.get("patch_failed"):
            verdict = ("✓ 通过" if b["passed_effective"] else "✗ 落盘了但测试不过")
            print(f"  {a['id']}  我的解析器（围栏错位）→ 补丁应用成功 → {verdict}")
    for a, b in zip(rec_pt, fixed_pt):
        if a.get("patch_failed") and b.get("patch_failed"):
            print(f"  {a['id']}  **不是解析器**（修正后仍抠不出/仍应用不上）→ "
                  f"{(b.get('patch_error') or '')[:64]}")

    print("\n== 已知局限 ==")
    print("  1. 重算值不是重跑值：模型当时的输出是既成事实，改的只有解析侧。")
    print("  2. 判分仍用**同一个裁判器**，conftest.py 注入绕过口径不变（本轮三臂统一关闭守卫）。")
    print("  3. 任务重建靠 `fix_sha` 匹配；重建不出的任务按原样保留，不当作成功。")
    print("  4. 报告里的 `tokens` / `cost_cny` / `steps` 从原报告搬运 —— 那次调用真发生过，")
    print("     修解析器不退款，成本口径不受影响。")


if __name__ == "__main__":
    main()
