"""文件工具：read / write / edit / glob / grep。

安全：所有路径先 _resolve 沙箱校验，越界立即 fail（含 write/edit 的父目录）。
编辑：CC 的"唯一匹配"设计——old_string 必须精确唯一，失败信息可行动，让模型自修复。
"""
from __future__ import annotations

import difflib
import fnmatch
import os
import re
from pathlib import Path

from pydantic import BaseModel, ValidationError

from agent.tools.base import Tool, ToolContext, ToolResult
from agent.workspace import FileChange

MAX_CHARS = 500_000  # 兜底安全上限；超大输出走 tool_result 落盘（M3-2）
#: 给人看的 diff 预览上限（M9-1）。比 `MAX_CHARS` 小两个数量级是**故意的**：
#: 它出现在人工确认框里，一段几万字符的 diff 会把人逼成"闭眼点允许"——
#: 而一个训练用户不看内容的确认框，等于没有确认框。
DIFF_PREVIEW_CHARS = 4_000
TRUNCATED_MESSAGE = (
    "\n... [输出被截断，共 {total} 字符，仅显示前 {limit} 字符] ..."
)
GLOB_TRUNCATED_MESSAGE = (
    "\n... [文件过多，仅显示前 {limit} 个。请用更精确的 glob pattern 或 read 深入探索] ..."
)


def _unified_diff(
    label: str, old: str, new: str, limit: int = DIFF_PREVIEW_CHARS
) -> str:
    """生成 `a/label` → `b/label` 的 unified diff（截断到 limit）。

    **改前预览与"编辑完成"回执共用这一个函数。** 两处各写一份的后果不是
    代码重复，而是口径分叉：给人看的 diff 和给模型看的 diff 迟早不一致，
    而这类不一致没有任何测试会自然覆盖到。
    """
    diff_text = "\n".join(
        difflib.unified_diff(
            old.splitlines(), new.splitlines(),
            fromfile=f"a/{label}", tofile=f"b/{label}", lineterm="",
        )
    )
    if len(diff_text) > limit:
        diff_text = diff_text[:limit] + TRUNCATED_MESSAGE.format(
            total=len(diff_text), limit=limit
        )
    return diff_text



def _resolve(ctx: ToolContext, raw: str, *, default_root: bool = False) -> Path | ToolResult:
    """解析路径并校验沙箱内。相对路径锚定 ctx.cwd。"""
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = ctx.cwd / path
    try:
        path = path.resolve()
        path.relative_to(ctx.workspace_root)
    except (ValueError, OSError):
        return ToolResult.fail(f"路径越界沙箱: {raw}")
    return path


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _snapshot_before(resolved: Path) -> FileChange:
    """写盘**之前**读出原样字节，作为 M9-8 快照的 `base`（"我们碰它之前长什么样"）。

    必须在 `write_text` 之前调用 —— 登记发生在工具返回之后，那时旧内容已经没了。

    为什么是**字节**而不是复用 `_read_text` 的文本：那是 `errors="replace"` 解出来
    的，重新编码**未必等于盘上的字节**（BOM、非 UTF-8、替换字符）。拿它当 base，
    还原出来的文件会与原始字节不一致，而错误要到回滚时校验哈希才暴露 —— 太晚。

    读不到（权限 / 被占用）时**不是**返回 `before=None`：那个值的含义是"改动前
    不存在"，回滚时会把文件**删掉**。所以显式标 `base_unknown=True`，让快照拒绝
    把这个路径纳入管辖（宁可回滚不到它，也不能拿它去赌）。
    """
    try:
        if not resolved.is_file():
            return FileChange(path=resolved, before=None)
        return FileChange(path=resolved, before=resolved.read_bytes())
    except OSError:
        return FileChange(path=resolved, before=None, base_unknown=True)


def _truncate(text: str, limit: int = MAX_CHARS) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    return text[:limit] + TRUNCATED_MESSAGE.format(total=len(text), limit=limit), True


# ---------- read ----------

class ReadInput(BaseModel):
    path: str
    offset: int = 0            # 起始行号（0 起）
    limit: int | None = None   # 最多读多少行；None = 全部（仍受 MAX_CHARS 截断）


class ReadTool(Tool):
    name = "read"
    description = "读取文件内容（带行号）。大文件自动截断，offset/limit 分段读取。"
    input_model = ReadInput

    @classmethod
    def is_read_only(cls) -> bool:
        return True

    def execute(self, args: ReadInput, ctx: ToolContext) -> ToolResult:
        resolved = _resolve(ctx, args.path)
        if isinstance(resolved, ToolResult):
            return resolved
        if not resolved.exists():
            return ToolResult.fail(f"文件不存在: {args.path}")
        if not resolved.is_file():
            return ToolResult.fail(f"不是文件: {args.path}")

        lines = _read_text(resolved).splitlines()
        total = len(lines)
        start = max(0, args.offset)
        end = total if args.limit is None else min(total, start + args.limit)
        selected = lines[start:end]

        body = "\n".join(
            f"{i + 1}: {line}" for i, line in enumerate(selected, start=start)
        )
        output, truncated = _truncate(body)
        header = f"文件 {args.path} 共 {total} 行，显示第 {start + 1}-{end} 行"
        if truncated:
            header += "（已截断）"

        return ToolResult.ok(
            header + "\n" + output,
            data={
                "path": str(resolved),
                "total_lines": total,
                "start_line": start + 1,
                "end_line": end,
                "chars": len(body),
                "truncated": truncated,
            },
        )


# ---------- write ----------

class WriteInput(BaseModel):
    path: str
    content: str


class WriteTool(Tool):
    name = "write"
    description = (
        "覆盖写入文件（自动创建父目录）。用于新建文件或整文件重写；小改动优先用 edit。"
    )
    input_model = WriteInput

    def execute(self, args: WriteInput, ctx: ToolContext) -> ToolResult:
        resolved = _resolve(ctx, args.path)
        if isinstance(resolved, ToolResult):
            return resolved
        change = _snapshot_before(resolved)      # ★ 必须在 write_text 之前
        try:
            resolved.parent.mkdir(parents=True, exist_ok=True)
            resolved.write_text(args.content, encoding="utf-8")
        except OSError as exc:
            # 写盘失败 → 不报告改动：盘上没变，登记它会让快照以为这个文件被改过。
            return ToolResult.fail(f"写入失败: {exc}")
        return ToolResult.ok(
            f"已写入 {len(args.content)} 字符到 {args.path}（覆盖）",
            data={"path": str(resolved), "chars": len(args.content)},
            file_changes=(change,),
        )

    def preview(self, arguments: dict, ctx: ToolContext) -> str | None:
        """覆盖写入前把 diff 交给人看（新文件则说明是新建）。

        这一类最需要预览：**整份内容被替换**，而工具结果里只有一句
        "已写入 N 字符到 X（覆盖）"—— 人根本看不出丢了什么。`edit` 至少
        还能从 old_string 猜到改动范围，`write` 连这个都没有。

        不抛异常、不写盘：拿不到就返回 None，确认框退回原来的样子。
        """
        try:
            args = WriteInput(**arguments)
        except ValidationError:
            return None

        resolved = _resolve(ctx, args.path)
        if isinstance(resolved, ToolResult):
            return None  # 越界由权限链的硬 deny 处理，这里不管
        if not resolved.exists():
            return f"[新建文件] {args.path}（{len(args.content)} 字符）"
        if not resolved.is_file():
            return None
        old = _read_text(resolved)
        if old == args.content:
            return f"[内容无变化] {args.path}"
        return _unified_diff(args.path, old, args.content)



# ---------- edit（CC 唯一匹配 + diff） ----------

class EditInput(BaseModel):
    path: str
    old_string: str
    new_string: str
    replace_all: bool = False


class EditTool(Tool):
    name = "edit"
    description = (
        "在文件中做精确字符串替换。old_string 必须唯一匹配（含完整上下文与缩进）；"
        "不匹配或匹配多次会报错并提示如何修正。"
    )
    input_model = EditInput

    def _plan(
        self, args: EditInput, resolved: Path
    ) -> tuple[str, str | None, str | None]:
        """算出编辑后的内容。返回 `(原文, 新内容, 失败原因)`（后两者恰有一个非 None）。

        **`preview` 与 `execute` 共用这一份匹配语义** —— 这是本项最要紧的一处约束：
        "唯一匹配"如果各判一遍，就会出现"预览说能改、执行说匹配不唯一"，
        而那时人已经照着预览点过允许了。匹配、计数、替换只写在这里。
        """
        content = _read_text(resolved)
        count = content.count(args.old_string)
        if count == 0:
            lines = content.count("\n") + 1
            return content, None, (
                f"old_string 未在文件中找到。当前文件共 {lines} 行。\n"
                "请先用 read 查看实际内容，注意缩进/换行/转义，再重试。\n"
                f"查找内容: {args.old_string[:200]!r}"
            )
        if count > 1 and not args.replace_all:
            return content, None, (
                f"old_string 在文件中出现 {count} 次，不唯一。\n"
                "请包含更多上下文行使其唯一，或传 replace_all=true。\n"
                f"查找内容: {args.old_string[:200]!r}"
            )
        if args.replace_all:
            return content, content.replace(args.old_string, args.new_string), None
        return content, content.replace(args.old_string, args.new_string, 1), None

    def preview(self, arguments: dict, ctx: ToolContext) -> str | None:
        """改**之前**把 diff 交给人看 —— 权限确认要看的正是这个。

        匹配失败时**返回失败原因而不是 None**，这是刻意的：人看到的不该是
        一片空白（"它没说要改什么"），而是"这次编辑根本改不动，因为
        old_string 匹配到 3 处"。预览的全部意义就是让这个判断发生在写盘之前。
        """
        try:
            args = EditInput(**arguments)
        except ValidationError:
            return None
        resolved = _resolve(ctx, args.path)
        if isinstance(resolved, ToolResult) or not resolved.exists():
            return None  # 越界/不存在：execute 与权限链各自有更准的话说
        old, new, error = self._plan(args, resolved)
        if error is not None:
            return f"[无法预览改动] {error}"
        return _unified_diff(args.path, old, new or "")

    def execute(self, args: EditInput, ctx: ToolContext) -> ToolResult:
        resolved = _resolve(ctx, args.path)
        if isinstance(resolved, ToolResult):
            return resolved
        if not resolved.exists():
            return ToolResult.fail(f"文件不存在: {args.path}（请先 write 或 read 确认）")

        old_content, new_content, error = self._plan(args, resolved)
        if error is not None:
            return ToolResult.fail(error)

        change = _snapshot_before(resolved)      # ★ 必须在 write_text 之前
        try:
            resolved.write_text(new_content, encoding="utf-8")
        except OSError as exc:
            return ToolResult.fail(f"写入失败: {exc}")

        return ToolResult.ok(
            "编辑完成，diff：\n"
            + _unified_diff(args.path, old_content, new_content, limit=MAX_CHARS),
            data={
                "path": str(resolved),
                "replacements": (
                    old_content.count(args.old_string) if args.replace_all else 1
                ),
            },
            file_changes=(change,),
        )


# ---------- glob ----------

class GlobInput(BaseModel):
    pattern: str              # glob 模式（** 递归）
    path: str | None = None   # 起始目录（默认 cwd）


class GlobTool(Tool):
    name = "glob"
    description = "按 glob 模式列出文件（** 递归）。返回相对路径。"
    input_model = GlobInput
    MAX_FILES = 100

    @classmethod
    def is_read_only(cls) -> bool:
        return True

    def execute(self, args: GlobInput, ctx: ToolContext) -> ToolResult:
        base_raw = args.path or str(ctx.cwd)
        resolved = _resolve(ctx, base_raw)
        if isinstance(resolved, ToolResult):
            return resolved
        if not resolved.is_dir():
            return ToolResult.fail(f"glob 起始目录不存在: {base_raw}")

        matches: list[str] = []
        truncated = False
        for path in sorted(resolved.rglob(args.pattern)):
            if not path.is_file():
                continue
            if len(matches) >= self.MAX_FILES:
                truncated = True
                break
            try:
                rel = path.relative_to(resolved).as_posix()
            except ValueError:
                continue
            matches.append(rel)

        output = "\n".join(matches)
        if truncated:
            output += GLOB_TRUNCATED_MESSAGE.format(limit=self.MAX_FILES)
        if not matches:
            output = f"没有匹配 {args.pattern!r} 的文件（起始目录: {base_raw}）"

        return ToolResult.ok(
            output,
            data={"matches": matches, "truncated": truncated},
        )


# ---------- grep ----------

class GrepInput(BaseModel):
    pattern: str                 # Python 正则
    path: str | None = None      # 搜索起始目录（默认 cwd）
    include: str | None = None   # 文件名 glob 过滤（如 "*.py"）
    max_matches: int = 100


class GrepTool(Tool):
    name = "grep"
    description = "在文件中按正则搜索，返回 file:line 命中行。"
    input_model = GrepInput
    SKIP_DIRS = {
        ".git", "node_modules", "__pycache__", ".venv", "venv",
        "dist", "build", "data", ".idea", ".pytest_cache",
    }
    MAX_FILE_SIZE = 1_000_000

    @classmethod
    def is_read_only(cls) -> bool:
        return True

    def execute(self, args: GrepInput, ctx: ToolContext) -> ToolResult:
        base_raw = args.path or str(ctx.cwd)
        resolved = _resolve(ctx, base_raw)
        if isinstance(resolved, ToolResult):
            return resolved
        if not resolved.is_dir():
            return ToolResult.fail(f"grep 起始目录不存在: {base_raw}")

        try:
            pattern = re.compile(args.pattern)
        except re.error as exc:
            return ToolResult.fail(f"正则无效: {exc}")

        max_matches = max(1, args.max_matches)
        hits: list[str] = []
        truncated = False

        for dirpath, dirnames, filenames in os.walk(resolved):
            dirnames[:] = [
                d for d in dirnames
                if d not in self.SKIP_DIRS and not d.startswith(".")
            ]
            for filename in sorted(filenames):
                if args.include and not fnmatch.fnmatch(filename, args.include):
                    continue
                full = Path(dirpath) / filename
                try:
                    if full.stat().st_size > self.MAX_FILE_SIZE:
                        continue
                except OSError:
                    continue
                try:
                    lines = full.read_text(encoding="utf-8", errors="replace").splitlines()
                except OSError:
                    continue
                for lineno, line in enumerate(lines, start=1):
                    if pattern.search(line):
                        rel = full.relative_to(resolved).as_posix()
                        hits.append(f"{rel}:{lineno}: {line.rstrip()}")
                        if len(hits) >= max_matches:
                            truncated = True
                            break
                if truncated:
                    break
            if truncated:
                break

        output = "\n".join(hits)
        if truncated:
            output += f"\n... [命中过多，仅显示前 {max_matches} 条，请加限定条件] ..."
        if not hits:
            output = f"没有匹配 {args.pattern!r} 的行（起始目录: {base_raw}）"

        return ToolResult.ok(
            output,
            data={"matches": hits, "truncated": truncated},
        )
