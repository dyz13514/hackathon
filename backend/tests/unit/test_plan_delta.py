"""`compute_plan_delta` 与 `churn_ratio` 单元测试（任务 7.2，承接原属性 11）。

覆盖 R10.1 / R9.3 / R9.6，**非可选**：

1. **五集合严格划分**：added / removed / moved / reassigned / unchanged 两两不相交、并集覆盖
   全部 job_id；每个作业恰属其一（`reassigned` 与 `moved` 条件互斥，自然不重叠）。
2. **并集分母的越界用例**：加急插单时 `churn_ratio` 仍落在 `[0, 1]`——这正是用并集而非
   `|ACTIVE|` 的原因；传统分母（`|ACTIVE|`）会让比率超过 1。
3. **逐类语义断言**：只换时间 → `moved`；换机器或工人 → `reassigned`；同时换时间和机器
   → `reassigned`（`reassigned` 优先）；完全不变 → `unchanged`。
4. **边界情形**：两计划均空 → `churn_ratio == 0.0`；同一计划与自身比较 → 全 `unchanged`。
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from app.core.delta import PlanDelta, compute_plan_delta
from app.core.scheduler import PlanCandidate, ScheduledJob

# --------------------------------------------------------------------------
# 夹具辅助
# --------------------------------------------------------------------------

DAY = datetime(2026, 3, 2, 8, 0)
T1 = datetime(2026, 3, 2, 9, 0)
T2 = datetime(2026, 3, 2, 10, 0)
T3 = datetime(2026, 3, 2, 11, 0)
T4 = datetime(2026, 3, 2, 12, 0)


def _sj(
    job_id: str,
    *,
    order_id: str = "ORD-A",
    product_id: str = "PRD-01",
    machine_id: str = "CNC-01",
    worker_id: str = "W-01",
    start_time: datetime = DAY,
    end_time: datetime = T1,
) -> ScheduledJob:
    return ScheduledJob(
        job_id=job_id,
        order_id=order_id,
        product_id=product_id,
        machine_id=machine_id,
        worker_id=worker_id,
        start_time=start_time,
        end_time=end_time,
        setup_minutes=0,
        changeover_minutes=0,
    )


def _plan(*jobs: ScheduledJob, feasibility: str = "FEASIBLE") -> PlanCandidate:
    return PlanCandidate(
        scheduled_jobs=tuple(jobs),
        unschedulable_jobs=(),
        feasibility=feasibility,  # type: ignore[arg-type]
    )


def _all_job_ids(delta: PlanDelta) -> set[str]:
    """把五集合里的全部 job_id 合并为一个集合（用于划分性断言）。"""
    return set(delta.added) | set(delta.removed) | set(delta.moved) | set(delta.reassigned) | set(delta.unchanged)


def _assert_partition(delta: PlanDelta, active: PlanCandidate, cand: PlanCandidate) -> None:
    """五集合严格划分断言（互不相交 + 并集为全体 job_id）。"""
    a_ids = {sj.job_id for sj in active.scheduled_jobs}
    b_ids = {sj.job_id for sj in cand.scheduled_jobs}
    universe = a_ids | b_ids

    all_in_delta = _all_job_ids(delta)
    assert all_in_delta == universe, f"五集合并集 {all_in_delta} ≠ 全体 {universe}"

    # 两两不相交
    sets = [
        ("added", set(delta.added)),
        ("removed", set(delta.removed)),
        ("moved", set(delta.moved)),
        ("reassigned", set(delta.reassigned)),
        ("unchanged", set(delta.unchanged)),
    ]
    for i, (name_i, s_i) in enumerate(sets):
        for name_j, s_j in sets[i + 1 :]:
            overlap = s_i & s_j
            assert not overlap, f"{name_i} ∩ {name_j} = {overlap}（应为空）"


# ==========================================================================
# 1. 空计划与自比较
# ==========================================================================


def test_both_empty_plans_produce_zero_delta() -> None:
    """两计划均空 → 五集合全空，churn_ratio == 0.0。"""
    delta = compute_plan_delta(_plan(), _plan())
    assert delta.added == ()
    assert delta.removed == ()
    assert delta.moved == ()
    assert delta.reassigned == ()
    assert delta.unchanged == ()
    assert delta.churn_ratio == 0.0


def test_same_plan_compared_with_itself_is_all_unchanged() -> None:
    """同一计划与自身比较 → 全部作业归 `unchanged`，churn_ratio == 0.0。"""
    plan = _plan(_sj("J1"), _sj("J2", order_id="ORD-B"))
    delta = compute_plan_delta(plan, plan)
    assert set(delta.unchanged) == {"J1", "J2"}
    assert delta.moved == ()
    assert delta.reassigned == ()
    assert delta.added == ()
    assert delta.removed == ()
    assert delta.churn_ratio == 0.0
    _assert_partition(delta, plan, plan)


# ==========================================================================
# 2. added / removed 语义
# ==========================================================================


def test_job_only_in_candidate_is_added() -> None:
    """候选计划新增的作业 → `added`。"""
    active = _plan(_sj("J1"))
    cand = _plan(_sj("J1"), _sj("J2", order_id="ORD-B"))
    delta = compute_plan_delta(active, cand)
    assert delta.added == ("J2",)
    assert delta.removed == ()
    _assert_partition(delta, active, cand)


def test_job_only_in_active_is_removed() -> None:
    """ACTIVE 计划删除的作业 → `removed`。"""
    active = _plan(_sj("J1"), _sj("J2", order_id="ORD-B"))
    cand = _plan(_sj("J1"))
    delta = compute_plan_delta(active, cand)
    assert delta.removed == ("J2",)
    assert delta.added == ()
    _assert_partition(delta, active, cand)


# ==========================================================================
# 3. moved 语义（仅时间平移）
# ==========================================================================


def test_job_with_different_start_time_only_is_moved() -> None:
    """只有 start_time 变化（机器与工人不变）→ `moved`。"""
    active = _plan(_sj("J1", start_time=DAY, end_time=T1))
    cand = _plan(_sj("J1", start_time=T2, end_time=T3))
    delta = compute_plan_delta(active, cand)
    assert delta.moved == ("J1",)
    assert delta.reassigned == ()
    assert delta.unchanged == ()
    _assert_partition(delta, active, cand)


def test_job_with_same_start_time_is_not_moved() -> None:
    """start_time 不变（其余也不变）→ `unchanged`，不进 `moved`。"""
    active = _plan(_sj("J1", start_time=DAY, end_time=T1))
    cand = _plan(_sj("J1", start_time=DAY, end_time=T2))
    delta = compute_plan_delta(active, cand)
    # start_time 相同，end_time 不同但不在 delta 的判断条件里 → unchanged
    assert delta.moved == ()
    assert delta.unchanged == ("J1",)
    _assert_partition(delta, active, cand)


# ==========================================================================
# 4. reassigned 语义（机器或工人变化）
# ==========================================================================


def test_job_with_different_machine_is_reassigned() -> None:
    """machine_id 变化 → `reassigned`。"""
    active = _plan(_sj("J1", machine_id="CNC-01"))
    cand = _plan(_sj("J1", machine_id="CNC-02"))
    delta = compute_plan_delta(active, cand)
    assert delta.reassigned == ("J1",)
    assert delta.moved == ()
    _assert_partition(delta, active, cand)


def test_job_with_different_worker_is_reassigned() -> None:
    """worker_id 变化 → `reassigned`。"""
    active = _plan(_sj("J1", worker_id="W-01"))
    cand = _plan(_sj("J1", worker_id="W-02"))
    delta = compute_plan_delta(active, cand)
    assert delta.reassigned == ("J1",)
    assert delta.moved == ()
    _assert_partition(delta, active, cand)


def test_job_with_different_machine_and_time_is_reassigned_not_moved() -> None:
    """machine_id 变化同时 start_time 变化 → `reassigned`（reassigned 优先于 moved）。

    这是任务描述里「reassigned 取优先」的关键验证：若先判 moved 再过滤 reassigned，
    这个作业可能被错误地归入 moved。
    """
    active = _plan(_sj("J1", machine_id="CNC-01", start_time=DAY, end_time=T1))
    cand = _plan(_sj("J1", machine_id="CNC-02", start_time=T2, end_time=T3))
    delta = compute_plan_delta(active, cand)
    assert delta.reassigned == ("J1",)
    assert delta.moved == ()
    _assert_partition(delta, active, cand)


def test_job_with_different_worker_and_time_is_reassigned_not_moved() -> None:
    """worker_id 变化同时 start_time 变化 → `reassigned`（reassigned 优先于 moved）。"""
    active = _plan(_sj("J1", worker_id="W-01", start_time=DAY, end_time=T1))
    cand = _plan(_sj("J1", worker_id="W-02", start_time=T2, end_time=T3))
    delta = compute_plan_delta(active, cand)
    assert delta.reassigned == ("J1",)
    assert delta.moved == ()
    _assert_partition(delta, active, cand)


# ==========================================================================
# 5. unchanged 语义
# ==========================================================================


def test_job_with_no_changes_is_unchanged() -> None:
    """机器、工人、开始时间均不变 → `unchanged`。"""
    job = _sj("J1")
    active = _plan(job)
    cand = _plan(job)
    delta = compute_plan_delta(active, cand)
    assert delta.unchanged == ("J1",)
    assert delta.moved == ()
    assert delta.reassigned == ()
    _assert_partition(delta, active, cand)


# ==========================================================================
# 6. 五集合划分性：综合用例
# ==========================================================================


def test_five_set_partition_comprehensive() -> None:
    """一个计划里同时包含 added / removed / moved / reassigned / unchanged 五类作业。

    ACTIVE：J1（unchanged）、J2（moved: 只换时间）、J3（reassigned: 换机器）、
            J4（removed: 候选里没有）。
    候选：J1（unchanged）、J2（移了时间）、J3（换了机器）、J5（added: ACTIVE 没有）。
    """
    active = _plan(
        _sj("J1"),                                          # unchanged
        _sj("J2", start_time=DAY, end_time=T1),            # → moved
        _sj("J3", machine_id="CNC-01"),                    # → reassigned
        _sj("J4", order_id="ORD-D"),                       # → removed
    )
    cand = _plan(
        _sj("J1"),                                          # unchanged
        _sj("J2", start_time=T2, end_time=T3),             # moved (only time changed)
        _sj("J3", machine_id="CNC-02"),                    # reassigned
        _sj("J5", order_id="ORD-E"),                       # added
    )
    delta = compute_plan_delta(active, cand)

    assert "J1" in delta.unchanged
    assert "J2" in delta.moved
    assert "J3" in delta.reassigned
    assert "J4" in delta.removed
    assert "J5" in delta.added

    _assert_partition(delta, active, cand)


# ==========================================================================
# 7. churn_ratio 的并集分母（越界用例，R9.3）
# ==========================================================================


def test_churn_ratio_with_union_denominator_stays_in_range() -> None:
    """`churn_ratio` 在 [0, 1] 内（并集分母保证）。

    设 ACTIVE 有 2 个作业（J1 不变，J2 不变），候选新增 10 个加急插单作业（J3-J12）。
    - 分子（churn_count）= 10（added），分母（|union|）= 12 → churn_ratio = 10/12 ≈ 0.833。
    - 若用传统分母 |ACTIVE| = 2，则 10/2 = 5.0，远超 1，K-05 的 ≤0.20 目标失去意义。
    """
    active_jobs = [_sj("J1"), _sj("J2", order_id="ORD-B")]
    new_jobs = [_sj(f"J{i}", order_id=f"ORD-{i}") for i in range(3, 13)]

    active = _plan(*active_jobs)
    cand = _plan(*active_jobs, *new_jobs)  # 保留原有 2 个 + 新增 10 个
    delta = compute_plan_delta(active, cand)

    assert len(delta.added) == 10
    assert len(delta.unchanged) == 2
    assert 0.0 <= delta.churn_ratio <= 1.0, f"churn_ratio={delta.churn_ratio} 超出 [0,1]"
    # 验证分母确实是并集（12），而非 |ACTIVE|（2）
    expected = 10 / 12
    assert abs(delta.churn_ratio - expected) < 1e-9
    _assert_partition(delta, active, cand)


def test_churn_ratio_pure_removal_stays_in_range() -> None:
    """ACTIVE 有 5 个作业，候选全删 → churn_ratio = 5/5 = 1.0（上界）。"""
    jobs = [_sj(f"J{i}", order_id=f"ORD-{i}") for i in range(1, 6)]
    active = _plan(*jobs)
    cand = _plan()
    delta = compute_plan_delta(active, cand)
    assert delta.removed == tuple(f"J{i}" for i in range(1, 6))
    assert delta.churn_ratio == 1.0
    _assert_partition(delta, active, cand)


def test_churn_ratio_zero_when_nothing_changed() -> None:
    """完全不变（全部 unchanged）→ churn_ratio == 0.0。"""
    jobs = [_sj("J1"), _sj("J2", order_id="ORD-B")]
    plan = _plan(*jobs)
    delta = compute_plan_delta(plan, plan)
    assert delta.churn_ratio == 0.0


def test_churn_ratio_both_empty_is_zero() -> None:
    """两计划均空 → churn_ratio == 0.0（分母 0 情形的边界保护）。"""
    delta = compute_plan_delta(_plan(), _plan())
    assert delta.churn_ratio == 0.0


def test_churn_ratio_never_exceeds_one_with_large_urgent_insert() -> None:
    """大量加急插单时 `churn_ratio` 仍不超过 1.0（并集分母的核心属性）。

    这是 K-05（churn_ratio ≤ 0.20）的可信前提：必须先证明它在数学上是有意义的比率。
    传统分母（|ACTIVE|）在这里会给出 50/1 = 50.0，完全没有意义。
    """
    original = [_sj(f"J{i}", order_id=f"ORD-{i}") for i in range(1, 2)]  # 1 个原有
    inserted = [_sj(f"U{i}", order_id=f"URG-{i}") for i in range(1, 51)]  # 50 个插入

    active = _plan(*original)
    cand = _plan(*original, *inserted)
    delta = compute_plan_delta(active, cand)

    assert 0.0 <= delta.churn_ratio <= 1.0
    assert len(delta.added) == 50
    _assert_partition(delta, active, cand)


# ==========================================================================
# 8. 结果确定性
# ==========================================================================


def test_compute_plan_delta_is_deterministic() -> None:
    """同输入两次调用结果逐字段相同（R5.7 纪律）。"""
    active = _plan(_sj("J1"), _sj("J2", order_id="ORD-B"))
    cand = _plan(_sj("J2", order_id="ORD-B", start_time=T2, end_time=T3), _sj("J3", order_id="ORD-C"))
    d1 = compute_plan_delta(active, cand)
    d2 = compute_plan_delta(active, cand)
    assert d1 == d2


# ==========================================================================
# 9. PlanDelta 排序：确保输出为升序序列
# ==========================================================================


def test_all_sets_are_sorted() -> None:
    """五集合的 job_id 序列均已升序排序（确定性输出，便于日志与断言）。"""
    # 用字典序不自然的 job_id，确认输出已排序
    active = _plan(
        _sj("ZZZ"),
        _sj("AAA", order_id="ORD-B"),
        _sj("MMM", order_id="ORD-C"),
    )
    cand = _plan(
        _sj("ZZZ"),                                        # unchanged
        _sj("AAA", order_id="ORD-B", start_time=T2, end_time=T3),  # moved
        _sj("NNN", order_id="ORD-D"),                      # added
    )
    delta = compute_plan_delta(active, cand)

    assert list(delta.added) == sorted(delta.added)
    assert list(delta.removed) == sorted(delta.removed)
    assert list(delta.moved) == sorted(delta.moved)
    assert list(delta.reassigned) == sorted(delta.reassigned)
    assert list(delta.unchanged) == sorted(delta.unchanged)
