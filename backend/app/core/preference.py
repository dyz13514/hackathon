"""偏好规则如何影响软评分与候选排序的**确定性纯计算**（任务 11.2，R7.6 / R18.7 / R18.9）。

design.md §4.3「规则如何变成 `preference_penalty` 的一项」把这段逻辑定成一个纯函数
`preference_penalty(plan, rules)`，并要求它在两处被使用：

1. **排产时**（`Scheduling_Core._best_candidate` 的候选打分）——`preference_delta(job, machine,
   worker, rules)` 算「把这道工序放在这台机器/这个工人上会新增多少偏好惩罚」，单位是分钟等价，
   直接加进 `cost`，因此规则**改变排产结果**（EVAL-011 第一断言）。
2. **评分时**（`Objective_Scorer.score`）——`preference_penalty(plan, rules, snapshot)` 对成型
   计划算总惩罚并输出逐 `rule_id` 的 `contributions`，UI 据此标注「JOB-012 受 PR-003 影响」
   （R18.7、EVAL-011 第二断言）。

两处共用本模块的**同一套匹配谓词**（`_order_hit` / `_product_hit` / `_skill_hit`），这是「规则
真的生效且可解释」的关键：排产时用它加惩罚、评分时用它算贡献，两处对「哪条规则命中哪个作业」
的判断永远一致。

## 安全边界（R18.8、EVAL-206）——本模块只影响软评分与排序，绝不碰可行性

- 偏好只产生**非负**的惩罚（`weight_delta > 0`），只能让某个选择**更不划算**，永远不能让一个
  不可行的选择变可行。`preference_delta` 恒 `>= 0`。
- 本模块**不导出**、也不接受任何可行性判定入口：`Constraint_Validator.validate` 与
  `Scheduling_Core.is_feasible_slot` 的签名里没有 `preference_rules`，本模块也不改变它们。规则
  只出现在候选**排序**的 `cost` 与成型计划的**软评分**里。
- `ADJUST_OBJECTIVE_WEIGHT` 不进 penalty，它以 `multiplier ∈ [0.5, 2.0]` 有界地缩放某个**软目标**
  分量的权重（`apply_weight_overrides`），既不能把目标归零也不能放大到无穷，更不触碰硬约束。

## 确定性

`structured_form` 从快照的原始 dict 解析；规则按传入顺序处理（快照按 `rule_id` 升序加载，见
`services/snapshot_loader`），命中作业在成型计划里按 `job_id` 升序取。因此同输入同输出，逐字段
可断言（属性 10b、Property 37 的同类要求）。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:  # 仅类型引用，避免与 scheduler / scoring 的运行期循环 import
    from app.core.scheduler import PlanCandidate, ProductionJob, ScheduledJob
    from app.core.snapshot import DomainSnapshot, Machine, PreferenceRule, Worker

__all__ = [
    "PREF_UNIT",
    "PreferenceContribution",
    "PenaltyResult",
    "apply_weight_overrides",
    "preference_delta",
    "preference_penalty",
]

#: 分钟等价基数（design.md §4.3、tasks.md 11.2）：违反 1 条规则 ≈ 晚完工 60 分钟的代价。
#: 让偏好惩罚与「晚完工」同量纲，规划员能理解「这条规则值多少分钟」。用 `Decimal` 与内核算术
#: 一致（design.md §3.1.4 禁止浮点进入排产算术）。
PREF_UNIT: Decimal = Decimal("60.0")

#: 4 类判别键（design.md §4.3）。`AVOID_MACHINE_FOR_ORDER` / `AVOID_MACHINE_FOR_PRODUCT` /
#: `PREFER_WORKER_FOR_SKILL` 进 penalty；`ADJUST_OBJECTIVE_WEIGHT` 走 weight override。
_PENALTY_KINDS = (
    "AVOID_MACHINE_FOR_ORDER",
    "AVOID_MACHINE_FOR_PRODUCT",
    "PREFER_WORKER_FOR_SKILL",
)

#: `ADJUST_OBJECTIVE_WEIGHT.component` 允许的 6 个软目标分量（与 `ObjectiveWeights` 的可覆盖
#: 字段、`tools.models.SoftWeightKey` 逐字一致）。指向此集合之外的字符串在偏好规则创建时就已
#: 被 Pydantic 判别联合拒绝（`PREFERENCE_RULE_OUT_OF_SCOPE`），这里再作一道防御性白名单：即便
#: 某条越界规则绕过创建闸门进了快照，评分也不会去缩放一个非软目标（尤其不会碰任何硬约束开关）。
_SOFT_WEIGHT_KEYS = frozenset(
    {
        "late_order_count",
        "total_tardiness_minutes",
        "urgent_order_lateness",
        "churn_ratio",
        "machine_utilisation",
        "total_changeover_minutes",
    }
)


class PreferenceContribution(BaseModel):
    """一条偏好规则对成型计划的惩罚贡献（design.md §4.3、R18.7）。

    `raw_value = len(violating_job_ids_all) × weight_delta`（命中数 × 规则权重）；
    `weighted_contribution = raw_value × PREF_UNIT`（分钟等价）。`violating_job_ids` 至多 10 条
    （UI 展示与 token 预算，design.md §4.3），但 `raw_value` 按**全部**命中数计，不被截断影响。
    `frozen`：它是评分产出的值对象。
    """

    model_config = ConfigDict(frozen=True)

    rule_id: str
    human_text: str
    kind: str
    violating_job_ids: tuple[str, ...]
    raw_value: float
    weighted_contribution: float


class PenaltyResult(BaseModel):
    """`preference_penalty` 的结果：分钟等价总惩罚 + 逐 `rule_id` 贡献（design.md §4.3）。"""

    model_config = ConfigDict(frozen=True)

    total: float
    contributions: tuple[PreferenceContribution, ...]


# --------------------------------------------------------------------------
# 单条规则的匹配谓词（排产与评分共用，保证两处口径一致）
# --------------------------------------------------------------------------


def _weight_delta(form: Mapping[str, Any]) -> Decimal:
    """规则的 `weight_delta`，缺省 1.0。恒 `> 0`（创建时由 `Field(gt=0, le=10)` 保证）。

    转成 `Decimal` 且下限截到 0：即便一条越界规则绕过创建闸门带来非正权重，惩罚也不会变成
    「奖励」（负惩罚会让不划算的选择反而更优，破坏 R18.8 的方向性）。
    """
    raw = form.get("weight_delta", 1.0)
    try:
        value = Decimal(str(raw))
    except (ArithmeticError, ValueError, TypeError):
        return Decimal("1.0")
    return value if value > 0 else Decimal("0")


def _order_hit(form: Mapping[str, Any], *, order_id: str, machine_id: str) -> bool:
    """AVOID_MACHINE_FOR_ORDER：该订单的作业排在被避开的机器上即命中。"""
    return form.get("order_id") == order_id and form.get("machine_id") == machine_id


def _product_hit(form: Mapping[str, Any], *, product_id: str, machine_id: str) -> bool:
    """AVOID_MACHINE_FOR_PRODUCT：该产品的作业排在被避开的机器上即命中。"""
    return form.get("product_id") == product_id and form.get("machine_id") == machine_id


def _skill_hit(
    form: Mapping[str, Any], *, required_worker_skill: str, worker_id: str
) -> bool:
    """PREFER_WORKER_FOR_SKILL：需要该技能但**未**用被偏好工人的作业即命中（design.md §4.3）。

    「偏好某工人做某技能」表达为「用了别的工人来做这技能就罚」，因此命中条件是技能匹配且
    `worker_id != 偏好工人`。用被偏好工人本人做则不罚。
    """
    return form.get("skill") == required_worker_skill and form.get("worker_id") != worker_id


# --------------------------------------------------------------------------
# 排产时：单个候选放置的新增惩罚（design.md §3.1.2 的 W_PREF 项）
# --------------------------------------------------------------------------


def preference_delta(
    job: ProductionJob,
    machine: Machine,
    worker: Worker,
    rules: Sequence[PreferenceRule],
) -> Decimal:
    """把 `job` 放在 `machine` / `worker` 上会新增多少偏好惩罚（分钟等价，design.md §3.1.2）。

    对每条 penalty 类规则判断这个放置是否命中，命中则累加 `weight_delta × PREF_UNIT`。返回值
    **恒 `>= 0`**：偏好只能让一个候选更不划算，不能让它更划算，因此它只改变**选谁**，绝不把一个
    不可行槽位变成可行（可行性早在 `earliest_feasible_slot` / `is_feasible_slot` 判定，与本函数
    无关）。`ADJUST_OBJECTIVE_WEIGHT` 不产生候选级惩罚（它改的是成型计划的目标权重）。

    与 `preference_penalty` 共用匹配谓词，因此「排产时按此放置会不会被罚」与「评分时这条规则
    是否把该作业记为受影响」永远一致。
    """
    total = Decimal("0")
    for rule in rules:
        form = _form_of(rule)
        kind = form.get("kind")
        if kind == "AVOID_MACHINE_FOR_ORDER":
            hit = _order_hit(form, order_id=job.order_id, machine_id=machine.machine_id)
        elif kind == "AVOID_MACHINE_FOR_PRODUCT":
            hit = _product_hit(form, product_id=job.product_id, machine_id=machine.machine_id)
        elif kind == "PREFER_WORKER_FOR_SKILL":
            hit = _skill_hit(
                form,
                required_worker_skill=job.required_worker_skill,
                worker_id=worker.worker_id,
            )
        else:  # ADJUST_OBJECTIVE_WEIGHT 或未知 kind：候选级不罚
            continue
        if hit:
            total += _weight_delta(form) * PREF_UNIT
    return total


# --------------------------------------------------------------------------
# 评分时：成型计划的总惩罚与逐规则归因（design.md §4.3、R18.7）
# --------------------------------------------------------------------------


def preference_penalty(
    plan: PlanCandidate,
    rules: Sequence[PreferenceRule],
    snapshot: DomainSnapshot,
) -> PenaltyResult:
    """对成型 `plan` 算总偏好惩罚并逐 `rule_id` 归因（design.md §4.3、R18.7、EVAL-011 第二断言）。

    对每条 penalty 类规则，扫全部 `scheduled_jobs` 收集命中作业（按 `job_id` 升序，确定性），
    `raw_value = len(hits) × weight_delta`，`weighted_contribution = raw_value × PREF_UNIT`。
    `violating_job_ids` 只保留前 10 条（design.md §4.3）。`total = Σ weighted_contribution`。

    `PREFER_WORKER_FOR_SKILL` 需要每个已排产作业的 `required_worker_skill`，而 `ScheduledJob`
    不带这个字段（它只记「谁在哪台机器什么时候」）——因此从 `snapshot` 按 `job_id` 回查工序技能
    （与 `Constraint_Validator` 的回查口径一致）。`ADJUST_OBJECTIVE_WEIGHT` 不进 penalty。

    空规则集或全部规则未命中 → `total=0.0`、`contributions=()`，因此**无启用偏好时评分逐字段等于
    接入前**（属性 10b、`preference_penalty` 分量原始值为 0）。
    """
    skill_by_job = _skill_by_job(plan, snapshot)
    contributions: list[PreferenceContribution] = []

    for rule in rules:
        form = _form_of(rule)
        kind = form.get("kind")
        if kind not in _PENALTY_KINDS:
            continue

        hits: list[str] = []
        for sj in _jobs_sorted(plan):
            if kind == "AVOID_MACHINE_FOR_ORDER":
                matched = _order_hit(form, order_id=sj.order_id, machine_id=sj.machine_id)
            elif kind == "AVOID_MACHINE_FOR_PRODUCT":
                matched = _product_hit(
                    form, product_id=sj.product_id, machine_id=sj.machine_id
                )
            else:  # PREFER_WORKER_FOR_SKILL
                matched = _skill_hit(
                    form,
                    required_worker_skill=skill_by_job.get(sj.job_id, ""),
                    worker_id=sj.worker_id,
                )
            if matched:
                hits.append(sj.job_id)

        if not hits:
            continue

        weight = _weight_delta(form)
        raw = Decimal(len(hits)) * weight
        weighted = raw * PREF_UNIT
        contributions.append(
            PreferenceContribution(
                rule_id=rule.rule_id,
                human_text=rule.human_text,
                kind=str(kind),
                violating_job_ids=tuple(hits[:10]),
                raw_value=float(raw),
                weighted_contribution=float(weighted),
            )
        )

    total = float(sum((Decimal(str(c.weighted_contribution)) for c in contributions), Decimal("0")))
    return PenaltyResult(total=total, contributions=tuple(contributions))


# --------------------------------------------------------------------------
# ADJUST_OBJECTIVE_WEIGHT：有界缩放软目标权重（design.md §4.3、R18.7）
# --------------------------------------------------------------------------


def apply_weight_overrides(
    weights: Mapping[str, float],
    rules: Sequence[PreferenceRule],
) -> tuple[dict[str, float], tuple[dict[str, Any], ...]]:
    """把 `ADJUST_OBJECTIVE_WEIGHT` 规则的 `multiplier` 有界地作用到软目标权重上。

    返回 `(覆盖后的权重 dict, 实际生效的覆盖列表)`。覆盖列表逐条含 `rule_id` / `component` /
    `multiplier` / `original_weight` / `new_weight`，供审计与 `weight_overrides_applied` 展示。

    - `component` 必须在 `_SOFT_WEIGHT_KEYS` 内（6 个软目标），否则跳过——绝不缩放非软目标，
      更不触碰任何硬约束（R18.8、EVAL-206 的防御性一道）。
    - `multiplier` 由创建闸门约束在 `[0.5, 2.0]`；多条规则作用同一分量时按传入顺序**依次相乘**
      （确定性）。此处不再夹取——有界性在创建时保证，评分只忠实复算。

    不改传入的 `weights`（返回新 dict），保持 `score()` 的纯粹性。
    """
    result = dict(weights)
    applied: list[dict[str, Any]] = []
    for rule in rules:
        form = _form_of(rule)
        if form.get("kind") != "ADJUST_OBJECTIVE_WEIGHT":
            continue
        component = form.get("component")
        if component not in _SOFT_WEIGHT_KEYS or component not in result:
            continue
        try:
            multiplier = float(form.get("multiplier", 1.0))
        except (TypeError, ValueError):
            continue
        original = result[component]
        new_weight = original * multiplier
        result[component] = new_weight
        applied.append(
            {
                "rule_id": rule.rule_id,
                "component": component,
                "multiplier": multiplier,
                "original_weight": original,
                "new_weight": new_weight,
            }
        )
    return result, tuple(applied)


# --------------------------------------------------------------------------
# 辅助
# --------------------------------------------------------------------------


def _form_of(rule: PreferenceRule) -> Mapping[str, Any]:
    """规则的 `structured_form`，非 dict 时退化为空 dict（防御：绝不因坏数据抛错影响排产）。"""
    form = getattr(rule, "structured_form", None)
    return form if isinstance(form, Mapping) else {}


def _jobs_sorted(plan: PlanCandidate) -> tuple[ScheduledJob, ...]:
    """成型计划的已排产作业按 `job_id` 升序（确定性归因顺序）。"""
    return tuple(sorted(plan.scheduled_jobs, key=lambda sj: sj.job_id))


def _skill_by_job(plan: PlanCandidate, snapshot: DomainSnapshot) -> dict[str, str]:
    """`job_id -> required_worker_skill`，从快照的订单→产品→工序回查。

    `job_id = "{order_id}-OP{sequence}"`（design.md §3.1.1）。用已排产作业的 `order_id` 找订单、
    找产品，再按工序 `sequence` 取 `required_worker_skill`。回查失败（数据不全）→ 该作业技能取
    空串，`PREFER_WORKER_FOR_SKILL` 因此不命中它（诚实：无法确认技能就不记为受影响）。
    """
    orders_by_id = snapshot.orders_by_id()
    products_by_id = snapshot.products_by_id()
    result: dict[str, str] = {}
    for sj in plan.scheduled_jobs:
        order = orders_by_id.get(sj.order_id)
        if order is None:
            continue
        product = products_by_id.get(order.product_id)
        if product is None:
            continue
        sequence = _sequence_of(sj.job_id)
        for op in product.operations:
            if op.sequence == sequence:
                result[sj.job_id] = op.required_worker_skill
                break
    return result


def _sequence_of(job_id: str) -> int | None:
    """从 `"{order_id}-OP{n}"` 解析工序序号 `n`；解析失败返回 None。"""
    marker = job_id.rfind("-OP")
    if marker < 0:
        return None
    tail = job_id[marker + 3 :]
    return int(tail) if tail.isdigit() else None
