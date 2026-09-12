"""Streamlit 控制台（M2-3 + M3-5 指标）：实时循环 / 工具调用 / 权限确认 / 运行指标。

运行：`streamlit run app/ui_streamlit.py`
- 无 DEEPSEEK_API_KEY → 自动 Mock 演示（glob → 结论），有 key 用真实 DeepSeek
- 权限 ask：worker 线程阻塞等待，UI 弹按钮（允许本次/本回合/总是/拒绝），
  选择写入线程安全队列后 worker 继续（决策粒度记忆进 PermissionsEngine）
- hooks：走 default_engine()（与 CLI 同一条链）—— block-at-submit 检查
  data/tests_pass.marker，marker 由测试命令跑成功时自动写入
- M3-5 指标：缓存命中率曲线 + 省钱估算（DeepSeek 输入缓存定价差）、上下文
  用量分级（最近 prompt tokens / 预算）、检查点列表（Session 落盘）

实现：QueryEngine 在后台线程跑，事件经 queue 流到主线程轮询渲染
（`st.rerun()` 定期刷新）。确认桥 ConfirmBridge 用两个队列把引擎的
confirm 回调接到按钮。
"""
from __future__ import annotations

import os
import queue
import threading
import time
from pathlib import Path

import streamlit as st
from dotenv import load_dotenv

from agent.context import ContextStats
from agent.hooks import default_engine
from agent.llm import BaseLLM, DeepSeekClient, MockLLM, LLMResult, ToolCall
from agent.loop import QueryEngine
from agent.permissions import PermissionsEngine
from agent.pricing import DEFAULT_MODEL as PRICING_MODEL, snapshot_for
from agent.session import Session, new_session_id, state_dict
from agent.skills import discover_skills
from agent.tools.ask import build_ask_tool
from agent.tools.base import ToolRegistry
from agent.tools.skills import build_skill_tools
from agent.tools.subagent import build_subagent_tools
from app.replay import (
    list_checkpoint_sessions,
    list_checkpoint_steps,
    load_checkpoint,
    render_message,
)

DEFAULT_WORKSPACE = Path(os.environ.get("WORKSPACE_ROOT", "workspace")).resolve()

# DeepSeek 输入缓存定价：查 `agent/pricing.py` 的快照表，**不在这里再抄一份常量**。
# （原来这里硬编码了命中/未命中两个价，且**漏了输出价** —— 两份常量迟早漂移。）
_PRICE = snapshot_for(PRICING_MODEL)
CACHE_HIT_PRICE_CNY = _PRICE.input_hit
CACHE_MISS_PRICE_CNY = _PRICE.input_miss
# 上下文预算（与 ContextManager 默认一致，仅用于 UI 分级展示）
CONTEXT_BUDGET = 64_000


class ConfirmBridge:
    """把权限引擎 confirm 回调接到 UI 按钮（跨线程队列，worker 阻塞等回应）。"""

    def __init__(self) -> None:
        self.answers: queue.Queue[str | None] = queue.Queue()
        self.current: str | None = None  # 待确认的问题（UI 读它渲染按钮）

    def ask(self, question: str) -> str | None:
        self.current = question
        return self.answers.get()  # 阻塞 worker 直到 UI 调用 respond()


def has_api_key() -> bool:
    load_dotenv()
    return bool(os.environ.get("DEEPSEEK_API_KEY"))


def build_llm(mock: bool) -> BaseLLM:
    if mock:
        # 确定性演示：glob 真实执行 → Mock 给最终回答（同 cli --mock）
        return MockLLM.script(
            LLMResult(content=None, tool_calls=[ToolCall(id="call_demo", name="glob", arguments={"pattern": "**/*"})]),
            LLMResult(content="（Mock 演示）我已探索工作目录。真实模式下这里会输出基于工具结果的分析结论。"),
        )
    return DeepSeekClient()


def fmt_event(ev: dict) -> str:
    t = ev["type"]
    step = ev.get("step", "?")
    if t == "llm_call":
        calls = ev.get("tool_calls") or []
        return f"[步 {step}] 🤖 模型 → {len(calls)} 个工具调用"
    if t == "tool_call":
        args = ev.get("arguments") or {}
        summary = ", ".join(f"{k}={str(v)[:50]}" for k, v in list(args.items())[:2])
        mark = "✓" if ev.get("success") else "✗"
        return f"[步 {step}] {mark} {ev.get('name')}({summary}) {ev.get('duration_ms', 0)}ms"
    if t == "empty_response_retry":
        return f"[步 {step}] ⚠️ 空响应 → 重试 {ev.get('attempt')}/{ev.get('limit')}"
    return f"[步 {step}] {t}"


def run_task(
    task: str,
    mock: bool,
    workspace: Path,
    events_q: queue.Queue,
    confirm: ConfirmBridge,
) -> None:
    """worker 线程：组引擎 → 跑任务 → 事件推队列。"""
    try:
        llm = build_llm(mock)
        registry = ToolRegistry.default(workspace)
        for subagent_tool in build_subagent_tools(llm, workspace):
            registry.register(subagent_tool)  # M4-2 / M9-7 子代理工具族
        # ask_user：与 CLI 共用同一处构造（见 build_ask_tool 的 docstring）。
        # 同子代理工具族一样**不进 default()** —— eval/runner 用 default()，
        # 而 headless 评测里没有人能回答。
        registry.register(build_ask_tool())
        # skills：与 CLI 共用同一套发现与构造（索引进提示词，正文按需 load_skill）
        skills = discover_skills(workspace)
        for skill_tool in build_skill_tools(skills):
            registry.register(skill_tool)
        # 第三方工具（MCP）默认需显式授权：这里虽未加载 MCP，仍按注册表实际
        # 情况传入，避免以后接上 MCP 时权限层悄悄漏掉（同类漂移已经犯过一次）
        permissions = PermissionsEngine(
            workspace,
            confirm=confirm.ask,
            external_tools=[tool.name for tool in registry.external()],
        )
        # 与 CLI 共用同一条标准治理链（避免两个入口接线漂移）
        hooks = default_engine(workspace)
        session = Session(workspace, new_session_id(), on_event=events_q.put)
        events_q.put({"type": "session_start", "session_id": session.session_id})
        engine = QueryEngine(
            llm,
            registry,
            workspace_root=workspace,
            permissions=permissions,
            hooks=hooks,
            session=session,
            skills=skills,
        )
        result = engine.run(task)
        events_q.put({"type": "__done__", "result": result, "session_id": session.session_id})
    except Exception as exc:
        events_q.put({"type": "__done__", "error": f"{type(exc).__name__}: {exc}"})


# ---------- 页面 ----------

st.set_page_config(page_title="CodeAgent 控制台", page_icon="🤖", layout="wide")
st.title("🤖 CodeAgent 控制台")

with st.sidebar:
    st.markdown("**运行设置**")
    mock = st.checkbox("Mock 演示（无 key）", value=not has_api_key())
    workspace_input = st.text_input("工作目录", value=str(DEFAULT_WORKSPACE))
    workspace = Path(workspace_input).resolve()

# 初始化会话状态
st.session_state.setdefault("events_q", queue.Queue())
st.session_state.setdefault("log", [])
st.session_state.setdefault("done", False)
st.session_state.setdefault("result", None)
st.session_state.setdefault("error", None)
st.session_state.setdefault("running", False)
st.session_state.setdefault("confirm", ConfirmBridge())
st.session_state.setdefault("session_id", None)

confirm: ConfirmBridge = st.session_state["confirm"]

task = st.text_area("任务描述", placeholder="例如：读 README 并总结项目结构；或：给 a.txt 加一行并验证")

col_go, col_stop, _ = st.columns([1, 1, 3])
if col_go.button("🚀 开始任务", use_container_width=True) and task.strip():
    st.session_state["log"] = []
    st.session_state["done"] = False
    st.session_state["result"] = None
    st.session_state["error"] = None
    st.session_state["running"] = True
    st.session_state["confirm"] = ConfirmBridge()  # 新任务重置确认桥
    st.session_state["session_id"] = None
    events_q = st.session_state["events_q"] = queue.Queue()
    st.session_state["task"] = task
    threading.Thread(
        target=run_task,
        args=(task, mock, workspace, events_q, st.session_state["confirm"]),
        daemon=True,
    ).start()
    st.rerun()

if col_stop.button("⏹ 停止", use_container_width=True):
    st.session_state["running"] = False
    st.session_state["done"] = True
    st.rerun()

# ---------- 轮询 worker 事件 ----------
events_q = st.session_state["events_q"]
while True:
    try:
        ev = events_q.get_nowait()
    except queue.Empty:
        break
    if ev["type"] == "__done__":
        st.session_state["running"] = False
        st.session_state["done"] = True
        st.session_state["result"] = ev.get("result")
        st.session_state["error"] = ev.get("error")
        st.session_state["session_id"] = ev.get("session_id") or st.session_state.get("session_id")
    elif ev["type"] == "session_start":
        st.session_state["session_id"] = ev.get("session_id")
    else:
        st.session_state["log"].append(ev)

# ---------- 权限确认按钮（worker 正阻塞等待） ----------
if st.session_state["running"] and confirm.current and not st.session_state["done"]:
    with st.container(border=True):
        st.warning("🔒 需要人工确认：")
        st.code(confirm.current)
        c1, c2, c3, c4, c5 = st.columns(5)
        for col, (label, choice) in zip(
            (c1, c2, c3, c4, c5),
            [
                ("允许本次", "allow_once"),
                ("允许本回合", "allow_turn"),
                ("总是允许", "allow_always"),
                ("拒绝本次", "deny_once"),
                ("总是拒绝", "deny_always"),
            ],
        ):
            if col.button(label, key=f"perm_{choice}", use_container_width=True):
                confirm.answers.put(choice)
                confirm.current = None
                st.rerun()

# ---------- 实时事件日志 ----------
log = st.session_state["log"]
with st.expander(f"事件日志（{len(log)} 条）", expanded=True):
    for ev in log:
        st.code(fmt_event(ev), language=None)
    if log and st.session_state["running"] and not st.session_state["done"]:
        st.caption("运行中…")

# ---------- M3-5 运行指标（缓存命中率/省钱 + 上下文分级 + 检查点） ----------
usage_pts = [
    ev for ev in log
    if ev["type"] == "llm_call" and (ev.get("usage") or {}).get("prompt_tokens") is not None
]
if usage_pts:
    # 上下文用量分级：用最近一次 provider 实测 prompt tokens 作上下文大小代理
    last_usage = usage_pts[-1]["usage"]
    utilization = last_usage["prompt_tokens"] / CONTEXT_BUDGET
    level = ContextStats.level_of(utilization)
    st.progress(
        min(1.0, utilization),
        text=f"上下文用量 {level}（{utilization:.0%}，最近 prompt {last_usage['prompt_tokens']} tokens / {CONTEXT_BUDGET} 预算）",
    )

    # 缓存命中率累计 + 省钱估算（DeepSeek 输入缓存定价差）
    total_hit = sum((ev.get("usage") or {}).get("cache_hit_tokens") or 0 for ev in usage_pts)
    total_miss = sum((ev.get("usage") or {}).get("cache_miss_tokens") or 0 for ev in usage_pts)
    if total_hit + total_miss:
        overall = total_hit / (total_hit + total_miss)
        saved_cny = total_hit * (CACHE_MISS_PRICE_CNY - CACHE_HIT_PRICE_CNY) / 1e6
        st.caption(
            f"累计缓存命中率 {overall:.0%} · 命中 {total_hit:,} tokens · "
            f"估算省钱 ¥{saved_cny:.3f}（命中 ¥{CACHE_HIT_PRICE_CNY}/M vs 未命中 ¥{CACHE_MISS_PRICE_CNY}/M"
            f"，按定价快照 {_PRICE.id}）"
        )
    else:
        st.caption("本轮暂无缓存流量（DeepSeek 首次调用会把 prompt 写入磁盘缓存）")

    with st.expander("📈 缓存命中率曲线", expanded=False):
        steps = [ev.get("step") for ev in usage_pts]
        ratios = []
        for ev in usage_pts:
            u = ev.get("usage") or {}
            hit = u.get("cache_hit_tokens") or 0
            miss = u.get("cache_miss_tokens") or 0
            ratios.append(hit / (hit + miss) if (hit + miss) else 0.0)
        if len(steps) > 1:
            st.line_chart({"步骤": steps, "缓存命中率": ratios})
        else:
            st.caption("至少 2 次模型调用后才画命中率曲线")

    # 检查点列表（Session 每 N 步落盘）
    session_id = st.session_state.get("session_id")
    if session_id:
        cp_dir = workspace / "data" / "checkpoints" / session_id
        if cp_dir.exists():
            cp_steps = sorted(
                int(p.stem.split("-")[1])
                for p in cp_dir.glob("step-*.json")
                if not p.name.endswith(".tmp")
            )
            st.caption(f"会话 {session_id} · 检查点 " + "、".join(f"step-{s}" for s in cp_steps))
        else:
            st.caption(f"会话 {session_id} · 检查点写入中…")

# ---------- M5-3 检查点回放视图 ----------
replay_sessions = list_checkpoint_sessions(workspace)
with st.expander(f"🎞 检查点回放（{len(replay_sessions)} 个会话）", expanded=False):
    if not replay_sessions:
        st.caption("暂无检查点。跑一个长任务（每 5 步落盘一次）后可在此回放每一步的完整对话。")
    else:
        c1, c2 = st.columns([2, 1])
        replay_sid = c1.selectbox("会话", replay_sessions)
        steps = list_checkpoint_steps(workspace, replay_sid)
        replay_step = (
            c2.selectbox("检查点", steps, format_func=lambda s: f"step-{s}")
            if steps
            else None
        )
        if replay_step is not None:
            payload = load_checkpoint(workspace, replay_sid, replay_step)
            if payload is None:
                st.caption("检查点正在写入，稍后再试。")
            else:
                # 用 state_dict 取字段，不直接翻 payload —— 落盘格式换过一版
                # （M7 从平铺改成 state 子对象），读者不该跟着格式走
                state = state_dict(payload)
                task = state.get("task", "") or "（未知任务）"
                term = state.get("terminated_reason")
                st.caption(
                    f"任务：{task[:100]}" + (f" · 终止：{term}" if term else "")
                )
                for msg in state.get("messages", []):
                    role = msg.get("role", "?")
                    label = {
                        "system": "🖥 system",
                        "user": "👤 user",
                        "assistant": "🤖 assistant",
                        "tool": "🔧 tool",
                    }.get(role, role)
                    st.markdown(f"**{label}**")
                    st.code(render_message(msg), language=None)

# ---------- 最终结果 ----------
if st.session_state["done"]:
    if st.session_state["error"]:
        st.error(st.session_state["error"])
    result = st.session_state["result"]
    if result is not None:
        if result.terminated_reason == "await_user":
            # 提问不是结论：标题必须换，否则人会以为任务跑完了
            st.markdown("### ❓ 需要你补充信息")
            st.markdown(result.final_text or "（问题内容为空）")
            st.info("把回答填进上方任务框、再点开始（会作为新会话的输入）。"
                    "要从**原会话**接着跑，用 CLI：\n"
                    f'`python -m app.cli --resume --session-id {st.session_state.get("session_id") or "<sid>"} "你的回答"`')
        else:
            st.markdown("### ✅ 最终结论")
            st.markdown(result.final_text or "（无结论）")
        usage = result.usage
        ratio = usage.cache_hit_ratio
        cache_line = f"，缓存命中 {ratio:.0%}" if ratio is not None else ""
        st.caption(
            f"终止原因 {result.terminated_reason} · 步骤 {result.steps} · "
            f"token {usage.total_tokens}（prompt {usage.prompt_tokens} + "
            f"completion {usage.completion_tokens}）{cache_line}"
        )
        st.markdown("---")
        if st.button("🆕 新任务"):
            st.session_state["log"] = []
            st.session_state["done"] = False
            st.session_state["result"] = None
            st.session_state["confirm"] = ConfirmBridge()
            st.rerun()
elif st.session_state["running"]:
    # 任务运行中：轮询 worker 事件（sleep + rerun 实现实时刷新）
    time.sleep(0.3)
    st.rerun()
# 空闲状态：不 rerun（避免无任务时无限刷新）
