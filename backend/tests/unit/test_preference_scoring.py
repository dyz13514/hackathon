"""偏好规则接入软评分与候选排序的聚焦单元测试（任务 11.2，R7.6 / R18.7 / R18.9）。

守 tasks.md 11.2「单元测试（非可选，承接原属性 9）」点名的内容与其余可验收点：

- 四类 `structured_form` 的归因各一例，`preference_penalty` 等于逐 `rule_id` 贡献之和；
- 命中与不命中（换机器/换工人后不再命中）；
- disabled 规则完全无效（快照只加载 enabled=True，因此 disabled 规则根本不在 `snapshot`
  里，评分与空规则集逐字段相同）；
- 组合聚合（多条规则求和）；
- 确定性（同输入两次逐字段相同、归因顺序稳定）；
- 贡献明细（rule_id / human_text / violating_job_ids ≤10 / raw_value / weighted_contribution）
  与 11.1 的「影响了哪些作业」口径一致；
- `ADJUST_OBJECTIVE_WEIGHT` 走 `weight_overrides_applied`、不进 penalty；
- 偏好只改软评分，绝不改变可行性（本文件断言评分侧；排产侧与硬约束侧见
  `test_scheduler_core.py` 与 `tests/structure/test_preference_rule_scope.py`）。

全部用直接构造的 `ScheduledJob` / `PlanCandidate` / `DomainSnapshot` 控制输入，不 mock、不碰库。
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from app.core.preference import PREF_UNIT, preference_penalty
from app.core.scheduler import PlanCandidate, ScheduledJob
from app.core.scoring import ObjectiveWeights, score
from app.core.snapshot import (
    DomainSnapshot,
    Machine,
    Operation,
    Order,
    PreferenceRule,
    Product,
    Worker,
)

PRODUCTION_DATE = date(2026, 3, 2)
NOW = datetime(2026, 3, 2, 8, 0)
DAY = datetime(2026, 3, 2, 8, 0)
DAY_END = datetime(2026, 3, 2, 18, 0)


# --------------------------------------------------------------------------
# 构造夹具
# --------------------------------------------------------------------------


def _order(order_id: str, *, product_id: str = "PRD-01") -> Order:
    return Order(
        order_id=order_id,
        product_id=product_id,
        quantity=Decimal("10"),
        due_date=DAY_END,
        promised_date=None,
        priority="NORMAL",
    )


def _machine(machine_id: str = "CNC-01") -> Machine:
    return Machine(
        machine_id=machine_id,
        machine_type="CNC",
        capabilities=(),
        status="AVAILABLE",
        available_start=DAY,
        available_end=DAY_END,
        rate_multiplier=Decimal("1.0"),
        downtime_windows=(),
    )


def _worker(worker_id: str = "W-01", *, skill: str = "CNC_OP") -> Worker:
    return Worker(
        worker_id=worker_id,
        name="工人",
        skills=(skill,),
        shift_start=DAY,
        shift_end=DAY_END,
        absences=(),
    )


def _product(product_id: str = "PRD-01", *, skill: str = "CNC_OP") -> Product:
    return Product(
        product_id=product_id,
        name="件",
        operations=(
            Operation(
                sequence=1,
                required_machine_type="CNC",
                required_capability=None,
                required_worker_skill=skill,
                base_processing_time_per_unit=Decimal("2.0"),
                setup_time=10,
            ),
        ),
        bom=(),
    )


def _sched(
    order_id: str,
    *,
    machine_id: str = "CNC-01",
    worker_id: str = "W-01",
    product_id: str = "PRD-01",
) -> ScheduledJob:
    return ScheduledJob(
        job_id=f"{order_id}-OP1",
        order_id=order_id,
        product_id=product_id,
        machine_id=machine_id,
        worker_id=worker_id,
        start_time=DAY,
        end_time=datetime(2026, 3, 2, 9, 0),
        setup_minutes=0,
        changeover_minutes=0,
    )


def _pref(rule_id: str, form: dict[str, object]) -> PreferenceRule:
    return PreferenceRule(rule_id=rule_id, human_text=f"text-{rule_id}", structured_form=form)


def _avoid_order(order_id: str, machine_id: str, weight: float | None = None) -> dict[str, object]:
    form: dict[str, object] = {
        "kind": "AVOID_MACHINE_FOR_ORDER",
        "order_id": order_id,
        "machine_id": machine_id,
    }
    if weight is not None:
        form["weight_delta"] = weight
    return form


def _avoid_product(
    product_id: str, machine_id: str, weight: float | None = None
) -> dict[str, object]:
    form: dict[str, object] = {
        "kind": "AVOID_MACHINE_FOR_PRODUCT",
        "product_id": product_id,
        "machine_id": machine_id,
    }
    if weight is not None:
        form["weight_delta"] = weight
    return form


def _snapshot(
    *,
    orders: tuple[Order, ...],
    products: tuple[Product, ...] = (),
    workers: tuple[Worker, ...] = (),
    machines: tuple[Machine, ...] = (),
    rules: tuple[PreferenceRule, ...] = (),
) -> DomainSnapshot:
    return DomainSnapshot(
        snapshot_version=1,
        production_date=PRODUCTION_DATE,
        now=NOW,
        orders=orders,
        products=products or (_product(),),
        materials=(),
        machines=machines or (_machine(),),
        workers=workers or (_worker(),),
        changeover_rules=(),
        preference_rules=rules,
    )


def _plan(*jobs: ScheduledJob) -> PlanCandidate:
    return PlanCandidate(scheduled_jobs=tuple(jobs), unschedulable_jobs=(), feasibility="FEASIBLE")


def _pref_component(breakdown: object) -> object:
    return next(c for c in breakdown.components if c.name == "preference_penalty")  # type: ignore[attr-defined]


# --------------------------------------------------------------------------
# 四类规则的归因各一例（承接原属性 9）
# --------------------------------------------------------------------------


def test_avoid_machine_for_order_attribution() -> None:
    rule = _pref("PR-1", _avoid_order("ORD-01", "CNC-01", 2))
    snap = _snapshot(orders=(_order("ORD-01"),), rules=(rule,))
    plan = _plan(_sched("ORD-01", machine_id="CNC-01"))

    result = preference_penalty(plan, snap.preference_rules, snap)
    assert len(result.contributions) == 1
    c = result.contributions[0]
    assert c.rule_id == "PR-1"
    assert c.violating_job_ids == ("ORD-01-OP1",)
    assert c.raw_value == 2.0  # 1 命中 × weight_delta 2
    assert c.weighted_contribution == float(Decimal("2") * PREF_UNIT)  # 120.0
    assert result.total == c.weighted_contribution


def test_avoid_machine_for_product_attribution() -> None:
    rule = _pref("PR-2", _avoid_product("PRD-01", "CNC-01"))
    snap = _snapshot(orders=(_order("ORD-01"),), rules=(rule,))
    plan = _plan(_sched("ORD-01", product_id="PRD-01", machine_id="CNC-01"))

    result = preference_penalty(plan, snap.preference_rules, snap)
    assert result.contributions[0].violating_job_ids == ("ORD-01-OP1",)
    assert result.total == 60.0  # 1 × 1 × 60


def test_prefer_worker_for_skill_attribution_hits_wrong_worker() -> None:
    """PREFER_WORKER_FOR_SKILL：需要该技能但用了别的工人 → 命中；用被偏好工人则不命中。"""
    rule = _pref(
        "PR-3",
        {"kind": "PREFER_WORKER_FOR_SKILL", "skill": "CNC_OP", "worker_id": "W-01"},
    )
    snap = _snapshot(
        orders=(_order("ORD-01"),),
        products=(_product(skill="CNC_OP"),),
        workers=(_worker("W-01"), _worker("W-02")),
        rules=(rule,),
    )
    # 用了 W-02（非偏好工人）→ 命中
    plan_wrong = _plan(_sched("ORD-01", worker_id="W-02"))
    hit = preference_penalty(plan_wrong, snap.preference_rules, snap)
    assert hit.contributions[0].violating_job_ids == ("ORD-01-OP1",)
    assert hit.total == 60.0

    # 用了 W-01（偏好工人）→ 不命中
    plan_right = _plan(_sched("ORD-01", worker_id="W-01"))
    miss = preference_penalty(plan_right, snap.preference_rules, snap)
    assert miss.contributions == ()
    assert miss.total == 0.0


def test_adjust_objective_weight_does_not_enter_penalty() -> None:
    """ADJUST_OBJECTIVE_WEIGHT 不进 penalty，走 weight_overrides_applied（tasks.md 11.2）。"""
    rule = _pref(
        "PR-4",
        {
            "kind": "ADJUST_OBJECTIVE_WEIGHT",
            "component": "total_tardiness_minutes",
            "multiplier": 2.0,
        },
    )
    snap = _snapshot(orders=(_order("ORD-01"),), rules=(rule,))
    plan = _plan(_sched("ORD-01"))

    result = preference_penalty(plan, snap.preference_rules, snap)
    assert result.total == 0.0
    assert result.contributions == ()

    # 但它作用到软目标权重上：score() 的 total_tardiness_minutes 权重被 ×2。
    breakdown = score(plan, snap, ObjectiveWeights())
    tardiness = next(c for c in breakdown.components if c.name == "total_tardiness_minutes")
    assert tardiness.weight == ObjectiveWeights().total_tardiness_minutes * 2.0
    assert len(breakdown.weight_overrides_applied) == 1
    assert breakdown.weight_overrides_applied[0]["rule_id"] == "PR-4"
    assert breakdown.weight_overrides_applied[0]["component"] == "total_tardiness_minutes"


# --------------------------------------------------------------------------
# 命中/不命中、disabled、组合聚合、确定性
# --------------------------------------------------------------------------


def test_no_match_when_machine_differs() -> None:
    rule = _pref("PR-1", _avoid_order("ORD-01", "CNC-01"))
    snap = _snapshot(
        orders=(_order("ORD-01"),), machines=(_machine("CNC-01"), _machine("CNC-02")), rules=(rule,)
    )
    plan = _plan(_sched("ORD-01", machine_id="CNC-02"))  # 排在别的机器上
    result = preference_penalty(plan, snap.preference_rules, snap)
    assert result.total == 0.0
    assert result.contributions == ()


def test_disabled_rule_has_no_effect() -> None:
    """disabled 规则完全无效：快照只加载 enabled=True，停用规则不在 rules，评分同空规则集。"""
    plan = _plan(_sched("ORD-01", machine_id="CNC-01"))
    snap_empty = _snapshot(orders=(_order("ORD-01"),), rules=())
    # 「快照里有一条 enabled 规则」对比「快照里没有规则（相当于该规则被停用后不再加载）」
    enabled_rule = _pref("PR-1", _avoid_order("ORD-01", "CNC-01"))
    snap_enabled = _snapshot(orders=(_order("ORD-01"),), rules=(enabled_rule,))

    base = score(plan, snap_empty, ObjectiveWeights())
    with_enabled = score(plan, snap_enabled, ObjectiveWeights())
    # 停用（= 不在快照）时逐字段等于空规则集
    disabled_equiv = score(plan, snap_empty, ObjectiveWeights())
    assert disabled_equiv.model_dump() == base.model_dump()
    # 启用时确实不同（证明规则真的生效，对照组）
    assert with_enabled.total_score != base.total_score


def test_combined_rules_aggregate_as_sum() -> None:
    """多条规则的总惩罚 = 逐 rule_id 贡献之和（承接原属性 9）。"""
    rules = (
        _pref("PR-1", _avoid_order("ORD-01", "CNC-01", 2)),
        _pref("PR-2", _avoid_product("PRD-01", "CNC-01", 3)),
    )
    snap = _snapshot(orders=(_order("ORD-01"),), rules=rules)
    plan = _plan(_sched("ORD-01", machine_id="CNC-01", product_id="PRD-01"))

    result = preference_penalty(plan, snap.preference_rules, snap)
    assert len(result.contributions) == 2
    assert result.total == sum(c.weighted_contribution for c in result.contributions)
    assert result.total == float(Decimal("2") * PREF_UNIT + Decimal("3") * PREF_UNIT)  # 300.0


def test_penalty_is_deterministic_and_stable_order() -> None:
    """同输入两次逐字段相同；归因顺序稳定（确定性，R7.5 / 属性 10b 同类）。"""
    rules = (
        _pref("PR-2", _avoid_order("ORD-02", "CNC-01")),
        _pref("PR-1", _avoid_order("ORD-01", "CNC-01")),
    )
    snap = _snapshot(orders=(_order("ORD-01"), _order("ORD-02")), rules=rules)
    plan = _plan(_sched("ORD-01", machine_id="CNC-01"), _sched("ORD-02", machine_id="CNC-01"))

    a = preference_penalty(plan, snap.preference_rules, snap)
    b = preference_penalty(plan, snap.preference_rules, snap)
    assert a.model_dump() == b.model_dump()
    # 规则按传入顺序处理（PR-2 先于 PR-1，因为快照按加载顺序传入）。
    assert [c.rule_id for c in a.contributions] == ["PR-2", "PR-1"]


def test_violating_job_ids_capped_at_ten_but_raw_counts_all() -> None:
    """violating_job_ids 至多 10 条，raw_value 仍按全部命中数计（design.md §4.3）。"""
    orders = tuple(_order(f"ORD-{i:02d}") for i in range(12))
    plan = _plan(*(_sched(f"ORD-{i:02d}", machine_id="CNC-01") for i in range(12)))
    rule = _pref("PR-1", _avoid_product("PRD-01", "CNC-01"))
    snap = _snapshot(orders=orders, rules=(rule,))

    result = preference_penalty(plan, snap.preference_rules, snap)
    c = result.contributions[0]
    assert len(c.violating_job_ids) == 10  # 截断
    assert c.raw_value == 12.0  # 但 raw 按全部 12 条命中计
    assert c.weighted_contribution == float(Decimal("12") * PREF_UNIT)


def test_score_preference_component_reflects_penalty_total() -> None:
    """score() 的 preference_penalty 分量原始值 = 总惩罚（分钟等价），加权贡献进 total_score。"""
    rule = _pref("PR-1", _avoid_order("ORD-01", "CNC-01", 2))
    snap = _snapshot(orders=(_order("ORD-01"),), rules=(rule,))
    plan = _plan(_sched("ORD-01", machine_id="CNC-01"))

    breakdown = score(plan, snap, ObjectiveWeights())
    pref = _pref_component(breakdown)
    assert pref.raw_value == 120.0  # 2 × 60
    assert pref.weighted_contribution == 120.0 * ObjectiveWeights().preference_penalty
    assert len(breakdown.preference_contributions) == 1
    assert breakdown.preference_contributions[0].rule_id == "PR-1"
