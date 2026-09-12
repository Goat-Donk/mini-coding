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

## M5-5 评估层加固 + 三臂真跑（2026-09-12）★「尺子先可信，再量」

**起点**：M5 那两条跑分（`--limit 2`）**不能作为依据**——侦察后发现的问题不是"样本只有 2 个"这么轻，而是**尺子本身不可信**，且 README 里有一句明确的假话（"绕开了 SWE-bench 的测试泄漏陷阱"，实测不成立）。所以这一轮的目标定为：**先让尺子可信，再量**。

- [x] **M5-5a 物化改「物理剥离」**：放弃 `git worktree add --detach`（它与主仓库**共享对象库与 refs**，fix 提交就在主仓库历史里，agent 一条 `git show <fix_sha>:tests/...` 就能拿到隐藏测试全文与金标准补丁，而 bash 工具只校验 cwd、不校验命令文本）。改为 `git archive base_sha` 流式导出 → 解到隔离区 → `git init -q -b master` + `git add .` + `git commit -m "Base commit for evaluation"`，给 agent「一个没有未来的单一初始提交」。硬判据：`git cat-file -e <fix_sha>` 从 exit 0 → **exit 1**（不是"不可达"，是对象**根本不在**）、`git rev-list --all --count` 748 → **1**、`in-pack: 3812` → `count: 78`、`git remote -v` 空。**每次跑都过一遍 `leak_probe`**，把"不泄漏"从一句声称变成一条**每次跑都测**的量（21/21 成立、0 泄漏）。
  - 顺带修：归档 tar 流的 `pax_global_header` 里写着 `comment=<base_sha>` → 必须整条流读进内存再解包，**绝不落进工作区**；`mode="r:"`（可 seek）而非流模式 `r|`（遇 symlink 条目会 `StreamError: seeking backwards is not allowed`，实测把 7 个候选记成"闸门自身异常"）；Windows 上 linkname 不是路径的 symlink 建不出来 → 物化后逐条比对并**打印出来**；目标目录已存在 → **自愈**（先删再建并打一行）；`_force_rmtree` 必须清只读位（否则 agent 跑一次 `git gc` 后下一轮清理 `PermissionError`）。
- [x] **M5-5b 有效性闸门**（SWE-bench FAIL_TO_PASS 式，**进 LLM 之前**跑，零 token 成本）：隐藏测试必须在 **base 失败、在 fix 通过**。base 侧 `rc==0` → 拒（**白送分**）；`rc==1` → 过；`rc==2` → **接受并打标 `base_collect_error`**（收集错误是 2，写成"必须 rc==1"会误杀合法任务）；`rc∈{3,4,5}` → 拒。fix 侧必须 `rc==0`，否则拒「金标准补丁在本机跑不过：这个任务谁都拿不到分」。**fix 侧同时就是 oracle 基线（= 100%）**——没有它，"0% 完成率"和"judge 坏了"在报告上长得一模一样。用 `tempfile.mkdtemp()` 物料，**绝不复用 `ws_root/task.id`**（judge 会把隐藏测试写进传入目录，复用 = 闸门自己制造泄漏）。
  - **39 个候选 → 21 个有效，拒 18 个**。被拒的头一个就是 README 曾经宣传的那个分子的来源：`e70f9b1d fix: correct Table.update transform type hints` 的源码 diff 逐行核对**只有 1 行 import + 2 行类型注解 + 1 行 docstring，零可执行语句变化**——agent 一个字不改，judge 也给 `109 passed`。
- [x] **M5-5c 指标诚实性**：`judged = error is None and not patch_failed`（进分母）；`effective_pass = judged and passed and invalid_reason is None`（进分子）。修掉两个漏洞：① `passed` 原先**没按 `error` 过滤**，一个任务可同时 `passed=True` 且 `error!=None`，**进分子不进分母**（极端算例：n=2 → 打印「完成率 100%（1/1）」）；② `steps == 0` 或工作区**零变更**时即使测试通过也标 `invalid`——闸门管不住 agent 改工作区之外的东西（往 site-packages 塞 conftest、装包）让测试变绿。**顺序是硬要求**：快照必须在 `judge` **之前**（judge 会把隐藏测试写回工作区，之后算 diff 永远非零）。
  - 报告补齐自证字段（`arm` / `model` / `base_url` / `repo_head` / `pricing_snapshot_id` / `gate` / `candidates`）+ 判定字段（`passed_effective` / `patch_error` / `raw_output` / `budget_exhausted` / `judge_tampering`）。**没有自证字段，两份报告是否同一批任务、同一把尺子就无法判定。**
- [x] **M5-5d `agent/pricing.py` 定价快照**：单价从散在两处的硬编码常量变成带 `id` / `verified_on` / `status` / `note` 的记录，报告里写 `pricing_snapshot_id` + `pricing_note`。查不到价时**不返回 `None`、不抛异常**，回落最近一条 `archived` 快照并如实标注。`deepseek-chat@2025` 的 `verified_on` **刻意留 `None`**——那组价是从 2025 年沿用的常量注释，**从未对着官方定价页核实过**；编一个日期填进去比"未核实"更糟，因为它看起来像核实过。
- [x] **M5-5e 三臂真跑**（`--arm agent|one-step|single-shot`）：`agent`（多轮循环，`max_steps=25`）/ `one-step`（**一切相同，只把 `max_steps` 25→1**，零风险真受控）/ `single-shot`（无工具、一次调用、要求输出 unified diff 由我们 `git apply`）。跑之前做**六项交叉验证**（模型 / 仓库 HEAD / 定价快照 / 闸门有效数 / 有效任务集 / 候选集），**逐字段一致才出数**。
- [x] **M5-5f 评测器审计 —— 修 `_FENCE_RE` 围栏配对错位，并做零成本离线重算**（详见下）

### 「评测器审计」这一项值得单独记：5 个假阴性

**bug 本体**（`eval/runner.py:279`）：`_FENCE_RE = re.compile(r"```(?:diff|patch)?[ \t]*\n(.*?)```", re.DOTALL)`——**开围栏只认 ` ``` ` / ` ```diff ` / ` ```patch `**。而模型回复里 diff 块**前面**常有一个 ` ```python ` 代码块，它当不了开围栏 → **围栏配对整个错位一格**：` ```diff ` 行被上一个散文块当成**闭围栏**吃掉，真正的 diff 从此没有开围栏 → `findall` 抠不到 → 落到兜底分支 `text[idx:]`「从 `diff --git` 切到**全文结尾**」→ 把模型后面的说明文字整段喂给 `git apply` → `corrupt patch`。**它一条测试都不会变红，却直接改掉头号指标。**

- **诊断**：`dcf0a013` 上逐字节取证——匹配跨行 27→38、382 字节、闭围栏是 `'复\n\n```'`。写离线脚本做 A/B/C 三档对照：**A（现行解析器）在 7 个任务上复现 0/7 通过**（证明复现忠实），**B（只修围栏 info string）/ C（B + 兜底截断）救回 5/7**，且 **B 与 C 结果完全相同** → 真正起作用的是**围栏配对**，兜底截断只是保险。
- **归因（7 个全覆盖）**：**5 个是我的解析器**（`dcf0a013` `6a84ca9c` `1e865a86` `f959c8cf` `08305a1e`，修好后都是真通过）；**2 个是模型自己的**（`45a4d4b3` 把相隔十几行的两段拼成一个 hunk，那段上下文根本不存在；`0e1ba9ca` 18,867 字节全在分析、一个 `diff` 都没输出）。
- **为什么走离线重算而不是改 runner**：`eval/runner.py` 本轮**已冻结**（三条臂必须跑同一份代码，中途改就等于三份分数不可比）。报告里存了每个任务的 `raw_output` → **重抠 + 重落 + 重判，LLM 调用 0 次**。
- **四条硬不变式**（缺一条，"重算"就退化成"重编故事"）：
  1. **零 LLM 调用**。它回答的是「**如果解析器没这个 bug 会被判成什么**」，**不是**「模型能打多少分」。
  2. **先自校验，再出修正值**。第 0 阶段用**现行**解析器重算一遍，必须**逐字段复现**留档聚合（21 / 14 / 11 / 7 / 0）才继续；对不上就说明重算流程本身有问题，此时任何"修正值"都不可信。
  3. **不覆盖留档**。修正结果写另一个文件（`evalverify/report_single_shot_rescored.json`），`data/eval/report-*.json` 一个字节都不动；输出 JSON 带 `rescored` 溯源块。
  4. **修复代码只有一份**。`evalverify/diff_extract_fixed.py` 是唯一真相源，两个消费脚本都 import 它——**两份副本 = 迟早漂移**，而这个 bug 的教训正是「解析器会悄悄决定分数」。
- **结果**：`single-shot` 补丁未应用 7 → 2、通过 11 → 16、分母 14 → 19、**完成率 52.4% → 84.2%**。**两个数都留着**：只报修正值 = 抹掉自己犯过的错，只报原值 = 拿自己的 bug 当模型的能力；差额 5 个任务**全部归解析器**，与模型无关。
- **顺带确认的一条边界**：`git apply --recount` 能修 hunk 行数，**修不了错的上下文**——`45a4d4b3` 那种拼出来的 hunk，上下文行在文件里根本不存在，`--recount` 救不回来。这条写进了 README 的已知边界。

### 三臂最终数字（同一批 21 个有效任务，同一把尺子）

| | `agent` | `one-step` | `single-shot`（留档） | `single-shot`＊（离线重算） |
|---|---|---|---|---|
| 计分（完成率分母） | 21 | 21 | 14 | 19 |
| 通过 | 14 | 0 | 11 | **16** |
| 能力完成率（passed / judged） | **66.7%** | 0.0% | 78.6% | **84.2%** |
| 补丁未应用 | 0 | 0 | 7 | 2 |
| 撞 `max_steps` | 3（**推定**） | 21（精确） | 0 | 0 |
| token / 成本 | 2,892,745 / ¥2.0754 | 42,073 / ¥0.0408 | 341,337 / ¥0.8423 | 同留档 |
| 每次成功修复的边际成本 | ¥0.1482 | — | ¥0.0766 | ¥0.0526 |

- **`agent` ∪ `single-shot`＊ = 19/21 = 90.5%** —— 这是**并集**（两条通路合起来覆盖多少），**不是任何单臂的成绩**。README 里已明确禁止把它写成「agent 的成绩」。
- **配对四格**（agent vs single-shot＊）：都修好 11 · 只有 agent 3（`0e1ba9ca` `45a4d4b3` `9eaf5626`）· 只有 single-shot 5（`06d64f46` `1e865a86` `5bb3a9e5` `9f55011e` `f3b18d2a`）· 都没修好 2（`03f8df75` `770486ff`）。
- **解耦归因**：agent 独有 3 = 纯能力 1 · 输出合规 2；single-shot＊ 独有 5 = 纯能力 2 · **预算 3**（那 3 个恰好就是 agent 那 3 个撞 `max_steps` 的任务）。⇒ **剥掉评测器偏差、输出合规、预算之后，真正的能力差异只剩 3 个任务——不足以支撑任何架构结论。**
- **所以"多轮循环值多少"不能用 `agent` vs `single-shot` 回答**（那个差里混着三样非能力因素）。要测循环本身，看**受控对** `agent` vs `one-step`：**66.7% vs 0%**，两条臂同工具、同提示、同流程，**只差 `max_steps` 一个数**。
  - ⚠️ 但 `one-step` 的 0/21 **不能读成「循环让模型变聪明了」**：它 21/21 撞满 `max_steps=1`、21/21 工作区**零变更**——它**没有机会**产出改动。这一对量的是「给不给得起第二次机会」，不是「循环的智能」。

### 遗留（**本轮已全部收口**，见 M5-6）

- ~~**`_FENCE_RE` 的 bug 仍在 `eval/runner.py` 里**~~ → **已回写**（唯一真相源现在在 `eval/runner.py`，在版本库里），并给报告加了 `ruler` 指纹。离线重算的 84.2% 仍是**反事实口径**，与重跑得到的实测数并列引用、不可混讲。
- ~~**`conftest.py` 判定器注入绕过**~~ → **守卫默认已开**：P4（`rc==0` 时要求至少一个用例真的通过，堵住「全 skip 也是退出码 0」）+ P1（判定前把判定相关文件恢复到 agent 动手之前，且**记录**恢复过哪些）。`--no-guard-judge` 保留为复现归档口径的唯一入口。**仍未覆盖**：`sitecustomize.py`/`.pth`/工作区外的 site-packages/模块遮蔽，逐条记在 README S 表。
- ~~**`env_timeout` 取证不到**~~ → `terminated_reason` 与 `tool_calls`/`gate_blocks` 已落进 `per_task`。**归档那三份报告改不了**（字段是之后才落地的）。

## M5-6 前三个优先级：S16 沙箱 + 评估层三条遗留 + 扩样本的零成本部分（2026-09-12）

**起点**：M5-5 把尺子做成了一件能自证的事，但**它自己留了三个口子**，其中最大的一个不在评估层 —— `bash` 子进程整个绕过了路径沙箱（README S16）。三条加在一起的性质是：**已经留档的数字没法逐条复核**。本轮把它们收口，并把扩样本的零成本部分做完、把花费摆出来。

- [x] **S16 收口：bash 的 `command` 文本受路径沙箱约束**（**分层**：相对逃逸 DENY，其余 ASK）。先把两份沙箱语义合成一份 —— 抽出 `resolve_in_workspace()`，`read`/`write`/`edit` 与 bash **共用同一个函数**（`tests/test_permissions.py` 有一条用例专门钉「两份语义不许漂移」：对一组 raw 路径断言 `engine._resolve(...) is None` ⟺ bash 判 DENY ⟺ `ReadTool` 失败）。判据分两档：**相对逃逸硬 deny**（`../.env` 锚定 cwd 后确实在工作区外，与 `read ../.env` 被拒是同一个不变式）；**绝对路径越界 / 含变量与命令替换 → ask**（`grep -rn "/usr/lib" .` 这种模式串误杀必须可恢复）。**两处执行点**：`BashTool.execute()` 步骤 3（`ctx.permissions is None` 时生效 —— **eval 走的就是这条路**，S16 的实测证据正是在 eval 工作区里跑出来的）与 `PermissionsEngine._decide()` 的步骤 1'（**在记忆之前**，否则用户开过一次 `allow_always` 就永久失效）。
  - **撤掉一条设计**：`-P`（`PYTHONSAFEPATH`）**不采纳**。合成的 flat 包**不带 `tests/__init__.py`** 时，`-P` 会掐掉 `python -m pytest` 提供的 cwd 插入 → `import <pkg>` 直接 `ModuleNotFoundError`；带 `tests/__init__.py`（tinydb 的情形）才无害。它挡的模块遮蔽换来的代价是**把一个仓直接跑废**，不划算。
  - **实测**：`cat ../.env` / `type ..\.env` / `git -C ../o status` / `cp ../.env /tmp/x` 全部拒；`python -m pytest tests/ -q` / `git status` / `ls -la` / `pip install requests` / `cd ..` / `echo hi` 全部放行；URL 与 `/dev/null` 放行；引号内的路径抓得到；`cat $HOME/.env` 在**工具档不拒**（钉「无引擎只执行 deny 档」）。新增边界 S43–S49 逐条写进 README。
- [x] **B1 `_FENCE_RE` 回写 + 报告尺子指纹**：判据按 `evalverify/diff_extract_fixed.py` 的 C 档重写进 `eval/runner.py`。**先写会红的用例再修** —— 回归夹具从留档的 5 个假阴性任务的**真实 `raw_output`** 里裁一段（散文 ` ```python ` 块 + ` ```diff ` 块 + 尾部散文）内联进测试，不依赖归档文件、无密钥。报告新增 `ruler` 块：`judge_version` + 两个源文件的 sha256 + `pytest.__version__` + `sys.version` —— **六项交叉验证里原先没有评测器自己**，而后两个直接决定 judge 结论。
- [x] **B2 判定守卫默认翻转为开**：三件一起做（默认 `True`、新增 `--no-guard-judge` 保住归档口径的可复现性、同步 `judge_note`），并改掉那条「不传参数就断言 `judge_guard is False`」的用例。
  - **P4（先做，价值最高）**：`rc==0` 时要求「至少有一个用例真的通过」。根因是**全 skip 时 pytest 退出码也是 0**——原来的守卫盯的是「谁改了文件」，**没盯「这次判定到底跑没跑测试」**。
    - ⚠️ **`None` 绝不能读成 0，且必须从完整 `out` 解析而不是 6 行 `summary`**：留档 `45a4d4b3` 的 `judge_summary` 完全被 `PytestUnraisableExceptionWarning` 的 traceback 占满、搜不到任何 `N passed`，而它是一次**真实通过**。把「数不出」读成 0 会让**修 bug 的动作本身制造一个新的假阴性**。
    - 顺带修掉统计行的锚定：pytest 8.4.1 在 `seconds >= 60` 时写成 `1 passed in 65.32s (0:01:05)`，少了 `(H:MM:SS)` 那一档就整条数不出来 → P4 静默失效。
  - **P1**：判定前把判定相关文件恢复到 agent 动手之前。内容**不用 git HEAD**（agent 可以合法 `git commit`，HEAD 可能已带篡改版），而是在 `before` 快照那一刻额外抓一份 `{相对路径: bytes | None}`。恢复点在 `judge` **之前**，硬顺序 `… → snapshot(after) → zero_change → judge_tampering → restore → judge`。新增 `judge_restored` 字段（三态：`None` = 没做）。
    - **名单是个正确性陷阱**：`tests/__init__.py` 必须按**相对路径**匹配 —— 把 `"__init__.py"` 加进按名字匹配的集合会让 `tinydb/__init__.py` 这类**合法源码**变成判定相关文件，恢复时把它**还原回去**，直接制造假阴性。所以引入 `_JUDGE_SENSITIVE_PATHS = {"tests/__init__.py"}`，与名字集合取并。
    - **`--noconftest` 已实测排除**：tinydb 的隐藏测试**真的用** `tests/conftest.py` 的 fixture（抽样 8 个任务，4 个用到），而它永远不被 judge 覆盖、是**基线文件**。用了会把诚实的通过也打掉。
- [x] **B3 `terminated_reason` + 工具调用记录落进 `per_task`**（**纯序列化，不新增采集**）：`terminated_reason` 原样落盘；`tool_calls` 从 `run.events` 投影（保留 `aborted` 三态 —— 它的语义是「这条调用**没有执行**」，与「跑了但失败」必须分开；只有 web 工具额外带截断到 300 字符的 `arguments`）；`gate_blocks` 让「拒绝」在轨迹里可见。`single-shot` 臂是手工构造的 `RunResult` → `tool_calls: []`（**是 `[]` 不是 `None`**：「没有工具可用」是事实，不是「没检查」）。
- [x] **变异测试（牙齿检查）· 四套，逐条打断新不变式**：`s16verify/mutate_s16.py` **16/16**、`evalverify/mutate_b1.py` **8/8**、`evalverify/mutate_b2.py` **17/17**、`evalverify/mutate_c.py` **1/1** —— **42/42，零 MISS 零 SKIP**，四个脚本退出码全 0，日志 `evalverify/mutate_m5_6.log`。A 套打断的是：去掉 `relative_to` / 去掉 `expanduser` / 去掉命令位置豁免 / 去掉 URL 剔除 / 去掉引号内重扫 / 去掉「必须含分隔符」门槛 / 去掉 `ctx.permissions is None` 守卫 / 把步骤 1' 挪到记忆之后 / 把 DENY 改成 ASK / 让 `_resolve` 重新自己实现一份；B2 套打断的是：把 `passed = rc == 0` 退回 / 从 `summary` 解析计数 / **把「解析不到」读成 0** / 恢复挪到 `after` 快照之前 / 挪到 `judge` 之后 / 不记 `judge_restored`。跑完核对 `git status`：**22 改 + 1 未跟踪**（`tests/real_fence_desync.py`），**无变异残留**。
- [x] **零成本探针**：`-P` / `--rootdir` / `--confcutdir` 是否采纳 → `-P` 不采纳（见上），后两个**采纳**并写进 `_pytest_argv`（它们把 rootdir 钉在工作区内；不然换一个**不自带 inifile** 的仓，rootdir 会一路爬到本项目根，评测器自己的 `conftest.py` 会在判定期被加载）。
- [x] **扩样本：多仓参数化 + 第二仓闸门 + 成本上限**（零 LLM 成本）
  - **实测硬约束**：tinydb 上同时改源码+tests 的合格提交**恰好 39 个** ⇒ `--limit` 上限就是 39，**再加大一个都不多**。所以 50+ 必须加第二个仓库。
  - **第二仓选 `sqlparse`**（在 25 个候选里实测筛出）：flat 布局、**原地 `pytest` 收集 rc=0**（不需要装任何东西）、**143 个候选**；代价是它**没有 inifile** —— 正好用来验这一轮 `--rootdir`/`--confcutdir` 的修复（tinydb 有 inifile，对它是个 no-op）。体积 168,862 字符 ≈ 2.00× tinydb（按实测 3.79 字符/token 折算 ≈ 44.6k vs 22.3k）。
  - **判据纠正**：调研脚本原先报包体积时把**全部 `.py`（含 `tests/`）** 当成了 single-shot 的输入，而真正的尺子是 `eval.runner._collect_sources`（全部非 `tests/` 的 `.py`）。`runner.py` 里「全包 ≈ 138 KB ≈ 35k tokens」那句话正是用错了尺子（138,575 字符是**含 tests** 的数），已改正。
  - **顺带挖出并修掉一个真缺陷**：`test_files` 取的是「`tests/` 下任何改动文件」，于是非 Python 的测试**数据**文件（`tests/files/*.sql`）也被当节点交给 pytest → `ERROR: not found: ... (no match in any of [<Dir files>])` → **退出码 4（usage error）**，一次用例都没跑，而闸门把它记成「判定无效」，**丢掉一个本来有效的任务**。影响面：sqlparse 143 个候选里 5 个（**现在是 0 个**），tinydb 39 个里 0 个（⇒ **对已留档的报告零改动**）。修法：`_pytest_targets()` 只把 `.py` 交给 pytest；全是非 `.py` 时直接返回「拒绝零验证通过」，**不起子进程**。`JUDGE_VERSION` 3 → 4。
  - **第二仓闸门实测**：修复前 **143 候选 → 51 有效 / 拒 92**；修复后 **143 → 54 有效 / 拒 89**（正好 +3 个：`791e25de` `d7b1ee37` `990500a1`，**零丢失**，`base_rc=4` 计数归 0）。拒绝画像（逐条数过，89 条对得上）：**64× `base_rc=2 fix_rc=2`**（Python-2 时代的树在本机 3.12 上收集期就过不去，两侧同号）、**22× `base_rc=1 fix_rc=1`**（隐藏测试在 base 就失败 —— 正常；但金标准补丁也没让它变绿 ⇒ 这个任务谁都拿不到分）、**3× `base_rc=0 fix_rc=None`**（base 就通过，白送分）。
  - **样本合计：tinydb 21 + sqlparse 54 = 75 个有效任务**（闸门侧实测）。**按臂的花费上限**：sqlparse 三臂 ≈ **¥6.28**（agent ¥2.07 + one-step ¥0.15 + single-shot ¥4.05）；两仓新增三臂合计 ≈ **¥7.88**。**这两笔没跑，等点头。**
- [x] **付费：`single-shot` 重跑**（唯一一笔花钱的，¥0.85 预授权）
  - **第一次跑错了，如实记**：我按 `--limit 21` 跑，把它当成了「21 个任务」——实际它是**候选提交数**，21 个候选只闸出 **13 个有效任务**，所以那一次**没有**把 84.2% 变成实测（花了 **¥0.2374**）。跑完核对 `run_eval` 的函数体与留档报告的 `candidates = 39` 才发现，`--limit` 的 help 文案这时还写着「跑几个任务」，**是我写错的**，已改成明写「候选数，不是任务数」并带上 39→21 / 21→13 两个实测。
  - **第二次按 `--limit 39` 重跑**（`report-20260912-210149.json`）：候选 39 → **有效 21**（与归档同一批）、通过 **17/19 = 89.5%**、补丁未应用 2、token 337,428、**估算成本 ¥0.3453**、缓存命中 **98.6%**。
  - **两个数字并列**：**84.2%（16/19）= 反事实**（对归档冻结输出的离线重算）；**89.5%（17/19）= 实测**（解析器修好后新采样）。**新数不等于旧数，差全部来自采样**；解析器那部分的效应被直接量到 —— 5 个假阴性里**有 4 个在新采样里真的通过了**。逐任务 21 个里 17 个一致，4 个分岔全部归因采样。
  - **本次沿用 `--no-guard-judge`（guard=关）**：为的是与 84.2% 对比时**只改解析器这一个变量**。所以这份新报告的 `judge_tampering`/`judge_restored` 也是 `null`（**没检查**），引用时不能说「查过且干净」。
  - **成本差 2.4 倍：已查明，是缓存冷热，不是 token 统计出错**。同一批任务、几乎相同的 token（341,337 vs 337,428，差 1.1%），归档那轮 0.0% 缓存命中 / ¥0.8423，这次 98.6% / ¥0.3453。排除过程：①**不是裁判阶段的 token 被算进去了** —— 裁判层一次 LLM 都不调（`eval/golden_tasks.py` 无任何 LLM 调用，判定是 `subprocess` 跑 pytest），且 `single-shot` 每任务只发一次请求（`runner.py:539-541`），两次 guard **都是关**，假设结构上不成立；②**不是统计口径变了** —— token 只差 1.1% 而成本差 144%，口径变了不可能只动成本。③剩下的解释**被直接量到**：两份报告里逐任务的 `single_shot_prompt`（就是真正发出去的那条 user message，`runner.py:540/554`）对同样 21 个任务**逐字节完全相同**，而 15:30 那次是这组字节**第一次**发出；20:49 重发其中 13 个 → **98.7%**，21:01 重发 21 个 → **98.6%**。同一段字节，第一次 0%、第二次 ~99%。④第一次必然是 0% 的原因：单条 prompt 是 44k~85k 字符的巨型 user message，而**任意两条任务之间只有 329~341 字符共有**（固定前置说明，之后在任务标题处分叉）⇒ 运行**内部**也无前缀可复用；本项目其他每份报告都在 **0.82~0.99**（agent/one-step 各步共享同一段 system prompt），只有这一次是 0.0%。**读法**：¥0.8423 是**冷缓存价**、¥0.3453 是**热缓存价**，**不能当同一条臂的两次成本比**。**边界如实留**：直接量到的是「同段字节第二次 ≈99% 命中」，「15:30 是冷缓存」是推断（无法回查 provider 侧缓存状态）；逐任务 hit/miss 未落盘，命中率**无法从归档重算**。
  - **本轮实际花费 ¥0.2374 + ¥0.3453 = ¥0.5827**（预授权 ¥0.85，未超）。

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
  - **单独用它时，它是对话分叉，不是工作区分叉**（必须主动说）：文件**不会**回滚到第 K 步的样子，分叉后的 agent 看到的是**当前**工作区。CLI 分叉后**把这句打印出来** —— 一句会让人误判成"文件也回去了"的提示，比没有提示更糟。
    - **M9-8 之后这句只在「不带 `--rewind`」时成立**（2026-09-12 补）：当时这条写的是「我们**没有**工作区快照机制（`grep rewind|snapshot` 在 `agent/ app/` 下零命中，2026-09-11 核实）」—— 那句在写下的当天是真的，M9-8 之后就成了假话。现在 `--fork --step K --rewind` 让文件**也**回到第 K 步，所以 CLI 的提示**按有没有 `--rewind` 分叉成两句**。**留这段不是考古**：一句无条件写死的提示，加了新出口之后它自己会开始骗人，这正是本项目记录在案的头号缺陷类（接线漂移）。
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
- [x] **M9-4 ⑩ MCP 远程 HTTP + resources/prompts**：面试稿 §10「下一步」第一条**已经承诺了** HTTP/SSE，做了就是兑现；且「协议层复用、只换传输」正是分层设计的证明

  **✅ 2026-09-11 完成。实现**：`agent/mcp.py` 重构成两层 —— `Transport`（抽象基类，`start/send/receive/close/hint` + `timeout`）下面挂 `StdioTransport`（原样搬过来）与新的 `HttpTransport`；`MCPClient` 只管"说什么"（握手、id 关联、超时整形、错误包装、会话自愈）。新增 `MCPTimeout` / `MCPSessionExpired` 两个异常、`_NoRedirect`、`_decode_body` / `_parse_sse`、`MCPResourceTool` / `MCPPromptTool` + `_render_resources` / `_render_prompts`、`build_transport`（**唯一一处判断用哪种传输**）。假 server 拆成 `fake_mcp_core.py`（协议面，两个壳共用）+ `fake_mcp_server.py`（stdio 壳）+ 新的 `fake_mcp_http_server.py`（真 HTTP 壳，`ThreadingHTTPServer`）。`app/cli.py` 只多了"相对 `--mcp` 路径按工作区解析"。
  - **分层不是洁癖，是"同一个错误不想修两遍"**：超时整形、id 关联、错误包装、会话重建这四件事在两种传输上必须完全一致，写两遍必然漂移，而**只测单一传输的套件看不见分歧**。所以有一条测试跑**同一条操作序列走两种传输**，逐项比对 `serverInfo` / `protocolVersion` / `capabilities` / 三个 list / 调用输出 / 资源正文，**连未知工具的报错文案都要逐字相同**。
  - **`timeout` 只存传输层一份**（`MCPClient.timeout` 是转发属性）。socket 连接超时与"等响应等到什么时候"必须是同一个值 —— 两处各存一份，表现是"有时报超时有时不报"。
  - **`MCPTimeout` 单独一个类型**是为了让报错带上**方法名**：传输层只知道"没人来"，知道"在等哪个方法"的只有协议层，合成一句放在 `_request` 里，两种传输的诊断才一致（否则 stdio 报得出方法、HTTP 报不出）。HTTP 侧还要带上**端点地址**（stdio 给的是 server stderr 末尾）。
  - **`_raise_for_http_error` 的判断顺序就是这一项的全部意义**：`404 + 带 session id` 必须排在"正文里有 JSON-RPC error"**之前**。真实 server 回 404 时正文里常常也放一条 error，反过来的话"会话过期"被报成一次普通调用失败 —— **文案看着完全合理**，但自愈那条路（重新 initialize + 重试）永远走不到，一次 server 重启就废掉整个任务。假 server 的 404 正文**故意**也塞了一条 JSON-RPC error 来钉这个顺序。
  - **重定向不跟随**（`_NoRedirect`）：`urllib` 默认把 301/302/303 上的 POST **改写成 GET** —— 一次 `tools/call` 变成一次静默的读请求，用户看到的是"工具调用失败"，真正的病因（`mcp.json` 里的 url 少个斜杠 / 还是 http）一个字都不会出现。现在报的是「重定向到 X；请把 mcp.json 里的 url 直接写成最终地址」。
  - **协议头不可被用户配置覆盖**：`Accept` 必须**同时**列出 `application/json` 与 `text/event-stream`（spec 的 MUST），`Content-Type`、`MCP-Protocol-Version` 同理。让它们"可配置"等于提供一个必然把自己配坏的口子；认证头之类照常补充。
  - **resources / prompts 的注册判据是两条**：server 声明了该能力 **且** 列表非空。只判能力的话，一个声明了 `resources` 却一处资源都没有的 server 会拿到一个**永远调不通**的工具（白占 schema token，模型每次调用都失败一次）。**真跑对上了**：DeepWiki 声明了两种能力、两个列表都是空的（原始响应就是 `{"resources": []}`），于是正确地没注册。
  - **描述里必须列出可用 uri 与提示词的参数名**：不列的话模型不知道有什么可读，只能瞎猜一个试试 —— 正是 M8 `update_plan` 那个 elicitation gap 的形状（机制全在，没有任何东西把模型引向它）。描述恒在上下文里，列出来是**零额外往返**。封顶 `MAX_LISTED_ITEMS = 20` 并如实说「另有 N 处未列出」。
  - **`--mcp` 的相对路径改成按工作区解析**（真实跑才暴露）：help 里给的例子就是 `.codeagent/mcp.json`，而它按进程 CWD 解析 —— 设了 `WORKSPACE_ROOT` 从别处跑，照着 help 抄下来只会得到「MCP 配置不存在」。**一条照着文档抄却走不通的指引，比没有指引更糟**（M7 的原话）。
  - **测试**：`tests/test_mcp.py` 42 → **47 例**（新增 SSE 解析边界、content 整形全类型、坏条目丢弃、`timeout`/`cwd` 落位、握手末尾的 `notifications/initialized`）+ `tests/test_cli.py` 1 例。全量 **515 全绿**。假 server 为此加了 `--require-initialized` 开关（spec 里那条通知是 MUST，而"什么通知都收"的 server 永远测不出漏发）。
  - **变异测试 41/41 被抓住**（`m9verify/mutate_m9_4.py`）。五类失效模式各覆盖：协议层漏进传输细节（id 关联 / EOF 哨兵 / 超时方法名 / 另存一份超时值 / 漏发 initialized）、HTTP 规范细节（session 头 / 版本头 / Accept / 重定向 / 记住 session / headers 覆盖 / 超时端点 / 202 空正文）、会话自愈（**判断顺序反了** / 不重试）、SSE 与 content 整形（不当 SSE 解析 / 空行不分发 / 末尾不 flush / 单块 content / image 与 resource 占位 / base64 填充 / 坏条目）、能力面接线与 elicit（能力面没接 / 不看能力声明 / 列表空也注册 / 不加前缀 / allow 不认注册名 / 单台挂了炸主流程 / 描述不列 uri 或参数名 / 封顶与"另有 N"）。
    - **一处已验证的等价变异体**（已从列表删掉，不留在里面骗计数）：`_parse_sse` 里 `if line.startswith(":")`（跳过 SSE 心跳注释）。它是**算术上不可观测**的，不是"暂时没测到"：注释行的判据是"以 `:` 开头"，而取字段用的是 `line.partition(":")` —— 以 `:` 开头的行 partition 出来的 field **恒为空串**，永远不等于 `"data"`（实测 `':data: {"x":1}'` → field=`''`）。保留那一行是把 spec 的规则显式写出来，但要如实说明**没有任何测试能抓住它**。
    - **一处"看起来该有、其实抓不住"的**：`if cap not in caps: continue`（能力没声明就跳过）。去掉它，后面 `except MCPError` 那条兜底也会把 `-32601` 吞掉、照样不注册 —— 从**注册结果**上看两种实现完全一样。所以它只能从**副作用**上钉：少了判断，一台不支持 resources 的 server 每次启动都会多打一行 `…/list 失败` 警告，而每台 server 都报一行噪声等于训练用户无视警告。对应测试因此断言 stderr 里**没有**那句话。
  - **真实远程 HTTP MCP 端到端跑通（DeepWiki，走公网真 server）**：工作区 `m9verify/ws_m94/`，`.codeagent/mcp.json` 里只写 `url`。
    - 握手：`protocolVersion 2025-06-18`、`serverInfo {name: DeepWiki, version: 2.14.3}`、capabilities 含 `tools/resources/prompts/experimental`。
    - **该 server 不回 `Mcp-Session-Id`（无状态模式）**，客户端照常工作 —— 说明 session 是可选增强而不是流程里的硬依赖。
    - 它声明了 `resources` / `prompts` 但**两个列表都是空的**（绕过客户端过滤直接看原始响应：`{"resources": []}` / `{"prompts": []}`），于是 `read_resource` / `get_prompt` 正确地**没有注册** —— 这条设计判据本来只能靠假 server 模拟，真 server 上对上了。
    - DeepSeek 驱动实跑：`read_wiki_structure(repoName=pallets/flask)` 真实成功（**1 步、2425ms、prompt 6190 + completion 112、缓存命中 45%**），返回的章节目录里含 `2.3 Blueprints`，模型的最终回答与之逐字一致。
    - 同轮探过但**用不了**的公网 server（如实记录，免得下次重复踩）：`remote.mcpservers.org`（DNS 解析不了）、`gitmcp.io` / `huggingface.co/mcp`（连接超时）、`docs.mcp.cloudflare.com`（连接超时）；`mcp.context7.com` 可用但同样只有 tools（2 个）、无 resources/prompts。**结论：能连上的公网 server 里没找到一个暴露非空 resources/prompts 的**，所以那两个工具面的真实对端仍是本地假 server —— 这一条如实标为**未在真实远程 server 上验证**。
  - **如实说明未做**：① 独立 GET SSE 流（server 主动发起请求那条长连接）与 `Last-Event-ID` 断点续传 —— tools/resources/prompts 三件事都走 POST 请求-响应，用不到；假 server 的 `GET` 一律 405，钉住客户端不会偷偷去开它。② `url` **不做 SSRF 校验**，与 `web_fetch` 刻意不同：那个 URL 是**模型**给的，这个是人写在 `mcp.json` 里的显式 opt-in，校验一个用户自己填的地址没有意义。

### 第三批 · 需要前置

- [x] **M9-5 常驻交互模式（REPL）** —— ④⑤ 的**硬前置**

  **✅ 2026-09-11 完成。实现**：新模块 `app/repl.py`（`Repl` 类 + `run_repl()` + 9 个斜杠命令 + `_activate`；**M9-6 加了 `/goal` 之后是 10 个**）；`app/cli.py` 把原先**构造了两遍**的 `QueryEngine(...)` 收成 `_Runtime` + `_build_runtime()`，加 `--repl` 开关；`agent/loop.py` 抽出 `new_state()`、新增 `run_turn()` 与 `_run_loop(budget_start=)`；`agent/permissions.py` 加 `new_turn()`；`agent/state.py` 加 `ensure_tool_pairing()`；`agent/llm.py` 加 `Usage.__sub__`。
  - **它不是新功能，是「换个用法」**：同一个 `AgentState` 连跑两次。这正好撞上本项目的头号缺陷类（机制在、测试绿、换个用法就静默失效）—— 下面四条**没有一条**是单发路径能观测到的，它们在改动前的代码上都表现为"完全正常"。
    1. **`terminated_reason` 从不复位**（只在 5 个返回点被写，也不参与循环条件）。单发时"一个回合"和"一个进程"是同一件事，所以从没有过清空动作；常驻进程里不清 = 第二回合全程带着上一回合的旧值，而 `app/ui_streamlit.py` 会从检查点 payload 里把它读出来显示。→ `run_turn` 每回合复位。
    2. **权限的「本回合」永不过期**：`PermissionsEngine._turn` 全仓零 clear/reset，`tests/test_permissions.py` 把"回合结束"定义成**新建实例**。常驻进程里 `allow_turn` 于是变成**永久放行** —— 与确认框上写着的「2) 本回合允许」直接矛盾。→ `new_turn()` 清 `_turn`、**保留 `_always`**（这是两个不同的承诺，清干净一点看起来更保险，但那等于把"一直允许"降级成"本回合允许"）。
    3. **`record_discovery` 每回合重放**：守卫是"block 不在 system prompt 里就跳过"，而第二回合 block **还在** prompt 里 → `skill_discovery` 与每条 `skill_shadowed` 事件每回合往轨迹里再写一份。→ 守卫改成认 `state.events` 里记过了没有。
    4. **中断会让消息配对残缺**：回合中途 Ctrl+C，`state.messages` 可能停在 `assistant(tool_calls=[3 个])` + 只有 1 条 tool 结果 → 下一轮请求直接 400，而**报错发生在下一回合**，与那次 Ctrl+C 看起来毫无关系。单发时进程就死了、靠检查点恢复所以从没暴露。→ `state.ensure_tool_pairing(messages) -> int`，`run_turn` 入口调用，**补了几条就记一条 `pairing_repaired` 事件**（不静默修：事后想不通"模型为什么又说要再调一次"）。
  - **`max_steps` 改成「每轮一份预算」**（用户拍板，**行为变更**）：`_run_loop` 进循环时记下 `budget_start = state.step`，条件从 `state.step < max_steps` 改成 `state.step - budget_start < max_steps`。`state.step` **本身照样累计不重置** —— 检查点文件名 `step-N.json`、`--fork --step K`、轨迹的 `step` 字段都依赖它单调递增，只改预算的**度量起点**。顺带修掉一个现有陷阱：会话跑满 25 步后 `--resume` 今天会**一次模型调用都不发**、直接又打印「已达到最大步数（请拆分子任务）」，而错误信息指的方向还是错的。
  - **装配不复制，是这一项防漂移的关键**：REPL 若自己装配一遍（注册工具、接权限、接 hooks、加载 MCP、装 `--review-edits` 的确认回调），迟早漏掉一样 —— CLI 历史上已经**漏接过 hooks 与 permissions 各一次，而两次都是静默的**。`_Runtime.engine(session)` 是唯一 `QueryEngine(...)` 构造点，单发与常驻都从它拿。
  - **两条硬不变量**（各有测试钉着）：① 不认识的斜杠命令**绝不发给模型** —— 打错一个字母的代价是一次真实的模型调用（钱 + 时间），而模型的回答看起来还挺像回事，于是这个错误不会被发现；② 会话切换失败**不能半切换**，`/resume 不存在的名字` 必须留在当前会话里 ——「session 换了、engine/state 没换」是一个**没有任何报错**的错配：轨迹写进 A、你在看 B，事后只能靠翻 `data/` 发现。`_activate` 三样一起换（engine 必须跟着换，`QueryEngine.session` 是构造期绑定的）。
  - **`await_user` 在 REPL 里不需要任何特殊机制**：模型提问 → 本轮结束 → 打印问题 → 下一行输入就是回答。这是 M8「把打断建模成数据标志而不是阻塞控制流」的回报，演示时可以顺带讲。反过来，若 REPL 自己 `input()` 一个"回答"，那才是把数据标志退化成阻塞控制流。
  - **符号命令表 `_COMMANDS` 是 `/help` 正文的唯一来源**：命令与帮助写在两个地方，迟早出现"帮助里有、实际没有"（或者反过来）。有一条测试断言 `/help` 列出的名字**恰好等于** `_COMMANDS` 的键集合。
  - **退出语必须给出可执行的续跑命令**（M7 教训：一条走不通的指引比没有更糟）。这条让 `_wrap_up` 多做一件事：退出时**无论走了几步都强制落一次检查点** —— 检查点是**按节拍**写的（默认 5 步）且**只在有工具调用的步上 tick**，所以纯聊天、或者只走两三步工具就退出，都写不出检查点，而那行承诺的命令跑起来会直接报"读不到检查点"。**这条是单测与真跑一起挖出来的**（见下）。
  - **每回合报的是增量，不是会话累计**：`RunResult.steps` / `usage` 五个返回点给的都是 `state.step` / `state.usage`。REPL 用回合前后的快照相减 —— 而不是让 loop 再维护第二套"本回合用量"的记账，那就是「两处各写一遍 → 漂移」。**`Usage` 是可变 dataclass，`state.usage += ...` 是原地累加**，所以快照必须 `dataclasses.replace()` 复制；不复制的话相减恒为 0 **且不报错**，只是"每次都说这回合没花钱"。
  - **测试**：`tests/test_repl.py` **34 例**（本仓第一条 `CliRunner(input=...)` stdin 端到端也在这里，7 例）+ `tests/test_loop.py` 8 例（多回合契约 + `ensure_tool_pairing` 纯函数）+ `tests/test_permissions.py` 2 例。全量 **559 全绿**（原 515；**这是 M9-5 当时的数字**，M9-8 之后为 707、M5-5 之后为 **769**）。
  - **变异测试 19/19 被抓住**（`m9verify/mutate_m9_5.py`）。八类失效模式各覆盖：每轮预算的起点、回合边界上的复位（`terminated_reason` / `new_turn` 的调用点 / `new_turn` 是否真清 `_turn` / 是否误清 `_always`）、入口重放的去重守卫、中断残局（不补配对 / 补了不记事件）、两条硬不变量（未知命令当任务发 / 半切换）、命令解析（大小写 / `raw` vs `strip` 后的行 / 空行）、增量记账（快照不复制 / `Usage.__sub__` 字段写错）、退出承诺（不强制落检查点 / `/new` 用秒级 id）。**三类需要说明**：
    - **一个证明过的等价变异体**（未设，不留在列表里骗计数）：`loop.py:214` 的 `_run_loop(..., budget_start=state.step)`。改成省略该实参**在任何输入下都不可观测** —— `_run_loop` 的兜底正是 `if budget_start is None: budget_start = state.step`，而两次读 `state.step` 之间没有任何东西改它（`ensure_tool_pairing` 只可能追加 tool 结果消息，`state.task = text` 与 `messages.append` 都不碰 step）。两处是**恒等**的，不是"碰巧一样"。保留显式传参是因为它把"本轮预算从这一步起"写在调用点上（`run_from` 那条路靠兜底，语义不同），不是为了测出什么。
    - **一个依赖时序、如实标注的**：`/new` 用 `unique_session_id` 而不是 `new_session_id`（后者粒度是**秒**）。变异回去之后，`/new` 与起始会话撞 id 需要**两次调用落在同一秒内**才算命中 —— 测试里是几毫秒的事，理论上跨秒就会 MISS。这条防的是**真实缺陷**（两个"不同"的会话静默共用同一个检查点目录，后者覆盖前者、全程不报错），它值得留着，只是不假装它是确定性的。
    - **一个刻意不设变异体**：`_dispatch` 里 `except typer.Exit: pass`（命令级失败不带走 REPL）。去掉它之后 `/fork 三步` 会带着 `typer.Exit` 冒到 `CliRunner`，但 `runner.invoke` 会接住并记进 `result.exception`、`exit_code` 照样是 0，**从输出上分辨不出来**。真要钉住得断言"REPL 之后还能继续跑"——`test_fork_with_a_non_numeric_step_does_not_kill_the_repl` 里那句 `repl.run_turn("继续")` 正是这个意思，它抓的是**行为**，不是这个 except 子句。
    - **另有一条变异体改写了测试**：空行变异（不再跳过空行）最初 **MISS**。查下来是：空行变成第 3 个回合、吃掉脚本响应，**最后一条真实输入**才 `RuntimeError` —— 但 loop 的 `except Exception` 把它变成 `terminated_reason="error"` 的 `RunResult`，**退出码仍是 0**，而原断言（"第二回合完成"在输出里）照样成立。**测试分不清"跳过"和"变成回合"**。修法：补 `assert "[error]" not in result.output` 与 `assert users == ["读一下 calc.py", "再改一下"]`（直接钉"模型收到的 user 消息序列"），并写明理由。
  - **真实 LLM 端到端跑了三轮**（DeepSeek 官方通路，工作区 `m9verify/ws_m95/`，素材是一份 `average_word_length` 对空文本 `ZeroDivisionError` 的 `textstat.py`，基线实测 `1 failed, 3 passed`）：
    - **A（多回合上下文接续 + 修 bug）**：①「跑一下测试，找出失败的那个并修好它，修完再跑一次确认」—— 模型自己 `bash` 跑 pytest → `read` → `edit` 加空列表守卫 → **再 `bash` 跑 pytest**，报 `4 passed`（**6 步 / 20,682 token / 缓存命中 80%**）；②「再补一个测试：空字符串也要返回 0.0」—— 模型**记得上一回合**，直接 `edit` 测试文件再 `bash` 验证，`5 passed`（**3 步 / 13,323 token / 命中 95%**）。第二回合的 prompt 没有重新读源码就写出了正确改动，这是上下文真的接上了的证据。
    - **B（`/rename` + 重进 + `/fork 2` + 新分支接着跑）**：`/rename 文本统计修复` → 退出 → `--resume --session-id <id>` 恢复（提示符显示 `文本统计修复`）→ `/fork 2` 分叉出 `文本统计修复 @2 分叉`，打印"搬了 2 个检查点"+ 对话分叉警告 + 可执行续跑命令 → 在新分支上「加一个 `median_word_length` 并补测试」→ `9 passed`（**6 步 / 26,832 token / 命中 94%**）。`/sessions` 显示两个会话、血统标着 `← 分叉自 s20260911-161113@2`。
    - **C（`--review-edits` 现场验收 `new_turn`）**：① 首回合改 `longest_word` 的 docstring → 确认框弹出（带 diff）→ 答「**2) 本回合允许**」→ 生效（**6 步 / 19,547 token / 命中 82%**）；② 第二回合**同一个 `edit` 工具、同一个文件** → **确认框重新弹出**，再答「2」→ 生效（**2 步 / 7,692 token / 命中 96%**）。权限的记忆键是 `_classify` 给的 `arguments["path"]`，两回合都是字面量 `textstat.py`，**键相同** —— 所以"重新问一次"只可能来自回合边界上的清空。**这正是 `_turn` 从不清空那个契约缺口在没有单测介入下的现场复现**（改动前，第二回合会被静默自动放行）。
    - **检查点计数对上了设计**：A 会话停在 step 9、盘上 8 个检查点（`step-1..5,7,8,9`）—— 前 5 个是工具步的节拍产物，第 8 个（`step-9.json`）是**退出时那次强制落盘**写出来的，而它在 REPL 的实时显示里**并不存在**（那一行报的是"检查点 7 个"）。这一条同时验证了 `_wrap_up` 的强制落盘确实在跑，也说明"实时计数"与"退出后盘上计数"本来就会差一个。C 会话同理（实时 6 个 → 盘上 7 个）。
    - **三个会话在盘上都可核对**：`data/checkpoints/s20260911-161113|161157|161252/`、`data/sessions/*.jsonl|.meta.json`；`.codeagent/rules/learned.md` 三轮累计提炼出 10 条仓库约定 —— 其中一条是"查找函数定义优先用 `grep` 的 `include` 参数限定文件名，比 `path` 更可靠"，来自 C 首回合模型自己那次**失败的** `grep(pattern=def longest_word, path=textstat.py)`（轨迹里是 ✗）。这是真实轨迹的产物。
  - **如实说明未做**：① **不是全屏 TUI**（`TASKS.md` 已有决定，⑥ 明确不做）—— 行式输入，没有 ANSI 控制、没有历史滚动；多行输入缓冲 / 自动补全也都不做，那是纯终端体验的体力活，对这份作品集要回答的问题（循环、上下文、权限、可恢复性）不加分。**⚠ 其中「历史文件 `readline`」这一项已于 2026-09-11 从"不做"里摘出来单独放行**（见 ⑥ 的第二条）：它只有几十行、不引入新状态，且吃掉观感差距的大半 —— 这里原文把它和"多行缓冲 / 自动补全"一起划掉是**过度收缩**，M9-5 当时确实没做，但它不属于"明确不做"。**尚未实现。**② **`/fork` 是对话分叉，工作区文件不回滚**（沿用 M9-3 的语义）—— 这一点在 **B 的真实跑里直接看到了后果**：分叉点是 step 2（修 bug 之前），但盘上文件已经是修好的，于是新分支的模型 `read` 到的是"已经修好"的文件、却又在结论里说了一遍"修好了失败的测试"。REPL 与 CLI 都会把这句话打印出来，但**它不会阻止人误判**。③ 并发多回合（同一个 state 被两个回合同时推进）没有做，也没有测试 —— 当前设计里一个 `Repl` 一次只有一个活跃回合。
  - **顺带收益已兑现**：面试稿 §12 的演示动线不再全是单发命令（`/sessions` → `/fork` → `/rename` → 接着聊 是一条能一口气演完的连续动线）。
- [x] **M9-6 ④ Goal**：进程内 Goal + 暂停/恢复 + **显式完成检查**（后者的判分思路与我们的评估层呼应）

  **✅ 2026-09-11 完成。实现**：新模块 `agent/goal.py`（状态机 + `Goal` + `goal_can_advance` + `render_goal` / `render_check_report` + 两条给模型的消息模板 + 三态判定常量）、`agent/tools/goal.py`（**唯一**目标工具 `declare_goal_done` + `build_goal_tools`）、`agent/state.py` 加 `goal` 字段、`agent/session.py` 的 `_FIELD_DECODERS` 加一行、`agent/loop.py` 的 `_run_loop` 插 4 行 + `_verify_goal` / `_goal_turn_result` + 两个新终止原因与集中集合 `TERMINATED_REASONS`、`app/repl.py` 的 `/goal` 命令族（`status`/`pause`/`resume`/`clear`）+ `_run_goal_burst` + `_pause_goal` + 提示符状态位、`app/cli.py` 的 `--goal` / `--goal-turns` / `_reinject_goal` / 目标工具注册。
  - **它不是任务树，而面试稿里那句"任务树"没有依据。** 面试稿原有一句「④ Goal（目标任务树）——把小任务规划从平铺清单升级成树」，而全仓关于 TS 原版 ④ 的**唯一**证据是 `docs/reference/minicode-notes.md:141` 那一行，原文是「进程内 Goal（跨回合推进 + 暂停/恢复/完成检查）」（原版是 `/goal` `/goal status` `/goal pause` `/goal resume` `/goal clear` 命令族 + `goal/context.ts`）；`isComplete` / `stop_condition` / 任务树 / `TodoWrite` **全仓零命中**。原版是一个**带状态机的目标对象**，不是树。这一项因此**同时更正了那句无据的话**（「不允许私自造假数据」的直接要求，不是顺手改文案），并把「允许中途改结构」那半个承诺如实归位 —— 现有 `update_plan`（全量覆盖）本来就能做，不是 ④ 的缺失。
  - **三件事决定了它的形状**：① **判分权在人手里** —— 目标由人 `/goal <目标> --check <命令>` 创建，`check_command` 是**人预先给的可执行判据**；模型只能**声明**（`declare_goal_done`），运行时随即去跑那条命令，**退出码说了算**。模型既不能建目标、也不能改判据、也不能清标记（同 `clear_taint` 的纪律：标记不由被标记者清除）。② **完成检查是三态，不是两态** —— `passed` / `failed` / `invalid`，对齐 `eval/golden_tasks.py` 的 `JudgeResult.executed` 那条教训：命令**压根没跑成**时，「算完成」和「算没完成」都是错的（门禁拦下 / 超时 / 工具异常 → `invalid` → "本次判定**不计入**"）。③ **暂停只有在"有东西在自动跑"时才不是装饰** —— 我们的架构里没有定时器，所以 `paused` 的真实后果是 **REPL 不再自动续跑这个目标**。
  - **「跨回合推进」= REPL 在目标未完成时自动续跑下一个回合**（目标驱动，不是定时器 → 与 ⑤ Loop 明确区分）。`loop()` 在进提示符前先跑一拍；`_run_goal_burst` 最多 `goal_turns` 个回合（首回合发 kickoff、之后发 continuation），**结尾一定 `_pause_goal(stop)`** —— 不留着 active 回提示符，否则循环顶部会立刻再起一拍（按一次回车、敲一条 `/help` 都会重新触发），那就等于无界。**上限不是优化，是防锁死**：`input()` 是阻塞的、没有定时器，一拍期间人**根本敲不进字**，无界推进 = 把人锁在门外直到烧完额度。所以 `/goal` 的输出里**把授权额度打给人看**（「最多 3 回合 × 每回合 25 步 = 75 步」）。
  - **「人敲一行字」= 隐式暂停，这不是偏好而是结构性事实**：人只能在一拍停下时才拿到提示符；那一行若不暂停，回合结束后循环立刻又起一拍，**人再也敲不进第二行**。选暂停还因为它是可逆的那一侧（`/goal resume` 一句话恢复），且它**不静默**（`_pause_goal` 把原因与恢复方式打出来）。暂停本身**是数据不是控制流**（同 `await_user` 的纪律）：只改 `status` + 记一条事件，既不 `return` 也不 `raise`。
  - **判定插在 `_run_loop` 里（批后、checkpoint 前）**：批后是因为「先跑测试验证、再声明完成」是模型同一步里最常见的形状，放批前只会看到上一轮的陈旧声明；checkpoint 前是为了让这一步的检查点带上**判定之后**的目标状态。插在 `_run_loop` 里的直接收益是**四条入口一起拿到**（同 `permissions.new_turn()` 的位置理由）—— 于是 **`--resume` 一个有活跃目标的会话时，人给的检查照跑**，否则目标在恢复路径上就是个假死状态。headless 没注册工具、也没有目标，完全 no-op。
  - **批前复位是结构性保证而不是纪律**：`if state.goal is not None: state.goal.declaration = None`（`_run_loop` 里）→「一次声明**恰好**触发一次检查」由结构保证。不复位的话第 N 步声明过一次之后**后面每一步**都会重跑一次完成检查 —— 烧钱、上下文爆，而且**不报任何错**。
  - **检查走 `_gate_and_run` 而不是直接 `tool.run`**：检查命令是**人写的一行 shell**，必须和模型自己发的命令受同一套治理（PreToolUse 的 block-at-submit、权限 deny、危险命令 ask、污染天花板）。顺带换来一个明确判据 ——「检查被拦下了」有 `gate_block` 事件带 `source`/`reason`，而不是靠猜错误文本。代价见 S19。
  - **绝不伪造 `assistant(tool_calls=[...]) + tool(...)` 消息对**把检查伪装成模型发起的一次工具调用 —— 那是在会话里写下一个模型从没发出过的调用，与 `PAIRING_FILLER`「必须说实话」的纪律直接冲突。判定以 **user 消息**回喂，同 `_reinject_plan` 的先例；合成的 `call.id` 只活在这一次调用里、不进任何消息。
  - **`declare_goal_done` 刻意不叫 `complete_goal`**：工具 description 是这条能力在**唯一常驻请求**（工具 schema）里的全部说明，而 `complete_goal` 读起来像「调用它 = 完成」—— 那正是本项要避免的误解。参数扁平（`summary` + 内联 `evidence: list[str]`），**不能用嵌套模型**（`base.py` 的 `raw.pop("$defs")` 会把 `$defs` **静默**削掉，模型收到的参数说明就是错的）—— 真跑里这条当场兑现：模型第一次把 `evidence` 传成字符串，被 schema 校验拒掉（`ToolResult.fail`），下一次改用列表才成功。
  - **声明标志走 `state` 而不是 `ToolResult` 上的第二个标志**：`_execute_tool_calls` 在批结束时已经把 `ToolResult` 都 `compact_batch` 成字符串了（只返回 `pending: str | None`），要带出第二个标志就得**改它的返回类型** —— 那是四个入口共用的核心循环契约，`TASKS.md` 把「动核心循环契约」留给了 M9-7。经由 `state` 传递，`_execute_tool_calls` **一个字符都没改**。
  - **目标一个字都不进 system prompt**（同 `plan` 的纪律，四条理由也一样：`system` 是 `messages[0]`、在 `_PREFIX_LEN` 保护区内，变一次 = 整条前缀缓存永久失效；目标是会话中途创建的，注入就得回改第 0 条；`state.system_prompt` 渲染一次就进检查点）。可见性靠**创建那一刻的 kickoff 消息** + `--resume` 时补投一次（`_reinject_goal`，与 `_reinject_plan` 同处同形）。**这是 M8 那个 elicitation gap 的正解形状**：引导必须出现在目标被创建的那一刻 —— 那里恰好有一个天然的用户回合。**刻意不给 `DEFAULT_SYSTEM_PROMPT` 加 `{goal_hint}` 槽位**：加了就得按"注册了哪些工具"填（同 `_ASK_USER_HINT`），而目标工具在 CLI 两个入口都注册 → **每个单发会话**都要为"一个不存在的目标"付 token，且模型会去调一个注定失败的声明工具。
  - **`--goal`（只读打印）与 `--plan` 完全同构**，理由也一样：没有这条出口，REPL 里设的目标在进程外**没有任何消费者**（本项目明确防这个 —— `--rename`/`--fork` 当初就是配着 `--sessions` 一起做的）。目标就是检查点里的一个字段，读它够了，所以不建会话、不要 key、不跑任务，排在 `_build_llm` 之前（测试用"一调用就炸"的 `_build_llm` 替身钉住）。它还把**判据原文与最近一次判定的输出印在一起**（S17 唯一的缓解手段）。**不做** `--goal/--check` 单发目标生命周期 —— 目标是"进程内跨回合"的能力，单发里连"下一个回合"都不存在。
  - **`_FIELD_DECODERS` 那一行是载荷性细节**：`load_state` 走 `AgentState(**raw)`，而 dataclass **不做类型检查** —— 漏了 `"goal"` 这一行，`state.goal` 会是个 `dict`，直到有人读 `.status` 才抛错，而那个炸点被 `_run_loop` 的 `except Exception` 吞成 `terminated_reason="error"`，**看起来像引擎出错**。一条测试 + 一条变异体专门钉它，并把这条纪律写进 `session.py` 的注释。
  - **`/goal` 的解析规则只有两条且必须确定**：带 `--check` 一定是设定；不带 `--check` 且首词是已知子命令（`status`/`pause`/`resume`/`clear`）→ 子命令；两者都不是 → 用法错（**只在命令内失败，不带走 REPL**，有测试钉着）。**已知边界如实记**：目标文本里出现字面 ` --check ` 会被切开 —— **不做引号解析**，加一层"半个 shell"只会造出第二个有歧义的解析器。`/goal X` 在已有未结束目标时**拒绝**（防旧目标的检查命令无声消失，而它是"完成与否"的唯一判据）；`/goal clear` 置 `None` 而**不是**置 `done`（后者会在轨迹里留下一句没发生过的成功）；`/goal resume` 在 `done` 时**拒绝**（完成是终态，能反复"完成"一次的目标等于没有检查）。
  - **`_STOP_REASONS` 每个原因都要有话说**：暂停是自动推进的唯一出口，理由说不清的话人只会看到「它自己停了」。而 `BURST_STOP_REASONS` 里 **`"completed"` 刻意不在**：一个回合"正常跑完"（模型给了文字、不再调工具）恰恰是自动推进要继续的情形 —— 目标的完成与否由人给的检查命令说了算，不由模型停不停下来说了算。
  - **测试**：`tests/test_goal.py` **38 例**（创建/暂停恢复/清除、三态判定、门禁拦下→invalid、批前复位、截断 40 行、schema 扁平无 `$defs`、REPL 一拍多回合与人工暂停、`dump_state`→`load_state` 回来是 `Goal` 实例、暂停跨进程往返仍门住一拍、`--goal` 不碰 LLM、`/help` 键集合等于 `_COMMANDS`）+ `tests/test_repl.py` / `tests/test_session.py` 补契约。全量 **597 全绿**（原 559；**这是 M9-6 当时的数字**，M9-7 之后为 623、M9-8 之后为 707、M5-5 之后为 **769**）。
  - **变异测试 20/20 被抓住**（`m9verify/mutate_m9_6.py`）：声明直接当完成 / 检查不走门禁链 / 无效→失败 / 无效→通过 / 失败也结束回合 / 通过不结束回合 / 判定不回喂 / 批前不复位 / 跑目标文本而非命令 / 无超时上限 / 漏解码器 / 输出不截断 / 暂停照样推进 / `clear` 实现成 done / 声明能自造目标 / 人工回合不暂停 / 到期不停止 / 恢复时每回合重投。
    - **一个如实标注的 MISS（不是缺陷，是不变式的推论）**：「人工回合不暂停」这个变异体实证 **MISS**，而原因是**结构性的** —— `loop()` 在进提示符前一定先跑一拍，而 `_run_goal_burst` 结尾一定 `_pause_goal(stop)`，所以**人拿到提示符时目标绝不可能是 active**，那句 `_pause_goal` 命中的永远是"已经暂停/已完成"的拒绝分支。这是「一拍 = 一次授权」不变式的直接推论，与动线 ④ 在纯 stdin 流程里不可观测**同源**（见下）。
  - **真实 LLM 端到端五条动线全跑**（DeepSeek 官方通路，真工作区 `m9verify/ws_m96_a|b|c|d|e/`，真 pytest 真跑；日志 `goal_a|b|b2|c|d|e.log` + 驱动脚本 `drive_m9_6.py` 的三个 case）：
    - **① 目标 → 自动推进 → 声明 → 检查真跑通过 → `goal_done`**（`goal_a.log`）：模型 dir/glob → 读源码与测试 → `edit` 把 `len(text.split(" "))` 改成 `split()` → **真跑 pytest（3757ms）** → `declare_goal_done`（第一次因 `evidence` 传成字符串被 schema 拒 → 改列表成功）→ **`✓ 完成检查 passed`** → `★ 目标完成`（**6 步 / 21,561 token / 缓存命中 81% / 检查点 2 个**）。之后 `/goal resume` 被拒「目标已完成（检查已通过），不能恢复」、`/goal status` 显示 `状态: done` / `最近判定: 通过 @step 6`。
    - **② 过早声明 → 判定未通过 → 回喂 → 继续修 → 再声明 → 通过**。先拿**失败半边**（`goal_b2.log`）：目标是**窄范围**（只修 `word_count`）、判据是**全目录**测试套件 —— 这是真实世界里常见的不对称。模型把 `word_count` 修完就声明 → **`[5] ✗ 完成检查 failed: python -m pytest tests/ -q [4338ms] [exit code: 1]`** → 回喂 → 模型**没有硬撑**，而是调 `ask_user` **指出这个冲突** → `[await_user]`（**6 步 / 24,241 token / 命中 91%**）。**失败后目标保持 `active`（不是 done）**，事件 `['goal_created','goal_check']`。随后 `--resume --session-id` 恢复同一会话（`goal_e.log`）——**这就是动线 ⑤**：打印「目标恢复: …」（`_reinject_goal` 生效）+「检查点节拍: 每 5 步（沿用该会话当初的设置）」→ 模型接着修 median → 真 pytest → 声明 → **`✓ 完成检查 passed [3559ms]`** → `goal_done`（step 6→9 / 16,313 token / 命中 94%）。
    - **③ `/goal pause <原因>` → `/goal status` → `/goal resume` → 自动推进 → 完成**（`goal_c.log`）：pause 存下原因 → resume **把 `pause_reason` 清掉**（不清的话 `/goal status` 会永远显示一条已不成立的过期原因）→ burst 里真 LLM 5 步修好 → 声明 → 检查 passed → `goal_done`。事件序列 `['goal_created','goal_paused','goal_resumed','goal_check','goal_completed']`（**5 步 / 17,416 token / 命中 78%**）。
    - **④ 人敲一行字 = 隐式暂停**：**这条动线在纯 stdin 流程里结构上不可观测**，如实记 —— 原因同上（`_run_goal_burst` 结尾必暂停 → 提示符处目标绝不为 active），`/goal pause` 与 else 分支那句 `_pause_goal` 命中的永远是拒绝分支。驱动脚本 `m9verify/drive_m9_6.py` 的 `case=human` 走 `loop()` 的 else 分支那**两行原话**（`repl._pause_goal("人工回合（你敲了一行字，目标转为暂停）")`），拿到的是「**对 active 目标生效** + 第二次调用是 no-op、**不覆盖原因**」（`goal_d_drive.log`）。**live 侧**（`goal_d.log`）如实记为「人敲一行字后目标**不会自动恢复推进**」：那次 burst 第一拍就把目标做完了（`--goal-turns 1`，6 步 / 25,637 token / 命中 80%），人那行到达时目标已是 `done`、else 分支 no-op，模型正常回答并逐行解释了 diff。**不把它讲成"那一行暂停了目标"。**
    - **⑤ `--resume` 一个有活跃目标的会话 → 检查照跑**：见 ② 的后半（`goal_e.log`）。这是「判定长在 `_run_loop` 里」的现场验收。
    - **顺带观测到的真实行为（值得记）**：① 目标 burst 里 `ask_user` 会让 `_run_goal_burst` 停下，并把目标暂停成「模型提问，等你回答」；② 有一次模型**拒绝**了人给的「跳过 CHANGELOG.md 直接声明完成」，并引用判据说明理由（"如果 CHANGELOG.md 不存在，这条命令会以非 0 退出，声明会被当场拆穿"）；③ `max_steps` 撞顶同样是 burst 的停止理由（「目标已暂停：本回合达到步数上限」）。
  - **一处我自己的素材错误（如实记，形态比错误本身更值得记）**：这次真跑用的工作区里有我手写的一份 `m9verify/ws_m96_b/tests/test_extra.py`（**是验证素材，不在仓库的 `tests/` 里** —— 写在这里免得被读成"仓库的测试套件里有条假断言"），里面写了 `assert median_word_length("a ccc") == 1.5`，而 `"a ccc"` 的词长是 `[1, 3]`、中位数是 **2.0** —— **那条断言不可能通过**。DeepSeek 算对了 2.0、看不懂测试为什么写 1.5，于是一整个回合反复探测 `python -c "print((1+3)/2)"`，烧掉 **151,038 token** 撞 `max_steps`；两次 `/goal resume` 又各烧 356,906 / 752,135 token，最终一次都没声明。**模型始终没有谎报完成**（这是设计上的正向信号）。修法：改成 `assert median_word_length("a cc") == 1.5`（词长 `[1,2]` → 1.5）并注明算法。**教训：真跑暴露的是我的素材错误，不是特性缺陷。**
  - **如实标注（README S17–S20）**：**S17 空转的检查命令** —— 判据是人给的，运行时只看退出码、**不评价它检查了什么**（`--check "true"` 永远通过），缓解只有**可见性**（判定文案永远把命令原文印在结果旁边），**没有任何机制阻止**；**S18 声明不是闸门，只是事后核验** —— 模型可以完全不声明就把活干完（目标停在 active、多花回合），也可以在毫无证据时声明（靠检查兜底）；防的是"谎报完成"，不防"判断失误"；**S19 检查的副作用能替 agent 打开一道治理闸门** —— 检查走 `_gate_and_run` → PostToolUse hooks 生效 → 一条成功的 `pytest` 会写 `data/tests_pass.marker`，而它正是解锁 `git commit` 的那道门（轨迹上与模型自己跑同一条命令**不可区分** —— 复用同一条链的代价，不是疏漏）；**S20「判定无效」比 eval 弱一层** —— eval 有 pytest 的退出码 2/3/4/5 认得出"压根没跑成"，**shell 没有这个信号**，`exit 127`、`No module named pytest` 都会被记成**未通过**，只有门禁拦下 / 超时 / 工具异常才算无效。另记：`/fork` 会连目标一起继承而检查跑在**当前**工作区上（对话分叉、文件不回滚 → 可能白捡一个通过）；通过即结束回合，模型没有机会补充「还有一件次要的事没做」（这是把判分权拿走的**代价**）；检查超时 120s 是常量、没有旋钮；`state.task` 会被续跑文本覆写（目标本身才是权威）。
  - **数字口径**：上面五条的数字与既有单发/REPL 数字**不可比** —— 自动续跑会把 `max_steps` 乘上 `goal_turns`。

### 压轴 · 价值最高，但唯一会动核心循环契约

- [x] **M9-7 ② sub-agent 并发 3 + wait/close**（2026-09-11 完成并真跑验证；用户拍板：**保留 `subagent` 作为阻塞便捷入口 + 加 4 个句柄工具**、**子代理用量计入父会话**）
  - **开工前的现状勘察（下面这一段是 M9-7 开工**之前**的样子，留档用）**：`agent/tools/subagent.py:56` 已经有 `subagent` 工具，但它是**同步阻塞单发**（`engine.run(...)` 跑完才返回），`MAX_SUBAGENT_STEPS=10`，只读 registry，且 `_restricted_registry` **永不包含 subagent 自身** → 结构上禁递归。这个形状与 Python 版的 `task` 工具相同，要变成句柄式就得动工具契约
  - **形状（2026-09-11 读了 TS 原版真代码后更新，`src/agents/manager.ts` + `src/tools/sub-agents.ts`）**：
    - `MAX_SUB_AGENTS = 3`（`agents/types.ts:1`）；`spawn` 时 `runningCount >= max` **直接抛**，不是排队
    - `spawn(task, parentSignal?)` **立刻返回句柄**（`id/status/startedAt`），后台跑 `runAgentTurn`，独立 `messages`（system 由 `buildSubAgentPrompt(cwd)` 生成）+ 自己的 `AbortController`
    - 父 signal abort → 级联 cancel：置 `status='closed'` + `abort`
    - `wait(ids, timeoutMs=30_000)` 用 `Promise.race` → `{timedOut, agents}`；**超时只返回最新状态、不关闭** ——「等」和「关」是**分开的两件事**，这个区分要抄
    - `close(id)` = abort + **`await completion`**（等它真的停下才返回）；另有 `closeAll()`
    - 四个工具：`spawn_agent` / `list_agents` / `wait_agent` / `close_agent`；**只读靠传进去的 registry 保证**，不靠权限提示
  - 我这边的真实成本（比"改个返回类型"小，但不是零）：
    - 我的引擎是**同步**的、没有 async → 后台要用 `ThreadPoolExecutor` + 句柄注册表；`spawn_agent` 仍然可以**同步返回一个 `ToolResult`**（句柄序列化进 output），所以 `_execute_tool_calls` 的返回类型**未必需要改**。真正新增的是**取消**这条通路 —— 现在 `_run_loop` 没有 abort 信号，而 `close_agent` 必须能真的停下一个回合
    - **必须先验证 LLM 客户端的多线程安全性**：原版是 promise、天然并发，我的 `BaseLLM` 共享实例跑在两条线程上是**新假设**。按 M7 的教训 —— **先怀疑自己的调用方式，再归因给库**
    - **回合边界上的 worker 结算**（原版叫 `settleWorkers`）：不做的话 worker 会**活过它的回合**、结论无处可去 —— 那正是本项目的头号缺陷类（静默丢失 / wiring drift）
    - REPL 目前假设「一次只有一个活跃回合」（M9-5 的如实标注 ③）
  - **一条可以直接讲的对照**：Python 版**声明**了 `SUBAGENT_START` / `SUBAGENT_STOP` 两个 hook 事件，但全仓**没有任何触发点**（只有声明和测试）—— 那正是我们花了四个里程碑在防的「机制在、没人接线」；它的 sub-agent 也是同步单发，没有并发
  - **这是简历上最值钱的一条**（多智能体编排是 Agent 岗最热的考点），但它不该是起步项
  - **实现**：新叶子模块 `agent/subagents.py`（540 行，**不 import `agent.loop`** —— `loop.py` 要反过来用它做结算，反向 import 即成环，所以跑什么由工具模块构造的闭包决定）；`agent/tools/subagent.py` 加 `build_subagent_tools` 单一构造点，5 个工具共用一个 `_build_runner`；`agent/loop.py` 加 `abort` 两处检查点 + `finally` 结算；`app/repl.py` 的 `BURST_STOP_REASONS` / `_STOP_REASONS` 加 `"aborted"`。
  - **单测**：`tests/test_subagent.py` **32 例**（原 6 例）。全量 `623 passed in 90.86s`（`python -m pytest tests/ -o addopts="" -q`，无 skip）。
  - **变异测试 29/29**（`m9verify/mutate_m9_7.py`，日志 `m9verify/mutate_m9_7.log`）。**两条是牙齿检查逼出来的修正，如实记**：
    1. **「超时顺手关掉」最初 MISS**，而**我的第一次修法也是错的**。第一层原因：该用例的 runner（`_blocking_runner`）docstring 自己写着"交结论时**不看 token**"（模拟卡在网络调用里），token 置位对它是个 no-op。第二层原因（第一次修完仍 MISS 才发现）：那条变异锚在 `remaining <= 0` 分支上，而它**在逐个 handle 的循环里** —— **只有一个 handle 时循环只走一圈，超时全被 `finished.wait(remaining)` 吃掉，那条分支根本到不了**。修法：改成**三个** token-aware worker（前一个慢的耗光预算，后面的才会走到那条分支）。不是断言太松，是**场景压根没覆盖到**。
    2. **「用量在 worker 线程合并」原本是 SKIP（锚点没找到）** —— 锚点写成了 16 空格缩进，而真代码在 `with self._lock:` 体内是 12 空格。**它从来没真正跑过**，却会被读成"这条覆盖了"。修好锚点后 OK。**教训：SKIP 和 MISS 一样要当失败看**，否则一个坏锚点会伪装成覆盖。
    3. 顺带把**「等而不等」的抓法从"靠竞态"改成"确定性"**：原用例先 `gate.open()` 再 `wait()`，worker 有起跑优势，重跑可能翻盘。改成 `threading.Timer(0.2, gate.open)` —— 若 `wait()` 不等就返回，拿到的是 `running`，裕度远大于抖动。
    - 脚本另加了子串过滤器（`python m9verify/mutate_m9_7.py <子串>`）用于改完测试后单独复验；**留档的 29/29 是不带参数跑出来的**。
  - **真实 LLM 端到端四条动线**（DeepSeek 官方 `deepseek-chat`，无 mock；工作区 `m9verify/ws_m97/`，素材是三者互不相干的 `pkg/{alpha,beta,gamma}.py`；日志 `m9verify/drive_m9_7_{concurrent,serial,close,abandon}.log`）：

    | 动线 | 墙钟 | 步数 | 子代理工具调用 | prompt（命中/未命中） | completion | 命中率 |
    |---|---|---|---|---|---|---|
    | 并发 `spawn×3 + wait` | **17.4s** | 3 | 4 | 27936（12416/15520） | 5500 | 44% |
    | 串行 `subagent×3` | **29.2s** | 4 | 3 | 34382（22144/12238） | 5547 | 64% |
    | `close`：派长任务→立刻叫停 | 5.5s | 3 | 2 | 12890（8832/4058） | 479 | 69% |
    | `abandon`：spawn 后不等就回答 | 4.8s | 2 | 1 | 9790（8832/958） | 449 | 90% |

    - **加速比 29.2 / 17.4 = 1.68x**（同一台机、两条动线背靠背跑，机器无其他负载）。
    - **`spawn_agent` 真的立刻返回，这次有真数字**：四条动线里 `spawn_agent` 各耗时 **0ms / 5ms / 0ms / 0ms**，而串行动线里 `subagent` 是 **5540ms / 7686ms / 7426ms**。这是「句柄」这条契约在真实会话里的现场，不是单测断言。
    - **`close_agent` 真停**：`spawn_agent`(0ms) → `close_agent`(**1140ms**) 返回，状态 `closed`、**没有结论**；模型如实回报"它在跑到一半时被取消，报告没有生成"。1140ms 是"等它真停"的实测代价（远小于最坏 ~120s —— 界线是它当时卡在哪一次操作里）。
    - **结算事件真的到达人眼前**：`abandon` 动线里 `EventPrinter` 打出 `■ 子代理结算：1 个被叫停、0 个结论没被取走` + `sa-1「…」跑到一半被杀，没有结论`。**这是「结论被丢」这件事在真实会话里到达人眼前的证据**（即替代 `/agents` 的那个可见性出口）。
    - **模型自己读懂了「只活一个回合」**：`abandon` 里模型在没被提示的情况下主动补了一句「子代理只活这一个回合，如果现在直接结束对话，它的结论会丢失 —— 需要的话我可以用 `wait_agent(ids=["sa-1"])` 把结果取回来」。工具 description 里那两条错题写对了。
    - 两个动线的调查结论都**实质正确**（alpha 的"先打折后计税"、beta 的 `reserve_all` 为什么要回滚、gamma 的状态机与"delivered 不能退款"），且**并发与串行两条动线各自独立地**都指出了 beta 里 `except OutOfStock` 不捕获 `ValueError` 的那个回滚缺口 —— 子代理不是在敷衍。
  - **如实标注（README S21–S26 已写）**：**worker 只活一个回合**；`close_agent` 最坏卡 ~120s；`settle()` 的 join **有界 1.5s**（超时未停的如实报"跑到一半被杀"，`abandon` 动线看到的就是这一支）；父→子**没有独立级联 abort 通路**（对 TS 契约的偏离，靠 `settle()` 在回合边界实现）；worker 完整轨迹**不进**父会话 JSONL，只留一条摘要事件；**用量计入父会话 → 缓存命中率被稀释**（并发 44% vs 串行 64% 是同向的现象，但**两条动线是不同会话，不能当受控对照读**）。
  - **一处如实记的计划偏离**：**`/agents` 斜杠命令与 `_prompt` 的 `agents:N` 状态位没有做**，理由是**结构性的**、已在代码里核实：`workers = AgentWorkers(...)` 在 `loop.py:357`（`_run_loop` 内，每回合新建），`self._settle_workers(workers, state)` 在 `loop.py:486` 的无条件 `finally` 里（覆盖 6 个返回点 + `KeyboardInterrupt`）→ **回合之间活跃 worker 恒为 0**。于是 `/agents` 永远打印"（没有子代理）"、`agents:N` 永远是 `agents:0` —— 两个都是本项目头号缺陷类的形状（机制在、没人 routing），而恒显一个值的状态位会让人不再看它。可见性改由 `subagent_settled` → `EventPrinter`（`app/cli.py:82-86`）承担，**这一条已由 `abandon` 动线真跑验收**。对照值得讲：`--plan`/`--goal` 能落盘导出是因为它们**跨回合存活**，worker 是进程内瞬时的、盘上什么都没有。

- [x] **M9-8 工作区 rewind 快照**（2026-09-11 立条目，**2026-09-12 完成并真跑验证**）
  - **它把一条已写进五处文档的"如实标注"变成"已修"**：本文件的 M9-3 条目、`docs/architecture.md` §3.6「分叉与命名」、`docs/interview_guide.md` §4 与 §12 的 4f 条、`docs/TECH_SPEC.md` §9.3b、`CLAUDE.md` 的 M9-3 段 都写着**「`/fork` 只分叉对话，工作区文件不回滚」**（原稿引的是行号，但行号会随编辑漂移 —— 起草后一天内就有两处失效，所以改成按章节定位）。M9-5 的 B 动线真跑**直接看到了后果**（本文件 M9-5 条目「如实说明未做」第 ② 点原话）：分叉点是 step 2（修 bug 之前），但盘上文件已经是修好的 —— 于是新分支的模型 `read` 到的是"已经修好"的文件、**却又在结论里说了一遍"修好了失败的测试"**。这不是"缺个功能"，是**一个已知会咬人的边界一直挂在文档里**，而且它咬的方式是让模型说出一句它没有做过的事。
  - **挂在我们已有的 step 级检查点上**（`--rewind --step K`），**不是**"回退最近 N 次编辑"：Python 移植版是后者，我们是前者 —— step 与检查点、`/fork --step K` 共用**同一个坐标系**，而"最近 N 次编辑"要另立一套账（还得处理"一次编辑被后续编辑部分覆盖"）。
  - **要老实回答的难点（开工前必须先想清楚，不然会做成一个假的 rewind）**：① 只读工具与 `bash` 的副作用**不可回滚**（`pytest` 写的 `.pyc`、`git` 改的状态、任意命令的任意后果）—— 快照只能覆盖**我们自己经 `write`/`edit` 写的文件**，这一点必须写在命令输出里，不能让人以为"回滚了 = 什么都没发生"；② 快照存哪（`Session` 目录旁边 vs 工作区内的 `.codeagent/`）—— 存工作区内会被自己快照进自己；③ 与 `subagent` 的关系：**worker 的写操作在 M9-7 之后不存在**（只读白名单），所以快照不必跨线程，但**要有一条断言钉住这个前提**（否则哪天白名单松了，rewind 会静默漏掉 worker 写过的文件）。

  **实现证据（每一项都可复现）**
  - **新叶子模块 `agent/workspace.py`（814 行，纯标准库）**：`WorkspaceSnapshots`（`note_write` / `capture` / `plan` / `restore` / `steps` / `usage` / `import_manifest`）、`FileChange`、`RestoreAction/Plan/Report`、`SnapshotUsage` / `ObjectStoreStats` / `DropResult`、模块级 `_put_object` / `_get_object` / `_live_shas` / `object_store_stats` / `list_snapshot_sessions` / `drop_snapshots`。**不认识 `QueryEngine`/`AgentState`**（同 `agent/subagents.py` 的叶子约定）。
  - **写盘顺序不变式：对象 → 清单 → 检查点**。任何一步被杀留下的只能是**孤儿对象**（不可达的字节），不能是**说谎的引用**。清单用**临时文件 + 原子替换**写。三个方向的边界测试逐条点名：`test_capture_writes_objects_before_manifest`（顺序）、`test_manifest_write_never_leaves_a_partial_target`（写一半被杀，目标名一次都不出现）、`test_torn_manifest_tmp_is_invisible`、`test_capture_survives_a_crash_between_manifest_and_checkpoint`、`test_half_written_object_is_not_counted_and_reads_as_missing`、`test_corrupted_object_is_detected_by_hash`、`test_corrupt_manifest_does_not_half_apply`。
  - **`--rewind` 默认预览 + 二次确认**（`--force` 跳过）。预览是纯读的：B 动线对**两个目标**（命中快照的第 5 步、走 `base` 分支的第 1 步）各拒绝两次（答 `n` 与答**空**），盘面**逐字节未变**；确认框每次都真的问了。
  - **全局共享对象库 + 跨会话回收**（用户拍板）。`data/snapshots/objects/{sha[:2]}/{sha}.bin`，`fork` **只复制清单、一个对象都不复制**；回收靠**从所有剩余清单现算** live set（`_live_shas`），**不存引用计数**。跨会话边界测试 6 条：`test_objects_are_shared_across_sessions`、`test_two_sessions_really_share_the_base_object`、`test_drop_keeps_objects_another_session_still_references`、`test_drop_reclaims_objects_nobody_references`、`test_corrupted_object_affects_both_sessions_the_same_way`、`test_concurrent_put_object_keeps_one_intact_copy`。
  - **`--fork --step K --rewind`**（用户拍板 P1）：回滚落在"只分叉不续跑"那个 `Exit` **之前**，两件事一起做。真 CLI 子进程验证：退出码 0、分叉警告跟着 `--rewind` 改了口径、文件真的回到第 5 步、收尾给出 `--resume --step 5` 的出口。
  - **测试**：`tests/test_workspace.py` **785 行 / 49 例**（新建；数字按 `pytest --collect-only` 数），加 `test_cli.py` / `test_repl.py` / `test_session.py` 的接线契约 22 例，全量 **707 passed in 81.36s**（M5-5 之后全量为 **769**）。
  - **变异测试**：`m9verify/mutate_m9_8.py`（506 行）**35 条，35/35 全被抓、零 SKIP、零 MISS**（`m9verify/mutate_m9_8.log`）。四个 MISS 全部修掉，其中**三个是测试本身没牙齿**（断言分辨不出差别 / 边界数据缺失 / 用户参数掩盖了被测行为），一个是**脚本自身的锚点缺陷**（`str.replace(..., 1)` 换到了逐字相同的另一个函数上，症状与"MISS"完全同形）。
  - **真实 LLM 动线 A~F**（`m9verify/drive_m9_8.py`，639 行，六份日志）：A 真改动 → 真快照（8 步 / 53676 token / 缓存命中 96% / 11.4s）；B 预览纯读；C 真回滚两条分支（走清单的第 5 步 + 走 `base` 的第 1 步）；D `--fork --step 5 --rewind` 走真 CLI；E 磁盘口径 + 并发探针；F `--snapshots` / `--drop-snapshots`。
  - **实测数字（E 动线，本机 Windows 10 + NTFS，**不许与 README 的 token 数字混着比**）**：
    - **基座成本 = 被 `write`/`edit` 碰过的文件的原始字节之和，不是整个工作区** —— 工作区 5 个文件 / 8892 B，进清单的只有 2 个，基座 **1949 B**（21.9%），且那个百分比只取决于"改了几个文件"。→ 用户裁定"直接存完整基座、不加开关"的依据。
    - 清单 599 B vs 对象 4339 B ≈ **13.8%**（每步全量清单的代价）；孤儿对象 **0 个**。
    - **`fork` 的磁盘增量：0 个对象 / 0 字节**，清单 +599 B。→ 全局共享对象库的依据（per-session 会让每次倒带重试都复制一遍当时的工作区）。
    - **回收**：删源会话的 2 份清单 → 回收 1 个对象 / 746 B，3 个因别的会话还在引用而保留。
    - **并发写同一对象（8 线程 × 5 轮 = 40 次，抢同一个目标名）**：裸替换冲突 **24~30 次**（`PermissionError`，6 次独立重跑：30/25/27/24/28/26），经 `_put_object` **未捕获异常 0 次**。→ **不加锁**（用户裁定 7：不要凭空增加锁的复杂度）。如实标注：Linux 上 `rename(2)` 通常不抛 `EACCES`，那条容错分支在别的平台上可能是死代码；两个平台都不需要锁。
  - **`plan()` 的新语义（初稿算法里没有的一支）**：`K` **低于所有快照**不是错误 —— `min(manifests)` 就是"第一个有受管文件的步"，比它还早的步"那时什么都没受管"是**事实**，于是所有路径回到各自的 `base`。**没有这一支，`base` 是结构上不可达的**。与它成对的是：夹在两快照中间的步（如 5 与 10 之间的 7）**仍然报错** —— 那是"不知道"，不是"没有"。
  - **六条开放问题的决议**全部写进 `docs/design/m9-8_rewind.md` §10（含初稿提问原文与结论的逐条对照）。

### 明确不做（理由留痕）

- **⑥ 全屏 TUI**：**不做。但理由是"第二套状态机"，不是"体力活"。** 原先这里写的是「纯终端渲染的体力活，面试加分有限」—— 那低估了成本，是错的（2026-09-11 逐文件核实）。
  - 真成本在于**第二个前端自带一整套状态机**：TS 原版自己的 `src/tui/` 只有 **8 个 .ts**（`chrome/index/input-parser/input/markdown/screen/transcript/types`），而 Python 移植版 `F:\MiniCode-Python-main\minicode\tui\` 长成了 **19 个 .py / 4,941 行**（多出 `event_flow` / `input_handler` / `navigation` / `runtime_control` / `session_flow` / `state` / `theme` / `tool_helpers` / `tool_lifecycle` / `ui_hints` —— 全是"状态与流程"，不是"画字符"）。
  - **Windows 上还得真写平台代码**（不是夸张，是移植版里的原样）：`screen.py:60-74` 用 `ctypes.windll.kernel32` + `GetStdHandle`/`GetConsoleMode`/`SetConsoleMode` 打开 `ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004`（不开的话 ANSI 全打在屏幕上）；`input_handler.py:63-73` 用 `msvcrt.kbhit()`/`getwch()` 逐键读，还要把扫描码手工翻成 ANSI 转义序列（文件里有一张映射表）。**这些都不是"渲染体力活"，是平台适配 + 一整套输入状态机。**
  - 行式 REPL 已经把"流式渲染 + 工具卡片 + 状态位"做完了，TUI 换不来新的**能力**，只换观感。我们已有 Streamlit 控制台可演示（且已在真实浏览器里跑通过）。
  - **单独放行一小块（从上面这个结论里摘出来，不连带否掉）**：行式 REPL 加 **`readline` 输入历史**（↑/↓ 翻历史、Ctrl+R 搜索）。几十行，吃掉观感差距的大半，且不引入任何新状态。它不在"不做"里。
- **⑤ Loop**：**真不做，理由不是"价值低"。** 原先这里写的是「价值低（本质是个定时器）；REPL 做出来之后**顺手做，不单独排期**」—— 那是**缓做**，却挂在一个叫「明确不做（理由留痕）」的标题下面。**让一个还没做的决定冒充已做的决定，比不做更糟**（这条本身就是本项目在防的缺陷类：文档与实现漂移）。
  - 真理由：**它与 Goal 抢同一个回合执行器。** TS 原版为此在 `src/runtime/session-runtime.ts` 里写了**五处 throw + 一个 busy 谓词**（2026-09-11 逐行核实）：
    - `:84` 起 Loop 时若 goal 活着 → `'Pause or clear the Goal before starting a Loop; answer or clear any pending Goal question.'`
    - `:132` 起/恢复 Goal 时若 loop 活着 → `'Stop the Loop before starting or resuming a Goal.'`（`assertIdle`）
    - `:34` Loop 自己的 `busy` 谓词里含 `this.goal.enabled`（`busy: () => this.turns.busy || this.turns.awaitingUser || this.goal.enabled || ...`）
    - `:62` / `:68` 起一个回合时若 loop 在跑 / goal 活着 → 各自 throw
    - `:133` `assertIdle` 第二句：回合忙 / goal 在跑 → `'Wait for the current turn to stop.'`
    - 也就是说：**两个都想驱动同一台回合执行器，于是互斥只能靠外部断言维持，而且要在每个入口都写一遍。** 实现它 = 多维护这一整套互斥不变式，并会把 M9-6 那条**「一拍 = 一次授权」**（`_run_goal_burst` 结尾必 `_pause_goal`）变复杂。
  - 一句话口径：**定时器人人写得出来，贵的是那一整套互斥不变式。**
- **⑧ context collapse**：**测过同类机制收益不达预期，所以不做**。分级截断与本项同为「改动 `_PREFIX_LEN` 之后窗口」的机制，实测（P7-d）：单次省 5,128 token ↔ 多付 **45,304 miss token（8.8 倍）**，代价形状是后缀失效。这与我们**唯一的缓存亮点**直接冲突 —— 拿一个反例数据说明「不做」，比照着原版补上更值得讲

---

## 进度快照

- 当前里程碑：**M5-6 前三个优先级（2026-09-12 完成）** —— S16 bash 命令文本沙箱（分层 deny/ask、两处执行点、与 `read`/`write` 共用 `resolve_in_workspace`）、评估层三条遗留全部收口（`_FENCE_RE` 回写 + `ruler` 指纹 / 判定守卫默认翻转为开 + P4 + P1 / `terminated_reason` 与工具调用记录落进 `per_task`）、扩样本的零成本部分（多仓参数化 + 第二仓 `sqlparse` 闸门 143→54 + 成本上限）、`single-shot` 重跑实测。详见上方 M5-6 一节。
- 上一里程碑：**M5-5 评估层加固 + 三臂真跑（2026-09-12 完成）** —— 物理剥离物化 + 泄漏探针、有效性闸门（39 候选 → 21 有效）、指标诚实性、定价快照、三臂真跑、**评测器审计（`_FENCE_RE` 围栏配对错位 → 5 个假阴性 → 零成本离线重算）**。详见上方 M5-5 一节。
- 上一里程碑：**M9 全部完成 —— 12 项核心能力清单落地 + M9-8 工作区 rewind 快照（2026-09-12 完成）**。第三批 M9-5 常驻交互模式 REPL、M9-6 ④ Goal；压轴 M9-7 ② sub-agent 并发（2026-09-11，变异 29/29、四条动线真跑）；**M9-8 工作区 rewind 快照 —— 2026-09-12 完成，变异 35/35、六条动线 A~F 真跑**。里程碑清单已清空，无未开工项。
- 上一里程碑：**M8 完成**（P1–P7 全部完成；四项真实验证已跑，见上节）
- 上一里程碑：**M7 完成**（M7-1~M7-6 + Part C 全部完成；四条真实 LLM 端到端验证 V1–V4 已全跑，工作区 `m7verify/`、`m7verify-nolock/` 已 gitignore）
- 代码状态：**14,425 行源码 / 31 模块 / 847 测试全绿**（`python -m pytest tests/ -o addopts="" -q` → `847 passed`，无 skip；M9-8 完成时那一次 **81.36s**、2026-09-12 文档同步后 **88.47s**、M5-5 完成时 **295.86s**、**M5-6 完成时 459.56s** —— **用例数稳定，墙钟随机器负载浮动**；M5-5 那次机器上并发跑过别的东西、M5-6 那次后台跑且机器同时在忙，都不要拿它们当基线）
  - 口径：源码 = `agent/` + `app/` + `eval/` 下的非空 `.py`（不含 `eval/repos/` 克隆仓），模块数 = 其中非空的 `.py` 文件数。**按文件系统数，不按 git 跟踪数** —— 新文件在提交前也该算进去（M9-5 时 `app/repl.py`(880) 与 `tests/test_repl.py` 尚未提交，按 git 数会各少一份，这正是上一条从 8,310 跳到 9,139 里的一部分；M9-6 的 `agent/goal.py`(232) / `agent/tools/goal.py`(118) / `tests/test_goal.py`(927)、M9-7 的 `agent/subagents.py`(540) / `tests/fake_llm.py`、M9-8 的 `agent/workspace.py`(814) / `tests/test_workspace.py`(785)、**M5-5 的 `agent/pricing.py`(181) / `tests/test_pricing.py`** 同理，目前也尚未提交）。
  - `tests/` 合计 **16,363 行 / 29 个非空 `.py`**（30 个文件，含一个空的 `__init__.py`；= **23 个 `test_*.py` + 7 个夹具模块**；上一版 14,551 行 / 28 个；M5-5 新增 `tests/test_pricing.py`，并给 `test_golden_tasks.py`（24→25 例）与 `test_eval_runner.py`（28→37 例）补了闸门 / 指标口径 / 三臂 / 快照成本的用例；**M5-6 又给 `test_golden_tasks.py`（25→39 例）与 `test_eval_runner.py`（37→65 例）补了 P4 计数解析 / P1 捕获-恢复-字段 / `ruler` 指纹 / 围栏错位回归，并新增夹具模块 `tests/real_fence_desync.py`（5 段真实围栏错位 `raw_output` 原文，**不被 pytest 收集**）**）。
  - M5-5 的验证脚本在 `evalverify/`（**gitignore**，同 `m9verify/` 惯例）：`diff_extract_fixed.py`（**已降级为第 0 阶段自校验用的历史 bug 复现**；修复已回写进版本库里的 `eval/runner.py`，见 §9.6d）/ `rescore_single_shot.py`（离线重算，零 LLM）/ `reanalyse_patch_failed.py`（逐任务取证）/ `drive_three_arms.py`（三臂大对照）/ `drive_isolation_report.py` / `drive_judge_gaming.py` / `mutate_m5_5.py` + 各步日志与修正版报告 JSON。
  - 验证脚本：`m9verify/mutate_m9_8.py`(506 行) / `m9verify/drive_m9_8.py`(639 行) + 六份动线日志。
- **M8 期间发现并修复的真 bug（真跑挖出来的，不是单测挖的）**：
  1. **`update_plan` 的引导缺失（elicitation gap）**：工具实现了、测试全绿、计划也能落盘 —— 但 system prompt 里**一个字都没提它**，模型 6 步跑完一次都没调。修法：prompt 里写明"任务复杂时先调 `update_plan` 排一份 3~6 步的简短计划"。修前 0 次 / 修后 2 次（同 P7-c 表）
  2. **`--resume` 静默丢掉 `checkpoint_every`**：`from_checkpoint` 的默认值是 5，而 `--resume` 这条路径没转发 → `--resume --checkpoint-every 1` 静默回落成"每 5 步一次"。后果不是报错，而是**恢复出来的这一段一步都不落盘**（kill 在 step 5，续跑到 step 9，检查点数还是 5）。修好后同一段跑出 `[1..10]`
  3. **恢复期的三条事件进不了轨迹**：`record_event` 只在 `state.emitter` 非空时写 JSONL，而 emitter 原先要等 `run_from` 才被引擎接上 —— 于是 `taint_cleared` / `plan_resumed` / `resume_instruction` 一条都落不了盘（`clear_taint` 注释里写的"同时往轨迹里记一条"因此是假的）。修法：`restored.emitter = session.emit` 先接上再记
  - 三条都是**同一个缺陷类**：机制在、测试绿、真跑才发现没生效。回归测试 `tests/test_cli.py::test_resume_honors_checkpoint_every`、`::test_resume_events_reach_the_trajectory`、`tests/test_loop.py::test_default_system_prompt_points_at_update_plan`，并逐条做了变异（3/3、2/2 被捕获）
- **M9-5 期间发现并修复的真 bug（两条，都是"多回合契约"这一类）：**
  1. **`/new` 的会话 id 会撞车（秒级粒度）**：`new_session_id()` 的格式是 `s%Y%m%d-%H%M%S`，而常驻进程里 `/new` 紧接着 `/new`、或者一次快速演示里连开两个会话，**完全可能落在同一秒**。撞了的话 `Session.__init__` **不报错**，两个"不同"的会话直接共用同一个检查点目录 —— 后者覆盖前者，**全程静默**。单发路径上这个碰撞要靠"人手动重跑"才会发生，所以从没暴露过。修法：`_new_session` 改用已有的 `unique_session_id()`（循环避让）。**是写测试时被断言 `assert new_id != old_id` 逼出来的**，不是真跑挖的 —— 如实记下来。
  2. **退出时承诺的续跑命令在纯聊天会话上跑不通**：`_farewell` 打印「继续: `python -m app.cli --repl --resume --session-id <sid>`」，而检查点是**按节拍**写的（默认 5 步）且**只在有工具调用的步上 tick**。纯聊天、或者只走两三步工具就退出，**一个检查点都不落** —— 那条命令跑起来直接报"读不到检查点"。**M7 的原话是"一条走不通的指引比没有指引更糟"，而这次是我们自己打印出来的。** 修法：`_wrap_up` 退出时 `checkpoint(self.state, force=True)`，**先落盘再提炼**。理由与 `_awaiting_user` 里 force 的理由是同一个：流程即将因非步数原因退出，节流的下一次 tick 永远等不来。真跑证据：A 会话实时显示"检查点 7 个"、退出后盘上是 8 个（多出来的 `step-9.json` 就是这一次强制落盘）。
     - 顺带一个同型判断：`_wrap_up` 只在 `self._turns > 0` 时才做约定提炼 —— `extract_and_learn` 是一次**真实的模型调用**，而它读的是累计轨迹，每回合跑一遍等于把同一段对话提炼 N 次；单发路径也是"一次运行提炼一次"，常驻对齐它。
- **M9-6 期间：真跑没有挖出产品缺陷**（如实记 —— 本项目此前的真跑几乎每次都挖出一两条"机制在、测试绿、没生效"的 bug，这次没有）。真跑暴露出来的两条都是**我的素材/流程问题**：① 测试素材里那条数学上不可能通过的断言（见 M9-6 条目最后）；② **动线 ④ 在纯 stdin 流程里结构上不可观测**这件事本身 —— 它不是 bug，是「一拍 = 一次授权」不变式的推论（`loop()` 进提示符前必先跑一拍 + `_run_goal_burst` 结尾必暂停 → 提示符处目标绝不为 active），驱动脚本 `m9verify/drive_m9_6.py` 的头部**逐字写明了为什么需要它**，而不是悄悄绕过去。
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
    - ⚠️ **数字漂移已修（M5-5）**：上面那组 `62,812 / ¥0.0625 / 83%` 是 **2026-09-10 那一次**的事实，不是"当前数字"。M9-2 之后同一条命令重跑为 **82,495 / ¥0.0769 / 82%**（`default()` 多了两个工具的 schema），而 M5-5 之后 `--limit 2` 这组**已经不再是可引用的跑分**——那两个任务里有一个（`e70f9b1d`）被有效性闸门判定为**白送分**（隐藏测试在 base 就通过）。**同一条命令在三处写了两组"最新"数字这件事本身就是缺陷**，所以从此统一口径：**只有 `data/eval/report-*.json` 里带 `pricing_snapshot_id` 的那份报告是跑分依据**，本文档只做历史记录。
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

