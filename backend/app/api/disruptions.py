"""扰动登记与影响查询 API（任务 7.4，R9、design.md §2.6 / §4.1、Error Handling §2）。

两个端点（design.md「API 端点」表的扰动行）：

- `POST /api/disruptions` —— 登记 5 类扰动之一，触发**确定性重排**（approach b：P0 主路径走
  确定性流水线，与 `DETERMINISTIC_ONLY` 降级同构，产出的 `ImpactAnalysis` 数值与 ReAct 路径
  逐字段相同）。90 秒内返回 `ImpactAnalysis` 与一个 `status = PENDING_APPROVAL` 的修订计划
  （R9.2）。无 `ACTIVE` 计划 → `NO_ACTIVE_PLAN`（R9.8）。若该生产日已有一个待审提案
  （`PENDING_APPROVAL`，例如任务 8.6 风险扫描为 CRITICAL 生成的 `RISK_MITIGATION` 提案）→
  `PENDING_PLAN_EXISTS`（409，R12.6）：单一待审提案是结构不变量，重排在登记与 `run_replan`
  之前先被这道领域守卫挡住，不自动取代既有提案（spec §4.1）。写端点，受 `Session_Auth` 保护。
- `GET /api/disruptions/{id}/impact` —— 回读该扰动的 `ImpactAnalysis`（R9.3）。

## 为什么分两个事务：先提交登记，再读快照重排

`load_snapshot` 要求**干净会话**（无未 flush 的改动），否则快照与 `input_snapshot_version`
不同源。扰动登记写 `disruptions` + `machine_downtime` / `worker_absences` / 物料副作用，这些
写入触发 `db/events.py` 的版本推进钩子。因此顺序是：**事务 1** 登记扰动并提交（版本推进一格）
→ **事务 2** 在干净会话上 `load_snapshot`（读到已反映扰动的新快照）→ `run_replan` 落库修订
计划。两个事务都在本端点内完成，对调用方是一次原子的「登记 + 重排」。

## 数值全部确定性（R9.4）

`ImpactAnalysis` 的每个数字由 `replan_deterministic.run_replan` 的确定性组件算出。本端点不
调用任何 LLM——它是 approach (b) 的 P0 主路径。ReAct 路径（`Planning_Agent` 驱动）复用同一
批确定性工具，因此其数值也来自同一处；LLM 只生成 `revision_summary` 叙述。

## 错误翻译

`NoActivePlanError` → `NO_ACTIVE_PLAN`（R9.8）；`DisruptionNotFoundError` →
`DISRUPTION_NOT_FOUND`。内核异常（`DataIntegrityError` / `InvalidRoutingError`）在重排读快照/
排产时可能抛出，翻译同 `POST /plans/generate`。
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Literal, cast

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.api.deps import PlannerSession
from app.api.errors import ErrorCode, NextAction, error_response
from app.core.snapshot import DataIntegrityError
from app.db import models as orm
from app.orchestrator.pipelines.replan_deterministic import (
    ImpactAnalysis,
    run_replan,
)
from app.seed.dataset import DEMO_ANCHOR
from app.services.auto_apply import AUTO_APPLIED_EXECUTION_PATH, auto_apply_if_l4
from app.services.feature_flags import read_feature_flags
from app.services.replanning import (
    DisruptionInput,
    DisruptionNotFoundError,
    NoActivePlanError,
    load_plan_candidate,
    locked_job_ids,
    register_disruption,
    require_any_active_plan,
    to_kernel_disruption,
)
from app.services.risk_triggers import trigger_scan
from app.services.snapshot_loader import load_snapshot

router = APIRouter(prefix="/disruptions", tags=["disruptions"])


# --------------------------------------------------------------------------
# 请求 / 响应契约（5 类扰动的判别联合）
# --------------------------------------------------------------------------


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class UrgentOrderBody(_Body):
    type: Literal["URGENT_ORDER"] = "URGENT_ORDER"
    product_id: str
    quantity: float = Field(gt=0)
    due_date: date


class MachineBreakdownBody(_Body):
    type: Literal["MACHINE_BREAKDOWN"] = "MACHINE_BREAKDOWN"
    machine_id: str
    start_time: datetime
    end_time: datetime


class MaterialShortageBody(_Body):
    type: Literal["MATERIAL_SHORTAGE"] = "MATERIAL_SHORTAGE"
    material_id: str
    available_quantity: float = Field(ge=0)


class WorkerUnavailableBody(_Body):
    type: Literal["WORKER_UNAVAILABLE"] = "WORKER_UNAVAILABLE"
    worker_id: str
    start_time: datetime
    end_time: datetime


class MaterialDelayBody(_Body):
    type: Literal["MATERIAL_DELAY"] = "MATERIAL_DELAY"
    material_id: str
    delivery_id: str
    new_eta: datetime


RegisterDisruptionBody = Annotated[
    UrgentOrderBody
    | MachineBreakdownBody
    | MaterialShortageBody
    | WorkerUnavailableBody
    | MaterialDelayBody,
    Field(discriminator="type"),
]


class RegisterDisruptionRequest(BaseModel):
    """`POST /api/disruptions` 的请求体。`reported_at` 缺省取演示锚点。"""

    model_config = ConfigDict(extra="forbid")

    disruption: RegisterDisruptionBody
    reported_at: datetime | None = None


class ImpactAnalysisOut(BaseModel):
    """`ImpactAnalysis` 的响应形态（R9.3）。全部数值由确定性组件计算（R9.4）。"""

    model_config = ConfigDict(extra="forbid")

    disruption_id: str
    candidate_plan_id: str
    affected_jobs: list[str]
    affected_orders: list[str]
    orders_at_risk_of_lateness: list[str]
    tardiness_delta_minutes: int
    churn_ratio: float
    impact_class: str
    autonomy_level: str
    #: 执行路径（R13.3/R13.4）：`PROPOSED`（L3 自主提案）或 `ESCALATED`（L5 上报人工）。
    #: P0 取值域 `{PROPOSED, ESCALATED}`——`AUTO_APPLIED`（L4）属 P1，运行期不出现。
    execution_path: str
    decisive_predicates: list[str]
    frozen_job_ids: list[str]
    substitute_unavailable_job_ids: list[str]


class RegisterDisruptionResponse(BaseModel):
    """`POST /api/disruptions` 的响应：登记结果 + 影响分析 + 修订计划句柄。"""

    model_config = ConfigDict(extra="forbid")

    disruption_id: str
    type: str
    registered_at: datetime
    revised_plan_id: str
    revised_plan_status: str
    #: 任务 13.4：本次重排是否被 L4 自动应用（IMPACT_MINOR 且开关开启）。
    auto_applied: bool = False
    #: 自动应用时的 `AutoAppliedChange.change_id`（供前端一键回滚入口）；否则 None。
    auto_applied_change_id: str | None = None
    impact: ImpactAnalysisOut


# --------------------------------------------------------------------------
# 端点
# --------------------------------------------------------------------------


@router.post(
    "",
    response_model=RegisterDisruptionResponse,
    summary="登记扰动并确定性重排（R9.1–9.4、R9.8）",
)
def post_disruption(
    request: Request, body: RegisterDisruptionRequest, session: PlannerSession
) -> RegisterDisruptionResponse | JSONResponse:
    """登记扰动 → 确定性重排 → 返回 `ImpactAnalysis` 与修订计划句柄。写端点。

    两段事务见模块 docstring。`session.subject` 进登记来源。`now = DEMO_ANCHOR` 是 P0 演示
    时钟（与 `POST /plans/generate` 同口径），使排产落在演示数据的时间坐标系里。
    """
    factory: sessionmaker[Session] = request.app.state.session_factory
    reported_at = body.reported_at or DEMO_ANCHOR
    payload = _to_disruption_input(body.disruption, reported_at)

    # ---- 事务 1：登记扰动（写 disruptions + 停机/缺勤窗 / 物料副作用，推进快照版本） ----
    db = factory()
    try:
        active_plan = require_any_active_plan(db)
        active_plan_id = active_plan.plan_id
        production_date = active_plan.production_date

        # 单一 `PENDING_APPROVAL`（R12.6，design.md §4.1）：重排会为本生产日落一个新的
        # `PENDING_APPROVAL` 修订计划，而 `ux_pending_per_day` 部分唯一索引规定同一生产日至多
        # 一个待审提案。若该日已有一个待审提案（例如任务 8.6 风险扫描为 CRITICAL 生成的
        # `RISK_MITIGATION` 提案），此处**在登记扰动、调用 run_replan 之前**先返回
        # `PENDING_PLAN_EXISTS`（409）并给「取消既有提案」入口——这是 spec 定义的冲突行为（人工
        # 裁决，非自动取代）。不登记扰动、不重排、不改动既有提案及其 mitigation_plan_id 链接。
        # 这是一道领域前置守卫（与 `Approval_Service.modify()` 遇同冲突时的返回同口径），
        # 不改 run_replan / _persist_revision 语义，不把 IntegrityError 当常规控制流。
        existing_pending = db.execute(
            select(orm.ProductionPlan.plan_id).where(
                orm.ProductionPlan.production_date == production_date,
                orm.ProductionPlan.status == "PENDING_APPROVAL",
            )
        ).scalars().first()
        if existing_pending is not None:
            db.rollback()
            return error_response(
                status_code=409,
                code=ErrorCode.PENDING_PLAN_EXISTS,
                message="该生产日已存在一个待审批计划，请先取消既有提案再登记扰动。",
                next_actions=[NextAction(action="cancel_pending", href="/plans/pending")],
                details={"pending_plan_id": existing_pending},
            )

        disruption_row = register_disruption(
            db,
            payload,
            active_plan_id=active_plan_id,
            source=f"session-{session.subject}",
            registered_at=reported_at,
        )
        disruption_id = disruption_row.disruption_id
        db.commit()
    except NoActivePlanError:
        db.rollback()
        return error_response(
            status_code=409,
            code=ErrorCode.NO_ACTIVE_PLAN,
            message="当前没有 ACTIVE 计划，无法登记扰动。请先生成并批准一个计划。",
            next_actions=[NextAction(action="generate_plan", href="/plans/generate")],
        )
    finally:
        db.close()

    # ---- 事务 2：干净会话上读扰动后快照并确定性重排 ----
    db = factory()
    try:
        snapshot = load_snapshot(db, now=DEMO_ANCHOR, production_date=production_date)
        active_candidate = load_plan_candidate(db, active_plan_id)
        kernel_disruption = to_kernel_disruption(payload)
        locked = locked_job_ids(db, active_plan_id)
        # 运行期特性开关（R13.8）从 `settings` 表读；缺行即 P0 默认（False）。这是把持久化
        # 开关注入确定性判定的接缝——`decide_autonomy` 仍是纯函数，flags 由此传入。
        flags = read_feature_flags(db)
        result = run_replan(
            db,
            disruption_id=disruption_id,
            active_plan_id=active_plan_id,
            active_plan=active_candidate,
            disruption=kernel_disruption,
            snapshot=snapshot,
            locked_job_ids=locked,
            now=DEMO_ANCHOR,
            session_id=f"session-{session.subject}",
            flags=flags,
        )
        # ---- 任务 13.4：L4 自动应用（仅 IMPACT_MINOR 且 auto_apply_minor_enabled=true）----
        # decide_autonomy 已保证只有 IMPACT_MINOR + 开关开启才返回 L4；auto_apply_if_l4 对非 L4
        # 直接不动作。自动应用经 activate_internal（完整重校验），失败则修订计划仍待人工审批。
        auto_result = auto_apply_if_l4(
            db,
            autonomy_level=result.impact.autonomy_level,
            revised_plan_id=result.plan.plan_id,
            active_plan_id=active_plan_id,
            assessment_id=result.assessment_id,
            now=DEMO_ANCHOR,
            events=request.app.state.event_bus,
        )
    except DataIntegrityError as error:
        db.rollback()
        return error_response(
            status_code=422,
            code=ErrorCode.DATA_INTEGRITY_ERROR,
            message="扰动后数据存在引用完整性错误，重排前已终止。",
            next_actions=[NextAction(action="fix_data", href="/")],
            details=error.details(),
        )
    finally:
        db.close()

    # 数据变更后触发一次风险扫描（R14.1 第 2 类触发器）：扰动登记改动了 Machine / Material /
    # Worker 状态。**在两个事务都提交之后**调用，独立会话、尽力而为——扫描失败不影响已完成的
    # 登记与重排（见 services.risk_triggers 的纪律）。
    trigger_scan(request.app.state.session_factory, trigger="DATA_CHANGE")

    # 若自动应用了，修订计划已 ACTIVE、execution_path=AUTO_APPLIED；否则仍 PENDING_APPROVAL。
    revised_status = "ACTIVE" if auto_result.applied else result.plan.status
    impact_out = _impact_out(result.impact)
    if auto_result.applied:
        impact_out = impact_out.model_copy(
            update={"execution_path": AUTO_APPLIED_EXECUTION_PATH}
        )
    return RegisterDisruptionResponse(
        disruption_id=disruption_id,
        type=payload.type,
        registered_at=reported_at,
        revised_plan_id=result.plan.plan_id,
        revised_plan_status=revised_status,
        auto_applied=auto_result.applied,
        auto_applied_change_id=auto_result.change_id,
        impact=impact_out,
    )


@router.get(
    "/{disruption_id}/impact",
    response_model=ImpactAnalysisOut,
    summary="回读扰动的影响分析（R9.3）",
)
def get_impact(request: Request, disruption_id: str) -> ImpactAnalysisOut | JSONResponse:
    """回读一次扰动的 `ImpactAnalysis`——从持久化的 `impact_assessments` + 修订计划重建。

    读端点（无需认证）。扰动不存在 → `DISRUPTION_NOT_FOUND`；已登记但尚无 assessment（例如
    重排失败）同样返回 not-found 语义的空态。
    """
    factory: sessionmaker[Session] = request.app.state.session_factory
    with factory() as db:
        try:
            impact = _load_impact(db, disruption_id)
        except DisruptionNotFoundError:
            return error_response(
                status_code=404,
                code=ErrorCode.DISRUPTION_NOT_FOUND,
                message=f"扰动 {disruption_id} 不存在或尚无影响分析。",
                next_actions=[NextAction(action="list_pending", href="/plans/pending")],
                details={"disruption_id": disruption_id},
            )
        return _impact_out(impact)


# --------------------------------------------------------------------------
# 辅助
# --------------------------------------------------------------------------


def _to_disruption_input(body: object, reported_at: datetime) -> DisruptionInput:
    """把请求体的判别联合摊平成服务层 `DisruptionInput`。"""
    if isinstance(body, MachineBreakdownBody):
        return DisruptionInput(
            type="MACHINE_BREAKDOWN",
            reported_at=reported_at,
            machine_id=body.machine_id,
            window_start=body.start_time,
            window_end=body.end_time,
        )
    if isinstance(body, WorkerUnavailableBody):
        return DisruptionInput(
            type="WORKER_UNAVAILABLE",
            reported_at=reported_at,
            worker_id=body.worker_id,
            window_start=body.start_time,
            window_end=body.end_time,
        )
    if isinstance(body, MaterialShortageBody):
        return DisruptionInput(
            type="MATERIAL_SHORTAGE",
            reported_at=reported_at,
            material_id=body.material_id,
            available_quantity=Decimal(str(body.available_quantity)),
        )
    if isinstance(body, MaterialDelayBody):
        return DisruptionInput(
            type="MATERIAL_DELAY",
            reported_at=reported_at,
            material_id=body.material_id,
            delivery_id=body.delivery_id,
            new_eta=body.new_eta,
        )
    urgent = cast(UrgentOrderBody, body)
    return DisruptionInput(
        type="URGENT_ORDER",
        reported_at=reported_at,
        product_id=urgent.product_id,
        quantity=Decimal(str(urgent.quantity)),
        due_date=urgent.due_date,
    )


def _impact_out(impact: ImpactAnalysis) -> ImpactAnalysisOut:
    return ImpactAnalysisOut(
        disruption_id=impact.disruption_id,
        candidate_plan_id=impact.candidate_plan_id,
        affected_jobs=list(impact.affected_jobs),
        affected_orders=list(impact.affected_orders),
        orders_at_risk_of_lateness=list(impact.orders_at_risk_of_lateness),
        tardiness_delta_minutes=impact.tardiness_delta_minutes,
        churn_ratio=impact.churn_ratio,
        impact_class=impact.impact_class,
        autonomy_level=impact.autonomy_level,
        execution_path=impact.execution_path,
        decisive_predicates=list(impact.decisive_predicates),
        frozen_job_ids=list(impact.frozen_job_ids),
        substitute_unavailable_job_ids=list(impact.substitute_unavailable_job_ids),
    )


def _load_impact(db: Session, disruption_id: str) -> ImpactAnalysis:
    """从 `disruptions` + `impact_assessments` 重建 `ImpactAnalysis`（回读端点用）。

    `affected_jobs` / `orders_at_risk_of_lateness` 等在登记时未逐一持久化到独立表——它们由
    重排流水线算出并存在 `impact_assessments` 的判据/输入里，回读时重算受影响集即可（确定性，
    同输入同结果）。为保持读端点轻量，这里取 `impact_assessments` 的已存字段，并从
    `disruptions.payload` + 当前快照重算 `affected_jobs`（与登记时一致）。
    """
    disruption_row = db.get(orm.Disruption, disruption_id)
    if disruption_row is None:
        raise DisruptionNotFoundError(disruption_id)
    assessment = db.execute(
        select(orm.ImpactAssessment).where(
            orm.ImpactAssessment.disruption_id == disruption_id
        )
    ).scalars().first()
    if assessment is None:
        raise DisruptionNotFoundError(disruption_id)

    # 重算受影响集（确定性）：读回 ACTIVE 计划 + 当前快照 + 映射扰动。
    from app.core.replanner import affected_by
    from app.tools.handlers.compute import _disruption_input_from_row

    payload = _disruption_input_from_row(disruption_row)
    kernel_disruption = to_kernel_disruption(payload)
    snapshot = load_snapshot(db, now=DEMO_ANCHOR)
    active_candidate = load_plan_candidate(db, assessment.baseline_plan_id)
    affected = tuple(sorted(affected_by(kernel_disruption, active_candidate, snapshot)))
    affected_orders = tuple(sorted({j.rsplit("-OP", 1)[0] for j in affected}))

    candidate = load_plan_candidate(db, assessment.candidate_plan_id)
    orders_at_risk = _orders_at_risk_readback(db, candidate)

    impact_input = _as_dict(assessment.impact_input)
    return ImpactAnalysis(
        disruption_id=disruption_id,
        candidate_plan_id=assessment.candidate_plan_id,
        affected_jobs=affected,
        affected_orders=affected_orders,
        orders_at_risk_of_lateness=orders_at_risk,
        tardiness_delta_minutes=int(impact_input.get("tardiness_delta_minutes", 0)),
        churn_ratio=float(impact_input.get("churn_ratio", 0.0)),
        impact_class=assessment.impact_class,
        autonomy_level=assessment.autonomy_level,
        execution_path=assessment.execution_path,
        decisive_predicates=tuple(_as_list(assessment.decisive_predicates)),
        frozen_job_ids=(),
        substitute_unavailable_job_ids=(),
    )


def _orders_at_risk_readback(db: Session, candidate: object) -> tuple[str, ...]:
    from app.core.scheduler import PlanCandidate

    if not isinstance(candidate, PlanCandidate):
        return ()
    snapshot = load_snapshot(db, now=DEMO_ANCHOR)
    orders_by_id = snapshot.orders_by_id()
    completions: dict[str, datetime] = {}
    for job in candidate.scheduled_jobs:
        cur = completions.get(job.order_id)
        if cur is None or job.end_time > cur:
            completions[job.order_id] = job.end_time
    at_risk = [
        oid
        for oid, comp in completions.items()
        if (o := orders_by_id.get(oid)) is not None and comp > o.due_date
    ]
    return tuple(sorted(at_risk))


def _as_dict(value: object) -> dict[str, object]:
    return dict(value) if isinstance(value, dict) else {}


def _as_list(value: object) -> list[str]:
    return [str(x) for x in value] if isinstance(value, list | tuple) else []
