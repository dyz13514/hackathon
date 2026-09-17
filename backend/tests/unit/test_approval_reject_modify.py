"""`Approval_Service.reject()` / `modify()` 与决策记录（任务 3.2，R11.4–7 / R12.6 / R18.1）。

走真实 SQLite + 真实 seed 数据 + 真实内核（`load_snapshot` / `Constraint_Validator`），不
mock：本任务交付的正是「拒绝/修改在这套 schema 与内核上的确定性行为」。

先经 `POST /plans/generate` 铺出一个 `PENDING_APPROVAL` 计划（形态 A 确定性流水线），再对它
施加 REJECT / MODIFY，断言：

- REJECT：置 `REJECTED`、理由 <5 字符被拒、写 `plan_approvals` 与 `planner_decisions`；
- MODIFY 无违反：生成新的 `PENDING_APPROVAL` 版本（`plan_version+1`，原计划 `SUPERSEDED`），
  绝不直接激活；`LOCK_JOB` 置 `scheduled_jobs.locked`；写决策记录；
- MODIFY 有违反：返回违反清单且**不改变任何状态**；
- MODIFY 指向不存在的作业：`JOB_NOT_IN_PLAN`，不落库；
- `ux_pending_per_day`：单一 `PENDING_APPROVAL` 在同一生产日成立。
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.db import models as orm
from app.db.models import Base
from app.main import create_app
from app.seed.loader import load_demo_data
from app.services.approval import (
    ApprovalService,
    LockJob,
    MoveTime,
    ModifyStatus,
    ReassignMachine,
    RejectStatus,
    RemoveFromPlan,
)
from app.services.events import EventBus
from app.settings import Settings

LOGIN = "/api/auth/login"
GENERATE = "/api/plans/generate"


@pytest.fixture
def app_settings(valid_env: pytest.MonkeyPatch, tmp_path: Path) -> Settings:
    db_file = tmp_path / "approval.db"
    valid_env.setenv("DATABASE_URL", f"sqlite:///{db_file.as_posix()}")
    return Settings()  # type: ignore[call-arg]


@pytest.fixture
def application(app_settings: Settings) -> object:
    app = create_app(app_settings)
    Base.metadata.create_all(app.state.engine)
    factory: sessionmaker[Session] = app.state.session_factory
    with factory() as session:
        load_demo_data(session)
        session.commit()
    return app


@pytest.fixture
def client(application: object) -> Iterator[TestClient]:
    app = application  # type: ignore[assignment]
    with TestClient(app) as test_client:  # type: ignore[arg-type]
        password = app.state.settings.session_shared_password.get_secret_value()  # type: ignore[attr-defined]
        test_client.post(LOGIN, json={"password": password})
        yield test_client


def _generate_pending_plan(client: TestClient) -> dict:
    """铺出一个 `PENDING_APPROVAL` 计划并返回其全文（含 `scheduled_jobs`）。"""
    response = client.post(GENERATE, json={})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "PENDING_APPROVAL"
    assert body["scheduled_jobs"], "seed 数据下应能排出作业"
    return body


def _service(application: object) -> tuple[ApprovalService, Session]:
    """在应用的会话工厂上开一个新 `ApprovalService`。`now` 取演示锚点（与生成端点同口径）。"""
    factory: sessionmaker[Session] = application.state.session_factory  # type: ignore[attr-defined]
    db = factory()
    from app.seed.dataset import DEMO_ANCHOR

    return ApprovalService(session=db, now=DEMO_ANCHOR, events=EventBus()), db


# --------------------------------------------------------------------------
# REJECT（R11.4、R18.1）
# --------------------------------------------------------------------------


def test_reject_sets_rejected_and_keeps_active(client: TestClient, application: object) -> None:
    """REJECT 置 `REJECTED`，写 `plan_approvals` 与 `planner_decisions`（R11.4、R18.1）。"""
    plan = _generate_pending_plan(client)
    plan_id = plan["plan_id"]

    service, db = _service(application)
    try:
        result = service.reject(plan_id, actor="PLANNER", rejection_reason="机器负载不均衡")
    finally:
        db.close()

    assert result.ok
    assert result.status is RejectStatus.OK

    factory: sessionmaker[Session] = application.state.session_factory  # type: ignore[attr-defined]
    with factory() as db2:
        row = db2.get(orm.ProductionPlan, plan_id)
        assert row is not None
        assert row.status == "REJECTED"
        assert row.rejection_reason == "机器负载不均衡"

        approval = db2.execute(
            select(orm.PlanApproval).where(orm.PlanApproval.plan_id == plan_id)
        ).scalar_one()
        assert approval.action == "REJECT"
        assert approval.rejection_reason == "机器负载不均衡"

        decision = db2.execute(
            select(orm.PlannerDecision).where(orm.PlannerDecision.plan_id == plan_id)
        ).scalar_one()
        assert decision.action == "REJECT"
        assert decision.rejection_reason == "机器负载不均衡"
        # 决策记录必须带目标拆解快照（R18.1–2 的偏好蒸馏证据）。
        assert decision.objective_breakdown_snapshot is not None
        assert "components" in decision.objective_breakdown_snapshot


def test_reject_short_reason_is_refused(client: TestClient, application: object) -> None:
    """理由不足 5 字符 → `REASON_TOO_SHORT`，状态不变（R11.4）。"""
    plan = _generate_pending_plan(client)
    plan_id = plan["plan_id"]

    service, db = _service(application)
    try:
        result = service.reject(plan_id, actor="PLANNER", rejection_reason="不行")
    finally:
        db.close()

    assert result.status is RejectStatus.REASON_TOO_SHORT

    factory: sessionmaker[Session] = application.state.session_factory  # type: ignore[attr-defined]
    with factory() as db2:
        row = db2.get(orm.ProductionPlan, plan_id)
        assert row is not None
        assert row.status == "PENDING_APPROVAL"  # 未改变


def test_reject_non_pending_is_invalid_transition(
    client: TestClient, application: object
) -> None:
    """对已 `REJECTED` 的计划再拒绝 → `INVALID_STATE_TRANSITION`。"""
    plan = _generate_pending_plan(client)
    plan_id = plan["plan_id"]

    service, db = _service(application)
    try:
        service.reject(plan_id, actor="PLANNER", rejection_reason="第一次拒绝理由")
    finally:
        db.close()

    service2, db2 = _service(application)
    try:
        result = service2.reject(plan_id, actor="PLANNER", rejection_reason="第二次拒绝理由")
    finally:
        db2.close()

    assert result.status is RejectStatus.INVALID_STATE_TRANSITION


# --------------------------------------------------------------------------
# MODIFY 无违反：生成新版本（R11.5、R11.7）
# --------------------------------------------------------------------------


def test_modify_lock_job_creates_new_pending_version(
    client: TestClient, application: object
) -> None:
    """`LOCK_JOB` 什么都不移动 → 必无违反 → 生成新 `PENDING_APPROVAL` 版本（R11.5、R11.7）。"""
    plan = _generate_pending_plan(client)
    plan_id = plan["plan_id"]
    target_job = plan["scheduled_jobs"][0]["job_id"]

    service, db = _service(application)
    try:
        result = service.modify(
            plan_id, actor="PLANNER", modifications=(LockJob(job_id=target_job),)
        )
    finally:
        db.close()

    assert result.ok, result
    assert result.status is ModifyStatus.OK
    new_plan_id = result.new_plan_id
    assert new_plan_id is not None

    factory: sessionmaker[Session] = application.state.session_factory  # type: ignore[attr-defined]
    with factory() as db2:
        source = db2.get(orm.ProductionPlan, plan_id)
        new_plan = db2.get(orm.ProductionPlan, new_plan_id)
        assert source is not None and new_plan is not None
        # 原计划被取代，绝不直接激活（R11.7）。
        assert source.status == "SUPERSEDED"
        assert source.superseded_by_plan_id == new_plan_id
        # 新版本是待审批、plan_version+1、origin=MODIFY。
        assert new_plan.status == "PENDING_APPROVAL"
        assert new_plan.plan_version == source.plan_version + 1
        assert new_plan.origin == "MODIFY"
        assert new_plan.supersedes_plan_id == plan_id

        # LOCK_JOB 置 scheduled_jobs.locked（任务 7.1 的冻结逻辑消费，R11.5）。
        locked_row = db2.execute(
            select(orm.ScheduledJob).where(
                orm.ScheduledJob.plan_id == new_plan_id,
                orm.ScheduledJob.job_id == target_job,
            )
        ).scalar_one()
        assert locked_row.locked is True

        # 决策记录写在源计划上，带修改载荷与目标拆解快照。
        decision = db2.execute(
            select(orm.PlannerDecision).where(orm.PlannerDecision.plan_id == plan_id)
        ).scalar_one()
        assert decision.action == "MODIFY"
        assert decision.modifications is not None
        assert decision.objective_breakdown_snapshot is not None


def test_modify_reassign_machine_no_violation_new_version(
    client: TestClient, application: object
) -> None:
    """`REASSIGN_MACHINE` 到同一机器（无实质变化）必无违反 → 生成新版本。

    改派到作业当前所在的机器是一个恒等修改：它不引入任何硬约束违反，因此可靠地走到「无违反
    → 新 `PENDING_APPROVAL` 版本」这条路径，同时覆盖 `REASSIGN_MACHINE` 的应用逻辑。
    """
    plan = _generate_pending_plan(client)
    plan_id = plan["plan_id"]
    job = plan["scheduled_jobs"][0]

    service, db = _service(application)
    try:
        result = service.modify(
            plan_id,
            actor="PLANNER",
            modifications=(ReassignMachine(job_id=job["job_id"], machine_id=job["machine_id"]),),
        )
    finally:
        db.close()

    assert result.ok, result
    assert result.new_plan_id is not None


# --------------------------------------------------------------------------
# MODIFY 有违反：不改变状态（R11.6）
# --------------------------------------------------------------------------


def test_modify_with_violation_does_not_change_state(
    client: TestClient, application: object
) -> None:
    """`MOVE_TIME` 到远古时间越出机器可用窗与班次 → 违反 → 状态不变（R11.6）。"""
    plan = _generate_pending_plan(client)
    plan_id = plan["plan_id"]
    target_job = plan["scheduled_jobs"][0]["job_id"]

    service, db = _service(application)
    try:
        result = service.modify(
            plan_id,
            actor="PLANNER",
            modifications=(
                MoveTime(
                    job_id=target_job,
                    start_time=datetime(2000, 1, 1, 8, 0),
                    end_time=datetime(2000, 1, 1, 9, 0),
                ),
            ),
        )
    finally:
        db.close()

    assert result.status is ModifyStatus.MODIFICATION_REVALIDATION_FAILED
    assert result.violations  # 附违反清单

    factory: sessionmaker[Session] = application.state.session_factory  # type: ignore[attr-defined]
    with factory() as db2:
        row = db2.get(orm.ProductionPlan, plan_id)
        assert row is not None
        assert row.status == "PENDING_APPROVAL"  # 未改变
        # 不生成新版本：没有任何 origin=MODIFY 的计划。
        modify_plans = db2.execute(
            select(orm.ProductionPlan).where(orm.ProductionPlan.origin == "MODIFY")
        ).scalars().all()
        assert modify_plans == []


def test_modify_unknown_job_is_rejected(client: TestClient, application: object) -> None:
    """修改指向计划里不存在的作业 → `JOB_NOT_IN_PLAN`，不落库。"""
    plan = _generate_pending_plan(client)
    plan_id = plan["plan_id"]

    service, db = _service(application)
    try:
        result = service.modify(
            plan_id,
            actor="PLANNER",
            modifications=(RemoveFromPlan(job_id="DOES-NOT-EXIST-OP1"),),
        )
    finally:
        db.close()

    assert result.status is ModifyStatus.JOB_NOT_IN_PLAN
    assert result.missing_job_id == "DOES-NOT-EXIST-OP1"

    factory: sessionmaker[Session] = application.state.session_factory  # type: ignore[attr-defined]
    with factory() as db2:
        row = db2.get(orm.ProductionPlan, plan_id)
        assert row is not None
        assert row.status == "PENDING_APPROVAL"


def test_modify_non_pending_is_invalid_transition(
    client: TestClient, application: object
) -> None:
    """对已 `REJECTED` 的计划修改 → `INVALID_STATE_TRANSITION`。"""
    plan = _generate_pending_plan(client)
    plan_id = plan["plan_id"]

    service, db = _service(application)
    try:
        service.reject(plan_id, actor="PLANNER", rejection_reason="先拒绝这个提案")
    finally:
        db.close()

    service2, db2 = _service(application)
    try:
        result = service2.modify(
            plan_id, actor="PLANNER", modifications=(LockJob(job_id="whatever"),)
        )
    finally:
        db2.close()

    assert result.status is ModifyStatus.INVALID_STATE_TRANSITION


# --------------------------------------------------------------------------
# API 层：端点接线与错误翻译（design.md Components §5 审批分组）
# --------------------------------------------------------------------------


def test_reject_endpoint_translates_result(client: TestClient) -> None:
    """`POST /plans/{id}/reject` 成功返回 200 + REJECTED；短理由返回 422。"""
    plan = _generate_pending_plan(client)
    plan_id = plan["plan_id"]

    short = client.post(f"/api/plans/{plan_id}/reject", json={"rejection_reason": "不"})
    assert short.status_code == 422
    assert short.json()["error"]["code"] == "REASON_TOO_SHORT"

    ok = client.post(
        f"/api/plans/{plan_id}/reject", json={"rejection_reason": "换型时间过长"}
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["status"] == "REJECTED"


def test_modify_endpoint_creates_new_version(client: TestClient) -> None:
    """`POST /plans/{id}/modify` 无违反 → 201 + 新的 PENDING_APPROVAL 版本 ID。"""
    plan = _generate_pending_plan(client)
    plan_id = plan["plan_id"]
    target_job = plan["scheduled_jobs"][0]["job_id"]

    response = client.post(
        f"/api/plans/{plan_id}/modify",
        json={"modifications": [{"kind": "LOCK_JOB", "job_id": target_job}]},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "PENDING_APPROVAL"
    assert body["source_plan_id"] == plan_id
    assert body["new_plan_id"] != plan_id


def test_modify_endpoint_requires_authentication(application: object) -> None:
    """未认证的修改请求得到 401（写端点受 `Session_Auth` 保护，R23.12）。"""
    app = application  # type: ignore[assignment]
    with TestClient(app) as anonymous:  # type: ignore[arg-type]
        response = anonymous.post(
            "/api/plans/PLAN-x/modify",
            json={"modifications": [{"kind": "LOCK_JOB", "job_id": "j"}]},
        )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHENTICATED"
