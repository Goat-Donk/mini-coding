"""update_plan tests：agent/tools/plan.py + 落盘跨回合 + `--plan`。

**每组测试都对着机制存在的理由**（而不是对着实现）：
- 全量覆盖：清单每次整体提交，状态只有一个写入者
- 参数不合法要**回喂具体差在哪**：模型能自己改对，而不是原样重试
- 落盘跨回合：中途被打断 / 换会话续跑时计划不丢 —— 这是这个工具存在的理由
- **不把计划快照注入每轮消息**：那是参考实现的做法，在我们的 cache-aware 布局下
  每注入一次就破坏一段前缀缓存
- 接不上会话状态时**要响**：静默降级会让"计划丢了"看起来像"一切正常"
"""
from __future__ import annotations

from pathlib import Path

from agent.llm import LLMResult, MockLLM, ToolCall
from agent.loop import QueryEngine
from agent.session import Session, load_state
from agent.state import AgentState
from agent.tools.base import ToolContext, ToolRegistry
from agent.tools.plan import PLAN_STATUSES, UpdatePlanTool, render_plan


def ctx(tmp_path: Path, state: object | None = None) -> ToolContext:
    return ToolContext(workspace_root=tmp_path, state=state)


def make_state() -> AgentState:
    return AgentState(session_id="t", task="任务", system_prompt="sys")


def run_plan(arguments: dict, *, state: object | None = None, tmp_path: Path | None = None):
    root = tmp_path or Path(".")
    return UpdatePlanTool().run(arguments, ctx(root, state if state is not None else make_state()))


# ---------- 全量覆盖契约 ----------

def test_full_list_lands_in_state(tmp_path: Path):
    """每次传完整清单 → state.plan 就是这一份（覆盖，不是追加）。"""
    state = make_state()
    first = run_plan(
        {"items": ["读 README", "改 a.txt"], "statuses": ["done", "in_progress"]},
        state=state, tmp_path=tmp_path,
    )
    assert first.success
    assert state.plan == [
        {"text": "读 README", "status": "done"},
        {"text": "改 a.txt", "status": "in_progress"},
    ]

    second = run_plan(
        {"items": ["读 README", "改 a.txt", "跑测试"], "statuses": ["done", "done", "pending"]},
        state=state, tmp_path=tmp_path,
    )
    assert second.success
    assert [item["text"] for item in state.plan] == ["读 README", "改 a.txt", "跑测试"]
    assert [item["status"] for item in state.plan] == ["done", "done", "pending"]
    assert "2/3 完成" in second.output


def test_render_marks_every_item(tmp_path: Path):
    """渲染里每条都带状态名（而不是只给个符号让读者猜图例）。"""
    text = render_plan([
        {"text": "a", "status": "done"},
        {"text": "b", "status": "in_progress"},
        {"text": "c", "status": "pending"},
    ])
    for status in PLAN_STATUSES:
        assert f"[{status}]" in text
    assert "1/3 完成" in text


def test_empty_list_clears_the_plan(tmp_path: Path):
    """空清单 = 清空，且是**合法**操作（不是错误）。

    清空是模型放弃原计划时的正常动作；把它判成失败会让模型只能靠"编一条假的
    done"来表达"我不做这个了"。
    """
    state = make_state()
    run_plan({"items": ["a"], "statuses": ["pending"]}, state=state, tmp_path=tmp_path)
    result = run_plan({"items": [], "statuses": []}, state=state, tmp_path=tmp_path)
    assert result.success
    assert state.plan == []
    assert "已清空" in result.output


# ---------- 参数校验：失败要能自修复 ----------

def test_length_mismatch_fails_with_counts(tmp_path: Path):
    """长度不一致 → fail，且**说出各处几条**（模型据此就能改对）。"""
    state = make_state()
    result = run_plan(
        {"items": ["a", "b", "c"], "statuses": ["done"]}, state=state, tmp_path=tmp_path
    )
    assert not result.success
    assert "3" in result.output and "1" in result.output
    assert state.plan == []  # 非法输入不留下半份状态


def test_illegal_status_fails_and_lists_legal_ones(tmp_path: Path):
    state = make_state()
    result = run_plan(
        {"items": ["a"], "statuses": ["finished"]}, state=state, tmp_path=tmp_path
    )
    assert not result.success
    assert "finished" in result.output
    for status in PLAN_STATUSES:
        assert status in result.output  # 回喂合法取值，别让模型瞎猜
    assert state.plan == []


def test_blank_item_text_fails(tmp_path: Path):
    result = run_plan({"items": ["a", "   "], "statuses": ["done", "pending"]}, tmp_path=tmp_path)
    assert not result.success
    assert "不能为空" in result.output


def test_unwired_state_fails_loudly(tmp_path: Path):
    """接不上会话状态 → **报错**，不静默降级。

    静默返回一份渲染好的清单会让它看起来一切正常（模型照样往下做），但计划其实
    落不了盘 —— 而"跨回合不丢"正是这个工具存在的唯一理由。
    这是接线缺口（本项目最高发的一类缺陷），所以必须响。
    """
    result = UpdatePlanTool().run(
        {"items": ["a"], "statuses": ["pending"]}, ToolContext(workspace_root=tmp_path)
    )
    assert not result.success
    assert "未接上会话状态" in result.output


# ---------- 接线与约束 ----------

def test_plan_tool_is_in_default_registry(tmp_path: Path):
    """进 `default()`：只依赖 `ctx.state`、零副作用、不需要外部配合。

    副作用要认下来：`eval/runner.py` 用的就是 `default()`，所以评测里的 agent
    也拿到了它。那是能力不是负担（对比 `ask_user`：那个需要"有个人在那儿"，
    所以**不能**进 default）。
    """
    registry = ToolRegistry.default(tmp_path)
    assert "update_plan" in registry.names()
    assert "ask_user" not in registry.names()


def test_plan_tool_is_not_read_only():
    """写 `state.plan` → 不能进并发批（只读标记的含义是"可以并发跑"）。"""
    assert UpdatePlanTool.is_read_only() is False


def test_plan_schema_is_flat():
    """两个平行数组 → 内联 array，**没有 $defs**。

    项目硬约束：`base.py` 把 `$defs` pop 掉，嵌套 pydantic 模型会被**静默**
    削成坏 schema（模型收到的参数说明是错的，却不报错）。
    """
    schema = UpdatePlanTool().schema()["function"]["parameters"]
    assert "$defs" not in schema
    assert schema["properties"]["items"]["type"] == "array"
    assert schema["properties"]["items"]["items"] == {"type": "string"}
    assert set(schema["properties"]) == {"items", "statuses"}


def test_context_state_is_filled_by_the_real_loop(tmp_path: Path):
    """真跑一次 `QueryEngine.run`，`ctx.state` 必须非 None。

    这条钉的是"声明了但没有任何入口填"这类缺陷 —— `settings`/`emitter` 都犯过，
    只是没人发现（没有任何工具读它们）。`state` 一填上就有真读者（update_plan），
    所以这里必须锁住：漏填的表现是"计划工具永远失败"，而原因看起来在工具里。
    """
    seen: dict = {}
    real_execute = UpdatePlanTool.execute

    def spy(self, args, ctx):  # noqa: ANN001 - 与被替身的方法同签名
        seen["state"] = ctx.state
        seen["emitter"] = ctx.emitter
        return real_execute(self, args, ctx)

    engine = QueryEngine(
        MockLLM.script(
            LLMResult(content=None, tool_calls=[
                ToolCall(id="c1", name="update_plan",
                         arguments={"items": ["a"], "statuses": ["pending"]}),
            ]),
            LLMResult(content="做完了"),
        ),
        ToolRegistry.default(tmp_path),
        workspace_root=tmp_path,
    )
    original = UpdatePlanTool.execute
    UpdatePlanTool.execute = spy
    try:
        result = engine.run("随便一个任务")
    finally:
        UpdatePlanTool.execute = original

    assert result.terminated_reason == "completed"
    assert isinstance(seen["state"], AgentState)
    assert seen["emitter"] is None  # 没接 session 就没有 emitter（不是漏填）


# ---------- 落盘跨回合 ----------

def test_plan_survives_the_checkpoint(tmp_path: Path):
    """检查点往返：`state.plan` 原样回来（对齐 dump/load 的全字段约定）。"""
    state = make_state()
    run_plan({"items": ["a", "b"], "statuses": ["done", "pending"]}, state=state, tmp_path=tmp_path)

    session = Session(tmp_path, "s-plan")
    session._write(state)
    payload = session._load_payload(None)
    restored = load_state(payload, "s-plan")

    assert restored.plan == state.plan


def _cli(workspace: Path, monkeypatch, llm) -> object:
    """在指定工作区跑一次 CLI（--mock），并把 LLM 换成脚本化的替身。

    与 test_cli.py 的 `_invoke` 同一个套路。**必须换掉 `_build_llm`**：不换的话
    `--mock` 用的是内置演示脚本（glob → 结论），根本不会调 update_plan ——
    测试会"通过"在一个从没排过计划的会话上。
    """
    from typer.testing import CliRunner

    from app.cli import app

    monkeypatch.setenv("WORKSPACE_ROOT", str(workspace))
    monkeypatch.setattr("app.cli._build_llm", lambda mock: llm)
    return CliRunner().invoke(app, ["开始干活", "--mock", "--checkpoint-every", "1"])


PLAN_STEP = LLMResult(content=None, tool_calls=[
    ToolCall(id="c1", name="update_plan",
             arguments={"items": ["读 README", "改 a.txt"], "statuses": ["done", "pending"]}),
])


def test_plan_is_restored_and_injected_once_on_resume(tmp_path: Path, monkeypatch):
    """`--resume` 之后：计划还在，且**被注入一次**（它可能已被 compact 裁掉）。

    注入用的是 user 角色并写明来源 —— 不说明就当成"用户说的话"塞进去，
    模型会以为那是人给的指令。
    """
    workspace = tmp_path / "ws"
    workspace.mkdir()
    first = _cli(workspace, monkeypatch, MockLLM.script(PLAN_STEP, LLMResult(content="第一步做完了")))
    assert first.exit_code == 0, first.output

    seen: list[str] = []

    def responder(messages, tools):  # noqa: ANN001 - MockLLM 回调签名
        seen.extend(
            m["content"] for m in messages
            if m.get("role") == "user" and isinstance(m.get("content"), str)
        )
        return LLMResult(content="继续做完了")

    from typer.testing import CliRunner

    from app.cli import app

    monkeypatch.setattr("app.cli._build_llm", lambda mock: MockLLM.script(responder))
    second = CliRunner().invoke(app, ["--resume", "--mock"])

    assert second.exit_code == 0, second.output
    assert "计划恢复: 2 条" in second.output
    assert any("改 a.txt" in text and "会话恢复" in text for text in seen), seen


def test_plan_flag_prints_without_touching_the_llm(tmp_path: Path, monkeypatch):
    """`--plan` 不需要 API key、不建会话、不跑任务 —— 计划只是检查点里的一个字段。

    这里把 `_build_llm` 换成"一被调用就炸"，用来钉住它确实没被碰到：不然
    "没 key 也能看计划"这条会在某次重构里悄悄失效（表现是要求先配 key，
    而那正是这条路径存在的意义 —— 复盘一个跑过的会话）。
    """
    workspace = tmp_path / "ws"
    workspace.mkdir()
    assert _cli(workspace, monkeypatch, MockLLM.script(PLAN_STEP, LLMResult(content="跑完了"))).exit_code == 0

    from typer.testing import CliRunner

    from app.cli import app

    def boom(mock):  # noqa: ANN001
        raise AssertionError("--plan 不该构造 LLM")

    monkeypatch.setattr("app.cli._build_llm", boom)
    result = CliRunner().invoke(app, ["--plan"])

    assert result.exit_code == 0, result.output
    assert "读 README" in result.output
    assert "[done]" in result.output and "[pending]" in result.output


def test_plan_flag_on_a_session_without_a_plan(tmp_path: Path, monkeypatch):
    """会话存在、检查点也在，只是没排过计划 → 说清楚"没有"。

    不要复用 update_plan 的"已清空"文案：那是"清空"这个动作的说法，会让人
    以为曾经有过一份。

    这里必须**真的调一次工具**（glob）：检查点是在工具执行之后落的
    （`loop.py` 的 `session.checkpoint(state)`），一次不调工具就直接给结论的
    运行**不会留下任何检查点** —— 那样测到的是"没有会话"而不是"没有计划"。
    """
    workspace = tmp_path / "ws"
    workspace.mkdir()
    no_plan = MockLLM.script(
        LLMResult(content=None, tool_calls=[ToolCall(id="c1", name="glob", arguments={"pattern": "*"})]),
        LLMResult(content="直接做完了"),
    )
    assert _cli(workspace, monkeypatch, no_plan).exit_code == 0

    from typer.testing import CliRunner

    from app.cli import app

    monkeypatch.setattr("app.cli._build_llm", lambda mock: MockLLM.text("x"))
    result = CliRunner().invoke(app, ["--plan"])

    assert result.exit_code == 0, result.output
    assert "没有计划清单" in result.output
    assert "已清空" not in result.output


def test_plan_is_not_injected_every_turn(tmp_path: Path):
    """**计划不进每轮消息** —— 只在 update_plan 自己的工具结果里出现。

    参考实现在第 5 步把当前计划贴进对话。在我们的 cache-aware 布局下那是负收益：
    计划每变一次就改一段消息，那段之后的**前缀缓存全部失效**，而收益只是"模型
    多看见一遍自己刚写的东西"。这条测试盯住"注入次数 == 工具调用次数"。
    """
    snapshots: list[list[dict]] = []

    def record(messages, tools):  # noqa: ANN001 - MockLLM 回调签名
        snapshots.append([dict(m) for m in messages])
        return LLMResult(content="完成")

    engine = QueryEngine(
        MockLLM.script(
            LLMResult(content=None, tool_calls=[
                ToolCall(id="c1", name="update_plan",
                         arguments={"items": ["a", "b"], "statuses": ["in_progress", "pending"]}),
            ]),
            # 第二次调用前先做一次别的工具，把"轮与轮之间会不会多贴一份"也覆盖上
            LLMResult(content=None, tool_calls=[
                ToolCall(id="c2", name="glob", arguments={"pattern": "*"}),
            ]),
            record,
        ),
        ToolRegistry.default(tmp_path),
        workspace_root=tmp_path,
    )
    result = engine.run("随便一个任务")

    assert result.terminated_reason == "completed"
    assert len(snapshots) == 1
    plan_mentions = [
        m for m in snapshots[0]
        if isinstance(m.get("content"), str) and "计划清单（" in m["content"]
    ]
    assert len(plan_mentions) == 1, "计划被额外注入到消息里了（每轮贴 = 破坏前缀缓存）"
    assert plan_mentions[0]["role"] == "tool"  # 它只是 update_plan 的工具结果
