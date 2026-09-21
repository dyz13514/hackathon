"""内核三模块的分支覆盖补洞（任务 2.13，R27.10）。

`test_scheduler_core.py` / `test_scheduling_core.py` / `test_validation.py` /
`test_scoring_core.py` 已经覆盖了 `Scheduling_Core` / `Constraint_Validator` /
`Objective_Scorer` 的绝大多数行为（承接被裁剪的原属性 3、5、6、7、8）。本文件只补它们
**尚未触达的分支**，把这三个模块的分支覆盖率补到 R27.10 要求的 100%——CI 门禁（任务 12.5）
对这四个模块单独设 100% 门槛。

不与既有测试重复：每个用例针对一条 `coverage --cov-branch` 报告里 miss 的具体分支，注释里
标注它守的那条边界。全部纯数据构造，不 mock、不碰库。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest

from app.core.scheduler import (
    Failure,
    InvalidRoutingError,
    PlanCandidate,
    ProductionJob,
    ScheduledJob,
    UnschedulableJob,
    expand,
    generate_schedule,
    material_ready_time,
    quantify,
)
from app.core.scheduling import Timeline, earliest_feasible_slot
from app.core.scoring import ObjectiveWeights, score
from app.core.snapshot import (
    BomLine,
    ChangeoverRule,
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
from app.core.validation import check_material_sufficient

PRODUCTION_DATE = date(2026, 3, 2)
NOW = datetime(2026, 3, 2, 8, 0)
SHIFT_START = datetime(2026, 3, 2, 0, 0)
DAY = datetime(2026, 3, 2, 8, 0)
DAY_END = datetime(2026, 3, 2, 18, 0)


def _at(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 3, 2, hour, minute)


# --------------------------------------------------------------------------
# 共用夹具
# --------------------------------------------------------------------------


def _op(
    sequence: int,
    *,
    machine_type: str = "CNC",
    capability: str | None = None,
    skill: str = "CNC_OP",
    per_unit: str = "2.0",
    setup: int = 10,
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
    available_start: datetime | None = None,
    available_end: datetime | None = None,
) -> Machine:
    return Machine(
        machine_id=machine_id,
        machine_type=machine_type,
        capabilities=capabilities,
        status=status,  # type: ignore[arg-type]
        available_start=available_start or DAY,
        available_end=available_end or DAY_END,
        rate_multiplier=Decimal(rate),
        downtime_windows=downtime,
    )


def _worker(
    worker_id: str = "W-01",
    *,
    skills: tuple[str, ...] = ("CNC_OP",),
    absences: tuple[TimeWindow, ...] = (),
    shift_start: datetime | None = None,
    shift_end: datetime | None = None,
) -> Worker:
    return Worker(
        worker_id=worker_id,
        name="张三",
        skills=skills,
        shift_start=shift_start or DAY,
        shift_end=shift_end or DAY_END,
        absences=absences,
    )


def _snapshot(
    *,
    orders: tuple[Order, ...],
    products: tuple[Product, ...],
    machines: tuple[Machine, ...] = (),
    workers: tuple[Worker, ...] = (),
    materials: tuple[Material, ...] = (),
    changeover_rules: tuple[ChangeoverRule, ...] = (),
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
        changeover_rules=changeover_rules,
        preference_rules=(),
    )


def _job(order_id: str = "ORD-01", *, quantity: str = "10") -> ProductionJob:
    return ProductionJob(
        job_id=f"{order_id}-OP1",
        order_id=order_id,
        product_id="PRD-01",
        quantity=Decimal(quantity),
        operation_sequence=1,
        predecessor_job_id=None,
        required_machine_type="CNC",
        required_capability=None,
        required_worker_skill="CNC_OP",
        base_processing_time_per_unit=Decimal("2.0"),
        setup_time=10,
    )


# ==========================================================================
# scheduling.py
# ==========================================================================


def test_slot_gap_insertion_fits_when_following_changeover_leaves_room() -> None:
    """空隙插入：后一个作业不同产品，但其换型**塞得进**空隙 → 返回该槽位（分支 313→316）。

    既有 `test_slot_gap_insertion_leaves_changeover_for_following_job` 覆盖的是「塞不下 → 跳过」；
    这里覆盖对偶的「塞得下 → 落位」，把 `if end + follow_changeover > nxt.start` 的 False 分支
    补上。作业做 P1 于 09:00–10:00，后一作业 P2 于 11:00 开始，P1→P2 换型 30 分钟能装进
    [10:00, 11:00) → 09:00 起可行。
    """
    machine = _machine("CNC-01")
    m_tl = Timeline()
    m_tl.occupy(_at(8), _at(9), "P0")  # t<ready_at 的占用结束点（用于 323 分支）
    m_tl.occupy(_at(11), _at(12), "P2")  # 后一个作业做 P2，11:00 开始
    rules = (ChangeoverRule(
        rule_id="R", machine_id=None, from_product_id=None, to_product_id=None,
        changeover_minutes=30, specificity=1,
    ),)
    slot = earliest_feasible_slot(
        m_tl,
        Timeline(),
        machine=machine,
        to_product="P1",
        setup_time=0,
        ready_at=_at(9),
        duration=60,  # 09:00 + 换型(P0→P1=30) + 60 = 10:30？—— 见下
        hard_end=_at(18),
        changeover_rules=rules,
    )
    # 09:00 起：setup=changeover(P0→P1)=30，end=09:00+30+60=10:30。后续 P1→P2 换型 30 分钟，
    # 10:30+30=11:00 <= nxt.start(11:00) → 恰好塞下（半开边界），返回该槽位。
    assert slot is not None
    assert slot.start == _at(9)
    assert slot.end == _at(10, 30)


def test_slot_skips_candidate_start_before_ready_at() -> None:
    """候选起点里存在早于 `ready_at` 的时间点 → 被 `if t < ready_at: continue` 跳过（行 323）。

    机器 08:00–09:00 有占用，其结束点 09:00 进入候选起点集合，但 `ready_at` 是 10:00：
    09:00 < 10:00 应被跳过，最终从 10:00 起。
    """
    machine = _machine("CNC-01")
    m_tl = Timeline()
    m_tl.occupy(_at(8), _at(9), "P1")  # 结束点 09:00 是一个早于 ready_at 的候选起点
    slot = earliest_feasible_slot(
        m_tl,
        Timeline(),
        machine=machine,
        to_product="P1",
        setup_time=0,
        ready_at=_at(10),
        duration=30,
        hard_end=_at(18),
        changeover_rules=(),
    )
    assert slot is not None
    assert slot.start == _at(10)


# ==========================================================================
# scoring.py
# ==========================================================================


def _sched(
    order_id: str,
    *,
    job_id: str | None = None,
    start: datetime,
    end: datetime,
    machine_id: str = "CNC-01",
    worker_id: str = "W-01",
) -> ScheduledJob:
    return ScheduledJob(
        job_id=job_id or f"{order_id}-OP1",
        order_id=order_id,
        product_id="PRD-01",
        machine_id=machine_id,
        worker_id=worker_id,
        start_time=start,
        end_time=end,
        setup_minutes=0,
        changeover_minutes=0,
    )


def _scoring_snapshot(orders: tuple[Order, ...]) -> DomainSnapshot:
    return _snapshot(orders=orders, products=(_product(),))


def test_completion_time_keeps_latest_when_second_job_ends_earlier() -> None:
    """同一订单的第二个作业结束**更早** → 不更新完工时刻（`_order_completion_times` 分支 171→169）。

    OP1 结束 10:00，OP2 结束 09:00（更早）：完工时刻应保持 10:00。既有测试只覆盖「后者更晚
    从而更新」的分支，这里补「后者更早、不更新」的对偶分支。
    """
    snap = _scoring_snapshot((_order("ORD-01", due=DAY_END),))
    plan = PlanCandidate(
        scheduled_jobs=(
            _sched("ORD-01", job_id="ORD-01-OP1", start=DAY, end=_at(10)),
            _sched("ORD-01", job_id="ORD-01-OP2", start=DAY, end=_at(9)),
        ),
        unschedulable_jobs=(),
        feasibility="FEASIBLE",
    )
    # 完工取最晚 10:00；交期 18:00 → 不迟。分量本身正确即证明取的是 10:00 而非 09:00。
    breakdown = score(plan, snap, ObjectiveWeights())
    late = next(c for c in breakdown.components if c.name == "late_order_count")
    assert late.raw_value == 0.0


def test_scoring_skips_scheduled_job_whose_order_missing_from_snapshot() -> None:
    """已排产作业的订单不在快照中 → `_late_and_tardiness` 的 `if order is None: continue`（行 211）。

    防御性分支：正常路径由引用完整性保证，但评分器不假设它，遇到孤儿作业跳过而非抛错。
    """
    snap = _scoring_snapshot((_order("ORD-01", due=DAY_END),))
    plan = PlanCandidate(
        scheduled_jobs=(
            _sched("ORD-01", start=DAY, end=_at(9)),
            _sched("ORD-GHOST", job_id="ORD-GHOST-OP1", start=DAY, end=_at(20)),  # 订单不在快照
        ),
        unschedulable_jobs=(),
        feasibility="FEASIBLE",
    )
    breakdown = score(plan, snap, ObjectiveWeights())
    # 孤儿订单被跳过，不计迟交；仅 ORD-01 参与（未迟）。
    assert next(c for c in breakdown.components if c.name == "late_order_count").raw_value == 0.0


def test_churn_ratio_zero_when_reference_given_but_both_plans_empty() -> None:
    """传入参照计划、但两份计划都无作业 → 并集为空 → churn 0.0（`_churn_ratio` 行 290）。

    既有测试覆盖「无参照 → 0.0」；这里覆盖「有参照但并集为空 → 0.0」的另一条 return。
    """
    empty = PlanCandidate(scheduled_jobs=(), unschedulable_jobs=(), feasibility="NO_FEASIBLE_PLAN")
    snap = _scoring_snapshot((_order("ORD-01"),))
    breakdown = score(empty, snap, ObjectiveWeights(), reference_plan=empty)
    assert next(c for c in breakdown.components if c.name == "churn_ratio").raw_value == 0.0


# ==========================================================================
# validation.py
# ==========================================================================


def _val_snapshot(
    *,
    materials: tuple[Material, ...] | None = None,
    products: tuple[Product, ...] | None = None,
    orders: tuple[Order, ...] | None = None,
) -> DomainSnapshot:
    return _snapshot(
        orders=orders or (_order("ORD-001"),),
        products=products or (
            _product(operations=(_op(1),), bom=(BomLine(material_id="MAT-01",
                     quantity_per_unit=Decimal("2.0")),)),
        ),
        materials=materials if materials is not None else (
            Material(material_id="MAT-01", name="钢", unit="kg", quantity_available=Decimal("100"),
                     reserved_quantity=Decimal("0"), incoming_deliveries=()),
        ),
    )


def _val_sj(
    *,
    job_id: str = "ORD-001-OP1",
    order_id: str = "ORD-001",
    start: datetime,
    end: datetime,
) -> ScheduledJob:
    return ScheduledJob(
        job_id=job_id,
        order_id=order_id,
        product_id="PRD-01",
        machine_id="CNC-01",
        worker_id="W-01",
        start_time=start,
        end_time=end,
        setup_minutes=0,
        changeover_minutes=0,
    )


def test_material_check_keeps_earliest_first_job_when_second_not_earlier() -> None:
    """同一订单两个已排产作业，第二个开工**不更早** → 保留首个作业作为预留发生点（分支 171→169）。

    `check_material_sufficient` 用 `(start_time, job_id)` 选每个订单的「首道工序」。既有测试
    没有触发「遇到一个不更早的候选、维持原选」的 else 分支，这里补上：OP1 于 08:00、OP2 于
    09:00（更晚），首个应恒为 OP1。
    """
    plan = PlanCandidate(
        scheduled_jobs=(
            _val_sj(job_id="ORD-001-OP1", start=NOW, end=NOW + timedelta(hours=1)),
            _val_sj(job_id="ORD-001-OP2", start=NOW + timedelta(hours=1),
                    end=NOW + timedelta(hours=2)),
        ),
        unschedulable_jobs=(),
        feasibility="FEASIBLE",
    )
    # 库存 100 足够（需 20），无违反；用例的价值在于走过 else 分支且结果正确。
    assert check_material_sufficient(plan, _val_snapshot()) == []


def test_operation_lookup_returns_none_when_sequence_absent() -> None:
    """`_operation_of`：产品存在、job_id 可解析，但该 sequence 在产品里没有对应工序 → None（行 351）。

    产品只有 sequence=1 的工序，作业 job_id 解析出 sequence=2 → 查不到 → 返回 None，能力检查
    因此跳过（不误报）。既有测试只覆盖「解析失败」的 None，这里补「解析成功但无匹配工序」。
    """
    from app.core.validation import check_machine_capability

    prod = _product(operations=(_op(1),))  # 只有 OP1
    snap = _snapshot(orders=(_order("ORD-001"),), products=(prod,),
                     machines=(_machine("CNC-01"),))
    plan = PlanCandidate(
        scheduled_jobs=(_val_sj(job_id="ORD-001-OP2", start=NOW, end=NOW + timedelta(hours=1)),),
        unschedulable_jobs=(),
        feasibility="FEASIBLE",
    )
    assert check_machine_capability(plan, snap) == []


# ==========================================================================
# scheduler.py
# ==========================================================================


def test_expand_rejects_more_than_three_operations() -> None:
    """4 道工序 → `INVALID_ROUTING`（`len(operations) > 3` 分支，行 242）。

    `Operation.sequence` 限定 1..3，4 道必含重复 sequence，但 `>3` 检查在重复检查**之前**，
    因此 4 道（sequence 1,2,3,3）先触发 `>3` 分支。既有测试注释说这条门「无法构造」——
    其实构造 4 个 Operation 对象即可，字段约束限制的是单个 sequence 的取值而非工序条数。
    """
    product = Product(
        product_id="PRD-4OP",
        name="四道工序",
        operations=(_op(1), _op(2), _op(3), _op(3)),
        bom=(),
    )
    with pytest.raises(InvalidRoutingError) as exc:
        expand(_order("ORD-01"), product)
    assert "at most 3 operations" in exc.value.detail


def test_material_ready_time_material_missing_from_snapshot() -> None:
    """`need` 里的物料在快照中不存在 → 缺料（`ready_points` 与候选循环的 material None 分支）。

    覆盖 material_ready_time 中 `if material is not None`（收集到货 eta，行 404→402 的 None 侧）
    与候选时刻循环里 `if material is None: break`（行 422-423）：缺失物料一律记满额缺口。
    """
    need = {"MAT-GHOST": Decimal("5")}
    snap = _snapshot(orders=(), products=(), materials=())
    ready, shortfalls = material_ready_time(need, snap, {}, SHIFT_START)
    assert ready is None
    assert shortfalls == {"MAT-GHOST": Decimal("5")}


def test_material_ready_time_mixed_shortfall_reports_only_deficient() -> None:
    """两种物料，一种够、一种缺 → 只报缺的那种（最终 shortfall 循环的 `total < required` 两侧）。

    覆盖行 444→437：物料 A 充足（跳过、不进 shortfalls），物料 B 不足（进 shortfalls）。
    """
    materials = (
        Material(material_id="MAT-A", name="充足", unit="kg", quantity_available=Decimal("100"),
                 reserved_quantity=Decimal("0"), incoming_deliveries=()),
        Material(material_id="MAT-B", name="不足", unit="kg", quantity_available=Decimal("1"),
                 reserved_quantity=Decimal("0"), incoming_deliveries=()),
    )
    need = {"MAT-A": Decimal("10"), "MAT-B": Decimal("10")}
    snap = _snapshot(orders=(), products=(), materials=materials)
    ready, shortfalls = material_ready_time(need, snap, {}, SHIFT_START)
    assert ready is None
    assert shortfalls == {"MAT-B": Decimal("9")}  # 只有 B，A 充足不报


def test_quantify_fallback_for_unknown_reason() -> None:
    """`quantify` 的兜底分支（行 620）：传入 7 类之外的 reason → 返回含数值字段的安全建议。

    主循环只构造 7 类，本分支「不应到达」，但为了 100% 分支覆盖显式触发它。
    """
    job = _job()
    snap = _snapshot(orders=(_order("ORD-01"),), products=(_product(),))
    out = quantify(Failure(reason="SOME_UNKNOWN_REASON"), job, snap)
    assert out == {"minutes_needed": 20}  # ceil(2.0 × 10 ÷ 1.0)


def test_quantify_capability_mismatch_none_capability_returns_empty_types() -> None:
    """`_machine_types_with_capability` 的 `capability is None` 分支（行 647）。

    工序无能力要求（`required_capability=None`）却报了 CAPABILITY_MISMATCH（罕见但可能，例如
    机型不匹配也归此类）→ 合格机型列表为空（能力为 None 无从枚举）。
    """
    job = ProductionJob(
        job_id="ORD-01-OP1", order_id="ORD-01", product_id="PRD-01", quantity=Decimal("10"),
        operation_sequence=1, predecessor_job_id=None, required_machine_type="LASER",
        required_capability=None, required_worker_skill="CNC_OP",
        base_processing_time_per_unit=Decimal("2.0"), setup_time=10,
    )
    snap = _snapshot(orders=(_order("ORD-01"),), products=(_product(),),
                     machines=(_machine("CNC-01"),))
    out = quantify(Failure(reason="MACHINE_CAPABILITY_MISMATCH"), job, snap)
    assert out["required_capability"] is None
    assert out["qualifying_machine_types"] == []


def test_quantify_worker_unavailable_shift_window_none_when_no_skilled_worker() -> None:
    """`_shift_window_for_skill` 的 `if not windows: return None` 分支（行 666）。

    直接对一个没有任何工人具备该技能的快照渲染 WORKER_UNAVAILABLE 的建议 → `shift_window` 为
    None（诊断路径下不会出现，但函数需对空集合安全）。
    """
    job = _job()
    snap = _snapshot(
        orders=(_order("ORD-01"),),
        products=(_product(),),
        workers=(_worker("W-01", skills=("WELDING",)),),  # 无 CNC_OP
    )
    out = quantify(Failure(reason="WORKER_UNAVAILABLE"), job, snap)
    assert out["shift_window"] is None


def test_diagnose_shift_boundary_with_zero_length_worker_shift() -> None:
    """班次边界：技能工人的班次窗**零长**（`shift_end == shift_start`）→ 可用窗口 0 分钟。

    覆盖 `_minutes_between` 的 `end <= start` 分支（scheduler.py 行 666）：`diagnose_blocking`
    逐「机器 × 工人」算 `[max(ready, shift_start, avail_start), min(shift_end, avail_end))` 的窗口
    长度，工人班次零长时 `hard_end == window_start` → `_minutes_between` 返回 0，没有任何组合
    够长 → 归因 `SHIFT_BOUNDARY_VIOLATION` 且 `available_minutes_in_shift == 0`。这是任务 2.13
    要求的「班次边界刚好卡住的作业」的退化端点：连一分钟窗口都没有。
    """
    from app.core.scheduler import diagnose_blocking

    job = _job()
    snap = _snapshot(
        orders=(_order("ORD-01"),),
        products=(_product(),),
        machines=(_machine("CNC-01"),),
        # 技能匹配、但班次窗零长（09:00–09:00）。
        workers=(_worker("W-01", skills=("CNC_OP",), shift_start=_at(9), shift_end=_at(9)),),
    )
    failure = diagnose_blocking(
        job, snap, {}, {}, frozenset(), ready=DAY,
    )
    assert failure.reason == "SHIFT_BOUNDARY_VIOLATION"
    assert failure.available_minutes_in_shift == 0


def _frozen_sj(
    *,
    job_id: str,
    order_id: str,
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
        end_time=end or _at(9),
        setup_minutes=0,
        changeover_minutes=0,
    )


def test_freeze_job_referencing_unknown_machine_and_worker() -> None:
    """冻结作业引用了快照里不存在的机器/工人 → 跳过占位（分支 734→736 与 736→738）。

    重排时故障机器可能被换掉、冻结的旧位置仍指向它；主循环用 `if sj.machine_id in machine_tls`
    / `if sj.worker_id in worker_tls` 防御性跳过占位，不抛 KeyError。
    """
    frozen = _frozen_sj(
        job_id="ORD-FZ-OP1", order_id="ORD-FZ",
        machine_id="GHOST-M", worker_id="GHOST-W", product_id="PRD-01",
    )
    # 无待排订单：只验证冻结占位不因未知资源崩溃，且冻结作业原样保留。
    snap = _snapshot(orders=(), products=(_product(),))
    plan = generate_schedule(snap, freeze=(frozen,))
    assert frozen in plan.scheduled_jobs


def test_freeze_non_op1_job_skips_material_reservation() -> None:
    """冻结作业不是 OP1（`endswith("-OP1")` 为 False） → 不做物料预留（分支 744→733）。

    物料在订单首道工序处一次性预留，冻结的非首道工序不触发预留（否则会重复扣料）。
    """
    frozen = _frozen_sj(job_id="ORD-FZ-OP2", order_id="ORD-FZ")
    product = _product(bom=(BomLine(material_id="MAT-01", quantity_per_unit=Decimal("1")),))
    material = Material(material_id="MAT-01", name="钢", unit="kg",
                        quantity_available=Decimal("100"), reserved_quantity=Decimal("0"),
                        incoming_deliveries=())
    snap = _snapshot(orders=(_order("ORD-FZ"),), products=(product,), materials=(material,))
    plan = generate_schedule(snap, freeze=(frozen,))
    # 冻结的是 OP2；其订单 ORD-FZ 整体视为已处理，不重排，也不因非 OP1 触发预留报错。
    assert frozen in plan.scheduled_jobs


def test_freeze_op1_job_with_bom_reserves_material() -> None:
    """冻结作业是 OP1 且产品有 BOM → 走物料预留循环（行 746）。

    库存 10，冻结的 ORD-FZ（需 10）在 OP1 处预留掉全部；另一待排订单 ORD-NEW 也需 10 → 因预留
    而缺料。这既覆盖 746 的预留循环体，又验证冻结作业的物料确实被扣。
    """
    frozen = _frozen_sj(job_id="ORD-FZ-OP1", order_id="ORD-FZ")
    product = _product(bom=(BomLine(material_id="MAT-01", quantity_per_unit=Decimal("1")),))
    material = Material(material_id="MAT-01", name="钢", unit="kg",
                        quantity_available=Decimal("10"), reserved_quantity=Decimal("0"),
                        incoming_deliveries=())
    orders = (_order("ORD-FZ", quantity="10"), _order("ORD-NEW", quantity="10"))
    snap = _snapshot(orders=orders, products=(product,), materials=(material,))
    plan = generate_schedule(snap, freeze=(frozen,))
    # 冻结作业占了物料 → ORD-NEW 缺料。
    assert {u.order_id for u in plan.unschedulable_jobs} == {"ORD-NEW"}
    assert plan.unschedulable_jobs[0].blocking_reason == "MATERIAL_INSUFFICIENT"


def test_fully_frozen_order_is_skipped() -> None:
    """一个订单的**全部**作业都在冻结集里 → 该订单被跳过（`all(... in frozen_job_ids)`，行 766）。

    两道工序都冻结，主循环在 `if jobs and all(...)` 处 continue，不重排、不产生 unschedulable。
    """
    product = _product(operations=(_op(1), _op(2)))
    frozen = (
        _frozen_sj(job_id="ORD-FZ-OP1", order_id="ORD-FZ", start=DAY, end=_at(9)),
        _frozen_sj(job_id="ORD-FZ-OP2", order_id="ORD-FZ", start=_at(9), end=_at(10)),
    )
    snap = _snapshot(orders=(_order("ORD-FZ"),), products=(product,))
    plan = generate_schedule(snap, freeze=frozen)
    assert set(plan.scheduled_jobs) == set(frozen)
    assert plan.unschedulable_jobs == ()
    assert plan.feasibility == "FEASIBLE"


def test_material_delivery_pushes_start_time_later_than_shift_start() -> None:
    """物料到货晚于班次起点 → `ready_material > ready`，作业开工被推迟（分支 887）。

    库存 0，一批 12:00 到货 100。物料齐备时刻 12:01 晚于班次起点 → `ready = ready_material`，
    作业从 12:01 起开工。覆盖 `_place_order` 里 `if ready_material > ready: ready = ready_material`
    的 True 分支。
    """
    material = Material(
        material_id="MAT-01", name="钢", unit="kg", quantity_available=Decimal("0"),
        reserved_quantity=Decimal("0"),
        incoming_deliveries=(IncomingDelivery(
            delivery_id="D-1", quantity=Decimal("100"), eta=_at(12), confirmed=True),),
    )
    product = _product(bom=(BomLine(material_id="MAT-01", quantity_per_unit=Decimal("1")),))
    snap = _snapshot(orders=(_order("ORD-01", quantity="10"),), products=(product,),
                     materials=(material,))
    plan = generate_schedule(snap)
    assert plan.feasibility == "FEASIBLE"
    assert plan.scheduled_jobs[0].start_time == _at(12, 1)  # 12:00 到货，最早开工 12:01


def test_best_candidate_skips_slotless_combo_and_uses_working_one() -> None:
    """`_best_candidate` 中一个组合无可行槽位（slot None）但另一个可行 → 走 `continue`（行 954）。

    CNC-BUSY 的可用窗口极短（放不下作业）、CNC-OK 正常：主循环枚举到 CNC-BUSY 时 slot 为
    None 而 continue，最终落到 CNC-OK。
    """
    machines = (
        _machine("CNC-BUSY", available_start=DAY, available_end=_at(8, 5)),  # 只有 5 分钟
        _machine("CNC-OK"),
    )
    # 工序需 setup 10 + 20 = 30 分钟，装不进 5 分钟窗口。
    snap = _snapshot(orders=(_order("ORD-01"),), products=(_product(),), machines=machines)
    plan = generate_schedule(snap)
    assert plan.scheduled_jobs[0].machine_id == "CNC-OK"


def test_best_candidate_skips_downtime_blocked_slot() -> None:
    """候选槽位落在停机窗内 → `if machine.is_blocked_during: continue`（行 957）。

    CNC-DOWN 整个可用窗都被停机窗覆盖 → 每个槽位都被 blocked 而 continue；CNC-OK 正常兜住。
    """
    downtime = (DowntimeWindow(start=DAY, end=DAY_END, reason="MAINTENANCE"),)
    machines = (
        _machine("CNC-DOWN", downtime=downtime),
        _machine("CNC-OK"),
    )
    snap = _snapshot(orders=(_order("ORD-01"),), products=(_product(),), machines=machines)
    plan = generate_schedule(snap)
    assert plan.scheduled_jobs[0].machine_id == "CNC-OK"


def test_best_candidate_skips_absent_worker_slot() -> None:
    """候选槽位落在工人缺勤窗内 → `if worker.is_absent_during: continue`（行 959）。

    W-ABSENT 全天缺勤（技能匹配、班次够长，因此能进入槽位判定但被缺勤剔除）；W-OK 正常兜住。
    """
    workers = (
        _worker("W-ABSENT", absences=(TimeWindow(start=DAY, end=DAY_END),)),
        _worker("W-OK"),
    )
    snap = _snapshot(orders=(_order("ORD-01"),), products=(_product(),), workers=workers)
    plan = generate_schedule(snap)
    assert plan.scheduled_jobs[0].worker_id == "W-OK"


def test_worst_shortfall_and_unit_missing_material_via_generate() -> None:
    """`_worst_shortfall`（多缺口取最大）与 `_unit_of` 的物料缺失分支（`else ""`）。

    产品 BOM 引用一个快照里不存在的物料 MAT-GHOST → 主循环判 MATERIAL_INSUFFICIENT，
    `quantify` 调 `_worst_shortfall` 选出它、调 `_unit_of` 因物料不存在返回空串。
    """
    product = _product(bom=(BomLine(material_id="MAT-GHOST", quantity_per_unit=Decimal("1")),))
    snap = _snapshot(orders=(_order("ORD-01", quantity="10"),), products=(product,), materials=())
    plan = generate_schedule(snap)
    u = plan.unschedulable_jobs[0]
    assert u.blocking_reason == "MATERIAL_INSUFFICIENT"
    assert u.unblock_suggestion["material_id"] == "MAT-GHOST"
    assert u.unblock_suggestion["unit"] == ""  # 物料不存在 → 空单位
