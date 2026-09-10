# Claude Code 源码架构笔记（主参考）

> 调研对象：[pengchengneo/Claude-Code](https://github.com/pengchengneo/Claude-Code)（Claude Code 完整 TypeScript 源码还原，仅供研究）
> 调研日期：2026-09-10（WebFetch 源码结构 + 用户飞书文档 "CC" 分析全文）
> 来源标注：〔repo〕= 源码结构调研；〔feishu〕= 用户飞书文档《CC》要点

## 0. 一句话

TS/Bun、约 2000 文件的完整 Claude Code 还原；我们要做的，是把它的**核心架构用 Python 从零移植**成小型 coding agent（约 3000 行）——移植架构不是移植语言。

## 1. 整体结构〔repo〕

```
src/
├── tools/       53 个工具（Bash / FileEdit / Agent / MCP...）
├── commands/    87 个斜杠命令
├── services/    API / MCP / analytics
├── components/  148 个终端 UI 组件（React + Ink）
├── hooks/       87 个自定义 hooks
├── assistant/   KAIROS 持久化助手模式（跨会话状态落盘）
├── coordinator/ 多代理协调器
├── bridge/      远程控制（claude.ai/手机 控制本地 CLI）
├── proactive/   主动模式；  voice/ 语音；  vim/ Vim 模式
docs/            7 篇隐藏功能分析文档
shims/           原生模块兼容层
```

**我们不做**：commands 市场、语音/Vim/bridge/proactive/UI 组件层——这些是"大规模"部分。

## 2. 核心引擎：query.ts（查询循环）〔feishu〕

```
async function* query(messages, systemPrompt, context, canUseTool, toolUseContext):
  1. fullSystemPrompt = formatSystemPromptWithContext(systemPrompt, context)   # 拼装上下文
  2. result = await queryWithBinaryFeedback(...)                              # 调模型
  3. toolUseMessages = assistantMessage.content.filter(m => m.type === 'tool_use')
  4. if all read-only: runToolsConcurrently      # ★ 只读工具并发
     else: runToolsSerially                      # 写工具串行
  5. yield* await query()                        # 递归继续
```

- **只读工具并发、写工具串行** —— 本项目 `agent/loop.py` 直接照搬此语义。
- 有 `canUseTool`（权限回调）——工具执行前过权限。

## 3. 工具系统〔feishu〕〔repo〕

统一接口（本项目用 pydantic 对齐 zod）：

```ts
interface Tool {
  name: string;
  description: string;
  inputSchema: z.ZodType;             // 输入 schema
  execute(params): Promise<ToolResult>; // 执行
  // 渲染相关：userFacingName / renderToolUseMessage / renderToolResultMessage / renderToolUseRejectedMessage
  needsPermissions(input): boolean;   // 工具自声明是否需要权限（不依赖外层规则）
}
```

- **`needsPermissions` 由工具自己声明** —— CC 注释："因为 cc 的工具都是自己实现的，他可以自己保证"；MCP 工具未来可能不同。
- 强大的工具是 CC 效果好的核心资产（尤其 bash tool 能调用 shell 全部命令）。

## 4. 权限与安全〔feishu〕

`permission.ts` 要点：
- `dangerouslySkipPermissions`（跳过权限模式）→ 本项目映射为 permissions 配置的 `allow_all`。
- `context.abortController.signal.aborted` → 中断检查。
- 工具执行前 `hasPermissionsToUseTool` 校验；用户确认机制；**最小权限原则**。

## 5. 上下文管理〔feishu〕

- **按需加载**：不一次性读全库，根据查询智能加载相关文件。
- **结果截断 + 明确提示**：
  - GlobTool：`{files, truncated, numFiles, durationMs}`，limit 100。
  - lsTool：`MAX_LINES=4, MAX_FILES=1000`，超限输出 `TRUNCATED_MESSAGE`（明确告诉模型"还有更多，用 LS/Bash 继续探索嵌套目录"）。
- **LRU 缓存**：文件编码检测 / 行尾类型检测 各一个 LRUCache（ttl 5min, max 1000）——避免重复 IO 探测。
- **拼装上下文**：`getContext()` → `{directoryStructure, gitStatus, codeStyle, ...}`，进 system prompt。
- 大规模压缩机制：CONTEXT_COLLAPSE / REACTIVE_COMPACT / CACHED_MICROCOMPACT / HISTORY_SNIP / TOKEN_BUDGET。

## 6. 记忆：Dream consolidation〔repo〕

- 距上次整合 >24h 且新增会话 ≥5 时，后台子代理做四阶段整合：**Orient → Gather → Consolidate → Prune**。
- `.consolidate-lock` + PID 活性检查防并发。
- 其他记忆：团队记忆同步（TEAMMEM）、自动提取记忆（EXTRACT_MEMORIES）、会话记忆。
- 我们只做简化版：任务结束提取 → 去重 → 写 repo 记忆文件（M4）。

## 7. 多代理：coordinator 模式〔repo〕

- 主 Claude 变"纯指挥官"：只剩 3 个工具 **Agent（委派）/ SendMessage（通信）/ Shutdown（停止）**。
- 工人（workers）跑在**独立子进程**，带完整工具集。
- 任务列表用文件共享：`~/.claude/tasks/`。
- 系统提示里禁止"甩锅式委派"（不能把说不清楚的需求丢给 worker）。
- 我们只做"研究子代理"：主循环调子循环（只读工具、独立上下文、返回结论）。

## 8. SubAgent 的价值（上下文经济学）〔feishu〕

> 一个复杂任务需要 X token 输入上下文，过程累积 Y token，产出 Z token 答案。
> 跑 N 个这样的任务，主窗口会累积 (X+Y+Z)*N；SubAgent 把 (X+Y)*N 外包，主窗口只收 Z。

**这是本项目 research 子代理的理论依据**：主上下文保持干净，子代理只回结论。

## 9. Skills〔feishu〕

- Skills = 领域知识胶囊 + 脚本（SKILL.md + 文档 + 模板 + 脚本），**渐进式披露**（只按需加载技能内容）。
- vs SubAgent：SubAgent 适合复杂多步多角色（独立上下文互不干扰）；Skill 适合单一明确可复用小任务。
- 结论："Agent 不再负责实现逻辑，只做调度。"
- ~~本项目不做 Skills 系统~~ → **2026-09-11 改**：做了**渐进披露这一层**（`agent/skills.py`：
  只把 name+简介进 system prompt，正文由 `load_skill` 按需取）。**没做**的是安装 / 市场 /
  权限元数据这些外围。原决定反转的理由见 `minicode-notes.md` §8。

## 10. Hooks〔feishu〕★ 我们直接采用

钩子是确定性的 **"必须做"** 规则（CLAUDE.md 是"应该做"建议），两种：

1. **Block-at-Submit 钩子（阻断型，主策略）**：
   - 示例：`PreToolUse` 钩子包裹任何 `Bash(git commit)`；检查 `/tmp/agent-pre-commit-pass` 文件（只有全部测试通过才创建）；不存在 → 阻断 commit → 迫使 Claude 进入"测试并修复"循环。
2. **Hint 钩子（提示型，非阻塞）**：Agent 做次优操作时"即发即忘"式反馈。

本项目 `agent/hooks.py`：PreToolUse / PostToolUse 两个事件；**内置 block-at-submit 示例**（git commit 前检查测试通过标记文件）。

## 11. 其他要点〔feishu〕

- **CLAUDE.md 哲学**：先设限制不写指南；少而精（只记 30% 工程师会用的工具/API）；**不"只说禁止"**——要提供可行替代（否则 Agent 左右脑互博卡住）；不堆 @引用（整份文件会每次塞进上下文）；"解释不清你的工具 = 还没准备好写进 CLAUDE.md"。
- **plan.md 哲学**：Plan 要"少而精"，只是执行索引（步骤/输入输出/决策点/工具时机）；细节放 Tool 说明；过于详细反而让 LLM 困惑。
- **上下文坍缩（Context Collapse）**：上下文过长时 Agent 过度压缩信息、偷懒、提前结束。对策：严格约束 + 分批处理。
- **单一权威来源**：每个内容只存在于一个位置，其他处用 @references 引用，不复制。
- 检查点/恢复：`/resume`（CLI）、rewind；JSONL 轨迹。
- MCP：工具接入标准；MCP_SKILLS 技能系统；MCP registry。

## 12. 对本项目的关键映射

| CC 机制 | 本项目实现 |
|---|---|
| query.ts 循环 + 只读并发 | agent/loop.py |
| Tool 接口（zod inputSchema） | agent/tools/base.py（pydantic schema 自动生成） |
| needsPermissions 自声明 | Tool.needs_permission() |
| 截断 + TRUNCATED_MESSAGE | files.py 的 glob/grep/read 截断提示 |
| 拼装上下文 getContext() | agent/context.py（M3，cache-aware） |
| CLAUDE.md 机制 | agent/memory.py（M4）+ 本仓库根 CLAUDE.md |
| SubAgent 只回结论 | agent/tools/subagent.py（M4） |
| block-at-submit hooks | agent/hooks.py（M2，内置 git commit 检查测试） |
| /resume + 轨迹 | agent/session.py（M3） |
| LRU 缓存 / 按需加载 | files.py 内部缓存（M1 可简化为结果截断，LRU 后置） |
