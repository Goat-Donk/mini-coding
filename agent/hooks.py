"""hooks 引擎（M2-2）：PreToolUse / PostToolUse 分发 + block-at-submit 示例。

对齐 CC 的两种钩子：
- PreToolUse：阻断型（Block-at-Submit）——返回 HookBlock 即拦截该工具调用，
  错误信息回喂模型让它改道（先跑测试再提交）。
- PostToolUse：观察/提示型——工具执行后分发，返回提示文本（非阻断）。

示例：require_tests_before_commit —— 包住 Bash(git commit)，检查
`data/tests_pass.marker`，测试未通过标记则阻断提交。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from agent.tools.base import ToolResult

PRE_EVENT = "PreToolUse"
POST_EVENT = "PostToolUse"


@dataclass
class HookBlock:
    """阻断结果：reason 给用户/模型看，hint 是可行动替代方案。"""

    reason: str
    hint: str | None = None


@dataclass
class HookContext:
    """单个 hook 收到的执行上下文。"""

    event_name: str          # "PreToolUse" | "PostToolUse"
    tool_name: str
    arguments: dict
    result: ToolResult | None = None   # PostToolUse 才有
    state: object | None = None        # AgentState（可选）
    workspace_root: Path | None = None


HookFn = Callable[[HookContext], HookBlock | None | str]


class HookEngine:
    """分发引擎：add 注册，run_pre 返回第一个阻断，run_post 收集提示。"""

    def __init__(
        self,
        pre_hooks: list[HookFn] = (),
        post_hooks: list[HookFn] = (),
        *,
        workspace_root: Path | None = None,
    ) -> None:
        self.workspace_root = Path(workspace_root).resolve() if workspace_root else None
        self._pre: list[HookFn] = []
        self._post: list[HookFn] = []
        for hook in pre_hooks:
            self.add_pre(hook)
        for hook in post_hooks:
            self.add_post(hook)

    def add_pre(self, hook: HookFn) -> None:
        self._pre.append(hook)

    def add_post(self, hook: HookFn) -> None:
        self._post.append(hook)

    def run_pre(
        self, tool_name: str, arguments: dict, state: object | None = None
    ) -> HookBlock | None:
        """依次运行 PreToolUse hooks，返回第一个阻断（None = 放行）。"""
        for hook in self._pre:
            outcome = self._invoke(hook, PRE_EVENT, tool_name, arguments, None, state)
            if outcome is not None:
                if isinstance(outcome, HookBlock):
                    return outcome
                return HookBlock(reason=str(outcome))
        return None

    def run_post(
        self, tool_name: str, arguments: dict, result: ToolResult, state: object | None = None
    ) -> list[str]:
        """运行 PostToolUse hooks，返回提示文本列表（非阻断）。"""
        hints: list[str] = []
        for hook in self._post:
            outcome = self._invoke(hook, POST_EVENT, tool_name, arguments, result, state)
            if outcome is not None and not isinstance(outcome, HookBlock):
                hints.append(str(outcome))
        return hints

    def _invoke(
        self,
        hook: HookFn,
        event: str,
        tool_name: str,
        arguments: dict,
        result: ToolResult | None,
        state: object | None,
    ) -> HookBlock | str | None:
        ctx = HookContext(
            event_name=event,
            tool_name=tool_name,
            arguments=dict(arguments),
            result=result,
            state=state,
            workspace_root=self.workspace_root,
        )
        outcome = hook(ctx)
        if isinstance(outcome, HookBlock) or outcome is None or isinstance(outcome, str):
            return outcome
        raise TypeError(f"hook 返回值必须是 HookBlock/str/None，得到 {type(outcome)}")


# ---------- 内置示例：block-at-submit ----------

def require_tests_before_commit(workspace_root: Path) -> HookFn:
    """PreToolUse 包 Bash(git commit)：tests_pass.marker 不存在 → 阻断提交。

    marker 由 mark_tests_pass() 在测试通过后写入（或由 CI/钩子工具调用）。
    """
    marker = Path(workspace_root).resolve() / "data" / "tests_pass.marker"

    def hook(ctx: HookContext) -> HookBlock | None:
        if ctx.tool_name != "bash":
            return None
        command = ctx.arguments.get("command", "")
        if re.search(r"git\s+commit", command, re.IGNORECASE):
            if not marker.exists():
                return HookBlock(
                    reason="git commit 前必须通过测试（未检测到 tests_pass.marker）",
                    hint="先运行 python -m pytest tests/，通过后写 data/tests_pass.marker，再提交",
                )
        return None

    return hook


def mark_tests_pass(workspace_root: Path) -> Path:
    """测试跑通后写 marker（block-at-submit 的解锁钥匙）。返回 marker 路径。"""
    marker = Path(workspace_root).resolve() / "data" / "tests_pass.marker"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("tests pass\n", encoding="utf-8")
    return marker
