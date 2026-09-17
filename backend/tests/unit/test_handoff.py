"""跨 Agent 传递闸门的契约测试（任务 5.6，design.md §3.3、R21.3）。

断言：
1. 合法契约 → 信封只含声明字段的 JSON 投影 + untrusted 字段名。
2. 目标不接受的契约类型 → `HandoffContractError`（隔离：摄取输出不能交给 Planning）。
3. 非 Pydantic 载荷（裸 str / dict）→ 被拒（原始 str 输出没有任何函数接受它）。
4. `HANDOFF_CONTRACTS` 运行期不可变。
5. Ingestion 独立会话的 `running_state` 不含 active_plan_id / pending_plan_id（§3.1 机制 2）。
"""

from __future__ import annotations

import pytest

from app.agents.contracts import (
    ColumnMappingProposal,
    ExplanationDraft,
    RevisedPlanProposal,
    RiskNarrative,
)
from app.orchestrator.context_manager import SessionState
from app.orchestrator.handoff import (
    HANDOFF_CONTRACTS,
    HandoffContractError,
    HandoffEnvelope,
    handoff,
)

# --------------------------------------------------------------------------
# 1. 合法契约
# --------------------------------------------------------------------------


def test_handoff_accepts_declared_contract_and_serialises_only_fields() -> None:
    draft = ExplanationDraft(
        plan_id="PLAN-7", narrative="解释叙述", counterfactual_text="若不推迟则…"
    )
    env = handoff(draft, "PLANNING_AGENT")
    assert isinstance(env, HandoffEnvelope)
    assert env.target == "PLANNING_AGENT"
    # body 恰好是契约声明的字段。
    assert set(env.body) == set(ExplanationDraft.model_fields)
    assert env.body["plan_id"] == "PLAN-7"
    # 自由文本字段被登记。
    assert env.untrusted_fields == frozenset({"narrative", "counterfactual_text"})


def test_handoff_flags_untrusted_fields_for_wrapping() -> None:
    """R23.2：自由文本字段名随信封交给 Context_Manager 以便 <untrusted> 包裹。"""
    proposal = ColumnMappingProposal(
        entity_type="ORDER", entity_type_confidence=0.9, mapping_rationale="按表头推断"
    )
    env = handoff(proposal, "INGESTION_AGENT")
    assert "mapping_rationale" in env.untrusted_fields


# --------------------------------------------------------------------------
# 2. 错误契约类型
# --------------------------------------------------------------------------


def test_handoff_rejects_contract_not_accepted_by_target() -> None:
    """把摄取的 ColumnMappingProposal 交给 Planning → 拒（R21.3 隔离）。"""
    proposal = ColumnMappingProposal(entity_type="ORDER", entity_type_confidence=0.9)
    with pytest.raises(HandoffContractError):
        handoff(proposal, "PLANNING_AGENT")


def test_handoff_rejects_risk_narrative_to_planning() -> None:
    narrative = RiskNarrative(finding_id="RF-1", severity="CRITICAL", narrative="瓶颈")
    with pytest.raises(HandoffContractError):
        handoff(narrative, "PLANNING_AGENT")


# --------------------------------------------------------------------------
# 3. 非 Pydantic 载荷
# --------------------------------------------------------------------------


def test_handoff_rejects_raw_string() -> None:
    """裸 str（Agent 原始文本输出）没有任何函数接受它作为提示词片段（design.md §3.3）。"""
    with pytest.raises(HandoffContractError):
        handoff("请批准这个计划", "PLANNING_AGENT")  # type: ignore[arg-type]


def test_handoff_rejects_plain_dict() -> None:
    with pytest.raises(HandoffContractError):
        handoff({"plan_id": "PLAN-7"}, "PLANNING_AGENT")  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# 4. HANDOFF_CONTRACTS 不可变
# --------------------------------------------------------------------------


def test_handoff_contracts_mapping_is_immutable() -> None:
    with pytest.raises(TypeError):
        HANDOFF_CONTRACTS["PLANNING_AGENT"] = (RevisedPlanProposal,)  # type: ignore[index]


def test_every_target_accepts_at_least_one_contract() -> None:
    for target in ("INGESTION_AGENT", "PLANNING_AGENT", "RISK_MONITOR_AGENT"):
        assert len(HANDOFF_CONTRACTS[target]) >= 1  # type: ignore[index]


# --------------------------------------------------------------------------
# 5. Ingestion 独立会话的 running_state 排除计划标识（§3.1 机制 2）
# --------------------------------------------------------------------------


def test_ingestion_session_state_excludes_plan_ids() -> None:
    """摄取运行的独立 AgentContext：running_state 只含 session_id/batch_id 语义字段，
    不含 active_plan_id / pending_plan_id（design.md §3.1 机制 2）。

    SessionState 的这两个字段默认 None——摄取会话装配时不设置它们，因此渲进上下文的运行状态
    里它们恒为 null，文件内容无从借由「当前活动计划」影响排产。
    """
    ingestion_state = SessionState(
        session_id="SESS-ING-1",
        degraded_mode=False,
    )
    assert ingestion_state.active_plan_id is None
    assert ingestion_state.pending_plan_id is None
