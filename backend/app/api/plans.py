"""计划端点（design.md Components §5「计划」分组，任务 2.12）。

四个端点：

- `POST /plans/generate` —— 形态 A 确定性流水线（R5.1）。60 秒内返回一个
  `status = PENDING_APPROVAL` 的计划，含 R5.5 逐字列出的六项：`feasibility`、
  `scheduled_jobs`、`unschedulable_jobs`、`objective_breakdown`、`baseline_comparison`、
  `generated_by_trace_id`。写端点，受 `Session_Auth` 保护（R23.12）。
- `GET /plans/{plan_id}` —— 计划全文含 `scheduled_jobs` 明细，供 UI 甘特图（R5.5）。
- `GET /plans/active` / `GET /plans/pending` —— 当前生效 / 待审批计划（R1.1）。

## 为什么生成端点与读端点的 `scheduled_jobs` 形状相同

生成端点直接把内核算出的 `PlanGenerationResult` 序列化，读端点从库里重建同一个形状。
两者共用 `PlanDetailOut`，因此「生成后立刻看到的」与「刷新页面后看到的」逐字段一致——
它们本来就该是同一份计划。

## 错误翻译

内核抛领域异常（`DataIntegrityError` / `InvalidRoutingError`），本层把它们翻译成
design.md Error Handling §2 的统一错误包。两者都在**写入之前**抛出（`load_snapshot` 的
预检、`generate_schedule` 的路线校验），因此失败路径不留半成品行——这正是任务 2.12
「五张表在同一事务内完成」想要的另一半保证。
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.api.deps import PlannerSession
from app.api.errors import ErrorCode, NextAction, error_response
from app.core.explain import NoTradeoff
from app.core.scheduler import InvalidRoutingError
from app.core.snapshot import DataIntegrityError
from app.db import audit
from app.db import models as orm
from app.db.repositories import current_input_snapshot_version
from app.llm.adapter import BedrockAdapter
from app.orchestrator.pipelines.plan_generation import (
    PlanGenerationResult,
    run_plan_generation,
)
from app.services.approval import ApprovalService
from app.services.events import EventBus
from app.services.explanation import (
    BaselineView,
    ComponentView,
    ExplanationInputs,
    ExplanationResult,
    ScheduledJobView,
    UnschedulableView,
    assemble_initial_plan_explanation,
    build_explanation,
)
from app.services.exporter import PlanNotFoundError, export_csv, export_xlsx
from app.services.runtime_clock import operational_now
from app.services.snapshot_loader import resolve_production_date

router = APIRouter(prefix="/plans", tags=["plans"])

# --------------------------------------------------------------------------
# 请求 / 响应契约
# --------------------------------------------------------------------------


class GeneratePlanIn(BaseModel):
    """`POST /plans/generate` 的请求体。**没有 `status` 字段**——状态由流水线硬编码。"""

    model_config = ConfigDict(extra="forbid")

    production_date: date | None = Field(
        default=None,
        description="目标生产日。缺省取当前运行时日期；DEMO/TEST 使用固定演示锚点。",
    )


class PlanUpdateIn(BaseModel):
    """`PATCH /plans/{id}` 的请求体（审批绕过防护，R11.8 / R22.9 / R23.4）。

    **刻意没有 `status` 字段。** 计划状态不是一个可经 REST 直接写的属性——它只能经
    `Approval_Service` 沿 design.md §8 的状态机迁移（`PENDING_APPROVAL → ACTIVE` 仅由
    `approve()` 触发）。`extra="forbid"` 让任何多余字段（不止 `status`）都被拒。

    P0 阶段本模型**没有任何可写字段**：计划的一切变更（审批、拒绝、修改）都有专属的
    动作端点（`/approve`、`/reject`、`/modify`），没有「就地改一个计划字段」的用例。因此
    这个模型此刻是空的——它存在的意义是给 `PATCH` 一个「没有 `status`」的契约锚点，而不是
    提供一条修改通路。将来若出现合法的就地可写字段（例如给计划打备注），在这里显式添加，
    而 `status` 永远不在其中。
    """

    model_config = ConfigDict(extra="forbid")


class ScheduledJobOut(BaseModel):
    """一条已排产作业（供甘特图渲染，R5.5）。

    `operation_sequence` 与 `product_id` 来自 `production_jobs`，其余来自 `scheduled_jobs`。
    `changeover_minutes` 单列出来，让前端把换型段画成斜纹（design.md §6 `/schedule` 行）。
    """

    model_config = ConfigDict(extra="forbid")

    job_id: str
    order_id: str
    product_id: str
    operation_sequence: int
    machine_id: str
    worker_id: str
    start_time: datetime
    end_time: datetime
    setup_minutes: int
    changeover_minutes: int


class UnschedulableJobOut(BaseModel):
    """一条不可排产作业及其量化解封条件（R8.2、R8.3、R8.6）。"""

    model_config = ConfigDict(extra="forbid")

    job_id: str
    order_id: str
    blocking_reason: str
    unblock_suggestion: dict[str, Any]


class ComponentScoreOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    raw_value: float
    weight: float
    weighted_contribution: float


class PreferenceContributionOut(BaseModel):
    """一条偏好规则对成型计划的惩罚贡献（R18.7、design.md §4.3）。

    UI 据此标注「JOB-012 的排产受 PR-003 影响」：`violating_job_ids` 是被该规则命中的作业
    （至多 10 条），`raw_value` = 命中数 × weight_delta，`weighted_contribution` = raw × PREF_UNIT。
    """

    model_config = ConfigDict(extra="forbid")

    rule_id: str
    human_text: str
    kind: str = ""
    violating_job_ids: list[str] = Field(default_factory=list)
    raw_value: float = 0.0
    weighted_contribution: float = 0.0


class ObjectiveBreakdownOut(BaseModel):
    """目标评分拆解（R7.2、R7.3）。UI 逐条展示 7 个分量的原始值、权重、加权贡献。"""

    model_config = ConfigDict(extra="forbid")

    components: list[ComponentScoreOut]
    total_score: float
    #: 逐 `rule_id` 的偏好惩罚归因（R18.7）。无启用偏好或无命中时为空。
    preference_contributions: list[PreferenceContributionOut] = Field(default_factory=list)
    #: 生效的 `ADJUST_OBJECTIVE_WEIGHT` 覆盖（design.md §4.3），每条为一个 dict。
    weight_overrides_applied: list[dict[str, Any]] = Field(default_factory=list)


class BaselineComparisonOut(BaseModel):
    """与 FCFS 基线的同口径 KPI 对比（R5.4、R19.2）。UI 的基线对比区据此显示按期率与拖期。"""

    model_config = ConfigDict(extra="forbid")

    baseline_plan_id: str
    snapshot_version: int
    on_time_rate: float
    baseline_on_time_rate: float
    total_tardiness_minutes: int
    baseline_total_tardiness_minutes: int
    late_order_count: int
    baseline_late_order_count: int


class PlanDetailOut(BaseModel):
    """计划全文（R5.5 的六项 + 计划头元数据）。生成端点与读端点共用它。"""

    model_config = ConfigDict(extra="forbid")

    plan_id: str
    production_date: date
    status: str
    plan_version: int
    origin: str
    input_snapshot_version: int
    generated_by_trace_id: str | None
    feasibility: str
    scheduled_jobs: list[ScheduledJobOut]
    unschedulable_jobs: list[UnschedulableJobOut]
    objective_breakdown: ObjectiveBreakdownOut | None
    baseline_comparison: BaselineComparisonOut | None


class DecisionEvidenceOut(BaseModel):
    """一条决策证据（R10.2）。P0 初始计划无 delta，此列表恒为空；供任务 8.4 重排路径填充。"""

    model_config = ConfigDict(extra="forbid")

    job_id: str
    trigger: str
    constraint: str
    resources: list[str]


class CounterfactualOut(BaseModel):
    """反事实（**恰好 1 项**，R10.3）。`kind` 区分 `TRADEOFF` / `NO_TRADEOFF`。

    P0 初始计划恒为 `NO_TRADEOFF`（非重排，无可比取舍）；`TRADEOFF` 的分量与 Z 值字段由
    任务 8.4 在重排路径填充，在那之前为 `None`。
    """

    model_config = ConfigDict(extra="forbid")

    kind: str
    reason: str | None = None
    pivotal_job_id: str | None = None
    component: str | None = None
    current_value: float | None = None
    counterfactual_value: float | None = None
    selection_basis: str | None = None


class AssumptionOut(BaseModel):
    """一条可能过期的输入假设（R10.4）。"""

    model_config = ConfigDict(extra="forbid")

    kind: str
    description: str
    stale_risk: str


class ConfidenceOut(BaseModel):
    """置信度及其判定依据（R10.5）。"""

    model_config = ConfigDict(extra="forbid")

    level: str
    basis: str


class ExplanationOut(BaseModel):
    """`GET /plans/{id}/explanation` 的响应：结构化解释 + 叙述文本 + numeric_check（R10）。

    `narrative` 是最终发布的文本（通过数值比对的 LLM 文本，或回退的模板文本）；`numeric_check`
    是 `PASS` / `FALLBACK` 徽章（design.md §6）。结构化字段（`decision_evidence`、
    `counterfactual`、`assumptions`、`confidence`）是确定性事实骨架，不含模型推理链（R10.6）。
    """

    model_config = ConfigDict(extra="forbid")

    plan_id: str
    llm_mode: str
    narrative: str
    numeric_check: str
    fallback_reason: str | None
    decision_evidence: list[DecisionEvidenceOut]
    counterfactual: CounterfactualOut
    assumptions: list[AssumptionOut]
    confidence: ConfidenceOut


class PlanSummaryOut(BaseModel):
    """计划列表项（`/plans/active` / `/plans/pending`）。不含明细行。"""

    model_config = ConfigDict(extra="forbid")

    plan_id: str
    production_date: date
    status: str
    plan_version: int
    origin: str
    feasibility: str
    input_snapshot_version: int
    generated_by_trace_id: str | None


class JobChangeOut(BaseModel):
    """一条逐作业变更（R10.1）。`change` ∈ ADDED/REMOVED/MOVED/REASSIGNED/UNCHANGED。

    `a_*` 是计划 A（对照，通常是当前 ACTIVE）里该作业的资源与时间；`b_*` 是计划 B（建议）里的。
    `ADDED` 只有 `b_*`（A 中不存在），`REMOVED` 只有 `a_*`（B 中不存在），其余两者都有。
    供 UI 的并排甘特逐作业标注（design.md §6 `/plans/:a/compare/:b`）。
    """

    model_config = ConfigDict(extra="forbid")

    job_id: str
    order_id: str
    change: str
    a_machine_id: str | None = None
    a_worker_id: str | None = None
    a_start_time: datetime | None = None
    a_end_time: datetime | None = None
    b_machine_id: str | None = None
    b_worker_id: str | None = None
    b_start_time: datetime | None = None
    b_end_time: datetime | None = None


class PlanCompareOut(BaseModel):
    """`GET /plans/{a}/compare/{b}` 的响应（R10.1、R10.2）。

    面向 UI 的**明细端点**——与句柄式 `compare_plans` 工具（只给聚合计数）区分：这里逐作业
    列出变更（`changes`）与每个 `MOVED` / `REASSIGNED` 作业的 `decision_evidence`（R10.2）。
    `churn_ratio` 与五个计数是同口径的聚合，便于 UI 顶栏展示。反事实（R10.3）属任务 8.4，
    本端点不含。
    """

    model_config = ConfigDict(extra="forbid")

    plan_id_a: str
    plan_id_b: str
    churn_ratio: float
    added_count: int
    removed_count: int
    moved_count: int
    reassigned_count: int
    unchanged_count: int
    changes: list[JobChangeOut]
    decision_evidence: list[DecisionEvidenceOut]


# --------------------------------------------------------------------------
# 端点
# --------------------------------------------------------------------------


@router.post(
    "/generate",
    response_model=PlanDetailOut,
    summary="形态 A 确定性流水线：生成一个 PENDING_APPROVAL 计划（R5.1）",
)
def generate_plan(
    request: Request, body: GeneratePlanIn, session: PlannerSession
) -> PlanDetailOut | JSONResponse:
    """执行固定 6 步流水线并返回计划全文。写端点，受 `Session_Auth` 保护。

    `session` 参数不只是装饰：`session.subject` 进审计记录的 `actor`。内核异常在此翻译成
    统一错误包，两者都在写入前抛出，因此不留半成品。
    """
    factory: sessionmaker[Session] = request.app.state.session_factory
    db = factory()

    # 目标生产日只解析一次，前置检查与流水线共用这一个值。请求缺省 `production_date` 时它是
    # `None`，**不能**直接拿去查库：`column == None` 会渲染成 `IS NULL`，前置检查因此恒不命中
    # 真实计划——这正是「重复生成」曾经绕过守卫、直接进入流水线并撞上清理阶段外键 /
    # `ux_pending_per_day` 的成因。解析规则归 `resolve_production_date`（与 `load_snapshot`
    # 同一处定义），两处口径不会分叉。
    now = operational_now(request.app.state.settings.app_env)
    production_date = resolve_production_date(body.production_date, now=now)

    if request.app.state.settings.app_env == "LOCAL":
        counts = {
            "orders": db.scalar(
                select(func.count()).select_from(orm.Order).where(
                    orm.Order.record_status == "ACTIVE"
                )
            ),
            "product routings": db.scalar(
                select(func.count()).select_from(orm.Operation).join(
                    orm.Product, orm.Operation.product_id == orm.Product.product_id
                ).where(orm.Product.record_status == "ACTIVE")
            ),
            "machines": db.scalar(
                select(func.count()).select_from(orm.Machine).where(
                    orm.Machine.record_status == "ACTIVE"
                )
            ),
            "workers": db.scalar(
                select(func.count()).select_from(orm.Worker).where(
                    orm.Worker.record_status == "ACTIVE"
                )
            ),
        }
        missing = [name for name, count in counts.items() if not count]
        if missing:
            db.close()
            return error_response(
                status_code=422,
                code=ErrorCode.SCHEDULING_INPUTS_MISSING,
                message=(
                    "Cannot generate a schedule yet. Import real orders, product routings, "
                    "machines and worker shifts/skills first. Missing: " + ", ".join(missing) + "."
                ),
                next_actions=[NextAction(action="import_data", href="/import")],
                details={"missing": missing},
            )

    # 前置检查：同一 production_date 已存在 PENDING_APPROVAL 计划时通常拒绝重复生成
    # （ux_pending_per_day 部分唯一索引的前端保护，R11.8 / R12.6）。若它已经陈旧，则
    # 这次“基于最新数据重新生成”本身会通过 ApprovalService 的 CANCEL 路径让旧计划让位；
    # 否则审批页的 regenerate 链接会陷入“不能审批也不能生成”的死锁。
    try:
        existing_pending = db.scalars(
            select(orm.ProductionPlan).where(
                orm.ProductionPlan.production_date == production_date,
                orm.ProductionPlan.status == "PENDING_APPROVAL",
            )
        ).first()
        if existing_pending is not None:
            current_version = current_input_snapshot_version(db)
            if existing_pending.input_snapshot_version != current_version:
                events: EventBus = request.app.state.event_bus
                cancelled = ApprovalService(
                    session=db, now=now, events=events
                ).cancel_stale_for_regeneration(
                    existing_pending.plan_id, actor=session.subject.upper()
                )
                if not cancelled:
                    db.close()
                    return error_response(
                        status_code=409,
                        code=ErrorCode.PENDING_PLAN_EXISTS,
                        message="The pending plan changed while regeneration was starting. Please refresh and try again.",
                        next_actions=[NextAction(action="view_pending", href="/plans/pending")],
                    )
            else:
                db.close()
                return error_response(
                    status_code=409,
                    code=ErrorCode.PENDING_PLAN_EXISTS,
                    message=(
                        f"Production date {production_date} already has a pending-approval plan "
                        f"({existing_pending.plan_id}). Please approve or reject the existing plan "
                        "before generating a new one."
                    ),
                    next_actions=[
                        NextAction(action="view_pending", href="/plans/pending"),
                        NextAction(action="approve", href=f"/plans/{existing_pending.plan_id}/approve"),
                    ],
                    details={"existing_plan_id": existing_pending.plan_id},
                )
    except Exception:
        db.close()
        raise

    try:
        result = run_plan_generation(
            db,
            production_date=production_date,
            now=now,
            actor=session.subject.upper(),
            session_id=f"session-{session.subject}",
        )
    except DataIntegrityError as error:
        db.rollback()
        return error_response(
            status_code=422,
            code=ErrorCode.DATA_INTEGRITY_ERROR,
            message="The input data has referential-integrity errors; aborted before scheduling. Please fix the references below first.",
            next_actions=[NextAction(action="fix_data", href="/")],
            details=error.details(),
        )
    except InvalidRoutingError as error:
        db.rollback()
        return error_response(
            status_code=422,
            code=ErrorCode.INVALID_ROUTING,
            message=f"Invalid operation routing for product {error.product_id}: {error.detail}",
            next_actions=[NextAction(action="fix_routing", href="/")],
            details={"product_id": error.product_id, "detail": error.detail},
        )
    finally:
        db.close()

    return _detail_from_result(result)


@router.get("/active", response_model=list[PlanSummaryOut], summary="当前 ACTIVE 计划（R1.1）")
def list_active(request: Request) -> list[PlanSummaryOut]:
    """当前生效的计划。任一生产日最多一个 `ACTIVE`（部分唯一索引保证），此处按生产日返回全部。"""
    return _list_by_status(request, status="ACTIVE")


@router.get(
    "/pending", response_model=list[PlanSummaryOut], summary="待审批计划（R1.1、R12.6）"
)
def list_pending(request: Request) -> list[PlanSummaryOut]:
    """待审批的计划。任一生产日最多一个 `PENDING_APPROVAL`（部分唯一索引保证）。"""
    return _list_by_status(request, status="PENDING_APPROVAL")


@router.get(
    "/{plan_id}",
    response_model=PlanDetailOut,
    summary="计划全文，含 scheduled_jobs 明细（供 UI，R5.5）",
)
def get_plan(request: Request, plan_id: str) -> PlanDetailOut | JSONResponse:
    """按 ID 返回计划全文。不存在返回 `PLAN_NOT_FOUND`。"""
    factory: sessionmaker[Session] = request.app.state.session_factory
    with factory() as db:
        plan = db.get(orm.ProductionPlan, plan_id)
        if plan is None:
            return error_response(
                status_code=404,
                code=ErrorCode.PLAN_NOT_FOUND,
                message=f"Plan {plan_id} does not exist.",
                next_actions=[NextAction(action="view_pending", href="/plans/pending")],
            )
        return _detail_from_db(db, plan)


@router.get(
    "/{plan_id}/explanation",
    response_model=ExplanationOut,
    summary="计划的结构化解释与叙述；LIVE 失败显式报错",
)
def get_plan_explanation(
    request: Request, plan_id: str
) -> ExplanationOut | JSONResponse:
    """返回一份计划的解释（design.md Architecture §2.1、Components §3.7、R10）。

    读端点，但它触发那**唯一一次**解释 LLM 调用（design.md §2.1）：从持久化的计划重建
    紧凑载荷（按机器聚合、7 分量、基线、不可排产摘要、假设、关键作业——**不含原始实体
    清单**，R21.12），经 `Guardrail_Layer` 发起 1 次 `Bedrock_Adapter.invoke`，数值比对通过
    则发布 LLM 文本；LIVE 调用失败或数值校验未通过时返回 503，不发布模板。离线模式仍保留
    明确标记的确定性模板供测试使用。

    同一份计划的载荷字节稳定，因此第二次请求命中内容哈希缓存、零 token（R25.7）——解释因此
    对同一计划是稳定的。计划不存在返回 `PLAN_NOT_FOUND`。
    """
    factory: sessionmaker[Session] = request.app.state.session_factory
    adapter: BedrockAdapter = request.app.state.llm_adapter
    with factory() as db:
        plan = db.get(orm.ProductionPlan, plan_id)
        if plan is None:
            return error_response(
                status_code=404,
                code=ErrorCode.PLAN_NOT_FOUND,
                message=f"Plan {plan_id} does not exist.",
                next_actions=[NextAction(action="view_pending", href="/plans/pending")],
            )
        inputs = _explanation_inputs_from_db(db, plan)

    from app.services.explanation import ExplanationGenerationFailed

    try:
        result = build_explanation(
            inputs.explanation,
            inputs.payload,
            adapter,
            trace_id=plan.generated_by_trace_id,
        )
    except ExplanationGenerationFailed:
        return error_response(
            status_code=503,
            code=ErrorCode.LLM_GENERATION_FAILED,
            message=(
                "LIVE plan explanation failed or did not pass numeric validation; "
                "no template was substituted."
            ),
            next_actions=[
                NextAction(action="retry_explanation", href=f"/plans/{plan_id}/explanation")
            ],
        )
    return _explanation_out(result, llm_mode=adapter.configured_mode.value)


@router.get(
    "/{plan_id_a}/compare/{plan_id_b}",
    response_model=PlanCompareOut,
    summary="两计划的逐作业对比 + 决策证据（R10.1、R10.2）",
)
def compare_plans_detail(
    request: Request, plan_id_a: str, plan_id_b: str
) -> PlanCompareOut | JSONResponse:
    """并排对比两份计划，逐作业标注 `ADDED`/`REMOVED`/`MOVED`/`REASSIGNED`/`UNCHANGED`（R10.1），
    并为每个 `MOVED` / `REASSIGNED` 作业给出 `decision_evidence`（R10.2）。

    这是**面向 UI 的明细端点**（design.md §6 `/plans/:a/compare/:b`），与句柄式 `compare_plans`
    工具区分：工具只返回聚合计数（喂给 Agent 上下文，ADR-004），这里返回逐作业明细供并排甘特
    渲染。全部由确定性组件计算：`compute_plan_delta`（任务 7.2）分类，`build_decision_evidence`
    （任务 7.5，`core/explain.py`）产出证据。反事实（R10.3）属任务 8.4，本端点不含。

    任一计划不存在 → `PLAN_NOT_FOUND`。读端点，不触发任何 LLM。
    """
    from app.core.delta import compute_plan_delta
    from app.core.explain import build_decision_evidence
    from app.services.replanning import load_plan_candidate

    factory: sessionmaker[Session] = request.app.state.session_factory
    with factory() as db:
        plan_a = db.get(orm.ProductionPlan, plan_id_a)
        plan_b = db.get(orm.ProductionPlan, plan_id_b)
        missing = plan_id_a if plan_a is None else (plan_id_b if plan_b is None else None)
        if missing is not None:
            return error_response(
                status_code=404,
                code=ErrorCode.PLAN_NOT_FOUND,
                message=f"Plan {missing} does not exist.",
                next_actions=[NextAction(action="view_pending", href="/plans/pending")],
                details={"plan_id": missing},
            )
        candidate_a = load_plan_candidate(db, plan_id_a)
        candidate_b = load_plan_candidate(db, plan_id_b)

    delta = compute_plan_delta(candidate_a, candidate_b)
    evidence = build_decision_evidence(delta, candidate_a, candidate_b)
    changes = _job_changes(delta, candidate_a, candidate_b)

    return PlanCompareOut(
        plan_id_a=plan_id_a,
        plan_id_b=plan_id_b,
        churn_ratio=delta.churn_ratio,
        added_count=len(delta.added),
        removed_count=len(delta.removed),
        moved_count=len(delta.moved),
        reassigned_count=len(delta.reassigned),
        unchanged_count=len(delta.unchanged),
        changes=changes,
        decision_evidence=[
            DecisionEvidenceOut(
                job_id=ev.job_id,
                trigger=ev.trigger,
                constraint=ev.constraint,
                resources=list(ev.resources),
            )
            for ev in evidence
        ],
    )


#: 导出格式 → (media type, 文件扩展名)。`.xlsx` 用 OOXML 的官方 MIME 类型，`.csv` 明确
#: 带 `charset=utf-8`，使浏览器不按本地编码猜测（会把中文列名与展开文本弄乱）。
_EXPORT_MEDIA_TYPES: dict[str, tuple[str, str]] = {
    "xlsx": (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "xlsx",
    ),
    "csv": ("text/csv; charset=utf-8", "csv"),
}


@router.post(
    "/{plan_id}/export",
    summary="导出计划为车间格式（xlsx | csv，R20）",
    # 返回二进制 `Response` 或错误 `JSONResponse`，两者都不是 Pydantic 模型——显式关掉
    # 响应模型推断，否则 FastAPI 会尝试把 `Response | JSONResponse` 当成响应字段类型而报错。
    response_model=None,
    responses={
        200: {
            "content": {
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": {},
                "text/csv": {},
            },
            "description": "计划的二进制导出文件。",
        }
    },
)
def export_plan(
    request: Request,
    plan_id: str,
    session: PlannerSession,
    export_format: str = Query(
        default="xlsx", alias="format", description="导出格式：xlsx 或 csv"
    ),
) -> Response | JSONResponse:
    """导出一个计划为 `.xlsx` 或 `.csv`（`Plan_Exporter`，R20.1–6）。

    是一个**写触发式读端点**：它不改任何业务状态，但按 design.md §5 的约定与其余 `POST`
    一样受 `Session_Auth` 保护（`session: PlannerSession`），因为「谁导出了车间表格」是一次
    应被认证的操作，且导出内容里含 `approved_by` 等审批元数据。

    `format` 是查询参数（`?format=xlsx|csv`）。未知格式返回 422 而非默认回退——「format 拼错了
    却拿到一个不是自己想要的文件」比一个明确的错误更难排查。计划不存在返回 `PLAN_NOT_FOUND`。
    """
    normalized = export_format.lower()
    if normalized not in _EXPORT_MEDIA_TYPES:
        return error_response(
            status_code=422,
            code=ErrorCode.EXPORT_FORMAT_UNSUPPORTED,
            message="Only xlsx and csv export formats are supported.",
            next_actions=[
                NextAction(action="export_xlsx", href=f"/plans/{plan_id}/export?format=xlsx"),
                NextAction(action="export_csv", href=f"/plans/{plan_id}/export?format=csv"),
            ],
            details={"requested_format": export_format},
        )

    media_type, extension = _EXPORT_MEDIA_TYPES[normalized]
    factory: sessionmaker[Session] = request.app.state.session_factory
    with factory() as db:
        try:
            content = (
                export_xlsx(db, plan_id)
                if normalized == "xlsx"
                else export_csv(db, plan_id)
            )
        except PlanNotFoundError:
            return error_response(
                status_code=404,
                code=ErrorCode.PLAN_NOT_FOUND,
                message=f"Plan {plan_id} does not exist.",
                next_actions=[NextAction(action="view_pending", href="/plans/pending")],
                details={"plan_id": plan_id},
            )

    return Response(
        content=content,
        media_type=media_type,
        headers={
            "Content-Disposition": (
                f'attachment; filename="{plan_id}.{extension}"'
            )
        },
    )


@router.patch(
    "/{plan_id}",
    summary="计划就地更新——检测到 status 键即 403（审批绕过防护，R11.8 / R22.9 / R23.4）",
)
async def update_plan(
    request: Request, plan_id: str, session: PlannerSession
) -> JSONResponse:
    """`PATCH /plans/{id}`：审批绕过防护的显式路由（design.md §4.1、§8）。

    这是 R11.8「计划状态不可被绕过审批地修改」在 REST 边界的第二道防护（第一道是
    `PlanUpdateIn` 里根本没有 `status` 字段）。请求体里**只要出现 `status` 键**，无论值是
    什么，都返回 `403 FORBIDDEN` 并写审计——不是 422（那会把它当成「字段格式错」），而是
    403（「这个操作本身不被允许」）。计划状态只能经 `Approval_Service` 沿 §8 状态机迁移。

    为什么要读原始请求体而不是直接让 Pydantic 校验：`PlanUpdateIn` 的 `extra="forbid"` 会
    把 `status` 连同任何多余字段一起判成 422。但 `status` 是一个**特殊**的越界——它是「试图
    绕过审批闸门」这一具体攻击面（EVAL-207），需要一条自己的、可审计的 403 路径，而不是
    淹没在通用的 422 里。因此先手动查 `status` 键，命中即 403 + 审计；未命中再走
    `PlanUpdateIn` 的常规校验（P0 该模型无可写字段，因此任何非空体都会 422）。
    """
    try:
        raw = await request.json()
    except Exception:
        raw = None

    if isinstance(raw, dict) and "status" in raw:
        # 审计：留痕一次「试图经 REST 直接改计划状态」的尝试（走独立连接，见 db/audit.py）。
        # attempted_status 只记键的存在与尝试值，供事后排查；不据它做任何状态变更。
        audit.append(
            event_category="APPROVAL_ACTION",
            event_type="PLAN_STATUS_WRITE_FORBIDDEN",
            actor=session.subject.upper(),
            payload={
                "plan_id": plan_id,
                "attempted_status": str(raw.get("status")),
                "reason": "REST 层不接受对 production_plans.status 的直接写入（R11.8）",
            },
            subject_type="ProductionPlan",
            subject_id=plan_id,
            occurred_at=datetime.now(),  # noqa: DTZ005  # 审计时刻用墙上时钟即可
        )
        return error_response(
            status_code=403,
            code=ErrorCode.PLAN_STATUS_WRITE_FORBIDDEN,
            message=(
                "Plan status cannot be modified through this endpoint. To approve, reject, or modify, "
                "use the corresponding action endpoints (/approve, /reject, /modify)."
            ),
            next_actions=[
                NextAction(action="approve", href=f"/plans/{plan_id}/approve"),
                NextAction(action="reject", href=f"/plans/{plan_id}/reject"),
                NextAction(action="modify", href=f"/plans/{plan_id}/modify"),
            ],
            details={"plan_id": plan_id},
        )

    # 未出现 status 键：走 PlanUpdateIn 的常规校验。P0 该模型无可写字段，因此任何非空
    # 请求体都会因 `extra="forbid"` 得到 422；空体则是「无字段可更新」的 no-op。
    try:
        PlanUpdateIn.model_validate(raw if isinstance(raw, dict) else {})
    except ValidationError as error:
        return error_response(
            status_code=422,
            code=ErrorCode.PLAN_UPDATE_INVALID,
            message="The request body contains unaccepted fields. In P0 the plan has no in-place editable fields.",
            next_actions=[NextAction(action="refresh", href=f"/plans/{plan_id}")],
            details={"errors": error.errors(include_url=False)},
        )
    return JSONResponse(
        status_code=200,
        content={"plan_id": plan_id, "updated_fields": []},
    )


# --------------------------------------------------------------------------
# 序列化辅助
# --------------------------------------------------------------------------


def _list_by_status(request: Request, *, status: str) -> list[PlanSummaryOut]:
    factory: sessionmaker[Session] = request.app.state.session_factory
    with factory() as db:
        rows = list(
            db.execute(
                select(orm.ProductionPlan)
                .where(orm.ProductionPlan.status == status)
                .order_by(orm.ProductionPlan.production_date, orm.ProductionPlan.plan_id)
            ).scalars()
        )
        return [
            PlanSummaryOut(
                plan_id=row.plan_id,
                production_date=row.production_date,
                status=row.status,
                plan_version=row.plan_version,
                origin=row.origin,
                feasibility=row.feasibility,
                input_snapshot_version=row.input_snapshot_version,
                generated_by_trace_id=row.generated_by_trace_id,
            )
            for row in rows
        ]


def _detail_from_result(result: PlanGenerationResult) -> PlanDetailOut:
    """把内核算出的 `PlanGenerationResult` 序列化成响应（生成端点用）。"""
    seq_of = {spec.job_id: spec.operation_sequence for spec in result.production_jobs}

    scheduled = [
        ScheduledJobOut(
            job_id=sj.job_id,
            order_id=sj.order_id,
            product_id=sj.product_id,
            operation_sequence=seq_of.get(sj.job_id, 0),
            machine_id=sj.machine_id,
            worker_id=sj.worker_id,
            start_time=sj.start_time,
            end_time=sj.end_time,
            setup_minutes=sj.setup_minutes,
            changeover_minutes=sj.changeover_minutes,
        )
        for sj in result.candidate.scheduled_jobs
    ]
    unschedulable = [
        UnschedulableJobOut(
            job_id=uj.job_id,
            order_id=uj.order_id,
            blocking_reason=uj.blocking_reason,
            unblock_suggestion=dict(uj.unblock_suggestion),
        )
        for uj in result.candidate.unschedulable_jobs
    ]
    breakdown = ObjectiveBreakdownOut(
        components=[
            ComponentScoreOut(
                name=c.name,
                raw_value=c.raw_value,
                weight=c.weight,
                weighted_contribution=c.weighted_contribution,
            )
            for c in result.objective_breakdown.components
        ],
        total_score=result.objective_breakdown.total_score,
        preference_contributions=[
            PreferenceContributionOut(
                rule_id=c.rule_id,
                human_text=c.human_text,
                kind=c.kind,
                violating_job_ids=list(c.violating_job_ids),
                raw_value=c.raw_value,
                weighted_contribution=c.weighted_contribution,
            )
            for c in result.objective_breakdown.preference_contributions
        ],
        weight_overrides_applied=[
            dict(o) for o in result.objective_breakdown.weight_overrides_applied
        ],
    )
    bc = result.baseline
    baseline = BaselineComparisonOut(
        baseline_plan_id=bc.baseline_plan_id,
        snapshot_version=bc.snapshot_version,
        on_time_rate=bc.on_time_rate,
        baseline_on_time_rate=bc.baseline_on_time_rate,
        total_tardiness_minutes=bc.total_tardiness_minutes,
        baseline_total_tardiness_minutes=bc.baseline_total_tardiness_minutes,
        late_order_count=bc.late_order_count,
        baseline_late_order_count=bc.baseline_late_order_count,
    )
    return PlanDetailOut(
        plan_id=result.plan_id,
        production_date=result.production_date,
        status=result.status,
        plan_version=1,
        origin="PLAN_GENERATION",
        input_snapshot_version=result.input_snapshot_version,
        generated_by_trace_id=result.generated_by_trace_id,
        feasibility=result.feasibility,
        scheduled_jobs=scheduled,
        unschedulable_jobs=unschedulable,
        objective_breakdown=breakdown,
        baseline_comparison=baseline,
    )


def _detail_from_db(db: Session, plan: orm.ProductionPlan) -> PlanDetailOut:
    """从库里重建计划全文（读端点用）。形状与 `_detail_from_result` 一致。"""
    sched_rows = list(
        db.execute(
            select(orm.ScheduledJob, orm.ProductionJob)
            .join(orm.ProductionJob, orm.ScheduledJob.job_id == orm.ProductionJob.job_id)
            .where(orm.ScheduledJob.plan_id == plan.plan_id)
            .order_by(orm.ScheduledJob.machine_id, orm.ScheduledJob.start_time)
        )
    )
    scheduled = [
        ScheduledJobOut(
            job_id=sj.job_id,
            order_id=pj.order_id,
            product_id=pj.product_id,
            operation_sequence=pj.operation_sequence,
            machine_id=sj.machine_id,
            worker_id=sj.worker_id,
            start_time=sj.start_time,
            end_time=sj.end_time,
            setup_minutes=sj.setup_minutes,
            changeover_minutes=sj.changeover_minutes,
        )
        for sj, pj in sched_rows
    ]

    unsched_rows = list(
        db.execute(
            select(orm.UnschedulableJob, orm.ProductionJob)
            .join(orm.ProductionJob, orm.UnschedulableJob.job_id == orm.ProductionJob.job_id)
            .where(orm.UnschedulableJob.plan_id == plan.plan_id)
            .order_by(orm.UnschedulableJob.job_id)
        )
    )
    unschedulable = [
        UnschedulableJobOut(
            job_id=uj.job_id,
            order_id=pj.order_id,
            blocking_reason=uj.blocking_reason,
            unblock_suggestion=_as_dict(uj.unblock_suggestion),
        )
        for uj, pj in unsched_rows
    ]

    breakdown_row = db.get(orm.ObjectiveBreakdown, plan.plan_id)
    breakdown: ObjectiveBreakdownOut | None = None
    if breakdown_row is not None:
        components = [
            ComponentScoreOut(**component)
            for component in _as_list(breakdown_row.components)
        ]
        breakdown = ObjectiveBreakdownOut(
            components=components,
            total_score=float(_as_decimal(breakdown_row.total_score)),
            preference_contributions=[
                PreferenceContributionOut(**contribution)
                for contribution in _as_list(breakdown_row.preference_contributions)
                if isinstance(contribution, dict)
            ],
            weight_overrides_applied=[
                override
                for override in _as_list(breakdown_row.weight_overrides_applied)
                if isinstance(override, dict)
            ],
        )

    bc_row = db.get(orm.BaselineComparison, plan.plan_id)
    baseline: BaselineComparisonOut | None = None
    if bc_row is not None:
        baseline = BaselineComparisonOut(
            baseline_plan_id=bc_row.baseline_plan_id,
            snapshot_version=bc_row.snapshot_version,
            on_time_rate=float(_as_decimal(bc_row.on_time_rate)),
            baseline_on_time_rate=float(_as_decimal(bc_row.baseline_on_time_rate)),
            total_tardiness_minutes=bc_row.total_tardiness_minutes,
            baseline_total_tardiness_minutes=bc_row.baseline_total_tardiness_minutes,
            late_order_count=bc_row.late_order_count,
            baseline_late_order_count=bc_row.baseline_late_order_count,
        )

    return PlanDetailOut(
        plan_id=plan.plan_id,
        production_date=plan.production_date,
        status=plan.status,
        plan_version=plan.plan_version,
        origin=plan.origin,
        input_snapshot_version=plan.input_snapshot_version,
        generated_by_trace_id=plan.generated_by_trace_id,
        feasibility=plan.feasibility,
        scheduled_jobs=scheduled,
        unschedulable_jobs=unschedulable,
        objective_breakdown=breakdown,
        baseline_comparison=baseline,
    )


def _duration_minutes(start: datetime, end: datetime) -> int:
    """`[start, end)` 的整分钟长度（与内核 `_minutes_between` 同口径），负值截断为 0。"""
    if end <= start:
        return 0
    return int((end - start).total_seconds() // 60)


def _job_changes(
    delta: object,
    candidate_a: object,
    candidate_b: object,
) -> list[JobChangeOut]:
    """把 `PlanDelta` 的五个集合摊成逐作业的 `JobChangeOut`（R10.1，供并排甘特标注）。

    `ADDED` 只填 `b_*`（A 中无该作业），`REMOVED` 只填 `a_*`（B 中无），
    `MOVED`/`REASSIGNED`/`UNCHANGED` 两侧都填。作业的资源/时间直接取自各自 `PlanCandidate`
    的 `ScheduledJob`（值对象自带 `order_id` / `machine_id` / `worker_id` / 时间），无需回库。
    返回按 (change 优先级, job_id) 稳定排序，供 UI 与测试确定性断言。
    """
    from app.core.delta import PlanDelta
    from app.core.scheduler import PlanCandidate, ScheduledJob

    assert isinstance(delta, PlanDelta)
    assert isinstance(candidate_a, PlanCandidate)
    assert isinstance(candidate_b, PlanCandidate)

    a_by_id: dict[str, ScheduledJob] = {sj.job_id: sj for sj in candidate_a.scheduled_jobs}
    b_by_id: dict[str, ScheduledJob] = {sj.job_id: sj for sj in candidate_b.scheduled_jobs}

    def _order_id(job_id: str) -> str:
        sj = b_by_id.get(job_id) or a_by_id.get(job_id)
        return sj.order_id if sj is not None else ""

    out: list[JobChangeOut] = []

    for job_id in delta.added:
        b = b_by_id[job_id]
        out.append(
            JobChangeOut(
                job_id=job_id,
                order_id=b.order_id,
                change="ADDED",
                b_machine_id=b.machine_id,
                b_worker_id=b.worker_id,
                b_start_time=b.start_time,
                b_end_time=b.end_time,
            )
        )
    for job_id in delta.removed:
        a = a_by_id[job_id]
        out.append(
            JobChangeOut(
                job_id=job_id,
                order_id=a.order_id,
                change="REMOVED",
                a_machine_id=a.machine_id,
                a_worker_id=a.worker_id,
                a_start_time=a.start_time,
                a_end_time=a.end_time,
            )
        )
    for change, job_ids in (
        ("REASSIGNED", delta.reassigned),
        ("MOVED", delta.moved),
        ("UNCHANGED", delta.unchanged),
    ):
        for job_id in job_ids:
            a = a_by_id[job_id]
            b = b_by_id[job_id]
            out.append(
                JobChangeOut(
                    job_id=job_id,
                    order_id=_order_id(job_id),
                    change=change,
                    a_machine_id=a.machine_id,
                    a_worker_id=a.worker_id,
                    a_start_time=a.start_time,
                    a_end_time=a.end_time,
                    b_machine_id=b.machine_id,
                    b_worker_id=b.worker_id,
                    b_start_time=b.start_time,
                    b_end_time=b.end_time,
                )
            )

    order = {"ADDED": 0, "REMOVED": 1, "REASSIGNED": 2, "MOVED": 3, "UNCHANGED": 4}
    return sorted(out, key=lambda c: (order[c.change], c.job_id))


def _explanation_inputs_from_db(
    db: Session, plan: orm.ProductionPlan
) -> ExplanationInputs:
    """从持久化的计划重建解释的确定性输入（结构化证据 + 紧凑载荷）。

    读 `scheduled_jobs`（算每条占用时长，供按机器聚合与关键作业挑选）、`unschedulable_jobs`、
    `objective_breakdowns`（7 分量）、`baseline_comparisons`（六个同口径数值），交给
    `assemble_initial_plan_explanation`。**不读**原始 Order / Machine / Worker / Material
    ——那些正是 R21.12 禁止进入解释载荷的原始清单。
    """
    sched_rows = list(
        db.execute(
            select(orm.ScheduledJob, orm.ProductionJob)
            .join(orm.ProductionJob, orm.ScheduledJob.job_id == orm.ProductionJob.job_id)
            .where(orm.ScheduledJob.plan_id == plan.plan_id)
            .order_by(orm.ScheduledJob.job_id)
        )
    )
    scheduled = tuple(
        ScheduledJobView(
            job_id=sj.job_id,
            order_id=pj.order_id,
            machine_id=sj.machine_id,
            duration_minutes=_duration_minutes(sj.start_time, sj.end_time),
        )
        for sj, pj in sched_rows
    )

    unsched_rows = list(
        db.execute(
            select(orm.UnschedulableJob, orm.ProductionJob)
            .join(orm.ProductionJob, orm.UnschedulableJob.job_id == orm.ProductionJob.job_id)
            .where(orm.UnschedulableJob.plan_id == plan.plan_id)
            .order_by(orm.UnschedulableJob.job_id)
        )
    )
    unschedulable = tuple(
        UnschedulableView(
            job_id=uj.job_id,
            order_id=pj.order_id,
            blocking_reason=uj.blocking_reason,
        )
        for uj, pj in unsched_rows
    )

    breakdown_row = db.get(orm.ObjectiveBreakdown, plan.plan_id)
    components: tuple[ComponentView, ...] = ()
    if breakdown_row is not None:
        components = tuple(
            ComponentView(
                name=str(component["name"]),
                raw_value=float(component["raw_value"]),
                weight=float(component["weight"]),
                weighted_contribution=float(component["weighted_contribution"]),
            )
            for component in _as_list(breakdown_row.components)
        )

    bc_row = db.get(orm.BaselineComparison, plan.plan_id)
    if bc_row is not None:
        baseline = BaselineView(
            on_time_rate=float(_as_decimal(bc_row.on_time_rate)),
            baseline_on_time_rate=float(_as_decimal(bc_row.baseline_on_time_rate)),
            total_tardiness_minutes=bc_row.total_tardiness_minutes,
            baseline_total_tardiness_minutes=bc_row.baseline_total_tardiness_minutes,
            late_order_count=bc_row.late_order_count,
            baseline_late_order_count=bc_row.baseline_late_order_count,
        )
    else:
        # 无基线对比行（不应发生在正式计划上）——用零值占位，解释仍可生成。
        baseline = BaselineView(
            on_time_rate=0.0,
            baseline_on_time_rate=0.0,
            total_tardiness_minutes=0,
            baseline_total_tardiness_minutes=0,
            late_order_count=0,
            baseline_late_order_count=0,
        )

    return assemble_initial_plan_explanation(
        plan_id=plan.plan_id,
        feasibility=plan.feasibility,
        scheduled=scheduled,
        unschedulable=unschedulable,
        components=components,
        baseline=baseline,
    )


def _explanation_out(result: ExplanationResult, *, llm_mode: str) -> ExplanationOut:
    """把 `ExplanationResult` 序列化成响应（结构化证据 + 叙述 + numeric_check）。"""
    exp = result.explanation
    cf = exp.counterfactual
    if isinstance(cf, NoTradeoff):
        counterfactual = CounterfactualOut(kind="NO_TRADEOFF", reason=cf.reason)
    else:
        counterfactual = CounterfactualOut(
            kind="TRADEOFF",
            pivotal_job_id=cf.pivotal_job_id,
            component=cf.component,
            current_value=cf.current_value,
            counterfactual_value=cf.counterfactual_value,
            selection_basis=cf.selection_basis,
        )
    return ExplanationOut(
        plan_id=exp.plan_id,
        llm_mode=llm_mode,
        narrative=result.narrative,
        numeric_check=result.numeric_check.value,
        fallback_reason=result.fallback_reason,
        decision_evidence=[
            DecisionEvidenceOut(
                job_id=ev.job_id,
                trigger=ev.trigger,
                constraint=ev.constraint,
                resources=list(ev.resources),
            )
            for ev in exp.decision_evidence
        ],
        counterfactual=counterfactual,
        assumptions=[
            AssumptionOut(kind=a.kind, description=a.description, stale_risk=a.stale_risk)
            for a in exp.assumptions
        ],
        confidence=ConfidenceOut(
            level=exp.confidence.level.value, basis=exp.confidence.basis
        ),
    )


def _as_dict(value: object) -> dict[str, Any]:
    return {str(k): v for k, v in value.items()} if isinstance(value, dict) else {}


def _as_list(value: object) -> list[Any]:
    return list(value) if isinstance(value, list) else []


def _as_decimal(value: object) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))
