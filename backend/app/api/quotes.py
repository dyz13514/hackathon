"""可承诺交期报价端点（design.md Components §3.7 / §6 `/quote`，任务 13.6，R17）。

- `POST /api/quotes/promise-date` —— 输入 `product_id` + `quantity` + 期望交期，通过
  `run_promise_date_sandbox(purpose=PROMISE_DATE)` 计算最早可承诺完工日、被推迟订单清单与总拖期
  变化；期望交期不可满足时返回最早可行日期与具体约束原因（R17.1–R17.3）。

**报价是只读沙箱计算**（R17.4）：不修改任何生产数据、`ACTIVE` 计划或 `input_snapshot_version`。
写端点（受 `Session_Auth` 保护——发起一次报价推演是有意的操作，与 What-if / 采纳同一信任边界）。
无 `ACTIVE` 计划 → `NO_ACTIVE_PLAN`。指向不存在的产品 → `SCENARIO_INVALID_MUTATION`。
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session, sessionmaker

from app.api.deps import PlannerSession
from app.api.errors import ErrorCode, NextAction, error_response
from app.core.sandbox import ScenarioMutationError
from app.services.replanning import NoActivePlanError
from app.services.runtime_clock import operational_now
from app.services.sandbox import run_promise_date_sandbox

router = APIRouter(prefix="/quotes", tags=["quotes"])


class PromiseDateRequest(BaseModel):
    """`POST /quotes/promise-date` 请求体（R17.1）。"""

    model_config = ConfigDict(extra="forbid")

    product_id: str = Field(min_length=1)
    quantity: float = Field(gt=0)
    desired_due_date: datetime


class PromiseDateResponse(BaseModel):
    """报价结果（R17.1–R17.3）。全部数值由只读沙箱确定性实算。"""

    model_config = ConfigDict(extra="forbid")

    feasible: bool
    #: 最早可承诺完工时刻（ISO8601）；不可行为 null。
    earliest_completion: datetime | None
    desired_due_date: datetime
    desired_date_met: bool
    #: 因插入这笔询价而新增迟交的既有订单（R17.2）。
    deferred_order_ids: list[str]
    total_tardiness_minutes: int
    active_total_tardiness_minutes: int
    total_tardiness_delta_minutes: int
    #: 不可行 / 无法满足期望交期时的具体约束原因（R17.3）。
    constraint_reason: str | None


@router.post(
    "/promise-date",
    response_model=PromiseDateResponse,
    summary="可承诺交期报价（只读沙箱实算最早完工日，R17）",
)
def promise_date(
    request: Request, body: PromiseDateRequest, session: PlannerSession
) -> PromiseDateResponse | JSONResponse:
    """计算一笔询价的最早可承诺完工日。**只读沙箱、无 LLM、不改动任何生产数据或计划（R17.4）。**"""
    factory: sessionmaker[Session] = request.app.state.session_factory
    with factory() as db:
        try:
            result = run_promise_date_sandbox(
                db,
                product_id=body.product_id,
                quantity=body.quantity,
                desired_due_date=body.desired_due_date,
                now=operational_now(request.app.state.settings.app_env),
            )
        except NoActivePlanError:
            return error_response(
                status_code=409,
                code=ErrorCode.NO_ACTIVE_PLAN,
                message="There is no ACTIVE plan, so a promise date cannot be computed. Please generate and approve a plan first.",
                next_actions=[NextAction(action="generate_plan", href="/plans/generate")],
            )
        except ScenarioMutationError as error:
            return error_response(
                status_code=422,
                code=ErrorCode.SCENARIO_INVALID_MUTATION,
                message=str(error),
                next_actions=[NextAction(action="fix_quote", href="/quote")],
            )
    return PromiseDateResponse(
        feasible=result.feasible,
        earliest_completion=result.earliest_completion,
        desired_due_date=result.desired_due_date,
        desired_date_met=result.desired_date_met,
        deferred_order_ids=list(result.deferred_order_ids),
        total_tardiness_minutes=result.total_tardiness_minutes,
        active_total_tardiness_minutes=result.active_total_tardiness_minutes,
        total_tardiness_delta_minutes=result.total_tardiness_delta_minutes,
        constraint_reason=result.constraint_reason,
    )
