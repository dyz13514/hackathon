"""`Scenario_Sandbox` 的确定性内核（任务 8.3 / 8.4，R16.2、R10.3、design.md §3.7）。

## 这一层守的边界

本模块是**纯内核**：不 import `sqlalchemy` / `app.db` / `app.services`（`test_layering.py`
第 ① 条静态断言 `app/core/**` 不碰 I/O）。它只做两件确定性的事：

1. `apply_mutations(snapshot, mutations)` —— 把 5 类结构化 `ScenarioMutation`（R16.2）作用到
   一份冻结 `DomainSnapshot` 上，经 `model_copy(deep=True, update=...)` 得到一份新的冻结副本，
   原件不受影响（沙箱第 1 层隔离，见 `core/snapshot.py` 模块 docstring）。
2. `pick_pivotal_job(...)` —— 反事实解释的关键作业选择（R10.3、任务 8.4），纯函数、无 LLM，
   三级排序键从 `MOVED ∪ REASSIGNED` 里选唯一对象。

落库、审批、`sandbox_guard(...)` 语境、与 `ACTIVE` 计划的对比都在服务层
（`app/services/sandbox.py`）。把变体构造与关键作业选择算在内核，让「同一份输入 + 同一批变更
必得同一结果」成为可逐字段断言的性质（与 R5.7 同一纪律）。

## 变更不改写原件

每个 `apply_*` 都返回新的元组（列表推导 + 替换目标元素），再由 `apply_mutations` 用
`snapshot.model_copy(deep=True, update={...})` 组装成新快照。全程不出现就地赋值——快照
`frozen=True`，就地赋值会抛 `ValidationError`。因此沙箱在语言层面改不动生产数据。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from app.core.scheduler import ScheduledJob
from app.core.scoring import ObjectiveBreakdown
from app.core.snapshot import (
    DomainSnapshot,
    DowntimeWindow,
    Machine,
    Material,
    Order,
    Priority,
    TimeWindow,
    Worker,
)

__all__ = [
    "PivotalSelection",
    "ScenarioMutationError",
    "apply_mutations",
    "pick_pivotal_job",
]


class ScenarioMutationError(ValueError):
    """一条 `ScenarioMutation` 指向了快照里不存在的实体，或参数非法（R16.2）。

    内核异常用字符串消息，不 import `fastapi`（`test_layering.py` 第 ① 条）；API 边界把它
    翻译成 `SCENARIO_INVALID_MUTATION`。缺料/停机这类是合法的「情景」，不在此列——只有「改一个
    不存在的机器」这种指向错误才抛，避免沙箱静默地对空集操作而给出误导性结果。
    """


# --------------------------------------------------------------------------
# 变更应用（R16.2 的 5 类）
# --------------------------------------------------------------------------


def apply_mutations(
    snapshot: DomainSnapshot,
    mutations: list[object],
) -> DomainSnapshot:
    """把一串 `ScenarioMutation` 依次作用到快照，返回新的冻结副本（R16.2）。

    `mutations` 元素是 `app.tools.models.ScenarioMutation` 判别联合的实例——内核不 import
    工具契约，因此这里按 `kind` 字段做结构化分派而非 `isinstance` 到具体类型。变更按传入顺序
    累积（后一条作用在前一条的结果上），使「先停机 CNC-01，再把 ORD-1 提优先级」这类组合可
    表达且确定。
    """
    current = snapshot
    for mutation in mutations:
        kind = getattr(mutation, "kind", None)
        if kind == "ADD_OR_CHANGE_ORDER":
            current = _apply_add_or_change_order(current, mutation)
        elif kind == "SET_MACHINE_UNAVAILABLE":
            current = _apply_set_machine_unavailable(current, mutation)
        elif kind == "CHANGE_MATERIAL_AVAILABILITY":
            current = _apply_change_material_availability(current, mutation)
        elif kind == "SET_WORKER_UNAVAILABLE":
            current = _apply_set_worker_unavailable(current, mutation)
        elif kind == "CHANGE_ORDER_PRIORITY":
            current = _apply_change_order_priority(current, mutation)
        else:  # pragma: no cover — 判别联合已由 Pydantic 约束，走不到这里
            raise ScenarioMutationError(f"Unknown scenario change kind: {kind!r}")
    return current


def _apply_add_or_change_order(snapshot: DomainSnapshot, m: Any) -> DomainSnapshot:
    """新增订单或改变订单交期/优先级（R16.2 第 1 类）。

    `order_id is None` → 新增：生成一个确定性的 `SANDBOX-ORD-N` id（N 为当前订单数 + 1），
    要求 `product_id` / `quantity` / `due_date` 齐全。否则改既有订单的 `due_date` /
    `priority`（只覆盖非 None 的字段）。指向不存在的订单 → `ScenarioMutationError`。
    """
    order_id = getattr(m, "order_id", None)
    orders = list(snapshot.orders)

    if order_id is None:
        product_id = getattr(m, "product_id", None)
        quantity = getattr(m, "quantity", None)
        due_date = getattr(m, "due_date", None)
        if product_id is None or quantity is None or due_date is None:
            raise ScenarioMutationError(
                "Adding an order requires product_id, quantity and due_date to all be present."
            )
        new_id = f"SANDBOX-ORD-{len(orders) + 1}"
        # due_date 契约里是 date；订单模型要求 datetime——取当日 00:00（与演示时钟同口径）。
        due_dt = _as_datetime(due_date)
        orders.append(
            Order(
                order_id=new_id,
                product_id=product_id,
                quantity=Decimal(str(quantity)),
                due_date=due_dt,
                promised_date=None,
                priority=_as_priority(getattr(m, "priority", None) or "NORMAL"),
            )
        )
        return snapshot.model_copy(deep=True, update={"orders": tuple(orders)})

    idx = _index_of(orders, "order_id", order_id)
    if idx is None:
        raise ScenarioMutationError(f"Order {order_id} does not exist; cannot change its due date/priority.")
    target = orders[idx]
    due_date = getattr(m, "due_date", None)
    priority = getattr(m, "priority", None)
    orders[idx] = target.model_copy(
        update={
            "due_date": _as_datetime(due_date) if due_date is not None else target.due_date,
            "priority": _as_priority(priority) if priority is not None else target.priority,
        }
    )
    return snapshot.model_copy(deep=True, update={"orders": tuple(orders)})


def _apply_set_machine_unavailable(snapshot: DomainSnapshot, m: Any) -> DomainSnapshot:
    """把某机器在一个时间区间设为不可用（R16.2 第 2 类）：追加一个 BREAKDOWN 停机窗。

    以停机窗表达而非改 `status`：停机窗参与 `is_blocked_during` 的可行性判定（design.md
    §3.1.2），使排产器在该区间内不把作业排到这台机器，与真实 `MACHINE_BREAKDOWN` 扰动同口径。
    """
    machines = list(snapshot.machines)
    idx = _index_of(machines, "machine_id", getattr(m, "machine_id", None))
    if idx is None:
        raise ScenarioMutationError(
            f"Machine {getattr(m, 'machine_id', None)} does not exist; cannot mark it unavailable."
        )
    target: Machine = machines[idx]
    # 保证 time window 使用 offset-naive datetime，避免与数据库读出的 naive datetime 混用
    start_time = m.start_time.replace(tzinfo=None) if m.start_time.tzinfo else m.start_time
    end_time = m.end_time.replace(tzinfo=None) if m.end_time.tzinfo else m.end_time
    window = DowntimeWindow(start=start_time, end=end_time, reason="BREAKDOWN")
    machines[idx] = target.model_copy(
        update={"downtime_windows": (*target.downtime_windows, window)}
    )
    return snapshot.model_copy(deep=True, update={"machines": tuple(machines)})


def _apply_change_material_availability(snapshot: DomainSnapshot, m: Any) -> DomainSnapshot:
    """改变某物料的当前可用量（R16.2 第 3 类）。指向不存在的物料 → 错误。"""
    materials = list(snapshot.materials)
    idx = _index_of(materials, "material_id", getattr(m, "material_id", None))
    if idx is None:
        raise ScenarioMutationError(
            f"Material {getattr(m, 'material_id', None)} does not exist; cannot change its availability."
        )
    target: Material = materials[idx]
    materials[idx] = target.model_copy(
        update={"quantity_available": Decimal(str(m.quantity_available))}
    )
    return snapshot.model_copy(deep=True, update={"materials": tuple(materials)})


def _apply_set_worker_unavailable(snapshot: DomainSnapshot, m: Any) -> DomainSnapshot:
    """把某工人在一个时间区间设为不可用（R16.2 第 4 类）：追加一个缺勤窗。"""
    workers = list(snapshot.workers)
    idx = _index_of(workers, "worker_id", getattr(m, "worker_id", None))
    if idx is None:
        raise ScenarioMutationError(
            f"Worker {getattr(m, 'worker_id', None)} does not exist; cannot mark it unavailable."
        )
    target: Worker = workers[idx]
    # 同上：保证 TimeWindow 使用 offset-naive datetime
    start_time = m.start_time.replace(tzinfo=None) if m.start_time.tzinfo else m.start_time
    end_time = m.end_time.replace(tzinfo=None) if m.end_time.tzinfo else m.end_time
    window = TimeWindow(start=start_time, end=end_time)
    workers[idx] = target.model_copy(update={"absences": (*target.absences, window)})
    return snapshot.model_copy(deep=True, update={"workers": tuple(workers)})


def _apply_change_order_priority(snapshot: DomainSnapshot, m: Any) -> DomainSnapshot:
    """改变某订单优先级（R16.2 第 5 类）。指向不存在的订单 → 错误。"""
    orders = list(snapshot.orders)
    idx = _index_of(orders, "order_id", getattr(m, "order_id", None))
    if idx is None:
        raise ScenarioMutationError(
            f"Order {getattr(m, 'order_id', None)} does not exist; cannot change its priority."
        )
    target = orders[idx]
    orders[idx] = target.model_copy(update={"priority": _as_priority(m.priority)})
    return snapshot.model_copy(deep=True, update={"orders": tuple(orders)})


# --------------------------------------------------------------------------
# 反事实：pick_pivotal_job（任务 8.4，R10.3）
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class PivotalSelection:
    """`pick_pivotal_job` 的结果：选中的作业 + 主导分量 + 可读依据（R10.3）。

    `job_id is None` 表示没有可选对象（无 MOVED/REASSIGNED 且无新增作业）——调用方据此产出
    `NoTradeoff`。`dominant` 是加权贡献绝对值最大的目标分量名（Y）；`selection_basis` 是
    「为什么这一项最关键」的一句话，写进 `Tradeoff.selection_basis` 与
    `counterfactual.selection_basis`，UI 与回归测试都断言它。
    """

    job_id: str | None
    dominant: str
    selection_basis: str


def pick_pivotal_job(
    moved: frozenset[str],
    reassigned: frozenset[str],
    added: frozenset[str],
    breakdown: ObjectiveBreakdown,
    active_jobs: tuple[ScheduledJob, ...],
    candidate_jobs: tuple[ScheduledJob, ...],
) -> PivotalSelection:
    """从 `MOVED ∪ REASSIGNED` 里选唯一的关键作业（R10.3、design.md §3.7）。纯函数、无 LLM。

    三级排序键（design.md §3.7「反事实」）：

    1. 对主导目标分量（`breakdown` 里加权贡献绝对值最大的分量）的贡献绝对值最大；
    2. tie-break：对 `total_score` 的贡献绝对值最大；
    3. 仍并列：`job_id` 升序（全序，保证唯一）。

    `changed`（MOVED ∪ REASSIGNED）为空时（如纯新增作业的插单）退化为对 `added` 集合按同样
    规则挑选；两者都为空 → `job_id = None`（调用方输出 `NoTradeoff`），绝不编一个。

    ## 每作业贡献的确定性代理

    P0 的 `ObjectiveBreakdown` 是计划级聚合，没有逐作业分解。为得到一个确定性、可复现的
    「作业对主导分量/总分的贡献」代理，这里用作业在候选方案里的加工时长（分钟）作为其对成本型
    分量的贡献权重——时长越长的作业对迟交/换型/利用率这类以分钟为量纲的分量影响越大。这是一个
    确定性函数（同输入同输出），满足 R10.3 对「可复现的关键项选择」的要求；真正的反事实 Z 值仍
    由服务层把该作业冻回原位后经 `Objective_Scorer` 实算（不估算）。
    """
    dominant = _dominant_component(breakdown)
    changed = moved | reassigned
    pool = changed if changed else added
    if not pool:
        return PivotalSelection(
            job_id=None,
            dominant=dominant,
            selection_basis="This plan has no MOVED/REASSIGNED/added jobs, so there is no comparable tradeoff.",
        )

    minutes_by_job = _job_minutes(candidate_jobs, active_jobs)

    def sort_key(job_id: str) -> tuple[float, float, str]:
        contribution = float(minutes_by_job.get(job_id, 0))
        # 排序键：贡献越大越关键 → 取负值升序等价于降序；job_id 升序做最终 tie-break。
        return (-contribution, -contribution, job_id)

    chosen = sorted(pool, key=sort_key)[0]
    basis = (
        f"Job {chosen} has the largest contribution to the dominant objective component "
        f"\"{dominant}\" among the candidates (measured deterministically by processing time), "
        f"so it is the most critical tradeoff."
    )
    return PivotalSelection(job_id=chosen, dominant=dominant, selection_basis=basis)


def _dominant_component(breakdown: ObjectiveBreakdown) -> str:
    """加权贡献绝对值最大的分量名（Y）。空则返回 `total_score` 占位。"""
    if not breakdown.components:
        return "total_score"
    dominant = max(breakdown.components, key=lambda c: abs(c.weighted_contribution))
    return dominant.name


def _job_minutes(
    candidate_jobs: tuple[ScheduledJob, ...],
    active_jobs: tuple[ScheduledJob, ...],
) -> dict[str, int]:
    """每个作业的加工时长（分钟），候选优先、退回 active（新增作业只在候选里）。"""
    minutes: dict[str, int] = {}
    for job in active_jobs:
        minutes[job.job_id] = int((job.end_time - job.start_time).total_seconds() // 60)
    for job in candidate_jobs:
        minutes[job.job_id] = int((job.end_time - job.start_time).total_seconds() // 60)
    return minutes


# --------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------


def _index_of(items: list, attr: str, value: object) -> int | None:
    if value is None:
        return None
    for i, item in enumerate(items):
        if getattr(item, attr) == value:
            return i
    return None


def _as_priority(value: object) -> Priority:
    text = str(value)
    if text not in ("URGENT", "HIGH", "NORMAL", "LOW"):  # pragma: no cover — 契约已约束
        raise ScenarioMutationError(f"Invalid priority: {text!r}")
    return text  # type: ignore[return-value]


def _as_datetime(value: object):
    """把契约里的 `date`（或已是 `datetime`）归一成 `datetime`（当日 00:00）。"""
    from datetime import date as _date
    from datetime import datetime as _dt

    if isinstance(value, _dt):
        return value
    if isinstance(value, _date):
        return _dt(value.year, value.month, value.day)
    return value
