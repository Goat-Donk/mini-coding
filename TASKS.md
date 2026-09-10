# TASKS — 任务清单（勾选式，每写一部分代码就更新）

> 续作流程：读本文件找第一个 `[ ]` → 读 `docs/TECH_SPEC.md` 对应模块规格 → 实现 → 跑验收命令/测试 → commit → 勾选 `[x]` → push。
> 约定：commit message 用 conventional 格式并以 `Co-Authored-By: Claude Code <noreply@anthropic.com>` 结尾；remote = `git@github.com:Goat-Donk/mini-coding.git`。

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
- [x] M3-4 `agent/session.py`：JSONL 轨迹 + 每 N 步检查点 + resume（验收：`pytest tests/test_session.py`；杀进程后 `--resume` 续跑）
- [x] M3-5 控制台加指标：缓存命中率/省钱曲线 + 检查点列表 + 上下文用量分级（验收：控制台可见指标）
- [x] M3-6 更新 TECH_SPEC + commit + push

## M4 记忆 + research 子 agent（Day 12–15）

- [x] M4-1 `agent/memory.py`：**分层指令文件**（工作区根 MINI.md/CLAUDE.md/.codeagent/rules/*.md + `@include` 解析 + hash 去重 + 每文件 8K/总计 20K 预算）+ **任务后提取 + 简化 consolidation**（跨会话应用约定）（验收：`pytest tests/test_memory.py`）
- [x] M4-2 `agent/tools/subagent.py`：research 子 agent（只读嵌套循环、独立上下文、受限工具集、可取消，返回结构化报告）（验收：`pytest tests/test_subagent.py`；「探索仓库并总结架构」出报告）
- [x] M4-3 更新 TECH_SPEC + commit + push

## M5 评估 + 控制台打磨（Day 16–18）

- [x] M5-1 `eval/golden_tasks.py`：黄金任务集（clone tinydb，从 git history 构造修 bug 任务 + 隐藏测试）
- [x] M5-2 `eval/runner.py`：跑任务→测试判定→完成率/成本指标→回归报告（验收：`python -m eval.runner` 出报告）
- [x] M5-3 控制台检查点回放视图
- [x] M5-4 更新 TECH_SPEC + commit + push

## M6 文档 + 打磨（Day 19–21）

- [x] M6-1 README 完善（mermaid 架构图）+ docs/architecture.md（逐层对应 CC 源码）
- [x] M6-2 docs/interview_guide.md（面试讲解稿）
- [x] M6-3 MCP 客户端接入一个标准 MCP server
- [x] M6-4 录制演示视频（修 bug → 加功能 → 杀进程恢复 → 跨会话记忆）
- [x] M6-5 收尾：CLAUDE.md 精简为 Lean 约定版 + 最终 commit/push
- [x] M6-6 真实 LLM 端到端验证（接真实 key 跑通全流程；修掉验证中暴露的真 bug：judge 假阴性、`--resume` 文档与实现不一致）
- [x] M6-7 修 M6-6 暴露的两个体验问题：CLI 事件实时流式打印 + agent 不知道工作目录（system prompt 注入 `workspace_root`/平台提示）

---

## 进度快照

- 当前里程碑：**M6 完成**（M6-1~M6-7 全部完成；M6-4 演示视频脚本见 docs/interview_guide.md §12）
- 最近完成：官方通路真实重跑 + M6-7 过度声称的更正（详见下方"验证中发现并修复的真 bug"第 3 条）
- 代码状态：4,162 行源码 / 19 模块 / 184 测试全绿
- 验证中发现并修复的真 bug：
  1. **judge 假阴性**：tinydb 的 `pytest.ini` 写死 `--cov*`，本机无 pytest-cov → pytest 以 usage error（退出码 4）退出，**测试一次没跑**，却被判成"没修好"，完成率被压成假的 0%。修法：`-o addopts=` + 把退出码 2/3/4/5 识别为无效判定（记入 error，不污染完成率）。修前 `0/2` → 修后 `1/2`
  2. **`--resume` 文档与实现不一致**：README/CLAUDE.md 写 `python -m app.cli --resume`，但 `task` 是必填位置参数 → 直接报 `Missing argument 'TASK'`。修法：`task` 改为可选 + 非 resume 时空任务报错
  3. **agent 不知道自己的工作目录**：system prompt 只说"只能在工作目录（沙箱）内操作"，却从没告诉它这个目录**是什么**。修法：system prompt 新增"工作目录"段，注入 `{workspace_root}` 与 `{platform}` 槽位；注入用逐个 `str.replace` 而非 `str.format`（自定义 prompt 含花括号会抛 KeyError）。
     **⚠️ 实测结论与当初的声称不符（2026-09-10 用 DeepSeek 官方通路做的 A/B + 机制探针）：**
     - A/B 修 bug 任务（改后 6 步 / 改前 6 步，两边都修好、两边都没有瞎猜路径）→ **步数收益未复现**
     - A/B `--resume` 续跑场景（`max_steps=3` 制造中途检查点；两边都续跑 3 步、都修好、都无瞎猜）→ **"修掉 `--resume` 迷路"这个因果未复现**
     - 机制探针（任务要求写出工作目录绝对路径）：A 组（新 prompt）**写对**（4 步）；B 组（旧 prompt）**写错**（5 步）—— 差异是真的，但不在步数上，在**路径正确性**上
     → 因此这次改动应如实定性为**防御性健壮性改进**：它消除了"模型不知道工作目录"这个不确定性（并让绝对路径一定写对），但**没有实测到步数下降**。commit `943939f` 的 message 里"修 `--resume` 迷路的真根因"属过度声称，以此条为准。
  4. **`pwd` 在 Windows 上返回无效路径**（本次 A/B 顺带挖出的真问题）：本机 `pwd` 被 Git for Windows 的 `D:\Git\usr\bin\pwd.exe` 抢占 → 返回 MSYS 风格 POSIX 路径 `/d/RAG项目/...`，**在 Windows 上不是合法路径**；`cd`（不带参数）返回的才是 `D:\RAG项目\...`。B 组就是这么写错的。修法：platform hint 里明确"确认当前目录用 `cd`，不要用 `pwd`"
  5. **CLI 跑完才一次性打印事件**：长任务中途零反馈。修法：`QueryEngine(on_event=...)` 实时回调 + `app/cli.py` 的 `EventPrinter`（**带锁** —— 只读工具并发执行时 `record_event` 会从多个工作线程回调，不加锁两行会交错）。顺带给 `Session.emit` 的 JSONL 写入加锁，让"append-only 不交错"成为真保证
- 已知待改进（未修）：
  - ~~**真实跑分用的是临时通路，待换官方口径重跑**~~ ✅ **已完成（2026-09-10）**：用 DeepSeek 官方 `deepseek-chat` 重跑 `python -m eval.runner --limit 2` → 完成率 50%（1/2）、62,812 token、¥0.0625、缓存命中 83%；README 的跑分与缓存曲线已全部换成官方口径（曲线为真·冷启动：step1 0% → 累计 77%）。历史 DashScope 数字只作为口径说明里的对照保留，并注明不可直接比。
  - **M6-7 的真实模型验证已补跑**（2026-09-10，DeepSeek 官方 `deepseek-chat`）：修 bug 任务 **5 步**修好（10,112 token / 缓存命中 73%）；kill → `--resume` 恢复后**第 4 步直接 `edit(path=calc.py)`**，无重新探路、无磁盘遍历，续跑至 11 步完成（缓存命中 90%）。轨迹扫描「瞎猜路径」4 类模式（`/workspace`、`C:\Users\<字母>`、`dir C:\Users`、全盘搜索）**无命中**
  - CLI 流式输出这条**已真实可见**（事件随步实时打印）；system prompt 注入工作目录这条的收益**见上条第 3 点的更正口径**
  - Streamlit 控制台只跑过 AppTest，没在浏览器里真开过
  - 真实 token 压力下的两级 compact 从未触发（实测上下文只到 10%）
