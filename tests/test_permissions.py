"""M2-1 tests: agent/permissions.py（规则引擎 + 决策粒度 + 黑名单 + 沙箱）。"""
import json
from pathlib import Path

from agent.llm import LLMResult, MockLLM
from agent.loop import QueryEngine
from agent.permissions import Decision, PermissionsEngine
from agent.tools.base import ToolContext, ToolRegistry


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
    """allow_turn → 本回合内同命令放行，turn 结束（新实例）后失效。"""
    asked = []
    engine = PermissionsEngine(tmp_path, confirm=lambda q: asked.append(q) or "allow_turn")
    ctx = make_ctx(tmp_path)
    assert engine.check("bash", {"command": "rm -rf x"}, ctx) is Decision.ALLOW
    assert engine.check("bash", {"command": "rm -rf x"}, ctx) is Decision.ALLOW
    assert len(asked) == 1
    # 新回合（同 workspace 新实例，无 confirm）→ 又需确认（turn 记忆不跨实例）
    engine2 = PermissionsEngine(tmp_path)
    assert engine2.check("bash", {"command": "rm -rf x"}, ctx) is Decision.ASK


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
