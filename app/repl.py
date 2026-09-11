"""常驻交互模式（REPL，M9-5）：一行输入 = 一个回合。

它存在的理由是 `TASKS.md` 里给它的定位 —— **④ Goal / ⑤ Loop 的硬前置**。
「跨回合自动推进」「每 N 分钟重复一次提示词」在"跑完就退出"的一次性进程里
没有任何意义：连"下一个回合"这个概念都不存在。

设计上的三件事值得单独说：

1. **它自己持有 `AgentState`**，一整个进程用同一份 `messages`。这正是暴露出
   四处"单发时看不见"的问题的地方（`terminated_reason` 不复位、`record_discovery`
   每个回合重放、权限的 `_turn` 永不过期、中断后的工具结果残缺），它们都已各自
   在 `agent/` 里修掉并留了注释 —— 多回合不是新功能，是**换个用法**。

2. **装配完全复用 `app.cli._build_runtime`**。REPL 若自己装配一遍（注册工具、
   接权限、接 hooks、加载 MCP、装 `--review-edits` 的确认回调），迟早漏掉一样
   —— CLI 历史上已经漏接过 hooks 与 permissions 各一次，而两次都是静默的。

3. **两条硬不变量**（各有测试钉着）：
   - 不认识的斜杠命令**绝不发给模型**。打错一个字母的代价是一次真实的模型调用
     （钱 + 时间），而模型的回答看起来还挺像回事，于是这个错误不会被发现。
   - 会话切换失败**不能半切换**。`/resume 不存在的名字` 必须留在当前会话里 ——
     「session 换了、engine/state 没换」是一个没有任何报错的错配：轨迹写进 A、
     你在看 B。

**它不是全屏 TUI**（那是 TASKS.md 里明确不做的一项）：行式输入，没有 ANSI
控制、没有历史滚动。多行输入缓冲 / 历史文件 / 自动补全也都不做 —— 那是纯终端
体验的体力活，对这份作品集要回答的问题（循环、上下文、权限、可恢复性）不加分。
"""
from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

import typer

if TYPE_CHECKING:                      # 只用于注解，避免与 app.cli 的导入成环
    from agent.loop import RunResult

from agent.goal import (
    DEFAULT_GOAL_TURNS,
    GOAL_ACTIVE,
    GOAL_DONE,
    GOAL_PAUSED,
    Goal,
    goal_can_advance,
    goal_continuation_message,
    goal_kickoff_message,
    render_goal,
)
from agent.loop import REASON_GOAL_CHECK_INVALID, REASON_GOAL_DONE
from agent.security import TAINT_NONE
from agent.session import (
    DEFAULT_CHECKPOINT_EVERY,
    Session,
    derive_taint,
    set_session_name,
    unique_session_id,
)
from agent.tools.plan import render_plan

# 复用 CLI 的装配与展示 helper。**这是刻意的**：会话解析、会话清单、分叉、
# 计划补投、约定提炼这几件事在单发与常驻两条路上必须是同一份实现（见模块
# docstring 第 2 点）。反向的导入放在 `app/cli.py` 的 `run()` 函数体里，避免成环。
from app.cli import (
    _Runtime,
    _do_fork,
    _extract_learned,
    _print_sessions,
    _reinject_goal,
    _reinject_plan,
    _resolve_session_ref,
    _session_label,
)

#: 斜杠命令表：名字 → (处理函数名, 一句话说明)。`/help` 的正文由它生成 ——
#: 命令与帮助写在两个地方，迟早会出现"帮助里有、实际没有"（或者反过来）。
_COMMANDS: dict[str, tuple[str, str]] = {
    "/help": ("_cmd_help", "显示这份帮助"),
    "/exit": ("_cmd_exit", "退出（等价于 Ctrl+D，或空行处 Ctrl+C）"),
    "/new": ("_cmd_new", "开一个新会话（当前会话留在检查点里，可 /resume 回来）"),
    "/resume": ("_cmd_resume", "切到某个会话：id 或名字；省略 = 最近一个"),
    "/fork": ("_cmd_fork", "从当前会话的第 N 步分叉出去并切过去（省略 = 最近检查点）"),
    "/plan": ("_cmd_plan", "打印 agent 自己排的任务计划清单"),
    "/goal": (
        "_cmd_goal",
        "设定并自动推进一个目标：/goal <目标> --check <命令>；"
        "子命令 status / pause [原因] / resume / clear",
    ),
    "/sessions": ("_cmd_sessions", "列出所有会话（名字/步数/检查点数/分叉来源）"),
    "/rename": ("_cmd_rename", "给当前会话起个人看得懂的名字"),
    "/clear-taint": ("_cmd_clear_taint", "复位本会话的污染标记（**人的动作**）"),
}

#: `/goal` 的子命令（不带 `--check` 时按它们解析）。**顺序无关**，只用于判"首词
#: 是不是子命令"。
_GOAL_SUBCOMMANDS: tuple[str, ...] = ("status", "pause", "resume", "clear")

#: 自动推进碰到这些终止原因就停。**`"completed"` 刻意不在里面**：一个回合
#: "正常跑完"（模型给出了文字、不再调工具）恰恰是自动推进要继续的情形 ——
#: 目标的完成与否由人给的检查命令说了算，不由模型停不停下来说了算。
#:
#: 这份集合必须覆盖 `loop.TERMINATED_REASONS` 里除 `completed` 之外的全部取值，
#: 有测试钉着（新增终止原因却忘了在这里停下 = 自动推进白烧预算，且不报错）。
BURST_STOP_REASONS: frozenset[str] = frozenset({
    "await_user", REASON_GOAL_CHECK_INVALID, REASON_GOAL_DONE,
    "max_steps", "loop_detected", "error",
})

#: 一拍停下来时，把终止原因翻成给人看的一句暂停理由。**每个原因都要有话说**：
#: 暂停是自动推进的唯一出口，理由说不清的话，人只会看到「它自己停了」。
_STOP_REASONS: dict[str, str] = {
    "await_user": "模型提问，等你回答",
    REASON_GOAL_CHECK_INVALID: "完成检查无法执行（见上方判定）",
    "max_steps": "本回合达到步数上限",
    "loop_detected": "本回合触发了循环检测",
    "error": "本回合出错",
    "interrupted": "本回合被 Ctrl+C 打断",
}


def help_text() -> str:
    width = max(len(name) for name in _COMMANDS)
    lines = ["斜杠命令（行首的 `/` 一律当命令；要把它当任务发出去，行首加一个空格）:"]
    lines += [
        f"  {name.ljust(width)}  {desc}" for name, (_, desc) in _COMMANDS.items()
    ]
    lines.append(
        "其他任何输入都是一条任务，走完整的 agent 循环"
        "（工具、权限、hooks、检查点都照旧）。"
    )
    return "\n".join(lines)


class Repl:
    """常驻会话：持有 `session` / `state` / `engine` 三样，一起换、不分开换。"""

    def __init__(
        self,
        runtime: _Runtime,
        *,
        checkpoint_every: int | None = None,
        clear_taint: bool = False,
        goal_turns: int = DEFAULT_GOAL_TURNS,
    ) -> None:
        self.runtime = runtime
        self.workspace_root = runtime.workspace_root
        self.checkpoint_every = checkpoint_every
        self.clear_taint = clear_taint
        # 一拍自动推进的回合上限。**刻意不进检查点** —— 它是交互策略，不是
        # 会话事实（对比 `checkpoint_every`：那个是"这个会话怎么落的盘"，
        # 恢复时必须沿用）。每次进 REPL 重新决定就好。
        self.goal_turns = max(1, goal_turns)
        self.session: Session | None = None
        self.state = None
        self.engine = None
        self._done = False
        self._turns = 0
        self._last_reason: str | None = None

    # ---------- 启动 / 主循环 ----------

    def start(
        self,
        *,
        start_session_id: str | None = None,
        resume: bool = False,
        step: int | None = None,
    ) -> None:
        """建立**起始**会话：`--resume`/`--fork` 给的那个，或者一个全新的。"""
        if resume:
            sid = _resolve_session_ref(self.workspace_root, start_session_id)
            if sid is None:
                # 起始会话解析不了 = 没得跑，结束进程（与单发 `--resume` 一致）。
                # **REPL 内部的 `/resume` 不能这么做** —— 那里必须留在原地，
                # 见 `_cmd_resume`。
                raise typer.Exit(1)
            try:
                session, state = Session.from_checkpoint(
                    self.workspace_root, sid, step=step,
                    checkpoint_every=self.checkpoint_every,
                )
            except FileNotFoundError as exc:
                typer.secho(f"读不到检查点: {exc}", fg=typer.colors.YELLOW)
                raise typer.Exit(1)
            typer.secho(
                f"检查点节拍: 每 {session.checkpoint_every} 步"
                + ("" if self.checkpoint_every is not None else "（沿用该会话当初的设置）"),
                fg=typer.colors.BRIGHT_BLACK,
            )
            # `_activate` 会接上 `state.emitter`，所以下面这几条恢复期事件
            # （taint_cleared / plan_resumed）能进轨迹 —— 顺序不能反。
            self._activate(session, state)
            self._announce_taint(state)
            if self.clear_taint:
                state.clear_taint(reason="repl:--clear-taint")
                typer.secho("污染标记已复位为 none", fg=typer.colors.GREEN)
            _reinject_plan(state)
            _reinject_goal(state)
            typer.secho(
                f"恢复会话 {_session_label(self.workspace_root, session.session_id)}"
                f"（step {state.step}）→ 交互模式",
                fg=typer.colors.CYAN, bold=True,
            )
        else:
            if self.clear_taint:
                # 静默忽略会让用户以为"我明明清过了"，而真正需要清的那个会话
                # 他并没有在跑（同 CLI 单发路径的提示）
                typer.secho(
                    "提示: --clear-taint 只对 --resume 的会话有意义"
                    "（新会话的污染标记本来就是 none）",
                    fg=typer.colors.YELLOW,
                )
            self._activate(self._new_session())
            typer.secho(
                f"会话: {self.session.session_id}", fg=typer.colors.CYAN, bold=True
            )

    def loop(self) -> None:
        """读一行 → 一个回合。**只有** EOF / 提示符处 Ctrl+C / `/exit` 会离开。

        目标活跃时，**每一轮先自动推进一拍**再回到提示符（M9-6）。一拍结束
        一定把它停下来（见 `_run_goal_burst`），所以下面这个 `if` 不会在同一
        个提示符上反复触发 —— 这也是「暂停」在架构里真实存在的地方：
        我们的架构里没有定时器，暂停的后果就是**这块不再自动跑**。
        """
        typer.secho(
            "常驻交互模式：一行一个回合。`/help` 看命令，`/exit` 退出。",
            fg=typer.colors.BRIGHT_BLACK,
        )
        try:
            while not self._done:
                if goal_can_advance(self.state.goal):
                    self._run_goal_burst()
                    if self._done:
                        break
                try:
                    raw = input(self._prompt())
                except EOFError:
                    typer.echo()
                    self._farewell("输入结束")
                    break
                except KeyboardInterrupt:
                    typer.echo()
                    self._farewell("Ctrl+C")
                    break
                line = raw.strip()
                if not line:
                    continue  # 空行不是任务（也不该让模型收到一条空消息）
                if raw.startswith("/"):
                    self._dispatch(line)
                else:
                    # ★ **人的一行输入 = 隐式暂停。** 这不是偏好，是结构性事实：
                    # `input()` 是阻塞的，人只能在一拍停下时才拿到提示符；这一行
                    # 若不暂停，回合结束后循环回到顶部、立刻又起一拍，
                    # **人再也敲不进第二行**。
                    #
                    # 选暂停而不是"插一回合后继续"，还因为它是可逆的那一侧
                    # （`/goal resume` 一句话恢复），而且它**不静默** ——
                    # `_pause_goal` 会把原因和恢复方式打出来。
                    self._pause_goal("人工回合（你敲了一行字，目标转为暂停）")
                    self.run_turn(line)
        finally:
            self._wrap_up()

    # ---------- 目标的自动推进（M9-6） ----------

    def _run_goal_burst(self) -> None:
        """自动推进**一拍**：最多 `goal_turns` 个回合，然后一定停下来。

        **上限必须有**：`input()` 是阻塞的、没有定时器，一拍期间人根本敲不进字
        —— 无界推进 = 把人锁在门外直到烧完额度。一拍最多
        `goal_turns × max_steps` 步，这个算术在 `/goal` 的输出里打给人看
        （人得知道自己授权了多少）。

        **结束时一定暂停**（而不是留着 active 回提示符）：否则循环顶部会立刻
        再起一拍 —— 按一次回车、或者敲一条 `/help`，都会重新触发一轮自动推进，
        那就等于无界。停下来这个动作让「一拍 = 一次授权」在结构上成立。
        """
        # 兜底理由：正常走到这里都是"跑满本拍上限"。
        stop = f"本拍自动推进已达上限（{self.goal_turns} 回合）"
        for index in range(self.goal_turns):
            goal = self.state.goal
            if not goal_can_advance(goal):
                return  # 已经 done / paused / clear 掉了 —— 没有要停的东西
            text = (
                goal_kickoff_message(goal) if index == 0
                else goal_continuation_message(goal)
            )
            goal.turns += 1
            result = self.run_turn(text)
            if result is None:  # Ctrl+C 打断
                stop = _STOP_REASONS["interrupted"]
                break
            if result.terminated_reason in BURST_STOP_REASONS:
                stop = _STOP_REASONS.get(
                    result.terminated_reason, result.terminated_reason
                )
                break
        self._pause_goal(stop)

    def _pause_goal(self, reason: str, *, auto: bool = True) -> None:
        """把当前目标停下来（数据变更 + 一条事件 + 一句给人看的话）。

        **它是数据，不是控制流** —— 与 `ask_user` 的 `await_user` 同一个纪律：
        暂停只是把 `status` 改掉，谁来读它、谁因此改变行为，是调用方的事。
        所以这里既不 `return` 也不 `raise`。
        """
        goal = self.state.goal
        if not goal_can_advance(goal):
            return
        goal.status = GOAL_PAUSED
        goal.pause_reason = reason
        self.state.record_event("goal_paused", reason=reason, auto=auto)
        typer.secho(f"目标已暂停：{reason}", fg=typer.colors.MAGENTA, bold=True)
        typer.secho(
            "  （继续自动推进: /goal resume；查看进度: /goal status）",
            fg=typer.colors.BRIGHT_BLACK,
        )

    # ---------- 一个回合 ----------

    def run_turn(self, text: str) -> RunResult | None:
        """跑一个回合：`engine.run_turn` 负责接上 user 消息并跑循环。

        **`run_turn` 是回合的唯一入口**（`agent/loop.py`）—— 它在里面先补齐被中断
        的工具结果配对、再追加 user 消息、再按"本轮一份预算"跑。REPL 这里只做
        显示与记账。

        返回 `RunResult`（被 Ctrl+C 打断时返回 None）—— 调用方/测试要能拿到
        这一回合的终止原因与结论。
        """
        # Usage 是**可变** dataclass，loop 里 `state.usage += ...` 是**原地**累加。
        # 所以快照必须复制一份：`before = self.state.usage` 拿到的是同一个对象，
        # 相减恒为 0 —— 而且不报错，只是"每次都说这回合没花钱"。
        before_usage = replace(self.state.usage)
        before_step = self.state.step
        self._turns += 1
        try:
            result = self.engine.run_turn(self.state, text)
        except KeyboardInterrupt:
            # 回合中途 Ctrl+C：**不退出进程**，回到提示符。被打断的那一轮多半
            # 留下了残缺的工具结果配对，下一个回合的入口会把它补齐（并记一条
            # `pairing_repaired`），所以这里不需要做任何修复动作。
            typer.secho(
                "\n（本回合已中断，回到提示符；下一步会自动补齐残缺的工具结果）",
                fg=typer.colors.YELLOW,
            )
            self._last_reason = "interrupted"
            return None
        self._last_reason = result.terminated_reason
        self._report(result, before_usage, before_step)
        return result

    # ---------- 斜杠命令 ----------

    def _dispatch(self, line: str) -> None:
        head, _, rest = line.partition(" ")
        entry = _COMMANDS.get(head.lower())
        if entry is None:
            # ★ 硬不变量：喂进来的东西**绝不**当任务发给模型。打错一个字母的代价
            # 是一次真实的模型调用，而且模型的回答看起来还挺像回事 —— 人不会
            # 发现，只会觉得"这次答得有点怪"。
            typer.secho(
                f"未知命令: {head}（用 /help 看有哪些命令）", fg=typer.colors.YELLOW
            )
            return
        try:
            getattr(self, entry[0])(rest.strip())
        except typer.Exit:
            # 命令自己判定失败并"退出"：在 REPL 里这条退出只作用于**这条命令**。
            # 打错一个步数不该把你踢出整个会话。（也正因为如此，`/exit` 用的是
            # 标志位而不是 typer.Exit —— 否则它会被这里吞掉。）
            pass

    def _cmd_help(self, arg: str) -> None:
        typer.echo(help_text())

    def _cmd_exit(self, arg: str) -> None:
        self._farewell("exit")
        self._done = True

    def _cmd_new(self, arg: str) -> None:
        self._activate(self._new_session())
        typer.secho(
            f"新会话: {self.session.session_id}"
            f"（旧会话的检查点都在，/resume 可以回去）",
            fg=typer.colors.CYAN, bold=True,
        )

    def _cmd_resume(self, arg: str) -> None:
        # ★ 硬不变量：**先解析、再切换**。任何一步失败都在 `_activate` 之前返回，
        # 于是"当前会话仍活跃"这件事是结构性成立的，而不是靠每处记得清理。
        sid = _resolve_session_ref(self.workspace_root, arg or None)
        if sid is None:
            return
        try:
            session, state = Session.from_checkpoint(
                self.workspace_root, sid, checkpoint_every=self.checkpoint_every
            )
        except FileNotFoundError as exc:
            typer.secho(f"读不到检查点: {exc}", fg=typer.colors.YELLOW)
            return
        self._activate(session, state)
        self._announce_taint(state)
        _reinject_plan(state)
        _reinject_goal(state)
        typer.secho(
            f"已切到 {_session_label(self.workspace_root, sid)}（step {state.step}）",
            fg=typer.colors.CYAN, bold=True,
        )

    def _cmd_fork(self, arg: str) -> None:
        step: int | None = None
        if arg:
            try:
                step = int(arg)
            except ValueError:
                typer.secho(f"步数要是个整数: {arg}", fg=typer.colors.YELLOW)
                return
        # `_do_fork` 在失败时 `raise typer.Exit(1)`（先打印人话）—— 被 `_dispatch`
        # 接住，于是"分叉失败"不会把 REPL 一起带走。
        fork, state = _do_fork(
            self.workspace_root, self.session.session_id, step, None
        )
        self._activate(fork, state)
        typer.secho(
            f"已切到分叉出的会话 {_session_label(self.workspace_root, fork.session_id)}"
            f"（step {state.step}）",
            fg=typer.colors.CYAN, bold=True,
        )

    def _cmd_plan(self, arg: str) -> None:
        typer.secho(
            f"会话 {_session_label(self.workspace_root, self.session.session_id)} 的计划:",
            fg=typer.colors.CYAN, bold=True,
        )
        # 空清单不复用 render_plan 的"已清空"文案：那是 update_plan 清空动作的说法，
        # 而这里只是"这个会话没排过计划"（同 CLI `--plan`）。
        typer.echo(
            render_plan(self.state.plan) if self.state.plan else "（该会话没有计划清单）"
        )

    _GOAL_USAGE = (
        "用法:\n"
        "  /goal <目标> --check <命令>   设定目标并自动推进（命令的退出码 0 才算完成）\n"
        "  /goal          或  /goal status          看当前目标\n"
        "  /goal pause [原因]                       停掉自动推进，回到你按行输入\n"
        "  /goal resume                             恢复自动推进\n"
        "  /goal clear                              丢弃目标（不是标记完成）"
    )

    def _cmd_goal(self, arg: str) -> None:
        """`/goal` 命令族：设定 / status / pause / resume / clear（M9-6）。

        `_dispatch` 用 `line.partition(" ")` 切键，所以键只能是第一段 token ——
        四个子命令因此必须由**同一个 handler** 解析剩下的部分。

        解析规则只有两条，且必须是确定的：
        - **带 `--check` 一定是设定**；
        - **不带 `--check` 且首词是已知子命令 → 子命令**；
        - 两者都不是 → 用法错（只在这条命令内失败，不带走 REPL）。

        已知边界（如实记）：目标文本里出现字面 ` --check ` 会被切开。**不做引号
        解析** —— 加一层"半个 shell"只会造出第二个有歧义的解析器。
        """
        rest = arg.strip()
        head, _, sub = rest.partition(" ")
        head = head.lower()

        # 判"带不带 --check"用带前导空格的版本：这样 `--check` 出现在最开头
        # 也算数（那样 objective 为空 → 用法错），不会静默当成目标文本。
        padded = f" {rest}"
        if " --check " in padded:
            objective, _, check = padded.partition(" --check ")
            self._create_goal(objective.strip(), check.strip())
            return

        if not rest or head == "status":
            self._goal_status()
        elif head == "pause":
            self._goal_pause(sub.strip())
        elif head == "resume":
            self._goal_resume()
        elif head == "clear":
            self._goal_clear()
        else:
            typer.secho(
                f"未知的 /goal 用法: {head}（要设定目标请带上 --check）",
                fg=typer.colors.YELLOW,
            )
            typer.echo(self._GOAL_USAGE)

    def _create_goal(self, objective: str, check_command: str) -> None:
        if not objective or not check_command:
            # 只报"参数不对"模型/人会原样重试；把缺的那一半说出来就能改对。
            missing = "目标文本" if not objective else "检查命令（--check 后面那段）"
            typer.secho(f"缺 {missing}", fg=typer.colors.YELLOW)
            typer.echo(self._GOAL_USAGE)
            return
        existing = self.state.goal
        if existing is not None and existing.status in (GOAL_ACTIVE, GOAL_PAUSED):
            # **拒绝静默替换**：旧目标的检查命令会无声消失，而它正是"完成与否"
            # 的唯一判据 —— 那种丢失事后只能靠翻轨迹发现。
            typer.secho(
                f"已有一个未结束的目标（{existing.status}），先 `/goal clear` 再设新的",
                fg=typer.colors.YELLOW,
            )
            typer.echo(render_goal(existing))
            return
        goal = Goal(
            objective=objective,
            check_command=check_command,
            created_step=self.state.step,
        )
        self.state.goal = goal
        self.state.record_event(
            "goal_created",
            objective=objective,
            check_command=check_command,
            step=self.state.step,
        )
        typer.secho("已设定目标:", fg=typer.colors.GREEN, bold=True)
        typer.echo(render_goal(goal))
        # **把授权额度打给人看**：自动推进会连跑若干回合、期间人敲不进字，
        # 不说清"最多多少步"就等于让人签一张空白支票。
        max_steps = getattr(self.engine, "max_steps", None)
        budget = (
            f"最多 {self.goal_turns} 回合 × 每回合 {max_steps} 步"
            f" = {self.goal_turns * max_steps} 步"
            if max_steps else f"最多 {self.goal_turns} 回合"
        )
        typer.secho(
            f"开始自动推进（{budget}）；跑完这一拍会停下来把提示符还给你。"
            "期间想插话就先 Ctrl+C，或者用 /goal pause。",
            fg=typer.colors.BRIGHT_BLACK,
        )

    def _goal_status(self) -> None:
        if self.state.goal is None:
            typer.secho(
                "本会话没有目标。用 /goal <目标> --check <命令> 设一个。",
                fg=typer.colors.BRIGHT_BLACK,
            )
            return
        typer.echo(render_goal(self.state.goal))

    def _goal_pause(self, reason: str) -> None:
        goal = self.state.goal
        if goal is None:
            typer.secho("本会话没有目标", fg=typer.colors.YELLOW)
            return
        if goal.status == GOAL_DONE:
            typer.secho("目标已经完成，无需暂停", fg=typer.colors.YELLOW)
            return
        if goal.status == GOAL_PAUSED:
            # 再次 pause 会**覆盖**掉原来的原因，而那个原因往往是唯一能解释
            # "它为什么停了"的东西 —— 所以先说出来再拒绝。
            typer.secho(
                f"目标已经是暂停状态（原因: {goal.pause_reason or '未记录'}）",
                fg=typer.colors.BRIGHT_BLACK,
            )
            return
        # 人显式暂停：`auto=False`，轨迹里与"自动停下来"区分开。
        self._pause_goal(reason or "人工暂停（/goal pause）", auto=False)

    def _goal_resume(self) -> None:
        goal = self.state.goal
        if goal is None:
            typer.secho("本会话没有目标", fg=typer.colors.YELLOW)
            return
        if goal.status == GOAL_DONE:
            # 完成是**终态**：检查已经通过，再把它翻回 active 等于让同一个目标
            # 可以反复"完成"一次。要接着干就设一个新目标。
            typer.secho(
                "目标已完成（检查已通过），不能恢复。要接着干请 /goal clear 后设新目标",
                fg=typer.colors.YELLOW,
            )
            return
        if goal.status == GOAL_ACTIVE:
            typer.secho("目标本来就在推进中", fg=typer.colors.BRIGHT_BLACK)
            return
        goal.status = GOAL_ACTIVE
        # ★ **必须清掉**：不清的话 `/goal status` 会永远显示一条过期的暂停原因，
        # 而那条原因在恢复之后已经完全不成立了（比没有原因更误导）。
        goal.pause_reason = None
        self.state.record_event("goal_resumed", objective=goal.objective)
        typer.secho("目标已恢复，继续自动推进", fg=typer.colors.GREEN)

    def _goal_clear(self) -> None:
        goal = self.state.goal
        if goal is None:
            typer.secho("本会话没有目标", fg=typer.colors.BRIGHT_BLACK)
            return
        # **置 None 而不是置 done**：两者差别是实质的 —— done 是"检查通过了"，
        # 而 clear 是"我不要这个目标了"，把它写成 done 就会在轨迹里留下一句
        # 没发生过的成功。
        self.state.goal = None
        self.state.record_event("goal_cleared", objective=goal.objective)
        typer.secho(f"已丢弃目标: {goal.objective}", fg=typer.colors.GREEN)

    def _cmd_sessions(self, arg: str) -> None:
        _print_sessions(self.workspace_root)

    def _cmd_rename(self, arg: str) -> None:
        if not arg:
            typer.secho("用法: /rename <名字>", fg=typer.colors.YELLOW)
            return
        try:
            set_session_name(self.workspace_root, self.session.session_id, arg)
        except ValueError as exc:
            typer.secho(f"改名失败: {exc}", fg=typer.colors.YELLOW)
            return
        typer.secho(f"会话改名: {self.session.session_id} → {arg}", fg=typer.colors.GREEN)

    def _cmd_clear_taint(self, arg: str) -> None:
        if self.state.taint == TAINT_NONE:
            typer.secho("污染标记本来就是 none，无需复位", fg=typer.colors.BRIGHT_BLACK)
            return
        # 复位只由**人**的显式动作触发（这条命令就是那个动作）。它同时往轨迹里
        # 记一条 taint_cleared，所以下次从这个检查点恢复时同样有效。
        self.state.clear_taint(reason="repl:/clear-taint")
        typer.secho("污染标记已复位为 none", fg=typer.colors.GREEN)

    # ---------- 内部 ----------

    def _activate(self, session: Session, state=None) -> None:
        """把"当前会话"整体切到 `(session, state)`。

        **三样一起换** —— session、state、**engine** —— 因为 `QueryEngine.session`
        是构造期绑定的。只换前两样的话，轨迹与检查点会写进旧会话（engine 还认着
        旧的那个），而提示符上显示的是新的：一个没有任何报错的错配，事后只能靠
        翻 `data/` 发现。

        `state=None` 表示"给这个会话开一份全新的状态"（`/new`）。
        """
        engine = self.runtime.engine(session)
        if state is None:
            state = engine.new_state("")
        # 先接事件出口，再让调用方记任何事件（恢复期的 plan_resumed 等）——
        # 顺序反了那些事件就进不了 JSONL（M8 修过的同型问题）。
        state.emitter = session.emit
        self.session, self.state, self.engine = session, state, engine

    def _new_session(self) -> Session:
        return Session(
            self.workspace_root,
            # `unique_session_id` 而不是 `new_session_id`：后者的粒度是**秒**，
            # 而常驻进程里 `/new` 紧接着 `/new`、或者一次快速演示里连开两个会话，
            # 完全可能落在同一秒。撞了的话 `Session.__init__` 不报错，两个"不同"
            # 的会话直接共用同一个检查点目录 —— 后者覆盖前者，且全程静默。
            # 单发路径上这个碰撞要靠"人手动重跑"才会发生，所以从没暴露过。
            unique_session_id(self.workspace_root),
            # 新会话没有"当初的值"可沿用，省略就是 CLI 的默认节拍
            checkpoint_every=(
                DEFAULT_CHECKPOINT_EVERY
                if self.checkpoint_every is None
                else self.checkpoint_every
            ),
        )

    def _prompt(self) -> str:
        bits = [
            _session_label(self.workspace_root, self.session.session_id),
            f"step {self.state.step}",
        ]
        if self.state.taint != TAINT_NONE:
            # 污染级别只在非 none 时显示：常显一个 "none" 会让人不再看这一段，
            # 等它真的变成 high 时也照样不看。
            bits.append(self.state.taint)
        if self.state.goal is not None:
            # 同理只在**有目标**时显示：目标不是默认状态，而它一旦存在就改变
            # 了提示符的行为（可能被自动推进）。状态值本身要露出来 ——
            # `active` 与 `paused` 下面会发生完全不一样的事。
            bits.append(f"goal:{self.state.goal.status}")
        return f"codeagent [{' · '.join(bits)}] › "

    def _announce_taint(self, state) -> None:
        if state.taint != TAINT_NONE:
            typer.secho(
                f"污染标记: {state.taint}（由轨迹里的 security_finding 重算；"
                f"用 /clear-taint 复位）",
                fg=typer.colors.YELLOW,
            )

    def _report(self, result, before_usage, before_step: int) -> None:
        """打印这一回合的结论 + **本回合**（不是会话累计）的用量。

        `RunResult.steps` / `RunResult.usage` 都是**会话累计**的（loop 里五个返回点
        给的都是 `state.step` / `state.usage`）。REPL 每回合要报的是增量，用回合
        前后的快照相减得到 —— 而不是让 loop 再维护第二套"本回合用量"的记账，
        那就是「两处各写一遍 → 漂移」。
        """
        if result.terminated_reason == "await_user":
            # 提问**不是结论**：用「结论」的样式打印它，人会以为任务跑完了，
            # 而这个回合的意义恰恰是"还没完，等你一句话"。
            #
            # 这里**不需要任何特殊机制**：模型提问 → 本轮结束 → 打印问题 → 下一行
            # 输入就是回答。这正是 M8「把打断建模成数据标志而不是阻塞控制流」的回报
            # —— `await_user` 只是一个终止原因，REPL 的循环天然能接住它。
            typer.secho("需要你补充信息:", fg=typer.colors.MAGENTA, bold=True)
            typer.echo(result.final_text or "（问题内容为空）")
            typer.secho(
                "（直接输入你的回答即可 —— 它就是这个回合的回复）",
                fg=typer.colors.BRIGHT_BLACK,
            )
        elif result.terminated_reason == REASON_GOAL_CHECK_INVALID:
            # 「判定无效」既不是成功也不是失败，而它**最容易被人当成跑完了**
            # （没有报错、退出码 0、还有一段像结论的话）。所以它单独一种样式，
            # 而且要把"该人出手了"说出来。
            typer.secho(
                "目标未完成：完成检查没能执行（本次判定不计入）",
                fg=typer.colors.YELLOW, bold=True,
            )
            typer.echo(result.final_text or "（无说明）")
        elif result.terminated_reason == REASON_GOAL_DONE:
            typer.secho("★ 目标完成（完成检查通过）:", fg=typer.colors.GREEN, bold=True)
            typer.echo(result.final_text or "（无结论）")
        else:
            typer.secho("结论:", fg=typer.colors.GREEN, bold=True)
            typer.echo(result.final_text or "（无结论）")

        delta = self.state.usage - before_usage
        ratio = delta.cache_hit_ratio
        cache_line = f"，缓存命中 {ratio:.0%}" if ratio is not None else ""
        ctx_line = ""
        stats = getattr(self.runtime.context, "last_stats", None)
        if stats is not None:
            ctx_line = f"，上下文 {stats.warning_level} ({stats.utilization:.0%})"
        checkpoints = self.session.list_checkpoints()
        cp_line = f"，检查点 {len(checkpoints)} 个" if checkpoints else ""
        typer.secho(
            f"[{result.terminated_reason}] 本回合 step {before_step}→{self.state.step}"
            f" · token {delta.total_tokens}"
            f"（prompt {delta.prompt_tokens} + completion {delta.completion_tokens}）"
            f"{cache_line}{ctx_line}{cp_line}",
            fg=typer.colors.BRIGHT_BLACK,
        )

    def _farewell(self, reason: str) -> None:
        """退出语。**必须给出可执行的续跑命令** —— M7 的教训是一条走不通的
        指引比没有指引更糟（当时文案让人 `--clear-taint` 复位后重试，而 CLI
        根本送不进人的回复）。这里给的是一条真的能跑起来的命令。
        """
        typer.secho(f"退出（{reason}）。", fg=typer.colors.BRIGHT_BLACK)
        typer.secho(
            f"会话 {_session_label(self.workspace_root, self.session.session_id)}"
            f" 停在 step {self.state.step}。",
            fg=typer.colors.BRIGHT_BLACK,
        )
        typer.secho(
            f"继续: python -m app.cli --repl --resume --session-id "
            f"{self.session.session_id}",
            fg=typer.colors.CYAN,
        )

    def _wrap_up(self) -> None:
        """退出时：强制落一次检查点，再做一次约定提炼（M4-1）。

        **先落盘再提炼**，而且无论如何都要落：`_farewell` 刚刚对着用户承诺了
        「继续: … --repl --resume --session-id <sid>」，而检查点是**按节拍**写的
        （默认 5 步）且只在**有工具调用**的步上 tick。纯聊天、或者只走了两三步
        工具就退出 —— 都写不出检查点，那条命令跑起来会直接报"读不到检查点"。
        这和 `_awaiting_user` 里 force 的理由是同一个：流程即将因非步数原因退出，
        节流的下一 tick 永远等不来。M7 的教训就是一条走不通的指引比没有更糟。

        提炼放在退出时而不是每个回合：`extract_and_learn` 是一次真实的模型调用，
        而它读的是**累计**轨迹 —— 每回合跑一遍等于把同一段对话提炼 N 次。单发
        路径也是"一次运行提炼一次"，这里对齐它。
        """
        if self.session is not None and hasattr(self.session, "checkpoint"):
            self.session.checkpoint(self.state, force=True)
        if self._turns == 0:
            return  # 一步没跑，没有可提炼的东西（也省一次模型调用）
        # 用 `derive_taint` 重放而不是直接读 `state.taint`：与 `RunResult.taint`
        # 和 `--resume` 走同一个函数，"跑完的会话"与"恢复出来的同一会话"必然给
        # 一样的答案。没有相关事件（重放返回 None）时回退到状态里的值。
        taint = derive_taint(self.state.events) or self.state.taint
        _extract_learned(
            self.runtime, self.state.events, taint,
            terminated_reason=self._last_reason,
        )


def run_repl(
    runtime: _Runtime,
    *,
    task: str = "",
    start_session_id: str | None = None,
    resume: bool = False,
    step: int | None = None,
    checkpoint_every: int | None = None,
    clear_taint: bool = False,
    goal_turns: int = DEFAULT_GOAL_TURNS,
) -> None:
    """进入常驻交互模式（`app/cli.py --repl`）。

    `task` 非空时它**作为第一个回合**跑掉再进提示符 —— 这样"我有个任务，
    跑完接着聊"和"我进来随便看看"是同一条路径，而不是两个入口。
    """
    repl = Repl(
        runtime,
        checkpoint_every=checkpoint_every,
        clear_taint=clear_taint,
        goal_turns=goal_turns,
    )
    repl.start(start_session_id=start_session_id, resume=resume, step=step)
    if task.strip():
        repl.run_turn(task)
    repl.loop()
