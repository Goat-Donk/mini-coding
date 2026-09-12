"""工作区快照（M9-8）单测。

分五组，与 `docs/design/m9-8_rewind.md` §7 对应：
1. **登记**：只有成功的 write/edit 会被登记（`data/` 与读不到原内容的路径不纳入）；
2. **落盘**：base 只记一次、对象去重、写盘顺序（对象 → 清单）；
3. **恢复**：K ≥ 首触用清单、K < 首触用 base、`base.existed=False` → 删除、
   管辖之外的文件一个都不动、对象损坏要**报错而不是写回**；
4. **坐标**：这一步没有快照要报出可用步数（不能只甩一句"找不到"）；
5. **共享对象库**：跨会话去重、`drop_snapshots` 只回收没人引用的对象；
6. **静默崩溃**：写盘顺序（对象 → 清单 → 检查点）四个被杀点各自留下的痕迹；
7. **并发写对象库**：同一份内容被多线程同时写只留一份、且字节完整。
"""
from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from agent.workspace import (
    ACTION_DELETE,
    ACTION_RESTORE,
    ACTION_SAME,
    ACTION_UNRESOLVABLE,
    FileChange,
    SnapshotError,
    WorkspaceSnapshots,
    _get_object,
    _put_object,
    drop_snapshots,
    list_snapshot_sessions,
    object_store_stats,
)


def apply_write(root: Path, rel: str, text: str) -> FileChange:
    """模拟 `write` 工具的一次覆盖写：**先读原样字节**，再写，返回工具会报告的那份。

    顺序是重点 —— 真实工具也必须这样（先读后写），否则 `base`（"我们碰它之前
    它长什么样"）就永远拿不到了。
    """
    path = root / rel
    before = path.read_bytes() if path.is_file() else None
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return FileChange(path=path, before=before)


def apply_edit(root: Path, rel: str, text: str) -> FileChange:
    """模拟 `edit`：同样是"读原样字节 → 替换 → 写回"。"""
    return apply_write(root, rel, text)


def snap(root: Path, sid: str = "s1") -> WorkspaceSnapshots:
    return WorkspaceSnapshots(root, sid)


def read_manifest(snaps: WorkspaceSnapshots, step: int) -> dict:
    return json.loads(snaps.manifest_path(step).read_text(encoding="utf-8"))


def write_fake_checkpoint(root: Path, sid: str, step: int, events: list[dict]) -> None:
    """写一份够用的检查点（只为 `_count_calls` 读 `state.events`）。"""
    path = root / "data" / "checkpoints" / sid / f"step-{step}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"session_id": sid, "step": step, "state": {"events": events}}),
        encoding="utf-8",
    )


# ---------- 1. 登记 ----------

def test_note_write_records_path_and_base(tmp_path):
    (tmp_path / "a.txt").write_text("v0", encoding="utf-8")
    snaps = snap(tmp_path)
    assert snaps.note_write(apply_write(tmp_path, "a.txt", "v1")) is True
    snaps.capture(5)
    entry = read_manifest(snaps, 5)["files"]["a.txt"]
    assert entry["base"]["existed"] is True
    assert entry["size"] == 2          # "v1"


def test_note_write_new_file_base_says_not_existed(tmp_path):
    snaps = snap(tmp_path)
    snaps.note_write(apply_write(tmp_path, "new.txt", "hi"))
    snaps.capture(5)
    base = read_manifest(snaps, 5)["files"]["new.txt"]["base"]
    # 「之前不存在」与「之前是空的」是两件不同的事，必须分开记 ——
    # 记混了，回滚时就会把用户的空文件删掉、或者把该删的新文件留成空文件。
    assert base == {"existed": False}


def test_data_dir_is_out_of_jurisdiction(tmp_path):
    """`data/` 是我们自己的账本，快照它等于自指；`.git/` 同理。"""
    snaps = snap(tmp_path)
    (tmp_path / "data").mkdir()
    assert snaps.note_write(apply_write(tmp_path, "data/tests_pass.marker", "x")) is False
    (tmp_path / ".git").mkdir()
    assert snaps.note_write(apply_write(tmp_path, ".git/config", "y")) is False
    assert snaps.capture(5) is None      # 一条都没登记 → 一个字节都不写


def test_unreadable_before_keeps_path_out_of_jurisdiction(tmp_path):
    """读不到原内容时**不纳入管辖**：宁可回滚不到它，也不能拿"读不到"当"不存在"。"""
    snaps = snap(tmp_path)
    path = tmp_path / "locked.txt"
    path.write_text("v0", encoding="utf-8")
    change = FileChange(path=path, before=None, base_unknown=True)
    assert snaps.note_write(change) is False
    assert snaps.capture(5) is None


def test_path_outside_workspace_is_refused(tmp_path):
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("x", encoding="utf-8")
    snaps = snap(tmp_path)
    assert snaps.note_write(FileChange(path=outside, before=b"x")) is False


# ---------- 2. 落盘 ----------

def test_base_is_recorded_only_in_first_touch_manifest(tmp_path):
    (tmp_path / "a.txt").write_text("v0", encoding="utf-8")
    snaps = snap(tmp_path)
    snaps.note_write(apply_write(tmp_path, "a.txt", "v1"))
    snaps.capture(5)
    snaps.note_write(apply_write(tmp_path, "a.txt", "v2"))
    snaps.capture(10)
    assert read_manifest(snaps, 5)["files"]["a.txt"]["base"]["existed"] is True
    # 第二次改同一文件**不再覆盖 base**，也不在后面的清单里重复记一份
    # （不变式 2：base 只在它首次出现那一步的清单里）。
    assert "base" not in read_manifest(snaps, 10)["files"]["a.txt"]


def test_second_write_before_first_capture_keeps_original_base(tmp_path):
    """节拍是 5：文件在第 6 步第一次被写、清单落在第 10 步 —— base 必须是**第 6 步之前**那份。"""
    (tmp_path / "a.txt").write_text("v0", encoding="utf-8")
    snaps = snap(tmp_path)
    snaps.note_write(apply_write(tmp_path, "a.txt", "v1"))
    snaps.note_write(apply_write(tmp_path, "a.txt", "v2"))   # 同一步区间内的第二次写
    snaps.capture(10)
    base = read_manifest(snaps, 10)["files"]["a.txt"]["base"]
    assert base["existed"] is True
    # 比的是**内容**，不是 size：`size` 在这里分不出差别（"v0" 与 "v1" 都是 2 字节），
    # 而一个分不出差别的断言等于没有断言 —— 变异测试实测过（把"只记第一次"改成
    # "每次都覆盖"之后，只比 size 的那版**照样全绿**）。
    assert _get_object(tmp_path, base["sha256"]) == b"v0"


def test_unchanged_file_does_not_write_a_new_object(tmp_path):
    (tmp_path / "a.txt").write_text("v0", encoding="utf-8")
    snaps = snap(tmp_path)
    snaps.note_write(apply_write(tmp_path, "a.txt", "v1"))
    snaps.capture(5)
    before = object_store_stats(tmp_path).objects
    snaps.capture(10)                       # 中间没人动过它
    assert object_store_stats(tmp_path).objects == before


def test_capture_writes_objects_before_manifest(tmp_path, monkeypatch):
    """**写盘顺序不变式**：清单写失败时，盘上只留孤儿对象，不留"说谎的引用"。"""
    snaps = snap(tmp_path)
    snaps.note_write(apply_write(tmp_path, "a.txt", "hello"))

    def boom(step, payload):
        raise OSError("模拟写到一半被杀")

    monkeypatch.setattr(snaps, "_write_manifest", boom)
    with pytest.raises(OSError):
        snaps.capture(5)
    assert not snaps.manifest_path(5).exists()
    stats = object_store_stats(tmp_path)
    assert stats.objects == 1 and stats.orphans == 1   # 孤儿：可见，但不自动删


def test_manifest_write_never_leaves_a_partial_target(tmp_path, monkeypatch):
    """清单是**临时文件 + 原子替换**写的：写到一半被杀，目标名从不出现。

    直接写目标名的话，一次中途失败就会在盘上留下半个 `step-5.json`，而它会被
    `plan()` 当成一份完整清单读进去（或者被当成"损坏"而让整次回滚拒绝）——
    两种结果都比"这一步没有快照"糟。这条钉的是**写侧**；`test_torn_manifest_tmp_is_invisible`
    钉的是读侧（半成品不冒充完整清单），两侧合起来才是不变式。
    """
    snaps = snap(tmp_path)
    snaps.note_write(apply_write(tmp_path, "a.txt", "v1"))
    real = Path.write_text

    def half(self, data, **kwargs):          # noqa: ANN001 - 替身签名
        real(self, data[: len(data) // 2], **kwargs)   # 先落半个，再炸
        raise OSError("模拟写到一半被杀")

    monkeypatch.setattr(Path, "write_text", half)
    with pytest.raises(OSError):
        snaps.capture(5)

    assert not snaps.manifest_path(5).exists()   # 原子版：目标名一次都没出现过
    assert snaps.steps() == []


def test_capture_with_no_writes_writes_nothing_at_all(tmp_path):
    """没碰过任何文件 → 不建目录、不写空文件（普通会话不该为这个机制付代价）。"""
    snaps = snap(tmp_path)
    assert snaps.capture(5) is None
    assert not snaps.manifest_dir.exists()
    assert object_store_stats(tmp_path).objects == 0


def test_identical_content_dedups_to_one_object(tmp_path):
    (tmp_path / "a.txt").write_text("same", encoding="utf-8")
    (tmp_path / "b.txt").write_text("same", encoding="utf-8")
    snaps = snap(tmp_path)
    snaps.note_write(apply_write(tmp_path, "a.txt", "same"))
    snaps.note_write(apply_write(tmp_path, "b.txt", "same"))
    snaps.capture(5)
    files = read_manifest(snaps, 5)["files"]
    assert files["a.txt"]["sha256"] == files["b.txt"]["sha256"]
    assert object_store_stats(tmp_path).objects == 1


# ---------- 3. 恢复 ----------

def _two_step_session(tmp_path: Path, sid: str = "s1") -> WorkspaceSnapshots:
    """第 5 步改了 keep.txt，第 10 步才第一次碰 a.txt —— 于是 a.txt 有 base 可用。"""
    (tmp_path / "a.txt").write_text("v0", encoding="utf-8")
    (tmp_path / "keep.txt").write_text("k0", encoding="utf-8")
    snaps = snap(tmp_path, sid)
    snaps.note_write(apply_write(tmp_path, "keep.txt", "k1"))
    snaps.capture(5)
    snaps.note_write(apply_write(tmp_path, "a.txt", "v1"))
    snaps.capture(10)
    return snaps


def test_rewind_to_step_after_first_touch_uses_manifest(tmp_path):
    snaps = _two_step_session(tmp_path)
    plan = snaps.plan(10)
    keep = next(a for a in plan.actions if a.rel == "keep.txt")
    assert keep.kind == ACTION_SAME      # 第 10 步时它就是 k1，现在也是 k1
    report = snaps.restore(plan)
    assert report.failures == ()


def test_rewind_to_step_before_first_touch_restores_base(tmp_path):
    """★ 本机制存在的主要用例：撤销"我们碰它之前"之后的所有改动。"""
    snaps = _two_step_session(tmp_path)
    plan = snaps.plan(5)
    a = next(a for a in plan.actions if a.rel == "a.txt")
    assert a.kind == ACTION_RESTORE
    report = snaps.restore(plan)
    assert report.failures == ()
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "v0"
    assert (tmp_path / "keep.txt").read_text(encoding="utf-8") == "k1"


def test_new_file_is_deleted_when_rewinding_before_it_existed(tmp_path):
    """第 10 步才第一次被碰的文件，回滚到第 5 步 → 它当时根本不存在 → 删掉。

    这是 `base = {"existed": False}` 的唯一用途，也是"记录之前不存在"与
    "记录之前是空的"必须分开的现场。
    """
    (tmp_path / "other.txt").write_text("o0", encoding="utf-8")
    snaps = snap(tmp_path)
    snaps.note_write(apply_write(tmp_path, "other.txt", "o1"))
    snaps.capture(5)                                   # 第 5 步还没有 made.txt
    snaps.note_write(apply_write(tmp_path, "made.txt", "x"))
    snaps.capture(10)
    plan = snaps.plan(5)
    assert next(a for a in plan.actions if a.rel == "made.txt").kind == ACTION_DELETE
    snaps.restore(plan)
    assert not (tmp_path / "made.txt").exists()
    assert (tmp_path / "other.txt").read_text(encoding="utf-8") == "o1"


def test_files_outside_jurisdiction_are_never_touched(tmp_path):
    """**管辖之外的文件一个都不动** —— 这同时是安全属性与能力边界。"""
    snaps = _two_step_session(tmp_path)
    (tmp_path / "human.txt").write_text("人写的", encoding="utf-8")
    (tmp_path / "bash_made.txt").write_text("bash 建的", encoding="utf-8")
    snaps.restore(snaps.plan(5))
    assert (tmp_path / "human.txt").read_text(encoding="utf-8") == "人写的"
    assert (tmp_path / "bash_made.txt").read_text(encoding="utf-8") == "bash 建的"


def test_unchanged_files_are_not_rewritten(tmp_path):
    """未变的文件不重写：mtime 不动，也不产生"其实没变"的写盘。"""
    snaps = _two_step_session(tmp_path)
    keep = tmp_path / "keep.txt"
    stamp = keep.stat().st_mtime_ns
    snaps.restore(snaps.plan(10))
    assert keep.stat().st_mtime_ns == stamp


def test_missing_object_reports_failure_instead_of_writing_garbage(tmp_path):
    """对象丢了 → **报错，不写回**。写回一段损坏内容比失败糟得多。"""
    snaps = _two_step_session(tmp_path)
    plan = snaps.plan(5)
    a = next(x for x in plan.actions if x.rel == "a.txt")
    object_file = tmp_path / "data" / "snapshots" / "objects" / a.sha256[:2] / f"{a.sha256}.bin"
    object_file.unlink()
    # 预览阶段就说清楚，而不是等人按了 --force 才失败
    assert next(x for x in snaps.plan(5).actions if x.rel == "a.txt").kind == ACTION_UNRESOLVABLE
    report = snaps.restore(plan)
    assert [rel for rel, _ in report.failures] == ["a.txt"]
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "v1"   # 原文未动


def test_corrupted_object_is_detected_by_hash(tmp_path):
    """对象被改写（agent 自己能 write 到那里 / 磁盘损坏）→ 哈希校验接住。"""
    snaps = _two_step_session(tmp_path)
    plan = snaps.plan(5)
    a = next(x for x in plan.actions if x.rel == "a.txt")
    object_file = tmp_path / "data" / "snapshots" / "objects" / a.sha256[:2] / f"{a.sha256}.bin"
    object_file.write_bytes(b"tampered")
    report = snaps.restore(plan)
    assert [rel for rel, _ in report.failures] == ["a.txt"]
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "v1"


def test_restore_reports_per_file_outcome(tmp_path):
    """`done` 只装**真正动过盘**的文件 —— keep.txt 在第 5 步就是 k1，不该出现在这里。"""
    snaps = _two_step_session(tmp_path)
    report = snaps.restore(snaps.plan(5))
    assert [a.rel for a in report.done] == ["a.txt"]
    assert report.failures == ()


def test_manifest_with_escaping_path_is_refused(tmp_path):
    """清单是磁盘上的数据、可能被手改：里面的 `../` 必须被拒绝，且**不能静默跳过**。"""
    snaps = _two_step_session(tmp_path)
    manifest = read_manifest(snaps, 5)
    manifest["files"]["../escaped.txt"] = manifest["files"]["keep.txt"]
    snaps.manifest_path(5).write_text(json.dumps(manifest), encoding="utf-8")
    plan = snaps.plan(5)
    escaped = next(a for a in plan.actions if a.rel == "../escaped.txt")
    assert escaped.kind == ACTION_UNRESOLVABLE
    report = snaps.restore(plan)
    assert [rel for rel, _ in report.failures] == ["../escaped.txt"]
    assert not (tmp_path.parent / "escaped.txt").exists()


# ---------- 4. 坐标 ----------

def test_plan_without_step_uses_latest_manifest(tmp_path):
    snaps = _two_step_session(tmp_path)
    assert snaps.plan().target_step == 10


def test_step_with_checkpoint_but_no_snapshot_lists_available(tmp_path):
    """报错必须给出**下一步** —— 只说"找不到"等于把用户扔在原地。"""
    snaps = _two_step_session(tmp_path)
    with pytest.raises(SnapshotError) as exc:
        snaps.plan(7)
    assert "第 7 步没有工作区快照" in str(exc.value)
    assert "[5, 10]" in str(exc.value)


def test_session_without_any_snapshot_says_so(tmp_path):
    """M9-8 之前的会话（有检查点、没有 `ws/`）走这一支，且是人话不是 traceback。"""
    snaps = snap(tmp_path, "old")
    with pytest.raises(SnapshotError) as exc:
        snaps.plan()
    assert "没有工作区快照" in str(exc.value)


def test_corrupted_manifest_refuses_to_guess(tmp_path):
    snaps = _two_step_session(tmp_path)
    snaps.manifest_path(5).write_text("{ 不是 json", encoding="utf-8")
    with pytest.raises(SnapshotError) as exc:
        snaps.plan(10)
    assert "损坏" in str(exc.value)


def test_steps_and_latest_step(tmp_path):
    snaps = _two_step_session(tmp_path)
    assert snaps.steps() == [5, 10]
    assert snaps.latest_step() == 10


def test_rewind_to_zero_undoes_everything(tmp_path):
    """★ `--step 0` = "回到我们碰任何东西之前" —— 本机制最常用的那一种用法。

    没有这一支，`base` 就是**不可达的**：节拍是 5 时第一个清单落在第 5 步，而
    `plan(5)` 读的是第 5 步的**盘面**（写完之后的），于是"撤销 agent 做的一切"
    这件事在最常见的形状下根本表达不出来 —— 记了 base 却没人能用它。
    """
    (tmp_path / "a.txt").write_text("v0", encoding="utf-8")      # 用户原有的文件
    snaps = snap(tmp_path)
    snaps.note_write(apply_write(tmp_path, "a.txt", "v1"))       # agent 改了它
    snaps.note_write(apply_write(tmp_path, "made.txt", "新"))    # agent 新建了它
    snaps.capture(5)                                            # 第一个快照在第 5 步

    assert snaps.steps() == [5]
    plan = snaps.plan(0)
    kinds = {a.rel: a.kind for a in plan.actions}
    assert kinds == {"a.txt": ACTION_RESTORE, "made.txt": ACTION_DELETE}

    assert snaps.restore(plan).failures == ()
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "v0"
    assert not (tmp_path / "made.txt").exists()


def test_rewind_to_a_step_before_any_touch_is_not_an_error(tmp_path):
    """比所有快照都早的**任意**步都一样（不只是 0）：那时我们还没碰过任何东西。"""
    (tmp_path / "a.txt").write_text("v0", encoding="utf-8")
    snaps = snap(tmp_path)
    snaps.note_write(apply_write(tmp_path, "a.txt", "v1"))
    snaps.capture(5)
    assert next(a for a in snaps.plan(3).actions if a.rel == "a.txt").kind == ACTION_RESTORE


def test_rewind_to_an_unjournaled_step_in_the_middle_still_errors(tmp_path):
    """但夹在两个快照**中间**的步仍然报错 —— 那一步的盘面我们没有任何记录。

    这正是"比所有快照都早"与"夹在中间"必须分开判的原因：前者有事实依据
    （那时什么都没受管），后者只能说"不知道"，而猜一个出来是本模块要消灭的东西。
    """
    snaps = _two_step_session(tmp_path)
    with pytest.raises(SnapshotError) as exc:
        snaps.plan(7)
    assert "第 7 步没有工作区快照" in str(exc.value)


# ---------- 5. 预览里的三个数字（可计算的现场事实，不是免责声明） ----------

def test_preview_counts_shell_calls_in_the_rewound_range(tmp_path):
    snaps = _two_step_session(tmp_path)
    write_fake_checkpoint(tmp_path, "s1", 10, [
        {"type": "tool_call", "name": "bash", "step": 6},
        {"type": "tool_call", "name": "bash", "step": 9},
        {"type": "tool_call", "name": "bash", "step": 3},    # 区间**之前** → 不算
        # 边界：第 5 步 = 回滚目标步本身。回滚区间是 `(target, 最新]`，**左开** ——
        # 目标步自己的副作用已经体现在"第 5 步的盘面"里了（`capture(5)` 记的是第 5
        # 步跑完**之后**的盘面），所以它不需要被警告。这一条数据就是用来钉这个边界的：
        # 少了它，把 `<=` 写成 `<` 也照样全绿（实测过）。
        {"type": "tool_call", "name": "bash", "step": 5},
        {"type": "tool_call", "name": "read", "step": 7},    # 只读 → 不算
        {"type": "tool_call", "name": "write", "step": 8},   # 在回滚范围内 → 不算
        {"type": "tool_call", "name": "mcp__x__write", "step": 8},   # 外部 → 算
    ])
    plan = snaps.plan(5)
    assert plan.shell_calls == 2
    assert plan.other_calls == 1


def test_unknown_tool_defaults_to_counted_as_side_effecting(tmp_path):
    """判据方向是"默认算有副作用" —— 多报可以，漏报不行（漏报 = 用户以为干净了）。"""
    snaps = _two_step_session(tmp_path)
    write_fake_checkpoint(tmp_path, "s1", 10, [
        {"type": "tool_call", "name": "some_future_tool", "step": 8},
    ])
    assert snaps.plan(5).other_calls == 1


def test_preview_counts_files_outside_jurisdiction(tmp_path):
    snaps = _two_step_session(tmp_path)
    (tmp_path / "human1.txt").write_text("a", encoding="utf-8")
    (tmp_path / "human2.txt").write_text("b", encoding="utf-8")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "nested.txt").write_text("c", encoding="utf-8")
    (tmp_path / "data").mkdir(exist_ok=True)
    (tmp_path / "data" / "x.json").write_text("{}", encoding="utf-8")
    plan = snaps.plan(5)
    # a.txt / keep.txt 受管；human1/2 + sub/nested 不在管辖；data/ 与 .git/ 被跳过
    assert plan.outside == 3


# ---------- 6. 共享对象库 ----------

def test_objects_are_shared_across_sessions(tmp_path):
    (tmp_path / "a.txt").write_text("v0", encoding="utf-8")
    a, b = snap(tmp_path, "sa"), snap(tmp_path, "sb")
    for s in (a, b):
        s.note_write(apply_write(tmp_path, "a.txt", "v1"))
        s.capture(5)
    assert object_store_stats(tmp_path).objects == 2   # v0 与 v1，各一份


def test_drop_keeps_objects_another_session_still_references(tmp_path):
    """★ 跨会话共享对象库的**核心安全属性**：删 A 的清单绝不能弄坏 B 的回滚。

    共享是这么真实发生的：A 把 `a.txt` 从 v0 改成 v1 并落了快照；B 随后接手，
    它的 `base`（"我碰它之前长什么样"）就是 v1 —— 与 A 那份"第 5 步盘面"
    是同一份字节，在对象库里就是**同一个对象**。

    所以 `drop_snapshots` 不能"删掉这个会话用过的所有对象"，只能删**现算活跃集
    之外**的：A 的 base（v0）从此没人要了，而 v1 必须留下。
    """
    (tmp_path / "a.txt").write_text("v0", encoding="utf-8")
    (tmp_path / "other.txt").write_text("o0", encoding="utf-8")
    a, b = snap(tmp_path, "sa"), snap(tmp_path, "sb")

    a.note_write(apply_write(tmp_path, "a.txt", "v1"))
    a.capture(5)

    b.note_write(apply_write(tmp_path, "other.txt", "o1"))
    b.capture(5)
    b.note_write(apply_write(tmp_path, "a.txt", "v2"))   # B 接手时盘上是 v1
    b.capture(10)

    assert object_store_stats(tmp_path).objects == 5     # v0 v1 o0 o1 v2

    result = drop_snapshots(tmp_path, "sa")
    assert result.manifests_removed == 1
    assert result.objects_removed == 1        # 只有 v0 从此没人要了
    assert result.kept_shared == 1            # v1 被 B 的 base 引用着 → 必须留下
    assert object_store_stats(tmp_path).objects == 4

    # ★ B 仍能回滚，而它读回的那份字节正是 A 留下的对象
    plan = b.plan(5)                          # a.txt 首触于第 10 步 → 走 base
    assert next(x for x in plan.actions if x.rel == "a.txt").kind == ACTION_RESTORE
    report = b.restore(plan)
    assert report.failures == ()
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "v1"


def test_drop_reclaims_objects_nobody_references(tmp_path):
    (tmp_path / "a.txt").write_text("v0", encoding="utf-8")
    a = snap(tmp_path, "sa")
    a.note_write(apply_write(tmp_path, "a.txt", "v1"))
    a.capture(5)
    assert object_store_stats(tmp_path).objects == 2

    result = drop_snapshots(tmp_path, "sa")
    assert result.objects_removed == 2
    assert result.bytes_freed == len("v0") + len("v1")
    assert object_store_stats(tmp_path).objects == 0


def test_drop_does_not_touch_checkpoints(tmp_path):
    """删快照只让 `--rewind` 对这几步失效，会话本身必须还能 `--resume`。"""
    (tmp_path / "a.txt").write_text("v0", encoding="utf-8")
    snaps = snap(tmp_path, "sa")
    snaps.note_write(apply_write(tmp_path, "a.txt", "v1"))
    snaps.capture(5)
    (tmp_path / "data" / "checkpoints" / "sa" / "step-5.json").write_text(
        "{}", encoding="utf-8"
    )
    drop_snapshots(tmp_path, "sa")
    assert (tmp_path / "data" / "checkpoints" / "sa" / "step-5.json").exists()
    assert not snaps.manifest_dir.exists()


def test_drop_unknown_session_is_an_error(tmp_path):
    with pytest.raises(SnapshotError):
        drop_snapshots(tmp_path, "nope")


def test_orphans_are_reported_not_removed(tmp_path):
    """孤儿对象如实报出、**不自动删**（"会自动删用户数据的机制在没有真实需求之前不该存在"）。"""
    snaps = snap(tmp_path)
    snaps.note_write(apply_write(tmp_path, "a.txt", "hello"))
    snaps.capture(5)
    stats = object_store_stats(tmp_path)
    assert (stats.objects, stats.orphans) == (1, 0)   # 有清单引用它，不是孤儿

    snaps.manifest_path(5).unlink()                   # 模拟"对象写完、清单没了"
    stats = object_store_stats(tmp_path)
    assert (stats.objects, stats.orphans) == (1, 1)   # 变孤儿，但对象还在
    assert stats.orphan_bytes == stats.total_bytes


def test_list_snapshot_sessions(tmp_path):
    (tmp_path / "a.txt").write_text("v0", encoding="utf-8")
    for sid in ("sa", "sb"):
        s = snap(tmp_path, sid)
        s.note_write(apply_write(tmp_path, "a.txt", "v1"))
        s.capture(5)
    usages = list_snapshot_sessions(tmp_path)
    assert [u.session_id for u in usages] == ["sa", "sb"]
    assert usages[0].steps == (5,)
    assert usages[0].manifest_bytes > 0


def test_import_manifest_copies_without_copying_objects(tmp_path):
    """fork 搬清单即可 —— 对象库全局共享，不需要复制任何对象。"""
    (tmp_path / "a.txt").write_text("v0", encoding="utf-8")
    src = snap(tmp_path, "src")
    src.note_write(apply_write(tmp_path, "a.txt", "v1"))
    src.capture(5)
    objects_before = object_store_stats(tmp_path).objects

    forked = snap(tmp_path, "fork")
    forked.import_manifest(5, read_manifest(src, 5))
    assert object_store_stats(tmp_path).objects == objects_before   # 没多存一份
    assert forked.steps() == [5]
    # 搬过来的清单要认自己的会话，不能照抄源会话的名字（那是说谎的记录）
    assert read_manifest(forked, 5)["session_id"] == "fork"
    # 分叉出来的会话自己就能回滚
    report = forked.restore(forked.plan(5))
    assert report.failures == ()
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "v1"


# ---------- 7. 静默崩溃：写盘顺序的四个被杀点 ----------
#
# 契约（模块 docstring 不变式 3）：对象 → 清单 → 检查点。任何一步被杀，盘上只允许
# 出现**孤儿**（不可达的字节），不允许出现**说谎的引用**（说"这里有快照"却读不到）。
# 这几个测试逐个钉住每个被杀点留下的**具体痕迹**。
#
# 之前那个 `test_capture_writes_objects_before_manifest` 钉的是被杀点 ①。

def test_capture_survives_a_crash_between_manifest_and_checkpoint(tmp_path):
    """被杀点 ②：清单写完、检查点没写。

    清单是**自足**的（每步全量），所以这一步照样回滚得动 —— 这正是"清单全量"
    换来的东西。反过来（检查点有、清单没有）在结构上不可能：**没有任何一个
    检查点字段记录"这里应该有快照"**，快照的有无就是 `ws/` 目录本身。
    """
    snaps = _two_step_session(tmp_path)
    assert not (tmp_path / "data" / "checkpoints" / "s1" / "step-10.json").exists()
    report = snaps.restore(snaps.plan(10))       # 一个检查点都没写也照样能规划
    assert report.failures == ()
    assert snaps.steps() == [5, 10]


def test_half_written_object_is_not_counted_and_reads_as_missing(tmp_path):
    """被杀点 ③：对象只写了一半（临时文件还在，目标名还没出现）。

    临时名不以 `.bin` 结尾，不该被当成对象（否则占用统计会虚高、孤儿数会乱）；
    这个 sha 读回来是 None → 预览里如实报 UNRESOLVABLE，而**不是把半个文件
    写回工作区**。
    """
    snaps = _two_step_session(tmp_path)
    a = next(x for x in snaps.plan(5).actions if x.rel == "a.txt")
    target = tmp_path / "data" / "snapshots" / "objects" / a.sha256[:2] / f"{a.sha256}.bin"
    target.replace(target.with_name(f"{a.sha256}.bin.tmp-9999-9999"))

    assert object_store_stats(tmp_path).objects == 3        # 半个不算一个
    assert _get_object(tmp_path, a.sha256) is None
    assert next(x for x in snaps.plan(5).actions if x.rel == "a.txt").kind == ACTION_UNRESOLVABLE


def test_torn_manifest_tmp_is_invisible(tmp_path):
    """被杀点 ④：清单只写了一半（`step-10.json.tmp` 留在盘上）。

    半份清单被当成完整清单读进去，就是最典型的**静默还原出错** —— 它会算出一个
    看着合理、其实不对的目标状态。所以它既不进 `steps()`、也不进 `plan()`。
    """
    snaps = _two_step_session(tmp_path)
    (snaps.manifest_dir / "step-10.json.tmp").write_text(
        '{"files": {"a.txt": {"sha256": "deadbeef"', encoding="utf-8"   # 截断的 JSON
    )
    assert snaps.steps() == [5, 10]                 # 半成品不冒充第 10 步
    assert snaps.latest_step() == 10                # 更不会顶掉真的第 10 步
    assert len(snaps.plan(10).actions) == 2         # 读的仍是完整的那份


def test_corrupt_manifest_does_not_half_apply(tmp_path):
    """一份坏清单 → **整次回滚拒绝**，不是"跳过坏的、应用好的"。

    半应用是最糟的结果：用户看到"回滚完成"，而工作区其实是三步里的两步 +
    一步没动，没有任何一致的语义能描述它。
    """
    snaps = _two_step_session(tmp_path)
    (tmp_path / "a.txt").write_text("v9", encoding="utf-8")     # 制造一个真的待还原
    snaps.manifest_path(5).write_text('{"files": []}', encoding="utf-8")  # 结构合法但内容丢了
    snaps.manifest_path(10).write_text("不是 json", encoding="utf-8")
    with pytest.raises(SnapshotError):
        snaps.plan(5)
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "v9"   # 一个字节都没动


# ---------- 8. 跨会话共享对象库的冲突面 ----------

def _two_sessions_sharing_a_base(tmp_path: Path) -> tuple[WorkspaceSnapshots, WorkspaceSnapshots]:
    """两个会话**都**从 `v0` 出发改 `a.txt`，且都回滚到"我们碰它之前"。

    这样两份清单的 `base` 就是**同一个对象**（对象库里只存一份），而且两个会话
    各自的 `plan(5)` 都走 base 那条路 —— 一处损坏会同时打到两边，正好用来验证
    "失败是响的、且两边一样响"。

    两个会话各有一个更早的清单（第 5 步只碰了 `other.txt`），`a.txt` 到第 10 步
    才首触。中间的 `write_text("v0")` 是现实中真会发生的那一步：人 `git checkout`
    把文件恢复回原文，另一个会话接着改。
    """
    (tmp_path / "a.txt").write_text("v0", encoding="utf-8")
    (tmp_path / "other.txt").write_text("o0", encoding="utf-8")
    a, b = snap(tmp_path, "sa"), snap(tmp_path, "sb")
    for s, content in ((a, "v1"), (b, "v2")):
        s.note_write(apply_write(tmp_path, "other.txt", f"o-{content}"))
        s.capture(5)
        (tmp_path / "a.txt").write_text("v0", encoding="utf-8")
        s.note_write(apply_write(tmp_path, "a.txt", content))
        s.capture(10)
    return a, b


def test_two_sessions_really_share_the_base_object(tmp_path):
    """先把上一段的前提本身钉住 —— 不共享的话，下面两条测的就不是共享。"""
    a, b = _two_sessions_sharing_a_base(tmp_path)
    base_a = read_manifest(a, 10)["files"]["a.txt"]["base"]["sha256"]
    base_b = read_manifest(b, 10)["files"]["a.txt"]["base"]["sha256"]
    assert base_a == base_b
    assert next(x for x in a.plan(5).actions if x.rel == "a.txt").kind == ACTION_RESTORE
    assert next(x for x in b.plan(5).actions if x.rel == "a.txt").kind == ACTION_RESTORE


def test_rewind_never_writes_to_the_object_store(tmp_path):
    """回滚是**纯读**对象库的。

    只要它顺手清一下"我们不再需要的对象"，另一个会话的回滚就会被它悄悄弄坏 ——
    而损坏要到那个会话下次 `--rewind` 才显形，中间隔着不知道多少时间。
    所以：回滚前后对象库**逐字段相等**。
    """
    a, b = _two_sessions_sharing_a_base(tmp_path)
    before = object_store_stats(tmp_path)

    report = a.restore(a.plan(5))
    assert report.failures == ()
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "v0"

    assert object_store_stats(tmp_path) == before          # 逐字段相等
    assert b.restore(b.plan(5)).failures == ()             # B 一点没受影响


def test_corrupted_object_affects_both_sessions_the_same_way(tmp_path):
    """对象损坏对两个会话**一视同仁**，不会出现"一个说成功、一个说失败"。

    这正是共享对象库要付的代价，所以它必须是**响的**：两边都拒绝写回、都如实报错。
    两个 plan 都必须先算出来（一旦有一边先还原成功，另一边的目标就变成"已到位"了）。
    """
    a, b = _two_sessions_sharing_a_base(tmp_path)
    plan_a, plan_b = a.plan(5), b.plan(5)

    sha = next(x for x in plan_a.actions if x.rel == "a.txt").sha256
    assert sha == next(x for x in plan_b.actions if x.rel == "a.txt").sha256
    obj = tmp_path / "data" / "snapshots" / "objects" / sha[:2] / f"{sha}.bin"
    obj.write_bytes(b"tampered")

    for s, plan in ((a, plan_a), (b, plan_b)):
        assert [x.rel for x in s.plan(5).actions if x.kind == ACTION_UNRESOLVABLE] == ["a.txt"]
        assert [rel for rel, _ in s.restore(s.plan(5)).failures] == ["a.txt"]

    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "v2"   # 两边都没写坏


def test_concurrent_put_object_keeps_one_intact_copy(tmp_path):
    """同一份内容被多个线程同时写进对象库：只留一份，且字节完整。

    这是"跨会话共享"的**写入面** —— 同一工作区开两个终端、各自跑一个会话时就是
    这个形状。靠的是"先写带 pid/tid 的临时文件、再原子替换"，**没有锁**
    （决议 7：锁的必要性要由实测说话，不凭空加）。

    这条测试**真的抓到过东西**：Windows 上并发替换同一个目标名会抛
    `PermissionError(13)`（8 线程 × 5 次里 19 次），而目标其实是对的 ——
    `_put_object` 因此要在替换失败后先看目标在不在，再决定是不是真失败。
    """
    data = b"shared content" * 200
    shas: list[str] = []
    errors: list[BaseException] = []
    barrier = threading.Barrier(8)

    def worker() -> None:
        try:
            barrier.wait(timeout=10)
            for _ in range(5):
                shas.append(_put_object(tmp_path, data))
        except BaseException as exc:      # 线程里的异常要在主线程里断言，不能吞
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)

    assert errors == []
    assert len(shas) == 40
    assert len(set(shas)) == 1                                  # 内容相同 → 同一个 sha
    assert object_store_stats(tmp_path).objects == 1            # 盘上只有一份
    assert _get_object(tmp_path, shas[0]) == data               # 且字节完整


def test_put_object_reports_real_failures(tmp_path, monkeypatch):
    """但真的写不进去时**必须抛** —— 上面那条"失败也当成功"的容忍不能吞掉真故障。"""
    def boom(self, target):
        raise OSError("模拟磁盘满")

    monkeypatch.setattr(Path, "replace", boom)
    with pytest.raises(OSError):
        _put_object(tmp_path, b"content")
    # 而且没留下临时垃圾
    assert list((tmp_path / "data" / "snapshots" / "objects").rglob("*.tmp-*")) == []
