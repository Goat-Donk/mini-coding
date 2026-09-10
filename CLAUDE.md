# CLAUDE.md — 项目约定与索引

> 本文件是新会话（包括 clear 对话后）的入口。**续作请按底部"续作三步"执行。**

## 项目定位

求职作品集：**CodeAgent** —— 参考 [pengchengneo/Claude-Code](https://github.com/pengchengneo/Claude-Code) 源码架构，用 Python 从零实现的小型 AI Coding Agent（6,241 行 / 24 模块 / 330 测试）。核心循环手写（不套 Agent SDK），支撑层用成熟库（openai / pydantic / streamlit / typer / pytest）。差异化：cache-aware 上下文 + 缓存省钱指标、step 级检查点恢复、block-at-submit hooks、轨迹驱动评估（真实 tinydb 提交 + 隐藏测试）、记忆自进化、MCP 工具接入、注入文本检测 + 会话污染天花板、skills 渐进披露、计划清单跨回合、提问暂停/续答。

> 数字口径（改数字时请沿用）：**源码 = `agent/` + `app/` + `eval/` 里被 git 跟踪的 `.py` 行数**（不含 `eval/repos/` 的克隆仓，它被 gitignore）；**模块数 = 其中非空的 `.py` 文件数**（4 个空 `__init__.py` 不计）；**测试 = `tests/` 行数 / pytest 用例数**。

**已验证状态**：真实 LLM 端到端跑通（修 bug 全流程、kill+`--resume` 续跑、eval 出真实报告 50% 1/2）。README「评估」章节有真实数字与口径说明。**M6 收尾后又补齐三项此前只是单测覆盖的验证**：compact 在真实 token 压力下真实触发（当时是两级；M8 加了分级截断，现为三级 compact）、MCP 接真实第三方 server（官方 `mcp-server-time`）并确认仍走权限/hook 门禁链、Streamlit 控制台用真实 Chrome 打开并操作控件跑通 mock 任务。

**CLI 治理链路**：`app/cli.py` 两处 `QueryEngine` 都已接 `PermissionsEngine`（默认 allow、危险命令 ask→无确认交互→拒绝、路径越界 deny、**第三方/MCP 工具须在 `mcp.json` 的 `allow` 里显式授权否则 ask**）与 hooks（`default_engine()`：block-at-submit + marker 自动维护 + **注入检测**）。两个入口共用 `hooks.default_engine()`，避免接线漂移（CLI 曾整体漏接 hooks）。`tests/test_cli.py` 有 6 例锁住这个接线。

**M7 安全机制（措辞红线，别写错）**：本项目**没有**做「防止 prompt 注入」。分三层：① `permissions.py` 执行（确定性）② `hooks.py` PreToolUse 阻断（确定性）③ `security.py` 检测（**概率性，只出告警，从不直接决定放行/拒绝**）。检测出 `high` 后收紧能力的是**后置天花板**（`_apply_taint_ceiling`，只把三类不可逆动作 网络外发/读凭据/写记忆文件 从 ALLOW 降为 ASK），位置**必须在 `_always`/`_turn` 之后、`confirm` 之前**。禁用措辞：不说「防御/防止注入」，不说「污点追踪/taint 传播」（实为**会话级粗粒度标记**），不说「纵深防御/零信任」，不说「子代理沙箱」（实为**受限只读工具集**），不给「误报率低」这类无数字形容词。局限逐条写在 README「已知未修复的绕过路径」（S1–S16，其中 S6–S10 配了探针实测），改任何一条都要同步改那张表。**bash 的凭据判据是「提到即命中」**（`CREDENTIAL_MENTION`，不锚定末尾，见 permissions.py 的注释）——别「顺手修回去」，那会让天花板重新空转。

> ✅ **真实跑分走的是项目选定通路（DeepSeek 官方）**：`https://api.deepseek.com` + `deepseek-chat`，与代码默认值、`.env.example` 一致。
> 最新一次：`python -m eval.runner --limit 2` → 完成率 50%（1/2）、62,812 token、¥0.0625、缓存命中 83%；真·冷启动曲线 step1 0% → 累计 77%。
> 早期曾临时借用 DashScope（阿里百炼）+ `deepseek-v4-flash` 验证端到端，**该手段已弃用**，其数字仅作历史记录留在 README 的口径说明里（命中率口径不同，不可直接比）。
> 本机 agentrouter 的 key 用不了——它只放行 Claude Code 客户端，自写程序一律 `401 unauthorized client detected`（实测 6 种认证头组合 × 2 个端点全 401）。

**M8 移植的四项机制（照设计自己重写，未复制任何代码）**：
- **提问暂停/续答**：`ask_user` 工具返回 `ToolResult(await_user=True)` —— 打断被建模成**数据标志**，不是阻塞式控制流，turn 语义由 loop 决定。所以 headless 只要**不注册**这个工具就完全不受影响（`eval/runner.py` 用 `default()`，故意不含它）。`--resume "回答"` 把人的回复作为一条 user 消息追加进会话。
- **skills 渐进披露**：system prompt 只放 name+简介，正文由 `load_skill` 按需取。**`render_skills_block` 的输出绝不能含正文** —— 那是"省 token"的全部依据。
- **分级截断**：compact 流水线的**第 0 级**（分级截断 → 确定性 snip → LLM 摘要）。**只在 utilization ≥ 0.70 时才跑，这是不变量不是优化开关**：低于阈值必须逐字节不碰，否则每个大工具结果都会破坏一次前缀缓存。破坏它是**静默的**（没有报错，只有一条走平的缓存命中曲线和更贵的账单），所以有一条「utilization < 0.70 时消息逐字节不变」的测试钉着。
- **计划清单**：`update_plan` 写 `state.plan`（随检查点走）。**每次传完整清单**，状态只有一个写入者。**不要每轮把 plan 注入 messages** —— 计划一变就改一段消息、那段之后的缓存全失效；可见性靠工具结果本身，只在 `--resume` 时补投一次。
- 这四项的变异测试结果（逐条打断机制、确认对应测试变红）：见 `TASKS.md`。

**模块 docstring 要注明机制出处**（先例 `agent/tool_result.py`、`agent/tools/plan.py`），与本项目「参考与来源」的惯例一致。

## 硬约束（不可违反）

- LLM 只用 DeepSeek（国内 API）；生产 `deepseek-chat`，测试用 MockLLM
- 所有路径操作在沙箱 `workspace_root` 内；bash 拒绝危险命令
- 数据全真实：agent 操作真实代码仓库，不造假
- 每个模块写完必须跑对应 pytest 通过才算完成

## 模块职责（一句话）

| 模块 | 文件 | 职责 |
|---|---|---|
| 循环 | agent/loop.py | QueryEngine：think→tool→observe→finish，只读并发/串行，终止判定+循环检测 |
| LLM | agent/llm.py | BaseLLM → DeepSeekClient / MockLLM；usage+cache 采集 |
| 工具 | agent/tools/ | base(基类+schema) / bash / files(read·write·edit·glob·grep) / subagent / ask / plan / skills |
| 状态 | agent/state.py | 消息构造（OpenAI 格式）+ AgentState（含 `plan`） |
| 上下文 | agent/context.py | provider-usage-first 记账 + cache-aware 布局 + 分级截断/snip/LLM compact（M3·M8） |
| 工具结果 | agent/tool_result.py | 超大工具结果落盘 + 预览替换 + 批预算（M3） |
| 权限 | agent/permissions.py | once/turn/always 决策粒度 + 黑名单 + 沙箱（M2） |
| 钩子 | agent/hooks.py | Pre/PostToolUse + block-at-submit（marker 由测试成功自动写）（M2） |
| 会话 | agent/session.py | JSONL 轨迹 + 检查点 + resume（M3） |
| 记忆 | agent/memory.py | 分层指令文件(@include+去重+预算) + 提取 + 简化 consolidation（M4） |
| 技能 | agent/skills.py | SKILL.md 渐进披露：只把 name+简介进 system prompt，正文由 load_skill 按需取（M8） |
| 安全 | agent/security.py | 注入文本检测（**概率性，只出告警**）+ 会话级污染标记 + 来源框架（M7） |
| MCP | agent/mcp.py | 手写 MCP stdio 客户端 + 工具适配器（第三方工具照样过权限/hooks）（M6） |
| 入口 | app/cli.py · app/ui_streamlit.py · app/replay.py | typer CLI / Streamlit 控制台 / 检查点回放 |
| 评估 | eval/golden_tasks.py · runner.py | 黄金任务 + 完成率/成本回归（M5） |

## 常用命令

```bash
python -m pytest tests/                      # 跑全部测试（每模块完成后必须过）
python -m pytest tests/test_xxx.py -k 用例    # 单模块/单用例
python -m app.cli --mock "任务"               # 无 key 演示
python -m app.cli "任务"                      # 真实 DeepSeek（需 .env 配 DEEPSEEK_API_KEY）
python -m app.cli --resume                   # 从最近检查点续跑（配合 Ctrl+C 演示）
python -m app.cli --plan                     # 只看最近会话的**任务计划清单**后退出（无 key 也能跑）
python -m app.cli --resume "补充的信息"       # 回答 agent 的提问后续跑（配 --session-id 更稳）
python -m app.cli --mcp .codeagent/mcp.json "任务"   # 加载 MCP server（见 mcp.example.json）
python -m eval.golden_tasks --clone --limit 10       # 拉 tinydb 并列出真实 fix 提交
python -m eval.runner --limit 3              # 跑黄金任务出回归报告（--mock 无 key 冒烟）
streamlit run app/ui_streamlit.py            # 控制台（指标 + 权限按钮 + 检查点回放）
```

> 本机跑真实任务前先 `export NO_PROXY="localhost,127.0.0.1,api.deepseek.com"` 并 unset `HTTPS_PROXY`（DeepSeek 官方是境内服务，走代理会绕远）。
> CLI 已支持**事件实时流式打印**（工具调用一发生就打，不用等任务结束）。

## 工程约定

- commit：conventional 格式，结尾带 `Co-Authored-By: Claude Code <noreply@anthropic.com>`
- 推送：`git push origin main`（remote 已配 SSH）
- 结构：核心层 agent/ + 应用层 app/ + 评估层 eval/ 分离；工具注册、权限、安全检查分层
- 工具参数扁平化（pydantic 无嵌套，schema 无 $defs）；工具结果 `output` 必须截断+提示
- 工具失败错误回喂模型自修复；消息必须 OpenAI 格式

## 参考与来源

- **README（作品集门面：能力表 + 架构图 + 快速开始）** → `README.md`
- **架构详解（逐层对应 CC 源码 + mermaid 图）** → `docs/architecture.md`
- **面试讲解稿（逐模块口径 + 压力问题应答 + 演示动线）** → `docs/interview_guide.md`
- CC 源码笔记 → `docs/reference/claude-code-notes.md`（含用户飞书文档《CC》要点）
- offer-Master 笔记 → `docs/reference/offer-master-notes.md`
- MiniCode 笔记 → `docs/reference/minicode-notes.md`（长会话上下文治理：落盘/记账/compact/分层记忆/权限粒度；§8 是**吸收表**，写明哪些移植、哪些明确不做及理由）
- Anthropic 模式 / MCP·A2A 调研 → 见 TECH_SPEC §0 与 reference 笔记

## 续作三步（clear 对话后从这里开始）

1. 读 `TASKS.md`，找第一个 `[ ]` 任务
2. 读 `docs/TECH_SPEC.md` 对应模块规格（函数签名/数据结构/边界/测试用例都在里面，照抄即可实现）
3. 实现 → 跑验收命令/测试 → commit → 勾选 `[x]` → `git push`
