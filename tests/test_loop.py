"""M1-6 tests: agent/loop.py（QueryEngine 循环）。全部用 MockLLM，无网络。"""
from pathlib import Path

import pytest

from agent.llm import LLMResult, MockLLM, ToolCall, Usage
from agent.loop import DEFAULT_SYSTEM_PROMPT, QueryEngine
from agent.session import Session
from agent.tools.ask import build_ask_tool
from agent.tools.base import ToolRegistry
from pydantic import BaseModel


def make_engine(tmp_path: Path, llm: MockLLM, **kwargs) -> QueryEngine:
    return QueryEngine(
        llm,
        ToolRegistry.default(tmp_path),
        workspace_root=tmp_path,
        **kwargs,
    )


def test_simple_answer(tmp_path):
    engine = make_engine(tmp_path, MockLLM.text("完成了", usage=Usage(prompt_tokens=5, completion_tokens=3)))
    result = engine.run("总结一下")
    assert result.terminated_reason == "completed"
    assert result.steps == 1
    assert result.final_text == "完成了"
    assert result.usage.prompt_tokens == 5
    assert len(result.events) == 1
    assert result.events[0]["type"] == "llm_call"


def test_tool_then_answer(tmp_path):
    (tmp_path / "a.txt").write_text("hello\n", encoding="utf-8")
    engine = make_engine(
        tmp_path,
        MockLLM.script(
            MockLLM.tool("read", {"path": "a.txt"}).responses[0],
            LLMResult(content="读到了"),
        ),
    )
    result = engine.run("读 a.txt")
    assert result.terminated_reason == "completed"
    assert result.steps == 2
    # 消息历史包含工具结果消息
    tool_messages = [m for m in result.events if m["type"] == "tool_call"]
    assert len(tool_messages) == 1
    assert tool_messages[0]["name"] == "read"
    assert tool_messages[0]["success"] is True


def test_unknown_tool_recovered(tmp_path):
    """模型先调未知工具 → 收到"未知工具"fail 回喂 → 再给文本答案 → 完成。"""
    seen = {}

    def unknown_then_answer(messages, tools):
        # 第二次调用时断言：错误确实回喂给了模型
        if any(m["role"] == "tool" for m in messages):
            seen["recovered"] = messages[-1]["content"]
        return LLMResult(content="已修复路径")

    engine = make_engine(
        tmp_path,
        MockLLM.script(
            LLMResult(content=None, tool_calls=[ToolCall(id="c1", name="no_such_tool", arguments={})]),
            unknown_then_answer,
        ),
    )
    result = engine.run("试试未知工具")
    assert result.terminated_reason == "completed"
    assert seen.get("recovered")
    assert "未知工具" in seen["recovered"]
    assert "no_such_tool" in seen["recovered"]


def test_read_only_concurrent(tmp_path):
    """一次返回 2 个只读工具调用 → 两者都执行且结果都回填。"""
    (tmp_path / "a.txt").write_text("AAA\n", encoding="utf-8")
    (tmp_path / "b.txt").write_text("BBB\n", encoding="utf-8")
    engine = make_engine(
        tmp_path,
        MockLLM.script(
            LLMResult(
                content=None,
                tool_calls=[
                    ToolCall(id="c1", name="read", arguments={"path": "a.txt"}),
                    ToolCall(id="c2", name="read", arguments={"path": "b.txt"}),
                ],
            ),
            LLMResult(content="都读完了"),
        ),
        max_steps=5,
    )
    result = engine.run("读两个文件")
    assert result.terminated_reason == "completed"
    calls = [e for e in result.events if e["type"] == "tool_call"]
    assert [c["name"] for c in calls] == ["read", "read"]
    assert all(c["success"] for c in calls)
    # 两个结果都回填到消息（id 对应）
    tool_msgs = [m for m in result.events if m["type"] == "tool_call"]
    assert len(tool_msgs) == 2


def test_max_steps(tmp_path):
    tool_result_llm = MockLLM.tool("glob", {"pattern": "*.txt"}).responses[0]
    engine = make_engine(
        tmp_path,
        MockLLM([tool_result_llm] * 20),
        max_steps=3,
        loop_detection_window=10,  # 排除循环检测干扰
    )
    result = engine.run("一直调工具")
    assert result.terminated_reason == "max_steps"
    assert result.steps == 3
    assert "最大步数" in result.final_text


def test_loop_detected(tmp_path):
    tool_result_llm = MockLLM.tool("glob", {"pattern": "*.txt"}).responses[0]
    engine = make_engine(
        tmp_path,
        MockLLM([tool_result_llm] * 20),
        max_steps=25,
        loop_detection_window=4,
    )
    result = engine.run("重复循环")
    assert result.terminated_reason == "loop_detected"
    assert "重复循环" in result.final_text


def test_e2e_real_files(tmp_path):
    """脚本化工具链在真实文件上跑通：glob → read → edit → 文本回答。"""
    (tmp_path / "a.txt").write_text("hello\n", encoding="utf-8")
    engine = make_engine(
        tmp_path,
        MockLLM.script(
            MockLLM.tool("glob", {"pattern": "*.txt"}).responses[0],
            MockLLM.tool("read", {"path": "a.txt"}).responses[0],
            MockLLM.tool("edit", {"path": "a.txt", "old_string": "hello", "new_string": "hello world"}).responses[0],
            LLMResult(content="已修改并验证"),
        ),
        max_steps=10,
    )
    result = engine.run("把 hello 改成 hello world")
    assert result.terminated_reason == "completed"
    assert result.steps == 4
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "hello world\n"
    assert result.final_text == "已修改并验证"


def test_default_system_prompt_has_placeholder():
    assert "{repo_memory_block}" in DEFAULT_SYSTEM_PROMPT


def test_empty_response_recovered(tmp_path):
    """M1-9：模型第一次返回空白文本 → push continuation prompt 重试 → 第二次正常回答。"""
    engine = make_engine(
        tmp_path,
        MockLLM.script(
            LLMResult(content="", usage=Usage(prompt_tokens=1, completion_tokens=0)),
            LLMResult(content="完成", usage=Usage(prompt_tokens=1, completion_tokens=1)),
        ),
    )
    result = engine.run("任务")
    assert result.terminated_reason == "completed"
    assert result.steps == 2
    assert result.final_text == "完成"
    retries = [e for e in result.events if e["type"] == "empty_response_retry"]
    assert len(retries) == 1
    assert retries[0]["attempt"] == 1


def test_empty_response_gives_up(tmp_path):
    """M1-9：空响应超过重试上限 → 按 completed（空结论）结束，不无限重试。"""
    empty = LLMResult(content="", usage=Usage(prompt_tokens=1, completion_tokens=0))
    engine = make_engine(tmp_path, MockLLM([empty] * 10), empty_response_retries=2)
    result = engine.run("任务")
    assert result.terminated_reason == "completed"
    assert result.steps == 3  # 初始 1 次 + 2 次重试
    retries = [e for e in result.events if e["type"] == "empty_response_retry"]
    assert len(retries) == 2
    assert result.final_text == ""


# ---------- M6-6：system prompt 槽位注入 + 实时事件回调 ----------

def test_system_prompt_injects_workspace_root(tmp_path):
    """工作目录必须写进 system prompt —— 否则模型会瞎猜 /workspace、遍历磁盘找路径。"""
    seen = {}

    def capture(messages, tools):
        seen["system"] = messages[0]["content"]
        return LLMResult(content="ok")

    engine = make_engine(tmp_path, MockLLM([capture]))
    engine.run("任务")
    system_prompt = seen["system"]
    assert str(tmp_path.resolve()) in system_prompt
    assert "{workspace_root}" not in system_prompt      # 槽位已填
    assert "{repo_memory_block}" not in system_prompt
    assert "工作目录（沙箱根）就是" in system_prompt


def test_system_prompt_fills_platform_hint(tmp_path):
    """平台提示要按运行平台给出对应语法（避免 Windows 下用 ls/find/grep）。"""
    seen = {}

    def capture(messages, tools):
        seen["system"] = messages[0]["content"]
        return LLMResult(content="ok")

    make_engine(tmp_path, MockLLM([capture])).run("任务")
    assert ("cmd /c" in seen["system"]) or ("bash -lc" in seen["system"])


def test_custom_system_prompt_with_braces_does_not_crash(tmp_path):
    """自定义 prompt 含花括号（JSON 示例）时不能炸 —— 所以注入用 replace 而非 format。"""
    custom = '你是助手。工作目录 {workspace_root}。输出格式示例：{"ok": true} 和 {未知槽位}'
    seen = {}

    def capture(messages, tools):
        seen["system"] = messages[0]["content"]
        return LLMResult(content="ok")

    engine = make_engine(tmp_path, MockLLM([capture]), system_prompt=custom)
    engine.run("任务")
    assert str(tmp_path.resolve()) in seen["system"]
    assert '{"ok": true}' in seen["system"]          # 无关花括号原样保留
    assert "{未知槽位}" in seen["system"]             # 未知槽位不报错、不消失


# ---------- M8-P7：真跑验证挖出的"机制在、但没人把模型引向它" ----------

def _capture_system_prompt(tmp_path, registry) -> str:
    """跑一个只回文本的假模型，把 system prompt 截回来。"""
    seen = {}

    def capture(messages, tools):
        seen["system"] = messages[0]["content"]
        return LLMResult(content="ok")

    QueryEngine(MockLLM([capture]), registry, workspace_root=tmp_path).run("任务")
    return seen["system"]


def test_default_system_prompt_points_at_update_plan():
    """计划那一条必须**点名工具**，否则模型只会写正文、从不调 `update_plan`。

    这是真跑验证挖出来的（2026-09-11，DeepSeek 官方通路）：给一个明确三步的任务，
    模型 6 步做完了全部三件事，但 `update_plan` 调用次数是 **0** —— 原 prompt 只说
    「先用 3~6 步的简短计划」，它写不写正文都算满足，于是"计划清单跨回合"这个
    能力在真跑里一次都没被触发过（连正文计划都没写）。机制、单测、文档都在，
    缺的是**把模型引向它的那一句话**。

    `update_plan` 在 `ToolRegistry.default()` 里，每个入口都有，所以可以无条件点名。
    """
    assert "`update_plan`" in DEFAULT_SYSTEM_PROMPT


def test_ask_user_hint_follows_whether_the_tool_is_registered(tmp_path):
    """`{ask_user_hint}` 要跟着**注册表**走，因为 ask_user 是按入口注册的。

    `eval/runner.py` 用的 `ToolRegistry.default()` 里**刻意没有** ask_user
    （headless 没人能回答）。prompt 若写死「用 ask_user 提问」，评测里模型就会去调
    一个不存在的工具 —— 后果不是报错，而是白费一步（`_gate_and_run` 回喂「未知工具」
    并列出可用工具，模型再改道）。所以两种情形都要钉住。
    """
    without = _capture_system_prompt(tmp_path, ToolRegistry.default(tmp_path))
    assert "{ask_user_hint}" not in without                  # 槽位必须被填掉
    assert "`ask_user`" not in without                       # 没有工具就不提它

    registry = ToolRegistry.default(tmp_path)
    registry.register(build_ask_tool())
    with_tool = _capture_system_prompt(tmp_path, registry)
    assert "{ask_user_hint}" not in with_tool
    assert "`ask_user`" in with_tool                         # 有工具才点它的名


def test_no_system_prompt_slot_is_left_unfilled(tmp_path):
    """所有槽位都要被填掉。漏一个的表现是模型读到字面量 `{xxx}` 而不是内容 ——
    不报错，只是那一段提示等于不存在。"""
    registry = ToolRegistry.default(tmp_path)
    registry.register(build_ask_tool())
    system_prompt = _capture_system_prompt(tmp_path, registry)
    for slot in (
        "{workspace_root}", "{platform}", "{repo_memory_block}",
        "{skills_block}", "{ask_user_hint}",
    ):
        assert slot not in system_prompt, f"{slot} 没被填"


def test_on_event_receives_events_in_order(tmp_path):
    """on_event 实时回调：按发生顺序收到每个事件（CLI 流式输出靠它）。"""
    (tmp_path / "a.txt").write_text("hello\n", encoding="utf-8")
    received: list[tuple[str, int]] = []
    engine = make_engine(
        tmp_path,
        MockLLM.script(
            MockLLM.tool("read", {"path": "a.txt"}).responses[0],
            LLMResult(content="读到了"),
        ),
        on_event=lambda e: received.append((e["type"], e["step"])),
    )
    result = engine.run("读 a.txt")
    assert received == [("llm_call", 1), ("tool_call", 1), ("llm_call", 2)]
    assert received == [(e["type"], e["step"]) for e in result.events]


def test_on_event_not_duplicated_with_session(tmp_path):
    """session + on_event 同时存在时：JSONL 一份、回调一份，两者都不重复。"""
    from agent.session import Session

    (tmp_path / "a.txt").write_text("hello\n", encoding="utf-8")
    session = Session(tmp_path, "s-test", checkpoint_every=5)
    received = []
    engine = make_engine(
        tmp_path,
        MockLLM.script(
            MockLLM.tool("read", {"path": "a.txt"}).responses[0],
            LLMResult(content="读到了"),
        ),
        session=session,
        on_event=received.append,
    )
    result = engine.run("读 a.txt")
    assert len(received) == len(result.events) == 3

    import json
    lines = [
        json.loads(line)
        for line in session.trajectory_path.read_text(encoding="utf-8").splitlines()
    ]
    assert [line["type"] for line in lines] == [e["type"] for e in result.events]


def test_tool_call_event_carries_exit_code(tmp_path):
    """tool_call 事件要带 exit_code（success 只表示工具跑完，退出码才反映命令成没成）。"""
    (tmp_path / "a.txt").write_text("hello\n", encoding="utf-8")
    engine = make_engine(
        tmp_path,
        MockLLM.script(
            MockLLM.tool("bash", {"command": "exit 3"}).responses[0],
            LLMResult(content="命令失败了"),
        ),
    )
    result = engine.run("跑个会失败的命令")
    call = [e for e in result.events if e["type"] == "tool_call"][0]
    assert call["exit_code"] == 3


# ---------- await_user：澄清提问 + 暂停/续答（MiniCode awaitUser 语义移植） ----------

def _ask_engine(tmp_path: Path, llm, **kwargs) -> QueryEngine:
    """装好 ask_user 的引擎 —— 与两个入口的注册方式一致（不进 default()）。"""
    engine = make_engine(tmp_path, llm, **kwargs)
    engine.registry.register(build_ask_tool())
    return engine


def test_ask_user_ends_the_round(tmp_path):
    """模型提问 → 本轮结束，terminated_reason=await_user，问题原文就是 final_text。"""
    engine = _ask_engine(
        tmp_path,
        MockLLM.script(
            MockLLM.tool("ask_user", {"question": "新函数叫什么名字？"}).responses[0],
            LLMResult(content="不该走到这里"),  # 备用：暂停后模型不该再被调用
        ),
    )
    result = engine.run("给项目加个工具函数")

    assert result.terminated_reason == "await_user"
    assert result.steps == 1, "提问后本轮就该结束，不该再往下走"
    assert "新函数叫什么名字？" in result.final_text
    kinds = [e["type"] for e in result.events]
    assert "await_user" in kinds
    assert kinds.count("llm_call") == 1, "暂停后模型不该被再调用一次"


def test_ask_user_keeps_tool_call_pairing_intact(tmp_path):
    """同批里排在 ask_user 后面的调用必须补上配对的 tool 结果。

    **这条钉的是一个会让会话彻底报废的失败模式**：assistant 消息的 tool_calls
    里有三个 id，只有前两个有对应的 tool 消息 —— OpenAI 兼容端点在**下一次**请求时
    直接 400（tool_call_id 无对应结果），而报错现场看起来和 ask_user 毫无关系。
    参考实现在这里直接 break，留下孤儿 id；我们不跟着抄。

    用真检查点验，因为那份 messages 才是下一次请求真正发出去的东西。
    """
    (tmp_path / "a.txt").write_text("hello\n", encoding="utf-8")
    session = Session(tmp_path, "s-pairing", checkpoint_every=5)
    llm = MockLLM.script(
        LLMResult(
            content=None,
            tool_calls=[
                ToolCall(id="c1", name="bash", arguments={"command": "echo hi"}),
                ToolCall(id="c2", name="ask_user", arguments={"question": "用哪个名字？"}),
                ToolCall(id="c3", name="read", arguments={"path": "a.txt"}),
            ],
        ),
        LLMResult(content="不该走到这里"),
    )
    engine = _ask_engine(tmp_path, llm, session=session)
    result = engine.run("多调用一批")

    assert result.terminated_reason == "await_user"
    # checkpoint_every=5，第 1 步能读到检查点**正是 force 在起作用**（见下一条测试）
    _, restored = Session.from_checkpoint(tmp_path, "s-pairing")
    messages = restored.messages

    asked = [m for m in messages if m.get("tool_call_id") == "c2"]
    assert asked and "用哪个名字？" in asked[0]["content"]
    skipped = [m for m in messages if m.get("tool_call_id") == "c3"]
    assert skipped, "后面的调用被丢了 → 孤儿 tool_call_id，下一次请求会 400"
    assert "跳过" in skipped[0]["content"]

    # 不变量：assistant 声明过的每个 tool_call_id 都有且只有一条 tool 结果
    declared: list[str] = []
    for msg in messages:
        if msg.get("role") == "assistant":
            declared.extend(c["id"] for c in (msg.get("tool_calls") or []))
    answered = [m["tool_call_id"] for m in messages if m.get("role") == "tool"]
    assert sorted(declared) == sorted(answered) == ["c1", "c2", "c3"]


def test_ask_user_persists_even_below_checkpoint_interval(tmp_path):
    """`checkpoint_every=5` 时第 3 步问的问题也必须落盘（钉住 force）。

    不加 force 的失败模式很隐蔽：默认 5 步节流，问题出现在第 3 步 → 本轮结束、
    进程退出 → 下一次 tick 永远不会来 → 问题没进检查点。`--resume` 能恢复会话，
    但恢复出来的那份 messages 里**没有问题**，用户对着一个不知道在问什么的会话
    回答。功能"看起来能用"（resume 成功、模型也回话了），只有真按这个顺序跑才看得见。
    """
    session = Session(tmp_path, "s-force", checkpoint_every=5)
    engine = _ask_engine(
        tmp_path,
        MockLLM.script(
            MockLLM.tool("read", {"path": "a.txt"}).responses[0],   # 第 1 步
            MockLLM.tool("glob", {"pattern": "*.txt"}).responses[0],  # 第 2 步
            MockLLM.tool("ask_user", {"question": "要改哪个文件？"}).responses[0],  # 第 3 步
            LLMResult(content="不该走到这里"),
        ),
        session=session,
    )
    (tmp_path / "a.txt").write_text("hello\n", encoding="utf-8")
    result = engine.run("改点东西")

    assert result.terminated_reason == "await_user"
    assert result.steps == 3
    checkpoints = session.list_checkpoints()
    assert checkpoints, "第 3 步提问 + checkpoint_every=5，没有 force 就不会有检查点"
    _, restored = Session.from_checkpoint(tmp_path, "s-force")
    assert any("要改哪个文件？" in str(m.get("content")) for m in restored.messages)


def test_denied_ask_user_does_not_pause(tmp_path):
    """被门禁拦下的 ask_user **不算提问** —— 暂停与否由「执行结果」决定，不由工具名决定。

    否则一个被拒绝的提问会把整轮静默终止掉，而模型收到的只是一条"权限拒绝"，
    它会以为还能继续修（这正是它被训练成会做的事），人却看到进程停了。
    """
    from agent.permissions import Decision

    class DenyAll:
        def check(self, name, arguments, ctx, *, details=None):  # noqa: ANN001 - 引擎接口
            return Decision.DENY

        def describe(self, name, arguments, *, details=None):  # noqa: ANN001
            return "全部拒绝（测试替身）"

    captured: list[str] = []

    def then_answer(messages, tools):  # noqa: ANN001 - MockLLM 回调签名
        captured.extend(m["content"] for m in messages if m.get("role") == "tool")
        return LLMResult(content="那就不问了")

    engine = _ask_engine(
        tmp_path,
        MockLLM.script(
            MockLLM.tool("ask_user", {"question": "能说说吗？"}).responses[0],
            then_answer,
        ),
        permissions=DenyAll(),
    )
    result = engine.run("需要澄清的任务")

    assert result.terminated_reason == "completed", "被拒的提问不该终止本轮"
    assert any("权限拒绝" in text for text in captured), captured


# ---------- M9-1：改前 diff review —— 人看到的必须是"改之前" ----------

def test_edit_diff_is_available_before_the_write(tmp_path):
    """确认回调被调用的**那一刻**，磁盘上必须仍是原文。

    这是 M9-1 的全部意义所在，也是它唯一会悄悄失效的地方：把预览挪到
    `_gate_and_run` 里 check() 之后、或者挪进 execute() 里，功能照样"能跑"、
    确认框照样显示 diff —— 只是那份 diff 已经是**事后**的了，人看着一份
    改完的 diff 点"允许"。没有任何东西会报错，所以必须由这条测试钉住。

    断在两个时刻之间：回调里读一次盘，执行完再读一次。
    """
    from agent.permissions import PermissionsEngine

    path = tmp_path / "calc.py"
    path.write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    seen: dict[str, object] = {}

    def confirm(question: str) -> str:
        seen["question"] = question
        seen["content_at_confirm"] = path.read_text(encoding="utf-8")
        return "allow_once"

    engine = make_engine(
        tmp_path,
        MockLLM.script(
            MockLLM.tool(
                "edit",
                {"path": "calc.py", "old_string": "a - b", "new_string": "a + b"},
            ).responses[0],
            LLMResult(content="改好了"),
        ),
        permissions=PermissionsEngine(
            tmp_path, confirm=confirm, rules={"tools": {"edit": "ask"}}
        ),
    )
    result = engine.run("修掉这个减法 bug")

    assert result.terminated_reason == "completed"
    # 1) 人拿到的确实是 diff
    assert "-    return a - b" in seen["question"]
    assert "+    return a + b" in seen["question"]
    # 2) 而那一刻文件还是旧的 —— 没有这一条，整项功能等于没做
    assert seen["content_at_confirm"] == "def add(a, b):\n    return a - b\n"
    # 3) 人点允许之后才真的落盘
    assert path.read_text(encoding="utf-8") == "def add(a, b):\n    return a + b\n"


def test_preview_failure_does_not_break_the_turn(tmp_path):
    """预览抛异常 → 记事件 + 忽略，本轮照常跑完。

    （注意要装一个权限引擎：没有确认交互时预览**根本不会被计算**，
    见下面那条测试。headless 评测就属于这种情形，这是有意省下的开销。）

    `preview` 是**信息**不是**判定**，所以它和权限链的处理方式相反：权限链
    算不出来必须大声失败（漏一次判定就是漏一次门禁），预览算不出来只该退回到
    原来的确认框。实现方的 bug 不该变成用户的"任务做不下去"。
    """
    from agent.permissions import PermissionsEngine
    from agent.tools.base import Tool, ToolContext, ToolResult

    class BoomInput(BaseModel):
        pass

    class ExplodingPreview(Tool):
        name = "boom"
        description = "预览会炸的写工具（测试用）"
        input_model = BoomInput

        @classmethod
        def is_read_only(cls) -> bool:
            return False

        def preview(self, arguments: dict, ctx: ToolContext) -> str | None:
            raise RuntimeError("预览实现有 bug")

        def execute(self, args, ctx: ToolContext) -> ToolResult:
            return ToolResult.ok("照常执行了")

    engine = make_engine(
        tmp_path,
        MockLLM.script(
            MockLLM.tool("boom", {}).responses[0],
            LLMResult(content="完成了"),
        ),
        permissions=PermissionsEngine(tmp_path),
    )
    engine.registry.register(ExplodingPreview())
    result = engine.run("跑一个预览会炸的工具")

    assert result.terminated_reason == "completed"
    calls = [e for e in result.events if e["type"] == "tool_call"]
    assert calls[0]["success"] is True, "预览失败不该影响执行"
    failures = [e for e in result.events if e["type"] == "preview_failed"]
    assert failures and "RuntimeError" in failures[0]["error"]


def test_preview_is_not_computed_without_a_permissions_engine(tmp_path):
    """没有权限引擎 = 没有任何确认交互 → 不该白算一份 diff。

    headless（`eval/runner.py`）走的就是这条路：那里没人能看预览，算了纯属
    浪费 —— 而 `edit` 的预览要把整个文件读进来做 diff，在评测里是实打实的
    每步开销。所以 `_preview` 挂在权限块**内部**，这条测试钉住那个位置。
    """
    from agent.tools.base import Tool, ToolContext, ToolResult

    class RecordingInput(BaseModel):
        pass

    calls: list[dict] = []

    class Recording(Tool):
        name = "rec"
        description = "记录 preview 是否被调用（测试用）"
        input_model = RecordingInput

        @classmethod
        def is_read_only(cls) -> bool:
            return False

        def preview(self, arguments: dict, ctx: ToolContext) -> str | None:
            calls.append(arguments)
            return "不该被算出来"

        def execute(self, args, ctx: ToolContext) -> ToolResult:
            return ToolResult.ok("执行了")

    engine = make_engine(
        tmp_path,
        MockLLM.script(
            MockLLM.tool("rec", {}).responses[0], LLMResult(content="完成了")
        ),
    )
    engine.registry.register(Recording())
    result = engine.run("跑一下")

    assert result.terminated_reason == "completed"
    assert calls == [], "没有确认交互却算了预览"
