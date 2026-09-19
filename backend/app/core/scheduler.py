"""`Scheduling_Core` 主循环与工序展开（任务 2.4，R4.1–R4.3 / R4.7 / R5.2 / R8.1 / R8.5 / R6.4）。

design.md Components §3.1 把这个组件的定位写成一句话：**唯一有权写时间与资源的组件**。
它是内核里最上层的一块，站在任务 2.3 的时间线原语（`Timeline`、`earliest_feasible_slot`、
`changeover`、`processing_minutes`）与任务 2.1 的冻结快照（`DomainSnapshot`、`available_at`）
之上，产出值对象 `PlanCandidate`——不碰 ORM、不碰 I/O、不读时钟（`snapshot.now` 是入参）。

本模块承担 design.md §3.1.1（作业派生）、§3.1.2（主循环）、§3.1.5（物料语义）三节：

- `expand` —— 每 Order 按 `sequence` 升序展开 1–3 个 `ProductionJob`，`job_id` 确定性、
  `predecessor_job_id` 成线性链；>3 道或 `sequence` 重复 → `INVALID_ROUTING`（§3.1.1、R4.7）；
- `generate_schedule` —— 逐订单原子放置的主循环（§3.1.2）；
- `material_ready_time` / 物料预留 —— design.md §3.1.5 的物料语义。

## 失败诊断与量化：`diagnose_blocking` / `quantify`（任务 2.6，R8.2–R8.7）

主循环在失败点需要**一个** `blocking_reason` 与**一份** `unblock_suggestion` 才能把整单放进
`unschedulable_jobs`。design.md §3.1.6 把这拆成两步：

- `diagnose_blocking(job, snapshot, machine_tls, worker_tls, ...) -> Failure` —— 按**固定判定
  顺序**（物料 → 机器能力 → 机器可用 → 工人技能 → 工人可用 → 班次边界 → 前序）给出唯一的
  `reason`，保证同一失败总报同一原因（便于评估断言）；
- `quantify(failure, job, snapshot) -> dict` —— 按 §3.1.6 的逐类量化字段表渲染
  `unblock_suggestion`，只描述「需要什么」，绝不虚构任何 snapshot 里没有的资源（R8.7）。

物料不足（`MATERIAL_INSUFFICIENT`）在候选枚举**之前**就已由 `material_ready_time` 判出（它
先于所有资源可用性，见 §3.1.5），因此 `_place_order` 直接构造带 `material_shortfalls` 的
`Failure`；其余六类由 `diagnose_blocking` 在候选枚举失败后判定。前序失败
（`OPERATION_PRECEDENCE_VIOLATION`）由主循环在同一订单内某道工序失败、后续工序连带回滚时
标注——它不是「后续工序自身」的失败，而是「前序没排上所以它也排不上」。

## `preference_delta` 已接入（任务 11.2）

偏好是软目标，进候选打分的 `W_PREF` 项（design.md §3.1.2）。任务 11.2 把匹配与分钟等价惩罚
落在 `core.preference` 里，本模块的 `preference_delta` 委托给它：对每条命中当前「作业 × 机器 ×
工人」放置的 penalty 类规则累加 `weight_delta × PREF_UNIT`。它恒 `>= 0`，因此偏好只改变**选谁**、
绝不把不可行变可行——可行性仍由 `earliest_feasible_slot` 独立判定，与偏好无关。空规则集恒返回 0，
无偏好时排产结果与接入前逐字段相同。`cost` 表达式一行未动。

## `freeze` / `locked` / `exclude_machine_ids`：为任务 7.1 的重排预留

三个入参在此实现、由任务 7.1（扰动重排）消费：

- `freeze` —— 一组已排定的 `ScheduledJob`，重排时原样保留：先占住时间线与物料，且其所属订单
  的作业不再参与放置。
- `exclude_machine_ids` —— 从候选机器集中排除（例如故障机器），落在 `candidate_machines` 里。
- `locked` —— 一组 `job_id`，任务 7.1 的语义是「被锁作业要么原样冻结、要么进 unschedulable，
  绝不悄悄移动」。本任务把它实现为：`locked` 中且**不**在 `freeze` 中的作业，其所属订单直接
  判 `LOCKED_JOB_INFEASIBLE`——因为一个被锁却没有冻结位置的作业无法被主循环安全放置。

初始计划生成时三者皆空，主循环退化为 design.md §3.1.2 的裸形态。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.core.preference import preference_delta as _preference_delta
from app.core.scheduling import Timeline, earliest_feasible_slot, processing_minutes
from app.core.snapshot import (
    DomainSnapshot,
    Machine,
    Order,
    PreferenceRule,
    Priority,
    Product,
    Worker,
    available_at,
)

# --------------------------------------------------------------------------
# 打分常量（design.md §3.1.2）
# --------------------------------------------------------------------------

#: 换型分钟在候选打分中的权重。
W_CHANGEOVER: Decimal = Decimal("1.0")

#: 偏好惩罚权重。`preference_delta` 已是「分钟等价」单位，故权重为 1（design.md §3.1.2）。
W_PREF: Decimal = Decimal("1.0")

#: 订单排序的优先级秩（design.md §3.1.2、R8.5）。数值越小越先排。
PRIORITY_RANK: dict[Priority, int] = {"URGENT": 0, "HIGH": 1, "NORMAL": 2, "LOW": 3}

#: 可行性三态（design.md §3.1.2 / R8.1）。
Feasibility = Literal["FEASIBLE", "PARTIAL", "NO_FEASIBLE_PLAN"]


# --------------------------------------------------------------------------
# 值对象
# --------------------------------------------------------------------------


class ProductionJob(BaseModel):
    """待排产的工作单元：一个 Order 的一道 Operation（R4.2、design.md §3.1.1）。

    `job_id` 是确定性的 `"{order_id}-OP{sequence}"`，不是 UUID——属性 1（R5.7）要求同输入
    两次运行产出逐字段相同的结果，随机 ID 会直接破坏它。`predecessor_job_id` 把同一订单的
    工序串成线性链（无返工、无分支，R4 限定）。

    `frozen=True`：作业一经展开即不可变。它是内核的中间值对象，可变性在这里没有任何用途，
    冻结让「主循环意外改写了作业」在类型层面不可能。
    """

    model_config = ConfigDict(frozen=True)

    job_id: str
    order_id: str
    product_id: str
    quantity: Decimal
    operation_sequence: int
    predecessor_job_id: str | None
    required_machine_type: str
    required_capability: str | None
    required_worker_skill: str
    base_processing_time_per_unit: Decimal
    setup_time: int


class ScheduledJob(BaseModel):
    """已排产作业：唯一记录「谁、在哪台机器、什么时候」的值对象（R4、design.md §3.1）。

    `setup_minutes = op.setup_time + changeover`（R4.4），`changeover_minutes` 单列供
    `Objective_Scorer` 的换型分量与本模块的候选打分使用。`[start_time, end_time)` 是含换型
    在内的整段占用，与 `Timeline` 里的占用区间一致。
    """

    model_config = ConfigDict(frozen=True)

    job_id: str
    order_id: str
    product_id: str
    machine_id: str
    worker_id: str
    start_time: datetime
    end_time: datetime
    setup_minutes: int = Field(ge=0)
    changeover_minutes: int = Field(ge=0)


class Failure(BaseModel):
    """一次工序放置失败的**分类结果**（design.md §3.1.6）。

    `diagnose_blocking` 按固定判定顺序产出它，`quantify` 据它与 `job` / `snapshot` 逐类
    渲染 `unblock_suggestion` 的结构化字段。把「判定原因」与「量化建议」拆成两步，是为了让
    §3.1.6 的两张表各自可测：判定顺序的正确性只看 `reason`，量化字段的正确性只看 `quantify`
    的输出，互不牵连。

    `reason` 取自 R6.1 的 9 类之一。其余字段是 `quantify` 需要、但 `diagnose_blocking` 在
    判定时**已经算出**的上下文，捎带过去免得 `quantify` 重算：

    - `material_shortfalls` —— `MATERIAL_INSUFFICIENT` 的每种物料缺口（`_place_order` 在候选
      枚举前就已算出，见 §3.1.5）；
    - `available_minutes_in_shift` —— `SHIFT_BOUNDARY_VIOLATION` 的班次剩余分钟（判定时求得的
      最大可用窗口）；
    - `predecessor_job_id` / `predecessor_reason` —— `OPERATION_PRECEDENCE_VIOLATION` 的前序
      作业与其自身失败原因（前序整单回滚时一并传下）。
    """

    model_config = ConfigDict(frozen=True)

    reason: str
    material_shortfalls: dict[str, Decimal] = Field(default_factory=dict)
    available_minutes_in_shift: int | None = None
    predecessor_job_id: str | None = None
    predecessor_reason: str | None = None


class UnschedulableJob(BaseModel):
    """不可排产作业及其解封条件（R8.2、R8.3）。"""

    model_config = ConfigDict(frozen=True)

    job_id: str
    order_id: str
    blocking_reason: str
    unblock_suggestion: dict[str, Any]


class PlanCandidate(BaseModel):
    """一次排产的完整结果（design.md §3.1.2、R8.1）。

    `scheduled_jobs` 与 `unschedulable_jobs` 是展开出的全部 `ProductionJob` 的一个划分：
    不相交、并集为全集（属性 4）。`feasibility` 与该划分一致——无 unschedulable →
    `FEASIBLE`；无 scheduled → `NO_FEASIBLE_PLAN`；否则 `PARTIAL`。
    """

    model_config = ConfigDict(frozen=True)

    scheduled_jobs: tuple[ScheduledJob, ...]
    unschedulable_jobs: tuple[UnschedulableJob, ...]
    feasibility: Feasibility


# --------------------------------------------------------------------------
# 路线错误（R4.7）
# --------------------------------------------------------------------------


class InvalidRoutingError(Exception):
    """产品路线非法（R4.7）：>3 道工序或 `sequence` 重复。

    `code` 是字符串常量而非 `api.errors.ErrorCode` 成员：内核不能 import `fastapi`
    （任务 1.8 断言 ①）。API 边界的映射随 `POST /api/plans/generate`（任务 2.12）落地时
    在 `errors.py` 追加同名成员——那里的约定是「枚举成员数恒等于系统当前真实能返回的错误
    种类数」。
    """

    code: str = "INVALID_ROUTING"

    def __init__(self, product_id: str, detail: str) -> None:
        self.product_id = product_id
        self.detail = detail
        super().__init__(f"产品 {product_id} 的工序路线非法：{detail}")


# --------------------------------------------------------------------------
# 工序展开（design.md §3.1.1）
# --------------------------------------------------------------------------


def expand(order: Order, product: Product) -> tuple[ProductionJob, ...]:
    """把一个 Order 按 `sequence` 升序展开成 1–3 个 `ProductionJob`（R4.2、§3.1.1）。

    `predecessor_job_id` 成线性链：第一道工序无前驱（`None`），其余每道的前驱是紧邻的前一
    道。`job_id = "{order_id}-OP{sequence}"` 确定性可重现（R5.7）。

    路线校验（R4.7）在展开前完成：

    - 工序数 >3 → `INVALID_ROUTING`；
    - `sequence` 重复 → `INVALID_ROUTING`。

    `Operation.sequence` 已由快照的字段约束限定在 1..3，但「1..3 之间不重复」与「至多 3 道」
    是两件事——三道全填 `sequence=1` 不违反字段约束却是非法路线，因此这里显式查重。
    """
    operations = product.operations_in_sequence()

    if len(operations) < 1:
        raise InvalidRoutingError(product.product_id, "至少需要 1 道工序")
    if len(operations) > 3:
        raise InvalidRoutingError(
            product.product_id, f"最多 3 道工序，实际 {len(operations)} 道"
        )

    sequences = [op.sequence for op in operations]
    if len(set(sequences)) != len(sequences):
        raise InvalidRoutingError(product.product_id, f"sequence 重复：{sequences}")

    jobs: list[ProductionJob] = []
    predecessor: str | None = None
    for op in operations:
        job_id = f"{order.order_id}-OP{op.sequence}"
        jobs.append(
            ProductionJob(
                job_id=job_id,
                order_id=order.order_id,
                product_id=order.product_id,
                quantity=order.quantity,
                operation_sequence=op.sequence,
                predecessor_job_id=predecessor,
                required_machine_type=op.required_machine_type,
                required_capability=op.required_capability,
                required_worker_skill=op.required_worker_skill,
                base_processing_time_per_unit=op.base_processing_time_per_unit,
                setup_time=op.setup_time,
            )
        )
        predecessor = job_id
    return tuple(jobs)


# --------------------------------------------------------------------------
# 偏好打分桩（任务 11.2 接入）
# --------------------------------------------------------------------------


def preference_delta(
    job: ProductionJob,
    machine: Machine,
    worker: Worker,
    rules: tuple[PreferenceRule, ...],
) -> Decimal:
    """把作业放在 `machine` / `worker` 上会新增多少偏好惩罚（分钟等价，design.md §3.1.2）。

    **任务 11.2 已接入**：委托给 `core.preference.preference_delta`，对每条命中当前放置的
    penalty 类规则累加 `weight_delta × PREF_UNIT`。返回值恒 `>= 0`——偏好只能让候选更不划算、
    改变**选谁**，绝不把不可行槽位变可行（可行性由 `earliest_feasible_slot` 独立判定，与偏好
    无关）。空规则集恒返回 0，因此无偏好时排产结果与接入前逐字段相同。

    这里保留一层薄封装（而非在 `_best_candidate` 直接调 `core.preference`），是为了让主循环
    的 `cost` 表达式一行不动，也让 `preference_delta` 这个被测契约名保持稳定。
    """
    return _preference_delta(job, machine, worker, rules)


# --------------------------------------------------------------------------
# 候选枚举（design.md §3.1.2）
# --------------------------------------------------------------------------


def candidate_machines(
    job: ProductionJob,
    snapshot: DomainSnapshot,
    exclude_machine_ids: frozenset[str],
) -> tuple[Machine, ...]:
    """`job` 的候选机器：类型匹配、能力覆盖、状态可用、未被排除（design.md §3.1.2）。

    过滤条件（全部满足才入选）：

    - `machine_type == job.required_machine_type`；
    - `required_capability ⊆ capabilities`（`None` 表示工序无能力要求）；
    - `status ∉ {DOWN, MAINTENANCE}`；
    - `machine_id ∉ exclude_machine_ids`（任务 7.1 的故障机器排除）。

    停机窗（`downtime_windows`）不在这里过滤：它是**时段**级别的不可用，由
    `earliest_feasible_slot` 在具体槽位上判定（一台机器上午停机、下午可用，不该整台被剔）。
    此处只剔除**整机**级别的不可用。

    返回按 `machine_id` 升序，让候选枚举顺序确定（属性 1）——尽管主循环的 tie-break 已经
    把 `machine_id` 编进打分键，稳定的枚举顺序仍让调试时的行为可预测。
    """
    result = [
        machine
        for machine in snapshot.machines
        if machine.machine_type == job.required_machine_type
        and machine.has_capability(job.required_capability)
        and machine.status not in ("DOWN", "MAINTENANCE")
        and machine.machine_id not in exclude_machine_ids
    ]
    return tuple(sorted(result, key=lambda m: m.machine_id))


def candidate_workers(job: ProductionJob, snapshot: DomainSnapshot) -> tuple[Worker, ...]:
    """`job` 的候选工人：技能匹配（design.md §3.1.2）。

    当日缺勤（`absences`）与班次边界同样是**时段**级别的判定，交给
    `earliest_feasible_slot`（`hard_end = min(worker.shift_end, ...)`）与 `is_absent_during`
    在具体槽位上处理，此处只按技能筛。返回按 `worker_id` 升序，理由同 `candidate_machines`。
    """
    result = [worker for worker in snapshot.workers if worker.has_skill(job.required_worker_skill)]
    return tuple(sorted(result, key=lambda w: w.worker_id))


# --------------------------------------------------------------------------
# 物料语义（design.md §3.1.5）
# --------------------------------------------------------------------------


def material_need(product: Product, order_quantity: Decimal) -> dict[str, Decimal]:
    """一个订单在 `sequence = 1` 一次性预留的物料需求（design.md §3.1.5）。

    `need[material_id] = Σ line.quantity_per_unit × order_quantity`。单层 BOM（R4 限定，
    无多层展开），因此这里不递归。同一物料在 BOM 里出现多行时累加。
    """
    need: dict[str, Decimal] = {}
    for line in product.bom:
        need[line.material_id] = need.get(line.material_id, Decimal("0")) + (
            line.quantity_per_unit * order_quantity
        )
    return need


def material_ready_time(
    need: dict[str, Decimal],
    snapshot: DomainSnapshot,
    reserved: dict[str, Decimal],
    shift_start: datetime,
) -> tuple[datetime | None, dict[str, Decimal]]:
    """订单物料齐备的最早时刻（design.md §3.1.5、R6.3 / R6.4）。

    对每种所需物料，可用量取 `available_at(material, t) − 本次排产已额外预留量`。判定：

    - 现有可用量已足够全部物料 → 齐备时刻 = `shift_start`；
    - 否则取「使每种物料累计可用量首次足够的**齐备起点**」中最晚的一个作为齐备时刻；
    - 时域内全部到货仍不足 → 返回 `(None, shortfalls)`，`shortfalls[material_id] =
      need − 时域内全部可用`（R6.4，只报缺口，不补足）。

    `reserved` 是**本次排产**已被先前订单预留掉的量（`available_at` 反映的是快照初始的
    `reserved_quantity`，不含本次运行内的滚动预留），两者相减才是此刻真正可动用的量。

    ## 齐备时刻必须是「作业真能开工」的时刻，与校验器的 `available_at(m, start_time)` 一致

    `available_at` 用**严格** `eta < t`（design.md §3.1.5）：到货与开工同一时刻，物料不算已到。
    返回的 `ready_time` 会被主循环当作作业 `start_time` 的下界（`ready = max(…, ready_material)`），
    而作业真正的 `start_time >= ready_time`。因此一批 `eta = e` 的到货，只有当作业开工时刻
    **严格晚于** `e` 时才算得上——在整分钟时间粒度下，最早能用上它的开工时刻是 `e + 1 分钟`，
    不是 `e` 本身。此前实现把候选齐备时刻取成 `eta` 却用 `eta + 1 分钟` 去探测可用量，于是把
    一批「恰好卡在开工时刻上、其实还没到货」的到货算成了在手量，主循环据此把订单排在
    `start_time = eta`——校验器随后用 `available_at(material, eta)`（不含该批到货）复算，判它
    `MATERIAL_INSUFFICIENT`，两个独立实现因此分歧（属性 2 red）。

    修复：候选齐备时刻取 `shift_start` 与每批到货的 `eta + 1 分钟`；在候选时刻 `t` **就地**用
    `available_at(material, t)` 判定（探测点即 `t` 本身，与校验器逐字一致）。这样 `ready_time`
    是「物料真正在手」的最早开工时刻，主循环放出的 `start_time` 恒 `>= ready_time`，校验器的
    `available_at(material, start_time)` 必然也把同一批到货计入——两侧对同一批到货是否「已到」
    的判断永远相同。

    返回 `(ready_time, shortfalls)`：可行时 `ready_time` 非空、`shortfalls` 为空；不可行时
    `ready_time` 为 `None`、`shortfalls` 列出每种不足物料的缺口。
    """
    materials = snapshot.materials_by_id()
    # 收集全部相关到货 eta。一批 `eta = e` 的到货最早能被用上的**开工时刻**是 `e + 1 分钟`
    # （校验器用严格 `eta < start_time`），因此候选齐备时刻用 `e + 1 分钟` 而非 `e`。
    ready_points: set[datetime] = set()
    for material_id in need:
        material = materials.get(material_id)
        if material is not None:
            ready_points.update(
                delivery.eta + timedelta(minutes=1) for delivery in material.incoming_deliveries
            )
    candidate_times = sorted(
        {shift_start} | {t for t in ready_points if t > shift_start}
    )

    shortfalls: dict[str, Decimal] = {}
    ready: datetime | None = None
    for t in candidate_times:
        # 就地用 `available_at(material, t)` 判定——探测点即候选齐备时刻 `t` 本身，与校验器
        # `check_material_sufficient` 对 `available_at(material, start_time)` 的用法逐字一致，
        # 两个独立实现因此对「t 时刻某批到货是否已到」给出相同答案。
        all_met = True
        for material_id, required in need.items():
            material = materials.get(material_id)
            if material is None:
                all_met = False
                break
            usable = available_at(material, t) - reserved.get(material_id, Decimal("0"))
            if usable < required:
                all_met = False
                break
        if all_met:
            ready = t
            break

    if ready is not None:
        return ready, {}

    # 时域内全部到货仍不足：报每种物料的缺口。用一个远期探测点把全部在途到货计入。
    horizon = max(candidate_times) + timedelta(days=3650) if candidate_times else shift_start
    for material_id, required in need.items():
        material = materials.get(material_id)
        total = (
            available_at(material, horizon) - reserved.get(material_id, Decimal("0"))
            if material is not None
            else Decimal("0")
        )
        if total < required:
            shortfalls[material_id] = required - total
    return None, shortfalls


# --------------------------------------------------------------------------
# 失败诊断（design.md §3.1.6 的固定判定顺序）
# --------------------------------------------------------------------------


def diagnose_blocking(
    job: ProductionJob,
    snapshot: DomainSnapshot,
    machine_tls: dict[str, Timeline],
    worker_tls: dict[str, Timeline],
    exclude_machine_ids: frozenset[str],
    *,
    ready: datetime,
) -> Failure:
    """候选枚举后无可行槽位时的失败原因（design.md §3.1.6 的**固定判定顺序**，R8.2）。

    判定顺序钉死为：**机器能力 → 机器可用 → 工人技能 → 工人可用 → 班次边界**（物料在候选
    枚举之前判定，前序由主循环标注，二者不落在本函数内）。顺序固定保证同一失败总报同一
    原因，便于评估断言——例如一个既缺能力机器又缺技能工人的作业，永远报
    `MACHINE_CAPABILITY_MISMATCH` 而非 `WORKER_SKILL_MISMATCH`。

    每一档判定只从 snapshot 枚举资源（R8.7 不虚构）：

    - **机器能力**：`machine_type` 匹配的机器里没有一台覆盖 `required_capability`；
    - **机器可用**：有能力机器，但全部 `DOWN` / `MAINTENANCE` 或被 `exclude_machine_ids` 排除；
    - **工人技能**：没有工人具备 `required_worker_skill`；
    - **工人可用**：有能力机器、有技能工人，但没有一个「机器 × 工人」组合能在 `ready` 之后、
      班次内放下这道工序，且**存在**至少一个组合其班次窗口本身够长——即冲突源于占用/缺勤而非
      窗口太短，归为 `WORKER_UNAVAILABLE`；
    - **班次边界**：上一档不成立（没有任何组合的可用窗口够长），即工序时长超过所有候选组合的
      班次剩余，归为 `SHIFT_BOUNDARY_VIOLATION`，并捎带最大可用窗口分钟给 `quantify`。

    `ready` 是主循环算出的最早可开工时刻（`max(班次起点, 前序结束, 物料齐备)`）——班次边界的
    「可用分钟」要从 `ready` 起算，不是从班次起点起算。
    """
    capable = _capable_machines(job, snapshot)
    if not capable:
        return Failure(reason="MACHINE_CAPABILITY_MISMATCH")

    available_machines = [
        m
        for m in capable
        if m.status not in ("DOWN", "MAINTENANCE") and m.machine_id not in exclude_machine_ids
    ]
    if not available_machines:
        return Failure(reason="MACHINE_UNAVAILABLE")

    skilled = [w for w in snapshot.workers if w.has_skill(job.required_worker_skill)]
    if not skilled:
        return Failure(reason="WORKER_SKILL_MISMATCH")

    # 有能力机器、有技能工人。区分「窗口本身太短」（班次边界）与「窗口够长但被占用/缺勤堵住」
    # （工人不可用）：逐「机器 × 工人」组合算 [max(ready, shift_start, avail_start), hard_end)
    # 的窗口长度，与工序含换型的时长比较。只要有一个组合窗口够长，失败就归因于占用冲突。
    max_window = 0
    window_ever_enough = False
    for machine in available_machines:
        proc = processing_minutes(
            job.base_processing_time_per_unit, job.quantity, machine.rate_multiplier
        )
        # 换型未知（取决于机器上此刻的产品），用 setup_time 作下界估计工序占用。
        needed = job.setup_time + proc
        for worker in skilled:
            window_start = max(ready, worker.shift_start, machine.available_start)
            hard_end = min(worker.shift_end, machine.available_end)
            window = _minutes_between(window_start, hard_end)
            max_window = max(max_window, window)
            if window >= needed:
                window_ever_enough = True

    if not window_ever_enough:
        return Failure(
            reason="SHIFT_BOUNDARY_VIOLATION",
            available_minutes_in_shift=max_window,
        )

    # 至少一个组合窗口够长，却仍放不下 → 该窗口被既有占用或当日缺勤堵住。
    return Failure(reason="WORKER_UNAVAILABLE")


def _capable_machines(job: ProductionJob, snapshot: DomainSnapshot) -> list[Machine]:
    """`machine_type` 匹配且覆盖 `required_capability` 的机器（不看状态/排除，供诊断用）。"""
    return [
        m
        for m in snapshot.machines
        if m.machine_type == job.required_machine_type and m.has_capability(job.required_capability)
    ]


# --------------------------------------------------------------------------
# 量化解封条件（design.md §3.1.6 的逐类字段表，R8.3 / R8.7）
# --------------------------------------------------------------------------


def quantify(failure: Failure, job: ProductionJob, snapshot: DomainSnapshot) -> dict[str, Any]:
    """把 `failure` 渲染成 `unblock_suggestion` 的结构化字段（design.md §3.1.6、R8.3）。

    逐类输出 §3.1.6 表里声明的字段，每类至少含一项可量化的解锁条件（R8.3）：

    - `MATERIAL_INSUFFICIENT` → `material_id` / `shortfall_quantity` / `unit` / `needed_before`
    - `MACHINE_UNAVAILABLE` → `required_machine_type` / `minutes_needed`
      / `earliest_window_needed`
    - `MACHINE_CAPABILITY_MISMATCH` → `required_capability` / `qualifying_machine_types`
    - `WORKER_SKILL_MISMATCH` → `required_skill` / `worker_minutes_needed`
    - `WORKER_UNAVAILABLE` → `required_skill` / `worker_minutes_needed` / `shift_window`
    - `SHIFT_BOUNDARY_VIOLATION` → `required_minutes` / `available_minutes_in_shift`
      / `deficit_minutes`
    - `OPERATION_PRECEDENCE_VIOLATION` → `predecessor_job_id` / `predecessor_blocking_reason`

    **不虚构资源**（R8.7）：候选机器类型只从 snapshot 枚举，分钟数只描述这道工序「需要多少」，
    不创建任何实体、不建议采购具体机器。`minutes_needed` / `worker_minutes_needed` 用倍率 1.0
    的裸加工时长（`ceil(base × qty)`），因为诊断时并不知道最终会落到哪台机器（倍率各异）——
    报「以基准速率需多少分钟」是一个稳定、可解释、不依赖某台具体机器的量。
    """
    minutes_needed = processing_minutes(
        job.base_processing_time_per_unit, job.quantity, Decimal("1")
    )

    if failure.reason == "MATERIAL_INSUFFICIENT":
        material_id, shortfall = _worst_shortfall(failure.material_shortfalls, snapshot)
        return {
            "material_id": material_id,
            "shortfall_quantity": str(shortfall),
            "unit": _unit_of(material_id, snapshot),
            "needed_before": _needed_before(job, snapshot),
        }

    if failure.reason == "MACHINE_CAPABILITY_MISMATCH":
        return {
            "required_capability": job.required_capability,
            "qualifying_machine_types": _machine_types_with_capability(
                job.required_capability, snapshot
            ),
        }

    if failure.reason == "MACHINE_UNAVAILABLE":
        return {
            "required_machine_type": job.required_machine_type,
            "minutes_needed": minutes_needed,
            "earliest_window_needed": _earliest_window_needed(job, snapshot),
        }

    if failure.reason == "WORKER_SKILL_MISMATCH":
        return {
            "required_skill": job.required_worker_skill,
            "worker_minutes_needed": minutes_needed,
        }

    if failure.reason == "WORKER_UNAVAILABLE":
        return {
            "required_skill": job.required_worker_skill,
            "worker_minutes_needed": minutes_needed,
            "shift_window": _shift_window_for_skill(job.required_worker_skill, snapshot),
        }

    if failure.reason == "SHIFT_BOUNDARY_VIOLATION":
        required = job.setup_time + minutes_needed
        available = failure.available_minutes_in_shift or 0
        return {
            "required_minutes": required,
            "available_minutes_in_shift": available,
            "deficit_minutes": max(0, required - available),
        }

    if failure.reason == "OPERATION_PRECEDENCE_VIOLATION":
        return {
            "predecessor_job_id": failure.predecessor_job_id,
            "predecessor_blocking_reason": failure.predecessor_reason,
        }

    # 兜底：不应到达（主循环只构造上述七类）。给一个至少含一个数值字段的安全建议。
    return {"minutes_needed": minutes_needed}


def _machine_types_with_capability(
    capability: str | None, snapshot: DomainSnapshot
) -> list[str]:
    """snapshot 里具备 `capability` 的机器类型（去重升序，R8.7 只枚举现有资源）。

    `MACHINE_CAPABILITY_MISMATCH` 报「哪些机型能做」——若一台都没有，返回空列表（诚实地说
    「当前没有任何机器具备该能力」，而非虚构一个机型）。
    """
    if capability is None:
        return []
    types = {m.machine_type for m in snapshot.machines if capability in m.capabilities}
    return sorted(types)


def _shift_window_for_skill(skill: str, snapshot: DomainSnapshot) -> str | None:
    """具备 `skill` 的工人里最早的班次窗口，作为「需要在哪个班次增加人手」的定位。

    取具备该技能的工人中 `shift_start` 最早者的 `[shift_start, shift_end)`。没有这样的工人
    （不会发生在 `WORKER_UNAVAILABLE`——那意味着有技能工人存在）→ `None`。
    """
    windows = [
        (w.shift_start, w.shift_end) for w in snapshot.workers if w.has_skill(skill)
    ]
    if not windows:
        return None
    start, end = min(windows, key=lambda pair: pair[0])
    return f"{start.isoformat()}/{end.isoformat()}"


def _earliest_window_needed(job: ProductionJob, snapshot: DomainSnapshot) -> str:
    """`MACHINE_UNAVAILABLE` 报「最早需要机时的时刻」= 排产时域起点（生产日 00:00）。"""
    return _horizon_start(snapshot).isoformat()


def _needed_before(job: ProductionJob, snapshot: DomainSnapshot) -> str:
    """`MATERIAL_INSUFFICIENT` 的「最迟到货时刻」= 该订单的 `due_date`（物料须在交期前齐备）。"""
    order = snapshot.orders_by_id().get(job.order_id)
    return order.due_date.isoformat() if order is not None else _horizon_start(snapshot).isoformat()


def _minutes_between(start: datetime, end: datetime) -> int:
    """`[start, end)` 的整分钟长度，负值截断为 0（用于班次窗口长度）。"""
    if end <= start:
        return 0
    return int((end - start).total_seconds() // 60)


# --------------------------------------------------------------------------
# 候选打分（design.md §3.1.2）
# --------------------------------------------------------------------------


class _Candidate(BaseModel):
    """主循环内部的一个候选放置及其打分键。不对外暴露。"""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    machine_id: str
    worker_id: str
    product_id: str
    start_time: datetime
    end_time: datetime
    setup_minutes: int
    changeover_minutes: int
    key: tuple[Decimal, str, str]


def _minutes_since(start: datetime, end: datetime) -> Decimal:
    """`end` 相对 `start` 的分钟数（`Decimal`，禁止浮点）。用于「越早完工越好」的打分。"""
    return Decimal((end - start).total_seconds()) / Decimal("60")


# --------------------------------------------------------------------------
# 主循环（design.md §3.1.2）
# --------------------------------------------------------------------------


def generate_schedule(
    snapshot: DomainSnapshot,
    *,
    freeze: tuple[ScheduledJob, ...] = (),
    locked: frozenset[str] = frozenset(),
    exclude_machine_ids: frozenset[str] = frozenset(),
) -> PlanCandidate:
    """在冻结快照上产出一份 `PlanCandidate`（design.md §3.1.2、R4 / R5.2 / R8）。

    确定性全序放置：订单按 `(PRIORITY_RANK[priority], due_date, order_id)` 升序（R8.5），
    每个订单内的工序按 `sequence` 升序（`expand` 已保证），逐工序枚举「候选机器 × 候选工人」
    找最早可行槽位并按 `cost` 打分，tie-break `(cost, machine_id, worker_id)` 是全序，保证
    可重现（属性 1、R5.7）。

    **订单是原子单位**（design.md §3.1.5 / §3.1.6、R8.1）：一个订单的作业先写入 `tentative`
    而不提交到时间线；任一工序失败即整单回滚（`tentative` 从未提交，无需撤销），该订单全部
    工序进入 `unschedulable_jobs`。半个订单在车间里是负价值（占了机时却交不出货）。

    `freeze` / `locked` / `exclude_machine_ids` 见模块 docstring。初始生成时三者皆空。
    """
    products = snapshot.products_by_id()
    orders_by_id = snapshot.orders_by_id()

    # ---- 0. 资源时间线初始化 ----
    machine_tls: dict[str, Timeline] = {m.machine_id: Timeline() for m in snapshot.machines}
    worker_tls: dict[str, Timeline] = {w.worker_id: Timeline() for w in snapshot.workers}
    # 本次排产的滚动物料预留（叠加在快照初始 reserved_quantity 之上）。
    reserved: dict[str, Decimal] = {}

    # 冻结集先占位（重排用；初始生成时 freeze=∅）。被冻结作业的订单不再参与放置。
    frozen_order_ids: set[str] = set()
    frozen_job_ids: set[str] = set()
    scheduled: list[ScheduledJob] = list(freeze)
    for sj in freeze:
        if sj.machine_id in machine_tls:
            machine_tls[sj.machine_id].occupy(sj.start_time, sj.end_time, sj.product_id)
        if sj.worker_id in worker_tls:
            worker_tls[sj.worker_id].occupy(sj.start_time, sj.end_time, sj.product_id)
        frozen_order_ids.add(sj.order_id)
        frozen_job_ids.add(sj.job_id)
        # 冻结作业的物料在其订单首道工序处已消耗，这里一并预留。`ScheduledJob` 不带
        # quantity（它只记「谁在哪台机器什么时候」），因此从快照的订单取需求量。
        product = products.get(sj.product_id)
        order = orders_by_id.get(sj.order_id)
        if product is not None and order is not None and sj.job_id.endswith("-OP1"):
            for material_id, qty in material_need(product, order.quantity).items():
                reserved[material_id] = reserved.get(material_id, Decimal("0")) + qty

    # ---- 1. 订单排序（确定性全序，R8.5） ----
    order_seq = sorted(
        snapshot.orders,
        key=lambda o: (PRIORITY_RANK[o.priority], o.due_date, o.order_id),
    )

    unschedulable: list[UnschedulableJob] = []

    # ---- 2. 逐订单、订单内逐工序放置 ----
    for order in order_seq:
        product = products[order.product_id]  # 引用完整性由 require_referential_integrity 保证
        jobs = expand(order, product)

        # 完全冻结的订单：作业已在时间线上，跳过。
        if jobs and all(job.job_id in frozen_job_ids for job in jobs):
            continue
        # 部分冻结在本任务不支持：一个订单要么整体冻结、要么整体重排（原子单位）。
        if order.order_id in frozen_order_ids:
            continue

        # 被锁但未冻结的作业无法安全放置 → 整单进 unschedulable（任务 7.1 语义）。
        locked_here = [job for job in jobs if job.job_id in locked]
        if locked_here:
            for job in jobs:
                unschedulable.append(
                    UnschedulableJob(
                        job_id=job.job_id,
                        order_id=order.order_id,
                        blocking_reason="PREDECESSOR_UNSCHEDULABLE",
                        unblock_suggestion={
                            "locked_job_ids": [j.job_id for j in locked_here],
                            "locked_count": len(locked_here),
                        },
                    )
                )
            continue

        tentative, failure, failed_job_id = _place_order(
            order,
            jobs,
            product,
            snapshot,
            machine_tls,
            worker_tls,
            reserved,
            exclude_machine_ids,
        )

        # ---- 4. 订单级原子提交或整单回滚 ----
        if failure is None:
            for sj in tentative:
                machine_tls[sj.machine_id].occupy(sj.start_time, sj.end_time, sj.product_id)
                worker_tls[sj.worker_id].occupy(sj.start_time, sj.end_time, sj.product_id)
            # R6.4：物料在 sequence=1 放置成功时一次性预留（订单提交后落定）。
            for material_id, qty in material_need(product, order.quantity).items():
                reserved[material_id] = reserved.get(material_id, Decimal("0")) + qty
            scheduled.extend(tentative)
        else:
            # 整单回滚：tentative 从未提交到时间线，无需撤销资源（§3.1.6）。
            # 失败工序带真实 reason + quantify 的量化建议；同单其余工序因整单原子性一并
            # 落空，标注 OPERATION_PRECEDENCE_VIOLATION 指向失败工序（R8.4：每个作业都有
            # blocking_reason）。
            job_by_id = {job.job_id: job for job in jobs}
            failed_job = job_by_id.get(failed_job_id) if failed_job_id is not None else None
            for job in jobs:
                if failed_job is not None and job.job_id == failed_job_id:
                    reason = failure.reason
                    suggestion = quantify(failure, failed_job, snapshot)
                else:
                    reason = "OPERATION_PRECEDENCE_VIOLATION"
                    suggestion = quantify(
                        Failure(
                            reason="OPERATION_PRECEDENCE_VIOLATION",
                            predecessor_job_id=failed_job_id,
                            predecessor_reason=failure.reason,
                        ),
                        job,
                        snapshot,
                    )
                unschedulable.append(
                    UnschedulableJob(
                        job_id=job.job_id,
                        order_id=order.order_id,
                        blocking_reason=reason,
                        unblock_suggestion=suggestion,
                    )
                )

    feasibility = _feasibility(scheduled, unschedulable, freeze)
    return PlanCandidate(
        scheduled_jobs=tuple(scheduled),
        unschedulable_jobs=tuple(unschedulable),
        feasibility=feasibility,
    )


def _place_order(
    order: Order,
    jobs: tuple[ProductionJob, ...],
    product: Product,
    snapshot: DomainSnapshot,
    machine_tls: dict[str, Timeline],
    worker_tls: dict[str, Timeline],
    reserved: dict[str, Decimal],
    exclude_machine_ids: frozenset[str],
) -> tuple[list[ScheduledJob], Failure | None, str | None]:
    """尝试把一个订单的全部工序放进 `tentative`；任一工序失败即返回 `(部分, Failure, 失败作业 id)`。

    只读时间线（用于 `earliest_feasible_slot` 的可行性检查）：**不**在此提交占用，提交由
    调用方在整单成功后统一完成（原子性）。同一订单内后序工序需要看到前序工序的占用，因此
    这里维护两条**临时**时间线，把已放定的 tentative 作业叠加上去。

    第三个返回值是失败作业的 `job_id`（成功时 `None`），供主循环给同单其余工序标注
    `OPERATION_PRECEDENCE_VIOLATION`——「因为这道工序没排上，整单都排不上」。
    """
    # 临时时间线：既有占用 + 本订单已放定的 tentative 作业。
    tmp_machine: dict[str, Timeline] = {mid: _clone(tl) for mid, tl in machine_tls.items()}
    tmp_worker: dict[str, Timeline] = {wid: _clone(tl) for wid, tl in worker_tls.items()}

    need = material_need(product, order.quantity)
    shift_start = _horizon_start(snapshot)
    ready_material, shortfalls = material_ready_time(need, snapshot, reserved, shift_start)
    if ready_material is None:
        # 时域内全部到货仍不足 → INFEASIBLE(shortfall)（R6.4，design.md §3.1.5）。物料先于
        # 一切资源可用性判定（§3.1.6 的判定顺序首档），失败作业记为首道工序。
        first_job_id = jobs[0].job_id if jobs else None
        return [], Failure(reason="MATERIAL_INSUFFICIENT", material_shortfalls=shortfalls), (
            first_job_id
        )

    tentative: list[ScheduledJob] = []
    predecessor_end: datetime | None = None

    for job in jobs:
        # ready = max(班次起点, 前序结束, 物料齐备)（design.md §3.1.2）。
        ready = shift_start
        if predecessor_end is not None and predecessor_end > ready:
            ready = predecessor_end
        if ready_material > ready:
            ready = ready_material

        best = _best_candidate(job, snapshot, tmp_machine, tmp_worker, ready, exclude_machine_ids)
        if best is None:
            failure = diagnose_blocking(
                job, snapshot, tmp_machine, tmp_worker, exclude_machine_ids, ready=ready
            )
            return tentative, failure, job.job_id

        sj = ScheduledJob(
            job_id=job.job_id,
            order_id=job.order_id,
            product_id=job.product_id,
            machine_id=best.machine_id,
            worker_id=best.worker_id,
            start_time=best.start_time,
            end_time=best.end_time,
            setup_minutes=best.setup_minutes,
            changeover_minutes=best.changeover_minutes,
        )
        tentative.append(sj)
        # 把这道工序占进临时时间线，后序工序才看得见它。
        tmp_machine[sj.machine_id].occupy(sj.start_time, sj.end_time, sj.product_id)
        tmp_worker[sj.worker_id].occupy(sj.start_time, sj.end_time, sj.product_id)
        predecessor_end = sj.end_time

    return tentative, None, None


def _best_candidate(
    job: ProductionJob,
    snapshot: DomainSnapshot,
    machine_tls: dict[str, Timeline],
    worker_tls: dict[str, Timeline],
    ready: datetime,
    exclude_machine_ids: frozenset[str],
) -> _Candidate | None:
    """枚举「候选机器 × 候选工人」找 `cost` 最小的可行放置（design.md §3.1.2）。

    对每台候选机器算加工时长 `proc = ceil(base × qty ÷ rate_multiplier)`（R4.5），对每个候选
    工人在其上求 `earliest_feasible_slot`（`hard_end = min(worker.shift_end,
    machine.available_end)`，拒绝跨班次，R4.6）。工人当日缺勤经 `is_absent_during` 剔除。

    `cost = minutes_since(horizon_start, slot.end) + W_CHANGEOVER × changeover +
    W_PREF × preference_delta`，tie-break `(cost, machine_id, worker_id)` 是全序。
    """
    horizon_start = _horizon_start(snapshot)
    best: _Candidate | None = None

    for machine in candidate_machines(job, snapshot, exclude_machine_ids):
        proc = processing_minutes(
            job.base_processing_time_per_unit, job.quantity, machine.rate_multiplier
        )
        for worker in candidate_workers(job, snapshot):
            hard_end = min(worker.shift_end, machine.available_end)
            slot = earliest_feasible_slot(
                machine_tls[machine.machine_id],
                worker_tls[worker.worker_id],
                machine=machine,
                to_product=job.product_id,
                setup_time=job.setup_time,
                ready_at=max(ready, worker.shift_start, machine.available_start),
                duration=proc,
                hard_end=hard_end,
                changeover_rules=snapshot.changeover_rules,
            )
            if slot is None:
                continue
            # 时段级不可用：停机窗与当日缺勤在具体槽位上判定（候选枚举只做整机/技能过滤）。
            if machine.is_blocked_during(slot.start, slot.end):
                continue
            if worker.is_absent_during(slot.start, slot.end):
                continue

            cost = (
                _minutes_since(horizon_start, slot.end)
                + W_CHANGEOVER * Decimal(slot.changeover_minutes)
                + W_PREF * preference_delta(job, machine, worker, snapshot.preference_rules)
            )
            key = (cost, machine.machine_id, worker.worker_id)
            if best is None or key < best.key:
                best = _Candidate(
                    machine_id=machine.machine_id,
                    worker_id=worker.worker_id,
                    product_id=job.product_id,
                    start_time=slot.start,
                    end_time=slot.end,
                    setup_minutes=slot.setup_minutes,
                    changeover_minutes=slot.changeover_minutes,
                    key=key,
                )

    return best


# --------------------------------------------------------------------------
# 辅助
# --------------------------------------------------------------------------


def _clone(timeline: Timeline) -> Timeline:
    """复制一条时间线的当前占用，得到一条独立的临时时间线。

    主循环在整单成功前不能污染真实时间线，因此每尝试一个订单就在真实时间线的**副本**上放
    作业；订单成功则把 tentative 一次性提交到真实时间线，失败则丢弃副本即可。
    """
    clone = Timeline()
    for interval in timeline.intervals:
        clone.occupy(interval.start, interval.end, interval.product_id)
    return clone


def _horizon_start(snapshot: DomainSnapshot) -> datetime:
    """排产时域起点：生产日 00:00（`snapshot.now` 不参与，保持内核时间纯净）。

    打分里的 `minutes_since(horizon_start, slot.end)` 只需要一个**固定**的参照点，所有候选
    共用同一个，谁完工更早谁的分量更小。取生产日零点即可，它不随运行时刻变化（R5.7）。
    """
    return datetime.combine(snapshot.production_date, datetime.min.time())


def _worst_shortfall(
    shortfalls: dict[str, Decimal], snapshot: DomainSnapshot
) -> tuple[str, Decimal]:
    """从多种不足物料里挑一个作为 `MATERIAL_INSUFFICIENT` 的代表（缺口最大者，tie 按 ID）。

    主循环的失败载荷是单条 `unblock_suggestion`；缺多种物料时报缺口最大的那个最有指导性。
    完整的多物料列举留给任务 2.6 的 `quantify`。
    """
    material_id = min(shortfalls, key=lambda mid: (-shortfalls[mid], mid))
    return material_id, shortfalls[material_id]


def _unit_of(material_id: str, snapshot: DomainSnapshot) -> str:
    material = snapshot.materials_by_id().get(material_id)
    return material.unit if material is not None else ""


def _feasibility(
    scheduled: list[ScheduledJob],
    unschedulable: list[UnschedulableJob],
    freeze: tuple[ScheduledJob, ...],
) -> Feasibility:
    """可行性三态（design.md §3.1.2 / R8.1）。

    无 unschedulable → `FEASIBLE`；无（本次新排的）scheduled → `NO_FEASIBLE_PLAN`；否则
    `PARTIAL`。冻结作业不计入「本次是否排出了东西」——重排时若冻结集非空但没能再排出任何
    新作业，那仍是 `NO_FEASIBLE_PLAN`（没有可行的增量）。
    """
    if not unschedulable:
        return "FEASIBLE"
    newly_scheduled = len(scheduled) - len(freeze)
    if newly_scheduled <= 0:
        return "NO_FEASIBLE_PLAN"
    return "PARTIAL"
