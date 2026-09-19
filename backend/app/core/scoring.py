"""`Objective_Scorer`：软目标分量、权重与总分的纯计算（任务 2.10，R7.1–R7.2 / R7.4–R7.5）。

design.md Components §3.3 把这个组件定成一句话：**确定性**、7 个软目标分量、权重、总分、
`preference_penalty` 逐条归因。本模块承担 §3.3 那段的全部纯计算部分——它站在任务 2.1 的
冻结快照（`DomainSnapshot`）与任务 2.4 的排产结果（`PlanCandidate` / `ScheduledJob`）之上，
产出值对象 `ObjectiveBreakdown`。不碰 ORM、不碰 I/O、不读时钟、**不调用 LLM**（R7.5）。

## `score()` 恰好输出 7 条 `ComponentScore`（R7.1–R7.2）

R7.1 逐字列出 7 个分量：`late_order_count`、`total_tardiness_minutes`、
`urgent_order_lateness`、`churn_ratio`、`machine_utilisation`、`total_changeover_minutes`、
`preference_penalty`。R7.2 要求每个分量都带 `raw_value` / `weight` / `weighted_contribution`，
并输出总分。因此 `score()` 的返回恒含 7 条 `ComponentScore`，缺一不可——`_COMPONENT_ORDER`
把这个「恰好 7 条且顺序固定」钉成一个常量元组，测试直接断言它。

`total_score = Σ weighted_contribution`，其中 `weighted_contribution = weight × raw_value`
（design.md §3.3）。**越小越好**：迟交、换型都是正权重的成本；`machine_utilisation` 例外，
它的权重是负的（−50.0，design.md §3.3 的注释「利用率越高越好」），于是利用率越高、总分
越低。这条方向性是 §2837 EVAL 覆盖清单点名要测的「负权重的方向正确性」。

## `churn_ratio` 只在传入 `reference_plan` 时有值（design.md §3.3 / §3.5）

不传参照计划（初始生成）时 `churn_ratio` 的 `raw_value = 0.0`，因此它对总分零贡献——一份
凭空生成的计划没有「相对谁的扰动」可言。传入参照计划时，按 design.md §3.5 的定义计算：

    churn_ratio = (added + removed + moved + reassigned) / (a ∪ b 的作业数)

分母取**并集**而非 `|ACTIVE|`：这样加急插单带来的新增作业不会让比率越过 1（design.md §3.5
明确了这一点，否则 K-05 的 ≤0.20 目标失去意义）。每个共有作业只归入 `moved` / `reassigned`
之一（`reassigned` 优先），避免重复计数。`compute_plan_delta`（任务 7.1）尚未落地，因此这
一份 churn 计算在本模块内自足实现；两处将来若都存在，语义须一致（同一条 §3.5 公式）。

## `machine_utilisation` = 已排产机时 ÷ 可用机时（design.md §3.3）

分子是全部 `ScheduledJob` 的占用分钟之和（`[start_time, end_time)` 含换型，与时间线口径
一致）；分母是快照里每台机器 `[available_start, available_end)` 的分钟之和。无可用机时
（分母为 0）时利用率取 0.0，避免除零。这个比率落在 `[0, 1]` 附近（多机器并行时分子可能
逼近分母），负权重让它把总分往下拉。

## `preference_penalty` 已接入（任务 11.2，design.md §3.3 / §4.3）

`preference_penalty` 分量的 `raw_value` 由 `core.preference.preference_penalty(plan, rules,
snapshot)` 算出——成型计划上每条命中规则的 `命中数 × weight_delta × PREF_UNIT` 之和（分钟
等价），逐 `rule_id` 的归因写进 `ObjectiveBreakdown.preference_contributions`（R18.7）。
`ADJUST_OBJECTIVE_WEIGHT` 不进 penalty，而是以有界 `multiplier` 缩放 6 个软目标分量的权重
（`apply_weight_overrides`），生效覆盖记入 `weight_overrides_applied`。空规则集或无命中时该
分量原始值为 0.0、归因为空——评分逐字段等于偏好接入前（属性 10b）。**偏好只影响软评分与
候选排序，绝不改变可行性**：`score()` 不判硬约束，`validate()` 在排产后无条件执行且签名不含
偏好规则（EVAL-206）。

## 权重变更写 `Audit_Log`（R7.4）——审计的接缝在服务/API 层，不在本模块

R7.4：规划员改权重时，下一次排产用新权重，并在 `Audit_Log` 记 `WEIGHT_CHANGE`。这里的
**纯计算**只消费 `ObjectiveWeights`——它不知道权重「变没变」，也不该知道：审计要写库，而
内核不能 import `sqlalchemy` / `app.db`（`tests/structure/test_layering.py` 第 ① 条静态断言）。

因此 `WEIGHT_CHANGE` 的写入是一道**跨层接缝**，落在权重真正被修改的地方——服务/API 层
（`app/db/audit_events.WEIGHT_CHANGE` 常量已就位）。本模块提供 `diff_weights()` 这个纯函数
帮那一侧算出「哪几个分量的权重从多少变到多少」，服务层拿着 diff 结果去写审计。纯计算与审计
写入就此分离：`score()` 可被逐字节断言（同权重同计划必得同分），审计的副作用不污染它。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict

from app.core.preference import (
    PreferenceContribution,
    apply_weight_overrides,
    preference_penalty,
)
from app.core.scheduler import PlanCandidate
from app.core.snapshot import DomainSnapshot, Order

# --------------------------------------------------------------------------
# 权重（design.md §3.3 的默认值）
# --------------------------------------------------------------------------


class ObjectiveWeights(BaseModel):
    """7 个软目标的权重，默认值逐字取自 design.md §3.3。

    `machine_utilisation` 是**负权重**（−50.0）：利用率越高越好，因此它把总分往下拉。
    其余全为正——迟交、换型、偏好违反都是成本。`ADJUST_OBJECTIVE_WEIGHT`（任务 11.x）以
    `multiplier ∈ [0.5, 2.0]` 有界地缩放某个分量，不能把任一目标归零。

    `frozen=True`：权重是一次评分的输入常量。改权重的正确方式是构造一份新的
    `ObjectiveWeights`（并在服务层写 `WEIGHT_CHANGE` 审计），而不是就地改一个字段——
    就地改会让「这次评分用的到底是哪套权重」变得不可复现。
    """

    model_config = ConfigDict(frozen=True)

    late_order_count: float = 100.0
    total_tardiness_minutes: float = 1.0
    urgent_order_lateness: float = 300.0
    churn_ratio: float = 500.0
    machine_utilisation: float = -50.0  # 负权重：利用率越高越好（design.md §3.3）
    total_changeover_minutes: float = 0.5
    preference_penalty: float = 1.0


# --------------------------------------------------------------------------
# 值对象（design.md §3.3）
# --------------------------------------------------------------------------


class ComponentScore(BaseModel):
    """一个软目标分量的三元组：原始值、权重、加权贡献（R7.2）。

    `weighted_contribution = weight × raw_value`。UI（R7.3）逐条展示这三个数，让规划员看到
    「系统在优化什么、各项权重多少、这一项贡献了多少」。`frozen=True`：它是 `score()` 的
    产出值对象，不可变。
    """

    model_config = ConfigDict(frozen=True)

    name: str
    raw_value: float
    weight: float
    weighted_contribution: float


class ObjectiveBreakdown(BaseModel):
    """一次评分的完整结果（design.md §3.3、R7.1–R7.2）。

    `components` 恒含**恰好 7 条**（R7.1），顺序为 `_COMPONENT_ORDER`。`total_score` 是全部
    `weighted_contribution` 之和（越小越好）。`preference_contributions` 逐 `rule_id`
    归因（R7.6）——本任务先留空列表，任务 11.2 接入。

    `weight_overrides_applied` 记录 `ADJUST_OBJECTIVE_WEIGHT` 实际生效的覆盖（design.md
    §3.3 / §4.3）。每条含 `rule_id` / `component` / `multiplier` / `original_weight` /
    `new_weight`，供审计与 UI 展示；无 `ADJUST_OBJECTIVE_WEIGHT` 规则时为空元组。
    """

    model_config = ConfigDict(frozen=True)

    components: tuple[ComponentScore, ...]
    total_score: float
    #: 逐 `rule_id` 的偏好惩罚归因（R18.7、design.md §4.3）。无启用偏好或无命中时为空元组，
    #: 因此此刻的评分逐字段等于偏好接入前（属性 10b）。
    preference_contributions: tuple[PreferenceContribution, ...] = ()
    #: 生效的 `ADJUST_OBJECTIVE_WEIGHT` 覆盖（design.md §4.3）。元素是 JSON 可序列化的 dict。
    weight_overrides_applied: tuple[dict[str, Any], ...] = ()


# --------------------------------------------------------------------------
# 分量顺序（「恰好 7 条」的载体，R7.1）
# --------------------------------------------------------------------------

#: R7.1 逐字列出的 7 个分量，顺序固定。`score()` 恒按此序产出 `ComponentScore`，
#: `test_scoring_core.py` 直接断言分量名列表等于 `list(_COMPONENT_ORDER)`。顺序固定让
#: UI 展示、审计快照、回归断言三处对「第几项是什么」有一致预期。
_COMPONENT_ORDER: tuple[str, ...] = (
    "late_order_count",
    "total_tardiness_minutes",
    "urgent_order_lateness",
    "churn_ratio",
    "machine_utilisation",
    "total_changeover_minutes",
    "preference_penalty",
)


# --------------------------------------------------------------------------
# 原始值计算（各分量的纯函数）
# --------------------------------------------------------------------------


def _order_completion_times(plan: PlanCandidate) -> dict[str, datetime]:
    """每个**有已排产作业**的订单的完工时刻 = 其全部 `ScheduledJob` 中最晚的 `end_time`。

    一个订单可能有 1–3 道工序，交期以整单完工为准，因此取最晚 `end_time`。未排产的订单不在
    此映射里——它进 `unschedulable_jobs`，其迟交不由 `Objective_Scorer` 计（它压根没被排上，
    R8 的不可排产清单才是它的去处）。
    """
    latest: dict[str, datetime] = {}
    for job in plan.scheduled_jobs:
        current = latest.get(job.order_id)
        if current is None or job.end_time > current:
            latest[job.order_id] = job.end_time
    return latest


def _tardiness_minutes(order: Order, completion: datetime) -> int:
    """订单的迟交分钟数 = `max(0, 完工时刻 − due_date)` 向下取整到分钟。

    未迟交（完工 ≤ 交期）返回 0。用整分钟：与 design.md 的 `total_tardiness_minutes: int`
    口径一致（§645 的 `ObjectiveSummary`），也让加权和不引入浮点尾差。
    """
    if completion <= order.due_date:
        return 0
    delta = completion - order.due_date
    return int(delta.total_seconds() // 60)


def _late_and_tardiness(
    plan: PlanCandidate, snapshot: DomainSnapshot
) -> tuple[int, int, int]:
    """`(late_order_count, total_tardiness_minutes, urgent_order_lateness)`。

    对每个有已排产作业的订单算迟交分钟：
    - `late_order_count` —— 迟交分钟 > 0 的订单数；
    - `total_tardiness_minutes` —— 全部订单迟交分钟之和；
    - `urgent_order_lateness` —— **仅** `priority == "URGENT"` 订单的迟交分钟之和
      （加急订单的迟交权重最高，design.md §3.3 给它 300.0）。

    只遍历排产结果里出现的订单：未排产订单的「迟交」没有意义（它没有完工时刻），归 R8 的
    不可排产清单处理，不在软目标里重复记账。
    """
    completions = _order_completion_times(plan)
    orders_by_id = snapshot.orders_by_id()

    late_count = 0
    total_tardiness = 0
    urgent_lateness = 0
    for order_id, completion in completions.items():
        order = orders_by_id.get(order_id)
        if order is None:
            continue
        tardiness = _tardiness_minutes(order, completion)
        if tardiness > 0:
            late_count += 1
        total_tardiness += tardiness
        if order.priority == "URGENT":
            urgent_lateness += tardiness
    return late_count, total_tardiness, urgent_lateness


def _machine_utilisation(plan: PlanCandidate, snapshot: DomainSnapshot) -> float:
    """已排产机时 ÷ 可用机时（design.md §3.3）。

    - 分子 = Σ `[start_time, end_time)` 分钟（全部 `ScheduledJob`，含换型，与时间线口径一致）；
    - 分母 = Σ 每台机器 `[available_start, available_end)` 分钟。

    分母为 0（快照无机器或全部机器可用窗口为空）→ 返回 0.0，避免除零。返回浮点：利用率是
    一个比率，不是分钟计数，浮点是它的自然表示；负权重把这个 `[0, ~1]` 的比率往总分下方拉。
    """
    scheduled_minutes = sum(
        _minutes_between(job.start_time, job.end_time) for job in plan.scheduled_jobs
    )
    available_minutes = sum(
        _minutes_between(machine.available_start, machine.available_end)
        for machine in snapshot.machines
    )
    if available_minutes <= 0:
        return 0.0
    return scheduled_minutes / available_minutes


def _total_changeover_minutes(plan: PlanCandidate) -> int:
    """全部已排产作业的换型分钟之和（design.md §3.3）。

    `ScheduledJob.changeover_minutes` 是单列出来的换型部分（不含 `setup_time`），正是这个
    分量要累加的量。`setup_minutes = setup_time + changeover`，但换型分量只记后者——换型是
    可优化的（换个排序能省），固定的准备时间不是。
    """
    return sum(job.changeover_minutes for job in plan.scheduled_jobs)


def _churn_ratio(plan: PlanCandidate, reference_plan: PlanCandidate | None) -> float:
    """相对 `reference_plan` 的作业扰动比率（design.md §3.5）。

    不传参照（初始生成）→ 0.0：没有「相对谁」的扰动。传入时按 §3.5：

        churn_ratio = (added + removed + moved + reassigned) / |a ∪ b|

    其中 `a` = 参照计划的作业（按 `job_id` 索引），`b` = 本计划的作业。
    - `added` —— 在 `b` 不在 `a`；
    - `removed` —— 在 `a` 不在 `b`；
    - 共有作业里：`machine_id` / `worker_id` 变了 → `reassigned`（优先）；否则 `start_time`
      变了 → `moved`。每个共有作业只归其一，不重复计数。

    分母取**并集**，保证新增作业不会让比率越过 1（§3.5：否则 K-05 的 ≤0.20 失去意义）。
    并集为空（两份计划都无作业）→ 0.0。
    """
    if reference_plan is None:
        return 0.0

    a = {job.job_id: job for job in reference_plan.scheduled_jobs}
    b = {job.job_id: job for job in plan.scheduled_jobs}

    added = b.keys() - a.keys()
    removed = a.keys() - b.keys()
    common = a.keys() & b.keys()

    reassigned = 0
    moved = 0
    for job_id in common:
        old = a[job_id]
        new = b[job_id]
        if old.machine_id != new.machine_id or old.worker_id != new.worker_id:
            reassigned += 1
        elif old.start_time != new.start_time:
            moved += 1

    union = a.keys() | b.keys()
    if not union:
        return 0.0
    return (len(added) + len(removed) + moved + reassigned) / len(union)


def _minutes_between(start: datetime, end: datetime) -> int:
    """`[start, end)` 的整分钟长度，负值截断为 0（与 scheduler 的同名辅助口径一致）。"""
    if end <= start:
        return 0
    return int((end - start).total_seconds() // 60)


# --------------------------------------------------------------------------
# 评分（纯函数，design.md §3.3）
# --------------------------------------------------------------------------


def score(
    plan: PlanCandidate,
    snapshot: DomainSnapshot,
    weights: ObjectiveWeights,
    *,
    reference_plan: PlanCandidate | None = None,
) -> ObjectiveBreakdown:
    """把一份 `PlanCandidate` 评成 `ObjectiveBreakdown`（design.md §3.3、R7.1–R7.2 / R7.5）。

    **纯确定性**：输出只由 `(plan, snapshot, weights, reference_plan)` 决定，不读时钟、不碰
    I/O、不调用 LLM（R7.5）。同样的四元入参两次运行必得逐字段相同的结果。

    产出**恰好 7 条** `ComponentScore`（R7.1），顺序为 `_COMPONENT_ORDER`；每条带
    `raw_value` / `weight` / `weighted_contribution`，`total_score = Σ weighted_contribution`
    （越小越好，R7.2）。`machine_utilisation` 的负权重让利用率越高、总分越低。

    **偏好接入（任务 11.2）**：`preference_penalty` 分量的 `raw_value` 是 `core.preference`
    对成型计划算出的分钟等价总惩罚（`Σ 命中数 × weight_delta × PREF_UNIT`），`weight` 为
    `weights.preference_penalty`（默认 1.0）；逐 `rule_id` 归因写入 `preference_contributions`
    （R18.7）。`ADJUST_OBJECTIVE_WEIGHT` 不进 penalty，而是以有界 `multiplier` 缩放 6 个软目标
    分量的权重（`apply_weight_overrides`），生效覆盖记入 `weight_overrides_applied`。

    **偏好绝不改变可行性**：本函数只算软评分，规则只影响权重与惩罚。空规则集或无命中时
    `preference_penalty.raw_value == 0.0`、`preference_contributions == ()`、
    `weight_overrides_applied == ()`，因此评分逐字段等于偏好接入前（属性 10b）。
    """
    rules = snapshot.preference_rules

    late_count, total_tardiness, urgent_lateness = _late_and_tardiness(plan, snapshot)
    utilisation = _machine_utilisation(plan, snapshot)
    changeover = _total_changeover_minutes(plan)
    churn = _churn_ratio(plan, reference_plan)
    penalty = preference_penalty(plan, rules, snapshot)

    #: 分量名 → 原始值。`preference_penalty` 的原始值是分钟等价总惩罚（含 PREF_UNIT）。
    raw_values: dict[str, float] = {
        "late_order_count": float(late_count),
        "total_tardiness_minutes": float(total_tardiness),
        "urgent_order_lateness": float(urgent_lateness),
        "churn_ratio": churn,
        "machine_utilisation": utilisation,
        "total_changeover_minutes": float(changeover),
        "preference_penalty": penalty.total,
    }

    base_weights: dict[str, float] = {
        "late_order_count": weights.late_order_count,
        "total_tardiness_minutes": weights.total_tardiness_minutes,
        "urgent_order_lateness": weights.urgent_order_lateness,
        "churn_ratio": weights.churn_ratio,
        "machine_utilisation": weights.machine_utilisation,
        "total_changeover_minutes": weights.total_changeover_minutes,
        "preference_penalty": weights.preference_penalty,
    }

    # ADJUST_OBJECTIVE_WEIGHT：有界缩放 6 个软目标分量的权重（design.md §4.3）。
    # `preference_penalty` 分量本身不是 `ADJUST_OBJECTIVE_WEIGHT` 的合法 `component`
    # （SoftWeightKey 只有 6 个软目标），因此其权重不受覆盖影响。
    weight_of, overrides = apply_weight_overrides(base_weights, rules)

    components: list[ComponentScore] = []
    total = 0.0
    for name in _COMPONENT_ORDER:
        raw = raw_values[name]
        weight = weight_of[name]
        contribution = weight * raw
        total += contribution
        components.append(
            ComponentScore(
                name=name,
                raw_value=raw,
                weight=weight,
                weighted_contribution=contribution,
            )
        )

    return ObjectiveBreakdown(
        components=tuple(components),
        total_score=total,
        preference_contributions=penalty.contributions,
        weight_overrides_applied=overrides,
    )


# --------------------------------------------------------------------------
# 权重变更 diff（R7.4 的跨层接缝）
# --------------------------------------------------------------------------


class WeightChange(BaseModel):
    """一个分量的权重变化（`old_weight → new_weight`），供服务层写 `WEIGHT_CHANGE` 审计。"""

    model_config = ConfigDict(frozen=True)

    component: str
    old_weight: float
    new_weight: float


def diff_weights(
    old: ObjectiveWeights, new: ObjectiveWeights
) -> tuple[WeightChange, ...]:
    """两套权重之间**变化了的**分量（R7.4 审计的纯计算部分）。

    这是一个纯函数：它只算「哪几项变了、从多少到多少」，**不写库**——内核不能 import
    `sqlalchemy` / `app.db`（`tests/structure/test_layering.py` 第 ① 条）。真正把
    `WEIGHT_CHANGE` 写进 `Audit_Log` 的动作在服务/API 层完成（那里能拿到会话，且
    `app/db/audit_events.WEIGHT_CHANGE` 常量已就位）：服务层拿本函数返回的 diff 作为审计
    载荷。分离的意义是 `score()` 与 `diff_weights()` 都保持可逐字节断言，审计副作用不渗进
    纯计算。

    返回按 `_COMPONENT_ORDER` 排序，无变化时为空元组。以整数比较避免浮点相等的脆弱：
    权重是配置量，`multiplier` 缩放后仍是有限精度的十进制，逐字段直接比较即可。
    """
    changes: list[WeightChange] = []
    for name in _COMPONENT_ORDER:
        old_weight = getattr(old, name)
        new_weight = getattr(new, name)
        if old_weight != new_weight:
            changes.append(
                WeightChange(
                    component=name,
                    old_weight=old_weight,
                    new_weight=new_weight,
                )
            )
    return tuple(changes)
