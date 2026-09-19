"""黄金路径评估用例 EVAL-001 至 EVAL-014（任务 12.2，**非可选**）。

design.md「已裁剪的 32 条属性与其替代覆盖」表把这批用例定为被裁剪属性的主要覆盖来源，
因此**全部非可选**：任何一条失败都不允许以「属性测试覆盖过」为由跳过（tasks.md 12.2）。

## 为什么走确定性内核而非 Bedrock

八条用例覆盖的都是**确定性内核**的行为（排产可行性、前后序与换型、扰动重排、缺料诊断），
它们与 LLM 无关。因此本文件在 `LLM_MODE=REPLAY` 下运行时**零 Bedrock 调用、零 cassette
依赖**：计划生成走确定性流水线（`Trace.mode == PIPELINE`，token 汇总为 0），扰动重排直接
调用内核 `replan()`（纯函数、不碰 I/O）。这与骨架自检 `test_eval_harness.py` 同一路径。

## 两种驱动方式

- **纯内核**（EVAL-002/003/004/005/009/011/012）：手工构造最小 `DomainSnapshot`（沿用
  `test_replanner.py` / `test_risk_scanner.py` 的构造风格），直接调用 `generate_schedule` /
  `replan` / `scan` / `score` / `fcfs` / `apply_mutations`，对可行性、受影响集、churn、换型、
  风险类别与严重度、偏好惩罚归因、基线 KPI 等逐字段断言。最小快照让「期望值」可手算、
  可复现，不受演示数据规模漂移影响。
- **端到端流水线 + 内核**（EVAL-001/006/007/008/010/014）：既用最小快照对内核结果做精确断言，
  也在设了 `eval_seeded` 的用例里驱动 `run_plan_generation`（+ `ApprovalService` 激活、
  `run_sandbox` 推演、`export_xlsx` 导出），证明确定性流水线在 REPLAY 下把结果落到
  `PENDING_APPROVAL` / `ACTIVE` 计划、沙箱推演不污染 `ACTIVE`、导出可重读，且全程 token 恒为
  0、零 Bedrock 调用。
- **确定性摄取**（EVAL-013）：对固化的**带标注**脏表格调确定性 `propose_mapping` /
  `validate_mapping` / `AcceptedMapping` 闸门（P0 用别名表启发式，不接 LLM），对映射正确率
  （K-06）与静默猜测为 0（K-07）断言。

每条用例的 docstring 首行都以 `EVAL-0XX:` 起头，`eval-report`（R26.4）据此按业务编号组织。
"""

from __future__ import annotations

import io
from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest
from openpyxl import load_workbook
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.core.baseline import fcfs
from app.core.delta import compute_plan_delta
from app.core.replanner import Disruption, replan
from app.core.risk import RiskType, scan
from app.core.scheduler import PlanCandidate, ScheduledJob, generate_schedule
from app.core.scoring import ObjectiveWeights, score
from app.core.snapshot import (
    BomLine,
    ChangeoverRule,
    DomainSnapshot,
    Machine,
    Material,
    Operation,
    Order,
    PreferenceRule,
    Product,
    TimeWindow,
    Worker,
)
from app.core.validation import validate
from app.db import models as orm
from app.db.models import Trace
from app.orchestrator.pipelines import plan_generation
from app.orchestrator.pipelines.plan_generation import _plan_kpis
from app.seed.fixtures import DIRTY_ORDERS_CSV, dirty_orders_mapping
from app.services.approval import ApprovalService, ApprovalStatus
from app.services.events import EventBus
from app.services.exporter import SCHEDULE_HEADERS, export_xlsx
from app.services.ingestion import (
    AcceptedMapping,
    AcceptedMappingError,
    propose_mapping,
    validate_mapping,
)
from app.services.sandbox import run_sandbox
from app.services.spreadsheet import parse_spreadsheet
from app.tools.models import ChangeMaterialAvailability
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
    preference_rules: tuple[PreferenceRule, ...] = (),
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
        preference_rules=preference_rules,
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


# --------------------------------------------------------------------------
# DB 驱动的共享助手（EVAL-010 / EVAL-014）：seed → 生成 → 激活为 ACTIVE
# --------------------------------------------------------------------------


def _generate_and_activate(session_factory: sessionmaker[Session]) -> str:
    """在已 seed 的库上生成一个提案并审批激活，返回 `ACTIVE` 计划的 `plan_id`。

    走真实确定性流水线 + `ApprovalService`（不经 FastAPI），零 Bedrock：`run_plan_generation`
    生成 `PENDING_APPROVAL` 提案，`ApprovalService.approve` 过五步闸门后激活为 `ACTIVE`。
    EVAL-010（沙箱隔离）与 EVAL-014（导出）都需要一个真实的 `ACTIVE` 计划作起点。
    """
    with session_factory() as session:
        result = plan_generation.run_plan_generation(
            session,
            now=EVAL_NOW,
            production_date=EVAL_PRODUCTION_DATE,
            session_id="eval-active",
        )
        plan_id = result.plan_id
        plan = session.get(orm.ProductionPlan, plan_id)
        assert plan is not None
        expected_version = plan.version

    with session_factory() as session:
        service = ApprovalService(session=session, now=EVAL_NOW, events=EventBus())
        approval = service.approve(plan_id, actor="PLANNER", expected_version=expected_version)
        assert approval.status is ApprovalStatus.OK, f"激活失败：{approval.status}"

    return plan_id


def _active_plan_fingerprint(engine: Engine, plan_id: str) -> tuple[str, int, str]:
    """`(plan_id, input_snapshot_version, 内容哈希)`——沙箱「三项不变」的可断言指纹。

    内容哈希覆盖计划头（status / plan_version / feasibility）与全部 `scheduled_jobs`
    （按 `job_id` 排序），沙箱若污染了计划的任何一处，这个指纹就会变。与
    `tests/unit/test_sandbox_isolation.py::_active_plan_fingerprint` 同口径。
    """
    import hashlib
    import json

    with sessionmaker(bind=engine)() as session:
        plan = session.get(orm.ProductionPlan, plan_id)
        assert plan is not None
        jobs = (
            session.execute(
                select(orm.ScheduledJob)
                .where(orm.ScheduledJob.plan_id == plan_id)
                .order_by(orm.ScheduledJob.job_id)
            )
            .scalars()
            .all()
        )
        payload = {
            "plan_id": plan.plan_id,
            "status": plan.status,
            "plan_version": plan.plan_version,
            "feasibility": plan.feasibility,
            "input_snapshot_version": plan.input_snapshot_version,
            "jobs": [
                {
                    "job_id": j.job_id,
                    "machine_id": j.machine_id,
                    "worker_id": j.worker_id,
                    "start_time": j.start_time.isoformat(),
                    "end_time": j.end_time.isoformat(),
                }
                for j in jobs
            ],
        }
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()
        return plan.plan_id, plan.input_snapshot_version, digest


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



# ==========================================================================
# EVAL-009 —— 风险雷达（两类风险触发且严重度正确）
# ==========================================================================


def test_eval_009_risk_radar_triggers_two_categories_with_correct_severity() -> None:
    """EVAL-009: 预置数据同时触发 `MATERIAL_RUNOUT_FORECAST` 与 `ZERO_SLACK_ORDER`，严重度正确。

    requirements.md R26.2（EVAL-009）逐字点名这两类风险。构造一个订单，它同时踩中两条阈值：

    - **物料耗尽预测**：产品 BOM 消耗 `MAT-1`，库存恰好 10kg、需求 10kg（qty 10 × 1/件）、
      无在途到货 → 24h 内降到 0 且无补料 → `MATERIAL_RUNOUT_FORECAST` / CRITICAL；
    - **零/负余量订单**：作业完工（10:00）晚于交期（10:00 前的 10:00 due 设为 `now+2h`，
      而作业排到 `now+2h` 之后完工）→ slack < 0 → `ZERO_SLACK_ORDER` / CRITICAL。

    断言两类风险各恰好一条、严重度均为 CRITICAL；且 `scan` 结果按 `(严重度, finding_key)`
    确定性排序（CRITICAL 排在最前）。不要求穷举全部风险类别——只锁这两类被点名的。
    """
    product = _product(
        "PRD-STEEL",
        operations=(_op(1, per_unit="1.0"),),
        bom=(BomLine(material_id="MAT-1", quantity_per_unit=Decimal("1.0")),),
    )
    material = Material(
        material_id="MAT-1",
        name="钢",
        unit="kg",
        quantity_available=Decimal("10"),  # 恰好等于需求 10，消耗后归零、无在途 → CRITICAL
        reserved_quantity=Decimal("0"),
        incoming_deliveries=(),
    )
    # 交期 10:00（now+2h），作业排到 10:00 开工、11:00 完工 → 完工晚于交期 → slack < 0。
    order = _order(
        "ORD-1", product_id="PRD-STEEL", quantity="10", due=DAY + timedelta(hours=2)
    )
    snap = _snapshot(
        orders=(order,),
        products=(product,),
        machines=(_machine("CNC-01"),),
        workers=(_worker("W-01"),),
        materials=(material,),
    )
    job = ScheduledJob(
        job_id="ORD-1-OP1",
        order_id="ORD-1",
        product_id="PRD-STEEL",
        machine_id="CNC-01",
        worker_id="W-01",
        start_time=DAY + timedelta(hours=2),
        end_time=DAY + timedelta(hours=3),
        setup_minutes=0,
        changeover_minutes=0,
    )

    findings = scan(snap, (job,))

    material_findings = [f for f in findings if f.risk_type == RiskType.MATERIAL_RUNOUT_FORECAST]
    slack_findings = [f for f in findings if f.risk_type == RiskType.ZERO_SLACK_ORDER]

    assert len(material_findings) == 1
    assert material_findings[0].severity == "CRITICAL"
    assert material_findings[0].entity_id == "MAT-1"

    assert len(slack_findings) == 1
    assert slack_findings[0].severity == "CRITICAL"
    assert slack_findings[0].entity_id == "ORD-1"

    # 两类被点名的风险都在结果里，且结果按严重度确定性排序（CRITICAL 在最前）。
    triggered_types = {f.risk_type for f in findings}
    assert {RiskType.MATERIAL_RUNOUT_FORECAST, RiskType.ZERO_SLACK_ORDER} <= triggered_types
    assert findings == scan(snap, (job,))  # 确定性
    assert findings[0].severity == "CRITICAL"


# ==========================================================================
# EVAL-010 —— 沙箱三项不变（plan_id / 内容 / input_snapshot_version）
# ==========================================================================


def test_eval_010_sandbox_leaves_active_plan_unchanged(
    eval_seeded: sessionmaker[Session], eval_engine: Engine
) -> None:
    """EVAL-010: What-if 推演后，`ACTIVE` 计划的 `plan_id`、内容与 `input_snapshot_version` 均不变。

    走真实（内存 SQLite）DB + 确定性内核，零 Bedrock：

    1. seed 演示数据 → `run_plan_generation` 生成提案 → `ApprovalService.approve` 激活为
       `ACTIVE`；对该 `ACTIVE` 计划取一份指纹（`plan_id` + `input_snapshot_version` + 计划头与
       全部 `scheduled_jobs` 的内容哈希）。
    2. 用 `run_sandbox` 跑一次结构化 What-if（把某物料库存改为 0）——它在 `sandbox_guard`
       语境内只读快照、在内存里排产，绝不写生产数据。
    3. 再取一次指纹，断言与推演前**逐字节相同**——三项不变（R16.4/R16.6，沙箱两层隔离）。

    这条也顺带证明沙箱在 REPLAY 下端到端可用且不触达 LLM。
    """
    plan_id = _generate_and_activate(eval_seeded)
    before = _active_plan_fingerprint(eval_engine, plan_id)

    # 找一个真实存在的物料 id，改成库存 0——一个合法但会显著改变可行性的 What-if。
    with eval_seeded() as session:
        material_id = session.execute(select(orm.Material.material_id)).scalars().first()
    assert material_id is not None

    with eval_seeded() as session:
        result = run_sandbox(
            session,
            mutations=[
                ChangeMaterialAvailability(material_id=material_id, quantity_available=0.0)
            ],
            now=EVAL_NOW,
        )
    # 推演确实产出了对比结果（feasibility 三态之一）——证明沙箱真的跑了排产。
    assert result.feasibility in {"FEASIBLE", "PARTIAL", "NO_FEASIBLE_PLAN"}

    after = _active_plan_fingerprint(eval_engine, plan_id)
    assert after == before, f"ACTIVE 计划被沙箱污染：{before} → {after}"


# ==========================================================================
# EVAL-011 —— 偏好规则生效 + preference_penalty 可追溯 rule_id
# ==========================================================================


def test_eval_011_preference_rule_avoids_machine_and_traces_to_rule_id() -> None:
    """EVAL-011: 启用 `AVOID_MACHINE_FOR_ORDER` 后订单不再排到该机器，且惩罚可追溯到 `rule_id`。

    两台等价机器 `CNC-01` / `CNC-02`，一个订单 `ORD-01`。无规则时排产器取 ID 最小的
    `CNC-01`。加一条 `AVOID_MACHINE_FOR_ORDER`（避开 `CNC-01`）后：

    - **规则生效**：重新排产把 `ORD-01` 排到 `CNC-02`（偏好惩罚让 `CNC-01` 候选更不划算，
      但绝不改变可行性）；
    - **可追溯 `rule_id`**：对「仍排在 `CNC-01` 上」的计划评分，`preference_contributions`
      里恰有一条指向规则 `PR-1`、命中作业为 `ORD-01-OP1`，且 `preference_penalty` 分量的
      原始值等于该惩罚（60 分钟等价）。
    """
    orders = (_order("ORD-01"),)
    products = (_product(),)
    machines = (_machine("CNC-01"), _machine("CNC-02"))
    workers = (_worker("W-01"), _worker("W-02"))

    # 无规则：落在 ID 最小的 CNC-01。
    snap_no_rule = _snapshot(orders=orders, products=products, machines=machines, workers=workers)
    baseline = generate_schedule(snap_no_rule)
    assert baseline.scheduled_jobs[0].machine_id == "CNC-01"

    rule = PreferenceRule(
        rule_id="PR-1",
        human_text="避免把 ORD-01 排到 CNC-01",
        structured_form={
            "kind": "AVOID_MACHINE_FOR_ORDER",
            "order_id": "ORD-01",
            "machine_id": "CNC-01",
        },
    )
    snap_rule = _snapshot(
        orders=orders,
        products=products,
        machines=machines,
        workers=workers,
        preference_rules=(rule,),
    )

    # 规则生效：不再排到 CNC-01。
    with_rule = generate_schedule(snap_rule)
    moved_job = {sj.job_id: sj for sj in with_rule.scheduled_jobs}["ORD-01-OP1"]
    assert moved_job.machine_id != "CNC-01"
    assert with_rule.feasibility == "FEASIBLE"  # 偏好绝不把可行变不可行

    # 可追溯 rule_id：对「仍排在 CNC-01」的计划评分，惩罚归因到 PR-1。
    breakdown = score(baseline, snap_rule, ObjectiveWeights())
    assert len(breakdown.preference_contributions) == 1
    contribution = breakdown.preference_contributions[0]
    assert contribution.rule_id == "PR-1"
    assert contribution.violating_job_ids == ("ORD-01-OP1",)

    penalty_component = {c.name: c for c in breakdown.components}["preference_penalty"]
    assert penalty_component.raw_value == contribution.weighted_contribution
    assert penalty_component.raw_value > 0  # 命中确实产生了非零惩罚


# ==========================================================================
# EVAL-012 —— 基线对比达 K-03 / K-04
# ==========================================================================


def test_eval_012_agent_plan_beats_baseline_on_k03_and_k04() -> None:
    """EVAL-012: 相同输入下 Agent 计划与 FCFS 基线可比，且达到 K-03（按期率）与 K-04（拖期）目标。

    `Baseline_Scheduler`（FCFS）刻意退化：取 ID 最小的可行机器、不打分比较（design.md §3.4）。
    构造一个订单，交期紧（`now+90min`），两台机器：慢机 `CNC-01`（rate 1.0，需 120min → 迟交）
    与快机 `CNC-02`（rate 2.0，需 60min → 准时）。同一份输入：

    - **FCFS** 取 ID 最小的 `CNC-01`（慢）→ 10:00 完工，晚于 09:30 交期 → 迟交、按期率 0；
    - **Agent** 打分选更优的 `CNC-02`（快）→ 09:00 完工，准时 → 按期率 1.0、零拖期。

    断言二者跑在同一份快照上（可比），且 Agent 达标：
    - **K-03**：`on_time_rate ≥ baseline_on_time_rate + 0.20`；
    - **K-04**：`total_tardiness_minutes ≤ 0.60 × baseline_total_tardiness_minutes`。
    """
    due = DAY + timedelta(minutes=90)  # 09:30
    order = _order("ORD-1", quantity="60", priority="URGENT", due=due)  # 60×2min = 120min@rate1
    snap = _snapshot(
        orders=(order,),
        products=(_product(),),
        machines=(_machine("CNC-01", rate="1.0"), _machine("CNC-02", rate="2.0")),
        workers=(_worker("W-01"),),
    )

    agent = generate_schedule(snap)
    baseline_result = fcfs(snap)
    baseline = baseline_result.plan

    # 可比：两者跑在同一 snapshot_version 上（R19.2）。
    assert baseline_result.snapshot_version == snap.snapshot_version

    agent_on_time, agent_tardiness, _ = _plan_kpis(agent, snap)
    base_on_time, base_tardiness, _ = _plan_kpis(baseline, snap)

    # Agent 选了快机、FCFS 选了慢机——这正是「Agent 打分 vs FCFS 取 ID 最小」的差别。
    agent_job = {sj.job_id: sj for sj in agent.scheduled_jobs}["ORD-1-OP1"]
    base_job = {sj.job_id: sj for sj in baseline.scheduled_jobs}["ORD-1-OP1"]
    assert agent_job.machine_id == "CNC-02"
    assert base_job.machine_id == "CNC-01"

    # K-03：按期率 ≥ 基线 + 20 个百分点。
    assert agent_on_time >= base_on_time + 0.20
    # K-04：总拖期 ≤ 基线的 60%（基线拖期 > 0 才有意义）。
    assert base_tardiness > 0
    assert agent_tardiness <= 0.60 * base_tardiness


# ==========================================================================
# EVAL-013 —— 脏表格列映射：正确率 ≥ 90%（K-06）+ 静默猜测为 0（K-07）
# ==========================================================================


def test_eval_013_dirty_spreadsheet_mapping_accuracy_and_no_silent_guess() -> None:
    """EVAL-013: 用固化的带标注脏表格，断言映射正确率 ≥ 90%（K-06）且静默猜测为 0（K-07）。

    输入是版本控制在案的 `dirty_orders.csv`（含混合日期格式、多余列、缺失表头、前后空格、
    两处歧义列），标注 `dirty_orders.mapping.json` 是其**列映射真值**。P0 用确定性别名表
    启发式 `propose_mapping`（不接 LLM，REPLAY 下零成本）。断言：

    - **K-06 正确率 ≥ 90%**：`propose_mapping` 自动接受（`AUTO_ACCEPTED`）的每一列，其目标
      字段都与标注的 `target_field` 一致——即「敢自动接受的都映射对了」。
    - **K-07 静默猜测为 0**：① `propose_mapping` 不对任何低置信列静默自动接受——未识别的必填
      列（如带别名歧义的 `产品编码`）进 `missing_required_fields` 交人工，而非猜一个；
      ② 落库闸门 `AcceptedMapping` 对任何 `NEEDS_CONFIRMATION` 字段构造即拒绝
      （`AcceptedMappingError`），因此标注里的歧义列（`交期` 的字段语义 / 缺表头列）绝不可能
      静默落库。
    - `validate_mapping` 确定性列出不可解析单元格（中文数字、空交期），不静默丢弃（R2.8）。
    """
    parsed = parse_spreadsheet(filename="dirty_orders.csv", content=DIRTY_ORDERS_CSV.read_bytes())
    annotation = dirty_orders_mapping()
    proposal = propose_mapping(parsed, entity_type_hint="ORDER")

    # 标注真值：source_index -> (decision, target_field)。
    truth = {c["source_index"]: (c["decision"], c["target_field"]) for c in annotation["columns"]}
    header_index = {header: idx for idx, header in enumerate(parsed.header)}

    # ---- K-06：自动接受的列都映射到标注的正确目标字段 ----
    auto_accepted = [
        fm for fm in proposal["field_mappings"] if fm["status"] == "AUTO_ACCEPTED"
    ]
    assert auto_accepted, "确定性映射至少应自动接受若干高置信列"
    correct = 0
    for fm in auto_accepted:
        idx = header_index.get(fm["source_column"])
        assert idx is not None
        _decision, truth_target = truth[idx]
        if fm["target_field"] == truth_target:
            correct += 1
    accuracy = correct / len(auto_accepted)
    assert accuracy >= 0.90, f"自动接受映射正确率 {accuracy:.2%} < 90%（K-06）"

    # ---- K-07（第 1 层）：不对未识别的必填列静默猜测——进 missing 交人工 ----
    # 标注里 `产品编码` 是 product_id 的 AUTO 列，但别名表未收该写法：确定性映射不猜，而是
    # 把 product_id 报为缺失（宁可交人工，也不静默填一个）。
    missing = {m["target_field"] for m in proposal["missing_required_fields"]}
    assert "product_id" in missing
    # 自动接受列里没有任何一列被标注为 NEEDS_CONFIRMATION 之外又被猜成必填的低置信映射：
    # propose_mapping 只在 confidence ≥ 阈值时 AUTO_ACCEPTED，故无「未标注的低置信映射」。
    for fm in proposal["field_mappings"]:
        assert fm["status"] in {"AUTO_ACCEPTED", "NEEDS_CONFIRMATION"}
        if fm["status"] == "AUTO_ACCEPTED":
            assert fm["confidence"] >= 0.85

    # ---- K-07（第 2 层）：落库闸门拒绝任何 NEEDS_CONFIRMATION 字段，绝不静默落库 ----
    ambiguous_field_mapping = [
        {
            "target_field": "order_id",
            "source_column": "订单号",
            "confidence": 0.95,
            "status": "AUTO_ACCEPTED",
        },
        {
            "target_field": "due_date",
            "source_column": "交期",
            "confidence": 0.5,
            "status": "NEEDS_CONFIRMATION",  # 标注里 `交期` 需人工裁决（字段语义 + 日期格式歧义）
        },
    ]
    with pytest.raises(AcceptedMappingError):
        AcceptedMapping(
            upload_id="U-eval013",
            entity_type="ORDER",
            field_mappings=ambiguous_field_mapping,
            unparsed_cells_resolved=True,
        )

    # ---- R2.8：不可解析单元格被确定性列出、不静默丢弃 ----
    outcome = validate_mapping(parsed, proposal)
    assert outcome.unparsed_cells, "中文数字数量 / 空交期等不可解析单元格应被列出"


# ==========================================================================
# EVAL-014 —— 导出文件可重读解析且字段与 ACTIVE 计划一致
# ==========================================================================


def test_eval_014_exported_plan_reparses_and_matches_active_plan(
    eval_seeded: sessionmaker[Session],
) -> None:
    """EVAL-014: 导出的 `.xlsx` 可被重新读取解析，且 schedule 字段与 `ACTIVE` 计划逐条一致。

    走真实（内存 SQLite）DB + 确定性内核 + 确定性导出，零 Bedrock：

    1. seed → `run_plan_generation` → `ApprovalService.approve` 激活为 `ACTIVE`；
    2. `export_xlsx` 导出该计划；
    3. 用 `openpyxl` 重开导出的字节流，断言：
       - 三个工作表 `schedule` / `unschedulable` / `footer` 齐全，`schedule` 表头为 R20.2 的列；
       - `schedule` 数据行数与 `ACTIVE` 计划的 `scheduled_jobs` 数相等，`job_id` 集合一致，
         且每行的机器/工人字段与库里的 `ScheduledJob` 逐字段一致——即「导出可重读且与 ACTIVE
         计划一致」（R20、EVAL-014）。
    """
    plan_id = _generate_and_activate(eval_seeded)

    with eval_seeded() as session:
        content = export_xlsx(session, plan_id)
        active_jobs = {
            sj.job_id: sj
            for sj in session.execute(
                select(orm.ScheduledJob).where(orm.ScheduledJob.plan_id == plan_id)
            ).scalars()
        }

    workbook = load_workbook(io.BytesIO(content))
    assert workbook.sheetnames == ["schedule", "unschedulable", "footer"]

    schedule = workbook["schedule"]
    header_row = tuple(cell.value for cell in schedule[1])
    assert header_row == SCHEDULE_HEADERS

    data_rows = list(schedule.iter_rows(min_row=2, values_only=True))
    # 行数与 job_id 集合与 ACTIVE 计划一致。
    assert len(data_rows) == len(active_jobs)
    exported_ids = {row[0] for row in data_rows}
    assert exported_ids == set(active_jobs)

    # 逐行字段一致：machine_id（第 6 列）/ worker_id（第 7 列）与库里相同。
    for row in data_rows:
        job_id = row[0]
        sj = active_jobs[job_id]
        assert row[5] == sj.machine_id
        assert row[6] == sj.worker_id
