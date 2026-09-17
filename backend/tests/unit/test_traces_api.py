"""可观测性端点的 API 测试（任务 5.12，design.md Components §5「观测」分组）。

覆盖三个只读端点与它们的筛选 / 关联 / 只读性质：

- `GET /traces` —— 列出运行；可按 Agent / 触发类型筛选（R24.2）。
- `GET /traces/{id}` —— 全文含逐步与工具调用；不存在返回 `TRACE_NOT_FOUND`；顶部有 `mode`。
- `GET /audit-log` —— 只读查询；`POST/PATCH/DELETE` 均不存在（append-only，R24.3）。

走真实 SQLite 文件与真实 seed，并用 `POST /plans/generate` 造出一条真实的 PIPELINE Trace
（六步 + 计划回链），因此断言的是「HTTP 层把 `Trace_Recorder` 落的库序列化成了什么」，不 mock。
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import Base
from app.main import create_app
from app.seed.loader import load_demo_data
from app.settings import Settings

LOGIN = "/api/auth/login"
GENERATE = "/api/plans/generate"
TRACES = "/api/traces"
AUDIT = "/api/audit-log"


@pytest.fixture
def app_settings(valid_env: pytest.MonkeyPatch, tmp_path: Path) -> Settings:
    db_file = tmp_path / "traces-api.db"
    valid_env.setenv("DATABASE_URL", f"sqlite:///{db_file.as_posix()}")
    return Settings()  # type: ignore[call-arg]


@pytest.fixture
def client(app_settings: Settings) -> Iterator[TestClient]:
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


def _generate_plan(client: TestClient) -> str:
    """造一条真实的 PIPELINE Trace，返回其 trace_id。"""
    body = client.post(GENERATE, json={}).json()
    return body["generated_by_trace_id"]


# --------------------------------------------------------------------------
# GET /traces
# --------------------------------------------------------------------------


def test_list_traces_returns_pipeline_run(client: TestClient) -> None:
    """生成一个计划后，列表里出现那条 PIPELINE Trace，头部字段齐全（R24.1）。"""
    trace_id = _generate_plan(client)
    response = client.get(TRACES)
    assert response.status_code == 200, response.text
    rows = response.json()
    match = [r for r in rows if r["trace_id"] == trace_id]
    assert len(match) == 1
    row = match[0]
    assert row["mode"] == "PIPELINE"
    assert row["agent"] is None
    assert row["trigger_source"] == "PLANNER_UI"
    assert row["outcome"] == "OK"
    assert row["step_count"] == 6


def test_list_traces_filter_by_agent_excludes_pipeline(client: TestClient) -> None:
    """按 Agent 筛选（REACT 路径）时，PIPELINE（agent=NULL）的 Trace 不出现（R24.2）。"""
    trace_id = _generate_plan(client)
    rows = client.get(TRACES, params={"agent": "PLANNING_AGENT"}).json()
    assert all(r["trace_id"] != trace_id for r in rows)


def test_list_traces_filter_by_trigger_source(client: TestClient) -> None:
    """按触发类型筛选：PLANNER_UI 命中，RISK_SCAN 不命中（R24.2）。"""
    trace_id = _generate_plan(client)
    hit = client.get(TRACES, params={"trigger_source": "PLANNER_UI"}).json()
    miss = client.get(TRACES, params={"trigger_source": "RISK_SCAN"}).json()
    assert any(r["trace_id"] == trace_id for r in hit)
    assert all(r["trace_id"] != trace_id for r in miss)


# --------------------------------------------------------------------------
# GET /traces/{id}
# --------------------------------------------------------------------------


def test_get_trace_detail_shows_mode_and_six_steps(client: TestClient) -> None:
    """详情顶部标注 mode，逐步显示 6 个确定性阶段与 decision_reason（R24.1、R24.7）。"""
    trace_id = _generate_plan(client)
    response = client.get(f"{TRACES}/{trace_id}")
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["mode"] == "PIPELINE"
    assert len(body["steps"]) == 6
    assert [s["step_index"] for s in body["steps"]] == [0, 1, 2, 3, 4, 5]
    assert body["steps"][0]["decision_reason"] == "load_snapshot"
    assert body["steps"][5]["decision_reason"] == "save_proposed_plan"
    assert all(s["step_kind"] == "DETERMINISTIC_STAGE" for s in body["steps"])


def test_get_trace_not_found(client: TestClient) -> None:
    """未知 trace_id 返回 404 + TRACE_NOT_FOUND。"""
    response = client.get(f"{TRACES}/TRACE-does-not-exist")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "TRACE_NOT_FOUND"


# --------------------------------------------------------------------------
# GET /audit-log（只读）
# --------------------------------------------------------------------------


def test_audit_log_lists_plan_generation_event(client: TestClient) -> None:
    """生成计划写了一条 PLAN_GENERATION 审计，只读端点能查到它。"""
    _generate_plan(client)
    rows = client.get(AUDIT, params={"event_category": "PLAN_GENERATION"}).json()
    assert len(rows) >= 1
    assert all(r["event_category"] == "PLAN_GENERATION" for r in rows)
    assert rows[0]["event_type"] == "PLAN_GENERATED"


def test_audit_log_has_no_write_endpoint(client: TestClient) -> None:
    """审计日志无写接口（R24.3）：POST / PATCH / DELETE 到 /audit-log 都不是已定义路由。

    路由未定义时 FastAPI 返回 405（方法不允许）或 404——两者都表示「这条写路径不存在」。
    """
    assert client.post(AUDIT, json={}).status_code in {404, 405}
    assert client.patch(AUDIT, json={}).status_code in {404, 405}
    assert client.delete(AUDIT).status_code in {404, 405}
