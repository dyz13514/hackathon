"""反事实关键作业选择 `pick_pivotal_job` 的三级判据（任务 8.4，R10.3，承接原属性 14）。

**非可选**（tasks.md 8.4）。纯内核层：构造 `ObjectiveBreakdown` + 两组 `ScheduledJob`，断言：

1. **判据 ①**：选中作业对**主导分量**（加权贡献绝对值最大）的贡献最大——由候选方案里加工
   时长最长的作业代表（确定性度量）。
2. **判据 ② tie-break**：主导贡献并列时比对总分贡献。
3. **判据 ③**：完全对称输入下按 `job_id` 升序唯一选出（无随机、可复现）。
4. **退化**：无 MOVED/REASSIGNED 时退回对新增作业集合挑选。
5. **NoTradeoff**：三者皆空 → `job_id = None`（调用方输出 NoTradeoff，不编造）。

Z 值等于沙箱重算值由 `test_scenario_sandbox` 的服务层路径覆盖（`build_counterfactual` 用
`Objective_Scorer` 实算）；此处只锁死内核选择的确定性。
"""

from __future__ import annotations

from datetime import datetime, timedelta

from app.core.sandbox import pick_pivotal_job
from app.core.scheduler import ScheduledJob
from app.core.scoring import ComponentScore, ObjectiveBreakdown

NOW = datetime(2026, 3, 2, 8, 0)


def _breakdown(*components: tuple[str, float]) -> ObjectiveBreakdown:
    """由 (name, weighted_contribution) 造 breakdown；raw/weight 不参与选择，占位即可。"""
    comps = tuple(
        ComponentScore(name=n, raw_value=0.0, weight=1.0, weighted_contribution=w)
        for n, w in components
    )
    total = sum(w for _, w in components)
    return ObjectiveBreakdown(components=comps, total_score=total)


def _job(job_id: str, *, minutes: int, order_id: str = "ORD-1") -> ScheduledJob:
    return ScheduledJob(
        job_id=job_id,
        order_id=order_id,
        product_id="PRD-1",
        machine_id="CNC-01",
        worker_id="W-01",
        start_time=NOW,
        end_time=NOW + timedelta(minutes=minutes),
        setup_minutes=0,
        changeover_minutes=0,
    )


# --------------------------------------------------------------------------
# 判据 ①：主导分量贡献最大（用加工时长作确定性代理）
# --------------------------------------------------------------------------


def test_picks_job_with_largest_contribution() -> None:
    breakdown = _breakdown(("total_tardiness", 500.0), ("changeover", 10.0))
    active = (_job("JOB-A", minutes=60), _job("JOB-B", minutes=60))
    # 候选里 JOB-B 更长 → 贡献更大 → 被选中。
    candidate = (_job("JOB-A", minutes=60), _job("JOB-B", minutes=240))
    selection = pick_pivotal_job(
        moved=frozenset({"JOB-A", "JOB-B"}),
        reassigned=frozenset(),
        added=frozenset(),
        breakdown=breakdown,
        active_jobs=active,
        candidate_jobs=candidate,
    )
    assert selection.job_id == "JOB-B"
    assert selection.dominant == "total_tardiness"  # 加权贡献绝对值最大的分量


def test_dominant_component_uses_absolute_value() -> None:
    """主导分量取加权贡献**绝对值**最大者（负权重的利用率也能主导）。"""
    breakdown = _breakdown(("utilisation", -900.0), ("tardiness", 100.0))
    candidate = (_job("JOB-X", minutes=120),)
    selection = pick_pivotal_job(
        moved=frozenset({"JOB-X"}),
        reassigned=frozenset(),
        added=frozenset(),
        breakdown=breakdown,
        active_jobs=(),
        candidate_jobs=candidate,
    )
    assert selection.dominant == "utilisation"


# --------------------------------------------------------------------------
# 判据 ③：完全对称输入的 tie-break 唯一性（job_id 升序）
# --------------------------------------------------------------------------


def test_symmetric_tie_break_is_deterministic_by_job_id() -> None:
    """两个作业时长相同（贡献并列）→ 按 job_id 升序唯一选出，且可复现。"""
    breakdown = _breakdown(("total_tardiness", 300.0))
    active = (_job("JOB-2", minutes=90), _job("JOB-1", minutes=90))
    candidate = (_job("JOB-2", minutes=120), _job("JOB-1", minutes=120))
    first = pick_pivotal_job(
        moved=frozenset({"JOB-1", "JOB-2"}),
        reassigned=frozenset(),
        added=frozenset(),
        breakdown=breakdown,
        active_jobs=active,
        candidate_jobs=candidate,
    )
    second = pick_pivotal_job(
        moved=frozenset({"JOB-2", "JOB-1"}),  # 顺序打乱
        reassigned=frozenset(),
        added=frozenset(),
        breakdown=breakdown,
        active_jobs=active,
        candidate_jobs=candidate,
    )
    assert first.job_id == "JOB-1"  # 升序 tie-break
    assert first.job_id == second.job_id  # 与输入集合顺序无关，唯一


# --------------------------------------------------------------------------
# 退化：无 MOVED/REASSIGNED → 对新增作业集合挑选
# --------------------------------------------------------------------------


def test_degrades_to_added_when_no_changed() -> None:
    breakdown = _breakdown(("total_tardiness", 200.0))
    candidate = (_job("NEW-1", minutes=30), _job("NEW-2", minutes=180))
    selection = pick_pivotal_job(
        moved=frozenset(),
        reassigned=frozenset(),
        added=frozenset({"NEW-1", "NEW-2"}),
        breakdown=breakdown,
        active_jobs=(),
        candidate_jobs=candidate,
    )
    assert selection.job_id == "NEW-2"  # 更长者


def test_reassigned_included_in_pool() -> None:
    breakdown = _breakdown(("total_tardiness", 200.0))
    candidate = (_job("R-1", minutes=240), _job("M-1", minutes=60))
    selection = pick_pivotal_job(
        moved=frozenset({"M-1"}),
        reassigned=frozenset({"R-1"}),
        added=frozenset(),
        breakdown=breakdown,
        active_jobs=(),
        candidate_jobs=candidate,
    )
    assert selection.job_id == "R-1"  # MOVED ∪ REASSIGNED，取更长者


# --------------------------------------------------------------------------
# NoTradeoff：三者皆空
# --------------------------------------------------------------------------


def test_no_tradeoff_when_all_empty() -> None:
    breakdown = _breakdown(("total_tardiness", 0.0))
    selection = pick_pivotal_job(
        moved=frozenset(),
        reassigned=frozenset(),
        added=frozenset(),
        breakdown=breakdown,
        active_jobs=(),
        candidate_jobs=(),
    )
    assert selection.job_id is None
    assert selection.selection_basis  # 诚实说明「无取舍项」，不编造


def test_selection_is_deterministic_across_runs() -> None:
    """同输入多次调用逐字段相同（R5.7 同一纪律）。"""
    breakdown = _breakdown(("total_tardiness", 500.0), ("changeover", 10.0))
    active = (_job("JOB-A", minutes=60), _job("JOB-B", minutes=60))
    candidate = (_job("JOB-A", minutes=60), _job("JOB-B", minutes=240))
    runs = [
        pick_pivotal_job(
            moved=frozenset({"JOB-A", "JOB-B"}),
            reassigned=frozenset(),
            added=frozenset(),
            breakdown=breakdown,
            active_jobs=active,
            candidate_jobs=candidate,
        )
        for _ in range(3)
    ]
    assert all(r == runs[0] for r in runs)
