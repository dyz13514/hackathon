"""L4 自动应用记录与一键回滚端点（任务 13.4，P1-J ④，R13.9/R13.10）。

两个端点：

- `GET  /api/autonomy/changes`              列出全部自动应用记录（供前端顶栏通知区）。
- `POST /api/autonomy/changes/{id}/revert`  一键回滚一个自动应用变更（写端点，受 Session_Auth）。

## 回滚的安全边界

回滚经 `services.auto_apply.revert_change`：从 `snapshot_before` 重建计划，再经
`Approval_Service.activate_internal()` 做**完整硬约束重校验**——回滚也不能产生违规计划
（R13.10）。校验失败返回 `REVALIDATION_FAILED`（422）并附违反清单，什么都不改。
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session, sessionmaker

from app.api.deps import PlannerSession
from app.api.errors import ErrorCode, NextAction, error_response
from app.db import models as orm
from app.services.auto_apply import (
    RevertStatus,
    list_auto_applied_changes,
    revert_change,
)
from app.services.runtime_clock import operational_now

router = APIRouter(prefix="/autonomy", tags=["autonomy"])


class AutoAppliedChangeOut(BaseModel):
    """一条自动应用记录（R13.9）。供前端通知区呈现变更与一键回滚入口。"""

    model_config = ConfigDict(extra="forbid")

    change_id: str
    assessment_id: str
    plan_id_before: str
    plan_id_after: str
    applied_at: datetime
    reverted: bool
    reverted_at: datetime | None
    revert_plan_id: str | None


class AutoAppliedChangeListOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    changes: list[AutoAppliedChangeOut]


class RevertResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    change_id: str
    revert_plan_id: str
    superseded_plan_id: str
    status: str


def _to_out(row: orm.AutoAppliedChange) -> AutoAppliedChangeOut:
    return AutoAppliedChangeOut(
        change_id=row.change_id,
        assessment_id=row.assessment_id,
        plan_id_before=row.plan_id_before,
        plan_id_after=row.plan_id_after,
        applied_at=row.applied_at,
        reverted=row.reverted,
        reverted_at=row.reverted_at,
        revert_plan_id=row.revert_plan_id,
    )


@router.get(
    "/changes",
    response_model=AutoAppliedChangeListOut,
    summary="列出 L4 自动应用记录（R13.9，供顶栏通知区）",
)
def list_changes(request: Request) -> AutoAppliedChangeListOut:
    """只读列出全部自动应用记录，最近的在前。"""
    factory: sessionmaker[Session] = request.app.state.session_factory
    with factory() as db:
        rows = list_auto_applied_changes(db)
        return AutoAppliedChangeListOut(changes=[_to_out(r) for r in rows])


@router.post(
    "/changes/{change_id}/revert",
    response_model=RevertResponse,
    summary="一键回滚一个 L4 自动应用变更（完整重校验，R13.10）",
)
def revert_endpoint(
    request: Request, change_id: str, session: PlannerSession
) -> RevertResponse | JSONResponse:
    """从 `snapshot_before` 重建计划并经 `activate_internal` 重新激活（写端点）。

    - 变更不存在 → 404。
    - 已回滚过 → 409（幂等保护）。
    - 重建计划重校验失败 → 422（回滚被拒，不产生违规计划，R13.10）。
    """
    factory: sessionmaker[Session] = request.app.state.session_factory
    with factory() as db:
        result = revert_change(
            db,
            change_id=change_id,
            now=operational_now(request.app.state.settings.app_env),
            events=request.app.state.event_bus,
        )
    if result.status is RevertStatus.NOT_FOUND:
        return error_response(
            status_code=404,
            code=ErrorCode.AUTO_APPLIED_CHANGE_NOT_FOUND,
            message=f"Auto-apply record {change_id} does not exist.",
            next_actions=[NextAction(action="list_changes", href="/autonomy/changes")],
            details={"change_id": change_id},
        )
    if result.status is RevertStatus.ALREADY_REVERTED:
        return error_response(
            status_code=409,
            code=ErrorCode.AUTO_APPLIED_CHANGE_ALREADY_REVERTED,
            message=f"Auto-apply record {change_id} has already been rolled back; no need to roll back again.",
            next_actions=[NextAction(action="list_changes", href="/autonomy/changes")],
            details={"change_id": change_id},
        )
    if result.status is RevertStatus.REVALIDATION_FAILED:
        return error_response(
            status_code=422,
            code=ErrorCode.REVALIDATION_FAILED,
            message="The plan rebuilt from the pre-change snapshot failed hard-constraint re-validation; the rollback was aborted (no violating plan is produced).",
            next_actions=[NextAction(action="list_changes", href="/autonomy/changes")],
            details={"change_id": change_id, "violation_count": len(result.violations)},
        )
    assert result.revert_plan_id is not None and result.superseded_plan_id is not None
    return RevertResponse(
        change_id=change_id,
        revert_plan_id=result.revert_plan_id,
        superseded_plan_id=result.superseded_plan_id,
        status="OK",
    )
