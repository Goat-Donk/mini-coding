"""CLI 入口：python -m app.cli "任务"（真实 DeepSeek）/ --mock（无 key 演示）。

M3 版：接入 ContextManager（记账/compact）+ Session（轨迹/检查点/--resume）。
"""
from __future__ import annotations

import os
from pathlib import Path

import typer
from dotenv import load_dotenv

from agent.context import ContextManager
from agent.llm import BaseLLM, DeepSeekClient, LLMResult, MockLLM, ToolCall
from agent.loop import QueryEngine
from agent.session import Session, latest_session, new_session_id
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
def run(
    task: str,
    mock: bool = typer.Option(False, "--mock", help="无 key 演示"),
    resume: bool = typer.Option(False, "--resume", help="从检查点续跑（不新建会话）"),
    session_id: str | None = typer.Option(
        None, "--session-id", help="会话 id（--resume 时指定；默认取最近 session）"
    ),
    step: int | None = typer.Option(
        None, "--step", help="--resume 时指定恢复步数（默认最近检查点）"
    ),
    checkpoint_every: int = typer.Option(
        5, "--checkpoint-every", help="每 N 步写一次检查点"
    ),
):
    """在 workspace 内执行一个任务（或从检查点续跑）。"""
    workspace_root = _default_workspace()
    workspace_root.mkdir(parents=True, exist_ok=True)

    llm = _build_llm(mock)
    registry = ToolRegistry.default(workspace_root)
    context = ContextManager(llm)  # M3-1 provider-usage-first 记账

    if resume:
        sid = session_id or latest_session(workspace_root)
        if sid is None:
            typer.secho("没有可恢复的会话检查点（data/checkpoints/ 为空）", fg=typer.colors.YELLOW)
            raise typer.Exit(1)
        session, restored = Session.from_checkpoint(workspace_root, sid, step=step)
        engine = QueryEngine(
            llm, registry, workspace_root=workspace_root, context=context, session=session
        )
        typer.secho(
            f"恢复会话 {sid}（step {restored.step}）→ 续跑", fg=typer.colors.CYAN, bold=True
        )
        result = engine.run_from(restored)
    else:
        session = Session(workspace_root, new_session_id(), checkpoint_every=checkpoint_every)
        engine = QueryEngine(
            llm, registry, workspace_root=workspace_root, context=context, session=session
        )
        typer.secho(f"会话: {session.session_id}", fg=typer.colors.CYAN, bold=True)
        typer.secho(f"任务: {task}", fg=typer.colors.CYAN, bold=True)
        result = engine.run(task)

    typer.secho(f"工作目录: {workspace_root}", fg=typer.colors.BRIGHT_BLACK)
    typer.secho("---", fg=typer.colors.BRIGHT_BLACK)

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
    ctx_line = ""
    if context.last_stats is not None:
        s = context.last_stats
        ctx_line = f"，上下文 {s.warning_level} ({s.utilization:.0%}/{s.total_tokens} tokens)"
    checkpoints = session.list_checkpoints()
    cp_line = f"，检查点 {len(checkpoints)} 个" if checkpoints else ""
    typer.secho(
        f"\n[{result.terminated_reason}] 步骤 {result.steps} · "
        f"token {usage.total_tokens}（prompt {usage.prompt_tokens} + "
        f"completion {usage.completion_tokens}）{cache_line}{ctx_line}{cp_line}",
        fg=typer.colors.BRIGHT_BLACK,
    )


if __name__ == "__main__":
    app()
