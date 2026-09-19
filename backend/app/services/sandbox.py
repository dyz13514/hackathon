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
from datetime import date, datetime

from sqlalchemy.orm import Session

from app.core.explain import NoTradeoff, Tradeoff
from app.core.sandbox import apply_mutations
from app.core.scheduler import PlanCandidate, generate_schedule
from app.core.scoring import ObjectiveBreakdown, ObjectiveWeights, score
from app.core.snapshot import DomainSnapshot
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

__all__ = [
    "ScenarioNotFoundError",
    "ScenarioStore",
    "SandboxResult",
    "adopt_scenario",
    "build_counterfactual",
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
