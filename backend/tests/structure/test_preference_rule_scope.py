"""EVAL-206 结构性证明：偏好规则永远不能放宽硬约束（任务 11.1，R18.8，design.md §4.3）。

这条测试是 design.md §4.3「四道结构性保障」的可执行断言，**非可选**（tasks.md 14 的三条结构性
证明之一）。它守的是一个安全论证：无论规划员把偏好规则的权重设成多大，规则都**只能改变选谁**，
不能让一个违反硬约束的计划通过校验——因为校验函数的签名里根本没有偏好规则这个入口。

放在 `tests/structure/`（而不是 `tests/eval/`）是刻意的：`make test` 跑 `tests` 但
`--ignore=tests/eval`，因此只放 eval 目录会让这条护栏在日常 `make test` 里被静默跳过。结构性
证明必须在每次 `make test` 都执行。

两组断言，各守一道保障：

1. **签名不含 rules**（第 4 道保障）——用 `inspect.signature` 反射断言
   `Constraint_Validator.validate` 与 `Scheduling_Core` 的 `is_feasible_slot` 的形参里
   **不存在** `preference_rules`（或任何以 `preference`/`rule` 命名的参数）。即使有人把偏好规则
   权重设成天文数字，这两个函数也无从读到规则，因此规则只能出现在候选**排序**的 cost 里，
   不能出现在**可行性**判定里。这是「规则改变选谁、不改变可行性」在类型层面的证据。

2. **越界表单被拒**（第 1–3 道保障）——提交指向硬约束开关的 `structured_form`
   （`component="allow_shift_overflow"`），断言 Pydantic 判别联合拒绝它；再断言 4 类合法成员
   的 `weight_delta`/`multiplier` 边界（惩罚只能为正、倍数有界），使规则无法把「不可行」变成
   「可行」。

3. **11.2 接线的调用路径不碰硬约束**——偏好接入软评分/排序后，再从行为侧证一遍：
   `preference_delta` 恒 `>= 0`（只能让候选更不划算，不能更划算）；`apply_weight_overrides`
   只写 6 个软目标权重，绝不产生指向任何硬约束键的覆盖（即便喂给它一条越界规则）。
"""

from __future__ import annotations

import inspect
from decimal import Decimal

import pytest
from pydantic import TypeAdapter, ValidationError

from app.core import preference as pref
from app.core.scheduling import is_feasible_slot
from app.core.validation import validate
from app.tools.models import PreferenceForm

#: 任何暗示「偏好规则进入了这里」的形参名。命中即视为把软偏好接进了硬约束路径。
_FORBIDDEN_PARAM_SUBSTRINGS = ("preference", "pref_rule", "rule")

_FORM_ADAPTER: TypeAdapter[object] = TypeAdapter(PreferenceForm)


def _param_names(func: object) -> list[str]:
    return list(inspect.signature(func).parameters)  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# 保障 4：可行性判定函数的签名里没有偏好规则入口
# --------------------------------------------------------------------------


def test_validate_signature_has_no_preference_rules() -> None:
    """`Constraint_Validator.validate` 的签名里不含任何偏好规则参数（R18.8）。"""
    params = _param_names(validate)
    assert params == ["candidate", "snapshot"], (
        f"validate 的签名应恰为 (candidate, snapshot)，实际 {params}；"
        "偏好规则绝不能作为参数进入硬约束校验。"
    )
    for name in params:
        assert not any(bad in name.lower() for bad in _FORBIDDEN_PARAM_SUBSTRINGS)


def test_is_feasible_slot_signature_has_no_preference_rules() -> None:
    """`Scheduling_Core` 的 `is_feasible_slot` 的签名里不含任何偏好规则参数（R18.8）。"""
    params = _param_names(is_feasible_slot)
    for name in params:
        assert not any(bad in name.lower() for bad in _FORBIDDEN_PARAM_SUBSTRINGS), (
            f"is_feasible_slot 的参数 {name!r} 暗示偏好规则进入了可行性判定；"
            "规则只能出现在候选排序的 cost 里，不能出现在可行性判定里。"
        )


# --------------------------------------------------------------------------
# 保障 1–3：越界表单被类型层面拒绝
# --------------------------------------------------------------------------


def test_hard_constraint_component_is_rejected() -> None:
    """指向硬约束开关的 component（如 allow_shift_overflow）被判别联合拒绝（保障 3）。"""
    with pytest.raises(ValidationError):
        _FORM_ADAPTER.validate_python(
            {
                "kind": "ADJUST_OBJECTIVE_WEIGHT",
                "component": "allow_shift_overflow",
                "multiplier": 1.5,
            }
        )


def test_unknown_kind_is_rejected() -> None:
    """未知 kind（自由谓词/表达式）被拒——判别联合只有 4 个封闭成员（保障 1）。"""
    with pytest.raises(ValidationError):
        _FORM_ADAPTER.validate_python(
            {"kind": "ALLOW_DOUBLE_BOOKING", "machine_id": "CNC-03"}
        )


def test_weight_delta_must_be_positive_and_bounded() -> None:
    """weight_delta ∈ (0, 10]：惩罚只能为正，规则只能让某选择更不划算（保障 2）。"""
    base = {"kind": "AVOID_MACHINE_FOR_ORDER", "order_id": "O", "machine_id": "M"}
    for bad in (0, -5, 11):
        with pytest.raises(ValidationError):
            _FORM_ADAPTER.validate_python({**base, "weight_delta": bad})


def test_multiplier_is_bounded() -> None:
    """multiplier ∈ [0.5, 2.0]：不能把某个软目标归零或放大到无穷（保障 2）。"""
    for bad in (0.0, 0.4, 2.1, 100.0):
        with pytest.raises(ValidationError):
            _FORM_ADAPTER.validate_python(
                {
                    "kind": "ADJUST_OBJECTIVE_WEIGHT",
                    "component": "total_tardiness_minutes",
                    "multiplier": bad,
                }
            )


def test_four_legal_forms_accepted() -> None:
    """4 类合法表单在边界内被接受——证明拒绝的是越界，不是全部。"""
    legal = [
        {
            "kind": "AVOID_MACHINE_FOR_ORDER",
            "order_id": "O",
            "machine_id": "M",
            "weight_delta": 1.0,
        },
        {
            "kind": "AVOID_MACHINE_FOR_PRODUCT",
            "product_id": "P",
            "machine_id": "M",
            "weight_delta": 10,
        },
        {
            "kind": "PREFER_WORKER_FOR_SKILL",
            "skill": "welding",
            "worker_id": "W",
            "weight_delta": 5,
        },
        {"kind": "ADJUST_OBJECTIVE_WEIGHT", "component": "churn_ratio", "multiplier": 2.0},
    ]
    for form in legal:
        model = _FORM_ADAPTER.validate_python(form)
        assert model.kind == form["kind"]  # type: ignore[attr-defined]


# --------------------------------------------------------------------------
# 保障（行为侧，任务 11.2 接线）：偏好只加非负惩罚、只缩放软目标
# --------------------------------------------------------------------------


class _Rule:
    """最小的 rule 替身：只有 `rule_id` 与 `structured_form`（`core.preference` 只读这两者）。"""

    def __init__(self, rule_id: str, structured_form: dict[str, object]) -> None:
        self.rule_id = rule_id
        self.human_text = rule_id
        self.structured_form = structured_form


def test_preference_delta_signature_has_no_feasibility_inputs() -> None:
    """`preference_delta` 只接受 (job, machine, worker, rules)——不接受时间线/槽位等可行性输入。

    它算的是「这个放置新增多少偏好惩罚」，是候选**排序**的一项，绝不参与可行性判定。
    """
    params = _param_names(pref.preference_delta)
    assert params == ["job", "machine", "worker", "rules"], params


def test_preference_delta_is_never_negative_even_for_bogus_weight() -> None:
    """即便一条越界规则带来非正/超大 `weight_delta`，`preference_delta` 也恒 `>= 0`（方向性）。

    非负性是「偏好只能让候选更不划算、不能更划算」的数值保证——负惩罚会把不划算的选择变成
    更优选择，等于用偏好改变了排序方向乃至可行性论证。用简单替身对象喂各种权重。
    """

    class _Job:
        order_id = "O"
        product_id = "P"
        required_worker_skill = "welding"

    class _Machine:
        machine_id = "M"

    class _Worker:
        worker_id = "W2"

    for bogus_weight in (-100, 0, 10, 10**9):
        rule = _Rule(
            "PR-x",
            {
                "kind": "AVOID_MACHINE_FOR_ORDER",
                "order_id": "O",
                "machine_id": "M",
                "weight_delta": bogus_weight,
            },
        )
        delta = pref.preference_delta(_Job(), _Machine(), _Worker(), (rule,))
        assert delta >= Decimal("0"), f"weight_delta={bogus_weight} 时惩罚为负：{delta}"


def test_apply_weight_overrides_only_touches_soft_keys() -> None:
    """`apply_weight_overrides` 绝不产生指向硬约束键的覆盖，也不新增软目标之外的键。

    即便喂进一条 `component` 指向硬约束开关的越界规则，它也被跳过（不生效、不进 applied），
    因此权重 dict 的键集恒不变，硬约束键永远不会被偏好写入。
    """
    base = {
        "late_order_count": 100.0,
        "total_tardiness_minutes": 1.0,
        "urgent_order_lateness": 300.0,
        "churn_ratio": 500.0,
        "machine_utilisation": -50.0,
        "total_changeover_minutes": 0.5,
        "preference_penalty": 1.0,
    }
    rules = (
        _Rule(
            "PR-legit",
            {
                "kind": "ADJUST_OBJECTIVE_WEIGHT",
                "component": "total_tardiness_minutes",
                "multiplier": 2.0,
            },
        ),
        _Rule(
            "PR-bogus",
            {
                "kind": "ADJUST_OBJECTIVE_WEIGHT",
                "component": "allow_shift_overflow",
                "multiplier": 2.0,
            },
        ),
    )
    result, applied = pref.apply_weight_overrides(base, rules)

    # 键集不变：没有任何硬约束键被引入。
    assert set(result) == set(base)
    # 合法软目标覆盖生效，越界规则被跳过（只 1 条 applied）。
    assert [o["component"] for o in applied] == ["total_tardiness_minutes"]
    assert result["total_tardiness_minutes"] == 2.0
    # preference_penalty 分量本身不是 ADJUST_OBJECTIVE_WEIGHT 的合法 component，未被改。
    assert result["preference_penalty"] == 1.0
