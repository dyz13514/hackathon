"""L4 自动应用与一键回滚（任务 13.4，P1-J ④，R13.7/R13.9/R13.10）。

守 tasks.md 13.4 的可验收点，逐条对应：

- **只有 IMPACT_MINOR 且开关开启才 L4**：`decide_autonomy` 分级判定（kernel）逐条断言。
- **L3、L5 仍走人工**：`auto_apply_if_l4` 对非 L4 不动作（applied=False，不写记录、计划仍待审）。
- **L4 自动应用写 AutoAppliedChange**（snapshot_before/after、reverted=false）；`execution_path`
  记 `AUTO_APPLIED`；修订计划变 ACTIVE、原 ACTIVE 被 supersede。
- **一键回滚经 activate_internal**：回滚后恢复计划 ACTIVE、`plan_id_after` SUPERSEDED、
  `reverted=true`；回滚计划逐字段等于 `snapshot_before`，且硬约束违反数为 0（R13.10）。
- **不新增 schema 迁移**：`auto_applied_changes` 表已在任务 1.2 建成，本测试直接读写它。
- **GET /api/autonomy/changes 与 POST .../revert**（含认证、幂等、404）。

走真实 create_app + 真实 SQLite + 真实内核，不 mock。零 LLM。
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.core.autonomy import FeatureFlags, ImpactClass, decide_autonomy
from app.core.validation import validate
from app.db import models as orm
from app.db.models import Base
from app.main import create_app
from app.orchestrator.pipelines.replan_deterministic import run_risk_mitigation
from app.seed.dataset import DEMO_ANCHOR
from app.seed.loader import load_demo_data
from app.services.approval import ApprovalService, ApprovalStatus
from app.services.auto_apply import (
    AUTO_APPLIED_EXECUTION_PATH,
    RevertStatus,
    auto_apply_if_l4,
    revert_change,
    serialize_scheduled_jobs,
)
from app.services.events import EventBus
from app.services.replanning import load_plan_candidate
from app.services.snapshot_loader import load_snapshot
from app.settings import Settings

LOGIN = "/api/auth/login"
GENERATE = "/api/plans/generate"
CHANGES = "/api/autonomy/changes"


# --------------------------------------------------------------------------
# 1. 分级判定（kernel）：只有 IMPACT_MINOR + 开关 → L4；L3/L5 仍人工
# --------------------------------------------------------------------------


def test_decide_autonomy_l4_only_for_minor_when_enabled() -> None:
    on = FeatureFlags(auto_apply_minor_enabled=True)
    off = FeatureFlags(auto_apply_minor_enabled=False)
    # IMPACT_MINOR：开关开 → L4；开关关 → L3（人工）。
    assert decide_autonomy(ImpactClass.IMPACT_MINOR, on).value == "L4"
    assert decide_autonomy(ImpactClass.IMPACT_MINOR, off).value == "L3"
    # IMPACT_MODERATE：无论开关，恒 L3（人工提案）。
    assert decide_autonomy(ImpactClass.IMPACT_MODERATE, on).value == "L3"
    assert decide_autonomy(ImpactClass.IMPACT_MODERATE, off).value == "L3"
    # IMPACT_MAJOR：无论开关，恒 L5（上报人工，结构上不可覆盖）。
    assert decide_autonomy(ImpactClass.IMPACT_MAJOR, on).value == "L5"
    assert decide_autonomy(ImpactClass.IMPACT_MAJOR, off).value == "L5"


# --------------------------------------------------------------------------
# 夹具
# --------------------------------------------------------------------------


@pytest.fixture
def app_settings(valid_env: pytest.MonkeyPatch, tmp_path: Path) -> Settings:
    db_file = tmp_path / "autoapply.db"
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


def _events(application: object) -> EventBus:
    return application.state.event_bus  # type: ignore[attr-defined,no-any-return]


def _generate_and_activate(client: TestClient, application: object) -> str:
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


def _reoptimize_revision(application: object, active_plan_id: str) -> tuple[str, str]:
    """在**不改变世界**的前提下产出一个 PENDING_APPROVAL 修订计划（确定性再优化）。

    返回 `(revised_plan_id, assessment_id)`。用 `run_risk_mitigation` 而非登记扰动：扰动会改变
    世界（机器停机 / 物料延迟 / 库存变化），使「回到从前那份计划」相对**已改变的**世界天然违约
    ——那是正确的领域行为，但不是本测试要验证的「回滚机制」。再优化路径的世界不变，因此
    `snapshot_before` 相对当前快照仍然可行，回滚得以逐字段还原且零违反（正是 R13.10 的语义）。
    """
    factory = _factory(application)
    with factory() as db:
        active = load_plan_candidate(db, active_plan_id)
        snapshot = load_snapshot(db, now=DEMO_ANCHOR)
        result = run_risk_mitigation(
            db,
            active_plan_id=active_plan_id,
            active_plan=active,
            snapshot=snapshot,
            finding_id="RISK-test",
            now=DEMO_ANCHOR,
            session_id="test-auto-apply",
        )
    return str(result.plan.plan_id), str(result.assessment_id)


def _plan_status(application: object, plan_id: str) -> str:
    factory = _factory(application)
    with factory() as db:
        return str(db.get(orm.ProductionPlan, plan_id).status)


# --------------------------------------------------------------------------
# 2. L4 自动应用（强制 autonomy_level=L4 驱动 auto_apply_if_l4）
# --------------------------------------------------------------------------


def test_auto_apply_l4_activates_and_records_change(
    client: TestClient, application: object
) -> None:
    """L4：修订计划被自动激活，写 AutoAppliedChange（reverted=false），并记 AUTO_APPLIED。"""
    active_before = _generate_and_activate(client, application)
    revised, assessment_id = _reoptimize_revision(application, active_before)

    factory = _factory(application)
    with factory() as db:
        result = auto_apply_if_l4(
            db,
            autonomy_level="L4",  # 强制走 L4 分支（分级本身由 kernel 测试守）
            revised_plan_id=revised,
            active_plan_id=active_before,
            assessment_id=assessment_id,
            now=DEMO_ANCHOR,
            events=_events(application),
        )
    assert result.applied is True
    assert result.change_id is not None

    # 修订计划 ACTIVE、原 ACTIVE SUPERSEDED。
    assert _plan_status(application, revised) == "ACTIVE"
    assert _plan_status(application, active_before) == "SUPERSEDED"

    # AutoAppliedChange 记录完整、未回滚。
    with factory() as db:
        change = db.get(orm.AutoAppliedChange, result.change_id)
        assert change is not None
        assert change.reverted is False
        assert change.plan_id_before == active_before
        assert change.plan_id_after == revised
        assert isinstance(change.snapshot_before, list) and change.snapshot_before
        assert isinstance(change.snapshot_after, list) and change.snapshot_after
        # execution_path 记 AUTO_APPLIED。
        assessment = db.get(orm.ImpactAssessment, assessment_id)
        assert assessment.execution_path == AUTO_APPLIED_EXECUTION_PATH


def test_auto_apply_noop_for_l3_and_l5(client: TestClient, application: object) -> None:
    """L3 / L5 不自动应用：修订计划仍 PENDING_APPROVAL，不写 AutoAppliedChange（人工流程）。"""
    active_before = _generate_and_activate(client, application)
    revised, assessment_id = _reoptimize_revision(application, active_before)
    factory = _factory(application)

    for level in ("L3", "L5"):
        with factory() as db:
            res = auto_apply_if_l4(
                db,
                autonomy_level=level,
                revised_plan_id=revised,
                active_plan_id=active_before,
                assessment_id=assessment_id,
                now=DEMO_ANCHOR,
                events=_events(application),
            )
        assert res.applied is False
    # 修订计划仍待审批；原 ACTIVE 未被取代；无自动应用记录。
    assert _plan_status(application, revised) == "PENDING_APPROVAL"
    assert _plan_status(application, active_before) == "ACTIVE"
    with factory() as db:
        assert db.execute(select(orm.AutoAppliedChange)).scalars().all() == []


# --------------------------------------------------------------------------
# 3. 一键回滚（承接原属性 19：逐字段等于 snapshot_before + 零违反）
# --------------------------------------------------------------------------


def test_revert_restores_snapshot_before_with_zero_violations(
    client: TestClient, application: object
) -> None:
    """回滚后：恢复计划 ACTIVE、plan_id_after SUPERSEDED、reverted=true；恢复计划逐字段等于
    snapshot_before，且硬约束违反数为 0（R13.10）。"""
    active_before = _generate_and_activate(client, application)
    revised, assessment_id = _reoptimize_revision(application, active_before)
    factory = _factory(application)

    with factory() as db:
        apply_result = auto_apply_if_l4(
            db,
            autonomy_level="L4",
            revised_plan_id=revised,
            active_plan_id=active_before,
            assessment_id=assessment_id,
            now=DEMO_ANCHOR,
            events=_events(application),
        )
    change_id = apply_result.change_id
    assert change_id is not None

    # 记下 snapshot_before 供逐字段比较。
    with factory() as db:
        change = db.get(orm.AutoAppliedChange, change_id)
        snapshot_before = list(change.snapshot_before)

    # 一键回滚。
    with factory() as db:
        revert_result = revert_change(
            db, change_id=change_id, now=DEMO_ANCHOR, events=_events(application)
        )
    assert revert_result.status is RevertStatus.OK
    revert_plan_id = revert_result.revert_plan_id
    assert revert_plan_id is not None

    # 恢复计划 ACTIVE；plan_id_after（revised）被 supersede；记录标 reverted=true。
    assert _plan_status(application, revert_plan_id) == "ACTIVE"
    assert _plan_status(application, revised) == "SUPERSEDED"
    with factory() as db:
        change = db.get(orm.AutoAppliedChange, change_id)
        assert change.reverted is True
        assert change.reverted_at is not None
        assert change.revert_plan_id == revert_plan_id

    # 逐字段等于 snapshot_before：恢复计划的 scheduled_jobs 序列化后与 snapshot_before 相同。
    with factory() as db:
        restored = load_plan_candidate(db, revert_plan_id)
        restored_snapshot = serialize_scheduled_jobs(restored)
    assert restored_snapshot == snapshot_before, "回滚后计划必须逐字段等于 snapshot_before"

    # 硬约束违反数为 0（回滚不产生违规计划，R13.10）。
    with factory() as db:
        snapshot = load_snapshot(db, now=DEMO_ANCHOR)
        report = validate(restored, snapshot)
    assert report.is_feasible, "回滚后计划不得有任何硬约束违反"
    assert len(report.violations) == 0


# --------------------------------------------------------------------------
# 4. API：GET /changes、POST revert、认证、幂等、404
# --------------------------------------------------------------------------


def test_get_changes_lists_records(client: TestClient, application: object) -> None:
    active_before = _generate_and_activate(client, application)
    revised, assessment_id = _reoptimize_revision(application, active_before)
    factory = _factory(application)
    with factory() as db:
        auto_apply_if_l4(
            db,
            autonomy_level="L4",
            revised_plan_id=revised,
            active_plan_id=active_before,
            assessment_id=assessment_id,
            now=DEMO_ANCHOR,
            events=_events(application),
        )
    resp = client.get(CHANGES)
    assert resp.status_code == 200, resp.text
    changes = resp.json()["changes"]
    assert len(changes) == 1
    assert changes[0]["reverted"] is False
    assert changes[0]["plan_id_after"] == revised


def test_revert_endpoint_requires_auth(application: object) -> None:
    with TestClient(application) as anon:  # type: ignore[arg-type]
        resp = anon.post(f"{CHANGES}/AAC-x/revert", json={})
    assert resp.status_code == 401, resp.text


def test_revert_endpoint_404_for_unknown(client: TestClient) -> None:
    resp = client.post(f"{CHANGES}/AAC-does-not-exist/revert", json={})
    assert resp.status_code == 404, resp.text
    assert resp.json()["error"]["code"] == "AUTO_APPLIED_CHANGE_NOT_FOUND"


def test_revert_endpoint_end_to_end_and_idempotent(
    client: TestClient, application: object
) -> None:
    """POST revert 成功后计划切换；重复回滚 → 409 ALREADY_REVERTED（幂等保护）。"""
    active_before = _generate_and_activate(client, application)
    revised, assessment_id = _reoptimize_revision(application, active_before)
    factory = _factory(application)
    with factory() as db:
        change_id = auto_apply_if_l4(
            db,
            autonomy_level="L4",
            revised_plan_id=revised,
            active_plan_id=active_before,
            assessment_id=assessment_id,
            now=DEMO_ANCHOR,
            events=_events(application),
        ).change_id
    assert change_id is not None

    first = client.post(f"{CHANGES}/{change_id}/revert", json={})
    assert first.status_code == 200, first.text
    assert first.json()["superseded_plan_id"] == revised

    second = client.post(f"{CHANGES}/{change_id}/revert", json={})
    assert second.status_code == 409, second.text
    assert second.json()["error"]["code"] == "AUTO_APPLIED_CHANGE_ALREADY_REVERTED"
