"""`Baseline_Scheduler`：确定性 FCFS 基线排产器（任务 2.11，R19.2 / R19.3 / R5.4）。

design.md §3.4 把这个组件的定位写成一句话：**模拟"人工用 Excel 按交期排"的今天**。它是价值
台账（`Value_Ledger`）的对照系——正式计划（`Scheduling_Core.generate_schedule`）在**完全相同
的 `DomainSnapshot`** 上跑，两者的 KPI（按期率、拖期分钟）同口径相减，那个差值就是系统的业务
价值（R19.2、K-03/K-04）。要让这个差值可信，基线必须满足两条：

1. **同输入同口径**：与正式计划消费的是同一个冻结快照，`snapshot_version` 必须相等
   （R19.2）。这个相等性是 Property 37（任务 11.5）的核心断言，也是 `baseline_comparisons`
   表的一列约束（design.md §5 DDL：`snapshot_version` 断言与 `plan.input_snapshot_version`
   相等）。持久化落在服务层（见下方"持久化缝"一节），但**版本号本身来自快照**，因此纯排产
   函数把它原样透传出来，服务层无需再猜。
2. **刻意退化**：基线不是"另一个优化器"，而是"没有系统时的排法"。它与 `Scheduling_Core`
   共享 `earliest_feasible_slot`、`Timeline` 与工序展开/物料语义，但在三处**故意变笨**
   （design.md §3.4 的伪码逐条对应）：

   ① **忽略 `priority`**：订单只按 `(due_date, order_id)` 升序排。正式计划先按
      `PRIORITY_RANK` 再按交期（`scheduler.PRIORITY_RANK`）——人工排表时没人先把 URGENT
      单挑出来，就是从早到晚照交期填。这一处退化正是 Property 37 后半段断言的：**仅置换
      `priority` 值不改变基线结果**。

   ② **不比较候选**：每道工序取 `candidate_machines` / `candidate_workers` 里 **ID 最小**
      的那台机器 / 那个工人，不做 `_best_candidate` 的 `cost` 打分。人工排表时挑的是"手边
      第一台能干的机器"，不会为了省几分钟换型去枚举所有组合。

   ③ **不做换型优化打分**：换型时间照常由 `earliest_feasible_slot` **物理插入**——换型是
      客观的物理约束（换产品要重新装夹），不是可以优化掉的选项。基线不为了减少换型而调整
      顺序或选型，它只是接受换型带来的时间损失。

## 基线也不应用任何 `PreferenceRule`

正式计划的候选打分里有 `W_PREF × preference_delta` 一项（`scheduler._best_candidate`）。基线
**根本不打分**，因此偏好自然不参与——但这里要把它写成一条明确的纪律而非巧合：即使将来有人
给基线加回某种选型逻辑，也不能让偏好进来。偏好是"这个规划员的规矩"，而基线模拟的是"没有这套
规矩、没有这套系统"的世界，让偏好影响基线会污染对照。

## 与 `Scheduling_Core` 的关系：复用可行性，退化选择

可行性判定（一个槽位物理上排不排得下）对基线和正式计划是同一件事——都靠
`earliest_feasible_slot` + `Timeline`。差别只在**选择**：正式计划在所有可行候选里挑 `cost`
最小的，基线在可行候选里挑 ID 最小的。因此本模块复用 `scheduler` 的：

- `expand` —— 工序展开与线性前序链（同一套 `job_id` 规则，R4.2）；
- `candidate_machines` / `candidate_workers` —— 候选过滤（类型/能力/状态/技能），基线只是
  不再对过滤结果打分而已；
- `material_need` / `material_ready_time` —— 物料语义（design.md §3.1.5），基线与正式计划
  对物料的处理必须一致，否则"基线更差"可能是物料算法不同造成的假象；
- `ProductionJob` / `ScheduledJob` / `PlanCandidate` / `UnschedulableJob` / `Failure` /
  `diagnose_blocking` / `quantify` —— 值对象与失败诊断，基线的 unschedulable 也要带
  `blocking_reason`（R8.4）。

订单级原子性（一单要么全排上、要么全进 unschedulable）与正式计划一致：半个订单在车间里是
负价值，基线也不例外。

## 持久化缝：纯排产在这里，落库在服务层

本模块是**内核纯函数**——不 import `sqlalchemy` / `fastapi`，不读时钟，不碰 I/O
（`test_layering.py` / `test_kernel_time_purity.py` 会断言）。design.md §5 要求基线计划：

- 存为 `production_plans` 行，`status = 'DRAFT'`、`origin = 'BASELINE'`，**永不进审批流**
  （`save_proposed_plan` 校验 `origin != 'BASELINE'`，见 design.md 状态机说明）；
- 其对比结果写 `baseline_comparisons`，其中 `snapshot_version` 必须等于正式计划的
  `input_snapshot_version`（同口径断言）。

这些都属于**服务层**（随任务 2.12 `POST /api/plans/generate` 的流水线落地）。纯函数
`fcfs(snapshot)` 返回 `BaselineResult`，把 `snapshot_version` 原样透传——服务层据此写库并
断言两个版本相等。这样纯排产逻辑可被单元测试完整覆盖（无需数据库），而"存哪张表、什么
status/origin"这类持久化决策留在有库的那一层。`BaselineResult.assert_same_version_as` 把
那条不变量做成一个可在服务层直接调用的断言方法，Property 37 也复用它。
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict

from app.core.scheduler import (
    Failure,
    Feasibility,
    PlanCandidate,
    ProductionJob,
    ScheduledJob,
    UnschedulableJob,
    candidate_machines,
    candidate_workers,
    diagnose_blocking,
    expand,
    material_need,
    material_ready_time,
    quantify,
)
from app.core.scheduling import Timeline, earliest_feasible_slot, processing_minutes
from app.core.snapshot import DomainSnapshot, Order, Product


class BaselineResult(BaseModel):
    """`fcfs` 的返回值：基线 `PlanCandidate` + 其所依赖快照的 `snapshot_version`（R19.2）。

    `snapshot_version` 原样来自入参快照，不是 `fcfs` 计算出来的——它存在的唯一目的是把"这份
    基线跑在哪个版本的输入上"透传给服务层，让服务层在写 `baseline_comparisons.snapshot_version`
    时能断言它等于正式计划的 `input_snapshot_version`（design.md §5 同口径约束）。

    `frozen=True`：与内核其余值对象一致，结果一经产出不可变。
    """

    model_config = ConfigDict(frozen=True)

    plan: PlanCandidate
    snapshot_version: int

    def assert_same_version_as(self, formal_snapshot_version: int) -> None:
        """断言基线与正式计划跑在同一版本输入上（R19.2、Property 37 的核心不变量）。

        服务层在写 `baseline_comparisons` 前调用它；不相等即抛 `ValueError`——那意味着基线与
        正式计划的输入口径不同，二者的 KPI 差值不再可信，宁可炸掉也不能写进台账。
        """
        if self.snapshot_version != formal_snapshot_version:
            raise ValueError(
                "基线与正式计划的 snapshot_version 不一致："
                f"基线 {self.snapshot_version} != 正式 {formal_snapshot_version}；"
                "同口径对比要求二者跑在完全相同的 DomainSnapshot 上（R19.2）"
            )


def fcfs(snapshot: DomainSnapshot) -> BaselineResult:
    """在冻结快照上产出 FCFS 基线计划（design.md §3.4、R19.3）。

    与 `generate_schedule` 同为确定性全序放置、订单原子提交，但三处刻意退化（见模块 docstring）：
    ① 订单只按 `(due_date, order_id)` 排（忽略 `priority`）；② 每道工序取 ID 最小的可行机器/
    工人（不打分比较）；③ 不做换型优化（换型由 `earliest_feasible_slot` 物理插入）。

    不应用任何 `PreferenceRule`。返回 `BaselineResult`，`snapshot_version` 原样透传（R19.2）。
    """
    products = snapshot.products_by_id()

    machine_tls: dict[str, Timeline] = {m.machine_id: Timeline() for m in snapshot.machines}
    worker_tls: dict[str, Timeline] = {w.worker_id: Timeline() for w in snapshot.workers}
    # 本次基线排产的滚动物料预留，叠加在快照初始 reserved_quantity 之上（同 generate_schedule）。
    reserved: dict[str, Decimal] = {}

    # ① 退化：只按 (due_date, order_id) 排，忽略 priority。
    order_seq = sorted(snapshot.orders, key=lambda o: (o.due_date, o.order_id))

    scheduled: list[ScheduledJob] = []
    unschedulable: list[UnschedulableJob] = []

    for order in order_seq:
        product = products[order.product_id]  # 引用完整性由加载期预检保证
        jobs = expand(order, product)

        tentative, failure, failed_job_id = _place_order_fcfs(
            order, jobs, product, snapshot, machine_tls, worker_tls, reserved
        )

        if failure is None:
            for sj in tentative:
                machine_tls[sj.machine_id].occupy(sj.start_time, sj.end_time, sj.product_id)
                worker_tls[sj.worker_id].occupy(sj.start_time, sj.end_time, sj.product_id)
            for material_id, qty in material_need(product, order.quantity).items():
                reserved[material_id] = reserved.get(material_id, Decimal("0")) + qty
            scheduled.extend(tentative)
        else:
            _emit_unschedulable(
                jobs, order.order_id, failure, failed_job_id, snapshot, unschedulable
            )

    plan = PlanCandidate(
        scheduled_jobs=tuple(scheduled),
        unschedulable_jobs=tuple(unschedulable),
        feasibility=_feasibility(scheduled, unschedulable),
    )
    return BaselineResult(plan=plan, snapshot_version=snapshot.snapshot_version)


# --------------------------------------------------------------------------
# 订单放置（原子单位，与 generate_schedule 同结构，但选型退化）
# --------------------------------------------------------------------------


def _place_order_fcfs(
    order: Order,
    jobs: tuple[ProductionJob, ...],
    product: Product,
    snapshot: DomainSnapshot,
    machine_tls: dict[str, Timeline],
    worker_tls: dict[str, Timeline],
    reserved: dict[str, Decimal],
) -> tuple[list[ScheduledJob], Failure | None, str | None]:
    """把一个订单的全部工序放进 `tentative`；任一工序失败即整单回滚（design.md §3.4 / §3.1.6）。

    与 `scheduler._place_order` 同构——只读临时时间线、物料齐备时刻先判、逐工序线性前序链、
    失败带 `Failure` 与失败作业 id——唯一差别是每道工序调 `_first_feasible_placement`（退化 ②③）
    而非 `_best_candidate`（打分选优）。
    """
    tmp_machine: dict[str, Timeline] = {mid: _clone(tl) for mid, tl in machine_tls.items()}
    tmp_worker: dict[str, Timeline] = {wid: _clone(tl) for wid, tl in worker_tls.items()}

    need = material_need(product, order.quantity)
    shift_start = _horizon_start(snapshot)
    ready_material, shortfalls = material_ready_time(need, snapshot, reserved, shift_start)
    if ready_material is None:
        first_job_id = jobs[0].job_id if jobs else None
        return (
            [],
            Failure(reason="MATERIAL_INSUFFICIENT", material_shortfalls=shortfalls),
            first_job_id,
        )

    tentative: list[ScheduledJob] = []
    predecessor_end: datetime | None = None

    for job in jobs:
        # ready = max(班次起点, 前序结束, 物料齐备)（design.md §3.1.2，基线沿用同一下界）。
        ready = shift_start
        if predecessor_end is not None and predecessor_end > ready:
            ready = predecessor_end
        if ready_material > ready:
            ready = ready_material

        placement = _first_feasible_placement(job, snapshot, tmp_machine, tmp_worker, ready)
        if placement is None:
            failure = diagnose_blocking(
                job, snapshot, tmp_machine, tmp_worker, frozenset(), ready=ready
            )
            return tentative, failure, job.job_id

        sj = placement
        tentative.append(sj)
        tmp_machine[sj.machine_id].occupy(sj.start_time, sj.end_time, sj.product_id)
        tmp_worker[sj.worker_id].occupy(sj.start_time, sj.end_time, sj.product_id)
        predecessor_end = sj.end_time

    return tentative, None, None


def _first_feasible_placement(
    job: ProductionJob,
    snapshot: DomainSnapshot,
    machine_tls: dict[str, Timeline],
    worker_tls: dict[str, Timeline],
    ready: datetime,
) -> ScheduledJob | None:
    """退化 ②③：取 ID 最小的可行「机器 × 工人」，不打分、不做换型优化（design.md §3.4）。

    `candidate_machines` / `candidate_workers` 已按 `machine_id` / `worker_id` 升序返回。基线
    按这个顺序遍历，**第一个**能在 `ready` 之后、班次内放下这道工序（且槽位不落在停机窗/缺勤
    窗内）的组合即被采用——不像 `_best_candidate` 那样枚举全部组合再按 `cost` 取最优。

    换型（退化 ③）不在这里"优化"：`earliest_feasible_slot` 照常按 `changeover_rules` 把换型
    时间物理插入到槽位里（`setup_minutes = setup_time + changeover`）。基线接受换型损失，只是
    不为减少它而挑机器或调顺序。

    偏好规则一律不参与——基线不打分，`preference_delta` 自然不出现，且不得以任何形式加回。

    候选机器/工人为空（无能力机器 / 无技能工人 / 全部不可用）→ 返回 `None`，由调用方交给
    `diagnose_blocking` 归因。
    """
    for machine in candidate_machines(job, snapshot, frozenset()):
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
            if machine.is_blocked_during(slot.start, slot.end):
                continue
            if worker.is_absent_during(slot.start, slot.end):
                continue
            # 第一个可行组合即采用（ID 最小），不比较其余候选。
            return ScheduledJob(
                job_id=job.job_id,
                order_id=job.order_id,
                product_id=job.product_id,
                machine_id=machine.machine_id,
                worker_id=worker.worker_id,
                start_time=slot.start,
                end_time=slot.end,
                setup_minutes=slot.setup_minutes,
                changeover_minutes=slot.changeover_minutes,
            )
    return None


def _emit_unschedulable(
    jobs: tuple[ProductionJob, ...],
    order_id: str,
    failure: Failure,
    failed_job_id: str | None,
    snapshot: DomainSnapshot,
    out: list[UnschedulableJob],
) -> None:
    """整单落空时给每道工序一条 `UnschedulableJob`（R8.4，与 generate_schedule 同规则）。

    失败工序带真实 `reason` + `quantify` 的量化建议；同单其余工序因订单原子性一并落空，
    标注 `OPERATION_PRECEDENCE_VIOLATION` 指向失败工序。
    """
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
        out.append(
            UnschedulableJob(
                job_id=job.job_id,
                order_id=order_id,
                blocking_reason=reason,
                unblock_suggestion=suggestion,
            )
        )


# --------------------------------------------------------------------------
# 辅助（与 scheduler 私有辅助同义，此处独立实现以免内核间 import 私有符号）
# --------------------------------------------------------------------------


def _clone(timeline: Timeline) -> Timeline:
    """复制一条时间线的当前占用，得到独立的临时时间线（订单成功前不污染真实时间线）。"""
    clone = Timeline()
    for interval in timeline.intervals:
        clone.occupy(interval.start, interval.end, interval.product_id)
    return clone


def _horizon_start(snapshot: DomainSnapshot) -> datetime:
    """排产时域起点：生产日 00:00（不读 `snapshot.now`，保持内核时间纯净，R5.7）。"""
    return datetime.combine(snapshot.production_date, datetime.min.time())


def _feasibility(
    scheduled: list[ScheduledJob], unschedulable: list[UnschedulableJob]
) -> Feasibility:
    """可行性三态（design.md §3.1.2 / R8.1）：无 unschedulable → FEASIBLE；无 scheduled →
    NO_FEASIBLE_PLAN；否则 PARTIAL。基线无冻结集，故直接看两份列表是否为空。"""
    if not unschedulable:
        return "FEASIBLE"
    if not scheduled:
        return "NO_FEASIBLE_PLAN"
    return "PARTIAL"
