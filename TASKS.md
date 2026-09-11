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
- [x] M6-3 MCP 客户端接入一个标准 MCP server（**2026-09-10 升级为接真实第三方 server**：modelcontextprotocol 官方 `mcp-server-time`，见下方验证记录）
- [x] M6-4 录制演示视频（修 bug → 加功能 → 杀进程恢复 → 跨会话记忆）
- [x] M6-5 收尾：CLAUDE.md 精简为 Lean 约定版 + 最终 commit/push
- [x] M6-6 真实 LLM 端到端验证（接真实 key 跑通全流程；修掉验证中暴露的真 bug：judge 假阴性、`--resume` 文档与实现不一致）
- [x] M6-7 修 M6-6 暴露的两个体验问题：CLI 事件实时流式打印 + agent 不知道工作目录（system prompt 注入 `workspace_root`/平台提示）

## M7 安全机制加固（2026-09-10，对照参考简历的「权限与安全审查 / AI 风险分类」补的）

**范围钉死**：本项**不是**「防止 prompt 注入」（防不住，改写一个词就绕过）。做的是两件分开的事：**结构性加固**（确定性，零误报风险）+ **检测器**（概率性，只出告警）。措辞红线：不说「防御/防止注入」，不说「污点追踪/taint 传播」，不说「纵深防御/零信任」，不说「子代理沙箱」，不给「误报率低」这类无数字形容词。

### Part A 结构性加固（确定性）

- [x] A1 `agent/tools/bash.py`：子进程环境清洗（`_scrubbed_env()`，按变量名剔除 `*_API_KEY`/`*_TOKEN`/`*_SECRET`/`*PASSWORD*`/`AWS_*`/`*_CREDENTIAL*`）。**ROI 最高**：`load_dotenv()` 把 key 灌进 `os.environ`，`subprocess.run` 默认继承 → `echo %DEEPSEEK_API_KEY%` 一条命令外泄，不需要读任何文件
- [x] A2 `agent/mcp.py` + `agent/permissions.py`：第三方工具必须显式授权（`Tool.is_external()` → `_classify` 归 `external` 类 → `_rule_check` 命中 `external.allow` 才 ALLOW，否则 ASK）。**破坏性变更**，测试/示例/文档已同步
- [x] A3 `agent/loop.py`：`run_post` 返回值不再被丢弃（post-hook 提示拼进 `result.output`）。**独立 commit** `be815ac`
- [x] A4 `agent/permissions.py`：污染升级做成**后置天花板**（在记忆之后、人工确认之前；只降不升；只覆盖三类不可逆动作）
- [x] A5 `agent/session.py`：检查点全字段往返 + `derive_taint` 从事件重算（重放语义，不是取 max —— 取 max 会让人类已复位的标记复活）+ 全字段往返回归测试
- [x] A6 `agent/loop.py`：`gate_block` 事件（四处 `ToolResult.fail` 前 emit，带 `reason`/`source`），让拒绝原因第一次进轨迹
- [x] A7 `agent/tools/bash.py`：修 `eval\s` 误报（`echo "medieval times"` 被误判成危险命令）→ 收紧为 `(^|[;&|]\s*)eval\s`

### Part B 检测器 + 会话级污染标记（诚实标注）

- [x] B1 `agent/security.py`：7 类规则的文本模式检测（`Finding` **不存原文**；长度上限 + 字面量预筛；`level_for` 按**证据强度**分级而非命中数量）
- [x] B2 `agent/hooks.py`：`detect_injection()` 挂进 `default_engine()` 的 post_hooks（唯一构造点）。扫 `ctx.result.output`（模型真读到的字节），**fail-open 但大声记日志**
- [x] B3 `AgentState.taint` 会话级粗粒度标记：只升不降、**没有 `set_taint`**、唯一复位者是人的动作（CLI `--clear-taint`）
- [x] B4 记忆按**来源**隔离而非按内容过滤：`memory_frame` 来源框架 + `high` 会话写入 `learned.pending.md`（不自动注入）+ `@include` 拒 `data/tool-results/` 与深度上限
- [x] B5 `tests/test_security.py`：**35 例**（payload 必命中 / 良性必不误报 / 文档讲注入不判 high / **自建样例集如实报实测数** / 天花板位置 / 端到端 / 场景 B 不锁死 / resume 往返 / fail-open）
- [x] B6 README「已知未修复的绕过路径」S1–S16 —— **本项可信度最高的部分**，写「已知绕过」比写「实现了注入防御」强得多。S6–S10 每条都配了探针实测结果，S16 是最该先修的那条

### Part C 文档

- [x] C1 `docs/TECH_SPEC.md` §9.9 + §9.10
- [x] C2 `docs/architecture.md` §4 表格行 + 「检测路径 vs 执行路径」小节
- [x] C3 `README.md` 能力表行 + 「已知未修复的绕过路径」一节 + 规模计数更新
- [x] C4 `TASKS.md` M7（本节）
- [x] C5 `docs/interview_guide.md` §5 + §12 同步

### 真实 LLM 端到端验证（DeepSeek 官方通路，不用 mock）

四条**全部跑完**（2026-09-10），工作区在 `m7verify/` 与 `m7verify-nolock/`（**已 gitignore**：里面是真实轨迹/检查点与刻意放置的载荷样本，载荷进仓库会给本项目自己的检测器添一条已知误报）。实测数字如下，未跑过的不写。

- [x] V1 H1 外泄拦截：同一命令跑两次对照 —— `env=None`（修复前语义）输出 **35 字节、含真实 key**；`_scrubbed_env()` 输出 **18 字节、字面量 `%DEEPSEEK_API_KEY%`**。真跑 agent 时它把 `%DEEPSEEK_API_KEY%` 写进文件，`sk-` 出现 **0 次**。
      **顺带挖出一条更严重的残留**：环境变量挡的是最短路径，但 bash **根本不经过路径沙箱** —— 实测 `type ..\.env` 与 `cat ../.env` 各返回 **336 字节、含真实 key**（对照：`read` 工具读同一路径被硬 deny）。已补进 README 的 S16。
- [x] V2 H2 MCP 授权：同一个官方 `mcp-server-time`、同一个任务，只改 `allow` —— 不配 → 被拒且理由带出处与确切改法；配上 → `MCP 授权: 2/2 个工具免确认`，返回真实时间（2026-09-10 22:05:12）
- [x] V3 注入检出：轨迹里有 `security_finding`（**5 个规则族 / 7 处命中 / 带行号 / `level=high` / 不含原文摘录**）与 `gate_block`（带 `source=permissions` 与完整理由）。三条支线：X 从轨迹重算把 `high` 复原；Y `--clear-taint` 复位（learning 目标从 `pending.md` 翻回 `learned.md`）；Z 送出续跑指示后，先前被拒的 `findstr .env` 在第 3 步执行成功
- [x] V4 不锁定验证：**8 次工具调用 / 0 次 `gate_block` / 1 次 `security_finding`**，任务正常完成（写载荷文件 → 回读 → 写测试 → `python -m pytest -q` 退出码 0）。这一轮同时真实验证了 A3（模型自述收到了 read 结果里的**安全告警横幅**）与 B4（提炼结果落进 `learned.pending.md`）

**另外跑了一个边界探针**（`m7verify/boundary_probe.py`，22 条命令，只打印判定不改动任何东西），用来把 README 的 S 表从「推演」变成「实测」：凭据类 10 种写法（含 `./.env`、`sub/../.env`、`cat $HOME/.env`、`cp .env /tmp/x`、`certutil -encode .env`）**全部命中**；改名/短名/运行时拼名（`config.txt`、`ENV~1`、`chr(46)+'env'`）**全部不命中**；`echo hi > CLAUDE.md` **不命中**（末尾锚定）而 `echo hi >> .codeagent/rules/learned.md` 命中；`python upload.py` 与 `powershell -c "Invoke-WebRequest ..."` 不命中。

---

## M8 按需移植参考实现的四项机制（2026-09-11）

**背景**：原提议是放弃本项目现有代码、直接复制本地那份第三方 Python 移植（`F:\MiniCode-Python-main`）在其上优化，理由是"从零写太慢"。**评估后该提议的前提不成立**，已改选「保留 CodeAgent，按需移植机制」：

1. 项目已完成（M1–M7），**不存在"从零开始"的剩余工作**，放弃现有代码是净成本；
2. 定位是「核心循环手写」，照抄会毁掉这个定位（"这部分是你写的吗"将无法回答），既有资产（S1–S16 绕过路径表、V1–V4 真实验证、缓存命中曲线、评估框架）也无法迁移；
3. 本地那份 Python 移植**自有部分未声明授权**（详见 `docs/reference/minicode-notes.md` 头部 —— 注意 TS 原版是 MIT，别写成"完全没授权"）。

**因此本里程碑的全部实现 = 照设计用自己的代码重写，未复制任何代码。**

- [x] P1 `ToolContext` 接线修复：`state`/`emitter` 声明了但**没有任何入口填**（工具作者照声明去读会拿到 None 且不报错）；删掉无读者的 `settings` 字段。顺带修 `ToolResultStore` 生产路径未接线
- [x] P2 `agent/tools/ask.py` + `ToolResult.await_user`：提问暂停/续答。**打断建模成数据标志而非阻塞控制流**，headless 靠"不注册"降级；`checkpoint(force=True)` 保证第 3 步的提问也能落盘
- [x] P3 `agent/skills.py` + `agent/tools/skills.py`：SKILL.md 渐进披露，system prompt 只放 name+简介
- [x] P4 `agent/context.py` 分级截断：compact 流水线**第 0 级**，按工具给预算、head70/tail30、失败结果给更大预算
- [x] P5 `agent/tools/plan.py` + `AgentState.plan` + `--plan`：计划清单落盘跨回合
- [x] P6 文档同步（CLAUDE.md / TECH_SPEC / architecture / interview_guide / README / minicode-notes §8 / 本节）
- [x] P7 真实 LLM 验证四项 + **缓存命中率 A/B**（分级截断最要紧）+ 全量测试 —— **四项的原始证据见下节；结论里有一条与设计预期不符，如实记**

### P7 真实验证（2026-09-11，DeepSeek 官方 `deepseek-chat`，全部无 mock）

验证脚本在 `m8verify/`（已 gitignore），原始输出留在同目录的 `*.json` / `*_out.txt` 里，下面每个数字都能追回去。

**P7-a `await_user` 完整动线** ✅ —— 会话 `s20260911-004921`（`m8verify/ask2/`）

任务故意给成「补一份 README，含维护者联系方式」，而仓库里**没有任何作者/邮箱信息**。模型的反应正是这条机制想要的那一个：

```
step 1  bash(cd) · glob(**/*)
step 2  read(README.md) · bash(findstr /i /n "mail contact 维护  maintainer" …)
step 3  read(data/sessions/…jsonl)          ← 连轨迹都翻了，是真的找不到
step 4  ask_user(question="README 里要写的维护者联系方式是什么？
                  （仓库内没有任何作者/邮箱信息，我不能编造）",
                 options=[邮箱 / GitHub / 两者都要 / 先写占位符 TODO])
        → terminated_reason=await_user，问题落盘
--- 人的回答经 --resume 送进去 ---
step 5  edit(README.md, old="## 待办" → new="## 维护者\n\n- GitHub：[@Goat-Donk…")
step 6  read(README.md) · bash(python -m pytest tests/ -q)
step 7  完成
```

- 7 步，两个回合。`ask_user` 在**第 4 步**问出（不是第 5 步的检查点节拍上）→ 证明 `checkpoint(force=True)` 那条线是真的：不加 force 这个问题根本不会落盘，`--resume` 拿回来的是空的。
- 模型**没有编造**联系方式，而是把「我不能编造」写进了问题里 —— 与项目「不允许私自造假数据」这条红线同向。
- 附带发现（**已修，见下**）：这一轮的 `resume_instruction` 事件**没进轨迹**。原因不是它没发生，而是 `state.emitter` 要等引擎启动才接上，恢复期记录的事件全部落在空处。

**P7-b skills 渐进披露** ✅ —— 会话 `s20260911-010211`（`m8verify/skills/`）

工作区放 `.codeagent/skills/docstring-style/SKILL.md`，正文里埋一个内部标记 `HOUSE_STYLE_MARKER_7f3a9c`：

```
step 0  skill_discovery  4 个 skill（项目 1 + 用户 .claude 3），只有 name+简介
step 1  load_skill(name="docstring-style")   ← 索引起了作用，模型自己去取正文
step 2  read(calc.py)
step 3  write(calc.py, 按该 skill 的注释规范重写)
step 4  bash(python -c "import calc, inspect; …getdoc…")   ← 自己验证 docstring
step 5  完成
```

- 索引块对同一批 skill 是 **1,095 字符**；标记出现在索引块里 = `False`，出现在 `load_skill` 取回的正文里 = `True`。**渐进披露成立的不是"设计上成立"，是这条断言离线复算过**。
- 顺带确认发现根的顺序正确：项目 `.codeagent/skills` 排在用户 `.claude/skills` 前面。

**P7-c `update_plan` 跨回合** ✅ —— 会话 `s20260911-011348`（`m8verify/plan/`）

三步任务（修失败测试 → 各加 `__all__` → 跑测试确认全绿）：

| | 修 prompt 之前 | 修 prompt 之后 |
|---|---|---|
| 会话 | `s20260911-010415` | `s20260911-011348` |
| 步数 | 6 | 9 |
| `update_plan` 调用 | **0 次** | **2 次**（step 4 排计划 / step 8 标 done） |
| 计划进检查点 | 无 | 3 条 |

跨回合那条链条也整条跑通了（真 kill，不是模拟）：`--checkpoint-every 1` 起跑 → **kill -9 于第 5 步**（exit 137，任务半程）→ `--plan` **不带 API key 也打印出 3 条计划**（exit 0）→ `--resume` 打印「恢复会话 …（step 5）→ 续跑」+「计划恢复: 3 条」→ 计划注入在 messages 里**恰好出现一次**（index 15），其后紧跟 assistant/tool → 续跑至 step 14 完成，13 个检查点。轨迹里 `plan_resumed` ×2、`resume_instruction` ×1。

**P7-d 分级截断 + 缓存命中率 A/B** ⚠️ —— **端到端命中率没有下降，但微观对照显示单次截断的代价是 8.8 倍；两层结论都要看**

三层测法，因为只测一层会得出片面结论：

**(1) 端到端 A/B**（`m8verify/ab_run.py`，两臂只差 `_truncate_oversized` 开/关，fixture 内容逐字节相同、目录不同以保证两边都是真冷启动）

| | 区间① budget 64,000 / 13×10KB | 区间② budget 34,000 / 18×6KB |
|---|---|---|
| 步数 | A 14 / B 14 | A 19 / B 19 |
| 累计命中率 | A **60.230%** / B **60.233%** | A **59.775%** / B **59.761%** |
| B−A | **+0.003%** | **−0.014%** |
| 累计 hit token | A 319,616 / B **319,616**（逐 token 相同） | A 291,840 / B 291,968 |
| 累计 miss token | A 211,046 / B 211,019 | A 196,390 / B 196,594 |
| 截断真实释放 | 3 次（step 9/11/13，各 4,738 token） | 5 次（step 9/11/13/15/17，2,008~2,088） |
| 尾部结论行保住 | **3/3** | **5/5** |
| 同臂的 LLM 摘要 | A 3 次 / B 3 次（**同一步**） | A 5 次 / B 5 次（**同一步**） |

**(2) 单次截断的微观对照**（`m8verify/ab_micro.py`，取真实检查点 `armbigA/step-9` 的 messages 做 base，每个变体只发一次；**踩过的坑写在脚本 docstring 里**：provider 缓存不是一条前缀链，一个发过的变体下次会整条命中 —— 第一版就是这么把第二轮测反的）

| 请求 | prompt | hit | miss | 命中率 |
|---|---|---|---|---|
| ② base（不截断） | 68,618 | 68,480 | 138 | **100%** |
| ③ 第 7 条截到 4,000（13,498→4,022 字符，**省 5,128 token**） | 63,490 | 18,048 | **45,442** | **28%** |
| ④ 同样截 4,000 字符，但挪到第 17 条（后面只剩 2 条） | 63,490 | 55,168 | 8,322 | 87% |
| ⑤ 同样第 7 条，预算压到 200（截掉的字符多得多） | 61,357 | 16,512 | 44,845 | 27% |
| ⑥ 重发 ③ 同一份 messages | 63,490 | 63,356 | **134** | **100%** |

- **省 5,128、付 45,304 → 8.8 倍**，而且是**一次性**的（⑥ 重发立刻回到基准 134，代价只在截断发生的那一步）。
- 代价的形状是**后缀失效**：`_head_tail` 保留头部 70%，前缀缓存一路匹配到那 70% 处才分叉，**被改的那条之后全部算未命中**。所以代价不取决于截掉多少字符，取决于**它后面还压着多少** —— ③ 与 ④ 截掉的字符数完全相同，只差位置，代价差 **3.8 倍**；⑤ 把预算从 4,000 压到 200（截掉更多）只再少命中 1,536 token，正好是保留头部那一段的长度。

**(3) 三级触发复测**（`m8verify/compact_probe.py`）—— 回答本节末尾那条 ⚠️ 待测：**两级时代的记录原样复现，因为第 0 级在这些场景里根本没有工作可做**

| budget | 第 0 级被调用 | 其中**有工作可做** | 确定性 snip | LLM 摘要 |
|---|---|---|---|---|
| 3,600（步 9 util 0.81） | 8 次 | **0 次** | **3 次 → step [9,11,13]** | 0 次 |
| 2,600（步 9 util 1.13） | 13 次 | **0 次** | 0 次 | **3 次 → step [9,11,13]** |

工具输出都只有 160 字符，而 `TRUNCATE_BUDGETS["read"] = 4,000` 字符 —— **一条超预算的都没有**。真实小仓库的工具输出就是这一档（本仓库真跑时最大 430 字符）。

**P7-d 的结论（如实写，包括不利的那半）**

1. **端到端命中率没有下降**：两个区间的 B−A 都在 ±0.02% 以内，区间①两臂的累计 hit token 甚至**逐 token 相同**。计划里要求的「不能只写设计上不影响」，到此为止是站得住的。
2. **但这不是因为代价为零，是因为代价被遮蔽了**：截断触发的那一步，下一级的 LLM 摘要**在同一步触发**（两个区间、两臂，摘要步号完全一致），缓存本来就整段失效 —— 45,304 token 的一次性代价淹在这里面，端到端看不出来。微观对照才是它的真身。
3. **它也没换来任何东西**：中段窗口 `[max(_PREFIX_LEN, min_keep), len − keep_recent)` 要到 **20 条消息**才非空，而那时 utilization 已经到了 **1.06（区间①）/ 1.23（区间②）**，远在 0.85 之上；一次释放的 4,738 / 2,008 token 只值预算的 **7.4 / 5.9 个百分点**，而每步增量是 **11 / 13 个百分点** —— **追不上**。有用窗口只有 `[0.70, 0.70 + 释放量/预算)` ≈ 6 个百分点宽，每步增量是它的 1.5~2.2 倍，一步就跨过去了。
4. 唯一稳定为正的收益是**尾部结论行保住 8/8 次**（`read`/`grep`/`pytest` 的结论在尾部，只留头等于把最该看的部分丢掉 —— 这条设计约束是对的，且实测每次都守住）。

> **这一条留给用户拍板，我没有单方面改设计。** 数据支持的三个选项：
> **(a) 保持现状** —— 代价一次性且被遮蔽，尾行保护 8/8，当个便宜的保险丝；
> **(b) 改成"优先截最靠后的那条合格消息"** —— 同样的字符数、同样的释放量，缓存代价按 ③/④ 的比例降到 **1/3.8**；代价是模型最近用过的结果先被缩；
> **(c) 去掉第 0 级** —— 数据说它在真实输出规模下（≤430 字符 vs 4,000 预算）被调用 8~13 次、**一次都没可截的东西**，在合成大输出下又追不上下一级。
> 注意 `min_keep=6` + `keep_recent=12` 这两个既有常量决定了它**永远只能挑中第 7 条**（后缀最长的那条），这是代价 8.8 倍的结构性原因。

### 变异测试（"牙齿检查"）：逐条打断机制，确认对应测试变红

- **P4 分级截断**：9 条变异全部被捕获
- **P5 计划清单**：11 条变异全部被捕获 —— 静默降级 / 去掉长度校验 / 去掉状态校验 / `is_read_only` 改 True / 覆盖改追加 / 从 `default()` 摘掉 / 不填 `state` / 不补投计划 / `--plan` 失去独立出口 / 复用"已清空"文案 / **每轮注入计划快照**（最后这条即参考实现的做法，被我们的缓存约束否决）

> 为什么必须做：单测全绿只说明"代码没崩"，不说明"测试真的在看这件事"。这条纪律在 P4 上尤其要紧 —— 分级截断的失败模式是**静默的**，「只在越过 0.70 时才跑」这条不变量一旦被改成每步都跑，**不会让任何测试变红**，只会让缓存命中曲线走平。所以专门有「utilization < 0.70 时消息逐字节不变」一条钉着它。

### 一处静默失效的修复（.gitignore）

`.pytest_tmp/` 与 `.codeagent/` 两条规则**从未生效过**：gitignore 只在**行首**把 `#` 当注释，写在模式后面的 `# ...` 会成为模式本身的一部分。`.codeagent/` 是运行期记忆目录（`rules/learned.md`），漏 ignore 会让它有机会进仓库。已把行尾注释移到独立行并加注说明。

---

## M9 向 TS 原版对齐（2026-09-11 用户决定）

**背景**：把 TS 原版 `F:\MiniCode-main` 的 12 项「核心能力」清单（`README.zh-CN.md:125-137`）逐条在源码里核实，
结论是 **TS 原版 12 项全部真实现** —— 那是一份成熟产品，清单一条不虚，差距真实存在。
完整的**三方对照表**（TS 原版 / Python 移植 / 我们，含实现文件与行号）与核实方法见
[`docs/reference/minicode-notes.md`](docs/reference/minicode-notes.md) §10。用户决定**往原版靠**。

**排序原则**：先做「小改动 + 直接强化已有资产」的，再做「兑现已写进面试稿的承诺」的，
最后做「会动核心循环契约」的。**不做的项也写下来并给理由** —— 「测过同类机制收益不达预期，所以不做」是一个可讲的工程判断，不是缺口。

### 第一批 · 小改动，直接强化已有资产

- [x] **M9-1 ⑪ 改前 diff review**：diff 生成从写入**之后**（`agent/tools/files.py:179-195`，`"编辑完成，diff："`）提到写入**之前**，并作为权限请求的 `details` 传下去。
  - 现状：`permissions.py:47` 已有 `EDIT_TOOLS` / `edit` 类（`_classify` `:361`、`kind == "edit"` `:431`），但**没有 `ensure_edit`** —— 权限决策时**看不到 diff**，diff 只在改完之后作为工具结果回喂
  - 收益：权限从「事后报告」变「事前审批」（CC 的做法）；Streamlit 的权限弹窗也能显示 diff 了
  - 验收：`pytest tests/test_permissions.py tests/test_tools.py`；真实 CLI 跑一条 edit 任务，权限请求的 details 里带 diff

  **✅ 2026-09-11 完成。实现**：`Tool.preview(arguments, ctx)`（`agent/tools/base.py`，纯函数，默认 None）→ `WriteTool.preview` / `EditTool.preview`（`files.py`）→ `_gate_and_run` 里 `details = self._preview(...)` **在 `check()` 之前**取 → `permissions.check/ask/describe/_confirm_and_record` 收 `details` 并拼进确认文案。
  - **唯一真相源**：`EditTool._plan` 同时供 `preview` 与 `execute`（匹配、计数、替换只写一份）。各判一遍就会出现「预览说能改、执行说匹配不唯一」——而那时人已经照着预览点过允许了。
  - **两处上限是分开的**：给人看的 `DIFF_PREVIEW_CHARS = 4_000`（几万字符的 diff 会把人逼成闭眼点允许），给模型的仍是 `MAX_CHARS`。两者共用 `_unified_diff`，所以 `execute` 的 `limit=` 实参是一条**漏了也没有测试会自然变红**的接线，单配一条测试钉着。
  - **fail-open vs fail-closed 的分工**：预览是**信息** → 抛异常只记 `preview_failed` 事件并退回原确认框；权限是**判定** → 算不出来必须大声失败。**没有权限引擎时根本不计算预览**（headless 没确认交互，而 edit 的预览要把整个文件读进来做 diff，算了是白花）。
  - **新开关 `--review-edits`（`app/cli.py`）**：给 CLI 装上 `confirm` 回调（编号菜单，**直接回车默认拒绝**）并把 `edit`/`write` 抬成 ask。为什么是开关而不是默认 —— TS 原版的 edit 是默认要批准的（`permissions.ts:427 ensureEdit`，无 TTY 时直接抛「Start minicode in TTY mode to review it」），但**我们的 CLI 没有确认回调**，改成默认 ASK 会让每次改动都退化成拒绝、整条 CLI 不可用（把安全机制变成路障）。默认路径与 eval 一字不变。
  - **测试**：`test_tools.py` 8 例（含「预览后磁盘必须仍是原文」「预览与执行对唯一性判断一致」「execute 不受 4K 上限影响」）、`test_permissions.py` 3 例（details 到达确认文案 / 不参与判定 / 文案顺序问什么→改什么→为什么问）、`test_loop.py` 3 例（**确认回调被调用那一刻磁盘仍是原文** / 预览异常不打断本轮 / 无权限引擎时不计算预览）、`test_cli.py` 5 例（开关真的把 confirm+rules 交给了引擎 / 不开开关参数一模一样 / 编号映射与默认拒绝 / 无输入→拒绝 / **端到端真跑 CLI 拿到 diff**）。**359 全绿**。
  - **变异测试 12/12 被抓住**（`m9verify/mutate_m9_1.py`）：预览挪到 check() 之后、preview 自判唯一匹配、details 不拼进文案、execute 误用 4K 上限、预览异常抛出、预览顺手写盘、无引擎也白算预览、write 预览不看原文件、开关没装回调、开关没抬 ask、默认值改成允许、没开的开关也生效。
  - **真实 LLM 跑过两个方向**（DeepSeek 官方通路，轨迹 `m9verify/review/`）：答「1 允许一次」→ 按 diff 改对（4 步、10755 token、缓存命中 71%）；答「4 拒绝一次」→ **`calc2.py` 磁盘上仍是 `return a - b`**，模型收到拒绝后如实回「编辑被权限系统拒绝了…补丁已准备好」（4 步、11377 token、命中 74%）。
  - **如实说明**：默认配置下这个 diff **到不了人眼前**（edit 默认 ALLOW、CLI 无确认交互），只有 `--review-edits`、或污染天花板收紧、或显式规则把工具抬成 ask 时才出现。看着像"做了个看不到的功能"，所以开关是这一项的必需部分，不是一个附加物。
- [x] **M9-2 ⑨ web fetch / search**：两个新工具 + SSRF 拦截，**并接上污染天花板**。
  - 现状：`ToolRegistry.default()`（`agent/tools/base.py:184-201`）里**没有任何联网工具** —— 于是 M7 的天花板收紧的三类不可逆动作中，「网络外发」那一类**打的是不存在的动作**
  - 收益：补工具面的同时，让一条已有机制第一次有真实对象（比新增机制更值得讲）
  - 验收：`pytest tests/test_tools.py`；真跑「查 X 并写进文件」

  **✅ 2026-09-11 完成。实现**：`agent/tools/web.py`（`WebFetchTool` / `WebSearchTool` / SSRF 判据 / 重定向守卫 / HTML→文本 / 内容编码解压）→ `permissions._irreversible_kind` 顶部的 web 分支 → `ToolRegistry.default()` 里加 `build_web_tools()`。
  - **判据的支点在「先解析、再判结果 IP」**：判 `hostname` 字面量是漏的。本机实测 `localtest.me` 解析到 `127.0.0.1`（字面量里一点内网字样都没有）；`2130706433` / `0x7f000001` / `127.1` 是 127.0.0.1 的十进制/十六进制/短写法，本机 Windows 的 `getaddrinfo` 恰好拒掉它们，但那是**操作系统的行为**，Linux 上会正常解析 —— 安全判据不能建在「目标平台恰好也拒绝」上面。解析失败 → **拒绝**（fail-closed）。
  - **`_is_internal` 的三处非显然判定（都是本机实测出来的，不是照抄文档）**：① `::ffff:100.64.0.1` 自己是 `is_private=False`/`is_reserved=False`，**只有拆开 `ipv4_mapped` 递归判**才拦得住；但 `::ffff:8.8.8.8` 必须放行，所以不能把整段 `::ffff:` 拉黑（两行都在判据表里）。② `100.64.0.0/10`（CGNAT）`is_private` 与 `is_reserved` 都是 False，得单列。③ NAT64（`64:ff9b::/96`）与 6to4（`2002::/16`）**整段**被 `is_reserved`/`is_private` 覆盖 —— 直接判会把合法映射一起拦掉，后果是纯 IPv6 + DNS64 网络上这两个工具**完全不可用**、且报的是「目标是内网/本机地址」这条**错误的**诊断。所以把内嵌的 IPv4 拆出来判（NAT64 在低 32 位、6to4 在第 16~48 位，位偏移取错就等于开一个洞，两种情况各钉了测试）。
  - **拦截的位置与拦截本身一样重要**：判据排在发请求**之前**（测试用一个「被调用就炸」的传输层钉着），`_opener()` 真的装了 `_GuardedRedirectHandler`，且**每一跳重定向都重跑一遍判据**。真实验证用的是**真实攻击形态**：`https://httpbin.org/redirect-to?url=http://169.254.169.254/latest/meta-data/` —— 首跳是货真价实的公网地址、能过首轮判据，在跳转处被拦住（`HTTP 302: 重定向目标被拦截`）。**只数跳数不校验目标是完全不设防的：一次跳转就够了**（Python 移植版把 `MAX_REDIRECTS` 注释成「限制重定向次数防止 SSRF」，那个上限对 SSRF 一点用没有）。
  - **接线是这一项的一半**：`permissions._irreversible_kind` 的 web 分支必须写在**取 `raw` 之前** —— 下面只取 `command`/`path`/`pattern` 三个键，web 工具的参数是 `url`/`query`，取不到就 `if not raw: return None` 直接返回。写在后面 = 两支永远不生效，**而这正好就是 M9-2 之前的原状**。有一条专门的变异体（「web 分支挪到取 raw 之后」）钉着这个位置。
  - **注册进 `default()` 的副作用已兑现**：`eval/runner` 也拿到了这两个工具，README 的评估数字因此**不可比**，已重跑（见下）。
  - **测试**：`test_tools.py` +73 例（SSRF 拦截/放行两张参数表、判据表、转换前缀取位表、fail-closed、拦截位置、重定向重校验与跳数上限、HTML→文本、内容编码 9 例、fetch 6 例、search 7 例）、`test_permissions.py` +6 例（**含一条不变量：三类不可逆动作各有一个触发工具真的在 `default()` 里**）。**449 全绿**。
  - **变异测试 31/31 被抓住**（`m9verify/mutate_m9_2.py`）。三类失效模式各覆盖：判据漏格（CGNAT / ipv4_mapped / 转换前缀 / 6to4 位偏移 / 解析失败放行 / 协议不查）、拦截位置（判据挪到发请求之后 / 不装重定向处理器 / 重定向不重查）、接线（web 工具不归类 / 归错类 / 分支位置错 / 不进 `default()`）。
  - **真跑挖出一个真 bug（单测没挖出来）**：抓 `python.org/downloads/release/python-3130/` 回来**一片乱码**。实测确认服务端在**我们没有请求压缩**的情况下回了 `Content-Encoding: gzip`（响应体以 `\x1f\x8b` 开头），而 `Content-Type` 是 `text/html; charset=utf-8` —— 于是 2MB 压缩字节顺利通过文本检查、被当正文解码后喂给模型。**乱码静默到达消费者**，正是本项目最忌讳的失效形态。修复：请求头加 `Accept-Encoding: identity`（减少该情况）+ `_decompress()` 真解压 gzip/deflate + **解压后同样封顶 `MAX_FETCH_BYTES`**（`MAX_FETCH_BYTES` 限的是读进来的字节，管不到解压之后 —— 几十 KB 的压缩炸弹能解出几 GB，两道限额缺一不可）+ **认不出的编码（`br`/`zstd`）如实报错，不退回原文**（退回原文等于把压缩字节当正文交出去）。`deflate` 在野外 zlib 包装与裸流两种实现都有，逐个试而不是猜一个。修复后真实复验：`m9verify/web/run_encoding.log` 记录了同一 URL 的**修复前（5100 字符替换字符）与修复后（`TITLE: Python Release Python 3.13.0 | Python.org` + 可读正文）**；另有一条端到端真跑（3 步 / 13,726 token / 缓存命中 70%）正确读出 `Release date: Oct. 7, 2024` 并写入 `release.md`。
  - **真实 LLM 端到端跑了四条**（DeepSeek 官方通路，工作区 `m9verify/web/`）：① 搜索→抓取→写入→读回（5 步 / 19,964 token / 命中 75%），产出的 `facts.md` 逐条对着来源核过；② 污染天花板：`hidden_text` 规则把 taint 抬到 high 后，`web_fetch` 与 `web_search` 被拒（0ms），模型转而用 `ask_user` 给出可执行的 `--clear-taint` 路径；③ `--resume --clear-taint` 解除天花板后完成（13 步 / 76,259 token / 命中 87%）；④ 上面的内容编码复验。
  - **如实说明（一）：默认搜索后端 `ddg` 在本机没有真跑校准过**。`lite.duckduckgo.com` 直连超时（8.2s）、走本机代理 SSL EOF（7.6s），两条路都不通（实测 2026-09-11）。默认选 `ddg` 是**对齐 TS 原版**（`web-search.ts` 走 DDG Lite）的刻意选择，不是因为它在本机好用；DDG 的解析正则按已知页面结构写，**没有对着真实响应校准**，代码注释与这里都如实标注，而不是让它看起来像验证过。真跑验证走的是 `CODEAGENT_SEARCH_BACKEND=bing`（Bing 解析是照着 98KB 真实响应写的）。
  - **如实说明（二）：一个只读并发批里，外发与污染读的判定有先后**。观察到的现象：第 2 步的 `web_search` 与那一步产生污染的 `read` 在**同一批**里并发执行，于是这次外发是按**批前**的污染级别判的。这是并发调度的物理顺序，不是漏洞（同一批里的调用本来就互为并发，没有"谁先"可依据），但它确实看起来像一个洞，所以写在这里、也写进 `docs/TECH_SPEC.md`。要严格堵住只能在批内串行判定并让整批重判，代价是只读并发这一条被废掉 —— 不值得。

### 第二批 · 兑现承诺 / 天然搭配

- [x] **M9-3 ⑦ fork + rename**：fork 挂在**我们的 step 级检查点**上 —— 可以从**任意一步**分叉，而 TS 的 fork 是**会话级**的。这是个能讲出真实差异、成本又低的点

  **✅ 2026-09-11 完成。实现**：`agent/session.py` 的元数据层（`meta_path` / `read_meta` / `update_meta` / `validate_name` / `set_session_name` / `session_name` / `SessionInfo` / `SessionNotFound` / `list_sessions` / `resolve_session` / `unique_session_id` / `default_fork_name`）+ `Session.fork` / `_copy_trajectory` / `_write_payload` → `app/cli.py` 的 `--sessions` / `--rename` / `--fork`，以及把"取会话 id"收成一处的 `_resolve_sid`。
  - **比 TS 版细一档**：TS 的 `/fork` 是**会话级**的（整段复制、从"现在"接着走），我们挂在 step 级检查点上，`--fork --step K` 能回到**任意一步**再开一条路。这不是多写出来的代码，是**检查点粒度的直接推论** —— 讲这一条比讲实现本身更有说服力。
  - **它是对话分叉，不是工作区分叉**（必须主动说）：文件**不会**回滚到第 K 步的样子，分叉后的 agent 看到的是**当前**工作区。我们**没有**工作区快照机制（`grep rewind|snapshot` 在 `agent/ app/` 下零命中，2026-09-11 核实）。CLI 分叉后**把这句打印出来** —— 一句会让人误判成"文件也回去了"的提示，比没有提示更糟。
  - **fork 搬三样，都以 fork 点为界**：① 检查点 `step-1..K`（于是新会话还能 `--resume --step` 回到其中任意一步）；② 轨迹里 `step <= K` 的行 —— **不能整份复制**，轨迹是追加的，源会话在 K 之后才发生的 `security_finding` 会被一起搬过去，分叉会话于是"继承"了它根本没经历过的事；③ meta 里的 `forked_from`（`--sessions` 用它显示血统）。**解析不了的轨迹行原样保留**：那是 kill 在写一半时唯一留下的现场。
  - **副本里唯一按新会话重写的是 `session_id`**。`load_state` 会 pop 掉它所以今天无害，但谁哪天直接读 `payload["session_id"]` 就会拿到源会话 —— 属"当前无人读、将来必有人读"的错值，在写入时修掉比留着便宜。
  - **`_write_payload(step, payload)` 把"怎么落盘"抽出来给 `_write` 与 `fork` 共用**：分叉写的是从别处读来的 payload，内容不由本会话的 state 决定，但原子写/缩进/编码必须是同一份知识 —— 各写一遍的失败方式是静的。
  - **`update_meta` 是合并式（read → update → 原子写）而不是覆盖式**：`--rename` 只该改名字，覆盖式会把 `forked_from` 一起抹掉且没有任何提示。文件自带 `session_id`（自描述）。**`read_meta` 与 `session_name` 对坏 meta 的反应刻意不同**：前者抛（文件在却读不出只可能是被手改坏，**名字是真丢了**，静默返回 `{}` 会让人以为"我从没起过名字"从而重起一个、旧的无声消失），后者返回 `None`（调用方只是要个显示名）；`--sessions` 逐行标出 `meta_error` —— 一个坏文件不该让另外九个正常会话也看不见。
  - **名字的两条判据合起来才成立**：写入侧 `validate_name` 拒绝「与任何已有 session_id 或会话名相同」，读取侧 `resolve_session` **先当 id、再当名字**。少了写入侧的拒绝，重名会让 `--resume --session-id <名字>` **安静地跑到先遍历到的那个会话上**（带着另一个任务的上下文继续）；名字等于某个 id 时那个 id 就永远解析不到自己。所以**不去读取侧加优先级"猜"** —— 猜错的代价是带着另一个会话的上下文继续跑。名字**从不参与路径拼接**（路径一律用 session_id），这条校验是防"看起来像地址"。
  - **`--sessions` 是这一项的另一半，不是附赠**：没有它，`--rename` 写的名字与 `--fork` 记的血统**没有任何消费者** —— 机制在、测试绿、文档写了，但没有任何东西把人引到它上面。这正是本项目的头号缺陷类（静默丢失 / wiring drift）。清单的键集合取「检查点目录 ∪ `data/sessions/*.jsonl`」，所以跑到一半被 kill、还没到第一个检查点的会话也在里面；`session_id` 生成是**秒级**的，同秒撞车靠 `unique_session_id` 让开（不报错、两个会话共用一个检查点目录、后者覆盖前者且全程静默）。
  - **`_resolve_sid` 是抽出来的，不是新写的**：「取会话 id」原先在 `_print_plan` 与 `--resume` 两处各写了一遍。加名字解析时只改一处、另一处照旧忽略 —— 那就是同一个缺陷类。收成一处，两条路径一起拿到（两条各有一个变异体钉着）。
  - **不需要 key 的路径**：`--sessions` / `--rename` / 不带任务的 `--fork` 都在 `_build_llm` **之前**处理完就退出（分叉不动模型）。`--fork` 后打印的续跑命令**必须可执行**（把新会话 id 原样带上）—— M7 的教训正是「一条走不通的解除指引比没有指引更糟」。
  - **测试**：`tests/test_session.py`（本项 22 例）+ `tests/test_cli.py`（本项 11 例）。**482 全绿**。其中两条是变异测试逼出来的：`test_fork_auto_name_uses_the_source_name_when_it_has_one`（原来的场景里源会话**没有名字**，"沿用名字"和"直接用 id"两条路都产出同一个值 → 变异假活）、`test_update_meta_writes_through_a_temp_file`（旧测试只看"没剩下 .tmp"，直接写也满足 → spy 了 `Path.write_text`/`Path.replace` 钉"写的经过"）。
  - **变异测试 36/36 被抓住**（`m9verify/mutate_m9_3.py`）。四类失效模式各覆盖：搬多了（检查点/事件越过 fork 点）、搬漏了（不重写 `session_id` / 不记 `forked_from` / 不沿用节拍 / 不记 `session_forked`）、元数据写坏（覆盖式写 / 坏 meta 静默 / 原子写 / 名字校验五条）、接线断了（`--sessions` 出口没接或列完不停 / `--rename` 不接 `--session-id` / 三条路径各漏一次 `_resolve_sid` / 报错信息退化）。**两类变异体需要说明**：① **等价变异体**「分叉时 state 取最新一步而不是分叉点」—— 拷贝循环只搬 `n <= fork_step` 且 `fork_step` 必在其中，两句**恒等**，已从脚本里删掉并写明理由；② **被别处顺手挡住的**「允许空名字」—— `_checkpoints_root / ""` 正好解析回检查点根目录本身（当然存在），所以去掉空名保护**照样抛 ValueError**，只是理由从"不能为空"变成"已被 占用"。**只断言异常类型的测试抓不住这种退化** —— 改成钉**拒绝理由**（`match=`）之后这条才重新有效，同时把该变异体换成纯净可观测的「名字不做 strip」。
  - **真实 LLM 端到端跑通（工作区 `m9verify/fork/`，DeepSeek 官方通路）**：素材是一份有三个独立 bug 的 `textutil.py`。① 种会话（4 步 / 15,805 token / 命中 **76%** / 4 个检查点）：模型读文件→`edit` 修 `slugify`→**`bash` 实跑验证**，输出 `'hello-world-this-is-python'`；② `--rename "基线方案"`；③ `--fork --step 2 --rename "另一条路"`（**不动模型**）→ 打印"搬了 2 个检查点"+ 对话分叉警告 + 可执行续跑命令；④ `--resume --session-id "另一条路"` —— **用名字**恢复到第 2 步 → 续跑到第 6 步（20,288 token / 命中 **81%** / 5 个检查点），自己写了 `test_textutil.py` 并 `pytest` **9 passed**；⑤ 事后核对：源会话仍是 `[1..4]` 逐字节未动，分叉是 `[1..5]`，**副本与源逐键比对只有 `session_id` 不同、`state` 完全相同**；分叉轨迹里来自源的部分恰是 `step<=2` 的 8 条，源在第 3 步之后的事件**一条都没被搬过去**。
  - **真跑挖出两个接口缺口（单测全绿、真跑才暴露）**：① `--fork --step K` 的 K **可能压根没落盘**（检查点按节拍落，`--checkpoint-every 2` 的会话只有偶数步），原来报的是一个裸文件路径 —— 现在 `_load_payload` 统一报 `第 K 步没有检查点（可用: [2, 4]）`，**分叉与续跑共用这一个入口**，一条守卫管住两条路；② **抛出侧只是一半**：三条入口各自要接住它，而真实跑发现**只有最常用的 `--resume` 没接** —— `--resume --step 9` 甩出一整个 traceback（`--fork` / `--plan` 早就各有一句人话）。同一个错误在三条路径上有三种表现，正是"两处各写一遍"的典型形状。
  - **顺带修掉一处静默失效的 `.gitignore`**：`.pytest_tmp*/.coverage` 是两条规则被粘成了一行，于是 `.pytest_tmp*/` **从未生效**（本文件开头那条警告说的正是这个坑，自己又踩了一次）。
  - **一处我自己造的假 bug（记下来，形态比 bug 本身更值得记）**：全量日志最后一行是 `[100%]` 就没了、**没有 `NNN passed in …` 汇总行**，我据此断定"汇总被吞了"，一路追到 `streamlit.testing.v1` 接管 `sys.stdout`，还照这个结论往 `tests/conftest.py` 加了个 autouse fixture。**全错**：`pyproject.toml` 的 `addopts` 里已经有 `-q`，我每条命令又传一个，合起来是 **`-qq`** —— pytest 在这个详细度下**本来就不打印汇总行**。`-o addopts="" -q` 立刻就有。fixture 已撤销，判断依据写进 `CLAUDE.md` 常用命令下面。
    教训与 M7/M9-2 那几条**方向相反**：那些是"测试绿了、机制其实没工作"（我把机制想得太好），这条是**"工具正常、我把工具想得太坏"** —— 同一个毛病的另一面：**在归因给库/框架之前，先穷尽自己的调用方式**。附带收获是数字因此对上了：汇总行一回来就看见是 **482** 而不是文档里的 481（我在统计之后又补过测试），源码 7,728 / tests 8,022 一并核准，四处文档同步。
  - **如实说明**：分叉的**默认名**用的是源会话名（`{源名} @{步} 分叉`），未起过名的会话回落成 `{源 id} @{步} 分叉`；重名时自动让开（`-2`/`-3`）而不是报错。**没有做**：工作区快照（所以它是对话分叉）、跨工作区分叉、把 `name` 塞进 `AgentState`（名字是**人给的标签**，不是"任务跑到哪了"，放进去等于让 state 多两个既不影响推理又要跟着全字段往返一起走的字段）。
- [ ] **M9-4 ⑩ MCP 远程 HTTP + resources/prompts**：面试稿 §10「下一步」第一条**已经承诺了** HTTP/SSE，做了就是兑现；且「协议层复用、只换传输」正是分层设计的证明

### 第三批 · 需要前置

- [ ] **M9-5 常驻交互模式（REPL）** —— ④⑤ 的**硬前置**
  - 现状核实（2026-09-11）：`app/cli.py` 是**单发**的，没有 `while True` / `input()` / REPL，跑完即退
  - 「跨回合自动推进」「每 N 分钟重复提示词」在一次性进程里**没有意义**，所以 ④⑤ 排在这里之后
  - 顺带收益：面试稿 §12 的演示动线不再全是单发命令
- [ ] **M9-6 ④ Goal**：进程内 Goal + 暂停/恢复 + **显式完成检查**（后者的判分思路与我们的评估层呼应）

### 压轴 · 价值最高，但唯一会动核心循环契约

- [ ] **M9-7 ② sub-agent 并发 3 + wait/close**
  - 现状：工具协议是「同步 `execute` → `ToolResult`」（`agent/tools/base.py`），要引入「后台任务 + 句柄」就得**改这个契约**
  - 还要与已有的「只读并发 / 写串行」语义协调，并防递归（我们现在的做法是 `_restricted_registry` 永不包含 subagent 自身，从结构上禁掉）
  - 形状参照 `agents/manager.ts`：并发上限 + `wait` + `close` + 独立上下文 + 受限工具集 + 可取消
  - **这是简历上最值钱的一条**（多智能体编排是 Agent 岗最热的考点），但它不该是起步项

### 明确不做（理由留痕）

- **⑥ 全屏 TUI**：纯终端渲染的体力活，面试加分有限；我们已有 Streamlit 控制台可演示（且已在真实浏览器里跑通过）
- **⑤ Loop**：价值低（本质是个定时器）；REPL 做出来之后顺手做，**不单独排期**
- **⑧ context collapse**：**测过同类机制收益不达预期，所以不做**。分级截断与本项同为「改动 `_PREFIX_LEN` 之后窗口」的机制，实测（P7-d）：单次省 5,128 token ↔ 多付 **45,304 miss token（8.8 倍）**，代价形状是后缀失效。这与我们**唯一的缓存亮点**直接冲突 —— 拿一个反例数据说明「不做」，比照着原版补上更值得讲

---

## 进度快照

- 当前里程碑：**M9 进行中**（第一批 M9-1、M9-2 已完成并真跑验证；第二批 M9-3 fork + rename 已完成并真跑验证；下一项 M9-4 MCP 远程 HTTP + resources/prompts）
- 上一里程碑：**M8 完成**（P1–P7 全部完成；四项真实验证已跑，见上节）
- 上一里程碑：**M7 完成**（M7-1~M7-6 + Part C 全部完成；四条真实 LLM 端到端验证 V1–V4 已全跑，工作区 `m7verify/`、`m7verify-nolock/` 已 gitignore）
- 代码状态：**7,175 行源码 / 25 模块 / 449 测试全绿**（`python -m pytest tests/` → `449 passed`）
  - 口径：源码 = `agent/` + `app/` + `eval/` 里被 git 跟踪的 `.py` 行数（不含 `eval/repos/` 克隆仓）；模块数 = 其中非空的 `.py` 文件数
- **M8 期间发现并修复的真 bug（真跑挖出来的，不是单测挖的）**：
  1. **`update_plan` 的引导缺失（elicitation gap）**：工具实现了、测试全绿、计划也能落盘 —— 但 system prompt 里**一个字都没提它**，模型 6 步跑完一次都没调。修法：prompt 里写明"任务复杂时先调 `update_plan` 排一份 3~6 步的简短计划"。修前 0 次 / 修后 2 次（同 P7-c 表）
  2. **`--resume` 静默丢掉 `checkpoint_every`**：`from_checkpoint` 的默认值是 5，而 `--resume` 这条路径没转发 → `--resume --checkpoint-every 1` 静默回落成"每 5 步一次"。后果不是报错，而是**恢复出来的这一段一步都不落盘**（kill 在 step 5，续跑到 step 9，检查点数还是 5）。修好后同一段跑出 `[1..10]`
  3. **恢复期的三条事件进不了轨迹**：`record_event` 只在 `state.emitter` 非空时写 JSONL，而 emitter 原先要等 `run_from` 才被引擎接上 —— 于是 `taint_cleared` / `plan_resumed` / `resume_instruction` 一条都落不了盘（`clear_taint` 注释里写的"同时往轨迹里记一条"因此是假的）。修法：`restored.emitter = session.emit` 先接上再记
  - 三条都是**同一个缺陷类**：机制在、测试绿、真跑才发现没生效。回归测试 `tests/test_cli.py::test_resume_honors_checkpoint_every`、`::test_resume_events_reach_the_trajectory`、`tests/test_loop.py::test_default_system_prompt_points_at_update_plan`，并逐条做了变异（3/3、2/2 被捕获）
- **已拍板（2026-09-11，用户决定）**：
  - **第 0 级分级截断：保持现状**（选项 a）。数据见 P7-d 结论第 4 条 —— 端到端命中率没降、代价被同一步的 LLM 摘要遮蔽，所以不为了微观对照里那 8.8 倍去改取向。**明确不选**的是 (b)「优先截最靠后的合格消息」：(b) 的缓存账更好看（代价降到 1/3.8），但它会**先丢掉最老的上下文**，而"最近的最相关"是比缓存算术更硬的设计约束；也不选 (c) 去掉，因为尾部结论行 8/8 保住这条收益是实测为正的。
  - **`--resume` 的检查点节拍：已修**（见下面第 4 条）。取向定为 **显式传参 > 会话里记的 > 默认 5**，并把实际生效的节拍**打印出来**（这是这次改动唯一的可见性出口）。
- **M8 真跑验证挖出来的真 bug（第 4 条）**：
  4. **`--resume` 不传 `--checkpoint-every` 时回落 CLI 默认值**：第 2 条修掉的是"传了不生效"，这一条是它的另一半 —— "不传就用 CLI 的默认 5"。**后半更难发现，因为 5 凑巧也是个合法节拍**：命令成功、输出正常、退出码 0，唯一证据是检查点数不涨。而 5 是 **CLI 的默认值，不是这个会话的事实**。修法：节拍随检查点落盘（`payload["checkpoint_every"]`），`from_checkpoint` 的 `checkpoint_every=None` 表示"沿用该会话当初的值"；老检查点没有这个字段 → 回落 `DEFAULT_CHECKPOINT_EVERY`；值非法（手改过）→ 同样回落，**不夹成 1**（对一个已损坏的检查点做出"每步都写"这种更激进的行为是错的方向）。
     - 回归测试：`tests/test_session.py::test_checkpoint_records_the_cadence` / `::test_resume_inherits_the_session_cadence` / `::test_resume_falls_back_for_legacy_checkpoints` / `::test_resume_ignores_a_corrupt_cadence`、`tests/test_cli.py::test_resume_without_the_flag_inherits_the_session_cadence`
     - 变异测试 4/4 被捕获（不落盘 / 不读 / 不校验 / 不转发显式传参）
     - **顺带发现一条测试自己骗自己**：`test_resume_honors_checkpoint_every` 原先第一段用 `--checkpoint-every 1`，而这正是会话会记住的值 —— 于是"显式传参没被转发"会因为**沿用了会话里那个同样是 1 的值**而假装通过。改成第一段用 2、显式传 1，两条路才分得开（未改时变异结果显示 `[2, 4]`）。
- 验证中发现并修复的真 bug（M7 期间）：
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
  6. **默认端口 8501 在本机起不来**（2026-09-10 挖出）：`streamlit run` 默认 8501，但本机 Windows 保留了 `8457-8556` 端口段（`netsh interface ipv4 show excludedportrange protocol=tcp`）→ 绑定报 `WinError 10013`（权限不允许），日志只说 "Port 8501 is not available"。**这大概就是控制台一直没在浏览器里真开过的原因。** 绕法：`--server.port 8600`（不在任何保留段内）。README 的快速开始已加此提示。
  7. **MCP 只用自建假 server 验过**（2026-09-10 补验）：原来只有 `tests/fake_mcp_server.py`。现改用 **modelcontextprotocol 官方 `mcp-server-time`**（PyPI `mcp-server-time 2026.8.18`，`python -m mcp_server_time`）真实跑通三段：
     - **协议层**：握手成功（serverInfo `mcp-time`，协议版本 `2025-06-18` 与客户端声明一致）→ `tools/list` 拿到 2 个工具（`get_current_time` / `convert_time`，均声明 `readOnlyHint=True`）→ `tools/call` 返回真实时间
     - **端到端**：`python -m app.cli --mcp <config> --step 8 "用 MCP 工具查上海时间并写进 shanghai_time.txt"` → 步 1 直接调 `get_current_time`，步 2 写文件、步 3 回读确认，4 步 `completed`
     - **门禁链（最关键）**：三组对照证明确实**不是绕过治理的后门** —— A 组（无权限无 hook）**放行**；B 组（`{"tools": {"get_current_time": "deny"}}`）被**权限拒绝**；C 组（PreToolUse hook 阻断）被 **hook 拦下**。附带收获：B/C 两组里模型如实回答"没能拿到时间，也不会凭空编"，没有编造时间
- 已知待改进（未修）：
  - ~~**真实跑分用的是临时通路，待换官方口径重跑**~~ ✅ **已完成（2026-09-10）**：用 DeepSeek 官方 `deepseek-chat` 重跑 `python -m eval.runner --limit 2` → 完成率 50%（1/2）、62,812 token、¥0.0625、缓存命中 83%；README 的跑分与缓存曲线已全部换成官方口径（曲线为真·冷启动：step1 0% → 累计 77%）。历史 DashScope 数字只作为口径说明里的对照保留，并注明不可直接比。
  - **M6-7 的真实模型验证已补跑**（2026-09-10，DeepSeek 官方 `deepseek-chat`）：修 bug 任务 **5 步**修好（10,112 token / 缓存命中 73%）；kill → `--resume` 恢复后**第 4 步直接 `edit(path=calc.py)`**，无重新探路、无磁盘遍历，续跑至 11 步完成（缓存命中 90%）。轨迹扫描「瞎猜路径」4 类模式（`/workspace`、`C:\Users\<字母>`、`dir C:\Users`、全盘搜索）**无命中**
  - CLI 流式输出这条**已真实可见**（事件随步实时打印）；system prompt 注入工作目录这条的收益**见上条第 3 点的更正口径**
  - ~~Streamlit 控制台没在浏览器里真开过~~ ✅ **已完成（2026-09-10）**：用真实 Chrome（CDP 驱动）打开 `http://localhost:8600`，**并操作控件跑通了一个 mock 任务**——勾 mock、输入任务、点"开始任务"，页面出现"事件日志（3 条）"[步1] glob → [步2] 0 工具调用、缓存命中率曲线、会话 `s20260910-194424`、检查点回放、"✅ 最终结论"与"终止原因 completed · 步骤 2"。**控制台在真实浏览器里可用，不只是 AppTest 能过**
  - ~~真实 token 压力下的两级 compact 从未触发~~ ✅ **已完成（2026-09-10，当时流水线只有两级）**：`token_budget` 调小（生产 64,000）后用**真实 provider usage** 驱动，两级都真实触发：
    - `BUDGET=3400` → 步 9（77.0%，20 条消息）与步 11 **确定性 snip** 触发（消息 20→15、19→15），stale=`snip_compact`
    - `BUDGET=2600` → 步 9（100.7%）与步 11 **LLM 摘要 compact** 触发，stale=`llm_compact`（不是退化成 snip）
    - compact 之后的步 10、11 LLM 调用均成功 → 压出来的消息序列合法（没有孤儿 tool 消息），否则 API 会 400
    - **顺带挖出一个真约束**：光有 token 压力不够。`_find_cut` 要求 `len(messages) - keep_recent > min_keep`（即 **>18 条消息**）才可能存在合法切割点。实测步 8 时 util 已 72.9%（18 条消息）但 `cut=6` 不合法 → **不触发**；步 9 消息涨到 20 条才触发。这解释了为什么"上下文只到 10%"和"util 100% 也不触发"是两回事
    > ✅ **已在 P7-d 复测（2026-09-11）—— 上面这组记录原样成立，三级时代没有位移**。
    > 担心的机制是「第 0 级先把 utilization 压回 0.70 以下 → snip 不再触发」。复测用同一场景（真实 provider usage、小输出、逐步读文件）跑了两个预算：
    > `3,600`（步 9 util 0.81，落在 snip 带）→ **snip 3 次 step [9,11,13]、摘要 0 次**；`2,600`（步 9 util 1.13，越过 0.85）→ **snip 0 次、摘要 3 次 step [9,11,13]**。
    > **两个预算下第 0 级"有工作可做"的次数都是 0** —— 工具输出只有 160 字符，而 `TRUNCATE_BUDGETS["read"] = 4,000` 字符，一条超预算的都没有（真实小仓库就是这一档，本仓库真跑时最大 430 字符）。所以它没有改变任何一步的判定。
    > 大输出场景另见 P7-d 的区间①②：第 0 级确实释放了 token（3 次 / 5 次），但摘要仍在**完全相同的步**触发 —— 追不上的算术写在 P7-d 结论第 3 条。

  - **CLI 接权限引擎与 hooks**（2026-09-10 发现 → 同日修）：`app/cli.py` 构造 `QueryEngine` 时既没传 `permissions=` 也没传 `hooks=`，只有 `app/ui_streamlit.py` 接了。后果：README/CLAUDE.md 宣传的「危险命令拦截 + block-at-submit hook」在 **CLI 路径上不生效**（Streamlit 路径生效）。
    - **权限**：两处 `QueryEngine(...)`（正常分支 + `--resume` 分支）都传 `permissions=PermissionsEngine(workspace_root)`。**不传 `confirm`** → 引擎对危险命令给 `ask`，loop 在无确认交互时按安全默认拒绝——与接入前的行为一致，只是判定改由引擎统一做。
    - **hooks**：两个入口改为共用 `hooks.default_engine(workspace_root)`（一处构造，避免再次漂移）。**默认规则不变**：普通工具/命令仍 `allow`；`bash` 工具自身的危险命令兜底只在「无权限引擎」时生效，现在由引擎接管（行为仍是拒绝，文案变化）。
    - **顺带修掉 marker 的「自欺」问题**：block-at-submit 原来只检查 `data/tests_pass.marker` 是否存在，而**没有任何代码会自动写它**（`mark_tests_pass()` 只被测试调用）——模型被拦后只能自己 `write` 出这个文件，等于不跑测试也能解锁。新增 `mark_tests_pass_on_success`（PostToolUse）：**测试命令真跑成功（退出码 0）才写 marker，失败则清除**，非测试命令不动（跑个 `ls` 不该解锁提交）。真实跑通：模型跑 `pytest tests/ -q` 得退出码 4（没有 tests/ 目录）→ **未解锁**；改跑 `python -m pytest -q` 得 0 → 解锁 → 提交成功。
    - **测试**：`tests/test_hooks.py` 新增 6 例（marker 写入/清除/非测试命令不碰/多种 test runner 识别/`default_engine` 一条链走完/真 pytest 端到端）；`tests/test_cli.py` 6 例（真跑 CLI + 引擎替身：危险命令 `ask` 且模型收到「权限拒绝」、普通工具 `ALLOW`、路径越界 `DENY`、`--resume` 分支同样接线、权限与 hooks 两层都接上）。
    - 沙箱本身没漏（`files.py` 每个路径都 `_resolve` 校验、`bash.py` 的 cwd 校验无条件生效）——漏的是规则引擎与 hooks 这两层，现已补上。
  - **Windows 上 bash 工具把双引号转义坏掉**（2026-09-10 接 hooks 时顺带挖出，**已修**）：`subprocess.run(["cmd", "/c", command])` 是列表参数 → Windows 上走 `subprocess.list2cmdline`，它把 command 里内嵌的 `"` 转义成 `\"`，cmd 收到的是字面反斜杠+引号。后果不是显示乱码而是**命令直接失败**：`git commit -m "feat: x"` 实测报 `error: pathspec '…"' did not match any file(s)`（git 把消息后半段当成 pathspec），`python -c "..."` 同理。修法：win32 上传**字符串** `f"cmd /c {command}"`（不经 list2cmdline，原样交给 CreateProcess）。回归测试 `tests/test_tools.py::test_bash_preserves_double_quotes`。这个 bug 正是接 hooks 才暴露的——block-at-submit 的演示动线就是 `git commit -m "..."`。
  - **污染天花板在实践中是空转的**（2026-09-10 真跑 V3 时挖出，**已修**，commit `a455dda`）：`_irreversible_kind` 的 bash 凭据分支要求「读动词 + 凭据路径」同现，动词表是 `cat|type|head|tail|less|more|Get-Content|gc`。模型读 `.env` 用的却是 `findstr /r /c:"^[A-Za-z_]" .env`（Windows 上 `grep` 的自然替代）——**不在表里**，于是天花板没生效：命令正常执行、变量名进了上下文，轨迹里连一条 `gate_block` 都没有。
    - 第一版修法是「去掉动词表、只按锚定的凭据路径判」，结果 `copy .env x.txt` 漏了（末尾是 `x.txt`）——而「把凭据文件当输入写到别处」正是最典型的带离手段。
    - 最终判据收敛成一句话：**high 会话里，提到凭据文件的 bash 命令都要人工确认**（新增 `CREDENTIAL_MENTION`，不锚定末尾；read/write/edit 仍用锚定的 `CREDENTIAL_PATH`）。
    - **这条的意义不在于修了一个正则，而在于它是一个「单测全绿但机制实际不工作」的样本**：原有的权限测试全部通过，因为测试里用的是 `cat .env` —— 我自己写的测试和我自己的判据共享同一个盲区。只有真跑真模型才会用出 `findstr`。回归测试 `tests/test_security.py::test_ceiling_catches_credential_read_whatever_the_verb` 钉住了 15 种写法（含真跑出现的那条 `findstr`）。
  - **`--resume "补充说明"` 静默丢掉这个参数**（2026-09-10 真跑 V3 支线 Y/Z 时挖出，**已修**，commit `a455dda`）：拒绝文案让用户「用 `--clear-taint` 复位标记再重试」，但 `run_from` 用的是 `state.task`，位置参数被直接忽略 —— 复位之后 CLI 没有任何办法把「我已复位，请重试」送进会话。真跑时就是这么卡住的：模型按拒绝文案的指引停下等人，人却回不了话，整条「收紧 → 人解锁 → 重试」的动线断在最后一步（轨迹显示标记确实翻过来了，但模型永远没重试）。
    - 修法：`task` 非空时作为一条 user 消息追加进会话 + 记一条 `resume_instruction` 事件 + 控制台打印「续跑指示: …」，并把参数 help 改成「`--resume` 时作为**续跑指示**追加进会话」。回归测试 `tests/test_cli.py::test_resume_instruction_is_delivered_not_dropped`。
    - **同类教训**：这两个 bug 都是「机制写了、单测过了、真跑才发现没生效」——诚实记录它们的价值高于多写十条单测。

