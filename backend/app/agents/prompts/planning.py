"""`Planning_Agent` 的静态提示词与前缀（任务 5.6，design.md §3.3、ADR-001）。

`Planning_Agent` 处理排产编排与解释，拥有**提案写**权限（只读 + 计算 + `save_proposed_plan` /
`register_disruption` / `propose_preference_rule`，但**没有** `save_import_batch`，也没有任何能
把计划置 `ACTIVE` 的工具，R22.9）。它在两条 P0 路径上被接线，各有自己的输出契约：

- 重排（replanning）→ `RevisedPlanProposal`。
- 解释（explanation）→ `ExplanationDraft`。

两条路径共用同一份 [1][2][3][4][5] 段（同一 Agent、同一白名单、同一职责），只有 [6 OUTPUT] 段
按目标契约不同。因此本模块暴露一个接受目标契约的 `build_static_prefix(contract)`——同一
(Agent, 契约) 组合返回逐字节确定的前缀，而每条路径在其整个 ReAct 运行里用的是同一个契约，
故「同一运行各轮逐字节相同」的不变量成立。
"""

from __future__ import annotations

from app.agents.contracts import (
    AgentContract,
    ExplanationDraft,
    RevisedPlanProposal,
)
from app.agents.prompts.shared import static_prefix
from app.orchestrator.context_manager import StaticPrefix

__all__ = [
    "ROLE_BLOCK",
    "build_explanation_prefix",
    "build_replan_prefix",
    "build_static_prefix",
]

#: [1 ROLE]——每 Agent 不同。
ROLE_BLOCK = (
    "[ROLE]\n"
    "你是 Planning_Agent，负责在扰动发生时重排生产计划、并为计划生成解释叙述。"
    "你不是排产器：具体的时间与资源分配由确定性内核计算，你通过工具驱动它、"
    "并把结构化结果组织成提案或解释。你只能生成待审批的提案，无权把任何计划置为 ACTIVE。"
)


def build_static_prefix(output_contract: type[AgentContract]) -> StaticPrefix:
    """装配 `Planning_Agent` 在给定输出契约下的静态前缀。"""
    return static_prefix(
        "PLANNING_AGENT",
        role_block=ROLE_BLOCK,
        output_contract=output_contract,
    )


def build_replan_prefix() -> StaticPrefix:
    """重排路径前缀（输出契约 `RevisedPlanProposal`）。"""
    return build_static_prefix(RevisedPlanProposal)


def build_explanation_prefix() -> StaticPrefix:
    """解释路径前缀（输出契约 `ExplanationDraft`）。"""
    return build_static_prefix(ExplanationDraft)
