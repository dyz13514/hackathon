"""CRITICAL 风险 → 确定性缓解提案（任务 8.6，R14.7，用户裁决 Option 2）。

严重度分流（R14.6–7）的行为证明（走真实 SQLite + 真实 seed + 真实内核，不 mock、无 LLM）：

- **INFO / WARNING 不生成提案**：面板专属；`_propose_mitigations_for_critical` 对它们空返回。
- **CRITICAL 生成恰好一个 RISK_MITIGATION 提案**：经确定性再优化 + 分级 + 自主判定 + 落库，
  `origin = 'RISK_MITIGATION'`、`status = 'PENDING_APPROVAL'`，写 `impact_assessments`
  （证明分级/自主判定确实执行）；`risk_findings.mitigation_plan_id` 链到该提案。
- **不伪造扰动**：提案的 `impact_assessments.disruption_id` 为 NULL（缓解不是扰动）。
- **幂等**：重复扫描不为同一批未解决 CRITICAL 重复生成提案。
- **确定性可复现**：两套等价初始状态各自独立跑，缓解提案的分级/自主/数值逐字段相同。

端到端：seed + 生成计划稳定含一条 CRITICAL（`ZERO_SLACK_ORDER`），因此激活后一次扫描应恰好
产出一个缓解提案。
"""

from __future__ import annotations

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
from app.services.risk_scan import _propose_mitigations_for_critical
from app.settings import Settings

LOGIN = "/api/auth/login"
GENERATE = "/api/plans/generate"
SCAN = "/api/risks/scan"
RISK_MITIGATION = "RISK_MITIGATION"


@pytest.fixture
def app_settings(valid_env: pytest.MonkeyPatch, tmp_path: Path) -> Settings:
    db_file = tmp_path / "mitigation.db"
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


def _mitigation_plans(application: object) -> list[orm.ProductionPlan]:
    factory = _factory(application)
    with factory() as db:
        return list(
            db.execute(
                select(orm.ProductionPlan).where(
                    orm.ProductionPlan.origin == RISK_MITIGATION
                )
            ).scalars()
        )


def _seed_finding(
    application: object,
    *,
    severity: str,
    finding_id: str,
    finding_key: str,
    now: datetime,
) -> None:
    """直接插入一条风险发现（用于隔离测试严重度分流，不经真实扫描）。"""
    factory = _factory(application)
    with factory() as db:
        db.add(
            orm.RiskFinding(
                finding_id=finding_id,
                finding_key=finding_key,
                risk_type="ZERO_SLACK_ORDER",
                severity=severity,
                entity_type="ORDER",
                entity_id="ORD-X",
                metric_value=0,
                threshold_value=120,
                affected_order_ids=["ORD-X"],
                narrative="t",
                narrative_source="TEMPLATE",
                first_seen_at=now,
                last_seen_at=now,
            )
        )
        db.commit()


# --------------------------------------------------------------------------
# 严重度分流：INFO / WARNING 不生成提案
# --------------------------------------------------------------------------


@pytest.mark.parametrize("severity", ["INFO", "WARNING"])
def test_info_and_warning_create_no_mitigation(
    client: TestClient, application: object, severity: str
) -> None:
    """INFO / WARNING 风险绝不生成缓解提案（R14.6，面板专属）。"""
    active_id = _activate_plan(client, application)
    # 清掉激活触发器可能已生成的东西，隔离本条断言：只放一条非 CRITICAL 发现。
    factory = _factory(application)
    with factory() as db:
        db.execute(orm.RiskFinding.__table__.delete())
        db.execute(
            orm.ProductionPlan.__table__.delete().where(
                orm.ProductionPlan.origin == RISK_MITIGATION
            )
        )
        db.commit()
    _seed_finding(
        application, severity=severity, finding_id="F-1", finding_key="k1", now=DEMO_ANCHOR
    )

    with factory() as db:
        proposed = _propose_mitigations_for_critical(
            db, active_plan_id=active_id, now=DEMO_ANCHOR
        )
    assert proposed == []
    assert _mitigation_plans(application) == []


# --------------------------------------------------------------------------
# CRITICAL → 恰好一个 RISK_MITIGATION 提案
# --------------------------------------------------------------------------


def test_critical_creates_one_mitigation_proposal_end_to_end(
    client: TestClient, application: object
) -> None:
    """激活计划后扫描：seed 的 CRITICAL 触发恰好一个 RISK_MITIGATION 提案，链接 + 分级齐全。"""
    active_id = _activate_plan(client, application)
    # 激活触发器可能已扫过一次；清空以从确定起点手动扫一次。
    factory = _factory(application)
    with factory() as db:
        db.execute(orm.RiskFinding.__table__.delete())
        # 解开可能已建的缓解提案自引用后删除，回到「只有 ACTIVE」的起点。
        db.execute(
            orm.ProductionPlan.__table__.update().values(
                supersedes_plan_id=None, superseded_by_plan_id=None
            )
        )
        # 删除除 ACTIVE 外的计划（清掉激活触发器可能生成的缓解提案 + 其基线）。
        db.execute(
            orm.ScheduledJob.__table__.delete().where(
                orm.ScheduledJob.plan_id != active_id
            )
        )
        db.execute(orm.ImpactAssessment.__table__.delete())
        db.execute(orm.BaselineComparison.__table__.delete())
        db.execute(
            orm.ProductionPlan.__table__.delete().where(
                orm.ProductionPlan.status != "ACTIVE"
            )
        )
        db.commit()

    resp = client.post(SCAN, json={})
    assert resp.status_code == 200, resp.text
    body = resp.json()

    # seed + 计划稳定含一条 CRITICAL（ZERO_SLACK_ORDER）。
    criticals = [f for f in body["findings"] if f["severity"] == "CRITICAL"]
    assert len(criticals) >= 1, "seed 应含至少一条 CRITICAL 风险"

    # 恰好一个 RISK_MITIGATION 提案。
    mitigations = _mitigation_plans(application)
    assert len(mitigations) == 1
    proposal = mitigations[0]
    assert proposal.origin == RISK_MITIGATION
    assert proposal.status == "PENDING_APPROVAL"  # 仍走审批（L5-bounded，R14.7）

    # 每条 CRITICAL 都链到该提案（mitigation_plan_id）。
    factory = _factory(application)
    with factory() as db:
        critical_rows = db.execute(
            select(orm.RiskFinding).where(orm.RiskFinding.severity == "CRITICAL")
        ).scalars().all()
        assert critical_rows
        for row in critical_rows:
            assert row.mitigation_plan_id == proposal.plan_id

        # 分级/自主判定确实执行：写了一行 impact_assessments，
        # 且 disruption_id 为 NULL（不伪造扰动）。
        assessment = db.execute(
            select(orm.ImpactAssessment).where(
                orm.ImpactAssessment.candidate_plan_id == proposal.plan_id
            )
        ).scalars().one()
        assert assessment.disruption_id is None  # 不伪造扰动（用户约束 1）
        assert assessment.impact_class in {"IMPACT_MINOR", "IMPACT_MODERATE", "IMPACT_MAJOR"}
        assert assessment.autonomy_level in {"L3", "L5"}  # P0 值域
        assert assessment.execution_path in {"PROPOSED", "ESCALATED"}


def test_repeat_scan_does_not_duplicate_mitigation(
    client: TestClient, application: object
) -> None:
    """重复扫描不为同一批未解决 CRITICAL 风险重复生成缓解提案（幂等）。"""
    _activate_plan(client, application)
    client.post(SCAN, json={})
    first = _mitigation_plans(application)
    # 第二、三次扫描：CRITICAL 已链接到仍在库的提案 → 不新增。
    client.post(SCAN, json={})
    client.post(SCAN, json={})
    second = _mitigation_plans(application)
    assert {p.plan_id for p in first} == {p.plan_id for p in second}
    assert len(second) <= 1  # 至多一个（每日一个 PENDING 的结构不变量）


# --------------------------------------------------------------------------
# 确定性可复现
# --------------------------------------------------------------------------


def test_mitigation_is_deterministic(
    valid_env: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """两套等价初始状态各自独立跑，缓解提案的分级/自主/数值逐字段相同（R5.7）。"""

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
            resp = c.post(GENERATE, json={})
            plan_id = resp.json()["plan_id"]
            with factory() as db:
                ev = db.get(orm.ProductionPlan, plan_id).version  # type: ignore[union-attr]
            db2 = factory()
            ApprovalService(session=db2, now=DEMO_ANCHOR, events=EventBus()).approve(
                plan_id, actor="PLANNER", expected_version=ev
            )
            db2.close()
            c.post(SCAN, json={})
        with factory() as db:
            proposal = db.execute(
                select(orm.ProductionPlan).where(
                    orm.ProductionPlan.origin == RISK_MITIGATION
                )
            ).scalars().first()
            if proposal is None:
                return {}
            a = db.execute(
                select(orm.ImpactAssessment).where(
                    orm.ImpactAssessment.candidate_plan_id == proposal.plan_id
                )
            ).scalars().one()
            return {
                "impact_class": a.impact_class,
                "autonomy_level": a.autonomy_level,
                "execution_path": a.execution_path,
                "decisive_predicates": list(a.decisive_predicates),
                "impact_input": dict(a.impact_input),  # type: ignore[arg-type]
            }

    first = run_once(tmp_path / "m1.db")
    second = run_once(tmp_path / "m2.db")
    assert first == second
    assert first  # 确实产出了一个缓解提案（seed 含 CRITICAL）


# --------------------------------------------------------------------------
# 无 LLM
# --------------------------------------------------------------------------


def test_mitigation_consumes_no_llm_budget(
    client: TestClient, application: object
) -> None:
    """缓解路径全程确定性：跑完扫描 + 提案后，traces 里没有任何真实 LLM 运行（token=0）。"""
    _activate_plan(client, application)
    client.post(SCAN, json={})
    factory = _factory(application)
    with factory() as db:
        total_tokens = db.execute(
            select(
                func.coalesce(func.sum(orm.Trace.total_input_tokens), 0)
                + func.coalesce(func.sum(orm.Trace.total_output_tokens), 0)
            )
        ).scalar_one()
    assert int(total_tokens) == 0  # 无 LLM 消耗
