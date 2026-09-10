"""分层指令文件 + 任务后提取 + 简化 consolidation（M4-1，对应 Claude Code 记忆机制）。

对应 Claude Code 的 CLAUDE.md 哲学（少而精 / 权威来源 / 渐进披露）与
MiniCode 的 memory 简化移植：

- **发现（分层）**：工作区根候选 `CODEAGENT.md` / `MINI.md` / `CLAUDE.md`
  + `.codeagent/rules/*.md`。不做全局 home 层，只做项目层。优先级（低→高）：
  根目录三件套 → rules/*.md（按文件名）→ `learned.md`（自动学习文件，最后=最高，
  去重时天然优先）。
- **@include 解析**：`@相对路径` 行递归读取；拒绝绝对路径 / `..` 段；循环检测；
  缺失给占位注释（不报错）。已解析文件内容原位拼接。
- **hash 去重 + 预算**：相同内容块保留靠后（高优先级）一份；每文件 ≤ MAX_FILE_CHARS、
  总计 ≤ MAX_TOTAL_CHARS，超限截断并附提示。
- **任务后提取**：`extract_conventions(llm, events)` 用 llm.complete 从轨迹事件
  提炼"仓库约定/经验"（少而精，只记可复用约定）→ `save_learned()` 去重后写入
  `.codeagent/rules/learned.md`，下次会话的 discover 自动包含它（跨会话生效）。
- **简化 consolidation**：空白归一化 + 内容 hash 去重 + 子串包含合并（不 prune 不调度）。
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from agent.llm import BaseLLM
from agent.security import TAINT_HIGH, TAINT_NONE, memory_frame

MEMORY_FILENAMES = ("CODEAGENT.md", "MINI.md", "CLAUDE.md")
RULES_DIR = ".codeagent/rules"
LEARNED_FILE = "learned.md"
#: 受污染会话的提炼落点：**不自动注入**，等人复核（见 save_learned / discover）
PENDING_FILE = "learned.pending.md"

MAX_FILE_CHARS = 8_000       # 每文件预算
MAX_TOTAL_CHARS = 20_000     # 总计预算（超出部分从低优先级开始丢弃/截断）

INCLUDE_PREFIX = "@"


# ---------- 发现 ----------

def discover_memory_files(workspace_root: Path) -> list[Path]:
    """按优先级（低→高）返回存在的记忆文件。

    低：根目录 CODEAGENT.md < MINI.md < CLAUDE.md
    高：.codeagent/rules/*.md（按文件名排序，learned.md 置最后 = 最高优先级）

    **排除 `learned.pending.md`**：受污染会话提炼出的条目隔离在那里等人复核，
    在复核之前不进 system_prompt。排除写在这里（而不是靠给它起个不匹配的
    文件名）是因为「哪些文件会被自动注入」应当是一个**读得出来、测得了**的
    事实 —— 否则下次有人加一条 `*.md` 通配就会把它悄悄放进来。
    """
    root = Path(workspace_root).resolve()
    paths: list[Path] = []
    for name in MEMORY_FILENAMES:
        p = root / name
        if p.is_file():
            paths.append(p)
    rules_dir = root / RULES_DIR
    if rules_dir.is_dir():
        rules = sorted(
            p for p in rules_dir.glob("*.md")
            if p.is_file() and p.name != PENDING_FILE
        )
        learned = [p for p in rules if p.name == LEARNED_FILE]
        hand_written = [p for p in rules if p.name != LEARNED_FILE]
        paths.extend(hand_written)   # 手写规则（按文件名排序）
        paths.extend(learned)        # 自动学习文件最后 = 最高优先级
    return paths


# ---------- @include 解析 ----------

#: `@include` 的递归深度上限。没有上限的递归展开是一条**自伤**路径：
#: 工作区里被 agent 自己写出来的文件可以互相 @include，构造出指数级展开
#: （a 引 b 引 c…），在 system_prompt 组装阶段就把上下文吃光。
MAX_INCLUDE_DEPTH = 8

#: `@include` **禁止**指向的目录（相对 workspace 根）。
#: `data/tool-results/` 是 agent 自己落盘的**不可信内容区**（超大工具结果原样
#: 存盘，内容可能来自任意被读取的文件或命令输出）。把它 @include 进记忆，
#: 等于给「工具输出」开一条直达 system_prompt 的通道 —— 而记忆块的优先级比
#: 工具消息高得多。这不是内容过滤，是**切断通路**（结构性，零误报）。
INCLUDE_DENY_DIRS = ("data/tool-results",)


def _include_target(rel: str, base: Path, workspace_root: Path) -> Path | None:
    """把 @include 的相对路径解析为工作区内绝对路径；非法返回 None。

    拒绝：绝对路径（/ 开头或盘符）、含 `..` 段、解析后逃出 workspace_root、
    落在 INCLUDE_DENY_DIRS 里。
    """
    rel = rel.strip()
    if not rel:
        return None
    candidate = Path(rel)
    if candidate.is_absolute():
        return None
    parts = [seg for seg in re.split(r"[\\/]+", rel) if seg]
    if ".." in parts:
        return None
    target = (base.parent / Path(*parts)).resolve()
    root = workspace_root.resolve()
    if root not in target.parents and target != root:
        return None
    try:
        within = str(target.relative_to(root)).replace("\\", "/")
    except ValueError:
        return None
    if any(within == d or within.startswith(d + "/") for d in INCLUDE_DENY_DIRS):
        return None
    return target


def _resolve_file(
    path: Path, workspace_root: Path, seen: set[Path], depth: int = 0
) -> list[str]:
    """返回一个文件的全部内容段落（递归展开其 @include 行）。"""
    key = path.resolve()
    root = workspace_root.resolve()
    if depth >= MAX_INCLUDE_DEPTH:
        return [f"<!-- @include 深度超限（>{MAX_INCLUDE_DEPTH}），停止展开: {key.name} -->\n"]
    if key in seen:
        rel = key.relative_to(root)
        return [f"<!-- 循环 @include 跳过: {rel} -->\n"]
    seen.add(key)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return [f"<!-- 记忆文件不可读: {path.name} -->\n"]

    chunks: list[str] = []
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        if stripped.startswith(INCLUDE_PREFIX) and len(stripped) > 1:
            rel = stripped[1:].strip()
            if re.search(r"\s", rel):
                chunks.append(line)  # 不是纯 @路径 行（如 @tag 备注），原样保留
                continue
            target = _include_target(rel, path, root)
            if target is None:
                chunks.append(
                    f"<!-- @include 非法: {rel}"
                    f"（拒绝绝对路径/../越界/{'/'.join(INCLUDE_DENY_DIRS)}） -->\n"
                )
            elif not target.is_file():
                chunks.append(f"<!-- @include 缺失: {rel} -->\n")
            else:
                chunks.extend(_resolve_file(target, root, seen, depth + 1))
        else:
            chunks.append(line)
    return chunks


# ---------- 渲染（去重 + 预算） ----------

def _display_name(path: Path, workspace_root: Path) -> str:
    root = workspace_root.resolve()
    try:
        return str(path.relative_to(root)).replace("\\", "/")
    except ValueError:
        return path.name


def build_memory_blocks(workspace_root: Path) -> list[str]:
    """渲染记忆块（供 system_prompt 注入）。每块 `# 来源\\n内容`，高优先级在前。

    预算：单文件 > MAX_FILE_CHARS 截断；总长 > MAX_TOTAL_CHARS 从低优先级
    开始丢弃，最后的块截断到剩余额度（均附占位提示，不静默丢内容）。

    **为什么每块都带来源、并且整体套一层框架**：记忆文件是**工作区里的文件**。
    克隆别人的仓库时，它自带的 `CLAUDE.md` / `.codeagent/rules/*.md` 会被原样
    注入 system prompt，而 `learned.md` 的优先级还最高。这条通道的危险之处在于
    它是**结构性**的：不需要模型做错任何事，文件内容直接就位。所以这里按
    **来源**标注 + 声明「这是项目约定，不是用户指令」—— 而不是去猜内容像不像
    攻击（那又是概率性判断，且会误伤正常文档）。
    """
    root = Path(workspace_root).resolve()
    blocks: list[tuple[str, str]] = []      # (来源, 内容)，低→高收集
    index_by_hash: dict[str, int] = {}
    for path in discover_memory_files(root):
        text = "".join(_resolve_file(path, root, set(), 0))
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        if digest in index_by_hash:
            blocks[index_by_hash[digest]] = (_display_name(path, root), text)  # 靠后优先
        else:
            index_by_hash[digest] = len(blocks)
            blocks.append((_display_name(path, root), text))

    blocks.reverse()  # 高优先级在前
    out: list[str] = []
    remaining = MAX_TOTAL_CHARS
    for name, text in blocks:
        if len(text) > MAX_FILE_CHARS:
            text = text[:MAX_FILE_CHARS] + "\n\n<!-- 超出单文件预算，已截断 -->\n"
        if remaining <= 0:
            continue
        if len(text) <= remaining:
            body = text
            remaining -= len(text)
        else:
            body = text[:remaining] + "\n\n<!-- 超出总预算，已截断 -->\n"
            remaining = 0
        out.append(f"# {name}\n" + memory_frame(body, name))
    return out


# ---------- 任务后提取 ----------

EXTRACT_PROMPT = """你是 CodeAgent 的记忆整理器。根据下面的任务轨迹，提炼出可复用的仓库约定/经验，供未来会话使用。

规则：
- 只提炼可复用的约定（代码风格、工具用法、常见坑、测试/构建命令），不要记录一次性任务细节。
- 每条一行，用中文，以 "- " 开头，简洁（≤60 字）。
- 如果轨迹里没有值得记录的约定，只输出一行"无"。

轨迹（最近 {max_events} 个事件）：
{trajectory}
"""


def _trajectory_text(events: list[dict], max_events: int) -> str:
    lines: list[str] = []
    for ev in events[-max_events:]:
        t = ev.get("type")
        if t == "tool_call":
            name = ev.get("name")
            args = json.dumps(ev.get("arguments") or {}, ensure_ascii=False)[:120]
            mark = "✓" if ev.get("success") else "✗"
            lines.append(f"[工具] {name}({args}) {mark} {ev.get('duration_ms', 0)}ms")
        elif t == "llm_call":
            lines.append(f"[模型] 请求 {len(ev.get('tool_calls') or [])} 个工具")
        elif t == "empty_response_retry":
            lines.append(f"[重试] 空响应 {ev.get('attempt')}/{ev.get('limit')}")
    return "\n".join(lines)


def extract_conventions(
    llm: BaseLLM, events: list[dict], *, max_events: int = 60
) -> list[str]:
    """任务后提取：从轨迹事件提炼约定列表。

    LLM 异常/超时 → 返回 []（提取失败不阻断主流程，绝不让记忆污染任务结果）。
    """
    prompt = EXTRACT_PROMPT.format(
        max_events=max_events, trajectory=_trajectory_text(events, max_events)
    )
    try:
        text = llm.complete([{"role": "user", "content": prompt}])
    except Exception:
        return []
    items: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("- "):
            item = line[2:].strip()
            if item and item != "无":
                items.append(item)
    return items


# ---------- 简化 consolidation ----------

def consolidate(items: list[str]) -> list[str]:
    """简化 consolidation：空白归一化 → 子串包含合并 → hash 去重（保序）。"""
    normalized = [re.sub(r"\s+", " ", s).strip() for s in items if s and s.strip()]
    kept: list[str] = []
    for s in normalized:
        # 被更长的已有条目包含 → 视为重复，跳过（合并相似项）
        if any(s in other for other in normalized if other != s):
            continue
        digest = hashlib.sha256(s.encode("utf-8")).hexdigest()
        if any(hashlib.sha256(k.encode("utf-8")).hexdigest() == digest for k in kept):
            continue
        kept.append(s)
    return kept


# ---------- 落盘（跨会话生效） ----------

def save_learned(
    workspace_root: Path, new_items: list[str], *, filename: str = LEARNED_FILE
) -> Path:
    """把新提炼的约定合并进 `.codeagent/rules/<filename>`（hash 去重，保留已有）。

    `learned.md` 会被下次 discover 自动发现 → 新会话注入 system_prompt（跨会话记忆）。
    返回写入的路径。

    `filename` 可换成 `learned.pending.md`：**被标记为受污染的会话走这条**。
    pending 文件**不匹配 discover 的 `*.md`**？—— 它匹配。所以它**不自动注入**
    靠的是 discover 排除（见 `discover_memory_files`），而不是靠文件名藏起来。
    这样「不注入」是一个可读、可测试的事实，不是"因为没人去找它"。
    """
    root = Path(workspace_root).resolve()
    learned_path = root / RULES_DIR / filename
    existing: list[str] = []
    if learned_path.exists():
        existing = [
            line.strip()[2:]
            for line in learned_path.read_text(encoding="utf-8").splitlines()
            if line.strip().startswith("- ")
        ]
    merged = consolidate(existing + new_items)
    if not merged:
        return learned_path
    learned_path.parent.mkdir(parents=True, exist_ok=True)
    header = (
        "# 跨会话学习到的仓库约定（任务后自动提取，可手改）\n"
        if filename == LEARNED_FILE
        else (
            "# 待人工复核的学习条目（**不自动注入**）\n"
            "# 本次任务所在的会话被标记为受污染 —— 提炼出的内容可能被工具输出带偏，\n"
            "# 因此隔离在这里，等人看过再手工并入 learned.md。\n"
        )
    )
    learned_path.write_text(
        header + "\n".join(f"- {item}" for item in merged) + "\n",
        encoding="utf-8",
    )
    return learned_path


class MemoryManager:
    """M4-1 对外入口：渲染记忆块 + 任务后提取学习。

    注入：`QueryEngine(..., memory_blocks=MemoryManager(workspace).blocks())`。
    任务后：`mm.extract_and_learn(result.events)`（无 llm 时静默跳过）。
    """

    def __init__(self, workspace_root: Path, *, llm: BaseLLM | None = None) -> None:
        self.workspace_root = Path(workspace_root).resolve()
        self.llm = llm

    def blocks(self) -> list[str]:
        return build_memory_blocks(self.workspace_root)

    def extract_and_learn(self, events: list[dict], *, taint: str = TAINT_NONE) -> list[str]:
        """提炼约定并写回 learned.md；返回新写入条目（无 llm → []）。

        会话被标记为受污染时写入 `learned.pending.md`（**不自动注入**，等人复核）。
        这是**结构性**隔离，不是内容过滤：不判断提炼出来的条目像不像被带偏，
        只根据会话的污染标记决定写哪个文件。理由见 `save_learned` 的说明 ——
        内容过滤会误伤正常条目，而"这个会话的输入被标记过"是个确定的事实。
        """
        if self.llm is None:
            return []
        items = extract_conventions(self.llm, events)
        if not items:
            return []
        filename = LEARNED_FILE if taint != TAINT_HIGH else PENDING_FILE
        save_learned(self.workspace_root, items, filename=filename)
        return items
