"""`core/scheduling.py` 的时间线原语单元测试（任务 2.3，R4.4 / R4.5 / R4.6）。

四组性质，各守一件后果很重的事：

1. **`processing_minutes`**——`ceil(base × qty ÷ rate_multiplier)` 全程 `Decimal`（R4.5）。
   浮点误差会让「同输入不同结果」在最后一位有效数字上偶发，直接击穿 R5.7（属性 1）。
2. **`changeover`**——三级退化查表（精确 → 机器默认 → 全局默认，design.md §3.1.4）。查错一级
   就会低估或高估换型，排出车间做不到的计划或凭空多出换型分钟。
3. **`Timeline`**——有序不重叠区间列表，`earliest_feasible_slot` 的候选起点扫描依赖其有序性。
4. **`earliest_feasible_slot`**——候选起点扫描 + 双侧换型 + `hard_end` 剪枝 + 拒绝跨班次
   （R4.6，由 `hard_end` 承载）。空隙插入必须为后一个作业留换型（§3.1.3）。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from app.core.scheduling import (
    DEFAULT_CHANGEOVER_MINUTES,
    Interval,
    Timeline,
    changeover,
    earliest_feasible_slot,
    is_feasible_slot,
    processing_minutes,
)
from app.core.snapshot import ChangeoverRule, Machine

DAY = datetime(2026, 3, 2, 8, 0)


def _at(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 3, 2, hour, minute)


def _machine(machine_id: str = "CNC-01", *, rate: str = "1.0") -> Machine:
    return Machine(
        machine_id=machine_id,
        machine_type="CNC",
        capabilities=(),
        status="AVAILABLE",
        available_start=_at(8),
        available_end=_at(18),
        rate_multiplier=Decimal(rate),
        downtime_windows=(),
    )


def _rule(
    rule_id: str,
    *,
    m: str | None,
    fr: str | None,
    to: str | None,
    mins: int,
    spec: int,
) -> ChangeoverRule:
    return ChangeoverRule(
        rule_id=rule_id,
        machine_id=m,
        from_product_id=fr,
        to_product_id=to,
        changeover_minutes=mins,
        specificity=spec,
    )


# --------------------------------------------------------------------------
# processing_minutes（R4.5）
# --------------------------------------------------------------------------


def test_processing_minutes_ceils_to_whole_minute() -> None:
    # 0.7 分钟/件 × 10 件 ÷ 1.0 = 7.0 → 7
    assert processing_minutes(Decimal("0.7"), Decimal("10"), Decimal("1.0")) == 7
    # 0.7 × 11 ÷ 1.0 = 7.7 → 向上取整 8
    assert processing_minutes(Decimal("0.7"), Decimal("11"), Decimal("1.0")) == 8


def test_processing_minutes_rate_multiplier_speeds_up_and_slows_down() -> None:
    base, qty = Decimal("2"), Decimal("100")  # 200 分钟 @ 1.0
    assert processing_minutes(base, qty, Decimal("1.0")) == 200
    assert processing_minutes(base, qty, Decimal("2.0")) == 100  # 快机
    assert processing_minutes(base, qty, Decimal("0.5")) == 400  # 慢机


def test_processing_minutes_ceils_after_division() -> None:
    # 1 ÷ 3 = 0.333... 分钟/件 × 100 = 33.33... → 34，且不经过浮点
    assert processing_minutes(Decimal("1"), Decimal("100"), Decimal("3")) == 34


def test_processing_minutes_is_exact_no_float_drift() -> None:
    # 0.1 + 0.2 在浮点下 != 0.3；这里 0.3 × 10 = 3.0 必须恰好取整为 3 而非 4
    assert processing_minutes(Decimal("0.3"), Decimal("10"), Decimal("1.0")) == 3


# --------------------------------------------------------------------------
# changeover（R4.4，design.md §3.1.4）
# --------------------------------------------------------------------------


def test_changeover_zero_when_no_prior_product() -> None:
    assert changeover(_machine(), None, "P1", ()) == 0


def test_changeover_zero_when_same_product() -> None:
    # 即便存在会匹配的规则，同产品也不换型
    rules = (_rule("R1", m=None, fr=None, to=None, mins=99, spec=1),)
    assert changeover(_machine(), "P1", "P1", rules) == 0


def test_changeover_falls_back_to_global_default_constant_when_no_rules() -> None:
    assert changeover(_machine(), "P1", "P2", ()) == DEFAULT_CHANGEOVER_MINUTES


def test_changeover_uses_global_rule_when_only_global_present() -> None:
    rules = (_rule("R1", m=None, fr=None, to=None, mins=15, spec=1),)
    assert changeover(_machine(), "P1", "P2", rules) == 15


def test_changeover_prefers_machine_default_over_global() -> None:
    rules = (
        _rule("R-global", m=None, fr=None, to=None, mins=15, spec=1),
        _rule("R-mach", m="CNC-01", fr=None, to=None, mins=25, spec=2),
    )
    assert changeover(_machine("CNC-01"), "P1", "P2", rules) == 25


def test_changeover_prefers_exact_over_machine_default_and_global() -> None:
    rules = (
        _rule("R-global", m=None, fr=None, to=None, mins=15, spec=1),
        _rule("R-mach", m="CNC-01", fr=None, to=None, mins=25, spec=2),
        _rule("R-exact", m="CNC-01", fr="P1", to="P2", mins=40, spec=3),
    )
    assert changeover(_machine("CNC-01"), "P1", "P2", rules) == 40


def test_changeover_machine_rule_does_not_apply_to_other_machine() -> None:
    rules = (
        _rule("R-global", m=None, fr=None, to=None, mins=15, spec=1),
        _rule("R-mach", m="CNC-01", fr=None, to=None, mins=25, spec=2),
    )
    # 查的是 CNC-02：机器默认不匹配，退回全局
    assert changeover(_machine("CNC-02"), "P1", "P2", rules) == 15


def test_changeover_tie_breaks_on_rule_id_for_determinism() -> None:
    # 两条同 specificity 同样匹配，取 rule_id 最小者（确定性 tie-break，属性 1）。
    # R-aaa < R-bbb，因此 R-aaa 的 50 胜出——与列表顺序（R-bbb 在前）无关。
    rules = (
        _rule("R-bbb", m="CNC-01", fr="P1", to="P2", mins=40, spec=3),
        _rule("R-aaa", m="CNC-01", fr="P1", to="P2", mins=50, spec=3),
    )
    assert changeover(_machine("CNC-01"), "P1", "P2", rules) == 50


# --------------------------------------------------------------------------
# Timeline 不变量
# --------------------------------------------------------------------------


def test_timeline_keeps_intervals_sorted_regardless_of_insert_order() -> None:
    tl = Timeline()
    tl.occupy(_at(12), _at(13), "P2")
    tl.occupy(_at(9), _at(10), "P1")
    tl.occupy(_at(10, 30), _at(11), "P3")
    starts = [iv.start for iv in tl.intervals]
    assert starts == sorted(starts)


def test_timeline_rejects_overlapping_occupy() -> None:
    tl = Timeline()
    tl.occupy(_at(9), _at(11), "P1")
    with pytest.raises(ValueError, match="重叠"):
        tl.occupy(_at(10), _at(12), "P2")


def test_timeline_allows_touching_intervals_half_open() -> None:
    tl = Timeline()
    tl.occupy(_at(9), _at(10), "P1")
    tl.occupy(_at(10), _at(11), "P2")  # [9,10) 与 [10,11) 相邻不重叠
    assert len(tl) == 2


def test_timeline_free_is_true_only_for_gaps() -> None:
    tl = Timeline()
    tl.occupy(_at(9), _at(10), "P1")
    assert tl.free(_at(10), _at(11)) is True
    assert tl.free(_at(9, 30), _at(9, 45)) is False
    assert tl.free(_at(8), _at(9)) is True  # 相邻不冲突


def test_timeline_end_points_are_deduplicated() -> None:
    tl = Timeline()
    tl.occupy(_at(9), _at(10), "P1")
    tl.occupy(_at(11), _at(12), "P2")
    assert tl.end_points() == frozenset({_at(10), _at(12)})


def test_product_immediately_before_picks_latest_ending_at_or_before_t() -> None:
    tl = Timeline()
    tl.occupy(_at(9), _at(10), "P1")
    tl.occupy(_at(10), _at(11), "P2")
    assert tl.product_immediately_before(_at(11)) == "P2"  # end == t 也算之前
    assert tl.product_immediately_before(_at(10, 30)) == "P1"
    assert tl.product_immediately_before(_at(8)) is None


def test_first_occupied_after_returns_earliest_start_at_or_after_e() -> None:
    tl = Timeline()
    tl.occupy(_at(9), _at(10), "P1")
    tl.occupy(_at(12), _at(13), "P2")
    nxt = tl.first_occupied_after(_at(10, 30))
    assert nxt is not None and nxt.start == _at(12)
    assert tl.first_occupied_after(_at(13)) is None  # start == e 边界外


def test_interval_rejects_empty_or_reversed() -> None:
    with pytest.raises(ValueError):
        Interval(start=_at(10), end=_at(10), product_id="P1")
    with pytest.raises(ValueError):
        Interval(start=_at(11), end=_at(10), product_id="P1")


# --------------------------------------------------------------------------
# earliest_feasible_slot（design.md §3.1.3，R4.4 / R4.6）
# --------------------------------------------------------------------------


def _slot_on_empty_lines(
    *,
    setup_time: int = 0,
    duration: int = 60,
    ready_at: datetime | None = None,
    hard_end: datetime | None = None,
    rules: tuple[ChangeoverRule, ...] = (),
    to_product: str = "P1",
):
    return earliest_feasible_slot(
        Timeline(),
        Timeline(),
        machine=_machine(),
        to_product=to_product,
        setup_time=setup_time,
        ready_at=ready_at or _at(8),
        duration=duration,
        hard_end=hard_end or _at(18),
        changeover_rules=rules,
    )


def test_slot_on_empty_lines_starts_at_ready_at() -> None:
    slot = _slot_on_empty_lines(duration=60, ready_at=_at(9))
    assert slot is not None
    assert slot.start == _at(9)
    assert slot.end == _at(10)
    assert slot.setup_minutes == 0
    assert slot.changeover_minutes == 0


def test_slot_includes_setup_time_in_occupied_span() -> None:
    # 无换型（机器空），setup_minutes == setup_time
    slot = _slot_on_empty_lines(setup_time=15, duration=60, ready_at=_at(9))
    assert slot is not None
    assert slot.setup_minutes == 15
    assert slot.changeover_minutes == 0
    assert slot.end == _at(10, 15)


def test_slot_adds_changeover_when_machine_did_other_product() -> None:
    machine = _machine("CNC-01")
    m_tl = Timeline()
    m_tl.occupy(_at(8), _at(9), "P0")  # 机器上一个作业做的是 P0
    rules = (_rule("R", m=None, fr=None, to=None, mins=20, spec=1),)
    slot = earliest_feasible_slot(
        m_tl,
        Timeline(),
        machine=machine,
        to_product="P1",
        setup_time=5,
        ready_at=_at(9),
        duration=60,
        hard_end=_at(18),
        changeover_rules=rules,
    )
    assert slot is not None
    assert slot.changeover_minutes == 20
    assert slot.setup_minutes == 25  # setup_time 5 + changeover 20
    assert slot.end == _at(9) + timedelta(minutes=25 + 60)


def test_slot_prunes_when_end_exceeds_hard_end() -> None:
    # 班次到 10:00，作业需 120 分钟从 09:00 起 → 越界，返回 None（R4.6）
    slot = _slot_on_empty_lines(duration=120, ready_at=_at(9), hard_end=_at(10))
    assert slot is None


def test_slot_rejects_crossing_worker_shift_end() -> None:
    # hard_end 由调用方取 min(shift_end, available_end)；这里模拟 shift_end = 17:30
    slot = _slot_on_empty_lines(duration=60, ready_at=_at(17), hard_end=_at(17, 30))
    assert slot is None


def test_slot_scans_to_next_candidate_start_when_ready_is_busy() -> None:
    machine = _machine("CNC-01")
    m_tl = Timeline()
    m_tl.occupy(_at(9), _at(10), "P1")  # 09:00–10:00 忙
    slot = earliest_feasible_slot(
        m_tl,
        Timeline(),
        machine=machine,
        to_product="P1",  # 同产品，无换型
        setup_time=0,
        ready_at=_at(9),
        duration=30,
        hard_end=_at(18),
        changeover_rules=(),
    )
    assert slot is not None
    assert slot.start == _at(10)  # 跳到占用结束点
    assert slot.end == _at(10, 30)


def test_slot_gap_insertion_leaves_changeover_for_following_job() -> None:
    """插入空隙时，必须为后一个不同产品的作业也留出换型时间（§3.1.3）。"""
    machine = _machine("CNC-01")
    m_tl = Timeline()
    m_tl.occupy(_at(8), _at(9), "P0")  # 之前做 P0
    m_tl.occupy(_at(10), _at(11), "P2")  # 后一个作业做 P2，10:00 开始
    rules = (_rule("R", m=None, fr=None, to=None, mins=30, spec=1),)

    # 作业做 P1：09:00 + setup(换型 P0→P1 =30) + dur(30) = 10:00。
    # 后续 P1→P2 换型 30 分钟要塞进 [10:00, 10:00)（后一作业 10:00 开始）放不下 → 跳过。
    slot = earliest_feasible_slot(
        m_tl,
        Timeline(),
        machine=machine,
        to_product="P1",
        setup_time=0,
        ready_at=_at(9),
        duration=30,
        hard_end=_at(18),
        changeover_rules=rules,
    )
    # 09:00 起放不下（给 P2 的换型塞不进），扫描到下一候选起点 10:00（P2 结束点之外无更早点），
    # 但 10:00 被 P2 占用；再下一个候选是 11:00（P2 结束点）
    assert slot is not None
    assert slot.start == _at(11)


def test_slot_gap_insertion_fits_when_following_job_same_product() -> None:
    """后一个作业同产品时无换型要求，空隙可紧贴填入。"""
    machine = _machine("CNC-01")
    m_tl = Timeline()
    m_tl.occupy(_at(10), _at(11), "P1")  # 后一个作业也做 P1
    slot = earliest_feasible_slot(
        m_tl,
        Timeline(),
        machine=machine,
        to_product="P1",
        setup_time=0,
        ready_at=_at(9),
        duration=60,  # 09:00–10:00 恰好贴住 P1 的 10:00
        hard_end=_at(18),
        changeover_rules=(),
    )
    assert slot is not None
    assert slot.start == _at(9)
    assert slot.end == _at(10)


def test_slot_requires_both_machine_and_worker_free() -> None:
    machine = _machine("CNC-01")
    m_tl = Timeline()
    w_tl = Timeline()
    w_tl.occupy(_at(9), _at(10), "P1")  # 工人 09:00–10:00 忙（别的作业）
    slot = earliest_feasible_slot(
        m_tl,
        w_tl,
        machine=machine,
        to_product="P1",
        setup_time=0,
        ready_at=_at(9),
        duration=30,
        hard_end=_at(18),
        changeover_rules=(),
    )
    assert slot is not None
    assert slot.start == _at(10)  # 等工人空出来


def test_slot_returns_none_when_no_room_before_hard_end() -> None:
    machine = _machine("CNC-01")
    m_tl = Timeline()
    m_tl.occupy(_at(9), _at(17), "P1")  # 整天占满到 17:00
    slot = earliest_feasible_slot(
        m_tl,
        Timeline(),
        machine=machine,
        to_product="P1",
        setup_time=0,
        ready_at=_at(9),
        duration=120,
        hard_end=_at(18),  # 17:00 起 120 分钟 = 19:00 > 18:00
        changeover_rules=(),
    )
    assert slot is None


# --------------------------------------------------------------------------
# is_feasible_slot（task 11.1 反射断言签名不含 preference_rules）
# --------------------------------------------------------------------------


def test_is_feasible_slot_true_on_free_lines_within_hard_end() -> None:
    assert is_feasible_slot(
        Timeline(), Timeline(), start=_at(9), end=_at(10), hard_end=_at(18)
    ) is True


def test_is_feasible_slot_false_when_crossing_hard_end() -> None:
    assert is_feasible_slot(
        Timeline(), Timeline(), start=_at(17), end=_at(18, 30), hard_end=_at(18)
    ) is False


def test_is_feasible_slot_false_when_line_busy() -> None:
    m_tl = Timeline()
    m_tl.occupy(_at(9), _at(10), "P1")
    assert is_feasible_slot(
        m_tl, Timeline(), start=_at(9, 30), end=_at(10, 30), hard_end=_at(18)
    ) is False


def test_is_feasible_slot_signature_excludes_preference_rules() -> None:
    """task 11.1 会反射断言这一点；此处提前把它钉在单元测试里。"""
    import inspect

    params = set(inspect.signature(is_feasible_slot).parameters)
    assert "preference_rules" not in params
