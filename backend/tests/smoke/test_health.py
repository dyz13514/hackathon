"""`GET /health` 冒烟（R27.6、tasks.md 1.7、design.md Testing Strategy §1）。

四件事被钉住：

1. **字段集合恰好六个**，`prompt_caching_available` 不得回来（prompt caching 全套机具
   已移出范围）。这是一条反向断言：健康检查是最容易被顺手加字段的端点，而它无认证，
   多一个字段就是多一寸泄漏面。
2. **两个路径都可用**：`/health`（本机探针）与 `/api/health`（经反向代理的前端）。
3. **数据库不可用时不崩、不返 5xx**，只把 `db_ok` 与 `status` 翻过来。
4. **计数器口径**：`real_run_count` 只数 `mode != 'REPLAY'` 的 trace，`project_usd_spent`
   是 `estimated_usd` 的和。回放运行计入支出为 0，因此这两个数字在 CI 里恒为 0。
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api.admin import PROJECT_REAL_RUN_CAP, count_real_runs
from app.db.models import Base, Trace
from app.main import create_app
from app.settings import Settings

#: design.md「运维要点」点名的六个字段，一个不多一个不少。
EXPECTED_FIELDS = {
    "status",
    "mode",
    "db_ok",
    "llm_mode",
    "project_usd_spent",
    "real_run_count",
}


@pytest.fixture
def app_with_db(valid_env: pytest.MonkeyPatch, tmp_path: Path) -> TestClient:
    """指向一个已建表的临时 SQLite 库的应用。"""
    db_file = tmp_path / "health.db"
    valid_env.setenv("DATABASE_URL", f"sqlite:///{db_file.as_posix()}")
    settings = Settings()  # type: ignore[call-arg]  # 值由环境变量提供
    application = create_app(settings)
    Base.metadata.create_all(application.state.engine)
    return TestClient(application)


def _make_trace(trace_id: str, mode: str, usd: str) -> Trace:
    return Trace(
        trace_id=trace_id,
        kind="GENERATE_PLAN",
        mode=mode,
        agent=None,
        trigger_source="PLANNER",
        session_id="SESS-0001",
        started_at=datetime(2025, 3, 1, 8, 0, tzinfo=UTC),
        estimated_usd=Decimal(usd),
    )


def test_health_returns_exactly_the_six_documented_fields(
    app_with_db: TestClient,
) -> None:
    response = app_with_db.get("/health")
    assert response.status_code == 200

    body = response.json()
    assert set(body) == EXPECTED_FIELDS
    assert "prompt_caching_available" not in body


def test_health_reports_a_reachable_database(app_with_db: TestClient) -> None:
    body = app_with_db.get("/health").json()
    assert body["db_ok"] is True
    assert body["status"] == "OK"
    assert body["llm_mode"] == "STUB"  # conftest 的 VALID_ENV
    assert body["mode"] == "NORMAL"
    assert body["project_usd_spent"] == 0.0
    assert body["real_run_count"] == 0


def test_health_is_available_under_both_paths(app_with_db: TestClient) -> None:
    """`/health` 给本机探针，`/api/health` 给经反向代理的前端。"""
    assert app_with_db.get("/health").json() == app_with_db.get("/api/health").json()


def test_only_non_replay_traces_count_against_the_real_run_cap(
    app_with_db: TestClient,
) -> None:
    """配额口径：回放不烧钱，因此不计数；`estimated_usd` 照常累加。"""
    factory = app_with_db.app.state.session_factory  # type: ignore[attr-defined]
    with factory() as session:
        session.add_all(
            [
                _make_trace("TRC-0001", "REPLAY", "0"),
                _make_trace("TRC-0002", "LIVE", "0.14"),
                _make_trace("TRC-0003", "LIVE", "0.06"),
            ]
        )
        session.commit()

    body = app_with_db.get("/health").json()
    assert body["real_run_count"] == 2
    assert body["project_usd_spent"] == pytest.approx(0.20)
    assert count_real_runs(app_with_db.app.state.engine) == 2  # type: ignore[attr-defined]


def test_real_run_cap_is_one_hundred_and_fifty() -> None:
    """`PROJECT_REAL_RUN_CAP` 是成本纪律的硬闸门（design.md 成本章节 ②、ADR-011）。

    数字写死在断言里是刻意的：改动它意味着改动 K-11 的成本论证，应当是一次显式决定。
    """
    assert PROJECT_REAL_RUN_CAP == 150


def test_health_reports_degraded_instead_of_crashing_when_db_is_unreachable(
    valid_env: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """库打不开时返回 200 + `status=DEGRADED`，而不是 5xx。

    理由见 `api/admin.py` 的模块 docstring：迁移没跑或文件权限不对，重启一百次也不会
    好，返回 5xx 只会把可读的诊断信息换成一串重启。
    """
    missing_dir = tmp_path / "not-created" / "health.db"
    valid_env.setenv("DATABASE_URL", f"sqlite:///{missing_dir.as_posix()}")
    client = TestClient(create_app(Settings()))  # type: ignore[call-arg]

    body = client.get("/health").json()
    assert body["db_ok"] is False
    assert body["status"] == "DEGRADED"
    assert body["project_usd_spent"] == 0.0
    assert body["real_run_count"] == 0


def test_health_reports_deterministic_only_mode(
    valid_env: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`LLM_MODE=DISABLED` 即 `DETERMINISTIC_ONLY`（design.md §2.6：旁路点只有一个）。"""
    db_file = tmp_path / "disabled.db"
    valid_env.setenv("DATABASE_URL", f"sqlite:///{db_file.as_posix()}")
    valid_env.setenv("LLM_MODE", "DISABLED")
    application = create_app(Settings())  # type: ignore[call-arg]
    Base.metadata.create_all(application.state.engine)

    body = TestClient(application).get("/health").json()
    assert body["mode"] == "DETERMINISTIC_ONLY"
    assert body["llm_mode"] == "DISABLED"
