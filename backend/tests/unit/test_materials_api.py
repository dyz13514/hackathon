"""A material correction changes current inputs, not historical plans or traces."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.db.models import Base
from app.main import create_app
from app.seed.loader import load_demo_data
from app.settings import Settings


def test_material_availability_edit_is_authenticated_versioned_and_audited(
    valid_env: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    valid_env.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'materials.db').as_posix()}")
    settings = Settings()
    app = create_app(settings)
    Base.metadata.create_all(app.state.engine)
    with app.state.session_factory() as db:
        load_demo_data(db)
        db.commit()
    with TestClient(app) as client:
        path = "/api/materials/MAT-STEEL-01/availability"
        before = client.get("/api/materials/MAT-STEEL-01")
        assert before.status_code == 200
        old = before.json()
        unauthenticated = client.patch(path, json={
            "quantity_available": 1000, "reason": "Inventory recount",
        })
        assert unauthenticated.status_code == 401
        client.post(
            "/api/auth/login",
            json={"password": settings.session_shared_password.get_secret_value()},
        )
        changed = client.patch(path, json={
            "quantity_available": 1000, "reason": "Inventory recount",
        })
        assert changed.status_code == 200, changed.text
        assert float(changed.json()["quantity_available"]) == 1000
        assert changed.json()["input_snapshot_version"] > old["input_snapshot_version"]
        assert client.get("/api/materials/MAT-STEEL-01").json() == changed.json()
        assert client.patch(path, json={
            "quantity_available": -1, "reason": "Invalid negative quantity",
        }).status_code == 422
        assert client.get("/api/materials/NOT-FOUND").status_code == 404
        with app.state.session_factory() as db:
            from app.db.models import AuditLog
            from sqlalchemy import select

            assert db.scalar(select(AuditLog).where(
                AuditLog.event_type == "MATERIAL_AVAILABILITY_EDIT"
            )) is not None
    app.state.engine.dispose()
    app.state.audit_engine.dispose()
