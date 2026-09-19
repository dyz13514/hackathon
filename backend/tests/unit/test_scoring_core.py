"""`core/scoring.py` 的纯计算断言（任务 2.10，R7.1–R7.2 / R7.4–R7.5）。

守着 §2838 EVAL 覆盖清单为 `Objective_Scorer` 点名的几件事：

1. **恰好 7 个分量、顺序固定**（R7.1）：`score()` 的输出永远是 `_COMPONENT_ORDER` 那 7 条，
   缺一条或多一条都要红。这是 UI 展示（R7.3）、审计快照与回归断言三处的共同前提。
2. **每分量三元组 + 总分**（R7.2）：`weighted_contribution = weight × raw_value`，
   `total_score = Σ weighted_contribution`（原属性 8）。
3. **负权重方向正确**：`machine_utilisation` 权重为负，利用率越高 → 该项贡献越负 → 总分越低。
4. **7 个分量各一例**：迟交计数、总迟交、加急迟交、换型、利用率、churn（有/无参照）、
   偏好惩罚留空。
5. **churn 仅在传入 `reference_plan` 时有值**，且按 §3.5 的并集分母落在 [0, 1]。
6. **纯确定性**（R7.5）：同输入两次运行结果逐字段相同。
7. **`diff_weights`**：R7.4 审计的纯计算接缝——只算「哪几项变了」，不写库。

全部用直接构造的 `ScheduledJob` / `PlanCandidate` 控制分量原始值，另有一例走
`generate_schedule` 验证真实排产下的利用率与换型口径。不 mock、不碰库。
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from app.core.scheduler import PlanCandidate, ScheduledJob, generate_schedule
from app.core.scoring import (
    _COMPONENT_ORDER,
    ComponentScore,
    ObjectiveBreakdown,
    ObjectiveWeights,
    diff_weights,
    score,
)
from app.core.snapshot import (
    DomainSnapshot,
    Machine,
    Operation,
    Order,
    Product,
    Worker,
)

PRODUCTION_DATE = date(2026, 3, 2)
NOW = datetime(2026, 3, 2, 8, 0)
DAY = datetime(2026, 3, 2, 8, 0)
DAY_END = datetime(2026, 3, 2, 18, 0)


# --------------------------------------------------------------------------
# 构造夹具（纯数据，不碰库）
# --------------------------------------------------------------------------


def _order(
    order_id: str,
    *,
    priority: str = "NORMAL",
    due: datetime = DAY_END,
    product_id: str = "PRD-01",
    quantity: str = "10",
) -> Order:
    return Order(
        order_id=order_id,
        product_id=product_id,
        quantity=Decimal(quantity),
        due_date=due,
        promised_date=None,
        priority=priority,  # type: ignore[arg-type]
    )


def _machine(
    machine_id: str = "CNC-01",
    *,
    start: datetime = DAY,
    end: datetime = DAY_END,
) -> Machine:
    return Machine(
        machine_id=machine_id,
        machine_type="CNC",
        capabilities=(),
        status="AVAILABLE",
        available_start=start,
        available_end=end,
        rate_multiplier=Decimal("1.0"),
        downtime_windows=(),
    )


def _sched(
    order_id: str,
    *,
    job_id: str | None = None,
    machine_id: str = "CNC-01",
    worker_id: str = "W-01",
    start: datetime,
    end: datetime,
    changeover: int = 0,
    product_id: str = "PRD-01",
) -> ScheduledJob:
    return ScheduledJob(
        job_id=job_id or f"{order_id}-OP1",
        order_id=order_id,
        product_id=product_id,
        machine_id=machine_id,
        worker_id=worker_id,
        start_time=start,
        end_time=end,
        setup_minutes=changeover,
        changeover_minutes=changeover,
    )


def _snapshot(
    *,
    orders: tuple[Order, ...],
    machines: tuple[Machine, ...] = (),
) -> DomainSnapshot:
    return DomainSnapshot(
        snapshot_version=1,
        production_date=PRODUCTION_DATE,
        now=NOW,
        orders=orders,
        products=(
            Product(
                product_id="PRD-01",
                name="支架",
                operations=(
                    Operation(
                        sequence=1,
                        required_machine_type="CNC",
                        required_capability=None,
                        required_worker_skill="CNC_OP",
                        base_processing_time_per_unit=Decimal("2.0"),
                        setup_time=10,
                    ),
                ),
                bom=(),
            ),
        ),
        materials=(),
        machines=machines or (_machine(),),
        workers=(
            Worker(
                worker_id="W-01",
                name="张三",
                skills=("CNC_OP",),
                shift_start=DAY,
                shift_end=DAY_END,
                absences=(),
            ),
        ),
        changeover_rules=(),
        preference_rules=(),
    )


def _plan(*jobs: ScheduledJob) -> PlanCandidate:
    return PlanCandidate(
        scheduled_jobs=tuple(jobs),
        unschedulable_jobs=(),
        feasibility="FEASIBLE",
    )


def _component(breakdown: ObjectiveBreakdown, name: str) -> ComponentScore:
    return next(c for c in breakdown.components if c.name == name)


# --------------------------------------------------------------------------
# 1. 恰好 7 个分量、顺序固定（R7.1）
# --------------------------------------------------------------------------


def test_score_emits_exactly_seven_components_in_fixed_order() -> None:
    snap = _snapshot(orders=(_order("ORD-01"),))
    plan = _plan(_sched("ORD-01", start=DAY, end=datetime(2026, 3, 2, 9, 0)))

    breakdown = score(plan, snap, ObjectiveWeights())

    assert len(breakdown.components) == 7
    assert [c.name for c in breakdown.components] == list(_COMPONENT_ORDER)


# --------------------------------------------------------------------------
# 2. 每分量三元组 + 总分为加权和（R7.2、原属性 8）
# --------------------------------------------------------------------------


def test_weighted_contribution_is_weight_times_raw_and_total_is_their_sum() -> None:
    snap = _snapshot(orders=(_order("ORD-01"),))
    plan = _plan(_sched("ORD-01", start=DAY, end=datetime(2026, 3, 2, 9, 0)))

    breakdown = score(plan, snap, ObjectiveWeights())

    for comp in breakdown.components:
        assert comp.weighted_contribution == comp.weight * comp.raw_value
    assert breakdown.total_score == sum(c.weighted_contribution for c in breakdown.components)


# --------------------------------------------------------------------------
# 3. 迟交计数 / 总迟交 / 加急迟交（R7.1 各一例）
# --------------------------------------------------------------------------


def test_late_order_count_and_total_tardiness() -> None:
    # ORD-01 交期 12:00，完工 13:00 → 迟 60 分钟；ORD-02 交期 18:00，完工 17:00 → 不迟。
    due_early = datetime(2026, 3, 2, 12, 0)
    snap = _snapshot(orders=(_order("ORD-01", due=due_early), _order("ORD-02", due=DAY_END)))
    plan = _plan(
        _sched("ORD-01", start=DAY, end=datetime(2026, 3, 2, 13, 0)),
        _sched("ORD-02", job_id="ORD-02-OP1", start=DAY, end=datetime(2026, 3, 2, 17, 0)),
    )

    breakdown = score(plan, snap, ObjectiveWeights())

    assert _component(breakdown, "late_order_count").raw_value == 1.0
    assert _component(breakdown, "total_tardiness_minutes").raw_value == 60.0


def test_urgent_order_lateness_counts_only_urgent_orders() -> None:
    due = datetime(2026, 3, 2, 12, 0)
    snap = _snapshot(
        orders=(
            _order("ORD-01", priority="URGENT", due=due),
            _order("ORD-02", priority="NORMAL", due=due),
        )
    )
    plan = _plan(
        _sched("ORD-01", start=DAY, end=datetime(2026, 3, 2, 13, 0)),  # URGENT 迟 60
        _sched("ORD-02", job_id="ORD-02-OP1", start=DAY, end=datetime(2026, 3, 2, 14, 0)),  # 迟 120
    )

    breakdown = score(plan, snap, ObjectiveWeights())

    # urgent_order_lateness 只累加 URGENT 的 60；total_tardiness 是两者之和 180。
    assert _component(breakdown, "urgent_order_lateness").raw_value == 60.0
    assert _component(breakdown, "total_tardiness_minutes").raw_value == 180.0


# --------------------------------------------------------------------------
# 4. 换型分量（R7.1）
# --------------------------------------------------------------------------


def test_total_changeover_sums_changeover_minutes() -> None:
    snap = _snapshot(orders=(_order("ORD-01"), _order("ORD-02")))
    plan = _plan(
        _sched("ORD-01", start=DAY, end=datetime(2026, 3, 2, 9, 0), changeover=15),
        _sched("ORD-02", job_id="ORD-02-OP1", start=datetime(2026, 3, 2, 9, 0),
               end=datetime(2026, 3, 2, 10, 0), changeover=25),
    )

    breakdown = score(plan, snap, ObjectiveWeights())

    assert _component(breakdown, "total_changeover_minutes").raw_value == 40.0


# --------------------------------------------------------------------------
# 5. 利用率与负权重方向（design.md §3.3）
# --------------------------------------------------------------------------


def test_machine_utilisation_ratio_and_negative_weight_direction() -> None:
    # 一台机器可用 10 小时 = 600 分钟；排了 60 分钟 → 利用率 0.1。
    snap = _snapshot(orders=(_order("ORD-01"),))
    plan = _plan(_sched("ORD-01", start=DAY, end=datetime(2026, 3, 2, 9, 0)))

    breakdown = score(plan, snap, ObjectiveWeights())
    util = _component(breakdown, "machine_utilisation")

    assert abs(util.raw_value - 0.1) < 1e-9
    assert util.weight == -50.0
    # 负权重：贡献为负，利用率越高把总分拉得越低。
    assert util.weighted_contribution < 0

    # 更高利用率（排 120 分钟 → 0.2）应得到更负的贡献。
    busier = _plan(_sched("ORD-01", start=DAY, end=datetime(2026, 3, 2, 10, 0)))
    busier_util = _component(score(busier, snap, ObjectiveWeights()), "machine_utilisation")
    assert busier_util.weighted_contribution < util.weighted_contribution


def test_machine_utilisation_zero_when_no_available_machine_minutes() -> None:
    # 机器可用窗口为空（start == end）→ 分母 0，利用率取 0.0，不除零。
    snap = _snapshot(
        orders=(_order("ORD-01"),),
        machines=(_machine(start=DAY, end=DAY),),
    )
    plan = _plan(_sched("ORD-01", start=DAY, end=datetime(2026, 3, 2, 9, 0)))

    breakdown = score(plan, snap, ObjectiveWeights())
    assert _component(breakdown, "machine_utilisation").raw_value == 0.0


# --------------------------------------------------------------------------
# 6. churn_ratio：无参照为 0，有参照按 §3.5 并集分母（R7.1）
# --------------------------------------------------------------------------


def test_churn_ratio_is_zero_without_reference_plan() -> None:
    snap = _snapshot(orders=(_order("ORD-01"),))
    plan = _plan(_sched("ORD-01", start=DAY, end=datetime(2026, 3, 2, 9, 0)))

    breakdown = score(plan, snap, ObjectiveWeights())
    assert _component(breakdown, "churn_ratio").raw_value == 0.0


def test_churn_ratio_counts_reassigned_and_added_over_union() -> None:
    # 参照：ORD-01 在 CNC-01；ORD-02 在 CNC-01。
    reference = _plan(
        _sched("ORD-01", start=DAY, end=datetime(2026, 3, 2, 9, 0), machine_id="CNC-01"),
        _sched("ORD-02", job_id="ORD-02-OP1", start=DAY, end=datetime(2026, 3, 2, 9, 0),
               machine_id="CNC-01"),
    )
    # 新计划：ORD-01 换到 CNC-02（reassigned）；ORD-02 不变；新增 ORD-03（added）。
    new = _plan(
        _sched("ORD-01", start=DAY, end=datetime(2026, 3, 2, 9, 0), machine_id="CNC-02"),
        _sched("ORD-02", job_id="ORD-02-OP1", start=DAY, end=datetime(2026, 3, 2, 9, 0),
               machine_id="CNC-01"),
        _sched("ORD-03", job_id="ORD-03-OP1", start=DAY, end=datetime(2026, 3, 2, 9, 0),
               machine_id="CNC-01"),
    )
    snap = _snapshot(orders=(_order("ORD-01"), _order("ORD-02"), _order("ORD-03")))

    breakdown = score(new, snap, ObjectiveWeights(), reference_plan=reference)
    # 并集 3 个作业；扰动 = 1 reassigned + 1 added = 2 → 2/3。
    churn = _component(breakdown, "churn_ratio").raw_value
    assert abs(churn - (2 / 3)) < 1e-9
    assert 0.0 <= churn <= 1.0


def test_churn_ratio_counts_moved_when_only_start_time_changes() -> None:
    reference = _plan(_sched("ORD-01", start=DAY, end=datetime(2026, 3, 2, 9, 0)))
    # 同机器同工人，只挪了开始时间 → moved。
    new = _plan(
        _sched("ORD-01", start=datetime(2026, 3, 2, 10, 0), end=datetime(2026, 3, 2, 11, 0))
    )
    snap = _snapshot(orders=(_order("ORD-01"),))

    breakdown = score(new, snap, ObjectiveWeights(), reference_plan=reference)
    # 并集 1，moved 1 → 1.0。
    assert _component(breakdown, "churn_ratio").raw_value == 1.0


# --------------------------------------------------------------------------
# 7. preference_penalty：无启用偏好时该分量为 0、归因为空（任务 11.2 的基线不变性）
# --------------------------------------------------------------------------


def test_preference_penalty_zero_when_no_rules() -> None:
    """快照无偏好规则 → preference_penalty 分量 raw_value == 0、contributions 为空。

    这条守的是「无启用偏好时评分逐字段等于偏好接入前」（属性 10b）：偏好接线不改变没有规则
    时的任何评分数值。四类规则真正生效的归因由 `test_preference_scoring.py` 覆盖。
    """
    snap = _snapshot(orders=(_order("ORD-01"),))
    plan = _plan(_sched("ORD-01", start=DAY, end=datetime(2026, 3, 2, 9, 0)))

    breakdown = score(plan, snap, ObjectiveWeights())
    assert _component(breakdown, "preference_penalty").raw_value == 0.0
    assert breakdown.preference_contributions == ()
    assert breakdown.weight_overrides_applied == ()


# --------------------------------------------------------------------------
# 8. 纯确定性（R7.5）
# --------------------------------------------------------------------------


def test_score_is_deterministic() -> None:
    snap = _snapshot(orders=(_order("ORD-01", due=datetime(2026, 3, 2, 12, 0)),))
    plan = _plan(_sched("ORD-01", start=DAY, end=datetime(2026, 3, 2, 13, 0), changeover=5))

    first = score(plan, snap, ObjectiveWeights())
    second = score(plan, snap, ObjectiveWeights())
    assert first == second


# --------------------------------------------------------------------------
# 9. 经真实排产验证利用率与换型口径
# --------------------------------------------------------------------------


def test_score_over_generated_schedule_matches_scheduled_minutes() -> None:
    snap = _snapshot(orders=(_order("ORD-01"),))
    plan = generate_schedule(snap)
    assert plan.feasibility == "FEASIBLE"

    breakdown = score(plan, snap, ObjectiveWeights())
    # 一道工序：setup 10 + 2 分钟/件 × 10 件 = 30 分钟占用；机器可用 600 分钟 → 0.05。
    util = _component(breakdown, "machine_utilisation")
    assert abs(util.raw_value - (30 / 600)) < 1e-9


# --------------------------------------------------------------------------
# 10. diff_weights：R7.4 审计的纯计算接缝
# --------------------------------------------------------------------------


def test_diff_weights_reports_only_changed_components() -> None:
    old = ObjectiveWeights()
    new = ObjectiveWeights(total_tardiness_minutes=2.0)

    changes = diff_weights(old, new)

    assert len(changes) == 1
    assert changes[0].component == "total_tardiness_minutes"
    assert changes[0].old_weight == 1.0
    assert changes[0].new_weight == 2.0


def test_diff_weights_empty_when_unchanged() -> None:
    assert diff_weights(ObjectiveWeights(), ObjectiveWeights()) == ()
