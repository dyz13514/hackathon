"""`core/baseline.py` FCFS 基线排产器断言（任务 2.11，R19.2 / R19.3 / R5.4）。

守着 design.md §3.4 的两组后果很重的事：

1. **三处刻意退化**——基线不是"另一个优化器"，是"没有系统时的排法"：
   ① 订单只按 `(due_date, order_id)` 排，**忽略 `priority`**（仅置换 priority 值不改变结果，
      Property 37 后半段）；② 每道工序取 **ID 最小**的可行机器/工人，不打分选优；③ 换型照常
      物理插入，基线不为减少换型而调整选型。
2. **同输入同口径**——`BaselineResult.snapshot_version` 原样透传自快照，`assert_same_version_as`
   在版本不一致时抛错（R19.2、Property 37 前半段）。

以及与正式计划一致的**订单原子性**与**确定性可重现**（同输入两次运行逐字段相同）。
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

import pytest

from app.core.baseline import BaselineResult, fcfs
from app.core.snapshot import (
    BomLine,
    DomainSnapshot,
    Machine,
    Material,
    Operation,
    Order,
    Product,
    Worker,
)

PRODUCTION_DATE = date(2026, 3, 2)
NOW = datetime(2026, 3, 2, 8, 0)
DAY = datetime(2026, 3, 2, 8, 0)
DAY_END = datetime(2026, 3, 2, 18, 0)
DUE_EARLY = datetime(2026, 3, 3, 0, 0)
DUE_LATE = datetime(2026, 3, 5, 0, 0)


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
) -> Worker:
    return Worker(
        worker_id=worker_id,
        name="张三",
        skills=skills,
        shift_start=DAY,
        shift_end=DAY_END,
        absences=(),
    )


def _material(material_id: str, *, available: str, reserved: str = "0") -> Material:
    return Material(
        material_id=material_id,
        name="钢",
        unit="kg",
        quantity_available=Decimal(available),
        reserved_quantity=Decimal(reserved),
        incoming_deliveries=(),
    )


def _snapshot(
    *,
    orders: tuple[Order, ...],
    products: tuple[Product, ...],
    machines: tuple[Machine, ...] = (),
    workers: tuple[Worker, ...] = (),
    materials: tuple[Material, ...] = (),
    snapshot_version: int = 7,
) -> DomainSnapshot:
    return DomainSnapshot(
        snapshot_version=snapshot_version,
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


# --------------------------------------------------------------------------
# 退化 ①：忽略 priority，只按 (due_date, order_id) 排
# --------------------------------------------------------------------------


def test_fcfs_orders_by_due_date_then_id_ignoring_priority() -> None:
    """两单只有一台机器抢占，基线按 due_date 决定谁先排——即便后者优先级更高。"""
    early = _order("ORD-A", priority="LOW", due=DUE_EARLY)
    late = _order("ORD-B", priority="URGENT", due=DUE_LATE)
    snap = _snapshot(orders=(late, early), products=(_product(),))

    result = fcfs(snap)
    jobs = {sj.order_id: sj for sj in result.plan.scheduled_jobs}

    # 交期早的先占最早槽位，尽管它 LOW；URGENT 的那单排在后面。
    assert jobs["ORD-A"].start_time <= jobs["ORD-B"].start_time


def test_fcfs_invariant_under_priority_permutation() -> None:
    """仅置换订单 `priority` 值，基线结果逐字段不变（Property 37 后半段、R19.3）。

    正式计划会因 priority 改变放置顺序；基线不看 priority，因此结果必须一模一样。
    """
    orders_a = (
        _order("ORD-A", priority="LOW", due=DUE_EARLY),
        _order("ORD-B", priority="URGENT", due=DUE_LATE),
    )
    # 同两单，交换优先级值（其余字段不动）。
    orders_b = (
        _order("ORD-A", priority="URGENT", due=DUE_EARLY),
        _order("ORD-B", priority="LOW", due=DUE_LATE),
    )
    product = _product()
    plan_a = fcfs(_snapshot(orders=orders_a, products=(product,))).plan
    plan_b = fcfs(_snapshot(orders=orders_b, products=(product,))).plan

    assert plan_a.scheduled_jobs == plan_b.scheduled_jobs
    assert plan_a.unschedulable_jobs == plan_b.unschedulable_jobs
    assert plan_a.feasibility == plan_b.feasibility


# --------------------------------------------------------------------------
# 退化 ②：取 ID 最小的可行机器/工人，不打分选优
# --------------------------------------------------------------------------


def test_fcfs_picks_lowest_id_feasible_machine_and_worker() -> None:
    """两台等价机器 + 两个等价工人，基线取 ID 最小的 CNC-01 / W-01，不比较。"""
    snap = _snapshot(
        orders=(_order("ORD-01"),),
        products=(_product(),),
        machines=(_machine("CNC-02"), _machine("CNC-01")),
        workers=(_worker("W-02"), _worker("W-01")),
    )
    result = fcfs(snap)
    sj = result.plan.scheduled_jobs[0]
    assert sj.machine_id == "CNC-01"
    assert sj.worker_id == "W-01"


def test_fcfs_skips_infeasible_lowest_id_machine() -> None:
    """ID 最小的机器停机（DOWN）时，基线取下一台可行的——退化是"第一台可行"，不是"第一台"。"""
    snap = _snapshot(
        orders=(_order("ORD-01"),),
        products=(_product(),),
        machines=(_machine("CNC-01", status="DOWN"), _machine("CNC-02")),
    )
    result = fcfs(snap)
    assert result.plan.scheduled_jobs[0].machine_id == "CNC-02"


# --------------------------------------------------------------------------
# 退化 ③：换型照常物理插入
# --------------------------------------------------------------------------


def test_fcfs_inserts_changeover_between_different_products() -> None:
    """同机上先后排两种产品，第二道工序的 setup 含默认换型（30 min，物理插入）。"""
    prd_a = _product("PRD-A", operations=(_op(1, setup=10),))
    prd_b = _product("PRD-B", operations=(_op(1, setup=10),))
    # 两单交期不同以固定顺序：A 先、B 后，落在同一台唯一机器上。
    orders = (
        _order("ORD-A", product_id="PRD-A", due=DUE_EARLY),
        _order("ORD-B", product_id="PRD-B", due=DUE_LATE),
    )
    snap = _snapshot(orders=orders, products=(prd_a, prd_b))
    result = fcfs(snap)
    by_order = {sj.order_id: sj for sj in result.plan.scheduled_jobs}

    # 两单都排上，且后排的 B 承担一次换型（changeover_minutes > 0）。
    assert set(by_order) == {"ORD-A", "ORD-B"}
    assert by_order["ORD-B"].changeover_minutes > 0
    assert by_order["ORD-B"].setup_minutes == 10 + by_order["ORD-B"].changeover_minutes


# --------------------------------------------------------------------------
# 基线不应用任何 PreferenceRule
# --------------------------------------------------------------------------


def test_fcfs_ignores_preference_rules() -> None:
    """快照带偏好规则时，基线结果与不带偏好规则时逐字段相同（基线不打分、不解释偏好）。"""
    from app.core.snapshot import PreferenceRule

    orders = (_order("ORD-01"),)
    products = (_product(),)
    machines = (_machine("CNC-01"), _machine("CNC-02"))

    with_pref = DomainSnapshot(
        snapshot_version=7,
        production_date=PRODUCTION_DATE,
        now=NOW,
        orders=orders,
        products=products,
        materials=(),
        machines=machines,
        workers=(_worker(),),
        changeover_rules=(),
        preference_rules=(
            PreferenceRule(
                rule_id="PR-01",
                human_text="尽量避免用 CNC-01",
                structured_form={"type": "AVOID_MACHINE", "machine_id": "CNC-01"},
            ),
        ),
    )
    without_pref = with_pref.model_copy(update={"preference_rules": ()})

    assert fcfs(with_pref).plan.scheduled_jobs == fcfs(without_pref).plan.scheduled_jobs
    # 基线仍取 ID 最小的 CNC-01，偏好"避免 CNC-01"未生效。
    assert fcfs(with_pref).plan.scheduled_jobs[0].machine_id == "CNC-01"


# --------------------------------------------------------------------------
# 订单原子性
# --------------------------------------------------------------------------


def test_fcfs_order_is_atomic_on_material_shortage() -> None:
    """物料不足时整单进 unschedulable，绝不留半个订单（R8.1、design.md §3.1.5）。"""
    product = _product(
        operations=(_op(1), _op(2)),
        bom=(BomLine(material_id="MAT-01", quantity_per_unit=Decimal("5.0")),),
    )
    # 需求 = 5 × 10 = 50，可用 10，缺口 40。
    snap = _snapshot(
        orders=(_order("ORD-01"),),
        products=(product,),
        materials=(_material("MAT-01", available="10"),),
    )
    result = fcfs(snap)
    assert result.plan.feasibility == "NO_FEASIBLE_PLAN"
    assert len(result.plan.scheduled_jobs) == 0
    # 两道工序都进 unschedulable，每条都有 blocking_reason（R8.4）。
    reasons = {u.job_id: u.blocking_reason for u in result.plan.unschedulable_jobs}
    assert reasons == {
        "ORD-01-OP1": "MATERIAL_INSUFFICIENT",
        "ORD-01-OP2": "OPERATION_PRECEDENCE_VIOLATION",
    }


def test_fcfs_partial_when_one_order_schedulable_other_not() -> None:
    """一单可排、一单缺料 → PARTIAL；可排单不受不可排单影响。"""
    ok_product = _product("PRD-OK")
    bad_product = _product(
        "PRD-BAD",
        bom=(BomLine(material_id="MAT-01", quantity_per_unit=Decimal("100.0")),),
    )
    snap = _snapshot(
        orders=(
            _order("ORD-OK", product_id="PRD-OK", due=DUE_EARLY),
            _order("ORD-BAD", product_id="PRD-BAD", due=DUE_LATE),
        ),
        products=(ok_product, bad_product),
        materials=(_material("MAT-01", available="0"),),
    )
    result = fcfs(snap)
    assert result.plan.feasibility == "PARTIAL"
    scheduled_orders = {sj.order_id for sj in result.plan.scheduled_jobs}
    assert scheduled_orders == {"ORD-OK"}


# --------------------------------------------------------------------------
# 确定性可重现（属性 1 前哨）
# --------------------------------------------------------------------------


def test_fcfs_is_deterministic_across_input_ordering() -> None:
    """打乱订单集合顺序，基线结果逐字段相同（确定性全序，R5.7）。"""
    o1 = _order("ORD-01", due=DUE_EARLY)
    o2 = _order("ORD-02", due=DUE_LATE)
    o3 = _order("ORD-03", due=DUE_EARLY)
    product = _product()
    plan_x = fcfs(_snapshot(orders=(o1, o2, o3), products=(product,))).plan
    plan_y = fcfs(_snapshot(orders=(o3, o1, o2), products=(product,))).plan
    assert plan_x.scheduled_jobs == plan_y.scheduled_jobs
    assert plan_x.unschedulable_jobs == plan_y.unschedulable_jobs


# --------------------------------------------------------------------------
# 同输入同口径：snapshot_version 透传与断言（R19.2、Property 37 前半段）
# --------------------------------------------------------------------------


def test_fcfs_passes_through_snapshot_version() -> None:
    """`BaselineResult.snapshot_version` 原样来自快照，不被 fcfs 改写。"""
    snap = _snapshot(orders=(_order("ORD-01"),), products=(_product(),), snapshot_version=42)
    assert fcfs(snap).snapshot_version == 42


def test_assert_same_version_passes_when_equal() -> None:
    """基线与正式计划同版本 → 断言通过（无异常）。"""
    result = BaselineResult(
        plan=fcfs(_snapshot(orders=(_order("ORD-01"),), products=(_product(),))).plan,
        snapshot_version=9,
    )
    result.assert_same_version_as(9)  # 不抛错


def test_assert_same_version_raises_when_mismatched() -> None:
    """基线与正式计划版本不一致 → 抛 ValueError（同口径不成立，宁可炸掉，R19.2）。"""
    result = BaselineResult(
        plan=fcfs(_snapshot(orders=(_order("ORD-01"),), products=(_product(),))).plan,
        snapshot_version=9,
    )
    with pytest.raises(ValueError, match="snapshot_version"):
        result.assert_same_version_as(10)
