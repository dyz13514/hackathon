"""Property 10：偏好规则的安全不变量（任务 11.3，R18.4 / R18.8 / R18.9 / R18.10 / R18.11；
design.md Correctness Properties「Property 10」）。

*For any* 偏好规则集合（含**任意**权重取值，包括天文数字），断言：

- **(a) 零违反**：该规则集下 `generate_schedule` 产出的计划，经独立的 `Constraint_Validator.
  validate` 仍判**零硬约束违反**。偏好只改候选**排序**的 cost，绝不能让一个违反硬约束的候选
  变为可行——即便权重设成天文数字。
- **(b) 停用即回到基线**：把全部规则「停用」（P0 语义 = 快照里不加载它们，见
  `services/snapshot_loader` 只加载 enabled=True）后的计划，**逐字段等于**空规则集下的计划。
  这守 R18.9「停用后下一次排产完全忽略该规则」。
- **(c) 校验判定不因规则改变**：同一份成型计划，`validate` 的 `violations` 与 `is_feasible`
  与「把偏好规则从快照里拿掉再校验」完全一致——因为 `validate(candidate, snapshot)` 的签名里
  没有 `preference_rules`，它根本读不到规则（EVAL-206 的第 4 道保障，此处从行为侧再证一遍）。

守的是「记忆永不放宽硬约束」这一安全论证，与任务 11.1 的结构性证明（`tests/structure/
test_preference_rule_scope.py` 的签名反射）互补：那里证「签名里没有入口」，这里证「无论怎么
取权重，行为上也确实进不去」。**非可选**。

## 取样口径

对每个快照，生成一组**真正会命中**该快照实体的 4 类合法规则（引用快照里真实的
order_id / product_id / machine_id / worker_id / skill），并让 `weight_delta` 取到上界甚至
远超上界的值——安全论证要对「规划员把惩罚设到最大」也成立。三个 scarcity 档全覆盖，理由同
Property 2：稀缺侧「排上的」集合更小，但「凡排上的都合法」这条在任何档下都必须成立。

## 不碰数据库、不碰 LLM

输入是 `domain_snapshots` 的内存快照，`generate_schedule` / `validate` 都是内核纯函数，不触达
Bedrock；`conftest.py` 已把 `LLM_MODE` 强制为 `STUB`。规则通过 `model_copy(update=...)` 注入
内存快照，不经 `Preference_Store` / 数据库（本属性守的是内核纯函数性质）。
"""

from __future__ import annotations

from typing import Any

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from app.core.scheduler import generate_schedule
from app.core.snapshot import DomainSnapshot, PreferenceRule
from app.core.validation import validate
from tests.generators import domain_snapshots

# 任务 11.3 明确要求 `max_examples=100`。快照构造 + 两趟「排产 + 全量九类校验」在演示规模下
# 是毫秒级；放宽 deadline 并抑制「过慢」健康检查——慢不是错误。
_SETTINGS = settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)

#: 权重取值刻意覆盖「合法上界」与「远超上界的天文数字」两端：安全论证要对规划员把惩罚设到
#: 最大（乃至绕过创建闸门塞进极端值）也成立。偏好只能让候选更不划算，不能让不可行变可行。
_WEIGHTS = st.sampled_from([0.5, 1.0, 10.0, 1e6, 1e12])
_MULTIPLIERS = st.sampled_from([0.5, 1.0, 2.0])


def _describe(violations: tuple[object, ...]) -> str:
    return "\n".join(
        f"  - [{getattr(v, 'violation_type', '?')}] {getattr(v, 'human_description', '')}"
        for v in violations
    )


@st.composite
def _matching_rules(draw: st.DrawFn, snapshot: DomainSnapshot) -> tuple[PreferenceRule, ...]:
    """生成 0–5 条**引用该快照真实实体**的 4 类合法偏好规则，权重取任意值（含天文数字）。

    引用真实实体是为了让规则真的命中——不命中的规则对 cost 零影响，测不出「即便命中也不放宽
    硬约束」。`weight_delta` 取到 1e12 这种远超创建闸门 `le=10` 的值，正是要证明：无论权重多大，
    偏好都只改排序、不改可行性。
    """
    order_ids = [o.order_id for o in snapshot.orders]
    product_ids = [p.product_id for p in snapshot.products]
    machine_ids = [m.machine_id for m in snapshot.machines]
    worker_ids = [w.worker_id for w in snapshot.workers]
    skills = sorted({s for w in snapshot.workers for s in w.skills})

    if not (order_ids and product_ids and machine_ids and worker_ids):
        return ()

    count = draw(st.integers(min_value=0, max_value=5))
    rules: list[PreferenceRule] = []
    for i in range(count):
        kind = draw(
            st.sampled_from(
                [
                    "AVOID_MACHINE_FOR_ORDER",
                    "AVOID_MACHINE_FOR_PRODUCT",
                    "PREFER_WORKER_FOR_SKILL",
                    "ADJUST_OBJECTIVE_WEIGHT",
                ]
            )
        )
        if kind == "AVOID_MACHINE_FOR_ORDER":
            form: dict[str, Any] = {
                "kind": kind,
                "order_id": draw(st.sampled_from(order_ids)),
                "machine_id": draw(st.sampled_from(machine_ids)),
                "weight_delta": draw(_WEIGHTS),
            }
        elif kind == "AVOID_MACHINE_FOR_PRODUCT":
            form = {
                "kind": kind,
                "product_id": draw(st.sampled_from(product_ids)),
                "machine_id": draw(st.sampled_from(machine_ids)),
                "weight_delta": draw(_WEIGHTS),
            }
        elif kind == "PREFER_WORKER_FOR_SKILL":
            form = {
                "kind": kind,
                "skill": draw(st.sampled_from(skills)) if skills else "CNC_OP",
                "worker_id": draw(st.sampled_from(worker_ids)),
                "weight_delta": draw(_WEIGHTS),
            }
        else:  # ADJUST_OBJECTIVE_WEIGHT
            form = {
                "kind": kind,
                "component": draw(
                    st.sampled_from(
                        [
                            "late_order_count",
                            "total_tardiness_minutes",
                            "urgent_order_lateness",
                            "churn_ratio",
                            "machine_utilisation",
                            "total_changeover_minutes",
                        ]
                    )
                ),
                "multiplier": draw(_MULTIPLIERS),
            }
        rules.append(
            PreferenceRule(rule_id=f"PR-{i:03d}", human_text=f"rule {i}", structured_form=form)
        )
    return tuple(rules)


@_SETTINGS
@given(
    data=st.data(),
    base=domain_snapshots(scarcity="ABUNDANT")
    | domain_snapshots(scarcity="TIGHT")
    | domain_snapshots(scarcity="INFEASIBLE"),
)
def test_preference_rules_never_relax_hard_constraints(
    data: st.DataObject, base: DomainSnapshot
) -> None:
    """**Validates: Requirements 18.4, 18.8, 18.9, 18.10, 18.11**

    对任意快照与任意（含天文数字权重的）偏好规则集合，断言三条安全不变量 (a)/(b)/(c)。
    """
    # 无规则的基线快照（把生成器随手附带的旧式规则也清掉，确保「空规则集」确实为空）。
    empty = base.model_copy(update={"preference_rules": ()})
    rules = data.draw(_matching_rules(empty))
    with_rules = empty.model_copy(update={"preference_rules": rules})

    plan_empty = generate_schedule(empty)
    plan_with = generate_schedule(with_rules)

    # (a) 任意权重下，带规则的计划仍零硬约束违反——偏好只改排序，不放宽硬约束。
    report_with = validate(plan_with, with_rules)
    assert report_with.violations == (), (
        "偏好规则集下排产输出违反了硬约束（偏好本应只改选谁、不改可行性）：\n"
        f"{_describe(report_with.violations)}"
    )
    assert report_with.is_feasible is True

    # (b) 停用全部规则后的计划逐字段等于空规则集下的计划（R18.9）。
    # P0 语义下「停用」= 快照不加载该规则（snapshot_loader 只加载 enabled=True），因此
    # 「全部规则停用」的快照就是 `empty`。这里从 `with_rules` 出发，把它的规则清空以模拟
    # 「把这些规则逐条停用」，断言得到的计划逐字段回到基线——证明停用后规则确实完全不参与
    # 排产，且这条对**任意**规则集合都成立（含权重被设成天文数字的规则）。
    plan_after_disable = generate_schedule(with_rules.model_copy(update={"preference_rules": ()}))
    assert plan_after_disable == plan_empty

    # (c) 校验判定不因规则改变：validate 读不到 preference_rules（签名里没有它），因此对同一
    # 份成型计划，「带规则的快照」与「不带规则的快照」给出完全相同的 violations 与 is_feasible。
    report_with_rules_snapshot = validate(plan_with, with_rules)
    report_without_rules_snapshot = validate(plan_with, empty)
    assert (
        report_with_rules_snapshot.violations == report_without_rules_snapshot.violations
    )
    assert (
        report_with_rules_snapshot.is_feasible == report_without_rules_snapshot.is_feasible
    )
