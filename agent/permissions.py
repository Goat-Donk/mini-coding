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
import re
from enum import Enum
from pathlib import Path
from typing import Callable, Iterable

from agent.security import TAINT_HIGH, TAINT_NONE
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


# ---------- 污染天花板覆盖的动作类别 ----------
# **只覆盖三类**，一类的都不多。判据是「一旦发生就收不回来」：
#   - 网络外发：数据出去了就出去了，事后撤销没有意义
#   - 读取凭据：key 进了模型上下文，就只能靠轮换补救
#   - 写入记忆文件：内容会被**后续每个会话**自动注入，是跨会话的持久化
# 反过来，`write` 一个普通源码文件、跑 `ls`、`git status` 都不在里面 ——
# 它们可撤销、可由人复核，收紧它们只会让工具变成路障（CLI 里没有确认交互）。
#
# 这三条都是**词法**判据，能被改名绕过（`curl` 换成 python 脚本、
# `.env` 换成 `config.txt`）。如实写在 README 的「已知未修复的绕过路径」里。
EGRESS_COMMAND = re.compile(
    r"(^|[\s;&|(])(curl|wget|Invoke-WebRequest|iwr|Invoke-RestMethod|irm"
    r"|nc|netcat|ncat|scp|sftp|rsync|telnet|ftp|ssh)\b"
    r"|requests\.(get|post|put|patch)|urllib\.request|httpx\.|socket\.socket",
    re.IGNORECASE,
)
CREDENTIAL_PATH = re.compile(
    r"(^|[\\/])(\.env(\.[\w.-]+)?|\.npmrc|\.netrc|\.git-credentials"
    r"|id_rsa|id_dsa|id_ed25519|credentials(\.json)?|\.aws|\.ssh|\.kube"
    r"|\.docker[\\/]config\.json|\.pypirc|\.pgpass)$",
    re.IGNORECASE,
)
#: bash **命令串**用的凭据判据：只要命令里**提到**凭据文件即可，不锚定路径末尾。
#:
#: 为什么不复用上面的锚定版：那个是给 read/write/edit 用的 —— 那些工具的
#: `path` 参数本身就是一条路径，锚定末尾正好表达「操作的就是这个文件」。
#: 但 bash 的 `command` 是一整条命令行，锚定末尾会漏掉**任何把凭据文件当输入
#: 再写到别处**的写法（`copy .env x.txt` / `cp .env /tmp/x` / `tar -cf - .env`），
#: 而这类恰恰是最典型的带离手段。
#:
#: 代价说清楚：`grep -rn "\.env" README.md` 这种只是**提到**这个词的命令，
#: 在 high 会话里也会被收紧。这是**故意的**取舍 —— 一句话能讲清的规则
#: （「high 会话里提到凭据文件的 bash 命令都要人工确认」）比一张要维护的
#: 动词表可靠，而解除只要人的 `--clear-taint` 一句话。
CREDENTIAL_MENTION = re.compile(
    r"\.env\b|\.npmrc|\.netrc|\.git-credentials|id_rsa|id_dsa|id_ed25519"
    r"|credentials(\.json)?\b|\.aws\b|\.ssh\b|\.kube\b|\.pypirc|\.pgpass",
    re.IGNORECASE,
)
MEMORY_PATH = re.compile(
    r"(^|[\\/])(CLAUDE\.md|CODEAGENT\.md|MINI\.md|AGENTS\.md|learned(\.[\w-]+)*\.md)$"
    r"|(^|[\\/])\.codeagent[\\/]rules",
    re.IGNORECASE,
)


def _irreversible_kind(tool_name: str, arguments: dict) -> str | None:
    """这次调用属于哪一类「不可逆动作」？不属于则 None。

    纯函数（不读引擎状态），所以 `check()` 与 `denial_hint()` 可以各自独立
    算一遍而不担心并发下互相串味。
    """
    raw = str(
        arguments.get("command")
        or arguments.get("path")
        or arguments.get("pattern")
        or ""
    )
    if not raw:
        return None
    if tool_name == "bash":
        if EGRESS_COMMAND.search(raw):
            return "网络外发"
        if MEMORY_PATH.search(raw):
            return "写入记忆文件"
        # bash 碰凭据：**只看命令里有没有提到凭据文件，不枚举读动词、不锚定路径末尾**。
        #
        # 两处都是真实 LLM 端到端验证时发现后改的：
        #
        # ① 原先要求「读动词 + 凭据路径」同现，动词表是
        #    `cat|type|head|tail|less|more|Get-Content|gc`。模型读 .env 用的却是
        #    `findstr ... .env`（Windows 上 grep 的自然替代）—— 不在表里，天花板
        #    **没生效**（轨迹里没有 gate_block，命令正常执行，变量名进了上下文）。
        #    枚举读动词是打地鼠：findstr / Select-String / grep / awk / sed / od /
        #    python -c … 无穷无尽，漏一个就等于这类动作完全没有天花板。
        # ② 改成锚定路径末尾也还不够：`copy .env x.txt` 的末尾是 `x.txt`，不命中 ——
        #    而「把凭据文件当输入写到别处」正是最典型的带离手段。
        #
        # 所以判据收敛成一句话：**high 会话里，提到凭据文件的 bash 命令都要人工确认**。
        # 代价是 `grep -rn "\.env" README.md` 这种只是提及的也会被收紧 —— 接受，
        # 因为它只在 high 会话生效，且人的 `--clear-taint` 一句话就能解除；
        # 相比之下「换个命令就把 key 读走」不可接受。
        if CREDENTIAL_MENTION.search(raw):
            return "读取凭据文件"
        return None
    if tool_name == "read" and CREDENTIAL_PATH.search(raw):
        return "读取凭据文件"
    if tool_name in ("write", "edit") and MEMORY_PATH.search(raw):
        return "写入记忆文件"
    return None


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
        external_tools: Iterable[str] = (),
    ) -> None:
        """confirm(question) -> 粒度字符串（如 "allow_once"/"deny_always"），None = 拒绝。

        external_tools：第三方工具名（MCP 等）。它们**默认不放行**，必须由
        `external.allow` 规则显式授权 —— 这些工具不受 workspace 沙箱约束，
        「没配规则就放行」对它们是错的默认值。
        """
        self.workspace_root = Path(workspace_root).resolve()
        self.confirm = confirm
        self._external: set[str] = set(external_tools)
        self._rules: dict = {
            "tools": {},
            "commands": {"allow": [], "deny": []},
            "paths": {"allow": [], "deny": []},
            "edits": {"allow": [], "deny": []},
            "external": {"allow": []},
        }
        self._turn: dict[str, Decision] = {}    # 本回合记忆
        self._always: dict[str, Decision] = {}  # 常驻记忆
        # 会话级污染级别（由 loop 从 AgentState.taint 同步过来）。
        # 引擎自己不改它 —— 标记的权威在状态里，只有人的动作能让它降下来。
        self._taint: str = TAINT_NONE
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
        if "external" in data and "allow" in data["external"]:
            self._rules["external"]["allow"] = list(data["external"]["allow"])

    def load_rules_file(self, path: Path) -> None:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        self.load_rules(data)

    def allow_external(self, names: Iterable[str]) -> None:
        """把第三方工具加进免确认名单（来自 mcp.json 的 `allow`）。

        与规则文件里的 `external.allow` 是**并集**，不互相覆盖 —— 否则
        「先 load_rules_file 还是先 load_mcp」会决定谁生效，成了隐性顺序依赖。
        """
        self._rules["external"]["allow"] = list(self._rules["external"]["allow"]) + list(names)

    # ---------- 主入口 ----------

    def check(self, tool_name: str, arguments: dict, ctx: ToolContext) -> Decision:
        """判定一次工具调用：allow / deny / ask（ask 且有 confirm 时直接内联确认）。"""
        return self._decide(tool_name, arguments, ctx)

    def _decide(self, tool_name: str, arguments: dict, ctx: ToolContext) -> Decision:
        """决策链本体：硬 deny → 记忆 → 规则 → **后置天花板** → 人工确认。

        天花板摆在哪里是这里最要紧的一件事，两个方向都不能错：

        - **必须在记忆之后**。写成规则链里的一条（`_rule_check` 的一个分支）是
          无效的：第 2、3 步的 `_always`/`_turn` 会在它之前 return，于是用户
          只要开过一次 `allow_always`，任何基于规则的收紧就永久失效 —— 而
          「记得越久越省事」正是用户去开它的原因，两个方向正好相反。
        - **必须在人工确认之前**。confirm 回调是一个真实的人当场作出的决定，
          自动机制不该反过来推翻它（否则人点了「允许」系统仍按拒绝处理，
          确认框就成了摆设）。
        """
        kind, target = self._classify(tool_name, arguments)

        # 1) 路径越界 → 硬 deny（read/write/edit 解析真实路径）
        if tool_name in ("read", "write", "edit") and self._resolve(
            arguments.get("path", ""), ctx
        ) is None:
            return Decision.DENY

        # 2) 常驻记忆 / 3) 本回合记忆 / 4) 规则判定
        if target in self._always:
            decision = self._always[target]
        elif target in self._turn:
            decision = self._turn[target]
        else:
            decision = self._rule_check(tool_name, kind, arguments, ctx)

        # 5) 后置天花板：只降不升
        decision = self._apply_taint_ceiling(tool_name, arguments, decision)

        # 6) ask → 回调确认（人的决定在最后）
        if decision is Decision.ASK and self.confirm is not None:
            return self._confirm_and_record(tool_name, arguments, kind, target)
        return decision

    def ask(self, tool_name: str, arguments: dict, ctx: ToolContext) -> Decision:
        """强制人工确认（CLI/控制台在 ASK 且无 confirm 回调时调用）。"""
        kind, target = self._classify(tool_name, arguments)
        return self._confirm_and_record(tool_name, arguments, kind, target)

    def describe(self, tool_name: str, arguments: dict) -> str:
        """构造确认问题文本（供 confirm 回调/UI 展示）。

        污染天花板触发的确认会**额外写明原因**。这很重要：控制台里用户看到的
        是一个弹窗，如果它和普通弹窗长得一样，用户只会觉得"怎么又问一遍"，
        然后条件反射地点允许 —— 一个不说理由的确认框，训练出的是不看理由的人。
        """
        kind, target = self._classify(tool_name, arguments)
        if kind == "command":
            what = f"执行命令: {target[:120]}"
        elif kind in ("path", "edit"):
            what = f"{tool_name} 文件: {target[:120]}"
        elif kind == "external":
            what = (
                f"调用第三方工具: {tool_name}"
                "（不受 workspace 沙箱约束，需显式授权）"
            )
        else:
            what = f"调用工具: {tool_name}"
        question = (
            f"是否允许 {what}？"
            "(allow_once/allow_turn/allow_always/deny_once/deny_always)"
        )
        if self._taint == TAINT_HIGH:
            label = _irreversible_kind(tool_name, arguments)
            if label is not None:
                question += (
                    f"\n[污染标记 high] 本会话的工具输出里命中过可疑文本模式，"
                    f"因此「{label}」这类不可逆动作会重新征询一次 —— "
                    f"即使之前选过 allow_always。"
                )
        return question

    def denial_hint(self, tool_name: str, arguments: dict | None = None) -> str | None:
        """被拒时给用户的**解除指引**（带出处，不是一句"没权限"）。

        拒绝而不说怎么解，等于把一个安全机制变成路障：用户既不知道是谁拦的，
        也不知道该改哪个文件。所以每类拒绝都带出处，并且**写出解除它的具体动作**。

        这里重算「是哪一类不可逆动作被收紧」，而不是读 `check()` 留下的某个字段：
        `_gate_and_run` 的只读批次会用线程池并发跑，任何"上一次判定"式的共享状态
        都可能把 A 调用的理由安到 B 调用头上。重算是纯函数，没有这个窗口。
        """
        if tool_name in self._external:
            return (
                f"{tool_name} 来自第三方（MCP）server，不受 workspace 沙箱约束，"
                f'需在 mcp.json 里显式授权：给对应 server 加 "allow": ["{tool_name}"]'
            )
        if self._taint == TAINT_HIGH:
            label = _irreversible_kind(tool_name, arguments or {})
            if label is not None:
                return (
                    f"本会话的污染标记为 high（工具输出里命中过可疑文本模式，"
                    f"见轨迹里同步骤的 security_finding 事件），因此「{label}」"
                    f"这类动作被收紧。确认那些输出没有在驱使你做这件事之后，"
                    f"用 --clear-taint 复位标记再重试。"
                )
        return None

    # ---------- 污染标记（会话级，粗粒度） ----------

    def note_taint(self, level: str) -> None:
        """同步本会话的污染级别。

        **这是一个镜像，不是权威**：权威在 `AgentState.taint`（只升不降，
        只有人的动作能复位）。引擎照着状态设值，而不是自己累加 —— 否则
        resume 出来的会话、`--clear-taint` 之后的会话，两边就会不一致。
        """
        self._taint = level

    @property
    def taint(self) -> str:
        return self._taint

    def _apply_taint_ceiling(
        self, tool_name: str, arguments: dict, decision: Decision
    ) -> Decision:
        """后置天花板：high 污染下，把三类**不可逆**动作从 ALLOW 降到 ASK。

        只降不升（`DENY` 不动、`ASK` 不动），只覆盖三类动作，见
        `_irreversible_kind` 的说明 —— 收紧范围刻意压到最小，理由见
        `agent/security.py` 里关于误报成本不对称的那段：多收紧一类动作，
        就是多一类"本该能做的事突然做不了、而 CLI 里没有确认交互可用来纠正"。
        """
        if decision is not Decision.ALLOW or self._taint != TAINT_HIGH:
            return decision
        if _irreversible_kind(tool_name, arguments) is None:
            return decision
        return Decision.ASK

    # ---------- 内部 ----------

    def _classify(self, tool_name: str, arguments: dict) -> tuple[str, str]:
        """返回 (请求类别, 记忆键 target)。

        `_external` 放在最前：名字同时像内置工具时按**外部**处理。两者不该
        重叠（MCP 工具与内置重名时 load_mcp_servers 会加 `<别名>__` 前缀），
        但万一重叠，取更严的那条 —— 外部工具的兜底是 ASK，内置工具是 ALLOW。
        """
        if tool_name in self._external:
            return "external", tool_name
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
        elif kind == "external":
            # 第三方工具（MCP 等）：不受 workspace 沙箱约束，「没配规则就放行」是错的
            # 默认值。必须显式授权：要么写进 external.allow，要么用户在确认里选 allow_*。
            if self._matches_any([tool_name], self._rules["external"]["allow"]):
                return Decision.ALLOW
            return Decision.ASK

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
