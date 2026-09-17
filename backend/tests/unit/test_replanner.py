"""`core/replanner.py` 受影响集、冻结集与锁定作业交互断言（任务 7.1）。

覆盖 R9.5–R9.7 / R11.5 / R6.5，**非可选**（承接原属性 12：冻结集作业逐字段不变；锁定作业
绝不被静默移动）。守着这几件后果很重的事：

1. **`affected_by` 逐类正确 + successor 传播**（R9.5–R9.7、§3.5）：五类扰动的受影响集判定，
   加急订单为空集，前序动了后序连带受影响。
2. **冻结集逐字段不变**（原属性 12 上半）：未受影响且仍可行的作业在重排后 `ScheduledJob`
   逐字段与 `ACTIVE` 计划相同——冻结先于优化，不因「重排能给它更优位置」而移动它。
3. **`LOCKED_JOB_INFEASIBLE` 分支**（原属性 12 下半、R11.5）：被锁且受影响/不可行的作业既不
   冻结也不重排，进 `unschedulable` 发 `LOCKED_JOB_INFEASIBLE`，绝不悄悄换位置。
4. **重排后全量校验**（R6.5）：`ReplanResult.report` 对整个候选计划跑完 9 类硬约束。
5. **无替代机器的明确报告**（R9.5）、**加急订单插入**（R9.6）、**物料类只依据实际库存重排**
   （R9.7）。
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from app.core.replanner import (
    LOCKED_JOB_INFEASIBLE,
    Disruption,
    affected_by,
    job_level_still_valid,
    replan,
)
from app.core.scheduler import PlanCandidate, ScheduledJob
from app.core.snapshot import (
    BomLine,
    DomainSnapshot,
    DowntimeWindow,
    IncomingDelivery,
    Machine,
    Material,
    Operation,
    Order,
    Product,
    TimeWindow,
    Worker,
)

PRODUCTION_DATE = date(2026, 3, 2)
NOW = datetime(2026, 3, 2, 8, 0)
DAY = datetime(2026, 3, 2, 8, 0)
DAY_END = datetime(2026, 3, 2, 18, 0)


# --------------------------------------------------------------------------
# 构造夹具（纯数据，不碰库）
# --------------------------------------------------------------------------


def _op(
    sequence: int,
    *,
    machine_type: str = "CNC",
    capability: str | None = None,
    skill: str = "CNC_OP",
    per_unit: str = "2.0",
    setup: int = 0,
) -> Operation:
    return Operation(
        sequence=sequence,
        required_machine_type=machine_type,
        required_capability=capability,
        required_worker_skill=skill,
        base_processing_time_per_unit=Decimal(per_unit),
        setup_time=setup,
    )


def _product(
    product_id: str = "PRD-01",
    *,
    operations: tuple[Operation, ...] = (),
    bom: tuple[BomLine, ...] = (),
) -> Product:
    return Product(
        product_id=product_id,
        name="支架",
        operations=operations or (_op(1),),
        bom=bom,
    )


def _order(
    order_id: str,
    *,
    product_id: str = "PRD-01",
    quantity: str = "10",
    priority: str = "NORMAL",
    due: datetime | None = None,
) -> Order:
    return Order(
        order_id=order_id,
        product_id=product_id,
        quantity=Decimal(quantity),
        due_date=due or DAY_END,
        promised_date=None,
        priority=priority,  # type: ignore[arg-type]
    )


def _machine(
    machine_id: str = "CNC-01",
    *,
    machine_type: str = "CNC",
    capabilities: tuple[str, ...] = (),
    status: str = "AVAILABLE",
    rate: str = "1.0",
    downtime: tuple[DowntimeWindow, ...] = (),
) -> Machine:
    return Machine(
        machine_id=machine_id,
        machine_type=machine_type,
        capabilities=capabilities,
        status=status,  # type: ignore[arg-type]
        available_start=DAY,
        available_end=DAY_END,
        rate_multiplier=Decimal(rate),
        downtime_windows=downtime,
    )


def _worker(
    worker_id: str = "W-01",
    *,
    skills: tuple[str, ...] = ("CNC_OP",),
    absences: tuple[TimeWindow, ...] = (),
) -> Worker:
    return Worker(
        worker_id=worker_id,
        name="张三",
        skills=skills,
        shift_start=DAY,
        shift_end=DAY_END,
        absences=absences,
    )


def _snapshot(
    *,
    orders: tuple[Order, ...],
    products: tuple[Product, ...],
    machines: tuple[Machine, ...] = (),
    workers: tuple[Worker, ...] = (),
    materials: tuple[Material, ...] = (),
) -> DomainSnapshot:
    return DomainSnapshot(
        snapshot_version=1,
        production_date=PRODUCTION_DATE,
        now=NOW,
        orders=orders,
        products=products,
        materials=materials,
        machines=machines or (_machine(),),
        workers=workers or (_worker(),),
        changeover_rules=(),
        preference_rules=(),
    )


def _sj(
    job_id: str,
    order_id: str,
    *,
    machine_id: str = "CNC-01",
    worker_id: str = "W-01",
    product_id: str = "PRD-01",
    start: datetime = DAY,
    end: datetime | None = None,
) -> ScheduledJob:
    return ScheduledJob(
        job_id=job_id,
        order_id=order_id,
        product_id=product_id,
        machine_id=machine_id,
        worker_id=worker_id,
        start_time=start,
        end_time=end or datetime(2026, 3, 2, 9, 0),
        setup_minutes=0,
        changeover_minutes=0,
    )


def _active(*jobs: ScheduledJob) -> PlanCandidate:
    return PlanCandidate(
        scheduled_jobs=tuple(jobs),
        unschedulable_jobs=(),
        feasibility="FEASIBLE",
    )


# ==========================================================================
# 1. affected_by 逐类 + successor 传播（R9.5–R9.7、§3.5）
# ==========================================================================


def test_machine_breakdown_affects_jobs_in_fault_window() -> None:
    active = _active(
        _sj("ORD-A-OP1", "ORD-A", machine_id="CNC-01", start=DAY, end=datetime(2026, 3, 2, 9, 0)),
        _sj(
            "ORD-B-OP1",
            "ORD-B",
            machine_id="CNC-01",
            start=datetime(2026, 3, 2, 14, 0),
            end=datetime(2026, 3, 2, 15, 0),
        ),
        _sj("ORD-C-OP1", "ORD-C", machine_id="CNC-02"),
    )
    snap = _snapshot(orders=(), products=())
    disruption = Disruption(
        type="MACHINE_BREAKDOWN",
        machine_id="CNC-01",
        fault_window=TimeWindow(start=DAY, end=datetime(2026, 3, 2, 12, 0)),
    )
    affected = affected_by(disruption, active, snap)
    # 只有 08:00–09:00 的 ORD-A 落在 08:00–12:00 故障窗内；ORD-B 在窗外；ORD-C 在别的机器上。
    assert affected == frozenset({"ORD-A-OP1"})


def test_machine_breakdown_without_window_affects_all_jobs_on_machine() -> None:
    active = _active(
        _sj("ORD-A-OP1", "ORD-A", machine_id="CNC-01"),
        _sj("ORD-B-OP1", "ORD-B", machine_id="CNC-02"),
    )
    snap = _snapshot(orders=(), products=())
    disruption = Disruption(type="MACHINE_BREAKDOWN", machine_id="CNC-01")
    assert affected_by(disruption, active, snap) == frozenset({"ORD-A-OP1"})


def test_worker_unavailable_affects_all_that_workers_jobs() -> None:
    active = _active(
        _sj("ORD-A-OP1", "ORD-A", worker_id="W-01"),
        _sj("ORD-B-OP1", "ORD-B", worker_id="W-02"),
        _sj("ORD-C-OP1", "ORD-C", worker_id="W-01"),
    )
    snap = _snapshot(orders=(), products=())
    disruption = Disruption(type="WORKER_UNAVAILABLE", worker_id="W-01")
    assert affected_by(disruption, active, snap) == frozenset({"ORD-A-OP1", "ORD-C-OP1"})


def test_material_shortage_affects_all_operations_of_consuming_orders() -> None:
    product = _product(
        "PRD-STEEL",
        operations=(_op(1), _op(2)),
        bom=(BomLine(material_id="MAT-STEEL", quantity_per_unit=Decimal("1")),),
    )
    other = _product("PRD-ALU", operations=(_op(1),), bom=())
    orders = (
        _order("ORD-A", product_id="PRD-STEEL"),
        _order("ORD-B", product_id="PRD-ALU"),
    )
    snap = _snapshot(orders=orders, products=(product, other))
    active = _active(
        _sj("ORD-A-OP1", "ORD-A", product_id="PRD-STEEL"),
        _sj("ORD-A-OP2", "ORD-A", product_id="PRD-STEEL", start=datetime(2026, 3, 2, 9, 0)),
        _sj("ORD-B-OP1", "ORD-B", product_id="PRD-ALU"),
    )
    disruption = Disruption(type="MATERIAL_SHORTAGE", material_id="MAT-STEEL")
    # ORD-A 的全部工序受影响（消耗 MAT-STEEL）；ORD-B 不消耗它，不受影响。
    assert affected_by(disruption, active, snap) == frozenset({"ORD-A-OP1", "ORD-A-OP2"})


def test_material_delay_uses_same_consumer_logic() -> None:
    product = _product(
        "PRD-STEEL",
        operations=(_op(1),),
        bom=(BomLine(material_id="MAT-STEEL", quantity_per_unit=Decimal("1")),),
    )
    snap = _snapshot(orders=(_order("ORD-A", product_id="PRD-STEEL"),), products=(product,))
    active = _active(_sj("ORD-A-OP1", "ORD-A", product_id="PRD-STEEL"))
    disruption = Disruption(type="MATERIAL_DELAY", material_id="MAT-STEEL")
    assert affected_by(disruption, active, snap) == frozenset({"ORD-A-OP1"})


def test_urgent_order_affects_nothing() -> None:
    active = _active(_sj("ORD-A-OP1", "ORD-A"), _sj("ORD-B-OP1", "ORD-B"))
    snap = _snapshot(orders=(), products=())
    disruption = Disruption(type="URGENT_ORDER")
    assert affected_by(disruption, active, snap) == frozenset()


def test_successor_operations_are_pulled_into_affected_set() -> None:
    # 三道工序，OP2 在故障机上受影响 → OP3（后序）连带受影响；OP1（前序）不因传播被牵连。
    active = _active(
        _sj("ORD-A-OP1", "ORD-A", machine_id="CNC-02", start=DAY, end=datetime(2026, 3, 2, 9, 0)),
        _sj(
            "ORD-A-OP2",
            "ORD-A",
            machine_id="CNC-01",
            start=datetime(2026, 3, 2, 9, 0),
            end=datetime(2026, 3, 2, 10, 0),
        ),
        _sj(
            "ORD-A-OP3",
            "ORD-A",
            machine_id="CNC-02",
            start=datetime(2026, 3, 2, 10, 0),
            end=datetime(2026, 3, 2, 11, 0),
        ),
    )
    snap = _snapshot(orders=(), products=())
    disruption = Disruption(
        type="MACHINE_BREAKDOWN",
        machine_id="CNC-01",
        fault_window=TimeWindow(start=DAY, end=DAY_END),
    )
    affected = affected_by(disruption, active, snap)
    assert "ORD-A-OP2" in affected  # 直接受影响
    assert "ORD-A-OP3" in affected  # 后序传播
    assert "ORD-A-OP1" not in affected  # 前序不被传播牵连


# ==========================================================================
# 2. job_level_still_valid（§3.5「资源仍可用、物料仍够」）
# ==========================================================================


def test_job_valid_when_resources_available() -> None:
    snap = _snapshot(orders=(_order("ORD-A"),), products=(_product(),))
    sj = _sj("ORD-A-OP1", "ORD-A")
    assert job_level_still_valid(sj, snap) is True


def test_job_invalid_when_machine_down() -> None:
    snap = _snapshot(
        orders=(_order("ORD-A"),),
        products=(_product(),),
        machines=(_machine("CNC-01", status="DOWN"),),
    )
    assert job_level_still_valid(_sj("ORD-A-OP1", "ORD-A"), snap) is False


def test_job_invalid_when_worker_absent_over_occupation() -> None:
    snap = _snapshot(
        orders=(_order("ORD-A"),),
        products=(_product(),),
        workers=(_worker("W-01", absences=(TimeWindow(start=DAY, end=DAY_END),)),),
    )
    assert job_level_still_valid(_sj("ORD-A-OP1", "ORD-A"), snap) is False


def test_job_invalid_when_material_no_longer_sufficient() -> None:
    material = Material(
        material_id="MAT-01",
        name="钢",
        unit="kg",
        quantity_available=Decimal("1"),  # 需 10（qty 10 × 1/件），仅 1 → 不足
        reserved_quantity=Decimal("0"),
        incoming_deliveries=(),
    )
    product = _product(bom=(BomLine(material_id="MAT-01", quantity_per_unit=Decimal("1")),))
    snap = _snapshot(
        orders=(_order("ORD-A", quantity="10"),),
        products=(product,),
        materials=(material,),
    )
    assert job_level_still_valid(_sj("ORD-A-OP1", "ORD-A"), snap) is False


# ==========================================================================
# 3. 冻结集逐字段不变（原属性 12 上半，冻结先于优化）
# ==========================================================================


def test_unaffected_jobs_are_frozen_field_by_field() -> None:
    # ORD-A 在 CNC-02 上、不受 CNC-01 故障影响 → 冻结，重排后逐字段不变。
    frozen_job = _sj(
        "ORD-A-OP1",
        "ORD-A",
        machine_id="CNC-02",
        start=DAY,
        end=datetime(2026, 3, 2, 9, 0),
    )
    active = _active(frozen_job)
    snap = _snapshot(
        orders=(_order("ORD-A"),),
        products=(_product(),),
        machines=(_machine("CNC-01"), _machine("CNC-02")),
    )
    disruption = Disruption(type="MACHINE_BREAKDOWN", machine_id="CNC-01")
    result = replan(active, disruption, snap)

    assert result.affected_job_ids == ()
    assert result.frozen_job_ids == ("ORD-A-OP1",)
    # 冻结集逐字段不变：重排结果里的这条与原 ACTIVE 计划里的这条完全相同。
    replanned = {sj.job_id: sj for sj in result.candidate.scheduled_jobs}
    assert replanned["ORD-A-OP1"] == frozen_job


def test_freeze_precedes_optimization_does_not_move_frozen_job() -> None:
    # 有一台更快的替代机（rate 2.0）能让 ORD-A 更早完工，但它未受影响 → 冻结优先，不搬到快机。
    frozen_job = _sj(
        "ORD-A-OP1",
        "ORD-A",
        machine_id="CNC-SLOW",
        start=datetime(2026, 3, 2, 12, 0),
        end=datetime(2026, 3, 2, 13, 0),
    )
    active = _active(frozen_job)
    snap = _snapshot(
        orders=(_order("ORD-A"),),
        products=(_product(),),
        machines=(_machine("CNC-FAST", rate="2.0"), _machine("CNC-SLOW", rate="1.0")),
    )
    # 一个不触及 ORD-A 的扰动（别的机器故障）；ORD-A 应原样冻结，不被优化搬走。
    disruption = Disruption(type="MACHINE_BREAKDOWN", machine_id="CNC-OTHER")
    result = replan(active, disruption, snap)
    replanned = {sj.job_id: sj for sj in result.candidate.scheduled_jobs}
    assert replanned["ORD-A-OP1"] == frozen_job  # 仍在慢机、仍在原时刻


# ==========================================================================
# 4. LOCKED_JOB_INFEASIBLE 分支（原属性 12 下半、R11.5）
# ==========================================================================


def test_locked_job_affected_goes_unschedulable_not_moved() -> None:
    # ORD-A 被锁，且落在故障机 CNC-01 上受影响 → 既不冻结也不重排，进 unschedulable。
    locked_job = _sj("ORD-A-OP1", "ORD-A", machine_id="CNC-01")
    active = _active(locked_job)
    snap = _snapshot(
        orders=(_order("ORD-A"),),
        products=(_product(),),
        machines=(_machine("CNC-01"), _machine("CNC-02")),
    )
    disruption = Disruption(type="MACHINE_BREAKDOWN", machine_id="CNC-01")
    result = replan(active, disruption, snap, locked_job_ids=frozenset({"ORD-A-OP1"}))

    # 绝不悄悄移动：被锁作业不出现在 scheduled_jobs 里。
    assert "ORD-A-OP1" not in {sj.job_id for sj in result.candidate.scheduled_jobs}
    assert result.locked_infeasible_job_ids == ("ORD-A-OP1",)
    unsched = {u.job_id: u for u in result.candidate.unschedulable_jobs}
    assert unsched["ORD-A-OP1"].blocking_reason == LOCKED_JOB_INFEASIBLE
    assert unsched["ORD-A-OP1"].unblock_suggestion["awaiting_unlock"] is True


def test_locked_job_still_valid_is_force_frozen() -> None:
    # ORD-A 被锁但未受影响、仍可行 → 强制冻结，原样保留。
    locked_job = _sj("ORD-A-OP1", "ORD-A", machine_id="CNC-02")
    active = _active(locked_job)
    snap = _snapshot(
        orders=(_order("ORD-A"),),
        products=(_product(),),
        machines=(_machine("CNC-01"), _machine("CNC-02")),
    )
    disruption = Disruption(type="MACHINE_BREAKDOWN", machine_id="CNC-01")
    result = replan(active, disruption, snap, locked_job_ids=frozenset({"ORD-A-OP1"}))

    assert result.locked_infeasible_job_ids == ()
    assert "ORD-A-OP1" in result.frozen_job_ids
    replanned = {sj.job_id: sj for sj in result.candidate.scheduled_jobs}
    assert replanned["ORD-A-OP1"] == locked_job


def test_locked_infeasible_excludes_whole_order_from_replan() -> None:
    # 多工序订单被锁其一并受影响 → 整单硬排除，全部工序进 unschedulable（LOCKED_JOB_INFEASIBLE）。
    active = _active(
        _sj("ORD-A-OP1", "ORD-A", machine_id="CNC-01", start=DAY, end=datetime(2026, 3, 2, 9, 0)),
        _sj(
            "ORD-A-OP2",
            "ORD-A",
            machine_id="CNC-01",
            start=datetime(2026, 3, 2, 9, 0),
            end=datetime(2026, 3, 2, 10, 0),
        ),
    )
    snap = _snapshot(
        orders=(_order("ORD-A", product_id="PRD-01"),),
        products=(_product(operations=(_op(1), _op(2))),),
        machines=(_machine("CNC-01"), _machine("CNC-02")),
    )
    disruption = Disruption(type="MACHINE_BREAKDOWN", machine_id="CNC-01")
    result = replan(active, disruption, snap, locked_job_ids=frozenset({"ORD-A-OP1"}))

    unsched = {u.job_id: u for u in result.candidate.unschedulable_jobs}
    assert unsched.keys() >= {"ORD-A-OP1", "ORD-A-OP2"}
    assert all(
        unsched[j].blocking_reason == LOCKED_JOB_INFEASIBLE for j in ("ORD-A-OP1", "ORD-A-OP2")
    )
    assert result.candidate.scheduled_jobs == ()


# ==========================================================================
# 5. 重排后全量校验（R6.5）
# ==========================================================================


def test_replan_runs_full_validation_and_is_feasible() -> None:
    active = _active(_sj("ORD-A-OP1", "ORD-A", machine_id="CNC-02"))
    snap = _snapshot(
        orders=(_order("ORD-A"),),
        products=(_product(),),
        machines=(_machine("CNC-01"), _machine("CNC-02")),
    )
    disruption = Disruption(type="MACHINE_BREAKDOWN", machine_id="CNC-01")
    result = replan(active, disruption, snap)
    assert result.report.is_feasible is True
    assert result.report.violations == ()


def test_replan_is_deterministic() -> None:
    active = _active(_sj("ORD-A-OP1", "ORD-A", machine_id="CNC-02"))
    snap = _snapshot(
        orders=(_order("ORD-A"),),
        products=(_product(),),
        machines=(_machine("CNC-01"), _machine("CNC-02")),
    )
    disruption = Disruption(type="MACHINE_BREAKDOWN", machine_id="CNC-01")
    r1 = replan(active, disruption, snap)
    r2 = replan(active, disruption, snap)
    assert r1 == r2


# ==========================================================================
# 6. MACHINE_BREAKDOWN 替代机器（R9.5）
# ==========================================================================


def test_machine_breakdown_reschedules_to_substitute_machine() -> None:
    # ORD-A 原在 CNC-01（现故障）；存在具备能力的替代机 CNC-02 → 重排到 CNC-02。
    active = _active(_sj("ORD-A-OP1", "ORD-A", machine_id="CNC-01"))
    snap = _snapshot(
        orders=(_order("ORD-A"),),
        products=(_product(),),
        machines=(_machine("CNC-01"), _machine("CNC-02")),
    )
    disruption = Disruption(type="MACHINE_BREAKDOWN", machine_id="CNC-01")
    result = replan(active, disruption, snap)

    replanned = {sj.job_id: sj for sj in result.candidate.scheduled_jobs}
    assert "ORD-A-OP1" in replanned
    assert replanned["ORD-A-OP1"].machine_id == "CNC-02"  # 换到替代机
    assert result.substitute_unavailable_job_ids == ()  # 找到了替代机


def test_machine_breakdown_reports_when_no_substitute_available() -> None:
    # 唯一能做这道工序的机器 CNC-01 故障，无替代 → 明确报告无替代机器。
    active = _active(_sj("ORD-A-OP1", "ORD-A", machine_id="CNC-01"))
    snap = _snapshot(
        orders=(_order("ORD-A"),),
        products=(_product(),),
        machines=(_machine("CNC-01"),),  # 只有这一台
    )
    disruption = Disruption(type="MACHINE_BREAKDOWN", machine_id="CNC-01")
    result = replan(active, disruption, snap)

    assert result.substitute_unavailable_job_ids == ("ORD-A-OP1",)
    unsched_ids = {u.job_id for u in result.candidate.unschedulable_jobs}
    assert "ORD-A-OP1" in unsched_ids


# ==========================================================================
# 7. URGENT_ORDER 插入与被推迟作业（R9.6）
# ==========================================================================


def test_urgent_order_insertion_reschedules_new_order() -> None:
    # ACTIVE 只有 ORD-EXISTING；snapshot 里额外有一个新加急订单 ORD-URGENT。
    # URGENT_ORDER 扰动下既有作业不受影响（冻结），新订单由排产器排入。
    existing = _sj(
        "ORD-EXISTING-OP1",
        "ORD-EXISTING",
        machine_id="CNC-01",
        start=DAY,
        end=datetime(2026, 3, 2, 9, 0),
    )
    active = _active(existing)
    orders = (
        _order("ORD-EXISTING", priority="NORMAL"),
        _order("ORD-URGENT", priority="URGENT"),
    )
    snap = _snapshot(orders=orders, products=(_product(),), machines=(_machine("CNC-01"),))
    disruption = Disruption(type="URGENT_ORDER")
    result = replan(active, disruption, snap)

    assert result.affected_job_ids == ()
    scheduled_orders = {sj.order_id for sj in result.candidate.scheduled_jobs}
    # 既有作业冻结保留 + 新加急订单被排入。
    assert "ORD-EXISTING" in scheduled_orders
    assert "ORD-URGENT" in scheduled_orders
    assert result.report.is_feasible is True


# ==========================================================================
# 8. 物料类扰动只依据实际库存重排（R9.7）
# ==========================================================================


def test_material_shortage_reschedules_only_on_actual_inventory() -> None:
    # 快照已反映扰动后的库存：MAT-01 只剩 1，需 10 → 消耗它的订单无法重排（不假设补料）。
    material = Material(
        material_id="MAT-01",
        name="钢",
        unit="kg",
        quantity_available=Decimal("1"),
        reserved_quantity=Decimal("0"),
        incoming_deliveries=(),
    )
    product = _product(bom=(BomLine(material_id="MAT-01", quantity_per_unit=Decimal("1")),))
    snap = _snapshot(
        orders=(_order("ORD-A", quantity="10"),),
        products=(product,),
        materials=(material,),
    )
    active = _active(_sj("ORD-A-OP1", "ORD-A"))
    disruption = Disruption(type="MATERIAL_SHORTAGE", material_id="MAT-01")
    result = replan(active, disruption, snap)

    # 受影响 → 进重排；实际库存不足 → 进 unschedulable（缺料），不虚构补料。
    assert result.affected_job_ids == ("ORD-A-OP1",)
    assert result.frozen_job_ids == ()
    unsched = {u.job_id: u for u in result.candidate.unschedulable_jobs}
    assert unsched["ORD-A-OP1"].blocking_reason == "MATERIAL_INSUFFICIENT"


def test_material_delivery_lets_order_reschedule_after_eta() -> None:
    # MAT-01 当前不足，但有一批 09:00 到货 → 订单可在 09:01 后重排（依据实际到货时间）。
    material = Material(
        material_id="MAT-01",
        name="钢",
        unit="kg",
        quantity_available=Decimal("0"),
        reserved_quantity=Decimal("0"),
        incoming_deliveries=(
            IncomingDelivery(
                delivery_id="D-01",
                quantity=Decimal("100"),
                eta=datetime(2026, 3, 2, 9, 0),
                confirmed=True,
            ),
        ),
    )
    product = _product(bom=(BomLine(material_id="MAT-01", quantity_per_unit=Decimal("1")),))
    snap = _snapshot(
        orders=(_order("ORD-A", quantity="10"),),
        products=(product,),
        materials=(material,),
    )
    active = _active(_sj("ORD-A-OP1", "ORD-A"))
    disruption = Disruption(type="MATERIAL_DELAY", material_id="MAT-01")
    result = replan(active, disruption, snap)

    replanned = {sj.job_id: sj for sj in result.candidate.scheduled_jobs}
    assert "ORD-A-OP1" in replanned
    # 依据实际到货：开工时刻严格晚于 09:00 ETA。
    assert replanned["ORD-A-OP1"].start_time >= datetime(2026, 3, 2, 9, 1)
    assert result.report.is_feasible is True


# ==========================================================================
# 9. Disruption 模型辅助
# ==========================================================================


def test_disruption_excluded_machine_ids() -> None:
    breakdown = Disruption(type="MACHINE_BREAKDOWN", machine_id="CNC-01")
    assert breakdown.excluded_machine_ids() == frozenset({"CNC-01"})
    urgent = Disruption(type="URGENT_ORDER")
    assert urgent.excluded_machine_ids() == frozenset()
