"""`core/explain.py` 的示例测试（任务 5.11，R10.2–R10.6、R21.12）。

覆盖 `Explanation_Builder` 的**确定性内核部分**：

1. `counterfactual` 是**单值**（R10.3）——类型层面非 `Optional`、非 `list`，初始计划取
   `NoTradeoff`。
2. `build_explanation_payload` 的形状：按机器聚合、7 分量、基线含换算形式、不可排产 ≤5、
   关键作业 ≤6、**绝不含原始 Order/Machine/Worker/Material 清单**（R21.12），且 token 量级
   落在 ≈3,000（K-10）。
3. `default_plan_assumptions` 列出三条可能过期的输入，`derive_confidence` 据此定 LOW（R10.4/5）。
4. `TemplateExplanationRenderer` 纯函数、数字全来自结构化真值（回退文本天然通过闭世界比对）。
"""

from __future__ import annotations

from app.core.explain import (
    Assumption,
    Confidence,
    ConfidenceLevel,
    DecisionEvidence,
    Explanation,
    NoTradeoff,
    TemplateExplanationRenderer,
    Tradeoff,
    default_plan_assumptions,
    derive_confidence,
    estimate_payload_tokens,
    initial_plan_counterfactual,
)
from app.services.explanation import (
    BaselineView,
    ComponentView,
    ScheduledJobView,
    UnschedulableView,
    assemble_initial_plan_explanation,
)

_COMPONENT_NAMES = (
    "late_order_count",
    "total_tardiness_minutes",
    "urgent_order_lateness",
    "churn_ratio",
    "machine_utilisation",
    "total_changeover_minutes",
    "preference_penalty",
)


def _components() -> tuple[ComponentView, ...]:
    return tuple(
        ComponentView(name=name, raw_value=1.0, weight=2.0, weighted_contribution=2.0)
        for name in _COMPONENT_NAMES
    )


def _baseline() -> BaselineView:
    return BaselineView(
        on_time_rate=0.85,
        baseline_on_time_rate=0.6,
        total_tardiness_minutes=315,
        baseline_total_tardiness_minutes=900,
        late_order_count=2,
        baseline_late_order_count=5,
    )


def _scheduled(n: int = 10) -> tuple[ScheduledJobView, ...]:
    return tuple(
        ScheduledJobView(
            job_id=f"ORD-{i:03d}-OP1",
            order_id=f"ORD-{i:03d}",
            machine_id=f"CNC-{i % 3:02d}",
            duration_minutes=30 + i,
        )
        for i in range(n)
    )


# --------------------------------------------------------------------------
# 1. counterfactual 单值（R10.3）
# --------------------------------------------------------------------------


def test_initial_plan_counterfactual_is_single_no_tradeoff() -> None:
    """初始计划的反事实是**单个** `NoTradeoff`，不是 list、不是 None（R10.3）。"""
    cf = initial_plan_counterfactual()
    assert isinstance(cf, NoTradeoff)
    assert cf.reason


def test_explanation_counterfactual_field_accepts_single_value() -> None:
    """`Explanation.counterfactual` 承载单个值（Tradeoff 或 NoTradeoff），非容器。"""
    assumptions = default_plan_assumptions()
    exp = Explanation(
        plan_id="PLAN-x",
        decision_evidence=(),
        counterfactual=initial_plan_counterfactual(),
        assumptions=assumptions,
        confidence=derive_confidence(assumptions),
    )
    assert isinstance(exp.counterfactual, NoTradeoff)

    # Tradeoff 也是同一个单值字段的合法取值（任务 8.4 用）。
    tradeoff = Tradeoff(
        pivotal_job_id="JOB-004",
        component="total_tardiness_minutes",
        current_value=315.0,
        counterfactual_value=3195.0,
        selection_basis="dominant=total_tardiness_minutes, job=JOB-004",
    )
    exp2 = exp.model_copy(update={"counterfactual": tradeoff})
    assert isinstance(exp2.counterfactual, Tradeoff)


# --------------------------------------------------------------------------
# 2. 载荷形状 + 无原始清单 + token 量级（R21.12、K-10）
# --------------------------------------------------------------------------


def test_payload_aggregates_by_machine_and_has_seven_components() -> None:
    """载荷按机器聚合、含恰好 7 个目标分量、基线含预置换算形式（R7.2、R21.12）。"""
    inputs = assemble_initial_plan_explanation(
        plan_id="PLAN-x",
        feasibility="FEASIBLE",
        scheduled=_scheduled(),
        unschedulable=(),
        components=_components(),
        baseline=_baseline(),
    )
    payload = inputs.payload

    # 按机器聚合：3 台机器（CNC-00/01/02），每台带 job_count / busy_minutes。
    loads = payload["machine_loads"]
    assert isinstance(loads, list)
    assert {ml["machine_id"] for ml in loads} == {"CNC-00", "CNC-01", "CNC-02"}
    assert all("job_count" in ml and "busy_minutes" in ml for ml in loads)

    # 7 分量。
    assert len(payload["objective_breakdown"]) == 7

    # 基线含换算形式（措施②）：315 分钟 → "5 小时 15 分钟"。
    bc = payload["baseline_comparison"]
    assert bc["total_tardiness_minutes"] == 315
    assert bc["total_tardiness_human"] == "5 hours 15 minutes"


def test_payload_never_contains_raw_entity_lists() -> None:
    """载荷**绝不含**原始 Order / Machine / Worker / Material 清单（R21.12）。

    检查的是键名：任何以这些实体命名的顶层列表都不该出现。载荷只有聚合摘要与关键作业。
    """
    inputs = assemble_initial_plan_explanation(
        plan_id="PLAN-x",
        feasibility="FEASIBLE",
        scheduled=_scheduled(),
        unschedulable=(),
        components=_components(),
        baseline=_baseline(),
    )
    forbidden = {"orders", "machines", "workers", "materials", "scheduled_jobs"}
    assert forbidden.isdisjoint(inputs.payload.keys())


def test_payload_caps_unschedulable_and_key_jobs() -> None:
    """不可排产摘要 ≤5 条（含真实总数）、关键作业明细 ≤6 条（tasks.md 5.11）。"""
    unsched = tuple(
        UnschedulableView(
            job_id=f"ORD-9{i:02d}-OP1",
            order_id=f"ORD-9{i:02d}",
            blocking_reason="NO_MATERIAL",
        )
        for i in range(8)
    )
    inputs = assemble_initial_plan_explanation(
        plan_id="PLAN-x",
        feasibility="PARTIAL",
        scheduled=_scheduled(20),
        unschedulable=unsched,
        components=_components(),
        baseline=_baseline(),
    )
    payload = inputs.payload

    assert payload["unschedulable_summary"]["total"] == 8
    assert len(payload["unschedulable_summary"]["items"]) == 5
    assert len(payload["key_jobs"]) == 6


def test_payload_token_estimate_is_in_the_compact_range() -> None:
    """载荷 token 量级落在 ≈3,000 以内（K-10 的 4,000 上限留出输出 + 前缀余量）。"""
    inputs = assemble_initial_plan_explanation(
        plan_id="PLAN-x",
        feasibility="FEASIBLE",
        scheduled=_scheduled(30),
        unschedulable=(),
        components=_components(),
        baseline=_baseline(),
    )
    assert estimate_payload_tokens(inputs.payload) <= 3_000


# --------------------------------------------------------------------------
# 3. 假设 + 置信度派生（R10.4、R10.5）
# --------------------------------------------------------------------------


def test_default_assumptions_list_the_three_stale_inputs() -> None:
    """三条可能过期的输入：到货 ETA、机器修复估计、物料首道工序一次性预留（R10.4）。"""
    kinds = {a.kind for a in default_plan_assumptions()}
    assert kinds == {"DELIVERY_ETA", "MACHINE_REPAIR_ETA", "MATERIAL_RESERVATION"}


def test_confidence_derivation_is_deterministic() -> None:
    """置信度按可能过期的假设数确定性派生：0→HIGH，1→MEDIUM，≥2→LOW（R10.5）。"""
    assert derive_confidence(()).level is ConfidenceLevel.HIGH
    one = (Assumption(kind="X", description="d", stale_risk="r"),)
    assert derive_confidence(one).level is ConfidenceLevel.MEDIUM
    assert derive_confidence(default_plan_assumptions()).level is ConfidenceLevel.LOW


# --------------------------------------------------------------------------
# 4. 模板渲染器（纯函数，回退路径）
# --------------------------------------------------------------------------


def test_template_renderer_is_pure_and_uses_only_structured_numbers() -> None:
    """模板渲染是纯函数；同一 Explanation 必得同一文本，且数字全来自结构化真值。"""
    assumptions = default_plan_assumptions()
    exp = Explanation(
        plan_id="PLAN-x",
        decision_evidence=(
            DecisionEvidence(
                job_id="JOB-004",
                trigger="MACHINE_BREAKDOWN",
                constraint="MACHINE_UNAVAILABLE",
                resources=("CNC-01",),
            ),
        ),
        counterfactual=initial_plan_counterfactual(),
        assumptions=assumptions,
        confidence=Confidence(level=ConfidenceLevel.LOW, basis="依赖 3 项可能过期的输入。"),
    )
    renderer = TemplateExplanationRenderer()
    first = renderer.render(exp)
    second = renderer.render(exp)
    assert first == second
    assert "JOB-004" in first
    assert "LOW" in first
    assert "Counterfactual" in first
