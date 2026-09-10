"""Streamlit 控制台 v1（M2-3）：实时循环 / 工具调用 / 权限确认按钮。

运行：`streamlit run app/ui_streamlit.py`
- 无 DEEPSEEK_API_KEY → 自动 Mock 演示（glob → 结论），有 key 用真实 DeepSeek
- 权限 ask：worker 线程阻塞等待，UI 弹按钮（允许本次/本回合/总是/拒绝），
  选择写入线程安全队列后 worker 继续（决策粒度记忆进 PermissionsEngine）
- hooks：内置 require_tests_before_commit（block-at-submit），git commit 前
  检查 data/tests_pass.marker

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

from agent.hooks import HookEngine, require_tests_before_commit
from agent.llm import BaseLLM, DeepSeekClient, MockLLM, LLMResult, ToolCall
from agent.loop import QueryEngine
from agent.permissions import PermissionsEngine
from agent.tools.base import ToolRegistry

DEFAULT_WORKSPACE = Path(os.environ.get("WORKSPACE_ROOT", "workspace")).resolve()


class ConfirmBridge:
    """把权限引擎 confirm 回调接到 UI 按钮（跨线程队列，worker 阻塞等回应）。"""

    def __init__(self) -> None:
        self.answers: queue.Queue[str | None] = queue.Queue()
        self.current: str | None = None  # 待确认的问题（UI 读它渲染按钮）

    def ask(self, question: str) -> str | None:
        self.current = question
        return self.answers.get()  # 阻塞 worker 直到 UI 调用 respond()


class _EmitProxy:
    """QueryEngine 期望 session 有 .emit 属性 → 事件推入队列。"""

    def __init__(self, emit) -> None:
        self.emit = emit


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
        permissions = PermissionsEngine(workspace, confirm=confirm.ask)
        hooks = HookEngine([require_tests_before_commit(workspace)], workspace_root=workspace)
        engine = QueryEngine(
            llm,
            registry,
            workspace_root=workspace,
            permissions=permissions,
            hooks=hooks,
            session=_EmitProxy(events_q.put),
        )
        result = engine.run(task)
        events_q.put({"type": "__done__", "result": result})
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

# ---------- 最终结果 ----------
if st.session_state["done"]:
    if st.session_state["error"]:
        st.error(st.session_state["error"])
    result = st.session_state["result"]
    if result is not None:
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
