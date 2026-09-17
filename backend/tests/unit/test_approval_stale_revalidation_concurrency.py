"""`Approval_Service.approve()` 的陈旧检测、重校验失败与并发互斥（任务 3.5）。

承接 design.md Testing Strategy §3 原属性 16、17：

- **陈旧检测（R12.2–3）**：提案生成后改动规划相关数据（版本号自动前进一格），审批时
  `current_input_snapshot_version()` 与 `plan.input_snapshot_version` 不等 →
  `STALE_PROPOSAL`；返回载荷**恰为两个版本号**（不列变化实体/字段）；写一条
  `STALE_PROPOSAL_REJECTED` 审计。
- **重校验失败（R11.3 / R12.5 / R6.5）**：版本号不变（陈旧闸门放行），但提案的已排产作业
  被改到机器可用窗之外 → 重校验报 `MACHINE_UNAVAILABLE` → `REVALIDATION_FAILED`，且计划
  状态仍为 `PENDING_APPROVAL`（R11.6：拦住违规计划但不销毁提案）。
- **并发互斥（R12.7）**：两个线程持各自会话、以**同一** `expected_version` 同时 `approve`
  同一提案 → 乐观并发的条件 UPDATE 使恰好一个成功、另一个 `CONCURRENT_MODIFICATION`；
  该生产日的 `ACTIVE` 计数为 1（`ux_active_per_day` 不变量）。

与 `test_approval_reject_modify.py` 不重叠：那里覆盖 REJECT / MODIFY，这里只覆盖 approve
的三条闸门。走真实 SQLite + 真实 seed 数据 + 真实内核（`load_snapshot` /
`Constraint_Validator`），不 mock。
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.db import models as orm
from app.db.models import Base
from app.main import create_app
from app.seed.dataset import DEMO_ANCHOR
from app.seed.loader import load_demo_data
from app.services.approval import ApprovalService, ApprovalStatus
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
    """在应用会话工厂上开一个新 `ApprovalService`。`now` 取演示锚点（与生成端点同口径）。"""
    factory: sessionmaker[Session] = application.state.session_factory  # type: ignore[attr-defined]
    db = factory()
    return ApprovalService(session=db, now=DEMO_ANCHOR, events=EventBus()), db


def _optimistic_version(application: object, plan_id: str) -> int:
    """读计划当前的乐观并发版本号（`production_plans.version`，approve 的 `expected_version`）。"""
    factory: sessionmaker[Session] = application.state.session_factory  # type: ignore[attr-defined]
    with factory() as db:
        row = db.get(orm.ProductionPlan, plan_id)
        assert row is not None
        return row.version


# --------------------------------------------------------------------------
# ① 陈旧检测（R12.2–3、R12.7 前置闸门）
# --------------------------------------------------------------------------


def test_approve_stale_proposal_after_data_change(
    client: TestClient, application: object
) -> None:
    """提案后改数据 → `STALE_PROPOSAL` + 载荷恰为两个版本号 + `STALE_PROPOSAL_REJECTED` 审计。"""
    plan = _generate_pending_plan(client)
    plan_id = plan["plan_id"]
    proposal_version = _snapshot_version_of(application, plan_id)
    expected_version = _optimistic_version(application, plan_id)

    # 改动一张规划相关表（machines）——`after_flush` 钩子在同一事务里把版本号推进一格。
    factory: sessionmaker[Session] = application.state.session_factory  # type: ignore[attr-defined]
    with factory() as db:
        machine = db.execute(select(orm.Machine)).scalars().first()
        assert machine is not None
        machine.rate_multiplier = machine.rate_multiplier + 1
        db.commit()

    current_version = _current_snapshot_version(application)
    assert current_version != proposal_version, "改数据后版本号必须前进"

    service, db2 = _service(application)
    try:
        result = service.approve(plan_id, actor="PLANNER", expected_version=expected_version)
    finally:
        db2.close()

    assert result.status is ApprovalStatus.STALE_PROPOSAL
    # 载荷恰为两个版本号（R12.3）：提案版本与当前版本，别无其他。
    assert result.proposal_version == proposal_version
    assert result.current_version == current_version
    assert result.violations == ()

    # 计划状态未变：陈旧路径不激活也不改状态。
    factory2: sessionmaker[Session] = application.state.session_factory  # type: ignore[attr-defined]
    with factory2() as db3:
        row = db3.get(orm.ProductionPlan, plan_id)
        assert row is not None
        assert row.status == "PENDING_APPROVAL"

        # 写了一条 STALE_PROPOSAL_REJECTED 审计，载荷同样只有两个版本号。
        audit_row = db3.execute(
            select(orm.AuditLog).where(
                orm.AuditLog.event_type == "STALE_PROPOSAL_REJECTED",
                orm.AuditLog.subject_id == plan_id,
            )
        ).scalar_one()
        assert audit_row.event_category == "STALE_PROPOSAL_REJECTED"
        assert audit_row.payload == {
            "proposal_version": proposal_version,
            "current_version": current_version,
        }


def _snapshot_version_of(application: object, plan_id: str) -> int:
    """提案计划记录的 `input_snapshot_version`。"""
    factory: sessionmaker[Session] = application.state.session_factory  # type: ignore[attr-defined]
    with factory() as db:
        row = db.get(orm.ProductionPlan, plan_id)
        assert row is not None
        return row.input_snapshot_version


def _current_snapshot_version(application: object) -> int:
    """当前最大快照版本号（`MAX(input_snapshots.snapshot_version)`）。"""
    factory: sessionmaker[Session] = application.state.session_factory  # type: ignore[attr-defined]
    with factory() as db:
        value = db.execute(select(func.max(orm.InputSnapshot.snapshot_version))).scalar_one()
        return int(value or 0)


# --------------------------------------------------------------------------
# ② 重校验失败（R11.3 / R12.5 / R6.5、R11.6）
# --------------------------------------------------------------------------


def test_approve_revalidation_failed_keeps_pending(
    client: TestClient, application: object
) -> None:
    """重校验失败 → `REVALIDATION_FAILED` 且状态仍为 `PENDING_APPROVAL`（R11.6）。"""
    plan = _generate_pending_plan(client)
    plan_id = plan["plan_id"]
    expected_version = _optimistic_version(application, plan_id)

    # 把提案里某个已排产作业挪到机器可用窗之外的远古时间。scheduled_jobs 不是规划相关表，
    # 因此这次改动**不推进快照版本**——陈旧闸门放行，重校验才是被触发的那道闸门。
    factory: sessionmaker[Session] = application.state.session_factory  # type: ignore[attr-defined]
    with factory() as db:
        sched = db.execute(
            select(orm.ScheduledJob).where(orm.ScheduledJob.plan_id == plan_id)
        ).scalars().first()
        assert sched is not None
        sched.start_time = datetime(2000, 1, 1, 8, 0)
        sched.end_time = datetime(2000, 1, 1, 9, 0)
        db.commit()

    service, db2 = _service(application)
    try:
        result = service.approve(plan_id, actor="PLANNER", expected_version=expected_version)
    finally:
        db2.close()

    assert result.status is ApprovalStatus.REVALIDATION_FAILED
    assert result.violations, "重校验失败必须附违反清单"
    assert result.current_status == "PENDING_APPROVAL"

    # 状态保持 PENDING_APPROVAL，不激活（R11.6）；写了一条重校验失败审计。
    factory2: sessionmaker[Session] = application.state.session_factory  # type: ignore[attr-defined]
    with factory2() as db3:
        row = db3.get(orm.ProductionPlan, plan_id)
        assert row is not None
        assert row.status == "PENDING_APPROVAL"

        audit_row = db3.execute(
            select(orm.AuditLog).where(
                orm.AuditLog.event_type == "APPROVAL_REVALIDATION_FAILED",
                orm.AuditLog.subject_id == plan_id,
            )
        ).scalar_one()
        assert audit_row.event_category == "APPROVAL_ACTION"
        assert audit_row.payload["violation_count"] >= 1


# --------------------------------------------------------------------------
# ③ 并发互斥（R12.7）
# --------------------------------------------------------------------------


def test_approve_concurrent_only_one_succeeds(
    client: TestClient, application: object
) -> None:
    """两线程同时 approve → 恰好一个成功、另一个 `CONCURRENT_MODIFICATION`、`ACTIVE` 计数为 1。"""
    plan = _generate_pending_plan(client)
    plan_id = plan["plan_id"]
    expected_version = _optimistic_version(application, plan_id)

    factory: sessionmaker[Session] = application.state.session_factory  # type: ignore[attr-defined]
    results: list[ApprovalStatus] = []
    results_lock = threading.Lock()
    barrier = threading.Barrier(2)

    def worker() -> None:
        db = factory()
        service = ApprovalService(session=db, now=DEMO_ANCHOR, events=EventBus())
        try:
            # 两线程尽量在同一时刻发起 approve，且用同一个 expected_version：
            # 乐观并发的条件 UPDATE 保证只有一个 rowcount==1，另一个 rowcount==0。
            barrier.wait(timeout=5)
            outcome = service.approve(
                plan_id, actor="PLANNER", expected_version=expected_version
            )
        finally:
            db.close()
        with results_lock:
            results.append(outcome.status)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert len(results) == 2
    ok_count = sum(1 for status in results if status is ApprovalStatus.OK)
    conflict_count = sum(
        1 for status in results if status is ApprovalStatus.CONCURRENT_MODIFICATION
    )
    assert ok_count == 1, f"恰好一个 approve 成功，实际 {results}"
    assert conflict_count == 1, f"另一个必须是 CONCURRENT_MODIFICATION，实际 {results}"

    # 该生产日的 ACTIVE 计数为 1（ux_active_per_day 不变量）。
    factory2: sessionmaker[Session] = application.state.session_factory  # type: ignore[attr-defined]
    with factory2() as db2:
        plan_row = db2.get(orm.ProductionPlan, plan_id)
        assert plan_row is not None
        active_count = db2.execute(
            select(func.count())
            .select_from(orm.ProductionPlan)
            .where(
                orm.ProductionPlan.production_date == plan_row.production_date,
                orm.ProductionPlan.status == "ACTIVE",
            )
        ).scalar_one()
        assert active_count == 1
        # 成功的那次把提案本身激活了。
        assert plan_row.status == "ACTIVE"
