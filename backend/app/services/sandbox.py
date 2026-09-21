"""`Scenario_Sandbox` 服务层：结构化 What-if 推演与场景采纳（任务 8.3，R16.2 / R16.7–9）。

## 分工

内核 `app/core/sandbox.py` 做纯确定性的两件事（变体构造、关键作业选择）；本模块负责有副作用
与编排的那半：

1. `run_sandbox(session, request, ...)` —— 沙箱推演统一入口（四个调用方共用，后两个 P1 才接线）：
   在 `sandbox_guard(scenario_id)` 语境内（第 2 层引擎级 DML 拦截，任务 8.1）读**冻结**快照、
   应用 5 类结构化变更、跑确定性 `generate_schedule` + `validate` + `score`，与当前 `ACTIVE`
   计划对比，返回 `SandboxResult`。**全程无 LLM，无生产数据写入。**
2. `adopt_scenario(session, scenario, ...)` —— 以某次场景生成一个正式 `PENDING_APPROVAL`
   提案（R16.9）。提案仍走审批流程；`origin = 'SCENARIO_ADOPTION'`。

## 为什么在 `sandbox_guard` 语境内跑

`run_sandbox` 只读快照、在内存里排产，本不该发生任何写。但 R16.5 / EVAL-204 要求写尝试被
**检测**到——`sandbox_guard(scenario_id)` 把 `SANDBOX_ACTIVE` 置真，万一某次改动让沙箱路径上
出现了真实 DML，引擎级监听器会拦下、写 `SANDBOX_WRITE_BLOCKED` 审计并终止模拟（任务 8.1）。
这层保护零成本（默认放行全部常规写），进沙箱即启用。

## 场景的暂存（无 scenarios 表）

沙箱结果是**易逝**的推演（30 秒内返回，R16.7），没有独立的 `scenarios` 表。采纳需要
`scenario_id` 定位到那批变更，因此本模块把「场景 → 变更列表」暂存在进程内
（`ScenarioStore`，挂在 `app.state`）。这满足 P0 演示：采纳紧随推演，进程重启后旧场景失效是
可接受的（它们本就是「假如」的临时推算）。持久化留给 P1（若需要跨会话采纳）。
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from enum import StrEnum

from sqlalchemy.orm import Session

from app.core.explain import NoTradeoff, Tradeoff
from app.core.sandbox import ScenarioMutationError, apply_mutations
from app.core.scheduler import PlanCandidate, generate_schedule
from app.core.scoring import ObjectiveBreakdown, ObjectiveWeights, score
from app.core.snapshot import DomainSnapshot, Machine
from app.core.validation import validate
from app.db import models as orm
from app.db.sandbox_guard import sandbox_guard
from app.logging_config import log_event
from app.orchestrator.pipelines.plan_generation import (
    BaselineComparison,
    PlanGenerationResult,
    _created_at,
    _ensure_production_jobs,
    _expand_production_jobs,
    _new_plan_id,
    _plan_kpis,
    save_proposed_plan,
)
from app.services.replanning import (
    load_plan_candidate,
    require_any_active_plan,
)
from app.services.snapshot_loader import load_sandbox_snapshot, load_snapshot

logger = logging.getLogger(__name__)

SCENARIO_ADOPTION_ORIGIN = "SCENARIO_ADOPTION"
PENDING_STATUS = "PENDING_APPROVAL"


class SandboxPurpose(StrEnum):
    """沙箱推演的用途（design.md §3.7 的 `run_sandbox(purpose=...)`）。

    - `WHATIF`：结构化 What-if 推演（任务 8.3）。
    - `BOTTLENECK`：产能洞察——机器可用工时 +20% 时 `total_tardiness_minutes` 的变化（任务 13.5）。
    - `PROMISE_DATE`：可承诺交期报价——加一个假想订单看最早可完工日（任务 13.6）。

    全部走同一套沙箱隔离（`sandbox_guard` + 冻结快照 + 确定性内核），只读生产数据、无 LLM。
    `purpose` 只进日志与审计，用于区分三类沙箱调用；不改变隔离语义。
    """

    WHATIF = "WHATIF"
    BOTTLENECK = "BOTTLENECK"
    PROMISE_DATE = "PROMISE_DATE"


__all__ = [
    "CapacitySandboxResult",
    "PromiseDateResult",
    "SandboxPurpose",
    "ScenarioNotFoundError",
    "ScenarioStore",
    "SandboxResult",
    "adopt_scenario",
    "build_counterfactual",
    "run_capacity_sandbox",
    "run_promise_date_sandbox",
    "run_sandbox",
]


class ScenarioNotFoundError(LookupError):
    """采纳时给出的 `scenario_id` 不在暂存里（已过期或不存在，R16.9）。"""


@dataclass
class ScenarioStore:
    """进程内的场景暂存（scenario_id → 变更列表 + 生产日）。见模块 docstring。

    只存采纳所需的最小信息：变更列表（重放用）与推演时的 `production_date`。结果本身不存
    ——采纳会用当前真实数据重新确定性生成，因此不依赖旧的推演产物。
    """

    _entries: dict[str, _ScenarioEntry] = field(default_factory=dict)

    def put(self, scenario_id: str, mutations: list[object], production_date: date) -> None:
        self._entries[scenario_id] = _ScenarioEntry(mutations, production_date)

    def get(self, scenario_id: str) -> _ScenarioEntry:
        entry = self._entries.get(scenario_id)
        if entry is None:
            raise ScenarioNotFoundError(scenario_id)
        return entry


@dataclass(frozen=True)
class _ScenarioEntry:
    mutations: list[object]
    production_date: date


@dataclass(frozen=True)
class SandboxResult:
    """一次沙箱推演的结果 + 与当前 `ACTIVE` 计划的对比（R16.7、R16.8）。

    全部数值确定性、无 LLM。`delta_*` 为「场景 − ACTIVE」：正=变差。`new_unschedulable_jobs`
    是场景下新增的不可排产作业（ACTIVE 里能排、场景里不能）。`scenario_id` 供采纳定位。
    """

    scenario_id: str
    feasibility: str
    late_order_count: int
    active_late_order_count: int
    total_tardiness_minutes: int
    active_total_tardiness_minutes: int
    total_score: float
    active_total_score: float
    new_unschedulable_jobs: tuple[str, ...]
    delayed_order_ids: tuple[str, ...]

    @property
    def late_order_count_delta(self) -> int:
        return self.late_order_count - self.active_late_order_count

    @property
    def total_tardiness_delta_minutes(self) -> int:
        return self.total_tardiness_minutes - self.active_total_tardiness_minutes


def run_sandbox(
    session: Session,
    *,
    mutations: list[object],
    now: datetime,
    store: ScenarioStore | None = None,
    weights: ObjectiveWeights | None = None,
) -> SandboxResult:
    """跑一次结构化 What-if 推演（R16.7）。**只读生产数据，无 LLM。** 无 ACTIVE 计划 → 抛。

    步骤（全部确定性）：
    1. 取当前 `ACTIVE` 计划头与其 `PlanCandidate`（对比基准）；
    2. 在 `sandbox_guard(scenario_id)` 内读**冻结**快照，应用变更得情景变体
       （内核 `apply_mutations`）；
    3. 对情景变体跑 `generate_schedule` + `validate` + `score`；
    4. 与 ACTIVE 的 KPI（按期/拖期/总分）对比，算新增 unschedulable 与新延误订单；
    5. 若传了 `store`，把「scenario_id → 变更」暂存供采纳。
    """
    resolved_weights = weights if weights is not None else ObjectiveWeights()
    active_plan = require_any_active_plan(session)  # 无 ACTIVE → NoActivePlanError
    active_candidate = load_plan_candidate(session, active_plan.plan_id)
    scenario_id = f"SCN-{uuid.uuid4().hex[:12]}"

    with sandbox_guard(scenario_id):
        # 第 1 层：冻结、脱离会话的快照；变体经 model_copy 得到新冻结副本（内核，无写）。
        base_snapshot = load_sandbox_snapshot(
            session, now=now, production_date=active_plan.production_date
        )
        scenario_snapshot: DomainSnapshot = apply_mutations(base_snapshot, mutations)

        candidate: PlanCandidate = generate_schedule(scenario_snapshot)
        validate(candidate, scenario_snapshot)  # 全量校验（沙箱结果同样须满足硬约束）
        breakdown: ObjectiveBreakdown = score(
            candidate, scenario_snapshot, resolved_weights, reference_plan=active_candidate
        )

    # ---- 对比（在沙箱语境外算纯值，不涉及 DB） ----
    active_on_time, active_tardiness, active_late = _plan_kpis(
        active_candidate, base_snapshot
    )
    scen_on_time, scen_tardiness, scen_late = _plan_kpis(candidate, scenario_snapshot)

    active_unsched = {u.job_id for u in active_candidate.unschedulable_jobs}
    scen_unsched = {u.job_id for u in candidate.unschedulable_jobs}
    new_unschedulable = tuple(sorted(scen_unsched - active_unsched))

    delayed = _delayed_order_ids(candidate, scenario_snapshot)

    if store is not None:
        store.put(scenario_id, mutations, active_plan.production_date)

    log_event(
        logger,
        "SCENARIO_RUN",
        scenario_id=scenario_id,
        feasibility=candidate.feasibility,
        late_delta=scen_late - active_late,
        tardiness_delta=scen_tardiness - active_tardiness,
        new_unschedulable=len(new_unschedulable),
    )
    return SandboxResult(
        scenario_id=scenario_id,
        feasibility=candidate.feasibility,
        late_order_count=scen_late,
        active_late_order_count=active_late,
        total_tardiness_minutes=scen_tardiness,
        active_total_tardiness_minutes=active_tardiness,
        total_score=breakdown.total_score,
        active_total_score=score(
            active_candidate, base_snapshot, resolved_weights
        ).total_score,
        new_unschedulable_jobs=new_unschedulable,
        delayed_order_ids=delayed,
    )


@dataclass(frozen=True)
class CapacitySandboxResult:
    """机器可用工时 +X% 的沙箱推演结果（任务 13.5，R15.2）。

    `total_tardiness_minutes` 是 +X% 场景下的总拖期；`active_total_tardiness_minutes` 是当前
    `ACTIVE` 计划的总拖期（同口径）；`total_tardiness_delta_minutes` = 前者 − 后者（负=改善）。
    全部由确定性 `generate_schedule` 实算，非静态估算。
    """

    machine_id: str
    hours_multiplier: float
    total_tardiness_minutes: int
    active_total_tardiness_minutes: int
    total_tardiness_delta_minutes: int
    feasibility: str


def run_capacity_sandbox(
    session: Session,
    *,
    machine_id: str,
    hours_multiplier: float,
    now: datetime,
    purpose: SandboxPurpose = SandboxPurpose.BOTTLENECK,
    weights: ObjectiveWeights | None = None,
) -> CapacitySandboxResult:
    """在沙箱里把某台机器的可用工时按 `hours_multiplier` 放大，实算 `total_tardiness_minutes`
    的变化（任务 13.5，R15.2）。**只读生产数据、无 LLM。** 无 ACTIVE 计划 → 抛。

    「+20% 工时」建模为把该机器的可用窗口 `[available_start, available_end)` 的**时长**乘以
    `hours_multiplier`（延后 `available_end`）——`Scheduling_Core` 用 `min(worker.shift_end,
    machine.available_end)` 作硬边界，因此延后 `available_end` 直接给该机器更多排产容量。

    与 `run_sandbox` 同构：在 `sandbox_guard(scenario_id)` 语境内读**冻结**快照、`model_copy`
    出放大窗口的变体、跑确定性 `generate_schedule` + `validate`，与 ACTIVE 的总拖期对比。
    整个计算在沙箱隔离下进行（EVAL-204 的引擎级 DML 拦截同样覆盖本路径），不写任何生产数据。
    """
    resolved_weights = weights if weights is not None else ObjectiveWeights()
    active_plan = require_any_active_plan(session)
    active_candidate = load_plan_candidate(session, active_plan.plan_id)
    scenario_id = f"SCN-{purpose.value}-{uuid.uuid4().hex[:10]}"

    with sandbox_guard(scenario_id):
        base_snapshot = load_sandbox_snapshot(
            session, now=now, production_date=active_plan.production_date
        )
        variant = _with_scaled_machine_hours(base_snapshot, machine_id, hours_multiplier)
        candidate = generate_schedule(variant)
        validate(candidate, variant)

    _, active_tardiness, _ = _plan_kpis(active_candidate, base_snapshot)
    _, scen_tardiness, _ = _plan_kpis(candidate, variant)

    log_event(
        logger,
        "CAPACITY_SANDBOX_RUN",
        purpose=purpose.value,
        machine_id=machine_id,
        hours_multiplier=hours_multiplier,
        tardiness_delta=scen_tardiness - active_tardiness,
    )
    del resolved_weights  # 产能推演只看拖期，不需要评分；保留参数以与 run_sandbox 对齐
    return CapacitySandboxResult(
        machine_id=machine_id,
        hours_multiplier=hours_multiplier,
        total_tardiness_minutes=scen_tardiness,
        active_total_tardiness_minutes=active_tardiness,
        total_tardiness_delta_minutes=scen_tardiness - active_tardiness,
        feasibility=candidate.feasibility,
    )


@dataclass(frozen=True)
class PromiseDateResult:
    """可承诺交期报价的沙箱推演结果（任务 13.6，R17.1–R17.3）。

    - `feasible`：这笔询价能否被排进当前计划（新订单的全部作业都排上了）。
    - `earliest_completion`：新订单最早可承诺完工时刻（其全部作业的最晚 end_time）；不可行为 None。
    - `desired_date_met`：`earliest_completion <= desired_due_date`（可行时才有意义）。
    - `deferred_order_ids`：因插入这笔询价而变得迟交的**既有**订单（相对 ACTIVE 新增的迟交）。
    - `total_tardiness_delta_minutes`：全局总拖期相对 ACTIVE 的变化（正=变差）。
    - `constraint_reason`：不可行 / 无法满足期望交期时的具体约束原因（供报价话术）。
    """

    feasible: bool
    earliest_completion: datetime | None
    desired_due_date: datetime
    desired_date_met: bool
    deferred_order_ids: tuple[str, ...]
    total_tardiness_minutes: int
    active_total_tardiness_minutes: int
    total_tardiness_delta_minutes: int
    constraint_reason: str | None = None


def run_promise_date_sandbox(
    session: Session,
    *,
    product_id: str,
    quantity: float,
    desired_due_date: datetime,
    now: datetime,
    purpose: SandboxPurpose = SandboxPurpose.PROMISE_DATE,
) -> PromiseDateResult:
    """把一笔假想询价（product + quantity + 期望交期）插入沙箱，实算最早可承诺完工日（任务 13.6）。

    **只读生产数据、无 LLM、不改动 ACTIVE 计划或 `input_snapshot_version`（R17.4）。** 无 ACTIVE
    计划 → 抛。

    做法：在 `sandbox_guard` 语境内读冻结快照，`apply_mutations` 追加一个 `ADD_OR_CHANGE_ORDER`
    新订单（`order_id=None` → 内核生成 `SANDBOX-ORD-N`，期望交期作为其 `due_date`），跑确定性
    `generate_schedule`。新订单的全部已排产作业的最晚 `end_time` 即**最早可承诺完工时刻**；若其任一
    作业进了 `unschedulable_jobs`，则该询价不可行，给出约束原因。deferred 与总拖期变化相对 ACTIVE
    计算，供「这笔单会推迟哪些既有订单」的报价话术（R17.2）。
    """
    active_plan = require_any_active_plan(session)
    active_candidate = load_plan_candidate(session, active_plan.plan_id)
    scenario_id = f"SCN-{purpose.value}-{uuid.uuid4().hex[:10]}"

    # 构造「新增订单」变更：用一个轻量对象承载 apply_mutations 需要的 getattr 字段（内核按 kind
    # 结构化分派，不 isinstance 到具体类型，因此这里不必 import 工具契约）。
    quote_mutation = _QuoteOrderMutation(
        product_id=product_id,
        quantity=quantity,
        due_date=desired_due_date.date(),
    )

    with sandbox_guard(scenario_id):
        base_snapshot = load_sandbox_snapshot(
            session, now=now, production_date=active_plan.production_date
        )
        # 询价的产品必须存在——否则排产器展开工序时会 KeyError。提前校验并抛
        # `ScenarioMutationError`（API 翻译成 SCENARIO_INVALID_MUTATION / 422），而不是让一个
        # 指向不存在产品的报价以 500 冒出去。
        if product_id not in base_snapshot.products_by_id():
            raise ScenarioMutationError(f"Product {product_id} does not exist; cannot quote.")
        # 追加订单前记下既有订单数，据此推出内核将分配的新订单 id（SANDBOX-ORD-{n+1}）。
        new_order_id = f"SANDBOX-ORD-{len(base_snapshot.orders) + 1}"
        variant = apply_mutations(base_snapshot, [quote_mutation])
        candidate = generate_schedule(variant)
        validate(candidate, variant)

    # 新订单的作业完工时刻（最晚 end_time）——最早可承诺完工日。
    quote_job_ends = [
        sj.end_time for sj in candidate.scheduled_jobs if sj.order_id == new_order_id
    ]
    quote_unschedulable = [
        uj for uj in candidate.unschedulable_jobs if uj.order_id == new_order_id
    ]
    feasible = bool(quote_job_ends) and not quote_unschedulable
    earliest_completion = max(quote_job_ends) if quote_job_ends else None

    _, active_tardiness, _ = _plan_kpis(active_candidate, base_snapshot)
    _, scen_tardiness, _ = _plan_kpis(candidate, variant)

    # 被推迟的既有订单：场景下迟交、而 ACTIVE 下不迟交的订单（不含这笔新询价本身）。
    active_late = set(_delayed_order_ids(active_candidate, base_snapshot))
    scen_late = set(_delayed_order_ids(candidate, variant))
    deferred = tuple(sorted((scen_late - active_late) - {new_order_id}))

    desired_met = feasible and earliest_completion is not None and (
        earliest_completion <= desired_due_date
    )
    reason: str | None = None
    if not feasible:
        reason = (
            f"This inquiry (product {product_id}, quantity {quantity}) cannot be scheduled under "
            f"current capacity: {len(quote_unschedulable)} of its operations cannot find a "
            f"feasible machine/worker/time slot."
        )
    elif not desired_met and earliest_completion is not None:
        reason = (
            f"The desired due date {desired_due_date.date().isoformat()} cannot be met; "
            f"without violating any hard constraint, the earliest committable completion time is "
            f"{earliest_completion.isoformat()}."
        )

    log_event(
        logger,
        "PROMISE_DATE_SANDBOX_RUN",
        purpose=purpose.value,
        product_id=product_id,
        feasible=feasible,
        desired_met=desired_met,
        deferred_count=len(deferred),
        tardiness_delta=scen_tardiness - active_tardiness,
    )
    return PromiseDateResult(
        feasible=feasible,
        earliest_completion=earliest_completion,
        desired_due_date=desired_due_date,
        desired_date_met=desired_met,
        deferred_order_ids=deferred,
        total_tardiness_minutes=scen_tardiness,
        active_total_tardiness_minutes=active_tardiness,
        total_tardiness_delta_minutes=scen_tardiness - active_tardiness,
        constraint_reason=reason,
    )


@dataclass(frozen=True)
class _QuoteOrderMutation:
    """`apply_mutations` 用的「新增订单」变更载体（kind=ADD_OR_CHANGE_ORDER，order_id=None）。

    内核 `apply_mutations` 按 `kind` 字段结构化分派并用 `getattr` 取字段，因此这里用一个轻量
    frozen dataclass 承载即可，不必 import `app.tools.models`（服务层可 import，但没有必要为
    一次内部构造引入契约依赖）。`order_id=None` 触发内核的「新增订单」分支。
    """

    product_id: str
    quantity: float
    due_date: date
    kind: str = "ADD_OR_CHANGE_ORDER"
    order_id: str | None = None
    priority: str | None = None


def _with_scaled_machine_hours(
    snapshot: DomainSnapshot, machine_id: str, multiplier: float
) -> DomainSnapshot:
    """返回一份把 `machine_id` 的可用窗口时长乘以 `multiplier` 的冻结快照副本（延后 end）。

    指向不存在的机器时原样返回（调用方已从 ACTIVE 计划的作业里取真实 machine_id，不会走到）。
    用 `model_copy(deep=True, update=...)`——原快照不受影响（沙箱第 1 层隔离）。
    """
    machines = list(snapshot.machines)
    changed = False
    new_machines: list[Machine] = []
    for m in machines:
        if m.machine_id == machine_id:
            window = m.available_end - m.available_start
            extra = timedelta(seconds=window.total_seconds() * (multiplier - 1.0))
            new_machines.append(
                m.model_copy(update={"available_end": m.available_end + extra})
            )
            changed = True
        else:
            new_machines.append(m)
    if not changed:
        return snapshot
    return snapshot.model_copy(deep=True, update={"machines": tuple(new_machines)})


def adopt_scenario(
    session: Session,
    *,
    scenario_id: str,
    store: ScenarioStore,
    now: datetime,
    weights: ObjectiveWeights | None = None,
) -> str:
    """以某次场景生成一个正式 `PENDING_APPROVAL` 提案（R16.9）。返回新 plan_id。**自提交。**

    采纳不是「把沙箱推演结果直接变成计划」——沙箱结果 `produced_in_sandbox=True`，永不进
    审批流（`save_proposed_plan` 的护栏，任务 3.3）。采纳的语义是：规划员认为该场景的假设值得
    落地，于是**用当前真实数据 + 该场景的变更**重新确定性生成一个正式提案，`origin =
    'SCENARIO_ADOPTION'`，状态 `PENDING_APPROVAL`——仍须人工审批（R16.9）。

    P0 简化：变更作用在快照层（不落 base 表），因此审批时的重校验看到的是变更后的排产结果
    本身满足硬约束这一事实；把变更真正写进 orders/machines 等 base 表属 P1 的采纳深化。
    """
    resolved_weights = weights if weights is not None else ObjectiveWeights()
    entry = store.get(scenario_id)  # 不存在 → ScenarioNotFoundError

    base_snapshot = load_snapshot(session, now=now, production_date=entry.production_date)
    scenario_snapshot = apply_mutations(base_snapshot, entry.mutations)

    candidate = generate_schedule(scenario_snapshot)
    validation = validate(candidate, scenario_snapshot)
    breakdown = score(candidate, scenario_snapshot, resolved_weights)

    plan_id = _new_plan_id()
    baseline_plan_id = _new_plan_id()
    on_time, tardiness, late = _plan_kpis(candidate, scenario_snapshot)
    baseline_comparison = BaselineComparison(
        baseline_plan_id=baseline_plan_id,
        snapshot_version=scenario_snapshot.snapshot_version,
        on_time_rate=on_time,
        baseline_on_time_rate=on_time,
        total_tardiness_minutes=tardiness,
        baseline_total_tardiness_minutes=tardiness,
        late_order_count=late,
        baseline_late_order_count=late,
    )
    # 采纳只有一份候选（无基线）；传同一份两次，`_expand_production_jobs` 按 order_id 去重。
    production_jobs = _expand_production_jobs(scenario_snapshot, candidate, candidate)
    result = PlanGenerationResult(
        plan_id=plan_id,
        production_date=scenario_snapshot.production_date,
        status=PENDING_STATUS,
        input_snapshot_version=scenario_snapshot.snapshot_version,
        generated_by_trace_id="",  # 采纳无 LLM trace；确定性生成
        candidate=candidate,
        objective_breakdown=breakdown,
        validation=validation,
        baseline=baseline_comparison,
        production_jobs=production_jobs,
    )

    try:
        _ensure_production_jobs(session, specs=result.production_jobs)
        # 基线计划头：`save_proposed_plan` 写的 `baseline_comparisons` 行外键指向它，故须先落库。
        # 采纳无独立 FCFS 基线，这里以候选自身作占位（DRAFT/BASELINE，永不进审批流）——它只为
        # 满足 `baseline_comparisons.baseline_plan_id` 的外键，其 KPI 已在 baseline_comparison
        # 里与候选相等（采纳的对比基准是「当前真实数据 + 变更后的候选自身」，非 FCFS）。
        session.add(
            orm.ProductionPlan(
                plan_id=baseline_plan_id,
                production_date=result.production_date,
                status="DRAFT",
                feasibility=candidate.feasibility,
                plan_version=1,
                version=1,
                input_snapshot_version=result.input_snapshot_version,
                origin="BASELINE",
                generated_by_trace_id=None,
                created_at=_created_at(result),
            )
        )
        session.add(
            orm.ProductionPlan(
                plan_id=plan_id,
                production_date=result.production_date,
                status=PENDING_STATUS,
                feasibility=candidate.feasibility,
                plan_version=1,
                version=1,
                input_snapshot_version=result.input_snapshot_version,
                origin=SCENARIO_ADOPTION_ORIGIN,
                generated_by_trace_id=None,
                created_at=_created_at(result),
            )
        )
        session.flush()
        # 明细四表由 save_proposed_plan 落库（produced_in_sandbox=False：采纳产出的是正式提案，
        # 不是沙箱推演结果本身）。origin=SCENARIO_ADOPTION 非 BASELINE，通过护栏。
        save_proposed_plan(
            session,
            result=result,
            weights=resolved_weights,
            origin=SCENARIO_ADOPTION_ORIGIN,
            produced_in_sandbox=False,
        )
        session.commit()
    except Exception:
        session.rollback()
        raise

    log_event(logger, "SCENARIO_ADOPTED", scenario_id=scenario_id, plan_id=plan_id)
    return plan_id


def build_counterfactual(
    *,
    active_plan: PlanCandidate,
    candidate: PlanCandidate,
    snapshot: DomainSnapshot,
    plan_id: str,
    weights: ObjectiveWeights | None = None,
) -> Tradeoff | NoTradeoff:
    """构造**恰好 1 项**反事实（任务 8.4，R10.3、design.md §3.7）。确定性、无 LLM。

    步骤：
    1. `compute_plan_delta(active, candidate)` 得 MOVED / REASSIGNED / ADDED；
    2. `pick_pivotal_job(...)` 选唯一关键作业（三级排序键，纯函数）；
    3. 无可选对象 → `NoTradeoff(reason=...)`（不编造）；
    4. 否则把该**一个**作业按 `ACTIVE` 中的原机器/原开工时间**冻回原位**，其余重排
       （`generate_schedule(freeze=该作业)`），`score` 得主导分量在反事实下的值 Z；
       `current_value` 是候选方案下同分量的值 Y。二者装进 `Tradeoff`（R10.3）。

    Z 由 `Objective_Scorer` **实算**（R10.3 要求「由沙箱对原方案实际计算得出」，不估算）。
    """
    from app.core.delta import compute_plan_delta
    from app.core.sandbox import pick_pivotal_job

    resolved_weights = weights if weights is not None else ObjectiveWeights()
    delta = compute_plan_delta(active_plan, candidate)

    cand_breakdown = score(candidate, snapshot, resolved_weights, reference_plan=active_plan)
    selection = pick_pivotal_job(
        moved=frozenset(delta.moved),
        reassigned=frozenset(delta.reassigned),
        added=frozenset(delta.added),
        breakdown=cand_breakdown,
        active_jobs=active_plan.scheduled_jobs,
        candidate_jobs=candidate.scheduled_jobs,
    )
    if selection.job_id is None:
        return NoTradeoff(reason=selection.selection_basis)

    # 把选中作业冻回它在 ACTIVE 里的原位（原机器/原开工时间），其余重排。
    frozen = tuple(
        job for job in active_plan.scheduled_jobs if job.job_id == selection.job_id
    )
    counterfactual_candidate = generate_schedule(snapshot, freeze=frozen)
    cf_breakdown = score(
        counterfactual_candidate, snapshot, resolved_weights, reference_plan=active_plan
    )

    current_value = _component_value(cand_breakdown, selection.dominant)
    counterfactual_value = _component_value(cf_breakdown, selection.dominant)
    return Tradeoff(
        pivotal_job_id=selection.job_id,
        component=selection.dominant,
        current_value=current_value,
        counterfactual_value=counterfactual_value,
        selection_basis=selection.selection_basis,
    )


def _component_value(breakdown: ObjectiveBreakdown, component_name: str) -> float:
    """取指定分量的 `weighted_contribution`；找不到则退回 `total_score`（Z 的实算值）。"""
    for c in breakdown.components:
        if c.name == component_name:
            return c.weighted_contribution
    return breakdown.total_score


def _delayed_order_ids(candidate: PlanCandidate, snapshot: DomainSnapshot) -> tuple[str, ...]:
    """完工晚于 `due_date` 的订单（R16.8 的迟交订单集）。确定性、按 ID 升序。"""
    orders_by_id = snapshot.orders_by_id()
    completion: dict[str, datetime] = {}
    for job in candidate.scheduled_jobs:
        cur = completion.get(job.order_id)
        if cur is None or job.end_time > cur:
            completion[job.order_id] = job.end_time
    late = [
        oid
        for oid, comp in completion.items()
        if (o := orders_by_id.get(oid)) is not None and comp > o.due_date
    ]
    return tuple(sorted(late))
