# CLAUDE.md — 项目约定与索引

> 本文件是新会话（包括 clear 对话后）的入口。**续作请按底部"续作三步"执行。**

## 项目定位

求职作品集：**CodeAgent** —— 参考 [pengchengneo/Claude-Code](https://github.com/pengchengneo/Claude-Code) 源码架构，用 Python 从零实现的小型 AI Coding Agent（8,310 行 / 25 模块 / 515 测试）。核心循环手写（不套 Agent SDK），支撑层用成熟库（openai / pydantic / streamlit / typer / pytest）。差异化：cache-aware 上下文 + 缓存省钱指标、step 级检查点恢复 + **step 级分叉/会话命名**、block-at-submit hooks、轨迹驱动评估（真实 tinydb 提交 + 隐藏测试）、记忆自进化、MCP 工具接入（**stdio + Streamable HTTP 两种传输**）、注入文本检测 + 会话污染天花板、联网工具 + SSRF 拦截、skills 渐进披露、计划清单跨回合、提问暂停/续答、改动前 diff 复核。

> 数字口径（改数字时请沿用）：**源码 = `agent/` + `app/` + `eval/` 里被 git 跟踪的 `.py` 行数**（不含 `eval/repos/` 的克隆仓，它被 gitignore）；**模块数 = 其中非空的 `.py` 文件数**（4 个空 `__init__.py` 不计）；**测试 = `tests/` 行数 / pytest 用例数**。

**已验证状态**：真实 LLM 端到端跑通（修 bug 全流程、kill+`--resume` 续跑、eval 出真实报告 50% 1/2）。README「评估」章节有真实数字与口径说明。**M6 收尾后又补齐三项此前只是单测覆盖的验证**：compact 在真实 token 压力下真实触发（当时是两级；M8 加了分级截断，现为三级 compact）、MCP 接真实第三方 server（官方 `mcp-server-time`，stdio）并确认仍走权限/hook 门禁链、Streamlit 控制台用真实 Chrome 打开并操作控件跑通 mock 任务。**M9-4 又补了一项**：MCP 接**真实远程 HTTP** server（DeepWiki 的 Streamable HTTP 端点，走公网）并端到端跑通工具调用。

**CLI 治理链路**：`app/cli.py` 两处 `QueryEngine` 都已接 `PermissionsEngine`（默认 allow、危险命令 ask→无确认交互→拒绝、路径越界 deny、**第三方/MCP 工具须在 `mcp.json` 的 `allow` 里显式授权否则 ask**）与 hooks（`default_engine()`：block-at-submit + marker 自动维护 + **注入检测**）。两个入口共用 `hooks.default_engine()`，避免接线漂移（CLI 曾整体漏接 hooks）。`tests/test_cli.py` 有 26 例锁住这个接线。
**唯一例外是 `--review-edits`（M9-1）**：它给 CLI 装上 `confirm` 回调并把 `edit`/`write` 抬成 ask，于是改动落盘前人能看到 diff。**默认关闭是刻意的** —— 我们的 CLI 本来没有确认回调，把 edit 改成默认 ask 会让每次改动都退化成拒绝、整条 CLI 不可用（那就把安全机制变成了路障；TS 原版靠 TTY 模式兜住，我们没有）。所以它是 opt-in，默认路径与 eval 一字不变。

**M7 安全机制（措辞红线，别写错）**：本项目**没有**做「防止 prompt 注入」。分三层：① `permissions.py` 执行（确定性）② `hooks.py` PreToolUse 阻断（确定性）③ `security.py` 检测（**概率性，只出告警，从不直接决定放行/拒绝**）。检测出 `high` 后收紧能力的是**后置天花板**（`_apply_taint_ceiling`，只把三类不可逆动作 网络外发/读凭据/写记忆文件 从 ALLOW 降为 ASK），位置**必须在 `_always`/`_turn` 之后、`confirm` 之前**。禁用措辞：不说「防御/防止注入」，不说「污点追踪/taint 传播」（实为**会话级粗粒度标记**），不说「纵深防御/零信任」，不说「子代理沙箱」（实为**受限只读工具集**），不给「误报率低」这类无数字形容词。局限逐条写在 README「已知未修复的绕过路径」（S1–S16，其中 S6–S10 配了探针实测），改任何一条都要同步改那张表。**bash 的凭据判据是「提到即命中」**（`CREDENTIAL_MENTION`，不锚定末尾，见 permissions.py 的注释）——别「顺手修回去」，那会让天花板重新空转。

> ✅ **真实跑分走的是项目选定通路（DeepSeek 官方）**：`https://api.deepseek.com` + `deepseek-chat`，与代码默认值、`.env.example` 一致。
> 最新一次：`python -m eval.runner --limit 2` → 完成率 50%（1/2）、82,495 token、¥0.0769、缓存命中 82%；真·冷启动曲线 step1 0% → 累计 77%。
> **M9-2 后重跑过，与旧的 62,812 / ¥0.0625 / 83% 不可直接比**：`default()` 里多了 `web_fetch`/`web_search` 两个 schema（eval 用的就是这个 registry），token +31%，**判定结论一字未变**。
> 早期曾临时借用 DashScope（阿里百炼）+ `deepseek-v4-flash` 验证端到端，**该手段已弃用**，其数字仅作历史记录留在 README 的口径说明里（命中率口径不同，不可直接比）。
> 本机 agentrouter 的 key 用不了——它只放行 Claude Code 客户端，自写程序一律 `401 unauthorized client detected`（实测 6 种认证头组合 × 2 个端点全 401）。

**M8 移植的四项机制（照设计自己重写，未复制任何代码）**：
- **提问暂停/续答**：`ask_user` 工具返回 `ToolResult(await_user=True)` —— 打断被建模成**数据标志**，不是阻塞式控制流，turn 语义由 loop 决定。所以 headless 只要**不注册**这个工具就完全不受影响（`eval/runner.py` 用 `default()`，故意不含它）。`--resume "回答"` 把人的回复作为一条 user 消息追加进会话。
- **skills 渐进披露**：system prompt 只放 name+简介，正文由 `load_skill` 按需取。**`render_skills_block` 的输出绝不能含正文** —— 那是"省 token"的全部依据。
- **分级截断**：compact 流水线的**第 0 级**（分级截断 → 确定性 snip → LLM 摘要）。**只在 utilization ≥ 0.70 时才跑，这是不变量不是优化开关**：低于阈值必须逐字节不碰，否则每个大工具结果都会破坏一次前缀缓存。破坏它是**静默的**（没有报错，只有一条走平的缓存命中曲线和更贵的账单），所以有一条「utilization < 0.70 时消息逐字节不变」的测试钉着。**实测结论对设计不利，别只讲设计**：端到端命中率没降（A/B 差 ±0.02% 以内），但单次截断要付 **8.8 倍**的 miss token（后缀失效，截得越靠前越贵，差 3.8 倍），且**免不掉第 1/2 级**。**去留已拍板（2026-09-11）：保持现状**（代价被同一步的 LLM 摘要遮蔽，不值得为微观那 8.8 倍改取向；也不选"优先截最靠后的"——那会先丢最老的上下文）。数据在 `TASKS.md` P7-d。
- **计划清单**：`update_plan` 写 `state.plan`（随检查点走）。**每次传完整清单**，状态只有一个写入者。**不要每轮把 plan 注入 messages** —— 计划一变就改一段消息、那段之后的缓存全失效；可见性靠工具结果本身，只在 `--resume` 时补投一次。
- 这四项的变异测试结果（逐条打断机制、确认对应测试变红）：见 `TASKS.md`。

**已做 M9-3 会话分叉 + 命名（`--fork` / `--rename` / `--sessions`）**，机制是：
- **分叉挂在 step 级检查点上**（与 TS 原版的差别）：它的 `/fork` 是**会话级**的，我们可以 `--fork --step K` **回到任意一步**再开一条路 —— 检查点本来就逐步落，这能力是现成的。
- **它是对话分叉，不是工作区分叉。** 工作区文件**不会**回滚到第 K 步，分叉后的 agent 看到的是**当前**工作区 —— 我们**没有**工作区快照机制（`grep rewind|snapshot` 在 `agent/ app/` 下零命中）。CLI 分叉后把这句打印出来、`--fork` 的 help 也写明。**别把这条说漏**，否则"回到第 3 步"几乎必然被读成文件也回去了。
- fork 搬三样，都以 fork 点为界：检查点 `step-1..K`、**轨迹里 `step <= K` 的行**（整份复制会让分叉会话"继承"源会话在 K 之后才发生的 `security_finding`）、`forked_from`。**副本里唯一按新会话重写的是 `session_id`**（今天 `load_state` 会 pop 掉所以无害，但谁哪天直接读 `payload["session_id"]` 就会拿到源会话）。
- **`--sessions` 是这一项的另一半，不是附赠**：没有它，`--rename` 写的名字与 `--fork` 记的血统**没有任何消费者** —— 正是本项目记录在案的头号缺陷类。清单的键集合取「检查点目录 ∪ `data/sessions/*.jsonl`」，所以**跑到一半被 kill、还没到第一个检查点的会话也在里面**。
- **名字的两条判据合起来才成立**：写入侧 `validate_name` 拒绝「与任何已有 session_id 或会话名相同」，读取侧 `resolve_session` **先当 id、再当名字**。少了写入侧的拒绝，重名会让 `--resume --session-id <名字>` **安静地跑到另一个会话上**（带着另一个任务的上下文）；名字等于某个 id 时那个 id 就永远解析不到自己。所以不去读取侧加优先级"猜"。
- **`update_meta` 是合并式**（read → update → 原子写）不是覆盖式：`--rename` 只该改名字，覆盖会静默抹掉 `forked_from`。**`read_meta` 与 `session_name` 对坏 meta 的反应刻意不同**：前者抛（名字是真丢了，静默返回 `{}` 会让人以为"我从没起过名字"从而重起一个），后者返回 `None`（调用方只是要个显示名），`--sessions` 逐行标 `meta_error`。
- **`--rename` / `--sessions` / `--fork`（不带任务）都不构造 LLM、不需要 key**，dispatch 排在 `_build_llm` 之前；测试用"`_build_llm` 一被调用就炸"的替身钉住（靠"本机没配 key 也过了"来测会在有 `.env` 时假装通过）。**不带任务就退出**是刻意的：分叉零成本、续跑要花钱，且上面已打出可直接粘贴的续跑命令。
- CLI 侧把"取会话 id"收成 `_resolve_sid` **一处**：原先 `_print_plan` 与 `--resume` 各写一遍，加名字解析时只改一处、另一处照旧忽略就是漂移。
- 变异测试与真跑数字见 `TASKS.md` 的 M9-3 条目。

**模块 docstring 要注明机制出处**（先例 `agent/tool_result.py`、`agent/tools/plan.py`），与本项目「参考与来源」的惯例一致。

**M9 向 TS 原版对齐（进行中，排序见 `TASKS.md`）**：核实后 TS 原版 12 项核心能力全部真实现（逐条对照与 file:line 证据在 `docs/reference/minicode-notes.md` §10），本项目按「先小后大」分四批靠。**已做 M9-1 改动前 diff 复核**，机制是：
- `Tool.preview(arguments, ctx) -> str | None`（`agent/tools/base.py`）声明**将要做什么**，默认 None；目前只有 `write`/`edit` 实现。
- **它必须是纯函数**（绝不写盘），且**只能有一份**匹配语义：`EditTool._plan` 同时供 `preview` 与 `execute` 用 —— 各判一遍就会出现「预览说能改、执行说匹配不唯一」，而那时人已经照着预览点过允许了（**两个真相源**）。
- `_gate_and_run` 里 `details = self._preview(...)` **必须在 `permissions.check()` 之前**取。位置就是这一项的全部意义：人看到 diff 时磁盘上还是旧内容。**挪到 check() 之后不会报任何错**，所以有测试专门钉住确认回调被调用那一刻文件仍是原文。
- 预览是**信息**不是判定，所以它 fail-open（抛异常就记 `preview_failed` 事件并退回原确认框）；权限链算不出来必须 fail-closed。**没有权限引擎时根本不计算预览**（headless 没有确认交互，算了是白花 —— edit 的预览要把整个文件读进来做 diff）。
- 给人看的 diff 上限 `DIFF_PREVIEW_CHARS = 4_000`，给模型的那份仍走 `MAX_CHARS`。**两处共用 `_unified_diff`，所以那个 `limit=` 实参要留神**：漏传就会把模型的 diff 从 500K 悄悄砍成 4K，且没有任何测试会自然变红。
- 变异测试（12 个变异体全部被抓住）：`m9verify/mutate_m9_1.py`。
- **真实 LLM 跑过两个方向**（DeepSeek 官方通路，轨迹留在 `m9verify/review/`）：答「允许一次」→ 文件按 diff 改对（4 步、缓存命中 71%）；答「拒绝一次」→ **磁盘上仍是原文**，模型收到拒绝理由后如实报告"补丁已备好但被拒"（4 步、命中 74%）。

**已做 M9-2 联网工具（`web_fetch` / `web_search`）+ SSRF 拦截**，机制是：
- **判据支点是「先解析域名、再判结果 IP」**，判 `hostname` 字面量是漏的（本机实测 `localtest.me` → `127.0.0.1`）；解析失败 **fail-closed**（拒绝）。
- **`_is_internal` 有三处非显然判定，都是实测出来的**（判据表在 `tests/test_tools.py::test_is_internal_table`）：`::ffff:100.64.0.1` 只有拆开 `ipv4_mapped` 才拦得住，但 `::ffff:8.8.8.8` 必须放行；CGNAT `100.64/10` 的 `is_private`/`is_reserved` 都是 False；NAT64/6to4 **整段**被标 reserved/private，直接判会让纯 IPv6+DNS64 网络上工具完全不可用且报**错误**诊断，所以要拆出内嵌 IPv4 判（位偏移取错=开洞）。
- **拦截位置与拦截本身一样重要**：判据在**发请求之前**，且**每一跳重定向都重查**（只数跳数不校验目标等于不设防，一次跳转就够）。真实攻击形态验过：`httpbin.org/redirect-to?url=http://169.254.169.254/...` 在跳转处被拦。
- **接线（这一项的另一半）**：`permissions._irreversible_kind` 的 web 分支必须写在**取 `raw` 之前** —— 它只取 `command`/`path`/`pattern`，web 参数是 `url`/`query`，写在后面就 `if not raw: return None` 永远不生效（**那正是 M9-2 之前的原状**：天花板三类动作里「网络外发」打的是不存在的动作）。有专门变异体钉位置。
- **内容编码**：真跑挖出的真 bug —— `python.org` 在**没被请求压缩**的情况下回 `Content-Encoding: gzip`，`Content-Type` 却是 `text/html`，于是 2MB 压缩字节被当正文喂给模型（**乱码静默到达消费者**）。修复：请求 `Accept-Encoding: identity`（减少该情况）+ `_decompress()` 真解压 + **解压后同样封顶 `MAX_FETCH_BYTES`**（原上限只管读进来的字节，管不到解压之后 —— 压缩炸弹能解出几 GB）+ **认不出的编码如实报错，不退回原文**。
- 变异测试 **31/31** 被抓住（`m9verify/mutate_m9_2.py`）：判据漏格 / 拦截位置 / 接线三类各覆盖。
- **如实标注**：① 默认搜索后端 `ddg`（**对齐 TS 原版**的刻意选择，非因为好用）在本机**连不上**（直连超时 8.2s、代理 SSL EOF 7.6s），其解析正则**未经真实响应校准** —— 代码注释、`TASKS.md` 都写明；真跑验证走 `CODEAGENT_SEARCH_BACKEND=bing`（照 98KB 真实响应写的）。② **只读并发批内，外发与产生污染的读是并发执行的**，该次外发按**批前**污染级别判定 —— 物理顺序，不是漏洞，但看起来像洞，已写进 `TASKS.md` 与 `docs/TECH_SPEC.md`。

**已做 M9-4 MCP 传输抽象 + HTTP + resources/prompts**，机制是：
- **`Transport`（怎么送）与 `MCPClient`（说什么）分开**：加 HTTP 传输时协议层**一行未改**。这不是洁癖，是**同一个错误不想修两遍** —— 超时整形、id 关联、错误包装、会话重建这四件事在两种传输上必须完全一致，写两遍必然漂移，而**只测单一传输的套件看不见分歧**。有一条测试专门跑同一条操作序列走两种传输、逐项比对结果**和报错文案**。
- **`timeout` 只存传输层一份**（`MCPClient.timeout` 是转发属性）。两处各存一份迟早对不上（一个 20s 一个 30s，表现成"有时报超时有时不报"）。
- **`MCPTimeout` 单独一个异常类型**：传输层只知道"没人来"，知道"在等哪个方法"的只有协议层 —— 合成一句放在 `_request` 里，两种传输的诊断才一致（否则 stdio 报得出方法、HTTP 报不出）。同理 HTTP 超时要带上端点地址（stdio 给的是 server stderr 末尾）。
- **`_raise_for_http_error` 的判断顺序是有讲究的**：`404 + 带 session id` 必须排在"正文里有 JSON-RPC error"**之前**。真实 server 回 404 时常常正文里也放一条 error，反过来的话"会话过期"被报成一次普通调用失败 —— 文案看着完全合理，但自愈那条路（重新 initialize + 重试）**永远走不到**，一次 server 重启就废掉整个任务。假 server 特意用这种正文钉顺序。
- **重定向不跟随**（`_NoRedirect`）：`urllib` 默认把 302 上的 POST **改写成 GET**，一次 `tools/call` 变成一次静默的读请求。宁可报一句能照着改的配置错误。
- **HTTP 明确未做**（如实标注）：独立 GET SSE 流（server 主动发起请求那条长连接）与 `Last-Event-ID` 断点续传。tools/resources/prompts 三件事都走 POST 请求-响应，用不到。`GET` 假 server 一律 405，钉住客户端不会偷偷去开它。
- **resources / prompts 两个工具面**：`read_resource` / `get_prompt`（都是只读 + 外部）。**两条都满足才注册**：server 声明了该能力**且列表非空** —— 只判能力的话，一个声明了 `resources` 却一处资源都没有的 server 会拿到一个**永远调不通**的工具。**真跑对上了**：DeepWiki 声明了两种能力，两个列表都是空的（原文 `{"resources": []}`），于是正确地没注册。
- **描述里必须列出可用 uri / 提示词名与参数名**：不列的话模型不知道有什么可读，只能瞎猜一个试试 —— 这正是 M8 `update_plan` 那个 elicitation gap 的形状。描述恒在上下文里，列出来是**零额外往返**。封顶 `MAX_LISTED_ITEMS = 20` 并如实说"另有 N 处未列出"。
- **`command` 与 `url` 二选一只在 `build_transport` 一处判断**（两处各判一遍 = 漂移，最后表现成"配了 url 却被当成 stdio 去起子进程"）。
- **`--mcp` 的相对路径按工作区解析，不按进程 CWD**（真实跑挖出来的：help 的例子 `.codeagent/mcp.json` 是 CWD 相对的，设了 `WORKSPACE_ROOT` 从别处跑就报"配置不存在"）。
- 变异测试 **41/41** 被抓住（`m9verify/mutate_m9_4.py`）。**一处已验证的等价变异体**（已从列表删掉，理由写在脚本头部）：`_parse_sse` 里 `if line.startswith(":")` 那条注释行判断**算术上不可观测** —— 以 `:` 开头的行 `partition(":")` 出来的 field 恒为空串，永远不等于 `"data"`。
- **真实远程 HTTP 端到端跑过**（DeepWiki `https://mcp.deepwiki.com/mcp`，真网络真 server）：握手拿到 `protocolVersion 2025-06-18` / `serverInfo DeepWiki 2.14.3`；DeepSeek 驱动实际调用 `read_wiki_structure(repoName=pallets/flask)` 成功（1 步、2425ms、缓存命中 45% → 终局 86%）。**该 server 不回 `Mcp-Session-Id`（无状态模式），我们的客户端照常工作**。

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
| 工具 | agent/tools/ | base(基类+schema+`preview` 预览钩子) / bash / files(read·write·edit·glob·grep) / web(fetch·search + SSRF) / subagent / ask / plan / skills |
| 状态 | agent/state.py | 消息构造（OpenAI 格式）+ AgentState（含 `plan`） |
| 上下文 | agent/context.py | provider-usage-first 记账 + cache-aware 布局 + 分级截断/snip/LLM compact（M3·M8） |
| 工具结果 | agent/tool_result.py | 超大工具结果落盘 + 预览替换 + 批预算（M3） |
| 权限 | agent/permissions.py | once/turn/always 决策粒度 + 黑名单 + 沙箱 + 确认文案带 `details`（M2·M9） |
| 钩子 | agent/hooks.py | Pre/PostToolUse + block-at-submit（marker 由测试成功自动写）（M2） |
| 会话 | agent/session.py | JSONL 轨迹 + 检查点 + resume + **step 级分叉 / 会话命名 / 清单**（M3·M9-3） |
| 记忆 | agent/memory.py | 分层指令文件(@include+去重+预算) + 提取 + 简化 consolidation（M4） |
| 技能 | agent/skills.py | SKILL.md 渐进披露：只把 name+简介进 system prompt，正文由 load_skill 按需取（M8） |
| 安全 | agent/security.py | 注入文本检测（**概率性，只出告警**）+ 会话级污染标记 + 来源框架（M7） |
| MCP | agent/mcp.py | 手写 MCP 客户端：`Transport` 抽象 + stdio/Streamable HTTP 两种实现，协议层与传输分离；tools/resources/prompts 三个能力面（第三方工具照样过权限/hooks）（M6-3 / M9-4） |
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
python -m app.cli --sessions                 # 列出会话：名字/步数/分叉来源（无 key）
python -m app.cli --rename "基线方案"          # 给最近会话起名（只动元数据，无 key）
python -m app.cli --fork --step 3 "换个思路"   # 从第 3 步分叉出新会话并续跑（对话分叉，见下）
python -m app.cli --resume --session-id "基线方案" "接着改"   # 会话 id **或名字**都能用来指会话
python -m app.cli "任务" --review-edits       # 改动前人工确认：edit/write 先显示 diff（M9-1）
CODEAGENT_SEARCH_BACKEND=bing python -m app.cli "查 X 并写进文件"   # 联网任务（默认后端 ddg 本机连不上，见下）
python -m app.cli --mcp .codeagent/mcp.json "任务"   # 加载 MCP server（见 mcp.example.json）
python -m eval.golden_tasks --clone --limit 10       # 拉 tinydb 并列出真实 fix 提交
python -m eval.runner --limit 3              # 跑黄金任务出回归报告（--mock 无 key 冒烟）
streamlit run app/ui_streamlit.py            # 控制台（指标 + 权限按钮 + 检查点回放）
```

> ⚠️ **别再给 pytest 传第二个 `-q`**：`pyproject.toml` 的 `addopts` 里已经有一份，命令行再传一份就是 **`-qq`**，而 pytest 在这个详细度下**根本不打印最后那行 `NNN passed in …`** —— 进度点照打、退出码照对，只有那行给人看的结论没了。要看汇总：不传 `-q`，或写 `-o addopts="" -q`。
> 2026-09-11 在这上面栽过：一份全量日志以 `[100%]` 结尾，我把它读成「汇总行被 streamlit 接管 stdout 吞了」，还照这个结论往 `tests/conftest.py` 加了个空转的 fixture（现已撤销）。**先怀疑自己的调用方式，再怀疑库。**

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
- MiniCode 笔记 → `docs/reference/minicode-notes.md`（长会话上下文治理：落盘/记账/compact/分层记忆/权限粒度；§8 是**吸收表**，写明哪些移植、哪些明确不做及理由；**§10 是 12 项核心能力对 TS 原版的逐条对照**，含 file:line 证据）
- Anthropic 模式 / MCP·A2A 调研 → 见 TECH_SPEC §0 与 reference 笔记

## 续作三步（clear 对话后从这里开始）

1. 读 `TASKS.md`，找第一个 `[ ]` 任务
2. 读 `docs/TECH_SPEC.md` 对应模块规格（函数签名/数据结构/边界/测试用例都在里面，照抄即可实现）
3. 实现 → 跑验收命令/测试 → commit → 勾选 `[x]` → `git push`
