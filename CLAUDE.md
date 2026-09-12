# CLAUDE.md — 项目约定与索引

> 本文件是新会话（包括 clear 对话后）的入口。**续作请按底部"续作三步"执行。**

## 项目定位

求职作品集：**CodeAgent** —— 参考 [pengchengneo/Claude-Code](https://github.com/pengchengneo/Claude-Code) 源码架构，用 Python 从零实现的小型 AI Coding Agent（13,636 行 / 31 模块 / 769 测试）。核心循环手写（不套 Agent SDK），支撑层用成熟库（openai / pydantic / streamlit / typer / pytest）。差异化：cache-aware 上下文 + 缓存省钱指标、step 级检查点恢复 + **step 级分叉/会话命名 + 工作区回滚快照（`--rewind` 与 `--fork` 共用同一个 K）**、**常驻交互模式（REPL，一行一个回合）**、**进程内目标 + 显式完成检查（判分权在人手里）**、**并发子代理（句柄式 spawn/wait/close + 回合边界结算）**、block-at-submit hooks、**轨迹驱动评估（真实 tinydb 提交 + 物理剥离工作区 + 有效性闸门 + 三条对照臂 + 定价快照 + 评测器分离与离线重算）**、记忆自进化、MCP 工具接入（**stdio + Streamable HTTP 两种传输**）、注入文本检测 + 会话污染天花板、联网工具 + SSRF 拦截、skills 渐进披露、计划清单跨回合、提问暂停/续答、改动前 diff 复核。

> 数字口径（改数字时请沿用）：**源码 = `agent/` + `app/` + `eval/` 下非空 `.py` 文件的全部行数**（含空行；不含 `eval/repos/` 的克隆仓，它被 gitignore）；**模块数 = 其中非空的 `.py` 文件数**（4 个空 `__init__.py` 不计）；**测试 = `tests/` 行数 / pytest 用例数**。**按文件系统数，不按 git 跟踪数** —— 新文件在提交前也该算进去（M9-8 的 `agent/workspace.py`(814) 与 `tests/test_workspace.py`(785) 目前尚未提交）。

**已验证状态**：真实 LLM 端到端跑通（修 bug 全流程、kill+`--resume` 续跑、eval 出真实报告（三臂 21 个有效任务，含一次解析器缺陷的自我更正））。README「评估」章节有真实数字与口径说明。**M6 收尾后又补齐三项此前只是单测覆盖的验证**：compact 在真实 token 压力下真实触发（当时是两级；M8 加了分级截断，现为三级 compact）、MCP 接真实第三方 server（官方 `mcp-server-time`，stdio）并确认仍走权限/hook 门禁链、Streamlit 控制台用真实 Chrome 打开并操作控件跑通 mock 任务。**M9-4 又补了一项**：MCP 接**真实远程 HTTP** server（DeepWiki 的 Streamable HTTP 端点，走公网）并端到端跑通工具调用。**M9-5 又补了三轮**：常驻 REPL 的多回合上下文接续（第二回合不重读源码就改对）、`/rename`+重进+`/fork 2` 新分支接着跑、`--review-edits` 下答「本回合允许」后下一回合同一命令**重新被问**（`m9verify/repl_a|b|c.log`）。**M9-6 又补了五条 Goal 动线**（`m9verify/goal_*.log`）：建目标→自动续跑→声明→检查真跑通过 / 一开始必然失败的检查→回喂→继续修→再声明→通过 / `pause`+`resume` / 人敲一行字 / `--resume` 一个有活跃目标的会话（检查照跑）。**M9-6 真跑没有挖出产品缺陷** —— 如实记，因为前面几轮都挖出了东西、这里没有；真跑暴露的是**我自己写的一条测试素材错误**（见下）。**M9-8 又补了六条动线**（`m9verify/drive_m9_8.py`，工作区 `m9verify/ws_m98/`）：写盘产快照 → 预览不动盘 → 真回滚 → `--fork --step K --rewind` → 磁盘实测 → `--drop-snapshots` 回收，**这一步的核心产出是"决定之前先测"**（见 M9-8 段的三条实测）。

**CLI 治理链路**：`app/cli.py` 的 `_Runtime.engine()` 是**唯一** `QueryEngine(...)` 构造点（M9-5 收的，原先两处各写一遍），单发与常驻 REPL 都从它拿引擎 —— 两处都已接 `PermissionsEngine`（默认 allow、危险命令 ask→无确认交互→拒绝、路径越界 deny、**第三方/MCP 工具须在 `mcp.json` 的 `allow` 里显式授权否则 ask**）与 hooks（`default_engine()`：block-at-submit + marker 自动维护 + **注入检测**）。入口层共用 `hooks.default_engine()`，避免接线漂移（CLI 曾整体漏接 hooks）。`tests/test_cli.py` 共 47 例（接线相关的多条在里面；数字按 `pytest --collect-only` 数）。
**唯一例外是 `--review-edits`（M9-1）**：它给 CLI 装上 `confirm` 回调并把 `edit`/`write` 抬成 ask，于是改动落盘前人能看到 diff。**默认关闭是刻意的** —— 我们的 CLI 本来没有确认回调，把 edit 改成默认 ask 会让每次改动都退化成拒绝、整条 CLI 不可用（那就把安全机制变成了路障；TS 原版靠 TTY 模式兜住，我们没有）。所以它是 opt-in，默认路径与 eval 一字不变。

**M7 安全机制（措辞红线，别写错）**：本项目**没有**做「防止 prompt 注入」。分三层：① `permissions.py` 执行（确定性）② `hooks.py` PreToolUse 阻断（确定性）③ `security.py` 检测（**概率性，只出告警，从不直接决定放行/拒绝**）。检测出 `high` 后收紧能力的是**后置天花板**（`_apply_taint_ceiling`，只把三类不可逆动作 网络外发/读凭据/写记忆文件 从 ALLOW 降为 ASK），位置**必须在 `_always`/`_turn` 之后、`confirm` 之前**。禁用措辞：不说「防御/防止注入」，不说「污点追踪/taint 传播」（实为**会话级粗粒度标记**），不说「纵深防御/零信任」，不说「子代理沙箱」（实为**受限只读工具集**），不给「误报率低」这类无数字形容词。局限逐条写在 README「已知未修复的绕过路径」（S1–S20，其中 S6–S10 配了探针实测、S17–S20 是 M9-6 目标完成检查的代价），改任何一条都要同步改那张表。**bash 的凭据判据是「提到即命中」**（`CREDENTIAL_MENTION`，不锚定末尾，见 permissions.py 的注释）——别「顺手修回去」，那会让天花板重新空转。

> ✅ **真实跑分走的是项目选定通路（DeepSeek 官方）**：`https://api.deepseek.com` + `deepseek-chat`，与代码默认值、`.env.example` 一致。
> 最新一次：`python -m eval.runner --limit 39`（三臂：`agent` / `one-step` / `single-shot`）→ 闸门后 **21 个有效任务**，完成率 **`agent` 14/21 = 66.7%** / **`one-step` 0/21 = 0%** / **`single-shot` 16/19 = 84.2%**。真·冷启动曲线 step1 0% → 累计 77%。
> ⚠️ **两条结论，来源不同，别说串**：① **多轮循环的价值有受控证据** —— `agent` vs `one-step` 是 66.7% vs 0%，这一对同工具、同提示、同流程，**只差 `max_steps` 一个数**（但 `one-step` 是 21/21 撞满一步预算、零变更，量的是"给不给得起第二次机会"，不是"循环的智能"）；② **单臂能力差距在样本内不显著** —— `agent`(14) 与 `single-shot`(16) 剥掉解析器偏差/输出合规/预算后**真正的能力差异只剩 3 个任务**，不足以支撑架构结论。
> ⚠️ **`single-shot` 的完成率有两个数**：留档 **52.4%（11/21）**，修正 **84.2%（16/19）**。**原 52.4% 系评测系统解析器缺陷导致** —— `eval/runner.py` 的 `_FENCE_RE` 开围栏只认 ``` / ```diff / ```patch，模型回复里 diff 块前面的 ```python 当不了开围栏 → **围栏配对整个错位一格** → diff 抠不出来 → 兜底把模型后面的散文整段喂给 `git apply` → `corrupt patch`。**7 个"补丁未应用"里 5 个是这一个 bug 造成的假阴性，不是模型的失败。** 本轮 `runner.py` 已冻结不改，修正走**离线重算**（`evalverify/rescore_single_shot.py`，重抠已有 `raw_output` + 重判，**零 LLM**）。**两个数字必须并列引用**，详见 README「评估」章节。
> 🚫 **绝对不要把「并集 90.5%」写成 `agent` 的成绩。** 那是 `agent ∪ single-shot`「两条通路合起来覆盖多少」，**不是任何单臂的数字**。
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
- **单独用时，它是对话分叉，不是工作区分叉。** 工作区文件**不会**回滚到第 K 步，分叉后的 agent 看到的是**当前**工作区。CLI 分叉后把这句打印出来、`--fork` 的 help 也写明。**别把这条说漏**，否则"回到第 3 步"几乎必然被读成文件也回去了。
  - **M9-8 之后这句话有了正面出口：加 `--rewind` 就让文件也回到第 K 步**（`--fork --step K --rewind`，见下）。**而且那句话本身必须跟着 `--rewind` 分叉写**：M9-8 之前它无条件说"工作区不会回滚"（那时是真的，因为没有回滚机制），现在带着 `--rewind` 时它会**变成假话** —— 而假话比不说更糟。
- fork 搬三样，都以 fork 点为界：检查点 `step-1..K`、**轨迹里 `step <= K` 的行**（整份复制会让分叉会话"继承"源会话在 K 之后才发生的 `security_finding`）、`forked_from`。**副本里唯一按新会话重写的是 `session_id`**（今天 `load_state` 会 pop 掉所以无害，但谁哪天直接读 `payload["session_id"]` 就会拿到源会话）。
- **`--sessions` 是这一项的另一半，不是附赠**：没有它，`--rename` 写的名字与 `--fork` 记的血统**没有任何消费者** —— 正是本项目记录在案的头号缺陷类。清单的键集合取「检查点目录 ∪ `data/sessions/*.jsonl`」，所以**跑到一半被 kill、还没到第一个检查点的会话也在里面**。
- **名字的两条判据合起来才成立**：写入侧 `validate_name` 拒绝「与任何已有 session_id 或会话名相同」，读取侧 `resolve_session` **先当 id、再当名字**。少了写入侧的拒绝，重名会让 `--resume --session-id <名字>` **安静地跑到另一个会话上**（带着另一个任务的上下文）；名字等于某个 id 时那个 id 就永远解析不到自己。所以不去读取侧加优先级"猜"。
- **`update_meta` 是合并式**（read → update → 原子写）不是覆盖式：`--rename` 只该改名字，覆盖会静默抹掉 `forked_from`。**`read_meta` 与 `session_name` 对坏 meta 的反应刻意不同**：前者抛（名字是真丢了，静默返回 `{}` 会让人以为"我从没起过名字"从而重起一个），后者返回 `None`（调用方只是要个显示名），`--sessions` 逐行标 `meta_error`。
- **`--rename` / `--sessions` / `--fork`（不带任务）都不构造 LLM、不需要 key**，dispatch 排在 `_build_llm` 之前；测试用"`_build_llm` 一被调用就炸"的替身钉住（靠"本机没配 key 也过了"来测会在有 `.env` 时假装通过）。**不带任务就退出**是刻意的：分叉零成本、续跑要花钱，且上面已打出可直接粘贴的续跑命令。
- CLI 侧把"取会话 id"收成 `_resolve_sid` **一处**：原先 `_print_plan` 与 `--resume` 各写一遍，加名字解析时只改一处、另一处照旧忽略就是漂移。
- 变异测试与真跑数字见 `TASKS.md` 的 M9-3 条目。

**模块 docstring 要注明机制出处**（先例 `agent/tool_result.py`、`agent/tools/plan.py`），与本项目「参考与来源」的惯例一致。

**M9 向 TS 原版对齐（2026-09-11 完成，排序见 `TASKS.md`）**：核实后 TS 原版 12 项核心能力全部真实现（逐条对照与 file:line 证据在 `docs/reference/minicode-notes.md` §10），本项目按「先小后大」分四批靠。**M9-1 ~ M9-8 全部完成，12 项核心能力清单全部落地 + 工作区 rewind 快照（M9-8，2026-09-12）**。**已做 M9-1 改动前 diff 复核**，机制是：
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

**已做 M9-5 常驻交互模式（REPL，`--repl` + `app/repl.py`）**，机制是：
- **它不是新功能，是「换个用法」**：同一个 `AgentState` 连跑两次。这个用法第一次让四条**单发进程里结构上不可观测**的契约成为真实路径 —— 这四条才是这一项的产出，界面只是把它们变成必经之路：
  1. `run_turn` 每回合**复位 `terminated_reason`**（单发时"一个回合"和"一个进程"是同一件事，所以从没有过清空动作；`app/ui_streamlit.py` 会从检查点 payload 里读出它）。
  2. `run_turn` 每回合调 **`permissions.new_turn()`**（清 `_turn`、**保留 `_always`**）。此前 `_turn` 全仓零 clear/reset，单发路径上它随进程一起消失，所以 `tests/test_permissions.py` 把"回合结束"定义成**新建实例**。常驻进程里 `allow_turn` 就变成**永久放行**，与确认框写的「2) 本回合允许」直接矛盾。**调用点在 `_run_loop` 入口**（唯一能让四个入口都拿到它的地方），单发路径上是 no-op。
  3. **`record_discovery` 去重认 `state.events`**，不是认"block 在不在 prompt 里"（第二回合 block 还在 → 每回合往轨迹里再写一份 `skill_discovery`）。
  4. `run_turn` 入口调 **`state.ensure_tool_pairing(messages)`** 补被中断打断的 tool 结果配对，**补了几条就记一条 `pairing_repaired` 事件**（不静默修）。不补的话下一轮请求是 400 形状，而**报错发生在下一回合**、与那次 Ctrl+C 看起来毫无关系。
- **`max_steps` 语义变更（行为变更，要主动说）**：从「整个 state 的累计步数上限」改成**「每轮一份预算」**（`_run_loop` 记 `budget_start = state.step`，条件 `state.step - budget_start < max_steps`）。`state.step` **照样累计不重置**（检查点文件名、`--fork --step K`、轨迹 `step` 字段都依赖它单调递增），只改预算的**度量起点**。顺带修掉一个陷阱：会话跑满 25 步后 `--resume` 旧语义下**一次模型调用都不发**、直接又打印「已达到最大步数」。
- **装配不复制（这一项防漂移的关键）**：`QueryEngine(...)` 原先在 `app/cli.py` 被构造**两遍**（`--resume` 一条路、全新会话一条路，参数逐字相同）。收成 `_Runtime.engine(session)` 一处，单发与常驻从同一处拿。**REPL 若自己装配一遍，迟早漏掉一样** —— CLI 历史上**漏接过 hooks 与 permissions 各一次，两次都是静默的**。
- **两条硬不变量**：① 不认识的斜杠命令**绝不发给模型**（打错一个字母 = 一次真实调用，而回答看起来还挺像回事 → 这个错误不会被发现）；② 会话切换失败**不能半切换**（`_activate` 三样一起换，**engine 必须跟着换** —— `QueryEngine.session` 构造期绑定；"session 换了、engine/state 没换"是**零报错**的错配：轨迹写进 A、你在看 B）。命令判据用 **`raw.startswith("/")`**（strip **之前**的行），所以行首加空格仍当任务发。
- **11 个命令的正文由 `_COMMANDS` 表生成**（`/help` 与命令表写两处迟早对不上），有测试断言 `/help` 列出的名字**恰好等于**表的键集合。
- **退出语必须可执行**：`_farewell` 打印「继续: … `--repl --resume --session-id <sid>`」，而检查点是**按节拍**落的、且**只在有工具调用的步上 tick** —— 纯聊天或只走两步就退出会一个检查点都不落，那条命令直接报"读不到检查点"。所以 `_wrap_up` **退出时强制落一次**（理由同 `_awaiting_user` 的 `force=True`：流程即将因非步数原因退出，节流的下一次 tick 永远等不来）。**这条是写测试时逼出来的，不是真跑**，如实记着。
- **每回合报增量**（`RunResult.steps`/`usage` 是**会话累计**的，五个返回点给的都是 `state.step`）。`Usage` 是**可变 dataclass**、`state.usage += …` 是**原地**累加 → 快照必须 `dataclasses.replace()` 复制，**不复制则相减恒为 0 且不报错**。
- **`await_user` 在 REPL 里不需要任何特殊代码**（模型提问 → 本轮结束 → 打印问题 → 下一行输入就是回答）。这是 M8「打断是数据标志而不是阻塞控制流」的回报 —— 反过来若 REPL 自己 `input()` 一个"回答"，就是把数据标志退化成阻塞控制流。
- 变异测试 **19/19** 被抓住（`m9verify/mutate_m9_5.py`）。**三类要说明的**：一个**证明过的等价变异体**（`_run_loop(..., budget_start=state.step)` 省略该实参恒等，因为兜底就是同一个表达式、两次读之间没有东西改 step）；一个**依赖时序的**（`/new` 用 `unique_session_id` 而非秒级 `new_session_id`，跨秒会 MISS —— 但它防的是真缺陷：两个"不同"会话**静默共用**一个检查点目录）；一个**刻意不设的**（`_dispatch` 的 `except typer.Exit: pass`，去掉后 `CliRunner` 会接住异常、退出码照样 0，从输出上分辨不出来）。
- **一处被变异测试逼出来的测试修正**：空行变异最初 **MISS** —— 空行变成第 3 个回合吃掉脚本响应，最后一条真实输入才报错，而 loop 的 `except Exception` 把它变成 `terminated_reason="error"` 的 `RunResult`、**退出码仍是 0**，原断言照样成立。修法是补 `assert "[error]" not in result.output` 与 `assert users == [...]`（直接钉"模型收到的 user 消息序列"）。
- **真实端到端跑了三轮**（`m9verify/repl_a|b|c.log`，工作区 `m9verify/ws_m95/`）：A 两回合修 bug + 补测试（第二回合**不重读源码**就改对了）；B `/rename` → 重进 → `/fork 2` → 新分支接着跑；C `--review-edits` 下答「2) 本回合允许」→ **下一回合同一个 edit、同一个文件重新被问**（权限记忆键是 `_classify` 给的 `arguments["path"]`，两回合都是字面量 `textstat.py`，**键相同**，所以"重新问一次"只可能来自回合边界上的清空）。

**已做 M9-6 进程内目标（`agent/goal.py` + `agent/tools/goal.py` + REPL 自动推进）**，机制是：
- **它不是任务树。** TS 原版 ④ 的唯一证据是 `docs/reference/minicode-notes.md:141` 一行：「进程内 Goal（跨回合推进 + 暂停/恢复/完成检查）」+ `/goal` 命令族 + `goal/context.ts` —— **带状态机的目标对象，没有任何层级结构**，`isComplete` / 停止条件 / 任务树 / `TodoWrite` 全仓零命中。**面试稿里原先那句「④ Goal（目标任务树）」是没有依据的，已更正**（`docs/interview_guide.md` §10 保留了这次自我更正）。「允许中途改结构」现有 `update_plan`（全量覆盖）本来就能做。
- **判分权在人手里（这一项的全部意义）**：目标由人用 `/goal <目标> --check <命令>` 创建，`check_command` 是**人预先给的可执行判据**。模型只有一个工具 `declare_goal_done` —— 它**声明**，运行时随即跑那条命令，**退出码说了算**。模型不能创建目标、不能改判据、不能暂停/清空（同 `clear_taint` 的纪律：**标记不由被标记者清除**）。**名字刻意不叫 `complete_goal`**：description 是这条能力在唯一常驻请求里的全部说明，而 `complete_goal` 读起来像"调用它 = 完成"。
- **判定是三态，不是两态**（`CHECK_PASSED` / `CHECK_FAILED` / `CHECK_INVALID`）：命令**压根没跑成**（门禁拦下 / 超时 / 工具异常）单独一档，算完成和算没完成都是错的 —— 直接对应 `eval/golden_tasks.py` 的 `JudgeResult.executed` 教训。**未通过不是终止**：判定 + 输出**尾部 40 行**以 `user` 消息回喂，模型在同一回合里接着修。**通过即结束回合**：让模型看到"检查通过"再自己写结论，完成就又变成模型说的话了。**无效也结束回合**：检查命令坏了模型**没有任何办法**修（它不能改 `check_command`）。
- **`terminated_reason` 新增两个值**（`goal_done` / `goal_check_invalid`）+ 集中常量 `TERMINATED_REASONS`（原先只有一行注释、无集合 —— 补的是"一共有哪些值"这个真缺口）。**无效不复用 `await_user`**：那个值连带两件事都不对（`_extract_learned` 会跳过约定提炼、REPL 会打印「需要你补充信息」而这里没有人被提问）。
- **`_run_loop` 只插 4 行，`_execute_tool_calls` 一个字符没改。** 声明经由 `state.goal.declaration` 传递，不走 `ToolResult` 上的第二个标志 —— 批结束时那个 `ToolResult` 已被 `compact_batch` 成字符串，要带出标志就得**改 `_execute_tool_calls` 的返回类型**，而那是 4 个入口共用的核心循环契约（留给 M9-7）。插在 `_run_loop` 里 → 四条入口一起拿到；**批前复位 `declaration = None`** 让「一次声明恰好触发一次检查」是结构性成立的（而不是靠"记得别重复跑"）。
- **检查复用 `_gate_and_run` 而不是 `bash.run`**：PreToolUse 的 block-at-submit、权限 deny、危险命令 ask、**污染天花板**一并生效，且"检查被拦下"有明确判据（`gate_block` 带 `source`/`reason`）而不是靠猜。代价如实记在 README S19（成功的 `pytest` 会写 `data/tests_pass.marker`，从而**替 agent 解锁 `git commit`**）。**绝不伪造** `assistant(tool_calls=[...]) + tool(...)` 消息对 —— 那等于写下一个模型从没发出过的调用，与 `PAIRING_FILLER`「必须说实话」冲突。判定以 `user` 消息回喂（同 `_reinject_plan` 先例）。
- **暂停 = REPL 不再自动续跑**（我们的架构里没有定时器，暂停必须停掉一个真的在跑的东西）。**REPL 结构不变量**：`_run_goal_burst` 结尾**一定** `_pause_goal(stop)`，所以**人拿到提示符时目标绝不可能是 `active`** → else 分支那次「人工回合顺便暂停」永远 no-op，**`/goal pause` 在纯 stdin 流程里结构上不可达**（不是 bug，是「一拍 = 一次授权」的推论；真跑那条动线只证明「人敲一行字后不再自动推进」，**不证明"那一行暂停了目标"**，别讲过头）。**一拍上限不是优化是防锁死**：`input()` 阻塞、无定时器，一拍期间人**根本敲不进字**。
- **目标一个字都不进 system prompt**（同 `plan` 的纪律）：`system` 是 `messages[0]`、在 `_PREFIX_LEN` 保护区内；引导放在**目标被创建的那一刻**（`goal_kickoff_message`）—— 那里天然有一个用户回合，这正是 M8 elicitation gap 的正解形状。
- **`_FIELD_DECODERS` 必须同步加 `"goal"` 一行**（`agent/session.py`）：`load_state` 走 `AgentState(**raw)`，dataclass **不做类型检查** → 漏了它 `state.goal` 是个 `dict`，直到有人读 `.status` 才抛错，而那个炸点被 `_run_loop` 的 `except Exception` 吞成 `terminated_reason="error"`，看起来像「引擎出错」。有测试 + 变异体专钉。
- **`--goal-turns` 不进检查点**（交互策略，不是会话事实 —— 与 `checkpoint_every` 相反）；目标工具**不进 `ToolRegistry.default()`**（eval 用的正是 `default()`，headless 里没人能建目标 → 模型只会看到一个永远失败的诱饵，同 `ask_user`）。
- 变异测试 **20/20**（`m9verify/mutate_m9_6.py`）。**一处如实标注的 MISS**：变异体「人工回合不暂停」被**证明等价** —— 因为 `_pause_goal` 在纯 stdin 流程里本来就不可能在 human-turn 分支产生可观测差异（见上一条不变量），理由写在脚本头部。
- **真跑五条动线全部有日志**（`m9verify/goal_*.log`）。**这些数字与既有单发/REPL 数字不可比**：自动续跑会把 `max_steps` 乘上 `goal_turns`，同一个任务换个 `--goal-turns` 能差一个量级 —— 所以只记动线是否走通，**不引用 token 数字下任何结论**。
- **真跑暴露的是我自己的测试素材错误，不是产品缺陷**：这次真跑用的工作区里有一份我手写的 `m9verify/ws_m96_b/tests/test_extra.py`（**验证素材，不在仓库的 `tests/` 里**），其中 `assert median_word_length("a ccc") == 1.5` 数学上不可能成立（词长 `[1,3]` → 中位数 2.0）。模型算对 2.0、看不懂测试，一整个回合反复探测烧掉 **151,038 token** 撞 `max_steps`；两次 `/goal resume` 又各烧 356,906 / 752,135。**模型始终没有谎报完成**（一直说"测试失败、我还没修好"）—— 这正是 `declare_goal_done` 那条纪律想要的行为。断言已改成 `median_word_length("a cc") == 1.5`。**教训：agent 卡住时先怀疑任务和判据，再怀疑 agent。**

**已做 M9-7 并发子代理（`agent/subagents.py` + `agent/tools/subagent.py` + `loop.py` 的取消/结算）**，机制是：
- **形状抄自 TS 原版**（`src/agents/manager.ts` + `src/tools/sub-agents.ts`）：`MAX_SUB_AGENTS = 3`；`spawn` **立刻返回句柄**、后台跑；**满了直接抛不排队**；`wait` **超时只返回最新状态、不关闭**（「等」和「关」是**分开的两件事**）；`close` = abort + **等它真停**。Python 侧用 `threading.Thread(daemon=True)`，**不用 `ThreadPoolExecutor`** —— 它的线程非 daemon，`atexit` 里 join，一个卡在 120s `llm.chat` 里的 worker 会把**解释器退出拖住两分钟**（症状是"CLI 退不出去"）。
- **`agent/subagents.py` 必须是叶子模块**：`agent/tools/subagent.py` 在**模块级** `import agent.loop`，而 `loop.py` 要做结算 —— 反向 import 即成环。所以管理器 `spawn(task, runner)` 收一个**闭包**，"跑什么"由工具模块决定。
- **`wait(ids=None)` 收的是「还没交回」而不是「还在跑」**（判据是 `reported` 而不是 `status`）。**这是写测试时被逼出来的真 bug**：一次 `[spawn×3, wait]` 里第一个 worker 完全可能在 `wait_agent` 跑起来之前就自己跑完了，只取 running 会让它被跳过、**结论静默丢失**。已交付过的不再返回（同一条信息出现第二次 = 白占 token + 破坏前缀缓存）。
- **`settle()` 的三元口径互斥**：`killed`（跑着被杀，无结论）/ `unclaimed`（跑完没人取，有结论但丢了）/ `still_running`（没停下来，用量不计入本回合）。**另一个被单测逼出来的真 bug**：`unclaimed` 原先没排除被叫停的 worker → 同一个 id 既"没有结论"又给出"结论尾部"，**事件自相矛盾**。
- **回合边界结算 = 一个 `finally`**（`_run_loop` 里），**一处覆盖 6 个返回点 + `KeyboardInterrupt`**。`except Exception` **抓不到 `KeyboardInterrupt`**（`BaseException`），而 Ctrl+C 恰恰是最需要结算的那一刻。**join 有界（`SETTLE_TIMEOUT = 1.5s`）**，与 `close()` 的「等到真停」**刻意不同**：回合边界不该阻塞在网络调用上，且它可能正跑在 `KeyboardInterrupt` 的传播路径上（用户按第二下 Ctrl+C 会在 `finally` 里再抛 → 一个 worker 都没被 join）。**一个会抛的 `finally` 会顶掉正在返回的 `RunResult`**，所以结算内部再包一层 try。
- **取消只有两个检查点，没有第三个**（不去打断 `llm.chat` 和 `subprocess.run`：客户端没有 per-call timeout，`BashTool` 没有 cancel token）：① `_run_loop` 的 `while` 体首句（`_prepare_messages` 与 `llm.chat` **之前** —— 不能写进 `while` 条件，条件求值时 `_prepare_messages` 已经跑完一整轮上下文整理的活）；② `invoke()` 顶部。串行批逐调用；**并发批的粒度是整批**（`executor.map` 一次性提交，检查对每个都是 False）—— **如实记，不声称批内逐调用粒度**。
- **不变式（有测试钉住）**：abort token **只由 `AgentWorkers.spawn` 创建、只装在每个 worker 那一次性引擎上**。常驻 REPL 的引擎**跨回合复用**，给它装 token 会让**后续每个回合**在第一个检查点就返回 `aborted` —— 用户看到的是"我说话它不理"，且完全无法解释。
- **串行路径命中 abort 必须补配对**：`assistant_tool_calls(calls)` 在批末才 append、覆盖**全部** `calls`，少一条 `tool` 消息就是孤儿 id，直接 400，而报错点在**下一轮**。
- **`"aborted"` 是 worker 专有的终止原因**：父引擎拿不到它（Ctrl+C 是 `BaseException`，在 `llm.chat` 里就抛穿了）。`app/repl.py` 那两处是「**方程要求它存在、但结构上不可达**」—— `tests/test_goal.py` 的两条方程（`set(BURST_STOP_REASONS) | {"completed"} == TERMINATED_REASONS`）逐字逼着加。**同样理由：`_extract_learned` 不加 `aborted` 守卫** —— 那是永远执行不到的分支，加了就是本项目的头号缺陷类。
- **用量只在父线程合并**（`Usage.__iadd__` 不是原子的），每个 handle 一个 `usage_counted`、**恰好一次**；**绝不动 `state.last_usage` / `usage_stale_reason`**（那是驱动 compact 的 provider 锚点）。**代价如实记**：worker 的 prompt 大多缓存未命中 → 父会话的**缓存命中率被稀释**（README S26）。
- **五个工具全部 `is_read_only() = False`**（含纯读的 `list_agents`）—— 理由**不是"子代理危险"而是调度**：只读工具走并发池，`executor.map` 保证**输出顺序**不保证**开始顺序**，`wait_agent` 会在两个 spawn 注册 id 之前跑起来。先例是 `DeclareGoalDoneTool`（「它改变的是**控制流**」）。
- **刻意不做（写进注释，不假装实现了）**：父→子**级联 abort 的独立通路**（靠 `settle()` 在回合边界实现，是对 TS 契约的**偏离**）；`closeAll()` 与 `settle()` 分开；`"closing"` 中间态；worker 再 spawn worker。
- **一处对计划的偏离（要主动说）**：**没做 `/agents` 斜杠命令、也没做 `_prompt` 的 `agents:N` 状态位**。理由是它们**结构上恒不触发**：`AgentWorkers` 每回合新建、`settle()` 在 `finally` 里跑，所以**回合之间活跃 worker 恒为 0** —— `/agents` 永远打印"（没有子代理）"，`agents:N` 永远是 `agents:0`，而"恒显一个值会让人不再看它"。两个都正是本项目头号缺陷类的形状（机制在、没人能触达）。替代的可见性出口是 `EventPrinter` 里那条 `subagent_settled`。
- **真实验证**：① 前置探针 `m9verify/probe_concurrent_llm.py`（真联网）—— 4 条并发 `chat()` 共用同一个 `DeepSeekClient`：**无串台、无异常、usage 各自可信**；**第一版测出"并发比串行慢 3.6 倍"是冷启动假象**（第一条真请求付了 39.92s 的 DNS+TLS+连接池成本，全落在先跑的那批头上），**预热后 1.88x**。这是"先把测量方法里的混淆项排掉再下结论"的实例。② 端到端四条动线（`m9verify/drive_m9_7.py` + `m9verify/ws_m97/`，日志 `m9verify/drive_m9_7_*.log`）：**并发 17.4s vs 串行 29.2s（1.68x）**；**`spawn_agent` 各耗时 0/5/0/0ms 而串行 `subagent` 是 5540/7686/7426ms**（句柄契约的现场）；**`close_agent` 1140ms 返回、状态 `closed`、无结论**（"等它真停"的实测代价）；**`abandon` 动线里 `subagent_settled` 真的到达 `EventPrinter`**（`■ 子代理结算：1 个被叫停、0 个结论没被取走`），且模型自己主动提醒"子代理只活这一个回合"。**变异 29/29**。

**已做 M9-8 工作区回滚快照（`agent/workspace.py` + `session.snapshots` + `--rewind/--snapshots/--drop-snapshots`）**，机制是：
- **存储布局**：对象库 `data/snapshots/objects/{sha[:2]}/{sha}.bin` **全局共享**；清单 `data/checkpoints/{sid}/ws/step-K.json` **按会话隔离**。**对象全局共享是实测推翻初稿的**：E 动线实测 `--fork` 的**对象增量 0 个 / 0 字节**（只有清单 +599 B）—— 项目初始文件、被改回原样的内容都命中同一份对象；按会话隔离会让同一份内容在每个会话各存一遍。代价：回收不能整目录删 → `drop_snapshots` **现算 live set**（`_live_shas` 扫所有剩余清单）。
- **三条不变式**（每条有测试钉着）：① `path ∈ manifest[K]` ⟺ `first_touch(path) ≤ K` → 清单是**全量**的，每步可独立还原（"按序重放前像"在中间缺一环时是**静默还原出错**）；② **`base`（我们碰它之前它长什么样）只记在首触那一步的清单里** —— 没有它，"回滚到第一次修改之前"就只能**删掉那个文件**，而它可能是仓库里人写的、我们并不认识的文件；③ **写盘顺序：对象 → 清单 → 检查点**（`capture` 跑完 `Session._write` 才落检查点），被杀只留**孤儿**（不可达字节），不留**说谎的引用**。孤儿**如实报出、不自动删**。
- **`plan(K)` 的 `K < lo` 那一支必须单独有**（`lo = min(manifests)`）：节拍 5 时第一个清单落在第 5 步，而 `plan(5)` 读的是第 5 步**盘面**（写完之后的），于是"撤销 agent 做过的一切"在最常见的形状下根本表达不出来 —— **没有这一支，`base` 是结构上不可达的**。夹在两快照中间的步（快照在 5 和 10、要回到 7）**仍然报错**：那是"不知道"，不是"没有"。
- **"不在管辖范围"是独立维度**（决议 6）：`plan` 同时给 `outside`（没被 write/edit 碰过的文件数）、`shell_calls`、`other_calls`，**跟着预览一起印出来**。把"文件系统回滚"与"不可回滚的外部副作用"混为一谈是推卸责任 —— `bash` 的 `rm/mv/重定向`、MCP 写入、目录增删、权限位/mtime、进程外的一切**明说不管**。回滚区间是**左开**的 `(target_step, 最新检查点]`，数据源是最新检查点的 `state.events`。
- **`--rewind` 默认只预览 + 二次确认（默认 n）**：`plan()` 纯读，输出 git-diff 风格四类（恢复/删除/不变/无法还原）。理由不是保守 —— 回滚是**全项目唯一一个不可逆的写操作**（对象库留了旧字节，但被覆盖的文件本身没有 undo）。`--force` 跳过；没有 changes 时不弹框但仍返回步号（调用方靠它拼"继续"那句 `--resume --step K`）。REPL 里是 `/rewind [step]`，`_confirm_rewind` 用 `input()` 而非 `typer.prompt`（常驻模式里后者会和自己的行读取打架）。
- **回滚之后必须告诉模型**（`_reinject_rewind`）：历史里写着"我改了 a.txt"而盘上已经没了，它会基于**不存在的现状**推理。触发判据是 `state.step > target`（等于或早于时历史与盘面一致，补投是噪音还会破缓存）。`last_rewind` 落 **meta 而不是只打印** —— 打印的字留在上个进程的屏上，下个进程要读文件。
- **`--fork --step K --rewind`**（决议 3，"倒带重试"）：fork 搬**对话**、rewind 搬**文件**。`_copy_snapshots` 只复制 `ws/step-n.json`（n ≤ K）、**一个对象都不复制**（库是全局的，sha 仍可读）；分叉点之后的清单**不搬**（那是源会话在分叉点之后的历史，搬过去就是**说谎的记录**）。
- **不加锁（决议 7，实测支撑）**：对象与清单**只由父线程写**（写工具不是只读工具，走串行分支；worker 的引擎 `session=None`，结构上拿不到本模块 —— 与"worker 写不了检查点"是同一条保证）。全局对象库的并发写实测（8 线程 × 5 轮 × 6 次重跑）：**裸 `os.replace` 冲突 24~30/40（全是 Windows `PermissionError(13)`），经 `_put_object` 未捕获异常 0 次** —— 容错就是"看目标在不在"（内容寻址，同 sha 必同内容），**不是加锁**。如实标注：Linux 上那条容错分支可能是死代码。
- **登记点挂在 `loop._gate_and_run` 工具成功之后**（`ToolResult.file_changes` 由工具在**写盘之前**读出原样字节报告）：于是"权限拒绝的那次调用压根没执行 / edit 匹配失败 / 工具自己抛了"三种情况是**结构上**走不到登记的，不靠谁记得判断。`base_unknown=True`（存在但读不到）**不纳入管辖** —— 当成"不存在"就会在回滚时**删掉用户的文件**。
- **决定之前先测（决议 1 的输入）**：基座 = 被 write/edit 碰过的文件原始字节之和，**不是整个工作区** —— 实测 **1949 B / 8892 B = 21.9%**，且只取决于"改了几个文件"。所以**不给基座加开关**（磁盘比用户的时间便宜；没有基座，"回滚到第一次修改之前"就可能删掉未跟踪的用户文件）。清单 599 B vs 对象 4339 B ≈ 13.8%，孤儿 0 个；回收 1 个对象 / 746 B，3 个因别的会话仍引用而保留。
- 测试 `tests/test_workspace.py` **49 例** + 接线契约 22 例；**变异 35/35 零 SKIP 零 MISS**（`m9verify/mutate_m9_8.py`，其中 5 处是**证明后不设**的候选）；**707 测试全绿**。六条动线 A~F 真跑（`m9verify/drive_m9_8.py`）**数字与 README 的 token/成本/缓存数字没有任何关系，不可混着比**（口径见驱动脚本 docstring）。

## 硬约束（不可违反）

- LLM 只用 DeepSeek（国内 API）；生产 `deepseek-chat`，测试用 MockLLM
- 所有路径操作在沙箱 `workspace_root` 内；bash 拒绝危险命令
- 数据全真实：agent 操作真实代码仓库，不造假
- 每个模块写完必须跑对应 pytest 通过才算完成

## 模块职责（一句话）

| 模块 | 文件 | 职责 |
|---|---|---|
| 循环 | agent/loop.py | QueryEngine：think→tool→observe→finish，只读并发/串行，终止判定+循环检测（含 M9-6 的目标判定 `_verify_goal`）；M9-7 加 `abort` 取消检查点 ×2 与回合边界的 `_settle_workers` |
| 子代理 | agent/subagents.py | M9-7 并发子代理管理器：`AgentWorkers`（spawn/列表/等/关/结算）+ `AgentHandle`/`AgentSnapshot` + `AbortToken`。**叶子模块，不 import `agent.loop`**（`agent/tools/subagent.py` 模块级 import loop，反向即成环）—— 跑什么由调用方传进来的闭包决定 |
| 目标 | agent/goal.py | 进程内目标状态机（active/paused/done）+ 三态完成判定 + 渲染（M9-6） |
| LLM | agent/llm.py | BaseLLM → DeepSeekClient / MockLLM；usage+cache 采集 |
| 工具 | agent/tools/ | base(基类+schema+`preview` 预览钩子+`ToolContext.workers`) / bash / files(read·write·edit·glob·grep) / web(fetch·search + SSRF) / **subagent(5 个工具族：subagent·spawn_agent·list_agents·wait_agent·close_agent，唯一构造点 `build_subagent_tools`)** / ask / plan / **goal(declare_goal_done)** / skills |
| 状态 | agent/state.py | 消息构造（OpenAI 格式）+ AgentState（含 `plan`、`goal`） |
| 上下文 | agent/context.py | provider-usage-first 记账 + cache-aware 布局 + 分级截断/snip/LLM compact（M3·M8） |
| 工具结果 | agent/tool_result.py | 超大工具结果落盘 + 预览替换 + 批预算（M3） |
| 权限 | agent/permissions.py | once/turn/always 决策粒度 + 黑名单 + 沙箱 + 确认文案带 `details`（M2·M9） |
| 钩子 | agent/hooks.py | Pre/PostToolUse + block-at-submit（marker 由测试成功自动写）（M2） |
| 会话 | agent/session.py | JSONL 轨迹 + 检查点 + resume + **step 级分叉 / 会话命名 / 清单**（M3·M9-3）；`_FIELD_DECODERS` 是新增 state 字段的**必改点**（M9-6 的 `goal`、M9-8 的 `last_rewind`），`_write` 里 **`snapshots.capture` 必须在落检查点之前**（M9-8 写盘顺序） |
| 快照 | agent/workspace.py | M9-8 工作区快照 + 回滚：`WorkspaceSnapshots`（登记/按步落清单/`plan`/`restore`）+ 全局内容寻址对象库 + `drop_snapshots` 现算活跃集回收。**登记只由父线程、只在工具成功后**（`loop._gate_and_run`），故不需要锁 |
| 记忆 | agent/memory.py | 分层指令文件(@include+去重+预算) + 提取 + 简化 consolidation（M4） |
| 技能 | agent/skills.py | SKILL.md 渐进披露：只把 name+简介进 system prompt，正文由 load_skill 按需取（M8） |
| 安全 | agent/security.py | 注入文本检测（**概率性，只出告警**）+ 会话级污染标记 + 来源框架（M7） |
| MCP | agent/mcp.py | 手写 MCP 客户端：`Transport` 抽象 + stdio/Streamable HTTP 两种实现，协议层与传输分离；tools/resources/prompts 三个能力面（第三方工具照样过权限/hooks）（M6-3 / M9-4） |
| 入口 | app/cli.py · app/repl.py · app/ui_streamlit.py · app/replay.py | typer CLI（含 `_Runtime`/`_build_runtime` 唯一 `QueryEngine` 构造点） / **常驻交互模式** / Streamlit 控制台 / 检查点回放 |
| 评估 | eval/golden_tasks.py · runner.py | 黄金任务 + 完成率/成本回归（M5） |

## 常用命令

```bash
python -m pytest tests/                      # 跑全部测试（每模块完成后必须过）
python -m pytest tests/test_xxx.py -k 用例    # 单模块/单用例
python -m app.cli --mock "任务"               # 无 key 演示
python -m app.cli "任务"                      # 真实 DeepSeek（需 .env 配 DEEPSEEK_API_KEY）
python -m app.cli --resume                   # 从最近检查点续跑（配合 Ctrl+C 演示）
python -m app.cli --plan                     # 只看最近会话的**任务计划清单**后退出（无 key 也能跑）
python -m app.cli --goal                     # 只看最近会话的**目标**（目标/状态/判据/最近判定，无 key）
python -m app.cli --resume "补充的信息"       # 回答 agent 的提问后续跑（配 --session-id 更稳）
python -m app.cli --sessions                 # 列出会话：名字/步数/分叉来源（无 key）
python -m app.cli --rename "基线方案"          # 给最近会话起名（只动元数据，无 key）
python -m app.cli --fork --step 3 "换个思路"   # 从第 3 步分叉出新会话并续跑（**对话**分叉，文件不动）
python -m app.cli --snapshots                # 列出有快照的会话 + 全局对象库占用/孤儿数（无 key）
python -m app.cli --rewind --step 3          # 回滚**预览**（默认只预览，确认之后才动盘）
python -m app.cli --rewind --step 3 --force  # 真回滚工作区到第 3 步（覆盖/删除文件，不可逆）
python -m app.cli --fork --step 3 --rewind --force   # "倒带重试"：对话 + 文件一起回到第 3 步
python -m app.cli --drop-snapshots --session-id <id> # 删该会话快照并回收无人引用的对象
python -m app.cli --resume --session-id "基线方案" "接着改"   # 会话 id **或名字**都能用来指会话
python -m app.cli --repl                     # 常驻交互模式：一行一个回合（M9-5）
python -m app.cli --repl "先跑一下测试"        # 带任务：它作为**第一个回合**跑掉再进提示符
python -m app.cli --repl --resume --session-id <sid>   # 恢复后接着聊（退出时会强制落一次检查点）
python -m app.cli --repl --goal-turns 5      # 目标自动推进一拍最多几个回合（默认 3，M9-6）
# 提示符内：/goal <目标> --check <命令> 建目标并自动推进；/goal status|pause|resume|clear
# 提示符内：/rewind [step] 把**工作区文件**回滚到第 N 步（同样默认只预览、要确认；对话另用 /fork）
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
