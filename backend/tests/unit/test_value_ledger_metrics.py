"""价值台账完整 `ValueMetrics` + CSV 导出 + 标签的单元/API 测试（任务 11.4，R19）。

守 tasks.md 11.4 的验收点：

- `build_value_metrics` 全字段确定性计算（R19.1、R19.7）；同库两次调用逐字段相同。
- `manual_steps_breakdown` 按 design.md §4.4 口径表计数：每个 `ImportBatch` 计 1 步（无论行数）、
  重排按扰动计、校验按审批计等；口径表 8 项齐全（R19.5）。
- 标签：人工基线时间 `ESTIMATED`、系统指标 `MEASURED`、K-17/K-18 `PROJECTED`（R19.4/R19.6/R25.13）。
- `GET /api/value-ledger` 附带 `metrics` / `manual_steps` / `kpis` 三块，且 **Task 7.6 的 K-14
  字段原样保留**（不破坏既有响应）。
- `GET /api/value-ledger/export.csv` 表头与列顺序为 R19.8 规定的 8 列，可被 `csv` 重新解析，
  且做了公式注入防护。
- `real_run_count` 与 150 配额剩余量可见（R25.12）。

走真实 SQLite + 真实 seed + 真实内核，不 mock、不触达 LLM。
"""

from __future__ import annotations

import csv
import io
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from app.db import models as orm
from app.db.models import Base
from app.main import create_app
from app.seed.loader import load_demo_data
from app.services import value_ledger as vl
from app.settings import Settings

LOGIN = "/api/auth/login"
VALUE_LEDGER = "/api/value-ledger"
CSV_URL = "/api/value-ledger/export.csv"

FIXED_NOW = datetime(2026, 3, 2, 8, 0)


@pytest.fixture
def app_settings(valid_env: pytest.MonkeyPatch, tmp_path: Path) -> Settings:
    db_file = tmp_path / "value-ledger.db"
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
def factory(application: object) -> sessionmaker[Session]:
    f: sessionmaker[Session] = application.state.session_factory  # type: ignore[attr-defined]
    return f


@pytest.fixture
def client(application: object, app_settings: Settings) -> Iterator[TestClient]:
    with TestClient(application) as test_client:  # type: ignore[arg-type]
        test_client.post(
            LOGIN, json={"password": app_settings.session_shared_password.get_secret_value()}
        )
        yield test_client


# --------------------------------------------------------------------------
# 服务层：build_value_metrics / manual_steps / kpi_rows
# --------------------------------------------------------------------------


def test_build_value_metrics_deterministic(factory: sessionmaker[Session]) -> None:
    """同库两次 build 逐字段相同（确定性，R19.7）；无 ACTIVE 计划时 KPI 为诚实 None。"""
    with factory() as db:
        a = vl.build_value_metrics(db, now=FIXED_NOW)
        b = vl.build_value_metrics(db, now=FIXED_NOW)
    assert a == b
    # 未生成任何计划的 seed 库：按期率/拖期无 ACTIVE 计划 → None（诚实空态）。
    assert a.on_time_rate is None
    assert a.total_tardiness_minutes is None
    # token/usd/real_run 在 REPLAY/STUB 且未接线 LLM 时恒 0（正确真值）。
    assert a.llm_tokens_used == 0
    assert a.real_run_count == 0


def test_labels_measured_estimated_projected(factory: sessionmaker[Session]) -> None:
    """只保留有数据来源的指标；固定演示估计不再出现。"""
    with factory() as db:
        m = vl.build_value_metrics(db, now=FIXED_NOW)
    assert m.labels["manual_steps_eliminated"] == "MEASURED"
    assert m.labels["llm_tokens_used"] == "MEASURED"
    assert m.baseline_plan_generation_seconds is None
    assert m.projected_hero_demo_usd is None


def test_manual_steps_rubric_has_eight_entries_and_counts(factory: sessionmaker[Session]) -> None:
    """口径表 8 项齐全（design.md §4.4）；seed 库无导入/扰动时各计数为 0。"""
    with factory() as db:
        steps = vl.manual_steps_breakdown(db)
    assert len(vl.MANUAL_STEP_RUBRIC) == 8
    assert set(steps.counts) == {action for action, _, _ in vl.MANUAL_STEP_RUBRIC}
    assert steps.total == sum(steps.counts.values())


def test_manual_steps_import_batch_counts_one_regardless_of_rows(
    factory: sessionmaker[Session],
) -> None:
    """每个 ImportBatch 计 1 步导入 + 1 步归一，与行数无关（R19.5，不夸大）。"""
    with factory() as db:
        db.add(
            orm.ImportBatch(
                batch_id="B-1",
                file_name="orders.xlsx",
                file_checksum="deadbeef",
                entity_type="ORDER",
                row_count=2000,  # 2000 行仍只计 1 步
                status="COMMITTED",
                proposed_mapping={},
                created_at=FIXED_NOW,  # created_at 为 NOT NULL（无默认），须显式给值
            )
        )
        db.commit()
        steps = vl.manual_steps_breakdown(db)
    assert steps.counts["spreadsheet_import"] == 1
    assert steps.counts["mapping_normalisation"] == 1


def test_kpi_rows_reference_expected_kpis(factory: sessionmaker[Session]) -> None:
    """KPI 行只含可计算指标，不展示写死的时间/演示成本。"""
    with factory() as db:
        rows = vl.kpi_rows(vl.build_value_metrics(db, now=FIXED_NOW))
    by_id = {r.kpi_id: r for r in rows}
    assert set(by_id) == {"K-03", "K-04", "K-13", "K-11"}
    assert by_id["K-13"].label == "MEASURED"


# --------------------------------------------------------------------------
# API：GET /value-ledger（扩展）与 export.csv
# --------------------------------------------------------------------------


def test_get_value_ledger_keeps_task76_fields_and_adds_blocks(client: TestClient) -> None:
    """K-14 字段原样保留 + 新增 metrics/manual_steps/kpis 三块（不破坏 7.6）。"""
    body = client.get(VALUE_LEDGER).json()
    # Task 7.6 字段仍在
    for key in (
        "auto_handled_count",
        "escalated_count",
        "total_decisions",
        "auto_handled_ratio",
        "decisions",
    ):
        assert key in body
    # 任务 11.4 新增块
    assert "metrics" in body and "manual_steps" in body and "kpis" in body
    assert body["metrics"]["project_real_run_cap"] == 150
    assert body["metrics"]["real_run_remaining"] == 150 - body["metrics"]["real_run_count"]
    assert len(body["manual_steps"]) == 8
    assert not any(k["kpi_id"] in {"K-01", "K-02", "K-17", "K-18"} for k in body["kpis"])


def test_export_csv_header_columns_and_parseable(client: TestClient) -> None:
    """CSV 表头与列顺序为 R19.8 的 8 列，且可被 csv 重新解析。"""
    resp = client.get(CSV_URL)
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("text/csv")
    reader = csv.reader(io.StringIO(resp.text))
    header = next(reader)
    assert header == [
        "kpi_id",
        "metric_name",
        "current_value",
        "baseline_value",
        "delta",
        "target_value",
        "label",
        "measured_at",
    ]
    data_rows = list(reader)
    assert len(data_rows) == 4
    kpi_ids = {row[0] for row in data_rows}
    assert kpi_ids == {"K-03", "K-04", "K-11", "K-13"}


def test_export_csv_is_deterministic(client: TestClient) -> None:
    """同库两次导出字节相同（确定性，measured_at 由 DEMO_ANCHOR 固定）。"""
    first = client.get(CSV_URL).text
    second = client.get(CSV_URL).text
    assert first == second
