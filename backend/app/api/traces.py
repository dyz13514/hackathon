"""可观测性端点（design.md Components §5「观测」分组，任务 5.12）。

三个**只读**端点，逐条对应 design.md §5 的观测行与 R24：

- `GET /traces` —— Trace 列表，可按时间（`started_after` / `started_before`）、Agent
  （`agent`）、触发类型（`trigger_source`）筛选（R24.2）。
- `GET /traces/{trace_id}` —— 单个 Trace 全文：头部元数据（含 `mode`）+ 逐步
  （`trace_steps`：`step_kind` / `decision_reason` / 耗时 / token）+ 每步关联的
  `tool_calls`（工具名 / 输入摘要 / 输出摘要 / 耗时 / token）（R24.1、R24.7、R22.11）。
- `GET /audit-log` —— append-only 审计日志的只读查询（R24.3：无写接口）。

## 为什么都是 `GET`、都不挂 `Session_Auth`

R24 是「可观测性与决策追踪」——Trace 查看器与审计日志在演示里要能直接翻看。
`SessionAuthMiddleware` 只拦 `POST/PUT/PATCH/DELETE`（`api/deps.py`），`GET` 天然放行，
与 `GET /plans/active`、`GET /state/dashboard` 同一口径。**审计日志尤其只能读**：写入的
唯一通路是 `app/db/audit.py::append()`（append-only，R24.3），本模块不提供任何写路径，
这本身就是 R24.3「不提供修改或删除既有条目的接口」在 REST 边界的体现。

## `decision_reason` 透传，不做二次解释（R24.7）

`trace_steps.decision_reason` 存的已是结构化摘要（工具名、护栏判定、阶段名），不是模型
原始推理链。本层**原样透传**——它不是渲染层，不该在这里编织叙述。前端逐步展示这些摘要，
连同工具名 / 输入输出摘要 / 耗时 / token，让规划员逐步看清「这次运行做了什么」。

## 筛选是 SQL 层的 WHERE，不是取全表再过滤

三个筛选条件都下推成 `select().where(...)`（`ix_traces_mode_started` 索引覆盖
`started_at`）。取全表再在 Python 里过滤会在演示数据增长后变慢，且把「筛选」这件事从一个
可被数据库优化的谓词降级成应用层循环——没有理由。
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.api.errors import ErrorCode, NextAction, error_response
from app.db import models as orm

router = APIRouter(tags=["observability"])

#: 列表端点单次返回的最大行数。演示规模下 Trace 不多，但仍设一个上限，避免「翻看列表」
#: 意外拉回全表——列表页只需要最近若干条，详情按 ID 单取。
_TRACE_LIST_LIMIT = 200
#: 审计日志列表的最大行数，同理。
_AUDIT_LIST_LIMIT = 500


# --------------------------------------------------------------------------
# 响应契约
# --------------------------------------------------------------------------


class TraceSummaryOut(BaseModel):
    """Trace 列表项（不含逐步明细）。`mode` 供列表页标注执行形态（R24.1）。"""

    model_config = ConfigDict(extra="forbid")

    trace_id: str
    kind: str
    mode: str
    agent: str | None
    trigger_source: str
    session_id: str
    started_at: datetime
    ended_at: datetime | None
    outcome: str | None
    step_count: int
    total_input_tokens: int
    total_output_tokens: int
    estimated_usd: float
    result_ref: str | None


class ToolCallOut(BaseModel):
    """一次工具调用（R22.11）。`result_summary` 是输出摘要，非完整结果。"""

    model_config = ConfigDict(extra="forbid")

    call_id: str
    step_id: str | None
    caller: str
    tool_name: str
    args_digest: str
    result_summary: str
    result_tokens: int
    truncated: bool
    outcome: str
    duration_ms: int


class TraceStepOut(BaseModel):
    """Trace 内的一步（R24.7）。`decision_reason` 是结构化摘要，非原始推理链。"""

    model_config = ConfigDict(extra="forbid")

    step_id: str
    step_index: int
    step_kind: str
    decision_reason: str | None
    duration_ms: int
    input_tokens: int
    output_tokens: int
    #: 挂在这一步上的工具调用（经 `tool_calls.step_id` 关联）。
    tool_calls: list[ToolCallOut]


class TraceDetailOut(TraceSummaryOut):
    """Trace 全文：头部元数据 + 逐步 + 未关联到某步的工具调用（R24.1）。"""

    steps: list[TraceStepOut]
    #: `step_id` 为空的工具调用（例如挂在 trace 而非某一步上）。放这里而不是丢弃，
    #: 使 `/traces/{id}` 能显示这次运行的**全部**工具调用，不漏。
    unassigned_tool_calls: list[ToolCallOut]


class AuditEntryOut(BaseModel):
    """一条审计记录（只读，R24.3）。`payload` 是开放 JSON，随事件类型而异。"""

    model_config = ConfigDict(extra="forbid")

    audit_id: str
    event_category: str
    event_type: str
    actor: str
    subject_type: str | None
    subject_id: str | None
    payload: dict[str, object]
    trace_id: str | None
    occurred_at: datetime


# --------------------------------------------------------------------------
# 端点
# --------------------------------------------------------------------------


@router.get(
    "/traces",
    response_model=list[TraceSummaryOut],
    summary="Trace 列表，可按时间 / Agent / 触发类型筛选（R24.2）",
)
def list_traces(
    request: Request,
    agent: Annotated[
        str | None, Query(description="按参与 Agent 筛选（REACT 路径）")
    ] = None,
    trigger_source: Annotated[
        str | None,
        Query(description="按触发类型：PLANNER_UI / SCHEDULED / DATA_CHANGE_EVENT / RISK_SCAN"),
    ] = None,
    started_after: Annotated[
        datetime | None, Query(description="只返回起始时刻 ≥ 此值的 Trace（含）")
    ] = None,
    started_before: Annotated[
        datetime | None, Query(description="只返回起始时刻 ≤ 此值的 Trace（含）")
    ] = None,
) -> list[TraceSummaryOut]:
    """Trace 列表。只读端点，无认证（与其余 GET 同口径）。

    四个筛选条件全部下推成 SQL 的 WHERE 谓词（`ix_traces_mode_started` 覆盖 `started_at`）。
    按 `started_at` 降序返回最近的一批（列表页只需近况，详情按 ID 单取），上限
    `_TRACE_LIST_LIMIT` 行。
    """
    factory: sessionmaker[Session] = request.app.state.session_factory
    stmt = select(orm.Trace)
    if agent is not None:
        stmt = stmt.where(orm.Trace.agent == agent)
    if trigger_source is not None:
        stmt = stmt.where(orm.Trace.trigger_source == trigger_source)
    if started_after is not None:
        stmt = stmt.where(orm.Trace.started_at >= started_after)
    if started_before is not None:
        stmt = stmt.where(orm.Trace.started_at <= started_before)
    stmt = stmt.order_by(orm.Trace.started_at.desc(), orm.Trace.trace_id).limit(
        _TRACE_LIST_LIMIT
    )

    with factory() as db:
        return [_summary_of(row) for row in db.execute(stmt).scalars()]


@router.get(
    "/traces/{trace_id}",
    response_model=TraceDetailOut,
    summary="单个 Trace 全文：逐步工具名 / 输入输出摘要 / 耗时 / token / decision_reason（R24.1）",
)
def get_trace(request: Request, trace_id: str) -> TraceDetailOut | JSONResponse:
    """按 ID 返回 Trace 全文。不存在返回 `TRACE_NOT_FOUND`。

    逐步（`trace_steps`）按 `step_index` 升序，每步挂上经 `tool_calls.step_id` 关联的工具
    调用；`step_id` 为空的工具调用归 `unassigned_tool_calls`，使详情不漏任何一次调用。
    """
    factory: sessionmaker[Session] = request.app.state.session_factory
    with factory() as db:
        trace = db.get(orm.Trace, trace_id)
        if trace is None:
            return error_response(
                status_code=404,
                code=ErrorCode.TRACE_NOT_FOUND,
                message=f"Trace {trace_id} does not exist.",
                next_actions=[NextAction(action="view_traces", href="/traces")],
                details={"trace_id": trace_id},
            )

        step_rows = list(
            db.execute(
                select(orm.TraceStep)
                .where(orm.TraceStep.trace_id == trace_id)
                .order_by(orm.TraceStep.step_index)
            ).scalars()
        )
        call_rows = list(
            db.execute(
                select(orm.ToolCall)
                .where(orm.ToolCall.trace_id == trace_id)
                .order_by(orm.ToolCall.call_id)
            ).scalars()
        )

    calls_by_step: dict[str, list[ToolCallOut]] = {}
    unassigned: list[ToolCallOut] = []
    for call in call_rows:
        out = _tool_call_of(call)
        if call.step_id is None:
            unassigned.append(out)
        else:
            calls_by_step.setdefault(call.step_id, []).append(out)

    steps = [
        TraceStepOut(
            step_id=step.step_id,
            step_index=step.step_index,
            step_kind=step.step_kind,
            decision_reason=step.decision_reason,
            duration_ms=step.duration_ms,
            input_tokens=step.input_tokens or 0,
            output_tokens=step.output_tokens or 0,
            tool_calls=calls_by_step.get(step.step_id, []),
        )
        for step in step_rows
    ]

    summary = _summary_of(trace)
    return TraceDetailOut(
        **summary.model_dump(),
        steps=steps,
        unassigned_tool_calls=unassigned,
    )


@router.get(
    "/audit-log",
    response_model=list[AuditEntryOut],
    summary="审计日志的只读查询（append-only，无写接口，R24.3）",
)
def list_audit_log(
    request: Request,
    event_category: Annotated[
        str | None, Query(description="按事件类别筛选（见 app.db.audit_events）")
    ] = None,
    subject_id: Annotated[
        str | None, Query(description="按主体 ID 筛选（如 plan_id）")
    ] = None,
    occurred_after: Annotated[
        datetime | None, Query(description="只返回发生时刻 ≥ 此值的记录（含）")
    ] = None,
    occurred_before: Annotated[
        datetime | None, Query(description="只返回发生时刻 ≤ 此值的记录（含）")
    ] = None,
) -> list[AuditEntryOut]:
    """审计日志。**只读**：本模块不提供任何写路径（写入唯一走 `app.db.audit.append`）。

    按类别 / 主体 / 时间窗筛选（`ix_audit_category_time` 覆盖类别与时间）。按 `occurred_at`
    降序返回最近的一批，上限 `_AUDIT_LIST_LIMIT` 行。
    """
    factory: sessionmaker[Session] = request.app.state.session_factory
    stmt = select(orm.AuditLog)
    if event_category is not None:
        stmt = stmt.where(orm.AuditLog.event_category == event_category)
    if subject_id is not None:
        stmt = stmt.where(orm.AuditLog.subject_id == subject_id)
    if occurred_after is not None:
        stmt = stmt.where(orm.AuditLog.occurred_at >= occurred_after)
    if occurred_before is not None:
        stmt = stmt.where(orm.AuditLog.occurred_at <= occurred_before)
    stmt = stmt.order_by(orm.AuditLog.occurred_at.desc(), orm.AuditLog.audit_id).limit(
        _AUDIT_LIST_LIMIT
    )

    with factory() as db:
        return [
            AuditEntryOut(
                audit_id=row.audit_id,
                event_category=row.event_category,
                event_type=row.event_type,
                actor=row.actor,
                subject_type=row.subject_type,
                subject_id=row.subject_id,
                payload=_as_dict(row.payload),
                trace_id=row.trace_id,
                occurred_at=row.occurred_at,
            )
            for row in db.execute(stmt).scalars()
        ]


# --------------------------------------------------------------------------
# 序列化辅助
# --------------------------------------------------------------------------


def _summary_of(row: orm.Trace) -> TraceSummaryOut:
    return TraceSummaryOut(
        trace_id=row.trace_id,
        kind=row.kind,
        mode=row.mode,
        agent=row.agent,
        trigger_source=row.trigger_source,
        session_id=row.session_id,
        started_at=row.started_at,
        ended_at=row.ended_at,
        outcome=row.outcome,
        step_count=row.step_count,
        total_input_tokens=row.total_input_tokens,
        total_output_tokens=row.total_output_tokens,
        estimated_usd=float(row.estimated_usd),
        result_ref=row.result_ref,
    )


def _tool_call_of(row: orm.ToolCall) -> ToolCallOut:
    return ToolCallOut(
        call_id=row.call_id,
        step_id=row.step_id,
        caller=row.caller,
        tool_name=row.tool_name,
        args_digest=row.args_digest,
        result_summary=row.result_summary,
        result_tokens=row.result_tokens,
        truncated=row.truncated,
        outcome=row.outcome,
        duration_ms=row.duration_ms,
    )


def _as_dict(value: object) -> dict[str, object]:
    return {str(k): v for k, v in value.items()} if isinstance(value, dict) else {}
