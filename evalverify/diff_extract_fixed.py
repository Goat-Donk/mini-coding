"""修好的 unified diff 抠取器 —— **只用于离线复核，`eval/runner.py` 已冻结、不改**。

## 为什么会有这个文件

三臂跑完后（2026-09-12）发现 single-shot 臂有 7 个任务记 `patch_failed`，而 `patch_failed`
会被摘出完成率分母 —— 也就是说**一个解析器 bug 直接决定了头号数字**。用户裁定：
**不重跑模型、不改 `runner.py`**（三臂必须同一份代码才可比），改走离线复核
（报告里存了每个任务的 `raw_output`，零 LLM 成本重抠 + 重落 + 重判）。

## 现行解析器的 bug（`eval/runner.py:279`，已实测定位）

```python
_FENCE_RE = re.compile(r"```(?:diff|patch)?[ \\t]*\\n(.*?)```", re.DOTALL)
```

开围栏只认 ```` ``` ```` / ```` ```diff ```` / ```` ```patch ````。模型回复里 diff 块**前面**
通常有一个 ```` ```python ```` 代码块（贴它分析的代码），那个围栏**当不了开围栏**
（"python" 既不匹配 `diff` 也不匹配 `patch`，后面的 `\\n` 也对不上）。

于是**围栏配对整个错位一格**：

```
行 6   ```python      ← 当不了开围栏，被引擎跳过
行 27  ```            ← 于是它成了「开围栏」
行 38  ```diff        ← 却被当成上一个块的「闭围栏」吃掉了
行 39  diff --git     ← 真正的 diff 从这里开始……但已经没有开围栏了
行 79  ```
```

`findall` 因此抓不到 diff 块，落到兜底分支 `text[idx:]` —— **从 `diff --git` 一路切到全文结尾**，
把模型后面的「改了哪些文件 / 测试结果」整段散文塞给 `git apply` → `corrupt patch`。

实测：7 个 `patch_failed` 里 **5 个**是这一个机制造成的（另 2 个是模型自己的问题：
一个编造了不存在的上下文、一个压根没输出 diff）。

## 修复

两处，各自独立可验证：

- **B 档**：开围栏放开 info string（```` ```[^\\n]* ````）。错位消失，diff 块正常闭合。
- **C 档**：B 之后仍走兜底时，遇到**第一个裸围栏就停**，不要一路切到全文结尾 ——
  补丁后面那段散文不是补丁的一部分。

B 与 C 在这 7 个任务上结果相同（C 只是更保守），所以**留档口径用 C**。
"""
from __future__ import annotations

import re

#: 开围栏允许**任意** info string（`python` / `text` / 空 / `diff` / `patch` 都算）。
_FENCE_ANY = re.compile(r"```[^\n]*\n(.*?)```", re.DOTALL)

#: 兜底时用来找「第一个裸围栏」的位置。
_BARE_FENCE = re.compile(r"^[ \t]*```[ \t]*$", re.MULTILINE)


def _is_diff_block(block: str) -> bool:
    return "diff --git" in block or block.lstrip().startswith("---")


def extract_diff_fixed(text: str) -> str:
    """修正版抠取。抠不到返回空串（→ 仍记 `patch_failed`，**不猜**）。

    与现行版的唯一区别就是上面那两处；其余（选最长块、抠不到就空串）保持一致，
    这样"修好之后差异全部来自这两处"是可验证的，而不是一锅乱炖。
    """
    text = text or ""
    blocks = [b for b in _FENCE_ANY.findall(text) if _is_diff_block(b)]
    if blocks:
        return max(blocks, key=len).strip() + "\n"

    idx = text.find("diff --git")          # 没围栏但直接贴了 diff
    if idx < 0:
        return ""
    tail = text[idx:]
    stop = _BARE_FENCE.search(tail)        # ← 补丁后第一个裸围栏就是它的尽头
    if stop:
        tail = tail[: stop.start()]
    return tail.strip() + "\n"
