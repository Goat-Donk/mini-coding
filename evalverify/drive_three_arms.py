"""三臂大对照：`agent`（多轮循环）· `one-step`（只差 max_steps=1）· `single-shot`（无工具一次调用）。

和 `drive_arm_compare.py` 的分工：那个是**单臂深查**（口径对账、闸门逐条对、盲点取证），
这个是**三臂横比**。横比有自己的坑，所以单独一个脚本：

1. **可比性的前提是"同一把尺子"**。三份报告的闸门结果、有效任务集、模型、定价快照
   必须逐字段对上，否则表做得再漂亮也是拿三把尺子量出来的。所以**先验可比性，再出数**，
   有一项对不上就打红并把该行放最前面 —— 不给"看起来很美"的表。
2. **`budget_exhausted` 必须分开列**。「撞 max_steps」和「修不对」在完成率里长得一样，
   但归因完全相反。有精确字段就用，没有就**推定并标注**（agent 臂跑在加字段之前）。
3. **`patch_failed` 不能混进完成率**（single-shot 专属）：补丁没应用上是"我的解析器/apply
   失败"，不是"模型没修好"。分母只算成功应用的补丁，`patch_failed` 印在旁边。
4. **成本要带分母**：`total_cost_cny` 覆盖全部任务（含无效判定），完成率分母不含 —— 两个数
   摆在一起才是完整的，单说一个必被误读。另外给出**每次成功修复的边际成本**。
5. **`single-shot` 要多一列「修正版」**。它的完成率被一个**解析器 bug**污染过（`_FENCE_RE`
   围栏配对错位，7 个补丁没抠出来，其中 5 个是我的锅）。`runner.py` 已冻结不能改，
   所以走离线重算（`evalverify/rescore_single_shot.py`，零 LLM）另出一份报告。
   ⇒ 表里**两列并列**：`single-shot`（留档）+ `single-shot＊`（修正）。
   只报修正值 = 抹掉自己犯过的错；只报原值 = 拿自己的 bug 当模型的能力。
6. **配对 > 聚合**。两条臂的 84.2% 与 66.7% 分母不同（19 vs 21），相减毫无意义。
   必须落到**同一批任务**上逐个看翻转，再把翻转**按原因分类**——否则"多轮循环值多少"
   和"评测器偏差"会搅成一个数。

只 `print`，日志靠 shell 重定向。

用法：
    python -u -m evalverify.drive_three_arms                  # 自动取每条臂最新的报告
    python -u -m evalverify.drive_three_arms <a.json> <o.json> <s.json>
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from evalverify.diff_extract_fixed import extract_diff_fixed
from evalverify.drive_arm_compare import (
    ARM_MAX_STEPS,
    GATE_ANCHORS,
    JUDGE_DISCLAIMER,
    LIMITATIONS,
    _recompute,
    budget_hits,
)

ARMS = ("agent", "one-step", "single-shot")
REPORT_DIR = Path("data/eval")

#: 修正版 single-shot 的离线重算报告（由 `evalverify/rescore_single_shot.py` 产出）。
#: 用带星号的键是为了**在每一张表里都显眼**——它跟另外三列不是同一批产物，绝不能混读。
RESCORED_KEY = "single-shot＊"
RESCORED_PATH = Path("evalverify/report_single_shot_rescored.json")

#: 表里出现的全部列，顺序即显示顺序。修正版也**必须**过可比性校验（见 `main`）。
ALL_COLUMNS = (*ARMS, RESCORED_KEY)


def _repo_of(report: dict) -> str:
    """报告里 `repo` 字段的**末段**（`eval\\repos\\sqlparse` → `sqlparse`）。

    报告用的是平台上当时的路径分隔符，所以按两种分隔符各切一次，别只看 `/`。
    """
    raw = str(report.get("repo") or "")
    return raw.replace("\\", "/").rstrip("/").split("/")[-1]


def _latest_reports(repo: str | None = None) -> dict[str, Path]:
    """每条臂取**最新**的那份报告（按文件名里的时间戳，不是 mtime）。

    `repo` 给定时只收该仓的报告 —— 留档的 tinydb 报告永远比新仓新，
    不过滤的话第二仓跑完还是会把 tinydb 挑出来。
    """
    found: dict[str, Path] = {}
    for p in sorted(REPORT_DIR.glob("report-*.json")):
        try:
            r = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        arm = r.get("arm")
        if arm in ARMS and (repo is None or _repo_of(r) == repo):
            found[arm] = p          # 排序后逐个覆盖 ⇒ 留下的是时间戳最大的
    return found


def _cell(r: dict) -> str:
    """一个任务在某条臂上的单元格。带 `!` 后缀 = 撞了 max_steps（「没预算」不是「修不对」）。"""
    tag = {True: "✓", False: "✗"}.get(r.get("passed_effective"))
    if tag is None:
        return "?"
    if not r["passed_effective"] and r.get("budget_exhausted"):
        return tag + "!"            # 撞预算
    return tag


def _compat(
    arms: dict[str, dict],
) -> tuple[list[tuple[str, bool, str]], list[str], list[str]]:
    """可比性校验：**逐项**列出用户点名的六项，再给结论。

    返回 `(逐项结果, 硬失败明细, 非致命说明)`。为什么先验这个：三条臂的完成率只有在
    **同一把尺子**量出来时才能互相比。模型换了、仓库 HEAD 变了、闸门放行了不同的任务集
    —— 任何一项不同，那张对比表就是在拿三把尺子量三个东西，数字再漂亮也没有意义。

    ⚠️ 这一节不通过时，输出里必须出现「以下数字不要引用」这句话（用户逐字要求）。

    **两个返回通道是分开的（三态纪律）**：
      - `problems` = 尺子确实不一样 → 出数无意义，终止；
      - `notes` = **没得比**（比如本仓本候选数没有留档外锚）→ 只是说明，不是失败。
    把后者读成前者，就会对第二个仓库放一张**假 ✗**，那比不检查更糟 —— 它教人
    忽略这个警告。
    """
    present = [a for a in ALL_COLUMNS if a in arms]
    if len(present) < 2:
        return [], [f"只有 {len(present)} 条臂的报告（{present}），横比无从谈起"], []

    ref_name = present[0]
    ref = arms[ref_name]
    ref_ids = sorted(r["id"] for r in ref["per_task"])
    details: list[str] = []
    notes: list[str] = []

    def cmp_field(label: str, get, extra: str = "") -> tuple[str, bool, str]:
        values = {a: get(arms[a]) for a in present}
        bad = {a: v for a, v in values.items() if v != values[ref_name]}
        ok = not bad
        if not ok:
            details.append(f"{label} 不一致：" +
                           "，".join(f"{a}={v!r}" for a, v in bad.items()) +
                           f"（基准 {ref_name}={values[ref_name]!r}）")
        shown = values[ref_name] if ok else "✗ 见下"
        return (label, ok, f"{shown}{extra}")

    def ids_of(a: str) -> list[str]:
        return sorted(r["id"] for r in arms[a]["per_task"])

    def gate_of(rep: dict, k: str):
        """收的是**报告**不是臂名 —— 和 `cmp_field` 的 `get(arms[a])` 保持一致。
        第一版这里收臂名、`cmp_field` 却把报告传进来，于是 `arms[报告]` → unhashable
        （dict 不能当键）。冒烟测试当场炸出来了。"""
        return (rep.get("gate") or {}).get(k)

    def ids_check() -> tuple[str, bool, str]:
        bad = {a: ids_of(a) for a in present if ids_of(a) != ref_ids}
        ok = not bad
        if not ok:
            for a, ids in bad.items():
                only_a = sorted(set(ids) - set(ref_ids))
                only_r = sorted(set(ref_ids) - set(ids))
                details.append(f"有效任务集不一致：{a} 独有 {only_a}，"
                               f"{ref_name} 独有 {only_r}")
            # 任务集不同，下面逐任务比 id 就没意义了：直接判这一项失败。
        return ("有效任务集（闸门放行了哪些任务）", ok,
                f"{len(ref_ids)} 个" if ok else "✗ 见下")

    checks = [
        # 仓库本身也要互相比 —— `repo_head` 相等理论上不能排除两个不同的仓，
        # 而"同一个仓"是下面每一列的前提。2026-09-13 补上（多仓改造时发现的缺口）。
        cmp_field("仓库（repo）", lambda x: _repo_of(x)),
        cmp_field("模型（model）", lambda x: x.get("model")),
        cmp_field("仓库 HEAD（repo_head）", lambda x: (x.get("repo_head") or "")[:12]),
        cmp_field("定价快照（pricing_snapshot_id）", lambda x: x.get("pricing_snapshot_id")),
        cmp_field("闸门有效数（gate.valid）", lambda x: gate_of(x, "valid")),
        ids_check(),
        cmp_field("候选集大小（candidates）", lambda x: x.get("candidates")),
    ]
    # 闸门必须开着 —— 关了闸门的那条臂跟前两条不是一回事。
    for a in present:
        if gate_of(arms[a], "enabled") is False:
            details.append(f"{a} 没开闸门（--no-validate）—— 与基线不可比")

    # ---- 外锚：与**留档的闸门日志**比（不是与自己比）----
    # 键是 (仓, 候选数)：闸门确定性，但基线逐轮留档，换 `--limit` 就是换尺子。
    # 查不到出处的组合**不判 ✗**，只记一条说明（三态纪律：没检查 ≠ 检查没过）。
    repo = _repo_of(arms[ref_name])
    cands = arms[ref_name].get("candidates")
    anchor = GATE_ANCHORS.get((repo, cands))
    if anchor is None:
        notes.append(
            f"本仓本候选数（{repo} · 候选 {cands}）**没有留档的闸门日志**作外部锚 —— "
            f"这一项是「未检查」，不是「不通过」。该轮的可信度改由下面的跨臂互证承担："
            f"三条臂各自独立跑闸门，得到逐个一致、顺序一致的同一批任务集。"
        )
    else:
        for a in present:
            if gate_of(arms[a], "valid") != anchor["valid"]:
                details.append(
                    f"{a} 的闸门有效数 {gate_of(arms[a], 'valid')} "
                    f"≠ 留档基线 {anchor['valid']}（出处：{anchor['provenance']}）"
                )
        notes.append(
            f"外锚已比对：{repo} · 候选 {cands} → 有效 {anchor['valid']} / "
            f"拒 {anchor['rejected']}（出处：{anchor['provenance']}）"
        )
    return checks, details, notes


def _arm_summary(report: dict) -> dict:
    pt = report["per_task"]
    mine = _recompute(pt)
    judged = mine["judged"]
    passed = mine["passed"]
    hits, budget_precise = budget_hits(report, pt)
    budget = len(hits)
    budget_fail = sum(1 for r in pt
                      if r["id"] in set(hits) and not r["passed_effective"])
    patch_failed = mine["patch_failed"]
    # ⚠️ 分母 `judged` **已经**把 `patch_failed` 摘掉了（见 TaskResult.judged）。
    # 这里最初又减了一次，于是 single-shot 打出「计分 18 − 补丁未应用 3 = 真实分母 15」——
    # 同一批任务被扣了两遍。分母就是 `judged`，没有第二个数。
    # 被摘出分母的恰好两类：补丁没落地 / 判定器没跑成。它们是**并集**不是划分，
    # 理论上可重叠（补丁失败又碰上判定器挂），所以重叠数要单独印出来，不能假装相加等于总数。
    error_n = sum(1 for r in pt if r["error"])
    excluded = mine["tasks"] - judged
    overlap = error_n + patch_failed - excluded
    # 「纯模型能力」的分母要再剥一层：D1 = 撞 max_steps。它已经落在 `judged` 里
    # （预算耗尽的任务判定器是跑通了的：agent 没修好、测试就不过），但它是**预算**的锅。
    buckets = classify(pt)
    d1 = [i for i in buckets["tests_failed"] if i in set(hits)]
    cost = report.get("total_cost_cny") or 0.0
    return {
        **mine,
        "budget": budget,
        "budget_fail": budget_fail,
        "budget_precise": budget_precise,
        "error_n": error_n,
        "excluded": excluded,
        "overlap": overlap,
        "d1": len(d1),
        "d1_ids": d1,
        "d2": len(buckets["tests_failed"]) - len(d1),
        "capability_denom": judged - len(d1),
        "cost": cost,
        "cost_per_solve": round(cost / passed, 4) if passed else None,
        "tokens": report.get("total_tokens"),
        "oracle": ((report.get("gate") or {}).get("oracle_baseline")),
    }


def classify(pt: list[dict]) -> dict[str, list[str]]:
    """把每个任务归入**恰好一类**（分区，不重复计数）。

    用户明确要求「不要只给一个百分比」。理由是对的：一个 67% 里面藏着四种完全不同的
    失败，而它们的**含义相反**：
    - `patch_failed`：补丁没落地。这是**我这边**（解析器 / git apply）的锅，不是模型的锅，
      所以它被摘出分母 —— 把它算成"模型没修好"等于用自己的 bug 给模型扣分。
    - `judge_error`：判定器没跑成。也不许冤枉 agent（`TaskResult.judged` 的注释写了）。
    - `not_scored`：白送分 / 零变更 / 零步 → 这个任务压根没测到东西。**这是筛子的锅，不是模型的**。
    - `tests_failed`：判定器跑通了、补丁落地了、测试就是不过 —— **只有这一类才是"真没修对"**。

    归类顺序是硬的：error → patch_failed → not_scored → passed → tests_failed。
    反过来的话，一个"判定器挂了"的任务会因为 `passed=False` 被算进 tests_failed，
    把基础设施的故障记成模型的能力问题。
    """
    buckets: dict[str, list[str]] = {
        "judge_error": [], "patch_failed": [], "not_scored": [],
        "passed": [], "tests_failed": [],
    }
    for r in pt:
        tid = r["id"]
        if r["error"]:
            buckets["judge_error"].append(tid)
        elif r.get("patch_failed"):
            buckets["patch_failed"].append(tid)
        elif r["invalid_reason"]:
            buckets["not_scored"].append(tid)
        elif r["passed_effective"]:
            buckets["passed"].append(tid)
        else:
            buckets["tests_failed"].append(tid)
    return buckets


def _failures(arms: dict[str, dict], present: list[str]) -> None:
    print()
    print("--- 1b. 失败分类（每个任务恰好归一类；**不做「一个百分比」**）---")
    print()
    print("  A patch_failed   补丁未应用 —— **我这边**的锅（解析器/apply），已摘出分母，不赖模型")
    print("  B judge_error    判定器没跑成（pytest rc 2/3/4/5）—— 同样不赖模型，已摘出分母")
    print("  C not_scored     不计分（白送分/零变更/零步/判定篡改）—— **筛子**的锅")
    print("  D tests_failed   判定器跑通、补丁落地、测试就是不过 —— **只有这一类是真没修对**")
    print("  E passed         通过")
    print("  （`env_timeout` **取证不到**，不在此列 —— 见开头局限声明第 6 条，"
          "不拿猜测填格子）")
    print()

    tables = {a: classify(arms[a]["per_task"]) for a in present}
    keys = ["passed", "tests_failed", "patch_failed", "judge_error", "not_scored"]
    head = "  " + "分类".ljust(26) + "".join(a.ljust(14) for a in present)
    print(head)
    print("  " + "-" * (len(head) - 2))
    labels = {
        "passed": "E passed（通过）",
        "tests_failed": "D tests_failed（真没修对）",
        "patch_failed": "A patch_failed（补丁没落地）",
        "judge_error": "B judge_error（判定器挂）",
        "not_scored": "C not_scored（没测到）",
    }
    for k in keys:
        line = f"  {labels[k]:<26}" + "".join(str(len(tables[a][k])).ljust(14) for a in present)
        print(line)
    print("  " + "-" * (len(head) - 2))
    tot = "  " + "合计（= 有效任务数）".ljust(26) + "".join(
        str(sum(len(v) for v in tables[a].values())).ljust(14) for a in present)
    print(tot)

    for a in present:
        for k in keys:
            ids = tables[a][k]
            if ids and k != "passed":
                print(f"    [{a}] {labels[k]} → {ids}")

    # D 的细分：撞预算 vs 有预算但没修对。**叠加在 D 上，不是并列的一格** ——
    # 撞预算的任务本来就属于"D 真没修对"，它只是 D 的一个**可解释的子集**。
    print()
    print("  --- D（真没修对）的进一步细分：是「没预算」还是「有预算但没修对」---")
    for a in present:
        hits, precise = budget_hits(arms[a], arms[a]["per_task"])
        ids = tables[a]["tests_failed"]
        d1 = [i for i in ids if i in set(hits)]
        d2 = [i for i in ids if i not in set(hits)]
        tag = "精确" if precise else "**推定**"
        print(f"    [{a}] D1 撞 max_steps（{tag}）{len(d1)} 个 {d1}")
        print(f"    [{a}] D2 预算够但没修对        {len(d2)} 个 {d2}")
        if not precise:
            print(f"           ↑ D1 是**推定**的（报告无 budget_exhausted 字段）——"
                  f"引用时必须写明，模型在最后一步收工也会落进来")
    print()
    print("  ⚠️ D1 是 D 的**子集**，不是并列的第五类。把 D1 和 D 相加 = 重复计数。")


def _why_ss_failed(rep: dict, tid: str) -> str:
    """single-shot 在一个任务上失败的原因（粗分三档：输出合规 / 基础设施 / 纯能力）。"""
    r = next((x for x in rep["per_task"] if x["id"] == tid), None)
    if r is None:
        return "缺数据"
    if r.get("patch_failed"):
        # 用**修好的**抠取器再判一次：抠得出来但应用不上 = 模型编了不存在的上下文，
        # 那是模型的输出合规问题，不是我的解析器。抠不出来 = 模型压根没输出 diff。
        d = extract_diff_fixed(r.get("raw_output") or "")
        return ("输出合规：一个 unified diff 都没输出" if not d
                else "输出合规：补丁抠出来了但应用不上（上下文对不上）")
    if r["error"]:
        return "基础设施：判定器故障"
    if r["invalid_reason"]:
        return "基础设施：不计分"
    return "纯能力：补丁落地了、测试就是不过"


def _why_agent_failed(arms: dict[str, dict], tid: str) -> str:
    r = next((x for x in arms["agent"]["per_task"] if x["id"] == tid), None)
    if r is None:
        return "缺数据"
    hits, precise = budget_hits(arms["agent"], arms["agent"]["per_task"])
    if tid in set(hits):
        return (f"预算：撞满 max_steps={ARM_MAX_STEPS['agent']}"
                f"（{'精确' if precise else '**推定**'}）")
    if r["error"]:
        return "基础设施：判定器故障"
    if r["invalid_reason"]:
        return "基础设施：不计分"
    return "纯能力：预算够、补丁落地、测试就是不过"


def _paired(arms: dict[str, dict], present: list[str]) -> None:
    """配对分析：把「完成率」这个聚合数字拆回**逐任务翻转**。

    用户点名要的那件事：agent 的 85.7% 与 single-shot 的 84.2% 到底差在哪里。
    直接比两个百分比是错的 —— 它们的**分母不同**（19 vs 21，因为 patch_failed 被摘出分母），
    而且 85.7% 当时算的是**并集**不是 agent 单臂。所以这里先统一分母，再逐任务四格摊开。
    """
    print("\n--- 2a. 全局同分母速览 ---\n")
    S = {a: _arm_summary(arms[a]) for a in present}
    # ⚠️ 分母**从报告里算**，不写死（2026-09-13 修）。
    # 原先这里写死的是 tinydb 那一轮的 21；换到 sqlparse（有效任务 19）之后，
    # **同一份输出里自相矛盾**：2a 打「5/21 = 23.8%」、2b 打「5/19 = 26.3%」。
    # 分母写死过一次就会写死第二次，所以这几处一律从数据取。
    n_tasks = max(S[a]["tasks"] for a in present) if present else 0
    judged_txt = " / ".join(str(S[a]["judged"]) for a in present)
    print(f"  完成率是**分数**：分母不同（judged {judged_txt}）时相减毫无意义。")
    print(f"  先把四条列摆到同一个「有效任务数 {n_tasks}」下面，再谈配对。\n")
    head = "  " + "口径".ljust(32) + "".join(a.ljust(16) for a in present)
    print(head)
    print("  " + "-" * (len(head) - 2))

    def row(label, fn, width=32):
        print(f"  {label:<{width}}" + "".join(str(fn(a)).ljust(16) for a in present))

    row("有效任务数（共同分母）", lambda a: S[a]["tasks"])
    row("通过 passed", lambda a: S[a]["passed"])
    row(f"→ passed / {n_tasks}（朴素）",
        lambda a: f"{S[a]['passed'] / S[a]['tasks']:.1%}")
    row("→ passed / judged（能力）",
        lambda a: (f"{S[a]['completion_rate']:.1%}"
                   if S[a]["completion_rate"] is not None else "N/A"))
    row("→ passed / (judged − 撞预算)",
        lambda a: (f"{S[a]['passed'] / S[a]['capability_denom']:.1%}"
                   if S[a]["capability_denom"] > 0 else "N/A"))
    row("judged（能力分母）", lambda a: S[a]["judged"])

    if "agent" in arms:
        pa = {r["id"] for r in arms["agent"]["per_task"] if r["passed_effective"]}
        for other in [a for a in present if a != "agent"]:
            pb = {r["id"] for r in arms[other]["per_task"] if r["passed_effective"]}
            # 并集的分母 = 两条臂**共同覆盖**的任务数（同样不写死，理由见本节开头）
            n_union = len({r["id"] for r in arms["agent"]["per_task"]}
                          | {r["id"] for r in arms[other]["per_task"]})
            print(f"\n  **并集（任一臂修好）** agent ∪ {other} = {len(pa | pb)}/{n_union} "
                  f"= {len(pa | pb) / n_union:.1%}")
            print(f"     ↑ 这是「两条通路合起来能覆盖多少」，**不是**任何单臂的成绩 ——")
            print(f"       它常被误记成「agent 的成绩」，引用时必须写明是并集。")

    print("\n--- 2b. 配对四格表（同一批任务逐个比）---\n")
    if "agent" not in arms:
        print("  （缺 agent 报告，跳过）")
        return
    A = {r["id"]: r for r in arms["agent"]["per_task"]}
    pa = {i for i, r in A.items() if r["passed_effective"]}
    for other in [a for a in present if a != "agent"]:
        B = {r["id"]: r for r in arms[other]["per_task"]}
        pb = {i for i, r in B.items() if r["passed_effective"]}
        ids = set(A) & set(B)
        both, neither = sorted(pa & pb), sorted(ids - pa - pb)
        only_a, only_b = sorted(pa - pb), sorted(pb - pa)
        print(f"  【agent vs {other}】")
        print(f"    都修好                 {len(both):2d}  {both}")
        print(f"    只有 agent 能修好      {len(only_a):2d}  {only_a}")
        print(f"    只有 {other:<14} 能修好 {len(only_b):2d}  {only_b}")
        print(f"    都没修好               {len(neither):2d}  {neither}")
        print(f"    ── 并集 {len(pa | pb)}/{len(ids)} = {len(pa | pb) / len(ids):.1%}")
        print()


def _decouple(arms: dict[str, dict], present: list[str]) -> None:
    """**解耦**：把「多轮循环的收益」与「评测器偏差」彻底拆开。

    这是本轮最容易出错的一步。如果不拆，`single-shot＊` 84.2% vs `agent` 66.7% 会被读成
    「不用循环更好」—— 但那个差里混着三样**完全不相干**的东西：

    1. **评测器偏差**（我的 `_FENCE_RE`）：只压低 single-shot，与循环无关。
    2. **输出合规**：single-shot 一次成文、没有工具、没有重试机会 —— 格式错了就是错了。
       agent 有 25 步可以改，天然对格式失误更宽容。这是**架构属性**，不是能力差。
    3. **预算**：agent 撞满 25 步就停，single-shot 没有预算这回事。这也是**架构属性**。

    只有第 4 类才是真正的**能力差异**：双方都有机会、都落了补丁、就是测试过不过。
    所以本节按这四个来源把翻转逐个归类，**每一类都不许混进别人的账**。
    """
    print("\n--- 2c. 解耦：多轮循环的收益 vs 评测器偏差 ---\n")
    if "agent" not in arms:
        print("  （缺 agent 报告，跳过）")
        return
    corrected = RESCORED_KEY if RESCORED_KEY in arms else "single-shot"
    if corrected not in arms:
        print("  （缺 single-shot 报告，跳过）")
        return

    print(f"  比较对：agent（多轮循环 + 工具，max_steps={ARM_MAX_STEPS['agent']}）")
    print(f"          vs {corrected}（无工具、一次调用、直出 diff）")
    print()
    print("  三者必须分开算，否则会互相冒充：")
    print("    ① 评测器偏差 —— 我的解析器 bug，影响的是**历史留档数字**，不是模型")
    print("    ② 架构属性   —— 输出合规（有无重试机会）· 预算（有无步数上限）")
    print("    ③ 纯能力差异 —— 双方都落了补丁、就是测试过不过")
    print()

    pa = {r["id"] for r in arms["agent"]["per_task"] if r["passed_effective"]}
    pb = {r["id"] for r in arms[corrected]["per_task"] if r["passed_effective"]}
    only_a, only_b = sorted(pa - pb), sorted(pb - pa)

    print(f"  ① 评测器偏差（独立核算，不进上面任何一格）")
    rec = arms.get("single-shot")
    if rec is not None and corrected != "single-shot":
        recp = {r["id"] for r in rec["per_task"] if r["passed_effective"]}
        n_union = len({r["id"] for r in arms["agent"]["per_task"]}
                      | {r["id"] for r in rec["per_task"]})
        print(f"     single-shot 留档 {len(recp)} → 修正 {len(pb)}"
              f"（+{len(pb) - len(recp)}，全部来自我修好的抠取器）")
        print(f"     并集：留档 {len(pa | recp)}/{n_union} → "
              f"修正 {len(pa | pb)}/{n_union}")
    print(f"     ②③ 见下（下表用的是**修正后**的 single-shot）")
    print()

    print(f"  agent 独有（single-shot 修不好、agent 修好了）{len(only_a)} 个 —— 逐个查为什么：")
    for tid in only_a:
        print(f"    {tid}  {_why_ss_failed(arms[corrected], tid)}")
    print()
    print(f"  {corrected} 独有（agent 修不好、single-shot 修好了）{len(only_b)} 个 —— 逐个查为什么：")
    for tid in only_b:
        print(f"    {tid}  {_why_agent_failed(arms, tid)}")

    # 归类计数
    a_buckets: dict[str, list[str]] = {}
    for tid in only_a:
        a_buckets.setdefault(_why_ss_failed(arms[corrected], tid).split("：")[0], []).append(tid)
    b_buckets: dict[str, list[str]] = {}
    for tid in only_b:
        b_buckets.setdefault(_why_agent_failed(arms, tid).split("：")[0], []).append(tid)

    print()
    print("  == 归因汇总（每一格的责任方写得明明白白）==")
    print(f"    agent 独有 {len(only_a)} 个 = " +
          " · ".join(f"{k} {len(v)}" for k, v in sorted(a_buckets.items())))
    print(f"    {corrected} 独有 {len(only_b)} 个 = " +
          " · ".join(f"{k} {len(v)}" for k, v in sorted(b_buckets.items())))
    pure_a = a_buckets.get("纯能力", [])
    pure_b = b_buckets.get("纯能力", [])
    print()
    print(f"  ⇒ **剥离掉评测器偏差、输出合规、预算之后**，真正的能力差异只剩：")
    print(f"      只有 agent 修得好：{len(pure_a)} 个 {pure_a}")
    print(f"      只有 single-shot 修得好：{len(pure_b)} 个 {pure_b}")
    print(f"    样本只有 {len(pure_a) + len(pure_b)} 个任务 —— **不足以支撑任何架构结论**。")
    print()
    print("  ⚠️ 所以「多轮循环值多少」这个问题，本轮**不能用 agent vs single-shot 回答**：")
    print("     那个差里混着预算、输出合规、解析器偏差三样非能力因素。")
    print("     要测循环本身，看受控对：agent vs one-step（**只差 max_steps 一个数**）。")


def _cost_coldhot(arms: dict[str, dict], present: list[str]) -> None:
    """[附录] 成本与缓存状态强绑定，跨仓对比需先声明冷热起点。

    用户逐字要求这个标题，理由是一次实测事故：tinydb 归档那份 `single-shot`
    报 ¥0.8423、重跑那份报 ¥0.3453，**同一批任务、token 只差 1.1%**，差 2.4 倍。
    查下来是缓存冷热：两次的 `single_shot_prompt`（真正发出去的那条 user message）
    **逐字节相同**，15:30 那次是这组字节第一次发出（0% 命中），20:49/21:01 重发同样
    字节（98.7% / 98.6%）。`input_hit` ¥0.5/M 与 `input_miss` ¥2.0/M 差 4 倍，
    而输入占 token 的九成 —— 冷热直接决定成本量级。

    所以这一节**只报三样事实**（token / 成本 / 命中率）＋**一条判读规则**，
    不把成本差读成"某个仓更贵"：

    - **热**：prompt 前缀在本仓之前发过（`agent`/`one-step` 的 system prompt
      跨仓逐字相同，各步之间也会互相预热）；
    - **冷**：整条 prompt 的字节第一次发出（`single-shot` 把任务文本与**整包源码**
      都塞进同一条 user message，换一个仓就整条不同）。
    """
    print("\n--- 附录 · 成本与缓存状态强绑定，跨仓对比需先声明冷热起点 ---\n")
    print("  规则：**热** = 这段 prompt 字节之前发过；**冷** = 第一次发出。")
    print("  命中率低 ⇒ 输入按 ¥2.0/M 计，命中 ⇒ 按 ¥0.5/M 计（差 4 倍），而输入约占 token 九成，")
    print("  所以**冷热的成本差可达 2~3 倍**，与模型做了多少活无关。\n")
    print("  " + "臂".ljust(14) + "仓".ljust(12) + "token".rjust(11) + "成本".rjust(11)
          + "命中率".rjust(9) + "  ¥/解题".rjust(11) + "  判读")
    print("  " + "-" * 92)
    for a in present:
        r = arms[a]
        m = _arm_summary(r)
        cache = r.get("avg_cache_hit_ratio")
        cache_s = "—" if cache is None else f"{cache:.3f}"
        cps = "—" if m["cost_per_solve"] is None else f"¥{m['cost_per_solve']}"
        # 判读**只用观测到的命中率**说话，不替读者下"该是多少"的结论。
        if cache is None:
            verdict = "没记录命中率（无法判读）"
        elif cache >= 0.8:
            verdict = "热：前缀已在本轮/本仓之前发过"
        elif cache <= 0.05:
            verdict = "冷：这组字节第一次发出"
        else:
            verdict = "半热：只有一段前缀命中（跨仓拼接的 prompt 常见）"
        print("  " + a.ljust(14) + _repo_of(r).ljust(12) + f"{m['tokens']:,}".rjust(11)
              + f"¥{m['cost']:.4f}".rjust(11) + cache_s.rjust(9) + cps.rjust(11)
              + "  " + verdict)
    print("\n  ⚠️ 跨仓比成本前先看这一列：`single-shot` 换仓就是**冷**的，")
    print("     它的成本会明显高于本仓留档值 —— 那是冷热起点不同，不是「这个仓更贵」。")
    print("     所有 ¥ 都按 `archived` 定价快照估算，**是量级估算，不是账单**。")


def _table(arms: dict[str, dict], present: list[str]) -> None:
    ref = arms[present[0]]
    rows = {r["id"]: r for r in ref["per_task"]}
    print("--- 逐任务 × 三臂 ---\n")
    print("  `✓` 通过 · `✗` 未通过 · `!` 后缀 = 撞上 max_steps 预算（「没预算」不是「修不对」）")
    print("  `*` = 网络寻源风险未阻断（**一律标注**，非逐任务查证，见单臂对照报告第 3 节）\n")
    head = "  " + "任务".ljust(12) + "标题".ljust(34) + "".join(a.ljust(14) for a in present)
    print(head)
    print("  " + "-" * (len(head) - 2))
    for tid in sorted(rows, key=lambda k: rows[k]["title"]):
        title = rows[tid]["title"][:32]
        cells = []
        for a in present:
            m = {r["id"]: r for r in arms[a]["per_task"]}.get(tid)
            cells.append(("—" if m is None else _cell(m)).ljust(14))
        print(f"  {tid:<12}{title:<34}" + "".join(cells))
        for a in present:
            m = {r["id"]: r for r in arms[a]["per_task"]}.get(tid)
            if m and (m["invalid_reason"] or m["error"] or m.get("patch_failed")):
                why = m["invalid_reason"] or m["error"] or m.get("patch_error") or ""
                print(f"      [{a}] {why[:96]}")

def _has_field(arms: dict[str, dict], present: list[str], field: str) -> bool:
    """每条臂的 `per_task` 首行是不是都带这个字段（带 = 这批报告是字段落地之后跑的）。"""
    for a in present:
        pt = arms[a].get("per_task") or []
        if not pt or field not in pt[0]:
            return False
    return True


def _blind_spots_archived() -> None:
    print("== 已知盲点（结尾复述；这两条本轮**无法**用数据回答） ==")
    print()
    print("  ① env_timeout —— **无法衡量**。")
    print("     报告只落了从 `terminated_reason` 派生的一个布尔（budget_exhausted），")
    print("     既没落 `terminated_reason` 本身，也没落工具调用记录。于是")
    print("     「agent 在自己工作区里跑 pytest 撞上 bash 工具 120s 超时」这类环境失败，")
    print("     与「真没修对」在数据上**完全同形**，区分不开。")
    print("     本报告**没有**这一格，也**不会**用 duration_s 之类的东西猜一个填上去。")
    print("     可测的那部分环境故障在 1b 的 B judge_error（判定器 rc 2/3/4/5）里。")
    print("     要让它可测：`per_task` 里落 `terminated_reason` + 工具调用记录。")
    print()
    print("  ② 网络寻源未阻断 —— **无法排除**。")
    print("     eval 的工具集里有 web_fetch / web_search，task_text 又带着 commit subject")
    print("     （常含 `(#618)` 这类 issue 号），agent 可以去 GitHub 把提交连同修复搜出来。")
    print("     隔离验证只证明了**本地**那条路是断的（21/21 隔离成立、0 泄漏），")
    print("     而 per_task 不含工具调用记录 ⇒ 做不到逐任务查证有没有真的联网。")
    print("     逐任务表里的 `*` 是**一律标注**，不是查证结果。")
    print("     同样的道理：运行期 `git clone` / `pip install` 也只能间接取证")
    print("     （site-packages 差集 + 残留工作区），排除不了「装完又删干净」。")


def _blind_spots_measured(arms: dict[str, dict], present: list[str]) -> None:
    """新批次：把原来那两条盲点**逐任务算出来**，别再当成盲点复述。

    这正是「能量就别当盲点」的落地：`terminated_reason` 与 `tool_calls`
    已经落盘，那么「环境掐死 vs 真没修对」与「有没有联网寻源」都是**可数的**。
    """
    print("== 原两条盲点：这两批报告**已能量到**（字段已落盘，不再靠猜） ==")
    print()
    for a in present:
        pt = arms[a]["per_task"]
        reasons: dict[str, int] = {}
        for r in pt:
            k = r.get("terminated_reason") or "(null)"
            reasons[k] = reasons.get(k, 0) + 1
        web = [r["id"] for r in pt
               if any((c.get("name") in ("web_fetch", "web_search"))
                      for c in (r.get("tool_calls") or []))]
        blocked = sum(len(r.get("gate_blocks") or []) for r in pt)
        pad = " " * (len(a) + 2)
        print(f"  [{a}] terminated_reason：" +
              " · ".join(f"{k}×{v}" for k, v in sorted(reasons.items(), key=lambda x: -x[1])))
        print(f"  {pad} 调过 web_fetch/web_search 的任务：{len(web)}/{len(pt)}"
              + (f"  → {web}" if web else "（一条都没有）"))
        print(f"  {pad} gate_block 总条数：{blocked}"
              + ("（本轮没有工具调用被闸门拦下）" if blocked == 0 else ""))
    print()
    print("  ① 环境失败与「真没修对」**已经分得开**：`terminated_reason` 原样落盘，")
    print("     `env_timeout` 不再与「测试没过」同形。")
    print("  ② 网络寻源**已经能逐任务查证**：上面那行就是逐条 API 记录数出来的，")
    print("     不是「一律标注」。⚠️ 但它只覆盖**工具**这条路：运行期 `git clone` /")
    print("     `pip install` 仍只能间接取证（site-packages 差集 + 残留工作区），")
    print("     排除不了「装完又删干净」。这条边界照旧留着。")


def _not_proving_archived(no_rescore: bool = False) -> None:
    print("== 这份对照**不**证明什么 ==")
    print("  1. 样本量：21 个有效任务**全部来自 tinydb 一个仓库、一个语言、一个测试风格** ——")
    print("     完成率不能外推到别的语言/仓库。它证明的是「这套流程在本仓库上跑得通」。")
    print("  2. 网络寻源未阻断：隔离验证只证明了**本地**那条路是断的（21/21、0 泄漏）；")
    print("     eval 工具集里有 web_fetch/web_search，而 task_text 带 commit subject。")
    print("     任务表的 `*` 是**一律标注**，不是逐任务查证（报告里没有工具调用记录）。")
    print("  3. conftest.py 判定绕过：本轮三臂统一没开守卫，见开头免责声明。")
    print("  4. 成本是**按快照单价估算**，不是账单；`deepseek-chat` 已不在官方定价页列示。")
    print("  5. `budget_exhausted` 对 agent 臂是**推定**（该报告跑在字段落地之前），")
    print("     对后两条臂才是精确字段。引用时必须写明是哪一种。")
    if no_rescore:
        print("  6. 本表**不含**离线重算列（`--no-rescore`）。")
    else:
        print("  6. `single-shot＊` 修正列是**离线重算**，不是重跑：模型当时的输出是既成事实，")
        print("     变的只有我这侧的解析器。它回答「如果解析器没这个 bug 会被判成什么」，")
        print("     不是「模型能打多少分」。且留档那份的 `_FENCE_RE` bug **仍在 `runner.py` 里** ——")
        print("     本轮的修正只存在于 `evalverify/`，没有回写（三臂代码必须一致，用户裁定）。")
    print("  7. 剥离掉解析器偏差/输出合规/预算之后，纯能力差异只剩个位数任务（见 2c）——")
    print("     **不足以支撑任何架构结论**。要谈架构，先扩样本。")


def _not_proving_new(arms: dict[str, dict], present: list[str], no_rescore: bool) -> None:
    """新批次：每一条都改成**按这批报告的数据**说，能算的当场算。"""
    repos = sorted({_repo_of(arms[a]) for a in present})
    tasks = {a: len(arms[a]["per_task"]) for a in present}
    guards = {a: arms[a].get("judge_guard") for a in ARMS if a in arms}
    tamper = {a: sum(1 for r in arms[a]["per_task"] if r.get("judge_tampering"))
              for a in present}
    restored = {a: sum(1 for r in arms[a]["per_task"] if r.get("judge_restored"))
                for a in present}
    print("== 这份对照**不**证明什么（逐条对着这批报告说） ==")
    print(f"  1. 样本量：本表覆盖 {len(repos)} 个仓库（{'、'.join(repos)}），"
          f"逐臂任务数 {tasks}。")
    print("     **仍是 Python、仍是 pytest、仍是一个语言** —— 完成率不能外推到别的语言/生态。")
    print("  2. 网络寻源：见上节逐任务计数（工具这条路已可查证；"
          "运行期 clone/install 仍只能间接取证）。")
    print(f"  3. conftest.py 判定绕过：本表各臂的 guard = {guards}。")
    if all(guards.values()):
        print(f"     守卫**开着**：`judge_tampering` 命中的任务 {tamper} 个，")
        print(f"     判定前恢复过文件的任务 {restored} 个。")
        print("     ⇒ 有命中就说明**确实有东西动过判定相关文件**，那些任务的分数按恢复后的盘面算。")
        print("     ⚠️ **仍未覆盖**：sitecustomize.py/.pth、工作区之外的 site-packages、")
        print("     模块遮蔽（根级 `pytest/` 或 `pytest.py`）。")
    else:
        print("     ⚠️ 有关闭守卫的臂 ⇒ 那些臂的 `judge_tampering`/`judge_restored` 是 `null`，")
        print("     那是「**没检查**」不是「查过且干净」，不能读成没问题。")
    print("  4. 成本是**按快照单价估算**，不是账单；`deepseek-chat` 已不在官方定价页列示。")
    print("     ⚠️ 且**冷热起点不同的成本不可比**，见附录那一节。")
    print("  5. `budget_exhausted` 这批是**精确字段**（`terminated_reason` 已落盘），")
    print("     不再是从派生布尔推定的。")
    if no_rescore:
        print("  6. 本表**不含**离线重算列（`--no-rescore`）—— 那份重算报告属于 tinydb 那一批。")
    else:
        print("  6. `single-shot＊` 修正列是**离线重算**，不是重跑，见该列注脚。")
    print("  7. 两仓之间的差异**只说明「换一个仓会不会变」**，不能说明「换成别的语言会不会变」；")
    print("     而且两仓的任务分布、闸门通过率都不同，**跨仓相减仍然不是受控对照**。")


def _preamble(arms: dict[str, dict], present: list[str]) -> None:
    """开头那两块（免责声明 + 局限清单）。

    **留档批次用用户逐字指定的原文，一字不改**；新批次用**同一结构、同一位置**，
    但内容按这批报告的真实口径写。为什么必须分版本：留档那句「本轮三臂统一沿用
    （没守卫的）口径」对着**开了守卫**的 sqlparse 那一轮就是假话 —— 而这两块的全部
    作用就是防误读，一句假话就能把整个功能反着做。
    """
    print("=" * 78)
    if not _has_field(arms, present, "terminated_reason"):
        print(JUDGE_DISCLAIMER)
        print()
        print(LIMITATIONS)
    else:
        guards = {a: arms[a].get("judge_guard") for a in ARMS if a in arms}
        repos = sorted({_repo_of(arms[a]) for a in present})
        n = {a: len(arms[a]["per_task"]) for a in present}
        print("⚠️ 免责声明（务必与数据同时引用）：")
        print("  裁判器有已知的判定绕过面。本批报告的守卫状态逐臂为：", guards, "。")
        if all(guards.values()):
            print("  **守卫开着** —— P4：rc=0 但零个用例通过 ⇒ 不算通过；")
            print("  P1：判定前把判定相关文件恢复到 agent 动手**之前**，并记 `judge_restored`。")
            print("  所以 `judge_tampering` / `judge_restored` **有值**（`[]` = 查过且干净，")
            print("  `null` = 没检查），两个字段的含义不许混。")
            print("  **仍未覆盖**：`sitecustomize.py` / `.pth`、工作区**之外**的 site-packages、")
            print("  模块遮蔽（根级 `pytest/` 或 `pytest.py`）。")
        else:
            print("  有关闭守卫的臂 ⇒ 那些臂的 `judge_tampering` / `judge_restored` 是 `null`，")
            print("  那是「**没检查**」不是「查过且干净」，不能读成没问题。")
        print()
        print("⚠️ 本批数据的已知局限（与数字同时引用；**只引数字不引这一节 = 误引**）：")
        print(f"  1. 样本量。本表覆盖 {len(repos)} 个仓库（{'、'.join(repos)}），"
              f"逐臂任务数 {n}。")
        print("     **仍是一个语言、一个测试风格** —— 完成率不能外推到别的语言/生态。")
        print("  2. 网络寻源：本批 `per_task` **已含 `tool_calls`**，所以「有没有调 web 工具」")
        print("     可以**逐任务查证**（见结尾那节的实际计数）。但运行期的 `git clone` /")
        print("     `pip install` 仍只能间接取证（site-packages 差集 + 残留工作区），")
        print("     **排除不了**「装完又删干净」。")
        print("  3. 判定器：见上方免责声明。")
        print("  4. 成本是按**快照单价估算**的值，不是账单。`deepseek-chat` 已不在官方定价页列示。")
        print("     ⚠️ 而且**冷热起点不同的成本不可比** —— 见附录那一节。")
        print("  5. `env_timeout`：本批 `terminated_reason` **已原样落盘**，")
        print("     「环境掐死」与「真没修对」**分得开**。")
    print("=" * 78)


def main() -> None:
    argv = sys.argv[1:]
    # 两个开关是为了**第二个仓**：`--repo` 只挑该仓的报告（留档的 tinydb 报告时间戳
    # 永远更大，不过滤就把 tinydb 又挑出来了）；`--no-rescore` 跳过修正版那一列 ——
    # 那份重算报告是 tinydb 的，拿它去和另一个仓的报告做可比性校验必然不通过，
    # 会把整张表打成「以下数字不要引用」。三个位置参数的老用法一字不动。
    no_rescore = "--no-rescore" in argv
    argv = [a for a in argv if a != "--no-rescore"]
    repo = None
    if "--repo" in argv:
        i = argv.index("--repo")
        if i + 1 >= len(argv):
            sys.exit("--repo 后面要跟仓名，例如 --repo sqlparse")
        repo = argv[i + 1]
        del argv[i:i + 2]

    if len(argv) == 3:
        paths = {a: Path(p) for a, p in zip(ARMS, argv)}
    else:
        paths = _latest_reports(repo)

    arms = {}
    for a, p in paths.items():
        if p.exists():
            arms[a] = json.loads(p.read_text(encoding="utf-8"))
    # 修正版 single-shot（离线重算，零 LLM）。**必须也过一遍可比性校验** ——
    # 它是同一次运行的产物，模型/HEAD/闸门/任务集四项都应当与留档版逐字相同；
    # 只要有一项不同，就说明"修正"动了不该动的东西，那张表不能出。
    # `--repo` 给定时还要确认它**属于同一个仓**，否则会有另一个仓的一列混进来。
    if RESCORED_PATH.exists() and not no_rescore:
        resc = json.loads(RESCORED_PATH.read_text(encoding="utf-8"))
        if repo is None or _repo_of(resc) == repo:
            arms[RESCORED_KEY] = resc
    present = [a for a in ALL_COLUMNS if a in arms]

    _preamble(arms, present)
    print("\n== 三臂大对照 ==")
    print("\n  数据来源：")
    for a in present:
        src = RESCORED_PATH if a == RESCORED_KEY else paths[a]
        print(f"    {a:<12} {src}   ts={arms[a].get('ts')}  "
              f"size={src.stat().st_size:,}B")
    if RESCORED_KEY in arms:
        rs = arms[RESCORED_KEY].get("rescored") or {}
        print(f"    {'':<12} ↑ 修正版 = **离线重算**（{rs.get('by')}）："
              f"重抠已有 raw_output + 重判，**LLM 调用 {rs.get('llm_reruns')} 次**。")
        print(f"    {'':<12}   原因：{rs.get('why')}")
        print(f"    {'':<12}   ⚠️ 它不是重跑值。留档列与修正列**必须并列**引用。")
    missing = [a for a in ARMS if a not in arms]
    if missing:
        print(f"    ⚠️ 缺报告：{missing}")

    # ---- 0. 可比性（先验尺子，再出数）----
    print("\n--- 0. 可比性校验（尺子是不是同一把）---\n")
    checks, problems, notes = _compat(arms)
    for label, ok, shown in checks:
        print(f"  {'✓' if ok else '✗'} {label:<34} {shown}")
    for n in notes:
        print(f"  ⓘ {n}")

    if problems:
        print()
        print("  ╔══════════════════════════════════════════════════════════════╗")
        print("  ║  ✗ 交叉验证未通过 —— 以下数字不要引用                        ║")
        print("  ╚══════════════════════════════════════════════════════════════╝")
        for q in problems:
            print(f"    ✗ {q}")
        print()
        print("  ⇒ 三条臂不是同一把尺子量出来的。")
        print("  ⇒ **报告输出就此终止**（用户要求：有任何一项 ✗ 就先排查差异，不出一张")
        print("     看起来很美但拿三把尺子量出来的表）。下面什么都不打。")
        raise SystemExit(1)

    print()
    print(f"  ✓ {len(present)} 条臂逐项一致 —— 同一把尺子，下面的数字可比")
    gv = {arms[a].get("gate", {}).get("valid") for a in ARMS if a in arms}
    gc = {arms[a].get("gate", {}).get("candidates") for a in ARMS if a in arms}
    print(f"  ✓ 闸门逐臂一致（候选 {sorted(gc)} · 有效 {sorted(gv)}）")
    guards = {a: arms[a].get("judge_guard") for a in ARMS if a in arms}
    absent = sorted(a for a, v in guards.items() if v is None)
    if set(guards.values()) == {False}:
        print("  ✓ 判定守卫三臂**统一关闭**（同口径；代价是 conftest.py 漏洞未拦，见免责声明）")
    elif set(guards.values()) == {True}:
        print("  ✓ 判定守卫三臂**统一开着**（P4 零验证通过不算通过 + P1 判定前恢复）")
    elif absent and set(guards.values()) - {None} == {False}:
        # ⚠️ 「字段不存在」**不等于**「关着」：那是该臂跑在字段落地**之前**，
        # 值根本没人记。把它读成 False 就是把「没记录」当成「没开」——
        # 本项目反复踩过这个坑（`judge_tampering` 的 `null` 同理）。
        print(f"  ⚠️ 判定守卫：{guards} —— {absent} 的报告里**没有这个字段**")
        print("     （跑在字段落地之前 ⇒ 是「**没记录**」，不是「关着」；其余臂为 False，即关闭）")
        print("     跨臂比完成率时必须原样写明这一条。")
    else:
        print(f"  ⚠️ 判定守卫口径**不统一**：{guards} —— 跨臂比完成率时必须写明这一条")
    # ---- 1. 汇总 ----
    print("\n--- 1. 三臂汇总 ---\n")
    S = {a: _arm_summary(arms[a]) for a in present}
    ref = S[present[0]]

    def row(label: str, fn, width=30):
        print(f"  {label:<{width}}" + "".join(str(fn(S[a])).ljust(16) for a in present))

    row("有效任务（闸门后）", lambda s: s["tasks"])
    row("计分（= 完成率分母）", lambda s: s["judged"])
    row("  ├ 被摘掉：补丁未应用", lambda s: s["patch_failed"])
    row("  ├ 被摘掉：判定器没跑成 error", lambda s: s["error_n"])
    row("  └ 合计摘掉 = 任务数 − 分母", lambda s: s["excluded"])
    print("    ↑ 分母 `judged` **已经**把上面两类摘掉了，不要再减一次（那样同一批任务扣两遍）")
    dup = [a for a in present if S[a]["overlap"]]
    if dup:
        print(f"    ⚠️ 两类**有重叠**（补丁失败又撞上判定器挂）："
              + "，".join(f"{a} 重叠 {S[a]['overlap']}" for a in dup))
    row("不计分（白送分/空转）", lambda s: s["not_scored"])
    print()
    row("通过", lambda s: s["passed"])
    row("朴素完成率 passed / tasks",
        lambda s: f"{s['passed'] / s['tasks']:.1%}" if s["tasks"] else "N/A")
    row("真实能力完成率 passed / judged",
        lambda s: f"{s['completion_rate']:.1%}" if s["completion_rate"] is not None else "N/A")
    print("    ↑ 分母 judged = tasks − 补丁未落地 − 判定器没跑成。**这两个百分比含义不同**：")
    print("      朴素那个把「我的解析器挂了」也算成「模型没修好」。本条臂上两者若相等，")
    print("      只说明**这条臂没踩到基础设施噪声**，不说明两个口径没差别。")
    row("再剥离撞预算 passed / (judged − D1)",
        lambda s: (f"{s['passed'] / s['capability_denom']:.1%}"
                   if s["capability_denom"] > 0 else "N/A"))
    print("    ↑ 分子分母：D1 = 撞 max_steps 而失败的任务数（见 1b）。")
    for a in present:
        s = S[a]
        if s["capability_denom"] <= 0:
            print(f"      ⚠️ [{a}] 分母为 0：{s['d1']} 个未通过**全部**撞上预算 —— "
                  f"这条臂根本没机会试，这个比率没有定义，不是 0%")
        elif s["capability_denom"] < 8:
            print(f"      ⚠️ [{a}] 分母只有 {s['capability_denom']} 个 —— "
                  f"样本这么小，比率抖动很大，别当结论用")
    print()
    row("撞 max_steps 的任务数", lambda s: s["budget"])
    row("  └ 其中**未通过**（= 报告汇总口径）", lambda s: s["budget_fail"])
    prec = [a for a in present if S[a]["budget_precise"]]
    est = [a for a in present if not S[a]["budget_precise"]]
    if prec:
        print(f"    ↑ 精确（loop 的 terminated_reason）: {prec}")
    if est:
        print(f"    ↑ **推定**（按 steps == max_steps 猜，该报告跑在字段落地之前）: {est}")
        print(f"      ⇒ 这些数字在引用时必须写「推定」。模型在最后一步收工也会落进这个集合。")
    print()
    row("oracle 基线（金标准补丁）",
        lambda s: "100%（fix 侧闸门全绿）" if s["oracle"] and "100%" in str(s["oracle"]) else s["oracle"])
    row("总 token", lambda s: f"{s['tokens']:,}" if s["tokens"] else "—")
    row("总成本 ¥（覆盖全部任务）", lambda s: f"{s['cost']}")
    row("每次成功修复的边际成本 ¥",
        lambda s: s["cost_per_solve"] if s["cost_per_solve"] is not None else "N/A（一个都没修好）")

    # ---- 1b. 失败分类（紧跟汇总，用户要求「不要只给一个百分比」）----
    _failures(arms, present)

    # ---- 2a/2b. 同分母 + 配对（聚合数字必须拆回逐任务才看得懂）----
    _paired(arms, present)

    # ---- 2. 循环到底值多少（**受控对**：agent vs one-step，只差一个 max_steps）----
    print("--- 2d. 「多轮循环值多少」—— 受控对（agent vs one-step，只差 max_steps）---\n")
    print("  ⚠️ 为什么要单独拎出来：agent vs single-shot 的差里混着预算、输出合规、")
    print("     解析器偏差三样**非能力**因素（见 2c）。要测循环本身，只有这一对是干净的 ——")
    print("     两条臂同工具、同提示、同流程，**只差 `max_steps` 一个数**。")
    print()
    if "agent" in S and "one-step" in S:
        a, o = S["agent"], S["one-step"]
        print(f"  agent     ：{a['passed']}/{a['judged']} = "
              f"{a['completion_rate']:.0%}   {a['tokens']:,} token   ¥{a['cost']}")
        print(f"  one-step  ：{o['passed']}/{o['judged']} = "
              f"{o['completion_rate']:.0%}   {o['tokens']:,} token   ¥{o['cost']}")
        d = a["passed"] - o["passed"]
        print(f"\n  ⇒ 多轮循环多修好 **{d}** 个任务；"
              f"多花 {a['tokens'] - o['tokens']:,} token / ¥{round(a['cost'] - o['cost'], 4)}")
        if d:
            print(f"     摊到每个多修好的任务是 "
                  f"{round((a['cost'] - o['cost']) / d, 4)} 元 —— 这是「值不值」的直接依据。")
            o_s = S.get("one-step")
            if o_s and o_s["capability_denom"] <= 0:
                # 分母同上：从数据取，不写死（写死的是 tinydb 的 21）
                n_o = o_s["tasks"]
                print()
                print(f"     ⚠️ **不能读成「循环让模型变聪明了」**：one-step 的 0/{n_o} 是")
                print(f"        {o_s['budget']}/{n_o} 撞满 max_steps=1、且 "
                      f"{n_o - o_s['passed']}/{n_o} 零变更 —— 它**没有机会**产出改动。")
                print(f"        这一对量的是「给不给得起第二次机会」，不是「循环的智能」。")
                print(f"        而且 one-step 只有一步，它的 `max_steps` 是设计值不是异常值。")
        elif d < 0:
            print("     ⚠️ 循环**更差** —— 先别解释成「循环有害」，先查是不是预算被分散了"
                  "（25 步里迷路 vs 1 步直奔主题）。")
        else:
            print(f"     ⇒ 打平。样本量 {S['agent']['tasks']}，这个差值为 0 "
                  f"**不能**证伪「循环无用」，只能说本轮没测出差异。")

        # 同一批任务上，臂之间逐任务翻转 —— 只看总数会把翻转抵消掉。
        flips = []
        for r in arms["agent"]["per_task"]:
            m = {x["id"]: x for x in arms["one-step"]["per_task"]}.get(r["id"])
            if m and r["passed_effective"] != m["passed_effective"]:
                flips.append((r["id"], r["passed_effective"], m["passed_effective"]))
        if flips:
            print(f"\n  逐任务翻转 {len(flips)} 个（总数相等背后可能互相抵消）：")
            for tid, ap, op in flips:
                print(f"    {tid}  agent={'通过' if ap else '未通过'}  "
                      f"one-step={'通过' if op else '未通过'}")
    else:
        print("  （缺 agent 或 one-step 报告，跳过）")

    # ---- 2c. 解耦（用户点名：把「循环收益」与「评测器偏差」彻底拆开）----
    _decouple(arms, present)

    # ---- 3. single-shot 的失败模式 ----
    print("\n--- 3. single-shot 的专属失败模式（补丁这一环）---\n")
    if "single-shot" in S:
        s = S["single-shot"]
        print(f"  补丁未应用 {s['patch_failed']} 个 —— **单独计数，不进完成率分子分母**")
        print(f"  ⇒ single-shot 完成率的分母是 {s['judged']}"
              f"（= 有效任务 {s['tasks']} − 补丁未应用 {s['patch_failed']}"
              f" − 判定器没跑成 {s['error_n']}，重叠 {s['overlap']}）")
        print(f"     注意：分母**已经**把补丁未应用摘掉了，它不等于有效任务数 —— "
              f"拿 {s['passed']}/{s['tasks']} 当完成率是错的（那是在用自己的解析器给模型扣分）")
        if s["patch_failed"]:
            print("     理由（模型原始输出已存进报告，可复核是不是我解析器的锅）：")
            for r in arms["single-shot"]["per_task"]:
                if r.get("patch_failed"):
                    print(f"    {r['id']}  {(r.get('patch_error') or '')[:88]}")

        print()
        print("  ⚠️ **这一格里绝大部分是我的 bug，不是模型的**（2026-09-12 查清）：")
        print("     `eval/runner.py:279` 的 `_FENCE_RE` 开围栏只认 ```/```diff/```patch。")
        print("     模型回复里 diff 块前面常有个 ```python 代码块 —— 它当不了开围栏，")
        print("     **围栏配对整个错位一格**：```diff 被上一个散文块当闭围栏吃掉，")
        print("     真正的 diff 从此没有开围栏 → 落到兜底分支 `text[idx:]` →")
        print("     把模型后面的散文整段喂给 `git apply` → corrupt patch。")
        print("     ⇒ `runner.py` 已冻结（三臂必须同一份代码），改走**离线重算**：")
        print("       见 `evalverify/rescore_single_shot.py`（零 LLM）与修正列。")
        if RESCORED_KEY in S:
            c = S[RESCORED_KEY]
            print()
            print(f"  == 留档 vs 修正（同一批 raw_output，只换解析器）==")
            print(f"     补丁未应用   {s['patch_failed']} → {c['patch_failed']}")
            print(f"     通过         {s['passed']} → {c['passed']}")
            print(f"     分母 judged  {s['judged']} → {c['judged']}")
            print(f"     朴素完成率   {s['passed'] / s['tasks']:.1%} → "
                  f"{c['passed'] / c['tasks']:.1%}")
            print(f"     能力完成率   {s['completion_rate']:.1%} → {c['completion_rate']:.1%}")
            print("     **两个数字都要报**：只报修正值 = 抹掉自己犯过的错；")
            print("     只报原值 = 拿自己的 bug 当模型的能力。差额（5 个任务）全部归我的解析器。")
    else:
        print("  （缺 single-shot 报告，跳过）")

    # ---- 4. 逐任务 ----
    print()
    _table(arms, present)


    # ---- 5. 成本口径 ----
    print("\n--- 5. 成本口径 ---\n")
    for a in present:
        print(f"  {a:<12} 定价快照 {arms[a].get('pricing_snapshot_id')}")
        print(f"  {'':<12} {arms[a].get('pricing_note')}")
        if arms[a].get("pricing_warning"):
            print(f"  {'':<12} ⚠️ {arms[a]['pricing_warning']}")

    # ---- 5b. [附录] 冷热起点 ----
    _cost_coldhot(arms, present)

    # ---- 6. 已知盲点（结尾**再**复述一遍：开头那一节太长，读到结尾的人未必还记得）----
    # 收尾这两节**分两套**，按报告里有没有 `terminated_reason` 自动选：
    #   老报告（tinydb 归档，字段落地之前跑的）→ 只能复述盲点；
    #   新报告（sqlparse 这轮）→ 能算的当场算（「能量就别当盲点」）。
    #
    # ⚠️ 2026-09-13 修：这两套函数**早就写好了却一直没接上**（全仓零调用），
    # 于是 `--repo sqlparse` 的表尾巴打的是 tinydb 的硬编码散文 —— 同一份输出里
    # 开头刚说过「守卫逐臂为 True」，结尾却说「本轮三臂统一没开守卫」，自相矛盾。
    # 这正是用户最防的那种东西：口径错的漂亮表。选择由**字段**决定，不由记性决定。
    if _has_field(arms, present, "terminated_reason"):
        _blind_spots_measured(arms, present)
        print()
        _not_proving_new(arms, present, no_rescore)
    else:
        _blind_spots_archived()
        print()
        _not_proving_archived(no_rescore)

    if problems:
        print()
        print("  ⚠️ 交叉验证未通过 —— **以上数字不要引用**（见第 0 节）。")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
