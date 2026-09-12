"""M2-1 tests: agent/permissions.py（规则引擎 + 决策粒度 + 黑名单 + 沙箱 + 第三方工具）。"""
import json
from pathlib import Path

import pytest
from pydantic import BaseModel

from agent.llm import LLMResult, MockLLM
from agent.loop import QueryEngine
from agent.permissions import Decision, PermissionsEngine, _irreversible_kind
from agent.tools.base import Tool, ToolContext, ToolRegistry, ToolResult
from agent.tools.bash import command_path_verdict
from agent.tools.files import ReadTool


def make_ctx(tmp_path: Path) -> ToolContext:
    return ToolContext(workspace_root=tmp_path)


def make_engine(tmp_path: Path, llm: MockLLM, **kwargs) -> QueryEngine:
    return QueryEngine(
        llm,
        ToolRegistry.default(tmp_path),
        workspace_root=tmp_path,
        **kwargs,
    )


# ---------- 默认行为 ----------

def test_default_allows(tmp_path):
    engine = PermissionsEngine(tmp_path)
    ctx = make_ctx(tmp_path)
    assert engine.check("bash", {"command": "ls"}, ctx) is Decision.ALLOW
    assert engine.check("read", {"path": "a.txt"}, ctx) is Decision.ALLOW
    assert engine.check("edit", {"path": "a.txt"}, ctx) is Decision.ALLOW
    assert engine.check("glob", {"pattern": "**/*.py"}, ctx) is Decision.ALLOW


# ---------- 危险命令 → ask ----------

def test_dangerous_asks_without_confirm(tmp_path):
    """危险命令默认 ask；无 confirm 回调时返回 ASK（由调用方处理）。"""
    engine = PermissionsEngine(tmp_path)
    ctx = make_ctx(tmp_path)
    assert engine.check("bash", {"command": "rm -rf x"}, ctx) is Decision.ASK
    assert engine.check("bash", {"command": "git push origin main"}, ctx) is Decision.ASK
    # 安全命令不受影响
    assert engine.check("bash", {"command": "git status"}, ctx) is Decision.ALLOW


def test_confirm_none_denies(tmp_path):
    """确认回调返回 None（用户拒绝）→ deny，且不记忆（once）。"""
    asked = []
    engine = PermissionsEngine(tmp_path, confirm=lambda q: asked.append(q) or None)
    ctx = make_ctx(tmp_path)
    assert engine.check("bash", {"command": "rm -rf x"}, ctx) is Decision.DENY
    # 拒绝不常驻 → 下次仍 ask
    assert engine.check("bash", {"command": "rm -rf x"}, ctx) is Decision.DENY
    assert len(asked) == 2
    assert "rm -rf x" in asked[0]


def test_confirm_allow_always_remembered(tmp_path):
    """allow_always → 该命令常驻放行，不再重复 ask；记忆按命令隔离。"""
    asked = []
    engine = PermissionsEngine(tmp_path, confirm=lambda q: asked.append(q) or "allow_always")
    ctx = make_ctx(tmp_path)
    assert engine.check("bash", {"command": "rm -rf x"}, ctx) is Decision.ALLOW
    assert engine.check("bash", {"command": "rm -rf x"}, ctx) is Decision.ALLOW
    assert len(asked) == 1  # 第二次走常驻记忆，不再 ask
    # 不同命令不共享记忆 → 触发新的确认（asked 变为 2）
    assert engine.check("bash", {"command": "rm -rf y"}, ctx) is Decision.ALLOW
    assert len(asked) == 2


def test_confirm_allow_once_not_remembered(tmp_path):
    asked = []
    engine = PermissionsEngine(tmp_path, confirm=lambda q: asked.append(q) or "allow_once")
    ctx = make_ctx(tmp_path)
    assert engine.check("bash", {"command": "rm -rf x"}, ctx) is Decision.ALLOW
    assert engine.check("bash", {"command": "rm -rf x"}, ctx) is Decision.ALLOW
    assert len(asked) == 2  # once 不记忆 → 每次都要确认


def test_confirm_deny_always_remembered(tmp_path):
    asked = []
    engine = PermissionsEngine(tmp_path, confirm=lambda q: asked.append(q) or "deny_always")
    ctx = make_ctx(tmp_path)
    assert engine.check("bash", {"command": "rm -rf x"}, ctx) is Decision.DENY
    assert engine.check("bash", {"command": "rm -rf x"}, ctx) is Decision.DENY
    assert len(asked) == 1


def test_confirm_allow_turn_remembered(tmp_path):
    """allow_turn → 本回合内同命令放行，回合结束后失效。"""
    asked = []
    engine = PermissionsEngine(tmp_path, confirm=lambda q: asked.append(q) or "allow_turn")
    ctx = make_ctx(tmp_path)
    assert engine.check("bash", {"command": "rm -rf x"}, ctx) is Decision.ALLOW
    assert engine.check("bash", {"command": "rm -rf x"}, ctx) is Decision.ALLOW
    assert len(asked) == 1
    # 新回合（同 workspace 新实例，无 confirm）→ 又需确认（turn 记忆不跨实例）
    engine2 = PermissionsEngine(tmp_path)
    assert engine2.check("bash", {"command": "rm -rf x"}, ctx) is Decision.ASK


def test_new_turn_clears_turn_memory_on_the_same_engine(tmp_path):
    """`new_turn()` 才是"本回合"的边界 —— 不必新建实例（M9-5）。

    上面那条把「回合结束」定义成**新建引擎实例**，在单发 CLI / eval 里这是对的
    （一个回合 = 一个进程）。常驻 REPL 里不成立：引擎实例是同一个（换会话才换
    引擎），于是 `_turn` 从不清空，「2) 本回合允许」悄悄变成了"允许到进程退出"
    —— 与菜单上写着的语义直接矛盾，而且没有任何出口能撤销它。
    """
    asked = []
    engine = PermissionsEngine(tmp_path, confirm=lambda q: asked.append(q) or "allow_turn")
    ctx = make_ctx(tmp_path)
    assert engine.check("bash", {"command": "rm -rf x"}, ctx) is Decision.ALLOW
    assert len(asked) == 1

    engine.new_turn()

    # 同一条命令、同一个引擎 → 重新回到确认（旧行为下这里 len(asked) 还是 1）
    assert engine.check("bash", {"command": "rm -rf x"}, ctx) is Decision.ALLOW
    assert len(asked) == 2


def test_new_turn_keeps_always_memory(tmp_path):
    """只清"本回合"那份，**不能**把常驻记忆一起清掉 —— 用户选「3) 一直允许」
    正是为了不再被问。两个方向都要钉住，否则"清干净一点"看起来更保险。"""
    asked = []
    engine = PermissionsEngine(tmp_path, confirm=lambda q: asked.append(q) or "allow_always")
    ctx = make_ctx(tmp_path)
    assert engine.check("bash", {"command": "rm -rf x"}, ctx) is Decision.ALLOW

    engine.new_turn()

    assert engine.check("bash", {"command": "rm -rf x"}, ctx) is Decision.ALLOW
    assert len(asked) == 1


# ---------- 规则文件 ----------

def test_rule_tool_dangerous_deny(tmp_path):
    engine = PermissionsEngine(
        tmp_path, rules={"tools": {"bash": {"dangerous": "deny"}}}
    )
    ctx = make_ctx(tmp_path)
    assert engine.check("bash", {"command": "rm -rf x"}, ctx) is Decision.DENY


def test_rule_tool_string_deny(tmp_path):
    engine = PermissionsEngine(tmp_path, rules={"tools": {"bash": "deny"}})
    ctx = make_ctx(tmp_path)
    assert engine.check("bash", {"command": "ls"}, ctx) is Decision.DENY


def test_command_deny_list(tmp_path):
    engine = PermissionsEngine(tmp_path, rules={"commands": {"deny": ["git push*"]}})
    ctx = make_ctx(tmp_path)
    assert engine.check("bash", {"command": "git push origin main"}, ctx) is Decision.DENY
    assert engine.check("bash", {"command": "git status"}, ctx) is Decision.ALLOW


def test_command_deny_wins_over_allow(tmp_path):
    """同一命令同时命中 allow 与 deny → deny 优先（安全优先）。"""
    engine = PermissionsEngine(
        tmp_path,
        rules={"commands": {"allow": ["git push*"], "deny": ["git push*"]}},
    )
    ctx = make_ctx(tmp_path)
    assert engine.check("bash", {"command": "git push origin main"}, ctx) is Decision.DENY


# ---------- 路径 / 编辑 ----------

def test_path_deny(tmp_path):
    (tmp_path / ".env").write_text("SECRET", encoding="utf-8")
    engine = PermissionsEngine(tmp_path, rules={"paths": {"deny": [".env"]}})
    ctx = make_ctx(tmp_path)
    assert engine.check("read", {"path": ".env"}, ctx) is Decision.DENY
    assert engine.check("read", {"path": "other.txt"}, ctx) is Decision.ALLOW


def test_path_out_of_sandbox(tmp_path):
    """沙箱外路径 → 硬 deny（即使无任何规则）。"""
    engine = PermissionsEngine(tmp_path)
    ctx = make_ctx(tmp_path)
    outside = tmp_path.parent / "secret.txt"
    assert engine.check("read", {"path": str(outside)}, ctx) is Decision.DENY
    assert engine.check("read", {"path": "../secret.txt"}, ctx) is Decision.DENY
    assert engine.check("write", {"path": str(outside)}, ctx) is Decision.DENY
    assert engine.check("edit", {"path": str(outside)}, ctx) is Decision.DENY


# ---------- S16：bash 命令文本里的路径（与 read/write/edit 同一个不变式） ----------
#
# 这一节的核心不是"多了一条判据"，而是**同一条沙箱规则不能有两种问法两种结果**。
# 所以最有价值的一条是 `test_s16_sandbox_semantics_do_not_drift`。

@pytest.mark.parametrize(
    "raw",
    [
        "../.env",           # 相对逃逸：S16 实测那条路
        "../secret.txt",
        "..\\..\\x.txt",
        "/etc/passwd",       # 绝对越界
        "~/secrets.txt",
        "a.txt",             # 沙箱内（对照组：不能一律判越界）
        "sub/a.txt",
    ],
)
def test_s16_sandbox_semantics_do_not_drift(tmp_path, raw):
    """**这是「沙箱语义只有一份」的牙齿。**

    同一组原始路径，三处必须给同一个答案：

    - `PermissionsEngine._resolve`（引擎层：read/write/edit 的硬拒依据）
    - `command_path_verdict`（bash 命令文本层）
    - `ReadTool`（工具层：真执行时再判一次）

    合并前工具层与引擎层**各有一份逐字相同的拷贝**，而"两份拷贝迟早漂移"是
    本项目已经吃过亏的缺陷类。谁再抄一份、或改 `resolve_in_workspace` 时漏掉
    某一路，都会在这里红。

    断言写成**三方相等**而不是各自对一个写死的期望值：真正的职责只有一个
    （`resolve_in_workspace`），这条要守的是"没有第二套语义"。
    """
    engine = PermissionsEngine(tmp_path)
    ctx = make_ctx(tmp_path)
    command = f"cat {raw}"

    engine_out = engine._resolve(raw, ctx) is None
    deny, ask = command_path_verdict(command, cwd=tmp_path, workspace_root=tmp_path)
    bash_out = raw in deny or raw in ask
    read_out = "路径越界沙箱" in (ReadTool().run({"path": raw}, ctx).error or "")

    assert engine_out == bash_out == read_out, (
        f"{raw!r}: engine={engine_out} bash={bash_out} read={read_out}"
    )


def test_s16_sandbox_authority_pins_ground_truth(tmp_path):
    """钉住**权威本身**的输出，不只是"三方一致"。

    上面那条三方相等能抓住"有人又抄了一份实现"，但**抓不住"唯一那份被改坏"**——
    改坏 `resolve_in_workspace` 会让三方**一起**给出错误答案，一致地错。
    两条缺一不可：一条守"没有第二套语义"，这条守"那一套是对的"。
    """
    from agent.tools.files import resolve_in_workspace

    def verdict(raw: str, cwd: Path | None = None) -> bool:
        return (
            resolve_in_workspace(
                raw, cwd=cwd or tmp_path, workspace_root=tmp_path
            )
            is not None
        )

    assert verdict("../.env") is False
    assert verdict("../secret.txt") is False
    assert verdict("/etc/passwd") is False
    assert verdict("~/secrets.txt") is False      # `~` 按 shell 真实行为展开
    assert verdict("a.txt") is True               # 对照组：不是一律判越界
    assert verdict("sub/a.txt") is True
    # 相对路径锚定 **cwd** 而不是 workspace_root —— 从子目录 `../x.txt` 回到
    # 工作区根部**不算逃逸**。锚错了会把这个正常写法判成越界。
    assert verdict("x.txt", cwd=tmp_path / "sub") is True
    assert verdict("../x.txt", cwd=tmp_path / "sub") is True


def test_s16_layering_deny_vs_ask(tmp_path):
    """分层的**内容**：相对逃逸硬拒，绝对越界/含变量只 ask。

    两档的确定性不同 ⇒ 动作不同。合成一个布尔会让分层失效 —— 而分层的意义
    正是"确定的那档硬拒、可能误杀的那档可恢复"。
    """
    engine = PermissionsEngine(tmp_path)
    ctx = make_ctx(tmp_path)

    assert engine.check("bash", {"command": "cat ../.env"}, ctx) is Decision.DENY
    # 绝对路径可能只是 grep 的模式串 —— 误杀必须可恢复
    assert engine.check("bash", {"command": 'grep -rn "/usr/lib" .'}, ctx) is Decision.ASK
    # 变量取值不可确定
    assert engine.check("bash", {"command": "cat $HOME/.env"}, ctx) is Decision.ASK
    # 反向：沙箱内的写法一律放行
    assert engine.check(
        "bash", {"command": "python -m pytest tests/ -q"}, ctx
    ) is Decision.ALLOW


def test_s16_deny_beats_allow_rules(tmp_path):
    """收紧类判据必须在**规则层之前** —— 否则一条 `cat *` 就把沙箱让掉了。"""
    engine = PermissionsEngine(tmp_path, rules={"commands": {"allow": ["cat *"]}})
    ctx = make_ctx(tmp_path)
    assert engine.check("bash", {"command": "cat ../.env"}, ctx) is Decision.DENY
    # 规则本身仍然有效（不是把整类命令都锁死了）
    assert engine.check("bash", {"command": "cat a.txt"}, ctx) is Decision.ALLOW


def test_s16_deny_beats_allow_always_memory(tmp_path):
    """同一件事在**记忆**层：`allow_always` 也绕不过沙箱硬拒。

    位置是硬要求：步骤 1' 必须在 `_always`/`_turn` 查表**之前**。否则用户开过
    一次 `allow_always` 就永久失效 —— 而"记得越久越省事"正是他去开它的原因，
    两个方向正好相反（同 `_apply_taint_ceiling` 那条 docstring）。

    这里**直接写 `_always`** 而不是走确认框：记忆键就是**整条命令文本**
    （`_classify` 对 bash 返回 `("command", command)`），只有直接种值才造得出
    "同一条命令既有 allow_always 记忆、又命中相对逃逸"这个局面。本条测的是
    **位置**，不是写入机制。
    """
    engine = PermissionsEngine(tmp_path)
    ctx = make_ctx(tmp_path)
    engine._always["cat ../.env"] = Decision.ALLOW
    assert engine.check("bash", {"command": "cat ../.env"}, ctx) is Decision.DENY


def test_s16_confirm_box_names_the_offending_path(tmp_path):
    """确认框必须说清**是哪条路径越界**（本仓纪律：拒绝要带出处与解除方式）。

    一个不说理由的确认框，训练出的是不看理由的人。
    """
    asked: list[str] = []
    engine = PermissionsEngine(
        tmp_path, confirm=lambda q: asked.append(q) or "allow_once"
    )
    ctx = make_ctx(tmp_path)

    assert engine.check(
        "bash", {"command": 'grep -rn "/usr/lib" .'}, ctx
    ) is Decision.ALLOW
    assert asked, "ask 档必须真的弹确认框（它是可恢复的那一档）"
    assert "[沙箱]" in asked[0]
    assert "/usr/lib" in asked[0]


def test_s16_denial_hint_states_how_to_unblock(tmp_path):
    """两档的**解除方式措辞必须不同** —— 这正是分档在用户侧的体现。"""
    engine = PermissionsEngine(tmp_path)
    ctx = make_ctx(tmp_path)

    deny_hint = engine.denial_hint("bash", {"command": "cat ../.env"}, ctx=ctx)
    assert deny_hint and "../.env" in deny_hint
    assert "不能用 allow_* 绕过" in deny_hint

    ask_hint = engine.denial_hint("bash", {"command": "cat $HOME/.env"}, ctx=ctx)
    assert ask_hint and "allow_once" in ask_hint

    # 无关命令不产出处（否则所有拒绝都会套上沙箱文案）
    assert engine.denial_hint("bash", {"command": "git status"}, ctx=ctx) is None


def test_s16_gate_block_carries_the_sandbox_reason_end_to_end(tmp_path):
    """端到端：拒绝文案里必须**看得见是哪条路径越界** —— 两处接线各一条断言。

    只测 `describe()` / `denial_hint()` 的返回值是不够的：那句话是**被 loop
    组装进 gate_block 的**（`loop.py` 的 DENY 分支用前者、ASK 分支用后者）。
    接线断在那一侧时，引擎自己再对也没用 —— 这一节的两条断言就是那两处接线的牙齿。
    """
    def run_once(command: str):
        engine = make_engine(
            tmp_path,
            MockLLM.script(
                MockLLM.tool("bash", {"command": command}).responses[0],
                LLMResult(content="明白了"),
            ),
            permissions=PermissionsEngine(tmp_path),
        )
        result = engine.run("照做")
        blocks = [e for e in result.events if e["type"] == "gate_block"]
        assert blocks and blocks[0]["source"] == "permissions", blocks
        return blocks[0]["reason"]

    # DENY 档：走 describe()（loop 的 DENY 分支）
    deny_reason = run_once("cat ../.env")
    assert "../.env" in deny_reason
    assert "不能用 allow_* 绕过" in deny_reason

    # ASK 档（无确认交互 → 按拒绝处理）：走 denial_hint()（loop 的 ASK 分支）
    ask_reason = run_once('grep -rn "/usr/lib" .')
    assert "/usr/lib" in ask_reason
    assert "allow_once" in ask_reason, ask_reason


def test_edit_deny(tmp_path):
    engine = PermissionsEngine(tmp_path, rules={"edits": {"deny": ["agent/llm.py"]}})
    ctx = make_ctx(tmp_path)
    assert engine.check("edit", {"path": "agent/llm.py"}, ctx) is Decision.DENY
    assert engine.check("edit", {"path": "agent/loop.py"}, ctx) is Decision.ALLOW


def test_load_rules_file(tmp_path):
    rules_file = tmp_path / "permissions.json"
    rules_file.write_text(
        json.dumps({"commands": {"deny": ["git push*"]}}), encoding="utf-8"
    )
    engine = PermissionsEngine(tmp_path, rules_path=rules_file)
    ctx = make_ctx(tmp_path)
    assert engine.check("bash", {"command": "git push origin main"}, ctx) is Decision.DENY
    assert engine.check("bash", {"command": "git status"}, ctx) is Decision.ALLOW


# ---------- 第三方工具（MCP）默认不放行 ----------

def test_external_tool_asks_by_default(tmp_path):
    """未授权的第三方工具 → ASK（不是默认 allow）；内置工具不受影响。

    这条钉住的是 H2：外部工具曾经落到 `("tool", name)` 分类 → `_rule_check`
    一路走到兜底的 `return ALLOW`，等于接上第三方 server 就零策略放行。
    """
    engine = PermissionsEngine(tmp_path, external_tools=["remote_read"])
    ctx = make_ctx(tmp_path)
    assert engine.check("remote_read", {"uri": "file:///c:/secret"}, ctx) is Decision.ASK
    assert engine.check("bash", {"command": "ls"}, ctx) is Decision.ALLOW


def test_external_tool_allowed_by_rule(tmp_path):
    """显式写进 external.allow → 放行（授权是配置动作，不是默认行为）。"""
    engine = PermissionsEngine(
        tmp_path, external_tools=["remote_read"], rules={"external": {"allow": ["remote_read"]}}
    )
    ctx = make_ctx(tmp_path)
    assert engine.check("remote_read", {}, ctx) is Decision.ALLOW


def test_allow_external_is_additive(tmp_path):
    """规则文件的 external.allow 与 mcp.json 的 allow 是**并集**（无顺序依赖）。"""
    rules_file = tmp_path / "permissions.json"
    rules_file.write_text(json.dumps({"external": {"allow": ["a"]}}), encoding="utf-8")
    engine = PermissionsEngine(tmp_path, rules_path=rules_file, external_tools=["a", "b"])
    engine.allow_external(["b"])
    ctx = make_ctx(tmp_path)
    assert engine.check("a", {}, ctx) is Decision.ALLOW
    assert engine.check("b", {}, ctx) is Decision.ALLOW


def test_external_tool_confirm_allow_always_remembers(tmp_path):
    """授权也可以是交互式的：用户对第三方工具选 allow_always → 该工具常驻放行。"""
    asked = []
    engine = PermissionsEngine(
        tmp_path,
        external_tools=["remote_read"],
        confirm=lambda q: asked.append(q) or "allow_always",
    )
    ctx = make_ctx(tmp_path)
    assert engine.check("remote_read", {}, ctx) is Decision.ALLOW
    assert engine.check("remote_read", {}, ctx) is Decision.ALLOW
    assert len(asked) == 1
    assert "第三方" in asked[0]  # 确认文案点明它不受沙箱约束


def test_external_classification_beats_path_classification(tmp_path):
    """名字同时像内置工具时，`external_tools` 里的名字按外部处理（更严的方向）。

    正常情况下不会出现：MCP 工具与内置重名时 `load_mcp_servers` 会加 `<别名>__`
    前缀。这里钉的是**优先级**——万一有人手工传进一个 `read`，也只会更严不会更松。
    """
    engine = PermissionsEngine(tmp_path, external_tools=["read"])
    ctx = make_ctx(tmp_path)
    assert engine.check("read", {"path": "a.txt"}, ctx) is Decision.ASK


def test_denial_hint_names_the_fix(tmp_path):
    """拒绝理由带出处与解除方式；无关工具不给误导性提示。"""
    engine = PermissionsEngine(tmp_path, external_tools=["remote_read"])
    hint = engine.denial_hint("remote_read")
    assert hint is not None
    assert "allow" in hint and "remote_read" in hint
    assert engine.denial_hint("bash") is None


# ---------- 循环集成 ----------

def test_loop_permissions_deny(tmp_path):
    """权限 deny 的命令 → 工具失败回喂模型（含"权限拒绝"），agent 改道完成。"""
    seen = {}

    def then_answer(messages, tools):
        if any(m["role"] == "tool" for m in messages):
            seen["feedback"] = messages[-1]["content"]
        return LLMResult(content="改用安全操作完成")

    engine = make_engine(
        tmp_path,
        MockLLM.script(
            MockLLM.tool("bash", {"command": "rm -rf x"}).responses[0],
            then_answer,
        ),
        permissions=PermissionsEngine(tmp_path, rules={"commands": {"deny": ["rm -rf *"]}}),
    )
    result = engine.run("清理目录")
    assert result.terminated_reason == "completed"
    calls = [e for e in result.events if e["type"] == "tool_call"]
    assert calls[0]["success"] is False
    assert "权限拒绝" in seen["feedback"]


def test_loop_permissions_dangerous_ask(tmp_path):
    """危险命令 → ASK → confirm allow_always → 放行执行；同命令第二次不再问。"""
    (tmp_path / "x").write_text("hi\n", encoding="utf-8")
    confirm_calls = []

    def confirm(question):
        confirm_calls.append(question)
        return "allow_always"

    engine = make_engine(
        tmp_path,
        MockLLM.script(
            MockLLM.tool("bash", {"command": "rm -rf ./x"}).responses[0],
            MockLLM.tool("bash", {"command": "rm -rf ./x"}).responses[0],
            LLMResult(content="完成"),
        ),
        permissions=PermissionsEngine(tmp_path, confirm=confirm),
    )
    result = engine.run("删掉 x")
    assert result.terminated_reason == "completed"
    assert len(confirm_calls) == 1  # allow_always 记住 → 第二次不重复问
    calls = [e for e in result.events if e["type"] == "tool_call"]
    assert len(calls) == 2
    assert all(c["success"] for c in calls)  # 权限放行，命令真的执行


def test_loop_permissions_ask_headless_denies(tmp_path):
    """无 confirm 回调时 ASK 视为未批准（安全默认）→ 危险命令不执行。"""
    seen = {}

    def then_answer(messages, tools):
        if any(m["role"] == "tool" for m in messages):
            seen["feedback"] = messages[-1]["content"]
        return LLMResult(content="没有权限，跳过")

    engine = make_engine(
        tmp_path,
        MockLLM.script(
            MockLLM.tool("bash", {"command": "rm -rf ./x"}).responses[0],
            then_answer,
        ),
        permissions=PermissionsEngine(tmp_path),  # 无 confirm
    )
    result = engine.run("删掉 x")
    assert result.terminated_reason == "completed"
    calls = [e for e in result.events if e["type"] == "tool_call"]
    assert calls[0]["success"] is False
    assert "权限拒绝" in seen["feedback"]


# ---------- 循环集成：第三方工具 ----------

class RemoteEchoInput(BaseModel):
    text: str = ""


class RemoteEchoTool(Tool):
    """假第三方工具：声明 is_external() → 权限默认不放行。"""

    name = "remote_echo"
    description = "假的 MCP 工具"
    input_model = RemoteEchoInput
    ran = False

    @classmethod
    def is_external(cls) -> bool:
        return True

    def execute(self, args, ctx):
        type(self).ran = True
        return ToolResult.ok("远端结果")


def _engine_with_external(tmp_path, permissions) -> tuple[QueryEngine, dict]:
    seen = {}

    def then_answer(messages, tools):
        if any(m["role"] == "tool" for m in messages):
            seen["feedback"] = messages[-1]["content"]
        return LLMResult(content="换个办法")

    registry = ToolRegistry.default(tmp_path)
    registry.register(RemoteEchoTool())
    engine = QueryEngine(
        MockLLM.script(MockLLM.tool("remote_echo", {"text": "hi"}).responses[0], then_answer),
        registry,
        workspace_root=tmp_path,
        permissions=permissions,
    )
    return engine, seen


def test_loop_unauthorized_external_tool_blocked_with_provenance(tmp_path):
    """未授权的第三方工具：不执行、回喂模型，且拒绝理由写明**怎么解除**。

    三件事一起验：① 工具真的没跑（`ran` 保持 False）；② 模型收到了拒绝；
    ③ 拒绝文案指出加 `allow` 到 mcp.json —— 没有出处和解除方式的拒绝，
    只会让 agent 反复重试同一个调用。
    """
    RemoteEchoTool.ran = False
    engine, seen = _engine_with_external(
        tmp_path, PermissionsEngine(tmp_path, external_tools=["remote_echo"])
    )
    result = engine.run("调远端")
    assert result.terminated_reason == "completed"
    assert RemoteEchoTool.ran is False
    calls = [e for e in result.events if e["type"] == "tool_call"]
    assert calls[0]["success"] is False
    assert "权限拒绝" in seen["feedback"]
    assert "mcp.json" in seen["feedback"]
    assert "remote_echo" in seen["feedback"]


def test_loop_authorized_external_tool_runs(tmp_path):
    """授权后照常执行 —— 加固不该把正常用法一并打死。"""
    RemoteEchoTool.ran = False
    engine, _ = _engine_with_external(
        tmp_path,
        PermissionsEngine(
            tmp_path, external_tools=["remote_echo"], rules={"external": {"allow": ["remote_echo"]}}
        ),
    )
    result = engine.run("调远端")
    assert RemoteEchoTool.ran is True
    calls = [e for e in result.events if e["type"] == "tool_call"]
    assert calls[0]["success"] is True



# ---------- M9-1：确认文案里的"将要改什么" ----------

def test_details_reach_the_confirmation_prompt(tmp_path):
    """预览文本要出现在**交给人看的那段字**里 —— 否则取了一遍预览等于白取。

    这是 M9-1 的接线点：`preview` 算出 diff、权限引擎把它拼进 `describe`。
    中间任何一处忘了传，失败模式都是**静默的**：功能"能跑"、测试"能过"、
    只是确认框里永远看不到 diff。
    """
    seen: dict[str, str] = {}

    def confirm(question: str) -> str:
        seen["question"] = question
        return "allow_once"

    engine = PermissionsEngine(
        tmp_path, confirm=confirm, rules={"tools": {"bash": "ask"}}
    )
    ctx = make_ctx(tmp_path)

    engine.check("bash", {"command": "ls"}, ctx, details="- a\n+ b")

    assert seen["question"] is not None
    assert "- a" in seen["question"] and "+ b" in seen["question"]


def test_details_do_not_change_the_decision(tmp_path):
    """预览**不参与判定**：同一条调用，给不给 details 结果必须一样。

    一旦 details 能影响判定，权限引擎就有了第二套"这次编辑合不合法"的判断，
    与工具的"唯一匹配"语义分叉 —— 正是 `Tool.preview` 文档里要防的那个真相源。
    """
    ctx = make_ctx(tmp_path)

    def decide(details):
        return PermissionsEngine(tmp_path).check(
            "write", {"path": "CLAUDE.md", "content": "x"}, ctx, details=details
        )

    assert decide(None) is decide("- 旧\n+ 新\n+ 又一行")


def test_description_order_is_what_then_change_then_why(tmp_path):
    """顺序是「问什么 → 改什么 → 为什么问」。

    天花板说明排在最后，是因为它解释的是"为什么又问一遍"，属于补充；
    而 diff 是**判断依据本身**，必须紧贴问句 —— 排在理由之后就会被折叠掉。
    """
    engine = PermissionsEngine(tmp_path)
    engine.note_taint("high")

    text = engine.describe(
        "write", {"path": "CLAUDE.md", "content": "x"}, details="[新建文件] CLAUDE.md"
    )

    assert text.index("是否允许") < text.index("[新建文件]") < text.index("污染标记")


# ---------- M9-2：web 工具接上污染天花板 ----------

def test_web_tools_are_egress_by_definition():
    """判据是「这个工具**做什么**」，不是「这次参数里有什么」。

    `web_fetch` 无论抓哪个 URL 都会把请求发出去，所以它恒属于「网络外发」。
    这条同时也是在钉：URL 里出现 `.env` 这类字样**不会**让它变成「读取凭据文件」
    —— 取远端 URL 跟读本地凭据文件是两件事，归类错了就会给出错误的解除指引。
    """
    assert _irreversible_kind("web_fetch", {"url": "https://example.com/"}) == "网络外发"
    assert _irreversible_kind("web_search", {"query": "python"}) == "网络外发"
    assert _irreversible_kind(
        "web_fetch", {"url": "https://example.com/?q=.env"}
    ) == "网络外发"


def test_web_tools_are_allowed_when_the_session_is_clean(tmp_path):
    """常态下联网工具是放行的 —— 收紧只在 high 污染时发生，别把它变成路障。"""
    engine = PermissionsEngine(tmp_path)
    ctx = make_ctx(tmp_path)

    assert engine.check("web_fetch", {"url": "https://example.com/"}, ctx) is Decision.ALLOW
    assert engine.check("web_search", {"query": "x"}, ctx) is Decision.ALLOW


def test_high_taint_tightens_web_tools(tmp_path):
    """这是 M9-2 的**主要目的**：让天花板里「网络外发」那一类第一次有真实对象。

    之前 `ToolRegistry.default()` 里没有任何联网工具，判据只能命中 bash 命令行里
    的 `curl|wget` —— 模型换个方式外发就绕过去了，那一类等于形同虚设。
    """
    engine = PermissionsEngine(tmp_path)
    engine.note_taint("high")
    ctx = make_ctx(tmp_path)

    assert engine.check("web_fetch", {"url": "https://example.com/"}, ctx) is Decision.ASK
    assert engine.check("web_search", {"query": "x"}, ctx) is Decision.ASK


def test_high_taint_still_leaves_local_reads_alone(tmp_path):
    """收紧范围必须压在最小 —— 多收紧一类，就多一类"本该能做的事突然做不了"。

    这条是反向闸门：别顺手把天花板扩到普通只读动作上。
    """
    engine = PermissionsEngine(tmp_path)
    engine.note_taint("high")
    ctx = make_ctx(tmp_path)

    assert engine.check("read", {"path": "notes.txt"}, ctx) is Decision.ALLOW
    assert engine.check("glob", {"pattern": "*.py"}, ctx) is Decision.ALLOW
    assert engine.check("bash", {"command": "git status"}, ctx) is Decision.ALLOW


def test_web_denial_hint_is_actionable(tmp_path):
    """拒绝而不说怎么解 = 路障。指引要带**类别**和**可执行的**解除动作。"""
    engine = PermissionsEngine(tmp_path)
    engine.note_taint("high")

    hint = engine.denial_hint("web_fetch", {"url": "https://example.com/"})

    assert hint is not None
    assert "网络外发" in hint
    assert "--clear-taint" in hint


def test_each_taint_category_has_a_trigger_inside_the_default_registry(tmp_path):
    """**通用不变量**：三类不可逆动作，每一类都要在 `default()` 里有触发对象。

    M9-2 之前这条是**不成立**的：「网络外发」的判据挂在那里，但默认工具集里
    一个联网工具都没有 —— 机制在、测试绿、文档也写了，可它打的是一个不存在的
    动作。这正是本项目最要防的那类缺陷（机制在、但没有任何东西会走到它）。

    所以这里不写死"web_fetch 属于网络外发"，而是从**注册表**这一侧检查：
    判据挂着的那个工具名，必须真的在模型的默认工具集里。以后谁再加一类动作、
    或者把某个工具移出 `default()`，这条会红。
    """
    registry = ToolRegistry.default(tmp_path)
    triggers = {
        "网络外发": ("web_fetch", {"url": "https://example.com/"}),
        "读取凭据文件": ("read", {"path": ".env"}),
        "写入记忆文件": ("write", {"path": "CLAUDE.md", "content": "x"}),
    }

    for category, (tool_name, args) in triggers.items():
        assert tool_name in registry.names(), (
            f"「{category}」的判据挂在 {tool_name} 上，但它不在 default() 里 —— "
            f"模型手上的默认工具集里没有能触发这一类的动作，天花板就是空的"
        )
        assert _irreversible_kind(tool_name, args) == category
