"""方案对比视图与决策证据（任务 7.5，R10.1、R10.2）。

`GET /api/plans/{a}/compare/{b}` 是**面向 UI 的明细端点**：逐作业标注
`ADDED`/`REMOVED`/`MOVED`/`REASSIGNED`/`UNCHANGED`（R10.1），并为每个 `MOVED` / `REASSIGNED`
作业给出 `decision_evidence`（R10.2，含 trigger / constraint / resources）。它与句柄式
`compare_plans` 工具（只给聚合计数）区分。全部由确定性组件计算，不触发 LLM。反事实（R10.3）
属任务 8.4，本端点不含。

覆盖：
1. 同一计划与自身对比 → 全部 `UNCHANGED`，无 decision_evidence，churn=0。
2. 计划 A（ACTIVE）与重排后的修订计划 B 对比 → 出现 MOVED/REASSIGNED，且每个这样的作业恰有
   一条 decision_evidence（R10.2）；ADDED/REMOVED/UNCHANGED 不产出证据。
3. 逐作业变更集与聚合计数一致；churn_ratio ∈ [0,1]。
4. 计划不存在 → 404 PLAN_NOT_FOUND。

也直接单元测试 `build_decision_evidence`（core 纯函数）：MOVED → 时间调整证据；REASSIGNED →
资源改派证据，resources 记录 machine/worker 的变化；ADDED/REMOVED/UNCHANGED 无证据。

走真实 SQLite + 真实 seed + 真实内核，不 mock。
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.core.delta import compute_plan_delta
from app.core.explain import build_decision_evidence
from app.core.scheduler import PlanCandidate, ScheduledJob
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
    db_file = tmp_path / "compare.db"
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


def _busy_machine_id(application: object, plan_id: str) -> str:
    factory = _factory(application)
    with factory() as db:
        rows = db.execute(
            select(orm.ScheduledJob.machine_id).where(orm.ScheduledJob.plan_id == plan_id)
        ).scalars().all()
    counts: dict[str, int] = {}
    for mid in rows:
        counts[mid] = counts.get(mid, 0) + 1
    return max(counts, key=lambda m: counts[m])


def _register_breakdown(client: TestClient, application: object, plan_id: str) -> str:
    """登记机器故障，返回重排后的修订计划 id（与 ACTIVE 计划有 MOVED/REASSIGNED 差异）。"""
    machine_id = _busy_machine_id(application, plan_id)
    body = {
        "disruption": {
            "type": "MACHINE_BREAKDOWN",
            "machine_id": machine_id,
            "start_time": DEMO_ANCHOR.isoformat(),
            "end_time": (DEMO_ANCHOR + timedelta(hours=6)).isoformat(),
        }
    }
    resp = client.post(DISRUPTIONS, json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()["revised_plan_id"]


# --------------------------------------------------------------------------
# 端点：同一计划与自身对比 → 全 UNCHANGED
# --------------------------------------------------------------------------


def test_compare_plan_with_itself_all_unchanged(
    client: TestClient, application: object
) -> None:
    """A vs A：全部作业 UNCHANGED，churn=0，无 decision_evidence。"""
    plan_id = _activate_plan(client, application)
    resp = client.get(f"/api/plans/{plan_id}/compare/{plan_id}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["plan_id_a"] == plan_id
    assert body["churn_ratio"] == 0.0
    assert body["added_count"] == 0
    assert body["removed_count"] == 0
    assert body["moved_count"] == 0
    assert body["reassigned_count"] == 0
    assert body["unchanged_count"] == len(body["changes"])
    assert all(c["change"] == "UNCHANGED" for c in body["changes"])
    assert body["decision_evidence"] == []


# --------------------------------------------------------------------------
# 端点：ACTIVE vs 重排修订计划 → MOVED/REASSIGNED + decision_evidence
# --------------------------------------------------------------------------


def test_compare_active_vs_revised_has_changes_and_evidence(
    client: TestClient, application: object
) -> None:
    """A（ACTIVE）vs B（重排）：出现变更；每个 MOVED/REASSIGNED 作业恰有一条 decision_evidence。"""
    active_id = _activate_plan(client, application)
    revised_id = _register_breakdown(client, application, active_id)

    resp = client.get(f"/api/plans/{active_id}/compare/{revised_id}")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    # 机器故障必然产生资源改派或时间调整——变更集非空。
    total_changed = (
        body["added_count"]
        + body["removed_count"]
        + body["moved_count"]
        + body["reassigned_count"]
    )
    assert total_changed > 0, "机器故障重排应产生逐作业变更"
    assert 0.0 <= body["churn_ratio"] <= 1.0

    # 逐作业变更集与聚合计数一致（R10.1）。
    by_change: dict[str, int] = {}
    for c in body["changes"]:
        by_change[c["change"]] = by_change.get(c["change"], 0) + 1
    assert by_change.get("ADDED", 0) == body["added_count"]
    assert by_change.get("REMOVED", 0) == body["removed_count"]
    assert by_change.get("MOVED", 0) == body["moved_count"]
    assert by_change.get("REASSIGNED", 0) == body["reassigned_count"]
    assert by_change.get("UNCHANGED", 0) == body["unchanged_count"]

    # R10.2：每个 MOVED / REASSIGNED 作业恰有一条 decision_evidence，且只有它们有。
    moved_reassigned_ids = {
        c["job_id"] for c in body["changes"] if c["change"] in ("MOVED", "REASSIGNED")
    }
    evidence_ids = {ev["job_id"] for ev in body["decision_evidence"]}
    assert evidence_ids == moved_reassigned_ids
    assert len(body["decision_evidence"]) == len(moved_reassigned_ids)
    # 每条证据三项齐全（trigger / constraint / resources）。
    for ev in body["decision_evidence"]:
        assert ev["trigger"]
        assert ev["constraint"]
        assert "resources" in ev


def test_compare_unknown_plan_returns_not_found(
    client: TestClient, application: object
) -> None:
    """任一计划不存在 → 404 PLAN_NOT_FOUND。"""
    active_id = _activate_plan(client, application)
    resp = client.get(f"/api/plans/{active_id}/compare/PLAN-nonexistent")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "PLAN_NOT_FOUND"


# --------------------------------------------------------------------------
# core 纯函数：build_decision_evidence
# --------------------------------------------------------------------------

_T0 = DEMO_ANCHOR
_T1 = DEMO_ANCHOR + timedelta(hours=1)
_T2 = DEMO_ANCHOR + timedelta(hours=2)


def _sj(
    job_id: str,
    *,
    machine: str,
    worker: str,
    start: datetime,
    end: datetime,
) -> ScheduledJob:
    return ScheduledJob(
        job_id=job_id,
        order_id=job_id.rsplit("-OP", 1)[0],
        product_id="P1",
        machine_id=machine,
        worker_id=worker,
        start_time=start,
        end_time=end,
        setup_minutes=0,
        changeover_minutes=0,
    )


def _plan(*jobs: ScheduledJob) -> PlanCandidate:
    return PlanCandidate(
        scheduled_jobs=tuple(jobs), unschedulable_jobs=(), feasibility="FEASIBLE"
    )


def test_build_decision_evidence_reassigned_records_resource_change() -> None:
    """REASSIGNED（换机器）→ 一条「资源改派」证据，resources 记录 machine 变化。"""
    a = _plan(_sj("ORD-1-OP1", machine="CNC-01", worker="W1", start=_T0, end=_T1))
    b = _plan(_sj("ORD-1-OP1", machine="CNC-02", worker="W1", start=_T0, end=_T1))
    delta = compute_plan_delta(a, b)
    assert delta.reassigned == ("ORD-1-OP1",)
    evidence = build_decision_evidence(delta, a, b)
    assert len(evidence) == 1
    ev = evidence[0]
    assert ev.job_id == "ORD-1-OP1"
    assert ev.trigger == "资源改派"
    assert "machine:CNC-01→CNC-02" in ev.resources


def test_build_decision_evidence_moved_records_time_adjustment() -> None:
    """MOVED（同机器同工人、只换时间）→ 一条「开始时间调整」证据。"""
    a = _plan(_sj("ORD-1-OP1", machine="CNC-01", worker="W1", start=_T0, end=_T1))
    b = _plan(_sj("ORD-1-OP1", machine="CNC-01", worker="W1", start=_T1, end=_T2))
    delta = compute_plan_delta(a, b)
    assert delta.moved == ("ORD-1-OP1",)
    evidence = build_decision_evidence(delta, a, b)
    assert len(evidence) == 1
    assert evidence[0].trigger == "开始时间调整"
    assert "machine:CNC-01" in evidence[0].resources


def test_build_decision_evidence_none_for_added_removed_unchanged() -> None:
    """ADDED / REMOVED / UNCHANGED 不产出 decision_evidence（R10.2 只覆盖 MOVED/REASSIGNED）。"""
    a = _plan(
        _sj("ORD-1-OP1", machine="CNC-01", worker="W1", start=_T0, end=_T1),
        _sj("ORD-2-OP1", machine="CNC-01", worker="W1", start=_T1, end=_T2),
    )
    b = _plan(
        # ORD-1-OP1 unchanged; ORD-2-OP1 removed; ORD-3-OP1 added.
        _sj("ORD-1-OP1", machine="CNC-01", worker="W1", start=_T0, end=_T1),
        _sj("ORD-3-OP1", machine="CNC-02", worker="W2", start=_T1, end=_T2),
    )
    delta = compute_plan_delta(a, b)
    assert delta.added == ("ORD-3-OP1",)
    assert delta.removed == ("ORD-2-OP1",)
    assert "ORD-1-OP1" in delta.unchanged
    assert build_decision_evidence(delta, a, b) == ()


def test_build_decision_evidence_is_deterministic() -> None:
    """同输入两次调用产出逐字段相同（纯函数，R5.7 同一纪律）。"""
    a = _plan(
        _sj("ORD-1-OP1", machine="CNC-01", worker="W1", start=_T0, end=_T1),
        _sj("ORD-2-OP1", machine="CNC-01", worker="W1", start=_T1, end=_T2),
    )
    b = _plan(
        _sj("ORD-1-OP1", machine="CNC-02", worker="W1", start=_T0, end=_T1),
        _sj("ORD-2-OP1", machine="CNC-01", worker="W1", start=_T2, end=_T2 + timedelta(hours=1)),
    )
    delta = compute_plan_delta(a, b)
    first = build_decision_evidence(delta, a, b)
    second = build_decision_evidence(delta, a, b)
    assert first == second
    # 排序稳定：按 job_id 升序。
    assert [e.job_id for e in first] == sorted(e.job_id for e in first)
