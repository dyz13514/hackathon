"""瓶颈与产能洞察端点（design.md Components §3.7 / §6 `/insights`，任务 13.5，R15）。

- `GET /api/insights/bottlenecks` —— 当前 `ACTIVE` 计划的每机器利用率 / 作业数 / 订单价值占比、
  关键机器标识、按技能聚合的缺口，以及每台机器「可用工时 +20% 时 `total_tardiness_minutes` 的
  变化量」（由 `run_capacity_sandbox(purpose=BOTTLENECK)` 实算，R15.2）。

只读端点（无需认证，与其余 GET 同口径）。全部数值确定性、无 LLM；+20% 变化量在沙箱隔离下实算，
不写任何生产数据。无 `ACTIVE` 计划 → `NO_ACTIVE_PLAN`。
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session, sessionmaker

from app.api.errors import ErrorCode, NextAction, error_response
from app.services.insights import NoActivePlanError, bottleneck_insights
from app.services.runtime_clock import operational_now

router = APIRouter(prefix="/insights", tags=["insights"])


class MachineInsightOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    machine_id: str
    machine_type: str
    capabilities: list[str]
    utilisation: float
    busy_minutes: int
    available_minutes: int
    job_count: int
    order_value_share: float
    is_critical: bool
    tardiness_delta_if_plus_20pct: int


class SkillGapOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    skill: str
    required_minutes: int
    available_minutes: int
    gap_minutes: int


class BottleneckInsightsOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    active_plan_id: str | None
    machines: list[MachineInsightOut]
    skill_gaps: list[SkillGapOut]


@router.get(
    "/bottlenecks",
    response_model=BottleneckInsightsOut,
    summary="瓶颈与产能洞察：利用率 / 关键机器 / 技能缺口 / +20% 工时拖期变化（R15）",
)
def get_bottlenecks(request: Request) -> BottleneckInsightsOut | JSONResponse:
    """计算当前 `ACTIVE` 计划的瓶颈与产能洞察。只读，无 LLM。"""
    factory: sessionmaker[Session] = request.app.state.session_factory
    with factory() as db:
        try:
            result = bottleneck_insights(
                db, now=operational_now(request.app.state.settings.app_env)
            )
        except NoActivePlanError:
            return error_response(
                status_code=409,
                code=ErrorCode.NO_ACTIVE_PLAN,
                message="There is no ACTIVE plan, so bottleneck insights cannot be computed. Please generate and approve a plan first.",
                next_actions=[NextAction(action="generate_plan", href="/plans/generate")],
            )
    return BottleneckInsightsOut(
        active_plan_id=result.active_plan_id,
        machines=[
            MachineInsightOut(
                machine_id=m.machine_id,
                machine_type=m.machine_type,
                capabilities=list(m.capabilities),
                utilisation=m.utilisation,
                busy_minutes=m.busy_minutes,
                available_minutes=m.available_minutes,
                job_count=m.job_count,
                order_value_share=m.order_value_share,
                is_critical=m.is_critical,
                tardiness_delta_if_plus_20pct=m.tardiness_delta_if_plus_20pct,
            )
            for m in result.machines
        ],
        skill_gaps=[
            SkillGapOut(
                skill=g.skill,
                required_minutes=g.required_minutes,
                available_minutes=g.available_minutes,
                gap_minutes=g.gap_minutes,
            )
            for g in result.skill_gaps
        ],
    )
