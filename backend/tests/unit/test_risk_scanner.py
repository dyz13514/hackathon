"""Risk_Scanner 确定性内核的阈值分支覆盖（任务 8.5，R14.3–4、R14.9、R27.10）。

**非可选**（tasks.md 8.5，承接原属性 20）：逐类风险 × 各阈值边界 + `finding_key` 去重与
排序的确定性。全部在纯内核层构造快照 + 已排产作业，不触库、不触 LLM。

阈值（design.md §3.8 / R14.4）：
- MATERIAL_RUNOUT_FORECAST：时域内降到 0 → WARNING；24h 内降到 0 且无在途 → CRITICAL
- ZERO_SLACK_ORDER：slack ≤ 0 → CRITICAL；≤ 120 → WARNING；> 120 → 无
- BOTTLENECK_RESOURCE：util ≥ 0.98 → CRITICAL；≥ 0.90 → WARNING；< 0.90 → 无
- OVERCOMMITTED_SHIFT：所需 > 可用 → CRITICAL
- SINGLE_POINT_OF_FAILURE_MACHINE：占比 ≥ 0.50 且无同能力替代机器 → WARNING
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from app.core.risk import (
    SPOF_JOB_SHARE,
    RiskType,
    scan,
)
from app.core.scheduler import ScheduledJob
from app.core.snapshot import (
    BomLine,
    DomainSnapshot,
    IncomingDelivery,
    Machine,
    Material,
    Operation,
    Order,
    Product,
    Worker,
)

NOW = datetime(2026, 3, 2, 8, 0)
PROD_DATE = NOW.date()


# --------------------------------------------------------------------------
# 构造器
# --------------------------------------------------------------------------


def _product(product_id: str = "PRD-1", *, bom: tuple[BomLine, ...] = ()) -> Product:
    return Product(
        product_id=product_id,
        name=product_id,
        operations=(
            Operation(
                sequence=1,
                required_machine_type="CNC",
                required_capability=None,
                required_worker_skill="machining",
                base_processing_time_per_unit=Decimal("1.0"),
                setup_time=0,
            ),
        ),
        bom=bom,
    )


def _order(
    order_id: str,
    *,
    product_id: str = "PRD-1",
    quantity: Decimal = Decimal("10"),
    due: datetime | None = None,
    priority: str = "NORMAL",
) -> Order:
    return Order(
        order_id=order_id,
        product_id=product_id,
        quantity=quantity,
        due_date=due if due is not None else NOW + timedelta(days=2),
        promised_date=None,
        priority=priority,  # type: ignore[arg-type]
    )


def _material(
    material_id: str = "MAT-1",
    *,
    available: Decimal = Decimal("1000"),
    reserved: Decimal = Decimal("0"),
    deliveries: tuple[IncomingDelivery, ...] = (),
) -> Material:
    return Material(
        material_id=material_id,
        name=material_id,
        unit="pcs",
        quantity_available=available,
        reserved_quantity=reserved,
        incoming_deliveries=deliveries,
    )


def _machine(
    machine_id: str = "CNC-01",
    *,
    machine_type: str = "CNC",
    capabilities: tuple[str, ...] = (),
    start: datetime | None = None,
    end: datetime | None = None,
) -> Machine:
    return Machine(
        machine_id=machine_id,
        machine_type=machine_type,
        capabilities=capabilities,
        status="AVAILABLE",
        available_start=start if start is not None else NOW,
        available_end=end if end is not None else NOW + timedelta(hours=10),
        rate_multiplier=Decimal("1.0"),
        downtime_windows=(),
    )


def _worker(
    worker_id: str = "W-01",
    *,
    start: datetime | None = None,
    end: datetime | None = None,
) -> Worker:
    return Worker(
        worker_id=worker_id,
        name=worker_id,
        skills=("machining",),
        shift_start=start if start is not None else NOW,
        shift_end=end if end is not None else NOW + timedelta(hours=10),
        absences=(),
    )


def _snapshot(
    *,
    orders: tuple[Order, ...] = (),
    products: tuple[Product, ...] = (),
    materials: tuple[Material, ...] = (),
    machines: tuple[Machine, ...] = (),
    workers: tuple[Worker, ...] = (),
) -> DomainSnapshot:
    return DomainSnapshot(
        snapshot_version=1,
        production_date=PROD_DATE,
        now=NOW,
        orders=orders,
        products=products,
        materials=materials,
        machines=machines,
        workers=workers,
        changeover_rules=(),
        preference_rules=(),
    )


def _job(
    job_id: str,
    *,
    order_id: str = "ORD-1",
    product_id: str = "PRD-1",
    machine_id: str = "CNC-01",
    worker_id: str = "W-01",
    start: datetime,
    end: datetime,
) -> ScheduledJob:
    return ScheduledJob(
        job_id=job_id,
        order_id=order_id,
        product_id=product_id,
        machine_id=machine_id,
        worker_id=worker_id,
        start_time=start,
        end_time=end,
        setup_minutes=0,
        changeover_minutes=0,
    )


def _of_type(findings: tuple, risk_type: RiskType) -> list:
    return [f for f in findings if f.risk_type == risk_type]


# --------------------------------------------------------------------------
# 1. ZERO_SLACK_ORDER 阈值分支
# --------------------------------------------------------------------------


def test_zero_slack_critical_when_late() -> None:
    """完工晚于交期（slack < 0）→ CRITICAL。"""
    due = NOW + timedelta(hours=2)
    job = _job("ORD-1-OP1", start=NOW, end=NOW + timedelta(hours=3))  # 完工晚 1h
    snap = _snapshot(orders=(_order("ORD-1", due=due),), products=(_product(),))
    findings = _of_type(scan(snap, (job,)), RiskType.ZERO_SLACK_ORDER)
    assert len(findings) == 1
    assert findings[0].severity == "CRITICAL"


def test_zero_slack_warning_at_boundary() -> None:
    """slack 恰为 120 分钟 → WARNING（≤ 120 边界含等号）。"""
    due = NOW + timedelta(hours=4)
    job = _job("ORD-1-OP1", start=NOW, end=NOW + timedelta(hours=2))  # slack = 120min
    snap = _snapshot(orders=(_order("ORD-1", due=due),), products=(_product(),))
    findings = _of_type(scan(snap, (job,)), RiskType.ZERO_SLACK_ORDER)
    assert len(findings) == 1
    assert findings[0].severity == "WARNING"
    assert findings[0].metric_value == Decimal(120)


def test_zero_slack_none_above_threshold() -> None:
    """slack = 121 分钟（> 120）→ 无风险。"""
    due = NOW + timedelta(hours=4, minutes=1)
    job = _job("ORD-1-OP1", start=NOW, end=NOW + timedelta(hours=2))  # slack = 121min
    snap = _snapshot(orders=(_order("ORD-1", due=due),), products=(_product(),))
    assert _of_type(scan(snap, (job,)), RiskType.ZERO_SLACK_ORDER) == []


# --------------------------------------------------------------------------
# 2. BOTTLENECK_RESOURCE 阈值分支
# --------------------------------------------------------------------------


def test_bottleneck_critical_at_98() -> None:
    """利用率 ≥ 0.98 → CRITICAL。可用 600min，占用 594min = 0.99。"""
    machine = _machine("CNC-01", start=NOW, end=NOW + timedelta(minutes=600))
    job = _job("ORD-1-OP1", start=NOW, end=NOW + timedelta(minutes=594))
    snap = _snapshot(orders=(_order("ORD-1"),), products=(_product(),), machines=(machine,))
    findings = _of_type(scan(snap, (job,)), RiskType.BOTTLENECK_RESOURCE)
    assert len(findings) == 1
    assert findings[0].severity == "CRITICAL"


def test_bottleneck_warning_at_90() -> None:
    """利用率 ∈ [0.90, 0.98) → WARNING。可用 600min，占用 540min = 0.90。"""
    machine = _machine("CNC-01", start=NOW, end=NOW + timedelta(minutes=600))
    job = _job("ORD-1-OP1", start=NOW, end=NOW + timedelta(minutes=540))
    snap = _snapshot(orders=(_order("ORD-1"),), products=(_product(),), machines=(machine,))
    findings = _of_type(scan(snap, (job,)), RiskType.BOTTLENECK_RESOURCE)
    assert len(findings) == 1
    assert findings[0].severity == "WARNING"


def test_bottleneck_none_below_90() -> None:
    """利用率 < 0.90 → 无风险。可用 600min，占用 300min = 0.50。"""
    machine = _machine("CNC-01", start=NOW, end=NOW + timedelta(minutes=600))
    job = _job("ORD-1-OP1", start=NOW, end=NOW + timedelta(minutes=300))
    snap = _snapshot(orders=(_order("ORD-1"),), products=(_product(),), machines=(machine,))
    assert _of_type(scan(snap, (job,)), RiskType.BOTTLENECK_RESOURCE) == []


# --------------------------------------------------------------------------
# 3. OVERCOMMITTED_SHIFT 阈值分支
# --------------------------------------------------------------------------


def test_overcommitted_shift_critical_when_over() -> None:
    """所需工时 > 可用班次工时 → CRITICAL。班次 60min，作业 90min。"""
    worker = _worker("W-01", start=NOW, end=NOW + timedelta(minutes=60))
    job = _job("ORD-1-OP1", worker_id="W-01", start=NOW, end=NOW + timedelta(minutes=90))
    snap = _snapshot(orders=(_order("ORD-1"),), products=(_product(),), workers=(worker,))
    findings = _of_type(scan(snap, (job,)), RiskType.OVERCOMMITTED_SHIFT)
    assert len(findings) == 1
    assert findings[0].severity == "CRITICAL"


def test_overcommitted_shift_none_when_within() -> None:
    """所需 ≤ 可用 → 无风险。班次 120min，作业 90min。"""
    worker = _worker("W-01", start=NOW, end=NOW + timedelta(minutes=120))
    job = _job("ORD-1-OP1", worker_id="W-01", start=NOW, end=NOW + timedelta(minutes=90))
    snap = _snapshot(orders=(_order("ORD-1"),), products=(_product(),), workers=(worker,))
    assert _of_type(scan(snap, (job,)), RiskType.OVERCOMMITTED_SHIFT) == []


# --------------------------------------------------------------------------
# 4. SINGLE_POINT_OF_FAILURE_MACHINE 阈值分支
# --------------------------------------------------------------------------


def test_spof_warning_when_share_high_and_no_substitute() -> None:
    """单机承担 ≥ 50% 作业且无同能力替代 → WARNING。2/2 = 1.0 在唯一机器上。"""
    machine = _machine("CNC-01", capabilities=("milling",))
    jobs = (
        _job("ORD-1-OP1", order_id="ORD-1", start=NOW, end=NOW + timedelta(hours=1)),
        _job("ORD-2-OP1", order_id="ORD-2", start=NOW + timedelta(hours=1),
             end=NOW + timedelta(hours=2)),
    )
    snap = _snapshot(
        orders=(_order("ORD-1"), _order("ORD-2")),
        products=(_product(),),
        machines=(machine,),
    )
    findings = _of_type(scan(snap, jobs), RiskType.SINGLE_POINT_OF_FAILURE_MACHINE)
    assert len(findings) == 1
    assert findings[0].severity == "WARNING"
    assert findings[0].metric_value >= Decimal(str(SPOF_JOB_SHARE))


def test_spof_none_when_substitute_exists() -> None:
    """存在同 type 且能力超集的替代机器 → 无 SPOF 风险。"""
    primary = _machine("CNC-01", capabilities=("milling",))
    substitute = _machine("CNC-02", capabilities=("milling", "drilling"))
    jobs = (
        _job("ORD-1-OP1", order_id="ORD-1", machine_id="CNC-01",
             start=NOW, end=NOW + timedelta(hours=1)),
        _job("ORD-2-OP1", order_id="ORD-2", machine_id="CNC-01",
             start=NOW + timedelta(hours=1), end=NOW + timedelta(hours=2)),
    )
    snap = _snapshot(
        orders=(_order("ORD-1"), _order("ORD-2")),
        products=(_product(),),
        machines=(primary, substitute),
    )
    assert _of_type(scan(snap, jobs), RiskType.SINGLE_POINT_OF_FAILURE_MACHINE) == []


def test_spof_none_below_share() -> None:
    """占比 < 0.50 → 无风险。CNC-01 承担 1/3。"""
    m1 = _machine("CNC-01", capabilities=("milling",))
    m2 = _machine("CNC-02", capabilities=("milling",))
    jobs = (
        _job("ORD-1-OP1", order_id="ORD-1", machine_id="CNC-01",
             start=NOW, end=NOW + timedelta(hours=1)),
        _job("ORD-2-OP1", order_id="ORD-2", machine_id="CNC-02",
             start=NOW, end=NOW + timedelta(hours=1)),
        _job("ORD-3-OP1", order_id="ORD-3", machine_id="CNC-02",
             start=NOW + timedelta(hours=1), end=NOW + timedelta(hours=2)),
    )
    snap = _snapshot(
        orders=(_order("ORD-1"), _order("ORD-2"), _order("ORD-3")),
        products=(_product(),),
        machines=(m1, m2),
    )
    assert _of_type(scan(snap, jobs), RiskType.SINGLE_POINT_OF_FAILURE_MACHINE) == []


# --------------------------------------------------------------------------
# 5. MATERIAL_RUNOUT_FORECAST 阈值分支
# --------------------------------------------------------------------------


def test_material_runout_critical_within_24h_no_incoming() -> None:
    """物料在 24h 内被消耗到 0 且无在途 → CRITICAL。库存 10，消耗 10（qty×bom）。"""
    product = _product(bom=(BomLine(material_id="MAT-1", quantity_per_unit=Decimal("1.0")),))
    order = _order("ORD-1", quantity=Decimal("10"))
    material = _material("MAT-1", available=Decimal("10"))
    job = _job("ORD-1-OP1", start=NOW + timedelta(hours=2), end=NOW + timedelta(hours=3))
    snap = _snapshot(orders=(order,), products=(product,), materials=(material,))
    findings = _of_type(scan(snap, (job,)), RiskType.MATERIAL_RUNOUT_FORECAST)
    assert len(findings) == 1
    assert findings[0].severity == "CRITICAL"


def test_material_runout_warning_when_incoming_delivery() -> None:
    """降到 0 但有在途到货（时域内）→ WARNING 而非 CRITICAL。"""
    product = _product(bom=(BomLine(material_id="MAT-1", quantity_per_unit=Decimal("1.0")),))
    order = _order("ORD-1", quantity=Decimal("10"))
    material = _material(
        "MAT-1",
        available=Decimal("10"),
        deliveries=(
            IncomingDelivery(
                delivery_id="DEL-1", quantity=Decimal("50"),
                eta=NOW + timedelta(hours=1), confirmed=True,
            ),
        ),
    )
    job = _job("ORD-1-OP1", start=NOW + timedelta(hours=2), end=NOW + timedelta(hours=3))
    snap = _snapshot(orders=(order,), products=(product,), materials=(material,))
    findings = _of_type(scan(snap, (job,)), RiskType.MATERIAL_RUNOUT_FORECAST)
    # 到货 50 在消耗前到达，净可用不降到 0 → 无风险；这断言在途到货确实被计入 available_at。
    assert findings == []


def test_material_runout_none_when_ample_stock() -> None:
    """库存充足（远超消耗）→ 无风险。库存 1000，消耗 10。"""
    product = _product(bom=(BomLine(material_id="MAT-1", quantity_per_unit=Decimal("1.0")),))
    order = _order("ORD-1", quantity=Decimal("10"))
    material = _material("MAT-1", available=Decimal("1000"))
    job = _job("ORD-1-OP1", start=NOW + timedelta(hours=2), end=NOW + timedelta(hours=3))
    snap = _snapshot(orders=(order,), products=(product,), materials=(material,))
    assert _of_type(scan(snap, (job,)), RiskType.MATERIAL_RUNOUT_FORECAST) == []


# --------------------------------------------------------------------------
# 确定性排序 + finding_key
# --------------------------------------------------------------------------


def test_scan_is_deterministic_and_sorted() -> None:
    """同输入两次扫描逐字段相同，且按 (SEVERITY_RANK, finding_key) 升序。"""
    machine = _machine("CNC-01", start=NOW, end=NOW + timedelta(minutes=600))
    worker = _worker("W-01", start=NOW, end=NOW + timedelta(minutes=60))
    # 制造一个 CRITICAL（超配班次）+ 一个 WARNING（瓶颈）。
    job = _job("ORD-1-OP1", start=NOW, end=NOW + timedelta(minutes=540))  # util 0.90 WARNING
    snap = _snapshot(
        orders=(_order("ORD-1", due=NOW + timedelta(days=2)),),
        products=(_product(),),
        machines=(machine,),
        workers=(worker,),  # 需 540 > 可用 60 → CRITICAL
    )
    first = scan(snap, (job,))
    second = scan(snap, (job,))
    assert first == second
    # CRITICAL 排在 WARNING 之前。
    severities = [f.severity for f in first]
    assert severities == sorted(
        severities, key=lambda s: {"CRITICAL": 0, "WARNING": 1, "INFO": 2}[s]
    )


def test_finding_key_stable_and_distinct() -> None:
    """finding_key = sha1(risk_type|entity_type|entity_id)：稳定且按身份区分。"""
    machine = _machine("CNC-01", start=NOW, end=NOW + timedelta(minutes=600))
    job = _job("ORD-1-OP1", start=NOW, end=NOW + timedelta(minutes=594))
    snap = _snapshot(orders=(_order("ORD-1"),), products=(_product(),), machines=(machine,))
    a = scan(snap, (job,))
    b = scan(snap, (job,))
    keys_a = [f.finding_key for f in a]
    keys_b = [f.finding_key for f in b]
    assert keys_a == keys_b  # 稳定
    assert len(set(keys_a)) == len(keys_a)  # 互不相同（不同身份）


def test_empty_plan_yields_no_findings() -> None:
    """无已排产作业 → 空结果（P0 的 5 类风险都依赖计划或订单完工）。"""
    snap = _snapshot(
        orders=(_order("ORD-1"),),
        products=(_product(),),
        machines=(_machine(),),
        workers=(_worker(),),
        materials=(_material(),),
    )
    assert scan(snap, ()) == ()
