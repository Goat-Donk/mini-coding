"""M9-6 Goal tests：agent/goal.py + agent/tools/goal.py + 完成检查 + REPL 命令族。

**每组测试都对着机制存在的理由**（而不是对着实现）：

- **判分权在人手里**：模型只能*声明*，检查命令由人预先给定，退出码说了算；
  模型既不能自造目标、也不能改判据、也没有 pause/clear 工具。
- **判定是三态，不是两态**：命令**没跑成**（被门禁拦下 / 超时 / 工具异常）时，
  「算完成」和「算没完成」都是错的 —— 那是 `CHECK_INVALID`，与 `CHECK_FAILED`
  分开走不同的回合语义。
- **「一次声明恰好跑一次检查」是结构性保证**：声明字段在批前被复位、判定的
  那一刻被取走就清 —— 两条防线都断了才会重跑（烧钱 + 上下文爆）。
- **「一拍 = 一次授权」**：自动推进跑到上限**一定暂停**，人敲一行字时目标
  必然不是 active —— 否则按一次回车就立刻再起一拍，人再也敲不进第二行。
- 目标**一个字都不进 system**（messages[0]）：那是 `_PREFIX_LEN` 保护区，
  一变 = 前缀缓存永久失效。

脚手架：`make_state()` + 注册成 `bash` 的 `StubBash`（可编程 `ToolResult`，
保证确定性）；另有一条用**真 BashTool + 真 PermissionsEngine** 的通路测试。
"""
from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel

from agent.goal import (
    CHECK_FAILED,
    CHECK_INVALID,
    CHECK_PASSED,
    GOAL_ACTIVE,
    GOAL_CHECK_TIMEOUT,
    GOAL_DONE,
    GOAL_PAUSED,
    Goal,
    goal_can_advance,
    render_goal,
    tail_lines,
)
from agent.llm import LLMResult, MockLLM, ToolCall
from agent.loop import QueryEngine, REASON_GOAL_CHECK_INVALID, REASON_GOAL_DONE
from agent.session import Session
from agent.state import AgentState, system
from agent.tools.base import Tool, ToolContext, ToolRegistry, ToolResult
from agent.tools.goal import DeclareGoalDoneTool, build_goal_tools


def ctx(tmp_path: Path, state: object | None = None) -> ToolContext:
    return ToolContext(workspace_root=tmp_path, state=state)


def make_state() -> AgentState:
    return AgentState(session_id="t", task="任务", system_prompt="sys")


class _BashInput(BaseModel):
    command: str
    cwd: str | None = None
    timeout: int = 120


class StubBash(Tool):
    """可编程的 bash 替身：退出码 / 输出 / 是否"没跑成"，全部由测试决定。

    `fail` 非 None 时返回 `ToolResult.fail`（对应超时/工具异常 → 判定无效），
    与"命令跑了但退出码非 0"（判定未通过）**必须能区分开** —— 这正是三态
    判定的全部意义。
    """

    name = "bash"
    description = "测试替身（可编程退出码）"
    input_model = _BashInput

    def __init__(self, exit_code: int = 0, output: str = "", *, fail: str | None = None):
        self.exit_code = exit_code
        self.output = output
        self.fail = fail
        self.seen: list[dict] = []
        self.calls = 0

    def execute(self, args: _BashInput, ctx: ToolContext) -> ToolResult:
        self.calls += 1
        self.seen.append({"command": args.command, "timeout": args.timeout})
        if self.fail is not None:
            return ToolResult.fail(self.fail)
        out = self.output + f"\n[exit code: {self.exit_code}]"
        return ToolResult.ok(
            out,
            data={
                "exit_code": self.exit_code,
                "stdout": self.output,
                "stderr": "",
                "truncated": False,
            },
        )


def make_goal_engine(
    tmp_path: Path,
    llm: MockLLM,
    *,
    bash: StubBash | None = None,
    permissions=None,
) -> QueryEngine:
    """注册成 `bash` 的 StubBash + 目标工具的最小引擎（无 hooks、无 session）。"""
    registry = ToolRegistry()
    registry.register(bash if bash is not None else StubBash())
    for goal_tool in build_goal_tools():
        registry.register(goal_tool)
    return QueryEngine(
        llm,
        registry,
        workspace_root=tmp_path,
        max_steps=5,
        loop_detection_window=50,  # 排除循环检测干扰（declare 签名可能重复）
        permissions=permissions,
    )


# ---------- 纯单元：Goal 数据类 / 判据 / 渲染 ----------

def test_goal_can_advance_only_when_active():
    """判据收在一处：active 才能推进，paused / done / 无目标都不行。"""
    assert goal_can_advance(None) is False
    assert goal_can_advance(Goal(objective="g", check_command="c")) is True
    assert goal_can_advance(Goal(objective="g", check_command="c", status=GOAL_PAUSED)) is False
    assert goal_can_advance(Goal(objective="g", check_command="c", status=GOAL_DONE)) is False


def test_render_goal_prints_the_check_command_verbatim():
    """判据原文永远印在结果旁边 —— S17（空转的检查命令）唯一的人可读缓解。"""
    goal = Goal(objective="让 test_textstat.py 全绿", check_command="python -m pytest tests/test_textstat.py -v")
    text = render_goal(goal)
    assert "python -m pytest tests/test_textstat.py -v" in text  # 原文，不是摘要
    assert "让 test_textstat.py 全绿" in text
    assert "active" in text


def test_render_goal_shows_pause_reason_only_when_paused():
    goal = Goal(objective="g", check_command="c", status=GOAL_PAUSED, pause_reason="等口径")
    text = render_goal(goal)
    assert "等口径" in text
    assert "/goal resume" in text
    plain = Goal(objective="g", check_command="c")
    assert "暂停原因" not in render_goal(plain)


def test_tail_lines_keeps_only_the_tail():
    text = "\n".join(f"line {i}" for i in range(100))
    tailed = tail_lines(text, limit=40)
    # 前缀「…（前 N 行已省略）」占一行，剩下的才是内容
    content_lines = [l for l in tailed.splitlines() if l.startswith("line")]
    assert len(content_lines) == 40
    assert tailed.splitlines()[0] == "...（前 60 行已省略）"
    assert "line 99" in tailed
    assert "line 0" not in tailed
    short = "只有两行\n第二行"
    assert tail_lines(short) == short  # 行数不超 = 原样，不做任何重排


# ---------- declare_goal_done 工具 ----------

def test_declare_schema_is_flat():
    """参数扁平、**没有 $defs** —— `base.py` 会静默 pop 掉 $defs，嵌套模型
    会让模型收到的参数说明是错的。"""
    schema = DeclareGoalDoneTool().schema()["function"]["parameters"]
    assert "$defs" not in schema
    assert "$defs" not in json.dumps(schema)
    assert schema["properties"]["summary"]["type"] == "string"
    assert schema["properties"]["evidence"]["type"] == "array"
    assert schema["properties"]["evidence"]["items"] == {"type": "string"}
    assert set(schema["properties"]) == {"summary", "evidence"}


def test_declare_writes_declaration_into_state(tmp_path):
    """声明是**数据**（写 state.goal.declaration），不是 ToolResult 上的标志。

    ToolResult 在批结束时已被 `compact_batch` 吞掉 —— 第二个标志过不去
    `_execute_tool_calls` 的返回类型，而那是四个入口共用的核心循环契约。
    """
    state = make_state()
    state.goal = Goal(objective="g", check_command="check-cmd")
    result = DeclareGoalDoneTool().run(
        {"summary": "全绿了", "evidence": ["pytest 0 失败"]}, ctx(tmp_path, state)
    )
    assert result.success
    assert result.await_user is False  # 不是 ask_user 那种控制流标志
    assert state.goal.declaration["summary"] == "全绿了"
    assert state.goal.declaration["evidence"] == ["pytest 0 失败"]
    assert state.goal.declaration["step"] == state.step


def test_declare_without_a_goal_fails_loudly(tmp_path):
    """无目标 → fail 并指向 `/goal`。**模型不能自造目标**（判分权在人手里）。"""
    state = make_state()  # goal 是 None
    result = DeclareGoalDoneTool().run({"summary": "我觉得做完了"}, ctx(tmp_path, state))
    assert not result.success
    assert "/goal" in result.output
    assert state.goal is None  # 没有借机建一个


def test_declare_rejected_when_already_done(tmp_path):
    state = make_state()
    state.goal = Goal(objective="g", check_command="c", status=GOAL_DONE)
    result = DeclareGoalDoneTool().run({"summary": "再来一次"}, ctx(tmp_path, state))
    assert not result.success
    assert state.goal.declaration is None


def test_declare_rejected_when_paused(tmp_path):
    """暂停期间不跑检查 —— 暂停的语义就是"停掉自动推进、由人接手"。"""
    state = make_state()
    state.goal = Goal(objective="g", check_command="c", status=GOAL_PAUSED, pause_reason="等人")
    result = DeclareGoalDoneTool().run({"summary": "暂停中还声明"}, ctx(tmp_path, state))
    assert not result.success
    assert "/goal resume" in result.output
    assert state.goal.declaration is None


def test_declare_rejects_empty_summary(tmp_path):
    state = make_state()
    state.goal = Goal(objective="g", check_command="c")
    result = DeclareGoalDoneTool().run({"summary": ""}, ctx(tmp_path, state))
    assert not result.success
    assert state.goal.declaration is None


# ---------- 完成检查：_run_loop 里的三态判定 ----------

def test_declare_then_exit_zero_marks_done_and_ends_the_turn(tmp_path):
    """声明 → 走门禁跑**人给定的命令** → 退出码 0 → done + 回合结束。

    MockLLM 只有一条响应：回合被 `goal_done` 结束掉之后 loop 不再调用模型
    （否则会抛「响应已耗尽」）。「通过就结束回合」让完成由**运行时**给出，
    而不是让模型再写一段"我完成了"。
    """
    bash = StubBash(exit_code=0, output="全部通过")
    state = make_state()
    state.goal = Goal(objective="让 test_x 全绿", check_command="python -m pytest tests/test_x.py -q")
    engine = make_goal_engine(
        tmp_path,
        MockLLM.script(
            MockLLM.tool("declare_goal_done", {"summary": "修好了", "evidence": ["本地全绿"]}).responses[0],
        ),
        bash=bash,
    )
    result = engine.run_turn(state, "开始")

    assert result.terminated_reason == REASON_GOAL_DONE
    assert state.goal.status == GOAL_DONE
    assert state.goal.pause_reason is None
    assert state.goal.declaration is None  # 瞬态字段取走就清
    checks = [e for e in state.events if e["type"] == "goal_check"]
    assert len(checks) == 1
    assert checks[0]["verdict"] == CHECK_PASSED
    assert checks[0]["exit_code"] == 0
    assert [e for e in state.events if e["type"] == "goal_completed"]
    # 检查跑的**就是** goal.check_command 原文，且带了超时上限
    assert bash.seen[0]["command"] == "python -m pytest tests/test_x.py -q"
    assert bash.seen[0]["timeout"] == GOAL_CHECK_TIMEOUT


def test_failed_check_keeps_active_and_feeds_back_via_user(tmp_path):
    """退出码非 0 → **保持 active、本轮继续**，判定以 user 消息回喂。

    结束回合会让模型失去修复机会 —— 而"检查没过 → 看输出 → 接着修"正是这条
    动线的全部价值。
    """
    bash = StubBash(exit_code=1, output="1 failed")
    state = make_state()
    state.goal = Goal(objective="g", check_command="python -m pytest -q")
    fed: list[str] = []

    def then_continue(messages, tools):  # noqa: ANN001 - MockLLM 回调签名
        fed.extend(
            m["content"] for m in messages
            if m.get("role") == "user" and isinstance(m.get("content"), str)
        )
        return LLMResult(content="我继续修")

    engine = make_goal_engine(
        tmp_path,
        MockLLM.script(
            MockLLM.tool("declare_goal_done", {"summary": "我宣布完成"}).responses[0],
            then_continue,
        ),
        bash=bash,
    )
    result = engine.run_turn(state, "干活")

    assert result.terminated_reason == "completed"  # 本轮走完，没有被 goal 掐断
    assert state.goal.status == GOAL_ACTIVE
    assert not [e for e in state.events if e["type"] == "goal_completed"]
    checks = [e for e in state.events if e["type"] == "goal_check"]
    assert checks[0]["verdict"] == CHECK_FAILED
    assert checks[0]["exit_code"] == 1
    # 判定回喂成 user 消息（不是伪造 assistant/tool 对）→ 模型看得到、能接着修
    assert any("完成检查未通过" in s for s in fed), fed
    assert any("python -m pytest -q" in s for s in fed), "判据原文要在回喂里"
    assert any("1 failed" in s for s in fed), fed


def test_check_blocked_by_gate_is_invalid_not_failed(tmp_path):
    """门禁拦下 → `invalid` 而非 `failed`（对齐 eval 的 `executed` 教训）。

    命令压根没跑成时，「算完成」和「算没完成」都是错的。阻断路径会先记一条
    `gate_block`（带 source/reason）再返回 fail —— 判定**拿事件切片**，
    不靠猜错误文本。
    """
    from agent.permissions import Decision

    class DenyBash:
        def check(self, name, arguments, ctx, *, details=None):  # noqa: ANN001
            return Decision.DENY if name == "bash" else Decision.ALLOW

        def describe(self, name, arguments, *, details=None, ctx=None):  # noqa: ANN001
            return "全部拒绝（测试替身）"

    state = make_state()
    state.goal = Goal(objective="g", check_command="python -m pytest -q")
    engine = make_goal_engine(
        tmp_path,
        MockLLM.script(
            MockLLM.tool("declare_goal_done", {"summary": "s"}).responses[0],
        ),
        bash=StubBash(exit_code=0),  # 就算 bash 本来会成功，被拦下也判无效
        permissions=DenyBash(),
    )
    result = engine.run_turn(state, "干活")

    assert result.terminated_reason == REASON_GOAL_CHECK_INVALID
    assert state.goal.status == GOAL_ACTIVE  # 无效不置 done
    assert not [e for e in state.events if e["type"] == "goal_completed"]
    checks = [e for e in state.events if e["type"] == "goal_check"]
    assert checks[0]["verdict"] == CHECK_INVALID
    assert checks[0]["exit_code"] is None
    gates = [e for e in state.events if e["type"] == "gate_block"]
    assert gates and gates[0]["source"] == "permissions", gates
    assert gates[0]["reason"]


def test_check_that_does_not_run_is_invalid(tmp_path):
    """命令没跑成（超时/工具异常的形状）→ invalid，回喂说清"不计入"。"""
    bash = StubBash(exit_code=0, fail="命令超时(120s)，已终止")
    state = make_state()
    state.goal = Goal(objective="g", check_command="python -m pytest -q")
    engine = make_goal_engine(
        tmp_path,
        MockLLM.script(
            MockLLM.tool("declare_goal_done", {"summary": "s"}).responses[0],
        ),
        bash=bash,
    )
    result = engine.run_turn(state, "干活")

    assert result.terminated_reason == REASON_GOAL_CHECK_INVALID
    assert "不计入" in result.final_text
    checks = [e for e in state.events if e["type"] == "goal_check"]
    assert checks[0]["verdict"] == CHECK_INVALID
    assert "超时" in str(checks[0]["reason"])


def test_one_declaration_triggers_exactly_one_check(tmp_path):
    """一次声明**恰好**跑一次检查。

    声明字段在批前复位 + 判定时取走就清 —— 两条防线都断了才会在后续每一步
    重跑完成检查（烧钱 + 上下文爆，还不报错）。步 2 是普通 bash 调用、没有
    声明：不该再出 goal_check。
    """
    bash = StubBash(exit_code=1, output="还没好")
    state = make_state()
    state.goal = Goal(objective="g", check_command="python -m pytest -q")
    engine = make_goal_engine(
        tmp_path,
        MockLLM.script(
            MockLLM.tool("declare_goal_done", {"summary": "第一次"}).responses[0],
            MockLLM.tool("bash", {"command": "type README"}).responses[0],
            LLMResult(content="结束"),
        ),
        bash=bash,
    )
    result = engine.run_turn(state, "干活")

    assert result.terminated_reason == "completed"
    checks = [e for e in state.events if e["type"] == "goal_check"]
    assert len(checks) == 1, f"一次声明只能触发一次检查: {checks}"
    assert bash.calls == 2  # 两次 bash 调用里只有一次是完成检查
    assert state.goal.declaration is None  # 判定后瞬态字段已经清掉


def test_check_beats_ask_user_in_the_same_step(tmp_path):
    """同一步既声明又提问 → **检查先跑**（goal_done 赢）。

    运行时的**事实**优先于模型的**陈述**；两个结论都进轨迹（提问的 tool_call
    记录 await_user=True，只是 `_awaiting_user` 没有机会收尾）。
    """
    from agent.tools.ask import build_ask_tool

    registry = ToolRegistry()
    registry.register(StubBash(exit_code=0))
    for goal_tool in build_goal_tools():
        registry.register(goal_tool)
    registry.register(build_ask_tool())
    state = make_state()
    state.goal = Goal(objective="g", check_command="check-cmd")
    engine = QueryEngine(
        MockLLM.script(
            LLMResult(content=None, tool_calls=[
                ToolCall(id="c1", name="declare_goal_done", arguments={"summary": "s"}),
                ToolCall(id="c2", name="ask_user", arguments={"question": "还有个问题"}),
            ]),
        ),
        registry,
        workspace_root=tmp_path,
        max_steps=5,
        loop_detection_window=50,
    )
    result = engine.run_turn(state, "干活")

    assert result.terminated_reason == REASON_GOAL_DONE
    assert state.goal.status == GOAL_DONE
    assert not [e for e in state.events if e["type"] == "await_user"]  # 检查先赢
    calls = [e for e in state.events if e["type"] == "tool_call"]
    assert [c["name"] for c in calls] == ["declare_goal_done", "ask_user"]
    assert calls[1]["await_user"] is True


def test_failed_check_feeds_only_the_output_tail(tmp_path):
    """回喂只带输出**尾部**，不带全量 —— 一次 pytest 的输出顶爆上下文。"""
    many = "\n".join(f"line {i:03d}" for i in range(200))
    bash = StubBash(exit_code=1, output=many)
    state = make_state()
    state.goal = Goal(objective="g", check_command="python -m pytest -q")
    fed: list[str] = []

    def then(messages, tools):  # noqa: ANN001
        fed.extend(
            m["content"] for m in messages
            if m.get("role") == "user" and isinstance(m.get("content"), str)
        )
        return LLMResult(content="好")

    engine = make_goal_engine(
        tmp_path,
        MockLLM.script(
            MockLLM.tool("declare_goal_done", {"summary": "s"}).responses[0],
            then,
        ),
        bash=bash,
    )
    engine.run_turn(state, "干活")

    full = many + "\n[exit code: 1]"
    checks = [e for e in state.events if e["type"] == "goal_check"]
    assert checks[0]["output_tail"] == tail_lines(full)
    assert checks[0]["output_chars"] == len(full)
    assert any("line 199" in s for s in fed), "尾部在回喂里"
    assert not any("line 000" in s for s in fed), "前 160 行被省略"


def test_goal_never_enters_messages_zero(tmp_path):
    """目标**一个字都不进 system**（messages[0]）：一变 = 前缀缓存永久失效。

    `system` 是 `messages[0]`、在 `_PREFIX_LEN` 保护区内；目标是会话中途
    创建的，注入就得回改第 0 条。
    """
    state = AgentState(
        session_id="t", task="任务", system_prompt="sys", messages=[system("sys")]
    )
    first = state.messages[0]
    state.goal = Goal(objective="g", check_command="check-cmd")
    engine = make_goal_engine(
        tmp_path,
        MockLLM.script(
            MockLLM.tool("declare_goal_done", {"summary": "s"}).responses[0],
        ),
        bash=StubBash(exit_code=0),
    )
    engine.run_turn(state, "干活")

    assert state.messages[0] == first
    assert state.messages[0]["role"] == "system"
    assert "check-cmd" not in json.dumps(state.messages[0])
    assert "目标" not in json.dumps(state.messages[0])


def test_real_bash_check_runs_through_the_gate_chain(tmp_path):
    """真 BashTool + 真 PermissionsEngine：检查**真的**过门禁链。

    第一条：一条必然 exit 0 的命令 → done，证明检查走的是真 bash 工具。
    第二条：危险命令被权限链拦下 → gate_block 带 source/reason（那是直接
    `tool.run` 永远不会有的东西）→ 判定 invalid。
    """
    from agent.tools.bash import BashTool

    from agent.permissions import PermissionsEngine

    def build(permissions):
        registry = ToolRegistry()
        registry.register(BashTool())
        for goal_tool in build_goal_tools():
            registry.register(goal_tool)
        state = make_state()
        return QueryEngine(
            MockLLM.script(
                MockLLM.tool("declare_goal_done", {"summary": "s"}).responses[0],
            ),
            registry,
            workspace_root=tmp_path,
            max_steps=5,
            loop_detection_window=50,
            permissions=permissions,
        ), state

    # ① 真跑：exit 0 → done
    engine, state = build(PermissionsEngine(tmp_path))
    state.goal = Goal(objective="g", check_command='python -c "print(1)"')
    result = engine.run_turn(state, "干活")
    assert result.terminated_reason == REASON_GOAL_DONE
    assert state.goal.status == GOAL_DONE

    # ② 危险命令（git push）被权限链拦下 → gate_block 带 source/reason → invalid
    engine2, state2 = build(PermissionsEngine(tmp_path))
    state2.goal = Goal(objective="g", check_command="git push origin main")
    result2 = engine2.run_turn(state2, "干活")
    assert result2.terminated_reason == REASON_GOAL_CHECK_INVALID
    gates = [e for e in state2.events if e["type"] == "gate_block"]
    assert gates and gates[0]["source"] == "permissions", gates
    assert gates[0]["reason"]  # ASK 无确认交互 → 安全默认拒绝，reason 是通用文案（CLI 无 denial_hint）
    # 危险命令原文在 goal_check 事件的 command 字段里（与 gate_block 一起构成因果链）
    checks = [e for e in state2.events if e["type"] == "goal_check"]
    assert checks[0]["command"] == "git push origin main"
    assert checks[0]["verdict"] == CHECK_INVALID
    assert [e for e in state2.events if e["type"] == "goal_completed"] == []


def test_goal_tool_not_in_the_default_registry(tmp_path):
    """目标工具**不进 `ToolRegistry.default()`** —— eval 用的正是它。

    headless 会话里没有人能用 `/goal` 创建目标，注册了就是个永远失败的诱饵
    （与 ask_user 完全同构）。
    """
    assert "declare_goal_done" not in ToolRegistry.default(tmp_path).names()


# ---------- 落盘跨回合 ----------

def test_paused_goal_survives_the_checkpoint_round_trip(tmp_path):
    """暂停状态跨进程往返后仍然门住一拍（不是只在内存里生效）。"""
    session = Session(tmp_path, "s-paused")
    state = make_state()
    state.goal = Goal(
        objective="g", check_command="check-cmd",
        status=GOAL_PAUSED, pause_reason="等口径", turns=2,
    )
    session.checkpoint(state, force=True)

    _, restored = Session.from_checkpoint(tmp_path, "s-paused")
    assert isinstance(restored.goal, Goal)  # 漏 _FIELD_DECODERS 时这里是个 dict
    assert restored.goal.status == GOAL_PAUSED
    assert restored.goal.pause_reason == "等口径"
    assert restored.goal.turns == 2
    assert goal_can_advance(restored.goal) is False


# ---------- REPL 命令族 ----------
#
# 脚手架与 test_repl.py 同款：走真 `_build_runtime`（顺带钉住「目标工具在
# 生产装配里注册了」这件事），只换 LLM 与输出。

def _runtime(tmp_path: Path, monkeypatch, llm):
    monkeypatch.setattr("app.cli._build_llm", lambda mock: llm)
    from agent.skills import discover_skills

    monkeypatch.setattr(
        "app.cli.discover_skills",
        lambda ws: discover_skills(ws, home=tmp_path / "home"),
    )
    from app.cli import _build_runtime

    return _build_runtime(tmp_path, mock=True, mcp=None, review_edits=False)


def _repl(tmp_path: Path, monkeypatch, llm):
    from app.repl import Repl

    repl = Repl(_runtime(tmp_path, monkeypatch, llm))
    repl.start()
    return repl


def _capture(monkeypatch) -> list[str]:
    """把 `app.repl` 的输出抓成行列表（typer 是同一个模块对象，cli 的也一起抓）。"""
    import app.repl

    lines: list[str] = []
    monkeypatch.setattr("app.repl.typer.echo", lambda msg="": lines.append(str(msg)))
    monkeypatch.setattr(
        "app.repl.typer.secho", lambda msg="", **_kw: lines.append(str(msg))
    )
    return lines


def test_goal_tool_is_registered_by_the_production_runtime(tmp_path, monkeypatch):
    runtime = _runtime(tmp_path, monkeypatch, MockLLM.text("x"))
    assert "declare_goal_done" in runtime.registry.names()


def test_create_goal_lands_verbatim(tmp_path, monkeypatch):
    """objective / check_command **逐字落库**（不顺手 strip、不规范化命令）。"""
    repl = _repl(tmp_path, monkeypatch, MockLLM.text("x"))
    _capture(monkeypatch)
    repl._cmd_goal("让 test_textstat.py 全绿 --check python -m pytest tests/test_textstat.py -q")

    goal = repl.state.goal
    assert goal is not None
    assert goal.objective == "让 test_textstat.py 全绿"
    assert goal.check_command == "python -m pytest tests/test_textstat.py -q"
    assert goal.status == GOAL_ACTIVE
    assert goal.created_step == 0
    assert [e for e in repl.state.events if e["type"] == "goal_created"]


def test_second_goal_is_rejected_while_one_is_active(tmp_path, monkeypatch):
    """已有未结束目标 → 拒绝第二个（旧检查命令不会无声消失）。"""
    repl = _repl(tmp_path, monkeypatch, MockLLM.text("x"))
    _capture(monkeypatch)
    repl._cmd_goal("第一个 --check cmd-a")
    repl._cmd_goal("第二个 --check cmd-b")

    assert repl.state.goal.objective == "第一个"  # 没被替换
    assert repl.state.goal.check_command == "cmd-a"
    assert not [e for e in repl.state.events if e["type"] == "goal_created"][1:]


def test_pause_stores_reason_and_resume_clears_it(tmp_path, monkeypatch):
    """resume **必须清 `pause_reason`** —— 不清的话 `/goal status` 永远显示
    一条过期原因，比没有原因更误导。"""
    repl = _repl(tmp_path, monkeypatch, MockLLM.text("x"))
    _capture(monkeypatch)
    repl._create_goal("g", "check-cmd")

    repl._goal_pause("等人确认口径")
    assert repl.state.goal.status == GOAL_PAUSED
    assert repl.state.goal.pause_reason == "等人确认口径"
    assert goal_can_advance(repl.state.goal) is False
    assert [e for e in repl.state.events if e["type"] == "goal_paused"]

    repl._goal_resume()
    assert repl.state.goal.status == GOAL_ACTIVE
    assert repl.state.goal.pause_reason is None  # ★ 必须清
    assert [e for e in repl.state.events if e["type"] == "goal_resumed"]


def test_resume_refuses_a_done_goal(tmp_path, monkeypatch):
    """完成是终态：再翻回 active = 同一个目标可以反复"完成"。"""
    repl = _repl(tmp_path, monkeypatch, MockLLM.text("x"))
    _capture(monkeypatch)
    repl._create_goal("g", "check-cmd")
    repl.state.goal.status = GOAL_DONE
    repl._goal_resume()
    assert repl.state.goal.status == GOAL_DONE


def test_clear_discards_the_goal_instead_of_marking_done(tmp_path, monkeypatch):
    """`/goal clear` → `state.goal is None`（**不是**置 done）。

    done 是"检查通过了"，clear 是"我不要这个目标了" —— 写成 done 就会在
    轨迹里留下一句没发生过的成功。
    """
    repl = _repl(tmp_path, monkeypatch, MockLLM.text("x"))
    _capture(monkeypatch)
    repl._create_goal("g", "check-cmd")
    repl._goal_clear()
    assert repl.state.goal is None
    assert not [e for e in repl.state.events if e["type"] == "goal_completed"]


def test_goal_usage_error_does_not_kill_the_repl(tmp_path, monkeypatch):
    repl = _repl(tmp_path, monkeypatch, MockLLM.text("x"))
    lines = _capture(monkeypatch)
    repl._dispatch("/goal 随便写一句")  # 没有 --check、首词也不是子命令
    assert not repl._done  # REPL 还活着
    assert repl.state.goal is None  # 也没有误建目标
    assert any("用法" in s for s in lines)
    assert any("未知的 /goal 用法" in s for s in lines)


def test_prompt_shows_goal_status_only_when_a_goal_exists(tmp_path, monkeypatch):
    repl = _repl(tmp_path, monkeypatch, MockLLM.text("x"))
    assert "goal:" not in repl._prompt()  # 没有目标就不显示
    repl._create_goal("g", "check-cmd")
    assert "goal:active" in repl._prompt()
    repl._goal_pause("等")
    assert "goal:paused" in repl._prompt()


# ---------- 一拍（自动推进） ----------

def test_burst_sends_kickoff_then_continuation(tmp_path, monkeypatch):
    """一拍里首回合发 kickoff、后续回合发 continuation —— 引导出现在目标
    被创建的那一刻（M8 elicitation gap 的正解形状）。"""
    seen: list[str] = []

    def responder(messages, tools):  # noqa: ANN001 - MockLLM 回调签名
        content = next(
            (
                m["content"] for m in reversed(messages)
                if m.get("role") == "user" and isinstance(m.get("content"), str)
                and "【" in m["content"]
            ),
            "",
        )
        seen.append(content)
        return LLMResult(content="推进中")

    repl = _repl(tmp_path, monkeypatch, MockLLM([responder, responder]))
    _capture(monkeypatch)
    repl.goal_turns = 2
    repl._create_goal("让测试全绿", "check-cmd")

    repl._run_goal_burst()

    assert len(seen) == 2
    assert "【目标】" in seen[0]
    assert "【继续推进目标】" in seen[1]
    assert repl.state.goal.turns == 2
    assert repl.state.goal.status == GOAL_PAUSED  # 到上限必停
    assert repl.state.goal.pause_reason  # 有可见的暂停原因


def test_burst_stops_on_a_stop_reason_before_the_limit(tmp_path, monkeypatch):
    """碰到 stop 原因（这里是模型提问）→ 立刻停，不把剩下回合烧完。"""
    runtime = _runtime(tmp_path, monkeypatch, MockLLM.script(
        MockLLM.tool("ask_user", {"question": "哪个口径？"}).responses[0],
    ))
    # 生产装配里 ask_user 已注册（_build_runtime 的 _ASK_USER_HINT 也依赖它）
    assert "ask_user" in runtime.registry.names()

    from app.repl import Repl

    repl = Repl(runtime)
    repl.start()
    _capture(monkeypatch)
    repl.goal_turns = 3
    repl._create_goal("g", "check-cmd")

    repl._run_goal_burst()

    assert repl.state.goal.turns == 1  # 只跑了一回合就停在提问上
    assert repl.state.goal.status == GOAL_PAUSED
    assert "模型提问" in (repl.state.goal.pause_reason or "")


def test_paused_goal_does_not_burst_until_resumed(tmp_path, monkeypatch):
    """暂停的目标**不推进**（除了 done 都推进 = 暂停形同虚设）。"""
    llm = MockLLM.text("不该被调用")
    repl = _repl(tmp_path, monkeypatch, llm)
    _capture(monkeypatch)
    repl._create_goal("g", "check-cmd")
    repl._goal_pause("等人确认")

    repl._run_goal_burst()  # MockLLM 被调用会抛「响应已耗尽」→ 测试失败
    assert repl.state.goal.turns == 0
    assert repl.state.goal.status == GOAL_PAUSED

    repl._goal_resume()
    repl.goal_turns = 1
    repl._run_goal_burst()  # resume 后推进
    assert repl.state.goal.turns == 1


def test_loop_never_reads_input_while_the_goal_is_active(tmp_path, monkeypatch):
    """「一拍 = 一次授权」的结构保证：人的一行输入只会落在目标**非 active** 时。

    如果 loop 允许带着 active 的目标读输入，按一次回车就会立刻再起一拍、人再
    也敲不进第二行。这是"人敲一行字 → 自动推进停下"的可观察形态。
    """
    llm = MockLLM([LLMResult(content="自动推进回合")] * 4)
    repl = _repl(tmp_path, monkeypatch, llm)
    _capture(monkeypatch)
    repl.goal_turns = 1
    repl._create_goal("让测试全绿", "check-cmd")

    status_at_input: dict[str, str] = {}
    calls = {"n": 0}

    def fake_input(prompt):  # noqa: ANN001
        calls["n"] += 1
        if calls["n"] == 1:
            status_at_input["status"] = repl.state.goal.status
            return "帮我修一个 bug"  # 人的一行
        raise EOFError

    monkeypatch.setattr("builtins.input", fake_input)
    repl.loop()

    assert status_at_input["status"] == GOAL_PAUSED  # 人拿到提示符时目标必然已停
    assert repl.state.goal.turns == 1  # 只有一拍
    assert repl.state.goal.status == GOAL_PAUSED  # 人的那行没有重新起拍


def test_burst_stop_reasons_cover_every_terminated_reason_except_completed():
    """自动推进必须在**每个**非 completed 终止原因上停下。

    新增终止原因却忘了让它停 = burst 白烧预算、不报任何错。这条是
    `TERMINATED_REASONS` 的第二个读者（`loop.py` 注释里写了）。
    """
    from agent.loop import TERMINATED_REASONS

    from app.repl import BURST_STOP_REASONS

    assert set(BURST_STOP_REASONS) | {"completed"} == TERMINATED_REASONS


def test_every_burst_stop_reason_has_a_human_reason():
    """每个会暂停的 stop 原因都翻成一句给人看的话。

    唯一例外是 `goal_done`：目标是**终态**，burst 碰见它时 `_pause_goal` 会因
    `goal_can_advance` 为 False 直接返回 —— 没有暂停发生、也就没有要给人看的
    暂停理由。给一条永远不会显示的文案写进 `_STOP_REASONS` 是给死代码加测试。
    """
    from agent.loop import REASON_GOAL_DONE

    from app.repl import BURST_STOP_REASONS, _STOP_REASONS

    for reason in BURST_STOP_REASONS - {REASON_GOAL_DONE}:
        assert reason in _STOP_REASONS, reason


# ---------- CLI：--goal 只读出口 ----------

def _seed_goal_session(tmp_path: Path, session_id: str, goal: Goal | None) -> None:
    session = Session(tmp_path, session_id)
    state = make_state()
    state.goal = goal
    session.checkpoint(state, force=True)


def test_goal_flag_prints_without_touching_the_llm(tmp_path, monkeypatch):
    """`--goal` 不需要 API key、不建会话、不跑任务 —— 目标只是检查点里的字段。

    与 `--plan` 同一条纪律：把 `_build_llm` 换成"一被调用就炸"，钉住它确实没
    被碰到 —— 不然"没 key 也能看目标"会在某次重构里悄悄失效。
    """
    from typer.testing import CliRunner

    from app.cli import app

    _seed_goal_session(
        tmp_path, "s-goal",
        Goal(
            objective="让 tests/test_textstat.py 全绿",
            check_command="python -m pytest tests/test_textstat.py -q",
            turns=3, status=GOAL_PAUSED, pause_reason="等口径",
        ),
    )
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))

    def boom(mock):  # noqa: ANN001
        raise AssertionError("--goal 不该构造 LLM")

    monkeypatch.setattr("app.cli._build_llm", boom)
    result = CliRunner().invoke(app, ["--goal", "--session-id", "s-goal"])

    assert result.exit_code == 0, result.output
    assert "让 tests/test_textstat.py 全绿" in result.output
    assert "python -m pytest tests/test_textstat.py -q" in result.output  # 判据原文
    assert "等口径" in result.output
    assert "s-goal" in result.output


def test_goal_flag_on_a_session_without_a_goal(tmp_path, monkeypatch):
    """会话存在、检查点也在，只是没设目标 → 说清楚"没有"，且说明去哪设。"""
    from typer.testing import CliRunner

    from app.cli import app

    _seed_goal_session(tmp_path, "s-no-goal", None)
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setattr("app.cli._build_llm", lambda mock: (_ for _ in ()).throw(
        AssertionError("--goal 不该构造 LLM")
    ))
    result = CliRunner().invoke(app, ["--goal", "--session-id", "s-no-goal"])

    assert result.exit_code == 0, result.output
    assert "没有目标" in result.output
    assert "/goal" in result.output


# ---------- 恢复补投（--resume 时的 _reinject_goal） ----------

def test_reinject_goal_appends_once_and_never_touches_messages_zero(tmp_path, monkeypatch):
    """`--resume` 时目标**补投一次**的纪律：追加一条 user、绝不碰 system。

    目标是会话中途创建的，注入就得回改 `messages[0]`（system，在 `_PREFIX_LEN`
    保护区里）—— 一动整条前缀缓存永久失效。具体可见行为：追加**一条** user 消息
    且目标原文在里、`messages[0]` 原封不动、记一条 `goal_resumed_in_context`。

    `_reinject_goal` 只有补投一条路：它被调用的**时机**（resume 时一次）才是
    「只补一次、不每轮贴」的所在，这里钉的是它在单次调用里不越界。
    """
    import app.cli

    from app.cli import _reinject_goal

    captured: list[str] = []
    monkeypatch.setattr(
        "app.cli.typer.secho", lambda msg="", **_kw: captured.append(str(msg))
    )
    state = make_state()
    state.messages = [system("sys")]
    first = state.messages[0]
    state.goal = Goal(
        objective="让 test_textstat 全绿", check_command="python -m pytest tests/test_textstat.py -q"
    )

    _reinject_goal(state)

    user_msgs = [m for m in state.messages if m["role"] == "user"]
    assert len(user_msgs) == 1, f"补投只追加一条 user 消息: {state.messages}"
    assert "让 test_textstat 全绿" in user_msgs[0]["content"]
    assert "python -m pytest tests/test_textstat.py -q" in user_msgs[0]["content"]
    assert state.messages[0] == first  # system 一个字没动（前缀缓存命脉）
    assert state.messages[0]["role"] == "system"
    assert [e for e in state.events if e["type"] == "goal_resumed_in_context"]
    assert any("目标恢复" in s for s in captured)
