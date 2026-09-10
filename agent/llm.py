"""LLM 层：BaseLLM 抽象 → DeepSeekClient（生产）/ MockLLM（测试，无网络）。

设计对齐 Claude Code：LLM 只暴露两个能力——带工具的 chat（agent 循环用）
与纯文本 complete（compact 摘要/记忆提取用）。usage 采集同时兼容
DeepSeek 的磁盘缓存字段（usage.prompt_cache_hit_tokens）与 OpenAI 风格
（usage.prompt_tokens_details.cached_tokens）。
"""
from __future__ import annotations

import json
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Callable, Sequence

from openai import OpenAI


@dataclass
class Usage:
    """单次 LLM 调用的 token 用量，支持累加到会话总用量。"""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    prompt_cache_hit_tokens: int = 0   # DeepSeek 磁盘缓存命中
    prompt_cache_miss_tokens: int = 0  # 未命中（= 新写入缓存的 token）

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    @property
    def cache_hit_ratio(self) -> float | None:
        """命中率；分母为 0（无缓存流量）时返回 None。"""
        total = self.prompt_cache_hit_tokens + self.prompt_cache_miss_tokens
        if total <= 0:
            return None
        return self.prompt_cache_hit_tokens / total

    def __iadd__(self, other: "Usage") -> "Usage":
        self.prompt_tokens += other.prompt_tokens
        self.completion_tokens += other.completion_tokens
        self.prompt_cache_hit_tokens += other.prompt_cache_hit_tokens
        self.prompt_cache_miss_tokens += other.prompt_cache_miss_tokens
        return self

    def __add__(self, other: "Usage") -> "Usage":
        return Usage(
            prompt_tokens=self.prompt_tokens + other.prompt_tokens,
            completion_tokens=self.completion_tokens + other.completion_tokens,
            prompt_cache_hit_tokens=self.prompt_cache_hit_tokens + other.prompt_cache_hit_tokens,
            prompt_cache_miss_tokens=self.prompt_cache_miss_tokens + other.prompt_cache_miss_tokens,
        )


@dataclass
class ToolCall:
    """一次工具调用（模型发出，回填 assistant tool_calls 消息）。"""

    id: str
    name: str
    arguments: dict

    def signature(self) -> str:
        """稳定签名，用于循环检测（同参数重复调用可识别）。"""
        return f"{self.name}({json.dumps(self.arguments, sort_keys=True, ensure_ascii=False)})"


@dataclass
class LLMResult:
    content: str | None
    tool_calls: list[ToolCall] = field(default_factory=list)  # 空列表 = 最终回答
    usage: Usage = field(default_factory=Usage)
    finish_reason: str | None = None


class BaseLLM(ABC):
    """LLM 统一接口。messages/tools 均为 OpenAI 格式 dict。"""

    model: str

    @abstractmethod
    def chat(
        self,
        messages: list[dict],
        tools: list[dict],
        *,
        temperature: float = 0.0,
    ) -> LLMResult: ...

    def complete(self, messages: list[dict], *, temperature: float = 0.0) -> str:
        """纯文本补全（不允许工具调用）。compact 摘要、记忆提取等场景用。"""
        result = self.chat(messages, tools=[], temperature=temperature)
        if result.content is None:
            return ""
        return result.content


def _usage_value(usage: object, *names: str) -> int:
    """从 usage 对象/字典中读取第一个存在的字段，缺失返回 0。

    openai SDK 3.x 的 usage 是 pydantic 对象；DeepSeek 官方还可能在顶层放
    prompt_cache_hit_tokens。两者都兜底读取，读不到不报错。
    """
    for name in names:
        if usage is None:
            return 0
        if isinstance(usage, dict):
            value = usage.get(name)
        else:
            value = getattr(usage, name, None)
        if isinstance(value, int):
            return value
    return 0


class DeepSeekClient(BaseLLM):
    """DeepSeek 生产客户端（OpenAI-compatible，官方推荐 openai SDK）。"""

    DEFAULT_BASE_URL = "https://api.deepseek.com"
    DEFAULT_MODEL = "deepseek-chat"

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        timeout: float = 120.0,
    ) -> None:
        self.api_key = api_key or os.environ.get("DEEPSEEK_API_KEY", "")
        if not self.api_key:
            raise ValueError(
                "缺少 DEEPSEEK_API_KEY。请在 .env 中配置，或调用前 load_dotenv()。"
            )
        self.base_url = base_url or os.environ.get("DEEPSEEK_BASE_URL", self.DEFAULT_BASE_URL)
        self.model = model or os.environ.get("DEEPSEEK_MODEL", self.DEFAULT_MODEL)
        self._client = OpenAI(
            api_key=self.api_key,
            base_url=self.base_url,
            timeout=timeout,
        )

    def chat(
        self,
        messages: list[dict],
        tools: list[dict],
        *,
        temperature: float = 0.0,
    ) -> LLMResult:
        kwargs: dict = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
        }
        if tools:  # 空列表不传（部分模型不支持）
            kwargs["tools"] = tools
        response = self._client.chat.completions.create(**kwargs)
        msg = response.choices[0].message
        usage = response.usage

        tool_calls: list[ToolCall] = []
        for tc in msg.tool_calls or []:
            raw_arguments = tc.function.arguments or "{}"
            try:
                parsed = json.loads(raw_arguments)
            except json.JSONDecodeError:
                parsed = {"_raw": raw_arguments}  # 模型偶发非法 JSON，兜底不回吐
            tool_calls.append(
                ToolCall(id=tc.id, name=tc.function.name, arguments=parsed)
            )

        content = msg.content
        if isinstance(content, list):  # 多模态响应兜底：只取 text 段
            content = "".join(
                part.get("text", "") for part in content if isinstance(part, dict)
            ) or None

        hit = _usage_value(usage, "prompt_cache_hit_tokens")
        miss = _usage_value(usage, "prompt_cache_miss_tokens")
        if hit == 0 and miss == 0:  # OpenAI 风格兜底
            cached = _usage_value(usage, "cached_tokens")
            if hasattr(usage, "prompt_tokens_details") and usage.prompt_tokens_details:
                cached = getattr(usage.prompt_tokens_details, "cached_tokens", 0) or 0
            if cached:
                hit = cached
                miss = max(0, _usage_value(usage, "prompt_tokens") - cached)

        return LLMResult(
            content=content,
            tool_calls=tool_calls,
            usage=Usage(
                prompt_tokens=_usage_value(usage, "prompt_tokens"),
                completion_tokens=_usage_value(usage, "completion_tokens"),
                prompt_cache_hit_tokens=hit,
                prompt_cache_miss_tokens=miss,
            ),
            finish_reason=response.choices[0].finish_reason,
        )


class MockLLM(BaseLLM):
    """确定性脚本化 LLM（测试用，无网络）。

    responses 顺序弹出：每个元素是 LLMResult，或
    Callable[[list[dict], list[dict]], LLMResult]（可断言 messages/tools 历史）。
    耗尽后调用抛 RuntimeError（用于断言"模型不该再被调用"）。
    """

    model = "mock"

    def __init__(self, responses: Sequence[LLMResult | Callable[..., LLMResult]] = ()) -> None:
        self.responses: list[LLMResult | Callable[..., LLMResult]] = list(responses)

    @staticmethod
    def text(content: str, *, usage: Usage | None = None) -> "MockLLM":
        return MockLLM([LLMResult(content=content, usage=usage or Usage())])

    @staticmethod
    def tool(
        tool_name: str,
        arguments: dict,
        *,
        content: str | None = None,
        usage: Usage | None = None,
    ) -> "MockLLM":
        """单次返回一个工具调用（tool id 自动生成 call_0001）。"""
        return MockLLM([
            LLMResult(
                content=content,
                tool_calls=[ToolCall(id="call_0001", name=tool_name, arguments=dict(arguments))],
                usage=usage or Usage(),
            )
        ])

    @staticmethod
    def script(*responses: LLMResult | Callable[..., LLMResult]) -> "MockLLM":
        """多步脚本：如 script(tool("glob",...), tool("read",...), text("完成"))。"""
        return MockLLM(list(responses))

    def chat(
        self,
        messages: list[dict],
        tools: list[dict],
        *,
        temperature: float = 0.0,
    ) -> LLMResult:
        if not self.responses:
            raise RuntimeError("MockLLM 响应已耗尽：模型不该再被调用")
        item = self.responses.pop(0)
        if callable(item):
            return item(messages, tools)
        return item
