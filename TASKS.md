# TASKS — 任务清单（勾选式，每写一部分代码就更新）

> 续作流程：读本文件找第一个 `[ ]` → 读 `docs/TECH_SPEC.md` 对应模块规格 → 实现 → 跑验收命令/测试 → commit → 勾选 `[x]` → push。
> 约定：commit message 用 conventional 格式并以 `Co-Authored-By: Claude Code <noreply@anthropic.com>` 结尾；remote = `git@github.com:tudfgfgvhjhb/mini-coding.git`。

## M1 交接文档 + 循环 + 核心工具（Day 1–3）

- [x] M1-1 脚手架：git init + 目录结构 + .gitignore/.env.example/pyproject.toml
- [x] M1-2 交接文档：docs/reference 两篇 + CLAUDE.md + TASKS.md + docs/TECH_SPEC.md（验收：三份文档齐全可续作）
- [x] M1-3 `agent/llm.py`：DeepSeek client（function calling + usage/cache 采集）+ MockLLM（验收：`python -m pytest tests/test_llm.py`）
- [x] M1-4 `agent/tools/base.py`：Tool 基类 + pydantic schema 自动生成 + ToolRegistry（验收：`pytest tests/test_tools.py::test_schema`）
- [x] M1-5 `agent/tools/bash.py` + `files.py`：bash（超时/危险过滤）+ read/write/edit(唯一匹配+diff)/glob/grep(截断)（验收：`pytest tests/test_tools.py`）
- [x] M1-6 `agent/state.py` + `agent/loop.py`：QueryEngine 循环 + 只读并发（验收：`pytest tests/test_loop.py`；`python -m app.cli "读 README 并总结"`）
- [x] M1-7 测试全部跑通 + `app/cli.py` 最小版（验收：`python -m pytest tests/` 全绿；`python -m app.cli --mock "任务"` 演示）
- [x] M1-8 首次 commit + push（验收：`git log` 有记录、`git push` 成功）
- [x] M1-9 loop 空响应恢复：模型返回空文本自动重试（≤2 次）+ continuation prompt（验收：`pytest tests/test_loop.py::test_empty_response_recovered`）

## M2 权限 + hooks + 控制台 v1（Day 4–6）

- [x] M2-1 `agent/permissions.py`：规则文件引擎 + **决策粒度（allow_once/allow_turn/allow_always/deny_once/deny_always/ask）** + 危险命令黑名单 + 路径沙箱（验收：`pytest tests/test_permissions.py`；`rm -rf` 走 ask）
- [x] M2-2 `agent/hooks.py`：PreToolUse/PostToolUse 分发 + 内置 block-at-submit 示例（git commit 前检查测试通过标记）（验收：`pytest tests/test_hooks.py`）
- [x] M2-3 `app/ui_streamlit.py` v1：实时循环/工具调用/权限确认按钮（验收：`streamlit run app/ui_streamlit.py` 能跑通一个任务）
- [x] M2-4 更新 TECH_SPEC（permissions/hooks 规格补全）+ commit + push

## M3 上下文 + 检查点（Day 7–11）★ 两大差异化 + MiniCode 吸收

- [x] M3-1 `agent/context.py`：**provider-usage-first 记账**（assistant 消息附 usage + 尾部估算 + 50/85/95% 分级告警）+ **cache-aware 消息布局**（稳定前缀置前）+ 预算（验收：`pytest tests/test_context.py`；命中率曲线上升）
- [x] M3-2 `agent/tool_result.py`：**超大工具结果落盘**（>50K 字符 → `data/tool-results/` + 预览替换 `<persisted-output>`；批内预算 200K 兜底）——替代纯截断（验收：`pytest tests/test_tool_result.py`）
- [x] M3-3 `agent/context.py` compact 流水线：**确定性 snip compact**（70% 触发、保留最近 12 条、无 LLM）→ **LLM 摘要 compact**（critical 才触发、boundary 对齐 API 轮次、压缩前 usage 标记 stale）（验收：`pytest tests/test_context.py`）
- [ ] M3-4 `agent/session.py`：JSONL 轨迹 + 每 N 步检查点 + resume（验收：`pytest tests/test_session.py`；杀进程后 `--resume` 续跑）
- [ ] M3-5 控制台加指标：缓存命中率/省钱曲线 + 检查点列表 + 上下文用量分级（验收：控制台可见指标）
- [ ] M3-6 更新 TECH_SPEC + commit + push

## M4 记忆 + research 子 agent（Day 12–15）

- [ ] M4-1 `agent/memory.py`：**分层指令文件**（工作区根 MINI.md/CLAUDE.md/.codeagent/rules/*.md + `@include` 解析 + hash 去重 + 每文件 8K/总计 20K 预算）+ **任务后提取 + 简化 consolidation**（跨会话应用约定）（验收：`pytest tests/test_memory.py`）
- [ ] M4-2 `agent/tools/subagent.py`：research 子 agent（只读嵌套循环、独立上下文、受限工具集、可取消，返回结构化报告）（验收：`pytest tests/test_subagent.py`；「探索仓库并总结架构」出报告）
- [ ] M4-3 更新 TECH_SPEC + commit + push

## M5 评估 + 控制台打磨（Day 16–18）

- [ ] M5-1 `eval/golden_tasks.py`：黄金任务集（clone tinydb，从 git history 构造修 bug 任务 + 隐藏测试）
- [ ] M5-2 `eval/runner.py`：跑任务→测试判定→完成率/成本指标→回归报告（验收：`python -m eval.runner` 出报告）
- [ ] M5-3 控制台检查点回放视图
- [ ] M5-4 更新 TECH_SPEC + commit + push

## M6 文档 + 打磨（Day 19–21）

- [ ] M6-1 README 完善（mermaid 架构图）+ docs/architecture.md（逐层对应 CC 源码）
- [ ] M6-2 docs/interview_guide.md（面试讲解稿）
- [ ] M6-3 （有余力）MCP 客户端接入一个标准 MCP server
- [ ] M6-4 录制演示视频（修 bug → 加功能 → 杀进程恢复 → 跨会话记忆）
- [ ] M6-5 收尾：CLAUDE.md 精简为 Lean 约定版 + 最终 commit/push

---

## 进度快照

- 当前里程碑：**M3 进行中**（上下文治理 + 检查点，差异化核心；M3-1~M3-3 已 push）
- 最近完成：M3-1 provider-usage-first 记账 + cache-aware 布局；M3-2 超大工具结果落盘 + 批预算；M3-3 compact 流水线（snip + LLM 摘要）
- 下一步：**M3-4 session**——JSONL 轨迹 + 每 N 步检查点 + resume
