"""CLI 入口：python -m app.cli "任务"（真实 DeepSeek）/ --mock（无 key 演示）。

M1 最小版：组装 QueryEngine → 跑任务 → 打印每步事件 + 最终结论 + 用量。
M2+ 会加 --resume、权限 ask 交互、--checkpoint-dir。
"""
from __future__ import annotations

import os
from pathlib import Path

import typer
from dotenv import load_dotenv

from agent.llm import BaseLLM, DeepSeekClient, LLMResult, MockLLM, ToolCall
from agent.loop import QueryEngine
from agent.tools.base import ToolRegistry

app = typer.Typer(no_args_is_help=True)


def _default_workspace() -> Path:
    return Path(os.environ.get("WORKSPACE_ROOT", "workspace")).resolve()


def _build_llm(mock: bool) -> BaseLLM:
    if mock:
        # 确定性演示：glob 真实执行 → 模型（Mock）给最终回答
        return MockLLM.script(
            LLMResult(content=None, tool_calls=[ToolCall(id="call_demo", name="glob", arguments={"pattern": "**/*"})]),
            LLMResult(content="（Mock 演示）我已探索工作目录。真实模式下这里会输出基于工具结果的分析结论。"),
        )
    load_dotenv()
    if not os.environ.get("DEEPSEEK_API_KEY"):
        typer.secho(
            "未配置 DEEPSEEK_API_KEY。请复制 .env.example 为 .env 并填入，"
            "或使用 --mock 无 key 演示。",
            fg=typer.colors.YELLOW,
        )
        raise typer.Exit(1)
    return DeepSeekClient()


@app.command()
def run(task: str, mock: bool = typer.Option(False, "--mock", help="无 key 演示")):
    """在 workspace 内执行一个任务。"""
    workspace_root = _default_workspace()
    workspace_root.mkdir(parents=True, exist_ok=True)

    llm = _build_llm(mock)
    registry = ToolRegistry.default(workspace_root)
    engine = QueryEngine(llm, registry, workspace_root=workspace_root)

    typer.secho(f"任务: {task}", fg=typer.colors.CYAN, bold=True)
    typer.secho(f"工作目录: {workspace_root}", fg=typer.colors.BRIGHT_BLACK)
    typer.secho("---", fg=typer.colors.BRIGHT_BLACK)

    result = engine.run(task)

    for event in result.events:
        if event["type"] == "tool_call":
            args = event["arguments"]
            summary = ", ".join(f"{k}={str(v)[:60]}" for k, v in list(args.items())[:3])
            status = "✓" if event["success"] else "✗"
            typer.echo(f"  [{event['step']}] {status} {event['name']}({summary}) "
                       f"[{event['duration_ms']}ms]")

    typer.secho("---", fg=typer.colors.BRIGHT_BLACK)
    typer.secho("最终结论:", fg=typer.colors.GREEN, bold=True)
    if result.final_text:
        typer.echo(result.final_text)
    else:
        typer.echo("（无结论）")

    usage = result.usage
    ratio = usage.cache_hit_ratio
    cache_line = f"，缓存命中 {ratio:.0%}" if ratio is not None else ""
    typer.secho(
        f"\n[{result.terminated_reason}] 步骤 {result.steps} · "
        f"token {usage.total_tokens}（prompt {usage.prompt_tokens} + "
        f"completion {usage.completion_tokens}）{cache_line}",
        fg=typer.colors.BRIGHT_BLACK,
    )


if __name__ == "__main__":
    app()
