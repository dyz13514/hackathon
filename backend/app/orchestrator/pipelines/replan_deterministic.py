"""确定性重排流水线（任务 7.4，design.md §2.6 / §3.5、R9）。

这是重排的**形态 A 同构**入口：与初始计划生成（`plan_generation.py`）一样是一条固定顺序、
零 LLM 编排的确定性流水线。`DETERMINISTIC_ONLY` 降级模式（任务 11.6）走的正是它；P0 的
`POST /api/disruptions` 也用它作为主路径（用户裁决 Option A + approach b），因为它产出的
`ImpactAnalysis` 数值与走 ReAct 路径时**逐字段相同**——所有数字都来自确定性内核。

## 固定序列（design.md §2.6「扰动重排」行）

    load_snapshot(扰动后) → replan(get_affected_jobs + generate_schedule(freeze) + check)
                          → evaluate_schedule → compute_plan_delta → classify_impact
                          → save_proposed_plan

对齐任务 7.4 bullet 的 `get_affected_jobs → generate_schedule(freeze) → check_constraints →
classify_impact → save_proposed_plan`：其中前三步由 `app.core.replanner.replan` 一次完成
（它内部做受影响集、冻结集、`generate_schedule(freeze=)`、全量 `validate`），本模块在其后
接 `compute_plan_delta`（任务 7.2）与 `classify_impact`（任务 7.3），最后落库。

## 数值全部确定性（R9.4）

`ImpactAnalysis` 的每个数值——`affected_jobs`、`affected_orders`、
`orders_at_risk_of_lateness`、`tardiness_delta_minutes`、`churn_ratio`、`impact_class`——都由
确定性组件算出：`replanner.affected_by`、`compute_plan_delta`、`_plan_kpis`、
`autonomy.classify_impact`。LLM 在本路径上**完全不参与**。ReAct 路径（任务 7.4 的
Planning_Agent 驱动）复用同一批工具 handler，因此它的数值也来自这里的同一批函数——LLM 只
生成 `revision_summary` 叙述，不产生任何数字（design.md §3.6、ADR-010）。

## 与初始生成流水线共享落库机具

修订计划同样落成 `production_plans` 五表 + 基线对比（同口径 KPI，R19.2），复用
`plan_generation` 的 `_expand_production_jobs` / `_plan_kpis` / `save_proposed_plan` /
`_ensure_production_jobs` / `_add_scheduled_jobs`，**不重复**任何一张表的写入逻辑。修订计划的
`origin = 'REPLANNING'`（`SaveProposedPlanIn.origin` 的合法取值之一），状态硬编码
`PENDING_APPROVAL`（R11.8）。额外写一行 `impact_assessments`（R13.12：分级 + 判据可复核）。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.orm import Session

from app.core.autonomy import (
    FeatureFlags,
    ImpactClass,
    ImpactInput,
    classify_impact,
    decide_autonomy,
    decisive_predicates,
)
from app.core.delta import PlanDelta, compute_plan_delta
from app.core.replanner import Disruption as KernelDisruption
from app.core.replanner import ReplanResult, replan
from app.core.scheduler import PlanCandidate
from app.core.scoring import ObjectiveBreakdown, ObjectiveWeights, score
from app.core.snapshot import DomainSnapshot
from app.core.validation import ValidationReport
from app.db import audit
from app.db import models as orm
from app.orchestrator.pipelines.plan_generation import (
    BaselineComparison,
    PlanGenerationResult,
    _add_scheduled_jobs,
    _created_at,
    _ensure_production_jobs,
    _expand_production_jobs,
    _new_plan_id,
    _plan_kpis,
    save_proposed_plan,
)
from app.orchestrator.trace_recorder import DbTracer
from app.services.replanning import active_plan_row, cand_plan_row

#: 修订计划来源。`SaveProposedPlanIn.origin` 的合法取值之一（design.md §8 状态机表）。
REPLAN_ORIGIN = "REPLANNING"
PENDING_STATUS = "PENDING_APPROVAL"

#: Trace.kind / mode。重排在 P0 的确定性路径上也是 PIPELINE（零 LLM 编排）；ReAct 路径
#: （任务 7.4 的 Planning_Agent 驱动）走 mode=REACT，两者共享同一批工具与记账入口。
TRACE_KIND = "REPLAN"
TRACE_MODE = "PIPELINE"

#: 确定性重排流水线的固定阶段名（写 `trace_steps.decision_reason`，R24.7）。逐字对应
#: design.md §2.6「扰动重排」行的确定性序列。
_REPLAN_STAGES: tuple[str, ...] = (
    "get_affected_jobs",
    "generate_schedule_freeze",
    "check_constraints",
    "compute_plan_delta",
    "classify_impact",
    "save_proposed_plan",
)


@dataclass(frozen=True)
class ImpactAnalysis:
    """扰动影响分析（R9.3）。**全部数值由确定性组件计算**（R9.4）。

    字段逐条对齐 R9.3：`affected_jobs`、`affected_orders`、`orders_at_risk_of_lateness`、
    `tardiness_delta_minutes`、`churn_ratio`、`impact_class`。另附 `autonomy_level` 与
    `decisive_predicates`（R13.12），以及 `frozen_job_ids` / `substitute_unavailable_job_ids`
    （重排稳定性与「无替代机器」报告，R9.5 / K-05）供 UI 展示。
    """

    disruption_id: str
    candidate_plan_id: str
    affected_jobs: tuple[str, ...]
    affected_orders: tuple[str, ...]
    orders_at_risk_of_lateness: tuple[str, ...]
    tardiness_delta_minutes: int
    churn_ratio: float
    impact_class: str
    autonomy_level: str
    decisive_predicates: tuple[str, ...]
    frozen_job_ids: tuple[str, ...]
    substitute_unavailable_job_ids: tuple[str, ...]
    #: P0 取值域 `{PROPOSED, ESCALATED}`（design.md §3.6）。L3 → PROPOSED（自主生成
    #: PENDING_APPROVAL 提案，R13.4）；L5 → ESCALATED（强制人工审批，R13.5）。与
    #: `impact_assessments.execution_path` 同源，供 UI 展示「这次是自主提案还是上报人工」。
    execution_path: str = "PROPOSED"


@dataclass(frozen=True)
class ReplanPipelineResult:
    """确定性重排流水线的完整产物。

    `plan` 是落库后的 `PlanGenerationResult`（修订计划，`status = PENDING_APPROVAL`）；
    `impact` 是 `ImpactAnalysis`（确定性数值）；`assessment_id` 关联 `impact_assessments` 行。
    """

    plan: PlanGenerationResult
    impact: ImpactAnalysis
    assessment_id: str


def _orders_at_risk(candidate: PlanCandidate, snapshot: DomainSnapshot) -> tuple[str, ...]:
    """完工晚于 `due_date` 的订单（R9.3 `orders_at_risk_of_lateness`）。确定性。

    每个订单以其最晚 `end_time` 为完工时刻（与 `_plan_kpis` 同口径）；晚于 `due_date` 即在
    风险清单里。返回按 `order_id` 升序，供确定性断言。
    """
    orders_by_id = snapshot.orders_by_id()
    completions: dict[str, datetime] = {}
    for job in candidate.scheduled_jobs:
        cur = completions.get(job.order_id)
        if cur is None or job.end_time > cur:
            completions[job.order_id] = job.end_time
    at_risk = [
        order_id
        for order_id, completion in completions.items()
        if (order := orders_by_id.get(order_id)) is not None and completion > order.due_date
    ]
    return tuple(sorted(at_risk))


def run_replan(
    session: Session,
    *,
    disruption_id: str,
    active_plan_id: str,
    active_plan: PlanCandidate,
    disruption: KernelDisruption,
    snapshot: DomainSnapshot,
    locked_job_ids: frozenset[str],
    now: datetime,
    trigger_source: str = "DISRUPTION",
    session_id: str,
    weights: ObjectiveWeights | None = None,
    flags: FeatureFlags | None = None,
) -> ReplanPipelineResult:
    """执行确定性重排的固定序列并落库（一个事务），返回 `ReplanPipelineResult`。

    入参：`active_plan` 是当前 `ACTIVE` 计划的内核候选（由 `replanning.load_plan_candidate`
    读回）；`snapshot` 必须**已反映扰动后的状态**（扰动登记的停机/缺勤窗与物料变化已写库并
    推进版本，`load_snapshot` 读到的即新快照）；`disruption` 是内核 `Disruption` 值对象。

    调用方持有会话：成功路径末尾 `commit()`，异常 `rollback()` 后重抛。修订计划的五表 +
    基线 + `impact_assessments` 构成一个原子单位。
    """
    resolved_weights = weights if weights is not None else ObjectiveWeights()
    resolved_flags = flags if flags is not None else FeatureFlags()

    # ---- 步 1–3：replan（受影响集 → 冻结 → generate_schedule(freeze) → 全量校验） ----
    replan_result: ReplanResult = replan(
        active_plan, disruption, snapshot, locked_job_ids=locked_job_ids
    )
    candidate = replan_result.candidate
    validation: ValidationReport = replan_result.report

    # ---- 步 4：evaluate_schedule（对修订计划评分；churn 以 active 为参照） ----
    breakdown: ObjectiveBreakdown = score(
        candidate, snapshot, resolved_weights, reference_plan=active_plan
    )

    # ---- 步 5：compute_plan_delta（任务 7.2） ----
    delta: PlanDelta = compute_plan_delta(active_plan, candidate)

    # ---- 同口径 KPI（active vs candidate，供 ImpactInput 与基线对比区） ----
    active_on_time, active_tardiness, active_late = _plan_kpis(active_plan, snapshot)
    cand_on_time, cand_tardiness, cand_late = _plan_kpis(candidate, snapshot)

    # ---- 步 6：classify_impact（任务 7.3）——数值输入，无字符串注入点 ----
    active_row = active_plan_row(
        session,
        active_plan_id,
        active_plan,
        total_tardiness_minutes=active_tardiness,
        unschedulable_count=len(active_plan.unschedulable_jobs),
    )
    cand_row = cand_plan_row(
        session,
        candidate,
        total_tardiness_minutes=cand_tardiness,
        unschedulable_count=len(candidate.unschedulable_jobs),
        # 重排绝不改变任何订单的 promised_date（对客户的承诺）；P0 恒为 False。
        promised_date_changed=False,
    )
    impact_input: ImpactInput = ImpactInput.from_delta(delta, active_row, cand_row)
    impact_class: ImpactClass = classify_impact(impact_input)
    autonomy_level = decide_autonomy(impact_class, resolved_flags)
    predicates = decisive_predicates(impact_input, impact_class)
    execution_path = _execution_path_for(autonomy_level.value)

    affected_orders = tuple(
        sorted({job_id.rsplit("-OP", 1)[0] for job_id in replan_result.affected_job_ids})
    )
    impact = ImpactAnalysis(
        disruption_id=disruption_id,
        candidate_plan_id="",  # 落库后回填
        affected_jobs=replan_result.affected_job_ids,
        affected_orders=affected_orders,
        orders_at_risk_of_lateness=_orders_at_risk(candidate, snapshot),
        tardiness_delta_minutes=cand_tardiness - active_tardiness,
        churn_ratio=delta.churn_ratio,
        impact_class=impact_class.value,
        autonomy_level=autonomy_level.value,
        decisive_predicates=tuple(predicates),
        frozen_job_ids=replan_result.frozen_job_ids,
        substitute_unavailable_job_ids=replan_result.substitute_unavailable_job_ids,
        execution_path=execution_path,
    )

    # ---- 落库（修订计划五表 + 基线 + impact_assessments，一个事务） ----
    plan_id = _new_plan_id()
    baseline_plan_id = _new_plan_id()
    assessment_id = f"IA-{uuid.uuid4().hex[:12]}"

    tracer = DbTracer(session, trigger_source=trigger_source, session_id=session_id, now=now)
    trace = tracer.begin(kind=TRACE_KIND, mode=TRACE_MODE, agent=None)
    trace_id = trace.trace_id

    baseline_comparison = BaselineComparison(
        baseline_plan_id=baseline_plan_id,
        snapshot_version=snapshot.snapshot_version,
        on_time_rate=cand_on_time,
        baseline_on_time_rate=active_on_time,
        total_tardiness_minutes=cand_tardiness,
        baseline_total_tardiness_minutes=active_tardiness,
        late_order_count=cand_late,
        baseline_late_order_count=active_late,
    )

    production_jobs = _expand_production_jobs(snapshot, candidate, active_plan)

    result = PlanGenerationResult(
        plan_id=plan_id,
        production_date=snapshot.production_date,
        status=PENDING_STATUS,
        input_snapshot_version=snapshot.snapshot_version,
        generated_by_trace_id=trace_id,
        candidate=candidate,
        objective_breakdown=breakdown,
        validation=validation,
        baseline=baseline_comparison,
        production_jobs=production_jobs,
    )
    impact = _with_plan_id(impact, plan_id)

    try:
        _record_replan_steps(tracer, trace)
        tracer.set_result_ref(trace, plan_id)
        tracer.end(trace)
        _persist_revision(
            session,
            result=result,
            weights=resolved_weights,
            impact=impact,
            assessment_id=assessment_id,
            active_plan_id=active_plan_id,
            baseline_plan_id=baseline_plan_id,
            disruption_id=disruption_id,
            impact_input=impact_input,
            now=now,
        )
        session.commit()
    except Exception:
        session.rollback()
        raise

    audit.append(
        event_category="DISRUPTION_REGISTERED",
        event_type="REPLAN_PROPOSED",
        actor="PLANNER",
        payload={
            "disruption_id": disruption_id,
            "candidate_plan_id": plan_id,
            "impact_class": impact.impact_class,
            "autonomy_level": impact.autonomy_level,
            "churn_ratio": impact.churn_ratio,
            "tardiness_delta_minutes": impact.tardiness_delta_minutes,
        },
        subject_type="ProductionPlan",
        subject_id=plan_id,
        trace_id=trace_id,
        occurred_at=now,
    )
    # 影响分级的独立审计（R13.12）：记录 impact_class、触发该等级的具体判据、最终执行路径。
    # 与上面的 DISRUPTION_REGISTERED 分开写——后者记「扰动触发了一次重排」，本条记「这次
    # 变更被判为何等级、依据是什么、走了哪条执行路径」，两者读者不同（审计筛选按类别）。
    audit.append(
        event_category="IMPACT_CLASSIFICATION",
        event_type="REPLAN_CLASSIFIED",
        actor="SYSTEM",
        payload={
            "assessment_id": assessment_id,
            "candidate_plan_id": plan_id,
            "impact_class": impact.impact_class,
            "autonomy_level": impact.autonomy_level,
            "execution_path": impact.execution_path,
            "decisive_predicates": list(impact.decisive_predicates),
        },
        subject_type="ImpactAssessment",
        subject_id=assessment_id,
        trace_id=trace_id,
        occurred_at=now,
    )
    return ReplanPipelineResult(plan=result, impact=impact, assessment_id=assessment_id)


def _with_plan_id(impact: ImpactAnalysis, plan_id: str) -> ImpactAnalysis:
    """回填 `candidate_plan_id`（`ImpactAnalysis` 是 frozen，用 replace 语义重建）。"""
    return ImpactAnalysis(
        disruption_id=impact.disruption_id,
        candidate_plan_id=plan_id,
        affected_jobs=impact.affected_jobs,
        affected_orders=impact.affected_orders,
        orders_at_risk_of_lateness=impact.orders_at_risk_of_lateness,
        tardiness_delta_minutes=impact.tardiness_delta_minutes,
        churn_ratio=impact.churn_ratio,
        impact_class=impact.impact_class,
        autonomy_level=impact.autonomy_level,
        decisive_predicates=impact.decisive_predicates,
        frozen_job_ids=impact.frozen_job_ids,
        substitute_unavailable_job_ids=impact.substitute_unavailable_job_ids,
        execution_path=impact.execution_path,
    )


def _persist_revision(
    session: Session,
    *,
    result: PlanGenerationResult,
    weights: ObjectiveWeights,
    impact: ImpactAnalysis,
    assessment_id: str,
    active_plan_id: str,
    baseline_plan_id: str,
    disruption_id: str,
    impact_input: ImpactInput,
    now: datetime,
) -> None:
    """写修订计划五表 + 基线计划头 + `impact_assessments`。**不提交**——调用方持有事务。

    结构与 `plan_generation._persist` 同构：先写两个计划头（基线 DRAFT / 修订
    PENDING_APPROVAL）并 flush 让外键有指向，再写基线作业行与修订四张明细表（复用
    `save_proposed_plan` 守住 §8 前置条件），最后写 `impact_assessments`。

    修订计划的 `origin = 'REPLANNING'`；基线以 active 计划为「同口径对照」——它是重排的参照
    计划，同样以 `status = DRAFT`、`origin = 'BASELINE'` 落库一份快照供 `baseline_comparisons`
    外键指向（重排的「基线」语义是当前 ACTIVE 计划的表现，见 `run_replan` 里的 KPI 赋值）。
    """
    _ensure_production_jobs(session, specs=result.production_jobs)

    session.add(
        orm.ProductionPlan(
            plan_id=baseline_plan_id,
            production_date=result.production_date,
            status="DRAFT",
            feasibility=result.candidate.feasibility,
            plan_version=1,
            version=1,
            input_snapshot_version=result.input_snapshot_version,
            origin="BASELINE",
            generated_by_trace_id=result.generated_by_trace_id,
            created_at=_created_at(result),
        )
    )
    session.add(
        orm.ProductionPlan(
            plan_id=result.plan_id,
            production_date=result.production_date,
            status=PENDING_STATUS,
            feasibility=result.candidate.feasibility,
            plan_version=1,
            version=1,
            input_snapshot_version=result.input_snapshot_version,
            origin=REPLAN_ORIGIN,
            generated_by_trace_id=result.generated_by_trace_id,
            created_at=_created_at(result),
        )
    )
    session.flush()

    # 基线计划头需要有作业行供 baseline_comparisons 外键与对比区读取——重排的「基线」是
    # 当前 ACTIVE 计划的表现，因此写入 ACTIVE 的已排产作业作为对照快照。
    active_candidate = load_plan_candidate_for_baseline(session, active_plan_id)
    _add_scheduled_jobs(session, plan_id=baseline_plan_id, candidate=active_candidate)

    save_proposed_plan(
        session,
        result=result,
        weights=weights,
        origin=REPLAN_ORIGIN,
        produced_in_sandbox=False,
    )

    session.add(
        orm.ImpactAssessment(
            assessment_id=assessment_id,
            candidate_plan_id=result.plan_id,
            baseline_plan_id=active_plan_id,
            disruption_id=disruption_id,
            impact_class=impact.impact_class,
            autonomy_level=impact.autonomy_level,
            decisive_predicates=list(impact.decisive_predicates),
            impact_input=_impact_input_json(impact_input),
            execution_path=impact.execution_path,
            created_at=now,
        )
    )
    session.flush()


def load_plan_candidate_for_baseline(session: Session, plan_id: str) -> PlanCandidate:
    """读回一份计划的已排产作业作为基线对照（供重排的 baseline_comparisons 外键）。

    独立小函数而非直接 import `replanning.load_plan_candidate`，避免 pipelines → services 的
    额外耦合面；两者读同一批列。委派回 `replanning`。
    """
    from app.services.replanning import load_plan_candidate

    return load_plan_candidate(session, plan_id)


def _execution_path_for(autonomy_level: str) -> str:
    """P0 执行路径取值域 `{PROPOSED, ESCALATED}`（design.md §3.6）。

    L3 → `PROPOSED`（自主生成 PENDING_APPROVAL 提案）；L5 → `ESCALATED`（强制人工审批）。
    `AUTO_APPLIED` 属 L4（P1），P0 运行期不出现。
    """
    return "ESCALATED" if autonomy_level == "L5" else "PROPOSED"


def _impact_input_json(x: ImpactInput) -> dict[str, object]:
    """把 `ImpactInput` 的 7 个数值字段落成 `impact_assessments.impact_input`（R13.12）。

    存原值使任何人都能离线复算分级结果，而不必相信当时的输出。
    """
    return {
        "changed_job_count": x.changed_job_count,
        "touches_urgent_or_high": x.touches_urgent_or_high,
        "promised_date_changed": x.promised_date_changed,
        "all_within_same_machine_and_shift": x.all_within_same_machine_and_shift,
        "tardiness_delta_minutes": x.tardiness_delta_minutes,
        "new_unschedulable_count": x.new_unschedulable_count,
        "churn_ratio": x.churn_ratio,
    }


def _record_replan_steps(tracer: DbTracer, trace: object) -> None:
    """把确定性重排的固定 6 步记成 `trace_steps`，并把 `outcome` 定型为 `OK`。"""
    from app.orchestrator.tracing import TraceHandle

    assert isinstance(trace, TraceHandle)
    for stage in _REPLAN_STAGES:
        tracer.record_step(
            trace, step_kind="DETERMINISTIC_STAGE", outcome="OK", detail=stage
        )
    trace.outcome = "OK"
