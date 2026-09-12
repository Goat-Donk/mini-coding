"""tests: app/repl.py —— 常驻交互模式（M9-5）。

这一批测试的重点不是"命令能不能用"，而是**多回合才暴露的那几件事**：

- 第二个回合还看得见第一个回合的消息与工具结果（REPL 持有同一份 state）
- `max_steps` 是**每轮一份**预算（旧语义下第二回合一步都跑不了）
- 权限的 `allow_turn` **不跨回合**（旧代码里 `_turn` 从不清空 → 变成永久放行）
- `skill_discovery` 只记一次（旧代码每回合往轨迹里再写一份）
- 回合中途被打断后，下一回合自动补齐残缺的工具结果配对（不补就是 API 400）

以及两条硬不变量：**未知斜杠命令绝不发给模型**、**切换失败不半切换**。

前一半直接驱动 `Repl`（不开 stdin），后一半用 `CliRunner(input=...)` 走真 CLI
—— `tests/test_cli.py` 到今天为止没有任何 stdin 测试，这是第一条。
"""
from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from agent.llm import LLMResult, MockLLM, ToolCall, Usage
from agent.security import TAINT_HIGH
from agent.skills import discover_skills
from agent.state import assistant_tool_calls, tool_result
from app.cli import _build_runtime, app
from app.repl import _COMMANDS, Repl, help_text

runner = CliRunner()


# ---------- 脚手架 ----------

def _runtime(tmp_path: Path, monkeypatch, llm, *, review_edits: bool = False):
    """按生产路径装配一个 `_Runtime`（**不走 CLI 命令**，只走 `_build_runtime`）。

    刻意不复刻装配过程：这一条本身就是"REPL 与单发共用同一份装配"的测试 ——
    `--review-edits`、skills、ask_user、hooks 全都在里面。
    """
    monkeypatch.setattr("app.cli._build_llm", lambda mock: llm)
    # 把 home 钉到临时目录：`discover_skills` 默认会读**真实的** `~/.claude/skills`
    # （本机就有），不钉住的话断言会随这台机器上装了什么而变。
    monkeypatch.setattr(
        "app.cli.discover_skills",
        lambda ws: discover_skills(ws, home=tmp_path / "home"),
    )
    return _build_runtime(tmp_path, mock=True, mcp=None, review_edits=review_edits)


def _repl(
    tmp_path,
    monkeypatch,
    llm,
    *,
    review_edits: bool = False,
    checkpoint_every: int | None = None,
) -> Repl:
    runtime = _runtime(tmp_path, monkeypatch, llm, review_edits=review_edits)
    repl = Repl(runtime, checkpoint_every=checkpoint_every)
    repl.start()
    return repl


def _seed(ws: Path, name: str = "calc.py", body: str = "def add(a, b):\n    return a - b\n"):
    (ws / name).write_text(body, encoding="utf-8")
    return ws / name


def _capture(monkeypatch, module: str = "app.repl") -> list[str]:
    """把 `app.repl` 的输出抓成行列表（typer 是同一个模块对象，cli 的也一起抓）。"""
    lines: list[str] = []
    monkeypatch.setattr(f"{module}.typer.echo", lambda msg="": lines.append(str(msg)))
    monkeypatch.setattr(
        f"{module}.typer.secho", lambda msg="", **_kw: lines.append(str(msg))
    )
    return lines


def _text_responder(content: str, sink: list | None = None):
    """造一个 LLM 响应回调：把收到的 messages 记进 `sink`（若给了）再回文本。"""
    def responder(messages, tools):  # noqa: ANN001 - MockLLM 回调签名
        if sink is not None:
            sink.append(messages)
        return LLMResult(content=content)
    return responder


def _glob_call(cid: str = "c1") -> LLMResult:
    """一步工具调用。**检查点只在有工具调用的步上 tick**，所以"要留下检查点"
    的用例首回合必须走工具（纯文本回合一个检查点都不落）。"""
    return LLMResult(content=None, tool_calls=[
        ToolCall(id=cid, name="glob", arguments={"pattern": "*"})
    ])


# ---------- 多回合：state 是同一份 ----------

def test_second_turn_sees_the_first_turn(tmp_path, monkeypatch):
    """第二个回合的请求里必须能看到第一个回合的消息与工具结果。

    这是 REPL 存在的全部意义。做法是让第二回合的 responder 把收到的 messages
    抓下来断言 —— 只断言"第二回合跑起来了"是不够的：一个每回合都新建 state 的
    实现同样能跑起来，而且看起来完全正常。
    """
    _seed(tmp_path)
    seen: list = []

    llm = MockLLM.script(
        LLMResult(content=None, tool_calls=[
            ToolCall(id="c1", name="read", arguments={"path": "calc.py"})
        ]),
        LLMResult(content="第一回合完成"),
        _text_responder("第二回合完成", seen),
    )
    repl = _repl(tmp_path, monkeypatch, llm)
    _capture(monkeypatch)

    repl.run_turn("读一下 calc.py")
    repl.run_turn("再改一下")

    messages = seen[0]
    texts = [m.get("content") or "" for m in messages]
    assert "读一下 calc.py" in texts          # 第一回合的 user 消息还在
    assert "再改一下" in texts                # 第二回合的也在
    # 第一回合那次 read 的**输出**还在（不是只有"人说过什么"）
    tool_contents = [m.get("content") or "" for m in messages if m.get("role") == "tool"]
    assert any("return a - b" in c for c in tool_contents)


def test_two_turns_share_one_session(tmp_path, monkeypatch):
    """两个回合落在**同一个**会话里（REPL 不该每回合换会话）。"""
    _seed(tmp_path)
    llm = MockLLM.script(LLMResult(content="一"), LLMResult(content="二"))
    repl = _repl(tmp_path, monkeypatch, llm, checkpoint_every=1)
    _capture(monkeypatch)

    repl.run_turn("第一件事")
    repl.run_turn("第二件事")

    assert repl.state.session_id == repl.session.session_id
    assert repl.state.step == 2
    # 轨迹是**追加**的：两条 llm_call 都在同一个 JSONL 里
    lines = repl.session.trajectory_path.read_text(encoding="utf-8").splitlines()
    assert sum(1 for line in lines if '"llm_call"' in line) == 2


# ---------- 每轮一份步数预算 ----------

def test_max_steps_is_a_per_turn_budget(tmp_path, monkeypatch):
    """`max_steps` 是"这次运行最多走几步"，不是"这个会话累计几步"。

    **旧语义下这一条必红**：`state.step` 累计到 2 之后，第二回合的循环条件
    `state.step < max_steps` 一开始就是假 —— 一次模型调用都不发、又打印一遍
    「已达到最大步数」，而且那句错误信息指的方向（"请拆分子任务"）还是错的。
    """
    _seed(tmp_path)
    llm = MockLLM.script(
        _glob_call("c1"),
        _glob_call("c2"),
        LLMResult(content="第二回合真的跑起来了"),   # 预算重新给一份，这一步才会被调到
    )
    repl = _repl(tmp_path, monkeypatch, llm)
    repl.engine.max_steps = 2
    _capture(monkeypatch)

    first = repl.run_turn("第一件事")
    second = repl.run_turn("第二件事")

    assert first.terminated_reason == "max_steps"
    assert first.steps == 2
    assert second.terminated_reason == "completed"
    assert second.final_text == "第二回合真的跑起来了"
    # step 本身照样**累计**：检查点文件名、--fork --step K、轨迹字段都靠它单调递增
    assert repl.state.step == 3


def test_terminated_reason_is_reset_each_turn(tmp_path, monkeypatch):
    """上一回合的终止原因不能带到下一回合。

    不复位的话第二回合**全程**带着旧值，而中途落的检查点会把它写进 payload ——
    于是"从检查点读出来的终止原因"与"这个会话真的怎么结束的"是两件事
    （`app/ui_streamlit.py` 就在读它）。断言点放在**循环内部**：从外面看
    `result.terminated_reason` 是对的，看不出请求发出那一刻它是什么。
    """
    _seed(tmp_path)
    llm = MockLLM.script(
        _glob_call("c1"),
        _glob_call("c2"),
    )
    repl = _repl(tmp_path, monkeypatch, llm)
    repl.engine.max_steps = 2
    _capture(monkeypatch)

    repl.run_turn("第一件事")
    assert repl.state.terminated_reason == "max_steps"   # 上一回合留下的

    probe: list[str | None] = []

    def responder(messages, tools):  # noqa: ANN001 - MockLLM 回调签名
        probe.append(repl.state.terminated_reason)
        return LLMResult(content="干净地跑完了")

    llm.responses.append(responder)
    repl.run_turn("第二件事")

    assert probe == [None]                    # 第一条请求发出时它已经复位
    assert repl.state.terminated_reason == "completed"


# ---------- 权限的"本回合"真的是一回合 ----------

def _edit_call(cid: str) -> LLMResult:
    return LLMResult(content=None, tool_calls=[ToolCall(
        id=cid, name="edit",
        arguments={"path": "calc.py", "old_string": "return a - b",
                   "new_string": "return a + b"},
    )])


def test_allow_turn_does_not_leak_into_the_next_turn(tmp_path, monkeypatch):
    """确认框上写着「2) 本回合允许」，那就必须**只**在这个回合有效。

    常驻进程里 `_turn` 从不清空的话，它就成了"一直允许到进程退出" ——
    语义悄悄变成了菜单上没写的那个。这里走的是 `--review-edits` 的真路径
    （`_build_runtime(review_edits=True)`），所以顺带钉住了"REPL 与单发共用装配"。
    """
    target = _seed(tmp_path)
    asked: list[str] = []

    monkeypatch.setattr("app.cli._confirm_prompt",
                        lambda question: (asked.append(question), "allow_turn")[1])

    llm = MockLLM.script(
        _edit_call("c1"), LLMResult(content="改好了"),
        _edit_call("c2"), LLMResult(content="又改好了"),
    )
    repl = _repl(tmp_path, monkeypatch, llm, review_edits=True)
    _capture(monkeypatch)

    repl.run_turn("修一下")
    assert len(asked) == 1
    assert "return a + b" in target.read_text(encoding="utf-8")

    repl.run_turn("再修一次")
    # ★ 同一个引擎实例、同一个目标，第二回合必须**重新问**（旧代码下这里是 1）
    assert len(asked) == 2


def test_allow_always_still_survives_a_new_turn(tmp_path, monkeypatch):
    """`allow_always` 是常驻记忆，`new_turn` 不能把它一起清掉。

    这一条是上一条的对照：只清"本回合"那一份 ≠ 把记得久的也忘了 ——
    用户选「3) 一直允许」正是为了不用再被问。
    """
    _seed(tmp_path)
    asked: list[str] = []

    monkeypatch.setattr("app.cli._confirm_prompt",
                        lambda question: (asked.append(question), "allow_always")[1])

    llm = MockLLM.script(
        _edit_call("c1"), LLMResult(content="改好了"),
        _edit_call("c2"), LLMResult(content="又改好了"),
    )
    repl = _repl(tmp_path, monkeypatch, llm, review_edits=True)
    _capture(monkeypatch)

    repl.run_turn("修一下")
    repl.run_turn("再修一次")

    assert len(asked) == 1     # 第二回合同一个命令没再被问


# ---------- skill 发现只记一次 ----------

def _seed_skill(ws: Path) -> None:
    skill_dir = ws / ".codeagent" / "skills" / "release"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\ndescription: 发布三步走\n---\n\n正文\n", encoding="utf-8"
    )


def test_skill_discovery_is_recorded_once_across_turns(tmp_path, monkeypatch):
    """`record_discovery` 在 `_run_loop` 入口被调用，而 REPL 每回合都走一遍入口。

    旧守卫是"block 不在 system prompt 里就跳过"，可第二回合 block **还在** prompt 里
    （`state.system_prompt` 是会话建好时渲染的那份，不随回合变）→ 每回合往轨迹里
    再写一份。单发只有一轮，看不出来；多跑几轮，同一件事就堆成 N 份副本。
    """
    _seed(tmp_path)
    _seed_skill(tmp_path)
    llm = MockLLM.script(LLMResult(content="一"), LLMResult(content="二"))
    repl = _repl(tmp_path, monkeypatch, llm)
    _capture(monkeypatch)

    repl.run_turn("第一件事")
    repl.run_turn("第二件事")

    assert [e["type"] for e in repl.state.events].count("skill_discovery") == 1


# ---------- 中断之后还能接着用 ----------

def test_interrupted_turn_is_repaired_on_the_next_one(tmp_path, monkeypatch):
    """回合中途 Ctrl+C 会留下「assistant(tool_calls) + 缺 tool 结果」的形状。

    带着孤儿 id 请求下一轮，OpenAI 兼容端点直接 400 —— 而报错发生在**下一回合**，
    看起来和上次那一下 Ctrl+C 毫无关系。单发进程从没暴露过这件事，因为它靠检查点
    恢复，而检查点落在**完整的步边界**上。
    """
    _seed(tmp_path)
    llm = MockLLM.script(LLMResult(content="继续做完了"))
    repl = _repl(tmp_path, monkeypatch, llm)
    _capture(monkeypatch)

    # 手工造一个"被打断"的现场：两个 tool_call，只有一个结果
    repl.state.messages.append(assistant_tool_calls([
        ToolCall(id="c1", name="read", arguments={"path": "calc.py"}),
        ToolCall(id="c2", name="read", arguments={"path": "calc.py"}),
    ]))
    repl.state.messages.append(tool_result("c1", "第一条结果"))

    repl.run_turn("接着做")

    tool_ids = [m.get("tool_call_id") for m in repl.state.messages if m.get("role") == "tool"]
    assert "c2" in tool_ids                       # 缺的那条被补上了
    assert repl.state.terminated_reason == "completed"


def test_pairing_repair_is_not_silent(tmp_path, monkeypatch):
    """补了几条要记进轨迹 —— **不静默修**。事后看一份对话想不通模型为什么说
    "我再调用一次"，翻轨迹要能看到这一回合是缝过的。"""
    _seed(tmp_path)
    llm = MockLLM.script(LLMResult(content="好了"))
    repl = _repl(tmp_path, monkeypatch, llm)
    _capture(monkeypatch)
    repl.state.messages.append(assistant_tool_calls([
        ToolCall(id="c1", name="read", arguments={"path": "calc.py"}),
    ]))

    repl.run_turn("接着做")

    events = [e for e in repl.state.events if e["type"] == "pairing_repaired"]
    assert len(events) == 1
    assert events[0]["count"] == 1


def test_healthy_turn_records_no_pairing_event(tmp_path, monkeypatch):
    """配对完好时不该有这条事件（否则"缝过"这个信号就失去意义）。"""
    _seed(tmp_path)
    llm = MockLLM.script(LLMResult(content="一"))
    repl = _repl(tmp_path, monkeypatch, llm)
    _capture(monkeypatch)

    repl.run_turn("随便做点")

    assert not [e for e in repl.state.events if e["type"] == "pairing_repaired"]


# ---------- await_user：一行输入就是回答 ----------

def test_await_user_turn_prints_the_question_and_takes_the_next_line(tmp_path, monkeypatch):
    """模型提问 → 本轮结束 → 打印问题 → 下一行输入就是回答（**零特殊处理**）。

    这正是 M8「把打断建模成数据标志而不是阻塞控制流」的回报：`await_user` 只是
    一个终止原因，REPL 的循环天然接得住。
    """
    _seed(tmp_path)
    seen: list = []

    llm = MockLLM.script(
        LLMResult(content=None, tool_calls=[ToolCall(
            id="c1", name="ask_user",
            arguments={"question": "要保留旧接口吗？", "options": ["保留", "删掉"]},
        )]),
        _text_responder("照你说的做了", seen),
    )
    repl = _repl(tmp_path, monkeypatch, llm)
    lines = _capture(monkeypatch)

    first = repl.run_turn("重构一下")
    text = "\n".join(lines)
    assert first.terminated_reason == "await_user"
    assert "需要你补充信息" in text
    assert "要保留旧接口吗？" in text
    assert "结论" not in text          # 提问不是结论

    second = repl.run_turn("保留")
    assert second.final_text == "照你说的做了"
    # 人的回答作为一条 user 消息进了会话，模型看得到
    assert any(m.get("content") == "保留" for m in seen[0])


# ---------- 斜杠命令 ----------

def test_unknown_slash_command_never_reaches_the_model(tmp_path, monkeypatch):
    """★ 硬不变量：打错一个字母的代价不该是一次真实的模型调用。

    MockLLM 不设任何响应 —— 一旦有东西被发给模型就 `RuntimeError`。
    所以这条测试是"零调用"的**可证伪**写法，而不是"输出里没有报错"。
    """
    llm = MockLLM.script()          # 耗尽即 RuntimeError
    repl = _repl(tmp_path, monkeypatch, llm)
    lines = _capture(monkeypatch)

    repl._dispatch("/hlep")          # 手滑

    text = "\n".join(lines)
    assert "未知命令" in text
    assert "/help" in text              # 要指向正确的那个
    assert repl.state.step == 0


def test_help_lists_exactly_the_commands_that_exist():
    """命令表与帮助是**同一份数据**生成的 —— 不这样迟早出现"帮助里有、实际没有"。"""
    text = help_text()
    for name in ("/help", "/exit", "/new", "/resume", "/fork", "/plan",
                 "/sessions", "/rename", "/clear-taint", "/goal", "/rewind"):
        assert name in text
    # 计数与 `_COMMANDS` 对齐：新加命令时这条会红，逼你回来看一眼帮助正文
    # （以及"这个命令真的有人能用上吗"）。
    assert len(_COMMANDS) == 11
    assert len([ln for ln in text.splitlines() if ln.strip().startswith("/")]) == 11


def test_command_names_are_case_insensitive(tmp_path, monkeypatch):
    llm = MockLLM.script()
    repl = _repl(tmp_path, monkeypatch, llm)
    lines = _capture(monkeypatch)

    repl._dispatch("/HELP")

    assert "斜杠命令" in "\n".join(lines)


def test_new_switches_to_a_fresh_session(tmp_path, monkeypatch):
    llm = MockLLM.script(LLMResult(content="一"))
    repl = _repl(tmp_path, monkeypatch, llm)
    _capture(monkeypatch)
    repl.run_turn("做点事")
    old = repl.session.session_id

    repl._dispatch("/new")

    assert repl.session.session_id != old
    assert repl.state.step == 0
    assert repl.engine.session.session_id == repl.session.session_id   # 引擎一起换了
    assert repl.state.messages[0]["role"] == "system"


def test_resume_switches_to_an_existing_session_by_name(tmp_path, monkeypatch):
    llm = MockLLM.script(_glob_call("c1"), LLMResult(content="一"))
    repl = _repl(tmp_path, monkeypatch, llm, checkpoint_every=1)
    _capture(monkeypatch)
    repl.run_turn("做点事")
    sid = repl.session.session_id
    repl._dispatch("/rename 基线方案")

    repl._dispatch("/new")
    assert repl.session.session_id != sid

    repl._dispatch("/resume 基线方案")

    assert repl.session.session_id == sid
    assert repl.state.step == 1                 # 恢复到最新检查点（工具调用那一步）
    assert repl.engine.session.session_id == sid


def test_failed_resume_keeps_the_current_session(tmp_path, monkeypatch):
    """★ 硬不变量：切换失败**不能半切换**。

    「session 换了、engine/state 没换」是一个没有任何报错的错配：轨迹写进 A、
    你在看 B。所以断言不只是"报错了"，还有"当前会话仍然完全能用"。
    """
    llm = MockLLM.script(LLMResult(content="一"), LLMResult(content="二"))
    repl = _repl(tmp_path, monkeypatch, llm)
    lines = _capture(monkeypatch)
    repl.run_turn("做点事")
    sid, engine = repl.session.session_id, repl.engine

    repl._dispatch("/resume 根本没有这个会话")

    assert "找不到会话" in "\n".join(lines)
    assert repl.session.session_id == sid      # 会话没换
    assert repl.engine is engine               # 引擎也没换
    repl.run_turn("我还能继续干活")             # 而且真的还能跑
    assert repl.state.step == 2


def test_failed_fork_keeps_the_current_session(tmp_path, monkeypatch):
    """另一个失败口：步数上没有检查点（`_load_payload` 抛 FileNotFoundError）。

    `_do_fork` 在失败时 `raise typer.Exit(1)` —— 在 REPL 里这条退出只该作用于
    **这条命令**，不能把你连人带会话一起踢出去。
    """
    llm = MockLLM.script(_glob_call("c1"), LLMResult(content="一"), _glob_call("c2"),
                         LLMResult(content="二"))
    repl = _repl(tmp_path, monkeypatch, llm, checkpoint_every=1)
    lines = _capture(monkeypatch)
    repl.run_turn("做点事")
    sid, engine = repl.session.session_id, repl.engine

    repl._dispatch("/fork 7")          # 只有 step 1 的检查点

    assert "分叉失败" in "\n".join(lines)
    assert repl.session.session_id == sid
    assert repl.engine is engine
    repl.run_turn("继续")               # REPL 还活着
    assert repl.state.step == 4


def test_fork_switches_to_the_new_branch(tmp_path, monkeypatch):
    """`/fork` 复用 `_do_fork`（单发那条路的同一份实现），切过去之后能接着跑。"""
    llm = MockLLM.script(
        _glob_call("c1"), LLMResult(content="一"),
        LLMResult(content="分叉之后"),
    )
    repl = _repl(tmp_path, monkeypatch, llm, checkpoint_every=1)
    _capture(monkeypatch)
    repl.run_turn("做点事")
    source = repl.session.session_id

    repl._dispatch("/fork 1")

    assert repl.session.session_id != source
    assert repl.state.step == 1                 # 回到第 1 步
    assert repl.engine.session.session_id == repl.session.session_id
    repl.run_turn("在新分支上接着做")
    assert repl.state.terminated_reason == "completed"


def test_fork_with_a_non_numeric_step_does_not_kill_the_repl(tmp_path, monkeypatch):
    """命令级失败只作用于那条命令 —— 打错一个步数不该把你踢出会话。"""
    llm = MockLLM.script(LLMResult(content="一"), LLMResult(content="二"))
    repl = _repl(tmp_path, monkeypatch, llm, checkpoint_every=1)
    lines = _capture(monkeypatch)
    repl.run_turn("做点事")
    sid = repl.session.session_id

    repl._dispatch("/fork 三步")

    assert "整数" in "\n".join(lines)
    assert repl.session.session_id == sid
    repl.run_turn("继续")                       # REPL 还活着


def test_plan_command_prints_the_checklist(tmp_path, monkeypatch):
    llm = MockLLM.script(LLMResult(content="一"))
    repl = _repl(tmp_path, monkeypatch, llm)
    lines = _capture(monkeypatch)
    repl.state.plan = [{"text": "读代码", "status": "done"},
                       {"text": "写测试", "status": "pending"}]

    repl._dispatch("/plan")

    text = "\n".join(lines)
    assert "读代码" in text and "写测试" in text


def test_plan_command_without_a_plan_says_so(tmp_path, monkeypatch):
    llm = MockLLM.script(LLMResult(content="一"))
    repl = _repl(tmp_path, monkeypatch, llm)
    lines = _capture(monkeypatch)

    repl._dispatch("/plan")

    # 不能复用 render_plan 的"已清空"文案：那是 update_plan 清空动作的说法，
    # 而这里只是"这个会话没排过计划"
    assert "没有计划清单" in "\n".join(lines)


def test_sessions_command_lists_what_is_there(tmp_path, monkeypatch):
    llm = MockLLM.script(LLMResult(content="一"))
    repl = _repl(tmp_path, monkeypatch, llm)
    lines = _capture(monkeypatch)
    repl.run_turn("做点事")

    repl._dispatch("/sessions")

    assert repl.session.session_id in "\n".join(lines)


def test_rename_command_rejects_a_blank_name(tmp_path, monkeypatch):
    llm = MockLLM.script(LLMResult(content="一"))
    repl = _repl(tmp_path, monkeypatch, llm)
    lines = _capture(monkeypatch)

    repl._dispatch("/rename")

    assert "用法" in "\n".join(lines)


def test_clear_taint_is_a_human_action(tmp_path, monkeypatch):
    """复位污染标记必须留下轨迹事件 —— 否则下次从这个检查点恢复时，
    旧事件会把标记重新抬回去（"我明明清过了"）。"""
    llm = MockLLM.script(LLMResult(content="一"))
    repl = _repl(tmp_path, monkeypatch, llm)
    lines = _capture(monkeypatch)
    repl.state.raise_taint(TAINT_HIGH)

    repl._dispatch("/clear-taint")

    assert repl.state.taint == "none"
    assert any(e["type"] == "taint_cleared" for e in repl.state.events)
    assert "复位" in "\n".join(lines)

    # 再清一次：要如实说"本来就是 none"，不能默默返回（人需要知道自己的动作没生效）
    lines.clear()
    repl._dispatch("/clear-taint")
    assert "本来就是 none" in "\n".join(lines)


# ---------- 每回合的增量报告 ----------

def test_report_shows_this_turns_usage_not_the_session_total(tmp_path, monkeypatch):
    """`RunResult.usage` 是**会话累计**的，而 REPL 要报的是本回合增量。

    顺带钉住一个陷阱：`Usage` 是可变 dataclass、loop 里是**原地** `+=`，
    所以"回合前的快照"必须复制一份 —— 直接引用的话相减恒为 0，而且不报错，
    表现只是"每回合都显示没花钱"。
    """
    _seed(tmp_path)
    first = Usage(prompt_tokens=100, completion_tokens=10,
                  prompt_cache_hit_tokens=80, prompt_cache_miss_tokens=20)
    second = Usage(prompt_tokens=300, completion_tokens=30,
                   prompt_cache_hit_tokens=200, prompt_cache_miss_tokens=100)
    llm = MockLLM.script(
        LLMResult(content="一", usage=first),
        LLMResult(content="二", usage=second),
    )
    repl = _repl(tmp_path, monkeypatch, llm)
    lines = _capture(monkeypatch)

    repl.run_turn("第一件事")
    assert "token 110（prompt 100 + completion 10）" in "\n".join(lines)

    lines.clear()
    repl.run_turn("第二件事")
    report = "\n".join(lines)
    assert "token 330（prompt 300 + completion 30）" in report   # 不是 440
    assert "step 1→2" in report


# ---------- 退出 ----------

def test_exit_command_prints_an_executable_resume_hint(tmp_path, monkeypatch):
    """退出时给的那条命令**必须真能跑**（M7 教训：走不通的指引比没有更糟）。"""
    llm = MockLLM.script(LLMResult(content="一"))
    repl = _repl(tmp_path, monkeypatch, llm)
    lines = _capture(monkeypatch)
    repl.run_turn("做点事")

    repl._dispatch("/exit")

    text = "\n".join(lines)
    assert repl._done is True
    assert f"python -m app.cli --repl --resume --session-id {repl.session.session_id}" in text


# ---------- 走真 CLI：--repl + stdin ----------

def _invoke_repl(tmp_path, monkeypatch, llm, stdin: str, *,
                 task: str | None = None, extra: tuple[str, ...] = ()):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setattr("app.cli._build_llm", lambda mock: llm)
    monkeypatch.setattr(
        "app.cli.discover_skills",
        lambda ws: discover_skills(ws, home=tmp_path / "home"),
    )
    args = ([task] if task else []) + ["--repl", "--mock", *extra]
    return runner.invoke(app, args, input=stdin)


def test_repl_flag_runs_two_turns_from_stdin(tmp_path, monkeypatch):
    """`--repl` 端到端：两行输入 = 两个回合，EOF 退出。

    输入里**夹了一个空行**：空行不是任务（否则模型会收到一条空消息，而 MockLLM
    的脚本会被它提前吃掉一个 —— 断言就会红）。

    这是 `tests/test_cli.py` 的第一条 stdin 测试 —— 在此之前 CLI 的交互读法
    （`--review-edits` 的确认框）从来没有被输入端到端地测过。
    """
    _seed(tmp_path)
    seen: list = []

    llm = MockLLM.script(
        LLMResult(content=None, tool_calls=[
            ToolCall(id="c1", name="read", arguments={"path": "calc.py"})
        ]),
        LLMResult(content="第一回合完成"),
        _text_responder("第二回合完成", seen),
    )
    result = _invoke_repl(tmp_path, monkeypatch, llm, "读一下 calc.py\n\n再改一下\n")

    assert result.exit_code == 0, result.output
    assert "第一回合完成" in result.output
    assert "第二回合完成" in result.output
    assert "常驻交互模式" in result.output
    # ★ 空行的两种处理方式（跳过 / 当成一个回合）在"输出里有没有那句话"上
    # 看不出区别 —— 都会被这条命令的其余输出盖过去。所以要直接钉**两个回合**：
    # 有 3 个回合的话，脚本响应会被空行提前吃掉、最后一条真实输入报 error。
    assert "[error]" not in result.output
    users = [m.get("content") for m in seen[0] if m.get("role") == "user"]
    assert users == ["读一下 calc.py", "再改一下"]      # 空行不产生任何消息
    # 第二回合的请求里带着第一回合的读结果（多回合上下文真的接上了）
    assert any("return a - b" in (m.get("content") or "") for m in seen[0])
    # 退出时给的是**可执行**的续跑命令
    assert "python -m app.cli --repl --resume --session-id" in result.output


def test_repl_flag_with_a_task_runs_it_as_the_first_turn(tmp_path, monkeypatch):
    """带任务进来 = 第一回合；`/exit` 结束（不靠 EOF）。"""
    _seed(tmp_path)
    llm = MockLLM.script(LLMResult(content="任务做完了"))
    result = _invoke_repl(tmp_path, monkeypatch, llm, "/exit\n", task="读一下 README")

    assert result.exit_code == 0, result.output
    assert "任务做完了" in result.output
    assert "退出（exit）" in result.output


def test_repl_flag_shows_session_and_step_in_the_prompt(tmp_path, monkeypatch):
    _seed(tmp_path)
    llm = MockLLM.script(LLMResult(content="一"))
    result = _invoke_repl(tmp_path, monkeypatch, llm, "做点事\n/exit\n")

    assert result.exit_code == 0, result.output
    assert "codeagent [" in result.output
    assert "step 1" in result.output


def test_repl_without_a_task_does_not_demand_one(tmp_path, monkeypatch):
    """`--repl` 不带任务**不该**报"请提供任务描述" —— 它就是要进提示符。"""
    llm = MockLLM.script()
    result = _invoke_repl(tmp_path, monkeypatch, llm, "/exit\n")

    assert result.exit_code == 0, result.output
    assert "请提供任务描述" not in result.output


def test_repl_unknown_command_costs_no_model_call_end_to_end(tmp_path, monkeypatch):
    llm = MockLLM.script()      # 一旦被调用就 RuntimeError
    result = _invoke_repl(tmp_path, monkeypatch, llm, "/hlep\n/exit\n")

    assert result.exit_code == 0, result.output
    assert "未知命令" in result.output


def test_repl_line_starting_with_space_is_still_a_task(tmp_path, monkeypatch):
    """行首加一个空格 = 把它当任务发出去。

    判据是**原始行**的首字符而不是 `strip()` 之后的：`/etc/hosts` 这种以斜杠
    开头的**路径**靠这条规则才能被正常送出去（否则它会变成一条"未知命令"，
    而人只会看到自己的任务被拒绝，完全不知道为什么）。
    """
    _seed(tmp_path)
    seen: list = []
    llm = MockLLM.script(_text_responder("看了", seen))
    result = _invoke_repl(tmp_path, monkeypatch, llm, " /etc/hosts 有问题\n/exit\n")

    assert result.exit_code == 0, result.output
    assert "未知命令" not in result.output
    assert seen and any("etc/hosts" in (m.get("content") or "") for m in seen[0])


def test_repl_resume_switches_into_an_existing_session(tmp_path, monkeypatch):
    """`--repl --resume` 拿那个会话当起始会话（会话 id 与步数都要对）。"""
    _seed(tmp_path)
    seeded = _invoke_repl(
        tmp_path, monkeypatch, MockLLM.script(LLMResult(content="种一个会话")),
        "/exit\n", task="先做点事", extra=("--checkpoint-every", "1"),
    )
    assert seeded.exit_code == 0, seeded.output

    from agent.session import latest_session
    sid = latest_session(tmp_path)
    assert sid

    llm = MockLLM.script(LLMResult(content="接着做"))
    result = _invoke_repl(
        tmp_path, monkeypatch, llm, "接着做\n/exit\n",
        extra=("--resume", "--session-id", sid),
    )
    assert result.exit_code == 0, result.output
    assert "恢复会话" in result.output
    assert sid in result.output
    assert "step 2" in result.output      # 提示符上接着上一回合的步数


# ---------- M9-8：/rewind（工作区文件回滚） ----------

def _seed_writes(tmp_path, monkeypatch):
    """真跑一个回合让 agent 写文件 —— 于是这个会话有工作区快照。

    不能用 `_glob_call`（只读工具）：只读工具**一个快照都不落**，而 `/rewind`
    正是要有快照才能测的东西。返回那个会话的 id。
    """
    (tmp_path / "a.txt").write_text("v0\n", encoding="utf-8")
    llm = MockLLM.script(
        LLMResult(content=None, tool_calls=[
            ToolCall(id="w1", name="write",
                     arguments={"path": "a.txt", "content": "v1\n"})
        ]),
        LLMResult(content="改完了"),
    )
    result = _invoke_repl(
        tmp_path, monkeypatch, llm, "/exit\n",
        task="把 a.txt 改成 v1", extra=("--checkpoint-every", "1"),
    )
    assert result.exit_code == 0, result.output
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "v1\n"
    from agent.session import latest_session
    return latest_session(tmp_path)


def _repl_in(tmp_path, monkeypatch, sid, llm) -> Repl:
    """开一个 REPL 并**切进 `sid` 那个会话**。

    `_repl()` 起的是**全新**会话（没有快照），拿它测 `/rewind` 只会得到"这一步
    没有快照"——那是测试的错，不是被测代码的错。
    """
    runtime = _runtime(tmp_path, monkeypatch, llm)
    repl = Repl(runtime, checkpoint_every=1)
    repl.start(start_session_id=sid, resume=True)
    return repl


def test_rewind_command_defaults_to_a_preview(tmp_path, monkeypatch):
    """★ `/rewind` 默认**只预览**：文件一个字节都不能变。

    与 CLI `--rewind` 同一条决议（决议 2）。REPL 这一侧更要紧 —— 人是随手敲的
    一条斜杠命令，没有 shell 的历史记录或者光标停留让人再想一遍。
    """
    sid = _seed_writes(tmp_path, monkeypatch)
    lines = _capture(monkeypatch)
    # 确认框被调用的**那一刻**，盘上必须还是 v1 —— 预览真就只是预览。
    # 只断言"最后文件是 v1"是不够的：一个先 restore、发现没确认再 restore 回去
    # 的实现同样能通过，而它在中间那段时间已经把用户的文件覆盖掉了。
    seen: dict = {}

    def spy(_q):
        seen["content"] = (tmp_path / "a.txt").read_text(encoding="utf-8")
        return "n"

    monkeypatch.setattr("builtins.input", spy)
    repl = _repl_in(tmp_path, monkeypatch, sid, MockLLM.script(LLMResult(content="没别的")))
    repl._dispatch("/rewind 0")

    text = "\n".join(lines)
    assert "回滚预览" in text, lines
    assert "a.txt" in text
    assert seen["content"] == "v1\n", "确认框弹出时文件就已经被改了 —— 预览不是预览"
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "v1\n"


def test_rewind_command_asks_and_restores_on_yes(tmp_path, monkeypatch):
    """确认了才动盘。确认框**必须**是 REPL 自己那一份（见 `_confirm_rewind`）。"""
    sid = _seed_writes(tmp_path, monkeypatch)
    lines = _capture(monkeypatch)
    monkeypatch.setattr("builtins.input", lambda *a: "y")
    repl = _repl_in(tmp_path, monkeypatch, sid, MockLLM.script(LLMResult(content="没别的")))
    repl._dispatch("/rewind 0")

    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "v0\n"
    assert "已回滚" in "\n".join(lines)


def test_rewind_command_declining_leaves_the_files_alone(tmp_path, monkeypatch):
    """★ 回滚是全项目唯一一个会覆盖/删除用户文件的动作 —— 默认那一侧必须是"不动"。

    （`input` 返回空串 = 直接回车 = 用户不假思索按下的那一下。）
    """
    sid = _seed_writes(tmp_path, monkeypatch)
    lines = _capture(monkeypatch)
    monkeypatch.setattr("builtins.input", lambda *a: "")
    repl = _repl_in(tmp_path, monkeypatch, sid, MockLLM.script(LLMResult(content="没别的")))
    repl._dispatch("/rewind 0")

    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "v1\n"
    assert "已取消" in "\n".join(lines)


def test_rewind_command_tells_the_live_conversation(tmp_path, monkeypatch):
    """★ 回滚之后，**当前这段对话**里必须多出一条通知。

    `_reinject_rewind` 走 meta，那条路只在 `--resume` 时生效；REPL 里人是**在一段
    活着的对话中途**回滚的，模型下一秒就要开口，它手上那份"我刚改过 a.txt"的
    记忆已经作废了。少了这条消息，模型会照着不存在的文件往下推理。
    """
    sid = _seed_writes(tmp_path, monkeypatch)
    _capture(monkeypatch)
    monkeypatch.setattr("builtins.input", lambda *a: "y")
    repl = _repl_in(tmp_path, monkeypatch, sid, MockLLM.script(LLMResult(content="没别的")))
    before = len(repl.state.messages)
    repl._dispatch("/rewind 0")

    assert len(repl.state.messages) == before + 1, "回滚通知没进当前会话的消息"
    notice = repl.state.messages[-1]
    assert notice["role"] == "user", "运行时通知不能冒充 assistant 的自述"
    assert "回滚" in notice["content"]


def test_rewind_rejects_a_non_numeric_step(tmp_path, monkeypatch):
    """`/rewind 零` 不能当成 0 —— 0 是有真实含义的坐标（"撤销我们做过的一切"）。

    所以判据不能只看"文件没变"：把打错的字当成 0 之后，**预览会照打**（列出
    "将撤销所有改动"），而人看到预览后的下一步就是按 y。所以打错字必须**连预览
    都没有**。
    """
    sid = _seed_writes(tmp_path, monkeypatch)
    lines = _capture(monkeypatch)
    repl = _repl_in(tmp_path, monkeypatch, sid, MockLLM.script(LLMResult(content="没别的")))
    repl._dispatch("/rewind 零")

    assert "步数要是个整数" in "\n".join(lines)
    assert "回滚预览" not in "\n".join(lines), "打错字却打出了第 0 步的预览"
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "v1\n"


def test_rewind_command_is_in_the_command_table():
    """`/rewind` 得真的在表里 —— 不在的话它会被当成一句话发给模型。"""
    assert "/rewind" in _COMMANDS
    assert "/rewind" in help_text()
