"""进程内目标（Goal，M9-6）—— 跨回合推进 + 暂停/恢复 + 显式完成检查。

对应 TS 原版 ④（`cli-commands.ts` 的 `/goal` 命令族 + `goal/context.ts` 的
目标状态机）。**它不是任务树**：原版是一个带状态机的目标对象，没有任何
层级结构（笔记 `minicode-notes.md:141` 是唯一证据，里面逐字是「进程内 Goal
（跨回合推进 + 暂停/恢复/完成检查）」）。把"目标"想成树是很容易犯的错，
因为名字听起来像 —— 但证据里没有树，所以这里也不造一棵。

三件事决定了这个模块的形状：

**① 判分权在人手里。** 目标由人用 `/goal <目标> --check <命令>` 创建，
`check_command` 是**人预先给的可执行判据**。模型只能*声明*自己完成了
（`declare_goal_done`），运行时随即去跑那条命令，**退出码说了算**。
模型既不能创建目标、也不能改判据。这是这一项与「让模型自己说完成了」
的全部区别 —— 后者不是机制，是一句话。

**② 完成检查的判定是三态，不是两态。** 与 `eval/golden_tasks.py` 的
`JudgeResult.executed` 同一条教训：命令**压根没跑成**的时候，「算完成」和
「算没完成」都是错的。所以 `CHECK_FAILED`（跑了、退出码非 0）与
`CHECK_INVALID`（没跑成：被门禁拦下 / 超时 / 工具异常）是两个不同的值，
分开走不同的回合语义。

**③ 暂停/恢复只有在"有东西在自动跑"时才不是装饰。** 我们的架构里没有
定时器，所以 `paused` 这个状态必须有真实后果 —— 它的后果是 **REPL 不再
自动续跑这个目标**（见 `app/repl.py` 的 `_run_goal_burst`）。反过来，
「人在提示符敲了一行字」是一次**隐式暂停**：那一行是人接手的意思。

为什么单独一个模块而不是塞进 `agent/state.py`：state.py 的自我定位是
「会话状态 + 消息构造」（纯数据、零行为），而 Goal 带状态机迁移和三份
给不同读者用的渲染文本。为什么不是 `agent/tools/goal.py`：那样 state.py
要 import tools/，与 `tools/base.py` 里「底座依赖上层是反的」相悖。
本模块只 import 标准库 → 无环。
"""
from __future__ import annotations

from dataclasses import dataclass

# ---------- 状态机 ----------

GOAL_ACTIVE = "active"    # 可以自动推进
GOAL_PAUSED = "paused"    # 停下来等人（等人 / 到上限 / 上一回合没跑好）
GOAL_DONE = "done"        # 完成检查通过，终态

GOAL_STATUSES: tuple[str, ...] = (GOAL_ACTIVE, GOAL_PAUSED, GOAL_DONE)

# ---------- 完成检查的三态判定 ----------

#: 命令跑了，退出码 0。
CHECK_PASSED = "passed"
#: 命令跑了，退出码非 0。
CHECK_FAILED = "failed"
#: 命令**压根没跑成**（门禁拦下 / 超时 / 工具异常）。见模块 docstring ②。
#: 如实标注（README S20）：shell 没有 pytest 那种 2/3/4/5 退出码来区分
#: "测试没通过"与"测试根本没跑起来"，所以 `exit 127`（命令不存在）、
#: `No module named pytest` 都会被记成 **failed** 而不是 invalid。
CHECK_INVALID = "invalid"

CHECK_VERDICTS: tuple[str, ...] = (CHECK_PASSED, CHECK_FAILED, CHECK_INVALID)

# ---------- 常量 ----------

#: 一拍自动推进最多几个回合。**上限不是优化，是防锁死**：`input()` 是阻塞的、
#: 没有定时器，一拍期间人**根本敲不进字** —— 无界推进 = 把人锁在门外直到
#: 烧完额度。`--goal-turns` 可调，但调不掉这条约束本身。
DEFAULT_GOAL_TURNS = 3

#: 完成检查命令的秒数上限（`bash` 工具自身的上限是 300）。
GOAL_CHECK_TIMEOUT = 120

#: 回喂模型时保留的输出尾部行数。一次 pytest 的输出可以到几十万字符
#: （bash 的兜底上限是 `MAX_CHARS = 500_000`），原样回喂一次就顶爆上下文。
GOAL_CHECK_TAIL_LINES = 40


@dataclass
class Goal:
    """一个进程内目标。**权威状态本身**，随检查点一起走（同 `state.plan`）。

    刻意**不做** `derive_goal(events)`（对比 `taint` 那个派生值）：污染标记有
    第二个判据依赖它（权限天花板），所以必须有重放；目标的唯一消费者就是它
    自己和给人看的 `/goal status`。给一个没有第二个读者的东西加重放，
    只会多出一份可能对不上的真相。
    """

    objective: str                      # 人写的目标
    check_command: str                  # 人给的可执行完成判据（唯一权威）
    status: str = GOAL_ACTIVE
    pause_reason: str | None = None     # 仅 paused 时有意义；resume 必须清掉
    created_step: int = 0
    turns: int = 0                      # 累计自动推进过的回合数（给人看）
    #: 瞬态：模型刚刚声明的完成，由 `_run_loop` 取出后**立刻清空**。
    #: 它在批前被复位，所以「一次声明恰好触发一次检查」是结构性成立的，
    #: 而不是靠"记得别重复跑"。
    declaration: dict | None = None
    #: 最近一次判定的摘要（`/goal status` 用）。**权威记录在事件里**
    #: （`goal_check`），这里只是一份缓存 —— 但它没有第二个判据依赖，
    #: 所以不需要重放。
    last_check: dict | None = None


def goal_can_advance(goal: Goal | None) -> bool:
    """这个目标现在能不能自动推进。**判据收在一处。**

    写成函数而不是各处 `goal.status == GOAL_ACTIVE`：这条判据至少有三个读者
    （REPL 的循环条件、burst 的循环条件、`declare_goal_done` 的拒绝逻辑），
    散着写就会出现"某一处忘了判 done"这种静默不一致。
    """
    return goal is not None and goal.status == GOAL_ACTIVE


def render_goal(goal: Goal | None) -> str:
    """把目标渲染成给人看的文本。**唯一定义"目标长什么样"**（同 `render_plan`
    的纪律）：`/goal status`、`/goal` 创建后的回显、`--goal` 都用它。

    分散成几份的话格式会各自漂移，而"目标显示得对不对"没人会专门测
    （它不影响任何功能，只影响人看不看得懂）。
    """
    if goal is None:
        return "（本会话没有目标）"
    lines = [
        f"目标: {goal.objective}",
        f"  状态: {goal.status}",
        # 判据永远印在结果旁边 —— 这是「空转的检查命令」那条已知局限
        # （README S17）**唯一**的缓解手段：运行时只看退出码、不评价这条命令
        # 检查了什么，所以至少要让人一眼看见它是什么。
        f"  完成判据（人给的，退出码 0 才算完成）: {goal.check_command}",
        f"  自动推进: 已 {goal.turns} 回合"
        f"（建于 step {goal.created_step}）",
    ]
    if goal.last_check:
        lines.append("  " + _render_last_check(goal.last_check))
    if goal.status == GOAL_PAUSED and goal.pause_reason:
        lines.append(f"  暂停原因: {goal.pause_reason}")
        lines.append("  （继续自动推进: /goal resume）")
    return "\n".join(lines)


def _render_last_check(check: dict) -> str:
    verdict = check.get("verdict")
    step = check.get("step")
    exit_code = check.get("exit_code")
    if verdict == CHECK_PASSED:
        return f"最近判定: 通过 @step {step}"
    if verdict == CHECK_FAILED:
        return f"最近判定: 未通过（退出码 {exit_code}）@step {step}"
    return f"最近判定: 判定无效（{check.get('reason') or '命令没能执行'}）@step {step}"


# ---------- 给模型看的两条消息模板 ----------

def goal_kickoff_message(goal: Goal) -> str:
    """一拍自动推进的**第一个**回合发这条。

    **这是 M8 那个 elicitation gap 的正解形状**：引导必须出现在**目标被创建
    的那一刻**。`update_plan` 当年就是工具实现了、能落盘，但 system prompt
    一个字没提它，模型 6 步一次没调。

    为什么不给 `DEFAULT_SYSTEM_PROMPT` 加 `{goal_hint}` 槽位：加了就得按
    「注册了哪些工具」填（同 `_ASK_USER_HINT`），而目标工具在 CLI 两个入口
    都注册 → **每一个单发会话**都要为"一个不存在的目标"付 token，而且模型
    会去调一个注定失败的声明工具。目标恰恰有个天然的用户回合可以承载这段
    引导 —— 用它。
    """
    return "\n".join([
        f"【目标】{goal.objective}",
        "",
        "【完成判据】由人预先给出，你无法修改。运行时会在你声明完成后执行这条命令，"
        "**退出码为 0 才算完成**：",
        f"    {goal.check_command}",
        "",
        "现在开始推进这个目标。做法和平常一样：先探索、再动手、用工具实际验证。",
        "认为目标已经达成时，调 `declare_goal_done` 声明（附一句总结与证据）。"
        "运行时随即执行上面那条命令：",
        "- 退出码 0 → 目标完成，本回合结束；",
        "- 非 0 → 你会收到命令输出，继续修，修好后再声明一次。",
        "",
        "不要在没有实际验证过的情况下声明：谎报会被那条命令当场拆穿，"
        "并白白浪费一整轮。也不要自行宣布完成 —— 完成与否不是你说的话，是那条命令的退出码。",
    ])


def goal_continuation_message(goal: Goal) -> str:
    """同一拍里后续回合发这条（比 kickoff 短，但判据仍重述一遍）。"""
    return "\n".join([
        f"【继续推进目标】{goal.objective}",
        f"完成判据（人给的，退出码 0 才算完成）: {goal.check_command}",
        "",
        "上一回合没有结束这个目标。继续推进；认为达成时调 `declare_goal_done`。",
    ])


def render_check_report(
    goal: Goal, verdict: str, output: str = "", reason: str | None = None
) -> str:
    """把一次判定渲染成**回喂给模型**的文本（也用于事件摘要）。

    永远是 `user` 消息回喂，同 `_reinject_plan` 的先例：这是运行时给出的观察，
    不是模型自己发出的调用。**绝不**伪造 `assistant(tool_calls=[...]) +
    tool(...)` 消息对 —— 那等于写下一个模型从没发出过的调用，与
    `PAIRING_FILLER`「必须说实话」的纪律直接冲突。

    判据原文永远印在结果旁边（S17 的唯一缓解手段，同 `render_goal`）。
    """
    if verdict == CHECK_FAILED:
        return "\n".join([
            "【完成检查未通过】",
            f"目标: {goal.objective}",
            f"检查命令（人给的判据，退出码 0 才算完成）: {goal.check_command}",
            "",
            f"命令输出（末尾 {GOAL_CHECK_TAIL_LINES} 行）:",
            output or "（无输出）",
            "",
            "目标仍未完成。请根据上面的输出继续修，修好后再次调 `declare_goal_done`。",
        ])
    return "\n".join([
        "【完成检查未能执行】",
        f"目标: {goal.objective}",
        f"检查命令: {goal.check_command}",
        f"原因: {reason or '命令没能跑起来'}",
        "",
        "本次判定**不计入** —— 这条命令没能跑起来，所以它既不算完成、也不算未通过。",
        "你不能修改这条命令（判据由人给定）。请把上面这个情况如实写进结论，"
        "让人来决定怎么办。",
    ])


def tail_lines(text: str, limit: int = GOAL_CHECK_TAIL_LINES) -> str:
    """取末尾 `limit` 行（行数不超就直接返回原文，不做任何重排）。"""
    lines = text.splitlines()
    if len(lines) <= limit:
        return text
    return "\n".join([f"...（前 {len(lines) - limit} 行已省略）"] + lines[-limit:])
