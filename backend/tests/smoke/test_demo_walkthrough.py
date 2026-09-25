"""录屏主线冒烟：隔离数据库、离线 LLM，不触碰开发者的演示库。"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app.db.models import Base
from app.main import create_app
from app.settings import Settings


SAMPLES = Path(__file__).resolve().parents[2] / "app" / "seed" / "samples"


def _ok(response):
    assert response.status_code == 200, response.text
    return response.json()


def test_demo_walkthrough(valid_env, tmp_path: Path) -> None:
    valid_env.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'walkthrough.db').as_posix()}")
    valid_env.setenv("UPLOAD_DIR", str(tmp_path / "uploads"))
    settings = Settings()
    application = create_app(settings)
    Base.metadata.create_all(application.state.engine)

    with TestClient(application) as client:
        sample = SAMPLES / "demo_material_chinese_columns.csv"
        unauth = client.post(
            "/api/imports/upload", files={"file": (sample.name, sample.read_bytes(), "text/csv")}
        )
        assert unauth.status_code == 401
        assert unauth.json()["error"]["code"] == "UNAUTHENTICATED"

        _ok(client.post("/api/auth/login", json={"password": "test-shared-password"}))
        _ok(client.post("/api/demo/reset"))
        dashboard_before = _ok(client.get("/api/state/dashboard"))

        uploaded = _ok(client.post(
            "/api/imports/upload", files={"file": (sample.name, sample.read_bytes(), "text/csv")}
        ))
        proposal = _ok(client.get(f"/api/imports/{uploaded['upload_id']}/proposal"))["proposal"]
        assert proposal["entity_type"] == "MATERIAL"
        assert not proposal["missing_required_fields"]
        mapping = {"field_mappings": proposal["field_mappings"]}
        validation = _ok(client.post(
            f"/api/imports/{uploaded['upload_id']}/validate", json={"mapping": mapping}
        ))
        assert validation["parsed_row_count"] == uploaded["total_rows"] == 2
        assert not validation["unparsed_cells"]
        committed = _ok(client.post(f"/api/imports/{uploaded['upload_id']}/confirm", json={
            "entity_type": "MATERIAL",
            "field_mappings": proposal["field_mappings"],
            "unparsed_cells_resolved": True,
        }))
        assert committed["imported_row_count"] == 2
        assert any(b["batch_id"] == committed["batch_id"] for b in _ok(client.get("/api/imports"))["batches"])
        _ok(client.get("/api/state/dashboard"))
        reverted = _ok(client.post(f"/api/imports/{committed['batch_id']}/revert", json={}))
        assert reverted["reverted_row_count"] == 2
        def material_values(dashboard):
            return {
                row["material_id"]: (row["name"], row["unit"], row["quantity_available"], row["source"])
                for row in dashboard["materials"]
            }

        assert material_values(_ok(client.get("/api/state/dashboard"))) == material_values(dashboard_before)

        plan = _ok(client.post("/api/plans/generate", json={}))
        assert plan["scheduled_jobs"] and plan["baseline_comparison"]
        approved = _ok(client.post(f"/api/plans/{plan['plan_id']}/approve", json={
            "expected_version": plan["plan_version"]
        }))
        assert approved["status"] == "ACTIVE"
        scenario = _ok(client.post("/api/scenarios/run", json={"mutations": [{
            "kind": "SET_MACHINE_UNAVAILABLE", "machine_id": "CNC-01",
            "start_time": "2026-03-03T08:00:00", "end_time": "2026-03-03T14:00:00",
        }]}))
        assert "late_order_count_delta" in scenario
        assert any(p["plan_id"] == plan["plan_id"] for p in _ok(client.get("/api/plans/active")))
        quote = _ok(client.post("/api/quotes/promise-date", json={
            "product_id": "PRD-BRACKET", "quantity": 5,
            "desired_due_date": "2026-03-04T17:00:00",
        }))
        assert "earliest_completion" in quote
        _ok(client.post("/api/risks/scan", json={}))
        _ok(client.get("/api/traces"))
        ledger = _ok(client.get("/api/value-ledger"))
        assert "kpis" in ledger
