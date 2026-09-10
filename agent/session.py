"""会话轨迹 + 检查点 + resume（M3-4，对应 Claude Code 的 /resume）。

- **JSONL 轨迹**：每个事件一行，`data/sessions/{session_id}.jsonl`。loop 经
  `state.emitter → session.emit` 写入，append-only——M3-3 compact 裁掉的中段
  消息在轨迹里仍完整保留（snip/摘要标记都指向这里）。
- **检查点**：每 N 步写 `data/checkpoints/{session_id}/step-{N}.json`
  （messages + 全 state 快照）。任务中途 kill 进程后，`--resume` 从最近
  检查点恢复，接着上次的 step 计数续跑（不重置，避免覆盖同名检查点）。
- **原子写**：先写 `.tmp` 再 rename，kill 不会留下半个检查点文件。
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict
from pathlib import Path

from agent.llm import Usage
from agent.state import AgentState


def new_session_id() -> str:
    """生成会话 id（秒级时间戳，够区分一次演示/任务的连续运行）。"""
    return time.strftime("s%Y%m%d-%H%M%S")


class Session:
    def __init__(
        self,
        workspace_root: Path,
        session_id: str,
        *,
        checkpoint_every: int = 5,
        on_event=None,
    ) -> None:
        self.workspace_root = Path(workspace_root).resolve()
        self.session_id = session_id
        self.checkpoint_every = max(1, checkpoint_every)
        self.trajectory_path = (
            self.workspace_root / "data" / "sessions" / f"{session_id}.jsonl"
        )
        self.checkpoint_dir = (
            self.workspace_root / "data" / "checkpoints" / session_id
        )
        self._on_event = on_event  # 可选监听器（UI 实时流式渲染用），失败不影响轨迹
        self._ticks = 0  # 恢复后重新计数：每 N 步（本段运行）写一个检查点

    # ---------- 轨迹 ----------

    def emit(self, event: dict) -> None:
        """追加一行事件到 JSONL（由 AgentState.record_event 回调）。"""
        if self._on_event is not None:
            try:
                self._on_event(event)
            except Exception:
                pass  # 监听器失败不阻断轨迹落盘
        self.trajectory_path.parent.mkdir(parents=True, exist_ok=True)
        with self.trajectory_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")

    # ---------- 检查点 ----------

    def checkpoint(self, state: AgentState) -> None:
        """每 checkpoint_every 步写一次检查点（幂等调用）。"""
        self._ticks += 1
        if self._ticks % self.checkpoint_every == 0:
            self._write(state)

    def _write(self, state: AgentState) -> Path:
        payload = {
            "session_id": self.session_id,
            "step": state.step,
            "ts": time.time(),
            "task": state.task,
            "system_prompt": state.system_prompt,
            "messages": state.messages,
            "events": state.events,
            "usage": asdict(state.usage),
            "last_usage": asdict(state.last_usage) if state.last_usage else None,
            "usage_stale_reason": state.usage_stale_reason,
            "terminated_reason": state.terminated_reason,
            "memory_blocks": state.memory_blocks,
        }
        self.checkpoint_dir.mkdir(parents=True, exist_ok=True)
        path = self.checkpoint_dir / f"step-{state.step}.json"
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        tmp.replace(path)  # 原子替换：进程被杀也不留半个文件
        return path

    # ---------- resume ----------

    @classmethod
    def from_checkpoint(
        cls,
        workspace_root: Path,
        session_id: str,
        *,
        step: int | None = None,
        checkpoint_every: int = 5,
    ) -> tuple["Session", AgentState]:
        """从检查点恢复；step=None → 该 session 最近一个检查点。

        返回 (Session, AgentState)；AgentState 直接传给
        QueryEngine.run_from(state) 继续执行。
        """
        session = cls(workspace_root, session_id, checkpoint_every=checkpoint_every)
        payload = session._load_payload(step)
        state = AgentState(
            session_id=session_id,
            task=payload["task"],
            system_prompt=payload["system_prompt"],
            messages=payload["messages"],
            step=payload["step"],
            usage=Usage(**payload["usage"]),
            events=payload.get("events", []),
            terminated_reason=payload.get("terminated_reason"),
            last_usage=(
                Usage(**payload["last_usage"]) if payload.get("last_usage") else None
            ),
            usage_stale_reason=payload.get("usage_stale_reason"),
            memory_blocks=payload.get("memory_blocks", []),
        )
        return session, state

    def _load_payload(self, step: int | None) -> dict:
        path = (
            self.checkpoint_dir / f"step-{step}.json"
            if step is not None
            else self.latest_checkpoint()
        )
        if path is None:
            raise FileNotFoundError(f"没有可恢复的检查点: {self.checkpoint_dir}")
        return json.loads(path.read_text(encoding="utf-8"))

    # ---------- 查询 ----------

    def list_checkpoints(self) -> list[int]:
        """该 session 已有的检查点 step（升序）。"""
        if not self.checkpoint_dir.exists():
            return []
        steps = []
        for p in self.checkpoint_dir.glob("step-*.json"):
            if not p.name.endswith(".tmp"):
                steps.append(int(p.stem.split("-")[1]))
        return sorted(steps)

    def latest_checkpoint(self) -> Path | None:
        """该 session 最近（step 最大）的检查点文件。"""
        steps = self.list_checkpoints()
        if not steps:
            return None
        return self.checkpoint_dir / f"step-{steps[-1]}.json"


def latest_session(workspace_root: Path) -> str | None:
    """返回有检查点的最近 session_id（按最近检查点的 mtime）。"""
    root = Path(workspace_root).resolve() / "data" / "checkpoints"
    if not root.exists():
        return None
    candidates: list[tuple[float, str]] = []
    for session_dir in root.iterdir():
        if not session_dir.is_dir():
            continue
        files = [
            p for p in session_dir.glob("step-*.json") if not p.name.endswith(".tmp")
        ]
        if not files:
            continue
        newest = max(p.stat().st_mtime for p in files)
        candidates.append((newest, session_dir.name))
    if not candidates:
        return None
    return max(candidates, key=lambda pair: pair[0])[1]
