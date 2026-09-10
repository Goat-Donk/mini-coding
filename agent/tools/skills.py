"""load_skill：按需取回一个 skill 的完整说明（渐进披露的第二半）。

**这个工具的形状本身就是一条边界**：它收的是 **name**，不是 path。可加载的
文件在**扫描阶段**就已经定死（4 个发现根下的 `SKILL.md`），运行期没有任何输入
能把它指到别处去 —— 对比 `read(path=...)`：那条路要一整层路径沙箱 +
`PermissionsEngine` 的越界硬 deny 才守得住，而这里**结构上就不存在**那条路。
不是"我们防住了"，是"没有这个入口"。（`~/.codeagent/skills` 在沙箱之外，但它
是**用户自己的**目录、且内容由用户手写：读它的风险等价于读项目内文件。）

正文进的是**工具结果**通道，与 `read` 一个文件同级 —— 那里本来就是不可信内容
该待的地方。**正文永远不会进 system prompt**，这是渐进披露顺带带来的结构性好处：
把一份 8K 的说明从"每个会话都占着最高优先级位置"变成"用到时才作为数据出现一次"。
"""
from __future__ import annotations

from pydantic import BaseModel, Field

from agent.skills import MAX_BODY_CHARS, Skill, SkillDiscovery, load_skill_body
from agent.tools.base import Tool, ToolContext, ToolResult


class LoadSkillInput(BaseModel):
    name: str = Field(min_length=1, description="skill 名（见系统提示里的 skills 索引）")


class LoadSkillTool(Tool):
    name = "load_skill"
    description = (
        "取回某个 skill 的完整说明。系统提示里只给了名字和一句话简介，"
        "真正照它做事之前必须先调用本工具拿到正文。"
    )
    input_model = LoadSkillInput

    def __init__(self, skills: SkillDiscovery, *, budget: int = MAX_BODY_CHARS) -> None:
        # 扫描结果**在构造时捕获**，不在 execute 里重新扫描：否则模型看到的索引
        # （上一轮渲染的）和它能加载的名字（这一轮磁盘上的）可能对不上 ——
        # 表现为"索引里有、load 说没有"，而两次调用之间没有任何东西变过。
        self.skills = skills
        self.budget = budget

    @classmethod
    def is_read_only(cls) -> bool:
        """只读：读一个 SKILL.md，不改工作区。可以进并发批。"""
        return True

    def execute(self, args: LoadSkillInput, ctx: ToolContext) -> ToolResult:
        skill: Skill | None = self.skills.get(args.name)
        if skill is None:
            # 回喂**可用名字**（照 loop 处理未知工具的做法）：模型拼错或臆造名字时
            # 能自己改对，而不是卡在那儿反复试 —— 只说"不存在"等于让它瞎猜。
            available = ", ".join(s.name for s in self.skills.skills) or "（无）"
            return ToolResult.fail(f"没有名为 {args.name!r} 的 skill。可用: {available}")

        body = load_skill_body(skill, budget=self.budget)
        notes = [f"skill: {skill.name}（来源: {skill.origin}）"]
        shadows = self.skills.shadowed_for(skill.name)
        if shadows:
            # 同名被遮蔽必须说出来：人改了自己那份发现没生效，是最难查的一类问题
            for _, winner, loser, path in shadows:
                notes.append(f"注意: 另有「{loser}」的同名 skill 被遮蔽（{path}），当前生效的是「{winner}」")
        return ToolResult.ok(
            "\n".join(notes) + "\n\n" + body,
            data={"name": skill.name, "origin": skill.origin, "path": skill.display_path},
        )


def build_skill_tools(discovery: SkillDiscovery) -> list[Tool]:
    """构造 skill 相关的工具（**一处构造、两个入口共用**，同 `build_ask_tool`）。

    发现结果为空时返回空列表：评测仓库里没有 skills，注册了只是白送一份 schema
    占 token —— 而 schema 是**每一步请求都要带**的（不像索引只在 system 里出现一次）。

    **不进 `ToolRegistry.default()`**：理由同上，且 `default()` 不受工作区影响，
    放进去等于让所有工作区都长出一个永远返回"没有 skill"的工具。
    """
    return [LoadSkillTool(discovery)] if discovery.skills else []
