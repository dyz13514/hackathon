"""计划状态机迁移许可表的单元测试（任务 3.3，design.md Data Models §8）。

守两件事：

1. **表内的迁移恰好是 design.md §8「允许的迁移与执行权限」表列出的那些** —— 一条不多、
   一条不少。多一条意味着开了一个 §8 没批准的口子；少一条意味着某个合法动作会被误拒。
2. **一切表外迁移都非法** —— 尤其是任何「跳过 `PENDING_APPROVAL` 直达 `ACTIVE`」的尝试、
   自迁移、以及从终态（`REJECTED` / `SUPERSEDED`）出发的迁移。这是 R11.8 / R23.4「计划
   状态不可被绕过审批地修改」在状态层面的直接编码。
"""

from __future__ import annotations

import itertools

from app.db.models import PLAN_STATUSES
from app.services.plan_state_machine import (
    ALLOWED_TRANSITIONS,
    COMPONENT_APPROVAL_SERVICE,
    COMPONENT_SAVE_PROPOSED_PLAN,
    PlanStatus,
    is_allowed_transition,
    transition_authority,
)

#: design.md §8 表逐格誊写（`∅ → DRAFT` 不是状态间迁移，不在此列，见模块 docstring）。
EXPECTED_TRANSITIONS = {
    (PlanStatus.DRAFT, PlanStatus.PENDING_APPROVAL): COMPONENT_SAVE_PROPOSED_PLAN,
    (PlanStatus.PENDING_APPROVAL, PlanStatus.ACTIVE): COMPONENT_APPROVAL_SERVICE,
    (PlanStatus.PENDING_APPROVAL, PlanStatus.REJECTED): COMPONENT_APPROVAL_SERVICE,
    (PlanStatus.PENDING_APPROVAL, PlanStatus.SUPERSEDED): COMPONENT_APPROVAL_SERVICE,
    (PlanStatus.ACTIVE, PlanStatus.SUPERSEDED): COMPONENT_APPROVAL_SERVICE,
}


def test_table_matches_design_section_8_exactly() -> None:
    """`ALLOWED_TRANSITIONS` 与 design.md §8 表逐格相等（不多不少）。"""
    assert ALLOWED_TRANSITIONS == EXPECTED_TRANSITIONS


def test_plan_status_values_align_with_orm_constant() -> None:
    """`PlanStatus` 的取值与 `models.PLAN_STATUSES` 完全一致，避免两处漂移。"""
    assert {s.value for s in PlanStatus} == set(PLAN_STATUSES)


def test_only_pending_reaches_active() -> None:
    """通向 `ACTIVE` 的合法迁移**只有** `PENDING_APPROVAL → ACTIVE`（属性 15 的状态切片）。"""
    reach_active = [
        source
        for source in PlanStatus
        if is_allowed_transition(source, PlanStatus.ACTIVE)
    ]
    assert reach_active == [PlanStatus.PENDING_APPROVAL]


def test_out_of_table_transitions_are_invalid() -> None:
    """全部 `(from, to)` 组合里，只有表内那些合法，其余一律非法——含自迁移。"""
    for source, target in itertools.product(PlanStatus, repeat=2):
        expected = (source, target) in EXPECTED_TRANSITIONS
        assert is_allowed_transition(source, target) is expected, (source, target)


def test_self_transitions_are_never_allowed() -> None:
    """任何状态到它自己的「迁移」都非法（没有对应的业务动作）。"""
    for status in PlanStatus:
        assert is_allowed_transition(status, status) is False


def test_terminal_states_have_no_outgoing_transitions() -> None:
    """终态 `REJECTED` / `SUPERSEDED` 没有任何合法的出边。"""
    for terminal in (PlanStatus.REJECTED, PlanStatus.SUPERSEDED):
        assert all(
            not is_allowed_transition(terminal, target) for target in PlanStatus
        )


def test_string_inputs_are_accepted() -> None:
    """裸字符串（库里 `status` 列的形态）与枚举等价判定。"""
    assert is_allowed_transition("PENDING_APPROVAL", "ACTIVE") is True
    assert is_allowed_transition("ACTIVE", "PENDING_APPROVAL") is False


def test_unknown_status_is_treated_as_illegal() -> None:
    """未知状态（脏值）不抛异常，落在「非法迁移」一侧（安全方向）。"""
    assert is_allowed_transition("GARBAGE", "ACTIVE") is False
    assert is_allowed_transition("PENDING_APPROVAL", "WAT") is False
    assert transition_authority("GARBAGE", "ACTIVE") is None


def test_transition_authority_names_the_component() -> None:
    """合法迁移能查到唯一授权组件；非法迁移返回 None。"""
    assert (
        transition_authority(PlanStatus.PENDING_APPROVAL, PlanStatus.ACTIVE)
        == COMPONENT_APPROVAL_SERVICE
    )
    assert (
        transition_authority(PlanStatus.DRAFT, PlanStatus.PENDING_APPROVAL)
        == COMPONENT_SAVE_PROPOSED_PLAN
    )
    assert transition_authority(PlanStatus.ACTIVE, PlanStatus.DRAFT) is None
