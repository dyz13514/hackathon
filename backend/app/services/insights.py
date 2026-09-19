"""瓶颈与产能洞察（任务 13.5，P1-J ⑤，R15.1–R15.4）。

对当前 `ACTIVE` 计划做**确定性**的产能分析，产出：

- 每台机器的**利用率**（占用工时 ÷ 可用工时）、承担**作业数**、承担**订单价值占比**（R15.1）；
- **关键机器**：承担了作业、且没有具备相同 `capabilities` 的替代机器（R15.3）；
- 按 `required_worker_skill` 聚合的**技能缺口**：所需工时 vs 具备该技能的可用工时（R15.4）；
- 每台机器「**可用工时 +20% 时 `total_tardiness_minutes` 的变化量**」——由
  `run_capacity_sandbox(purpose=BOTTLENECK)` **实际计算**（R15.2），禁止静态估算或伪造数字。

## 只读、无 LLM

利用率/作业数/技能缺口全部由确定性代码从快照 + `ACTIVE` 计划的已排产作业算出；+20% 变化量由
沙箱确定性排产实算。全程不写任何生产数据（沙箱推演在 `sandbox_guard` 隔离下进行）。

## 订单价值占比的口径

演示数据的 `orders` 没有独立的金额/单价字段，因此「订单价值」以订单 `quantity`（下单数量）作为
规模代理：一台机器的「承担订单价值占比」= 该机器上作业所属订单的 quantity 之和 ÷ 全部已排产
作业所属订单的 quantity 之和。这是一个透明、确定性的口径；接入真实单价后只需替换权重来源。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from sqlalchemy.orm import Session

from app.core.scheduler import PlanCandidate
from app.core.snapshot import DomainSnapshot
from app.logging_config import log_event
from app.services.replanning import NoActivePlanError, load_plan_candidate, require_any_active_plan
from app.services.sandbox import SandboxPurpose, run_capacity_sandbox
from app.services.snapshot_loader import load_snapshot

logger = logging.getLogger(__name__)

#: 产能推演的工时放大倍数（R15.2「+20%」）。
CAPACITY_UPLIFT_MULTIPLIER = 1.2


@dataclass(frozen=True)
class MachineInsight:
    """一台机器在当前 `ACTIVE` 计划中的产能画像（R15.1–R15.3）。"""

    machine_id: str
    machine_type: str
    capabilities: tuple[str, ...]
    utilisation: float
    busy_minutes: int
    available_minutes: int
    job_count: int
    order_value_share: float
    #: 无相同 `capabilities` 替代机器且承担了作业 → 关键机器（R15.3）。
    is_critical: bool
    #: 可用工时 +20% 时 `total_tardiness_minutes` 的变化量（R15.2，沙箱实算，负=改善）。
    tardiness_delta_if_plus_20pct: int


@dataclass(frozen=True)
class SkillGap:
    """按 `required_worker_skill` 聚合的技能缺口（R15.4）。`gap_minutes > 0` 表示供不应求。"""

    skill: str
    required_minutes: int
    available_minutes: int
    gap_minutes: int


@dataclass(frozen=True)
class BottleneckInsights:
    machines: list[MachineInsight] = field(default_factory=list)
    skill_gaps: list[SkillGap] = field(default_factory=list)
    active_plan_id: str | None = None


def bottleneck_insights(session: Session, *, now: datetime) -> BottleneckInsights:
    """计算当前 `ACTIVE` 计划的瓶颈与产能洞察。**只读、无 LLM。** 无 ACTIVE 计划 → 抛。

    每台**承担了作业**的机器都做一次 `run_capacity_sandbox(+20%)` 实算其拖期变化量（R15.2）。
    未承担作业的机器不纳入洞察（它们不是当前的产能约束）。
    """
    active = require_any_active_plan(session)  # 无 ACTIVE → NoActivePlanError
    candidate = load_plan_candidate(session, active.plan_id)
    snapshot = load_snapshot(session, now=now, production_date=active.production_date)

    machines_by_id = snapshot.machines_by_id()
    orders_by_id = snapshot.orders_by_id()

    # 每台机器的占用分钟数、作业集合、承担订单价值（quantity 之和）。
    busy_minutes: dict[str, int] = {}
    job_count: dict[str, int] = {}
    machine_order_value: dict[str, Decimal] = {}
    total_order_value = Decimal("0")
    for sj in candidate.scheduled_jobs:
        minutes = int((sj.end_time - sj.start_time).total_seconds() // 60)
        busy_minutes[sj.machine_id] = busy_minutes.get(sj.machine_id, 0) + minutes
        job_count[sj.machine_id] = job_count.get(sj.machine_id, 0) + 1
        order = orders_by_id.get(sj.order_id)
        value = order.quantity if order is not None else Decimal("0")
        machine_order_value[sj.machine_id] = (
            machine_order_value.get(sj.machine_id, Decimal("0")) + value
        )
        total_order_value += value

    insights: list[MachineInsight] = []
    for machine_id in sorted(job_count):  # 只纳入承担了作业的机器，确定性顺序
        machine = machines_by_id.get(machine_id)
        if machine is None:
            continue
        available = int((machine.available_end - machine.available_start).total_seconds() // 60)
        busy = busy_minutes.get(machine_id, 0)
        utilisation = round(busy / available, 4) if available > 0 else 0.0
        value_share = (
            float(machine_order_value.get(machine_id, Decimal("0")) / total_order_value)
            if total_order_value > 0
            else 0.0
        )
        is_critical = _is_critical_machine(machine_id, snapshot)

        # +20% 工时的拖期变化量：沙箱实算（R15.2，禁止静态估算）。
        capacity = run_capacity_sandbox(
            session,
            machine_id=machine_id,
            hours_multiplier=CAPACITY_UPLIFT_MULTIPLIER,
            now=now,
            purpose=SandboxPurpose.BOTTLENECK,
        )

        insights.append(
            MachineInsight(
                machine_id=machine_id,
                machine_type=machine.machine_type,
                capabilities=machine.capabilities,
                utilisation=utilisation,
                busy_minutes=busy,
                available_minutes=available,
                job_count=job_count[machine_id],
                order_value_share=round(value_share, 4),
                is_critical=is_critical,
                tardiness_delta_if_plus_20pct=capacity.total_tardiness_delta_minutes,
            )
        )

    skill_gaps = _skill_gaps(candidate, snapshot)

    log_event(
        logger,
        "BOTTLENECK_INSIGHTS_COMPUTED",
        active_plan_id=active.plan_id,
        machine_count=len(insights),
        critical_count=sum(1 for m in insights if m.is_critical),
        skill_gap_count=sum(1 for g in skill_gaps if g.gap_minutes > 0),
    )
    return BottleneckInsights(
        machines=insights, skill_gaps=skill_gaps, active_plan_id=active.plan_id
    )


def _is_critical_machine(machine_id: str, snapshot: DomainSnapshot) -> bool:
    """该机器是否为关键机器：不存在具备**相同 `capabilities`** 替代机器（R15.3）。

    「相同 capabilities」以能力集合相等为准：另一台机器的能力集合与本机相同（且不是本机自己）
    即视为可替代。没有任何这样的替代机器 → 关键机器。这与「单点故障」的直觉一致：这台机器一旦
    出问题，没有能力等价的机器能接手它的作业。
    """
    target = snapshot.machines_by_id().get(machine_id)
    if target is None:
        return False
    target_caps = frozenset(target.capabilities)
    for other in snapshot.machines:
        if other.machine_id == machine_id:
            continue
        if frozenset(other.capabilities) == target_caps:
            return False  # 存在能力相同的替代机器 → 非关键
    return True


def _skill_gaps(candidate: PlanCandidate, snapshot: DomainSnapshot) -> list[SkillGap]:
    """按 `required_worker_skill` 聚合技能缺口（R15.4）：所需工时 vs 具备该技能的可用工时。

    - **所需工时**：已排产作业按其工序 `required_worker_skill` 分组，各作业时长（分钟）之和。
    - **可用工时**：具备该技能的工人各自班次时长（`shift_end − shift_start`）之和。
    - **缺口** = 所需 − 可用（正=供不应求）。技能集合取「计划里出现的技能」∪「工人具备的技能」，
      使既有作业需要但无人具备的技能也显式出现（缺口 = 全部所需，available=0）。
    """
    products_by_id = snapshot.products_by_id()

    # job_id → required_worker_skill（从产品工序取，job_id 形如 "{order}-OP{seq}"）。
    orders_by_id = snapshot.orders_by_id()
    required_by_skill: dict[str, int] = {}
    for sj in candidate.scheduled_jobs:
        order = orders_by_id.get(sj.order_id)
        product = products_by_id.get(order.product_id) if order is not None else None
        if product is None:
            continue
        seq = _op_sequence(sj.job_id)
        op = next((o for o in product.operations if o.sequence == seq), None)
        if op is None:
            continue
        minutes = int((sj.end_time - sj.start_time).total_seconds() // 60)
        required_by_skill[op.required_worker_skill] = (
            required_by_skill.get(op.required_worker_skill, 0) + minutes
        )

    available_by_skill: dict[str, int] = {}
    for worker in snapshot.workers:
        shift_minutes = int((worker.shift_end - worker.shift_start).total_seconds() // 60)
        for skill in worker.skills:
            available_by_skill[skill] = available_by_skill.get(skill, 0) + shift_minutes

    skills = sorted(set(required_by_skill) | set(available_by_skill))
    gaps: list[SkillGap] = []
    for skill in skills:
        required = required_by_skill.get(skill, 0)
        available = available_by_skill.get(skill, 0)
        # 只报告计划里实际需要的技能（required>0）——从不需要的技能谈「缺口」无意义。
        if required == 0:
            continue
        gaps.append(
            SkillGap(
                skill=skill,
                required_minutes=required,
                available_minutes=available,
                gap_minutes=required - available,
            )
        )
    return gaps


def _op_sequence(job_id: str) -> int:
    """从 `"{order_id}-OP{seq}"` 解析工序序号；解析失败返回 -1（不匹配任何工序）。"""
    marker = "-OP"
    idx = job_id.rfind(marker)
    if idx < 0:
        return -1
    try:
        return int(job_id[idx + len(marker) :])
    except ValueError:
        return -1


__all__ = [
    "CAPACITY_UPLIFT_MULTIPLIER",
    "BottleneckInsights",
    "MachineInsight",
    "NoActivePlanError",
    "SkillGap",
    "bottleneck_insights",
]
