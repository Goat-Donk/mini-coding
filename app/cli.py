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
from dataclasses import dataclass, field
from pathlib import Path

import typer
from dotenv import load_dotenv

from agent.context import ContextManager
from agent.goal import DEFAULT_GOAL_TURNS, GOAL_CHECK_TAIL_LINES, render_goal
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
from agent.tools.goal import build_goal_tools
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
        elif etype == "goal_check":
            # 完成检查必须**实时可见**：它是人给的判据在跑，而这条命令的退出码
            # 决定了整个目标算不算完成。藏在最终报告里等于让人事后才发现
            # 「原来它跑的是那条命令」。
            line = self._format_goal_check(event)
        elif etype == "goal_completed":
            line = f"  [{event.get('step')}] ★ 目标完成（完成检查通过）"
        else:
            return  # llm_call 等事件太吵，不逐条打印（轨迹 JSONL 里都有）
        with self._lock:
            typer.echo(line)

    def _format_goal_check(self, event: dict) -> str:
        verdict = event.get("verdict")
        mark = {"passed": "✓", "failed": "✗", "invalid": "!"}.get(verdict, "?")
        line = (
            f"  [{event.get('step')}] {mark} 完成检查 {verdict}: "
            f"{str(event.get('command'))[:60]} [{event.get('duration_ms')}ms]"
        )
        if verdict != "passed":
            line += f" [exit code: {event.get('exit_code')}]"
        if verdict == "invalid":
            # 判定无效是**最容易被忽略**的一态（它既不报错也不失败）——
            # 原因必须跟着打出来，否则人只会看到一行莫名其妙的 "!"
            line += f"\n      {str(event.get('reason'))[:200]}"
        return line

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


def _resolve_session_ref(workspace_root: Path, ref: str | None) -> str | None:
    """把会话引用（**先当 id、再当名字**）解析成 session_id；省略取最近会话。

    单独一个函数是因为原先"取会话 id"这件事在 `_print_plan` 与 `--resume` 两处
    各写了一遍（都是 `session_id or latest_session(...)` 加一句 None 检查）。
    加名字解析时只改一处、另一处照旧忽略 —— 那就是本项目的头号缺陷类：
    机制在某条路径生效、在另一条路径静默不生效。收成一处，两条路径一起拿到。

    **解析不了返回 None 并打印原因，不退出** —— 「要不要结束进程」是调用方的事：
    `--resume` 解析不了就该退出（见下面的 `_resolve_sid`），而常驻 REPL 里的
    `/resume 不存在的名字` 必须留在原地（半切换比报错糟得多）。
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
            return None
    sid = latest_session(workspace_root)
    if sid is None:
        typer.secho(
            "没有可恢复的会话检查点（data/checkpoints/ 为空）", fg=typer.colors.YELLOW
        )
        return None
    return sid


def _resolve_sid(workspace_root: Path, ref: str | None) -> str:
    """`_resolve_session_ref` 的"解析不了就结束进程"版本（`--resume`/`--plan`/…）。"""
    sid = _resolve_session_ref(workspace_root, ref)
    if sid is None:
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
    workspace_root: Path,
    ref: str | None,
    step: int | None,
    name: str | None,
) -> tuple[Session, object]:
    """从某一步分叉出新会话，返回 `(Session, AgentState)`（并打印分叉信息）。

    返回会话与状态而不只是 id：常驻 REPL 的 `/fork` 要**直接切过去**，只要一个 id
    是切不过去的（还得再 `from_checkpoint` 一次，多一次读盘、也多一处能忘的地方）。

    结尾那句"怎么接着跑"由**调用方**打印，因为单发（`python -m app.cli --resume …`）
    与 REPL（人已经在里面了）要说的话不一样 —— 但那句话依赖 sid，只有调用方知道。
    """
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
    return fork, restored


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


@dataclass
class _Runtime:
    """**除「当前会话」之外**的全部装配结果（M9-5）。

    单发路径与常驻 REPL 共用它。为什么必须共用：REPL 若自己装配一遍，迟早漏掉
    `ask_user` 注册、skills 工具、MCP 加载或 `--review-edits` 的确认回调 ——
    而 CLI 历史上**已经漏接过 hooks 与 permissions 各一次**（两个入口各装各的，
    一个装了、另一个没装，且失败是静默的）。装配点只有一处，就不会有第二次。

    会话不在这里：`QueryEngine.session` 是**构造期绑定**的，REPL 换会话就得换
    引擎，所以 `engine(session)` 是方法而不是字段。
    """

    workspace_root: Path
    llm: BaseLLM
    registry: ToolRegistry
    permissions: PermissionsEngine
    hooks: object
    memory: MemoryManager
    memory_blocks: list[str]
    skills: object
    context: ContextManager
    printer: EventPrinter
    mcp_clients: list = field(default_factory=list)
    mock: bool = False

    def engine(self, session: Session) -> QueryEngine:
        """按会话装配引擎。**这是唯一一处 `QueryEngine(...)` 构造。**"""
        return QueryEngine(
            self.llm, self.registry, workspace_root=self.workspace_root,
            context=self.context, session=session, memory_blocks=self.memory_blocks,
            on_event=self.printer, permissions=self.permissions, hooks=self.hooks,
            skills=self.skills,
        )

    def close(self) -> None:
        """回收 MCP 连接（子进程 + 管道），**幂等** —— 常驻 REPL 每个回合都可能调。"""
        for client in self.mcp_clients:
            client.close()
        self.mcp_clients = []


def _build_runtime(
    workspace_root: Path, *, mock: bool, mcp: Path | None, review_edits: bool
) -> _Runtime:
    """装配一个运行环境（`_Runtime`）并打印一行启动信息。

    照着 `run()` 里原先那段逐字搬过来的，只把结尾的 `QueryEngine(...)` 换成
    `runtime.engine(session)` —— 也就是把「两处各写一遍」收成一处。
    """
    llm = _build_llm(mock)
    registry = ToolRegistry.default(workspace_root)
    registry.register(SubagentTool(llm, workspace_root))  # M4-2 research 子代理
    # ask_user：**刻意不进 ToolRegistry.default()** —— eval/runner.py 用的正是
    # default()，而 headless 评测里没有人能回答，模型一提问 eval 就提前终止：
    # 完成率被一个"没人在那儿"的机制拉低，而且是静默的（judge 只跑测试）。
    # 与 SubagentTool 一样按入口注册（构造点见 build_ask_tool）。
    registry.register(build_ask_tool())
    # M9-6 目标：**同样刻意不进 `default()`** —— eval 用的正是 default()，而
    # headless 里没有任何入口能创建目标（`/goal` 是 REPL 的命令），模型会拿到
    # 一个永远失败的诱饵。理由与 ask_user 完全同构。
    for goal_tool in build_goal_tools():
        registry.register(goal_tool)
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
        # 相对路径按**工作区**解析，不按进程 CWD。`--mcp .codeagent/mcp.json` 正是
        # help 里给的例子，而它在 CWD != 工作区时（比如设了 WORKSPACE_ROOT 从别处跑）
        # 会报"配置不存在" —— 一句照着文档抄却走不通的指引。这与本项目"路径一律落在
        # workspace_root 内"的约定也一致。
        mcp_path = Path(mcp)
        if not mcp_path.is_absolute():
            mcp_path = workspace_root / mcp_path
        try:
            mcp_clients, registered, mcp_allowed = load_mcp_servers(
                mcp_path, registry, workspace_root=workspace_root
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
    return _Runtime(
        workspace_root=workspace_root,
        llm=llm,
        registry=registry,
        permissions=permissions,
        hooks=hooks,
        memory=memory,
        memory_blocks=memory_blocks,
        skills=skills,
        context=context,
        printer=printer,
        mcp_clients=mcp_clients,
        mock=mock,
    )


def _reinject_plan(state) -> None:
    """恢复会话时把计划清单**补投一次**（单发与 REPL 共用一处）。

    恢复时补、而不是每轮都贴：每轮贴等于把"计划有没有变"变成"消息有没有变"，
    一变就破坏 `_PREFIX_LEN` 之后的前缀缓存。用 user 角色 + 说明来源：计划是
    agent 自己产出的，不加以说明地当成"用户说的话"塞进去，模型会以为是人给的指令。
    """
    if not state.plan:
        return
    state.messages.append(
        user_message(
            "（会话恢复：这是你之前列的计划清单，继续按它推进）\n"
            + render_plan(state.plan)
        )
    )
    state.record_event("plan_resumed", items=len(state.plan))
    typer.secho(f"计划恢复: {len(state.plan)} 条", fg=typer.colors.CYAN)


def _reinject_goal(state) -> None:
    """恢复会话时把目标**补投一次**（单发与 REPL 共用一处）。

    补投的理由与 `_reinject_plan` 完全一样：目标很可能已经被 compact（snip /
    摘要）裁掉了，那样模型就不知道自己在为什么干活。**只补一次、不每轮贴** ——
    每轮贴等于把"目标有没有变"变成"消息有没有变"，一变就破坏 `_PREFIX_LEN`
    之后的前缀缓存。

    （目标**从不进 system prompt**：`system` 是 `messages[0]`、在 `_PREFIX_LEN`
    保护区内，而且目标是会话中途创建的、注入就得回改第 0 条。可见性靠这里的
    补投 + 创建那一刻的 kickoff 消息，本来就够。）
    """
    if state.goal is None:
        return
    state.messages.append(
        user_message(
            "（会话恢复：这是本会话正在推进的目标。完成判据由人给定，你无法修改，"
            "运行时会在你声明完成后执行它。）\n" + render_goal(state.goal)
        )
    )
    state.record_event("goal_resumed_in_context", status=state.goal.status)
    typer.secho(f"目标恢复: {state.goal.objective}", fg=typer.colors.CYAN)


def _print_goal(workspace_root: Path, session_id: str | None, step: int | None) -> None:
    """打印某个会话的目标（`--goal`）。

    与 `--plan` 完全同构，理由也一样：**没有这条出口，REPL 里设的目标在进程外
    没有任何消费者**（本项目明确防这个 —— `--rename`/`--fork` 当初就是配着
    `--sessions` 一起做的）。目标本身是检查点里的一个字段，读它就够了，
    所以不需要 key、不建会话、不跑任务，因此排在 `_build_llm` 之前。
    """
    sid = _resolve_sid(workspace_root, session_id)
    try:
        _, state = Session.from_checkpoint(workspace_root, sid, step=step)
    except FileNotFoundError as exc:
        typer.secho(f"读不到检查点: {exc}", fg=typer.colors.YELLOW)
        raise typer.Exit(1)
    typer.secho(
        f"会话 {_session_label(workspace_root, sid)} 的目标:",
        fg=typer.colors.CYAN, bold=True,
    )
    if state.goal is None:
        typer.echo("（该会话没有目标 —— 目标只能在 REPL 里用 /goal 设定）")
        return
    typer.echo(render_goal(state.goal))
    # 输出**从事件里取**，不从 `goal.last_check` 取：事件是判定的权威记录、
    # 本来就在检查点里（`state.events`），而 `last_check` 只是给 `/goal status`
    # 用的摘要缓存。在 `last_check` 里再存一份输出就是同一个东西的第二份拷贝。
    last = next(
        (e for e in reversed(state.events) if e.get("type") == "goal_check"), None
    )
    if last and last.get("output_tail"):
        # **判据与它的输出印在一起**：这是「空转的检查命令」（README S17）唯一
        # 的缓解手段 —— 运行时只看退出码、不评价这条命令检查了什么，所以至少
        # 要让人一眼看见它到底跑了什么、回了什么。
        typer.secho(
            f"最近一次判定输出（末尾 {GOAL_CHECK_TAIL_LINES} 行）:",
            fg=typer.colors.BRIGHT_BLACK,
        )
        typer.echo(str(last["output_tail"]))


def _extract_learned(runtime: _Runtime, events: list[dict], taint: str, *,
                     terminated_reason: str | None = None) -> None:
    """任务后提炼仓库约定（M4-1；单发与 REPL 共用一处）。

    受污染的会话写到 `learned.pending.md`（不自动注入，等人复核）—— 判据是
    **会话标记**，不是提炼出来的内容像不像被带偏（内容过滤会误伤正常条目）。

    停在提问处的会话**不提炼**：那是一段半程轨迹，模型当时正因为信息不足在猜，
    把它猜的东西提炼成"仓库约定"再自动注入后续所有会话，是污染而不是学习。
    续跑那一轮会照常提炼（那时轨迹是完整的），所以什么都没丢。
    """
    if runtime.mock:
        return
    if terminated_reason == "await_user":
        typer.secho(
            "本轮停在提问处（半程轨迹），不做约定提炼 —— 续跑完成后照常提炼",
            fg=typer.colors.BRIGHT_BLACK,
        )
        return
    learned = runtime.memory.extract_and_learn(events, taint=taint)
    if learned:
        target = (
            ".codeagent/rules/learned.pending.md（会话被标记为受污染，待人工复核）"
            if taint == TAINT_HIGH
            else ".codeagent/rules/learned.md"
        )
        typer.secho(
            f"已提炼 {len(learned)} 条仓库约定 → {target}",
            fg=typer.colors.YELLOW if taint == TAINT_HIGH else typer.colors.GREEN,
        )


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
    goal: bool = typer.Option(
        False, "--goal",
        help="只打印最近会话的**目标**（含最近一次完成检查的判定与输出）后退出",
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
    repl: bool = typer.Option(
        False, "--repl",
        help="常驻交互模式：进提示符，一行一个回合（可带任务作为第一回合）",
    ),
    goal_turns: int = typer.Option(
        DEFAULT_GOAL_TURNS, "--goal-turns",
        help="REPL 里目标**一拍**自动推进最多几个回合（上限 3×25 步，防止把人锁在提示符外）",
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
    if goal:
        # 与 --plan 同类：读检查点里的一个字段就该够，所以不需要 key、不建会话、
        # 不跑任务 —— 因此它排在 `_build_llm` 之前（测试用"一调用就炸"的
        # `_build_llm` 替身钉住这条）。
        _print_goal(workspace_root, session_id, step)
        raise typer.Exit()
    if rename is not None and not fork:
        # 只改名：不跑任务。`--fork` 一起给时改名的是**分叉出来的那个**，
        # 所以那种情况留给下面的分叉路径处理。
        _do_rename(workspace_root, session_id, rename)
        raise typer.Exit()

    forked_sid: str | None = None
    if fork:
        fork_session, _fork_state = _do_fork(workspace_root, session_id, step, rename)
        forked_sid = fork_session.session_id
        if not task.strip() and not repl:
            # 只分叉、不续跑：分叉本身是零成本的（不动模型），而续跑要花钱。
            # 不带上任务就退出，把"要不要接着跑"留给用户显式说 —— 下面已经
            # 打出了可执行的续跑命令。（`--repl` 时不退出：人已经在终端前，
            # 进去之后第一行输入就是"要不要接着跑"。）
            typer.secho(
                f'  继续: python -m app.cli --resume --session-id {forked_sid} "你的指示"',
                fg=typer.colors.CYAN,
            )
            raise typer.Exit()

    if not resume and forked_sid is None and not task.strip() and not repl:
        typer.secho(
            '请提供任务描述，例如：python -m app.cli "读 README 并总结项目结构"',
            fg=typer.colors.YELLOW,
        )
        raise typer.Exit(1)
    workspace_root.mkdir(parents=True, exist_ok=True)

    # M9-5：装配收进 `_build_runtime`（原先这里与下面 `else` 分支各构造一遍
    # `QueryEngine(...)`，参数逐字相同）—— 单发与常驻 REPL 共用同一份装配，
    # 就不会再出现"一个入口接了、另一个没接"（hooks/permissions 都漏接过一次）。
    runtime = _build_runtime(
        workspace_root, mock=mock, mcp=mcp, review_edits=review_edits
    )

    if repl:
        # 常驻交互模式：整个进程一条 `_Runtime`，会话由 REPL 自己切换。
        # 带任务 = 第一回合；`--resume`/`--fork` 则拿那个会话当起始会话。
        #
        # **导入放在函数里**：`app/repl.py` 在模块级 `from app.cli import ...` 复用
        # 上面的会话解析/打印 helper（这正是"两处各写一遍 → 漂移"的解药），
        # 所以 cli 反过来在模块级导入 repl 会成环。放在这里时 cli 已经加载完了。
        from app.repl import run_repl

        try:
            run_repl(
                runtime,
                task=task,
                start_session_id=forked_sid or (session_id if resume else None),
                resume=resume or forked_sid is not None,
                step=step,
                checkpoint_every=checkpoint_every,
                clear_taint=clear_taint,
                goal_turns=goal_turns,
            )
        finally:
            runtime.close()
        raise typer.Exit()

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
            engine = runtime.engine(session)
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
                _reinject_plan(restored)
            _reinject_goal(restored)
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
            engine = runtime.engine(session)
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
        runtime.close()

    typer.secho("---", fg=typer.colors.BRIGHT_BLACK)

    # M4-1 任务后提取（跨会话生效）。**只在**这里做一次 —— REPL 在退出时调同一个
    # helper，两份实现迟早会有一份漏掉"受污染写 pending"或"半程轨迹不提炼"。
    _extract_learned(
        runtime, result.events, result.taint,
        terminated_reason=result.terminated_reason,
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
    if runtime.context.last_stats is not None:
        s = runtime.context.last_stats
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
