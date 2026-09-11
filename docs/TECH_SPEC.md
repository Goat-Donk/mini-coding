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
  ├── session.py    (轨迹/检查点/resume —— M3 补)
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
    # 支持 __iadd__ / __add__（累加到会话总 usage）

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
    @staticmethod
    def ok(output: str, data: dict | None = None) -> "ToolResult"
    @staticmethod
    def fail(error: str, output: str | None = None) -> "ToolResult"
        # fail 时 output 默认 = f"工具执行失败: {error}"（回喂模型，可自修复）
```

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
```

> **`state` 曾经是"声明了但没人填"的缺口**（`emitter` 也一样：两个槽位都声明了，而
> `loop.py` 构造 `ToolContext` 时一个都没传）。工具作者照声明去读会拿到 `None`，
> **而且不报错** —— 直到 M8 的 `update_plan` 成了第一个真读者，这个缺口才暴露。
> 原 `settings: dict` 字段已删除：全项目没有任何读者，是同一个缺口的另一半。
> 现在 `tests/test_plan.py::test_context_state_is_filled_by_the_real_loop` 钉住它。

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
    last_usage: Usage | None = None                  # M3 provider 锚点（compact 后置 stale）
    usage_stale_reason: str | None = None            # "tool_output_truncated"|"snip_compact"|"llm_compact"
    taint: str = TAINT_NONE                          # M7 会话级污染标记（只升不降）
    emitter: Callable[[dict], None] | None = None     # 运行时回调，**不进检查点**
    # 方法:
    #   record_event(type: str, **data)  → 加 ts/step 后 append，若设了 emitter 则回调
    #   raise_taint(level) / clear_taint(reason)      → 只升不降；复位只由人的动作调用
```

> `plan` **不是**从事件派生的值（对比 `taint`：那个由轨迹里的 `security_finding` 重放得出），
> 它就是权威状态本身 —— 所以没有对应的 `derive_plan`。检查点是按 `dataclasses.fields()`
> **全字段**快照的，加字段自动进检查点；`tests/test_session.py` 有一条从 `fields()` 反推
> 字段全集的往返测试，**加字段时它会失败**，提醒你把新字段填进测试的 `values`。
> `emitter` 是唯一的例外（运行时对象，故意不落盘）。

### 5.3 边界

- tool_calls 的 arguments 在 `assistant_tool_calls` 里统一 `json.dumps(ensure_ascii=False)`。
- content=None 是合法 assistant tool-use 消息（OpenAI 要求）。

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
    terminated_reason: str          # "completed" | "max_steps" | "loop_detected" | "error" | "await_user"
    task: str

class QueryEngine:
    def __init__(self, llm: BaseLLM, registry: ToolRegistry, *,
                 workspace_root: Path,
                 system_prompt: str | None = None,     # None → 内置默认
                 max_steps: int = 25,
                 loop_detection_window: int = 4,       # 最近 N 步工具签名相同即停
                 memory_blocks: list[str] | None = None,   # M4 注入
                 context: ContextManager | None = None,
                 session: object | None = None):       # M3 接 session（轨迹/检查点）
    def run(self, task: str, *, cwd: Path | None = None) -> RunResult:
        # 1) state = AgentState(...)；system_prompt 拼装（见 7.2）
        # 2) while state.step < max_steps:
        #      messages = self.context.prepare(state)（M1=原样）
        #      result = llm.chat(messages, registry.schemas())
        #      state.usage += result.usage; state.step += 1
        #      state.record_event("llm_call", tool_calls=[...], usage=..., step=state.step)
        #      if result.tool_calls:
        #         loop 检测（7.3）
        #         execute_tool_calls(result.tool_calls, state, ctx)
        #         continue
        #      else:
        #         state.terminated_reason = "completed"
        #         return RunResult(final_text=result.content, ...)
        # 3) 循环外：terminated_reason = "max_steps"（或 loop_detected）
        #     final_text = "已达到最大步数 / 检测到重复循环，任务中止"

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

---

## 8. app/cli.py（◐M1 最小版 → 后续增强）

**文件**：`app/cli.py`
**M1 目标**：`python -m app.cli "任务"` 能用真实 DeepSeek 跑通；`python -m app.cli --mock "任务"` 无 key 演示。
- `load_dotenv()`；构建 DeepSeekClient / MockLLM（--mock 或自动降级提示）
- ToolRegistry.default(workspace_root)；QueryEngine 组装
- 打印：每步事件（工具调用名+参数摘要+结果截断）+ 最终结论 + 总 token/步骤
- **M2+**：加 `--resume`、权限 ask 交互、`--checkpoint-dir`（M3）

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

---

## 9. 后续里程碑模块规格（占位，届时展开补全）

### 9.1 agent/permissions.py（M2，含 MiniCode 决策粒度）
- **决策粒度**（替代原 allow/deny/ask 三态）：`allow_once / allow_turn / allow_always / deny_once / deny_always / ask`。ask 时 CLI/Streamlit 弹确认，用户选一次性/本回合/总是/拒绝。
- 规则文件 `permissions.json`：`{"tools": {"bash": {"dangerous": "ask"}}, "commands": {...}, "paths": {"allow": [...], "deny": [...]}}`
- 三类请求：**path**（读/写/列/搜）、**command**、**edit**；每类 allowlist/denylist + 决策记忆（once/turn/always 落内存或文件）
- 危险命令黑名单从 bash.py 提取共享；路径沙箱复用 files._resolve 语义
- `PermissionsEngine.check(tool_name, arguments, ctx, *, details=None) -> 决策`；ask → 回调用户确认

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
_FIELD_DECODERS = {"usage": ..., "last_usage": ...}   # JSON dict → Usage
_LEGACY_FLAT_KEYS: tuple[str, ...]           # M7 之前的平铺格式（冻结，只读老文件）

def dump_state(state: AgentState) -> dict    # 全字段快照；不可序列化→报字段名
def load_state(payload: dict, session_id: str) -> AgentState   # 新旧格式都吃
def state_dict(payload: dict) -> dict        # 读者入口：兼容两种格式取 state
def latest_session(workspace_root: Path) -> str | None  # 按检查点 mtime 选最近会话
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
- **它是对话分叉，不是工作区分叉** —— 必须说清楚，否则极容易被读成"时光倒流"：
  工作区（文件）**不会**回滚到第 K 步的样子，分叉后的 agent 看到的是**当前**工作区。
  我们**没有**工作区快照机制（`grep rewind|snapshot` 在 `agent/ app/` 下零命中，2026-09-11
  核实）。CLI 分叉后把这句**打印出来**，help 里也写明。
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

### 9.5 agent/tools/subagent.py（M4-2 已实现：research 子代理）

```python
class SubagentInput(BaseModel):
    task: str
    tools: list[str] = ["glob", "grep", "read"]   # 只读白名单
    max_steps: int = 10

class SubagentTool(Tool):
    name = "subagent"  # is_read_only() = False（串行执行，避免嵌套并发）
    def __init__(self, llm, workspace_root): ...
    def execute(self, args, ctx) -> ToolResult: ...
    @staticmethod _restricted_registry(requested: list[str]) -> ToolRegistry
```

- **独立上下文**：子代理用全新 AgentState + `SUBAGENT_SYSTEM_PROMPT`（只读定位），
  主循环 messages/usage/compact 全不共享；内部复用 QueryEngine（同一 LLM），代码零重复。
- **只读受限**：默认 glob/grep/read，写工具/bash 一律不给；`_restricted_registry` 永不包含
  subagent 自身 → 天然禁止递归嵌套；未知名字静默忽略。
- **返回**：`ToolResult.ok(结论报告)`（主上下文只收 Z tokens）；子代理失败/超步如实
  `ToolResult.fail("[子代理 max_steps，N 步] …")`，不假装成功，主模型可据此调整。
- **接入**：cli/ui 引擎组装处 `registry.register(SubagentTool(llm, workspace_root))`
  （不能进 ToolRegistry.default——需要 llm/workspace 构造参数）。
- 测试：tests/test_subagent.py（6 个：跑通回报告/只读白名单+不改文件/max_steps 兜底/
  system prompt 定位/禁止递归/串行注册）。

### 9.6 eval/golden_tasks.py（M5-1 已实现：SWE-bench 思路的黄金任务集）

```python
TINYDB_REPO = "https://github.com/msiemens/tinydb.git"
DEFAULT_REPO = Path("eval/repos/tinydb")
FIX_KEYWORDS = ("fix","fixes","fixed","bug","error","crash","issue","regression","broken","incorrect")

def git(repo_dir: Path, *args) -> str                          # 包装 subprocess git
def ensure_repo(repo_dir=DEFAULT_REPO, *, clone_url=TINYDB_REPO) -> Path  # clone（3 次重试）
def discover_fix_commits(repo_dir, *, limit=20, keywords=FIX_KEYWORDS) -> list[dict]
def render_task_text(commit: dict) -> str                      # 真实 bug 报告（subject+body，不造假）
@dataclass GoldenTask: id, base_sha, title, task_text, changed_sources, hidden_tests
def build_task(repo_dir, commit) -> GoldenTask                 # base_sha = commit^
def materialize(task, target_dir, repo_dir) -> Path            # git worktree add --detach
def remove_worktree(repo_dir, target_dir)
@dataclass JudgeResult: passed, returncode, summary
def judge(task, workspace) -> JudgeResult                      # hidden tests 覆盖写回 → pytest
```

- **任务构造**：从 tinydb git history 找 subject 含 fix 关键字的提交，且**同时改源码与 tests/**。
  `base_sha = fix 提交的父提交`（= bug 存在状态）；`task_text` = 该提交的 subject+body
  （真实 bug 报告，绝不编造）；`hidden_tests` = fix 提交里 tests/ 的新内容——agent 全程看不到，
  判定用。避免 SWE-bench 两个陷阱：测试泄漏（隐藏测试只在 judge 时覆盖写回）与任务描述失真。
- **物化**：`git worktree add --detach base_sha` 出干净工作区（不污染主仓库）；
  完成后 `remove_worktree`。judge：hidden_tests 写回 `tests/` → `subprocess pytest` 判定。
- 测试：tests/test_golden_tasks.py（5 个）+ conftest `fixture_repo`（本地迷你仓库离线造
  buggy → fix → 非 fix 提交，不联网）。

### 9.6b eval/runner.py（M5-2 已实现：回归报告）

```python
PRICE_INPUT_HIT_CNY = 0.5;  PRICE_INPUT_MISS_CNY = 2.0;  PRICE_OUTPUT_CNY = 8.0  # DeepSeek 元/M
DEFAULT_WS_ROOT = Path("data/eval/ws")

@dataclass TaskResult: task, passed, run, error, duration_s, cost_cny
def estimate_cost_cny(prompt_hit, prompt_miss, completion) -> float
def _build_llm(mock) -> BaseLLM        # mock → MockLLM.text（无 key 冒烟验证管线）
def run_single(task, repo_dir, *, ws_root, mock) -> TaskResult
def run_eval(repo_dir=DEFAULT_REPO, *, ws_root, limit=5, mock=False) -> dict
def main()                              # CLI: --repo / --ws-root / --limit / --mock / --keep
```

- **流程**：物化 → QueryEngine 跑 task_text 修 bug → judge 隐藏测试 → remove_worktree。
  agent 异常 / judge 异常**如实记入 `error` 字段**（不假装成功），工作区 always 清理。
- **成本**：按 DeepSeek 公开定价估算，用量取 provider 实测
  `prompt_cache_hit / prompt_cache_miss / completion` tokens（provider-usage-first）。
- **报告**：ts / mode(mock|deepseek) / tasks / passed / completion_rate / total_tokens /
  total_cost_cny / avg_cache_hit_ratio / per_task[]（id/title/passed/error/duration_s/steps/
  tokens/cost_cny），落 `data/eval/report-*.json`。
- 测试：tests/test_eval_runner.py（3 个：定价函数 / mock 冒烟整管线 / 报告字段可 JSON 落盘）。

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

### 9.8 agent/mcp.py（M6-3 已实现：MCP 客户端，有余力项）

```python
PROTOCOL_VERSION = "2025-06-18";  DEFAULT_TIMEOUT = 20.0

class MCPError(RuntimeError): ...

class MCPClient:
    def __init__(self, command: list[str], *, name="mcp", timeout=20.0, cwd=None): ...
    def start(self) -> MCPClient          # Popen + initialize 握手 + initialized 通知
    def list_tools(self) -> list[dict]    # tools/list → [{name, description, inputSchema, annotations}]
    def call_tool(self, name, arguments) -> ToolResult   # tools/call → content 拍平成文本
    def close(self) -> None               # 关 stdin → terminate → kill 兜底
    def __enter__/__exit__                # with 用法
    server_info: dict;  protocol_version: str | None

class MCPToolAdapter(Tool):
    input_model = BaseModel               # 占位：schema/run 均覆写
    def is_read_only(self) -> bool        # 只信 server 的 annotations.readOnlyHint，默认 False
    def is_external(self) -> bool         # True：第三方工具，权限引擎默认不放行（M7）
    def schema(self) -> dict              # 用远端 inputSchema，不从 pydantic 生成
    def run(self, arguments, ctx) -> ToolResult   # 跳过本地校验，参数原样透传

def load_mcp_servers(config_path, registry, *, workspace_root=None)
        -> tuple[list[MCPClient], list[str], list[str]]   # (clients, 注册名, 已授权名)
def _is_allowed(raw_name, final_name, patterns) -> bool    # 远端名或注册名命中 allow 即免确认
def _flatten_content(content) -> str      # text 拼接；resource/其它类型给可读占位（不静默丢）
```

- **为什么手写而非用官方 `mcp` SDK**：官方 SDK 是 async（anyio），而 QueryEngine 是同步循环，
  为一个工具把整条循环改成 async 不划算；MCP stdio 就是「JSON-RPC 2.0 按行分隔」，
  协议面很窄（3 个方法），手写更透明且不引入新依赖。
- **传输实现**：stdout/stderr 各一个后台线程抽干（管道阻塞读没法设超时，Windows 上
  `select` 也不支持 pipe）；stdout → 队列，请求按 `id` 关联响应；stderr → 环形缓冲
  （40 行），出错时把 server 的真实报错带进异常信息，而不是干巴巴一句"超时"。
- **安全边界（重要）**：MCP 工具来自第三方 server，**不受 workspace 沙箱约束**。所以
  ① 必须显式配置（`--mcp`）才注册，不进 `ToolRegistry.default`；
  ② 只读性只信 server 声明的 `readOnlyHint`，没声明就当可写（串行，绝不并发跑未知副作用）；
  ③ 它们**照样走 `_gate_and_run` 门禁链**——hooks 与权限引擎对 MCP 工具同样生效；
  ④ **权限默认不放行**（M7）：`is_external()` 为真的工具由 `_classify` 归到 `external`
  类，`_rule_check` 命中 `rules["external"]["allow"]` 才 `ALLOW`，否则 `ASK`。
  这正是把权限/钩子做成独立层的回报：接入新工具来源无需改循环。
- **配置**（`.codeagent/mcp.json`）：
  `{"servers": {"<别名>": {"command": ["python","-m","some_server"], "timeout": 20, "allow": ["工具名"]}}}`
  `allow` 是**免确认白名单**，支持 fnmatch（`"e*"` / `"*"`）。匹配在 `load_mcp_servers`
  里做，因为只有那一处同时知道「远端名」与「注册名」（重名时后者加了 `<别名>__` 前缀）——
  否则用户得去猜前缀，配置就成了实现细节的泄漏。`load_mcp_servers` 返回的第三个值
  （已授权名列表）由入口交给 `PermissionsEngine.allow_external()`；它与规则文件里的
  `external.allow` 是**并集**，所以「先加载 mcp 还是先加载规则文件」不影响结果。
- **容错**：单个 server 启动失败/崩溃只打印 stderr 提示并跳过，不影响其它 server 与主流程
  （MCP 是增强项，不该成为启动路径上的单点故障）；与内置工具重名时加 `<别名>__` 前缀，
  **不静默遮蔽**内置工具。客户端由调用方 `close()`（子进程 + 管道是资源，不等 GC）。
- 测试：tests/test_mcp.py（14 个）+ tests/fake_mcp_server.py——**真子进程、真管道、
  真 JSON-RPC**（不是 mock）：握手/列工具/成功·isError·未知工具/只读注解透传/
  跳过本地校验/重名加前缀/坏 server 不炸主流程/崩溃与超时诊断/**allow 白名单语义**/
  **经真实 QueryEngine 循环调用 MCP 工具**。权限侧的判定与拒绝文案在
  tests/test_permissions.py（外部工具默认 `ASK`、授权后 `ALLOW`、裸名与加前缀名都能配、
  拒绝理由含出处与解除方式）。

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

---

## 10. 验收总命令

```bash
python -m pytest tests/                       # 全部测试
python -m app.cli --mock "读 README 并总结项目结构"   # 无 key 演示（M1 末可用）
python -m app.cli "给 README 加一行说明并验证"     # 真实 DeepSeek（需 .env 配 key）
python -m eval.golden_tasks --clone --limit 10   # M5-1：拉 tinydb 并列出真实 fix 提交
python -m eval.runner --limit 3                  # M5-2：真实跑 3 个黄金任务出回归报告
python -m eval.runner --limit 2 --mock           # M5-2 无 key 冒烟（judge 会如实失败）
python -m app.cli --mcp .codeagent/mcp.json "任务"  # M6-3 加载 MCP server 后执行任务
python -m app.cli --sessions                    # M9-3 会话清单：名字 / 步数 / 分叉来源（无 key）
python -m app.cli --rename "基线方案"             # M9-3 起名（只动元数据，无 key）
python -m app.cli --fork --step 3 "换个思路"      # M9-3 从第 3 步分叉并续跑（对话分叉）
streamlit run app/ui_streamlit.py               # M2-3 控制台（含 M5-3 检查点回放）
```
