# CLAUDE.md — 项目约定与索引

> 本文件是新会话（包括 clear 对话后）的入口。**续作请按底部"续作三步"执行。**

## 项目定位

求职作品集：**CodeAgent** —— 参考 [pengchengneo/Claude-Code](https://github.com/pengchengneo/Claude-Code) 源码架构，用 Python 从零实现的小型 AI Coding Agent（4,162 行 / 19 模块 / 184 测试）。核心循环手写（不套 Agent SDK），支撑层用成熟库（openai / pydantic / streamlit / typer / pytest）。差异化：cache-aware 上下文 + 缓存省钱指标、step 级检查点恢复、block-at-submit hooks、轨迹驱动评估（真实 tinydb 提交 + 隐藏测试）、记忆自进化、MCP 工具接入。

**已验证状态**：真实 LLM 端到端跑通（修 bug 全流程、kill+`--resume` 续跑、eval 出真实报告 50% 1/2）。README「评估」章节有真实数字与口径说明。

> ✅ **真实跑分走的是项目选定通路（DeepSeek 官方）**：`https://api.deepseek.com` + `deepseek-chat`，与代码默认值、`.env.example` 一致。
> 最新一次：`python -m eval.runner --limit 2` → 完成率 50%（1/2）、62,812 token、¥0.0625、缓存命中 83%；真·冷启动曲线 step1 0% → 累计 77%。
> 早期曾临时借用 DashScope（阿里百炼）+ `deepseek-v4-flash` 验证端到端，**该手段已弃用**，其数字仅作历史记录留在 README 的口径说明里（命中率口径不同，不可直接比）。
> 本机 agentrouter 的 key 用不了——它只放行 Claude Code 客户端，自写程序一律 `401 unauthorized client detected`（实测 6 种认证头组合 × 2 个端点全 401）。

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
| 工具 | agent/tools/ | base(基类+schema) / bash / files(read·write·edit·glob·grep) / subagent |
| 状态 | agent/state.py | 消息构造（OpenAI 格式）+ AgentState |
| 上下文 | agent/context.py | provider-usage-first 记账 + cache-aware 布局 + snip/LLM compact（M3） |
| 工具结果 | agent/tool_result.py | 超大工具结果落盘 + 预览替换 + 批预算（M3） |
| 权限 | agent/permissions.py | once/turn/always 决策粒度 + 黑名单 + 沙箱（M2） |
| 钩子 | agent/hooks.py | Pre/PostToolUse + block-at-submit（M2） |
| 会话 | agent/session.py | JSONL 轨迹 + 检查点 + resume（M3） |
| 记忆 | agent/memory.py | 分层指令文件(@include+去重+预算) + 提取 + 简化 consolidation（M4） |
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
- MiniCode 笔记 → `docs/reference/minicode-notes.md`（长会话上下文治理：落盘/记账/compact/分层记忆/权限粒度）
- Anthropic 模式 / MCP·A2A 调研 → 见 TECH_SPEC §0 与 reference 笔记

## 续作三步（clear 对话后从这里开始）

1. 读 `TASKS.md`，找第一个 `[ ]` 任务
2. 读 `docs/TECH_SPEC.md` 对应模块规格（函数签名/数据结构/边界/测试用例都在里面，照抄即可实现）
3. 实现 → 跑验收命令/测试 → commit → 勾选 `[x]` → `git push`
