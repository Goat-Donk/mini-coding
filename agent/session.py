"""会话轨迹 + 检查点 + resume（M3-4，对应 Claude Code 的 /resume）。

- **JSONL 轨迹**：每个事件一行，`data/sessions/{session_id}.jsonl`。loop 经
  `state.emitter → session.emit` 写入，append-only——M3-3 compact 裁掉的中段
  消息在轨迹里仍完整保留（snip/摘要标记都指向这里）。
- **检查点**：每 N 步写 `data/checkpoints/{session_id}/step-{N}.json`
  （messages + 全 state 快照）。任务中途 kill 进程后，`--resume` 从最近
  检查点恢复，接着上次的 step 计数续跑（不重置，避免覆盖同名检查点）。
- **原子写**：先写 `.tmp` 再 rename，kill 不会留下半个检查点文件。
- **全字段往返（M7）**：state 的序列化/反序列化按 `dataclasses.fields()` 自动
  推导，**不再手写字段白名单**。原先 `_write` 与 `from_checkpoint` 各有一份手写
  字段表，给 AgentState 加字段**不会落盘**且**没有任何提示**——已有先例：
  `cli.py` 重算 `memory_blocks`，`restored.memory_blocks` 被静默忽略。手写白名单
  是那种「加了新东西看起来对、实际悄悄不生效」的坑，所以这里改成结构性修法：
  新增字段自动进检查点，只有**确实是运行时对象**的字段才需要显式排除。
"""
from __future__ import annotations

import dataclasses
import json
import threading
import time
from dataclasses import asdict, is_dataclass
from pathlib import Path

from agent.llm import Usage
from agent.state import AgentState

# 不落盘的字段：运行时对象（回调、句柄），不是状态。**这份名单应该保持短**——
# 每加一项都意味着「这个字段跨会话会丢」，需要有理由。
_SKIP_FIELDS = frozenset({"emitter"})

# 需要反序列化回类型的字段（JSON 里是 dict，dataclass 要的是对象）。
# 其余字段按 JSON 原样传回 `AgentState(**raw)`。
_FIELD_DECODERS: dict[str, object] = {
    "usage": lambda raw: Usage(**raw),
    "last_usage": lambda raw: Usage(**raw) if raw else None,
}

# M7 之前的落盘格式：state 字段平铺在 payload 顶层。这份名单是**冻结的**——
# 它只用来解已经写在磁盘上的老检查点，不会再跟着 AgentState 演进（新字段一律
# 走 `state` 子对象那条路）。所以它不会重复 H5 那个「白名单忘记更新」的坑。
_LEGACY_FLAT_KEYS: tuple[str, ...] = (
    "task", "system_prompt", "messages", "step", "usage", "events",
    "terminated_reason", "last_usage", "usage_stale_reason", "memory_blocks",
)


def new_session_id() -> str:
    """生成会话 id（秒级时间戳，够区分一次演示/任务的连续运行）。"""
    return time.strftime("s%Y%m%d-%H%M%S")


# ---------- state 序列化（全字段，不手写白名单） ----------

def dump_state(state: AgentState) -> dict:
    """AgentState → 可 JSON 序列化的 dict（全字段，排除运行时对象）。

    非 JSON 可序列化的字段会被**明确指出字段名**地报错，而不是让
    `json.dumps` 抛一句看不出是哪个字段的 `TypeError`，也不是静默丢掉它。
    检查点写失败是响的、看得见的；字段悄悄丢失是哑的——这一条改动就是为了
    把哑失败变成响失败。
    """
    data: dict = {}
    for f in dataclasses.fields(state):
        if f.name in _SKIP_FIELDS:
            continue
        value = getattr(state, f.name)
        data[f.name] = asdict(value) if is_dataclass(value) else value
    try:
        json.dumps(data, ensure_ascii=False)
    except TypeError as exc:
        for name, value in data.items():
            try:
                json.dumps(value, ensure_ascii=False)
            except TypeError:
                raise TypeError(
                    f"检查点无法序列化 AgentState.{name}"
                    f"（类型 {type(value).__name__}）：{exc}。"
                    f"若它是运行时对象而非状态，加进 session._SKIP_FIELDS。"
                ) from exc
        raise
    return data


def state_dict(payload: dict) -> dict:
    """从检查点 payload 里取出 state 字段字典（兼容 M7 之前的扁平格式）。

    **读检查点的地方都应该用这个**，而不是自己去 payload 里翻 `"task"` /
    `"messages"`——那样每换一次落盘格式，所有读者都得跟着改一遍（回放 UI 与
    测试就踩过这个）。格式知识集中在 session.py 这一处。
    """
    if "state" in payload:
        return payload["state"]
    return {k: payload[k] for k in _LEGACY_FLAT_KEYS if k in payload}


def load_state(payload: dict, session_id: str) -> AgentState:
    """检查点 payload → AgentState（兼容旧的扁平格式）。"""
    raw = dict(state_dict(payload))
    for name, decode in _FIELD_DECODERS.items():
        if name in raw and raw[name] is not None:
            raw[name] = decode(raw[name])  # type: ignore[operator]
    raw.pop("session_id", None)  # 用调用方给的那个（payload 里的可能是旧值）
    return AgentState(session_id=session_id, **raw)


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
        # 只读工具是并发执行的，emit 会被多个线程同时调用；
        # 加锁保证 JSONL 一行一个完整事件、不会两行交错（轨迹可被逐行解析）
        self._write_lock = threading.Lock()

    # ---------- 轨迹 ----------

    def emit(self, event: dict) -> None:
        """追加一行事件到 JSONL（由 AgentState.record_event 回调）。"""
        if self._on_event is not None:
            try:
                self._on_event(event)
            except Exception:
                pass  # 监听器失败不阻断轨迹落盘
        self.trajectory_path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(event, ensure_ascii=False) + "\n"
        with self._write_lock:
            with self.trajectory_path.open("a", encoding="utf-8") as f:
                f.write(line)

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
            "state": dump_state(state),   # 全字段快照（见模块 docstring）
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
        return session, load_state(payload, session_id)

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
