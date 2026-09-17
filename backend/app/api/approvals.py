"""审批端点（design.md Components §5「审批」分组，任务 3.1 / 3.2）。

三个写端点，全部落到 `Approval_Service` 的一个方法上，再把结构化结果翻译成 design.md
Error Handling §2 的统一错误包：

- `POST /plans/{plan_id}/approve` —— 五步审批闸门，唯一能置 `ACTIVE` 的路径（R11.3、R12）。
- `POST /plans/{plan_id}/reject` —— 置 `REJECTED`，保留原 `ACTIVE`，理由 ≥5 字符（R11.4）。
- `POST /plans/{plan_id}/modify` —— 5 类结构化修改 → 新 `PENDING_APPROVAL` 版本（R11.5–7）。

## 为什么翻译发生在这一层

`Approval_Service` 在 `app/services/`，分层规则允许它用 SQLAlchemy 但仍不 import
`fastapi`（把 HTTP 语义留在边界是纪律，不是硬禁令）。因此服务方法返回判别联合结果对象
（`ApprovalResult` / `RejectResult` / `ModifyResult`），本层按 `status` 选 HTTP 状态码与
`ErrorCode`。这样服务方法可以在没有请求上下文的地方被调用（脚本、测试、将来的编排）。

## `now` 与 `actor`

`now` 取演示锚点 `DEMO_ANCHOR`（与 `plans.py` 生成端点同口径）：审批时的重校验要加载
「当时」的快照，P0 演示数据以锚点为「今天」。`actor` 取会话主体（`session.subject`）——
写端点受 `Session_Auth` 保护，主体进审计与决策记录。
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session, sessionmaker

from app.api.deps import PlannerSession
from app.api.errors import ErrorCode, NextAction, error_response
from app.seed.dataset import DEMO_ANCHOR
from app.services.approval import (
    ApprovalService,
    ApprovalStatus,
    Modification,
    ModifyStatus,
    RejectStatus,
)
from app.services.events import EventBus

router = APIRouter(prefix="/plans", tags=["approvals"])

#: 审批发生的时刻。P0 演示口径与 `plans.py` 一致（见模块 docstring）。
APPROVAL_NOW: datetime = DEMO_ANCHOR


# --------------------------------------------------------------------------
# 请求契约
# --------------------------------------------------------------------------


class ApproveIn(BaseModel):
    """`POST /plans/{id}/approve` 的请求体。`expected_version` 是乐观并发的期望行版本（R12.7）。"""

    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(ge=1, description="乐观并发的期望行版本")


class RejectIn(BaseModel):
    """`POST /plans/{id}/reject` 的请求体。`rejection_reason` 必填、≥5 字符（R11.4）。

    这里只声明字段存在，长度下限的**权威判定在服务层**（`Approval_Service.reject`）——
    它同时是 `EVAL-203` 的输入路径，服务层是唯一入口，校验放在那里才不会被别的调用方绕过。
    """

    model_config = ConfigDict(extra="forbid")

    rejection_reason: str = Field(description="拒绝理由，不受信任输入（R23.1）")


class ModifyIn(BaseModel):
    """`POST /plans/{id}/modify` 的请求体。`modifications` 是 5 类结构化修改的有序列表（R11.5）。"""

    model_config = ConfigDict(extra="forbid")

    modifications: list[Modification] = Field(min_length=1, description="5 类结构化修改")


# --------------------------------------------------------------------------
# 端点
# --------------------------------------------------------------------------


@router.post("/{plan_id}/approve", summary="五步审批闸门：唯一能置 ACTIVE 的路径（R11.3、R12）")
def approve_plan(
    request: Request, plan_id: str, body: ApproveIn, session: PlannerSession
) -> JSONResponse:
    """执行 `Approval_Service.approve()` 并翻译结果。"""
    factory: sessionmaker[Session] = request.app.state.session_factory
    events: EventBus = request.app.state.event_bus
    with factory() as db:
        service = ApprovalService(session=db, now=APPROVAL_NOW, events=events)
        result = service.approve(
            plan_id, actor=session.subject.upper(), expected_version=body.expected_version
        )

    if result.ok:
        return JSONResponse(status_code=200, content={"plan_id": result.plan_id, "status": "ACTIVE"})

    if result.status is ApprovalStatus.INVALID_STATE_TRANSITION:
        return error_response(
            status_code=409,
            code=ErrorCode.INVALID_STATE_TRANSITION,
            message=f"计划 {plan_id} 当前状态无法审批（仅 PENDING_APPROVAL 可批准）。",
            next_actions=[NextAction(action="view_pending", href="/plans/pending")],
            details={"current_status": result.current_status},
        )
    if result.status is ApprovalStatus.STALE_PROPOSAL:
        return error_response(
            status_code=409,
            code=ErrorCode.STALE_PROPOSAL,
            message="该提案所依赖的输入数据已在提案生成之后发生变化。请基于最新数据重新生成。",
            next_actions=[NextAction(action="regenerate", href="/plans/generate")],
            details={
                "proposal_version": result.proposal_version,
                "current_version": result.current_version,
            },
        )
    if result.status is ApprovalStatus.REVALIDATION_FAILED:
        return error_response(
            status_code=422,
            code=ErrorCode.REVALIDATION_FAILED,
            message="激活前重校验发现硬约束违反，计划保持待审批状态。",
            next_actions=[NextAction(action="modify", href=f"/plans/{plan_id}/modify")],
            details={"violations": [v.model_dump(mode="json") for v in result.violations]},
        )
    # CONCURRENT_MODIFICATION
    return error_response(
        status_code=409,
        code=ErrorCode.CONCURRENT_MODIFICATION,
        message="该计划已被另一操作修改，请刷新后重试。",
        next_actions=[NextAction(action="refresh", href=f"/plans/{plan_id}")],
        details={"current_status": result.current_status},
    )


@router.post("/{plan_id}/reject", summary="拒绝提案：置 REJECTED，保留原 ACTIVE（R11.4）")
def reject_plan(
    request: Request, plan_id: str, body: RejectIn, session: PlannerSession
) -> JSONResponse:
    """执行 `Approval_Service.reject()` 并翻译结果。"""
    factory: sessionmaker[Session] = request.app.state.session_factory
    events: EventBus = request.app.state.event_bus
    with factory() as db:
        service = ApprovalService(session=db, now=APPROVAL_NOW, events=events)
        result = service.reject(
            plan_id, actor=session.subject.upper(), rejection_reason=body.rejection_reason
        )

    if result.ok:
        return JSONResponse(
            status_code=200, content={"plan_id": result.plan_id, "status": "REJECTED"}
        )
    if result.status is RejectStatus.REASON_TOO_SHORT:
        return error_response(
            status_code=422,
            code=ErrorCode.REASON_TOO_SHORT,
            message="拒绝理由至少需要 5 个字符。",
            next_actions=[NextAction(action="retry")],
        )
    # INVALID_STATE_TRANSITION
    return error_response(
        status_code=409,
        code=ErrorCode.INVALID_STATE_TRANSITION,
        message=f"计划 {plan_id} 当前状态无法拒绝（仅 PENDING_APPROVAL 可拒绝）。",
        next_actions=[NextAction(action="view_pending", href="/plans/pending")],
        details={"current_status": result.current_status},
    )


@router.post(
    "/{plan_id}/modify", summary="5 类结构化修改 → 新的 PENDING_APPROVAL 版本（R11.5–7）"
)
def modify_plan(
    request: Request, plan_id: str, body: ModifyIn, session: PlannerSession
) -> JSONResponse:
    """执行 `Approval_Service.modify()` 并翻译结果。绝不直接激活（R11.7）。"""
    factory: sessionmaker[Session] = request.app.state.session_factory
    events: EventBus = request.app.state.event_bus
    with factory() as db:
        service = ApprovalService(session=db, now=APPROVAL_NOW, events=events)
        result = service.modify(
            plan_id, actor=session.subject.upper(), modifications=tuple(body.modifications)
        )

    if result.ok:
        return JSONResponse(
            status_code=201,
            content={
                "new_plan_id": result.new_plan_id,
                "source_plan_id": result.source_plan_id,
                "status": "PENDING_APPROVAL",
            },
        )
    if result.status is ModifyStatus.JOB_NOT_IN_PLAN:
        return error_response(
            status_code=422,
            code=ErrorCode.JOB_NOT_IN_PLAN,
            message=f"作业 {result.missing_job_id} 不在该计划的已排产作业里。",
            next_actions=[NextAction(action="refresh", href=f"/plans/{plan_id}")],
            details={"job_id": result.missing_job_id},
        )
    if result.status is ModifyStatus.MODIFICATION_REVALIDATION_FAILED:
        return error_response(
            status_code=422,
            code=ErrorCode.MODIFICATION_REVALIDATION_FAILED,
            message="修改后校验发现硬约束违反，计划状态未改变。",
            next_actions=[NextAction(action="revise", href=f"/plans/{plan_id}/modify")],
            details={"violations": [v.model_dump(mode="json") for v in result.violations]},
        )
    if result.status is ModifyStatus.PENDING_PLAN_EXISTS:
        return error_response(
            status_code=409,
            code=ErrorCode.PENDING_PLAN_EXISTS,
            message="该生产日已存在一个待审批计划，请先取消既有提案。",
            next_actions=[NextAction(action="cancel_pending", href="/plans/pending")],
        )
    # INVALID_STATE_TRANSITION
    return error_response(
        status_code=409,
        code=ErrorCode.INVALID_STATE_TRANSITION,
        message=f"计划 {plan_id} 当前状态无法修改（仅 PENDING_APPROVAL 可修改）。",
        next_actions=[NextAction(action="view_pending", href="/plans/pending")],
        details={"current_status": result.current_status},
    )
