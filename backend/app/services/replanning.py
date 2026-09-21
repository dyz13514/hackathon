"""扰动登记 + 计划态读回 + 确定性重排编排（任务 7.4，R9、R13、design.md §3.5 / §2.6）。

本模块是 Task 7.4 的服务层，把三样东西聚在一处，都是**确定性**的（无 LLM）：

1. **计划态读回**（`load_plan_candidate` / `plan_row_metrics`）：把持久化的 `scheduled_jobs`
   （+ `production_jobs` join）重建成内核的 `PlanCandidate`，并算出 `Autonomy_Policy_Engine`
   需要的 `ActivePlanRow` / `CandPlanRow` 关键度量。此前这段重建逻辑重复在
   `api/plans.py._detail_from_db` 与 `services/approval.py._candidate_from_plan` 两处；本模块
   给出 §7 明确要求的「计划态读回」单点，供重排流水线与工具 handler 共用。**不改动**那两处
   既有实现（它们各有自己的输出形状），只在这里提供内核值对象口径的读回。

2. **扰动登记**（`register_disruption`）：写 `disruptions` 行，`MACHINE_BREAKDOWN` /
   `WORKER_UNAVAILABLE` 同时写 `machine_downtime` / `worker_absences` 窗口并回指该扰动
   （R9.1）。无 `ACTIVE` 计划 → `NoActivePlanError`（R9.8）。把 API 边界的
   `RegisterDisruptionIn`（判别联合载荷）与 DB `disruptions` 行都映射到内核的 `Disruption`
   值对象（`app.core.replanner.Disruption`，只承载 `affected_by` / `replan` 需要的字段）。

3. **`DomainSnapshot` 版本推进**：写入停机/缺勤窗后，`db/events.py` 的 `after_flush` 钩子
   监听 `machine_downtime` / `worker_absences`（`PLANNING_RELEVANT_TABLES`），使
   `input_snapshot_version` 自动推进——重排读到的快照因此已反映扰动后的状态（机器停机、
   工人缺勤、物料库存/ETA 更新）。这与 R9.7「只依据实际库存与到货时间重排」一致。

## 为什么扰动登记先落库、再由重排读快照

design.md §3.5 的 `replan` 要求快照**已反映扰动后的状态**。因此顺序是：登记扰动
→ 写停机/缺勤窗（推进快照版本）→ `load_snapshot` 读到含这些窗口的新快照 → `replan`。
物料类扰动（`MATERIAL_SHORTAGE` / `MATERIAL_DELAY`）改的是物料表本身，其登记副作用由
调用方在同一事务里施加（本模块提供的 `apply_material_effect`），同样在读快照之前完成。

## 分层

本模块住在 `app/services/`，可以 import `sqlalchemy` 与 `app.core`。它**不** import
`app.agents` / `app.llm`——重排的全部数值都来自确定性内核，LLM 只在 ReAct 路径上生成
`revision_summary` 叙述（任务 7.4 的 Planning_Agent 驱动），绝不进入本模块。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.autonomy import ActivePlanRow, CandPlanRow
from app.core.replanner import Disruption as KernelDisruption
from app.core.scheduler import PlanCandidate, ScheduledJob
from app.core.snapshot import TimeWindow
from app.db import models as orm

#: 订单优先级 → 秩（与 `scheduler.PRIORITY_RANK` 同口径）。`ActivePlanRow.job_priority_map`
#: 用它把「变更作业是否涉及 URGENT/HIGH」判成一个 int 比较（`rank <= 1`）。
PRIORITY_RANK: dict[str, int] = {"URGENT": 0, "HIGH": 1, "NORMAL": 2, "LOW": 3}

#: 停机原因：扰动登记写入的是真实故障（区别于 seed 里的计划保养 `MAINTENANCE`）。
DOWNTIME_REASON_BREAKDOWN = "BREAKDOWN"

ACTIVE_STATUS = "ACTIVE"


class NoActivePlanError(Exception):
    """登记扰动时不存在 `ACTIVE` 计划（R9.8 → `NO_ACTIVE_PLAN`）。

    扰动是「对当前生效计划的干扰」；没有生效计划就无从谈影响与重排。API 边界把它翻译成
    `NO_ACTIVE_PLAN` 错误码并给「先生成并激活一个计划」的下一步。
    """


class DisruptionNotFoundError(Exception):
    """按 `disruption_id` 找不到扰动（`GET /disruptions/{id}/impact`）。"""

    def __init__(self, disruption_id: str) -> None:
        self.disruption_id = disruption_id
        super().__init__(f"扰动 {disruption_id} 不存在")


# --------------------------------------------------------------------------
# 计划态读回（§7「计划态读回/写回」的读一侧）
# --------------------------------------------------------------------------


def load_plan_candidate(session: Session, plan_id: str) -> PlanCandidate:
    """从库里的 `scheduled_jobs`（+ `production_jobs` join）重建 `PlanCandidate`。

    与 `approval._candidate_from_plan` 同口径：只重建**已排产**部分，`unschedulable_jobs`
    置空（它们没排进计划，重排的受影响集/冻结集只看 `scheduled_jobs`）。`order_id` /
    `product_id` 从 `production_jobs` 取（`scheduled_jobs` 表不带它们）。`feasibility` 取计划头
    存的值。

    计划不存在（无任何 `scheduled_jobs` 行且计划头缺失）时返回一个空的 `FEASIBLE` 候选是
    错误的——调用方应先确认计划存在。这里若无行则返回空 `scheduled_jobs`；调用方（重排
    编排）在此之前已用 `require_active_plan` 拿到真实的 ACTIVE 计划头。
    """
    plan = session.get(orm.ProductionPlan, plan_id)
    feasibility = plan.feasibility if plan is not None else "FEASIBLE"

    rows = list(
        session.execute(
            select(orm.ScheduledJob, orm.ProductionJob)
            .join(orm.ProductionJob, orm.ScheduledJob.job_id == orm.ProductionJob.job_id)
            .where(orm.ScheduledJob.plan_id == plan_id)
            .order_by(orm.ScheduledJob.job_id)
        )
    )
    scheduled = tuple(
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
    return PlanCandidate(
        scheduled_jobs=scheduled,
        unschedulable_jobs=(),
        feasibility=feasibility,  # type: ignore[arg-type]  # 取值域由写入侧保证
    )


def locked_job_ids(session: Session, plan_id: str) -> frozenset[str]:
    """计划里已被 `LOCK_JOB` 锁定的作业 ID（供 `replan` 的 `locked_job_ids` 入参）。"""
    return frozenset(
        session.execute(
            select(orm.ScheduledJob.job_id).where(
                orm.ScheduledJob.plan_id == plan_id,
                orm.ScheduledJob.locked.is_(True),
            )
        ).scalars()
    )


def require_active_plan(session: Session, production_date: date) -> orm.ProductionPlan:
    """取该生产日的 `ACTIVE` 计划头，不存在抛 `NoActivePlanError`（R9.8）。

    `ux_active_per_day` 部分唯一索引保证任一生产日至多一个 `ACTIVE`，因此这里取首条即唯一。
    """
    plan = session.execute(
        select(orm.ProductionPlan).where(
            orm.ProductionPlan.production_date == production_date,
            orm.ProductionPlan.status == ACTIVE_STATUS,
        )
    ).scalars().first()
    if plan is None:
        raise NoActivePlanError(f"No ACTIVE plan for production date {production_date.isoformat()}")
    return plan


def require_any_active_plan(session: Session) -> orm.ProductionPlan:
    """取任一 `ACTIVE` 计划头（P0 演示单生产日；不存在抛 `NoActivePlanError`）。

    扰动登记的 `POST /disruptions` 不带生产日——它作用于「当前生效的计划」。P0 演示只有一个
    生产日，因此取任一 `ACTIVE` 即可；多生产日的选择留给后续。
    """
    plan = session.execute(
        select(orm.ProductionPlan).where(orm.ProductionPlan.status == ACTIVE_STATUS)
    ).scalars().first()
    if plan is None:
        raise NoActivePlanError("There is no ACTIVE plan; cannot register a disruption")
    return plan


# --------------------------------------------------------------------------
# 计划关键度量（ImpactInput.from_delta 的入参）
# --------------------------------------------------------------------------


def _order_priority_ranks(session: Session, plan_id: str) -> dict[str, int]:
    """`{job_id: priority_rank}`——把计划里每个作业所属订单的优先级映射成秩。

    `_any_changed_job_touches_high_priority` 据它判断变更集是否涉及 URGENT/HIGH（rank ≤ 1）。
    从 `scheduled_jobs → production_jobs → orders` join 取每个作业的订单优先级。
    """
    rows = session.execute(
        select(orm.ScheduledJob.job_id, orm.Order.priority)
        .join(orm.ProductionJob, orm.ScheduledJob.job_id == orm.ProductionJob.job_id)
        .join(orm.Order, orm.ProductionJob.order_id == orm.Order.order_id)
        .where(orm.ScheduledJob.plan_id == plan_id)
    ).all()
    return {job_id: PRIORITY_RANK.get(str(priority), 2) for job_id, priority in rows}


def _job_time_maps(
    candidate: PlanCandidate, shift_end_by_worker: dict[str, int]
) -> tuple[dict[str, str], dict[str, str], dict[str, int], dict[str, int]]:
    """从一份候选计划算出 `from_delta` 需要的四个 {job_id: ...} 映射。

    `job_shift_end_ts` 用作业所属工人的 `shift_end`（POSIX 秒）；`job_end_ts` 用作业自身的
    `end_time`（POSIX 秒）。`_all_within_same_machine_and_shift` 据这两者判断 moved 作业是否
    跨班次。
    """
    machine_map: dict[str, str] = {}
    worker_map: dict[str, str] = {}
    shift_end_ts: dict[str, int] = {}
    end_ts: dict[str, int] = {}
    for sj in candidate.scheduled_jobs:
        machine_map[sj.job_id] = sj.machine_id
        worker_map[sj.job_id] = sj.worker_id
        end_ts[sj.job_id] = int(sj.end_time.timestamp())
        shift = shift_end_by_worker.get(sj.worker_id)
        if shift is not None:
            shift_end_ts[sj.job_id] = shift
    return machine_map, worker_map, shift_end_ts, end_ts


def _worker_shift_end_ts(session: Session) -> dict[str, int]:
    """`{worker_id: shift_end POSIX 秒}`，供 `_job_time_maps` 判跨班次。"""
    rows = session.execute(select(orm.Worker.worker_id, orm.Worker.shift_end)).all()
    return {worker_id: int(shift_end.timestamp()) for worker_id, shift_end in rows}


def active_plan_row(
    session: Session,
    plan_id: str,
    candidate: PlanCandidate,
    *,
    total_tardiness_minutes: int,
    unschedulable_count: int,
) -> ActivePlanRow:
    """构造 `ActivePlanRow`（`ImpactInput.from_delta` 的 active 入参）。

    全部字段是数值/映射，无自由文本——保持 `Autonomy_Policy_Engine` 的「无字符串注入点」
    不变量（design.md §3.6、ADR-010）。
    """
    shift_end_by_worker = _worker_shift_end_ts(session)
    machine_map, worker_map, shift_end_ts, end_ts = _job_time_maps(candidate, shift_end_by_worker)
    return ActivePlanRow(
        total_tardiness_minutes=total_tardiness_minutes,
        unschedulable_count=unschedulable_count,
        job_priority_map=_order_priority_ranks(session, plan_id),
        job_machine_map=machine_map,
        job_worker_map=worker_map,
        job_shift_end_ts=shift_end_ts,
        job_end_ts=end_ts,
    )


def cand_plan_row(
    session: Session,
    candidate: PlanCandidate,
    *,
    total_tardiness_minutes: int,
    unschedulable_count: int,
    promised_date_changed: bool,
) -> CandPlanRow:
    """构造 `CandPlanRow`（`ImpactInput.from_delta` 的 cand 入参）。

    `promised_date_changed` 由调用方判定——重排绝不改变任何订单的 `promised_date`（那是对
    客户的承诺），因此 P0 恒为 `False`；保留参数是为了与 `from_delta` 契约一致。
    """
    shift_end_by_worker = _worker_shift_end_ts(session)
    machine_map, worker_map, shift_end_ts, end_ts = _job_time_maps(candidate, shift_end_by_worker)
    return CandPlanRow(
        total_tardiness_minutes=total_tardiness_minutes,
        unschedulable_count=unschedulable_count,
        promised_date_changed=promised_date_changed,
        job_machine_map=machine_map,
        job_worker_map=worker_map,
        job_shift_end_ts=shift_end_ts,
        job_end_ts=end_ts,
    )


# --------------------------------------------------------------------------
# API 载荷 → 内核 Disruption 的映射
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class DisruptionInput:
    """API/服务边界的扰动登记入参（`RegisterDisruptionIn` 的服务层等价物）。

    单独一个值对象而非直接吃 `RegisterDisruptionIn`：`app.services` 不该反向依赖
    `app.tools.models` 的契约类型（那是给 LLM 看的接口）。API 端点把 `RegisterDisruptionIn`
    的判别联合载荷摊平成这里的可选字段，服务层据 `type` 取用。
    """

    type: str
    reported_at: datetime
    # MACHINE_BREAKDOWN / WORKER_UNAVAILABLE
    machine_id: str | None = None
    worker_id: str | None = None
    window_start: datetime | None = None
    window_end: datetime | None = None
    # MATERIAL_SHORTAGE / MATERIAL_DELAY
    material_id: str | None = None
    available_quantity: Decimal | None = None
    delivery_id: str | None = None
    new_eta: datetime | None = None
    # URGENT_ORDER
    product_id: str | None = None
    quantity: Decimal | None = None
    due_date: date | None = None


def to_kernel_disruption(payload: DisruptionInput) -> KernelDisruption:
    """把 `DisruptionInput` 映射成内核 `Disruption`（`affected_by` / `replan` 的入参）。

    内核 `Disruption` 只承载受影响集计算需要的字段。物料类扰动的库存/ETA 变化不进内核模型
    ——它们改的是快照本身（`apply_material_effect` 在登记时写库），`affected_by` 靠
    `material_id` 找到消耗该物料的订单。`URGENT_ORDER` 无额外字段（新订单进快照即可）。
    """
    if payload.type == "MACHINE_BREAKDOWN":
        window = (
            TimeWindow(start=payload.window_start, end=payload.window_end)
            if payload.window_start is not None and payload.window_end is not None
            else None
        )
        return KernelDisruption(
            type="MACHINE_BREAKDOWN", machine_id=payload.machine_id, fault_window=window
        )
    if payload.type == "WORKER_UNAVAILABLE":
        return KernelDisruption(type="WORKER_UNAVAILABLE", worker_id=payload.worker_id)
    if payload.type in ("MATERIAL_SHORTAGE", "MATERIAL_DELAY"):
        return KernelDisruption(type=payload.type, material_id=payload.material_id)  # type: ignore[arg-type]
    # URGENT_ORDER
    return KernelDisruption(type="URGENT_ORDER")


# --------------------------------------------------------------------------
# 扰动登记（写 disruptions + 停机/缺勤窗 + 物料副作用）
# --------------------------------------------------------------------------


def _new_disruption_id() -> str:
    return f"DSR-{uuid.uuid4().hex[:12]}"


def _new_row_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def register_disruption(
    session: Session,
    payload: DisruptionInput,
    *,
    active_plan_id: str,
    source: str,
    registered_at: datetime,
    trace_id: str | None = None,
) -> orm.Disruption:
    """写一行 `disruptions`，并按类型施加登记副作用（R9.1、R9.9）。不提交——调用方持有事务。

    副作用：
    - `MACHINE_BREAKDOWN` → 写一行 `machine_downtime`（`reason = BREAKDOWN`，回指该扰动）；
    - `WORKER_UNAVAILABLE` → 写一行 `worker_absences`（回指该扰动）；
    - `MATERIAL_SHORTAGE` → 直接把该物料的 `quantity_available` 改为登记值（R9.7：只依据
      实际库存重排）；
    - `MATERIAL_DELAY` → 把对应在途到货的 `eta` 改为新 ETA；
    - `URGENT_ORDER` → P0 不在此新增订单行（新订单的落库属摄取/手工录入路径）；登记扰动
      本身记录该诉求，重排读快照时若该订单已在库即被排入。

    这些写入触发 `db/events.py` 的版本推进钩子（`machine_downtime` / `worker_absences` /
    `materials` / `incoming_deliveries` 都在 `PLANNING_RELEVANT_TABLES` 里），使随后的
    `load_snapshot` 读到已反映扰动的快照。
    """
    disruption_id = _new_disruption_id()
    row = orm.Disruption(
        disruption_id=disruption_id,
        type=payload.type,
        payload=_payload_json(payload),
        reported_at=payload.reported_at,
        registered_at=registered_at,
        source=source,
        active_plan_id=active_plan_id,
        trace_id=trace_id,
    )
    session.add(row)
    session.flush()  # 让停机/缺勤窗的 disruption_id 外键有指向

    if payload.type == "MACHINE_BREAKDOWN" and payload.machine_id is not None:
        session.add(
            orm.MachineDowntime(
                downtime_id=_new_row_id("DT"),
                machine_id=payload.machine_id,
                start_time=_require(payload.window_start, "window_start"),
                end_time=_require(payload.window_end, "window_end"),
                reason=DOWNTIME_REASON_BREAKDOWN,
                disruption_id=disruption_id,
            )
        )
    elif payload.type == "WORKER_UNAVAILABLE" and payload.worker_id is not None:
        session.add(
            orm.WorkerAbsence(
                absence_id=_new_row_id("AB"),
                worker_id=payload.worker_id,
                start_time=_require(payload.window_start, "window_start"),
                end_time=_require(payload.window_end, "window_end"),
                disruption_id=disruption_id,
            )
        )
    elif payload.type == "MATERIAL_SHORTAGE" and payload.material_id is not None:
        _apply_material_shortage(session, payload)
    elif payload.type == "MATERIAL_DELAY" and payload.material_id is not None:
        _apply_material_delay(session, payload)

    session.flush()
    return row


def _apply_material_shortage(session: Session, payload: DisruptionInput) -> None:
    """把物料可用量改为登记值（R9.7）。物料不存在则无副作用——受影响集仍按 material_id 计算。"""
    material = session.get(orm.Material, payload.material_id)
    if material is not None and payload.available_quantity is not None:
        material.quantity_available = payload.available_quantity


def _apply_material_delay(session: Session, payload: DisruptionInput) -> None:
    """把指定在途到货的 ETA 改为新值（R9.7：按到货时间重排）。"""
    if payload.delivery_id is None or payload.new_eta is None:
        return
    delivery = session.get(orm.IncomingDelivery, payload.delivery_id)
    if delivery is not None:
        delivery.eta = payload.new_eta


def _payload_json(payload: DisruptionInput) -> dict[str, object]:
    """把登记入参落成 `disruptions.payload` 的结构化 JSON（R9.9：登记内容可回溯）。

    只存该类型用到的字段，`Decimal` / `datetime` / `date` 转成可序列化形态。
    """
    data: dict[str, object] = {"type": payload.type}
    if payload.machine_id is not None:
        data["machine_id"] = payload.machine_id
    if payload.worker_id is not None:
        data["worker_id"] = payload.worker_id
    if payload.window_start is not None:
        data["window_start"] = payload.window_start.isoformat()
    if payload.window_end is not None:
        data["window_end"] = payload.window_end.isoformat()
    if payload.material_id is not None:
        data["material_id"] = payload.material_id
    if payload.available_quantity is not None:
        data["available_quantity"] = str(payload.available_quantity)
    if payload.delivery_id is not None:
        data["delivery_id"] = payload.delivery_id
    if payload.new_eta is not None:
        data["new_eta"] = payload.new_eta.isoformat()
    if payload.product_id is not None:
        data["product_id"] = payload.product_id
    if payload.quantity is not None:
        data["quantity"] = str(payload.quantity)
    if payload.due_date is not None:
        data["due_date"] = payload.due_date.isoformat()
    return data


def _require(value: datetime | None, name: str) -> datetime:
    if value is None:
        raise ValueError(f"{name} 不能为空")
    return value
