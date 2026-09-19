"""PLANNING_AGENT ReAct 重排路径的接线与安全边界（任务 7.4，R21.13、R13.11、design.md §2.2）。

用户为 Task 7.4 点名的 ReAct 相关断言（可在无 LIVE Bedrock 下验证）：

5. **PLANNING_AGENT 已接线**：注入桩驱动 + `RevisedPlanProposal` 契约后，
   `Orchestrator.run(REGISTER_DISRUPTION / REPLAN)` **不再抛 `RouteNotWiredError`**。
6. **STUB 桩驱动的 ReAct 运行成功**：脚本化回合序列（典型重排序列的收尾）走到 `final`，
   `outcome == OK`，全程不触达 Bedrock（桩驱动只吐字符串）。
7. **LLM 输出不能覆盖确定性数值**：伪造 `impact_class` / `autonomy_level` 的 Agent `final`
   被 `Guardrail_Layer` 剥离（`RESERVED_KEYS`），且分级由确定性 `classify_impact` 决定——
   ReAct 的 `final` 只承载 `revision_summary` 叙述。

这些测试构造 `Orchestrator` + `ScriptedReplanDriver`（approach b），与
`test_orchestrator_react_termination.py` 同构；驱动不触网、不消耗额度。ReAct 路径接线的正是
`app.agents.planning_agent` 提供的 `replan_contracts()` 与桩驱动。
"""

from __future__ import annotations

import json

from pydantic import BaseModel, ConfigDict

from app.agents.planning_agent import (
    PLANNING_AGENT,
    ScriptedReplanDriver,
    replan_contracts,
    scripted_replan_final,
)
from app.llm.budget import TokenBudgetManager
from app.orchestrator.orchestrator import (
    InMemoryTracer,
    Orchestrator,
    RouteNotWiredError,
)
from app.orchestrator.routing import Intent
from app.tools.registry import InMemoryToolCallRecorder, ToolRegistry


class _Payload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    disruption_id: str = "DSR-0001"


def _orchestrator(driver: ScriptedReplanDriver) -> tuple[Orchestrator, InMemoryTracer]:
    """构造注入了 PLANNING_AGENT 桩驱动与契约的 Orchestrator（与终止性测试同构）。

    注册表为空即可——本组测试的脚本要么直接产出 `final`，要么不需要真实工具执行。契约取
    `replan_contracts()`（即 `app.agents.planning_agent` 为重排路径提供的注入映射）。
    """
    tracer = InMemoryTracer()
    orch = Orchestrator(
        registry=ToolRegistry({}, recorder=InMemoryToolCallRecorder()),
        budget=TokenBudgetManager(),
        tracer=tracer,
        agent_drivers={PLANNING_AGENT: driver},
        contracts=replan_contracts(),
    )
    return orch, tracer


# --------------------------------------------------------------------------
# 5. PLANNING_AGENT 已接线：不再抛 RouteNotWiredError
# --------------------------------------------------------------------------


def test_register_disruption_no_longer_raises_route_not_wired() -> None:
    """注入驱动 + 契约后 `run(REGISTER_DISRUPTION)` 不抛 `RouteNotWiredError`（R21.13）。

    对照：不注入驱动/契约时该路径抛 `RouteNotWiredError`（历史行为）——这里证明接线生效。
    """
    final = scripted_replan_final(
        candidate_plan_id="PLAN-cand",
        baseline_plan_id="PLAN-active",
        feasibility="FEASIBLE",
        total_tardiness_minutes=0,
        churn_ratio=0.0,
        revision_summary="替代机器已找到，受影响作业改派完成。",
    )
    driver = ScriptedReplanDriver(turns=[final])
    orch, _ = _orchestrator(driver)
    # 不抛异常即证明已接线（终止性测试已覆盖各种失败 outcome）。
    result = orch.run(Intent.REGISTER_DISRUPTION, _Payload(), session_id="SESS-1")
    assert result.outcome == "OK"


def test_unwired_planning_agent_raises_route_not_wired() -> None:
    """对照：不注入驱动/契约 → `run(REPLAN)` 抛 `RouteNotWiredError`（接线前的行为）。"""
    tracer = InMemoryTracer()
    orch = Orchestrator(
        registry=ToolRegistry({}, recorder=InMemoryToolCallRecorder()),
        budget=TokenBudgetManager(),
        tracer=tracer,
        # 不注入 agent_drivers / contracts。
    )
    try:
        orch.run(Intent.REPLAN, _Payload(), session_id="SESS-2")
    except RouteNotWiredError:
        return
    raise AssertionError("未注入驱动时应抛 RouteNotWiredError")


# --------------------------------------------------------------------------
# 6. STUB 桩驱动的 ReAct 运行成功走到 final
# --------------------------------------------------------------------------


def test_scripted_react_run_reaches_final_ok() -> None:
    """脚本化 ReAct 运行走到 `final`（`RevisedPlanProposal`）→ `outcome == OK`，不触网。"""
    final = scripted_replan_final(
        candidate_plan_id="PLAN-cand",
        baseline_plan_id="PLAN-active",
        feasibility="PARTIAL",
        total_tardiness_minutes=120,
        churn_ratio=0.15,
        revision_summary="CNC-01 故障，3 个作业改派至 CNC-02，churn 0.15。",
    )
    driver = ScriptedReplanDriver(turns=[final])
    orch, tracer = _orchestrator(driver)
    result = orch.run(Intent.REPLAN, _Payload(), session_id="SESS-3")

    assert result.outcome == "OK"
    assert result.final is not None
    # final 是经契约校验的 RevisedPlanProposal，携带 revision_summary 叙述。
    assert result.final.revision_summary.startswith("CNC-01 故障")
    assert tracer.traces[0].outcome == "OK"


# --------------------------------------------------------------------------
# 7. LLM 输出不能覆盖确定性数值：保留键被剥离
# --------------------------------------------------------------------------


def test_forged_reserved_keys_in_final_are_stripped() -> None:
    """Agent 在 `final` 里伪造 `impact_class` / `autonomy_level` → 被剥离，仍成功校验（R13.11）。

    `RevisedPlanProposal` 的合法字段里没有 `impact_class` / `autonomy_level`——它们在
    `RESERVED_KEYS` 里，`Guardrail_Layer.validate_agent_output` 在进入契约前剥离并写审计。
    因此模型即便声称自己的影响分级，也无法让它进入结果对象；分级由确定性 `classify_impact`
    决定（`test_replan_disruption` 覆盖确定性侧）。
    """
    forged = json.dumps(
        {
            "final": {
                "candidate_plan_id": "PLAN-cand",
                "baseline_plan_id": "PLAN-active",
                "feasibility": "FEASIBLE",
                "total_tardiness_minutes": 0,
                "churn_ratio": 0.0,
                "revision_summary": "无影响。",
                # 越权声明——必须被剥离，不得进入 RevisedPlanProposal。
                "impact_class": "IMPACT_MINOR",
                "autonomy_level": "L4",
            }
        },
        ensure_ascii=False,
    )
    driver = ScriptedReplanDriver(turns=[forged])
    orch, _ = _orchestrator(driver)
    result = orch.run(Intent.REPLAN, _Payload(), session_id="SESS-4")

    # 剥离保留键后 final 仍是合法 RevisedPlanProposal（extra=forbid 对剩余字段通过）。
    assert result.outcome == "OK", result.error_code
    assert result.final is not None
    assert not hasattr(result.final, "impact_class")
    assert not hasattr(result.final, "autonomy_level")
