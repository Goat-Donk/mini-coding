"""skills 渐进披露 tests：agent/skills.py + agent/tools/skills.py。

**每组测试都对着机制存在的理由**（而不是对着实现）：
- 索引里绝不能有正文 —— 这是"省 token"的全部依据，漏了不会报错
- 同名遮蔽必须说出来 —— 静默覆盖会造出"改了自己那份却没生效"这种查不动的问题
- 发现只扫 4 个根、且只认 `<name>/SKILL.md` —— 边界要能一眼说清
- `load_skill` 收的是 name 不是 path —— 这条是它不需要路径沙箱的全部原因
"""
from __future__ import annotations

from pathlib import Path

from agent.llm import LLMResult, MockLLM
from agent.loop import QueryEngine
from agent.skills import (
    MAX_BODY_CHARS,
    MAX_DESCRIPTION_CHARS,
    SKILL_FILENAME,
    SkillDiscovery,
    discover_skills,
    extract_description,
    load_skill_body,
    render_skills_block,
)
from agent.tools.base import ToolContext, ToolRegistry
from agent.tools.skills import LoadSkillTool, build_skill_tools


def write_skill(root: Path, name: str, text: str) -> Path:
    """建一个 `<root>/<name>/SKILL.md` 并返回该文件路径。"""
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    manifest = directory / SKILL_FILENAME
    manifest.write_text(text, encoding="utf-8")
    return manifest


def ctx(tmp_path: Path) -> ToolContext:
    return ToolContext(workspace_root=tmp_path)


BODY = "# 发布流程\n\n1. 跑全量测试\n2. 改版本号\n"


# ---------- extract_description：四种输入 ----------

def test_description_from_frontmatter():
    text = '---\nname: release\ndescription: "发布一个新版本时必须走的三步"\n---\n\n# 正文\n'
    assert extract_description(text) == "发布一个新版本时必须走的三步"


def test_description_falls_back_to_first_text_line():
    """没有 frontmatter → 第一行非标题文本。标题不是简介（它是流程名）。"""
    text = "# 发布流程\n\n先跑测试再改版本号\n\n## 细节\n"
    assert extract_description(text) == "先跑测试再改版本号"


def test_description_falls_back_to_heading_then_placeholder():
    """只有标题的文件用标题当简介；空文件用调用方给的占位（不编一个出来）。"""
    assert extract_description("# 只有标题\n") == "只有标题"
    assert extract_description("   \n\n") == ""
    assert extract_description("", fallback="（空 skill）") == "（空 skill）"


def test_description_is_truncated():
    text = "x" * 500
    assert len(extract_description(text)) == MAX_DESCRIPTION_CHARS


# ---------- 发现：4 个根 + 优先级 + 遮蔽 ----------

def test_discovers_project_and_user_roots(tmp_path: Path):
    workspace = tmp_path / "ws"
    home = tmp_path / "home"
    write_skill(workspace / ".codeagent" / "skills", "release", BODY)
    write_skill(home / ".codeagent" / "skills", "personal", BODY)
    write_skill(workspace / ".claude" / "skills", "compat-proj", BODY)
    write_skill(home / ".claude" / "skills", "compat-user", BODY)

    found = discover_skills(workspace, home=home)

    assert [s.name for s in found.skills] == [
        "compat-proj", "compat-user", "personal", "release",  # 按名字排序
    ]
    by_name = {s.name: s for s in found.skills}
    assert by_name["release"].origin == "项目"
    assert by_name["personal"].origin == "用户"
    assert by_name["compat-proj"].origin == "项目(.claude)"
    assert by_name["compat-user"].origin == "用户(.claude)"
    assert found.shadowed == []


def test_project_wins_and_loser_is_recorded(tmp_path: Path):
    """同名时项目优先，**且被遮蔽的那份要记下来**（不静默覆盖）。

    静默覆盖的代价：用户改 `~/.codeagent/skills/x` 发现"没生效"，而系统里
    没有任何一处提到还有第二份 x —— 这类问题查起来比报错贵得多。
    """
    workspace = tmp_path / "ws"
    home = tmp_path / "home"
    write_skill(workspace / ".codeagent" / "skills", "x", "项目那份\n")
    user_path = write_skill(home / ".codeagent" / "skills", "x", "用户那份\n")

    found = discover_skills(workspace, home=home)

    assert len(found.skills) == 1
    assert found.skills[0].description == "项目那份"
    assert found.skills[0].origin == "项目"
    assert len(found.shadowed) == 1
    name, winner, loser, path = found.shadowed[0]
    assert (name, winner, loser) == ("x", "项目", "用户")
    assert path == str(user_path.resolve())


def test_flat_md_file_is_not_a_skill(tmp_path: Path):
    """只认 `<name>/SKILL.md`：扁平 `x.md` 不算。

    少一种形态 = 少一条"为什么我这个没被发现"的排查路径，收益只是少建一层目录。
    """
    skills_dir = tmp_path / "ws" / ".codeagent" / "skills"
    skills_dir.mkdir(parents=True)
    (skills_dir / "flat.md").write_text("我不算 skill\n", encoding="utf-8")
    (skills_dir / "empty-dir").mkdir()
    write_skill(skills_dir, "real", BODY)

    assert [s.name for s in discover_skills(tmp_path / "ws", home=tmp_path).skills] == ["real"]


def test_no_skills_at_all(tmp_path: Path):
    """一个都没有 → 空发现是**假值**，入口可以 `if skills:` 一句话跳过。"""
    found = discover_skills(tmp_path, home=tmp_path / "nope")
    assert not found
    assert found.skills == [] and found.shadowed == []
    assert render_skills_block(found) == ""


# ---------- 渲染：索引里绝不能有正文 ----------

def test_render_block_has_name_and_description_but_never_body(tmp_path: Path):
    """**渐进披露省钱的全部依据**，必须钉死。

    正文一旦漏进索引，机制就不省 token 了 —— 而且不会有任何报错，表现只是
    「缓存命中率不如预期 / 账单变贵」。所以用正文里独有的字符串做断言，
    而不是数长度（数长度会被无关改动碰坏）。
    """
    workspace = tmp_path / "ws"
    secret = "这段文字只存在于正文里-8f3a"
    write_skill(
        workspace / ".codeagent" / "skills", "release",
        f"---\ndescription: 发布流程三步走\n---\n\n{secret}\n",
    )

    block = render_skills_block(discover_skills(workspace, home=tmp_path))

    assert "release" in block and "发布流程三步走" in block
    assert secret not in block
    assert "load_skill" in block, "索引必须告诉模型正文怎么取，否则它不知道要去拿"


def test_render_block_mentions_shadowed_skill(tmp_path: Path):
    """遮蔽信息要进提示词：模型也该知道"这个 name 有多份，生效的是哪份"。"""
    workspace = tmp_path / "ws"
    home = tmp_path / "home"
    write_skill(workspace / ".codeagent" / "skills", "x", "项目\n")
    write_skill(home / ".codeagent" / "skills", "x", "用户\n")

    block = render_skills_block(discover_skills(workspace, home=home))

    assert "有多份" in block and "项目" in block


# ---------- 正文加载 ----------

def test_load_body_strips_frontmatter(tmp_path: Path):
    """frontmatter 是元数据，description 已经在索引里了 —— 正文里再带一遍是重复。"""
    workspace = tmp_path / "ws"
    write_skill(
        workspace / ".codeagent" / "skills", "release",
        "---\ndescription: 简介\n---\n\n# 正文标题\n内容\n",
    )
    skill = discover_skills(workspace, home=tmp_path).skills[0]
    body = load_skill_body(skill)
    assert body.startswith("# 正文标题")
    assert "description:" not in body


def test_load_body_truncates_with_explicit_notice(tmp_path: Path):
    """超预算必须带提示：静默截断会让模型以为看到的是全部说明，
    然后照着一半的步骤做事 —— 而缺的那半可能正是"改完跑哪个测试"。"""
    workspace = tmp_path / "ws"
    write_skill(workspace / ".codeagent" / "skills", "big", "y" * (MAX_BODY_CHARS + 500))
    skill = discover_skills(workspace, home=tmp_path).skills[0]
    body = load_skill_body(skill)
    assert len(body) < MAX_BODY_CHARS + 200
    assert "已截断" in body


# ---------- load_skill 工具 ----------

def load_tool(tmp_path: Path, workspace: Path | None = None) -> LoadSkillTool:
    discovery = discover_skills(workspace or tmp_path, home=tmp_path / "home")
    return build_skill_tools(discovery)[0]  # type: ignore[return-value]


def test_load_skill_returns_body(tmp_path: Path):
    workspace = tmp_path / "ws"
    write_skill(workspace / ".codeagent" / "skills", "release", BODY)
    result = load_tool(tmp_path, workspace).run({"name": "release"}, ctx(tmp_path))
    assert result.success
    assert "跑全量测试" in result.output
    assert result.data["origin"] == "项目"


def test_load_skill_unknown_lists_available(tmp_path: Path):
    """名字不存在 → **列出可用的**（照 loop 处理未知工具的做法）。

    只说"不存在"等于让模型瞎猜；回喂可用名字它就能自己改对。
    """
    workspace = tmp_path / "ws"
    write_skill(workspace / ".codeagent" / "skills", "release", BODY)
    write_skill(workspace / ".codeagent" / "skills", "lint", BODY)
    result = load_tool(tmp_path, workspace).run({"name": "relase"}, ctx(tmp_path))
    assert not result.success
    assert "lint" in result.output and "release" in result.output


def test_load_skill_reports_shadowing(tmp_path: Path):
    workspace = tmp_path / "ws"
    home = tmp_path / "home"
    write_skill(workspace / ".codeagent" / "skills", "x", "项目那份\n")
    write_skill(home / ".codeagent" / "skills", "x", "用户那份\n")
    result = load_tool(tmp_path, workspace).run({"name": "x"}, ctx(tmp_path))
    assert result.success
    assert "项目那份" in result.output
    assert "遮蔽" in result.output and "用户" in result.output


def test_no_skill_means_no_tool(tmp_path: Path):
    """没有 skill 就不注册工具：schema 是**每一步请求都要带**的，白送一份占 token。"""
    assert build_skill_tools(discover_skills(tmp_path, home=tmp_path)) == []


def test_load_skill_is_read_only():
    """只读：读一个 SKILL.md，不改工作区 —— 可以进并发批。"""
    assert LoadSkillTool.is_read_only() is True


def test_load_skill_takes_a_name_not_a_path():
    """**这条断言是结构性的**：输入模型里只有 name，没有 path。

    所以"load_skill 能不能被诱导去读任意文件"这个问题**不存在** —— 可加载的
    文件在扫描阶段就定死了（4 个根下的 SKILL.md）。对比 `read(path=...)`：
    那条路要一整层路径沙箱 + 越界硬 deny 才守得住。不是"防住了"，是"没有这个入口"。
    """
    assert set(LoadSkillTool.input_model.model_fields) == {"name"}
    # 且 schema 是扁平的（项目硬约束：无嵌套模型，否则 $defs 被 pop 掉成坏 schema）
    assert "$defs" not in LoadSkillTool(SkillDiscovery()).schema()["function"]["parameters"]


# ---------- 端到端：索引进提示词、正文不进 ----------

def test_engine_prompt_has_index_and_tool_returns_body(tmp_path: Path):
    """跑一次真循环：system prompt 里只有索引，正文要等模型调 load_skill 才出现。"""
    workspace = tmp_path / "ws"
    secret = "正文独有内容-c71b"
    write_skill(
        workspace / ".codeagent" / "skills", "release",
        f"---\ndescription: 发布流程三步走\n---\n\n{secret}\n",
    )
    discovery = discover_skills(workspace, home=tmp_path / "home")
    seen_system: list[str] = []
    seen_tool: list[str] = []

    def then_answer(messages, tools):  # noqa: ANN001 - MockLLM 回调签名
        # 第一次回调是 load_skill 之后的第二步：把模型真正收到的上下文抓下来
        for msg in messages:
            if msg.get("role") == "system":
                seen_system.append(msg["content"])
            elif msg.get("role") == "tool":
                seen_tool.append(msg["content"])
        return LLMResult(content="按 skill 做完了")

    engine = QueryEngine(
        MockLLM.script(
            MockLLM.tool("load_skill", {"name": "release"}).responses[0], then_answer
        ),
        ToolRegistry.default(workspace),
        workspace_root=workspace,
        skills=discovery,
    )
    for tool in build_skill_tools(discovery):
        engine.registry.register(tool)

    result = engine.run("发个版")

    assert result.terminated_reason == "completed"
    system = seen_system[-1]
    assert "release" in system and "发布流程三步走" in system   # 索引在
    assert secret not in system                                 # 正文不在
    assert any(secret in text for text in seen_tool)            # 正文在工具结果里
    # 轨迹里能查"这轮带着哪些 skill"（resume 时 system 是会话当初那份，只有轨迹能回答）
    assert [e["type"] for e in result.events].count("skill_discovery") == 1
