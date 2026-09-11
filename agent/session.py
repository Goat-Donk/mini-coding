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
- **会话元数据（M9-3）**：`data/sessions/{session_id}.meta.json`——人给的名字、
  分叉来源。**它不是 AgentState 的一部分**，所以刻意不塞进检查点：检查点里放的
  是"任务跑到哪了"（可被 `--resume` 喂回引擎的状态），而名字是**人给的标签**、
  分叉来源是**派生自检查点目录布局的事实**。放进去等于让 state 多两个既不影响
  推理、又要跟着全字段往返一起走的字段。
- **分叉（M9-3）**：`fork()` 从**任意一步**检查点开一个新会话。注意它是
  **对话分叉，不是工作区分叉**（见 `fork()` 的说明）。
"""
from __future__ import annotations

import dataclasses
import json
import threading
import time
from dataclasses import asdict, dataclass, is_dataclass
from pathlib import Path

from agent.goal import Goal
from agent.llm import Usage
from agent.security import TAINT_MEDIUM, TAINT_NONE, higher
from agent.state import AgentState

# 不落盘的字段：运行时对象（回调、句柄），不是状态。**这份名单应该保持短**——
# 每加一项都意味着「这个字段跨会话会丢」，需要有理由。
_SKIP_FIELDS = frozenset({"emitter"})

# 检查点节拍的缺省值。放在模块级是因为它有两个读者：新会话的构造、以及
# `from_checkpoint` 读到**没有记节拍的老检查点**时的回落。两边写在两个地方，
# 就会出现"新会话每 5 步、老会话恢复成别的数"这种没人能一眼看出的不一致。
DEFAULT_CHECKPOINT_EVERY = 5

# 需要反序列化回类型的字段（JSON 里是 dict，dataclass 要的是对象）。
# 其余字段按 JSON 原样传回 `AgentState(**raw)`。
#
# ⚠️ **给 AgentState 加一个 dataclass 字段时，如果它不是 JSON 原生类型，就必须
# 在这里补一行。** `load_state` 走的是 `AgentState(**raw)`，而 dataclass 不做
# 类型检查 —— 漏了这一行，字段会是个 `dict`，直到有人读它的属性才炸；而那个
# 炸点被 `QueryEngine._run_loop` 的 `except Exception` 吞成
# `terminated_reason="error"`，**看起来像引擎出错**（M9-6 的 `goal` 就属于这类）。
_FIELD_DECODERS: dict[str, object] = {
    "usage": lambda raw: Usage(**raw),
    "last_usage": lambda raw: Usage(**raw) if raw else None,
    "goal": lambda raw: Goal(**raw) if raw else None,
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


# ---------- 会话元数据（M9-3：名字 + 分叉来源） ----------

#: 会话名的长度上限。**长度本身不是安全问题**（名字不参与任何路径拼接），
#: 它只是给人看的；设上限是为了让 `--sessions` 的表格不被一个超长名字撑爆。
NAME_MAX_CHARS = 60

#: 名字里不允许出现的字符：路径分隔符（避免有人误以为名字能当地址用）与控制字符。
#: 注意名字**从不参与路径拼接**——路径一律用 session_id（那是我们生成的、
#: 字符集可控的）。这条校验是防"看起来像地址"，不是防穿越。
_NAME_FORBIDDEN = set('\\/:*?"<>|')


def _sessions_dir(workspace_root: Path) -> Path:
    return Path(workspace_root).resolve() / "data" / "sessions"


def _checkpoints_root(workspace_root: Path) -> Path:
    return Path(workspace_root).resolve() / "data" / "checkpoints"


def meta_path(workspace_root: Path, session_id: str) -> Path:
    """会话元数据文件路径：`data/sessions/{session_id}.meta.json`。

    与轨迹 `{session_id}.jsonl` 同目录、不同后缀，`*.json` 的 glob 不会误伤 `.jsonl`。
    """
    return _sessions_dir(workspace_root) / f"{session_id}.meta.json"


def read_meta(workspace_root: Path, session_id: str) -> dict:
    """读会话元数据；**没有这个文件返回空 dict**，文件坏了则抛错。

    两者的区别是刻意的：没有 meta 是正常状态（M9-3 之前建的会话、或从没改过名的
    会话都没有），而 meta 存在却解析不了只可能是被手改坏或写了一半 —— 那时**名字
    是真的丢了**，静默返回 `{}` 会让人以为"我从没起过名字"，于是重新起一个、
    旧的就此消失且没有任何痕迹。
    """
    path = meta_path(workspace_root, session_id)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"会话元数据损坏: {path}（{exc}）") from exc
    if not isinstance(data, dict):
        raise ValueError(f"会话元数据格式不对（应为 JSON 对象）: {path}")
    return data


def update_meta(workspace_root: Path, session_id: str, **fields) -> Path:
    """合并式更新会话元数据（原子写）。

    **合并而不是覆盖**：`--rename` 只该改名字，不该把 `forked_from` 一起抹掉。
    覆盖式写在这里是一个安静的 bug —— 改完名之后 `--sessions` 里的分叉来源
    就没了，而且没有任何提示。
    """
    data = read_meta(workspace_root, session_id)
    data.update(fields)
    data["session_id"] = session_id   # 自描述：文件被挪走时还认得出自己是谁
    path = meta_path(workspace_root, session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)   # 同检查点：原子替换，kill 不留半个文件
    return path


def _name_taken(workspace_root: Path, name: str, *, allow_session: str | None = None) -> str | None:
    """名字被占用则返回占用者的 session_id，否则 None。

    `allow_session` 是"这个名字本来就属于它"的那一个 —— 把一个会话改成它当前
    的名字不该报错（幂等）。除此之外任何重复都要拦。

    **为什么重名也必须拦**：`resolve_session` 按名字查找只能返回**一个**结果。
    两个会话同名时，`--resume --session-id <name>` 会安静地跑到其中先被遍历到的
    那个上去 —— 带着另一个任务的上下文继续。这比"报错说我找不到"糟糕得多。
    """
    if (_checkpoints_root(workspace_root) / name).exists():
        return name  # 撞上某个 session_id
    for info in list_sessions(workspace_root):
        if info.name == name and info.session_id != allow_session:
            return info.session_id
    return None


def validate_name(
    workspace_root: Path, name: str, *, allow_session: str | None = None
) -> str:
    """校验并返回规范化的会话名；不合法则抛 `ValueError`（带可读原因）。

    判据里最要紧的是「不与任何已有 session_id / 会话名相同」。`resolve_session`
    先按 id 再按名字解析，如果名字能等于某个 id，那个 id 就永远解析不到自己的
    会话了 —— 而且失败方式是"静默跑到另一个会话上去"。所以在这头拒绝，
    而不是在解析那头加优先级去猜（猜错的代价是带着另一个会话的上下文继续跑）。

    `allow_session`：这个名字本来就属于的那个会话（改名幂等用），见 `_name_taken`。
    """
    cleaned = (name or "").strip()
    if not cleaned:
        raise ValueError("会话名不能为空")
    if len(cleaned) > NAME_MAX_CHARS:
        raise ValueError(f"会话名最长 {NAME_MAX_CHARS} 字符，当前 {len(cleaned)}")
    bad = sorted(_NAME_FORBIDDEN & set(cleaned))
    if bad or any(ord(c) < 32 for c in cleaned):
        raise ValueError(f"会话名不能含路径分隔符/控制字符: {''.join(bad) or '控制字符'}")
    taken = _name_taken(workspace_root, cleaned, allow_session=allow_session)
    if taken is not None:
        raise ValueError(f"会话名 {cleaned!r} 已被 {taken} 占用，换一个")
    return cleaned


def set_session_name(workspace_root: Path, session_id: str, name: str) -> Path:
    """给人一个名字（`--rename`）。目标会话必须**已经存在**。

    不存在的会话不接受改名：那会造出一个只有 meta 文件、没有任何检查点的幽灵，
    `--sessions` 里看着像回事，`--resume` 却报"没有可恢复的检查点"。
    """
    if not (_checkpoints_root(workspace_root) / session_id).exists():
        raise ValueError(f"会话不存在: {session_id}")
    return update_meta(
        workspace_root, session_id,
        name=validate_name(workspace_root, name, allow_session=session_id),
    )


def session_name(workspace_root: Path, session_id: str) -> str | None:
    """读会话名；没有/不是字符串 → None。meta 损坏同样返回 None。

    这里**不抛错**：调用方是取名字来显示或拼默认名的，为一个坏 meta 让整条
    命令失败不成比例。真正需要"名字丢了要响"的地方是 `--sessions`（那里
    逐行标出 `meta_error`），不是这里。
    """
    try:
        name = read_meta(workspace_root, session_id).get("name")
    except ValueError:
        return None
    return name if isinstance(name, str) and name else None


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


def derive_taint(events: list[dict]) -> str | None:
    """从轨迹事件重算污染级别。**没有相关事件时返回 None**（区别于"算出 none"）。

    为什么要有这个函数：`taint` 是**派生值**，落盘的那份只是缓存。如果只信检查点
    里那个字段，一块损坏/被改写的检查点就能把标记悄悄抹掉（或者把项目永久锁死）。
    按事件重放则两者都能收敛：`security_finding` 抬高、`taint_cleared` 复位。

    「没有相关事件 → None」这个区分是必要的：老检查点（M7 之前）根本没有这些
    事件，此时必须回退到落盘值，而不能因为"重放没算出东西"就把标记当 none。
    """
    if not any(e.get("type") in ("security_finding", "taint_cleared") for e in events):
        return None
    level = TAINT_NONE
    for event in events:
        if event.get("type") == "security_finding":
            level = higher(level, str(event.get("level") or TAINT_MEDIUM))
        elif event.get("type") == "taint_cleared":
            level = TAINT_NONE          # 人的复位动作在事件流里同样有效
    return level


def load_state(payload: dict, session_id: str) -> AgentState:
    """检查点 payload → AgentState（兼容旧的扁平格式）。"""
    raw = dict(state_dict(payload))
    for name, decode in _FIELD_DECODERS.items():
        if name in raw and raw[name] is not None:
            raw[name] = decode(raw[name])  # type: ignore[operator]
    raw.pop("session_id", None)  # 用调用方给的那个（payload 里的可能是旧值）

    # 污染标记：事件重放优先于落盘字段。重放算得出结果就以它为准 —— 它同时
    # 包含了「抬升」与「人的复位」，所以 `--clear-taint` 之后 resume 不会被
    # 旧事件重新抬回去；重放算不出（M7 之前的检查点）才用落盘值。
    derived = derive_taint(raw.get("events") or [])
    if derived is not None:
        raw["taint"] = derived
    return AgentState(session_id=session_id, **raw)


def _stored_cadence(payload: dict) -> int | None:
    """从检查点 payload 里读会话当初的检查点节拍；没有/不可用 → None。

    读不出来时返回 None 而**不是**某个默认值：调用方才是决定"回落成什么"的地方，
    这里替它决定会让 `DEFAULT_CHECKPOINT_EVERY` 的改动只生效一半。

    非法值（0 / 负数 / 非整数 —— 比如检查点被手改过）同样返回 None 走回落，
    而不是夹到 1：夹成 1 意味着"每步都写"，对一个已经损坏的检查点做出比正常
    默认更激进的行为，是错的方向。
    """
    value = payload.get("checkpoint_every")
    if isinstance(value, int) and not isinstance(value, bool) and value >= 1:
        return value
    return None


class Session:
    def __init__(
        self,
        workspace_root: Path,
        session_id: str,
        *,
        checkpoint_every: int = DEFAULT_CHECKPOINT_EVERY,
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

    def checkpoint(self, state: AgentState, *, force: bool = False) -> None:
        """每 checkpoint_every 步写一次检查点（幂等调用）。

        `force=True` 绕过节流，立刻落盘。**这不是优化，是正确性**：节流默认 5 步，
        而 `ask_user` 可能发生在第 3 步 —— 那一刻进程就退出了，下一次 tick 永远
        不会来，于是问题没进检查点，`--resume` 恢复出来的会话里**没有那个问题**，
        用户对着一个不知道在问什么的会话回答。凡是「流程即将因非步数原因退出」的
        场合都要 force（当前只有 await_user 一处）。
        """
        self._ticks += 1
        if force or self._ticks % self.checkpoint_every == 0:
            self._write(state)

    def _write(self, state: AgentState) -> Path:
        payload = {
            "session_id": self.session_id,
            "step": state.step,
            "ts": time.time(),
            # 节拍**必须跟着会话落盘**，否则 `--resume` 只能靠"没传就用 5"这个
            # 隐式回落 —— 而 5 只是 CLI 的默认值，不是这个会话的事实。会话当初
            # 是 `--checkpoint-every 1` 起的，恢复后却按 5 走，用户没有任何办法
            # 看出这件事（命令成功、输出正常、退出码 0，唯一证据是检查点数不涨）。
            # 放在 payload 顶层而不是 state 里：它是**会话配置**，不是 AgentState
            # 的字段（state 会被 `load_state` 整个喂给 `AgentState(**raw)`）。
            "checkpoint_every": self.checkpoint_every,
            "state": dump_state(state),   # 全字段快照（见模块 docstring）
        }
        return self._write_payload(state.step, payload)

    def _write_payload(self, step: int, payload: dict) -> Path:
        """把一个**已经组装好的** payload 原子写到 `step-{step}.json`。

        与 `_write` 分开是因为 `fork()` 要写的是从别处读来的 payload（内容不由
        本会话的 state 决定）。两条路径共用同一份"怎么落盘"的知识 —— 原子写、
        缩进、编码。各写一遍的话，将来改落盘格式就会漏掉分叉这条路径，
        而它的失败方式是**静默的**：分叉出来的会话少了某个字段，跑起来才发现。
        """
        self.checkpoint_dir.mkdir(parents=True, exist_ok=True)
        path = self.checkpoint_dir / f"step-{step}.json"
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
        checkpoint_every: int | None = None,
    ) -> tuple["Session", AgentState]:
        """从检查点恢复；step=None → 该 session 最近一个检查点。

        返回 (Session, AgentState)；AgentState 直接传给
        QueryEngine.run_from(state) 继续执行。

        `checkpoint_every=None` = **沿用这个会话当初的节拍**（从被恢复的那个
        检查点里读），而不是回落 CLI 的默认值。优先级：**显式传参 > 会话里记的
        > `DEFAULT_CHECKPOINT_EVERY`**（最后一条只对本次改动之前写下的老检查点
        生效 —— 它们没有这个字段）。

        为什么值得单独一档：节拍是**这个会话事实的一部分**。用 `--checkpoint-every 1`
        起的会话崩在半路，恢复时按 5 走，代价是丢一整段工作（真跑现场：kill 在
        step 5，续跑到 step 9，检查点数还是 5）。而且它是**哑的**：命令成功、
        退出码 0，唯一证据是检查点数没涨。
        """
        session = cls(
            workspace_root, session_id,
            # 显式传了就用它；没传先按默认构造，下面读到会话里记的再改
            checkpoint_every=(
                DEFAULT_CHECKPOINT_EVERY if checkpoint_every is None else checkpoint_every
            ),
        )
        payload = session._load_payload(step)
        if checkpoint_every is None:
            stored = _stored_cadence(payload)
            if stored is not None:
                session.checkpoint_every = stored
        return session, load_state(payload, session_id)

    def _load_payload(self, step: int | None) -> dict:
        if step is None:
            path = self.latest_checkpoint()
            if path is None:
                raise FileNotFoundError(f"没有可恢复的检查点: {self.checkpoint_dir}")
        else:
            path = self.checkpoint_dir / f"step-{step}.json"
            if not path.exists():
                # 检查点是**按节拍**落的：`--checkpoint-every 2` 的会话只有偶数步。
                # 裸的 `FileNotFoundError: .../step-3.json` 既看不出是步数写错了、
                # 也看不出是节拍问题，而 step 级分叉正是我们对外宣传的能力 ——
                # 所以要把**实际有哪几步**说出来（同 `SessionNotFound` 的做法：
                # 报错必须给出下一步，只说"找不到"等于把用户扔在原地）。
                available = self.list_checkpoints()
                hint = f"可用: {available}" if available else "这个会话还没有检查点"
                raise FileNotFoundError(f"第 {step} 步没有检查点（{hint}）")
        return json.loads(path.read_text(encoding="utf-8"))

    # ---------- 分叉 ----------

    @classmethod
    def fork(
        cls,
        workspace_root: Path,
        session_id: str,
        *,
        step: int | None = None,
        new_id: str | None = None,
        name: str | None = None,
    ) -> tuple["Session", AgentState]:
        """从源会话的**某一步**检查点分叉出一个新会话，返回 (Session, AgentState)。

        与 TS 原版的差别在这里：它的 `/fork` 是**会话级**的（把整段会话复制一份
        从"现在"接着走），我们挂在 **step 级检查点**上，所以可以**回到任意一步**
        再开一条路 —— 检查点本来就是逐步落的，这个能力是现成的。

        **它是对话分叉，不是工作区分叉。** 这条必须说清楚，否则很容易被当成
        "时光倒流"：工作区（文件）不会被回滚到第 N 步的样子，分叉后的 agent 看到
        的是**当前**的工作区。想做"回到第 3 步且文件也回到第 3 步"，需要的是
        工作区快照，我们**没有**这个机制。所以 CLI 在分叉后会把这句打出来。

        搬什么（三样，都以 fork_step 为界）：
        1. **检查点 `step-1..fork_step`** —— 于是新会话可以 `--resume --step K`
           回到其中任意一步，而不只是分叉点。
        2. **轨迹里 `step <= fork_step` 的行**。不能整份复制：轨迹是**追加**的，
           源会话在 fork_step 之后的 `security_finding` 也会被一起搬过去，
           分叉出来的会话于是"继承"了它根本没发生过的事件。
        3. **meta 里的 `forked_from`**（来源 + 步数），供 `--sessions` 显示血统。
        """
        source = cls(Path(workspace_root), session_id)
        payload = source._load_payload(step)
        fork_step = int(payload["step"])
        sid = new_id or unique_session_id(workspace_root)

        cadence = _stored_cadence(payload)
        fork = cls(
            Path(workspace_root), sid,
            checkpoint_every=DEFAULT_CHECKPOINT_EVERY if cadence is None else cadence,
        )

        for n in source.list_checkpoints():
            if n > fork_step:
                continue
            src = source.checkpoint_dir / f"step-{n}.json"
            copied = json.loads(src.read_text(encoding="utf-8"))
            # 副本里那个 `session_id` 是**源会话的**。`load_state` 会 pop 掉它，
            # 所以今天无害 —— 但它是一颗定时炸弹：谁哪天直接读 `payload["session_id"]`
            # 就会拿到源会话。宁可现在重写，也不留一个"当前无人读、将来必有人读"的错值。
            copied["session_id"] = sid
            fork._write_payload(n, copied)

        fork._copy_trajectory(source, fork_step)
        update_meta(
            Path(workspace_root), sid,
            name=(validate_name(workspace_root, name) if name else default_fork_name(
                workspace_root, session_id, fork_step
            )),
            forked_from={"session": session_id, "step": fork_step},
        )
        return fork, load_state(fork._load_payload(fork_step), sid)

    def _copy_trajectory(self, source: "Session", fork_step: int) -> int:
        """把源轨迹里 `step <= fork_step` 的行搬过来，返回搬了多少行。

        解析不了的行**原样保留**：轨迹是 kill 在写一半时唯一留下的证据，
        因为"这一步我读不懂"就把它删掉，等于把唯一的现场扔了。它也不会影响
        任何判定 —— 污染标记重放读的是**检查点里的 `state.events`**，不是轨迹。
        """
        if not source.trajectory_path.exists():
            return 0
        kept: list[str] = []
        for line in source.trajectory_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                event = json.loads(line)
                if int(event.get("step", 0)) > fork_step:
                    continue
            except (json.JSONDecodeError, TypeError, ValueError):
                pass  # 读不懂 → 留着（见 docstring）
            kept.append(line)
        if kept:
            self.trajectory_path.parent.mkdir(parents=True, exist_ok=True)
            self.trajectory_path.write_text(
                "\n".join(kept) + "\n", encoding="utf-8"
            )
        # 分叉这件事本身也记一笔：新会话的轨迹第一眼看不出自己是分叉来的，
        # 而 `--sessions` 只有最近状态、没有历史。事件补上这段血统。
        self.emit({
            "ts": time.time(),
            "type": "session_forked",
            "step": fork_step,
            "from_session": source.session_id,
            "from_step": fork_step,
        })
        return len(kept)

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


# ---------- 会话清单与查找（M9-3） ----------

@dataclass(frozen=True)
class SessionInfo:
    """`--sessions` 一行所需的全部信息（只读快照，不载入检查点正文）。"""

    session_id: str
    name: str | None
    latest_step: int
    checkpoint_count: int
    mtime: float
    forked_from: dict | None = None
    meta_error: str | None = None   # meta 文件坏了：名字没了，但会话本身还在


class SessionNotFound(LookupError):
    """按 id/名字都找不到会话。`available` 是候选清单（供报错时列出来）。"""

    def __init__(self, ref: str, available: list[str]) -> None:
        self.ref = ref
        self.available = available
        super().__init__(f"找不到会话: {ref}")


def list_sessions(workspace_root: Path) -> list[SessionInfo]:
    """列出工作区里所有会话（按最近活动倒序）。

        **键集合取自"轨迹 ∪ 检查点目录"**，而不是只看检查点目录：跑到一半被 kill、
        还没到第一个检查点就死掉的会话，只有轨迹。那种会话恰恰是最需要被看见的
        ——「我明明跑过」和「列表里没有」之间不该有落差。

    读不出名字（meta 损坏）不会让整张表挂掉，只在那一行标出来：一个坏文件
    不该让另外九个正常会话也看不见。
    """
    root = Path(workspace_root).resolve()
    ids: set[str] = set()
    checkpoints_root = root / "data" / "checkpoints"
    if checkpoints_root.exists():
        ids |= {d.name for d in checkpoints_root.iterdir() if d.is_dir()}
    sessions_dir = root / "data" / "sessions"
    if sessions_dir.exists():
        ids |= {p.stem for p in sessions_dir.glob("*.jsonl")}

    infos: list[SessionInfo] = []
    for sid in ids:
        cp_dir = checkpoints_root / sid
        steps = sorted(
            int(p.stem.split("-")[1])
            for p in cp_dir.glob("step-*.json")
            if not p.name.endswith(".tmp")
        ) if cp_dir.exists() else []
        stamps = [p.stat().st_mtime for p in cp_dir.glob("step-*.json")] if cp_dir.exists() else []
        traj = sessions_dir / f"{sid}.jsonl"
        if traj.exists():
            stamps.append(traj.stat().st_mtime)
        name: str | None = None
        forked_from: dict | None = None
        meta_error: str | None = None
        try:
            meta = read_meta(root, sid)
            raw_name = meta.get("name")
            name = raw_name if isinstance(raw_name, str) and raw_name else None
            raw_from = meta.get("forked_from")
            forked_from = raw_from if isinstance(raw_from, dict) else None
        except ValueError as exc:
            meta_error = str(exc)
        infos.append(SessionInfo(
            session_id=sid,
            name=name,
            latest_step=steps[-1] if steps else 0,
            checkpoint_count=len(steps),
            mtime=max(stamps) if stamps else 0.0,
            forked_from=forked_from,
            meta_error=meta_error,
        ))
    return sorted(infos, key=lambda i: i.mtime, reverse=True)


def resolve_session(workspace_root: Path, ref: str) -> str:
    """把用户给的引用解析成 session_id：**先当 id，再当名字**。

    id 优先是没有商量余地的：id 是权威标识，名字只是标签。而"名字与 id 撞车"
    这种情况在 `validate_name` 那头就被拒绝了，所以这里不需要靠优先级去消歧 ——
    两处合起来才成立：**拒绝坏名字（写入侧）+ id 优先（读取侧）**。
    """
    root = Path(workspace_root).resolve()
    if (root / "data" / "checkpoints" / ref).exists():
        return ref
    infos = list_sessions(root)
    for info in infos:
        if info.name == ref:
            return info.session_id
    raise SessionNotFound(ref, [i.session_id for i in infos])


def unique_session_id(workspace_root: Path) -> str:
    """分配一个没被占用的 session_id。

    `new_session_id()` 的粒度是**秒**，而两条命令在同一秒里跑完完全正常
    （测试里几乎是必然）。撞了的话 `Session.__init__` 不报错，两个会话直接
    共用一个检查点目录 —— 后者的检查点覆盖前者，且全程静默。所以这里显式让开。
    """
    base = new_session_id()
    sid, n = base, 2
    while (
        (_checkpoints_root(workspace_root) / sid).exists()
        or (_sessions_dir(workspace_root) / f"{sid}.jsonl").exists()
    ):
        sid = f"{base}-{n}"
        n += 1
    return sid


def default_fork_name(workspace_root: Path, source_id: str, fork_step: int) -> str:
    """分叉会话的默认名字：`{源名或 id} @{步} 分叉`（重名时补 `-2` / `-3`）。

    默认给名字而不是留空，是为了让 `--sessions` 里一眼能看出血统和分叉点
    —— 一排 `s20260911-153012` 之间看不出哪两个是同一个任务的两条路。
    用户可以 `--rename` 覆盖它。

    重名只能**自动让开**、不能报错：同一秒里从同一步分叉两次是很正常的操作，
    为此让第二条命令失败，是把我们自己造的命名细节变成了用户的路障。
    """
    label = session_name(workspace_root, source_id) or source_id
    base = label[: NAME_MAX_CHARS - len(" @999 分叉")] + f" @{fork_step} 分叉"
    candidate, n = base, 2
    while _name_taken(workspace_root, candidate) is not None:
        tail = f"-{n}"
        candidate = base[: NAME_MAX_CHARS - len(tail)] + tail
        n += 1
    return candidate
