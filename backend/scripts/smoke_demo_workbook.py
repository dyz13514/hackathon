"""Exercise workbook import and the real API workflow in a temporary database."""

from __future__ import annotations

import argparse
from pathlib import Path
from tempfile import TemporaryDirectory

from fastapi.testclient import TestClient

from app.db.models import Base
from app.main import create_app
from app.settings import Settings


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("workbook", type=Path)
    arguments = parser.parse_args()
    workbook = arguments.workbook.resolve()
    with TemporaryDirectory(prefix="planning-workbook-smoke-") as temporary:
        settings = Settings(
            database_url=f"sqlite:///{(Path(temporary) / 'planning.db').as_posix()}",
            session_shared_password="smoke-test-password",
            session_secret_key="smoke-test-secret-key-longer-than-32-chars",
            llm_mode="STUB",
            app_env="LOCAL",
        )
        app = create_app(settings)
        Base.metadata.create_all(app.state.engine)
        with TestClient(app) as client:
            login = client.post(
                "/api/auth/login",
                json={"password": settings.session_shared_password.get_secret_value()},
            )
            assert login.status_code == 200, login.text
            preview = client.post(
                "/api/imports/package/preview",
                files={"file": (workbook.name, workbook.read_bytes())},
            )
            assert preview.status_code == 200, preview.text
            upload_id = preview.json()["upload_id"]
            confirm = client.post(
                f"/api/imports/package/{upload_id}/confirm", json={"confirm": True}
            )
            assert confirm.status_code == 200, confirm.text
            batch_id = confirm.json()["batch_id"]
            assert confirm.json()["imported_row_count"] == 89
            assert any(
                batch["batch_id"] == batch_id
                for batch in client.get("/api/imports").json()["batches"]
            )
            generated = client.post("/api/plans/generate", json={})
            assert generated.status_code == 200, generated.text
            plan = generated.json()
            assert plan["status"] == "PENDING_APPROVAL"
            assert plan["scheduled_jobs"] or plan["unschedulable_jobs"]
            approved = client.post(
                f"/api/plans/{plan['plan_id']}/approve",
                json={"expected_version": plan["plan_version"]},
            )
            assert approved.status_code == 200, approved.text
            assert client.get("/api/plans/active").json() is not None
            exported = client.post(f"/api/plans/{plan['plan_id']}/export?format=csv", json={})
            assert exported.status_code == 200, exported.text
            explanation = client.get(f"/api/plans/{plan['plan_id']}/explanation")
            assert explanation.status_code == 200, explanation.text
            assert explanation.json()["numeric_check"] in {"PASS", "FALLBACK"}
            assert explanation.json()["llm_mode"] == "STUB"
            for path in (
                "/api/state/dashboard", "/api/risks", "/api/insights/bottlenecks",
                "/api/value-ledger", "/api/preferences", "/api/traces",
            ):
                response = client.get(path)
                assert response.status_code == 200, (path, response.text)
            scenario = client.post(
                "/api/scenarios/run",
                json={"mutations": [{
                    "kind": "CHANGE_ORDER_PRIORITY", "order_id": "ORD-001",
                    "priority": "URGENT",
                }]},
            )
            assert scenario.status_code == 200, scenario.text
            restock = client.post(
                "/api/scenarios/run",
                json={"mutations": [{
                    "kind": "CHANGE_MATERIAL_AVAILABILITY",
                    "material_id": "MAT-STEEL-01", "quantity_available": 1000,
                }]},
            )
            assert restock.status_code == 200, restock.text
            assert restock.json()["feasibility"] == "FEASIBLE"
            for pending in client.get("/api/plans/pending").json():
                rejected = client.post(
                    f"/api/plans/{pending['plan_id']}/reject",
                    json={"rejection_reason": "Exercise scenario adoption separately"},
                )
                assert rejected.status_code == 200, rejected.text
            adopted = client.post(
                f"/api/scenarios/{scenario.json()['scenario_id']}/adopt", json={}
            )
            assert adopted.status_code == 200, adopted.text
            comparison = client.get(
                f"/api/plans/{plan['plan_id']}/compare/{adopted.json()['plan_id']}"
            )
            assert comparison.status_code == 200, comparison.text
            quote = client.post(
                "/api/quotes/promise-date",
                json={
                    "product_id": "PRD-BRACKET", "quantity": 1,
                    "desired_due_date": "2026-10-02T17:00:00",
                },
            )
            assert quote.status_code == 200, quote.text
            reverted = client.post(f"/api/imports/{batch_id}/revert", json={})
            assert reverted.status_code == 200, reverted.text
            assert reverted.json()["reverted_row_count"] == 89
            replay_preview = client.post(
                "/api/imports/package/preview",
                files={"file": (workbook.name, workbook.read_bytes())},
            )
            assert replay_preview.status_code == 200, replay_preview.text
            replayed = client.post(
                f"/api/imports/package/{replay_preview.json()['upload_id']}/confirm",
                json={"confirm": True},
            )
            assert replayed.status_code == 200, replayed.text
            print(
                {
                    "batch_id": batch_id,
                    "rows": confirm.json()["imported_row_count"],
                    "plan_id": plan["plan_id"],
                    "scheduled_jobs": len(plan["scheduled_jobs"]),
                    "unschedulable_jobs": len(plan["unschedulable_jobs"]),
                    "approved": approved.json()["status"],
                    "csv_bytes": len(exported.content),
                    "scenario": scenario.json()["scenario_id"],
                    "restock_feasibility": restock.json()["feasibility"],
                    "quote_feasible": quote.json()["feasible"],
                    "replayed_rows": replayed.json()["imported_row_count"],
                }
            )
        app.state.engine.dispose()
        app.state.audit_engine.dispose()


if __name__ == "__main__":
    main()
