"""`core/validation.py` 的分支覆盖单元测试（任务 2.8，R6.1–R6.6）。

design.md §3.2 把 `Constraint_Validator` 定为**唯一判定可行性的组件**，且**与 `Scheduling_Core`
独立实现**。本套测试守的正是那条独立性的价值：每一类违反都用一个**手工构造的坏计划**
（不经排产器产出，因此排产器的 bug 不会掩盖校验器的 bug）触发，并配一个「刚好不违反」的
边界证明检查不误报。

覆盖策略（对齐任务 2.13「9 类违反各一个最小复现 + 每类『刚好不违反』边界」）：

1. 9 类违反各一个最小复现；
2. 每类一个「刚好不违反」边界（半开区间端点相接、eta 恰好早于开工一分钟、能力恰好覆盖……）；
3. 输出 ID 封闭性：`job_ids` / `resource_ids` 只含计划里真实出现的 ID；
4. 缺料只报缺口（R6.4），不推断补足；
5. `validate` 合并全部检查、`is_feasible ⟺ violations 为空`、只看 `scheduled_jobs`。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal

from app.core.scheduler import PlanCandidate, ScheduledJob, UnschedulableJob
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
from app.core.validation import (
    CHECKS,
    ValidationReport,
    Violation,
    check_machine_available,
    check_machine_capability,
    check_machine_no_overlap,
    check_material_sufficient,
    check_operation_precedence,
    check_shift_boundary,
    check_worker_available,
    check_worker_no_overlap,
    check_worker_skill,
    validate,
)

NOW = datetime(2026, 3, 2, 8, 0)
PRODUCTION_DATE = date(2026, 3, 2)


# --------------------------------------------------------------------------
# 构造夹具（全部为纯数据，绝不经排产器 —— 见模块 docstring 的独立性理由）
# --------------------------------------------------------------------------


def _op(
    sequence: int = 1,
    *,
    machine_type: str = "CNC",
    capability: str | None = "PRECISION_MILLING",
    skill: str = "CNC_OPERATION",
    base: str = "4.0",
    setup: int = 20,
) -> Operation:
    return Operation(
        sequence=sequence,
        required_machine_type=machine_type,
        required_capability=capability,
        required_worker_skill=skill,
        base_processing_time_per_unit=Decimal(base),
        setup_time=setup,
    )


def _product(
    product_id: str = "PRD-01",
    *,
    operations: tuple[Operation, ...] = (),
    material_ids: tuple[str, ...] = ("MAT-01",),
    qty_per_unit: str = "2.0",
) -> Product:
    return Product(
        product_id=product_id,
        name="加固支架",
        operations=operations if operations else (_op(),),
        bom=tuple(
            BomLine(material_id=mid, quantity_per_unit=Decimal(qty_per_unit))
            for mid in material_ids
        ),
    )


def _material(
    material_id: str = "MAT-01",
    *,
    available: str = "100",
    reserved: str = "0",
    deliveries: tuple[IncomingDelivery, ...] = (),
) -> Material:
    return Material(
        material_id=material_id,
        name="结构钢棒料",
        unit="kg",
        quantity_available=Decimal(available),
        reserved_quantity=Decimal(reserved),
        incoming_deliveries=deliveries,
    )


def _order(
    order_id: str = "ORD-001", *, product_id: str = "PRD-01", quantity: str = "10"
) -> Order:
    return Order(
        order_id=order_id,
        product_id=product_id,
        quantity=Decimal(quantity),
        due_date=NOW + timedelta(days=2),
        promised_date=None,
        priority="NORMAL",
    )


def _machine(
    machine_id: str = "CNC-01",
    *,
    machine_type: str = "CNC",
    capabilities: tuple[str, ...] = ("PRECISION_MILLING", "DEEP_DRILLING"),
    status: str = "AVAILABLE",
    downtime: tuple[DowntimeWindow, ...] = (),
    available_start: datetime | None = None,
    available_end: datetime | None = None,
) -> Machine:
    return Machine(
        machine_id=machine_id,
        machine_type=machine_type,
        capabilities=capabilities,
        status=status,  # type: ignore[arg-type]  # 测试刻意传各状态字面量
        available_start=available_start if available_start is not None else NOW,
        available_end=available_end if available_end is not None else NOW + timedelta(hours=12),
        rate_multiplier=Decimal("1.0"),
        downtime_windows=downtime,
    )


def _worker(
    worker_id: str = "W-01",
    *,
    skills: tuple[str, ...] = ("CNC_OPERATION",),
    shift_start: datetime | None = None,
    shift_end: datetime | None = None,
    absences: tuple[TimeWindow, ...] = (),
) -> Worker:
    return Worker(
        worker_id=worker_id,
        name="Anna Petrova",
        skills=skills,
        shift_start=shift_start if shift_start is not None else NOW,
        shift_end=shift_end if shift_end is not None else NOW + timedelta(hours=9),
        absences=absences,
    )


def _snapshot(
    *,
    orders: tuple[Order, ...] | None = None,
    products: tuple[Product, ...] | None = None,
    materials: tuple[Material, ...] | None = None,
    machines: tuple[Machine, ...] | None = None,
    workers: tuple[Worker, ...] | None = None,
) -> DomainSnapshot:
    return DomainSnapshot(
        snapshot_version=1,
        production_date=PRODUCTION_DATE,
        now=NOW,
        orders=orders if orders is not None else (_order(),),
        products=products if products is not None else (_product(),),
        materials=materials if materials is not None else (_material(),),
        machines=machines if machines is not None else (_machine(),),
        workers=workers if workers is not None else (_worker(),),
        changeover_rules=(),
        preference_rules=(),
    )


def _sj(
    *,
    job_id: str = "ORD-001-OP1",
    order_id: str = "ORD-001",
    product_id: str = "PRD-01",
    machine_id: str = "CNC-01",
    worker_id: str = "W-01",
    start: datetime | None = None,
    end: datetime | None = None,
    setup_minutes: int = 20,
    changeover_minutes: int = 0,
) -> ScheduledJob:
    return ScheduledJob(
        job_id=job_id,
        order_id=order_id,
        product_id=product_id,
        machine_id=machine_id,
        worker_id=worker_id,
        start_time=start if start is not None else NOW,
        end_time=end if end is not None else NOW + timedelta(hours=1),
        setup_minutes=setup_minutes,
        changeover_minutes=changeover_minutes,
    )


def _plan(
    *scheduled: ScheduledJob, unschedulable: tuple[UnschedulableJob, ...] = ()
) -> PlanCandidate:
    feasibility = "FEASIBLE" if scheduled and not unschedulable else "PARTIAL"
    if not scheduled:
        feasibility = "NO_FEASIBLE_PLAN"
    return PlanCandidate(
        scheduled_jobs=tuple(scheduled),
        unschedulable_jobs=unschedulable,
        feasibility=feasibility,  # type: ignore[arg-type]
    )


def _types(violations: list[Violation]) -> set[str]:
    return {v.violation_type for v in violations}


# --------------------------------------------------------------------------
# 1. MATERIAL_INSUFFICIENT（R6.3 / R6.4）
# --------------------------------------------------------------------------


def test_material_insufficient_reports_shortfall_only() -> None:
    """库存不够 → 报缺口，且只报缺口不补足（R6.4）。需求 10×2=20，库存 15，缺 5。"""
    snap = _snapshot(materials=(_material(available="15", reserved="0"),))
    plan = _plan(_sj())

    violations = check_material_sufficient(plan, snap)

    assert len(violations) == 1
    v = violations[0]
    assert v.violation_type == "MATERIAL_INSUFFICIENT"
    assert v.job_ids == ["ORD-001-OP1"]
    assert v.resource_ids == ["MAT-01"]
    # str(Decimal) 保留标度：需求 2.0×10=20.0，可用 15，缺口 5.0。
    assert Decimal(v.quantified["shortfall_quantity"]) == Decimal("5")
    assert Decimal(v.quantified["required_quantity"]) == Decimal("20")
    assert Decimal(v.quantified["available_quantity"]) == Decimal("15")


def test_material_exactly_enough_does_not_violate() -> None:
    """库存恰好等于需求（20）→ 不违反（边界：`usable < required` 是严格小于）。"""
    snap = _snapshot(materials=(_material(available="20", reserved="0"),))
    assert check_material_sufficient(_plan(_sj()), snap) == []


def test_material_delivery_counts_only_if_eta_strictly_before_start() -> None:
    """到货 eta 严格早于开工才计入（R6.3）。开工 08:00，一批 07:59 到、一批 08:00 到。"""
    on_time = IncomingDelivery(
        delivery_id="D-1", quantity=Decimal("10"), eta=NOW - timedelta(minutes=1), confirmed=True
    )
    at_start = IncomingDelivery(
        delivery_id="D-2", quantity=Decimal("10"), eta=NOW, confirmed=True
    )
    # 库存 5 + 07:59 到货 10 = 15 可用（08:00 那批 eta==start 不计），需求 20 → 缺 5。
    snap = _snapshot(materials=(_material(available="5", deliveries=(on_time, at_start)),))
    violations = check_material_sufficient(_plan(_sj(start=NOW)), snap)
    assert len(violations) == 1
    assert Decimal(violations[0].quantified["shortfall_quantity"]) == Decimal("5")


def test_material_reserved_quantity_reduces_availability() -> None:
    """`reserved_quantity` 先扣：库存 30、预留 15 → 可用 15，需求 20，缺 5。"""
    snap = _snapshot(materials=(_material(available="30", reserved="15"),))
    violations = check_material_sufficient(_plan(_sj()), snap)
    assert len(violations) == 1
    assert Decimal(violations[0].quantified["shortfall_quantity"]) == Decimal("5")


def test_material_two_orders_consume_sequentially() -> None:
    """两个订单竞争同一物料，按开工顺序滚动扣减：库存 30，各需 20，后一单缺 10。"""
    snap = _snapshot(
        orders=(_order("ORD-001"), _order("ORD-002")),
        materials=(_material(available="30"),),
    )
    early = _sj(job_id="ORD-001-OP1", order_id="ORD-001", start=NOW)
    late = _sj(
        job_id="ORD-002-OP1", order_id="ORD-002", start=NOW + timedelta(hours=2)
    )
    violations = check_material_sufficient(_plan(early, late), snap)
    assert len(violations) == 1
    assert violations[0].job_ids == ["ORD-002-OP1"]
    assert Decimal(violations[0].quantified["shortfall_quantity"]) == Decimal("10")


def test_material_not_found_reports_full_shortfall() -> None:
    """作业需要的物料在快照中不存在 → 报满额缺口，可用量 0。"""
    snap = _snapshot(materials=())  # MAT-01 缺失
    violations = check_material_sufficient(_plan(_sj()), snap)
    assert len(violations) == 1
    assert Decimal(violations[0].quantified["available_quantity"]) == Decimal("0")
    assert Decimal(violations[0].quantified["shortfall_quantity"]) == Decimal("20")


def test_material_skips_when_order_or_product_missing() -> None:
    """作业引用的订单/产品缺失 → 防御性跳过，不抛错（引用完整性本应在排产前拦下）。"""
    snap = _snapshot(orders=(), products=())
    assert check_material_sufficient(_plan(_sj()), snap) == []


# --------------------------------------------------------------------------
# 2. MACHINE_UNAVAILABLE（status + downtime + 可用窗 + 不存在）
# --------------------------------------------------------------------------


def test_machine_down_status_violates() -> None:
    snap = _snapshot(machines=(_machine(status="DOWN"),))
    violations = check_machine_available(_plan(_sj()), snap)
    assert _types(violations) == {"MACHINE_UNAVAILABLE"}
    assert violations[0].quantified["status"] == "DOWN"


def test_machine_maintenance_status_violates() -> None:
    snap = _snapshot(machines=(_machine(status="MAINTENANCE"),))
    assert _types(check_machine_available(_plan(_sj()), snap)) == {"MACHINE_UNAVAILABLE"}


def test_machine_not_found_violates() -> None:
    snap = _snapshot(machines=(_machine("CNC-01"),))
    violations = check_machine_available(_plan(_sj(machine_id="CNC-99")), snap)
    assert violations[0].quantified["reason"] == "NOT_FOUND"
    assert violations[0].resource_ids == ["CNC-99"]


def test_machine_downtime_window_overlap_violates() -> None:
    """作业占用落在停机窗内 → 违反。"""
    downtime = (
        DowntimeWindow(
            start=NOW + timedelta(minutes=30),
            end=NOW + timedelta(hours=2),
            reason="MAINTENANCE",
        ),
    )
    snap = _snapshot(machines=(_machine(downtime=downtime),))
    violations = check_machine_available(
        _plan(_sj(start=NOW, end=NOW + timedelta(hours=1))), snap
    )
    assert violations[0].quantified["reason"] == "DOWNTIME_WINDOW"


def test_machine_downtime_window_touching_edge_does_not_violate() -> None:
    """半开区间：作业 08:00–09:00 与 09:00 开始的停机窗端点相接 → 不违反。"""
    downtime = (
        DowntimeWindow(
            start=NOW + timedelta(hours=1), end=NOW + timedelta(hours=2), reason="MAINTENANCE"
        ),
    )
    snap = _snapshot(machines=(_machine(downtime=downtime),))
    assert check_machine_available(_plan(_sj(start=NOW, end=NOW + timedelta(hours=1))), snap) == []


def test_machine_outside_available_window_violates() -> None:
    """作业越出机器 `[available_start, available_end)` → 违反。"""
    snap = _snapshot(
        machines=(_machine(available_start=NOW, available_end=NOW + timedelta(minutes=30)),)
    )
    violations = check_machine_available(
        _plan(_sj(start=NOW, end=NOW + timedelta(hours=1))), snap
    )
    assert violations[0].quantified["reason"] == "OUTSIDE_AVAILABLE_WINDOW"


def test_machine_available_within_window_does_not_violate() -> None:
    assert check_machine_available(_plan(_sj()), _snapshot()) == []


# --------------------------------------------------------------------------
# 3. MACHINE_CAPABILITY_MISMATCH
# --------------------------------------------------------------------------


def test_machine_wrong_type_violates() -> None:
    snap = _snapshot(machines=(_machine(machine_type="LATHE"),))
    violations = check_machine_capability(_plan(_sj()), snap)
    assert violations[0].violation_type == "MACHINE_CAPABILITY_MISMATCH"
    assert violations[0].quantified["actual_machine_type"] == "LATHE"


def test_machine_missing_capability_violates() -> None:
    snap = _snapshot(machines=(_machine(capabilities=("DEEP_DRILLING",)),))
    violations = check_machine_capability(_plan(_sj()), snap)
    assert violations[0].quantified["required_capability"] == "PRECISION_MILLING"


def test_machine_capability_none_requirement_never_violates() -> None:
    """工序无能力要求（`required_capability is None`）→ 任何同型机器都满足。"""
    prod = _product(operations=(_op(capability=None),))
    snap = _snapshot(products=(prod,), machines=(_machine(capabilities=()),))
    assert check_machine_capability(_plan(_sj()), snap) == []


def test_machine_capability_exact_match_does_not_violate() -> None:
    snap = _snapshot(machines=(_machine(capabilities=("PRECISION_MILLING",)),))
    assert check_machine_capability(_plan(_sj()), snap) == []


def test_machine_capability_skips_missing_machine_or_operation() -> None:
    """机器不存在（交给 available 检查）或工序解析不出 → 跳过。"""
    snap = _snapshot()
    # 机器不存在：
    assert check_machine_capability(_plan(_sj(machine_id="X")), snap) == []
    # job_id 无 -OP 结构：
    assert check_machine_capability(_plan(_sj(job_id="WEIRD")), snap) == []
    # job_id 的 sequence 非整数：
    assert check_machine_capability(_plan(_sj(job_id="ORD-001-OPx")), snap) == []
    # product 不存在：
    assert check_machine_capability(_plan(_sj(product_id="NOPE")), snap) == []


# --------------------------------------------------------------------------
# 4. WORKER_UNAVAILABLE（absence + 不存在）
# --------------------------------------------------------------------------


def test_worker_not_found_violates() -> None:
    violations = check_worker_available(_plan(_sj(worker_id="W-99")), _snapshot())
    assert violations[0].violation_type == "WORKER_UNAVAILABLE"
    assert violations[0].quantified["reason"] == "NOT_FOUND"


def test_worker_absence_overlap_violates() -> None:
    absences = (TimeWindow(start=NOW + timedelta(minutes=30), end=NOW + timedelta(hours=2)),)
    snap = _snapshot(workers=(_worker(absences=absences),))
    violations = check_worker_available(_plan(_sj(end=NOW + timedelta(hours=1))), snap)
    assert violations[0].quantified["reason"] == "ABSENCE"


def test_worker_absence_touching_edge_does_not_violate() -> None:
    absences = (TimeWindow(start=NOW + timedelta(hours=1), end=NOW + timedelta(hours=2)),)
    snap = _snapshot(workers=(_worker(absences=absences),))
    assert check_worker_available(_plan(_sj(end=NOW + timedelta(hours=1))), snap) == []


# --------------------------------------------------------------------------
# 5. WORKER_SKILL_MISMATCH
# --------------------------------------------------------------------------


def test_worker_missing_skill_violates() -> None:
    snap = _snapshot(workers=(_worker(skills=("WELDING",)),))
    violations = check_worker_skill(_plan(_sj()), snap)
    assert violations[0].violation_type == "WORKER_SKILL_MISMATCH"
    assert violations[0].quantified["required_skill"] == "CNC_OPERATION"


def test_worker_has_skill_does_not_violate() -> None:
    assert check_worker_skill(_plan(_sj()), _snapshot()) == []


def test_worker_skill_skips_missing_worker_or_operation() -> None:
    assert check_worker_skill(_plan(_sj(worker_id="X")), _snapshot()) == []
    assert check_worker_skill(_plan(_sj(job_id="WEIRD")), _snapshot()) == []


# --------------------------------------------------------------------------
# 6. MACHINE_DOUBLE_BOOKING（含换型占用区间）
# --------------------------------------------------------------------------


def test_machine_double_booking_violates() -> None:
    """同机两作业 08:00–09:00 与 08:30–09:30 重叠 30 分钟。"""
    a = _sj(job_id="ORD-001-OP1", start=NOW, end=NOW + timedelta(hours=1))
    b = _sj(
        job_id="ORD-002-OP1",
        order_id="ORD-002",
        start=NOW + timedelta(minutes=30),
        end=NOW + timedelta(minutes=90),
    )
    violations = check_machine_no_overlap(_plan(a, b), _snapshot())
    assert violations[0].violation_type == "MACHINE_DOUBLE_BOOKING"
    assert violations[0].quantified["overlap_minutes"] == 30
    assert set(violations[0].job_ids) == {"ORD-001-OP1", "ORD-002-OP1"}


def test_machine_back_to_back_does_not_violate() -> None:
    """半开区间：08:00–09:00 与 09:00–10:00 端点相接 → 不重叠。"""
    a = _sj(job_id="A", start=NOW, end=NOW + timedelta(hours=1))
    b = _sj(
        job_id="B",
        order_id="ORD-002",
        start=NOW + timedelta(hours=1),
        end=NOW + timedelta(hours=2),
    )
    assert check_machine_no_overlap(_plan(a, b), _snapshot()) == []


def test_machine_double_booking_ignores_different_machines() -> None:
    a = _sj(job_id="A", machine_id="CNC-01", start=NOW, end=NOW + timedelta(hours=1))
    b = _sj(job_id="B", machine_id="CNC-02", start=NOW, end=NOW + timedelta(hours=1))
    assert check_machine_no_overlap(_plan(a, b), _snapshot()) == []


# --------------------------------------------------------------------------
# 7. WORKER_DOUBLE_BOOKING
# --------------------------------------------------------------------------


def test_worker_double_booking_violates() -> None:
    a = _sj(
        job_id="A", machine_id="CNC-01", worker_id="W-01", start=NOW, end=NOW + timedelta(hours=1)
    )
    b = _sj(
        job_id="B",
        machine_id="CNC-02",
        worker_id="W-01",
        start=NOW + timedelta(minutes=30),
        end=NOW + timedelta(minutes=90),
    )
    violations = check_worker_no_overlap(_plan(a, b), _snapshot())
    assert violations[0].violation_type == "WORKER_DOUBLE_BOOKING"
    assert violations[0].quantified["overlap_minutes"] == 30


def test_worker_no_overlap_when_disjoint() -> None:
    a = _sj(job_id="A", worker_id="W-01", start=NOW, end=NOW + timedelta(hours=1))
    b = _sj(
        job_id="B",
        worker_id="W-01",
        start=NOW + timedelta(hours=2),
        end=NOW + timedelta(hours=3),
    )
    assert check_worker_no_overlap(_plan(a, b), _snapshot()) == []


# --------------------------------------------------------------------------
# 8. OPERATION_PRECEDENCE_VIOLATION（R4.3）
# --------------------------------------------------------------------------


def _two_op_product() -> Product:
    return _product(operations=(_op(sequence=1), _op(sequence=2, machine_type="CNC")))


def test_precedence_successor_before_predecessor_end_violates() -> None:
    """OP2 于 08:30 开工，早于 OP1 的结束 09:00 → 违反。"""
    snap = _snapshot(products=(_two_op_product(),))
    op1 = _sj(job_id="ORD-001-OP1", start=NOW, end=NOW + timedelta(hours=1))
    op2 = _sj(
        job_id="ORD-001-OP2",
        start=NOW + timedelta(minutes=30),
        end=NOW + timedelta(minutes=90),
    )
    violations = check_operation_precedence(_plan(op1, op2), snap)
    assert violations[0].violation_type == "OPERATION_PRECEDENCE_VIOLATION"
    assert violations[0].job_ids == ["ORD-001-OP1", "ORD-001-OP2"]
    assert violations[0].resource_ids == []
    assert violations[0].quantified["overlap_minutes"] == 30


def test_precedence_successor_at_predecessor_end_does_not_violate() -> None:
    """OP2 恰好在 OP1 结束时刻开工 → 不违反（`start < prev_end` 是严格小于）。"""
    snap = _snapshot(products=(_two_op_product(),))
    op1 = _sj(job_id="ORD-001-OP1", start=NOW, end=NOW + timedelta(hours=1))
    op2 = _sj(
        job_id="ORD-001-OP2",
        start=NOW + timedelta(hours=1),
        end=NOW + timedelta(hours=2),
    )
    assert check_operation_precedence(_plan(op1, op2), snap) == []


def test_precedence_single_op_never_violates() -> None:
    assert check_operation_precedence(_plan(_sj()), _snapshot()) == []


def test_precedence_skips_unresolvable_operation() -> None:
    """工序解析不出（job_id 无结构）→ 跳过，不进前后序判定。"""
    snap = _snapshot(products=(_two_op_product(),))
    assert check_operation_precedence(_plan(_sj(job_id="WEIRD")), snap) == []


# --------------------------------------------------------------------------
# 9. SHIFT_BOUNDARY_VIOLATION（R4.6）
# --------------------------------------------------------------------------


def test_shift_end_overflow_violates() -> None:
    """作业结束 17:30 晚于班次结束 17:00（NOW+9h）→ 违反，报超出分钟数。"""
    end = NOW + timedelta(hours=9, minutes=30)
    violations = check_shift_boundary(_plan(_sj(start=NOW, end=end)), _snapshot())
    assert violations[0].violation_type == "SHIFT_BOUNDARY_VIOLATION"
    assert violations[0].quantified["minutes_after_shift"] == 30
    assert violations[0].quantified["minutes_before_shift"] == 0


def test_shift_start_underflow_violates() -> None:
    """作业开工早于班次开始 → 违反。班次 09:00 起，作业 08:30 开工。"""
    worker = _worker(shift_start=NOW + timedelta(hours=1), shift_end=NOW + timedelta(hours=9))
    snap = _snapshot(workers=(worker,))
    sj = _sj(start=NOW + timedelta(minutes=30), end=NOW + timedelta(hours=2))
    violations = check_shift_boundary(_plan(sj), snap)
    assert violations[0].quantified["minutes_before_shift"] == 30


def test_shift_exact_fit_does_not_violate() -> None:
    """作业占满整个班次 `[shift_start, shift_end)` → 不违反（端点闭合）。"""
    sj = _sj(start=NOW, end=NOW + timedelta(hours=9))
    assert check_shift_boundary(_plan(sj), _snapshot()) == []


def test_shift_boundary_skips_missing_worker() -> None:
    assert check_shift_boundary(_plan(_sj(worker_id="X")), _snapshot()) == []


# --------------------------------------------------------------------------
# validate() 聚合、封闭性、可行性
# --------------------------------------------------------------------------


def test_validate_clean_plan_is_feasible() -> None:
    report = validate(_plan(_sj()), _snapshot())
    assert isinstance(report, ValidationReport)
    assert report.is_feasible is True
    assert report.violations == ()


def test_validate_merges_multiple_checks() -> None:
    """一个同时违反多类的坏计划：错机型 + 缺料 + 越班次，validate 全部合并。"""
    snap = _snapshot(
        materials=(_material(available="0"),),
        machines=(_machine(machine_type="LATHE"),),
    )
    sj = _sj(start=NOW, end=NOW + timedelta(hours=10))  # 越出 9h 班次
    report = validate(_plan(sj), snap)
    assert report.is_feasible is False
    expected = {"MATERIAL_INSUFFICIENT", "MACHINE_CAPABILITY_MISMATCH", "SHIFT_BOUNDARY_VIOLATION"}
    assert expected <= _types(list(report.violations))


def test_validate_only_inspects_scheduled_jobs() -> None:
    """`unschedulable_jobs` 不参与校验：一个只含不可排产作业的计划无任何违反。"""
    unsched = (
        UnschedulableJob(
            job_id="ORD-001-OP1",
            order_id="ORD-001",
            blocking_reason="MATERIAL_INSUFFICIENT",
            unblock_suggestion={},
        ),
    )
    snap = _snapshot(materials=(_material(available="0"),))
    report = validate(_plan(unschedulable=unsched), snap)
    assert report.is_feasible is True


def test_output_ids_are_closed_over_the_plan() -> None:
    """输出封闭性：违反里出现的 ID 都是计划/快照里真实存在的（不虚构资源）。"""
    snap = _snapshot(materials=(_material(available="0"),), machines=(_machine(status="DOWN"),))
    report = validate(_plan(_sj()), snap)
    known_jobs = {"ORD-001-OP1"}
    known_resources = {"CNC-01", "W-01", "MAT-01"}
    for v in report.violations:
        assert set(v.job_ids) <= known_jobs
        assert set(v.resource_ids) <= known_resources


def test_checks_tuple_has_nine_functions() -> None:
    """9 类硬约束 ⟺ 9 个检查函数（R6.1）。"""
    assert len(CHECKS) == 9


def test_validate_signature_excludes_preference_rules() -> None:
    """`validate` 签名不含 `preference_rules`（任务 11.1 反射断言的对象，在此先自测）。"""
    import inspect

    params = set(inspect.signature(validate).parameters)
    assert params == {"candidate", "snapshot"}
    assert "preference_rules" not in params
