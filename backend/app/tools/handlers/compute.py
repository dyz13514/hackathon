"""确定性计算工具的 handler（任务 5.2，R22.4、ADR-004）。

9 个计算工具。它们**委派给确定性内核**（`app/core/*`）与服务层，绝不在这里重新实现排产/
校验/评分逻辑——内核的价值正在于「排产器与校验器独立实现」，handler 再抄一遍会毁掉那道
交叉验证。返回一律为句柄/聚合形态（`PlanHandle` / `ObjectiveBreakdownOut` / …），作业级
明细只经 `get_job_details`（ADR-004）。

## 已接线 vs 占位

`generate_schedule` 在此完整接线：加载快照 → 调 `scheduler.generate_schedule` → 评分 →
投影成 `PlanHandle`。这条路径依赖的内核（任务 2.4 / 2.10）已全部落地。

其余若干工具依赖尚未落地的内核/服务件——从**已持久化的 `plan_id`** 重建 `PlanCandidate`
（需要 §7 的重排流水线里那套「读计划态回内核值对象」的机具）、沙箱推演（§8）、影响分级
（§7.3 的 `Autonomy_Policy_Engine`）、风险扫描内核（§8 的 `Risk_Radar`）。这些 handler
现在定义完整的输入/输出契约（使 26 工具注册表完整、白名单矩阵契约测试可覆盖每一格），执行
体则委派「若内核函数已存在」否则抛 `NotImplementedError` 并指明落地任务。这样契约层此刻
即完整，接线是后续任务把 `raise` 换成 `return delegate(...)` 的一行改动。
"""

from __future__ import annotations

from datetime import datetime
from typing import cast

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.scheduler import PlanCandidate, ScheduledJob
from app.core.scheduler import generate_schedule as kernel_generate
from app.core.scoring import ObjectiveBreakdown, ObjectiveWeights, score
from app.services.snapshot_loader import load_snapshot
from app.tools import models as m
from app.tools.registry import ToolContext


def _session(ctx: ToolContext) -> Session:
    if ctx.session is None:
        raise RuntimeError("计算 handler 需要 ctx.session；registry 装配时必须注入会话")
    # `ToolContext.session` 刻意是 `Any`（见 registry）；在此显式收窄为 `Session`。
    return cast(Session, ctx.session)


def _now(ctx: ToolContext) -> datetime:
    return datetime.now()  # noqa: DTZ005


def _objective_summary(breakdown: ObjectiveBreakdown) -> m.ObjectiveSummary:
    """把内核 `ObjectiveBreakdown` 投影成契约的紧凑 `ObjectiveSummary`。"""
    by_name = {c.name: c.raw_value for c in breakdown.components}
    return m.ObjectiveSummary(
        total_score=float(breakdown.total_score),
        late_order_count=int(by_name.get("late_order_count", 0.0)),
        total_tardiness_minutes=int(by_name.get("total_tardiness_minutes", 0.0)),
        churn_ratio=by_name.get("churn_ratio"),
        total_changeover_minutes=int(by_name.get("total_changeover_minutes", 0.0)),
        preference_penalty=float(by_name.get("preference_penalty", 0.0)),
    )


def _plan_handle_from_candidate(
    candidate: PlanCandidate, breakdown: ObjectiveBreakdown, *, plan_id: str, trace_id: str
) -> m.PlanHandle:
    return m.PlanHandle(
        plan_id=plan_id,
        plan_version=1,
        feasibility=m.Feasibility(candidate.feasibility),
        objective=_objective_summary(breakdown),
        scheduled_job_count=len(candidate.scheduled_jobs),
        unschedulable_count=len(candidate.unschedulable_jobs),
        trace_id=trace_id,
    )


# --------------------------------------------------------------------------
# generate_schedule —— 完整接线（内核已落地）
# --------------------------------------------------------------------------


def generate_schedule(args: m.GenerateScheduleIn, ctx: ToolContext) -> m.PlanHandle:
    """确定性全序排产，落成 **DRAFT 候选**并返回句柄（R22.13、ADR-004、design.md §8）。

    在当前快照上跑排产 + 评分，把候选经 `save_candidate` 落成一行 `DRAFT` 计划（§8 的
    `∅ → DRAFT`），返回携带**真实 `plan_id`** 的 `PlanHandle`——不含任何逐作业字段。

    为什么要落库（相对任务 5.2 初版的窄改动）：ReAct 序列的后续步骤
    （`check_constraints` / `classify_impact` / `save_proposed_plan`）都按 `plan_id` 读回候选，
    而工具之间只能传句柄不能传内存对象。返回一个临时的 `CAND-` 句柄会让这些下游工具拿着一个
    库里不存在的 id——`save_proposed_plan` 尤其需要一份可读回的 DRAFT 候选才能物化成
    `PENDING_APPROVAL`（任务 7.4）。因此这里把候选真正写成 DRAFT 行并返回其 id。

    冻结集/排除机器由入参透传给内核；`weight_overrides` 目前不改内核权重（任务 11.x 的
    `ADJUST_OBJECTIVE_WEIGHT` 落地后接入），此处用默认权重评分。DRAFT 计划不受
    `ux_pending_per_day` / `ux_active_per_day` 约束，因此多次调用可并存多份候选。
    """
    from app.orchestrator.pipelines.plan_generation import save_candidate

    session = _session(ctx)
    now = _now(ctx)
    snapshot = load_snapshot(session, now=now, production_date=args.production_date)
    frozen = _frozen_jobs(session, args.freeze_job_ids)
    candidate = kernel_generate(
        snapshot,
        freeze=frozen,
        exclude_machine_ids=frozenset(args.exclude_machine_ids),
    )
    breakdown = score(candidate, snapshot, ObjectiveWeights())
    plan_id = save_candidate(
        session, candidate=candidate, snapshot=snapshot, breakdown=breakdown, now=now
    )
    return _plan_handle_from_candidate(
        candidate, breakdown, plan_id=plan_id, trace_id=ctx.trace_id
    )


def _frozen_jobs(session: Session, freeze_job_ids: list[str]) -> tuple[ScheduledJob, ...]:
    """把 `freeze_job_ids` 解析成冻结的 `ScheduledJob` 元组（供 `generate_schedule(freeze=)`）。

    冻结作业来自当前 `ACTIVE` 计划的已排产行——重排时不动它们（design.md §3.5）。空列表时返回
    空元组（初始生成无冻结）。`ScheduledJob` 值对象从持久化行重建，与
    `replanning.load_plan_candidate` 同口径。
    """
    if not freeze_job_ids:
        return ()
    from app.services.replanning import require_any_active_plan

    orm = m_orm()
    try:
        active = require_any_active_plan(session)
    except Exception:
        return ()
    rows = session.execute(
        select(orm.ScheduledJob, orm.ProductionJob)
        .join(orm.ProductionJob, orm.ScheduledJob.job_id == orm.ProductionJob.job_id)
        .where(
            orm.ScheduledJob.plan_id == active.plan_id,
            orm.ScheduledJob.job_id.in_(list(freeze_job_ids)),
        )
    ).all()
    return tuple(
        ScheduledJob(
            job_id=sj.job_id,
            order_id=pj.order_id,
            product_id=pj.product_id,
            machine_id=sj.machine_id,
            worker_id=sj.worker_id,
            start_time=sj.start_time,
            end_time=sj.end_time,
            setup_minutes=sj.setup_minutes,
            changeover_minutes=sj.changeover_minutes,
        )
        for sj, pj in rows
    )


# --------------------------------------------------------------------------
# 依赖「从持久化 plan_id 重建 PlanCandidate」的计算工具（§7 落地）
# --------------------------------------------------------------------------


def check_constraints(args: m.CheckConstraintsIn, ctx: ToolContext) -> m.ValidationOut:
    """对已持久化计划跑全部 9 类硬约束（R6.1–R6.6）。委派给 `validation.validate`。

    需要先把 `plan_id` 的 `scheduled_jobs` / `unschedulable_jobs` 重建成内核
    `PlanCandidate`——那套「读计划态回内核值对象」的机具随 §7 重排流水线落地。届时本 handler
    即为：重建 candidate → `validate(candidate, snapshot)` → 投影成 `ValidationOut`。
    """
    from app.core.validation import validate
    from app.services.replanning import load_plan_candidate

    session = _session(ctx)
    snapshot = load_snapshot(session, now=_now(ctx))
    candidate = load_plan_candidate(session, args.plan_id)
    report = validate(candidate, snapshot)
    return m.ValidationOut(
        plan_id=args.plan_id,
        feasibility=m.Feasibility(candidate.feasibility),
        violation_count=len(report.violations),
        violations=[
            m.ViolationBrief(
                violation_type=v.violation_type,
                job_ids=list(v.job_ids)[:20],
                human_description=v.human_description,
            )
            for v in report.violations[:20]
        ],
    )


def evaluate_schedule(
    args: m.EvaluateScheduleIn, ctx: ToolContext
) -> m.ObjectiveBreakdownOut:
    """对已持久化计划评分（R7.1–R7.2）。委派给 `scoring.score`。

    同 `check_constraints`：待「读计划态回内核值对象」的机具落地后，重建 candidate →
    `score(...)` → 投影成 `ObjectiveBreakdownOut`（7 条分量摘要 + 总分）。
    """
    from app.services.replanning import load_plan_candidate

    session = _session(ctx)
    snapshot = load_snapshot(session, now=_now(ctx))
    candidate = load_plan_candidate(session, args.plan_id)
    breakdown = score(candidate, snapshot, ObjectiveWeights())
    return m.ObjectiveBreakdownOut(
        plan_id=args.plan_id,
        total_score=float(breakdown.total_score),
        components=[
            m.ComponentScoreBrief(
                name=c.name,
                raw_value=c.raw_value,
                weight=c.weight,
                weighted_contribution=c.weighted_contribution,
            )
            for c in breakdown.components
        ],
    )


def compute_baseline(
    args: m.ComputeBaselineIn, ctx: ToolContext
) -> m.BaselineComparisonOut:
    """同口径 FCFS 基线对比（R19.2）。委派给 `baseline.fcfs` + `assert_same_version_as`。

    P0 的初始生成流水线（`plan_generation`）已在内部算出 `BaselineComparison` 并落库；作为
    工具的 `compute_baseline` 读回该计划的 `baseline_comparisons` 行即可。此接线随 §7 的
    「计划态读回」机具一并落地。
    """
    session = _session(ctx)
    orm = m_orm()
    row = session.get(orm.BaselineComparison, args.plan_id)
    if row is None:
        raise RuntimeError(f"计划 {args.plan_id} 无 baseline_comparisons 行")
    return m.BaselineComparisonOut(
        plan_id=args.plan_id,
        baseline_plan_id=row.baseline_plan_id,
        snapshot_version=row.snapshot_version,
        on_time_rate=float(row.on_time_rate),
        baseline_on_time_rate=float(row.baseline_on_time_rate),
        total_tardiness_minutes=row.total_tardiness_minutes,
        baseline_total_tardiness_minutes=row.baseline_total_tardiness_minutes,
        late_order_count=row.late_order_count,
        baseline_late_order_count=row.baseline_late_order_count,
    )


# --------------------------------------------------------------------------
# 依赖 §7 重排 / §8 沙箱 / §7.3 自治引擎的计算工具
# --------------------------------------------------------------------------


def compare_plans(args: m.ComparePlansIn, ctx: ToolContext) -> m.ComparePlansOut:
    """两份计划的聚合 diff（句柄形态，ADR-004）。委派给 `compute_plan_delta`（任务 7.2）。

    只给聚合计数与 churn，不给逐行 diff——想看明细走 `get_job_details`。两份计划都从持久化的
    `scheduled_jobs` 重建成内核 `PlanCandidate`（`replanning.load_plan_candidate`），再调
    `compute_plan_delta`（design.md §3.5 的五集合划分 + 并集分母 churn）。`objective_delta`
    在句柄层用两计划评分的差；`top_changed_job_ids` 取变更集前 10 个（确定性排序）。
    """
    from app.core.delta import compute_plan_delta
    from app.core.scoring import ObjectiveWeights, score
    from app.services.replanning import load_plan_candidate

    session = _session(ctx)
    snapshot = load_snapshot(session, now=_now(ctx))
    plan_a = load_plan_candidate(session, args.plan_id_a)
    plan_b = load_plan_candidate(session, args.plan_id_b)
    delta = compute_plan_delta(plan_a, plan_b)

    weights = ObjectiveWeights()
    score_a = score(plan_a, snapshot, weights)
    score_b = score(plan_b, snapshot, weights, reference_plan=plan_a)
    raw_a = {c.name: c.raw_value for c in score_a.components}
    raw_b = {c.name: c.raw_value for c in score_b.components}
    objective_delta = m.ObjectiveDelta(
        total_score=float(score_b.total_score - score_a.total_score),
        late_order_count=int(
            raw_b.get("late_order_count", 0.0) - raw_a.get("late_order_count", 0.0)
        ),
        total_tardiness_minutes=int(
            raw_b.get("total_tardiness_minutes", 0.0) - raw_a.get("total_tardiness_minutes", 0.0)
        ),
        total_changeover_minutes=int(
            raw_b.get("total_changeover_minutes", 0.0)
            - raw_a.get("total_changeover_minutes", 0.0)
        ),
    )
    changed = (*delta.added, *delta.removed, *delta.reassigned, *delta.moved)
    return m.ComparePlansOut(
        added_count=len(delta.added),
        removed_count=len(delta.removed),
        moved_count=len(delta.moved),
        reassigned_count=len(delta.reassigned),
        unchanged_count=len(delta.unchanged),
        churn_ratio=delta.churn_ratio,
        objective_delta=objective_delta,
        top_changed_job_ids=list(changed[:10]),
    )


def get_affected_jobs(args: m.GetAffectedJobsIn, ctx: ToolContext) -> m.AffectedJobsOut:
    """扰动波及的作业/订单聚合（R9）。委派给 `replanner.affected_by`（任务 7.1）。

    从 `disruptions` 行取回登记内容 → 映射成内核 `Disruption` → 在当前（扰动后）快照与
    ACTIVE 计划上算受影响集。ACTIVE 计划从持久化的 `scheduled_jobs` 重建
    （`replanning.load_plan_candidate`）。`affected_order_ids` 由作业 ID 的 `{order}-OP{seq}`
    前缀去重得到。
    """
    from app.core.replanner import affected_by
    from app.services.replanning import load_plan_candidate, to_kernel_disruption

    session = _session(ctx)
    row = session.get(m_orm().Disruption, args.disruption_id)
    if row is None:
        raise RuntimeError(f"扰动 {args.disruption_id} 不存在")
    payload = _disruption_input_from_row(row)
    kernel_disruption = to_kernel_disruption(payload)

    snapshot = load_snapshot(session, now=_now(ctx))
    active_plan = load_plan_candidate(session, row.active_plan_id)
    affected = affected_by(kernel_disruption, active_plan, snapshot)
    affected_sorted = tuple(sorted(affected))
    order_ids = tuple(sorted({job_id.rsplit("-OP", 1)[0] for job_id in affected_sorted}))
    return m.AffectedJobsOut(
        disruption_id=args.disruption_id,
        affected_job_ids=list(affected_sorted[:60]),
        affected_order_ids=list(order_ids[:60]),
        affected_job_count=len(affected_sorted),
        affected_order_count=len(order_ids),
    )


def classify_impact(args: m.ClassifyImpactIn, ctx: ToolContext) -> m.ImpactOut:
    """影响分级 + 自主等级裁决（R13）。委派给 §7.3 的 `Autonomy_Policy_Engine`（任务 7.3）。

    分级只吃 `ImpactInput` 的 7 个数值字段（无字符串入口，因此不可被 LLM 影响，
    `test_layering.py` 断言它与 LLM 无 import 边）。本 handler 组装那 7 个数值并调用
    `classify_impact` / `decide_autonomy` / `decisive_predicates`：

    `candidate_plan_id` 是修订计划，`baseline_plan_id` 缺省取当前 ACTIVE。两份计划从持久化
    `scheduled_jobs` 重建，`compute_plan_delta` 求变更集，KPI 由确定性口径算出，再经
    `ImpactInput.from_delta` 组装——全程无 LLM，Agent 提供的只有经 schema 校验的 `plan_id`。
    """
    from app.core.autonomy import (
        ImpactInput,
        decide_autonomy,
        decisive_predicates,
    )
    from app.core.autonomy import (
        classify_impact as kernel_classify,
    )
    from app.core.delta import compute_plan_delta
    from app.services.feature_flags import read_feature_flags
    from app.services.replanning import (
        active_plan_row,
        cand_plan_row,
        load_plan_candidate,
        require_any_active_plan,
    )

    session = _session(ctx)
    snapshot = load_snapshot(session, now=_now(ctx))

    baseline_plan_id = args.baseline_plan_id
    if baseline_plan_id is None:
        baseline_plan_id = require_any_active_plan(session).plan_id

    active_plan = load_plan_candidate(session, baseline_plan_id)
    candidate = load_plan_candidate(session, args.candidate_plan_id)
    delta = compute_plan_delta(active_plan, candidate)

    from app.orchestrator.pipelines.plan_generation import _plan_kpis

    _, active_tardiness, _ = _plan_kpis(active_plan, snapshot)
    _, cand_tardiness, _ = _plan_kpis(candidate, snapshot)

    active_row = active_plan_row(
        session,
        baseline_plan_id,
        active_plan,
        total_tardiness_minutes=active_tardiness,
        unschedulable_count=len(active_plan.unschedulable_jobs),
    )
    cand_row = cand_plan_row(
        session,
        candidate,
        total_tardiness_minutes=cand_tardiness,
        unschedulable_count=len(candidate.unschedulable_jobs),
        promised_date_changed=False,
    )
    x = ImpactInput.from_delta(delta, active_row, cand_row)
    impact_class = kernel_classify(x)
    # 运行期特性开关从 `settings` 表读（R13.8）；缺行即 P0 默认（False）。IMPACT_MAJOR 的
    # L5 判定在 decide_autonomy 内读 flags 之前就返回，开关如何设都不能覆盖（R13.5）。
    autonomy_level = decide_autonomy(impact_class, read_feature_flags(session))
    predicates = decisive_predicates(x, impact_class)
    return m.ImpactOut(
        impact_class=impact_class.value,  # type: ignore[arg-type]
        autonomy_level=autonomy_level.value,  # type: ignore[arg-type]
        decisive_predicates=predicates[:8],
        churn_ratio=x.churn_ratio,
        tardiness_delta_minutes=x.tardiness_delta_minutes,
        changed_job_count=x.changed_job_count,
        promised_date_changed=x.promised_date_changed,
        touches_high_priority=x.touches_urgent_or_high,
        new_unschedulable_count=x.new_unschedulable_count,
    )


def m_orm():  # noqa: ANN201
    """延迟 import ORM（避免 handler 模块顶层拖入 `app.db`；分层规则只约束 `app.core`）。"""
    from app.db import models

    return models


def _disruption_input_from_row(row: object):  # noqa: ANN202
    """把持久化的 `disruptions` 行还原成 `DisruptionInput`（读 `payload` JSON）。"""
    from datetime import date as _date

    from app.services.replanning import DisruptionInput

    payload = getattr(row, "payload", {}) or {}
    if not isinstance(payload, dict):
        payload = {}

    def _dt(key: str) -> datetime | None:
        raw = payload.get(key)
        return datetime.fromisoformat(raw) if isinstance(raw, str) else None

    def _d(key: str) -> _date | None:
        raw = payload.get(key)
        return _date.fromisoformat(raw) if isinstance(raw, str) else None

    from decimal import Decimal as _Decimal

    def _dec(key: str) -> _Decimal | None:
        raw = payload.get(key)
        return _Decimal(str(raw)) if raw is not None else None

    return DisruptionInput(
        type=str(getattr(row, "type", "")),
        reported_at=getattr(row, "reported_at", _now_module()),
        machine_id=payload.get("machine_id"),
        worker_id=payload.get("worker_id"),
        window_start=_dt("window_start"),
        window_end=_dt("window_end"),
        material_id=payload.get("material_id"),
        available_quantity=_dec("available_quantity"),
        delivery_id=payload.get("delivery_id"),
        new_eta=_dt("new_eta"),
        product_id=payload.get("product_id"),
        quantity=_dec("quantity"),
        due_date=_d("due_date"),
    )


def _now_module() -> datetime:
    return datetime.now()  # noqa: DTZ005


def run_scenario(args: m.RunScenarioIn, ctx: ToolContext) -> m.ScenarioOut:
    """沙箱推演（R16）。委派给 §8 的 `Scenario_Sandbox.run_sandbox`（句柄形态，ADR-004）。

    5 类结构化 `ScenarioMutation` 已在契约里定义（R16.2）。沙箱在生产数据的**内存副本**上跑
    内核（design.md §3.7 的两层隔离），返回聚合摘要 + 相对 ACTIVE 的 delta——无逐作业明细。
    沙箱随 §8 落地。
    """
    raise NotImplementedError(
        "run_scenario 委派 §8 的 Scenario_Sandbox.run_sandbox；5 类 mutation 契约已定义"
    )


def scan_risks(args: m.ScanRisksIn, ctx: ToolContext) -> m.RiskFindingListOut:
    """滚动时域风险扫描（R14）。委派给 §8 的 `Risk_Scanner`（确定性度量 + 模板叙述）。

    5 类风险的度量与 `severity` 阈值由确定性代码计算（R14.4），叙述在 P0 恒为模板
    （`narrative_source = TEMPLATE`）。本 handler 扫描并去重落库（`scan_and_persist`），再把
    结果投影成句柄式 `RiskFindingListOut`（按严重度排序，`items ≤ 10`，ADR-004）。无 LLM。
    """
    from app.services.risk_scan import scan_and_persist

    session = _session(ctx)
    result = scan_and_persist(session, now=_now(ctx), horizon_days=args.horizon_days)

    severity_rank = {"CRITICAL": 0, "WARNING": 1, "INFO": 2}
    rows = sorted(
        result.findings,
        key=lambda r: (severity_rank.get(str(r.severity), 2), r.finding_key),
    )
    items = [
        m.RiskFindingBrief(
            finding_id=r.finding_id,
            risk_type=str(r.risk_type),
            severity=str(r.severity),  # type: ignore[arg-type]
            subject_id=str(r.entity_id),
            metric_value=float(r.metric_value),
            threshold=float(r.threshold_value),
            narrative=str(r.narrative or ""),
        )
        for r in rows[:10]
    ]
    return m.RiskFindingListOut(items=items, total=len(result.findings))
