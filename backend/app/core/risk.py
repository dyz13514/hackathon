"""`Risk_Scanner` 的确定性内核（任务 8.5，R14.1–4 / R14.9、design.md §3.8、ADR-013）。

## 这一层守的边界

本模块是**纯内核**：不 import `sqlalchemy` / `app.db` / `app.services`（`test_layering.py` 第
①② 条静态断言 `app/core/**` 不碰 I/O）。它只吃两样东西——一份冻结的 `DomainSnapshot` 与当前
`ACTIVE` 计划的已排产作业（`tuple[ScheduledJob, ...]`），产出一组 `RiskFinding` 值对象。落库、
去重、模板叙述、触发器都在服务层（`app/services/risk_scan.py`，任务 8.5 的另一半）。把度量算在
内核让「同一份输入必得同一组风险」成为可逐字段断言的性质（与 R5.7 同一纪律）。

## 5 类风险与阈值（R14.3–4）

阈值全部写成**模块级常量**（design.md §3.8 点名的五个），由 Pytest 逐分支覆盖（R27.10）。
每类风险的度量与 severity 判据：

- `MATERIAL_RUNOUT_FORECAST`：度量=时域内某物料可用量降到 0 的时刻；
  降到 0 → `WARNING`；24h 内降到 0 且无在途 → `CRITICAL`。
- `ZERO_SLACK_ORDER`：度量=订单 slack（交期 − 预计完工，分钟）；
  `slack ≤ 0` → `CRITICAL`；`≤ 120` → `WARNING`。
- `BOTTLENECK_RESOURCE`：度量=机器利用率（占用 ÷ 可用工时）；
  `≥ 0.90` → `WARNING`；`≥ 0.98` → `CRITICAL`。
- `OVERCOMMITTED_SHIFT`：度量=某工人班次内所需工时 vs 可用工时；
  所需 > 可用 → `CRITICAL`。
- `SINGLE_POINT_OF_FAILURE_MACHINE`：度量=某机器承担的作业占比；
  `≥ 0.50` 且无同能力替代机器 → `WARNING`。

## 确定性排序与 `finding_key`（R14.9）

`scan()` 返回按 `(SEVERITY_RANK[severity], finding_key)` 升序的元组——同输入同顺序。
`finding_key = sha1(f"{risk_type}|{entity_type}|{entity_id}")`：服务层据它去重，同一风险在多次
扫描里反复出现只 `UPDATE last_seen_at, metric_value`，不新增行。key 由内核算是刻意的——它是
风险身份的一部分，而身份不该依赖落库顺序或时间戳。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Literal

from app.core.scheduler import ScheduledJob
from app.core.snapshot import DomainSnapshot, Material, available_at

__all__ = [
    "MATERIAL_CRITICAL_HOURS",
    "ROLLING_HORIZON_DAYS",
    "SEVERITY_RANK",
    "SLACK_WARNING_MINUTES",
    "SPOF_JOB_SHARE",
    "UTIL_CRITICAL",
    "UTIL_WARNING",
    "RiskFinding",
    "RiskSeverity",
    "RiskType",
    "scan",
]

# --------------------------------------------------------------------------
# 阈值（design.md §3.8 点名的五个 + 时域）
# --------------------------------------------------------------------------

#: 物料在 24 小时内降到 0 且无在途 → CRITICAL（R14.4 第 1 条）。
MATERIAL_CRITICAL_HOURS = 24
#: 订单 slack ≤ 120 分钟 → WARNING（R14.4 第 2 条）。
SLACK_WARNING_MINUTES = 120
#: 机器利用率阈值（R14.4 第 3 条）。
UTIL_WARNING = 0.90
UTIL_CRITICAL = 0.98
#: 单机承担作业占比阈值（R14.4 第 4 条 SPOF）。
SPOF_JOB_SHARE = 0.50
#: 滚动扫描时域（R14.2），默认 3 天；随 seed 定稿复核（Open Question 5）。
ROLLING_HORIZON_DAYS = 3


class RiskType(StrEnum):
    """5 类风险（R14.3）。取值与 DB 列值、工具契约逐字对齐。

    用 `enum.StrEnum`（与 `api/errors.py::ErrorCode`、`services/approval.py::ApprovalStatus`
    同一写法）：成员即字符串，`.value` 与直接当字符串用的语义与旧式 `(str, Enum)` 完全一致，
    因此 DB 列值、`finding_key` 拼接、契约取值都不受影响。
    """

    MATERIAL_RUNOUT_FORECAST = "MATERIAL_RUNOUT_FORECAST"
    ZERO_SLACK_ORDER = "ZERO_SLACK_ORDER"
    BOTTLENECK_RESOURCE = "BOTTLENECK_RESOURCE"
    OVERCOMMITTED_SHIFT = "OVERCOMMITTED_SHIFT"
    SINGLE_POINT_OF_FAILURE_MACHINE = "SINGLE_POINT_OF_FAILURE_MACHINE"


RiskSeverity = Literal["INFO", "WARNING", "CRITICAL"]

#: 严重度秩：CRITICAL 最先（数值最小），供确定性排序（design.md §3.8）。
SEVERITY_RANK: dict[str, int] = {"CRITICAL": 0, "WARNING": 1, "INFO": 2}


@dataclass(frozen=True)
class RiskFinding:
    """一条风险发现（内核值对象，R14.9）。

    与 ORM `RiskFinding` 区分：这是纯计算产物，不含 `finding_id` / 时间戳 / `narrative`
    ——那些是落库时由服务层补的。`finding_key` 由内核算（身份的一部分，见模块 docstring）。
    `affected_order_ids` 已排序去重，供叙述展开与确定性断言。
    """

    risk_type: RiskType
    severity: RiskSeverity
    entity_type: str  # "MATERIAL" | "ORDER" | "MACHINE" | "WORKER"
    entity_id: str
    metric_value: Decimal
    threshold_value: Decimal
    affected_order_ids: tuple[str, ...] = field(default_factory=tuple)

    @property
    def finding_key(self) -> str:
        """`sha1(risk_type|entity_type|entity_id)`（R14.9 去重键）。"""
        raw = f"{self.risk_type.value}|{self.entity_type}|{self.entity_id}"
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()  # noqa: S324 — 去重键，非安全用途


# --------------------------------------------------------------------------
# 主入口
# --------------------------------------------------------------------------


def scan(
    snapshot: DomainSnapshot,
    scheduled_jobs: tuple[ScheduledJob, ...],
    *,
    horizon_days: int = ROLLING_HORIZON_DAYS,
) -> tuple[RiskFinding, ...]:
    """在滚动时域内扫描 5 类风险，返回按 `(SEVERITY_RANK, finding_key)` 升序的元组。

    `scheduled_jobs` 是当前 `ACTIVE` 计划的已排产作业（服务层用 `load_plan_candidate` 读回）
    ——利用率 / SPOF / 班次工时 / 物料消耗都据它算。无 ACTIVE 计划时传空元组，此时只有
    「不依赖计划」的风险可能触发（当前 5 类都依赖计划或快照，空计划下多为空）。确定性、无 LLM。
    """
    horizon_end = snapshot.now + timedelta(days=horizon_days)
    findings: list[RiskFinding] = [
        *_material_runout(snapshot, scheduled_jobs, horizon_end),
        *_zero_slack_orders(snapshot, scheduled_jobs),
        *_bottleneck_resources(snapshot, scheduled_jobs),
        *_overcommitted_shifts(snapshot, scheduled_jobs),
        *_single_point_of_failure(snapshot, scheduled_jobs),
    ]
    return tuple(sorted(findings, key=lambda f: (SEVERITY_RANK[f.severity], f.finding_key)))


# --------------------------------------------------------------------------
# 1. MATERIAL_RUNOUT_FORECAST（R14.4 第 1 条）
# --------------------------------------------------------------------------


def _material_runout(
    snapshot: DomainSnapshot,
    scheduled_jobs: tuple[ScheduledJob, ...],
    horizon_end: datetime,
) -> list[RiskFinding]:
    """按已排产消耗，物料在时域内降到 0 → WARNING；24h 内降到 0 且无在途 → CRITICAL。

    消耗按 BOM 归到「作业开工时刻」：一个作业在 `start_time` 消耗其订单量 × BOM 每件用量。
    在时域内累计消耗后，用 `available_at`（含在途到货）判断某时刻净可用是否 ≤ 0。
    """
    products = snapshot.products_by_id()
    orders = snapshot.orders_by_id()

    # 一个订单的物料按 BOM 只消耗一次（在该订单最早开工时刻）——核心 `ScheduledJob` 不带
    # 工序号，因此以「订单的最早 start_time」定位消耗时刻，避免多工序作业重复计同一份物料。
    order_earliest_start: dict[str, datetime] = {}
    for job in scheduled_jobs:
        if job.start_time >= horizon_end:
            continue
        cur = order_earliest_start.get(job.order_id)
        if cur is None or job.start_time < cur:
            order_earliest_start[job.order_id] = job.start_time

    # 每种物料的消耗事件：(消耗时刻, 数量)。
    consumption: dict[str, list[tuple[datetime, Decimal]]] = {}
    for order_id, start in order_earliest_start.items():
        order = orders.get(order_id)
        if order is None:
            continue
        product = products.get(order.product_id)
        if product is None:
            continue
        for line in product.bom:
            consumption.setdefault(line.material_id, []).append(
                (start, line.quantity_per_unit * order.quantity)
            )

    findings: list[RiskFinding] = []
    critical_before = snapshot.now + timedelta(hours=MATERIAL_CRITICAL_HOURS)
    for material in snapshot.materials:
        events = sorted(consumption.get(material.material_id, []), key=lambda e: e[0])
        if not events:
            continue
        runout_at = _runout_time(material, events, horizon_end)
        if runout_at is None:
            continue
        has_incoming = any(
            d.eta < horizon_end for d in material.incoming_deliveries
        )
        severity: RiskSeverity = (
            "CRITICAL" if (runout_at <= critical_before and not has_incoming) else "WARNING"
        )
        # metric = 距降到 0 的小时数（≥0）；threshold = CRITICAL 小时阈值。
        hours_to_runout = max((runout_at - snapshot.now).total_seconds() / 3600.0, 0.0)
        affected = tuple(
            sorted(
                {
                    job.order_id
                    for job in scheduled_jobs
                    if job.start_time <= runout_at
                    and any(
                        line.material_id == material.material_id
                        for line in products.get(job.product_id, _EMPTY_PRODUCT).bom
                    )
                }
            )
        )
        findings.append(
            RiskFinding(
                risk_type=RiskType.MATERIAL_RUNOUT_FORECAST,
                severity=severity,
                entity_type="MATERIAL",
                entity_id=material.material_id,
                metric_value=Decimal(str(round(hours_to_runout, 2))),
                threshold_value=Decimal(MATERIAL_CRITICAL_HOURS),
                affected_order_ids=affected,
            )
        )
    return findings


def _runout_time(
    material: Material,
    events: list[tuple[datetime, Decimal]],
    horizon_end: datetime,
) -> datetime | None:
    """在每个消耗时刻后检查净可用是否 ≤ 0；返回首个降到 0 的时刻，否则 None。

    `available_at(material, t)` 已含「`eta < t` 的在途到货」。这里在每个消耗事件的时刻，
    先取该时刻的到货后可用量，再减去截至该时刻（含）的累计消耗——即那一刻的真实净可用。
    """
    cumulative = Decimal("0")
    for moment, qty in events:
        if moment >= horizon_end:
            break
        cumulative += qty
        net = available_at(material, moment) - cumulative
        if net <= 0:
            return moment
    return None


# --------------------------------------------------------------------------
# 2. ZERO_SLACK_ORDER（R14.4 第 2 条）
# --------------------------------------------------------------------------


def _zero_slack_orders(
    snapshot: DomainSnapshot,
    scheduled_jobs: tuple[ScheduledJob, ...],
) -> list[RiskFinding]:
    """订单 slack = 交期 − 预计完工（分钟）。`≤ 0` → CRITICAL；`≤ 120` → WARNING。

    预计完工取该订单全部已排产作业的最晚 `end_time`（与 `_orders_at_risk` 同口径）。未排产的
    订单没有完工时刻，不在本风险内（它们是 `unschedulable`，另有 R8 路径）。
    """
    completion: dict[str, datetime] = {}
    for job in scheduled_jobs:
        cur = completion.get(job.order_id)
        if cur is None or job.end_time > cur:
            completion[job.order_id] = job.end_time

    orders = snapshot.orders_by_id()
    findings: list[RiskFinding] = []
    for order_id, finish in sorted(completion.items()):
        order = orders.get(order_id)
        if order is None:
            continue
        slack_minutes = int((order.due_date - finish).total_seconds() // 60)
        if slack_minutes <= 0:
            severity: RiskSeverity = "CRITICAL"
        elif slack_minutes <= SLACK_WARNING_MINUTES:
            severity = "WARNING"
        else:
            continue
        findings.append(
            RiskFinding(
                risk_type=RiskType.ZERO_SLACK_ORDER,
                severity=severity,
                entity_type="ORDER",
                entity_id=order_id,
                metric_value=Decimal(slack_minutes),
                threshold_value=Decimal(SLACK_WARNING_MINUTES),
                affected_order_ids=(order_id,),
            )
        )
    return findings


# --------------------------------------------------------------------------
# 3. BOTTLENECK_RESOURCE（R14.4 第 3 条）
# --------------------------------------------------------------------------


def _bottleneck_resources(
    snapshot: DomainSnapshot,
    scheduled_jobs: tuple[ScheduledJob, ...],
) -> list[RiskFinding]:
    """机器利用率 = 占用分钟 ÷ 可用分钟。`≥ 0.98` → CRITICAL；`≥ 0.90` → WARNING。

    占用取该机器全部作业 `[start_time, end_time)` 的总时长（含换型）；可用取
    `[available_start, available_end)` 减去停机窗。可用为 0 的机器跳过（除零无意义）。
    """
    busy: dict[str, int] = {}
    orders_by_machine: dict[str, set[str]] = {}
    for job in scheduled_jobs:
        minutes = int((job.end_time - job.start_time).total_seconds() // 60)
        busy[job.machine_id] = busy.get(job.machine_id, 0) + minutes
        orders_by_machine.setdefault(job.machine_id, set()).add(job.order_id)

    findings: list[RiskFinding] = []
    for machine in snapshot.machines:
        available = _machine_available_minutes(machine)
        if available <= 0:
            continue
        occupied = busy.get(machine.machine_id, 0)
        util = occupied / available
        if util >= UTIL_CRITICAL:
            severity: RiskSeverity = "CRITICAL"
        elif util >= UTIL_WARNING:
            severity = "WARNING"
        else:
            continue
        findings.append(
            RiskFinding(
                risk_type=RiskType.BOTTLENECK_RESOURCE,
                severity=severity,
                entity_type="MACHINE",
                entity_id=machine.machine_id,
                metric_value=Decimal(str(round(util, 4))),
                threshold_value=Decimal(str(UTIL_WARNING)),
                affected_order_ids=tuple(sorted(orders_by_machine.get(machine.machine_id, ()))),
            )
        )
    return findings


def _machine_available_minutes(machine: object) -> int:
    """机器可用分钟：`[available_start, available_end)` 减去停机窗内的分钟（不重叠假设）。"""
    start = machine.available_start  # type: ignore[attr-defined]
    end = machine.available_end  # type: ignore[attr-defined]
    total = int((end - start).total_seconds() // 60)
    downtime = 0
    for window in machine.downtime_windows:  # type: ignore[attr-defined]
        lo = max(window.start, start)
        hi = min(window.end, end)
        if hi > lo:
            downtime += int((hi - lo).total_seconds() // 60)
    return max(total - downtime, 0)


# --------------------------------------------------------------------------
# 4. OVERCOMMITTED_SHIFT（R14.4 第 5 条）
# --------------------------------------------------------------------------


def _overcommitted_shifts(
    snapshot: DomainSnapshot,
    scheduled_jobs: tuple[ScheduledJob, ...],
) -> list[RiskFinding]:
    """某工人班次内所需工时 > 可用工时 → CRITICAL。

    所需 = 该工人全部作业时长之和；可用 = `[shift_start, shift_end)` 减去缺勤窗。
    """
    required: dict[str, int] = {}
    orders_by_worker: dict[str, set[str]] = {}
    for job in scheduled_jobs:
        minutes = int((job.end_time - job.start_time).total_seconds() // 60)
        required[job.worker_id] = required.get(job.worker_id, 0) + minutes
        orders_by_worker.setdefault(job.worker_id, set()).add(job.order_id)

    findings: list[RiskFinding] = []
    for worker in snapshot.workers:
        need = required.get(worker.worker_id, 0)
        if need == 0:
            continue
        available = _worker_available_minutes(worker)
        if need > available:
            findings.append(
                RiskFinding(
                    risk_type=RiskType.OVERCOMMITTED_SHIFT,
                    severity="CRITICAL",
                    entity_type="WORKER",
                    entity_id=worker.worker_id,
                    metric_value=Decimal(need),
                    threshold_value=Decimal(available),
                    affected_order_ids=tuple(
                        sorted(orders_by_worker.get(worker.worker_id, ()))
                    ),
                )
            )
    return findings


def _worker_available_minutes(worker: object) -> int:
    start = worker.shift_start  # type: ignore[attr-defined]
    end = worker.shift_end  # type: ignore[attr-defined]
    total = int((end - start).total_seconds() // 60)
    absent = 0
    for window in worker.absences:  # type: ignore[attr-defined]
        lo = max(window.start, start)
        hi = min(window.end, end)
        if hi > lo:
            absent += int((hi - lo).total_seconds() // 60)
    return max(total - absent, 0)


# --------------------------------------------------------------------------
# 5. SINGLE_POINT_OF_FAILURE_MACHINE（R14.4 第 4 条）
# --------------------------------------------------------------------------


def _single_point_of_failure(
    snapshot: DomainSnapshot,
    scheduled_jobs: tuple[ScheduledJob, ...],
) -> list[RiskFinding]:
    """某机器承担 ≥ 50% 已排产作业且无同能力替代机器 → WARNING。

    「同能力替代」判据：存在另一台机器，其 `machine_type` 相同且 `capabilities` 是该机器所需
    能力的超集（保守取「capabilities ⊇ 本机 capabilities」，即能接下本机全部作业）。
    """
    total_jobs = len(scheduled_jobs)
    if total_jobs == 0:
        return []
    count: dict[str, int] = {}
    orders_by_machine: dict[str, set[str]] = {}
    for job in scheduled_jobs:
        count[job.machine_id] = count.get(job.machine_id, 0) + 1
        orders_by_machine.setdefault(job.machine_id, set()).add(job.order_id)

    machines = snapshot.machines_by_id()
    findings: list[RiskFinding] = []
    for machine_id, n in count.items():
        share = n / total_jobs
        if share < SPOF_JOB_SHARE:
            continue
        this = machines.get(machine_id)
        if this is None:
            continue
        has_substitute = any(
            other.machine_id != machine_id
            and other.machine_type == this.machine_type
            and set(this.capabilities).issubset(set(other.capabilities))
            for other in snapshot.machines
        )
        if has_substitute:
            continue
        findings.append(
            RiskFinding(
                risk_type=RiskType.SINGLE_POINT_OF_FAILURE_MACHINE,
                severity="WARNING",
                entity_type="MACHINE",
                entity_id=machine_id,
                metric_value=Decimal(str(round(share, 4))),
                threshold_value=Decimal(str(SPOF_JOB_SHARE)),
                affected_order_ids=tuple(sorted(orders_by_machine.get(machine_id, ()))),
            )
        )
    return findings


# --------------------------------------------------------------------------
# 内部占位（避免 KeyError 时的空产品）
# --------------------------------------------------------------------------


class _EmptyProduct:
    bom: tuple = ()
    operations: tuple = ()


_EMPTY_PRODUCT = _EmptyProduct()
