"""检查点回放（M5-3）：把落盘的会话检查点渲染成可读对话，供控制台回放。

独立于 Streamlit 的纯函数层：视图只管「选会话 + 选步骤 + 渲染」，
列表/读取/渲染逻辑放这里，可用普通 pytest 离线测试（不依赖 streamlit runtime）。
"""
from __future__ import annotations

import json
from pathlib import Path

from agent.session import Session


def list_checkpoint_sessions(workspace_root: Path) -> list[str]:
    """有检查点的 session id，按最近检查点时间降序（新的在前）。"""
    root = Path(workspace_root).resolve() / "data" / "checkpoints"
    if not root.exists():
        return []
    candidates: list[tuple[float, str]] = []
    for session_dir in root.iterdir():
        if not session_dir.is_dir():
            continue
        files = [
            p for p in session_dir.glob("step-*.json")
            if not p.name.endswith(".tmp")
        ]
        if not files:
            continue
        newest = max(p.stat().st_mtime for p in files)
        candidates.append((newest, session_dir.name))
    return [name for _, name in sorted(candidates, reverse=True)]


def list_checkpoint_steps(workspace_root: Path, session_id: str) -> list[int]:
    """该 session 的检查点 step 列表（升序）。"""
    return Session(workspace_root, session_id).list_checkpoints()


def load_checkpoint(workspace_root: Path, session_id: str, step: int) -> dict | None:
    """读一个检查点 payload；不存在返回 None（并发落盘时正常出现）。"""
    path = (
        Path(workspace_root).resolve()
        / "data" / "checkpoints" / session_id / f"step-{step}.json"
    )
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def render_message(message: dict) -> str:
    """把 OpenAI 格式消息渲染成回放可读文本（长 tool result 截断到 500 字符）。"""
    role = message.get("role", "?")
    if role == "tool":
        content = str(message.get("content", ""))
        return content[:500] + ("…" if len(content) > 500 else "")
    calls = message.get("tool_calls")
    if calls:
        lines = []
        for c in calls:
            fn = c.get("function", {})
            raw = fn.get("arguments", "")
            try:  # arguments 是 JSON 字符串，解析后压缩成一行展示
                raw = json.dumps(json.loads(raw), ensure_ascii=False)
            except Exception:
                pass
            lines.append(f"→ {fn.get('name', '?')}({raw})")
        return "\n".join(lines)
    content = message.get("content")
    if isinstance(content, list):  # 多模态响应兜底（取纯文本片段）
        return "".join(
            part.get("text", "") for part in content if isinstance(part, dict)
        )
    return str(content) if content is not None else ""
