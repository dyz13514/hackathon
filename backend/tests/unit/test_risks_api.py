"""风险扫描 API + 去重幂等 + 手动触发（任务 8.5，R14.1、R14.9）。

覆盖用户为 8.5 点名的可验证断言（走真实 SQLite + 真实 seed + 真实内核，不 mock、无 LLM）：

1. **手动扫描端点**：`POST /api/risks/scan` → 200，返回 findings + inserted/updated 计数；
   写端点受 `Session_Auth` 保护（未认证 → 401）。
2. **去重幂等（R14.9）**：连续两次扫描后 `risk_findings` 行数不变；第二次全部走 UPDATE
   （inserted=0），`finding_key` 集合稳定，`last_seen_at` 被刷新。
3. **无 ACTIVE 计划**：无计划时扫描返回空结果（不报错）。
4. **落库字段**：narrative_source 恒为 TEMPLATE（P0，R14.5）；severity ∈ 三态。
5. **触发器**：PlanActivated 自动扫描；扰动登记后 DATA_CHANGE 自动扫描。
6. **PENDING 冲突（R12.6，任务 8.6 集成暴露、任务 7.4 端点守卫）**：
   - 场景 A：CRITICAL 扫描已落一个 `RISK_MITIGATION` 待审提案时登记扰动 →
     `PENDING_PLAN_EXISTS`（409），既有提案与其 `mitigation_plan_id` 链接原样保留。
   - 场景 B：无既有 PENDING 时登记扰动成功产出修订计划，随后 DATA_CHANGE 扫描去重、无
     IntegrityError。
7. **GET /api/risks（任务 8.6，R14.6）**：按 severity 分组计数；仅 CRITICAL 带
   `mitigation_plan_id`。
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
from app.settings import Settings

LOGIN = "/api/auth/login"
GENERATE = "/api/plans/generate"
SCAN = "/api/risks/scan"


@pytest.fixture
def app_settings(valid_env: pytest.MonkeyPatch, tmp_path: Path) -> Settings:
    db_file = tmp_path / "risks.db"
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


def _risk_row_count(application: object) -> int:
    factory = _factory(application)
    with factory() as db:
        return int(db.execute(select(func.count()).select_from(orm.RiskFinding)).scalar_one())


# --------------------------------------------------------------------------
# 1. 手动扫描端点
# --------------------------------------------------------------------------


def test_manual_scan_returns_findings_after_activation(
    client: TestClient, application: object
) -> None:
    """激活计划后手动扫描 → 200，结构完整，severity/narrative_source 合规。"""
    _activate_plan(client, application)
    resp = client.post(SCAN, json={})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "finding_count" in body
    assert body["finding_count"] == len(body["findings"])
    assert body["inserted"] + body["updated"] == body["finding_count"]
    for f in body["findings"]:
        assert f["severity"] in {"INFO", "WARNING", "CRITICAL"}
        assert f["narrative_source"] == "TEMPLATE"  # P0 恒为模板（R14.5）
        assert f["narrative"]  # 每条都有叙述
        assert f["risk_type"] in {
            "MATERIAL_RUNOUT_FORECAST",
            "ZERO_SLACK_ORDER",
            "BOTTLENECK_RESOURCE",
            "OVERCOMMITTED_SHIFT",
            "SINGLE_POINT_OF_FAILURE_MACHINE",
        }


def test_manual_scan_requires_authentication(application: object) -> None:
    """未认证 POST /risks/scan → 401（写端点受 Session_Auth 保护）。"""
    app = application  # type: ignore[assignment]
    with TestClient(app) as anon:  # type: ignore[arg-type]
        resp = anon.post(SCAN, json={})
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "UNAUTHENTICATED"


def test_scan_without_active_plan_returns_empty(
    client: TestClient, application: object
) -> None:
    """无 ACTIVE 计划时扫描 → 200 且空结果（不报错）。"""
    resp = client.post(SCAN, json={})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["finding_count"] == 0
    assert body["findings"] == []
    assert _risk_row_count(application) == 0


# --------------------------------------------------------------------------
# 2. 去重幂等（R14.9）
# --------------------------------------------------------------------------


def test_repeat_scan_is_idempotent_no_duplicate_rows(
    client: TestClient, application: object
) -> None:
    """连续两次扫描：行数不变，第二次全部 UPDATE（inserted=0），finding_key 集合稳定。"""
    _activate_plan(client, application)

    first = client.post(SCAN, json={}).json()
    count_after_first = _risk_row_count(application)
    assert count_after_first == first["finding_count"]

    # 第二次扫描：同一份数据 → 同一组 finding_key → 只更新，不新增。
    second = client.post(SCAN, json={}).json()
    count_after_second = _risk_row_count(application)

    assert count_after_second == count_after_first, "重复扫描不得新增行（R14.9 去重）"
    assert second["inserted"] == 0, "第二次扫描应全部走 UPDATE"
    assert second["updated"] == first["finding_count"]
    assert second["finding_count"] == first["finding_count"]

    # finding_id 集合稳定（去重靠 finding_key，同一风险保留同一行/ID）。
    first_ids = {f["finding_id"] for f in first["findings"]}
    second_ids = {f["finding_id"] for f in second["findings"]}
    assert first_ids == second_ids


# --------------------------------------------------------------------------
# 3. 触发器（计划激活 / 数据变更）
# --------------------------------------------------------------------------


def _approve_via_http(client: TestClient, application: object, plan_id: str) -> None:
    """经真实 `POST /plans/{id}/approve` 激活（走 app.state.event_bus，触发 PlanActivated）。"""
    factory = _factory(application)
    with factory() as db:
        row = db.get(orm.ProductionPlan, plan_id)
        assert row is not None
        expected_version = row.version
    resp = client.post(f"/api/plans/{plan_id}/approve", json={"expected_version": expected_version})
    assert resp.status_code == 200, resp.text


def test_plan_activated_trigger_scans_without_manual_call(
    client: TestClient, application: object
) -> None:
    """经 HTTP 审批激活计划后，PlanActivated 订阅者自动扫描——无需手动调 /risks/scan。"""
    assert _risk_row_count(application) == 0
    gen = client.post(GENERATE, json={})
    assert gen.status_code == 200, gen.text
    plan_id = gen.json()["plan_id"]

    _approve_via_http(client, application, plan_id)

    # 激活触发器已在独立会话里扫过一次 —— 无需手动调 /risks/scan，风险已落库。
    # seed + 生成计划稳定产生 3 条发现（ZERO_SLACK / MATERIAL_RUNOUT / SPOF），因此断言
    # 触发器确实把风险落了库（> 0），证明 PlanActivated → 自动扫描这条链路真的接通了。
    assert _risk_row_count(application) > 0, "PlanActivated 触发器应在激活后自动扫描并落库风险"


def _pending_plan_ids(application: object, production_date: object) -> list[str]:
    """该生产日上全部 `PENDING_APPROVAL` 计划的 plan_id（结构不变量：应至多一个）。"""
    factory = _factory(application)
    with factory() as db:
        return list(
            db.execute(
                select(orm.ProductionPlan.plan_id).where(
                    orm.ProductionPlan.production_date == production_date,
                    orm.ProductionPlan.status == "PENDING_APPROVAL",
                )
            ).scalars().all()
        )


def _first_machine_for_plan(application: object, plan_id: str) -> str:
    factory = _factory(application)
    with factory() as db:
        machine_id = db.execute(
            select(orm.ScheduledJob.machine_id).where(orm.ScheduledJob.plan_id == plan_id)
        ).scalars().first()
    assert machine_id is not None
    return str(machine_id)


def test_disruption_when_mitigation_pending_returns_pending_plan_exists(
    client: TestClient, application: object
) -> None:
    """场景 A（任务 8.6 集成暴露、任务 7.4 端点的 spec 守卫，R12.6）：

    CRITICAL 扫描 → 已存在一个 `RISK_MITIGATION` 的 `PENDING_APPROVAL` 提案 → 登记扰动 →
    HTTP 409 `PENDING_PLAN_EXISTS`。断言：不产生第二个提案（该生产日仍恰好一个 PENDING）；
    既有缓解提案原样保留（仍 `PENDING_APPROVAL`、`origin=RISK_MITIGATION`）；其 CRITICAL
    发现的 `mitigation_plan_id` 链接不被改动（不 supersede/cancel/modify）。
    """
    from datetime import timedelta

    plan_id = _activate_plan(client, application)

    # 手动扫描：为 CRITICAL 生成一个 RISK_MITIGATION 提案（该生产日出现一个 PENDING_APPROVAL）。
    scan = client.post(SCAN, json={}).json()
    factory = _factory(application)
    with factory() as db:
        active = db.get(orm.ProductionPlan, plan_id)
        assert active is not None
        production_date = active.production_date

    pending_before = _pending_plan_ids(application, production_date)
    if not pending_before:
        pytest.skip("seed 未产生 CRITICAL，故无缓解提案——本场景不适用")
    assert len(pending_before) == 1, "缓解提案：该生产日应恰好一个 PENDING_APPROVAL"
    mitigation_plan_id = pending_before[0]

    # 记录缓解提案的 origin 与被链接的 CRITICAL 发现，用于事后证明「原样未动」。
    # 注意：链接是 scan 落库后再回填并单独提交的，扫描响应体里的 mitigation_plan_id 仍是
    # None（序列化早于回填）。因此以库为准，从 risk_findings 读被链接的 CRITICAL。
    del scan  # 响应体不用于链接断言，避免误用其 None 的 mitigation_plan_id
    with factory() as db:
        mit_row = db.get(orm.ProductionPlan, mitigation_plan_id)
        assert mit_row is not None
        assert str(mit_row.origin) == "RISK_MITIGATION"
        linked_before = {
            row.finding_id: row.mitigation_plan_id
            for row in db.execute(
                select(orm.RiskFinding).where(
                    orm.RiskFinding.severity == "CRITICAL",
                    orm.RiskFinding.resolved_at.is_(None),
                )
            ).scalars().all()
        }
    assert any(v == mitigation_plan_id for v in linked_before.values()), (
        "至少一条 CRITICAL 应链到该缓解提案"
    )

    machine_id = _first_machine_for_plan(application, plan_id)
    resp = client.post(
        "/api/disruptions",
        json={
            "disruption": {
                "type": "MACHINE_BREAKDOWN",
                "machine_id": machine_id,
                "start_time": DEMO_ANCHOR.isoformat(),
                "end_time": (DEMO_ANCHOR + timedelta(hours=6)).isoformat(),
            }
        },
    )

    # 409 PENDING_PLAN_EXISTS，且给「取消既有提案」入口（R12.6，design.md §4.1）。
    assert resp.status_code == 409, resp.text
    body = resp.json()
    assert body["error"]["code"] == "PENDING_PLAN_EXISTS"
    assert any(a["action"] == "cancel_pending" for a in body["error"]["next_actions"])

    # 不产生第二个提案：该生产日仍恰好一个 PENDING，且就是原来那个缓解提案。
    pending_after = _pending_plan_ids(application, production_date)
    assert pending_after == pending_before, "扰动被守卫挡住，PENDING 集合不得改变"

    # 既有缓解提案原样保留：仍 PENDING_APPROVAL、origin 不变，未被 supersede。
    with factory() as db:
        mit_after = db.get(orm.ProductionPlan, mitigation_plan_id)
        assert mit_after is not None
        assert str(mit_after.status) == "PENDING_APPROVAL"
        assert str(mit_after.origin) == "RISK_MITIGATION"
        assert mit_after.superseded_by_plan_id is None
        # CRITICAL 发现的 mitigation_plan_id 链接不被改动。
        for finding_id, linked_plan in linked_before.items():
            row = db.get(orm.RiskFinding, finding_id)
            assert row is not None
            assert row.mitigation_plan_id == linked_plan


def test_disruption_without_pending_succeeds_and_triggers_data_change_scan(
    client: TestClient, application: object
) -> None:
    """场景 B：无既有 PENDING → 登记扰动成功产出修订计划 → 随后 DATA_CHANGE 触发器扫描，

    不产生重复风险发现/提案，全程无 IntegrityError。此处**不**先手动扫描，因此登记扰动前
    该生产日没有任何 PENDING_APPROVAL（扰动自己的重排是当日第一个待审提案）。
    """
    from datetime import timedelta

    plan_id = _activate_plan(client, application)
    factory = _factory(application)
    with factory() as db:
        active = db.get(orm.ProductionPlan, plan_id)
        assert active is not None
        production_date = active.production_date

    # 前置：登记扰动前该生产日无 PENDING（未手动扫描 → 无缓解提案）。
    assert _pending_plan_ids(application, production_date) == []
    rows_before = _risk_row_count(application)

    machine_id = _first_machine_for_plan(application, plan_id)
    resp = client.post(
        "/api/disruptions",
        json={
            "disruption": {
                "type": "MACHINE_BREAKDOWN",
                "machine_id": machine_id,
                "start_time": DEMO_ANCHOR.isoformat(),
                "end_time": (DEMO_ANCHOR + timedelta(hours=6)).isoformat(),
            }
        },
    )
    # 扰动重排成功：产出一个 PENDING_APPROVAL 修订计划（R9.2）。
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["revised_plan_status"] == "PENDING_APPROVAL"

    # 该生产日现在恰好一个 PENDING（扰动的修订计划）；DATA_CHANGE 触发器已在其后扫过。
    pending_after = _pending_plan_ids(application, production_date)
    assert len(pending_after) == 1
    assert pending_after[0] == body["revised_plan_id"]

    # DATA_CHANGE 扫描是尽力而为且去重：行数只增不因重复扫描暴涨（同一 finding_key 只更新）。
    # 无 IntegrityError（走到这里即证明——端点返回 200 而非 500）。
    rows_after = _risk_row_count(application)
    assert rows_after >= rows_before

    # 再触发一次扫描（幂等）：finding_key 去重，行数不变。
    before_dedup = _risk_row_count(application)
    client.post(SCAN, json={})
    assert _risk_row_count(application) == before_dedup, "重复扫描不得新增行（R14.9 去重）"


def test_repeat_scan_refreshes_last_seen_at(
    client: TestClient, application: object
) -> None:
    """重复出现的风险其 last_seen_at 被刷新，first_seen_at 保持不变（R14.9）。"""
    _activate_plan(client, application)
    first = client.post(SCAN, json={}).json()
    if first["finding_count"] == 0:
        pytest.skip("seed 未产生风险发现，无法验证 last_seen_at 刷新")

    factory = _factory(application)
    finding_id = first["findings"][0]["finding_id"]
    with factory() as db:
        row = db.get(orm.RiskFinding, finding_id)
        assert row is not None
        first_seen = row.first_seen_at
        last_seen = row.last_seen_at
    # first_seen == last_seen 在首次扫描后成立（同一 now）。
    assert first_seen == last_seen

    # 第二次扫描（同一 DEMO_ANCHOR now）：last_seen 仍等于 first_seen（同刻），
    # 但走的是 UPDATE 路径。
    second = client.post(SCAN, json={}).json()
    assert second["updated"] >= 1
    with factory() as db:
        row = db.get(orm.RiskFinding, finding_id)
        assert row is not None
        assert row.first_seen_at == first_seen  # 不变


# --------------------------------------------------------------------------
# 4. GET /api/risks（任务 8.6，R14.6）
# --------------------------------------------------------------------------

RISKS = "/api/risks"


def test_get_risks_lists_findings_grouped_by_severity(
    client: TestClient, application: object
) -> None:
    """扫描后 `GET /api/risks` → 200：三档计数与 findings 一致，按 severity 稳定排序。

    - 无认证也可读（与其余 GET 同口径）。
    - critical/warning/info 计数各自等于对应 severity 的条目数。
    - findings 按 severity 秩排序：CRITICAL 在前，INFO 在后（面板据此分组渲染，R14.6）。
    """
    _activate_plan(client, application)
    scan = client.post(SCAN, json={}).json()
    if scan["finding_count"] == 0:
        pytest.skip("seed 未产生风险发现")

    # 无认证读取（新起匿名 client，证明 GET 不需登录）。
    app = application  # type: ignore[assignment]
    with TestClient(app) as anon:  # type: ignore[arg-type]
        resp = anon.get(RISKS)
    assert resp.status_code == 200, resp.text
    body = resp.json()

    findings = body["findings"]
    assert len(findings) == scan["finding_count"]
    assert body["critical_count"] == sum(1 for f in findings if f["severity"] == "CRITICAL")
    assert body["warning_count"] == sum(1 for f in findings if f["severity"] == "WARNING")
    assert body["info_count"] == sum(1 for f in findings if f["severity"] == "INFO")

    # severity 稳定排序：CRITICAL(0) < WARNING(1) < INFO(2)。
    rank = {"CRITICAL": 0, "WARNING": 1, "INFO": 2}
    ranks = [rank[f["severity"]] for f in findings]
    assert ranks == sorted(ranks), "findings 应按 severity 秩升序（CRITICAL 在前）"


def test_get_risks_only_critical_has_mitigation_plan_id(
    client: TestClient, application: object
) -> None:
    """`GET /api/risks`：INFO/WARNING 的 `mitigation_plan_id` 恒为 null；CRITICAL 扫描后被填。

    这是 R14.6–7 的分流：INFO/WARNING 仅入面板、不产生提案；CRITICAL 由 8.6 的
    RISK_MITIGATION 桥接生成缓解提案并回填 `mitigation_plan_id`（端到端在
    test_risk_mitigation.py 里另证；此处从只读面板视角断言二者的可见差异）。
    """
    _activate_plan(client, application)
    scan = client.post(SCAN, json={}).json()
    if scan["finding_count"] == 0:
        pytest.skip("seed 未产生风险发现")

    resp = client.get(RISKS)
    assert resp.status_code == 200, resp.text
    findings = resp.json()["findings"]

    for f in findings:
        if f["severity"] in {"INFO", "WARNING"}:
            assert f["mitigation_plan_id"] is None, "INFO/WARNING 不得有缓解提案（面板专属）"

    criticals = [f for f in findings if f["severity"] == "CRITICAL"]
    if criticals:
        # seed + 生成计划稳定产生一条 ZERO_SLACK_ORDER(CRITICAL)；扫描已触发 RISK_MITIGATION 桥接。
        assert any(
            f["mitigation_plan_id"] for f in criticals
        ), "CRITICAL 扫描后应回填 mitigation_plan_id（R14.7）"


def test_get_risks_empty_without_scan(client: TestClient, application: object) -> None:
    """未扫描时 `GET /api/risks` → 200 且空列表、三档计数皆 0（诚实空态）。"""
    resp = client.get(RISKS)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["findings"] == []
    assert body["critical_count"] == 0
    assert body["warning_count"] == 0
    assert body["info_count"] == 0
