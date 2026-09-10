"""权限引擎（M2-1，吸收 MiniCode 决策粒度）。

决策粒度：allow_once / allow_turn / allow_always / deny_once / deny_always / ask。
三类请求：command（bash）、path（read/write/glob/grep）、edit（edit）。

判定优先级（安全优先）：
1. 路径越界沙箱 → deny（硬性）
2. 常驻记忆（allow_always / deny_always）→ 直接生效
3. 本回合记忆（allow_turn / deny_turn）→ 直接生效
4. 规则判定：危险命令默认 ask；deny 规则先于 allow（deny wins）
5. 默认 allow

ask 时调用 confirm 回调（CLI/Streamlit 弹确认），把用户的粒度选择
写回记忆（once 不记忆 / turn 记本回合 / always 常驻）。
"""
from __future__ import annotations

import fnmatch
import json
from enum import Enum
from pathlib import Path
from typing import Callable

from agent.tools.bash import BashTool
from agent.tools.base import ToolContext

# 用户确认返回的粒度 → 记忆类别
GRANULARITY: dict[str, str] = {
    "allow_once": "once",
    "allow_turn": "turn",
    "allow_always": "always",
    "deny_once": "once",
    "deny_turn": "turn",
    "deny_always": "always",
}


class Decision(str, Enum):
    ALLOW = "allow"
    DENY = "deny"
    ASK = "ask"


PATH_TOOLS = {"read", "write", "glob", "grep"}
EDIT_TOOLS = {"edit"}

_RULE_TO_DECISION = {
    "allow": Decision.ALLOW,
    "deny": Decision.DENY,
    "ask": Decision.ASK,
    "allow_once": Decision.ALLOW,
    "allow_turn": Decision.ALLOW,
    "allow_always": Decision.ALLOW,
    "deny_once": Decision.DENY,
    "deny_always": Decision.DENY,
}


def _rule_decision(value: str) -> Decision:
    """规则文件里的字符串（ask/deny/allow 或粒度串）→ 决策。未知按 ask 兜底。"""
    return _RULE_TO_DECISION.get(value, Decision.ASK)


class PermissionsEngine:
    """规则引擎 + 决策记忆。"""

    def __init__(
        self,
        workspace_root: Path,
        *,
        rules: dict | None = None,
        rules_path: Path | None = None,
        confirm: Callable[[str], str | None] | None = None,
    ) -> None:
        """confirm(question) -> 粒度字符串（如 "allow_once"/"deny_always"），None = 拒绝。"""
        self.workspace_root = Path(workspace_root).resolve()
        self.confirm = confirm
        self._rules: dict = {
            "tools": {},
            "commands": {"allow": [], "deny": []},
            "paths": {"allow": [], "deny": []},
            "edits": {"allow": [], "deny": []},
        }
        self._turn: dict[str, Decision] = {}    # 本回合记忆
        self._always: dict[str, Decision] = {}  # 常驻记忆
        if rules:
            self.load_rules(rules)
        if rules_path:
            self.load_rules_file(rules_path)

    # ---------- 规则加载 ----------

    def load_rules(self, data: dict) -> None:
        if "tools" in data:
            self._rules["tools"].update(data["tools"])
        for key in ("commands", "paths", "edits"):
            if key in data:
                for sub in ("allow", "deny"):
                    if sub in data[key]:
                        self._rules[key][sub] = list(data[key][sub])

    def load_rules_file(self, path: Path) -> None:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        self.load_rules(data)

    # ---------- 主入口 ----------

    def check(self, tool_name: str, arguments: dict, ctx: ToolContext) -> Decision:
        """判定一次工具调用：allow / deny / ask（ask 且有 confirm 时直接内联确认）。"""
        kind, target = self._classify(tool_name, arguments)

        # 1) 路径越界 → 硬 deny（read/write/edit 解析真实路径）
        if tool_name in ("read", "write", "edit") and self._resolve(
            arguments.get("path", ""), ctx
        ) is None:
            return Decision.DENY

        # 2) 常驻记忆
        if target in self._always:
            return self._always[target]

        # 3) 本回合记忆
        if target in self._turn:
            return self._turn[target]

        # 4) 规则判定
        decision = self._rule_check(tool_name, kind, arguments, ctx)

        # 5) ask → 回调确认
        if decision is Decision.ASK and self.confirm is not None:
            return self._confirm_and_record(tool_name, arguments, kind, target)
        return decision

    def ask(self, tool_name: str, arguments: dict, ctx: ToolContext) -> Decision:
        """强制人工确认（CLI/控制台在 ASK 且无 confirm 回调时调用）。"""
        kind, target = self._classify(tool_name, arguments)
        return self._confirm_and_record(tool_name, arguments, kind, target)

    def describe(self, tool_name: str, arguments: dict) -> str:
        """构造确认问题文本（供 confirm 回调/UI 展示）。"""
        kind, target = self._classify(tool_name, arguments)
        if kind == "command":
            what = f"执行命令: {target[:120]}"
        elif kind in ("path", "edit"):
            what = f"{tool_name} 文件: {target[:120]}"
        else:
            what = f"调用工具: {tool_name}"
        return (
            f"是否允许 {what}？"
            "(allow_once/allow_turn/allow_always/deny_once/deny_always)"
        )

    # ---------- 内部 ----------

    def _classify(self, tool_name: str, arguments: dict) -> tuple[str, str]:
        """返回 (请求类别, 记忆键 target)。"""
        if tool_name == "bash":
            return "command", arguments.get("command", "")
        if tool_name in EDIT_TOOLS:
            return "edit", arguments.get("path", "")
        if tool_name in PATH_TOOLS:
            raw = arguments.get("path") or arguments.get("pattern") or ""
            return "path", raw
        return "tool", tool_name

    def _resolve(self, raw: str, ctx: ToolContext) -> Path | None:
        """复用 files._resolve 的沙箱语义：越界返回 None。"""
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = ctx.cwd / path
        try:
            path = path.resolve()
            path.relative_to(self.workspace_root)
        except (ValueError, OSError):
            return None
        return path

    def _rule_check(
        self, tool_name: str, kind: str, arguments: dict, ctx: ToolContext
    ) -> Decision:
        # 0) 工具级规则（字符串 = 整工具决策；dict = 危险命令等细粒度）
        tool_rules = self._rules["tools"].get(tool_name)
        if isinstance(tool_rules, str):
            return _rule_decision(tool_rules)
        if isinstance(tool_rules, dict):
            if tool_name == "bash":
                command = arguments.get("command", "")
                if BashTool._is_dangerous(command) and "dangerous" in tool_rules:
                    return _rule_decision(tool_rules["dangerous"])
            if "deny" in tool_rules:
                return Decision.DENY
            if "allow" in tool_rules:
                return Decision.ALLOW

        # 1) 类别规则（deny wins）——显式规则优先于危险命令默认 ask
        if kind == "command":
            command = arguments.get("command", "")
            if self._matches_any([command], self._rules["commands"]["deny"]):
                return Decision.DENY
            if self._matches_any([command], self._rules["commands"]["allow"]):
                return Decision.ALLOW
        elif kind in ("path", "edit"):
            raw = arguments.get("path") or arguments.get("pattern") or ""
            resolved = self._resolve(raw, ctx)
            candidates = [raw.replace("\\", "/"), Path(raw).name]
            if resolved is not None:
                candidates.append(str(resolved).replace("\\", "/"))
                try:
                    candidates.append(
                        resolved.relative_to(self.workspace_root).as_posix()
                    )
                except ValueError:
                    pass
            if self._matches_any(candidates, self._rules["paths"]["deny"]):
                return Decision.DENY
            if self._matches_any(candidates, self._rules["paths"]["allow"]):
                return Decision.ALLOW
            if kind == "edit":
                if self._matches_any(candidates, self._rules["edits"]["deny"]):
                    return Decision.DENY
                if self._matches_any(candidates, self._rules["edits"]["allow"]):
                    return Decision.ALLOW

        # 2) 危险命令默认 ask（兜底）
        if tool_name == "bash" and BashTool._is_dangerous(
            arguments.get("command", "")
        ):
            return Decision.ASK
        return Decision.ALLOW

    @staticmethod
    def _matches_any(candidates: list[str], patterns: list[str]) -> bool:
        """fnmatch 匹配多个候选（绝对/相对/basename），满足其一即 True。"""
        for pat in patterns:
            for cand in candidates:
                if fnmatch.fnmatch(cand, pat):
                    return True
        return False

    def _confirm_and_record(
        self, tool_name: str, arguments: dict, kind: str, target: str
    ) -> Decision:
        choice = self.confirm(self.describe(tool_name, arguments)) if self.confirm else None
        decision, memory = self._apply_choice(choice)
        if memory == "turn":
            self._turn[target] = decision
        elif memory == "always":
            self._always[target] = decision
        return decision

    @staticmethod
    def _apply_choice(choice: str | None) -> tuple[Decision, str]:
        if not choice or choice not in GRANULARITY:
            return Decision.DENY, "once"  # 无确认 → 拒绝（安全默认）
        decision = Decision.ALLOW if choice.startswith("allow_") else Decision.DENY
        return decision, GRANULARITY[choice]
