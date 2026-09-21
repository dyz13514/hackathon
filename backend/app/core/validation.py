"""`Constraint_Validator` —— 确定性硬约束校验器（任务 2.8，R6.1–R6.6）。

design.md Components §3.2 把这个组件的定位写成一句话：**唯一有权判定计划可行性的组件**。
它站在任务 2.1 的冻结快照（`DomainSnapshot`、`available_at`）与任务 2.4 的排产结果
（`PlanCandidate`、`ScheduledJob`）之上，产出 `list[Violation]`——不碰 ORM、不碰 I/O、
不读时钟（时间语义全部来自 `snapshot.now` 与作业自身的 `start_time` / `end_time`）。

## 与 `Scheduling_Core` **刻意独立实现**（design.md §3.2）

校验器不复用排产器的内部函数。除了两个 design.md 点名允许共用的纯函数——
`available_at`（物料可用量语义，R6.3）与 `processing_minutes`（`ceil(base × qty ÷ rate)`
纯算术）——本模块的每一处判定都独立于 `scheduler.py`：不 import `candidate_machines`、
`_best_candidate`、`diagnose_blocking` 之类的内部函数。

这不是重复劳动。校验器的价值正在于「排产器写错了能被抓到，而不是两者一起错」：如果校验
器复用排产器算重叠的那段代码，排产器把重叠算漏了，校验器会以同一个 bug 放行同一个坏计划。
两处独立实现，则任一处的 bug 都会让属性 2（任务 2.9：排产输出满足全部 9 类硬约束）变红。
这是全系统最承重的正确性主张的来源（design.md §3.2）。

## `validate()` 无「快速校验」变体（design.md §3.2、R6.5）

`validate(candidate, snapshot)` 在三个时机被调用：排产后（流水线第 2 步）、提交审批时、
激活前（R6.5）。三次调用**同一个函数**，没有「快速校验」或「增量校验」变体——增量校验会
引入「哪些没变所以不用查」的推断，而那个推断本身可能错，于是激活了一个实际违反硬约束的
计划（K-08 要求 `ACTIVE` 计划零违反）。全量校验每次都跑完 9 个检查，代价是演示规模下
可忽略的毫秒级开销。

## 签名中**刻意不含 `preference_rules`**（任务 11.1 反射断言）

`validate` 的签名只有 `(candidate, snapshot)`。偏好是**软目标**（R7、R18），进
`Objective_Scorer` 的打分，绝不进可行性判定。可行性判定一旦看得见偏好，一条「避免用
CNC-03」的偏好就能让一个物理上完全可行的计划被判为不可行——那是把软约束偷偷升级成硬约束。
任务 11.1 用 `inspect.signature` 反射断言本函数签名不含 `preference_rules`，把这条纪律
钉死在类型层面。（快照里带着 `preference_rules` 字段，但本模块从不读它。）

## 9 类违反的判定各自独立、`validate` 顺序合并

design.md §3.2 的 `CHECKS` 元组固定了 9 个检查的顺序。每个检查是一个纯函数
`(candidate, snapshot) -> list[Violation]`，`validate` 顺序执行并把结果拼接。顺序只影响
`violations` 列表里违反出现的先后，不影响「是否可行」（可行 ⟺ 列表为空），因此顺序是可读性
选择而非语义选择——与排产器 `diagnose_blocking` 的「固定判定顺序」不同，那里顺序决定了报
哪一个原因（因为只报一个），这里 9 个检查各报各的，互不遮蔽。
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.core.scheduler import PlanCandidate, ScheduledJob
from app.core.snapshot import DomainSnapshot, Product, available_at

#: 9 类硬约束违反（R6.1）。取值域与 R6.1 逐字对齐，声明为 `Literal` 使拼写错误在类型层暴露。
ViolationType = Literal[
    "MATERIAL_INSUFFICIENT",
    "MACHINE_UNAVAILABLE",
    "MACHINE_CAPABILITY_MISMATCH",
    "WORKER_UNAVAILABLE",
    "WORKER_SKILL_MISMATCH",
    "MACHINE_DOUBLE_BOOKING",
    "WORKER_DOUBLE_BOOKING",
    "OPERATION_PRECEDENCE_VIOLATION",
    "SHIFT_BOUNDARY_VIOLATION",
]


class Violation(BaseModel):
    """一处硬约束违反（R6.2）。

    `job_ids` / `resource_ids` 是**涉及**该违反的作业与资源 ID——重叠类违反涉及两个作业，
    前序类违反涉及后序作业（`resource_ids` 为空），资源类违反涉及一个作业与一个资源。
    `quantified` 承载可量化的证据（缺口数量、重叠分钟、超出班次的分钟数等），供解释与
    审批界面直接展示，不必二次计算。

    `frozen=True`：违反是校验产出的值对象，一经生成即为事实记录，可变性在此没有用途。
    """

    model_config = ConfigDict(frozen=True)

    violation_type: ViolationType
    job_ids: list[str]
    resource_ids: list[str]
    human_description: str
    quantified: dict[str, Any]


#: 一个检查函数的类型：吃 `(candidate, snapshot)`，吐该类违反的列表（design.md §3.2）。
Check = Callable[[PlanCandidate, DomainSnapshot], list[Violation]]


# --------------------------------------------------------------------------
# 内部工具：分钟数（禁止浮点）
# --------------------------------------------------------------------------


def _overlap_minutes(
    a_start: datetime, a_end: datetime, b_start: datetime, b_end: datetime
) -> int:
    """`[a_start, a_end)` 与 `[b_start, b_end)` 的重叠分钟数，无重叠为 0。

    半开区间语义与 `Timeline` / `TimeWindow` 一致：一段 09:00–10:00 与一段 10:00 开始的
    占用重叠 0 分钟。整分钟，不经浮点（`timedelta.total_seconds()` 是精确的秒数）。
    """
    start = max(a_start, b_start)
    end = min(a_end, b_end)
    if end <= start:
        return 0
    return int((end - start).total_seconds() // 60)


def _minutes_between(start: datetime, end: datetime) -> int:
    """`[start, end)` 的整分钟长度，`end <= start` 截断为 0。"""
    if end <= start:
        return 0
    return int((end - start).total_seconds() // 60)


# --------------------------------------------------------------------------
# 1. 物料充足（MATERIAL_INSUFFICIENT，R6.3 / R6.4）
# --------------------------------------------------------------------------


def _material_need(product: Product, order_quantity: Decimal) -> dict[str, Decimal]:
    """一个订单的物料需求 `Σ line.quantity_per_unit × order_quantity`（单层 BOM，R4 限定）。

    与排产器的 `material_need` 是同一个纯算术公式的**独立抄写**——它不在 design.md §3.2
    允许共用的两个函数（`available_at` / `processing_minutes`）之列，因此此处自行实现，好让
    「排产器把物料需求算错了」也能被校验器抓到。同一物料多行时累加。
    """
    need: dict[str, Decimal] = {}
    for line in product.bom:
        need[line.material_id] = need.get(line.material_id, Decimal("0")) + (
            line.quantity_per_unit * order_quantity
        )
    return need


def check_material_sufficient(
    candidate: PlanCandidate, snapshot: DomainSnapshot
) -> list[Violation]:
    """物料在作业开工时是否足够（R6.3 / R6.4）。

    对每个**已排产订单**，物料在其首道工序（`OP1`）开工时一次性预留（与排产器的语义一致：
    整单在 `sequence=1` 预留）。可用量按 R6.3：

        available_at(material, start_time) = quantity_available − reserved_quantity
                                             + Σ{ d.quantity | d.eta < start_time }

    只把 `eta < start_time`（严格不等）的到货计入——到货与开工同一时刻，物料不算已到。
    多个订单竞争同一物料时，按作业开工时间（tie-break `order_id`）**顺序扣减**：先开工的
    订单先占用，后开工的订单看到的是扣减后的余量。这与排产器逐订单滚动预留的语义一致，但
    实现独立。

    缺料时**只报缺口**（R6.4），`quantified.shortfall_quantity = need − usable`，绝不推断
    补料或建议采购。`resource_ids` 是缺料的 `material_id`，`job_ids` 是该订单被扣料的首道
    工序（真正触发预留的那道）。
    """
    products = snapshot.products_by_id()
    orders = snapshot.orders_by_id()
    materials = snapshot.materials_by_id()

    # 每个已排产订单的「首道工序」= start_time 最早的那个作业（预留发生点）。
    first_job_of_order: dict[str, ScheduledJob] = {}
    for sj in candidate.scheduled_jobs:
        existing = first_job_of_order.get(sj.order_id)
        if existing is None or (sj.start_time, sj.job_id) < (existing.start_time, existing.job_id):
            first_job_of_order[sj.order_id] = sj

    # 按开工时间（tie-break order_id）顺序处理订单，模拟滚动预留。
    ordered = sorted(
        first_job_of_order.values(), key=lambda sj: (sj.start_time, sj.order_id)
    )

    # 本次校验内已被先前订单预留掉的量（叠加在快照 reserved_quantity 之上）。
    consumed: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    violations: list[Violation] = []

    for sj in ordered:
        order = orders.get(sj.order_id)
        product = products.get(sj.product_id)
        if order is None or product is None:
            continue  # 引用完整性由 require_referential_integrity 在排产前保证；此处防御性跳过
        need = _material_need(product, order.quantity)
        for material_id, required in need.items():
            material = materials.get(material_id)
            if material is None:
                # 物料本身不存在：报满额缺口（无可用量可言）。
                violations.append(
                    Violation(
                        violation_type="MATERIAL_INSUFFICIENT",
                        job_ids=[sj.job_id],
                        resource_ids=[material_id],
                        human_description=(
                            f"Material {material_id} required by job {sj.job_id} does not exist in the snapshot; "
                            f"shortfall {required}"
                        ),
                        quantified={
                            "material_id": material_id,
                            "shortfall_quantity": str(required),
                            "required_quantity": str(required),
                            "available_quantity": "0",
                        },
                    )
                )
                continue
            usable = available_at(material, sj.start_time) - consumed[material_id]
            if usable < required:
                shortfall = required - usable
                violations.append(
                    Violation(
                        violation_type="MATERIAL_INSUFFICIENT",
                        job_ids=[sj.job_id],
                        resource_ids=[material_id],
                        human_description=(
                            f"At job {sj.job_id} start, {material_id} has {usable} available, "
                            f"needs {required}, shortfall {shortfall}"
                        ),
                        quantified={
                            "material_id": material_id,
                            "shortfall_quantity": str(shortfall),
                            "required_quantity": str(required),
                            "available_quantity": str(usable),
                            "unit": material.unit,
                        },
                    )
                )
            # 无论是否够，都记为已预留：后续订单看到扣减后的余量（缺料订单也占用它拿得到的）。
            consumed[material_id] += required

    return violations


# --------------------------------------------------------------------------
# 2. 机器可用（MACHINE_UNAVAILABLE，R6.1）
# --------------------------------------------------------------------------


def check_machine_available(
    candidate: PlanCandidate, snapshot: DomainSnapshot
) -> list[Violation]:
    """作业所在机器整机可用且时段可用（status + downtime_windows）。

    两档判定：

    - **整机不可用**：机器 `status ∈ {DOWN, MAINTENANCE}`，或作业引用了快照中不存在的机器；
    - **时段不可用**：机器 `AVAILABLE`，但作业占用 `[start, end)` 落在某个 `downtime_window`
      内，或越出机器的 `[available_start, available_end)` 可用窗。

    停机窗判定用 `machine.is_blocked_during`（与快照模型自带的语义一致，非排产器内部函数）。
    """
    machines = snapshot.machines_by_id()
    violations: list[Violation] = []

    for sj in candidate.scheduled_jobs:
        machine = machines.get(sj.machine_id)
        if machine is None:
            violations.append(
                Violation(
                    violation_type="MACHINE_UNAVAILABLE",
                    job_ids=[sj.job_id],
                    resource_ids=[sj.machine_id],
                    human_description=f"Job {sj.job_id} references a machine {sj.machine_id} that does not exist",
                    quantified={"machine_id": sj.machine_id, "reason": "NOT_FOUND"},
                )
            )
            continue
        if machine.status in ("DOWN", "MAINTENANCE"):
            violations.append(
                Violation(
                    violation_type="MACHINE_UNAVAILABLE",
                    job_ids=[sj.job_id],
                    resource_ids=[sj.machine_id],
                    human_description=(
                        f"Job {sj.job_id} is scheduled on machine {sj.machine_id}, which has status {machine.status}"
                    ),
                    quantified={"machine_id": sj.machine_id, "status": machine.status},
                )
            )
            continue
        if machine.is_blocked_during(sj.start_time, sj.end_time):
            violations.append(
                Violation(
                    violation_type="MACHINE_UNAVAILABLE",
                    job_ids=[sj.job_id],
                    resource_ids=[sj.machine_id],
                    human_description=(
                        f"Job {sj.job_id}'s occupation [{sj.start_time}, {sj.end_time}) "
                        f"falls within a downtime window of machine {sj.machine_id}"
                    ),
                    quantified={"machine_id": sj.machine_id, "reason": "DOWNTIME_WINDOW"},
                )
            )
            continue
        if sj.start_time < machine.available_start or sj.end_time > machine.available_end:
            violations.append(
                Violation(
                    violation_type="MACHINE_UNAVAILABLE",
                    job_ids=[sj.job_id],
                    resource_ids=[sj.machine_id],
                    human_description=(
                        f"Job {sj.job_id}'s occupation [{sj.start_time}, {sj.end_time}) falls outside machine "
                        f"{sj.machine_id}'s available window "
                        f"[{machine.available_start}, {machine.available_end})"
                    ),
                    quantified={
                        "machine_id": sj.machine_id,
                        "reason": "OUTSIDE_AVAILABLE_WINDOW",
                        "available_start": machine.available_start.isoformat(),
                        "available_end": machine.available_end.isoformat(),
                    },
                )
            )

    return violations


# --------------------------------------------------------------------------
# 3. 机器能力（MACHINE_CAPABILITY_MISMATCH，R6.1）
# --------------------------------------------------------------------------


def _operation_of(sj: ScheduledJob, snapshot: DomainSnapshot) -> Any:
    """作业对应的工序定义（按 `product_id` + 从 `job_id` 解析出的 `sequence`）。

    `job_id` 是排产器构造的 `"{order_id}-OP{sequence}"`（design.md §3.1.1）。校验器需要工序的
    `required_machine_type` / `required_capability` / `required_worker_skill` /
    `base_processing_time_per_unit` / `setup_time` 来独立判定资源匹配与班次边界——这些在
    `ScheduledJob` 上没有（它只记「谁在哪台机器什么时候」），因此从快照的产品定义回查。

    解析失败或产品/工序缺失 → `None`（调用方据此跳过，防御性；正常路径由引用完整性保证）。
    """
    product = snapshot.products_by_id().get(sj.product_id)
    if product is None:
        return None
    marker = "-OP"
    idx = sj.job_id.rfind(marker)
    if idx == -1:
        return None
    try:
        sequence = int(sj.job_id[idx + len(marker) :])
    except ValueError:
        return None
    for op in product.operations:
        if op.sequence == sequence:
            return op
    return None


def check_machine_capability(
    candidate: PlanCandidate, snapshot: DomainSnapshot
) -> list[Violation]:
    """作业所在机器的类型与能力是否满足工序要求（R6.1）。

    - `machine.machine_type == operation.required_machine_type`；
    - `operation.required_capability ⊆ machine.capabilities`（`None` 表示无能力要求）。

    机器不存在的情形由 `check_machine_available` 报 `MACHINE_UNAVAILABLE`，此处跳过以免同一
    问题报两类违反。
    """
    machines = snapshot.machines_by_id()
    violations: list[Violation] = []

    for sj in candidate.scheduled_jobs:
        machine = machines.get(sj.machine_id)
        if machine is None:
            continue
        op = _operation_of(sj, snapshot)
        if op is None:
            continue
        if machine.machine_type != op.required_machine_type:
            violations.append(
                Violation(
                    violation_type="MACHINE_CAPABILITY_MISMATCH",
                    job_ids=[sj.job_id],
                    resource_ids=[sj.machine_id],
                    human_description=(
                        f"Job {sj.job_id} requires machine type {op.required_machine_type}, "
                        f"but machine {sj.machine_id} is of type {machine.machine_type}"
                    ),
                    quantified={
                        "machine_id": sj.machine_id,
                        "required_machine_type": op.required_machine_type,
                        "actual_machine_type": machine.machine_type,
                    },
                )
            )
            continue
        if not machine.has_capability(op.required_capability):
            violations.append(
                Violation(
                    violation_type="MACHINE_CAPABILITY_MISMATCH",
                    job_ids=[sj.job_id],
                    resource_ids=[sj.machine_id],
                    human_description=(
                        f"Job {sj.job_id} requires capability {op.required_capability}, "
                        f"but machine {sj.machine_id} does not have it"
                    ),
                    quantified={
                        "machine_id": sj.machine_id,
                        "required_capability": op.required_capability,
                        "machine_capabilities": list(machine.capabilities),
                    },
                )
            )

    return violations


# --------------------------------------------------------------------------
# 4. 工人可用（WORKER_UNAVAILABLE，R6.1）
# --------------------------------------------------------------------------


def check_worker_available(
    candidate: PlanCandidate, snapshot: DomainSnapshot
) -> list[Violation]:
    """作业所在工人当日是否可用（缺勤 absence）。

    - 工人引用了快照中不存在的工人 → `WORKER_UNAVAILABLE`；
    - 作业占用 `[start, end)` 与工人某段缺勤窗相交 → `WORKER_UNAVAILABLE`。

    班次边界（`[shift_start, shift_end)`）不在这里判，交给 `check_shift_boundary`；此处只判
    「登记在案的缺勤」这一档，与排产器把两者分开处理的语义一致但实现独立。
    """
    workers = snapshot.workers_by_id()
    violations: list[Violation] = []

    for sj in candidate.scheduled_jobs:
        worker = workers.get(sj.worker_id)
        if worker is None:
            violations.append(
                Violation(
                    violation_type="WORKER_UNAVAILABLE",
                    job_ids=[sj.job_id],
                    resource_ids=[sj.worker_id],
                    human_description=f"Job {sj.job_id} references a worker {sj.worker_id} that does not exist",
                    quantified={"worker_id": sj.worker_id, "reason": "NOT_FOUND"},
                )
            )
            continue
        if worker.is_absent_during(sj.start_time, sj.end_time):
            violations.append(
                Violation(
                    violation_type="WORKER_UNAVAILABLE",
                    job_ids=[sj.job_id],
                    resource_ids=[sj.worker_id],
                    human_description=(
                        f"Job {sj.job_id}'s occupation [{sj.start_time}, {sj.end_time}) "
                        f"falls within an absence window of worker {sj.worker_id}"
                    ),
                    quantified={"worker_id": sj.worker_id, "reason": "ABSENCE"},
                )
            )

    return violations


# --------------------------------------------------------------------------
# 5. 工人技能（WORKER_SKILL_MISMATCH，R6.1）
# --------------------------------------------------------------------------


def check_worker_skill(
    candidate: PlanCandidate, snapshot: DomainSnapshot
) -> list[Violation]:
    """作业所在工人是否具备工序要求的技能（R6.1）。

    工人不存在由 `check_worker_available` 报，此处跳过。`operation.required_worker_skill ∈
    worker.skills`（`has_skill` 是快照模型自带语义，非排产器内部函数）。
    """
    workers = snapshot.workers_by_id()
    violations: list[Violation] = []

    for sj in candidate.scheduled_jobs:
        worker = workers.get(sj.worker_id)
        if worker is None:
            continue
        op = _operation_of(sj, snapshot)
        if op is None:
            continue
        if not worker.has_skill(op.required_worker_skill):
            violations.append(
                Violation(
                    violation_type="WORKER_SKILL_MISMATCH",
                    job_ids=[sj.job_id],
                    resource_ids=[sj.worker_id],
                    human_description=(
                        f"Job {sj.job_id} requires skill {op.required_worker_skill}, "
                        f"but worker {sj.worker_id} does not have it"
                    ),
                    quantified={
                        "worker_id": sj.worker_id,
                        "required_skill": op.required_worker_skill,
                        "worker_skills": list(worker.skills),
                    },
                )
            )

    return violations


# --------------------------------------------------------------------------
# 6. 机器不重叠（MACHINE_DOUBLE_BOOKING，含换型占用区间，R6.1）
# --------------------------------------------------------------------------


def _pairwise_overlaps(
    jobs_by_resource: dict[str, list[ScheduledJob]],
    violation_type: ViolationType,
    resource_label: str,
) -> list[Violation]:
    """同一资源上任意两个作业的占用区间两两比较，重叠即报（通用于机器与工人）。

    `[start_time, end_time)` 是**含换型在内**的整段占用（`end_time = start + setup_minutes +
    duration`，见 `ScheduledJob` docstring）——因此换型占用区间天然被纳入重叠判定，无需
    额外为换型开一个区间。半开区间语义：09:00–10:00 与 10:00–11:00 不重叠。

    每对作业按 `(job_id_a, job_id_b)` 升序去重报一次，`quantified.overlap_minutes` 给出
    重叠分钟数。O(n²)：演示规模（单机几十个作业）下可忽略，且独立于排产器的时间线结构。
    """
    violations: list[Violation] = []
    for resource_id, jobs in sorted(jobs_by_resource.items()):
        ordered = sorted(jobs, key=lambda sj: (sj.start_time, sj.job_id))
        for i in range(len(ordered)):
            for j in range(i + 1, len(ordered)):
                a, b = ordered[i], ordered[j]
                minutes = _overlap_minutes(
                    a.start_time, a.end_time, b.start_time, b.end_time
                )
                if minutes > 0:
                    violations.append(
                        Violation(
                            violation_type=violation_type,
                            job_ids=[a.job_id, b.job_id],
                            resource_ids=[resource_id],
                            human_description=(
                                f"Jobs {a.job_id} and {b.job_id} overlap by {minutes} minutes "
                                f"on {resource_label} {resource_id}"
                            ),
                            quantified={
                                "overlap_minutes": minutes,
                                "resource_id": resource_id,
                            },
                        )
                    )
    return violations


def check_machine_no_overlap(
    candidate: PlanCandidate, snapshot: DomainSnapshot
) -> list[Violation]:
    """同一台机器上不得有两个作业占用区间重叠（含换型，R6.1）。"""
    by_machine: dict[str, list[ScheduledJob]] = defaultdict(list)
    for sj in candidate.scheduled_jobs:
        by_machine[sj.machine_id].append(sj)
    return _pairwise_overlaps(by_machine, "MACHINE_DOUBLE_BOOKING", "machine")


# --------------------------------------------------------------------------
# 7. 工人不重叠（WORKER_DOUBLE_BOOKING，R6.1）
# --------------------------------------------------------------------------


def check_worker_no_overlap(
    candidate: PlanCandidate, snapshot: DomainSnapshot
) -> list[Violation]:
    """同一个工人不得同时被排在两个作业上（R6.1）。"""
    by_worker: dict[str, list[ScheduledJob]] = defaultdict(list)
    for sj in candidate.scheduled_jobs:
        by_worker[sj.worker_id].append(sj)
    return _pairwise_overlaps(by_worker, "WORKER_DOUBLE_BOOKING", "worker")


# --------------------------------------------------------------------------
# 8. 工序前后序（OPERATION_PRECEDENCE_VIOLATION，R4.3）
# --------------------------------------------------------------------------


def check_operation_precedence(
    candidate: PlanCandidate, snapshot: DomainSnapshot
) -> list[Violation]:
    """后序作业的 `start_time` 不得早于前序作业的 `end_time`（R4.3）。

    前后序关系由 `job_id` 的 `"{order_id}-OP{sequence}"` 结构独立重建：同一订单内 `sequence`
    相邻的两道工序即前后序。**不复用**排产器 `expand` 出的 `predecessor_job_id`——那正是要
    校验的对象，用它来校验它自己就成了同义反复。

    只对**都已排产**的前后序对判定：若前序未排产，后序不该出现在 `scheduled_jobs` 里
    （排产器订单原子性保证），但校验器不假设这一点；缺了前序则此处无从比较，交给排产结果的
    其他不变量。
    """
    # 按订单聚合已排产作业，解析出 (sequence, ScheduledJob)。
    by_order: dict[str, list[tuple[int, ScheduledJob]]] = defaultdict(list)
    for sj in candidate.scheduled_jobs:
        op = _operation_of(sj, snapshot)
        if op is None:
            continue
        by_order[sj.order_id].append((op.sequence, sj))

    violations: list[Violation] = []
    for _order_id, items in sorted(by_order.items()):
        ordered = sorted(items, key=lambda pair: pair[0])
        for idx in range(1, len(ordered)):
            _prev_seq, prev_sj = ordered[idx - 1]
            _cur_seq, cur_sj = ordered[idx]
            if cur_sj.start_time < prev_sj.end_time:
                gap = _minutes_between(cur_sj.start_time, prev_sj.end_time)
                violations.append(
                    Violation(
                        violation_type="OPERATION_PRECEDENCE_VIOLATION",
                        job_ids=[prev_sj.job_id, cur_sj.job_id],
                        resource_ids=[],
                        human_description=(
                            f"Successor job {cur_sj.job_id} starts at {cur_sj.start_time}, "
                            f"before predecessor job {prev_sj.job_id} ends at {prev_sj.end_time}"
                        ),
                        quantified={
                            "predecessor_job_id": prev_sj.job_id,
                            "successor_job_id": cur_sj.job_id,
                            "overlap_minutes": gap,
                        },
                    )
                )

    return violations


# --------------------------------------------------------------------------
# 9. 班次边界（SHIFT_BOUNDARY_VIOLATION，R4.6）
# --------------------------------------------------------------------------


def check_shift_boundary(
    candidate: PlanCandidate, snapshot: DomainSnapshot
) -> list[Violation]:
    """作业不得跨越所在工人的班次边界（R4.6）。

    作业占用 `[start_time, end_time)` 必须完全落在工人 `[shift_start, shift_end)` 内：
    `start_time >= shift_start` 且 `end_time <= shift_end`。工人不存在由
    `check_worker_available` 报，此处跳过。

    `quantified` 报超出边界的分钟数：早于 `shift_start` 的部分 + 晚于 `shift_end` 的部分。
    班次判定只比较作业占用 `[start, end)` 与班次窗，不依赖排产器算出的 `setup_minutes`——
    这样即便排产器把 `setup_minutes` 算错、`end_time` 却仍落在班次内，本检查也不会误报。
    """
    workers = snapshot.workers_by_id()
    violations: list[Violation] = []

    for sj in candidate.scheduled_jobs:
        worker = workers.get(sj.worker_id)
        if worker is None:
            continue
        before = _minutes_between(sj.start_time, worker.shift_start)  # start 早于 shift_start
        after = _minutes_between(worker.shift_end, sj.end_time)  # end 晚于 shift_end
        if before > 0 or after > 0:
            violations.append(
                Violation(
                    violation_type="SHIFT_BOUNDARY_VIOLATION",
                    job_ids=[sj.job_id],
                    resource_ids=[sj.worker_id],
                    human_description=(
                        f"Job {sj.job_id}'s occupation [{sj.start_time}, {sj.end_time}) falls outside worker "
                        f"{sj.worker_id}'s shift [{worker.shift_start}, {worker.shift_end})"
                    ),
                    quantified={
                        "worker_id": sj.worker_id,
                        "minutes_before_shift": before,
                        "minutes_after_shift": after,
                        "shift_start": worker.shift_start.isoformat(),
                        "shift_end": worker.shift_end.isoformat(),
                    },
                )
            )

    return violations


# --------------------------------------------------------------------------
# 聚合（design.md §3.2 的 CHECKS 元组）
# --------------------------------------------------------------------------

#: 9 个独立检查，顺序与 design.md §3.2 一致。`validate` 顺序执行并合并结果。
CHECKS: tuple[Check, ...] = (
    check_material_sufficient,  # MATERIAL_INSUFFICIENT
    check_machine_available,  # MACHINE_UNAVAILABLE（status + downtime_windows）
    check_machine_capability,  # MACHINE_CAPABILITY_MISMATCH
    check_worker_available,  # WORKER_UNAVAILABLE（absence）
    check_worker_skill,  # WORKER_SKILL_MISMATCH
    check_machine_no_overlap,  # MACHINE_DOUBLE_BOOKING（含换型占用区间）
    check_worker_no_overlap,  # WORKER_DOUBLE_BOOKING
    check_operation_precedence,  # OPERATION_PRECEDENCE_VIOLATION（R4.3）
    check_shift_boundary,  # SHIFT_BOUNDARY_VIOLATION（R4.6）
)


class ValidationReport(BaseModel):
    """一次完整校验的结果。`is_feasible ⟺ violations 为空`（R6.1、R6.6）。

    单独立一个报告类型而不是裸返回 `list[Violation]`：审批闸门（任务 3.1）与激活前重校验
    （R6.5）需要一个「可行/不可行」的布尔判定，把它算在这里一次，避免每个调用点各自
    `len(violations) == 0` 地重写同一句话（而某处写成 `> 0` 就是一个静默的激活坏计划）。
    """

    model_config = ConfigDict(frozen=True)

    is_feasible: bool
    violations: tuple[Violation, ...] = Field(default_factory=tuple)


def validate(candidate: PlanCandidate, snapshot: DomainSnapshot) -> ValidationReport:
    """对已排产部分运行全部 9 个硬约束检查，合并违反（R6.1–R6.6）。

    **无「快速校验」变体**：三个时机（排产后、提交审批、激活前）调用同一个函数（R6.5）。
    **签名不含 `preference_rules`**：偏好是软目标，绝不进可行性判定（任务 11.1 反射断言）。

    只校验 `candidate.scheduled_jobs`——`unschedulable_jobs` 本就没有排入计划，对它们谈
    「机器重叠」「班次越界」没有意义。可行 ⟺ 全部 9 个检查都返回空列表。
    """
    violations: list[Violation] = []
    for check in CHECKS:
        violations.extend(check(candidate, snapshot))
    return ValidationReport(is_feasible=not violations, violations=tuple(violations))
