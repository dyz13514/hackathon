"""黄金路径评估用例 EVAL-001 至 EVAL-008（任务 12.2，**非可选**）。

design.md「已裁剪的 32 条属性与其替代覆盖」表把这批用例定为被裁剪属性的主要覆盖来源，
因此**全部非可选**：任何一条失败都不允许以「属性测试覆盖过」为由跳过（tasks.md 12.2）。

## 为什么走确定性内核而非 Bedrock

八条用例覆盖的都是**确定性内核**的行为（排产可行性、前后序与换型、扰动重排、缺料诊断），
它们与 LLM 无关。因此本文件在 `LLM_MODE=REPLAY` 下运行时**零 Bedrock 调用、零 cassette
依赖**：计划生成走确定性流水线（`Trace.mode == PIPELINE`，token 汇总为 0），扰动重排直接
调用内核 `replan()`（纯函数、不碰 I/O）。这与骨架自检 `test_eval_harness.py` 同一路径。

## 两种驱动方式

- **纯内核**（EVAL-002/003/004/005）：手工构造最小 `DomainSnapshot`（沿用 `test_replanner.py`
  的构造风格），直接调用 `generate_schedule` / `replan`，对可行性、受影响集、churn、换型等
  逐字段断言。最小快照让「期望值」可手算、可复现，不受演示数据规模漂移影响。
- **端到端流水线 + 内核**（EVAL-001/006/007/008）：既用最小快照对内核结果做精确断言，也在
  设了 `eval_seeded` 的用例里驱动 `run_plan_generation`，证明确定性流水线在 REPLAY 下把
  同一类结果落到 `PENDING_APPROVAL` 计划、且 token 恒为 0。

每条用例的 docstring 首行都以 `EVAL-00X:` 起头，`eval-report`（R26.4）据此按业务编号组织。
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.core.delta import compute_plan_delta
from app.core.replanner import Disruption, replan
from app.core.scheduler import PlanCandidate, ScheduledJob, generate_schedule
from app.core.snapshot import (
    BomLine,
    ChangeoverRule,
    DomainSnapshot,
    Machine,
    Material,
    Operation,
    Order,
    Product,
    TimeWindow,
    Worker,
)
from app.core.validation import validate
from app.db.models import Trace
from app.orchestrator.pipelines import plan_generation
from tests.eval.conftest import EVAL_NOW, EVAL_PRODUCTION_DATE

# --------------------------------------------------------------------------
# 最小快照构造（沿用 tests/unit/test_replanner.py 的风格，纯数据、不碰库）
# --------------------------------------------------------------------------

DAY = datetime(2026, 3, 2, 8, 0)
DAY_END = datetime(2026, 3, 2, 18, 0)
PRODUCTION_DATE = date(2026, 3, 2)


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
        name="产品",
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
    absences: tuple[TimeWindow, ...] = (),
) -> Worker:
    return Worker(
        worker_id=worker_id,
        name="工人",
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
    changeover_rules: tuple[ChangeoverRule, ...] = (),
) -> DomainSnapshot:
    return DomainSnapshot(
        snapshot_version=1,
        production_date=PRODUCTION_DATE,
        now=DAY,
        orders=orders,
        products=products,
        materials=materials,
        machines=machines or (_machine(),),
        workers=workers or (_worker(),),
        changeover_rules=changeover_rules,
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
    return PlanCandidate(scheduled_jobs=tuple(jobs), unschedulable_jobs=(), feasibility="FEASIBLE")


def _sequence_of(job_id: str) -> int:
    return int(job_id.rsplit("-OP", 1)[1])


# ==========================================================================
# EVAL-001 —— FEASIBLE + 零违反
# ==========================================================================


def test_eval_001_feasible_plan_has_zero_violations() -> None:
    """EVAL-001: 资源充足的输入产出 `FEASIBLE` 计划，且全量硬约束校验零违反。

    最小快照：两个单工序订单、一台能做它们的机器、一个具备技能的工人，资源绰绰有余。
    断言 `feasibility == "FEASIBLE"`（无不可排产作业）、独立的 `validate` 报告零违反
    （排产器与校验器独立实现，双向印证），且每个作业都进了排产结果。
    """
    snap = _snapshot(
        orders=(_order("ORD-A", quantity="5"), _order("ORD-B", quantity="5")),
        products=(_product(),),
        machines=(_machine("CNC-01"),),
        workers=(_worker("W-01"),),
    )
    candidate = generate_schedule(snap)

    assert candidate.feasibility == "FEASIBLE"
    assert candidate.unschedulable_jobs == ()
    assert len(candidate.scheduled_jobs) == 2

    report = validate(candidate, snap)
    assert report.is_feasible is True
    assert report.violations == ()


def test_eval_001_pipeline_persists_feasible_plan_zero_tokens(
    eval_seeded: sessionmaker[Session], eval_engine: Engine
) -> None:
    """EVAL-001（端到端）: 流水线在 REPLAY 下落 `PENDING_APPROVAL`、校验零违反、token 恒 0。

    演示 seed 的整体可行性是 `PARTIAL`（有一单缺钢），但**已排产部分**必须零违反——这正是
    流水线内 `check_constraints` 步骤的独立复核对象。这里断言：计划状态硬编码
    `PENDING_APPROVAL`、`validation.is_feasible` 为真（已排产部分无硬约束违反）、且这条路径
    的 `Trace` 是 `PIPELINE`、token 汇总为 0（零 Bedrock 消耗的证据）。
    """
    with eval_seeded() as session:
        result = plan_generation.run_plan_generation(
            session,
            now=EVAL_NOW,
            production_date=EVAL_PRODUCTION_DATE,
            session_id="eval-001",
        )

    assert result.status == "PENDING_APPROVAL"
    assert result.validation.is_feasible is True
    assert result.validation.violations == ()
    assert len(result.candidate.scheduled_jobs) > 0

    with eval_engine.connect() as conn:
        trace = conn.execute(
            select(Trace).where(Trace.trace_id == result.generated_by_trace_id)
        ).one()
    assert trace.mode == "PIPELINE"
    assert trace.total_input_tokens == 0
    assert trace.total_output_tokens == 0


# ==========================================================================
# EVAL-002 —— 前后序正确 + 换型正确插入
# ==========================================================================


def test_eval_002_predecessor_order_and_changeover_inserted() -> None:
    """EVAL-002: 多工序订单按前后序排产（后序不早于前序完工），换型分钟正确插入。

    构造两件事在同一份快照里：

    1. **前后序**：一个两工序订单 `ORD-A`（OP1→OP2），断言 OP2 的开工时刻 **不早于** OP1 的
       完工时刻（线性工艺链，R4.2），且 OP2 因 `setup_time` 在 `setup_minutes` 里体现换型/
       装夹时长。
    2. **换型**：同一台机器上先做 `PRD-X` 再做 `PRD-Y`，命中一条 `ChangeoverRule`
       （PRD-X→PRD-Y，30 分钟）。断言排在后面的 `PRD-Y` 作业 `changeover_minutes == 30`，
       而首个作业无前驱产品、`changeover_minutes == 0`。
    """
    # --- 1. 前后序 ---
    prod_a = _product(
        "PRD-A", operations=(_op(1, per_unit="1.0", setup=10), _op(2, per_unit="1.0", setup=20))
    )
    snap_seq = _snapshot(
        orders=(_order("ORD-A", product_id="PRD-A", quantity="5"),), products=(prod_a,)
    )
    cand_seq = generate_schedule(snap_seq)

    by_id = {sj.job_id: sj for sj in cand_seq.scheduled_jobs}
    assert {"ORD-A-OP1", "ORD-A-OP2"} <= by_id.keys()
    assert by_id["ORD-A-OP2"].start_time >= by_id["ORD-A-OP1"].end_time  # 后序不早于前序完工
    assert by_id["ORD-A-OP2"].setup_minutes == 20  # OP2 的装夹/换型时长进 setup_minutes

    # --- 2. 换型 ---
    prod_x = _product("PRD-X", operations=(_op(1, per_unit="1.0"),))
    prod_y = _product("PRD-Y", operations=(_op(1, per_unit="1.0"),))
    rule = ChangeoverRule(
        rule_id="CR-XY",
        machine_id="CNC-01",
        from_product_id="PRD-X",
        to_product_id="PRD-Y",
        changeover_minutes=30,
        specificity=3,
    )
    snap_co = _snapshot(
        orders=(
            _order("ORD-X", product_id="PRD-X", quantity="5", priority="HIGH"),
            _order("ORD-Y", product_id="PRD-Y", quantity="5", priority="NORMAL"),
        ),
        products=(prod_x, prod_y),
        machines=(_machine("CNC-01"),),
        workers=(_worker("W-01"),),
        changeover_rules=(rule,),
    )
    cand_co = generate_schedule(snap_co)
    ordered = sorted(cand_co.scheduled_jobs, key=lambda sj: sj.start_time)

    assert cand_co.feasibility == "FEASIBLE"
    assert ordered[0].product_id == "PRD-X"
    assert ordered[0].changeover_minutes == 0  # 首个作业无前驱产品，无换型
    assert ordered[1].product_id == "PRD-Y"
    assert ordered[1].changeover_minutes == 30  # 命中 PRD-X→PRD-Y 换型规则


# ==========================================================================
# EVAL-003 —— 故障重排（替代机器 + churn_ratio ≤ 0.20）
# ==========================================================================


def test_eval_003_breakdown_reschedules_to_substitute_with_low_churn() -> None:
    """EVAL-003: 机器故障后重排到替代机，其余作业冻结不动，`churn_ratio ≤ 0.20`（K-05）。

    `ACTIVE` 计划有 6 个作业：1 个在故障机 `CNC-01` 上，5 个在 `CNC-02` 上。`CNC-01`
    整机故障后，唯有那一个作业需要换机；`CNC-03` 是具备能力的替代机。断言：

    - 那个受影响作业重排到替代机（`machine_id` 变为 `CNC-03`），且明确报告「有替代机」
      （`substitute_unavailable_job_ids` 为空）；
    - 其余 5 个作业**逐字段不变**（冻结先于优化）；
    - `churn_ratio == 1/6 ≈ 0.167 ≤ 0.20`——freeze-before-optimize 的直接产物；
    - 重排后全量校验可行。
    """
    active = _active(
        _sj("ORD-A-OP1", "ORD-A", machine_id="CNC-01", worker_id="W-01",
            start=DAY, end=datetime(2026, 3, 2, 9, 0)),
        _sj("ORD-B-OP1", "ORD-B", machine_id="CNC-02", worker_id="W-02",
            start=DAY, end=datetime(2026, 3, 2, 9, 0)),
        _sj("ORD-C-OP1", "ORD-C", machine_id="CNC-02", worker_id="W-02",
            start=datetime(2026, 3, 2, 9, 0), end=datetime(2026, 3, 2, 10, 0)),
        _sj("ORD-D-OP1", "ORD-D", machine_id="CNC-02", worker_id="W-03",
            start=datetime(2026, 3, 2, 10, 0), end=datetime(2026, 3, 2, 11, 0)),
        _sj("ORD-E-OP1", "ORD-E", machine_id="CNC-02", worker_id="W-03",
            start=datetime(2026, 3, 2, 11, 0), end=datetime(2026, 3, 2, 12, 0)),
        _sj("ORD-F-OP1", "ORD-F", machine_id="CNC-02", worker_id="W-02",
            start=datetime(2026, 3, 2, 12, 0), end=datetime(2026, 3, 2, 13, 0)),
    )
    orders = tuple(_order(oid) for oid in ("ORD-A", "ORD-B", "ORD-C", "ORD-D", "ORD-E", "ORD-F"))
    snap = _snapshot(
        orders=orders,
        products=(_product(),),
        machines=(_machine("CNC-01"), _machine("CNC-02"), _machine("CNC-03")),
        workers=(_worker("W-01"), _worker("W-02"), _worker("W-03")),
    )

    result = replan(active, Disruption(type="MACHINE_BREAKDOWN", machine_id="CNC-01"), snap)
    replanned = {sj.job_id: sj for sj in result.candidate.scheduled_jobs}

    # 受影响作业换到替代机，且报告「有替代机」。
    assert result.affected_job_ids == ("ORD-A-OP1",)
    assert replanned["ORD-A-OP1"].machine_id == "CNC-03"
    assert result.substitute_unavailable_job_ids == ()

    # 其余 5 个作业逐字段不变（冻结先于优化）。
    original = {sj.job_id: sj for sj in active.scheduled_jobs}
    for job_id in ("ORD-B-OP1", "ORD-C-OP1", "ORD-D-OP1", "ORD-E-OP1", "ORD-F-OP1"):
        assert replanned[job_id] == original[job_id]

    # churn_ratio ≤ 0.20（K-05）。
    delta = compute_plan_delta(active, result.candidate)
    assert delta.churn_ratio <= 0.20
    assert result.report.is_feasible is True


# ==========================================================================
# EVAL-004 —— 加急插单（URGENT 前置 + 被推迟订单）
# ==========================================================================


def test_eval_004_urgent_order_scheduled_first_defers_others() -> None:
    """EVAL-004: 加急订单被前置排产，与之竞争资源的普通订单被推迟。

    一台机器、一个工人上有两个订单：一个 `URGENT`、一个 `NORMAL`，二者不可能同时开工。
    确定性排产器按优先级全序（`URGENT` 秩最小）先排加急单。断言：

    - 加急单 `ORD-U` 从班次起点 08:00 开工（拿到最早的时槽）；
    - 普通单 `ORD-N` 被推迟到加急单完工之后（`start_time` 严格晚于加急单，且不早于其完工）；
    - 两单都排上、计划可行（推迟不等于丢弃，被推迟订单仍在计划里）。
    """
    snap = _snapshot(
        orders=(
            _order("ORD-N", priority="NORMAL", quantity="30"),
            _order("ORD-U", priority="URGENT", quantity="10"),
        ),
        products=(_product(),),
        machines=(_machine("CNC-01"),),
        workers=(_worker("W-01"),),
    )
    candidate = generate_schedule(snap)
    by_order = {sj.order_id: sj for sj in candidate.scheduled_jobs}

    assert candidate.feasibility == "FEASIBLE"
    assert {"ORD-U", "ORD-N"} <= by_order.keys()  # 两单都排上，被推迟单未被丢弃
    assert by_order["ORD-U"].start_time == DAY  # 加急单拿到最早时槽
    # 普通单被推迟：开工晚于加急单，且不早于加急单完工（资源互斥）。
    assert by_order["ORD-N"].start_time >= by_order["ORD-U"].end_time
    assert by_order["ORD-N"].start_time > by_order["ORD-U"].start_time


# ==========================================================================
# EVAL-005 —— 工人缺席重排
# ==========================================================================


def test_eval_005_worker_absence_reschedules_to_substitute_worker() -> None:
    """EVAL-005: 工人整日缺席后，其作业重排给具备同技能的替代工人，计划仍可行。

    `ACTIVE` 计划里 `ORD-A-OP1` 由 `W-01` 执行。快照已反映扰动：`W-01` 全天缺勤，`W-02`
    具备同技能且在岗。`WORKER_UNAVAILABLE` 扰动使该作业进重排。断言：

    - 该作业被列入受影响集；
    - 重排后仍被排产（未丢弃），执行工人改为 `W-02`（`reassigned`）；
    - 全量校验可行。
    """
    active = _active(_sj("ORD-A-OP1", "ORD-A", machine_id="CNC-01", worker_id="W-01"))
    snap = _snapshot(
        orders=(_order("ORD-A"),),
        products=(_product(),),
        machines=(_machine("CNC-01"),),
        workers=(
            _worker("W-01", absences=(TimeWindow(start=DAY, end=DAY_END),)),
            _worker("W-02"),
        ),
    )

    result = replan(active, Disruption(type="WORKER_UNAVAILABLE", worker_id="W-01"), snap)
    replanned = {sj.job_id: sj for sj in result.candidate.scheduled_jobs}

    assert result.affected_job_ids == ("ORD-A-OP1",)
    assert "ORD-A-OP1" in replanned  # 未丢弃
    assert replanned["ORD-A-OP1"].worker_id == "W-02"  # 换到替代工人

    delta = compute_plan_delta(active, result.candidate)
    assert "ORD-A-OP1" in delta.reassigned
    assert result.report.is_feasible is True


# ==========================================================================
# EVAL-006 —— 物料短缺（不虚构库存 + 输出缺口）
# ==========================================================================


def test_eval_006_material_shortage_reports_gap_without_fabricating_inventory() -> None:
    """EVAL-006: 物料不足时报确切缺口、不虚构库存补足。

    单个订单需 10 件钢（每件 1kg），库存仅 3kg、无在途到货。排产器不假设任何未登记补料
    （R6.4/R8.7）：该作业进 `unschedulable`，`blocking_reason == MATERIAL_INSUFFICIENT`，
    解锁建议量化出缺口 `shortfall_quantity == "7"`（= 10 需求 − 3 库存），并附单位与
    「需在何时前到位」。断言里核对缺口正是需求减库存——即「不虚构库存」的可观测形式。
    """
    material = Material(
        material_id="MAT-01",
        name="钢",
        unit="kg",
        quantity_available=Decimal("3"),
        reserved_quantity=Decimal("0"),
        incoming_deliveries=(),
    )
    product = _product(
        "PRD-S",
        operations=(_op(1),),
        bom=(BomLine(material_id="MAT-01", quantity_per_unit=Decimal("1")),),
    )
    snap = _snapshot(
        orders=(_order("ORD-A", product_id="PRD-S", quantity="10"),),
        products=(product,),
        materials=(material,),
    )
    candidate = generate_schedule(snap)

    assert candidate.scheduled_jobs == ()  # 缺料 → 无法排产，且不虚构补料
    unsched = {u.job_id: u for u in candidate.unschedulable_jobs}
    assert "ORD-A-OP1" in unsched
    suggestion = unsched["ORD-A-OP1"].unblock_suggestion
    assert unsched["ORD-A-OP1"].blocking_reason == "MATERIAL_INSUFFICIENT"
    assert suggestion["material_id"] == "MAT-01"
    assert Decimal(suggestion["shortfall_quantity"]) == Decimal("7")  # 10 需求 − 3 库存
    assert suggestion["unit"] == "kg"
    assert "needed_before" in suggestion


# ==========================================================================
# EVAL-007 —— PARTIAL + 每项量化解锁建议
# ==========================================================================


def test_eval_007_partial_plan_quantifies_every_unlock_suggestion() -> None:
    """EVAL-007: 部分可行时，可行订单被排产、不可行订单各带一份**量化**解锁建议。

    两个订单：`ORD-OK`（物料充足）与 `ORD-BAD`（需 10kg 钢、仅 2kg）。结果必为 `PARTIAL`
    （既有已排产、也有不可排产）。断言：

    - `feasibility == "PARTIAL"`，可行单 `ORD-OK` 进 `scheduled_jobs`；
    - **每一个** `unschedulable_jobs` 都带非空、含数值的 `unblock_suggestion`（R8.3：逐项
      量化，不允许空建议）；`ORD-BAD` 的缺口量化为 8kg（= 10 − 2）。
    """
    mat_ok = Material(
        material_id="MAT-OK", name="铝", unit="kg", quantity_available=Decimal("1000"),
        reserved_quantity=Decimal("0"), incoming_deliveries=(),
    )
    mat_bad = Material(
        material_id="MAT-BAD", name="钢", unit="kg", quantity_available=Decimal("2"),
        reserved_quantity=Decimal("0"), incoming_deliveries=(),
    )
    prod_ok = _product(
        "PRD-OK",
        operations=(_op(1),),
        bom=(BomLine(material_id="MAT-OK", quantity_per_unit=Decimal("1")),),
    )
    prod_bad = _product(
        "PRD-BAD",
        operations=(_op(1),),
        bom=(BomLine(material_id="MAT-BAD", quantity_per_unit=Decimal("1")),),
    )
    snap = _snapshot(
        orders=(
            _order("ORD-OK", product_id="PRD-OK", quantity="5"),
            _order("ORD-BAD", product_id="PRD-BAD", quantity="10"),
        ),
        products=(prod_ok, prod_bad),
        materials=(mat_ok, mat_bad),
    )
    candidate = generate_schedule(snap)

    assert candidate.feasibility == "PARTIAL"
    scheduled_orders = {sj.order_id for sj in candidate.scheduled_jobs}
    assert "ORD-OK" in scheduled_orders
    assert len(candidate.unschedulable_jobs) >= 1

    # 每个不可排产作业都必须带一份非空、含数值字段的量化建议（R8.3）。
    for unsched in candidate.unschedulable_jobs:
        assert unsched.blocking_reason  # 非空原因
        suggestion = unsched.unblock_suggestion
        assert suggestion, f"{unsched.job_id} 的解锁建议不得为空"
        assert any(isinstance(v, (int, float)) or _looks_numeric(v) for v in suggestion.values()), (
            f"{unsched.job_id} 的解锁建议必须含至少一个量化字段"
        )

    bad = {u.job_id: u for u in candidate.unschedulable_jobs}["ORD-BAD-OP1"]
    assert bad.blocking_reason == "MATERIAL_INSUFFICIENT"
    assert Decimal(bad.unblock_suggestion["shortfall_quantity"]) == Decimal("8")  # 10 − 2


def _looks_numeric(value: object) -> bool:
    """字符串是否能解析为 `Decimal`（缺口量以字符串承载，避免浮点漂移）。"""
    if not isinstance(value, str):
        return False
    try:
        Decimal(value)
        return True
    except Exception:
        return False


# ==========================================================================
# EVAL-008 —— NO_FEASIBLE_PLAN + 每作业 blocking_reason
# ==========================================================================


def test_eval_008_no_feasible_plan_annotates_every_job() -> None:
    """EVAL-008: 完全无法排产时，`feasibility == NO_FEASIBLE_PLAN`，每个作业都带 `blocking_reason`。

    两个订单要求 `WELD` 型机器，但快照里只有 `CNC` 型机器——没有任何机器能做这道工序。
    因此无一作业可排产。断言：

    - `feasibility == "NO_FEASIBLE_PLAN"`（无任何已排产作业）；
    - 展开出的**每一个**作业都出现在 `unschedulable_jobs` 里、且各带一个非空
      `blocking_reason`（R8.2：不允许静默丢弃作业，每个作业必须有归属与原因）。
    """
    product = _product("PRD-W", operations=(_op(1, machine_type="WELD", skill="WELDING"),))
    snap = _snapshot(
        orders=(_order("ORD-A", product_id="PRD-W"), _order("ORD-B", product_id="PRD-W")),
        products=(product,),
        machines=(_machine("CNC-01", machine_type="CNC"),),
        workers=(_worker("W-01", skills=("CNC_OP",)),),
    )
    candidate = generate_schedule(snap)

    assert candidate.feasibility == "NO_FEASIBLE_PLAN"
    assert candidate.scheduled_jobs == ()

    unschedulable_ids = {u.job_id for u in candidate.unschedulable_jobs}
    assert unschedulable_ids == {"ORD-A-OP1", "ORD-B-OP1"}  # 每个展开作业都有归属
    for unsched in candidate.unschedulable_jobs:
        assert unsched.blocking_reason, f"{unsched.job_id} 缺 blocking_reason"
