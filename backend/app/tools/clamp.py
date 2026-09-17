"""字段投影与 token 硬截断（任务 5.1，R22.12 / R22.16 / R25.6）。

`Tool_Registry.invoke()` 的第 ⑤、⑥ 步在这里落地。两者服务同一个目的——把「注入 Agent
上下文的工具响应」压在 2,000 token 以内（R22.16 与 R25.6 是同一约束），但分工不同：

- **投影（`project`）** 是**语义**层的收窄：Agent 显式声明只要 `order_id` 与 `due_date`
  两个字段，就不该把整条 `OrderBrief` 塞回去。它减小的是「本来就不需要」的部分。
- **截断（`clamp_tokens`）** 是**兜底**层的保险：即使投影后、即使句柄式返回，某个响应仍
  可能超过上限（例如 `get_job_details` 一次要 10 条明细）。它是最后一道，保证**无论如何**
  注入上下文的字节数有一个硬顶，超出即标 `truncated`，让 Agent 知道「这里被砍过」。

## 为什么 token 计数是一个确定性启发式而不是真的分词器

真正的分词器（tiktoken 之类）会引入一个运行期依赖，且其结果依赖模型版本——而 R5.7 要求
「同样输入必得同样输出」。这里要守的不是「精确等于 Bedrock 计费的 token 数」，而是「有一个
稳定、单调、无外部依赖的上界估计」：截断阈值宁可偏保守（略微高估），也不能因为换了个库或
模型版本让同一份 payload 时而超限时而不超。因此用 `ceil(len(utf-8 字节) / CHARS_PER_TOKEN)`
——UTF-8 字节数对中文（每字 3 字节）天然给出比字符数更高的估计，方向正确。

这条估计的经济学后果在 `EVAL-015` 的周期 token 断言与 `Context_Manager` 的增长斜率断言
（任务 5.5）上另有探测，因此即使这里偏差几个百分点，也不会让「上下文烧穿预算」这件事无声
发生。
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterable, Mapping
from typing import Any, Final

__all__ = [
    "CHARS_PER_TOKEN",
    "clamp_tokens",
    "count_tokens",
    "project",
]

#: 每 token 折算的 UTF-8 字节数。4 是英文文本的常见经验值；对中文（每字 3 字节）它给出
#: 偏高的估计，这正是「上界」想要的方向——见模块 docstring。
CHARS_PER_TOKEN: Final[int] = 4

#: 截断时替换超限尾部的占位。它本身也占 token，因此截断预算里要给它留位（见 `clamp_tokens`）。
_TRUNCATION_MARKER: Final[str] = "…[TRUNCATED]"


def _canonical_json(payload: Any) -> str:
    """把 payload 序列化成稳定字节序。

    `sort_keys=True` 让同一份内容无论构造顺序如何都得到同一串字节——token 计数因此对
    「字段声明顺序」不敏感，与 R5.7 的可重现性一致。`ensure_ascii=False` 保留中文原样，
    使字节数反映真实体积而不是 `\\uXXXX` 转义后的膨胀值。
    """
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)


def count_tokens(payload: Any) -> int:
    """payload 的 token 数上界估计。

    先序列化成规范 JSON，再按 UTF-8 字节数除以 `CHARS_PER_TOKEN` 向上取整。向上取整保证
    空 payload 也不会算成 0 token 之外的负数，且任何非空内容至少记 1 token。
    """
    byte_length = len(_canonical_json(payload).encode("utf-8"))
    return math.ceil(byte_length / CHARS_PER_TOKEN)


def project(payload: Mapping[str, Any], fields: Iterable[str] | None) -> dict[str, Any]:
    """字段投影：`fields is None` 原样返回，否则只保留请求集合里的键（R22.12）。

    **投影只在顶层生效，且只做子集过滤，不做重命名或嵌套裁剪。** 这是刻意的：契约测试
    断言「投影后的字段集合是请求集合的子集」，一个会往里加字段（或改名）的投影会让这条
    断言失去意义。请求了但 payload 里根本不存在的字段被静默忽略——那不是错误，只是「这
    条记录没有那个字段」，例如 `changed_job_count` 在初次生成的计划上为 `None` 而调用方
    仍把它列进 `fields`。

    返回的是一个新 dict，不改动入参：调用方（`invoke` 第 ⑤ 步）随后还要把它交给截断，
    原地修改会让「投影前的完整输出」在同一次调用里不可再取。
    """
    if fields is None:
        return dict(payload)
    requested = set(fields)
    return {key: value for key, value in payload.items() if key in requested}


def _truncate_string(value: str, max_tokens: int) -> str:
    """把单个字符串截到 `max_tokens` 以内，尾部接 `_TRUNCATION_MARKER`。

    预算里先扣掉 marker 自身的 token，剩下的换算成字节再换算成字符上界。marker 比整个预算
    还长这种极端情形下（`max_tokens` 极小），至少保留 marker 本身，绝不返回空串——「被截断
    了」这个信号比「保留了几个字符」更重要。
    """
    marker_tokens = count_tokens(_TRUNCATION_MARKER)
    body_tokens = max(max_tokens - marker_tokens, 0)
    keep_chars = body_tokens * CHARS_PER_TOKEN
    return value[:keep_chars] + _TRUNCATION_MARKER


def clamp_tokens(
    payload: dict[str, Any], max_response_tokens: int
) -> tuple[dict[str, Any], bool, int]:
    """把 payload 硬截断至 `max_response_tokens`，返回 `(payload, truncated, n_tokens)`。

    R22.16 / R25.6：注入上下文的任何工具响应不得超过其 `max_response_tokens`。本函数是这条
    约束的**唯一**强制点——不依赖任何调用方「记得控制返回大小」。

    截断策略分两级，都保持返回值仍是合法 JSON 对象（Agent 侧要 `json.loads`，截成半截字节
    会让整条观察不可解析，比超限更糟）：

    1. **未超限**：原样返回，`truncated=False`。这是绝大多数句柄式返回（`PlanHandle` ≈ 120
       token）与聚合返回走的路径。
    2. **超限**：逐个顶层字段丢弃，从**序列化后体积最大**的字段开始丢，直到总量落回上限；
       每丢一个字段就往 payload 里记一条 `_truncated_fields` 说明，使「哪些被砍了」在观察
       结果里可见而不是凭空消失。若丢到只剩说明字段仍超限（单个说明就撑爆极小预算），把
       说明本身的字符串也截短。

    为什么按体积从大到小丢：一次 `get_job_details` 超限，最该被砍的是那条最占地方的明细，
    而不是碰巧排在最后的小字段。这让截断的信息损失最小化，同时保持确定性（体积相同时按键名
    排序 tie-break）。
    """
    n_tokens = count_tokens(payload)
    if n_tokens <= max_response_tokens:
        return payload, False, n_tokens

    working = dict(payload)
    dropped: list[str] = []
    # 按 (序列化字节数, 键名) 降序：先丢最大的，字节数相同则按键名逆序，保持确定性。
    droppable = sorted(
        working.keys(),
        key=lambda key: (len(_canonical_json(working[key]).encode("utf-8")), key),
        reverse=True,
    )
    for key in droppable:
        if count_tokens(working) <= max_response_tokens:
            break
        del working[key]
        dropped.append(key)
        working["_truncated_fields"] = sorted(dropped)

    # 仍超限：说明字段本身太长（极小预算下可能发生），把它截成字符串。
    if count_tokens(working) > max_response_tokens and "_truncated_fields" in working:
        summary = "dropped:" + ",".join(sorted(dropped))
        working["_truncated_fields"] = _truncate_string(summary, max_response_tokens)

    return working, True, count_tokens(working)
