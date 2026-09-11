"""并发子代理管理器（M9-7，对应 Claude Code 的 spawn/wait/close 句柄契约）。

**它管的是线程与句柄，不管 agent 长什么样。** 跑什么由调用方给的 `runner`
闭包决定（`agent/tools/subagent.py` 构造它），所以这个模块是**叶子**：只用标准库，
不认识 `QueryEngine` / `RunResult` / `Usage`。这不是洁癖 —— `agent/tools/subagent.py`
在模块级 `import agent.loop`，而 `agent/loop.py` 要 import 本模块来做回合边界结算
（`settle`），反向再 import 回去就是环。

三条来自原版（TS）的契约，逐条对着抄：

1. **`spawn` 立刻返回句柄**，工作在后头跑。满了（`MAX_SUB_AGENTS` 个还在跑）
   **直接抛**，不排队 —— 排队会让"我派了三个"这句话变成"我派了三个，其中一个
   在等前面那个死"，而模型完全看不到这件事。
2. **「等」和「关」是两件事**：`wait` 超时只返回当下状态，**不关闭**。这不是
   疏忽 —— 关掉一个正跑得好好的 worker 只因为这一轮等够了，等于用一个超时
   去赌它的死活。
3. **`close` = abort + 等它真停**。返回时线程已经死了，句柄上的结论是它停下
   那一刻的事实。

**状态字母表刻意只有四个**：`running` / `done` / `failed` / `closed`。没有
`"closing"` —— `close()` 是阻塞的（它 join），没有任何时刻能观测到一个中间态，
为一个观测不到的态留一个位置只会让读的人以为它会出现。**被叫停的 worker 记
`closed`，不记 `done`/`failed`**：它没跑完，也没出错，是被叫停的。

线程用 `threading.Thread(daemon=True)`，**不用 `ThreadPoolExecutor`**：后者的
线程是非 daemon 的，`concurrent.futures.thread` 在解释器退出时挂了一个 atexit
join 钩子 —— 一个正卡在 120 秒 `llm.chat` 里的 worker 会让 CLI **退不出去**
（最长两分钟），而那时用户已经按了 Ctrl+D、屏幕上什么都没有。worker 的结论
只经句柄交付，不依赖线程池的生命周期，所以拆掉它是安全的。
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Sequence

#: 同时在跑的 worker 上限（对齐原版 `MAX_SUB_AGENTS`）。**数的是"还在跑"，
#: 不是"曾经派过"** —— 数后者的话，一次长会话在成功跑过 3 个子代理之后会
#: **永久锁死**，而且看起来像"子代理坏了"。
MAX_SUB_AGENTS = 3

AGENT_RUNNING = "running"
AGENT_DONE = "done"
AGENT_FAILED = "failed"
AGENT_CLOSED = "closed"

TERMINAL_STATUSES = frozenset({AGENT_DONE, AGENT_FAILED, AGENT_CLOSED})

#: 回合边界结算时 join 的上限。**与 `close()` 的"等到真停"刻意不同** ——
#: 见 `AgentWorkers.settle`。
SETTLE_TIMEOUT = 1.5

#: 结算事件里每条结论最多留多少字符。事件会进检查点，而 `--resume` 每次恢复
#: 都要读它（同 `loop._verify_goal` 里 `output_tail` 的理由）。
NOTE_CHARS = 400


class AbortToken(threading.Event):
    """取消标志。就是一个 `threading.Event` —— 起个名字是为了让「这个 Event
    的含义是取消」在类型与签名上看得见，否则 `Event` 在函数签名里毫无信息量。

    **不提供 `clear()` 的封装、也不阻止调用它**：取消不是可恢复的状态，
    但为一个没人会犯的错加一层守卫是白加的机制。文档说了，就这样。

    **装到引擎上时有一条硬约束**（`QueryEngine.__init__` 的 `abort` 参数）：
    它只由 `AgentWorkers.spawn` 创建、只装在每个 worker 那**一次性**的引擎上。
    常驻 REPL 的引擎跨回合复用，给它装 token 会让后续每一个回合在第一个检查点
    就返回 `aborted`。
    """


@dataclass(frozen=True)
class WorkerOutcome:
    """worker 跑完后交回来的东西。

    `usage` / `reason` 是**可选约定**：给了就进句柄（用量好合并进父会话、
    终止原因好在花名册与结算里显示），不给就是 `None`。管理器对它们做鸭子
    类型，因此不必认识 `Usage` 或 `RunResult`。
    """

    result: object | None = None
    usage: object | None = None
    reason: str | None = None


@dataclass
class AgentHandle:
    """一个 worker 的句柄。**可变**，且它的字段会被 worker 线程改写 ——
    所有读写都必须在本模块那把锁下进行，对外只经 `AgentSnapshot` 交付。
    """

    id: str
    task: str
    status: str = AGENT_RUNNING
    result: object | None = None       # runner 交回的结论对象（内容不解释）
    error: str | None = None           # runner 抛了的话，这里是异常摘要
    usage: object | None = None        # Usage | None —— 被杀/崩掉的报"未知"，不报 0
    reason: str | None = None          # 终局 terminated_reason（"aborted" 在这里）
    started_at: float = 0.0
    finished_at: float | None = None
    #: 结论/状态是否已经被交到模型眼前（`wait`/`close` 交付时置位）。
    #: 结算是靠它区分「跑完了但结论从没被取走」与「已经交过了，无事发生」的。
    reported: bool = False
    #: 用量是否已合并进父会话。**恰好合并一次** —— `wait` 合并它报的那些，
    #: `settle` 合并剩下的，靠这个标志保证两者不会重叠。
    usage_counted: bool = False
    token: AbortToken = field(default_factory=AbortToken, repr=False)
    #: 线程退出信号。等待用它而不是轮询 `status`：轮询总得挑一个间隔，
    #: 挑短了空转、挑长了让 `wait` 白等。
    finished: threading.Event = field(default_factory=threading.Event, repr=False)
    thread: threading.Thread | None = field(default=None, repr=False)

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES


@dataclass(frozen=True)
class AgentSnapshot:
    """句柄的**冻结副本**。对外只给这个：交付一个还能被 worker 线程改写的
    对象，等于把锁的边界泄给调用方。"""

    id: str
    task: str
    status: str
    result: object | None
    error: str | None
    usage: object | None
    reason: str | None
    started_at: float
    finished_at: float | None

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    @property
    def elapsed(self) -> float:
        end = self.finished_at if self.finished_at is not None else time.time()
        return max(0.0, end - self.started_at)


def _snapshot(handle: AgentHandle) -> AgentSnapshot:
    return AgentSnapshot(
        id=handle.id,
        task=handle.task,
        status=handle.status,
        result=handle.result,
        error=handle.error,
        usage=handle.usage,
        reason=handle.reason,
        started_at=handle.started_at,
        finished_at=handle.finished_at,
    )


class TooManyAgents(RuntimeError):
    """并发数已满。文案里带**在跑的花名册** —— 只说"满了"会让模型反复重试
    同一个 spawn，或者干脆放弃；给了 id 它就能先 `wait_agent` 收一个。"""

    def __init__(self, limit: int, running: Sequence[str]) -> None:
        self.limit = limit
        self.running = list(running)
        super().__init__(
            f"并发子代理已达上限 {limit}（仍在跑: {', '.join(self.running)}）。"
            "先用 wait_agent 收结论、或用 close_agent 停掉一个，再 spawn。"
        )


class UnknownAgent(LookupError):
    """id 不认识。**带已知 id 列表**：模型抄错一个 id 时，只回"未知 id"它只能
    猜；给出花名册它下一步就能改对（仿 `session.SessionNotFound` 的 available）。
    """

    def __init__(self, agent_id: str, known_ids: Sequence[str]) -> None:
        self.agent_id = agent_id
        self.known_ids = list(known_ids)
        super().__init__(
            f"未知子代理 {agent_id}；当前已知: {', '.join(self.known_ids) or '（无）'}"
        )


@dataclass(frozen=True)
class SettleReport:
    """回合边界结算的结果。**三种情形分开报**，因为它们的含义完全不同。"""

    killed: tuple[AgentSnapshot, ...] = ()
    unclaimed: tuple[AgentSnapshot, ...] = ()
    still_running: tuple[AgentSnapshot, ...] = ()

    @property
    def any(self) -> bool:
        return bool(self.killed or self.unclaimed or self.still_running)

    def as_event(self) -> dict:
        """翻成 `subagent_settled` 事件的字段。

        **一个地方同时产出机器可读的列表与人读的说明** —— 分成两处迟早会出现
        "轨迹里记了 sa-2、说明里漏了它"。措辞把两种情形**分开**：一种是跑到
        一半被杀（没有结论），一种是跑完了但没人来取（有结论，只是丢了）。
        后者才是常见情形，把结论尾部带上，事后翻轨迹能看出丢了什么。
        """
        notes: list[str] = []
        for snap in self.killed:
            notes.append(f"{snap.id}「{brief_task(snap.task)}」跑到一半被杀，没有结论")
        for snap in self.unclaimed:
            tail = _tail(snap)
            notes.append(
                f"{snap.id}「{brief_task(snap.task)}」跑完了但结论从没被取走"
                + (f"；它的结论尾部: {tail}" if tail else "")
            )
        for snap in self.still_running:
            notes.append(
                f"{snap.id}「{brief_task(snap.task)}」在结算时限内没有停下来，"
                "仍在后台运行（它的用量不计入本回合）"
            )
        return {
            "killed": [s.id for s in self.killed],
            "unclaimed": [s.id for s in self.unclaimed],
            "still_running": [s.id for s in self.still_running],
            "notes": notes,
        }


def brief_task(text: str, limit: int = 60) -> str:
    """把任务描述压成一行短摘要（花名册与结算说明共用）。

    单独抽出来是因为它有两个消费者，而"两处各写一遍截断"迟早会变成
    "花名册里显示的和结算说明里说的不是同一件事"。
    """
    flat = " ".join((text or "").split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def _tail(snap: AgentSnapshot, limit: int = NOTE_CHARS) -> str:
    """结论对象的尾部文本 —— 鸭子类型取 `final_text`，取不到就不编。"""
    text = getattr(snap.result, "final_text", None)
    if not isinstance(text, str) or not text.strip():
        return ""
    flat = text.strip()
    return flat if len(flat) <= limit else "…" + flat[-limit:]


class AgentWorkers:
    """一次 `_run_loop` 生命周期内的子代理管理器。

    **生命周期 = 一个回合。** 每个回合新建一个，回合结束（含被 Ctrl+C 打断）
    由 `settle()` 收尾。所以 worker **活不过它被派出去的那一个回合** —— 这条
    要写进工具 description，否则模型会以为"我派出去它就一直在那儿"。

    线程模型：`spawn` / `list` / `wait` / `close` / `settle` **全部只在父线程
    调用**；worker 线程只改自己那个句柄的字段，改的时候拿同一把锁。`_handles`
    与 `_order` 这两个容器本身因此是父线程独占的（没有别的线程会增删它们），
    锁保护的是句柄的**内容**。
    """

    def __init__(
        self,
        *,
        max_agents: int = MAX_SUB_AGENTS,
        on_usage: Callable[[object], None] | None = None,
    ) -> None:
        self.max_agents = max(1, max_agents)
        #: 合并用量的出口（引擎传 `state.usage += u`）。**只在父线程被调用** ——
        #: `Usage.__iadd__` 是四个字段各一次 read-modify-write，不是原子的：
        #: 在 worker 线程里合并会与主循环的 `state.usage += result.usage` 赛跑，
        #: 在两个 worker 线程里合并会互相丢更新。
        self.on_usage = on_usage
        self._handles: dict[str, AgentHandle] = {}
        self._order: list[str] = []      # 派发顺序（花名册按它排，确定性）
        self._counter = 0
        # RLock：`settle` 内部会再次进入加锁的方法（`_flush_usage` / `_snapshot`），
        # 普通 Lock 会在那里自锁死。
        self._lock = threading.RLock()

    # ---------- 派发 ----------

    def spawn(
        self, task: str, runner: Callable[[AbortToken], object]
    ) -> AgentSnapshot:
        """起一个 worker 并**立刻**返回句柄；满了抛 `TooManyAgents`。

        `runner` 在工作线程里跑，参数是它自己的 `AbortToken`。它返回
        `WorkerOutcome`（推荐）或任意结论对象；**抛异常不丢**，会被记成
        `failed` 并把异常摘要留在句柄上。
        """
        with self._lock:
            running = [
                h.id for h in self._handles.values() if h.status == AGENT_RUNNING
            ]
            if len(running) >= self.max_agents:
                raise TooManyAgents(self.max_agents, running)
            self._counter += 1
            handle = AgentHandle(
                id=f"sa-{self._counter}", task=task, started_at=time.time()
            )
            self._handles[handle.id] = handle
            self._order.append(handle.id)

        thread = threading.Thread(
            target=self._run_worker,
            args=(handle, runner),
            name=f"codeagent-{handle.id}",
            daemon=True,   # 见模块 docstring：非 daemon 会拖住解释器退出
        )
        handle.thread = thread
        thread.start()
        return _snapshot(handle)

    def _run_worker(
        self, handle: AgentHandle, runner: Callable[[AbortToken], object]
    ) -> None:
        """worker 线程主体。**所有出口都必须把 `finished` 置位**，否则
        `wait` 会一直等到超时、`close` 会 join 到一个早已死掉却没人知道的线程。
        """
        status = AGENT_DONE
        result: object | None = None
        usage: object | None = None
        reason: str | None = None
        error: str | None = None
        try:
            outcome = runner(handle.token)
            if isinstance(outcome, WorkerOutcome):
                result, usage, reason = outcome.result, outcome.usage, outcome.reason
            else:
                # 宽容：runner 直接返回结论对象也收下，但没有用量/原因可记 ——
                # 不猜（猜一个 0 用量会让父会话的账目变假）。
                result = outcome
            if handle.token.is_set():
                # 被叫停的记 closed（哪怕它恰好在同一瞬间跑完）：**"叫停"优先**，
                # 因为从人的视角看就是"我让它停，它停了"。
                status = AGENT_CLOSED
        except Exception as exc:
            # 只兜 Exception，不兜 BaseException：`KeyboardInterrupt` 只投递给
            # 主线程，worker 里收到 `SystemExit` 之类应该让它真的退出。
            status = AGENT_FAILED
            error = f"{type(exc).__name__}: {exc}"

        with self._lock:
            handle.status = status
            handle.result = result
            handle.usage = usage
            handle.reason = reason
            handle.error = error
            handle.finished_at = time.time()
            handle.finished.set()

    # ---------- 查询 ----------

    def list(self) -> tuple[AgentSnapshot, ...]:
        """全部句柄（含已终结的），按派发顺序。**已终结的留着**：花名册是模型
        决定下一步的依据，把跑完的抹掉会让 `wait_agent` 的 id 突然"不认识"。"""
        with self._lock:
            return tuple(_snapshot(self._handles[i]) for i in self._order)

    def live_ids(self) -> list[str]:
        """仍在跑的 id（给 REPL 状态位与 `/agents` 用）。"""
        with self._lock:
            return [
                i for i in self._order if self._handles[i].status == AGENT_RUNNING
            ]

    def __len__(self) -> int:
        return len(self._order)

    # ---------- 等待 ----------

    def wait(
        self, ids: Sequence[str] | None = None, *, timeout: float | None = None
    ) -> tuple[AgentSnapshot, ...]:
        """等一组 worker，返回它们**最新**的快照（可能仍有在跑的）。

        `ids` 省略或为空 = 全部**还没交付**的：在跑的，加上"跑完了但结论还没
        交到模型眼前"的。**不能只取在跑的** —— 一次 `[spawn×3, wait]` 里，
        第一个 worker 完全可能在 `wait_agent` 跑起来之前就自己跑完了；只取
        在跑的会让它**被跳过**，模型拿到两份结论、第三份在回合结束时报成
        "从没被取走"。那正是这一项要消灭的静默丢失。

        已交付过的不再返回：结论已经进过上下文，再交一次就是同一条信息
        出现第二次，白占 token 还破坏前缀缓存。

        `timeout` 到了就返回当下状态，**不关闭任何东西** —— 「等够了」和
        「别跑了」是两件事，混在一起会让一个慢 worker 死在一次不耐烦的等待上。

        返回的**终态**句柄会被标记为"已交付"并计入用量；仍在跑的不会被标记
        （结论还没到模型眼前，它当然还可能被 `settle` 报成"没被取走"）。
        """
        handles = self._resolve(ids)
        deadline = None if timeout is None else time.monotonic() + timeout
        for handle in handles:
            if deadline is None:
                handle.finished.wait()
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            handle.finished.wait(remaining)

        with self._lock:
            snapshots = tuple(_snapshot(h) for h in handles)
            for handle in handles:
                if handle.is_terminal:
                    handle.reported = True
            pending = self._take_usage(handles)
        self._emit_usage(pending)
        return snapshots

    def _resolve(self, ids: Sequence[str] | None) -> list[AgentHandle]:
        with self._lock:
            if not ids:
                # 「还没交付」= 在跑的 + 跑完了但没人取过的。判据是 `reported`
                # 而不是 `status`：见 `wait` 的 docstring。
                return [
                    self._handles[i]
                    for i in self._order
                    if not (self._handles[i].is_terminal and self._handles[i].reported)
                ]
            handles = []
            for agent_id in ids:
                handle = self._handles.get(agent_id)
                if handle is None:
                    raise UnknownAgent(agent_id, self._order)
                handles.append(handle)
            return handles

    # ---------- 关闭 ----------

    def close(
        self, agent_id: str, *, timeout: float | None = None
    ) -> AgentSnapshot:
        """abort + **等它真停**（默认一直等到线程退出），返回终态快照。

        幂等：已经终结的句柄直接返回它的终态，不重复做任何事。
        最坏情况要等一次 `llm.chat` 的客户端超时（120s）—— 取消是在**检查点**
        上生效的，不是抢占（见 `QueryEngine` 的 `abort` 参数）。
        """
        with self._lock:
            handle = self._handles.get(agent_id)
            if handle is None:
                raise UnknownAgent(agent_id, self._order)
            handle.token.set()
            thread = handle.thread
        if thread is not None and thread.is_alive():
            thread.join(timeout)
        with self._lock:
            snapshot = _snapshot(handle)
            if handle.is_terminal:
                handle.reported = True
            pending = self._take_usage([handle])
        self._emit_usage(pending)
        return snapshot

    # ---------- 回合边界结算 ----------

    def settle(self, *, timeout: float = SETTLE_TIMEOUT) -> SettleReport:
        """回合结束时的收尾（原版 `settleWorkers`）。**不做的话 worker 会活过
        它的回合、结论无处可去** —— 那是本项目的头号缺陷类（机制在、测试绿、
        没有任何东西路由到它）。

        三件事，一条都不能少：

        1. **没 spawn 过就是免费的**：第一句直接返回。每个普通回合都会走到这里，
           建线程/加锁/记事件都会让"没用子代理的会话"多出一条噪音事件。
        2. **有界 join**（`SETTLE_TIMEOUT`），**与 `close()` 的"等到真停"刻意
           不同**：回合边界不是阻塞在网络调用上的地方；而且它可能正跑在
           `KeyboardInterrupt` 的传播路径上 —— 用户按第二下 Ctrl+C 会在
           `finally` 里再抛，于是一个 worker 都没被 join。**迟迟不停的如实
           报进 `still_running`，不假装清干净了。**
        3. **合并剩下的用量**：`wait` 报过的已经计过，这里只收拾没被取走的。

        `killed`（跑着被杀）与 `unclaimed`（跑完没人取）**分开报**，因为前者
        没有结论、后者有结论只是丢了 —— 混成一句"清理了 N 个"就把信息丢了。
        两者**互斥**：`unclaimed` 只数"在我们动手之前就已经自己跑完"的。
        不排掉的话，一个被叫停的 worker 会在 join 之后变成终态，于是同时进
        `killed` 和 `unclaimed` —— 事件里会写"跑到一半被杀，没有结论"，紧接着
        又写"跑完了但结论从没被取走；它的结论尾部: 子代理已被取消…"。后一句
        是假的，而且它自己打自己的脸。
        """
        if not self._handles:
            return SettleReport()

        with self._lock:
            running = [
                h for h in self._handles.values() if h.status == AGENT_RUNNING
            ]
            for handle in running:
                handle.token.set()

        deadline = time.monotonic() + max(0.0, timeout)
        for handle in running:
            thread = handle.thread
            if thread is None:
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            thread.join(remaining)

        with self._lock:
            killed = tuple(
                _snapshot(h) for h in running if not _thread_alive(h)
            )
            stuck = tuple(
                _snapshot(h) for h in running if _thread_alive(h)
            )
            interrupted = {h.id for h in running}
            unclaimed = tuple(
                _snapshot(h)
                for i in self._order
                for h in (self._handles[i],)
                if h.is_terminal and not h.reported and h.id not in interrupted
            )
            pending = self._take_usage(list(self._handles.values()))
        self._emit_usage(pending)
        return SettleReport(killed=killed, unclaimed=unclaimed, still_running=stuck)

    # ---------- 用量 ----------

    def _take_usage(self, handles: Sequence[AgentHandle]) -> list[object]:
        """取走尚未计入的用量并**当场标记已计**（锁内）。必须与 `_emit_usage`
        分成两步：`on_usage` 是外来回调，不该在我们的锁下被调用。"""
        pending: list[object] = []
        for handle in handles:
            if handle.usage is None or handle.usage_counted:
                continue
            handle.usage_counted = True
            pending.append(handle.usage)
        return pending

    def _emit_usage(self, pending: Sequence[object]) -> None:
        if self.on_usage is None:
            return
        for usage in pending:
            self.on_usage(usage)


def _thread_alive(handle: AgentHandle) -> bool:
    thread = handle.thread
    return thread is not None and thread.is_alive()
