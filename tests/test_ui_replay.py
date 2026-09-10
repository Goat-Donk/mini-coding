"""M5-3 tests: 控制台检查点回放视图（离线，不联网）。

纯函数测试（app/replay.py 不依赖 streamlit runtime）：
  会话列表排序 / 步骤列表 / 检查点读取 / 消息渲染。
AppTest 冒烟：app/ui_streamlit.py 在测试运行时无异常渲染，
且设置工作目录后可回放已落盘的检查点。
"""
import pytest
from streamlit.testing.v1 import AppTest

from agent.llm import ToolCall
from agent.session import Session, new_session_id, state_dict
from agent.state import AgentState, assistant_tool_calls, tool_result, user
from app.replay import (
    list_checkpoint_sessions,
    list_checkpoint_steps,
    load_checkpoint,
    render_message,
)


def _make_checkpoint_session(tmp_path, *, sid=None, task="修 bug") -> tuple:
    """造一个两段式检查点 session（step 5 与 step 10），返回 (workspace, sid)。"""
    ws = tmp_path / "ws"
    sid = sid or new_session_id()
    sess = Session(ws, sid, checkpoint_every=5)

    state1 = AgentState(
        session_id=sid, task=task, system_prompt="sys", step=5,
        messages=[user("第一步")],
    )
    sess._ticks = 4  # +1 → 5 触发落盘
    sess.checkpoint(state1)

    state2 = AgentState(
        session_id=sid, task=task, system_prompt="sys", step=10,
        messages=[
            user("第一步"),
            assistant_tool_calls(
                [ToolCall(id="t1", name="read", arguments={"path": "a.txt"})]
            ),
            tool_result("t1", "文件内容"),
        ],
        terminated_reason="done",
    )
    sess._ticks = 9  # +1 → 10 触发落盘
    sess.checkpoint(state2)
    return ws, sid


# ---------- 纯函数 ----------


def test_render_message_roles():
    """assistant 工具调用压缩成一行；tool 结果截断；user 原文透传；空 content 安全。"""
    assert render_message(
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "t1",
                    "type": "function",
                    "function": {"name": "read", "arguments": '{"path": "a.txt"}'},
                }
            ],
        }
    ) == '→ read({"path": "a.txt"})'
    long_tool = "x" * 600
    out = render_message({"role": "tool", "content": long_tool})
    assert len(out) == 501 and out.endswith("…")
    assert render_message({"role": "user", "content": "你好"}) == "你好"
    assert render_message({"role": "assistant", "content": None}) == ""


def test_render_message_multimodal_fallback():
    """多模态 content 列表兜底取纯文本片段。"""
    assert render_message(
        {"role": "user", "content": [{"type": "text", "text": "看图后回答"}]}
    ) == "看图后回答"


def test_list_checkpoint_sessions_orders_newest_first(tmp_path):
    """多个会话按最近检查点 mtime 降序。"""
    import os
    import time as _time

    ws1, sid1 = _make_checkpoint_session(tmp_path)
    ws2, sid2 = _make_checkpoint_session(tmp_path)

    root = tmp_path / "ws" / "data" / "checkpoints"
    for f in (root / sid1 / "step-10.json", root / sid2 / "step-10.json"):
        os.utime(f, (f.stat().st_atime, _time.time() - 3600))  # 把 sid1/sid2 调旧
    newest = root / sid2 / "step-5.json"  # sid2 的 step-5 最晚
    os.utime(newest, (newest.stat().st_atime, _time.time() + 3600))

    sessions = list_checkpoint_sessions(ws1)
    assert sessions[0] == sid2  # 最新的在前
    assert set(sessions) == {sid1, sid2}


def test_list_checkpoint_steps_and_load(tmp_path):
    """步骤升序；读 state；缺文件返回 None。"""
    ws, sid = _make_checkpoint_session(tmp_path)
    assert list_checkpoint_steps(ws, sid) == [5, 10]
    payload = load_checkpoint(ws, sid, 10)
    state = state_dict(payload)
    assert state["task"] == "修 bug"
    assert state["terminated_reason"] == "done"
    assert len(state["messages"]) == 3
    assert load_checkpoint(ws, sid, 99) is None


# ---------- AppTest 冒烟 ----------


def test_app_renders_without_exception():
    """空载入（无任务、无检查点）不抛异常。"""
    at = AppTest.from_file("app/ui_streamlit.py", default_timeout=10)
    at.run()
    assert not at.exception


def test_app_replay_shows_checkpointed_session(tmp_path):
    """设置工作目录后，回放区出现会话/步骤选择并可读回放消息。"""
    ws, sid = _make_checkpoint_session(tmp_path, task="修 LRU 缓存 bug")
    at = AppTest.from_file("app/ui_streamlit.py", default_timeout=10)
    at.run()
    at.sidebar.text_input[0].set_value(str(ws))  # 工作目录 → 含检查点的 ws
    at.run()
    assert not at.exception
    # 回放区：会话选择框（索引 0 是会话，1 是步骤）
    assert at.selectbox and sid in at.selectbox[0].options
