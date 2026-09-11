# MiniCode 架构笔记（第三参考，TS 实现）

> 调研对象：[LiuMengxuan04/MiniCode](https://github.com/LiuMengxuan04/MiniCode)（本地 F:\MiniCode-main）
> 调研日期：2026-09-10
> 参考定位：**长会话上下文治理**（它的产品核心）+ 分层记忆 + 权限粒度 + 工具结果落盘。
> 该项目也是简历对照对象（原 MiniCode 简历的 Skill/记忆/上下文压缩/多Agent/权限五项在源码里都能对上）。
>
> **本地另有一份第三方 Python 移植 `F:\MiniCode-Python-main`**（本人未参与，2026-09-11 核实）：
> - 自有包 `minicode/` **59,852 行 / 140 个 .py**；整仓 118,632 行 / 349 个 .py（含 `tests/` 28,518、`benchmarks/` 2,296、`Package/` 5,474）。
> - **授权情况（别写错）**：TS 原版 `F:\MiniCode-main` 是 **MIT**（`LICENSE`，Copyright (c) 2026 Liu Mengxuan）。Python 移植**仓库根与自有包 `minicode/` 都没有 LICENSE**（默认保留所有权利）；它目录下的 `ts-src/` 是 TS 原版的副本，带着原版那份 MIT。
> - 因此本项目的取舍**不是**"因为完全没授权所以不能看"，而是：**① 它自有的移植部分未声明授权；② 就算授权允许，照抄也会让「核心循环手写」这个作品集定位失效**（"这部分是你写的吗"将无法回答）。所以：**只作设计参考，不复制任何代码**，见 §8。
> - 它的 `minicode/context_manager.py` 有 1056 行，**明确不整块移植**（理由见 §8 末尾）。

## 1. 整体结构（TS，`src/` 15,840 行 / 77 文件）

```
src/
├── agent-loop.ts          # 核心循环（runAgentTurnWithOutcome）
├── tool.ts                # 统一工具协议 {schema(zod), execute}
├── tools/                 # list_files/grep/read/write/edit/patch/modify/run_command/web/ask_user/load_skill + sub-agents
├── compact/               # ★ 上下文治理（约 2000 行，最大投资）
│   ├── constants.ts       # 阈值集中管理
│   ├── microcompact.ts    # 轻量 token 记账
│   ├── snipCompact.ts     # 确定性中段裁剪（无 LLM）
│   ├── context-collapse.ts# LLM 辅助的上下文折叠投影
│   ├── auto-compact.ts    # LLM 摘要压缩（critical 才触发）
│   └── compact.ts         # 摘要压缩核心（boundary 对齐 + stale usage）
├── utils/
│   ├── token-estimator.ts # ★ provider-usage-first 记账 + 尾部估算
│   └── tool-result-storage.ts # ★ 超大工具结果落盘 + 预览替换
├── memory.ts              # ★ 分层指令文件（MINI.md/CLAUDE.md/rules + @include + 去重 + 预算）
├── permissions.ts         # 路径/命令/编辑三类权限 + 6 级决策
├── agents/manager.ts      # sub-agent 生命周期（并发 3、spawn/wait/close、AbortController）
├── session.ts             # JSONL 会话 + parentUuid + compact boundary + resume
└── tui/                   # 全屏终端 UI（Ink/React）
```

## 2. 核心循环（agent-loop.ts，与我们 QueryEngine 同构 + 恢复机制）

每步前的上下文流水线（按序）：
1. `snipCompactConversation`（utilization>70% → 目标 60%；确定性移除中段，保留最近 12 条，至少删 6 条，释放 ≥2000 token；插入 `snip_boundary` 标记消息）
2. `microcompact`（消息级记账）
3. `applyContextCollapseIfNeeded`（LLM 识别"可摘要片段"如重复工具结果 → 替换为摘要；目标 65%，每趟 ≤2 段，连续失败 3 次禁用）
4. `autoCompact`（仅 step 0 且 critical/blocked：LLM 摘要压缩，boundary 对齐完整 API 轮次，保留 system+summary+tail，压缩前 usage 标记 stale）
5. 注入 plan snapshot / runtime context

**模型响应恢复机制**（我们没有的）：
- 空响应重试（≤2 次）+ continuation prompt
- pause_turn/max_tokens thinking 恢复（≤3 次）
- 纯文本且有工具结果 → 当 progress 处理 + 续写 prompt

工具执行：**串行**（MiniCode 主循环无并发；并发只在 sub-agent，最多 3 个）。

## 3. ★ provider-usage-first token 记账（token-estimator.ts）

- 每条 assistant 消息附带 `providerUsage`；`tokenCountWithEstimation` 从尾部找最近一条有效 usage → `total = provider_total + 估算(tail)`
- `estimateMessageTokens`：按角色给 字符/token 比例（system 3.5 / user 3.0 / tool_result 2.0 ...）
- warningLevel：normal(<50%) / warning(≥50%) / critical(≥85%) / blocked(≥95%)
- compact 后把保留消息的 usage 标记 stale（`usageStale=true`），避免用旧 usage 计新上下文

## 4. ★ 超大工具结果落盘（tool-result-storage.ts）

- 单条 >50K 字符 → 落盘到 `~/.mini-code/tool-results/{session}/{id}.txt`，上下文里替换为：
  `<persisted-output>` + "Output too large (N chars). Full output saved to: path" + 前 2000 字符预览 + `</persisted-output>`
- **每轮工具结果批次预算 200K**：即使单条没超 50K，批内总量超限时按"最大优先"把大结果落盘
- 同一次运行内替换结果复用（replacements map）
- 相比"截断丢弃"，落盘 + 路径让模型/用户能随时读回完整输出（cc 也是这个思路的加强版）

## 5. 分层记忆（memory.ts）

- 发现顺序：用户全局（home/MINI.md 或 CLAUDE.md，只取一个）→ 全局 rules/*.md → **每个祖先目录**（MINI.md / MINI.local.md / .mini-code/MINI.md / CLAUDE.md / CLAUDE.local.md / .claude/CLAUDE.md + .mini-code/rules/*.md）
- `@path` include：相对路径、拒绝绝对/`..`、循环检测、缺失给占位注释
- 内容 hash 去重（靠后/靠 cwd 的优先）
- 预算渲染：每文件 ≤8K 字符、总计 ≤20K，超限截断 + 提示

## 6. 权限（permissions.ts）

- 三类请求：path / command / edit；每类有 allowlist/denylist
- 决策粒度：`allow_once / allow_always / allow_turn / allow_all_turn / deny_once / deny_always / deny_with_feedback`（我们 M2 原计划只有 allow/deny/ask，太粗）

## 7. sub-agent（agents/manager.ts + tools/sub-agents.ts）

- `spawn_agent`（独立消息数组 + AbortController + 受限工具集：只读 + web + load_skill）/ `list_agents` / `wait_agent` / `close_agent`（取消进行中的模型请求）
- 并发上限 3；worker 不能写文件/跑命令/再建 agent → 所有修改由 root 完成

## 8. 对我们的影响（吸收清单）

| MiniCode 设计 | 本项目是否吸收 | 用在哪 |
|---|---|---|
| **超大工具结果落盘 + 预览**（替代纯截断） | ✅ 吸收 | M3 context.py + 改造 M1 的 _truncate |
| **provider-usage-first 记账 + 尾部估算 + warning 分级** | ✅ 吸收 | M3 context.py（叠加我们的 cache-aware 布局 = 差异） |
| **空响应重试 + continuation prompt** | ✅ 吸收 | M1-6 loop.py（小改动，现在补） |
| **确定性 snip compact（中段裁剪）** | ✅ 简化吸收 | M3：70% 触发、保留最近 N 条、LLM compact 前先试它 |
| **LLM 摘要压缩（boundary 对齐 + stale usage）** | ✅ 吸收 | M3：critical 才触发 |
| **分层记忆 + @include + 去重 + 预算** | ◐ 简化吸收 | M4：项目根 + rules + @include，不做全局 home 层 |
| **权限决策粒度（once/turn/always）** | ✅ 吸收 | M2 permissions.py |
| **工具输出分级截断**（按工具给不同预算；先缩内容，缩不够再删） | ✅ 吸收（2026-09-11），**但实测收益比预期小** | M8 context.py 第 0 级；**我们加了它没有的两条**：失败结果给更大预算、head70/tail30 保尾部结论行。**实测（P7-d）**：端到端命中率没降（A/B 差 ±0.02% 以内），但单次截断要付 **8.8 倍的 miss token**（后缀失效，截得越靠前越贵，差 3.8 倍），且**免不掉第 1/2 级**。**去留已拍板（2026-09-11）：保持现状**，数据见 `TASKS.md` P7-d |
| **skills 渐进披露**（SKILL.md：只 name+简介进 prompt，正文按需 load） | ✅ 吸收（2026-09-11） | M8 `agent/skills.py` + `load_skill` 工具 |
| **`ask_user` 提问暂停**（awaitUser 标志位而非阻塞） | ✅ 吸收（2026-09-11） | M8 `agent/tools/ask.py`；**我们改成"headless 下不注册"来降级**，它没有这条 |
| **Plan 持久化**（每次传完整列表 + 落盘） | ✅ **吸收（决定反转）**（2026-09-11） | M8 `agent/tools/plan.py` + `state.plan`。**反转理由**：原判"TUI 产品特性"是按它 TS 版的形态下的（步骤快照注入对话 + 全屏 UI 回放）；真正与产品耦合的是**注入**，不是**持久化**。我们只取持久化，**不学它每步把 plan 注入消息**——那会破坏 `_PREFIX_LEN` 之后的前缀缓存。Goal/Loop 维持 ❌ |
| 上下文折叠投影（LLM 识别可摘要片段） | ❌ 不做 | 复杂度高；cache-aware + snip + compact 覆盖主要收益 |
| thinking progress 恢复 | ❌ 不做 | DeepSeek 无 thinking block |
| Goal / Loop 持久化 | ❌ 不做 | 维持原判：TUI 产品特性，非核心技术 |
| sub-agent 并发 3 + wait/close 生命周期 | ◐ 参考 | M4 保持单 research 子 agent，但学"独立上下文+受限工具+可取消" |

### 明确不吸收（2026-09-11 逐项核实后补）

写下来是因为"看起来能用但实际不该搬"的判断**必须留痕**，否则下次调研会重新评估一遍、或者更糟——只看到行数就搬进来。

| 对象 | 规模 | 不吸收的理由 |
|---|---|---|
| `context_manager.py` | 1056 行 / 20+ 调用点 | 它把记账、截断、snip、摘要、折叠**揉在一个类**里，且第 4 阶段对全量消息做 O(n²) token 重算。我们只摘思路（分级截断按工具给预算），实现照自己的 `context.py` 分工写 |
| Python 版 `tools/task.py` | — | 同步执行、无并发，移植要动运行时入口；收益不抵风险 |
| Python 版 `task_graph.py` | — | DAG 能力在它自己代码里也没有真实调用方 |
| Python 版 `task_tracker.py` | 349 行 | **死代码**：全仓无调用方 |
| Python 版 `todo_write.py` | — | 有真 bug：`_tasks.clear()` 在遍历循环**之前**执行 → `existing` 恒为 None，更新分支是死代码。我们照自己的设计重写，不搬它的 |
| `agents/manager.ts` 的子代理并发 | — | 只有 TS 版有；要做是自己照设计写，不在本次范围 |

## 9. 我们的差异化（MiniCode 没有的，保留并强化）

1. **主循环只读工具并发**（MiniCode 主循环串行）——CC 特性，继续做
2. **Cache-aware 消息布局 + DeepSeek 缓存命中指标**（MiniCode 没有缓存概念）——省钱曲线是简历亮点
3. **Block-at-Submit hooks**（MiniCode 无 hooks）
4. **轨迹驱动评估**（MiniCode 无 eval）——完成率/成本回归报告
5. **记忆自进化**（MiniCode 只有加载无提取/consolidation）
6. **step 级检查点/崩溃恢复**（MiniCode 是会话恢复，无任务中途续跑）

第 2 条（cache-aware 布局）在 M8 之后还多了一层意义：它从"一个亮点"变成了**一条筛选规则** —— 移植任何机制前先问"它会不会改动 `_PREFIX_LEN` 之后的消息"。`update_plan` 就是被它挡下来一次的例子（我们只取落盘，不取参考实现那种每步注入 plan snapshot 的做法）。这条判据比"参考实现这么做"更硬：参考实现没有缓存概念，它的做法在 DeepSeek 的 prefix cache 下是负收益。

## 10. 12 项「核心能力」逐条对照（2026-09-11 核实）

对照对象是 TS 仓库 `README.zh-CN.md:125-137`「核心能力」一节的 12 条。核实方式：逐条在**三份代码**里找实现与调用点（不看文档声称），结果如下。

**核实结论先说：TS 原版 12 项全部真实现，那份清单一条不虚。** 差距真实存在，不是文档吹的。

| # | 声称的能力 | TS 原版（`F:\MiniCode-main`） | Python 移植（`F:\MiniCode-Python-main`） | 我们 |
|---|---|---|---|---|
| ① | model→tool→model 闭环 | ✅ `agent-loop.ts` | ✅ `agent_loop.py:1347` 循环 / `:1523` `_model_next` / `:1946` 并发后保序重排 | ✅ 一致 |
| ② | ≤3 并发只读 sub-agent，可 wait/close | ✅ `agents/manager.ts:25` `maxConcurrent` / `:39` 超限抛错 / `:75` `wait()` / `:108` `close()`；`tools/sub-agents.ts:22` 文案逐字 "At most 3 … cannot edit code" | ⚠️ 只有「只读」半个（`tools/task.py:137` 按 `allowed_tools` 结构裁剪 + `permissions.py:436` 拒绝写）；**同步阻塞无句柄** → 无 wait/close、无并发；且默认 `agent_type="general"`（`:81`）拿全量工具可改文件 | ✅ **已补（M9-7）**：句柄式 5 工具族（`spawn_agent`/`list_agents`/`wait_agent`/`close_agent` + 兼容入口 `subagent`）；`MAX_SUB_AGENTS=3` 满了直接抛不排队、`wait` 超时只回报不关闭、`close` = abort + **等它真停**、回合边界结算（一个 `finally` 覆盖 6 个返回点 + `KeyboardInterrupt`）。真跑：并发 17.4s vs 串行 29.2s |
| ③ | 内存 Todo + `update_plan` + `/plan` | ✅ `cli-commands.ts:32` `/plan` + `tools/plan.ts`；`plan/context.ts:26` 明写 in-memory、`/new` `/resume` `/fork` 清空 | ⚠️ 只有 `todo_write`（`tools/todo_write.py:7` 模块级全局 list）；**`update_plan` 全仓 0 命中、`/plan` 不在命令表**；`:32` `_tasks.clear()` 排在 `:34` 遍历前 → 更新分支不可达 | ✅ **更强**：`state.plan` 随检查点落盘、`--plan` 可查（无需 key） |
| ④ | 进程内 Goal（跨回合推进 + 暂停/恢复/完成检查） | ✅ `cli-commands.ts:25-30` 全套 `/goal` `/goal status` `/goal pause [reason]` `/goal resume` `/goal clear`；`goal/context.ts`、`tools/goal.ts`、`index.ts:317` `goal.manager.dispose()` | ❌ 无（`pause/resume/goal_manager/active_goal` 全 0 命中） | ✅ **已补（M9-6）**：`/goal` 全套 + 目标随检查点落盘 + REPL 自动续跑。**判分权在人手里**：模型只有 `declare_goal_done`（刻意不叫 `complete_goal`）—— 它**声明**，运行时随即跑人给的 `--check` 命令、**退出码说了算**；判定三态（`passed`/`failed`/`invalid`） |
| ⑤ | 进程内 Loop（固定间隔重复提示词，与 Goal 互斥） | ✅ `cli-commands.ts:22-24` `/loop [Nm\|Nh] <prompt>`（默认 10m、最小 1m）+ `/loop stop`；`loop/scheduler.ts:74` 有"已有 Loop 先 stop"的互斥校验 | ❌ 无 | ❌ **真不做**：它与 Goal **抢同一个回合执行器**，TS 为此在 `runtime/session-runtime.ts` 写了**五处 throw + 一个 busy 谓词** —— **定时器人人写得出来，贵的是那一整套互斥不变式** |
| ⑥ | 全屏 TUI（历史/滚动/slash 菜单/审批） | ✅ `tty-app.ts`（Ink/React） | ✅ 自研 ANSI，无第三方 TUI 库；`main.py:639 run_tty_app` | ❌ **真不做**（成本是**第二个前端自带一整套状态机**：TS `src/tui/` 只有 8 个 .ts，而该移植版长成 **19 个 .py / 4,941 行**；Windows 还要 `ctypes` 开 `ENABLE_VIRTUAL_TERMINAL_PROCESSING` + `msvcrt` 逐键读）。行式 REPL 已覆盖**能力**面（流式渲染/工具卡片/状态位）；`readline` 历史**已放行、尚未实现** |
| ⑦ | 按项目持久化 + 恢复/重命名/分叉/压缩 | ✅ `cli-commands.ts:77/82/87/92/147` `/resume` `/rename` `/new` `/fork` `/compact`；`index.ts:11,53-137` `forkSession`；`session.ts:22` `rename` 事件 | ⚠️ resume 有；隔离是**读时过滤** `meta.workspace == workspace`（扁平 `~/.mini-code/sessions/<uuid>.json`，非 hash 路径）；**rename/fork 无** | ✅ **已补（M9-3）**：resume 本就是 **step 级**检查点，`--fork --step K` 因此能**回到任意一步**（TS 的 fork 是**会话级**的）；`--rename` + `--sessions`。**是对话分叉不是工作区分叉**（无工作区快照） |
| ⑧ | provider usage 优先 + tail estimate + 自动压缩 + 折叠 + 裁剪 | ✅ 5/5。`utils/token-estimator.ts:6` `provider_usage_plus_estimate` / `:130` `tailMessages = messages.slice(i+1)`；`compact/` 下 auto-compact / context-collapse / microcompact / snipCompact 全在 | ⚠️ **只有 auto-compact 真接线**。`token_count_with_estimation()`（`context_manager.py:245`）无运行路径调用方，且自认"退化为 estimate_only"；tail estimate 无；collapse 无对应模块；snip 实际是整条丢弃 | ✅ **更强**：usage-first + 尾部估算 + snip + LLM 摘要**全部真接线**。**context collapse 真不做** —— 同类机制实测（P7-d）单次省 5,128 token ↔ 多付 **45,304 miss token（8.8 倍）**，代价形状是后缀失效，与我们**唯一的缓存亮点**直接冲突 |
| ⑨ | 内置工具含 Web fetch/search | ✅ `tools/web-fetch.ts` / `web-search.ts` / `ask-user.ts` | ✅ 真网络 IO（非桩）：`web_fetch.py:53` 真 `urllib` + SSRF 拦截；`web_search.py:26` 真打 DuckDuckGo | ✅ **已补（M9-2）**：`agent/tools/web.py` 的 `web_fetch` / `web_search` + SSRF 拦截（**重写，不照抄移植版**——它的 `_is_safe_url` 有四个真漏洞，见下） |
| ⑩ | SKILL.md + MCP stdio 或远程 HTTP，tools/resources/prompts | ✅ `skills.ts` + `tools/load-skill.ts` + `/skills`；`mcp.ts:62` `'streamable-http'` → **远程 HTTP 有** | ⚠️ skills 真实现；MCP **tools/resources/prompts 三项都有**（`mcp.py:531/540/553`），但**传输只有 stdio**（http/sse 零命中） | ✅ **已补（M9-4）**：`Transport` 抽象 + **stdio 与 Streamable HTTP 两种传输**，协议层与传输分离；tools/resources/prompts **三个能力面**（后两个**声明了能力且列表非空**才注册）。真跑接上 DeepWiki 的公开远程端点 |
| ⑪ | 改文件前 review diff + 路径/命令权限 | ✅ `file-review.ts` `applyReviewedFileChange`，被 `tools/edit-file.ts:48` 与 `write-file.ts:28` 调用；`permissions.ts:427` `ensureEdit(targetPath, diffPreview)` | ✅ 同上真接线（`file_review.py:44-46` 先 `ensure_edit` 再 `write_text`，另加 rewind 快照） | ✅ **已补（M9-1）**：`Tool.preview()` 在写盘**之前**产 diff 交权限确认；给人 4K / 给模型 500K 两处上限分开；`--review-edits` 打开交互确认 |
| ⑫ | 超大结果落盘 + 短预览 + 路径 | ✅ `utils/tool-result-storage.ts:11` `DEFAULT_MAX_RESULT_SIZE_CHARS = 50_000` / `:12` `MAX_TOOL_RESULTS_PER_BATCH_CHARS = 200_000` + `<persisted-output>` 标签 | ⚠️ 真落盘（`context_compactor.py:162` `PERSIST_THRESHOLD = 4000`）但**只在 usage≥0.85 时跑**、预览 `Path` 只有 basename、**无批次预算**（`budget_per_message` 属性不存在却被三处赋值；`hasattr(x,"flush")` 永不成立） | ✅ 单条阈值 + **批次预算** + 预览带完整路径（本就照 §4 吸收的） |

### 三档结论

- **TS → 我们（截至 M9-7，2026-09-11）**：**12 项里 9 项完整落地**（①②③④⑦⑨⑩⑪⑫）。**剩下三项都不是"没做完"，是"真不做"且各有留痕的理由**：⑤ Loop（与 Goal 抢同一个回合执行器，贵的是那套互斥不变式）、⑥ 全屏 TUI（成本是第二套状态机）、⑧ 只差 context collapse 一块（同类机制实测单次省 5,128 token ↔ 多付 45,304 miss token，与我们的缓存亮点直接冲突）。**唯一新立的未开工条目是 M9-8 工作区 rewind 快照**，它不属于这 12 项。
- **Python 移植 → 我们**：③⑧⑫ **我们更强**（那边是死代码 / 半接线 / 只在高压才跑，我们是真接线）；①⑥ 它强（⑥ 是我们明确不做的）。**⑨⑪ 已补齐** —— 而且⑨ 这一项我们是**照它的意图重写、不照抄**：它的 `_is_safe_url` 有四个真漏洞（见下面「移植版的四个 SSRF 漏洞」）。② 在它那里是**同步阻塞、无句柄**，M9-7 之后我们这一项是**超过**它的。

### 移植版的四个 SSRF 漏洞（我们重写而不是照抄的理由）

`F:\MiniCode-Python-main` 的 `minicode/tools/web_fetch.py` 有 SSRF 拦截，**意图是对的、实现是漏的**。逐行核实（2026-09-11，行号即该文件）：

| # | 漏洞 | 代码位置与为什么它是真漏洞 |
|---|---|---|
| 1 | **完全不解析域名**，判据只有 `hostname.startswith(...)` | `:16-24`。域名解析成什么没查。**实测**：一个域名字面量里毫无内网字样、却解析到 `127.0.0.1`（`localtest.me` 就是这种），前缀表一个字都拦不住 |
| 2 | **前缀表本身不全**：`["localhost","127.","10.","192.168.","172.16.","0.0.0.0","::1","fe80:"]` | `:22`。`172.16.` 漏掉同段另外 15 个 /16；没有 `169.254.169.254`（云元数据端点，这类攻击最常见的具体目标）；没有 `100.64/10`（CGNAT，`is_private` 判 False） |
| 3 | **重定向只数跳数、不校验目标**，而注释把跳数上限写成了"防止 SSRF" | `:8` `MAX_REDIRECTS = 5  # 限制重定向次数防止 SSRF`、`:62-70` 的 handler 只做 `redirect_count += 1`。**跳数上限对 SSRF 一点用没有：一次跳转就够** —— 首跳是个货真价实的公网地址，`https://某站/redirect?to=http://169.254.169.254/` 会被自动跟随，判据全在首跳上通过 |
| 4 | **没有十进制 / 八进制 / 十六进制 IP 写法的处理** | 同 1。`2130706433` / `0x7f000001` / `017700000001` / `127.1` 都是 `127.0.0.1` 的合法写法。本机 Windows 的 `getaddrinfo` 恰好拒掉它们 —— 但那是**操作系统的行为**，Linux 上会正常解析。**安全判据不能建在「目标平台恰好也拒绝」上面** |

（公平起见也说清它**做对**的两处：`:35` 的协议白名单 `http://` / `https://` 挡住了 `file://` 与 `gopher://`；`_is_safe_url` 里的 `try/except` 兜底返回 `False`，方向上是 fail-closed 的。）

我们的做法是照它的**意图**（"别让模型打到内网"）重写：统一走「先解析、再判结果 IP」，解析失败 fail-closed；每一跳重定向重查；`::ffff:` / CGNAT / NAT64 / 6to4 的内嵌地址拆开判。判据表与变异体在 `tests/test_tools.py` 与 `m9verify/mutate_m9_2.py`。
- **Python 移植 ≠ TS 原版**：移植版把 12 项做成了「一部分真、一部分半、一部分无」，并额外引入了自己的问题——**上下文管理三层并行实现只有一层生效**（`ContextManager` 估算层 / `ContextCompactor` / `ContextCybernetics` PID 闭环；`agent_loop.py:1311`、`:1331` 两段 `elif` 因对象恒同时创建而永不命中），`task_tracker.py` / `task_graph.py` 的落盘函数**无任何调用方**。这一族与我们自己的「静默丢失」缺陷类同源。

> **⚠️ 两次同型错误的记录（核实方法本身的教训）**
> 本次核实中我两次把"没搜到"当成了"不存在"，两次都错，且都是**同一个原因：搜索范围比结论范围窄**。
> 1. 第一次：据 `ts-src/`（`F:\MiniCode-Python-main` 下那份**残缺副本**，命令表止于 `/cmd`）判定 TS 无 Goal/Loop/sub-agent 并发。**副本残缺 ≠ 原版没有** —— 真 TS 仓库里三者都在。
> 2. 第二次：搜 ⑫ 与 tail estimate 时只查了 `src/*.ts` 与 `src/compact/*.ts`，**漏了 `src/utils/`** —— 而这两个东西恰恰就住在 `utils/tool-result-storage.ts` 与 `utils/token-estimator.ts`。
>
> 教训与项目里那两条 M7 bug 一致：**"我没找到"和"它不存在"是两句不同的话**，前者需要报出**搜索范围**才能成立。本表每条都标了实现文件与行号，就是为了让"范围"可被复查。

**用户决定（2026-09-11）：往 TS 原版靠。** 因此 §8 里若干 ❌ 项进入重评 —— 其中 `Goal / Loop 持久化`（:103）与 `上下文折叠投影`（:101）两条原判"TUI 产品特性，非核心技术"，现在需要按"是否值得为了对齐原版而做"重新权衡，而不是按"是否只是产品装饰"。

#### ⑪ 的补注：TS 的 edit 是**默认要批准**的（2026-09-11 实施时核实）

上表 ⑪ 行只写了「TS 有 `ensureEdit(targetPath, diffPreview)`」。动手实现时把 `permissions.ts:427-470` 读完，
发现两处上表没写、但对「怎么靠」有决定性影响的事实：

- **它的 edit 默认需要批准**：`ensureEdit` 先查 `sessionDeniedEdits` / `allowedEditPatterns` 等记忆，
  **没命中就问**；只有显式 allow 过的路径才直接返回。
- **无交互时它直接抛错**：`if (!this.prompt) throw new Error("Edit requires approval: … Start minicode in TTY mode to review it.")`
  —— 也就是说 TS 是**靠 TTY 模式兜住**「默认 ask」的。

**我们照搬不了这个默认值**：`app/cli.py` 没有确认回调，`ask` 在无回调时按安全默认落成拒绝，
于是「edit 默认 ask」= 每一次改动都失败、整条 CLI 不可用。所以 M9-1 落成
**`--review-edits` 开关**（装上确认回调 + 把 edit/write 抬成 ask），默认值留在 ALLOW。
差别如实记在 `TASKS.md` M9-1 与 README 的能力表里，不写成「已对齐」。

同时确认了确认菜单的出处：TS 的 `requestApproval` 选项表就是六级粒度
（apply once / allow this file in this turn / allow all edits in this turn / always allow this file /
reject once / reject and send guidance），我们的编号菜单 (1-6) 与它对齐 —— 少一个
`deny_with_feedback`（把拒绝理由回喂模型），我们的做法是拒绝后由 loop 统一回喂「权限拒绝」。

