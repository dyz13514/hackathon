"""价值台账端点（design.md Components §5「台账」分组、§6 `/value` 视图，任务 7.6 的 K-14 片）。

两个只读端点：

- `GET /api/value-ledger` —— 价值台账视图的数据源：K-14「自主处理 vs 上报人工」比例
  （R13.13）+ 每次影响分级裁决的**决定性判据**（R13.12，情节 8），**加上**完整的
  `ValueMetrics`（design.md §4.4 全字段：节省时间、按期率提升、拖期减少、消除的人工步骤、
  累计 token / 美元、`real_run_count` 等）、`manual_steps_eliminated` 的口径表、以及带
  MEASURED / ESTIMATED / PROJECTED 标签的 KPI 行（R19.1、R19.4–R19.7、R25.12/13）。
- `GET /api/value-ledger/export.csv` —— 把 KPI 行导出为 CSV（R19.8），列为
  `kpi_id, metric_name, current_value, baseline_value, delta, target_value, label, measured_at`。

## 范围说明（任务 11.4）

Task 7.6 只交付 K-14 计数与逐条判据；任务 11.4 在**同一端点上补齐** design.md §4.4 的
`ValueMetrics` 全字段与 CSV 导出。K-14 的 `autonomy_*` 字段与 ACTIVE 计划基线 KPI **原样保留**
（既有前端与测试依赖它们），新增字段以 `metrics` / `manual_steps` / `kpis` 三个块并列附加，
不改动既有字段的形状。全部确定性计算，不依赖 LLM（R19.7）。

## 为什么是读端点、无认证

与 `GET /state/dashboard`、`GET /plans/active` 同口径：`SessionAuthMiddleware` 只拦写方法，
`GET` 天然放行。台账在演示里要能直接打开看（情节 10）。
"""

from __future__ import annotations

import csv
import io
from datetime import datetime

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.db import models as orm
from app.services.runtime_clock import operational_now
from app.services.value_ledger import (
    MANUAL_STEP_RUBRIC,
    PROJECT_REAL_RUN_CAP,
    autonomy_decisions,
    autonomy_summary,
    build_value_metrics,
    kpi_rows,
    manual_steps_breakdown,
)

router = APIRouter(prefix="/value-ledger", tags=["value-ledger"])

#: CSV 导出列（R19.8，design.md §4.4）。顺序固定，逐字对应 KpiRow 字段。
CSV_COLUMNS = (
    "kpi_id",
    "metric_name",
    "current_value",
    "baseline_value",
    "delta",
    "target_value",
    "label",
    "measured_at",
)


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


class ValueMetricsOut(BaseModel):
    """完整 `ValueMetrics`（design.md §4.4，R19.1）。

    `labels` 逐字段带 MEASURED / ESTIMATED / PROJECTED 标签（R19.4/R19.6/R25.13）。
    """

    model_config = ConfigDict(extra="forbid")

    plan_id: str | None = None
    measured_at: str
    plan_generation_seconds: float | None = None
    disruption_response_seconds: float | None = None
    on_time_rate: float | None = None
    total_tardiness_minutes: int | None = None
    churn_ratio: float | None = None
    manual_steps_eliminated: int
    auto_handled_count: int
    escalated_count: int
    llm_tokens_used: int
    estimated_usd_cost: float
    real_run_count: int
    project_real_run_cap: int = Field(description="真实运行硬上限（150），供计算剩余配额")
    real_run_remaining: int = Field(description="剩余真实运行次数 = cap - real_run_count")
    baseline_plan_generation_seconds: float | None
    baseline_disruption_response_seconds: float | None
    baseline_on_time_rate: float | None = None
    baseline_total_tardiness_minutes: int | None = None
    projected_hero_demo_usd: float | None = None
    projected_build_total_usd: float | None = None
    labels: dict[str, str] = Field(description="逐字段 MEASURED/ESTIMATED/PROJECTED 标签")


class ManualStepEntryOut(BaseModel):
    """`manual_steps_eliminated` 口径表的一行（R19.5，UI 原样展示）。"""

    model_config = ConfigDict(extra="forbid")

    action: str
    label: str = Field(description="中文动作名")
    rule: str = Field(description="计 1 步的条件说明")
    count: int


class KpiRowOut(BaseModel):
    """一行 KPI（引用 K-01…K-18，R19.4）。字段与 CSV 列一致。"""

    model_config = ConfigDict(extra="forbid")

    kpi_id: str
    metric_name: str
    current_value: str
    baseline_value: str
    delta: str
    target_value: str
    label: str
    measured_at: str


class ValueLedgerOut(BaseModel):
    """`GET /value-ledger` 的响应。

    Task 7.6 的字段（`auto_handled_*` / `decisions` / ACTIVE 基线 KPI）**原样保留**；任务 11.4
    新增 `metrics`（完整 ValueMetrics）、`manual_steps`（口径表）、`kpis`（带标签的 KPI 行）。
    """

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
    # --- 任务 11.4 新增块 ---
    metrics: ValueMetricsOut = Field(description="完整 ValueMetrics（design.md §4.4）")
    manual_steps: list[ManualStepEntryOut] = Field(
        description="manual_steps_eliminated 口径表（R19.5）"
    )
    kpis: list[KpiRowOut] = Field(description="带 MEASURED/ESTIMATED/PROJECTED 标签的 KPI 行")


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
        return _build_ledger(db, now=operational_now(request.app.state.settings.app_env))


def _build_ledger(db: Session, *, now: datetime) -> ValueLedgerOut:
    """把 Task 7.6 的 K-14 块与任务 11.4 的 ValueMetrics/口径表/KPI 行组装成一个响应。"""
    summary = autonomy_summary(db)
    decisions = autonomy_decisions(db)
    metrics = build_value_metrics(db, now=now)
    steps = manual_steps_breakdown(db)
    rows = kpi_rows(metrics)

    active = db.execute(
        select(orm.ProductionPlan).where(orm.ProductionPlan.status == "ACTIVE")
    ).scalars().first()
    active_plan_id = active.plan_id if active is not None else None
    bc = (
        db.get(orm.BaselineComparison, active_plan_id)
        if active_plan_id is not None
        else None
    )

    metrics_out = ValueMetricsOut(
        plan_id=metrics.plan_id,
        measured_at=metrics.measured_at.isoformat(),
        plan_generation_seconds=metrics.plan_generation_seconds,
        disruption_response_seconds=metrics.disruption_response_seconds,
        on_time_rate=metrics.on_time_rate,
        total_tardiness_minutes=metrics.total_tardiness_minutes,
        churn_ratio=metrics.churn_ratio,
        manual_steps_eliminated=metrics.manual_steps_eliminated,
        auto_handled_count=metrics.auto_handled_count,
        escalated_count=metrics.escalated_count,
        llm_tokens_used=metrics.llm_tokens_used,
        estimated_usd_cost=float(metrics.estimated_usd_cost),
        real_run_count=metrics.real_run_count,
        project_real_run_cap=PROJECT_REAL_RUN_CAP,
        real_run_remaining=max(0, PROJECT_REAL_RUN_CAP - metrics.real_run_count),
        baseline_plan_generation_seconds=metrics.baseline_plan_generation_seconds,
        baseline_disruption_response_seconds=metrics.baseline_disruption_response_seconds,
        baseline_on_time_rate=metrics.baseline_on_time_rate,
        baseline_total_tardiness_minutes=metrics.baseline_total_tardiness_minutes,
        projected_hero_demo_usd=None,
        projected_build_total_usd=None,
        labels=dict(metrics.labels),
    )

    counts = steps.counts
    manual_steps = [
        ManualStepEntryOut(action=action, label=label, rule=rule, count=counts.get(action, 0))
        for action, label, rule in MANUAL_STEP_RUBRIC
    ]

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
        metrics=metrics_out,
        manual_steps=manual_steps,
        kpis=[
            KpiRowOut(
                kpi_id=r.kpi_id,
                metric_name=r.metric_name,
                current_value=r.current_value,
                baseline_value=r.baseline_value,
                delta=r.delta,
                target_value=r.target_value,
                label=r.label,
                measured_at=r.measured_at,
            )
            for r in rows
        ],
    )


@router.get(
    "/export.csv",
    summary="价值台账 CSV 导出（R19.8）：8 列，见 CSV_COLUMNS",
)
def export_value_ledger_csv(request: Request) -> StreamingResponse:
    """把 KPI 行导出为 CSV（R19.8）。列顺序固定为 `CSV_COLUMNS`。只读端点，无需认证。

    公式注入防护与 `Plan_Exporter` 同口径：以 `=`、`+`、`-`、`@` 等开头的文本单元格加单引号
    前缀转义，避免导出的 CSV 在 Excel 里被当公式执行（R20.6 的同类防护）。
    """
    factory: sessionmaker[Session] = request.app.state.session_factory
    with factory() as db:
        metrics = build_value_metrics(
            db, now=operational_now(request.app.state.settings.app_env)
        )
        rows = kpi_rows(metrics)

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(CSV_COLUMNS)
    for r in rows:
        writer.writerow(
            [
                _escape_csv(r.kpi_id),
                _escape_csv(r.metric_name),
                _escape_csv(r.current_value),
                _escape_csv(r.baseline_value),
                _escape_csv(r.delta),
                _escape_csv(r.target_value),
                _escape_csv(r.label),
                _escape_csv(r.measured_at),
            ]
        )
    buffer.seek(0)
    return StreamingResponse(
        iter([buffer.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="value-ledger.csv"'},
    )


def _escape_csv(value: str) -> str:
    """公式注入防护（R20.6 同口径）：以 `= + - @` 或制表/回车开头的值加单引号前缀。"""
    return "'" + value if value[:1] in "=+-@\t\r" else value
