"""可承诺交期报价（任务 13.6，P1-J ⑥，R17.1–R17.4）。

守 tasks.md 13.6 的可验收点，逐条对应：

- **最早可承诺完工日由 run_sandbox(PROMISE_DATE) 实算**（R17.1）：新订单全部作业的最晚 end_time。
- **输出被推迟订单清单与 total_tardiness_minutes 变化**（R17.2）。
- **期望交期不可满足 → 最早可行日期 + 具体约束原因**（R17.3）。
- **报价只读**（R17.4）：执行前后**数据库行数、ACTIVE 计划、input_snapshot_version 逐一不变**——
  证明报价是只读沙箱计算，不改动任何生产数据或计划。
- **API**：`POST /api/quotes/promise-date`（认证、无 ACTIVE 计划 409、不存在产品 422）。

走真实 create_app + 真实 SQLite + 真实内核，不 mock。零 LLM。
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
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
from app.services.sandbox import run_promise_date_sandbox
from app.settings import Settings

LOGIN = "/api/auth/login"
GENERATE = "/api/plans/generate"
PROMISE = "/api/quotes/promise-date"


@pytest.fixture
def app_settings(valid_env: pytest.MonkeyPatch, tmp_path: Path) -> Settings:
    db_file = tmp_path / "quotes.db"
    valid_env.setenv("DATABASE_URL", f"sqlite:///{db_file.as_posix()}")
    valid_env.setenv("LLM_MODE", "REPLAY")
    return Settings()  # type: ignore[call-arg]


@pytest.fixture
def application(app_settings: Settings) -> Iterator[object]:
    app = create_app(app_settings)
    Base.metadata.create_all(app.state.engine)
    factory: sessionmaker[Session] = app.state.session_factory
    with factory() as session:
        load_demo_data(session)
        session.commit()
    yield app
    from app.db.audit import set_audit_engine

    set_audit_engine(None)


@pytest.fixture
def client(application: object, app_settings: Settings) -> Iterator[TestClient]:
    with TestClient(application) as test_client:  # type: ignore[arg-type]
        password = app_settings.session_shared_password.get_secret_value()
        test_client.post(LOGIN, json={"password": password})
        yield test_client


def _factory(application: object) -> sessionmaker[Session]:
    return application.state.session_factory  # type: ignore[attr-defined,no-any-return]


def _activate(client: TestClient, application: object) -> str:
    resp = client.post(GENERATE, json={})
    assert resp.status_code == 200, resp.text
    plan_id = resp.json()["plan_id"]
    factory = _factory(application)
    with factory() as db:
        version = db.get(orm.ProductionPlan, plan_id).version
    with factory() as db:
        res = ApprovalService(session=db, now=DEMO_ANCHOR, events=EventBus()).approve(
            plan_id, actor="PLANNER", expected_version=version
        )
        assert res.status is ApprovalStatus.OK
    return str(plan_id)


def _a_product(application: object) -> str:
    factory = _factory(application)
    with factory() as db:
        return str(db.execute(select(orm.Product.product_id)).scalars().first())


# --------------------------------------------------------------------------
# 服务层
# --------------------------------------------------------------------------


def test_promise_date_feasible_far_due(client: TestClient, application: object) -> None:
    """充裕期望交期 → 可行且满足，earliest_completion 非空（R17.1）。"""
    _activate(client, application)
    product = _a_product(application)
    factory = _factory(application)
    with factory() as db:
        result = run_promise_date_sandbox(
            db,
            product_id=product,
            quantity=3,
            desired_due_date=DEMO_ANCHOR + timedelta(days=14),
            now=DEMO_ANCHOR,
        )
    assert result.feasible is True
    assert result.earliest_completion is not None
    assert result.desired_date_met is True
    assert result.constraint_reason is None


def test_promise_date_unmeetable_returns_earliest_and_reason(
    client: TestClient, application: object
) -> None:
    """期望交期过早（当天）→ 报最早可行日期 + 具体约束原因（R17.3）。"""
    _activate(client, application)
    product = _a_product(application)
    factory = _factory(application)
    with factory() as db:
        result = run_promise_date_sandbox(
            db,
            product_id=product,
            quantity=3,
            desired_due_date=DEMO_ANCHOR,  # 当天，几乎不可能满足
            now=DEMO_ANCHOR,
        )
    if result.feasible:
        # 可行但不满足期望：必须给出最早可行时刻与原因。
        assert result.desired_date_met is False
        assert result.earliest_completion is not None
        assert result.constraint_reason is not None
        assert "最早可承诺" in result.constraint_reason
    else:
        # 不可行：给出约束原因。
        assert result.constraint_reason is not None


def test_promise_date_reports_tardiness_delta(client: TestClient, application: object) -> None:
    """报价输出 total_tardiness 变化与被推迟订单清单（R17.2）。"""
    _activate(client, application)
    product = _a_product(application)
    factory = _factory(application)
    with factory() as db:
        result = run_promise_date_sandbox(
            db,
            product_id=product,
            quantity=50,  # 较大数量，更可能挤占既有订单
            desired_due_date=DEMO_ANCHOR + timedelta(days=3),
            now=DEMO_ANCHOR,
        )
    # delta = 场景总拖期 − ACTIVE 总拖期（确定性）。
    assert (
        result.total_tardiness_delta_minutes
        == result.total_tardiness_minutes - result.active_total_tardiness_minutes
    )
    # 被推迟订单不含这笔新询价本身（只报既有订单）。
    assert all(not oid.startswith("SANDBOX-ORD") for oid in result.deferred_order_ids)


def test_promise_date_is_read_only(client: TestClient, application: object) -> None:
    """报价只读（R17.4）：执行前后 DB 行数、ACTIVE 计划、input_snapshot_version 逐一不变。"""
    active_plan_id = _activate(client, application)
    product = _a_product(application)
    factory = _factory(application)

    def _snapshot_state() -> dict[str, object]:
        with factory() as db:
            return {
                "orders": int(
                    db.execute(select(func.count()).select_from(orm.Order)).scalar_one()
                ),
                "plans": int(
                    db.execute(select(func.count()).select_from(orm.ProductionPlan)).scalar_one()
                ),
                "scheduled_jobs": int(
                    db.execute(select(func.count()).select_from(orm.ScheduledJob)).scalar_one()
                ),
                "active_plan_id": db.execute(
                    select(orm.ProductionPlan.plan_id).where(
                        orm.ProductionPlan.status == "ACTIVE"
                    )
                ).scalars().first(),
                "active_version": db.get(orm.ProductionPlan, active_plan_id).version,
                "max_snapshot_version": db.execute(
                    select(func.max(orm.InputSnapshot.snapshot_version))
                ).scalar_one(),
            }

    before = _snapshot_state()
    with factory() as db:
        run_promise_date_sandbox(
            db,
            product_id=product,
            quantity=25,
            desired_due_date=DEMO_ANCHOR + timedelta(days=2),
            now=DEMO_ANCHOR,
        )
    after = _snapshot_state()
    assert after == before, "报价必须只读——DB / ACTIVE 计划 / input_snapshot_version 均不得变"


# --------------------------------------------------------------------------
# API
# --------------------------------------------------------------------------


def test_api_promise_date_requires_auth(application: object) -> None:
    with TestClient(application) as anon:  # type: ignore[arg-type]
        resp = anon.post(
            PROMISE,
            json={
                "product_id": "PRD-X",
                "quantity": 1,
                "desired_due_date": DEMO_ANCHOR.isoformat(),
            },
        )
    assert resp.status_code == 401, resp.text


def test_api_promise_date_no_active_plan_409(client: TestClient, application: object) -> None:
    product = _a_product(application)
    resp = client.post(
        PROMISE,
        json={
            "product_id": product,
            "quantity": 1,
            "desired_due_date": (DEMO_ANCHOR + timedelta(days=5)).isoformat(),
        },
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == "NO_ACTIVE_PLAN"


def test_api_promise_date_unknown_product_422(client: TestClient, application: object) -> None:
    _activate(client, application)
    resp = client.post(
        PROMISE,
        json={
            "product_id": "PRD-DOES-NOT-EXIST",
            "quantity": 1,
            "desired_due_date": (DEMO_ANCHOR + timedelta(days=5)).isoformat(),
        },
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == "SCENARIO_INVALID_MUTATION"


def test_api_promise_date_full_shape(client: TestClient, application: object) -> None:
    _activate(client, application)
    product = _a_product(application)
    resp = client.post(
        PROMISE,
        json={
            "product_id": product,
            "quantity": 3,
            "desired_due_date": (DEMO_ANCHOR + timedelta(days=10)).isoformat(),
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    for key in (
        "feasible",
        "earliest_completion",
        "desired_date_met",
        "deferred_order_ids",
        "total_tardiness_delta_minutes",
        "constraint_reason",
    ):
        assert key in body
