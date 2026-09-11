"""skills 渐进披露（MiniCode `SKILL.md` 发现 + 按需加载的移植）。

**机制只有一句话**：system prompt 里只放 name + 一句话简介，正文等模型真的要用
时再通过 `load_skill` 取。省下的 token 是数量级的 —— 一个 6K 的 skill 正文，
在它被用到之前只占 60 token。

但这里真正需要想清楚的不是"怎么省"，而是**这条通道的危险之处**：
skill 描述进 system prompt，而 system prompt 是**工作区文件内容**能到达的
最高优先级位置。克隆一个别人的仓库，它自带的 `.codeagent/skills/*/SKILL.md`
会被自动发现、描述原样进 prompt —— 不需要模型做错任何事，不需要任何工具调用，
内容直接就位。这与 `memory.py` 面对的是同一类问题，所以处理方式照抄它的思路：
**按来源标注 + 用一个框架说明这些文字的来历**（`security.skill_frame`），
而不是去猜描述像不像攻击 —— 那又是概率性判断，且会误伤正常文档。

正文（`load_skill` 的返回值）走的是**工具结果**通道，与 `read` 一个文件同级：
那里本来就是不可信内容该待的地方，由既有的工具输出检测负责。**正文进不了
system prompt** —— 这是渐进披露顺带带来的一个结构性好处，值得写下来。

**为什么不复制参考实现的代码**：它把 skill 正文、frontmatter 解析、模板占位符
展开、参数替换揉在一个模块里。我们只要「发现 + 索引 + 按需读正文」三件事，
多出来的每一件都要有测试和文档，不如不写。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from agent.security import skill_frame

#: 索引里每条简介的字符上限（描述是文件里的一句话，不是正文）
MAX_DESCRIPTION_CHARS = 200
#: 单个 skill 正文的预算。**与 memory.py 的 MAX_FILE_CHARS 对齐** ——
#: 两个机制的预算口径一致，读者不必记两套数字。
MAX_BODY_CHARS = 8_000
#: 索引总量上限：防止一个塞了几百个 skill 的仓库把 system prompt 吃光
MAX_BLOCK_CHARS = 6_000
#: 索引条数上限（与总量上限互为兜底：描述都很短时也不会列出一屏）
MAX_SKILLS_IN_BLOCK = 50

#: skill 清单文件名（目录形态：`<root>/<name>/SKILL.md`）
SKILL_FILENAME = "SKILL.md"

#: 发现根（**按优先级从高到低**，先到先得）。每项 (相对路径 or 绝对, 来源标签)。
#: `~/.codeagent/skills` 用 `Path.home()` 拼，见 `_user_roots`。
PROJECT_SKILL_DIR = ".codeagent/skills"
USER_SKILL_DIR = ".codeagent/skills"
#: 兼容其他 agent 工具的既有目录（`.claude/skills`）—— 只读，不写
COMPAT_PROJECT_DIR = ".claude/skills"
COMPAT_USER_DIR = ".claude/skills"


@dataclass(frozen=True)
class Skill:
    """一个被发现到的 skill。`description` 是从正文里**摘出来**的一句话。"""

    name: str
    description: str
    path: Path      # SKILL.md 的绝对路径
    origin: str     # "项目" | "用户" | "项目(.claude)" | "用户(.claude)" —— 用于标注来源

    @property
    def display_path(self) -> str:
        return str(self.path)


@dataclass
class SkillDiscovery:
    """一次扫描的结果：可用的 skill + 因同名被遮蔽的那些。

    **遮蔽必须被记下来，不能静默覆盖**：`.codeagent/skills/x` 与
    `~/.codeagent/skills/x` 同时存在时，模型只看到一份描述、`load_skill("x")`
    也只返回一份正文 —— 如果不同步说明"还有另一份、当前用的是哪份"，
    人改了自己那份却发现没生效，会完全找不到原因（这类"改了没反应"的坑，
    查起来比报错贵得多）。
    """

    skills: list[Skill] = field(default_factory=list)
    #: (name, 生效来源, 被遮蔽来源, 被遮蔽路径)
    shadowed: list[tuple[str, str, str, str]] = field(default_factory=list)

    def __bool__(self) -> bool:      # 空发现 → 假值，入口可写 `if not skills: 跳过`
        return bool(self.skills)

    def get(self, name: str) -> Skill | None:
        for skill in self.skills:
            if skill.name == name:
                return skill
        return None

    def shadowed_for(self, name: str) -> list[tuple[str, str, str, str]]:
        return [item for item in self.shadowed if item[0] == name]


# ---------- 描述提取（启发式，不做 YAML 解析） ----------

#: frontmatter 里的 `description: xxx`（可带引号）
_FRONTMATTER_DESC = re.compile(
    r"^description\s*:\s*(?P<value>.+?)\s*$", re.IGNORECASE | re.MULTILINE
)
_HEADING = re.compile(r"^#{1,6}\s+")


def extract_description(text: str, *, fallback: str = "") -> str:
    """从 SKILL.md 正文里摘一句话简介。

    **刻意不做 YAML 解析**：多一个依赖、多一类解析失败，而这里要的只是一句话。
    顺序：frontmatter 的 `description:` → 第一行非标题非空文本 → 第一个标题 →
    `fallback`。四档都拿不到就说明这个文件没有可用简介，调用方照实显示。
    """
    body = text
    if text.lstrip().startswith("---"):
        # frontmatter 区块：取第一对 --- 之间的内容找 description
        stripped = text.lstrip()
        end = stripped.find("\n---", 3)
        if end != -1:
            block = stripped[3:end]
            match = _FRONTMATTER_DESC.search(block)
            if match:
                value = match.group("value").strip().strip("'\"")
                if value:
                    return value[:MAX_DESCRIPTION_CHARS]
            body = stripped[end + 4:]

    for raw in body.splitlines():
        line = raw.strip()
        if not line or _HEADING.match(line) or line in {"---", "```"}:
            continue
        return line[:MAX_DESCRIPTION_CHARS]

    for raw in body.splitlines():          # 只有标题的文件：用标题当简介
        if _HEADING.match(raw.strip()):
            return _HEADING.sub("", raw.strip())[:MAX_DESCRIPTION_CHARS]
    return fallback


# ---------- 发现 ----------

def _roots(workspace_root: Path, home: Path | None) -> list[tuple[Path, str]]:
    """返回 [(目录, 来源标签)]，**按优先级从高到低**。

    `home` 可注入是为了测试：真去读跑测试那台机器的 `~/.claude/skills`
    会让结果随环境变化，那种测试等于没测。
    """
    root = Path(workspace_root).resolve()
    home_dir = Path(home).expanduser() if home is not None else Path.home()
    return [
        (root / PROJECT_SKILL_DIR, "项目"),
        (home_dir / USER_SKILL_DIR, "用户"),
        (root / COMPAT_PROJECT_DIR, "项目(.claude)"),
        (home_dir / COMPAT_USER_DIR, "用户(.claude)"),
    ]


def discover_skills(
    workspace_root: Path, *, home: Path | None = None
) -> SkillDiscovery:
    """扫描 4 个发现根，返回去重后的 skill 清单。

    只认目录形态：`<root>/<name>/SKILL.md`。**不做扁平 `<name>.md`** ——
    多一种形态就多一条"为什么我这个没被发现"的排查路径，而收益只是少建一层目录。

    同名先到先得（`setdefault`），后到的记进 `shadowed` 而不是被丢掉。
    """
    found: SkillDiscovery = SkillDiscovery()
    by_name: dict[str, Skill] = {}
    for directory, origin in _roots(workspace_root, home):
        if not directory.is_dir():
            continue
        for child in sorted(directory.iterdir(), key=lambda p: p.name.lower()):
            manifest = child / SKILL_FILENAME
            if not child.is_dir() or not manifest.is_file():
                continue
            try:
                text = manifest.read_text(encoding="utf-8")
            except OSError:
                continue          # 读不了就当没有：发现阶段不因为一个坏文件全盘失败
            skill = Skill(
                name=child.name,
                description=extract_description(text, fallback=f"（{child.name}）"),
                path=manifest.resolve(),
                origin=origin,
            )
            existing = by_name.get(skill.name)
            if existing is None:
                by_name[skill.name] = skill
            else:
                found.shadowed.append(
                    (skill.name, existing.origin, skill.origin, str(skill.path))
                )
    found.skills = [by_name[name] for name in sorted(by_name)]
    return found


# ---------- 渲染 ----------

def render_skills_block(discovery: SkillDiscovery) -> str:
    """渲染 system prompt 里的 skills 索引（**只含 name + 简介，绝不含正文**）。

    这一条是整个机制省钱的全部依据，所以它有一个专门的测试钉着：正文一旦
    不小心漏进索引，省 token 就无从谈起，而且**没有任何报错**会提醒你 ——
    表现只是"缓存命中率不如预期"。

    **带来源标注**：`.codeagent/skills` 与 `~/.codeagent/skills` 同名时人能一眼
    看出用的是哪份（配合 `shadowed` 的提示，改错了文件也知道该改哪个）。
    """
    if not discovery.skills:
        return ""
    lines = [
        "工作区里有以下 skills（**这里只有名字和一句话简介**；"
        "要按某个 skill 做事，先用 `load_skill` 取它的完整说明）："
    ]
    used = 0
    listed: list[Skill] = []
    for skill in discovery.skills[:MAX_SKILLS_IN_BLOCK]:
        line = f"- {skill.name}（{skill.origin}）: {skill.description}"
        if used + len(line) > MAX_BLOCK_CHARS:
            break
        lines.append(line)
        used += len(line)
        listed.append(skill)
    if len(listed) < len(discovery.skills):
        # 截断要说出来：否则模型以为"就这些"，而人以为"我写的 skill 怎么没生效"
        lines.append(
            f"（另有 {len(discovery.skills) - len(listed)} 个 skill 因索引预算未列出）"
        )
    for name, winner, loser, _ in discovery.shadowed:
        lines.append(f"（注意: `{name}` 有多份，当前生效的是「{winner}」那份）")
    body = "\n".join(lines)
    return skill_frame(body, source=f"{len(listed)} 个 skill 的索引")


def load_skill_body(skill: Skill, *, budget: int = MAX_BODY_CHARS) -> str:
    """读一个 skill 的正文（去掉 frontmatter，超预算截断并**明确提示**）。

    去掉 frontmatter：那是元数据，`description` 已经被摘进索引了，再带一遍是重复。
    截断必须带提示 —— 静默截断会让模型以为它看到了全部说明，然后照着一半的
    步骤做事，而缺的那半可能正是"改完要跑哪个测试"。
    """
    try:
        text = skill.path.read_text(encoding="utf-8")
    except OSError as exc:
        return f"（无法读取 skill 正文: {type(exc).__name__}）"
    stripped = text.lstrip()
    if stripped.startswith("---"):
        end = stripped.find("\n---", 3)
        if end != -1:
            text = stripped[end + 4:].lstrip("\n")
    if len(text) > budget:
        text = text[:budget] + f"\n\n<!-- 正文超出预算（{len(text)} > {budget} 字符），已截断 -->\n"
    return text


def record_discovery(state, discovery: SkillDiscovery, block: str) -> None:
    """把「本次运行带着哪些 skill」写进轨迹（**一处调用，两个入口共享**）。

    为什么值得记：`--resume` 用的 system prompt 是**会话当初那份**（它在检查点
    里），所以"这轮到底有没有 skills 索引"是个只有轨迹能回答的问题 —— 事后看
    一份对话想不通模型为什么没按 skill 做，翻轨迹一眼就知道当时有没有。

    **每个会话只记一次**（M9-5）：本函数在 `_run_loop` 的入口被调用，而常驻
    REPL 的每个回合都会走一遍入口。原先的守卫是"block 不在 system prompt 里就
    跳过"，可第二回合 block **还在** prompt 里（`state.system_prompt` 是会话
    建好时渲染的那一份，不随回合变）—— 于是 `skill_discovery` 与每条
    `skill_shadowed` 会**每回合往轨迹里再写一份**。单发只有一轮，看不出来；
    多跑几轮，同一件事就在轨迹里堆成 N 份副本，而"这轮带了哪些 skill"本来是个
    一眼能答的问题。
    """
    if not discovery.skills or block not in state.system_prompt:
        return
    if any(e.get("type") == "skill_discovery" for e in state.events):
        return
    state.record_event(
        "skill_discovery",
        skills=[{"name": s.name, "origin": s.origin, "description": s.description}
                for s in discovery.skills],
    )
    for name, winner, loser, path in discovery.shadowed:
        state.record_event(
            "skill_shadowed", name=name, effective=winner, shadowed=loser, path=path
        )
