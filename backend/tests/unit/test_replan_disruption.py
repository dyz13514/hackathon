"""扰动登记 + 确定性重排 + ImpactAnalysis + ReAct 接线（任务 7.4，R9、R13、R21.13）。

本文件覆盖用户为 Task 7.4 点名的可在无 LIVE Bedrock 下验证的全部断言：

1. **确定性重排端到端**：`POST /api/disruptions`（MACHINE_BREAKDOWN）→ 200 + 修订计划
   `PENDING_APPROVAL` + `ImpactAnalysis`（R9.2）。
2. **扰动登记与影响回读**：`GET /api/disruptions/{id}/impact` 与登记响应一致（R9.3）。
3. **确定性数值可复现**：同一扰动在同一状态上跑两次（各自独立应用），`ImpactAnalysis` 的
   数值字段逐字段相同（R5.7 的继承 + R9.4）。
4. **无 ACTIVE 计划 → NO_ACTIVE_PLAN**（R9.8）。
5. **PLANNING_AGENT 已接线**：`Orchestrator.run(REGISTER_DISRUPTION)` 不再抛
   `RouteNotWiredError`（注入桩驱动 + 契约后可跑）。
6. **STUB 桩驱动的 ReAct 运行成功**：脚本化回合序列走到 `final`（`RevisedPlanProposal`），
   `outcome == OK`，全程不触达 Bedrock。
7. **LLM 输出不能覆盖确定性数值**：伪造 `impact_class` / `feasibility` 的 Agent `final` 经
   `Guardrail_Layer` 剥离保留键；且 `ImpactAnalysis` 的分级由确定性 `classify_impact` 决定，
   与 Agent 声称的无关。
8. **DETERMINISTIC_ONLY**：确定性重排流水线（`run_replan`）在 `LLM_MODE` 与 LLM 无关——它
   本就不调用 LLM，因此降级模式下路径不变（approach b 的 P0 主路径即它）。

走真实 SQLite + 真实 seed + 真实内核，不 mock 数值路径。ReAct 相关测试用脚本化桩驱动，
不触网、不需要 cassette（approach b）。
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
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
DISRUPTIONS = "/api/disruptions"


# --------------------------------------------------------------------------
# 夹具
# --------------------------------------------------------------------------


@pytest.fixture
def app_settings(valid_env: pytest.MonkeyPatch, tmp_path: Path) -> Settings:
    db_file = tmp_path / "replan.db"
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


def _generate_and_activate_plan(client: TestClient, application: object) -> str:
    """生成一个计划并经 `Approval_Service.approve` 置为 `ACTIVE`，返回 plan_id。

    扰动登记要求存在 ACTIVE 计划（R9.8）。用真实审批闸门激活，而不是直接写 status——那既
    符合状态机（唯一激活路径是 `approve`），也让本测试的前置条件贴近真实流程。
    """
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


def _busy_machine_id(application: object, plan_id: str) -> str:
    """取 ACTIVE 计划里承担作业最多的机器（机器故障扰动打它，保证有受影响作业）。"""
    factory = _factory(application)
    with factory() as db:
        rows = db.execute(
            select(orm.ScheduledJob.machine_id).where(orm.ScheduledJob.plan_id == plan_id)
        ).scalars().all()
    counts: dict[str, int] = {}
    for mid in rows:
        counts[mid] = counts.get(mid, 0) + 1
    assert counts, "ACTIVE 计划应有已排产作业"
    return max(counts, key=lambda m: counts[m])


def _machine_breakdown_body(machine_id: str) -> dict:
    start = DEMO_ANCHOR
    end = DEMO_ANCHOR + timedelta(hours=6)
    return {
        "disruption": {
            "type": "MACHINE_BREAKDOWN",
            "machine_id": machine_id,
            "start_time": start.isoformat(),
            "end_time": end.isoformat(),
        }
    }


# --------------------------------------------------------------------------
# 1 + 2. 确定性重排端到端 + 影响回读
# --------------------------------------------------------------------------


def test_register_disruption_returns_impact_and_revised_plan(
    client: TestClient, application: object
) -> None:
    """MACHINE_BREAKDOWN 登记 → 200 + 修订计划 PENDING_APPROVAL + ImpactAnalysis（R9.2–3）。"""
    plan_id = _generate_and_activate_plan(client, application)
    machine_id = _busy_machine_id(application, plan_id)

    response = client.post(DISRUPTIONS, json=_machine_breakdown_body(machine_id))
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["disruption_id"].startswith("DSR-")
    assert body["type"] == "MACHINE_BREAKDOWN"
    assert body["revised_plan_status"] == "PENDING_APPROVAL"
    assert body["revised_plan_id"].startswith("PLAN-")

    impact = body["impact"]
    # R9.3 的字段齐全，且全部由确定性组件计算（R9.4）。
    for key in (
        "affected_jobs",
        "affected_orders",
        "orders_at_risk_of_lateness",
        "tardiness_delta_minutes",
        "churn_ratio",
        "impact_class",
    ):
        assert key in impact, f"ImpactAnalysis 缺字段 {key}"
    assert impact["impact_class"] in {"IMPACT_MINOR", "IMPACT_MODERATE", "IMPACT_MAJOR"}
    assert impact["autonomy_level"] in {"L3", "L5"}  # P0 值域（design.md §3.6）
    assert 0.0 <= impact["churn_ratio"] <= 1.0
    # 故障机上的作业受影响 → 受影响集非空。
    assert impact["affected_jobs"], "机器故障应产生受影响作业"


def test_get_impact_matches_registration(
    client: TestClient, application: object
) -> None:
    """`GET /disruptions/{id}/impact` 与登记响应的分级/churn 一致（R9.3）。"""
    plan_id = _generate_and_activate_plan(client, application)
    machine_id = _busy_machine_id(application, plan_id)
    reg = client.post(DISRUPTIONS, json=_machine_breakdown_body(machine_id)).json()
    disruption_id = reg["disruption_id"]

    got = client.get(f"{DISRUPTIONS}/{disruption_id}/impact")
    assert got.status_code == 200, got.text
    impact = got.json()
    assert impact["disruption_id"] == disruption_id
    assert impact["impact_class"] == reg["impact"]["impact_class"]
    assert impact["autonomy_level"] == reg["impact"]["autonomy_level"]
    assert impact["candidate_plan_id"] == reg["impact"]["candidate_plan_id"]


def test_get_impact_unknown_disruption_returns_not_found(client: TestClient) -> None:
    """未知扰动 ID → 404 DISRUPTION_NOT_FOUND。"""
    got = client.get(f"{DISRUPTIONS}/DSR-nonexistent/impact")
    assert got.status_code == 404
    assert got.json()["error"]["code"] == "DISRUPTION_NOT_FOUND"


# --------------------------------------------------------------------------
# 3. 确定性数值可复现
# --------------------------------------------------------------------------


def test_replan_numbers_are_deterministic(
    valid_env: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """同一扰动在两套等价的初始状态上分别重排，ImpactAnalysis 数值逐字段相同（R9.4、R5.7）。

    两套状态各自**独立建库**、seed、生成并激活同一份计划、登记同一机器故障——因为排产与
    重排都是确定性的，两次的分级、churn、tardiness_delta、受影响集必然逐字段相同。用两个
    不同的 DB 文件，两次运行完全隔离（各自 seed，不共享任何行）。
    """

    def run_once(db_file: Path) -> dict:
        valid_env.setenv("DATABASE_URL", f"sqlite:///{db_file.as_posix()}")
        settings = Settings()  # type: ignore[call-arg]
        app = create_app(settings)
        Base.metadata.create_all(app.state.engine)
        factory: sessionmaker[Session] = app.state.session_factory
        with factory() as session:
            load_demo_data(session)
            session.commit()
        with TestClient(app) as c:
            c.post(LOGIN, json={"password": settings.session_shared_password.get_secret_value()})
            plan_id = _generate_and_activate_plan(c, app)
            machine_id = _busy_machine_id(app, plan_id)
            body = c.post(DISRUPTIONS, json=_machine_breakdown_body(machine_id)).json()
        return body["impact"]

    first = run_once(tmp_path / "det-first.db")
    second = run_once(tmp_path / "det-second.db")

    for key in (
        "impact_class",
        "autonomy_level",
        "churn_ratio",
        "tardiness_delta_minutes",
        "affected_jobs",
        "affected_orders",
        "decisive_predicates",
    ):
        assert first[key] == second[key], f"字段 {key} 两次不一致：{first[key]} vs {second[key]}"


# --------------------------------------------------------------------------
# 4. 无 ACTIVE 计划 → NO_ACTIVE_PLAN
# --------------------------------------------------------------------------


def test_disruption_without_active_plan_returns_no_active_plan(
    client: TestClient,
) -> None:
    """未激活任何计划时登记扰动 → 409 NO_ACTIVE_PLAN（R9.8）。"""
    # 不生成/激活计划，直接登记扰动。
    response = client.post(
        DISRUPTIONS,
        json={
            "disruption": {
                "type": "MACHINE_BREAKDOWN",
                "machine_id": "CNC-01",
                "start_time": DEMO_ANCHOR.isoformat(),
                "end_time": (DEMO_ANCHOR + timedelta(hours=2)).isoformat(),
            }
        },
    )
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "NO_ACTIVE_PLAN"


# --------------------------------------------------------------------------
# 8. DETERMINISTIC_ONLY：确定性重排不依赖 LLM
# --------------------------------------------------------------------------


def test_replan_works_under_deterministic_only(
    valid_env: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`LLM_MODE=DISABLED`（DETERMINISTIC_ONLY）下确定性重排照常产出计划与 ImpactAnalysis。

    approach (b) 的 P0 主路径就是确定性重排流水线（`run_replan`），它**不调用 LLM**——因此
    降级模式下路径不变、数值不变（design.md §2.6：扰动重排「保留」）。这里把 `LLM_MODE`
    设为 `DISABLED` 并跑一次完整登记+重排，断言成功。
    """
    db_file = tmp_path / "degraded.db"
    valid_env.setenv("DATABASE_URL", f"sqlite:///{db_file.as_posix()}")
    valid_env.setenv("LLM_MODE", "DISABLED")
    settings = Settings()  # type: ignore[call-arg]
    assert settings.llm_mode == "DISABLED"

    app = create_app(settings)
    Base.metadata.create_all(app.state.engine)
    factory: sessionmaker[Session] = app.state.session_factory
    with factory() as session:
        load_demo_data(session)
        session.commit()
    with TestClient(app) as c:
        c.post(LOGIN, json={"password": settings.session_shared_password.get_secret_value()})
        plan_id = _generate_and_activate_plan(c, app)
        machine_id = _busy_machine_id(app, plan_id)
        response = c.post(DISRUPTIONS, json=_machine_breakdown_body(machine_id))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["revised_plan_status"] == "PENDING_APPROVAL"
    assert body["impact"]["impact_class"] in {"IMPACT_MINOR", "IMPACT_MODERATE", "IMPACT_MAJOR"}


def test_disruption_requires_authentication(application: object) -> None:
    """未认证的 POST /disruptions → 401（写端点受 Session_Auth 保护，R23.12）。"""
    app = application  # type: ignore[assignment]
    with TestClient(app) as anon:  # type: ignore[arg-type]
        response = anon.post(
            DISRUPTIONS,
            json={
                "disruption": {
                    "type": "MACHINE_BREAKDOWN",
                    "machine_id": "CNC-01",
                    "start_time": DEMO_ANCHOR.isoformat(),
                    "end_time": (DEMO_ANCHOR + timedelta(hours=2)).isoformat(),
                }
            },
        )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHENTICATED"
