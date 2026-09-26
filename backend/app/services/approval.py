"""`Approval_Service` —— 唯一能把一个计划置为 `ACTIVE` 的组件（任务 3.1，R11 / R12）。

design.md §3 的确定性/LLM 归属总表把这一行写死：`Approval_Service` 是**确定性**组件，且
是「唯一能置 `ACTIVE`」的那一个（R11.1）。属性 15 由此展开——使某个计划变为 `ACTIVE` 的
操作只可能是 `Approval_Service.approve()`（P1 另有 `activate_internal` 回滚路径）。本模块
落地它的 `approve()` 分支。

## approve() 的五步闸门（design.md §4.1）

    状态检查 → ① 陈旧检测 → ② 重校验 → ③ 乐观并发激活 → supersede + plan_approvals + emit

每一步都是一道**拒绝**闸门，任一步不过即返回对应的结构化结果，不进入下一步：

1. **状态检查**：计划状态非 `PENDING_APPROVAL` → `INVALID_STATE_TRANSITION`。只有待审批的
   提案才谈得上「批准」，一个已 `ACTIVE` / `REJECTED` / `SUPERSEDED` 的计划不能被再次激活。
2. **① 陈旧检测（R12.2–3）**：比较 `current_input_snapshot_version()` 与
   `plan.input_snapshot_version`。不等意味着「提案生成之后输入数据变过」，写
   `STALE_PROPOSAL_REJECTED` 审计并返回 `STALE_PROPOSAL`。**载荷只有两个版本号**——不列出
   变化的实体与字段（design.md 的刻意减法：无论变了什么，规划员的下一步都是基于最新数据
   重新生成，R12.4）。
3. **② 重校验（R11.3 / R12.5 / R6.5）**：在**当前**快照上完整重跑 `Constraint_Validator`。
   有违反则写 `APPROVAL_REVALIDATION_FAILED` 审计并返回 `REVALIDATION_FAILED` + 违反清单，
   **计划状态保持 `PENDING_APPROVAL`**（R11.6 的语义：拦住违规计划但不销毁提案）。审批不是
   盖章：K-08 要求 `ACTIVE` 计划零违反，而数据可能在提案生成后以「版本号没变但语义边界移动」
   之外的方式让计划失效——重校验是最后一道独立复核。
4. **③ 乐观并发（R12.7）**：`UPDATE production_plans SET status='ACTIVE', version=version+1
   WHERE plan_id=? AND version=?`。`rowcount == 0` 表示另一线程已抢先推进了行版本 →
   `CONCURRENT_MODIFICATION`。这是「两个规划员同时点批准，恰好一个成功」的结构性保证
   （属性 17）。
5. **让位 → 就位 → 收尾（顺序敏感）**：同一事务内 `supersede_previous_active`（把该生产日
   上原来的 `ACTIVE` 计划置 `SUPERSEDED`）**必须先于**步 4 的状态 UPDATE 执行。
   `ux_active_per_day` 是 `WHERE status='ACTIVE'` 的部分唯一索引且 SQLite 逐语句校验，
   先就位会让新旧两个 `ACTIVE` 短暂并存而撞索引——这正是「同一生产日第二次审批必 500」的
   根因。让位、就位之后写 `plan_approvals`（含 `revalidation_result` 完整结果，R11.9），
   提交，最后 `emit` `PlanActivated`（任务 8.5 的风险扫描触发器消费，R14.1）。

## 事务边界与副作用的顺序

- 步 ③④⑤ 的三次写（UPDATE 状态、supersede、插 plan_approvals）在**一个业务事务**里，
  要么一起成功要么一起回滚。
- 审计写入（步 ①②）走 `db/audit.py` 的独立连接，**不参与业务事务**：即便随后业务回滚，
  「拦住了一个陈旧/违规提案」这条记录也必须留存（design.md Error Handling §4）。因此陈旧与
  重校验这两条拒绝路径上没有业务写，审计写完即返回。
- `emit(PlanActivated)` 在业务事务**提交之后**才调用：事件的订阅者（风险扫描）看到的必须是
  一个已经落库的 `ACTIVE` 计划，而不是一个可能被回滚的中间态。

## 为什么 `approve()` 返回结果对象而不是抛 HTTP 异常

本模块在 `app/services/` 层，分层规则禁止 `app/core/**` import fastapi——服务层没有这条硬
禁令（它可以用 sqlalchemy），但把 HTTP 语义留在 API 边界仍是纪律：`approve()` 返回一个
`ApprovalResult`（判别联合），任务 3.2 的路由层把它翻译成 design.md Error Handling §2 的统一
错误包（`ErrorCode.STALE_PROPOSAL` 等）。这样 `approve()` 可以在没有 FastAPI 请求上下文的
地方被调用（脚本、测试、将来的编排），而不必伪造一个 `Request`。

## `update_plan_status_if_version` 的调用点受控（R11.8）

状态迁移用的原子条件 UPDATE 封装为本模块的 `update_plan_status_if_version`。
`tests/structure/test_layering.py` 的第 ④ 条断言这个名字**只在本文件
（`app/services/approval.py`）** 被引用（design.md §8 指定的另一处调用点是 P1 的
`AutoAppliedChange.revert`，尚未落地）——外层（api / agents / tools / orchestrator / core）
以及 `app/services/` 下的其余模块一律不得触及。它是「计划状态不可被绕过审批地修改」的
内部防线（外部防线是 REST 层无 `status` 字段 + 显式 403 路由）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum
from typing import Annotated, Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.scheduler import PlanCandidate, ScheduledJob
from app.core.validation import ValidationReport, Violation, validate
from app.db import audit
from app.db import models as orm
from app.db.repositories import current_input_snapshot_version
from app.services.events import EventBus, PlanActivated
from app.services.plan_state_machine import PlanStatus, is_allowed_transition
from app.services.snapshot_loader import load_snapshot

#: 唯一可被激活的前置状态。
PENDING_STATUS = "PENDING_APPROVAL"
#: 激活后的目标状态。**硬编码**——没有任何入参能改它（R11.8、R23.4）。
ACTIVE_STATUS = "ACTIVE"
#: 被新计划取代的旧 `ACTIVE` 计划落到的状态。
SUPERSEDED_STATUS = "SUPERSEDED"

#: 审批动作记录里的 `action` 取值（`APPROVAL_ACTIONS` 之一）。
APPROVE_ACTION = "APPROVE"
REJECT_ACTION = "REJECT"
MODIFY_ACTION = "MODIFY"
CANCEL_ACTION = "CANCEL"

#: `rejection_reason` 的最小长度（R11.4）。一句「不行」不构成可供审计与偏好蒸馏
#: （R18.1）复盘的理由；design.md §4.1 明确「最少 5 字符」。
MIN_REJECTION_REASON_LENGTH = 5

#: `MODIFY` 生成新版本时 `production_plans.origin` 的取值（design.md Data Models §8）。
MODIFY_ORIGIN = "MODIFY"

#: `MODIFY` 生成的新版本的初始状态。**硬编码**——绝不直接激活（R11.7）。
REVISED_STATUS = "PENDING_APPROVAL"


class ApprovalStatus(StrEnum):
    """`approve()` 的判别结果。API 层据此选 HTTP 状态码与 `ErrorCode`（任务 3.2）。

    取值与 `api/errors.py` 的 `ErrorCode` 成员一一对应（`OK` 除外——成功不是错误）。分开
    定义而不复用 `ErrorCode`：服务层不 import api 层（保持翻译方向单一），且 `OK` 在
    `ErrorCode` 里本就不该有。
    """

    OK = "OK"
    INVALID_STATE_TRANSITION = "INVALID_STATE_TRANSITION"
    STALE_PROPOSAL = "STALE_PROPOSAL"
    REVALIDATION_FAILED = "REVALIDATION_FAILED"
    CONCURRENT_MODIFICATION = "CONCURRENT_MODIFICATION"


@dataclass(frozen=True)
class ApprovalResult:
    """`approve()` 的结构化结果（判别联合，按 `status` 解读其余字段）。

    - `OK`：`plan_id` 是被激活的计划。
    - `STALE_PROPOSAL`：`proposal_version` / `current_version` 是**仅有的**两个版本号
      （R12.3 的载荷范围）。
    - `REVALIDATION_FAILED`：`violations` 是重校验的违反清单（原样来自
      `Constraint_Validator`）。
    - `INVALID_STATE_TRANSITION` / `CONCURRENT_MODIFICATION`：`current_status` 供 API 组织
      面向规划员的说明与「下一步」入口。

    `frozen=True`：结果是一次审批判定的事实记录。
    """

    status: ApprovalStatus
    plan_id: str
    current_status: str | None = None
    proposal_version: int | None = None
    current_version: int | None = None
    violations: tuple[Violation, ...] = field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        return self.status is ApprovalStatus.OK


# --------------------------------------------------------------------------
# MODIFY 的 5 类结构化修改（R11.5、design.md §4.1）
#
# 判别联合，`kind` 是判别键。刻意封闭为 5 个成员，没有自由谓词、没有「直接置状态」的
# 成员——修改只能重新安排一个作业的机器/工人/时间，或把它移出计划、锁定它。任何一类都
# 不能放宽硬约束：修改后无条件重跑 `Constraint_Validator`（R11.6），违反即拒绝。
# --------------------------------------------------------------------------


class ReassignMachine(BaseModel):
    """把某个已排产作业改派到另一台机器（R11.5）。"""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["REASSIGN_MACHINE"] = "REASSIGN_MACHINE"
    job_id: str
    machine_id: str


class ReassignWorker(BaseModel):
    """把某个已排产作业改派给另一名工人（R11.5）。"""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["REASSIGN_WORKER"] = "REASSIGN_WORKER"
    job_id: str
    worker_id: str


class MoveTime(BaseModel):
    """把某个已排产作业整体平移到新的起止时间（R11.5）。

    `start_time` / `end_time` 都由规划员给定：本服务不重算工期（那是排产器的职责），只把
    作业挪到规划员指定的窗口，再让 `Constraint_Validator` 判定这个窗口是否越过班次、是否
    与别的作业重叠。`end_time > start_time` 由校验兜底（也由 `scheduled_jobs` 的 CHECK 兜底）。
    """

    model_config = ConfigDict(extra="forbid")

    kind: Literal["MOVE_TIME"] = "MOVE_TIME"
    job_id: str
    start_time: datetime
    end_time: datetime


class RemoveFromPlan(BaseModel):
    """把某个已排产作业移出计划（R11.5）。

    移出后该作业不再出现在新版本的 `scheduled_jobs` 里。它**不**被记为不可排产
    （`unschedulable_jobs`）：那是排产器判定「排不进」的结论，而这里是规划员主动拿掉——
    两者语义不同。新版本的可行性判定只看剩下的已排产作业。
    """

    model_config = ConfigDict(extra="forbid")

    kind: Literal["REMOVE_FROM_PLAN"] = "REMOVE_FROM_PLAN"
    job_id: str


class LockJob(BaseModel):
    """锁定某个已排产作业（R11.5）。

    在新版本里把该作业的 `scheduled_jobs.locked` 置真。它不改变作业的机器/工人/时间，只标记
    「重排时冻结这一条」——任务 7.1 的 `Replanner` 冻结逻辑消费这个标记。因此 `LOCK_JOB`
    永远不可能引入硬约束违反（它什么都没移动），但仍与其余四类一样走重校验，保持路径统一。
    """

    model_config = ConfigDict(extra="forbid")

    kind: Literal["LOCK_JOB"] = "LOCK_JOB"
    job_id: str


#: 5 类结构化修改的判别联合（design.md §4.1 的 `MODIFY` 五类）。
Modification = Annotated[
    ReassignMachine | ReassignWorker | MoveTime | RemoveFromPlan | LockJob,
    Field(discriminator="kind"),
]


class ModifyStatus(StrEnum):
    """`modify()` 的判别结果。API 层据此选 HTTP 状态码与 `ErrorCode`（任务 3.2）。"""

    OK = "OK"
    INVALID_STATE_TRANSITION = "INVALID_STATE_TRANSITION"
    #: 某条修改指向计划里不存在的 `job_id`。
    JOB_NOT_IN_PLAN = "JOB_NOT_IN_PLAN"
    #: 修改后重校验发现硬约束违反（R11.6）：**不改变任何状态**，返回违反清单。
    MODIFICATION_REVALIDATION_FAILED = "MODIFICATION_REVALIDATION_FAILED"
    #: 目标生产日已存在另一个 `PENDING_APPROVAL`（`ux_pending_per_day` 冲突，R12.6）。
    PENDING_PLAN_EXISTS = "PENDING_PLAN_EXISTS"


@dataclass(frozen=True)
class ModifyResult:
    """`modify()` 的结构化结果（判别联合，按 `status` 解读其余字段）。

    - `OK`：`new_plan_id` 是新生成的 `PENDING_APPROVAL` 版本；`source_plan_id` 是被
      `SUPERSEDED` 的原计划。
    - `MODIFICATION_REVALIDATION_FAILED`：`violations` 是修改后校验的违反清单；原计划
      状态**未变**（R11.6）。
    - `JOB_NOT_IN_PLAN`：`missing_job_id` 是那条无法定位的作业。
    - `PENDING_PLAN_EXISTS` / `INVALID_STATE_TRANSITION`：由 API 组织下一步入口。
    """

    status: ModifyStatus
    source_plan_id: str
    new_plan_id: str | None = None
    current_status: str | None = None
    missing_job_id: str | None = None
    violations: tuple[Violation, ...] = field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        return self.status is ModifyStatus.OK


class RejectStatus(StrEnum):
    """`reject()` 的判别结果。"""

    OK = "OK"
    INVALID_STATE_TRANSITION = "INVALID_STATE_TRANSITION"
    #: `rejection_reason` 不足 5 字符（R11.4）。
    REASON_TOO_SHORT = "REASON_TOO_SHORT"


@dataclass(frozen=True)
class RejectResult:
    """`reject()` 的结构化结果。`plan_id` 是被置 `REJECTED` 的计划。"""

    status: RejectStatus
    plan_id: str
    current_status: str | None = None

    @property
    def ok(self) -> bool:
        return self.status is RejectStatus.OK


def update_plan_status_if_version(
    session: Session, plan_id: str, new_status: str, expected_version: int
) -> int:
    """原子条件状态迁移（R12.7 的乐观并发控制）。返回受影响行数（`rowcount`）。

        UPDATE production_plans
           SET status = :new_status, version = version + 1
         WHERE plan_id = :plan_id AND version = :expected_version

    `rowcount == 0` 意味着 `WHERE version = expected` 没匹配上——另一次 `approve()` 已经
    抢先把行版本推进了一格。调用方据此返回 `CONCURRENT_MODIFICATION`，绝不重试（重试会在
    一个已被别人改过的行上盲目覆盖）。

    **只在这里改 `production_plans.status`。** 名字受 `test_layering.py` 第 ④ 条的调用点
    集合约束（R11.8）：除本模块（`Approval_Service`）与 P1 的 `AutoAppliedChange.revert`
    外，任何层引用这个名字都会让那条静态断言变红。

    不提交——调用方持有事务边界，supersede 与 `plan_approvals` 要与这次 UPDATE 同事务。
    """
    result = session.execute(
        update(orm.ProductionPlan)
        .where(
            orm.ProductionPlan.plan_id == plan_id,
            orm.ProductionPlan.version == expected_version,
        )
        .values(status=new_status, version=orm.ProductionPlan.version + 1)
    )
    return int(result.rowcount)


def supersede_previous_active(
    session: Session, production_date: date, *, except_id: str
) -> list[str]:
    """把该生产日上除 `except_id` 之外的全部 `ACTIVE` 计划置为 `SUPERSEDED`（R11.3）。

    维持 `ux_active_per_day` 部分唯一索引所要求的不变量：任一生产日至多一个 `ACTIVE`
    （属性 15）。返回被取代的计划 ID 列表（供审计载荷与测试断言）。

    正常情况下待取代的至多一个（索引保证），但这里用「除 except 之外全部」的写法而非「取
    那一个」：更防御，且与 design.md `supersede_previous_active(production_date, except_id)`
    的签名一致。同时把它们的 `superseded_by_plan_id` 指向新激活的计划，使取代关系可追溯。

    不提交——同一事务。
    """
    superseded_ids = list(
        session.execute(
            select(orm.ProductionPlan.plan_id).where(
                orm.ProductionPlan.production_date == production_date,
                orm.ProductionPlan.status == ACTIVE_STATUS,
                orm.ProductionPlan.plan_id != except_id,
            )
        ).scalars()
    )
    if superseded_ids:
        session.execute(
            update(orm.ProductionPlan)
            .where(orm.ProductionPlan.plan_id.in_(superseded_ids))
            .values(status=SUPERSEDED_STATUS, superseded_by_plan_id=except_id)
        )
    return superseded_ids


def _revalidation_result(report: ValidationReport) -> dict[str, Any]:
    """把校验报告落成 `plan_approvals.revalidation_result` 的 JSON（R11.9）。

    存**完整**结果而非布尔：R11 要求审批时重跑硬约束校验，「当时校验到了什么」是事后追责
    的唯一凭据。违反逐条 `model_dump(mode="json")`，`Decimal` / `datetime` 等经 pydantic 的
    JSON 模式转成可序列化形态。
    """
    return {
        "is_feasible": report.is_feasible,
        "violation_count": len(report.violations),
        "violations": [v.model_dump(mode="json") for v in report.violations],
    }


def _candidate_from_plan(session: Session, plan_id: str, feasibility: str) -> PlanCandidate:
    """从库里的 `scheduled_jobs`（+ `production_jobs`）重建一个 `PlanCandidate` 供重校验。

    校验器只读 `candidate.scheduled_jobs`（`unschedulable_jobs` 本就没排进计划，对它们谈
    机器重叠/班次越界没有意义，见 `validation.validate` docstring）。因此这里只重建已排产
    部分，`unschedulable_jobs` 置空、`feasibility` 直接取计划头存的值——这不影响 9 个检查的
    任何一个，它们全部只遍历 `scheduled_jobs`。

    `order_id` / `product_id` 不在 `scheduled_jobs` 表上（它只记「谁在哪台机器什么时候」），
    从 `production_jobs` join 取——与 `api/plans.py._detail_from_db` 的重建口径一致。
    """
    rows = list(
        session.execute(
            select(orm.ScheduledJob, orm.ProductionJob)
            .join(orm.ProductionJob, orm.ScheduledJob.job_id == orm.ProductionJob.job_id)
            .where(orm.ScheduledJob.plan_id == plan_id)
            .order_by(orm.ScheduledJob.job_id)
        )
    )
    scheduled = tuple(
        ScheduledJob(
            job_id=sj.job_id,
            order_id=pj.order_id,
            product_id=pj.product_id,
            machine_id=sj.machine_id,
            worker_id=sj.worker_id,
            start_time=sj.start_time,
            end_time=sj.end_time,
            setup_minutes=sj.setup_minutes,
            changeover_minutes=sj.changeover_minutes,
        )
        for sj, pj in rows
    )
    return PlanCandidate(
        scheduled_jobs=scheduled,
        unschedulable_jobs=(),
        feasibility=feasibility,  # type: ignore[arg-type]  # 取值域由写入侧保证
    )


def _locked_job_ids(session: Session, plan_id: str) -> frozenset[str]:
    """计划里已被 `LOCK_JOB` 锁定的作业 ID 集合。

    `_candidate_from_plan` 重建的 `PlanCandidate` 不携带 `locked`（`ScheduledJob` 值对象没有
    这一列——锁定是持久化层的调度提示，不是排产语义）。因此单独读一次，供 `modify()` 把已有
    的锁定状态延续到新版本，再叠加本次的 `LOCK_JOB`。
    """
    return frozenset(
        session.execute(
            select(orm.ScheduledJob.job_id)
            .where(orm.ScheduledJob.plan_id == plan_id, orm.ScheduledJob.locked.is_(True))
        ).scalars()
    )


def _objective_breakdown_snapshot(session: Session, plan_id: str) -> dict[str, Any]:
    """把计划当时的目标拆解读成 `planner_decisions.objective_breakdown_snapshot`（R18.1–2）。

    这是「决策发生时，这个计划的目标得分长什么样」的定格。偏好蒸馏（P1）据此判断规划员反复
    拒绝/修改的计划有什么共同的目标特征；P0 它只服务审计与 `EVAL-205`。

    计划可能没有拆解行（例如某些历史/基线路径），此时存一个显式的空快照而非报错：决策记录
    的价值在于「有没有发生这个决策」，缺一份得分快照不该阻断记录本身。
    """
    row = session.get(orm.ObjectiveBreakdown, plan_id)
    if row is None:
        return {"components": [], "total_score": None, "weights": {}}
    return {
        "components": _as_json(row.components),
        "total_score": str(row.total_score) if row.total_score is not None else None,
        "weights": _as_json(row.weights),
        "preference_contributions": _as_json(row.preference_contributions),
        "weight_overrides_applied": _as_json(row.weight_overrides_applied),
    }


def _as_json(value: object) -> Any:
    """JSON 列取出后原样透传（`dict` / `list` / 标量）。非期望类型退化为 `None`。"""
    if isinstance(value, dict):
        return {str(k): _as_json(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_as_json(v) for v in value]
    return value


class _JobNotInPlan(Exception):
    """某条修改指向计划里不存在的 `job_id`（`modify()` 内部信号，翻译成 `JOB_NOT_IN_PLAN`）。"""

    def __init__(self, job_id: str) -> None:
        self.job_id = job_id
        super().__init__(f"作业 {job_id} 不在该计划的已排产作业里")


def _apply_modifications(
    original: PlanCandidate,
    locked_job_ids: frozenset[str],
    modifications: tuple[Modification, ...],
) -> tuple[PlanCandidate, frozenset[str]]:
    """把 5 类修改依次应用到 `original`，返回 `(修改后的候选, 锁定作业集合)`。

    纯函数：不触库、不改 `original`（`PlanCandidate` 与 `ScheduledJob` 都 `frozen`）。逐条按
    顺序应用，因此对同一作业的多条修改「后者叠加在前者之上」。任一修改指向计划里没有的
    `job_id` → 抛 `_JobNotInPlan`（`REMOVE_FROM_PLAN` 移走后再被引用也算不存在）。

    - `REASSIGN_MACHINE` / `REASSIGN_WORKER`：换掉对应字段，其余不动。
    - `MOVE_TIME`：换 `start_time` / `end_time`，工期由规划员给定（本服务不重算）。
    - `REMOVE_FROM_PLAN`：从已排产集合里删掉该作业（不进 `unschedulable_jobs`）。
    - `LOCK_JOB`：作业本身不变，只把它加入锁定集合，写库时置 `scheduled_jobs.locked`。

    修改后的 `feasibility`：仍有已排产作业 → `PARTIAL` 或 `FEASIBLE` 由原值延续（校验器判定
    硬约束，这里不重判可行性等级）；全部被移出 → `NO_FEASIBLE_PLAN`。P0 的取舍是保守的：
    只在「一个作业都不剩」时降级，其余保持原等级，真正的可行性由随后的 `validate` 把关。
    """
    by_id: dict[str, ScheduledJob] = {sj.job_id: sj for sj in original.scheduled_jobs}
    locked: set[str] = set(locked_job_ids)

    for mod in modifications:
        if mod.job_id not in by_id:
            raise _JobNotInPlan(mod.job_id)
        job = by_id[mod.job_id]
        if isinstance(mod, ReassignMachine):
            by_id[mod.job_id] = job.model_copy(update={"machine_id": mod.machine_id})
        elif isinstance(mod, ReassignWorker):
            by_id[mod.job_id] = job.model_copy(update={"worker_id": mod.worker_id})
        elif isinstance(mod, MoveTime):
            by_id[mod.job_id] = job.model_copy(
                update={"start_time": mod.start_time, "end_time": mod.end_time}
            )
        elif isinstance(mod, RemoveFromPlan):
            del by_id[mod.job_id]
            locked.discard(mod.job_id)
        elif isinstance(mod, LockJob):
            locked.add(mod.job_id)

    scheduled = tuple(by_id[job_id] for job_id in sorted(by_id))
    if scheduled:
        feasibility = original.feasibility
    else:
        feasibility = "NO_FEASIBLE_PLAN"
    revised = PlanCandidate(
        scheduled_jobs=scheduled,
        unschedulable_jobs=original.unschedulable_jobs,
        feasibility=feasibility,  # type: ignore[arg-type]  # 取值域由上方分支保证
    )
    return revised, frozenset(locked & set(by_id))


@dataclass
class ApprovalService:
    """审批闸门。持有会话、审批时刻与事件总线（依赖注入，便于测试替换）。

    `now` 是重校验加载当前快照所需的「现在」（内核可重现性的入口条件，见
    `snapshot_loader.load_snapshot`）——审批发生的时刻。生产装配处传系统时钟（P0 演示传
    `DEMO_ANCHOR`），测试传固定值。
    """

    session: Session
    now: datetime
    events: EventBus

    def cancel_stale_for_regeneration(self, plan_id: str, actor: str) -> bool:
        """Cancel a stale proposal so the planner can generate a current replacement.

        A stale plan is deliberately kept pending when an approval attempt fails so its
        evidence remains reviewable.  When the planner explicitly starts a new generation,
        however, that old proposal must leave the per-day pending slot; otherwise the
        regenerate action can never succeed.  This is the state-machine-authorised
        ``PENDING_APPROVAL -> SUPERSEDED`` cancellation path.
        """
        plan = self.session.get(orm.ProductionPlan, plan_id)
        if plan is None or not is_allowed_transition(plan.status, PlanStatus.SUPERSEDED):
            return False
        current_version = current_input_snapshot_version(self.session)
        if plan.input_snapshot_version == current_version:
            return False

        proposal_version = plan.input_snapshot_version
        plan.status = SUPERSEDED_STATUS
        self.session.add(
            orm.PlanApproval(
                approval_id=f"APR-{uuid4().hex[:16]}",
                plan_id=plan_id,
                action=CANCEL_ACTION,
                actor=actor,
                timestamp=self.now,
                rejection_reason="Superseded by regeneration after input data changed.",
                revalidation_result={
                    "skipped": True,
                    "reason": "STALE_REGENERATION",
                    "proposal_version": proposal_version,
                    "current_version": current_version,
                },
                modifications=None,
            )
        )
        self.session.commit()
        audit.append(
            event_category="APPROVAL_ACTION",
            event_type=CANCEL_ACTION,
            actor=actor,
            payload={
                "plan_id": plan_id,
                "reason": "STALE_REGENERATION",
                "proposal_version": proposal_version,
                "current_version": current_version,
            },
            subject_type="ProductionPlan",
            subject_id=plan_id,
            occurred_at=self.now,
        )
        return True

    def approve(self, plan_id: str, actor: str, expected_version: int) -> ApprovalResult:
        """执行五步审批闸门（见模块 docstring）。绝不直接激活未过闸门的计划。"""
        plan = self.session.get(orm.ProductionPlan, plan_id)
        if plan is None:
            # 不存在的计划无法「批准」——按非法状态迁移处理（没有可迁移的源状态）。
            return ApprovalResult(
                status=ApprovalStatus.INVALID_STATE_TRANSITION,
                plan_id=plan_id,
                current_status=None,
            )

        # A competing approval may have committed before this session's first read.
        # The stale optimistic version identifies that race even though the row is
        # already ACTIVE by the time the state check runs.
        if plan.status == ACTIVE_STATUS and plan.version != expected_version:
            return ApprovalResult(
                status=ApprovalStatus.CONCURRENT_MODIFICATION,
                plan_id=plan_id,
                current_status=plan.status,
            )

        # ---- 状态检查（经计划状态机的迁移许可表，design.md §8） ----
        # `PENDING_APPROVAL → ACTIVE` 是表内唯一通向 `ACTIVE` 的迁移；任何其他源状态
        # （已 ACTIVE / REJECTED / SUPERSEDED / DRAFT）到 ACTIVE 都是表外迁移 →
        # `INVALID_STATE_TRANSITION`（R11.8 / R23.4）。合法性判定走单点入口
        # `is_allowed_transition`，与属性 15 用的是同一张表。
        if not is_allowed_transition(plan.status, PlanStatus.ACTIVE):
            return ApprovalResult(
                status=ApprovalStatus.INVALID_STATE_TRANSITION,
                plan_id=plan_id,
                current_status=plan.status,
            )

        # 把后续要用到的计划字段读进局部变量：重校验的 `load_snapshot` 会
        # `expunge_all()`（解绑 identity map），此后再经 `plan` 取属性属于访问已分离对象。
        # 先取出来，后面全部用局部变量，与会话状态无关。
        proposal_version = plan.input_snapshot_version
        production_date = plan.production_date
        pending_status = plan.status

        # ---- ① 陈旧检测（R12.2–3）----
        current_version = current_input_snapshot_version(self.session)
        if current_version != proposal_version:
            audit.append(
                event_category="STALE_PROPOSAL_REJECTED",
                event_type="STALE_PROPOSAL_REJECTED",
                actor=actor,
                # 载荷只有两个版本号，不列出变化的实体与字段（R12.3 的载荷范围）。
                payload={
                    "proposal_version": proposal_version,
                    "current_version": current_version,
                },
                subject_type="ProductionPlan",
                subject_id=plan_id,
                occurred_at=self.now,
            )
            return ApprovalResult(
                status=ApprovalStatus.STALE_PROPOSAL,
                plan_id=plan_id,
                proposal_version=proposal_version,
                current_version=current_version,
            )

        # ---- ② 重校验（R11.3 / R12.5 / R6.5）----
        # 在当前快照上完整重跑校验。快照要求一个干净会话——此前只做了读，无未 flush 的改动。
        # `feasibility` 在 expunge 前取出（load_snapshot 会解绑 plan）。
        feasibility = plan.feasibility
        snapshot = load_snapshot(
            self.session,
            now=self.now,
            production_date=production_date,
            snapshot_version=proposal_version,
        )
        candidate = _candidate_from_plan(self.session, plan_id, feasibility)
        report = validate(candidate, snapshot)
        if not report.is_feasible:
            audit.append(
                event_category="APPROVAL_ACTION",
                event_type="APPROVAL_REVALIDATION_FAILED",
                actor=actor,
                payload={
                    "plan_id": plan_id,
                    "violation_count": len(report.violations),
                    "violations": [v.model_dump(mode="json") for v in report.violations],
                },
                subject_type="ProductionPlan",
                subject_id=plan_id,
                occurred_at=self.now,
            )
            # 状态保持 PENDING_APPROVAL：拦住违规计划，但不销毁提案（R11.6）。
            return ApprovalResult(
                status=ApprovalStatus.REVALIDATION_FAILED,
                plan_id=plan_id,
                current_status=plan.status,
                violations=report.violations,
            )

        # ---- ③ 乐观并发 + 原子状态迁移（R12.7）----
        try:
            # 先 supersede 旧 ACTIVE，再把目标置 ACTIVE——顺序是 load-bearing 的：
            # `ux_active_per_day` 是 `WHERE status='ACTIVE'` 的部分唯一索引，SQLite 逐语句
            # 校验，因此「先就位、后让位」会让新计划与仍为 ACTIVE 的旧计划在同一生产日短暂
            # 并存，UPDATE 当场撞索引（`UNIQUE constraint failed:
            # production_plans.production_date`）→ 审批 500。反过来（先让位、后就位）避免这一
            # 瞬时冲突。此处与 `activate_internal()` 的 ③ 段同口径。
            supersede_previous_active(
                self.session, plan.production_date, except_id=plan_id
            )
            self.session.flush()
            affected = update_plan_status_if_version(
                self.session, plan_id, ACTIVE_STATUS, expected_version
            )
            if affected == 0:
                # 另一线程抢先推进了行版本。回滚本事务里已发出的语句（含上面的让位 UPDATE，
                # 因此原 ACTIVE 计划仍是 ACTIVE），不激活本计划。
                self.session.rollback()
                return ApprovalResult(
                    status=ApprovalStatus.CONCURRENT_MODIFICATION,
                    plan_id=plan_id,
                    current_status=plan.status,
                )

            # 收尾：写审批记录（含完整重校验结果），同事务。旧 ACTIVE 已在上面让位。
            self.session.add(
                orm.PlanApproval(
                    approval_id=f"APR-{uuid4().hex[:16]}",
                    plan_id=plan_id,
                    action=APPROVE_ACTION,
                    actor=actor,
                    timestamp=self.now,
                    rejection_reason=None,
                    revalidation_result=_revalidation_result(report),
                    modifications=None,
                )
            )
            self.session.commit()
        except Exception:
            self.session.rollback()
            raise

        # 审计（走独立连接，提交后写）：审批动作本身留痕（R11.9）。
        audit.append(
            event_category="APPROVAL_ACTION",
            event_type=APPROVE_ACTION,
            actor=actor,
            payload={
                "plan_id": plan_id,
                "production_date": plan.production_date.isoformat(),
                "input_snapshot_version": plan.input_snapshot_version,
            },
            subject_type="ProductionPlan",
            subject_id=plan_id,
            occurred_at=self.now,
        )

        # 提交之后才发事件：订阅者（任务 8.5 风险扫描）看到的是已落库的 ACTIVE 计划。
        self.events.emit(PlanActivated(plan_id=plan_id))

        return ApprovalResult(status=ApprovalStatus.OK, plan_id=plan_id)

    # ----------------------------------------------------------------------
    # activate_internal —— 系统内部激活（L4 自动应用 与 一键回滚，任务 13.4，R13.7/R13.9/R13.10）
    # ----------------------------------------------------------------------

    def activate_internal(
        self, plan_id: str, *, actor: str = "SYSTEM"
    ) -> ApprovalResult:
        """把一个 `PENDING_APPROVAL` 计划激活为 `ACTIVE`——**仍走一次完整硬约束重校验**（R13.10）。

        这是 design.md §8 状态机表里 `ACTIVE → SUPERSEDED`「或 P1 activate_internal」那条注解的
        落点，供两处系统内部激活复用：

        - **L4 自动应用**（`auto_apply_minor_enabled=true` 且 `IMPACT_MINOR`）：确定性重排刚产出
          的修订计划被系统自动激活，无需人工点「批准」。
        - **一键回滚**（`AutoAppliedChange.revert`）：从 `snapshot_before` 重建的计划被重新激活。

        与 `approve()` 的异同：

        - **相同（不可削弱的部分）**：② 完整重校验（在当前快照上重跑 `validate`，任一硬约束违反
          即拒绝、状态保持 `PENDING_APPROVAL`）、③ 原子状态迁移经**唯一**的
          `update_plan_status_if_version`、supersede 旧 ACTIVE、写 `plan_approvals`、emit
          `PlanActivated`。**回滚也不能产生违规计划**（R13.10）正是靠这道重校验守住。
        - **不同**：不做 ① 陈旧检测（L4/回滚发生在系统内部、计划刚基于当前快照构建，`input_
          snapshot_version` 天然一致）；不要求调用方传 `expected_version`（内部激活不面临两个
          规划员并发点批准的竞态，用计划当前 `version` 做乐观并发即可）。审批记录的 `action`
          记为 `AUTO_APPLY`，与人工 `APPROVE` 在审计上可区分。

        返回 `ApprovalResult`：`OK` 表示已激活；`REVALIDATION_FAILED` 附违反清单且计划仍
        `PENDING_APPROVAL`（回滚/自动应用被拒，绝不落一个违规的 ACTIVE）；
        `INVALID_STATE_TRANSITION` 表示计划不是可激活的 `PENDING_APPROVAL`。
        """
        plan = self.session.get(orm.ProductionPlan, plan_id)
        if plan is None or not is_allowed_transition(plan.status, PlanStatus.ACTIVE):
            return ApprovalResult(
                status=ApprovalStatus.INVALID_STATE_TRANSITION,
                plan_id=plan_id,
                current_status=None if plan is None else plan.status,
            )

        production_date = plan.production_date
        proposal_version = plan.input_snapshot_version
        feasibility = plan.feasibility
        current_version = plan.version

        # ---- ② 完整重校验（R13.10：回滚/自动应用也不能产生违规计划）----
        snapshot = load_snapshot(
            self.session,
            now=self.now,
            production_date=production_date,
            snapshot_version=proposal_version,
        )
        candidate = _candidate_from_plan(self.session, plan_id, feasibility)
        report = validate(candidate, snapshot)
        if not report.is_feasible:
            audit.append(
                event_category="APPROVAL_ACTION",
                event_type="AUTO_APPLY_REVALIDATION_FAILED",
                actor=actor,
                payload={
                    "plan_id": plan_id,
                    "violation_count": len(report.violations),
                    "violations": [v.model_dump(mode="json") for v in report.violations],
                },
                subject_type="ProductionPlan",
                subject_id=plan_id,
                occurred_at=self.now,
            )
            return ApprovalResult(
                status=ApprovalStatus.REVALIDATION_FAILED,
                plan_id=plan_id,
                current_status=plan.status,
                violations=report.violations,
            )

        # ---- ③ 乐观并发 + 原子状态迁移（经唯一的 update_plan_status_if_version）----
        try:
            # 先 supersede 旧 ACTIVE，再把目标置 ACTIVE：`ux_active_per_day` 是部分唯一索引
            # （WHERE status='ACTIVE'），SQLite 逐语句校验，若先置目标为 ACTIVE 会与仍 ACTIVE 的
            # 旧计划在同一生产日短暂并存而撞索引。顺序反过来（先让位、后就位）避免这一瞬时冲突。
            superseded = supersede_previous_active(
                self.session, production_date, except_id=plan_id
            )
            self.session.flush()
            affected = update_plan_status_if_version(
                self.session, plan_id, ACTIVE_STATUS, current_version
            )
            if affected == 0:
                self.session.rollback()
                return ApprovalResult(
                    status=ApprovalStatus.CONCURRENT_MODIFICATION,
                    plan_id=plan_id,
                    current_status=plan.status,
                )
            del superseded  # 已让位；此处不需要返回值，保留调用是为其副作用
            self.session.add(
                orm.PlanApproval(
                    approval_id=f"APR-{uuid4().hex[:16]}",
                    plan_id=plan_id,
                    action="AUTO_APPLY",
                    actor=actor,
                    timestamp=self.now,
                    rejection_reason=None,
                    revalidation_result=_revalidation_result(report),
                    modifications=None,
                )
            )
            self.session.commit()
        except Exception:
            self.session.rollback()
            raise

        audit.append(
            event_category="APPROVAL_ACTION",
            event_type="AUTO_APPLY",
            actor=actor,
            payload={
                "plan_id": plan_id,
                "production_date": production_date.isoformat(),
                "input_snapshot_version": proposal_version,
            },
            subject_type="ProductionPlan",
            subject_id=plan_id,
            occurred_at=self.now,
        )
        self.events.emit(PlanActivated(plan_id=plan_id))
        return ApprovalResult(status=ApprovalStatus.OK, plan_id=plan_id)

    # ----------------------------------------------------------------------
    # REJECT（R11.4、R18.1、design.md §4.1）
    # ----------------------------------------------------------------------

    def reject(self, plan_id: str, actor: str, rejection_reason: str) -> RejectResult:
        """拒绝一个待审批计划：置 `REJECTED`，保留原 `ACTIVE` 不变（R11.4）。

        三道闸门：

        1. **状态检查**：非 `PENDING_APPROVAL` → `INVALID_STATE_TRANSITION`（只有待审批的
           提案谈得上「拒绝」）。
        2. **理由长度**：`rejection_reason` 去首尾空白后不足 5 字符 → `REASON_TOO_SHORT`。
           理由按不受信任输入处理（R23.1）：它由规划员自由输入，会进 `EVAL-203` 的输入路径
           （任务 5.8）。本层不解释它的任何指令语义，只作为一段文本原样存库——注入检测与
           包裹是 `Guardrail_Layer` 读取时的职责，存储侧不做也不该做过滤。
        3. **落库**：把 `status` 置 `REJECTED`、记 `rejection_reason`，同事务写
           `plan_approvals`（`action=REJECT`）与 `planner_decisions`（供偏好蒸馏，R18.1）。

        **原 `ACTIVE` 计划一动不动**：拒绝一个提案不影响当前正在执行的计划。R11.4 的语义是
        「不采纳这个提案」，而不是「回到没有计划的状态」。

        乐观并发：这里不需要 `expected_version`——拒绝不改变「哪个计划是 ACTIVE」这个受
        `ux_active_per_day` 保护的不变量，两个规划员同时拒绝同一提案，结果都是 `REJECTED`，
        无竞态可言。
        """
        plan = self.session.get(orm.ProductionPlan, plan_id)
        # 迁移许可表：`PENDING_APPROVAL → REJECTED` 是表内唯一到达 REJECTED 的迁移。
        if plan is None or not is_allowed_transition(plan.status, PlanStatus.REJECTED):
            return RejectResult(
                status=RejectStatus.INVALID_STATE_TRANSITION,
                plan_id=plan_id,
                current_status=None if plan is None else plan.status,
            )

        if len(rejection_reason.strip()) < MIN_REJECTION_REASON_LENGTH:
            return RejectResult(
                status=RejectStatus.REASON_TOO_SHORT,
                plan_id=plan_id,
                current_status=plan.status,
            )

        breakdown_snapshot = _objective_breakdown_snapshot(self.session, plan_id)

        try:
            plan.status = "REJECTED"
            plan.rejection_reason = rejection_reason
            self.session.add(
                orm.PlanApproval(
                    approval_id=f"APR-{uuid4().hex[:16]}",
                    plan_id=plan_id,
                    action=REJECT_ACTION,
                    actor=actor,
                    timestamp=self.now,
                    rejection_reason=rejection_reason,
                    # 拒绝路径不重校验（没有要激活的东西），但列非空：存一个显式的空结果。
                    revalidation_result={"skipped": True, "reason": "REJECT"},
                    modifications=None,
                )
            )
            self.session.add(
                orm.PlannerDecision(
                    decision_id=f"DEC-{uuid4().hex[:16]}",
                    plan_id=plan_id,
                    action=REJECT_ACTION,
                    rejection_reason=rejection_reason,
                    modifications=None,
                    objective_breakdown_snapshot=breakdown_snapshot,
                    created_at=self.now,
                )
            )
            self.session.commit()
        except Exception:
            self.session.rollback()
            raise

        audit.append(
            event_category="APPROVAL_ACTION",
            event_type=REJECT_ACTION,
            actor=actor,
            payload={
                "plan_id": plan_id,
                "rejection_reason_length": len(rejection_reason),
            },
            subject_type="ProductionPlan",
            subject_id=plan_id,
            occurred_at=self.now,
        )
        return RejectResult(status=RejectStatus.OK, plan_id=plan_id)

    # ----------------------------------------------------------------------
    # MODIFY（R11.5–7、R12.6、R18.1、design.md §4.1）
    # ----------------------------------------------------------------------

    def modify(
        self, plan_id: str, actor: str, modifications: tuple[Modification, ...]
    ) -> ModifyResult:
        """对一个待审批计划应用 5 类结构化修改，生成**新的** `PENDING_APPROVAL` 版本。

        步骤（design.md §4.1）：

        1. **状态检查**：非 `PENDING_APPROVAL` → `INVALID_STATE_TRANSITION`。
        2. **应用修改**：在内存里把原计划的 `scheduled_jobs` 依次应用五类修改，得到一个新的
           `PlanCandidate`。任一修改指向计划里不存在的 `job_id` → `JOB_NOT_IN_PLAN`（不落库）。
        3. **重校验**：对修改后的候选跑 `Constraint_Validator.validate`。**有违反 → 返回违反
           清单且不改变任何状态**（R11.6）——原计划仍 `PENDING_APPROVAL`，不生成新版本。
        4. **生成新版本**：无违反才落库。新计划 `status = PENDING_APPROVAL`、
           `plan_version = 原 + 1`、`origin = MODIFY`，原计划置 `SUPERSEDED`。**绝不直接激活**
           （R11.7）：修改后的计划仍须走一次完整审批。
        5. **决策记录**：写 `plan_approvals`（`action=MODIFY`，含修改载荷与重校验结果）与
           `planner_decisions`（供偏好蒸馏，R18.1，含 `objective_breakdown_snapshot`）。

        **单一 `PENDING_APPROVAL`（R12.6）**：新版本与原计划同一 `production_date`。原计划在
        同一事务里先置 `SUPERSEDED` 再插新的 `PENDING_APPROVAL`，因此 `ux_pending_per_day`
        部分唯一索引在提交时只见到一个 `PENDING_APPROVAL`。若该生产日上另有一个**互不相关**
        的 `PENDING_APPROVAL`（不是本次要 supersede 的那个），插入会撞唯一索引 →
        `PENDING_PLAN_EXISTS`，API 给「取消既有提案」入口。
        """
        plan = self.session.get(orm.ProductionPlan, plan_id)
        # MODIFY 只作用于**待审批**提案：它把原计划迁移到 SUPERSEDED 并生成一个新的
        # PENDING_APPROVAL 版本（R11.7）。虽然表内 `ACTIVE → SUPERSEDED` 也是合法迁移，
        # 但那条只属于 `approve` 内的 supersede / P1 回滚，不属于 MODIFY —— 因此这里把源
        # 状态显式钉在 PENDING_APPROVAL，而不是只问「能不能到 SUPERSEDED」。
        if plan is None or plan.status != PlanStatus.PENDING_APPROVAL:
            return ModifyResult(
                status=ModifyStatus.INVALID_STATE_TRANSITION,
                source_plan_id=plan_id,
                current_status=None if plan is None else plan.status,
            )

        production_date = plan.production_date
        source_version = plan.plan_version
        input_snapshot_version = plan.input_snapshot_version
        feasibility = plan.feasibility
        trace_id = plan.generated_by_trace_id

        # ---- 步 2：应用修改（纯内存） ----
        original = _candidate_from_plan(self.session, plan_id, feasibility)
        locked_job_ids = _locked_job_ids(self.session, plan_id)
        try:
            revised, revised_locked = _apply_modifications(
                original, locked_job_ids, modifications
            )
        except _JobNotInPlan as error:
            return ModifyResult(
                status=ModifyStatus.JOB_NOT_IN_PLAN,
                source_plan_id=plan_id,
                current_status=plan.status,
                missing_job_id=error.job_id,
            )

        # ---- 步 3：重校验（修改后无条件执行，R11.6） ----
        snapshot = load_snapshot(
            self.session,
            now=self.now,
            production_date=production_date,
            snapshot_version=input_snapshot_version,
        )
        report = validate(revised, snapshot)
        if not report.is_feasible:
            # 不改变任何状态（R11.6）：原计划仍 PENDING_APPROVAL，不生成新版本。
            audit.append(
                event_category="APPROVAL_ACTION",
                event_type="MODIFICATION_REVALIDATION_FAILED",
                actor=actor,
                payload={
                    "plan_id": plan_id,
                    "violation_count": len(report.violations),
                    "violations": [v.model_dump(mode="json") for v in report.violations],
                },
                subject_type="ProductionPlan",
                subject_id=plan_id,
                occurred_at=self.now,
            )
            return ModifyResult(
                status=ModifyStatus.MODIFICATION_REVALIDATION_FAILED,
                source_plan_id=plan_id,
                current_status=plan.status,
                violations=report.violations,
            )

        # ---- 步 4 + 5：生成新版本并写决策记录（同事务） ----
        new_plan_id = f"PLAN-{uuid4().hex[:12]}"
        modifications_payload = [m.model_dump(mode="json") for m in modifications]
        breakdown_snapshot = _objective_breakdown_snapshot(self.session, plan_id)

        try:
            # 三步落库，顺序由两条约束共同决定，不能合并：
            #
            #   ① 原计划先退出 PENDING_APPROVAL（置 SUPERSEDED），flush。
            #   ② 插入新的 PENDING_APPROVAL 版本，flush。
            #   ③ 回填原计划的 `superseded_by_plan_id`。
            #
            # 为什么 `superseded_by_plan_id` 要留到 ③：它是指向 `production_plans` 的自引用外键，
            # 新计划在 ② 之前还不存在，在 ① 就回填会撞 FOREIGN KEY 约束。为什么 ① 必须在 ② 之前：
            # `ux_pending_per_day` 是部分唯一索引，若新计划先落库、旧计划仍是 PENDING_APPROVAL，
            # 同一生产日两个 PENDING_APPROVAL 撞唯一索引。
            #
            # 用语句级 UPDATE 而非改 ORM 属性：`load_snapshot` 在上一步做过 `expunge_all()`，此时
            # `plan` 已从会话分离，改它的属性不会被 flush。
            self.session.execute(
                update(orm.ProductionPlan)
                .where(orm.ProductionPlan.plan_id == plan_id)
                .values(status=SUPERSEDED_STATUS)
            )
            self.session.flush()

            self.session.add(
                orm.ProductionPlan(
                    plan_id=new_plan_id,
                    production_date=production_date,
                    status=REVISED_STATUS,
                    feasibility=revised.feasibility,
                    plan_version=source_version + 1,
                    version=1,
                    input_snapshot_version=input_snapshot_version,
                    origin=MODIFY_ORIGIN,
                    supersedes_plan_id=plan_id,
                    generated_by_trace_id=trace_id,
                    created_at=self.now,
                )
            )
            self.session.flush()

            self.session.execute(
                update(orm.ProductionPlan)
                .where(orm.ProductionPlan.plan_id == plan_id)
                .values(superseded_by_plan_id=new_plan_id)
            )

            for sj in revised.scheduled_jobs:
                self.session.add(
                    orm.ScheduledJob(
                        scheduled_job_id=f"ROW-{uuid4().hex[:16]}",
                        plan_id=new_plan_id,
                        job_id=sj.job_id,
                        machine_id=sj.machine_id,
                        worker_id=sj.worker_id,
                        start_time=sj.start_time,
                        end_time=sj.end_time,
                        setup_minutes=sj.setup_minutes,
                        changeover_minutes=sj.changeover_minutes,
                        locked=sj.job_id in revised_locked,
                    )
                )

            self.session.add(
                orm.PlanApproval(
                    approval_id=f"APR-{uuid4().hex[:16]}",
                    plan_id=plan_id,
                    action=MODIFY_ACTION,
                    actor=actor,
                    timestamp=self.now,
                    rejection_reason=None,
                    revalidation_result=_revalidation_result(report),
                    modifications=modifications_payload,
                )
            )
            self.session.add(
                orm.PlannerDecision(
                    decision_id=f"DEC-{uuid4().hex[:16]}",
                    plan_id=plan_id,
                    action=MODIFY_ACTION,
                    rejection_reason=None,
                    modifications=modifications_payload,
                    objective_breakdown_snapshot=breakdown_snapshot,
                    created_at=self.now,
                )
            )
            self.session.commit()
        except IntegrityError:
            # ux_pending_per_day 冲突：该生产日已有另一个互不相关的 PENDING_APPROVAL。
            self.session.rollback()
            return ModifyResult(
                status=ModifyStatus.PENDING_PLAN_EXISTS,
                source_plan_id=plan_id,
                current_status=PENDING_STATUS,
            )
        except Exception:
            self.session.rollback()
            raise

        audit.append(
            event_category="APPROVAL_ACTION",
            event_type=MODIFY_ACTION,
            actor=actor,
            payload={
                "source_plan_id": plan_id,
                "new_plan_id": new_plan_id,
                "plan_version": source_version + 1,
                "modification_count": len(modifications),
                "modification_kinds": [m.kind for m in modifications],
            },
            subject_type="ProductionPlan",
            subject_id=new_plan_id,
            occurred_at=self.now,
        )
        return ModifyResult(
            status=ModifyStatus.OK,
            source_plan_id=plan_id,
            new_plan_id=new_plan_id,
        )
