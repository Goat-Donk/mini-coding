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
