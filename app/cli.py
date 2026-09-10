"""CLI 入口：python -m app.cli "任务"（真实 DeepSeek）/ --mock（无 key 演示）。

M3 版：接入 ContextManager（记账/compact）+ Session（轨迹/检查点/--resume）。
M6-6：事件实时流式打印（不必等任务结束才看到进度）。
权限：所有工具调用统一过 PermissionsEngine（默认 allow，路径越界 deny，危险命令 ask，
      第三方/MCP 工具须在 mcp.json 的 allow 里显式授权，否则 ask；
      CLI 无确认交互，故 ask 由 loop 按安全默认拒绝）。hooks 走 default_engine()
      （block-at-submit：git commit 前需 data/tests_pass.marker，由测试成功自动写入）。
"""
from __future__ import annotations

import os
import threading
from pathlib import Path

import typer
from dotenv import load_dotenv

from agent.context import ContextManager
from agent.hooks import default_engine
from agent.llm import BaseLLM, DeepSeekClient, LLMResult, MockLLM, ToolCall
from agent.loop import QueryEngine
from agent.mcp import MCPError, load_mcp_servers
from agent.memory import MemoryManager
from agent.security import TAINT_HIGH, TAINT_NONE
from agent.skills import discover_skills
from agent.permissions import PermissionsEngine
from agent.session import Session, latest_session, new_session_id
from agent.state import user as user_message
from agent.tools.ask import build_ask_tool
from agent.tools.base import ToolRegistry
from agent.tools.plan import render_plan
from agent.tools.skills import build_skill_tools
from agent.tools.subagent import SubagentTool

app = typer.Typer(no_args_is_help=True)


class EventPrinter:
    """把 agent 事件实时打到终端（M6-6）。

    只读工具是**并发**执行的，record_event 会从多个工作线程回调进来 ——
    所以打印必须加锁，否则两行会交错成乱码。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.tool_calls = 0

    def __call__(self, event: dict) -> None:
        etype = event.get("type")
        if etype == "tool_call":
            line = self._format_tool_call(event)
        elif etype == "empty_response_retry":
            line = (
                f"  [{event.get('step')}] … 模型返回空响应，"
                f"重试 {event.get('attempt')}/{event.get('limit')}"
            )
        else:
            return  # llm_call 等事件太吵，不逐条打印（轨迹 JSONL 里都有）
        with self._lock:
            typer.echo(line)

    def _format_tool_call(self, event: dict) -> str:
        self.tool_calls += 1
        args = event.get("arguments") or {}
        summary = ", ".join(f"{k}={str(v)[:60]}" for k, v in list(args.items())[:3])
        status = "✓" if event.get("success") else "✗"
        line = (
            f"  [{event.get('step')}] {status} {event.get('name')}({summary}) "
            f"[{event.get('duration_ms')}ms]"
        )
        # success 只表示"工具跑完了"，bash 的退出码才反映命令本身成没成
        exit_code = event.get("exit_code")
        if exit_code not in (None, 0):
            line += f" [exit code: {exit_code}]"
        return line


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
            "未配置 DEEPSEEK_API_KEY。请在 .env 中填入（格式见 .env.example）——"
            "官方 key 申请：https://platform.deepseek.com → API Keys。"
            "或用 --mock 做无 key 演示。",
            fg=typer.colors.YELLOW,
        )
        raise typer.Exit(1)
    return DeepSeekClient()


def _print_plan(workspace_root: Path, session_id: str | None, step: int | None) -> None:
    """打印某个会话的计划清单（`--plan`）。

    计划是**检查点里的一个字段**，读它就够了 —— 所以这条路径不需要 API key、
    不建会话、不跑任务（也因此它在 `_build_llm` 之前处理）。
    """
    sid = session_id or latest_session(workspace_root)
    if sid is None:
        typer.secho(
            "没有可读的会话检查点（data/checkpoints/ 为空）", fg=typer.colors.YELLOW
        )
        raise typer.Exit(1)
    try:
        _, state = Session.from_checkpoint(workspace_root, sid, step=step)
    except FileNotFoundError as exc:
        typer.secho(f"读不到检查点: {exc}", fg=typer.colors.YELLOW)
        raise typer.Exit(1)
    typer.secho(f"会话 {sid} 的计划:", fg=typer.colors.CYAN, bold=True)
    # 空清单不要复用 render_plan 的"已清空"文案：那是 update_plan 清空动作的说法，
    # 而这里只是"这个会话没排过计划"。
    typer.echo(render_plan(state.plan) if state.plan else "（该会话没有计划清单）")


@app.command()
def run(
    task: str = typer.Argument(
        "", help="任务描述；--resume 时作为**续跑指示**追加进会话（可省略）"
    ),
    mock: bool = typer.Option(False, "--mock", help="无 key 演示"),
    resume: bool = typer.Option(False, "--resume", help="从检查点续跑（不新建会话）"),
    plan: bool = typer.Option(
        False, "--plan",
        help="只打印最近会话的**任务计划清单**后退出（agent 自己排的，不是 TASKS.md）",
    ),
    session_id: str | None = typer.Option(
        None, "--session-id", help="会话 id（--resume 时指定；默认取最近 session）"
    ),
    step: int | None = typer.Option(
        None, "--step", help="--resume 时指定恢复步数（默认最近检查点）"
    ),
    checkpoint_every: int = typer.Option(
        5, "--checkpoint-every", help="每 N 步写一次检查点"
    ),
    mcp: Path | None = typer.Option(
        None, "--mcp", help="MCP 配置文件路径（如 .codeagent/mcp.json），加载后注册远端工具"
    ),
    clear_taint: bool = typer.Option(
        False, "--clear-taint",
        help="复位本会话的污染标记（**人的动作**；误报被收紧时用它解锁）",
    ),
):
    """在 workspace 内执行一个任务（或从检查点续跑）。"""
    workspace_root = _default_workspace()
    if plan:
        # 放在"任务不能为空"检查之前：--plan 本来就不带任务
        _print_plan(workspace_root, session_id, step)
        raise typer.Exit()
    if not resume and not task.strip():
        typer.secho(
            '请提供任务描述，例如：python -m app.cli "读 README 并总结项目结构"',
            fg=typer.colors.YELLOW,
        )
        raise typer.Exit(1)
    workspace_root.mkdir(parents=True, exist_ok=True)

    llm = _build_llm(mock)
    registry = ToolRegistry.default(workspace_root)
    registry.register(SubagentTool(llm, workspace_root))  # M4-2 research 子代理
    # ask_user：**刻意不进 ToolRegistry.default()** —— eval/runner.py 用的正是
    # default()，而 headless 评测里没有人能回答，模型一提问 eval 就提前终止：
    # 完成率被一个"没人在那儿"的机制拉低，而且是静默的（judge 只跑测试）。
    # 与 SubagentTool 一样按入口注册（构造点见 build_ask_tool）。
    registry.register(build_ask_tool())
    # skills 渐进披露：索引进 system prompt（只 name+简介），正文由 load_skill 按需取。
    # 发现结果**只扫一次**，同时喂给工具与引擎 —— 两边各扫一次会让"索引里有的名字
    # load 不到"这种不一致有地方发生。无 skill 时不注册工具（不白送 schema 占 token）。
    skills = discover_skills(workspace_root)
    for skill_tool in build_skill_tools(skills):
        registry.register(skill_tool)
    # M6-3 MCP：显式配置才加载（第三方 server 不受沙箱约束，必须 opt-in）
    mcp_clients: list = []
    mcp_allowed: list[str] = []
    if mcp is not None:
        try:
            mcp_clients, registered, mcp_allowed = load_mcp_servers(
                mcp, registry, workspace_root=workspace_root
            )
        except (FileNotFoundError, MCPError) as exc:
            typer.secho(f"MCP 加载失败: {exc}", fg=typer.colors.RED)
            raise typer.Exit(1)
        typer.secho(
            f"MCP: {len(mcp_clients)} 个 server，注册 {len(registered)} 个工具"
            + (f"（{', '.join(registered)}）" if registered else ""),
            fg=typer.colors.BRIGHT_BLACK,
        )
        typer.secho(
            f"MCP 授权: {len(mcp_allowed)}/{len(registered)} 个工具免确认"
            + ("（其余需人工确认，CLI 无交互 → 拒绝）" if len(mcp_allowed) < len(registered) else ""),
            fg=typer.colors.BRIGHT_BLACK,
        )
    context = ContextManager(llm)  # M3-1 provider-usage-first 记账
    # M2 权限引擎：CLI 无交互确认（不传 confirm），所以判定是：
    #   默认 allow（正常行为不变）· 路径越界 deny · 危险命令 ask
    #   · 第三方工具 ask（须在 mcp.json 的 allow 里显式授权）
    #   （引擎不把 ask 转成 deny；是 loop 在「无确认交互」时按安全默认拒绝，理由回喂模型）
    permissions = PermissionsEngine(
        workspace_root,
        external_tools=[tool.name for tool in registry.external()],
    )
    permissions.allow_external(mcp_allowed)
    # M2 hooks：标准治理链（block-at-submit + 测试结果维护 marker）。
    # 与控制台共用 default_engine()，避免两个入口各接一套接出漂移。
    hooks = default_engine(workspace_root)
    # M4-1 记忆：启动注入工作区记忆块；任务后提取约定（mock 模式不提取，保持脚本确定性）
    memory = MemoryManager(workspace_root, llm=None if mock else llm)
    memory_blocks = memory.blocks()
    if memory_blocks:
        typer.secho(
            f"记忆注入: {len(memory_blocks)} 个记忆块", fg=typer.colors.BRIGHT_BLACK
        )

    printer = EventPrinter()
    typer.secho(f"工作目录: {workspace_root}", fg=typer.colors.BRIGHT_BLACK)
    if skills.skills:
        # 把「有哪些 skill、有没有被遮蔽」打出来：人改了自己那份却没生效时，
        # 这行是唯一能一眼看出问题的东西（详见 skills.SkillDiscovery 的说明）
        shadow_note = (
            f"，{len(skills.shadowed)} 个同名被遮蔽（{skills.shadowed[0][0]}）"
            if skills.shadowed else ""
        )
        typer.secho(
            f"skills: {len(skills.skills)} 个（索引进提示词，正文按需加载）{shadow_note}",
            fg=typer.colors.BRIGHT_BLACK,
        )
    typer.secho("---", fg=typer.colors.BRIGHT_BLACK)

    try:
        if resume:
            sid = session_id or latest_session(workspace_root)
            if sid is None:
                typer.secho("没有可恢复的会话检查点（data/checkpoints/ 为空）", fg=typer.colors.YELLOW)
                raise typer.Exit(1)
            session, restored = Session.from_checkpoint(workspace_root, sid, step=step)
            engine = QueryEngine(
                llm, registry, workspace_root=workspace_root, context=context,
                session=session, memory_blocks=memory_blocks, on_event=printer,
                permissions=permissions, hooks=hooks, skills=skills,
            )
            typer.secho(
                f"恢复会话 {sid}（step {restored.step}）→ 续跑", fg=typer.colors.CYAN, bold=True
            )
            if restored.taint != TAINT_NONE:
                typer.secho(
                    f"污染标记: {restored.taint}（由轨迹里的 security_finding 重算）",
                    fg=typer.colors.YELLOW,
                )
            if clear_taint:
                # 复位只由**人**的显式动作触发。它同时往轨迹里记一条 taint_cleared，
                # 所以这次复位在下次 resume 重算时同样有效（不会被旧事件抬回去）。
                restored.clear_taint(reason="cli:--clear-taint")
                typer.secho("污染标记已复位为 none", fg=typer.colors.GREEN)
            if restored.plan:
                # 恢复时**补一次**计划：它很可能已经被 compact（snip/摘要）裁掉了
                # —— 那样模型就失去了"我排到哪了"，而计划正是为跨回合准备的。
                # 只在恢复时补、而不是每轮都贴：每轮贴等于把"计划有没有变"变成
                # "消息有没有变"，一变就破坏 `_PREFIX_LEN` 之后的前缀缓存。
                # 用 user 角色 + 说明来源：计划是 agent 自己产出的，不加以说明地
                # 当成"用户说的话"塞进去，模型会以为是人给的指令。
                restored.messages.append(
                    user_message(
                        "（会话恢复：这是你之前列的计划清单，继续按它推进）\n"
                        + render_plan(restored.plan)
                    )
                )
                restored.record_event("plan_resumed", items=len(restored.plan))
                typer.secho(f"计划恢复: {len(restored.plan)} 条", fg=typer.colors.CYAN)
            if task.strip():
                # 续跑指示：`--resume "..."` 曾经**静默丢掉**这个参数（run_from 用的是
                # state.task），于是「拒绝文案让你 --clear-taint 复位后重试」这条动线
                # 断在这里 —— 复位之后你没有任何办法告诉 agent「再试一次」。
                # 真人验证时就是这么卡住的：模型按拒绝文案的指引停下等人，而 CLI
                # 送不进去人的回复。现在把它作为一条 user 消息追加进会话。
                restored.messages.append(user_message(task))
                restored.record_event("resume_instruction", text=task[:200])
                typer.secho(
                    f"续跑指示: {task[:80]}", fg=typer.colors.CYAN
                )
            result = engine.run_from(restored)
        else:
            session = Session(workspace_root, new_session_id(), checkpoint_every=checkpoint_every)
            engine = QueryEngine(
                llm, registry, workspace_root=workspace_root, context=context,
                session=session, memory_blocks=memory_blocks, on_event=printer,
                permissions=permissions, hooks=hooks, skills=skills,
            )
            typer.secho(f"会话: {session.session_id}", fg=typer.colors.CYAN, bold=True)
            typer.secho(f"任务: {task}", fg=typer.colors.CYAN, bold=True)
            if clear_taint:
                # 新会话本来就是 none —— 静默忽略会让用户以为"我明明清过了"，
                # 而真正需要清的那个会话（被标记的那个）他并没有在跑
                typer.secho(
                    "提示: --clear-taint 只对 --resume 的会话有意义"
                    "（新会话的污染标记本来就是 none）",
                    fg=typer.colors.YELLOW,
                )
            result = engine.run(task)
    finally:
        # MCP 连接是资源（子进程 + 管道），任务结束必须回收，不能等 GC
        for client in mcp_clients:
            client.close()

    typer.secho("---", fg=typer.colors.BRIGHT_BLACK)

    # M4-1 任务后提取：把轨迹里可复用的约定写回 learned.md（跨会话生效）
    # 受污染的会话写到 learned.pending.md（不自动注入，等人复核）—— 判据是
    # 会话标记，不是提炼出来的内容像不像被带偏（内容过滤会误伤正常条目）。
    #
    # 停在提问处的会话**不提炼**：那是一段半程轨迹，模型当时正因为信息不足在猜，
    # 把它猜的东西提炼成"仓库约定"再自动注入后续所有会话，是污染而不是学习。
    # 续跑那一轮会照常提炼（那时轨迹是完整的），所以什么都没丢。
    if not mock:
        if result.terminated_reason == "await_user":
            typer.secho(
                "本轮停在提问处（半程轨迹），不做约定提炼 —— 续跑完成后照常提炼",
                fg=typer.colors.BRIGHT_BLACK,
            )
        else:
            learned = memory.extract_and_learn(result.events, taint=result.taint)
            if learned:
                target = (
                    ".codeagent/rules/learned.pending.md（会话被标记为受污染，待人工复核）"
                    if result.taint == TAINT_HIGH
                    else ".codeagent/rules/learned.md"
                )
                typer.secho(
                    f"已提炼 {len(learned)} 条仓库约定 → {target}",
                    fg=typer.colors.YELLOW if result.taint == TAINT_HIGH else typer.colors.GREEN,
                )
    if result.taint != TAINT_NONE:
        typer.secho(
            f"本会话污染标记: {result.taint}"
            + ("（受影响的动作: 网络外发 / 读取凭据文件 / 写入记忆文件）"
               if result.taint == TAINT_HIGH else ""),
            fg=typer.colors.YELLOW,
        )

    if result.terminated_reason == "await_user":
        # 提问**不是结论**：用「最终结论」的样式打印它，人会以为任务跑完了，
        # 而这个回合的意义恰恰是"还没完，等你一句话"。
        typer.secho("需要你补充信息:", fg=typer.colors.MAGENTA, bold=True)
        typer.echo(result.final_text or "（问题内容为空）")
        # 指引必须**可执行**：M7 的教训是一条走不通的解除指引比没有指引更糟
        # （文案让用户 --clear-taint 复位后重试，而 CLI 当时送不进人的回复）。
        # --session-id 显式给出，免得依赖"最近会话"这个隐式顺序。
        typer.secho(
            f'回答后继续: python -m app.cli --resume --session-id {session.session_id} "你的回答"',
            fg=typer.colors.CYAN,
        )
    else:
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
