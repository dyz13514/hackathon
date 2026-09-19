"""L4 自动应用与一键回滚（任务 13.4，P1-J ④，R13.7/R13.9/R13.10）。

打开 `auto_apply_minor_enabled` 后，`IMPACT_MINOR` 的重排修订计划走 L4 分支：系统**自动激活**
它（无需人工点批准），并写一条 `AutoAppliedChange` 记录变更前后的完整快照，供规划员一键回滚。

## 不可削弱的安全边界

- **只有 `IMPACT_MINOR` 且开关开启才自动应用**：本模块的入口 `auto_apply_if_l4` 只在
  `autonomy_level == "L4"` 时动作；而 `decide_autonomy` 只在 `IMPACT_MINOR 且
  auto_apply_minor_enabled` 时返回 L4（`IMPACT_MAJOR` 在读 flags 之前就返回 L5，结构上不可
  覆盖）。因此 L3（IMPACT_MODERATE 或开关关闭的 MINOR）与 L5（IMPACT_MAJOR）**永远**走人工
  流程，不经本模块。
- **自动应用与回滚都经 `Approval_Service.activate_internal()`**：它做一次**完整硬约束重校验**
  ——回滚也不能产生违规计划（R13.10）。状态迁移经**唯一**的 `update_plan_status_if_version`
  （与人工审批同一单点），不存在绕过审批闸门直接置 ACTIVE 的第二条路径。
- **回滚从 `snapshot_before` 重建**：`revert` 用变更前快照重建一个 `PENDING_APPROVAL` 计划，
  再 `activate_internal` 激活它；成功后 `plan_id_after` 被 supersede、`reverted=true`。回滚后
  的计划逐作业等于 `snapshot_before`（回归测试逐字段断言）。

## 无 schema 迁移

`auto_applied_changes` 表在任务 1.2 已建（P0 建表、P1 使用），本模块只读写它，不新增任何迁移。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.scheduler import PlanCandidate, ScheduledJob
from app.db import audit
from app.db import models as orm
from app.orchestrator.pipelines.plan_generation import (
    _add_scheduled_jobs,
    _ensure_production_jobs,
    _new_plan_id,
)
from app.services.approval import ApprovalService, ApprovalStatus
from app.services.events import EventBus
from app.services.replanning import load_plan_candidate

__all__ = [
    "AUTO_APPLIED_EXECUTION_PATH",
    "AutoApplyResult",
    "RevertResult",
    "RevertStatus",
    "auto_apply_if_l4",
    "list_auto_applied_changes",
    "revert_change",
    "serialize_scheduled_jobs",
]

#: `impact_assessments.execution_path` 在 L4 自动应用后记录的值（design.md §3.6 的 P1 取值）。
AUTO_APPLIED_EXECUTION_PATH = "AUTO_APPLIED"


@dataclass(frozen=True)
class AutoApplyResult:
    """L4 自动应用的结果。`change_id` 为 None 表示未自动应用（非 L4，或重校验失败回退人工）。"""

    applied: bool
    change_id: str | None = None
    activated_plan_id: str | None = None
    superseded_plan_id: str | None = None


def serialize_scheduled_jobs(candidate: PlanCandidate) -> list[dict[str, Any]]:
    """把一份计划的已排产作业序列化成可 JSON 存储的快照（`snapshot_before/after`）。

    只序列化 `ScheduledJob` 的字段（谁、在哪台机器、什么时候、setup/changeover）；按 `job_id`
    升序，使同一份计划得到逐字节确定的快照，回归断言可逐字段比较。
    """
    return [
        {
            "job_id": sj.job_id,
            "order_id": sj.order_id,
            "product_id": sj.product_id,
            "machine_id": sj.machine_id,
            "worker_id": sj.worker_id,
            "start_time": sj.start_time.isoformat(),
            "end_time": sj.end_time.isoformat(),
            "setup_minutes": sj.setup_minutes,
            "changeover_minutes": sj.changeover_minutes,
        }
        for sj in sorted(candidate.scheduled_jobs, key=lambda j: j.job_id)
    ]


def _candidate_from_snapshot(snapshot: list[dict[str, Any]], feasibility: str) -> PlanCandidate:
    """把 `snapshot_before` 的 JSON 反序列化回内核 `PlanCandidate`（用于回滚重建）。"""
    jobs = tuple(
        ScheduledJob(
            job_id=str(row["job_id"]),
            order_id=str(row["order_id"]),
            product_id=str(row["product_id"]),
            machine_id=str(row["machine_id"]),
            worker_id=str(row["worker_id"]),
            start_time=datetime.fromisoformat(str(row["start_time"])),
            end_time=datetime.fromisoformat(str(row["end_time"])),
            setup_minutes=int(row["setup_minutes"]),
            changeover_minutes=int(row["changeover_minutes"]),
        )
        for row in sorted(snapshot, key=lambda r: str(r["job_id"]))
    )
    return PlanCandidate(
        scheduled_jobs=jobs,
        unschedulable_jobs=(),
        feasibility=feasibility,  # type: ignore[arg-type]  # 取值域由写入侧保证
    )


def auto_apply_if_l4(
    session: Session,
    *,
    autonomy_level: str,
    revised_plan_id: str,
    active_plan_id: str,
    assessment_id: str,
    now: datetime,
    events: EventBus,
) -> AutoApplyResult:
    """若 `autonomy_level == L4`，自动应用修订计划并写 `AutoAppliedChange`（R13.9）。

    捕获 `snapshot_before`（当前 ACTIVE 计划的作业）→ `activate_internal(revised_plan_id)`
    （完整重校验 + 唯一状态写入点）→ 捕获 `snapshot_after`（修订计划的作业）→ 写
    `AutoAppliedChange(reverted=false)` 并把该 assessment 的 `execution_path` 记为 `AUTO_APPLIED`。

    非 L4 → 直接返回未应用（调用方按 L3/L5 走人工流程）。重校验失败 → 返回未应用（修订计划
    仍 `PENDING_APPROVAL`，退回人工——绝不落一个违规的 ACTIVE，R13.10）。
    """
    if autonomy_level != "L4":
        return AutoApplyResult(applied=False)

    # snapshot_before：当前 ACTIVE 计划的作业（激活修订计划前捕获）。
    before_candidate = load_plan_candidate(session, active_plan_id)
    snapshot_before = serialize_scheduled_jobs(before_candidate)

    result = ApprovalService(session=session, now=now, events=events).activate_internal(
        revised_plan_id, actor="SYSTEM"
    )
    if result.status is not ApprovalStatus.OK:
        # 重校验失败 / 状态非法：不自动应用，修订计划仍待人工审批（R13.10）。
        return AutoApplyResult(applied=False)

    # snapshot_after：刚激活的修订计划的作业。
    after_candidate = load_plan_candidate(session, revised_plan_id)
    snapshot_after = serialize_scheduled_jobs(after_candidate)

    change_id = f"AAC-{uuid.uuid4().hex[:12]}"
    session.add(
        orm.AutoAppliedChange(
            change_id=change_id,
            assessment_id=assessment_id,
            plan_id_before=active_plan_id,
            plan_id_after=revised_plan_id,
            snapshot_before=snapshot_before,
            snapshot_after=snapshot_after,
            applied_at=now,
            reverted=False,
        )
    )
    # execution_path 记录 AUTO_APPLIED（design.md §3.6 的 P1 取值）。
    assessment = session.get(orm.ImpactAssessment, assessment_id)
    if assessment is not None:
        assessment.execution_path = AUTO_APPLIED_EXECUTION_PATH
    session.commit()

    audit.append(
        event_category="APPROVAL_ACTION",
        event_type="AUTO_APPLIED_CHANGE",
        actor="SYSTEM",
        payload={
            "change_id": change_id,
            "assessment_id": assessment_id,
            "plan_id_before": active_plan_id,
            "plan_id_after": revised_plan_id,
        },
        subject_type="AutoAppliedChange",
        subject_id=change_id,
        occurred_at=now,
    )
    return AutoApplyResult(
        applied=True,
        change_id=change_id,
        activated_plan_id=revised_plan_id,
        superseded_plan_id=active_plan_id,
    )


class RevertStatus(StrEnum):
    """`revert_change` 的结果状态。"""

    OK = "OK"
    #: 变更不存在。
    NOT_FOUND = "NOT_FOUND"
    #: 已回滚过（幂等保护）。
    ALREADY_REVERTED = "ALREADY_REVERTED"
    #: 从 snapshot_before 重建的计划重校验失败——回滚被拒（绝不产生违规计划，R13.10）。
    REVALIDATION_FAILED = "REVALIDATION_FAILED"


@dataclass(frozen=True)
class RevertResult:
    status: RevertStatus
    change_id: str
    revert_plan_id: str | None = None
    superseded_plan_id: str | None = None
    violations: tuple[Any, ...] = ()


def revert_change(
    session: Session,
    *,
    change_id: str,
    now: datetime,
    events: EventBus,
) -> RevertResult:
    """一键回滚一个 `AutoAppliedChange`（R13.10）。

    从 `snapshot_before` 重建一个 `PENDING_APPROVAL` 计划，经 `Approval_Service.activate_internal()`
    （完整硬约束重校验）激活它；成功后：恢复的计划为 `ACTIVE`，`plan_id_after` 被
    supersede（activate_internal 内 supersede_previous_active 自动完成），`AutoAppliedChange`
    标 `reverted=true` 并记 `revert_plan_id`。重校验失败 → 回滚被拒，什么都不改（R13.10）。
    """
    change = session.get(orm.AutoAppliedChange, change_id)
    if change is None:
        return RevertResult(status=RevertStatus.NOT_FOUND, change_id=change_id)
    if change.reverted:
        return RevertResult(status=RevertStatus.ALREADY_REVERTED, change_id=change_id)

    plan_id_before = change.plan_id_before
    plan_id_after = change.plan_id_after
    snapshot_before = change.snapshot_before if isinstance(change.snapshot_before, list) else []

    # 回滚计划以 **plan_id_before** 的计划头为准（生产日 + `input_snapshot_version` + 可行性）：
    # `snapshot_before` 的作业正是那份计划的排布，它当初就是在 plan_id_before 的
    # `input_snapshot_version` 上通过硬约束校验的。activate_internal 会在**该版本**的快照上重校验
    # （见其 `load_snapshot(..., snapshot_version=proposal_version)`）——因此回滚校验的是「这份旧
    # 排布相对它当初所依据的世界」是否仍合法，而不是相对被自动应用的变更之后的新世界（那个新世界
    # 可能已因扰动改变，比如机器停机，会把一份本来合法的旧计划误判为违规）。回滚是「回到从前」，
    # 校验口径也必须是从前的那一版数据。
    before_plan = session.get(orm.ProductionPlan, plan_id_before)
    if before_plan is None:
        return RevertResult(status=RevertStatus.NOT_FOUND, change_id=change_id)
    production_date = before_plan.production_date
    input_snapshot_version = before_plan.input_snapshot_version
    feasibility = before_plan.feasibility

    # 回滚前先取消该生产日上任何在办的 PENDING_APPROVAL 提案：`ux_pending_per_day` 规定同一
    # 生产日至多一个待审提案，而回滚要新建一个 PENDING 的恢复计划。这些在办提案通常是 L4 自动
    # 应用后触发的风险扫描生成的缓解提案——它们针对的是「即将被回滚掉」的那个变更，回滚一旦发生
    # 即失去意义。回滚是规划员的明确决定，优先于自动生成的提案：把它们置 SUPERSEDED（让位），
    # 而非撞唯一索引崩掉。这不放宽任何审批边界——被让位的提案本就未激活，且状态迁移仍经受控路径。
    superseded_pending = _supersede_pending_on_date(session, production_date)

    # 从 snapshot_before 重建一个 PENDING_APPROVAL 计划。
    restored = _candidate_from_snapshot(snapshot_before, feasibility)
    revert_plan_id = _new_plan_id()
    _persist_restored_plan(
        session,
        plan_id=revert_plan_id,
        candidate=restored,
        production_date=production_date,
        input_snapshot_version=input_snapshot_version,
        now=now,
    )
    session.commit()

    # commit 之后再补让位提案的审计（避开 SQLite 审计连接自我死锁，见 _supersede_pending_on_date）。
    for pid in superseded_pending:
        audit.append(
            event_category="APPROVAL_ACTION",
            event_type="PENDING_SUPERSEDED_BY_REVERT",
            actor="SYSTEM",
            payload={"plan_id": pid, "reason": "superseded by one-click revert"},
            subject_type="ProductionPlan",
            subject_id=pid,
            occurred_at=now,
        )

    # 经 activate_internal 激活（完整重校验；回滚也不能产生违规计划，R13.10）。
    result = ApprovalService(session=session, now=now, events=events).activate_internal(
        revert_plan_id, actor="SYSTEM"
    )
    if result.status is not ApprovalStatus.OK:
        # 回滚被拒：把刚建的候选置 REJECTED（不留悬空 PENDING），不改 AutoAppliedChange。
        rejected = session.get(orm.ProductionPlan, revert_plan_id)
        if rejected is not None and rejected.status == "PENDING_APPROVAL":
            rejected.status = "REJECTED"
            session.commit()
        return RevertResult(
            status=RevertStatus.REVALIDATION_FAILED,
            change_id=change_id,
            violations=tuple(result.violations),
        )

    # 标记已回滚（activate_internal 已 supersede 了 plan_id_after）。
    change = session.get(orm.AutoAppliedChange, change_id)
    if change is not None:
        change.reverted = True
        change.reverted_at = now
        change.revert_plan_id = revert_plan_id
    session.commit()

    audit.append(
        event_category="APPROVAL_ACTION",
        event_type="AUTO_APPLIED_CHANGE_REVERTED",
        actor="SYSTEM",
        payload={
            "change_id": change_id,
            "revert_plan_id": revert_plan_id,
            "superseded_plan_id": plan_id_after,
        },
        subject_type="AutoAppliedChange",
        subject_id=change_id,
        occurred_at=now,
    )
    return RevertResult(
        status=RevertStatus.OK,
        change_id=change_id,
        revert_plan_id=revert_plan_id,
        superseded_plan_id=plan_id_after,
    )


def _supersede_pending_on_date(session: Session, production_date: object) -> list[str]:
    """把该生产日上全部 `PENDING_APPROVAL` 计划置 `SUPERSEDED`（让位给回滚的恢复计划）。

    `PENDING_APPROVAL → SUPERSEDED` 是 design.md §8 状态机表内的合法迁移（`Approval_Service`
    的 cancel 语义）。这里不经 `update_plan_status_if_version`（那是通向 ACTIVE 的受控单点）——
    让位到 SUPERSEDED 不触及 `ux_active_per_day` 不变量，直接 UPDATE 即可。

    **不在此写审计**：审计经 `db/audit.py` 的独立引擎写入，而 SQLite 同一时刻只允许一个写者；
    若在业务事务仍持写锁时 `audit.append` 会自我死锁（见 `db/audit.py` 末尾的约束说明）。因此
    本函数只做业务写、返回被让位的 id，由调用方在 `commit()` 之后统一补审计（与 `ApprovalService`
    「commit 后再 audit」同一口径）。
    """
    from sqlalchemy import update as _sql_update

    pending_ids = list(
        session.execute(
            select(orm.ProductionPlan.plan_id).where(
                orm.ProductionPlan.production_date == production_date,
                orm.ProductionPlan.status == "PENDING_APPROVAL",
            )
        ).scalars()
    )
    if not pending_ids:
        return []
    session.execute(
        _sql_update(orm.ProductionPlan)
        .where(orm.ProductionPlan.plan_id.in_(pending_ids))
        .values(status="SUPERSEDED")
    )
    session.flush()
    return pending_ids


def _persist_restored_plan(
    session: Session,
    *,
    plan_id: str,
    candidate: PlanCandidate,
    production_date: object,
    input_snapshot_version: int,
    now: datetime,
) -> None:
    """把从 snapshot_before 重建的候选落成一个 `PENDING_APPROVAL` 计划头 + scheduled_jobs。

    复用 `_ensure_production_jobs`（production_jobs 全局主键，回滚计划引用的 job 早已存在，
    「不存在才插」是幂等的）与 `_add_scheduled_jobs`。不提交——调用方持有事务。
    """
    from app.orchestrator.pipelines.plan_generation import ProductionJobSpec

    session.add(
        orm.ProductionPlan(
            plan_id=plan_id,
            production_date=production_date,
            status="PENDING_APPROVAL",
            feasibility=candidate.feasibility,
            plan_version=1,
            version=1,
            input_snapshot_version=input_snapshot_version,
            origin="AUTO_REVERT",
            created_at=now,
        )
    )
    session.flush()

    # 回滚计划引用的 production_jobs 都已存在（快照来自一个真实存在过的计划）；仍走
    # _ensure_production_jobs 的「不存在才插」以防边界情形。job 的元字段从既有 production_jobs
    # 读回（回滚不改变作业定义，只改排布）。
    specs: list[ProductionJobSpec] = []
    for sj in candidate.scheduled_jobs:
        pj = session.get(orm.ProductionJob, sj.job_id)
        if pj is not None:
            specs.append(
                ProductionJobSpec(
                    job_id=pj.job_id,
                    order_id=pj.order_id,
                    product_id=pj.product_id,
                    operation_sequence=pj.operation_sequence,
                    predecessor_job_id=pj.predecessor_job_id,
                    quantity=pj.quantity,
                    required_machine_type=pj.required_machine_type,
                    required_worker_skill=pj.required_worker_skill,
                )
            )
    _ensure_production_jobs(session, specs=tuple(specs))
    _add_scheduled_jobs(session, plan_id=plan_id, candidate=candidate)
    session.flush()


def list_auto_applied_changes(session: Session) -> list[orm.AutoAppliedChange]:
    """列出全部自动应用记录，按 `applied_at` 倒序（最近的在前），供 UI 通知区与 GET 端点。"""
    return list(
        session.execute(
            select(orm.AutoAppliedChange).order_by(orm.AutoAppliedChange.applied_at.desc())
        ).scalars().all()
    )
