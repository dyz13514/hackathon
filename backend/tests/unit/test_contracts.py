"""Agent 输出契约的契约测试（任务 5.6，design.md §3.3、R21.3、R23.2）。

断言：
1. 每个契约的 `untrusted_field_names()` 是其真实声明字段的子集（登记不指向幽灵字段）。
2. 自由文本字段确实被登记为 untrusted；结构化标识符/枚举字段不被误登记。
3. 全部契约 `extra="forbid"` + `frozen=True`（契约即给 LLM 的接口，且一经校验不可变）。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.agents.contracts import (
    AgentContract,
    ColumnMappingProposal,
    ExplanationDraft,
    PreferenceRuleCandidate,
    RevisedPlanProposal,
    RiskNarrative,
    ScenarioTranslation,
)

ALL_CONTRACTS: list[type[AgentContract]] = [
    ColumnMappingProposal,
    RevisedPlanProposal,
    ExplanationDraft,
    ScenarioTranslation,
    PreferenceRuleCandidate,
    RiskNarrative,
]


@pytest.mark.parametrize("contract", ALL_CONTRACTS)
def test_untrusted_fields_are_declared_fields(contract: type[AgentContract]) -> None:
    """登记的 untrusted 字段必须是模型真实声明的字段（否则 handoff 会包裹幽灵字段）。"""
    declared = set(contract.model_fields)
    assert contract.untrusted_field_names() <= declared


@pytest.mark.parametrize("contract", ALL_CONTRACTS)
def test_contracts_forbid_extra_and_are_frozen(contract: type[AgentContract]) -> None:
    """全部契约 extra=forbid + frozen（R22.1 同理 + §3.3 的定型）。"""
    assert contract.model_config.get("extra") == "forbid"
    assert contract.model_config.get("frozen") is True


def test_column_mapping_flags_rationale_as_untrusted() -> None:
    assert ColumnMappingProposal.untrusted_field_names() == frozenset(
        {"mapping_rationale"}
    )


def test_explanation_flags_narrative_fields_untrusted_but_not_plan_id() -> None:
    """narrative / counterfactual_text 是自由散文（untrusted）；plan_id 是标识符（受信任）。"""
    untrusted = ExplanationDraft.untrusted_field_names()
    assert untrusted == frozenset({"narrative", "counterfactual_text"})
    assert "plan_id" not in untrusted


def test_revised_plan_numeric_fields_are_trusted() -> None:
    """重排的数值字段来自确定性内核，不是模型编的 → 不登记为 untrusted。"""
    untrusted = RevisedPlanProposal.untrusted_field_names()
    assert untrusted == frozenset({"revision_summary"})
    for numeric in ("total_tardiness_minutes", "churn_ratio", "candidate_plan_id"):
        assert numeric not in untrusted


def test_contract_rejects_unknown_field() -> None:
    """extra=forbid：模型多吐一个键会被 schema 校验挡下（R22.1）。"""
    with pytest.raises(ValidationError):
        ExplanationDraft(plan_id="PLAN-1", narrative="x", surprise="nope")  # type: ignore[call-arg]


def test_contract_instance_is_immutable() -> None:
    """frozen：契约一经校验不可再改字段。"""
    draft = ExplanationDraft(plan_id="PLAN-1", narrative="正常解释")
    with pytest.raises(ValidationError):
        draft.plan_id = "PLAN-2"  # type: ignore[misc]
