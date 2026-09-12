"""eval runner（M5-2）：物化黄金任务 → 跑 agent → judge → 回归报告。

流程（每个任务独立、可审计）：
1. `materialize` 把 task.base_sha **物理剥离**到独立工作区（`git archive` 导出 + 隔离区
   新建一个只有 base 的仓库 —— 不含原仓库历史，见 `eval/golden_tasks.py` 模块 docstring）；
2. `leak_probe` 核实金标准提交在这个工作区里**不存在**（把"不泄漏"从声称变成每次跑都测的量）；
3. 真实 DeepSeek（或 --mock 冒烟）驱动 QueryEngine 跑 task_text 修 bug；
4. `judge` 用 hidden tests（fix 版测试）判定，agent 全程看不到；
5. 清理工作区；聚合报告：完成率 / token / 成本（定价快照估算）/ 耗时 / 缓存命中率，
   打印表格并存 JSON。

**完成率口径**（分子分母都必须排除"判定不可采信"的任务）：
- 分子：`judge 判定通过` **且** `判定有效（pytest 真跑了）` **且** `不是白送分/空转`；
- 分母：所有 `判定有效` 的任务。pytest 压根没跑成的（退出码 2/3/4/5）**不进分母**——
  那是判定器的锅，不是 agent 的锅；
- 白送分防御（agent 零文件变更 / 0 步却通过）**进分母、不计入分子**。

用法：
  python -m eval.runner --limit 3            # 真实 DeepSeek 跑 3 个任务（需要 .env key）
  python -m eval.runner --limit 2 --mock     # 无 key 冒烟：验证整条管线（judge 会失败）
  python -m eval.runner --limit 3 --keep     # 保留工作区（调试用；下次 run 会先清空重建）
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from agent.llm import BaseLLM, DeepSeekClient, MockLLM
from agent.loop import QueryEngine, RunResult
from agent.pricing import DEFAULT_MODEL as _PRICING_DEFAULT_MODEL, PriceSnapshot, pricing_note, resolve
from agent.tools.base import ToolRegistry
from eval.golden_tasks import (
    DEFAULT_REPO,
    GoldenTask,
    build_task,
    discover_fix_commits,
    git,
    judge,
    leak_probe,
    materialize,
    remove_workspace,
    validate_tasks,
)

# 成本按 `agent/pricing.py` 的**定价快照**估算，不再在这里硬编码单价。
# 快照带核实日期与状态（active/archived），成本口径说明随报告一起落盘。
DEFAULT_MODEL_FOR_PRICING = _PRICING_DEFAULT_MODEL

DEFAULT_WS_ROOT = Path("data/eval/ws")

# 快照忽略的目录：agent 跑测试就会生成这些缓存，它们不是"对源码的改动"。
# 不排除的话，一个一字未改的 agent 只要跑过一次 pytest 就会被算成"有变更"，
# 白送分防御直接失效。`.git` 是物化时新建的，当然也不算。
_SNAPSHOT_SKIP_DIRS = {".git", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}

#: **判定相关文件**：改了这些，就能在不碰一行源码的情况下翻盘 `judge`。
#:
#: 实测的翻盘路径（`evalverify/drive_judge_gaming.py`，188 字节 PoC 已验证）：
#: `judge` 是"把修复版隐藏测试覆盖写回工作区 → 在工作区里 `pytest <test_files>`"。
#: 隐藏测试被覆盖这一步是稳的，**但工作区根目录的 `conftest.py` 不会被冲掉** ——
#: pytest 把它当插件自动加载，于是一个 `pytest_collection_modifyitems` 钩子把用例
#: 全标成 skip，而**全 skip 时 pytest 退出码是 0** → 判"修好了"。
#:
#: 这条路径三层防守都拦不住：`zero_change` 拦不住（新文件，工作区确实变了）；
#: 闸门拦不住（它跑的是 base/fix 原始树，没有这个文件）；报告事后也查不出
#: （此前只存 `zero_change` 布尔，不存改了哪几个文件）。
_JUDGE_SENSITIVE = frozenset({
    "conftest.py", "pytest.ini", "pyproject.toml", "setup.cfg", "tox.ini",
})


def _judge_tampering(before: dict[str, str], after: dict[str, str]) -> list[str]:
    """工作区里**判定相关文件**被新增/改动/删除的清单（排序后的相对路径）。

    纯函数，只比两份快照 —— 好在它可以在没有工作区、没有 LLM 的情况下被单测直接钉住。
    """
    changed = sorted(
        p for p in set(before) | set(after) if before.get(p) != after.get(p)
    )
    return [p for p in changed if Path(p).name in _JUDGE_SENSITIVE]


def _budget_exhausted(run: RunResult | None) -> bool:
    """循环是**撞 max_steps 预算**停的，还是模型自己认为做完了。

    ⚠️ 取 loop 给的 `terminated_reason`，**绝不用 `steps == max_steps` 猜** ——
    模型恰好在最后一步自己收工是可能的（steps 一样、reason 是 `completed`），
    那样猜出来的"预算耗尽"是假的。

    为什么单列这个标签：把"没预算了"和"想清楚了修不对"混成一个 `passed=False`，
    会让基线臂看起来比实际差 —— `one-step` 的 `max_steps=1`，它**天然**预算受限。

    抽成独立函数是为了能被单测**直接**钉住：如果它埋在 `run_single` 里，
    测 `_aggregate` 的用例可以手工传 `budget_exhausted=True` 绕开推导，
    于是"推导写错了"没有任何测试会红（本项目头号缺陷类）。
    """
    return run is not None and run.terminated_reason == "max_steps"


def _snapshot_tree(root: Path) -> dict[str, str]:
    """工作区文件内容快照 {相对路径: sha256}。用于判定 agent 到底改没改东西。

    用**文件内容哈希**而不是 `git status`：git diff 看不见被目标仓库 `.gitignore`
    忽略的新建文件（tinydb 的 `.gitignore` 有未锚定的 `lib`/`bin`/`build`/`dist`），
    而 agent 完全可能把改动写进这类目录。
    """
    root = Path(root)
    snap: dict[str, str] = {}
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(root)
        if any(part in _SNAPSHOT_SKIP_DIRS for part in rel.parts):
            continue
        try:
            snap[str(rel).replace("\\", "/")] = hashlib.sha256(p.read_bytes()).hexdigest()
        except OSError:
            continue  # 读不到（被占用/权限）→ 不进入快照，如实少一个条目
    return snap


@dataclass
class TaskResult:
    task: GoldenTask
    passed: bool                       # judge 的**原始**判定
    run: RunResult | None
    error: str | None                  # 异常 / 判定器没跑成（pytest 退出码 2/3/4/5）
    duration_s: float                  # agent 阶段耗时（不含物化与判定）
    cost_cny: float
    judge_summary: str | None = None   # judge 的 pytest 摘要（判定依据，可回溯）
    invalid_reason: str | None = None  # 判定不可采信（白送分/空转）→ 进分母不进分子
    zero_change: bool | None = None    # agent 没改工作区任何文件（judge 之前测的）
    leak_reachable: bool | None = None # 金标准在工作区里可达？True = 泄漏（不该发生）
    setup_s: float = 0.0               # 物化耗时（物理剥离的代价，如实记）
    patch_failed: bool = False         # single-shot 专有：补丁没解析出来/没应用上
    #: 循环是**撞上 max_steps 预算**停的（`terminated_reason == "max_steps"`），
    #: 不是模型自己认为做完了。取的是 loop 的终止原因，不是 `steps == max_steps` 猜的。
    #: 为什么单列：把"没预算了"和"想清楚了修不对"混成一个 `passed=False`，
    #: 会让基线臂（`one-step` 的 max_steps=1）看起来比实际差 —— 那种臂**天然**预算受限。
    budget_exhausted: bool = False
    #: 判定相关文件（conftest.py 等）的改动清单。**None = 没检查**（guard 关着），
    #: 空列表 = 查过了、干净。这两个状态必须能分开，否则"没查"会被读成"没问题"。
    judge_tampering: list[str] | None = None
    patch_error: str | None = None     # 应用失败的原因（git apply 的 stderr）
    raw_output: str | None = None      # single-shot 的模型原始输出（防"是我解析器的锅"）
    prompt: str | None = None          # single-shot 真正发出去的 prompt（原样进报告）

    @property
    def judged(self) -> bool:
        """计入完成率分母。False = 这次运行的分数**压根不该算**，理由有两类：

        - 判定器的锅：pytest 没跑成（退出码 2/3/4/5）→ 不许冤枉 agent；
        - 还没到判定那一步：`single-shot` 的补丁没应用上 → 工作区根本没变，
          judge 只会跑出个"未修复"，那是在量解析器而不是量模型。

        （`single-shot` 的 `patch_failed` 单独计数、印在完成率旁边 —— 把它塞进
        分母等于用自己的解析器给模型扣分。）
        """
        return self.error is None and not self.patch_failed

    @property
    def effective_pass(self) -> bool:
        """计入完成率分子。三个条件缺一不可，见模块 docstring 的口径。"""
        return self.judged and self.passed and self.invalid_reason is None

    @property
    def summary_line(self) -> str:
        if self.patch_failed:
            return f"  ! {self.task.id}  {self.task.title[:58]:<58}  [补丁未应用] {self.patch_error}"
        if self.error:
            return f"  ! {self.task.id}  {self.task.title[:58]:<58}  [无效判定] {self.error}"
        if self.invalid_reason:
            return f"  ! {self.task.id}  {self.task.title[:58]:<58}  [不计分] {self.invalid_reason}"
        mark = "✓" if self.passed else "✗"
        stats = ""
        if self.run is not None:
            stats = f" {self.run.steps}步 {self.run.usage.total_tokens}t ¥{self.cost_cny:.3f} {self.duration_s:.0f}s"
        return f"  {mark} {self.task.id}  {self.task.title[:58]:<58}{stats}"


def estimate_cost_cny(
    prompt_hit: int, prompt_miss: int, completion: int, *, snapshot: PriceSnapshot | None = None
) -> float:
    """按定价快照估算单次运行成本（元）。

    `snapshot` 缺省用项目当前模型的快照（`deepseek-chat@2025`，已 archived）。
    传 None 也不会返回 None / 抛异常 —— 报告生成不该因为查不到价而失败。
    """
    snap = snapshot or resolve(DEFAULT_MODEL_FOR_PRICING)[0]
    return snap.cost_cny(prompt_hit, prompt_miss, completion)


def _build_llm(mock: bool) -> BaseLLM:
    if mock:
        # 确定性冒烟：agent 走 1 步给结论（不会修 bug，judge 必失败——管线验证用）
        return MockLLM.text("（mock 冒烟）我无法修复这个 bug。")
    load_dotenv()
    if not os.environ.get("DEEPSEEK_API_KEY"):
        raise SystemExit(
            "未配置 DEEPSEEK_API_KEY：请复制 .env.example 为 .env 填入后重试，"
            "或加 --mock 做无 key 冒烟。"
        )
    return DeepSeekClient()


# ---------- 基线臂（M5-5）----------
#
# 「循环到底值多少」要有对照，否则完成率是一个没有分母的分子。两条基线：
#
# - `one-step`：与 agent 臂**一切相同**，只把 `max_steps` 25 → 1。零风险真受控：
#   同样的工具、同样的 prompt、同样的工作区，只砍掉"多轮"。
# - `single-shot`：无工具、一次调用、要求输出 unified diff，由我们 `git apply` 落地。
#   它量的是"模型一次能写出多少"，量不到"自己去看、去跑测试"的能力。
#
# ⚠️ `single-shot` 喂什么，直接决定这个基线是不是稻草人。**不送任何定位提示**
# （`changed_sources` 一个字都不给），喂**整个包的非 tests 源码** —— 定位是这类
# 任务里最难的一环，把它送给模型就等于把基线抬高到一个"其实有工具"的水平。
# 实测 tinydb 全包 ≈ 138 KB ≈ 35k tokens，放得进上下文。

ARMS = ("agent", "one-step", "single-shot")


@dataclass
class ArmOutcome:
    """一条臂的一次运行结果。`patch_failed` / `raw_output` / `prompt` 只有
    `single-shot` 会填 —— 那三样是"防稻草人"要用的审计材料（见上）。"""

    run: RunResult
    patch_failed: bool = False
    patch_error: str | None = None
    raw_output: str | None = None
    prompt: str | None = None


SINGLE_SHOT_INSTRUCTIONS = """\
你是 CodeAgent，一个在代码仓库内工作的 AI 编程代理。

这一次你**没有工具**、不能执行任何命令、也没有第二次机会：下面给你一个真实仓库的
**全部源码文件内容**和一个 bug 报告，请一次性给出修复。

要求：
1. 只修改源码文件，不要修改 tests/ 下的测试文件；
2. 输出**一个 unified diff**（`git diff` 格式：带 `diff --git`、`---`/`+++` 头与
   `@@` 行号），放在一个 ```diff 代码块里 —— 我们会把它直接 `git apply` 到仓库上；
3. 代码块之外可以写你的说明。
"""


def _collect_sources(ws: Path) -> dict[str, str]:
    """工作区里所有**非测试**的 .py 源码 {相对路径: 内容} —— single-shot 的全部输入。"""
    out: dict[str, str] = {}
    for p in sorted(Path(ws).rglob("*.py")):
        rel = p.relative_to(ws).as_posix()
        if rel.startswith("tests/") or rel.startswith(".git/"):
            continue
        try:
            out[rel] = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue  # 读不到就少给一个文件，如实少给
    return out


def single_shot_prompt(task: GoldenTask, ws: Path) -> str:
    """single-shot 臂真正发出去的那条 user message（原样进报告，可审计）。"""
    parts = [SINGLE_SHOT_INSTRUCTIONS, "\n\n# 任务\n\n" + task.task_text, "\n\n# 仓库源码\n"]
    for rel, content in _collect_sources(ws).items():
        parts.append(f"\n=== FILE: {rel} ===\n{content}\n")
    return "".join(parts)


_FENCE_RE = re.compile(r"```(?:diff|patch)?[ \t]*\n(.*?)```", re.DOTALL)


def _extract_diff(text: str) -> str:
    """从模型回复里抠出 unified diff。抠不到返回空串（→ 记 patch_failed，不猜）。"""
    text = text or ""
    blocks = [b for b in _FENCE_RE.findall(text) if "diff --git" in b or b.lstrip().startswith("---")]
    if blocks:
        return max(blocks, key=len).strip() + "\n"
    idx = text.find("diff --git")  # 没围栏但直接贴了 diff
    if idx >= 0:
        return text[idx:].strip() + "\n"
    return ""


def _apply_patch(ws: Path, diff: str) -> tuple[bool, str]:
    """把 diff 应用到工作区。返回 (成功?, 失败原因)。

    `--recount` 是**故意的宽容**：模型把 hunk 行数写错是常见失误，那不是"修不好
    bug"，不该用我们自己解析器的严格性给它扣分。`--3way` 兜第二次（base 已提交，
    所以三方合并有底可用）。
    """
    last = ""
    for args in (
        ("apply", "--recount", "--whitespace=nowarn", "-"),
        ("apply", "--recount", "--3way", "-"),
    ):
        p = subprocess.run(
            ["git", "-C", str(ws), *args], input=diff,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        if p.returncode == 0:
            return True, ""
        last = ((p.stderr or "") + (p.stdout or "")).strip()[:400]
    return False, last


def _run_arm(arm: str, llm: BaseLLM, task: GoldenTask, ws: Path) -> "ArmOutcome":
    """跑一条臂，返回 `ArmOutcome`。

    `one-step` 只差一个 `max_steps` —— 其余全同，所以"多轮循环值多少"这件事
    是**受控对比**，而不是两次不同的实验。
    """
    if arm == "single-shot":
        prompt = single_shot_prompt(task, ws)
        result = llm.chat([{"role": "user", "content": prompt}], tools=[])
        raw = result.content or ""
        diff = _extract_diff(raw)
        if not diff:
            ok, err = False, "回复里找不到 unified diff（没有 ```diff 块、也没有 `diff --git`）"
        else:
            ok, err = _apply_patch(ws, diff)
        run = RunResult(
            final_text=raw, steps=1, usage=result.usage, events=[],
            terminated_reason="completed", task=task.task_text,
        )
        return ArmOutcome(
            run=run, patch_failed=not ok, patch_error=None if ok else err,
            raw_output=raw, prompt=prompt,
        )

    registry = ToolRegistry.default(ws)
    engine = QueryEngine(
        llm, registry, workspace_root=ws, max_steps=1 if arm == "one-step" else 25
    )
    return ArmOutcome(run=engine.run(task.task_text))


def run_single(
    task: GoldenTask, repo_dir: Path, *, ws_root: Path, llm: BaseLLM,
    snapshot: PriceSnapshot | None = None, keep: bool = False, arm: str = "agent",
    judge_guard: bool = False,
) -> TaskResult:
    """跑一个任务：物化 → 泄漏探针 → agent → judge → 清理。任何一步失败都如实记入报告。

    ⚠️ **物化也在 try 里**：`materialize` 失败（磁盘满/权限/残留目录）以前会让整批
    任务中断、报告一个都不落盘；现在它和 judge 一样只是这一个任务的 `error`。

    `llm` 由调用方构造并传入（不是在这里 new）：报告要用它的 `model`/`base_url`
    自证"是哪把尺子量的"，两处各建一次会漂移。`snapshot` 同理（成本口径要跟模型对上）。

    `judge_guard` **默认关**（见 `_JUDGE_SENSITIVE` 的说明）。开着时，判定相关文件被
    动过就记 `invalid_reason`。默认关是**有意的**：三条对照臂必须跑在同一套裁判上，
    中途换裁判就等于三份分数不可比。基建先落、口径下轮再统一换。
    """
    ws = ws_root / task.id
    run: RunResult | None = None
    error: str | None = None
    invalid_reason: str | None = None
    passed = False
    judge_summary: str | None = None
    zero_change: bool | None = None
    leak_reachable: bool | None = None
    patch_failed = False
    patch_error: str | None = None
    raw_output: str | None = None
    prompt: str | None = None
    duration_s = 0.0
    setup_s = 0.0
    budget_exhausted = False
    judge_tampering: list[str] | None = None

    try:
        t_setup = time.perf_counter()
        materialize(task, ws, repo_dir)
        setup_s = time.perf_counter() - t_setup
        leak_reachable = leak_probe(task, ws)
        if leak_reachable:
            # 不抛异常：把它如实记进报告（读数本身就是证据），但必须刺眼。
            print(f"  !! 泄漏探针报警：{task.id} 的金标准提交在工作区里可达！")
        before = _snapshot_tree(ws)

        t0 = time.perf_counter()
        outcome = _run_arm(arm, llm, task, ws)
        run = outcome.run
        patch_failed = outcome.patch_failed
        patch_error = outcome.patch_error
        raw_output = outcome.raw_output
        prompt = outcome.prompt
        duration_s = time.perf_counter() - t0
        # 循环是**撞预算**停的，还是模型自己认为做完了 —— 取 loop 给的终止原因，
        # 不用 `steps == max_steps` 猜（模型恰好在最后一步收工是完全可能的）。
        budget_exhausted = _budget_exhausted(run)

        # ⚠️ 顺序是硬要求：必须在 judge **之前**测。judge 会把隐藏测试写回工作区，
        # 之后算 diff 永远非零，这个检查就等于没做。
        after = _snapshot_tree(ws)
        zero_change = after == before
        # guard 关着时留 None（= 没检查），**不能**填 []：空列表会被读成"查过了、干净"。
        judge_tampering = _judge_tampering(before, after) if judge_guard else None

        if patch_failed:
            # 补丁没落地 → 工作区一个字没变，跑 judge 只是在量我们自己的解析器。
            # 直接跳过判定，比分母排除（见 TaskResult.judged）。
            pass
        else:
            try:
                judged = judge(task, ws)
                passed = judged.passed
                judge_summary = judged.summary
                if judged.error is not None:
                    # 测试压根没跑成 → 这是「判定无效」，不是「agent 没修好」，
                    # 必须记成 error 而不是让它污染完成率（假阴性）
                    error = judged.error
            except Exception as exc:
                error = f"judge 失败: {exc}"

        # 白送分防御：测试通过，但 agent 一个字没改 / 一步没走。
        # 闸门（validate_task）管住"base 就能过"，管不住 agent 改工作区**之外**的东西
        # （往 site-packages 塞 conftest、装个包）让测试变绿而工作区零变更 —— 这一条管。
        if passed and error is None:
            if run is not None and run.steps == 0:
                invalid_reason = "agent 一步都没执行，测试却通过"
            elif zero_change:
                invalid_reason = "agent 没改工作区任何文件，测试却通过"
            elif judge_tampering:
                # 碰了判定相关文件才通过的 —— 修的是裁判，不是 bug。见 _JUDGE_SENSITIVE。
                invalid_reason = (
                    f"agent 改动了判定相关文件 {judge_tampering}，测试通过不可采信"
                    "（存在 conftest.py 注入绕过，见 evalverify/drive_judge_gaming.py）"
                )
            if invalid_reason:
                passed = False
    except Exception as exc:  # agent/物化异常 → 记入报告（不假装成功）
        error = f"{type(exc).__name__}: {exc}"
    finally:
        if not keep:
            remove_workspace(ws)

    cost = (
        estimate_cost_cny(
            run.usage.prompt_cache_hit_tokens,
            # 用 billable_miss_tokens（有兜底）而不是原始字段：端点不返回缓存字段时
            # 原始字段是 0，会让全部输入按 ¥0 计费，而 total_tokens 照样把它们算进去。
            run.usage.billable_miss_tokens,
            run.usage.completion_tokens,
            snapshot=snapshot,
        )
        if run is not None
        else 0.0
    )
    return TaskResult(
        task=task, passed=passed, run=run, error=error,
        duration_s=duration_s, cost_cny=cost, judge_summary=judge_summary,
        invalid_reason=invalid_reason, zero_change=zero_change,
        leak_reachable=leak_reachable, setup_s=setup_s,
        budget_exhausted=budget_exhausted, judge_tampering=judge_tampering,
        patch_failed=patch_failed, patch_error=patch_error, raw_output=raw_output,
        prompt=prompt,
    )


def _aggregate(results: list[TaskResult]) -> dict:
    """把逐任务结果汇总成报告统计量（口径见模块 docstring）。

    单独成一个函数：完成率的分子/分母口径是这个文件里最容易出错、也最该被单测
    直接钉住的东西，不该埋在 `run_eval` 的返回值字面量里。
    """
    n = len(results)
    invalid = sum(1 for r in results if not r.judged)         # 判定器没跑成 / 补丁没落地 → 不进分母
    not_scored = sum(1 for r in results if r.invalid_reason)  # 白送分/空转 → 进分母、记失败
    passed = sum(1 for r in results if r.effective_pass)      # 分子
    scored = sum(1 for r in results if r.judged)              # 分母
    patch_failed = sum(1 for r in results if r.patch_failed)  # 单独计数，绝不并进上面任何一格
    # 撞预算而失败的：是 `passed=False` 的一个**子集**，不是并列的一格。
    # 从分子分母里摘出去是错的（它确实没修好）；它要的是"能被单独读出来"。
    budget_exhausted = sum(1 for r in results if r.budget_exhausted and not r.effective_pass)
    ratios = [
        r.run.usage.cache_hit_ratio
        for r in results if r.run is not None and r.run.usage.cache_hit_ratio is not None
    ]
    leaked = [r.task.id for r in results if r.leak_reachable]
    return {
        "tasks": n,
        "judged": scored,
        "invalid": invalid,
        "not_scored": not_scored,
        "passed": passed,
        "completion_rate": round(passed / scored, 3) if scored else None,
        "patch_failed": patch_failed,
        "budget_exhausted": budget_exhausted,
        "total_tokens": sum((r.run.usage.total_tokens if r.run else 0) for r in results),
        "total_cost_cny": round(sum(r.cost_cny for r in results), 4),
        "avg_cache_hit_ratio": round(sum(ratios) / len(ratios), 3) if ratios else None,
        "leak_reachable": bool(leaked),          # 任何一条泄漏都为 True（硬判据）
        "leaked_tasks": leaked,
        "total_setup_s": round(sum(r.setup_s for r in results), 1),
    }


def run_eval(
    repo_dir: Path = DEFAULT_REPO,
    *,
    ws_root: Path = DEFAULT_WS_ROOT,
    limit: int = 5,
    mock: bool = False,
    keep: bool = False,
    validate: bool = True,
    arm: str = "agent",
    judge_guard: bool = False,
) -> dict:
    """跑一批黄金任务，返回可打印/可落盘的报告 dict。

    `validate=True`（默认）先在**不花一分钱**的前提下过一遍有效性闸门：
    隐藏测试必须在 base 失败、在 fix 通过。被拒的候选**不进 LLM** ——
    既省钱，也不让"agent 什么都不做也能过"的任务混进完成率的分子。
    `validate=False` 是逃生舱，用来复现加闸门之前的行为。

    `arm` 决定跑哪一条臂（见 `ARMS`）：`agent` / `one-step` / `single-shot`。
    三臂共用同一个闸门、同一批任务、同一个 judge —— 臂之间只有"循环"这一个变量。
    """
    if arm not in ARMS:
        raise ValueError(f"未知的臂 {arm!r}，可选：{ARMS}")
    repo_dir = Path(repo_dir)
    ws_root = Path(ws_root)
    ws_root.mkdir(parents=True, exist_ok=True)

    commits = discover_fix_commits(repo_dir, limit=limit)
    candidates = [build_task(repo_dir, c) for c in commits]

    rejected: list[dict] = []
    if validate:
        tasks = []
        # 走 validate_tasks（而不是逐个 validate_task）：闸门自身异常时它返回"拒"，
        # 不会让一个任务的异常把整批任务连报告一起带走。
        # **它是生成器，所以这里是真·逐条流式**：闸门要跑十几分钟（39 个候选 × 解两次包
        # 跑两次 pytest），全跑完再一次性输出等于没有进度可言 —— 而且中途崩溃就什么都看不到。
        for task, v in validate_tasks(candidates, repo_dir):
            flag = "  [base 侧收集错误]" if v.base_collect_error else ""
            if v.valid:
                tasks.append(task)
                print(f"  ✓ 有效 {task.id}  {task.title[:48]}"
                      f"  base_rc={v.base_rc} fix_rc={v.fix_rc}{flag}")
            else:
                rejected.append({
                    "id": task.id, "title": task.title, "reason": v.reason,
                    "base_rc": v.base_rc, "fix_rc": v.fix_rc,
                    "base_collect_error": v.base_collect_error,
                })
                print(f"  ✗ 闸门拒绝 {task.id}  {task.title[:48]}"
                      f"  base_rc={v.base_rc} fix_rc={v.fix_rc}{flag}")
                print(f"      {v.reason}")
    else:
        tasks = candidates

    llm = _build_llm(mock)
    model_name = "mock" if mock else getattr(llm, "model", DEFAULT_MODEL_FOR_PRICING)
    snapshot, pricing_warning = resolve(model_name)
    results = [
        run_single(t, repo_dir, ws_root=ws_root, llm=llm, snapshot=snapshot,
                   keep=keep, arm=arm, judge_guard=judge_guard)
        for t in tasks
    ]

    return {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "mode": "mock" if mock else "deepseek",
        # 报告必须能自证"是哪把尺子量的哪一堆料"：模型/端点/仓库 HEAD 一个都不能缺，
        # 否则两次跑出的两份报告无法判定是不是同一批任务。
        "model": "mock" if mock else getattr(llm, "model", None),
        "base_url": None if mock else getattr(llm, "base_url", None),
        "repo": str(repo_dir),
        "repo_head": git(repo_dir, "rev-parse", "HEAD"),
        "candidates": len(candidates),
        "arm": arm,
        # 报告要自证**用的是哪一版裁判**。本轮三条臂统一 guard=False（沿用已知有
        # conftest 注入绕过的那一版），为的是三份分数可比；下一轮统一换成 True。
        "judge_guard": judge_guard,
        "judge_note": (
            "判定器存在已知的 conftest.py 注入绕过（188 字节 PoC："
            "evalverify/drive_judge_gaming.py 已验证）。本轮 guard=开，判定相关文件"
            "被改动即标 invalid。"
            if judge_guard else
            "⚠️ 判定器存在已知的 conftest.py 注入绕过（188 字节 PoC："
            "evalverify/drive_judge_gaming.py 已验证），本轮 guard=关，"
            "judge_tampering 一律为 null（**没检查**，不等于没问题）。"
            "三条对照臂统一沿用该口径以保证可比性；修复列入下一轮。"
        ),
        # 成本列的时效性写在报告里，不写在某个人的记忆里。
        # mock 不花钱，成本恒为 0，快照字段如实置 None 而不是填一个假的。
        "pricing_snapshot_id": None if mock else snapshot.id,
        "pricing_note": "mock 运行不产生费用" if mock else pricing_note(model_name),
        "pricing_warning": None if mock else pricing_warning,
        "gate": {
            "enabled": validate,
            "candidates": len(candidates),
            "valid": len(tasks),
            "rejected": rejected,
            "oracle_baseline": "100%（fix 侧闸门：金标准补丁在本机跑通）" if validate else None,
        },
        **_aggregate(results),
        "per_task": [
            {
                "id": r.task.id,
                "fix_sha": r.task.fix_sha,
                "title": r.task.title,
                "passed": r.passed,                    # judge 原始判定
                "passed_effective": r.effective_pass,  # 计入分子的那个
                "error": r.error,
                "invalid_reason": r.invalid_reason,
                "zero_change": r.zero_change,
                "leak_reachable": r.leak_reachable,
                # 撞预算 vs 修不对：两者都是 passed=False，但含义完全不同。
                # 基线臂（one-step 的 max_steps=1）**天然**预算受限，不分开就把它读成"能力差"。
                "budget_exhausted": r.budget_exhausted,
                # None = 本轮没查（guard 关着）；[] = 查过了、干净。两者不能混。
                "judge_tampering": r.judge_tampering,
                "patch_failed": r.patch_failed,
                "patch_error": r.patch_error,
                # single-shot 的"防稻草人"材料：prompt 与模型原始输出**原样**落盘。
                # 不写进来的话，一个 0% 的基线分不清"模型不行"还是"我的解析器不行"。
                "single_shot_prompt": r.prompt,
                "raw_output": r.raw_output,
                "judge_summary": r.judge_summary,
                "duration_s": round(r.duration_s, 1),
                "setup_s": round(r.setup_s, 2),
                "steps": r.run.steps if r.run else None,
                "tokens": r.run.usage.total_tokens if r.run else None,
                "cost_cny": round(r.cost_cny, 4),
            }
            for r in results
        ],
    }


def main() -> None:
    import argparse

    ap = argparse.ArgumentParser(description="跑 tinydb 黄金修 bug 任务并出回归报告")
    ap.add_argument("--repo", type=Path, default=DEFAULT_REPO, help="tinydb 仓库路径")
    ap.add_argument("--ws-root", type=Path, default=DEFAULT_WS_ROOT, help="任务工作区根目录")
    ap.add_argument("--limit", type=int, default=5, help="跑几个任务")
    ap.add_argument("--mock", action="store_true", help="无 key 冒烟（验证管线）")
    ap.add_argument(
        "--keep", action="store_true",
        help="保留工作区（调试）。注意：目标路径已存在时下次 run 会先清空重建，"
             "所以它只保证本次 run 之后不清理，不保证跨 run 保留",
    )
    ap.add_argument(
        "--no-validate", action="store_true",
        help="跳过有效性闸门（复现加闸门之前的行为；被拒任务会照跑并烧 token）",
    )
    ap.add_argument(
        "--arm", choices=ARMS, default="agent",
        help="跑哪条臂：agent（多轮循环，默认）/ one-step（max_steps=1）/ "
             "single-shot（无工具、一次调用、输出 diff 由我们 git apply）",
    )
    ap.add_argument(
        "--guard-judge", action="store_true",
        help="开启判定守卫：工作区里 conftest.py / pytest.ini 等**判定相关文件**被改动"
             "即标 invalid（防 conftest 注入绕过，见 evalverify/drive_judge_gaming.py）。"
             "**默认关**：对照臂必须跑在同一套裁判上，中途换裁判三份分数就不可比",
    )
    args = ap.parse_args()

    if not (args.repo / ".git").exists():
        raise SystemExit(
            f"tinydb 仓库不存在: {args.repo}。先跑 `python -m eval.golden_tasks --clone`。"
        )

    print(f"== eval runner（{'mock 冒烟' if args.mock else 'DeepSeek'}）"
          f"· {args.repo} · 任务数 {args.limit} · 臂 {args.arm} ==")
    if not args.no_validate:
        print("有效性闸门（零 LLM 成本；base 必败 + fix 必过）…")
    report = run_eval(
        args.repo, ws_root=args.ws_root, limit=args.limit, mock=args.mock,
        keep=args.keep, validate=not args.no_validate, arm=args.arm,
        judge_guard=args.guard_judge,
    )

    print("\n逐任务结果：")
    for r in report["per_task"]:
        if r["patch_failed"]:
            print(f"  ! {r['id']}  {r['title'][:58]:<58}  [补丁未应用] {r['patch_error']}")
            continue
        if r["error"]:
            print(f"  ! {r['id']}  {r['title'][:58]:<58}  [无效判定] {r['error']}")
            continue
        if r["invalid_reason"]:
            print(f"  ! {r['id']}  {r['title'][:58]:<58}  [不计分] {r['invalid_reason']}")
            continue
        mark = "✓" if r["passed"] else "✗"
        # 撞预算的单独标出来：`✗` 和 `✗[预算耗尽]` 是两件事。
        tag = "  [预算耗尽：撞 max_steps，不是修不对]" if r.get("budget_exhausted") else ""
        print(
            f"  {mark} {r['id']}  {r['title'][:58]:<58}"
            f" {r['steps']}步 {r['tokens']}t ¥{r['cost_cny']:.3f} {r['duration_s']}s{tag}"
        )

    print("\n== 汇总 ==")
    gate = report["gate"]
    if gate["enabled"]:
        print(
            f"  闸门：候选 {gate['candidates']} → 有效 {gate['valid']} "
            f"→ 拒 {len(gate['rejected'])}（被拒任务没进 LLM）"
            f" · oracle 基线 {gate['oracle_baseline']}"
        )
    else:
        print("  闸门：已用 --no-validate 跳过")
    rate = report["completion_rate"]
    rate_str = f"{rate:.0%}" if rate is not None else "N/A（无有效判定）"
    notes = []
    if report["invalid"]:
        notes.append(f"无效判定 {report['invalid']} 个（不计入完成率）")
    if report["not_scored"]:
        notes.append(f"白送分/空转 {report['not_scored']} 个（计入分母、不计入分子）")
    if report["patch_failed"]:
        # 单独印在完成率旁边：既不并进分子也不并进分母（见 TaskResult.judged）
        notes.append(f"补丁未应用 {report['patch_failed']} 个（不计入完成率，原始输出见报告）")
    if report.get("budget_exhausted"):
        # 失败的**子集**，不是并列的一格：它照样计入分母（确实没修好），
        # 但必须能被单独读出来，否则基线臂的分数会被读成"能力差"。
        notes.append(
            f"其中撞 max_steps 预算 {report['budget_exhausted']} 个"
            "（是「没预算」，不是「修不对」）"
        )
    note_str = (" · " + " · ".join(notes)) if notes else ""
    print(
        f"  完成率 {rate_str}（{report['passed']}/{report['judged']} 个有效任务）{note_str}"
        f" · token {report['total_tokens']} · 估算成本 ¥{report['total_cost_cny']}"
        f" · 平均缓存命中率 "
        + (f"{report['avg_cache_hit_ratio']:.0%}" if report["avg_cache_hit_ratio"] is not None else "N/A")
    )
    print(f"  成本口径：{report['pricing_note']}")
    print(f"  泄漏探针：{'⚠️ 有泄漏' if report['leak_reachable'] else '✓ 金标准在工作区不可达'}"
          f" · 物化总耗时 {report['total_setup_s']}s")

    out_dir = Path("data/eval")
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"report-{time.strftime('%Y%m%d-%H%M%S')}.json"
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  报告已存: {out_path}")


if __name__ == "__main__":
    main()
