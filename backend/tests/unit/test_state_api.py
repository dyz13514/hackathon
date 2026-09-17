"""状态看板端点的 API 测试（任务 3.7，design.md Components §6 `/` 行）。

覆盖 `GET /api/state/dashboard`：

- 返回五类实体（Order / Material / Machine / Worker / ProductionPlan），每条带
  `source` 与 `last_updated_at`（R1.1、R1.3）。
- 只读端点，未认证也可访问（R1 状态可见性，与 `GET /plans/active` 同口径）。
- Order 的 `notes` 与 `injection_suspected` 原样透传，供前端打 `untrusted` 徽章（R1.4）。
- 生成计划后，计划出现在 `plans` 列（`source` = origin、`last_updated_at` = created_at）。

走真实 SQLite 文件与真实 seed 数据（同 `test_plans_api.py`），不 mock。
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import Base
from app.main import create_app
from app.seed.dataset import INJECTION_DEMO_ORDER_ID
from app.seed.loader import load_demo_data
from app.settings import Settings

LOGIN = "/api/auth/login"
DASHBOARD = "/api/state/dashboard"
GENERATE = "/api/plans/generate"


@pytest.fixture
def app_settings(valid_env: pytest.MonkeyPatch, tmp_path: Path) -> Settings:
    db_file = tmp_path / "state-api.db"
    valid_env.setenv("DATABASE_URL", f"sqlite:///{db_file.as_posix()}")
    return Settings()  # type: ignore[call-arg]


@pytest.fixture
def client(app_settings: Settings) -> Iterator[TestClient]:
    """已建表、已铺 seed、已登录的客户端。"""
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


def test_dashboard_returns_five_entity_groups(client: TestClient) -> None:
    """返回五类实体分组，均非空（seed 数据下）。"""
    response = client.get(DASHBOARD)
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {"orders", "materials", "machines", "workers", "plans"}
    assert body["orders"], "seed 下应有订单"
    assert body["materials"]
    assert body["machines"]
    assert body["workers"]


def test_every_entity_carries_source_and_last_updated(client: TestClient) -> None:
    """五类实体的每一条都带 `source` 与 `last_updated_at`（R1.3）。"""
    body = client.get(DASHBOARD).json()
    for group in ("orders", "materials", "machines", "workers"):
        for row in body[group]:
            assert row["source"] == "SEED_DATA"
            assert row["last_updated_at"]


def test_dashboard_is_readable_without_authentication(app_settings: Settings) -> None:
    """看板是只读端点，未认证也能拿到数据（R1 状态可见性）。"""
    with _anonymous(app_settings) as anonymous:
        response = anonymous.get(DASHBOARD)
    assert response.status_code == 200
    assert response.json()["orders"]


def test_order_notes_and_injection_flag_pass_through(client: TestClient) -> None:
    """`notes` 与 `injection_suspected` 原样透传，供前端打 `untrusted` 徽章（R1.4）。"""
    body = client.get(DASHBOARD).json()
    by_id = {o["order_id"]: o for o in body["orders"]}
    adversarial = by_id[INJECTION_DEMO_ORDER_ID]
    # seed 的对抗订单 notes 非空；injection_suspected 由 Guardrail 置位，seed 不越权，
    # 因此这里断言字段存在而非其真值。
    assert adversarial["notes"]
    assert "injection_suspected" in adversarial


def test_generated_plan_appears_in_plans_group(client: TestClient) -> None:
    """生成计划后出现在 `plans` 列，`source` = origin、`last_updated_at` = created_at。"""
    generated = client.post(GENERATE, json={}).json()
    body = client.get(DASHBOARD).json()
    by_id = {p["plan_id"]: p for p in body["plans"]}
    assert generated["plan_id"] in by_id
    plan_row = by_id[generated["plan_id"]]
    assert plan_row["source"] == "PLAN_GENERATION"
    assert plan_row["status"] == "PENDING_APPROVAL"
    assert plan_row["last_updated_at"]
