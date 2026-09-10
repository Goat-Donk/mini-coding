"""eval runner（M5-2）：物化黄金任务 → 跑 agent → judge → 回归报告。

流程（每个任务独立、可审计）：
1. `materialize` 把 task.base_sha 检出到独立 worktree（agent 的工作区）；
2. 真实 DeepSeek（或 --mock 冒烟）驱动 QueryEngine 跑 task_text 修 bug；
3. `judge` 用 hidden tests（fix 版测试）判定，agent 全程看不到；
4. 清理 worktree；聚合报告：完成率 / token / 成本（DeepSeek 公开定价估算）/
   耗时 / 缓存命中率，打印表格并存 JSON。

用法：
  python -m eval.runner --limit 3            # 真实 DeepSeek 跑 3 个任务（需要 .env key）
  python -m eval.runner --limit 2 --mock     # 无 key 冒烟：验证整条管线（judge 会失败）
  python -m eval.runner --limit 3 --keep     # 保留工作区（调试用）
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from agent.llm import BaseLLM, DeepSeekClient, MockLLM
from agent.loop import QueryEngine, RunResult
from agent.tools.base import ToolRegistry
from eval.golden_tasks import (
    DEFAULT_REPO,
    GoldenTask,
    build_task,
    discover_fix_commits,
    judge,
    materialize,
    remove_worktree,
)

# DeepSeek 公开定价（元/M tokens，2025，与 UI 常量一致）：输入命中 ¥0.5 / 未命中 ¥2，输出 ¥8
PRICE_INPUT_HIT_CNY = 0.5
PRICE_INPUT_MISS_CNY = 2.0
PRICE_OUTPUT_CNY = 8.0

DEFAULT_WS_ROOT = Path("data/eval/ws")


@dataclass
class TaskResult:
    task: GoldenTask
    passed: bool
    run: RunResult | None
    error: str | None
    duration_s: float
    cost_cny: float

    @property
    def summary_line(self) -> str:
        if self.error:
            return f"  ✗ {self.task.id}  {self.task.title[:58]:<58}  [error] {self.error}"
        mark = "✓" if self.passed else "✗"
        stats = ""
        if self.run is not None:
            stats = f" {self.run.steps}步 {self.run.usage.total_tokens}t ¥{self.cost_cny:.3f} {self.duration_s:.0f}s"
        return f"  {mark} {self.task.id}  {self.task.title[:58]:<58}{stats}"


def estimate_cost_cny(prompt_hit: int, prompt_miss: int, completion: int) -> float:
    """按 DeepSeek 公开定价估算单次运行成本（元）。"""
    return (
        prompt_hit * PRICE_INPUT_HIT_CNY
        + prompt_miss * PRICE_INPUT_MISS_CNY
        + completion * PRICE_OUTPUT_CNY
    ) / 1e6


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


def run_single(
    task: GoldenTask, repo_dir: Path, *, ws_root: Path, mock: bool
) -> TaskResult:
    """跑一个任务：物化 → agent → judge → 清理。任何一步失败都如实记入报告。"""
    llm = _build_llm(mock)
    ws = ws_root / task.id
    materialize(task, ws, repo_dir)

    t0 = time.perf_counter()
    run: RunResult | None = None
    error: str | None = None
    try:
        registry = ToolRegistry.default(ws)
        engine = QueryEngine(llm, registry, workspace_root=ws, max_steps=25)
        run = engine.run(task.task_text)
    except Exception as exc:  # agent 异常 → 记入报告（不假装成功）
        error = f"{type(exc).__name__}: {exc}"
    duration_s = time.perf_counter() - t0

    passed = False
    try:
        judged = judge(task, ws)
        passed = judged.passed
    except Exception as exc:
        error = f"judge 失败: {exc}"
    finally:
        remove_worktree(repo_dir, ws)

    cost = (
        estimate_cost_cny(
            run.usage.prompt_cache_hit_tokens,
            run.usage.prompt_cache_miss_tokens,
            run.usage.completion_tokens,
        )
        if run is not None
        else 0.0
    )
    return TaskResult(
        task=task, passed=passed, run=run, error=error,
        duration_s=duration_s, cost_cny=cost,
    )


def run_eval(
    repo_dir: Path = DEFAULT_REPO,
    *,
    ws_root: Path = DEFAULT_WS_ROOT,
    limit: int = 5,
    mock: bool = False,
) -> dict:
    """跑一批黄金任务，返回可打印/可落盘的报告 dict。"""
    repo_dir = Path(repo_dir)
    ws_root = Path(ws_root)
    ws_root.mkdir(parents=True, exist_ok=True)

    commits = discover_fix_commits(repo_dir, limit=limit)
    tasks = [build_task(repo_dir, c) for c in commits]
    results = [run_single(t, repo_dir, ws_root=ws_root, mock=mock) for t in tasks]

    n = len(results)
    passed = sum(1 for r in results if r.passed)
    total_tokens = sum((r.run.usage.total_tokens if r.run else 0) for r in results)
    total_cost = sum(r.cost_cny for r in results)
    ratios = [
        r.run.usage.cache_hit_ratio
        for r in results if r.run is not None and r.run.usage.cache_hit_ratio is not None
    ]
    avg_ratio = round(sum(ratios) / len(ratios), 3) if ratios else None

    return {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "mode": "mock" if mock else "deepseek",
        "repo": str(repo_dir),
        "tasks": n,
        "passed": passed,
        "completion_rate": round(passed / n, 3) if n else 0.0,
        "total_tokens": total_tokens,
        "total_cost_cny": round(total_cost, 4),
        "avg_cache_hit_ratio": avg_ratio,
        "per_task": [
            {
                "id": r.task.id,
                "title": r.task.title,
                "passed": r.passed,
                "error": r.error,
                "duration_s": round(r.duration_s, 1),
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
    ap.add_argument("--keep", action="store_true", help="保留工作区（调试）")
    args = ap.parse_args()

    if not (args.repo / ".git").exists():
        raise SystemExit(
            f"tinydb 仓库不存在: {args.repo}。先跑 `python -m eval.golden_tasks --clone`。"
        )

    print(f"== eval runner（{'mock 冒烟' if args.mock else 'DeepSeek'}）"
          f"· {args.repo} · 任务数 {args.limit} ==")
    report = run_eval(
        args.repo, ws_root=args.ws_root, limit=args.limit, mock=args.mock,
    )
    if not args.keep:
        pass  # run_single 已清理每个 worktree

    print("\n逐任务结果：")
    for r in report["per_task"]:
        mark = "✓" if r["passed"] else "✗"
        err = f"  [error] {r['error']}" if r["error"] else ""
        print(
            f"  {mark} {r['id']}  {r['title'][:58]:<58}"
            f" {r['steps']}步 {r['tokens']}t ¥{r['cost_cny']:.3f} {r['duration_s']}s{err}"
        )

    print("\n== 汇总 ==")
    print(
        f"  完成率 {report['completion_rate']:.0%}（{report['passed']}/{report['tasks']}）"
        f" · token {report['total_tokens']} · 估算成本 ¥{report['total_cost_cny']}"
        f" · 平均缓存命中率 "
        + (f"{report['avg_cache_hit_ratio']:.0%}" if report["avg_cache_hit_ratio"] is not None else "N/A")
    )

    out_dir = Path("data/eval")
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"report-{time.strftime('%Y%m%d-%H%M%S')}.json"
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  报告已存: {out_path}")


if __name__ == "__main__":
    main()
