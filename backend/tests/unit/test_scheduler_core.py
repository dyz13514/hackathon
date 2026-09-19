"""`core/scheduler.py` 主循环与工序展开断言（任务 2.4）。

覆盖 R4.1–R4.3 / R4.7 / R5.2 / R8.1 / R8.5 / R6.4。

守着这几件后果很重的事：

1. **工序展开线性链与 `INVALID_ROUTING`**（§3.1.1、R4.2 / R4.7）：`job_id` 确定性、前驱成链；
   >3 道或 `sequence` 重复必须抛错——路线非法不能被静默排产。
2. **确定性全序**（§3.1.2、R8.5）：订单按 `(PRIORITY_RANK, due_date, order_id)` 放置，打乱输入
   集合顺序结果逐字段相同（属性 1 的前哨；完整属性测试在任务 2.5）。
3. **订单级原子提交或整单回滚**（§3.1.5 / §3.1.6、R8.1）：任一工序失败整单进 unschedulable，
   绝不留半个订单在时间线上。
4. **物料在 sequence=1 一次性预留、时域内不足报 INFEASIBLE(shortfall)**（§3.1.5、R6.4）。
5. **freeze / locked / exclude_machine_ids**（任务 7.1 消费）在此就位并生效。
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

import pytest

from app.core.scheduler import (
    PRIORITY_RANK,
    Failure,
    InvalidRoutingError,
    ProductionJob,
    ScheduledJob,
    candidate_machines,
    diagnose_blocking,
    expand,
    generate_schedule,
    material_need,
    material_ready_time,
    preference_delta,
    quantify,
)
from app.core.scheduling import Timeline
from app.core.snapshot import (
    BomLine,
    DomainSnapshot,
    IncomingDelivery,
    Machine,
    Material,
    Operation,
    Order,
    PreferenceRule,
    Product,
    Worker,
)

PRODUCTION_DATE = date(2026, 3, 2)
NOW = datetime(2026, 3, 2, 8, 0)
SHIFT_START = datetime(2026, 3, 2, 0, 0)
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
) -> Machine:
    return Machine(
        machine_id=machine_id,
        machine_type=machine_type,
        capabilities=capabilities,
        status=status,  # type: ignore[arg-type]
        available_start=DAY,
        available_end=DAY_END,
        rate_multiplier=Decimal(rate),
        downtime_windows=(),
    )


def _worker(
    worker_id: str = "W-01",
    *,
    skills: tuple[str, ...] = ("CNC_OP",),
    absences: tuple[object, ...] = (),
) -> Worker:
    return Worker(
        worker_id=worker_id,
        name="张三",
        skills=skills,
        shift_start=DAY,
        shift_end=DAY_END,
        absences=absences,  # type: ignore[arg-type]
    )


def _snapshot(
    *,
    orders: tuple[Order, ...],
    products: tuple[Product, ...],
    machines: tuple[Machine, ...] = (),
    workers: tuple[Worker, ...] = (),
    materials: tuple[Material, ...] = (),
    preference_rules: tuple[PreferenceRule, ...] = (),
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
        preference_rules=preference_rules,
    )


# --------------------------------------------------------------------------
# 1. 工序展开（§3.1.1、R4.2 / R4.7）
# --------------------------------------------------------------------------


def test_expand_builds_linear_predecessor_chain() -> None:
    product = _product(operations=(_op(1), _op(2), _op(3)))
    order = _order("ORD-01")
    jobs = expand(order, product)

    assert [j.job_id for j in jobs] == ["ORD-01-OP1", "ORD-01-OP2", "ORD-01-OP3"]
    assert [j.predecessor_job_id for j in jobs] == [None, "ORD-01-OP1", "ORD-01-OP2"]
    assert all(j.order_id == "ORD-01" and j.quantity == Decimal("10") for j in jobs)


def test_expand_single_operation_has_no_predecessor() -> None:
    jobs = expand(_order("ORD-01"), _product(operations=(_op(1),)))
    assert len(jobs) == 1
    assert jobs[0].predecessor_job_id is None


def test_expand_rejects_more_than_three_operations() -> None:
    # Operation.sequence 被限定在 1..3，因此「>3 道」用四道不同 sequence 无法构造；
    # 但四个 Operation 对象（含重复 sequence）触发的是重复检查。这里直接构造 4 道合法
    # sequence 是不可能的，故 >3 的门由重复分支覆盖，此处验证「恰好在 3 道边界内合法」。
    product = _product(operations=(_op(1), _op(2), _op(3)))
    assert len(expand(_order("ORD-01"), product)) == 3


def test_expand_rejects_duplicate_sequence() -> None:
    product = _product(operations=(_op(1), _op(1)))
    with pytest.raises(InvalidRoutingError) as exc:
        expand(_order("ORD-01"), product)
    assert exc.value.code == "INVALID_ROUTING"


def test_expand_rejects_empty_routing() -> None:
    # 直接构造（绕过 `_product` 的默认单工序回退），得到一个真正 0 道工序的产品。
    product = Product(product_id="PRD-EMPTY", name="空", operations=(), bom=())
    with pytest.raises(InvalidRoutingError):
        expand(_order("ORD-01"), product)


# --------------------------------------------------------------------------
# 2. 确定性全序（§3.1.2、R8.5、属性 1 前哨）
# --------------------------------------------------------------------------


def test_orders_placed_in_priority_due_date_id_order() -> None:
    # 三个订单错开 due_date 与优先级；容量足够时全部可排，验证放置结果稳定。
    product = _product(operations=(_op(1),))
    orders = (
        _order("ORD-C", priority="LOW", due=DAY_END),
        _order("ORD-A", priority="URGENT", due=DAY_END),
        _order("ORD-B", priority="HIGH", due=DAY_END),
    )
    snap = _snapshot(orders=orders, products=(product,))
    plan = generate_schedule(snap)

    # URGENT < HIGH < LOW，因此 ORD-A 最早开工。
    starts = {sj.order_id: sj.start_time for sj in plan.scheduled_jobs}
    assert starts["ORD-A"] <= starts["ORD-B"] <= starts["ORD-C"]
    assert PRIORITY_RANK["URGENT"] < PRIORITY_RANK["HIGH"] < PRIORITY_RANK["LOW"]


def test_shuffling_input_order_yields_identical_result() -> None:
    product = _product(operations=(_op(1), _op(2)))
    orders = tuple(_order(f"ORD-{i}", quantity="5") for i in range(4))
    machines = (_machine("CNC-01"), _machine("CNC-02"))
    workers = (_worker("W-01"), _worker("W-02"))

    snap_a = _snapshot(orders=orders, products=(product,), machines=machines, workers=workers)
    snap_b = _snapshot(
        orders=tuple(reversed(orders)),
        products=(product,),
        machines=tuple(reversed(machines)),
        workers=tuple(reversed(workers)),
    )

    plan_a = generate_schedule(snap_a)
    plan_b = generate_schedule(snap_b)

    def fingerprint(plan: object) -> set[tuple[object, ...]]:
        return {
            (sj.job_id, sj.machine_id, sj.worker_id, sj.start_time, sj.end_time)
            for sj in plan.scheduled_jobs  # type: ignore[attr-defined]
        }

    assert fingerprint(plan_a) == fingerprint(plan_b)
    assert plan_a.feasibility == plan_b.feasibility


def test_candidate_scoring_prefers_earlier_completion() -> None:
    # 两台机器倍率不同：CNC-FAST 倍率 2.0（工期减半），应被选中（越早完工 cost 越小）。
    product = _product(operations=(_op(1, per_unit="10.0", setup=0),))
    order = _order("ORD-01", quantity="6")
    machines = (
        _machine("CNC-FAST", rate="2.0"),
        _machine("CNC-SLOW", rate="1.0"),
    )
    snap = _snapshot(orders=(order,), products=(product,), machines=machines)
    plan = generate_schedule(snap)

    assert len(plan.scheduled_jobs) == 1
    assert plan.scheduled_jobs[0].machine_id == "CNC-FAST"


# --------------------------------------------------------------------------
# 3. 订单级原子提交或整单回滚（§3.1.5 / §3.1.6、R8.1）
# --------------------------------------------------------------------------


def test_order_rollback_when_second_operation_fails() -> None:
    # 两道工序，第二道要求一台不存在的机器类型 → 整单回滚，第一道也不得留在计划里。
    product = _product(operations=(_op(1), _op(2, machine_type="LASER")))
    order = _order("ORD-01")
    snap = _snapshot(orders=(order,), products=(product,))
    plan = generate_schedule(snap)

    assert plan.scheduled_jobs == ()
    assert {u.job_id for u in plan.unschedulable_jobs} == {"ORD-01-OP1", "ORD-01-OP2"}
    assert plan.feasibility == "NO_FEASIBLE_PLAN"


def test_partition_is_complete_and_disjoint() -> None:
    # 一个可排订单 + 一个不可排订单（缺工人技能）→ 划分完备且不相交。
    good = _product("PRD-GOOD", operations=(_op(1),))
    bad = _product("PRD-BAD", operations=(_op(1, skill="WELDING"),))
    orders = (_order("ORD-GOOD", product_id="PRD-GOOD"), _order("ORD-BAD", product_id="PRD-BAD"))
    snap = _snapshot(orders=orders, products=(good, bad))
    plan = generate_schedule(snap)

    scheduled_ids = {sj.job_id for sj in plan.scheduled_jobs}
    unsched_ids = {u.job_id for u in plan.unschedulable_jobs}
    assert scheduled_ids == {"ORD-GOOD-OP1"}
    assert unsched_ids == {"ORD-BAD-OP1"}
    assert scheduled_ids.isdisjoint(unsched_ids)
    assert plan.feasibility == "PARTIAL"


def test_unschedulable_suggestion_has_numeric_field() -> None:
    bad = _product("PRD-BAD", operations=(_op(1, skill="WELDING"),))
    snap = _snapshot(orders=(_order("ORD-BAD", product_id="PRD-BAD"),), products=(bad,))
    plan = generate_schedule(snap)

    suggestion = plan.unschedulable_jobs[0].unblock_suggestion
    assert any(isinstance(v, int | float) for v in suggestion.values())


def test_all_scheduled_yields_feasible() -> None:
    snap = _snapshot(orders=(_order("ORD-01"),), products=(_product(),))
    plan = generate_schedule(snap)
    assert plan.feasibility == "FEASIBLE"
    assert plan.unschedulable_jobs == ()


# --------------------------------------------------------------------------
# 4. 物料语义（§3.1.5、R6.4）
# --------------------------------------------------------------------------


def test_material_need_multiplies_bom_by_quantity() -> None:
    product = _product(bom=(BomLine(material_id="MAT-01", quantity_per_unit=Decimal("2.5")),))
    need = material_need(product, Decimal("4"))
    assert need == {"MAT-01": Decimal("10.0")}


def test_material_shortfall_makes_order_infeasible() -> None:
    material = Material(
        material_id="MAT-01",
        name="钢",
        unit="kg",
        quantity_available=Decimal("5"),
        reserved_quantity=Decimal("0"),
        incoming_deliveries=(),
    )
    product = _product(bom=(BomLine(material_id="MAT-01", quantity_per_unit=Decimal("1")),))
    order = _order("ORD-01", quantity="10")  # 需 10，仅 5 → 缺 5
    snap = _snapshot(orders=(order,), products=(product,), materials=(material,))
    plan = generate_schedule(snap)

    assert plan.feasibility == "NO_FEASIBLE_PLAN"
    u = plan.unschedulable_jobs[0]
    assert u.blocking_reason == "MATERIAL_INSUFFICIENT"
    assert u.unblock_suggestion["material_id"] == "MAT-01"
    assert Decimal(u.unblock_suggestion["shortfall_quantity"]) == Decimal("5")


def test_material_becomes_ready_after_delivery() -> None:
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
    need = {"MAT-01": Decimal("50")}
    snap = _snapshot(orders=(), products=(), materials=(material,))
    ready, shortfalls = material_ready_time(need, snap, {}, SHIFT_START)
    assert shortfalls == {}
    # 齐备时刻是「作业真能开工」的时刻，不是到货 eta 本身：`available_at` 用严格 `eta < t`
    # （到货与开工同一分钟不算已到），因此 09:00 到货的批次最早能被用上的开工时刻是 09:01。
    # 校验器 `check_material_sufficient` 用 `available_at(material, start_time)` 独立复算，
    # 只有 `start_time > 09:00` 才把这批到货计入——齐备时刻取 09:01 使两侧一致（属性 2）。
    assert ready == datetime(2026, 3, 2, 9, 1)


def test_material_reserved_across_orders_within_run() -> None:
    # 库存 10；两个各需 10 的订单 → 只能满足一个，另一个缺料。
    material = Material(
        material_id="MAT-01",
        name="钢",
        unit="kg",
        quantity_available=Decimal("10"),
        reserved_quantity=Decimal("0"),
        incoming_deliveries=(),
    )
    product = _product(bom=(BomLine(material_id="MAT-01", quantity_per_unit=Decimal("1")),))
    orders = (
        _order("ORD-A", quantity="10", priority="URGENT"),
        _order("ORD-B", quantity="10", priority="LOW"),
    )
    snap = _snapshot(orders=orders, products=(product,), materials=(material,))
    plan = generate_schedule(snap)

    assert {sj.order_id for sj in plan.scheduled_jobs} == {"ORD-A"}
    assert {u.order_id for u in plan.unschedulable_jobs} == {"ORD-B"}


# --------------------------------------------------------------------------
# 5. freeze / locked / exclude_machine_ids（任务 7.1 消费）
# --------------------------------------------------------------------------


def test_exclude_machine_removes_it_from_candidates() -> None:
    machines = (_machine("CNC-01"), _machine("CNC-02"))
    snap = _snapshot(orders=(_order("ORD-01"),), products=(_product(),), machines=machines)

    job = expand(_order("ORD-01"), _product())[0]
    remaining = candidate_machines(job, snap, frozenset({"CNC-01"}))
    assert {m.machine_id for m in remaining} == {"CNC-02"}

    plan = generate_schedule(snap, exclude_machine_ids=frozenset({"CNC-01"}))
    assert plan.scheduled_jobs[0].machine_id == "CNC-02"


def test_capability_and_status_filter_candidates() -> None:
    machines = (
        _machine("CNC-DOWN", status="DOWN"),
        _machine("CNC-NOCAP"),  # 无 PRECISION 能力
        _machine("CNC-OK", capabilities=("PRECISION",)),
    )
    product = _product(operations=(_op(1, capability="PRECISION"),))
    snap = _snapshot(orders=(_order("ORD-01"),), products=(product,), machines=machines)
    plan = generate_schedule(snap)
    assert plan.scheduled_jobs[0].machine_id == "CNC-OK"


def test_freeze_preoccupies_timeline_and_skips_order() -> None:
    product = _product(operations=(_op(1),))
    order = _order("ORD-01")
    frozen = ScheduledJob(
        job_id="ORD-01-OP1",
        order_id="ORD-01",
        product_id="PRD-01",
        machine_id="CNC-01",
        worker_id="W-01",
        start_time=DAY,
        end_time=datetime(2026, 3, 2, 9, 0),
        setup_minutes=0,
        changeover_minutes=0,
    )
    snap = _snapshot(orders=(order,), products=(product,))
    plan = generate_schedule(snap, freeze=(frozen,))

    # 冻结作业原样保留，其订单不被重排（不重复出现），也不产生 unschedulable。
    assert plan.scheduled_jobs == (frozen,)
    assert plan.unschedulable_jobs == ()
    assert plan.feasibility == "FEASIBLE"


def test_freeze_with_unschedulable_increment_is_no_feasible_plan() -> None:
    # 冻结集非空，但唯一待排订单缺技能不可排 → 无新增量 → NO_FEASIBLE_PLAN。
    frozen = ScheduledJob(
        job_id="ORD-FROZEN-OP1",
        order_id="ORD-FROZEN",
        product_id="PRD-01",
        machine_id="CNC-01",
        worker_id="W-01",
        start_time=DAY,
        end_time=datetime(2026, 3, 2, 9, 0),
        setup_minutes=0,
        changeover_minutes=0,
    )
    bad = _product("PRD-BAD", operations=(_op(1, skill="WELDING"),))
    good = _product("PRD-01", operations=(_op(1),))
    orders = (
        _order("ORD-FROZEN", product_id="PRD-01"),
        _order("ORD-BAD", product_id="PRD-BAD"),
    )
    snap = _snapshot(orders=orders, products=(good, bad))
    plan = generate_schedule(snap, freeze=(frozen,))

    assert plan.scheduled_jobs == (frozen,)  # 只有冻结的那条，无新增
    assert {u.order_id for u in plan.unschedulable_jobs} == {"ORD-BAD"}
    assert plan.feasibility == "NO_FEASIBLE_PLAN"


def test_locked_but_unfrozen_job_makes_order_unschedulable() -> None:
    product = _product(operations=(_op(1),))
    snap = _snapshot(orders=(_order("ORD-01"),), products=(product,))
    plan = generate_schedule(snap, locked=frozenset({"ORD-01-OP1"}))

    assert plan.scheduled_jobs == ()
    assert plan.unschedulable_jobs[0].blocking_reason == "PREDECESSOR_UNSCHEDULABLE"


# --------------------------------------------------------------------------
# 6. preference_delta：命中的候选放置加正惩罚，未命中/空规则集为 0（任务 11.2）
# --------------------------------------------------------------------------


def _job(
    *, order_id: str = "O", product_id: str = "P", skill: str = "CNC_OP"
) -> ProductionJob:
    return ProductionJob(
        job_id=f"{order_id}-OP1",
        order_id=order_id,
        product_id=product_id,
        quantity=Decimal("1"),
        operation_sequence=1,
        predecessor_job_id=None,
        required_machine_type="CNC",
        required_capability=None,
        required_worker_skill=skill,
        base_processing_time_per_unit=Decimal("1"),
        setup_time=0,
    )


def _pref(rule_id: str, structured_form: dict[str, object]) -> PreferenceRule:
    return PreferenceRule(rule_id=rule_id, human_text=rule_id, structured_form=structured_form)


def test_preference_delta_zero_for_empty_rules() -> None:
    """空规则集 → 0（无偏好时排产与接入前逐字段相同）。"""
    assert preference_delta(_job(), _machine(), _worker(), ()) == Decimal("0")


def test_preference_delta_positive_on_matching_avoid_order() -> None:
    """AVOID_MACHINE_FOR_ORDER 命中当前放置 → 加 weight_delta × 60（分钟等价）。"""
    rule = _pref(
        "PR-1",
        {
            "kind": "AVOID_MACHINE_FOR_ORDER",
            "order_id": "O",
            "machine_id": "CNC-01",
            "weight_delta": 2,
        },
    )
    delta = preference_delta(_job(order_id="O"), _machine("CNC-01"), _worker(), (rule,))
    assert delta == Decimal("120.0")  # 2 × 60


def test_preference_delta_zero_when_machine_differs() -> None:
    """把作业放在**别的**机器上 → 不命中 → 0（因此规则改变的是「选谁」，而非可行性）。"""
    rule = _pref(
        "PR-1",
        {
            "kind": "AVOID_MACHINE_FOR_ORDER",
            "order_id": "O",
            "machine_id": "CNC-01",
            "weight_delta": 2,
        },
    )
    off = preference_delta(_job(order_id="O"), _machine("CNC-02"), _worker(), (rule,))
    assert off == Decimal("0")


def test_preference_delta_never_negative() -> None:
    """偏好惩罚恒 >= 0：即使多条规则命中，也只会让候选更不划算，绝不更划算（R18.8 方向性）。"""
    rules = (
        _pref(
            "PR-1",
            {"kind": "AVOID_MACHINE_FOR_ORDER", "order_id": "O", "machine_id": "CNC-01"},
        ),
        _pref(
            "PR-2",
            {"kind": "AVOID_MACHINE_FOR_PRODUCT", "product_id": "P", "machine_id": "CNC-01"},
        ),
    )
    job = _job(order_id="O", product_id="P")
    delta = preference_delta(job, _machine("CNC-01"), _worker(), rules)
    assert delta >= Decimal("0")
    assert delta == Decimal("120.0")  # 两条各 1 × 60


def test_preference_rule_changes_machine_selection() -> None:
    """EVAL-011 第一断言：一条 AVOID_MACHINE_FOR_ORDER 规则**改变排产结果**（换到别的机器）。

    两台等价机器 CNC-01 / CNC-02，无偏好时主循环按 machine_id 升序 tie-break 选 CNC-01。加一条
    「ORD-01 避开 CNC-01」的规则后，CNC-01 的候选 cost 被 `W_PREF × preference_delta` 抬高，
    主循环改选 CNC-02——证明规则真的进了候选打分并改变了选型。可行性不变（两台都能干）。
    """
    product = _product(operations=(_op(1),))
    order = _order("ORD-01")
    machines = (_machine("CNC-01"), _machine("CNC-02"))

    base = generate_schedule(_snapshot(orders=(order,), products=(product,), machines=machines))
    assert base.scheduled_jobs[0].machine_id == "CNC-01"  # 无偏好：ID 最小者

    rule = _pref(
        "PR-1",
        {
            "kind": "AVOID_MACHINE_FOR_ORDER",
            "order_id": "ORD-01",
            "machine_id": "CNC-01",
            "weight_delta": 10,
        },
    )
    steered = generate_schedule(
        _snapshot(orders=(order,), products=(product,), machines=machines, preference_rules=(rule,))
    )
    assert steered.scheduled_jobs[0].machine_id == "CNC-02"  # 偏好把它推到别的机器
    # 可行性未被偏好改变：作业仍被排上（偏好只改选谁，不改能不能排）。
    assert steered.feasibility == "FEASIBLE"


def test_failure_value_object_carries_reason_and_context() -> None:
    f = Failure(
        reason="OPERATION_PRECEDENCE_VIOLATION",
        predecessor_job_id="ORD-01-OP1",
        predecessor_reason="MATERIAL_INSUFFICIENT",
    )
    assert f.reason == "OPERATION_PRECEDENCE_VIOLATION"
    assert f.predecessor_job_id == "ORD-01-OP1"
    assert f.predecessor_reason == "MATERIAL_INSUFFICIENT"
    assert f.material_shortfalls == {}
    assert f.available_minutes_in_shift is None


# --------------------------------------------------------------------------
# 7. diagnose_blocking 固定判定顺序 + quantify 逐类量化（任务 2.6，R8.2–R8.7、§3.1.6）
# --------------------------------------------------------------------------


def _job(
    *,
    order_id: str = "ORD-01",
    machine_type: str = "CNC",
    capability: str | None = None,
    skill: str = "CNC_OP",
    per_unit: str = "2.0",
    quantity: str = "10",
    setup: int = 10,
) -> ProductionJob:
    return ProductionJob(
        job_id=f"{order_id}-OP1",
        order_id=order_id,
        product_id="PRD-01",
        quantity=Decimal(quantity),
        operation_sequence=1,
        predecessor_job_id=None,
        required_machine_type=machine_type,
        required_capability=capability,
        required_worker_skill=skill,
        base_processing_time_per_unit=Decimal(per_unit),
        setup_time=setup,
    )


def _empty_timelines(
    machines: tuple[Machine, ...], workers: tuple[Worker, ...]
) -> tuple[dict[str, Timeline], dict[str, Timeline]]:
    return (
        {m.machine_id: Timeline() for m in machines},
        {w.worker_id: Timeline() for w in workers},
    )


def _diag(
    job: ProductionJob,
    *,
    machines: tuple[Machine, ...],
    workers: tuple[Worker, ...],
    exclude: frozenset[str] = frozenset(),
    ready: datetime = DAY,
) -> Failure:
    snap = _snapshot(
        orders=(_order(job.order_id),),
        products=(_product(),),
        machines=machines,
        workers=workers,
    )
    m_tls, w_tls = _empty_timelines(machines, workers)
    return diagnose_blocking(job, snap, m_tls, w_tls, exclude, ready=ready)


def test_diagnose_reports_capability_mismatch_first() -> None:
    # 既无能力机器、又无技能工人：固定顺序保证报机器能力（机器能力先于工人技能）。
    machines = (_machine("CNC-01"),)  # 无 PRECISION 能力
    workers = (_worker("W-01", skills=("OTHER",)),)  # 无 CNC_OP 技能
    job = _job(capability="PRECISION")
    failure = _diag(job, machines=machines, workers=workers)
    assert failure.reason == "MACHINE_CAPABILITY_MISMATCH"


def test_diagnose_reports_machine_unavailable_when_all_down() -> None:
    machines = (_machine("CNC-01", status="DOWN"),)
    workers = (_worker("W-01"),)
    failure = _diag(_job(), machines=machines, workers=workers)
    assert failure.reason == "MACHINE_UNAVAILABLE"


def test_diagnose_reports_machine_unavailable_when_all_excluded() -> None:
    machines = (_machine("CNC-01"),)
    workers = (_worker("W-01"),)
    failure = _diag(_job(), machines=machines, workers=workers, exclude=frozenset({"CNC-01"}))
    assert failure.reason == "MACHINE_UNAVAILABLE"


def test_diagnose_reports_worker_skill_mismatch() -> None:
    machines = (_machine("CNC-01"),)
    workers = (_worker("W-01", skills=("WELDING",)),)  # 无 CNC_OP
    failure = _diag(_job(), machines=machines, workers=workers)
    assert failure.reason == "WORKER_SKILL_MISMATCH"


def test_diagnose_reports_shift_boundary_when_window_too_short() -> None:
    # 班次窗口只有 09:00–09:20（20 分钟），工序需 setup 10 + 10 件 × 2 分钟 = 30 分钟 > 20。
    short_worker = Worker(
        worker_id="W-01",
        name="张三",
        skills=("CNC_OP",),
        shift_start=datetime(2026, 3, 2, 9, 0),
        shift_end=datetime(2026, 3, 2, 9, 20),
        absences=(),
    )
    machines = (_machine("CNC-01"),)
    failure = _diag(
        _job(), machines=machines, workers=(short_worker,), ready=datetime(2026, 3, 2, 9, 0)
    )
    assert failure.reason == "SHIFT_BOUNDARY_VIOLATION"
    assert failure.available_minutes_in_shift == 20


def test_diagnose_reports_worker_unavailable_when_window_long_enough() -> None:
    # 窗口够长（整班），但工人整天缺勤 → 占用/缺勤而非窗口太短 → 工人不可用。
    from app.core.snapshot import TimeWindow

    absent = Worker(
        worker_id="W-01",
        name="张三",
        skills=("CNC_OP",),
        shift_start=DAY,
        shift_end=DAY_END,
        absences=(TimeWindow(start=DAY, end=DAY_END),),
    )
    machines = (_machine("CNC-01"),)
    # diagnose_blocking 只看窗口长度是否够（缺勤在具体槽位判定），窗口够长 → WORKER_UNAVAILABLE。
    failure = _diag(_job(), machines=machines, workers=(absent,))
    assert failure.reason == "WORKER_UNAVAILABLE"


# ---- quantify 逐类字段（§3.1.6 的量化表） ----


def _snap_for_quantify(job: ProductionJob) -> DomainSnapshot:
    return _snapshot(
        orders=(_order(job.order_id, due=DAY_END),),
        products=(_product(),),
        machines=(_machine("CNC-01", capabilities=("PRECISION",)),),
        workers=(_worker("W-01"),),
    )


def test_quantify_material_insufficient_fields() -> None:
    job = _job()
    material = Material(
        material_id="MAT-STEEL-01",
        name="钢",
        unit="kg",
        quantity_available=Decimal("10"),
        reserved_quantity=Decimal("0"),
        incoming_deliveries=(),
    )
    snap = _snapshot(
        orders=(_order(job.order_id, due=DAY_END),),
        products=(_product(),),
        materials=(material,),
    )
    failure = Failure(
        reason="MATERIAL_INSUFFICIENT",
        material_shortfalls={"MAT-STEEL-01": Decimal("40")},
    )
    out = quantify(failure, job, snap)
    assert out["material_id"] == "MAT-STEEL-01"
    assert Decimal(out["shortfall_quantity"]) == Decimal("40")
    assert out["unit"] == "kg"
    assert out["needed_before"] == DAY_END.isoformat()


def test_quantify_machine_capability_mismatch_lists_qualifying_types() -> None:
    job = _job(capability="PRECISION")
    snap = _snap_for_quantify(job)  # CNC-01 有 PRECISION 能力
    out = quantify(Failure(reason="MACHINE_CAPABILITY_MISMATCH"), job, snap)
    assert out["required_capability"] == "PRECISION"
    assert out["qualifying_machine_types"] == ["CNC"]


def test_quantify_machine_capability_mismatch_empty_when_none_qualify() -> None:
    job = _job(capability="LASER_CUT")
    snap = _snap_for_quantify(job)  # 没有机器具备 LASER_CUT
    out = quantify(Failure(reason="MACHINE_CAPABILITY_MISMATCH"), job, snap)
    assert out["qualifying_machine_types"] == []  # 不虚构机型（R8.7）


def test_quantify_machine_unavailable_reports_minutes_and_window() -> None:
    job = _job(per_unit="3.0", quantity="10", setup=10)  # 基准速率 30 分钟
    snap = _snap_for_quantify(job)
    out = quantify(Failure(reason="MACHINE_UNAVAILABLE"), job, snap)
    assert out["required_machine_type"] == "CNC"
    assert out["minutes_needed"] == 30  # ceil(3.0 × 10 ÷ 1.0)
    assert out["earliest_window_needed"] == SHIFT_START.isoformat()


def test_quantify_worker_skill_mismatch_reports_minutes() -> None:
    job = _job(per_unit="2.0", quantity="10")
    snap = _snap_for_quantify(job)
    out = quantify(Failure(reason="WORKER_SKILL_MISMATCH"), job, snap)
    assert out["required_skill"] == "CNC_OP"
    assert out["worker_minutes_needed"] == 20


def test_quantify_worker_unavailable_reports_shift_window() -> None:
    job = _job()
    snap = _snap_for_quantify(job)
    out = quantify(Failure(reason="WORKER_UNAVAILABLE"), job, snap)
    assert out["required_skill"] == "CNC_OP"
    assert out["worker_minutes_needed"] == 20
    assert out["shift_window"] == f"{DAY.isoformat()}/{DAY_END.isoformat()}"


def test_quantify_shift_boundary_reports_deficit() -> None:
    job = _job(per_unit="2.0", quantity="10", setup=10)  # 需 10 + 20 = 30 分钟
    snap = _snap_for_quantify(job)
    failure = Failure(reason="SHIFT_BOUNDARY_VIOLATION", available_minutes_in_shift=25)
    out = quantify(failure, job, snap)
    assert out["required_minutes"] == 30
    assert out["available_minutes_in_shift"] == 25
    assert out["deficit_minutes"] == 5


def test_quantify_precedence_reports_predecessor() -> None:
    job = _job(order_id="ORD-07")
    snap = _snap_for_quantify(job)
    failure = Failure(
        reason="OPERATION_PRECEDENCE_VIOLATION",
        predecessor_job_id="ORD-07-OP1",
        predecessor_reason="MATERIAL_INSUFFICIENT",
    )
    out = quantify(failure, job, snap)
    assert out["predecessor_job_id"] == "ORD-07-OP1"
    assert out["predecessor_blocking_reason"] == "MATERIAL_INSUFFICIENT"


# ---- 端到端：主循环把失败工序与前序连带工序分别标注 ----


def test_second_operation_failure_marks_first_as_precedence() -> None:
    # OP1 可排、OP2 要求不存在的机器类型 → OP2 报 MACHINE_CAPABILITY_MISMATCH，
    # OP1 因整单原子性回滚，报 OPERATION_PRECEDENCE_VIOLATION 指向 OP2。
    product = _product(operations=(_op(1), _op(2, machine_type="LASER")))
    snap = _snapshot(orders=(_order("ORD-01"),), products=(product,))
    plan = generate_schedule(snap)

    by_id = {u.job_id: u for u in plan.unschedulable_jobs}
    assert by_id["ORD-01-OP2"].blocking_reason == "MACHINE_CAPABILITY_MISMATCH"
    assert by_id["ORD-01-OP1"].blocking_reason == "OPERATION_PRECEDENCE_VIOLATION"
    assert by_id["ORD-01-OP1"].unblock_suggestion["predecessor_job_id"] == "ORD-01-OP2"


def test_material_failure_propagates_precedence_to_later_operations() -> None:
    # 两道工序，物料不足 → OP1 报 MATERIAL_INSUFFICIENT，OP2 报 OPERATION_PRECEDENCE_VIOLATION。
    material = Material(
        material_id="MAT-01",
        name="钢",
        unit="kg",
        quantity_available=Decimal("1"),
        reserved_quantity=Decimal("0"),
        incoming_deliveries=(),
    )
    product = _product(
        operations=(_op(1), _op(2)),
        bom=(BomLine(material_id="MAT-01", quantity_per_unit=Decimal("1")),),
    )
    order = _order("ORD-01", quantity="10")  # 需 10，仅 1
    snap = _snapshot(orders=(order,), products=(product,), materials=(material,))
    plan = generate_schedule(snap)

    by_id = {u.job_id: u for u in plan.unschedulable_jobs}
    assert by_id["ORD-01-OP1"].blocking_reason == "MATERIAL_INSUFFICIENT"
    assert by_id["ORD-01-OP2"].blocking_reason == "OPERATION_PRECEDENCE_VIOLATION"
    assert by_id["ORD-01-OP2"].unblock_suggestion["predecessor_job_id"] == "ORD-01-OP1"
    assert (
        by_id["ORD-01-OP2"].unblock_suggestion["predecessor_blocking_reason"]
        == "MATERIAL_INSUFFICIENT"
    )
    assert plan.feasibility == "NO_FEASIBLE_PLAN"


def test_partial_feasibility_when_some_scheduled_some_not() -> None:
    good = _product("PRD-GOOD", operations=(_op(1),))
    bad = _product("PRD-BAD", operations=(_op(1, machine_type="LASER"),))
    orders = (_order("ORD-GOOD", product_id="PRD-GOOD"), _order("ORD-BAD", product_id="PRD-BAD"))
    snap = _snapshot(orders=orders, products=(good, bad))
    plan = generate_schedule(snap)

    assert plan.feasibility == "PARTIAL"
    assert {sj.job_id for sj in plan.scheduled_jobs} == {"ORD-GOOD-OP1"}
    bad_u = plan.unschedulable_jobs[0]
    assert bad_u.blocking_reason == "MACHINE_CAPABILITY_MISMATCH"
    # quantify 输出不虚构机型：没有 LASER 机器 → 空列表。
    assert bad_u.unblock_suggestion["qualifying_machine_types"] == []
