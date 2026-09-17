"""跨 Agent 传递的唯一入口（任务 5.6，design.md §3.3、R21.3）。

## 这条闸门守的性质

R21.3：`Orchestrator` **不得**把一个 Agent 的原始输出直接作为另一个 Agent 的提示词内容注入，
除非该输出已通过结构化 schema 校验。design.md §3.3 把它落成一条代码强制的规则——`Orchestrator`
里不存在「把 Agent A 的文本塞进 Agent B 的提示词」的路径：

- `handoff()` 是**唯一**的跨 Agent 传递函数；它只接受**已校验的 Pydantic 契约实例**。一个裸
  `str`（Agent 的原始文本输出）根本无法进入——签名类型是 `AgentContract`，传 `str` 是类型错误，
  运行期也会被 `isinstance` 拒。
- 它只序列化契约**声明的字段**（`model_dump`），因此模型多吐的任何东西（若绕过 schema）都不会
  被带过边界。
- 自由文本字段（模型自由发挥的散文）由契约的 `untrusted_field_names()` 显式登记，随信封一起
  交给 `Context_Manager`——后者在注入下一段上下文前用 `<untrusted source="...">` 包裹（R23.2）。

`assemble_messages` 的签名只接受 `AgentContext`，而 `AgentContext.observations[*].payload_json`
只能由 `Tool_Registry` 写入——因此「原始 str 输出被当作提示词片段」在类型层面就没有入口
（design.md §3.3 末段）。本模块补上的是「跨 Agent 结构化传递」这条唯一被允许的路径。

## `HANDOFF_CONTRACTS`：每个目标 Agent 接受哪些契约

映射目标 Agent → 它可以接收的契约类型元组。`isinstance(payload, HANDOFF_CONTRACTS[target])`
用元组做多类型判定。用元组而非单一类型是因为一个 Agent 可能在多条路径上有不同契约（`Planning`
有 `RevisedPlanProposal` 与 `ExplanationDraft` 两条 P0 路径）。

契约与目标的绑定是**白名单式**的：只有登记在册的 (目标, 契约) 组合能通过。传错契约类型（例如
把摄取的 `ColumnMappingProposal` 交给 `Planning`）会被 `HandoffContractError` 拒——这正是 R21.3
物理隔离在跨 Agent 侧的落点（design.md §3.1 第 3 道机制：摄取输出没有路径进 Planning 提示词）。
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.agents.contracts import (
    AgentContract,
    ColumnMappingProposal,
    ExplanationDraft,
    PreferenceRuleCandidate,
    RevisedPlanProposal,
    RiskNarrative,
    ScenarioTranslation,
)

__all__ = [
    "HANDOFF_CONTRACTS",
    "AgentName",
    "HandoffContractError",
    "HandoffEnvelope",
    "handoff",
]

#: 跨 Agent 传递的目标 Agent 名（design.md ADR-001）。
AgentName = Literal["INGESTION_AGENT", "PLANNING_AGENT", "RISK_MONITOR_AGENT"]


class HandoffContractError(TypeError):
    """`payload` 不是目标 Agent 所接受的契约类型（R21.3）。

    这是 `TypeError` 而非可作为观察结果回给 Agent 的软错误：跨 Agent 传递错类型的载荷是
    **编程/编排错误**，不是模型可自我修正的调用错误。它应在装配/测试期就炸出来，而不是被静默
    转成一次「失败观察」——那会让「摄取输出被交给了 Planning」这种隔离破坏悄悄通过。
    """

    def __init__(self, target: str, got: type) -> None:
        self.target = target
        self.got = got
        accepted = ", ".join(c.__name__ for c in HANDOFF_CONTRACTS[target])  # type: ignore[index]
        super().__init__(
            f"handoff 到 {target} 只接受 [{accepted}]，实际收到 {got.__name__}。"
        )


class HandoffEnvelope(BaseModel):
    """一次跨 Agent 传递的信封（design.md §3.3）。

    `body` 只含契约**声明字段**的 JSON 投影（`model_dump(mode="json")`）；`untrusted_fields`
    是其中需要 `<untrusted>` 包裹的自由文本字段名——`Context_Manager` 据此包裹（R23.2）。
    `frozen=True`：信封一经装配即定型，序列化事实不可再被改动。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    target: AgentName
    body: dict[str, Any] = Field(default_factory=dict)
    untrusted_fields: frozenset[str] = Field(default_factory=frozenset)


#: 每个目标 Agent 接受的契约类型元组（见模块 docstring）。`MappingProxyType` 包裹 = 运行期
#: 不可变：没有 API 能给某个目标临时加一种可接受契约，扩展只能改这段源码（与 `TOOL_WHITELIST`
#: 同一思路）。
#:
#: - `INGESTION_AGENT`：`ColumnMappingProposal`（列映射提案）。
#: - `PLANNING_AGENT`：`RevisedPlanProposal`（重排）/ `ExplanationDraft`（解释）P0；
#:   `ScenarioTranslation` / `PreferenceRuleCandidate`（P1）。
#: - `RISK_MONITOR_AGENT`：`RiskNarrative`（P1）。
HANDOFF_CONTRACTS: dict[AgentName, tuple[type[AgentContract], ...]] = MappingProxyType(  # type: ignore[assignment]
    {
        "INGESTION_AGENT": (ColumnMappingProposal,),
        "PLANNING_AGENT": (
            RevisedPlanProposal,
            ExplanationDraft,
            ScenarioTranslation,
            PreferenceRuleCandidate,
        ),
        "RISK_MONITOR_AGENT": (RiskNarrative,),
    }
)


def handoff(payload: AgentContract, target: AgentName) -> HandoffEnvelope:
    """跨 Agent 传递的唯一入口（design.md §3.3）。

    `payload` 必须是**已校验的 Pydantic 契约实例**，且其类型在 `HANDOFF_CONTRACTS[target]` 中。
    只序列化契约声明的字段；自由文本字段由 `payload.untrusted_field_names()` 显式登记，随信封
    交给 `Context_Manager` 包裹（R23.2）。

    传入裸 `str` 或任意非契约对象一律被拒：签名类型是 `AgentContract`，运行期 `isinstance` 兜底
    ——「Agent 的原始 str 输出没有任何函数接受它作为提示词片段」这条不变量在此得到强制。
    """
    accepted = HANDOFF_CONTRACTS[target]
    if not isinstance(payload, accepted):
        raise HandoffContractError(target, type(payload))
    return HandoffEnvelope(
        target=target,
        body=payload.model_dump(mode="json"),
        untrusted_fields=payload.untrusted_field_names(),
    )
