"""工作区快照（M9-8）：把 `write` / `edit` 写过的文件回滚到某一步的样子。

**作用范围**（这一节是模块存在的理由，不是免责声明）

管的是「经 `write` / `edit` 工具**成功写盘**的文件」。这是唯一被登记的通路，
因为它们是唯一**经由门禁链**执行的写路径（`loop._gate_and_run`）：登记点挂在
`tool.run(...)` 成功返回之后，于是「权限拒绝的那次调用压根没执行」「edit 匹配
失败」「工具自己抛了」三种情况是**结构上**走不到登记那一行的，不靠谁记得判断。

**回滚不了、并且必须如实告诉用户的**：
- `bash` 的一切副作用（`rm` / `mv` / 重定向 / `git` 状态 / pytest 生成的缓存目录）；
- 外部工具（MCP）的写入 —— 我们不知道它有没有副作用，因此也不登记它；
- 文件系统元数据（权限位、mtime）与**目录**的增删（快照存的是文件内容，
  不是目录树：被 `bash rm -rf` 删掉的目录不会回来，被 `bash mkdir` 建的也不会消失）；
- 进程外的一切（另一个终端、编辑器、git hook、别的会话）。

**存储布局**

    data/snapshots/objects/{sha[:2]}/{sha}.bin    ← 全局共享的内容寻址对象库
    data/checkpoints/{sid}/ws/step-{K}.json       ← 每步一份的**全量清单**

对象**全局共享**：不同会话改同一个文件的同一份内容只存一份（项目初始文件、
被改回原样的内容都会命中）。清单**按会话隔离**："到第 K 步为止我管过哪些路径"
是会话事实，与检查点同一个坐标系（`--rewind --step K` 与 `--fork --step K`
指的是同一个 K，这一点是结构性的，不靠约定）。

**三条不变式**（每条都有测试钉着）

1. `path ∈ manifest[K]` ⟺ `first_touch(path) ≤ K`。于是清单是**全量**的：
   只要 `K >= first_touch`，还原所需的信息全在 `manifest[K]` 里，不用往前翻任何
   清单。代价是每步多写一份清单（几十行 JSON），换来的是**每一步都能独立还原**
   —— 而"按序重放前像"的方案在中间缺一环时会**静默还原出错**。
2. `base`（"我们碰它之前它长什么样"）只记在**它首次出现那一步**的清单里。
   没有它，"回滚到第一次修改之前"就只能**删掉那个文件** —— 而它可能是仓库里
   人写的、我们并不认识的文件。那是一次静默的数据破坏，是本项目最不能接受的
   一类失败。
3. **写盘顺序：对象 → 清单 → 检查点**（`capture` 整个跑完，`Session._write` 才落
   检查点）。任何一步被杀，留下的都只能是**孤儿**（不可达的字节），不能是
   **说谎的引用**（检查点说有快照、快照却不存在）。

**为什么 `before` 必须由工具报告**：登记发生在工具跑完之后，那时旧内容已经被
覆盖掉了。`EditTool` 的 diff 预览虽然算出了旧内容，但那是对 `errors="replace"`
解出来的文本重新编码，**未必等于盘上的字节**；`WriteTool` 更是一路都不需要旧
内容。所以由工具在写盘**之前**把**原样字节**报告出来（`ToolResult.file_changes`）。

**不需要锁**：对象与清单只由**父线程**写。写工具不是只读工具，走 `_execute_
tool_calls` 的串行分支；而子代理（worker）的引擎 `session=None`，结构上拿不到
本模块 —— 与"worker 写不了检查点"是同一条保证，不另加守卫。

**不做**（理由留痕，见 `docs/design/m9-8_rewind.md` §9）：自动 GC、检查点保留
策略、压缩、增量 diff 链、跨工作区回滚。**对象库的回收只有一条通路**：
`drop_snapshots`（人的显式动作），且它按**所有剩余清单**现算活跃集。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import threading
import time
from dataclasses import dataclass
from pathlib import Path

#: 清单的落盘格式版本。将来改结构时靠它区分，而不是靠"字段在不在"猜。
SCHEMA = 1

#: 清单目录名（`data/checkpoints/{sid}/ws/`）。
MANIFEST_DIRNAME = "ws"

#: 不纳入管辖的顶层目录。`data/` 是我们自己的账本（会话、检查点、对象库、工具
#: 结果、marker），`data/` 里再存一份 `data/` 的快照是自指的；`.git/` 是版本库
#: 的内部状态，回滚它等于绕过 git 自己的语义去改它，比不动它危险得多。
#: 被排除的路径会落进报告里的"不在管辖范围"一栏 —— **不假装管过**。
_EXCLUDED_TOP = ("data", ".git")

#: 已知**没有文件系统副作用**的工具。判据的方向是「默认算有副作用」：新加一个
#: 工具却忘了加进这里，回滚报告会把它算进"不可回滚的动作"（**多报**），而不是
#: 漏报。漏报的后果是用户以为工作区已经干净了 —— 那正是本项最不能接受的失败。
_NO_SIDE_EFFECT_TOOLS = frozenset({
    "read", "glob", "grep",          # 只读
    "write", "edit",                 # 这两个**在回滚范围内**，不算"不可回滚"
    "update_plan", "declare_goal_done", "ask_user",   # 只改 state / 只提问
})


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_bytes(path: Path) -> bytes | None:
    """读文件原样字节；不存在 / 不是文件 / 读不到 → None。

    **一律用字节，不用文本**：哈希必须对得上盘上的字节。若先把内容解码成 str
    再重新编码算哈希，带 BOM 或非 UTF-8 的文件会算出另一个哈希，还原时校验
    就会失败 —— 那是我们自己造出来的"完整性错误"。
    """
    try:
        if not path.is_file():
            return None
        return path.read_bytes()
    except OSError:
        return None


def _skip_dirs() -> frozenset[str]:
    """`GrepTool.SKIP_DIRS` —— **一份定义，不复制**。

    延迟导入是必须的：`agent/tools/base.py` 要 `FileChange` 而 import 本模块，
    所以本模块在**模块级**导入 `agent.tools.files` 会成环
    （workspace → files → base → workspace）。函数内导入时 base 已经加载完了。
    """
    from agent.tools.files import GrepTool

    return GrepTool.SKIP_DIRS


def _load_json(path: Path) -> dict | None:
    """读 JSON 对象；读不到 / 坏了 / 不是对象 → None（调用方各自决定怎么报）。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


class SnapshotError(Exception):
    """快照不可用（这一步没有快照、清单损坏、对象缺失……）。消息要能直接给人看。"""


# ---------- 工具报告的改动 ----------

@dataclass(frozen=True)
class FileChange:
    """一次工具调用对某个文件的改动，由**工具自己**报告（见模块 docstring）。

    `before` 是改动前的**原样字节**；`None` 表示"改动前不存在"。
    """

    path: Path
    before: bytes | None

    #: True = 改动前**存在但我们读不到**（权限 / 被占用）。
    #: 这不是"不存在" —— 把它当成"不存在"，回滚时就会**删掉用户的文件**。
    #: 所以这种路径**不纳入管辖**：宁可回滚不到它，也不能拿它去赌。
    base_unknown: bool = False


# ---------- 报告用的数据结构（都是冻结的只读快照） ----------

ACTION_RESTORE = "restore"
ACTION_DELETE = "delete"
ACTION_SAME = "same"
ACTION_UNRESOLVABLE = "unresolvable"


@dataclass(frozen=True)
class RestoreAction:
    """一个文件在回滚中要做的事。`kind` 取值见上面四个常量。"""

    kind: str
    rel: str
    #: 目标状态的内容哈希（`delete` 时是 None）。还原时用它**再校验一次**对象。
    sha256: str | None = None
    #: 目标状态的字节数（`delete` 时是 0）。
    target_size: int = 0
    #: 盘上现在的字节数；`None` = 现在不存在。
    current_size: int | None = None


@dataclass(frozen=True)
class RestorePlan:
    """回滚预览：**改什么、不改什么、以及哪些后果不在回滚范围内**。"""

    session_id: str
    target_step: int
    actions: tuple[RestoreAction, ...]
    #: 不在管辖范围内的文件数（没被 write/edit 碰过的）。
    outside: int
    #: 回滚区间内的 `bash` 调用次数 —— 它们的副作用**不会**被回滚。
    shell_calls: int
    #: 回滚区间内其余可能带副作用的工具调用次数（外部工具 / 未知工具）。
    other_calls: int

    @property
    def changes(self) -> tuple[RestoreAction, ...]:
        """真正会动盘的那些（排除 `same`）—— 判定"这次回滚有没有事做"用它。"""
        return tuple(a for a in self.actions if a.kind != ACTION_SAME)

    @property
    def unchanged(self) -> tuple[RestoreAction, ...]:
        return tuple(a for a in self.actions if a.kind == ACTION_SAME)


@dataclass(frozen=True)
class RestoreReport:
    """回滚结果。**逐文件记录成败**，不假装清干净了。"""

    plan: RestorePlan
    done: tuple[RestoreAction, ...]
    #: `(相对路径, 失败原因)`。非空就是**有文件没回到位**，必须打出来。
    failures: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class SnapshotUsage:
    """一个会话的快照占用（`--snapshots` 的一行）。"""

    session_id: str
    steps: tuple[int, ...]
    manifest_bytes: int


@dataclass(frozen=True)
class ObjectStoreStats:
    """全局对象库的占用。`orphans` = 不被**任何**清单引用的对象数。

    孤儿来自写盘顺序（对象写完、清单还没写就被杀）。**如实报出，不自动删** ——
    可见但不擅自动手。
    """

    objects: int
    total_bytes: int
    orphans: int
    orphan_bytes: int
    live: int


@dataclass(frozen=True)
class DropResult:
    """`drop_snapshots` 的结果。`kept_shared` 是"因为别的会话还在用而留下"的对象数
    —— 没有这个数，用户会以为"我删了但我看磁盘没怎么变"是我们的 bug。"""

    manifests_removed: int
    objects_removed: int
    bytes_freed: int
    kept_shared: int


# ---------- 路径与对象库 ----------

def objects_root(workspace_root: Path) -> Path:
    """全局对象库根目录：`data/snapshots/objects/`（跨会话共享）。"""
    return Path(workspace_root).resolve() / "data" / "snapshots" / "objects"


def _object_path(workspace_root: Path, sha: str) -> Path:
    return objects_root(workspace_root) / sha[:2] / f"{sha}.bin"


def _put_object(workspace_root: Path, data: bytes) -> str:
    """把内容写进对象库，返回 sha256（已存在则直接复用）。

    原子写（临时文件 + `os.replace`），且临时名带 pid 与线程 id：两个**进程**
    （同一工作区开两个终端）同时写同一个对象时，各写各的临时文件、再原子替换成
    同一个目标名 —— 内容本来就相同，谁赢都一样。没有这一步，一个进程可能读到
    另一个进程写了一半的对象。

    **不加锁**：进程内只有父线程写（见模块 docstring），跨进程靠上面的原子性。

    但"不加锁"必须处理**并发替换本身会失败**这件事：Windows 的 `MoveFileEx`
    在同一目标名被两个线程/进程同时替换时会抛 `ERROR_ACCESS_DENIED`（实测：
    8 线程 × 5 次里有 19 次；Linux 上通常不会）。所以替换失败**先看目标在不在** ——
    在，就说明别人刚刚用同样的字节赢了这一局，对内容寻址的对象库来说这次调用
    是成功的（写的本来就是同一个 sha 的内容）。目标不存在才是真失败（磁盘满、
    只读、权限），如实抛出去。

    这里**不加锁**正是决议 7 的意思：加锁是为了排除一个不存在的问题（内容相同，
    谁赢都一样），而它带来的复杂度（锁的粒度、跨进程锁的文件位置、死锁）是真的。
    """
    sha = _sha256(data)
    path = _object_path(workspace_root, sha)
    if path.exists():
        return sha
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{sha}.bin.tmp-{os.getpid()}-{threading.get_ident()}")
    try:
        tmp.write_bytes(data)
        tmp.replace(path)
    except OSError:
        tmp.unlink(missing_ok=True)
        if not path.exists():
            raise
    return sha


def _get_object(workspace_root: Path, sha: str) -> bytes | None:
    """读对象并**校验哈希**；缺失或损坏 → None。

    校验是刻意的：对象库在工作区内，agent 自己就能 `write` 到那里；磁盘损坏也
    一样。没有这一步，损坏的字节会被**静默写回工作区**，而用户以为文件回到了
    快照的样子。有了它，失败是响的。
    """
    data = _read_bytes(_object_path(workspace_root, sha))
    if data is None or _sha256(data) != sha:
        return None
    return data


def _live_shas(workspace_root: Path) -> set[str]:
    """**所有**会话的清单引用到的对象集合（含 `base` 指向的）。

    活跃集是**现算**的，不存引用计数：存计数就是第二个真相源，迟早与清单不
    一致，而不一致的表现是"某个会话的回滚悄悄失效"。
    """
    live: set[str] = set()
    root = Path(workspace_root).resolve() / "data" / "checkpoints"
    if not root.exists():
        return live
    for manifest in root.glob(f"*/{MANIFEST_DIRNAME}/step-*.json"):
        if manifest.name.endswith(".tmp"):
            continue
        payload = _load_json(manifest)
        if payload is None:
            continue
        for entry in (payload.get("files") or {}).values():
            if not isinstance(entry, dict):
                continue
            if isinstance(entry.get("sha256"), str):
                live.add(entry["sha256"])
            base = entry.get("base")
            if isinstance(base, dict) and isinstance(base.get("sha256"), str):
                live.add(base["sha256"])
    return live


def object_store_stats(workspace_root: Path) -> ObjectStoreStats:
    """全局对象库占用 + 孤儿对象统计（只读）。"""
    live = _live_shas(workspace_root)
    objects = total = orphans = orphan_bytes = 0
    root = objects_root(workspace_root)
    if root.exists():
        for path in root.glob("*/*.bin"):
            try:
                size = path.stat().st_size
            except OSError:
                continue
            objects += 1
            total += size
            if path.stem not in live:
                orphans += 1
                orphan_bytes += size
    return ObjectStoreStats(objects, total, orphans, orphan_bytes, len(live))


def list_snapshot_sessions(workspace_root: Path) -> list[SnapshotUsage]:
    """所有**有工作区快照**的会话（按 session_id 升序）。"""
    root = Path(workspace_root).resolve() / "data" / "checkpoints"
    if not root.exists():
        return []
    out: list[SnapshotUsage] = []
    for session_dir in sorted(root.iterdir()):
        ws_dir = session_dir / MANIFEST_DIRNAME
        if not ws_dir.is_dir():
            continue
        steps = sorted(
            int(p.stem.split("-")[1])
            for p in ws_dir.glob("step-*.json")
            if not p.name.endswith(".tmp")
        )
        if not steps:
            continue
        size = 0
        for p in ws_dir.glob("step-*.json"):
            try:
                size += p.stat().st_size
            except OSError:
                pass
        out.append(SnapshotUsage(session_dir.name, tuple(steps), size))
    return out


def drop_snapshots(workspace_root: Path, session_id: str) -> DropResult:
    """删掉一个会话的快照清单，并回收**不再被任何会话引用**的对象。

    **不能**直接删这个会话用过的对象：对象库是全局共享的，另一个会话的清单可能
    正引用着同一个 sha。所以先删清单，再用 `_live_shas`（现在只剩其它会话了）
    现算活跃集，只删不在里面的。删的对象数会少于这个会话用过的对象数 ——
    `kept_shared` 就是那个差额，**要报出来**，否则用户会以为删了个寂寞。

    只碰 `{session}/ws/` 这一个子目录，**绝不碰检查点本身**：删掉快照只是让
    `--rewind` 对这几步失效（报告里会说清楚），会话本身仍然可以 `--resume`。
    """
    root = Path(workspace_root).resolve() / "data" / "checkpoints" / session_id
    ws_dir = root / MANIFEST_DIRNAME
    if not ws_dir.is_dir():
        raise SnapshotError(f"会话 {session_id} 没有工作区快照")

    used: set[str] = set()
    manifests = 0
    for path in ws_dir.glob("step-*.json"):
        if path.name.endswith(".tmp"):
            continue
        manifests += 1
        payload = _load_json(path)
        if payload is None:
            continue
        for entry in (payload.get("files") or {}).values():
            if not isinstance(entry, dict):
                continue
            if isinstance(entry.get("sha256"), str):
                used.add(entry["sha256"])
            base = entry.get("base")
            if isinstance(base, dict) and isinstance(base.get("sha256"), str):
                used.add(base["sha256"])

    shutil.rmtree(ws_dir)

    live = _live_shas(workspace_root)
    removed = freed = 0
    for sha in sorted(used - live):
        path = _object_path(workspace_root, sha)
        try:
            size = path.stat().st_size
            path.unlink()
        except OSError:
            continue
        removed += 1
        freed += size
    return DropResult(manifests, removed, freed, len(used & live))


# ---------- 会话侧 ----------

class WorkspaceSnapshots:
    """一个会话的工作区快照：登记改动 → 按步落清单 → 回滚。

    生命周期与 `Session` 一致。**只由父线程使用**（见模块 docstring 的"不需要锁"）。
    """

    def __init__(self, workspace_root: Path, session_id: str) -> None:
        self.workspace_root = Path(workspace_root).resolve()
        self.session_id = session_id
        self.manifest_dir = (
            self.workspace_root / "data" / "checkpoints" / session_id / MANIFEST_DIRNAME
        )
        #: rel → base 条目（`{"existed": bool, "sha256": str, "size": int}`）。
        self._managed: dict[str, dict] = {}
        #: 已登记但**还没进任何清单**的路径 → 改动前的原样字节。
        #: 它跨越若干次 capture 存活：文件在第 6 步第一次被写、而清单落在第 10 步
        #: （节拍 5）时，base 必须出现在第 10 步那份清单里 —— 那一份才是它的首触。
        self._pending: dict[str, bytes | None] = {}

    # ---------- 登记 ----------

    def note_write(self, change: FileChange) -> bool:
        """登记一次成功的写盘。返回是否真纳入了管辖。

        **只在工具成功之后调用**（`loop._gate_and_run`），所以"失败/被拒的调用"
        不会走到这里 —— 这是结构性的，不是靠调用方记得判断。
        """
        rel = self._relative(change.path)
        if rel is None or _excluded(rel) or change.base_unknown:
            return False
        if rel not in self._managed and rel not in self._pending:
            # 只在**第一次**记：第二次写同一文件时，`_pending` 里那份才是
            # "我们碰它之前"的样子，覆盖掉它就把 base 变成了"第一次改完的样子"。
            self._pending[rel] = change.before
        return True

    def _relative(self, path: Path) -> str | None:
        try:
            return Path(path).resolve().relative_to(self.workspace_root).as_posix()
        except (OSError, ValueError):
            return None

    # ---------- 落盘 ----------

    def capture(self, step: int) -> Path | None:
        """按当前盘面写第 `step` 步的清单（+它需要的对象）。没管过任何文件 → None。

        **必须由调用方在写检查点之前调用**（写盘顺序：对象 → 清单 → 检查点）。
        """
        if not self._managed and not self._pending:
            return None   # 没碰过任何文件：一个字节都不写（不建目录、不留空文件）

        fresh: dict[str, dict] = {}
        for rel, before in sorted(self._pending.items()):
            base = (
                {"existed": True, "sha256": _put_object(self.workspace_root, before),
                 "size": len(before)}
                if before is not None
                else {"existed": False}
            )
            self._managed[rel] = base
            fresh[rel] = base
        self._pending.clear()

        files: dict[str, dict] = {}
        for rel in sorted(self._managed):
            # **重新读盘**，而不是记住"我们上次写了什么"：清单记的是"第 step 步
            # 时这个文件长什么样"，而 bash 也能改它。记"我们写的内容"会让清单在
            # 这种情况下**说谎**，而回滚的全部价值就在于这份记录可信。
            data = _read_bytes(self.workspace_root / rel)
            entry: dict = (
                {"missing": True} if data is None
                else {"sha256": _put_object(self.workspace_root, data), "size": len(data)}
            )
            if rel in fresh:
                entry["base"] = fresh[rel]
            files[rel] = entry

        return self._write_manifest(step, {
            "schema": SCHEMA,
            "session_id": self.session_id,
            "step": step,
            "ts": time.time(),
            "files": files,
        })

    def _write_manifest(self, step: int, payload: dict) -> Path:
        """原子写清单。与 `Session._write_payload` 同一套做法（临时文件 + replace）。"""
        self.manifest_dir.mkdir(parents=True, exist_ok=True)
        path = self.manifest_path(step)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        tmp.replace(path)
        return path

    def manifest_path(self, step: int) -> Path:
        return self.manifest_dir / f"step-{step}.json"

    def steps(self) -> list[int]:
        """该会话已有的快照步（升序）。坏文件名跳过，不让它把整张表带崩。"""
        if not self.manifest_dir.exists():
            return []
        out: list[int] = []
        for p in self.manifest_dir.glob("step-*.json"):
            if p.name.endswith(".tmp"):
                continue
            try:
                out.append(int(p.stem.split("-")[1]))
            except (IndexError, ValueError):
                continue
        return sorted(out)

    def latest_step(self) -> int | None:
        steps = self.steps()
        return steps[-1] if steps else None

    def read_manifest(self, step: int) -> dict | None:
        """读一份清单的原始 payload（读不到 / 坏了 → None）。

        对外开这个口子，是为了让 `Session._copy_snapshots`（fork 搬清单）不必自己
        去 `json.loads` —— **清单的格式知识只住在本模块**，与检查点格式只住在
        `agent/session.py` 是同一条纪律。别处各解析一遍，改格式时就会漏。
        """
        return _load_json(self.manifest_path(step))

    def import_manifest(self, step: int, payload: dict) -> Path:
        """把别处读来的清单写进来（`Session.fork` 用）。

        对象库是全局共享的，所以**不需要复制任何对象** —— 分叉带过来的清单里的
        sha 在原库仍然可读。这也是"删掉源会话会不会弄坏分叉"的答案：不会，
        `drop_snapshots` 的活跃集是按**所有**清单算的，分叉的清单也算在内。

        `session_id` 改写成自己的：清单里那个字段是给人看的出处，照抄源会话的
        名字就是一份**说谎的记录**（排查时分不清"这份是谁的快照"）。
        """
        return self._write_manifest(step, {**payload, "session_id": self.session_id})

    def usage(self) -> SnapshotUsage:
        size = 0
        for p in self.manifest_dir.glob("step-*.json") if self.manifest_dir.exists() else ():
            try:
                size += p.stat().st_size
            except OSError:
                pass
        return SnapshotUsage(self.session_id, tuple(self.steps()), size)

    # ---------- 规划 ----------

    def _load_manifests(self) -> dict[int, dict]:
        """读全部清单。**坏掉的那份跳过并在调用方报出**（不静默当成"没有"）。"""
        self._broken: list[str] = []
        out: dict[int, dict] = {}
        if not self.manifest_dir.exists():
            return out
        for path in sorted(self.manifest_dir.glob("step-*.json")):
            if path.name.endswith(".tmp"):
                continue
            try:
                step = int(path.stem.split("-")[1])
            except (IndexError, ValueError):
                continue
            payload = _load_json(path)
            if payload is None or not isinstance(payload.get("files"), dict):
                self._broken.append(path.name)
                continue
            out[step] = payload
        return out

    def plan(self, step: int | None = None) -> RestorePlan:
        """算出"回滚到第 step 步"要做的事。**纯读，不动盘。**

        `step=None` → 最近一个有快照的步（**不是**最近检查点：M9-8 之前的会话
        有检查点没有快照，两者不是一回事），即"撤销最后一个快照之后的所有改动"。

        `step=0`（或任何比所有快照都早的步）→ **撤销我们做过的一切**：所有受管
        路径都回到各自的 `base`。这是本机制最常用的那一种用法，所以它必须存在
        —— 见下面那一段注释说的"没有这一支 base 就是不可达的"。

        中间那些没落过快照的步（例如快照在 5 和 10，而你要回到 7）**报错**：
        第 7 步的盘面我们没有任何记录，猜一个出来正是本模块要消灭的东西。
        """
        manifests = self._load_manifests()
        if self._broken:
            raise SnapshotError(
                "快照清单损坏，无法安全回滚（宁可不做，也不猜）："
                + ", ".join(self._broken)
            )
        if not manifests:
            raise SnapshotError(
                f"会话 {self.session_id} 没有工作区快照"
                "（M9-8 之前的会话没有这个机制；也可能是快照被 --drop-snapshots 删过）"
            )
        if step is None:
            step = max(manifests)
        if step not in manifests and step >= min(manifests):
            available = sorted(manifests)
            raise SnapshotError(
                f"第 {step} 步没有工作区快照。可用: {available}"
                "（快照与检查点同一个节拍，所以只有那几步有）"
            )
        # `step` 比**所有**快照都早（含 `--step 0` = "回到我们碰任何东西之前"）：
        # 那时还没有任何受管路径，所以每个路径的答案都是它的 `base`。
        #
        # 这不是猜测，是"`min(manifests)` 就是第一个有受管文件的步"这个事实的直接
        # 推论。**没有这一支，`base` 就是不可达的**：节拍是 5 时第一个清单落在第 5
        # 步，而 `plan(5)` 读的是第 5 步**盘面**（写完之后的），于是"撤销 agent 做的
        # 一切"这件事在最常见的形状下根本表达不出来 —— 记了 base 却没人能用它。
        target = manifests.get(step)

        first: dict[str, int] = {}
        for n in sorted(manifests):
            for rel in manifests[n]["files"]:
                first.setdefault(rel, n)

        actions: list[RestoreAction] = []
        for rel in sorted(first):
            if target is not None and step >= first[rel]:
                # 不变式 1：只要 K >= first_touch，这个路径就一定在 manifest[K] 里。
                entry = target["files"].get(rel)
                if not isinstance(entry, dict):
                    raise SnapshotError(
                        f"快照清单自相矛盾：{rel} 首触于第 {first[rel]} 步，"
                        f"却不在第 {step} 步的清单里（清单可能被手改过）"
                    )
                want = None if entry.get("missing") else entry.get("sha256")
                want_size = 0 if want is None else int(entry.get("size") or 0)
            else:
                # K < first_touch（含"比所有快照都早"）：要的是"我们碰它之前"的样子，
                # 即首触那一步记的 base。
                base = manifests[first[rel]]["files"].get(rel, {}).get("base")
                if not isinstance(base, dict):
                    raise SnapshotError(
                        f"快照清单缺少 {rel} 的 base（首触第 {first[rel]} 步），无法还原"
                    )
                if base.get("existed"):
                    want = base.get("sha256")
                    want_size = int(base.get("size") or 0)
                else:
                    want = None
                    want_size = 0

            actions.append(self._action(rel, want, want_size))

        shell_calls, other_calls = self._count_calls(step)
        return RestorePlan(
            session_id=self.session_id,
            target_step=step,
            actions=tuple(actions),
            outside=_count_outside(self.workspace_root, set(first)),
            shell_calls=shell_calls,
            other_calls=other_calls,
        )

    def _action(self, rel: str, want: str | None, want_size: int) -> RestoreAction:
        path = self._safe_join(rel)
        if path is None:
            # 清单是磁盘上的数据，可能被手改。越界的路径**拒绝**，不是跳过 ——
            # 静默跳过等于让人以为那个文件已经回滚好了。
            return RestoreAction(ACTION_UNRESOLVABLE, rel, None, 0, None)
        current = _read_bytes(path)
        cur_size = None if current is None else len(current)
        if want is None:
            kind = ACTION_DELETE if current is not None else ACTION_SAME
        elif current is None or _sha256(current) != want:
            kind = ACTION_RESTORE
        else:
            kind = ACTION_SAME
        if kind == ACTION_RESTORE and _get_object(self.workspace_root, want) is None:
            # 对象缺失/损坏：**预览里就说**，别等到动手才失败。
            return RestoreAction(ACTION_UNRESOLVABLE, rel, want, want_size, cur_size)
        return RestoreAction(kind, rel, want, want_size, cur_size)

    def _safe_join(self, rel: str) -> Path | None:
        try:
            path = (self.workspace_root / rel).resolve()
            path.relative_to(self.workspace_root)
        except (OSError, ValueError):
            return None
        return path

    def _count_calls(self, target_step: int) -> tuple[int, int]:
        """回滚区间 `(target_step, 最新检查点]` 内的工具调用数：`(bash, 其余可能有副作用的)`。

        数据源是**最新检查点里的 `state.events`**（`dump_state` 落全字段，事件不
        截断）。这条数字是这一项里唯一"可计算的现场事实"—— 不是免责声明。
        """
        latest = self._latest_checkpoint_payload()
        if latest is None:
            return (0, 0)
        from agent.session import state_dict   # 延迟导入：模块级会成环

        events = state_dict(latest).get("events") or []
        shell = other = 0
        for event in events:
            if not isinstance(event, dict) or event.get("type") != "tool_call":
                continue
            try:
                event_step = int(event.get("step") or 0)
            except (TypeError, ValueError):
                continue
            if event_step <= target_step:
                continue
            name = event.get("name")
            if name == "bash":
                shell += 1
            elif name not in _NO_SIDE_EFFECT_TOOLS:
                other += 1
        return (shell, other)

    def _latest_checkpoint_payload(self) -> dict | None:
        session_dir = self.manifest_dir.parent
        steps: list[int] = []
        for p in session_dir.glob("step-*.json"):
            if p.name.endswith(".tmp"):
                continue
            try:
                steps.append(int(p.stem.split("-")[1]))
            except (IndexError, ValueError):
                continue
        if not steps:
            return None
        return _load_json(session_dir / f"step-{max(steps)}.json")

    # ---------- 回滚 ----------

    def restore(self, plan: RestorePlan) -> RestoreReport:
        """执行 `plan`。**逐文件记录成败**，中途失败不假装成功。"""
        done: list[RestoreAction] = []
        failures: list[tuple[str, str]] = []
        for action in plan.actions:
            if action.kind == ACTION_SAME:
                continue
            target = self._safe_join(action.rel)
            if target is None:
                failures.append((action.rel, "清单里的路径越界，拒绝写入"))
                continue
            if action.kind == ACTION_UNRESOLVABLE:
                failures.append((action.rel, "快照对象缺失或已损坏，未改动这个文件"))
                continue
            try:
                if action.kind == ACTION_DELETE:
                    target.unlink(missing_ok=True)
                else:
                    data = _get_object(self.workspace_root, action.sha256 or "")
                    if data is None:
                        failures.append((action.rel, "快照对象缺失或已损坏，未改动这个文件"))
                        continue
                    target.parent.mkdir(parents=True, exist_ok=True)
                    # 原子写：中途被杀不会留下半个文件（半个文件比旧文件更糟）
                    tmp = target.with_name(f"{target.name}.rewind-{os.getpid()}")
                    tmp.write_bytes(data)
                    tmp.replace(target)
            except OSError as exc:
                failures.append((action.rel, f"{type(exc).__name__}: {exc}"))
                continue
            done.append(action)
        return RestoreReport(plan, tuple(done), tuple(failures))


# ---------- 报告里那个"不在管辖范围"的数字 ----------

def _excluded(rel: str) -> bool:
    return rel.split("/", 1)[0] in _EXCLUDED_TOP


def _count_outside(workspace_root: Path, managed: set[str]) -> int:
    """数出"这次回滚**没管**的文件"。

    口径写死在这里：工作区下递归，跳过 `GrepTool.SKIP_DIRS`（与 grep 共用一份
    定义）与点开头目录，再减去受管集合。**口径含糊的计数比不报还糟** —— 所以
    它跟着"不在管辖范围"这一栏一起印出来，而不是单独一个裸数字。

    这个数**同时**是安全属性（不误删人写的文件）和能力边界（`bash` 建的文件
    落在这里）的体现，所以它不是装饰。
    """
    skip = _skip_dirs()
    count = 0
    for dirpath, dirnames, filenames in os.walk(workspace_root):
        dirnames[:] = [d for d in dirnames if d not in skip and not d.startswith(".")]
        for name in filenames:
            full = Path(dirpath) / name
            try:
                rel = full.relative_to(workspace_root).as_posix()
            except ValueError:
                continue
            if rel not in managed:
                count += 1
    return count
