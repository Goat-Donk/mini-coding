"""M2-3 smoke tests: app/ui_streamlit.py 用 Streamlit AppTest 无头跑通。

验收：Mock 模式下"开始任务"→ 事件日志渲染 → 最终结论出现。
AppTest 逐次 run() 模拟一次脚本执行；worker 线程后台跑完任务后
页面由 done 状态收敛（不再 st.rerun）。
"""
import os

import pytest

pytest.importorskip("streamlit.testing")

from streamlit.testing.v1 import AppTest  # noqa: E402

APP_FILE = "app/ui_streamlit.py"


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)  # 强制 mock 默认
    at = AppTest.from_file(APP_FILE, default_timeout=60)
    at.run()
    return at


def test_ui_initial_render(app):
    assert not app.exception
    assert len(app.text_area) == 1
    assert app.text_area[0].label == "任务描述"
    # 初始有"开始任务"与"停止"两个按钮
    labels = [b.label for b in app.button]
    assert any("开始" in l for l in labels)


def test_ui_mock_task_completes(app, tmp_path):
    (tmp_path / "README.md").write_text("# Demo\nhello\n", encoding="utf-8")
    app.text_area[0].set_value("读 README 并总结")
    app.button[0].click()
    app.run()

    # worker 线程跑完 + 页面收敛后：出现最终结论与事件日志
    assert not app.exception
    text = " ".join(getattr(e, "value", "") for e in app.markdown)
    assert "最终结论" in text
    # 事件日志渲染了 glob 工具调用
    codes = "\n".join(c.value for c in app.code)
    assert "glob" in codes
