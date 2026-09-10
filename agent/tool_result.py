"""超大工具结果落盘（M3-2，MiniCode tool-result-storage 移植）。

问题：M1 的 _truncate 截断即丢弃（如 grep 上百条、大文件 read），模型拿不到
完整信息。方案：超大工具输出落盘到 `data/tool-results/{session}/{id}.txt`，
上下文里替换为 `<persisted-output>` 标签 + 路径 + 前 2000 字符预览。

规则：
- 单条 >50K 字符 → 落盘；
- **批内预算 200K/轮**：即使单条没超，批总量超限时按"最大优先"落盘；
- 同一次运行内同 id 复用替换（不重复写盘）；
- session_id 隔离目录。

接入点：loop._execute_tool_calls 执行后 `compact_batch(results, store)`，
返回替换后的 (id, output) 列表再拼 tool_result 消息。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

PERSIST_THRESHOLD = 50_000      # 单条落盘阈值（字符）
BATCH_BUDGET = 200_000          # 每轮工具结果批次预算
PREVIEW_CHARS = 2000            # 上下文里保留的预览长度

TAG_OPEN = "<persisted-output>"
TAG_CLOSE = "</persisted-output>"


@dataclass
class PersistResult:
    text: str              # 替换后的文本（小输出原样返回）
    persisted: bool
    rel_path: str | None   # 落盘相对路径（相对 workspace_root）


class ToolResultStore:
    """按 session 落盘工具结果，同轮内 id 去重。"""

    def __init__(self, workspace_root: Path, session_id: str) -> None:
        self.workspace_root = Path(workspace_root).resolve()
        self.dir = self.workspace_root / "data" / "tool-results" / session_id
        # tool_call_id -> (落盘绝对路径, 已写内容)：同 id 同内容复用不重复写盘；
        # 同 id 不同内容（MockLLM 固定 call_0001 跨轮复用）重写，保证文件与预览一致
        self._written: dict[str, tuple[Path, str]] = {}

    def persist(
        self, tool_call_id: str, text: str, *, force: bool = False
    ) -> PersistResult:
        """单条落盘；≤阈值原样返回，>阈值落盘并生成替换文本。

        force=True 用于批内预算强制落盘（单条可能没超阈值，但批总量超了）。
        头部文案如实区分两种原因，不误导模型（用户红线：不造假）。
        """
        if len(text) <= PERSIST_THRESHOLD and not force:
            return PersistResult(text=text, persisted=False, rel_path=None)

        if len(text) > PERSIST_THRESHOLD:
            header = f"Output too large ({len(text)} chars)"
        else:
            header = f"Output batch-compacted ({len(text)} chars)"

        # 同 id 复用：同内容不重复写盘；内容变了重写（不留下过时文件）
        if tool_call_id in self._written:
            abs_path, old_text = self._written[tool_call_id]
            if old_text != text:
                abs_path.write_text(text, encoding="utf-8")
                self._written[tool_call_id] = (abs_path, text)
        else:
            self.dir.mkdir(parents=True, exist_ok=True)
            abs_path = self.dir / f"{tool_call_id}.txt"
            abs_path.write_text(text, encoding="utf-8")
            self._written[tool_call_id] = (abs_path, text)

        rel = str(abs_path.relative_to(self.workspace_root)).replace("\\", "/")
        return PersistResult(
            text=self._replacement(rel, text, header), persisted=True, rel_path=rel
        )

    @staticmethod
    def _replacement(rel_path: str, text: str, header: str) -> str:
        preview = text[:PREVIEW_CHARS]
        return (
            f"{TAG_OPEN}\n"
            f"{header}. Full output saved to: {rel_path}\n"
            f"Preview (first {PREVIEW_CHARS} chars):\n"
            f"{preview}\n"
            f"{TAG_CLOSE}"
        )


def compact_batch(
    results: list[tuple[str, str]], store: ToolResultStore
) -> list[tuple[str, str]]:
    """对一轮工具结果做批次落盘：单条超限 → 落盘；批总量超预算 → 最大优先落盘。

    results: [(tool_call_id, output), ...]（顺序与 calls 对应）
    返回替换后的 [(tool_call_id, replaced_output), ...]。
    """
    replaced: list[tuple[str, str]] = []
    persisted: set[str] = set()
    for call_id, text in results:
        res = store.persist(call_id, text)
        if res.persisted:
            persisted.add(call_id)
        replaced.append((call_id, res.text))

    def inline_bytes() -> int:
        return sum(len(t) for cid, t in replaced if cid not in persisted)

    # 批内预算：剩余未落盘的总长 > 预算 → 从最大开始强制落盘直到 ≤ 预算
    if inline_bytes() > BATCH_BUDGET:
        candidates = sorted(
            ((cid, t) for cid, t in replaced if cid not in persisted),
            key=lambda p: len(p[1]),
            reverse=True,
        )
        for call_id, text in candidates:
            res = store.persist(call_id, text, force=True)  # force：单条未超阈值也落盘
            if res.persisted:
                persisted.add(call_id)
                replaced = [
                    (cid, res.text if cid == call_id else t) for cid, t in replaced
                ]
            if inline_bytes() <= BATCH_BUDGET:
                break
    return replaced
