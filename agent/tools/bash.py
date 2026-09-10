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
# 换个名字的私密变量（MY_PRIVATE_STUFF=xxx）照样漏；bash 也仍能
# `type ..\.env` 直接把仓库根的 .env 读出来（bash 的沙箱只管 cwd，
# 不管命令文本里的 `..`）。两条都列在 README「已知未修复的绕过路径」。
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

        # 3) 平台命令
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

        # 4) 拼输出 + 截断
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
