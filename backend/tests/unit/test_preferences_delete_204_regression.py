"""回归：删除端点的 HTTP 204 契约 + 偏好写路径审计不死锁（任务 13 前置修复）。

本文件锁住两个此前被同一处缺陷掩盖、修复后必须保持绿色的事实：

1. **204 无响应体契约（fastapi==0.115.5 导入期约束）**：`app/api/preferences.py` 的删除端点
   曾声明 `status_code=204` 却让处理函数返回带响应体的 `JSONResponse`。在锁定的 fastapi 版本下，
   「204 不得携带响应体」这条校验在 **import 期**即抛 `AssertionError`，连带 `app.main` /
   `app.api` / （经 `app.api.admin`）`app.llm.budget` 都无法导入，整个应用与 TestClient 起不来。
   修复是把成功路径显式返回一个无响应体的 `Response(status_code=204)`，把「不存在」的 404 JSON
   信封分开处理（404 允许带响应体）。本文件断言：删除成功 → 204 且响应体为空；删除不存在 → 404
   且带错误码信封；并且 `app.main` 可导入（导入本模块顶部的 `create_app` 就已隐式验证这一点）。

2. **审计不与业务写自我死锁（SQLite 单写者约束）**：偏好写路径（create / update / enable /
   disable / delete）每次都要写一条 `PREFERENCE_RULE_CHANGE` 审计。审计经 `db/audit.py` 的
   **独立引擎、独立事务**落库，而 SQLite（即便 WAL）同一时刻只允许一个写者。此前服务层在业务
   事务**仍持有写锁**时就调用 `audit.append()`，同一线程的审计连接会自我死锁到 `busy_timeout`
   后抛 `database is locked`——因此一次干净的 `POST /api/preferences` 都会 500。修复是让服务层
   「先 commit 业务事务、再写审计」（与 `ApprovalService` 既有口径一致）。本文件断言：一次
   `POST /api/preferences` 在真实文件型 SQLite（业务引擎与审计引擎为两个引擎）上成功返回 201，
   不再抛 `database is locked`。

走真实 `create_app` 装配 + 真实文件型 SQLite（复现两个引擎的锁竞争），不 mock。
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.db.audit import set_audit_engine
from app.db.models import Base
from app.main import create_app
from app.settings import Settings

LOGIN = "/api/auth/login"
PREFS = "/api/preferences"

AVOID_ORDER = {
    "kind": "AVOID_MACHINE_FOR_ORDER",
    "order_id": "ORD-007",
    "machine_id": "CNC-03",
    "weight_delta": 2.0,
}


@pytest.fixture
def app_settings(valid_env: pytest.MonkeyPatch, tmp_path: Path) -> Settings:
    # 文件型库（非 :memory:）：只有文件型库才有「业务引擎 + 审计引擎」两个连接争抢同一写锁的
    # 现象，死锁回归必须在文件型库上复现。
    db_file = tmp_path / "prefs_regression.db"
    valid_env.setenv("DATABASE_URL", f"sqlite:///{db_file.as_posix()}")
    return Settings()  # type: ignore[call-arg]


@pytest.fixture
def client(app_settings: Settings) -> Iterator[TestClient]:
    app = create_app(app_settings)
    Base.metadata.create_all(app.state.engine)
    with TestClient(app) as test_client:
        password = app_settings.session_shared_password.get_secret_value()
        test_client.post(LOGIN, json={"password": password})
        yield test_client
    set_audit_engine(None)


def _create_rule(client: TestClient) -> str:
    resp = client.post(
        PREFS, json={"human_text": "ORD-007 避开 CNC-03", "structured_form": AVOID_ORDER}
    )
    assert resp.status_code == 201, resp.text
    return str(resp.json()["rule_id"])


def test_post_preference_does_not_deadlock_on_audit(client: TestClient) -> None:
    """一次干净的 POST 不再因审计连接与业务写锁自我死锁而 500（database is locked）。"""
    resp = client.post(PREFS, json={"human_text": "x", "structured_form": AVOID_ORDER})
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["enabled"] is False


def test_delete_existing_rule_returns_204_with_empty_body(client: TestClient) -> None:
    """删除成功 → 204，且响应体为空（HTTP 204 不得携带响应体）。"""
    rule_id = _create_rule(client)
    resp = client.request("DELETE", f"{PREFS}/{rule_id}")
    assert resp.status_code == 204, resp.text
    assert resp.content == b""
    # 删除生效：再取为 404。
    assert client.get(f"{PREFS}/{rule_id}").status_code == 404


def test_delete_missing_rule_returns_404_envelope(client: TestClient) -> None:
    """删除不存在的规则 → 404，且带标准错误码信封（404 允许带响应体）。"""
    resp = client.request("DELETE", f"{PREFS}/PR-does-not-exist")
    assert resp.status_code == 404, resp.text
    assert resp.json()["error"]["code"] == "PREFERENCE_RULE_NOT_FOUND"
