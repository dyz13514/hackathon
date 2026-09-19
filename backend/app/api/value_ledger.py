"""价值台账端点（design.md Components §5「台账」分组、§6 `/value` 视图，任务 7.6 的 K-14 片）。

一个只读端点：`GET /api/value-ledger` —— 供价值台账视图渲染「自主处理 vs 上报人工」的比例
（K-14，R13.13）与每次影响分级裁决的**决定性判据**（R13.12，情节 8 的展示对象）。

## 范围说明

Task 7.6 只要求本端点承载 K-14 的两个计数与逐条判据。完整的价值台账（节省时间、按期率提升、
拖期减少、消除的人工步骤、累计 token / 美元，design.md §4.4 的 `ValueMetrics`）是任务 11.x
的范围；本端点先把 Task 7.6 明确点名的「自主 vs 上报比例 + 决定性判据」交付，并附上当前 ACTIVE
计划的基线对比 KPI（已有数据，顺带展示），不预取 11.x 的字段以免与后续实现打架。

## 为什么是读端点、无认证

与 `GET /state/dashboard`、`GET /plans/active` 同口径：`SessionAuthMiddleware` 只拦写方法，
`GET` 天然放行。台账在演示里要能直接打开看（情节 10）。
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.db import models as orm
from app.services.value_ledger import autonomy_decisions, autonomy_summary

router = APIRouter(prefix="/value-ledger", tags=["value-ledger"])


# --------------------------------------------------------------------------
# 响应契约
# --------------------------------------------------------------------------


class AutonomyDecisionOut(BaseModel):
    """一次影响分级裁决的可展示摘要（R13.12）。"""

    model_config = ConfigDict(extra="forbid")

    assessment_id: str
    candidate_plan_id: str
    impact_class: str
    autonomy_level: str
    execution_path: str
    decisive_predicates: list[str]


class ValueLedgerOut(BaseModel):
    """`GET /value-ledger` 的响应：K-14 自主 vs 上报计数 + 逐条判据 + 基线 KPI（如有）。"""

    model_config = ConfigDict(extra="forbid")

    auto_handled_count: int = Field(
        description="自主处理数（execution_path ∈ PROPOSED/AUTO_APPLIED）"
    )
    escalated_count: int = Field(description="上报人工数（execution_path == ESCALATED）")
    total_decisions: int = Field(description="被分级的变更总数 = 两计数之和")
    auto_handled_ratio: float = Field(description="自主占比 ∈ [0,1]；无裁决时为 0")
    decisions: list[AutonomyDecisionOut] = Field(description="最近若干次裁决，逐条含决定性判据")
    active_plan_id: str | None = Field(default=None, description="当前 ACTIVE 计划（如有）")
    on_time_rate: float | None = Field(default=None, description="本计划按期率（相对 FCFS 基线）")
    baseline_on_time_rate: float | None = Field(default=None, description="FCFS 基线按期率")
    total_tardiness_minutes: int | None = Field(default=None, description="本计划拖期分钟")
    baseline_total_tardiness_minutes: int | None = Field(
        default=None, description="FCFS 基线拖期分钟"
    )


# --------------------------------------------------------------------------
# 端点
# --------------------------------------------------------------------------


@router.get(
    "",
    response_model=ValueLedgerOut,
    summary="价值台账：自主 vs 上报比例 + 每次判定的决定性判据（K-14、R13.12/R13.13）",
)
def get_value_ledger(request: Request) -> ValueLedgerOut:
    """只读端点：聚合 `impact_assessments` 的自主 vs 上报计数与逐条判据，附 ACTIVE 计划 KPI。

    计数与判据由 `services.value_ledger` 确定性地从 `impact_assessments` 重算——同输入同结果，
    不落派生表（见该模块 docstring）。基线 KPI 取当前 ACTIVE 计划的 `baseline_comparisons` 行，
    没有 ACTIVE 计划或没有基线对比时为 `None`（台账早于任何计划生成时的诚实空态）。
    """
    factory: sessionmaker[Session] = request.app.state.session_factory
    with factory() as db:
        summary = autonomy_summary(db)
        decisions = autonomy_decisions(db)

        active = db.execute(
            select(orm.ProductionPlan).where(orm.ProductionPlan.status == "ACTIVE")
        ).scalars().first()
        active_plan_id = active.plan_id if active is not None else None
        bc = (
            db.get(orm.BaselineComparison, active_plan_id)
            if active_plan_id is not None
            else None
        )

        return ValueLedgerOut(
            auto_handled_count=summary.auto_handled_count,
            escalated_count=summary.escalated_count,
            total_decisions=summary.total,
            auto_handled_ratio=summary.auto_handled_ratio,
            decisions=[
                AutonomyDecisionOut(
                    assessment_id=d.assessment_id,
                    candidate_plan_id=d.candidate_plan_id,
                    impact_class=d.impact_class,
                    autonomy_level=d.autonomy_level,
                    execution_path=d.execution_path,
                    decisive_predicates=list(d.decisive_predicates),
                )
                for d in decisions
            ],
            active_plan_id=active_plan_id,
            on_time_rate=float(bc.on_time_rate) if bc is not None else None,
            baseline_on_time_rate=float(bc.baseline_on_time_rate) if bc is not None else None,
            total_tardiness_minutes=bc.total_tardiness_minutes if bc is not None else None,
            baseline_total_tardiness_minutes=(
                bc.baseline_total_tardiness_minutes if bc is not None else None
            ),
        )
