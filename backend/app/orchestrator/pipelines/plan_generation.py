"""形态 A：初始计划生成的**确定性流水线**（任务 2.12，R5.1 / R21.11）。

design.md Architecture §2.1 把这条路径画成一张固定顺序的时序图，本模块就是那张图的
逐句翻译：

    load_snapshot → generate_schedule → check_constraints → evaluate_schedule
                  → compute_baseline → save_proposed_plan

**没有「LLM 选择下一个工具」的环节。** 这条序列是我们**已经知道**的（R21 第 11 条：
「初始计划生成路径实现为确定性流水线，由确定性代码按固定顺序依次调用」），让模型重新
发现它是纯粹的 token 浪费（requirements.md §"理由（token 经济学）"）。因此本阶段的
**LLM token 消耗恒为 0**——末端那次「把紧凑载荷转成解释文本」的调用属任务 5.11，本模块
只在 `explanation` 处留一个有文档的接缝，绝不发起任何网络请求。

## 六步为什么按这个顺序，而不是别的

- `load_snapshot` 先行且只读一次：`snapshot_version` 与全部实体同源（`snapshot_loader`
  的第 1 条不变量），后面五步全部读同一份冻结快照，因此整条流水线对同一份库是纯函数
  （属性 1 的前提）。
- `check_constraints` 在 `generate_schedule` 之后立即跑：排产器与校验器**独立实现**
  （design.md §3.2），这一步是「排产器有没有排出违反硬约束的作业」的独立复核。P0 的
  正常路径上它恒为零违反；留它在流水线里，是为了在排产器将来被改坏时当场红掉，而不是
  等到审批时（任务 3.1 的重校验）才发现。
- `compute_baseline` 与正式排产跑在**完全相同**的 `DomainSnapshot` 上（同一个对象，
  连 `snapshot_version` 都相等）：`BaselineResult.assert_same_version_as` 会断言这一点
  （R19.2、属性 37）。两者口径一致，KPI 差值才可信。
- `save_proposed_plan` 收尾：五张表在**同一事务**内落盘（见 `_persist`），避免「有计划头
  没有作业行」的半成品（design.md §4.4）。

## 事务与快照加载的先后

`load_snapshot` 要求一个**干净会话**（无未 flush 的改动），否则读出来的行与
`snapshot_version` 不同源。因此本模块的纪律是：**先在干净会话上加载快照并把六步算完
（全部纯内存、纯函数），最后才开始写、一次性提交。** 写入阶段不再读快照，两个阶段不
交错。

## 解释接缝（任务 5.11）

`PlanGenerationResult.explanation` 恒为 `None`：解释文本由任务 5.11 的单次 LLM 调用
产出，经任务 5.10 的数值一致性比对后才发布，并存到独立的读路径
（`GET /api/plans/{id}/explanation`）。本模块不碰它，也因此这条路径在**无 Bedrock 凭证**
时端到端可用（任务 4 的检查点）。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.baseline import BaselineResult, fcfs
from app.core.scheduler import PlanCandidate, expand, generate_schedule
from app.core.scoring import ObjectiveBreakdown, ObjectiveWeights, score
from app.core.snapshot import DomainSnapshot
from app.core.validation import ValidationReport, validate
from app.db import audit
from app.db import models as orm
from app.orchestrator.trace_recorder import DbTracer
from app.services.snapshot_loader import load_snapshot

#: 本流水线写入 `production_plans.origin` 的取值。正式计划的来源恒为 `PLAN_GENERATION`
#: （design.md Data Models §8 的状态机表；`save_proposed_plan` 校验 `origin != 'BASELINE'`）。
FORMAL_ORIGIN = "PLAN_GENERATION"

#: 基线计划的来源。它以 `status = DRAFT`、`origin = 'BASELINE'` 落库，永不进审批流
#: （design.md §3.4 / §8）。
BASELINE_ORIGIN = "BASELINE"

#: 正式提案的初始状态。**硬编码**——没有任何输入能改它（R11.8、R22.9、R23.4）。
PENDING_STATUS = "PENDING_APPROVAL"

#: 基线计划的状态。DRAFT 永不迁移到 PENDING_APPROVAL。
DRAFT_STATUS = "DRAFT"

#: Trace.kind / mode（design.md Architecture §2.1：`Trace(kind=PLAN_GENERATION, mode=PIPELINE)`）。
TRACE_KIND = "GENERATE_PLAN"
TRACE_MODE = "PIPELINE"


# --------------------------------------------------------------------------
# 结果值对象
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ProductionJobSpec:
    """一个 `production_jobs` 行的全部列，在流水线里从快照展开算出。

    `production_jobs` 是工序展开的结果表（不在 seed 里），正式计划与基线计划引用**同一批**
    job 行（`job_id` 是全局主键）。把它算成一个纯值对象在快照仍在作用域时产出，写入阶段
    只需「不存在才插」，无需回读库重建（那会既慢又要重造快照模型）。
    """

    job_id: str
    order_id: str
    product_id: str
    operation_sequence: int
    predecessor_job_id: str | None
    quantity: Decimal
    required_machine_type: str
    required_worker_skill: str


@dataclass(frozen=True)
class BaselineComparison:
    """正式计划与 FCFS 基线的同口径 KPI 对比（R5.4、R19.2）。

    六个数值全部由确定性内核算出：正式计划一组、基线一组，UI 的基线对比区据此显示
    「按期率」与「拖期分钟」的差（design.md §6 `/schedule` 行）。`snapshot_version` 是两者
    共同的输入版本号——写库前 `assert_same_version_as` 已断言它等于正式计划的
    `input_snapshot_version`。
    """

    baseline_plan_id: str
    snapshot_version: int
    on_time_rate: float
    baseline_on_time_rate: float
    total_tardiness_minutes: int
    baseline_total_tardiness_minutes: int
    late_order_count: int
    baseline_late_order_count: int


@dataclass(frozen=True)
class PlanGenerationResult:
    """一次计划生成的完整产物（供 API 序列化、供 `_persist` 落库）。

    `explanation` 恒为 `None`：解释文本是任务 5.11 的单次 LLM 调用产物，本确定性流水线
    不产出它（见模块 docstring）。字段命名对齐 R5.5 逐字列出的六项：`feasibility`、
    `scheduled_jobs`、`unschedulable_jobs`、`objective_breakdown`、`baseline_comparison`、
    `generated_by_trace_id`。
    """

    plan_id: str
    production_date: date
    status: str
    input_snapshot_version: int
    generated_by_trace_id: str
    candidate: PlanCandidate
    objective_breakdown: ObjectiveBreakdown
    validation: ValidationReport
    baseline: BaselineComparison
    #: 两份计划引用的全部工序展开行（`job_id → ProductionJobSpec`）。在流水线里、快照仍
    #: 在作用域内时算出，写入阶段直接落库，无需回读库重建（见 `_persist`）。
    production_jobs: tuple[ProductionJobSpec, ...]

    @property
    def feasibility(self) -> str:
        return self.candidate.feasibility


# --------------------------------------------------------------------------
# KPI（同口径，供基线对比）
# --------------------------------------------------------------------------


def _plan_kpis(plan: PlanCandidate, snapshot: DomainSnapshot) -> tuple[float, int, int]:
    """`(on_time_rate, total_tardiness_minutes, late_order_count)`。

    只统计**有已排产作业**的订单（每单以其最晚 `end_time` 为完工时刻）：未排产订单进
    `unschedulable_jobs`，它的「迟交」没有完工时刻可谈，归 R8 的不可排产清单，不在这里
    重复记账（与 `Objective_Scorer._late_and_tardiness` 同一取舍）。

    `on_time_rate` 的分母是「被排上的订单数」而不是「全部订单数」：基线与正式计划都用同一
    口径，差值才可比。分母为 0（无任何作业排上）时按期率取 1.0——没有订单迟交（因为没有
    订单被排），这与「迟交订单数 = 0」一致。
    """
    orders_by_id = snapshot.orders_by_id()

    completions: dict[str, datetime] = {}
    for job in plan.scheduled_jobs:
        current = completions.get(job.order_id)
        if current is None or job.end_time > current:
            completions[job.order_id] = job.end_time

    total_tardiness = 0
    late_count = 0
    for order_id, completion in completions.items():
        order = orders_by_id.get(order_id)
        if order is None:
            continue
        if completion > order.due_date:
            late_count += 1
            total_tardiness += int((completion - order.due_date).total_seconds() // 60)

    scheduled_order_count = len(completions)
    if scheduled_order_count == 0:
        on_time_rate = 1.0
    else:
        on_time_rate = (scheduled_order_count - late_count) / scheduled_order_count
    return on_time_rate, total_tardiness, late_count


# --------------------------------------------------------------------------
# 流水线主入口
# --------------------------------------------------------------------------


def run_plan_generation(
    session: Session,
    *,
    production_date: date | None = None,
    now: datetime,
    actor: str = "PLANNER",
    session_id: str,
    trigger_source: str = "PLANNER_UI",
    weights: ObjectiveWeights | None = None,
) -> PlanGenerationResult:
    """执行固定 6 步序列并把结果落库（一个事务），返回 `PlanGenerationResult`。

    `now` 是必填关键字参数（内核可重现性的入口条件，见 `load_snapshot`）。`weights` 缺省
    取 `ObjectiveWeights()` 的默认值（design.md §3.3）。

    调用方持有会话；本函数在成功路径末尾 `commit()`，异常时 `rollback()` 后重抛——由
    `save_proposed_plan` 的五表写入构成一个原子单位。传入的会话必须是干净的（无未 flush
    的改动），否则快照与版本号不同源（`load_snapshot` 会拒绝）。

    可能抛出的内核异常（由 API 边界翻译成错误响应）：
    - `DataIntegrityError`（`load_snapshot` 引用完整性预检失败，R5.6）；
    - `InvalidRoutingError`（`generate_schedule` 遇到非法路线，R4.7）。

    这两个异常都在**写入之前**抛出，因此不会留下任何半成品行。
    """
    resolved_weights = weights if weights is not None else ObjectiveWeights()

    # ---- 步 1：load_snapshot（只读，单事务，版本号与内容同源） ----
    snapshot = load_snapshot(session, now=now, production_date=production_date)

    # ---- 步 2：generate_schedule（确定性全序放置） ----
    candidate = generate_schedule(snapshot)

    # ---- 步 3：check_constraints（与排产器独立实现的复核） ----
    validation = validate(candidate, snapshot)

    # ---- 步 4：evaluate_schedule（纯确定性评分） ----
    breakdown = score(candidate, snapshot, resolved_weights)

    # ---- 步 5：compute_baseline（同一快照上的 FCFS，同口径断言） ----
    baseline_result = fcfs(snapshot)
    baseline_result.assert_same_version_as(snapshot.snapshot_version)

    # 工序展开行：趁快照仍在作用域，一次性算出两份计划引用的全部 job 行（纯函数）。
    production_jobs = _expand_production_jobs(snapshot, candidate, baseline_result.plan)

    # ---- 步 6：save_proposed_plan（五表同事务落盘，状态硬编码） ----
    # 至此 LLM token 消耗 = 0（design.md Architecture §2.1 的 Note）。
    plan_id = _new_plan_id()
    baseline_plan_id = _new_plan_id()

    # 开一个真实的 Trace（任务 5.12 的 `Trace_Recorder`）：`begin` 立即写一行 `traces`
    # 并 flush，使 `production_plans.generated_by_trace_id` 的外键有指向。trace_id 由记录器
    # 生成，正式与基线两个计划头都引用它（R24.5）。这条路径是流水线（`mode = PIPELINE`、
    # `agent = NULL`），token 汇总恒为 0——流水线确实不触达 Bedrock（design.md §2.1 Note）。
    tracer = DbTracer(session, trigger_source=trigger_source, session_id=session_id, now=now)
    trace = tracer.begin(kind=TRACE_KIND, mode=TRACE_MODE, agent=None)
    trace_id = trace.trace_id

    formal_on_time, formal_tardiness, formal_late = _plan_kpis(candidate, snapshot)
    base_on_time, base_tardiness, base_late = _plan_kpis(baseline_result.plan, snapshot)
    baseline_comparison = BaselineComparison(
        baseline_plan_id=baseline_plan_id,
        snapshot_version=snapshot.snapshot_version,
        on_time_rate=formal_on_time,
        baseline_on_time_rate=base_on_time,
        total_tardiness_minutes=formal_tardiness,
        baseline_total_tardiness_minutes=base_tardiness,
        late_order_count=formal_late,
        baseline_late_order_count=base_late,
    )

    result = PlanGenerationResult(
        plan_id=plan_id,
        production_date=snapshot.production_date,
        status=PENDING_STATUS,
        input_snapshot_version=snapshot.snapshot_version,
        generated_by_trace_id=trace_id,
        candidate=candidate,
        objective_breakdown=breakdown,
        validation=validation,
        baseline=baseline_comparison,
        production_jobs=production_jobs,
    )

    try:
        # Trace 行已在 `tracer.begin` 里写下并 flush（外键指向它）。记录固定 6 步为
        # `trace_steps`（`step_kind = DETERMINISTIC_STAGE`，`decision_reason` 为阶段名的
        # 结构化摘要而非推理链，R24.7），使 `/traces` 详情能逐步回看这条确定性路径。
        _record_pipeline_steps(tracer, trace)
        tracer.set_result_ref(trace, plan_id)
        tracer.end(trace)
        _persist(session, result=result, baseline=baseline_result, weights=resolved_weights)
        session.commit()
    except Exception:
        session.rollback()
        raise

    # 审计走独立连接（`db/audit.py`），不参与业务事务；提交成功后写。
    audit.append(
        event_category="PLAN_GENERATION",
        event_type="PLAN_GENERATED",
        actor=actor,
        payload={
            "plan_id": plan_id,
            "production_date": snapshot.production_date.isoformat(),
            "feasibility": candidate.feasibility,
            "scheduled_job_count": len(candidate.scheduled_jobs),
            "unschedulable_job_count": len(candidate.unschedulable_jobs),
            "input_snapshot_version": snapshot.snapshot_version,
        },
        subject_type="ProductionPlan",
        subject_id=plan_id,
        trace_id=trace_id,
        occurred_at=now,
    )
    return result


# --------------------------------------------------------------------------
# 持久化（五表 + 基线计划头，同一事务）
# --------------------------------------------------------------------------


def _persist(
    session: Session,
    *,
    result: PlanGenerationResult,
    baseline: BaselineResult,
    weights: ObjectiveWeights,
) -> None:
    """把正式计划的五张表与基线计划一并写入。**不提交**——调用方持有事务边界。

    五张表（design.md §4.4）：`production_plans` + `scheduled_jobs` + `unschedulable_jobs`
    + `objective_breakdowns` + `baseline_comparisons`。它们要么全部落盘、要么全部不落盘，
    否则会出现「有计划头没有作业行」的半成品，而 UI 与审批都会把那种半成品当成一份真实
    计划。

    基线计划本身也是 `production_plans` 的一行（`status = DRAFT`、`origin = 'BASELINE'`），
    在这里一并写，使 `baseline_comparisons.baseline_plan_id` 的外键有指向。基线不进审批流。

    `production_jobs` 是工序展开的结果表，正式计划与基线计划引用**同一批** job 行
    （`job_id` 是全局主键）。因此这里对每个 job 做「不存在才插」——重复生成计划、或
    同一份 job 同时被两个计划引用，都不该撞主键。
    """
    _ensure_production_jobs(session, specs=result.production_jobs)

    # 两个计划头先落库并 flush：`scheduled_jobs` / `baseline_comparisons` 的外键都指向
    # `production_plans`，SQLite 在 `foreign_keys = ON` 下逐条 INSERT 就检查外键，因此被
    # 引用的计划头必须先在库里。先 add 两个头再 flush，比依赖 SQLAlchemy 对无 relationship
    # 的自引用表做拓扑排序更可靠——那条路径在有自引用 FK（`supersedes_plan_id`）时会把子行
    # 排到父行之前。`_ensure_production_jobs` 也已在此前 add，flush 一并把 job 行写入，
    # 使 `scheduled_jobs.job_id` 的外键有指向。
    session.add(
        orm.ProductionPlan(
            plan_id=result.baseline.baseline_plan_id,
            production_date=result.production_date,
            status=DRAFT_STATUS,
            feasibility=baseline.plan.feasibility,
            plan_version=1,
            version=1,
            input_snapshot_version=result.input_snapshot_version,
            origin=BASELINE_ORIGIN,
            generated_by_trace_id=result.generated_by_trace_id,
            created_at=_created_at(result),
        )
    )
    session.add(
        orm.ProductionPlan(
            plan_id=result.plan_id,
            production_date=result.production_date,
            status=PENDING_STATUS,
            feasibility=result.candidate.feasibility,
            plan_version=1,
            version=1,
            input_snapshot_version=result.input_snapshot_version,
            origin=FORMAL_ORIGIN,
            generated_by_trace_id=result.generated_by_trace_id,
            created_at=_created_at(result),
        )
    )
    session.flush()

    # 基线计划只写作业行（它不进审批流，无需 objective / baseline 明细）。
    _add_scheduled_jobs(session, plan_id=result.baseline.baseline_plan_id, candidate=baseline.plan)

    # 正式计划的四张明细表。状态由 `save_proposed_plan` 硬编码 PENDING_APPROVAL，且校验
    # origin != BASELINE 且 produced_in_sandbox == false（design.md §8、任务 3.3）。
    save_proposed_plan(
        session,
        result=result,
        weights=weights,
        origin=FORMAL_ORIGIN,
        produced_in_sandbox=False,
    )


class SaveProposedPlanRejected(Exception):
    """`save_proposed_plan` 拒绝一次非法的「提案落库」尝试（design.md §8、R11.8 / R23.4）。

    这是一个**编程错误信号**而非业务性拒绝：能走到 `save_proposed_plan` 的只有确定性流水线
    与（P1 的）`Planning_Agent` 提案路径，两者都在代码里硬编码 `origin=FORMAL_ORIGIN` 与
    `produced_in_sandbox=False`。因此本异常被触发只可能意味着某段新代码试图把一个基线计划
    或一份沙箱推演结果推进审批流——那是 §8 状态机明令禁止的迁移（`BASELINE` / 沙箱候选
    永不迁移到 `PENDING_APPROVAL`）。让它在写入前当场抛出，而不是留下一个能被审批的脏行。
    """


def save_proposed_plan(
    session: Session,
    *,
    result: PlanGenerationResult,
    weights: ObjectiveWeights,
    origin: str,
    produced_in_sandbox: bool,
) -> None:
    """写正式提案的四张明细表，把 `DRAFT/候选 → PENDING_APPROVAL` 这次迁移的前置条件钉死。

    design.md §8「计划状态机」把 `DRAFT → PENDING_APPROVAL` 的执行组件唯一指定为
    `save_proposed_plan`，并在表下补了一句硬约束：

    > `DRAFT` 状态的 `BASELINE` 计划与沙箱产生的候选计划永不迁移到 `PENDING_APPROVAL`：
    > `save_proposed_plan` 的实现校验 `origin != 'BASELINE'` 且 `plan.produced_in_sandbox == false`。

    本函数就是那道校验（任务 3.3）。三件事：

    1. **`origin != 'BASELINE'`**：基线计划以 `status = DRAFT`、`origin = 'BASELINE'` 存在，
       只用于同口径 KPI 对比，**永不进审批流**。若 `origin == 'BASELINE'`，抛
       `SaveProposedPlanRejected`——绝不把基线推进成待审批提案。
    2. **`produced_in_sandbox == False`**：沙箱推演（P1 的 `Scenario_Sandbox`）产出的候选是
       「假如……会怎样」的推算，不是真实提案。它同样绝不进审批流。P0 没有沙箱，这个入参
       恒为 `False`；把校验此刻就放进来，是为了 P1 的沙箱落地时这条护栏已经在位，而不是
       事后补。
    3. **`status` 硬编码 `PENDING_APPROVAL`**：本函数**没有 `status` 入参**。提案的状态不是
       调用方能选的——它只能是 `PENDING_APPROVAL`（R11.8 / R22.9 / R23.4）。计划头由
       `_persist` 用常量 `PENDING_STATUS` 写成；这里再守一道运行期断言，防止将来有人给
       `result` 塞了别的状态就绕过了 `_persist` 的常量。

    这三条合起来，使「把一个不该被审批的计划推进 `PENDING_APPROVAL`」在这条路径上要么类型
    层面不可能（没有 `status` 参数），要么写入前当场抛出（`origin` / `produced_in_sandbox`）。

    计划头已在 `_persist` 里以 `PENDING_STATUS` / `origin` 落库；本函数只写四张明细表。
    """
    # ---- §8 前置条件校验（写任何明细行之前） ----
    if origin == BASELINE_ORIGIN:
        raise SaveProposedPlanRejected(
            f"基线计划（origin={origin!r}）永不进审批流，不能经 save_proposed_plan 落成 "
            f"{PENDING_STATUS}（design.md §8）"
        )
    if produced_in_sandbox:
        raise SaveProposedPlanRejected(
            "沙箱推演产生的候选永不进审批流，不能经 save_proposed_plan 落成 "
            f"{PENDING_STATUS}（design.md §8）"
        )
    if result.status != PENDING_STATUS:
        raise ValueError(f"正式提案状态只能是 {PENDING_STATUS}，收到 {result.status!r}")

    _add_scheduled_jobs(session, plan_id=result.plan_id, candidate=result.candidate)

    for uj in result.candidate.unschedulable_jobs:
        session.add(
            orm.UnschedulableJob(
                id=_new_row_id(),
                plan_id=result.plan_id,
                job_id=uj.job_id,
                blocking_reason=uj.blocking_reason,
                unblock_suggestion=uj.unblock_suggestion,
            )
        )

    breakdown = result.objective_breakdown
    session.add(
        orm.ObjectiveBreakdown(
            plan_id=result.plan_id,
            components=[component.model_dump(mode="json") for component in breakdown.components],
            total_score=Decimal(str(breakdown.total_score)),
            weights=weights.model_dump(mode="json"),
            preference_contributions=list(breakdown.preference_contributions),
            weight_overrides_applied=list(breakdown.weight_overrides_applied),
        )
    )

    bc = result.baseline
    session.add(
        orm.BaselineComparison(
            plan_id=result.plan_id,
            baseline_plan_id=bc.baseline_plan_id,
            snapshot_version=bc.snapshot_version,
            on_time_rate=Decimal(str(bc.on_time_rate)),
            baseline_on_time_rate=Decimal(str(bc.baseline_on_time_rate)),
            total_tardiness_minutes=bc.total_tardiness_minutes,
            baseline_total_tardiness_minutes=bc.baseline_total_tardiness_minutes,
            late_order_count=bc.late_order_count,
            baseline_late_order_count=bc.baseline_late_order_count,
        )
    )


def _add_scheduled_jobs(session: Session, *, plan_id: str, candidate: PlanCandidate) -> None:
    for sj in candidate.scheduled_jobs:
        session.add(
            orm.ScheduledJob(
                scheduled_job_id=_new_row_id(),
                plan_id=plan_id,
                job_id=sj.job_id,
                machine_id=sj.machine_id,
                worker_id=sj.worker_id,
                start_time=sj.start_time,
                end_time=sj.end_time,
                setup_minutes=sj.setup_minutes,
                changeover_minutes=sj.changeover_minutes,
            )
        )


def _expand_production_jobs(
    snapshot: DomainSnapshot,
    candidate: PlanCandidate,
    baseline_plan: PlanCandidate,
) -> tuple[ProductionJobSpec, ...]:
    """把两份计划引用的订单展开成 `ProductionJobSpec`（趁快照在作用域，纯函数）。

    用内核的 `expand` 保证 `job_id` 与排产结果一致（`"{order_id}-OP{sequence}"`）。只展开
    被两份计划实际引用到的订单，得到的 spec 集合是两份计划全部 `job_id` 的并集——写入阶段
    对每个 `job_id` 做「不存在才插」，因此顺序与去重都在这里完成。
    """
    products = snapshot.products_by_id()
    orders_by_id = snapshot.orders_by_id()

    referenced_orders: set[str] = set()
    for plan in (candidate, baseline_plan):
        for sj in plan.scheduled_jobs:
            referenced_orders.add(sj.order_id)
        for uj in plan.unschedulable_jobs:
            referenced_orders.add(uj.order_id)

    specs: dict[str, ProductionJobSpec] = {}
    for order_id in referenced_orders:
        order = orders_by_id.get(order_id)
        if order is None:
            continue
        product = products.get(order.product_id)
        if product is None:
            continue
        for job in expand(order, product):
            specs[job.job_id] = ProductionJobSpec(
                job_id=job.job_id,
                order_id=job.order_id,
                product_id=job.product_id,
                operation_sequence=job.operation_sequence,
                predecessor_job_id=job.predecessor_job_id,
                quantity=job.quantity,
                required_machine_type=job.required_machine_type,
                required_worker_skill=job.required_worker_skill,
            )
    return tuple(specs[job_id] for job_id in sorted(specs))


def _ensure_production_jobs(session: Session, *, specs: tuple[ProductionJobSpec, ...]) -> None:
    """确保每个 `production_jobs` 行都存在（不存在才插）。

    `production_jobs.job_id` 是全局主键，重复生成计划得到的是**相同的** job 行，重复插入会
    撞主键。因此先查已存在的一批，再只插缺的。`predecessor_job_id` 是自引用外键——按
    `operation_sequence` 升序插入，前序行总先于后序行落库。
    """
    if not specs:
        return

    existing = set(
        session.execute(
            select(orm.ProductionJob.job_id).where(
                orm.ProductionJob.job_id.in_([spec.job_id for spec in specs])
            )
        ).scalars()
    )

    for spec in sorted(specs, key=lambda s: (s.order_id, s.operation_sequence)):
        if spec.job_id in existing:
            continue
        session.add(
            orm.ProductionJob(
                job_id=spec.job_id,
                order_id=spec.order_id,
                product_id=spec.product_id,
                operation_sequence=spec.operation_sequence,
                predecessor_job_id=spec.predecessor_job_id,
                quantity=spec.quantity,
                required_machine_type=spec.required_machine_type,
                required_worker_skill=spec.required_worker_skill,
            )
        )


# --------------------------------------------------------------------------
# Trace 记录（任务 5.12 的 `Trace_Recorder`，写 traces + trace_steps）
# --------------------------------------------------------------------------

#: 流水线固定 6 步的阶段名，逐字对应 design.md Architecture §2.1 的时序图。它们作为
#: `trace_steps.decision_reason` 的结构化摘要写入（`step_kind = DETERMINISTIC_STAGE`），
#: 使 `/traces` 详情能逐步回看这条确定性路径「按固定顺序做了哪 6 件事」（R24.1、R24.7）。
_PIPELINE_STAGES: tuple[str, ...] = (
    "load_snapshot",
    "generate_schedule",
    "check_constraints",
    "evaluate_schedule",
    "compute_baseline",
    "save_proposed_plan",
)


def _record_pipeline_steps(tracer: DbTracer, trace: object) -> None:
    """把固定 6 步记成 `trace_steps`，并把 trace 的 `outcome` 定型为成功（`OK`）。

    这条路径是**确定性流水线**：6 步的顺序在编译期就定死（`_PIPELINE_STAGES`），没有
    「LLM 选择下一个工具」的环节（R21.11）。因此这里不测每步真实耗时的分支——`DbTracer`
    的单调时钟会给出各步间隔，逐步 token 恒为 0（流水线不触达 Bedrock，design.md §2.1）。

    `outcome = OK`：流水线走到这里意味着六步全部完成、五表即将同事务落盘，是一次成功收尾
    （`traces.outcome` 取值域见 design.md §7，成功为 `OK`）。`tracer.end` 会把它落库。
    """
    from app.orchestrator.tracing import TraceHandle

    assert isinstance(trace, TraceHandle)
    for stage in _PIPELINE_STAGES:
        tracer.record_step(
            trace, step_kind="DETERMINISTIC_STAGE", outcome="OK", detail=stage
        )
    trace.outcome = "OK"


# --------------------------------------------------------------------------
# ID 生成
# --------------------------------------------------------------------------


def _new_plan_id() -> str:
    """可读的计划 ID。计划 ID 不参与任何确定性断言（属性 1 断言的是排产结果，不是行 ID），
    因此用随机后缀安全。"""
    return f"PLAN-{uuid.uuid4().hex[:12]}"


def _new_row_id() -> str:
    return f"ROW-{uuid.uuid4().hex[:16]}"


def _created_at(result: PlanGenerationResult) -> datetime:
    """计划头的 `created_at`。取 trace 里的时刻口径的替身：用当前墙上时钟即可，它不进
    排产算术，也不参与确定性断言。"""
    return datetime.now()  # noqa: DTZ005
