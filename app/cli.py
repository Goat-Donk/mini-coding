"""CLI 入口：python -m app.cli "任务"（真实 DeepSeek）/ --mock（无 key 演示）。

M3 版：接入 ContextManager（记账/compact）+ Session（轨迹/检查点/--resume）。
M6-6：事件实时流式打印（不必等任务结束才看到进度）。
权限：所有工具调用统一过 PermissionsEngine（默认 allow，路径越界 deny，危险命令 ask，
      第三方/MCP 工具须在 mcp.json 的 allow 里显式授权，否则 ask；
      CLI 无确认交互，故 ask 由 loop 按安全默认拒绝）。hooks 走 default_engine()
      （block-at-submit：git commit 前需 data/tests_pass.marker，由测试成功自动写入）。
      `--review-edits` 是唯一例外：它给 CLI 装上确认回调，并把 edit/write 抬成 ask，
      于是改动落盘前人能看到 diff（M9-1）。默认关闭 —— 打开它会让每次改动都停下来问。
"""
from __future__ import annotations

import os
import threading
import time
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
from agent.session import (
    DEFAULT_CHECKPOINT_EVERY,
    Session,
    SessionNotFound,
    latest_session,
    list_sessions,
    new_session_id,
    resolve_session,
    session_name,
    set_session_name,
)
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


def _resolve_sid(workspace_root: Path, ref: str | None) -> str:
    """把 `--session-id` 的引用解析成 session_id：**先当 id、再当名字**；省略取最近会话。

    单独一个函数是因为原先"取会话 id"这件事在 `_print_plan` 与 `--resume` 两处
    各写了一遍（都是 `session_id or latest_session(...)` 加一句 None 检查）。
    加名字解析时只改一处、另一处照旧忽略 —— 那就是本项目的头号缺陷类：
    机制在某条路径生效、在另一条路径静默不生效。收成一处，两条路径一起拿到。
    """
    if ref:
        try:
            return resolve_session(workspace_root, ref)
        except SessionNotFound as exc:
            hint = "、".join(exc.available) if exc.available else "（没有任何会话）"
            typer.secho(
                f"找不到会话: {ref}\n可用的会话: {hint}\n"
                f"（用 python -m app.cli --sessions 看完整清单）",
                fg=typer.colors.YELLOW,
            )
            raise typer.Exit(1)
    sid = latest_session(workspace_root)
    if sid is None:
        typer.secho(
            "没有可恢复的会话检查点（data/checkpoints/ 为空）", fg=typer.colors.YELLOW
        )
        raise typer.Exit(1)
    return sid


def _session_label(workspace_root: Path, sid: str) -> str:
    """`名字 (id)` 或只有 `id` —— 打印时统一用它，免得每处各拼一遍。"""
    name = session_name(workspace_root, sid)
    return f"{name} ({sid})" if name else sid


def _print_sessions(workspace_root: Path) -> None:
    """列出会话（`--sessions`）：名字、最新步、检查点数、分叉来源。

    这条命令是 M9-3 的另一半。`--rename` 与 `--fork` 都往 `data/` 里写东西，
    但没有这个出口的话，那些东西**没有任何消费者** —— 名字起完看不见、
    分叉来源记了没人读，于是"机制在、测试绿、实际没人用得上"，正是要防的那类。
    """
    infos = list_sessions(workspace_root)
    if not infos:
        typer.secho("没有任何会话（data/ 下为空）", fg=typer.colors.YELLOW)
        return
    typer.secho(
        f"会话 {len(infos)} 个（按最近活动倒序）:", fg=typer.colors.CYAN, bold=True
    )
    for info in infos:
        # 名字放最前面：这是给人看的清单，id 是给命令用的
        label = info.name or "(未命名)"
        line = (
            f"  {label}"
            f"\n      {info.session_id} · step {info.latest_step}"
            f"（{info.checkpoint_count} 个检查点）"
            f" · {time.strftime('%m-%d %H:%M', time.localtime(info.mtime)) if info.mtime else '—'}"
        )
        if info.forked_from:
            line += (
                f"\n      ← 分叉自 {info.forked_from.get('session')}"
                f"@{info.forked_from.get('step')}"
            )
        if info.meta_error:
            # 名字丢了要**响**：静默显示"(未命名)"会让人以为从没起过名字
            line += f"\n      [元数据损坏，名字读不出: {info.meta_error}]"
        typer.echo(line)


def _do_rename(workspace_root: Path, ref: str | None, name: str) -> None:
    """给会话改名（`--rename`）。只动元数据，不建引擎、不需要 key。"""
    sid = _resolve_sid(workspace_root, ref)
    try:
        set_session_name(workspace_root, sid, name)
    except ValueError as exc:
        typer.secho(f"改名失败: {exc}", fg=typer.colors.YELLOW)
        raise typer.Exit(1)
    typer.secho(f"会话改名: {sid} → {name}", fg=typer.colors.GREEN)


def _do_fork(
    workspace_root: Path, ref: str | None, step: int | None, name: str | None
) -> str:
    """从某一步分叉出新会话，返回新会话 id。"""
    sid = _resolve_sid(workspace_root, ref)
    try:
        fork, restored = Session.fork(workspace_root, sid, step=step, name=name)
    except (FileNotFoundError, ValueError) as exc:
        typer.secho(f"分叉失败: {exc}", fg=typer.colors.YELLOW)
        raise typer.Exit(1)
    typer.secho(
        f"已分叉: {_session_label(workspace_root, sid)} 的第 {restored.step} 步 "
        f"→ {_session_label(workspace_root, fork.session_id)}",
        fg=typer.colors.CYAN, bold=True,
    )
    typer.secho(
        f"  搬了 {len(fork.list_checkpoints())} 个检查点 + "
        f"{'该步之前的' if restored.step else ''}轨迹事件",
        fg=typer.colors.BRIGHT_BLACK,
    )
    # 这句必须打：不说的话，"回到第 3 步"极容易被理解成工作区也回去了。
    # 我们的分叉只复制**对话**，文件停在当前状态（没有工作区快照机制）。
    typer.secho(
        "  注意: 这是对话分叉 —— 工作区文件**不会**回滚到那一步，"
        "分叉后的 agent 看到的是当前的文件",
        fg=typer.colors.YELLOW,
    )
    typer.secho(
        f'  继续: python -m app.cli --resume --session-id {fork.session_id} "你的指示"',
        fg=typer.colors.CYAN,
    )
    return fork.session_id


#: 确认菜单：编号 → 权限引擎的粒度串（见 agent/permissions.GRANULARITY）。
#: 键与文案对齐 TS 原版 `permissions.ts` 的 `requestApproval` 选项表。
_CONFIRM_CHOICES: dict[str, str] = {
    "1": "allow_once",
    "2": "allow_turn",
    "3": "allow_always",
    "4": "deny_once",
    "5": "deny_turn",
    "6": "deny_always",
}


def _confirm_prompt(question: str) -> str | None:
    """控制台确认回调（`--review-edits`）：把问题**连同 diff** 显示出来，读一个选择。

    这个回调只负责"显示 + 读数"，不重新解释 `question` —— 里面已经带了这次调用
    **将要做什么**（M9-1：edit/write 的 diff，由 `Tool.preview` 产出）。
    权限引擎与预览的分工是固定的：引擎判"要不要问"，工具说"改完什么样"。

    读不到输入（管道、Ctrl-C、EOF）一律返回 None → 引擎按安全默认拒绝。
    非交互环境下"卡住等输入"比"拒绝"糟得多：前者看起来像死机。

    直接回车默认拒绝而**不是**允许：确认框的默认值就是用户不假思索按下的那个，
    它必须选更安全的那个方向。
    """
    typer.echo()
    typer.secho("─" * 68, fg=typer.colors.BRIGHT_BLACK)
    typer.secho(question, fg=typer.colors.YELLOW)
    typer.secho(
        "  1) 允许一次      2) 本回合允许     3) 一直允许\n"
        "  4) 拒绝一次      5) 本回合拒绝     6) 一直拒绝",
        fg=typer.colors.BRIGHT_BLACK,
    )
    try:
        raw = typer.prompt("选择 (1-6)", default="4", show_default=False)
    except (EOFError, KeyboardInterrupt, typer.Abort):
        typer.secho("（读不到输入，按拒绝处理）", fg=typer.colors.BRIGHT_BLACK)
        return None
    return _CONFIRM_CHOICES.get(str(raw).strip())


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
    sid = _resolve_sid(workspace_root, session_id)
    try:
        _, state = Session.from_checkpoint(workspace_root, sid, step=step)
    except FileNotFoundError as exc:
        typer.secho(f"读不到检查点: {exc}", fg=typer.colors.YELLOW)
        raise typer.Exit(1)
    typer.secho(
        f"会话 {_session_label(workspace_root, sid)} 的计划:",
        fg=typer.colors.CYAN, bold=True,
    )
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
        None, "--session-id",
        help="会话 id **或名字**（--resume/--fork/--rename/--plan 时指定；默认取最近会话）",
    ),
    step: int | None = typer.Option(
        None, "--step", help="--resume/--fork 时指定步数（默认最近检查点）"
    ),
    sessions: bool = typer.Option(
        False, "--sessions", help="列出所有会话（名字/步数/分叉来源）后退出",
    ),
    rename: str | None = typer.Option(
        None, "--rename", help="给会话起个人看得懂的名字后退出（只动元数据，不需要 key）",
    ),
    fork: bool = typer.Option(
        False, "--fork",
        help="从 --step 那一步分叉出新会话（**对话**分叉，不回滚工作区文件）",
    ),
    checkpoint_every: int | None = typer.Option(
        None, "--checkpoint-every",
        help="每 N 步写一次检查点；省略时新会话用 5、--resume 沿用该会话当初的值",
    ),
    mcp: Path | None = typer.Option(
        None, "--mcp", help="MCP 配置文件路径（如 .codeagent/mcp.json），加载后注册远端工具"
    ),
    clear_taint: bool = typer.Option(
        False, "--clear-taint",
        help="复位本会话的污染标记（**人的动作**；误报被收紧时用它解锁）",
    ),
    review_edits: bool = typer.Option(
        False, "--review-edits",
        help="改动前人工确认：edit/write 每次都把 diff 显示出来等你批准",
    ),
):
    """在 workspace 内执行一个任务（或从检查点续跑、分叉、改名、列会话）。"""
    workspace_root = _default_workspace()

    # ---- 不需要 API key 的只读/元数据路径，全部放在 `_build_llm` 之前。
    # 顺序：先列（纯读）→ 再 plan（读检查点）→ 再改名（写元数据）→ 再分叉。
    if sessions:
        _print_sessions(workspace_root)
        raise typer.Exit()
    if plan:
        # 放在"任务不能为空"检查之前：--plan 本来就不带任务
        _print_plan(workspace_root, session_id, step)
        raise typer.Exit()
    if rename is not None and not fork:
        # 只改名：不跑任务。`--fork` 一起给时改名的是**分叉出来的那个**，
        # 所以那种情况留给下面的分叉路径处理。
        _do_rename(workspace_root, session_id, rename)
        raise typer.Exit()

    forked_sid: str | None = None
    if fork:
        forked_sid = _do_fork(workspace_root, session_id, step, rename)
        if not task.strip():
            # 只分叉、不续跑：分叉本身是零成本的（不动模型），而续跑要花钱。
            # 不带上任务就退出，把"要不要接着跑"留给用户显式说 —— 上面已经
            # 打出了可执行的续跑命令。
            raise typer.Exit()

    if not resume and forked_sid is None and not task.strip():
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
    #
    # M9-1 `--review-edits`：给上面这套补上**交互确认**，让 edit/write 的改动
    # 在落盘前先给人看 diff。为什么做成开关而不是改成默认：
    # TS 原版的 edit 是默认要批准的（`permissions.ts:ensureEdit`，无 TTY 时直接抛
    # "Start minicode in TTY mode to review it"），但**我们的 CLI 没有确认回调**，
    # 默认 ASK 会退化成"每一次改动都被拒绝"，整条 CLI 直接不可用 —— 那就把一个
    # 安全机制变成了路障。开关让两件事同时成立：默认路径一字不变（eval/runner
    # 也走的同一条），需要时又有一条真的能批准的路。
    review_rules = (
        {"tools": {"edit": "ask", "write": "ask"}} if review_edits else None
    )
    permissions = PermissionsEngine(
        workspace_root,
        external_tools=[tool.name for tool in registry.external()],
        confirm=_confirm_prompt if review_edits else None,
        rules=review_rules,
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
        if resume or forked_sid is not None:
            # 分叉出来的会话**直接接着跑**（--fork "指示"）：分叉已经把检查点铺好了，
            # 之后的路径与 --resume 完全一样 —— 所以这里共用同一条路，而不是
            # 复制一份"分叉后怎么跑"。两份写法迟早会有一份漏掉某个补投步骤。
            sid = forked_sid or _resolve_sid(workspace_root, session_id)
            # `checkpoint_every` **必须转发**：`from_checkpoint` 的默认值是 5，而这条
            # 路径原先没传 —— 于是 `--resume --checkpoint-every 1` 会静默回落到
            # "每 5 步一次"。后果不是报错，而是**恢复出来的这一段一步都不落盘**：
            # 真跑现场是 kill 在 step 5（检查点 [1..5]），`--resume` 接着跑到 step 9，
            # 结束时的检查点数**还是 5** —— 4 步工作全在内存里，再崩一次就整段丢。
            # 而"计划清单跨回合"恰恰是为这种场景准备的。修好后同一段跑出 [1..10]。
            # 回落的那条路也有测试钉着（tests/test_cli.py::test_resume_honors_checkpoint_every）。
            #
            # 现在再进一步：**没传**这个参数时不再回落 5，而是沿用该会话当初的节拍
            # （`None` 就是这个意思，由 `from_checkpoint` 从检查点里读）。原来的行为
            # 是"传了不生效 + 不传就用 CLI 的默认值"——后半句同样是错的：5 是 CLI 的
            # 默认值，不是这个会话的事实。用户没法看出恢复后节拍变了，所以下面把
            # **实际生效的节拍**打出来（这是这次改动里唯一的"可见性"出口）。
            try:
                session, restored = Session.from_checkpoint(
                    workspace_root, sid, step=step, checkpoint_every=checkpoint_every
                )
            except FileNotFoundError as exc:
                # `--step K` 里的 K 可能压根没落盘（检查点**按节拍**落，`--checkpoint-every 2`
                # 的会话只有偶数步）。不接的话用户拿到的是**整个 traceback** —— 真实跑
                # 撞到过（`--resume --step 9`）。`--fork` 与 `--plan` 两条路早就各有一句
                # 人话，只有这条最常用的路漏了。文案与 `--plan` 保持一致。
                typer.secho(f"读不到检查点: {exc}", fg=typer.colors.YELLOW)
                raise typer.Exit(1)
            typer.secho(
                f"检查点节拍: 每 {session.checkpoint_every} 步"
                + ("" if checkpoint_every is not None else "（沿用该会话当初的设置）"),
                fg=typer.colors.BRIGHT_BLACK,
            )
            # 先接上事件出口，**再**记下面的 resume 事件。`record_event` 只在
            # `state.emitter` 非空时才写 JSONL，而 emitter 原先要等 `run_from` 才被
            # 引擎接上（`loop.py` 里 `state.emitter = self._make_emitter()`）——
            # 于是 `taint_cleared` / `plan_resumed` / `resume_instruction` 这三条
            # 恢复期事件一条都进不了轨迹（`clear_taint` 的注释里写的"同时往轨迹里
            # 记一条"因此是假的）。引擎启动时会重新接一个"session + 实时回调"的组合
            # 出口，这里先接 session 那半边不会写重复：两边写的是不同的事件。
            restored.emitter = session.emit
            engine = QueryEngine(
                llm, registry, workspace_root=workspace_root, context=context,
                session=session, memory_blocks=memory_blocks, on_event=printer,
                permissions=permissions, hooks=hooks, skills=skills,
            )
            typer.secho(
                f"恢复会话 {_session_label(workspace_root, sid)}（step {restored.step}）→ 续跑",
                fg=typer.colors.CYAN, bold=True,
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
            session = Session(
                workspace_root, new_session_id(),
                # 新会话没有"当初的值"可沿用，省略就是 CLI 的默认节拍
                checkpoint_every=(
                    DEFAULT_CHECKPOINT_EVERY if checkpoint_every is None else checkpoint_every
                ),
            )
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
