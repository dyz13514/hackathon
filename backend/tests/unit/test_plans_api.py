"""计划端点的 API 测试（任务 2.12，design.md Components §5）。

覆盖四个端点与认证边界：

- `POST /plans/generate` —— 需要会话；返回 `PENDING_APPROVAL` 计划，含 R5.5 六项；
  未认证得到 401（写端点受 `Session_Auth` 保护，R23.12）。
- `GET /plans/{plan_id}` —— 生成后可回读，形状与生成响应一致；不存在返回 `PLAN_NOT_FOUND`。
- `GET /plans/pending` —— 生成后列出该计划。
- `GET /plans/active` —— 无 ACTIVE 计划时为空（本任务不激活计划）。

走真实 SQLite 文件与真实 seed 数据（经 `POST /demo/reset` 铺入），不 mock：这几个端点
交付的就是「HTTP 层把内核结果序列化成了什么、认证有没有真的挡住写请求」。
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import Base
from app.db.session import create_db_engine
from app.main import create_app
from app.seed.loader import load_demo_data
from app.settings import Settings

LOGIN = "/api/auth/login"
GENERATE = "/api/plans/generate"


@pytest.fixture
def app_settings(valid_env: pytest.MonkeyPatch, tmp_path: Path) -> Settings:
    db_file = tmp_path / "plans-api.db"
    valid_env.setenv("DATABASE_URL", f"sqlite:///{db_file.as_posix()}")
    return Settings()  # type: ignore[call-arg]


@pytest.fixture
def client(app_settings: Settings) -> Iterator[TestClient]:
    """已建表、已铺 seed、已登录的客户端。

    seed 直接经会话工厂写入（不经 `POST /demo/reset`，那条路径在 test_demo_reset 已覆盖），
    因此这里聚焦计划端点本身。
    """
    application = create_app(app_settings)
    Base.metadata.create_all(application.state.engine)
    factory: sessionmaker[Session] = application.state.session_factory
    with factory() as session:
        load_demo_data(session)
        session.commit()
    with TestClient(application) as test_client:
        test_client.post(
            LOGIN, json={"password": app_settings.session_shared_password.get_secret_value()}
        )
        yield test_client


def _anonymous(app_settings: Settings) -> TestClient:
    application = create_app(app_settings)
    Base.metadata.create_all(application.state.engine)
    factory: sessionmaker[Session] = application.state.session_factory
    with factory() as session:
        load_demo_data(session)
        session.commit()
    return TestClient(application)


# --------------------------------------------------------------------------
# POST /plans/generate
# --------------------------------------------------------------------------


def test_generate_returns_pending_plan_with_all_six_fields(client: TestClient) -> None:
    """生成返回 200 + `PENDING_APPROVAL` + R5.5 六项齐全。"""
    response = client.post(GENERATE, json={})
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["status"] == "PENDING_APPROVAL"
    assert body["feasibility"] in {"FEASIBLE", "PARTIAL", "NO_FEASIBLE_PLAN"}
    assert isinstance(body["scheduled_jobs"], list)
    assert isinstance(body["unschedulable_jobs"], list)
    assert len(body["objective_breakdown"]["components"]) == 7
    assert body["baseline_comparison"]["snapshot_version"] >= 1
    assert body["generated_by_trace_id"].startswith("TRACE-")


def test_generate_scheduled_jobs_carry_operation_and_changeover(client: TestClient) -> None:
    """已排产作业带工序号与换型分钟（供甘特图，R5.5 / R4.4）。"""
    body = client.post(GENERATE, json={}).json()
    assert body["scheduled_jobs"], "seed 数据下应能排出作业"
    job = body["scheduled_jobs"][0]
    assert set(job) == {
        "job_id",
        "order_id",
        "product_id",
        "operation_sequence",
        "machine_id",
        "worker_id",
        "start_time",
        "end_time",
        "setup_minutes",
        "changeover_minutes",
    }
    assert job["operation_sequence"] >= 1


def test_generate_requires_authentication(app_settings: Settings) -> None:
    """未认证的生成请求得到 401（写端点受 `Session_Auth` 保护，R23.12）。"""
    with _anonymous(app_settings) as anonymous:
        response = anonymous.post(GENERATE, json={})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHENTICATED"


def test_generate_rejects_unknown_fields(client: TestClient) -> None:
    """请求体不接受 `status`（或任何多余字段）——状态由流水线硬编码（R11.8）。"""
    response = client.post(GENERATE, json={"status": "ACTIVE"})
    assert response.status_code == 422


# --------------------------------------------------------------------------
# GET /plans/{plan_id}
# --------------------------------------------------------------------------


def test_get_plan_roundtrips_the_generated_plan(client: TestClient) -> None:
    """生成后按 ID 回读，关键字段与生成响应一致。"""
    generated = client.post(GENERATE, json={}).json()
    plan_id = generated["plan_id"]

    fetched = client.get(f"/api/plans/{plan_id}")
    assert fetched.status_code == 200
    body = fetched.json()

    assert body["plan_id"] == plan_id
    assert body["status"] == "PENDING_APPROVAL"
    assert body["feasibility"] == generated["feasibility"]
    assert len(body["scheduled_jobs"]) == len(generated["scheduled_jobs"])
    assert body["baseline_comparison"]["baseline_plan_id"] == (
        generated["baseline_comparison"]["baseline_plan_id"]
    )


def test_get_unknown_plan_returns_plan_not_found(client: TestClient) -> None:
    response = client.get("/api/plans/PLAN-nope")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "PLAN_NOT_FOUND"


# --------------------------------------------------------------------------
# GET /plans/{plan_id}/explanation —— 结构化解释 + numeric_check（R10，任务 5.11）
# --------------------------------------------------------------------------


def test_get_explanation_returns_structured_evidence_and_numeric_check(
    client: TestClient,
) -> None:
    """解释端点返回结构化证据 + 叙述 + numeric_check（R10、design.md §2.1）。

    测试环境 `LLM_MODE=STUB`：未录制的解释调用返回固定占位文本（不含数字，因此数值比对
    通过），`numeric_check = PASS`。结构化字段是确定性真值：初始计划无 delta，故
    `decision_evidence` 为空、`counterfactual.kind = NO_TRADEOFF`、置信度 LOW（3 条假设）。
    """
    generated = client.post(GENERATE, json={}).json()
    plan_id = generated["plan_id"]

    response = client.get(f"/api/plans/{plan_id}/explanation")
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["plan_id"] == plan_id
    assert body["numeric_check"] in {"PASS", "FALLBACK"}
    assert body["narrative"]
    # 初始计划：无逐作业决策证据，反事实为单值 NO_TRADEOFF（R10.3 恰好 1 项）。
    assert body["decision_evidence"] == []
    assert body["counterfactual"]["kind"] == "NO_TRADEOFF"
    assert body["counterfactual"]["reason"]
    # 三条可能过期的假设 → 置信度 LOW（R10.4 / R10.5）。
    assert len(body["assumptions"]) == 3
    assert body["confidence"]["level"] == "LOW"
    assert body["confidence"]["basis"]


def test_get_explanation_for_unknown_plan_is_404(client: TestClient) -> None:
    response = client.get("/api/plans/PLAN-nope/explanation")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "PLAN_NOT_FOUND"


# --------------------------------------------------------------------------
# GET /plans/pending & /plans/active
# --------------------------------------------------------------------------


def test_pending_lists_the_generated_plan(client: TestClient) -> None:
    generated = client.post(GENERATE, json={}).json()
    pending = client.get("/api/plans/pending").json()
    ids = {plan["plan_id"] for plan in pending}
    assert generated["plan_id"] in ids
    assert all(plan["status"] == "PENDING_APPROVAL" for plan in pending)


def test_active_is_empty_before_any_approval(client: TestClient) -> None:
    """本任务不激活计划，因此生成后 `/plans/active` 仍为空。"""
    client.post(GENERATE, json={})
    active = client.get("/api/plans/active").json()
    assert active == []


def test_baseline_plan_is_not_listed_as_pending(client: TestClient) -> None:
    """基线计划（DRAFT / BASELINE）永不进审批流，因此不出现在 pending 列表（design.md §8）。"""
    generated = client.post(GENERATE, json={}).json()
    baseline_id = generated["baseline_comparison"]["baseline_plan_id"]
    pending_ids = {plan["plan_id"] for plan in client.get("/api/plans/pending").json()}
    assert baseline_id not in pending_ids


# --------------------------------------------------------------------------
# PATCH /plans/{id} —— 审批绕过防护（R11.8 / R22.9 / R23.4，任务 3.3）
# --------------------------------------------------------------------------


def test_patch_with_status_key_is_forbidden_403(client: TestClient) -> None:
    """请求体含 `status` 键即 `403 FORBIDDEN`——试图绕过审批直接改计划状态（EVAL-207）。"""
    generated = client.post(GENERATE, json={}).json()
    plan_id = generated["plan_id"]

    response = client.patch(f"/api/plans/{plan_id}", json={"status": "ACTIVE"})
    assert response.status_code == 403, response.text
    assert response.json()["error"]["code"] == "PLAN_STATUS_WRITE_FORBIDDEN"


def test_patch_status_does_not_change_plan_state(client: TestClient) -> None:
    """403 之后计划状态保持 `PENDING_APPROVAL`——绕过尝试没有任何副作用。"""
    generated = client.post(GENERATE, json={}).json()
    plan_id = generated["plan_id"]

    client.patch(f"/api/plans/{plan_id}", json={"status": "ACTIVE"})

    after = client.get(f"/api/plans/{plan_id}").json()
    assert after["status"] == "PENDING_APPROVAL"


def test_patch_status_null_is_still_forbidden(client: TestClient) -> None:
    """`status` 键存在即拒，与它的值无关（`null` 也不行）。"""
    generated = client.post(GENERATE, json={}).json()
    plan_id = generated["plan_id"]

    response = client.patch(f"/api/plans/{plan_id}", json={"status": None})
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "PLAN_STATUS_WRITE_FORBIDDEN"


def test_patch_writes_audit_on_forbidden_status(client: TestClient, app_settings: Settings) -> None:
    """403 的同时写一条 `PLAN_STATUS_WRITE_FORBIDDEN` 审计（留痕绕过尝试，R24.4）。"""
    generated = client.post(GENERATE, json={}).json()
    plan_id = generated["plan_id"]

    client.patch(f"/api/plans/{plan_id}", json={"status": "ACTIVE"})

    engine = create_db_engine(app_settings)
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT event_type FROM audit_log "
                "WHERE subject_id = :sid AND event_type = 'PLAN_STATUS_WRITE_FORBIDDEN'"
            ),
            {"sid": plan_id},
        ).fetchall()
    assert len(rows) == 1


def test_patch_unknown_field_is_422(client: TestClient) -> None:
    """非 `status` 的多余字段走常规校验：`PlanUpdateIn` 的 `extra="forbid"` → 422。"""
    generated = client.post(GENERATE, json={}).json()
    plan_id = generated["plan_id"]

    response = client.patch(f"/api/plans/{plan_id}", json={"feasibility": "FEASIBLE"})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "PLAN_UPDATE_INVALID"


def test_patch_empty_body_is_noop_200(client: TestClient) -> None:
    """空请求体是「无字段可更新」的 no-op，返回 200（P0 计划无可就地写字段）。"""
    generated = client.post(GENERATE, json={}).json()
    plan_id = generated["plan_id"]

    response = client.patch(f"/api/plans/{plan_id}", json={})
    assert response.status_code == 200
    assert response.json()["updated_fields"] == []


def test_patch_requires_authentication(app_settings: Settings) -> None:
    """未认证的 PATCH 得到 401（写端点受 `Session_Auth` 保护，R23.12）。"""
    with _anonymous(app_settings) as anonymous:
        response = anonymous.patch("/api/plans/PLAN-x", json={"status": "ACTIVE"})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHENTICATED"
