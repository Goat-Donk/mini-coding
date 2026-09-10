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
    settings: dict = field(default_factory=dict)   # 来自 session 的配置
    emitter: Callable[[dict], None] | None = None  # 轨迹事件回调（M3 接 session）
    permissions: object | None = None              # M2 接入
    hooks: object | None = None                    # M2 接入
```

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
    # execute:
    #   1) content = read
    #   2) count = content.count(old_string)
    #      0 → fail(f"old_string 未在文件中找到。请先用 read 查看实际内容，注意缩进/换行/转义，再重试。当前文件共 {lines} 行。")
    #      >1 且 not replace_all → fail(f"old_string 在文件中出现 {count} 次，不唯一。请包含更多上下文行使其唯一，或传 replace_all=true")
    #   3) new = content.replace(old_string, new_string)（replace_all=True 时替换全部）
    #   4) diff = difflib.unified_diff(old_lines, new_lines, fromfile=f"a/{path}", tofile=f"b/{path}", lineterm="")
    #   5) 写回文件；output = "编辑完成，diff：\n" + diff（diff 展示给模型确认改动）
    #   data = {"path", "replacements": count if replace_all else 1}
```

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
    # 方法:
    #   record_event(type: str, **data)  → 加 ts/step 后 append，若设了 emitter 则回调
```

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

### 6.3 compact 流水线（M3-3 已实现：warning 带 → 确定性 snip；critical → LLM 摘要 + snip 兜底）

`prepare()` 实测触发逻辑（两级阈值带）：
```python
self.last_stats = self.account(state)
if self.last_stats.utilization >= self.compact_threshold:   # ≥0.85 critical/blocked
    self._compact_with_summary(state, state.messages)       # LLM 摘要，失败退化 snip
elif self.last_stats.utilization >= self.snip_threshold:    # 0.70~0.85 warning
    self._snip(state, state.messages)                       # 确定性裁剪（无 LLM，成本≈0）
return state.messages
```

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
   - 两种 compact 都会把保留消息的 usage 标记 stale：`state.usage_stale_reason = "snip_compact" | "llm_compact"`。

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
    terminated_reason: str          # "completed" | "max_steps" | "loop_detected" | "error"
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

    def _execute_tool_calls(self, calls: list[ToolCall], state: AgentState, ctx: ToolContext):
        # ★ 只读并发：若 calls 全部是只读工具 → ThreadPoolExecutor(max_workers=min(4,len)) 并发
        #   否则串行（保持顺序）
        # 每条：tool = registry.get(name)（未知 → ToolResult.fail(可用工具列表)）
        #       result = tool.run(arguments, ctx)
        #       state.record_event("tool_call", name, arguments, result 摘要, duration)
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

### 8.2 app/ui_streamlit.py（M2-3 控制台 v1）

**文件**：`app/ui_streamlit.py`；运行 `streamlit run app/ui_streamlit.py`（无 key 自动 Mock 演示）。

**架构（worker 线程 + 事件队列轮询）**：
- QueryEngine 在后台线程跑（daemon）；用真实 `Session(workspace, new_session_id(), on_event=events_q.put)` ——
  `Session.emit` 同一通道双写：轨迹 JSONL 落盘 + on_event 回调推入 `queue.Queue`（UI 实时流式渲染），
  监听器异常被 try/except 吞掉不影响轨迹落盘
- 主线程每次脚本运行 `get_nowait()` 排空队列 → 追加到 `session_state["log"]` → 渲染；running 中 `sleep(0.3) + st.rerun()` 轮询；**空闲状态不 rerun**（AppTest 无头测试必需，否则初始渲染死循环）
- **权限确认桥 ConfirmBridge**：worker 的 confirm 回调 `answers.get()` 阻塞等待；UI 读到 `confirm.current` 渲染 5 按钮（允许本次/本回合/总是/拒绝本次/总是），点击把粒度字符串 `answers.put` 并 rerun；新任务重置桥
- hooks：内置 `require_tests_before_commit`（git commit 前检查 data/tests_pass.marker）
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
- `PermissionsEngine.check(tool_name, arguments, ctx) -> 决策`；ask → 回调用户确认

### 9.2 agent/hooks.py（M2）
- `HookContext`：event_name / tool_name / arguments / result / state
- `HookEngine.run_pre(...) -> None | (block, reason)`；`run_post(...)`；示例 hook：`require_tests_before_commit`（PreToolUse 包 Bash(git commit)，检查 `data/tests_pass.marker` 文件，不存在 → block 返回"先跑 python -m pytest 验证"）
- 提供 mark_tests_pass() 工具/函数写 marker（可由 hook 自身或单独工具）

### 9.3 agent/session.py（M3-4 已实现：JSONL 轨迹 + 检查点 + resume）

```python
def new_session_id() -> str: ...                      # "s%Y%m%d-%H%M%S"

class Session:
    def __init__(self, workspace_root: Path, session_id: str, *,
                 checkpoint_every: int = 5, on_event=None): ...
    def emit(self, event: dict) -> None               # on_event 转发 + append JSONL
    def checkpoint(self, state: AgentState) -> None   # 每 N 步写一次（_ticks 计数，恢复后重新数）
    def _write(self, state: AgentState) -> Path       # 原子写：.json.tmp → replace
    @classmethod
    def from_checkpoint(cls, workspace_root, session_id, *, step=None,
                        checkpoint_every=5) -> tuple[Session, AgentState]  # step=None → 最近
    def list_checkpoints(self) -> list[int]
    def latest_checkpoint(self) -> Path | None
def latest_session(workspace_root: Path) -> str | None  # 按检查点 mtime 选最近会话
```

- 轨迹文件 `data/sessions/{id}.jsonl`（append-only；M3-3 compact 裁掉的中段消息仍完整保留，
  snip/摘要标记都指向这里）；检查点 `data/checkpoints/{id}/step-{N}.json` 含
  messages / events / usage / last_usage / usage_stale_reason / terminated_reason / memory_blocks。
- **resume 不重置 step 计数**：恢复的 state.step 延续 → 续跑产生的检查点不会覆盖恢复前的同名文件。
- loop 接入：`QueryEngine(session=...)`；`run()` 建 state 时
  `session_id = getattr(self.session, "session_id", "m1")`；每轮 `_execute_tool_calls` 后
  `session.checkpoint(state)`；`run_from(state)` 从恢复的 state 继续执行。
- CLI：`--resume`（自动选最近会话）/ `--session-id` / `--step` / `--checkpoint-every`。

### 9.4 agent/memory.py（M4，分层指令文件 + 提取，MiniCode 简化移植）
- **分层指令文件**：在工作区根（workspace_root）发现候选 `CODEAGENT.md` / `MINI.md` / `CLAUDE.md` + `.codeagent/rules/*.md`（不做全局 home 层，只做项目层）。
- `@include` 解析：`@相对路径` 行递归读取；拒绝绝对路径/`..`；循环检测；缺失给占位注释。
- 内容 hash 去重（靠后/靠 workspace_root 的优先）；预算渲染：每文件 ≤8K 字符、总计 ≤20K，超限截断 + 提示。
- 任务结束提取：llm.complete 从轨迹提炼"仓库约定/经验" → 去重 → 追加/更新记忆文件（少而精、只记可复用约定）。
- 跨会话注入：新会话把记忆文件内容注入 system_prompt（loop 已支持 memory_blocks）。
- 简化 consolidation：去重 + 合并相似条目（不 prune 不调度）。

### 9.5 agent/tools/subagent.py（M4）
- `SubagentInput{task, tools: list[str] = ["glob","grep","read"]}`（只读工具受限）
- 独立子循环：新 AgentState + 只读 registry + 自己的 system prompt（"你是研究子代理，只读探索，返回结构化结论"）
- 返回：`ToolResult.ok(结论报告)`；主循环上下文只收 Z 结论（CC SubAgent 经济学）
- max_steps 子代理 10 步上限

### 9.6 eval/（M5）
- golden_tasks.py：从 tinydb 的 git history 找真实 bug 修复 commit → 任务 = "修复 <commit前> 的 bug"，隐藏判定 = 该 commit 的测试
- runner.py：每任务起新会话（git checkout 干净工作区）→ 跑 agent → 跑测试判分 → 报告（完成率 + token/耗时/缓存成本 + 与基线 diff）

---

## 10. 验收总命令

```bash
python -m pytest tests/                       # 全部测试
python -m app.cli --mock "读 README 并总结项目结构"   # 无 key 演示（M1 末可用）
python -m app.cli "给 README 加一行说明并验证"     # 真实 DeepSeek（需 .env 配 key）
```
