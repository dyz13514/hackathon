"""自主等级执行路径 + 特性开关 + 价值台账（任务 7.6，R13.3/R13.4/R13.12/R13.13）。

Task 7.6 把已就位的 `Autonomy_Policy_Engine`（任务 7.3）与 `impact_assessments` 写入路径
（任务 7.4）接到三个可观测面上，本文件逐条守它们：

1. **特性开关端点**：`GET /api/settings/flags` 默认 `auto_apply_minor_enabled=false`（R13.8）；
   `PATCH` 翻成 `true` 后回读一致，且留一条审计（R13.8 的开关变更可追溯）。未认证的 PATCH
   → 401（写端点受 Session_Auth 保护）。空 PATCH 幂等不写。
2. **执行路径外露**：登记一次扰动后，`ImpactAnalysisOut.execution_path ∈ {PROPOSED, ESCALATED}`，
   且与 `autonomy_level` 一致（L5→ESCALATED，否则 PROPOSED）——P0 值域（design.md §3.6）。
   持久化的 `impact_assessments.execution_path` 与响应一致（R13.3/R13.4/R13.12）。
3. **影响分级审计**：重排写一条 `IMPACT_CLASSIFICATION` 审计，载荷含 `decisive_predicates` 与
   `execution_path`（R13.12）。
4. **价值台账 K-14**：`GET /api/value-ledger` 的 `auto_handled_count` / `escalated_count` 与
   `impact_assessments` 的执行路径分布一致；`auto_handled_ratio ∈ [0,1]`；逐条 `decisions`
   带决定性判据（R13.13、R13.12）。台账早于任何裁决时为诚实空态（计数全 0）。

走真实 SQLite + 真实 seed + 真实内核，不 mock。不触达 LLM（确定性重排是 approach b 主路径）。
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
FLAGS = "/api/settings/flags"
VALUE_LEDGER = "/api/value-ledger"


# --------------------------------------------------------------------------
# 夹具（与 test_replan_disruption.py 同口径）
# --------------------------------------------------------------------------


@pytest.fixture
def app_settings(valid_env: pytest.MonkeyPatch, tmp_path: Path) -> Settings:
    db_file = tmp_path / "autonomy.db"
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
    return {
        "disruption": {
            "type": "MACHINE_BREAKDOWN",
            "machine_id": machine_id,
            "start_time": DEMO_ANCHOR.isoformat(),
            "end_time": (DEMO_ANCHOR + timedelta(hours=6)).isoformat(),
        }
    }


def _register_breakdown(client: TestClient, application: object) -> dict:
    """生成+激活计划，登记一次机器故障，返回登记响应体（含 impact）。"""
    plan_id = _generate_and_activate_plan(client, application)
    machine_id = _busy_machine_id(application, plan_id)
    resp = client.post(DISRUPTIONS, json=_machine_breakdown_body(machine_id))
    assert resp.status_code == 200, resp.text
    return resp.json()


# --------------------------------------------------------------------------
# 1. 特性开关端点（R13.8）
# --------------------------------------------------------------------------


def test_flags_default_false(client: TestClient) -> None:
    """`GET /settings/flags` 缺行即 P0 默认：auto_apply_minor_enabled=false（R13.8）。"""
    resp = client.get(FLAGS)
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"auto_apply_minor_enabled": False}


def test_flags_patch_toggles_and_persists(client: TestClient, application: object) -> None:
    """PATCH 翻成 true → 响应与再次 GET 都是 true；写一条审计（R13.8）。"""
    patched = client.patch(FLAGS, json={"auto_apply_minor_enabled": True})
    assert patched.status_code == 200, patched.text
    assert patched.json() == {"auto_apply_minor_enabled": True}

    # 回读一致（持久化到 settings 表）。
    assert client.get(FLAGS).json() == {"auto_apply_minor_enabled": True}

    # 审计留痕：WEIGHT_CHANGE / FLAG_CHANGE，载荷含 flag 名与新值。
    factory = _factory(application)
    with factory() as db:
        rows = db.execute(
            select(orm.AuditLog).where(orm.AuditLog.event_type == "FLAG_CHANGE")
        ).scalars().all()
    assert len(rows) == 1
    assert rows[0].event_category == "WEIGHT_CHANGE"
    assert rows[0].payload["flag"] == "auto_apply_minor_enabled"
    assert rows[0].payload["enabled"] is True


def test_flags_empty_patch_is_idempotent_no_audit(
    client: TestClient, application: object
) -> None:
    """空 PATCH（无任何开关）不写、不审计，回读仍是默认值。"""
    resp = client.patch(FLAGS, json={})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"auto_apply_minor_enabled": False}
    factory = _factory(application)
    with factory() as db:
        rows = db.execute(
            select(orm.AuditLog).where(orm.AuditLog.event_type == "FLAG_CHANGE")
        ).scalars().all()
    assert rows == []


def test_flags_patch_requires_authentication(application: object) -> None:
    """未认证的 PATCH /settings/flags → 401（写端点受 Session_Auth 保护）。"""
    app = application  # type: ignore[assignment]
    with TestClient(app) as anon:  # type: ignore[arg-type]
        resp = anon.patch(FLAGS, json={"auto_apply_minor_enabled": True})
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "UNAUTHENTICATED"


def test_flags_patch_rejects_unknown_key(client: TestClient) -> None:
    """未知开关键名 → 422（extra=forbid 使拼错不被静默忽略）。"""
    resp = client.patch(FLAGS, json={"nonexistent_flag": True})
    assert resp.status_code == 422


# --------------------------------------------------------------------------
# 2 + 3. 执行路径外露 + 影响分级审计（R13.3/R13.4/R13.12）
# --------------------------------------------------------------------------


def test_impact_exposes_execution_path_consistent_with_level(
    client: TestClient, application: object
) -> None:
    """ImpactAnalysisOut.execution_path ∈ {PROPOSED, ESCALATED} 且与 autonomy_level 一致。"""
    body = _register_breakdown(client, application)
    impact = body["impact"]
    assert impact["execution_path"] in {"PROPOSED", "ESCALATED"}
    # L5 → ESCALATED；L3 → PROPOSED（design.md §3.6 P0 映射）。
    if impact["autonomy_level"] == "L5":
        assert impact["execution_path"] == "ESCALATED"
    else:
        assert impact["autonomy_level"] == "L3"
        assert impact["execution_path"] == "PROPOSED"

    # 持久化的 impact_assessments 行与响应一致。
    factory = _factory(application)
    with factory() as db:
        assessment = db.execute(select(orm.ImpactAssessment)).scalars().one()
    assert assessment.execution_path == impact["execution_path"]
    assert assessment.autonomy_level == impact["autonomy_level"]


def test_get_impact_readback_includes_execution_path(
    client: TestClient, application: object
) -> None:
    """`GET /disruptions/{id}/impact` 回读也带 execution_path，与登记一致。"""
    body = _register_breakdown(client, application)
    disruption_id = body["disruption_id"]
    got = client.get(f"{DISRUPTIONS}/{disruption_id}/impact")
    assert got.status_code == 200, got.text
    assert got.json()["execution_path"] == body["impact"]["execution_path"]


def test_replan_writes_impact_classification_audit(
    client: TestClient, application: object
) -> None:
    """重排写一条 IMPACT_CLASSIFICATION 审计，载荷含判据与 execution_path（R13.12）。"""
    body = _register_breakdown(client, application)
    factory = _factory(application)
    with factory() as db:
        rows = db.execute(
            select(orm.AuditLog).where(
                orm.AuditLog.event_category == "IMPACT_CLASSIFICATION"
            )
        ).scalars().all()
    assert len(rows) == 1
    payload = rows[0].payload
    assert payload["impact_class"] == body["impact"]["impact_class"]
    assert payload["autonomy_level"] == body["impact"]["autonomy_level"]
    assert payload["execution_path"] == body["impact"]["execution_path"]
    assert "decisive_predicates" in payload
    assert isinstance(payload["decisive_predicates"], list)


# --------------------------------------------------------------------------
# 4. 价值台账 K-14（R13.13、R13.12）
# --------------------------------------------------------------------------


def test_value_ledger_empty_before_any_decision(client: TestClient) -> None:
    """尚无裁决时：计数全 0、比例 0、decisions 空（诚实空态）。"""
    resp = client.get(VALUE_LEDGER)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["auto_handled_count"] == 0
    assert body["escalated_count"] == 0
    assert body["total_decisions"] == 0
    assert body["auto_handled_ratio"] == 0.0
    assert body["decisions"] == []


def test_value_ledger_counts_match_assessments(
    client: TestClient, application: object
) -> None:
    """一次重排后：K-14 计数与 impact_assessments 的执行路径分布一致；逐条判据可见。"""
    body = _register_breakdown(client, application)
    execution_path = body["impact"]["execution_path"]

    resp = client.get(VALUE_LEDGER)
    assert resp.status_code == 200, resp.text
    ledger = resp.json()

    assert ledger["total_decisions"] == 1
    if execution_path == "ESCALATED":
        assert ledger["escalated_count"] == 1
        assert ledger["auto_handled_count"] == 0
        assert ledger["auto_handled_ratio"] == 0.0
    else:  # PROPOSED
        assert ledger["auto_handled_count"] == 1
        assert ledger["escalated_count"] == 0
        assert ledger["auto_handled_ratio"] == 1.0

    assert 0.0 <= ledger["auto_handled_ratio"] <= 1.0

    # 逐条裁决带决定性判据（R13.12）。
    assert len(ledger["decisions"]) == 1
    decision = ledger["decisions"][0]
    assert decision["execution_path"] == execution_path
    assert decision["impact_class"] == body["impact"]["impact_class"]
    assert decision["autonomy_level"] == body["impact"]["autonomy_level"]
    assert "decisive_predicates" in decision


def test_value_ledger_counts_are_cumulative(
    client: TestClient, application: object
) -> None:
    """多条裁决 → 计数按 execution_path 累计（K-14）；PROPOSED/ESCALATED 各计一侧。

    只能有一次真实重排：同一 `production_date` 上 `ux_active_per_day` / `ux_pending_per_day`
    两个部分唯一索引把「一天一个 ACTIVE、一个 PENDING」写死（属性 15，R11.8/R12.6），因此无法
    在同一天生成/重排出第二个独立计划。要验证「计数是累计的、两类路径分别归边」这条纯聚合性质，
    在真实的第一条裁决之外**直接补写**一条相反路径的 `impact_assessments` 行（FK 指向已存在的
    计划），再断言 `autonomy_summary` 把两条分别计入 auto_handled / escalated。这样测的是 K-14
    聚合本身，而不去违反计划的每日唯一性不变量。
    """
    from datetime import datetime

    body = _register_breakdown(client, application)
    first_path = body["impact"]["execution_path"]
    # 相反的执行路径：确保两侧计数各有一条，能验证「分别归边」。
    other_path = "ESCALATED" if first_path == "PROPOSED" else "PROPOSED"

    factory = _factory(application)
    with factory() as db:
        existing = db.execute(select(orm.ImpactAssessment)).scalars().one()
        db.add(
            orm.ImpactAssessment(
                assessment_id="IA-synthetic-2",
                candidate_plan_id=existing.candidate_plan_id,
                baseline_plan_id=existing.baseline_plan_id,
                disruption_id=existing.disruption_id,
                impact_class="IMPACT_MAJOR" if other_path == "ESCALATED" else "IMPACT_MODERATE",
                autonomy_level="L5" if other_path == "ESCALATED" else "L3",
                decisive_predicates=["synthetic=true"],
                impact_input={},
                execution_path=other_path,
                created_at=datetime.now(),  # noqa: DTZ005 — 全库 naive 本地时间口径
            )
        )
        db.commit()

    resp = client.get(VALUE_LEDGER)
    assert resp.status_code == 200, resp.text
    ledger = resp.json()
    # 两条裁决，一 PROPOSED 一 ESCALATED，因此两侧各 1。
    assert ledger["total_decisions"] == 2
    assert ledger["auto_handled_count"] == 1
    assert ledger["escalated_count"] == 1
    assert ledger["auto_handled_ratio"] == 0.5
    assert len(ledger["decisions"]) == 2
