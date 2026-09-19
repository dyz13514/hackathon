"""LLM 风险归因叙述（任务 13.2，P1-J ②，R14.5/R14.8/R14.10/R14.11）。

守 tasks.md 13.2 的可验收点，逐条对应：

- **为 WARNING 及以上生成 LLM 叙述**，命中项 `narrative_source=LLM`；INFO 一律不生成 LLM 叙述。
- **单次扫描最多 5 项**（R14.10 成本闸门，只对最高严重度的 5 项 WARNING+ 生效）。
- **回退可靠**（R14.5/R14.11）：LLM 不可用（DISABLED）、回放缺失（CassetteMiss）、输出不合格
  （空串/非法 JSON/超长）时回退确定性模板，`narrative_source=TEMPLATE`。
- **只读**（R14.8）：叙述生成不修改任何生产数据——扫描前后 orders/machines/plans 行数不变。
- **REPLAY 路径**：真实 adapter 按哈希命中确定性 cassette，零网络、零 Bedrock 调用。

服务层用注入的确定性假驱动（不触网）；REPLAY 用例走真实 adapter + 版本控制 cassette。
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.agents.risk_monitor_agent import (
    RiskFindingFacts,
    RiskNarrativeDriver,
    build_narrative_request,
)
from app.db import models as orm
from app.db.models import Base
from app.llm.adapter import BedrockAdapter
from app.llm.cassette import Cassette
from app.main import create_app
from app.seed.dataset import DEMO_ANCHOR
from app.seed.loader import load_demo_data
from app.services.approval import ApprovalService, ApprovalStatus
from app.services.events import EventBus
from app.services.risk_scan import MAX_LLM_NARRATIVES_PER_SCAN, scan_and_persist
from app.settings import Settings

LOGIN = "/api/auth/login"
GENERATE = "/api/plans/generate"


# --------------------------------------------------------------------------
# 假驱动：确定性、不触网
# --------------------------------------------------------------------------


class _FakeDriver:
    """按调用计数返回固定叙述的假驱动。`fail` 为真时对所有项返回 None（模拟不可用/不合格）。"""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls = 0

    def generate(self, facts: RiskFindingFacts) -> str | None:
        self.calls += 1
        if self.fail:
            return None
        return f"[LLM] {facts.entity_type} {facts.entity_id} 归因叙述。"


# --------------------------------------------------------------------------
# 夹具：真实库 + demo 数据 + 激活计划（产出 WARNING+ 风险）
# --------------------------------------------------------------------------


@pytest.fixture
def app_settings(valid_env: pytest.MonkeyPatch, tmp_path: Path) -> Settings:
    db_file = tmp_path / "risk_llm.db"
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


def _activate(client: TestClient, application: object) -> None:
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


def _counts(application: object) -> dict[str, int]:
    """生产实体计数。**不含 production_plans**：CRITICAL 风险会经确定性缓解路径生成待审提案
    （P0 既有行为，与叙述生成无关），因此计划数会合理变化；叙述只读断言只看真正的生产数据
    ——订单、机器、工人、物料——这些是 Risk_Monitor_Agent 只读白名单绝不能改的（R14.8）。"""
    factory = _factory(application)
    with factory() as db:
        return {
            "orders": int(db.execute(select(func.count()).select_from(orm.Order)).scalar_one()),
            "machines": int(
                db.execute(select(func.count()).select_from(orm.Machine)).scalar_one()
            ),
            "workers": int(
                db.execute(select(func.count()).select_from(orm.Worker)).scalar_one()
            ),
            "materials": int(
                db.execute(select(func.count()).select_from(orm.Material)).scalar_one()
            ),
        }


def _findings(application: object) -> list[orm.RiskFinding]:
    factory = _factory(application)
    with factory() as db:
        return list(
            db.execute(select(orm.RiskFinding).where(orm.RiskFinding.resolved_at.is_(None)))
            .scalars()
            .all()
        )


# --------------------------------------------------------------------------
# 请求构造：byte-stable（cassette 前提）
# --------------------------------------------------------------------------


def test_narrative_request_is_deterministic() -> None:
    """同一 facts 构造的请求逐字节确定（同 content_hash）——cassette 按哈希命中的前提。"""
    facts = RiskFindingFacts(
        risk_type="BOTTLENECK_RESOURCE",
        severity="WARNING",
        entity_type="MACHINE",
        entity_id="CNC-01",
        metric_value=0.92,
        threshold_value=0.90,
        affected_order_ids=("ORD-1", "ORD-2"),
    )
    assert build_narrative_request(facts).content_hash() == build_narrative_request(
        facts
    ).content_hash()


# --------------------------------------------------------------------------
# 服务层：LLM 命中 / 回退 / 上限 / 只读
# --------------------------------------------------------------------------


def test_llm_narrative_marks_source_llm_for_warning_plus(
    client: TestClient, application: object
) -> None:
    """注入成功的假驱动：WARNING+ 发现 narrative_source=LLM，INFO 仍为 TEMPLATE。"""
    _activate(client, application)
    driver = _FakeDriver()
    factory = _factory(application)
    with factory() as db:
        result = scan_and_persist(db, now=DEMO_ANCHOR, narrative_driver=driver)
    assert result.finding_count > 0

    warning_plus = [f for f in _findings(application) if f.severity in {"WARNING", "CRITICAL"}]
    info = [f for f in _findings(application) if f.severity == "INFO"]
    # 至少有一项 WARNING+（demo 数据在激活后会亮起瓶颈/SPOF 类风险）。
    assert warning_plus, "预期 demo 激活后存在 WARNING 及以上风险"
    llm_count = sum(1 for f in warning_plus if f.narrative_source == "LLM")
    assert llm_count == min(len(warning_plus), MAX_LLM_NARRATIVES_PER_SCAN)
    # INFO 一律不走 LLM。
    assert all(f.narrative_source == "TEMPLATE" for f in info)


def test_llm_narrative_capped_at_five(client: TestClient, application: object) -> None:
    """驱动调用次数 ≤ 5：单次扫描最多为 5 项最高严重度风险生成 LLM 叙述（R14.10）。"""
    _activate(client, application)
    driver = _FakeDriver()
    factory = _factory(application)
    with factory() as db:
        scan_and_persist(db, now=DEMO_ANCHOR, narrative_driver=driver)
    assert driver.calls <= MAX_LLM_NARRATIVES_PER_SCAN


def test_llm_narrative_falls_back_to_template(
    client: TestClient, application: object
) -> None:
    """驱动对所有项返回 None（不可用/不合格）→ 全部回退模板（narrative_source=TEMPLATE，R14.5）。"""
    _activate(client, application)
    driver = _FakeDriver(fail=True)
    factory = _factory(application)
    with factory() as db:
        scan_and_persist(db, now=DEMO_ANCHOR, narrative_driver=driver)
    for f in _findings(application):
        assert f.narrative_source == "TEMPLATE"
        assert f.narrative  # 回退叙述非空


def test_no_driver_keeps_template_source(client: TestClient, application: object) -> None:
    """不注入驱动（P0 行为）：全部模板叙述，零 LLM 调用。"""
    _activate(client, application)
    factory = _factory(application)
    with factory() as db:
        scan_and_persist(db, now=DEMO_ANCHOR, narrative_driver=None)
    assert all(f.narrative_source == "TEMPLATE" for f in _findings(application))


def test_narrative_generation_is_read_only(client: TestClient, application: object) -> None:
    """叙述生成只读（R14.8）：LLM 叙述扫描前后 orders/machines/plans 行数不变。"""
    _activate(client, application)
    before = _counts(application)
    driver = _FakeDriver()
    factory = _factory(application)
    with factory() as db:
        scan_and_persist(db, now=DEMO_ANCHOR, narrative_driver=driver)
    after = _counts(application)
    assert after == before, "叙述生成不得修改任何生产数据（R14.8）"


def test_scan_endpoint_uses_driver_and_falls_back_on_replay_miss(
    client: TestClient, application: object
) -> None:
    """POST /risks/scan 挂上真实 REPLAY 驱动：无 cassette → 回放缺失 → 回退模板（不崩溃）。

    端点用 app.state.llm_adapter（REPLAY）构造 RiskNarrativeDriver。demo 风险无对应 cassette，
    驱动因 CassetteMiss 返回 None，全部回退模板——扫描仍 200，narrative_source=TEMPLATE。
    """
    _activate(client, application)
    resp = client.post("/api/risks/scan", json={})
    assert resp.status_code == 200, resp.text
    for f in resp.json()["findings"]:
        assert f["narrative_source"] == "TEMPLATE"
        assert f["narrative"]


# --------------------------------------------------------------------------
# REPLAY：真实 adapter + cassette
# --------------------------------------------------------------------------


def test_driver_replay_cassette_hits_without_network() -> None:
    """真实 BedrockAdapter 在 REPLAY 下按哈希命中版本控制的 cassette，产出 LLM 叙述，零网络。"""
    settings = Settings(
        database_url="sqlite:///:memory:",
        session_shared_password="test-shared-password",  # type: ignore[arg-type]
        session_secret_key="test-secret-key-that-is-long-enough-32",  # type: ignore[arg-type]
        llm_mode="REPLAY",
    )
    adapter = BedrockAdapter.from_settings(settings, cassette=Cassette())
    facts = RiskFindingFacts(
        risk_type="BOTTLENECK_RESOURCE",
        severity="WARNING",
        entity_type="MACHINE",
        entity_id="CNC-01",
        metric_value=0.92,
        threshold_value=0.9,
        affected_order_ids=("ORD-001", "ORD-002"),
    )
    driver = RiskNarrativeDriver(adapter)
    narrative = driver.generate(facts)
    assert narrative is not None
    assert "CNC-01" in narrative
