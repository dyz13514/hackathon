"""`Replanner` —— 确定性重排与计划稳定性（任务 7.1，R9.5–R9.7 / R11.5 / R6.5）。

design.md Components §3.5 把这个组件的定位写成一句话：**在扰动发生后，尽量少动地把
`ACTIVE` 计划改成一份仍然可行的修订计划**。它站在任务 2.4 的排产器
（`generate_schedule`，已实现 `freeze` / `locked` / `exclude_machine_ids` 三个入参）、
任务 2.8 的校验器（`validate`）与任务 2.1 的冻结快照之上，产出值对象——不碰 ORM、不碰
I/O、不读时钟。它只 import `app.core.*`，因此天然满足分层规则第 ① 条
（`tests/structure/test_layering.py`）。

## `replan` 的五步（design.md §3.5 伪代码）

    1. 受影响集   affected = affected_by(disruption, active_plan, snapshot)
    2. 冻结集     freeze   = {未受影响且 job_level_still_valid 的作业}
                  locked 交互：被锁且受影响/已不可行 → LOCKED_JOB_INFEASIBLE，硬排除
    3. 重排       candidate = generate_schedule(snapshot, freeze=, locked=, exclude=)
    4. 全量校验   report = validate(candidate, snapshot)      （不是增量校验，R6.5）
    5. delta      compute_plan_delta（任务 7.2）——本任务不计算，留给消费方

## 冻结先于优化——它是 K-05 的实现手段，不是性能优化（design.md §3.5）

不受扰动影响、且资源与物料仍然可行的作业**不参与重排**，因此天然不产生 churn
（`churn_ratio ≤ 0.20`，K-05）。代价是解质量可能次优——这是 requirements 明确接受的
取舍：计划稳定性对车间的价值高于最优性。所以冻结**先于**优化：先把该冻的冻住，再让排产器
在剩下的自由度里找最优，而不是先求全局最优再看动了多少。

## 冻结的粒度是**订单**，不是单个作业（与排产器的原子性一致）

design.md 的伪代码按 `ScheduledJob` 逐个判冻结，但任务 2.4 的排产器把**订单**当原子单位：
一个订单要么整体冻结、要么整体重排（`generate_schedule` 跳过任何含冻结作业的订单）。若在
这里冻结一个订单的 OP1、却让被 successor 传播波及的 OP2/OP3 去重排，排产器会因为该订单已
有冻结作业而**整单跳过**——OP2/OP3 既不冻结也不重排，静默消失。这正是 R11.5「绝不悄悄
移动/丢弃作业」要防的事。

因此本模块在**订单粒度**上决定冻结：一个订单的全部作业都未受影响且仍可行，才整单冻结；
只要该订单有任一作业受影响，整单都进重排。`affected_by` 的 successor 传播已经把「同订单里
排在受影响工序之后的工序」纳入 affected；订单粒度的冻结把「排在**之前**的工序」也一并带进
重排，从而消除静默丢弃的窗口。这是对伪代码的**收紧**，不是偏离：伪代码的 `job ∉ affected`
在订单粒度上表达为「订单内无一作业 ∈ affected」。

## `locked_job_ids`：绝不悄悄移动被锁作业（R11.5、design.md §3.5）

`MODIFY` 的 `LOCK_JOB` 在 `scheduled_jobs.locked = true`，被本模块消费。规则：

- 被锁作业**未受影响且仍可行** → 强制冻结（优先级高于优化，即便重排能给它找到更优位置也
  不动它）；
- 被锁作业**受影响或已不可行** → 既不冻结也不重排，进 `unschedulable` 并发
  `LOCKED_JOB_INFEASIBLE`，等待规划员解锁。**绝不悄悄给它换个位置。**

被锁作业强制冻结会把它所属订单整单冻结（订单粒度）；被锁且不可行则把该订单整单硬排除
（从重排的候选订单里剔除，其全部作业记 `LOCKED_JOB_INFEASIBLE`）。

## 五类扰动的受影响集（`affected_by`，R9.5–R9.7）

- `MACHINE_BREAKDOWN`  → 落在故障机器故障时间窗内的 `ScheduledJob`。替代机器的搜索由
  排产器承担：把故障机器放进 `exclude_machine_ids`，排产器在剩余候选里自动找具备所需
  `capabilities` 的替代机（R9.5）；无替代时该作业进 `unschedulable`（排产器的常规诊断），
  本模块据此在结果里明确报告「无替代机器」。
- `WORKER_UNAVAILABLE` → 该工人的全部 `ScheduledJob`（R9 第 1 条）。
- `MATERIAL_SHORTAGE` / `MATERIAL_DELAY` → 消耗该物料的订单的全部工序（R9.7）。物料类扰动
  只依据实际库存与到货时间重排（快照已反映扰动后的库存/ETA），不假设任何未登记的补料。
- `URGENT_ORDER`       → ∅（新订单只是新增作业；既有作业不因它受影响）。加急订单的插入由
  排产器在重排时完成——新订单进 snapshot，排产器按优先级全序把它排进去，被它挤后的作业
  表现为 delta 里的 `moved`（R9.6，被推迟作业及其订单影响由任务 7.2 的 delta 量化）。
- **successor 传播**：affected 中任一作业的所有后序工序（同订单内 `sequence` 更大者）也进
  affected——前序动了，后序不可能原样冻结。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

from app.core.scheduler import (
    PlanCandidate,
    ScheduledJob,
    UnschedulableJob,
    generate_schedule,
    material_need,
)
from app.core.snapshot import (
    DomainSnapshot,
    TimeWindow,
    available_at,
)
from app.core.validation import ValidationReport, validate

#: 5 类扰动（R9.1）。取值域与 design.md `RegisterDisruptionIn.type` 逐字对齐，声明为
#: `Literal` 使拼写错误在类型层暴露。
DisruptionType = Literal[
    "URGENT_ORDER",
    "MACHINE_BREAKDOWN",
    "MATERIAL_SHORTAGE",
    "WORKER_UNAVAILABLE",
    "MATERIAL_DELAY",
]

#: 被锁且受影响/不可行作业的解封提示原因（design.md §3.5、R11.5）。它不是 R6.1 的 9 类
#: 硬约束违反之一——它是「等待规划员解锁」的计划稳定性信号，因此单列一个常量。
LOCKED_JOB_INFEASIBLE = "LOCKED_JOB_INFEASIBLE"


# --------------------------------------------------------------------------
# 扰动模型（核心层自有：内核不能 import tools/api，因此判别联合在此定义）
# --------------------------------------------------------------------------


class Disruption(BaseModel):
    """一次扰动的结构化描述（R9.1）。

    这是**内核层**的扰动模型，只承载 `affected_by` / `replan` 需要的字段——不含登记时间、
    来源、`trace_id` 之类的持久化元数据（那些在任务 7.4 的 `disruptions` 表与
    `RegisterDisruptionIn` 里）。API 边界的判别联合在任务 7.4 落地时映射到本模型。

    各字段按扰动类型取用，未用到的留空（`None` / 空元组）：

    - `MACHINE_BREAKDOWN` → `machine_id` + `fault_window`（故障时间窗）；
    - `WORKER_UNAVAILABLE` → `worker_id`；
    - `MATERIAL_SHORTAGE` / `MATERIAL_DELAY` → `material_id`；
    - `URGENT_ORDER` → 无额外字段（新订单已进 snapshot）。

    `frozen=True`：扰动是重排的输入事实，一经登记即不可变。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    type: DisruptionType
    machine_id: str | None = None
    fault_window: TimeWindow | None = None
    worker_id: str | None = None
    material_id: str | None = None

    def excluded_machine_ids(self) -> frozenset[str]:
        """重排时须从候选机器集排除的机器（`MACHINE_BREAKDOWN` 的故障机）。

        排产器在剩余候选里自动搜索具备所需 `capabilities` 的替代机（R9.5）——排除故障机后，
        `candidate_machines` 只保留能力覆盖、状态可用的机器，因此「找替代机」不是本模块的
        显式搜索，而是排产器的常规候选枚举在故障机被剔除后的自然结果。
        """
        if self.type == "MACHINE_BREAKDOWN" and self.machine_id is not None:
            return frozenset({self.machine_id})
        return frozenset()


# --------------------------------------------------------------------------
# 重排结果
# --------------------------------------------------------------------------


class ReplanResult(BaseModel):
    """一次重排的完整结果（design.md §3.5 的 `replan` 返回）。

    `candidate` 是重排后的计划（含冻结作业原样保留 + 重排出的新位置 + 无法排产的作业）。
    `report` 是对 `candidate` 的**全量**校验结果（R6.5，不是增量校验）。`affected_job_ids`
    与 `frozen_job_ids` 是本次重排的两个关键集合，供解释与 `ImpactAnalysis` 展示；
    `locked_infeasible_job_ids` 是被锁却受影响/不可行、进入 `unschedulable` 等待解锁的作业
    （R11.5）。

    `substitute_unavailable_job_ids`：`MACHINE_BREAKDOWN` 下，故障机上的作业在排除故障机后
    仍找不到替代机而进 `unschedulable` 的作业——据此明确报告「无替代机器」（R9.5）。

    delta 与 `churn_ratio` **不在此计算**：它们是任务 7.2 的 `compute_plan_delta` 的产物，
    由消费方对 `active_plan` 与 `candidate` 计算。本任务只交付受影响集、冻结集与锁定交互。
    """

    model_config = ConfigDict(frozen=True)

    candidate: PlanCandidate
    report: ValidationReport
    affected_job_ids: tuple[str, ...]
    frozen_job_ids: tuple[str, ...]
    locked_infeasible_job_ids: tuple[str, ...]
    substitute_unavailable_job_ids: tuple[str, ...]


# --------------------------------------------------------------------------
# 受影响集（R9.5–R9.7、design.md §3.5）
# --------------------------------------------------------------------------


def _sequence_of(job_id: str) -> int | None:
    """从 `"{order_id}-OP{sequence}"` 解析出工序号（解析失败返回 `None`）。

    与校验器 `_operation_of` 的解析逻辑一致：successor 传播需要知道同订单内工序的先后，而
    `ScheduledJob` 只带 `job_id`，工序号编码在其中。
    """
    marker = "-OP"
    idx = job_id.rfind(marker)
    if idx == -1:
        return None
    try:
        return int(job_id[idx + len(marker) :])
    except ValueError:
        return None


def _consumers_of_material(material_id: str, snapshot: DomainSnapshot) -> frozenset[str]:
    """消耗 `material_id` 的订单 ID 集合（R9.7）。

    单层 BOM（R4 限定）：一个订单消耗某物料，当且仅当其产品的 BOM 有一行指向该物料。
    """
    products = snapshot.products_by_id()
    order_ids: set[str] = set()
    for order in snapshot.orders:
        product = products.get(order.product_id)
        if product is None:
            continue
        if any(line.material_id == material_id for line in product.bom):
            order_ids.add(order.order_id)
    return frozenset(order_ids)


def _propagate_successors(
    seed: set[str], active_plan: PlanCandidate
) -> frozenset[str]:
    """把 `seed` 中每个作业的**后序工序**（同订单内 `sequence` 更大者）并入受影响集。

    前序工序动了，后序工序不可能原样冻结（它的开工时刻依赖前序结束）。因此沿前后序链把
    后序纳入 affected（design.md §3.5「传播」）。前序工序**不**被牵连——一个订单的 OP2 因
    机器故障受影响时，OP1 本身仍可原样保留；但订单粒度的冻结会因 OP2 受影响而让整单进重排
    （见模块 docstring），这与「后序传播」是两件互补的事，不在这里合并。
    """
    by_order: dict[str, list[ScheduledJob]] = {}
    for sj in active_plan.scheduled_jobs:
        by_order.setdefault(sj.order_id, []).append(sj)

    affected = set(seed)
    for sj in active_plan.scheduled_jobs:
        if sj.job_id not in seed:
            continue
        seed_seq = _sequence_of(sj.job_id)
        if seed_seq is None:
            continue
        for other in by_order.get(sj.order_id, ()):
            other_seq = _sequence_of(other.job_id)
            if other_seq is not None and other_seq > seed_seq:
                affected.add(other.job_id)
    return frozenset(affected)


def affected_by(
    disruption: Disruption, active_plan: PlanCandidate, snapshot: DomainSnapshot
) -> frozenset[str]:
    """扰动直接影响的作业 `job_id` 集合（含 successor 传播，R9.5–R9.7、design.md §3.5）。

    - `MACHINE_BREAKDOWN`  → 该故障机上、且占用与 `fault_window` 相交的作业；
    - `WORKER_UNAVAILABLE` → 该工人的全部作业；
    - `MATERIAL_SHORTAGE` / `MATERIAL_DELAY` → 消耗该物料的订单的全部工序；
    - `URGENT_ORDER`       → ∅（新订单只是新增作业，既有作业不受影响）；
    - 全部类型：affected 中任一作业的后序工序也并入 affected。

    纯函数，返回排序后的 `frozenset`（顺序无关，供确定性断言）。
    """
    seed: set[str] = set()

    if disruption.type == "MACHINE_BREAKDOWN" and disruption.machine_id is not None:
        window = disruption.fault_window
        for sj in active_plan.scheduled_jobs:
            if sj.machine_id != disruption.machine_id:
                continue
            # 无故障窗（整机不可用）→ 该机上全部作业受影响；有窗则只取占用与窗相交者。
            if window is None or window.overlaps(sj.start_time, sj.end_time):
                seed.add(sj.job_id)

    elif disruption.type == "WORKER_UNAVAILABLE" and disruption.worker_id is not None:
        for sj in active_plan.scheduled_jobs:
            if sj.worker_id == disruption.worker_id:
                seed.add(sj.job_id)

    elif disruption.type in ("MATERIAL_SHORTAGE", "MATERIAL_DELAY") and (
        disruption.material_id is not None
    ):
        consumer_orders = _consumers_of_material(disruption.material_id, snapshot)
        for sj in active_plan.scheduled_jobs:
            if sj.order_id in consumer_orders:
                seed.add(sj.job_id)

    # URGENT_ORDER → seed 保持空集。

    return _propagate_successors(seed, active_plan)


# --------------------------------------------------------------------------
# 作业级仍然可行（job_level_still_valid，design.md §3.5「资源仍可用、物料仍够」）
# --------------------------------------------------------------------------


def job_level_still_valid(sj: ScheduledJob, snapshot: DomainSnapshot) -> bool:
    """作业 `sj` 在扰动后的快照上是否仍能原样冻结（资源仍可用、物料仍够）。

    冻结一个作业等于断言「它的既有位置在新快照下依然成立」。因此逐条检查它当初依赖的资源
    是否仍然可用——若资源已因扰动消失，冻结它只会在重排后被全量校验判为违反，还不如一开始
    就不冻、让它进重排。检查项（与校验器同源语义，实现独立）：

    - 机器仍存在、状态可用（非 `DOWN` / `MAINTENANCE`）、占用不落在停机窗内、不越出可用窗；
    - 工人仍存在、占用不落在缺勤窗内、不越出班次；
    - 物料仍够（对首道工序 `OP1` 检查整单需求，与排产器「首道工序一次性预留」的语义一致）。

    物料检查只对 `OP1` 做：整单在首道工序一次性预留（design.md §3.1.5），后续工序不再单独
    占料。非首道工序的物料充分性由其订单的 OP1 代表。
    """
    machines = snapshot.machines_by_id()
    workers = snapshot.workers_by_id()

    machine = machines.get(sj.machine_id)
    if machine is None:
        return False
    if machine.status in ("DOWN", "MAINTENANCE"):
        return False
    if machine.is_blocked_during(sj.start_time, sj.end_time):
        return False
    if sj.start_time < machine.available_start or sj.end_time > machine.available_end:
        return False

    worker = workers.get(sj.worker_id)
    if worker is None:
        return False
    if worker.is_absent_during(sj.start_time, sj.end_time):
        return False
    if sj.start_time < worker.shift_start or sj.end_time > worker.shift_end:
        return False

    # 物料只对首道工序检查（整单在 OP1 一次性预留，design.md §3.1.5）。
    return _sequence_of(sj.job_id) != 1 or _material_still_sufficient(sj, snapshot)


def _material_still_sufficient(sj: ScheduledJob, snapshot: DomainSnapshot) -> bool:
    """首道工序 `sj` 所属订单的物料，在其开工时刻是否仍足够（R9.7、R6.3）。

    用 `available_at(material, sj.start_time)`（严格 `eta < start_time`）独立复算——不考虑本次
    重排的滚动预留（冻结的是**既有** `ACTIVE` 计划里已成立的位置，其物料当初就已被这道工序
    占用；这里只问「扰动后的快照里，这批料是否还在」）。物料不存在或不足 → 不可冻结。
    """
    products = snapshot.products_by_id()
    orders = snapshot.orders_by_id()
    materials = snapshot.materials_by_id()

    order = orders.get(sj.order_id)
    product = products.get(sj.product_id)
    if order is None or product is None:
        return False

    need = material_need(product, order.quantity)
    for material_id, required in need.items():
        material = materials.get(material_id)
        if material is None:
            return False
        if available_at(material, sj.start_time) < required:
            return False
    return True


# --------------------------------------------------------------------------
# 冻结集与锁定作业交互（design.md §3.5 第 2 步）
# --------------------------------------------------------------------------


def _order_id_of(job_id: str, active_plan: PlanCandidate) -> str | None:
    for sj in active_plan.scheduled_jobs:
        if sj.job_id == job_id:
            return sj.order_id
    return None


class _FreezePlan(BaseModel):
    """冻结集计算的中间结果：哪些作业冻结、哪些被锁作业不可行、哪些订单硬排除。"""

    model_config = ConfigDict(frozen=True)

    frozen_jobs: tuple[ScheduledJob, ...]
    locked_infeasible_job_ids: tuple[str, ...]
    hard_excluded_order_ids: frozenset[str]


def _compute_freeze(
    active_plan: PlanCandidate,
    affected: frozenset[str],
    snapshot: DomainSnapshot,
    locked_job_ids: frozenset[str],
) -> _FreezePlan:
    """按 design.md §3.5 第 2 步计算冻结集与锁定交互（订单粒度，见模块 docstring）。

    步骤：

    1. **订单粒度的候选冻结**：一个订单的全部作业都不在 `affected` 且 `job_level_still_valid`，
       该订单整单进候选冻结集；只要有一作业受影响或已不可行，整单进重排（不冻结）。
    2. **locked 交互**：遍历 `locked_job_ids`——
       - 被锁作业受影响或已不可行 → 记 `LOCKED_JOB_INFEASIBLE`，其**整个订单**硬排除
         （从重排候选里剔除，绝不悄悄移动被锁作业，R11.5）；同时若该订单在候选冻结集里，
         移出（它不能被冻结——被锁作业本身不可行）；
       - 被锁作业未受影响且仍可行 → 强制冻结其整个订单（优先级高于优化，design.md §3.5）。

    订单粒度保证：被冻结/硬排除的订单，其全部作业要么都冻结、要么都硬排除，不会出现「半个
    订单被冻、另半个静默消失」。
    """
    by_order: dict[str, list[ScheduledJob]] = {}
    for sj in active_plan.scheduled_jobs:
        by_order.setdefault(sj.order_id, []).append(sj)

    # ---- 1. 订单粒度的候选冻结 ----
    freezable_orders: set[str] = set()
    for order_id, jobs in by_order.items():
        if all(
            job.job_id not in affected and job_level_still_valid(job, snapshot)
            for job in jobs
        ):
            freezable_orders.add(order_id)

    # ---- 2. locked 交互 ----
    locked_infeasible: list[str] = []
    hard_excluded_orders: set[str] = set()
    force_freeze_orders: set[str] = set()

    for locked_id in sorted(locked_job_ids):
        locked_order_id = _order_id_of(locked_id, active_plan)
        if locked_order_id is None:
            # 被锁作业不在 ACTIVE 计划里（例如已被移出）：无从冻结或重排，记为不可行。
            locked_infeasible.append(locked_id)
            continue
        sj = next(s for s in by_order[locked_order_id] if s.job_id == locked_id)
        is_infeasible = locked_id in affected or not job_level_still_valid(sj, snapshot)
        if is_infeasible:
            # 被锁却受影响/不可行：既不冻结也不重排，整单硬排除，等待解锁（R11.5）。
            locked_infeasible.append(locked_id)
            hard_excluded_orders.add(locked_order_id)
            freezable_orders.discard(locked_order_id)
        else:
            # 被锁且仍可行：强制冻结整单（优先级高于优化）。
            force_freeze_orders.add(locked_order_id)

    # 硬排除优先于强制冻结（同一订单不可能既硬排除又强制冻结，因为二者的判据互斥；
    # 但 force_freeze 可能来自订单里另一个可行的被锁作业——硬排除是整单级，胜出）。
    freeze_orders = (freezable_orders | force_freeze_orders) - hard_excluded_orders

    frozen_jobs = tuple(
        sj
        for order_id in sorted(freeze_orders)
        for sj in sorted(by_order[order_id], key=lambda s: s.job_id)
    )
    return _FreezePlan(
        frozen_jobs=frozen_jobs,
        locked_infeasible_job_ids=tuple(sorted(set(locked_infeasible))),
        hard_excluded_order_ids=frozenset(hard_excluded_orders),
    )


# --------------------------------------------------------------------------
# 主入口（design.md §3.5 的 replan）
# --------------------------------------------------------------------------


def replan(
    active_plan: PlanCandidate,
    disruption: Disruption,
    snapshot: DomainSnapshot,
    locked_job_ids: frozenset[str] = frozenset(),
) -> ReplanResult:
    """在扰动后的快照上产出一份修订计划（design.md §3.5、R9.5–R9.7 / R11.5 / R6.5）。

    五步见模块 docstring。返回 `ReplanResult`——纯函数，不碰 I/O，同输入两次调用结果逐字段
    相同（继承排产器的确定性，属性 1）。

    `snapshot` 必须**已反映扰动后的状态**：机器 `DOWN`、工人缺勤、物料库存/ETA 已更新、
    加急订单已加入。本模块不改快照（它是冻结的），只据它重排。这与 R9.7「只依据实际库存与
    到货时间重排、不假设未登记补料」一致——物料语义全部走 `available_at`，看到的就是快照里
    登记的量。
    """
    # ---- 1. 受影响集 ----
    affected = affected_by(disruption, active_plan, snapshot)

    # ---- 2. 冻结集 + locked 交互 ----
    freeze_plan = _compute_freeze(active_plan, affected, snapshot, locked_job_ids)

    # ---- 3. 重排 ----
    # 硬排除的订单不参与重排：把它们的作业从快照的订单里剔除，排产器就不会去排它们。
    # 冻结作业经 `freeze` 入参原样保留并占住时间线/物料；故障机经 `exclude_machine_ids` 排除。
    replan_snapshot = _snapshot_excluding_orders(
        snapshot, freeze_plan.hard_excluded_order_ids
    )
    candidate = generate_schedule(
        replan_snapshot,
        freeze=freeze_plan.frozen_jobs,
        locked=locked_job_ids,
        exclude_machine_ids=disruption.excluded_machine_ids(),
    )

    # 被锁且不可行的作业进 unschedulable，发 LOCKED_JOB_INFEASIBLE（R11.5）——它们的订单已从
    # 重排快照里剔除，因此不会出现在 candidate 里，需在此显式补进 unschedulable。
    candidate = _append_locked_infeasible(
        candidate, freeze_plan, active_plan
    )

    # ---- 4. 全量校验（R6.5，不是增量校验） ----
    report = validate(candidate, snapshot)

    # ---- 无替代机器的报告（R9.5）：受影响作业排除故障机后仍不可排 ----
    substitute_unavailable = _substitute_unavailable_jobs(candidate, disruption, affected)

    return ReplanResult(
        candidate=candidate,
        report=report,
        affected_job_ids=tuple(sorted(affected)),
        frozen_job_ids=tuple(sorted(sj.job_id for sj in freeze_plan.frozen_jobs)),
        locked_infeasible_job_ids=freeze_plan.locked_infeasible_job_ids,
        substitute_unavailable_job_ids=substitute_unavailable,
    )


def _snapshot_excluding_orders(
    snapshot: DomainSnapshot, excluded_order_ids: frozenset[str]
) -> DomainSnapshot:
    """去掉 `excluded_order_ids` 的订单后的快照副本（用于把硬排除订单排除出重排）。

    经 `model_copy(update=...)` 得到变体——快照冻结，原件不受影响（沙箱第 1 层隔离的同一
    机制）。空排除集时原样返回，避免无谓的深拷贝。
    """
    if not excluded_order_ids:
        return snapshot
    kept = tuple(o for o in snapshot.orders if o.order_id not in excluded_order_ids)
    return snapshot.model_copy(update={"orders": kept})


def _append_locked_infeasible(
    candidate: PlanCandidate, freeze_plan: _FreezePlan, active_plan: PlanCandidate
) -> PlanCandidate:
    """把被锁且不可行的作业补进 `candidate.unschedulable_jobs`，发 `LOCKED_JOB_INFEASIBLE`。

    这些作业的订单已从重排快照里剔除，因此既不在 `scheduled_jobs` 也不在 `unschedulable_jobs`
    里。R8.4 要求每个作业都有归属——它们的归属是「等待解锁」，故显式补进 unschedulable，
    `blocking_reason = LOCKED_JOB_INFEASIBLE`，绝不悄悄移动（R11.5）。

    补的是**整个订单**的作业（订单粒度硬排除），不只是被锁的那道工序——半个订单在车间里
    是负价值，且规划员解锁后要能重排整单。
    """
    if not freeze_plan.hard_excluded_order_ids:
        return candidate

    locked_set = set(freeze_plan.locked_infeasible_job_ids)
    additions: list[UnschedulableJob] = []
    for sj in active_plan.scheduled_jobs:
        if sj.order_id not in freeze_plan.hard_excluded_order_ids:
            continue
        additions.append(
            UnschedulableJob(
                job_id=sj.job_id,
                order_id=sj.order_id,
                blocking_reason=LOCKED_JOB_INFEASIBLE,
                unblock_suggestion={
                    "locked_job_id": sj.job_id if sj.job_id in locked_set else None,
                    "reason": "Job is locked and affected by this disruption or already infeasible; awaiting planner unlock before rescheduling",
                    "awaiting_unlock": True,
                },
            )
        )

    merged = tuple(candidate.unschedulable_jobs) + tuple(additions)
    return candidate.model_copy(update={"unschedulable_jobs": merged})


def _substitute_unavailable_jobs(
    candidate: PlanCandidate, disruption: Disruption, affected: frozenset[str]
) -> tuple[str, ...]:
    """`MACHINE_BREAKDOWN` 下，排除故障机后仍无替代机而进 unschedulable 的受影响作业（R9.5）。

    排产器已经在剩余候选里搜过替代机（`exclude_machine_ids` 剔除故障机后的 `candidate_machines`
    自动完成）；一个受影响作业若仍落在 `unschedulable_jobs`，即「没有具备所需 capabilities
    的替代机器」。据此明确报告该结论（R9.5：无替代机器时报告）。

    只对 `MACHINE_BREAKDOWN` 返回非空；其余扰动的 unschedulable 有各自的常规原因，不归此类。
    """
    if disruption.type != "MACHINE_BREAKDOWN":
        return ()
    unsched_ids = {u.job_id for u in candidate.unschedulable_jobs}
    return tuple(sorted(job_id for job_id in affected if job_id in unsched_ids))
