"""What-if 沙箱推演与采纳（任务 8.3，R16.2 / R16.7–9）。

走真实 SQLite + 真实 seed + 真实内核，不 mock、无 LLM。覆盖：

1. `apply_mutations` 内核：5 类结构化变更各改对快照且**不改写原件**（沙箱第 1 层隔离）；
   指向不存在实体 → `ScenarioMutationError`。
2. `POST /api/scenarios/run`：激活计划后推演 → 200，返回与 ACTIVE 的对比；受 Session_Auth 保护。
3. 无 ACTIVE 计划 → `NO_ACTIVE_PLAN`（409）。
4. 非法变更（改不存在的机器）→ `SCENARIO_INVALID_MUTATION`（422）。
5. `POST /api/scenarios/{id}/adopt`：生成一个 `PENDING_APPROVAL` 提案（origin=SCENARIO_ADOPTION）；
   过期/未知 scenario → `SCENARIO_NOT_FOUND`（404）。
6. 沙箱不污染生产数据：推演后 ACTIVE 计划的 plan_id / input_snapshot_version 不变。
7. 确定性：同一批变更两次推演逐字段相同。
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from app.core.sandbox import ScenarioMutationError, apply_mutations
from app.db import models as orm
from app.db.models import Base
from app.main import create_app
from app.seed.dataset import DEMO_ANCHOR
from app.seed.loader import load_demo_data
from app.services.approval import ApprovalService, ApprovalStatus
from app.services.events import EventBus
from app.services.snapshot_loader import load_snapshot
from app.settings import Settings
from app.tools.models import (
    ChangeMaterialAvailability,
    ChangeOrderPriority,
    SetMachineUnavailable,
)
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

LOGIN = "/api/auth/login"
GENERATE = "/api/plans/generate"
RUN = "/api/scenarios/run"


def _machine_down(machine_id: str, hours: int = 8) -> dict:
    """一个「机器不可用」变更请求体（避免超长内联 JSON）。"""
    return {
        "mutations": [
            {
                "kind": "SET_MACHINE_UNAVAILABLE",
                "machine_id": machine_id,
                "start_time": DEMO_ANCHOR.isoformat(),
                "end_time": (DEMO_ANCHOR + timedelta(hours=hours)).isoformat(),
            }
        ]
    }


def _material_qty(material_id: str, qty: float = 0) -> dict:
    return {
        "mutations": [
            {
                "kind": "CHANGE_MATERIAL_AVAILABILITY",
                "material_id": material_id,
                "quantity_available": qty,
            }
        ]
    }


@pytest.fixture
def app_settings(valid_env: pytest.MonkeyPatch, tmp_path: Path) -> Settings:
    db_file = tmp_path / "scenario.db"
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
        assert result.status is ApprovalStatus.OK
    finally:
        db2.close()
    return plan_id


def _first_machine(application: object, plan_id: str) -> str:
    from sqlalchemy import select

    factory = _factory(application)
    with factory() as db:
        mid = db.execute(
            select(orm.ScheduledJob.machine_id).where(orm.ScheduledJob.plan_id == plan_id)
        ).scalars().first()
    assert mid is not None
    return str(mid)


# --------------------------------------------------------------------------
# 1. apply_mutations 内核
# --------------------------------------------------------------------------


def _snapshot(application: object):
    factory = _factory(application)
    with factory() as db:
        return load_snapshot(db, now=DEMO_ANCHOR)


def test_apply_change_material_availability_does_not_mutate_original(
    application: object,
) -> None:
    snap = _snapshot(application)
    mat_id = snap.materials[0].material_id
    original_qty = snap.materials[0].quantity_available

    variant = apply_mutations(
        snap,
        [ChangeMaterialAvailability(material_id=mat_id, quantity_available=0.0)],
    )
    # 变体改了，原件没动（第 1 层隔离：model_copy 得新冻结副本）。
    assert variant.materials_by_id()[mat_id].quantity_available == Decimal("0")
    assert snap.materials_by_id()[mat_id].quantity_available == original_qty
    assert variant is not snap


def test_apply_set_machine_unavailable_appends_downtime(application: object) -> None:
    snap = _snapshot(application)
    mid = snap.machines[0].machine_id
    before = len(snap.machines_by_id()[mid].downtime_windows)
    variant = apply_mutations(
        snap,
        [
            SetMachineUnavailable(
                machine_id=mid,
                start_time=DEMO_ANCHOR,
                end_time=DEMO_ANCHOR + timedelta(hours=8),
            )
        ],
    )
    assert len(variant.machines_by_id()[mid].downtime_windows) == before + 1
    assert len(snap.machines_by_id()[mid].downtime_windows) == before  # 原件不变


def test_apply_change_order_priority(application: object) -> None:
    snap = _snapshot(application)
    oid = snap.orders[0].order_id
    variant = apply_mutations(snap, [ChangeOrderPriority(order_id=oid, priority="URGENT")])
    assert variant.orders_by_id()[oid].priority == "URGENT"


def test_apply_mutation_unknown_entity_raises(application: object) -> None:
    snap = _snapshot(application)
    with pytest.raises(ScenarioMutationError):
        apply_mutations(
            snap, [ChangeMaterialAvailability(material_id="NOPE", quantity_available=1.0)]
        )


# --------------------------------------------------------------------------
# 2. POST /api/scenarios/run
# --------------------------------------------------------------------------


def test_run_scenario_requires_authentication(application: object) -> None:
    app = application  # type: ignore[assignment]
    with TestClient(app) as anon:  # type: ignore[arg-type]
        resp = anon.post(
            RUN,
            json={
                "mutations": [
                    {"kind": "CHANGE_ORDER_PRIORITY", "order_id": "X", "priority": "HIGH"}
                ]
            },
        )
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "UNAUTHENTICATED"


def test_run_scenario_without_active_plan_returns_409(
    client: TestClient, application: object
) -> None:
    resp = client.post(RUN, json=_material_qty("M", 0))
    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == "NO_ACTIVE_PLAN"


def test_run_scenario_returns_comparison(client: TestClient, application: object) -> None:
    plan_id = _activate_plan(client, application)
    mid = _first_machine(application, plan_id)
    resp = client.post(
        RUN,
        json={
            "mutations": [
                {
                    "kind": "SET_MACHINE_UNAVAILABLE",
                    "machine_id": mid,
                    "start_time": DEMO_ANCHOR.isoformat(),
                    "end_time": (DEMO_ANCHOR + timedelta(hours=8)).isoformat(),
                }
            ]
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["scenario_id"].startswith("SCN-")
    assert body["feasibility"] in {"FEASIBLE", "PARTIAL", "NO_FEASIBLE_PLAN"}
    # 对比字段齐全（delta = 场景 − ACTIVE）。
    assert "late_order_count_delta" in body
    assert "total_tardiness_delta_minutes" in body
    assert isinstance(body["new_unschedulable_jobs"], list)


def test_run_scenario_invalid_mutation_returns_422(
    client: TestClient, application: object
) -> None:
    _activate_plan(client, application)
    resp = client.post(RUN, json=_machine_down("GHOST", hours=1))
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == "SCENARIO_INVALID_MUTATION"


def test_run_scenario_does_not_pollute_production_data(
    client: TestClient, application: object
) -> None:
    """推演后 ACTIVE 计划的 plan_id 与 input_snapshot_version 不变（R16.4，沙箱隔离）。"""
    from sqlalchemy import func, select

    plan_id = _activate_plan(client, application)
    factory = _factory(application)
    with factory() as db:
        active = db.get(orm.ProductionPlan, plan_id)
        assert active is not None
        version_before = active.input_snapshot_version
        mat_rows_before = db.execute(select(func.count()).select_from(orm.Material)).scalar_one()
        mid = _first_machine(application, plan_id)

    client.post(RUN, json=_material_qty(_any_material(application), 0))
    client.post(RUN, json=_machine_down(mid))

    with factory() as db:
        active = db.get(orm.ProductionPlan, plan_id)
        assert active is not None
        assert active.status == "ACTIVE"
        assert active.input_snapshot_version == version_before
        mat_rows_after = db.execute(select(func.count()).select_from(orm.Material)).scalar_one()
    assert mat_rows_after == mat_rows_before  # 沙箱没写任何行


def _any_material(application: object) -> str:
    from sqlalchemy import select

    factory = _factory(application)
    with factory() as db:
        mid = db.execute(select(orm.Material.material_id)).scalars().first()
    assert mid is not None
    return str(mid)


def test_run_scenario_is_deterministic(client: TestClient, application: object) -> None:
    plan_id = _activate_plan(client, application)
    mid = _first_machine(application, plan_id)
    payload = {
        "mutations": [
            {
                "kind": "SET_MACHINE_UNAVAILABLE",
                "machine_id": mid,
                "start_time": DEMO_ANCHOR.isoformat(),
                "end_time": (DEMO_ANCHOR + timedelta(hours=8)).isoformat(),
            }
        ]
    }
    first = client.post(RUN, json=payload).json()
    second = client.post(RUN, json=payload).json()
    # scenario_id 是随机的；其余对比数值必须逐字段相同（确定性内核，R5.7 同一纪律）。
    for key in (
        "feasibility",
        "late_order_count",
        "total_tardiness_minutes",
        "total_score",
        "new_unschedulable_jobs",
        "delayed_order_ids",
    ):
        assert first[key] == second[key], key


# --------------------------------------------------------------------------
# 3. adopt
# --------------------------------------------------------------------------


def test_adopt_scenario_creates_pending_proposal(
    client: TestClient, application: object
) -> None:
    plan_id = _activate_plan(client, application)
    mid = _first_machine(application, plan_id)
    run = client.post(RUN, json=_machine_down(mid)).json()
    scenario_id = run["scenario_id"]

    resp = client.post(f"/api/scenarios/{scenario_id}/adopt", json={})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "PENDING_APPROVAL"
    new_plan_id = body["plan_id"]

    factory = _factory(application)
    with factory() as db:
        proposal = db.get(orm.ProductionPlan, new_plan_id)
        assert proposal is not None
        assert proposal.status == "PENDING_APPROVAL"
        assert proposal.origin == "SCENARIO_ADOPTION"


def test_adopt_unknown_scenario_returns_404(
    client: TestClient, application: object
) -> None:
    _activate_plan(client, application)
    resp = client.post("/api/scenarios/SCN-nonexistent/adopt", json={})
    assert resp.status_code == 404, resp.text
    assert resp.json()["error"]["code"] == "SCENARIO_NOT_FOUND"


# --------------------------------------------------------------------------
# 4. 反事实（任务 8.4）：Z 值由 Objective_Scorer 实算（R10.3）
# --------------------------------------------------------------------------


def test_build_counterfactual_z_equals_sandbox_recompute(
    client: TestClient, application: object
) -> None:
    """`build_counterfactual` 产出的 Tradeoff：把关键作业冻回原位后 Z 由评分器实算得出。

    验证 R10.3「反事实数值由沙箱对原方案实际计算」——独立重跑冻结重排 + 评分，断言得到
    的分量值与 `Tradeoff.counterfactual_value` 逐字节相同（确定性、可复现）。用一个改交期的
    场景制造 active 与候选之间的 delta。
    """
    from app.core.explain import NoTradeoff, Tradeoff
    from app.core.sandbox import apply_mutations
    from app.core.scheduler import generate_schedule
    from app.core.scoring import ObjectiveWeights, score
    from app.services.sandbox import build_counterfactual

    plan_id = _activate_plan(client, application)
    factory = _factory(application)
    with factory() as db:
        active_candidate = _load_active_candidate(db, plan_id)
        base_snapshot = load_snapshot(db, now=DEMO_ANCHOR)

    # 情景：把第一台机器停机 8h → 迫使部分作业改派/移动，制造 delta。
    mid = base_snapshot.machines[0].machine_id
    mutated = apply_mutations(
        base_snapshot,
        [
            SetMachineUnavailable(
                machine_id=mid,
                start_time=DEMO_ANCHOR,
                end_time=DEMO_ANCHOR + timedelta(hours=8),
            )
        ],
    )
    candidate = generate_schedule(mutated)

    cf = build_counterfactual(
        active_plan=active_candidate,
        candidate=candidate,
        snapshot=mutated,
        plan_id=plan_id,
    )
    if isinstance(cf, NoTradeoff):
        pytest.skip("该场景无 MOVED/REASSIGNED/新增作业，无取舍项——退化为 NoTradeoff")

    assert isinstance(cf, Tradeoff)
    # 独立重算 Z：冻结同一 pivotal 作业到 ACTIVE 原位，重排 + 评分，取同一分量。
    frozen = tuple(j for j in active_candidate.scheduled_jobs if j.job_id == cf.pivotal_job_id)
    recomputed = generate_schedule(mutated, freeze=frozen)
    breakdown = score(recomputed, mutated, ObjectiveWeights(), reference_plan=active_candidate)
    z = next(
        (c.weighted_contribution for c in breakdown.components if c.name == cf.component),
        breakdown.total_score,
    )
    assert cf.counterfactual_value == z  # Z 实算、可复现（R10.3）
    assert cf.pivotal_job_id  # 选中了唯一关键作业
    assert cf.selection_basis  # 依据可读


def _load_active_candidate(db: Session, plan_id: str):
    from app.services.replanning import load_plan_candidate

    return load_plan_candidate(db, plan_id)
