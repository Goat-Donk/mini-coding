# offer-Master 架构笔记（次级参考）

> 调研对象：[happyFigure/offer-Master](https://github.com/happyFigure/offer-Master)（JobPilot Agent，Python/FastAPI 后端）
> 调研日期：2026-09-10（WebFetch 文件树）
> 参考定位：**工程组织**（模块化分层、工具注册、安全检查分层），不是产品场景

## 1. 整体结构

```
apps/
├── api/            # FastAPI 后端
│   └── app/
│       ├── agent_runtime/    # ★ agent 运行时的核心目录（最值得参考）
│       │   ├── loop_agent/   # controller.py / react_strategy.py / tool_choice_runner.py
│       │   ├── tool_registry.py
│       │   ├── contracts/    # 数据契约（请求/响应/状态）
│       │   ├── memory/
│       │   ├── planning/
│       │   ├── reflection/
│       │   ├── routing/
│       │   ├── durable_state/
│       │   ├── external_tasks/
│       │   ├── human_approval.py   # 人工审批
│       │   ├── guardrails.py       # 护栏
│       │   ├── output_sanitizer.py # 输出清洗
│       │   └── tracing.py          # 链路追踪
│       ├── mcp_gateway/      # MCP 网关
│       ├── rag/              # chunking / embeddings / retrievers / rerankers / citations / evals
│       └── domains/
├── web/           # React/Vite/TS 前端
└── worker/        # 后台任务
```

## 2. 对我们有用的设计

| offer-Master 设计 | 本项目采纳 |
|---|---|
| **核心层/应用层/评估层分离**（agent_runtime / apps / rag·evals） | 同构：agent/ + app/ + eval/ |
| **tool_registry**（工具注册中心，集中管理工具清单与 schema） | agent/tools/base.py 的 ToolRegistry |
| **contracts/**（数据契约先定义） | agent/state.py 的消息/结果数据结构 + TECH_SPEC 先定格式 |
| **human_approval.py 独立成模块** | agent/permissions.py 的 ask/人工确认（M2） |
| **guardrails + output_sanitizer 分置** | 权限引擎（进）+ 工具结果封包/截断（出）分离 |
| **tracing.py** | agent/session.py 的 JSONL 轨迹（M3） |
| **durable_state**（持久化状态） | 检查点落盘（M3） |
| **mcp_gateway 独立** | M6 有余力的 MCP 客户端 |

## 3. 我们明确不照搬的

- LangGraph 编排（本项目核心循环从零手写，这才是卖点；offer-Master 用 LangGraph 的 graph_factory 很大——参考其"运行时目录划分"即可）。
- 大而全的 rag 模块（RAG 不强制；代码检索用 grep/glob）。
- 前后端分离的 React 前端（我们只会 Streamlit）。
