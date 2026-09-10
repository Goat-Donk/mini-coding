"""M1-3 tests: agent/llm.py（Usage / ToolCall / MockLLM / DeepSeekClient 构造）。"""
import pytest

from agent.llm import (
    BaseLLM,
    DeepSeekClient,
    LLMResult,
    MockLLM,
    ToolCall,
    Usage,
)


# ---------- Usage ----------

def test_usage_total_and_ratio():
    usage = Usage(
        prompt_tokens=1000,
        completion_tokens=200,
        prompt_cache_hit_tokens=900,
        prompt_cache_miss_tokens=100,
    )
    assert usage.total_tokens == 1200
    assert usage.cache_hit_ratio == 0.9


def test_usage_ratio_zero_denominator():
    usage = Usage(prompt_tokens=100, completion_tokens=50)
    assert usage.cache_hit_ratio is None


def test_usage_iadd_accumulates():
    a = Usage(prompt_tokens=10, completion_tokens=5, prompt_cache_hit_tokens=8, prompt_cache_miss_tokens=2)
    b = Usage(prompt_tokens=20, completion_tokens=10, prompt_cache_hit_tokens=10, prompt_cache_miss_tokens=10)
    a += b
    assert a.prompt_tokens == 30
    assert a.completion_tokens == 15
    assert a.prompt_cache_hit_tokens == 18
    assert a.prompt_cache_miss_tokens == 12
    assert a.cache_hit_ratio == 0.6


def test_usage_add_returns_new():
    a = Usage(prompt_tokens=10)
    b = Usage(completion_tokens=5)
    c = a + b
    assert c is not a and c is not b
    assert c.prompt_tokens == 10 and c.completion_tokens == 5
    assert a.prompt_tokens == 10  # 原对象不变


# ---------- ToolCall ----------

def test_toolcall_signature_sorted_keys():
    call = ToolCall(id="c1", name="edit", arguments={"path": "a.py", "old_string": "x", "new_string": "y"})
    assert call.signature() == 'edit({"new_string": "y", "old_string": "x", "path": "a.py"})'


# ---------- MockLLM ----------

def test_mock_text_single_answer():
    llm = MockLLM.text("hello", usage=Usage(prompt_tokens=3, completion_tokens=2))
    result = llm.chat([], [])
    assert result.content == "hello"
    assert result.tool_calls == []
    assert result.usage.completion_tokens == 2


def test_mock_tool_factory():
    llm = MockLLM.tool("read", {"path": "README.md"})
    result = llm.chat([], [])
    assert len(result.tool_calls) == 1
    call = result.tool_calls[0]
    assert call.name == "read"
    assert call.arguments == {"path": "README.md"}
    assert call.id.startswith("call_")


def test_mock_script_pop_order():
    llm = MockLLM.script(
        MockLLM.tool("glob", {"pattern": "*.py"}).responses[0],
        MockLLM.tool("read", {"path": "a.py"}).responses[0],
        LLMResult(content="done"),
    )
    assert llm.chat([], []).tool_calls[0].name == "glob"
    assert llm.chat([], []).tool_calls[0].name == "read"
    assert llm.chat([], []).content == "done"


def test_mock_script_from_scratch():
    """直接 script(...) 构造（避免上面绕一圈的写法）。"""
    llm = MockLLM.script(
        LLMResult(content=None, tool_calls=[ToolCall(id="c1", name="bash", arguments={"command": "ls"})]),
        LLMResult(content="ok"),
    )
    assert llm.chat([], []).tool_calls[0].name == "bash"
    assert llm.chat([], []).content == "ok"


def test_mock_exhausted_raises():
    llm = MockLLM.text("once")
    llm.chat([], [])
    with pytest.raises(RuntimeError, match="耗尽"):
        llm.chat([], [])


def test_mock_callable_generates_result():
    """callable 响应：可用历史断言（loop 测试会用到）。"""

    def respond(messages, tools):
        assert tools  # 有工具定义
        return LLMResult(content=f"saw {len(messages)} messages")

    llm = MockLLM([respond])
    result = llm.chat([{"role": "user", "content": "hi"}], [{"type": "function"}])
    assert result.content == "saw 1 messages"


# ---------- BaseLLM.complete ----------

def test_complete_returns_text():
    llm = MockLLM.text("summary")
    assert llm.complete([{"role": "user", "content": "summarize"}]) == "summary"


def test_complete_without_content_returns_empty():
    llm = MockLLM.tool("read", {"path": "a"})
    assert llm.complete([]) == ""


# ---------- DeepSeekClient（不真正调用） ----------

def test_deepseek_requires_api_key(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    with pytest.raises(ValueError, match="DEEPSEEK_API_KEY"):
        DeepSeekClient(api_key="")


def test_deepseek_constructs_with_key():
    client = DeepSeekClient(api_key="sk-test", base_url="https://api.deepseek.com", model="deepseek-chat")
    assert isinstance(client, BaseLLM)
    assert client.model == "deepseek-chat"
    assert client.api_key == "sk-test"


def test_deepseek_reads_env(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-env")
    monkeypatch.setenv("DEEPSEEK_MODEL", "deepseek-reasoner")
    client = DeepSeekClient()
    assert client.api_key == "sk-env"
    assert client.model == "deepseek-reasoner"
