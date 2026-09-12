"""定价快照（Pricing Snapshot）：把"价格"从一个会腐烂的常量变成一条带日期与状态的记录。

**为什么需要它**：`eval/runner.py` 与 `app/ui_streamlit.py` 原来各自硬编码一份单价常量。
官方页面一改，两份常量同时变成假话，而**历史报告里的成本数字不会跟着变** —— 于是
同一份报告里，"token 数"是真的，"成本"是拿今天的价乘当天的量算出来的，两者不同源。
更糟的是查不到价时没有诚实的表达方式：返回 None / 抛异常都会让报告生成炸掉。

**做法**：单价进快照表，每条带 `model` / `verified_on`（对着官方页核实的日期）/ `status`
（active | archived）/ `note`（口径说明）。历史报告按其**当时**的快照计费；官方下架后
把 status 置 `archived`、价格锁死不再修改。查不到时不返回 None、不抛异常，回落到
最近一条 archived 快照**并如实标注**。

叶子模块：不 import 任何 agent 内部东西（同 `agent/workspace.py` 的约定）。
价格的来源与核实日期一律写在 `note` 里 —— 看代码的人不必去翻 git log 猜。

⚠️ 一处必须说清的边界：`deepseek-chat@2025` 的 `verified_on` 是 **None**，
因为那组价格是项目从 2025 年沿用的**常量注释**，从来没有对着官方定价页核实过。
不编一个日期填进去 —— 编出来的日期比"未核实"更糟，因为它看起来像核实过。
"""
from __future__ import annotations

from dataclasses import dataclass

# 现行定价页（中英文各核一次，2026-09-12）：
#   https://api-docs.deepseek.com/zh-cn/quick_start/pricing
# 高峰时段 = 北京时间周一至周五 9:00-12:00、14:00-18:00，空闲 = 高峰的一半。
# 该页**全页零命中 `deepseek-chat`**；页脚注 (1) 只点名 `deepseek-v4-flash` /
# `deepseek-v4-flash-vision-exp` 两个别名"仍可调用，但对应模型已下线"，
# `deepseek-chat` **不在那份名单里** —— 所以能确定的只是"已不在定价页列示"，
# 不是"已下线"。
PRICING_PAGE_URL = "https://api-docs.deepseek.com/zh-cn/quick_start/pricing"


@dataclass(frozen=True)
class PriceSnapshot:
    """一条定价快照（单价单位：元 / 百万 token）。"""

    id: str                # 稳定标识，写进报告，如 "deepseek-chat@2025"
    model: str             # 对应的模型名
    verified_on: str | None  # 对着官方定价页核实的日期；None = 从未核实过
    status: str            # "active"（现行）| "archived"（已不在定价页列示，价格锁死）
    note: str              # 口径说明（来源、时效性、已知的不确定性）
    input_hit: float       # 缓存命中输入
    input_miss: float      # 缓存未命中输入
    output: float          # 输出

    def cost_cny(self, prompt_hit: int, prompt_miss: int, completion: int) -> float:
        """按本快照估算一次运行的成本（元）。"""
        return (
            prompt_hit * self.input_hit
            + prompt_miss * self.input_miss
            + completion * self.output
        ) / 1e6


# ---------- 快照表 ----------

_CHAT_2025 = PriceSnapshot(
    id="deepseek-chat@2025",
    model="deepseek-chat",
    verified_on=None,
    status="archived",
    note=(
        "该价格基于 2025 年的公开价快照，本项目自 2025 年起沿用，**从未对着官方定价页核实过**。"
        "deepseek-chat 已不在官方定价页列示（2026-09-12 中英文页各复核一次，全页零命中），"
        "实际计费单价未经核实；该模型名仍可调用（2026-09-11 实测跑通）。"
        "注意：定价页只说 `deepseek-v4-flash` 等别名对应模型已下线，"
        "`deepseek-chat` 不在那份名单里，所以这里写的是「已不在定价页列示」而非「已下线」。"
    ),
    input_hit=0.5, input_miss=2.0, output=8.0,
)

_FLASH_PEAK = PriceSnapshot(
    id="deepseek-flash@2026-09-12-peak",
    model="deepseek-flash",
    verified_on="2026-09-12",
    status="active",
    note=(
        "高峰时段价（北京时间周一至周五 9:00-12:00、14:00-18:00）。"
        f"来源：官方定价页 {PRICING_PAGE_URL}，2026-09-12 核实。"
        "空闲时段是高峰的一半，见 deepseek-flash@2026-09-12-offpeak。"
    ),
    input_hit=0.04, input_miss=2.0, output=8.0,
)

_FLASH_OFFPEAK = PriceSnapshot(
    id="deepseek-flash@2026-09-12-offpeak",
    model="deepseek-flash",
    verified_on="2026-09-12",
    status="active",
    note=(
        "空闲时段价（= 高峰的一半）。"
        f"来源：官方定价页 {PRICING_PAGE_URL}，2026-09-12 核实。"
    ),
    input_hit=0.02, input_miss=1.0, output=4.0,
)

_V4PRO_PEAK = PriceSnapshot(
    id="deepseek-v4-pro@2026-09-12-peak",
    model="deepseek-v4-pro",
    verified_on="2026-09-12",
    status="active",
    note=(
        "高峰时段价。"
        f"来源：官方定价页 {PRICING_PAGE_URL}，2026-09-12 核实。"
        "空闲时段是高峰的一半，见 deepseek-v4-pro@2026-09-12-offpeak。"
    ),
    input_hit=0.30, input_miss=9.0, output=27.0,
)

_V4PRO_OFFPEAK = PriceSnapshot(
    id="deepseek-v4-pro@2026-09-12-offpeak",
    model="deepseek-v4-pro",
    verified_on="2026-09-12",
    status="active",
    note=(
        "空闲时段价（= 高峰的一半）。"
        f"来源：官方定价页 {PRICING_PAGE_URL}，2026-09-12 核实。"
    ),
    input_hit=0.15, input_miss=4.5, output=13.5,
)

SNAPSHOTS: tuple[PriceSnapshot, ...] = (
    _CHAT_2025, _FLASH_PEAK, _FLASH_OFFPEAK, _V4PRO_PEAK, _V4PRO_OFFPEAK,
)

# 这个项目当前实际跑的模型（agent/llm.py 的 DeepSeekClient.DEFAULT_MODEL）。
DEFAULT_MODEL = "deepseek-chat"

# 查不到任何快照时的最后回落点。选 archived 的那条：它是**已知会被用到**的那个模型，
# 回落到一条现行模型的价去算一个不同模型的账，只会给出一个看着更可信的错数。
FALLBACK_SNAPSHOT_ID = _CHAT_2025.id


def resolve(model: str) -> tuple[PriceSnapshot, str | None]:
    """查 `model` 的价快照。返回 (快照, 告警)。

    **绝不返回 None、绝不抛异常**（调用方在生成报告，一个查不到价的模型不该让
    报告生成失败）。查不到时回落到 `FALLBACK_SNAPSHOT_ID` 并在第二个返回值里
    如实说明"这个成本是按别的模型估的"。

    同一模型有高峰/空闲两档时取**高峰档**：估成本要的是上界，用空闲档会给出一个
    偏小、且看起来同样可信的数。
    """
    exact = [s for s in SNAPSHOTS if s.model == model]
    if exact:
        active = [s for s in exact if s.status == "active"]
        pool = active or exact          # 现行优先；全 archived 就用 archived
        peak = [s for s in pool if "offpeak" not in s.id]
        chosen = (peak or pool)[0]
        warning = None
        if chosen.status == "archived":
            warning = (
                f"模型 {model} 的定价快照已归档（不在现行定价页列示）"
                f"，成本按历史快照 {chosen.id} 估算，实际计费单价未经核实"
            )
        return chosen, warning

    fallback = next(s for s in SNAPSHOTS if s.id == FALLBACK_SNAPSHOT_ID)
    return fallback, (
        f"没有模型 {model} 的定价快照 → 成本回落到 {fallback.id} 估算，"
        f"**仅供量级参考，不是该模型的价**"
    )


def snapshot_for(model: str) -> PriceSnapshot:
    """查 `model` 的价快照（不关心告警时用这个）。"""
    return resolve(model)[0]


def pricing_note(model: str) -> str:
    """一段可直接写进报告的成本口径说明（含快照 id 与时效性）。"""
    snap, warning = resolve(model)
    parts = [f"成本按定价快照 {snap.id} 估算"]
    if snap.verified_on:
        parts.append(f"（{snap.verified_on} 核实）")
    else:
        parts.append("（该快照从未对着官方定价页核实过）")
    if warning:
        parts.append(f"；⚠️ {warning}")
    return "".join(parts)
