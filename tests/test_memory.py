"""M4-1 tests: agent/memory.py（分层指令文件 + @include + 去重/预算 + 提取 + consolidation）。"""
from pathlib import Path

import pytest

from agent.llm import MockLLM
from agent.memory import (
    MemoryManager,
    build_memory_blocks,
    consolidate,
    discover_memory_files,
    extract_conventions,
    save_learned,
)


def write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


# ---------- 发现 ----------

def test_discover_layered_order(tmp_path):
    """发现顺序：根目录 CODEAGENT < MINI < CLAUDE → rules/*.md → learned.md 最后。"""
    write(tmp_path / "MINI.md", "# mini")
    write(tmp_path / "CODEAGENT.md", "# ca")
    write(tmp_path / "CLAUDE.md", "# claude")
    write(tmp_path / ".codeagent/rules/a.md", "# a")
    write(tmp_path / ".codeagent/rules/learned.md", "# learned")
    names = [p.relative_to(tmp_path).as_posix() for p in discover_memory_files(tmp_path)]
    assert names == [
        "CODEAGENT.md",
        "MINI.md",
        "CLAUDE.md",
        ".codeagent/rules/a.md",
        ".codeagent/rules/learned.md",
    ]


def test_discover_ignores_missing(tmp_path):
    assert discover_memory_files(tmp_path) == []
    write(tmp_path / "CLAUDE.md", "# claude")
    assert [p.name for p in discover_memory_files(tmp_path)] == ["CLAUDE.md"]


# ---------- @include ----------

def test_include_inline_resolution(tmp_path):
    """@include 递归展开，内容原位拼接，@路径行被替换。"""
    write(tmp_path / "notes/extra.md", "extra 内容\n")
    write(tmp_path / "notes/more/deep.md", "deep 内容\n")
    write(tmp_path / "notes/extra.md", "extra 内容\n@more/deep.md\n")
    write(tmp_path / "CLAUDE.md", "# 主\n@notes/extra.md\ntail\n")
    blocks = build_memory_blocks(tmp_path)
    assert len(blocks) == 1
    assert "extra 内容" in blocks[0]
    assert "deep 内容" in blocks[0]
    assert "tail" in blocks[0]
    assert "@notes/extra.md" not in blocks[0].split("# 主\n")[1].split("tail")[0]


def test_include_rejects_absolute_and_dotdot(tmp_path):
    """拒绝绝对路径 / .. 段：给出占位注释，不读文件不报错。"""
    write(tmp_path / "CLAUDE.md", "@/etc/passwd\n@../secret.md\n")
    blocks = build_memory_blocks(tmp_path)
    assert "非法" in blocks[0]
    assert "拒绝绝对路径/.." in blocks[0]  # 占位注释，不暴露内容
    assert "@/etc/passwd" not in blocks[0]  # 原始 @ 行被替换掉


def test_include_cycle_detection(tmp_path):
    """循环 include：A→B→A 时对 A 输出占位注释，不死循环。"""
    write(tmp_path / "a.md", "@b.md\n")
    write(tmp_path / "b.md", "@a.md\n")
    write(tmp_path / "CLAUDE.md", "@a.md\n")
    blocks = build_memory_blocks(tmp_path)
    assert "循环" in blocks[0]


def test_include_missing_placeholder(tmp_path):
    """缺失的 include 目标 → 占位注释。"""
    write(tmp_path / "CLAUDE.md", "@not_exist.md\n")
    blocks = build_memory_blocks(tmp_path)
    assert "缺失" in blocks[0]


def test_include_escape_rejected(tmp_path):
    """sandbox 逃逸（sub/../x 形式含 .. 段）也被拒绝。"""
    write(tmp_path / "CLAUDE.md", "@sub/../outside.md\n")
    blocks = build_memory_blocks(tmp_path)
    assert "非法" in blocks[0]


# ---------- 去重 + 预算 ----------

def test_hash_dedup_keeps_high_priority(tmp_path):
    """相同内容块保留靠后（高优先级）一份，来源标注为高优先级文件。"""
    text = "完全相同的约定\n"
    write(tmp_path / "MINI.md", text)
    write(tmp_path / "CLAUDE.md", text)
    blocks = build_memory_blocks(tmp_path)
    assert len(blocks) == 1
    assert "# CLAUDE.md" in blocks[0]
    assert "# MINI.md" not in blocks[0]


def test_per_file_budget_truncates(tmp_path, monkeypatch):
    monkeypatch.setattr("agent.memory.MAX_FILE_CHARS", 10)
    write(tmp_path / "CLAUDE.md", "x" * 50 + "\n")
    blocks = build_memory_blocks(tmp_path)
    assert "已截断" in blocks[0]


def test_total_budget_drops_low_priority(tmp_path, monkeypatch):
    """总预算 100：高优先级 c 全保留，b 截断，低优先级 a/CLAUDE 被丢弃。"""
    monkeypatch.setattr("agent.memory.MAX_TOTAL_CHARS", 100)
    write(tmp_path / "CLAUDE.md", "@.codeagent/rules/a.md\n@.codeagent/rules/b.md\n@.codeagent/rules/c.md\n")
    write(tmp_path / ".codeagent/rules/a.md", "a" * 60)
    write(tmp_path / ".codeagent/rules/b.md", "b" * 60)
    write(tmp_path / ".codeagent/rules/c.md", "c" * 60)
    blocks = build_memory_blocks(tmp_path)
    joined = "".join(blocks)
    assert "超出总预算" in joined
    assert "# .codeagent/rules/c.md" in joined  # 高优先级 c 全保留
    assert "c" * 60 in joined
    assert "# CLAUDE.md" not in joined  # 低优先级被丢弃


def test_blocks_format_has_source_header(tmp_path):
    write(tmp_path / "CLAUDE.md", "约定一\n")
    blocks = build_memory_blocks(tmp_path)
    assert len(blocks) == 1
    assert blocks[0].startswith("# CLAUDE.md")


# ---------- 任务后提取 ----------

def test_extract_conventions_parses_lines():
    llm = MockLLM.text("- 用 python -m pytest 跑测试\n- 无\n- 编辑前先 read 文件\n")
    events = [
        {"type": "tool_call", "name": "edit", "arguments": {"path": "a.py"},
         "success": True, "duration_ms": 5},
    ]
    items = extract_conventions(llm, events)
    assert items == ["用 python -m pytest 跑测试", "编辑前先 read 文件"]


def test_extract_conventions_llm_failure_returns_empty():
    llm = MockLLM()  # 无响应 → chat 抛 RuntimeError
    items = extract_conventions(llm, [{"type": "tool_call", "name": "read", "arguments": {}}])
    assert items == []


def test_extract_returns_empty_when_only_无():
    llm = MockLLM.text("无\n")
    assert extract_conventions(llm, []) == []


# ---------- 简化 consolidation ----------

def test_consolidate_normalize_and_dedup():
    items = ["a  b", "a b", "单独约定"]
    assert consolidate(items) == ["a b", "单独约定"]


def test_consolidate_substring_merge():
    """短的被长的包含 → 合并掉短的。"""
    items = ["用 python 测试", "用 python 测试并检查输出"]
    assert consolidate(items) == ["用 python 测试并检查输出"]


def test_consolidate_filters_empty():
    assert consolidate(["", "   ", "有效约定"]) == ["有效约定"]


# ---------- 落盘 + 跨会话 ----------

def test_save_learned_roundtrip(tmp_path):
    """保存 → 再次保存去重追加 → 下次 blocks() 自动包含 learned.md。"""
    path = save_learned(tmp_path, ["约定A", "约定B"])
    assert path.exists()
    save_learned(tmp_path, ["约定A", "约定C"])  # A 去重，C 追加
    text = path.read_text(encoding="utf-8")
    assert "- 约定A" in text and "- 约定B" in text and "- 约定C" in text
    assert text.count("- 约定A") == 1

    blocks = build_memory_blocks(tmp_path)
    assert any("# .codeagent/rules/learned.md" in b for b in blocks)


def test_memory_manager_blocks_and_noop(tmp_path):
    mm = MemoryManager(tmp_path, llm=None)
    assert mm.blocks() == []
    assert mm.extract_and_learn([{"type": "tool_call", "name": "read", "arguments": {}}]) == []
    assert not (tmp_path / ".codeagent/rules/learned.md").exists()
