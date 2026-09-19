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
"""

from __future__ import annotations

import inspect

import pytest
from pydantic import TypeAdapter, ValidationError

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
