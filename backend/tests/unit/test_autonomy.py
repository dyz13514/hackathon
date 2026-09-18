"""`Autonomy_Policy_Engine` 分支覆盖 + 结构隔离证明（任务 7.3，承接原属性 18）。

**非可选**（tasks.md 任务 7.3、「绝不砍」清单第三条）。本文件覆盖任务 7.3 明确点名的
全部非可选断言：

1. **分支覆盖单元测试（承接原属性 18，满足 R27.10）**：R13.1 三条判据的**每个合取项各一个
   刚好越界的用例**共 12 例，另加 P0 默认下 `IMPACT_MINOR → L3` 的用例，以及
   `IMPACT_MODERATE → L3`、`IMPACT_MAJOR → L5`（P0 值域 {L3, L5}）。

2. **结构性证明测试（三条之一）**：
   - ① 反射断言 `ImpactInput` 的 7 个字段无字符串类型——LLM 文本输出没有可注入的入口
     （R13.11、design.md §3.6 第 1 条）。
   - ② 把伪造 `impact_class` / `autonomy_level` 的 Agent 输出送入真实的输出校验路径
     （`validate_agent_output`，任务 5.9），断言这些保留键被剥离、写下
     `AGENT_RESERVED_KEY_DROPPED` 审计，且自主等级判定只从**数值** `ImpactInput` 得出——
     伪造的字符串在结构上到不了 `classify_impact` / `decide_autonomy`。

3. **`decide_autonomy` 的「不可覆盖」是结构性的**：`IMPACT_MAJOR` 分支在读取 `FeatureFlags`
   之前返回，flags 在该路径上不被求值（R13.5、design.md §3.6 第 5 条）。

4. **`decisive_predicates`**：以「哪个条件先失败」形式返回判据（R13.12）。

`ImpactInput` 的 7 个字段（R13.1 判据来源）：
    changed_job_count, touches_urgent_or_high, promised_date_changed,
    all_within_same_machine_and_shift, tardiness_delta_minutes,
    new_unschedulable_count, churn_ratio
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import SecretStr
from sqlalchemy import Engine, Row, select

from app.agents.contracts import ExplanationDraft
from app.core.autonomy import (
    ActivePlanRow,
    AutonomyLevel,
    CandPlanRow,
    FeatureFlags,
    ImpactClass,
    ImpactInput,
    classify_impact,
    decide_autonomy,
    decisive_predicates,
)
from app.core.delta import PlanDelta
from app.db import audit
from app.db.models import AuditLog, Base
from app.db.session import create_db_engine
from app.services.guardrail import validate_agent_output
from app.settings import Settings

# --------------------------------------------------------------------------
# ImpactInput 工厂：默认落在 IMPACT_MINOR 的「全部满足」区域
# --------------------------------------------------------------------------

#: 基准输入：满足 R13.1 IMPACT_MINOR 的全部 6 个合取项 → classify 为 IMPACT_MINOR。
#: 各越界用例只翻动**一个**判据，证明该判据单独就足以把等级推出 MINOR。
_MINOR_BASE: dict[str, Any] = {
    "changed_job_count": 2,  # ≤ 2
    "touches_urgent_or_high": False,
    "promised_date_changed": False,
    "all_within_same_machine_and_shift": True,
    "tardiness_delta_minutes": 0,  # ≤ 0
    "new_unschedulable_count": 0,
    "churn_ratio": 0.0,  # ≤ 0.20
}


def _impact(**overrides: Any) -> ImpactInput:
    """构造一个 `ImpactInput`，默认全部满足 MINOR，用 overrides 翻动单个判据。"""
    return ImpactInput(**{**_MINOR_BASE, **overrides})


# ==========================================================================
# 1a. IMPACT_MINOR 的六个合取项：各一个「刚好越界」用例（原属性 18 的核心）
# ==========================================================================


def test_minor_baseline_all_conditions_satisfied() -> None:
    """六条 MINOR 判据全满足 → IMPACT_MINOR（越界用例的对照基准）。"""
    assert classify_impact(_impact()) is ImpactClass.IMPACT_MINOR


def test_minor_boundary_changed_job_count_just_over() -> None:
    """变更作业数 2→3：刚好越过「≤ 2」，跌出 MINOR（其余判据仍 MODERATE 合格 → MODERATE）。"""
    x = _impact(changed_job_count=3)
    assert classify_impact(x) is ImpactClass.IMPACT_MODERATE


def test_minor_boundary_touches_high_priority() -> None:
    """涉及 URGENT/HIGH 订单：跌出 MINOR。其余 MODERATE 判据仍合格 → MODERATE。"""
    x = _impact(touches_urgent_or_high=True)
    assert classify_impact(x) is ImpactClass.IMPACT_MODERATE


def test_minor_boundary_promised_date_changed_goes_major() -> None:
    """改变 promised_date：同时击穿 MINOR 与 MODERATE 的第 1 条 → 直接 IMPACT_MAJOR。"""
    x = _impact(promised_date_changed=True)
    assert classify_impact(x) is ImpactClass.IMPACT_MAJOR


def test_minor_boundary_not_same_machine_and_shift() -> None:
    """跨机器/跨班次（reassigned 存在）：跌出 MINOR → MODERATE。"""
    x = _impact(all_within_same_machine_and_shift=False)
    assert classify_impact(x) is ImpactClass.IMPACT_MODERATE


def test_minor_boundary_tardiness_delta_just_positive() -> None:
    """拖期增量 0→1 分钟：刚好越过 MINOR 的「≤ 0」，但仍 ≤ 60 → MODERATE。"""
    x = _impact(tardiness_delta_minutes=1)
    assert classify_impact(x) is ImpactClass.IMPACT_MODERATE


def test_minor_boundary_new_unschedulable_just_over() -> None:
    """新增 1 个不可排产作业：同时击穿 MINOR 与 MODERATE 的对应条 → IMPACT_MAJOR。"""
    x = _impact(new_unschedulable_count=1)
    assert classify_impact(x) is ImpactClass.IMPACT_MAJOR


# ==========================================================================
# 1b. IMPACT_MODERATE 的四个合取项：各一个「刚好越界」用例
# ==========================================================================
#
# MODERATE 的四条：not promised_date_changed / churn_ratio ≤ 0.20 /
# tardiness_delta ≤ 60 / new_unschedulable == 0。
# 从一个「已跌出 MINOR 但仍在 MODERATE 内」的输入出发，各翻一个 MODERATE 判据到刚好越界，
# 断言跌到 IMPACT_MAJOR。用 changed_job_count=3 把基准移出 MINOR。

#: 已跌出 MINOR、但四条 MODERATE 判据仍全部满足 → MODERATE。
_MODERATE_BASE: dict[str, Any] = {**_MINOR_BASE, "changed_job_count": 3}


def _moderate(**overrides: Any) -> ImpactInput:
    return ImpactInput(**{**_MODERATE_BASE, **overrides})


def test_moderate_baseline_is_moderate() -> None:
    """跌出 MINOR 但四条 MODERATE 判据全满足 → IMPACT_MODERATE（越界用例的基准）。"""
    assert classify_impact(_moderate()) is ImpactClass.IMPACT_MODERATE


def test_moderate_boundary_promised_date_changed() -> None:
    """MODERATE 第 1 条：promised_date 改变 → IMPACT_MAJOR。"""
    assert classify_impact(_moderate(promised_date_changed=True)) is ImpactClass.IMPACT_MAJOR


def test_moderate_boundary_churn_ratio_just_over() -> None:
    """MODERATE 第 2 条：churn_ratio 0.20→0.2001 刚好越过「≤ 0.20」→ IMPACT_MAJOR。"""
    assert classify_impact(_moderate(churn_ratio=0.2001)) is ImpactClass.IMPACT_MAJOR


def test_moderate_boundary_churn_ratio_exactly_020_stays_moderate() -> None:
    """边界含等号：churn_ratio 恰为 0.20 仍属 MODERATE（「≤」而非「<」）。"""
    assert classify_impact(_moderate(churn_ratio=0.20)) is ImpactClass.IMPACT_MODERATE


def test_moderate_boundary_tardiness_delta_just_over_60() -> None:
    """MODERATE 第 3 条：拖期增量 60→61 刚好越过「≤ 60」→ IMPACT_MAJOR。"""
    assert classify_impact(_moderate(tardiness_delta_minutes=61)) is ImpactClass.IMPACT_MAJOR


def test_moderate_boundary_tardiness_delta_exactly_60_stays_moderate() -> None:
    """边界含等号：拖期增量恰为 60 仍属 MODERATE。"""
    assert classify_impact(_moderate(tardiness_delta_minutes=60)) is ImpactClass.IMPACT_MODERATE


def test_moderate_boundary_new_unschedulable_just_over() -> None:
    """MODERATE 第 4 条：新增 1 个不可排产作业 → IMPACT_MAJOR。"""
    assert classify_impact(_moderate(new_unschedulable_count=1)) is ImpactClass.IMPACT_MAJOR


# ==========================================================================
# 2. decide_autonomy：P0 值域 {L3, L5}，含默认下 IMPACT_MINOR → L3
# ==========================================================================

_P0_FLAGS = FeatureFlags(auto_apply_minor_enabled=False)


def test_decide_autonomy_minor_maps_to_l3_under_p0_default() -> None:
    """P0 默认（auto_apply_minor_enabled=False）下 IMPACT_MINOR → L3（R13.6/R13.8）。"""
    assert decide_autonomy(ImpactClass.IMPACT_MINOR, _P0_FLAGS) is AutonomyLevel.L3


def test_decide_autonomy_moderate_maps_to_l3() -> None:
    """IMPACT_MODERATE → L3（R13.6）。"""
    assert decide_autonomy(ImpactClass.IMPACT_MODERATE, _P0_FLAGS) is AutonomyLevel.L3


def test_decide_autonomy_major_maps_to_l5() -> None:
    """IMPACT_MAJOR → L5（R13.5，不可覆盖）。"""
    assert decide_autonomy(ImpactClass.IMPACT_MAJOR, _P0_FLAGS) is AutonomyLevel.L5


def test_decide_autonomy_minor_l4_only_when_flag_enabled_p1() -> None:
    """P1：仅当 auto_apply_minor_enabled=True 时 IMPACT_MINOR → L4（R13.7）。"""
    flags = FeatureFlags(auto_apply_minor_enabled=True)
    assert decide_autonomy(ImpactClass.IMPACT_MINOR, flags) is AutonomyLevel.L4


def test_decide_autonomy_major_is_not_overridable_by_flag() -> None:
    """IMPACT_MAJOR 即便开了 auto_apply_minor_enabled 也恒为 L5（R13.5：不可覆盖）。"""
    flags = FeatureFlags(auto_apply_minor_enabled=True)
    assert decide_autonomy(ImpactClass.IMPACT_MAJOR, flags) is AutonomyLevel.L5


class _ExplodingFlags:
    """读取 `auto_apply_minor_enabled` 即抛错的探针（非 FeatureFlags 子类）。

    `FeatureFlags` 是 frozen dataclass，无法用 `@property` 覆盖其字段（会与生成的
    构造器冲突）。因此这里用一个**独立的**探针对象：它不是 `FeatureFlags`，但
    `decide_autonomy` 只读取 `flags.auto_apply_minor_enabled` 这一个属性——一旦该属性被
    访问就抛错。

    用于证明 `IMPACT_MAJOR` 分支**在读取 flags 之前**就返回 L5（design.md §3.6 第 5 条的
    「结构化不可覆盖」）：若该分支求值了 flags，本探针会让测试炸出 `AssertionError`。
    """

    @property
    def auto_apply_minor_enabled(self) -> bool:
        raise AssertionError("IMPACT_MAJOR path must not read FeatureFlags")


def test_decide_autonomy_major_does_not_evaluate_flags() -> None:
    """结构性证明：IMPACT_MAJOR 路径不求值 flags（读到即抛，测试却应通过并得 L5）。"""
    probe = cast("FeatureFlags", _ExplodingFlags())
    assert decide_autonomy(ImpactClass.IMPACT_MAJOR, probe) is AutonomyLevel.L5


# ==========================================================================
# 3. decisive_predicates：「哪个条件先失败」形式（R13.12）
# ==========================================================================


def test_decisive_predicates_minor_reports_all_satisfied() -> None:
    """MINOR：判据列表表明全部条件满足。"""
    x = _impact()
    preds = decisive_predicates(x, ImpactClass.IMPACT_MINOR)
    assert preds == ["all_minor_conditions_satisfied=true"]


def test_decisive_predicates_lists_failing_conjuncts() -> None:
    """越界用例：判据以「field=value op threshold」形式列出失败的合取项。"""
    x = _impact(changed_job_count=3, touches_urgent_or_high=True)
    preds = decisive_predicates(x, classify_impact(x))
    assert "changed_job_count=3 > 2" in preds
    assert "touches_high_priority=true" in preds


def test_decisive_predicates_major_includes_moderate_failure() -> None:
    """MAJOR：既列 MINOR 的失败，也列把它推到 MAJOR 的 MODERATE 失败（如 churn > 0.20）。"""
    x = _moderate(churn_ratio=0.5)
    cls = classify_impact(x)
    assert cls is ImpactClass.IMPACT_MAJOR
    preds = decisive_predicates(x, cls)
    assert any("churn_ratio" in p and "> 0.20" in p for p in preds)


# ==========================================================================
# 4. 结构性证明 ①：ImpactInput 的 7 个字段无字符串类型（R13.11）
# ==========================================================================


def test_impact_input_has_no_string_fields() -> None:
    """反射断言：`ImpactInput` 的 7 个字段全部是 int / bool / float，无 str（design.md §3.6）。

    这是「LLM 文本输出结构上无法影响分级」的机器可检查形式：不存在字符串字段，就没有
    模型输出可以落脚的注入点。若有人日后加了一个 `impact_class_hint: str`，本测试立刻失败。
    """
    hints = {f.name: f.type for f in dataclasses.fields(ImpactInput)}
    # 期望的 7 个字段一个不多不少。
    assert set(hints) == {
        "changed_job_count",
        "touches_urgent_or_high",
        "promised_date_changed",
        "all_within_same_machine_and_shift",
        "tardiness_delta_minutes",
        "new_unschedulable_count",
        "churn_ratio",
    }
    # 解析注解（dataclass 存的是字符串形式的注解），断言全部落在 {int, bool, float}。
    resolved = {
        name: eval(t) if isinstance(t, str) else t  # noqa: S307 - 注解来自本模块，可信
        for name, t in hints.items()
    }
    allowed = {int, bool, float}
    for name, typ in resolved.items():
        assert typ in allowed, f"字段 {name} 的类型 {typ!r} 不在 {allowed}（不得引入字符串注入点）"
        assert typ is not str


# ==========================================================================
# 5. 结构性证明 ②：伪造 impact_class/autonomy_level 被剥离 + 审计，判定仍从数值得出
# ==========================================================================


def _settings(db_path: str) -> Settings:
    return Settings(
        database_url=f"sqlite:///{db_path}",
        session_shared_password=SecretStr("test-shared-password"),
        session_secret_key=SecretStr("test-secret-key-that-is-long-enough-32"),
        llm_mode="STUB",
    )


@pytest.fixture
def audit_engine(tmp_path: Path) -> Iterator[Engine]:
    """独立审计引擎：保留键被剥离时的 AGENT_RESERVED_KEY_DROPPED 写这里。"""
    db_file = (tmp_path / "autonomy.db").as_posix()
    engine = create_db_engine(_settings(db_file))
    Base.metadata.create_all(engine)
    audit.set_audit_engine(engine)
    yield engine
    audit.set_audit_engine(None)
    engine.dispose()


def _dropped_rows(engine: Engine) -> list[Row[Any]]:
    with engine.connect() as conn:
        return list(
            conn.execute(
                select(
                    AuditLog.event_type,
                    AuditLog.actor,
                    AuditLog.payload,
                ).where(AuditLog.event_category == "AGENT_RESERVED_KEY_DROPPED")
            ).all()
        )


def test_forged_impact_class_is_stripped_and_audited(audit_engine: Engine) -> None:
    """Agent 伪造 impact_class/autonomy_level：被剥离 + 写 AGENT_RESERVED_KEY_DROPPED（R13.11）。

    这是 EVAL-209 在 §7 侧的结构性证明：模型试图声称自己的自主等级/影响分级，输出校验路径
    （任务 5.9 的 `validate_agent_output`）在进入契约前就把这两个保留键剪掉并留痕。
    """
    forged = {
        "plan_id": "PLAN-7",
        "narrative": "该修订计划优先保障高优先级订单。",
        # 越权声明——必须被剥离，绝不进入契约或执行路径。
        "impact_class": "IMPACT_MINOR",
        "autonomy_level": "L4",
    }
    result = validate_agent_output(
        forged, ExplanationDraft, agent="PLANNING_AGENT", trace_id="TRACE-autonomy"
    )
    # 剥离后仍是合法契约实例，且不含被伪造的字段。
    assert isinstance(result, ExplanationDraft)
    assert not hasattr(result, "impact_class")
    assert not hasattr(result, "autonomy_level")

    rows = _dropped_rows(audit_engine)
    assert len(rows) == 1
    row = rows[0]
    assert row.event_type == "AGENT_RESERVED_KEY_DROPPED"
    assert row.actor == "PLANNING_AGENT"
    payload = cast("dict[str, Any]", row.payload)
    assert set(payload["dropped_keys"]) == {"autonomy_level", "impact_class"}


def test_classification_ignores_forged_strings_uses_numeric_input(
    audit_engine: Engine,
) -> None:
    """判定只从数值 ImpactInput 得出：伪造的字符串 impact_class 到不了 classify/decide。

    即便 Agent 声称 `impact_class="IMPACT_MINOR"`，确定性引擎对同一场景（改了 promised_date）
    的数值输入判定为 IMPACT_MAJOR → L5。伪造声明既被剥离（上一个测试），又在类型上无法进入
    `classify_impact`（它只接受 `ImpactInput`，7 个数值字段）。
    """
    # 确定性引擎看到的是数值事实：promised_date 变了 → MAJOR。
    numeric = _impact(promised_date_changed=True)
    cls = classify_impact(numeric)
    level = decide_autonomy(cls, _P0_FLAGS)
    assert cls is ImpactClass.IMPACT_MAJOR
    assert level is AutonomyLevel.L5
    # 与 Agent 伪造的 "IMPACT_MINOR"/"L4" 相反——数值事实胜出。


# ==========================================================================
# 6. ImpactInput.from_delta 与两个辅助函数（覆盖 from_delta / 高优先级 / 同机同班次）
# ==========================================================================
#
# from_delta 是 ImpactInput 的唯一合法产生路径（design.md §3.6 第 2 条）。这里驱动它以及
# 它调用的两个辅助函数 _any_changed_job_touches_high_priority /
# _all_within_same_machine_and_shift 的各分支，使 core/autonomy.py 达 100% 分支覆盖。


def _delta(
    *,
    added: tuple[str, ...] = (),
    removed: tuple[str, ...] = (),
    moved: tuple[str, ...] = (),
    reassigned: tuple[str, ...] = (),
    unchanged: tuple[str, ...] = (),
    churn_ratio: float = 0.0,
) -> PlanDelta:
    return PlanDelta(
        added=added,
        removed=removed,
        moved=moved,
        reassigned=reassigned,
        unchanged=unchanged,
        churn_ratio=churn_ratio,
    )


def _active_row(
    *,
    total_tardiness_minutes: int = 0,
    unschedulable_count: int = 0,
    priorities: dict[str, int] | None = None,
    machines: dict[str, str] | None = None,
    workers: dict[str, str] | None = None,
    shift_end_ts: dict[str, int] | None = None,
    end_ts: dict[str, int] | None = None,
) -> ActivePlanRow:
    return ActivePlanRow(
        total_tardiness_minutes=total_tardiness_minutes,
        unschedulable_count=unschedulable_count,
        job_priority_map=priorities or {},
        job_machine_map=machines or {},
        job_worker_map=workers or {},
        job_shift_end_ts=shift_end_ts or {},
        job_end_ts=end_ts or {},
    )


def _cand_row(
    *,
    total_tardiness_minutes: int = 0,
    unschedulable_count: int = 0,
    promised_date_changed: bool = False,
    machines: dict[str, str] | None = None,
    workers: dict[str, str] | None = None,
    shift_end_ts: dict[str, int] | None = None,
    end_ts: dict[str, int] | None = None,
) -> CandPlanRow:
    return CandPlanRow(
        total_tardiness_minutes=total_tardiness_minutes,
        unschedulable_count=unschedulable_count,
        promised_date_changed=promised_date_changed,
        job_machine_map=machines or {},
        job_worker_map=workers or {},
        job_shift_end_ts=shift_end_ts or {},
        job_end_ts=end_ts or {},
    )


def test_from_delta_minor_scenario_all_numeric() -> None:
    """from_delta：干净的小改动 → 全部判据落在 MINOR 区，new_unschedulable 夹到 0。"""
    delta = _delta(moved=("ORD-1-OP1",), unchanged=("ORD-2-OP1",), churn_ratio=0.1)
    active = _active_row(
        total_tardiness_minutes=100,
        unschedulable_count=2,
        priorities={"ORD-1-OP1": 2},  # NORMAL
        machines={"ORD-1-OP1": "CNC-01"},
        shift_end_ts={"ORD-1-OP1": 10_000},
        end_ts={"ORD-1-OP1": 5_000},
    )
    cand = _cand_row(
        total_tardiness_minutes=90,  # 改善 → delta = -10 ≤ 0
        unschedulable_count=1,  # 比 active 少 → max(0, -1) = 0
        machines={"ORD-1-OP1": "CNC-01"},
        shift_end_ts={"ORD-1-OP1": 10_000},
        end_ts={"ORD-1-OP1": 6_000},  # 仍在班次内
    )
    x = ImpactInput.from_delta(delta, active, cand)
    assert x.changed_job_count == 1
    assert x.touches_urgent_or_high is False
    assert x.all_within_same_machine_and_shift is True
    assert x.tardiness_delta_minutes == -10
    assert x.new_unschedulable_count == 0
    assert classify_impact(x) is ImpactClass.IMPACT_MINOR


def test_from_delta_touches_high_priority_true() -> None:
    """辅助函数：变更集含 HIGH（rank=1）订单 → touches_urgent_or_high=True。"""
    delta = _delta(reassigned=("ORD-9-OP1",), churn_ratio=0.1)
    active = _active_row(priorities={"ORD-9-OP1": 1})  # HIGH
    cand = _cand_row()
    x = ImpactInput.from_delta(delta, active, cand)
    assert x.touches_urgent_or_high is True


def test_from_delta_touches_high_priority_false_when_unknown_or_low() -> None:
    """辅助函数：变更 job 不在优先级表（新增作业）或非高优先级 → False。"""
    delta = _delta(added=("ORD-NEW-OP1",), moved=("ORD-3-OP1",))
    active = _active_row(priorities={"ORD-3-OP1": 3})  # LOW；ORD-NEW 不在表里
    cand = _cand_row()
    x = ImpactInput.from_delta(delta, active, cand)
    assert x.touches_urgent_or_high is False


def test_from_delta_reassigned_makes_same_machine_false() -> None:
    """辅助函数：reassigned 非空 → all_within_same_machine_and_shift=False（提前返回）。"""
    delta = _delta(reassigned=("ORD-4-OP1",))
    x = ImpactInput.from_delta(delta, _active_row(), _cand_row())
    assert x.all_within_same_machine_and_shift is False


def test_from_delta_moved_different_machine_false() -> None:
    """辅助函数：moved 作业在 active/cand 的 machine 不同 → False。"""
    delta = _delta(moved=("ORD-5-OP1",))
    active = _active_row(machines={"ORD-5-OP1": "CNC-01"})
    cand = _cand_row(machines={"ORD-5-OP1": "CNC-02"})
    x = ImpactInput.from_delta(delta, active, cand)
    assert x.all_within_same_machine_and_shift is False


def test_from_delta_moved_past_shift_end_false() -> None:
    """辅助函数：moved 作业在候选中 end_time 越过 shift_end → False。"""
    delta = _delta(moved=("ORD-6-OP1",))
    active = _active_row(machines={"ORD-6-OP1": "CNC-01"})
    cand = _cand_row(
        machines={"ORD-6-OP1": "CNC-01"},
        shift_end_ts={"ORD-6-OP1": 5_000},
        end_ts={"ORD-6-OP1": 6_000},  # 越过班次结束
    )
    x = ImpactInput.from_delta(delta, active, cand)
    assert x.all_within_same_machine_and_shift is False


def test_from_delta_moved_within_shift_true() -> None:
    """辅助函数：moved 作业同机器、end_time 在班次内 → True。"""
    delta = _delta(moved=("ORD-7-OP1",))
    active = _active_row(machines={"ORD-7-OP1": "CNC-01"})
    cand = _cand_row(
        machines={"ORD-7-OP1": "CNC-01"},
        shift_end_ts={"ORD-7-OP1": 10_000},
        end_ts={"ORD-7-OP1": 8_000},  # 班次内
    )
    x = ImpactInput.from_delta(delta, active, cand)
    assert x.all_within_same_machine_and_shift is True


def test_from_delta_moved_missing_ts_maps_treated_as_within_shift() -> None:
    """辅助函数：moved 作业缺 end_ts/shift_end_ts（None）时不判越界 → True。"""
    delta = _delta(moved=("ORD-8-OP1",))
    active = _active_row(machines={"ORD-8-OP1": "CNC-01"})
    cand = _cand_row(machines={"ORD-8-OP1": "CNC-01"})  # 无 ts 映射
    x = ImpactInput.from_delta(delta, active, cand)
    assert x.all_within_same_machine_and_shift is True


# ==========================================================================
# 7. decisive_predicates 的 IMPACT_MAJOR 去重分支（覆盖 MODERATE 侧的补充判据）
# ==========================================================================


def test_decisive_predicates_major_promised_date_only_via_moderate_dedup() -> None:
    """MAJOR：promised_date 改变已在 MINOR 失败里，MODERATE 侧去重不重复添加。"""
    x = _impact(promised_date_changed=True)
    cls = classify_impact(x)
    assert cls is ImpactClass.IMPACT_MAJOR
    preds = decisive_predicates(x, cls)
    # promised_date_changed=true 只出现一次（MINOR 侧已加，MODERATE 侧跳过）。
    assert preds.count("promised_date_changed=true") == 1


def test_decisive_predicates_major_tardiness_over_60_dedup() -> None:
    """MAJOR：tardiness_delta > 60 时，MINOR 侧已记 '> 0'，MODERATE 侧不再重复加 '> 60'。"""
    x = _impact(tardiness_delta_minutes=120)
    cls = classify_impact(x)
    assert cls is ImpactClass.IMPACT_MAJOR
    preds = decisive_predicates(x, cls)
    # MINOR 侧已有 tardiness 条目，MODERATE 侧 any(...) 命中 → 不重复添加 '> 60'。
    tardiness_preds = [p for p in preds if "tardiness_delta_minutes" in p]
    assert len(tardiness_preds) == 1
    assert "> 0" in tardiness_preds[0]


def test_decisive_predicates_major_new_unschedulable_dedup() -> None:
    """MAJOR：new_unschedulable > 0 时 MINOR 侧已记，MODERATE 侧去重跳过。"""
    x = _impact(new_unschedulable_count=3)
    cls = classify_impact(x)
    assert cls is ImpactClass.IMPACT_MAJOR
    preds = decisive_predicates(x, cls)
    unsched_preds = [p for p in preds if "new_unschedulable_count" in p]
    assert len(unsched_preds) == 1


def test_decisive_predicates_major_churn_only_adds_via_moderate() -> None:
    """MAJOR：仅 churn_ratio 越界（MINOR 侧无对应条目）→ MODERATE 侧添加 churn 判据。"""
    x = _moderate(churn_ratio=0.9)
    cls = classify_impact(x)
    assert cls is ImpactClass.IMPACT_MAJOR
    preds = decisive_predicates(x, cls)
    assert any("churn_ratio=0.9000 > 0.20" == p for p in preds)


def test_decisive_predicates_major_new_unschedulable_added_when_not_in_minor() -> None:
    """MAJOR：changed 触发 MINOR 失败但 new_unschedulable 单独把它推到 MAJOR 的补充路径。

    构造：changed_job_count 越界（MINOR 失败），但 tardiness/churn 合规、new_unschedulable>0，
    使 new_unschedulable 的 MODERATE 侧补充判据被添加（MINOR 侧的 tardiness/unsched 也会记，
    这里断言 unschedulable 判据出现且只出现一次）。
    """
    x = _impact(changed_job_count=5, new_unschedulable_count=2)
    cls = classify_impact(x)
    assert cls is ImpactClass.IMPACT_MAJOR
    preds = decisive_predicates(x, cls)
    assert any("changed_job_count=5 > 2" == p for p in preds)
    assert len([p for p in preds if "new_unschedulable_count" in p]) == 1


def test_decisive_predicates_lists_same_machine_shift_failure() -> None:
    """decisive_predicates 列出 'all_within_same_machine_and_shift=false' 判据（覆盖该分支）。

    构造一个 MAJOR 输入（promised_date 改变），同时 all_within_same_machine_and_shift=False，
    使 MINOR 失败收集里的该条判据被执行并出现在结果中。
    """
    x = _impact(all_within_same_machine_and_shift=False, promised_date_changed=True)
    cls = classify_impact(x)
    assert cls is ImpactClass.IMPACT_MAJOR
    preds = decisive_predicates(x, cls)
    assert "all_within_same_machine_and_shift=false" in preds
