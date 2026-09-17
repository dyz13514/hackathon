"""`core/snapshot.py` 的纯模型与纯语义断言（任务 2.1，R5.6 / R5.7 / R6.3）。

三组性质，各自守着一件后果很重的事：

1. **冻结**——`frozen=True` 是沙箱第 1 层隔离的载体（design.md §3.7，任务 8.1 依赖它）。
   赋值必须抛错，变体必须只能经 `model_copy(deep=True, update=...)` 得到，且变体与原件
   互不影响。若哪天有人为了方便把 `frozen` 去掉，沙箱推演就能改到调用方手里的那份快照，
   而这不会让任何别的测试变红。
2. **`available_at`**——排产器与校验器**唯一**共用的物料语义（design.md §3.2）。它的两个
   边界（`eta < t` 严格不等、关于 `t` 单调不减）是 R6.3 的全部内容；共用一个实现的意义正
   在于这两条只需要被断言一次。
3. **引用完整性预检**——R5.6 要求「列出**全部**错误引用」。快速失败在这里是错的：坏引用
   总是成批出现（一次导入、一次批次回滚），逐个报会让规划员修一处跑一次。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.core.snapshot import (
    BomLine,
    DataIntegrityError,
    DomainSnapshot,
    DowntimeWindow,
    IncomingDelivery,
    Machine,
    Material,
    Operation,
    Order,
    PreferenceRule,
    Product,
    TimeWindow,
    Worker,
    available_at,
    check_referential_integrity,
    require_referential_integrity,
)

NOW = datetime(2026, 3, 2, 8, 0)
PRODUCTION_DATE = date(2026, 3, 2)


# --------------------------------------------------------------------------
# 构造夹具（全部为纯数据，不碰库）
# --------------------------------------------------------------------------


def _material(
    material_id: str = "MAT-01",
    *,
    available: str = "100",
    reserved: str = "10",
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


def _product(product_id: str = "PRD-01", *, material_ids: tuple[str, ...] = ("MAT-01",)) -> Product:
    return Product(
        product_id=product_id,
        name="加固支架",
        operations=(
            Operation(
                sequence=1,
                required_machine_type="CNC",
                required_capability="PRECISION_MILLING",
                required_worker_skill="CNC_OPERATION",
                base_processing_time_per_unit=Decimal("4.0"),
                setup_time=20,
            ),
        ),
        bom=tuple(
            BomLine(material_id=material_id, quantity_per_unit=Decimal("2.0"))
            for material_id in material_ids
        ),
    )


def _order(order_id: str = "ORD-001", *, product_id: str = "PRD-01") -> Order:
    return Order(
        order_id=order_id,
        product_id=product_id,
        quantity=Decimal("10"),
        due_date=NOW + timedelta(days=2),
        promised_date=None,
        priority="NORMAL",
    )


def _machine(machine_id: str = "CNC-01") -> Machine:
    return Machine(
        machine_id=machine_id,
        machine_type="CNC",
        capabilities=("PRECISION_MILLING", "DEEP_DRILLING"),
        status="AVAILABLE",
        available_start=NOW,
        available_end=NOW + timedelta(hours=12),
        rate_multiplier=Decimal("1.0"),
        downtime_windows=(
            DowntimeWindow(
                start=NOW + timedelta(hours=4),
                end=NOW + timedelta(hours=6),
                reason="MAINTENANCE",
            ),
        ),
    )


def _worker(worker_id: str = "W-01") -> Worker:
    return Worker(
        worker_id=worker_id,
        name="Anna Petrova",
        skills=("CNC_OPERATION",),
        shift_start=NOW,
        shift_end=NOW + timedelta(hours=9),
        absences=(TimeWindow(start=NOW + timedelta(hours=2), end=NOW + timedelta(hours=3)),),
    )


def _snapshot(
    *,
    orders: tuple[Order, ...] | None = None,
    products: tuple[Product, ...] | None = None,
    materials: tuple[Material, ...] | None = None,
) -> DomainSnapshot:
    return DomainSnapshot(
        snapshot_version=7,
        production_date=PRODUCTION_DATE,
        now=NOW,
        orders=orders if orders is not None else (_order(),),
        products=products if products is not None else (_product(),),
        materials=materials if materials is not None else (_material(),),
        machines=(_machine(),),
        workers=(_worker(),),
        changeover_rules=(),
        preference_rules=(),
    )


# --------------------------------------------------------------------------
# ① 冻结与变体
# --------------------------------------------------------------------------


def test_snapshot_assignment_raises_validation_error() -> None:
    """给快照字段赋值即抛 `ValidationError`（沙箱第 1 层隔离，任务 8.1 依赖）。"""
    snapshot = _snapshot()

    with pytest.raises(ValidationError):
        snapshot.snapshot_version = 8  # type: ignore[misc]  # 正是要证明它不被允许
    with pytest.raises(ValidationError):
        snapshot.now = NOW + timedelta(hours=1)  # type: ignore[misc]


def test_nested_models_are_frozen_too() -> None:
    """嵌套模型同样冻结。只冻顶层等于没冻——写入点几乎总在嵌套层。"""
    snapshot = _snapshot()

    with pytest.raises(ValidationError):
        snapshot.orders[0].quantity = Decimal("999")  # type: ignore[misc]
    with pytest.raises(ValidationError):
        snapshot.materials[0].quantity_available = Decimal("0")  # type: ignore[misc]
    with pytest.raises(ValidationError):
        snapshot.machines[0].status = "DOWN"  # type: ignore[misc]


def test_collection_fields_are_tuples_so_they_cannot_be_appended_to() -> None:
    """集合字段是 `tuple`：连绕过属性赋值的 `append` 也不存在。"""
    snapshot = _snapshot()

    assert isinstance(snapshot.orders, tuple)
    assert isinstance(snapshot.products, tuple)
    assert isinstance(snapshot.materials, tuple)
    assert isinstance(snapshot.machines, tuple)
    assert isinstance(snapshot.workers, tuple)
    assert isinstance(snapshot.products[0].operations, tuple)
    assert isinstance(snapshot.materials[0].incoming_deliveries, tuple)
    assert not hasattr(snapshot.orders, "append")


def test_variants_come_from_model_copy_and_leave_the_original_intact() -> None:
    """变体只能经 `model_copy(deep=True, update=...)`，且原件逐字段不变。

    这是沙箱得到「假如 CNC-01 停机」这类情景的唯一途径（design.md §3.7）。深拷贝那一步
    不是保险：`deep=False` 会让变体与原件共享嵌套元组，情景推演随后就能污染真实快照。
    """
    original = _snapshot()
    variant = original.model_copy(
        deep=True,
        update={"materials": (_material(available="0", reserved="0"),)},
    )

    assert variant.materials[0].quantity_available == Decimal("0")
    assert original.materials[0].quantity_available == Decimal("100")
    assert variant.snapshot_version == original.snapshot_version
    assert variant.machines[0] is not original.machines[0], "deep=True 未生效，嵌套对象被共享"


def test_snapshot_rejects_undeclared_fields() -> None:
    """`extra="forbid"`：加载器多传一个字段就会失败，而不是把它悄悄带进内核。"""
    with pytest.raises(ValidationError):
        DomainSnapshot(
            snapshot_version=1,
            production_date=PRODUCTION_DATE,
            now=NOW,
            orders=(),
            products=(),
            materials=(),
            machines=(),
            workers=(),
            changeover_rules=(),
            preference_rules=(),
            weights={"tardiness": 1.0},  # type: ignore[call-arg]  # 任务 2.10 才有这个字段
        )


def test_preference_rule_carries_structured_form_verbatim() -> None:
    """偏好规则的 `structured_form` 原样携带，内核此刻不解释它（任务 11.x）。"""
    rule = PreferenceRule(
        rule_id="PR-001",
        human_text="精加工优先排在 CNC-02",
        structured_form={"kind": "MACHINE_PREFERENCE", "machine_id": "CNC-02"},
    )
    assert rule.structured_form["machine_id"] == "CNC-02"
    with pytest.raises(ValidationError):
        rule.rule_id = "PR-002"  # type: ignore[misc]


# --------------------------------------------------------------------------
# ② available_at
# --------------------------------------------------------------------------


def test_available_at_without_deliveries_is_on_hand_minus_reserved() -> None:
    """无在途到货时 `available_at = quantity_available − reserved_quantity`（R6.3）。"""
    material = _material(available="380", reserved="20")
    assert available_at(material, NOW) == Decimal("360")


def test_available_at_counts_only_deliveries_strictly_before_t() -> None:
    """`eta < t` 严格成立：到货与开工同一时刻，物料不算已到。

    严格不等号必须两侧一致——校验器用 `job.start_time` 调同一个函数，若两处对端点的处理
    不同，校验器就会否掉排产器刚刚排出的、刚好卡在到货时刻的作业。
    """
    eta = NOW + timedelta(hours=2)
    material = _material(
        available="100",
        reserved="0",
        deliveries=(
            IncomingDelivery(
                delivery_id="DLV-001", quantity=Decimal("50"), eta=eta, confirmed=False
            ),
        ),
    )

    assert available_at(material, eta - timedelta(minutes=1)) == Decimal("100")
    assert available_at(material, eta) == Decimal("100"), "eta == t 不得计入"
    assert available_at(material, eta + timedelta(minutes=1)) == Decimal("150")


def test_available_at_is_monotone_non_decreasing_in_t() -> None:
    """关于 `t` 单调不减：时间往后走，可用量只可能增加（到货只增不减库存）。"""
    material = _material(
        available="10",
        reserved="0",
        deliveries=(
            IncomingDelivery(
                delivery_id="DLV-A",
                quantity=Decimal("5"),
                eta=NOW + timedelta(hours=1),
                confirmed=True,
            ),
            IncomingDelivery(
                delivery_id="DLV-B",
                quantity=Decimal("7"),
                eta=NOW + timedelta(hours=3),
                confirmed=False,
            ),
        ),
    )

    probes = [NOW + timedelta(minutes=30 * step) for step in range(9)]
    values = [available_at(material, probe) for probe in probes]

    assert values == sorted(values), f"available_at 关于 t 不单调：{values}"
    assert values[0] == Decimal("10")
    assert values[-1] == Decimal("22")


def test_available_at_can_go_negative_and_does_not_compensate() -> None:
    """预留超过库存时返回负数：只报缺口，不补足（R6.4）。"""
    material = _material(available="5", reserved="40")
    assert available_at(material, NOW) == Decimal("-35")


def test_available_at_returns_decimal_not_float() -> None:
    """返回 `Decimal`。浮点会让「同输入不同结果」在最后一位有效数字上偶发（R5.7）。"""
    material = _material(available="0.3", reserved="0.1")
    result = available_at(material, NOW)
    assert isinstance(result, Decimal)
    assert result == Decimal("0.2")


# --------------------------------------------------------------------------
# ③ 引用完整性预检
# --------------------------------------------------------------------------


def test_intact_snapshot_passes_the_precheck() -> None:
    snapshot = _snapshot()
    assert check_referential_integrity(snapshot) == ()
    assert require_referential_integrity(snapshot) is snapshot


def test_precheck_lists_every_bad_reference_not_just_the_first() -> None:
    """R5.6：列出**全部**错误引用。两类各两处，四条都要在。"""
    snapshot = _snapshot(
        orders=(
            _order("ORD-001", product_id="PRD-MISSING"),
            _order("ORD-002", product_id="PRD-ALSO-MISSING"),
            _order("ORD-003", product_id="PRD-01"),
        ),
        products=(_product("PRD-01", material_ids=("MAT-01", "MAT-GONE", "MAT-VANISHED")),),
        materials=(_material("MAT-01"),),
    )

    references = check_referential_integrity(snapshot)
    missing = {(ref.entity_id, ref.missing_id) for ref in references}

    assert missing == {
        ("ORD-001", "PRD-MISSING"),
        ("ORD-002", "PRD-ALSO-MISSING"),
        ("PRD-01", "MAT-GONE"),
        ("PRD-01", "MAT-VANISHED"),
    }


def test_precheck_result_order_is_independent_of_input_order() -> None:
    """清单顺序只由排序键决定：它会进 API 响应与审计，不能随元组顺序漂移。"""
    products = (_product("PRD-01", material_ids=("MAT-Z", "MAT-A")),)
    forward = _snapshot(orders=(), products=products, materials=())
    reversed_bom = _snapshot(
        orders=(),
        products=(_product("PRD-01", material_ids=("MAT-A", "MAT-Z")),),
        materials=(),
    )

    assert [ref.missing_id for ref in check_referential_integrity(forward)] == ["MAT-A", "MAT-Z"]
    assert check_referential_integrity(forward) == check_referential_integrity(reversed_bom)


def test_require_referential_integrity_raises_with_code_and_details() -> None:
    """失败抛 `DataIntegrityError`，`code` 为 `DATA_INTEGRITY_ERROR`，`details` 可直接进响应。"""
    snapshot = _snapshot(orders=(_order("ORD-001", product_id="PRD-MISSING"),))

    with pytest.raises(DataIntegrityError) as excinfo:
        require_referential_integrity(snapshot)

    error = excinfo.value
    assert error.code == "DATA_INTEGRITY_ERROR"
    assert len(error.references) == 1
    assert "PRD-MISSING" in str(error)
    assert error.details()["bad_references"][0]["missing_type"] == "Product"


# --------------------------------------------------------------------------
# 辅助语义：时间窗与索引
# --------------------------------------------------------------------------


def test_time_window_is_half_open() -> None:
    """`[start, end)`：16:00 结束的保养窗与 16:00 开始的作业不冲突。"""
    window = TimeWindow(start=NOW, end=NOW + timedelta(hours=2))

    assert window.contains(NOW)
    assert not window.contains(NOW + timedelta(hours=2))
    assert window.overlaps(NOW + timedelta(hours=1), NOW + timedelta(hours=3))
    assert not window.overlaps(NOW + timedelta(hours=2), NOW + timedelta(hours=3))
    assert not window.overlaps(NOW - timedelta(hours=1), NOW)


def test_machine_and_worker_eligibility_helpers() -> None:
    """能力 / 技能 / 停机 / 缺勤的判定（design.md §3.1.2 的候选枚举条件）。"""
    machine = _machine()
    worker = _worker()

    assert machine.has_capability("DEEP_DRILLING")
    assert machine.has_capability(None), "工序无能力要求时任何机器都合格"
    assert not machine.has_capability("SURFACE_FINISH")
    assert machine.is_blocked_during(NOW + timedelta(hours=5), NOW + timedelta(hours=7))
    assert not machine.is_blocked_during(NOW, NOW + timedelta(hours=4))

    assert worker.has_skill("CNC_OPERATION")
    assert not worker.has_skill("WELDING")
    assert worker.is_absent_during(NOW + timedelta(hours=2), NOW + timedelta(hours=4))
    assert not worker.is_absent_during(NOW + timedelta(hours=3), NOW + timedelta(hours=4))


def test_indexes_look_up_by_id() -> None:
    """五个索引各查一次。"""
    snapshot = _snapshot()

    assert snapshot.products_by_id()["PRD-01"].name == "加固支架"
    assert snapshot.materials_by_id()["MAT-01"].unit == "kg"
    assert snapshot.machines_by_id()["CNC-01"].machine_type == "CNC"
    assert snapshot.workers_by_id()["W-01"].name == "Anna Petrova"
    assert snapshot.orders_by_id()["ORD-001"].priority == "NORMAL"


def test_indexes_are_rebuilt_so_variants_never_see_a_stale_index() -> None:
    """索引不缓存：`model_copy(update=...)` 后的变体查到的是**新**集合。

    缓存住在实例 `__dict__` 里，而 `model_copy` 会把 `__dict__` 整个拷过去。若索引被缓存，
    沙箱的「假如这台机器停机」变体就会拿着缓存里那台没停机的机器算下去——不抛异常，只是
    算错（design.md §3.7 的情景推演正是靠这个调用）。
    """
    original = _snapshot()
    assert original.machines_by_id()["CNC-01"].status == "AVAILABLE"

    stopped = _machine().model_copy(update={"status": "DOWN"})
    variant = original.model_copy(deep=True, update={"machines": (stopped,)})

    assert variant.machines_by_id()["CNC-01"].status == "DOWN"
    assert original.machines_by_id()["CNC-01"].status == "AVAILABLE"


def test_operations_in_sequence_sorts_by_sequence() -> None:
    """工序按 `sequence` 升序：这是工艺语义上的全序，与「不规范化集合顺序」不冲突。"""
    def op(sequence: int) -> Operation:
        return Operation(
            sequence=sequence,
            required_machine_type="CNC",
            required_capability=None,
            required_worker_skill="CNC_OPERATION",
            base_processing_time_per_unit=Decimal("1"),
            setup_time=0,
        )

    product = Product(
        product_id="PRD-X", name="乱序路线", operations=(op(3), op(1), op(2)), bom=()
    )
    assert [operation.sequence for operation in product.operations_in_sequence()] == [1, 2, 3]


def test_operation_sequence_is_bounded_to_three() -> None:
    """`sequence` ∈ 1..3（R4.1）。第 4 道工序在快照边界就被拒。"""
    with pytest.raises(ValidationError):
        Operation(
            sequence=4,
            required_machine_type="CNC",
            required_capability=None,
            required_worker_skill="CNC_OPERATION",
            base_processing_time_per_unit=Decimal("1"),
            setup_time=0,
        )
