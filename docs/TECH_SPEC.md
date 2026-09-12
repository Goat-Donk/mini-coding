# TECH_SPEC — 详细技术方案（模块级设计规格）

> 本文档是**唯一权威**的模块设计规格：实现以本文为准，写完代码/改了设计都要回填本文，保证 clear 对话后照抄可续作。
> 文档级别：每个模块一节，含 文件路径 / 核心类与函数签名 / 数据结构与 JSON 格式 / 算法要点 / 边界与坑 / 测试文件与关键用例 / 验收命令。
> 模块成熟度标注：★M1 已完成 / ◐M1 部分完成（后续里程碑补） / ○ 未开始（后续里程碑写规格）。

---

## 0. 总览

### 0.1 分层与依赖方向

```
app/  (cli + streamlit 入口)
  ↓
agent/loop.py  (QueryEngine，唯一编排者)
  ├── llm.py        (BaseLLM 抽象 → DeepSeek/Mock)
  ├── tools/        (Tool 基类 + 具体工具；registry 集中注册)
  ├── state.py      (AgentState：消息/usage/事件)
  ├── context.py    (消息布局/预算/compact —— M3 补)
  ├── permissions.py (allow/deny/ask —— M2 补)
  ├── hooks.py      (Pre/PostToolUse —— M2 补)
  ├── session.py    (轨迹/检查点/resume/fork —— M3 补，M9-3 补元数据)
  ├── workspace.py  (工作区快照 + 回滚 —— M9-8 补)
  ├── subagents.py  (并发子代理管理器 —— M9-7 补)
  └── memory.py     (repo 记忆 —— M4 补)
```

依赖方向严格单向：loop 依赖一切；tools 只依赖 base 的 ToolResult/ToolContext 与 state 的消息构造；下层不 import 上层。

### 0.2 核心数据结构速查

- 消息：OpenAI Chat Completion 格式 dict（`system` / `user` / `assistant(content, tool_calls)` / `tool`）。
- 工具 schema：OpenAI function calling 格式（`{type:"function", function:{name, description, parameters}}`）。
- 工具结果：`ToolResult{success, output, data, error, duration_ms}` —— `output` 是给模型看的文本（截断后），`data` 是结构化数据。
- 轨迹事件：`{ts, type, step, ...}` 的 dict 列表，写 JSONL（M3）。

### 0.3 硬约束（任何实现不得违反）

1. LLM 只通过 `BaseLLM` 接口调用；生产用 DeepSeekClient（OpenAI-compatible），测试用 MockLLM。
2. 所有路径操作必须经过沙箱校验（`workspace_root` 内，见 tools/files.py `_resolve`）。
3. bash 工具禁止执行危险命令（黑名单，M1 直接拒绝、M2 转 ask）。
4. 工具结果 `output` 必须截断（大结果给模型留有余量），截断必须带明确提示（CC 的 TRUNCATED_MESSAGE 风格）。
5. 工具调用失败时，错误信息必须**回喂给模型**（作为 tool 结果），让模型自修复。
6. 循环必须有终止判定：max_steps 上限 + 循环检测（坍缩防护）。

---

## 1. agent/llm.py（★M1 完成）

**文件**：`agent/llm.py`

### 1.1 数据结构

```python
@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    prompt_cache_hit_tokens: int = 0      # DeepSeek 磁盘缓存命中
    prompt_cache_miss_tokens: int = 0     # 未命中（= 新写入缓存的 token）
    # properties:
    #   total_tokens = prompt + completion
    #   cache_hit_ratio = hit / (hit + miss)  （分母为 0 时返回 None）
    # 支持 __iadd__ / __add__（累加到会话总 usage）与 __sub__（★M9-5：取回合增量）

@dataclass
class ToolCall:
    id: str            # 工具调用 id（回填 tool_calls 消息 / tool 结果消息）
    name: str
    arguments: dict    # 解析后的 dict（JSON）
    def signature(self) -> str:  # f"{name}({json.dumps(arguments, sort_keys=True)})"，用于循环检测

@dataclass
class LLMResult:
    content: str | None
    tool_calls: list[ToolCall]     # 空列表 = 没有工具调用（最终回答）
    usage: Usage
    finish_reason: str | None
```

### 1.2 类

```python
class BaseLLM(ABC):
    model: str
    @abstractmethod
    def chat(self, messages: list[dict], tools: list[dict], *, temperature: float = 0.0) -> LLMResult:
        """messages 已是 OpenAI 格式；tools 已是 OpenAI function schema 列表。"""
    # 便捷方法
    def complete(self, messages, *, temperature=0.0) -> str:   # 不允许工具调用的纯文本补全（compact 摘要等用）

class DeepSeekClient(BaseLLM):
    def __init__(self, api_key: str | None = None, base_url: str | None = None,
                 model: str | None = None, timeout: float = 120.0):
        # 默认 base_url=https://api.deepseek.com, model=deepseek-chat
        # 读环境变量 DEEPSEEK_API_KEY / DEEPSEEK_BASE_URL / DEEPSEEK_MODEL（.env 由调用方 load）
        # self._client = openai.OpenAI(api_key=..., base_url=..., timeout=...)
    # chat():
    #   resp = self._client.chat.completions.create(model, messages=messages, tools=tools or omit,
    #                                               temperature=temperature)
    #   msg = resp.choices[0].message
    #   tool_calls = [ToolCall(id=tc.id, name=tc.function.name, arguments=json.loads(tc.function.arguments or "{}"))
    #                 for tc in (msg.tool_calls or [])]
    #   usage = Usage(prompt_tokens=..., completion_tokens=...,
    #                 prompt_cache_hit_tokens=u.get("prompt_cache_hit_tokens") 或 usage.prompt_tokens_details.cached_tokens,
    #                 prompt_cache_miss_tokens=u.get("prompt_cache_miss_tokens", 0))
    #   content = msg.content（可能为 None，或 content 是列表时取其中 text 拼起来）

class MockLLM(BaseLLM):
    """确定性脚本化 LLM（测试用，无网络）。
    responses: list[LLMResult | Callable[[list[dict], list[dict]], LLMResult]]
    每次 chat() pop 一个；是 callable 则调用它生成（可依赖 messages/tools 断言历史）。耗尽抛 RuntimeError。
    """
    def __init__(self, responses: list): self.responses = list(responses)
    # 静态工厂：
    #   MockLLM.text(content, usage: Usage|None=None) -> MockLLM（单次纯文本回答）
    #   MockLLM.tool(tool_name, arguments: dict, *, content: str | None = None, usage=None) -> MockLLM
    #   MockLLM.script(*responses) -> MockLLM
    # tool id 自动生成：f"call_{i:04d}"
```

### 1.3 边界与坑

- openai SDK 3.x 返回的 `message.content` 可能是字符串或 None；DeepSeek 下是字符串。
- DeepSeek 缓存字段是顶层 `usage.prompt_cache_hit_tokens`；OpenAI 风格是 `usage.prompt_tokens_details.cached_tokens` —— 两者都兜底读取。
- `tools=None` 时不传 tools 参数（部分模型不支持空列表）。
- MockLLM 耗尽时抛 `RuntimeError`，loop 测试用于断言"不该再调用模型"。

### 1.4 测试

`tests/test_llm.py`：
- Usage 累加/命中率（hit=900, miss=100 → 0.9；分母 0 → None）
- MockLLM.text / tool / script 三种构造与 pop 顺序；耗尽抛错
- DeepSeekClient 构造（不真正调用；无 key 时跳过，见测试 skip 逻辑）

**验收**：`python -m pytest tests/test_llm.py`

---

## 2. agent/tools/base.py（★M1 完成）

**文件**：`agent/tools/base.py`

### 2.1 ToolResult

```python
@dataclass
class ToolResult:
    success: bool
    output: str                 # 给模型看的文本（截断后）
    data: dict | None = None    # 结构化数据
    error: str | None = None
    duration_ms: int = 0
    await_user: bool = False    # M8：工具声明"本轮到此为止，等人回答"（是数据标志，不是阻塞）
    file_changes: tuple["FileChange", ...] = ()   # ★M9-8：这次调用**成功写盘**的文件改动
    @staticmethod
    def ok(output, data=None, *, await_user=False, file_changes=()) -> "ToolResult"
    @staticmethod
    def fail(error: str, output: str | None = None) -> "ToolResult"
        # fail 时 output 默认 = f"工具执行失败: {error}"（回喂模型，可自修复）
```

**`file_changes`（M9-8）为什么必须由工具报告**：登记发生在工具跑完**之后**，那时旧内容
已经被覆盖掉了。`EditTool` 的 diff 预览虽然算出了旧内容，但那是对 `errors="replace"`
解出来的文本重新编码，**未必等于盘上的字节**；`WriteTool` 更是一路都不需要旧内容。
所以工具在写盘**之前**把 `FileChange{path, before, base_unknown}` 报告出来，`loop._gate_and_run`
在成功后逐条 `note_write` —— 这是工作区快照的**唯一**登记来源，且"权限拒绝的那次调用压根没
执行 / edit 匹配失败 / 工具自己抛了"三种情况**结构上**走不到登记那一行，不靠谁记得判断。
`base_unknown=True`（改动前存在但读不到）的路径**不纳入管辖**：把它当成"不存在"，回滚时就会
**删掉用户的文件**（`agent/workspace.py` 不变式 2 的同一个理由）。

### 2.2 ToolContext

```python
@dataclass
class ToolContext:
    workspace_root: Path        # 沙箱根（所有路径操作不得越界）
    cwd: Path                   # 当前工作目录（默认 = workspace_root；bash 可改）
    emitter: Callable[[dict], None] | None = None  # 轨迹事件回调（M3 接 session）
    permissions: object | None = None              # M2 接入
    hooks: object | None = None                    # M2 接入
    state: "AgentState | None" = None              # M8 接入（update_plan 经它写 state.plan）
    workers: "AgentWorkers | None" = None          # ★M9-7 接入（5 个子代理工具经它 spawn/wait/close）
```

> **`state` 曾经是"声明了但没人填"的缺口**（`emitter` 也一样：两个槽位都声明了，而
> `loop.py` 构造 `ToolContext` 时一个都没传）。工具作者照声明去读会拿到 `None`，
> **而且不报错** —— 直到 M8 的 `update_plan` 成了第一个真读者，这个缺口才暴露。
> 原 `settings: dict` 字段已删除：全项目没有任何读者，是同一个缺口的另一半。
> 现在 `tests/test_plan.py::test_context_state_is_filled_by_the_real_loop` 钉住它。
> **`workers`（M9-7）是第三个同类槽位**，所以它一并带上了守卫：`ctx.workers is None`
> 时 5 个子代理工具返回 `ToolResult.fail` 并**说明这是接线缺口**，而不是静默降级或抛异常。

### 2.3 Tool 基类

```python
class Tool(ABC):
    name: ClassVar[str]          # 如 "read" / "edit" / "bash"
    description: ClassVar[str]   # 写给模型的说明（少而精，参照 CC：解释不清=不成熟）
    input_model: ClassVar[type[BaseModel]]   # pydantic 输入模型（参数必须扁平，暂不支持嵌套）

    @classmethod
    def is_read_only(cls) -> bool: return False      # ★ 决定 loop 里并发还是串行

    def needs_permission(self, arguments: dict) -> bool: return False
        # 工具自声明是否需要人工确认（M1：bash 危险命令 True；M2 起由权限引擎统一裁决）

    def preview(self, arguments: dict, ctx: ToolContext) -> str | None: return None
        # ★M9-1 声明这次调用**将要做什么**，供权限确认在动手之前查看。
        # 收**原始 dict**（门禁链跑在 run() 之前，还没有 pydantic 校验）→ 实现方自己容忍缺字段，返回 None。
        # **必须是纯函数：绝不写盘、不改状态** —— 调用方不为它准备回滚。
        # 放在工具上而不是让权限引擎自己算：edit 的「唯一匹配」语义属于工具，
        # 引擎重算一遍就有了两个真相源，迟早「预览说能改、执行说不唯一」，而那时人已经点过允许了。
        # 目前只有 WriteTool / EditTool 实现（整份覆盖与精确替换是两类误伤收不回来的动作）。

    def schema(self) -> dict:
        # s = self.input_model.model_json_schema()
        # 清理：删 title、删 $defs（扁平参数不应有）；确保 {"type":"object","properties":...,"required":...}
        # 返回 OpenAI 格式: {"type":"function","function":{"name","description","parameters": s}}

    def run(self, arguments: dict, ctx: ToolContext) -> ToolResult:
        # 1) t0 = perf_counter
        # 2) pydantic 校验: try: args = self.input_model(**arguments) except ValidationError as e:
        #       return ToolResult.fail(f"参数校验失败: {e.errors()... }"（把缺参/类型错误明细回喂模型）
        # 3) 执行 execute；异常兜底 except Exception as e → fail(f"{type(e).__name__}: {e}")
        # 4) duration_ms 回填 result
        # hooks 注入点（M2）：pre/post 由 loop 统一调用，不进 run()

    @abstractmethod
    def execute(self, args: BaseModel, ctx: ToolContext) -> ToolResult: ...
```

### 2.4 ToolRegistry

```python
class ToolRegistry:
    def __init__(self, tools: Iterable[Tool] = ()): self._tools: dict[str, Tool] = {}
        # register 时检查 name 唯一，重复抛 ValueError
    def register(self, tool: Tool) -> None
    def get(self, name: str) -> Tool                     # KeyError 由调用方转 ToolResult.fail
    def schemas(self) -> list[dict]                      # 全部 schema()，OpenAI 格式
    def names(self) -> list[str]
    def read_only(self) -> list[Tool]                    # is_read_only() 的工具
    def writable(self) -> list[Tool]                     # 非只读工具
    @classmethod
    def default(cls, workspace_root: Path) -> "ToolRegistry"
        # 注册 bash/read/write/edit/glob/grep（M4 加 subagent）
```

### 2.5 边界与坑

- pydantic v2 的 `model_json_schema()` 会给 `$defs`（嵌套模型时）。**约定：工具参数扁平化**，出现 `$defs` 即视为设计错误（测试里断言）。
- `required` 列表：仅填没有默认值的字段。
- 未知工具名：loop 返回 `ToolResult.fail(f"未知工具 {name}，可用: ...")` 回喂模型。
- **`preview` 的调用时机是它唯一的失败模式，而且失败时是静默的**（M9-1）：必须在 `permissions.check()` **之前**取。挪到之后不会报任何错 —— 功能照样跑、确认框照样显示 diff，只是那份 diff 已经是**事后**的了。所以 `tests/test_loop.py::test_edit_diff_is_available_before_the_write` 断的是「确认回调被调用的那一刻，磁盘上必须仍是原文」。
- **预览 fail-open、权限 fail-closed**：`_preview` 抛异常只记 `preview_failed` 事件并退回原确认框（预览是**信息**）；权限链算不出来必须大声失败（判定漏一次就是门禁漏一次）。
- **没有权限引擎时根本不计算预览**：没有确认交互就没有人看得到它，而 edit 的预览要把整个文件读进来做 diff。headless（`eval/runner.py`）走的就是这条路，白算是实打实的每步开销。

### 2.6 测试

`tests/test_tools.py`（公共部分）：
- test_schema：从简单模型生成的 schema 结构正确（type/properties/required）；扁平模型无 $defs
- test_run_validation_error：缺参/类型错 → success=False 且 output 含错误详情
- test_run_success_timing：duration_ms > 0

**验收**：`python -m pytest tests/test_tools.py::test_schema tests/test_tools.py::test_run_validation_error tests/test_tools.py::test_run_success_timing`

---

## 3. agent/tools/bash.py（★M1 完成）

**文件**：`agent/tools/bash.py`

### 3.1 输入模型

```python
class BashInput(BaseModel):
    command: str                    # shell 命令
    cwd: str | None = None          # 相对 workspace_root 或绝对路径（必须在其内）
    timeout: int = 120              # 秒，上限 300
```

### 3.2 类

```python
class BashTool(Tool):
    name = "bash"
    description = "在 workspace 内执行 shell 命令并返回 stdout/stderr 与退出码。用于运行测试、查看文件、git 操作等。禁止危险命令。"
    DANGEROUS_PATTERNS: list[str] = [
        r"(^|[;&|]\s*)rm\s+(-[a-z]*[rf][a-z]*\s+)+",   # rm -rf
        r"git\s+push", r"git\s+reset\s+--hard",
        r"mkfs", r"fdisk", r"shutdown", r"reboot",
        r"format\s+[a-zA-Z]:", r"del\s+/[sqf]", r"rd\s+/[sq]",
        r":\(\)\s*\{", r"eval\s", r"curl\s+[^|;]*\|\s*(ba)?sh",   # fork 炸弹 / eval / curl|sh
    ]
    def is_read_only(cls) -> bool: return False
    def needs_permission(self, arguments): 
        return self._is_dangerous(arguments.get("command", ""))
    @staticmethod
    def _is_dangerous(command: str) -> bool   # 任一 pattern 命中（正则 search, re.IGNORECASE）
    def execute(self, args: BashInput, ctx: ToolContext) -> ToolResult:
        # 1) cwd 解析：cwd or ctx.cwd；必须 resolve 后 is_relative_to(ctx.workspace_root)，越界 → fail
        # 2) 危险检查：命中 → fail("命令命中危险模式，被拒绝：... 请改用安全的等效操作（如先 read 文件再 edit）")
        #    （M2 起改为走权限 ask 流程，此处保留为兜底）
        # 3) 平台命令：POSIX → ["bash", "-lc", args.command]；win32 → ["cmd", "/c", args.command]
        # 4) subprocess.run(..., cwd=..., capture_output=True, text=True, timeout=min(timeout,300), encoding="utf-8", errors="replace")
        #    TimeoutExpired → fail(f"命令超时({timeout}s)，请拆分或减少输出")
        #    FileNotFoundError（bash 不存在）→ fail 提示平台差异
        # 5) 拼输出：stdout + ("\n"+stderr if stderr) + 退出码行；截断 MAX_CHARS=20000 + TRUNCATED_MESSAGE
        # 6) data = {"exit_code", "stdout", "stderr", "truncated"}
        #    退出码非 0 不算工具失败：output 带上 exit_code，让模型自己判断（但 success=True）
```

### 3.3 边界与坑

- **退出码非 0 → success 仍为 True**：命令执行本身"成功完成"，业务失败信息在 output 里（符合 CC：把判断交给模型，别替模型下结论）。
- Windows 下无 `bash`：cmd 的 `echo` 语法不同 → 测试里用跨平台命令（`python -c`）。
- 大输出截断必须带提示：`\n... [输出被截断，共 N 字符，仅显示前 20000] ...\n`。
- timeout 上限 300s，防止模型自杀式长命令。

### 3.4 测试（tests/test_tools.py）

- test_bash_echo：`python -c "print('hello')"` → success, output 含 hello, exit_code 0
- test_bash_dangerous_rejected：`rm -rf /`、`git push origin main` → success=False 且 output 说明拒绝原因
- test_bash_timeout：`python -c "import time; time.sleep(5)"` timeout=1 → success=False 提示超时
- test_bash_cwd_escape：cwd="../outside" → success=False 提示越界
- test_bash_exit_code_not_fail：`python -c "exit(3)"` → success=True, data.exit_code=3

---

## 4. agent/tools/files.py（★M1 完成）

**文件**：`agent/tools/files.py`
**公共工具函数**（模块内，非 Tool）：

```python
def _resolve(ctx: ToolContext, raw: str, *, default_root: bool = False) -> Path | ToolResult:
    # Path(raw)；相对路径锚定 ctx.cwd；resolve(strict=False)；is_relative_to(ctx.workspace_root) 校验
    # 越界 → 返回 ToolResult.fail(f"路径越界沙箱: {raw}")；正常返回 Path
def _read_text(path: Path) -> str:      # encoding="utf-8", errors="replace"
def _truncate(text: str, limit: int = MAX_CHARS, *, kind: str = "字符") -> tuple[str, bool]
    # 超限截断 + TRUNCATED_MESSAGE；MAX_CHARS = 20000
```

### 4.1 ReadTool（只读）

```python
class ReadInput(BaseModel):
    path: str                 # 文件路径
    offset: int = 0           # 起始行号（0 起）
    limit: int | None = None  # 最多读多少行；None = 全部（仍受 MAX_CHARS 截断）
class ReadTool(Tool):
    name="read"; is_read_only=True
    description = "读取文件内容（带行号）。大文件自动截断，offset/limit 分段读取。"
    # execute:
    #   路径校验（必须存在、必须是文件，否则 fail）
    #   取行切片 → 拼 "行号: 内容"；截断 MAX_CHARS；输出开头注明共 N 行
    #   data = {"path", "total_lines", "start_line", "chars", "truncated"}
```

### 4.2 WriteTool

```python
class WriteInput(BaseModel):
    path: str
    content: str
class WriteTool(Tool):
    name="write"
    description="覆盖写入文件（自动创建父目录）。用于新建文件或整文件重写；小改动优先用 edit。"
    # execute: 路径校验（父目录也必须在沙箱内）→ mkdir(parents=True, exist_ok=True) → 写 utf-8
    # 返回 "已写入 N 字符到 path（覆盖）"
```

### 4.3 EditTool（★ CC 唯一匹配设计 + 失败自修复）

```python
class EditInput(BaseModel):
    path: str
    old_string: str
    new_string: str
    replace_all: bool = False
class EditTool(Tool):
    name="edit"
    description = "在文件中做精确字符串替换。old_string 必须唯一匹配（含完整上下文与缩进）；不匹配或匹配多次会报错并提示如何修正。"

    # _plan(args, resolved) -> (原文, 新内容, 失败原因)   ★M9-1
    #   **preview 与 execute 共用这一份匹配语义**，匹配/计数/替换只写在这里。
    #   各写一份的后果不是代码重复，而是「预览说能改、执行说匹配不唯一」——
    #   而那时人已经照着预览点过允许了。后两者恰有一个非 None。

    # preview(arguments, ctx) -> str | None            ★M9-1
    #   arguments 是原始 dict → EditInput(**arguments) 校验失败则 None（门禁链跑在 run() 之前）
    #   越界/文件不存在 → None（execute 与权限链各自有更准的话说）
    #   _plan 失败 → **返回 "[无法预览改动] {error}" 而不是 None**
    #     （人看到的该是「这次编辑根本改不动，因为 old_string 匹配到 3 处」，
    #      而不是一片空白；预览的全部意义就是让这个判断发生在写盘之前）
    #   否则 → _unified_diff(path, old, new)，上限 DIFF_PREVIEW_CHARS = 4_000

    # execute:
    #   1) resolved = _resolve(...)；不存在 → fail
    #   2) old_content, new_content, error = self._plan(args, resolved)；error → fail(error)
    #   3) 写回文件
    #   4) output = "编辑完成，diff：\n" + _unified_diff(path, old, new, limit=MAX_CHARS)
    #      ★ 这个 limit=MAX_CHARS 是必需的：_unified_diff 的默认值是给人看的 4_000，
    #        漏传就会把模型那份 diff 从 500K 悄悄砍成 4K —— 没有任何测试会自然变红，
    #        所以 tests/test_tools.py::test_execute_diff_is_not_capped_at_the_preview_limit 钉着它
    #   data = {"path", "replacements": count if replace_all else 1}
```

> **两处上限刻意不同**（M9-1）：`DIFF_PREVIEW_CHARS = 4_000` 给人看，`MAX_CHARS = 500_000` 给模型看。
> 几万字符的 diff 出现在确认框里，会把人逼成"闭眼点允许"—— 而一个训练用户不看内容的确认框，等于没有确认框。
> 两者共用 `_unified_diff`，所以那个 `limit=` 实参是这一项最容易被漏掉的接线。
>
> `WriteTool.preview` 同理，但它处理的是**整份覆盖**：工具结果里只有一句"已写入 N 字符到 X（覆盖）"，
> 人根本看不出丢了什么（`edit` 至少还能从 `old_string` 猜到改动范围）。新文件 → `[新建文件] p（N 字符）`；
> 内容相同 → `[内容无变化] p`；否则 unified diff。

### 4.4 GlobTool（只读）

```python
class GlobInput(BaseModel):
    pattern: str              # glob 模式（** 递归）
    path: str | None = None   # 起始目录（默认 cwd）
class GlobTool(Tool):
    name="glob"; is_read_only=True
    MAX_FILES = 100
    # execute: 沙箱内 Path.glob；收集相对路径；截断到 MAX_FILES
    # output: 每行一个相对路径；超限 → TRUNCATED_MESSAGE（CC 风格：明确告诉模型"还有更多，用 glob 更精确的 pattern 或 read 探索"）
```

### 4.5 GrepTool（只读）

```python
class GrepInput(BaseModel):
    pattern: str                 # 正则表达式（Python re，大小写敏感默认）
    path: str | None = None      # 搜索起始目录（默认 cwd）
    include: str | None = None   # 文件名过滤 glob（如 "*.py"；None = 全部文本文件）
    max_matches: int = 100
class GrepTool(Tool):
    name="grep"; is_read_only=True
    SKIP_DIRS = {".git","node_modules","__pycache__",".venv","venv","dist","build","data",".idea"}
    MAX_FILE_SIZE = 1_000_000    # >1MB 跳过
    # execute:
    #   os.walk 跳过 SKIP_DIRS；跳过 >MAX_FILE_SIZE；按 include 过滤文件名（fnmatch）
    #   逐行 re.search；收集 f"{relpath}:{lineno}: {line.rstrip()}"；达 max_matches 停止并标记 truncated
    #   output: 匹配行列表 + 截断提示（如有）
```

### 4.6 边界与坑

- **所有路径**先 `_resolve`，越界立即 fail（含 write/edit 的父目录）。
- read 的行号格式对模型友好：`12: content`，让 edit 能按 read 到的内容精确匹配（唯一匹配依赖此）。
- edit 失败信息要"可行动"（告诉模型下一步做什么），这是失败自修复循环的关键。
- glob 默认从 cwd 起；返回相对 cwd 的路径。

### 4.7 测试（tests/test_tools.py）

- test_write_then_read：写文件 → read 回读一致；read 带行号
- test_read_missing_file：fail，信息含路径
- test_edit_unique：唯一匹配替换成功，output 含 diff
- test_edit_no_match：fail 且提示用 read 查看
- test_edit_multiple_match：fail 提示不唯一 / replace_all=True 成功且 data.replacements=N
- test_glob_basic + 截断：>100 文件截断提示
- test_grep_basic：命中 file:line；include 过滤；max_matches 截断
- test_path_escape：read/write/edit/glob 的 path 指向沙箱外 → fail 越界
- **M9-1 预览**：`test_edit_preview_shows_the_diff_without_touching_the_file`（两头断言：diff 对**且**磁盘仍是原文）· `test_edit_preview_reports_why_it_cannot_change_anything`（匹配不唯一 → 给原因不是空白）· `test_edit_preview_and_execute_agree_on_uniqueness`（唯一性口径一致）· `test_write_preview_diffs_the_file_it_would_overwrite` · `test_write_preview_reports_a_new_file` · `test_preview_defaults_to_none` · `test_edit_preview_is_capped_for_the_human` · `test_execute_diff_is_not_capped_at_the_preview_limit`

---

## 4b. agent/tools/ask.py（M8，`await_user` 提问暂停）

```python
class AskUserTool(Tool):
    name = "ask_user"
    input_model = AskUserInput          # question: str

    def execute(self, args, ctx) -> ToolResult:
        # output = 问题原文：它同时也是回喂给模型的 tool 结果内容，
        # 于是 resume 后模型看到的是「assistant 调 ask_user(问题) → tool(问题) → user(回答)」，
        # 因果链完整，不需要再补一条 assistant 文本（那只是同一条信息出现第二次，
        # 白占 token 还破坏前缀缓存）。
        return ToolResult.ok(args.question, await_user=True)

    @classmethod
    def is_read_only(cls) -> bool:
        return False        # 它改控制流，必须串行
```

**核心不是这个工具，而是把「打断」建模成数据标志，而不是阻塞式控制流**：
`ToolResult.await_user: bool = False` 只是数据，工具自己不等人；turn 语义由 `loop._awaiting_user()` 决定。
于是 headless 通路**只要不注册这个工具就完全不受影响** —— 不需要在循环里写任何 `if headless`。

**注册：刻意不进 `ToolRegistry.default()`。** `eval/runner.py` 用的正是 `default()`，
而 headless 评测里没有人能回答 —— 模型一提问，eval 就提前终止，**完成率被一个"没人在那儿"
的机制拉低，而且是静默的**（judge 只跑测试）。按入口注册（`app/cli.py`、`app/ui_streamlit.py`），
用 `build_ask_tool()` 一处构造防接线漂移（同 `hooks.default_engine()` 的模式）。

**`checkpoint(force=True)` 不是优化，是正确性**：节流默认 5 步，而提问可能发生在第 3 步 ——
那一刻进程就退出了，下一次 tick 永远不来，问题就**没进检查点**，`--resume` 恢复出来的会话里
没有那个问题，用户对着一个不知道在问什么的会话回答。凡是「流程即将因非步数原因退出」的场合都要 force。

**loop 里的顺序**：`_execute_tool_calls` → 普通 `checkpoint(state)` → 若有待回答问题 →
`terminated_reason = "await_user"` + `record_event("await_user", ...)` + `checkpoint(force=True)` + return。
判据是 `if pending is not None` 而**不是**真值判断 —— 问题理论上可以是空串。

**CLI**：`terminated_reason == "await_user"` 时用独立的「需要你补充信息」样式打印问题
（用「最终结论」的样式打印它，人会以为任务跑完了，而这个回合的意义恰恰是"还没完，等你一句话"），
并给出**可执行**的续跑指引（M7 的教训：一条走不通的解除指引比没有指引更糟）。
停在提问处的会话**不做约定提炼** —— 那是一段半程轨迹，模型当时正在猜。

---

## 4c. agent/skills.py + agent/tools/skills.py（M8，SKILL.md 渐进披露）

**渐进披露 = 省 token 的全部依据**：system prompt 里**只有 `name` + 简介**，
正文由模型调 `load_skill` 按需取。装 50 个 skill 也不额外占常驻 token。

- 发现根（`setdefault` 先到先得）：项目 `<ws>/.codeagent/skills` → 用户 `~/.codeagent/skills`
  → 兼容 `<ws>/.claude/skills` → 用户 `~/.claude/skills`；**同名被遮蔽时记 `skill_shadowed` 事件**（不静默覆盖）。
- `extract_description(text)`：启发式取 frontmatter 的 `description`，退化到第一个非标题行，**不做 YAML 解析**。
- `load_skill_body(skill)`：单 skill 正文预算对齐 `memory.py` 的 8K 惯例，超限**截断并明确提示**。
- 加载不存在的 name → `fail` 且**列出可用 skill 名**（照既有 `_gate_and_run` 未知工具回喂可用名的做法）。
- 扫描结果**只扫一次**，同时喂给工具与引擎（两边各扫一次会让"索引里有的名字 load 不到"这种不一致有地方发生）；
  `build_skill_tools(skills)` **闭包捕获扫描结果**，不在每次 `execute` 里重扫。
- 无 skill 时**不注册工具**（不白送 schema 占 token）。

**system prompt 的纪律**：`system` 每次 run 只算一次并写进 `state.system_prompt`（进检查点），
**绝不能每轮重新发现** —— system 是 `messages[0]`、在 `_PREFIX_LEN` 保护区内，
每轮变一次等于整条前缀缓存永久失效。`--resume` 的语义正好因此是对的：
恢复时用会话当初那份 system prompt，会话中途新增的 skill 不影响本会话。

---

## 4d. agent/tools/plan.py（M8，计划清单落盘跨回合）

> **命名纪律**：这里的 plan 是 **agent 给自己的任务计划**，与仓库根的 `TASKS.md`
> （人类开发者的清单）**毫无关系**。文档与 CLI help 里必须区分，否则读者会以为 agent 在改 TASKS.md。

```python
class UpdatePlanInput(BaseModel):
    items: list[str]        # 完整清单（不是只传变化的那几条）；空数组 = 清空
    statuses: list[str]     # 与 items 一一对应，只能是 pending / in_progress / done
```

三条设计决定，逐条都有理由：

1. **两个平行数组，不是嵌套对象列表。** 项目硬约束是「工具参数扁平化、schema 无 `$defs`」，
   而 `base.py` 的 `raw.pop("$defs", None)` 会把嵌套模型**静默**削成坏 schema
   （模型收到的参数说明是错的，却不报错）。`list[str]` 生成内联 array，安全。
2. **全量覆盖，不是增量。** 增量要求工具自己维护"第 3 条是哪个"的对应关系，而工具的每次调用都是
   独立的 —— 模型对序号的记忆一旦与工具不一致，改的就是错的那条，**而且不报错**。
3. **不把计划快照注入每轮消息。** 参考实现在第 5 步做这件事；在我们的 cache-aware 布局下
   那是负收益：计划每变一次就改一段消息 → 那段之后的**前缀缓存全部失效**，而收益只是
   "模型多看见一遍自己刚写的东西"。可见性本来就够 —— `update_plan` 的工具结果（渲染出的清单）
   就在会话里。**只在 `--resume` 时补投一次**（那时计划很可能已被 compact 裁掉），
   用 `user` 角色并**写明来源**（不说明就当成"用户说的话"塞进去，模型会以为那是人给的指令）。

**校验失败要回喂具体差在哪**（这是我们自己的工具，不是外部 API，没理由让模型去猜）：
长度不一致报「收到 3 条文本、1 个状态」；非法状态列出合法取值；条目文本不能为空。

**接不上 `ctx.state` 时 `fail`，不静默降级**：那等于计划落不了盘，而"跨回合不丢"正是这个工具
存在的唯一理由；静默返回一份渲染好的清单会让它看起来一切正常。这是接线缺口（本项目最高发的缺陷类）。

**`--plan`**（无任务）：读最近会话检查点 → 打印清单 → 退出。计划只是检查点里的一个字段，
所以这条路径**不需要 API key、不建会话、不跑任务**（因此它在 `_build_llm` 之前处理）。
空清单不要复用 `render_plan` 的"已清空"文案 —— 那是"清空"这个动作的说法。

---

## 4e. agent/goal.py + agent/tools/goal.py（★M9-6，进程内目标 + 显式完成检查）

**文件**：`agent/goal.py`（纯 stdlib、零行为依赖）、`agent/tools/goal.py`（唯一工具）。

> ⚠️ **它不是任务树。** 全仓关于 TS 原版 ④ 的唯一证据是 `docs/reference/minicode-notes.md:141`
> 那一行，原文是「进程内 Goal（跨回合推进 + 暂停/恢复/完成检查）」+ `/goal` 命令族
> （`cli-commands.ts:25-30`）与 `goal/context.ts`。`isComplete` / `stop_condition` / 任务树 /
> `TodoWrite` **全仓零命中**。原版是一个**带状态机的目标对象**，不是树 —— 面试稿里原先那句
> 「④ Goal（目标任务树）」**没有依据，已更正**（「不允许私自造假数据」的直接要求）。
> 「允许中途改结构」这半个承诺由现有的 `update_plan`（全量覆盖）本来就兑现了。

### 4e.1 数据结构与判据

```python
GOAL_ACTIVE, GOAL_PAUSED, GOAL_DONE = "active", "paused", "done"
CHECK_PASSED, CHECK_FAILED, CHECK_INVALID = "passed", "failed", "invalid"   # 三态，见 4e.3

DEFAULT_GOAL_TURNS = 3        # 一拍自动推进最多几个回合
GOAL_CHECK_TIMEOUT = 120      # 完成检查命令的秒数上限（bash 工具自身是 300）
GOAL_CHECK_TAIL_LINES = 40    # 回喂模型的输出尾部行数（事件里也只存尾部）

@dataclass
class Goal:
    objective: str                  # 人写的目标
    check_command: str              # 人给的可执行完成判据（**唯一权威**）
    status: str = GOAL_ACTIVE
    pause_reason: str | None = None # 仅 paused 有意义；**resume 必须清掉**
    created_step: int = 0
    turns: int = 0                  # 累计自动推进过的回合数（给人看）
    declaration: dict | None = None # 瞬态：模型刚声明的完成，批前/取走时复位
    last_check: dict | None = None  # 最近判定的摘要缓存（给 /goal status）

def goal_can_advance(goal) -> bool: ...     # 判据收在一处：goal is not None and status == active
def render_goal(goal) -> str: ...           # 唯一定义"目标长什么样"（同 render_plan 的纪律）
def goal_kickoff_message(goal) -> str: ...  # 一拍第一个回合发
def goal_continuation_message(goal) -> str: # 同一拍后续回合发（短一些，判据仍重述）
def render_check_report(goal, verdict, output, reason) -> str: ...   # failed / invalid 两态
def tail_lines(text, limit=GOAL_CHECK_TAIL_LINES) -> str: ...
```

**为什么单独一个模块**：`agent/state.py` 的自我定位是「会话状态 + 消息构造」（纯数据、零行为），
而 Goal 带状态机迁移和三份给不同读者用的渲染文本。**为什么不是 `agent/tools/goal.py`**：那样
`state.py` 要 import `tools/`，与 `tools/base.py` 里「底座依赖上层是反的」相悖。本模块只 import
标准库 → 无环。

**刻意不做 `derive_goal(events)`**（对比 `taint`）：污染标记有第二个判据依赖它（权限天花板），
所以必须有重放；目标的唯一消费者是它自己和给人看的 `/goal status`。给一个没有第二个读者的
东西加重放，只会多出一份可能对不上的真相。

### 4e.2 唯一工具：`declare_goal_done`

```python
class DeclareGoalDoneInput(BaseModel):
    summary: str                                  # 一句总结（进轨迹，给人看）
    evidence: list[str] = Field(default_factory=list)   # 内联 array，**不能**用嵌套模型

class DeclareGoalDoneTool(Tool):
    name = "declare_goal_done"
    def is_read_only(cls) -> bool: return False   # 它改变控制流，必须串行
    def execute(self, args, ctx) -> ToolResult:
        # 无 state / 无目标 → fail（**模型不能自造目标**）
        # status == done   → fail（已完成，不再需要声明）
        # status == paused → fail（**暂停期间不跑检查**）
        # 正常：goal.declaration = {"summary", "evidence", "step"}；返回**普通** ToolResult
```

- **命名 `declare_goal_done` 而不是 `complete_goal`**：工具 description 是这条能力在**唯一常驻
  请求**（工具 schema）里的全部说明，而 `complete_goal` 读起来像「调用它 = 完成」—— 那正是本项
  要避免的误解。真实语义是：模型**声明**，运行时拿**人预先给定的命令**去跑，**退出码**说了算。
- **参数扁平**（`summary` + 内联 `evidence: list[str]`）：`base.py` 的 `raw.pop("$defs")` 会把嵌套
  模型**静默**削成坏 schema。真跑兑现过：模型第一次把 `evidence` 传成字符串，schema 校验直接拒掉。
- **标志走 `state.goal.declaration`，不走 `ToolResult` 上的第二个标志**：`_execute_tool_calls`
  在批结束时已经把每个 `ToolResult` `compact_batch` 成字符串了（只返回 `pending: str | None`），
  要带出第二个标志就得**改它的返回类型** —— 那是四个入口共用的核心循环契约，留给 M9-7。
  经由 `state` 传递，`_execute_tool_calls` **一个字符都没改**。
- **没有 pause / clear / status 工具**：与 `clear_taint` 的纪律一致 —— **标记不由被标记者清除**。
  理由不是"不信任模型"，是**判分权**：模型能改判据或撤销目标，那套检查就退化成自己跟自己打分。
- **刻意不进 `ToolRegistry.default()`**（按入口注册，`build_goal_tools()` 一处构造）：eval 用的正是
  `default()`，而 headless 里没有任何入口能创建目标 → 模型只会看到一个永远失败的诱饵（与
  `ask_user` 同构）。streamlit 同样不注册（它没有 `/goal` 入口）。

### 4e.3 完成检查：三态判定与回合语义

对齐 `eval/golden_tasks.py` 的 `JudgeResult.executed`：命令**压根没跑成**时，「算完成」和
「算没完成」都是错的。

| 判定 | 触发 | 目标状态 | 回喂 | 事件 | `terminated_reason` | 回合 |
|---|---|---|---|---|---|---|
| **`passed`** | exit 0 | `done` | 不喂 | `goal_check` + `goal_completed` | **`REASON_GOAL_DONE`**（新） | 结束 |
| **`failed`** | exit ≠ 0 | 保持 `active` | 判定文本 + 输出尾部 40 行（**user 消息**） | `goal_check` | 不变 | **继续** |
| **`invalid`** | 门禁拦下 / 超时 / 工具异常 | 保持 `active` | 「本次判定**不计入**」 | `goal_check` + 门禁自己的 `gate_block` | **`REASON_GOAL_CHECK_INVALID`**（新） | 结束 |

- **通过为什么不回喂、直接结束回合**：让模型看到「检查通过」再自己写结论，完成就又变成模型
  说的话了 —— 恰是本项的反面。结论由运行时给出（连同模型的声明原文，人能看到它当初声称了什么）。
- **无效为什么也结束回合**：检查命令坏了，模型**没有任何办法**修它（它不能改 `check_command`），
  留着继续只会反复声明、每次拿一条无效判定、把步数烧光。
- **无效为什么不复用 `await_user`**：那个值连带两件事、两件都不对 —— `_extract_learned` 会跳过
  约定提炼（而这里的轨迹是完整的），REPL 会打印「需要你补充信息」（而这里没有人被提问）。
- **失败为什么保持 active、本轮继续**：结束回合会让模型失去修复机会 —— 而「检查没过 → 看输出
  → 接着修」正是这条动线的全部价值。

### 4e.4 检查怎么跑（`loop._verify_goal`）

```python
call = ToolCall(id=f"goal_check@{state.step}", name="bash",
                arguments={"command": goal.check_command, "timeout": GOAL_CHECK_TIMEOUT})
before = len(state.events)
result = self._gate_and_run(call, state, ctx)          # ← 同一条钩子/权限链
gate_blocks = [e for e in state.events[before:] if e.get("type") == "gate_block"]
exit_code = result.data.get("exit_code") if isinstance(result.data, dict) else None
# gate_blocks 或 not result.success 或 exit_code is None → CHECK_INVALID
# exit_code == 0 → CHECK_PASSED；否则 CHECK_FAILED
```

- **走 `_gate_and_run` 而不是直接 `tool.run`**：检查命令是**人写的一行 shell**，必须和模型自己发的
  命令受同一套治理（PreToolUse 的 block-at-submit、权限 deny、危险命令 ask、污染天花板）。顺带换来
  一个明确判据 ——「检查被拦下了」有 `gate_block` 事件带 `source`/`reason`，而不是靠猜错误文本。代价见 S19。
- **合成的 `call.id` 只活在这一次调用里，不进任何消息**。**绝不伪造
  `assistant(tool_calls=[...]) + tool(...)` 消息对**把检查伪装成模型发起的一次调用 —— 那是在会话里
  写下一个模型从没发出过的调用，与 `PAIRING_FILLER`「必须说实话」的纪律直接冲突。判定以 **user 消息**
  回喂，同 `_reinject_plan` 的先例。
- **事件里只存输出尾部**（`output_tail`）+ `output_chars`：events 会进检查点，`--resume` 每次恢复
  都要读它，而一次 pytest 的输出可达几十万字符（bash 兜底上限 `MAX_CHARS = 500_000`）。

### 4e.5 插在 `_run_loop` 的哪一行（4 行）

```python
                # ★ 批前复位：结构性地保证「一次声明只触发一次检查」
                if state.goal is not None:
                    state.goal.declaration = None
                pending = self._execute_tool_calls(result.tool_calls, state, ctx)
                # ★ 判定在批后、checkpoint 前
                if state.goal is not None and state.goal.declaration is not None:
                    goal_end = self._verify_goal(state, task, ctx)
                    if goal_end is not None:
                        return goal_end
                if self.session is not None:
                    self.session.checkpoint(state)
```

- **批后**：「先跑测试验证、再声明完成」是模型同一步里最常见的形状；放批前只会看到上一轮的陈旧声明。
- **checkpoint 之前**：让这一步的检查点带上**判定之后**的目标状态（done / 仍 active），否则
  `--resume` 恢复出来的目标是错的。
- **批前复位**：不复位的话第 N 步声明过一次之后**后面每一步**都会重跑一次完成检查 —— 烧钱、
  上下文爆，而且**不报任何错**。`tests/test_goal.py` 有一条专门钉它。
- **插在 `_run_loop` 里的收益是四条入口一起拿到**（同 `permissions.new_turn()` 的位置理由）——
  于是 **`--resume` 一个有活跃目标的会话时，人给的检查照跑**（真跑验收过），否则目标在恢复路径上
  就是个假死状态。headless 没注册工具、也没有目标，完全 no-op。
- **同一步里既有声明又有 `ask_user` → 检查先跑**（`goal_done` 赢）：运行时的**事实**优先于模型的
  **陈述**；两个结论都进轨迹。有测试钉这条优先级。

### 4e.6 新终止原因（`agent/loop.py`）

```python
REASON_GOAL_DONE = "goal_done"
REASON_GOAL_CHECK_INVALID = "goal_check_invalid"
TERMINATED_REASONS = frozenset({
    "completed", "max_steps", "loop_detected", "error", "await_user",
    REASON_GOAL_DONE, REASON_GOAL_CHECK_INVALID,
})
```

- 前缀 `REASON_` 是**刻意**的：`goal.py` 里 `GOAL_DONE` 是**状态**（`"done"`），这里是**终止原因**
  （`"goal_done"`），两个词都在 `loop.py` 被 import —— 同名会让读代码的人以为它们是同一个东西。
- **既有五个字面量保持不动**：那是纯改名扫荡，会把"加一个功能"变成"加功能 + 重构核心循环"，
  而五个返回点正是变异体最常锚的地方。集中集合补的是真缺口（**一共有哪些值**）—— 在此之前
  "新增一个终止原因但忘了让 REPL 的自动推进在它上面停下来"不会有任何东西变红。
- `_goal_turn_result` 与 `_awaiting_user` 一样 `checkpoint(force=True)`：同一个「流程即将因**非步数**
  原因结束，节流的下一次 tick 永远等不来」的理由。

### 4e.7 `AgentState.goal` 与 `_FIELD_DECODERS`

```python
    goal: Optional["Goal"] = None     # agent/state.py（TYPE_CHECKING 导入，避免 state ↔ goal 成环）
```

> ⚠️ **加这个字段必须同时给 `agent/session.py` 的 `_FIELD_DECODERS` 补一行**
> （`"goal": lambda raw: Goal(**raw) if raw else None`）。`load_state` 走 `AgentState(**raw)`，
> 而 dataclass **不做类型检查** —— 漏了这一行，字段会是个 `dict`，直到有人读 `.status` 才抛错，
> 而那个炸点被 `_run_loop` 的 `except Exception` 吞成 `terminated_reason="error"`，**看起来像引擎出错**。
> 一条测试 + 一条变异体专门钉它。**绝不能**把目标放进 `terminated_reason` —— 那个是**每回合一份**、
> 由 `_run_loop` 入口复位（M9-5 修的正是这个），把跨回合的东西放进去等于重造那个 bug。

### 4e.8 `/goal` 命令族与自动推进（`app/repl.py`）

`_dispatch` 用 `line.partition(" ")` 切键，键只能是第一段 token → 四个子命令由**同一个 handler**
按 `rest` 解析。解析规则只有两条且必须确定：**带 `--check` 一定是设定**；**不带 `--check` 且首词是
已知子命令 → 子命令**；两者都不是 → 用法错（**只在这条命令内失败、不带走 REPL**）。**已知边界**：
目标文本里出现字面 ` --check ` 会被切开 —— **不做引号解析**，加一层"半个 shell"只会造出第二个
有歧义的解析器。

| 命令 | 行为 | 事件 |
|---|---|---|
| `/goal X --check C` | 已有活跃/暂停目标则**拒绝**（防旧目标的检查命令无声消失）；否则建目标 | `goal_created` |
| `/goal` / `/goal status` | 纯读（`render_goal`） | 无 |
| `/goal pause [原因]` | `status=paused` + 存原因；已暂停**拒绝且不覆盖**原原因 | `goal_paused`(auto=False) |
| `/goal resume` | `status=active`、**必须清 `pause_reason`**；`done` 时拒绝（完成是终态） | `goal_resumed` |
| `/goal clear` | `state.goal = None`（**不是置 done** —— 后者会在轨迹里留下一句没发生过的成功） | `goal_cleared` |

```python
def loop(self):
    while not self._done:
        if goal_can_advance(self.state.goal):
            self._run_goal_burst()                      # 一拍自动推进，然后回到提示符
            if self._done: break
        raw = input(self._prompt())
        ...
        else:
            self._pause_goal("人工回合（你敲了一行字，目标转为暂停）")   # ★ 隐式暂停
            self.run_turn(line)

BURST_STOP_REASONS = {"await_user", REASON_GOAL_CHECK_INVALID, REASON_GOAL_DONE,
                      "max_steps", "loop_detected", "error"}    # ← "completed" 刻意不在
```

- **`_run_goal_burst` 结尾一定 `_pause_goal(stop)`**（仅在 `goal_can_advance` 为 False 时提前返回）：
  不留着 active 回提示符，否则循环顶部会立刻再起一拍（按一次回车、敲一条 `/help` 都会重新触发），
  那就等于无界。**停下来这个动作让「一拍 = 一次授权」在结构上成立。**
- **上限不是优化，是防锁死**：`input()` 是阻塞的、没有定时器，一拍期间人**根本敲不进字**，无界推进
  = 把人锁在门外直到烧完额度。所以 `/goal` 的输出里**把授权额度打给人看**（「最多 3 回合 × 每回合
  25 步 = 75 步」）。`--goal-turns` **刻意不进检查点** —— 它是交互策略，不是会话事实（对比
  `checkpoint_every`：那个恢复时必须沿用）。`Repl.__init__` 做 `max(1, goal_turns)`。
- **`"completed"` 刻意不在 `BURST_STOP_REASONS`**：一个回合"正常跑完"（模型给了文字、不再调工具）
  恰恰是自动推进**要继续**的情形 —— 目标的完成与否由人给的检查命令说了算，不由模型停不停下来说了算。
  这份集合必须覆盖 `TERMINATED_REASONS` 里除 `completed` 之外的全部取值，有测试钉着。
- **`_STOP_REASONS` 每个原因都要有话说**：暂停是自动推进的唯一出口，理由说不清的话人只会看到
  「它自己停了」。
- **「人敲一行字」= 隐式暂停，这不是偏好而是结构性事实**：人只能在一拍停下时才拿到提示符；那一行
  若不暂停，回合结束后循环立刻又起一拍，**人再也敲不进第二行**。选暂停还因为它是可逆的那一侧
  （`/goal resume` 一句话恢复），且它**不静默**（把原因与恢复方式打出来）。
- **暂停是数据不是控制流**（同 `await_user` 的纪律）：`_pause_goal` 只改 `status` + 记一条事件，
  既不 `return` 也不 `raise`。
- **提示符状态位 `goal:<status>`** 只在**有目标**时显示（同 taint 只在非 `none` 时显示的处理：
  恒显一个值会让人不再看它）。状态值本身要露出来 —— `active` 与 `paused` 下面会发生完全不一样的事。
- **判定无效单独一种打印样式**：它既不是成功也不是失败，却**最容易被人当成跑完了**（没有报错、
  退出码 0、还有一段像结论的话）。判据原文永远印在判定结果旁边（S17 唯一的缓解手段）。

### 4e.9 目标不进 system prompt（与 `plan` 同一纪律）

四条理由逐条同 §4d 第 3 条：① `system` 是 `messages[0]`、在 `_PREFIX_LEN` 保护区内，变一次 = 整条
前缀缓存永久失效；② 目标是会话中途创建的，注入就得**回改第 0 条**；③ `state.system_prompt` 渲染
一次就进检查点，`--resume` 必须拿到当初那份；④ **可见性本来就够且更好** —— 创建那一刻的 kickoff
消息是一条真实的 `user` 消息，判定与续跑文本都在会话尾部。

**这是 M8 那个 elicitation gap 的正解形状**：引导必须出现在**目标被创建的那一刻**，那里恰好有一个
天然的用户回合。**刻意不给 `DEFAULT_SYSTEM_PROMPT` 加 `{goal_hint}` 槽位**：加了就得按"注册了哪些
工具"填（同 `_ASK_USER_HINT`），而目标工具在 CLI 两个入口都注册 → **每个单发会话**都要为"一个不存在
的目标"付 token，且模型会去调一个注定失败的声明工具。

`--resume` 时 `_reinject_goal(state)` 补投一次（与 `_reinject_plan` 同处、同形状、同理由：目标很可能
已被 compact 裁掉）。**只补一次、不每轮贴** —— 每轮贴等于把"目标有没有变"变成"消息有没有变"。

### 4e.10 `--goal`（只读）与不做的事

- **`--goal` 与 `--plan` 完全同构**：读检查点里的一个字段就该够 → 不建会话、不要 key、不跑任务，
  排在 `_build_llm` 之前（测试用"一调用就炸"的 `_build_llm` 替身钉住）。没有这条出口，REPL 里设的
  目标在进程外**没有任何消费者**（本项目明确防这个）。输出**从事件里取**（`goal_check` 的
  `output_tail`），不从 `last_check` 取 —— 后者只是给 `/goal status` 的摘要，存第二份就是第二个真相源。
- **不做 `--goal/--check` 单发目标生命周期**：目标是"进程内跨回合"的能力，单发里连"下一个回合"
  都不存在。但**完成检查天然生效**（它长在 `_run_loop` 里），这是白拿的、也是必须的。

### 4e.11 如实标注（README S17–S20）

| 编号 | 局限 |
|---|---|
| **S17** | **空转的检查命令**：判据是人给的，运行时只看退出码、**不评价它检查了什么**（`--check "true"` 永远通过）。缓解只有**可见性**（判定文案永远把命令原文印在结果旁边），**没有任何机制阻止** |
| **S18** | **声明不是闸门，只是事后核验**：模型可以完全不声明就把活干完（目标停在 active、多花回合），也可以在毫无证据时声明（靠检查兜底）。防的是"谎报完成"，不防"判断失误" |
| **S19** | **检查的副作用能替 agent 打开一道治理闸门**：检查走 `_gate_and_run` → PostToolUse hooks 生效 → 一条成功的 `pytest` 会写 `data/tests_pass.marker`，而它正是解锁 `git commit` 的那道门。轨迹上与模型自己跑同一条命令**不可区分** —— 复用同一条链的代价，不是疏漏 |
| **S20** | **「判定无效」比 eval 弱一层**：eval 有 pytest 的退出码 2/3/4/5 认得出"压根没跑成"，**shell 没有这个信号** —— `exit 127`、`No module named pytest` 都会被记成**未通过**。只有门禁拦下 / 超时 / 工具异常才算无效 |

另记：`/fork` 会连目标一起继承而检查跑在**当前**工作区上（单独 `--fork` 时文件不回滚 →
可能白捡一个通过；`--fork --step K --rewind` 时文件也回到第 K 步，这条就不成立了 —— 见 §9.13）；
通过即结束回合，模型没有机会补充「还有一件次要的事没做」（这是把判分权拿走的**代价**）；检查超时
120s 是常量、没有旋钮；`state.task` 会被续跑文本覆写（目标本身才是权威）。

### 4e.12 测试与真实验证

- `tests/test_goal.py` **38 例** + `tests/test_repl.py` / `tests/test_session.py` 补契约；全量 **597 全绿**（M9-6 当时的数字；M9-7 之后为 623、M9-8 之后为 707、M5-5 之后为 **769**）。
  重点用例：创建时 `objective`/`check_command` **逐字落库**、`pause` 存原因 / `resume` **必须清
  `pause_reason`**、`clear` → `None`（杀"实现成置 done"）、已有活跃目标时拒绝第二个、检查跑的是
  `check_command` 原文、exit≠0 **保持 active 且本轮继续**、失败判定以 `user` 消息回喂、**门禁拦下 →
  `invalid` 而非 `failed`**（且不记 `goal_completed`）、一次声明**恰好**跑一次检查、输出截到最后 40 行、
  schema 扁平无 `$defs`、一拍自动跑多个回合、**`dump_state` → `load_state` 回来是 `Goal` 实例不是 `dict`**、
  暂停跨进程往返后仍门住一拍、`--goal` 不碰 LLM（`_build_llm` 换成 boom）、`/help` 键集合恰好等于
  `_COMMANDS`（M9-6 时计数 10，M9-8 加 `/rewind` 后为 11）、真 `BashTool` + 真 permissions 下检查**真的**过门禁链（`gate_block` 带
  `source`/`reason`）。
- 变异测试 **20/20 被抓住**（`m9verify/mutate_m9_6.py`）。**一个如实标注的 MISS**：「人工回合不暂停」
  实证 MISS，原因是**结构性的** —— `loop()` 进提示符前必先跑一拍 + `_run_goal_burst` 结尾必暂停 →
  提示符处目标绝不为 active，那句 `_pause_goal` 命中的永远是拒绝分支。这是「一拍 = 一次授权」
  不变式的推论，与动线 ④ 在纯 stdin 流程里不可观测**同源**。
- **真实 LLM 端到端五条动线全部有日志**（DeepSeek 官方通路，真工作区 `m9verify/ws_m96_a|b|c|d|e/`，
  真 pytest 真跑；`goal_a|b|b2|c|d|e.log` + 驱动脚本 `drive_m9_6.py` 的三个 case）。逐条证据与数字见
  `TASKS.md` 的 M9-6 条目。**这五条的数字与既有单发/REPL 数字不可比** —— 自动续跑会把 `max_steps`
  乘上 `goal_turns`。

---

## 5. agent/state.py（★M1 完成）

**文件**：`agent/state.py`

### 5.1 消息构造（OpenAI 格式 dict）

```python
def system(content: str) -> dict                     # {"role":"system","content":...}
def user(content: str) -> dict                       # {"role":"user","content":...}
def assistant_text(content: str) -> dict             # {"role":"assistant","content":...}
def assistant_tool_calls(calls: list[ToolCall]) -> dict
    # {"role":"assistant","content":None,
    #  "tool_calls":[{"id","type":"function","function":{"name","arguments": json.dumps}}]}
def tool_result(tool_call_id: str, content: str) -> dict   # {"role":"tool","tool_call_id":...,"content":...}
def text_of(message: dict) -> str   # 提取 content（兼容 content 为 str/None/列表）
```

### 5.2 AgentState

```python
@dataclass
class AgentState:
    session_id: str
    task: str                        # 用户任务原文
    system_prompt: str               # 拼装后的 system prompt（含记忆/工具说明/plan 模板）
    messages: list[dict]             # OpenAI 格式消息历史（含工具结果）
    step: int = 0
    usage: Usage = field(default_factory=Usage)          # 累计
    events: list[dict] = field(default_factory=list)     # 轨迹事件
    terminated_reason: str | None = None
    memory_blocks: list[str] = field(default_factory=list)  # 注入的 repo 记忆段落
    plan: list[dict] = field(default_factory=list)   # M8 [{"text": str, "status": str}]
    goal: Optional["Goal"] = None                    # ★M9-6 进程内目标（见 §4e；TYPE_CHECKING 导入）
    last_usage: Usage | None = None                  # M3 provider 锚点（compact 后置 stale）
    usage_stale_reason: str | None = None            # "tool_output_truncated"|"snip_compact"|"llm_compact"
    taint: str = TAINT_NONE                          # M7 会话级污染标记（只升不降）
    emitter: Callable[[dict], None] | None = None     # 运行时回调，**不进检查点**
    # 方法:
    #   record_event(type: str, **data)  → 加 ts/step 后 append，若设了 emitter 则回调
    #   raise_taint(level) / clear_taint(reason)      → 只升不降；复位只由人的动作调用
```

> `plan` **不是**从事件派生的值（对比 `taint`：那个由轨迹里的 `security_finding` 重放得出），
> 它就是权威状态本身 —— 所以没有对应的 `derive_plan`。`goal`（★M9-6）同样如此：它是权威状态
> 本身、随检查点走，**也没有** `derive_goal`（见 §4e.1 末：给一个没有第二个读者的东西加重放，
> 只会多出一份可能对不上的真相）。检查点是按 `dataclasses.fields()`
> **全字段**快照的，加字段自动进检查点；`tests/test_session.py` 有一条从 `fields()` 反推
> 字段全集的往返测试，**加字段时它会失败**，提醒你把新字段填进测试的 `values`。
> `emitter` 是唯一的例外（运行时对象，故意不落盘）。
> ⚠️ **加字段时还有一处必改**：若它不是 JSON 原生类型，必须同时给 `agent/session.py` 的
> `_FIELD_DECODERS` 补一行 —— `load_state` 走 `AgentState(**raw)` 而 dataclass **不做类型检查**，
> 漏了的话字段会是个 `dict`，直到有人读它的属性才炸，而那个炸点被 `_run_loop` 的
> `except Exception` 吞成 `terminated_reason="error"`，**看起来像引擎出错**（见 §9.3）。

### 5.3 边界

- tool_calls 的 arguments 在 `assistant_tool_calls` 里统一 `json.dumps(ensure_ascii=False)`。
- content=None 是合法 assistant tool-use 消息（OpenAI 要求）。

#### `ensure_tool_pairing(messages) -> int`（★M9-5）

补上**孤儿 `tool_call` 的应答消息**，返回补了几条。复用已有的 `assistant_tool_calls` /
`tool_result` 两个构造函数，不新造消息形状。

**为什么需要它**：回合中途 Ctrl+C，`state.messages` 可能停在
`assistant(tool_calls=[3 个])` + 只有 1 条 tool 结果 —— 下一轮请求直接 400。
单发进程里这一刀下去进程就死了、靠检查点恢复，所以从没暴露过；REPL 要接着用
同一个 state 就必须补上。**而报错发生在下一回合**，与那次 Ctrl+C 看起来毫无关系 ——
这是它值得单独成一个函数的原因。

三条性质（各有纯函数用例）：**幂等**（补过的不会被补第二遍）、**位置正确**（补的
结果消息紧跟已有结果，不破坏"assistant tool_calls 后跟 N 条 tool 结果、顺序与 calls
对应"这条 OpenAI 硬性要求）、**以普通文本收尾时不误判**（不能把"模型这轮只想说话"
当成残缺）。调用方 `run_turn` 补了几条就记一条 `pairing_repaired` 事件 —— **不静默修**。

---

## 6. agent/context.py（◐M1 占位 → M3 补全，★ 差异化 + MiniCode 吸收）

**文件**：`agent/context.py`（M1 已建文件，功能 M3 实现）

### 6.1 类与数据结构

```python
@dataclass
class ContextStats:
    total_tokens: int
    provider_usage_tokens: int      # 最近一次 provider usage 的 total
    estimated_tokens: int           # usage 之后尾部消息的估算
    utilization: float              # total / budget
    warning_level: str              # "normal"(<50%) | "warning"(≥50%) | "critical"(≥85%) | "blocked"(≥95%)

class ContextManager:
    def __init__(self, llm: BaseLLM, *, token_budget: int = 64000,
                 snip_threshold: float = 0.70, compact_threshold: float = 0.85,
                 keep_recent: int = 12, min_keep: int = 6, snip_target: float = 0.60):
        ...
    def prepare(self, state: AgentState, tool_schemas: list[dict] | None = None) -> list[dict]:
        # M3 流水线（按序）：记账(6.2) → 分级 compact(6.3) → 返回消息（前缀稳定，6.4）
    # 内部：
    def _snip(self, state, messages) -> bool           # 确定性裁剪（无 LLM）
    def _compact_with_summary(self, state, messages) -> bool  # LLM 摘要（失败退化 snip）
    def _find_cut(self, messages) -> int | None        # 轮次边界对齐的保留窗口起点
    @staticmethod _is_round_boundary(messages, idx) -> bool
```

**实现状态**：M3-1（记账+布局）/ M3-3（compact 流水线）已完成并测试。

### 6.2 provider-usage-first token 记账（MiniCode token-estimator 移植）

- **思路**：assistant 消息后附带本次调用的 `Usage`（在 loop 里 `withProviderUsage` 写入消息）；统计时**从尾部找最近一条有效 usage**，`total = usage.total_tokens + estimate(尾部新增消息)` —— 不需要每步都重算全量。
- 消息级估算：`estimate_tokens(message)`，按角色给 字符/token 比例（system 3.5 / user 3.0 / assistant 3.5 / tool 2.0）。
- 分级告警：utilization = total/budget；`normal <0.5 ≤ warning <0.85 ≤ critical <0.95 ≤ blocked`。
- **compact 后**：保留消息的 usage 标记 stale（state 里记 `usage_stale_reason`），避免旧 usage 计新上下文。

### 6.3 compact 流水线（三级：分级截断 → 确定性 snip → LLM 摘要）

`prepare()` 实测触发逻辑：
```python
self.last_stats = self.account(state)
if self.last_stats.utilization >= self.snip_threshold:      # 第 0 级：只在 0.70 之上才跑
    freed = self._truncate_oversized(state)
    if freed:
        self.last_stats = self.account(state)
        if self.last_stats.utilization < self.snip_threshold:
            state.usage_stale_reason = "tool_output_truncated"
            return state.messages                            # 缩够了就不删
if self.last_stats.utilization >= self.compact_threshold:   # ≥0.85 critical/blocked
    self._compact_with_summary(state, state.messages)       # LLM 摘要，失败退化 snip
elif self.last_stats.utilization >= self.snip_threshold:    # 0.70~0.85 warning
    self._snip(state, state.messages)                       # 确定性裁剪（无 LLM，成本≈0）
return state.messages
```

0. **分级截断**（M8 第 0 级，MiniCode 工具输出分级截断移植 —— 详见 §6.3b）：**先缩内容，缩不够再删**。
   它比 snip 便宜（不删消息 → `tool_call_id` 配对天然完整），所以排在最前。
1. **确定性 snip compact**（warning 带，无 LLM）：`_find_cut` 从尾部往回保留最近
   keep_recent=12 条（至少 min_keep=6）作"保留窗口"，窗口之前的中段删除。
   - 窗口起点用 `_is_round_boundary(messages, idx)` 对齐：切点必须落在完整 API 轮次
     边界（assistant tool_calls + 其 tool 结果整组），绝不切开工具组（孤儿 tool result
     会破坏 OpenAI 消息格式）。
   - 删除处插入 `SNIP_BOUNDARY_MARKER = "[已裁剪的历史消息，见会话轨迹 JSONL]"`。
   - 目标：裁剪后 utilization ≤ snip_target(0.60)。
2. **LLM 摘要 compact**（critical/blocked ≥0.85）：`llm.complete(build_compact_summary_prompt(text))`
   把最早的中段对话压成 `context_summary` 消息（保留：任务目标、关键决策、错误与修复、未完成任务）。
   - 插 `SUMMARY_MARKER = "[历史对话已摘要，完整轨迹见会话 JSONL]"`；稳定前缀
     （`_PREFIX_LEN = 2`：system + task）永远保留。
   - **LLM 摘要失败 → `except Exception: return self._snip(...)` 兜底**（绝不让 compact 因摘要失败而跳过）。
   - 三种 compact 都会把保留消息的 usage 标记 stale：`state.usage_stale_reason = "tool_output_truncated" | "snip_compact" | "llm_compact"`。

### 6.3b 分级截断（M8，MiniCode 工具输出分级截断移植）

`_truncate_oversized(state) -> int`（返回释放的估算 token）。八条约束，每条都对应一个具体的坏结果：

| 约束 | 违反之后会怎样 |
|---|---|
| **只在 `utilization >= snip_threshold`(0.70) 时才跑** | 每个大工具结果在其生命周期内至少破坏一次前缀缓存 → 缓存命中曲线走平。**破坏是静默的**：没有报错，只有更贵的账单。这是**不变量，不是优化开关** |
| **原地改 `tool` 消息的 content，不删消息** | 删消息会造出 `tool_call_id` 孤儿边界，正是 `_find_cut` 要处理的那两类问题 |
| **幂等稳定**（改过的内容留在 `state.messages`，同会话内不再改） | 同一段内容被反复截断，越截越短 |
| **只动 `messages[min_keep : len - keep_recent]`** | 模型正在用的结果被缩掉 |
| **跳过占位文本**：含 `<persisted-output>` + `[... 省略` 的不缩 | 那是**路径指针**，缩了就毁掉回读能力 = 废掉 `tool_result.py` 的全部意义 |
| **按工具给预算**：`read` 4,000 / `grep` 3,000 / `glob` 2,000 / `bash` 6,000 / default 4,000；**失败一律 16,000** | 错误原文是模型自修复的唯一依据（「失败一律文本回喂」原则）。工具名从 `tool_call_id` 反查前一条 assistant 的 `tool_calls` 得到 |
| **head 70% + tail 30%**，不是只留头部 | `grep`/`pytest` 的**结论在尾部**（失败摘要、总计行）——只留头等于把最该看的部分丢掉 |
| **置 `usage_stale_reason = "tool_output_truncated"`** | 内容变了，旧 provider 锚点度量的已不是当前上下文（模块 docstring 写过的经典错误） |

截断处插 `TRUNCATED_MARK = "[... 省略 {n} 字符 ...]"`。

**测试必须包含这条不变量**：`utilization < 0.70` 时消息**逐字节不变** —— 它是唯一能挡住
"顺手把分级截断改成每步都跑"的测试，因为那个改动不会让任何别的东西变红。

**实测（2026-09-11，DeepSeek 官方通路，脚本与原始输出在 `m8verify/`，已 gitignore）**：

| 测法 | 结果 |
|---|---|
| 端到端 A/B（两臂只差本开关；budget 64,000/13×10KB 与 34,000/18×6KB 两个区间） | 累计命中率 B−A = **+0.003% / −0.014%**；区间①两臂累计 hit token **逐 token 相同**（319,616）。**命中率没有下降** |
| 单次截断微观对照（真实检查点做 base，每个变体只发一次） | 省 5,128 token ↔ 多付 **45,304 miss token（8.8 倍）**，该步命中率 100% → 28%；**一次性**（重发同一份 messages，miss 从 45,442 回到 134） |
| 代价的形状 | **后缀失效**：前缀缓存匹配到 `_head_tail` 保留的头部 70% 处才分叉，被改的那条之后全算未命中。同样截 4,000 字符，第 7 条（后面 12 条）少命中 50,432，第 17 条（后面 2 条）只少 13,312，**差 3.8 倍** |
| 能否免掉第 2 级 | **不能**。中段窗口要到 20 条消息才非空，那时 utilization 已 1.06~1.23；一次释放值预算的 5.9~7.4 个百分点，每步增量 11~13 个百分点 |
| 真实小仓库输出规模下 | 第 0 级被调用 8~13 次、**一次可截的都没有**（工具输出 160~430 字符 vs `read` 预算 4,000 字符）。三级时代的两级触发记录**原样复现** |
| 唯一稳定为正的收益 | **尾部结论行 8/8 次保住** |

> 结论：**上面那条不变量该守，但这条机制的收益比设计预期小得多，代价只是被同一步触发的
> LLM 摘要遮蔽了**。**去留已拍板（2026-09-11）：保持现状** —— 不选"优先截最靠后的合格
> 消息"（缓存代价降到 1/3.8）是因为它会先丢掉最老的上下文，而"最近的最相关"是比缓存
> 算术更硬的约束；不选去掉是因为"尾部结论行 8/8 保住"这条收益实测为正。完整数据在
> `TASKS.md` 的 P7-d 一节。

### 6.4 cache-aware 消息布局（我们的独家，叠加在其上）

- **目标**：最大化 DeepSeek 磁盘缓存命中 → 稳定前缀（system + 全部工具 schema + 记忆块）永远在最前且不变。
- **实现**：`prepare()` 返回的消息顺序 = `[稳定前缀, ...对话消息]`，稳定前缀 `_PREFIX_LEN = 2`
  （system + task）；**不允许** compact/插入把稳定前缀顺序打乱。
- 命中度量：每次 llm 调用后把 `usage.prompt_cache_hit_tokens / prompt_cache_miss_tokens` 记入事件，控制台画"命中率×步骤"曲线 + 省钱估算（省省钱 = 命中 token × 单价差）。

### 6.5 测试（tests/test_context.py，M3）

- 记账：mock messages → total 精确值 + 尾部估算正确；warning 分级正确
- snip：>70% 触发、保留最近 N 条、boundary 对齐不切工具组、裁剪后 ≤60%
- LLM compact：critical 触发、stale usage 标记、system 保留
- cache-aware：稳定前缀顺序不被 compact 破坏
- **M3-3 追加**：warning 带(0.70≤u<0.85) 只 snip 不触发 LLM；critical(≥0.85) 触发 LLM 摘要；
  LLM 摘要抛异常 → 退化 snip（不失败不卡死）；`_is_round_boundary` 拒绝切开
  assistant(tool_calls)+tool 组；`_PREFIX_LEN` 前缀在 snip/摘要后保持；compact 后
  `usage_stale_reason` 正确设置

---

## 6b. agent/tool_result.py（M3，MiniCode tool-result-storage 移植）

**文件**：`agent/tool_result.py`

### 6b.1 设计

- **问题**：M1 的 `_truncate` 截断丢弃了超出 MAX_CHARS 的数据（如 grep 100 条、大文件 read），模型拿不到完整信息。
- **方案**：超大工具输出**落盘**到 `data/tool-results/{session_id}/{tool_call_id}.txt`，上下文里替换为：
  ```
  <persisted-output>
  Output too large (N chars). Full output saved to: data/tool-results/{session}/{id}.txt
  Preview (first 2000 chars):
  <预览>
  </persisted-output>
  ```
- 阈值：单条 >50K 字符落盘；**批内预算** 200K/轮（即使单条没超，批总量超限按最大优先落盘）。
- 同一次运行内替换复用（`replacements: dict[id, str]`），不重复写盘。
- **接入点**：loop 的 `_execute_tool_calls` 执行结果后调用 `compact_batch(results, store) -> [(id, 替换文本)]`，再拼 tool_result 消息；M1 的 `_truncate` 保留给 read/edit 的 diff 展示（可读性），工具结果走落盘。
- **force 路径（批内预算兜底）**：`persist(tool_call_id, text, *, force=False)` —— 单条 ≤ PERSIST_THRESHOLD 且非 force → 原样返回（`PersistResult(text, False, None)`）；超过阈值或 force=True → 落盘 + `<persisted-output>` 替换。`compact_batch` 第一轮单条超限落盘；第二轮把批总量压到 BATCH_BUDGET=200K 以内，按"最大优先"对余量 `persist(id, text, force=True)`（force：单条未超阈值也落盘），直到 ≤ 预算。替换文本 header **如实区分原因**（用户红线：不造假）：`"Output too large (N chars)"` vs `"Output batch-compacted (N chars)"`。
- `_written: dict[str, tuple[Path, str]]`（tool_call_id → 落盘路径+已写内容）：同 id 同内容不重复写盘；同 id 内容变化才重写文件（MockLLM 固定 call_0001 跨轮复用时保证文件与预览一致）。

### 6b.2 测试（tests/test_tool_result.py）

- 大输出落盘：内容写入正确路径，替换文本含预览+路径
- 小输出不动；批内预算触发时最大优先落盘（force 路径：≤50K 单条被批预算强制落盘）
- 同 id 二次出现复用替换不重复写盘
- session_id 隔离目录

---

## 7. agent/loop.py（★M1 完成 → M1-9 补空响应恢复）

**文件**：`agent/loop.py`

### 7.1 QueryEngine

```python
@dataclass
class RunResult:
    final_text: str | None
    steps: int
    usage: Usage
    events: list[dict]
    terminated_reason: str          # 取值见 loop.TERMINATED_REASONS（"completed" | "max_steps" |
                                    # "loop_detected" | "error" | "await_user" |
                                    # ★M9-6 新增 "goal_done" | "goal_check_invalid"）
    task: str

REASON_GOAL_DONE = "goal_done"                   # ★M9-6
REASON_GOAL_CHECK_INVALID = "goal_check_invalid" # ★M9-6
TERMINATED_REASONS = frozenset({...})            # ★M9-6 集中列举，见 §4e.6

class QueryEngine:
    def __init__(self, llm: BaseLLM, registry: ToolRegistry, *,
                 workspace_root: Path,
                 system_prompt: str | None = None,     # None → 内置默认
                 max_steps: int = 25,
                 loop_detection_window: int = 4,       # 最近 N 步工具签名相同即停
                 memory_blocks: list[str] | None = None,   # M4 注入
                 context: ContextManager | None = None,
                 session: object | None = None):       # M3 接 session（轨迹/检查点）

    # ---- 单发入口（M1 起）----
    def new_state(self, task: str) -> AgentState:
        # 建一份全新 state + 拼装 system prompt（见 7.2）。
        # ★ M9-5 抽出来的：原先这段写在 run() 里、state 建在本地就丢掉，
        #   于是 REPL 要跨回合持有 state 就得把「prompt 渲染 + 记忆块拼装 +
        #   session_id 归属」再写一遍 —— 就是「两处各写一遍 → 漂移」。
    def run(self, task: str, *, cwd: Path | None = None) -> RunResult:
        # = new_state(task) + run_from(state, ..., push_user=True)，两行。
        #   （M9-5 之后 run() 自己不再有任何循环逻辑）

    # ---- 多回合入口（★M9-5）----
    def run_turn(self, state: AgentState, text: str, *, cwd: Path | None = None) -> RunResult:
        """一个用户回合 = 常驻模式（app/repl.py）的一行输入。顺序固定不可调换：
             1) state.ensure_tool_pairing() → 补上被中断打断的 tool 结果配对，
                补了几条就记一条 pairing_repaired 事件（**不静默修**）
             2) state.terminated_reason = None        # 上一回合的值不许跨回合
             3) permissions.new_turn()                # 见 §9.1：本回合记忆作废
             4) state.task = text；messages.append(user(text))
             5) _run_loop(..., budget_start=state.step)   # ★ 本轮一份步数预算
        user 消息由 run_turn 追加、不由调用方追加：--resume 那条路径是 CLI 自己
        append 的，两处各写一遍的话，漏了就是「模型收到一个没有提问方的回合」。
        """

    def _run_loop(self, state, task, cwd, *, budget_start: int | None = None):
        if budget_start is None:
            budget_start = state.step
        # while state.step - budget_start < self.max_steps:   ← ★M9-5 行为变更
        #   messages = self.context.prepare(state)（M1=原样）
        #   result = llm.chat(messages, registry.schemas())
        #   state.usage += result.usage; state.step += 1
        #   state.record_event("llm_call", tool_calls=[...], usage=..., step=state.step)
        #   if result.tool_calls:
        #      loop 检测（7.3）
        #      ★ M9-6 批前复位：if state.goal is not None: state.goal.declaration = None
        #      execute_tool_calls(result.tool_calls, state, ctx)
        #      ★ M9-6 批后判定：声明存在 → _verify_goal(...) 返回非 None 就 return（见 7.7）
        #      continue
        #   else:
        #      state.terminated_reason = "completed"
        #      return RunResult(final_text=result.content, ...)
        # 3) 循环外：terminated_reason = "max_steps"（或 loop_detected）
        #     final_text = "已达到最大步数 / 检测到重复循环，任务中止"

        # ★ M9-5：max_steps 的语义从「整个 state 的累计步数上限」改成
        #   **「本次运行/本回合的步数预算」**。state.step 本身照样累计不重置
        #   （检查点文件名 step-N.json、--fork --step K、轨迹的 step 字段都
        #   依赖它单调递增），只改预算的**度量起点**。
        #   四个入口（run / run_from / run_turn / 控制台）由此统一拿到
        #   「本次运行有多少步」。顺带修掉一个陷阱：会话跑满 max_steps 之后
        #   `--resume` 在旧语义下**一次模型调用都不发**、直接又打印「已达到
        #   最大步数（请拆分子任务）」，而错误信息指的方向还是错的。

    def _gate_and_run(self, call, state, ctx) -> ToolResult:
        # 门禁链：hooks.run_pre（可阻断）→ permissions.check → tool.run → hooks.run_post
        # 每一处「阻断」都走 _gate_block()：先记事件再返回失败
        #   state.record_event("gate_block", tool, source, reason)
        #   source ∈ {"registry"(未知工具), "hooks", "permissions"（含第三方未授权）}
        # 原先这几处只 return ToolResult.fail(...)：轨迹里只剩 success=False，
        # 事后翻轨迹分不清是权限拒绝、hook 阻断、工具自己报错还是模型编了个工具名。
        # gate_block 让「拒绝的原因 + 是哪一层拒的」第一次在轨迹里可见。

    def _execute_tool_calls(self, calls: list[ToolCall], state: AgentState, ctx: ToolContext):
        # ★ 只读并发：若 calls 全部是只读工具 → ThreadPoolExecutor(max_workers=min(4,len)) 并发
        #   否则串行（保持顺序）
        # 每条：result = self._gate_and_run(call, state, ctx)
        #       state.record_event("tool_call", name, arguments, success, duration_ms, exit_code)
        # 结果消息：assistant_tool_calls(calls) 一条 + 每个 tool_result(call.id, result.output) 一条（顺序与 calls 对应）
        # ★ M9-6 一个字符都没改它 —— 完成声明经由 state.goal.declaration 传递，见 §4e.2

    # ---- 目标（★M9-6，实现细节见 §4e.4 / §4e.3）----
    def _verify_goal(self, state, task, ctx) -> RunResult | None:
        # 跑一次**人预先给定的**完成检查命令（合成 bash 调用 → _gate_and_run）。
        # 返回 None = 未通过（回喂 user 消息、本轮继续）；否则返回已带
        # REASON_GOAL_DONE / REASON_GOAL_CHECK_INVALID 的 RunResult。
    def _goal_turn_result(self, state, task, reason, final_text) -> RunResult:
        # checkpoint(force=True)，理由同 _awaiting_user（非步数原因退出）
```

### 7.2 默认 system prompt 模板（坍缩防护 + plan 少而精）

```python
DEFAULT_SYSTEM_PROMPT = f"""\
你是 CodeAgent，一个在代码仓库内工作的 AI 编程代理。你的目标是高效、正确地完成用户任务。

工作方式：
- 先用工具探索（glob/grep/read）理解代码，再动手修改；不要臆测文件内容。
- 修改代码前，如果任务复杂，先用 3~6 步的简短计划（仅步骤与验证方式，不要冗长）。
- 小改动用 edit（精确匹配），大改动用 write；完成后运行测试验证。
- 每条消息最多做必要的工具调用；工具失败时根据错误信息自行修复后重试。

硬性约束：
- 只能在工作目录（沙箱）内操作，禁止访问沙箱外路径。
- 禁止执行危险命令（rm -rf、git push 等被工具拒绝）。
- 完成任务后：输出最终结论（做了什么、验证结果如何）。
- 不要假装完成：必须用测试/命令实际验证，验证失败要报告。
- 不要无意义重复同一操作；若连续多次得到相同失败，停下来向用户说明。

{repo_memory_block}   # M4：注入 repo 记忆；M1 为空
"""
```

### 7.3 循环检测（坍缩防护）

- 维护 deque(maxlen=loop_detection_window)，记录每次 turn 的所有 `call.signature()`。
- 若窗口内所有 turn 的工具调用集合完全相同 → `terminated_reason = "loop_detected"`，中止。

### 7.4 边界与坑

- **并发只读**：`ThreadPoolExecutor`；工具必须线程安全（files/glob/grep 只用局部变量，OK）。
- **消息顺序**：assistant tool_calls 消息后跟 N 条 tool 结果消息，顺序与 calls 一致（OpenAI 硬性要求 id 对应）。
- max_steps 到 → final_text 说明原因（不是假装成功）。
- 并发结果也要按 calls 顺序回填 tool 结果消息（concurrent.futures as_completed 不可用，需 map 保序）。
- **空响应恢复（M1-9）**：模型返回空/纯空白文本且无工具调用 → 若 `empty_retry_count < empty_response_retries(2)`，push continuation prompt（"上次返回为空，继续完成下一步或给出最终结论"）重试；达到上限才按 completed(空) 结束。
- **超大工具结果**：M3 接入 `agent/tool_result.py`，执行结果落盘替换（替代截断）。

### 7.5 测试（tests/test_loop.py）

- test_simple_answer：MockLLM.text → terminated_reason="completed", steps=1, final_text 匹配
- test_tool_then_answer：MockLLM.script(工具 read → 文本) → 走过工具调用，消息历史含 tool 结果
- test_unknown_tool_recovered：模型先调未知工具 → 收到"未知工具"fail → 再给文本答案 → 完成（验证错误回喂）
- test_read_only_concurrent：一次调用含 2 个只读工具 → 两者都执行（记录并发；用记录完成顺序即可，不强断言线程）
- test_max_steps：MockLLM 一直调工具 → 到 max_steps 终止，reason=max_steps
- test_loop_detected：MockLLM 连续返回相同工具调用 → reason=loop_detected
- test_e2e_real_files：MockLLM 脚本化（glob → read → edit → 文本）在 tmp_path 真实文件上跑通（验证工具链真实可用）

### 7.6 多回合契约（★M9-5）

常驻 REPL 让「同一个 `AgentState` 连跑两次」第一次成为一条真实路径。下面五条是
**单发进程里结构上不可观测**的契约，各有测试钉着：

| 契约 | 单发时为什么看不见 | 违反后的表现 |
|---|---|---|
| `run_turn` 复位 `terminated_reason` | "一个回合"和"一个进程"是同一件事，从没有过清空动作 | 第二回合全程带着上一回合的旧值；控制台从检查点 payload 里读出来显示 |
| `run_turn` 调 `permissions.new_turn()` | 同上（`_turn` 会随进程一起消失） | `allow_turn` 变成**永久放行**，与确认框写的「2) 本回合允许」矛盾 |
| `record_discovery` 认 `state.events` 去重 | 只有一轮，重放一次看不出来 | `skill_discovery` / `skill_shadowed` 每回合往轨迹里再写一份 |
| 入口 `ensure_tool_pairing` | 回合中途 Ctrl+C 后进程就死了，靠检查点恢复 | 下一轮请求是 400 形状，而**报错发生在下一回合**，与那次 Ctrl+C 看起来无关 |
| 步数预算是**每轮一份** | 一个进程只有一个回合，累计与每轮等价 | 第二回合一次模型调用都不发、直接说"已达到最大步数" |

`ensure_tool_pairing(messages) -> int` 补的是**孤儿 `tool_call` 的应答消息**（复用
`assistant_tool_calls` / `tool_result` 两个已有构造函数），返回补了几条。它**幂等**，
且以普通文本收尾时**不误判**（不能把"模型这轮只想说话"当成残缺）。补了必须记一条
`pairing_repaired` 事件 —— 静默修的话，事后翻轨迹只会看到模型莫名其妙又说要再调一次。

`tests/test_loop.py` 里这五条各有一组用例（含 `ensure_tool_pairing` 的 4 条纯函数用例：
healthy 不动 / 补全且紧跟已有结果 / 幂等 / 以普通文本收尾不误判）。

### 7.7 目标判定（★M9-6）

完整规格见 §4e（模块、工具、三态、命令族）。这里只记**它长在循环里的哪一处、以及为什么**：

```python
                if state.goal is not None:          # 批前复位
                    state.goal.declaration = None
                pending = self._execute_tool_calls(result.tool_calls, state, ctx)
                if state.goal is not None and state.goal.declaration is not None:
                    goal_end = self._verify_goal(state, task, ctx)   # 批后判定
                    if goal_end is not None:
                        return goal_end
                if self.session is not None:
                    self.session.checkpoint(state)
                if pending is not None:
                    return self._awaiting_user(state, task, pending)
```

**五个既有返回点、`_execute_tool_calls`、`_awaiting_user`、`budget_start` 语义全部不动。**
插在 `_run_loop` 里 → 四条入口（`run` / `run_from` / `run_turn` / 控制台）一起拿到，
理由同 `permissions.new_turn()` 的位置；headless 因为没注册工具 + 没目标，完全 no-op。

| 判定 | 目标 | 回喂 | 事件 | `terminated_reason` | 回合 |
|---|---|---|---|---|---|
| 通过（exit 0） | `done` | 不喂 | `goal_check` + `goal_completed` | `goal_done`（新） | 结束 |
| 未通过（exit≠0） | 保持 `active` | 判定 + 输出尾部 40 行 | `goal_check`(failed) | 不变 | **继续** |
| 判定无效（门禁拦下/超时/工具异常） | 保持 `active` | 「命令没能执行，本次不计入」 | `goal_check`(invalid) + 门禁的 `gate_block` | `goal_check_invalid`（新） | 结束 |

**同一步里既有声明又有 `ask_user` → 检查先跑**（`goal_done` 赢）：运行时的**事实**优先于模型的
**陈述**；两个结论都进轨迹。有测试钉这条优先级。

**目标与 `plan` 在系统提示词上的纪律完全一致**：一个字都不进 `messages[0]`（前缀缓存 + 回改第 0 条
两笔账），可见性靠创建那一刻的 kickoff 消息与 `--resume` 时的一次补投。

这些**位置的用例全在 `tests/test_goal.py` 里**（`tests/test_loop.py` **一条都没动** —— 这一项
刻意没碰既有测试文件，见下）：`test_one_declaration_triggers_exactly_one_check`（批前复位 →
一次声明恰好一次检查）、`test_check_beats_ask_user_in_the_same_step`（优先级）、
`test_failed_check_keeps_active_and_feeds_back_via_user`（失败保持 active 且本轮继续）、
`test_check_blocked_by_gate_is_invalid_not_failed` / `test_check_that_does_not_run_is_invalid`
（三态）、`test_real_bash_check_runs_through_the_gate_chain`（真 `BashTool` + 真 permissions）、
`test_goal_never_enters_messages_zero`（前缀缓存不变式）。工具面与命令族的用例见 §4e.12。

> 为什么 `tests/test_loop.py` 不改：这一项往 `_run_loop` 里插的 4 行**没有改变任何既有行为**
> （`state.goal is None` 时两条 `if` 都不进），而既有测试文件正是这一项用来证明「没动到别人」的
> 基线 —— 让它保持逐字节不变，比往里补两条更说明问题。同理 `_execute_tool_calls` 一个字符没改。

---

## 8. app/cli.py（◐M1 最小版 → 后续增强）

**文件**：`app/cli.py`
**M1 目标**：`python -m app.cli "任务"` 能用真实 DeepSeek 跑通；`python -m app.cli --mock "任务"` 无 key 演示。
- `load_dotenv()`；构建 DeepSeekClient / MockLLM（--mock 或自动降级提示）
- ToolRegistry.default(workspace_root)；QueryEngine 组装
- 打印：每步事件（工具调用名+参数摘要+结果截断）+ 最终结论 + 总 token/步骤
- **M2+**：加 `--resume`、权限 ask 交互、`--checkpoint-dir`（M3）

### 8.0 `_Runtime` / `_build_runtime()`：装配只写一遍（★M9-5）

```python
@dataclass
class _Runtime:                      # 除 session/state 之外的全部装配结果
    llm; registry; permissions; hooks; memory; memory_blocks
    skills; context; printer; mcp_clients
    workspace_root: Path
    def engine(self, session) -> QueryEngine: ...   # ★ 唯一 QueryEngine(...) 构造点
    def close(self) -> None: ...

def _build_runtime(workspace_root, *, mock, mcp, review_edits) -> _Runtime: ...
```

原先 `QueryEngine(...)` 在 `run()` 里被构造了**两遍**（`--resume` 一条路、全新会话一条路，
参数逐字相同）—— 这本身就是「两处各写一遍」。收成一处不只是整洁：**REPL 也必须从
同一个 `_Runtime` 拿引擎**。REPL 若自己装配一遍（注册工具、接权限、接 hooks、加载 MCP、
装 `--review-edits` 的确认回调），迟早漏掉一样 —— CLI 历史上**漏接过 hooks 与 permissions
各一次，而两次都是静默的**。

### 8.0b `--repl`：常驻交互模式（★M9-5）

见 §8.3 `app/repl.py`。CLI 侧只多一个开关：

- `--repl` **不带任务** → 建立/恢复会话后直接进提示符（"进来随便看看"）。
- `--repl` **带任务** → 该任务**作为第一个回合**跑掉再进提示符（"我有个任务，跑完接着聊"）。
  两者是同一条路径而不是两个入口（`run_repl` 里就一句 `if task.strip(): repl.run_turn(task)`）。
- 与 `--resume` / `--fork` / `--clear-taint` / `--checkpoint-every` / `--review-edits` 正交可组合。

### 8.1 `--review-edits`：改动前人工确认（★M9-1）

**为什么是开关而不是默认**：TS 原版的 edit 是**默认要批准**的（`permissions.ts:427 ensureEdit`，无 TTY 时直接抛
`Edit requires approval: … Start minicode in TTY mode to review it`）。但**我们的 CLI 没有确认回调**，
把 `edit` 改成默认 ASK 会让每一次改动都退化成拒绝 —— 整条 CLI 不可用，等于把安全机制变成了路障。
所以做成 opt-in：默认路径（含 `eval/runner.py`）一字不变，需要时再打开。

```python
_CONFIRM_CHOICES = {"1": "allow_once", "2": "allow_turn", "3": "allow_always",
                    "4": "deny_once",  "5": "deny_turn",  "6": "deny_always"}

def _confirm_prompt(question: str) -> str | None:
    # 只负责「显示 + 读数」，**不重新解释 question** —— 里面已经带了将要做什么（Tool.preview 产出）
    # 打印 question（含 diff）→ 编号菜单 → typer.prompt(default="4")
    # 读不到输入（EOF / KeyboardInterrupt / Abort）→ None → 引擎按安全默认拒绝
    #   （非交互环境下"卡住等输入"比"拒绝"糟得多：前者看起来像死机）
    # 直接回车默认拒绝而不是允许：确认框的默认值就是用户不假思索按下的那个，
    #   它必须选更安全的方向（有测试钉住 default 落在 deny_* 一侧）

# run() 里：
review_rules = {"tools": {"edit": "ask", "write": "ask"}} if review_edits else None
permissions = PermissionsEngine(..., confirm=_confirm_prompt if review_edits else None,
                                rules=review_rules)
```

**测试**（`tests/test_cli.py`）：开关真把 `confirm`+`rules` 交给了引擎（断构造实参，不断最终行为 ——
开关类功能挂在"解析了但 `if` 写反/漏传"上，表现是加不加参数行为完全一样且不报错）·
不开开关时构造参数与 M9-1 之前一模一样 · 编号映射与默认拒绝 · 无输入→拒绝 · **端到端真跑 CLI 拿到 diff**。

### 8.2 app/ui_streamlit.py（M2-3 控制台 v1）

**文件**：`app/ui_streamlit.py`；运行 `streamlit run app/ui_streamlit.py`（无 key 自动 Mock 演示）。

**架构（worker 线程 + 事件队列轮询）**：
- QueryEngine 在后台线程跑（daemon）；用真实 `Session(workspace, new_session_id(), on_event=events_q.put)` ——
  `Session.emit` 同一通道双写：轨迹 JSONL 落盘 + on_event 回调推入 `queue.Queue`（UI 实时流式渲染），
  监听器异常被 try/except 吞掉不影响轨迹落盘
- 主线程每次脚本运行 `get_nowait()` 排空队列 → 追加到 `session_state["log"]` → 渲染；running 中 `sleep(0.3) + st.rerun()` 轮询；**空闲状态不 rerun**（AppTest 无头测试必需，否则初始渲染死循环）
- **权限确认桥 ConfirmBridge**：worker 的 confirm 回调 `answers.get()` 阻塞等待；UI 读到 `confirm.current` 渲染 5 按钮（允许本次/本回合/总是/拒绝本次/总是），点击把粒度字符串 `answers.put` 并 rerun；新任务重置桥
- hooks：走 `default_engine()`（与 CLI 同一条链）—— block-at-submit 检查 data/tests_pass.marker，marker 由测试命令真跑成功时自动写入
- 最终展示：结论 + 终止原因/步骤/token/缓存命中率
- **M3-5 运行指标**（始终可见，不折叠）：① 上下文用量分级进度条（最近一次 provider
  `usage.prompt_tokens` / CONTEXT_BUDGET=64K，用 `ContextStats.level_of` 分
  normal/warning/critical/blocked）；② 缓存累计命中率 + 省钱估算 caption
  （`total_hit × (¥2/M − ¥0.5/M) / 1e6`，DeepSeek 输入缓存定价差；无流量时提示
  "首次调用会把 prompt 写入磁盘缓存"）；③ 折叠的"📈 缓存命中率曲线"（≥2 次调用才画）；
  ④ 检查点列表 caption（`data/checkpoints/{session_id}/step-*`，按 step 排序）

**测试**：`tests/test_ui_streamlit.py`（AppTest 无头跑通 mock 任务；验收 `python -m pytest tests/test_ui_streamlit.py`）。

### 8.3 app/repl.py（★M9-5 常驻交互模式）

**文件**：`app/repl.py`；运行 `python -m app.cli --repl`（可带任务 / `--resume` / `--fork`）。

它存在的理由是 `TASKS.md` 给它的定位 —— **④ Goal / ⑤ Loop 的硬前置**：「跨回合自动推进」
「每 N 分钟重复提示词」在"跑完就退出"的一次性进程里没有任何意义，连"下一个回合"这个
概念都不存在。**它自己持有 `AgentState`**，一整个进程用同一份 `messages`。

```python
class Repl:
    def __init__(self, runtime: _Runtime, *, checkpoint_every=None, clear_taint=False,
                 goal_turns=DEFAULT_GOAL_TURNS): ...   # ★M9-6 goal_turns: max(1, ...)，不进检查点
    def start(self, *, start_session_id=None, resume=False, step=None) -> None: ...
    def loop(self) -> None:            # 读一行 → 一个回合；只有 EOF / 提示符处 Ctrl+C / /exit 会离开
                                       # ★M9-6 进提示符前先 `if goal_can_advance(...): _run_goal_burst()`
    def run_turn(self, text) -> RunResult | None:   # 只做显示与记账，循环逻辑在 engine.run_turn 里
    def _activate(self, session, state=None) -> None:   # ★ 三样一起换：session / state / **engine**
    def _dispatch(self, line) -> None:  # 斜杠命令分派
    def _report(self, result, before_usage, before_step) -> None:   # 本回合增量
    def _farewell(self, reason) -> None:   # 退出语：必须给可执行的续跑命令
    def _wrap_up(self) -> None:            # ★ 退出时强制落检查点 + 一次约定提炼
    # ---- ★M9-6 目标 ----
    def _run_goal_burst(self) -> None:     # 自动推进一拍，**结尾一定 _pause_goal(stop)**
    def _pause_goal(self, reason, *, auto=True) -> None:   # 只改数据 + 记事件，不 return 不 raise
    def _cmd_goal(self, arg) -> None:      # /goal 的四个子命令由它一个解析（_dispatch 只切第一段 token）
    def _create_goal / _goal_status / _goal_pause / _goal_resume / _goal_clear(self) -> None: ...
```

**提示符**：`codeagent [名字或 sid 前 8 位 · step 12 · high · goal:active] › `
（污染级别只在非 `none` 时显示 —— 常显一个 "none" 会让人不再看这一段，等它真的变成 high 时
也照样不看；★M9-6 的 `goal:<status>` 同理，**只在有目标时显示**，因为 `active` 与 `paused`
下面会发生完全不一样的事）。

**11 个斜杠命令**（每个都复用已有函数、零新机制）：

| 命令 | 复用 |
|---|---|
| `/help` | `help_text()`，正文由 `_COMMANDS` 表生成 |
| `/exit` | **标志位**而不是 `typer.Exit`（后者会被 `_dispatch` 的 except 吞掉） |
| `/new` | `Session` + `engine.new_state("")` → `_activate` |
| `/resume [id\|名字]` | `_resolve_session_ref` + `Session.from_checkpoint` |
| `/fork [步]` | `cli._do_fork`（**只搬对话**） |
| `/rewind [步]` | ★M9-8：`cli._do_rewind`（**只搬文件**，默认预览 + `_confirm_rewind`） |
| `/plan` | `render_plan`（空清单不复用"已清空"文案 —— 那是 `update_plan` 清空动作的说法） |
| `/goal …` | ★M9-6：`render_goal` + `Goal` 状态机 + `_run_goal_burst`（完整规格见 §4e.8） |
| `/sessions` | `cli._print_sessions` |
| `/rename <名>` | `set_session_name` |
| `/clear-taint` | `state.clear_taint(reason="repl:/clear-taint")` —— **人的动作** |

> ★M9-8 的 `/rewind` 与 `/fork` 是**两个方向上的同一件事**（一个动对话、一个动文件），
> 拼起来才是 CLI 里的 `--fork --step K --rewind`。REPL 里的 `_confirm_rewind` 用 `input()`
> **而不是** `typer.prompt`：常驻模式里后者会和 REPL 自己的行读取打架。

> ★M9-6 的 `/goal` 是**唯一一个有自己的状态机**的命令（其余九个都是"转发给已有函数"），
> 但它同样没往 REPL 里引新机制：自动推进就是**连着调 `run_turn`**，暂停就是**改一个数据字段**。
> 详见 §4e.8（含 `BURST_STOP_REASONS` 为什么不含 `"completed"`、以及「人敲一行字 = 隐式暂停」
> 那条结构性事实）。

**两条硬不变量**（各有测试钉着）：

1. **不认识的斜杠命令绝不发给模型**。打错一个字母的代价是一次真实的模型调用
   （钱 + 时间），而模型的回答看起来还挺像回事 —— 于是这个错误不会被发现，
   只会觉得"这次答得有点怪"。只打印提示 + 指向 `/help`。
   命令判据用 **`raw.startswith("/")`**（strip **之前**的行）而不是 `line`，
   这样 `/etc/hosts 这个路径看一下` 这种以空格开头的输入仍然是任务。
2. **切换失败不能半切换**。`/resume 不存在的名字` 必须在 `_activate` **之前**返回 ——
   于是"当前会话仍活跃"是**结构性成立**的，而不是靠每处记得清理。「session 换了、
   engine/state 没换」是一个**没有任何报错**的错配：轨迹写进 A、你在看 B，事后只能
   靠翻 `data/` 发现。`_activate` 三样一起换，engine 必须跟着换（`QueryEngine.session`
   是构造期绑定的）。

**`await_user` 不需要任何特殊机制**：模型提问 → 本轮结束 → 打印问题 → **下一行输入就是
回答**。这是 M8「把打断建模成数据标志而不是阻塞控制流」的回报。反过来，若 REPL 自己
`input()` 一个"回答"，那就是把数据标志退化成阻塞控制流。

**每回合报的是增量**（步数 / token / 缓存命中 / 上下文水位 / 检查点数），不是会话累计。
`RunResult.steps` / `usage` 五个返回点给的都是 `state.step` / `state.usage`，所以 REPL
用回合前后的快照相减 —— 而不是让 loop 再维护第二套"本回合用量"的记账（那是「两处各写
一遍 → 漂移」）。**`Usage` 是可变 dataclass、`state.usage += ...` 是原地累加**，所以快照
必须 `dataclasses.replace()` 复制；不复制的话相减恒为 0 **且不报错**，只是"每次都说这
回合没花钱"。`Usage.__sub__` 与已有的 `__iadd__` 同处一个类。

**`_wrap_up`：退出时强制落一次检查点，再做一次约定提炼**（M4-1）。
`_farewell` 刚对着用户承诺了「继续: … `--repl --resume --session-id <sid>`」，
而检查点是**按节拍**写的（默认 5 步）且**只在有工具调用的步上 tick** —— 纯聊天、
或者只走两三步工具就退出，都写不出检查点，那条命令跑起来会直接报"读不到检查点"。
理由与 `_awaiting_user` 里 `force=True` 的理由相同：流程即将因非步数原因退出，
节流的下一次 tick 永远等不来。**先落盘再提炼**。提炼只做一次（`_turns == 0` 时
直接返回）：`extract_and_learn` 是一次**真实的模型调用**，而它读的是累计轨迹，
每回合跑一遍等于把同一段对话提炼 N 次；单发路径也是"一次运行提炼一次"，这里对齐它。

**它明确不是全屏 TUI**（`TASKS.md` 里 ⑥ 已决定不做）：行式输入，没有 ANSI 控制、
没有历史滚动。多行输入缓冲 / 历史文件 `readline` / 自动补全也都不做 —— 那是纯终端
体验的体力活，对这份作品集要回答的问题（循环、上下文、权限、可恢复性）不加分。

**测试**：`tests/test_repl.py`（34 例，M9-8 后为 40 例）。本仓第一条 `CliRunner(input=...)` 的 stdin
端到端也在其中（7 例）—— 在这之前 `tests/test_cli.py` 没有任何 stdin 测试。

### 8.4 `--snapshots` / `--drop-snapshots` / `--rewind`（★M9-8）

```python
def _render_rewind_preview(plan: RestorePlan) -> str          # git-diff 风格，四类 + 三个计数字段
def _do_rewind(workspace_root, sid, step, *, force, confirm=None) -> int | None
    # 返回**已经落在那一步的目标步号**，没执行则 None（调用方拿它拼 --resume --step K）
def _reinject_rewind(state) -> None                           # 恢复会话时补投"工作区被回滚过"
def _print_snapshots(workspace_root) -> None                  # --snapshots：会话清单 + 对象库占用
def _do_drop_snapshots(workspace_root, ref) -> None           # --drop-snapshots：删清单 + 回收
```

- **`_do_rewind` 返回步号而不是 bool**：调用方要拿它拼"继续"那句 `--resume --step K`，
  而目标步号在 `plan()` 里才定下来（`step=None` 时取最近一个有快照的步）—— 调用方自己算
  会算错，而算错的那句话会把人引到一个**错位的对话**上。
- **没有 changes 时不弹确认框，但仍返回步号**：`plan.changes` 为空说明工作区已经是那一步的
  样子，可"对话该回到哪一步"这个信息仍然要给。
- **部分失败不退非零退出码**：回滚本身做完了，失败的是其中几个文件；项目里的退出码语义是
  "命令有没有做成"，混起来会让脚本判错 —— 所以逐条红字报出 `(文件, 原因)` 就够。
- **`--snapshots` 与 `--sessions` 是两张表，不能合并**：一个列**对话**、一个列**文件**；
  一个会话完全可能有检查点却一个快照都没有（从没改过文件，或 M9-8 之前建的）。
- **`--drop-snapshots` 不动检查点**（输出里明说"该会话的检查点未动，仍可 `--resume`；
  仅 `--rewind` 对这几步失效"）—— 删的只是快照，`--resume` 与 `--fork` 照常。

---

## 9. 后续里程碑模块规格（占位，届时展开补全）

### 9.1 agent/permissions.py（M2，含 MiniCode 决策粒度）
- **决策粒度**（替代原 allow/deny/ask 三态）：`allow_once / allow_turn / allow_always / deny_once / deny_always / ask`。ask 时 CLI/Streamlit 弹确认，用户选一次性/本回合/总是/拒绝。
- 规则文件 `permissions.json`：`{"tools": {"bash": {"dangerous": "ask"}}, "commands": {...}, "paths": {"allow": [...], "deny": [...]}}`
- 三类请求：**path**（读/写/列/搜）、**command**、**edit**；每类 allowlist/denylist + 决策记忆（once/turn/always 落内存或文件）
- 危险命令黑名单从 bash.py 提取共享；路径沙箱复用 files._resolve 语义
- `PermissionsEngine.check(tool_name, arguments, ctx, *, details=None) -> 决策`；ask → 回调用户确认

#### `new_turn()`：让「本回合」真的是一回合（★M9-5）

```python
def new_turn(self) -> None:
    """开始一个新回合：清空**本回合**记忆（self._turn），保留 self._always。"""
    self._turn.clear()
```

**调用点在 `QueryEngine._run_loop` 入口**，不在这里的判断逻辑里 —— 那是唯一一处能让
四个入口（CLI 单发 / CLI REPL / Streamlit 控制台 / `eval/runner`）都拿到它的地方。
在单发路径上它是 **no-op**（一次运行本来就只有一回合），所以不改变任何现有行为。

在此之前 `_turn` **从来没有任何 clear/reset**（全仓零命中），`tests/test_permissions.py`
把"回合结束"定义成**新建实例**。这在单发进程里是等价的，在常驻进程里不是：
`allow_turn` 于是变成**永久放行**，与确认框上写着的「2) 本回合允许」直接矛盾。

**`_always` 刻意不清** —— 那是另一个承诺（"一直允许"），清掉等于把用户明确选过的
"总是"降级成"本回合"。有两条测试分别钉这两侧：`test_new_turn_clears_turn_memory_on_the_same_engine`
与 `test_new_turn_keeps_always_memory`，各有一个变异体（"方法在、接线在，但什么也没做"
与"顺手把 `_always` 也清了，看起来更保险"）。

**记忆键**来自 `_classify(tool_name, arguments)`：`edit`/`write` 类是 `arguments["path"]`、
`bash` 是 `arguments["command"]`、其他按工具名。所以"同一工具 + 同一路径"**键必然相同**，
跨回合重新被问只可能来自回合边界上的清空 —— 这一点在 `m9verify/repl_c.log` 的真实跑里
现场复现过（两回合都是 `edit` + `textstat.py`，第二回合重新弹确认框）。


#### `details`：确认文案里的"将要改什么"（★M9-1）

- `check` / `ask` / `describe` / `_confirm_and_record` 都多一个 **keyword-only** 的 `details: str | None`，由调用方从 `tool.preview()` 取。
- **它不参与判定** —— 只被带进确认文案给人看。一旦它能影响判定，权限引擎就有了第二套"这次编辑合不合法"的判断，与工具的「唯一匹配」语义成为**两个真相源**（见 §2.3 `Tool.preview`）。
- `describe()` 的拼接顺序是**「问什么 → 改什么 → 为什么问」**：`details` 紧跟问句，污染天花板的说明排最后。diff 是**判断依据本身**，排在理由之后就会被折叠掉；而天花板说明解释的是"为什么又问一遍"，属于补充。
- 引擎**刻意不自己去算 diff**：那正是上面那条真相源禁令。引擎判"要不要问"，工具说"改完什么样"。

### 9.2 agent/hooks.py（M2）
- `HookContext`：event_name / tool_name / arguments / result / state
- `HookEngine.run_pre(...) -> None | (block, reason)`；`run_post(...)`；示例 hook：`require_tests_before_commit`（PreToolUse 包 Bash(git commit)，检查 `data/tests_pass.marker` 文件，不存在 → block 返回"先跑 python -m pytest 验证"）
- `mark_tests_pass(workspace_root)`：手动写 marker（测试/CLI 工具用）
- `mark_tests_pass_on_success(workspace_root)`：PostToolUse —— **测试命令真跑成功（exit_code==0）才写 marker，失败则清除**，非测试命令不动。marker 必须由真实测试结果产生，否则模型不跑测试也能 `write` 出 marker 解锁，block-at-submit 就成了摆设。
- `default_engine(workspace_root) -> HookEngine`：入口层标准治理链（pre=block-at-submit + post=marker 自动维护）。**CLI 与控制台共用这一处构造**，避免两个入口各接一套接出漂移（`app/cli.py` 曾整体漏接 hooks 层）。
- 测试命令识别用 `TEST_COMMAND` 正则（pytest / npm test / cargo test / go test / mvn test …），要求前面是行首或分隔符，避免把 `git commit -m "add tests"` 误判成测试

### 9.3 agent/session.py（M3-4 已实现：JSONL 轨迹 + 检查点 + resume）

```python
def new_session_id() -> str: ...                      # "s%Y%m%d-%H%M%S"

class Session:
    def __init__(self, workspace_root: Path, session_id: str, *,
                 checkpoint_every: int = DEFAULT_CHECKPOINT_EVERY, on_event=None): ...
    def emit(self, event: dict) -> None               # on_event 转发 + append JSONL
    def checkpoint(self, state: AgentState) -> None   # 每 N 步写一次（_ticks 计数，恢复后重新数）
    def _write(self, state: AgentState) -> Path       # 原子写：.json.tmp → replace
    @classmethod
    def from_checkpoint(cls, workspace_root, session_id, *, step=None,
                        checkpoint_every: int | None = None
                        ) -> tuple[Session, AgentState]  # step=None → 最近；None → 沿用会话里记的
    def list_checkpoints(self) -> list[int]
    def latest_checkpoint(self) -> Path | None

DEFAULT_CHECKPOINT_EVERY = 5                 # 新会话的默认节拍；老检查点的回落实也在用它
_SKIP_FIELDS = frozenset({"emitter"})        # 运行时对象，不落盘
_FIELD_DECODERS = {"usage": ..., "last_usage": ..., "goal": lambda raw: Goal(**raw) if raw else None}
                                             # JSON dict → 真类型。★M9-6 加了 goal：
                                             # **给 AgentState 加非 JSON 原生类型的字段就必须在这里补一行**，
                                             # 漏了不报错（见 §5.2 的警告 —— 那个炸点会被 loop 吞成
                                             # terminated_reason="error"，看起来像引擎出错）
_LEGACY_FLAT_KEYS: tuple[str, ...]           # M7 之前的平铺格式（冻结，只读老文件）

def dump_state(state: AgentState) -> dict    # 全字段快照；不可序列化→报字段名
def load_state(payload: dict, session_id: str) -> AgentState   # 新旧格式都吃
def state_dict(payload: dict) -> dict        # 读者入口：兼容两种格式取 state
def latest_session(workspace_root: Path) -> str | None  # 按检查点 mtime 选最近会话
def unique_session_id(workspace_root: Path) -> str    # ★ 让开已存在的 id（循环加后缀）
def _stored_cadence(payload: dict) -> int | None  # 读会话当初的节拍；缺失/非法 → None
```

- 轨迹文件 `data/sessions/{id}.jsonl`（append-only；M3-3 compact 裁掉的中段消息仍完整保留，
  snip/摘要标记都指向这里）；检查点 `data/checkpoints/{id}/step-{N}.json` 形如
  `{"session_id", "step", "ts", "checkpoint_every", "state": {...}}`，`state` 是
  **AgentState 全字段快照**。
- **检查点节拍是会话的事实，跟着检查点落盘（M8）**。`checkpoint_every` 放在 payload
  **顶层**而不是 `state` 里 —— 它是会话配置，不是 `AgentState` 的字段（`state` 会被
  整个喂给 `AgentState(**raw)`）。恢复时的优先级：

  | 优先级 | 来源 | 说明 |
  |---|---|---|
  | 1 | `from_checkpoint(checkpoint_every=N)` 显式传参 | 用户当场指定，**压过**会话里记的 |
  | 2 | `payload["checkpoint_every"]` | `checkpoint_every=None` 时的来源 |
  | 3 | `DEFAULT_CHECKPOINT_EVERY` | 只对**本次改动之前**写下的检查点生效（它们没这个字段） |

  为什么要有第 2 档：节拍是**这个会话事实的一部分**。用 `--checkpoint-every 1` 起的
  会话崩在半路，恢复时按 5 走，代价是丢一整段工作（真跑现场：kill 在 step 5，续跑到
  step 9，检查点数还是 5）。而且它是**哑的** —— 命令成功、输出正常、退出码 0，唯一
  证据是检查点数不涨。CLI 因此把**实际生效的节拍打印出来**（唯一的可见性出口）。
  `_stored_cadence` 对缺失/非法值（0 / 负数 / 非整数，比如检查点被手改过）一律返回
  `None` 走回落，**不夹成 1**：对一个已损坏的检查点做出"每步都写"这种比默认更激进的
  行为，是错的方向。
- **全字段往返（M7）**：state 的落盘/恢复按 `dataclasses.fields()` 自动推导，
  **不手写字段白名单**。原先 `_write` 与 `from_checkpoint` 各有一份手写字段表，
  给 AgentState 加字段**不会落盘且没有任何提示**（已有先例：`cli.py` 重算
  `memory_blocks`，`restored.memory_blocks` 被静默忽略）。现在只有
  `_SKIP_FIELDS`（默认仅 `emitter`，运行时回调）需要显式排除；
  不可序列化的字段会在落盘时**报出字段名**（响失败 > 哑丢失）。
  `dump_state()` / `load_state()` / `state_dict()` 三个函数是唯一的格式入口：
  **读检查点的地方都走 `state_dict()`**（回放 UI、测试），不自己翻 payload 的键——
  否则每换一次格式，所有读者都得跟着改一遍（回放 UI 与测试就踩过）。
  旧格式（字段平铺在顶层）由 `_LEGACY_FLAT_KEYS` 这段**冻结的**兼容代码解，
  磁盘上的老检查点照样能 resume。
- **resume 不重置 step 计数**：恢复的 state.step 延续 → 续跑产生的检查点不会覆盖恢复前的同名文件。
- loop 接入：`QueryEngine(session=...)`；`run()` 建 state 时
  `session_id = getattr(self.session, "session_id", "m1")`；每轮 `_execute_tool_calls` 后
  `session.checkpoint(state)`；`run_from(state)` 从恢复的 state 继续执行。
- CLI：`--resume`（自动选最近会话）/ `--session-id` / `--step` / `--checkpoint-every`。

### 9.3b 会话元数据 · 分叉 · 改名（★M9-3）

```python
# ---------- 元数据（data/sessions/{id}.meta.json，与轨迹同目录不同后缀） ----------
NAME_MAX_CHARS = 60
_NAME_FORBIDDEN = set('\\/:*?"<>|')

def meta_path(workspace_root, session_id) -> Path
def read_meta(workspace_root, session_id) -> dict          # 没有 → {}；损坏 → ValueError
def update_meta(workspace_root, session_id, **fields) -> Path   # **合并**式 + 原子写
def validate_name(workspace_root, name, *, allow_session=None) -> str
def set_session_name(workspace_root, session_id, name) -> Path
def session_name(workspace_root, session_id) -> str | None  # 没有/损坏 → None（不抛）

# ---------- 分叉 ----------
class Session:
    @classmethod
    def fork(cls, workspace_root, session_id, *, step=None, new_id=None, name=None
             ) -> tuple[Session, AgentState]
    def _copy_trajectory(self, source: Session, fork_step: int) -> int

# ---------- 清单与查找 ----------
@dataclass(frozen=True)
class SessionInfo:  # session_id / name / latest_step / checkpoint_count / mtime
                    # / forked_from / meta_error（meta 坏了：名字没了，会话还在）
class SessionNotFound(LookupError):  # .ref / .available（候选清单，报错时列出来）
def list_sessions(workspace_root) -> list[SessionInfo]     # 按 mtime 倒序
def resolve_session(workspace_root, ref) -> str            # **先当 id、再当名字**
def unique_session_id(workspace_root) -> str               # 同秒撞车时让开（-2/-3）
def default_fork_name(workspace_root, source_id, fork_step) -> str  # "{源名} @{步} 分叉"
```

- **分叉挂在 step 级检查点上**（与 TS 原版的差别）：它的 `/fork` 是**会话级**的
  （整段复制、从"现在"接着走）；我们逐 step 落检查点，于是 `--fork --step K`
  可以**回到任意一步**再开一条路，这个能力是现成的。
- **单独用时，它是对话分叉，不是工作区分叉** —— 必须说清楚，否则极容易被读成"时光倒流"：
  工作区（文件）**不会**回滚到第 K 步的样子，分叉后的 agent 看到的是**当前**工作区。
  （**M9-8 之后**这句话有了正面出口：加 `--rewind` 就让文件也回到第 K 步 —— 见 §9.13。
  `--fork` 单独用时仍然只搬对话，这是刻意的：动文件要人显式说。）CLI 分叉后把这句
  **打印出来**，help 里也写明。**这句话必须跟着 `--rewind` 分叉写** —— M9-8 之前它无条件
  说"工作区不会回滚"，那时它是真的（没有回滚机制）；现在带着 `--rewind` 时它会变成假话，
  而假话比不说更糟（本项目记录在案的头号缺陷类）。
- fork 搬三样，都以 `fork_step` 为界：
  1. **检查点 `step-1..fork_step`** → 新会话因此能 `--resume --step K` 回到其中任意一步；
  2. **轨迹里 `step <= fork_step` 的行**。**不能整份复制**：轨迹是追加的，源会话在
     fork_step 之后的 `security_finding` 会被一起搬过去，分叉出来的会话于是"继承"了
     它根本没发生过的事件。**解析不了的行原样保留** —— 那是 kill 在写一半时唯一留下的
     现场，不会影响判定（污染重放读的是检查点里的 `state.events`，不是轨迹）；
  3. **meta 里的 `forked_from`**（`{"session", "step"}`），供 `--sessions` 显示血统。
- **`_load_payload(step)` 在"这一步没有检查点"时要说清有哪几步**（M9-3 补）：
  检查点是**按节拍**落的，`--checkpoint-every 2` 的会话只有偶数步，而 `--fork --step K`
  正是我们对外宣传的能力 —— 撞上没落盘的那一步时，裸的
  `FileNotFoundError: .../step-K.json` 既不像步数写错了、也不像节拍问题，用户只能去翻目录。
  现在改成 `第 K 步没有检查点（可用: [2, 4]）`，同 `SessionNotFound` 的「报错必须给出下一步」。
  **分叉与续跑共用这一个入口**，所以一条守卫同时管住两条路（两处各写一遍必然漂移）。
  但那只是**抛出**侧 —— 三条 CLI 入口（`--resume` / `--fork` / `--plan`）各自要**接住**它。
  真实跑发现只有 `--resume` 没接：`--resume --step 9` 甩出一整个 traceback，而
  `--fork` / `--plan` 早就各有一句人话。现在三条一致，都打 `读不到检查点: …` 并退 1。
- **副本里唯一不能原样搬的字段是 `session_id`**，按新会话重写。`load_state` 会 pop 掉它
  所以今天无害，但那是一颗定时炸弹：谁哪天直接读 `payload["session_id"]` 就会拿到源会话。
  属"当前无人读、将来必有人读"的错值，在写入时修掉。
- **`_write_payload(step, payload)` 把"怎么落盘"抽出来给 `_write` 与 `fork` 共用**：
  分叉写的是从别处读来的 payload，内容不由本会话的 state 决定，但原子写/缩进/编码必须
  是同一份知识。各写一遍的失败方式是静的 —— 分叉出来的会话少个字段，跑起来才发现。
- **`update_meta` 是合并式（read → update → 原子写）而不是覆盖式**：`--rename` 只该改名字，
  覆盖式会把 `forked_from` 一起抹掉，且没有任何提示。文件里自带 `session_id`（自描述）。
- **`read_meta` 与 `session_name` 对坏 meta 的反应刻意不同**：前者抛
  （"文件在但读不出"只可能是被手改坏或写了一半，**名字是真的丢了**，静默返回 `{}`
  会让人以为"我从没起过名字"从而重起一个、旧的无声消失）；后者返回 `None`
  （它的调用方只是要个显示名或拼默认名，为一个坏 meta 让整条命令失败不成比例）。
  `--sessions` 逐行标出 `meta_error` —— 一个坏文件不该让另外九个正常会话也看不见。
- **`validate_name` 的五条判据**：非空、≤60 字符、无路径分隔符/控制字符、
  **不与任何已有 `session_id` 相同**、**不与任何已有会话名相同**。后两条都是必需的：
  `resolve_session` 按名字只返回**一个**结果，重名时 `--resume --session-id <名字>`
  会安静地跑到先被遍历到的那个上去（带着另一个任务的上下文继续）；名字等于某个 id 时
  那个 id 就永远解析不到自己的会话了。**两处合起来才成立**：写入侧拒绝坏名字，
  读取侧 id 优先 —— 所以不去读取侧加优先级"猜"，猜错的代价太大。
  名字**从不参与路径拼接**（路径一律用 session_id），这条校验是防"看起来像地址"。
- **`unique_session_id` 的让开也是必需的**：`new_session_id()` 粒度是**秒**，
  同一秒里跑两条命令完全正常（测试里几乎是必然）。撞了 `Session.__init__` 不报错，
  两个会话直接共用一个检查点目录，后者覆盖前者且全程静默。
- **`--sessions` 是这一项的另一半，不是附赠**：没有它，`--rename` 写的名字和 `--fork`
  记的血统**没有任何消费者** —— 正是本项目记录在案的头号缺陷类（机制在、测试绿、
  文档写了，但没有任何东西把模型或人引到它上面）。清单的键集合取
  **检查点目录 ∪ `data/sessions/*.jsonl`**：跑到一半被 kill、还没到第一个检查点就死掉的
  会话只有轨迹，而那种会话恰恰最需要被看见。
- **CLI 侧把"取会话 id"收成 `_resolve_sid` 一处**：原先 `_print_plan` 与 `--resume`
  各写了一遍，加名字解析时只改一处、另一处照旧忽略就是漂移。收成一处，两条路径一起拿到。
- **`--rename` / `--sessions` / `--fork`（不带任务）都不构造 LLM、不需要 API key**，
  所以它们的 dispatch 放在 `_build_llm` **之前**。测试用"`_build_llm` 一被调用就炸"的
  替身钉住这一点 —— 靠"本机没配 key 也过了"来测会在有 `.env` 时假装通过。
- **不带任务就退出**是刻意的：分叉本身零成本（不动模型），续跑要花钱，
  把"要不要接着跑"留给用户显式说；上面已经打出可执行的续跑命令
  （`--resume --session-id <新 id> "你的指示"`）。M7 的教训是**一条走不通的解除指引
  比没有指引更糟**，所以指引必须可直接粘贴执行。

### 9.4 agent/memory.py（M4-1 已实现：分层指令文件 + 提取 + consolidation）

```python
MEMORY_FILENAMES = ("CODEAGENT.md", "MINI.md", "CLAUDE.md")
RULES_DIR = ".codeagent/rules";  LEARNED_FILE = "learned.md"
MAX_FILE_CHARS = 8_000;  MAX_TOTAL_CHARS = 20_000

def discover_memory_files(workspace_root: Path) -> list[Path]   # 低→高优先级
def build_memory_blocks(workspace_root: Path) -> list[str]      # 渲染记忆块（高优先级在前）
def extract_conventions(llm, events, *, max_events=60) -> list[str]  # 任务后提炼（LLM 失败→[]）
def consolidate(items: list[str]) -> list[str]                  # 归一化 + 子串合并 + hash 去重
def save_learned(workspace_root: Path, new_items) -> Path       # 写回 learned.md
class MemoryManager:
    def __init__(self, workspace_root, *, llm=None): ...
    def blocks(self) -> list[str]
    def extract_and_learn(self, events) -> list[str]
```

- **分层优先级（低→高）**：根目录 `CODEAGENT.md` < `MINI.md` < `CLAUDE.md`
  → `.codeagent/rules/*.md`（按文件名，`learned.md` 置最后=最高）。不做全局 home 层。
- **@include**：`@相对路径` 行递归原位展开；拒绝绝对路径 / `..` 段（含 `sub/../x` 形式）
  / 沙箱逃逸；循环检测与缺失都给占位注释（不报错不中断）。
- **去重 + 预算**：内容 sha256 去重，重复时保留靠后（高优先级）一份；单文件 >8K 截断、
  总长 >20K 从低优先级丢弃 + 末尾截断，均附 `<!-- 已截断 -->` 提示。
- **任务后提取**：llm.complete 从轨迹事件（tool_call/llm_call 摘要）提炼 `- ` 条目，
  过滤"无"；LLM 异常 → []（提取失败不阻断任务）。`save_learned` 去重合并写回
  `learned.md`，下次 discover 自动包含 → **跨会话生效**。
- **consolidation**：空白归一化 → 被更长条目包含的合并掉 → hash 去重（不 prune 不调度）。
- **接入**：cli.py 启动 `memory_blocks=MemoryManager(ws, llm=None if mock else llm).blocks()`；
  真实模式任务后 `extract_and_learn(result.events)`（mock 保持脚本确定性）。
- 测试：tests/test_memory.py（19 个：发现顺序/@include 全部拒绝路径/循环/去重/预算/提取/consolidation/落盘往返）。

### 9.5 agent/tools/subagent.py（M4-2 建，**M9-7 扩成 5 个工具族**）

**唯一构造点** `build_subagent_tools(llm, workspace_root) -> list[Tool]`（仿 `build_goal_tools`）。
五个工具共用**一个** `_SubagentRunner`，于是"怎么造一个子代理"只有一份实现：受限 registry
的构建、步数上限、系统提示词、abort 接线、落盘 store。

| 工具 | 参数 | 语义 |
|---|---|---|
| `subagent` | `task`, `tools`, `max_steps` | 阻塞便捷入口：spawn + 无限等。**M4-2 的签名一字未改** |
| `spawn_agent` | `task`, `tools`, `max_steps` | 立刻返回句柄 `sa-1`；满了返回 `ToolResult.fail` + 花名册（不排队） |
| `list_agents` | — | 花名册（id / 状态 / 任务摘要 / 终局 reason） |
| `wait_agent` | `ids`(可省=全部**还没交回**的), `timeout_ms` | 等；**超时只回报最新状态、不关闭** |
| `close_agent` | `id` | abort + 等它真停；对已终结的幂等 |

```python
class _SubagentToolBase(Tool):
    def __init__(self, runner: _SubagentRunner) -> None: ...
    @staticmethod _session_id(ctx) -> str        # 落盘目录用的父会话标识

class _SubagentRunner:
    def build(*, task, tool_names, max_steps, cwd, parent_session_id
              ) -> Callable[[AbortToken], WorkerOutcome]
    #   registry = _restricted_registry(tool_names)
    #   steps    = min(max(1, max_steps), MAX_SUBAGENT_STEPS)      # 上限 10
    #   store    = ToolResultStore(workspace_root, f"{parent_session_id}-sa{self._seq}")
```

- **独立上下文**：子代理用全新 AgentState + `SUBAGENT_SYSTEM_PROMPT`（只读定位），
  主循环 messages/usage/compact 全不共享；内部复用 QueryEngine（同一 LLM），代码零重复。
- **只读受限**：默认 glob/grep/read，写工具/bash 一律不给；`_restricted_registry` 是**白名单**、
  永不包含任何子代理工具 → 天然禁止递归嵌套；未知名字静默忽略（**黑名单在"以后加了新工具"时
  会静默失效**，所以用白名单）。
- **五个全部 `is_read_only() == False`**（含纯读的 `list_agents`）。理由**不是"子代理危险"，
  是调度**：只读工具走并发池（`loop._execute_tool_calls` 的 `executor.map`），而 `map` 保证的是
  **输出顺序**、不是**开始顺序** —— 一批 `[spawn(A), spawn(B), wait([A,B])]` 若进并发池，
  `wait_agent` 可能在两个 spawn 注册 id **之前**就跑起来，模型会看到自己刚派出去的 id 报"未知"。
  先例：`agent/tools/goal.py` 的 `DeclareGoalDoneTool` 同为 `False`，理由写着「它改变的是**控制流**」。
- **`ctx.workers is None`**（引擎没接管理器）时五个工具都 `ToolResult.fail` 并**说明这是接线缺口**，
  不抛异常、不假装成功（同 `DeclareGoalDoneTool` 的「不静默降级」）。
- **不进 `ToolRegistry.default()`**：同 `ask_user` / goal 的理由，外加一条更硬的 ——
  `eval/runner.py` 用的正是 `default()`，而子代理会把 README 的 token/成本/缓存数字**悄悄变得不可比**。
- **系统提示词带 `{workspace_root}` 槽位**：`_render_system_prompt` 对自定义 prompt 也做替换，
  加一行就让 worker 知道自己在哪个沙箱里，零新机制。
- **worker 刻意不给的四样**：`session`（`Session.checkpoint` 没有锁，两个 worker 同 session_id 会
  互相覆盖检查点文件）、`on_event`（否则 worker 的工具调用渲染成父的、步号也对不上）、
  `permissions` / `hooks`（只读白名单已保证不写，接上反而是多一条要推理的路径，且它们读的是**父**的
  污染标记，语义不对）、`context`（worker 上下文短且只活一个回合）。
- **给**：显式 `ToolResultStore(ws, f"{父sid}-sa{n}")` —— 补上 M3-2 在子代理里的缺口
  （`_store_for` 在无 session 时返回 `None`，超大的 read/grep 结果会**无界**进 worker 的 messages）。
  `session_id` **必须每个 worker 不同**，否则两个 worker 撞同一批落盘文件、后写的覆盖先写的。
- 测试：tests/test_subagent.py（**32 个**，三段：工具面 8 / 并发契约 14 / 接线与循环契约 10）。

### 9.5b agent/subagents.py（**M9-7 新建**：并发子代理管理器）

**必须是叶子模块**：`agent/tools/subagent.py` 在模块级 `import agent.loop`，而 `loop.py` 需要它来做
回合边界结算 —— 反向 import 即成环。所以管理器**不 import `loop.py`**：
`spawn(task, runner: Callable[[AbortToken], object])`，**跑什么由工具模块构造的闭包决定**。

```python
MAX_SUB_AGENTS = 3
AGENT_RUNNING, AGENT_DONE, AGENT_FAILED, AGENT_CLOSED = "running", "done", "failed", "closed"
TERMINAL_STATUSES = frozenset({AGENT_DONE, AGENT_FAILED, AGENT_CLOSED})
SETTLE_TIMEOUT = 1.5          # 回合边界 join 的预算
NOTE_CHARS = 400              # 结算事件里结论尾部的截断长度

class AbortToken(threading.Event):
    """就是一个**命名过的 Event**：子类化而不是包一层 —— 取消本来就只有
    "置位 / 查询 / 等待"三个动作，包装类只会多一层要读的间接。"""
    # set() / is_set() / wait(timeout=None) 全部继承自 threading.Event

@dataclass
class AgentHandle:            # 可变；全部读写走 AgentWorkers 的那把锁
    id; task; status=AGENT_RUNNING
    result=None; error=None; usage=None      # Usage | None：被杀/崩掉的报"用量未知"，不报 0
    reason=None                              # 终局 terminated_reason（"aborted" 在这里，不在 status 里）
    started_at=0.0; finished_at=None
    token: AbortToken; finished: threading.Event
    reported=False                           # 「结论已经交付过」—— 见 wait 的语义
    usage_counted=False                      # 恰好合并一次

@dataclass(frozen=True)
class AgentSnapshot: ...      # list()/wait()/close() 返回的**冻结副本**；**刻意不含 thread 字段**

class TooManyAgents(RuntimeError)   # 管理器抛；工具翻译成人话 + 花名册
class UnknownAgent(LookupError)     # 带 known_ids（仿 session.SessionNotFound 的 available）

@dataclass(frozen=True)
class SettleReport:           # killed / unclaimed / still_running / usage
    @property any -> bool; as_event() -> dict
```

- **`spawn` 立刻返回**：`threading.Thread(daemon=True)` + `start()`，不 join。
  **刻意不用 `ThreadPoolExecutor`** —— 它的线程**非 daemon**，`concurrent.futures.thread` 装了
  `atexit` join 钩子，一个卡在 120s `llm.chat` 里的 worker 会让**解释器退出被拖住最多两分钟**
  （症状是"为什么 CLI 退不出去"）。worker 的输出只经句柄交付，所以拆掉它是结构上安全的。
- **id 确定性**：`sa-1` / `sa-2`（管理器本地计数器）。测试要能逐字断言，模型要能从工具输出里
  原样抄进 `wait_agent`。
- **一把 `threading.Lock`** 保护句柄的迁移与快照 —— 否则 `wait()` 会看到 `status == "done"`
  而 `result is None` 的**撕裂读**，工具输出就成了「完成了，但没结论」。
- **容量只数非终态**（running）；数 `len(self._handles)` 会让一次长会话在成功跑过 3 个子代理之后
  **永久锁死**，而表现只是"子代理坏了"。
- **`wait(ids=None)` 的"还没交付"语义**：`not (is_terminal and reported)` —— 在跑的，**加上跑完了
  但没人取过的**。判据是 `reported` 而不是 `status`：一次 `[spawn×3, wait]` 里，第一个 worker
  完全可能在 `wait_agent` 跑起来之前就自己跑完了，只取 running 会让它被跳过、**结论静默丢失**。
  已交付过的不再返回（同一条信息出现第二次，白占 token 还破坏前缀缓存）。
- **`settle()` 的三元口径互斥**（`killed` / `unclaimed` / `still_running`）：
  - `killed` = 我们动手时还在跑、**且确实停下来了**（`not _thread_alive`）→ 跑到一半被杀，没有结论
  - `still_running` = 我们动手时还在跑、**但没停下来**（`_thread_alive`）→ 如实说没清干净
  - `unclaimed` = 在我们动手**之前**就已经自己跑完、且从没被取走 → 有结论，但丢了
  - `unclaimed` 必须排除被叫停的（`h.id not in interrupted`），否则同一个 id 会同时出现在
    `killed`（"没有结论"）和 `unclaimed`（"结论尾部: …"）里，**事件自相矛盾**。
- **`close(id)` = set token + join（有界）**；对已终结的幂等，且**改写状态时会如实报告它其实跑完了**
  （不把已完成的抹成 `closed`）。
- **用量只在父线程合并**：`Usage.__iadd__` 是四个字段各一次 read-modify-write、**不是原子的**，
  在 worker 线程里合并会丢更新，也会与 `loop.py` 的 `state.usage += result.usage` 赛跑。
  `AgentWorkers(on_usage=...)` 是**唯一**通路，每个 handle 一个 `usage_counted`、恰好合并一次
  （`wait` 合并它报的那些，`settle()` 合并剩下的）。
- **绝不动 `state.last_usage` / `usage_stale_reason`** —— 那是驱动 compact 的 provider 锚点，
  锚在一个 worker 的 token 上等于描述父会话**没有**的消息。
- 测试：见 9.5 末尾（两节共用 `tests/test_subagent.py`）。

### 9.6 eval/golden_tasks.py（M5-1 已实现：SWE-bench 思路的黄金任务集）

```python
TINYDB_REPO = "https://github.com/msiemens/tinydb.git"
DEFAULT_REPO = Path("eval/repos/tinydb")
FIX_KEYWORDS = ("fix","fixes","fixed","bug","error","crash","issue","regression","broken","incorrect")
_NON_SOURCE_NAMES = ("setup.py", "conftest.py")                # 不算"被测源码"

def git(repo_dir: Path, *args) -> str                          # 包装 subprocess git
def _force_rmtree(task_path: Path) -> None                     # rmtree + 清只读位（3.12 onexc / 3.11 onerror）
def ensure_repo(repo_dir=DEFAULT_REPO, *, clone_url=TINYDB_REPO) -> Path  # clone（3 次重试）
def discover_fix_commits(repo_dir, *, limit=20, keywords=FIX_KEYWORDS) -> list[dict]
def render_task_text(commit: dict) -> str                      # 真实 bug 报告（subject+body，不造假）
@dataclass GoldenTask: id, base_sha, fix_sha, title, task_text, changed_sources, hidden_tests
def build_task(repo_dir, commit) -> GoldenTask                 # base_sha = commit^
def _archive_to(sha, repo_dir, target) -> None                 # git archive → tar → 解包（见下）
def materialize(task, target_dir, repo_dir) -> Path            # 物理剥离（不是 worktree）
def remove_workspace(target_dir) -> None
def leak_probe(task, workspace) -> bool                        # cat-file -e fix_sha == 0 → 泄漏了
@dataclass JudgeResult: passed, returncode, summary
def _run_pytest(workspace, test_files) -> JudgeResult          # judge 与闸门共用；sys.executable
def _write_hidden_tests(workspace, hidden_tests) -> None
def judge(task, workspace) -> JudgeResult                      # hidden tests 覆盖写回 → pytest
@dataclass TaskValidity: valid, reason, base_rc, fix_rc, base_collect_error
def _stage_and_run(sha, repo_dir, hidden_tests, test_files) -> JudgeResult
def validate_task(task, repo_dir) -> TaskValidity              # 有效性闸门（两侧都查）
def validate(task, repo_dir) -> TaskValidity                   # 单任务，含兜底
def validate_tasks(tasks, repo_dir) -> list[tuple[GoldenTask, TaskValidity]]
```

- **任务构造**：从 tinydb git history 找 subject 含 fix 关键字的提交，且**同时改源码与 tests/**。
  `base_sha = fix 提交的父提交`（= bug 存在状态）；`task_text` = 该提交的 subject+body
  （真实 bug 报告，绝不编造）；`hidden_tests` = fix 提交里 tests/ 的新内容——agent 全程看不到，
  判定用。**关键字只负责缩小候选集，筛掉谁由闸门用事实说了算**（`chore: fix a lot of typos`
  这类会走进来，然后在闸门处被拒）。`setup.py`/`conftest.py` **不算源码**——它们是打包配置与测试夹具，
  算成源码会让"改了源码 + 改了测试"这个筛选条件失真（实测命中 `40398b96`）。
- **物化 = 物理剥离（不是 worktree）**：`git archive base_sha` 导出 tree，解到隔离区后
  `git init -q -b master` + `git add .` + `git commit -m "Base commit for evaluation"`。
  **为什么不能用 `git worktree`**：worktree 与主仓库**共享对象库与 refs**，而 fix 提交就在主仓库
  历史里 —— agent 一条 `git show <fix_sha>:tests/test_xxx.py` 就能拿到隐藏测试全文，
  `git show <fix_sha>:tinydb/table.py` 就是金标准补丁，而 bash 工具只校验 cwd、不校验命令文本。
  物理剥离后 agent 仍有 git 可用（能 `git diff` 自己的改动），但**没有未来**：工作区的 git 历史是
  评测生成的，不含任何原仓库历史。每次跑都过一遍 `leak_probe`（`git cat-file -e <fix_sha>`
  必须非零退出 —— 不是"不可达"，是对象**根本不在**）把这条断言测成一个量。
  - ⚠️ **归档不能落进工作区**：git 的 tar 流以 `pax_global_header` 开头，里面写着 `comment=<base_sha>`，
    先存文件再解包就等于在工作区里留一个含 sha 的明文二进制 → 整条流读进 `io.BytesIO` 再解包。
  - ⚠️ **必须 `mode="r:"`（可 seek），不能用流模式 `r|`**：归档里有 `120000` symlink 条目时，
    tarfile 要回到归档开头重读才能解析它，流模式直接 `StreamError: seeking backwards is not allowed`
    （实测把 7 个候选记成"闸门自身异常"，一个都没验成）。
  - ⚠️ **Windows 上 linkname 不是路径的 symlink 建不出来**（tinydb 的 `CONTRIBUTING.rst` 就是一条，
    linkname 是一整篇文档文本），**所有 filter 下都一样丢**，tarfile 只当非致命错误静默跳过 →
    物化后拿 `tf.getmembers()` 与 `os.path.lexists(target / m.name)` 比对，跳过就**打印出来**
    （用 `lexists` 不跟随链接，免得把悬空 symlink 误报成丢失）。
  - 目标已存在 → **自愈**（先删再建并打一行，不静默删）；`_force_rmtree` 必须清只读位，
    否则 agent 跑一次 `git gc` 产生只读 pack 后下一轮清理就 `PermissionError`。
- **有效性闸门（SWE-bench FAIL_TO_PASS 式，进入 LLM 之前，零 token 成本）**：隐藏测试必须在
  **base 失败、在 fix 通过**。base 侧 `rc==0` → 拒（**白送分**：这任务不测任何东西）；
  `rc==1` → 过（跑了且失败，最强信号）；`rc==2` → **接受并打标 `base_collect_error`**
  （收集错误是 2，写成"必须 rc==1"会误杀 fix 提交新增了 base 没有符号的合法任务；
  敢接受是因为 fix 侧已经在守）；`rc∈{3,4,5}` → 拒。fix 侧必须 `rc==0`，否则拒
  「金标准补丁在本机跑不过：这个任务谁都拿不到分」——**fix 侧同时就是 oracle 基线（= 100%）**，
  没有它，"0% 完成率"和"judge 坏了"在报告上长得一模一样。`task.test_files` 为空**短路拒绝**
  （否则 judge 跑的是整个套件 → base 侧 rc=0 → 白送分）。用 `tempfile.mkdtemp()` 物料，
  **绝不复用 `ws_root/task.id`**（judge 会把隐藏测试写进传入目录，复用就等于闸门自己制造泄漏）。
- **判定口径**：`judged = error is None and not patch_failed`（进分母）；
  `effective_pass = judged and passed and invalid_reason is None`（进分子）；
  `patch_failed` 单独计数、**绝不混进完成率**。`steps == 0` 或工作区**零变更**时即使测试通过也标
  `invalid`（理由如实写「agent 没改工作区任何文件，测试却通过」）——闸门管不住 agent
  改工作区之外的东西（往 site-packages 塞 conftest、装包）让测试变绿。
  - ⚠️ **顺序是硬要求**：`run_single` 必须 `物化 → leak_probe → 快照(before) → 跑臂 → 快照(after)
    → 判零变更 → 最后才 judge`。`judge` 会把隐藏测试**写回工作区**，快照一旦排在它后面，
    零变更检测永远非零，等于没做。
  - ⚠️ **白送分防御的顺序也是硬的**：`steps == 0` → `zero_change` → `judge_tampering`，
    每条都置 `invalid_reason` 并强制 `passed = False`。
  - ⚠️ **失败分类的桶序是硬的**：`error → patch_failed → not_scored → passed → tests_failed`。
    反过来会把"判定器崩了"记成"模型没修好"。
  - ⚠️ **D1 是 D 的子集，不是并列的第五类**：D1 = 撞 `max_steps` 且未通过 ⊆ D = `tests_failed`。
    D1 + D 相加 = 重复计数。D1 的意义是把"没预算"从"没修对"里拆出来——`one-step` 这类
    天然预算受限的臂，不拆就会被读成能力差。
- 测试：tests/test_golden_tasks.py（25 个）+ conftest `fixture_repo`（本地迷你仓库离线造
  buggy → fix → 非 fix 提交，不联网）。

### 9.6b eval/runner.py（M5-2 已实现：回归报告）

```python
DEFAULT_MODEL_FOR_PRICING = agent.pricing.DEFAULT_MODEL   # 单价查 agent/pricing.py 快照表
DEFAULT_WS_ROOT = Path("data/eval/ws")
ARMS = ("agent", "one-step", "single-shot")

@dataclass TaskResult: task, passed, run, error, duration_s, cost_cny,
                       judge_summary, invalid_reason, zero_change, leak_reachable,
                       setup_s, patch_failed, budget_exhausted, judge_tampering,
                       patch_error, raw_output, prompt
                       @property judged        # error is None and not patch_failed → 进分母
                       @property effective_pass # judged and passed and invalid_reason is None → 进分子
@dataclass ArmOutcome: run, patch_failed, patch_error, raw_output, prompt
def estimate_cost_cny(prompt_hit, prompt_miss, completion) -> float   # miss 缺省 = prompt - hit
def _snapshot_tree(root) -> dict[str, str]     # 工作区文件哈希快照（judge 之前测零变更）
def _build_llm(mock) -> BaseLLM                # mock → MockLLM.text（无 key 冒烟验证管线）
def _collect_sources(ws) -> dict[str, str]     # single-shot 的输入：全部非 tests/ 的 .py
def single_shot_prompt(task, ws) -> str        # prompt 原样进报告（可审计）
def _extract_diff(text) -> str                 # 抠 unified diff；抠不出返回 ""（不当补丁猜）
def _apply_patch(ws, diff) -> tuple[bool, str]
def _run_arm(arm, llm, task, ws) -> ArmOutcome # max_steps=1 if arm=="one-step" else 25
def run_single(task, repo_dir, *, ws_root, mock, arm, validate) -> TaskResult
def _aggregate(results) -> dict
def run_eval(repo_dir=DEFAULT_REPO, *, ws_root, limit=5, mock=False, arm="agent") -> dict
def main()   # CLI: --repo / --ws-root / --limit / --mock / --keep / --no-validate / --arm
```

- **流程**：闸门（拒掉的不进 LLM）→ 物化（物理剥离）→ 按 arm 跑 task_text 修 bug →
  零变更快照比对 → judge 隐藏测试 → `remove_workspace`。
  agent 异常 / judge 异常**如实记入 `error` 字段**（不假装成功）；物化也在 `try` 之内，
  单任务失败记成 `error` 而非整批中断。
- **三条臂**：`agent`（多轮循环，max_steps=25）/ `one-step`（**一切相同，只把 max_steps 25→1**，
  零风险真受控）/ `single-shot`（无工具、一次调用、要求输出 unified diff 由我们 `git apply`）。
  ⚠️ `single-shot` 喂的是**全部非 `tests/` 的 `.py` 源码**，不送任何定位提示——原设计只喂
  `changed_sources`，那等于把"定位"这个最难的部分直接送给模型。补丁解析/应用失败单独计数
  `patch_failed`、**绝不混进完成率**，且把模型原始输出 `raw_output` 存进报告
  （万一是我解析器的锅，人看得出来）。
- **成本**：按 **`agent/pricing.py` 的定价快照**估算（单价 + `verified_on` + `status`），
  用量取 provider 实测 `prompt_cache_hit / prompt_cache_miss / completion` tokens
  （provider-usage-first；端点不返回缓存字段时 `miss = prompt_tokens - hit` 兜底，
  否则全部输入按 ¥0 计、而同报告的 `total_tokens` 照旧计入 → 报告自相矛盾）。
- **报告**（字段名逐一对应实际落盘 JSON）：ts / mode(mock|deepseek) / repo / **model / base_url /
  repo_head** / arm / **gate{candidates, valid, rejected, oracle_baseline, enabled}** /
  candidates / pricing_snapshot_id / pricing_note / pricing_warning / judge_guard / judge_note /
  tasks / **judged**（完成率分母）/ invalid / not_scored / **passed** / patch_failed /
  completion_rate / total_tokens / total_cost_cny / total_setup_s / avg_cache_hit_ratio /
  leak_reachable / leaked_tasks / budget_exhausted /
  per_task[]（id / title / **fix_sha** / passed / **passed_effective** / error / duration_s /
  setup_s / steps / tokens / cost_cny / **patch_failed / patch_error / raw_output** /
  invalid_reason / zero_change / leak_reachable / budget_exhausted / judge_summary /
  judge_tampering / single_shot_prompt），落 `data/eval/report-*.json`。
  **自证字段**（`arm` / `model` / `base_url` / `repo_head` / `pricing_snapshot_id` / `gate` /
  `candidates`）不是装饰：没有它们，两份报告是否同一批任务、同一把尺子就无法判定。
  `raw_output` 的意义同理——补丁没落地时把模型原始输出存进报告，**能不能翻案有据可查**
  （本轮 5 个假阴性正是这样翻过来的，见 9.6d）。
- 测试：tests/test_eval_runner.py（37 个：定价快照成本 / mock 冒烟整管线 / 报告字段可 JSON 落盘 /
  `passed∧¬error` 口径 / 零变更判 invalid / 三臂 / `--keep` / `patch_failed` 不计入分母 /
  闸门崩掉只拒那一个任务）。

### 9.6c agent/pricing.py（M5-5 新建：定价快照）

**为什么需要它**：`eval/runner.py` 与 `app/ui_streamlit.py` 原来各自硬编码一份单价常量。
官方页面一改，两份常量同时变成假话，而**历史报告里的成本数字不会跟着变** —— 于是同一份报告里，
"token 数"是真的、"成本"是拿今天的价乘当天的量算出来的，**两者不同源**。更糟的是查不到价时
没有诚实的表达方式：返回 `None` / 抛异常都会让报告生成炸掉。

```python
@dataclass(frozen=True) PriceSnapshot:
    id: str                # 稳定标识，写进报告，如 "deepseek-chat@2025"
    model: str
    verified_on: str | None  # 对着官方定价页核实的日期；None = 从未核实过
    status: str            # "active"（现行）| "archived"（已不在定价页列示，价格锁死）
    note: str              # 口径说明（来源、时效性、已知的不确定性）
    input_hit: float; input_miss: float; output: float    # 元 / 百万 token
    def cost_cny(prompt_hit, prompt_miss, completion) -> float
SNAPSHOTS: tuple[PriceSnapshot, ...]
DEFAULT_MODEL = "deepseek-chat"          # agent/llm.py 实际跑的模型
FALLBACK_SNAPSHOT_ID = "deepseek-chat@2025"
def resolve(model) -> tuple[PriceSnapshot, str | None]   # 绝不返回 None、绝不抛异常
def snapshot_for(model) -> PriceSnapshot
def pricing_note(model) -> str           # 可直接写进报告的成本口径说明
```

- 叶子模块：**不 import 任何 agent 内部东西**（同 `agent/workspace.py` 的约定）。
- **查不到时不返回 `None`、不抛异常**（调用方在生成报告），回落到最近一条 `archived` 快照
  **并如实标注**"这个成本是按别的模型估的"。回落到 archived 而不是现行模型：回落到一条现行模型的
  价去算一个不同模型的账，只会给出一个看着更可信的错数。
- 同一模型有高峰/空闲两档时取**高峰档**：估成本要的是上界，用空闲档会给出偏小、且看起来同样可信的数。
- `deepseek-chat@2025`（0.5 / 2.0 / 8.0，`status="archived"`，`verified_on=None`）：
  **`verified_on` 刻意留 `None`** —— 那组价是项目从 2025 年沿用的常量注释，**从未对着官方定价页核实过**；
  编一个日期填进去比"未核实"更糟，因为它看起来像核实过。`note` 里的措辞是**「已不在定价页列示」**：
  2026-09-12 中英文页各复核一次全页零命中 `deepseek-chat`，而页脚注只点名 `deepseek-v4-flash` 等别名
  "仍可调用、对应模型已下线"，**`deepseek-chat` 不在那份名单里** —— 所以能确定的只是"不在定价页列示"，
  **不是"已下线"**（2026-09-11 实测仍能调用）。
- 同时收录现行 `deepseek-flash` / `deepseek-v4-pro` 的高峰/空闲四档（来源：官方定价页 2026-09-12），
  这样"换模型"不必重新发明价格表。
- 测试：tests/test_pricing.py（9 个：`snapshot_for` 默认模型 / archived 快照被标注而非被藏起来 /
  现行模型无告警 / 高峰档优先于空闲档 / 未知模型回落而不抛异常 / 成本算术 /
  **每条快照都必须声明 status 与来源** / `pricing_note` 含快照 id 与时效性 / 快照 id 不重复）。

### 9.6d evalverify/（M5-6：评测器分离与离线重算机制）

**为什么需要它**：跑三臂对照时 `eval/runner.py` 必须**冻结**（三条臂要跑同一份代码，中途改一版
就等于三份分数不可比）。但冻结带来一个死角：**如果尺子本身有 bug，冻结会让错误数字变成"留档事实"**。
本轮就撞上了这个死角——`_FENCE_RE` 的围栏配对错位制造了 5 个假阴性，而它在报告上表现为
「模型没修对」，**一条测试都不会变红**。

于是把**验证器具**与**被测代码**彻底分开：`evalverify/` 里所有脚本只做两件事——**读已落盘的
`raw_output` 重算**、**把对照关系打印出来**。它是这套评测框架的一个架构特性，不是一次性排错脚本。

```
evalverify/
├── diff_extract_fixed.py        # ★ 修复逻辑的【唯一真相源】（含 bug 机制的完整 ASCII 说明）
├── rescore_single_shot.py       # 离线重算：重抠 + 重落 + 重判，**LLM 调用 0 次**
├── reanalyse_patch_failed.py    # 逐任务取证：7 个 patch_failed 里，几个该由我负责
├── drive_three_arms.py          # 三臂大对照：可比性校验 → 同分母 → 配对四格 → 解耦归因
├── drive_arm_compare.py         # 单臂 × 历史基线的逐任务对照
├── drive_isolation_report.py    # 泄漏隔离取证（21/21 成立、0 泄漏）
├── drive_judge_gaming.py        # 判定器绕过 PoC（188 字节，已验证）
├── drive_gate_repro.py / drive_gate_summary.py / drive_cost_estimate.py
└── mutate_m5_5.py               # 变异测试（牙齿检查）
```

**四条硬不变式**（缺一条，"重算"就退化成"重编故事"）：

1. **零 LLM 调用。** 重算只消费报告里已存的 `raw_output`。模型当时的输出是**既成事实**，
   变的只有解析侧——所以重算回答的是「**如果解析器没这个 bug 会被判成什么**」，
   **不是**「模型能打多少分」。这个措辞在文档里必须原样保留。
2. **先自校验，再出修正值。** `rescore_single_shot.py` 的第 0 阶段用**现行**解析器重算一遍，
   必须**逐字段复现**留档聚合（21 / 14 / 11 / 7 / 0）才继续；对不上就说明重算流程本身有问题，
   此时任何"修正值"都不可信。
3. **不覆盖留档。** 修正结果写**另一个文件**（`evalverify/report_single_shot_rescored.json`），
   `data/eval/report-*.json` 一个字节都不动；输出 JSON 里带 `rescored` 溯源块。
   报告里逐字段搬运的只有**非解析相关**的字段（`tokens` / `cost_cny` 照搬——那次调用**真的发生过**）。
4. **修复代码只有一份。** `diff_extract_fixed.py` 是唯一真相源，`reanalyse_patch_failed.py` 与
   `rescore_single_shot.py` 都 import 它。**两份副本 = 迟早漂移**，而这个 bug 的教训正是
   「解析器会悄悄决定分数」。

**被修的那个 bug**（`eval/runner.py:279`，本轮**仍在**——冻结不改）：

```python
_FENCE_RE = re.compile(r"```(?:diff|patch)?[ \t]*\n(.*?)```", re.DOTALL)   # 开围栏只认三种 info string
```

模型回复里 diff 块**前面**常有一个 ` ```python ` 代码块。它当不了开围栏 ⇒ **围栏配对整个错位一格**：
` ```diff ` 行被上一个散文块当成**闭围栏**吃掉，真正的 diff 从此没有开围栏 ⇒ `findall` 抠不到 ⇒
落到兜底分支 `text[idx:]`「从 `diff --git` 切到**全文结尾**」⇒ 把模型后面的说明文字整段喂给
`git apply` ⇒ `corrupt patch` / `repository lacks the necessary blob`。

修法是两处（B/C 两档在全部 7 个任务上结果相同 ⇒ 真正起作用的是**围栏配对**，兜底截断只是保险）：

```python
_FENCE_ANY = re.compile(r"```[^\n]*\n(.*?)```", re.DOTALL)      # B: 开围栏接受任意 info string
_BARE_FENCE = re.compile(r"^[ \t]*```[ \t]*$", re.MULTILINE)    # C: 兜底切到第一个裸围栏就停
```

**三档对照是设计的一部分，不是调试残留**：A（现行）/ B（只修围栏）/ C（B + 兜底截断）逐档独立
落盘判分。A 必须复现 0/7（**证明复现忠实**），B/C 救回 5/7 —— 差额的来源因此是**可归因**的，
而不是"改了几个地方就好了"。

- ⚠️ **`evalverify/` 被 gitignore**（同 `m9verify/` 的三段式惯例）。接受的代价与补偿：
  这些脚本不进版本库，但它们产出的**结论**（修正值、归因、口径边界）**全部写进了**
  README / TECH_SPEC / TASKS.md，且每条都能追溯到 `data/eval/` 里那份**已落档且有字段自证**的报告。
- ⚠️ **修正值是反事实口径**，引用时必须与留档值**并列**：只报修正值 = 抹掉自己犯过的错；
  只报原值 = 拿自己的 bug 当模型的能力。差额（5 个任务）全部归解析器，与模型无关。
- 测试：**这些脚本本身没有 pytest**（验证器具，不是产品代码）；它们的正确性由不变式 2
  的自校验承担——自校验失败就拒绝出数。

### 9.7 app/replay.py（M5-3 已实现：控制台检查点回放视图）

```python
def list_checkpoint_sessions(workspace_root: Path) -> list[str]  # 有检查点的会话，新→旧
def list_checkpoint_steps(workspace_root, session_id) -> list[int]
def load_checkpoint(workspace_root, session_id, step) -> dict | None
def render_message(message: dict) -> str    # tool 结果截断 500；tool_calls 压缩一行；多模态兜底
```

- **回放数据** = §9.3 Session 检查点 `data/checkpoints/{session_id}/step-{N}.json` 的
  `messages`（OpenAI 格式）+ `task`/`terminated_reason`。纯函数层不依赖 streamlit runtime。
- **视图**（ui_streamlit.py）：expander 内「选会话 → 选 step → 逐条渲染」，role 标签
  system/user/assistant/tool。复用了 M3-3 compact 的"轨迹完整保留"设计：回放看到的是
  compact 前完整消息。
- 测试：tests/test_ui_replay.py（6 个：消息渲染/tool 截断/多模态兜底/会话排序/步骤读取/
  AppTest 无异常 + 回放区出现该会话）。

### 9.8 agent/mcp.py（M6-3 起步，M9-4 补传输与能力面：MCP 客户端）

**分层：`Transport`（怎么把一条 JSON-RPC 消息送出去、拿回来）与 `MCPClient`（说什么）分开。**
加 HTTP 传输时协议层（`_request` / `_notify` / `list_tools` / `call_tool` / 会话自愈）**一行未改**。
这不是设计洁癖，是同一个错误不想修两遍：超时整形、id 关联、错误包装、会话重建这四件事在两种传输上
必须完全一致，写两遍必然漂移 —— 而**只测单一传输的套件看不见分歧**。

```python
PROTOCOL_VERSION = "2025-06-18";  DEFAULT_TIMEOUT = 20.0;  MAX_LISTED_ITEMS = 20

class MCPError(RuntimeError): ...
class MCPTimeout(MCPError): ...          # 让报错能带上方法名（传输层不知道在等哪个方法）
class MCPSessionExpired(MCPError): ...   # HTTP 404 + 带 session id → 可自愈的那一类

class Transport(ABC):                    # timeout 是它的属性：超时只有这一个真相源
    def start(self) -> None
    def send(self, payload: dict) -> None            # 响应（若有）进内部队列
    def receive(self, timeout: float) -> dict | None # None = 通道结束；超时抛 MCPError
    def close(self) -> None
    def hint(self) -> str                            # 出错时的补充上下文

class StdioTransport(Transport):         # Popen + stdout/stderr 各一个后台抽干线程
class HttpTransport(Transport):          # Streamable HTTP：POST 同时承担收发，无需后台线程
    session_id: str | None

def build_transport(spec, *, workspace_root=None) -> Transport   # **唯一一处**判断 command/url
def _decode_body(raw, content_type) -> list[dict]   # JSON 单条，或 SSE 若干条
def _parse_sse(text) -> list[dict]                  # 空行才是分发点；末尾无空行也收下
class _NoRedirect(HTTPRedirectHandler): ...         # 3xx 不跟随，如实报出目标地址

class MCPClient:
    def __init__(self, transport: Transport, *, name="mcp"): ...
    def start(self) -> MCPClient          # transport.start() + initialize 握手 + initialized 通知
    @property
    def timeout(self) -> float            # 转发 transport.timeout（不另存一份）
    def list_tools(self) -> list[dict]
    def call_tool(self, name, arguments) -> ToolResult
    def list_resources(self) -> list[dict]
    def read_resource(self, uri) -> ToolResult        # blob（base64）不解码，如实报字节数
    def list_prompts(self) -> list[dict]
    def get_prompt(self, name, arguments=None) -> ToolResult
    def close(self) -> None;  __enter__/__exit__
    server_info: dict;  protocol_version: str | None;  capabilities: dict

class MCPToolAdapter(Tool):               # 远端工具 → 本项目 Tool
    input_model = BaseModel               # 占位：schema/run 均覆写
    def is_read_only(self) -> bool        # 只信 server 的 annotations.readOnlyHint，默认 False
    def is_external(self) -> bool         # True：第三方工具，权限引擎默认不放行（M7）
    def schema(self) -> dict              # 用远端 inputSchema，不从 pydantic 生成
    def run(self, arguments, ctx) -> ToolResult   # 跳过本地校验，参数原样透传

class MCPResourceTool(Tool):  name = "read_resource"   # 只读 + 外部（M9-4）
class MCPPromptTool(Tool):    name = "get_prompt"      # 只读 + 外部；参数是两个平行数组

def load_mcp_servers(config_path, registry, *, workspace_root=None)
        -> tuple[list[MCPClient], list[str], list[str]]   # (clients, 注册名, 已授权名)
def _register_capability_tools(...) -> None   # 能力声明 **且** 列表非空才注册那个工具面
def _register(registry, tool, alias, allow_patterns, registered, allowed) -> None
def _is_allowed(raw_name, final_name, patterns) -> bool    # 远端名或注册名命中 allow 即免确认
def _render_resources/_render_prompts(client_name, items) -> str   # 描述里列出可用项
def _flatten_content(content) -> str      # text 拼接；dict 归一成单元素列表；其余给可读占位
def _blob_size(blob) -> int               # base64 → 原始字节数（**要算 `=` 填充**）
```

- **为什么手写而非用官方 `mcp` SDK**：官方 SDK 是 async（anyio），而 QueryEngine 是同步循环，
  为一个工具把整条循环改成 async 不划算；MCP 的协议面很窄（JSON-RPC 2.0 + 十来个方法），
  手写更透明且不引入新依赖（HTTP 传输用标准库 `urllib`，与 `tools/web.py` 同一手法）。
- **传输实现**：
  - **stdio**：stdout/stderr 各一个后台线程抽干（管道阻塞读没法设超时，Windows 上 `select`
    也不支持 pipe）；stdout → 队列，请求按 `id` 关联响应；stderr → 环形缓冲（40 行），出错时
    把 server 的真实报错带进异常信息，而不是干巴巴一句"超时"。EOF 时往队列塞 `None` 哨兵，
    让等待中的请求立刻知道 server 没了。
  - **HTTP（Streamable HTTP，2025-06-18）**：单个端点收 POST，响应**可能**是
    `application/json`（一条消息）也可能是 `text/event-stream`（若干条，最后一条通常是本次响应）
    —— spec 允许两种，客户端必须都认。POST 本身**同时承担收发**，所以不需要后台线程：
    `send()` 把响应解析进队列，`receive()` 从队列取。`initialize` 回 `Mcp-Session-Id` 就记住，
    后续每个请求都带上；同时带 `MCP-Protocol-Version`，`Accept` **必须同时**列出
    `application/json` 与 `text/event-stream`（spec 的 MUST）。
  - **`Accept` / `Content-Type` / `MCP-Protocol-Version` 不可被用户 `headers` 覆盖**（认证头之类照常补充）：
    让它们"可配置"等于提供一个必然把自己配坏的口子。
  - **会话自愈**：带 session id 的请求收到 404 → `MCPSessionExpired` → `_request` 重新
    initialize 开一个新会话再重试**一次**（只重试一次：server 每次都回 404 时无限重试等于把超时改成死循环）。
  - **`_raise_for_http_error` 的判断顺序有讲究**：`404 + 带 session id` 必须排在
    "正文里有 JSON-RPC error" **之前**。真实 server 回 404 时正文里常常也放一条 error，反过来
    "会话过期"会被报成一次普通调用失败 —— 文案看着完全合理，但自愈那条路**永远走不到**。
  - **重定向不跟随**：`urllib` 默认把 301/302/303 上的 POST **改写成 GET**，一次 `tools/call`
    变成一次静默的读请求。宁可报一句能照着改配置的话（含目标地址）。
  - **`timeout` 只在传输层存一份**；`MCPTimeout` 单独一个类型，由 `_request` 补上方法名
    （HTTP 侧还带端点地址，stdio 侧带 server stderr 末尾）—— 否则两种传输的诊断信息不一致。
  - **明确未做**（如实标注）：独立 GET SSE 流（server 主动发起请求那条长连接）与
    `Last-Event-ID` 断点续传。tools/resources/prompts 三件事都走 POST 请求-响应，用不到它们。
- **安全边界（重要）**：MCP 工具来自第三方 server，**不受 workspace 沙箱约束**。所以
  ① 必须显式配置（`--mcp`）才注册，不进 `ToolRegistry.default`；
  ② 只读性只信 server 声明的 `readOnlyHint`，没声明就当可写（串行，绝不并发跑未知副作用）；
  ③ 它们**照样走 `_gate_and_run` 门禁链**——hooks 与权限引擎对 MCP 工具同样生效；
  ④ **权限默认不放行**（M7）：`is_external()` 为真的工具由 `_classify` 归到 `external`
  类，`_rule_check` 命中 `rules["external"]["allow"]` 才 `ALLOW`，否则 `ASK`。
  这正是把权限/钩子做成独立层的回报：接入新工具来源无需改循环。
  ⑤ `url` **不做 SSRF 校验**，与 `web_fetch` 刻意不同：那个 URL 是**模型**给的，这个是人写在
  `mcp.json` 里的显式 opt-in —— 校验一个用户自己填的地址没有意义。
- **resources / prompts 能力面（M9-4）**：server 在 `initialize` 里声明了该能力、**且列表非空**时，
  才注册 `read_resource` / `get_prompt`。只判能力的话，一个声明了 `resources` 却一处资源都没有的
  server 会拿到一个**永远调不通**的工具（白占 schema token，模型每次调用都失败一次）。两个工具面
  都是**只读 + 外部**（进并发只读批，且权限默认不放行）。工具描述里**列出可用 uri / 提示词名与
  参数名（含必填性）**，不列的话模型只能瞎猜一个试试 —— 那正是 M8 `update_plan` 栽过的
  elicitation gap 形状。描述恒在上下文里，列出来是**零额外往返**；封顶 `MAX_LISTED_ITEMS = 20`
  并如实说「另有 N 处未列出」（一个挂了 500 处资源的 server 不该把 schema 撑爆）。
- **配置**（`.codeagent/mcp.json`）：
  ```json
  {"servers": {"<别名>": {"command": ["python","-m","some_server"],   // 或
                          "url": "https://host/mcp",                   // 二选一
                          "headers": {"Authorization": "Bearer ..."},  // 仅 HTTP
                          "timeout": 20, "allow": ["工具名"]}}}
  ```
  `command` 与 `url` **二选一只在 `build_transport` 里判断**（两处各判一遍必然漂移，最后表现成
  "配了 url 却被当成 stdio 去起子进程"）；两个都给 → 报错并跳过该 server，**不静默选一个**。
  `--mcp` 的**相对路径按工作区解析**，不按进程 CWD（help 里给的例子就是 `.codeagent/mcp.json`）。
  `allow` 是**免确认白名单**，支持 fnmatch（`"e*"` / `"*"`）。匹配在 `load_mcp_servers`
  里做，因为只有那一处同时知道「远端名」与「注册名」（重名时后者加了 `<别名>__` 前缀）——
  否则用户得去猜前缀，配置就成了实现细节的泄漏。`load_mcp_servers` 返回的第三个值
  （已授权名列表）由入口交给 `PermissionsEngine.allow_external()`；它与规则文件里的
  `external.allow` 是**并集**，所以「先加载 mcp 还是先加载规则文件」不影响结果。
- **容错**：单个 server 启动失败/崩溃只打印 stderr 提示并跳过，不影响其它 server 与主流程
  （MCP 是增强项，不该成为启动路径上的单点故障）；与内置工具重名时加 `<别名>__` 前缀，
  **不静默遮蔽**内置工具。客户端由调用方 `close()`（子进程 + 管道是资源，不等 GC）。
- 测试：tests/test_mcp.py（**47 个**）+ `tests/fake_mcp_core.py` / `fake_mcp_server.py` /
  `fake_mcp_http_server.py`——**真子进程、真管道、真 socket、真 JSON-RPC**（不是 mock）：
  握手/列工具/成功·isError·未知工具/只读注解透传/跳过本地校验/重名加前缀/坏 server 不炸主流程/
  崩溃与超时诊断/**allow 白名单语义**/**经真实 QueryEngine 循环调用 MCP 工具**；
  传输层覆盖 JSON 与 SSE 两种响应 / SSE 里夹通知 / session 与协议版本头 / 会话过期后重新
  initialize / 重定向如实报错 / 缺 `Accept` 被拒 / **同一条操作序列走两种传输逐项比对（含报错文案）**；
  能力面覆盖文本与二进制资源 / 模板参数 / 描述里列出可用项 / 能力缺失或列表为空则不注册 / 与 allow 的接线。
  权限侧的判定与拒绝文案在 tests/test_permissions.py（外部工具默认 `ASK`、授权后 `ALLOW`、
  裸名与加前缀名都能配、拒绝理由含出处与解除方式）。
- **真实远程 HTTP 端到端验证过**（DeepWiki `https://mcp.deepwiki.com/mcp`，走公网）：握手拿到
  `serverInfo DeepWiki 2.14.3`，DeepSeek 实际调用 `read_wiki_structure` 成功（1 步 / 2425ms）。
  该 server **无状态**（不回 `Mcp-Session-Id`），客户端照常工作；它声明了 `resources` / `prompts`
  但列表均为空，于是正确地**没有**注册那两个工具面。

---

### 9.9 agent/security.py（M7 已实现：注入文本检测 + 会话级污染标记）

**先把范围钉死**：本模块**不是**「防止 prompt 注入」。注入防不住 —— 攻击者改写一个词就能绕过，规则无法穷举。它是**概率性**的文本模式匹配，输出只影响**告警**与**降级判断**，**不直接决定放行/拒绝**。

```python
TAINT_NONE = "none"; TAINT_MEDIUM = "medium"; TAINT_HIGH = "high"
higher(a, b) -> str                     # 取更高级别（标记只升不降）
MAX_SCAN_CHARS = 200_000                # 扫描前的长度上限

@dataclass(frozen=True)
class Finding:
    rule: str; pattern_id: str; line: int    # 如 "instruction_override#2"、"@行12"
    def label(self) -> str                   # **没有 excerpt 字段**（见下）

def scan_text(text) -> list[Finding]         # 长度上限 → 字面量预筛 → 正则
def level_for(findings) -> str               # 由命中推导级别
def scan_tool_output(text, *, source) -> tuple[list[Finding], str]

def spotlight(text, source) -> str           # 不可信**数据**外框
def memory_frame(text, source) -> str        # **项目约定**外框（措辞不同，见下）
```

- **规则族**（7 类）：`instruction_override`（指令覆盖）· `persona_hijack`（人格劫持）· `fake_authority`（伪授权：声称"用户已批准"/"无需确认"/"不要告诉用户"）· `fake_system_frame`（伪造 `<system>` / `system:` 角色帧）· `hidden_text`（零宽字符 `U+200B-U+200D/U+2060/U+FEFF`、bidi 覆盖 `U+202A-U+202E/U+2066-U+2069`）· `exfiltration` · `memory_poisoning`。
- **同现判据，不是关键词命中**。`exfiltration` 要求「凭据名词 + 外发动词」在**同一行 80 字符内**同时出现；`memory_poisoning` 要求「记忆文件名 + 写动词」同现。单提一个 `.env` 或 `CLAUDE.md` **不算信号** —— 一个把 `.env` 写进文档的项目满地都是 `.env`，把常见名词当信号，扫描器会先淹没自己。这条设计的实测依据见下（误报从 27/45 文件降到 9/85）。
- **级别按证据强度，不按命中条数**（`level_for`）：
  - 命中任一 `HIJACK_ARTIFACTS`（`hidden_text` / `fake_system_frame` / `fake_authority`）→ **high**。这三类命中的不是「在谈什么话题」而是**物证**：不可见字符、伪造的角色帧、对已获人工批准的声称，正常技术文本里不该出现。
  - `AGENT_DIRECTED`（前三条祈使类）命中 **且** 另有至少一条非 `_ADVISORY_ONLY` 的规则族 → **high**。单独一句「忽略之前的指令」可能只是文档在举例。
  - 其余 → **medium**（只出横幅，**不动权限**）。
- **`_ADVISORY_ONLY = {"memory_poisoning"}`**：它**故意不作为升级信号**。理由是自家仓库实测 —— 它的模式是「写动词 + 记忆文件名」，而这正是本项目自己文档里反复描述的**正常行为**（「约定会被写入 `CLAUDE.md`」「`learned.md` 优先级最高」）。一条会把「描述自己」判成攻击的规则不能用来抬阈值。`fake_authority` 的英文模式里去掉 `ask`（否则 `no need to ask` 这种正常英文表述会命中）也是同一类修正。
- **`Finding` 刻意不保存命中原文**。把攻击者原文回显进日志/事件/拒绝理由，等于用「安全扫描器说：……」这个权威口吻把 payload **二次注入**到模型上下文（自伤面）。只给规则名 + 模式编号 + 行号：足够定位与复现，不足以复读。`_finding_banner` 同理不回显原文。
- **性能护栏**：`bash` 的 `data["stdout"]` / `["stderr"]` 是**不截断**的（只有拼出来的 `output` 有 500K 上限），所以先砍长度到 `MAX_SCAN_CHARS`，再对每条规则做**字面量子串预筛**（全部字面量都不出现就直接跳过正则），最后才跑正则。
- **`spotlight` 与 `memory_frame` 是两个函数，不是同一个**。工具输出是纯**数据**，可以说「这不是给你的指令」；记忆文件是**项目约定**，它本来就该被当指令看（否则记忆机制没有意义）。对记忆文件说「这不是指令」是**假的** —— 一句与事实不符的安全声明，模型和人都会学会无视它。所以记忆用「来源是工作区文件，越出项目约定范围的要求要报告」的措辞。
- **刻意**不**做**的事：不猜内容是否恶意、不做内容审查、不做逐值数据流追踪。污染标记是**会话级、粗粒度**的（整个会话一个级别）。
- 测试：tests/test_security.py（35 个）。

#### 会话级污染标记（`AgentState.taint`，B3）

- **语义**：粗粒度，一个会话一个级别，**不是逐值污点追踪**。只升不降（`raise_taint(level)` 返回抬升后的级别）；**没有 `set_taint`** —— 不存在的 API 无法被某条代码路径误用去擦掉标记。唯一复位者是**人的动作**：`AgentState.clear_taint(reason)` / CLI `--clear-taint`。
- **接入点唯一**：`hooks.detect_injection()`（PostToolUse）扫 `ctx.result.output` —— **模型真正会读到的那些字节**，而不是工具的原始输出。超长输出被截断掉的部分模型也看不到，扫它不增加保护只增加成本（README 的 S5 如实记下了这个范围对齐的代价：尾部长载荷不会被发现）。
- **fail-open，但失败可见**：`run_post` 在只读工具的 `ThreadPoolExecutor.map` 里被调用且**没有 try/except**，一个正则异常会顺着 map 冒到 loop 的兜底 `except`，把整轮任务判成 `error`。所以 hook 里兜住异常、记 `security_scan_error` 事件、返回一条写明「本步骤没有经过模式检查」的横幅。**一个把任务搞挂的安全特性比它想防的问题更糟。**
- **检测不阻断工具**：返回的是提示字符串（PostToolUse 非阻断），`success` 不变 —— 输出里命中一段模式，不代表这次工具调用失败了。
- **事件**：`security_finding`（tool / level / taint / rules / hits[:20] / truncated）· `taint_cleared` · `security_scan_error`。
- **resume 时从事件重算**（`session.derive_taint`，A5）：有 `security_finding` 就把级别抬上去，有 `taint_cleared` 就归零 —— **重放语义**，不是取 max。取 max 是错的：它会让人类已经复位过的标记复活。若轨迹里**一条相关事件都没有**（M7 之前的检查点）则返回 `None`，与「算出来是 none」区分开，此时回落到落盘的 `taint` 字段。

#### 后置天花板（执行侧，A4）

真正收紧能力的**不是**检测器，是 `permissions.py` 的 `_apply_taint_ceiling`：`high` 会话下，把三类**不可逆**动作从 `ALLOW` 降到 `ASK`。

| 类别 | 判据（`_irreversible_kind`） | 为什么算不可逆 |
|---|---|---|
| 网络外发 | **`web_fetch` / `web_search`（工具名即判据，M9-2）**；bash 命令行里的 `curl` / `wget` / `nc` / `scp` / `ssh` / `Invoke-WebRequest` …、`requests.` / `httpx.` / `urllib.request` / `socket.socket` | 数据出去了就出去了，事后撤销没有意义 |
| 读取凭据 | `.env` / `id_rsa` / `.aws` / `credentials.json` / `.npmrc` … | key 进了模型上下文，只能靠轮换补救 |
| 写入记忆文件 | `CLAUDE.md` / `CODEAGENT.md` / `learned*.md` / `.codeagent/rules` | 会被**后续每个会话**自动注入，是跨会话持久化 |

> **第一行在 M9-2 之前是空的**：`ToolRegistry.default()` 里一个联网工具都没有，所以「网络外发」这一类只能靠 bash 命令行命中 —— 一个天花板挂着一个打不到的动作。加工具的同时接线也必须跟上，而接线有个位置陷阱：`_irreversible_kind` 的 web 分支**必须写在取 `raw` 之前**（它只取 `command`/`path`/`pattern`，web 参数是 `url`/`query`），写在后面等于永远返回 None，**单测不会变红**。有专门的变异体钉这个位置。

- **bash 的凭据判据是「提到即命中」，不是「读动词 + 路径」**（`CREDENTIAL_MENTION`，不锚定末尾）。这一条是**真跑真模型之后改的**，两版都不对：
  1. 第一版要求「读动词 + 凭据路径」同现，动词表是 `cat|type|head|tail|less|more|Get-Content|gc`。模型读 `.env` 用的却是 `findstr /r /c:"^[A-Za-z_]" .env`（Windows 上 `grep` 的自然替代）—— **不在表里，天花板没生效**：命令正常执行、变量名进了上下文，轨迹里连一条 `gate_block` 都没有。枚举读动词是打地鼠（`findstr` / `Select-String` / `grep` / `awk` / `sed` / `od` / `strings` / `python -c` …），漏一个就等于这类动作完全没有天花板。
  2. 第二版去掉动词表、只按**锚定**的凭据路径判 —— 于是 `copy .env x.txt` 漏了（末尾是 `x.txt`），而「把凭据文件当输入写到别处」正是最典型的带离手段。
  3. 收敛成一句话：**high 会话里，提到凭据文件的 bash 命令都要人工确认**。`read`/`write`/`edit` 仍用锚定的 `CREDENTIAL_PATH`（它们的 `path` 参数本身就是一条路径，锚定末尾正好表达「操作的就是这个文件」）。
  - **代价（故意的）**：`grep -rn "\.env" README.md` 这种只是**提及**的命令在 `high` 会话里也会被收紧。一句话能讲清的规则比一张要维护的动词表可靠，而解除只要人的 `--clear-taint` 一句话。实测边界见 README 的 S6–S10（22 条命令的探针结果）。
- **只降不升**（`DENY`/`ASK` 不动）、**只覆盖这三类**。`write` 一个普通源码文件、`ls`、`git status` 都不在里面 —— 它们可撤销、可由人复核，收紧它们只会让工具变成路障（CLI 里没有确认交互）。
- **位置是这里最要紧的一件事**：天花板在**记忆之后**、**人工确认之前**。写成规则链里的一条无效 —— `_always`/`_turn` 会在它之前 return，用户只要开过一次 `allow_always`，任何基于规则的收紧就永久失效，而「记得越久越省事」正是用户去开它的原因（H4）。放在人工确认之前也是必须的：confirm 是一个真实的人当场作出的决定，自动机制不该反过来推翻它。
- **`denial_hint` 重算而不读状态**：`_gate_and_run` 的只读批次用线程池并发跑，任何「上一次判定」式的共享字段都可能把 A 调用的理由安到 B 调用头上。`_irreversible_kind` 是纯函数，重算没有这个窗口。
- **`describe()` 会写明原因**：污染触发的确认框额外说明「本会话命中过可疑文本模式，因此这类动作重新征询」。一个不说理由的确认框，训练出的是不看理由的人。
- **拒绝文案带出处 + 解除方式**：指出是哪一类动作被收紧、原因看轨迹里同步骤的 `security_finding`、用 `--clear-taint` 复位后重试。拒绝而不说怎么解，等于把安全机制变成路障。
- **解除动线必须真的闭合**：文案让用户「复位后重试」，那就得有一条路能把「我已复位，请重试」送进会话。`--resume` 原先**静默丢掉**位置参数（`run_from` 用 `state.task`），于是模型按文案停下等人、人却回不了话 —— 真跑就是这么卡住的。现在非空 `task` 会作为一条 user 消息追加进会话，并记一条 `resume_instruction` 事件、在控制台打出「续跑指示: …」。**一条走不通的解除指引，比没有指引更糟**：它让人以为机制已经放开了，实际只是没人接话。

#### 记忆：按来源隔离，而非按内容过滤（B4）

- **一个被推翻的初始判断**（记录在案）：原以为高危通道是「读投毒文件 → 轨迹 → `extract_conventions` 提炼 → 写进 `learned.md` → 下次注入」。查代码后否掉了 —— `_trajectory_text` 只输出 `tool_call` 的 name + arguments（截断 120 字符）+ success 与 `llm_call` 的**计数**，**工具输出根本不进提炼**。按内容过滤是瞄错了通道。
- 真实通道是**直接的**：克隆来的仓库自带 `CLAUDE.md` / `.codeagent/rules/*.md`，**原样**注入 system prompt。因此修法是**结构性的**：
  1. 记忆块按**来源**标注 + `memory_frame` 外框（`build_memory_blocks`）。
  2. `high` 会话的 `extract_and_learn` 写 `learned.pending.md`（`PENDING_FILE`）—— **不自动注入**，等人复核。判据是**会话标记**，不是「提炼出来的内容像不像被带偏」（内容过滤会误伤正常条目）。`discover_memory_files` 结构性排除它，不是靠文件名藏着。
  3. `@include` 拒绝 `INCLUDE_DENY_DIRS = ("data/tool-results",)` —— agent 自己的不可信落盘区；并加 `MAX_INCLUDE_DEPTH = 8` 防深链条。
- 全部局限（记忆文件仍原样进 prompt、目录黑名单是枚举的…）列在 README「已知未修复的绕过路径」的 S13/S14。

#### 实测数字（自建回归样例，非基准，不构成检出率）

`tests/test_security.py` 里 `test_corpus_reports_measured_numbers` 直接打印并断言真实计数：**payload 21/21 命中预期规则族 · 良性文本 0/8 判到 high · 已知误报 2 条钉死**。全仓扫描（85 个文件）当前 **9 个文件报告 medium 以上、1 个 high** —— 那一个是 `agent/security.py` 自己（里面逐字写着这些模式），已作为**已知误报**写进 README 的 S3。调优前的数字是 45 个文件里 27 个报告、13 个 high，不可用；改法就是上面那条「同现判据 + 物证分级」，不是调阈值。

---

### 9.10 跨模块的两条确定性措施（M7 已实现）

这两条不属于任何单一模块，但都是**确定性**的（不猜文本），列在一起。

#### bash 子进程环境清洗（`agent/tools/bash.py`，A1）

- **问题**：`app/cli.py` 的 `load_dotenv()` 把 `DEEPSEEK_API_KEY` 灌进 `os.environ`，而 `subprocess.run` 默认**继承父进程环境** —— 于是 `echo %DEEPSEEK_API_KEY%`（POSIX 下 `printenv DEEPSEEK_API_KEY`）一条命令就能把 key 打出来，**完全不需要读任何文件**。这是最短的外泄路径，比「读 `.env` 再外发」短得多，只盯着「读凭据 + 网络外发」的规则会系统性漏掉它。
- **做法**：`subprocess.run(..., env=_scrubbed_env())`。`SENSITIVE_ENV_PATTERNS` 按**变量名**剔除：`*_API_KEY` / `API_KEY` / `*_TOKEN` / `TOKEN` / `*_SECRET` / `*_SECRET_*` / `*PASSWORD*` / `*PASSWD*` / `*_CREDENTIAL(S)` / `AWS_ACCESS_KEY_ID` / `AWS_SESSION_TOKEN` / `GH_TOKEN` / `GITHUB_TOKEN`。
- **刻意不用白名单**：白名单会把 `VIRTUAL_ENV` / `PYTHONPATH` / 代理设置一并干掉，把正常任务跑坏。按名剔除是这里更合适的粒度。
- **局限（不夸大）**：这是按**名**的黑名单，**不是保证** —— 换个名字（`MY_PRIVATE_STUFF=xxx`）照样漏；bash 也仍能 `type ..\.env` 直接把仓库根的 `.env` 读出来（bash 的沙箱只管 cwd，不管命令文本里的 `..`）。两条都在 README「已知未修复的绕过路径」的 S12 / S16。
- **实测（真跑，非 mock）**：同一命令两版对照 —— `env=None`（修复前语义）输出 **35 字节、含真实 key**；`_scrubbed_env()` 输出 **18 字节、字面量 `%DEEPSEEK_API_KEY%`**。真跑 agent 时它把 `%DEEPSEEK_API_KEY%` 写进文件，`sk-` 出现 **0 次**。**但对照组也确认了上面那条残留是真的**：`type ..\.env` 与 `cat ../.env` 各返回 **336 字节、含真实 key**（`read` 工具读同一路径被硬 deny）。

#### 第三方（MCP）工具必须显式授权（`agent/mcp.py` + `agent/permissions.py`，A2）

- **问题**：MCP 工具原本落到 `_classify` 的通用 `("tool", name)` 分支 → `_rule_check` 兜底 `return Decision.ALLOW`，即「接上第三方 server 就默认信任」。
- **做法**：`Tool.is_external()`（默认 `False`）→ `MCPToolAdapter` 返回 `True`；`_classify` 归到 `"external"` 类；`_rule_check` 的 `external` 分支命中 `rules["external"]["allow"]` 才 `ALLOW`，**否则 `ASK`**。授权来自 `mcp.json` 每个 server 的可选 `"allow": [...]`，经 `load_mcp_servers` 的第三个返回值交给 `PermissionsEngine.allow_external()`；它与规则文件里的 `external.allow` 是**并集**（否则「先加载 mcp 还是先加载规则」会决定谁生效，成了隐性顺序依赖）。`allow` 支持 fnmatch（`"e*"` / `"*"`）。
- **这是破坏性变更**（M7）：在此之前 MCP 工具是零策略放行的。CLI 无交互确认 → 默认判定变成拒绝，并把「给对应 server 加 `allow`」写进拒绝理由回喂模型。`tests/test_mcp.py` 的「无权限无 hook → 放行」用例、`.codeagent/mcp.json` 示例、README 与 interview_guide 都已同步改掉。
- **但要说清它是什么**：这是**策略**，不是隔离。显式 `allow` 之后，第三方 server 做什么由它自己决定 —— 它不受 workspace 沙箱约束。README 的 S11 如实写着「这不是沙箱，只是授权开关」。
- **实测（真跑，非 mock）**：同一个官方 `mcp-server-time`（`python -m mcp_server_time`）、同一个任务，**只改 `allow`** —— 不配 → 工具被拒，拒绝文案带出处与确切改法；配上 → 控制台打 `MCP 授权: 2/2 个工具免确认`，返回真实时间。这一条是 A2 的验收点：证明它不是「文档里写了」而是**行为真的变了**。

### 9.11 四条真实验证（DeepSeek 官方通路，不用 mock）

M7 的机制不能只靠单测验收 —— 下面四条各跑了一遍真实 LLM，工作区在 `m7verify/`、`m7verify-nolock/`（已 gitignore）。**跑过就是跑过，没跑就是没跑**，数字都是实测：

| # | 验的是什么 | 实测结果 |
|---|---|---|
| V1 | 环境变量外泄（A1） | 见 §9.10 的对照数字；真跑时 `sk-` 出现 0 次。顺带确认 `cat ../.env` 残留为真 |
| V2 | MCP 授权（A2） | 见上一节；`2/2 免确认` + 真实时间 |
| V3 | 检出 → 收紧 → 人解锁（B1–B4） | 轨迹有 `security_finding`（5 规则族 / 7 处命中 / 带行号 / `level=high` / **无原文摘录**）与 `gate_block`（`source=permissions` + 完整理由）。三条支线：从轨迹重算把 `high` 复原；`--clear-taint` 复位（learning 目标 `pending.md` → `learned.md`）；送出续跑指示后先前被拒的命令在第 3 步执行成功 |
| V4 | **不锁定**（误报政策的验收点） | 8 次工具调用 / **0 次 `gate_block`** / 1 次 `security_finding`，任务正常完成（写载荷文件 → 回读 → 写测试 → `pytest` 退出码 0）。同时真实验证 A3（模型自述收到了 read 结果里的告警横幅）与 B4（提炼落进 `learned.pending.md`） |

**这轮验证挖出两个「单测全绿但机制不工作」的真 bug**（commit `a455dda`，各带回归测试）：

1. **天花板空转**：bash 凭据判据要求「读动词 + 凭据路径」同现，而真模型用的是不在动词表里的 `findstr ... .env` → 天花板没生效、连 `gate_block` 都没有。**根因不是正则写错了，是我的测试和我自己的判据共享同一个盲区** —— 测试里用的是 `cat .env`。
2. **`--resume` 丢掉续跑指示**：拒绝文案指引「复位后重试」，但复位后 CLI 无法把话送进会话（`run_from` 用 `state.task`）→ 「收紧 → 人解锁 → 重试」动线断在最后一步。

两条的意义都在于：**它们是只有真跑真模型才会暴露的失败模式**，也正是「每条都要如实写明跑过/没跑过」这条纪律的价值所在。

### 9.12 agent/tools/web.py（M9-2 已实现：联网工具 + SSRF 拦截）

```python
MAX_FETCH_BYTES = 2_000_000     # 单次抓取上限；压缩前与解压后各算一次
COMPRESSION_HINT = "identity"   # 请求头里显式要求不压缩（服务端可以不听）
DEFAULT_MAX_CHARS = 12_000      # 回给模型的正文上限（对齐 TS 原版）
MAX_REDIRECTS = 5
ALLOWED_SCHEMES = frozenset({"http", "https"})

class WebFetchInput(BaseModel):
    url: str
    max_chars: int = Field(default=DEFAULT_MAX_CHARS, ge=500, le=200_000)

class WebSearchInput(BaseModel):
    query: str
    max_results: int = Field(default=5, ge=1, le=20)

def _blocked_reason(url: str) -> str | None          # None = 放行
def _is_internal(ip) -> bool
def _embedded_ipv4(ip: IPv6Address) -> IPv4Address | None
def _decompress(body: bytes, content_encoding: str) -> tuple[bytes | None, str | None]
def _html_to_text(raw: str) -> tuple[str, str]        # (标题, 正文)
class _GuardedRedirectHandler(urllib.request.HTTPRedirectHandler)
def build_web_tools() -> list[Tool]
```

- **分层边界**：本模块判「这个 URL **能不能碰**」（确定性，与权限无关）；权限层判「这次外发**要不要人点头**」（`high` 污染时收紧）。两者不重叠、也不互为备份。
- **判据支点 = 先解析、再判结果 IP**。判 `hostname` 字面量是漏的（实测 `localtest.me` → `127.0.0.1`）。**解析失败 → 拒绝（fail-closed）**，理由写进返回值（「域名解析失败（host）」），不是一句"不允许"。
- **地址分类的三处非显然判定**（判据表在 `tests/test_tools.py::test_is_internal_table`）：`::ffff:` 映射拆开递归判（`::ffff:100.64.0.1` 只有拆开才够得着 CGNAT，而 `::ffff:8.8.8.8` 必须放行）；CGNAT `100.64.0.0/10` 单列（`is_private`/`is_reserved` 都是 False）；NAT64/6to4 拆出内嵌 IPv4 再判（整段判会误拦合法映射，且诊断信息是错的）。
- **三条拦截位置各自的失效形态**：① 判据必须在**发请求之前**；② `_opener()` 必须真装 `_GuardedRedirectHandler`；③ **每一跳重定向都要重查**（只数跳数不校验目标等于不设防，一次跳转就够 —— `max_redirections` 对 SSRF 毫无作用）。
- **内容编码**：`Accept-Encoding: identity` **减少**服务端压缩的情况，但服务端可以不理会（实测 `python.org` 就是），所以必须真解压；`_decompress` 支持 gzip / deflate（zlib 包装与裸流两种都试），**解压后同样封顶 `MAX_FETCH_BYTES`**（原上限只管读进来的字节，压缩炸弹能解出几 GB）；**认不出的编码如实返回错误**，绝不退回原文。
- **正文提取**：不做 DOM 解析（要引依赖），只丢明显不是内容的块。**`header` 刻意不在丢弃表里**（不少站把 `<h1>` 放在里面）；`_TITLE` 必须在 `_DROP_BLOCK` **之前**取（丢弃表含 `head`，否则标题永远为空）。
- **搜索后端可切换**：`CODEAGENT_SEARCH_BACKEND`（`ddg` 默认，对齐 TS 原版 / `bing`），非法值回落默认。**查询词必须 `quote_plus`**（来自模型，是不可信输入）。解析不到结果时说「没有解析到结果」而不是「没找到相关内容」—— 后者会把"解析器失效"伪装成"这个词真的搜不到"。
- **`is_read_only() -> True`**：不改本地文件 → 可进只读并发批。这不与「它有副作用」矛盾：并发与否是**调度**问题，要不要人点头是**判定**问题（权限层看到的是「网络外发」）。
- **注册**：`build_web_tools()` 进 `ToolRegistry.default()`。**副作用**：`eval/runner` 也拿到这两个工具，token 从 62.8k 升到 82.5k（+31%），**判定结论不变** —— 见 README 的口径说明。
- 测试：`tests/test_tools.py`（+73 例）+ `tests/test_permissions.py`（+6 例，含「三类不可逆动作各有一个触发工具真的在 `default()` 里」这条不变量）。变异测试 31/31（`m9verify/mutate_m9_2.py`）。
- **如实标注**：默认后端 `ddg` 在本机连不通（直连超时 / 代理 SSL 中断），其解析正则**未经真实响应校准**；真跑走 `bing`。**只读并发批内，外发与产生污染的读同时执行**，该次外发按批前级别判定 —— 物理顺序，不是漏洞，见 `docs/architecture.md` §4。

### 9.13 agent/workspace.py（★M9-8：工作区快照 + 回滚）

**文件**：`agent/workspace.py`（814 行）；设计文档 `docs/design/m9-8_rewind.md`（含六条决议）。

它把一条**已经写进五处文档**的如实标注（"`--fork` 只分叉对话、工作区文件不回滚"）
变成"已修"，且回滚挂在**已有的 step 级检查点**上 —— `--rewind --step K` 与
`--fork --step K` 指的是**同一个 K**，这一点是结构性的，不靠约定。

```python
MANIFEST_DIRNAME = "ws";  SCHEMA = 1
_EXCLUDED_TOP = ("data", ".git")              # 不纳入管辖的顶层目录
_NO_SIDE_EFFECT_TOOLS = frozenset({...})      # 判据方向：默认"算有副作用"（多报，不漏报）

@dataclass(frozen=True)
class FileChange:        # 由工具报告（见 §2.1）；before=None 表示"改动前不存在"
    path: Path; before: bytes | None; base_unknown: bool = False
@dataclass(frozen=True)
class RestoreAction:     # kind ∈ {restore, delete, same, unresolvable}
    kind: str; rel: str; sha256: str | None; target_size: int; current_size: int | None
@dataclass(frozen=True)
class RestorePlan:       # 预览：改什么 + 不在管辖范围内的计数
    session_id: str; target_step: int; actions: tuple[RestoreAction, ...]
    outside: int; shell_calls: int; other_calls: int
    @property changes / unchanged        # changes = 真正会动盘的（排除 same）
@dataclass(frozen=True)
class RestoreReport:     # 执行结果：**逐文件**记录成败
    plan: RestorePlan; done: tuple[...]; failures: tuple[tuple[str, str], ...]
@dataclass(frozen=True)  class SnapshotUsage:  session_id / steps / manifest_bytes
@dataclass(frozen=True)  class ObjectStoreStats: objects / total_bytes / orphans / orphan_bytes / live
@dataclass(frozen=True)  class DropResult:  manifests_removed / objects_removed / bytes_freed / kept_shared
class SnapshotError(Exception): ...          # 消息要能直接给人看

def objects_root(workspace_root) -> Path                     # data/snapshots/objects/（全局）
def object_store_stats(workspace_root) -> ObjectStoreStats    # 含孤儿统计
def list_snapshot_sessions(workspace_root) -> list[SnapshotUsage]   # 新→旧
def drop_snapshots(workspace_root, session_id) -> DropResult  # 删清单 + 现算活跃集回收对象

class WorkspaceSnapshots:
    def __init__(self, workspace_root, session_id)
    def note_write(self, change: FileChange) -> bool          # 登记（只在工具成功后调用）
    def capture(self, step: int) -> Path | None               # 落清单+对象；没管过文件 → None
    def steps() / latest_step() / usage()
    def read_manifest(self, step) / import_manifest(self, step, payload)   # fork 搬清单用
    def plan(self, step: int | None = None) -> RestorePlan     # 纯读，不动盘
    def restore(self, plan: RestorePlan) -> RestoreReport
```

**存储布局**（决议 5：对象库**全局共享**、清单**按会话隔离**）：

```
data/snapshots/objects/{sha[:2]}/{sha}.bin     ← 全局共享的内容寻址对象库
data/checkpoints/{sid}/ws/step-{K}.json        ← 每步一份的**全量清单**
```

**为什么对象全局共享**（这条是实测推翻初稿的）：E 动线实测 `--fork` 的**对象增量为
0 个 / 0 字节**（只有清单 +599 B）—— 项目初始文件、被改回原样的内容全都命中同一份
对象。按会话隔离会让同一份内容在每个会话里各存一遍。代价有两条且都认了：
① 回收不能整目录删 → `drop_snapshots` **现算 live set**；② 并发写同一目标 →
见下面"不需要锁"。

**三条不变式**（每条都有测试钉着）：

1. **`path ∈ manifest[K]` ⟺ `first_touch(path) ≤ K`**。于是清单是**全量**的：只要
   `K >= first_touch`，还原所需信息全在 `manifest[K]` 里，不用往前翻。代价是每步多写
   一份清单（几十行 JSON），换来的是**每一步都能独立还原** —— "按序重放前像"的方案
   在中间缺一环时会**静默还原出错**。
2. **`base`（"我们碰它之前它长什么样"）只记在它首次出现那一步的清单里**。没有它，
   "回滚到第一次修改之前"就只能**删掉那个文件** —— 而它可能是仓库里人写的、我们并不
   认识的文件。那是一次静默的数据破坏。
3. **写盘顺序：对象 → 清单 → 检查点**。`capture` 整个跑完 `Session._write` 才落检查点。
   任何一步被杀，留下的都只能是**孤儿**（不可达的字节），不能是**说谎的引用**
   （检查点说有快照、快照却不存在）。孤儿**如实报出、不自动删**。

**`plan(K)` 的四支，`K < lo` 那支是必须单独有的**：

| K | 语义 |
|---|---|
| `K` 落在已有快照步上 | 用 `manifest[K]` 的盘面（不变式 1） |
| `K = None` | 最近一个**有快照**的步（**不是**最近检查点：M9-8 之前的会话有检查点没有快照） |
| `K < lo`（`lo = min(manifests)`，含 `--step 0`） | **撤销我们做过的一切**：所有受管路径回到各自的 `base` |
| `lo <= K` 但该步没落快照 | **报错**（`第 K 步没有工作区快照。可用: [...]`），不猜 |

**`K < lo` 不是"顺手兼容"**：节拍是 5 时第一个清单落在第 5 步，而 `plan(5)` 读的是第 5
步**盘面**（写完之后的），于是"撤销 agent 做的一切"这件事在最常见的形状下**根本表达
不出来** —— 记了 `base` 却没人能用它。**没有这一支，`base` 是结构上不可达的。**
夹在两快照中间的步（快照在 5 和 10、要回到 7）**仍然报错**：那是"不知道"，不是"没有"。

**"不在管辖范围"是独立维度，分开计数**（决议 6）：`RestorePlan` 同时带 `outside`
（没被 `write`/`edit` 碰过的文件数）、`shell_calls`（回滚区间内 `bash` 调用数）、
`other_calls`。**把"文件系统回滚"与"不可回滚的外部副作用"混为一谈是推卸责任** ——
所以 `bash` 的 `rm`/`mv`/重定向、MCP 的写入、目录增删、元数据（权限位/mtime）、
进程外的一切都**明说不管**，且这三个数**跟着预览一起印出来**。`outside` 的口径写死
在一处（递归跳过 `GrepTool.SKIP_DIRS` 与点开头目录再减受管集合）：**口径含糊的计数
比不报还糟**。区间是**左开**的 `(target_step, 最新检查点]`，数据源是最新检查点的
`state.events`（`dump_state` 落全字段、事件不截断）。

**不需要锁（决议 7，实测支撑）**：对象与清单只由**父线程**写 —— 写工具不是只读工具，
走 `_execute_tool_calls` 的串行分支；子代理（worker）的引擎 `session=None`，结构上
拿不到本模块（与"worker 写不了检查点"是同一条保证，不另加守卫）。全局共享对象库理论上
有并发写同一 `{sha}.bin` 的可能，实测（`m9verify/drive_m9_8.py` E 动线，8 线程 × 5 轮，
六次独立重跑）**裸 `os.replace` 冲突 24~30/40 次（全是 Windows `PermissionError(13)`），
而经 `_put_object` 未捕获异常 0 次** —— 容错就是"看目标在不在"（内容寻址，同 sha 必然
同内容），**不是加锁**。如实标注：Linux 上 `os.replace` 到同一目标不抛，那条容错分支
可能是死代码。

**`--rewind` 默认只预览、二次确认**（决议 2）：`plan()` 是纯读的，输出 git-diff 风格的
预览（`恢复/删除/不变/无法还原` 四类 + 三个"不在管辖范围"的数），确认框**默认 n**。
理由不是保守：回滚是**全项目唯一一个不可逆的写操作**（对象库留了旧字节，但被覆盖的
文件本身没有 undo）。`--force` 跳过此问；没有 changes 时不弹框但仍返回步号（调用方靠它
拼"继续"那句 `--resume --step K`）。REPL 里是 `/rewind [step]`，`_confirm_rewind` 用
`input()` 而**不是** `typer.prompt`（常驻模式里后者会和自己的行读取打架）。

**回滚之后必须告诉模型**（`app/cli.py::_reinject_rewind`）：模型历史里写着"我改了 a.txt"，
而盘上那改动已经没了，它会基于**不存在的现状**往下推理。触发判据是 `state.step > target`
（等于或早于时历史与盘面一致，补投反而是噪音，还会平白改一次消息前缀、破缓存）。
`last_rewind` 落 meta 而**不是**只打印 —— 打印的字留在上一个进程的屏上，而下个进程要读文件；
`Session._load_payload` 把它取回 `state.last_rewind`，与 `_reinject_plan`/`_reinject_goal`
同一条纪律。**同理，`--fork` 那句"工作区文件不会回滚"必须跟着 `--rewind` 分叉写** ——
M9-8 之前它无条件说是真的，现在带着 `--rewind` 时它会变成假话。

**`--fork --step K --rewind`**（决议 3，P1 的"倒带重试"黄金组合）：fork 搬**对话**
（检查点 1..K + 轨迹里 `step <= K` 的行 + `forked_from`），rewind 搬**文件**。
`Session._copy_snapshots` 只复制 `ws/step-n.json`（n ≤ K）、**一个对象都不复制** ——
对象库全局共享，分叉带过来的清单里的 sha 在原库仍可读。这也是"删掉源会话会不会弄坏
分叉"的答案：不会，`drop_snapshots` 的活跃集按**所有**清单算，分叉的清单算在内。
分叉点之后的清单**不搬**是对的：那是源会话在分叉点之后的历史，搬过去就是**说谎的记录**。

- 测试：`tests/test_workspace.py` **49 例** + 接线契约 22 例（cli 8 / repl 6 / session 8）；全量 **707 全绿**（M9-8 当时的数字；M5-5 之后为 **769**）。变异测试
  **35/35 零 SKIP 零 MISS**（`m9verify/mutate_m9_8.py`），其中五处是**证明后不设**的候选。
  边界测试逐条点名见 `docs/design/m9-8_rewind.md` §7。
- **真实动线 A~F 全部有日志**（`m9verify/drive_m9_8.py`，真工作区 `m9verify/ws_m98/`）：
  A 写盘产生快照 → B 预览不动盘（拒绝两次后断言盘面逐字节不变）→ C 真回滚 → D fork+rewind
  → E 磁盘实测 → F `--drop-snapshots` 回收。**实测数字**：基座 1949 B / 8892 B = **21.9%**
  （基座 = 被 `write`/`edit` 碰过的文件原始字节之和，**不是整个工作区**，只取决于"改了几个
  文件"，这就否掉了"给基座加速开关"的必要性）；清单 599 B vs 对象 4339 B ≈ 13.8%，孤儿
  0 个；fork 增量 0 对象 / 0 字节；回收 1 个对象 / 746 B，3 个因别的会话仍引用而保留。
- **这些数字与 README 的 token/成本/缓存数字没有任何关系，不可混着比**（口径见驱动脚本
  docstring：本机 Windows 10 + NTFS，夹具从 `ws_m97` 拷来）。

---

## 10. 验收总命令

```bash
python -m pytest tests/                       # 全部测试
python -m app.cli --mock "读 README 并总结项目结构"   # 无 key 演示（M1 末可用）
python -m app.cli "给 README 加一行说明并验证"     # 真实 DeepSeek（需 .env 配 key）
python -m eval.golden_tasks --clone --limit 10   # M5-1：拉 tinydb 并列出真实 fix 提交
python -m eval.runner --limit 39 --mock          # M5-5：闸门逐条流式判定 + 报告（零成本）
python -m eval.runner --limit 39 --arm agent     # M5-5 三臂：完整循环（max_steps=25）
python -m eval.runner --limit 39 --arm one-step  # M5-5 三臂：只跑一步（其余全同）
python -m eval.runner --limit 39 --arm single-shot  # M5-5 三臂：无工具、一次调用出 diff
python -m app.cli --mcp .codeagent/mcp.json "任务"  # M6-3/M9-4 加载 MCP server（stdio 或 HTTP）后执行任务
python -m app.cli --sessions                    # M9-3 会话清单：名字 / 步数 / 分叉来源（无 key）
python -m app.cli --rename "基线方案"             # M9-3 起名（只动元数据，无 key）
python -m app.cli --fork --step 3 "换个思路"      # M9-3 从第 3 步分叉并续跑（**对话**分叉，文件不动）
python -m app.cli --snapshots                   # M9-8 会话快照清单 + 全局对象库占用 / 孤儿数（无 key）
python -m app.cli --rewind --step 3             # M9-8 回滚预览（默认**只预览**，不确认不动盘）
python -m app.cli --rewind --step 3 --force     # M9-8 真回滚工作区到第 3 步（不可逆）
python -m app.cli --fork --step 3 --rewind --force   # M9-8 "倒带重试"：对话 + 文件一起回到第 3 步
python -m app.cli --drop-snapshots --session-id <id> # M9-8 删该会话快照 + 回收无人引用的对象
streamlit run app/ui_streamlit.py               # M2-3 控制台（含 M5-3 检查点回放）
```
