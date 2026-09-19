"""`Scenario_Sandbox` 两层隔离的结构性证明（任务 8.1，R16.4–6、EVAL-204、ADR-009）。

design.md Testing Strategy §1 点名的「沙箱写入阻断」测试（**非可选**，EVAL-204 的实现 +
Property 21 的具体实例）：通过内部钩子在沙箱执行中插入一次**真实**的 `UPDATE orders ...`，
断言三件事同时成立——

1. 抛 `SandboxWriteBlocked`（第 2 层引擎级监听器拦下，R16.5）；
2. 留下一条 `SANDBOX_WRITE_BLOCKED` 审计（阻断被**检测**到，而非仅被禁止）；
3. 当前 `ACTIVE` 计划的 `plan_id` / 内容 / `input_snapshot_version` 三者均未变（R16.6）；
   且生产数据表（这里以 `orders` 为代表）行内容未变。

另附几条守边界的单元断言：默认（非沙箱）语境下同样的 `UPDATE` 照常执行——证明进程级监听器
**不泄漏**到常规写路径；`load_sandbox_snapshot` 与 `load_snapshot` 同构且快照冻结（第 1 层）。

走真实 SQLite + 真实 seed + 真实内核，不 mock。不触达 LLM。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text, update
from sqlalchemy.orm import Session, sessionmaker

from app.db import models as orm
from app.db.models import Base
from app.db.sandbox_guard import SANDBOX_ACTIVE, SandboxWriteBlocked, sandbox_guard
from app.main import create_app
from app.seed.dataset import DEMO_ANCHOR
from app.seed.loader import load_demo_data
from app.services.approval import ApprovalService, ApprovalStatus
from app.services.events import EventBus
from app.services.snapshot_loader import load_sandbox_snapshot, load_snapshot
from app.settings import Settings

LOGIN = "/api/auth/login"
GENERATE = "/api/plans/generate"


# --------------------------------------------------------------------------
# 夹具（与其它 API 测试同口径）
# --------------------------------------------------------------------------


@pytest.fixture
def app_settings(valid_env: pytest.MonkeyPatch, tmp_path: Path) -> Settings:
    db_file = tmp_path / "sandbox.db"
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


def _factory(application: object) -> sessionmaker[Session]:
    return application.state.session_factory  # type: ignore[attr-defined,no-any-return]


def _activate_plan(client: TestClient, application: object) -> str:
    response = client.post(GENERATE, json={})
    assert response.status_code == 200, response.text
    plan_id = response.json()["plan_id"]
    factory = _factory(application)
    with factory() as db:
        row = db.get(orm.ProductionPlan, plan_id)
        assert row is not None
        expected_version = row.version
    db2 = factory()
    try:
        service = ApprovalService(session=db2, now=DEMO_ANCHOR, events=EventBus())
        result = service.approve(plan_id, actor="PLANNER", expected_version=expected_version)
        assert result.status is ApprovalStatus.OK, f"激活失败：{result.status}"
    finally:
        db2.close()
    return plan_id


# --------------------------------------------------------------------------
# 快照工具：给 ACTIVE 计划与 orders 表算内容指纹（用于「未变」断言）
# --------------------------------------------------------------------------


def _active_plan_fingerprint(application: object, plan_id: str) -> tuple[str, int, str]:
    """返回 (plan_id, input_snapshot_version, 内容哈希)。

    内容哈希覆盖计划头（status/plan_version/feasibility）+ 全部 `scheduled_jobs` 行，
    按 job_id 排序后 sha256——沙箱污染若改了计划的任何一处，这个指纹会变。
    """
    factory = _factory(application)
    with factory() as db:
        plan = db.get(orm.ProductionPlan, plan_id)
        assert plan is not None
        jobs = db.execute(
            select(orm.ScheduledJob)
            .where(orm.ScheduledJob.plan_id == plan_id)
            .order_by(orm.ScheduledJob.job_id)
        ).scalars().all()
        payload = {
            "plan_id": plan.plan_id,
            "status": plan.status,
            "plan_version": plan.plan_version,
            "feasibility": plan.feasibility,
            "input_snapshot_version": plan.input_snapshot_version,
            "jobs": [
                {
                    "job_id": j.job_id,
                    "machine_id": j.machine_id,
                    "worker_id": j.worker_id,
                    "start_time": j.start_time.isoformat(),
                    "end_time": j.end_time.isoformat(),
                }
                for j in jobs
            ],
        }
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True).encode("utf-8")
        ).hexdigest()
        return plan.plan_id, plan.input_snapshot_version, digest


def _orders_fingerprint(application: object) -> str:
    factory = _factory(application)
    with factory() as db:
        rows = db.execute(
            select(orm.Order.order_id, orm.Order.priority, orm.Order.notes).order_by(
                orm.Order.order_id
            )
        ).all()
    return hashlib.sha256(repr(rows).encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------
# EVAL-204：沙箱内的真实 UPDATE 被拦下 + 审计 + ACTIVE 计划三项不变
# --------------------------------------------------------------------------


def test_eval204_sandbox_blocks_real_update_and_leaves_active_plan_unchanged(
    client: TestClient, application: object
) -> None:
    """在沙箱语境中插入一次真实 `UPDATE orders`：抛 SandboxWriteBlocked + 审计 + 计划三项不变。"""
    plan_id = _activate_plan(client, application)
    before = _active_plan_fingerprint(application, plan_id)
    orders_before = _orders_fingerprint(application)

    factory = _factory(application)
    from app.db import audit as audit_mod

    audit_before = _audit_block_count(audit_mod)

    # 沙箱执行：加载冻结快照（第 1 层），然后通过内部钩子发起一次**真实**的生产数据写
    # （模拟「万一沙箱路径上重新出现了会话」）。第 2 层监听器应在语句到达游标前拦下。
    scenario_id = "SCN-eval204"
    with (
        pytest.raises(SandboxWriteBlocked),
        sandbox_guard(scenario_id),
        factory() as sandbox_session,
    ):
        # 第 1 层：冻结快照可加载且不可变（赋值即抛）。
        snap = load_sandbox_snapshot(sandbox_session, now=DEMO_ANCHOR)
        assert snap.snapshot_version >= 1
        with pytest.raises(Exception):  # noqa: B017,PT011 — frozen=True 拒绝赋值
            snap.orders[0].priority = "URGENT"  # type: ignore[misc]
        # 第 2 层：内部钩子发起真实 UPDATE orders —— 应被引擎级监听器拦截。
        sandbox_session.execute(update(orm.Order).values(priority="URGENT"))
        sandbox_session.flush()

    # 审计：恰好多了一条 SANDBOX_WRITE_BLOCKED。
    assert _audit_block_count(audit_mod) == audit_before + 1

    # ACTIVE 计划三项不变（plan_id / 内容 / input_snapshot_version）。
    after = _active_plan_fingerprint(application, plan_id)
    assert after == before, f"ACTIVE 计划被沙箱污染：{before} → {after}"

    # 生产数据（orders 行）未变——那次 UPDATE 没有落库。
    assert _orders_fingerprint(application) == orders_before


def _audit_block_count(audit_mod: object) -> int:
    """用审计模块的引擎数 SANDBOX_WRITE_BLOCKED 条数（审计走独立引擎）。"""
    engine = audit_mod.get_audit_engine()  # type: ignore[attr-defined]
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT COUNT(*) FROM audit_log WHERE event_category = 'SANDBOX_WRITE_BLOCKED'"
            )
        ).scalar_one()
    return int(rows)


# --------------------------------------------------------------------------
# 边界：监听器不泄漏到常规写路径
# --------------------------------------------------------------------------


def test_guard_does_not_leak_into_normal_writes(
    client: TestClient, application: object
) -> None:
    """非沙箱语境下，同样的 `UPDATE orders` 照常执行——进程级监听器只在 SANDBOX_ACTIVE 时拦。"""
    assert SANDBOX_ACTIVE.get() is False
    factory = _factory(application)
    with factory() as db:
        # 常规写：不在 sandbox_guard 语境内，应当成功。
        db.execute(update(orm.Order).values(notes="normal-write-ok"))
        db.commit()
    with factory() as db:
        sample = db.execute(select(orm.Order.notes).limit(1)).scalar_one()
    assert sample == "normal-write-ok"
    # 语境已退出，标记复位。
    assert SANDBOX_ACTIVE.get() is False


def test_sandbox_active_resets_after_block(client: TestClient, application: object) -> None:
    """被 SandboxWriteBlocked 终止后，SANDBOX_ACTIVE 仍被复位（finally 保证），不泄漏。"""
    _activate_plan(client, application)
    factory = _factory(application)
    with (
        pytest.raises(SandboxWriteBlocked),
        sandbox_guard("SCN-reset"),
        factory() as db,
    ):
        db.execute(update(orm.Order).values(priority="HIGH"))
        db.flush()
    assert SANDBOX_ACTIVE.get() is False
    # 复位后，常规写恢复正常。
    with factory() as db:
        db.execute(update(orm.Order).values(notes="after-reset"))
        db.commit()
    with factory() as db:
        assert db.execute(select(orm.Order.notes).limit(1)).scalar_one() == "after-reset"


def test_load_sandbox_snapshot_matches_load_snapshot(
    client: TestClient, application: object
) -> None:
    """`load_sandbox_snapshot` 与 `load_snapshot` 读出逐字段相同的冻结快照（第 1 层同构）。"""
    _activate_plan(client, application)
    factory = _factory(application)
    with factory() as db:
        plain = load_snapshot(db, now=DEMO_ANCHOR)
    with factory() as db:
        sandbox = load_sandbox_snapshot(db, now=DEMO_ANCHOR)
    assert sandbox == plain
    # 冻结：赋值抛 ValidationError（第 1 层「快照不可变」）。
    with pytest.raises(Exception):  # noqa: B017,PT011
        sandbox.orders[0].priority = "LOW"  # type: ignore[misc]
