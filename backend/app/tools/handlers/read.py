"""只读工具的 handler（任务 5.2，R22.3 / R22.12 / R22.14–15）。

10 个只读工具。它们把库里的规划实体投影成 `models.py` 的紧凑契约形态，绝不返回逐
`ScheduledJob` 明细以外的作业级数据——作业明细的唯一出口是 `get_job_details`（ADR-004）。

## handler 的形状与执行上下文

每个 handler 是 `(args: <InputModel>, ctx: ToolContext) -> <OutputModel>`。`ctx.session` 是
调用方（`Orchestrator` / 系统流水线）注入的 `Session`——handler 通过它读库，而**不是**通过
全局状态或自己开会话。这让 handler 可测（注入一个测试会话即可），也让分层扫描成立：handler
住在 `tools/handlers/`，被 `tools/registry` 装配，`app/agents/**` 不 import 它（只经 `invoke`）。

## 读路径复用 `load_snapshot`

订单/产品/物料/机器/工人五类只读工具直接读 `DomainSnapshot`——它已是「哪些数据参与排产」的
唯一权威答案（排除 `REVERTED`，见 `snapshot_loader`）。因此这些 handler 加载一次快照再投影，
而不是各自写 ORM 查询：一致的可见性口径（Agent 看到的订单集合 = 排产器看到的）由此免费得到。

计划句柄 / 明细 / 风险 / 偏好 / 价值这五个工具需要读**计划态**数据（`production_plans`、
`scheduled_jobs`、`risk_findings` 等），这些不在 `DomainSnapshot` 里；它们直接查 ORM。
"""

from __future__ import annotations

from datetime import datetime
from typing import cast

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import models as orm
from app.services.snapshot_loader import load_snapshot
from app.tools import models as m
from app.tools.registry import ToolContext


def _session(ctx: ToolContext) -> Session:
    """从上下文取出会话；未注入即编程错误（只读工具必须在有库的语境下调用）。"""
    if ctx.session is None:
        raise RuntimeError("只读 handler 需要 ctx.session；registry 装配时必须注入会话")
    # `ToolContext.session` 刻意是 `Any`（见 registry）；handler 知道注入的是 `Session`，
    # 在此显式收窄，避免 `warn_return_any`。
    return cast(Session, ctx.session)


def _now(ctx: ToolContext) -> datetime:
    """快照加载所需的 `now`。只读投影不参与确定性排产断言，用当前会话时钟安全。

    只读工具不产出计划，因此它读快照用的 `now` 不进任何 `test_deterministic_scheduling`
    断言——那些断言作用在 `generate_schedule` 的输出上。这里用墙上时钟仅用来解析
    `production_date` 默认值。
    """
    return datetime.now()  # noqa: DTZ005


# --------------------------------------------------------------------------
# get_orders / get_products / get_inventory / get_machines / get_workers
# --------------------------------------------------------------------------


def get_orders(args: m.GetOrdersIn, ctx: ToolContext) -> m.OrderListOut:
    """订单摘要，支持交期区间、优先级过滤与分页（`fields` 投影由 registry 第 ⑤ 步执行）。"""
    snapshot = load_snapshot(_session(ctx), now=_now(ctx))
    orders = list(snapshot.orders)

    if args.date_from is not None:
        orders = [o for o in orders if o.due_date >= args.date_from]
    if args.date_to is not None:
        orders = [o for o in orders if o.due_date <= args.date_to]
    if args.priorities is not None:
        allowed = set(args.priorities)
        orders = [o for o in orders if o.priority in allowed]

    orders = sorted(orders, key=lambda o: o.order_id)
    total = len(orders)
    window = orders[args.offset : args.offset + args.limit]
    items = [
        m.OrderBrief(
            order_id=o.order_id,
            product_id=o.product_id,
            quantity=float(o.quantity),
            due_date=o.due_date,
            promised_date=o.promised_date,
            priority=o.priority,
        )
        for o in window
    ]
    return m.OrderListOut(
        items=items, total=total, truncated=args.offset + args.limit < total
    )


def get_products(args: m.GetProductsIn, ctx: ToolContext) -> m.ProductListOut:
    """产品摘要，含工序数与（可选）路线机型序列。"""
    snapshot = load_snapshot(_session(ctx), now=_now(ctx))
    products = list(snapshot.products)
    if args.product_ids is not None:
        wanted = set(args.product_ids)
        products = [p for p in products if p.product_id in wanted]

    items = []
    for p in sorted(products, key=lambda p: p.product_id):
        ops = p.operations_in_sequence()
        machine_types = (
            [op.required_machine_type for op in ops] if args.include_routing else []
        )
        items.append(
            m.ProductBrief(
                product_id=p.product_id,
                name=p.name,
                operation_count=len(ops),
                machine_types=machine_types,
            )
        )
    return m.ProductListOut(items=items, total=len(items))


def get_inventory(args: m.GetInventoryIn, ctx: ToolContext) -> m.InventoryOut:
    """物料库存摘要，可选带在途到货合计。"""
    snapshot = load_snapshot(_session(ctx), now=_now(ctx))
    materials = list(snapshot.materials)
    if args.material_ids is not None:
        wanted = set(args.material_ids)
        materials = [mat for mat in materials if mat.material_id in wanted]

    items = []
    for mat in sorted(materials, key=lambda mat: mat.material_id):
        incoming = (
            float(sum(d.quantity for d in mat.incoming_deliveries))
            if args.include_incoming
            else None
        )
        items.append(
            m.MaterialBrief(
                material_id=mat.material_id,
                name=mat.name,
                unit=mat.unit,
                quantity_available=float(mat.quantity_available),
                reserved_quantity=float(mat.reserved_quantity),
                incoming_quantity=incoming,
            )
        )
    return m.InventoryOut(items=items, total=len(items))


def get_machines(args: m.GetMachinesIn, ctx: ToolContext) -> m.MachineListOut:
    """机器摘要，支持机型与状态过滤。"""
    snapshot = load_snapshot(_session(ctx), now=_now(ctx))
    machines = list(snapshot.machines)
    if args.machine_types is not None:
        wanted = set(args.machine_types)
        machines = [mc for mc in machines if mc.machine_type in wanted]
    if args.statuses is not None:
        allowed = set(args.statuses)
        machines = [mc for mc in machines if mc.status in allowed]

    items = [
        m.MachineBrief(
            machine_id=mc.machine_id,
            machine_type=mc.machine_type,
            status=mc.status,
            capabilities=list(mc.capabilities),
            rate_multiplier=float(mc.rate_multiplier),
        )
        for mc in sorted(machines, key=lambda mc: mc.machine_id)
    ]
    return m.MachineListOut(items=items, total=len(items))


def get_workers(args: m.GetWorkersIn, ctx: ToolContext) -> m.WorkerListOut:
    """工人摘要，支持技能过滤。"""
    snapshot = load_snapshot(_session(ctx), now=_now(ctx))
    workers = list(snapshot.workers)
    if args.skills is not None:
        wanted = set(args.skills)
        workers = [w for w in workers if wanted.issubset(set(w.skills))]

    items = [
        m.WorkerBrief(
            worker_id=w.worker_id,
            name=w.name,
            skills=list(w.skills),
            shift_start=w.shift_start,
            shift_end=w.shift_end,
        )
        for w in sorted(workers, key=lambda w: w.worker_id)
    ]
    return m.WorkerListOut(items=items, total=len(items))


# --------------------------------------------------------------------------
# get_current_plan / get_job_details（计划态，直接查 ORM）
# --------------------------------------------------------------------------


def _plan_handle(session: Session, plan: orm.ProductionPlan) -> m.PlanHandle:
    """把一行 `production_plans` + 其明细计数投影成 `PlanHandle`（无逐作业字段，ADR-004）。"""
    scheduled_count = session.execute(
        select(orm.ScheduledJob).where(orm.ScheduledJob.plan_id == plan.plan_id)
    ).scalars()
    scheduled_job_count = len(list(scheduled_count))
    unschedulable_count = len(
        list(
            session.execute(
                select(orm.UnschedulableJob).where(
                    orm.UnschedulableJob.plan_id == plan.plan_id
                )
            ).scalars()
        )
    )
    breakdown = session.get(orm.ObjectiveBreakdown, plan.plan_id)
    if breakdown is not None:
        objective = m.ObjectiveSummary(
            total_score=float(breakdown.total_score),
            late_order_count=0,
            total_tardiness_minutes=0,
            total_changeover_minutes=0,
            preference_penalty=0.0,
        )
    else:
        objective = m.ObjectiveSummary(
            total_score=0.0,
            late_order_count=0,
            total_tardiness_minutes=0,
            total_changeover_minutes=0,
            preference_penalty=0.0,
        )
    return m.PlanHandle(
        plan_id=plan.plan_id,
        plan_version=plan.plan_version,
        feasibility=m.Feasibility(plan.feasibility),
        objective=objective,
        scheduled_job_count=scheduled_job_count,
        unschedulable_count=unschedulable_count,
        trace_id=plan.generated_by_trace_id or "",
    )


def get_current_plan(args: m.GetCurrentPlanIn, ctx: ToolContext) -> m.PlanHandle:
    """返回指定计划或当前 `ACTIVE` 计划的句柄（不含明细，ADR-004）。"""
    session = _session(ctx)
    if args.plan_id is not None:
        plan = session.get(orm.ProductionPlan, args.plan_id)
    else:
        plan = session.execute(
            select(orm.ProductionPlan).where(orm.ProductionPlan.status == "ACTIVE")
        ).scalars().first()
    if plan is None:
        raise LookupError("未找到指定计划或当前 ACTIVE 计划")
    return _plan_handle(session, plan)


def get_job_details(args: m.GetJobDetailsIn, ctx: ToolContext) -> m.JobDetailListOut:
    """作业级明细的**唯一出口**（R22.14–15）。`job_ids ≤ 10` 已由输入模型强制。

    在 `scheduled_jobs` 表里按 `job_id` 取出被请求的行，连同 `production_jobs` 的工序序号
    与前序关系拼成明细。缺失的 `job_id` 静默略过（Agent 可能引用了未排产的作业）。
    """
    session = _session(ctx)
    scheduled = session.execute(
        select(orm.ScheduledJob).where(orm.ScheduledJob.job_id.in_(args.job_ids))
    ).scalars().all()
    jobs_by_id = {
        row.job_id: row
        for row in session.execute(
            select(orm.ProductionJob).where(orm.ProductionJob.job_id.in_(args.job_ids))
        ).scalars()
    }

    items = []
    for sj in scheduled:
        pj = jobs_by_id.get(sj.job_id)
        items.append(
            m.JobDetail(
                job_id=sj.job_id,
                order_id=pj.order_id if pj else "",
                product_id=pj.product_id if pj else "",
                operation_sequence=pj.operation_sequence if pj else 0,
                predecessor_job_id=pj.predecessor_job_id if pj else None,
                machine_id=sj.machine_id,
                worker_id=sj.worker_id,
                start_time=sj.start_time,
                end_time=sj.end_time,
                setup_minutes=sj.setup_minutes,
            )
        )
    return m.JobDetailListOut(items=items[:10])


# --------------------------------------------------------------------------
# get_preference_rules / get_risk_findings / get_value_metrics
# --------------------------------------------------------------------------


def get_preference_rules(
    args: m.GetPreferenceRulesIn, ctx: ToolContext
) -> m.PreferenceRuleListOut:
    """偏好规则摘要（R18.11）。`enabled_only` 默认只返回已启用规则。"""
    session = _session(ctx)
    stmt = select(orm.PreferenceRule)
    if args.enabled_only:
        stmt = stmt.where(orm.PreferenceRule.enabled.is_(True))
    rules = session.execute(stmt.order_by(orm.PreferenceRule.rule_id)).scalars().all()

    items = []
    for r in rules[:20]:
        form = r.structured_form if isinstance(r.structured_form, dict) else {}
        items.append(
            m.PreferenceRuleBrief(
                rule_id=r.rule_id,
                human_text=r.human_text,
                kind=str(form.get("kind", "UNKNOWN")),
                enabled=bool(r.enabled),
            )
        )
    return m.PreferenceRuleListOut(items=items, total=len(rules))


def get_risk_findings(
    args: m.GetRiskFindingsIn, ctx: ToolContext
) -> m.RiskFindingListOut:
    """风险发现摘要，按严重度过滤（R14.9）。叙述 P0 恒为模板来源。"""
    session = _session(ctx)
    severity_rank = {"INFO": 0, "WARNING": 1, "CRITICAL": 2}
    floor = severity_rank[args.min_severity]
    rows = session.execute(select(orm.RiskFinding)).scalars().all()
    filtered = [r for r in rows if severity_rank.get(str(r.severity), 0) >= floor]
    filtered.sort(key=lambda r: (-severity_rank.get(str(r.severity), 0), r.finding_id))

    items = []
    for r in filtered[: args.limit]:
        items.append(
            m.RiskFindingBrief(
                finding_id=r.finding_id,
                risk_type=str(r.risk_type),
                severity=str(r.severity),  # type: ignore[arg-type]
                subject_id=str(getattr(r, "subject_id", "") or ""),
                metric_value=float(getattr(r, "metric_value", 0) or 0),
                threshold=float(getattr(r, "threshold", 0) or 0),
                narrative=str(getattr(r, "narrative", "") or ""),
            )
        )
    return m.RiskFindingListOut(items=items, total=len(filtered))


def get_value_metrics(args: m.GetValueMetricsIn, ctx: ToolContext) -> m.ValueMetricsOut:
    """价值台账当前值摘要（R19.1）。从该计划的 `baseline_comparisons` 读同口径 KPI。"""
    session = _session(ctx)
    plan_id = args.plan_id
    if plan_id is None:
        active = session.execute(
            select(orm.ProductionPlan).where(orm.ProductionPlan.status == "ACTIVE")
        ).scalars().first()
        plan_id = active.plan_id if active is not None else None

    bc = session.get(orm.BaselineComparison, plan_id) if plan_id is not None else None
    if bc is None:
        return m.ValueMetricsOut(
            plan_id=plan_id,
            on_time_rate=0.0,
            baseline_on_time_rate=0.0,
            total_tardiness_minutes=0,
            baseline_total_tardiness_minutes=0,
        )
    return m.ValueMetricsOut(
        plan_id=plan_id,
        on_time_rate=float(bc.on_time_rate),
        baseline_on_time_rate=float(bc.baseline_on_time_rate),
        total_tardiness_minutes=bc.total_tardiness_minutes,
        baseline_total_tardiness_minutes=bc.baseline_total_tardiness_minutes,
    )
