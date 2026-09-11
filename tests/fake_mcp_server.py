"""测试用的假 MCP server（stdio 壳）：给 tests/test_mcp.py 当对端。

**不是 mock**：这是一个真实的子进程、真实的管道、真实的 JSON-RPC 往返——
测试因此能覆盖握手、跨进程读超时、server 崩溃等只有真进程才会暴露的问题。

协议面全部在 `fake_mcp_core.py`（与 HTTP 壳共用一份），本文件只做搬运：
逐行读 stdin → `FakeServer.handle` → 逐行写 stdout。

选项：`--note-path P` / `--crash-immediately` / `--crash-on-call` /
`--silent-on METHOD`（故意不回该方法的响应）/ `--no-resources` / `--no-prompts` /
`--empty-resources` / `--empty-prompts` / `--require-initialized`。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))  # 直接跑脚本时能找到同目录的 core

from fake_mcp_core import FakeServer, dump  # noqa: E402

NOTE_PATH = Path(__file__).parent / "_mcp_note.txt"  # 由 --note-path 覆盖


def _flag_value(flag: str, default=None):
    return sys.argv[sys.argv.index(flag) + 1] if flag in sys.argv else default


def main() -> None:
    if "--crash-immediately" in sys.argv:
        sys.stderr.write("故意崩溃\n")
        sys.exit(3)

    server = FakeServer(
        Path(_flag_value("--note-path", str(NOTE_PATH))),
        resources="--no-resources" not in sys.argv,
        prompts="--no-prompts" not in sys.argv,
        empty_resources="--empty-resources" in sys.argv,
        empty_prompts="--empty-prompts" in sys.argv,
        require_initialized="--require-initialized" in sys.argv,
    )
    silent_on = _flag_value("--silent-on")
    crash_on_call = "--crash-on-call" in sys.argv

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            continue
        if crash_on_call and request.get("method") == "tools/call":
            sys.stderr.write("调用时崩溃\n")
            sys.exit(4)
        if silent_on and request.get("method") == silent_on:
            continue
        response = server.handle(request)
        if response is not None:
            sys.stdout.write(dump(response) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
