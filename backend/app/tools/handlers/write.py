"""写入工具的 handler（任务 5.2，R22.5、R22.9、R23.4）。

4 个写入工具。它们把 Agent/流水线的意图落成持久记录，其中两处的**状态与启用位是硬编码的**，
不接受任何输入覆盖——这是 R22.9 / R23.4 / R18.4 在工具层的强制点：

- `save_proposed_plan` 只写 `status=PENDING_APPROVAL`（无 `status` 入参），复用
  `plan_generation` 流水线里那道 `save_proposed_plan` 守卫（`origin != 'BASELINE'` 且
  `produced_in_sandbox == False`）——没有任何工具能把计划写成 `ACTIVE`。
- `propose_preference_rule` 只写 `enabled=False`（无 `enabled` 入参）。**P0 不接线，仅 P1
  使用**；此处定义完整契约并留一个明确的占位，使 26 工具注册表完整、白名单矩阵契约覆盖到它。

`register_disruption` 与 `save_import_batch` 落库路径分别随 §7（扰动登记）与 §2（摄取流水线）
落地；契约在此完整定义，接线后委派对应服务。
"""

from __future__ import annotations

from typing import cast

from sqlalchemy.orm import Session

from app.orchestrator.pipelines.plan_generation import (
    PENDING_STATUS,
    SaveProposedPlanRejected,
)
from app.tools import models as m
from app.tools.registry import ToolContext

#: 基线来源常量。`save_proposed_plan` 拒绝把基线推进审批流（design.md §8，与流水线守卫同源）。
BASELINE_ORIGIN = "BASELINE"


def _session(ctx: ToolContext) -> Session:
    if ctx.session is None:
        raise RuntimeError("写入 handler 需要 ctx.session；registry 装配时必须注入会话")
    # `ToolContext.session` 刻意是 `Any`（见 registry）；在此显式收窄为 `Session`。
    return cast(Session, ctx.session)


def save_proposed_plan(
    args: m.SaveProposedPlanIn, ctx: ToolContext
) -> m.SaveProposedPlanOut:
    """把候选计划落成 `PENDING_APPROVAL` 提案（R22.9、R23.4，复用流水线守卫）。

    **状态硬编码**：输出模型 `SaveProposedPlanOut.status` 是 `Literal["PENDING_APPROVAL"]`，
    输入模型没有 `status` 字段——「把计划写成 `ACTIVE`」在类型层面不可表达。此外复用流水线
    的 §8 守卫：`origin == 'BASELINE'` 直接抛 `SaveProposedPlanRejected`（基线永不进审批流）。

    落库路径（把 `candidate_plan_id` 的候选计划物化成五张表）随 §7 的「计划态读回/写回」机具
    落地——P0 的初始提案由 `plan_generation` 流水线直接产出并落库，不经本工具。本 handler 此刻
    强制状态语义并守住 §8 前置条件，持久化接线后委派 `plan_generation.save_proposed_plan`。
    """
    # §8 前置条件（与 plan_generation.save_proposed_plan 同源）：基线永不进审批流。
    if args.origin == BASELINE_ORIGIN:
        raise SaveProposedPlanRejected(
            f"基线计划（origin={args.origin!r}）永不进审批流，不能经 save_proposed_plan 落成 "
            f"{PENDING_STATUS}（design.md §8）"
        )
    from app.orchestrator.pipelines.plan_generation import (
        materialize_pending_from_candidate,
    )

    session = _session(ctx)
    now = _now()
    # 从 DRAFT 候选（generate_schedule 落库）创建一个新的 PENDING_APPROVAL 计划行。
    # design.md §8：DRAFT → PENDING_APPROVAL 由 save_proposed_plan 执行——采用**创建新行**
    # 而非改写 DRAFT 的 status（后者只允许经 Approval_Service 的 update_plan_status_if_version，
    # `test_layering.py` 断言其调用点仅限 approval.py）。§8 的基线/沙箱守卫在
    # `materialize_pending_from_candidate` 内复用 plan_generation.save_proposed_plan 施加。
    # 返回带上候选与评分，避免再读一次快照（会话此刻已有未提交行，load_snapshot 会拒绝脏会话）。
    proposal = materialize_pending_from_candidate(
        session,
        candidate_plan_id=args.candidate_plan_id,
        production_date=args.production_date,
        origin=args.origin,
        now=now,
    )
    candidate = proposal.candidate
    breakdown = proposal.breakdown
    by_name = {c.name: c.raw_value for c in breakdown.components}
    return m.SaveProposedPlanOut(
        plan_id=proposal.plan_id,
        plan_version=1,
        feasibility=m.Feasibility(candidate.feasibility),
        objective=m.ObjectiveSummary(
            total_score=float(breakdown.total_score),
            late_order_count=int(by_name.get("late_order_count", 0.0)),
            total_tardiness_minutes=int(by_name.get("total_tardiness_minutes", 0.0)),
            churn_ratio=by_name.get("churn_ratio"),
            total_changeover_minutes=int(by_name.get("total_changeover_minutes", 0.0)),
            preference_penalty=float(by_name.get("preference_penalty", 0.0)),
        ),
        scheduled_job_count=len(candidate.scheduled_jobs),
        unschedulable_count=len(candidate.unschedulable_jobs),
        trace_id=ctx.trace_id,
    )


def register_disruption(
    args: m.RegisterDisruptionIn, ctx: ToolContext
) -> m.RegisterDisruptionOut:
    """登记一类扰动（R9.1）。委派给 §7 的扰动登记服务（写 `disruptions` + 相应停机/缺勤窗）。

    5 类扰动的判别联合载荷把 `RegisterDisruptionIn.payload` 摊平成服务层的 `DisruptionInput`，
    再交 `replanning.register_disruption`：写 `disruptions` 行，`MACHINE_BREAKDOWN` /
    `WORKER_UNAVAILABLE` 同时写 `machine_downtime` / `worker_absences` 并回指该扰动，物料类
    扰动施加库存/ETA 副作用（R9.7）。无 `ACTIVE` 计划 → `NoActivePlanError`（R9.8）。

    本 handler 只登记扰动并返回 `disruption_id`——重排由调用方（ReAct 循环或确定性流水线）随后
    以 `get_affected_jobs → generate_schedule(freeze) → ...` 继续。它**不**发起重排，也不改计划
    状态（唯一能置 `ACTIVE` 的是 `Approval_Service`）。不提交——registry 装配的会话持有事务。
    """
    from app.services.replanning import (
        register_disruption as service_register,
    )
    from app.services.replanning import (
        require_any_active_plan,
    )

    session = _session(ctx)
    active_plan = require_any_active_plan(session)
    payload = _disruption_input_from_contract(args)
    row = service_register(
        session,
        payload,
        active_plan_id=active_plan.plan_id,
        source="PLANNER_UI",
        registered_at=_now(),
        trace_id=ctx.trace_id,
    )
    return m.RegisterDisruptionOut(
        disruption_id=row.disruption_id,
        type=row.type,
        registered_at=row.registered_at,
    )


def _now():  # noqa: ANN202
    from datetime import datetime

    return datetime.now()  # noqa: DTZ005


def _disruption_input_from_contract(args: m.RegisterDisruptionIn):  # noqa: ANN202
    """把 `RegisterDisruptionIn`（判别联合载荷）摊平成服务层的 `DisruptionInput`。

    按 `payload.kind` 取该类型的字段，其余留 `None`。`Decimal` 从契约的 `float` 精确构造
    （经 `str` 中转，避免二进制浮点误差进入库/排产算术）。
    """
    from decimal import Decimal

    from app.services.replanning import DisruptionInput
    from app.tools import models as mm

    p = args.payload
    kwargs: dict[str, object] = {"type": args.type, "reported_at": args.reported_at}
    if isinstance(p, mm.MachineBreakdownPayload):
        kwargs.update(
            machine_id=p.machine_id, window_start=p.start_time, window_end=p.end_time
        )
    elif isinstance(p, mm.WorkerUnavailablePayload):
        kwargs.update(
            worker_id=p.worker_id, window_start=p.start_time, window_end=p.end_time
        )
    elif isinstance(p, mm.MaterialShortagePayload):
        kwargs.update(
            material_id=p.material_id,
            available_quantity=Decimal(str(p.available_quantity)),
        )
    elif isinstance(p, mm.MaterialDelayPayload):
        kwargs.update(
            material_id=p.material_id, delivery_id=p.delivery_id, new_eta=p.new_eta
        )
    elif isinstance(p, mm.UrgentOrderPayload):
        kwargs.update(
            product_id=p.product_id,
            quantity=Decimal(str(p.quantity)),
            due_date=p.due_date,
        )
    return DisruptionInput(**kwargs)  # type: ignore[arg-type]


def propose_preference_rule(
    args: m.ProposePreferenceRuleIn, ctx: ToolContext
) -> m.ProposePreferenceRuleOut:
    """提出一条候选偏好规则，**恒 `enabled=False`**（R18.4）。**P0 不接线，仅 P1 使用。**

    输入模型没有 `enabled` 字段，输出 `enabled` 是 `Literal[False]`——「候选规则自动生效」在
    类型层面不可表达（R18.4：只有显式人工确认能启用）。P0 只有手工新建规则的入口，本工具的
    自动蒸馏路线属 P1；此处定义完整契约并留占位，使注册表 26 工具完整、白名单矩阵覆盖到它。
    """
    raise NotImplementedError(
        "propose_preference_rule 是 P1 偏好蒸馏路线（R18）；契约已定义，enabled 恒为 False"
    )


def save_import_batch(args: m.SaveImportBatchIn, ctx: ToolContext) -> m.SaveImportBatchOut:
    """把一批已确认映射的行落成正式记录（R3.2）。委派给 §2 的摄取落库服务。

    落库路径（写目标实体表 + `import_batches` + 逐行 `import_row_provenance`）随摄取流水线
    落地；契约在此完整定义，接线后委派对应服务。
    """
    raise NotImplementedError(
        "save_import_batch 委派 §2 的摄取落库服务；契约已定义"
    )
