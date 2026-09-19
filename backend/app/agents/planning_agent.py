"""`Planning_Agent` 的 ReAct 驱动（任务 7.4，形态 B，design.md §2.2 / §3.3、R21.13）。

`Orchestrator._react_loop` 通过一个 `AgentDriver`（`next_turn(ctx) -> AgentTurn | None`）取
「下一轮模型输出」。P0 之前没有为 `PLANNING_AGENT` 注入过驱动，因此
`Orchestrator.run(REGISTER_DISRUPTION / REPLAN)` 会抛 `RouteNotWiredError`。本模块提供两个
驱动，都产出 `AgentTurn(raw=<JSON 字符串>)`，与 `_process_turn` 的解析契约一致：

1. **`PlanningAgentDriver`**（真实驱动）：用 `Context_Manager.assemble_messages(ctx, prefix=...)`
   装配请求，交 `BedrockAdapter.invoke()`，把响应文本包成 `AgentTurn`。在 `STUB` / `REPLAY`
   模式下，`BedrockAdapter.invoke` 从 cassette 取回响应（不触网）；`DISABLED` 模式抛
   `LlmDisabledError`——那由降级路径（任务 11.6 的确定性重排流水线）承接，不在本模块。

2. **`ScriptedReplanDriver`**（脚本化桩驱动）：按一个预置的「回合脚本」逐轮返回 JSON 字符串，
   **完全不触达 Bedrock、不需要 cassette**。它让 ReAct 循环在没有真实/录制 LLM 响应时也能被
   端到端测试：典型重排序列
   `get_affected_jobs → generate_schedule(freeze) → check_constraints → classify_impact →
   compare_plans → save_proposed_plan`，最后一轮吐一个 `{"final": {...}}`（`RevisedPlanProposal`）。
   这是用户裁决的 approach (b)：ReAct 路径「已接线、可用桩驱动测试」，且 LLM 只提供
   `revision_summary` 叙述，**绝不产生任何调度/影响数值**——那些数字全部来自确定性工具的观察
   结果（design.md §3.6、ADR-010）。

## 为什么 LLM 无法覆盖确定性数值

`RevisedPlanProposal` 的数值字段（`total_tardiness_minutes`、`churn_ratio`、`feasibility`）
来自模型输出，但它们**不被任何执行路径当作权威**：`ImpactAnalysis` 与落库计划的全部数字由
`replan_deterministic.run_replan` 的确定性组件算出（`compute_plan_delta` / `classify_impact` /
`_plan_kpis`）。ReAct 的 `final` 只承载 `revision_summary` 这段叙述供展示；即便模型在数值字段
里填了错的数，`Guardrail_Layer` 的保留键剥离（`impact_class` / `feasibility` 等在 `RESERVED_KEYS`
里）会先剪掉越权字段，且 API 展示与审批读的是持久化的确定性计划，不是 `final` 的数值。测试
`test_replan_react.py` 对此有断言。

## 分层

本模块住在 `app/agents/`，`tests/structure/test_layering.py` 第 ② 条断言它**不 import 内核或
`tools/handlers`**。因此这里只 import `app.orchestrator.context_manager`（装配纯函数）、
`app.llm.adapter`（LLM 出口）、`app.agents.*`（契约与提示词）、以及 `Orchestrator` 的
`AgentDriver` / `AgentTurn` 协议类型。脚本化桩驱动只吐字符串，不碰任何内核类型。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from app.agents.contracts import ExplanationDraft, RevisedPlanProposal
from app.agents.prompts.planning import build_explanation_prefix, build_replan_prefix
from app.llm.adapter import BedrockAdapter, LlmRequest
from app.orchestrator.context_manager import AgentContext, StaticPrefix, assemble_messages
from app.orchestrator.orchestrator import AgentTurn

#: `PLANNING_AGENT` 在编排里的名字（与 `MAX_AGENT_STEPS` / `TOOL_WHITELIST` 的键一致）。
PLANNING_AGENT = "PLANNING_AGENT"


# --------------------------------------------------------------------------
# 真实驱动：Context_Manager 装配 + Bedrock_Adapter 调用
# --------------------------------------------------------------------------


class PlanningAgentDriver:
    """真实 ReAct 驱动：每轮装配上下文 → 调 `BedrockAdapter.invoke` → 返回 `AgentTurn`。

    `prefix` 是本次运行的静态前缀（重排用 `build_replan_prefix()`，逐轮逐字节相同——这正是
    「装配是纯函数」赖以成立的前提）。`max_tokens` 给单轮响应上限，`REPLANNING` 作用域的
    14,000 token 硬上限由 `Token_Budget_Manager` 在循环外把关，不在这里。

    在 `STUB` / `REPLAY` 模式下 `invoke` 从 cassette 取回响应（不触网）；`LIVE` 才真实调用。
    本类不判断模式——那是 `BedrockAdapter` 的职责，符合「全仓库唯一 LLM 出口」（任务 5.3）。
    """

    def __init__(
        self,
        adapter: BedrockAdapter,
        *,
        prefix: StaticPrefix,
        max_tokens: int = 800,
    ) -> None:
        self._adapter = adapter
        self._prefix = prefix
        self._max_tokens = max_tokens

    def next_turn(self, ctx: AgentContext) -> AgentTurn | None:
        request: LlmRequest = assemble_messages(ctx, prefix=self._prefix)
        # `assemble_messages` 返回的 LlmRequest 的 max_tokens 由装配决定；这里若需另设上限，
        # 用 model_copy 覆盖（保持前缀/user 不变，故哈希只随 max_tokens 变——与缓存一致）。
        if request.max_tokens != self._max_tokens:
            request = request.model_copy(update={"max_tokens": self._max_tokens})
        response = self._adapter.invoke(request)
        return AgentTurn(raw=response.content)


def build_replan_driver(adapter: BedrockAdapter) -> PlanningAgentDriver:
    """重排路径的真实驱动（输出契约 `RevisedPlanProposal`）。"""
    return PlanningAgentDriver(adapter, prefix=build_replan_prefix())


def build_explanation_driver(adapter: BedrockAdapter) -> PlanningAgentDriver:
    """解释路径的真实驱动（输出契约 `ExplanationDraft`）。P0 解释走 §5.11 的单次调用，本驱动
    是 ReAct 形态的备用接线口，当前不在主路径。"""
    return PlanningAgentDriver(adapter, prefix=build_explanation_prefix())


# --------------------------------------------------------------------------
# 脚本化桩驱动：不触达 Bedrock，逐轮返回预置 JSON
# --------------------------------------------------------------------------


@dataclass
class ScriptedReplanDriver:
    """按预置脚本逐轮产出 ReAct 回合的桩驱动（approach b：ReAct 可用桩测试）。

    `turns` 是一串 JSON 字符串，每次 `next_turn` 弹出下一条。每条要么是
    `{"action": {"tool": ..., "args": {...}}}`（工具调用轮），要么是 `{"final": {...}}`
    （终止轮）。脚本耗尽后返回 `None`（模拟模型停止响应，循环视作一次错误观察）。

    它**不 import 内核、不触网**——只吐字符串。真实数值由确定性工具 handler 在
    `_dispatch_action` 里算出并作为观察结果回给循环；本驱动的 `final` 只携带
    `revision_summary` 叙述。这样一次 ReAct 运行可在 `STUB` 环境里端到端通过，且不存在
    「桩驱动伪造了调度数字」的可能——数字不由它产生。
    """

    turns: Sequence[str]
    _index: int = field(default=0, init=False)

    def next_turn(self, ctx: AgentContext) -> AgentTurn | None:  # noqa: ARG002 - 桩忽略 ctx
        if self._index >= len(self.turns):
            return None
        raw = self.turns[self._index]
        self._index += 1
        return AgentTurn(raw=raw)


def scripted_replan_final(
    *,
    candidate_plan_id: str,
    baseline_plan_id: str | None,
    feasibility: str,
    total_tardiness_minutes: int,
    churn_ratio: float,
    revision_summary: str,
) -> str:
    """构造一个 `{"final": {...}}` 的 JSON 字符串（`RevisedPlanProposal` 形状）。

    供脚本化桩驱动与测试构造终止轮。数值字段按契约填，但如模块 docstring 所述，它们不被当作
    权威——执行路径读的是确定性计划。
    """
    import json

    return json.dumps(
        {
            "final": {
                "candidate_plan_id": candidate_plan_id,
                "baseline_plan_id": baseline_plan_id,
                "feasibility": feasibility,
                "total_tardiness_minutes": total_tardiness_minutes,
                "churn_ratio": churn_ratio,
                "revision_summary": revision_summary,
            }
        },
        ensure_ascii=False,
    )


def scripted_action(tool: str, **args: object) -> str:
    """构造一个 `{"action": {"tool": ..., "args": {...}}}` 的 JSON 字符串（工具调用轮）。"""
    import json

    return json.dumps({"action": {"tool": tool, "args": args}}, ensure_ascii=False)


# --------------------------------------------------------------------------
# Orchestrator 注入映射
# --------------------------------------------------------------------------


def replan_contracts() -> dict[str, type]:
    """`Orchestrator(contracts=...)` 的注入：重排路径的输出契约。

    `REGISTER_DISRUPTION` / `REPLAN` 都路由到 `PLANNING_AGENT`，输出契约 `RevisedPlanProposal`。
    """
    return {PLANNING_AGENT: RevisedPlanProposal}


__all__ = [
    "PLANNING_AGENT",
    "PlanningAgentDriver",
    "ScriptedReplanDriver",
    "build_replan_driver",
    "build_explanation_driver",
    "scripted_replan_final",
    "scripted_action",
    "replan_contracts",
    "RevisedPlanProposal",
    "ExplanationDraft",
]
