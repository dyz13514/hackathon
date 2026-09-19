"""瓶颈与产能洞察（任务 13.5，P1-J ⑤，R15.1–R15.4）。

守 tasks.md 13.5 的可验收点，逐条对应：

- **每机器利用率 / 作业数 / 订单价值占比**（R15.1）。
- **关键机器标识**：无相同 capabilities 替代机器（R15.3）。
- **技能缺口按 required_worker_skill 聚合**（R15.4）。
- **+20% 工时的拖期变化量由沙箱实算**（R15.2）：不是静态估算——它等于
  `run_capacity_sandbox(+20%)` 的确定性结果。
- **只读**：计算前后 orders / machines / plans / scheduled_jobs 行数不变（沙箱不写生产数据）。
- **API**：`GET /api/insights/bottlenecks`；无 ACTIVE 计划 → 409 NO_ACTIVE_PLAN。

走真实 create_app + 真实 SQLite + 真实内核，不 mock。零 LLM。
"""

from __future__ import annotations

from collections.abc import Iterator
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
from app.services.insights import (
    CAPACITY_UPLIFT_MULTIPLIER,
    bottleneck_insights,
)
from app.services.sandbox import SandboxPurpose, run_capacity_sandbox
from app.settings import Settings

LOGIN = "/api/auth/login"
GENERATE = "/api/plans/generate"
BOTTLENECKS = "/api/insights/bottlenecks"


@pytest.fixture
def app_settings(valid_env: pytest.MonkeyPatch, tmp_path: Path) -> Settings:
    db_file = tmp_path / "insights.db"
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


def _row_counts(application: object) -> dict[str, int]:
    factory = _factory(application)
    with factory() as db:
        return {
            "orders": int(db.execute(select(func.count()).select_from(orm.Order)).scalar_one()),
            "machines": int(
                db.execute(select(func.count()).select_from(orm.Machine)).scalar_one()
            ),
            "plans": int(
                db.execute(select(func.count()).select_from(orm.ProductionPlan)).scalar_one()
            ),
            "scheduled_jobs": int(
                db.execute(select(func.count()).select_from(orm.ScheduledJob)).scalar_one()
            ),
        }


# --------------------------------------------------------------------------
# 服务层
# --------------------------------------------------------------------------


def test_insights_per_machine_metrics(client: TestClient, application: object) -> None:
    """每台承担作业的机器都有利用率∈[0,1]、作业数≥1、订单价值占比∈[0,1]（R15.1）。"""
    _activate(client, application)
    factory = _factory(application)
    with factory() as db:
        result = bottleneck_insights(db, now=DEMO_ANCHOR)
    assert result.machines, "ACTIVE 计划应至少有一台机器承担作业"
    for m in result.machines:
        assert 0.0 <= m.utilisation <= 1.0
        assert m.job_count >= 1
        assert 0.0 <= m.order_value_share <= 1.0
        assert m.busy_minutes <= m.available_minutes
    # 订单价值占比之和 ≈ 1（每个已排产作业的订单价值都归到某台机器）。
    total_share = sum(m.order_value_share for m in result.machines)
    assert abs(total_share - 1.0) < 0.01


def test_insights_flags_critical_machine(client: TestClient, application: object) -> None:
    """无相同 capabilities 替代机器的承担作业机器被标为关键（R15.3）。"""
    _activate(client, application)
    factory = _factory(application)
    with factory() as db:
        result = bottleneck_insights(db, now=DEMO_ANCHOR)
        machines_by_id = {m.machine_id: m for m in db.execute(select(orm.Machine)).scalars()}

    for insight in result.machines:
        # 独立复算：是否存在能力集合相同的另一台机器。
        target_caps = frozenset(machines_by_id[insight.machine_id].capabilities)
        has_substitute = any(
            mid != insight.machine_id and frozenset(m.capabilities) == target_caps
            for mid, m in machines_by_id.items()
        )
        assert insight.is_critical == (not has_substitute)


def test_insights_skill_gaps_aggregate_by_skill(client: TestClient, application: object) -> None:
    """技能缺口按 required_worker_skill 聚合，gap = required − available（R15.4）。"""
    _activate(client, application)
    factory = _factory(application)
    with factory() as db:
        result = bottleneck_insights(db, now=DEMO_ANCHOR)
    assert result.skill_gaps, "应有按技能聚合的缺口条目"
    for g in result.skill_gaps:
        assert g.required_minutes > 0  # 只报告计划实际需要的技能
        assert g.gap_minutes == g.required_minutes - g.available_minutes


def test_insights_plus20_delta_matches_real_sandbox(
    client: TestClient, application: object
) -> None:
    """+20% 工时的拖期变化量等于 run_capacity_sandbox 的确定性实算值（R15.2，非静态估算）。"""
    _activate(client, application)
    factory = _factory(application)
    with factory() as db:
        result = bottleneck_insights(db, now=DEMO_ANCHOR)
    # 对每台机器，独立再跑一次 run_capacity_sandbox，断言洞察里的值与之逐一相等。
    for insight in result.machines:
        with factory() as db:
            recomputed = run_capacity_sandbox(
                db,
                machine_id=insight.machine_id,
                hours_multiplier=CAPACITY_UPLIFT_MULTIPLIER,
                now=DEMO_ANCHOR,
                purpose=SandboxPurpose.BOTTLENECK,
            )
        assert (
            insight.tardiness_delta_if_plus_20pct
            == recomputed.total_tardiness_delta_minutes
        )


def test_insights_is_read_only(client: TestClient, application: object) -> None:
    """计算洞察（含 +20% 沙箱实算）不修改任何生产数据或计划（沙箱隔离）。"""
    _activate(client, application)
    before = _row_counts(application)
    factory = _factory(application)
    with factory() as db:
        bottleneck_insights(db, now=DEMO_ANCHOR)
    after = _row_counts(application)
    assert after == before, "瓶颈洞察必须只读——不得改动生产数据或计划"


# --------------------------------------------------------------------------
# API
# --------------------------------------------------------------------------


def test_api_bottlenecks_without_active_plan_returns_409(client: TestClient) -> None:
    resp = client.get(BOTTLENECKS)
    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == "NO_ACTIVE_PLAN"


def test_api_bottlenecks_returns_full_shape(client: TestClient, application: object) -> None:
    _activate(client, application)
    resp = client.get(BOTTLENECKS)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["active_plan_id"]
    assert body["machines"]
    m = body["machines"][0]
    for key in (
        "machine_id",
        "utilisation",
        "job_count",
        "order_value_share",
        "is_critical",
        "tardiness_delta_if_plus_20pct",
    ):
        assert key in m
    assert "skill_gaps" in body
